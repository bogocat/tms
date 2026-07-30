"""Evidence directory convention tests (tms#127).

The evidence directory (~/.tms/evidence/<dispatch-id>/) is how the AC-verify
step stores concrete artifacts (command transcripts, test output, log excerpts,
screenshots) that prove each acceptance criterion. These tests verify the
convention itself — the directory layout, per-AC artifact files, and survival
outside the worktree — so tooling and reviewers can depend on the structure.
"""

import os
import tempfile
from pathlib import Path


def test_evidence_dir_convention():
    """Evidence lives at ~/.tms/evidence/<dispatch-id>/ — outside any
    worktree so it survives worktree cleanup."""
    evidence_root = Path.home() / '.tms' / 'evidence'
    dispatch_id = 'feat-tms#999'
    evidence_dir = evidence_root / dispatch_id

    # Convention: dispatch-id is the tmq session name (feat-<repo>#<num>).
    assert '/.tms/evidence/' in str(evidence_dir), \
        "evidence must live under ~/.tms/evidence/"


def test_evidence_per_ac_file_naming():
    """Each AC gets its own evidence file: ac<n>-<descriptor>.txt for
    text artifacts (command transcripts, test output, log excerpts)."""
    dispatch_id = 'feat-tms#999'
    ac_files = [
        'ac1-test-output.txt',
        'ac2-command-transcript.txt',
        'ac3-log-excerpt.txt',
    ]
    for fname in ac_files:
        assert fname.startswith('ac'), \
            f"evidence file {fname} must start with ac<n>"
        assert '.' in fname, \
            f"evidence file {fname} must have an extension"
        parts = fname.split('-', 2)
        assert len(parts) >= 2, \
            f"evidence file {fname} must have ac<n>-<descriptor> format"


def test_evidence_dir_outside_worktree():
    """Evidence must survive worktree cleanup — the evidence root
    (~/.tms/evidence/) is NOT under /root/wt-* or any repo path."""
    evidence_root = Path.home() / '.tms' / 'evidence'

    # Must be under $HOME, not under any worktree path
    assert str(evidence_root).startswith(str(Path.home())), \
        "evidence root must be under $HOME"

    # Must NOT be under a worktree prefix
    assert '/root/wt-' not in str(evidence_root), \
        "evidence root must NOT be under a worktree path"


def test_evidence_dir_creation():
    """The evidence directory can be created with a single mkdir -p."""
    with tempfile.TemporaryDirectory() as tmp:
        evidence_root = Path(tmp) / '.tms' / 'evidence'
        dispatch_id = 'feat-tms#999'
        evidence_dir = evidence_root / dispatch_id

        evidence_dir.mkdir(parents=True, exist_ok=True)
        assert evidence_dir.is_dir(), \
            f"evidence directory {evidence_dir} was not created"


def test_evidence_artifact_file_write():
    """Evidence files are plain text artifacts written by the agent
    during AC-verify."""
    with tempfile.TemporaryDirectory() as tmp:
        evidence_dir = Path(tmp) / 'feat-test#1'
        evidence_dir.mkdir(parents=True)

        artifact = evidence_dir / 'ac1-test-output.txt'
        content = "=== Test Output ===\n3 passed, 0 failed\n"
        artifact.write_text(content)

        assert artifact.exists(), \
            f"artifact file {artifact} does not exist"
        assert artifact.read_text() == content, \
            "artifact file content was corrupted"
        assert artifact.stat().st_size > 0, \
            "artifact file is empty"


def test_screenshot_evidence_naming():
    """Screenshots use ac<n>-<descriptor>.png and are stored alongside
    text artifacts in the same evidence directory."""
    dispatch_id = 'feat-home-portal#42'
    screenshot_files = [
        'ac2-browser-render.png',
        'ac3-modal-close.png',
    ]
    for fname in screenshot_files:
        assert fname.startswith('ac'), \
            f"screenshot {fname} must start with ac<n>"
        assert fname.endswith('.png'), \
            f"screenshot {fname} must be .png"


def test_evidence_dir_disambiguates_concurrent_dispatches():
    """Two concurrent dispatches must not collide on evidence paths —
    the dispatch-id (session name) ensures uniqueness."""
    dispatch_a = 'feat-distillery#600'
    dispatch_b = 'feat-home-portal#300'

    evidence_root = Path('/tmp/.tms/evidence')
    dir_a = evidence_root / dispatch_a
    dir_b = evidence_root / dispatch_b

    assert str(dir_a) != str(dir_b), \
        "concurrent dispatch evidence paths must be distinct"
    assert dispatch_a in str(dir_a) and dispatch_b in str(dir_b), \
        "evidence path must encode the dispatch identity"
