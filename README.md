# NEWR Guardian

Anti-nuke / anti-raid emergency bot for NEWR LABS (guild ID
`1465422166790242377`). Distinct from NEWR Support's daily
moderation: Guardian does nothing day to day except watch the
audit log. When one non-exempt user performs many destructive
actions in a short window, it locks the server down and logs
everything, so staff can respond before more damage is done.

**Code only, local only, not live yet.** No Discord application,
invite, or token exists for Guardian yet. The parent will supply a
real token later; this repo ships no token and no `.env` is
committed. The bot has never been started live.

## Slash commands only

**Every user-triggered action is a slash command** under the single
top-level `/guard` group (`discord.app_commands`, guild-synced to
NEWR LABS). There is no prefix command, no chat-triggered command,
and no feature that requires typing a prefix — `command_prefix`
is `None` in `bot.py`. The only non-command behaviour is passive
audit-log watching (see below), which never responds to a user.

| Slash command | Who | What it does |
|---|---|---|
| `/guard status` | Anyone in the server | Armed state (View Audit Log + Manage Channels present?), lockdown state, raidmode, threshold, whitelist count, log channel. |
| `/guard setup [log_channel] [threshold] [window_seconds]` | Staff only | Store the log channel and/or detection threshold. Audit watching itself is always on. |
| `/guard lockdown` | Staff only | Lock all text channels for @everyone (send_messages off, create_instant_invite off, previous settings saved) and delete all active invites. |
| `/guard unlock` | Staff only | Restore the channel permissions saved before lockdown. |
| `/guard panic` | Staff only | Immediate lockdown + delete invites + turn raidmode ON, and post to the log channel. |
| `/guard whitelist add <member>` | Staff only | Exempt a member from detection. |
| `/guard whitelist remove <member>` | Staff only | Remove a member from the whitelist. |
| `/guard whitelist list` | Staff only | List whitelisted members. |
| `/guard threshold <count> [window_seconds]` | Staff only | Set the mass-action threshold (count 1–20, window 10–600s). Default: 3 actions in 60s. |
| `/guard raidmode <enabled>` | Staff only | Turn join-burst protection on/off. While ON, 5 member joins within 60 seconds trigger lockdown. |
| `/guard log` | Staff only | Show the latest 10 incidents from the local database. |
| `/guard backup` | Staff only | Snapshot roles, channels and permission overwrites to `backups/backup-<timestamp>.json`. Changes nothing. |
| `/guard restore <backup_file> <confirm>` | Guild owner only | Recreate missing roles/channels from a backup. Requires `confirm=True`. Non-destructive: see below. |

Total: **1 top-level slash command** (`/guard`), with **13 leaf
commands** underneath it (10 direct subcommands + 3 under the
`/guard whitelist` subgroup), under the 100-command limit.

**Staff gate:** guild owner, or a member with the **Administrator**
or **Manage Guild** permission, or a role whose name matches a known
staff name (Founder, Executive, Admin, Moderator, Staff, Support,
etc., checked by name at runtime, never by a hardcoded role ID).
Non-staff users get an ephemeral "Staff only" reply. `/guard status`
is the one exception: anyone may use it. All commands are
guild-only; used in DMs they reply that they must be used inside a
server.

## Audit-log watching — what it does and does not do

- **Watches:** mass ban, mass kick (a member leave is only counted
  if the audit log shows a kick for that member), channel delete,
  role delete, role create, and webhook create — all attributed
  through `guild.audit_logs` within ~20 seconds of the event.
- **Counts:** destructive actions by one non-exempt user inside the
  configured window. Default: **3 actions in 60 seconds** triggers
  auto-lockdown. The window and count are configurable with
  `/guard threshold` (or `/guard setup`).
- **Exempt by default:** the guild owner, staff (see above), the bot
  itself, and members on the `/guard whitelist`. Whitelist changes
  are staff-only.
- **Auto-lockdown does:** lock all text channels for @everyone,
  delete all active invites, write an incident to the local
  database, and post an alert embed to the log channel.
- **Auto-lockdown does NOT:** ban, kick, strip roles, or delete
  anything. Staff decide on bans manually, because audit
  attribution can lag and a wrong auto-ban would be worse than a
  lockdown.

**Log channel resolution** (no channel ID is hardcoded except the
one given for this task): the per-guild setting from `/guard setup`
first, then `#logs` (`1472425621866024960`) if that channel exists
in this guild, then a text channel named `logs`, else no channel
post (the incident is still recorded in the database and shown by
`/guard log`).

## Raidmode

`/guard raidmode` on enables join-burst watching only: 5 member
joins within 60 seconds trigger lockdown. Bots joining are ignored.
Raidmode does not change Discord's verification level, slowmode, or
any other guild setting automatically — staff set those manually if
wanted. `/guard panic` also turns raidmode on.

## Backup and restore

- `/guard backup` writes `backups/backup-<UTC timestamp>.json`
  containing every role (name, colour, hoist, mentionable, position,
  permission value, managed flag) and every channel (name, type,
  category, topic, position, permission overwrites by target ID and
  type). It changes nothing in the server.
