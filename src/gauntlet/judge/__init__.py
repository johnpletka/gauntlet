"""Safety judge: localhost service, policy fast path, hook clients (FR-7).

Heavy members (Policy/PolicyEngine pull in pydantic + yaml) are exposed
lazily via module ``__getattr__`` so importing the stdlib-only hook client
(`gauntlet.judge.hook_client`) does not pay for them on every tool call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # for type checkers only; no runtime import cost
    from gauntlet.judge.decision import Decision, JudgeDecision, Source
    from gauntlet.judge.policy import Policy, PolicyEngine, PolicyRule

_LAZY = {
    "Decision": "gauntlet.judge.decision",
    "JudgeDecision": "gauntlet.judge.decision",
    "Source": "gauntlet.judge.decision",
    "Policy": "gauntlet.judge.policy",
    "PolicyEngine": "gauntlet.judge.policy",
    "PolicyRule": "gauntlet.judge.policy",
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)
