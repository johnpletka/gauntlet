"""Structured-output parsing: bounded repair + position-bearing errors (#169)."""

from __future__ import annotations

import json

import pytest

from gauntlet.adapters._structured import extract_json, parse_json_output


def _proposals_with_extra_brace() -> str:
    """The #169 failure shape: a valid proposals document except that the
    first proposal's object is closed with ``}}`` right after its long
    ``diff`` string — what the proposer emitted on all nine attempts."""
    first = {
        "slug": "tighten-fix-scope",
        "target_path": "prompts/fix.md",
        "rationale": "the fixer touched unrelated files",
        "diff": (
            "--- a/prompts/fix.md\n+++ b/prompts/fix.md\n@@ -1,2 +1,3 @@\n"
            " Fix the finding.\n Keep the change small.\n"
            "+Fix exactly the remainder its `claim`/`location` names.\n"
        ),
    }
    second = {
        "slug": "require-counter-fixtures",
        "target_path": "prompts/review.md",
        "rationale": "reviewer accepted untested claims",
        "diff": "--- a/prompts/review.md\n+++ b/prompts/review.md\n",
    }
    good = json.dumps({"proposals": [first, second]})
    marker = '"}, {"slug": "require-counter'
    assert marker in good
    return good.replace(marker, '"}}, {"slug": "require-counter', 1)


def test_extra_closing_brace_is_repaired():
    text = _proposals_with_extra_brace()
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    value, repairs = parse_json_output(text)
    assert [p["slug"] for p in value["proposals"]] == [
        "tighten-fix-scope", "require-counter-fixtures",
    ]
    assert len(repairs) == 1 and "removed stray '}'" in repairs[0]


def test_valid_json_reports_no_repairs():
    value, repairs = parse_json_output('{"verdict": "ok"}')
    assert value == {"verdict": "ok"} and repairs == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": [1, 2,]}', {"a": [1, 2]}),  # trailing comma in an array
        ('{"a": 1, "b": 2,}', {"a": 1, "b": 2}),  # trailing comma in an object
        ('{"a": [1, 2}', {"a": [1, 2]}),  # unclosed array before the object closes
        ('{"a": {"b": 1}}}', {"a": {"b": 1}}),  # surplus closer at the end
        ('Here you go:\n```json\n{"a": [1,]}\n```', {"a": [1]}),  # inside a fence
    ],
)
def test_bounded_structural_repairs(text, expected):
    assert extract_json(text) == expected


def test_unrepairable_output_names_the_error_position():
    text = 'prose first {"proposals": [{"slug": "x" "diff": "y"}]} trailing'
    with pytest.raises(ValueError) as exc:
        extract_json(text)
    msg = str(exc.value)
    assert msg.startswith("no parseable JSON found in output text: ")
    assert "Expecting ',' delimiter" in msg
    assert "(char " in msg and "line 1 column" in msg
    assert "near " in msg and '"diff"' in msg


def test_free_text_is_not_repaired_into_json():
    with pytest.raises(ValueError):
        extract_json("I could not produce the proposals this time.")
