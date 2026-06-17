"""Guard the tmq dispatch-prompt wiring of the in-session loop (#20).

These are static guards: build_prompt is a bash function in bin/tmq that
fetches the issue from GitHub, so it isn't cleanly unit-testable. Instead we
assert the source carries the loop pointers, so the wiring can't be silently
removed. The agent-type branching is checked by parsing the `case "$agent"`
block out of bin/tmq.
"""

import re
from pathlib import Path

BIN_TMQ = Path(__file__).resolve().parent.parent / 'bin' / 'tmq'


def _agent_case_block():
    """Return the body of the `case "$agent"` block that sets loop_instruction."""
    src = BIN_TMQ.read_text()
    m = re.search(r'loop_instruction=""\s*\n\s*case "\$agent" in(.*?)\n\s*esac', src, re.S)
    assert m, "loop_instruction case block not found in bin/tmq"
    return m.group(1)


def test_pi_branch_names_dispatch_loop_skill():
    block = _agent_case_block()
    pi_arm = re.search(r'\bpi\)(.*?);;', block, re.S)
    assert pi_arm, "no pi) arm in the loop_instruction case block"
    assert 'dispatch-loop' in pi_arm.group(1), \
        "pi dispatch prompt no longer names the dispatch-loop skill"


def test_pi_branch_requests_state_markers():
    block = _agent_case_block()
    assert '<<AGENT-STATE' in block, "loop pointer dropped the AGENT-STATE marker contract"


def _arm(block, label):
    """Return the body of a single `<label>)` arm in the case block."""
    m = re.search(re.escape(label) + r'\)(.*?);;', block, re.S)
    assert m, f"no {label}) arm in the loop_instruction case block"
    return m.group(1)


def test_cc_branch_prefers_dispatch_loop_skill():
    """#38: cc agents prefer the Claude-native dispatch-loop skill when it is
    installed, so the loop travels even into a repo with no AGENTS.md."""
    cc_arm = _arm(_agent_case_block(), 'cc')
    assert '.claude/skills/dispatch-loop/SKILL.md' in cc_arm, \
        "cc arm no longer detects the installed Claude dispatch-loop skill"
    assert 'dispatch-loop' in cc_arm, \
        "cc arm no longer points at the dispatch-loop skill"


def test_cc_branch_falls_back_to_agents_md():
    """cc must still fall back to AGENTS.md when the skill is not installed."""
    cc_arm = _arm(_agent_case_block(), 'cc')
    assert 'agents_md_ptr' in cc_arm or 'AGENTS.md' in cc_arm, \
        "cc arm lost the AGENTS.md fallback"


def test_oc_branch_falls_back_to_agents_md():
    """oc has no Claude skill (out of scope); keeps the AGENTS.md fallback."""
    oc_arm = _arm(_agent_case_block(), 'oc')
    assert 'agents_md_ptr' in oc_arm or 'AGENTS.md' in oc_arm, \
        "oc arm lost the AGENTS.md fallback"


def test_build_prompt_receives_agent_arg():
    """build_prompt must take the agent param and be called with it."""
    src = BIN_TMQ.read_text()
    assert re.search(r'build_prompt\(\)\s*\{\s*\n\s*local repo=\$1 number=\$2 type=\$3 agent=', src), \
        "build_prompt no longer declares the agent parameter"
    assert 'build_prompt "$repo" "$number" "$type" "$agent"' in src, \
        "build_prompt is not called with $agent"


def test_pi_dispatch_sets_autoapprove_env():
    """Dispatched pi agents must auto-approve the safety-guards CONFIRM tier
    (#30) or they stall on permission prompts with no human attached."""
    src = BIN_TMQ.read_text()
    # pi launch is `PI_DISPATCH_AUTOAPPROVE=1 pi${pi_extra} @<file>` — pi_extra
    # (the #37 --provider/--model forwarding) sits between `pi` and `@`.
    assert re.search(r'PI_DISPATCH_AUTOAPPROVE=1 pi(\$\{pi_extra\})? @', src), \
        "pi cmd_override no longer sets PI_DISPATCH_AUTOAPPROVE — dispatched pi stalls on prompts"


