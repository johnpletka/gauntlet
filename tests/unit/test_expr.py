"""`when:` / `foreach:` evaluator (FR-5.4)."""

import pytest

from gauntlet.engine.expr import eval_when, resolve_list


def test_when_none_is_true():
    assert eval_when(None, {}) is True


def test_when_truthy_path():
    assert eval_when("vars.ready", {"vars": {"ready": True}}) is True
    assert eval_when("vars.ready", {"vars": {"ready": False}}) is False
    assert eval_when("vars.missing", {"vars": {}}) is False


def test_when_negation():
    assert eval_when("not vars.skip", {"vars": {"skip": False}}) is True
    assert eval_when("not vars.skip", {"vars": {"skip": True}}) is False


def test_when_equality():
    ctx = {"vars": {"mode": "code_review"}}
    assert eval_when("vars.mode == 'code_review'", ctx) is True
    assert eval_when("vars.mode == 'document'", ctx) is False
    assert eval_when("vars.mode != 'document'", ctx) is True


def test_when_int_equality():
    assert eval_when("vars.n == 2", {"vars": {"n": 2}}) is True


def test_resolve_list():
    assert resolve_list("plan.phases", {"plan": {"phases": [1, 2, 3]}}) == [1, 2, 3]


def test_resolve_list_rejects_non_list():
    with pytest.raises(ValueError):
        resolve_list("vars.x", {"vars": {"x": "nope"}})


def test_resolve_list_missing():
    with pytest.raises(ValueError):
        resolve_list("vars.absent", {"vars": {}})
