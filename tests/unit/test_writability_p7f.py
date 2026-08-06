"""The per-adapter writability preflight (P7f, `P7d-gate-blocker.md` §5 Option 4).

The defect this detects cost a whole dogfood run and was invisible in every
durable record: Gauntlet's judge is a **PreToolUse** hook, so it adjudicates
before the CLI applies its own permission rules and logs `Write … allow` for
writes that never happened. The only engine-visible symptom was "the fixer made
no changes in round 1 despite 7 accepted finding(s)" — which reads as a model
failure.

Three properties are load-bearing and each has a test here, because getting any
one of them wrong reproduces the original failure in a new costume:

1. the verdict comes from the FILESYSTEM after the turn, never from the
   adapter's own account of what it did (§2.4);
2. each write mechanism is exercised BY NAME, so a model cannot pass the probe
   with `tee` while the real task later fails on `Write` (§2.3);
3. adapters are probed SEPARATELY and never generalized — `codex` is not
   subject to the guard that blocks `claude`, and a codex-only check would have
   called the failing dogfood tree healthy (§2.5 probe 2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.engine import writability as W


class _Adapter:
    """A stand-in for a real CLI adapter, scripted per mechanism.

    ``lands`` names the mechanisms whose bytes actually appear. Everything else
    "runs" and writes nothing — which is precisely the refusal shape: the CLI
    accepted the turn, the model believed it wrote, and no bytes landed.
    """

    probes_writability = True

    def __init__(self, lands=W.MECHANISMS, raises: set[str] | None = None,
                 report_success=True):
        self.lands = set(lands)
        self.raises = raises or set()
        self.report_success = report_success
        self.calls: list[tuple[str, Path, tuple]] = []

    @staticmethod
    def writability_flags(tools):
        return ["--allowedTools", ",".join(tools)]

    def run(self, prompt, *, cwd=None, extra_flags=None, **_kw):
        mechanism = _mechanism_of(prompt)
        self.calls.append((mechanism, cwd, tuple(extra_flags or ())))
        if mechanism in self.raises:
            raise RuntimeError("adapter exploded")
        if mechanism in self.lands:
            target = Path(cwd) / {
                W.WRITE_TOOL: "probe.txt",
                W.EDIT_TOOL: "probe_edit.txt",
                W.SHELL_REDIRECT: "probe_sh.txt",
            }[mechanism]
            target.write_text("OK")
        return object()


def _mechanism_of(prompt: str) -> str:
    if "Write tool to create" in prompt:
        return W.WRITE_TOOL
    if "Edit tool to replace" in prompt:
        return W.EDIT_TOOL
    return W.SHELL_REDIRECT


# --- property 1: the verdict is the filesystem, not the adapter's account ----


def test_an_adapter_that_claims_success_but_writes_nothing_is_refused(tmp_path):
    """The exact P7d shape, and the reason the probe cannot trust output.

    Both dogfood rounds had the model report a completed edit for a write the
    CLI had refused. An adapter's own account of what it did is therefore not
    evidence, and this test fails the moment the probe starts believing it.
    """
    adapter = _Adapter(lands=(), report_success=True)
    report = W.probe(adapter, tmp_path, adapter_name="claude-code")

    assert not report.ok
    assert set(report.refused) == set(W.MECHANISMS)
    assert report.errored == ()


def test_a_landed_write_is_the_only_thing_that_passes(tmp_path):
    adapter = _Adapter()
    report = W.probe(adapter, tmp_path, adapter_name="claude-code")
    assert report.ok, report.summary()
    assert report.refused == () and report.errored == ()


def test_the_probe_directory_is_removed_so_the_tree_stays_clean(tmp_path):
    """The preflight runs before the first agent step; the invariant is that the
    tree is clean at every handoff (CLAUDE.md §1), so the probe must leave none
    of itself behind — including when a mechanism blew up."""
    W.probe(_Adapter(raises={W.SHELL_REDIRECT}), tmp_path, adapter_name="c")
    assert not (tmp_path / W.PROBE_DIRNAME).exists()
    assert list(tmp_path.iterdir()) == []


# --- property 2: each mechanism by name, no model choice ---------------------


def test_every_mechanism_is_exercised_separately_and_named(tmp_path):
    """§2.3: the guard is NOT uniform, so one turn cannot stand for the others.

    Measured under `.git/`: `Write`/`Edit`/`printf >` refused, `tee`/`cat >`
    permitted. A single "write a file here" turn would pass or fail on whichever
    form the model improvised.
    """
    adapter = _Adapter()
    W.probe(adapter, tmp_path, adapter_name="claude-code")

    assert [c[0] for c in adapter.calls] == list(W.MECHANISMS)
    # Each turn is narrowed to the tools its mechanism needs, so the model has
    # no alternative route to satisfy the prompt.
    by_mechanism = {c[0]: c[2] for c in adapter.calls}
    assert "Write" in by_mechanism[W.WRITE_TOOL][1]
    assert "Bash" not in by_mechanism[W.WRITE_TOOL][1]
    assert "Edit" in by_mechanism[W.EDIT_TOOL][1]
    assert "Bash" in by_mechanism[W.SHELL_REDIRECT][1]


def test_one_refused_mechanism_fails_the_whole_report(tmp_path):
    """A partially-working tree is not a working tree: the builder picks the
    mechanism, so any refused form is a live failure mode for the run."""
    report = W.probe(
        _Adapter(lands=(W.SHELL_REDIRECT,)), tmp_path, adapter_name="claude-code"
    )
    assert not report.ok
    assert set(report.refused) == {W.WRITE_TOOL, W.EDIT_TOOL}
    assert W.SHELL_REDIRECT not in report.refused


def test_the_edit_probe_is_seeded_by_the_engine_not_by_a_prior_write(tmp_path):
    """Edit needs a file to edit. Seeding it from the engine keeps the Edit
    verdict independent of whether the Write probe succeeded — otherwise a
    refused Write would cascade into a false Edit refusal."""
    adapter = _Adapter(lands=(W.EDIT_TOOL,))
    report = W.probe(adapter, tmp_path, adapter_name="claude-code")
    assert W.EDIT_TOOL not in report.refused


# --- property 3: never generalize between adapters ---------------------------


def test_a_refusal_and_a_pass_coexist_without_contaminating_each_other(tmp_path):
    """§2.5 probe 2, made executable: codex worked throughout the dogfood while
    every claude step was refused. A check that generalized would have reported
    the tree healthy."""
    claude = W.probe(_Adapter(lands=()), tmp_path, adapter_name="claude-code")
    codex = W.probe(_Adapter(), tmp_path, adapter_name="codex")

    assert not claude.ok and codex.ok
    reason = W.park_reason([claude, codex])
    assert reason is not None
    assert "claude-code" in reason
    assert "codex:" not in reason, "a healthy adapter must not be blamed"


def test_park_reason_is_none_when_every_adapter_is_clean(tmp_path):
    reports = [
        W.probe(_Adapter(), tmp_path, adapter_name=n) for n in ("claude-code", "codex")
    ]
    assert W.park_reason(reports) is None


# --- scope: only real CLI permission layers are probed -----------------------


def test_only_adapters_declaring_a_real_permission_layer_are_probed():
    """Not a test accommodation — a statement about what the probe MEANS.

    The question is "will this CLI's permission layer refuse writes at this
    path?". The `api` adapter has no repo access, and an in-process double has
    no permission layer, so probing either measures nothing — and a false
    refusal would park a healthy run, which is worse than silence.
    """
    from gauntlet.adapters.claude_code import ClaudeCodeAdapter
    from gauntlet.adapters.codex import CodexAdapter
    from gauntlet.adapters.api import ApiAdapter

    assert W.should_probe(ClaudeCodeAdapter)
    assert W.should_probe(CodexAdapter)
    assert not W.should_probe(ApiAdapter)
    assert not W.should_probe(object())


# --- an adapter that cannot run is a different problem -----------------------


def test_an_adapter_that_cannot_run_is_reported_as_an_error_not_a_refusal(tmp_path):
    """Conflating them would send an operator to fix the wrong thing: a missing
    CLI or absent credentials has nothing to do with the path's writability."""
    report = W.probe(
        _Adapter(raises=set(W.MECHANISMS)), tmp_path, adapter_name="claude-code"
    )
    assert set(report.errored) == set(W.MECHANISMS)
    assert report.refused == ()
    assert not report.ok
    reason = W.park_reason([report])
    assert "could not run" in reason


