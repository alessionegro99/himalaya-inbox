"""Synthetic links/viewer checks: no real mailbox, clipboard, or GUI is used."""

from collections import deque
import curses
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_inbox import FakeScreen, inbox, row


URL = 'https://example.org/join?token=SYNTHETIC%2Bvalue&group=test#finish'


def html_mail(html: str) -> EmailMessage:
    message = EmailMessage()
    message.set_content(html, subtype='html')
    return message


class LinkExtractionTests(unittest.TestCase):
    def test_button_url_label_and_entities_are_preserved(self) -> None:
        message = html_mail('<a href="' + URL.replace('&', '&amp;') + '">Join <b>group</b></a>')
        self.assertEqual(inbox.message_links(message), [('Join group', URL)])
        rendered = inbox.message_text(message.as_string(), row('test', '1', 'one'))
        self.assertIn('[1] Join group\n' + URL, rendered)
        self.assertNotIn('&amp;', rendered)

    def test_plain_view_keeps_button_targets_from_html_alternative(self) -> None:
        message = EmailMessage()
        message.set_content('Plain text remains the preferred body.')
        message.add_alternative(f'<p>HTML-ONLY-LAYOUT</p><a href="{URL}">Join group</a>', subtype='html')
        rendered = inbox.message_text(message.as_string(), row('test', '1', 'one'))
        self.assertIn('Plain text remains', rendered)
        self.assertNotIn('HTML-ONLY-LAYOUT', rendered)
        self.assertIn(URL, rendered)

    def test_deduplication_preserves_first_label_and_url_case(self) -> None:
        message = EmailMessage()
        message.set_content(URL)
        message.add_alternative(f'<a href="{URL}">First</a><a href="{URL}">Again</a>'
                                '<a href="https://example.org/UPPER">Different</a>', subtype='html')
        self.assertEqual(inbox.message_links(message), [('First', URL), ('Different', 'https://example.org/UPPER')])

    def test_bare_links_balanced_parentheses_and_sentence_punctuation(self) -> None:
        text = ('See (https://example.org/a_(b)). Then <https://example.org/other>; '
                'mail mailto:person@example.net. HTTPS://example.org/UPPER')
        self.assertEqual([url for _, url in inbox.text_links(text)], [
            'https://example.org/a_(b)', 'https://example.org/other',
            'mailto:person@example.net', 'HTTPS://example.org/UPPER'])

    def test_signed_href_and_query_are_not_rewritten(self) -> None:
        for url in (URL, 'https://example.org/?token=a+b%2F%3D,.;',
                    'https://example.org/trailing.', 'https://example.org/?literal=%2526amp%3B'):
            with self.subTest(url=url):
                self.assertEqual(inbox.message_links(html_mail(f'<a href="{url}">Go</a>')), [('Go', url)])
        query = 'https://example.org/?token=a+b%2F%3D,.;'
        self.assertEqual(inbox.text_links(query), [(query, query)])

    def test_mime_transfer_encoding_does_not_break_long_invitation_links(self) -> None:
        url = 'https://example.org/join?token=' + 'SYNTHETIC%2B' * 40 + '&group=test'
        for cte in ('quoted-printable', 'base64'):
            message = EmailMessage()
            message.set_content(f'<a href="{url}">Join</a>', subtype='html', cte=cte)
            parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
            self.assertEqual(inbox.message_links(parsed), [('Join', url)])

    def test_blank_image_and_unclosed_anchors_have_targets(self) -> None:
        self.assertEqual(inbox.message_links(html_mail(f'<a href="{URL}"><img src="cid:logo"></a>')), [(URL, URL)])
        self.assertEqual(inbox.message_links(html_mail(f'<a href="{URL}">Join')), [('Join', URL)])

    def test_visible_html_urls_are_found_but_remote_resources_are_not(self) -> None:
        message = html_mail('<head><a href="https://head.invalid/">hidden</a></head>'
                            '<script>https://script.invalid/</script><style>https://style.invalid/</style>'
                            '<img src="https://image.invalid/">'
                            '<p>Visit https://example.org/visible</p>')
        self.assertEqual(inbox.message_links(message), [('https://example.org/visible', 'https://example.org/visible')])

    def test_attached_html_text_and_forwarded_email_are_excluded(self) -> None:
        message = EmailMessage()
        message.set_content('Normal body')
        message.add_attachment('<a href="https://attachment.invalid/">Hidden</a>', subtype='html', filename='file.html')
        message.add_attachment('https://text.invalid/', subtype='plain', filename='file.txt')
        message.add_attachment(html_mail('<a href="https://nested.invalid/">Nested</a>'), filename='forward.eml')
        self.assertEqual(inbox.message_links(message), [])

    def test_related_html_body_is_found_without_inline_image_urls(self) -> None:
        message = EmailMessage()
        message.set_content('Plain')
        message.add_alternative(f'<a href="{URL}">Go</a><img src="cid:logo">', subtype='html')
        message.get_payload()[1].add_related(b'IMAGE', maintype='image', subtype='png', cid='<logo>')
        self.assertEqual(inbox.message_links(message), [('Go', URL)])

    def test_dangerous_relative_and_control_character_urls_are_rejected(self) -> None:
        for url in ('javascript:alert(1)', 'data:text/html,body', 'file:///private', '//example.org',
                    'https://', 'https://[bad', 'https://example.org:bad/',
                    'https://example.org/white space', 'https://example.org/\x1b',
                    'https://example.org/\u202e', 'https://example.org/\n', ''):
            with self.subTest(url=url):
                self.assertFalse(inbox.link_url(url))
        message = html_mail('<a href="https://example.org/a&#10;b">Bad</a>'
                            '<a href="javascript:alert(1)">Bad</a>')
        self.assertEqual(inbox.message_links(message), [])

    def test_empty_or_encrypted_message_has_no_links(self) -> None:
        message = EmailMessage()
        message.set_content(b'ENCRYPTED', maintype='application', subtype='octet-stream')
        self.assertEqual(inbox.message_links(message), [])


