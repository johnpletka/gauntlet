"""Operator skill + skill-registry generalization (P5: FR-6, FR-7).

Offline, deterministic coverage of:

* the skill registry (FR-7.1) and the byte-for-byte preservation of prd-author
  behavior across the generalization (FR-7.2);
* `init` installing/refreshing the operator skill with the *same* posture as
  prd-author (FR-7.3);
* the operator skill's thin-pointer shape, closed seven-phrase trigger corpus,
  frontmatter, and the playbook's state-class/guardrail coverage (FR-6.1–6.4);
* `doctor` validating the operator skill + playbook at the same severity as
  prd-author (FR-6.5).

The recorded natural-language *trigger* qualification (FR-6.6) is the integration
check in ``tests/integration/test_skill_trigger.py``; metadata inspection here
proves only discovery and the closed corpus, never live triggering.
"""

from __future__ import annotations

import re
from pathlib import Path

from gauntlet.engine import doctor as D
from gauntlet.engine import skill as S
from gauntlet.engine.doctor import FAIL, OK, WARN, DoctorProbes, run_doctor
from gauntlet.engine.init import (
    CREATED,
    CUSTOMIZED,
    MISSING,
    PRESENT,
    REFRESHED,
    SKIPPED,
    WARNED,
    init_repo,
)

REPO = Path(__file__).resolve().parents[2]


# ---- FR-7.1: the registry ---------------------------------------------------

def test_registry_contains_both_skills():
    names = [spec.name for spec in S.SKILL_REGISTRY]
    assert "gauntlet-prd-author" in names
    assert "gauntlet-operator" in names
    # The prd-author spec is bound to the legacy module-level constants so the
    # back-compat shims and the registry can never disagree (FR-7.2).
    assert S.PRD_AUTHOR_SPEC.name == S.SKILL_NAME
    assert S.PRD_AUTHOR_SPEC.playbook_rel == S.PLAYBOOK_REL
    assert S.PRD_AUTHOR_SPEC.template_version == S.CURRENT_TEMPLATE_VERSION
    assert S.PRD_AUTHOR_SPEC.trigger_phrases == S.TRIGGER_PHRASES


def test_operator_spec_skill_rel_and_playbook_rel():
    spec = S.OPERATOR_SPEC
    assert spec.skill_rel == ".claude/skills/gauntlet-operator/SKILL.md"
    assert spec.playbook_rel == "prompts/operator.md"
    assert spec.playbook_ref(".") == "prompts/operator.md"
    assert spec.playbook_ref(".gauntlet") == ".gauntlet/prompts/operator.md"


# ---- FR-7.2: no prd-author regression (byte-identical golden) ---------------

def test_prd_author_committed_skill_byte_identical_after_generalization():
    # The committed prd-author skill is exactly the prd-author template rendered
    # at asset_root "." — unchanged by the registry refactor (FR-7.2 golden).
    own = (REPO / S.SKILL_REL).read_text()
    via_shim = S.render_skill(S.current_template_path().read_text(), ".")
    via_spec = S.PRD_AUTHOR_SPEC.render(S.PRD_AUTHOR_SPEC.template_path().read_text(), ".")
    assert own == via_shim == via_spec
    assert S.classify_skill(own) == "generated"
    assert S.PRD_AUTHOR_SPEC.classify(own) == "generated"


def test_shim_and_spec_agree_for_prd_author():
    # Every back-compat shim equals the prd-author spec method (FR-7.2): the two
    # surfaces are one computation.
    for ar in (".", ".gauntlet"):
        assert S.playbook_ref(ar) == S.PRD_AUTHOR_SPEC.playbook_ref(ar)
        tmpl = S.current_template_path().read_text()
        assert S.render_skill(tmpl, ar) == S.PRD_AUTHOR_SPEC.render(tmpl, ar)
        rendered = S.render_skill(tmpl, ar)
        assert S.classify_skill(rendered) == S.PRD_AUTHOR_SPEC.classify(rendered)
        assert S.skill_looks_stale(rendered, ".") == S.PRD_AUTHOR_SPEC.looks_stale(rendered, ".")
    assert S.current_template_path() == S.PRD_AUTHOR_SPEC.template_path()
    assert S.version_template_path(1) == S.PRD_AUTHOR_SPEC.resolve_version_template(1)


# ---- FR-6.4 / §4.5: operator skill provenance + classification --------------

def test_operator_committed_skill_matches_render_and_validates():
    spec = S.OPERATOR_SPEC
    own = (REPO / spec.skill_rel).read_text()
    expected = spec.render(spec.template_path().read_text(), ".")
    assert own == expected
    assert spec.classify(own) == "generated"
    # FR-6.4: same normative frontmatter schema as prd-author.
    assert S.validate_skill_frontmatter(own) == []
    meta = S.parse_frontmatter(own)
    assert meta["name"] == "gauntlet-operator"
    assert meta["x-gauntlet-generated"] is True
    assert meta["x-gauntlet-template-version"] == spec.template_version


