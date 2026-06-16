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
