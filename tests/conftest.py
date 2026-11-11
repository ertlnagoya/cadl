"""Fixtures shared by several test modules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="session")
def norms_eval_cases(tmp_path_factory):
    """Every `evaluate` call of tests/test_norms_eval.py with its trace,
    the text of the Python monitor generated from the file, the parsed
    file (pickled) and the result or error of `cadl norms-eval` (see
    tests/norms_monitor_cases.py). Recorded once per session."""
    recorded = tmp_path_factory.mktemp("norms_cases") / "recorded.json"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_norms_eval.py", "-q", "-x",
         "-p", "tests.norms_monitor_cases", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, CADL_NORMS_CASES=str(recorded), CADL_NORMS_CASES_SOS="1"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(recorded.read_text(encoding="utf-8"))
