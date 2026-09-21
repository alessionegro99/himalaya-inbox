"""Synthetic IMAP transactions: exact UIDs, safe fallbacks, and no leaked credentials."""

from contextlib import nullcontext
from threading import Event
import unittest
from unittest.mock import Mock, patch

from test_inbox import inbox, row


class FakeIMAP:
    def __init__(self, capabilities=b'IMAP4rev1 MOVE UIDPLUS CONDSTORE') -> None:
        self.capabilities = capabilities
        self.epoch = 10
        self.mid = None
        self.missing = False
        self.copyuid = True
        self.copy_source = None
        self.fail = {}
        self.last_uid = '1'
        self.remaining = b''
        self.select = Mock(return_value=('OK', [b'3']))
        self.uid = Mock(side_effect=self.command)
        self.shutdown = Mock()

    def capability(self):
        return 'OK', [self.capabilities]

    def response(self, name):
        if name == 'UIDVALIDITY':
            return name, [str(self.epoch).encode()]
        return name, [f'20 {self.copy_source or self.last_uid} 99'.encode()] if self.copyuid else [None]

    def command(self, name, *args):
        failure = self.fail.get(name)
        if isinstance(failure, BaseException):
            raise failure
        if failure:
            return 'NO', [b'PRIVATE_DIAGNOSTICS']
        if name == 'FETCH':
            if self.missing:
                return 'OK', [None]
            mid = self.mid or 'message-' + args[0]
            return 'OK', [(f'3 (UID {args[0]} BODY[HEADER.FIELDS (MESSAGE-ID)] {{40}}'.encode(),
                           f'Message-ID: <{mid}>\r\n\r\n'.encode()), b')']
        if name == 'SEARCH':
            return 'OK', [self.remaining]
        self.last_uid = args[0]
        return 'OK', [b'Done']