# ── #39: cc dispatch under root ──────────────────────────────────
# Claude Code refuses --dangerously-skip-permissions for root/sudo, so a cc
# dispatch on a root host dies instantly and vanishes on the next aoe daemon
# restart. spawn_agent must gate the root cc path: fail fast by default, with
# an explicit opt-in IS_SANDBOX=1 escape hatch.

def _spawn_agent_src():
    src = BIN_TMQ.read_text()
    m = re.search(r'\nspawn_agent\(\)\s*\{(.*?)\n\}', src, re.S)
    assert m, "spawn_agent function not found in bin/tmq"
    return m.group(1)


def test_root_cc_gate_exists():
    """spawn_agent must detect root (EUID 0) for the cc agent."""
    body = _spawn_agent_src()
    assert re.search(r'EUID', body) and re.search(r'cc', body), \
        "spawn_agent no longer gates the cc path on root (EUID)"


def test_root_cc_escape_hatch_is_opt_in():
    """The IS_SANDBOX=1 escape hatch must be guarded by the explicit
    TMQ_ALLOW_ROOT_CC opt-in, never enabled by default."""
    body = _spawn_agent_src()
    assert 'TMQ_ALLOW_ROOT_CC' in body, \
        "root cc escape hatch lost its TMQ_ALLOW_ROOT_CC opt-in guard"
    assert 'IS_SANDBOX=1' in body, \
        "root cc path no longer offers the IS_SANDBOX=1 escape hatch"


def test_root_cc_fails_fast_with_message():
    """Default (no opt-in) root cc must abort with a non-zero return and a
    clear message rather than spawning a vanishing dead pane."""
    body = _spawn_agent_src()
    assert re.search(r'refused under root', body), \
        "root cc default path lost its clear fail-fast message"
    assert re.search(r'return 1', body), \
        "root cc default path no longer aborts (return 1) before spawning"


# ── #42: cc prompt must not be word-split ────────────────────────
# An unquoted $(cat ... | jq) passed to `-p` word-splits the prompt: only its
# first token reaches claude and any `--word` in the issue body becomes a stray
# flag. The prompt must travel via stdin (like the oc branch) instead.

def test_cc_prompt_not_word_split_inline():
    """No cc launch line may pass the prompt as an inline command
    substitution to -p (the word-splitting bug, #42)."""
    src = BIN_TMQ.read_text()
    for line in src.splitlines():
        if 'claude --dangerously-skip-permissions' not in line:
            continue
        assert not re.search(r'-p\s+\$\(', line), \
            f"cc launch still word-splits the prompt into -p: {line.strip()}"


def test_cc_prompt_piped_via_stdin():
    """Every cc launch must feed the prompt file to claude on stdin."""
    src = BIN_TMQ.read_text()
    cc_lines = [l for l in src.splitlines()
                if 'claude --dangerously-skip-permissions' in l]
    assert cc_lines, "no cc claude launch lines found in bin/tmq"
    for line in cc_lines:
        assert re.search(r"cat '[^']*prompt_file[^']*'\s*\|", line) \
            or re.search(r"cat \"[^\"]*prompt_file[^\"]*\"\s*\|", line), \
            f"cc launch does not pipe the prompt file via stdin: {line.strip()}"


# ── #37: --provider/--model pass-through to pi ───────────────────

def test_provider_model_args_parsed():
    """main() must parse --provider/--model into the PI_* globals."""
    src = BIN_TMQ.read_text()
    assert re.search(r'--provider\)\s*PI_PROVIDER="\$2"', src), \
        "--provider is not parsed into PI_PROVIDER"
    assert re.search(r'--model\)\s*PI_MODEL="\$2"', src), \
        "--model is not parsed into PI_MODEL"


def test_pi_launch_forwards_provider_model():
    """Every pi launch must forward PI_PROVIDER/PI_MODEL via pi_extra."""
    src = BIN_TMQ.read_text()
    pi_lines = [l for l in src.splitlines()
                if re.search(r'(PI_DISPATCH_AUTOAPPROVE=1 pi|"pi)\$\{pi_extra\}', l)]
    assert pi_lines, "no pi launch line forwards ${pi_extra}"
    # pi_extra must be built from both flags
    assert 'pi_extra+=" --provider $PI_PROVIDER"' in src, \
        "pi_extra does not forward --provider"
    assert 'pi_extra+=" --model $PI_MODEL"' in src, \
        "pi_extra does not forward --model"


