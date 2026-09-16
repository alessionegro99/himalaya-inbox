"""Offline read/unread behavior; all server writes are synthetic."""

from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
import curses
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from test_inbox import FakeScreen, inbox, row


def message(uid: str = '1', **values) -> dict:
    return dict(row('test', uid, 'message-' + uid), mailbox='INBOX', flags=[], **values)


def immediate(function, *args) -> Future:
    result = Future()
    try:
        result.set_result(function(*args))
    except Exception as error:
        result.set_exception(error)
    return result


class ReadStateTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in [('SEEN_STATES', {}), ('SEEN_QUEUE', deque()), ('REFRESH_SEEN_STATES', {}),
                            ('REFRESH_JOB', None), ('FOLDER_CACHE', {}), ('HEADER_CACHE_PATH', None),
                            ('SETTINGS', {'test': {'imap': {}, 'email': 'test@example.org'}}),
                            ('MESSAGE_CACHE', OrderedDict()), ('MESSAGE_READS', {}),
                            ('PREFETCH_ENABLED', False), ('PREFETCH_JOB', None), ('PREFETCH_KEY', None)]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)
        # A missing mock must fail closed, never launch a real mail command.
        self.transport = patch.object(inbox, 'run_private', return_value=b'').start()
        self.addCleanup(patch.stopall)
        self.background = patch.object(inbox, 'background', side_effect=immediate).start()

    def browse(self, screen, rows, **kwargs) -> None:
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'):
            inbox.browse(screen, rows, **kwargs)

    def test_seen_commands_scope_account_mailbox_and_id_and_only_change_seen(self) -> None:
        item = dict(message('42'), account='second', mailbox='Filed mail')
        for seen, action in [(True, 'add'), (False, 'remove')]:
            inbox.save_seen(item, seen)
            self.assertEqual(self.transport.call_args.args[0], [
                '--account', 'second', 'flag', action, '--mailbox', 'Filed mail', '--flag', 'seen', '--', '42'])

    def test_flag_updates_preserve_all_unrelated_flags(self) -> None:
        other = [{'iana': 'answered'}, {'iana': 'flagged'}, {'raw': '$Important'}]
        item = dict(message(), flags=[*other, {'raw': '\\SEEN'}, {'iana': 'Seen'}])
        inbox.set_seen_flag(item, False)
        self.assertEqual(item['flags'], other)
        self.assertTrue(inbox.is_unread(item))
        inbox.set_seen_flag(item, True)
        self.assertEqual(item['flags'], [*other, {'iana': 'seen'}])
        self.assertFalse(inbox.is_unread(item))

    def test_known_uid_epoch_is_checked_before_writing(self) -> None:
        item = message(uidvalidity=10, _cached=True)
        for state in ({}, {'INBOX': {'UIDVALIDITY': 11}}):
            with patch.object(inbox, 'mailbox_states', return_value=state):
                with self.assertRaises(RuntimeError):
                    inbox.save_seen(item, True)
        self.transport.assert_not_called()
        with patch.object(inbox, 'mailbox_states', return_value={'INBOX': {'UIDVALIDITY': 10}}):
            inbox.save_seen(item, True)
        self.transport.assert_called_once()

    def test_cached_uid_without_epoch_checks_fresh_message_id_not_body_cache(self) -> None:
        item = message(_cached=True)
        inbox.MESSAGE_CACHE[inbox.message_cache_key(item)] = 'Message-ID: <message-1>\n\nOLD'
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'Message-ID: <different>\n\nNEW'}):
            with self.assertRaises(RuntimeError):
                inbox.save_seen(item, True)
        self.transport.assert_not_called()
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'Message-ID: <message-1>\n\nBODY'}):
            inbox.save_seen(item, True)
        self.transport.assert_called_once()

    def test_unsafe_cached_or_invalid_ids_cannot_write(self) -> None:
        for item in [dict(message(_cached=True), **{'message-id': None}), message('0'), message('1:99')]:
            with self.assertRaises(RuntimeError):
                inbox.save_seen(item, True)
        self.transport.assert_not_called()

    def test_opening_marks_read_once_and_removes_star(self) -> None:
        item = message()
        with patch.object(inbox, 'read_raw', return_value='Subject: Test\n\nBODY'):
            inbox.show_message(FakeScreen(['q']), item)
            inbox.show_message(FakeScreen(['q']), item)
        self.assertFalse(inbox.is_unread(item))
        self.assertFalse(inbox.entry_line([item]).startswith('*'))
        self.transport.assert_called_once()

    def test_reader_star_leaves_message_unread_until_opened_again(self) -> None:
        item = message()
        with patch.object(inbox, 'read_raw', return_value='Subject: Test\n\nBODY'):
            inbox.show_message(FakeScreen(['*', 'q']), item)
            self.assertTrue(inbox.is_unread(item))
            inbox.show_message(FakeScreen(['q']), item)
        self.assertFalse(inbox.is_unread(item))
        self.assertEqual([call.args[0][3] for call in self.transport.call_args_list], ['add', 'remove', 'add'])

    def test_download_render_failures_and_cancelled_open_do_not_mark_read(self) -> None:
        item = message()
        with patch.object(inbox, 'read_raw', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                inbox.show_message(FakeScreen([]), item)
        with patch.object(inbox, 'read_raw', return_value='body'), \
                patch.object(inbox, 'message_text', side_effect=ValueError('PRIVATE')):
            with self.assertRaises(RuntimeError):
                inbox.show_message(FakeScreen([]), item)
        with patch.object(inbox, 'PREFETCH_ENABLED', True), patch.object(inbox, 'background', return_value=Future()):
            inbox.show_message(FakeScreen(['q']), item)
        self.transport.assert_not_called()
        self.assertTrue(inbox.is_unread(item))

    def test_prefetch_and_raw_reads_never_mark_read(self) -> None:
        item = message()
        with patch.object(inbox, 'PREFETCH_ENABLED', True), \
                patch.object(inbox, 'run_himalaya', return_value={'message': 'Subject: Test\n\nBODY'}):
            inbox.prefetch_message(item)
            inbox.PREFETCH_JOB.result(timeout=2)
            inbox.read_raw(item)
        self.transport.assert_not_called()
        self.assertTrue(inbox.is_unread(item))

    def test_pending_save_does_not_block_opening_or_duplicate_auto_marking(self) -> None:
        item = message()
        pending = Future()
        with patch.object(inbox, 'background', return_value=pending) as start, \
                patch.object(inbox, 'read_raw', return_value='Subject: Test\n\nBODY'):
            inbox.show_message(FakeScreen(['q']), item)
            inbox.show_message(FakeScreen(['q']), item)
        self.assertFalse(inbox.is_unread(item))
        self.assertEqual(len(inbox.SEEN_QUEUE), 1)
        start.assert_called_once()
        self.assertFalse(pending.done())

    def test_rapid_toggles_are_written_in_request_order(self) -> None:
        item = message()
        first, second, third = Future(), Future(), Future()
        with patch.object(inbox, 'background', side_effect=[first, second, third]) as start:
            inbox.mark_seen([item], True)
            inbox.mark_seen([item], False)
            inbox.mark_seen([item], True)
            self.assertFalse(inbox.is_unread(item))
            self.assertEqual(start.call_count, 1)
            first.set_result(None)
            inbox.poll_seen_changes()
            self.assertEqual(start.call_count, 2)
            second.set_result(None)
            inbox.poll_seen_changes()
            third.set_result(None)
            inbox.poll_seen_changes()
        self.assertEqual([call.args[2] for call in start.call_args_list], [True, False, True])
        self.assertFalse(inbox.SEEN_QUEUE)
        self.assertFalse(inbox.is_unread(item))

    def test_failure_rolls_back_to_last_confirmed_state_without_private_diagnostics(self) -> None:
        item = message()
        first, second = Future(), Future()
        with patch.object(inbox, 'background', side_effect=[first, second]):
            inbox.mark_seen([item], True)
            inbox.mark_seen([item], False)
            first.set_result(None)
            inbox.poll_seen_changes()
            second.set_exception(RuntimeError('PRIVATE_TOKEN'))
            status = inbox.poll_seen_changes()
        self.assertFalse(inbox.is_unread(item))
        self.assertIn('Could not save', status)
        self.assertNotIn('PRIVATE_TOKEN', status)

    def test_failed_initial_read_restores_the_asterisk(self) -> None:
        item = message()
        self.transport.side_effect = RuntimeError('PRIVATE_TOKEN')
        status = inbox.mark_seen([item], True)
        self.assertTrue(inbox.is_unread(item))
        self.assertIn('Could not save', status)
        self.assertNotIn('PRIVATE_TOKEN', status)

    def test_later_success_does_not_hide_a_failed_conversation_save(self) -> None:
        items = [message('1'), message('2')]
        failed, pending = Future(), Future()
        failed.set_exception(RuntimeError('PRIVATE_TOKEN'))
        with patch.object(inbox, 'background', side_effect=[failed, pending]):
            status = inbox.mark_seen(items, True)
            pending.set_result(None)
            status = inbox.poll_seen_changes(status)
        self.assertIn('Could not save read status', status)
        self.assertTrue(inbox.is_unread(items[0]))
        self.assertFalse(inbox.is_unread(items[1]))

    def test_list_star_toggles_only_the_selected_message(self) -> None:
        items = [message('1'), message('2')]
        self.browse(FakeScreen(['j', '*', '*', 'q']), items, threaded=False)
        self.assertEqual([call.args[0][-1] for call in self.transport.call_args_list], ['2', '2'])
        self.assertTrue(all(inbox.is_unread(item) for item in items))

    def test_conversation_star_toggles_all_but_opening_its_list_does_not(self) -> None:
        items = [message('1'), dict(message('2'), **{'in-reply-to': ['message-1']})]
        self.browse(FakeScreen(['\n', 'q', 'q']), items)
        self.transport.assert_not_called()
        self.browse(FakeScreen(['*', 'q']), items)
        self.assertFalse(any(inbox.is_unread(item) for item in items))
        self.browse(FakeScreen(['*', 'q']), items)
        self.assertTrue(all(inbox.is_unread(item) for item in items))
        self.assertEqual(self.transport.call_count, 4)

    def test_star_in_filter_and_empty_list_cannot_change_flags(self) -> None:
        self.browse(FakeScreen(['/', '*', '\n', 'q']), [message()])
        self.browse(FakeScreen(['*', 'q']), [])
        self.transport.assert_not_called()

    def test_in_flight_refresh_cannot_undo_a_newer_action(self) -> None:
        item = message()
        pending = Future()
        with patch.object(inbox, 'background', return_value=pending):
            inbox.start_refresh()
        inbox.mark_seen([item], True)
        pending.set_result([message()])
        items = [item]
        self.browse(FakeScreen(['q']), items)
        self.assertFalse(inbox.is_unread(items[0]))

    def test_refresh_after_a_settled_write_accepts_external_read_state(self) -> None:
        item = message()
        inbox.mark_seen([item], True)
        pending = Future()
        with patch.object(inbox, 'background', return_value=pending):
            inbox.start_refresh()
        pending.set_result([message()])  # Another client subsequently marked it unread.
        items = [item]
        self.browse(FakeScreen(['q']), items)
        self.assertTrue(inbox.is_unread(items[0]))
        self.assertFalse(inbox.SEEN_STATES)

    def test_newer_toggle_survives_refresh_that_started_after_an_older_save(self) -> None:
        item = message()
        inbox.mark_seen([item], True)
        pending = Future()
        with patch.object(inbox, 'background', return_value=pending):
            inbox.start_refresh()
        inbox.mark_seen([item], False)
        pending.set_result([dict(message(), flags=[{'iana': 'seen'}])])
        items = [item]
        self.browse(FakeScreen(['q']), items)
        self.assertTrue(inbox.is_unread(items[0]))

    def test_failed_refresh_keeps_saved_read_status(self) -> None:
        item = message()
        inbox.mark_seen([item], True)
        pending = Future()
        with patch.object(inbox, 'background', return_value=pending):
            inbox.start_refresh()
        pending.set_exception(RuntimeError('PRIVATE'))
        self.browse(FakeScreen(['q']), [item])
        self.assertFalse(inbox.is_unread(item))

    def test_cache_persists_only_confirmed_flags_and_invalidates_folder_state(self) -> None:
        item = message()
        pending = Future()
        with tempfile.TemporaryDirectory() as directory:
            inbox.HEADER_CACHE_PATH = Path(directory) / 'headers.json'
            inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': {'UIDVALIDITY': 10}, 'rows': [item]}
            with patch.object(inbox, 'background', return_value=pending):
                inbox.mark_seen([item], True)
            inbox.write_header_cache()
            saved = json.loads(inbox.HEADER_CACHE_PATH.read_text())['folders'][0]
            self.assertFalse(inbox.has_seen_flag(saved['rows'][0]))
            pending.set_result(None)
            inbox.poll_seen_changes()
            saved = json.loads(inbox.HEADER_CACHE_PATH.read_text())['folders'][0]
            self.assertTrue(inbox.has_seen_flag(saved['rows'][0]))
            self.assertIsNone(saved['state'])
            self.assertEqual(inbox.HEADER_CACHE_PATH.stat().st_mode & 0o777, 0o600)

    def test_read_state_is_scoped_to_account_folder_and_uid_epoch(self) -> None:
        item = message(uidvalidity=10)
        with patch.object(inbox, 'mailbox_states', return_value={'INBOX': {'UIDVALIDITY': 10}}):
            inbox.mark_seen([item], True)
        for different in [dict(item, account='other'), dict(item, mailbox='Other'), dict(item, uidvalidity=11)]:
            self.assertTrue(inbox.is_unread(different))

    def test_normal_exit_waits_for_saves_and_escape_keeps_browsing(self) -> None:
        item = message()
        pending = Future()
        with patch.object(inbox, 'background', return_value=pending):
            inbox.mark_seen([item], True)
        self.assertIn('still saving', inbox.wait_seen_changes(FakeScreen(['\x1b'])))
        self.assertTrue(inbox.SEEN_QUEUE)
        screen = FakeScreen([])

        def complete():
            pending.set_result(None)
            raise curses.error()

        screen.get_wch = complete
        self.assertEqual(inbox.wait_seen_changes(screen), '')
        self.assertFalse(inbox.SEEN_QUEUE)

    def test_concurrent_cache_writer_cannot_overwrite_a_confirmed_read_flag(self) -> None:
        item = message()
        entered, release = Event(), Event()
        original_dump = json.dump

        def delayed_dump(data, file, **kwargs):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
            original_dump(data, file, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            inbox.HEADER_CACHE_PATH = Path(directory) / 'headers.json'
            inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': None, 'rows': [item]}
            with patch.object(inbox.json, 'dump', side_effect=delayed_dump), ThreadPoolExecutor(max_workers=2) as pool:
                old_write = pool.submit(inbox.write_header_cache)
                try:
                    self.assertTrue(entered.wait(2))
                    new_write = pool.submit(inbox.mark_seen, [item], True)
                finally:
                    release.set()
                old_write.result(timeout=2)
                new_write.result(timeout=2)
            saved = json.loads(inbox.HEADER_CACHE_PATH.read_text())['folders'][0]['rows'][0]
            self.assertTrue(inbox.has_seen_flag(saved))


if __name__ == '__main__':
    unittest.main()