class ClipboardLinkTests(unittest.TestCase):
    def test_available_backends_get_full_url_on_stdin_not_command_line(self) -> None:
        cases = [({'DISPLAY': ':synthetic'}, {'xclip'}, ['xclip', '-selection', 'clipboard', '-silent']),
                 ({'WAYLAND_DISPLAY': 'synthetic', 'DISPLAY': ':synthetic'}, {'wl-copy', 'xclip'},
                  ['wl-copy', '--type', 'text/plain;charset=utf-8']),
                 ({}, {'termux-clipboard-set'}, ['termux-clipboard-set']),
                 ({'WAYLAND_DISPLAY': 'synthetic', 'DISPLAY': ':synthetic'}, {'xclip'},
                  ['xclip', '-selection', 'clipboard', '-silent'])]
        for env, binaries, expected in cases:
            with self.subTest(command=expected), patch.dict(os.environ, env, clear=True), \
                    patch.object(inbox.shutil, 'which', side_effect=lambda name: '/fake/' + name if name in binaries else None), \
                    patch.object(subprocess, 'run') as run:
                self.assertIn('Full link copied', inbox.copy_link(URL))
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], expected)
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs['input'], URL.encode())
            self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            self.assertEqual(kwargs['timeout'], 5)
            self.assertTrue(kwargs['start_new_session'])
            self.assertTrue(kwargs['check'])
            self.assertFalse(kwargs.get('shell', False))

    def test_no_helper_or_invalid_url_does_not_launch_anything(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(inbox.shutil, 'which', return_value=None), \
                patch.object(subprocess, 'run') as run:
            self.assertIn('No clipboard helper', inbox.copy_link(URL))
            self.assertIn('nothing copied', inbox.copy_link('javascript:alert(1)'))
            run.assert_not_called()

    def test_failure_and_timeout_do_not_leak_urls_or_diagnostics(self) -> None:
        for error in (OSError('PRIVATE'), subprocess.CalledProcessError(1, ['PRIVATE'], stderr=b'PRIVATE'),
                      subprocess.TimeoutExpired(['PRIVATE'], 5)):
            with self.subTest(error=type(error)), patch.dict(os.environ, {'DISPLAY': ':synthetic'}), \
                    patch.object(inbox.shutil, 'which', return_value='/fake/xclip'), \
                    patch.object(subprocess, 'run', side_effect=error):
                status = inbox.copy_link(URL)
                self.assertIn('Could not copy', status)
                self.assertNotIn('PRIVATE', status)
                self.assertNotIn(URL, status)


class LinkMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.message = EmailMessage()
        self.urls = [URL + str(i) for i in range(20)]
        self.message.set_content('\n'.join(self.urls))
        self.raw = self.message.as_string()
        self.copy = patch.object(inbox, 'copy_link', return_value='Copied.').start()
        self.addCleanup(patch.stopall)

    def test_keyboard_counts_and_end_copy_only_selected_link(self) -> None:
        for keys, index in [(['3', 'j', '\n'], 3), (['G', '\n'], 19), (['G', '3', 'k', '\n'], 16)]:
            self.copy.reset_mock()
            self.assertEqual(inbox.show_links(FakeScreen(keys, (8, 40)), self.raw), 'Copied.')
            self.copy.assert_called_once_with(self.urls[index])

    def test_single_click_copies_full_url_not_clipped_display(self) -> None:
        screen = FakeScreen([curses.KEY_MOUSE], (8, 20))
        with patch.object(curses, 'getmouse', return_value=(0, 2, 2, 0, curses.BUTTON1_CLICKED)):
            self.assertEqual(inbox.show_links(screen, self.raw), 'Copied.')
        self.copy.assert_called_once_with(self.urls[1])

    def test_click_tracks_scrolled_and_resized_menu(self) -> None:
        screen = FakeScreen(['G', curses.KEY_RESIZE, curses.KEY_MOUSE], (8, 30))
        with patch.object(curses, 'getmouse', return_value=(0, 2, 2, 0, curses.BUTTON1_CLICKED)):
            inbox.show_links(screen, self.raw)
        self.copy.assert_called_once_with(self.urls[15])

    def test_cancel_hover_wheel_and_clicking_chrome_do_not_copy(self) -> None:
        for buttons, y in [(curses.BUTTON1_CLICKED, 0), (curses.BUTTON1_CLICKED, 7),
                           (curses.BUTTON4_PRESSED, 2), (curses.REPORT_MOUSE_POSITION, 2)]:
            with self.subTest(buttons=buttons, y=y), \
                    patch.object(curses, 'getmouse', return_value=(0, 2, y, 0, buttons)):
                inbox.show_links(FakeScreen([curses.KEY_MOUSE, 'q'], (8, 30)), self.raw)
        self.copy.assert_not_called()

    def test_single_click_still_only_selects_in_other_menus(self) -> None:
        with patch.object(curses, 'getmouse', return_value=(0, 2, 2, 0, curses.BUTTON1_CLICKED)):
            self.assertIsNone(inbox.choose(FakeScreen([curses.KEY_MOUSE, 'q']), 'OTHER', ['a', 'b']))

    def test_no_links_and_parse_error_do_not_copy(self) -> None:
        self.assertIn('No web or mailto', inbox.show_links(FakeScreen([]), 'Subject: Synthetic\n\nNo link'))
        with patch.object(inbox, 'message_links', side_effect=ValueError('PRIVATE')):
            self.assertNotIn('PRIVATE', inbox.show_links(FakeScreen([]), self.raw))
        self.copy.assert_not_called()

    def test_reader_dispatches_copy_and_thunderbird_without_reply_or_refetch(self) -> None:
        screen = FakeScreen(['l', 'j', '\n', 'h', 'q'])
        with patch.object(inbox, 'read_raw', return_value=self.raw) as read, \
                patch.object(inbox, 'mark_seen', return_value=''), \
                patch.object(inbox, 'SEEN_QUEUE', deque()), \
                patch.object(inbox, 'PREFETCH_ENABLED', False), \
                patch.object(inbox, 'open_in_thunderbird', return_value='Requested.') as launch, \
                patch.object(inbox, 'compose') as compose:
            inbox.show_message(screen, row('test', '1', 'one'))
        read.assert_called_once()
        launch.assert_called_once_with(self.raw)
        self.copy.assert_called_once_with(self.urls[1])
        compose.assert_not_called()


class ThunderbirdTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix='inbox-viewer-test-')
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name)
        for override in (patch.dict(os.environ, {'DISPLAY': ':synthetic', 'XDG_RUNTIME_DIR': str(self.runtime)}, clear=True),
                         patch.object(inbox.shutil, 'which', return_value='/fake/thunderbird'),
                         patch.object(inbox, 'Thread')):
            override.start()
            self.addCleanup(override.stop)
        self.launch = patch.object(subprocess, 'Popen', return_value=Mock()).start()
        self.addCleanup(patch.stopall)

    def test_original_mime_and_attachments_are_private_and_survive_handoff(self) -> None:
        message = EmailMessage()
        message['Subject'] = 'Synthetic;$(not-a-command)'
        message.set_content('Plain body')
        message.add_alternative(f'<b>Full HTML</b><a href="{URL}">Join</a>', subtype='html')
        message.add_attachment(b'\x00\xffATTACHMENT', maintype='application', subtype='octet-stream', filename='file.bin')
        raw = message.as_string(policy=policy.SMTP)
        self.assertIn('Requested Thunderbird', inbox.open_in_thunderbird(raw))
        args = self.launch.call_args.args[0]
        self.assertEqual(args[:2], ['/fake/thunderbird', '-file'])
        path = Path(args[2])
        self.assertEqual(path.name, 'message.eml')
        self.assertEqual(path.parent.parent, self.runtime)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.read_bytes(), raw.encode('utf-8'))
        parsed = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        self.assertIn('Full HTML', parsed.get_body(('html',)).get_content())
        self.assertEqual(next(parsed.iter_attachments()).get_payload(decode=True), b'\x00\xffATTACHMENT')
        kwargs = self.launch.call_args.kwargs
        for name in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(kwargs[name], subprocess.DEVNULL)
        self.assertTrue(kwargs['start_new_session'])
        self.assertFalse(kwargs.get('shell', False))

    def test_missing_thunderbird_or_desktop_creates_no_preview(self) -> None:
        with patch.object(inbox.shutil, 'which', return_value=None):
            self.assertIn('not installed', inbox.open_in_thunderbird('Synthetic'))
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn('graphical desktop', inbox.open_in_thunderbird('Synthetic'))
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.launch.assert_not_called()

    def test_failed_launch_cleans_up_and_suppresses_diagnostics(self) -> None:
        self.launch.side_effect = OSError('PRIVATE')
        status = inbox.open_in_thunderbird('Subject: Synthetic\n\n' + URL)
        self.assertIn('Could not launch', status)
        self.assertNotIn('PRIVATE', status)
        self.assertNotIn(URL, status)
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_missing_runtime_uses_private_system_temp_directory(self) -> None:
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': ''}), \
                patch.object(inbox.tempfile, 'tempdir', str(self.runtime)):
            self.assertIn('Requested', inbox.open_in_thunderbird('Subject: Synthetic\n\nBody'))
        path = Path(self.launch.call_args.args[0][2])
        self.assertEqual(path.parent.parent, self.runtime)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_separate_previews_do_not_overwrite_each_other(self) -> None:
        inbox.open_in_thunderbird('Subject: First\n\nFirst')
        inbox.open_in_thunderbird('Subject: Second\n\nSecond')
        paths = [Path(call.args[0][2]) for call in self.launch.call_args_list]
        self.assertNotEqual(paths[0], paths[1])
        self.assertIn('First', paths[0].read_text())
        self.assertIn('Second', paths[1].read_text())


if __name__ == '__main__':
    unittest.main()
