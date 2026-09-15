"""Offline checks for unified ordering, account-scoped IDs and pagination."""

import importlib.machinery
import importlib.util
import curses
from io import StringIO
from contextlib import redirect_stdout
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


loader = importlib.machinery.SourceFileLoader('inbox', os.environ.get('INBOX_HELPER', str(Path(__file__).resolve().parents[1] / 'himalaya-inbox')))
spec = importlib.util.spec_from_loader(loader.name, loader)
inbox = importlib.util.module_from_spec(spec)
loader.exec_module(inbox)


class InboxTests(unittest.TestCase):
    def test_global_order_uses_timezones_and_preserves_account_ids(self) -> None:
        first = {'account': 'first', 'id': '1', 'date': '2026-09-15T11:00:00+02:00'}
        second = {'account': 'second', 'id': '1', 'date': '2026-09-15T10:00:00Z'}
        missing = {'account': 'first', 'id': '2', 'date': None}
        self.assertEqual(inbox.merge([[first, missing], [second]], None), [second, first, missing])
        self.assertEqual(inbox.merge([[first], [second]], 1), [second])

    def test_all_fetches_every_page(self) -> None:
        full = [{'id': str(i)} for i in range(500)]
        with patch.object(inbox, 'run_himalaya', side_effect=[{'envelopes': full}, {'envelopes': [{'id': '500'}]}]) as run:
            rows = inbox.fetch('test', None)
        self.assertEqual(len(rows), 501)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][-3:], ['2', '--page-size', '500'])
        self.assertTrue(all(row['account'] == 'test' for row in rows))

    def test_limit_stops_fetching(self) -> None:
        with patch.object(inbox, 'run_himalaya', return_value={'envelopes': [{'id': '1'}, {'id': '2'}]}) as run:
            self.assertEqual(len(inbox.fetch('test', 2)), 2)
        self.assertEqual(run.call_count, 1)

    def test_pagination_cannot_loop_forever(self) -> None:
        full = [{'id': str(i)} for i in range(500)]
        with patch.object(inbox, 'run_himalaya', return_value={'envelopes': full}):
            with self.assertRaisesRegex(RuntimeError, 'no partial unified inbox'):
                inbox.fetch('test', None)

    def test_private_errors_are_not_forwarded(self) -> None:
        with patch.object(inbox, 'run_himalaya', side_effect=RuntimeError('SENSITIVE_DIAGNOSTIC')):
            with self.assertRaises(RuntimeError) as caught:
                inbox.fetch('test', 1)
        self.assertNotIn('SENSITIVE_DIAGNOSTIC', str(caught.exception))

    def test_render_removes_terminal_controls_and_marks_unread(self) -> None:
        row = {'account': 'test', 'id': '7', 'date': None, 'flags': [],
               'from': [{'email': 'sender@example.org'}], 'subject': 'a\x1bb\nc'}
        output = StringIO()
        with redirect_stdout(output):
            inbox.render([row])
        self.assertNotIn('\x1b', output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertIn('*', output.getvalue())


def row(account: str, uid: str, mid: str | None, replies: list[str] | None = None,
        subject: str = 'Same subject') -> dict:
    """Build synthetic mail without touching live accounts."""
    return dict(account=account, id=uid, subject=subject, date=None,
                **{'message-id': mid, 'in-reply-to': replies or [], 'from': []})


class BrowserTests(unittest.TestCase):
    def test_terminal_default_fetches_all_and_opens_browser(self) -> None:
        rows = [row('a', '1', 'one')]
        with patch.object(sys, 'argv', ['himalaya-inbox']), \
                patch.object(sys.stdin, 'isatty', return_value=True), \
                patch.object(sys.stdout, 'isatty', return_value=True), \
                patch.object(inbox, 'read_settings'), \
                patch.object(inbox, 'load_all', return_value=rows) as load, \
                patch.object(curses, 'wrapper') as wrapper, patch.object(sys, 'stderr', StringIO()):
            inbox.main()
        load.assert_called_once_with()
        wrapper.assert_called_once_with(inbox.browse, rows, 'Unified inbox', True)

    def test_piping_preserves_plain_list_default_and_all_option(self) -> None:
        for options, limit in (([], 50), (['--all'], None), (['-n', '7'], 7)):
            with patch.object(sys, 'argv', ['himalaya-inbox', *options]), \
                    patch.object(sys.stdin, 'isatty', return_value=False), \
                    patch.object(inbox, 'run_himalaya', return_value={'accounts': [{'name': 'a'}]}), \
                    patch.object(inbox, 'fetch', return_value=[]) as fetch, \
                    patch.object(curses, 'wrapper') as wrapper, \
                    patch.object(sys, 'stdout', StringIO()), patch.object(sys, 'stderr', StringIO()):
                inbox.main()
            fetch.assert_called_once_with('a', limit)
            wrapper.assert_not_called()

    def test_reply_graph_is_transitive_and_keeps_account_scoped_uids(self) -> None:
        rows = [row('a', '1', 'grandchild', ['child']), row('b', '1', 'child', ['root']),
                row('c', '1', 'root'), row('a', '2', 'unrelated')]
        self.assertEqual(inbox.conversations(rows), [rows[:3], rows[3:]])

    def test_same_subject_is_not_a_thread_and_missing_ids_are_distinct(self) -> None:
        rows = [row('a', '1', None), row('a', '2', None), row('a', '3', 'distinct')]
        self.assertEqual(inbox.conversations(rows), [[item] for item in rows])

    def test_shared_missing_parent_groups_siblings(self) -> None:
        rows = [row('a', '1', 'child1', ['missing']), row('a', '2', 'child2', ['missing'])]
        self.assertEqual(inbox.conversations(rows), [rows])

    def test_cycles_and_duplicate_message_ids_terminate(self) -> None:
        rows = [row('a', '1', 'one', ['two']), row('a', '2', 'two', ['one']), row('b', '1', 'one')]
        self.assertEqual(inbox.conversations(rows), [rows])
        self.assertEqual(inbox.conversations([]), [])

    def test_plain_body_is_preferred_and_attachments_not_opened(self) -> None:
        raw = ('Subject: =?utf-8?q?Caff=C3=A8?=\nMIME-Version: 1.0\n'
               'Content-Type: multipart/mixed; boundary=outer\n\n--outer\n'
               'Content-Type: multipart/alternative; boundary=inner\n\n--inner\n'
               'Content-Type: text/plain; charset=utf-8\n\nPlain body\n--inner\n'
               'Content-Type: text/html\n\n<p>HTML body</p>\n--inner--\n--outer\n'
               'Content-Type: text/plain\nContent-Disposition: attachment; filename=test.txt\n\n'
               'PRIVATE_ATTACHMENT_CONTENT\n--outer--\n')
        result = inbox.message_text(raw, row('a', '1', None))
        self.assertIn('Caffè', result)
        self.assertIn('Plain body', result)
        self.assertIn('test.txt', result)
        self.assertNotIn('HTML body', result)
        self.assertNotIn('PRIVATE_ATTACHMENT_CONTENT', result)

    def test_html_is_text_only_and_terminal_controls_removed(self) -> None:
        raw = ('Subject: Test\nContent-Type: text/html; charset=utf-8\n\n'
               '<html><head><style>HIDDEN_STYLE</style></head><body><p>Hello &amp; bye</p>'
               '<script>HIDDEN_SCRIPT</script><img src="https://tracker.invalid/">'
               '<p>Next\x1b\x00\x07\u202e line</p></body></html>')
        result = inbox.message_text(raw, row('a', '1', None))
        self.assertIn('Hello & bye', result)
        self.assertIn('Next', result)
        for forbidden in ('HIDDEN_STYLE', 'HIDDEN_SCRIPT', 'tracker.invalid', '\x1b', '\x00', '\x07', '\u202e'):
            self.assertNotIn(forbidden, result)

    def test_message_fetch_uses_exact_account_and_no_seen_flag(self) -> None:
        with patch.object(inbox, 'run_himalaya', return_value={'message': 'Subject: Test\n\nBody'}) as run:
            self.assertIn('Body', inbox.read_message(row('test-account', '42', None)))
        self.assertEqual(run.call_args.args[0], ['--account', 'test-account', 'message', 'read',
                                               '--mailbox', 'inbox', '--raw', '--', '42'])

    def test_reader_suppresses_all_private_diagnostics(self) -> None:
        for error in (RuntimeError('PRIVATE_DIAGNOSTIC'), ValueError('PRIVATE_DIAGNOSTIC')):
            with patch.object(inbox, 'run_himalaya', side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    inbox.read_message(row('a', '1', None))
            self.assertNotIn('PRIVATE_DIAGNOSTIC', str(caught.exception))

    def test_browser_keys_select_correct_message_and_return_from_thread(self) -> None:
        rows = [row('a', '1', 'child', ['parent']), row('b', '1', 'parent'), row('a', '3', 'solo')]
        screen = FakeScreen(['\n', 'j', '\n', 'q', 'j', '\n', 'q'])
        with patch.object(curses, 'curs_set'), patch.object(curses, 'mousemask'), patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, rows)
        self.assertEqual([call.args[1] for call in show.call_args_list], [rows[1], rows[2]])

    def test_filter_by_name_then_open(self) -> None:
        rows = [row('a', '1', None, subject='First'), row('b', '1', None, subject='Second')]
        screen = FakeScreen(['/', *'second', '\n', '\n', 'q'])
        with patch.object(curses, 'curs_set'), patch.object(curses, 'mousemask'), patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, rows)
        show.assert_called_once_with(screen, rows[1])

    def test_empty_and_small_terminal_navigation(self) -> None:
        screen = FakeScreen([curses.KEY_END, curses.KEY_NPAGE, '\n', '/', 'z', '\n', 'q'], (3, 12))
        with patch.object(curses, 'curs_set'), patch.object(curses, 'mousemask'), patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, [])
        show.assert_not_called()

    def test_mouse_wheel_and_keyboard_page_navigation(self) -> None:
        rows = [row('a', str(i), str(i)) for i in range(20)]
        screen = FakeScreen([curses.KEY_MOUSE, '\n', curses.KEY_END, '\n', curses.KEY_HOME,
                             curses.KEY_NPAGE, '\n', curses.KEY_UP, '\n', 'q'], (10, 100))
        with patch.object(curses, 'curs_set'), patch.object(curses, 'mousemask'), \
                patch.object(curses, 'getmouse', return_value=(0, 1, 3, 0, curses.BUTTON5_PRESSED)), \
                patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, rows, threaded=False)
        self.assertEqual([call.args[1]['id'] for call in show.call_args_list], ['3', '19', '5', '4'])

    def test_terminal_restoration_on_failure(self) -> None:
        screen = FakeScreen([])
        with patch.object(curses, 'mousemask'), patch.object(curses, 'def_prog_mode'), \
                patch.object(curses, 'endwin'), patch.object(curses, 'reset_prog_mode') as reset, \
                self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with inbox.terminal_child(screen):
                raise RuntimeError('synthetic')
        reset.assert_called_once()
        self.assertTrue(screen.cleared)


class FakeScreen:
    """Minimal deterministic screen; real terminal/pager is covered separately."""

    def __init__(self, keys: list, size: tuple[int, int] = (24, 100)) -> None:
        self.keys = iter(keys)
        self.size = size
        self.cleared = False

    def getmaxyx(self):
        return self.size

    def get_wch(self):
        return next(self.keys)

    def keypad(self, value):
        pass

    def erase(self):
        pass

    def refresh(self):
        pass

    def addnstr(self, *args):
        pass

    def clearok(self, value):
        self.cleared = value

    def move(self, *args):
        pass

    def clrtoeol(self):
        pass


if __name__ == '__main__':
    unittest.main()
