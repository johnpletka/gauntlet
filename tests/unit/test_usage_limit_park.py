"""Usage-limit park + session-preserving resume (harness-efficiency FR-3.2/3.3).

Drives the orchestrator with a stub adapter that raises a classified transient
failure, then resumes. Asserts: the step parks (not FAILED) with
parked_reason=usage_limit, the dirty worktree is left intact, the session is
preserved and the reset time stamped; a plain resume continues the session with
the short continuation prompt; an expired session falls back to a full re-run;
and a terminal failure still fails closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.adapters.base import (
    FAILURE_TERMINAL,
    FAILURE_TRANSIENT_USAGE_LIMIT,
    AgentFailedError,
    AgentResult,
    FailureInfo,
    SessionNotFoundError,
    Usage,
)
from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import Manifest, PipelineRef

from test_orchestrator import _build

PIPE = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, output: out.txt, prompt_text: do the real work}
"""


def _manifest() -> Manifest:
    return Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )


class ScriptedAdapter:
    """Raises a scripted failure/exception on early calls, then succeeds.

    Each call appends to ``calls``; ``script`` is a list of actions, one per
    call: ``("raise", exc)`` raises ``exc``, ``("ok", text)`` returns success
    (writing ``partial_file`` first when set, to prove a dirty worktree). After
    the script is exhausted, calls succeed.
    """

    name = "fake"

    def __init__(self, script, *, session="sess-1", partial_file=None):
        from gauntlet.adapters.base import AdapterCapabilities

        self.capabilities = AdapterCapabilities(
            repo_write=True, structured_output="native", resume=True
        )
        self.script = list(script)
        self.session = session
        self.partial_file = partial_file
        self.calls: list[dict] = []
        self.timeout_s = 600.0

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls.append({"prompt": prompt, "session": session})
        action = self.script.pop(0) if self.script else ("ok", "done")
        if action[0] == "raise":
            # write a partial edit before failing, to prove the park preserves it
            if self.partial_file is not None and cwd is not None:
                (Path(cwd) / self.partial_file).write_text("partial work\n")
            raise action[1]
        return AgentResult(text=action[1], session_id=self.session, exit_code=0)


def _transient(retry_after_s=None):
    return AgentFailedError(
        "usage limit hit",
        partial=AgentResult(text="", session_id="sess-1", usage=Usage(
            input_tokens=100, output_tokens=0, cost_usd=0.5), exit_code=1),
        failure_info=FailureInfo(
            kind=FAILURE_TRANSIENT_USAGE_LIMIT, marker="claude_usage_limit_message",
            retry_after_s=retry_after_s,
        ),
    )


def test_transient_failure_parks_preserving_worktree_and_session(fixture_repo):
    man = _manifest()
    adapter = ScriptedAdapter(
        [("raise", _transient(retry_after_s=1234))], partial_file="scratch.txt"
    )
    orch = _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man)
    assert orch.drive() == M.RUN_PARKED

    rec = man.record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_USAGE_LIMIT
    assert rec.session_id == "sess-1"  # session preserved for resume
    assert rec.retry_after_s == 1234
    assert rec.quota_reset_at is not None  # reset time derived + stamped
    # the failed call still cost tokens — accounted, not discarded
    assert man.totals.cost_usd == pytest.approx(0.5)
    # worktree left UNTOUCHED: the partial edit survives the park (no reset)
    assert (fixture_repo / "scratch.txt").read_text() == "partial work\n"


def test_zero_retry_after_is_a_real_hint_not_missing(fixture_repo):
    # RFC 7231 allows `Retry-After: 0` ("retry now"); a falsy-but-present hint
    # must still derive a reset time (= now) so auto-resume can fire — it is
    # not the same as an absent hint.
    man = _manifest()
    adapter = ScriptedAdapter([("raise", _transient(retry_after_s=0))])
    orch = _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.retry_after_s == 0
    assert rec.quota_reset_at is not None  # reset = now, not omitted


def test_plain_resume_continues_session_with_continuation_prompt(fixture_repo):
    man = _manifest()
    adapter = ScriptedAdapter([("raise", _transient()), ("ok", "finished")])
    # first drive: parks
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_PARKED
    # first call: fresh (no session), full prompt
    assert adapter.calls[0]["session"] is None
    assert "do the real work" in adapter.calls[0]["prompt"]
    # resume: re-drive the same manifest
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_DONE
    # second call continued the persisted session with the short continuation prompt
    assert adapter.calls[1]["session"] == "sess-1"
    assert "interrupted by a provider usage limit" in adapter.calls[1]["prompt"]
    assert "do the real work" not in adapter.calls[1]["prompt"]  # not the full prompt
    rec = man.record("implement")
    assert rec.status == M.DONE
    assert rec.parked_reason is None  # cleared on DONE (current-state)
    assert rec.retry_after_s is None and rec.quota_reset_at is None


def test_expired_session_falls_back_to_full_rerun_with_note(fixture_repo):
    man = _manifest()
    adapter = ScriptedAdapter([
        ("raise", _transient()),                                  # first run parks
        ("raise", SessionNotFoundError("session sess-1 gone")),    # resume: session dead
        ("ok", "finished afresh"),                                 # fallback full re-run
    ])
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_PARKED
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_DONE
    # resume attempt used the stored session + continuation prompt...
    assert adapter.calls[1]["session"] == "sess-1"
    assert "interrupted by a provider usage limit" in adapter.calls[1]["prompt"]
    # ...then fell back to a full re-run with NO session and the full prompt
    assert adapter.calls[2]["session"] is None
    assert "do the real work" in adapter.calls[2]["prompt"]
    rec = man.record("implement")
    assert rec.status == M.DONE
    assert "unknown/expired" in (rec.notes or "")


def test_terminal_failure_fails_closed(fixture_repo):
    man = _manifest()
    terminal = AgentFailedError(
        "auth error",
        partial=AgentResult(text="", session_id="sess-1", exit_code=1),
        failure_info=FailureInfo(kind=FAILURE_TERMINAL, marker="unmatched"),
    )
    adapter = ScriptedAdapter([("raise", terminal)])
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_FAILED
    rec = man.record("implement")
    assert rec.status == M.FAILED
    assert rec.parked_reason is None


def test_unclassified_agent_failure_fails_closed(fixture_repo):
    # An AgentFailedError with NO failure_info (older adapter) is treated as
    # terminal — never auto-continued past an unknown error (§7).
    man = _manifest()
    unclassified = AgentFailedError(
        "mystery", partial=AgentResult(text="", exit_code=1), failure_info=None
    )
    adapter = ScriptedAdapter([("raise", unclassified)])
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_FAILED
    assert man.record("implement").status == M.FAILED


def test_usage_limit_is_not_response_resolvable():
    # A usage_limit park is resumed by a PLAIN `gauntlet resume`, never
    # `--response` — so it must not be in the response-resolvable set (the guard
    # that would otherwise reject a plain resume and demand a decision).
    assert M.PARKED_REASON_USAGE_LIMIT not in M.RESPONSE_RESOLVABLE_PARK_REASONS
