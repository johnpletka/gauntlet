"""Tiny, deterministic expression evaluator for `when:` / `foreach:` (FR-5.4).

Deliberately *not* a Python ``eval``: the orchestrator is a state machine and
must stay inspectable and safe (plan §2 — determinism over cleverness). Only two
shapes are supported, resolved against a plain context dict:

* ``foreach``: a dotted path that must resolve to a list (e.g. ``plan.phases``,
  ``vars.items``).
* ``when``: a dotted path (truthy), optionally ``not <path>``, or
  ``<path> == <literal>`` / ``<path> != <literal>``.
"""

from __future__ import annotations

from typing import Any

_MISSING = object()


def _resolve_path(path: str, context: dict[str, Any]) -> Any:
    node: Any = context
    for part in path.strip().split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            node = getattr(node, part, _MISSING)
        if node is _MISSING:
            return _MISSING
    return node


def resolve_list(expr: str, context: dict[str, Any]) -> list[Any]:
    value = _resolve_path(expr, context)
    if value is _MISSING:
        raise ValueError(f"foreach source {expr!r} does not resolve")
    if not isinstance(value, list):
        raise ValueError(
            f"foreach source {expr!r} resolved to {type(value).__name__}, not a list"
        )
    return value


def _parse_literal(token: str) -> Any:
    token = token.strip()
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    if (token.startswith('"') and token.endswith('"')) or (
        token.startswith("'") and token.endswith("'")
    ):
        return token[1:-1]
    try:
        return int(token)
    except ValueError:
        return token


def eval_when(expr: str | None, context: dict[str, Any]) -> bool:
    if expr is None:
        return True
    expr = expr.strip()
    for op in ("==", "!="):
        if op in expr:
            lhs, rhs = expr.split(op, 1)
            left = _resolve_path(lhs.strip(), context)
            right = _parse_literal(rhs)
            left = None if left is _MISSING else left
            return (left == right) if op == "==" else (left != right)
    if expr.startswith("not "):
        value = _resolve_path(expr[4:].strip(), context)
        return not (value if value is not _MISSING else None)
    value = _resolve_path(expr, context)
    return bool(value if value is not _MISSING else None)
