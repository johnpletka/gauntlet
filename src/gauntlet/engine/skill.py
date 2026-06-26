"""PRD-authoring Claude Code skill: template rendering, provenance, schema.

The ``gauntlet-prd-author`` skill (PRD: PRD-authoring aids, P1) is a committable
*thin pointer* installed at ``.claude/skills/gauntlet-prd-author/SKILL.md`` by
``gauntlet init``. It routes a Claude session that expresses PRD-authoring intent
to the playbook at ``<asset_root>/prompts/prd-author.md`` (a **repository-relative**
reference, never an absolute path — FR-1.3) and states the authoring conventions.
The skill copies none of the playbook's prose (FR-1.3a); it points at it (G3).

This module owns the deterministic machinery the installer (``engine/init.py``)
and later ``doctor`` (P3) build on:

* **Rendering** — the shipped template carries a ``{{PLAYBOOK_PATH}}`` placeholder;
  :func:`render_skill` substitutes the repository-relative playbook reference for
  the repo's ``asset_root`` so the committed file works after a clone/copy to any
  absolute location (FR-1.3).
* **Provenance / refresh classification (§4.5, review F-004, F-001)** — recognition
  is a *version-keyed re-render-and-compare*, not a flat checksum list, because the
  rendered playbook path is configuration-dependent. An installed file claiming
  ``x-gauntlet-template-version: N`` is recognized as generated when it equals
  version N's template rendered under *any* ``asset_root``; recognition is
  deliberately independent of *this* repo's current ``asset_root`` so a file
  generated under a prior ``asset_root`` stays an (unmodified, refreshable)
  generated file rather than being frozen as a customization (review F-001). Match
  → refreshable to the current configuration; any mismatch / missing provenance /
  unknown version → a customization (never clobbered). This fails safe toward
  never-clobber.
* **Normative frontmatter schema (§6, OQ-2)** — :data:`SKILL_FRONTMATTER_SCHEMA`
  pins the required field set the format check (FR-1.5) and ``doctor`` validate
  against; it is mirrored byte-for-byte by the committed ``schemas/skill-frontmatter.json``
  (drift-guarded in the test suite). OQ-2 is empirical on the pinned Claude Code
  version, so P1 halts for human ratification of this schema before P2/P3 depend
  on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import jsonschema
import yaml

# The scaffold tree this package ships (mirrors engine/init.SCAFFOLD_DIR).
SCAFFOLD_DIR = Path(__file__).resolve().parent.parent / "scaffold"
SKILLS_SCAFFOLD = SCAFFOLD_DIR / "skills"

# The skill's stable kebab id and its install location (project-level, committable
# — §2.2: never a user-level ~/.claude/skills global, so it travels with the repo).
SKILL_NAME = "gauntlet-prd-author"
SKILL_REL = f".claude/skills/{SKILL_NAME}/SKILL.md"

# The current generated-template version (§4.5). Bumping this is the trigger to
# first COPY the outgoing template into ``_versions/<old>/`` (see the registry
# note below), so every version Gauntlet has ever generated stays recognizable.
CURRENT_TEMPLATE_VERSION = 1

# The placeholder the shipped template carries for the repo-relative playbook
# reference; rendered per ``asset_root`` at install time (FR-1.3).
PLAYBOOK_PLACEHOLDER = "{{PLAYBOOK_PATH}}"

# The playbook the skill points at, relative to ``asset_root``.
PLAYBOOK_REL = "prompts/prd-author.md"

# Documented natural-language trigger phrases the description must carry (FR-1.2).
# Presence proves *discovery*; the recorded FR-1.6 integration test proves the
# skill actually *triggers* on the pinned Claude Code version.
TRIGGER_PHRASES = (
    "write a PRD",
    "draft a PRD",
    "author a PRD",
    "plan a PRD",
    "start a Gauntlet run",
)


# Normative SKILL.md frontmatter schema (§6 / OQ-2). The single shape the format
# pin (FR-1.5) and doctor (P3) validate against. Mirrored by the committed
# ``schemas/skill-frontmatter.json`` (drift-guarded). ``additionalProperties`` is
# permissive so a future Claude Code frontmatter field does not fail an otherwise
# well-formed skill (the skill gates nothing — FR-1.5 is warn-only).
SKILL_FRONTMATTER_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "gauntlet/schemas/skill-frontmatter.json",
    "title": "gauntlet-prd-author SKILL.md frontmatter (PRD-authoring aids §6, OQ-2)",
    "description": (
        "Normative frontmatter shape for the committable PRD-authoring skill, "
        "pinned against the named Claude Code version recorded in BOOTSTRAP-NOTES "
        "/ .gauntlet/pins.yaml. `name` and `description` are the Claude Code skill "
        "fields (the description carries the documented trigger phrases, FR-1.2); "
        "`x-gauntlet-generated` / `x-gauntlet-template-version` are Gauntlet "
        "provenance (§4.5). Extra keys are permitted so a future Claude Code field "
        "does not fail an otherwise valid skill (FR-1.5 is warn-only)."
    ),
    "type": "object",
    "additionalProperties": True,
    "required": [
        "name",
        "description",
        "x-gauntlet-generated",
        "x-gauntlet-template-version",
    ],
    "properties": {
        "name": {
            "type": "string",
            "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$",
            "description": "Kebab-case skill id (e.g. gauntlet-prd-author).",
        },
        "description": {
            "type": "string",
            "minLength": 1,
            "description": "Trigger sentence; carries the FR-1.2 NL trigger phrases.",
        },
        "x-gauntlet-generated": {
            "const": True,
            "description": "Provenance marker: this file was generated by gauntlet init.",
        },
        "x-gauntlet-template-version": {
            "type": "integer",
            "minimum": 1,
            "description": "Integer template version keyed into the version registry (§4.5).",
        },
    },
}


def _playbook_ref(playbook_rel: str, asset_root: str) -> str:
    """The repository-relative reference to ``playbook_rel`` for ``asset_root``.

    The skill-agnostic core of :func:`playbook_ref` (FR-1.3 / FR-7.1): every skill
    spec resolves its own playbook this way. ``"."`` → ``<playbook_rel>``;
    ``".gauntlet"`` → ``.gauntlet/<playbook_rel>``. Always repo-relative — never an
    absolute filesystem path — so the committed skill keeps resolving after the
    repo is cloned or copied elsewhere. Normalises spelling the same way
    ``config._validate_repo_relative`` does, so the rendered path and the engine's
    own asset resolution agree.
    """
    parts = [p for p in (asset_root or ".").split("/") if p not in ("", ".")]
    return "/".join(parts + playbook_rel.split("/"))


def playbook_ref(asset_root: str) -> str:
    """The repository-relative ``prd-author`` playbook reference (FR-1.3).

    Back-compat shim bound to the prd-author skill; the generalized core is
    :func:`_playbook_ref`, used per-spec by the registry (FR-7.1).
    """
    return _playbook_ref(PLAYBOOK_REL, asset_root)


def render_skill(template_text: str, asset_root: str) -> str:
    """Render the prd-author skill template (FR-1.3). See :meth:`SkillSpec.render`."""
    return template_text.replace(PLAYBOOK_PLACEHOLDER, playbook_ref(asset_root))


def current_template_path() -> Path:
    """The current generated-template source (PRD §4.1 named path)."""
    return SKILLS_SCAFFOLD / SKILL_NAME / "SKILL.md"


def version_template_path(version: object) -> Path | None:
    """Template path for a recognized version, or ``None`` if unknown (§4.5).

    The *current* version lives at the PRD-named path; superseded versions live
    under ``_versions/<N>/SKILL.md`` (append-only — never retired, so any file
    gauntlet has ever generated stays recognizable). An out-of-range or
    non-integer version is unknown → ``None`` → the caller fails safe to
    "customization" (never clobber).
    """
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if version == CURRENT_TEMPLATE_VERSION:
        return current_template_path()
    superseded = SKILLS_SCAFFOLD / "_versions" / str(version) / "SKILL.md"
    return superseded if superseded.is_file() else None


def parse_frontmatter(text: str) -> dict | None:
    """Parse the leading ``---`` YAML frontmatter block, or ``None`` if malformed.

    Returns the mapping, or ``None`` when there is no fenced block, the YAML is
    unparseable, or the document is not a mapping. Callers treat ``None`` as
    "no usable provenance" and fail safe to never-clobber.
    """
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def validate_skill_frontmatter(text: str) -> list[str]:
    """Validate a SKILL.md's frontmatter against the normative schema (FR-1.5).

    Returns a list of human-readable violations (empty == well-formed). A missing
    or unparseable frontmatter block is itself a single violation. Validation is
    pure/offline so the format pin and doctor can run it deterministically.
    """
    meta = parse_frontmatter(text)
    if meta is None:
        return ["SKILL.md has no parseable YAML frontmatter block"]
    validator = jsonschema.Draft7Validator(SKILL_FRONTMATTER_SCHEMA)
    return [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(meta), key=lambda e: list(e.path))
    ]


def _asset_root_of_playbook_ref(playbook_rel: str, ref: str) -> str | None:
    """The ``asset_root`` a rendered playbook ``ref`` came from, or ``None`` if it
    is not a canonical rendering of ``playbook_rel``. ``"prompts/prd-author.md"`` →
    ``"."``; ``".gauntlet/prompts/prd-author.md"`` → ``".gauntlet"``. Used to
    confirm a candidate substitution is a value :func:`_playbook_ref` could
    actually produce, not arbitrary edited text occupying the placeholder slot.
    """
    if ref == playbook_rel:
        return "."
    tail = "/" + playbook_rel
    return ref[: -len(tail)] if ref.endswith(tail) else None


def _is_generated_rendering(text: str, template_text: str, playbook_rel: str) -> bool:
    """True if ``text`` is an unmodified rendering of ``template_text`` under
    *some* ``asset_root`` (review F-001), for a skill whose playbook is
    ``playbook_rel``.

    Recognition is independent of *this* repo's current ``asset_root``: a file
    generated under a previous ``asset_root`` (so it carries the old resolved
    playbook path) is still an unmodified generated file and must stay
    refreshable, not be misclassified as a customization. ``text`` qualifies iff
    it equals ``template_text`` with every ``{{PLAYBOOK_PATH}}`` replaced by one
    consistent value :func:`_playbook_ref` could have produced for ``playbook_rel``.
    """
    segments = template_text.split(PLAYBOOK_PLACEHOLDER)
    if len(segments) == 1:
        return text == template_text  # template carries no placeholder
    prefix, after = segments[0], segments[1]
    if not text.startswith(prefix):
        return False
    rest = text[len(prefix):]
    # The substituted value runs up to the literal segment following the first
    # placeholder (or, if that segment is empty, up to the template's suffix).
    if after:
        idx = rest.find(after)
        if idx == -1:
            return False
        value = rest[:idx]
    else:
        suffix = segments[-1]
        if not rest.endswith(suffix):
            return False
        value = rest[: len(rest) - len(suffix)]
    ar = _asset_root_of_playbook_ref(playbook_rel, value)
    if ar is None or _playbook_ref(playbook_rel, ar) != value:
        return False
    # Full reconstruction confirms the single consistent value across every
    # placeholder occurrence (and that nothing else in the body was touched).
    return text == template_text.replace(PLAYBOOK_PLACEHOLDER, value)


def _classify(text: str, playbook_rel: str, resolve_version: Callable[[object], Path | None]) -> str:
    """Skill-agnostic core of :func:`classify_skill` (FR-7.1).

    ``resolve_version`` maps a recorded ``x-gauntlet-template-version`` to that
    version's template path (or ``None`` when unknown), so the prd-author shim can
    route through the monkeypatchable module-level :func:`version_template_path`
    while the registry uses each spec's own resolver.
    """
    meta = parse_frontmatter(text)
    if not meta or meta.get("x-gauntlet-generated") is not True:
        return "customization"
    tmpl_path = resolve_version(meta.get("x-gauntlet-template-version"))
    if tmpl_path is None:
        return "customization"
    return "generated" if _is_generated_rendering(text, tmpl_path.read_text(), playbook_rel) else "customization"


def classify_skill(text: str) -> str:
    """Classify an installed prd-author SKILL.md as ``"generated"``/``"customization"``.

    Version-keyed re-render-and-compare (§4.5, review F-004), with recognition
    *independent of the current ``asset_root``* (review F-001): a file claiming
    ``x-gauntlet-generated: true`` and a *known* ``x-gauntlet-template-version``
    is an unmodified generated file when it equals that version's template
    rendered under *any* ``asset_root`` — so a file generated under a prior
    ``asset_root`` stays refreshable rather than being frozen as a customization.
    Any mismatch, missing provenance, or unknown version → a customization
    (never clobbered) — failing safe toward never-clobber. The skill-agnostic
    core is :func:`_classify` (FR-7.1); this shim binds it to the prd-author skill.
    """
    return _classify(text, PLAYBOOK_REL, version_template_path)


def _skill_looks_stale(text: str, rendered_ref: str) -> bool:
    """Skill-agnostic core of :func:`skill_looks_stale` (FR-7.1).

    True when ``text`` claims ``x-gauntlet-generated: true`` but the
    backtick-delimited ``rendered_ref`` the current ``asset_root`` would produce
    is absent from it.
    """
    meta = parse_frontmatter(text)
    if not meta or meta.get("x-gauntlet-generated") is not True:
        return False
    return f"`{rendered_ref}`" not in text


def skill_looks_stale(text: str, asset_root: str) -> bool:
    """True if a *customized* prd-author skill carries provenance but a drifted path.

    Only meaningful for a file that still claims ``x-gauntlet-generated: true``
    (a hand-authored skill with no provenance makes no claim that could go stale).
    The signal (§4.5): the backtick-delimited playbook reference the current
    ``asset_root`` would render is absent from the file — i.e. it points at a path
    that no longer matches this repo's layout. The reference is checked
    *backtick-delimited* (the form the template renders it in) so that a shorter
    repo-relative path is not spuriously found as a suffix of a longer one
    (``prompts/prd-author.md`` is a suffix of ``.gauntlet/prompts/prd-author.md``).
    Used to *warn* (never modify) on re-run. The skill-agnostic core is
    :func:`_skill_looks_stale` (FR-7.1).
    """
    return _skill_looks_stale(text, playbook_ref(asset_root))


# ---- skill registry (FR-7.1) ------------------------------------------------
# A small list of skill specs generalizes the single-skill constants above so
# `init` installs and `doctor` validates every committable skill with the same
# machinery. The prd-author rendering/provenance/refresh behavior is preserved
# byte-for-byte (FR-7.2): its spec routes template/version resolution through the
# monkeypatchable module-level functions, and the generalized `_classify` /
# `_skill_looks_stale` / `_is_generated_rendering` cores are exactly the prd-author
# logic, parameterized by playbook path.


@dataclass(frozen=True)
class SkillSpec:
    """One committable skill: its id, playbook, template version, and triggers.

    The rendering, provenance classification, refresh, and stale-warning logic is
    the same skill-agnostic core for every spec (FR-7.1/FR-7.3); only this data
    differs. The prd-author spec deliberately routes :meth:`template_path` /
    :meth:`resolve_version_template` through the module-level functions so the
    §4.5 refresh tests that monkeypatch them keep passing unchanged (FR-7.2).
    """

    name: str
    playbook_rel: str
    template_version: int
    trigger_phrases: tuple[str, ...]

    @property
    def skill_rel(self) -> str:
        """Project-level install path (committable; never a user-global)."""
        return f".claude/skills/{self.name}/SKILL.md"

    def template_path(self) -> Path:
        """The current generated-template source for this skill (PRD §4.1)."""
        if self.name == SKILL_NAME:
            # Route prd-author through the module-level function so its existing
            # §4.5 refresh tests (which monkeypatch it) are preserved (FR-7.2).
            return current_template_path()
        return SKILLS_SCAFFOLD / self.name / "SKILL.md"

    def resolve_version_template(self, version: object) -> Path | None:
        """Template path for a recognized version of this skill, else ``None``.

        The current version lives at the named path; superseded versions live
        append-only under ``_versions/<name>/<N>/SKILL.md`` (§4.5). prd-author
        keeps its historical ``_versions/<N>/`` layout via the module-level
        function so nothing about its recognition changes (FR-7.2).
        """
        if self.name == SKILL_NAME:
            return version_template_path(version)
        if not isinstance(version, int) or isinstance(version, bool):
            return None
        if version == self.template_version:
            return self.template_path()
        superseded = SKILLS_SCAFFOLD / "_versions" / self.name / str(version) / "SKILL.md"
        return superseded if superseded.is_file() else None

    def playbook_ref(self, asset_root: str) -> str:
        """This skill's repository-relative playbook reference (FR-1.3)."""
        return _playbook_ref(self.playbook_rel, asset_root)

    def render(self, template_text: str, asset_root: str) -> str:
        """Render this skill's template by substituting the playbook reference."""
        return template_text.replace(PLAYBOOK_PLACEHOLDER, self.playbook_ref(asset_root))

    def classify(self, text: str) -> str:
        """Classify an installed copy as ``"generated"``/``"customization"`` (§4.5)."""
        return _classify(text, self.playbook_rel, self.resolve_version_template)

    def looks_stale(self, text: str, asset_root: str) -> bool:
        """True if a customized copy carries provenance but a drifted playbook path."""
        return _skill_looks_stale(text, self.playbook_ref(asset_root))


