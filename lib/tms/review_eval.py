"""Reviewer eval harness — the review-side counterpart to the coding harness.

Append-only JSONL logs for reviewer_runs, escaped_defects, and
defect_attributions — consistent with #53's events.jsonl pattern.

Public API:
  - log_reviewer_run(...)          — append a reviewer run record (AC1)
  - record_escaped_defect(...)     — append escaped_defect + attributions (AC2)
  - backjudge_propose(...)         — walk git history, propose attributions (AC3)
  - apply_attribution_rules(...)   — enforce rules 1-3 in code (AC4)
  - compute_review_stats(...)      — per-reviewer/author stats (AC6)
  - format_review_report(stats)    — pretty-print the report

Data paths:
  ~/.local/state/tmq/reviewer_runs.jsonl       (AC1) reviewer_runs
  ~/.local/state/tmq/escaped_defects.jsonl     (AC2) escaped_defects
  ~/.local/state/tmq/defect_attributions.jsonl (AC2) defect_attributions

The append uses Python's open(path, 'a') which maps to O_APPEND:
POSIX guarantees atomic writes <= PIPE_BUF (~4KB). Each JSONL record
is <2KB, so concurrent appends are safe without locking.
"""

import datetime
import json
import os
import sys
import uuid


# ── Paths ─────────────────────────────────────────────────────────

REVIEWER_RUNS_PATH = os.path.expanduser(
    "~/.local/state/tmq/reviewer_runs.jsonl"
)
ESCAPED_DEFECTS_PATH = os.path.expanduser(
    "~/.local/state/tmq/escaped_defects.jsonl"
)
DEFECT_ATTRIBUTIONS_PATH = os.path.expanduser(
    "~/.local/state/tmq/defect_attributions.jsonl"
)


# ── Core append ───────────────────────────────────────────────────

def _append_jsonl(path, record):
    """Append one JSONL record to the given path (O_APPEND atomic)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with open(path, "a") as f:
        f.write(line)


# ── Validation ────────────────────────────────────────────────────

_VALID_DEFECT_CLASSES = frozenset([
    "sql-syntax", "logic", "auth", "schema",
    "data-loss", "perf", "convention",
])

_VALID_ATTRIBUTION_OUTCOMES = frozenset([
    "shipped", "missed", "flagged-but-rebutted", "not-in-scope",
])


def _validate_attributions(attributions):
    """Validate attribution list. Raises ValueError on invalid data."""
    for i, attr in enumerate(attributions):
        if not isinstance(attr, dict):
            raise ValueError(
                f"attribution[{i}] must be a dict, got {type(attr).__name__}"
            )
        if "run_source" not in attr:
            raise ValueError(
                f"attribution[{i}] missing required field 'run_source'"
            )
        if attr["run_source"] not in ("coding_runs", "reviewer_runs"):
            raise ValueError(
                f"attribution[{i}] run_source must be 'coding_runs' or "
                f"'reviewer_runs', got '{attr['run_source']}'"
            )
        if "run_id" not in attr:
            raise ValueError(
                f"attribution[{i}] missing required field 'run_id'"
            )
        if "role" not in attr:
            raise ValueError(
                f"attribution[{i}] missing required field 'role'"
            )
        role = attr.get("role")
        if role not in ("author", "reviewer"):
            raise ValueError(
                f"attribution[{i}] role must be 'author' or 'reviewer', "
                f"got '{role}'"
            )
        outcome = attr.get("outcome")
        if outcome is None:
            raise ValueError(
                f"attribution[{i}] missing required field 'outcome'"
            )
        if outcome not in _VALID_ATTRIBUTION_OUTCOMES:
            raise ValueError(
                f"attribution[{i}] outcome must be one of "
                f"{sorted(_VALID_ATTRIBUTION_OUTCOMES)}, got '{outcome}'"
            )


# ── AC2: escaped_defects + defect_attributions ────────────────────

def record_escaped_defect(
    repo,
    introducing_pr,
    introducing_commit,
    defect_class,
    severity,
    discovered_at,
    discovery_source,
    description,
    fix_pr,
    attributions,
    defect_id=None,
):
    """Record an escaped defect and its attributions.

    Writes one record to escaped_defects.jsonl (if defect_id is None,
    i.e. first discovery) and one record per attribution to
    defect_attributions.jsonl. Both are append-only: re-judgment
    (defect_id provided) adds new attribution rows but NEVER mutates
    original defect or attribution records.

    Args:
        repo: fleet shortname where the defect shipped
        introducing_pr: PR that introduced the defect
        introducing_commit: commit SHA that introduced the defect
        defect_class: one of sql-syntax|logic|auth|schema|data-loss|
                      perf|convention
        severity: free-form severity label (critical, major, minor)
        discovered_at: ISO 8601 when the defect was discovered
        discovery_source: ci|incident|fix-pr|manual
        description: human-readable description of the defect
        fix_pr: PR that fixed the defect (or same PR if amended)
        attributions: list of dicts with {run_source, run_id, role,
                      outcome}
        defect_id: optional existing defect ID for re-judgment.
                   When None (default), a new defect record is written
                   and a new defect_id is generated. When provided,
                   only attribution rows are appended.

    Returns:
        The defect_id (UUID string) for cross-reference.
    """
    if defect_class not in _VALID_DEFECT_CLASSES:
        raise ValueError(
            f"defect_class must be one of {sorted(_VALID_DEFECT_CLASSES)}, "
            f"got '{defect_class}'"
        )

    _validate_attributions(attributions)

    if defect_id is None:
        defect_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Write the defect record (append-only — never mutated)
        defect_record = {
            "defect_id": defect_id,
            "timestamp": now,
            "repo": repo,
            "introducing_pr": introducing_pr,
            "introducing_commit": introducing_commit,
            "defect_class": defect_class,
            "severity": severity,
            "discovered_at": discovered_at,
            "discovery_source": discovery_source,
            "description": description,
            "fix_pr": fix_pr,
        }
        _append_jsonl(ESCAPED_DEFECTS_PATH, defect_record)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Write attribution records (append-only — re-judgment adds new
    # rows, never mutates originals)
    for attr in attributions:
        attr_record = {
            "attribution_id": str(uuid.uuid4()),
            "defect_id": defect_id,
            "timestamp": now,
            "run_source": attr["run_source"],
            "run_id": attr["run_id"],
            "role": attr["role"],
            "outcome": attr["outcome"],
        }
        _append_jsonl(DEFECT_ATTRIBUTIONS_PATH, attr_record)

    return defect_id


def _read_jsonl_if_exists(path):
    """Read all JSONL records from path. Returns [] if not found."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    return records


