"""agent_session_stopped: a Slack stop click stops the run for that thread and moves the session off processing.

Doc contract (docs.slack.dev/reference/events/agent_session_stopped, ai/agent-sessions):
  1. stop in-progress work for the channel and thread,
  2. confirm to the user that work stopped,
  3. set the session status away from processing (setStatus or chat.stopStream).
Slack does not transition the session itself. Without a subscription the stop button is not interactive.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod


@pytest.fixture(autouse=True)
def _allow_founder(monkeypatch):
    """The founder is the only allowed clicker, through the gate's env allowlist (as in
    test_slack_approval_buttons.py). Tests that need a refusal use another user."""
    for var in ("SLACK_ALLOWED_USERS", "SLACK_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "U_FOUNDER")


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
    return a


_EVENT = {
    "type": "agent_session_stopped",
    "channel": "C123",
    "thread_ts": "1700000000.000100",
    "user": "U_FOUNDER",
    "event_ts": "1700000001.000000",
    "streaming_message_ts": [],
}


@pytest.mark.asyncio
async def test_stop_interrupts_the_thread_run_then_sets_active():
    """A stop click interrupts the run for that thread once, then moves the session to active."""
    a = _adapter()
    calls = []
    a._stop_thread_run = AsyncMock(side_effect=lambda *args, **kw: calls.append(("interrupt", args, kw)))
    await a._handle_agent_session_stopped(_EVENT, body={"team_id": "T1"})
    assert len(calls) == 1, calls
    a._app.client.agents_sessions_setStatus.assert_awaited_once()
    kwargs = a._app.client.agents_sessions_setStatus.await_args.kwargs
    assert kwargs["status"] == "active", kwargs
    assert kwargs["channel_id"] == "C123" and kwargs["thread_ts"] == "1700000000.000100"


@pytest.mark.asyncio
async def test_stop_without_a_running_turn_is_a_noop_plus_active():
    """A stop with nothing running still moves the session off processing; it does not raise."""
    a = _adapter()
    a._stop_thread_run = AsyncMock(return_value=False)
    await a._handle_agent_session_stopped(_EVENT, body={"team_id": "T1"})
    kwargs = a._app.client.agents_sessions_setStatus.await_args.kwargs
    assert kwargs["status"] == "active"


@pytest.mark.asyncio
async def test_stop_from_a_non_admin_is_refused_and_does_not_interrupt():
    """Identity: only a user the admin gate accepts can stop a run. A refused stop changes nothing."""
    a = _adapter()
    a._stop_thread_run = AsyncMock(return_value=True)
    await a._handle_agent_session_stopped(dict(_EVENT, user="U_OTHER"), body={"team_id": "T1"})
    a._stop_thread_run.assert_not_awaited()
    a._app.client.agents_sessions_setStatus.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_event_without_a_channel_or_thread_is_ignored():
    """A malformed event must not reach the runner with an empty key."""
    a = _adapter()
    a._stop_thread_run = AsyncMock(return_value=True)
    await a._handle_agent_session_stopped({"type": "agent_session_stopped", "user": "U_FOUNDER"}, body={})
    a._stop_thread_run.assert_not_awaited()
    a._app.client.agents_sessions_setStatus.assert_not_awaited()


def test_listener_is_registered_for_agent_session_stopped():
    """Routing: the event type maps to a listener in the Bolt registry, ahead of the catch-all.

    Without this, a stop click is acknowledged by the catch-all and nothing runs.
    """
    import inspect
    src = inspect.getsource(SlackAdapter._connect_socket_mode if hasattr(SlackAdapter, "_connect_socket_mode")
                            else SlackAdapter)
    assert '"agent_session_stopped", self._handle_agent_session_stopped' in src
    assert src.index('"agent_session_stopped", self._handle_agent_session_stopped') < src.index(
        "Catch-all ack")


class _Runner:
    """A runner whose keys carry a profile namespace, as under ``multiplex_profiles``."""

    def __init__(self, running):
        self._running_agents = running
        self.interrupted = []

    def _session_key_for_source(self, source):
        return f"agent:barx:slack:group:T1:{source.chat_id}:{source.thread_id}"

    async def _interrupt_and_clear_session(self, key, source, **kw):
        self.interrupted.append((key, kw))


@pytest.mark.asyncio
async def test_stop_uses_the_runner_key_not_a_bare_session_key():
    """Under multiplex the run is stored under the runner's profile-namespaced key. A bare
    build_session_key misses it, so the click would clear the indicator while the run keeps going."""
    a = _adapter()
    key = "agent:barx:slack:group:T1:C123:1700000000.000100"
    runner = _Runner({key: object()})
    a.gateway_runner = runner
    assert await a._stop_thread_run("C123", "1700000000.000100", team_id="T1", user_id="U_FOUNDER")
    assert runner.interrupted and runner.interrupted[0][0] == key
    assert runner.interrupted[0][1]["invalidation_reason"] == "agent_session_stopped"


@pytest.mark.asyncio
async def test_stop_never_interrupts_a_different_users_run_in_the_same_thread():
    """No loose channel/thread match: another user's key for this thread must not be stopped."""
    a = _adapter()
    other = "agent:barx:slack:group:T1:C123:1700000000.000100:U_OTHER"
    runner = _Runner({other: object()})
    a.gateway_runner = runner
    assert not await a._stop_thread_run("C123", "1700000000.000100", team_id="T1", user_id="U_FOUNDER")
    assert runner.interrupted == []


@pytest.mark.asyncio
async def test_stop_with_no_matching_run_interrupts_nothing():
    a = _adapter()
    runner = _Runner({"agent:barx:slack:group:T1:C999:1.2": object()})
    a.gateway_runner = runner
    assert not await a._stop_thread_run("C123", "1700000000.000100", team_id="T1", user_id="U_FOUNDER")
    assert runner.interrupted == []


@pytest.mark.asyncio
async def test_stop_confirms_to_the_user_only_when_a_run_was_stopped():
    a = _adapter()
    a._stop_thread_run = AsyncMock(return_value=True)
    await a._handle_agent_session_stopped(_EVENT, body={"team_id": "T1"})
    a._app.client.chat_postMessage.assert_awaited_once()
    assert a._app.client.chat_postMessage.await_args.kwargs["thread_ts"] == "1700000000.000100"
    b = _adapter()
    b._stop_thread_run = AsyncMock(return_value=False)
    await b._handle_agent_session_stopped(_EVENT, body={"team_id": "T1"})
    b._app.client.chat_postMessage.assert_not_awaited()
