---
name: gauntlet-operator
description: >-
  Use when the user wants to operate, supervise, check on, or recover a Gauntlet
  run in this repository — for example: "check the gauntlet run", "is the run
  stuck", "is the run parked", "approve the gate", "reject the gate", "why did
  the step fail", or "recover the run". Routes the session to this repo's
  operator playbook: the run-state triage tree, the gate decisions, and the
  guarded recovery and evidence commands.
x-gauntlet-generated: true
x-gauntlet-template-version: 1
---

# Operating a Gauntlet run in this repository

This repo runs on Gauntlet, so a live run moves through a state machine that
pauses for human decisions and can wedge in ways that need a careful, guarded
response. Before you touch anything, open the playbook at `prompts/operator.md` and
follow it — that file holds this project's triage tree, the meaning of each
run-state, and which command resolves which one. Treat it as the source; this
skill only sends you there.

The reflex it teaches, worth internalizing up front:

- **Ask the tool, do not infer.** Begin with `gauntlet status <slug>`; it
  computes the real state — including whether the driver is actually alive — and
  prints the next command. `gauntlet status <slug> --json` is the same answer for
  a script.
- **Read before you mutate.** `gauntlet logs <slug>` surfaces a failing step's
  evidence; reach for it before resuming anything.
- **Recover is narrow and guarded.** `gauntlet recover <slug>` is only for a
  proven-alive, wedged driver, and it never auto-resumes.

Stay inside your lane: this skill is for operating a run, not building or
reviewing one. A human ratifies every gate, the safety judge is never bypassed,
and the operator never hand-edits files the pipeline owns.
