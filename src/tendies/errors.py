"""Domain errors.

Services raise :class:`GameError` (or a subclass) with a player-facing message.
Cogs catch it and reply with ``str(err)`` — so the message text is the thing the
user sees in Discord, and services never touch Discord objects.
"""

from __future__ import annotations


class GameError(Exception):
    """A rule violation with a message safe to show the user."""


class NotFound(GameError):
    """A referenced ticker / job / user / offer doesn't exist."""


class NotAllowed(GameError):
    """The caller lacks permission for this action (e.g. not the owner)."""


class InsufficientFunds(GameError):
    """Not enough nuggies in the relevant account."""


class BadInput(GameError):
    """Malformed or out-of-range arguments."""
