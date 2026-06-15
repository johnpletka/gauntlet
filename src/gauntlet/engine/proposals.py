"""Improvement proposals: path-contained diffs + governed apply (FR-6.3/6.4/6.5).

Proposal generation (FR-6.3) writes the cheap model's synthesised diffs here as
``retro/proposals/NNN-<slug>.md`` — rationale + the literal unified diff. Each
proposal is **path-contained** (review F-001 / plan §0 trust model): a diff may
touch only the versioned-asset allowlist (``prompts/``, ``pipelines/``,
``schemas/``, ``policy.yaml``) via repo-relative paths. Anything else is rejected
at parse time, before a human is ever asked.

``gauntlet proposals review`` (FR-6.4) presents the pending proposals; the human
approves or rejects each. An approved diff is applied and committed with the
proposal as the commit body, and the rationale is appended to
``prompts/CHANGELOG.md`` (FR-6.5). **No proposal self-applies** — generation only
ever writes ``status: pending`` files; apply is a separate, human-driven,
engine-deterministic step (never an agent action).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gauntlet.engine import gitops
from gauntlet.engine.gitops import Identity
from gauntlet.logging.redact import RedactingWriter

# Versioned-asset allowlist (review F-001). A proposal diff may touch only paths
# under these prefixes or exactly these files; anything else is refused before
# the human sees it. Kept deliberately small and explicit.
ALLOWLIST_PREFIXES = ("prompts/", "pipelines/", "schemas/")
ALLOWLIST_FILES = ("policy.yaml",)

# Proposal lifecycle states.
PENDING = "pending"
APPLIED = "applied"
REJECTED = "rejected"
INVALID = "invalid"  # path-escape or non-applying diff: never approvable

_CHANGELOG_ANCHOR = "<!-- gauntlet:changelog -->"


# --- path containment (the security control) ---------------------------------
def _allowlist_for(asset_root: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Concrete ``(prefixes, files)`` allowlist under ``asset_root``.

    The bare ALLOWLIST_* are repo-root-relative; an adopter's assets live under
    ``asset_root`` (e.g. ``.gauntlet``), so the diff paths — and therefore the
    allowlist they are checked against — carry that prefix. ``asset_root="."``
    (the default / Gauntlet's own layout) yields the bare allowlist unchanged.
    """
    base = "" if asset_root.strip("/") in ("", ".") else asset_root.strip("/") + "/"
    return (tuple(base + p for p in ALLOWLIST_PREFIXES),
            tuple(base + f for f in ALLOWLIST_FILES))


def path_allowed(path: str, asset_root: str = ".") -> bool:
    """True iff ``path`` is a repo-relative path inside the asset allowlist.

    Absolute paths, ``..`` traversal, and anything outside the allowlist are
    rejected — this is the parse-time containment the trust model relies on.
    The allowlist is taken under ``asset_root`` (default "." = the repo root).
    """
    p = path.strip()
    if not p or p.startswith("/") or p.startswith("~"):
        return False
    parts = p.split("/")
    if ".." in parts or "" in parts[:-1]:
        return False
    prefixes, files = _allowlist_for(asset_root)
    if p in files:
        return True
    return any(p.startswith(prefix) for prefix in prefixes)


def diff_target_paths(diff: str) -> list[str]:
    """Repo-relative paths a unified diff touches (from ---/+++/diff --git lines).

    ``a/``/``b/`` prefixes are stripped; ``/dev/null`` (a pure add/delete side)
    is ignored. Order-preserving, de-duplicated.
    """
    paths: list[str] = []

    def _add(raw: str) -> None:
        raw = raw.strip()
        if not raw or raw == "/dev/null":
            return
        for pre in ("a/", "b/"):
            if raw.startswith(pre):
                raw = raw[len(pre):]
                break
        if raw not in paths:
            paths.append(raw)

    for line in diff.splitlines():
        if line.startswith("--- "):
            _add(line[4:])
        elif line.startswith("+++ "):
            _add(line[4:])
        elif line.startswith("diff --git "):
            for tok in line[len("diff --git "):].split():
                _add(tok)
    return paths


