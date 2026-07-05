"""P7 — effort tiering plumbing (FR-6.1).

The canonical effort enum `{minimal, low, medium, high}` maps onto each adapter's
accepted surface at build time (claude `--effort`, codex/api
`reasoning_effort`); an unsupported value fails at config load rather than
silently dropping; a step-level `effort:` composes with the profile, the step
winning. These assertions are independent of any not-yet-ratified default effort
*value* (Q2) — they test plumbing/wiring only.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from gauntlet.engine.config import (
    CANONICAL_EFFORTS,
    AgentProfile,
    RunConfig,
    map_effort,
)
from gauntlet.engine.pipeline import Pipeline
from gauntlet.engine.validate import PipelineValidationError, validate_pipeline


# --- per-adapter mapping lands in the constructed adapter --------------------
def test_claude_effort_maps_to_effort_flag():
    adapter = AgentProfile(adapter="claude-code", model="opus", effort="high").build_adapter()
    assert adapter.effort == "high"


def test_codex_effort_maps_to_reasoning_effort():
    adapter = AgentProfile(adapter="codex", effort="medium").build_adapter()
    assert adapter.reasoning_effort == "medium"
    # and it lands in the constructed argv as the config-key override
    argv = adapter._build_argv(session=None, schema_path=None, output_path=None)
    idx = argv.index("-c")
    assert argv[idx + 1] == 'model_reasoning_effort="medium"'


def test_api_effort_maps_to_reasoning_effort():
    adapter = AgentProfile(adapter="api", model="gpt-5-mini", effort="low").build_adapter()
    assert adapter.reasoning_effort == "low"


def test_claude_minimal_remaps_to_low_with_load_warning():
    # FR-6.1: claude `--effort` accepts {low, medium, high}; canonical `minimal`
    # maps to `low` with a load-time warning (never a silent drop, never an error).
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        profile = AgentProfile(adapter="claude-code", model="opus", effort="minimal")
    assert any("minimal" in str(w.message) and "low" in str(w.message) for w in caught)
    assert profile.build_adapter().effort == "low"


# --- unsupported value is a config-load error, not a silent drop -------------
def test_noncanonical_effort_raises_at_config_load():
    # `xhigh` is outside the canonical enum (even though codex accepts it live);
    # the normative enum is the contract, so it fails closed at load.
    with pytest.raises(ValidationError):
        AgentProfile(adapter="codex", effort="xhigh")


def test_noncanonical_effort_rejected_in_runconfig_load():
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {"agents": {"b": {"adapter": "claude-code", "model": "opus", "effort": "ultra"}}}
        )


def test_map_effort_rejects_unknown_adapter():
    with pytest.raises(ValueError):
        map_effort("no-such-adapter", "high")


def test_map_effort_rejects_noncanonical_value():
    with pytest.raises(ValueError):
        map_effort("claude-code", "xhigh")


def test_map_effort_surface_per_adapter():
    assert map_effort("claude-code", "high") == ("effort", "high", None)
    assert map_effort("codex", "minimal") == ("reasoning_effort", "minimal", None)
    assert map_effort("api", "medium") == ("reasoning_effort", "medium", None)
    kwarg, value, warning = map_effort("claude-code", "minimal")
    assert (kwarg, value) == ("effort", "low") and warning is not None


# --- profile + step compose, step winning ------------------------------------
def test_step_effort_overrides_profile_effort_step_wins():
    profile = AgentProfile(adapter="claude-code", model="opus", effort="low")
    assert profile.build_adapter().effort == "low"          # profile default
    assert profile.build_adapter(effort="high").effort == "high"  # step wins


def test_no_effort_leaves_adapter_default():
    # A profile with no effort passes nothing through — the adapter default holds.
    assert AgentProfile(adapter="claude-code", model="opus").build_adapter().effort is None


# --- pipeline load validates a step-level `effort:` --------------------------
_CONFIG = {
    "agents": {"builder": {"adapter": "claude-code", "model": "opus"}},
    "identities": {},
}


def _pipeline(effort: str) -> Pipeline:
    return Pipeline.model_validate(
        {
            "name": "p", "version": 1,
            "stages": [{"id": "s", "steps": [
                {"id": "impl", "type": "agent_task", "agent": "builder",
                 "prompt_text": "go", "effort": effort},
            ]}],
        }
    )


def test_pipeline_load_accepts_valid_step_effort():
    report = validate_pipeline(_pipeline("high"), RunConfig.model_validate(_CONFIG))
    assert report.ok()


def test_pipeline_load_rejects_bad_step_effort():
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(_pipeline("xhigh"), RunConfig.model_validate(_CONFIG))
    assert any("effort" in e for e in exc.value.errors)


def test_canonical_enum_is_the_documented_set():
    assert CANONICAL_EFFORTS == ("minimal", "low", "medium", "high")


# --- adversarial_cycle step-level `effort:` (review F-004) --------------------
# The plan (P7) says a cycle step effort accepts the canonical enum and overrides
# each role profile (step wins). Validation must therefore run for a cycle — whose
# roles are `reviewer`/`triager`/`fixer`/`confirmer`, NOT `agent` — against every
# role's adapter, rather than being silently skipped for the whole step type.
_CYCLE_CONFIG = {
    "agents": {
        "reviewer": {"adapter": "codex"},
        "triage": {"adapter": "api", "model": "gpt-5-mini"},
        "builder": {"adapter": "claude-code", "model": "opus"},
    },
    "identities": {},
}


def _cycle_pipeline(effort: str) -> Pipeline:
    return Pipeline.model_validate(
        {
            "name": "p", "version": 1,
            "stages": [{"id": "s", "steps": [
                {"id": "cyc", "type": "adversarial_cycle", "mode": "artifact",
                 "artifact": "prd.md", "phase": "P1", "reviewer": "reviewer",
                 "triager": "triage", "fixer": "builder", "effort": effort},
            ]}],
        }
    )


def test_cycle_step_effort_accepted_when_valid():
    report = validate_pipeline(
        _cycle_pipeline("high"), RunConfig.model_validate(_CYCLE_CONFIG)
    )
    assert report.ok()


def test_cycle_step_effort_rejected_when_noncanonical():
    # Regression (F-004): a non-canonical cycle effort must fail closed at load —
    # previously it was silently ignored because the cycle has no `agent:` field.
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(
            _cycle_pipeline("xhigh"), RunConfig.model_validate(_CYCLE_CONFIG)
        )
    assert any("effort" in e for e in exc.value.errors)


def test_cycle_step_effort_minimal_warns_via_claude_role_surface():
    # Per-role validation proof (F-004): `minimal` is canonical but claude's
    # --effort surface only accepts {low, medium, high}, so it remaps minimal→low
    # with a WARNING (not an error). A warning therefore only appears if the
    # claude fixer role's adapter surface was actually consulted for the cycle
    # step effort — the bug was that the whole step type was skipped.
    report = validate_pipeline(
        _cycle_pipeline("minimal"), RunConfig.model_validate(_CYCLE_CONFIG)
    )
    assert report.ok()  # a remap is a warning, not an error
    assert any("effort" in w and "minimal" in w for w in report.warnings), report.warnings
