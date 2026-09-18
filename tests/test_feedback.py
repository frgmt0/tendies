from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from tendies.cogs.feedback import BugReportModal, FeedbackCog, issue_url, repo_slug


def test_issue_url_preserves_special_characters_and_release():
    url = issue_url('frgmt0/tendies', 'tax & jobs?', '$clockin failed #1', 'Get paid', 'abc123')
    query = parse_qs(urlparse(url).query)
    assert query['title'] == ['tax & jobs?']
    assert '$clockin failed #1' in query['body'][0]
    assert 'abc123' in query['body'][0]


def test_unicode_report_fits_discord_and_rejects_untrusted_repository():
    url = issue_url('frgmt0/tendies', '🍗' * 100, '🍗' * 600, '🍗' * 300, 'abc123')
    assert len(url) <= 1700
    with pytest.raises(ValueError):
        issue_url('evil.test/?redirect=x', 'test', '', '', '')


async def test_slash_bug_opens_modal_without_publishing():
    interaction = SimpleNamespace(response=SimpleNamespace(send_modal=AsyncMock()))
    cog = FeedbackCog()
    await FeedbackCog.bug.callback(cog, interaction)
    modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, BugReportModal)
    assert modal.repo == 'frgmt0/tendies'


def test_truncation_preserves_headers_and_bound_with_multibyte_input():
    details = ('🍗詳細' * 200)[:600]  # matches the modal's 600-char field limit
    expected = ('期待🍗' * 100)[:300]  # matches the modal's 300-char field limit
    url = issue_url('frgmt0/tendies', 'title', details, expected, 'abc123')
    assert len(url) <= 1700
    query = parse_qs(urlparse(url).query)
    body = query['body'][0]
    assert '### What happened / steps to reproduce' in body
    assert '### Expected behavior' in body
    # Headers must appear in order and only once each.
    assert body.count('### What happened / steps to reproduce') == 1
    assert body.count('### Expected behavior') == 1
    assert body.index('### What happened / steps to reproduce') < body.index('### Expected behavior')


def test_repo_slug_default_and_env_override(monkeypatch):
    monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)
    assert repo_slug() == 'frgmt0/tendies'
    monkeypatch.setenv('GITHUB_REPOSITORY', 'someone/else')
    assert repo_slug() == 'someone/else'


def test_issue_url_raises_on_invalid_repo():
    with pytest.raises(ValueError):
        issue_url('not-a-valid-repo', 'title', 'details', 'expected', 'abc123')


async def test_modal_on_error_sends_ephemeral_fallback():
    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
    interaction = SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))
    modal = BugReportModal('frgmt0/tendies')
    await modal.on_error(interaction, RuntimeError('boom'))
    response.send_message.assert_called_once()
    args, kwargs = response.send_message.call_args
    assert 'github.com/frgmt0/tendies/issues/new' in args[0]
    assert kwargs.get('ephemeral') is True


async def test_modal_on_error_uses_followup_when_response_done():
    response = SimpleNamespace(is_done=lambda: True, send_message=AsyncMock())
    interaction = SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))
    modal = BugReportModal('frgmt0/tendies')
    await modal.on_error(interaction, RuntimeError('boom'))
    response.send_message.assert_not_called()
    interaction.followup.send.assert_called_once()
    args, kwargs = interaction.followup.send.call_args
    assert 'github.com/frgmt0/tendies/issues/new' in args[0]
    assert kwargs.get('ephemeral') is True


def test_bug_label_workflow_matches_the_issue_body_marker():
    """The labeler greps issue bodies for this exact string. If ``_MARKER``
    drifts, /bug reports silently stop being labeled — nothing else would
    notice, so assert the two copies agree."""
    from pathlib import Path

    from tendies.cogs.feedback import _MARKER

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github" / "workflows" / "label-bug-reports.yml"
    )
    assert workflow.is_file(), f"missing {workflow}"
    text = workflow.read_text(encoding="utf-8")
    assert _MARKER in text, (
        f"{workflow.name} must match the exact marker {_MARKER!r}"
    )
