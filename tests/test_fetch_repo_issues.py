"""Tests for fetch_repo_issues pagination (tms#51).

Replaces the hardcoded --limit 300 with a pagination loop so no
issues are silently dropped when a repo exceeds the cap.
"""

import json
import os
import stat
import subprocess
import tempfile

import pytest


def _extract_fetch_repo_issues(tms_path: str) -> str:
    """Extract the fetch_repo_issues function body from bin/tms."""
    import re

    with open(tms_path) as f:
        lines = f.readlines()

    in_func = False
    func_lines = []
    brace_depth = 0
    heredoc_delim = None
    for line in lines:
        if not in_func:
            if line.startswith("fetch_repo_issues()"):
                in_func = True
                func_lines.append(line)
                brace_depth = line.count("{") - line.count("}")
        else:
            # Track heredocs: braces inside them are not bash braces
            if heredoc_delim:
                func_lines.append(line)
                stripped = line.strip()
                if stripped == heredoc_delim or stripped.startswith(heredoc_delim):
                    heredoc_delim = None
                continue

            m = re.search(r"<<\s*'?(\w+)'?", line)
            if not m:
                m = re.search(r"<<-\s*'?(\w+)'?", line)
            if m:
                heredoc_delim = m.group(1)
                func_lines.append(line)
                continue

            func_lines.append(line)
            brace_depth += line.count("{") - line.count("}")
            if brace_depth == 0 and "{" in func_lines[0]:
                break

    assert func_lines, "could not find fetch_repo_issues in bin/tms"
    return "".join(func_lines)


def _generate_issues(start: int, end: int, created_at: str) -> list[dict]:
    """Generate a list of issue dicts with descending numbers."""
    issues = []
    for n in range(start, end - 1, -1):
        issues.append(
            {
                "number": n,
                "title": f"Issue {n}",
                "labels": [],
                "url": f"https://github.com/test/repo/issues/{n}",
                "createdAt": created_at,
                "updatedAt": created_at,
            }
        )
    return issues


