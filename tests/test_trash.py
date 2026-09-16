"""Trash moves are confirmed, scoped and recoverable; never touch live mail."""

from collections import OrderedDict, deque
from concurrent.futures import Future
import curses
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from test_inbox import FakeScreen, inbox, row


def message(uid: str = '1', **values) -> dict:
    return dict(row('test', uid, 'message-' + uid), mailbox='INBOX', flags=[], references=[], uidvalidity=10, **values)


class TrashTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = {account: {'imap': {}, 'mailbox': {'alias': {'inbox': 'INBOX', 'trash': folder}}}
                    for account, folder in [('test', 'Trash'), ('other', 'Deleted Items')]}
        for name, value in [('SETTINGS', settings), ('TRASHED_MESSAGES', set()), ('SEEN_STATES', {}),
                            ('SEEN_QUEUE', deque()), ('REFRESH_JOB', None), ('REFRESH_SEEN_STATES', {}),
                            ('FOLDER_CACHE', {}), ('HEADER_CACHE_PATH', None), ('REFERENCE_CACHE', {}), ('CONTACTS', []),
                            ('MESSAGE_CACHE', OrderedDict()), ('MESSAGE_READS', {}), ('IO_STOP', Event()),
                            ('PREFETCH_ENABLED', False), ('PREFETCH_JOB', None), ('PREFETCH_KEY', None),
                            ('AUTO_REFRESH_ENABLED', False)]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)
        self.transport_patch = patch.object(inbox, 'run_private', return_value=b'')
        self.transport = self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)
        refresh_patch = patch.object(inbox, 'start_refresh')
        self.refresh = refresh_patch.start()
        self.addCleanup(refresh_patch.stop)
        states_patch = patch.object(inbox, 'mailbox_states', return_value={
            name: {'UIDVALIDITY': 10} for name in ('INBOX', 'Sent', 'Archive')})
        self.states = states_patch.start()
        self.addCleanup(states_patch.stop)

    def browse(self, screen, rows, **kwargs) -> None:
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'):
            inbox.browse(screen, rows, **kwargs)

    def test_move_command_uses_exact_account_folder_uid_and_configured_trash(self) -> None:
        item = dict(message('42'), account='other', mailbox='Archive')
        inbox.move_to_trash(item, 'Deleted Items')
        self.transport.assert_called_once_with([
            '--account', 'other', 'message', 'move', '--from', 'Archive', '--to', 'Deleted Items', '--', '42'])
        self.states.assert_called_once_with('other', ['Archive'])

    def test_missing_or_unsafe_trash_alias_fails_without_a_move(self) -> None:
        for destination in [None, '', ' ', 'Trash\nINBOX']:
            inbox.SETTINGS['test']['mailbox']['alias']['trash'] = destination
            status = inbox.trash_message(FakeScreen([]), message())
            self.assertIn('Configure mailbox.alias.trash', status)
        self.transport.assert_not_called()

    def test_trash_alias_is_case_insensitive(self) -> None:
        inbox.SETTINGS['test']['mailbox']['alias'] = {'TrAsH': 'Bin'}
        self.assertEqual(inbox.trash_folder(message()), 'Bin')

    def test_messages_already_in_trash_are_never_deleted(self) -> None:
        for mailbox in ('Trash', 'trash', 'TRASH'):
            item = dict(message(), mailbox=mailbox)
            self.assertIn('already in Trash', inbox.trash_message(FakeScreen([]), item))
        inbox.SETTINGS['test']['mailbox']['alias']['trash'] = 'Deleted Items'
        self.assertIn('already in Trash', inbox.trash_message(FakeScreen([]), dict(message(), mailbox='trash')))
        self.transport.assert_not_called()

    def test_destination_cannot_change_after_confirmation(self) -> None:
        with self.assertRaises(RuntimeError):
            inbox.move_to_trash(message(), 'Unconfirmed folder')
        self.transport.assert_not_called()

    def test_cancel_has_no_move_or_local_changes(self) -> None:
        for key in ('n', 'N', 'q', '\x1b'):
            status = inbox.trash_message(FakeScreen([key]), message())
            self.assertIn('Cancelled', status)
        self.transport.assert_not_called()
        self.states.assert_not_called()
        self.refresh.assert_not_called()
        self.assertFalse(inbox.TRASHED_MESSAGES)

    def test_confirmation_identifies_the_single_message_and_destination(self) -> None:
        rendered = []
        screen = FakeScreen(['n'])
        screen.addnstr = lambda y, x, text, *args: rendered.append(text)
        inbox.trash_message(screen, dict(message(), subject='Specific synthetic subject'))
        text = '\n'.join(rendered)
        for marker in ('Specific synthetic subject', 'Account: test', 'INBOX -> Trash', 'Only this message'):
            self.assertIn(marker, text)

    def test_success_prunes_source_cache_body_and_seen_overlay(self) -> None:
        item, keep = message(), message('2')
        key = inbox.message_cache_key(item)
        inbox.MESSAGE_CACHE[key] = 'SYNTHETIC_BODY'
        inbox.SEEN_STATES[key] = {'pending': 0, 'seen': True, 'confirmed': True, 'saved': True}
        inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': {'UIDVALIDITY': 10}, 'rows': [item, keep]}
        status = inbox.trash_message(FakeScreen(['y']), item)
        self.assertIn('Moved to Trash', status)
        self.assertEqual(inbox.FOLDER_CACHE['test', 'INBOX']['rows'], [keep])
        self.assertIsNone(inbox.FOLDER_CACHE['test', 'INBOX']['state'])
        self.assertNotIn(key, inbox.MESSAGE_CACHE)
        self.assertNotIn(key, inbox.SEEN_STATES)
        self.assertIn(key, inbox.TRASHED_MESSAGES)
        self.refresh.assert_called_once()

    def test_uppercase_confirmation_also_works(self) -> None:
        self.assertIn('Moved to Trash', inbox.trash_message(FakeScreen(['Y']), message()))
        self.transport.assert_called_once()

    def test_changed_uid_epoch_never_moves_a_different_message(self) -> None:
        self.states.return_value = {'INBOX': {'UIDVALIDITY': 11}}
        status = inbox.trash_message(FakeScreen(['y']), message())
        self.assertIn('Move not confirmed', status)
        self.transport.assert_not_called()
        self.assertFalse(inbox.TRASHED_MESSAGES)

    def test_missing_epoch_requires_a_fresh_matching_message_id(self) -> None:
        item = dict(message(), uidvalidity=None)
        inbox.MESSAGE_CACHE[inbox.message_cache_key(item)] = 'Message-ID: <message-1>\n\nOLD'
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'Message-ID: <wrong>\n\nNEW'}):
            with self.assertRaises(RuntimeError):
                inbox.move_to_trash(item, 'Trash')
        self.transport.assert_not_called()
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'Message-ID: <message-1>\n\nBODY'}):
            inbox.move_to_trash(item, 'Trash')
        self.transport.assert_called_once()

    def test_unverifiable_message_and_uid_ranges_cannot_move(self) -> None:
        cases = [dict(message(), uidvalidity=None, **{'message-id': None}),
                 message('0'), message('*'), message('1:99'), message('--all')]
        for item in cases:
            with self.assertRaises(RuntimeError):
                inbox.move_to_trash(item, 'Trash')
        self.transport.assert_not_called()

    def test_failed_or_interrupted_move_keeps_message_and_never_retries(self) -> None:
        item = message()
        for error in (RuntimeError('PRIVATE_TOKEN'), OSError('PRIVATE_TOKEN'), KeyboardInterrupt()):
            self.transport.reset_mock()
            self.transport.side_effect = error
            inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': {'UIDVALIDITY': 10}, 'rows': [item]}
            status = inbox.trash_message(FakeScreen(['y']), item)
            self.assertIn('Move not confirmed', status)
            self.assertNotIn('PRIVATE_TOKEN', status)
            self.transport.assert_called_once()
            self.assertFalse(inbox.TRASHED_MESSAGES)
            self.assertEqual(inbox.FOLDER_CACHE['test', 'INBOX']['rows'], [item])
            self.assertIsNone(inbox.FOLDER_CACHE['test', 'INBOX']['state'])
        self.refresh.assert_not_called()

    def test_pending_read_save_finishes_before_move(self) -> None:
        item = message()
        pending = Future()
        with patch.object(inbox, 'background', return_value=pending):
            inbox.mark_seen([item], True)
        screen = FakeScreen([])
        keys = iter(['y', 'finish'])

        def key():
            value = next(keys)
            if value == 'finish':
                pending.set_result(None)
                raise curses.error()
            return value

        def move(args):
            self.assertFalse(inbox.SEEN_QUEUE)
            self.assertEqual(args[2:4], ['message', 'move'])
            return b''

        screen.get_wch = key
        self.transport.side_effect = move
        self.assertIn('Moved to Trash', inbox.trash_message(screen, item))

    def test_escape_while_waiting_for_read_save_cancels_trash(self) -> None:
        item = message()
        with patch.object(inbox, 'background', return_value=Future()):
            inbox.mark_seen([item], True)
        status = inbox.trash_message(FakeScreen(['y', '\x1b']), item)
        self.assertIn('Nothing moved', status)
        self.transport.assert_not_called()
        self.assertFalse(inbox.TRASHED_MESSAGES)

    def test_list_key_moves_only_the_highlighted_email(self) -> None:
        items = [message('1'), message('2')]
        self.browse(FakeScreen(['j', 'x', 'y', 'q']), items, threaded=False)
        self.assertEqual([item['id'] for item in items], ['1'])
        self.assertEqual(self.transport.call_args.args[0][-1], '2')

    def test_conversation_summary_requires_choosing_one_message(self) -> None:
        first = message()
        second = dict(message('2'), mailbox='Sent', sent=True, **{'in-reply-to': ['message-1']})
        items = [first, second]
        self.browse(FakeScreen(['x', 'j', '\n', 'y', 'q']), items)
        self.assertEqual(items, [first])
        self.transport.assert_called_once()
        self.assertEqual(self.transport.call_args.args[0][-1], '2')
        self.assertEqual(self.transport.call_args.args[0][5], 'Sent')

    def test_cancelling_conversation_selection_preserves_every_message(self) -> None:
        items = [message(), dict(message('2'), **{'in-reply-to': ['message-1']})]
        self.browse(FakeScreen(['x', 'q', 'q']), items)
        self.assertEqual(len(items), 2)
        self.transport.assert_not_called()

    def test_filter_typing_and_empty_views_cannot_trigger_trash(self) -> None:
        self.browse(FakeScreen(['/', 'x', '\n', 'q']), [message()])
        self.browse(FakeScreen(['x', 'q']), [])
        self.transport.assert_not_called()

    def test_reader_trash_returns_to_list_after_success(self) -> None:
        item = dict(message(), flags=[{'iana': 'seen'}])
        with patch.object(inbox, 'read_raw', return_value='Subject: Synthetic\n\nBODY'):
            inbox.show_message(FakeScreen(['x', 'y']), item)
        self.assertIn(inbox.message_cache_key(item), inbox.TRASHED_MESSAGES)
        self.transport.assert_called_once()

    def test_reader_cancellation_keeps_message_open(self) -> None:
        item = dict(message(), flags=[{'iana': 'seen'}])
        with patch.object(inbox, 'read_raw', return_value='Subject: Synthetic\n\nBODY'):
            inbox.show_message(FakeScreen(['x', 'n', 'q']), item)
        self.transport.assert_not_called()
        self.assertFalse(inbox.TRASHED_MESSAGES)

    def test_stale_in_flight_refresh_cannot_restore_a_moved_message(self) -> None:
        item, keep = message(), message('2')
        pending = inbox.REFRESH_JOB = Future()
        inbox.trash_message(FakeScreen(['y']), item)
        pending.set_result([dict(item), keep])
        items = [item, keep]
        self.browse(FakeScreen(['q']), items, threaded=False)
        self.assertEqual(items, [keep])

    def test_suppression_is_scoped_and_allows_restoration_with_a_new_uid(self) -> None:
        item = message()
        inbox.TRASHED_MESSAGES.add(inbox.message_cache_key(item))
        visible = [dict(item, account='other'), dict(item, mailbox='Archive'),
                   dict(item, id='99'), dict(item, uidvalidity=11)]
        self.assertEqual(inbox.without_trashed([item, *visible]), visible)

    def test_late_cache_writer_cannot_persist_deleted_headers(self) -> None:
        item, keep = message(), message('2')
        inbox.TRASHED_MESSAGES.add(inbox.message_cache_key(item))
        inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': {'UIDVALIDITY': 10}, 'rows': [item, keep]}
        with tempfile.TemporaryDirectory() as directory:
            inbox.HEADER_CACHE_PATH = Path(directory) / 'headers.json'
            inbox.write_header_cache()
            folder = json.loads(inbox.HEADER_CACHE_PATH.read_text())['folders'][0]
            self.assertEqual([entry['id'] for entry in folder['rows']], ['2'])
            self.assertIsNone(folder['state'])
            self.assertEqual(inbox.HEADER_CACHE_PATH.stat().st_mode & 0o777, 0o600)

    def test_late_prefetch_cannot_recache_a_trashed_body(self) -> None:
        item = message()
        key = inbox.message_cache_key(item)

        def read(args):
            inbox.TRASHED_MESSAGES.add(key)
            return {'message': 'Message-ID: <message-1>\n\nSYNTHETIC_BODY'}

        with patch.object(inbox, 'run_himalaya', side_effect=read):
            inbox.read_raw(item)
        self.assertNotIn(key, inbox.MESSAGE_CACHE)

    def test_refresh_result_excludes_old_moved_uids(self) -> None:
        item, keep = message(), message('2')
        inbox.TRASHED_MESSAGES.add(inbox.message_cache_key(item))
        with patch.object(inbox, 'load_account', side_effect=lambda account: [item, keep] if account == 'test' else []):
            self.assertEqual(inbox.load_all(), [keep])


if __name__ == '__main__':
    unittest.main()
