"""Research 005: per-account tagging of performance/mission-stats output.

Accounts at different progression fly different jets with different missiles.
An untagged mix silently corrupts the performance regression baseline and is
near-impossible to unpick afterwards, so the tag is asserted at the source.

Usage: uv run pytest tests/test_account_tag.py -q
"""

import pytest

from wingman.performance import account_tag


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("WINGMAN_ACCOUNT", raising=False)


def test_unset_is_empty_so_run_ids_are_unchanged(monkeypatch):
    """Single-account users must see byte-identical run_id behaviour."""
    assert account_tag() == ""


def test_tag_is_passed_through(monkeypatch):
    monkeypatch.setenv("WINGMAN_ACCOUNT", "acct1")
    assert account_tag() == "acct1"


def test_blank_and_whitespace_are_treated_as_unset(monkeypatch):
    monkeypatch.setenv("WINGMAN_ACCOUNT", "   ")
    assert account_tag() == ""


def test_path_separators_cannot_escape_the_output_directory(monkeypatch):
    """The tag becomes part of a filename — a separator would let it write
    outside docs/performance/current/."""
    monkeypatch.setenv("WINGMAN_ACCOUNT", "../../etc/passwd")
    tag = account_tag()
    assert "/" not in tag and ".." not in tag, tag


def test_tag_is_length_bounded(monkeypatch):
    monkeypatch.setenv("WINGMAN_ACCOUNT", "a" * 500)
    assert len(account_tag()) <= 32