def test_fetch_repo_issues_paginates_issues(tmp_path):
    """fetch_repo_issues paginates gh issue list when results exceed page limit.

    Creates a fake gh script that returns 250 issues across 3 pages
    (100 + 100 + 50). Verifies the merged output contains all 250.
    """
    tms_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "tms",
    )
    func_body = _extract_fetch_repo_issues(tms_path)

    # Pre-generate page payloads
    pages = {
        1: _generate_issues(250, 151, "2026-07-01T00:00:00Z"),
        2: _generate_issues(150, 51, "2026-06-01T00:00:00Z"),
        3: _generate_issues(50, 1, "2026-05-01T00:00:00Z"),
    }

    page_file = tmp_path / "page"
    page_file.write_text("1")

    gh_script = tmp_path / "gh"
    gh_script.write_text(
        f"""#!/bin/bash
# Fake gh for testing fetch_repo_issues pagination.
# Returns issues in pages of 100; page 3 has 50 (end condition).

PAGE_FILE="{page_file}"

if [[ "$1" == "issue" && "$2" == "list" ]]; then
    PAGE=$(cat "$PAGE_FILE" 2>/dev/null || echo 1)
    echo $((PAGE + 1)) > "$PAGE_FILE"

    if [[ "$PAGE" -eq 1 ]]; then
        cat <<'JSONEOF'
{json.dumps(pages[1])}
JSONEOF
    elif [[ "$PAGE" -eq 2 ]]; then
        cat <<'JSONEOF'
{json.dumps(pages[2])}
JSONEOF
    else
        cat <<'JSONEOF'
{json.dumps(pages[3])}
JSONEOF
    fi
elif [[ "$1" == "pr" && "$2" == "list" ]]; then
    echo '[]'
else
    echo "unexpected gh call: $*" >&2
    exit 1
fi
"""
    )
    gh_script.chmod(gh_script.stat().st_mode | stat.S_IEXEC)

    # Build the test script: source the function, call it with fake gh in PATH
    test_script = tmp_path / "test_runner.sh"
    test_script.write_text(
        f"""#!/bin/bash
set -euo pipefail

# Re-initialize page counter
echo 1 > "{page_file}"

# Define the function
{func_body}

# Run it with fake gh in PATH
export PATH="{tmp_path}:$PATH"
fetch_repo_issues test test/repo
"""
    )
    test_script.chmod(test_script.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        ["/bin/bash", str(test_script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Parse NDJSON output — each line is a JSON object
    lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
    assert len(lines) == 250, (
        f"Expected 250 issues, got {len(lines)}\n"
        f"stderr: {result.stderr[:500]}"
    )

    # Verify all numbers are present and in descending order (newest first)
    numbers = []
    for line in lines:
        obj = json.loads(line)
        assert obj["repo"] == "test"
        numbers.append(obj["number"])

    assert numbers == list(range(250, 0, -1)), (
        f"Expected numbers 250..1 in descending order, "
        f"got first 10: {numbers[:10]}, last 5: {numbers[-5:]}"
    )

    # Verify each issue has the expected fields
    first = json.loads(lines[0])
    assert set(first.keys()) == {
        "repo", "number", "title", "labels", "status", "createdAt", "updatedAt"
    }, f"Unexpected fields: {first.keys()}"


def test_fetch_repo_issues_empty_repo(tmp_path):
    """fetch_repo_issues handles repos with zero open issues gracefully."""
    tms_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "tms",
    )
    func_body = _extract_fetch_repo_issues(tms_path)

    page_file = tmp_path / "page"
    page_file.write_text("1")

    gh_script = tmp_path / "gh"
    gh_script.write_text(
        f"""#!/bin/bash
if [[ "$1" == "issue" && "$2" == "list" ]]; then
    echo '[]'
elif [[ "$1" == "pr" && "$2" == "list" ]]; then
    echo '[]'
fi
"""
    )
    gh_script.chmod(gh_script.stat().st_mode | stat.S_IEXEC)

    test_script = tmp_path / "test_runner.sh"
    test_script.write_text(
        f"""#!/bin/bash
set -euo pipefail
{func_body}
export PATH="{tmp_path}:$PATH"
fetch_repo_issues empty empty/repo
"""
    )
    test_script.chmod(test_script.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        ["/bin/bash", str(test_script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    lines = [l.strip() for l in result.stdout.split("\n") if l.strip()]
    assert len(lines) == 0, (
        f"Expected 0 issues for empty repo, got {len(lines)}\n"
        f"stdout: {result.stdout[:500]}"
    )


def test_fetch_repo_issues_exactly_one_page(tmp_path):
    """fetch_repo_issues doesn't loop when results < page limit."""
    tms_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "tms",
    )
    func_body = _extract_fetch_repo_issues(tms_path)

    page_file = tmp_path / "page"
    page_file.write_text("1")

    # Only 50 issues — less than the 100 page limit
    issues = _generate_issues(50, 1, "2026-07-01T00:00:00Z")
    gh_script = tmp_path / "gh"
    gh_script.write_text(
        f"""#!/bin/bash
PAGE_FILE="{page_file}"
if [[ "$1" == "issue" && "$2" == "list" ]]; then
    PAGE=$(cat "$PAGE_FILE" 2>/dev/null || echo 1)
    echo $((PAGE + 1)) > "$PAGE_FILE"
    cat <<'JSONEOF'
{json.dumps(issues)}
JSONEOF
elif [[ "$1" == "pr" && "$2" == "list" ]]; then
    echo '[]'
fi
"""
    )
    gh_script.chmod(gh_script.stat().st_mode | stat.S_IEXEC)

    test_script = tmp_path / "test_runner.sh"
    test_script.write_text(
        f"""#!/bin/bash
set -euo pipefail
echo 1 > "{page_file}"
{func_body}
export PATH="{tmp_path}:$PATH"
fetch_repo_issues small small/repo
"""
    )
    test_script.chmod(test_script.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        ["/bin/bash", str(test_script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    lines = [l.strip() for l in result.stdout.split("\n") if l.strip()]
    assert len(lines) == 50, (
        f"Expected 50 issues, got {len(lines)}\n"
        f"stderr: {result.stderr[:500]}"
    )


def test_fetch_repo_issues_includes_pr_status(tmp_path):
    """PR status enrichment still works after pagination changes."""
    tms_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "tms",
    )
    func_body = _extract_fetch_repo_issues(tms_path)

    gh_script = tmp_path / "gh"
    gh_script.write_text(
        f"""#!/bin/bash
if [[ "$1" == "issue" && "$2" == "list" ]]; then
    cat <<'JSONEOF'
[{json.dumps({"number": 42, "title": "Test issue", "labels": [{"name":"bug"}], "url": "https://github.com/test/repo/issues/42", "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-02T00:00:00Z"})}]
JSONEOF
elif [[ "$1" == "pr" && "$2" == "list" ]]; then
    cat <<'JSONEOF'
[{json.dumps({"number": 99, "title": "Fix test (#42)", "body": "Closes #42", "headRefName": "fix/issue-42", "reviewDecision": "APPROVED", "url": "https://github.com/test/repo/pull/99"})}]
JSONEOF
fi
"""
    )
    gh_script.chmod(gh_script.stat().st_mode | stat.S_IEXEC)

    test_script = tmp_path / "test_runner.sh"
    test_script.write_text(
        f"""#!/bin/bash
set -euo pipefail
{func_body}
export PATH="{tmp_path}:$PATH"
fetch_repo_issues test test/repo
"""
    )
    test_script.chmod(test_script.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        ["/bin/bash", str(test_script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    lines = [l.strip() for l in result.stdout.split("\n") if l.strip()]
    assert len(lines) == 1

    issue = json.loads(lines[0])
    assert issue["number"] == 42
    assert issue["status"] == "ready-to-merge", (
        f"Expected 'ready-to-merge' (APPROVED PR linked), got '{issue['status']}'"
    )
    assert issue["repo"] == "test"
    assert issue["labels"] == "bug"
