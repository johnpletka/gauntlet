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


# --- FR-3.1 failure classification: LiteLLM exceptions ----------------------
def test_rate_limit_exception_classified_transient(fake_litellm):
    from gauntlet.adapters.base import AgentFailedError

    class RateLimitError(Exception):
        retry_after = 30

    def boom(**kwargs):
        raise RateLimitError("429 rate limit exceeded")

    fake_litellm.completion = boom
    with pytest.raises(AgentFailedError) as excinfo:
        ApiAdapter(model="m").run("classify")
    info = excinfo.value.failure_info
    assert info is not None and info.kind == "transient_usage_limit"
    assert info.retry_after_s == 30


def test_internal_server_error_classified_overload(fake_litellm):
    from gauntlet.adapters.base import AgentFailedError

    class InternalServerError(Exception):
        pass

    def boom(**kwargs):
        raise InternalServerError("Overloaded")

    fake_litellm.completion = boom
    with pytest.raises(AgentFailedError) as excinfo:
        ApiAdapter(model="m").run("classify")
    assert excinfo.value.failure_info.kind == "transient_overload"


def test_unknown_exception_classified_terminal(fake_litellm):
    from gauntlet.adapters.base import AgentFailedError

    class AuthenticationError(Exception):
        pass

    def boom(**kwargs):
        raise AuthenticationError("bad api key")

    fake_litellm.completion = boom
    with pytest.raises(AgentFailedError) as excinfo:
        ApiAdapter(model="m").run("classify")
    assert excinfo.value.failure_info.kind == "terminal"


# --- #169: provider JSON mode + local repair ---------------------------------
def _provider(name):
    return lambda model, **_: (model, name, None, None)


def test_json_mode_requested_when_litellm_reports_support(fake_litellm):
    fake_litellm.get_llm_provider = _provider("openai")
    fake_litellm.get_supported_openai_params = (
        lambda model=None, **_: ["temperature", "response_format"]
    )
    fake_litellm.responses = [make_response('{"verdict": "ok"}')]
    ApiAdapter(model="m").run("judge", schema=SCHEMA)
    assert fake_litellm.calls[0]["response_format"] == {"type": "json_object"}


def test_json_mode_auto_stays_off_for_emulated_providers(fake_litellm):
    """LiteLLM emulates JSON mode for Anthropic with a forced tool call; auto
    mode leaves those requests exactly as before (forcing still works)."""
    fake_litellm.get_llm_provider = _provider("anthropic")
    fake_litellm.get_supported_openai_params = lambda model=None, **_: ["response_format"]
    fake_litellm.responses = [make_response('{"verdict": "ok"}')] * 2
    ApiAdapter(model="claude-x").run("judge", schema=SCHEMA)
    ApiAdapter(model="claude-x", json_mode=True).run("judge", schema=SCHEMA)
    assert ["response_format" in c for c in fake_litellm.calls] == [False, True]


def test_json_mode_not_requested_without_support_or_schema(fake_litellm):
    fake_litellm.get_llm_provider = _provider("openai")
    fake_litellm.get_supported_openai_params = lambda model=None, **_: ["temperature"]
    fake_litellm.responses = [make_response('{"verdict": "ok"}'), make_response("x")]
    ApiAdapter(model="m").run("judge", schema=SCHEMA)
    ApiAdapter(model="m", json_mode=True).run("no schema, no json mode")
    assert all("response_format" not in c for c in fake_litellm.calls)


def test_json_mode_rejected_by_provider_retries_without_it(fake_litellm):
    class BadRequestError(Exception):
        pass

    def completion(**kwargs):
        fake_litellm.calls.append(kwargs)
        if "response_format" in kwargs:
            raise BadRequestError("response_format json_object is not supported")
        return make_response('{"verdict": "ok"}')

    fake_litellm.completion = completion
    adapter = ApiAdapter(model="m", json_mode=True)
    result = adapter.run("judge", schema=SCHEMA)
    assert result.structured == {"verdict": "ok"}
    assert ["response_format" in c for c in fake_litellm.calls] == [True, False]
    assert adapter.json_mode is False  # not re-asked on later calls
    assert any(e["type"] == "api.json_mode_unsupported" for e in result.raw_events)


def test_unrelated_bad_request_is_not_swallowed_by_json_mode(fake_litellm):
    class BadRequestError(Exception):
        pass

    def completion(**kwargs):
        raise BadRequestError("context window exceeded")

    fake_litellm.completion = completion
    from gauntlet.adapters.base import AgentFailedError

    with pytest.raises(AgentFailedError):
        ApiAdapter(model="m", json_mode=True).run("judge", schema=SCHEMA)


def test_near_miss_json_is_repaired_without_a_reask(fake_litellm):
    fake_litellm.responses = [make_response('{"verdict": "ok"}}')]
    result = ApiAdapter(model="m").run("judge", schema=SCHEMA)
    assert result.structured == {"verdict": "ok"}
    assert len(fake_litellm.calls) == 1
    [repaired] = [e for e in result.raw_events if e["type"] == "api.json_repaired"]
    assert repaired["repairs"] and "'}'" in repaired["repairs"][0]


def test_exhaustion_error_names_the_parse_position(fake_litellm):
    bad = '{"verdict" "ok"}'
    fake_litellm.responses = [make_response(bad) for _ in range(3)]
    with pytest.raises(MalformedOutputError) as exc:
        ApiAdapter(model="m").run("judge", schema=SCHEMA)
    assert "(char 11 of 16)" in str(exc.value)
    # the re-ask told the model where its JSON broke
    reask = fake_litellm.calls[1]["messages"][-1]["content"]
    assert "Expecting ':' delimiter" in reask
