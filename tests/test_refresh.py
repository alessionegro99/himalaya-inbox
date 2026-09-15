"""Offline change detection, private header caching and responsive UI checks."""

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
import curses
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from test_inbox import FakeScreen, inbox, row


STATE = {'MESSAGES': 1, 'UIDVALIDITY': 10, 'UIDNEXT': 2, 'HIGHESTMODSEQ': 30}
BOXES = [{'name': 'INBOX', 'attributes': [], 'delimiter': '/'}]


def envelope(uid: str = '1', mid: str = 'one') -> dict:
    return dict(row('test', uid, mid), mailbox='INBOX', flags=[], references=[],
                uidvalidity=10, in_inbox=True)


class FastInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = {'test': {'email': 'me@example.org', 'imap': {}}}
        for name, value in [('SETTINGS', settings), ('CONFIG_ARGS', []), ('FOLDER_CACHE', {}),
                            ('HEADER_CACHE_PATH', None), ('HEADER_CACHE_READY', False),
                            ('REFERENCE_CACHE', {}), ('MESSAGE_CACHE', OrderedDict()), ('MESSAGE_READS', {}),
                            ('PREFETCH_ENABLED', False), ('PREFETCH_JOB', None), ('PREFETCH_KEY', None),
                            ('REFRESH_JOB', None), ('IO_STOP', Event()), ('IO_PROCESSES', set())]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)

    def test_status_parser_handles_quotes_literals_and_out_of_order_completions(self) -> None:
        names = ['INBOX', 'A "quoted" folder', '台北']
        encoded = inbox.imap_mailbox(names[2])[1:-1].encode()
        values = b' (MESSAGES 1 UIDVALIDITY 10 UIDNEXT 2 HIGHESTMODSEQ 30)\r\n'
        raw = (b'* STATUS "A \\"quoted\\" folder"' + values + b'hs1 OK done\r\n'
               + b'* STATUS {' + str(len(encoded)).encode() + b'}\r\n' + encoded + values
               + b'hs2 OK done\r\n* STATUS INBOX' + values + b'hs0 OK done\r\n')
        self.assertEqual(inbox.parse_mailbox_states(raw, names), {name: STATE for name in names})

    def test_unconfirmed_malformed_or_unsupported_status_is_not_reused(self) -> None:
        valid = b'* STATUS INBOX (MESSAGES 1 UIDVALIDITY 10 UIDNEXT 2 HIGHESTMODSEQ 30)\r\n'
        for output in [valid, valid + b'hs0 NO denied\r\n', valid.replace(b'30', b'NIL') + b'hs0 OK done\r\n']:
            self.assertEqual(inbox.parse_mailbox_states(output, ['INBOX']), {})
        with patch.object(inbox, 'run_private', side_effect=RuntimeError('PRIVATE')):
            self.assertEqual(inbox.mailbox_states('test', ['INBOX']), {})

    def test_status_only_uses_independent_read_only_commands(self) -> None:
        with patch.object(inbox, 'run_private', return_value=b'') as run:
            inbox.mailbox_states('test', ['INBOX', 'Sent'])
        data = run.call_args.args[1]
        self.assertTrue(data.endswith(b'\r\n\r\n'))
        self.assertEqual(data.count(b' STATUS '), 2)
        for forbidden in [b'SELECT', b'FETCH', b'STORE ', b'EXPUNGE']:
            self.assertNotIn(forbidden, data)

    def test_unchanged_folder_reuses_headers_without_fetching(self) -> None:
        inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': STATE, 'rows': [envelope()]}
        with patch.object(inbox, 'run_himalaya', return_value={'mailboxes': BOXES}), \
                patch.object(inbox, 'mailbox_states', return_value={'INBOX': STATE}), \
                patch.object(inbox, 'fetch') as fetch, patch.object(inbox, 'add_references') as refs:
            result = inbox.load_account('test')
        self.assertEqual(result[0]['id'], '1')
        fetch.assert_not_called()
        refs.assert_not_called()

    def test_every_state_change_and_missing_modseq_force_a_fresh_fetch(self) -> None:
        for field in STATE:
            changed = dict(STATE, **{field: STATE[field] + 1})
            for state in [changed, dict(STATE, HIGHESTMODSEQ=0)]:
                inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': STATE, 'rows': [envelope()]}
                fresh = dict(envelope(), flags=[{'iana': 'seen'}])
                with patch.object(inbox, 'run_himalaya', return_value={'mailboxes': BOXES}), \
                        patch.object(inbox, 'mailbox_states', return_value={'INBOX': state}), \
                        patch.object(inbox, 'fetch', return_value=[fresh]) as fetch, \
                        patch.object(inbox, 'add_references'):
                    result = inbox.load_account('test')
                fetch.assert_called_once()
                self.assertFalse(inbox.is_unread(result[0]))
                self.assertEqual(result[0]['uidvalidity'], state['UIDVALIDITY'])

    def test_empty_and_removed_folders_do_not_leave_ghost_messages(self) -> None:
        inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': STATE, 'rows': [envelope()]}
        inbox.FOLDER_CACHE['test', 'Gone'] = {'state': STATE, 'rows': [envelope()]}
        with patch.object(inbox, 'run_himalaya', return_value={'mailboxes': BOXES}), \
                patch.object(inbox, 'mailbox_states', return_value={'INBOX': dict(STATE, MESSAGES=0)}), \
                patch.object(inbox, 'fetch') as fetch:
            self.assertEqual(inbox.load_account('test'), [])
        fetch.assert_not_called()
        self.assertNotIn(('test', 'Gone'), inbox.FOLDER_CACHE)

    def test_new_folders_are_discovered_and_fetched(self) -> None:
        inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': STATE, 'rows': [envelope()]}
        boxes = [*BOXES, {'name': 'New', 'attributes': [], 'delimiter': '/'}]
        new = dict(envelope('9', 'new'), mailbox='New')
        with patch.object(inbox, 'run_himalaya', return_value={'mailboxes': boxes}), \
                patch.object(inbox, 'mailbox_states', side_effect=[{'INBOX': STATE}, {'New': STATE}]) as states, \
                patch.object(inbox, 'fetch', return_value=[new]) as fetch, patch.object(inbox, 'add_references'):
            result = inbox.load_account('test')
        self.assertEqual(len(result), 2)
        fetch.assert_called_once_with('test', None, 'New')
        self.assertEqual(states.call_args.args[1], ['New'])

    def test_private_header_cache_excludes_bodies_credentials_and_bcc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox.HEADER_CACHE_PATH = Path(directory) / 'cache/headers.json'
            item = dict(envelope(), body='PRIVATE_BODY', password='PRIVATE_PASSWORD', bcc='PRIVATE_BCC')
            inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': STATE, 'rows': [item]}
            inbox.write_header_cache()
            self.assertEqual(inbox.HEADER_CACHE_PATH.stat().st_mode & 0o777, 0o600)
            self.assertEqual(inbox.HEADER_CACHE_PATH.parent.stat().st_mode & 0o777, 0o700)
            text = inbox.HEADER_CACHE_PATH.read_text()
            self.assertNotIn('PRIVATE_', text)
            inbox.FOLDER_CACHE.clear()
            inbox.read_header_cache()
            self.assertTrue(inbox.HEADER_CACHE_READY)
            self.assertEqual(inbox.FOLDER_CACHE['test', 'INBOX']['rows'], [envelope()])
            self.assertFalse(list(inbox.HEADER_CACHE_PATH.parent.glob('.headers-*')))

    def test_corrupt_incomplete_and_wrong_account_caches_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox.HEADER_CACHE_PATH = Path(directory) / 'headers.json'
            cases = ['not JSON', json.dumps({'version': 99}),
                     json.dumps({'version': 2, 'accounts': ['other'], 'folders': []}),
                     json.dumps({'version': 2, 'accounts': ['test'], 'folders': [{'account': 'test', 'mailbox': 'INBOX', 'rows': [{}], 'state': STATE}]})]
            for data in cases:
                inbox.HEADER_CACHE_PATH.write_text(data)
                inbox.read_header_cache()
                self.assertFalse(inbox.HEADER_CACHE_READY)
                self.assertFalse(inbox.FOLDER_CACHE)

    def test_cache_path_changes_with_configuration_and_worker_does_not_load_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_CACHE_HOME': directory}):
            config = Path(directory) / 'config.toml'
            config.write_text('[accounts.test]\nemail="one@example.org"\n')
            inbox.read_settings(str(config), cache=True)
            first = inbox.HEADER_CACHE_PATH
            config.write_text('[accounts.test]\nemail="two@example.org"\n')
            inbox.read_settings(str(config), cache=True)
            self.assertNotEqual(first, inbox.HEADER_CACHE_PATH)
            with patch.object(inbox, 'read_header_cache') as cache:
                inbox.read_settings(str(config))
            cache.assert_not_called()
            self.assertIsNone(inbox.HEADER_CACHE_PATH)

    def test_refresh_preserves_verified_bodies_and_discards_reused_uids(self) -> None:
        good = envelope()
        old = dict(good, uidvalidity=9)
        inbox.MESSAGE_CACHE[inbox.message_cache_key(good)] = 'KEEP'
        inbox.MESSAGE_CACHE[inbox.message_cache_key(old)] = 'DROP'
        with patch.object(inbox, 'load_account', return_value=[good]):
            inbox.load_all()
        self.assertEqual(inbox.MESSAGE_CACHE, {inbox.message_cache_key(good): 'KEEP'})

    def test_total_memory_size_is_bounded(self) -> None:
        with patch.object(inbox, 'MESSAGE_CACHE_LIMIT', 5000), \
                patch.object(inbox, 'run_himalaya', return_value={'message': 'x' * 2000}):
            for number in range(4):
                inbox.read_raw(row('test', str(number), None))
        self.assertEqual(len(inbox.MESSAGE_CACHE), 2)

    def test_cached_uid_cannot_open_a_different_message(self) -> None:
        item = dict(envelope(), _cached=True)
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'Message-ID: <wrong>\n\nBody'}):
            with self.assertRaises(RuntimeError):
                inbox.read_raw(item)
        self.assertFalse(inbox.MESSAGE_CACHE)
        with patch.object(inbox, 'run_himalaya') as fetch:
            with self.assertRaises(RuntimeError):
                inbox.read_raw(dict(item, **{'message-id': None}))
        fetch.assert_not_called()

    def test_prefetch_and_explicit_open_share_one_request(self) -> None:
        entered, release = Event(), Event()
        raw = 'Message-ID: <one>\n\nSynthetic body'

        def fetch(args):
            entered.set()
            self.assertTrue(release.wait(3))
            return {'message': raw}

        with patch.object(inbox, 'PREFETCH_ENABLED', True), patch.object(inbox, 'run_himalaya', side_effect=fetch) as run:
            inbox.prefetch_message(envelope())
            try:
                self.assertTrue(entered.wait(2))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    explicit = pool.submit(inbox.read_raw, envelope())
                    release.set()
                    self.assertEqual(explicit.result(timeout=2), raw)
                inbox.PREFETCH_JOB.result(timeout=2)
                self.assertEqual(inbox.read_raw(envelope()), raw)
                self.assertEqual(run.call_count, 1)
            finally:
                release.set()

    def test_failed_prefetch_does_not_retry_on_every_screen_poll(self) -> None:
        with patch.object(inbox, 'PREFETCH_ENABLED', True), \
                patch.object(inbox, 'read_raw', side_effect=RuntimeError('PRIVATE')) as read:
            inbox.prefetch_message(envelope())
            inbox.PREFETCH_JOB.result(timeout=2)
            for _ in range(20):
                inbox.prefetch_message(envelope())
        read.assert_called_once()

    def test_pending_refresh_does_not_block_navigation_or_start_duplicate_jobs(self) -> None:
        pending = Future()
        screen = FakeScreen(['u', 'u', 'j', '\n', 'q'])
        rows = [envelope('1', 'one'), envelope('2', 'two')]
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'), \
                patch.object(inbox, 'background', return_value=pending) as start, patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, rows, threaded=False)
        start.assert_called_once()
        self.assertEqual(show.call_args.args[1]['id'], '2')

    def test_background_refresh_preserves_selection_when_new_mail_arrives(self) -> None:
        pending = inbox.REFRESH_JOB = Future()
        rows = [envelope('1', 'one'), envelope('2', 'two')]
        screen = FakeScreen([])
        keys = iter(['j', '?', '\n', 'q'])

        def key():
            value = next(keys)
            if value == '?':
                pending.set_result([envelope('3', 'new'), *rows])
            return value

        screen.get_wch = key
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'), patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, rows, threaded=False)
        self.assertEqual(show.call_args.args[1]['id'], '2')
        self.assertEqual(len(rows), 3)

    def test_failed_refresh_preserves_headers_and_suppresses_diagnostics(self) -> None:
        failed = inbox.REFRESH_JOB = Future()
        failed.set_exception(RuntimeError('PRIVATE'))
        rows = [envelope()]
        rendered = []
        screen = FakeScreen(['q'])
        screen.addnstr = lambda y, x, text, *args: rendered.append(text)
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'):
            inbox.browse(screen, rows)
        self.assertEqual(rows, [envelope()])
        self.assertNotIn('PRIVATE', ''.join(rendered))
        self.assertIn('Refresh failed', ''.join(rendered))

    def test_refresh_inside_thread_includes_sent_replies_chronologically(self) -> None:
        original = dict(envelope(), date='2026-01-01T10:00:00Z')
        sent = dict(envelope('2', 'reply'), date='2026-01-01T11:00:00Z', sent=True,
                    in_inbox=False, references=['one'])
        inbox.REFRESH_JOB = Future()
        inbox.REFRESH_JOB.set_result([sent, original])
        catalog, thread = [original], [original]
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'):
            inbox.browse(FakeScreen(['q']), thread, threaded=False, in_thread=True, catalog=catalog)
        self.assertEqual(thread, [original, sent])
        self.assertEqual(catalog, [sent, original])

    def test_opening_an_uncached_message_can_be_cancelled(self) -> None:
        pending = Future()
        screen = FakeScreen(['q'])
        with patch.object(inbox, 'PREFETCH_ENABLED', True), patch.object(inbox, 'background', return_value=pending):
            inbox.show_message(screen, envelope())
        self.assertTrue(pending.cancelled())
        self.assertEqual(screen.timeout_value, -1)

    def test_cached_startup_displays_headers_before_refresh_finishes(self) -> None:
        inbox.HEADER_CACHE_READY = True
        inbox.FOLDER_CACHE['test', 'INBOX'] = {'state': STATE, 'rows': [envelope()]}
        pending = Future()
        with patch.object(sys, 'argv', ['inbox']), patch.object(sys.stdin, 'isatty', return_value=True), \
                patch.object(sys.stdout, 'isatty', return_value=True), patch.object(inbox, 'read_settings'), \
                patch.object(inbox, 'background', return_value=pending), patch.object(inbox.curses, 'wrapper') as display:
            inbox.main()
        self.assertFalse(pending.done())
        self.assertTrue(display.call_args.args[1][0]['_cached'])

    def test_quitting_stops_only_the_clients_own_background_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / 'slow-read-only-helper'
            helper.write_text('#!/bin/sh\nsleep 30\n')
            helper.chmod(0o700)
            with patch.object(inbox, 'HIMALAYA', str(helper)):
                job = inbox.background(inbox.run_private, ['synthetic-read'])
                deadline = time.monotonic() + 3
                while not inbox.IO_PROCESSES and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(inbox.IO_PROCESSES)
                try:
                    inbox.stop_background()
                    with self.assertRaises(RuntimeError):
                        job.result(timeout=2)
                    self.assertFalse(inbox.IO_PROCESSES)
                finally:
                    inbox.stop_background()


if __name__ == '__main__':
    unittest.main()