# ── AC3: backjudge CLI ────────────────────────────────────────────


def backjudge_propose(repo_path, fix_commit):
    """Walk git history to propose defect attributions.

    1. Derives the introducing commit from fix_commit (via git log -S
       on keywords from the fix commit message).
    2. Finds the GitHub PR that introduced the defect (gh pr list).
    3. Looks up author coding_runs and reviewer_runs for that PR.
    4. Runs apply_attribution_rules to propose attributions.

    Args:
        repo_path: path to the local git repo
        fix_commit: the commit SHA that fixed the defect

    Returns:
        A proposal dict with keys: repo, introducing_pr,
        introducing_commit, fix_commit, defect_class, severity,
        discovered_at, discovery_source, description, fix_pr,
        attributions (list). The operator confirms before writing.

    The proposal is semi-automated on purpose: the operator reviews
    the proposed attributions and confirms before they're written to
    the append-only log.
    """
    import subprocess

    # 1. Get fix commit message to extract search terms
    result = subprocess.run(
        ["git", "-C", repo_path, "log", "-1", "--format=%s", fix_commit],
        capture_output=True, text=True, timeout=10,
    )
    fix_message = result.stdout.strip()

    # Extract search terms: look for SQL keywords, function names,
    # or quoted strings in the fix message
    search_terms = _extract_search_terms(fix_message)
    if not search_terms:
        # Fall back to looking at the diff itself
        search_terms = ["fix"]  # generic

    # 2. Find the introducing commit: git log -S for each search term,
    #    take the one before the fix commit (the non-fix entry)
    introducing_commit = None
    repo_name = os.path.basename(repo_path.rstrip("/"))
    for term in search_terms:
        result = subprocess.run(
            ["git", "-C", repo_path, "log",
             "-S", term, "--format=%H %s", "--", "src/"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        for line in lines:
            if not line:
                continue
            parts = line.split(" ", 1)
            commit_sha = parts[0]
            if commit_sha != fix_commit:
                introducing_commit = commit_sha
                break
        if introducing_commit:
            break

    if not introducing_commit:
        # Fallback: just use the parent of the fix commit
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "-1", "--format=%H",
             f"{fix_commit}^"],
            capture_output=True, text=True, timeout=10,
        )
        introducing_commit = result.stdout.strip()

    # 3. Find the introducing PR via gh CLI
    introducing_pr = None
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", f"bogocat/{repo_name}",
             "--search", introducing_commit,
             "--json", "number,mergeCommit,mergedAt",
             "--state", "merged", "--limit", "1"],
            capture_output=True, text=True, timeout=15,
        )
        prs = json.loads(result.stdout) if result.stdout.strip() else []
        if prs:
            introducing_pr = prs[0]["number"]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass

    # 4. Look up reviewer_runs for the introducing PR
    reviewer_runs = _read_jsonl_if_exists(REVIEWER_RUNS_PATH)
    matching_reviewers = [
        r for r in reviewer_runs
        if r.get("repo") == repo_name and r.get("pr_number") == introducing_pr
    ]

    # 5. Build reviewer run summaries for apply_attribution_rules
    #    We don't know which reviewers flagged the defect from logs
    #    alone — this is the semi-automated part: the operator reviews
    #    the finding lists and marks which reviewers flagged it.
    reviewer_summaries = []
    for rr in matching_reviewers:
        # Check if this reviewer had any non-zero findings
        # (a heuristic — the operator refines this during confirmation)
        reviewer_summaries.append({
            "run_source": "reviewer_runs",
            "run_id": rr.get("run_id", "unknown"),
            "diff_sha_reviewed": rr.get("diff_sha_reviewed", ""),
            "flagged_defect": False,  # operator must confirm
        })

    # 6. Look up coding_runs (stub — #52 not yet implemented)
    #    For now, propose a placeholder author run
    author_run = {
        "run_source": "coding_runs",
        "run_id": f"author-pr-{introducing_pr}",
    }

    # 7. Apply attribution rules
    attributions = apply_attribution_rules(
        introducing_diff_sha=introducing_commit,
        author_run=author_run,
        reviewer_runs=reviewer_summaries,
        author_rebuttals={},
    )

    return {
        "repo": repo_name,
        "introducing_pr": introducing_pr,
        "introducing_commit": introducing_commit,
        "fix_commit": fix_commit,
        "defect_class": _infer_defect_class(fix_message),
        "severity": _infer_severity(fix_message),
        "discovered_at": "",  # filled by operator
        "discovery_source": "manual",
        "description": fix_message,
        "fix_pr": introducing_pr,  # heuristic: same PR if amended
        "attributions": attributions,
    }