def test_operator_rendered_classifies_generated_for_any_asset_root():
    spec = S.OPERATOR_SPEC
    tmpl = spec.template_path().read_text()
    assert spec.classify(spec.render(tmpl, ".")) == "generated"
    assert spec.classify(spec.render(tmpl, ".gauntlet")) == "generated"
    # an edited body or stripped provenance → customization (never clobbered)
    assert spec.classify(spec.render(tmpl, ".") + "\nedit\n") == "customization"


# ---- FR-6.1: thin pointer, playbook reference resolves, no prose copy --------

def test_operator_skill_is_thin_pointer_with_resolving_reference():
    spec = S.OPERATOR_SPEC
    rendered = spec.render(spec.template_path().read_text(), ".")
    # The rendered skill references the playbook repo-relative, never absolute.
    assert "`prompts/operator.md`" in rendered
    assert S.PLAYBOOK_PLACEHOLDER not in rendered
    for absolute in ("/Users/", "/home/", "/private/", "/tmp/", "C:\\"):
        assert absolute not in rendered
    # FR-6.1: the reference resolves to the committed playbook under the repo's
    # asset_root (".").
    assert (REPO / spec.playbook_ref(".")).is_file()


def _normalize_words(text: str, exempt: set[str]) -> list[str]:
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


def test_operator_skill_does_not_copy_playbook_prose():
    # FR-6.1: the skill points at the playbook; it copies none of the prose body.
    spec = S.OPERATOR_SPEC
    rendered = spec.render(spec.template_path().read_text(), ".")
    playbook = (S.SCAFFOLD_DIR / "prompts" / "operator.md").read_text()
    exempt = {
        "gauntlet", "operator", "run", "slug", "status", "logs", "recover",
        "resume", "approve", "reject", "json", "prompts", "md",
    }
    a = _normalize_words(rendered, exempt)
    b = _normalize_words(playbook, exempt)
    bgrams = {" ".join(b[i:i + 12]) for i in range(len(b) - 11)}
    shared = {" ".join(a[i:i + 12]) for i in range(len(a) - 11)} & bgrams
    assert shared == set(), f"shared >=12-word run(s): {shared}"


# ---- FR-6.2: closed, ordered seven-phrase trigger corpus --------------------

def _extract_trigger_phrases(description: str) -> list[str]:
    """The double-quoted phrases in the description, in document order.

    The operator skill renders its corpus as a quoted, ordered enumeration so the
    closed set can be extracted and checked for *exact* equality — not mere
    presence (an extra eighth phrase must fail).
    """
    return re.findall(r'"([^"]+)"', description)


def test_operator_trigger_corpus_is_the_closed_seven_in_order():
    spec = S.OPERATOR_SPEC
    # The seven phrases are the normative v1 contract (FR-6.2), fixing the FR-6.6
    # denominator.
    assert spec.trigger_phrases == (
        "check the gauntlet run",
        "is the run stuck",
        "is the run parked",
        "approve the gate",
        "reject the gate",
        "why did the step fail",
        "recover the run",
    )
    own = (REPO / spec.skill_rel).read_text()
    meta = S.parse_frontmatter(own)
    extracted = _extract_trigger_phrases(meta["description"])
    # Exact set + order equality, no extras: a presence-only check would pass with
    # an added eighth phrase and silently change the FR-6.6 denominator.
    assert tuple(extracted) == spec.trigger_phrases


# ---- FR-6.3: the playbook covers every state class and guardrail by name -----

def test_operator_playbook_references_every_state_class_and_guardrail():
    playbook = (S.SCAFFOLD_DIR / "prompts" / "operator.md").read_text()
    state_classes = [
        "in_progress", "orphaned", "indeterminate", "parked_gate",
        "parked_for_response", "failed", "halted", "interrupted",
        "done", "aborted", "unknown",
    ]
    for cls in state_classes:
        assert cls in playbook, f"playbook missing run-state class {cls!r}"
    guardrails = [
        "approve a gate unilaterally",   # never approve a gate unilaterally
        "--no-judge",                    # never --no-judge
        "work around a judge deny",      # never work around a judge deny
        "modify files a reviewer or builder owns",  # never touch owned files
    ]
    for guard in guardrails:
        assert guard in playbook, f"playbook missing guardrail {guard!r}"


def test_canonical_and_scaffold_playbooks_are_identical():
    # The canonical playbook (Gauntlet's own repo) and the scaffolded one (shipped
    # to adopters) are byte-identical, mirroring prd-author.
    canonical = (REPO / "prompts" / "operator.md").read_text()
    scaffold = (S.SCAFFOLD_DIR / "prompts" / "operator.md").read_text()
    assert canonical == scaffold


# ---- FR-7.3: init installs/refreshes the operator skill, prd-author posture --

def _actions(result) -> dict:
    return {a.path: a.action for a in result.actions}


def test_init_creates_operator_skill_with_adopter_playbook_path(tmp_path):
    result = init_repo(tmp_path)
    rel = S.OPERATOR_SPEC.skill_rel
    skill_file = tmp_path / rel
    assert skill_file.exists()
    assert _actions(result)[rel] == CREATED
    text = skill_file.read_text()
    # A fresh adopter init scaffolds asset_root .gauntlet → adopter-relative ref.
    assert "`.gauntlet/prompts/operator.md`" in text
    assert S.PLAYBOOK_PLACEHOLDER not in text
    assert S.validate_skill_frontmatter(text) == []
    assert S.OPERATOR_SPEC.classify(text) == "generated"
    # The adopter playbook is scaffolded under .gauntlet/prompts/ too.
    assert (tmp_path / ".gauntlet/prompts/operator.md").is_file()


