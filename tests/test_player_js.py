import os
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_player_listening_rules_pass_under_node():
    """The player's position rules (state.mjs) are plain JS with their own tests."""
    r = subprocess.run(["node", "--test", os.path.join("tests", "js", "state.test.mjs")],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
