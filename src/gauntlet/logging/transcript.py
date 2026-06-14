"""Transcript logger (FR-4): the durable, human-readable record of a run.

Per step (and per ``adversarial_cycle`` sub-step), three files land in the
FR-4.1 layout, all through the :class:`RedactingWriter` (FR-4.4 — redaction
happens before any byte reaches disk, because these logs target git):

- ``prompt.md``      the exact prompt sent (FR-4.1)
- ``transcript.md``  faithful rendering of every message — prompts, assistant
                     turns, tool calls + results, final output. Nothing
                     summarized away (FR-4.2).
- ``events.jsonl``   the lossless raw event stream (FR-4.2)
- a structured-output file (``findings.json`` etc.) when the step is schema'd

``RUN.md`` (FR-4.3) is the per-run index: every step's transcript link,
verdict, duration, and cost, regenerated from the manifest so it is always
consistent with the state machine.

The renderer understands the three adapter event shapes (claude json/
stream-json, codex JSONL, api.completion) and falls back to a fenced JSON dump
for anything else — unknown events are preserved, never dropped. Codex
sandbox refusals surface only as an OS errno inside ``agent_message`` text on
codex-cli 0.139.0 (BOOTSTRAP-NOTES #11); the renderer flags those lines so the
refusal is findable in the transcript.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from gauntlet.adapters.base import AgentResult
from gauntlet.logging.redact import RedactingWriter

# FR-4.5: written into the scaffolded .gitignore guidance by `gauntlet init`
# (P6 ships the init command; the text is the P4 logger's contract).
GITIGNORE_GUIDANCE = """\
# Gauntlet run logs (FR-4.5)
#
# Manifests and transcripts are designed to be committed: they are the audit
# trail a reviewer uses to reconstruct every decision. The raw event streams
# (events.jsonl) are lossless but heavy; teams that find them too large for
# git can uncomment the exclusion below — transcript.md remains the faithful
# committable record.
#
# runs/*/run-*/steps/**/events.jsonl
#
# The live run-instance directory ignores itself (the engine writes a
# `.gitignore` containing `*` into it); finalized artifacts you want tracked
# should be copied/committed deliberately, not swept in by `git add -A`.
"""

_SANDBOX_REFUSAL_MARKER = "operation not permitted"


class StepLogger:
    """Writes one step's FR-4.1 file set through the redacting writer."""

    def __init__(self, writer: RedactingWriter, step_dir: Path) -> None:
        self.writer = writer
        self.step_dir = Path(step_dir)

    def log_prompt(self, prompt: str) -> None:
        self.writer.write_text(self.step_dir / "prompt.md", prompt)

    def log_result(
        self,
        result: AgentResult,
        *,
        structured_name: str = "structured.json",
        suffix: str = "",
    ) -> None:
        """Persist transcript.md + events.jsonl (+ structured output) for one
        adapter invocation. Lossless: every raw event is written. ``suffix``
        names a failed attempt's record (e.g. ``-attempt1``) so retries never
        overwrite evidence (FR-4.2 / P4.r1 F-007)."""
        self.writer.write_text(
            self.step_dir / f"transcript{suffix}.md",
            render_transcript(result.raw_events, final_text=result.text),
        )
        events_path = self.step_dir / f"events{suffix}.jsonl"
        if not result.raw_events:
            # the lossless record exists even when an adapter reported no
            # events (e.g. test fakes) — absence would read as "not captured"
            self.writer.write_text(events_path, "")
        for event in result.raw_events:
            self.writer.append_jsonl(events_path, event)
        if result.structured is not None:
            self.writer.write_text(
                self.step_dir / structured_name,
                json.dumps(result.structured, indent=2, ensure_ascii=False),
            )

    def log_text(self, name: str, text: str) -> None:
        self.writer.write_text(self.step_dir / name, text)


# --- transcript rendering (FR-4.2) -------------------------------------------
def render_transcript(events: list[dict[str, Any]], *, final_text: str = "") -> str:
    parts: list[str] = ["# Transcript\n"]
    for event in events:
        parts.append(_render_event(event))
    if final_text:
        parts.append(f"## Final output\n\n{final_text}\n")
    return "\n".join(parts)


def _render_event(event: dict[str, Any]) -> str:
    etype = event.get("type", "")
    # -- claude shapes ---------------------------------------------------------
    if etype == "system":
        return _kv_block("system", {
            "subtype": event.get("subtype"),
            "session_id": event.get("session_id"),
            "model": event.get("model"),
        })
    if etype == "assistant" or etype == "user":
        return _render_claude_message(etype, event.get("message") or {})
    if etype == "result":
        body = event.get("result") or ""
        meta = _kv_block("result", {
            "subtype": event.get("subtype"),
            "is_error": event.get("is_error"),
            "total_cost_usd": event.get("total_cost_usd"),
        })
        return f"{meta}\n{body}\n"
    # -- codex shapes ----------------------------------------------------------
    if etype == "thread.started":
        return _kv_block("codex thread.started", {"thread_id": event.get("thread_id")})
    if etype == "item.completed":
        return _render_codex_item(event.get("item") or {})
    if etype in ("turn.completed", "turn.failed"):
        return _kv_block(f"codex {etype}", {"usage": event.get("usage"),
                                            "error": event.get("error")})
    # -- api shape ---------------------------------------------------------------
    if etype == "api.completion":
        return _render_api_completion(event.get("response"))
    # -- gauntlet bookkeeping / unknown: preserve verbatim -----------------------
    return f"## event: {etype or 'unknown'}\n\n```json\n{_dump(event)}\n```\n"


