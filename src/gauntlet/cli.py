"""Gauntlet CLI entry point.

P3 adds the run lifecycle (`new`, `run`, `status`, `approve`, `reject`,
`resume`, `abort`, `rollback`); P6 adds `init` (idempotent scaffolding) and
`doctor` (environment validation).
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

import typer
from typer.core import TyperCommand

from gauntlet import __version__

app = typer.Typer(
    name="gauntlet",
    no_args_is_help=True,
    help="Adversarial multi-agent development harness.",
    # A genuinely unexpected traceback stays loud (it is a bug), but it must
    # not dump local variables — locals can carry tokens/paths the redaction
    # layer never sees (issue #21 hygiene).
    pretty_exceptions_show_locals=False,
)


def _known_user_errors() -> tuple[type[BaseException], ...]:
    """The operational-failure types the CLI reports as one line (issue #21).

    These are conditions a user can act on — a missing/malformed config, a
    guard that refused a verb, an unresolvable run — not bugs. Anything outside
    this tuple keeps its traceback: fail closed means a real defect stays
    loud, never laundered into a polite message. Imported lazily so the happy
    path keeps the CLI's deferred-import startup profile.
    """
    from gauntlet.engine.config import ConfigLoadError, ConfigNotFoundError
    from gauntlet.engine.operator import RunResolutionError, StatusContractError
    from gauntlet.engine.planphases import PlanPhasesError
    from gauntlet.engine.review import ReviewFailClosed
    from gauntlet.engine.run import (
        AbortGuardError,
        EntryContractError,
        RecoverError,
        RollbackGuardError,
        UnsafeRunSegment,
        WorktreeDirtyError,
    )

    return (
        ConfigNotFoundError,
        ConfigLoadError,
        EntryContractError,
        RollbackGuardError,
        AbortGuardError,
        RecoverError,
        UnsafeRunSegment,
        WorktreeDirtyError,
        RunResolutionError,
        StatusContractError,
        PlanPhasesError,
        ReviewFailClosed,
    )


def _friendly_errors(fn):
    """CLI error boundary (issue #21): known operational failures print
    ``error: <message>`` and exit 1 instead of a traceback.

    Commands that already map specific errors to specific exit codes keep
    doing so — their inner handlers run first; this boundary only catches what
    escapes them. Everything not in :func:`_known_user_errors` re-raises
    untouched (including ``typer.Exit``), so unexpected exceptions remain
    visibly a bug.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if isinstance(exc, _known_user_errors()):
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(1) from exc
            raise

    return wrapper

# Bare `--interactive` selects this monitor agent (FR-7.1). Mirrors
# interactive.DEFAULT_MONITOR_AGENT; a drift guard test pins them equal so the
# normalization default below never diverges from the launcher's validator.
_BARE_INTERACTIVE_VALUE = "claude"


def _normalize_interactive_argv(args: list[str]) -> list[str]:
    """Rewrite a bare ``--interactive`` token to ``--interactive=<default>``.

    `--interactive[=claude|codex]` is an optional-value flag (FR-7.1): bare →
    claude, ``--interactive=codex`` → codex. typer 0.26's vendored parser has no
    optional-value support (a value-bearing option always demands an argument),
    so we normalize the bare form here before the parser runs. Only an exact bare
    ``--interactive`` token before any ``--`` separator is rewritten;
    ``--interactive=<v>`` and tokens after ``--`` are left untouched.
    """
    out: list[str] = []
    after_separator = False
    for arg in args:
        if not after_separator and arg == "--":
            after_separator = True
        elif not after_separator and arg == "--interactive":
            out.append(f"--interactive={_BARE_INTERACTIVE_VALUE}")
            continue
        out.append(arg)
    return out


class _InteractiveCommand(TyperCommand):
    """A typer command whose ``--interactive`` is an optional-value flag (FR-7.1).

    typer 0.26's parser cannot express an option that is bare-or-valued, so this
    subclass normalizes a bare ``--interactive`` to ``--interactive=<default>``
    in :meth:`parse_args` before delegating to the normal parser. Everything else
    about the command is unchanged.
    """

    def parse_args(self, ctx, args):  # type: ignore[override]
        return super().parse_args(ctx, _normalize_interactive_argv(args))

judge_app = typer.Typer(no_args_is_help=True, help="Safety judge service (FR-7).")
app.add_typer(judge_app, name="judge")


@app.callback()
def main() -> None:
    """Adversarial multi-agent development harness."""


@app.command()
@_friendly_errors
def version() -> None:
    """Print the installed gauntlet version."""
    typer.echo(f"gauntlet {__version__}")


@app.command()
@_friendly_errors
def init(
    from_repo: bool = typer.Option(
        False, "--from-repo",
        help="The repo already carries committed Gauntlet assets; only ensure "
        "machine-local hook wiring + .gitignore guidance (team-adopter path).",
    ),
) -> None:
    """Scaffold config/pipeline/prompts/policy + hook wiring (FR-1.2, idempotent)."""
    from gauntlet.engine.init import init_repo

    result = init_repo(Path.cwd(), from_repo=from_repo)
    for a in result.actions:
        suffix = f" — {a.detail}" if a.detail else ""
        typer.echo(f"  {a.action:8} {a.path}{suffix}")
    if result.missing:
        typer.echo(
            "\nmissing committed assets (expected with --from-repo on a "
            "configured repo): " + ", ".join(a.path for a in result.missing)
        )
    typer.echo("\nnext: `gauntlet doctor`, then `gauntlet new <slug>` / `gauntlet run <slug>`")


@app.command()
@_friendly_errors
def doctor() -> None:
    """Validate the environment: CLIs, auth, hooks, judge, keys (FR-1.3, FR-1.5)."""
    from gauntlet.engine.doctor import FAIL, OK, WARN, has_failure, run_doctor

    glyph = {OK: "✓", WARN: "!", FAIL: "✗"}
    results = run_doctor(Path.cwd())
    for r in results:
        line = f"  {glyph.get(r.status, '?')} {r.name}: {r.detail}"
        typer.echo(line)
        if r.remedy and r.status in (WARN, FAIL):
            typer.echo(f"      → {r.remedy}")
    if has_failure(results):
        typer.echo("\ndoctor found blocking problems (see ✗ above)", err=True)
        raise typer.Exit(1)
    typer.echo("\nenvironment OK")


def _manager() -> "object":
    from gauntlet.engine.run import RunManager

    return RunManager(Path.cwd())


def _resolve_run_instance_dir(mgr, slug: str) -> Path:
    """Resolve <slug>'s run instance through the safe resolver (review F-002).

    Validates the slug, resolves the instance via the deterministic operator
    selection (``active-run.txt`` else lexically-greatest ``run-*``), and confirms
    it stays under the slug dir before any caller reads or attaches to it. Raises
    ``typer.Exit(1)`` on an unsafe slug/pointer, an unresolvable run, or an
    instance that escapes the run tree. Shared by ``status`` and ``status
    --interactive`` so both inherit the same FR-10.1 containment (the resolution
    never flows through the unchecked ``active_run_dir()``).
    """
    from gauntlet.engine import operator
    from gauntlet.engine.operator import RunResolutionError
    from gauntlet.engine.run import UnsafeRunSegment, safe_run_segment

    layout = mgr.layout(slug)
    try:
        safe_run_segment(slug, kind="slug")
        run_instance_dir = operator.resolve_run_instance(layout.slug_dir)
    except (UnsafeRunSegment, RunResolutionError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    # Containment is a two-link chain (F-002): resolving the run instance against
    # the slug dir alone is not enough — a `runs/<slug>` symlink pointing outside
    # the configured run_root resolves both the slug dir AND the instance to the
    # same escaped location, so the child-of-slug check passes vacuously. Prove
    # the slug dir is itself under the resolved run_root FIRST, then the instance
    # under the resolved slug dir, so neither link can escape the run tree.
    run_root = (mgr.repo_root / mgr.config.run_root).resolve()
    slug_dir = layout.slug_dir.resolve()
    try:
        slug_dir.relative_to(run_root)
        run_instance_dir.resolve().relative_to(slug_dir)
    except ValueError as exc:
        typer.echo(
            f"error: resolved run instance {run_instance_dir} escapes the run "
            f"tree for {slug!r}; refusing to read it",
            err=True,
        )
        raise typer.Exit(1) from exc
    return run_instance_dir


def _default_policy_path() -> Path:
    """`<asset_root>/policy.yaml` from the repo config (review F-005): a fresh
    adopter keeps the policy under `.gauntlet/`, so the bare `policy.yaml` default
    would not load. Falls back to the bare name when no config is present.

    Only the no-config case falls back (F-001): a *malformed* config raises
    ``ConfigLoadError``, which propagates to the ``_friendly_errors`` boundary so
    ``gauntlet judge serve`` surfaces it as a one-line ``error: invalid run
    config …`` instead of silently ignoring it and serving a bad policy path."""
    from gauntlet.engine.config import ConfigNotFoundError, RunConfig

    try:
        asset_root = RunConfig.load(Path.cwd() / ".gauntlet/config.yaml").asset_root
    except (ConfigNotFoundError, FileNotFoundError):
        asset_root = "."
    return Path.cwd() / asset_root / "policy.yaml"


@app.command()
@_friendly_errors
def new(slug: str) -> None:
    """Scaffold the run dir (run_root/<slug>/, default .gauntlet/runs/) with a human-authored PRD stub (FR-8.1, FR-10.1)."""
    manager = _manager()
    path = manager.new(slug)
    typer.echo(f"scaffolded {path}; author the PRD, then `gauntlet run {slug}`")
    # OQ-4 (decided "yes" in P3): a cheap, CLI-agnostic pointer to the authoring
    # aids, so the convention is reinforced on the `gauntlet new` path even outside
    # a skill-aware Claude session. It shapes no gate and no required acceptance
    # test; it is pure reinforcement of G1 (the playbook is otherwise inert).
    from gauntlet.engine import skill as S

    playbook = S.playbook_ref(manager.config.asset_root)
    typer.echo(
        f"  authoring help: open {playbook} for the playbook; in a Claude session "
        "the `gauntlet-prd-author` skill routes you there automatically."
    )


@app.command(cls=_InteractiveCommand)
@_friendly_errors
def run(
    slug: str,
    pipeline: str = typer.Option("standard", help="Pipeline name under pipelines/."),
    pipeline_file: Path = typer.Option(
        None, help="Explicit pipeline file path (overrides --pipeline)."
    ),
    no_judge: bool = typer.Option(
        False, "--no-judge", help="Do not start the judge (unsafe; testing only)."
    ),
    run_id: str = typer.Option(
        None, "--run-id",
        help="Pre-allocated run id (FR-6.1a handshake; the console supervisor "
        "passes this so it knows run_dir before launch). Single-use: errors if "
        "that run dir already exists.",
    ),
    reservation_token: str = typer.Option(
        None, "--reservation-token", hidden=True,
        help="Single-use reservation token paired with --run-id (FR-6.1a "
        "handshake). Set by the console supervisor before launch so this child "
        "may adopt the pre-created run dir; not for manual use.",
    ),
    watch: bool = typer.Option(
        False, "--watch", help="Ensure the supervisory console is running "
        "(boot/reuse it) and print its URL before running in the foreground "
        "(FR-12.1).",
    ),
    interactive: str = typer.Option(
        None, "--interactive",
        help="Launch the run DETACHED and hand the terminal to an interactive "
        "monitor agent (bare → claude; --interactive=codex for codex). The "
        "monitor is wired to the run's judge as the operator's own session when "
        "the judge is live and the driver is alive, else a normal prompted "
        "session (FR-7). Composes with --watch.",
    ),
    console_host: str = typer.Option(
        "127.0.0.1", "--console-host", help="Console bind host for --watch.",
    ),
    console_port: int = typer.Option(
        8765, "--console-port", help="Console bind port for --watch.",
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="With --watch, do not open a browser; just "
        "print the console URL (FR-1). Also honored when not on a TTY.",
    ),
) -> None:
    """Start a run on branch gauntlet/<slug> (FR-8.1)."""
    mgr = _manager()
    if interactive is not None:
        # --interactive launches the run detached + foregrounds the monitor; the
        # pre-allocation/handshake is owned by the launch path, so the manual
        # --run-id/--reservation-token and --pipeline-file knobs do not apply.
        if run_id is not None or reservation_token is not None:
            typer.echo(
                "error: --run-id/--reservation-token are managed automatically by "
                "--interactive (it pre-allocates the run-id + reservation token)",
                err=True,
            )
            raise typer.Exit(2)
        if pipeline_file is not None:
            typer.echo(
                "error: --pipeline-file is not supported with --interactive; use "
                "--pipeline <name>",
                err=True,
            )
            raise typer.Exit(2)
        _run_interactive(
            mgr, slug, agent=interactive, pipeline=pipeline, no_judge=no_judge,
            watch=watch, console_host=console_host, console_port=console_port,
            no_browser=no_browser,
        )
        return
    if watch:
        _ensure_watch_console(
            mgr, host=console_host, port=console_port, no_browser=no_browser
        )
    path = pipeline_file or (Path.cwd() / mgr.config.asset_root / "pipelines" / f"{pipeline}.yaml")
    status = mgr.start(
        slug, path, use_judge=not no_judge, run_id=run_id,
        reservation_token=reservation_token,
    )
    typer.echo(f"run status: {status}")


@app.command()
@_friendly_errors
def review(
    branch: str = typer.Argument(
        None, help="Local branch to review (default: the current branch)."
    ),
    pr: str = typer.Option(
        None, "--pr",
        help="GitHub PR number or URL: check it out locally and review it "
        "against its base + linked ticket, landing fixes locally (FR-4).",
    ),
    issue: str = typer.Option(
        None, "--issue", help="Issue tracker ref/URL (e.g. ENG-1234) for intent."
    ),
    intent: Path = typer.Option(
        None, "--intent", help="Path to a problem-statement file for intent."
    ),
    message: str = typer.Option(
        None, "-m", "--message", help="Inline problem statement for intent."
    ),
    intent_provenance: str = typer.Option(
        None, "--intent-provenance",
        help="Independence of a manual intent: tracker | tracker-session | "
        "author-session-summary (default author-session-summary). Rejected with "
        "--issue (always 'tracker').",
    ),
    approved_intent: bool = typer.Option(
        False, "--approved-intent",
        help="Assert a non-independent intent was ratified out of band (the "
        "non-interactive form of the FR-2.5 ratification hook).",
    ),
    base: str = typer.Option(
        None, "--base", help="Diff base ref (default: config.base_branch or origin/HEAD).",
    ),
    code_only: bool = typer.Option(
        False, "--code-only", help="Diff-only review with no intent (FR-2.3)."
    ),
    rounds: int = typer.Option(
        1, "--rounds", help="Adversarial-cycle rounds, 1..10 (default 1)."
    ),
    test: bool = typer.Option(
        None, "--test/--no-test",
        help="Run config.test_command as a baseline step first (off by default).",
    ),
    response: str = typer.Option(
        None, "--response",
        help="Resume a parked/failed review with a human decision (FR-3.2/FR-10.4).",
    ),
) -> None:
    """Adversarially review a change in place against its originating intent.

    Runs only the adversarial review cycle (review → triage → fix → confirm)
    against an already-implemented change on a branch, landing accepted fixes as
    REVIEW.x commits in place (no branch minted, nothing pushed). Zero routine
    gates; an unresolved blocking finding parks the run (resume with --response),
    an unresolved legitimate non-blocking finding completes and is surfaced as
    residual risk (FR-3.4).
    """
    from gauntlet.engine import manifest as M
    from gauntlet.engine.review import (
        Hooks,
        ReviewInputs,
        ReviewLifecycle,
        ReviewUsageError,
        ReviewFailClosed,
        drive_review,
        load_review_run,
        resume_review,
    )

    mgr = _manager()
    inputs = ReviewInputs(
        branch=branch,
        pr=pr,
        issue=issue,
        intent_path=str(intent) if intent is not None else None,
        message=message,
        intent_provenance=intent_provenance,
        approved_intent=approved_intent,
        base=base,
        code_only=code_only,
        rounds=rounds,
        test=test,
    )
    hooks = Hooks(
        isatty=sys.stdin.isatty,
        edit_statement=lambda text, _root: typer.edit(text) or text,
        confirm_statement=lambda text: typer.confirm(
            "Ratify this problem statement and start the review?", default=False
        ),
    )
    lifecycle = ReviewLifecycle(mgr.repo_root, mgr.config, hooks=hooks)
    try:
        # Locate the (side-effect-free) state dir first so an existing parked/
        # running review is resumed, not clobbered by a fresh resolution.
        _target, _slug, state_dir = lifecycle.locate(inputs)
        existing = load_review_run(state_dir)
        if response is not None or existing is not None:
            if existing is None:
                typer.echo(
                    f"review cannot proceed: no resumable review run at {state_dir} "
                    "(nothing to resume). Run `gauntlet review` without --response "
                    "to start one.",
                    err=True,
                )
                raise typer.Exit(1)
            outcome = resume_review(
                mgr.repo_root, mgr.config, state_dir, response=response,
            )
        else:
            resolution = lifecycle.resolve(inputs)
            outcome = drive_review(mgr.repo_root, mgr.config, resolution)
    except ReviewUsageError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ReviewFailClosed as exc:
        typer.echo(f"review cannot proceed: {exc}", err=True)
        raise typer.Exit(1) from exc

    _render_review_outcome(outcome, M)


def _render_review_outcome(outcome, M) -> None:
    """Print a review run's terminal state: status, REVIEW.x commits, residual
    risk / declined findings (FR-3.4), and the state dir."""
    typer.echo(f"review {outcome.status} (branch operated on in place)")
    # PR-mode notes (FR-4.3/FR-4.4): the chosen linked ticket + any ignored
    # secondary refs, and the fork manual-push note, surfaced in the summary.
    if outcome.pr_chosen_ref:
        typer.echo(f"  PR intent from linked ticket {outcome.pr_chosen_ref}")
    if outcome.pr_ignored_refs:
        typer.echo(
            "  warning: PR body links multiple tickets; using "
            f"{outcome.pr_chosen_ref}. Ignored secondary refs: "
            f"{', '.join(outcome.pr_ignored_refs)} (override with --issue)."
        )
    if outcome.pr_is_fork:
        typer.echo(
            "  fork PR: fixes landed locally; push-back is your action and may "
            "need maintainer-edit access on the PR (FR-4.4)."
        )
    if outcome.commits:
        typer.echo(f"  landed {len(outcome.commits)} fix commit(s):")
        for phase, sha in outcome.commits:
            typer.echo(f"    {phase}: {sha[:10]}")
    else:
        typer.echo("  no fix commits landed")

    summary = outcome.summary
    if summary.residual_risk:
        typer.echo(
            f"  residual risk — {len(summary.residual_risk)} legitimate "
            "non-blocking finding(s) not fully resolved (surface on the PR):"
        )
        for f in summary.residual_risk:
            cv = f.confirm_verdict or "not confirmed"
            typer.echo(f"    [{f.severity}] {f.id} @ {f.location}: {f.claim} ({cv})")
    if summary.declined:
        typer.echo(f"  declined — {len(summary.declined)} finding(s) not fixed:")
        for f in summary.declined:
            typer.echo(
                f"    [{f.severity}] {f.id} ({f.triage_verdict}): {f.triage_reasoning}"
            )

    if outcome.parked:
        typer.echo(
            "  PARKED on an unresolved blocking finding (fail closed, FR-3.2); "
            'resume with `gauntlet resume --response "<decision>"`.'
        )
        if outcome.cycle_notes:
            typer.echo(f"  reason: {outcome.cycle_notes}")
    typer.echo(f"  state: {outcome.state_dir}")
    # Any non-DONE terminal state is a non-zero exit: a park (fail closed, FR-3.2),
    # a failure, or a budget/timeout halt — never a silent exit 0 on an incomplete
    # review (data over inference).
    if outcome.status != M.RUN_DONE:
        raise typer.Exit(1)


def _run_interactive(
    mgr, slug: str, *, agent: str, pipeline: str, no_judge: bool, watch: bool,
    console_host: str, console_port: int, no_browser: bool = False,
) -> None:
    """`gauntlet run <slug> --interactive`: detached run + foreground monitor (FR-7).

    Validates the monitor agent BEFORE any launch (FR-7.1), optionally boots the
    --watch console (composes), pre-allocates a run-id + single-use reservation
    token and launches the run DETACHED via the sanctioned RunProcess handshake
    (FR-7.2, reusing the console supervisor's launch path), then foregrounds the
    shared monitor on that run's dir (FR-7.3). The monitor exec replaces this
    process; the detached run keeps running.
    """
    from gauntlet import interactive as interactive_mod
    from gauntlet.web.supervisor import JobSupervisor

    # Unknown agent errors before any launch, naming the valid choices (FR-7.1).
    try:
        interactive_mod.validate_monitor_agent(agent)
    except interactive_mod.MonitorAgentError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    if watch:
        _ensure_watch_console(
            mgr, host=console_host, port=console_port, no_browser=no_browser
        )

    # Pre-allocate run-id + reservation token and launch DETACHED via RunProcess
    # (FR-7.2) — the same FR-6.1a handshake the console supervisor uses.
    supervisor = JobSupervisor(mgr.repo_root, mgr.config)
    rp = supervisor.launch_run(slug, pipeline=pipeline, no_judge=no_judge)
    typer.echo(
        f"run launched detached: {slug}/{rp.run_id} (log: {rp.log_path})"
    )

    # Foreground the operator monitor on the just-launched run (FR-7.3). This
    # execs and replaces the process; the detached run keeps running.
    repo_root = mgr.repo_root.resolve()
    run_root = repo_root / mgr.config.run_root
    interactive_mod.launch_monitor(
        repo_root=repo_root,
        run_root=run_root,
        slug=slug,
        run_dir=rp.run_dir,
        agent=agent,
        use_judge=not no_judge,
        asset_root=mgr.config.asset_root,
    )


def _ensure_watch_console(mgr, *, host: str, port: int, no_browser: bool = False) -> None:
    """Boot/reuse the detached console for `run --watch` and open it (FR-12.1/FR-1).

    Fail-soft: the console is a convenience surface, so a boot failure (e.g. an
    unrelated process on the port) is surfaced loudly but does **not** abort the
    run — the foreground pipeline still runs exactly as today. The booted console
    is detached and persists after the foreground run returns (FR-12.2). On a TTY
    (unless ``--no-browser``) the operator's browser is opened to an already
    authenticated ``?p=`` URL so there is no token to paste (FR-1, goal G1).
    """
    from gauntlet.web.launch import open_authenticated
    from gauntlet.web.registry import ConsoleBootError, ensure_console

    repo_root = mgr.repo_root.resolve()
    run_root = repo_root / mgr.config.run_root
    try:
        handle = ensure_console(repo_root, run_root, host=host, port=port)
    except ConsoleBootError as exc:
        typer.echo(f"warning: {exc}", err=True)
        typer.echo("continuing without a --watch console.", err=True)
        return
    if handle.reused:
        typer.echo("reusing the running console")
        if handle.token_mismatch:
            typer.echo(
                "note: the running console uses a different token than your "
                "GAUNTLET_WEB_TOKEN; it was not restarted — sign in with the "
                "console's own token (FR-12.4).",
                err=True,
            )
    else:
        typer.echo("console started")
        if handle.token:
            typer.echo(f"GAUNTLET_WEB_TOKEN={handle.token}", err=True)
    # Surface (and on a TTY open) the authenticated landing URL (FR-1); fail-soft.
    open_authenticated(handle, no_browser=no_browser, echo=typer.echo)


@app.command(cls=_InteractiveCommand)
@_friendly_errors
def status(
    slug: str,
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit the run state as a single JSON object conforming to "
        "schemas/status.json (FR-4) — the stable machine contract for an agent "
        "or script. Stdout is only the JSON; exits non-zero only on an actual "
        "error (a parked/failed run is a valid state, exit 0).",
    ),
    interactive: str = typer.Option(
        None, "--interactive",
        help="Attach an interactive monitor agent to the EXISTING run (bare → "
        "claude; --interactive=codex for codex). Starts no new run; foregrounds "
        "the same monitor as `run --interactive`, wired to the run's judge as the "
        "operator's own session when the driver is alive, else a normal prompted "
        "session for diagnosis (FR-8).",
    ),
) -> None:
    """Show the current run status for <slug> with driver liveness + next action.

    Read-only (FR-1/FR-2): reports the computed driver liveness and the concrete
    next action for the run's composite state. It never writes — a surviving
    recovery intent is *reported*, never finalized (FR-5.6). With ``--json`` it
    emits the same computed state as a lone, schema-stable JSON object (FR-4).
    With ``--interactive`` it foregrounds a monitor agent attached to the run
    instead of rendering status (FR-8); the two output modes are exclusive.
    """
    import json

    from gauntlet.engine import operator
    from gauntlet.engine.manifest import Manifest
    from gauntlet.engine.operator import StatusContractError

    mgr = _manager()
    if interactive is not None:
        # `--interactive` attaches a foreground monitor (FR-8) instead of
        # rendering status; combining it with the `--json` machine contract is
        # nonsensical, so reject the pair rather than silently picking one.
        if json_output:
            typer.echo(
                "error: --interactive and --json are mutually exclusive; "
                "--interactive foregrounds a monitor agent, --json emits the "
                "machine status contract",
                err=True,
            )
            raise typer.Exit(2)
        _status_interactive(mgr, slug, agent=interactive)
        return

    # FR-10.1 containment: validate the slug, resolve the instance through the
    # safe resolver, and confirm it stays under the slug dir BEFORE reading the
    # manifest or a recovery intent — never via the unchecked `active_run_dir()`
    # (F-002). Shared with `status --interactive` via `_resolve_run_instance_dir`.
    run_instance_dir = _resolve_run_instance_dir(mgr, slug)

    # A missing/unreadable/invalid manifest is an actual error (FR-4.3 — exit
    # non-zero), surfaced on stderr so `--json` stdout stays a lone object (or
    # empty on error), never an interleaved traceback.
    try:
        man = Manifest.load(run_instance_dir / "manifest.json")
    except (OSError, ValueError) as exc:
        typer.echo(
            f"error: cannot load manifest for {slug!r}: {exc}", err=True
        )
        raise typer.Exit(1) from exc

    run_root = mgr.repo_root / mgr.config.run_root
    driver = operator.driver_info(run_root, slug)
    # A persisted-state contract violation (a non-canonical iteration, an unsafe
    # step id, or a payload that fails schema validation) is an actual error
    # (FR-4.3 — exit non-zero) surfaced on stderr, so `--json` stdout stays empty
    # rather than a contract-breaking object (operator F-001/F-002/F-003).
    try:
        from gauntlet.engine.pipeline import (
            load_pipeline,
            upstream_cycle_id_for_gate,
        )

        # The run's own pipeline snapshot (FR-5.6), loaded once and reused for the
        # gate→cycle resolution, the effective-timeout lookup, and the gate context
        # below. Fail-soft to None (a corrupt/absent snapshot degrades an advisory
        # field, never crashes status).
        try:
            pipeline, _ = load_pipeline(run_instance_dir / "pipeline.yaml")
        except (OSError, ValueError):
            pipeline = None

        rstate = operator.compute_run_state(man, driver.state)
        # FR-8.2 / F-001: when parked at a gate, name the cycle a reject would
        # ACTUALLY re-drive — resolved from the pipeline snapshot with the same
        # rule as the reject path (same non-foreach stage), not the manifest-order
        # heuristic which can name a cross-stage/foreach cycle a reject never
        # touches. Recompute the actions with the pipeline-resolved id (fail-soft
        # to the pure default when the snapshot is unavailable).
        if (
            pipeline is not None
            and rstate.state == operator.STATE_PARKED_GATE
            and rstate.parked is not None
        ):
            gate_rec0 = next(
                (r for r in man.steps
                 if operator.render_step_id(r) == rstate.parked.step_id),
                None,
            )
            if gate_rec0 is not None:
                rstate = operator.compute_run_state(
                    man, driver.state,
                    gate_cycle_id=upstream_cycle_id_for_gate(pipeline, gate_rec0.id),
                )
        recon, anomaly = operator.read_recovery_intent(run_root, run_instance_dir, slug)

        # Advisory freshness (live-run-observability FR-5): the single I/O point
        # (a stat of the running step's events.jsonl), gated on the streaming
        # flag, computed here and threaded into the pure renderers below so both
        # the JSON contract and the human footer report the same value. None for
        # a non-streamed / not-applicable step (→ `current_step_freshness: null`).
        freshness = operator.compute_current_step_freshness(
            man, run_instance_dir,
            streaming=bool(getattr(mgr.config, "stream_step_output", False)),
        )

        # Suspend/sleep view (FR-5.3): heartbeat age, detected intervals, and the
        # stall classification, sampled from disk here and threaded into the pure
        # renderers so the JSON contract and human footer report the same value.
        suspension = operator.compute_suspension_view(
            man, run_instance_dir, driver.state,
            agent_silence_s=getattr(mgr.config, "agent_silence_s",
                                    operator.HB.DEFAULT_AGENT_SILENCE_S),
            interval_s=getattr(mgr.config, "heartbeat_interval_s",
                               operator.HB.DEFAULT_HEARTBEAT_INTERVAL_S),
        )

        # Timing/usage inputs (FR-7.1/FR-7.3), sampled once here (the clock is the
        # single non-pure input) and threaded into both the JSON serializer and the
        # human footer so the two never diverge. The current running step's
        # effective timeout is resolved from the persisted pipeline snapshot using
        # the SAME precedence as execution (per-step `timeout_s` → profile
        # `step_timeout_s`), so a per-step override or a shell step's own timeout is
        # reported, not a profile-only guess (F-003). Fail closed to null (advisory
        # field) when the snapshot or the step is unresolvable.
        from datetime import datetime as _dt, timezone as _tz

        from gauntlet.engine.steptypes import resolve_step_timeout_s

        now = _dt.now(_tz.utc)
        current_step_timeout_s = None
        if rstate.current_step:
            cur = next(
                (r for r in man.steps
                 if operator.render_step_id(r) == rstate.current_step),
                None,
            )
            if cur is not None and cur.status == "running":
                pstep = next(
                    (s for s in pipeline.all_steps() if s.id == cur.id), None
                ) if pipeline is not None else None
                if pstep is not None:
                    current_step_timeout_s = resolve_step_timeout_s(
                        pstep, cur.agent, mgr.config
                    )

        # Gate decision context (FR-8.1): assembled only when parked at a human
        # gate, from the manifest + the upstream cycle's persisted artifacts (the
        # I/O point), then threaded into the pure serializer below like the other
        # sampled inputs. None for every other state → `gate: null`. The pipeline
        # snapshot resolves the upstream cycle (F-001) and the configured redaction
        # list masks configured secrets (F-002); both are threaded through.
        gate_ctx = None
        if rstate.state == operator.STATE_PARKED_GATE and rstate.parked is not None:
            gate_rec = next(
                (r for r in man.steps
                 if operator.render_step_id(r) == rstate.parked.step_id),
                None,
            )
            if gate_rec is not None:
                gate_ctx = operator.compute_gate_context(
                    man, run_instance_dir, gate_rec,
                    pipeline=pipeline, redaction=mgr.config.redaction,
                )

        if json_output:
            # A single JSON object on stdout, no interleaved log lines (FR-4.3). A
            # malformed surviving intent is a human-footer anomaly only, so `recon`
            # is None there and `reconciliation` is null — never a fabricated object.
            payload = operator.status_payload(
                man, driver, rstate, recon,
                run_root=run_root, run_instance_dir=run_instance_dir,
                current_step_freshness=freshness,
                suspension=suspension,
                gate=gate_ctx,
                now=now,
                current_step_timeout_s=current_step_timeout_s,
            )
            typer.echo(json.dumps(payload, indent=2))
            return
    except StatusContractError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"{man.slug}: {man.status} (current step: {man.current_step})")
    for rec in man.steps:
        it = f"[{rec.iteration}]" if rec.iteration is not None else ""
        typer.echo(f"  {rec.id}{it}: {rec.status}")

    # FR-7.3 footer enrichment: elapsed, cost-so-far, and — when parked on a
    # usage limit — the reset time, all sourced from the manifest so no parked
    # state requires reading a transcript to identify the next command.
    quota_reset_at = None
    if rstate.state in (
        operator.STATE_PARKED_USAGE_LIMIT, operator.STATE_PARKED_USAGE_WINDOW
    ) and rstate.parked is not None:
        pr = next(
            (r for r in man.steps
             if operator.render_step_id(r) == rstate.parked.step_id),
            None,
        )
        quota_reset_at = pr.quota_reset_at if pr is not None else None
    for line in operator.render_footer(
        driver, rstate, reconciliation=recon, anomaly=anomaly,
        current_step_freshness=freshness, suspension=suspension,
        run_elapsed_s=operator._run_elapsed_s(man, now),
        cost_usd=man.totals.cost_usd,
        quota_reset_at=quota_reset_at,
    ):
        typer.echo(line)


def _status_interactive(mgr, slug: str, *, agent: str) -> None:
    """`gauntlet status <slug> --interactive`: attach the monitor to an EXISTING run (FR-8).

    Resolves the run instance with the same deterministic selection operator-aids
    uses (``active-run.txt`` else lexically-greatest ``run-*``, via the shared safe
    resolver); an unknown/absent run errors. Starts **no** ``RunProcess`` — the
    run already exists — and only foregrounds the shared P3 monitor, reusing
    ``build_monitor_command`` unchanged so the attach path inherits the exact same
    argv/env/prompt launch contract (review F-002). Judge wiring follows
    ``driver_liveness`` inside ``launch_monitor`` (FR-8.2): the operator-session
    env (§6.3) only when the driver is alive **and** ``judge.json`` is readable,
    else a normal prompted session for diagnosis (the agent can still read
    ``status``/``logs`` and ``resume``).
    """
    from gauntlet import interactive as interactive_mod

    # An unknown agent value errors BEFORE any resolution/launch, naming the valid
    # choices (FR-7.1) — never half-attach to a run.
    try:
        interactive_mod.validate_monitor_agent(agent)
    except interactive_mod.MonitorAgentError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    run_instance_dir = _resolve_run_instance_dir(mgr, slug)

    # The resolver proves containment but not that the directory is a real run
    # (F-001): a stale reservation or a hand-made `runs/<slug>/run-*` with no
    # manifest would otherwise launch a monitor against a non-run, violating the
    # FR-8.1 unknown/absent-run error contract. Load and validate the manifest
    # with the same handling as the normal `status` path BEFORE foregrounding the
    # agent, and confirm it is the manifest for this slug + this instance.
    from gauntlet.engine.manifest import Manifest

    try:
        man = Manifest.load(run_instance_dir / "manifest.json")
    except (OSError, ValueError) as exc:
        typer.echo(
            f"error: cannot load manifest for {slug!r}: {exc}", err=True
        )
        raise typer.Exit(1) from exc
    if man.slug != slug or man.run_id != run_instance_dir.name:
        typer.echo(
            f"error: manifest in {run_instance_dir} does not match run "
            f"{slug}/{run_instance_dir.name} (got {man.slug}/{man.run_id}); "
            "refusing to attach",
            err=True,
        )
        raise typer.Exit(1)

    repo_root = mgr.repo_root.resolve()
    run_root = repo_root / mgr.config.run_root
    # No RunProcess: the run already exists (FR-8.1). `judge_wait_s=0` — unlike
    # `run --interactive`'s detached launch, there is no startup race to wait
    # through: an already-running run's judge has long since written `judge.json`,
    # so a missing record means the driver is not serving a live judge and we
    # degrade to a prompted session at once rather than blocking the operator.
    interactive_mod.launch_monitor(
        repo_root=repo_root,
        run_root=run_root,
        slug=slug,
        run_dir=run_instance_dir,
        agent=agent,
        use_judge=True,
        asset_root=mgr.config.asset_root,
        judge_wait_s=0.0,
    )


@app.command()
@_friendly_errors
def logs(
    slug: str,
    step: str = typer.Option(
        None, "--step",
        help="Step to show (default: the deterministically-selected last "
        "non-done step). A top-level rendered id (`<id>` or `<id>.<iteration>`), "
        "or a composite role sub-leaf path (`<cycle-leaf>/r2-fix`, "
        "`<cycle-leaf>/r1-triage/<finding-id>`).",
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="Tail the step's events.jsonl live, printing appended events as "
        "they arrive and exiting when the step ends or on Ctrl-C. A finished "
        "step degrades to a one-shot dump (no hang).",
    ),
) -> None:
    """Surface a step's evidence: its dir + transcript tail (read-only, FR-3).

    Resolves the run-instance and step deterministically from run metadata
    (never mtime), prints the resolved dirs, the last 200 lines of the step's
    transcript, and names the `events.jsonl` path (never parsed). It writes
    nothing and reads only within the run tree; a missing/unreadable transcript
    is a notice, not an error (exit 0).

    With `--follow`, instead of the transcript tail it streams the step's
    `events.jsonl` live (the per-line redacted on-disk file, never the raw pipe),
    exiting cleanly when the step ends or on SIGINT (live-run-observability FR-3).
    """
    from gauntlet.engine import operator
    from gauntlet.engine.operator import (
        LogsError,
        RunResolutionError,
        StatusContractError,
    )
    from gauntlet.engine.run import UnsafeRunSegment

    mgr = _manager()
    layout = mgr.layout(slug)
    run_root = mgr.repo_root / mgr.config.run_root

    if follow:
        try:
            fr = operator.follow_logs(
                run_root, layout.slug_dir, slug, step=step,
                emit=lambda text: typer.echo(text, nl=False),
            )
        except (
            UnsafeRunSegment, RunResolutionError, LogsError, StatusContractError
        ) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        if fr.interrupted:
            typer.echo("")  # finish the partial line SIGINT cut off
        return

    try:
        result = operator.resolve_logs(run_root, layout.slug_dir, slug, step=step)
    except (UnsafeRunSegment, RunResolutionError, LogsError, StatusContractError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"run instance: {result.run_instance_dir}")
    typer.echo(f"step: {result.step_id} ({result.step_status})")
    typer.echo(f"step dir: {result.transcript_dir}")
    typer.echo(f"events: {result.events_path}")
    if result.transcript_lines is None:
        typer.echo(result.notice)
        return
    suffix = f" (last {operator.TRANSCRIPT_TAIL_LINES} lines)" if result.truncated else ""
    typer.echo(f"transcript: {result.transcript_path}{suffix}")
    typer.echo("--- transcript ---")
    for line in result.transcript_lines:
        typer.echo(line)


@app.command()
@_friendly_errors
def approve(
    slug: str,
    gate: str = typer.Option(None, "--gate", help="Gate step id (default: current)."),
    notes: str = typer.Option(None, help="Approval notes."),
    no_judge: bool = typer.Option(False, "--no-judge"),
) -> None:
    """Approve a parked human_gate and continue the run (FR-8.1)."""
    typer.echo(f"run status: {_manager().approve(slug, gate, notes, use_judge=not no_judge)}")


@app.command()
@_friendly_errors
def reject(
    slug: str,
    notes: str = typer.Option(..., help="Why the gate was rejected."),
    gate: str = typer.Option(None, "--gate", help="Gate step id (default: current)."),
    no_judge: bool = typer.Option(
        False, "--no-judge",
        help="Drive the re-driven cycle without the judge (testing/diagnosis only; "
        "the judge is the safety layer).",
    ),
) -> None:
    """Reject a parked human_gate (FR-8.1).

    A rejection is not a dead end: when the gate sits downstream of an
    adversarial_cycle (the PRD/plan review loops), the note is injected into that
    cycle as a new fix round and the run is re-driven, re-parking the gate for a
    fresh decision. Re-drives agents, so it honors the judge like `approve`. Only
    a gate with no upstream cycle to iterate ends the run (terminal reject).
    """
    typer.echo(
        f"run status: {_manager().reject(slug, notes, gate, use_judge=not no_judge)}"
    )


def _locate_review_run(mgr, slug: str) -> Path | None:
    """The out-of-repo state dir of a resumable review run named by ``slug``, else None.

    A review run's on-disk ``<slug>`` is `review_slug(<target-branch>)`, so accept
    either the review slug itself or a raw branch name (which is sanitized to that
    slug). Returns the state dir only when a *bound, non-terminal* review run lives
    there (``load_review_run``), so a slug that collides with a heavyweight run —
    or a review run that never bound / already finished — falls through to the
    heavyweight resume path unchanged."""
    import os

    from gauntlet.engine.review import (
        ReviewFailClosed,
        derive_repo_id,
        load_review_run,
        resolve_state_dir,
        review_slug,
    )

    repo_id = derive_repo_id(mgr.repo_root)
    seen: set[str] = set()
    for candidate in (slug, review_slug(slug)):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            state_dir = resolve_state_dir(
                mgr.repo_root, mgr.config,
                repo_id=repo_id, slug=candidate, environ=os.environ,
            )
        except ReviewFailClosed:
            continue
        if load_review_run(state_dir) is not None:
            return state_dir
    return None


def _resume_review_cli(mgr, state_dir: Path, *, response: str | None, no_judge: bool) -> None:
    """Resume a parked/failed review run and render its terminal outcome (FR-3.2)."""
    from gauntlet.engine import manifest as M
    from gauntlet.engine.review import ReviewFailClosed, resume_review

    try:
        outcome = resume_review(
            mgr.repo_root, mgr.config, state_dir,
            response=response, use_judge=not no_judge,
        )
    except ReviewFailClosed as exc:
        typer.echo(f"resume cannot proceed: {exc}", err=True)
        raise typer.Exit(1) from exc
    _render_review_outcome(outcome, M)


@app.command()
@_friendly_errors
def resume(
    slug: str,
    response: str = typer.Option(
        None, "--response",
        help='Human decision for a step parked awaiting one (FR-10.4): a builder '
             'UPSTREAM CONFLICT (agent_task) re-runs with this injected; a '
             'reviewer-surfaced cycle escalation (adversarial_cycle) re-drives '
             'with it injected into the reviewer/triager so they re-evaluate the '
             'parked finding. Required to resume either; passed verbatim, no '
             'parsing.',
    ),
    no_judge: bool = typer.Option(False, "--no-judge"),
    reset_interrupted: bool = typer.Option(
        False, "--reset-interrupted",
        help="One-shot: discard an INTERRUPTED step's partial work and re-run "
             "it cleanly (#72). Preserves the partial work as a complete "
             "recovery snapshot under refs/gauntlet/recovery/ first and "
             "rewinds only to the latest committed checkpoint (never past "
             "committed milestones). Applies to this resume only — the "
             "configured interrupted_step policy is unchanged. A no-op when "
             "nothing is interrupted-dirty.",
    ),
) -> None:
    """Resume an interrupted run at its last incomplete step (FR-8.2).

    For a step parked awaiting a human decision — a builder UPSTREAM CONFLICT or
    an adversarial_cycle escalation its own loop cannot resolve (FR-10.4/10.5) —
    supply `--response "<decision>"` to record it (audited in the manifest) and
    re-drive with it injected. Other parks resume as before.

    A lightweight `gauntlet review` run keeps its state out-of-repo (not in
    run_root), so when the slug names a resumable review run this routes to the
    review resume path — the PRD-documented recovery for a parked review is
    `gauntlet resume --response` (FR-3.2), not only `gauntlet review --response`.
    """
    mgr = _manager()
    review_dir = _locate_review_run(mgr, slug)
    if review_dir is not None:
        _resume_review_cli(mgr, review_dir, response=response, no_judge=no_judge)
        return
    try:
        status = mgr.resume(
            slug, response=response, use_judge=not no_judge,
            reset_interrupted=reset_interrupted,
        )
    except ValueError as exc:
        # A terminal/parked run resume cannot proceed: surface WHY + the next
        # verb on stderr and exit non-zero — never silently print a status and
        # exit 0 (the contradiction `status` recommended `resume` papered over).
        typer.echo(f"resume cannot proceed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"run status: {status}")
    _echo_interrupted_park_detail(mgr, slug, status)


def _echo_interrupted_park_detail(mgr, slug: str, status: str) -> None:
    """After a resume that ends parked, say WHY — never a bare status (#65).

    A dirty-base insta-park does zero agent work and used to print only
    `run status: parked` (exit 0), sending the operator straight back into the
    resume loop. The park's evidence already lives in the step notes (the dirty
    verdict + offending commit range); surface it here — on stdout, with the
    status line it explains: a park after a successful resume is an outcome,
    not an error (stderr is this CLI's refusal/error channel). Best-effort
    read-only reporting: a failure to load the manifest never masks the resume
    outcome.
    """
    from gauntlet.engine import manifest as M

    if status != M.RUN_PARKED:
        return
    try:
        run_dir = mgr.layout(slug).active_run_dir()
        man = M.Manifest.load(run_dir / "manifest.json")
    except Exception:
        return
    noted = [
        s for s in man.steps
        if s.status in (M.INTERRUPTED, M.HALTED) and s.notes
    ]
    if not noted:
        return
    step = noted[-1]
    typer.echo(f"step {step.id}: {step.notes}")


@app.command()
@_friendly_errors
def abort(slug: str) -> None:
    """Abort a run (FR-8.1)."""
    typer.echo(f"run status: {_manager().abort(slug)}")


@app.command()
@_friendly_errors
def recover(
    slug: str,
    reason: str = typer.Option(
        None, "--reason",
        help="Optional operator note recorded verbatim in the recovery audit "
        "record (§6.4); omitted ⇒ recorded as null.",
    ),
) -> None:
    """Terminate a verified, wedged live driver and mark its step INTERRUPTED (FR-5).

    Operator-only and fail-closed: signals only a process it can prove via
    process identity is the same driver it launched — on this host, still in the
    recorded process group — never a recycled, foreign-host, or unverifiable PID.
    Fills the gap `resume` cannot (a *live* lock is never reclaimed). It does
    **not** auto-resume: run `gauntlet resume <slug>` afterwards. Refuses inside a
    pipeline-agent context.
    """
    from gauntlet.engine.operator import RunResolutionError
    from gauntlet.engine.run import RecoverError, UnsafeRunSegment

    mgr = _manager()
    try:
        status = mgr.recover(slug, reason=reason)
    except (RecoverError, UnsafeRunSegment, RunResolutionError, FileNotFoundError) as exc:
        typer.echo(f"recover refused: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"run status: {status}")


@app.command()
@_friendly_errors
def clean(
    slug: str,
    force: bool = typer.Option(
        False, "--force",
        help="Delete the branch even if it is not merged into its base (unsafe).",
    ),
) -> None:
    """Delete a merged run branch + clear its pointer; keep the run record.

    Refuses unless `gauntlet/<slug>` is fully merged into its recorded base
    (pass --force to override). Never touches the committed run dir.
    """
    typer.echo(_manager().clean(slug, force=force))


@app.command()
@_friendly_errors
def finish(slug: str) -> None:
    """Merge a completed run into its base, then delete the branch + pointer.

    Requires the run to be done and the worktree clean; a merge conflict is
    aborted and surfaced for a manual merge.
    """
    typer.echo(_manager().finish(slug))


@app.command()
@_friendly_errors
def report(
    slug: str,
    trend: bool = typer.Option(
        False, "--trend", help="Also show cross-run improvement metrics (FR-6.6)."
    ),
) -> None:
    """Print the per-step / per-agent-profile cost breakdown for a run (FR-3.2).

    With ``--trend``, also print the cross-run improvement metrics (findings per
    round, %legitimate, fix-survival, test loops, judge ask-rate, cost/phase).
    """
    from gauntlet.engine.report import render_report
    from gauntlet.engine.trend import render_trend

    mgr = _manager()
    man = mgr.status(slug)
    # FR-7.4 cold-start metric needs to know which profiles support session
    # resume; resolve it from config (adapter capabilities). Best-effort: an
    # unresolvable/unregistered adapter simply drops out of the resume set, so a
    # profile's cold-start column reads `—` rather than crashing the report.
    resume_capable: set[str] = set()
    for name in mgr.config.agents:
        try:
            if mgr.config.profile(name).adapter_class().capabilities.resume:
                resume_capable.add(name)
        except (KeyError, AttributeError):
            continue
    typer.echo(render_report(man, resume_capable=resume_capable), nl=False)
    if trend:
        typer.echo("")
        typer.echo(render_trend(mgr.trend(slug)), nl=False)


@app.command()
@_friendly_errors
def feedback(slug: str) -> None:
    """Capture human feedback for a run into retro/feedback.md (FR-6.1)."""
    from gauntlet.engine.feedback import FeedbackData, TriageCorrection, VERDICTS

    rating = typer.prompt("Outcome rating (e.g. good/mixed/poor)", default="")
    misses = typer.prompt("What did the reviewers miss?", default="")
    corrections: list[TriageCorrection] = []
    typer.echo("Triage corrections (false legitimate / false bikeshedding). "
               "Leave the finding id blank to finish.")
    while True:
        fid = typer.prompt("  finding id", default="")
        if not fid.strip():
            break
        while True:
            verdict = typer.prompt(f"  correct verdict {VERDICTS}", default="legitimate").strip()
            if verdict in VERDICTS:
                break
            typer.echo(f"    '{verdict}' is not a valid verdict; choose one of {VERDICTS}")
        note = typer.prompt("  note", default="")
        corrections.append(
            TriageCorrection(finding_id=fid.strip(), correct_verdict=verdict, note=note)
        )
    notes = typer.prompt("Freeform notes", default="")
    data = FeedbackData(
        outcome_rating=rating, reviewer_misses=misses,
        triage_corrections=corrections, notes=notes,
    )
    mgr = _manager()
    path = mgr.save_feedback(slug, data)
    typer.echo(f"feedback saved to {path}")
    # FR-6.1: feedback captured at run end or LATER must be able to drive
    # proposal generation. The retro step already ran, so re-synthesise now with
    # the feedback present (review F-001), appending any new pending proposals.
    if typer.confirm(
        "Regenerate improvement proposals from this feedback now?", default=True
    ):
        generated = mgr.regenerate_proposals(slug)
        valid = sum(1 for p in generated if getattr(p, "valid", False))
        typer.echo(
            f"generated {len(generated)} proposal(s), {valid} applyable; "
            f"review with `gauntlet proposals review --slug {slug}`"
        )


proposals_app = typer.Typer(no_args_is_help=True, help="Improvement proposals (FR-6.4).")
app.add_typer(proposals_app, name="proposals")


@proposals_app.command("review")
@_friendly_errors
def proposals_review(
    slug: str = typer.Option(None, "--slug", help="Limit to one run slug (default: all)."),
) -> None:
    """Present pending proposals; approve/reject + apply approved diffs (FR-6.4)."""
    mgr = _manager()
    pending = [
        (rd, p) for rd, p in mgr.list_proposals(slug)
        if getattr(p, "status", "") == "pending" and getattr(p, "valid", False)
    ]
    if not pending:
        typer.echo("no pending, applyable proposals")
        return

    def decide(proposal):
        typer.echo("")
        typer.echo(f"Proposal {proposal.name} (from {proposal.source_run})")
        typer.echo(f"  targets: {', '.join(proposal.targets) or '(none)'}")
        typer.echo(f"  rationale: {proposal.rationale.strip()[:500]}")
        typer.echo("  diff:")
        for line in proposal.diff.splitlines():
            typer.echo(f"    {line}")
        if typer.confirm("Approve and apply this proposal?", default=False):
            return "approve", ""
        notes = typer.prompt("Rejection notes", default="")
        return "reject", notes

    results = mgr.review_proposals(slug, decide=decide)
    for r in results:
        extra = r.get("sha", r.get("reason", ""))
        typer.echo(f"  {r['proposal']}: {r['action']}" + (f" ({extra[:60]})" if extra else ""))


@app.command()
@_friendly_errors
def rollback(
    slug: str,
    phase: int = typer.Option(..., "--phase", help="Roll the branch back to phase N."),
) -> None:
    """Reset the branch + manifest to a phase boundary (FR-9.9, guarded)."""
    target = _manager().rollback(slug, phase)
    typer.echo(f"rolled back to {target[:10]}")


ledger_app = typer.Typer(
    no_args_is_help=True, help="Machine-global usage ledger (FR-10)."
)
app.add_typer(ledger_app, name="ledger")


@ledger_app.command("backfill")
@_friendly_errors
def ledger_backfill() -> None:
    """Reconstruct the usage ledger from this repo's existing run manifests (FR-10.1).

    One-shot and idempotent: gives the window estimator history from the first
    enforced run instead of a cold start. Re-running appends nothing (de-dup by
    run_id::step_id).
    """
    from gauntlet.engine.ledger import default_ledger_path

    res = _manager().backfill_ledger()
    typer.echo(
        f"ledger backfill: scanned {res.manifests} manifest(s), "
        f"added {res.rows_added} row(s), skipped {res.rows_skipped} duplicate(s) "
        f"→ {default_ledger_path()}"
    )


@app.command()
@_friendly_errors
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host (loopback only)."),
    port: int = typer.Option(8765, help="Bind port."),
    enable_handoff: bool = typer.Option(
        False,
        "--enable-handoff",
        help="Enable the FR-4.7 scoped-analysis hand-off (opt-in; off by "
        "default). The console only assembles a copy-pasteable, read-only "
        "prompt — it spawns nothing and makes no model call. Overrides the "
        "`web.handoff` config key.",
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Reuse a live console (or boot one detached), open "
        "the authenticated browser, and return immediately instead of binding in "
        "the foreground (FR-4).",
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="With --resume, do not open a browser; just "
        "print the console URL (FR-1).",
    ),
) -> None:
    """Run the local supervisory console over loopback (FR-11.1).

    A read model + (in later phases) a run supervisor. Resolves config like the
    CLI, validates it is inside a git repo (fail-closed), mints a per-serve token
    and prints it + the URL on startup. The console scopes to this one repo; all
    of its slugs and run history are browsable (FR-1.1/FR-2.4).

    ``--resume`` is the non-blocking variant (FR-4): it reuses a live console or
    boots one **detached**, opens the authenticated browser, and returns — for
    re-attaching to a console after the launching terminal is gone. Plain
    ``serve`` (no ``--resume``) is unchanged: it binds in the foreground and never
    auto-opens a browser (FR-4.3).
    """
    if resume:
        _serve_resume(host=host, port=port, no_browser=no_browser)
        return

    from gauntlet.web.runner import serve as serve_console

    # Only pass the flag through when explicitly set, so an unset CLI flag falls
    # back to the `web.handoff` config key rather than forcing it off.
    serve_console(
        Path.cwd(),
        host=host,
        port=port,
        enable_handoff=True if enable_handoff else None,
    )


def _serve_resume(*, host: str, port: int, no_browser: bool) -> None:
    """`gauntlet serve --resume`: reuse/boot detached, open browser, return (FR-4).

    Reuses a live registered console if there is one (no new process); otherwise
    boots one detached and waits for healthz. Either way it opens the
    authenticated browser and returns rather than blocking. A boot that never
    answers healthz fails closed naming the log path (FR-4.2) — unlike
    ``run --watch``, where the console is a convenience and a boot failure is only
    a warning, ``serve --resume``'s sole job is the console, so it exits non-zero.
    """
    from gauntlet.web.launch import open_authenticated
    from gauntlet.web.registry import ConsoleBootError, ensure_console

    mgr = _manager()
    repo_root = mgr.repo_root.resolve()
    run_root = repo_root / mgr.config.run_root
    try:
        handle = ensure_console(repo_root, run_root, host=host, port=port)
    except ConsoleBootError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("reusing the running console" if handle.reused else "console started")
    if not handle.reused and handle.token:
        typer.echo(f"GAUNTLET_WEB_TOKEN={handle.token}", err=True)
    open_authenticated(handle, no_browser=no_browser, echo=typer.echo)


@judge_app.command("serve")
@_friendly_errors
def judge_serve(
    policy: Path = typer.Option(
        None, help="Fast-path policy file (default: <asset_root>/policy.yaml from "
        ".gauntlet/config.yaml, else policy.yaml).",
    ),
    audit: Path = typer.Option(
        None, help="Path to append the judge audit log (judge-audit.jsonl)."
    ),
    judge_model: str = typer.Option(
        None, help="LiteLLM model for the LLM classifier rung (omit to fail-closed)."
    ),
    host: str = typer.Option("127.0.0.1", help="Bind host (loopback only)."),
    port: int = typer.Option(8787, help="Bind port."),
    repo_root: Path = typer.Option(
        None, help="Authoritative repo boundary for path checks (#31); "
        "the engine passes this so checks never depend on the agent's cwd."
    ),
    run_id: str = typer.Option(
        None, help="Bind the judge to this run id (FR-10.2); /decide rejects "
        "requests whose run_id does not match. Omit for a run-agnostic dev judge."
    ),
) -> None:
    """Run the localhost judge service (dev command; engine-managed in P3)."""
    from gauntlet.judge.runner import serve

    if policy is None:
        policy = _default_policy_path()
    serve(
        policy_path=policy,
        audit_path=audit,
        judge_model=judge_model,
        host=host,
        port=port,
        repo_root=repo_root,
        run_id=run_id,
    )
