"""Agent-session ``processing`` is sent when the status changes, not on every keep-alive tick.

``_keep_typing`` refreshes ``send_typing`` about every 2 seconds (30 calls a minute per turn).
``agents.sessions.setStatus`` is Tier 3 (50+ per minute), so two concurrent turns would exceed it.
Slack drops ``processing`` back to ``active`` after one hour unless it is re-sent, so a long turn
needs one bounded re-send inside the hour. Failures retry on a short backoff, never every tick.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod

THREAD = {"thread_id": "1700000000.000100"}


class _SlackApiErrorLike(Exception):
    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


@pytest.fixture(autouse=True)
def _pin_agent_sessions_api():
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
    a._now = [1000.0]
    a._status_clock = lambda: a._now[0]
    return a


def _sent(a):
    return [c.kwargs["status"] for c in a._app.client.agents_sessions_setStatus.await_args_list]


@pytest.mark.asyncio
async def test_refresh_ticks_send_processing_once():
    """30 keep-alive ticks (one minute of a live turn) make one API call, not 30."""
    a = _adapter()
    for _ in range(30):
        await a.send_typing("C123", metadata=THREAD)
        a._now[0] += 2.0
    assert _sent(a) == ["processing"]


@pytest.mark.asyncio
async def test_processing_is_resent_before_the_one_hour_timeout():
    """Slack times a processing session out after 3600s; re-send inside it, then go quiet again."""
    a = _adapter()
    await a.send_typing("C123", metadata=THREAD)
    a._now[0] += a._STATUS_RESEND_SECS - 1
    await a.send_typing("C123", metadata=THREAD)
    assert _sent(a) == ["processing"], "no re-send before the interval"
    assert a._STATUS_RESEND_SECS < 3600
    a._now[0] += 2
    await a.send_typing("C123", metadata=THREAD)
    assert _sent(a) == ["processing", "processing"]
    a._now[0] += 2
    await a.send_typing("C123", metadata=THREAD)
    assert _sent(a) == ["processing", "processing"], "quiet again after the re-send"


@pytest.mark.asyncio
async def test_new_turn_after_stop_sends_processing_again():
    """stop_typing ends the turn (active); the next turn's first tick must send processing."""
    a = _adapter()
    await a.send_typing("C123", metadata=THREAD)
    await a.stop_typing("C123", metadata=THREAD)
    a._now[0] += 5
    await a.send_typing("C123", metadata=THREAD)
    assert _sent(a) == ["processing", "active", "processing"]


@pytest.mark.asyncio
async def test_threads_are_independent():
    """Two concurrent turns each send their own processing once; ticks stay quiet."""
    a = _adapter()
    other = {"thread_id": "1700000000.000200"}
    for _ in range(5):
        await a.send_typing("C123", metadata=THREAD)
        await a.send_typing("C123", metadata=other)
        a._now[0] += 2.0
    assert _sent(a) == ["processing", "processing"]
    threads = [c.kwargs["thread_ts"] for c in a._app.client.agents_sessions_setStatus.await_args_list]
    assert sorted(threads) == ["1700000000.000100", "1700000000.000200"]


@pytest.mark.asyncio
async def test_failed_send_retries_on_a_backoff_not_every_tick():
    """A rejected status call is retried after the short backoff, so a persistent rejection
    cannot write a warning every two seconds."""
    a = _adapter()
    a._app.client.agents_sessions_setStatus = AsyncMock(
        side_effect=_SlackApiErrorLike("invalid_arguments", {"ok": False, "error": "invalid_arguments"}))
    for _ in range(10):  # 20 seconds of ticks
        await a.send_typing("C123", metadata=THREAD)
        a._now[0] += 2.0
    assert a._app.client.agents_sessions_setStatus.await_count == 1
    a._now[0] += a._STATUS_RETRY_SECS
    await a.send_typing("C123", metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 2


@pytest.mark.asyncio
async def test_failure_then_success_goes_quiet():
    """After a retry succeeds the thread is quiet for the full re-send interval."""
    a = _adapter()
    ok = AsyncMock(return_value={"ok": True})
    a._app.client.agents_sessions_setStatus = AsyncMock(
        side_effect=[_SlackApiErrorLike("ratelimited", {"ok": False, "error": "ratelimited"}), {"ok": True}])
    await a.send_typing("C123", metadata=THREAD)
    a._now[0] += a._STATUS_RETRY_SECS + 1
    await a.send_typing("C123", metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 2
    for _ in range(20):
        a._now[0] += 2.0
        await a.send_typing("C123", metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 2
    del ok


@pytest.mark.asyncio
async def test_legacy_assistant_threads_path_still_refreshes_every_tick():
    """Without the agents API the status is a text line that Slack clears on its own: the
    refresh behaviour there is unchanged."""
    _slack_mod._AGENT_SESSIONS_SUPPORTED = False
    a = _adapter()
    for _ in range(3):
        await a.send_typing("C123", metadata=THREAD)
        a._now[0] += 2.0
    assert a._app.client.assistant_threads_setStatus.await_count == 3
