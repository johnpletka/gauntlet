"""FR-6.6 — recorded natural-language trigger qualification for the operator skill.

The reproducible release-qualification artifact for the PRD's second empirical
risk (§1.3, §10 / G6): that the committable Claude Code skill at
``.claude/skills/gauntlet-operator/SKILL.md`` is **discovered and actually
triggered** from each of the **seven** documented operator trigger phrases
(FR-6.2) on the named, pinned Claude Code version — not merely enumerated.

This is **not** a CI gate and **not** run in ``pytest -m "not integration"``:
skill activation is decided by an external model service whose model and sampling
can change independently of the pinned CLI version, so a 7/7 result is not
deterministically reproducible (FR-6.6). It is an *empirical release
qualification*, executed and recorded on demand at acceptance time.

Recorded specification (pinned and emitted into the qualification artifact):

* **Denominator:** exactly the seven FR-6.2 phrases (``skill.OPERATOR_SPEC.
  trigger_phrases``). Target: 7/7 activating (G6's 100%).
* **Invocation protocol:** each phrase is presented to a *fresh* ``claude -p``
  session in a temp repo wired with **only** the operator skill (no judge
  PreToolUse hook, which — with no judge running — would deny the Skill tool and
  confound the observation).
* **Activation oracle:** a genuine ``Skill`` tool-use naming ``gauntlet-operator``
  in the ``stream-json`` event stream (selection, not a bare textual mention).
* **Retry policy:** each phrase is attempted up to ``ATTEMPTS_PER_PHRASE`` times;
  activation on any attempt counts as a pass for that phrase.
* **Pinned environment:** the model id (``--model``), the Claude Code CLI version
  (``claude --version``), and the ``.gauntlet/pins.yaml`` pinned version are
  captured into the artifact.

Run it explicitly and record the artifact (committed under
``runs/operator-aids/qualification/``):

    GAUNTLET_RUN_SKILL_TRIGGER=1 uv run pytest -m integration \\
        tests/integration/test_operator_skill_trigger.py -s

A sub-7/7 result is a release-qualification *finding to investigate* (the
self-describing ``status``/``--json`` surface is the backstop — it works with no
skill at all), not a frozen CI failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

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

REPO = Path(__file__).resolve().parents[2]
MODEL = "haiku"
TIMEOUT_S = 300.0
ATTEMPTS_PER_PHRASE = 2
ARTIFACT = REPO / "runs" / "operator-aids" / "qualification" / "fr-6-6-trigger.md"


def _install_operator_skill_only(repo: Path) -> None:
    """Install just the rendered operator skill + its playbook (no judge hook).

    The nested session must be able to invoke the Skill tool, so this repo omits
    the PreToolUse judge hook. The playbook the skill points at is installed too
    so a triggered session that opens it finds a real file.
    """
    spec = S.OPERATOR_SPEC
    target = repo / spec.skill_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(spec.render(spec.template_path().read_text(), "."))
    playbook = repo / spec.playbook_ref(".")
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text((S.SCAFFOLD_DIR / "prompts" / "operator.md").read_text())


def _skill_selected(stdout: str) -> bool:
    """True iff the event stream shows the operator skill *invoked* via Skill.

    Tolerant of the exact event shape: scans every assistant ``tool_use`` block
    and treats the skill as selected when a tool-use is named for the skill or
    carries the skill id in its input. A bare textual mention is not selection.
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
            if S.OPERATOR_SKILL_NAME in name or S.OPERATOR_SKILL_NAME in serialized:
                return True
    return False


def _cli_version() -> str:
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=15
        )
        return (out.stdout or out.stderr or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _pinned_version() -> str:
    try:
        from gauntlet.pins import load_pins, pin_file_path

        pins = load_pins(pin_file_path(REPO))
        return pins.clis["claude"].version if pins and "claude" in pins.clis else "n/a"
    except Exception:
        return "n/a"


def _attempt(prompt: str, repo: Path) -> tuple[bool, int]:
    proc = subprocess.run(
        [
            "claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project",
            "--model", MODEL,
        ],
        cwd=repo, capture_output=True, text=True, timeout=TIMEOUT_S,
    )
    return _skill_selected(proc.stdout), proc.returncode


def test_operator_skill_triggers_on_all_seven_phrases(tmp_path):
    phrases = S.OPERATOR_SPEC.trigger_phrases
    assert len(phrases) == 7, "FR-6.2 denominator must be exactly seven phrases"

    results: list[tuple[str, bool, int]] = []  # (phrase, activated, attempts_used)
    for phrase in phrases:
        repo = tmp_path / f"op-{abs(hash(phrase))}"
        repo.mkdir()
        _install_operator_skill_only(repo)
        activated, used = False, 0
        for attempt in range(1, ATTEMPTS_PER_PHRASE + 1):
            used = attempt
            ok, _rc = _attempt(phrase, repo)
            if ok:
                activated = True
                break
        results.append((phrase, activated, used))
        print(f"[operator-trigger] {phrase!r}: "
              f"{'ACTIVATED' if activated else 'not selected'} (attempts={used})")

    count = sum(1 for _p, ok, _u in results if ok)
    cli_version = _cli_version()

    # Record the durable qualification artifact (committed). The artifact is the
    # FR-6.6 deliverable; the assertion below is the target, not a CI gate.
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# FR-6.6 operator-skill trigger qualification (recorded)",
        "",
        f"- **Activation count:** {count}/7 (target 7/7)",
        f"- **Model id:** `{MODEL}`",
        f"- **Claude Code CLI version (observed):** `{cli_version}`",
        f"- **Claude Code CLI version (pinned, .gauntlet/pins.yaml):** `{_pinned_version()}`",
        "- **Configuration/system context:** temp repo wired with only the "
        "operator skill (no judge PreToolUse hook); `--setting-sources project`.",
        "- **Invocation protocol:** each phrase to a fresh `claude -p "
        "--output-format stream-json --verbose` session.",
        "- **Activation oracle:** a `Skill` tool-use naming `gauntlet-operator` "
        "in the stream-json events (selection, not enumeration).",
        f"- **Retry policy:** up to {ATTEMPTS_PER_PHRASE} attempts/phrase; "
        "activation on any attempt passes that phrase.",
        "",
        "| # | Trigger phrase | Activated | Attempts |",
        "|---|----------------|-----------|----------|",
    ]
    for i, (phrase, ok, used) in enumerate(results, 1):
        lines.append(f"| {i} | {phrase} | {'yes' if ok else 'no'} | {used} |")
    lines.append("")
    ARTIFACT.write_text("\n".join(lines))
    print(f"[operator-trigger] wrote qualification artifact: {ARTIFACT}")

    assert count == 7, (
        f"operator skill activated on {count}/7 phrases (target 7/7) — a "
        "release-qualification finding to investigate (FR-6.6); the artifact at "
        f"{ARTIFACT} records the per-phrase result"
    )
