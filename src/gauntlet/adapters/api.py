"""ApiAdapter: LiteLLM completions for non-agentic tasks (PRD §4.1).

Used for classification work that needs no repo access: triage, judge LLM
fallback, retro summarization, commit messages. Structured output is enforced
locally — schema embedded in the prompt, response parsed and validated with
jsonschema, bounded validate-and-retry with error feedback (FR-3.4
groundwork) — so correctness never depends on per-provider response_format
support. For providers whose API has a native JSON mode (OpenAI/Azure) and a
model LiteLLM reports accepts ``response_format``, the adapter ALSO asks for
it as a first net (#169); the local
parse/validate stays the authority, and a provider that rejects the parameter
is retried once without it. A near-miss reply (one stray bracket) is repaired
locally before spending a re-ask, and the repair is recorded in the events.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from gauntlet.adapters._structured import (
    parse_json_output,
    schema_instruction,
    validate_schema,
)
from gauntlet.adapters.base import (
    AdapterCapabilities,
    AgentFailedError,
    AgentResult,
    MalformedOutputError,
    UnsupportedFeatureError,
    Usage,
)
from gauntlet.adapters.failure_markers import classify_api_failure

DEFAULT_TIMEOUT_S = 120.0
# Providers whose API implements ``response_format: json_object`` natively.
# LiteLLM also advertises it for others (e.g. Anthropic) by emulating it with a
# forced tool call, which changes the reply's shape; auto mode stays off there
# (``json_mode=True`` still forces it).
_NATIVE_JSON_MODE_PROVIDERS = frozenset({"openai", "azure"})


class ApiAdapter:
    name = "api"
    capabilities = AdapterCapabilities(
        # FR-1.3: the in-process API adapter has no repo access — it never runs a
        # tool in the working tree — so reference/`phase` context modes are
        # rejected for an api profile at load (reads_repo stays False).
        repo_write=False, structured_output="native", resume=False,
        reads_repo=False,
    )

    def __init__(
        self,
        *,
        model: str,
        max_schema_retries: int = 2,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        json_mode: bool | None = None,
    ) -> None:
        self.model = model
        self.max_schema_retries = max_schema_retries
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Reasoning-model latency knob (gpt-5 family): "minimal" turns a
        # multi-second deliberation into a fast classification (notes #26).
        self.reasoning_effort = reasoning_effort
        # Provider JSON mode for schema-bound calls (#169): None = auto (on when
        # LiteLLM reports `response_format` support for this model), True/False
        # force it. Flipped off for this adapter instance if the provider
        # rejects the parameter, so one unsupported model costs one extra call.
        self.json_mode = json_mode

    def run(
        self,
        prompt: str,
        *,
        session: str | None = None,
        schema: dict | None = None,
        cwd: Path | None = None,  # accepted for interface parity; unused
        extra_flags: list[str] | None = None,  # meaningless for an API call
        # accepted for interface parity; unused. Live streaming is a durable
        # Non-Goal for the API adapter: it is an in-process call (no subprocess
        # reader) and streams token deltas — a secret can span chunks, which
        # breaks the per-line redaction unit (live-run-observability §2.2).
        sink: Callable[[str], None] | None = None,
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
            try:
                response = self._complete_maybe_json(messages, schema, raw_events)
            except Exception as exc:  # noqa: BLE001 — classify then fail closed
                # FR-3.1: a LiteLLM RateLimitError (429) / InternalServerError
                # (overload) is a transient park-and-resume condition, not a step
                # failure; classify by exception class and carry it on the error
                # so the orchestrator parks with parked_reason=usage_limit. Any
                # unrecognized exception classifies terminal (fail closed).
                raise AgentFailedError(
                    f"api call failed: {type(exc).__name__}: {exc}",
                    partial=self._result(text, None, usage_total, raw_events),
                    failure_info=classify_api_failure(exc),
                ) from exc
            raw_events.append(_response_to_dict(response))
            text = response.choices[0].message.content or ""
            _accumulate_usage(usage_total, response)
            if schema is None:
                return self._result(text, None, usage_total, raw_events)
            try:
                structured, repairs = parse_json_output(text)
                validate_schema(structured, schema)
            except ValueError as exc:
                last_error = str(exc)
                continue
            if repairs:
                # Data over inference: the value we are about to trust was
                # repaired, so say exactly how next to the raw completion.
                raw_events.append({"type": "api.json_repaired", "repairs": repairs})
            return self._result(text, structured, usage_total, raw_events)
        raise MalformedOutputError(
            f"schema validation failed after {1 + self.max_schema_retries} "
            f"attempts: {last_error}",
            partial=self._result(text, None, usage_total, raw_events),
        )

    @staticmethod
    def _render(prompt: str, schema: dict | None) -> str:
        return prompt + schema_instruction(schema) if schema else prompt

    def _wants_json_mode(self, schema: dict | None) -> bool:
        """Request provider JSON mode for this call?

        Only for an object-rooted schema (``json_object`` mode cannot return a
        top-level array) and only when forced on, or the provider's JSON mode
        is native and LiteLLM reports the model accepts ``response_format``.
        Any lookup failure means "not supported" —
        the local parse is the authority either way, so the safe default is to
        leave the request exactly as it was before JSON mode existed.
        """
        if schema is None or schema.get("type") != "object" or self.json_mode is False:
            return False
        if self.json_mode is True:
            return True
        try:
            import litellm

            provider = litellm.get_llm_provider(self.model)[1]
            if provider not in _NATIVE_JSON_MODE_PROVIDERS:
                return False
            supported = litellm.get_supported_openai_params(
                model=self.model, custom_llm_provider=provider
            ) or []
        except Exception:
            return False
        return "response_format" in supported

    def _complete_maybe_json(
        self, messages: list[dict[str, str]], schema: dict | None,
        raw_events: list[dict[str, Any]],
    ) -> Any:
        """One completion, in provider JSON mode when :meth:`_wants_json_mode`.

        A provider that refuses the parameter itself (not a quota/overload
        fault — those propagate and park) is recorded, JSON mode is switched
        off for this adapter instance, and the same call is retried without it.
        """
        if not self._wants_json_mode(schema):
            return self._complete(messages)
        try:
            return self._complete(messages, response_format={"type": "json_object"})
        except Exception as exc:  # noqa: BLE001 — narrowed just below
            if not _rejects_response_format(exc):
                raise
            self.json_mode = False
            raw_events.append({
                "type": "api.json_mode_unsupported",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            })
        return self._complete(messages)

    def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, str] | None = None,
    ) -> Any:
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
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if response_format is not None:
            kwargs["response_format"] = response_format
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


def _rejects_response_format(exc: Exception) -> bool:
    """True when ``exc`` is the provider refusing the ``response_format`` param.

    Matched narrowly — a bad-request / unsupported-params class whose message
    names the parameter — so a rate limit, an overload, or an unrelated 400
    still propagates to the FR-3.1 classifier unchanged.
    """
    name = type(exc).__name__
    if name not in {"BadRequestError", "UnsupportedParamsError", "UnprocessableEntityError"}:
        return False
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text or "json mode" in text


def _accumulate_usage(total: Usage, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is not None:
        total.input_tokens = (total.input_tokens or 0) + (
            getattr(usage, "prompt_tokens", 0) or 0
        )
        total.output_tokens = (total.output_tokens or 0) + (
            getattr(usage, "completion_tokens", 0) or 0
        )
        # OpenAI-style detail blocks (LiteLLM normalizes providers onto them):
        # cached prompt tokens and reasoning output. Absent → left None.
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
        if isinstance(cached, int):
            total.cached_input_tokens = (total.cached_input_tokens or 0) + cached
        reasoning = getattr(
            getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
        )
        if isinstance(reasoning, int):
            total.reasoning_output_tokens = (total.reasoning_output_tokens or 0) + reasoning
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


def model_provider_error(model: str) -> str | None:
    """``None`` if LiteLLM can resolve a provider for ``model`` (a no-network
    lookup), else a short one-line error.

    The single source of truth for "is this model id usable", shared by the
    judge startup path and ``gauntlet doctor`` so an unresolvable id (e.g.
    ``claude-heroku``) is caught the SAME way in both — it would otherwise be
    announced as an enabled classifier and then fail every call closed (FR-7.2).
    LiteLLM unavailable → ``None``: we cannot verify offline, so we do not warn."""
    try:
        import litellm

        litellm.get_llm_provider(model)
    except ImportError:
        return None
    except Exception as exc:
        first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        return first[:200]
    return None
