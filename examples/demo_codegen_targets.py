#!/usr/bin/env python3
"""CADL Demo: Multi-Target Code Generation

Generates code for all three targets (Python, Solidity, OPA/Rego)
from the Supply Chain SoS example and compares the output.

Run:
    python examples/demo_codegen_targets.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cadl.parser import parse_file
from cadl.codegen import generate

CADL_FILE = Path(__file__).resolve().parent / "supply_chain.cadl"


def show_file(path: Path, max_lines: int = 30) -> None:
    """Print the contents of a file with line limit."""
    text = path.read_text()
    lines = text.splitlines()
    for line in lines[:max_lines]:
        print(f"    {line}")
    if len(lines) > max_lines:
        print(f"    ... ({len(lines) - max_lines} more lines)")


def main() -> None:
    print("=" * 60)
    print("CADL Demo: Multi-Target Code Generation")
    print("=" * 60)

    sos = parse_file(CADL_FILE)
    print(f"\nSource: {CADL_FILE.name}")
    print(f"SoS: {sos.name} ({sos.type.value})")
    print(f"Actors: {len(sos.actors)}, Contracts: {len(sos.contracts)}")
    print(f"Protocols: {len(sos.protocols)}, Transitions: {len(sos.transitions)}")

    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Python ---
        print("\n" + "-" * 60)
        print("Target: Python")
        print("-" * 60)
        py_dir = Path(tmpdir) / "python"
        generate(sos, py_dir, target="python")
        py_files = sorted(f.name for f in py_dir.glob("*.py"))
        print(f"Generated {len(py_files)} files: {', '.join(py_files)}\n")
        print("  --- contracts.py ---")
        show_file(py_dir / "contracts.py", max_lines=25)

        # --- Solidity ---
        print("\n" + "-" * 60)
        print("Target: Solidity")
        print("-" * 60)
        sol_dir = Path(tmpdir) / "solidity"
        generate(sos, sol_dir, target="solidity")
        sol_files = sorted(f.name for f in sol_dir.glob("*.sol"))
        print(f"Generated {len(sol_files)} files: {', '.join(sol_files)}\n")
        print(f"  --- {sol_files[0]} ---")
        show_file(sol_dir / sol_files[0], max_lines=35)

        # --- OPA/Rego ---
        print("\n" + "-" * 60)
        print("Target: OPA/Rego")
        print("-" * 60)
        rego_dir = Path(tmpdir) / "rego"
        generate(sos, rego_dir, target="opa")
        rego_files = sorted(f.name for f in rego_dir.glob("*.rego"))
        print(f"Generated {len(rego_files)} files: {', '.join(rego_files)}\n")
        print(f"  --- {rego_files[0]} ---")
        show_file(rego_dir / rego_files[0], max_lines=35)

    # --- Summary ---
    print("\n" + "=" * 60)
    print("Summary: Same CADL definition -> 3 different targets")
    print("=" * 60)
    print(f"  Python:   Runtime monitors with async protocols")
    print(f"  Solidity: On-chain assume/guarantee verification")
    print(f"  Rego:     Policy-as-code for OPA enforcement")
    print()


if __name__ == "__main__":
    main()
