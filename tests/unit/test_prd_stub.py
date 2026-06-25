"""Structured PRD stub: §6 manifest parser, §4.3 precedence, template validity (P2).

Module-level, offline machinery of ``gauntlet.engine.prd_stub``: the playbook
manifest parser (§6), the single-resolved-source lookup precedence (§4.3), and
the guarantee that the shipped stub/playbook pair is a valid gate input (§4.4).
The run-side gate behaviour (FR-2.3/2.4, FR-3.3) and the install (FR-3.2) are in
``test_run_lifecycle.py`` / ``test_init.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.engine import prd_stub as PS

REPO = Path(__file__).resolve().parents[2]


# ---- §6 manifest parser -----------------------------------------------------

def test_parser_reproduces_the_prd_section_manifest():
    # The §6 prose list (informative): mandatory = header-block, §1, §2, §5, §8,
    # §9, §11; scale-with-size = §3, §4, §6, §7, §10. The parser is the source of
    # truth — assert its output matches that prose set exactly.
    playbook = (REPO / "prompts" / "prd-author.md").read_text()
    manifest = PS.parse_manifest(playbook)
    mandatory = [e.name for e in manifest if e.cls == PS.MANDATORY]
    scale = [e.name for e in manifest if e.cls == PS.SCALE]
    assert mandatory == [
        "header block",
        "§1 overview",
        "§2 goals and non-goals",
        "§5 functional requirements",
        "§8 implementation plan (phased, assumption-validating)",
        "§9 success metrics",
        "§11 open questions",
    ]
    assert scale == [
        "§3 users and personas",
        "§4 system architecture",
        "§6 data & schemas (normative excerpts)",
        "§7 security & privacy",
        "§10 risks & mitigations",
    ]


def test_parser_classifies_present_whenever_markers_as_scale():
    # §6/§7 carry "*(present whenever …)*", not the literal word "scale-with-
    # size", and their marker wraps across a line break — the binary class rule
    # (mandatory iff first marker word is "mandatory", else scale) still files
    # them as scale-with-size, matching the PRD §6 prose.
    playbook = (REPO / "prompts" / "prd-author.md").read_text()
    by_name = {e.name: e.cls for e in PS.parse_manifest(playbook)}
    assert by_name["§6 data & schemas (normative excerpts)"] == PS.SCALE
    assert by_name["§7 security & privacy"] == PS.SCALE


# ---- F-002: the parser fails closed on a malformed/ambiguous playbook -------

def _mini_playbook(body: str) -> str:
    """A minimal §2-bounded playbook wrapping ``body`` (the catalogue entries)."""
    return f"## 2. The PRD structure\n\n{body}\n\n## 3. How to grill me\n"


def test_parse_manifest_raises_when_structure_section_absent():
    # No §2 boundary at all → cannot derive the manifest; refuse (no silent empty).
    text = "## 1. intro\n\n**Header block** *(mandatory)*\n\n## 3. next\n"
    with pytest.raises(PS.StubTemplateError, match="§2"):
        PS.parse_manifest(text)


def test_parse_manifest_raises_on_marker_typo_instead_of_demoting():
    # A typo on a MANDATORY marker must not be silently classified scale-with-size
    # (the fail-open the binary rule used to allow); reject the ambiguous marker.
    text = _mini_playbook("**Header block** *(mandtory)*")
    with pytest.raises(PS.StubTemplateError, match="unrecognized class marker"):
        PS.parse_manifest(text)


def test_parse_manifest_raises_on_entry_without_a_class_marker():
    # A bold-paragraph catalogue entry with no *(<class>)* marker is malformed;
    # silently skipping it would drop a (possibly mandatory) section.
    text = _mini_playbook("**Header block** *(mandatory)*\n\n**§5 Functional Requirements**")
    with pytest.raises(PS.StubTemplateError, match="no recognizable"):
        PS.parse_manifest(text)


def test_parse_manifest_raises_on_duplicate_entry():
    text = _mini_playbook("**Header block** *(mandatory)*\n\n**Header block** *(mandatory)*")
    with pytest.raises(PS.StubTemplateError, match="duplicate"):
        PS.parse_manifest(text)


def test_parse_manifest_raises_on_empty_manifest():
    text = _mini_playbook("(no catalogue entries here, just prose)")
    with pytest.raises(PS.StubTemplateError, match="empty"):
        PS.parse_manifest(text)


def test_parse_manifest_raises_when_header_block_anchor_missing():
    text = _mini_playbook(
        "**§1 Overview** *(mandatory)*\n\n**§3 Users and Personas** *(scale-with-size)*"
    )
    with pytest.raises(PS.StubTemplateError, match="header block"):
        PS.parse_manifest(text)


def test_validate_template_rejects_empty_manifest():
    # An empty manifest would enforce no mandatory headers at all (fail open).
    stub = PS.packaged_stub_path().read_text()
    with pytest.raises(PS.StubTemplateError, match="empty §6 manifest"):
        PS.validate_template(stub, [])


def test_scaffold_playbook_twin_is_byte_identical_to_canonical():
    # The manifest may be parsed from either copy (§4.3 fallback); they must agree.
    assert (REPO / "prompts" / "prd-author.md").read_bytes() == (
        PS.SCAFFOLD_DIR / "prompts" / "prd-author.md"
    ).read_bytes()


# ---- shipped stub/playbook pair is a valid gate input (§4.4) ----------------

def test_packaged_stub_validates_against_the_playbook_manifest():
    stub = PS.packaged_stub_path().read_text()
    manifest = PS.parse_manifest((PS.SCAFFOLD_DIR / "prompts" / "prd-author.md").read_text())
    PS.validate_template(stub, manifest)  # must not raise


def test_packaged_stub_structure_matches_the_full_manifest_order():
    # FR-2.2: the stub mirrors the FULL parsed manifest — both classes, in order.
    stub = PS.packaged_stub_path().read_text()
    manifest = PS.parse_manifest((PS.SCAFFOLD_DIR / "prompts" / "prd-author.md").read_text())
    assert PS.stub_section_names(stub) == [e.name for e in manifest]


def test_validate_rejects_empty_and_unmarked_templates():
    manifest = PS.parse_manifest((PS.SCAFFOLD_DIR / "prompts" / "prd-author.md").read_text())
    with pytest.raises(PS.StubTemplateError, match="empty"):
        PS.validate_template("   \n  ", manifest)
    with pytest.raises(PS.StubTemplateError, match="exactly one"):
        PS.validate_template("# no marker here\n\n## §1 Overview\n", manifest)


# ---- §4.3 lookup precedence: one resolved source per repo -------------------

def test_stub_rel_is_repo_relative_per_asset_root():
    assert PS.stub_rel(".") == "prd-stub.md"
    assert PS.stub_rel(".gauntlet") == ".gauntlet/prd-stub.md"
    assert PS.stub_rel("./.gauntlet") == ".gauntlet/prd-stub.md"  # spelling normalised


def test_resolution_prefers_repo_copy_then_falls_back_to_package(tmp_path):
    # absent repo copy → packaged fallback
    text, src = PS.resolve_stub_template(tmp_path, ".")
    assert src == PS.packaged_stub_path()
    assert text == PS.packaged_stub_path().read_text()
    # present repo copy → it is used, by BOTH consumers (same bytes)
    repo_copy = tmp_path / "prd-stub.md"
    repo_copy.write_text("custom\n" + PS.packaged_stub_path().read_text())
    text2, src2 = PS.resolve_stub_template(tmp_path, ".")
    assert src2 == repo_copy
    assert text2 == repo_copy.read_text()


def test_playbook_resolution_falls_back_to_package(tmp_path):
    # no repo copy → packaged twin (always resolvable)
    assert PS.resolve_playbook_text(tmp_path, ".") == (
        PS.SCAFFOLD_DIR / "prompts" / "prd-author.md"
    ).read_text()
    # adopter layout: a repo copy under .gauntlet/prompts/ is preferred
    pb = tmp_path / ".gauntlet" / "prompts" / "prd-author.md"
    pb.parent.mkdir(parents=True)
    pb.write_text("## 2. structure\n\n**Header block** *(mandatory)*\n\n## 3. next\n")
    assert PS.resolve_playbook_text(tmp_path, ".gauntlet") == pb.read_text()