def test_model_help_documented():
    """--provider/--model must appear in usage()."""
    src = BIN_TMQ.read_text()
    assert '--model <id>' in src and '--provider <name>' in src, \
        "usage() no longer documents --provider/--model"


def test_model_with_non_pi_agent_errors_fast():
    """tmq <repo> <n> --agent cc --model X must exit non-zero with a clear
    message rather than silently ignoring the flag (#37)."""
    import subprocess
    r = subprocess.run(
        ['bash', str(BIN_TMQ), 'tms', '37', '--agent', 'cc', '--model', 'MiniMax-M3'],
        capture_output=True, text=True)
    assert r.returncode != 0, "cc + --model should fail fast, not proceed"
    assert 'only apply to --agent pi' in r.stderr, \
        f"cc + --model error message unclear: {r.stderr!r}"


# ── #10: tmq review reuse follow-up improvements ─────────────────


def _create_worktree_review_body():
    """Extract the review-reuse block from create_worktree — the code
    between feat_path=$(find_feat_session ...) and TMQ_REUSED=1."""
    src = BIN_TMQ.read_text()
    m = re.search(
        r'create_worktree\(\).*?'
        r'if \[\[ "\$type" == "review" \]\].*?\n'
        r'(.*?)\bTMQ_REUSED=1\b',
        src, re.S,
    )
    assert m, "review reuse block (create_worktree review → TMQ_REUSED=1) not found"
    return m.group(1)


def test_create_worktree_gates_reuse_on_worktree_repo():
    """Item 2: Non-worktree repos must skip the reuse path. The
    create_worktree review-reuse block must check REPO_WORKTREE before
    accepting the worktree."""
    body = _create_worktree_review_body()
    assert 'REPO_WORKTREE' in body, (
        "create_worktree review reuse path no longer checks REPO_WORKTREE"
    )


def test_create_worktree_logs_non_worktree_skip():
    """Item 2: When the non-worktree gate fires, the operator should see
    why reuse was skipped. Check for a log message near the gate."""
    body = _create_worktree_review_body()
    assert 'non-worktree' in body or 'skipping reuse' in body, (
        "create_worktree no longer logs when skipping reuse for non-worktree repos"
    )


def test_find_feat_session_loops_over_prefixes():
    """Item 1: find_feat_session must try feat-, fix-, chore- prefixes
    in a loop, not just the hardcoded feat-."""
    src = BIN_TMQ.read_text()
    # The find_feat_session function body must contain a loop over prefixes
    m = re.search(r'find_feat_session\(\).*?^\}', src, re.S | re.M)
    assert m, "find_feat_session function not found"
    body = m.group(0)
    assert re.search(r'for\s+\w+\s+in\s+.*feat.*fix.*chore', body), (
        "find_feat_session no longer loops over feat/fix/chore prefixes"
    )


def test_find_feat_session_returns_prefix():
    """Item 1: find_feat_session must communicate which prefix matched
    so the liveness check in create_worktree can use a precise aoe id
    match instead of a broad glob (P0 fix from proposal review)."""
    src = BIN_TMQ.read_text()
    m = re.search(r'find_feat_session\(\).*?^\}', src, re.S | re.M)
    assert m, "find_feat_session function not found"
    body = m.group(0)
    # Must output more than just the path — either two lines or a global
    has_prefix_out = 'TMQ_FEAT_PREFIX' in body or 'TMQ_SESSION_PREFIX' in body
    has_id_out = 'TMQ_AOE_ID' in body or 'aoe_id' in body.lower()
    assert has_prefix_out or has_id_out, (
        "find_feat_session no longer communicates the matched prefix/id "
        "— liveness check can't use a precise match (P0)"
    )


