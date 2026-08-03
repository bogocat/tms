"""Evidence directory convention tests (tms#127).

The evidence directory (~/.tms/evidence/<session-name>/) is how the AC-verify
step stores concrete artifacts (command transcripts, test output, log excerpts,
screenshots) that prove each acceptance criterion. These tests are static
guards against bin/tmq itself: they parse the evidence block out of
build_prompt() and assert the instructions agents actually receive — so they
fail if the convention drifts (e.g. evidence moves into the worktree, or the
per-agent session suffix is dropped).
"""

import re
from pathlib import Path

BIN_TMQ = Path(__file__).resolve().parent.parent / 'bin' / 'tmq'


def _evidence_block():
    """Extract the evidence-first verification block from build_prompt()."""
    src = BIN_TMQ.read_text()
    m = re.search(
        r'### Evidence-first verification.*?(?=\nRepository:)', src, re.S)
    assert m, "evidence-first verification block not found in bin/tmq"
    return m.group(0)


def _evidence_session_logic():
    """Extract the evidence_session computation preceding the prompt heredoc."""
    src = BIN_TMQ.read_text()
    m = re.search(
        r'local evidence_session\b.*?(?=\n\s*cat <<PROMPT)', src, re.S)
    assert m, "evidence_session computation not found in bin/tmq"
    return m.group(0)


def test_evidence_dir_outside_worktree():
    """The rendered instruction must place evidence under ~/.tms/evidence/,
    never under a worktree path — evidence must survive worktree cleanup."""
    block = _evidence_block()
    assert '~/.tms/evidence/' in block, \
        "prompt must instruct evidence under ~/.tms/evidence/"
    assert '/root/wt-' not in block and '$TMQ_CWD' not in block, \
        "evidence path must not reference the worktree"
    assert 'outside the worktree' in block, \
        "prompt must state evidence survives worktree cleanup"


def test_evidence_dir_uses_full_session_name():
    """The evidence dir must be the full session name (evidence_session),
    not the bare prefix-repo#number — otherwise concurrent pi/cc/oc
    dispatches of the same issue clobber each other."""
    block = _evidence_block()
    assert '${evidence_session}' in block, \
        "evidence dir must use ${evidence_session} (full session name)"
    assert re.search(r'\$\{session_prefix\}-\$\{repo\}#\$\{number\}', block) is None, \
        "evidence dir must not inline the un-suffixed session name"


def test_evidence_session_mirrors_main_agent_suffix():
    """evidence_session must come from the same tmq_session_name helper
    main() uses for session names (tms#138), so the per-agent suffixes
    (cc -> -cc, oc -> -oc, pi -> none) can never drift apart."""
    logic = _evidence_session_logic()
    assert 'tmq_session_name' in logic, \
        "evidence_session no longer computed via the shared tmq_session_name helper"
    src = BIN_TMQ.read_text()
    m = re.search(r'tmq_session_name\(\)\s*\{.*?^\}', src, re.S | re.M)
    assert m, "tmq_session_name helper not found in bin/tmq"
    helper = m.group(0)
    assert re.search(r'cc\)\s*name="\$\{name\}-cc"', helper), \
        "cc agent suffix missing from tmq_session_name"
    assert re.search(r'oc\)\s*name="\$\{name\}-oc"', helper), \
        "oc agent suffix missing from tmq_session_name"
    # main() must still name sessions via the same helper
    assert 'session_name=$(tmq_session_name' in src, \
        "main() no longer names sessions via tmq_session_name"


def test_evidence_per_ac_artifact_instruction():
    """Each AC gets one evidence file with ac<n>-<descriptor> naming, holding
    the actual transcript/output — not prose."""
    block = _evidence_block()
    assert 'ac1-test-output.txt' in block and 'ac2-command-transcript.txt' in block, \
        "prompt must show ac<n>-<descriptor> file naming examples"
    assert 'not prose' in block, \
        "prompt must require raw transcripts/output, not prose descriptions"


def test_evidence_pr_body_verification_mapping():
    """The prompt must instruct mapping each AC to its artifact path in the
    PR body Verification section."""
    block = _evidence_block()
    assert 'Verification' in block, \
        "prompt must reference the PR body Verification section"
    assert 'artifact path' in block, \
        "prompt must instruct AC -> artifact path mapping"


def test_evidence_screenshot_instruction_is_valid():
    """Screenshot handling must not use the broken `gh pr comment ... <file>`
    form (gh pr comment has no file-attach positional)."""
    block = _evidence_block()
    assert not re.search(r'gh pr comment[^\n]*<screenshot>', block), \
        "screenshot instruction uses invalid gh pr comment file syntax"
    assert 'evidence dir' in block or 'Screenshots' in block, \
        "prompt must say where screenshots live"
