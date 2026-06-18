"""Gauntlet Console — supervisory web UI (PRD: Gauntlet Console).

A new surface that sits *above* the orchestrator: it parses on-disk run state
(`manifest.json` + artifacts) into a read model and, in later phases, supervises
runs by launching the same `gauntlet` CLI verbs a human would type. P1 is the
read-only observer MVP — zero engine changes, the load-bearing premise of the
whole design (D2, FR-11.2).

Submodules (introduced as each phase needs them):
- ``store``   — the read model (P1)
- ``service`` — FastAPI app factory (P1)
- ``runner``  — uvicorn host + `gauntlet serve` plumbing (P1)
"""