- `/guard restore` is **owner-only** and requires `confirm=True`.
  It is **non-destructive by design**: it only recreates roles and
  channels that are missing (matched by name). Managed roles are
  skipped, existing roles/channels are never edited, nothing is
  ever deleted, and overwrites whose target role no longer exists
  are skipped rather than guessed. A backup from a different server
  is refused. Channel positions and role positions from the backup
  are not re-applied (Discord reorders on creation); staff can
  reorder manually after a restore.

## Honest limits — read before relying on Guardian

- **Role hierarchy is everything.** Anti-nuke only works if
  Guardian's role is **above the attacker's role** in the role list.
  It cannot stop, undo, or override actions by the server owner or
  by anyone whose role is above Guardian's, and it cannot restore
  permissions on channels/roles it cannot manage.
- **Audit-log attribution can lag.** Events with no matching audit
  entry within ~20 seconds are not counted, so a very fast attack
  that outruns the audit log, or an attack while Guardian lacks
  the **View Audit Log** permission, may be missed or counted late.
  Guardian also only sees events while it is online; actions taken
  while it is down are not backfilled (there is no audit-history
  scan on startup).
- **Lockdown scope.** Lockdown changes @everyone overwrites on text
  channels only. It does not lock voice channels, change role
  permissions, or remove an attacker's roles. Unlock restores the
  two changed settings from the lockdown snapshot; if the bot
  restarted mid-lockdown and no snapshot exists, unlock clears
  those two @everyone settings back to inherit (None) instead of
  guessing.
- **Detection state is in-memory.** The sliding-window counters
  reset on restart; the SQLite incident log and the lockdown
  snapshot file survive restarts.
- **Backup is a snapshot, not a full restore.** Messages, members,
  bans, webhooks, emojis, stickers, and integrations are not in a
  backup, and restore recreates only missing roles/channels (see
  above). It is a recovery aid, not a one-click server rebuild.
- **Permissions needed:** View Audit Log (detection), Manage
  Channels (lockdown/unlock, backup channel reads), Manage Guild
  (invite deletion), Manage Roles (restore), Send Messages + Embed
  Links (log channel posts). Without View Audit Log or Manage
  Channels, `/guard status` reports the bot as not armed.

## Setup

Prerequisites: Python 3.11+, a Discord application + bot for NEWR
Guardian (not created by this repo — the parent supplies this
later), and these intents enabled in the Developer Portal:
**Server Members Intent** (staff/whitelist checks, join bursts).
Message Content Intent is **not** needed and is off.

1. Put the bot token in `.env` (never commit `.env`). Copy
   `.env.example` to `.env` and fill in `DISCORD_TOKEN`. `GUILD_ID`
   defaults to NEWR LABS (`1465422166790242377`) in `bot.py` when
   unset.
2. Invite the bot to NEWR LABS with at least: View Audit Log, Manage
   Channels, Manage Guild, Manage Roles, Send Messages, Embed Links.
   **Place Guardian's role above the roles it must be able to
   restrain** (see Honest limits above); it cannot act on higher
   roles or the owner.
3. Run it (foreground, log to `~/workspace/bots/logs/newr-guardian.log`):
   ```bash
   ~/workspace/bots/ops/run-newr-guardian.sh
   ```
   The script creates `~/workspace/bots/newr-guardian/.venv` on
   first run, installs `requirements.txt`, refuses to start with an
   empty/placeholder token, then runs `python -u bot.py` from the
   bot directory. On ready it logs `Logged in as ...` and
   `Synced 1 slash commands to guild 1465422166790242377` (1
   top-level command; the 13 leaf commands live under it). Slash
   commands may take a few minutes to appear in the client after
   first sync.
4. VM egress note (same as NEWR Support/NEWR AI/Stats/Social):
   direct TLS to Discord fails from this VM, so `bot.py` passes the
   proxy from `HTTPS_PROXY` explicitly via `_proxy_kwargs()`. If the
   bot runs elsewhere without a proxy set, that function returns
   `{}` and the bot connects directly.
5. After first login, run `/guard setup` (staff) to confirm the log
   channel, and `/guard status` to confirm the bot reports armed.

## Files

- `bot.py` — entrypoint, intents, proxy kwargs, guild-only slash sync.
- `db.py` — SQLite: settings, whitelist, incidents.
- `staff.py` — staff gate (owner / Administrator / Manage Guild /
  staff role name) and owner check for restore.
- `ui.py` — premium embeds: blurple 0x5865F2, success 0x57F287,
  error 0xED4245, warning 0xFEE75C; every standardized embed carries
  the footer "NEWR Guardian • NEWR LABS" + timestamp.
- `cogs/guard.py` — the `/guard` group (10 subcommands + the
  `/guard whitelist` subgroup with 3 subcommands), lockdown/unlock,
  backup/restore, and the passive audit watcher.
- `requirements.txt` — discord.py 2.7, python-dotenv, aiohttp.
- `.env.example`, `.env` — token config (`.env` is gitignored and
  ships empty, chmod 600, when created).
- `backups/` — JSON backups and lockdown snapshots (gitignored).
