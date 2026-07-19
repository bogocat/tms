"""Tests for lib/tms/diff_shape.py — specialist review routing (issue #94).

Covers each specialist signal (security, schema, duplication, editorial)
and edge cases (multi-signal, no-signal, empty diff).
"""

import pytest

from tms.diff_shape import classify_diff, SPECIALIST_SIGNALS


# ── Security signal ───────────────────────────────────────────────

class TestSecuritySignal:
    def test_auth_keyword_in_added_line(self):
        diff = """diff --git a/src/login.ts b/src/login.ts
+  const authToken = await authenticate(user);
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_crypto_keyword_triggers_security(self):
        diff = """diff --git a/utils/crypto.ts b/utils/crypto.ts
+  const hash = await crypto.subtle.digest('SHA-256', data);
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_password_in_deleted_line_no_signal(self):
        # Deleted lines only: a removal of security code is not a
        # positive security signal (it could be cleanup).
        diff = """diff --git a/config.ts b/config.ts
-  const password = process.env.DB_PASS;
"""
        result = classify_diff(diff)
        assert "security" not in result

    def test_security_file_path_triggers_signal(self):
        diff = """diff --git a/src/auth/login.ts b/src/auth/login.ts
+  // updated comment
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_compound_path_authenticator_triggers_signal(self):
        diff = """diff --git a/src/authenticator.py b/src/authenticator.py
+  # updated
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_compound_path_crypto_helper_triggers_signal(self):
        diff = """diff --git a/utils/crypto_helper.py b/utils/crypto_helper.py
+  # updated
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_secret_file_triggers_signal(self):
        diff = """diff --git a/k8s/secrets.yaml b/k8s/secrets.yaml
+  apiKey: xyz
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_policy_file_triggers_signal(self):
        diff = """diff --git a/src/permissions/policy.ts b/src/permissions/policy.ts
+  allow: ['admin']
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_multiple_security_keywords(self):
        diff = """diff --git a/auth.ts b/auth.ts
+  const token = jwt.sign({ sub: user.id }, secret);
+  res.cookie('session', token, { httpOnly: true, secure: true });
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_csrf_keyword_triggers_security(self):
        diff = """diff --git a/middleware.ts b/middleware.ts
+  // Apply CSRF protection
+  app.use(csrf());
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_compound_identifiers_authToken(self):
        diff = """diff --git a/src/api.ts b/src/api.ts
+  const authToken = await refreshToken(req.sessionId);
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_compound_identifiers_passwordHash(self):
        diff = """diff --git a/src/user.ts b/src/user.ts
+  const hash = await bcrypt.hash(passwordHash, 10);
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_compound_identifiers_permissions_set(self):
        diff = """diff --git a/src/guard.ts b/src/guard.ts
+  if (!permissions.has('admin')) throw new ForbiddenError();
"""
        result = classify_diff(diff)
        assert "security" in result

    def test_compound_identifiers_oauth_token(self):
        diff = """diff --git a/src/provider.ts b/src/provider.ts
+  const oauthToken = await exchangeCode(code);
"""
        result = classify_diff(diff)
        assert "security" in result


# ── Schema signal ─────────────────────────────────────────────────

class TestSchemaSignal:
    def test_migration_file_path_triggers_signal(self):
        diff = """diff --git a/migrations/006_add_col.sql b/migrations/006_add_col.sql
+  ALTER TABLE foo ADD COLUMN bar TEXT;
"""
        result = classify_diff(diff)
        assert "schema" in result

    def test_sql_file_triggers_signal(self):
        diff = """diff --git a/schema.sql b/schema.sql
+  CREATE INDEX idx_foo ON foo (bar);
"""
        result = classify_diff(diff)
        assert "schema" in result

    def test_create_table_in_added_line(self):
        diff = """diff --git a/src/db.ts b/src/db.ts
+  await db.execute('CREATE TABLE users (id INT PRIMARY KEY)');
"""
        result = classify_diff(diff)
        assert "schema" in result

    def test_alter_table_in_added_line(self):
        diff = """diff --git a/src/migrate.ts b/src/migrate.ts
+  ALTER TABLE items ADD COLUMN price DECIMAL;
"""
        result = classify_diff(diff)
        assert "schema" in result

    def test_drop_table_in_added_line(self):
        diff = """diff --git a/src/cleanup.ts b/src/cleanup.ts
+  DROP TABLE IF EXISTS old_records;
"""
        result = classify_diff(diff)
        assert "schema" in result

    def test_schema_directory_triggers_signal(self):
        diff = """diff --git a/schema/latest.sql b/schema/latest.sql
+  -- comment only
"""
        result = classify_diff(diff)
        assert "schema" in result

    def test_alembic_file_triggers_signal(self):
        diff = """diff --git a/alembic/versions/abc123.py b/alembic/versions/abc123.py
+  def upgrade():
+      pass
"""
        result = classify_diff(diff)
        assert "schema" in result


# ── Duplication signal ────────────────────────────────────────────

class TestDuplicationSignal:
    def test_over_40_percent_deletions(self):
        # 5 lines deleted, 2 added → 5/7 = 71% deletions → signal
        diff = """diff --git a/src/refactor.ts b/src/refactor.ts
-  line1
-  line2
-  line3
-  line4
-  line5
+  newLine1
+  newLine2
"""
        result = classify_diff(diff)
        assert "duplication" in result

    def test_under_40_percent_no_signal(self):
        # 1 deleted, 3 added → 1/4 = 25% → no signal
        diff = """diff --git a/src/small.ts b/src/small.ts
-  old
+  new1
+  new2
+  new3
"""
        result = classify_diff(diff)
        assert "duplication" not in result

    def test_exactly_40_percent_no_signal(self):
        # 2 deleted, 3 added → 2/5 = 40% → no signal (>40% required)
        diff = """diff --git a/src/edge.ts b/src/edge.ts
