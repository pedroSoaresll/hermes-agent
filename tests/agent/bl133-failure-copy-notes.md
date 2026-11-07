# BL-133 — provider failure replies stay plain-language (rendered evidence)

BL-133 removes raw provider detail from user-visible failure replies. `agent/turn_failure_copy.py`
used to append a trailing ``Provider said: {summary}`` line to the three provider-failure replies
(`exhausted_copy`, `nonretryable_copy`, `content_policy_copy`), which let operator mechanics —
``key_cmd`` output, retry counts, model-switch plumbing, OAuth instructions — reach a user's chat
verbatim (it surfaced in a founder-facing thread on 2026-11-05). Those three return tails now end
with one plain sentence from :func:`agent.turn_failure_copy.deferred_provider_notice`, and the full
raw summary goes to the log instead.

**The guarantee:** the raw summary is emitted only through
`logger.warning("provider failure detail withheld from reply: %s", summary)` in
`agent/turn_failure_copy.py` (module logger `agent.turn_failure_copy`, WARNING level); it never
appears in `final_response`. The `summary` parameter stays on all three functions (signatures
unchanged for the callers in `agent/turn_recovery.py` and `agent/turn_truncation.py`); it is now
logged, not interpolated.

## failure class -> rendered reply

Rendered with the worktree's test interpreter from the worktree root against the 2026-11-05 incident
summary (`key_cmd for provider 'genplat' exited 1: databricks auth login required (attempt 3/5,
falling back to model 'acme-mini-v2')`); the log lines below each reply are what the same call
emitted.

**Exhausted (rate_limit / server_error / timeout / overloaded share the shape — the per-lead
sentence differs, the tail does not):**

`rate_limit`:

```
Genplat rate-limited every one of 3 attempts — it looks temporarily unavailable. Wait a minute and send /retry, or switch models with /model. To avoid this in future, add a backup provider with `hermes fallback add`.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

`server_error`:

```
Genplat returned a server error on all 3 attempts — it looks temporarily unavailable. Wait a minute and send /retry, or switch models with /model. To avoid this in future, add a backup provider with `hermes fallback add`.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

`timeout`:

```
Genplat didn't respond in time on any of 3 attempts — it looks temporarily unavailable. Wait a minute and send /retry, or switch models with /model. To avoid this in future, add a backup provider with `hermes fallback add`.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

`overloaded`:

```
Genplat reported it was overloaded on all 3 attempts — it looks temporarily unavailable. Wait a minute and send /retry, or switch models with /model. To avoid this in future, add a backup provider with `hermes fallback add`.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

**Nonretryable auth — OAuth branch** (`openai-codex`; under the ambient session's named profile the
sanctioned re-login command carries the profile selector, `-p barx` here — that selector comes from
`profile_cli_selector()` reading the active home, and under an isolated test home it renders bare):

```
ChatGPT or Codex Subscription rejected your sign-in, so the model can't be reached. Sign in again: `hermes -p barx auth add openai-codex --type oauth`.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

Isolated-home render (the shape the suite sees, no named profile):

```
ChatGPT or Codex Subscription rejected your sign-in, so the model can't be reached. Sign in again: `hermes auth add openai-codex --type oauth`.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

**Nonretryable auth — api-key branch** (`genplat`; no catalog descriptor, so `auth_kind` returns
`api_key` and the branch's own copy points at Settings / `hermes setup` — it carries no re-login
command by design, which is why the new test pins the OAuth slug):

```
genplat rejected your API key, so the model can't be reached. Update it in Settings → Providers, or run `hermes setup` in a terminal.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

**Nonretryable — format_error:**

```
genplat rejected this request as malformed, so the model didn't answer. Start a clean session with /new or switch models with /model; if it keeps happening, run `hermes doctor`.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

**Content policy:**

```
Genplat's safety filter refused this request, so the model didn't answer. Try rewording your message or removing sensitive attachments, or switch to another model with /model.

The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.
```

**The exact deferral sentence** (all classes, verbatim):

> The work from this request is deferred, not lost — it will resume once the AI model service can take requests again.

Every render above also emitted exactly one log line, to `agent.turn_failure_copy` at WARNING:

```
provider failure detail withheld from reply: key_cmd for provider 'genplat' exited 1: databricks auth login required (attempt 3/5, falling back to model 'acme-mini-v2')
```

## integration-review flags

1. `tests/agent/test_failed_turn_chat_copy.py`'s former
   `assert "token_revoked" in text  # the raw error survives for the judge to quote` (that
   assertion existed so the goal judge could quote raw detail from the reply, #114012) now asserts
   the opposite: `assert "token_revoked" not in text  # BL-133: raw provider detail is logged,
   never shown`. The judge-visible contract that DOES survive is the sanctioned re-login command
   naming the provider slug and the profile selector (`hermes -p codex auth add openai-codex
   --type oauth` in that test; `<provider>` placeholder still absent), plus the CLI 💡 hint
   assertions on the same test.
2. `tests/e2e/core/providers/test_native_vertex_errors.py` line 149
   (`guidance = _output(turn).split("Provider said:")[0].lower()`) — that split is now a harmless
   no-op: the marker no longer appears in the reply, so the split returns the whole string and the
   downstream substring assertions still hold. It can be dropped in a follow-up. The file is
   untouched here (out of scope).
3. The existing per-lead copy tables still mention attempt counts ("every one of {attempts}
   attempts", "on all {attempts} attempts") by design — BL-133's scope is the summary-handling tail,
   not the copy tables. Likewise `result["error"]` still carries the raw summary unchanged: it is
   the machine-readable debug channel (persistence, logs, error surface), not the user-visible
   reply.
