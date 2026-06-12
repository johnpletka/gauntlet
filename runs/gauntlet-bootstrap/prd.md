# PRD: Gauntlet — Adversarial Multi-Agent Development Harness (pointer)

The canonical, human-authored PRD for this run is `PRD-gauntlet.md` at the
repository root (v1.3, authored by John). It is the spec; agents must not
modify it (CLAUDE.md §8). The approved implementation plan is
`runs/gauntlet/plan.md`.

This pointer satisfies the FR-10.1 entry contract for
`gauntlet run gauntlet-bootstrap` without duplicating the spec: a run must
never split across roots (BOOTSTRAP-NOTES #2), and the bootstrap predates the
single-slug layout. Builder prompts in `pipelines/bootstrap.yaml` reference
the canonical files directly.
