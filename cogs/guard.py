"""NEWR Guardian: anti-nuke / anti-raid emergency bot for NEWR LABS.

SLASH COMMANDS ONLY: every user-triggered action is a discord
app_command under the single top-level ``/guard`` group (see the
command table in README.md). ``command_prefix`` is None in bot.py
and no prefix command or chat trigger exists anywhere in this bot.
The only non-command behaviour is passive: audit-log watching
(on_member_ban, on_member_remove for kicks, channel/role delete and
create, webhook create, and member joins while raidmode is on).
Those listeners never respond to a user message.

Detection (see README.md for the honest limits):
  * Each destructive audit event is attributed through the guild
    audit log (``guild.audit_logs``). Events with no matching audit
    entry within ~20 seconds are NOT counted, so a lagging or
    missing audit entry cannot cause a false lockdown.
  * Events by one non-exempt user are counted in a sliding window
    (default 3 actions in 60 seconds, configurable with
    /guard threshold). Crossing it triggers auto-lockdown.
  * Exempt by default: the guild owner, staff (see staff.py), the
    bot itself, and members on the /guard whitelist.
  * Guardian NEVER auto-bans or auto-kicks anyone. Auto-lockdown
    only locks channels, deletes invites, and logs. Staff decide
    on bans manually, because audit attribution can lag and a
    wrong auto-ban would be worse than a lockdown.

Lockdown: for every text channel, the @everyone overwrite is
changed to send_messages=False and create_instant_invite=False,
after saving the previous send_messages / create_instant_invite
tri-state for that channel to backups/lockdown-snapshot-<guild>.json
so /guard unlock can restore it. Active invites are also deleted.
Unlock restores the snapshot; if no snapshot exists (e.g. the bot
restarted mid-lockdown) it clears those two @everyone settings
back to inherit (None) instead of guessing.

Backup / restore (/guard backup, /guard restore):
  * Backup writes a JSON snapshot of roles, channels and permission
    overwrites to backups/backup-<timestamp>.json. Snapshot only,
    nothing is changed.
  * Restore is owner-only, requires confirm=True, and is
    NON-destructive: it only recreates roles/channels that are
    missing (matched by name). It never deletes anything and never
    changes an existing role or channel.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db
import ui
from staff import is_owner, is_staff, require_staff

DEFAULT_LOG_CHANNEL_ID = 1472425621866024960  # #logs in NEWR LABS
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")
AUDIT_WINDOW_SECONDS = 20
TRIGGER_COOLDOWN_SECONDS = 600
RAID_JOIN_BURST = 5
RAID_JOIN_WINDOW = 60

# In-memory detection state (per guild). Restart clears it; the
# SQLite incident log and the lockdown snapshot file survive.
_events: dict[int, deque] = {}
_joins: dict[int, deque] = {}
_seen_audit_ids: set[int] = set()
_last_trigger: dict[int, float] = {}


# --- Helpers ---------------------------------------------------------------

async def _reply(interaction: discord.Interaction, embed: discord.Embed,
                 ephemeral: bool = True) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except Exception:
        pass


async def _guild_only(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await _reply(interaction, ui.err("Server only", "This command only works inside a server."))
        return False
    return True


async def _resolve_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Log channel, in order: configured setting, the #logs default
    (1472425621866024960) if it exists in this guild, a text channel
    named 'logs', else None. No other channel ID is hardcoded."""
    configured = db.get_int_setting(guild.id, "log_channel", 0)
    if configured:
        channel = guild.get_channel(configured)
        if isinstance(channel, discord.TextChannel):
            return channel
    channel = guild.get_channel(DEFAULT_LOG_CHANNEL_ID)
    if isinstance(channel, discord.TextChannel):
        return channel
    for channel in guild.text_channels:
        if channel.name.lower() == "logs":
            return channel
    return None


def _snapshot_path(guild_id: int) -> str:
    return os.path.join(BACKUPS_DIR, f"lockdown-snapshot-{guild_id}.json")


def _tri_state(allow: discord.Permissions, deny: discord.Permissions,
               name: str) -> bool | None:
    if getattr(allow, name, False):
        return True
    if getattr(deny, name, False):
        return False
    return None


