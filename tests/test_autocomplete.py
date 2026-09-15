"""Synthetic contacts and real Neovim popup tests; no accounts or transport."""

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

from test_inbox import inbox, row


CONTACTS = [
    {'email': 'alice.jones@example.net', 'name': 'Alice Jones'},
    {'email': 'alice.smith@example.org', 'name': 'Alice Smith'},
    {'email': 'bob@example.net', 'name': 'Robert Brown'},
    {'email': 'elodie@example.org', 'name': 'Élodie Test'},
]


# A test-only observer avoids changing editor modes or using a network/socket.
OBSERVER = r'''
local tick = 0
local timer = vim.uv.new_timer()
timer:start(0, 20, vim.schedule_wrap(function()
  tick = tick + 1
  local state = {tick = tick, line = vim.fn.getline('.'), lines = vim.fn.getline(1, '$'),
                 info = vim.fn.complete_info(), error = vim.v.errmsg}
  local path = vim.env.HIMALAYA_TEST_STATE
  vim.fn.writefile({vim.json.encode(state)}, path .. '.tmp')
  vim.uv.fs_rename(path .. '.tmp', path)
end))
'''


class ContactTests(unittest.TestCase):
    def test_collects_senders_and_recipients_not_bcc_or_bodies(self) -> None:
        rows = [{'from': CONTACTS[:1], 'to': CONTACTS[1:2], 'bcc': CONTACTS[2:],
                 'body': 'hidden@example.org', 'date': '2026-01-01T00:00:00Z'}]
        self.assertEqual(inbox.collect_contacts(rows), CONTACTS[:2])

    def test_deduplication_uses_latest_name_and_preserves_local_part_case(self) -> None:
        rows = [
            {'date': '2026-01-01T00:00:00Z', 'from': [{'email': 'me@EXAMPLE.org', 'name': 'Old'}]},
            {'date': '2026-02-01T00:00:00Z', 'to': [{'email': 'me@example.org', 'name': 'New'}]},
            {'from': [{'email': 'Me@example.org', 'name': 'Different mailbox'}]},
        ]
        self.assertEqual(inbox.collect_contacts(rows), [
            {'email': 'Me@example.org', 'name': 'Different mailbox'},
            {'email': 'me@example.org', 'name': 'New'},
        ])

    def test_invalid_addresses_are_skipped_and_names_are_plain_text(self) -> None:
        invalid = ['', 'missing-domain', 'a@', '@example.org', 'a@b\nCc: injected@example.org',
                   'two@example.org, other@example.org', '<a@example.org>', 'a@[', None]
        rows = [{'from': [{'email': email, 'name': 'bad'} for email in invalid],
                 'to': [{'email': 'good@example.org', 'name': 'Name\x1b\nwith\tcontrols'}]}]
        self.assertEqual(inbox.collect_contacts(rows), [
            {'email': 'good@example.org', 'name': 'Name with controls'},
        ])

    def test_loading_and_refresh_replace_the_contact_catalog(self) -> None:
        item = dict(row('test', '1', 'one'), to=CONTACTS[:1])
        with patch.object(inbox, 'SETTINGS', {'test': {}}), \
                patch.object(inbox, 'CONTACTS', CONTACTS[1:]), \
                patch.object(inbox, 'load_account', return_value=[item]):
            self.assertEqual(inbox.load_all(), [item])
            self.assertEqual(inbox.CONTACTS, CONTACTS[:1])

    def test_editor_data_is_private_temporary_json_not_executable(self) -> None:
        contacts = [{'email': 'test@example.org', 'name': '\"]; error("NOT_CODE"); --'}]
        with patch.object(inbox, 'CONTACTS', contacts):
            with inbox.editor_environment() as environment:
                path = Path(environment['HIMALAYA_INBOX_CONTACTS'])
                init = Path(environment['HIMALAYA_INBOX_INIT'])
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(init.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text()), contacts)
                self.assertNotIn('NOT_CODE', init.read_text())
                self.assertEqual(environment['NVIM_LOG_FILE'], '/dev/null')
            self.assertFalse(path.parent.exists())


