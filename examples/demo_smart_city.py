#!/usr/bin/env python3
"""CADL Demo: Smart City Traffic — Regime Map & Compliance Analysis

Demonstrates Phase 5-6 features:
  1. Multi-regime transition analysis (NORMAL / CONGESTED / EMERGENCY)
  2. Regime map graph export (DOT format for Graphviz)
  3. IEC 62853 Open Systems Dependability compliance report
  4. Multi-target code generation comparison

Run:
    python examples/demo_smart_city.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cadl.parser import parse_file
from cadl.type_checker import type_check
from cadl.regime_map import RegimeMap
from cadl.iec62853 import generate_iec62853_report
from cadl.codegen import generate

CADL_FILE = Path(__file__).resolve().parent / "smart_city_traffic.cadl"


def main() -> None:
    print("=" * 60)
    print("CADL Demo: Smart City Traffic Management")
    print("=" * 60)

    # Parse and verify
    sos = parse_file(CADL_FILE)
    tc = type_check(sos)
    print(f"\nParsed: {sos.name} ({sos.type.value})")
    print(f"  Actors: {len(sos.actors)}, Contracts: {len(sos.contracts)}")
    print(f"  Protocols: {len(sos.protocols)}, Transitions: {len(sos.transitions)}")
    print(f"  Type check: {'PASS' if tc.ok else 'FAIL'}")

    # --- Regime Map Analysis ---
    print("\n" + "-" * 60)
    print("Regime Map Analysis")
    print("-" * 60)

    rm = RegimeMap.from_sos(sos)
    print(f"\nStates: {sorted(rm.states.keys())}")
    print(f"Initial state: {rm.initial_state}")
    print(f"Transitions: {len(rm.transitions)}")

    # Reachability
    reachable = rm.reachable_states(rm.initial_state)
    unreachable = rm.find_unreachable_states()
    print(f"Reachable from {rm.initial_state}: {sorted(reachable)}")
    print(f"Unreachable states: {sorted(unreachable) or 'none'}")

    # Cycles
    cycles = rm.find_cycles()
    print(f"Cycles (SCCs): {len(cycles)}")
    for i, cycle in enumerate(cycles):
        print(f"  Cycle {i+1}: {' -> '.join(sorted(cycle))}")

    # Dead states
    dead = rm.find_dead_states()
    print(f"Dead-end states: {sorted(dead) or 'none'}")

    # Shortest paths
    for target in sorted(rm.states.keys()):
        if target != rm.initial_state:
            path = rm.shortest_path(rm.initial_state, target)
            print(f"  Shortest path {rm.initial_state} -> {target}: {' -> '.join(path)}")

    # DOT export
    print(f"\nGraphviz DOT format:")
    dot = rm.to_dot()
    for line in dot.splitlines():
        print(f"  {line}")

    # --- IEC 62853 Compliance Report ---
    print("\n" + "-" * 60)
    print("IEC 62853 Compliance Report")
    print("-" * 60)

    report = generate_iec62853_report(sos)
    print(f"\nSystem: {report['sos_name']}")
    print(f"SoS Type: {report['sos_type']}")
    print(f"Integration Level: {report['system_integration_level']}")

    print("\nInstitutional Parameters:")
    for p in report["institutional_parameters"]:
        print(f"  [{p['contract_id']}] {p['cadl_concept']} = {p['value']}")
        print(f"    IEC 62853: {p['iec62853_concept']} ({p['description']})")

    print("\nService Level Agreements:")
    for sla in report["service_level_agreements"]:
        print(f"  {sla['contract_id']}: {sla['assumption_count']} assumptions, "
              f"{sla['guarantee_count']} guarantees")
        if sla["failure_response"]:
            print(f"    Failure response: {sla['failure_response'][:80]}...")

    osm = report["operational_state_machine"]
    if osm:
        print(f"\nOperational State Machine:")
        print(f"  {osm['state_count']} states, {osm['transition_count']} transitions")
        print(f"  Initial: {osm['initial_state']}")
        print(f"  Has cycles: {osm['has_cycles']}")

    dep = report["dependability_summary"]
    print(f"\nDependability Summary:")
    print(f"  Governance Index (beta): {dep['governance_index']}")
    print(f"  Transparency Level (alpha): {dep['transparency_level']}")
    print(f"  Alignment Metric (lambda): {dep['alignment_metric']}")

    # --- Multi-target Code Generation ---
    print("\n" + "-" * 60)
    print("Multi-Target Code Generation")
    print("-" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        for target in ["python", "solidity", "opa"]:
            out = Path(tmpdir) / target
            generate(sos, out, target=target)
            files = sorted(f.name for f in out.iterdir())
            print(f"\n  [{target}] {len(files)} files: {', '.join(files)}")

    print("\n" + "=" * 60)
    print("Demo complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
