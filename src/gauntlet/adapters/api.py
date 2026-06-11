"""ApiAdapter: LiteLLM completions for non-agentic tasks (PRD §4.1).

Used for classification work that needs no repo access: triage, judge LLM
fallback, retro summarization, commit messages. Structured output is enforced
locally — schema embedded in the prompt, response parsed and validated with
jsonschema, bounded validate-and-retry with error feedback (FR-3.4
groundwork) — so behavior is identical across providers rather than
depending on per-provider response_format support.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gauntlet.adapters._structured import (
    extract_json,
    schema_instruction,
    validate_schema,
)
from gauntlet.adapters.base import (
    AdapterCapabilities,
    AgentResult,
    MalformedOutputError,
    UnsupportedFeatureError,
    Usage,
)

DEFAULT_TIMEOUT_S = 120.0


class ApiAdapter:
    name = "api"
    capabilities = AdapterCapabilities(
        repo_write=False, structured_output="native", resume=False
    )

    def __init__(
        self,
        *,
        model: str,
        max_schema_retries: int = 2,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.model = model
        self.max_schema_retries = max_schema_retries
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.max_tokens = max_tokens

    def run(
        self,
        prompt: str,
        *,
        session: str | None = None,
        schema: dict | None = None,
        cwd: Path | None = None,  # accepted for interface parity; unused
        extra_flags: list[str] | None = None,  # meaningless for an API call
    ) -> AgentResult:
        if session is not None:
            raise UnsupportedFeatureError(
                "ApiAdapter has no session continuity (capabilities.resume=False)"
            )
        if extra_flags:
            raise UnsupportedFeatureError(
                "ApiAdapter takes no CLI flags; configure model parameters instead"
            )
        messages = [{"role": "user", "content": self._render(prompt, schema)}]
        raw_events: list[dict[str, Any]] = []
        usage_total = Usage(input_tokens=0, output_tokens=0, cost_usd=None)
        last_error: str | None = None
        text = ""
        for _attempt in range(1 + self.max_schema_retries):
            if last_error is not None:
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was rejected: "
                            f"{last_error}. Respond again with only the "
                            "corrected JSON."
                        ),
                    }
                )
            response = self._complete(messages)
            raw_events.append(_response_to_dict(response))
            text = response.choices[0].message.content or ""
            _accumulate_usage(usage_total, response)
            if schema is None:
                return self._result(text, None, usage_total, raw_events)
            try:
                structured = extract_json(text)
                validate_schema(structured, schema)
            except ValueError as exc:
                last_error = str(exc)
                continue
            return self._result(text, structured, usage_total, raw_events)
        raise MalformedOutputError(
            f"schema validation failed after {1 + self.max_schema_retries} "
            f"attempts: {last_error}",
            partial=self._result(text, None, usage_total, raw_events),
        )

    @staticmethod
    def _render(prompt: str, schema: dict | None) -> str:
        return prompt + schema_instruction(schema) if schema else prompt

    def _complete(self, messages: list[dict[str, str]]) -> Any:
        import litellm  # deferred: heavy import, only needed on the API path

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "timeout": self.timeout_s,
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        return litellm.completion(**kwargs)

    @staticmethod
    def _result(
        text: str,
        structured: Any | None,
        usage: Usage,
        raw_events: list[dict[str, Any]],
    ) -> AgentResult:
        return AgentResult(
            text=text,
            structured=structured,
            session_id=None,
            usage=usage,
            raw_events=raw_events,
            exit_code=0,
        )


def _accumulate_usage(total: Usage, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is not None:
        total.input_tokens = (total.input_tokens or 0) + (
            getattr(usage, "prompt_tokens", 0) or 0
        )
        total.output_tokens = (total.output_tokens or 0) + (
            getattr(usage, "completion_tokens", 0) or 0
        )
    cost = _completion_cost(response)
    if cost is not None:
        total.cost_usd = (total.cost_usd or 0.0) + cost
    # if cost stays None: tokens-only degraded path (PRD §12 Q3)


def _completion_cost(response: Any) -> float | None:
    import litellm

    try:
        return litellm.completion_cost(completion_response=response)
    except Exception:
        return None


def _response_to_dict(response: Any) -> dict[str, Any]:
    for attr in ("model_dump", "dict", "to_dict"):
        method = getattr(response, attr, None)
        if callable(method):
            try:
                return {"type": "api.completion", "response": method()}
            except Exception:
                continue
    return {"type": "api.completion", "response": repr(response)}
