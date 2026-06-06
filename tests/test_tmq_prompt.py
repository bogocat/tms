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


def test_cc_oc_branch_falls_back_to_agents_md():
    block = _agent_case_block()
    cc_arm = re.search(r'cc\|oc\)(.*?);;', block, re.S)
    assert cc_arm, "no cc|oc) arm in the loop_instruction case block"
    assert 'AGENTS.md' in cc_arm.group(1), \
        "cc/oc dispatch prompt lost the AGENTS.md fallback (they have no pi skills)"


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
    assert 'PI_DISPATCH_AUTOAPPROVE=1 pi @' in src, \
        "pi cmd_override no longer sets PI_DISPATCH_AUTOAPPROVE — dispatched pi stalls on prompts"
