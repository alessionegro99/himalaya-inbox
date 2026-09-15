"""Real Neovim handoff with synthetic mail; sending is replaced by an in-memory check."""

import curses
import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import tempfile
import termios
import time
import unittest
from pathlib import Path

from test_inbox import inbox, row
from test_reply import CONFIG, SOURCE


@unittest.skipUnless(shutil.which('nvim'), 'Neovim is not installed')
class EditorIntegrationTests(unittest.TestCase):
    def test_actual_editor_cancel_and_confirmed_mock_send(self) -> None:
        cases = [(False, False, False), (True, False, False), (False, True, False),
                 (True, True, False), (True, True, True)]
        for send, attach, new in cases:
            with self.subTest(send=send, attach=attach, new=new), tempfile.TemporaryDirectory() as directory:
                attachment_path = Path(directory) / 'synthetic file with spaces.bin'
                attachment_path.write_bytes(bytes(range(256)))
                pid, master = pty.fork()
                if pid == 0:
                    try:
                        os.environ['TERM'] = 'xterm-256color'
                        inbox.SETTINGS = CONFIG
                        inbox.draft_directory = lambda: Path(directory)
                        inbox.read_raw = lambda _: SOURCE
                        sent = []

                        def mock_send(account, message):
                            assert account == 'work'
                            assert 'Synthetic editor reply' in message.get_body().get_content()
                            assert str(message['To']) == 'reply@example.net'
                            files = inbox.attachment_parts(message)
                            assert len(files) == int(attach)
                            if attach:
                                assert inbox.attachment_bytes(files[0]) == bytes(range(256))
                            sent.append(message)
                            return 'SENT. Synthetic transport only.'

                        inbox.send_confirmed = mock_send
                        curses.wrapper(inbox.compose, None if new else row('work', '1', 'one'))
                        assert bool(sent) == send
                        os.write(1, b'EDITOR_TEST_FINISHED\n')
                        os._exit(0)
                    except BaseException:
                        os._exit(1)
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 28, 110, 0, 0))
                buffer = bytearray()

                def expect(marker):
                    deadline = time.monotonic() + 12
                    while marker not in buffer:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AssertionError('Synthetic editor test did not reach expected state')
                        if select.select([master], [], [], remaining)[0]:
                            buffer.extend(os.read(master, 65536))
                    buffer.clear()

                try:
                    if new:
                        expect(b'Send from account')
                        os.write(master, b'\r')
                    expect(b'X-Himalaya-Account')
                    if new:
                        os.write(master, b'gg/^To:\rAreply@example.net\x1b')
                    os.write(master, b'GoSynthetic editor reply\x1b:wq\r')
                    expect(b'REVIEW')
                    if attach:
                        os.write(master, b'a')
                        expect(b'ATTACH FILE')
                        os.write(master, b'\r')
                        expect(b'File/folder path')
                        os.write(master, str(attachment_path).encode() + b'\r')
                        expect(b'File attached')
                    if send:
                        os.write(master, b'N')
                        expect(b'Type SEND NOW')
                        os.write(master, b'SEND NOW\r')
                        expect(b'Synthetic transport only')
                    os.write(master, b'q')
                    expect(b'EDITOR_TEST_FINISHED')
                    _, status = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    paths = list(Path(directory).glob('draft-*.txt'))
                    self.assertEqual(len(paths), 0 if send else 1)
                    if paths:
                        self.assertEqual(inbox.draft_attachments(paths[0].read_text()), [str(attachment_path)] if attach else [])
                except BaseException:
                    try:
                        os.kill(pid, signal.SIGTERM)
                        os.waitpid(pid, 0)
                    except ProcessLookupError:
                        pass
                    raise
                finally:
                    os.close(master)


if __name__ == '__main__':
    unittest.main()
