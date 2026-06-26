# FR-6.6 operator-skill trigger qualification (recorded)

- **Activation count:** 7/7 (target 7/7)
- **Model id:** `haiku`
- **Claude Code CLI version (observed):** `2.1.193 (Claude Code)`
- **Claude Code CLI version (pinned, .gauntlet/pins.yaml):** `2.1.190`
- **Configuration/system context:** temp repo wired with only the operator skill (no judge PreToolUse hook); `--setting-sources project`.
- **Invocation protocol:** each phrase to a fresh `claude -p --output-format stream-json --verbose` session.
- **Activation oracle:** a `Skill` tool-use naming `gauntlet-operator` in the stream-json events (selection, not enumeration).
- **Retry policy:** up to 2 attempts/phrase; activation on any attempt passes that phrase.

| # | Trigger phrase | Activated | Attempts |
|---|----------------|-----------|----------|
| 1 | check the gauntlet run | yes | 1 |
| 2 | is the run stuck | yes | 1 |
| 3 | is the run parked | yes | 1 |
| 4 | approve the gate | yes | 1 |
| 5 | reject the gate | yes | 1 |
| 6 | why did the step fail | yes | 1 |
| 7 | recover the run | yes | 1 |