def _extract_search_terms(fix_message):
    """Extract search-able terms from a fix commit message."""
    terms = []
    # Look for quoted strings
    import re
    quoted = re.findall(r'["\']([^"\']+)["\']', fix_message)
    terms.extend(quoted)
    # SQL keywords
    sql_keywords = [
        "DISTINCT ON", "ORDER BY", "GROUP BY", "JOIN",
        "WHERE", "HAVING", "WINDOW", "PARTITION",
    ]
    lower_msg = fix_message.lower()
    for kw in sql_keywords:
        if kw.lower() in lower_msg:
            terms.append(kw)
    # Function names (camelCase or snake_case)
    funcs = re.findall(r'\b([a-z]+[A-Z][a-zA-Z]+|\w+_\w+)\b', fix_message)
    terms.extend([f for f in funcs if len(f) > 4])
    return terms[:5]  # max 5 terms to search


def _infer_defect_class(fix_message):
    """Heuristic: infer defect_class from fix commit message."""
    msg = fix_message.lower()
    if any(w in msg for w in ("syntax", "parse", "distinct on", "order by")):
        return "sql-syntax"
    if any(w in msg for w in ("auth", "permission", "authn")):
        return "auth"
    if any(w in msg for w in ("schema", "migration", "alter table")):
        return "schema"
    if any(w in msg for w in ("data loss", "delete", "drop", "truncate")):
        return "data-loss"
    if any(w in msg for w in ("slow", "perf", "index", "optimiz")):
        return "perf"
    if any(w in msg for w in ("convention", "lint", "style")):
        return "convention"
    return "logic"


def _infer_severity(fix_message):
    """Heuristic: infer severity from fix commit message."""
    msg = fix_message.lower()
    if any(w in msg for w in ("critical", "would 500", "crash", "data loss")):
        return "critical"
    if any(w in msg for w in ("major", "wrong", "incorrect")):
        return "major"
    return "minor"


def backjudge_confirm(proposal, defect_id=None):
    """Write a confirmed backjudge proposal to the append-only logs.

    This is the write side of backjudge: after the operator reviews
    the proposal and confirms it, this function writes the escaped
    defect and attributions using record_escaped_defect().

    Args:
        proposal: dict from backjudge_propose()
        defect_id: optional existing defect ID for re-judgment

    Returns:
        The defect_id (UUID string).
    """
    return record_escaped_defect(
        repo=proposal["repo"],
        introducing_pr=proposal["introducing_pr"],
        introducing_commit=proposal["introducing_commit"],
        defect_class=proposal["defect_class"],
        severity=proposal["severity"],
        discovered_at=proposal.get("discovered_at", ""),
        discovery_source=proposal.get("discovery_source", "manual"),
        description=proposal["description"],
        fix_pr=proposal.get("fix_pr", proposal["introducing_pr"]),
        attributions=proposal["attributions"],
        defect_id=defect_id,
    )


# ── AC4: attribution rules 1-3 ───────────────────────────────────


