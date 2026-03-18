#!/usr/bin/env python3
"""Demo: CADL → 3-Layer Simulator IR → Config Generation

This script demonstrates the Phase 7 simulator IR pipeline:

1. Parse two CADL files (A-SoS and C-SoS)
2. Lower each to the 3-layer IR
3. Show the IR structure (Institution / Protocol / Algorithm)
4. Validate the IR
5. Generate simulator configs for Python, Unity, and Go
6. Compare governance characteristics between A-SoS and C-SoS

Usage:
    python examples/demo_sim_ir.py
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

# Ensure the package is importable when running from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cadl.parser import parse_file
from cadl.sim import generate_config, lower_to_ir, validate_ir

EXAMPLES = Path(__file__).resolve().parent

DIVIDER = "=" * 70
SECTION = "-" * 50


def heading(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def subheading(title: str) -> None:
    print(f"\n{SECTION}")
    print(f"  {title}")
    print(SECTION)


# =========================================================================
# 1. Parse both samples
# =========================================================================

heading("Step 1: Parse CADL files")

a_sos_path = EXAMPLES / "a_sos_robot_delivery.cadl"
c_sos_path = EXAMPLES / "c_sos_taxi_fleet.cadl"

a_sos = parse_file(a_sos_path)
c_sos = parse_file(c_sos_path)

print(f"  A-SoS: {a_sos.name} ({a_sos.type.value})")
print(f"    Actors: {len(a_sos.actors)}, Contracts: {len(a_sos.contracts)}, "
      f"Protocols: {len(a_sos.protocols)}")
print(f"  C-SoS: {c_sos.name} ({c_sos.type.value})")
print(f"    Actors: {len(c_sos.actors)}, Contracts: {len(c_sos.contracts)}, "
      f"Protocols: {len(c_sos.protocols)}")

# =========================================================================
# 2. Lower to 3-Layer IR
# =========================================================================

heading("Step 2: Lower AST → 3-Layer IR")

a_ir = lower_to_ir(a_sos)
c_ir = lower_to_ir(c_sos)

for label, ir in [("A-SoS", a_ir), ("C-SoS", c_ir)]:
    subheading(f"{label}: {ir.name}")

    print(f"\n  [Layer 1: Institution / Governance]")
    print(f"    Actors:")
    for actor in ir.institution.actors:
        count_str = f" x{actor.count}" if actor.count else ""
        print(f"      {actor.id}{count_str} — role={actor.role}, "
              f"autonomy={actor.autonomy}")
        if actor.capabilities:
            print(f"        capabilities: {', '.join(actor.capabilities[:4])}"
                  f"{'...' if len(actor.capabilities) > 4 else ''}")

    print(f"    Contracts:")
    for contract in ir.institution.contracts:
        g = contract.governance
        print(f"      {contract.id}")
        print(f"        parties: {contract.parties}")
        print(f"        governance: alpha={g.alpha}, beta={g.beta}, "
              f"lambda={g.lambda_}")
        print(f"        decision_holder: {g.decision_holder}")
        if g.sharing_mode:
            # Truncate long sharing modes
            sharing = g.sharing_mode
            if len(sharing) > 60:
                sharing = sharing[:57] + "..."
            print(f"        sharing: {sharing}")

    print(f"\n  [Layer 2: Interaction Protocol]")
    print(f"    Events: {ir.protocol.events[:3]}"
          f"{'...' if len(ir.protocol.events) > 3 else ''}")
    print(f"    Protocols:")
    for proto in ir.protocol.protocols:
        msg_steps = [s for s in proto.steps if s.type == "message"]
        comp_steps = [s for s in proto.steps if s.type == "compute"]
        print(f"      {proto.id}: {len(msg_steps)} messages, "
              f"{len(comp_steps)} computations")
        if proto.timing:
            print(f"        timing: {proto.timing}")

    print(f"\n  [Layer 3: Operational Algorithm]")
    for algo in ir.algorithm.algorithms:
        print(f"    {algo.name}: central={algo.central}, local={algo.local}")

    if ir.transitions:
        regimes = set()
        for t in ir.transitions:
            regimes.add(t.from_regime)
            regimes.add(t.to_regime)
        print(f"\n  [Cross-cutting]")
        print(f"    Regimes: {sorted(regimes)}")
        print(f"    Transitions: {len(ir.transitions)}")
        print(f"    Metrics: {[m.id for m in ir.metrics]}")

# =========================================================================
# 3. Validate
# =========================================================================

heading("Step 3: Validate IR")

for label, ir in [("A-SoS", a_ir), ("C-SoS", c_ir)]:
    errors = validate_ir(ir)
    if errors:
        print(f"  {label}: FAILED")
        for e in errors:
            print(f"    - {e}")
    else:
        print(f"  {label}: OK (no errors)")

# =========================================================================
# 4. Generate configs for all targets
# =========================================================================

heading("Step 4: Generate Simulator Configs")

for label, ir in [("A-SoS", a_ir), ("C-SoS", c_ir)]:
    subheading(f"{label}: {ir.name}")

    # Python config (YAML)
    py_config = generate_config(ir, "python")
    py_lines = py_config.strip().split("\n")
    print(f"\n  [Python Simulator Config — YAML] ({len(py_lines)} lines)")
    for line in py_lines[:12]:
        print(f"    {line}")
    print(f"    ...")

    # Unity config (JSON)
    unity_config = generate_config(ir, "unity")
    unity_data = json.loads(unity_config)
    n_templates = len(unity_data.get("agentTemplates", []))
    n_protocols = len(unity_data.get("protocols", []))
    print(f"\n  [Unity Simulator Config — JSON]")
    print(f"    agentTemplates: {n_templates}")
    print(f"    protocols: {n_protocols}")
    first_template = unity_data["agentTemplates"][0]
    print(f"    first template: {first_template['templateId']} "
          f"(prefab={first_template['prefab']})")

    # Go config (JSON)
    go_config = generate_config(ir, "go")
    go_data = json.loads(go_config)
    n_agents = len(go_data.get("agents", []))
    n_contracts = len(go_data.get("contracts", []))
    print(f"\n  [Go Simulator Config — JSON]")
    print(f"    agents: {n_agents}, contracts: {n_contracts}")
    first_agent = go_data["agents"][0]
    algo_info = first_agent.get("algorithm", {})
    print(f"    first agent: {first_agent['id']} "
          f"(algorithm: central={algo_info.get('central')}, "
          f"local={algo_info.get('local')})")

# =========================================================================
# 5. A-SoS vs C-SoS Governance Comparison
# =========================================================================

heading("Step 5: A-SoS vs C-SoS Governance Comparison")

def avg_param(ir, param_name):
    vals = []
    for c in ir.institution.contracts:
        v = getattr(c.governance, param_name)
        if v is not None:
            vals.append(v)
    return sum(vals) / len(vals) if vals else None

rows = [
    ("SoS Type", a_ir.sos_type, c_ir.sos_type),
    ("Decision Holders",
     ", ".join(sorted({c.governance.decision_holder
                       for c in a_ir.institution.contracts
                       if c.governance.decision_holder})),
     ", ".join(sorted({c.governance.decision_holder
                       for c in c_ir.institution.contracts
                       if c.governance.decision_holder}))),
    ("Avg alpha (info transparency)",
     f"{avg_param(a_ir, 'alpha'):.2f}",
     f"{avg_param(c_ir, 'alpha'):.2f}"),
    ("Avg beta (authority central.)",
     f"{avg_param(a_ir, 'beta'):.2f}",
     f"{avg_param(c_ir, 'beta'):.2f}"),
    ("Avg lambda (incentive align.)",
     f"{avg_param(a_ir, 'lambda_'):.2f}",
     f"{avg_param(c_ir, 'lambda_'):.2f}"),
    ("Central Planner",
     a_ir.algorithm.algorithms[0].central if a_ir.algorithm.algorithms else "N/A",
     c_ir.algorithm.algorithms[0].central if c_ir.algorithm.algorithms else "N/A"),
    ("Local Planner",
     a_ir.algorithm.algorithms[0].local if a_ir.algorithm.algorithms else "N/A",
     c_ir.algorithm.algorithms[0].local if c_ir.algorithm.algorithms else "N/A"),
    ("Regime Count",
     str(len({t.from_regime for t in a_ir.transitions} |
             {t.to_regime for t in a_ir.transitions})),
     str(len({t.from_regime for t in c_ir.transitions} |
             {t.to_regime for t in c_ir.transitions}))),
]

print(f"\n  {'Property':<30} {'A-SoS (Robot)':<22} {'C-SoS (Taxi)':<22}")
print(f"  {'-'*30} {'-'*22} {'-'*22}")
for prop, a_val, c_val in rows:
    print(f"  {prop:<30} {a_val:<22} {c_val:<22}")

# =========================================================================
# Summary
# =========================================================================

heading("Summary")
print(textwrap.dedent("""\
    The 3-layer IR explicitly separates three research-relevant concerns:

      Layer 1 (Institution):  WHO decides, WHO knows, WHO benefits
      Layer 2 (Protocol):     HOW they interact, WHEN, fallback on failure
      Layer 3 (Algorithm):    WHAT runs centrally vs locally

    This separation enables:
      - Comparing governance structures (A-SoS vs C-SoS) at the IR level
      - Generating simulator configs for different platforms from one source
      - Reasoning about institutional design independently of implementation
"""))
print("Done.")
