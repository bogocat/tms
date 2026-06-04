"""Tests for parse_issue_title (aoe title) and parse_tmq_session_name
(tmq session name) — both regexes used in the session→issue matcher.

Both return (type, repo, num) tuples for valid names and None otherwise.
The aoe-title regex is the source of truth for aoe session matching
(joined via the 8-char id prefix); the tmq-name regex is the source of
truth for tmq-named sessions (feat-/fix-/chore-/review- prefix).

The "tms issue filing" descriptive-title case is the one that triggered
the P1#3 fix in PR #7 (a descriptive-titled aoe session on /root/tms
must not auto-link to whatever issue branch is checked out there).
"""

from tms.session_map import parse_issue_title, parse_tmq_session_name


# ── parse_issue_title (aoe titles) ───────────────────────────────


def test_parses_aoe_feat_title():
    assert parse_issue_title("feat-home-portal#5") == ("feat", "home-portal", "5")


def test_parses_aoe_fix_title():
    assert parse_issue_title("fix-tms#10") == ("fix", "tms", "10")


def test_parses_aoe_review_title():
    assert parse_issue_title("review-distillery#245") == ("review", "distillery", "245")


def test_returns_none_for_descriptive_title():
    # The P1#3 case: descriptive aoe title with no #num.
    # This MUST return None so the matcher falls back to worktree-branch
    # parsing instead of fabricating a link.
    assert parse_issue_title("tms issue filing") is None


def test_returns_none_for_repo_only():
    # No type prefix, no #num
    assert parse_issue_title("home-portal#5") is None


def test_returns_none_for_type_only():
    # Type prefix but no repo
    assert parse_issue_title("feat-#5") is None


def test_returns_none_for_missing_num():
    # Type and repo, no #num
    assert parse_issue_title("feat-home-portal") is None


def test_returns_none_for_agent_suffix():
    # -oc / -cc is a tmq session-name marker, not an aoe title
    assert parse_issue_title("feat-home-portal#5-oc") is None


def test_returns_none_for_empty_aoe_title():
    assert parse_issue_title("") is None


def test_returns_none_for_none():
    # Graceful None handling
    assert parse_issue_title(None) is None


# ── parse_tmq_session_name (tmq session names) ───────────────────


def test_parses_tmq_feat_name():
    assert parse_tmq_session_name("feat-distillery#245") == ("feat", "distillery", "245")


def test_parses_tmq_feat_name_with_oc_suffix():
    # tmq session names carry -oc / -cc as agent markers; the
    # matcher's key is repo#num (suffix is not part of the key)
    assert parse_tmq_session_name("feat-distillery#245-oc") == (
        "feat",
        "distillery",
        "245",
    )


def test_parses_tmq_feat_name_with_cc_suffix():
    assert parse_tmq_session_name("feat-distillery#245-cc") == (
        "feat",
        "distillery",
        "245",
    )


def test_parses_tmq_fix_name():
    assert parse_tmq_session_name("fix-tms#10") == ("fix", "tms", "10")


def test_returns_none_for_aoe_prefix():
    # aoe session names are matched via the id prefix, not this regex
    assert parse_tmq_session_name("aoe_feat-tms_1_abc12345") is None


def test_returns_none_for_scratch_name():
    # c0/c1/o0/p0 are matched by a different regex in the heredoc
    assert parse_tmq_session_name("c5") is None


def test_returns_none_for_empty_tmq_name():
    assert parse_tmq_session_name("") is None
