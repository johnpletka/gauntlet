# Confirm pass — Gauntlet bootstrap, Phase P1, round 1

You are the adversarial reviewer who produced the findings below against
commit `725f8ac`. The builder has applied fixes in commit `de140c6`
("P1.1: Address review"). Your job now is ONLY to check whether the diff
addressed each of your findings — you are NOT re-reviewing the whole phase.
Scope yourself to the diff.

For each finding, return a verdict: `resolved | partially_resolved |
unresolved | regression_introduced`, with a one-or-two-sentence note. If the
diff itself introduces a new defect, report it in `new_findings` (otherwise
return an empty array). Output only JSON conforming to the provided schema.

## Your prior findings (round 1)

```json
{"findings":[{"id":"F-001","severity":"blocking","category":"correctness","location":"src/gauntlet/adapters/claude_code.py:121; src/gauntlet/adapters/codex.py:122","claim":"Both CLI adapters can return a successful AgentResult for a failed CLI invocation.","evidence":"ClaudeCodeAdapter._parse returns AgentResult from the last result event without checking out.exit_code, result_event.subtype, or result_event.is_error. CodexAdapter._parse likewise returns once it finds a final message, even if out.exit_code is non-zero. CLAUDE.md §2 requires halt on unexpected exit code, and PRD §8 requires fail-closed posture throughout.","suggested_fix":"Require exit_code == 0 and adapter-specific success markers before returning. Raise AdapterError or MalformedOutputError with a partial AgentResult on non-zero exit or explicit error events, and add unit tests for parseable non-zero outputs."},{"id":"F-002","severity":"major","category":"principle-violation","location":"src/gauntlet/adapters/claude_code.py:157; src/gauntlet/adapters/codex.py:157; tests/unit/test_codex_adapter.py:131","claim":"Malformed JSONL event streams are tolerated as successful raw events instead of failing closed.","evidence":"Both _decode_events methods catch json.JSONDecodeError and append a gauntlet.unparsed_line event; Codex has a unit test explicitly asserting this success path. The P1 plan calls for malformed-output errors, and CLAUDE.md §2 says parse errors from external calls must halt, not continue.","suggested_fix":"Treat any non-empty non-JSON stdout line in JSON/JSONL modes as MalformedOutputError while preserving raw stdout/stderr in the partial result. If a CLI emits known benign warnings, pin and parse that behavior explicitly instead of silently accepting arbitrary garbage."},{"id":"F-003","severity":"blocking","category":"security","location":"src/gauntlet/config.py:35; src/gauntlet/adapters/codex.py:105","claim":"The bypass lint misses Codex sandbox overrides via -c sandbox_mode=\"danger-full-access\".","evidence":"BANNED_FLAG_VALUES only checks --sandbox and -s values. CodexAdapter itself uses -c sandbox_mode=\"...\" on resume, so this is a real control path; base_flags or extra_flags can pass a later -c sandbox_mode=\"danger-full-access\" through lint. PRD §8 and the P1 plan require permission-bypass and hook-disabling flags to be rejected at config load, not merely avoided.","suggested_fix":"Parse -c/--config assignments in lint_flags and reject sandbox_mode=\"danger-full-access\" and equivalent bypass settings. Also reject duplicate sandbox overrides after the adapter has emitted its safe sandbox setting."},{"id":"F-004","severity":"major","category":"security","location":"src/gauntlet/logging/redact.py:93; src/gauntlet/logging/redact.py:131","claim":"append_jsonl can leak known env-secret values that are JSON-escaped before redaction.","evidence":"Env-secret redaction is an exact text.count/text.replace of the raw env value. append_jsonl first json.dumps the object and only then redacts, so a secret containing a quote, backslash, or newline is serialized as an escaped string that no longer matches the raw env value. FR-4.4 and the P1 plan require values of known secret env vars to be masked before write.","suggested_fix":"Redact structured string values recursively before json.dumps, or add JSON-escaped variants of env secrets to the redactor. Add tests for env secrets containing quotes, backslashes, and newlines."},{"id":"F-005","severity":"minor","category":"spec-gap","location":".gauntlet/pins.yaml:65; .gauntlet/pins.yaml:72; tests/integration/test_codex_contract.py:47","claim":"The pin file records some Codex behavior as contract-verified without a matching live contract test.","evidence":"pins.yaml says exec resume accepts --json, --output-schema, and -o, and says --full-auto does not exist. The live Codex resume contract test only exercises a plain resume path without schema; there is no integration assertion for resume + --output-schema, nor for --full-auto absence. The P1 plan says the pin file records the exact flags the contract tests verified.","suggested_fix":"Add a live contract test for resume with schema=SCHEMA, and either add a read-only help/flag assertion for --full-auto absence or remove that claim from the contract-verified section."}],"open_questions":[],"summary":"P1 has the right broad shape, but it is not shippable as-is because the adapter core violates fail-closed behavior and the safety lint has a real bypass path. The tests cover happy paths and some malformed outputs, but they miss parseable failures and overstate what the pin file proves. The redacting writer is a useful start, but JSONL escaping creates a concrete secret-leak false negative."}
```