def _apply_tri(overwrite: discord.PermissionOverwrite, name: str,
               value: bool | None) -> None:
    setattr(overwrite, name, value)


async def _delete_invites(guild: discord.Guild) -> tuple[int, str | None]:
    """Delete all active invites. Returns (deleted_count, error_or_None)."""
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        return 0, "Missing Manage Guild permission to list invites."
    except Exception:
        return 0, "Could not list invites."
    deleted = 0
    for invite in invites:
        try:
            await invite.delete()
            deleted += 1
        except Exception:
            pass
    return deleted, None


async def do_lockdown(guild: discord.Guild) -> dict:
    """Lock all text channels for @everyone and delete invites.

    Returns {'locked': int, 'invites': int, 'errors': [str]}.
    Saves the pre-lockdown @everyone tri-state per channel first.
    """
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    snapshot: dict[str, dict] = {}
    locked = 0
    errors: list[str] = []
    everyone = guild.default_role
    for channel in guild.text_channels:
        overwrite = channel.overwrites_for(everyone)
        snapshot[str(channel.id)] = {
            "name": channel.name,
            "send_messages": _tri_state(overwrite.allow, overwrite.deny, "send_messages"),
            "create_instant_invite": _tri_state(
                overwrite.allow, overwrite.deny, "create_instant_invite"),
        }
        new_overwrite = discord.PermissionOverwrite.from_pair(overwrite.allow, overwrite.deny)
        new_overwrite.send_messages = False
        new_overwrite.create_instant_invite = False
        try:
            await channel.set_permissions(everyone, overwrite=new_overwrite)
            locked += 1
        except discord.Forbidden:
            errors.append(f"No permission to lock #{channel.name}.")
        except Exception:
            errors.append(f"Failed to lock #{channel.name}.")
    try:
        with open(_snapshot_path(guild.id), "w", encoding="utf-8") as fh:
            json.dump({"guild_id": guild.id, "channels": snapshot}, fh, indent=2)
    except Exception:
        errors.append("Could not save the lockdown snapshot file.")
    invites_deleted, invite_error = await _delete_invites(guild)
    if invite_error:
        errors.append(invite_error)
    db.set_lockdown_active(guild.id, True)
    return {"locked": locked, "invites": invites_deleted, "errors": errors}


async def do_unlock(guild: discord.Guild) -> dict:
    """Restore @everyone overwrites from the lockdown snapshot.

    Returns {'restored': int, 'errors': [str]}. Without a snapshot,
    the two lockdown settings are cleared back to inherit (None)
    rather than guessed.
    """
    snapshot: dict = {}
    path = _snapshot_path(guild.id)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                snapshot = json.load(fh).get("channels", {})
        except Exception:
            snapshot = {}
    restored = 0
    errors: list[str] = []
    everyone = guild.default_role
    for channel in guild.text_channels:
        saved = snapshot.get(str(channel.id))
        overwrite = channel.overwrites_for(everyone)
        new_overwrite = discord.PermissionOverwrite.from_pair(overwrite.allow, overwrite.deny)
        if saved is not None:
            _apply_tri(new_overwrite, "send_messages", saved.get("send_messages"))
            _apply_tri(new_overwrite, "create_instant_invite", saved.get("create_instant_invite"))
        else:
            new_overwrite.send_messages = None
            new_overwrite.create_instant_invite = None
        try:
            await channel.set_permissions(everyone, overwrite=new_overwrite)
            restored += 1
        except discord.Forbidden:
            errors.append(f"No permission to unlock #{channel.name}.")
        except Exception:
            errors.append(f"Failed to unlock #{channel.name}.")
    db.set_lockdown_active(guild.id, False)
    return {"restored": restored, "errors": errors,
            "had_snapshot": bool(snapshot)}


async def _post_log(guild: discord.Guild, embed: discord.Embed) -> bool:
    channel = await _resolve_log_channel(guild)
    if channel is None:
        return False
    try:
        await channel.send(embed=embed)
        return True
    except Exception:
        return False


