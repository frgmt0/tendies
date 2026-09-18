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

import asyncio
from contextlib import suppress
import logging
import uuid
import os
from pathlib import Path

import discord
from discord.ext import commands

from .config import Settings, get_settings
from .db import Database
from .errors import GameError
from .help_menu import TendiesHelp
from . import gameday
from .health import acquire_database_lease, heartbeat, write_heartbeat

log = logging.getLogger("tendies")

#: Cog modules loaded at startup. Each defines ``async def setup(bot)``.
COGS: tuple[str, ...] = (
    "tendies.cogs.player",
    "tendies.cogs.company",
    "tendies.cogs.capital",
    "tendies.cogs.market",
    "tendies.cogs.admin",
    "tendies.cogs.feedback",
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
            help_command=TendiesHelp(),
            case_insensitive=True,
        )
        self.db: Database = db or Database(self.settings.database_url)
        # Set by setup_hook; imported lazily to avoid a circular import.
        self.scheduler = None
        self._database_lease = None
        self._heartbeat_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        self._database_lease = acquire_database_lease(self.settings.database_url)
        try:
            await self.db.create_all()
            for ext in COGS:
                await self.load_extension(ext)
            # /bug is an application command. Registration is part of readiness;
            # startup must fail visibly if Discord rejects it.
            await self.tree.sync()
            from .scheduler import TickScheduler

            self.scheduler = TickScheduler(self)
            await self.scheduler.catch_up_all_guilds()
            self.scheduler.start()
            self._heartbeat_task = asyncio.create_task(
                heartbeat(self), name="tendies-heartbeat"
            )
        except Exception:
            if self._database_lease is not None:
                self._database_lease.close()
                self._database_lease = None
            raise
        log.info("Tendies ready: %d cogs loaded.", len(COGS))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s).", self.user, getattr(self.user, "id", "?"))

    async def before_invoke_hook(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            raise GameError("Tendies runs per-server — use these commands in a server channel.")
        from .services import economy

        today = gameday.local_date(self.settings.calendar_timezone)
        async with self.db.session() as session:
            await economy.ensure_bootstrapped(session, ctx.guild.id, today)
        # Synchronize before the command opens its business transaction.  This
        # closes the narrow race where the first post-midnight command could act
        # on yesterday's still-open calendar date.
        if self.scheduler is not None and not self.scheduler.accelerated:
            await self.scheduler.synchronize_guild(ctx.guild.id, target_day=today)

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
        reference = uuid.uuid4().hex[:8]
        log.exception("Command failure %s in %s", reference, ctx.command, exc_info=original)
        await ctx.send(f"💥 That command failed. Use `/bug` and include reference `{reference}` so we can investigate.")

    async def close(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        health_target = os.getenv("TENDIES_HEALTH_FILE")
        if health_target:
            try:
                write_heartbeat(Path(health_target), ready=False)
            except Exception:
                log.exception("Could not write shutdown heartbeat")
        try:
            await super().close()
            await self.db.dispose()
        finally:
            if self._database_lease is not None:
                self._database_lease.close()
                self._database_lease = None


def build_bot() -> TendiesBot:
    bot = TendiesBot()
    # Register the bootstrap hook (before_invoke must be a coroutine accepting ctx).
    bot.before_invoke(bot.before_invoke_hook)
    return bot
