"""Agent-session ``processing`` is sent when the status changes, not on every keep-alive tick.

``_keep_typing`` refreshes ``send_typing`` about every 2 seconds, each call under a 1.5 s timeout.
``agents.sessions.setStatus`` is Tier 3 (50+ per minute), so a send per tick exceeds it with two
turns. Slack drops ``processing`` back to ``active`` after one hour, and sets ``active`` itself
when a stream is stopped, so the status must be re-sent on those two changes.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod

THREAD = {"thread_id": "1700000000.000100"}
CHAT = "C123"


class _SlackApiErrorLike(Exception):
    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


class _Resp(dict):
    def __init__(self, error, headers=None):
        super().__init__({"ok": False, "error": error})
        self.headers = headers or {}


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
    a._app.client.chat_stopStream = AsyncMock(return_value={"ok": True})
    a._now = [1000.0]
    a._status_clock = lambda: a._now[0]
    return a


def _sent(a):
    return [c.kwargs["status"] for c in a._app.client.agents_sessions_setStatus.await_args_list]


def _err(code, headers=None):
    return _SlackApiErrorLike(code, _Resp(code, headers))


@pytest.mark.asyncio
async def test_refresh_ticks_send_processing_once():
    """30 keep-alive ticks (one minute of a live turn) make one API call, not 30."""
    a = _adapter()
    for _ in range(30):
        await a.send_typing(CHAT, metadata=THREAD)
        a._now[0] += 2.0
    assert _sent(a) == ["processing"]


@pytest.mark.asyncio
async def test_processing_is_resent_before_the_one_hour_timeout():
    a = _adapter()
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._PROCESSING_REFRESH_SECONDS < 3600
    a._now[0] += a._PROCESSING_REFRESH_SECONDS - 1
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing"], "no re-send before the interval"
    a._now[0] += 2
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing", "processing"]
    a._now[0] += 2
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing", "processing"], "quiet again after the re-send"


@pytest.mark.asyncio
async def test_new_turn_after_stop_sends_processing_again():
    a = _adapter()
    await a.send_typing(CHAT, metadata=THREAD)
    await a.stop_typing(CHAT, metadata=THREAD)
    a._now[0] += 5
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing", "active", "processing"]


@pytest.mark.asyncio
async def test_threads_are_independent():
    a = _adapter()
    other = {"thread_id": "1700000000.000200"}
    for _ in range(5):
        await a.send_typing(CHAT, metadata=THREAD)
        await a.send_typing(CHAT, metadata=other)
        a._now[0] += 2.0
    assert _sent(a) == ["processing", "processing"]


@pytest.mark.asyncio
async def test_failed_send_retries_on_a_backoff_not_every_tick():
    a = _adapter()
    a._app.client.agents_sessions_setStatus = AsyncMock(side_effect=_err("invalid_arguments"))
    for _ in range(10):  # 20 seconds of ticks
        await a.send_typing(CHAT, metadata=THREAD)
        a._now[0] += 2.0
    assert a._app.client.agents_sessions_setStatus.await_count == 1
    a._now[0] += a._STATUS_RETRY_SECONDS
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 2


@pytest.mark.asyncio
async def test_retry_after_header_is_honoured_and_bounded():
    a = _adapter()
    a._app.client.agents_sessions_setStatus = AsyncMock(
        side_effect=_err("ratelimited", {"Retry-After": "120"}))
    await a.send_typing(CHAT, metadata=THREAD)
    a._now[0] += 100
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 1, "still inside Retry-After"
    a._now[0] += 21
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 2

    b = _adapter()
    b._app.client.agents_sessions_setStatus = AsyncMock(
        side_effect=_err("ratelimited", {"Retry-After": "99999"}))
    await b.send_typing(CHAT, metadata=THREAD)
    b._now[0] += b._STATUS_RETRY_MAX_SECONDS + 1
    await b.send_typing(CHAT, metadata=THREAD)
    assert b._app.client.agents_sessions_setStatus.await_count == 2, "Retry-After is capped"


@pytest.mark.asyncio
async def test_slow_status_call_cancelled_by_the_keep_alive_timeout_is_not_resent():
    """_keep_typing cancels a send_typing that takes over 1.5 s. The status call keeps running and
    records its result, so the next ticks do not send again (review item 1)."""
    a = _adapter()
    gate = asyncio.Event()

    async def slow(**kwargs):
        await gate.wait()
        return {"ok": True}

    a._app.client.agents_sessions_setStatus = AsyncMock(side_effect=slow)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(a.send_typing(CHAT, metadata=THREAD), timeout=0.05)
    for _ in range(5):
        a._now[0] += 2.0
        await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 1
    gate.set()
    await asyncio.sleep(0.05)
    for _ in range(5):
        a._now[0] += 2.0
        await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 1, "success recorded despite the cancel"


@pytest.mark.asyncio
async def test_concurrent_ticks_on_one_thread_send_once():
    """Two overlapping send_typing calls claim the slot before awaiting (review item 2)."""
    a = _adapter()
    gate = asyncio.Event()

    async def slow(**kwargs):
        await gate.wait()
        return {"ok": True}

    a._app.client.agents_sessions_setStatus = AsyncMock(side_effect=slow)
    first = asyncio.ensure_future(a.send_typing(CHAT, metadata=THREAD))
    second = asyncio.ensure_future(a.send_typing(CHAT, metadata=THREAD))
    await asyncio.sleep(0.02)
    gate.set()
    await asyncio.gather(first, second)
    assert a._app.client.agents_sessions_setStatus.await_count == 1


@pytest.mark.asyncio
async def test_mid_turn_stream_seal_resends_processing():
    """Slack sets ``active`` when a stream is stopped; a mid-turn seal (tool-call segment break)
    must make the next tick send ``processing`` again, not wait up to 50 minutes."""
    a = _adapter()
    await a.send_typing(CHAT, metadata=THREAD)
    key = next(iter(a._active_status_threads))
    stream = {"ts": "1700000001.000100", "sent": "", "base": 0}
    assert await a._seal_stream(key, stream) is True
    a._now[0] += 2.0
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing", "processing"]
    a._now[0] += 2.0
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing", "processing"], "quiet again after the re-send"


@pytest.mark.asyncio
async def test_failed_seal_also_resets_the_gate():
    a = _adapter()
    a._app.client.chat_stopStream = AsyncMock(side_effect=_err("message_not_found"))
    await a.send_typing(CHAT, metadata=THREAD)
    key = next(iter(a._active_status_threads))
    assert await a._seal_stream(key, {"ts": "1700000001.000100"}) is False
    a._now[0] += 2.0
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing", "processing"]


@pytest.mark.asyncio
async def test_stop_with_team_id_clears_a_team_less_tracked_thread():
    """send_typing tracked the thread without a team id; stop_typing arrives with one. The entry
    must not survive, or the next turn sends no ``processing`` for up to 50 minutes (item 3)."""
    a = _adapter()
    await a.send_typing(CHAT, metadata=THREAD)
    assert list(a._active_status_threads) == [("", CHAT, "1700000000.000100")]
    await a.stop_typing(CHAT, metadata={**THREAD, "team_id": "T1"})
    assert a._active_status_threads == {}
    a._now[0] += 2.0
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing", "active", "processing"]


@pytest.mark.asyncio
async def test_failed_clear_is_retried_once():
    """A rejected ``active`` would leave the thread on processing for an hour; retry it once."""
    a = _adapter()
    a._STATUS_RETRY_SECONDS = 0.01
    a._app.client.agents_sessions_setStatus = AsyncMock(
        side_effect=[{"ok": True}, _err("ratelimited"), {"ok": True}])
    await a.send_typing(CHAT, metadata=THREAD)
    await a.stop_typing(CHAT, metadata=THREAD)
    await asyncio.sleep(0.1)
    assert _sent(a) == ["processing", "active", "active"]


@pytest.mark.asyncio
async def test_clear_retry_yields_to_a_new_turn():
    a = _adapter()
    a._STATUS_RETRY_SECONDS = 0.05
    a._app.client.agents_sessions_setStatus = AsyncMock(
        side_effect=[{"ok": True}, _err("ratelimited"), {"ok": True}])
    await a.send_typing(CHAT, metadata=THREAD)
    await a.stop_typing(CHAT, metadata=THREAD)
    a._now[0] += 1.0
    await a.send_typing(CHAT, metadata=THREAD)  # new turn starts before the retry fires
    await asyncio.sleep(0.15)
    assert _sent(a) == ["processing", "active", "processing"], "the retry must not clobber it"


@pytest.mark.asyncio
async def test_legacy_assistant_threads_path_still_refreshes_every_tick():
    _slack_mod._AGENT_SESSIONS_SUPPORTED = False
    a = _adapter()
    for _ in range(3):
        await a.send_typing(CHAT, metadata=THREAD)
        a._now[0] += 2.0
    assert a._app.client.assistant_threads_setStatus.await_count == 3


@pytest.mark.asyncio
async def test_cancelled_call_that_later_fails_still_honours_retry_after():
    """The cancelled call's result lands in the live entry even though later ticks refreshed it
    (reviewer probe P2): Retry-After 120 must hold, not the 30 s default."""
    a = _adapter()
    gate = asyncio.Event()

    async def slow_then_rate_limited(**kwargs):
        await gate.wait()
        raise _err("ratelimited", {"Retry-After": "120"})

    a._app.client.agents_sessions_setStatus = AsyncMock(side_effect=slow_then_rate_limited)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(a.send_typing(CHAT, metadata=THREAD), timeout=0.05)
    a._now[0] += 2.0
    await a.send_typing(CHAT, metadata=THREAD)  # tick while the call is still in flight
    gate.set()
    await asyncio.sleep(0.05)
    a._now[0] += 60.0  # past the 30 s default, inside Retry-After
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 1
    a._now[0] += 61.0
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_call_that_later_succeeds_is_not_resent_after_the_retry_window():
    """Reviewer probe P1: success of a cancelled call must be recorded in the live entry."""
    a = _adapter()
    gate = asyncio.Event()

    async def slow(**kwargs):
        await gate.wait()
        return {"ok": True}

    a._app.client.agents_sessions_setStatus = AsyncMock(side_effect=slow)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(a.send_typing(CHAT, metadata=THREAD), timeout=0.05)
    a._now[0] += 2.0
    await a.send_typing(CHAT, metadata=THREAD)
    gate.set()
    await asyncio.sleep(0.05)
    a._now[0] += a._STATUS_RETRY_SECONDS + 1  # past the claim window
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 1


@pytest.mark.asyncio
async def test_seal_during_an_in_flight_processing_call_resends():
    """A seal that runs while processing is in flight: Slack sets active after it, so the result
    of the older call must not push the next send out to the refresh interval (probe P4)."""
    a = _adapter()
    gate = asyncio.Event()

    async def slow(**kwargs):
        await gate.wait()
        return {"ok": True}

    a._app.client.agents_sessions_setStatus = AsyncMock(side_effect=slow)
    first = asyncio.ensure_future(a.send_typing(CHAT, metadata=THREAD))
    await asyncio.sleep(0.02)
    key = next(iter(a._active_status_threads))
    await a._seal_stream(key, {"ts": "1700000001.000100"})
    gate.set()
    await first
    a._now[0] += 2.0
    a._app.client.agents_sessions_setStatus = AsyncMock(return_value={"ok": True})
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 1, "re-sent after the seal"


@pytest.mark.asyncio
async def test_seal_resets_a_status_entry_tracked_under_a_different_team_key():
    """Stream key team and status key team can differ (probe P3): match on channel and thread."""
    a = _adapter()
    await a.send_typing(CHAT, metadata=THREAD)
    assert list(a._active_status_threads) == [("", CHAT, "1700000000.000100")]
    await a._seal_stream(("T1", CHAT, "1700000000.000100"), {"ts": "1700000001.000100"})
    a._now[0] += 2.0
    await a.send_typing(CHAT, metadata=THREAD)
    assert _sent(a) == ["processing", "processing"]


@pytest.mark.asyncio
async def test_seal_during_a_rejected_call_keeps_the_retry_after():
    """A rejection keeps its Retry-After even when a seal bumped the generation meanwhile."""
    a = _adapter()
    gate = asyncio.Event()

    async def slow_rate_limited(**kwargs):
        await gate.wait()
        raise _err("ratelimited", {"Retry-After": "120"})

    a._app.client.agents_sessions_setStatus = AsyncMock(side_effect=slow_rate_limited)
    first = asyncio.ensure_future(a.send_typing(CHAT, metadata=THREAD))
    await asyncio.sleep(0.02)
    await a._seal_stream(next(iter(a._active_status_threads)), {"ts": "1700000001.000100"})
    gate.set()
    await first
    a._now[0] += 2.0
    await a.send_typing(CHAT, metadata=THREAD)
    assert a._app.client.agents_sessions_setStatus.await_count == 1
