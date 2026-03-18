#!/usr/bin/env python3
"""CADL Demo: Robot Delivery System — End-to-End Workflow

This demo shows the complete CADL toolchain in action:
  1. Parse a CADL file
  2. Type-check for semantic errors
  3. SMT verification + deadlock detection
  4. Generate Python runtime code
  5. Generate Solidity smart contracts
  6. Generate OPA/Rego policies
  7. Analyze regime transitions

Run:
    python examples/demo_robot_delivery.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Ensure cadl is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cadl.parser import parse_file
from cadl.type_checker import type_check
from cadl.verifier import verify
from cadl.deadlock import detect_deadlocks
from cadl.codegen import generate
from cadl.regime_map import RegimeMap
from cadl.iec62853 import generate_iec62853_report

CADL_FILE = Path(__file__).resolve().parent / "robot_delivery.cadl"


def main() -> None:
    print("=" * 60)
    print("CADL Demo: Robot Delivery System")
    print("=" * 60)

    # --- Step 1: Parse ---
    print("\n[1] Parsing CADL file...")
    sos = parse_file(CADL_FILE)
    print(f"    SoS Name: {sos.name}")
    print(f"    Type: {sos.type.value}")
    print(f"    Actors: {len(sos.actors)}")
    print(f"    Contracts: {len(sos.contracts)}")
    print(f"    Protocols: {len(sos.protocols)}")
    print(f"    Transitions: {len(sos.transitions)}")

    # --- Step 2: Type Check ---
    print("\n[2] Running type checks...")
    tc_result = type_check(sos)
    if tc_result.ok:
        print(f"    PASS ({len(tc_result.warnings)} warnings)")
    else:
        print(f"    FAIL: {len(tc_result.errors)} errors")
    for w in tc_result.warnings:
        print(f"      Warning: {w}")

    # --- Step 3: SMT Verification + Deadlock Detection ---
    print("\n[3] SMT verification...")
    v_results = verify(sos)
    for r in v_results:
        status = "PASS" if r.status == "passed" else "FAIL"
        print(f"    [{status}] {r.check_name}: {r.message}")

    print("\n[4] Deadlock detection...")
    d_results = detect_deadlocks(sos)
    for r in d_results:
        status = "PASS" if r.status == "passed" else "FAIL"
        print(f"    [{status}] {r.check_name}: {r.message}")

    # --- Step 4: Python Code Generation ---
    print("\n[5] Generating Python runtime code...")
    with tempfile.TemporaryDirectory() as tmpdir:
        py_dir = Path(tmpdir) / "python"
        generate(sos, py_dir, target="python")
        py_files = sorted(f.name for f in py_dir.glob("*.py"))
        print(f"    Output: {len(py_files)} files")
        for f in py_files:
            print(f"      - {f}")

        # Show a snippet of the generated runtime
        runtime_code = (py_dir / "runtime.py").read_text()
        lines = runtime_code.splitlines()
        print(f"\n    --- runtime.py (first 15 lines) ---")
        for line in lines[:15]:
            print(f"    {line}")
        print("    ...")

    # --- Step 5: Solidity Code Generation ---
    print("\n[6] Generating Solidity smart contracts...")
    with tempfile.TemporaryDirectory() as tmpdir:
        sol_dir = Path(tmpdir) / "solidity"
        generate(sos, sol_dir, target="solidity")
        sol_files = sorted(f.name for f in sol_dir.glob("*.sol"))
        print(f"    Output: {len(sol_files)} files")
        for f in sol_files:
            print(f"      - {f}")

        # Show a snippet of one contract
        first_sol = sol_dir / sol_files[0]
        sol_code = first_sol.read_text()
        lines = sol_code.splitlines()
        print(f"\n    --- {sol_files[0]} (first 20 lines) ---")
        for line in lines[:20]:
            print(f"    {line}")
        print("    ...")

    # --- Step 6: OPA/Rego Code Generation ---
    print("\n[7] Generating OPA/Rego policies...")
    with tempfile.TemporaryDirectory() as tmpdir:
        rego_dir = Path(tmpdir) / "rego"
        generate(sos, rego_dir, target="opa")
        rego_files = sorted(f.name for f in rego_dir.glob("*.rego"))
        print(f"    Output: {len(rego_files)} files")
        for f in rego_files:
            print(f"      - {f}")

        # Show the contract policy
        contract_rego = rego_dir / "delivery_sla.rego"
        rego_code = contract_rego.read_text()
        lines = rego_code.splitlines()
        print(f"\n    --- delivery_sla.rego (first 25 lines) ---")
        for line in lines[:25]:
            print(f"    {line}")
        print("    ...")

    # --- Step 7: Regime Map ---
    print("\n[8] Analyzing regime transitions...")
    rm = RegimeMap.from_sos(sos)
    print(f"    States: {sorted(rm.states.keys())}")
    print(f"    Initial: {rm.initial_state}")
    print(f"    Transitions: {len(rm.transitions)}")
    cycles = rm.find_cycles()
    print(f"    Cycles: {len(cycles)}")
    dead = rm.find_dead_states()
    print(f"    Dead states: {dead or 'none'}")
    print(f"\n    Regime Map (text):")
    for line in rm.to_text().splitlines():
        print(f"      {line}")

    # --- Step 8: IEC 62853 Compliance ---
    print("\n[9] IEC 62853 compliance report...")
    report = generate_iec62853_report(sos)
    print(f"    System Integration Level: {report['system_integration_level']}")
    for param in report["institutional_parameters"]:
        print(f"    {param['cadl_concept']}: {param['value']}")
        print(f"      -> {param['iec62853_concept']}: {param['description']}")
    dep = report["dependability_summary"]
    print(f"    Governance Index: {dep['governance_index']}")
    print(f"    Transparency Level: {dep['transparency_level']}")
    print(f"    Alignment Metric: {dep['alignment_metric']}")

    print("\n" + "=" * 60)
    print("Demo complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