def test_the_park_reason_explains_why_nothing_recorded_the_failure(tmp_path):
    """The park has to teach, because the operator's instinct will be to blame
    the model — that is what the P7d dogfood looked like from outside."""
    report = W.probe(_Adapter(lands=()), tmp_path, adapter_name="claude-code")
    reason = W.park_reason([report])
    assert "PreToolUse" in reason
    assert "migrate-worktree" in reason
    assert str(tmp_path) in reason


# --- the start-time surface: it must actually park a real run ----------------


REFUSING_CONFIG = """
base_branch: main
run_root: runs
worktree:
  mode: dedicated
agents:
  builder: {adapter: claude-code}
"""

ONE_GATE = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [prd.md]}
"""


def test_start_parks_before_any_agent_step_when_the_tree_is_not_writable(
    fixture_repo,
):
    """The second surface, end to end — and the one that matters at 3am.

    Asserted through a REAL `start` rather than by calling the preflight
    directly, because the failure this guards against is the preflight being
    unreachable. An earlier draft of this commit wired it in between a
    `@contextmanager` decorator and its function; every dedicated-run test broke
    loudly, which is the only reason it was caught. A unit test of the helper
    alone would have stayed green.
    """
    from gauntlet.engine import manifest as M
    from gauntlet.engine.run import RunManager, WorktreeUnavailableError
    from conftest import git

    (fixture_repo / ".gauntlet").mkdir(exist_ok=True)
    (fixture_repo / ".gauntlet" / "config.yaml").write_text(REFUSING_CONFIG)
    (fixture_repo / "pipelines").mkdir(exist_ok=True)
    path = fixture_repo / "pipelines" / "p.yaml"
    path.write_text(ONE_GATE)
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "add config and pipeline")

    mgr = RunManager(fixture_repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# Real PRD\n\nA genuine PRD.\n")

    refusing = _Adapter(lands=())
    with pytest.raises(WorktreeUnavailableError) as exc:
        mgr.start("demo", path, use_judge=False, adapter_factory=lambda _n: refusing)

    msg = str(exc.value)
    assert "not writable" in msg
    assert "PreToolUse" in msg, "the park must explain why nothing recorded it"
    assert "NOT been moved or modified" in msg
    # It parked BEFORE any agent step: the only turns taken were the probe's.
    assert [c[0] for c in refusing.calls] == list(W.MECHANISMS)


def test_a_same_tree_run_is_not_probed_at_all(fixture_repo):
    """Nothing to discover: the operator edits that tree themselves."""
    from gauntlet.engine.run import RunManager
    from conftest import git

    (fixture_repo / ".gauntlet").mkdir(exist_ok=True)
    (fixture_repo / ".gauntlet" / "config.yaml").write_text(
        REFUSING_CONFIG.replace("mode: dedicated", "mode: same_tree")
    )
    (fixture_repo / "pipelines").mkdir(exist_ok=True)
    path = fixture_repo / "pipelines" / "p.yaml"
    path.write_text(ONE_GATE)
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "add config and pipeline")

    mgr = RunManager(fixture_repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# Real PRD\n\nA genuine PRD.\n")

    refusing = _Adapter(lands=())
    mgr.start("demo", path, use_judge=False, adapter_factory=lambda _n: refusing)
    assert refusing.calls == [], "a same_tree run must not pay for a probe"
