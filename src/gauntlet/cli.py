"""Gauntlet CLI entry point.

P1 ships a stub: the full lifecycle commands (`run`, `status`, `approve`,
`resume`, `rollback`, ...) land in P3 and `init`/`doctor` in P6 per the plan.
"""

from __future__ import annotations

import typer

from gauntlet import __version__

app = typer.Typer(
    name="gauntlet",
    no_args_is_help=True,
    help="Adversarial multi-agent development harness.",
)


@app.callback()
def main() -> None:
    """Adversarial multi-agent development harness."""


@app.command()
def version() -> None:
    """Print the installed gauntlet version."""
    typer.echo(f"gauntlet {__version__}")
