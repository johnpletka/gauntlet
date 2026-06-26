"""Gauntlet CLI entry point.

P3 adds the run lifecycle (`new`, `run`, `status`, `approve`, `reject`,
`resume`, `abort`, `rollback`); P6 adds `init` (idempotent scaffolding) and
`doctor` (environment validation).
"""

from __future__ import annotations

from pathlib import Path

import typer

from gauntlet import __version__

app = typer.Typer(
    name="gauntlet",
    no_args_is_help=True,
    help="Adversarial multi-agent development harness.",
)

judge_app = typer.Typer(no_args_is_help=True, help="Safety judge service (FR-7).")
app.add_typer(judge_app, name="judge")


@app.callback()
def main() -> None:
    """Adversarial multi-agent development harness."""


@app.command()
def version() -> None:
    """Print the installed gauntlet version."""
    typer.echo(f"gauntlet {__version__}")


@app.command()
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


def _default_policy_path() -> Path:
    """`<asset_root>/policy.yaml` from the repo config (review F-005): a fresh
    adopter keeps the policy under `.gauntlet/`, so the bare `policy.yaml` default
    would not load. Falls back to the bare name when no config is present."""
    from gauntlet.engine.config import RunConfig

    try:
        asset_root = RunConfig.load(Path.cwd() / ".gauntlet/config.yaml").asset_root
    except Exception:
        asset_root = "."
    return Path.cwd() / asset_root / "policy.yaml"


@app.command()
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


@app.command()
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
    console_host: str = typer.Option(
        "127.0.0.1", "--console-host", help="Console bind host for --watch.",
    ),
    console_port: int = typer.Option(
        8765, "--console-port", help="Console bind port for --watch.",
    ),
) -> None:
    """Start a run on branch gauntlet/<slug> (FR-8.1)."""
    mgr = _manager()
    if watch:
        _ensure_watch_console(mgr, host=console_host, port=console_port)
    path = pipeline_file or (Path.cwd() / mgr.config.asset_root / "pipelines" / f"{pipeline}.yaml")
    status = mgr.start(
        slug, path, use_judge=not no_judge, run_id=run_id,
        reservation_token=reservation_token,
    )
    typer.echo(f"run status: {status}")


def _ensure_watch_console(mgr, *, host: str, port: int) -> None:
    """Boot/reuse the detached console for `run --watch` (FR-12.1/12.4).

    Fail-soft: the console is a convenience surface, so a boot failure (e.g. an
    unrelated process on the port) is surfaced loudly but does **not** abort the
    run — the foreground pipeline still runs exactly as today. The booted console
    is detached and persists after the foreground run returns (FR-12.2).
    """
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
        typer.echo(f"reusing console at {handle.login_url}")
        if handle.token_mismatch:
            typer.echo(
                "note: the running console uses a different token than your "
                "GAUNTLET_WEB_TOKEN; it was not restarted — sign in with the "
                "console's own token (FR-12.4).",
                err=True,
            )
    else:
        typer.echo(f"console started at {handle.login_url}")
        if handle.token:
            typer.echo(f"GAUNTLET_WEB_TOKEN={handle.token}", err=True)


