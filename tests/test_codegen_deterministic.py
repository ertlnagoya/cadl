"""Generated code must not depend on Python's per-process hash seed.

Regenerating from an unchanged .cadl file has to give byte-identical
output; otherwise committed generated files show spurious diffs.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).parent.parent / "examples" / "sos_dsl_robot_delivery.cadl"

_SCRIPT = """
import hashlib, sys
from pathlib import Path
from cadl.parser import parse_file
from cadl.codegen import generate
out = Path(sys.argv[3])
generate(parse_file(Path(sys.argv[1])), out, target=sys.argv[2])
digest = hashlib.sha256()
for path in sorted(p for p in out.rglob("*") if p.is_file()):
    digest.update(path.relative_to(out).as_posix().encode())
    digest.update(path.read_bytes())
print(digest.hexdigest())
"""


def _digest(target: str, seed: int, out: Path) -> str:
    env = dict(os.environ, PYTHONHASHSEED=str(seed))
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(EXAMPLE), target, str(out)],
        capture_output=True, text=True, env=env, check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("target", ["unity-csharp", "python", "solidity", "opa"])
def test_output_is_identical_across_hash_seeds(target, tmp_path):
    digests = {
        _digest(target, seed, tmp_path / f"{target}-{seed}") for seed in (0, 1, 2, 3, 4)
    }
    assert len(digests) == 1


def test_terminal_states_follow_declaration_order(tmp_path):
    from cadl.codegen import generate
    from cadl.parser import parse_file

    generate(parse_file(EXAMPLE), tmp_path, target="unity-csharp")
    text = (tmp_path / "Generated" / "DeliverySlaContract.cs").read_text()
    body = text[text.index("private bool IsTerminal"):]
    body = body[: body.index("default: return false")]
    order = [line.split("State.")[1].split(":")[0] for line in body.splitlines() if "case " in line]
    assert order == ["Completed", "Violated", "Terminated"]