def test_init_operator_skill_idempotent_and_never_clobbers_customization(tmp_path):
    init_repo(tmp_path)
    rel = S.OPERATOR_SPEC.skill_rel
    skill_file = tmp_path / rel
    before = skill_file.read_text()
    assert _actions(init_repo(tmp_path))[rel] == SKIPPED
    assert skill_file.read_text() == before
    # Customize → never overwritten (skipped or warned).
    custom = before + "\n<!-- hand-tuned -->\n"
    skill_file.write_text(custom)
    assert _actions(init_repo(tmp_path))[rel] in (SKIPPED, WARNED)
    assert skill_file.read_text() == custom


def test_init_refreshes_unmodified_generated_operator_skill_on_asset_root_change(tmp_path):
    init_repo(tmp_path)
    rel = S.OPERATOR_SPEC.skill_rel
    skill_file = tmp_path / rel
    assert "`.gauntlet/prompts/operator.md`" in skill_file.read_text()
    cfg = tmp_path / ".gauntlet/config.yaml"
    cfg.write_text(cfg.read_text().replace("asset_root: .gauntlet", 'asset_root: "."'))
    assert _actions(init_repo(tmp_path))[rel] == REFRESHED
    after = skill_file.read_text()
    assert "`prompts/operator.md`" in after
    assert "`.gauntlet/prompts/operator.md`" not in after


def test_from_repo_reports_operator_skill_present_missing_customized(tmp_path):
    rel = S.OPERATOR_SPEC.skill_rel
    assert _actions(init_repo(tmp_path, from_repo=True))[rel] == MISSING
    init_repo(tmp_path)  # scaffold it
    assert _actions(init_repo(tmp_path, from_repo=True))[rel] == PRESENT
    skill_file = tmp_path / rel
    skill_file.write_text(skill_file.read_text() + "\n<!-- custom -->\n")
    assert _actions(init_repo(tmp_path, from_repo=True))[rel] == CUSTOMIZED


# ---- FR-6.5: doctor validates the operator skill + playbook -----------------

_PINS = (REPO / ".gauntlet" / "pins.yaml").read_text() if (
    REPO / ".gauntlet" / "pins.yaml"
).exists() else "verified_date: 2026-01-01\nclis: {}\n"


def _probes() -> DoctorProbes:
    return DoctorProbes(
        cli_version=lambda name: {"claude": "2.1.172", "codex": "0.139.0"}.get(name),
        env={"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y"},
        cli_authenticated=lambda name: True,
        which=lambda name: f"/usr/bin/{name}",
        judge_model_resolvable=lambda _m: None,
    )


def _healthy(tmp_path: Path) -> Path:
    init_repo(tmp_path)
    (tmp_path / ".gauntlet/pins.yaml").write_text(_PINS)
    return tmp_path


def _by_name(results) -> dict:
    return {r.name: r for r in results}


def test_doctor_operator_skill_ok_when_well_formed(tmp_path):
    results = _by_name(run_doctor(_healthy(tmp_path), probes=_probes()))
    assert results["operator-skill"].status == OK
    assert results["operator-skill-playbook"].status == OK


def test_doctor_operator_skill_warns_never_fails_when_missing(tmp_path):
    repo = _healthy(tmp_path)
    (repo / S.OPERATOR_SPEC.skill_rel).unlink()
    results = _by_name(run_doctor(repo, probes=_probes()))
    op = results["operator-skill"]
    prd = results["prd-skill"]
    # Same severity as prd-author: warn-only, never a FAIL (FR-6.5).
    assert op.status == WARN and op.status != FAIL
    assert prd.status == OK  # the prd-author skill is untouched and healthy


def test_doctor_operator_skill_warns_on_malformed_frontmatter(tmp_path):
    repo = _healthy(tmp_path)
    (repo / S.OPERATOR_SPEC.skill_rel).write_text("no frontmatter at all\n")
    op = _by_name(run_doctor(repo, probes=_probes()))["operator-skill"]
    assert op.status == WARN and op.status != FAIL


def test_doctor_operator_playbook_warns_when_missing(tmp_path):
    repo = _healthy(tmp_path)
    (repo / ".gauntlet/prompts/operator.md").unlink()
    pb = _by_name(run_doctor(repo, probes=_probes()))["operator-skill-playbook"]
    assert pb.status == WARN and pb.status != FAIL


def test_doctor_operator_and_prd_skills_same_severity_when_both_missing(tmp_path):
    repo = _healthy(tmp_path)
    (repo / S.OPERATOR_SPEC.skill_rel).unlink()
    (repo / S.SKILL_REL).unlink()
    results = _by_name(run_doctor(repo, probes=_probes()))
    assert results["operator-skill"].status == results["prd-skill"].status == WARN
