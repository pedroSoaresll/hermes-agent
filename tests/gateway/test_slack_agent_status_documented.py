"""Agent-session status uses only the documented values (processing / active / suspended / closed).

Live probe 2026-11-26 against Slack: ``agents.sessions.setStatus`` accepts processing, active,
suspended and closed, and rejects free text and the empty string with ``invalid_arguments``.
These tests pin the adapter to the documented values and check the failure is visible.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod

DOCUMENTED = {"processing", "active", "suspended", "closed"}


class _SlackApiErrorLike(Exception):
    """slack_sdk.SlackApiError's shape (``exc.response[\"error\"]``). CI stubs slack_sdk as a bare
    module, so the test carries its own stand-in; the adapter only reads the response mapping."""

    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


@pytest.fixture(autouse=True)
def _pin_agent_sessions_api():
    """Route through the agents.sessions API and restore the cached capability probe after."""
    prev = _slack_mod._AGENT_SESSIONS_SUPPORTED
    _slack_mod._AGENT_SESSIONS_SUPPORTED = True
    yield
    _slack_mod._AGENT_SESSIONS_SUPPORTED = prev


def _adapter():
    a = SlackAdapter(PlatformConfig(enabled=True, token="***"))
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._app.client.agents_sessions_setStatus = AsyncMock(return_value={"ok": True})
    a._app.client.assistant_threads_setStatus = AsyncMock()
    return a


def _statuses_sent(a):
    return [c.kwargs["status"] for c in a._app.client.agents_sessions_setStatus.await_args_list]


@pytest.mark.asyncio
async def test_typing_sends_processing_not_free_text():
    """A turn start sets the documented 'processing' state, never a free-text phrase."""
    a = _adapter()
    await a.send_typing("C123", metadata={"thread_id": "1700000000.000100"})
    sent = _statuses_sent(a)
    assert sent == ["processing"], sent
    assert all(s in DOCUMENTED for s in sent)


@pytest.mark.asyncio
async def test_stop_typing_sends_active_not_empty_string():
    """A turn end clears to the documented 'active' state; '' is rejected by Slack."""
    a = _adapter()
    meta = {"thread_id": "1700000000.000100"}
    await a.send_typing("C123", metadata=meta)
    a._app.client.agents_sessions_setStatus.reset_mock()
    await a.stop_typing("C123", metadata=meta)
    sent = _statuses_sent(a)
    assert sent == ["active"], sent
    assert "" not in sent


@pytest.mark.asyncio
async def test_no_status_call_uses_an_undocumented_value():
    """Across a full turn, every status value is in the documented set."""
    a = _adapter()
    meta = {"thread_id": "1700000000.000100"}
    await a.send_typing("C123", metadata=meta)
    await a.send_typing("C123", metadata=meta)  # keep-typing refresh
    await a.stop_typing("C123", metadata=meta)
    sent = _statuses_sent(a)
    assert sent, "expected at least one status call"
    assert set(sent) <= DOCUMENTED, sent


@pytest.mark.asyncio
async def test_non_ok_status_response_is_logged_at_warning(caplog):
    """Slack's rejection of a status call must reach the logs at WARNING, not DEBUG."""
    a = _adapter()
    a._app.client.agents_sessions_setStatus = AsyncMock(
        side_effect=_SlackApiErrorLike(
            "invalid_arguments", {"ok": False, "error": "invalid_arguments"}))
    with caplog.at_level(logging.WARNING):
        await a.send_typing("C123", metadata={"thread_id": "1700000000.000100"})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "setStatus" in r.getMessage()]
    assert warnings, [r.getMessage() for r in caplog.records]
    assert "invalid_arguments" in warnings[0].getMessage()