async def _trigger_lockdown(guild: discord.Guild, attacker_id: int | None,
                            summary: str, count: int, reason: str) -> bool:
    """Run auto-lockdown once (cooldown-guarded). Returns True if it ran."""
    if db.lockdown_active(guild.id):
        return False
    now_ts = time.time()
    if now_ts - _last_trigger.get(guild.id, 0) < TRIGGER_COOLDOWN_SECONDS:
        return False
    _last_trigger[guild.id] = now_ts
    result = await do_lockdown(guild)
    db.log_incident(guild.id, attacker_id, summary, count, "auto-lockdown")
    attacker_text = f"<@{attacker_id}>" if attacker_id else "unknown (join burst)"
    await _post_log(guild, ui.warn(
        "🚨 Guardian auto-lockdown",
        f"{reason}\nAttacker: {attacker_text}\n"
        f"Channels locked: {result['locked']}, invites deleted: {result['invites']}.\n"
        "Staff: review the attacker, then run /guard unlock when safe. "
        "Guardian did not ban or kick anyone automatically."))
    return True


async def _is_exempt(guild: discord.Guild, user_id: int, bot: commands.Bot) -> bool:
    if bot.user is not None and user_id == bot.user.id:
        return True
    if guild.owner_id == user_id:
        return True
    if db.is_whitelisted(guild.id, user_id):
        return True
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            member = None
    return member is not None and is_staff(member)


async def _audit_actor(guild: discord.Guild, action: discord.AuditLogAction,
                       target_id: int | None) -> tuple[int | None, int | None]:
    """Most recent audit entry for action/target within the audit window.

    Returns (user_id, entry_id), or (None, None). Entries already
    seen are skipped so one audit entry is never counted twice.
    """
    try:
        async for entry in guild.audit_logs(limit=15, action=action):
            if entry.id in _seen_audit_ids:
                continue
            if entry.created_at is None:
                continue
            age = (datetime.now(timezone.utc) - entry.created_at.replace(
                tzinfo=timezone.utc)).total_seconds()
            if age > AUDIT_WINDOW_SECONDS:
                continue
            target = getattr(entry, "target", None)
            target_match = target_id is None or (
                target is not None and getattr(target, "id", None) == target_id)
            if not target_match:
                continue
            user = getattr(entry, "user", None)
            if user is None:
                continue
            if len(_seen_audit_ids) > 2000:
                _seen_audit_ids.clear()
            _seen_audit_ids.add(entry.id)
            return user.id, entry.id
    except discord.Forbidden:
        return None, None
    except Exception:
        return None, None
    return None, None


async def _record(bot: commands.Bot, guild: discord.Guild,
                  action: discord.AuditLogAction, kind: str,
                  target_id: int | None) -> None:
    """Attribute one destructive event and count it toward the threshold."""
    actor_id, _entry_id = await _audit_actor(guild, action, target_id)
    if actor_id is None:
        return  # unattributed events are never counted (see module docstring)
    if await _is_exempt(guild, actor_id, bot):
        return
    count, window = db.threshold(guild.id)
    now_ts = time.time()
    events = _events.setdefault(guild.id, deque())
    events.append((now_ts, actor_id, kind))
    while events and now_ts - events[0][0] > window:
        events.popleft()
    recent = [(ts, uid, k) for ts, uid, k in events if uid == actor_id]
    if len(recent) >= count:
        kinds = ", ".join(sorted({k for _ts, _uid, k in recent}))
        await _trigger_lockdown(
            guild, actor_id, f"{kinds} x{len(recent)}", len(recent),
            f"Mass-action threshold reached: {len(recent)} actions "
            f"({kinds}) by one user within {window}s (threshold: {count}).")


# --- Slash command group ---------------------------------------------------

guard = app_commands.Group(
    name="guard",
    description="NEWR Guardian anti-nuke and anti-raid controls.")


