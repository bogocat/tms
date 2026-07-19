"""Diff-shape classifier for specialist review routing (issue #94).

Analyzes a unified diff for specialist routing signals. Each signal maps
to a specialist reviewer persona that gets added to the default review
panel (composition rule: specialists ADD, never replace).

Signals:
  - security:  auth/crypto/session/permission keywords or file paths
  - schema:    migration/SQL files, DDL keywords
  - duplication: >40% deleted lines or duplicated added blocks
  - editorial: all changed files are docs (fast-path, generalist still runs)

Public API:
  - SPECIALIST_SIGNALS — frozenset of all valid signal names
  - classify_diff(diff_text) -> set[str]
"""

import re


SPECIALIST_SIGNALS = frozenset([
    "security",
    "schema",
    "duplication",
    "editorial",
])


# ── Keyword / pattern sets ────────────────────────────────────────

_SECURITY_KEYWORDS = re.compile(
    r'^\+\s*.*\b(?:'
    r'auth(?:enticate|oriz|entication|orization|n)?'
    r'|crypto(?:graphic|\.subtle)?'
    r'|session(?:s|Id|Token|Key)?'
    r'|token(?:s|Id|Hash|Secret)?'
    r'|password(?:s|Hash)?'
    r'|secret(?:s|Key|Id)?'
    r'|permission(?:s|Check|Set|Denied)?'
    r'|polic(?:y|ies)'
    r'|csrf(?:Token|Protect)?'
    r'|xss(?:Protect|Escape)?'
    r'|jwt(?:Verify|Sign|Decode)?'
    r'|oauth(?:Token|Client|Callback)?'
    r')'
    r'(?:\b|[A-Z_])',
    re.IGNORECASE,
)

_SECURITY_PATH_RE = re.compile(
    r'diff --git a/(?:.*/)?(?:'
    r'auth|secrets?|crypto|permission|policy'
    r')(?:/|\.)',
    re.IGNORECASE,
)

_SCHEMA_PATH_RE = re.compile(
    r'diff --git a/(?:.*/)?(?:'
    r'migration|schema|alembic'
    r')(?:/|\.)|'
    r'diff --git a/.*\.sql\b',
    re.IGNORECASE,
)

_SCHEMA_DDL_RE = re.compile(
    r'^\+\s*.*\b(?:'
    r'CREATE\s+TABLE'
    r'|ALTER\s+TABLE'
    r'|DROP\s+TABLE'
    r'|ADD\s+COLUMN'
    r'|CREATE\s+INDEX'
    r')\b',
    re.IGNORECASE,
)

_DOC_EXTENSION_RE = re.compile(
    r'\.(?:md|rst|txt)$',
    re.IGNORECASE,
)

_DOC_PATH_RE = re.compile(
    r'diff --git a/(?:'
    r'docs?/|README|CHANGELOG|LICENSE'
    r')',
    re.IGNORECASE,
)

_FILE_HEADER_RE = re.compile(r'^diff --git ')


# ── Classification ────────────────────────────────────────────────

def classify_diff(diff_text):
    """Analyze a unified diff and return the set of specialist signals.

    Args:
        diff_text: The full unified diff as a string.

    Returns:
        A ``set[str]`` of signal names, each a member of
        ``SPECIALIST_SIGNALS``. An empty set means generalist-only
        panel (no specialist routing signals detected).
    """
    if not diff_text or not diff_text.strip():
        return set()

    lines = diff_text.splitlines()
    signals = set()

    # ── security ──────────────────────────────────────────────
    # Keyword match in added lines OR file path pattern match.
    for line in lines:
        if line.startswith('+') and _SECURITY_KEYWORDS.search(line):
            signals.add('security')
            break
    else:
        for line in lines:
            if line.startswith('diff --git') and _SECURITY_PATH_RE.search(line):
                signals.add('security')
                break

    # ── schema ────────────────────────────────────────────────
    # File path match (migration/sql) OR DDL keyword in added line.
    for line in lines:
        if line.startswith('diff --git') and _SCHEMA_PATH_RE.search(line):
            signals.add('schema')
            break
    else:
        for line in lines:
            if line.startswith('+') and _SCHEMA_DDL_RE.search(line):
                signals.add('schema')
                break

    # ── duplication ───────────────────────────────────────────
    # >40% of changed lines are deletions (AND at least one addition —
    # pure-removal diffs are cleanup, not copy-paste refactors), OR
    # duplicated + blocks.
    added_lines = [l for l in lines if l.startswith('+') and not l.startswith('+++')]
    deleted_lines = [l for l in lines if l.startswith('-') and not l.startswith('---')]
    total_changed = len(added_lines) + len(deleted_lines)
    if len(added_lines) > 0:
        if total_changed > 0:
            deletion_ratio = len(deleted_lines) / total_changed
            if deletion_ratio > 0.4:
                signals.add('duplication')

    # Duplicated added blocks (same content line appears 2+ times)
    if 'duplication' not in signals:
        added_content = [
            l[1:].strip() for l in added_lines if l[1:].strip()
        ]
        seen = set()
        for content in added_content:
            if content in seen:
                signals.add('duplication')
                break
            seen.add(content)

    # ── editorial ─────────────────────────────────────────────
    # ALL changed files are documentation.
    changed_files = [l for l in lines if l.startswith('diff --git')]
    if changed_files:
        all_docs = True
        for file_line in changed_files:
            # Extract the file path from "diff --git a/<path> b/<path>"
            parts = file_line.split()
            if len(parts) >= 3:
                path = parts[2]  # b/<path>
                if path.startswith('b/'):
                    path = path[2:]
                is_doc = bool(
                    _DOC_EXTENSION_RE.search(path) or
                    _DOC_PATH_RE.search(file_line)
                )
                if not is_doc:
                    all_docs = False
                    break
        if all_docs:
            signals.add('editorial')

    return signals
