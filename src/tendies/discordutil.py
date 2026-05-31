"""Discord helpers shared by every cog.

Cogs are thin: they parse arguments, open a DB session, call a service, and
render the result. These helpers cover the cross-cutting bits — permission
checks, reaction confirmations, interactive prompts, embeds — so each cog
doesn't reinvent them. Services never import this module; it's the Discord edge.
"""

from __future__ import annotations

import discord
from discord.ext import commands

NUGGIE_GOLD = 0xF1C40F


def mention(user_id: int) -> str:
    return f"<@{user_id}>"


def embed(title: str, description: str | None = None, *, color: int = NUGGIE_GOLD) -> discord.Embed:
    return discord.Embed(title=title, description=description or "", color=color)


def is_manager(ctx: commands.Context) -> bool:
    """A member is a Manager if they hold the configured role or have
    ``manage_guild`` (basic mod perms)."""
    author = ctx.author
    perms = getattr(author, "guild_permissions", None)
    if perms is not None and (perms.manage_guild or perms.administrator):
        return True
    role_name = ctx.bot.settings.manager_role
    roles = getattr(author, "roles", [])
    return any(getattr(r, "name", None) == role_name for r in roles)


async def require_manager(ctx: commands.Context) -> bool:
    """Reply and return ``False`` if the caller isn't a Manager."""
    if is_manager(ctx):
        return True
    await ctx.send(
        f"⛔ Manager only. You need the **{ctx.bot.settings.manager_role}** role "
        f"(or Manage Server permission)."
    )
    return False


async def confirm(ctx: commands.Context, text: str, *, timeout: int = 60) -> bool:
    """Post ``text``, add a ✅ reaction, and wait for the command author to react
    within ``timeout`` seconds. Returns whether they confirmed."""
    msg = await ctx.send(text)
    await msg.add_reaction("✅")

    def check(reaction: discord.Reaction, user: discord.abc.User) -> bool:
        return (
            user.id == ctx.author.id
            and reaction.message.id == msg.id
            and str(reaction.emoji) == "✅"
        )

    try:
        await ctx.bot.wait_for("reaction_add", timeout=timeout, check=check)
        return True
    except Exception:
        return False


async def prompt_text(ctx: commands.Context, text: str, *, timeout: int = 120) -> str | None:
    """Ask the author a question and wait for their next message in the same
    channel. Returns the message content, or ``None`` on timeout."""
    await ctx.send(text)

    def check(message: discord.Message) -> bool:
        return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id

    try:
        reply = await ctx.bot.wait_for("message", timeout=timeout, check=check)
        return reply.content
    except Exception:
        return None
