# FR-6.6 operator-skill trigger qualification (recorded)

**Status:** methodology pinned; live activation **PENDING operator-session run.**

This is the durable, committed artifact for the FR-6.6 release qualification: the
empirical check that the committable `gauntlet-operator` skill is *discovered and
actually triggered* from each of the seven documented operator trigger phrases
(FR-6.2) on the pinned Claude Code version. Per the §6.6 contract it is an
**empirical release qualification, not a deterministic CI gate** — skill
activation is decided by an external model service whose model/sampling can change
independently of the pinned CLI version, so a 7/7 result is not deterministically
reproducible.

## Why this is run by the operator session, not the build session

The build sandbox **denies the builder agent from invoking the external `claude`
binary** (it reaches a remote model service), so the builder cannot spawn the
nested live sessions this check requires. This is the same, already-ratified
division of labor recorded for the prd-author FR-1.6 trigger in
`BOOTSTRAP-NOTES.md` (note 49b): the build session ships the deterministic test
+ pinned methodology; the **operator/human session runs the live trigger and
records the per-phrase evidence here.** It is therefore not a deferral of an
in-reach deliverable — it is a structural sandbox boundary, resolved exactly as
prd-author's equivalent was.

## Pinned methodology (the FR-6.6 reproducible specification)

| Knob | Value |
|------|-------|
| **Denominator (phrases)** | the **seven** FR-6.2 phrases (below); target **7/7** |
| **Model id** | `haiku` (`--model haiku`) |
| **Invocation protocol** | each phrase → a *fresh* `claude -p "<phrase>" --output-format stream-json --verbose --setting-sources project --model haiku`, in a temp repo wired with **only** the operator skill + its playbook (no judge PreToolUse hook, which with no judge running would deny the Skill tool and confound the observation) |
| **Activation oracle** | a genuine `Skill` tool-use naming `gauntlet-operator` in the `stream-json` event stream (selection, not a bare textual mention / enumeration) |
| **Retry policy** | up to **2** attempts per phrase; activation on any attempt passes that phrase |
| **Claude Code CLI version (pinned)** | `.gauntlet/pins.yaml` → `claude` (re-record on a version bump; capture `claude --version` at run time, noting any local auto-update drift exactly as note 49b did for 2.1.190 vs the pin) |

The executable form of this methodology is
`tests/integration/test_operator_skill_trigger.py`, which writes the per-phrase
results table back into this artifact when run with `GAUNTLET_RUN_SKILL_TRIGGER=1`:

```
GAUNTLET_RUN_SKILL_TRIGGER=1 uv run pytest -m integration \
    tests/integration/test_operator_skill_trigger.py -s
```

## The seven trigger phrases (FR-6.2 closed corpus)

1. "check the gauntlet run"
2. "is the run stuck"
3. "is the run parked"
4. "approve the gate"
5. "reject the gate"
6. "why did the step fail"
7. "recover the run"

## Per-phrase activation result (filled by the operator-session run)

| # | Trigger phrase | Activated | Attempts |
|---|----------------|-----------|----------|
| 1 | check the gauntlet run | _pending_ | _pending_ |
| 2 | is the run stuck | _pending_ | _pending_ |
| 3 | is the run parked | _pending_ | _pending_ |
| 4 | approve the gate | _pending_ | _pending_ |
| 5 | reject the gate | _pending_ | _pending_ |
| 6 | why did the step fail | _pending_ | _pending_ |
| 7 | recover the run | _pending_ | _pending_ |

- **Activation count:** _pending_ / 7 (target 7/7)
- **Model id (observed):** _pending_
- **Claude Code CLI version (observed `claude --version`):** _pending_

A sub-7/7 outcome is a release-qualification **finding to investigate**, not a
frozen CI failure; the self-describing `gauntlet status` / `--json` surface is the
backstop and works with no skill at all.
