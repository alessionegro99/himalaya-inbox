"""Real Neovim yanks with a fake clipboard; never read or replace user clipboard data."""

import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from test_inbox import inbox


# Use Neovim's documented provider interface, entirely in memory:
# https://neovim.io/doc/user/provider/#g%3Aclipboard
CLIPBOARD_LUA = r'''
local data = {{'Unchanged clipboard'}, 'v'}
local function copy(lines, regtype)
  data = {lines, regtype}
  vim.g.test_copies = (vim.g.test_copies or 0) + 1
end
local function paste() return data end
vim.g.clipboard = {
  name = 'Synthetic test clipboard',
  copy = {['+'] = copy, ['*'] = copy},
  paste = {['+'] = paste, ['*'] = paste},
  cache_enabled = 0,
}
vim.g.test_copies = 0
'''


@unittest.skipUnless(shutil.which('nvim'), 'Neovim is not installed')
class ClipboardTests(unittest.TestCase):
    def test_yanks_reach_clipboard_but_named_and_blackhole_registers_do_not(self) -> None:
        cases = [
            ('ggyy', ['First synthetic line'], 'V', 1),
            ('gg2yy', ['First synthetic line', 'Second café line'], 'V', 1),
            ('ggVjy', ['First synthetic line', 'Second café line'], 'V', 1),
            ('ggviwy', ['First'], 'v', 1),
            ('gg"ayy', ['Unchanged clipboard'], 'v', 0),
            ('gg"_yy', ['Unchanged clipboard'], 'v', 0),
        ]
        for keys, lines, regtype, copies in cases:
            with self.subTest(keys=keys), patch.object(inbox, 'CONTACTS', []), \
                    inbox.editor_environment() as environment:
                yank = (
                    "vim.api.nvim_buf_set_lines(0, 0, -1, false, "
                    "{'First synthetic line', 'Second café line', 'Third line'}); "
                    f'vim.cmd.normal({{args = {{{json.dumps(keys)}}}, bang = true}})'
                )
                # Clipboard writes are flushed after returning from Lua.
                check = (
                    "print(vim.json.encode({lines = vim.fn.getreg('+', 1, true), "
                    "regtype = vim.fn.getregtype('+'), copies = vim.g.test_copies}))"
                )
                result = subprocess.run([
                    shutil.which('nvim'), '--headless', '-u', 'NONE', '-n', '-i', 'NONE',
                    '--cmd', 'set nomodeline noswapfile nobackup nowritebackup noundofile',
                    '--cmd', 'lua ' + CLIPBOARD_LUA,
                    '--cmd', 'lua dofile(vim.env.HIMALAYA_INBOX_INIT)',
                    '-c', 'lua ' + yank, '-c', 'lua ' + check, '-c', 'qa!',
                ], env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(json.loads(result.stdout), {
                    'lines': lines, 'regtype': regtype, 'copies': copies,
                })


if __name__ == '__main__':
    unittest.main()
