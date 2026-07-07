"""§9 proposal-mode governance triggers (FR-5.1, P6).

Two of PRD §9's feedback paths are wired here into the **same ratified
retro-proposal flow** the lens governance uses, so the spec's kill/rollback
criteria are actually exercisable — and, per §9, nothing self-tunes: each trigger
emits a *ratifiable proposal* read by a human, never a config mutation. The only
*auto* actions in the system are fail-closed; a loosening (panel shrink, verifier
revert) is always a proposal.

- **Panel-shrink (§9 ensemble-yield kill criterion, §1.3).** When a panel member
  contributes < 25% of a run's unique-after-dedup legitimate findings across two
  consecutive comparison runs (from P1's ``metrics.ensemble.unique_legit_by_member``),
  emit a proposal that removes that member from the pipeline's ``reviewers:``
  list, citing both runs.
- **Verifier-revert-to-opt-in (§9 behavioral-signal miss).** When the verifier's
  triage-legitimate yield (P5's ``metrics.verifier.legit_findings``) averages
  below the §9 threshold across the first three verifier-enabled runs, emit a
  proposal that removes the ``verifier:`` sub-step from the pipeline (reverting it
  from default to opt-in), citing those runs.

Both read the metrics P1/P5 already persist to the manifest; both are
deterministic (no LLM judgement — reproducible and auditable). The diff is a real
``git apply``-able unified diff against the shipped pipeline, validated by the
proposals machinery exactly like an LLM-synthesised proposal — so a fired trigger
lands as a ``pending`` proposal the human ratifies, and nothing changes without
that ratification.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

# §9 ratified thresholds (Q5): 25% ensemble yield, ≥1 legit behavioral finding/run.
PANEL_YIELD_THRESHOLD = 0.25
VERIFIER_LEGIT_THRESHOLD = 1.0
VERIFIER_WINDOW = 3  # §9 window: the first three verifier-enabled runs


# --- corpus reading ----------------------------------------------------------
def read_corpus(
    repo_root: Path, run_root: str, slug: str, *, current: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Every completed run manifest for this PRD family, oldest first.

    Loads ``<run_root>/<slug>/run-*/manifest.json`` (run ids are timestamp-based,
    so lexicographic sort is chronological). ``current`` (the in-flight run's
    manifest, whose metrics may not yet be flushed to disk) overrides any on-disk
    copy of the same run id, so the corpus always reflects the freshest state.
    """
    base = repo_root / run_root / slug
    runs: dict[str, dict[str, Any]] = {}
    if base.exists():
        for mpath in base.glob("run-*/manifest.json"):
            try:
                obj = json.loads(mpath.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            rid = obj.get("run_id")
            if rid:
                runs[rid] = obj
    if current is not None and current.get("run_id"):
        runs[current["run_id"]] = current
    return [runs[k] for k in sorted(runs)]


def run_ensemble_members(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-member unique-legit yield summed across a run's cycles (FR-1.3)."""
    members: dict[str, dict[str, Any]] = {}
    for step in manifest.get("steps") or []:
        ens = ((step.get("metrics") or {}).get("ensemble") or {}).get(
            "unique_legit_by_member"
        ) or {}
        for key, m in ens.items():
            entry = members.setdefault(
                key, {"profile": m.get("profile"), "lens": m.get("lens"), "unique_legit": 0}
            )
            entry["unique_legit"] += int(m.get("unique_legit", 0) or 0)
    return members


def run_verifier_legit(manifest: dict[str, Any]) -> int | None:
    """Triage-legitimate behavioral findings summed across a run's cycles, or
    ``None`` when no verifier ran in the run (not a verifier-enabled run)."""
    total: int | None = None
    for step in manifest.get("steps") or []:
        v = (step.get("metrics") or {}).get("verifier")
        if v is not None:
            total = (total or 0) + int(v.get("legit_findings", 0) or 0)
    return total


# --- deterministic pipeline transforms ---------------------------------------
_REVIEWERS_RE = re.compile(r"reviewers:\s*\[(?P<body>.*?)\]", re.DOTALL)
_MEMBER_RE = re.compile(r"\{[^{}]*\}")


def remove_reviewer(text: str, profile: str) -> str | None:
    """Remove the panel member with ``profile`` from every ``reviewers: [...]``
    flow list in ``text``. Returns the new text, or ``None`` if the profile was
    not found in any panel (nothing to shrink)."""
    changed = False
    prof_re = re.compile(rf"profile:\s*{re.escape(profile)}\b")

    def _repl(m: re.Match[str]) -> str:
        nonlocal changed
        members = _MEMBER_RE.findall(m.group("body"))
        kept = [mm.strip() for mm in members if not prof_re.search(mm)]
        if len(kept) == len(members):
            return m.group(0)
        changed = True
        return "reviewers: [" + ", ".join(kept) + "]"

    new = _REVIEWERS_RE.sub(_repl, text)
    return new if changed else None


_VERIFIER_LINE_RE = re.compile(r"^[ \t]*verifier:\s*\S+,?[ \t]*\n", re.MULTILINE)


def remove_verifier(text: str) -> str | None:
    """Remove the ``verifier:`` sub-step line(s) from a pipeline, reverting the
    verifier from default to opt-in. Returns new text, or ``None`` if absent."""
    new, n = _VERIFIER_LINE_RE.subn("", text)
    return new if n else None


def _unified_diff(rel: str, old: str, new: str) -> str:
    """A ``git apply``-able unified diff (``a/``/``b/`` prefixes) between two
    versions of the same file's text."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    return "".join(diff)


def _pipeline_rel(asset_root: str, name: str) -> str:
    prefix = "" if asset_root.strip("/") in ("", ".") else asset_root.strip("/") + "/"
    return f"{prefix}pipelines/{name}.yaml"


def _pipeline_text(repo_root: Path, asset_root: str, name: str) -> str | None:
    path = repo_root / asset_root / f"pipelines/{name}.yaml"
    try:
        return path.read_text()
    except OSError:
        return None


# --- trigger evaluation ------------------------------------------------------
def panel_shrink_items(
    corpus: list[dict[str, Any]],
    repo_root: Path,
    asset_root: str,
    pipeline_name: str,
    *,
    threshold: float = PANEL_YIELD_THRESHOLD,
) -> list[dict[str, Any]]:
    """A panel-shrink proposal item per member below ``threshold`` unique-legit
    yield across the last two consecutive comparison (≥2-member) runs (§9)."""
    comparison = [
        (m["run_id"], run_ensemble_members(m))
        for m in corpus
        if len(run_ensemble_members(m)) >= 2 and m.get("run_id")
    ]
    if len(comparison) < 2:
        return []
    (r1_id, r1), (r2_id, r2) = comparison[-2], comparison[-1]

    def _below(members: dict[str, dict[str, Any]], key: str) -> bool | None:
        total = sum(e["unique_legit"] for e in members.values())
        if total <= 0:
            return None  # cannot judge a yield share when nobody was legit
        return (members[key]["unique_legit"] / total) < threshold

    text = _pipeline_text(repo_root, asset_root, pipeline_name)
    if text is None:
        return []
    rel = _pipeline_rel(asset_root, pipeline_name)
    items: list[dict[str, Any]] = []
    for key in sorted(set(r1) & set(r2)):
        if _below(r1, key) and _below(r2, key):
            profile = r2[key].get("profile") or key.split("::", 1)[0]
            new = remove_reviewer(text, profile)
            if not new or new == text:
                continue
            items.append({
                "slug": f"shrink-panel-{profile}",
                "target_path": rel,
                "rationale": (
                    "§9 ensemble-yield kill criterion (§1.3): panel member "
                    f"'{profile}' contributed <{int(threshold * 100)}% of the "
                    "unique-after-dedup legitimate findings across two consecutive "
                    f"comparison runs ({r1_id}, {r2_id}). Proposing removal from the "
                    "review panel. The panel changes only on human ratification — "
                    "nothing shrinks without approving this proposal."
                ),
                "diff": _unified_diff(rel, text, new),
            })
    return items


def verifier_revert_item(
    corpus: list[dict[str, Any]],
    repo_root: Path,
    asset_root: str,
    pipeline_name: str,
    *,
    threshold: float = VERIFIER_LEGIT_THRESHOLD,
    window: int = VERIFIER_WINDOW,
) -> dict[str, Any] | None:
    """A verifier-revert-to-opt-in proposal item when the verifier's legit yield
    averages below ``threshold`` across the first ``window`` verifier-enabled
    runs (§9 behavioral-signal miss)."""
    verifier_runs = [
        (m["run_id"], run_verifier_legit(m))
        for m in corpus
        if run_verifier_legit(m) is not None and m.get("run_id")
    ]
    if len(verifier_runs) < window:
        return None
    first = verifier_runs[:window]
    mean = sum(v for _, v in first) / window
    if mean >= threshold:
        return None
    text = _pipeline_text(repo_root, asset_root, pipeline_name)
    if text is None:
        return None
    new = remove_verifier(text)
    if not new or new == text:
        return None
    rel = _pipeline_rel(asset_root, pipeline_name)
    ids = ", ".join(rid for rid, _ in first)
    return {
        "slug": "revert-verifier-to-opt-in",
        "target_path": rel,
        "rationale": (
            "§9 behavioral-signal miss: verifier triage-legitimate findings "
            f"averaged {mean:.2f} (< {threshold}) across the first {window} "
            f"verifier-enabled runs ({ids}). Proposing revert of the verifier from "
            "default to opt-in. The profile config changes only on human "
            "ratification — nothing reverts without approving this proposal."
        ),
        "diff": _unified_diff(rel, text, new),
    }


# --- orchestration -----------------------------------------------------------
def build_governance_proposals(ctx: Any) -> list[Any]:
    """Evaluate the §9 triggers against this family's run corpus and materialize
    any fired proposals as ``pending`` files (validated by the proposals
    machinery — path-contained + ``git apply``-checked). Returns the written
    :class:`Proposal` objects (empty when no trigger fires)."""
    from gauntlet.engine.proposals import materialize_proposals

    repo_root = ctx.repo_root
    asset_root = ctx.config.asset_root
    pipeline_name = ctx.manifest.pipeline.name
    corpus = read_corpus(
        repo_root, ctx.config.run_root, ctx.manifest.slug,
        current=ctx.manifest.model_dump(mode="json"),
    )
    items: list[dict[str, Any]] = []
    items.extend(panel_shrink_items(corpus, repo_root, asset_root, pipeline_name))
    vr = verifier_revert_item(corpus, repo_root, asset_root, pipeline_name)
    if vr is not None:
        items.append(vr)
    if not items:
        return []
    proposals_dir = ctx.run_dir / "retro" / "proposals"
    return materialize_proposals(
        repo_root, proposals_dir, items,
        source_run=ctx.manifest.run_id, writer=ctx.writer, asset_root=asset_root,
    )
