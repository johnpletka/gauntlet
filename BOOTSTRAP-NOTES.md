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

## 2026-06-10 — Plan review cycle, round 1

4. **Unstructured review output required manual normalization.** The human ran
   codex directly, so the review came back as prose — no finding IDs, severities
   `High`/`Medium` instead of the §7 enum, and triage had to assign IDs and map
   severities by hand before point-by-point verdicts were possible.
   *Design feedback:* this is the concrete cost `findings_schema` +
   `--output-schema` enforcement (FR-5.2, §7) exists to remove; also, the triage
   step should tolerate/normalize schema-violating reviewer output as a fallback
   path rather than failing, since ad-hoc human-run reviews will happen.

5. **Reviewers naturally emit open questions; the findings schema has no slot for
   them.** Codex produced three "Open Questions" alongside findings — one of which
   (stale PRD text) was a real defect. Forcing these through the
   `legitimate|bikeshedding|...` verdict enum is awkward (they're questions, not
   claims).
   *Design feedback:* consider an `open_questions` array in `findings.json` (or
   instruct reviewers to express questions as `spec-gap` findings) so the triage
   prompt and transcripts handle them first-class.

6. **A finding can implicate an upstream artifact, not the reviewed one.** OQ-3
   flagged a defect in the PRD while reviewing the plan. The process answer
   (FR-10.4: halt, propose, human amends) worked, but the triage schema has no way
   to mark "fix lands in a different artifact owned by someone else."
   *Design feedback:* triage verdicts could use an optional `target_artifact`
   field so upstream-invalidation routing (FR-10.4) can be automated instead of
   inferred from reasoning text.

7. **Naive credential regexes false-positive on ordinary prose.** A pre-commit
   scan of the confirm-pass event log for `sk-…` matched "a**sk-with**-warning"
   inside a verdict note. No real secret, but it cost a manual inspection.
   *Design feedback:* the P1 redacting writer's patterns need word boundaries +
   minimum length/entropy (e.g. `\bsk-[A-Za-z0-9]{20,}\b`), and the redactor
   should log *what pattern* fired so false positives are diagnosable. Exact
   env-var **value** matching (known secrets) stays the primary mechanism;
   regexes are the fallback.

8. **Schema-constrained confirm pass worked end-to-end.** `codex exec -s
   read-only --output-schema … -o …` returned valid JSON on the first try (13/13
   verdicts, parseable, no retry needed), with usage in the event stream
   (21.5k in / 1.2k out tokens) and a session/thread id. The PRD's adapter
   assumptions (§4.1 CodexAdapter, FR-9.5 confirm mapping) look sound on
   codex-cli 0.139.0 — first real datapoint for the P1 pin file.
