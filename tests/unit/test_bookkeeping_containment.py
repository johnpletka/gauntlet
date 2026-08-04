"""Declared containment for the bookkeeping-path builders (P7a, spike §9.3).

Every builder in ``execution.py`` answers "what is this path, relative to the
tree the engine commits in?", and every one of them used to swallow the failure
and return a quietly shorter list. An empty result is not neutral:

* an empty ``engine_bookkeeping_candidates`` makes
  ``gitops.advance_is_engine_bookkeeping`` classify *nothing* as bookkeeping, so
  every resume of an interrupted step re-parks — the #62/#65 regression;
* an empty ``run_bookkeeping_paths`` makes the manifest-checkpoint commit a
  silent no-op, dropping the FR-2.2 audit trail with no diagnostic;
* an empty ``governed_artifact_paths`` means an approved-artifact edit inside a
  rewind range is never flagged, degrading R9 from "never silently adopt" to
  "never notice".

Being outside the tree is nevertheless correct for exactly one caller — a
``gauntlet review`` run keeps its state out-of-repo on purpose — so these tests
pin BOTH halves of the contract: uncontained-and-undeclared fails closed,
uncontained-and-declared returns empty by design. Testing only the raising half
would let a fix that breaks review runs pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.engine.execution import (
    StateDirNotContained,
    engine_bookkeeping_candidates,
    governed_artifact_paths,
    run_bookkeeping_excludes,
    run_bookkeeping_paths,
)


@pytest.fixture
def trees(tmp_path):
    """A work tree with an in-tree run dir, plus an out-of-tree state dir."""
    work = tmp_path / "repo"
    slug_dir = work / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}")
    (run_dir / "RUN.md").write_text("# run\n")
    (slug_dir / "prd.md").write_text("# prd\n")
    (slug_dir / "plan.md").write_text("# plan\n")

    external = tmp_path / "state" / "reviews" / "repo-id" / "branch"
    external.mkdir(parents=True)
    (external / "manifest.json").write_text("{}")
    return work, slug_dir, run_dir, external


# --- the contained case is unchanged -----------------------------------------


def test_contained_run_dir_yields_the_same_paths_as_before(trees):
    work, slug_dir, run_dir, _ = trees
    assert run_bookkeeping_excludes(work, run_dir, slug_dir) == [
        "runs/demo/run-1",
        "runs/*/PR.md",
    ]
    assert engine_bookkeeping_candidates(work, run_dir) == [
        "runs/demo/run-1/manifest.json",
        "runs/demo/run-1/RUN.md",
    ]
    assert run_bookkeeping_paths(work, run_dir) == [
        "runs/demo/run-1/manifest.json",
        "runs/demo/run-1/RUN.md",
    ]
    assert governed_artifact_paths(work, slug_dir) == [
        "runs/demo/prd.md",
        "runs/demo/plan.md",
    ]


def test_run_bookkeeping_paths_still_filters_to_files_that_exist(trees):
    work, _, run_dir, _ = trees
    (run_dir / "RUN.md").unlink()
    assert run_bookkeeping_paths(work, run_dir) == [
        "runs/demo/run-1/manifest.json"
    ]
    # ...while the candidate allowlist stays existence-independent, because a
    # commit classifier walks history where the path may no longer be on disk.
    assert engine_bookkeeping_candidates(work, run_dir) == [
        "runs/demo/run-1/manifest.json",
        "runs/demo/run-1/RUN.md",
    ]


# --- undeclared + uncontained fails closed -----------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda work, ext: run_bookkeeping_excludes(work, ext, ext),
            id="run_bookkeeping_excludes",
        ),
        pytest.param(
            lambda work, ext: engine_bookkeeping_candidates(work, ext),
            id="engine_bookkeeping_candidates",
        ),
        pytest.param(
            lambda work, ext: run_bookkeeping_paths(work, ext),
            id="run_bookkeeping_paths",
        ),
        pytest.param(
            lambda work, ext: governed_artifact_paths(work, ext),
            id="governed_artifact_paths",
        ),
    ],
)
def test_uncontained_and_undeclared_raises_instead_of_degrading(trees, call):
    work, _, _, external = trees
    with pytest.raises(StateDirNotContained) as exc:
        call(work, external)
    # The diagnostic must name both paths and the escape hatch — a bare
    # "ValueError" here is what the silent version effectively was.
    message = str(exc.value)
    assert str(external) in message
    assert str(work) in message
    assert "state_outside_worktree=True" in message


def test_a_sibling_directory_is_uncontained_even_when_it_looks_adjacent(tmp_path):
    """`relative_to` is a prefix test, so `repo-2` must not pass as `repo`."""
    work = tmp_path / "repo"
    work.mkdir()
    sibling = tmp_path / "repo-2" / "run-1"
    sibling.mkdir(parents=True)
    with pytest.raises(StateDirNotContained):
        engine_bookkeeping_candidates(work, sibling)


# --- declared external is empty by design (the review-run contract) ----------


def test_declared_external_state_returns_empty_without_raising(trees):
    work, _, _, external = trees
    assert (
        run_bookkeeping_excludes(
            work, external, external, state_outside_worktree=True
        )
        == []
    )
    assert (
        engine_bookkeeping_candidates(
            work, external, state_outside_worktree=True
        )
        == []
    )
    assert (
        run_bookkeeping_paths(work, external, state_outside_worktree=True) == []
    )
    assert (
        governed_artifact_paths(work, external, state_outside_worktree=True)
        == []
    )


def test_the_flag_does_not_suppress_a_contained_result(trees):
    """`state_outside_worktree` declares a possibility, it does not force one.

    A caller that sets the flag but happens to hold a contained state dir (an
    adopter pointing `review.state_dir` inside a sibling checkout) must still
    get its real paths — the flag widens what is tolerated, never what is
    returned.
    """
    work, slug_dir, run_dir, _ = trees
    assert engine_bookkeeping_candidates(
        work, run_dir, state_outside_worktree=True
    ) == [
        "runs/demo/run-1/manifest.json",
        "runs/demo/run-1/RUN.md",
    ]
    assert governed_artifact_paths(
        work, slug_dir, state_outside_worktree=True
    ) == ["runs/demo/prd.md", "runs/demo/plan.md"]


def test_review_declares_the_external_state_dir_at_the_call_site():
    """The one production caller that needs the permissive half declares it.

    Guards the wiring, not the builder: if `_build_review_orchestrator` ever
    stops passing the flag, review runs would start raising
    `StateDirNotContained` on an entirely legitimate out-of-repo state dir.
    """
    import inspect

    from gauntlet.engine import review

    source = inspect.getsource(review._build_review_orchestrator)
    assert "state_outside_worktree=True" in source
