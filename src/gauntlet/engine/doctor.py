"""``gauntlet doctor`` — environment validation (FR-1.3, FR-1.5).

Validates that a clone is ready to run a pipeline: the agent CLIs are installed
(and their versions match what the pin file verified), the judge is startable,
the hook wiring is in place, and an ApiAdapter credential is present in the
environment (never in repo config — FR-1.4). Each check yields an actionable
:class:`CheckResult`; the CLI prints them and exits non-zero if any FAILs.

The checks take their environment through an injectable :class:`DoctorProbes`
so the unit suite can simulate broken environments (missing CLI, stale version,
absent hooks, missing key) without touching the real machine — the live CLI
probes only run when the real default probes are used.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gauntlet import __version__
from gauntlet.engine.init import HOOK_COMMAND
from gauntlet.pins import PinFile, load_pins, pin_file_path

OK = "ok"
WARN = "warn"
FAIL = "fail"

# The two agent CLIs doctor checks for and pins.
AGENT_CLIS = ("claude", "codex")

# model-name prefix -> the env var that must carry its key (FR-1.4). Matched
# case-insensitively against the start of the configured model name.
_KEY_BY_PREFIX = {
    "gpt": "OPENAI_API_KEY",
    "o1": "OPENAI_API_KEY",
    "o3": "OPENAI_API_KEY",
    "o4": "OPENAI_API_KEY",
    "openai/": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "anthropic/": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google/": "GEMINI_API_KEY",
}

# Credential literals that must never appear in committed repo config (FR-1.4).
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),       # OpenAI / Anthropic style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),            # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),      # Google API key
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str          # OK | WARN | FAIL
    detail: str
    remedy: str | None = None


@dataclass(frozen=True)
class DoctorProbes:
    """Injectable environment access so checks are testable offline."""

    # name -> version string (e.g. "2.1.172"), or None if the CLI is absent.
    cli_version: Callable[[str], str | None]
    env: Mapping[str, str]


def _real_cli_version(name: str) -> str | None:
    """Best-effort `<cli> --version`; None if the binary is not on PATH."""
    if shutil.which(name) is None:
        return None
    try:
        out = subprocess.run(
            [name, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr or "").strip()
    return text or None


def real_probes() -> DoctorProbes:
    return DoctorProbes(cli_version=_real_cli_version, env=os.environ)


def _extract_version(text: str | None) -> str | None:
    """Pull a dotted version out of a `--version` line (`codex-cli 0.139.0`)."""
    if not text:
        return None
    m = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    return m.group(0) if m else text


def _pin_version(pins: PinFile | None, cli: str) -> str | None:
    if pins is None or cli not in pins.clis:
        return None
    return _extract_version(pins.clis[cli].version)


# ---- individual checks ------------------------------------------------------

def _check_version() -> CheckResult:
    return CheckResult("gauntlet", OK, f"version {__version__}")


def _check_cli(cli: str, probes: DoctorProbes, pins: PinFile | None) -> CheckResult:
    raw = probes.cli_version(cli)
    if raw is None:
        return CheckResult(
            cli, FAIL, "not found on PATH",
            remedy=f"install the {cli!r} CLI and authenticate it",
        )
    found = _extract_version(raw)
    pinned = _pin_version(pins, cli)
    detail = f"{raw}"
    if pinned and found and found != pinned:
        return CheckResult(
            cli, WARN,
            f"installed {found} != pin-verified {pinned}",
            remedy=(
                f"hook/flag behavior was verified against {cli} {pinned}; "
                "re-run the contract suite and update .gauntlet/pins.yaml if it "
                "still passes (FR-1.5)"
            ),
        )
    return CheckResult(cli, OK, detail + (f" (matches pin {pinned})" if pinned else ""))


def _check_claude_hook(repo_root: Path) -> CheckResult:
    path = repo_root / ".claude" / "settings.json"
    rel = ".claude/settings.json"
    if not path.exists():
        return CheckResult(
            "claude-hook", FAIL, f"{rel} missing",
            remedy="run `gauntlet init` to wire the PreToolUse judge hook",
        )
    if HOOK_COMMAND not in path.read_text():
        return CheckResult(
            "claude-hook", FAIL, f"{rel} does not wire {HOOK_COMMAND}",
            remedy="run `gauntlet init` to add the PreToolUse judge hook",
        )
    return CheckResult("claude-hook", OK, f"PreToolUse → {HOOK_COMMAND}")


def _check_codex_hook(repo_root: Path, pins: PinFile | None) -> CheckResult:
    path = repo_root / ".codex" / "hooks.json"
    rel = ".codex/hooks.json"
    if not path.exists():
        return CheckResult(
            "codex-hook", WARN, f"{rel} missing",
            remedy="run `gauntlet init` to write the (forward-looking) codex hooks config",
        )
    # Sandbox-primary on the pinned build: codex exec does not fire exec hooks,
    # so a present-but-inert config is healthy, not a failure (BOOTSTRAP-NOTES
    # #10). Doctor reports the firing status from the pin rather than asserting
    # a hook that cannot fire.
    note = "present"
    if pins and "codex" in pins.clis:
        joined = " ".join(pins.clis["codex"].notes).lower()
        if "never fire" in joined or "does not fire" in joined:
            note = "present (inert on pinned codex; sandbox-primary control — FR-7.3)"
    return CheckResult("codex-hook", OK, note)


def _check_judge(repo_root: Path) -> CheckResult:
    policy = repo_root / "policy.yaml"
    if not policy.exists():
        return CheckResult(
            "judge", FAIL, "policy.yaml missing",
            remedy="run `gauntlet init` to scaffold the judge fast-path policy",
        )
    try:
        from gauntlet.judge.policy import Policy

        Policy.load(policy)
    except Exception as exc:  # parse / schema fault
        return CheckResult(
            "judge", FAIL, f"policy.yaml does not load: {exc}",
            remedy="fix the YAML / regex in policy.yaml (FR-7.6)",
        )
    return CheckResult("judge", OK, "policy.yaml loads; judge startable")


def _api_models(config) -> list[str]:
    return [
        p.model
        for p in config.agents.values()
        if p.adapter == "api" and p.model
    ]


def _required_key(model: str) -> str | None:
    low = model.lower()
    for prefix, var in _KEY_BY_PREFIX.items():
        if low.startswith(prefix):
            return var
    return None


def _check_api_keys(config, probes: DoctorProbes) -> CheckResult:
    models = _api_models(config)
    if not models:
        return CheckResult("api-keys", WARN, "no `api` adapter profiles configured")
    required: dict[str, set[str]] = {}     # env var -> models needing it
    unknown: list[str] = []
    for model in models:
        var = _required_key(model)
        if var is None:
            unknown.append(model)
        else:
            required.setdefault(var, set()).add(model)
    have = {v for v in required if probes.env.get(v)}
    missing = sorted(set(required) - have)
    if required and not have:
        return CheckResult(
            "api-keys", FAIL,
            f"no API key present for any configured api model (need one of: "
            f"{', '.join(sorted(required))})",
            remedy="export the key in your shell / keychain, never in repo config (FR-1.4)",
        )
    detail = f"present: {', '.join(sorted(have))}" if have else "none required"
    if missing:
        return CheckResult(
            "api-keys", WARN,
            f"{detail}; missing {', '.join(missing)} "
            f"(needed by {', '.join(sorted(m for v in missing for m in required[v]))})",
            remedy="export the missing key(s) before using those profiles (FR-1.4)",
        )
    if unknown:
        return CheckResult(
            "api-keys", WARN,
            f"{detail}; could not infer the key var for model(s): {', '.join(unknown)}",
        )
    return CheckResult("api-keys", OK, detail)


def _check_repo_secrets(repo_root: Path) -> CheckResult:
    """FR-1.4: no credential literal may live in committed repo config."""
    offenders: list[str] = []
    for rel in (".gauntlet/config.yaml", "policy.yaml", "pipelines/standard.yaml"):
        path = repo_root / rel
        if not path.exists():
            continue
        text = path.read_text()
        if any(p.search(text) for p in _SECRET_PATTERNS):
            offenders.append(rel)
    if offenders:
        return CheckResult(
            "repo-secrets", FAIL,
            f"credential-shaped literal found in {', '.join(offenders)}",
            remedy="remove the secret; keys live in env/keychain only (FR-1.4)",
        )
    return CheckResult("repo-secrets", OK, "no credential literals in repo config")


def _check_pin_file(repo_root: Path, pins: PinFile | None) -> CheckResult:
    if pins is None:
        return CheckResult(
            "pin-file", WARN, f"{pin_file_path(repo_root)} missing or unreadable",
            remedy="run the contract suite to regenerate .gauntlet/pins.yaml (FR-1.5)",
        )
    return CheckResult(
        "pin-file", OK,
        f"verified {pins.verified_date}; clis: {', '.join(sorted(pins.clis))}",
    )


# ---- aggregation ------------------------------------------------------------

def run_doctor(
    repo_root: Path, *, probes: DoctorProbes | None = None
) -> list[CheckResult]:
    """Run every environment check; returns results in display order."""
    probes = probes or real_probes()

    try:
        pins = load_pins(pin_file_path(repo_root))
    except Exception:
        pins = None

    results = [_check_version()]
    for cli in AGENT_CLIS:
        results.append(_check_cli(cli, probes, pins))
    results.append(_check_claude_hook(repo_root))
    results.append(_check_codex_hook(repo_root, pins))
    results.append(_check_judge(repo_root))
    results.append(_check_repo_secrets(repo_root))

    # Config-dependent checks: load it once, and surface a clean FAIL if absent.
    try:
        from gauntlet.engine.config import RunConfig

        config = RunConfig.load(repo_root / ".gauntlet" / "config.yaml")
    except Exception as exc:
        results.append(CheckResult(
            "config", FAIL, f".gauntlet/config.yaml not loadable: {exc}",
            remedy="run `gauntlet init` to scaffold a config",
        ))
    else:
        results.append(_check_api_keys(config, probes))
    results.append(_check_pin_file(repo_root, pins))
    return results


def has_failure(results: list[CheckResult]) -> bool:
    return any(r.status == FAIL for r in results)
