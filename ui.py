"""Premium embed design system for NEWR Guardian.

Palette (single source of truth, matching the other new bots):
  blurple 0x5865F2 - primary / info
  success 0x57F287 - success
  error   0xED4245 - error
  warning 0xFEE75C - warning

Every embed built here carries the standard footer
"NEWR Guardian • NEWR LABS" and a timestamp (discord.utils.utcnow()).
Callers that need an extra honesty / explanation note pass it as
``footer``; it is merged in front of the standard footer so the
note is never dropped.

Titles use a single leading emoji. Descriptions stay clean, with
no ALL-CAPS shouting and no em-dash heavy text.
"""
from __future__ import annotations

import discord

BLURPLE = 0x5865F2
SUCCESS = 0x57F287
ERROR = 0xED4245
WARNING = 0xFEE75C

# Backwards-friendly aliases (same values, same meaning).
PRIMARY = BLURPLE
INFO = BLURPLE
GREEN = SUCCESS
RED = ERROR
YELLOW = WARNING

FOOTER = "NEWR Guardian • NEWR LABS"


def _footer_text(footer: str | None) -> str:
    if footer:
        footer = footer.strip()
        if footer and FOOTER not in footer:
            return f"{footer} • {FOOTER}"
        return footer
    return FOOTER


def panel(title: str, description: str | None = None,
          colour: int = BLURPLE) -> discord.Embed:
    """A plain panel embed: title, optional description, standard footer + timestamp."""
    embed = discord.Embed(title=title, description=description,
                          colour=discord.Colour(colour))
    embed.set_footer(text=FOOTER)
    embed.timestamp = discord.utils.utcnow()
    return embed


def make(title: str, description: str | None = None,
         colour: int = BLURPLE,
         fields: list[tuple[str, str, bool]] | None = None,
         thumbnail: str | None = None,
         footer: str | None = None) -> discord.Embed:
    """Build an embed with fields, optional thumbnail, merged footer + timestamp.

    ``fields`` is a list of (name, value, inline) tuples.
    ``footer`` is an extra note merged in front of the standard footer.
    """
    embed = discord.Embed(title=title, description=description,
                          colour=discord.Colour(colour))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text=_footer_text(footer))
    embed.timestamp = discord.utils.utcnow()
    return embed


def ok(title: str, description: str | None = None,
       fields: list[tuple[str, str, bool]] | None = None) -> discord.Embed:
    """Success embed (green) with standard footer + timestamp."""
    return make(title, description, colour=SUCCESS, fields=fields)


def err(title: str, description: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None) -> discord.Embed:
    """Error embed (red) with standard footer + timestamp."""
    return make(title, description, colour=ERROR, fields=fields)


def warn(title: str, description: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None) -> discord.Embed:
    """Warning embed (yellow) with standard footer + timestamp."""
    return make(title, description, colour=WARNING, fields=fields)


def info(title: str, description: str | None = None,
         fields: list[tuple[str, str, bool]] | None = None) -> discord.Embed:
    """Info embed (blurple) with standard footer + timestamp."""
    return make(title, description, colour=BLURPLE, fields=fields)
