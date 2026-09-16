"""Exercise the real curses terminal and message reader with synthetic mail only."""

import curses
from email.message import EmailMessage
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import sys
import tempfile
import termios
import time

from test_inbox import inbox, row


def child() -> None:
    """Display deterministic synthetic messages through the real pager."""
    os.environ['TERM'] = 'xterm-256color'
    rows = [row('test', '1', 'solo', subject='SYNTHETIC_SINGLE'),
            row('test', '2', 'reply', ['root'], subject='SYNTHETIC_REPLY'),
            row('other', '2', 'root', subject='SYNTHETIC_ROOT')]

    def fake_read(args: list[str]) -> dict:
        assert '--seen' not in args
        account = args[args.index('--account') + 1]
        body = '\n'.join(f'SYNTHETIC_BODY_{account}_{args[-1]}_LINE_{i:03d}' for i in range(90))
        message = EmailMessage()
        message['Subject'] = 'Synthetic test'
        message.set_content(body)
        message.add_attachment(b'SYNTHETIC_ATTACHMENT', maintype='application', subtype='pdf', filename='terminal file.pdf')
        return {'message': message.as_string()}

    inbox.run_himalaya = fake_read
    flag_calls = []

    def fake_flags(args, data=None):
        assert args[2:4] in (['flag', 'add'], ['flag', 'remove'])
        assert args[4:8] == ['--mailbox', 'inbox', '--flag', 'seen']
        flag_calls.append(args)
        return b''

    inbox.run_private = fake_flags
    with tempfile.TemporaryDirectory() as directory:
        inbox.download_directory = lambda: Path(directory)

        def mock_viewer(path):
            assert path.read_bytes() == b'SYNTHETIC_ATTACHMENT'
            return 'SYNTHETIC_VIEWER'

        inbox.open_attachment = mock_viewer
        curses.wrapper(inbox.browse, rows)
        assert len(list(Path(directory).iterdir())) == 1
        assert [(args[1], args[3], args[-1]) for args in flag_calls] == [
            ('test', 'add', '1'), ('test', 'remove', '1'),
            ('test', 'add', '1'), ('other', 'add', '2')]


pid, master = pty.fork()
if pid == 0:
    try:
        child()
    except BaseException:
        os._exit(1)
    os._exit(0)

fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 110, 0, 0))
buffer = bytearray()


def expect(marker: bytes, timeout: float = 6) -> None:
    """Check terminal output without dumping message text or terminal controls."""
    deadline = time.monotonic() + timeout
    while marker not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f'Terminal did not reach expected synthetic state: {marker!r}')
        if select.select([master], [], [], remaining)[0]:
            buffer.extend(os.read(master, 65536))
    buffer.clear()


try:
    expect(b'SYNTHETIC_SINGLE')
    os.write(master, b'\r')
    expect(b'SYNTHETIC_BODY_test_1_LINE_000')
    # Test read/unread in the real reader. Curses may update just "un" in
    # the footer, so verify the backend call sequence in the child instead.
    os.write(master, b'*a')
    expect(b'ATTACHMENTS')
    os.write(master, b'\r')
    # Curses retains the unchanged ATTACHMENT prefix from the previous screen.
    expect(b'o open')
    os.write(master, b's')
    expect(b'Saved:')
    os.write(master, b'o')
    expect(b'SYNTHETIC_VIEWER')
    os.write(master, b'qq')
    expect(b'SYNTHETIC_BODY_test_1_LINE_000')
    # Search and scrolling happen inside the actual message reader.
    os.write(master, b'/LINE_070\r')
    expect(b'LINE_070')
    # Curses can update only the changed digits; do not expect full lines to be
    # re-emitted after a scroll. Unit tests check the resulting line positions.
    os.write(master, b'*gGq')
    expect(b'Unified inbox')
    # Down arrow in xterm application mode, then open the conversation.
    os.write(master, b'\x1bOB\r')
    expect(b'Conversation:')
    os.write(master, b'j\r')
    expect(b'SYNTHETIC_BODY_other_2_LINE_000')
    os.write(master, b'q')
    expect(b'Conversation:')
    os.write(master, b'q')
    expect(b'Unified inbox')
    # A resize must not lose the selection or break rendering.
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 18, 90, 0, 0))
    os.kill(pid, signal.SIGWINCH)
    os.write(master, b'q')
    deadline = time.monotonic() + 6
    while True:
        ended, status = os.waitpid(pid, os.WNOHANG)
        if ended:
            assert os.waitstatus_to_exitcode(status) == 0
            break
        if time.monotonic() >= deadline:
            raise AssertionError('Terminal did not exit cleanly')
        select.select([master], [], [], 0.05)
    attrs = termios.tcgetattr(master)
    assert attrs[3] & termios.ECHO and attrs[3] & termios.ICANON
    print('PASS: real terminal navigation, thread opening, reader search/scroll, resize, and terminal restoration.')
except BaseException:
    try:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
    except ProcessLookupError:
        pass
    raise
finally:
    os.close(master)