def message(uid='1', **extra):
    return dict(row('test', uid, 'message-' + uid), mailbox='INBOX', uidvalidity=10, **extra)


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeIMAP()
        for name, value in [('SETTINGS', {'test': {'mailbox': {'alias': {'trash': 'Trash', 'archive': 'Archive', 'inbox': 'INBOX'}}}}),
                            ('IO_STOP', Event())]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)
        override = patch.object(inbox, 'action_connection', side_effect=lambda _: nullcontext(self.client))
        self.connection = override.start()
        self.addCleanup(override.stop)

    def commands(self):
        return [call.args for call in self.client.uid.call_args_list]

    def test_native_move_uses_one_connection_and_one_verified_uid(self) -> None:
        receipt = inbox.transfer_message(message('42'), 'Deleted Items')
        self.connection.assert_called_once_with('test')
        self.client.select.assert_called_once_with('"INBOX"')
        self.assertEqual(self.commands(), [('FETCH', '42', '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])'),
                                          ('MOVE', '42', '"Deleted Items"')])
        self.assertEqual(receipt['copyuid'], (20, '99'))

    def test_hiskp_style_server_uses_confirmed_copy_then_uid_only_expunge(self) -> None:
        self.client.capabilities = b'IMAP4rev1 UIDPLUS CONDSTORE'
        inbox.transfer_message(message(), 'Archive')
        self.assertEqual(self.commands()[1:], [('COPY', '1', '"Archive"'),
                         ('STORE', '1', '+FLAGS.SILENT', r'(\Deleted)'), ('EXPUNGE', '1'),
                         ('SEARCH', None, 'UID', '1')])
        self.connection.assert_called_once()

    def test_native_source_name_is_not_redirected_through_a_logical_alias(self) -> None:
        inbox.SETTINGS['test']['mailbox']['alias']['archive'] = 'Other physical folder'
        inbox.transfer_message(dict(message(), mailbox='Archive'), 'Trash')
        self.client.select.assert_called_once_with('"Archive"')

    def test_missing_uidplus_refuses_fallback_or_deletion_before_any_mutation(self) -> None:
        self.client.capabilities = b'IMAP4rev1'
        for destination, mailbox in [('Archive', 'INBOX'), (None, 'Trash')]:
            with self.subTest(destination=destination), self.assertRaisesRegex(inbox.MailActionError, 'lacks UIDPLUS'):
                inbox.transfer_message(dict(message(), mailbox=mailbox), destination)
        self.client.uid.assert_not_called()

    def test_epoch_change_or_message_id_mismatch_cannot_move_another_message(self) -> None:
        for epoch, mid in [(11, None), (10, 'different')]:
            self.client.epoch, self.client.mid = epoch, mid
            self.client.uid.reset_mock()
            with self.assertRaisesRegex(inbox.MailActionError, 'identity changed'):
                inbox.transfer_message(message(), 'Trash')
            self.assertFalse(any(command[0] in ('MOVE', 'COPY', 'STORE', 'EXPUNGE') for command in self.commands()))

    def test_without_cached_epoch_fresh_message_id_is_required(self) -> None:
        item = dict(message(), uidvalidity=None)
        self.client.mid = 'wrong'
        with self.assertRaisesRegex(inbox.MailActionError, 'identity changed'):
            inbox.transfer_message(item, 'Trash')
        self.client.mid = 'message-1'
        inbox.transfer_message(item, 'Trash')
        self.assertEqual(self.commands()[-1], ('MOVE', '1', '"Trash"'))

    def test_invalid_or_missing_ids_do_not_even_connect(self) -> None:
        for uid in ('0', '*', '1:99', '--all', '1\r\nEXPUNGE', '01'):
            with self.subTest(uid=uid), self.assertRaises(inbox.MailActionError):
                inbox.transfer_message(message(uid), 'Trash')
        with self.assertRaises(inbox.MailActionError):
            inbox.transfer_message(dict(message(), uidvalidity=None, **{'message-id': None}), 'Trash')
        self.connection.assert_not_called()

    def test_disappeared_message_is_not_reported_as_moved(self) -> None:
        self.client.missing = True
        with self.assertRaisesRegex(inbox.MailActionError, 'no longer'):
            inbox.transfer_message(message(), 'Trash')
        self.assertEqual(len(self.commands()), 1)

    def test_failed_copy_never_flags_or_deletes_the_source(self) -> None:
        self.client.capabilities = b'IMAP4rev1 UIDPLUS'
        self.client.fail['COPY'] = True
        with self.assertRaisesRegex(inbox.MailActionError, 'source was not removed'):
            inbox.transfer_message(message(), 'Trash')
        self.assertEqual([command[0] for command in self.commands()], ['FETCH', 'COPY'])

    def test_missing_or_wrong_copyuid_never_removes_source(self) -> None:
        self.client.capabilities = b'IMAP4rev1 UIDPLUS'
        for available, source in [(False, None), (True, '2')]:
            self.client.copyuid, self.client.copy_source = available, source
            self.client.uid.reset_mock()
            with self.assertRaisesRegex(inbox.MailActionError, 'source was not removed'):
                inbox.transfer_message(message(), 'Trash')
            self.assertEqual([command[0] for command in self.commands()], ['FETCH', 'COPY'])

    def test_interrupted_mutations_are_not_retried_and_diagnostics_stay_private(self) -> None:
        for command in ('MOVE', 'COPY', 'STORE', 'EXPUNGE'):
            self.client = FakeIMAP(b'IMAP4rev1 UIDPLUS MOVE' if command == 'MOVE' else b'IMAP4rev1 UIDPLUS')
            self.client.fail[command] = OSError('PRIVATE_TOKEN')
            with self.assertRaises(inbox.MailActionError) as error:
                inbox.transfer_message(message(), 'Trash')
            self.assertNotIn('PRIVATE_TOKEN', str(error.exception))
            self.assertEqual(sum(call[0] == command for call in self.commands()), 1)
            self.assertEqual(self.commands()[-1][0], command)

    def test_permanent_delete_is_only_one_uid_and_only_from_trash(self) -> None:
        with self.assertRaisesRegex(inbox.MailActionError, 'only allowed'):
            inbox.transfer_message(message(), None)
        self.connection.assert_not_called()
        inbox.transfer_message(dict(message(), mailbox='Trash'), None)
        self.assertEqual(self.commands()[1:], [('STORE', '1', '+FLAGS.SILENT', r'(\Deleted)'),
                                              ('EXPUNGE', '1'), ('SEARCH', None, 'UID', '1')])

    def test_expunge_must_actually_remove_target_before_success(self) -> None:
        self.client.remaining = b'1'
        with self.assertRaisesRegex(inbox.MailActionError, 'not confirmed'):
            inbox.transfer_message(dict(message(), mailbox='Trash'), None)