@app.command()
def status(
    slug: str,
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit the run state as a single JSON object conforming to "
        "schemas/status.json (FR-4) — the stable machine contract for an agent "
        "or script. Stdout is only the JSON; exits non-zero only on an actual "
        "error (a parked/failed run is a valid state, exit 0).",
    ),
) -> None:
    """Show the current run status for <slug> with driver liveness + next action.

    Read-only (FR-1/FR-2): reports the computed driver liveness and the concrete
    next action for the run's composite state. It never writes — a surviving
    recovery intent is *reported*, never finalized (FR-5.6). With ``--json`` it
    emits the same computed state as a lone, schema-stable JSON object (FR-4).
    """
    import json

    from gauntlet.engine import operator
    from gauntlet.engine.manifest import Manifest
    from gauntlet.engine.operator import RunResolutionError, StatusContractError
    from gauntlet.engine.run import UnsafeRunSegment, safe_run_segment

    mgr = _manager()
    # FR-10.1 containment: the slug and the active-run pointer both flow into
    # filesystem paths, so validate the slug, resolve the instance through the
    # safe resolver (which validates active-run.txt), and confirm the resolved
    # instance stays under the slug dir BEFORE reading the manifest or a
    # recovery intent — never via the unchecked `active_run_dir()` (F-002).
    layout = mgr.layout(slug)
    try:
        safe_run_segment(slug, kind="slug")
        run_instance_dir = operator.resolve_run_instance(layout.slug_dir)
    except (UnsafeRunSegment, RunResolutionError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        run_instance_dir.resolve().relative_to(layout.slug_dir.resolve())
    except ValueError as exc:
        typer.echo(
            f"error: resolved run instance {run_instance_dir} escapes the run "
            f"tree for {slug!r}; refusing to read it",
            err=True,
        )
        raise typer.Exit(1) from exc

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
        rstate = operator.compute_run_state(man, driver.state)
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

        if json_output:
            # A single JSON object on stdout, no interleaved log lines (FR-4.3). A
            # malformed surviving intent is a human-footer anomaly only, so `recon`
            # is None there and `reconciliation` is null — never a fabricated object.
            payload = operator.status_payload(
                man, driver, rstate, recon,
                run_root=run_root, run_instance_dir=run_instance_dir,
                current_step_freshness=freshness,
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

    for line in operator.render_footer(
        driver, rstate, reconciliation=recon, anomaly=anomaly,
        current_step_freshness=freshness,
    ):
        typer.echo(line)


@app.command()
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
def approve(
    slug: str,
    gate: str = typer.Option(None, "--gate", help="Gate step id (default: current)."),
    notes: str = typer.Option(None, help="Approval notes."),
    no_judge: bool = typer.Option(False, "--no-judge"),
) -> None:
    """Approve a parked human_gate and continue the run (FR-8.1)."""
    typer.echo(f"run status: {_manager().approve(slug, gate, notes, use_judge=not no_judge)}")


@app.command()
def reject(
    slug: str,
    notes: str = typer.Option(..., help="Why the gate was rejected."),
    gate: str = typer.Option(None, "--gate", help="Gate step id (default: current)."),
) -> None:
    """Reject a parked human_gate (FR-8.1)."""
    typer.echo(f"run status: {_manager().reject(slug, notes, gate)}")


@app.command()
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
) -> None:
    """Resume an interrupted run at its last incomplete step (FR-8.2).

    For a step parked awaiting a human decision — a builder UPSTREAM CONFLICT or
    an adversarial_cycle escalation its own loop cannot resolve (FR-10.4/10.5) —
    supply `--response "<decision>"` to record it (audited in the manifest) and
    re-drive with it injected. Other parks resume as before.
    """
    typer.echo(
        "run status: "
        f"{_manager().resume(slug, response=response, use_judge=not no_judge)}"
    )


@app.command()
def abort(slug: str) -> None:
    """Abort a run (FR-8.1)."""
    typer.echo(f"run status: {_manager().abort(slug)}")


@app.command()
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
def finish(slug: str) -> None:
    """Merge a completed run into its base, then delete the branch + pointer.

    Requires the run to be done and the worktree clean; a merge conflict is
    aborted and surfaced for a manual merge.
    """
    typer.echo(_manager().finish(slug))


@app.command()
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
    typer.echo(render_report(man), nl=False)
    if trend:
        typer.echo("")
        typer.echo(render_trend(mgr.trend(slug)), nl=False)


@app.command()
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
def rollback(
    slug: str,
    phase: int = typer.Option(..., "--phase", help="Roll the branch back to phase N."),
) -> None:
    """Reset the branch + manifest to a phase boundary (FR-9.9, guarded)."""
    target = _manager().rollback(slug, phase)
    typer.echo(f"rolled back to {target[:10]}")


@app.command()
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
) -> None:
    """Run the local supervisory console over loopback (FR-11.1).

    A read model + (in later phases) a run supervisor. Resolves config like the
    CLI, validates it is inside a git repo (fail-closed), mints a per-serve token
    and prints it + the URL on startup. The console scopes to this one repo; all
    of its slugs and run history are browsable (FR-1.1/FR-2.4).
    """
    from gauntlet.web.runner import serve as serve_console

    # Only pass the flag through when explicitly set, so an unset CLI flag falls
    # back to the `web.handoff` config key rather than forcing it off.
    serve_console(
        Path.cwd(),
        host=host,
        port=port,
        enable_handoff=True if enable_handoff else None,
    )


@judge_app.command("serve")
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
    )
