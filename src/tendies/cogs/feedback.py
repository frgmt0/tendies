"""Player-reviewed GitHub issue reports; no GitHub credential on the bot."""
from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("tendies")

#: Stamped into every /bug issue body. .github/workflows/label-bug-reports.yml
#: matches on this exact string to auto-label the issue, so the two MUST stay
#: byte-identical; tests/test_feedback.py asserts that they do.
_MARKER = "Reported using Tendies /bug."


def release_id() -> str:
    value = os.getenv("TENDIES_RELEASE", "")
    if not value:
        path = Path(__file__).resolve().parents[3] / ".release-id"
        value = path.read_text().strip() if path.exists() else "development"
    return re.sub(r"[^a-zA-Z0-9._-]", "", value)[:64] or "development"


def repo_slug() -> str:
    """The owner/repository used for both the modal target and error fallbacks."""
    return os.getenv("GITHUB_REPOSITORY", "frgmt0/tendies")


def issue_url(repo: str, title: str, details: str, expected: str, release: str) -> str:
    """Bound the encoded URL so the ephemeral Discord message always fits.

    Truncation happens on the raw `details`/`expected` fields before the body
    (with its two `###` headers) is composed, so the headers are never cut.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("GITHUB_REPOSITORY must be owner/repository")
    title = title.strip()[:100] or "Tendies bug report"
    context = f"\n\nRelease: `{release}`\n{_MARKER}"
    base = f"https://github.com/{repo}/issues/new?"

    details = details.strip()
    expected = expected.strip()
    marker = "…(truncated)"

    def compose(details_text: str, expected_text: str) -> str:
        body = (
            f"### What happened / steps to reproduce\n{details_text}\n\n"
            f"### Expected behavior\n{expected_text}"
        )
        return base + urlencode({"title": title, "body": body + context})

    # Shrink `details` first, then `expected`, in small steps, on the raw
    # field text, so the surrounding "### ..." headers are composed only
    # after truncation and are never themselves mangled.
    truncated_details = False
    truncated_expected = False
    while len(compose(details, expected)) > 1700 and (details or expected):
        if details:
            details = details[:-16]
            truncated_details = True
        elif expected:
            expected = expected[:-16]
            truncated_expected = True

    if truncated_details:
        details = details.rstrip() + marker
    if truncated_expected:
        expected = expected.rstrip() + marker

    result = compose(details, expected)
    # Truncation can grow the body slightly (the marker text); if that
    # pushed it back over bound, fall back to hard-trimming without a marker.
    while len(result) > 1700 and (details or expected):
        if details:
            details = details[:-16]
        elif expected:
            expected = expected[:-16]
        result = compose(details, expected)
    return result


class BugReportModal(discord.ui.Modal, title="Report a Tendies bug"):
    summary = discord.ui.TextInput(label="Short summary", max_length=100)
    details = discord.ui.TextInput(
        label="What happened? How can we reproduce it?",
        style=discord.TextStyle.paragraph, max_length=600,
        placeholder="Which command did you use, and what went wrong?",
    )
    expected = discord.ui.TextInput(
        label="What did you expect?", style=discord.TextStyle.paragraph,
        max_length=300, required=False,
    )

    def __init__(self, repo: str):
        super().__init__(timeout=600)
        self.repo = repo

    async def on_submit(self, interaction: discord.Interaction) -> None:
        release = release_id()
        url = issue_url(self.repo, str(self.summary), str(self.details), str(self.expected), release)
        log.info(
            "Bug report drafted guild=%s user=%s release=%s summary=%r",
            interaction.guild_id, interaction.user.id, release, str(self.summary)[:100],
        )
        await interaction.response.send_message(
            f"[Review and submit your bug report on GitHub]({url})\n"
            "You'll need a GitHub account. The issue is public—leave out private information. "
            "Nothing has been posted yet; you can edit the report before submitting.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        reference = uuid.uuid4().hex[:8]
        log.exception("Bug report modal failure %s", reference, exc_info=error)
        message = (
            f"Could not prepare the report (reference `{reference}`). "
            f"Open one directly at https://github.com/{self.repo}/issues/new"
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)


class FeedbackCog(commands.Cog):
    @app_commands.command(name="bug", description="Quickly report a Tendies bug on GitHub")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
    async def bug(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BugReportModal(repo_slug()))

    @bug.error
    async def bug_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        message = "Please wait a moment before opening another report."
        if not isinstance(error, app_commands.CommandOnCooldown):
            message = f"Could not open the form. Report it at https://github.com/{repo_slug()}/issues."
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(FeedbackCog())
