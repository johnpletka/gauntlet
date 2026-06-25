---
name: gauntlet-prd-author
description: >-
  Use when the user wants to write a PRD, draft a PRD, author a PRD, plan a PRD,
  or start a Gauntlet run in this repository. Routes the session to this repo's
  PRD-authoring playbook and the conventions for where the PRD goes and how to
  scaffold and launch it.
x-gauntlet-generated: true
x-gauntlet-template-version: 1
---

# Authoring a PRD for this Gauntlet repository

This repo runs on Gauntlet, so a PRD here feeds an adversarial pipeline rather
than going straight to code. Before drafting anything, open the playbook at
`{{PLAYBOOK_PATH}}` and work through it with the user — that file is the one
place this project keeps its interview process, its required section list, and
the bar a draft has to clear. Treat it as the source; this skill only sends you
there.

Hold to these conventions while you do it:

- **Where it goes.** Each PRD is one file at `<run_root>/<slug>/prd.md`, resolved
  beneath this repo's configured `asset_root`. Choose a short kebab-case `<slug>`
  up front.
- **Create the file with the tool, not by hand.** Run `gauntlet new <slug>`; it
  drops a stub for the human to fill, and the run gate refuses to start until a
  person has replaced that stub with real content.
- **Hand off by launching the pipeline.** Once the user is happy, the move is
  `gauntlet run <slug>`. That kicks off review first — no implementation yet —
  and the human signs off before the build proceeds.

Stay inside your lane: this skill teaches authoring only. Never draft the PRD
unilaterally, and never advise sidestepping a review gate or the safety judge —
a human owns the document and ratifies every result.