def path_containment(diff: str, asset_root: str = ".") -> tuple[bool, list[str]]:
    """``(ok, offending_paths)`` — every touched path must be in the allowlist."""
    targets = diff_target_paths(diff)
    offending = [p for p in targets if not path_allowed(p, asset_root)]
    return (not offending and bool(targets)), offending


# --- proposal model + (de)serialisation --------------------------------------
@dataclass
class Proposal:
    number: int
    slug: str
    status: str
    source_run: str
    targets: list[str]
    rationale: str
    diff: str
    valid: bool = True
    invalid_reason: str = ""
    path: Path | None = None

    @property
    def name(self) -> str:
        return f"{self.number:03d}-{self.slug}"


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "proposal").lower()).strip("-")
    return s or "proposal"


def render_proposal(p: Proposal) -> str:
    targets = ", ".join(p.targets) if p.targets else "(none)"
    lines = [
        f"# Proposal {p.number:03d}: {p.slug}",
        "",
        f"- status: {p.status}",
        f"- source_run: {p.source_run}",
        f"- targets: {targets}",
        f"- valid: {'true' if p.valid else 'false'}",
    ]
    if p.invalid_reason:
        lines.append(f"- invalid_reason: {p.invalid_reason}")
    lines += ["", "## Rationale", "", p.rationale.strip() or "(none given)", ""]
    lines += ["## Diff", "", "```diff", p.diff.rstrip("\n"), "```", ""]
    return "\n".join(lines) + "\n"


_FIELD_RE = re.compile(r"^- (\w+):\s*(.*)$")
_DIFF_RE = re.compile(r"```diff\n(.*)\n```\s*$", re.DOTALL)
_TITLE_RE = re.compile(r"^# Proposal (\d+):\s*(.*)$", re.MULTILINE)


def parse_proposal(path: Path) -> Proposal:
    text = path.read_text()
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = _FIELD_RE.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    title = _TITLE_RE.search(text)
    number = int(title.group(1)) if title else 0
    slug = title.group(2).strip() if title else fields.get("slug", "proposal")
    diff_m = _DIFF_RE.search(text)
    diff = diff_m.group(1) if diff_m else ""
    rationale = ""
    if "## Rationale" in text:
        after = text.split("## Rationale", 1)[1]
        rationale = after.split("## Diff", 1)[0].strip()
    targets_raw = fields.get("targets", "")
    targets = [t.strip() for t in targets_raw.split(",") if t.strip() and t.strip() != "(none)"]
    return Proposal(
        number=number,
        slug=slug,
        status=fields.get("status", PENDING),
        source_run=fields.get("source_run", ""),
        targets=targets,
        rationale=rationale,
        diff=diff,
        valid=fields.get("valid", "true") == "true",
        invalid_reason=fields.get("invalid_reason", ""),
        path=path,
    )


# --- listing + numbering ------------------------------------------------------
def list_proposals(proposals_dir: Path) -> list[Proposal]:
    if not proposals_dir.exists():
        return []
    out = [parse_proposal(p) for p in sorted(proposals_dir.glob("*.md"))]
    return sorted(out, key=lambda p: p.number)


def next_proposal_number(proposals_dir: Path) -> int:
    existing = list_proposals(proposals_dir)
    return (max((p.number for p in existing), default=0)) + 1