def _render_claude_message(role: str, message: dict[str, Any]) -> str:
    parts = [f"## {role}\n"]
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content + "\n")
        return "\n".join(parts)
    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", "") + "\n")
        elif btype == "tool_use":
            parts.append(
                f"**tool call** `{block.get('name')}` (id {block.get('id')})\n\n"
                f"```json\n{_dump(block.get('input'))}\n```\n"
            )
        elif btype == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = "\n".join(
                    b.get("text", _dump(b)) if isinstance(b, dict) else str(b)
                    for b in body
                )
            error = " (is_error)" if block.get("is_error") else ""
            parts.append(
                f"**tool result**{error} (for {block.get('tool_use_id')})\n\n"
                f"```\n{body}\n```\n"
            )
        else:
            parts.append(f"```json\n{_dump(block)}\n```\n")
    return "\n".join(parts)


def _render_codex_item(item: dict[str, Any]) -> str:
    itype = item.get("type")
    if itype == "agent_message":
        text = item.get("text", "")
        note = ""
        if _SANDBOX_REFUSAL_MARKER in text:
            # codex 0.139.0 emits no command_execution event for a command the
            # sandbox refuses; the errno in agent text is the only signal
            # (BOOTSTRAP-NOTES #11). Flag it so the refusal is findable.
            note = (
                "\n> ⚠ contains a sandbox-refusal errno "
                "(no command_execution event is emitted for refused commands "
                "on codex-cli 0.139.0 — BOOTSTRAP-NOTES #11)\n"
            )
        return f"## codex agent_message\n{note}\n{text}\n"
    if itype == "command_execution":
        return (
            f"## codex command_execution\n\n"
            f"```\n$ {item.get('command', '')}\n"
            f"exit: {item.get('exit_code')}\n"
            f"{item.get('aggregated_output', '')}\n```\n"
        )
    if itype == "reasoning":
        return f"## codex reasoning\n\n{item.get('text', '')}\n"
    return f"## codex item: {itype}\n\n```json\n{_dump(item)}\n```\n"


def _render_api_completion(response: Any) -> str:
    if isinstance(response, dict):
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = None
        if content is not None:
            usage = response.get("usage")
            return (
                f"## api completion\n\n{content}\n\n"
                f"_usage: {_dump(usage)}_\n"
            )
    return f"## api completion (raw)\n\n```json\n{_dump(response)}\n```\n"


def _kv_block(title: str, fields: dict[str, Any]) -> str:
    lines = [f"## {title}\n"]
    for key, value in fields.items():
        if value is not None:
            lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _dump(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(obj)


# --- RUN.md index (FR-4.3) ----------------------------------------------------
def write_run_index(run_dir: Path, manifest: Any, writer: RedactingWriter) -> None:
    """Regenerate RUN.md from the manifest. Idempotent; called on every
    checkpoint so the index never lags the state machine."""
    lines = [
        f"# Run {manifest.run_id} — `{manifest.slug}`\n",
        f"- branch: `{manifest.branch}` (base `{manifest.base_branch}`)",
        f"- pipeline: `{manifest.pipeline.name}` v{manifest.pipeline.version} "
        f"(`{manifest.pipeline.hash[:19]}…`)",
        f"- status: **{manifest.status}**"
        + (f" (at `{manifest.current_step}`)" if manifest.current_step else ""),
        f"- totals: {_fmt_usage(manifest.totals)}",
        "",
        "| step | type | status | duration | usage | notes |",
        "|---|---|---|---|---|---|",
    ]
    for rec in manifest.steps:
        leaf = rec.id if rec.iteration is None else f"{rec.id}.{rec.iteration}"
        step_dir = run_dir / "steps" / leaf
        name = (
            f"[{leaf}](steps/{leaf}/transcript.md)"
            if (step_dir / "transcript.md").exists()
            else f"[{leaf}](steps/{leaf}/)" if step_dir.exists() else leaf
        )
        lines.append(
            f"| {name} | {rec.type} | {rec.status} | {_duration(rec)} "
            f"| {_fmt_usage(rec.usage)} | {_cell(rec.notes)} |"
        )
    if manifest.commits:
        lines += ["", "## Commits", ""]
        lines += [
            f"- `{c.sha[:10]}` {c.phase} (step `{c.step_id}`)"
            for c in manifest.commits
        ]
    writer.write_text(run_dir / "RUN.md", "\n".join(lines) + "\n")


def _duration(rec: Any) -> str:
    if not rec.started or not rec.ended:
        return "—"
    try:
        delta = datetime.fromisoformat(rec.ended) - datetime.fromisoformat(rec.started)
        return f"{delta.total_seconds():.0f}s"
    except ValueError:
        return "—"


def _fmt_usage(usage: Any) -> str:
    if usage is None:
        return "—"
    tokens = f"{usage.input_tokens or 0}in/{usage.output_tokens or 0}out"
    if usage.cost_usd is not None:
        return f"{tokens} ${usage.cost_usd:.4f}"
    return f"{tokens} (tokens only)"  # degraded path, PRD §12 Q3


def _cell(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ")