@guard.command(name="status", description="Show Guardian status (anyone).")
async def guard_status(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    guild = interaction.guild
    count, window = db.threshold(guild.id)
    log_channel = await _resolve_log_channel(guild)
    me = guild.me
    perms = me.guild_permissions if me else None
    armed = bool(perms and perms.view_audit_log and perms.manage_channels)
    await _reply(interaction, ui.info(
        "🛡️ Guardian status",
        f"Armed: {'Yes' if armed else 'No — Guardian needs View Audit Log and Manage Channels'}.\n"
        f"Lockdown: {'ACTIVE' if db.lockdown_active(guild.id) else 'not active'}.\n"
        f"Raidmode: {'ON' if db.raidmode_enabled(guild.id) else 'OFF'}.\n"
        f"Threshold: {count} actions in {window}s.\n"
        f"Whitelisted members: {len(db.whitelist_list(guild.id))} "
        "(owner and staff are exempt by default).\n"
        f"Log channel: {log_channel.mention if log_channel else 'not found — run /guard setup'}.\n"
        "Guardian never auto-bans or auto-kicks; it locks down and logs."))
    return


@guard.command(name="setup", description="Staff only: set the log channel and detection threshold.")
@app_commands.describe(
    log_channel="Channel for Guardian alerts (default: #logs if reachable).",
    threshold="Actions by one user that trigger lockdown (1-20, default 3).",
    window_seconds="Window for the threshold in seconds (10-600, default 60).")
async def guard_setup(interaction: discord.Interaction,
                      log_channel: discord.TextChannel | None = None,
                      threshold: int | None = None,
                      window_seconds: int | None = None) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    guild = interaction.guild
    if log_channel is not None:
        db.set_setting(guild.id, "log_channel", str(log_channel.id))
    if threshold is not None:
        db.set_setting(guild.id, "threshold_count", str(max(1, min(20, threshold))))
    if window_seconds is not None:
        db.set_setting(guild.id, "threshold_window", str(max(10, min(600, window_seconds))))
    count, window = db.threshold(guild.id)
    resolved = await _resolve_log_channel(guild)
    await _reply(interaction, ui.ok(
        "🛡️ Guardian setup saved",
        f"Log channel: {resolved.mention if resolved else 'not found'}.\n"
        f"Threshold: {count} actions in {window}s.\n"
        "Audit watching is always on; /guard setup only stores these settings."))
    return


@guard.command(name="lockdown", description="Staff only: lock all text channels for @everyone and delete invites.")
async def guard_lockdown(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    result = await do_lockdown(interaction.guild)
    db.log_incident(interaction.guild.id, interaction.user.id,
                     "manual lockdown", 0, "manual lockdown")
    note = ("\n" + "\n".join(result["errors"][:5])) if result["errors"] else ""
    await interaction.followup.send(embed=ui.warn(
        "🔒 Lockdown active",
        f"Locked {result['locked']} text channels for @everyone and "
        f"deleted {result['invites']} invites. Run /guard unlock when safe.{note}"),
        ephemeral=True)


@guard.command(name="unlock", description="Staff only: restore channel permissions saved before lockdown.")
async def guard_unlock(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    result = await do_unlock(interaction.guild)
    note = ("\n" + "\n".join(result["errors"][:5])) if result["errors"] else ""
    snapshot_note = ("Restored from the lockdown snapshot."
                     if result["had_snapshot"]
                     else "No snapshot found, so the two lockdown settings were cleared to inherit.")
    await interaction.followup.send(embed=ui.ok(
        "🔓 Lockdown lifted",
        f"Restored permissions on {result['restored']} text channels. {snapshot_note}{note}"),
        ephemeral=True)


@guard.command(name="panic", description="Staff only: immediate lockdown, delete invites, and turn raidmode on.")
async def guard_panic(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    result = await do_lockdown(interaction.guild)
    db.set_setting(interaction.guild.id, "raidmode", "1")
    db.log_incident(interaction.guild.id, interaction.user.id,
                     "panic", 0, "panic lockdown + raidmode on")
    await _post_log(interaction.guild, ui.warn(
        "🚨 Guardian panic",
        f"Panic triggered by <@{interaction.user.id}>. "
        f"Locked {result['locked']} channels, deleted {result['invites']} invites, "
        "raidmode turned ON."))
    await interaction.followup.send(embed=ui.warn(
        "🚨 Panic lockdown active",
        f"Locked {result['locked']} text channels, deleted {result['invites']} invites, "
        "and turned raidmode ON. Run /guard unlock and /guard raidmode off when safe."),
        ephemeral=True)


@guard.command(name="threshold", description="Staff only: set how many actions by one user trigger lockdown.")
@app_commands.describe(
    count="Actions by one user that trigger lockdown (1-20).",
    window_seconds="Window for the count in seconds (10-600).")
async def guard_threshold(interaction: discord.Interaction, count: int,
                          window_seconds: int = 60) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    db.set_setting(interaction.guild.id, "threshold_count", str(max(1, min(20, count))))
    db.set_setting(interaction.guild.id, "threshold_window",
                   str(max(10, min(600, window_seconds))))
    saved_count, saved_window = db.threshold(interaction.guild.id)
    await _reply(interaction, ui.ok(
        "🛡️ Threshold saved",
        f"Lockdown now triggers at {saved_count} actions by one user "
        f"within {saved_window}s."))
    return


@guard.command(name="raidmode", description="Staff only: turn join-burst raid protection on or off.")
@app_commands.describe(enabled="ON watches for join bursts (5 joins in 60s trigger lockdown).")
async def guard_raidmode(interaction: discord.Interaction, enabled: bool) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    db.set_setting(interaction.guild.id, "raidmode", "1" if enabled else "0")
    state = "ON" if enabled else "OFF"
    extra = (" While ON, 5 member joins within 60 seconds trigger lockdown."
             if enabled else "")
    await _reply(interaction, ui.ok(f"🛡️ Raidmode {state}", f"Raidmode {state}.{extra}"))
    return


@guard.command(name="log", description="Staff only: show recent Guardian incidents.")
async def guard_log(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    rows = db.recent_incidents(interaction.guild.id, limit=10)
    if not rows:
        await _reply(interaction, ui.info("🛡️ Guardian log", "No incidents recorded yet."))
        return
    lines = []
    for row in rows:
        attacker = f"<@{row['attacker_id']}>" if row["attacker_id"] else "unknown"
        lines.append(f"#{row['id']} {row['created_at'][:19]} — {row['summary']} "
                     f"— attacker {attacker} — {row['action']}")
    await _reply(interaction, ui.info("🛡️ Guardian log (latest 10)", "\n".join(lines)))
    return


@guard.command(name="backup", description="Staff only: snapshot roles, channels and permissions to a JSON file.")
async def guard_backup(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"backup-{stamp}.json"
    roles = []
    for role in guild.roles:
        roles.append({
            "id": role.id, "name": role.name, "colour": role.colour.value,
            "hoist": role.hoist, "mentionable": role.mentionable,
            "position": role.position, "permissions": role.permissions.value,
            "managed": role.managed,
        })
    channels = []
    for channel in guild.channels:
        overwrites = []
        for target, overwrite in channel.overwrites.items():
            overwrites.append({
                "id": target.id,
                "type": "role" if isinstance(target, discord.Role) else "member",
                "allow": overwrite.allow.value, "deny": overwrite.deny.value,
            })
        channels.append({
            "id": channel.id, "name": channel.name, "type": str(channel.type),
            "category_id": channel.category_id,
            "category_name": channel.category.name if channel.category else None,
            "topic": getattr(channel, "topic", None),
            "position": channel.position, "overwrites": overwrites,
        })
    data = {"guild_id": guild.id, "guild_name": guild.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "roles": roles, "channels": channels}
    path = os.path.join(BACKUPS_DIR, filename)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    await interaction.followup.send(embed=ui.ok(
        "💾 Backup saved",
        f"Saved {len(roles)} roles and {len(channels)} channels to `{filename}` "
        "in the bot's backups/ folder. Nothing was changed."), ephemeral=True)


@guard.command(name="restore", description="Owner only: recreate missing roles/channels from a backup (never deletes).")
@app_commands.describe(
    backup_file="Backup filename from /guard backup, e.g. backup-20261102-120000.json.",
    confirm="Must be True. Restore only recreates what is missing; it never deletes or edits existing roles/channels.")
async def guard_restore(interaction: discord.Interaction, backup_file: str,
                        confirm: bool) -> None:
    if not await _guild_only(interaction):
        return
    if not is_owner(interaction):
        await _reply(interaction, ui.err("Owner only", "Restore is owner-only."))
        return
    if not confirm:
        await _reply(interaction, ui.err(
            "Confirmation required",
            "Re-run /guard restore with confirm=True. Restore only recreates "
            "missing roles/channels; it never deletes or edits existing ones."))
        return
    safe_name = os.path.basename(backup_file.strip())
    path = os.path.join(BACKUPS_DIR, safe_name)
    if not os.path.exists(path):
        await _reply(interaction, ui.err("Backup not found", f"No backup file named `{safe_name}`."))
        return
    await interaction.response.defer(ephemeral=True)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        await interaction.followup.send(embed=ui.err("Bad backup", "That backup file could not be read."), ephemeral=True)
        return
    if data.get("guild_id") != interaction.guild.id:
        await interaction.followup.send(embed=ui.err(
            "Wrong server",
            "That backup is from a different server and was not applied."), ephemeral=True)
        return
    guild = interaction.guild
    created_roles = skipped_roles = 0
    existing_role_names = {role.name for role in guild.roles}
    role_by_name: dict[str, discord.Role] = {role.name: role for role in guild.roles}
    for role_data in data.get("roles", []):
        if role_data["name"] in existing_role_names or role_data.get("managed"):
            skipped_roles += 1
            continue
        try:
            new_role = await guild.create_role(
                name=role_data["name"],
                colour=discord.Colour(role_data.get("colour", 0)),
                hoist=bool(role_data.get("hoist", False)),
                mentionable=bool(role_data.get("mentionable", False)),
                permissions=discord.Permissions(role_data.get("permissions", 0)))
            role_by_name[new_role.name] = new_role
            created_roles += 1
        except Exception:
            skipped_roles += 1
    created_channels = skipped_channels = 0
    existing_names = {(ch.name, str(ch.type)) for ch in guild.channels}
    category_by_name: dict[str, discord.CategoryChannel] = {
        cat.name: cat for cat in guild.categories}
    for channel_data in data.get("channels", []):
        key = (channel_data["name"], channel_data["type"])
        if key in existing_names:
            skipped_channels += 1
            continue
        ctype = channel_data["type"]
        if "category" in ctype:
            try:
                new_cat = await guild.create_category(name=channel_data["name"])
                category_by_name[new_cat.name] = new_cat
                created_channels += 1
            except Exception:
                skipped_channels += 1
            continue
        if "text" not in ctype and "voice" not in ctype:
            skipped_channels += 1
            continue
        category = None
        if channel_data.get("category_name"):
            category = category_by_name.get(channel_data["category_name"])
        overwrites: dict = {}
        for ow in channel_data.get("overwrites", []):
            target = guild.get_role(ow["id"])
            if target is None:
                continue  # target role no longer exists; skip, do not guess
            overwrites[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"]))
        try:
            if "voice" in ctype:
                await guild.create_voice_channel(
                    name=channel_data["name"], category=category, overwrites=overwrites)
            else:
                await guild.create_text_channel(
                    name=channel_data["name"], category=category,
                    topic=channel_data.get("topic"), overwrites=overwrites)
            created_channels += 1
        except Exception:
            skipped_channels += 1
    db.log_incident(guild.id, interaction.user.id,
                     f"restore from {safe_name}", 0,
                     f"restore: {created_roles} roles, {created_channels} channels created")
    await interaction.followup.send(embed=ui.ok(
        "💾 Restore complete (non-destructive)",
        f"Created {created_roles} roles ({skipped_roles} skipped: already exist or managed) "
        f"and {created_channels} channels ({skipped_channels} skipped). "
        "Nothing existing was deleted or edited."), ephemeral=True)


# --- Whitelist subgroup --------------------------------------------------------

whitelist = app_commands.Group(
    name="whitelist", description="Manage members exempt from Guardian detection.",
    parent=guard)


@whitelist.command(name="add", description="Staff only: exempt a member from Guardian detection.")
@app_commands.describe(member="Member to whitelist. The owner and staff are already exempt.")
async def whitelist_add(interaction: discord.Interaction, member: discord.Member) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    added = db.whitelist_add(interaction.guild.id, member.id, interaction.user.id)
    if added:
        await _reply(interaction, ui.ok("✅ Whitelisted", f"{member.mention} is now exempt from Guardian detection."))
    else:
        await _reply(interaction, ui.info("Already whitelisted", f"{member.mention} was already whitelisted."))
    return


@whitelist.command(name="remove", description="Staff only: remove a member from the Guardian whitelist.")
async def whitelist_remove(interaction: discord.Interaction, member: discord.Member) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    removed = db.whitelist_remove(interaction.guild.id, member.id)
    if removed:
        await _reply(interaction, ui.ok("✅ Removed from whitelist", f"{member.mention} is no longer whitelisted."))
    else:
        await _reply(interaction, ui.info("Not whitelisted", f"{member.mention} was not on the whitelist."))
    return


@whitelist.command(name="list", description="Staff only: list whitelisted members.")
async def whitelist_list(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not await require_staff(interaction):
        return
    ids = db.whitelist_list(interaction.guild.id)
    if not ids:
        await _reply(interaction, ui.info(
            "🛡️ Whitelist",
            "No members whitelisted. The guild owner and staff are exempt by default."))
        return
    lines = [f"<@{uid}>" for uid in ids[:50]]
    if len(ids) > 50:
        lines.append(f"…and {len(ids) - 50} more.")
    await _reply(interaction, ui.info("🛡️ Whitelist", "\n".join(lines)))
    return


# --- Passive audit watcher (no user-triggered action) --------------------------

class Guardian(commands.Cog):
    """Passive listeners only; all commands live in the /guard group above."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        await _record(self.bot, guild, discord.AuditLogAction.ban, "ban", user.id)

    async def on_member_remove(self, member: discord.Member) -> None:
        # A leave is not a kick: only count it if the audit log shows
        # a kick for this member within the audit window.
        actor_id, _ = await _audit_actor(
            member.guild, discord.AuditLogAction.kick, member.id)
        if actor_id is None:
            return
        # _audit_actor already consumed the entry, so count directly.
        if await _is_exempt(member.guild, actor_id, self.bot):
            return
        count, window = db.threshold(member.guild.id)
        now_ts = time.time()
        events = _events.setdefault(member.guild.id, deque())
        events.append((now_ts, actor_id, "kick"))
        while events and now_ts - events[0][0] > window:
            events.popleft()
        recent = [e for e in events if e[1] == actor_id]
        if len(recent) >= count:
            kinds = ", ".join(sorted({k for _ts, _uid, k in recent}))
            await _trigger_lockdown(
                member.guild, actor_id, f"{kinds} x{len(recent)}", len(recent),
                f"Mass-action threshold reached: {len(recent)} actions "
                f"({kinds}) by one user within {window}s (threshold: {count}).")

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await _record(self.bot, channel.guild,
                      discord.AuditLogAction.channel_delete, "channel_delete", channel.id)

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await _record(self.bot, role.guild,
                      discord.AuditLogAction.role_delete, "role_delete", role.id)

    async def on_guild_role_create(self, role: discord.Role) -> None:
        await _record(self.bot, role.guild,
                      discord.AuditLogAction.role_create, "role_create", role.id)

    async def on_webhook_update(self, channel: discord.abc.GuildChannel) -> None:
        await _record(self.bot, channel.guild,
                      discord.AuditLogAction.webhook_create, "webhook_create", None)

    async def on_member_join(self, member: discord.Member) -> None:
        # Raidmode only: join bursts trigger lockdown. Outside raidmode,
        # joins are ignored entirely.
        if not db.raidmode_enabled(member.guild.id):
            return
        if member.bot:
            return
        now_ts = time.time()
        joins = _joins.setdefault(member.guild.id, deque())
        joins.append(now_ts)
        while joins and now_ts - joins[0] > RAID_JOIN_WINDOW:
            joins.popleft()
        if len(joins) >= RAID_JOIN_BURST:
            await _trigger_lockdown(
                member.guild, None, f"join burst x{len(joins)}", len(joins),
                f"Raidmode join burst: {len(joins)} members joined within "
                f"{RAID_JOIN_WINDOW}s.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Guardian(bot))
    # The /guard group (with its /guard whitelist subgroup) is added
    # to the tree here, not as a cog command, so the subgroup nesting
    # is explicit. bot.py guild-syncs the whole tree on ready.
    if bot.tree.get_command("guard") is None:
        bot.tree.add_command(guard)
