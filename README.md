# NEWR Guardian

Anti-nuke / anti-raid bot for NEWR LABS. Most of the time it does
nothing except watch the audit log. If one non-exempt user performs a
bunch of destructive actions in a short window (default: 3 actions in
60 seconds), it locks the server down and logs what happened.

Everything lives under one `/guard` command group: status, setup,
lockdown, unlock, panic, whitelist, threshold, raidmode, log, backup,
restore.

Two things worth knowing before relying on it. It never auto-bans
anyone, staff decide bans by hand, because audit-log attribution can
lag. And it can't stop the server owner or anyone whose role sits
above Guardian's in the role list. Backups snapshot roles and
channels only, not messages or members.

Not live yet: no application, invite, or token exists for it. When
there is one, the token goes in `.env` and it runs with:

```bash
~/workspace/bots/ops/run-newr-guardian.sh
```

It needs View Audit Log, Manage Channels, Manage Guild, Manage Roles,
and Server Members Intent. Data lives in `data.db` (SQLite, local).
