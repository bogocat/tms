"""Tests for parse_issue_branch — the branch-name regex used in
_issue_session_map to identify which issue a worktree session is on.

Bash callers expect (type, num) tuple or None for non-issue branches.
Test coverage includes the four valid types and the common negatives.
"""

from tms.session_map import parse_issue_branch


def test_parses_feat_branch():
    assert parse_issue_branch("feat/issue-245-blah") == ("feat", "245")


def test_parses_fix_branch():
    assert parse_issue_branch("fix/issue-10-crash-on-startup") == ("fix", "10")


def test_parses_chore_branch():
    assert parse_issue_branch("chore/issue-3-bump-deps") == ("chore", "3")


def test_parses_review_branch():
    assert parse_issue_branch("review/issue-1") == ("review", "1")


def test_returns_none_for_main():
    assert parse_issue_branch("main") is None


def test_returns_none_for_branch_without_number():
    # Looks like feat/ but missing the issue-N segment
    assert parse_issue_branch("feat/something-without-num") is None


def test_returns_none_for_unrelated_type():
    # Must start with feat|fix|chore|review — random prefix doesn't match
    assert parse_issue_branch("random/issue-99-foo") is None


def test_returns_none_for_empty_string():
    assert parse_issue_branch("") is None


def test_returns_none_for_none():
    # Graceful None handling (the heredoc uses `branch or ''`)
    assert parse_issue_branch(None) is None
