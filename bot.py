"""NEWR Guardian bot entrypoint. Config comes only from environment variables.

Required: DISCORD_TOKEN, GUILD_ID (guild commands sync target).
Optional: DB_PATH, LOG_CHANNEL_ID (defaults to #logs, 1472425621866024960,
          only if that channel exists in the guild; see cogs/guard.py
          for the name-based fallback).
Run:  pip install -r requirements.txt && python3 bot.py
       (or use ../ops/run-newr-guardian.sh)

SLASH COMMANDS ONLY: command_prefix is None and no prefix command,
no chat-triggered command, and no message-content command exists
anywhere in this bot. Every user-triggered action is a discord
app_command under the single /guard group, synced to the guild
below. The only non-command behaviour is passive audit-log
watching (see cogs/guard.py); it never responds to a user message.

Structure copied from newr-stats/bot.py and newr-social/bot.py:
discord.py 2.7, _proxy_kwargs() for VM egress, guild-only sync
on ready.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv  # optional, only if installed
    load_dotenv()
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

COGS = ["cogs.guard"]
# Guild-sync target. Defaults to NEWR LABS so the shipped placeholder
# .env (which contains only DISCORD_TOKEN=) still syncs guild-only;
# setting GUILD_ID in .env overrides the default.
DEFAULT_GUILD_ID = 1465422166790242377
try:
    GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else DEFAULT_GUILD_ID
except ValueError:
    GUILD_ID = DEFAULT_GUILD_ID


def _proxy_kwargs() -> dict:
    """VM egress goes through an HTTP proxy; direct TLS to Discord fails
    here (SSL WRONG_VERSION_NUMBER), while aiohttp with trust_env works.
    discord.py does not read proxy env vars itself, so pass them explicitly.
    Credentials stay in the environment, never in code or logs."""
    from urllib.parse import unquote, urlparse

    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not raw:
        return {}
    parsed = urlparse(raw)
    proxy = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 3128}"
    kwargs: dict = {"proxy": proxy}
    if parsed.username:
        import aiohttp

        kwargs["proxy_auth"] = aiohttp.BasicAuth(
            unquote(parsed.username),
            unquote(parsed.password or ""))
    return kwargs


class GuardianBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True  # privileged: staff/whitelist checks, join bursts
        intents.bans = True  # ban events for audit watching
        intents.webhooks = True  # webhook-create audit watching
        # message_content intentionally OFF: no chat trigger exists.
        # presences intentionally OFF (not needed for any Guardian feature).
        # command_prefix=None: slash commands ONLY. No prefix commands
        # can be registered or triggered in this bot.
        super().__init__(command_prefix=None, intents=intents, help_command=None,
                         **_proxy_kwargs())
        self.started_at = datetime.now(timezone.utc)
        self._synced = False

    async def setup_hook(self) -> None:
        db.init_db()
        for cog in COGS:
            await self.load_extension(cog)

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (id={self.user.id})")
        if self._synced:
            return
        self._synced = True
        # Always guild-only: GUILD_ID always has a value (env or default).
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash commands to guild {GUILD_ID}")

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        # A failed slash command must never crash the bot.
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Something went wrong. Please try again later.", ephemeral=True)
        except Exception:
            pass
        print(f"[error] slash command failed: {type(error).__name__}")


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. See .env.example.")
    asyncio.run(GuardianBot().start(token))


if __name__ == "__main__":
    main()