def apply_attribution_rules(
    introducing_diff_sha,
    author_run,
    reviewer_runs,
    author_rebuttals,
):
    """Apply the three attribution rules to proposed attributions.

    Rules (encoded here, NEVER left to operator judgment):

    1. Round-scoping: a reviewer is only charged with a miss if the
       defect was present in the diff SHA they actually reviewed.
    2. Rebutted-finding flip: if a reviewer flagged the defect and
       the author rebutted/ignored it, that's a reviewer *hit*
       (flagged-but-rebutted) and an author miss (shipped).
    3. Individual panel misses: every reviewer who saw the offending
       hunk and didn't flag it gets a 'missed' row. Panel-level
       stats are derived, not stored.

    Args:
        introducing_diff_sha: the commit SHA that introduced the defect
        author_run: dict with {run_source, run_id} for the author
        reviewer_runs: list of dicts with {run_source, run_id,
                       diff_sha_reviewed, flagged_defect}
        author_rebuttals: dict mapping reviewer_run_id -> bool
                         (True = author rebutted the finding)

    Returns:
        List of attribution dicts:
        [{run_source, run_id, role, outcome}, ...]
        Always includes the author row + one row per reviewer.
    """
    attributions = []

    # Author always gets a shipped row when a defect escapes
    attributions.append({
        "run_source": author_run["run_source"],
        "run_id": author_run["run_id"],
        "role": "author",
        "outcome": "shipped",
    })

    for reviewer in reviewer_runs:
        run_source = reviewer["run_source"]
        run_id = reviewer["run_id"]
        diff_sha_reviewed = reviewer["diff_sha_reviewed"]
        flagged = reviewer.get("flagged_defect", False)
        rebutted = author_rebuttals.get(run_id, False)

        # Rule 1: round-scoping — reviewer not charged if they didn't
        # review the commit that introduced the defect
        if diff_sha_reviewed != introducing_diff_sha:
            attributions.append({
                "run_source": run_source,
                "run_id": run_id,
                "role": "reviewer",
                "outcome": "not-in-scope",
            })
            continue

        # Rule 2: rebutted-finding flip
        if flagged and rebutted:
            # Reviewer flagged the defect; author rebutted/ignored.
            # This is a reviewer HIT (flagged-but-rebutted) — do not
            # score against the reviewer.
            attributions.append({
                "run_source": run_source,
                "run_id": run_id,
                "role": "reviewer",
                "outcome": "flagged-but-rebutted",
            })
            continue

        # Rule 3: individual miss — every reviewer who saw the hunk
        # and didn't flag (or flagged but not rebutted, yet defect
        # still escaped) gets a 'missed' row.
        attributions.append({
            "run_source": run_source,
            "run_id": run_id,
            "role": "reviewer",
            "outcome": "missed",
        })

    return attributions


# ── AC5: seeded-defect gold set ──────────────────────────────────

# Path to the seeded-gold manifest (relative to the module, resolved
# via __file__ when needed by consumers).
_SEEDED_GOLD_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "seeded_gold",
)


def load_seeded_gold_manifest():
    """Load the seeded-gold manifest and validate it.

    Returns:
        dict with key 'fixtures' → list of fixture metadata dicts.

    Raises:
        ValueError: if the manifest fails structural validation.
    """
    manifest_path = os.path.join(_SEEDED_GOLD_DIR, "manifest.yaml")

    # We use a local import to keep pyyaml optional for tests.
    # If pyyaml is not installed, fall back to a simple YAML parser
    # that handles the subset our manifest uses.
    try:
        import yaml
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
    except ImportError:
        manifest = _parse_simple_yaml(manifest_path)

    if not isinstance(manifest, dict):
        raise ValueError(
            f"Seeded-gold manifest is not a dict: {manifest_path}"
        )
    fixtures = manifest.get("fixtures", [])
    if not isinstance(fixtures, list) or len(fixtures) < 5:
        raise ValueError(
            f"Seeded-gold manifest must have >= 5 fixtures, "
            f"got {len(fixtures) if isinstance(fixtures, list) else type(fixtures).__name__}"
        )

    required_fields = [
        "id", "defect_class", "severity", "description",
        "expected_detection", "fixture_file",
    ]
    seen_ids = set()
    seen_classes = set()
    for i, fixture in enumerate(fixtures):
        for field in required_fields:
            if field not in fixture:
                raise ValueError(
                    f"Fixture {i} missing required field '{field}'"
                )
        fid = fixture["id"]
        if fid in seen_ids:
            raise ValueError(f"Duplicate fixture id: {fid}")
        seen_ids.add(fid)
        dc = fixture["defect_class"]
        if dc not in _VALID_DEFECT_CLASSES:
            raise ValueError(
                f"Fixture {fid}: defect_class '{dc}' not in "
                f"{sorted(_VALID_DEFECT_CLASSES)}"
            )
        seen_classes.add(dc)

        # Verify fixture file exists
        fixture_path = _resolve_fixture_path(fixture)
        if not os.path.exists(fixture_path):
            raise ValueError(
                f"Fixture {fid}: file not found: {fixture_path}"
            )

    # Check that all 7 defect classes are represented
    missing_classes = _VALID_DEFECT_CLASSES - seen_classes
    if missing_classes:
        raise ValueError(
            f"Seeded-gold manifest missing defect classes: "
            f"{sorted(missing_classes)}"
        )

    return manifest


def _resolve_fixture_path(fixture):
    """Resolve the absolute path to a fixture's diff file."""
    fixture_file = fixture["fixture_file"]
    if os.path.isabs(fixture_file):
        return fixture_file
    return os.path.join(_SEEDED_GOLD_DIR, fixture_file)


def load_fixture_diff(fixture):
    """Load the diff content for a fixture.

    Returns:
        str: the raw diff content.
    """
    path = _resolve_fixture_path(fixture)
    with open(path) as f:
        return f.read()