def test_create_worktree_liveness_uses_precise_match():
    """Item 1: The liveness check must use a precise aoe session id
    match, not a broad glob (P0 fix from proposal review: aoe_*-...
    matches review- sessions and cross-type ghosts)."""
    body = _create_worktree_review_body()
    # The liveness check must reference a captured prefix or id, not a
    # hardcoded glob with a wildcard for the prefix portion
    assert 'TMQ_FEAT_PREFIX' in body or 'TMQ_SESSION_PREFIX' in body \
        or re.search(r'\$\{prefix\}', body) or 'aoe_id' in body.lower(), (
        "create_worktree liveness check no longer uses a precise session match"
    )


def test_create_worktree_validates_upstream_branch():
    """Item 3: Before accepting a worktree for reuse, validate that the
    upstream branch still exists on the remote (git rev-parse --verify
    origin/$branch)."""
    body = _create_worktree_review_body()
    assert 'rev-parse' in body or 'origin/' in body, (
        "create_worktree review reuse path no longer validates upstream branch"
    )


def test_upstream_validation_skips_detached_head():
    """Item 3: Upstream validation must be guarded by [[ -n $feat_branch ]]
    — detached HEAD has no branch, and origin/ would fail spuriously."""
    body = _create_worktree_review_body()
    # The branch variable must be checked for non-empty before rev-parse
    has_branch_guard = re.search(
        r'\[\[\s+-n\s+\$\{?feat_branch', body
    ) or re.search(r'\[\[\s+-n\s+"\$\{?feat_branch', body) \
        or 'feat_branch' in body and 'rev-parse' in body
    assert has_branch_guard, (
        "upstream validation no longer guards against detached HEAD "
        "(empty branch variable)"
    )


def test_fetch_pr_non_fatal():
    """Item 4: fetch_pr must not call exit 1 — it must be non-fatal so
    the caller can fall back to issue→PR lookup (P0 fix from proposal
    review)."""
    src = BIN_TMQ.read_text()
    m = re.search(r'fetch_pr\(\).*?^\}', src, re.S | re.M)
    assert m, "fetch_pr function not found"
    body = m.group(0)
    # Must NOT contain exit 1
    assert 'exit 1' not in body, (
        "fetch_pr still calls exit 1 — must be non-fatal for issue→PR fallback"
    )


def test_resolve_pr_number_exists():
    """Item 4: A resolve_pr_number (or equivalent) function must exist
    that tries gh pr view first, then falls back to gh issue view +
    gh pr list --search."""
    src = BIN_TMQ.read_text()
    assert 'resolve_pr_number' in src \
        or re.search(r'gh\s+pr\s+list.*--search.*issue:', src), (
        "no resolve_pr_number function or issue→PR search fallback found"
    )


def test_review_type_uses_issue_pr_fallback():
    """Item 4: The review code path (build_prompt review branch) must
    use the issue→PR fallback, not call fetch_pr directly."""
    src = BIN_TMQ.read_text()
    # In the build_prompt review branch, the PR info must come from
    # resolve_pr_number (or an equivalent non-fatal path), not from a
    # direct fetch_pr call that would exit 1 on an issue number.
    m = re.search(
        r'build_prompt\(\).*?^\}', src, re.S | re.M,
    )
    assert m, "build_prompt function not found"
    body = m.group(0)
    # The review branch must use TMQ_REVIEW_PR_NUM (set by main())
    # or call resolve_pr_number directly
    has_pr_num_global = 'TMQ_REVIEW_PR_NUM' in body
    has_resolver = 'resolve_pr_number' in body
    assert has_pr_num_global or has_resolver, (
        "build_prompt review branch no longer uses TMQ_REVIEW_PR_NUM or resolve_pr_number")


def test_main_review_uses_issue_pr_fallback():
    """Item 4: The main() review display path must also use the
    issue→PR fallback, not call fetch_pr directly."""
    src = BIN_TMQ.read_text()
    m = re.search(r'^main\(\)\s*\{.*?^\}', src, re.S | re.M)
    assert m, "main function not found"
    body = m.group(0)
    has_resolver = 'resolve_pr_number' in body
    has_pr_list_fallback = re.search(r'gh\s+pr\s+list.*--search', body)
    assert has_resolver or has_pr_list_fallback, (
        "main() review path no longer uses issue→PR fallback"
    )