## Triage verdicts (ratified by the human)

```json
{
  "verdicts": [
    {
      "finding_id": "F-001",
      "verdict": "legitimate",
      "reasoning": "ClaudeCodeAdapter._parse returns a normal AgentResult even when is_error/subtype signals failure or exit_code != 0, and CodexAdapter does the same on nonzero exit. CLAUDE.md \u00a72's fail-closed rule names unexpected exit codes explicitly; a downstream consumer reading .text would continue past a failed call. Fix: raise AdapterError carrying the fully-parsed partial result on nonzero exit, is_error (claude), or turn.failed (codex), with unit tests for parseable-failure outputs.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-002",
      "verdict": "legitimate",
      "reasoning": "Tolerating non-JSON stdout lines in JSONL modes is the 'silently continue' posture \u00a72 forbids \u2014 with --json both CLIs route logs to stderr, so a non-JSON stdout line means the output contract broke. Fix: MalformedOutputError with the raw lines preserved in the partial result; the codex unit test that asserted tolerance changes with this ratified behavior and is documented per-finding in the fix commit. Any benign warning line that surfaces later gets pinned explicitly, not absorbed silently.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-003",
      "verdict": "legitimate",
      "reasoning": "Self-inflicted hole: the adapter itself introduced the -c sandbox_mode spelling on resume, so the config-override channel is a live control path, and lint_flags never inspects -c/--config assignments \u2014 extra_flags could smuggle sandbox_mode=danger-full-access through. Fix: lint parses -c/--config key=value pairs (quoted or bare, = or space form) and rejects bypass sandbox values. The suggested duplicate-override policing beyond bypass values is P3 config-validation scope and is not adopted here.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-004",
      "verdict": "legitimate",
      "reasoning": "Correct false negative: json.dumps escapes quotes, backslashes and newlines, so an env secret containing any of them no longer matches its raw value at redaction time and lands on disk un-masked. Fix: the Redactor also matches the JSON-escaped variant of every env secret value; tests cover quote/backslash/newline-bearing secrets through append_jsonl.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-005",
      "verdict": "legitimate",
      "reasoning": "Fair catch on pin-file honesty: resume-accepts-schema and --full-auto absence were verified by --help surface inspection, not by the contract suite, yet sit alongside contract-verified entries with no distinction. Fix: add a live resume-with-schema contract test (P4's diff-scoped confirm pass will lean on exactly that combination) plus a help-surface assertion for --full-auto absence, and reword the affected pin entries to cite how each was verified.",
      "action": "fix_now"
    }
  ]
}
```

## The fix diff (commit range 980020e..de140c6 — the only thing under review)