class ConnectionTests(unittest.TestCase):
    def test_auth_helpers_are_captured_tls_verified_and_connection_closed(self) -> None:
        for mechanism, credential in [('plain', 'password'), ('xoauth2', 'token')]:
            config = {'server': 'imaps://mail.example.org:993', 'sasl': {
                mechanism: {'username': 'synthetic@example.org', credential: {'command': ['synthetic-helper']}}}}
            client = Mock()
            def authenticate(name, callback):
                value = callback(b'')
                self.assertIn(b'SYNTHETIC_SECRET', value)
                if name == 'XOAUTH2':
                    self.assertEqual(callback(b'failure challenge'), b'')
                return 'OK', [b'']
            client.authenticate.side_effect = authenticate
            with self.subTest(mechanism=mechanism), patch.object(inbox, 'SETTINGS', {'test': {'imap': config}}), \
                    patch.object(inbox, 'run_command_private', return_value=b'SYNTHETIC_SECRET\n') as helper, \
                    patch.object(inbox.imaplib, 'IMAP4_SSL', return_value=client) as connect:
                with inbox.action_connection('test') as opened:
                    self.assertIs(opened, client)
                helper.assert_called_once_with(['synthetic-helper'])
                context = connect.call_args.kwargs['ssl_context']
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, inbox.ssl.CERT_REQUIRED)
                self.assertEqual(connect.call_args.kwargs['timeout'], 15)
                client.shutdown.assert_called_once()
                client.close.assert_not_called()
                client.expunge.assert_not_called()

    def test_insecure_or_unsupported_configuration_never_runs_secret_helper(self) -> None:
        for server in ('imap://mail.example.org', 'imaps://user:pass@mail.example.org', 'imaps://mail.example.org?verify=false'):
            with patch.object(inbox, 'SETTINGS', {'test': {'imap': {'server': server}}}), \
                    patch.object(inbox, 'run_command_private') as helper, \
                    self.assertRaises(inbox.MailActionError):
                with inbox.action_connection('test'):
                    self.fail('Unexpected connection')
            helper.assert_not_called()

    def test_auth_failure_never_exposes_helper_or_server_output(self) -> None:
        config = {'server': 'imaps://mail.example.org', 'sasl': {'plain': {
            'username': 'synthetic@example.org', 'password': {'command': ['synthetic-helper']}}}}
        client = Mock()
        client.authenticate.side_effect = inbox.imaplib.IMAP4.error('SYNTHETIC_PRIVATE_SECRET')
        with patch.object(inbox, 'SETTINGS', {'test': {'imap': config}}), \
                patch.object(inbox, 'run_command_private', return_value=b'SYNTHETIC_PRIVATE_SECRET'), \
                patch.object(inbox.imaplib, 'IMAP4_SSL', return_value=client), \
                self.assertRaises(inbox.MailActionError) as error:
            with inbox.action_connection('test'):
                self.fail('Unexpected authentication success')
        self.assertNotIn('SYNTHETIC_PRIVATE_SECRET', str(error.exception))
        client.shutdown.assert_called_once()


if __name__ == '__main__':
    unittest.main()
