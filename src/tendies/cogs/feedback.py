"""Player-reviewed GitHub issue reports; no GitHub credential on the bot."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import commands


def release_id() -> str:
    value = os.getenv("TENDIES_RELEASE", "")
    if not value:
        path = Path(__file__).resolve().parents[3] / ".release-id"
        value = path.read_text().strip() if path.exists() else "development"
    return re.sub(r"[^a-zA-Z0-9._-]", "", value)[:64] or "development"


def issue_url(repo: str, title: str, details: str, expected: str, release: str) -> str:
    """Bound the encoded URL so the ephemeral Discord message always fits."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("GITHUB_REPOSITORY must be owner/repository")
    title = title.strip()[:100] or "Tendies bug report"
    context = f"\n\nRelease: `{release}`\nReported using Tendies /bug."
    body = f"### What happened / steps to reproduce\n{details.strip()}\n\n### Expected behavior\n{expected.strip()}"
    base = f"https://github.com/{repo}/issues/new?"
    def encode(text):
        return base + urlencode({"title": title, "body": text + context})
    while len(encode(body)) > 1700:
        body = body[:-16]
        if not body:
            break
    return encode(body)


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
        url = issue_url(self.repo, str(self.summary), str(self.details), str(self.expected), release_id())
        await interaction.response.send_message(
            f"[Review and submit your bug report on GitHub]({url})\n"
            "You'll need a GitHub account. The issue is public—leave out private information. "
            "Nothing has been posted yet; you can edit the report before submitting.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )


class FeedbackCog(commands.Cog):
    @app_commands.command(name="bug", description="Quickly report a Tendies bug on GitHub")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
    async def bug(self, interaction: discord.Interaction) -> None:
        repo = os.getenv("GITHUB_REPOSITORY", "frgmt0/tendies")
        await interaction.response.send_modal(BugReportModal(repo))

    @bug.error
    async def bug_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        message = "Please wait a moment before opening another report."
        if not isinstance(error, app_commands.CommandOnCooldown):
            message = "Could not open the form. Report it at https://github.com/frgmt0/tendies/issues."
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(FeedbackCog())
