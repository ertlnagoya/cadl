"""Every shipped example must pass `cadl verify` with no failed check."""

from pathlib import Path

import pytest

from cadl.deadlock import detect_deadlocks
from cadl.parser import parse_file
from cadl.type_checker import type_check
from cadl.verifier import verify

EXAMPLES = sorted((Path(__file__).parent.parent / "examples").glob("*.cadl"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_verifies(path):
    sos = parse_file(path)

    tc = type_check(sos)
    assert tc.ok, f"type errors: {tc.errors}"

    failed = [r for r in verify(sos) if r.status == "failed"]
    assert not failed, "\n".join(str(r) for r in failed)

    deadlocks = [r for r in detect_deadlocks(sos) if r.status == "failed"]
    assert not deadlocks, "\n".join(str(r) for r in deadlocks)


def test_examples_found():
    assert EXAMPLES, "no example .cadl files found"
