"""Every git call in the engine is routed to the right root (P7a, spike §9).

`repo_root` has meant three things at once since the bootstrap — the git
repository, the tree the agent edits, and where run artifacts live. While a run
drives the operator's own checkout the three are the same path, so passing the
wrong one is *completely invisible*: the operation succeeds, against a tree that
happens to be correct. The moment P7c gives a run its own worktree they diverge,
and the same call silently observes or commits the OPERATOR's tree instead —
which is the exact failure P7 exists to make structurally impossible.

A hand audit cannot hold that line: there are 200+ call sites, and every new one
is another chance to reach for `repo_root` out of habit. So the classification
lives in `gitops.ROOT_SCOPE` as data and this test enforces it against the
source, with two failure modes that both matter:

* a WORK-scoped helper handed something that is not a work-tree root — the
  F-003 class of defect;
* a helper missing from the table — so the classification cannot rot silently
  as helpers are added.

This is a static check by design. A runtime test cannot see the difference while
`work_root == repo_root`, which is precisely why the defect class survived the
existing 2700-test suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gauntlet.engine import gitops

ENGINE = Path(gitops.__file__).parent
SOURCES = sorted(p for p in ENGINE.glob("*.py") if p.name != "gitops.py")

# Expressions that NAME A TREE EXPLICITLY. The rule this test enforces is not
# "always use work_root" — some operations act on the operator's checkout by
# design (`finish` merges the run branch into the operator's base; `clean`
# deletes it from there). The rule is that the AMBIGUOUS name is banned: a
# work-scoped call may not take `repo_root`, because that name means "the git
# repository" and reveals nothing about which tree the caller intended.
#
# Every work-scoped call must therefore say one of:
#   *work_root*     — the tree this run's agents edit and the engine commits in
#   *operator_root* — the operator's own checkout, deliberately
# so a reviewer can grep for `operator_root` and audit exactly the sites that
# reach into the human's tree on purpose.
WORK_ROOT_NAMES = {
    # the run's tree
    "work_root",
    "self.work_root",
    "ctx.work_root",
    "copy.path",          # verifier disposable copy — a throwaway work tree
    "worktree",           # collector enumeration target
    "linked",             # test/lab worktrees
    # the operator's tree, named deliberately
    "operator_root",
    "self.operator_root",
    "ctx.operator_root",
}

# Locals that are conventionally bound from a work root inside a function; the
# resolver below proves the binding rather than trusting the name.
_RESOLVABLE = {"repo", "work", "tree", "root"}

# Modules whose git calls operate on the OPERATOR's checkout by design, with the
# reason recorded. An exemption is a decision, not a silence: each names why the
# operator's tree is the correct target, and `test_no_stale_exemptions` fails if
# a module stops needing its exemption, so the list cannot outlive its reason.
EXEMPT_MODULES: dict[str, str] = {
    "review.py": (
        "`gauntlet review` is ratified OUT OF SCOPE for P7 (spike §14.3): it "
        "reviews the operator's own branch and lands REVIEW.x fix commits on it "
        "in place, so the operator's checkout IS its work tree. Giving it a "
        "private worktree would put the fixes somewhere the operator cannot "
        "see, inverting the verb's purpose."
    ),
    "reviewpr.py": (
        "PR checkout resolution for a review run — same scope decision as "
        "review.py (spike §14.3)."
    ),
    "proposals.py": (
        "The governed proposal apply (FR-6.4) patches ASSETS (prompts, policy) "
        "under asset_root in the operator's checkout, driven by the operator "
        "verb `gauntlet proposals review`. There is no run worktree in play."
    ),
}


def _expr(node: ast.AST) -> str:
    """`a.b.c` / `name` for the simple expressions these call sites use."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_expr(node.value)}.{node.attr}"
    return f"<{type(node).__name__}>"


