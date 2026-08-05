# Phase prompts — recovery redesign

The verbatim phase prompts the maintainer issued to each builder session for
the `RECOVERY-REDESIGN-PLAN.md` phases, recorded here as **evidence**.

## Why these are in the repository

The P7b review round raised a finding (F-005) that could not be answered from
the repository: the P7b commit body justified a deviation from the ratified
`P7-worktree-spike.md` by citing the phase prompt that authorized it, but the
prompt existed only in a session transcript. A reviewer working from repository
evidence alone could see the deviation and the builder's explanation of it, but
not the instruction behind it — so the explanation was indistinguishable from
self-authorization.

That is a gap in the audit trail, not a code defect. *Data over inference:*
future readers debugging a phase should never have to infer what the builder
was told.

## What these files are NOT

**They are not ratification.** Recording an instruction is not the same as a
human approving its outcome. Humans ratify; agents propose (CLAUDE.md §2).
Specifically:

- A prompt here shows what a builder was **asked** to do. Whether the work it
  produced is **accepted** is decided at that phase's gate, by the maintainer,
  after review.
- A prompt is **not** an amendment to an approved artifact. Where a prompt
  authorizes departing from `PRD-gauntlet.md`, `RECOVERY-REDESIGN-PLAN.md`, or
  a ratified spike, the approved artifact still says what it says. The
  deviation is recorded in the relevant commit body under a
  `CORRECTION TO THE RATIFIED SPIKE` heading; changing the artifact itself
  requires its own review loop and gate.
- They carry **no authority over a future phase**. Each prompt scopes exactly
  one stage and says so.

## Files

| File | Stage | Status |
|---|---|---|
| `P7b.md` | P7b — lock and liveness relocation | issued; implemented by 7f9787e, 9da3189 |
| `P7c.md` | P7c — dedicated run worktree behind a flag | drafted, **not yet issued or approved by the maintainer** |

`P7c.md` was drafted by the P7b builder session at the maintainer's request so
the next session has a starting point. It has not been reviewed or issued, and
nothing in it is authorized until the maintainer says so. Treat it as a
proposal for the next phase's scope, which is what it is.

Prompts for P7.0, P7a and earlier phases are not recorded here — they predate
this convention, and reconstructing them after the fact would produce exactly
the unreliable evidence this directory exists to avoid.
