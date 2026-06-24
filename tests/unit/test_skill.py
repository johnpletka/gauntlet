"""PRD-authoring skill: rendering, provenance, schema, no-duplication (P1).

Covers the deterministic, offline machinery of the ``gauntlet-prd-author`` skill
(FR-1.1–FR-1.3a, FR-1.5 schema groundwork, §4.5 provenance). The recorded
natural-language *trigger* test (FR-1.6) is the integration test in
``tests/integration/test_skill_trigger.py``; metadata inspection here proves only
discovery, never trigger.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from gauntlet.engine import skill as S

REPO = Path(__file__).resolve().parents[2]


# ---- rendering / repo-relative reference (FR-1.3) ---------------------------

def test_playbook_ref_is_repo_relative_per_asset_root():
    assert S.playbook_ref(".") == "prompts/prd-author.md"
    assert S.playbook_ref(".gauntlet") == ".gauntlet/prompts/prd-author.md"
    # spelling is normalised the way config._validate_repo_relative does
    assert S.playbook_ref("./.gauntlet") == ".gauntlet/prompts/prd-author.md"


def test_render_substitutes_placeholder_and_leaves_no_absolute_path():
    tmpl = S.current_template_path().read_text()
    assert S.PLAYBOOK_PLACEHOLDER in tmpl  # the shipped template is a placeholder
    rendered = S.render_skill(tmpl, ".gauntlet")
    assert S.PLAYBOOK_PLACEHOLDER not in rendered
    assert ".gauntlet/prompts/prd-author.md" in rendered
    # FR-1.3: the playbook reference is repo-relative, never an absolute
    # source-machine path that would break after a clone/copy elsewhere.
    assert not S.playbook_ref(".gauntlet").startswith("/")
    for absolute in ("/Users/", "/home/", "/private/", "/tmp/", "C:\\"):
        assert absolute not in rendered


# ---- frontmatter parsing + normative schema (FR-1.2, FR-1.5 groundwork) -----

def test_template_frontmatter_validates_and_carries_trigger_phrases():
    tmpl = S.current_template_path().read_text()
    assert S.validate_skill_frontmatter(tmpl) == []
    meta = S.parse_frontmatter(tmpl)
    assert meta["name"] == S.SKILL_NAME
    # FR-1.2: the description carries the documented trigger phrases (discovery,
    # not trigger — the latter is FR-1.6's recorded integration test).
    desc = meta["description"].lower()
    for phrase in S.TRIGGER_PHRASES:
        assert phrase.lower() in desc, phrase


def test_validate_rejects_missing_and_malformed_frontmatter():
    assert S.validate_skill_frontmatter("no frontmatter here") != []
    # missing the provenance fields
    bad = "---\nname: x\ndescription: y\n---\nbody\n"
    violations = S.validate_skill_frontmatter(bad)
    assert any("x-gauntlet-generated" in v for v in violations)
    # non-kebab name
    bad_name = (
        "---\nname: Not Kebab\ndescription: y\n"
        "x-gauntlet-generated: true\nx-gauntlet-template-version: 1\n---\n"
    )
    assert S.validate_skill_frontmatter(bad_name) != []


def test_parse_frontmatter_none_on_unfenced_or_nonmapping():
    assert S.parse_frontmatter("plain text") is None
    assert S.parse_frontmatter("---\n- a\n- b\n---\n") is None  # a list, not a map


# ---- provenance classification (§4.5, review F-004) -------------------------

def test_rendered_template_classifies_as_generated_for_its_asset_root():
    tmpl = S.current_template_path().read_text()
    assert S.classify_skill(S.render_skill(tmpl, "."), ".") == "generated"
    assert S.classify_skill(S.render_skill(tmpl, ".gauntlet"), ".gauntlet") == "generated"


def test_edited_or_provenance_stripped_skill_is_a_customization():
    tmpl = S.current_template_path().read_text()
    rendered = S.render_skill(tmpl, ".")
    # an edited body no longer byte-matches the re-render → customization
    assert S.classify_skill(rendered + "\nmy custom note\n", ".") == "customization"
    # provenance stripped → customization (fail safe to never-clobber)
    no_prov = rendered.replace("x-gauntlet-generated: true\n", "")
    assert S.classify_skill(no_prov, ".") == "customization"


def test_unknown_version_is_a_customization():
    tmpl = S.current_template_path().read_text()
    bumped = S.render_skill(tmpl, ".").replace(
        "x-gauntlet-template-version: 1", "x-gauntlet-template-version: 999"
    )
    assert S.version_template_path(999) is None
    assert S.classify_skill(bumped, ".") == "customization"


def test_generated_file_for_other_asset_root_is_a_customization_here():
    # A skill generated for an adopter (.gauntlet) is NOT an unmodified generated
    # file when re-rendered under asset_root "." — the playbook path differs.
    tmpl = S.current_template_path().read_text()
    adopter = S.render_skill(tmpl, ".gauntlet")
    assert S.classify_skill(adopter, ".") == "customization"


def test_skill_looks_stale_only_for_provenance_bearing_drift():
    tmpl = S.current_template_path().read_text()
    adopter = S.render_skill(tmpl, ".gauntlet")
    # carries provenance, but its playbook ref does not match asset_root "."
    assert S.skill_looks_stale(adopter, ".") is True
    # matches its own asset_root → not stale
    assert S.skill_looks_stale(adopter, ".gauntlet") is False
    # a hand-authored skill with no provenance makes no stale-able claim
    assert S.skill_looks_stale("---\nname: x\ndescription: y\n---\n", ".") is False


# ---- FR-1.3a: bounded no-prose-duplication ----------------------------------

def _normalize_words(text: str) -> list[str]:
    """Lowercased word stream excluding frontmatter, ATX headings, and the
    exempt shared-vocab tokens (command names, path components) per FR-1.3a."""
    exempt = {
        "gauntlet", "new", "run", "slug", "prd", "md", "prompts", "runs",
        "author", "asset", "root", "prd-author.md",
    }
    body, in_fm = [], False
    for ln in text.splitlines():
        if ln.strip() == "---":
            in_fm = not in_fm
            continue
        if in_fm or ln.lstrip().startswith("#"):
            continue
        body.append(ln)
    words = re.findall(r"[a-z0-9]+", " ".join(body).lower())
    return [w for w in words if w not in exempt]


def test_skill_does_not_copy_playbook_prose_fr_1_3a():
    rendered = S.render_skill(S.current_template_path().read_text(), ".")
    playbook = (S.SCAFFOLD_DIR / "prompts" / "prd-author.md").read_text()
    a, b = _normalize_words(rendered), _normalize_words(playbook)
    # A shared run of >= 12 words exists iff some 12-gram is shared, so checking
    # the 12-grams is sufficient and exact for the FR-1.3a "< 12" rule.
    bgrams = {" ".join(b[i:i + 12]) for i in range(len(b) - 11)}
    shared = {" ".join(a[i:i + 12]) for i in range(len(a) - 11)} & bgrams
    assert shared == set(), f"shared >=12-word run(s): {shared}"


# ---- committed-artifact drift guards (single source of truth) ---------------

def test_committed_schema_matches_normative_constant():
    on_disk = json.loads((REPO / "schemas" / "skill-frontmatter.json").read_text())
    assert on_disk == S.SKILL_FRONTMATTER_SCHEMA


def test_own_committed_skill_matches_render_for_repo_asset_root():
    # OQ-1: Gauntlet's own committed skill is the template rendered at asset_root "."
    own = (REPO / S.SKILL_REL).read_text()
    expected = S.render_skill(S.current_template_path().read_text(), ".")
    assert own == expected
    assert S.classify_skill(own, ".") == "generated"
    assert S.validate_skill_frontmatter(own) == []


def test_version_registry_is_append_only_and_consistent():
    # The current version resolves to the named template path.
    assert S.version_template_path(S.CURRENT_TEMPLATE_VERSION) == S.current_template_path()
    # Every superseded version (< current) must remain recognizable in the
    # append-only registry (never retired — §4.5 / review F-004).
    for v in range(1, S.CURRENT_TEMPLATE_VERSION):
        assert S.version_template_path(v) is not None, v