def run_seeded_fixture(fixture, reviewer_fn, seed_result_path=None):
    """Run a single seeded fixture against a reviewer function.

    Args:
        fixture: fixture metadata dict from the manifest
        reviewer_fn: callable(diff_text) -> dict with keys:
                     {detected: bool, false_positives: int,
                      model: str, provider: str, wall_time_ms: int}
                     For tests, use a deterministic stub.
                     For production, dispatches a real reviewer agent.
        seed_result_path: optional path to append results JSONL.
                          Defaults to ~/.local/state/tmq/seeded_results.jsonl.

    Returns:
        dict: result record (also appended to seed_result_path).
    """
    diff_text = load_fixture_diff(fixture)
    review_result = reviewer_fn(diff_text)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = {
        "result_id": str(uuid.uuid4()),
        "timestamp": now,
        "fixture_id": fixture["id"],
        "defect_class": fixture["defect_class"],
        "expected_detection": fixture["expected_detection"],
        "known_false_positive": fixture.get(
            "known_false_positive", False
        ),
        "model": review_result.get("model", "unknown"),
        "provider": review_result.get("provider", "unknown"),
        "detected": review_result.get("detected", False),
        "false_positives": review_result.get("false_positives", 0),
        "wall_time_ms": review_result.get("wall_time_ms", 0),
    }

    if seed_result_path is None:
        seed_result_path = os.path.expanduser(
            "~/.local/state/tmq/seeded_results.jsonl"
        )
    _append_jsonl(seed_result_path, result)

    return result


def run_seeded_gold(reviewer_fn, seed_result_path=None):
    """Run all seeded-gold fixtures against a reviewer function.

    Args:
        reviewer_fn: same as run_seeded_fixture
        seed_result_path: optional path for results JSONL

    Returns:
        list of result dicts (one per fixture).
    """
    manifest = load_seeded_gold_manifest()
    results = []
    for fixture in manifest["fixtures"]:
        result = run_seeded_fixture(
            fixture, reviewer_fn, seed_result_path,
        )
        results.append(result)
    return results


def _parse_simple_yaml(path):
    """Minimal YAML parser for the subset our manifest uses.

    Handles: top-level mappings, list items (start with '-'),
    string scalars (with > for folded blocks). Avoids pyyaml
    dependency for tests and lightweight deploys.
    """
    import re

    with open(path) as f:
        text = f.read()

    result = {}
    in_fixtures = False
    current_fixture = None
    current_key = None
    block_scalar = None
    block_lines = []

    for line in text.split("\n"):
        # Skip comments and blank
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            if block_scalar:
                block_lines.append(line)
            continue

        if block_scalar:
            # Check if this is still the block scalar continuation
            # (indented or starts with a non-key)
            if line.startswith("  ") or line.startswith("\t"):
                block_lines.append(stripped)
                continue
            else:
                # End of block scalar
                current_fixture[block_scalar] = " ".join(block_lines)
                block_scalar = None
                block_lines = []

        # Top-level key
        m = re.match(r'^(\w+):\s*$', line)
        if m:
            key = m.group(1)
            if key == "fixtures":
                result["fixtures"] = []
                in_fixtures = True
            continue

        # Fixture list item
        m = re.match(r'^\s+-\s+(\w+):\s*(.*)$', line)
        if m and in_fixtures:
            key = m.group(1)
            val = m.group(2).strip()
            if key == "id":
                current_fixture = {}
                result["fixtures"].append(current_fixture)
            if current_fixture is not None:
                current_fixture[key] = _parse_yaml_value(val)
            continue

        # Indented key-value
        m = re.match(r'^\s+(\w+):\s*(.*)$', line)
        if m and current_fixture is not None:
            key = m.group(1)
            val = m.group(2).strip()
            if val == ">" or val == ">-":
                block_scalar = key
                block_lines = []
            else:
                current_fixture[key] = _parse_yaml_value(val)
            continue

    # Close any remaining block scalar
    if block_scalar and current_fixture:
        current_fixture[block_scalar] = " ".join(block_lines)

    return result


def _parse_yaml_value(val):
    """Parse a YAML scalar value."""
    val = val.strip()
    if val in ("true", "True"):
        return True
    if val in ("false", "False"):
        return False
    if val in ("null", "~", ""):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        pass
    # Remove surrounding quotes
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    return val


# ── AC6: reports ──────────────────────────────────────────────────

# Path for seeded-gold result runs
SEEDED_RESULTS_PATH = os.path.expanduser(
    "~/.local/state/tmq/seeded_results.jsonl"
)


