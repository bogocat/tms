"""tms#137: the suite must be hermetic to git hook environment variables.

git exports an absolute GIT_DIR to hooks; if it survives into test
subprocesses, fixture git calls operate on the real repo family. conftest
strips all GIT_* at import — this pins that contract.
"""

import os


def test_no_git_env_leaks_into_suite():
    leaked = [k for k in os.environ if k.startswith("GIT_")]
    assert not leaked, f"conftest.py must strip git hook env vars: {leaked}"
