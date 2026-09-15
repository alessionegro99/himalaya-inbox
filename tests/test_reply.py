"""Offline thread and sending-safety tests; all addresses and mail are synthetic."""

from email import policy
from email.parser import Parser
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_inbox import FakeScreen, inbox, row


CONFIG = {
    'work': {'email': 'me@example.org', 'display-name': 'Example User',
             'imap': {'server': 'imaps://mail.example.org:993'},
             'smtp': {'server': 'smtps://mail.example.org:465'}},
    'other': {'email': 'me@other.example', 'smtp': {'server': 'smtps://smtp.gmail.com:465'}},
}
SOURCE = ('From: Sender <sender@example.net>\nReply-To: reply@example.net\n'
          'To: me@example.org, colleague@example.net\nCc: me@other.example, team@example.net\n'
          'Bcc: hidden@example.net\nSubject: Example conversation\nMessage-ID: <child@example.net>\n'
          'References: <root@example.net> <middle@example.net>\n\nOriginal text.\n')


class ReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = patch.object(inbox, 'SETTINGS', CONFIG)
        self.config.start()
        self.addCleanup(self.config.stop)

    def test_references_bridge_missing_parent(self) -> None:
        first = row('work', '3', '<latest>', ['missing'])
        first['references'] = ['root', 'missing']
        root = row('work', '1', 'root')
        unrelated = row('work', '2', 'other')
        self.assertEqual(inbox.conversations([first, unrelated, root]), [[first, root], [unrelated]])

    def test_dedup_keeps_folder_uids_separate_and_prefers_inbox(self) -> None:
        first = dict(row('work', '1', 'same'), mailbox='INBOX', in_inbox=True)
        copy = dict(row('work', '9', '<same>'), mailbox='Sent', sent=True)
        different = dict(row('work', '1', 'different'), mailbox='Sent', sent=True)
        other_account = dict(row('other', '9', 'same'), mailbox='INBOX', in_inbox=True)
        result = inbox.deduplicate([copy, first, different, other_account])
        self.assertEqual(len(result), 3)
        original = next(r for r in result if r['account'] == 'work' and r['message-id'] == 'same')
        self.assertEqual(original['mailbox'], 'INBOX')
        self.assertTrue(original['sent'])

    def test_reference_literal_parser_supports_uid_before_or_after_literal(self) -> None:
        header = b'References: <root>\r\n <middle>\r\n\r\n'
        output = (b'hi1 OK selected\r\n* 1 FETCH (UID 42 BODY[HEADER.FIELDS (REFERENCES)] {'
                  + str(len(header)).encode() + b'}\r\n' + header + b')\r\n'
                  + b'* 2 FETCH (BODY[HEADER.FIELDS (REFERENCES)] {'
                  + str(len(header)).encode() + b'}\r\n' + header + b' UID 43)\r\nhi2 OK done\r\n')
        self.assertEqual(inbox.parse_reference_fetch(output), {'42': ['root', 'middle'], '43': ['root', 'middle']})
        with self.assertRaises(RuntimeError):
            inbox.parse_reference_fetch(output.replace(b'hi2 OK', b'hi2 NO'))

    def test_header_fetch_is_peek_and_read_only(self) -> None:
        item = row('work', '42', 'one')
        with patch.object(inbox, 'run_private', return_value=b'fake') as run, \
                patch.object(inbox, 'parse_reference_fetch', return_value={'42': ['root']}):
            inbox.add_references('work', 'Sent', [item])
        command = run.call_args.args[1]
        self.assertIn(b'EXAMINE "Sent"', command)
        self.assertIn(b'BODY.PEEK[HEADER.FIELDS (REFERENCES)]', command)
        self.assertTrue(command.endswith(b'\r\n\r\n'))
        self.assertNotIn(b'STORE', command)
        self.assertEqual(item['references'], ['root'])

    def test_reader_scrolls_to_last_and_first_lines(self) -> None:
        screen = FakeScreen(['G', 'g', 'q'], (8, 100))
        rendered = []
        screen.addnstr = lambda y, x, text, *args: rendered.append((y, text))
        inbox.text_view(screen, '\n'.join(f'line-{i}' for i in range(30)), 'help', {'q'})
        self.assertIn((6, 'line-29'), rendered)
        self.assertEqual(sum(item == (1, 'line-0') for item in rendered), 2)

    def test_thread_opens_in_chronological_order_with_sent_replies(self) -> None:
        earlier = dict(row('work', '1', 'root'), date='2026-01-01T10:00:00Z', in_inbox=True)
        sent = dict(row('work', '2', 'sent', ['root']), date='2026-01-01T11:00:00Z', sent=True, in_inbox=False)
        latest = dict(row('work', '3', 'latest', ['sent']), date='2026-01-01T12:00:00Z', in_inbox=True)
        screen = FakeScreen(['\n', '\n', 'j', '\n', 'j', '\n', 'q', 'q'])
        with patch.object(inbox.curses, 'curs_set'), patch.object(inbox.curses, 'mousemask'), \
                patch.object(inbox, 'show_message') as show:
            inbox.browse(screen, [latest, sent, earlier])
        self.assertEqual([call.args[1]['id'] for call in show.call_args_list], ['1', '2', '3'])

    def test_mailbox_encoding_and_command_injection_rejection(self) -> None:
        self.assertEqual(inbox.imap_mailbox('A&B'), '"A&-B"')
        self.assertEqual(inbox.imap_mailbox('台北'), '"&U,BTFw-"')
        for invalid in ('INBOX\r\nhi3 EXPUNGE', 'box\\name', 'box\x00'):
            with self.assertRaises(RuntimeError):
                inbox.imap_mailbox(invalid)

    def test_reply_recipient_and_complete_ancestry(self) -> None:
        draft = inbox.reply_template(SOURCE, 'work')
        parsed = Parser(policy=policy.default).parsestr(draft)
        self.assertEqual(str(parsed['To']), 'reply@example.net')
        self.assertFalse(parsed['Cc'])
        self.assertNotIn('hidden@example.net', draft)
        self.assertEqual(inbox.message_ids(parsed['References']), ['root@example.net', 'middle@example.net', 'child@example.net'])
        account, message = inbox.build_message(draft)
        self.assertEqual(account, 'work')
        self.assertEqual(message['From'].addresses[0].addr_spec, 'me@example.org')
        self.assertIsNone(message['X-Himalaya-Account'])

    def test_reply_all_excludes_all_own_addresses_and_bcc(self) -> None:
        parsed = Parser(policy=policy.default).parsestr(inbox.reply_template(SOURCE, 'work', True))
        self.assertEqual({a.addr_spec for a in parsed['To'].addresses}, {'reply@example.net', 'colleague@example.net'})
        self.assertEqual({a.addr_spec for a in parsed['Cc'].addresses}, {'team@example.net'})
        self.assertIsNone(parsed['Bcc'])

    def test_reply_to_own_sent_message_uses_original_recipient(self) -> None:
        raw = 'From: me@example.org\nTo: sender@example.net\nSubject: Test\n\nText'
        parsed = Parser(policy=policy.default).parsestr(inbox.reply_template(raw, 'work'))
        self.assertEqual(str(parsed['To']), 'sender@example.net')

    def test_invalid_drafts_cannot_be_sent(self) -> None:
        good = 'X-Himalaya-Account: work\nTo: you@example.net\nSubject: Test\n\nBody'
        bad = [good.replace('work', 'unknown'), good.replace('you@example.net', 'invalid'),
               good.replace('Subject:', 'Bcc: unexpected@example.net\nSubject:'),
               good.replace('Subject:', 'To: duplicate@example.net\nSubject:'), good.replace('\n\nBody', '\n\n')]
        for draft in bad:
            with self.assertRaises(RuntimeError):
                inbox.build_message(draft)

    def test_success_sends_once_and_saves_sent_without_resending(self) -> None:
        _, message = inbox.build_message(inbox.reply_template(SOURCE, 'work'))
        with patch.object(inbox, 'run_private', return_value=b'') as run:
            result = inbox.send_confirmed('work', message)
        self.assertTrue(result.startswith('SENT.'))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][-2:], ['message', 'send'])
        self.assertIn('add', run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[0].args[1], run.call_args_list[1].args[1])

    def test_gmail_does_not_duplicate_automatic_sent_copy(self) -> None:
        _, message = inbox.build_message(inbox.reply_template(SOURCE, 'other'))
        with patch.object(inbox, 'run_private', return_value=b'') as run:
            self.assertTrue(inbox.send_confirmed('other', message).startswith('SENT.'))
        self.assertEqual(run.call_count, 1)

    def test_uncertain_send_is_never_retried(self) -> None:
        _, message = inbox.build_message(inbox.reply_template(SOURCE, 'work'))
        with patch.object(inbox, 'run_private', side_effect=RuntimeError('PRIVATE')) as run:
            result = inbox.send_confirmed('work', message)
        self.assertEqual(run.call_count, 1)
        self.assertIn('uncertain', result)
        self.assertNotIn('PRIVATE', result)

    def test_sent_copy_failure_is_not_reported_as_send_failure(self) -> None:
        _, message = inbox.build_message(inbox.reply_template(SOURCE, 'work'))
        with patch.object(inbox, 'run_private', side_effect=[b'', RuntimeError('PRIVATE')]) as run:
            result = inbox.send_confirmed('work', message)
        self.assertEqual(run.call_count, 2)
        self.assertIn('SENT,', result)
        self.assertIn('Do NOT resend', result)

    def test_saving_editor_never_sends_without_exact_confirmation(self) -> None:
        for decision, confirmation, sent in (('q', 'SEND NOW', False), ('N', 'SEND', False), ('N', 'SEND NOW', True)):
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(inbox, 'draft_directory', return_value=Path(directory)), \
                        patch.object(inbox, 'read_raw', return_value=SOURCE), \
                        patch.object(inbox.shutil, 'which', return_value='/synthetic/nvim'), \
                        patch.object(inbox, 'terminal_child'), \
                        patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as editor, \
                        patch.object(inbox, 'text_view', side_effect=[decision, 'q']), \
                        patch.object(inbox, 'input_line', return_value=confirmation), \
                        patch.object(inbox, 'send_confirmed', return_value='SENT. Test') as send:
                    inbox.compose(FakeScreen([]), row('work', '1', 'one'))
                self.assertEqual(send.call_count, int(sent))
                self.assertEqual(editor.call_args.args[0][0], '/synthetic/nvim')
                self.assertIn('nomodeline', editor.call_args.args[0][7])
                paths = list(Path(directory).glob('*.txt'))
                self.assertEqual(len(paths), 0 if sent else 1)
                if paths:
                    self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