class _Scan(ast.NodeVisitor):
    """Collect (function, lineno, helper, first-arg-expression) per call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str, str]] = []
        self.assigns: dict[str, dict[str, str]] = {}
        self._fn: list[str] = ["<module>"]

    def _enter(self, node) -> None:
        self._fn.append(node.name)
        self.assigns.setdefault(self._fn[-1], {})
        self.generic_visit(node)
        self._fn.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter

    def visit_Assign(self, node: ast.Assign) -> None:
        # Track `x = <expr>` so a local rebinding of a work root resolves.
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self.assigns.setdefault(self._fn[-1], {})[
                node.targets[0].id
            ] = _expr(node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "gitops"
            and node.args
        ):
            self.calls.append(
                (self._fn[-1], node.lineno, func.attr, _expr(node.args[0]))
            )
        self.generic_visit(node)


def _resolve(scan: _Scan, fn: str, expr: str) -> str:
    """Follow simple local rebindings (`repo = ctx.work_root`) to their source."""
    seen = set()
    while expr in _RESOLVABLE and expr not in seen:
        seen.add(expr)
        nxt = scan.assigns.get(fn, {}).get(expr)
        if nxt is None or nxt == expr:
            break
        expr = nxt
    return expr


def _scan(path: Path) -> _Scan:
    scan = _Scan()
    scan.visit(ast.parse(path.read_text()))
    return scan


def test_every_gitops_helper_is_classified():
    """A helper with no ROOT_SCOPE entry is an unreviewed root decision."""
    public = {
        node.name
        for node in ast.parse((ENGINE / "gitops.py").read_text()).body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    missing = sorted(public - set(gitops.ROOT_SCOPE))
    assert not missing, (
        "these gitops helpers are not classified in ROOT_SCOPE — decide whether "
        "each operates on a WORKING TREE, the REPOSITORY, or the COMMON git "
        f"dir, and record it: {missing}"
    )
    stale = sorted(set(gitops.ROOT_SCOPE) - public)
    assert not stale, f"ROOT_SCOPE names helpers that no longer exist: {stale}"


def test_scope_values_are_valid():
    valid = {
        gitops.ROOT_SCOPE_WORK,
        gitops.ROOT_SCOPE_REPO,
        gitops.ROOT_SCOPE_COMMON,
    }
    bad = {k: v for k, v in gitops.ROOT_SCOPE.items() if v not in valid}
    assert not bad, f"invalid ROOT_SCOPE values: {bad}"


def _violations(source: Path) -> list[str]:
    scan = _scan(source)
    out = []
    for fn, lineno, helper, arg in scan.calls:
        if gitops.ROOT_SCOPE.get(helper) != gitops.ROOT_SCOPE_WORK:
            continue
        resolved = _resolve(scan, fn, arg)
        if resolved in WORK_ROOT_NAMES:
            continue
        out.append(
            f"  {source.name}:{lineno} in {fn}(): "
            f"gitops.{helper}({arg}"
            + (f" -> {resolved}" if resolved != arg else "")
            + ")"
        )
    return out


def test_no_stale_exemptions():
    """An exemption that no longer has violations must be deleted, not kept.

    Otherwise the list becomes a place for future violations to hide behind a
    reason that was true once.
    """
    stale = [
        name
        for name in EXEMPT_MODULES
        if not _violations(ENGINE / name)
    ]
    assert not stale, (
        "these modules are exempt in EXEMPT_MODULES but no longer contain any "
        f"work-scoped call on a non-work root — drop the exemption: {stale}"
    )


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_work_scoped_git_calls_take_a_work_root(source):
    """No WORK-scoped helper may be handed a non-work-tree root.

    The failure message names the call site so the fix is mechanical: route it
    through `work_root` (the tree the agent edits), or — if the call really is
    repository-scoped — reclassify the helper in `gitops.ROOT_SCOPE` with a
    reason.
    """
    if source.name in EXEMPT_MODULES:
        pytest.skip(f"exempt: {EXEMPT_MODULES[source.name]}")
    violations = _violations(source)
    assert not violations, (
        f"work-scoped git calls handed a non-work root in {source.name}:\n"
        + "\n".join(violations)
        + "\n\nEach observes or mutates a WORKING TREE. While work_root == "
        "repo_root these pass against the right tree by accident; once a run "
        "has its own worktree they would act on the operator's checkout "
        "instead (spike §9 / review F-003)."
    )
