"""Contract test for the LiteLLM ApiAdapter (plan P1): one cheap real call."""

import os

import pytest

from gauntlet.adapters.api import ApiAdapter

pytestmark = pytest.mark.integration

# Override with GAUNTLET_TEST_API_MODEL for environments keyed differently.
MODEL = os.environ.get("GAUNTLET_TEST_API_MODEL", "gpt-5-mini")


@pytest.fixture(autouse=True)
def _need_key():
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        pytest.skip("no API key in environment for the ApiAdapter contract test")


def test_cheap_completion_with_schema():
    schema = {
        "type": "object",
        "properties": {"capital": {"type": "string"}},
        "required": ["capital"],
        "additionalProperties": False,
    }
    result = ApiAdapter(model=MODEL).run(
        "What is the capital of France?", schema=schema
    )
    assert result.structured["capital"].lower().startswith("paris")
    assert result.exit_code == 0
    assert (result.usage.input_tokens or 0) > 0
    assert (result.usage.output_tokens or 0) > 0
    # cost is derivable for API-key models; tokens-only is the fallback
    assert result.usage.cost_usd is None or result.usage.cost_usd > 0
