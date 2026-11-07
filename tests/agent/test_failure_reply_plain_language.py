"""BL-133: provider/LLM failure replies stay plain-language.

A raw provider summary (``key_cmd`` output, retry counts, model-switch plumbing, OAuth
instructions) used to ride a trailing ``Provider said:`` line into user-visible failure
replies — it surfaced verbatim in a founder-facing chat thread on 2026-11-05. Contract:
the reply keeps its plain what-happened lead + the existing Next guidance, names the work
as deferred and resuming, and the raw provider summary goes to the log, never to the user.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_failure_copy import content_policy_copy, exhausted_copy, nonretryable_copy
from agent.turn_recovery import max_retries_exhausted_result, nonretryable_client_error_result

# The 2026-11-05 incident shape: operator mechanics packed into one provider summary.
RAW_SUMMARY = (
    "key_cmd for provider 'genplat' exited 1: databricks auth login required "
    "(attempt 3/5, falling back to model 'acme-mini-v2')"
)
FORBIDDEN_IN_REPLY = (
    "Provider said:", "key_cmd", "databricks auth login", "attempt 3/5", "acme-mini-v2",
)
DEFERRAL_MARKERS = ("deferred", "resume")


def _assert_plain_language(text: str) -> None:
    for fragment in FORBIDDEN_IN_REPLY:
        assert fragment not in text, f"provider mechanics leaked into the reply: {fragment!r}\n{text}"
    tail = text.rstrip().rsplit("\n\n", 1)[-1]
    assert "Provider said:" not in tail and "\n" not in tail
    assert all(marker in tail for marker in DEFERRAL_MARKERS), tail


class _Agent:
    """Mirror of tests/agent/test_failed_turn_chat_copy.py::_Agent (loop-path stub)."""

    log_prefix = ""
    verbose = False
    verbose_logging = False
    provider = "openrouter"
    model = "m"
    base_url = "https://openrouter.ai/api/v1"
    max_iterations = 30
    suppress_status_output = True
    _fallback_chain = ()
    _fallback_index = 0

    def _summarize_api_error(self, error):
        return str(error)

    def _clean_error_message(self, msg):
        return msg

    def _has_pending_fallback(self):
        return False

    def _try_activate_fallback(self):
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Http(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize("reason", [
    FailoverReason.rate_limit.value, FailoverReason.server_error.value,
    FailoverReason.timeout.value, FailoverReason.overloaded.value,
])
def test_exhausted_reply_is_plain_language_with_deferral(caplog, reason):
    with caplog.at_level(logging.WARNING, logger="agent.turn_failure_copy"):
        text = exhausted_copy(reason, label="Genplat", attempts=3, summary=RAW_SUMMARY)
    _assert_plain_language(text)
    assert "/retry" in text and "/model" in text  # the existing Next guidance survives
    assert RAW_SUMMARY in caplog.text  # ...and the full raw detail is in the log, not the reply


def test_nonretryable_auth_reply_keeps_our_relogin_command_but_not_the_providers(caplog):
    # openai-codex is the OAuth branch (the only one carrying the sanctioned re-login command);
    # genplat resolves to the api_key branch, whose copy points at Settings/`hermes setup` instead.
    with caplog.at_level(logging.WARNING, logger="agent.turn_failure_copy"):
        text = nonretryable_copy(
            SimpleNamespace(reason=FailoverReason.auth, is_auth=True),
            provider="openai-codex", model="acme-mini-v2", summary=RAW_SUMMARY,
        )
    _assert_plain_language(text)
    assert "auth add openai-codex" in text  # Hermes' own sanctioned re-login step stays
    assert RAW_SUMMARY in caplog.text


def test_nonretryable_format_error_reply_is_plain_language_with_deferral(caplog):
    with caplog.at_level(logging.WARNING, logger="agent.turn_failure_copy"):
        text = nonretryable_copy(
            SimpleNamespace(reason=FailoverReason.format_error), provider="genplat",
            model="acme-mini-v2", summary=RAW_SUMMARY,
        )
    _assert_plain_language(text)
    assert RAW_SUMMARY in caplog.text


def test_content_policy_reply_is_plain_language_with_deferral(caplog):
    with caplog.at_level(logging.WARNING, logger="agent.turn_failure_copy"):
        text = content_policy_copy(label="Genplat", summary=RAW_SUMMARY)
    _assert_plain_language(text)
    assert "safety filter" in text
    assert RAW_SUMMARY in caplog.text


def test_exhausted_result_keeps_stamping_and_plain_reply(caplog):
    error = _Http(503, RAW_SUMMARY)
    classified = classify_api_error(error, provider="openrouter", model="m")
    with caplog.at_level(logging.WARNING, logger="agent.turn_failure_copy"):
        result = max_retries_exhausted_result(
            _Agent(), error, classified, attempts=3, is_rate_limited=False,
            error_msg=str(error).lower(), api_kwargs=None, api_messages=[], messages=[],
            conversation_history=None, api_call_count=1, approx_tokens=10,
            provider="openrouter", base_url="https://openrouter.ai/api/v1", model="m",
        )
    assert result["failure_reason"] == classified.reason.value
    assert result["failure_retryable"] is True
    _assert_plain_language(result["final_response"])
    assert RAW_SUMMARY in caplog.text


def test_nonretryable_result_keeps_stamping_and_plain_reply(caplog):
    error = _Http(400, RAW_SUMMARY)
    classified = classify_api_error(error, provider="openrouter", model="m")
    with caplog.at_level(logging.WARNING, logger="agent.turn_failure_copy"):
        result = nonretryable_client_error_result(
            _Agent(), error, classified, status_code=400, api_kwargs=None, api_messages=[],
            messages=[], conversation_history=None, api_call_count=1, approx_tokens=10,
            provider="openrouter", base_url="https://openrouter.ai/api/v1", model="m",
        )
    assert result["failure_reason"] == classified.reason.value
    _assert_plain_language(result["final_response"])
    assert RAW_SUMMARY in caplog.text
