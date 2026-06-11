"""Gauntlet CLI entry point.

P1 ships a stub: the full lifecycle commands (`run`, `status`, `approve`,
`resume`, `rollback`, ...) land in P3 and `init`/`doctor` in P6 per the plan.
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
) -> None:
    """Run the localhost judge service (dev command; engine-managed in P3)."""
    from gauntlet.judge.runner import serve

    serve(
        policy_path=policy,
        audit_path=audit,
        judge_model=judge_model,
        host=host,
        port=port,
    )
