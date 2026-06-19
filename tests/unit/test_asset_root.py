"""`asset_root`: adopters consolidate every gauntlet-owned asset under .gauntlet/
(via the init-scaffolded config), while Gauntlet's own repo keeps them at the
repo root through the default ``asset_root: "."``. One knob, threaded through
every resolution site; "." is a no-op in path joins, so it is transparent."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from gauntlet.engine import proposals as P
from gauntlet.engine.config import RunConfig
from gauntlet.engine.init import init_repo

REPO = Path(__file__).resolve().parents[2]


def test_asset_root_defaults_to_dot():
    # "." keeps assets at the repo root (Gauntlet's own layout, backward-compat).
    assert RunConfig().asset_root == "."


def test_path_allowed_shifts_with_asset_root():
    # Default "." — the bare, repo-root-relative allowlist.
    assert P.path_allowed("prompts/triage.md")
    assert P.path_allowed("policy.yaml")
    # A .gauntlet/-prefixed path is NOT an asset under the "." layout.
    assert not P.path_allowed(".gauntlet/prompts/triage.md")

    # Adopter (".gauntlet") — the real on-disk paths carry the prefix, so the
    # allowlist shifts with them; the bare path no longer matches.
    assert P.path_allowed(".gauntlet/prompts/triage.md", asset_root=".gauntlet")
    assert P.path_allowed(".gauntlet/policy.yaml", asset_root=".gauntlet")
    assert not P.path_allowed("prompts/triage.md", asset_root=".gauntlet")

    # Containment (absolute / traversal) is rejected under ANY asset_root.
    assert not P.path_allowed("/etc/passwd", asset_root=".gauntlet")
    assert not P.path_allowed(".gauntlet/prompts/../../etc/passwd", asset_root=".gauntlet")
    assert not P.path_allowed("src/gauntlet/cli.py", asset_root=".gauntlet")


def test_path_containment_uses_asset_root():
    diff = (
        "--- a/.gauntlet/prompts/triage.md\n"
        "+++ b/.gauntlet/prompts/triage.md\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    ok, offending = P.path_containment(diff, asset_root=".gauntlet")
    assert ok and not offending
    # The same diff is OUT of allowlist under the default "." layout.
    ok2, offending2 = P.path_containment(diff)
    assert not ok2 and offending2 == [".gauntlet/prompts/triage.md"]


def test_init_scaffolds_consolidated_adopter_config(tmp_path):
    # `gauntlet init` produces an adopter repo whose config consolidates under
    # .gauntlet/, and every asset the engine resolves under asset_root is there.
    init_repo(tmp_path)
    cfg = RunConfig.load(tmp_path / ".gauntlet/config.yaml")
    assert cfg.asset_root == ".gauntlet"
    assert cfg.run_root == ".gauntlet/runs"
    base = tmp_path / cfg.asset_root
    assert (base / "policy.yaml").exists()
    assert (base / "pipelines" / "standard.yaml").exists()
    assert (base / "prompts" / "triage.md").exists()
    assert (base / "schemas" / "findings.json").exists()


def test_gauntlet_own_repo_keeps_root_layout():
    # Gauntlet's OWN config does NOT consolidate — its source assets stay at the
    # repo root (a dotfile dir is for tool data in *other* projects).
    cfg = RunConfig.load(REPO / ".gauntlet/config.yaml")
    assert cfg.asset_root == "."
    assert (REPO / "policy.yaml").exists()
    assert (REPO / "pipelines" / "standard.yaml").exists()
    assert not (REPO / ".gauntlet" / "pipelines").exists()


def test_asset_root_validation_and_normalization():
    # Normalises spelling so the path join and the proposals allowlist agree
    # (Copilot): "./.gauntlet" and ".gauntlet/" both collapse to ".gauntlet".
    assert RunConfig(asset_root="./.gauntlet").asset_root == ".gauntlet"
    assert RunConfig(asset_root=".gauntlet/").asset_root == ".gauntlet"
    assert RunConfig(asset_root="a/./b").asset_root == "a/b"
    assert RunConfig(asset_root=".").asset_root == "."
    # Fail closed (F-006) on anything that could escape the repo boundary.
    for bad in ("/abs/path", "~/x", "../escape", "a/../../b", ""):
        with pytest.raises(ValidationError):
            RunConfig(asset_root=bad)


def test_run_root_validation_and_normalization():
    # run_root gets the same repo-relative containment as asset_root (F-001):
    # an absolute or escaping value would let `gauntlet serve` browse outside
    # the repo. Normalisation collapses redundant "./" segments.
    assert RunConfig().run_root == "runs"
    assert RunConfig(run_root="./.gauntlet/runs").run_root == ".gauntlet/runs"
    assert RunConfig(run_root="runs/").run_root == "runs"
    for bad in ("/abs/runs", "~/runs", "../outside", "a/../../b", ""):
        with pytest.raises(ValidationError):
            RunConfig(run_root=bad)
