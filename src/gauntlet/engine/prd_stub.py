"""Structured PRD stub: single committable template, manifest parser, fail-closed gate.

PRD-authoring aids, P2. Replaces the inline ``_PRD_STUB`` literal with one
committable template (``scaffold/prd-stub.md``) installed to
``<asset_root>/prd-stub.md``. Both ``gauntlet new`` and ``check_entry_contract``
resolve the stub *identically* (§4.3) and validate it against the §4.4 template
invariants before use, failing closed so a malformed customization of this
gate-input template cannot silently weaken the FR-10.1 human-author gate (FR-3.3).

The §6 mandatory-section manifest is **parsed from the playbook**
(``prompts/prd-author.md``), never hand-restated, so a change to the playbook's
section catalogue changes the manifest — which drives both the required-header
invariant (§4.4) and the drift guard (FR-2.2).

**Single source per repo.** ``new`` (which writes the scaffold) and
``check_entry_contract`` (which decides whether the scaffold is "still a stub")
read the *same* resolved file via :func:`resolve_stub_template`, so they can
never disagree about what an unfilled stub is. The packaged scaffold is always
present, so the stub source is always resolvable (a missing repo copy is not an
error — it falls back to the package).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# The scaffold tree this package ships (mirrors engine/init.SCAFFOLD_DIR).
SCAFFOLD_DIR = Path(__file__).resolve().parent.parent / "scaffold"

# FR-10.1 stub marker. Defined here — the single stub source and its gate share
# one definition — and re-exported by ``engine/run.py`` for backward-compat.
PRD_STUB_MARKER = "<!-- GAUNTLET-PRD-STUB: replace this file with a real PRD -->"

# The committable stub template's filename under ``asset_root`` (§4.3), and the
# playbook the manifest is parsed from (also under ``asset_root``).
STUB_REL = "prd-stub.md"
PLAYBOOK_REL = "prompts/prd-author.md"

# §4.5 provenance for the *generated* stub. A PRD carries no YAML frontmatter (the
# skill's provenance vehicle), so the stub's provenance rides in an HTML comment.
# ``CURRENT_STUB_VERSION`` is the current generated-template version; bumping it
# first COPIES the outgoing template into the append-only version registry under
# ``scaffold/_stub-versions/<N>/`` so every stub gauntlet has ever generated stays
# recognizable (mirrors :mod:`gauntlet.engine.skill`'s version registry).
CURRENT_STUB_VERSION = 1
_STUB_PROVENANCE_RE = re.compile(
    r"<!--\s*gauntlet-generated:\s*true;\s*gauntlet-template-version:\s*(?P<version>\d+)\s*-->"
)

# Manifest entry classes (§6).
MANDATORY = "mandatory"
SCALE = "scale-with-size"

# The synthetic ``header-block`` entry (§6): it is the playbook's ``**Header
# block**`` bold entry, but — unlike a normal section — it is *not* a Markdown
# heading. Its §4.4 validation is satisfied by the presence of its metadata
# labels (review F-006), not by a heading line. Its parsed/normalized name:
HEADER_BLOCK = "header block"
# The metadata labels that satisfy (and define) the header-block invariant (§6).
HEADER_BLOCK_LABELS = ("Status", "Author")


class StubTemplateError(RuntimeError):
    """A resolved stub template violates a §4.4 invariant (fail closed, FR-3.3).

    Raised by both ``gauntlet new`` and ``check_entry_contract`` before they use
    a resolved stub, so a broken customization of this gate-input template is
    treated as "cannot prove this is human-authored", never as "authored".
    """


@dataclass(frozen=True)
class ManifestEntry:
    """One parsed §6 section catalogue entry: a normalized name and its class."""

    name: str  # normalized (lowercased, whitespace-collapsed) section name
    cls: str   # MANDATORY | SCALE


def _norm(text: str) -> str:
    """Canonical form for comparing section names/headings: collapse + lowercase."""
    return re.sub(r"\s+", " ", text).strip().lower()


# --- §6 machine-readable manifest parser -------------------------------------
#
# Representation (review F-001): the playbook's classified section entries are
# NOT ATX headings — ``## 0.``/``## 1.``/… are the playbook's own document
# structure and carry no class marker, while the catalogue of PRD sections lives
# in §2 as **bold-paragraph entries** of the form ``**<name>** *(<class …>)*``.
# The parser targets that existing syntax.
#
# Class rule (deterministic): an entry is MANDATORY iff the first word inside the
# parenthetical marker is exactly ``mandatory`` (so §11's "mandatory if any
# exist" → mandatory); every other allowed marker → SCALE. This binary rule
# reproduces the PRD §6 prose manifest exactly, including §6/§7 whose playbook
# marker reads "*(present whenever …)*" but which §6's prose lists as
# scale-with-size. The marker may wrap to the next line (§6/§7 do), so we only
# need its leading word.
_ENTRY_RE = re.compile(r"^\*\*(?P<name>.+?)\*\*\s+\*\((?P<marker>[a-z][a-z-]*)")

# A line that *opens* a top-level bold paragraph — i.e. a catalogue-entry
# candidate. Within §2 every such line is a classified entry, so one that does
# not also satisfy :data:`_ENTRY_RE` (a missing/garbled ``*(<class>)*`` marker)
# is malformed and must fail closed rather than be silently skipped (review
# F-002 — a marker typo would otherwise demote a mandatory section).
_ENTRY_CANDIDATE_RE = re.compile(r"^\*\*.+?\*\*")

# The only leading marker words §6 defines. Any other leading word is ambiguous
# and cannot be classified deterministically → fail closed (review F-002).
_ALLOWED_MARKERS = frozenset({MANDATORY, SCALE, "present"})


def _structure_section(playbook_text: str) -> str:
    """The playbook's §2 'PRD structure' section text (catalogue entries live
    only here, so scoping to it keeps unrelated bold paragraphs out)."""
    lines = playbook_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^##\s+2\.", ln)), None)
    if start is None:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^##\s+3\.", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def parse_manifest(playbook_text: str) -> list[ManifestEntry]:
    """Extract the ordered ``(name, class)`` manifest from the playbook (§6).

    Returns the catalogue entries in document order. Driven entirely off the
    parsed playbook, so adding, renaming, or removing a §2 bold-paragraph entry
    changes the manifest (the property the drift guard, FR-2.2, relies on).

    Fails closed (review F-002): a missing §2 section, a bold-paragraph entry with
    no recognizable ``*(<class>)*`` marker, an unrecognized marker word, a
    duplicate entry, an empty manifest, or a manifest missing the required
    header-block anchor all raise :class:`StubTemplateError` rather than silently
    producing an empty or weakened manifest that would let the gate fail open.
    """
    section = _structure_section(playbook_text)
    if not section.strip():
        raise StubTemplateError(
            "playbook §2 'PRD structure' section not found; cannot derive the "
            "§6 mandatory-section manifest (fail closed, FR-3.3)"
        )

    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for raw in section.splitlines():
        line = raw.strip()
        if not _ENTRY_CANDIDATE_RE.match(line):
            continue  # not a top-level bold catalogue entry
        m = _ENTRY_RE.match(line)
        if not m:
            raise StubTemplateError(
                f"playbook §2 catalogue entry has no recognizable *(<class>)* "
                f"marker: {line!r}"
            )
        marker = m.group("marker")
        if marker not in _ALLOWED_MARKERS:
            raise StubTemplateError(
                f"playbook §2 entry has an unrecognized class marker {marker!r} "
                f"(allowed: {sorted(_ALLOWED_MARKERS)}); refusing to guess its "
                f"class: {line!r}"
            )
        name = _norm(m.group("name"))
        if name in seen:
            raise StubTemplateError(
                f"playbook §2 has a duplicate catalogue entry {name!r}; the §6 "
                "manifest must be unambiguous"
            )
        seen.add(name)
        cls = MANDATORY if marker == MANDATORY else SCALE
        entries.append(ManifestEntry(name=name, cls=cls))

    if not entries:
        raise StubTemplateError(
            "playbook §2 yielded no catalogue entries; the §6 manifest cannot be "
            "empty (fail closed, FR-3.3)"
        )
    header_block = next((e for e in entries if e.name == HEADER_BLOCK), None)
    if header_block is None:
        raise StubTemplateError(
            f"playbook §2 manifest is missing the required {HEADER_BLOCK!r} entry "
            "(the header-block invariant anchor)"
        )
    if header_block.cls != MANDATORY:
        # validate_template runs the §4.4 metadata checks (Status/Author) only for
        # MANDATORY entries, so a header-block parsed as scale-with-size would let
        # a stub omit its required metadata — the FR-3.3 fail-open this guards
        # against (review F-002). The header-block invariant is mandatory by
        # definition; refuse any playbook that demotes it.
        raise StubTemplateError(
            f"playbook §2 classifies the {HEADER_BLOCK!r} entry as "
            f"{header_block.cls!r}; it must be mandatory (its metadata validation "
            "runs only for a mandatory entry)"
        )
    return entries


# --- stub structure extraction (drift guard + §4.4 header invariant) ---------

def _h2_headings(text: str) -> list[str]:
    """Normalized text of the stub's level-2 (``##``) headings, in order.

    The stub's section skeleton is written at ``##``; the ``# PRD: <title>`` H1
    and any ``###`` subsections are intentionally excluded so the manifest (which
    is level-agnostic catalogue entries) maps cleanly onto the stub's sections.
    """
    out: list[str] = []
    for line in text.splitlines():
        if re.match(r"^##\s", line):  # H2 only — '### ' fails (no \s after '##')
            out.append(_norm(re.sub(r"^#+\s*", "", line)))
    return out


def _label_count(text: str, label: str) -> int:
    """Number of lines that are a ``<label>:`` metadata line (bold optional)."""
    return len(re.findall(rf"(?mi)^\s*\*{{0,2}}{re.escape(label)}\*{{0,2}}\s*:", text))


def stub_section_names(text: str) -> list[str]:
    """The stub's section structure as an ordered name list, for the drift guard.

    The header-block is not a heading, so it is represented by its synthetic
    :data:`HEADER_BLOCK` name (prepended when the metadata labels are present),
    matching the manifest's first entry; every ``##`` section follows in order.
    A drift test asserts this equals ``[e.name for e in parse_manifest(...)]``.
    """
    names: list[str] = []
    if all(_label_count(text, label) >= 1 for label in HEADER_BLOCK_LABELS):
        names.append(HEADER_BLOCK)
    names.extend(_h2_headings(text))
    return names


# --- §4.4 template-invariant validation (fail closed) ------------------------

def validate_template(
    text: str, manifest: list[ManifestEntry], *, source: object = "<stub>"
) -> None:
    """Validate a resolved stub template against the §4.4 invariants (FR-3.3).

    Raises :class:`StubTemplateError` naming the violated invariant and the file
    path when: the template is empty after normalization; it does not contain
    **exactly one** FR-10.1 marker; a mandatory manifest section is missing (the
    ``header-block`` entry requires each metadata label exactly once; every other
    mandatory entry requires a matching ``##`` heading). A malformed gate-input
    template must never be usable, so both consumers call this before use.
    """
    src = str(source)
    if not manifest:
        # An empty manifest would enforce no mandatory headers at all — a fail-open
        # gate input. The parser already fails closed on this, but a directly
        # supplied empty manifest is rejected here too (review F-002).
        raise StubTemplateError(
            f"{src}: refusing to validate against an empty §6 manifest "
            "(no mandatory sections could be enforced)"
        )
    if not text.strip():
        raise StubTemplateError(f"{src}: stub template is empty after normalization")

    n_marker = text.count(PRD_STUB_MARKER)
    if n_marker != 1:
        raise StubTemplateError(
            f"{src}: stub template must contain exactly one FR-10.1 marker "
            f"({PRD_STUB_MARKER!r}); found {n_marker}"
        )

    headings = set(_h2_headings(text))
    for entry in manifest:
        if entry.cls != MANDATORY:
            continue
        if entry.name == HEADER_BLOCK:
            for label in HEADER_BLOCK_LABELS:
                count = _label_count(text, label)
                if count != 1:
                    raise StubTemplateError(
                        f"{src}: header-block requires exactly one {label!r} "
                        f"metadata label; found {count}"
                    )
            continue
        if entry.name not in headings:
            raise StubTemplateError(
                f"{src}: stub template is missing mandatory section header "
                f"{entry.name!r} (from the playbook §6 manifest)"
            )


# --- FR-2.4 deterministic authored-content predicate -------------------------

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _authored_normalize(text: str) -> str:
    """Normalize for the authored-content predicate (FR-2.4).

    Drop HTML/Markdown guidance comments (including the marker and the provenance
    comment) and section-heading lines, then collapse whitespace. What remains is
    the substantive body — so editing only comments, headings, or whitespace does
    not change the normalized form.
    """
    text = _COMMENT_RE.sub("", text)
    lines = []
    for ln in text.splitlines():
        if re.match(r"^\s*#", ln):  # ATX heading line
            continue
        collapsed = re.sub(r"\s+", " ", ln).strip()
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines)


def has_authored_content(candidate: str, template: str) -> bool:
    """True iff ``candidate`` is an authored PRD relative to the stub (FR-2.4).

    Passes iff (1) it contains **no** FR-10.1 marker, **and** (2) its normalized
    form (see :func:`_authored_normalize`) is non-empty **and not equal to** the
    template's normalized form. So a whitespace-/comment-/heading-only edit of the
    stub, or a stub with the marker still present, never counts as authored.
    """
    if PRD_STUB_MARKER in candidate:
        return False
    cand = _authored_normalize(candidate)
    return bool(cand) and cand != _authored_normalize(template)


# --- §4.3 lookup precedence: one resolved source per repo --------------------

def stub_rel(asset_root: str) -> str:
    """Repo-relative install path of the stub template for ``asset_root`` (§4.3).

    ``"."`` → ``prd-stub.md`` (Gauntlet's own repo); ``".gauntlet"`` →
    ``.gauntlet/prd-stub.md`` (an adopter). Normalises spelling the way
    :func:`config._validate_repo_relative` does, so the install path and the
    resolution path agree.
    """
    parts = [p for p in (asset_root or ".").split("/") if p not in ("", ".")]
    return "/".join(parts + [STUB_REL])


def packaged_stub_path() -> Path:
    """The packaged stub template (PRD §4.3 package source / fallback)."""
    return SCAFFOLD_DIR / STUB_REL


def resolve_stub_template(repo_root: Path, asset_root: str) -> tuple[str, Path]:
    """Resolve the stub template per §4.3: repo copy if present, else packaged.

    Returns ``(text, source_path)``. The packaged fallback always exists, so the
    stub source is always resolvable; an absent repo copy is not an error.
    """
    repo_copy = repo_root / stub_rel(asset_root)
    if repo_copy.is_file():
        return repo_copy.read_text(), repo_copy
    packaged = packaged_stub_path()
    return packaged.read_text(), packaged


def resolve_playbook_text(repo_root: Path, asset_root: str) -> str:
    """Resolve the playbook the manifest is parsed from: repo copy else packaged.

    Mirrors the stub's precedence so the manifest used to validate a stub comes
    from the same repo the stub was authored against (or the package, byte-for-
    byte the canonical playbook, when the repo carries no copy).
    """
    parts = [p for p in (asset_root or ".").split("/") if p not in ("", ".")]
    repo_copy = repo_root / "/".join(parts + PLAYBOOK_REL.split("/"))
    if repo_copy.is_file():
        return repo_copy.read_text()
    return (SCAFFOLD_DIR / PLAYBOOK_REL).read_text()


def resolve_manifest(repo_root: Path, asset_root: str) -> list[ManifestEntry]:
    """Parse the §6 manifest from the resolved playbook for this repo."""
    return parse_manifest(resolve_playbook_text(repo_root, asset_root))


# --- §4.5 provenance / refresh classification (the stub analogue of skill.py) -

def stub_version_template_path(version: object) -> Path | None:
    """Template path for a recognized stub version, or ``None`` if unknown (§4.5).

    The *current* version lives at the packaged path; superseded versions live
    under ``_stub-versions/<N>/prd-stub.md`` (append-only — never retired, so any
    stub gauntlet has ever generated stays recognizable). An out-of-range or
    non-integer version is unknown → ``None`` → the caller fails safe to
    "customization" (never clobber). Mirrors :func:`skill.version_template_path`.
    """
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if version == CURRENT_STUB_VERSION:
        return packaged_stub_path()
    superseded = SCAFFOLD_DIR / "_stub-versions" / str(version) / STUB_REL
    return superseded if superseded.is_file() else None


def stub_provenance_version(text: str) -> int | None:
    """The template version a stub claims in its provenance comment, or ``None``.

    ``None`` when the file carries no ``gauntlet-generated: true`` provenance
    comment, so a hand-authored stub (or one with the line stripped) makes no
    recognizable provenance claim and is treated as a customization downstream
    (fail safe toward never-clobber).
    """
    m = _STUB_PROVENANCE_RE.search(text)
    return int(m.group("version")) if m else None


def classify_stub(text: str) -> str:
    """Classify an installed stub as ``"generated"`` or ``"customization"`` (§4.5).

    The stub analogue of :func:`skill.classify_skill`, using the same version-keyed
    compare (review F-004): a file whose provenance claims a *known* version and is
    byte-for-byte that version's template is an unmodified generated file
    (refreshable); any byte mismatch, missing provenance, or unknown version is a
    customization (never clobbered) — failing safe toward never-clobber. The stub
    carries no ``asset_root``-dependent rendered path, so "re-render" is the
    identity here and the compare is a plain byte equality.
    """
    version = stub_provenance_version(text)
    if version is None:
        return "customization"
    tmpl_path = stub_version_template_path(version)
    if tmpl_path is None:
        return "customization"
    return "generated" if text == tmpl_path.read_text() else "customization"
