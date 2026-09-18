"""A no-token Discord harness for end-to-end cog tests.

The engine/service suite (everything else in ``tests/``) never imports
``discord`` and drives the game logic directly. This module is the opposite: it
exercises the **cog layer** — argument parsing, service wiring, embed rendering,
the reaction-confirm / text-prompt flows, Manager gating, and centralized error
rendering — by running the *real* cogs against a *real* in-memory database
through duck-typed ``ctx`` / ``bot`` fakes. No Discord gateway, no bot token.

What it faithfully reproduces from production (:mod:`tendies.bot`):

* ``before_invoke`` bootstrap — every invoke first ensures the guild's economy
  exists, exactly like ``TendiesBot.before_invoke_hook``.
* ``on_command_error`` — a ``GameError`` raised by a service is caught and
  rendered as ``"⚠️ {message}"``, exactly like ``TendiesBot.on_command_error``.
* ``discordutil.confirm`` / ``prompt_text`` — backed by a programmable
  ``bot.wait_for`` response queue, so the yes/no/timeout branches are testable.

What it does NOT cover (and why it's acceptable): discord.py's own argument
*conversion* (``job_id: int``, ``equity_pct: float``). We pass already-typed
values to the command callbacks; converting strings to ints is discord.py's
job, not ours. Our string parsers (``parse_amount``, ``_parse_found``,
``_parse_event_args``, ``_parse_job_fields``) take ``str`` and ARE exercised.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import deque

import discord
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from tendies import money
from tendies.cogs.admin import AdminCog
from tendies.cogs.capital import CapitalCog
from tendies.cogs.company import CompanyCog
from tendies.cogs.market import Market
from tendies.cogs.player import PlayerCog
from tendies.db import Database
from tendies.errors import GameError
from tendies.models import Company, Job, ServerState
from tendies.services import economy

GUILD_ID = 424242


def usage_hint(ctx) -> str:
    """The *real* :meth:`tendies.bot.TendiesBot.usage_hint`, called unbound.

    It only reads ``ctx.command``, ``ctx.clean_prefix`` and
    ``self.settings.command_prefix`` — all of which :class:`FakeBot` /
    :class:`FakeContext` provide — so the test exercises production code
    instead of a copy of it.
    """
    from tendies.bot import TendiesBot

    return TendiesBot.usage_hint(ctx.bot, ctx)


# ---------------------------------------------------------------------------
# Discord object fakes — duck-typed to exactly what the cogs touch.
# ---------------------------------------------------------------------------

class FakePerms:
    def __init__(self, *, manage_guild: bool = False, administrator: bool = False):
        self.manage_guild = manage_guild
        self.administrator = administrator


class FakeRole:
    def __init__(self, name: str):
        self.name = name


class FakeMember:
    def __init__(self, user_id: int, *, display_name: str | None = None,
                 manager: bool = False, roles: list[str] | None = None):
        self.id = user_id
        self.display_name = display_name or f"user{user_id}"
        self.guild_permissions = FakePerms(manage_guild=manager)
        self.roles = [FakeRole(r) for r in (roles or [])]


class FakeGuild:
    def __init__(self, guild_id: int):
        self.id = guild_id


class FakeChannel:
    def __init__(self, channel_id: int = 1):
        self.id = channel_id


class FakeMessage:
    """Returned by ``ctx.send``; ``confirm`` adds a reaction to it."""

    _counter = 1000

    def __init__(self, content: str | None = None, embed: discord.Embed | None = None,
                 *, can_react: bool = True):
        FakeMessage._counter += 1
        self.id = FakeMessage._counter
        self.content = content
        self.embed = embed
        self.reactions: list[str] = []
        self._can_react = can_react

    async def add_reaction(self, emoji: str) -> None:
        if not self._can_react:
            raise forbidden("Missing Permissions: Add Reactions")
        self.reactions.append(emoji)


def forbidden(text: str = "Missing Permissions") -> discord.Forbidden:
    """A ``discord.Forbidden`` built without a live HTTP response — what Discord
    raises when the bot lacks Embed Links / Add Reactions in a channel."""

    class _Resp:
        status = 403
        reason = "Forbidden"

    return discord.Forbidden(_Resp(), text)


class _FakeReaction:
    def __init__(self, message):
        self.message = message
        self.emoji = "✅"


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeSettings:
    def __init__(self, manager_role: str = "Tendies Manager", prefix: str = "$"):
        self.manager_role = manager_role
        self.command_prefix = prefix
        self.accelerated_mode = True


class FakeCommandContext:
    """What :meth:`FakeBot.get_context` returns — only ``.valid`` is read."""

    def __init__(self, command):
        self.command = command

    @property
    def valid(self) -> bool:
        return self.command is not None


class FakeBot:
    """Owns the real DB + settings and a programmable ``wait_for`` queue."""

    def __init__(self, db: Database):
        self.db = db
        self.settings = FakeSettings()
        self._responses: deque = deque()
        self._last_message: FakeMessage | None = None
        #: name/alias -> command, filled in by :class:`Harness`. Mirrors
        #: ``commands.Bot.all_commands`` closely enough for command resolution.
        self.all_commands: dict[str, object] = {}

    def queue(self, *responses) -> None:
        """Program upcoming ``wait_for`` results.

        For a ``reaction_add`` wait (``confirm``): a truthy value confirms, a
        falsy value simulates a timeout/decline. For a ``message`` wait
        (``prompt_text``): a string is the reply content, ``None`` is a timeout.
        """
        self._responses.extend(responses)

    async def wait_for(self, event: str, *, timeout=None, check=None):
        if not self._responses:
            raise asyncio.TimeoutError
        resp = self._responses.popleft()
        if event == "reaction_add":
            if resp:
                return _FakeReaction(self._last_message), _FakeUser(0)
            raise asyncio.TimeoutError
        if event == "message":
            if resp is None:
                raise asyncio.TimeoutError
            return FakeMessage(content=str(resp))
        raise asyncio.TimeoutError

    async def get_context(self, message) -> "FakeCommandContext":
        """The slice of ``commands.Bot.get_context`` that
        ``discordutil.prompt_text`` uses: does this message resolve to a real
        command (``.valid``), or does it merely start with the prefix?"""
        content = (getattr(message, "content", "") or "").strip()
        prefix = self.settings.command_prefix
        command = None
        if content.startswith(prefix):
            rest = content[len(prefix):].strip()
            name = rest.split(maxsplit=1)[0] if rest else ""
            command = self.all_commands.get(name)
        return FakeCommandContext(command)


class FakeContext:
    """The thin ``ctx`` surface the cogs read."""

    def __init__(self, bot: FakeBot, author: FakeMember, *, prefix: str = "$",
                 can_react: bool = True):
        self.bot = bot
        self.author = author
        self.guild = FakeGuild(GUILD_ID)
        self.channel = FakeChannel()
        self.prefix = prefix
        self.clean_prefix = prefix
        self.command = None  # set by Harness.invoke, like discord.py does
        #: False simulates a channel where the bot lacks Add Reactions.
        self.can_react = can_react
        self.sent: list[FakeMessage] = []

    async def send(self, content: str | None = None, *, embed: discord.Embed | None = None,
                   **kwargs):
        """``kwargs`` (e.g. ``allowed_mentions``) are accepted and recorded so
        cogs can opt a single send back in to mentions."""
        msg = FakeMessage(content=content, embed=embed, can_react=self.can_react)
        msg.kwargs = kwargs
        self.sent.append(msg)
        self.bot._last_message = msg
        return msg

    # -- assertion conveniences ------------------------------------------

    @property
    def last(self) -> FakeMessage:
        assert self.sent, "no message was sent"
        return self.sent[-1]

    def last_text(self) -> str:
        """The last reply as plain text — embed title+description+fields, or content."""
        m = self.last
        if m.embed is not None:
            parts = [m.embed.title or "", m.embed.description or ""]
            for f in m.embed.fields:
                parts.append(f"{f.name}\n{f.value}")
            return "\n".join(parts)
        return m.content or ""

    def all_text(self) -> str:
        """Every reply this context received, concatenated (for multi-send flows)."""
        out = []
        for m in self.sent:
            if m.embed is not None:
                out.append(m.embed.title or "")
                out.append(m.embed.description or "")
                out.extend(f"{f.name}\n{f.value}" for f in m.embed.fields)
            if m.content:
                out.append(m.content)
        return "\n".join(out)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------

class Harness:
    """A bootstrapped guild + the real cogs, dispatchable by command name."""

    def __init__(self, db: Database, bot: FakeBot):
        self.db = db
        self.bot = bot
        cogs = [
            PlayerCog(bot), CompanyCog(bot), CapitalCog(bot),
            Market(bot), AdminCog(bot),
        ]
        # cmd.cog is None until a cog is add_cog'd to a real bot, so we keep the
        # instance ourselves and pass it as ``self`` to the callback.
        self.registry: dict[str, tuple[object, object]] = {}
        for cog in cogs:
            for cmd in cog.get_commands():
                for name in (cmd.name, *cmd.aliases):
                    self.registry[name] = (cog, cmd)
        # Command resolution for prompt_text's "that was a command" check.
        bot.all_commands = {name: cmd for name, (_, cmd) in self.registry.items()}
        bot.all_commands.setdefault("help", object())

    @classmethod
    async def create(cls, weekday: str = "monday") -> "Harness":
        db = Database(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        await db.create_all()
        h = cls(db, FakeBot(db))
        await h._bootstrap(weekday)
        return h

    async def _bootstrap(self, weekday: str) -> None:
        async with self.db.session() as session:
            await economy.ensure_bootstrapped(session, GUILD_ID, dt.date.today())
            # Bootstrap anchors the game day to *today's* real date, which may be
            # any weekday; pin it to the requested one so tests are deterministic.
            state = (await session.execute(
                select(ServerState).where(ServerState.guild_id == GUILD_ID)
            )).scalars().one()
            await economy.set_day(session, state, weekday)

    async def close(self) -> None:
        await self.db.dispose()

    # -- contexts ---------------------------------------------------------

    def ctx(self, user_id: int, *, manager: bool = False,
            display_name: str | None = None, can_react: bool = True,
            roles: list[str] | None = None) -> FakeContext:
        member = FakeMember(user_id, manager=manager, display_name=display_name,
                            roles=roles)
        return FakeContext(self.bot, member, can_react=can_react)

    # -- dispatch ---------------------------------------------------------

    async def invoke(self, ctx: FakeContext, command: str, *args, **kwargs) -> FakeContext:
        """Run a command by name through the full bootstrap + error pipeline."""
        # Mirror TendiesBot.before_invoke_hook: bootstrap is idempotent.
        async with self.db.session() as session:
            await economy.ensure_bootstrapped(session, ctx.guild.id, dt.date.today())

        entry = self.registry.get(command)
        if entry is None:
            raise AssertionError(f"unknown command {command!r}")
        cog, cmd = entry
        ctx.command = cmd
        try:
            await cmd.callback(cog, ctx, *args, **kwargs)
        except GameError as err:  # mirror TendiesBot.on_command_error
            await ctx.send(f"⚠️ {err}")
        except TypeError as err:
            # discord.py would have raised MissingRequiredArgument before ever
            # reaching the callback; calling the callback short of an argument
            # raises TypeError instead. Render the same usage hint the bot does.
            if "required positional argument" not in str(err):
                raise
            await ctx.send(f"⚠️ {err}{usage_hint(ctx)}")
        return ctx

    async def invoke_help(self, ctx: FakeContext, *args) -> FakeContext:
        """Drive the real ``TendiesHelp`` (the bot's custom help command).

        ``invoke_help(ctx)`` → landing page; ``invoke_help(ctx, "found")`` →
        command detail (exercising the live fee-ladder path); an unknown name
        → the not-found error, exactly as the bot routes it.
        """
        from tendies.help_menu import TendiesHelp

        async with self.db.session() as session:
            await economy.ensure_bootstrapped(session, ctx.guild.id, dt.date.today())

        hc = TendiesHelp()
        hc.context = ctx
        hc.get_destination = lambda: ctx  # capture sends on the test context
        if not args:
            await hc.send_bot_help({})
            return ctx
        name = args[0]
        entry = self.registry.get(name)
        if entry is None:
            await hc.send_error_message(await hc.command_not_found(name))
        else:
            await hc.send_command_help(entry[1])
        return ctx

    # -- test-only state helpers (conserving; never via fake commands) ----

    async def fund_wallet(self, user_id: int, amount: int) -> None:
        """Move pool → wallet (a conserving transfer, like conftest.make_rich).

        Test scaffolding to reach a state quickly. It does NOT record income,
        so it does not make a user accredited — that path is owned by the
        engine suite's ``test_accredited_gate``.
        """
        async with self.db.session() as session:
            user = await money.get_or_create_user(session, GUILD_ID, user_id)
            state = (await session.execute(
                select(ServerState).where(ServerState.guild_id == GUILD_ID)
            )).scalars().one()
            state.pool_balance -= amount
            user.wallet += amount

    async def fund_treasury(self, ticker: str, amount: int) -> None:
        """Move pool → a company treasury (conserving) so dividends/M&A have funds."""
        async with self.db.session() as session:
            state = (await session.execute(
                select(ServerState).where(ServerState.guild_id == GUILD_ID)
            )).scalars().one()
            co = (await session.execute(
                select(Company).where(
                    Company.guild_id == GUILD_ID,
                    Company.ticker == ticker.upper(),
                )
            )).scalars().one()
            state.pool_balance -= amount
            co.treasury += amount

    async def first_state_job_id(self) -> int:
        async with self.db.session() as session:
            row = (await session.execute(
                select(Job.id)
                .join(Company, Job.company_id == Company.id)
                .where(Company.guild_id == GUILD_ID, Company.is_state == True)  # noqa: E712
                .order_by(Job.id.asc())
            )).scalars().first()
            assert row is not None, "no state job seeded"
            return int(row)

    async def open_job_id(self, ticker: str) -> int:
        async with self.db.session() as session:
            co = (await session.execute(
                select(Company).where(
                    Company.guild_id == GUILD_ID,
                    Company.ticker == ticker.upper(),
                )
            )).scalars().one()
            row = (await session.execute(
                select(Job.id).where(
                    Job.company_id == co.id, Job.open == True  # noqa: E712
                ).order_by(Job.id.desc())
            )).scalars().first()
            assert row is not None, f"no open job at {ticker}"
            return int(row)

    async def pool_balance(self) -> int:
        async with self.db.session() as session:
            state = (await session.execute(
                select(ServerState).where(ServerState.guild_id == GUILD_ID)
            )).scalars().one()
            return int(state.pool_balance)

    async def money_supply(self) -> int:
        async with self.db.session() as session:
            return await money.money_supply(session, GUILD_ID)
