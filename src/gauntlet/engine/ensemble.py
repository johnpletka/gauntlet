"""Deterministic pre-triage merge/dedup for ensemble review (FR-1.2, PRD §6).

An ensemble review round runs a *panel* of reviewers (distinct lenses on
distinct profiles). Each member returns findings-schema output. Before triage —
which is priced per finding (FR-1.2) — the panel's findings are merged by this
module's fully-specified, deterministic rule so the same defect raised by two
members is triaged once, while genuinely distinct findings are never dropped.

This is the canonical definition the plan pins ("two builders following it
produce identical merged sets"). Every rule below is mechanical — no LLM
judgement — so a merge is reproducible and auditable.

Merge rule (all four required): two findings merge into one primary iff they
share the same ``file``, their locations overlap (the normalized-location model
below), share the same ``category``, **and** their claim fingerprints share the
keyword core (Jaccard ≥ threshold). Location overlap + same category alone is
*not* sufficient — a whole-file/line-less finding overlaps *every* finding in
its file, so merging on location+category alone would silently drop a distinct
claim. Only primaries reach triage (FR-1.2), so dedup **fails toward keeping
findings**: an un-merged duplicate is one wasted triage call; an over-merge is a
lost defect.

Note (plan vs. PRD §6 prose, deliberate): the plan's canonical spec makes an
*invalid/unparseable* location **fail open to non-overlap** (never merged),
whereas PRD §6's prose sketches "treat as whole-file if the file is known". The
plan is the current-phase authority and its rule is strictly fail-toward-keeping
(never drops a finding on an unparseable location); this module implements the
plan's rule, tested accordingly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# The engine/merge-annotated fields (FR-1.2 / plan review F-007). They are
# NEVER emitted by a reviewer agent's strict structured output — ``source``/
# ``lens`` are stamped per member by the engine, ``duplicate_of``/``sources``/
# ``source_members`` are written here by the merge. They are absent from a
# single-reviewer artifact.
ENSEMBLE_FIELDS = ("source", "lens", "duplicate_of", "sources", "source_members")

# Claim-fingerprint Jaccard tie value (FR-1.2). Pinned + documented, not magic;
# the cycle passes the config-overridable value, defaulting to this.
DEFAULT_JACCARD_THRESHOLD = 0.5

# Fixed severity order for primary selection (highest first).
SEVERITY_ORDER = {"blocking": 0, "major": 1, "minor": 2, "nit": 3}
_SEVERITY_FALLBACK = len(SEVERITY_ORDER)  # unknown severities sort last

# Versioned v1 stopword list for the claim fingerprint (FR-1.2). Checked in with
# the merge module so the fingerprint is reproducible; articles, prepositions,
# auxiliaries, conjunctions, and pronoun/deictic filler only. Semantically loaded
# words (negations like ``not``/``no``, modals of obligation left in as content)
# are deliberately NOT dropped — dropping them would over-merge opposite claims,
# and dedup fails toward keeping findings. Bump the label on any change.
STOPWORDS_VERSION = "stopwords@v1"
STOPWORDS: frozenset[str] = frozenset(
    {
        # articles
        "a", "an", "the",
        # coordinating/subordinating conjunctions
        "and", "or", "but", "if", "then", "so", "as", "than", "because",
        "while", "although", "though", "whereas",
        # prepositions
        "in", "on", "at", "to", "for", "of", "with", "by", "from", "into",
        "onto", "over", "under", "about", "against", "between", "through",
        "during", "before", "after", "above", "below", "up", "down", "out",
        "off", "per", "via", "within", "without", "upon", "across",
        # auxiliaries / copulas
        "is", "are", "was", "were", "be", "been", "being", "am",
        "has", "have", "had", "having", "do", "does", "did", "done",
        # pronouns / deictic filler
        "it", "its", "this", "that", "these", "those", "they", "them",
        "their", "theirs", "there", "here", "we", "our", "ours", "us",
        "you", "your", "yours", "i", "me", "my", "mine", "he", "she",
        "him", "her", "his", "hers", "who", "whom", "whose", "which", "what",
        # misc filler
        "s", "t",
    }
)

_PUNCT_RE = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True)
class Location:
    """A finding location normalized to the §6 model.

    ``valid`` is False for an unparseable location (fail-open: it overlaps
    nothing). A whole-file/line-less location has all of
    ``start``/``end``/``section`` None with ``valid`` True.
    """

    file: str | None
    start: int | None
    end: int | None
    section: str | None
    valid: bool = True

    @property
    def whole_file(self) -> bool:
        return (
            self.valid
            and self.file is not None
            and self.start is None
            and self.end is None
            and self.section is None
        )


_LINE_RE = re.compile(r"^\d+$")
_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
# Common reviewer-emitted line shapes (PR #59 review F-008) — previously these
# silently degraded to *section* kind in a code file, where the cross-kind rule
# made them undeduplicable against genuine line ranges:
#   "12:5"            line:column  ⇒ line 12 (column dropped)
#   "12, 15" / "3,9"  comma list   ⇒ conservative envelope [min, max]
#   "12-14 (loop)"    range + parenthetical comment ⇒ the range
_LINECOL_RE = re.compile(r"^(\d+):(\d+)$")
_LIST_RE = re.compile(r"^\d+(\s*,\s*\d+)+$")
_ANNOTATED_RE = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?\s*\(.*\)$")
# "looks like it was meant to be a line/range ref" — only digits and dashes but
# not one of the two valid forms ⇒ a malformed number ⇒ invalid location.
_NUMERICISH_RE = re.compile(r"^[\d-]+$")


def parse_location(raw: str | None) -> Location:
    """Parse a finding ``location`` string to the normalized §6 model.

    Canonical parser (plan "Location grammar"):
      * split on the FIRST ``:`` — left is ``file`` (path as written, no
        resolution); everything right is the locator.
      * a ``file`` with no ``:`` (and hence no locator) ⇒ whole-file.
      * locator ``<n>`` ⇒ single line ``[n, n]``; ``<a>-<b>`` ⇒ inclusive range;
        ``<n>:<col>`` ⇒ line ``[n, n]``; ``<a>, <b>, …`` ⇒ envelope
        ``[min, max]``; ``<n>``/``<a>-<b>`` + a parenthetical comment ⇒ the
        line/range; ``§<s>``/``#<s>``/bare non-numeric text ⇒ section; empty ⇒
        whole-file.
      * a locator that looks numeric (digits/dashes) but is not a valid
        line/range ⇒ **invalid** (``valid=False``).
    """
    if raw is None:
        return Location(file=None, start=None, end=None, section=None)
    text = raw.strip()
    if not text:
        return Location(file=None, start=None, end=None, section=None)
    if ":" not in text:
        # A bare file (or bare section text with no file) ⇒ whole-file on that
        # name. Path/section text as written is the "file" key for same-file
        # comparison.
        return Location(file=text, start=None, end=None, section=None)
    file, _, locator = text.partition(":")
    file = file.strip()
    locator = locator.strip()
    if not locator:
        return Location(file=file, start=None, end=None, section=None)
    if locator[0] in "§#":
        section = _canonical_section(locator)
        return Location(file=file, start=None, end=None, section=section)
    m = _LINE_RE.match(locator)
    if m:
        n = int(locator)
        return Location(file=file, start=n, end=n, section=None)
    m = _RANGE_RE.match(locator)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = (a, b) if a <= b else (b, a)
        return Location(file=file, start=lo, end=hi, section=None)
    m = _LINECOL_RE.match(locator)
    if m:  # "12:5" line:column — the column is sub-line detail, keep the line
        n = int(m.group(1))
        return Location(file=file, start=n, end=n, section=None)
    m = _LIST_RE.match(locator)
    if m:  # "12, 15" — conservative envelope; over-covers, never wrong-kind
        nums = [int(x) for x in re.findall(r"\d+", locator)]
        return Location(file=file, start=min(nums), end=max(nums), section=None)
    m = _ANNOTATED_RE.match(locator)
    if m:  # "12-14 (the loop)" — the parenthetical is commentary, not a section
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        lo, hi = (a, b) if a <= b else (b, a)
        return Location(file=file, start=lo, end=hi, section=None)
    if _NUMERICISH_RE.match(locator):
        # digits/dashes but not a valid line or range ⇒ malformed number.
        return Location(file=file, start=None, end=None, section=None, valid=False)
    # bare non-numeric text ⇒ a section
    section = _canonical_section(locator)
    return Location(file=file, start=None, end=None, section=section)


_SECTION_LEADERS = ("§", "#", "sec.", "section")


def _strip_leader(segment: str) -> str:
    for leader in _SECTION_LEADERS:
        if segment.startswith(leader):
            return segment[len(leader):].strip()
    return segment


def _canonical_section(text: str) -> str:
    """Canonicalize a section id to a dotted heading path: lowercase, collapse
    whitespace, strip a leading section marker off every path segment, and
    normalize the hierarchy separators reviewers actually emit (``/``, ``>``)
    to ``.``. ``§5/FR-6.1`` and ``#5 > FR-6.1`` both canonicalize to
    ``5.fr-6.1``, so the §6 prefix rule (``§5`` overlaps ``§5/FR-6.1``) holds
    across notations — not only for dotted ids (PR #59 review F-001).
    Whitespace inside a segment (a heading *title*) is never a separator."""
    s = " ".join(text.strip().lower().split())
    segments = [
        _strip_leader(p.strip()) for p in re.split(r"\s*[/>]\s*", s) if p.strip()
    ]
    return ".".join(seg for seg in segments if seg)


def section_is_prefix(a: str, b: str) -> bool:
    """True iff section ``a`` is a prefix of section ``b`` (``4`` matches ``4.2``
    but not ``42``): ``b == a`` or ``b`` starts with ``a + "."``. Canonical
    sections are dotted paths (``_canonical_section``), so this covers ``/``-
    and ``>``-separated heading paths too."""
    return b == a or b.startswith(a + ".")


def _ranges_overlap(a: Location, b: Location) -> bool:
    return a.start <= b.end and b.start <= a.end  # type: ignore[operator]


def locations_overlap(a: Location, b: Location) -> bool:
    """Deterministic overlap for the dedup rule (§6).

    Invalid ⇒ never overlaps (fail-open). Different file ⇒ never overlaps. A
    whole-file/line-less location overlaps any location in the same file. Two
    line ranges overlap iff their inclusive ranges intersect (touching endpoints
    count). Two sections overlap iff one path is a prefix of the other. Line and
    section kinds are never compared across kinds (only whole-file bridges them).
    """
    if not a.valid or not b.valid:
        return False
    if a.file is None or b.file is None or a.file != b.file:
        return False
    if a.whole_file or b.whole_file:
        return True  # a file-scoped finding subsumes line/section-scoped ones
    a_lines = a.start is not None
    b_lines = b.start is not None
    if a_lines and b_lines:
        return _ranges_overlap(a, b)
    a_sec = a.section is not None
    b_sec = b.section is not None
    if a_sec and b_sec:
        return section_is_prefix(a.section, b.section) or section_is_prefix(
            b.section, a.section
        )
    # mixed line-vs-section (neither whole-file) ⇒ never overlap
    return False


def claim_fingerprint(claim: str | None) -> frozenset[str]:
    """The keyword-core fingerprint of a claim (FR-1.2): lowercase, punctuation →
    spaces, tokenize on whitespace, drop the versioned stopword list, no
    stemming; the fingerprint is the sorted set of unique tokens."""
    if not claim:
        return frozenset()
    lowered = claim.lower()
    spaced = _PUNCT_RE.sub(" ", lowered)
    tokens = spaced.split()
    return frozenset(tok for tok in tokens if tok and tok not in STOPWORDS)


def fingerprints_share_core(
    a: frozenset[str], b: frozenset[str], threshold: float = DEFAULT_JACCARD_THRESHOLD
) -> bool:
    """True iff the Jaccard overlap of two claim fingerprints ≥ ``threshold``.

    An empty fingerprint (a contentless / all-stopword claim) shares no core with
    anything — return False so dedup keeps the findings distinct (fail toward
    keeping)."""
    if not a or not b:
        return False
    inter = len(a & b)
    union = len(a | b)
    return union > 0 and (inter / union) >= threshold


def _severity_rank(finding: dict[str, Any]) -> int:
    return SEVERITY_ORDER.get(finding.get("severity", ""), _SEVERITY_FALLBACK)


def merge_compatible(
    a: dict[str, Any], b: dict[str, Any], *, threshold: float
) -> bool:
    """Full merge predicate: same file + overlapping location + same category +
    compatible claim fingerprint (all four required, §6)."""
    la = parse_location(a.get("location"))
    lb = parse_location(b.get("location"))
    if la.file is None or lb.file is None or la.file != lb.file:
        return False
    if not locations_overlap(la, lb):
        return False
    if a.get("category") != b.get("category"):
        return False
    return fingerprints_share_core(
        claim_fingerprint(a.get("claim")),
        claim_fingerprint(b.get("claim")),
        threshold,
    )


def member_key(finding: dict[str, Any]) -> str:
    """The panel-member identity that raised ``finding``: ``profile::lens``.

    A panel member is a (profile, lens) pair, not a profile — the same profile is
    a valid panel entry under two different lenses (``_panel`` builds it,
    ``PanelMember.metric_key`` keys the yield metrics on it). ``source`` alone
    therefore does NOT identify a member, and aggregating merge provenance by
    profile collapses two distinct members into one (PR #59 review F-005). This
    mirrors ``cycle.PanelMember.metric_key`` exactly; the two must agree or the
    yield metrics silently attribute to a member key that never existed."""
    return f"{finding.get('source')}::{finding.get('lens') or 'nolens'}"


@dataclass
class MergeResult:
    """Merged panel findings.

    ``findings`` is the full persisted set (primaries + marked duplicates, each
    carrying ``source``/``lens``; primaries carry ``sources``, duplicates carry
    ``duplicate_of``). ``primaries`` is the subset that reaches triage.
    ``owner_of`` maps every input finding id → the id of the primary its group
    resolved to (a primary maps to itself), for per-member yield attribution.
    """

    findings: list[dict[str, Any]]
    primaries: list[dict[str, Any]]
    duplicates: list[dict[str, Any]]
    owner_of: dict[str, str]


def merge_findings(
    stamped: list[dict[str, Any]],
    *,
    panel_order: dict[str, int],
    threshold: float = DEFAULT_JACCARD_THRESHOLD,
) -> MergeResult:
    """Deterministically merge stamped panel findings (§6).

    ``stamped`` findings must already carry a unique ``id``, a ``source``
    (profile), and a ``lens``; ``panel_order`` maps a profile → its panel index
    for the severity tie-break. Grouping is greedy **complete-linkage**: a
    finding joins the first group it is compatible with **every** member of,
    else starts its own, iterated in canonical order (panel index, then the
    finding's original position). Complete linkage — not single linkage — because
    both legs of ``merge_compatible`` are non-transitive: A can overlap B and B
    overlap C while A and C are disjoint (chained line ranges), and claim
    fingerprints can chain the same way. Under single linkage that closure puts
    A, B and C in one group, and C is then marked ``duplicate_of`` a primary it
    is NOT compatible with — C never reaches triage and its distinct claim is
    silently lost (PR #59 review F-004). Complete linkage makes every group a
    clique, so a duplicate is always compatible with the primary that subsumes
    it. It can only ever split groups further, never merge more: dedup fails
    toward keeping findings (FR-1.2, §4.2 — "an over-merge is a lost defect").
    The primary of a group is the highest-severity member,
    ties broken by (1) panel index then (2) lexicographic id. The primary keeps
    its own phrasing and records all group profiles in ``sources``; every
    non-primary carries ``duplicate_of: <primary id>``. Output order is
    deterministic: groups by (primary panel index, primary id); within a group
    the primary first, then its duplicates by (panel index, id).
    """

    def canonical_key(f: dict[str, Any]) -> tuple[int, str]:
        return (panel_order.get(f.get("source", ""), len(panel_order)), str(f.get("id")))

    ordered = sorted(enumerate(stamped), key=lambda p: (canonical_key(p[1]), p[0]))
    groups: list[list[dict[str, Any]]] = []
    for _, finding in ordered:
        placed = False
        for group in groups:
            if all(merge_compatible(finding, member, threshold=threshold) for member in group):
                group.append(finding)
                placed = True
                break
        if not placed:
            groups.append([finding])

    def primary_key(f: dict[str, Any]) -> tuple[int, int, str]:
        return (
            _severity_rank(f),
            panel_order.get(f.get("source", ""), len(panel_order)),
            str(f.get("id")),
        )

    result_findings: list[dict[str, Any]] = []
    primaries: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    owner_of: dict[str, str] = {}
    resolved_groups: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for group in groups:
        members = sorted(group, key=primary_key)
        primary = members[0]
        others = members[1:]
        # sources: every group PROFILE, unique, in panel order (FR-1.2 — the
        # schema defines this field as profiles, and FR-1.2's `source` IS the
        # profile, so this stays profile-keyed).
        # source_members: every group MEMBER (profile::lens), unique, in the same
        # order. Distinct from `sources` because one profile may sit on the panel
        # under two lenses: both members raising the same finding collapse to a
        # single `sources` entry, which then reads as sole-source coverage when it
        # is really shared (PR #59 review F-005). Member identity is what the
        # FR-1.3 yield metrics and the §1.3 kill criterion actually mean.
        seen: set[str] = set()
        sources: list[str] = []
        seen_members: set[str] = set()
        source_members: list[str] = []
        for m in sorted(group, key=lambda f: (panel_order.get(f.get("source", ""), len(panel_order)), str(f.get("id")))):
            src = m.get("source")
            if src is not None and src not in seen:
                seen.add(src)
                sources.append(src)
            key = member_key(m)
            if m.get("source") is not None and key not in seen_members:
                seen_members.add(key)
                source_members.append(key)
        primary_out = {**primary, "sources": sources, "source_members": source_members}
        primary_out.pop("duplicate_of", None)
        for m in group:
            owner_of[str(m.get("id"))] = str(primary.get("id"))
        dup_out = [
            {**m, "duplicate_of": str(primary.get("id"))}
            for m in sorted(
                others,
                key=lambda f: (
                    panel_order.get(f.get("source", ""), len(panel_order)),
                    str(f.get("id")),
                ),
            )
        ]
        resolved_groups.append((primary_out, dup_out))

    resolved_groups.sort(
        key=lambda g: (
            panel_order.get(g[0].get("source", ""), len(panel_order)),
            str(g[0].get("id")),
        )
    )
    for primary_out, dup_out in resolved_groups:
        primaries.append(primary_out)
        result_findings.append(primary_out)
        duplicates.extend(dup_out)
        result_findings.extend(dup_out)

    return MergeResult(
        findings=result_findings,
        primaries=primaries,
        duplicates=duplicates,
        owner_of=owner_of,
    )
