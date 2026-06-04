"""Tests for the ST column label/color mapping.

The ST column in `tms ls` is the "is the agent doing something useful
right now" indicator. aoe status strings map to 4-char compact labels
and ANSI color codes; the labels are padded for column alignment.

The source of truth is FORMAT_TABLE in lib/tms/aoe_status.py. The bash
`_format_status` and `_status_color` case statements in bin/tms should
be regenerated from this table (via emit_bash_format_fn) — out of
scope for the initial extraction, but this test pins the values so a
follow-up sync can be verified mechanically.
"""

import subprocess

from tms.aoe_status import FORMAT_TABLE, format_status, emit_bash_format_fn


# ── FORMAT_TABLE has the right entries ────────────────────────────


def test_table_has_all_five_known_statuses():
    for s in ('running', 'waiting', 'idle', 'error', 'stopped'):
        assert s in FORMAT_TABLE, f"missing status: {s}"


def test_table_labels_are_4_chars_padded():
    """Labels must be exactly 4 chars for column alignment in tms ls."""
    for status, (label, _) in FORMAT_TABLE.items():
        assert len(label) == 4, f"{status} label is {len(label)} chars, expected 4"


def test_table_color_codes_are_ansi_compatible():
    """Color codes are 'X;Y' format: bold(1)/dim(2) + fg color (30-37)."""
    valid_prefixes = ('32;1', '33;1', '2', '31;1', '31;2')
    for status, (_, color) in FORMAT_TABLE.items():
        assert color in valid_prefixes, f"{status} color {color!r} not in {valid_prefixes}"


# ── format_status returns the right values ────────────────────────


def test_running_is_green_bold():
    assert format_status('running') == ('run ', '32;1')


def test_waiting_is_yellow_bold():
    assert format_status('waiting') == ('wait', '33;1')


def test_idle_is_dim():
    assert format_status('idle') == ('idle', '2')


def test_error_is_red_bold():
    assert format_status('error') == ('err ', '31;1')


def test_stopped_is_red_dim():
    assert format_status('stopped') == ('stop', '31;2')


def test_unknown_status_uses_placeholder():
    """A status we don't recognize (or None) shows an em-dash placeholder."""
    label, color = format_status('something-else')
    assert label == '—   '
    assert color == '2'


def test_none_status_uses_placeholder():
    label, color = format_status(None)
    assert label == '—   '
    assert color == '2'


# ── emit_bash_format_fn produces parseable bash ──────────────────


def test_emitted_bash_is_parseable():
    """The bash source emitted from FORMAT_TABLE must `bash -n` cleanly."""
    bash_src = emit_bash_format_fn()
    # bash -n parses without executing; non-zero = syntax error
    result = subprocess.run(
        ['bash', '-n'], input=bash_src, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"emitted bash failed to parse:\n{result.stderr}\n--- source ---\n{bash_src}"
    )


def test_emitted_bash_contains_all_statuses():
    """The emitted case statement must cover every status in the table."""
    bash_src = emit_bash_format_fn()
    for status in FORMAT_TABLE:
        assert f'{status})' in bash_src, f"missing case branch for {status}"