# --- generation: turn synthesiser output into pending proposal files ----------
def materialize_proposals(
    repo_root: Path,
    proposals_dir: Path,
    items: list[dict[str, Any]],
    *,
    source_run: str,
    writer: RedactingWriter,
    asset_root: str = ".",
) -> list[Proposal]:
    """Validate + write the synthesiser's raw proposals as ``pending`` files.

    Path-containment (review F-001) and ``git apply --check`` run here, at
    generation time, so an out-of-allowlist or non-applying diff is recorded as
    ``invalid`` (visible to the human — data over inference — but never
    approvable), not silently dropped.
    """
    proposals_dir.mkdir(parents=True, exist_ok=True)
    written: list[Proposal] = []
    number = next_proposal_number(proposals_dir)
    for item in items:
        diff = item.get("diff", "") or ""
        slug = _slugify(item.get("slug", ""))
        declared = (item.get("target_path") or "").strip()
        targets = diff_target_paths(diff)
        valid, reason = _validate_diff(repo_root, diff, declared, targets, asset_root)
        proposal = Proposal(
            number=number,
            slug=slug,
            status=PENDING if valid else INVALID,
            source_run=source_run,
            targets=targets or ([declared] if declared else []),
            rationale=item.get("rationale", ""),
            diff=diff,
            valid=valid,
            invalid_reason=reason,
        )
        proposal.path = proposals_dir / f"{proposal.name}.md"
        writer.write_text(proposal.path, render_proposal(proposal))
        written.append(proposal)
        number += 1
    return written


def _validate_diff(
    repo_root: Path, diff: str, declared: str, targets: list[str], asset_root: str = "."
) -> tuple[bool, str]:
    if not diff.strip():
        return False, "empty diff"
    contained, offending = path_containment(diff, asset_root)
    if not contained:
        if not targets:
            return False, "diff names no target file (unparseable)"
        return False, (
            "diff escapes the versioned-asset allowlist (prompts/, pipelines/, "
            f"schemas/, policy.yaml): {', '.join(offending)}"
        )
    # Single-file contract (F-005): the proposal schema/prompt say each proposal
    # edits exactly ONE file, and apply stages all touched paths under one
    # approval — so a diff touching several allowlisted files would apply
    # multiple asset changes under a single rationale/review decision. Reject
    # anything whose de-duplicated target set is not exactly one path.
    if len(targets) != 1:
        return False, (
            "a proposal must edit exactly one file (proposal contract), but the "
            f"diff touches {len(targets)}: {', '.join(targets)}"
        )
    if declared and not path_allowed(declared, asset_root):
        return False, f"declared target_path {declared!r} is outside the allowlist"
    if declared and declared not in targets:
        return False, (
            f"declared target_path {declared!r} does not match the diff's "
            f"actual target(s) {targets}"
        )
    if not gitops.apply_patch_check(repo_root, _ensure_trailing_nl(diff)):
        return False, "diff does not apply cleanly to the current asset"
    return True, ""


def _ensure_trailing_nl(diff: str) -> str:
    return diff if diff.endswith("\n") else diff + "\n"


# --- governed apply / reject (FR-6.4) ----------------------------------------
class ProposalError(RuntimeError):
    """A proposal cannot be applied (guard tripped); fail closed."""


def _changelog_rel(repo_root: Path, changelog_path: Path) -> str:
    try:
        return changelog_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:  # pragma: no cover - defensive: changelog outside the repo
        return "prompts/CHANGELOG.md"