# The seven documented operator trigger phrases — the finite, closed normative
# corpus (FR-6.2) that fixes the FR-6.6 acceptance denominator. Order is the
# contract: the SKILL.md description enumerates them verbatim and in this order.
OPERATOR_SKILL_NAME = "gauntlet-operator"
OPERATOR_PLAYBOOK_REL = "prompts/operator.md"
OPERATOR_TEMPLATE_VERSION = 1
OPERATOR_TRIGGER_PHRASES = (
    "check the gauntlet run",
    "is the run stuck",
    "is the run parked",
    "approve the gate",
    "reject the gate",
    "why did the step fail",
    "recover the run",
)

PRD_AUTHOR_SPEC = SkillSpec(
    name=SKILL_NAME,
    playbook_rel=PLAYBOOK_REL,
    template_version=CURRENT_TEMPLATE_VERSION,
    trigger_phrases=TRIGGER_PHRASES,
)
OPERATOR_SPEC = SkillSpec(
    name=OPERATOR_SKILL_NAME,
    playbook_rel=OPERATOR_PLAYBOOK_REL,
    template_version=OPERATOR_TEMPLATE_VERSION,
    trigger_phrases=OPERATOR_TRIGGER_PHRASES,
)

# Every committable skill `init` installs and `doctor` validates (FR-7.1).
SKILL_REGISTRY: tuple[SkillSpec, ...] = (PRD_AUTHOR_SPEC, OPERATOR_SPEC)
