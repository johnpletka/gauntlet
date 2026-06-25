"""FR-1.6 — recorded natural-language trigger test for the PRD-authoring skill.

This is the reproducible artifact validating the PRD's **riskiest external
dependency** (§1.3, §10): that a per-repo, committable Claude Code skill placed
at ``.claude/skills/gauntlet-prd-author/SKILL.md`` is **discovered and actually
triggered** from a natural-language PRD request on the *named, pinned Claude Code
version* — not merely enumerated.

Recorded specification (also mirrored in BOOTSTRAP-NOTES.md):

* **Pinned Claude Code version:** the version recorded in ``.gauntlet/pins.yaml``
  (``claude`` 2.1.177 at this revision). Re-record on a version bump.
* **Prompt(s):** at minimum ``"help me write a PRD"`` (see ``TRIGGER_PROMPTS``).
* **Expected observation:** the ``gauntlet-prd-author`` skill is *selected /
  invoked* — i.e. a ``Skill`` tool-use naming it appears in the session's
  event stream. Enumeration or metadata inspection alone does **not** satisfy
  this (that is covered offline in ``tests/unit/test_skill.py``).
* **Failure criterion:** the skill is not selected for any trigger prompt.

Why opt-in: this spawns a live, model-driven Claude Code session (cost + latency)
and observes non-deterministic selection behavior, so it is gated behind both the
``integration`` marker *and* ``GAUNTLET_RUN_SKILL_TRIGGER=1`` so it never fires
implicitly. The temp repo wires **only** the skill (no judge PreToolUse hook), so
the nested session is not gated by an absent judge. Run it explicitly:

    GAUNTLET_RUN_SKILL_TRIGGER=1 uv run pytest -m integration \
        tests/integration/test_skill_trigger.py -s

and record the outcome (version, prompt, observation, pass/fail) in
BOOTSTRAP-NOTES.md. A *failed* trigger is not a passing exit: per the plan it
halts P1 with an UPSTREAM CONFLICT (the §1.3 assumption is falsified) and blocks
P2/P3 until a human resolves it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from gauntlet.engine import skill as S

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("claude") is None, reason="claude CLI not installed"
    ),
    pytest.mark.skipif(
        os.environ.get("GAUNTLET_RUN_SKILL_TRIGGER") != "1",
        reason="opt-in: set GAUNTLET_RUN_SKILL_TRIGGER=1 to run the live trigger check",
    ),
]

TRIGGER_PROMPTS = ("help me write a PRD",)
TIMEOUT_S = 300.0


def _install_skill_only(repo):
    """Install just the rendered skill under .claude/ (no judge hook wiring).

    The nested session must be able to invoke the Skill tool, so this repo
    deliberately omits the PreToolUse judge hook (which, with no judge running,
    would deny tool calls and confound the observation).
    """
    target = repo / S.SKILL_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(S.render_skill(S.current_template_path().read_text(), "."))


def _skill_selected(stdout: str) -> bool:
    """True iff the event stream shows the skill *invoked* via a Skill tool-use.

    Tolerant of the exact event shape: scans every assistant ``tool_use`` block
    and treats the skill as selected when a tool-use either is named for the
    skill or carries the skill id in its input (how Claude Code surfaces a
    ``Skill`` invocation). A bare textual mention that is not a tool-use does not
    count — that would be enumeration, not selection.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name", ""))
            serialized = json.dumps(block.get("input", {}))
            if S.SKILL_NAME in name or S.SKILL_NAME in serialized:
                return True
    return False


@pytest.mark.parametrize("prompt", TRIGGER_PROMPTS)
def test_prd_author_skill_triggers_on_natural_language(prompt, tmp_path):
    repo = tmp_path / "skill-repo"
    repo.mkdir()
    _install_skill_only(repo)

    proc = subprocess.run(
        [
            "claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project",
            "--model", "haiku",
        ],
        cwd=repo, capture_output=True, text=True, timeout=TIMEOUT_S,
    )
    # Surface the captured stream so the operator can record the observation.
    print(f"\n[skill-trigger] prompt={prompt!r} exit={proc.returncode}")
    print(proc.stdout[-4000:])
    if proc.returncode != 0:
        print(proc.stderr[-2000:])
    assert _skill_selected(proc.stdout), (
        f"the {S.SKILL_NAME!r} skill was not selected for prompt {prompt!r} "
        "(FR-1.6 failure → PRD §1.3 riskiest assumption falsified)"
    )
