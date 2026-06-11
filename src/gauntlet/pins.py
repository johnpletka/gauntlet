"""Doctor pin file: verified CLI versions + flag behavior (FR-1.5 groundwork).

The pin file records what the *installed* CLIs actually did when the contract
tests exercised them — not what their docs (or the PRD) claim. Where observed
behavior differs from the PRD/prompt, the pin file and a BOOTSTRAP-NOTES
entry win. `gauntlet doctor` (P6) compares installed versions against it.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

PIN_FILE_RELPATH = Path(".gauntlet") / "pins.yaml"


class FlagPin(BaseModel):
    """One verified flag: what was exercised and what was observed."""

    flag: str
    verified: str  # observed behavior, in one or two sentences


class CliPin(BaseModel):
    """Verified state of one CLI at a specific version."""

    version: str
    verified_flags: list[FlagPin] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PinFile(BaseModel):
    verified_date: str  # ISO date the contract suite last verified these
    clis: dict[str, CliPin]


def pin_file_path(repo_root: Path) -> Path:
    return repo_root / PIN_FILE_RELPATH


def load_pins(path: Path) -> PinFile:
    return PinFile.model_validate(yaml.safe_load(path.read_text()))
