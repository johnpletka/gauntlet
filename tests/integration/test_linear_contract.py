"""Contract test for the Linear issue tracker (plan P1): one real fetch.

Requires a live Linear personal API key (``LINEAR_API_KEY``) and a resolvable
issue ref (``GAUNTLET_TEST_LINEAR_REF``, e.g. ``ENG-1``). Skipped otherwise so
``pytest -m 'not integration'`` never touches the live API.
"""

import os

import pytest

from gauntlet.engine.config import IssueTrackerConfig
from gauntlet.trackers import get_tracker
from gauntlet.trackers.base import Issue

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _need_key_and_ref():
    if not os.environ.get("LINEAR_API_KEY"):
        pytest.skip("no LINEAR_API_KEY in environment for the Linear contract test")
    if not os.environ.get("GAUNTLET_TEST_LINEAR_REF"):
        pytest.skip("set GAUNTLET_TEST_LINEAR_REF to a resolvable issue key (e.g. ENG-1)")


def _tracker():
    return get_tracker(IssueTrackerConfig())


def test_verify_auth_succeeds():
    # A cheap `viewer { id }` probe against the real API — the doctor path.
    _tracker().verify_auth()


def test_fetch_resolves_a_real_issue():
    tk = _tracker()
    ref = tk.parse_ref(os.environ["GAUNTLET_TEST_LINEAR_REF"])
    issue = tk.fetch(ref)
    assert isinstance(issue, Issue)
    assert issue.identifier == ref.key
    assert issue.url
    # description may be empty, but the object must round-trip cleanly.
    assert isinstance(issue.description, str)
