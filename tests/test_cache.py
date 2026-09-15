"""Read-only cache behavior with synthetic mail and mocked network calls."""

from collections import OrderedDict
from threading import Barrier
import unittest
from unittest.mock import patch

from test_inbox import inbox, row


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in [('REFERENCE_CACHE', {}), ('MESSAGE_CACHE', OrderedDict()), ('CONTACTS', []),
                            ('FOLDER_CACHE', {}), ('HEADER_CACHE_PATH', None), ('MESSAGE_READS', {})]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)

    def test_reopening_and_replying_reuse_exact_account_folder_uid(self) -> None:
        first = dict(row('one', '42', 'message'), mailbox='INBOX')
        second = dict(first, account='two')
        third = dict(first, mailbox='Sent')
        with patch.object(inbox, 'run_himalaya', side_effect=[
            {'message': 'first'}, {'message': 'other account'}, {'message': 'other folder'},
        ]) as fetch:
            self.assertEqual(inbox.read_raw(first), 'first')
            self.assertEqual(inbox.read_raw(first), 'first')
            self.assertEqual(inbox.read_raw(second), 'other account')
            self.assertEqual(inbox.read_raw(third), 'other folder')
        self.assertEqual(fetch.call_count, 3)

    def test_body_cache_is_bounded_and_large_messages_are_not_retained(self) -> None:
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'text'}):
            for number in range(20):
                inbox.read_raw(row('one', str(number), str(number)))
        self.assertEqual(len(inbox.MESSAGE_CACHE), 16)
        self.assertNotIn(inbox.message_cache_key(row('one', '0', '0')), inbox.MESSAGE_CACHE)
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'x' * (1024 * 1024 + 1)}):
            inbox.read_raw(row('one', 'large', 'large'))
        self.assertEqual(len(inbox.MESSAGE_CACHE), 16)
        self.assertIn(inbox.message_cache_key(row('one', 'large', 'large')), inbox.MESSAGE_CACHE)
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'x' * (32 * 1024 * 1024 + 1)}):
            inbox.read_raw(row('one', 'oversized', 'oversized'))
        self.assertNotIn(inbox.message_cache_key(row('one', 'oversized', 'oversized')), inbox.MESSAGE_CACHE)

    def test_failed_reads_are_not_cached(self) -> None:
        item = row('one', '42', 'message')
        with patch.object(inbox, 'run_himalaya', side_effect=[RuntimeError('private'), {'message': 'recovered'}]):
            with self.assertRaises(RuntimeError):
                inbox.read_raw(item)
            self.assertEqual(inbox.read_raw(item), 'recovered')

    def test_cached_references_skip_network_but_new_messages_fetch(self) -> None:
        inbox.REFERENCE_CACHE[('one', 'known')] = ('root',)
        known = row('one', '1', '<known>')
        new = row('one', '2', 'new')
        with patch.object(inbox, 'run_private', return_value=b'headers') as run, \
                patch.object(inbox, 'parse_reference_fetch', return_value={'2': ['root', 'known']}):
            inbox.add_references('one', 'INBOX', [known, new])
        self.assertEqual(known['references'], ['root'])
        self.assertEqual(new['references'], ['root', 'known'])
        self.assertIn(b'UID FETCH 2 ', run.call_args.args[1])
        self.assertNotIn(b'UID FETCH 1', run.call_args.args[1])

    def test_reference_cache_is_not_shared_across_accounts_or_bare_uids(self) -> None:
        inbox.REFERENCE_CACHE[('one', 'known')] = ('wrong-account',)
        for item in [row('two', '1', 'known'), row('two', '1', None)]:
            with patch.object(inbox, 'run_private', return_value=b'headers') as run, \
                    patch.object(inbox, 'parse_reference_fetch', return_value={'1': ['correct']}):
                inbox.add_references('two', 'INBOX', [item])
            run.assert_called_once()
            self.assertEqual(item['references'], ['correct'])

    def test_refresh_clears_bodies_updates_flags_and_prunes_old_references(self) -> None:
        inbox.MESSAGE_CACHE[inbox.message_cache_key(row('one', '1', 'old'))] = 'old'
        inbox.REFERENCE_CACHE[('one', 'removed')] = ('root',)
        latest = dict(row('one', '1', 'new'), references=['root'], flags=[{'iana': 'seen'}])
        anonymous = dict(row('one', '2', None), references=[])
        with patch.object(inbox, 'SETTINGS', {'one': {}}), \
                patch.object(inbox, 'load_account', return_value=[latest, anonymous]) as load:
            rows = inbox.load_all()
        load.assert_called_once_with('one')
        self.assertEqual(inbox.MESSAGE_CACHE, {})
        self.assertEqual(inbox.REFERENCE_CACHE, {('one', 'new'): ('root',)})
        self.assertFalse(inbox.is_unread(next(r for r in rows if r['id'] == '1')))

    def test_two_folders_are_loaded_concurrently_and_excluded_folders_skipped(self) -> None:
        barrier = Barrier(2)

        def fetch(account, limit, mailbox):
            barrier.wait(timeout=3)
            return [row(account, '1', mailbox)]

        boxes = [
            {'name': 'INBOX', 'attributes': [], 'delimiter': '/'},
            {'name': 'Sent', 'attributes': ['\\Sent'], 'delimiter': '/'},
            {'name': 'Trash', 'attributes': ['\\Trash'], 'delimiter': '/'},
            {'name': 'Drafts', 'attributes': ['\\Drafts'], 'delimiter': '/'},
        ]
        with patch.object(inbox, 'SETTINGS', {'one': {'imap': {}}}), \
                patch.object(inbox, 'run_himalaya', return_value={'mailboxes': boxes}), \
                patch.object(inbox, 'mailbox_states', return_value={}), \
                patch.object(inbox, 'fetch', side_effect=fetch) as fetch_mock, \
                patch.object(inbox, 'add_references'):
            rows = inbox.load_account('one')
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertTrue(rows[0]['in_inbox'])
        self.assertTrue(rows[1]['sent'])


if __name__ == '__main__':
    unittest.main()
