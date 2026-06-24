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
    path = _manager().new(slug)
    typer.echo(f"scaffolded {path}; author the PRD, then `gauntlet run {slug}`")


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
def status(slug: str) -> None:
    """Show the current run status for <slug> (FR-8.1)."""
    man = _manager().status(slug)
    typer.echo(f"{man.slug}: {man.status} (current step: {man.current_step})")
    for rec in man.steps:
        it = f"[{rec.iteration}]" if rec.iteration is not None else ""
        typer.echo(f"  {rec.id}{it}: {rec.status}")


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
        help='Human decision for a step parked on an UPSTREAM CONFLICT '
             '(FR-10.4): re-runs the builder with this context injected instead '
             'of re-surfacing the conflict. Required to resume a conflict park; '
             'passed verbatim, no parsing.',
    ),
    no_judge: bool = typer.Option(False, "--no-judge"),
) -> None:
    """Resume an interrupted run at its last incomplete step (FR-8.2).

    For a step parked on an UPSTREAM CONFLICT, supply
    `--response "<decision>"` to record your decision (audited in the manifest)
    and re-run the builder with it. Other parks resume as before.
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
