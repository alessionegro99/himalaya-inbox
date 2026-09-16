"""Synthetic attachments only: MIME round trips, private files and UI actions."""

from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser, Parser
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_inbox import FakeScreen, inbox, row
from test_reply import CONFIG, SOURCE


DRAFT = 'X-Himalaya-Account: work\nTo: you@example.net\nSubject: Synthetic files\n\nBody\n'
BINARY = bytes(range(256)) + b'\x00\xff\r\n\x1b'


def attachment(name: str = 'report.pdf', data: bytes = BINARY) -> EmailMessage:
    message = EmailMessage(policy=policy.SMTP)
    message.set_content('Synthetic message body.')
    message.add_attachment(data, maintype='application', subtype='pdf', filename=name)
    return list(message.iter_attachments())[0]


def with_files(draft: str, *paths: Path) -> str:
    header, body = draft.split('\n\n', 1)
    return header + ''.join(f'\nAttach: {path}' for path in paths) + '\n\n' + body


class IncomingAttachmentTests(unittest.TestCase):
    def test_decode_binary_and_unicode_filename_without_showing_contents(self) -> None:
        part = attachment('résultats 台北.pdf')
        raw = part.as_string()
        decoded = Parser(policy=policy.default).parsestr(raw)
        self.assertEqual(inbox.attachment_bytes(decoded), BINARY)
        self.assertEqual(inbox.attachment_name(decoded), 'résultats 台北.pdf')
        self.assertEqual(inbox.attachment_parts(decoded), [decoded])
        self.assertNotIn('AAECAwQF', inbox.message_text(raw, row('test', '42', 'one')))

    def test_inline_images_and_attached_mail_are_separate_files(self) -> None:
        message = EmailMessage()
        message.set_content('Plain body')
        message.add_alternative('<p>HTML body</p><img src="cid:logo">', subtype='html')
        message.get_payload()[1].add_related(b'IMAGE', maintype='image', subtype='png',
                                            cid='<logo>', disposition='inline')
        nested = EmailMessage()
        nested['Subject'] = 'Attached message'
        nested.set_content('Nested text')
        nested.add_attachment(b'INNER', maintype='application', subtype='pdf', filename='inside.pdf')
        message.add_attachment(nested, filename='forwarded.eml')
        parts = inbox.attachment_parts(message)
        self.assertEqual(len(parts), 2)
        self.assertEqual(inbox.attachment_bytes(parts[0]), b'IMAGE')
        self.assertEqual(inbox.attachment_name(parts[0]), 'attachment.png')
        saved_mail = BytesParser(policy=policy.default).parsebytes(inbox.attachment_bytes(parts[1]))
        self.assertEqual(str(saved_mail['Subject']), 'Attached message')
        self.assertEqual(inbox.attachment_bytes(inbox.attachment_parts(saved_mail)[0]), b'INNER')

    def test_untrusted_names_are_safe_visible_basenames(self) -> None:
        cases = [('../../report.pdf', 'report.pdf'), ('C:\\temp\\report.pdf', 'report.pdf'),
                 ('.hidden.pdf', 'hidden.pdf'), ('--report.pdf', 'report.pdf'),
                 ('..', 'attachment.pdf'), ('fake\u202eexe.pdf', 'fake_exe.pdf')]
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(inbox.attachment_name(attachment(original)), expected)
        name = inbox.attachment_name(attachment('台' * 200 + '.pdf'))
        self.assertLess(len(name.encode('utf-8')), 220)
        self.assertTrue(name.endswith('.pdf'))

    def test_save_is_private_exact_and_does_not_follow_existing_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            victim = base / 'existing'
            victim.write_bytes(b'PRESERVE')
            downloads = base / 'downloads'
            downloads.mkdir()
            (downloads / 'report.pdf').symlink_to(victim)
            first = inbox.save_attachment(attachment('../../report.pdf'), downloads)
            second = inbox.save_attachment(attachment(), downloads)
            self.assertEqual(first.name, 'report (1).pdf')
            self.assertEqual(second.name, 'report (2).pdf')
            self.assertEqual(first.read_bytes(), BINARY)
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual(victim.read_bytes(), b'PRESERVE')

    def test_simultaneous_downloads_never_overwrite_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: inbox.save_attachment(attachment(), Path(directory)), range(4)))
            self.assertEqual(len(set(results)), 4)
            self.assertTrue(all(path.read_bytes() == BINARY for path in results))

    def test_private_diagnostics_and_decode_errors_do_not_escape(self) -> None:
        with patch.object(inbox, 'attachment_parts', side_effect=ValueError('PRIVATE')):
            status = inbox.show_attachments(FakeScreen([]), 'Synthetic')
        self.assertNotIn('PRIVATE', status)
        part = attachment()
        with patch.object(part, 'get_payload', side_effect=ValueError('PRIVATE')):
            with self.assertRaises(RuntimeError) as caught:
                inbox.attachment_bytes(part)
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_quoted_printable_and_corrupted_base64(self) -> None:
        raw = ('Content-Type: application/octet-stream\nContent-Disposition: attachment; filename=file.bin\n'
               'Content-Transfer-Encoding: quoted-printable\n\n=00=FF=3D\n')
        self.assertEqual(inbox.attachment_bytes(Parser(policy=policy.default).parsestr(raw)), b'\x00\xff=\n')
        raw = raw.replace('quoted-printable', 'base64').replace('=00=FF=3D', 'bad!base64!')
        with self.assertRaises(RuntimeError):
            inbox.attachment_bytes(Parser(policy=policy.default).parsestr(raw))

    def test_desktop_download_directory_and_fallback(self) -> None:
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'/synthetic/downloads\n')):
            self.assertEqual(inbox.download_directory(), Path('/synthetic/downloads'))
        with patch.object(subprocess, 'run', side_effect=FileNotFoundError), patch.object(Path, 'home', return_value=Path('/synthetic')):
            self.assertEqual(inbox.download_directory(), Path('/synthetic/Downloads'))

    def test_open_uses_literal_absolute_arguments_and_suppresses_diagnostics(self) -> None:
        path = Path('/synthetic/report;$(not-a-command).pdf')
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertIn('Opened', inbox.open_attachment(path))
        self.assertEqual(run.call_args.args[0], ['gio', 'open', str(path)])
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertEqual(run.call_args.kwargs['stdout'], subprocess.DEVNULL)
        with patch.object(subprocess, 'run', side_effect=OSError('PRIVATE')):
            self.assertNotIn('PRIVATE', inbox.open_attachment(path))
        with patch.object(subprocess, 'run') as run:
            self.assertIn('not opened', inbox.open_attachment(Path('/synthetic/run.desktop')))
            run.assert_not_called()

    def test_browsing_and_cancelling_do_not_save_or_open(self) -> None:
        screen = FakeScreen(['\n', 'q', 'q'])
        with patch.object(inbox, 'save_attachment') as save, patch.object(inbox, 'open_attachment') as launch:
            inbox.show_attachments(screen, attachment().as_string())
        save.assert_not_called()
        launch.assert_not_called()

    def test_save_then_open_reuses_the_download_and_returns_to_reader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            screen = FakeScreen(['a', '\n', 's', 'o', 'q', 'q', 'q'])
            with patch.object(inbox, 'read_raw', return_value=attachment().as_string()) as fetch, \
                    patch.object(inbox, 'mark_seen', return_value=''), \
                    patch.object(inbox, 'download_directory', return_value=Path(directory)), \
                    patch.object(inbox, 'open_attachment', return_value='Opened.') as launch:
                inbox.show_message(screen, dict(row('test', '42', 'one'), mailbox='Archive'))
            fetch.assert_called_once()
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), BINARY)
            launch.assert_called_once_with(files[0])


class OutgoingAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        setting = patch.object(inbox, 'SETTINGS', CONFIG)
        setting.start()
        self.addCleanup(setting.stop)

    def test_new_mail_and_replies_preserve_exact_bytes_and_hide_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'résultats with spaces.pdf'
            path.write_bytes(BINARY)
            for draft in (DRAFT, inbox.reply_template(SOURCE, 'work'), inbox.reply_template(SOURCE, 'work', True)):
                _, message = inbox.build_message(with_files(draft, path))
                raw = message.as_bytes()
                parsed = BytesParser(policy=policy.default).parsebytes(raw)
                parts = inbox.attachment_parts(parsed)
                self.assertEqual(len(parts), 1)
                self.assertEqual(parts[0].get_filename(), path.name)
                self.assertEqual(parts[0].get_content_type(), 'application/pdf')
                self.assertEqual(inbox.attachment_bytes(parts[0]), BINARY)
                self.assertIsNone(parsed['Attach'])
                self.assertIsNone(parsed['X-Himalaya-Account'])
                self.assertNotIn(directory.encode(), raw)
                self.assertIn('Body' if draft == DRAFT else 'Original text', parsed.get_body().get_content())

    def test_empty_body_with_attachment_and_multiple_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'first.dat', Path(directory) / 'second.txt'
            first.write_bytes(b'')
            second.write_bytes(BINARY)
            _, message = inbox.build_message(with_files(DRAFT.replace('Body\n', ''), first, second))
            self.assertEqual([inbox.attachment_bytes(part) for part in inbox.attachment_parts(message)], [b'', BINARY])

    def test_body_paths_and_received_attach_headers_never_attach_local_files(self) -> None:
        _, message = inbox.build_message(DRAFT + '\nAttach: /synthetic/private-file\n')
        self.assertEqual(inbox.attachment_parts(message), [])
        source = SOURCE.replace('\n\n', '\nAttach: /synthetic/private-file\n\n', 1)
        _, reply = inbox.build_message(inbox.reply_template(source, 'work'))
        self.assertEqual(inbox.attachment_parts(reply), [])

    def test_missing_relative_directory_and_fifo_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / 'pipe'
            os.mkfifo(fifo)
            for path in (Path(directory) / 'missing', Path('relative.pdf'), Path(directory), fifo):
                with self.subTest(path=path), self.assertRaises(RuntimeError):
                    inbox.build_message(with_files(DRAFT, path))

    def test_total_size_limit_is_bounded_and_includes_every_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(inbox, 'MAX_ATTACHMENT_BYTES', 10):
            first, second = Path(directory) / 'one', Path(directory) / 'two'
            first.write_bytes(b'1' * 6)
            second.write_bytes(b'2' * 4)
            inbox.build_message(with_files(DRAFT, first, second))
            second.write_bytes(b'2' * 5)
            with self.assertRaisesRegex(RuntimeError, 'combined file-size limit'):
                inbox.build_message(with_files(DRAFT, first, second))

    def test_file_picker_browses_or_accepts_literal_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            folder = base / 'folder'
            folder.mkdir()
            path = folder / 'résultat $(literal).pdf'
            path.write_bytes(BINARY)
            with patch.object(Path, 'home', return_value=base):
                screen = FakeScreen(['j', 'j', '\n', 'j', 'j', '\n'])
                self.assertEqual(inbox.choose_file(screen), path)
                screen = FakeScreen(['\n', *str(path), '\n'])
                self.assertEqual(inbox.choose_file(screen), path)
                self.assertIsNone(inbox.choose_file(FakeScreen(['q'])))

    def test_adding_removing_and_resuming_preserve_utf8_paths_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ('long résumé ' * 9 + '.pdf')
            path.write_bytes(BINARY)
            draft_path = Path(directory) / 'draft.txt'
            with patch.object(inbox, 'choose_file', return_value=path):
                updated = inbox.change_attachments(FakeScreen([]), DRAFT)
            self.assertEqual(inbox.draft_attachments(updated), [str(path)])
            inbox.save_draft(draft_path, updated)
            self.assertEqual(draft_path.stat().st_mode & 0o777, 0o600)
            _, message = inbox.build_message(draft_path.read_text())
            self.assertEqual(inbox.attachment_bytes(inbox.attachment_parts(message)[0]), BINARY)
            with patch.object(inbox, 'choose', return_value=0):
                removed = inbox.change_attachments(FakeScreen([]), updated, remove=True)
            self.assertEqual(inbox.draft_attachments(removed), [])
            self.assertEqual(Parser(policy=policy.default).parsestr(removed).get_payload(), 'Body\n')

    def test_confirmed_payload_is_independent_of_subsequent_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.pdf'
            path.write_bytes(BINARY)
            _, message = inbox.build_message(with_files(DRAFT.replace('Body', 'Caffè 台北'), path))
            path.write_bytes(b'CHANGED')
            drafts = Path(directory) / 'state/drafts'
            drafts.mkdir(parents=True)
            with patch.object(inbox, 'draft_directory', return_value=drafts):
                inbox.queue_message('work', message)
                payload = inbox.pending_messages()[0]['payload']
                transported = []

                def mock_send(account, outgoing):
                    transported.append(BytesParser(policy=policy.default).parsebytes(outgoing.as_bytes()))
                    return 'SENT. Synthetic only.'

                with patch.object(inbox.time, 'time', return_value=10**12), \
                        patch.object(inbox, 'read_settings'), patch.object(inbox, 'send_confirmed', side_effect=mock_send):
                    inbox.process_outbox()
            queued = BytesParser(policy=policy.default).parsebytes(payload)
            self.assertEqual(inbox.attachment_bytes(inbox.attachment_parts(queued)[0]), BINARY)
            self.assertEqual(len(transported), 1)
            self.assertEqual(inbox.attachment_bytes(inbox.attachment_parts(transported[0])[0]), BINARY)
            self.assertIn('Caffè 台北', transported[0].get_body().get_content())

    def test_reading_raw_utf8_bodies_and_attached_messages(self) -> None:
        raw = ('Subject: Synthetic\nContent-Type: text/plain; charset=utf-8\n'
               'Content-Transfer-Encoding: 8bit\n\nCaffè 台北\n')
        self.assertIn('Caffè 台北', inbox.message_text(raw, row('test', '1', 'one')))
        nested = ('Content-Type: message/rfc822\nContent-Disposition: attachment; filename=mail.eml\n\n' + raw)
        part = BytesParser(policy=policy.default).parsebytes(nested.encode('utf-8'))
        saved = BytesParser(policy=policy.default).parsebytes(inbox.attachment_bytes(part))
        self.assertIn('Caffè 台北', saved.get_content())

    def test_compose_add_remove_and_cancel_keep_draft_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.pdf'
            path.write_bytes(BINARY)
            draft_path = Path(directory) / 'draft.txt'
            draft_path.write_text(DRAFT)
            with patch.object(inbox, 'terminal_child'), \
                    patch.object(inbox.shutil, 'which', return_value='/synthetic/nvim'), \
                    patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(inbox, 'choose_file', return_value=path), \
                    patch.object(inbox, 'choose', return_value=0), \
                    patch.object(inbox, 'text_view', side_effect=['a', 'x', 'a', 'q']) as view, \
                    patch.object(inbox, 'send_confirmed') as send, patch.object(inbox, 'queue_message') as queue:
                inbox.compose(FakeScreen([]), draft_path=draft_path)
            send.assert_not_called()
            queue.assert_not_called()
            self.assertEqual(inbox.draft_attachments(draft_path.read_text()), [str(path)])
            self.assertIn('a attach(1)', view.call_args_list[1].args[2])
            self.assertIn('a attach(0)', view.call_args_list[2].args[2])

    def test_failed_add_does_not_change_previous_draft_or_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            draft_path = Path(directory) / 'draft.txt'
            draft_path.write_text(DRAFT)
            with patch.object(inbox, 'terminal_child'), \
                    patch.object(inbox.shutil, 'which', return_value='/synthetic/nvim'), \
                    patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(inbox, 'choose_file', return_value=Path(directory) / 'missing'), \
                    patch.object(inbox, 'text_view', side_effect=['a', 'q']) as view:
                inbox.compose(FakeScreen([]), draft_path=draft_path)
            self.assertEqual(draft_path.read_text(), DRAFT)
            self.assertIn('Cannot read attachment', view.call_args.args[4])


if __name__ == '__main__':
    unittest.main()
