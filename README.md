# Himalaya Inbox

My personal interactive setup for the original [Himalaya](https://github.com/pimalaya/himalaya) email CLI, shared so others can adapt it.

This is an independent **companion wrapper**, not a fork or an official part of Himalaya. It uses Himalaya for account access and sending; it does not modify the upstream program. No personal account configuration, email, draft, password, or token is included here.

## What it does

- Combines configured accounts into one interactive inbox.
- Groups conversations using `Message-ID`, `In-Reply-To`, and the full `References` ancestry, not subject matching.
- Includes your own sent replies and archived/filed messages in threads, oldest first.
- Reads plain text or extracts text from HTML without loading remote images or scripts.
- Replies, replies to all, and composes new mail in Neovim.
- Shows the message for review and queues it with a cancellable five-minute delay by default.
- Offers a custom delay or an explicit send-now confirmation.
- Keeps unfinished drafts locally so you can resume them.

The inbox is still the starting view: a thread appears there if it contains an Inbox message. Sent-only and archived conversations are available in the Sent and All views. Drafts, Trash, and Junk folders are excluded from the conversation index. Unrelated messages with identical subjects are not merged; missing or malformed threading headers can still leave messages separate.

## Requirements and installation

- Himalaya **2.1.x**, already configured with working incoming and outgoing authentication.
- IMAP accounts. Other Himalaya backends are not currently supported by full-conversation browsing.
- Linux/Unix terminal, Python 3.12+ with curses, [uv](https://docs.astral.sh/uv/), and Neovim.

```sh
git clone https://github.com/alessionegro99/himalaya-inbox.git
cd himalaya-inbox
install -m 755 himalaya-inbox "$HOME/.local/bin/himalaya-inbox"
himalaya-inbox
```

Ensure `~/.local/bin` is on your PATH. The script uses `uv` to run Python without installing packages into your system Python; it has no third-party Python dependencies.

It reads your existing configuration from `$HIMALAYA_CONFIG`, or `$XDG_CONFIG_HOME/himalaya/config.toml` (default `~/.config/himalaya/config.toml`). Use `--config /path/to/config.toml` for a different **single** configuration file. Split/merged config paths are not supported. `HIMALAYA_BIN` can select a different Himalaya executable.

Configure the `inbox`, `sent`, `drafts`, and `trash` mailbox aliases in your own Himalaya config. The wrapper reads all selectable mail folders except Drafts/Trash/Junk and their configured equivalents. The first load and refresh can take time on large accounts: headers are fetched, but there is no persistent mail index.

## Keys

| Key | Action |
| --- | --- |
| Arrows, `j`/`k`, mouse wheel | Scroll/select |
| Page Up/Down, `g`/`G` | Page or jump to beginning/end |
| Enter | Open a conversation, then a message |
| `r` / `R` | Reply / reply to all |
| `c` | Compose a new message; choose the sending account |
| `d` | Resume a local draft |
| `o` | Outbox: see countdowns and press `x` to cancel pending mail |
| `i` / `s` / `a` | Inbox / Sent / all indexed mail |
| `t` | Toggle conversation grouping |
| `/` | Filter the list or search an open message |
| Escape | Clear the list filter |
| `u` | Refresh mail, including sent replies |
| `q` | Go back or quit |

Within a conversation, messages are oldest first. `You (sent)` identifies sent copies. A reply from a conversation summary targets its latest received message; open the conversation and select another message to reply to that one instead.

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

Neovim runs without your plugins, modelines, swap, undo history, or shada to avoid executing anything in quoted email or copying drafts into editor caches.

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
- Browsing uses read-only/peek fetching and does not mark messages read.
- Message bodies are fetched only when opened or used to prepare a reply. Remote HTML resources are never fetched; terminal control characters are removed before display.
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

Tests cover ordering, paging, account/folder identity, conversation ancestry, MIME rendering, terminal controls, actual pseudo-terminal and Neovim navigation, reply recipients, confirmation, fake-clock delays, competing delivery claims, cancellation, and uncertain-delivery behavior. Sending is mocked; no real messages are sent.

## Scope and upstream credit

This is a small personal interface, not a full replacement for Thunderbird. Attachments are listed but not opened or composed; encryption/signing, remote draft synchronization, server-side deletion, and persistent/offline mail indexing are not implemented. Only one configuration file is supported.

The transport/backend work is provided by [Pimalaya's Himalaya](https://github.com/pimalaya/himalaya). Thread identifiers follow [RFC 5322 §3.6.4](https://www.rfc-editor.org/rfc/rfc5322#section-3.6.4); header-only fetching follows [IMAP RFC 3501](https://www.rfc-editor.org/rfc/rfc3501). Python's standard-library `email` and `curses` modules provide MIME handling and the terminal interface.

The wrapper is MIT-licensed; upstream projects retain their own licenses. Contributions that keep the code small, testable, and safe are welcome.
