"""The Discord bot core.

``TendiesBot`` owns the database handle, runtime settings, and the tick
scheduler, and exposes them to cogs as ``bot.db`` / ``bot.settings`` /
``bot.scheduler``. Two cross-cutting behaviors live here so cogs stay thin:

* **Bootstrap on demand** — a ``before_invoke`` hook guarantees a guild's economy
  (server_state + seeded state companies) exists before any command runs, so
  cogs can assume ``get_state`` returns a row.
* **Centralized error display** — services raise :class:`~tendies.errors.GameError`
  with a player-facing message; ``on_command_error`` catches it (even when
  discord.py wraps it in ``CommandInvokeError``) and replies with the text.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands

from .config import Settings, get_settings
from .db import Database
from .errors import GameError

log = logging.getLogger("tendies")

#: Cog modules loaded at startup. Each defines ``async def setup(bot)``.
COGS: tuple[str, ...] = (
    "tendies.cogs.player",
    "tendies.cogs.company",
    "tendies.cogs.capital",
    "tendies.cogs.market",
    "tendies.cogs.admin",
)


def _intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True  # prefix commands need to read message text
    intents.members = True  # resolve member roles/mentions for hiring, managers
    return intents


class TendiesBot(commands.Bot):
    def __init__(self, settings: Settings | None = None, *, db: Database | None = None):
        self.settings: Settings = settings or get_settings()
        super().__init__(
            command_prefix=self.settings.command_prefix,
            intents=_intents(),
            help_command=commands.DefaultHelpCommand(no_category="Commands"),
            case_insensitive=True,
        )
        self.db: Database = db or Database(self.settings.database_url)
        # Set by setup_hook; imported lazily to avoid a circular import.
        self.scheduler = None

    async def setup_hook(self) -> None:
        await self.db.create_all()
        for ext in COGS:
            await self.load_extension(ext)
        from .scheduler import TickScheduler

        self.scheduler = TickScheduler(self)
        self.scheduler.start()
        log.info("Tendies ready: %d cogs loaded.", len(COGS))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s).", self.user, getattr(self.user, "id", "?"))

    async def before_invoke_hook(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            raise GameError("Tendies runs per-server — use these commands in a server channel.")
        from .services import economy

        async with self.db.session() as session:
            await economy.ensure_bootstrapped(session, ctx.guild.id, dt.date.today())

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, GameError):
            await ctx.send(f"⚠️ {original}")
            return
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.UserInputError)):
            await ctx.send(f"⚠️ {error}")
            return
        log.exception("Unhandled command error in %s", ctx.command, exc_info=original)
        await ctx.send("💥 Something broke on our end. The Managers have been notified.")

    async def close(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown()
        await super().close()
        await self.db.dispose()


def build_bot() -> TendiesBot:
    bot = TendiesBot()
    # Register the bootstrap hook (before_invoke must be a coroutine accepting ctx).
    bot.before_invoke(bot.before_invoke_hook)
    return bot