def compute_review_stats():
    """Read all JSONL logs and compute per-reviewer/author stats.

    Reads from:
      - reviewer_runs.jsonl
      - escaped_defects.jsonl
      - defect_attributions.jsonl
      - seeded_results.jsonl

    Returns a dict with keys:
      - total_reviewer_runs, total_escaped_defects, total_seeded_runs
      - per_reviewer: {model: {total_reviews, avg_findings, ...}}
      - per_author: {author_run_id: {escaped_defects, models, ...}}
      - per_defect_class: {class: {detected, missed, fp, ...}}
      - panel_uniqueness: {model: unique_finding_count}
      - panel_suggestions: list of suggested panel compositions
    """
    # Read data
    reviewer_runs = _read_jsonl_if_exists(REVIEWER_RUNS_PATH)
    defects = _read_jsonl_if_exists(ESCAPED_DEFECTS_PATH)
    attributions = _read_jsonl_if_exists(DEFECT_ATTRIBUTIONS_PATH)
    seeded_results = _read_jsonl_if_exists(SEEDED_RESULTS_PATH)

    # ── Per-reviewer stats ──────────────────────────────────────
    per_reviewer = {}
    for rr in reviewer_runs:
        model = rr.get("model", rr.get("reviewer_agent", "unknown"))
        if model not in per_reviewer:
            per_reviewer[model] = {
                "total_reviews": 0,
                "total_p0": 0,
                "total_p1": 0,
                "total_p2": 0,
                "total_wall_time_ms": 0,
                "finding_lists": [],
            }
        entry = per_reviewer[model]
        entry["total_reviews"] += 1
        entry["total_p0"] += rr.get("p0", 0)
        entry["total_p1"] += rr.get("p1", 0)
        entry["total_p2"] += rr.get("p2", 0)
        entry["total_wall_time_ms"] += rr.get("wall_time_ms", 0)
        if rr.get("findings"):
            entry["finding_lists"].append(rr["findings"])

    # ── Per-author stats from defect_attributions ───────────────
    per_author = {}
    for attr in attributions:
        if attr.get("role") != "author":
            continue
        author_id = attr.get("run_id", "unknown")
        if author_id not in per_author:
            per_author[author_id] = {
                "escaped_defects": 0,
                "defect_ids": [],
            }
        per_author[author_id]["escaped_defects"] += 1
        per_author[author_id]["defect_ids"].append(
            attr.get("defect_id", "")
        )

    # ── Per-defect-class stats from seeded results ──────────────
    per_defect_class = {}
    for sr in seeded_results:
        dc = sr.get("defect_class", "unknown")
        if dc not in per_defect_class:
            per_defect_class[dc] = {
                "total": 0, "detected": 0, "missed": 0, "fp": 0,
                "by_model": {},
            }
        entry = per_defect_class[dc]
        entry["total"] += 1
        if sr.get("detected"):
            entry["detected"] += 1
        else:
            entry["missed"] += 1
        entry["fp"] += sr.get("false_positives", 0)
        model = sr.get("model", "unknown")
        if model not in entry["by_model"]:
            entry["by_model"][model] = {
                "detected": 0, "missed": 0, "fp": 0,
            }
        if sr.get("detected"):
            entry["by_model"][model]["detected"] += 1
        else:
            entry["by_model"][model]["missed"] += 1
        entry["by_model"][model]["fp"] += sr.get(
            "false_positives", 0
        )

    # ── Panel uniqueness ───────────────────────────────────────
    panel_uniqueness = {}
    panels = {}
    for rr in reviewer_runs:
        key = (rr.get("repo"), rr.get("pr_number"),
               rr.get("review_round"))
        panels.setdefault(key, []).append(rr)

    for panel_key, panel_runs in panels.items():
        if len(panel_runs) < 2:
            continue
        for rr in panel_runs:
            model = rr.get("model", rr.get("reviewer_agent", "unknown"))
            findings = rr.get("findings", [])
            if not findings:
                continue
            other_findings = set()
            for other in panel_runs:
                if other.get("run_id") == rr.get("run_id"):
                    continue
                for f in other.get("findings", []):
                    sig = (f.get("file", ""), f.get("severity", ""))
                    other_findings.add(sig)

            unique_count = 0
            for f in findings:
                sig = (f.get("file", ""), f.get("severity", ""))
                if sig not in other_findings:
                    unique_count += 1

            if model not in panel_uniqueness:
                panel_uniqueness[model] = {
                    "unique_findings": 0,
                    "panels_participated": 0,
                }
            panel_uniqueness[model]["unique_findings"] += unique_count
            panel_uniqueness[model]["panels_participated"] += 1

    # ── Panel composition suggestions ──────────────────────────
    panel_suggestions = []
    known_models = sorted(
        set(
            sr.get("model", "") for sr in seeded_results
            if sr.get("model")
        )
    )
    if known_models:
        panel_suggestions.append({
            "target": "detection_rate_p90",
            "models": known_models[:3],
            "note": (
                "Panel suggestion is v1 — uses all known models. "
                "Future: compute cheapest subset clearing 90% detection."
            ),
        })

    return {
        "total_reviewer_runs": len(reviewer_runs),
        "total_escaped_defects": len(defects),
        "total_seeded_runs": len(seeded_results),
        "per_reviewer": per_reviewer,
        "per_author": per_author,
        "per_defect_class": per_defect_class,
        "panel_uniqueness": panel_uniqueness,
        "panel_suggestions": panel_suggestions,
    }


