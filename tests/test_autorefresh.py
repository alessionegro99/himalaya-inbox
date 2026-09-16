"""Automatic refresh timing and lifecycle, using synthetic mail only."""

from concurrent.futures import Future, ThreadPoolExecutor
import curses
from io import StringIO
import sys
from threading import Event, Thread
import unittest
from unittest.mock import Mock, call, patch

from test_inbox import FakeScreen, inbox, row


def completed(value=None, error=None) -> Future:
    future = Future()
    if error is None:
        future.set_result(value)
    else:
        future.set_exception(error)
    return future


class AutoRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in [('IO_STOP', Event()), ('REFRESH_JOB', None), ('REFRESH_SEEN_STATES', {}),
                            ('SEEN_STATES', {}), ('AUTO_REFRESH_ENABLED', False),
                            ('PREFETCH_ENABLED', False), ('HEADER_CACHE_PATH', None)]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)
        transport = patch.object(inbox, 'run_private', side_effect=AssertionError('Live mail is forbidden in this test'))
        transport.start()
        self.addCleanup(transport.stop)

    def test_timer_waits_five_minutes_before_each_attempt(self) -> None:
        stop = Mock()
        stop.wait.side_effect = [False, False, True]
        with patch.object(inbox, 'IO_STOP', stop), patch.object(inbox, 'start_refresh') as start:
            inbox.auto_refresh()
        self.assertEqual(inbox.AUTO_REFRESH_INTERVAL, 300)
        self.assertEqual(stop.wait.call_args_list, [call(300), call(300), call(300)])
        self.assertEqual(start.call_count, 2)

    def test_timer_refreshes_repeatedly_without_ui_input_and_stops_promptly(self) -> None:
        twice = Event()
        calls = []

        def load():
            calls.append(None)
            if len(calls) >= 2:
                twice.set()
            return []

        # No curses calls or keyboard polling: also covers time spent in Neovim.
        with patch.object(inbox, 'AUTO_REFRESH_INTERVAL', 0.01), patch.object(inbox, 'load_all', side_effect=load):
            timer = Thread(target=inbox.auto_refresh, daemon=True)
            timer.start()
            try:
                self.assertTrue(twice.wait(2))
            finally:
                inbox.IO_STOP.set()
                timer.join(timeout=1)
                if inbox.REFRESH_JOB is not None:
                    inbox.REFRESH_JOB.result(timeout=1)
        self.assertFalse(timer.is_alive())
        self.assertGreaterEqual(len(calls), 2)

    def test_manual_and_timer_requests_cannot_start_overlapping_refreshes(self) -> None:
        pending = Future()
        with patch.object(inbox, 'background', return_value=pending) as start:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: inbox.start_refresh(), range(20)))
        start.assert_called_once_with(inbox.load_all)
        self.assertIs(inbox.REFRESH_JOB, pending)

    def test_completed_unseen_snapshot_can_be_replaced_while_reading(self) -> None:
        old = inbox.REFRESH_JOB = completed(['old'])
        saved = {'pending': 0, 'seen': True}
        pending_change = {'pending': 1, 'seen': False}
        inbox.SEEN_STATES.update({('saved',): saved, ('pending',): pending_change})
        newer = Future()
        with patch.object(inbox, 'background', return_value=newer) as start:
            inbox.start_refresh()
        start.assert_called_once()
        self.assertIs(inbox.REFRESH_JOB, newer)
        self.assertIsNot(old, newer)
        self.assertEqual(inbox.REFRESH_SEEN_STATES, {('saved',): saved})
        self.assertIs(inbox.REFRESH_SEEN_STATES[('saved',)], saved)

    def test_failed_refresh_retries_on_next_interval_not_in_a_tight_loop(self) -> None:
        stop = Mock()
        stop.wait.side_effect = [False, False, True]
        stop.is_set.return_value = False
        failed = completed(error=RuntimeError('PRIVATE_DIAGNOSTIC'))
        recovered = completed([])
        with patch.object(inbox, 'IO_STOP', stop), \
                patch.object(inbox, 'background', side_effect=[failed, recovered]) as start:
            inbox.auto_refresh()
        self.assertEqual(start.call_count, 2)
        self.assertEqual(stop.wait.call_args_list, [call(300), call(300), call(300)])
        self.assertIs(inbox.take_refresh()[0], recovered)

    def test_shutdown_prevents_more_refreshes(self) -> None:
        inbox.IO_STOP.set()
        with patch.object(inbox, 'background') as start:
            inbox.auto_refresh()
            inbox.start_refresh()
        start.assert_not_called()

    def test_handoff_keeps_result_and_read_state_snapshot_together(self) -> None:
        first = inbox.REFRESH_JOB = completed(['first'])
        first_states = inbox.REFRESH_SEEN_STATES = {('first',): {'pending': 0}}
        taken, states = inbox.take_refresh()
        second_state = {'pending': 0}
        inbox.SEEN_STATES[('second',)] = second_state
        with patch.object(inbox, 'background', return_value=completed(['second'])):
            inbox.start_refresh()
        self.assertIs(taken, first)
        self.assertIs(states, first_states)
        self.assertEqual(states, {('first',): {'pending': 0}})
        self.assertEqual(inbox.take_refresh()[1], {('second',): second_state})

    def test_handoff_never_waits_for_a_running_request(self) -> None:
        pending = inbox.REFRESH_JOB = Future()
        self.assertEqual(inbox.take_refresh(), (None, {}))
        self.assertIs(inbox.REFRESH_JOB, pending)
        self.assertFalse(pending.cancelled())

    def test_idle_browser_receives_refresh_without_keys_and_preserves_selection(self) -> None:
        inbox.AUTO_REFRESH_ENABLED = True
        items = [row('test', '1', 'one'), row('test', '2', 'two')]
        newest = row('test', '3', 'three')
        screen = FakeScreen([])
        keys = iter(['j', 'timeout', '\n', 'q'])
        rendered = []
        screen.addnstr = lambda y, x, text, *args: rendered.append(text)

        def key():
            value = next(keys)
            if value == 'timeout':
                self.assertEqual(screen.timeout_value, 1000)
                inbox.REFRESH_JOB = completed([newest, *items])
                raise curses.error()
            return value

        screen.get_wch = key
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'), \
                patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, items, threaded=False)
        self.assertEqual(show.call_args.args[1]['id'], '2')
        self.assertEqual(items[0], newest)
        self.assertIn('auto-refresh 5m', ''.join(rendered))
        self.assertEqual(screen.timeout_value, -1)

    def test_automatic_refresh_preserves_active_filter(self) -> None:
        inbox.AUTO_REFRESH_ENABLED = True
        alpha = row('test', '1', 'one', subject='Alpha')
        beta = row('test', '2', 'two', subject='Beta')
        newer = row('test', '3', 'three', subject='Beta newer')
        items = [alpha, beta]
        screen = FakeScreen([])
        keys = iter(['/', *'beta', '\n', 'timeout', '\n', 'q'])

        def key():
            value = next(keys)
            if value == 'timeout':
                inbox.REFRESH_JOB = completed([newer, *items])
                raise curses.error()
            return value

        screen.get_wch = key
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'), \
                patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, items, threaded=False)
        self.assertEqual(show.call_args.args[1]['id'], '2')

    def test_interactive_lifecycle_starts_timer_and_cleans_up_on_error(self) -> None:
        for failure in (None, RuntimeError('synthetic UI failure')):
            with patch.object(sys, 'argv', ['inbox']), patch.object(sys.stdin, 'isatty', return_value=True), \
                    patch.object(sys.stdout, 'isatty', return_value=True), patch.object(sys, 'stderr', StringIO()), \
                    patch.object(inbox, 'read_settings'), patch.object(inbox, 'HEADER_CACHE_READY', False), \
                    patch.object(inbox, 'load_all', return_value=[]), patch.object(inbox, 'Thread') as thread, \
                    patch.object(inbox.curses, 'wrapper', side_effect=failure):
                if failure is None:
                    inbox.main()
                else:
                    with self.assertRaises(RuntimeError):
                        inbox.main()
            thread.assert_called_once_with(target=inbox.auto_refresh, name='inbox-auto-refresh', daemon=True)
            thread.return_value.start.assert_called_once()
            thread.return_value.join.assert_called_once_with(timeout=1)
            self.assertTrue(inbox.IO_STOP.is_set())
            self.assertFalse(inbox.AUTO_REFRESH_ENABLED)

    def test_noninteractive_listing_does_not_start_timer(self) -> None:
        with patch.object(sys, 'argv', ['inbox', '--list']), patch.object(sys.stdin, 'isatty', return_value=False), \
                patch.object(sys, 'stdout', StringIO()), patch.object(sys, 'stderr', StringIO()), \
                patch.object(inbox, 'run_himalaya', return_value={'accounts': [{'name': 'test'}]}), \
                patch.object(inbox, 'fetch', return_value=[]), patch.object(inbox, 'Thread') as thread:
            inbox.main()
        thread.assert_not_called()

    def test_background_sender_does_not_start_refresh_timer(self) -> None:
        with patch.object(sys, 'argv', ['inbox', '--process-outbox']), \
                patch.object(inbox, 'process_outbox') as process, patch.object(inbox, 'Thread') as thread:
            inbox.main()
        process.assert_called_once()
        thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
