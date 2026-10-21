"""Escalation brief: a pre-digested, per-finding fix contract for a cycle park (#163).

When an ``adversarial_cycle`` parks for a human (an FR-10.5 escalation — round
budget exhausted with open blockers, or a blocker not fixed this round — an
FR-10.4 upstream invalidation, or a review F-009 verdict that needs a human),
the raw evidence is spread across ``findings.json``, per-finding triage
verdicts, and every round's ``confirm.json``. The operator then has to
reconstruct, per finding, what was claimed, what the fix rounds already tried,
and where they stopped short — before making what is usually a quick call.

This module does that synthesis once, at park time, from data the cycle already
holds in memory:

* **Deterministic digest** (always; no model call): per open finding, the
  one-line claim, the triage verdict, the *residual gap* (the latest confirm
  verdict + notes), and the fix history (each round's fix commit SHA and the
  confirm verdict/notes on it).
* **Proposed fix contract** (only when the cycle step names a
  ``fix_contract_agent``): one structured call asks that profile for the
  specific change the next round should make, the approved-artifact constraint
  that binds it, and how the confirmer will know it landed. Advisory and
  fail-soft: any failure records *why* on the brief and the cycle parks exactly
  as it would have without one — the park itself is the fail-closed state, and
  a contract never unparks anything.

The brief is persisted as run bookkeeping (``artifacts/escalation-brief.json``),
rendered by ``gauntlet status`` (human + ``--json``) and the web console, and —
on the ``resume --response`` re-drive that answers it — handed to the reviewer,
triager, and fixer as agent-drafted data the operator's response approves,
amends, or rejects. The operator's response is always the authority; a contract
binds only as far as the response accepts it.

Pure functions only (no adapter calls, no git): the cycle owns the agent call
and the write; :mod:`gauntlet.engine.operator` owns the status read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BRIEF_ARTIFACT = "escalation-brief.json"
BRIEF_VERSION = 1
DEFAULT_CONTRACT_PROMPT = "prompts/escalation-contract.md"
DEFAULT_CONTRACT_SCHEMA = "schemas/fix-contract.json"

# Park kinds — which closure path parked the cycle.
KIND_MAX_ROUNDS = "max_rounds_exhausted"  # FR-10.5, after the final confirm
KIND_CLOSURE = "closure_guard"  # FR-10.5 unfixed blocker / FR-10.4 upstream
KIND_TRIAGE = "triage_needs_human"  # review F-009, no escalation resolution

# Contract status on the brief.
CONTRACT_PROPOSED = "proposed"
CONTRACT_NOT_CONFIGURED = "not_configured"
CONTRACT_UNAVAILABLE = "unavailable"

RECOMMENDATIONS = ("fix_in_scope", "amend_upstream", "decline")

_CLAIM_LIMIT = 240
_NOTE_LIMIT = 400

# Used when the asset root predates this schema (an adopter that has not run
# `gauntlet upgrade`), so a configured contract agent still gets a schema.
# Keep in lockstep with schemas/fix-contract.json (a unit test pins equality).
BUILTIN_CONTRACT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "gauntlet/schemas/fix-contract.json",
    "title": "Proposed fix contracts for an escalation park (#163)",
    "type": "object",
    "additionalProperties": False,
    "required": ["contracts", "summary"],
    "properties": {
        "contracts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "finding_id",
                    "proposed_change",
                    "grounding",
                    "acceptance_check",
                    "recommendation",
                ],
                "properties": {
                    "finding_id": {"type": "string"},
                    "proposed_change": {"type": "string"},
                    "grounding": {"type": "string"},
                    "acceptance_check": {"type": "string"},
                    "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)},
                },
            },
        },
        "summary": {"type": "string"},
    },
}

BUILTIN_CONTRACT_PROMPT = (
    "You are the escalation analyst for an adversarial review cycle that has "
    "parked for a human decision. For EACH open finding in the brief below, "
    "draft a proposed fix contract the human can approve or redline: "
    "proposed_change = the specific change the next fix round should make "
    "(concrete enough to implement, scoped to what the finding and the residual "
    "gap demand — no scope creep); grounding = the approved PRD/plan clause(s) "
    "that bind the fix (cite section/FR ids), or 'no binding clause found'; "
    "acceptance_check = what the confirm pass should verify to call it "
    "resolved; recommendation = fix_in_scope | amend_upstream (the fix needs an "
    "approved artifact changed by a human) | decline (the finding should not "
    "be fixed — say why in grounding). Build on what the fix rounds already "
    "did and the confirm notes on where they fell short; do not repeat an "
    "approach that already failed. Do not edit files. Emit one contract per "
    "open finding id, using that id exactly."
)


def _condense(text: object, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


# --- fix history (accumulated by the cycle, one entry per confirm) ---------------
class FixHistory:
    """Per-finding record of what each fix round did and what confirm said.

    The cycle calls :meth:`record_round` after every confirm (fresh or reused
    from a checkpoint, so a resumed cycle rebuilds the same history). Carried
    remainders (FR-6.1) link to their parent via ``carried_from`` so a
    remainder's history starts with the parent's attempts.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, list[dict[str, Any]]] = {}
        self._parent: dict[str, str] = {}

    def record_round(
        self,
        rnd: int,
        fix_sha: str | None,
        accepted_ids: list[str],
        cdata: dict[str, Any],
        remainders: list[dict[str, Any]] = (),  # type: ignore[assignment]
    ) -> None:
        verdicts: dict[str, dict[str, Any]] = {}
        for v in cdata.get("verdicts") or []:
            if isinstance(v, dict) and v.get("finding_id") is not None:
                verdicts[str(v["finding_id"])] = v  # duplicates: last wins
        for fid in accepted_ids:
            v = verdicts.get(fid)
            self._by_id.setdefault(fid, []).append({
                "round": rnd,
                "fix_sha": fix_sha,
                "verdict": (v or {}).get("verdict") or "unresolved",
                "notes": _condense(
                    (v or {}).get("notes")
                    or "no confirm verdict returned; treated as unresolved",
                    _NOTE_LIMIT,
                ),
            })
        for r in remainders or []:
            rid, parent = r.get("id"), r.get("carried_from")
            if rid and parent:
                self._parent[str(rid)] = str(parent)

    def history_for(self, fid: str) -> list[dict[str, Any]]:
        chain: list[str] = []
        cur: str | None = fid
        while cur is not None and cur not in chain:
            chain.append(cur)
            cur = self._parent.get(cur)
        out: list[dict[str, Any]] = []
        for link in reversed(chain):  # oldest ancestor first
            out.extend(dict(e) for e in self._by_id.get(link, []))
        return out


