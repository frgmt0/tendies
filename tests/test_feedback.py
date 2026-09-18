from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from tendies.cogs.feedback import BugReportModal, FeedbackCog, issue_url


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
