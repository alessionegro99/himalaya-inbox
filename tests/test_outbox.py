"""Fake-clock tests for persistent delays, cancellation and exactly-once claims."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_inbox import FakeScreen, inbox, row
from test_reply import CONFIG, SOURCE


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for patcher in (
            patch.dict(os.environ, {'XDG_STATE_HOME': self.directory.name}),
            patch.object(inbox, 'SETTINGS', CONFIG),
            patch.object(inbox, 'CONFIG_ARGS', ['--config', '/synthetic/config.toml']),
            patch.object(inbox.time, 'time', return_value=1000),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        _, self.message = inbox.build_message(inbox.reply_template(SOURCE, 'work'))

    def queue(self) -> str:
        return inbox.queue_message('work', self.message)

    def test_default_is_five_minutes_and_never_sends_early(self) -> None:
        identifier = self.queue()
        item = inbox.pending_messages()[0]
        self.assertEqual(item['due'], 1300)
        self.assertEqual(item['status'], 'pending')
        self.assertIsNone(inbox.claim_due(1299.999))
        self.assertEqual(inbox.claim_due(1300)['id'], identifier)
        self.assertIsNone(inbox.claim_due(1400))

    def test_cancellation_removes_queued_body_and_blocks_delivery(self) -> None:
        identifier = self.queue()
        self.assertTrue(inbox.cancel_queued(identifier))
        self.assertFalse(inbox.cancel_queued(identifier))
        self.assertIsNone(inbox.claim_due(2000))
        item = inbox.pending_messages()[0]
        self.assertEqual(item['status'], 'cancelled')
        self.assertIsNone(item['payload'])

    def test_outbox_x_cancels_and_restores_blocking_keyboard(self) -> None:
        self.queue()
        screen = FakeScreen(['x', 'q'])
        with patch.object(inbox.curses, 'curs_set'):
            inbox.outbox_view(screen)
        self.assertEqual(inbox.pending_messages()[0]['status'], 'cancelled')
        self.assertEqual(screen.timeout_value, -1)

    def test_cannot_claim_twice_or_cancel_after_delivery_starts(self) -> None:
        identifier = self.queue()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(inbox.claim_due, [1300] * 4))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertFalse(inbox.cancel_queued(identifier))
        # This also simulates a crash after claim: it must NOT resend next time.
        self.assertIsNone(inbox.claim_due(2000))

    def test_worker_never_contacts_transport_before_deadline(self) -> None:
        self.queue()
        with patch.object(inbox, 'read_settings') as config, patch.object(inbox, 'send_confirmed') as send:
            inbox.process_outbox()
        config.assert_not_called()
        send.assert_not_called()

    def test_due_worker_claims_before_send_and_sends_only_once(self) -> None:
        identifier = self.queue()

        def fake_send(account, message):
            self.assertEqual(inbox.pending_messages()[0]['status'], 'sending')
            self.assertFalse(inbox.cancel_queued(identifier))
            self.assertEqual(account, 'work')
            self.assertEqual(str(message['To']), 'reply@example.net')
            return 'SENT. Synthetic only.'

        with patch.object(inbox.time, 'time', return_value=1300), patch.object(inbox, 'read_settings'), \
                patch.object(inbox, 'send_confirmed', side_effect=fake_send) as send:
            inbox.process_outbox()
            inbox.process_outbox()
        send.assert_called_once()
        item = inbox.pending_messages()[0]
        self.assertEqual(item['status'], 'sent')
        self.assertIsNone(item['payload'])

    def test_uncertain_delivery_is_kept_and_never_retried(self) -> None:
        self.queue()
        with patch.object(inbox.time, 'time', return_value=1400), patch.object(inbox, 'read_settings'), \
                patch.object(inbox, 'send_confirmed', return_value='Delivery is uncertain.') as send:
            inbox.process_outbox()
            inbox.process_outbox()
        send.assert_called_once()
        item = inbox.pending_messages()[0]
        self.assertEqual(item['status'], 'uncertain')
        self.assertIsNotNone(item['payload'])

    def test_config_identity_change_blocks_delivery(self) -> None:
        self.queue()
        changed = dict(CONFIG, work=dict(CONFIG['work'], email='changed@example.org'))
        with patch.object(inbox.time, 'time', return_value=1400), patch.object(inbox, 'read_settings'), \
                patch.object(inbox, 'SETTINGS', changed), patch.object(inbox, 'send_confirmed') as send:
            inbox.process_outbox()
        send.assert_not_called()
        self.assertNotEqual(inbox.pending_messages()[0]['status'], 'pending')

    def test_storage_is_private_and_outside_worktree(self) -> None:
        self.queue()
        path = Path(self.directory.name) / 'himalaya-inbox/outbox.sqlite3'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_invalid_delays_are_rejected(self) -> None:
        for value in (0, -1, 1441):
            with self.assertRaises(RuntimeError):
                inbox.queue_message('work', self.message, value)

    def test_default_confirmation_only_queues_not_sends(self) -> None:
        with patch.object(inbox, 'read_raw', return_value=SOURCE), \
                patch.object(inbox.shutil, 'which', return_value='/synthetic/nvim'), \
                patch.object(inbox, 'terminal_child'), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(inbox, 'text_view', side_effect=['s', 'q']), \
                patch.object(inbox, 'input_line', return_value='SEND'), \
                patch.object(inbox, 'scheduler_active', return_value=True), \
                patch.object(inbox, 'send_confirmed') as send:
            status = inbox.compose(FakeScreen([]), row('work', '1', 'one'))
        self.assertTrue(status.startswith('QUEUED'))
        send.assert_not_called()
        self.assertEqual(inbox.pending_messages()[0]['due'], 1300)

    def test_missing_scheduler_does_not_create_a_silent_queue(self) -> None:
        with patch.object(inbox, 'read_raw', return_value=SOURCE), \
                patch.object(inbox.shutil, 'which', return_value='/synthetic/nvim'), \
                patch.object(inbox, 'terminal_child'), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(inbox, 'text_view', side_effect=['s', 'q']), \
                patch.object(inbox, 'scheduler_active', return_value=False), \
                patch.object(inbox, 'send_confirmed') as send:
            status = inbox.compose(FakeScreen([]), row('work', '1', 'one'))
        self.assertIn('not active', status)
        send.assert_not_called()
        self.assertEqual(inbox.pending_messages(), [])


if __name__ == '__main__':
    unittest.main()
