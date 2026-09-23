"""Out-of-office checks: synthetic servers only; never enable a live reply."""

from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from http.cookiejar import Cookie
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request
from zoneinfo import ZoneInfo

from test_inbox import FakeScreen, inbox


class FakeSieve(inbox.SieveVacation):
    def __init__(self):
        super().__init__('work', {'host': 'mail.example.org'})
        self.files = {'original': b'require "fileinto";\r\n'}
        self.active = 'original'
        self.commands = []
        self.fail_upload = False
        self.concurrent_edit = False

    def command(self, command):
        self.commands.append(command)
        if command == b'LISTSCRIPTS':
            return [[name.encode(), *([b'ACTIVE'] if name == self.active else [])] for name in self.files]
        match = re.fullmatch(rb'GETSCRIPT "([^"]+)"', command)
        if match:
            return [[self.files[match[1].decode()]]]
        match = re.fullmatch(rb'PUTSCRIPT "([^"]+)" \{([0-9]+)\+\}\r\n(.*)', command, re.S)
        if match:
            if self.fail_upload:
                raise inbox.VacationError('Rejected')
            assert len(match[3]) == int(match[2])
            assert match[1].decode() != self.active, 'Never replace the active script during upload'
            self.files[match[1].decode()] = match[3]
            if self.concurrent_edit:
                self.files['original'] += b'# concurrent change\r\n'
            return []
        match = re.fullmatch(rb'SETACTIVE "([^"]*)"', command)
        if match:
            self.active = match[1].decode()
            return []
        raise AssertionError('Unexpected command')


class VacationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = {'backend': 'sieve', 'host': 'mail.example.org', 'port': 4190}
        self.settings = {'work': {'email': 'worker@example.org', 'imap': {
            'server': 'imaps://mail.example.org', 'sasl': {'plain': {
                'username': 'worker', 'password': {'command': ['synthetic-helper']}}}}}}
        for name, value in [('SETTINGS', self.settings), ('CONFIG_ARGS', ['--config', '/synthetic/config.toml'])]:
            override = patch.object(inbox, name, value)
            override.start()
            self.addCleanup(override.stop)
        override = patch.dict(inbox.os.environ, {'XDG_STATE_HOME': str(self.root / 'state'),
                                                'XDG_CONFIG_HOME': str(self.root / 'config')})
        override.start()
        self.addCleanup(override.stop)
        for name in ('run_command_private', 'vacation_connection'):
            override = patch.object(inbox, name, side_effect=AssertionError('Real credentials/network forbidden'))
            override.start()
            self.addCleanup(override.stop)

    def defaults(self):
        return {'SOGoTimeZone': 'Europe/Berlin', 'SOGoRememberLastModule': True, 'SOGoLoginModule': 'Mail',
                'SOGoSieveFilters': [{'name': 'existing', 'active': True, 'actions': [{'method': 'fileinto', 'argument': 'Filed'}]}],
                'Forward': {'enabled': 1, 'forwardAddress': ['forward@example.org']},
                'SOGoMailLabelsColors': {'$label': ['Label', '#123456']},
                'Vacation': {'enabled': 0, 'autoReplyText': 'Saved old text'}, 'locale': {'months': ['synthetic']}}

    def test_protocol_literals_can_contain_fake_statuses_and_quoted_names(self):
        content = b'OK\r\nNO "private diagnostic"\r\n'
        wire = b'"a\\"b" ACTIVE\r\n{' + str(len(content)).encode() + b'}\r\n' + content + b'\r\nOK\r\n'
        self.assertEqual(inbox.sieve_reply(BytesIO(wire)), [[b'a"b', b'ACTIVE'], [content]])

    def test_protocol_quoting_and_response_errors_are_private(self):
        self.assertEqual(inbox.sieve_quote('a\\b"c'), b'"a\\\\b\\"c"')
        for value in ('evil\r\nSETACTIVE', 'zero\0byte'):
            with self.assertRaises(inbox.VacationError):
                inbox.sieve_quote(value)
        for response in (b'NO "SYNTHETIC_SECRET"\r\n', b'{999999999}\r\n', b'{8}\r\nshort', b'"unterminated\r\n'):
            with self.assertRaises(inbox.VacationError) as error:
                inbox.sieve_reply(BytesIO(response))
            self.assertNotIn('SYNTHETIC_SECRET', str(error.exception))

    def test_script_escapes_text_and_preserves_filter_order(self):
        script = inbox.vacation_script('original', 'worker@example.org', 'Hello\n.\n..two\n"; discard; #')
        self.assertIn(b'\r\n..\r\n...two\r\n', script)
        self.assertLess(script.index(b'include :personal'), script.index(b'  vacation :days'))
        self.assertEqual(inbox.vacation_metadata(script)['text'], 'Hello\n.\n..two\n"; discard; #')
        for modified in (script + b'stop;', b'# unrelated script\r\n'):
            with self.assertRaises(inbox.VacationError):
                inbox.vacation_metadata(modified)

    def test_sieve_read_default_off_makes_no_writes(self):
        server = FakeSieve()
        self.assertEqual(server.read()['label'], 'OFF')
        self.assertTrue(all(command.startswith((b'GETSCRIPT', b'LISTSCRIPTS')) for command in server.commands))

    def test_sieve_on_update_off_preserves_original_byte_for_byte(self):
        server = FakeSieve()
        original = server.files['original']
        for text in ('First reply', 'Second reply', 'Third reply'):
            server.write(server.read(), True, text, '', '')
            self.assertEqual(server.read()['label'], 'ON')
            self.assertEqual(server.read()['text'], text)
            self.assertEqual(server.files['original'], original)
        server.write(server.read(), False, '', '', '')
        self.assertEqual(server.active, 'original')
        self.assertEqual(server.read()['label'], 'OFF')
        self.assertEqual(server.files['original'], original)
        self.assertEqual(len(server.files), 3)

    def test_sieve_no_previous_script_restores_normal_delivery(self):
        server = FakeSieve()
        server.files, server.active = {}, ''
        server.write(server.read(), True, 'Away', '', '')
        server.write(server.read(), False, '', '', '')
        self.assertEqual(server.active, '')
        self.assertEqual(server.read()['label'], 'OFF')

    def test_sieve_refuses_conflicts_dates_collisions_and_failed_uploads(self):
        for action in ('vacation', 'include', 'reject', 'ereject'):
            server = FakeSieve()
            server.files['original'] = action.encode() + b' "existing";'
            with self.assertRaises(inbox.VacationError):
                server.read()
        for kind in ('date', 'collision', 'upload', 'concurrent'):
            server = FakeSieve()
            if kind == 'collision':
                server.files[inbox.VACATION_SCRIPTS[0]] = b'private existing filter'
            server.fail_upload = kind == 'upload'
            server.concurrent_edit = kind == 'concurrent'
            with self.assertRaises(inbox.VacationError):
                server.write(server.read(), True, 'Away', '2030-01-01' if kind == 'date' else '', '')
            self.assertEqual(server.active, 'original')
            self.assertFalse(any(command.startswith(b'SETACTIVE') for command in server.commands))

    def test_sogo_disable_preserves_message_dates_filters_and_input(self):
        defaults = self.defaults()
        defaults['Vacation'].update(enabled=1, startDateEnabled=1, startDate=2000000000)
        before = deepcopy(defaults)
        payload = inbox.sogo_vacation_payload(defaults, 'work', False, '', '', '')
        self.assertEqual(defaults, before)
        for key in ('SOGoSieveFilters', 'Forward', 'SOGoMailLabelsColors', 'SOGoTimeZone'):
            self.assertEqual(payload['defaults'][key], defaults[key])
        self.assertNotIn('settings', payload)
        self.assertNotIn('locale', payload['defaults'])
        self.assertEqual(payload['defaults']['SOGoLoginModule'], 'Last')
        self.assertEqual(payload['defaults']['Vacation'], dict(defaults['Vacation'], enabled=0))

    def test_sogo_enable_never_discards_mail_or_enables_reply_to_lists(self):
        defaults = self.defaults()
        defaults['Vacation'].update(discardMails=1, alwaysSend=1, startTimeEnabled=1, weekdaysEnabled=1)
        payload = inbox.sogo_vacation_payload(defaults, 'work', True, 'Away', '', '')['defaults']['Vacation']
        self.assertEqual(payload['enabled'], 1)
        for key in ('discardMails', 'alwaysSend', 'startTimeEnabled', 'endTimeEnabled', 'weekdaysEnabled'):
            self.assertEqual(payload[key], 0)
        self.assertEqual(payload['ignoreLists'], 1)
        self.assertEqual(payload['daysBetweenResponse'], 7)
        self.assertEqual(payload['autoReplyEmailAddresses'], ['worker@example.org'])
        self.assertNotIn('endDate', payload)

    def test_dates_inclusive_expiry_scheduling_and_dst(self):
        tz = ZoneInfo('Europe/Berlin')
        now = datetime(2026, 1, 1, tzinfo=tz)
        start, end = inbox.vacation_dates('2026-03-29', '2026-03-30', 'Europe/Berlin', now)
        self.assertEqual(end - start, 23 * 3600)
        defaults = self.defaults()
        defaults['Vacation'].update(enabled=1, startDateEnabled=1, startDate=start, endDateEnabled=1, endDate=end)
        for date, label in ((datetime(2026, 3, 28, tzinfo=tz), 'SCHEDULED'),
                            (datetime(2026, 3, 30, 23, 59, tzinfo=tz), 'ON'),
                            (datetime(2026, 3, 31, tzinfo=tz), 'OFF (expired)')):
            self.assertEqual(inbox.sogo_vacation_label(defaults, date), label)
        defaults['Vacation']['enabled'] = '0'
        self.assertEqual(inbox.sogo_vacation_label(defaults, now), 'OFF')
        for dates in [('2025-01-01', ''), ('2026-02-30', ''), ('2026-2-3', ''), ('2026-05-02', '2026-05-01')]:
            with self.assertRaises(inbox.VacationError):
                inbox.vacation_dates(*dates, 'Europe/Berlin', now)

    def test_sogo_page_parser_is_data_only(self):
        page = inbox.VacationPage()
        page.feed('<script>evil()</script><script id="UserDefaults">{"Vacation":{"enabled":0}}</script>'
                  '<md-datepicker ng-model="app.preferences.defaults.Vacation.startDate"></md-datepicker>'
                  '<md-datepicker ng-model="app.preferences.defaults.Vacation.endDate"></md-datepicker>')
        self.assertEqual(json.loads(page.defaults), {'Vacation': {'enabled': 0}})
        self.assertEqual(page.date_controls, {'startDate', 'endDate'})
        template = inbox.VacationPage()
        template.feed('<script type="text/ng-template"><md-datepicker ng-model="app.preferences.defaults.Vacation.startDate"></md-datepicker>'
                      '<md-datepicker ng-model="app.preferences.defaults.Vacation.endDate"></md-datepicker></script>')
        self.assertEqual(template.date_controls, {'startDate', 'endDate'})
        self.assertEqual(template.defaults, '')

    def test_sogo_dates_require_both_provider_controls_and_server_date_capability(self):
        server = inbox.SOGoVacation('work', {'url': 'https://mail.example.org/SOGo/'})
        server.base = server.root + 'so/worker/'
        controls = '<script type="text/ng-template">' + ''.join(
            f'<md-datepicker ng-model="app.preferences.defaults.Vacation.{field}"></md-datepicker>'
            for field in ('startDate', 'endDate')) + '</script>'
        for capability, widgets, available in ((True, True, True), (False, True, False), (True, False, False)):
            html = ('<script id="UserDefaults">' + json.dumps(self.defaults()) + '</script>'
                    + '<script>var sieveCapabilities = ' + json.dumps(['date'] if capability else []) + ';</script>'
                    + (controls if widgets else '')).encode()
            server.request = Mock(side_effect=[(200, html), HTTPError('https://mail.example.org', 404, 'No external script', {}, None)])
            self.assertIs(server.read()['dates'], available)

    def test_sogo_external_script_or_failed_check_never_reports_off(self):
        server = inbox.SOGoVacation('work', {'url': 'https://mail.example.org/SOGo/'})
        server.base = server.root + 'so/worker/'
        html = ('<script id="UserDefaults">' + json.dumps(self.defaults()) + '</script>').encode()
        for check in ((204, b''), HTTPError('https://mail.example.org', 500, 'SYNTHETIC_SECRET', {}, None)):
            server.request = Mock(side_effect=[(200, html), check])
            with self.assertRaises(inbox.VacationError) as error:
                server.read()
            self.assertNotIn('SYNTHETIC_SECRET', str(error.exception))
        server.request = Mock(side_effect=[(200, html), HTTPError('https://mail.example.org', 404, 'No external script', {}, None)])
        self.assertEqual(server.read()['label'], 'OFF')

    def test_sogo_request_adds_private_csrf_header_without_cross_origin_redirect(self):
        server = inbox.SOGoVacation('work', {'url': 'https://mail.example.org/SOGo/'})
        server.cookies.set_cookie(Cookie(0, 'XSRF-TOKEN', 'synthetic%2Btoken', None, False,
                                        'mail.example.org', False, False, '/SOGo/', True, True, None, True, None, None, {}))
        response = Mock(status=200)
        response.read.return_value = b'{}'
        server.opener = Mock()
        server.opener.open.return_value.__enter__ = Mock(return_value=response)
        server.opener.open.return_value.__exit__ = Mock(return_value=False)
        server.request(server.root + 'Preferences')
        self.assertEqual(server.opener.open.call_args.args[0].get_header('X-xsrf-token'), 'synthetic+token')
        redirects = inbox.VacationRedirect()
        for request, target in ((Request(server.root), 'https://other.example.org/'),
                                (Request(server.root, data=b'private'), server.root + 'next')):
            with self.assertRaises(inbox.VacationError):
                redirects.redirect_request(request, None, 302, 'Found', {}, target)

    def test_sieve_credentials_are_sent_only_after_verified_tls(self):
        plain = Mock()
        plain.makefile.return_value = BytesIO(b'"STARTTLS"\r\nOK\r\nOK\r\n')
        secure = Mock()
        secure.makefile.return_value = BytesIO(b'"SASL" "PLAIN"\r\nOK\r\nOK\r\n"SIEVE" "vacation include"\r\nOK\r\n')
        context = Mock()
        context.wrap_socket.return_value = secure
        with patch.object(inbox.socket, 'create_connection', return_value=plain), \
                patch.object(inbox.ssl, 'create_default_context', return_value=context) as tls, \
                patch.object(inbox, 'run_command_private', return_value=b'SYNTHETIC_SECRET\n') as helper:
            server = inbox.SieveVacation('work', self.profile)
            server.connect()
            server.close()
        tls.assert_called_once_with()
        context.wrap_socket.assert_called_once_with(plain, server_hostname='mail.example.org')
        plain.sendall.assert_called_once_with(b'STARTTLS\r\n')
        helper.assert_called_once_with(['synthetic-helper'])
        self.assertTrue(secure.sendall.call_args_list[0].args[0].startswith(b'AUTHENTICATE "PLAIN"'))
        secure.close.assert_called_once()

    def test_sogo_write_verifies_after_save_without_posting_ui_settings(self):
        server = inbox.SOGoVacation('work', {'url': 'https://mail.example.org/SOGo/'})
        server.base = server.root + 'so/worker/'
        defaults = self.defaults()
        state = {'defaults': defaults, 'dates': True}
        expected = inbox.sogo_vacation_payload(defaults, 'work', True, 'Away', '', '')['defaults']
        server.read = Mock(return_value={'defaults': expected})
        server.request = Mock(return_value=(200, b''))
        server.write(state, True, 'Away', '', '')
        server.request.assert_called_once_with(server.base + 'Preferences/save', {'defaults': expected})
        self.assertNotIn('settings', server.request.call_args.args[1])
        self.assertEqual(defaults['Vacation']['enabled'], 0)
        for changed_key in ('Vacation', 'SOGoSieveFilters', 'Forward', 'Notification'):
            changed = deepcopy(expected)
            changed[changed_key] = {}
            server.read.return_value = {'defaults': changed}
            with self.assertRaises(inbox.VacationError):
                server.write(state, True, 'Away', '', '')

    def test_sogo_external_site_request_cannot_receive_csrf_or_credentials(self):
        server = inbox.SOGoVacation('work', {'url': 'https://mail.example.org/SOGo/'})
        server.opener = Mock()
        with self.assertRaises(inbox.VacationError):
            server.request('https://other.example.org/', {'password': 'synthetic'})
        server.opener.open.assert_not_called()

    def test_successful_editor_save_is_only_local_and_uses_isolated_neovim(self):
        path = inbox.vacation_draft_path('work', self.profile)
        def edit(command, **kwargs):
            self.assertIn('nvim', command[0])
            self.assertIn('NONE', command)
            self.assertIn('set nomodeline noswapfile nobackup nowritebackup noundofile', command)
            Path(command[-1]).write_text('A saved reply')
            return Mock(returncode=0)
        with patch.object(inbox.shutil, 'which', return_value='/synthetic/nvim'), \
                patch.object(inbox, 'terminal_child', return_value=nullcontext()), \
                patch.object(inbox.subprocess, 'run', side_effect=edit), \
                patch.object(inbox, 'text_view', return_value='s'), \
                patch.object(inbox, 'vacation_change') as change:
            inbox.vacation_edit(FakeScreen([]), path)
        self.assertEqual(path.read_text(), 'A saved reply')
        change.assert_not_called()

    def test_unknown_status_does_not_allow_a_write_when_read_fails(self):
        with patch.object(inbox, 'choose', side_effect=[0, 1, 4]), \
                patch.object(inbox, 'vacation_read', side_effect=inbox.VacationError('Unavailable')), \
                patch.object(inbox, 'text_view', return_value='q'), patch.object(inbox, 'vacation_change') as change:
            inbox.vacation_account(FakeScreen([]), 'work', self.profile, None)
        change.assert_not_called()

    def test_no_configuration_never_opts_accounts_in(self):
        self.assertEqual(inbox.vacation_profiles(), {})
        with patch.object(inbox, 'text_view', return_value='q'), patch.object(inbox, 'vacation_change') as change:
            inbox.vacation_view(FakeScreen([]))
        change.assert_not_called()

    def test_profile_hosts_are_checked_before_credentials(self):
        directory = self.root / 'config/himalaya-inbox'
        directory.mkdir(parents=True)
        path = directory / 'vacation.toml'
        path.write_text('[accounts.work]\nbackend="sieve"\nhost="mail.example.org"\n')
        self.assertEqual(list(inbox.vacation_profiles()), ['work'])
        path.write_text('[accounts.work]\nbackend="sogo"\nurl="https://unrelated.example.org/SOGo/"\n')
        with self.assertRaises(inbox.VacationError):
            inbox.vacation_profiles()

    def test_local_message_storage_has_no_enabled_flag_and_is_private(self):
        path = inbox.vacation_draft_path('work', self.profile)
        inbox.save_draft(path, 'Away message')
        self.assertEqual(path.read_text(), 'Away message')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_changed_state_aborts_before_write_and_uncertain_write_never_retries(self):
        server = Mock()
        server.read.return_value = {'stamp': 'current'}
        with patch.object(inbox, 'vacation_connection', return_value=nullcontext(server)):
            with self.assertRaisesRegex(inbox.VacationError, 'Settings changed'):
                inbox.vacation_change('work', self.profile, {'stamp': 'old'}, True, 'Away')
            server.write.assert_not_called()
            server.write.side_effect = OSError('SYNTHETIC_SECRET')
            with self.assertRaisesRegex(inbox.VacationError, 'Change not confirmed') as error:
                inbox.vacation_change('work', self.profile, {'stamp': 'current'}, True, 'Away')
            server.write.assert_called_once()
            self.assertNotIn('SYNTHETIC_SECRET', str(error.exception))

    def test_account_initial_selection_and_already_off_never_enable(self):
        state = {'label': 'OFF', 'stamp': 'original', 'text': '', 'dates': False}
        with patch.object(inbox, 'choose', side_effect=[0, 4]) as choose, \
                patch.object(inbox, 'vacation_read', return_value=state), \
                patch.object(inbox, 'vacation_change') as change:
            inbox.vacation_account(FakeScreen([]), 'work', self.profile, state)
        self.assertTrue(choose.call_args_list[0].args[2][0].startswith('Turn OFF'))
        change.assert_not_called()

    def test_edit_only_and_cancelled_activation_never_write_server(self):
        state = {'label': 'OFF', 'stamp': 'original', 'text': '', 'dates': False}
        path = inbox.vacation_draft_path('work', self.profile)
        inbox.save_draft(path, 'Away')
        for action, confirm in ((2, None), (1, 'q'), (1, 'y')):
            with patch.object(inbox, 'choose', side_effect=[action, 4]), \
                    patch.object(inbox, 'vacation_read', return_value=state), \
                    patch.object(inbox, 'vacation_edit'), patch.object(inbox, 'vacation_change') as change, \
                    patch.object(inbox, 'text_view', return_value=confirm), \
                    patch.object(inbox, 'input_line', return_value='wrong confirmation'):
                inbox.vacation_account(FakeScreen([]), 'work', self.profile, state)
            change.assert_not_called()

    def test_explicit_on_and_off_are_separate_confirmed_actions(self):
        state = {'label': 'OFF', 'stamp': 'original', 'text': '', 'dates': False}
        path = inbox.vacation_draft_path('work', self.profile)
        inbox.save_draft(path, 'Away')
        for enabled, choices in ((True, [1, 4]), (False, [0, 4])):
            current = state if enabled else dict(state, label='ON')
            with patch.object(inbox, 'choose', side_effect=choices), \
                    patch.object(inbox, 'vacation_read', return_value=current), \
                    patch.object(inbox, 'vacation_change', return_value=state) as change, \
                    patch.object(inbox, 'text_view', return_value='y'), \
                    patch.object(inbox, 'input_line', return_value='TURN ON'):
                inbox.vacation_account(FakeScreen([]), 'work', self.profile, current)
            change.assert_called_once()
            self.assertIs(change.call_args.args[3], enabled)
            self.assertEqual(path.read_text(), 'Away')

    def test_turning_off_retains_a_server_message_even_without_a_local_draft(self):
        state = {'label': 'ON', 'stamp': 'original', 'text': 'Previously enabled reply', 'dates': False}
        path = inbox.vacation_draft_path('work', self.profile)
        self.assertFalse(path.exists())
        with patch.object(inbox, 'choose', side_effect=[0, 4]), \
                patch.object(inbox, 'vacation_read', return_value=state), \
                patch.object(inbox, 'vacation_change', return_value=dict(state, label='OFF')) as change, \
                patch.object(inbox, 'text_view', return_value='y'):
            inbox.vacation_account(FakeScreen([]), 'work', self.profile, state)
        change.assert_called_once_with('work', self.profile, state, False)
        self.assertEqual(path.read_text(), 'Previously enabled reply')

    def test_direct_menu_never_loads_mail_or_changes_server(self):
        with patch.object(inbox.sys, 'argv', ['inbox', '--vacation']), \
                patch.object(inbox.sys.stdin, 'isatty', return_value=True), \
                patch.object(inbox.sys.stdout, 'isatty', return_value=True), \
                patch.object(inbox, 'read_settings'), patch.object(inbox, 'load_all') as mail, \
                patch.object(inbox.curses, 'wrapper') as wrapper, patch.object(inbox, 'stop_background'):
            inbox.main()
        wrapper.assert_called_once_with(inbox.vacation_view)
        mail.assert_not_called()