def format_review_report(stats, as_json=False):
    """Pretty-print the review eval stats report.

    If as_json=True, output as JSON for machine consumption.
    """
    if as_json:
        print(json.dumps(stats, indent=2, default=str))
        return

    print("=== Reviewer Eval Report ===")
    print()
    print(f"  Total reviewer runs:      {stats['total_reviewer_runs']}")
    print(f"  Total escaped defects:    {stats['total_escaped_defects']}")
    print(f"  Total seeded runs:        {stats['total_seeded_runs']}")
    print()

    pr = stats.get("per_reviewer", {})
    if pr:
        print("  Per-reviewer:")
        hdr = (
            f"  {'Model':<24} {'Runs':>5} {'P0':>5} "
            f"{'P1':>5} {'P2':>5} {'Avg Time':>10}"
        )
        print(hdr)
        sep = f"  {'─'*24} {'─'*5} {'─'*5} {'─'*5} {'─'*5} {'─'*10}"
        print(sep)
        for model, mstats in sorted(pr.items()):
            runs = mstats["total_reviews"]
            avg_ms = (
                mstats["total_wall_time_ms"] / runs
                if runs > 0 else 0
            )
            avg_sec = avg_ms / 1000
            print(
                f"  {model:<24} "
                f"{runs:>5} "
                f"{mstats['total_p0']:>5} "
                f"{mstats['total_p1']:>5} "
                f"{mstats['total_p2']:>5} "
                f"{avg_sec:>9.1f}s"
            )
        print()

    pa = stats.get("per_author", {})
    if pa:
        print("  Per-author escaped defects:")
        for author_id, astats in sorted(pa.items()):
            print(
                f"    {author_id}: {astats['escaped_defects']} "
                f"defect(s)"
            )
        print()

    pdc = stats.get("per_defect_class", {})
    if pdc:
        print("  Per-defect-class (seeded gold):")
        hdr = (
            f"  {'Class':<20} {'Total':>6} {'Detected':>9} "
            f"{'Missed':>7} {'FP':>5} {'Rate':>8}"
        )
        print(hdr)
        print(f"  {'─'*20} {'─'*6} {'─'*9} {'─'*7} {'─'*5} {'─'*8}")
        for dc, dcstats in sorted(pdc.items()):
            total = dcstats["total"]
            rate = (
                dcstats["detected"] / total * 100
                if total > 0 else 0
            )
            print(
                f"  {dc:<20} {total:>6} "
                f"{dcstats['detected']:>9} "
                f"{dcstats['missed']:>7} "
                f"{dcstats['fp']:>5} "
                f"{rate:>7.1f}%"
            )
        print()

    pu = stats.get("panel_uniqueness", {})
    if pu:
        print("  Panel uniqueness (findings one reviewer found alone):")
        for model, ustats in sorted(pu.items()):
            print(
                f"    {model}: {ustats['unique_findings']} unique "
                f"in {ustats['panels_participated']} panel(s)"
            )
        print()

    ps = stats.get("panel_suggestions", [])
    if ps:
        print("  Panel composition suggestions:")
        for suggestion in ps:
            print(
                f"    Target {suggestion['target']}: "
                f"{', '.join(suggestion['models'])}"
            )
            print(f"      {suggestion.get('note', '')}")
        print()


# ── AC7: backfill audiobook incident ─────────────────────────────


def backfill_audiobook_incident():
    """Backfill the 2026-06-05 audiobook DISTINCT ON incident.

    This records the first escaped-defect row for the fleet:
    - Repo: home-portal
    - Introducing PR: #102 (feat(audiobook): surface root user's
      listening on dashboard)
    - Introducing commit: e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4
    - Defect: DISTINCT ON placed after ORDER BY (sql-syntax)
    - Discovered: 2026-06-05 by GitHub Actions CI code review
    - Author: claude (shipped)
    - 3 multi-model reviewers: all missed

    Returns the defect_id for cross-reference.

    This is idempotent in the sense that record_escaped_defect
    accepts defect_id for re-judgment. The first call creates the
    defect record + 4 attributions. Re-running with the same
    defect_id appends only new attributions.
    """
    return record_escaped_defect(
        repo="home-portal",
        introducing_pr=102,
        introducing_commit="e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4",
        defect_class="sql-syntax",
        severity="critical",
        discovered_at="2026-06-05T06:27:08+00:00",
        discovery_source="ci",
        description=(
            "DISTINCT ON placed after ORDER BY instead of "
            "immediately after SELECT — a PostgreSQL syntax error "
            "that would 500 the widget on every render. "
            "Fix: commit ce0d29b61357a93fe0134c77e76a4d56aedbac33"
        ),
        fix_pr=102,
        attributions=[
            {
                "run_source": "coding_runs",
                "run_id": "pr-102-author-claude",
                "role": "author",
                "outcome": "shipped",
            },
            {
                "run_source": "reviewer_runs",
                "run_id": "pr-102-review-3-reviewer",
                "role": "reviewer",
                "outcome": "missed",
            },
            {
                "run_source": "reviewer_runs",
                "run_id": "pr-102-review-3-reviewer-m3",
                "role": "reviewer",
                "outcome": "missed",
            },
            {
                "run_source": "reviewer_runs",
                "run_id": "pr-102-review-3-reviewer-fast",
                "role": "reviewer",
                "outcome": "missed",
            },
        ],
    )


