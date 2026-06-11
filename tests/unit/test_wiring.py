"""The committed Claude hook wiring is present and correct (review P2 F-006)."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS = REPO_ROOT / ".claude" / "settings.json"


def test_settings_file_committed():
    assert SETTINGS.exists(), ".claude/settings.json (the repo hook wiring) is missing"


def test_pretooluse_wires_judge_hook():
    data = json.loads(SETTINGS.read_text())
    groups = data["hooks"]["PreToolUse"]
    commands = [
        h["command"]
        for group in groups
        for h in group["hooks"]
        if h.get("type") == "command"
    ]
    assert "gauntlet-judge-hook" in commands, commands
    # matches all tools (the judge gates every tool call, FR-7.3)
    assert any(group.get("matcher") == "*" for group in groups)