@unittest.skipUnless(shutil.which('nvim'), 'Neovim is not installed')
class AutocompleteIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.draft = self.folder / 'draft.txt'
        self.draft.write_text('X-Himalaya-Account: test\nTo: \nCc: \nSubject: Test\n\nBody\nTo: quoted\n')
        self.snapshot = self.folder / 'state.json'
        observer = self.folder / 'observer.lua'
        observer.write_text(OBSERVER)
        self.after_tick = 0
        self.contacts = patch.object(inbox, 'CONTACTS', CONTACTS)
        self.contacts.start()
        self.addCleanup(self.contacts.stop)
        self.environment = inbox.editor_environment()
        environment = self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)
        environment['TERM'] = 'xterm-256color'
        environment['HIMALAYA_TEST_STATE'] = str(self.snapshot)
        environment['HIMALAYA_TEST_OBSERVER'] = str(observer)
        self.env = environment
        self.nvim = shutil.which('nvim')
        self.master, slave = pty.openpty()
        self.output = bytearray()
        self.addCleanup(os.close, self.master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 28, 110, 0, 0))
        self.process = subprocess.Popen([
            self.nvim, '-u', 'NONE', '-n', '-i', 'NONE',
            '--cmd', 'set nomodeline noswapfile nobackup nowritebackup noundofile',
            '--cmd', 'lua dofile(vim.env.HIMALAYA_INBOX_INIT)',
            '--cmd', 'lua dofile(vim.env.HIMALAYA_TEST_OBSERVER)',
            '+set filetype=mail', '--', str(self.draft),
        ], stdin=slave, stdout=slave, stderr=slave, env=environment, start_new_session=True)
        os.close(slave)
        self.addCleanup(self.stop_editor)
        self.wait_for(lambda state: state['line'].startswith('X-Himalaya'))

    def stop_editor(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def state(self) -> dict:
        """Read snapshots from our private synthetic editor, not user sessions."""
        while select.select([self.master], [], [], 0)[0]:
            try:
                self.output.extend(os.read(self.master, 65536))
            except OSError:
                self.fail(f'Synthetic editor exited: {self.output!r}')
        return json.loads(self.snapshot.read_text()) if self.snapshot.exists() else {}

    def wait_for(self, predicate) -> dict:
        deadline = time.monotonic() + 8
        latest = {}
        while time.monotonic() < deadline:
            latest = self.state()
            if latest and latest['tick'] > self.after_tick and predicate(latest):
                self.assertEqual(latest['error'], '')
                return latest
            select.select([self.master], [], [], 0.04)
        self.fail(f'Synthetic editor state did not match: {latest}')

    def type(self, keys: str) -> None:
        self.after_tick = self.state().get('tick', 0)
        os.write(self.master, keys.encode())

    def test_name_search_refinement_selection_and_acceptance(self) -> None:
        self.type('2GAa')
        state = self.wait_for(lambda s: s['line'] == 'To: a')
        self.assertFalse(state['info']['pum_visible'])
        self.type('l')
        state = self.wait_for(lambda s: s['info']['pum_visible'])
        self.assertEqual(state['line'], 'To: al')
        self.assertEqual(state['info']['selected'], -1)
        self.assertEqual([item['word'] for item in state['info']['items']],
                         ['alice.jones@example.net', 'alice.smith@example.org'])
        self.type('ice s')
        state = self.wait_for(lambda s: len(s['info']['items']) == 1 and s['line'] == 'To: alice s')
        self.assertEqual(state['info']['items'][0]['word'], 'alice.smith@example.org')
        self.type('\t')
        self.wait_for(lambda s: s['info']['selected'] == 0)
        self.type('\r')
        state = self.wait_for(lambda s: not s['info']['pum_visible'] and s['line'] == 'To: alice.smith@example.org')
        self.assertEqual(len(state['lines']), 7)

    def test_cc_multiple_recipients_and_name_not_email_prefix(self) -> None:
        self.type('3GAexisting@example.org, Ro')
        state = self.wait_for(lambda s: s['info']['pum_visible'])
        self.assertEqual(state['info']['items'][0]['word'], 'bob@example.net')
        self.type('\t\r')
        self.wait_for(lambda s: s['line'] == 'Cc: existing@example.org, bob@example.net' and not s['info']['pum_visible'])

    def test_cancel_and_backspace_never_choose_an_address(self) -> None:
        self.type('2GAali')
        self.wait_for(lambda s: s['info']['pum_visible'])
        self.type('\t\x05')
        state = self.wait_for(lambda s: not s['info']['pum_visible'])
        self.assertEqual(state['line'], 'To: ali')
        self.type('c')
        self.wait_for(lambda s: s['info']['pum_visible'])
        self.type('\x7f\x7f\x7f')
        state = self.wait_for(lambda s: s['line'] == 'To: a' and not s['info']['pum_visible'])
        self.assertEqual(state['line'], 'To: a')

    def test_subject_and_body_have_no_recipient_popup(self) -> None:
        for keys, line in [('4GA al', 'Subject: Test al'), ('\x1b6GA al', 'Body al'),
                           ('\x1b7GA al', 'To: quoted al')]:
            self.type(keys)
            state = self.wait_for(lambda s: s['line'] == line)
            self.assertFalse(state['info']['pum_visible'])

    def test_enter_without_selection_does_not_insert_a_suggestion(self) -> None:
        self.type('2GAal')
        self.wait_for(lambda s: s['info']['pum_visible'])
        self.type('\r')
        state = self.wait_for(lambda s: s['line'] == '' and not s['info']['pum_visible'])
        self.assertEqual(state['lines'][1:3], ['To: al', ''])

    def test_folded_recipient_header(self) -> None:
        self.type('2Go  Ro')
        state = self.wait_for(lambda s: s['info']['pum_visible'])
        self.assertEqual(state['info']['items'][0]['word'], 'bob@example.net')
        self.type('\t\r')
        self.wait_for(lambda s: s['line'] == '  bob@example.net' and not s['info']['pum_visible'])

    def test_mid_address_is_not_overwritten(self) -> None:
        self.type('2GAalice.smith@example.org\x1b0f.lial')
        state = self.wait_for(lambda s: s['line'] == 'To: alice.alsmith@example.org')
        self.assertFalse(state['info']['pum_visible'])

    def test_unicode_name_search(self) -> None:
        self.type('2GAÉl')
        state = self.wait_for(lambda s: s['info']['pum_visible'])
        self.assertEqual(state['info']['items'][0]['word'], 'elodie@example.org')
        self.type('\t\r')
        self.wait_for(lambda s: s['line'] == 'To: elodie@example.org' and not s['info']['pum_visible'])


if __name__ == '__main__':
    unittest.main()
