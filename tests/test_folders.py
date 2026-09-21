"""Unified folder views and destructive-action confirmation, using synthetic mail."""

from collections import deque
from concurrent.futures import Future
from threading import Event
import unittest
from unittest.mock import patch

from test_inbox import inbox, FakeScreen, row


def message(account='one', uid='1', mailbox='INBOX', **extra):
    return dict(row(account, uid, account + '-' + uid), mailbox=mailbox, uidvalidity=10,
                flags=[], in_inbox=mailbox == 'INBOX', in_trash=mailbox == 'Trash',
                in_archive=mailbox == 'Archive', **extra)


class FolderTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = {account: {'mailbox': {'alias': {'inbox': 'INBOX', 'trash': 'Trash', 'archive': 'Archive'}}}
                    for account in ('one', 'two')}
        for name, value in [('SETTINGS', settings), ('SEEN_STATES', {}), ('SEEN_QUEUE', deque()),
                            ('TRASHED_MESSAGES', set()), ('REFRESH_JOB', None), ('REFRESH_SEEN_STATES', {}),
                            ('REFRESH_AGAIN', set()), ('FOLDER_CACHE', {}), ('HEADER_CACHE_PATH', None),
                            ('AUTO_REFRESH_ENABLED', False), ('PREFETCH_ENABLED', False), ('IO_STOP', Event())]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)

    def browse(self, keys, messages):
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'):
            inbox.browse(FakeScreen(keys), messages, threaded=False)

    def test_trash_and_archive_combine_accounts_without_polluting_inbox_or_all(self) -> None:
        inbox_row = message()
        trash1 = message('one', '2', 'Trash')
        trash2 = message('two', '3', 'Trash')
        archive = message('two', '4', 'Archive')
        messages = [inbox_row, trash1, trash2, archive]
        for keys, expected in [(['\n', 'q'], inbox_row), (['T', '\n', 'q'], trash1),
                               (['T', 'j', '\n', 'q'], trash2), (['A', '\n', 'q'], archive),
                               (['a', 'G', '\n', 'q'], archive)]:
            with self.subTest(keys=keys), patch.object(inbox, 'show_message', return_value=None) as opened:
                self.browse(keys, list(messages))
                opened.assert_called_once()
                self.assertEqual(opened.call_args.args[1], expected)

    def test_archive_from_list_and_restore_from_trash_use_exact_account(self) -> None:
        for keys, item, destination in [(['e', 'y', 'q'], message(), 'Archive'),
                                         (['T', 'I', 'y', 'q'], message('two', '2', 'Trash'), 'INBOX')]:
            with self.subTest(keys=keys), patch.object(inbox, 'transfer_message', return_value={'gmail': False}) as transfer, \
                    patch.object(inbox, 'start_refresh') as refresh:
                messages = [item]
                self.browse(keys, messages)
                transfer.assert_called_once_with(item, destination)
                refresh.assert_called_once_with([item['account']])
                self.assertEqual(messages, [])

    def test_permanent_delete_requires_trash_and_exact_typed_confirmation(self) -> None:
        for confirmation, expected in [('delete', False), ('DELETE ALL', False), (None, False), ('DELETE', True)]:
            item = message('two', '42', 'Trash')
            inbox.TRASHED_MESSAGES.clear()
            with self.subTest(confirmation=confirmation), patch.object(inbox, 'input_line', return_value=confirmation), \
                    patch.object(inbox, 'transfer_message', return_value={'gmail': False}) as transfer, \
                    patch.object(inbox, 'start_refresh'):
                self.browse(['T', 'X', 'y', 'q'], [item])
                self.assertEqual(transfer.call_count, int(expected))
                if expected:
                    transfer.assert_called_once_with(item, None)
        with patch.object(inbox, 'transfer_message') as transfer:
            self.browse(['X', 'q'], [message()])
            transfer.assert_not_called()

    def test_permanent_delete_confirmation_can_be_cancelled_without_prompt_or_network(self) -> None:
        with patch.object(inbox, 'input_line') as prompt, patch.object(inbox, 'transfer_message') as transfer:
            self.browse(['T', 'X', 'n', 'q'], [message(mailbox='Trash')])
            prompt.assert_not_called()
            transfer.assert_not_called()

    def test_archive_reader_returns_after_success(self) -> None:
        item = message()
        item['flags'] = [{'iana': 'seen'}]
        with patch.object(inbox, 'read_raw', return_value='Subject: Synthetic\n\nBODY'), \
                patch.object(inbox, 'transfer_message', return_value={'gmail': False}) as transfer, \
                patch.object(inbox, 'start_refresh'):
            status = inbox.show_message(FakeScreen(['e', 'y']), item)
        self.assertIn('Archived', status)
        transfer.assert_called_once_with(item, 'Archive')

    def test_gmail_all_mail_is_not_archived_while_in_inbox(self) -> None:
        inbox_row = message()
        archive = dict(inbox_row, mailbox='All Mail', id='2', in_inbox=False, in_archive=True)
        rows = inbox.deduplicate([archive, inbox_row])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['in_archive'])
        self.assertTrue(inbox.deduplicate([archive])[0]['in_archive'])

    def test_trash_copy_is_not_deduplicated_away_by_live_mail(self) -> None:
        item = message()
        trash = dict(item, mailbox='Trash', id='2', in_inbox=False, in_trash=True)
        self.assertEqual(len(inbox.deduplicate([item, trash])), 2)

    def test_gmail_trash_retires_stale_label_copies_only_in_same_account(self) -> None:
        item = message()
        copy = dict(item, id='2', mailbox='Archive', in_inbox=False)
        other = dict(copy, account='two')
        for candidate in (item, copy, other):
            inbox.FOLDER_CACHE[candidate['account'], candidate['mailbox']] = {'state': None, 'rows': [candidate]}
        with patch.object(inbox, 'transfer_message', return_value={'gmail': True}), patch.object(inbox, 'start_refresh'):
            inbox.trash_message(FakeScreen(['y']), item)
        self.assertEqual(inbox.without_trashed([item, copy, other]), [other])

    def test_partial_refresh_contacts_only_changed_account_and_keeps_others(self) -> None:
        first, second = message(), message('two')
        inbox.FOLDER_CACHE['two', 'INBOX'] = {'state': None, 'rows': [second]}
        with patch.object(inbox, 'load_account', return_value=[first]) as load:
            rows = inbox.load_all(['one'])
        load.assert_called_once_with('one')
        self.assertEqual({row['account'] for row in rows}, {'one', 'two'})

    def test_move_during_refresh_schedules_a_followup_read_not_another_move(self) -> None:
        pending = inbox.REFRESH_JOB = Future()
        inbox.start_refresh(['two'])
        self.assertEqual(inbox.REFRESH_AGAIN, {'two'})
        pending.set_result([])
        with patch.object(inbox, 'background', return_value=Future()) as background:
            job, _ = inbox.take_refresh()
        self.assertIs(job, pending)
        background.assert_called_once_with(inbox.load_all, ['two'])
        self.assertFalse(inbox.REFRESH_AGAIN)


if __name__ == '__main__':
    unittest.main()
