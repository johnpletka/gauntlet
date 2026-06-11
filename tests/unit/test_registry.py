"""Entry-point adapter registry (FR-2.4) and capability declarations (FR-2.3)."""

import pytest

from gauntlet.adapters import available_adapters, get_adapter_class
from gauntlet.adapters.api import ApiAdapter
from gauntlet.adapters.base import AgentAdapter
from gauntlet.adapters.claude_code import ClaudeCodeAdapter
from gauntlet.adapters.codex import CodexAdapter


def test_builtin_adapters_registered_via_entry_points():
    adapters = available_adapters()
    assert adapters["claude-code"] is ClaudeCodeAdapter
    assert adapters["codex"] is CodexAdapter
    assert adapters["api"] is ApiAdapter


def test_get_adapter_class_by_name():
    assert get_adapter_class("codex") is CodexAdapter


def test_unknown_adapter_raises_with_known_names():
    with pytest.raises(KeyError, match="claude-code"):
        get_adapter_class("gemini-cli")


def test_capability_declarations():
    assert ClaudeCodeAdapter.capabilities.repo_write is True
    assert ClaudeCodeAdapter.capabilities.resume is True
    assert CodexAdapter.capabilities.repo_write is True
    assert CodexAdapter.capabilities.structured_output == "native"
    assert CodexAdapter.capabilities.resume is True
    # FR-2.3: a repo-write step must not bind to the api adapter
    assert ApiAdapter.capabilities.repo_write is False
    assert ApiAdapter.capabilities.resume is False


def test_instances_satisfy_protocol():
    assert isinstance(ClaudeCodeAdapter(), AgentAdapter)
    assert isinstance(CodexAdapter(), AgentAdapter)
    assert isinstance(ApiAdapter(model="m"), AgentAdapter)
