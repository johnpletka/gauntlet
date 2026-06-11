# Bootstrap notes — process pain points as design feedback

Running log, newest at the bottom. Each entry: what happened while following the
process, and what it suggests for Gauntlet's design.

## 2026-06-10 — Planning stage

1. **PRD has an unlabeled section.** The CLI command reference (between FR-10's
   acceptance and §7) has no section header — references like "§6" resolve only by
   guessing. Footer also says "*End of PRD v1.0*" while the header says v1.3.
   *Design feedback:* the PRD adversarial-review prompt should explicitly hunt for
   structural defects (numbering, dangling refs, stale footers), not just semantic
   ones — cheap to catch, and downstream prompts cite sections by number.

2. **Two slugs for one effort.** The bootstrap places the plan at
   `runs/gauntlet/plan.md` but manual transcripts at
   `runs/gauntlet-bootstrap/manual/`, while FR-4.1 puts run artifacts under
   `.gauntlet/runs/<prd-slug>/`. Three layouts for one project invites confusion.
   *Design feedback:* FR-4.1's "configurable to an external path" should be a single
   run-root setting that everything (plan, transcripts, manifests) lives under; a
   run should never split across roots.

3. **Plan gate ordering: bootstrap prompt vs. PRD.** FR-5.1's pipeline runs
   `plan-cycle` (adversarial review) *before* `plan-approve` (human gate), but
   bootstrap rule 2 says stop for human approval immediately after authoring the
   plan, with no review cycle. Followed the bootstrap prompt (it owns the process)
   and surfaced the option of a manual codex plan review at the stop point.
   *Design feedback:* none for Gauntlet itself — the pipeline YAML makes this
   ordering explicit, which is exactly the property the bootstrap prompt lacked.
