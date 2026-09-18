"""Discord helpers shared by every cog.

Cogs are thin: they parse arguments, open a DB session, call a service, and
render the result. These helpers cover the cross-cutting bits — permission
checks, reaction confirmations, interactive prompts, embeds — so each cog
doesn't reinvent them. Services never import this module; it's the Discord edge.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

import discord
from discord.ext import commands

from .errors import BadInput
from .money import MAX_INT64

NUGGIE_GOLD = 0xF1C40F
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096

_AMOUNT_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}


def parse_amount(text: str) -> int:
    """Parse a nuggie amount into a positive integer. THE single parser shared by
    every command that takes a money argument, so ``$print`` and the capital
    commands accept exactly the same strings.

    Accepts a K/M/B/T suffix, underscore grouping, and well-formed thousands
    commas: ``5000``, ``5,000,000``, ``5_000_000``, ``1.5M``, ``200B``. Rejects
    mis-grouped commas (``1,5M`` does NOT become 15M), non-numeric input, and
    non-positive values — raising :class:`BadInput` so callers can let it
    propagate to the standard error renderer.
    """
    if text is None:
        raise BadInput("Expected an amount, e.g. `5000`, `1.5M`, or `200B`.")
    token = text.strip().upper()
    if not token:
        raise BadInput("Expected an amount, e.g. `5000`, `1.5M`, or `200B`.")

    multiplier = 1
    if token[-1] in _AMOUNT_SUFFIXES:
        multiplier = _AMOUNT_SUFFIXES[token[-1]]
        token = token[:-1].strip()

    token = token.replace("_", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+", token):  # well-formed thousands grouping
        token = token.replace(",", "")
    elif "," in token:
        raise BadInput(f"Couldn't read **{text}** as an amount — check the commas.")

    if not re.fullmatch(r"\d+(\.\d+)?", token):
        raise BadInput(
            f"Couldn't read **{text}** as an amount. Try `5000`, `1.5M`, or `200B`."
        )

    try:
        amount = int(
            (Decimal(token) * multiplier).to_integral_value(rounding=ROUND_HALF_EVEN)
        )
    except (InvalidOperation, ValueError, OverflowError):
        raise BadInput(f"Couldn't read **{text}** as a finite amount.")
    if amount <= 0:
        raise BadInput("Amount must be positive.")
    if amount > MAX_INT64:
        raise BadInput("Amount is too large to store safely.")
    return amount


def mention(user_id: int) -> str:
    return f"<@{user_id}>"


def embed(title: str, description: str | None = None, *, color: int = NUGGIE_GOLD) -> discord.Embed:
    safe_title = title[:EMBED_TITLE_LIMIT]
    safe_description = description or ""
    if len(safe_description) > EMBED_DESCRIPTION_LIMIT:
        safe_description = safe_description[: EMBED_DESCRIPTION_LIMIT - 24].rstrip()
        safe_description += "\n… additional rows omitted"
    return discord.Embed(title=safe_title, description=safe_description, color=color)


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
