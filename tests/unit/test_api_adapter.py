"""ApiAdapter: schema validate-and-retry, usage accumulation, no-resume."""

import sys
import types
from types import SimpleNamespace

import pytest

from gauntlet.adapters.api import ApiAdapter
from gauntlet.adapters.base import MalformedOutputError, UnsupportedFeatureError


def make_response(content, *, prompt_tokens=10, completion_tokens=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )


@pytest.fixture
def fake_litellm(monkeypatch):
    """Install a fake litellm module so units never import the real one."""
    module = types.ModuleType("litellm")
    module.calls = []
    module.responses = []
    module.cost_per_call = 0.001

    def completion(**kwargs):
        module.calls.append(kwargs)
        return module.responses.pop(0)

    def completion_cost(completion_response=None):
        if module.cost_per_call is None:
            raise ValueError("no pricing for model")
        return module.cost_per_call

    module.completion = completion
    module.completion_cost = completion_cost
    monkeypatch.setitem(sys.modules, "litellm", module)
    return module


SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}


def test_plain_completion(fake_litellm):
    fake_litellm.responses = [make_response("fine answer")]
    result = ApiAdapter(model="test/cheap").run("classify this")
    assert result.text == "fine answer"
    assert result.structured is None
    assert result.session_id is None
    assert result.exit_code == 0
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.usage.cost_usd == pytest.approx(0.001)
    assert fake_litellm.calls[0]["model"] == "test/cheap"


def test_schema_embedded_in_prompt(fake_litellm):
    fake_litellm.responses = [make_response('{"verdict": "ok"}')]
    ApiAdapter(model="m").run("judge", schema=SCHEMA)
    prompt = fake_litellm.calls[0]["messages"][0]["content"]
    assert "JSON schema" in prompt
    assert '"verdict"' in prompt


def test_schema_valid_first_try(fake_litellm):
    fake_litellm.responses = [make_response('{"verdict": "legit"}')]
    result = ApiAdapter(model="m").run("judge", schema=SCHEMA)
    assert result.structured == {"verdict": "legit"}
    assert len(fake_litellm.calls) == 1


def test_schema_retry_recovers(fake_litellm):
    fake_litellm.responses = [
        make_response("not json at all"),
        make_response('{"verdict": 3}'),  # parses but violates schema
        make_response('{"verdict": "legit"}'),
    ]
    result = ApiAdapter(model="m", max_schema_retries=2).run("judge", schema=SCHEMA)
    assert result.structured == {"verdict": "legit"}
    assert len(fake_litellm.calls) == 3
    # retry feedback names the rejection
    retry_prompt = fake_litellm.calls[1]["messages"][-1]["content"]
    assert "rejected" in retry_prompt
    # usage accumulates across attempts
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 15
    assert len(result.raw_events) == 3


def test_schema_retry_exhaustion_raises(fake_litellm):
    fake_litellm.responses = [make_response("nope")] * 3
    with pytest.raises(MalformedOutputError) as excinfo:
        ApiAdapter(model="m", max_schema_retries=2).run("judge", schema=SCHEMA)
    assert len(fake_litellm.calls) == 3  # bounded: 1 + max_schema_retries
    partial = excinfo.value.partial
    assert partial is not None
    assert partial.usage.input_tokens == 30  # spend is still accounted


def test_tokens_only_when_cost_unavailable(fake_litellm):
    fake_litellm.cost_per_call = None  # completion_cost raises
    fake_litellm.responses = [make_response("hi")]
    result = ApiAdapter(model="m").run("x")
    assert result.usage.input_tokens == 10
    assert result.usage.cost_usd is None  # PRD §12 Q3 degraded path


def test_resume_unsupported(fake_litellm):
    with pytest.raises(UnsupportedFeatureError):
        ApiAdapter(model="m").run("x", session="sess-1")


def test_extra_flags_unsupported(fake_litellm):
    with pytest.raises(UnsupportedFeatureError):
        ApiAdapter(model="m").run("x", extra_flags=["--whatever"])