# ── AC1: reviewer_runs audit records (moved down — was above AC2) ─

def log_reviewer_run(
    repo,
    pr_number,
    review_round,
    reviewer_agent,
    model,
    provider_used,
    diff_sha_reviewed,
    p0,
    p1,
    p2,
    wall_time_ms,
    findings=None,
    input_tokens=None,
    output_tokens=None,
):
    """Append a reviewer_run record to reviewer_runs.jsonl.

    Args:
        repo: fleet shortname (tms, distillery, home-portal, ...)
        pr_number: GitHub PR number
        review_round: 1-indexed review round number
        reviewer_agent: agent name (reviewer, reviewer-m3, ...)
        model: model ID (deepseek-v4-pro, MiniMax-M3, ...)
        provider_used: provider name (deepseek, minimax, ...)
        diff_sha_reviewed: the exact diff SHA the reviewer examined
        p0: number of P0 findings
        p1: number of P1 findings
        p2: number of P2 findings
        wall_time_ms: wall-clock time in milliseconds
        findings: optional list of normalized finding dicts
        input_tokens: optional input token count
        output_tokens: optional output token count
    """
    record = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "repo": repo,
        "pr_number": pr_number,
        "review_round": review_round,
        "reviewer_agent": reviewer_agent,
        "model": model,
        "provider_used": provider_used,
        "diff_sha_reviewed": diff_sha_reviewed,
        "p0": p0,
        "p1": p1,
        "p2": p2,
        "wall_time_ms": wall_time_ms,
    }
    if findings is not None:
        record["findings"] = findings
    if input_tokens is not None:
        record["input_tokens"] = input_tokens
    if output_tokens is not None:
        record["output_tokens"] = output_tokens

    _append_jsonl(REVIEWER_RUNS_PATH, record)


# ── CLI entry point ───────────────────────────────────────────────


def main():
    """Entry point for `python3 -m tms.review_eval <subcommand>`.

    Subcommands:
      log-run <repo> <pr> <round> <agent> <model> <provider>
              <diff-sha> <p0> <p1> <p2> <wall-time-ms>
          Append a reviewer_runs record.

      report [--json]
          Compute and print the review eval stats report.

      backfill-audiobook
          Backfill the 2026-06-05 audiobook DISTINCT ON incident.

      seeded-run [--fixture-id ID]
          Run seeded-gold fixtures. Requires a reviewer binary.
    """
    if len(sys.argv) < 2:
        print(
            "usage: python3 -m tms.review_eval "
            "<log-run|report|backfill-audiobook|seeded-run> [...]",
            file=sys.stderr,
        )
        sys.exit(1)

    subcmd = sys.argv[1]

    if subcmd == "log-run":
        if len(sys.argv) < 13:
            print(
                "usage: python3 -m tms.review_eval log-run "
                "<repo> <pr> <round> <agent> <model> <provider> "
                "<diff-sha> <p0> <p1> <p2> <wall-time-ms> "
                "[--input-tokens N] [--output-tokens N]",
                file=sys.stderr,
            )
            sys.exit(1)
        repo = sys.argv[2]
        pr_number = int(sys.argv[3])
        review_round = int(sys.argv[4])
        agent = sys.argv[5]
        model = sys.argv[6]
        provider = sys.argv[7]
        diff_sha = sys.argv[8]
        p0 = int(sys.argv[9])
        p1 = int(sys.argv[10])
        p2 = int(sys.argv[11])
        wall_time_ms = int(sys.argv[12])

        input_tokens = None
        output_tokens = None
        args = sys.argv[13:]
        i = 0
        while i < len(args):
            if args[i] == "--input-tokens" and i + 1 < len(args):
                input_tokens = int(args[i + 1])
                i += 2
            elif args[i] == "--output-tokens" and i + 1 < len(args):
                output_tokens = int(args[i + 1])
                i += 2
            else:
                i += 1

        log_reviewer_run(
            repo=repo, pr_number=pr_number,
            review_round=review_round,
            reviewer_agent=agent, model=model,
            provider_used=provider,
            diff_sha_reviewed=diff_sha,
            p0=p0, p1=p1, p2=p2,
            wall_time_ms=wall_time_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        print("Logged reviewer run.")

    elif subcmd == "report":
        as_json = "--json" in sys.argv
        stats = compute_review_stats()
        format_review_report(stats, as_json=as_json)

    elif subcmd == "backfill-audiobook":
        defect_id = backfill_audiobook_incident()
        print(f"Backfilled audiobook incident: defect_id={defect_id}")

    elif subcmd == "seeded-run":
        print(
            "seeded-run: requires a reviewer callable. "
            "Use from Python module directly.",
            file=sys.stderr,
        )
        sys.exit(1)

    else:
        print(f"unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