def apply_proposal(
    repo_root: Path,
    proposal: Proposal,
    *,
    identity: Identity,
    changelog_path: Path,
    timestamp: str,
    asset_root: str = ".",
) -> str:
    """Apply an approved proposal: patch + CHANGELOG append + commit. Returns SHA.

    Re-validates everything at apply time (defense in depth): a non-pending
    proposal, an invalid one, or a diff that no longer applies / escapes the
    allowlist is refused. The CHANGELOG accumulation (FR-6.5) and the applied
    asset are committed together with the proposal as the commit body; the
    commit is engine-made (no agent), which is what "no proposal self-applies"
    means in practice.
    """
    if proposal.status != PENDING:
        raise ProposalError(
            f"proposal {proposal.name} is {proposal.status!r}, not pending; "
            "refusing to apply (FR-6.4 governance)"
        )
    if not proposal.valid:
        raise ProposalError(
            f"proposal {proposal.name} is invalid ({proposal.invalid_reason}); "
            "refusing to apply"
        )
    # Re-run the FULL generation-time contract against the diff itself, not the
    # mutable markdown `valid:`/`targets:` fields (review): a proposal file
    # edited between generation and approval could otherwise smuggle a different
    # or multi-file allowlisted diff through under one human approval. Recompute
    # the targets from the diff and re-validate (containment + single-file +
    # applies-cleanly), so apply enforces exactly what generation did.
    targets = diff_target_paths(proposal.diff)
    ok, reason = _validate_diff(repo_root, proposal.diff, "", targets, asset_root)
    if not ok:
        raise ProposalError(
            f"proposal {proposal.name} failed apply-time revalidation: {reason} "
            "(the proposal file may have been edited since generation)"
        )
    patch = _ensure_trailing_nl(proposal.diff)
    gitops.apply_patch(repo_root, patch)
    append_changelog(changelog_path, proposal, timestamp)
    message = _commit_message(proposal, timestamp)
    # Stage exactly the patched asset(s) + the CHANGELOG — never `add -A`, so a
    # gitignored-or-not run dir can never ride into the commit. Use the targets
    # recomputed from the diff (not the markdown field) so staging matches what
    # was actually validated and patched.
    paths = list(dict.fromkeys(targets + [_changelog_rel(repo_root, changelog_path)]))
    sha = gitops.commit_paths(repo_root, message, paths, identity=identity)
    proposal.status = APPLIED
    if proposal.path is not None:
        proposal.path.write_text(render_proposal(proposal))
    return sha


def reject_proposal(proposal: Proposal, notes: str) -> None:
    proposal.status = REJECTED
    proposal.invalid_reason = (
        f"{proposal.invalid_reason}; rejected: {notes}".lstrip("; ")
        if proposal.invalid_reason else f"rejected: {notes}"
    )
    if proposal.path is not None:
        proposal.path.write_text(render_proposal(proposal))


def append_changelog(changelog_path: Path, proposal: Proposal, timestamp: str) -> None:
    """Append the approved proposal's rationale to ``prompts/CHANGELOG.md``.

    Append-only (CLAUDE.md §8): never rewrites existing history. Creates the
    file with a header if a freshly ``init``'d repo has none yet.
    """
    targets = ", ".join(proposal.targets) if proposal.targets else "(none)"
    entry = (
        f"\n## {timestamp} — proposal {proposal.number:03d} `{proposal.slug}`\n\n"
        f"- source run: `{proposal.source_run}`\n"
        f"- targets: `{targets}`\n\n"
        f"{proposal.rationale.strip() or '(no rationale recorded)'}\n"
    )
    if not changelog_path.exists():
        changelog_path.parent.mkdir(parents=True, exist_ok=True)
        changelog_path.write_text(
            "# Prompt & policy changelog\n\n"
            "Append-only record of approved improvement proposals (FR-6.5).\n\n"
            f"{_CHANGELOG_ANCHOR}\n"
        )
    changelog_path.write_text(changelog_path.read_text().rstrip("\n") + "\n" + entry)


def _commit_message(proposal: Proposal, timestamp: str) -> str:
    targets = ", ".join(proposal.targets) if proposal.targets else "(none)"
    header = f"retro: apply proposal {proposal.number:03d} {proposal.slug}"
    if len(header) > 72:
        header = header[:72]
    body = (
        f"Approved improvement proposal {proposal.name} (FR-6.4), generated from "
        f"run {proposal.source_run} and ratified by a human via "
        f"`gauntlet proposals review`.\n\n"
        f"Targets: {targets}\n"
        f"Applied: {timestamp}\n\n"
        f"Rationale:\n{proposal.rationale.strip() or '(none given)'}\n"
    )
    return f"{header}\n\n{body}"