```diff
diff --git a/.gauntlet/pins.yaml b/.gauntlet/pins.yaml
index 4458303..3c359a0 100644
--- a/.gauntlet/pins.yaml
+++ b/.gauntlet/pins.yaml
@@ -64,14 +64,18 @@ clis:
           file in a disposable fixture repo.
       - flag: "exec resume <session-id>"
         verified: >-
-          Prior-turn content (codeword) recalled. resume accepts --json,
-          --output-schema and -o, but NOT --sandbox/-s — the sandbox must be
-          pinned via -c sandbox_mode="..." on resume.
+          Live contract tests: plain resume recalled prior-turn content
+          (codeword), and resume + --output-schema + -o returned conforming
+          JSON (the combination P4's confirm pass relies on). resume accepts
+          --json/--output-schema/-o but NOT --sandbox/-s (help-surface
+          assertion test) — the sandbox must be pinned via
+          -c sandbox_mode="..." on resume.
     notes:
       - >-
         --full-auto does NOT exist on `codex exec` 0.139.0 (PRD §4.1/plan
         mention it): exec is already non-interactive; the sandbox flag alone
-        governs write access. See BOOTSTRAP-NOTES #9.
+        governs write access. Verified by a help-surface assertion test, not
+        a live run. See BOOTSTRAP-NOTES #9.
       - >-
         Cost is never reported in the event stream — tokens-only degraded
         path (PRD §12 Q3) is codex's normal mode.
diff --git a/runs/gauntlet-bootstrap/manual/p1-cycle-r1/triage.json b/runs/gauntlet-bootstrap/manual/p1-cycle-r1/triage.json
index d1eb343..b8cfbfa 100644
--- a/runs/gauntlet-bootstrap/manual/p1-cycle-r1/triage.json
+++ b/runs/gauntlet-bootstrap/manual/p1-cycle-r1/triage.json
@@ -4,7 +4,7 @@
   "reviewer": "codex exec -s read-only --json --output-schema (codex-cli 0.139.0); structured findings, no normalization needed",
   "triager": "claude (bootstrap builder, manual triage pre-P4)",
   "date": "2026-06-10",
-  "status": "AWAITING HUMAN RATIFICATION \u2014 no fixes applied yet",
+  "status": "ratified by human 2026-06-10 ('Fix all'); all 5 fix_now applied in P1.1",
   "verdicts": [
     {
       "finding_id": "F-001",
diff --git a/src/gauntlet/adapters/__init__.py b/src/gauntlet/adapters/__init__.py
index fb03027..808a27e 100644
--- a/src/gauntlet/adapters/__init__.py
+++ b/src/gauntlet/adapters/__init__.py
@@ -13,6 +13,7 @@ from gauntlet.adapters.base import (
     AdapterCapabilities,
     AdapterError,
     AgentAdapter,
+    AgentFailedError,
     AgentResult,
     AgentTimeoutError,
     MalformedOutputError,
@@ -46,6 +47,7 @@ __all__ = [
     "AdapterCapabilities",
     "AdapterError",
     "AgentAdapter",
+    "AgentFailedError",
     "AgentResult",
     "AgentTimeoutError",
     "MalformedOutputError",
diff --git a/src/gauntlet/adapters/base.py b/src/gauntlet/adapters/base.py
index 04d8a8f..9392571 100644
--- a/src/gauntlet/adapters/base.py
+++ b/src/gauntlet/adapters/base.py
@@ -79,6 +79,12 @@ class AgentTimeoutError(AdapterError):
     """The CLI invocation exceeded its hard timeout and was killed (FR-3.3)."""
 
 
+class AgentFailedError(AdapterError):
+    """The CLI ran and produced parseable output, but reported failure
+    (nonzero exit, is_error, turn.failed). Fail closed: a failed call never
+    surfaces as a normal AgentResult (review P1 F-001)."""
+
+
 class MalformedOutputError(AdapterError):
     """Adapter output could not be parsed (or failed schema validation)."""
 
diff --git a/src/gauntlet/adapters/claude_code.py b/src/gauntlet/adapters/claude_code.py
index fa6b74b..2ee5b05 100644
--- a/src/gauntlet/adapters/claude_code.py
+++ b/src/gauntlet/adapters/claude_code.py
@@ -17,6 +17,7 @@ from gauntlet.adapters._structured import extract_json, validate_schema
 from gauntlet.adapters.base import (
     AdapterCapabilities,
     AdapterError,
+    AgentFailedError,
     AgentResult,
     AgentTimeoutError,
     MalformedOutputError,
@@ -119,31 +120,50 @@ class ClaudeCodeAdapter:
     # -- output parsing --------------------------------------------------------
 
     def _parse(self, out: ProcessOutput, *, schema: dict | None) -> AgentResult:
-        events = self._decode_events(out)
+        events = self._decode_events(out, strict=True)
         result_event = next(
             (e for e in reversed(events) if e.get("type") == "result"), None
         )
+        partial = AgentResult(
+            text=(result_event or {}).get("result") or "",
+            session_id=(result_event or {}).get("session_id")
+            or next((e["session_id"] for e in events if e.get("session_id")), None),
+            usage=self._extract_usage(result_event or {}),
+            raw_events=events,
+            exit_code=out.exit_code,
+        )
+        # Fail closed on reported failure, even when output parses (F-001).
+        failure = self._failure_marker(out, result_event)
+        if failure:
+            raise AgentFailedError(
+                f"claude reported failure: {failure}; stderr: {out.stderr[:500]}",
+                partial=partial,
+            )
         if result_event is None:
             raise MalformedOutputError(
                 f"no result event in claude output (exit {out.exit_code}); "
                 f"stderr: {out.stderr[:500]}",
-                partial=self._partial_result(out, events=events),
+                partial=partial,
             )
         text = result_event.get("result") or ""
         structured = self._extract_structured(result_event, text, schema)
-        return AgentResult(
-            text=text,
-            structured=structured,
-            session_id=result_event.get("session_id")
-            or next(
-                (e["session_id"] for e in events if e.get("session_id")), None
-            ),
-            usage=self._extract_usage(result_event),
-            raw_events=events,
-            exit_code=out.exit_code,
-        )
+        return partial.model_copy(update={"text": text, "structured": structured})
 
-    def _decode_events(self, out: ProcessOutput) -> list[dict[str, Any]]:
+    @staticmethod
+    def _failure_marker(out: ProcessOutput, result_event: dict | None) -> str | None:
+        if out.exit_code != 0:
+            return f"exit code {out.exit_code}"
+        if result_event is not None:
+            if result_event.get("is_error"):
+                return f"is_error=true (subtype: {result_event.get('subtype')!r})"
+            subtype = result_event.get("subtype") or ""
+            if subtype.startswith("error"):
+                return f"subtype {subtype!r}"
+        return None
+
+    def _decode_events(
+        self, out: ProcessOutput, *, strict: bool
+    ) -> list[dict[str, Any]]:
         if self.output_format == "json":
             try:
                 obj = json.loads(out.stdout)
@@ -161,7 +181,15 @@ class ClaudeCodeAdapter:
                 continue
             try:
                 events.append(json.loads(line))
-            except json.JSONDecodeError:
+            except json.JSONDecodeError as exc:
+                # Fail closed (F-002): with stream-json, logs go to stderr, so
+                # a non-JSON stdout line means the output contract broke.
+                # Lenient mode is only for building checkpointable partials.
+                if strict:
+                    raise MalformedOutputError(
+                        f"non-JSON line in claude stream-json output: {line[:200]!r}",
+                        partial=self._raw_partial(out),
+                    ) from exc
                 events.append({"type": "gauntlet.unparsed_line", "line": line})
         return events
 
@@ -208,7 +236,7 @@ class ClaudeCodeAdapter:
     ) -> AgentResult:
         if events is None:
             try:
-                events = self._decode_events(out)
+                events = self._decode_events(out, strict=False)
             except AdapterError:
                 events = [{"type": "gauntlet.raw_stdout", "stdout": out.stdout}]
         return AgentResult(
diff --git a/src/gauntlet/adapters/codex.py b/src/gauntlet/adapters/codex.py
index dcd96ba..de5e2e1 100644
--- a/src/gauntlet/adapters/codex.py
+++ b/src/gauntlet/adapters/codex.py
@@ -18,6 +18,7 @@ from typing import Any
 from gauntlet.adapters._structured import validate_schema
 from gauntlet.adapters.base import (
     AdapterCapabilities,
+    AgentFailedError,
     AgentResult,
     AgentTimeoutError,
     MalformedOutputError,
@@ -122,7 +123,14 @@ class CodexAdapter:
     def _parse(
         self, out: ProcessOutput, *, schema: dict | None, last_message: str | None
     ) -> AgentResult:
-        events = self._decode_events(out.stdout)
+        events = self._decode_events(out.stdout, strict=True, out=out)
+        # Fail closed on reported failure, even when output parses (F-001).
+        failure = self._failure_marker(out, events)
+        if failure:
+            raise AgentFailedError(
+                f"codex reported failure: {failure}; stderr: {out.stderr[:500]}",
+                partial=self._partial_result(out),
+            )
         text = last_message if last_message is not None else self._last_agent_message(events)
         if text is None:
             raise MalformedOutputError(
@@ -154,8 +162,9 @@ class CodexAdapter:
             exit_code=out.exit_code,
         )
 
-    @staticmethod
-    def _decode_events(stdout: str) -> list[dict[str, Any]]:
+    def _decode_events(
+        self, stdout: str, *, strict: bool, out: ProcessOutput | None = None
+    ) -> list[dict[str, Any]]:
         events: list[dict[str, Any]] = []
         for line in stdout.splitlines():
             line = line.strip()
@@ -163,10 +172,32 @@ class CodexAdapter:
                 continue
             try:
                 events.append(json.loads(line))
-            except json.JSONDecodeError:
+            except json.JSONDecodeError as exc:
+                # Fail closed (F-002): with --json, codex logs go to stderr,
+                # so a non-JSON stdout line means the event contract broke.
+                # Lenient mode is only for building checkpointable partials.
+                if strict:
+                    raise MalformedOutputError(
+                        f"non-JSON line in codex --json output: {line[:200]!r}",
+                        partial=self._partial_result(out)
+                        if out is not None
+                        else None,
+                    ) from exc
                 events.append({"type": "gauntlet.unparsed_line", "line": line})
         return events
 
+    @staticmethod
+    def _failure_marker(
+        out: ProcessOutput, events: list[dict[str, Any]]
+    ) -> str | None:
+        if out.exit_code != 0:
+            return f"exit code {out.exit_code}"
+        for event in events:
+            if event.get("type") in ("turn.failed", "error"):
+                detail = event.get("error") or event.get("message") or event
+                return f"{event['type']} event: {str(detail)[:300]}"
+        return None
+
     @staticmethod
     def _thread_id(events: list[dict[str, Any]]) -> str | None:
         for event in events:
@@ -202,7 +233,7 @@ class CodexAdapter:
         )
 
     def _partial_result(self, out: ProcessOutput) -> AgentResult:
-        events = self._decode_events(out.stdout)
+        events = self._decode_events(out.stdout, strict=False)
         return AgentResult(
             text=self._last_agent_message(events) or "",
             session_id=self._thread_id(events),
diff --git a/src/gauntlet/config.py b/src/gauntlet/config.py
index 3d97346..4e255fc 100644
--- a/src/gauntlet/config.py
+++ b/src/gauntlet/config.py
@@ -39,6 +39,13 @@ BANNED_FLAG_VALUES: dict[str, frozenset[str]] = {
     "-s": frozenset({"danger-full-access"}),
 }
 
+# codex config overrides (`-c key=value`) that bypass the sandbox the same
+# way the banned flag values do (review P1 F-003: the adapter itself uses
+# `-c sandbox_mode=...` on resume, so this channel is a live control path).
+BANNED_CONFIG_VALUES: dict[str, frozenset[str]] = {
+    "sandbox_mode": frozenset({"danger-full-access"}),
+}
+
 
 def lint_flags(argv: Sequence[str]) -> None:
     """Reject permission-bypass / hook-disabling flags anywhere in ``argv``.
@@ -62,3 +69,20 @@ def lint_flags(argv: Sequence[str]) -> None:
                     f"banned value {effective!r} for {flag!r}: amounts to a "
                     "permission/sandbox bypass (PRD §8)"
                 )
+        if flag in ("-c", "--config"):
+            assignment = value if eq else (tokens[i + 1] if i + 1 < len(tokens) else "")
+            _check_config_assignment(assignment)
+
+
+def _check_config_assignment(assignment: str) -> None:
+    """Reject codex `-c key=value` overrides that bypass the sandbox."""
+    key, eq, value = assignment.partition("=")
+    if not eq:
+        return
+    key = key.strip()
+    value = value.strip().strip("\"'")  # TOML values arrive quoted or bare
+    if key in BANNED_CONFIG_VALUES and value in BANNED_CONFIG_VALUES[key]:
+        raise BannedFlagError(
+            f"banned config override {key}={value!r}: amounts to a sandbox "
+            "bypass (PRD §8)"
+        )
diff --git a/src/gauntlet/logging/redact.py b/src/gauntlet/logging/redact.py
index 01fb16a..368e815 100644
--- a/src/gauntlet/logging/redact.py
+++ b/src/gauntlet/logging/redact.py
@@ -52,6 +52,15 @@ CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
 )
 
 
+def _secret_variants(value: str) -> set[str]:
+    """A secret's raw form plus its JSON-escaped serializations."""
+    return {
+        value,
+        json.dumps(value)[1:-1],  # ensure_ascii=True spelling
+        json.dumps(value, ensure_ascii=False)[1:-1],
+    }
+
+
 @dataclass(frozen=True)
 class RedactionHit:
     """One redaction occurrence: which pattern fired, how many times."""
@@ -74,13 +83,17 @@ class Redactor:
             import os
 
             env = os.environ
-        # Longest values first so overlapping secrets mask fully.
+        # Each secret is matched in its raw form AND its JSON-escaped forms:
+        # writers serialize before redacting, and json.dumps escapes quotes,
+        # backslashes and newlines, so the raw value alone would miss them
+        # (review P1 F-004). Longest variants first so overlaps mask fully.
         self._env_secrets: list[tuple[str, str]] = sorted(
             (
-                (name, value)
+                (name, variant)
                 for name, value in env.items()
                 if SECRET_ENV_NAME_RE.search(name)
                 and len(value) >= min_value_length
+                for variant in _secret_variants(value)
             ),
             key=lambda item: len(item[1]),
             reverse=True,
@@ -90,11 +103,16 @@ class Redactor:
     def redact(self, text: str) -> tuple[str, list[RedactionHit]]:
         """Return ``(redacted_text, hits)``; hits name the patterns that fired."""
         hits: list[RedactionHit] = []
-        for name, value in self._env_secrets:
-            count = text.count(value)
+        env_counts: dict[str, int] = {}
+        for name, variant in self._env_secrets:
+            count = text.count(variant)
             if count:
-                text = text.replace(value, f"[REDACTED:env:{name}]")
-                hits.append(RedactionHit(pattern=f"env:{name}", count=count))
+                text = text.replace(variant, f"[REDACTED:env:{name}]")
+                env_counts[name] = env_counts.get(name, 0) + count
+        hits.extend(
+            RedactionHit(pattern=f"env:{name}", count=count)
+            for name, count in env_counts.items()
+        )
         for pattern_name, pattern in self._patterns:
             text, count = pattern.subn(f"[REDACTED:{pattern_name}]", text)
             if count:
diff --git a/tests/integration/test_codex_contract.py b/tests/integration/test_codex_contract.py
index c60d2fa..4124cf0 100644
--- a/tests/integration/test_codex_contract.py
+++ b/tests/integration/test_codex_contract.py
@@ -58,6 +58,53 @@ def test_resume_continuity(fixture_repo):
     assert "ZIRCON-42" in second.text
 
 
+# F-005 (P1 review round 1): pin-file claims need matching contract tests.
+def test_resume_with_output_schema(fixture_repo):
+    # P4's diff-scoped confirm pass leans on exactly this combination:
+    # exec resume + --output-schema + -o.
+    adapter = CodexAdapter(sandbox="read-only", timeout_s=TIMEOUT_S)
+    first = adapter.run(
+        "The codeword is OBSIDIAN-7. Reply with exactly: OK", cwd=fixture_repo
+    )
+    assert first.session_id
+    schema = {
+        "type": "object",
+        "properties": {"codeword": {"type": "string"}},
+        "required": ["codeword"],
+        "additionalProperties": False,
+    }
+    second = adapter.run(
+        "Report the codeword I gave you earlier via the schema.",
+        session=first.session_id,
+        schema=schema,
+        cwd=fixture_repo,
+    )
+    assert second.structured["codeword"] == "OBSIDIAN-7"
+    assert second.usage is not None
+
+
+def test_help_surface_matches_pin_file():
+    # Backs the pin-file divergence claims with assertions against the
+    # installed binary, not memory of its docs.
+    import subprocess
+
+    exec_help = subprocess.run(
+        ["codex", "exec", "--help"], capture_output=True, text=True, timeout=30
+    ).stdout
+    resume_help = subprocess.run(
+        ["codex", "exec", "resume", "--help"],
+        capture_output=True,
+        text=True,
+        timeout=30,
+    ).stdout
+    assert "--full-auto" not in exec_help  # PRD §4.1 mentions it; 0.139.0 lacks it
+    for flag in ("--json", "--output-schema", "--output-last-message"):
+        assert flag in exec_help, flag
+        assert flag in resume_help, flag
+    assert "--sandbox" in exec_help
+    assert "--sandbox" not in resume_help  # resume re-pins via -c sandbox_mode
+
+
 def test_workspace_write_in_disposable_fixture_repo(fixture_repo):
     # The write-mode sandbox flag is itself under test (plan F-002 carve-out).
     adapter = CodexAdapter(sandbox="workspace-write", timeout_s=TIMEOUT_S)
diff --git a/tests/unit/test_claude_adapter.py b/tests/unit/test_claude_adapter.py
index ac7c89b..9b6797b 100644
--- a/tests/unit/test_claude_adapter.py
+++ b/tests/unit/test_claude_adapter.py
@@ -4,7 +4,11 @@ import json
 
 import pytest
 
-from gauntlet.adapters.base import AgentTimeoutError, MalformedOutputError
+from gauntlet.adapters.base import (
+    AgentFailedError,
+    AgentTimeoutError,
+    MalformedOutputError,
+)
 from gauntlet.adapters.claude_code import ClaudeCodeAdapter
 from gauntlet.adapters.process import ProcessOutput
 
@@ -125,6 +129,44 @@ def test_malformed_output_raises_with_partial(monkeypatch):
     assert any("boom" in str(e) for e in partial.raw_events)
 
 
+# F-001 (P1 review round 1): parseable output must not mask a failed call.
+def test_is_error_result_raises(monkeypatch):
+    event = dict(RESULT_EVENT)
+    event["is_error"] = True
+    event["subtype"] = "error_during_execution"
+    patch_run(monkeypatch, fake_output(json.dumps(event)))
+    with pytest.raises(AgentFailedError, match="is_error") as excinfo:
+        ClaudeCodeAdapter().run("ping")
+    partial = excinfo.value.partial
+    assert partial.session_id == RESULT_EVENT["session_id"]
+    assert partial.text == "GAUNTLET_PONG"  # evidence preserved
+
+
+def test_error_subtype_raises(monkeypatch):
+    event = dict(RESULT_EVENT)
+    event["subtype"] = "error_max_turns"
+    patch_run(monkeypatch, fake_output(json.dumps(event)))
+    with pytest.raises(AgentFailedError, match="error_max_turns"):
+        ClaudeCodeAdapter().run("ping")
+
+
+def test_nonzero_exit_with_parseable_output_raises(monkeypatch):
+    patch_run(monkeypatch, fake_output(json.dumps(RESULT_EVENT), exit_code=1))
+    with pytest.raises(AgentFailedError, match="exit code 1") as excinfo:
+        ClaudeCodeAdapter().run("ping")
+    assert excinfo.value.partial.usage.input_tokens == 14
+
+
+# F-002: stream-json is a JSONL contract; a non-JSON stdout line fails closed.
+def test_stream_json_garbage_line_fails_closed(monkeypatch):
+    stdout = "\n".join(
+        [json.dumps({"type": "system", "session_id": "abc"}), "garbage here"]
+    )
+    patch_run(monkeypatch, fake_output(stdout))
+    with pytest.raises(MalformedOutputError, match="non-JSON line"):
+        ClaudeCodeAdapter(output_format="stream-json").run("ping")
+
+
 def test_missing_result_event_raises(monkeypatch):
     patch_run(
         monkeypatch,
diff --git a/tests/unit/test_codex_adapter.py b/tests/unit/test_codex_adapter.py
index 748b0ca..617d9ed 100644
--- a/tests/unit/test_codex_adapter.py
+++ b/tests/unit/test_codex_adapter.py
@@ -9,7 +9,11 @@ from pathlib import Path
 
 import pytest
 
-from gauntlet.adapters.base import AgentTimeoutError, MalformedOutputError
+from gauntlet.adapters.base import (
+    AgentFailedError,
+    AgentTimeoutError,
+    MalformedOutputError,
+)
 from gauntlet.adapters.codex import CodexAdapter
 from gauntlet.adapters.process import ProcessOutput
 
@@ -121,14 +125,28 @@ def test_schema_violation_raises_with_partial(monkeypatch):
 
 
 def test_no_agent_message_raises(monkeypatch):
+    # exit 0 so the F-001 failure check doesn't fire first; a clean exit
+    # with no agent message is a malformed-output case
     events = [{"type": "thread.started", "thread_id": THREAD_ID}]
-    patch_run(monkeypatch, fake_output(events, exit_code=1, stderr="auth error"))
+    patch_run(monkeypatch, fake_output(events, exit_code=0))
     with pytest.raises(MalformedOutputError) as excinfo:
         CodexAdapter().run("hi")
+    assert excinfo.value.partial.exit_code == 0
+
+
+def test_no_agent_message_with_nonzero_exit_raises_agent_failed(monkeypatch):
+    # F-001 ratified: nonzero exit takes precedence over the missing message
+    events = [{"type": "thread.started", "thread_id": THREAD_ID}]
+    patch_run(monkeypatch, fake_output(events, exit_code=1, stderr="auth error"))
+    with pytest.raises(AgentFailedError, match="exit code 1") as excinfo:
+        CodexAdapter().run("hi")
     assert excinfo.value.partial.exit_code == 1
 
 
-def test_unparsed_lines_are_tolerated(monkeypatch):
+# Behavior ratified in P1 review round 1 (F-002): non-JSON stdout lines were
+# previously tolerated; with --json the event contract puts logs on stderr,
+# so they now fail closed.
+def test_unparsed_lines_fail_closed(monkeypatch):
     out = fake_output(make_events("ok"))
     noisy = ProcessOutput(
         argv=out.argv,
@@ -139,9 +157,37 @@ def test_unparsed_lines_are_tolerated(monkeypatch):
         timed_out=False,
     )
     patch_run(monkeypatch, noisy)
-    result = CodexAdapter().run("hi")
-    assert result.text == "ok"
-    assert result.raw_events[0]["type"] == "gauntlet.unparsed_line"
+    with pytest.raises(MalformedOutputError) as excinfo:
+        CodexAdapter().run("hi")
+    # the checkpointable partial preserves the offending line and the events
+    partial = excinfo.value.partial
+    assert partial is not None
+    assert any(
+        e.get("type") == "gauntlet.unparsed_line" and "WARN" in e.get("line", "")
+        for e in partial.raw_events
+    )
+    assert partial.session_id == THREAD_ID
+
+
+def test_turn_failed_event_raises(monkeypatch):
+    events = [
+        {"type": "thread.started", "thread_id": THREAD_ID},
+        {"type": "turn.failed", "error": {"message": "rate limited"}},
+    ]
+    patch_run(monkeypatch, fake_output(events, exit_code=0))
+    with pytest.raises(AgentFailedError, match="turn.failed") as excinfo:
+        CodexAdapter().run("hi")
+    assert excinfo.value.partial.session_id == THREAD_ID
+
+
+def test_nonzero_exit_with_parseable_output_raises(monkeypatch):
+    # F-001: a parseable stream must not mask a failed invocation
+    patch_run(monkeypatch, fake_output(make_events("looks fine"), exit_code=2))
+    with pytest.raises(AgentFailedError, match="exit code 2") as excinfo:
+        CodexAdapter().run("hi")
+    partial = excinfo.value.partial
+    assert partial.text == "looks fine"  # evidence preserved for checkpointing
+    assert partial.usage.input_tokens == 21497
 
 
 def test_resume_argv(monkeypatch):
diff --git a/tests/unit/test_flag_lint.py b/tests/unit/test_flag_lint.py
index 13ee234..38e6cb1 100644
--- a/tests/unit/test_flag_lint.py
+++ b/tests/unit/test_flag_lint.py
@@ -21,6 +21,11 @@ from gauntlet.config import BannedFlagError, lint_flags
         ["codex", "exec", "--sandbox", "danger-full-access"],
         ["codex", "exec", "-s", "danger-full-access"],
         ["codex", "exec", "-s=danger-full-access"],
+        # F-003: the -c/--config override channel must not bypass the sandbox
+        ["codex", "exec", "-c", 'sandbox_mode="danger-full-access"'],
+        ["codex", "exec", "-c", "sandbox_mode=danger-full-access"],
+        ["codex", "exec", "-c=sandbox_mode=danger-full-access"],
+        ["codex", "exec", "--config", 'sandbox_mode="danger-full-access"'],
     ],
 )
 def test_banned_argv_rejected(argv):
@@ -42,6 +47,10 @@ def test_benign_argv_passes():
         ]
     )
     lint_flags(["codex", "exec", "--json", "--sandbox", "read-only", "-"])
+    # benign config overrides pass, including the adapter's own resume spelling
+    lint_flags(["codex", "exec", "-c", 'sandbox_mode="read-only"'])
+    lint_flags(["codex", "exec", "-c", 'model="o3"'])
+    lint_flags(["codex", "exec", "-c"])  # dangling -c is the CLI's problem
 
 
 def test_claude_adapter_rejects_banned_base_flags():
diff --git a/tests/unit/test_redact.py b/tests/unit/test_redact.py
index 932a4cd..037a163 100644
--- a/tests/unit/test_redact.py
+++ b/tests/unit/test_redact.py
@@ -103,6 +103,35 @@ def test_writer_append_jsonl_redacts_values(tmp_path):
     assert json.loads(lines[1]) == {"cmd": "clean"}
 
 
+# F-004 (P1 review round 1): writers serialize before redacting, and
+# json.dumps escapes quotes/backslashes/newlines — the escaped form must
+# also be masked.
+def test_json_escaped_secret_variants_masked(tmp_path):
+    secrets = {
+        "QUOTE_TOKEN": 'abc"def"ghi12345',
+        "BACKSLASH_TOKEN": "abc\\def\\ghi12345",
+        "NEWLINE_TOKEN": "abc\ndef\nghi12345",
+    }
+    for name, value in secrets.items():
+        writer = RedactingWriter(Redactor(env={name: value}))
+        target = tmp_path / f"{name}.jsonl"
+        writer.append_jsonl(target, {"payload": value})
+        line = target.read_text()
+        assert "def" not in line, name  # no fragment of the secret survives
+        assert json.loads(line)["payload"] == f"[REDACTED:env:{name}]"
+
+
+def test_escaped_variant_hits_aggregate_per_env_name():
+    secret = 'top"secret"value99'
+    r = Redactor(env={"MY_KEY": secret})
+    # raw and JSON-escaped forms in the same text -> one aggregated hit entry
+    text, hits = r.redact(f"raw={secret} escaped={json.dumps(secret)}")
+    assert secret not in text
+    assert '\\"' not in text
+    assert len([h for h in hits if h.pattern == "env:MY_KEY"]) == 1
+    assert hits[0].count == 2
+
+
 def test_writer_no_hits_for_clean_text(tmp_path):
     writer = RedactingWriter(redactor())
     hits = writer.write_text(tmp_path / "clean.md", "nothing secret here")

```
