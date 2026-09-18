"""Tendies — a per-server Discord economy bot. See DESIGN.md for the full spec."""

from __future__ import annotations

import logging
import sys


def main() -> None:
    """Console entry point (``tendies``). Boots the Discord bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # apscheduler logs "Running job ... executed successfully" on every tick
    # (every 60s in production), which drowns real signal in the journal.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    from .bot import build_bot

    bot = build_bot()
    if not bot.settings.discord_token:
        sys.exit(
            "No DISCORD_TOKEN set. Copy .env.example to .env and add your bot token."
        )
    bot.run(bot.settings.discord_token, log_handler=None)
