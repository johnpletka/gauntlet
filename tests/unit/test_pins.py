"""Doctor pin file loading (FR-1.5 groundwork)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from gauntlet.pins import PIN_FILE_RELPATH, load_pins, pin_file_path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_load_valid_pin_file(tmp_path):
    pin = tmp_path / "pins.yaml"
    pin.write_text(
        """
verified_date: "2026-06-10"
clis:
  claude:
    version: "2.1.172"
    verified_flags:
      - flag: "--output-format json"
        verified: "returns a single result object"
    notes: ["model alias 'haiku' resolves"]
  codex:
    version: "codex-cli 0.139.0"
    verified_flags: []
"""
    )
    pins = load_pins(pin)
    assert pins.clis["claude"].version == "2.1.172"
    assert pins.clis["claude"].verified_flags[0].flag == "--output-format json"
    assert pins.clis["codex"].notes == []


def test_invalid_pin_file_rejected(tmp_path):
    pin = tmp_path / "pins.yaml"
    pin.write_text("clis: {claude: {verified_flags: []}}")  # missing versions
    with pytest.raises(ValidationError):
        load_pins(pin)


def test_repo_pin_file_exists_and_validates():
    # P1 exit criterion: the pin file exists and reflects verified behavior.
    path = pin_file_path(REPO_ROOT)
    assert path == REPO_ROOT / PIN_FILE_RELPATH
    pins = load_pins(path)
    assert "claude" in pins.clis
    assert "codex" in pins.clis
    assert pins.clis["claude"].verified_flags
    assert pins.clis["codex"].verified_flags
