# Himalaya Inbox

My personal interactive setup for the original [Himalaya](https://github.com/pimalaya/himalaya) email CLI, shared so others can adapt it.

This is an independent **companion wrapper**, not a fork or an official part of Himalaya. It uses Himalaya for account access and sending; it does not modify the upstream program. No personal account configuration, email, draft, password, or token is included here.

## What it does

- Combines configured accounts into one interactive inbox.
- Groups conversations using `Message-ID`, `In-Reply-To`, and the full `References` ancestry, not subject matching.
- Includes your own sent replies and archived/filed messages in threads, oldest first.
- Reads plain text or extracts text from HTML without loading remote images or scripts.
- Lists attachments and inline images; saves or opens selected files with your desktop application.
- Replies, replies to all, and composes new mail in Neovim.
- Attaches local files to new messages and replies, with a file picker and removable attachments.
- Suggests recipients automatically by name or address in Neovim's To/Cc fields.
- Opens cached headers immediately, checks for new mail in the background, and preloads the highlighted message.
- Shows the message for review and queues it with a cancellable five-minute delay by default.
- Offers a custom delay or an explicit send-now confirmation.
- Keeps unfinished drafts locally so you can resume them.

The inbox is still the starting view: a thread appears there if it contains an Inbox message. Sent-only and archived conversations are available in the Sent and All views. Drafts, Trash, and Junk folders are excluded from the conversation index. Unrelated messages with identical subjects are not merged; missing or malformed threading headers can still leave messages separate.

## Requirements and installation

- Himalaya **2.1.x**, already configured with working incoming and outgoing authentication.
- IMAP accounts. Other Himalaya backends are not currently supported by full-conversation browsing.
- Linux/Unix terminal, Python 3.12+ with curses, [uv](https://docs.astral.sh/uv/), and Neovim 0.11+.
- Optional Linux desktop tools: `gio` (GLib) for opening downloaded files, and `xdg-user-dir` for finding your configured Downloads folder. Saving works without these tools.

```sh
git clone https://github.com/alessionegro99/himalaya-inbox.git
cd himalaya-inbox
install -m 755 himalaya-inbox "$HOME/.local/bin/himalaya-inbox"
himalaya-inbox
```

Ensure `~/.local/bin` is on your PATH. The script uses `uv` to run Python without installing packages into your system Python; it has no third-party Python dependencies.

It reads your existing configuration from `$HIMALAYA_CONFIG`, or `$XDG_CONFIG_HOME/himalaya/config.toml` (default `~/.config/himalaya/config.toml`). Use `--config /path/to/config.toml` for a different **single** configuration file. Split/merged config paths are not supported. `HIMALAYA_BIN` can select a different Himalaya executable.

Configure the `inbox`, `sent`, `drafts`, and `trash` mailbox aliases in your own Himalaya config. The wrapper reads all selectable mail folders except Drafts/Trash/Junk and their configured equivalents. The first launch builds a private local header index. Later launches show that index immediately while checking for new mail; the footer identifies the previous headers until synchronization completes. Press `u` to refresh in the background without blocking navigation. Failed refreshes retain the previous view and show a warning.

While the interactive inbox is open, it automatically refreshes every five minutes, including while you read a message or compose in Neovim. It preserves the selection, filter and conversation view; the current message/editor is not interrupted, and the updated list appears when you return. Manual `u` refresh remains available. Slow refreshes never overlap; offline failures retry at the next interval. The timer stops on exit and does not run for piped/`--list` output or background sending.

Folder discovery and change checks run concurrently. On servers supporting persistent `HIGHESTMODSEQ`, matching UID validity, UID-next, message count and modification sequence allow unchanged folders to reuse their indexed headers. Servers without usable modification sequences still get full header/flag checks, so read/unread changes in other clients are not missed. The selected conversation/message stays selected when new mail arrives.

The highlighted message is preloaded read-only. Opening it shares any in-flight download instead of starting another one. Recently opened messages and normal-sized attachments stay in a bounded RAM cache across refreshes if their identities still match. Press `q` to leave a loading message; quitting the client stops its outstanding background read processes. Message bodies are not persisted on disk by this cache.

Opening a message marks it read and removes its `*`. Selecting/preloading a message or opening the conversation list does not mark it read. Press `*` on a message, or inside the reader, to toggle read/unread. On a conversation summary, `*` marks the whole conversation read if any message is unread; otherwise it marks the conversation unread. Open the conversation first to change just one message.

Read/unread changes appear immediately and save to the mail server in the background, in the order you requested them. A failed save restores the last confirmed status and shows a warning; press `u` to check the server. A refresh already in progress cannot undo a newer action. Normal exit waits for pending read/unread saves; Escape returns to browsing while they finish.

## Keys

| Key | Action |
| --- | --- |
| Arrows, `j`/`k`, mouse wheel | Scroll/select |
| `3j` / `3k` (or another number) | Move down/up that many entries, or lines inside a message |
| Page Up/Down, `g`/`G` | Page or jump to beginning/end |
| Enter | Open a conversation, then a message (marks that message read) |
| `*` | Toggle read/unread for the selected message/conversation, or the open message |
| `x` in the message list or reader | Move one message to Trash, after confirmation |
| `r` / `R` | Reply / reply to all |
| `c` | Compose a new message; choose the sending account |
| `d` | Resume a local draft |
| `o` | Outbox: see countdowns and press `x` to cancel pending mail |
| `i` / `s` / `a` | Inbox / Sent / all indexed mail |
| `a` inside an open message | Browse attachments; Enter selects a file, then `o` opens or `s` saves it |
| `a` / `x` on the compose review screen | Attach a file / remove an attachment |
| `t` | Toggle conversation grouping |
| `/` | Filter the list or search an open message |
| Escape | Clear the list filter |
| `u` | Refresh mail in the background, including sent replies |
| `q` | Go back or quit |

Within a conversation, messages are oldest first. `You (sent)` identifies sent copies. A reply from a conversation summary targets its latest received message; open the conversation and select another message to reply to that one instead.

Type a number followed by `j` or `k`: `3j` moves down three, `3k` moves up three, and `20j` moves down twenty. This works in the inbox, conversation lists, selection menus, and message reader; Up/Down arrows also accept counts. Movement stops at the beginning/end. The footer shows a pending count; Escape cancels it (and still clears a list filter or closes a menu). Counts apply only to these up/down motions, not to reply, Trash, or other actions. Use `G` to jump straight to the last/newest message in a conversation, then `r` to reply to it.

### Moving mail to Trash

Select a message and press `x`, or press `x` while reading it. Review the subject, account and destination, then press `y` to move it to that account's configured Trash folder. Press `n`, Escape or `q` to cancel. On a conversation summary, first choose **one** message; the rest of the conversation, including your replies, is left alone. The `d` key still opens drafts.

This uses Himalaya's `message move`, never permanent deletion or expunge. Restore a message from the account's Trash folder in Thunderbird or webmail, subject to the provider's retention policy. A failed or interrupted move is not retried automatically: check Trash and press `u` before trying again. Successfully moved messages are removed from the view and local header/body caches; an older in-flight refresh cannot put the old message back. Tests use synthetic mail and do not move real emails.

### Replying

1. Press `r` (or `R` for reply all).
2. Edit the recipient/subject fields and write your reply above the quoted text in Neovim.
3. Save and exit with `:wq`. **This does not send.**
4. Review the complete message. Press `e` to edit again or `q` to keep the draft.
5. Press `s`, then type `SEND` and Enter to **queue for five minutes**. Press `t` on the review screen to choose another delay (1-1440 minutes).
6. To cancel before delivery starts, press `o` in the message list, select the queued mail and press `x`. You can also open `himalaya-inbox --outbox` directly.
7. For immediate delivery instead, press capital `N` on the review screen and type **`SEND NOW`**.
8. Press `u` in the message list to refresh the conversation after delivery.

The account is selected from the original message, and the configured email is used for the From address. Reply-To is honored. Reply-all removes your configured addresses and never copies Bcc recipients. Reply ancestry is retained.

The editor is explicitly `nvim`, regardless of `$EDITOR` or `$VISUAL`. It runs without your plugins, modelines, swap, undo history, or shada to avoid executing anything in quoted email or copying drafts into editor caches. The added editor configuration provides recipient completion and system-clipboard access; your normal Neovim configuration is not changed.

To copy from the email editor, use `yy` for a line, `3yy` for three lines, or select text with `v`/`V` and press `y`. Paste into another terminal with `Ctrl+Shift+V`, or into a desktop app with `Ctrl+V`. This uses Neovim's [built-in clipboard integration](https://neovim.io/doc/user/provider/#provider-clipboard) (`unnamedplus`) and its automatically selected clipboard tool, such as `xclip` on X11 or `wl-copy`/`wl-paste` on Wayland. Cuts also use the clipboard, and `p` pastes from it. Copied/cut text may be retained by your desktop clipboard history; the editor's other privacy protections remain enabled.

### New messages and recipient suggestions

Press `c` in the inbox and choose the sending account. In Neovim, press `i` to edit the To, Cc, Subject, and body fields.

In **To** or **Cc**, type at least two characters of a name or email address. A popup shows matching addresses and names from already-loaded From/To headers across your accounts, including Sent mail. It does not import Thunderbird's separate address book. Matching is case-insensitive and supports parts of names, multiword names, and addresses; no extra network request is made while typing.

- `Tab` / `Shift-Tab` or `Ctrl-N` / `Ctrl-P`: select the next/previous suggestion.
- `Enter`: accept the selected address. With no suggestion selected, Enter retains its normal newline behavior.
- `Ctrl-E`: close the popup and keep what you typed.
- Type a comma and start typing again to add another recipient.

No suggestion is selected automatically. Only the email address is inserted, and autocomplete stays out of Subject and body text. Check the complete recipient addresses in the review screen: display names from received mail are not verified identities.

Finish editing with `Esc`, then `:wq` and Enter. Review and queue/send as described above; saving in Neovim never sends mail.

### Attachments

**Received files:** open a message and press `a`. Choose an attachment with the arrows or mouse and press Enter. Press `s` to save it to your desktop Downloads folder, or `o` to save and open it with the default application. Press `q` to go back. Inline images are included; attached emails are saved as `.eml` files without separately extracting their internal attachments.

The saved path appears at the bottom. Existing files are never overwritten: a duplicate gets a numbered name such as `report (1).pdf`. Downloaded files have permissions 600 and no executable bit. Filenames are stripped of directory components and unsafe display characters. Opening is an explicit action, never automatic; known executable/launcher extensions are save-only. External viewers are not sandboxed and may access the network, so only open files you trust. The fallback download location is `~/Downloads` if `xdg-user-dir` is unavailable.

**Sending files:** compose with `c`, or reply with `r`/`R`, then save and exit Neovim using `:wq`. On the review screen press `a`. Browse to a file with arrows and Enter, or select the first entry to type/paste a path. Choose directories to enter them and `../` to go up; hidden files can be selected by typing their full path. Repeat `a` for more files. Press `x` to choose an attachment to remove. The review shows the attachment count and filenames; adding/removing files does not send the message.

You can also fill in the `Attach:` header directly in Neovim, above the blank line separating headers from the message body:

```text
Attach: ~/Documents/report.pdf
Attach: ~/Pictures/figure with spaces.png
```

Use one absolute path (or `~/...`) per header, without shell quotes or escaping. Neovim's built-in `Ctrl-X Ctrl-F` completes file paths. These headers stay local; recipients receive only attachment basenames and contents, never your local paths. A saved draft remembers file paths, not copies: files must still exist when you resume it. Files are read when the message is built for review; the confirmed outgoing message contains its own copy. Incoming attachments are **not** automatically included in replies. The combined attachment file-size limit is 20 MiB before MIME encoding; your mail server may impose a smaller limit.

### Enable delayed sending (Linux/systemd)

Install the per-user background timer after installing the executable:

```sh
mkdir -p "$HOME/.config/systemd/user"
install -m 644 systemd/himalaya-inbox-outbox.service systemd/himalaya-inbox-outbox.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now himalaya-inbox-outbox.timer
```

The timer checks every 10 seconds, so delivery starts **no earlier than** the chosen delay and normally within about 10 seconds after it. Closing the browser does not cancel queued mail. The laptop must be awake, logged in, online and able to unlock the configured mail credentials; overdue mail is processed after the timer next runs. This is a local queue, not a provider-hosted scheduling service.

The browser refuses to queue silently if the timer is not active. Immediate sending does not require systemd. To pause all delayed deliveries:

```sh
systemctl --user stop himalaya-inbox-outbox.timer
```

Stopping the timer does not recall a delivery already in progress and does not remove queued messages. Re-enabling it can send overdue pending messages; inspect/cancel them in Outbox first.

If you customize `XDG_STATE_HOME` or `HIMALAYA_BIN`, set the same values in the user service with `systemctl --user edit himalaya-inbox-outbox.service` so the browser and worker use the same queue and executable.

Cancellation and delivery claims are serialized with SQLite transactions. If cancellation succeeds, the queued body is removed and cannot be sent. Once a worker claims an item, it is no longer cancellable. A crashed/interrupted worker leaves the item out of the retry queue; check Sent if an item remains `sending` or reports `uncertain`. This deliberately favors avoiding duplicate or unintended mail over automatic retries.

### Plain listing

The original script-friendly listing remains available:

```sh
himalaya-inbox --list           # newest 50 inbox messages
himalaya-inbox --list --all     # all inbox messages
himalaya-inbox --list -n 100
himalaya-inbox --all | less -S  # piping also selects plain-list mode
```

## Privacy and sending safety

- Authentication stays with Himalaya and its configured helpers. This wrapper does not print their raw output, errors, or credentials.
- Header browsing and preloading use read-only/peek fetching. Opening a message marks it read; `*` changes read/unread explicitly. Only the Seen flag is changed; other flags are preserved. These changes synchronize through your mail server to other clients.
- Headers (including subjects and From/To addresses) are cached as **plaintext** under `$XDG_CACHE_HOME/himalaya-inbox` (default `~/.cache/himalaya-inbox`), with directory permissions 700 and file permissions 600. This cache contains no message bodies, Bcc fields, passwords, OAuth tokens, or account configuration. It is isolated by a hash of the configuration and written atomically. Removing this directory forces a full header reload next time.
- Message bodies are fetched when highlighted, opened, or used to prepare a reply, without marking messages read. The RAM cache retains at most 16 messages, at most 32 MiB each and 64 MiB combined in UTF-8 encoding, until exit. Refresh preserves only bodies whose account/folder/UID validity/UID/Message-ID still match. Larger messages are not cached. Remote HTML resources are never fetched; terminal control characters are removed before display.
- Recipient suggestions are collected in memory from loaded address headers, not bodies or Bcc. While editing, Neovim receives a private temporary JSON file (permissions 600 inside a 700 directory), removed when the editor returns. No contacts database, mail cache, or personal editor configuration is included in this repository.
- Drafts are **plaintext**, stored outside the repository under `$XDG_STATE_HOME/himalaya-inbox/drafts` (default `~/.local/state/himalaya-inbox/drafts`). Directory permissions are 700 and draft files start at 600. Protect the laptop/account and its backups accordingly.
- The scheduled Outbox is also **plaintext**, in `himalaya-inbox/outbox.sqlite3` beside the drafts directory (permissions 600). Pending or uncertain deliveries retain their bodies. Successful deliveries and cancelled entries retain only status metadata; their queued bodies are removed. This is not a guarantee that older backups contain no copies.
- Sent drafts are removed locally after successful sending and saving. Cancelled or uncertain drafts remain available through `d`.
- After scheduling, the draft moves into Outbox. Cancelling there removes the queued body rather than restoring a draft.
- Delivery is attempted once. An error or timeout can mean delivery is uncertain: **check Sent before retrying**. The program never retries automatically.
- Sending and saving the Sent copy are separate operations. If sending succeeds but saving fails, the UI explicitly says not to resend. Gmail's automatic SMTP Sent copy is not duplicated.
- No test in this repository authenticates to a real account or sends real mail. All fixtures use synthetic addresses.

Do not commit your live Himalaya configuration, OAuth data, mail, logs, or drafts. The ignore rules provide a safety net, not a substitute for reviewing files before publishing.

## Tests

```sh
uv run --no-project --python 3.12 python -m unittest discover -s tests
```

Tests cover ordering, paging, account/folder identity, conversation ancestry, MIME rendering, attachment byte round trips and safe downloads, draft attachment persistence, terminal controls, actual pseudo-terminal and Neovim navigation/autocomplete, private temporary contacts and header caches, bounded body caches, shared in-flight downloads, concurrent folder loading, safe change detection, nonblocking refresh and selection stability, automatic read marking, read/unread toggles and ordered saves, refresh races and failed-save rollback, reply recipients, confirmation, fake-clock delays, competing delivery claims, cancellation, and uncertain-delivery behavior. Sending, read/unread writes and attachment viewers are mocked; no real messages are sent or marked by the tests.

## Scope and upstream credit

This is a small personal interface, not a full replacement for Thunderbird. Encryption/signing, remote draft synchronization, server-side deletion, and persistent/offline message-body storage are not implemented. Cached headers can be browsed offline; opening an uncached body still needs the server. Only one configuration file is supported.

The transport/backend work is provided by [Pimalaya's Himalaya](https://github.com/pimalaya/himalaya). Thread identifiers follow [RFC 5322 §3.6.4](https://www.rfc-editor.org/rfc/rfc5322#section-3.6.4); header-only fetching follows [IMAP RFC 3501](https://www.rfc-editor.org/rfc/rfc3501). Python's standard-library `email` and `curses` modules provide MIME handling and the terminal interface.

The wrapper is MIT-licensed; upstream projects retain their own licenses. Contributions that keep the code small, testable, and safe are welcome.
