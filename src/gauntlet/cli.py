"""Gauntlet CLI entry point.

P3 adds the run lifecycle (`new`, `run`, `status`, `approve`, `reject`,
`resume`, `abort`, `rollback`); `init`/`doctor` land in P6 per the plan.
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


def _manager() -> "object":
    from gauntlet.engine.run import RunManager

    return RunManager(Path.cwd())


@app.command()
def new(slug: str) -> None:
    """Scaffold runs/<slug>/ with a human-authored PRD stub (FR-8.1, FR-10.1)."""
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
) -> None:
    """Start a run on branch gauntlet/<slug> (FR-8.1)."""
    mgr = _manager()
    path = pipeline_file or (Path.cwd() / "pipelines" / f"{pipeline}.yaml")
    status = mgr.start(slug, path, use_judge=not no_judge)
    typer.echo(f"run status: {status}")


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
    no_judge: bool = typer.Option(False, "--no-judge"),
) -> None:
    """Resume an interrupted run at its last incomplete step (FR-8.2)."""
    typer.echo(f"run status: {_manager().resume(slug, use_judge=not no_judge)}")


@app.command()
def abort(slug: str) -> None:
    """Abort a run (FR-8.1)."""
    typer.echo(f"run status: {_manager().abort(slug)}")


@app.command()
def rollback(
    slug: str,
    phase: int = typer.Option(..., "--phase", help="Roll the branch back to phase N."),
) -> None:
    """Reset the branch + manifest to a phase boundary (FR-9.9, guarded)."""
    target = _manager().rollback(slug, phase)
    typer.echo(f"rolled back to {target[:10]}")


@judge_app.command("serve")
def judge_serve(
    policy: Path = typer.Option(
        Path("policy.yaml"), help="Path to the fast-path policy file."
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

    serve(
        policy_path=policy,
        audit_path=audit,
        judge_model=judge_model,
        host=host,
        port=port,
        repo_root=repo_root,
    )
