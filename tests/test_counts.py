"""Vim-style movement counts, using synthetic mail and no live transport."""

from collections import deque
from concurrent.futures import Future
import curses
from datetime import UTC, datetime, timedelta
from threading import Event
import unittest
from unittest.mock import patch

from test_inbox import FakeScreen, inbox, row


class CountTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in [('SEEN_STATES', {}), ('SEEN_QUEUE', deque()),
                            ('REFRESH_JOB', None), ('REFRESH_SEEN_STATES', {}),
                            ('TRASHED_MESSAGES', set()), ('IO_STOP', Event()),
                            ('PREFETCH_ENABLED', False), ('AUTO_REFRESH_ENABLED', False),
                            ('HEADER_CACHE_PATH', None)]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)
        for name in ('run_private', 'run_himalaya'):
            transport = patch.object(inbox, name, side_effect=AssertionError('Live mail is forbidden in this test'))
            transport.start()
            self.addCleanup(transport.stop)
        self.rows = [row('test', str(i + 1), f'message-{i}', subject=f'Synthetic message {i:03d}')
                     for i in range(100)]

    def browse(self, screen: FakeScreen, rows: list[dict] | None = None, **kwargs) -> list[dict]:
        with patch.object(curses, 'curs_set'), patch.object(curses, 'mousemask'), \
                patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, self.rows if rows is None else rows, **kwargs)
        return [call.args[1] for call in show.call_args_list]

    def reader(self, keys: list) -> list[str]:
        """Return the first visible line from every frame of a short reader."""
        screen = FakeScreen(keys, (8, 100))
        first_lines = []
        screen.addnstr = lambda y, x, text, *args: first_lines.append(text) if y == 1 else None
        inbox.text_view(screen, '\n'.join(f'line-{i:03d}' for i in range(100)), 'help', {'q'})
        return first_lines

    def test_browser_moves_three_both_ways_and_consumes_count_once(self) -> None:
        keys = [*'3j', '\n', *'3k', '\n', *'20j', '\n', 'j', '\n', 'q']
        opened = self.browse(FakeScreen(keys))
        self.assertEqual(opened, [self.rows[i] for i in (3, 0, 20, 21)])

    def test_counts_work_with_arrow_keys(self) -> None:
        keys = ['5', curses.KEY_DOWN, '3', curses.KEY_UP, '\n', 'q']
        self.assertEqual(self.browse(FakeScreen(keys)), [self.rows[2]])

    def test_large_counts_stop_at_edges_without_wrapping(self) -> None:
        keys = [*('9' * 100), 'j', '\n', *('9' * 100), 'k', '\n', 'q']
        self.assertEqual(self.browse(FakeScreen(keys)), [self.rows[-1], self.rows[0]])

    def test_empty_list_and_tiny_terminal_accept_counts(self) -> None:
        self.assertEqual(self.browse(FakeScreen([*'99j99k', '\n', 'q'], (3, 12)), []), [])

    def test_thread_counts_follow_chronology_and_reply_to_selected_message(self) -> None:
        start = datetime(2026, 9, 17, tzinfo=UTC)
        for i, item in enumerate(self.rows):
            item['date'] = (start + timedelta(minutes=i)).isoformat()
            item['in-reply-to'] = ['message-0'] if i else []
        # Summary is newest-first; opening it must give chronological order.
        screen = FakeScreen(['\n', *'99j3k', 'r', '\n', 'q', 'q'])
        with patch.object(inbox, 'compose', return_value='Draft kept.') as compose:
            opened = self.browse(screen, list(reversed(self.rows)))
        self.assertEqual(opened, [self.rows[96]])
        compose.assert_called_once_with(screen, self.rows[96], reply_all=False)

    def test_count_does_not_leak_into_or_out_of_thread(self) -> None:
        for item in self.rows[1:10]:
            item['in-reply-to'] = ['message-0']
        # A pending count before Enter is discarded, as is one before q.
        keys = ['7', '\n', 'j', '\n', '9', 'q', 'j', '\n', 'q']
        self.assertEqual(self.browse(FakeScreen(keys)), [self.rows[1], self.rows[10]])

    def test_other_keys_cancel_count_and_never_repeat_actions(self) -> None:
        for action, name in [('x', 'trash_message'), ('r', 'compose'), ('*', 'mark_seen')]:
            with self.subTest(action=action), patch.object(inbox, name, return_value='Done.') as command:
                opened = self.browse(FakeScreen(['3', action, 'j', '\n', 'q']))
            command.assert_called_once()
            self.assertEqual(opened, [self.rows[1]])
        for action in ('\x1b', '?', 'i', 't', 'g'):
            with self.subTest(action=action):
                self.assertEqual(self.browse(FakeScreen(['3', action, 'j', '\n', 'q'])), [self.rows[1]])

    def test_enter_cancels_count_without_moving_or_repeating_open(self) -> None:
        keys = ['3', '\n', 'j', '\n', 'q']
        self.assertEqual(self.browse(FakeScreen(keys)), [self.rows[0], self.rows[1]])

    def test_digits_in_filter_stay_text_and_prefix_does_not_leak(self) -> None:
        screen = FakeScreen(['9', '/', *'03', '\n', 'j', '\n', '\x1b', 'j', '\n', 'q'])
        self.assertEqual(self.browse(screen), [self.rows[30], self.rows[2]])

    def test_pending_count_is_visible_and_survives_polling_and_resize(self) -> None:
        screen = FakeScreen([])
        rendered = []
        screen.addnstr = lambda y, x, text, *args: rendered.append(text)
        screen.get_wch = unittest.mock.Mock(side_effect=['3', curses.error(), curses.KEY_RESIZE, 'j', '\n', 'q'])
        with patch.object(inbox, 'AUTO_REFRESH_ENABLED', True):
            self.assertEqual(self.browse(screen), [self.rows[3]])
        self.assertTrue(any(text.startswith('Count: 3 |') for text in rendered))
        self.assertEqual(screen.timeout_value, -1)

    def test_refresh_keeps_pending_count_and_selected_message_anchor(self) -> None:
        screen = FakeScreen([*'3j', '\n', 'q'])
        refresh = Future()
        refresh.set_result([row('test', '101', 'newest'), *self.rows])

        def keypress():
            key = next(screen.keys)
            if key == '3':
                inbox.REFRESH_JOB = refresh
            return key

        screen.get_wch = keypress
        self.assertEqual(self.browse(screen, self.rows.copy()), [self.rows[3]])

    def test_unicode_numeric_characters_do_not_break_input(self) -> None:
        keys = ['3', '²', '٣', 'j', '\n', 'q']
        self.assertEqual(self.browse(FakeScreen(keys)), [self.rows[1]])

    def test_reader_counts_scroll_lines_and_are_consumed_once(self) -> None:
        frames = self.reader([*'3j3k20jj', 'q'])
        self.assertEqual(frames, ['line-000', 'line-000', 'line-003', 'line-003', 'line-000',
                                  'line-000', 'line-000', 'line-020', 'line-021'])

    def test_reader_count_clamps_to_last_page_and_first_line(self) -> None:
        frames = self.reader([*'999j999k', 'q'])
        self.assertEqual(frames[4], 'line-094')  # Six visible body lines.
        self.assertEqual(frames[-1], 'line-000')

    def test_reader_search_digits_stay_text_and_cancel_pending_count(self) -> None:
        frames = self.reader(['9', '/', *'line-070', '\n', 'k', 'q'])
        self.assertEqual(frames[-2:], ['line-070', 'line-069'])

    def test_reader_escape_cancels_count_and_resize_preserves_it(self) -> None:
        self.assertEqual(self.reader(['3', '\x1b', 'j', 'q'])[-1], 'line-001')
        self.assertEqual(self.reader(['3', curses.KEY_RESIZE, 'j', 'q'])[-1], 'line-003')

    def test_reader_action_returns_once_and_does_not_leak_count_to_next_view(self) -> None:
        screen = FakeScreen(['3', 'r', 'j', 'q'])
        text = '\n'.join(f'line-{i}' for i in range(50))
        self.assertEqual(inbox.text_view(screen, text, 'help', {'r', 'q'}), 'r')
        rendered = []
        screen.addnstr = lambda y, x, text, *args: rendered.append((y, text))
        self.assertEqual(inbox.text_view(screen, text, 'help', {'q'}), 'q')
        self.assertIn((1, 'line-1'), rendered)
        self.assertNotIn((1, 'line-3'), rendered)

    def test_chooser_counts_and_boundaries(self) -> None:
        choices = [item['subject'] for item in self.rows]
        for keys, expected in [(list('20j3kj'), 18), (list('999j'), 99),
                               (list('3j99k'), 0), (['3', '?', 'j'], 1),
                               (['3', curses.KEY_RESIZE, 'j'], 3), (['3'], 0)]:
            with self.subTest(keys=keys):
                self.assertEqual(inbox.choose(FakeScreen([*keys, '\n']), 'Test', choices), expected)
        self.assertIsNone(inbox.choose(FakeScreen(['3', '\x1b']), 'Test', choices))


if __name__ == '__main__':
    unittest.main()