-  old1
-  old2
+  new1
+  new2
+  new3
"""
        result = classify_diff(diff)
        assert "duplication" not in result

    def test_duplicated_added_blocks(self):
        # Same + line appears twice → signal
        diff = """diff --git a/src/dup.ts b/src/dup.ts
+  const x = computeValue(input);
+  const y = transform(x);
+  const x = computeValue(input);
"""
        result = classify_diff(diff)
        assert "duplication" in result

    def test_no_additions_no_duplication(self):
        diff = """diff --git a/README.md b/README.md
-  Old docs line
"""
        result = classify_diff(diff)
        assert "duplication" not in result


# ── Editorial signal ──────────────────────────────────────────────

class TestEditorialSignal:
    def test_markdown_only(self):
        diff = """diff --git a/README.md b/README.md
+  # New section
+  Content here.
diff --git a/docs/guide.md b/docs/guide.md
+  Updated guide text.
"""
        result = classify_diff(diff)
        assert "editorial" in result

    def test_rst_only(self):
        diff = """diff --git a/docs/api.rst b/docs/api.rst
+  New section
+  ===========
"""
        result = classify_diff(diff)
        assert "editorial" in result

    def test_code_and_docs_mixed_no_editorial(self):
        diff = """diff --git a/README.md b/README.md
+  Updated readme
diff --git a/src/app.ts b/src/app.ts
+  console.log('hello');
"""
        result = classify_diff(diff)
        assert "editorial" not in result

    def test_txt_file_triggers_editorial(self):
        diff = """diff --git a/CHANGELOG.txt b/CHANGELOG.txt
+  v1.2.3 - Bug fixes
"""
        result = classify_diff(diff)
        assert "editorial" in result

    def test_license_only(self):
        diff = """diff --git a/LICENSE b/LICENSE
+  Copyright 2026
"""
        result = classify_diff(diff)
        assert "editorial" in result


# ── Multi-signal ──────────────────────────────────────────────────

class TestMultiSignal:
    def test_security_and_schema(self):
        diff = """diff --git a/auth/migration.sql b/auth/migration.sql
+  ALTER TABLE users ADD COLUMN password_hash TEXT;
+  CREATE INDEX idx_tokens ON sessions (token);
"""
        result = classify_diff(diff)
        assert "security" in result
        assert "schema" in result

    def test_all_signals(self):
        # Contrived: file path is auth, is a migration, has deletions
        # AND duplicated blocks. Should hit all four.
        diff = """diff --git a/src/auth/migrations/006.sql b/src/auth/migrations/006.sql
+  ALTER TABLE users ADD COLUMN token TEXT;
+  -- same block
-  old_col1
-  old_col2
-  old_col3
-  old_col4
+  new_col1
"""
        result = classify_diff(diff)
        assert "security" in result
        assert "schema" in result
        assert "duplication" in result

    def test_security_and_editorial_can_coexist(self):
        # Security file path triggers security, but all changed files
        # are docs → editorial only gets triggered if all files are docs.
        # This diff has an auth file, so editorial won't fire even
        # though the auth file could be a doc.
        diff = """diff --git a/auth/README.md b/auth/README.md
+  Auth module documentation
"""
        result = classify_diff(diff)
        assert "security" in result
        # Editorial only fires when ALL files are docs. Here the file
        # is a doc by extension but its path triggers security first.
        # The editorial check looks at extensions, security at path
        # patterns — they can co-exist since the file is both .md
        # and in auth/.
        assert "editorial" in result


# ── No signal ─────────────────────────────────────────────────────

class TestNoSignal:
    def test_plain_typescript_diff(self):
        diff = """diff --git a/src/utils.ts b/src/utils.ts
+  export function add(a: number, b: number): number {
+    return a + b;
+  }
"""
        result = classify_diff(diff)
        assert result == set()

    def test_empty_diff(self):
        assert classify_diff("") == set()

    def test_only_file_headers(self):
        diff = """diff --git a/src/app.ts b/src/app.ts
--- a/src/app.ts
+++ b/src/app.ts
"""
        result = classify_diff(diff)
        assert result == set()

    def test_binary_file_diff(self):
        diff = """diff --git a/icon.png b/icon.png
Binary files differ
"""
        result = classify_diff(diff)
        assert result == set()


# ── SPECIALIST_SIGNALS constant ────────────────────────────────────

class TestSpecialistSignalsConstant:
    def test_contains_all_four_signals(self):
        assert SPECIALIST_SIGNALS == frozenset({
            "security", "schema", "duplication", "editorial",
        })

    def test_all_classify_results_are_valid_signals(self):
        # Every signal returned by classify_diff must be in
        # SPECIALIST_SIGNALS (no typos, no orphaned signals).
        for diff_text in _ALL_TEST_DIFFS:
            result = classify_diff(diff_text)
            for signal in result:
                assert signal in SPECIALIST_SIGNALS, (
                    f"classify_diff returned unknown signal '{signal}'"
                )


# Sample diffs for signal-validation test
_ALL_TEST_DIFFS = [
    # security
    "diff --git a/auth.ts b/auth.ts\n+  const token = jwt.sign({});\n",
    # schema
    "diff --git a/migrations/001.sql b/migrations/001.sql\n+  CREATE TABLE x (id INT);\n",
    # duplication
    "diff --git a/x.ts b/x.ts\n- a\n- b\n- c\n+ d\n",
    # editorial
    "diff --git a/README.md b/README.md\n+  text\n",
    # none
    "diff --git a/src/utils.ts b/src/utils.ts\n+  const x = 1;\n",
]
