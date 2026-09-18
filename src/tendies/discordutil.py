"""Discord helpers shared by every cog.

Cogs are thin: they parse arguments, open a DB session, call a service, and
render the result. These helpers cover the cross-cutting bits — permission
checks, reaction confirmations, interactive prompts, embeds — so each cog
doesn't reinvent them. Services never import this module; it's the Discord edge.
"""

from __future__ import annotations

import asyncio
import math
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


def parse_percent(raw, *, label: str = "percentage") -> float:
    """Parse a percentage into **percent units** (``"15"`` and ``"15%"`` → ``15.0``).

    THE single percent parser, shared by ``$taxrate``, ``$raise``, and
    ``$promote`` so all three accept exactly the same strings. The trailing
    ``%`` is optional and purely cosmetic: **a bare number is always a
    percent**, so ``1`` → 1%, ``0.5`` → 0.5%, and ``2.5`` → 2.5%. (An earlier
    version treated values ≤ 1 as an already-normalized fraction, which made
    ``$taxrate 1`` silently mean 100% — never again.)

    Callers that need a fraction divide by 100 themselves. Already-numeric
    input passes through unchanged, so a discord.py-converted float still works.
    """
    if isinstance(raw, bool):  # bool is an int subclass; reject it explicitly
        raise BadInput(f"Couldn't read that as a {label}. Try `15` or `15%`.")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        if raw is None:
            raise BadInput(f"Give me a {label}, e.g. `15` or `15%`.")
        token = str(raw).strip().rstrip("%").strip().replace("_", "").replace(",", "")
        if not token:
            raise BadInput(f"Give me a {label}, e.g. `15` or `15%`.")
        try:
            value = float(token)
        except ValueError:
            raise BadInput(
                f"Couldn't read **{raw}** as a {label}. Try `15` or `15%`."
            )
    if not math.isfinite(value):
        raise BadInput(f"The {label} must be a finite number.")
    if value < 0:
        raise BadInput(f"The {label} can't be negative.")
    return value


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
    role_name = (ctx.bot.settings.manager_role or "").strip().casefold()
    roles = getattr(author, "roles", [])
    return any(
        (getattr(r, "name", None) or "").strip().casefold() == role_name for r in roles
    )


async def require_manager(ctx: commands.Context) -> bool:
    """Reply and return ``False`` if the caller isn't a Manager."""
    if is_manager(ctx):
        return True
    await ctx.send(
        f"⛔ Manager only. You need the **{ctx.bot.settings.manager_role}** role "
        f"(or Manage Server permission)."
    )
    return False


_YES = ("yes", "y", "confirm", "ok", "okay", "✅")
_NO = ("no", "n", "cancel", "stop", "abort")


def command_prefixes(ctx: commands.Context) -> tuple[str, ...]:
    """Every string that marks a message as "another command, not an answer"."""
    candidates = [getattr(ctx, "prefix", None), getattr(ctx, "clean_prefix", None)]
    configured = getattr(getattr(ctx.bot, "settings", None), "command_prefix", None)
    if isinstance(configured, str):
        candidates.append(configured)
    elif isinstance(configured, (list, tuple)):
        candidates.extend(p for p in configured if isinstance(p, str))
    return tuple({p for p in candidates if p})


async def confirm(ctx: commands.Context, text: str, *, timeout: int = 60) -> bool:
    """Post ``text`` and wait for the command author to confirm within
    ``timeout`` seconds.

    The happy path is a ✅ reaction. If the bot lacks **Add Reactions** in this
    channel, ``add_reaction`` raises :class:`discord.Forbidden` — that used to
    escape as a generic "that command failed", making ``$quit``/``$print``
    unusable. Instead we fall back to a typed confirmation and wait for the
    author to reply ``yes``/``no``.
    """
    msg = await ctx.send(text)
    try:
        await msg.add_reaction("✅")
    except discord.Forbidden:
        return await _typed_confirm(ctx, timeout=timeout)

    def check(reaction: discord.Reaction, user: discord.abc.User) -> bool:
        return (
            user.id == ctx.author.id
            and reaction.message.id == msg.id
            and str(reaction.emoji) == "✅"
        )

    try:
        await ctx.bot.wait_for("reaction_add", timeout=timeout, check=check)
        return True
    except asyncio.TimeoutError:
        return False


async def _typed_confirm(ctx: commands.Context, *, timeout: int) -> bool:
    """Reaction-free confirmation: reply `yes` or `no` in the channel."""
    await ctx.send(
        "I can't add reactions in this channel (I need the **Add Reactions** "
        f"permission). Reply `yes` within {timeout}s to confirm, or `no` to cancel."
    )

    def check(message: discord.Message) -> bool:
        return (
            message.author.id == ctx.author.id
            and message.channel.id == ctx.channel.id
        )

    deadline = _now() + timeout
    while True:
        remaining = deadline - _now()
        if remaining <= 0:
            return False
        try:
            reply = await ctx.bot.wait_for("message", timeout=remaining, check=check)
        except asyncio.TimeoutError:
            return False
        answer = (reply.content or "").strip().casefold()
        if answer in _YES:
            return True
        if answer in _NO:
            return False
        # Anything else isn't an answer — keep waiting until the deadline.


async def prompt_text(ctx: commands.Context, text: str, *, timeout: int = 120) -> str | None:
    """Ask the author a question and wait for their next message in the same
    channel. Returns the message content, or ``None`` on timeout.

    Replies that resolve to a *real* command are ignored (and we keep waiting):
    someone typing ``$help`` mid-flow means "I'm lost", not "this is my job
    title". We resolve rather than just prefix-match, because a legitimate
    answer can start with the prefix — a job description of ``$5K/day plus
    equity`` is not a command, and used to hang the whole flow.
    """
    await ctx.send(text)

    def check(message: discord.Message) -> bool:
        return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id

    deadline = _now() + timeout
    while True:
        remaining = deadline - _now()
        if remaining <= 0:
            return None
        try:
            reply = await ctx.bot.wait_for("message", timeout=remaining, check=check)
        except asyncio.TimeoutError:
            return None
        content = reply.content or ""
        if await _is_command(ctx, reply):
            await ctx.send(
                "That looked like a command; still waiting for your reply "
                "(or `cancel`)."
            )
            continue
        return content


async def _is_command(ctx: commands.Context, message: discord.Message) -> bool:
    """Does ``message`` resolve to an actual command of this bot?

    Prefers discord.py's own resolution (``bot.get_context(...).valid``); falls
    back to prefix + a registered command/alias name when the bot doesn't offer
    it. A prefix alone is never enough.
    """
    content = (message.content or "").strip()
    prefixes = command_prefixes(ctx)
    if not prefixes or not content.startswith(prefixes):
        return False

    get_context = getattr(ctx.bot, "get_context", None)
    if get_context is not None:
        try:
            probe = await get_context(message)
        except Exception:  # pragma: no cover - defensive; fall through below
            probe = None
        if probe is not None:
            return bool(getattr(probe, "valid", False))

    all_commands = getattr(ctx.bot, "all_commands", None) or {}
    for prefix in prefixes:
        if not content.startswith(prefix):
            continue
        rest = content[len(prefix):].strip()
        name = rest.split(maxsplit=1)[0] if rest else ""
        if name and name in all_commands:
            return True
    return False


def _now() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:  # pragma: no cover - only outside an event loop
        import time

        return time.monotonic()