# --- building -------------------------------------------------------------------
def _decision_owed(target_artifact: str | None, recommendation: str | None) -> str:
    if target_artifact or recommendation == "amend_upstream":
        where = target_artifact or "the approved artifact"
        return (
            f"approve (amend {where} through human ratification, then resume) | "
            "amend (rule it in-scope / redline the contract) | reject-with-reason"
        )
    return "approve as proposed | amend (redline the contract) | reject-with-reason"


def build_brief(
    *,
    step_id: str,
    iteration: int | None,
    phase: str | None,
    kind: str,
    notes: str,
    open_findings: list[dict[str, Any]],
    verdicts_by_id: dict[str, dict[str, Any]],
    history: FixHistory,
    response_count: int,
    surfaced_ids: list[str] = (),  # type: ignore[assignment]
    rounds_run: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The deterministic per-finding digest (no contract yet).

    ``open_findings`` are finding dicts (an FR-10.5 forcing item also carries
    ``confirm_verdict``/``confirm_notes``); ``verdicts_by_id`` maps finding id →
    the latest triage verdict for it. ``response_count`` is how many human
    responses the step had at park time, so a reader can tell whether the
    brief predates a later response.
    """
    now = now or datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in open_findings:
        fid = str(f.get("id") or "?")
        if fid in seen:
            continue
        seen.add(fid)
        v = verdicts_by_id.get(fid) or {}
        fix_rounds = history.history_for(fid)
        residual = None
        if f.get("confirm_verdict"):
            residual = {
                "verdict": str(f["confirm_verdict"]),
                "notes": _condense(f.get("confirm_notes") or f.get("claim"), _NOTE_LIMIT),
            }
        elif fix_rounds:
            last = fix_rounds[-1]
            residual = {"verdict": last["verdict"], "notes": last["notes"]}
        items.append({
            "finding_id": fid,
            "severity": f.get("severity"),
            "category": f.get("category"),
            "location": f.get("location"),
            "claim": _condense(f.get("claim"), _CLAIM_LIMIT),
            "carried_from": f.get("carried_from"),
            "triage": {
                "verdict": v.get("verdict"),
                "action": v.get("action"),
                "confidence": v.get("confidence"),
                "target_artifact": v.get("target_artifact"),
                "reasoning": _condense(v.get("reasoning"), _NOTE_LIMIT),
            },
            "residual_gap": residual,
            "fix_rounds": fix_rounds,
            "proposed_contract": None,
            "decision_owed": _decision_owed(v.get("target_artifact"), None),
        })
    return {
        "version": BRIEF_VERSION,
        "step_id": step_id,
        "iteration": iteration,
        "phase": phase,
        "kind": kind,
        "parked_notes": notes,
        "rounds_run": rounds_run,
        "response_count": response_count,
        "created_at": now.isoformat(),
        "findings": items,
        "surfaced_ids": [str(s) for s in surfaced_ids],
        "contract": {"status": CONTRACT_NOT_CONFIGURED, "agent": None,
                     "error": None, "summary": None, "dropped_ids": []},
    }


def contract_prompt(template: str, brief: dict[str, Any], grounding: str,
                    wrap: Callable[[str], str]) -> str:
    """The fix-contract prompt: template + approved-artifact grounding + the
    digest wrapped as untrusted data (§8 — it quotes reviewer/confirmer text)."""
    digest = {
        "kind": brief.get("kind"),
        "phase": brief.get("phase"),
        "rounds_run": brief.get("rounds_run"),
        "open_findings": [
            {k: it.get(k) for k in (
                "finding_id", "severity", "category", "location", "claim",
                "carried_from", "triage", "residual_gap", "fix_rounds",
            )}
            for it in brief.get("findings") or []
        ],
    }
    return (
        template
        + grounding
        + "\n\n--- escalation brief (open findings, fix history, residual gaps) ---\n"
        + wrap(json.dumps(digest, indent=2, ensure_ascii=False))
    )


def mark_unavailable(brief: dict[str, Any], agent: str, error: str) -> None:
    brief["contract"] = {"status": CONTRACT_UNAVAILABLE, "agent": agent,
                         "error": _condense(error, _NOTE_LIMIT), "summary": None,
                         "dropped_ids": []}


def merge_contracts(brief: dict[str, Any], agent: str, structured: dict[str, Any]) -> None:
    """Attach the agent's contracts to their findings by id.

    A contract for an id not in the brief is dropped (recorded, never shown as
    if it applied to something). A finding the agent skipped keeps
    ``proposed_contract: null`` — the digest still stands on its own.
    """
    by_id = {it["finding_id"]: it for it in brief.get("findings") or []}
    dropped: list[str] = []
    attached = 0
    for c in structured.get("contracts") or []:
        if not isinstance(c, dict):
            continue
        fid = str(c.get("finding_id") or "")
        item = by_id.get(fid)
        if item is None:
            dropped.append(fid or "?")
            continue
        rec = c.get("recommendation")
        item["proposed_contract"] = {
            "proposed_change": str(c.get("proposed_change") or ""),
            "grounding": str(c.get("grounding") or ""),
            "acceptance_check": str(c.get("acceptance_check") or ""),
            "recommendation": rec if rec in RECOMMENDATIONS else None,
        }
        item["decision_owed"] = _decision_owed(
            (item.get("triage") or {}).get("target_artifact"), rec
        )
        attached += 1
    missing = [fid for fid, it in by_id.items() if it["proposed_contract"] is None]
    if attached == 0 and by_id:
        mark_unavailable(brief, agent, "the contract agent returned no contract "
                         "for any open finding")
        brief["contract"]["dropped_ids"] = dropped
        return
    brief["contract"] = {
        "status": CONTRACT_PROPOSED,
        "agent": agent,
        "error": (f"no contract for: {', '.join(missing)}" if missing else None),
        "summary": _condense(structured.get("summary"), _NOTE_LIMIT) or None,
        "dropped_ids": dropped,
    }


def park_note_suffix(brief: dict[str, Any]) -> str:
    """One line appended to the park notes. Deliberately free of finding ids and
    of the escalation sub-kind markers web/intel.py classifies on."""
    status = (brief.get("contract") or {}).get("status")
    what = {
        CONTRACT_PROPOSED: "with a proposed fix contract per open item",
        CONTRACT_UNAVAILABLE: "contract drafting failed; digest only",
    }.get(status, "digest only; set fix_contract_agent for proposed contracts")
    return (
        f"Escalation brief ({what}): artifacts/{BRIEF_ARTIFACT} — "
        "`gauntlet status` renders it."
    )


# --- reading ------------------------------------------------------------------
def load(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "artifacts" / BRIEF_ARTIFACT
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_for_step(run_dir: Path, step_id: str, iteration: int | None) -> dict[str, Any] | None:
    """The persisted brief iff it belongs to this step record (id + iteration).

    The cycle removes its brief when it converges, so the brief on disk for a
    step is always from that step's most recent escalation park."""
    brief = load(run_dir)
    if brief is None:
        return None
    if brief.get("step_id") != step_id or brief.get("iteration") != iteration:
        return None
    return brief


def load_answered(run_dir: Path, step_id: str, iteration: int | None,
                  responses: list[Any]) -> dict[str, Any] | None:
    """The brief a pending ``--response`` answers, or ``None``.

    Requires the latest response to be pending (we are on the re-drive that
    consumes it) and the brief to predate it (``response_count`` below the
    current count) — a brief written after the latest response is a newer
    park the human has not answered yet."""
    if not responses or getattr(responses[-1], "state", None) != "pending":
        return None
    brief = load_for_step(run_dir, step_id, iteration)
    if brief is None:
        return None
    try:
        count = int(brief.get("response_count", -1))
    except (TypeError, ValueError):
        return None
    return brief if 0 <= count < len(responses) else None


def answered_contracts_block(brief: dict[str, Any], wrap: Callable[[str], str]) -> str:
    """The contracts a pending response answers, for the re-drive prompts.

    Agent-drafted, so wrapped as data: the operator's response (rendered as the
    trusted decision block) is the authority, and a contract binds only as far
    as that response approves or amends it."""
    rows = []
    for it in brief.get("findings") or []:
        rows.append({
            "finding_id_at_park": it.get("finding_id"),
            "claim": it.get("claim"),
            "residual_gap": it.get("residual_gap"),
            "proposed_contract": it.get("proposed_contract"),
        })
    if not rows:
        return ""
    return (
        "\n\n--- PROPOSED FIX CONTRACT(S) presented at the park this response "
        "answers (agent-drafted; binding ONLY as far as the human decision "
        "above approves or amends each one — an approval adopts the contract "
        "as written, an amendment wins over the contract text, a rejection "
        "drops it; finding ids are from the parked round and may differ now) ---\n"
        + wrap(json.dumps(rows, indent=2, ensure_ascii=False))
        + "\n--- END PROPOSED FIX CONTRACT(S) ---"
    )


def status_block(brief: dict[str, Any], *, responses_now: int,
                 redact: Callable[[object], object]) -> dict[str, Any]:
    """The §6.1 ``status --json`` ``escalation`` object (redacted projection)."""
    def r(x: object) -> object:
        return redact(x) if isinstance(x, str) else x

    findings = []
    for it in brief.get("findings") or []:
        tri = it.get("triage") or {}
        gap = it.get("residual_gap")
        pc = it.get("proposed_contract")
        findings.append({
            "finding_id": str(it.get("finding_id")),
            "severity": it.get("severity"),
            "location": r(it.get("location")),
            "claim": r(it.get("claim")),
            "triage_verdict": tri.get("verdict"),
            "triage_action": tri.get("action"),
            "target_artifact": tri.get("target_artifact"),
            "residual_gap": (
                {"verdict": gap.get("verdict"), "notes": r(gap.get("notes"))}
                if isinstance(gap, dict) else None
            ),
            "fix_rounds": [
                {"round": e.get("round"), "fix_sha": e.get("fix_sha"),
                 "verdict": e.get("verdict"), "notes": r(e.get("notes"))}
                for e in it.get("fix_rounds") or []
            ],
            "proposed_contract": (
                {k: r(pc.get(k)) for k in (
                    "proposed_change", "grounding", "acceptance_check",
                )} | {"recommendation": pc.get("recommendation")}
                if isinstance(pc, dict) else None
            ),
            "decision_owed": it.get("decision_owed"),
        })
    contract = brief.get("contract") or {}
    try:
        later = max(0, responses_now - int(brief.get("response_count", responses_now)))
    except (TypeError, ValueError):
        later = 0
    return {
        "step_id": brief.get("step_id"),
        "kind": brief.get("kind"),
        "brief_path": f"artifacts/{BRIEF_ARTIFACT}",
        "created_at": brief.get("created_at"),
        "responses_since": later,
        "contract_status": contract.get("status"),
        "contract_agent": contract.get("agent"),
        "contract_error": r(contract.get("error")),
        "findings": findings,
        "surfaced_ids": list(brief.get("surfaced_ids") or []),
    }


def render_lines(block: dict[str, Any], *, slug: str) -> list[str]:
    """Human ``gauntlet status`` lines for an ``escalation`` status block."""
    out = [f"escalation brief ({block.get('kind')}; {len(block['findings'])} open):"]
    if block.get("responses_since"):
        out.append(f"  note: drafted before {block['responses_since']} later "
                   "response(s) — the latest park may concern something else")
    status = block.get("contract_status")
    if status == CONTRACT_UNAVAILABLE:
        out.append(f"  proposed contracts unavailable: {block.get('contract_error')}")
    elif status == CONTRACT_NOT_CONFIGURED:
        out.append("  (digest only — set `fix_contract_agent:` on the cycle step "
                   "for proposed fix contracts)")
    for f in block["findings"]:
        out.append(f"  {f['finding_id']} [{f.get('severity')}] {f.get('location') or ''}")
        out.append(f"    claim: {f.get('claim')}")
        gap = f.get("residual_gap")
        if gap:
            out.append(f"    residual gap ({gap.get('verdict')}): {gap.get('notes')}")
        for e in f.get("fix_rounds") or []:
            sha = (e.get("fix_sha") or "")[:10] or "-"
            out.append(f"    round {e.get('round')} fix {sha}: {e.get('verdict')} — "
                       f"{e.get('notes')}")
        if f.get("target_artifact"):
            out.append(f"    fix lands in approved artifact: {f['target_artifact']}")
        pc = f.get("proposed_contract")
        if pc:
            rec = f" [{pc.get('recommendation')}]" if pc.get("recommendation") else ""
            out.append(f"    proposed contract{rec}: {pc.get('proposed_change')}")
            out.append(f"      grounding: {pc.get('grounding')}")
            out.append(f"      acceptance: {pc.get('acceptance_check')}")
        out.append(f"    decision owed: {f.get('decision_owed')}")
    if block.get("surfaced_ids"):
        out.append("  also surfaced (non-blocking): " + ", ".join(block["surfaced_ids"]))
    out.append(
        f"  respond per item, e.g.: gauntlet resume {slug} --response "
        "\"<id>: approve; <id>: amend — <redline>; <id>: reject — <reason>\""
    )
    out.append(f"  full brief: {block.get('brief_path')}")
    return out


__all__ = [
    "BRIEF_ARTIFACT",
    "BUILTIN_CONTRACT_PROMPT",
    "BUILTIN_CONTRACT_SCHEMA",
    "DEFAULT_CONTRACT_PROMPT",
    "DEFAULT_CONTRACT_SCHEMA",
    "FixHistory",
    "KIND_CLOSURE",
    "KIND_MAX_ROUNDS",
    "KIND_TRIAGE",
    "answered_contracts_block",
    "build_brief",
    "contract_prompt",
    "load",
    "load_answered",
    "load_for_step",
    "mark_unavailable",
    "merge_contracts",
    "park_note_suffix",
    "render_lines",
    "status_block",
]
