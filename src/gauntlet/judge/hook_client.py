"""PreToolUse hook client (FR-7.3, FR-7.4; review F-004 operational model).

Invoked by the CLI per tool call: reads the hook payload as JSON on stdin,
asks the judge `/decide`, and emits the CLI's permission-decision contract.
Pure stdlib (no pydantic/httpx import) so per-call startup stays cheap and the
client works even if the package's heavier deps are unavailable.

Degraded behavior when the judge is unreachable (review F-004):
- ``GAUNTLET_JUDGE_MODE=unattended`` (default): fail closed -> deny.
- ``GAUNTLET_JUDGE_MODE=interactive``: emit ``ask`` with a loud warning so the
  human at the keyboard becomes the backstop instead of the session
  deadlocking. A judge *deny* always denies, in both modes.

Output contract (claude + codex share it): print a
``hookSpecificOutput.permissionDecision`` JSON object on stdout, and exit 0 for
allow/ask. For deny we ALSO exit 2 with the reason on stderr, covering the
exit-code-2 path both CLIs honor even if JSON parsing differs.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TOKEN_ENV_VAR = "GAUNTLET_JUDGE_TOKEN"
URL_ENV_VAR = "GAUNTLET_JUDGE_URL"
MODE_ENV_VAR = "GAUNTLET_JUDGE_MODE"
RUN_ID_ENV_VAR = "GAUNTLET_RUN_ID"
STEP_ID_ENV_VAR = "GAUNTLET_STEP_ID"
DEFAULT_URL = "http://127.0.0.1:8787"
HOOK_TIMEOUT_S = 8.0


def _emit(decision: str, reason: str) -> int:
    """Write the permission-decision contract; return the process exit code."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,  # allow | deny | ask
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))
    if decision == "deny":
        print(reason, file=sys.stderr)
        return 2  # exit-2 deny path, honored by both CLIs
    return 0


def _ask_judge(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{url}/decide",
        data=data,
        headers={"Content-Type": "application/json", "X-Gauntlet-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HOOK_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def decide_from_payload(payload, env: dict | None = None) -> tuple[str, str, int]:
    """Pure decision logic for one hook payload. Returns (decision, reason, exit)."""
    env = env if env is not None else os.environ
    if not isinstance(payload, dict):
        # Malformed shape (e.g. a JSON list): fail closed (review F-003).
        return "deny", "hook payload was not a JSON object; failing closed", 2

    url = env.get(URL_ENV_VAR, DEFAULT_URL)
    token = env.get(TOKEN_ENV_VAR, "")
    mode = env.get(MODE_ENV_VAR, "unattended")

    # Safe-by-default (review F-006): with no judge token configured we are not
    # running under a gauntlet judge, so defer to the CLI's own permission
    # handling (ask) rather than denying — a plain session in a repo whose
    # settings wire this hook must not be bricked. A judge is only treated as
    # "should be up" when a token is present.
    if not token:
        return (
            "ask",
            "no gauntlet judge configured (GAUNTLET_JUDGE_TOKEN unset); "
            "deferring to normal permission handling",
            0,
        )

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    repo_root = payload.get("cwd") or env.get("GAUNTLET_REPO_ROOT") or os.getcwd()

    body = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "repo_root": repo_root,
        "run_id": env.get(RUN_ID_ENV_VAR),
        "step_id": env.get(STEP_ID_ENV_VAR),
    }
    try:
        result = _ask_judge(url, token, body)
    except urllib.error.HTTPError as exc:
        # The judge answered with an HTTP error (401 foreign/bad token, 5xx
        # decision fault): fail closed in BOTH modes — this is not a liveness
        # failure, so it must not degrade to ask (review F-002).
        return (
            "deny",
            f"judge returned HTTP {exc.code}; failing closed",
            2,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # Genuine liveness failure (connection refused / timeout): distinguish
        # from a judge deny (review F-004). Interactive falls back to a prompt;
        # unattended fails closed.
        if mode == "interactive":
            return (
                "ask",
                f"⚠ GAUNTLET JUDGE UNREACHABLE ({exc}). Falling back to a "
                "normal permission prompt — you are the backstop. Restart it "
                "with `gauntlet judge serve`.",
                0,
            )
        return (
            "deny",
            f"judge unreachable and mode is unattended; failing closed ({exc})",
            2,
        )
    except ValueError as exc:
        # Response body was not valid JSON: malformed, fail closed.
        return "deny", f"judge response was unparseable; failing closed ({exc})", 2

    if not isinstance(result, dict):
        return "deny", "judge response was not a JSON object; failing closed", 2
    decision = result.get("decision", "deny")
    reason = result.get("rationale") or f"judge decision: {decision}"
    if decision not in ("allow", "deny", "ask"):
        decision, reason = "deny", f"judge returned invalid decision {decision!r}; failing closed"
    exit_code = 2 if decision == "deny" else 0
    return decision, reason, exit_code


def main(argv: list[str] | None = None) -> int:
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            # Can't parse the hook payload: fail closed (PRD §8).
            return _emit("deny", "hook payload was not valid JSON; failing closed")
        decision, reason, _ = decide_from_payload(payload)
        return _emit(decision, reason)
    except Exception as exc:  # any unexpected fault must fail closed (F-003)
        return _emit("deny", f"hook client error; failing closed: {exc}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
