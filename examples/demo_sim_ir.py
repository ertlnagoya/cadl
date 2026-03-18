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
# 5. A-SoS vs C-SoS Governance Structure Comparison
# =========================================================================

heading("Step 5: A-SoS vs C-SoS — Governance Structure Comparison")


def _extract_profile(ir):
    """Extract a structured governance profile from a SimIR for comparison."""
    p = {}

    # --- Institution ---
    p["sos_type"] = ir.sos_type

    # Decision holders: unique set across all contracts
    holders = sorted({c.governance.decision_holder
                      for c in ir.institution.contracts
                      if c.governance.decision_holder})
    p["decision_holders"] = ", ".join(holders) if holders else "N/A"

    # Authority distribution: classify as centralized / distributed / hybrid
    betas = [(c.id, c.governance.beta) for c in ir.institution.contracts
             if c.governance.beta is not None]
    if betas:
        avg_b = sum(b for _, b in betas) / len(betas)
        if avg_b >= 0.6:
            auth_label = "centralized"
        elif avg_b <= 0.2:
            auth_label = "distributed"
        else:
            auth_label = "hybrid"
        detail = ", ".join(f"β={b}" for _, b in betas)
        p["authority_distribution"] = f"{auth_label} ({detail})"
    else:
        p["authority_distribution"] = "N/A"

    # Responsibility allocation: summarize as role-based
    resp_parts = []
    for actor in ir.institution.actors:
        if actor.capabilities:
            key_cap = actor.capabilities[0]
            resp_parts.append(f"{actor.id}={actor.role}")
    p["responsibility_allocation"] = ", ".join(resp_parts)

    # Information visibility: classify
    alphas = [(c.id, c.governance.alpha) for c in ir.institution.contracts
              if c.governance.alpha is not None]
    if alphas:
        avg_a = sum(a for _, a in alphas) / len(alphas)
        if avg_a >= 0.7:
            vis_label = "high"
        elif avg_a >= 0.4:
            vis_label = "moderate"
        else:
            vis_label = "low"
        detail = ", ".join(f"α={a}" for _, a in alphas)
        p["info_visibility"] = f"{vis_label} ({detail})"
    else:
        p["info_visibility"] = "N/A"

    # Information sharing mode: derive from sharing_mode strings
    sharing_patterns: list[str] = []
    for c in ir.institution.contracts:
        if c.governance.sharing_mode:
            for part in c.governance.sharing_mode.split(" ; "):
                arrow = part.split(" -> ")
                if len(arrow) == 2:
                    src_base = arrow[0].strip().split("[")[0]
                    rest = arrow[1].strip()
                    tgt_base = rest.split(" : ")[0].split("[")[0] if " : " in rest else rest.split("[")[0]
                    direction = f"{src_base}→{tgt_base}"
                    if direction not in sharing_patterns:
                        sharing_patterns.append(direction)
    # Classify
    if any(p == q[::-1] for p in sharing_patterns for q in sharing_patterns if p != q):
        pass  # bidirectional exists
    peer = [p for p in sharing_patterns if p.split("→")[0] == p.split("→")[1]]
    uplink = []
    downlink = []
    for pat in sharing_patterns:
        src, tgt = pat.split("→")
        src_actor = next((a for a in ir.institution.actors if a.id == src), None)
        tgt_actor = next((a for a in ir.institution.actors if a.id == tgt), None)
        if src == tgt:
            continue  # peer
        if src_actor and tgt_actor:
            if src_actor.autonomy == "high" and tgt_actor.autonomy == "low":
                uplink.append(pat)
            elif src_actor.autonomy == "low" and tgt_actor.autonomy == "high":
                downlink.append(pat)
    mode_desc_parts = []
    if peer:
        mode_desc_parts.append("peer-to-peer")
    if uplink:
        mode_desc_parts.append("uplink")
    if downlink:
        mode_desc_parts.append("broadcast")
    if not mode_desc_parts:
        mode_desc_parts = [" + ".join(sharing_patterns[:3])]
    p["sharing_mode"] = " + ".join(mode_desc_parts)
    p["sharing_detail"] = ", ".join(sharing_patterns)

    # --- Protocol ---
    # Failure handling: find the protocol triggered by failure/obstacle/incident
    failure_protos = [proto for proto in ir.protocol.protocols
                      if any(kw in proto.trigger.lower()
                             for kw in ("fail", "obstacle", "collision",
                                        "incident", "conflict", "emergency"))]
    if failure_protos:
        triggers = [fp.trigger.split("(")[0] for fp in failure_protos]
        p["failure_trigger"] = ", ".join(triggers)
    else:
        p["failure_trigger"] = "N/A"

    # Replanning flow: describe the message flow of the first failure protocol
    if failure_protos:
        fp = failure_protos[0]
        msg_steps = [s for s in fp.steps if s.type == "message"]
        flow_parts = []
        for s in msg_steps:
            src = s.sender.split("[")[0] if s.sender else "?"
            tgt = s.receiver.split("[")[0] if s.receiver else "?"
            flow_parts.append(f"{src}→{tgt}:{s.content}")
        p["replan_flow"] = " → ".join(flow_parts) if flow_parts else "N/A"
    else:
        p["replan_flow"] = "N/A"

    # Timeout / fallback policy — concise summary
    fallback_parts = []
    for proto in ir.protocol.protocols:
        if proto.fallback:
            for key, val in proto.fallback.items():
                # Extract just the action name
                action = val.split(":")[-1].strip().rstrip(")")
                action = action.split("(")[0].strip()
                actor_part = val.split(":")[0].strip() if ":" in val else ""
                actor_base = actor_part.split("[")[0].strip()
                fallback_parts.append(f"{actor_base}: {action}")
    p["fallback_policy"] = "; ".join(fallback_parts[:3]) if fallback_parts else "N/A"

    # --- Algorithm Binding ---
    algo_bindings = []
    for a in ir.algorithm.algorithms:
        parts = []
        if a.central and a.central.lower() not in ("none", "n/a"):
            parts.append(f"central={a.central}")
        if a.local and a.local.lower() not in ("none", "n/a"):
            parts.append(f"local={a.local}")
        algo_bindings.append(f"{a.name}: {', '.join(parts)}")
    p["central_planner"] = ", ".join(
        a.central for a in ir.algorithm.algorithms
        if a.central and a.central.lower() not in ("none",)) or "none"
    p["local_planner"] = ", ".join(
        a.local for a in ir.algorithm.algorithms
        if a.local and a.local.lower() not in ("none",)) or "none"

    # --- Derived Metrics ---
    def avg(param_name):
        vals = [getattr(c.governance, param_name)
                for c in ir.institution.contracts
                if getattr(c.governance, param_name) is not None]
        return sum(vals) / len(vals) if vals else None

    p["avg_alpha"] = avg("alpha")
    p["avg_beta"] = avg("beta")
    p["avg_lambda"] = avg("lambda_")

    # Incentive types
    itypes = sorted({c.governance.incentive_type
                     for c in ir.institution.contracts
                     if c.governance.incentive_type})
    p["incentive_types"] = ", ".join(itypes) if itypes else "N/A"

    # --- Modes / Regimes ---
    regimes = sorted({t.from_regime for t in ir.transitions} |
                     {t.to_regime for t in ir.transitions})
    p["regime_names"] = " / ".join(regimes) if regimes else "N/A"
    # Regime triggers: what conditions drive escalation
    escalation = [f"{t.from_regime}→{t.to_regime}"
                  for t in ir.transitions if t.condition]
    p["regime_transitions_summary"] = "; ".join(escalation[:4]) if escalation else "N/A"

    # --- SoS type inference reasoning ---
    if p["avg_beta"] is not None and p["avg_beta"] >= 0.5:
        p["type_rationale"] = (
            f"β_avg={p['avg_beta']:.2f} (>0.5): central authority "
            f"acknowledged by constituents"
        )
    elif p["avg_beta"] is not None and p["avg_beta"] < 0.2:
        p["type_rationale"] = (
            f"β_avg={p['avg_beta']:.2f} (<0.2): no central command; "
            f"each actor decides autonomously"
        )
    else:
        beta_str = f"{p['avg_beta']:.2f}" if p['avg_beta'] is not None else "N/A"
        p["type_rationale"] = f"β_avg={beta_str}: mixed authority pattern"

    return p


a_prof = _extract_profile(a_ir)
c_prof = _extract_profile(c_ir)

# --- Render comparison table ---

COL_CAT = 20
COL_PROP = 26
COL_A = 42
COL_C = 42

def _trunc(s: str, w: int) -> str:
    if len(s) <= w:
        return s.ljust(w)
    return s[:w - 1] + "…"

def row(cat, prop, a_val, c_val, note=""):
    """Print one row of the comparison table."""
    cat_s = cat.ljust(COL_CAT)
    prop_s = prop.ljust(COL_PROP)
    a_s = _trunc(str(a_val), COL_A)
    c_s = _trunc(str(c_val), COL_C)
    line = f"  {cat_s}{prop_s}{a_s}{c_s}"
    if note:
        line += f" {note}"
    print(line)


def separator():
    total = COL_CAT + COL_PROP + COL_A + COL_C + 2
    print(f"  {'─' * total}")


print()
row("Category", "Property", "A-SoS (MAPFRobotDelivery)", "C-SoS (AutonomousTaxiFleet)", "Notes")
separator()

# Institution
row("Institution", "SoS type (inferred)",
    a_prof["sos_type"], c_prof["sos_type"],
    "← declared; see rationale below")
row("", "Decision holder(s)",
    a_prof["decision_holders"], c_prof["decision_holders"], "")
row("", "Authority distribution",
    a_prof["authority_distribution"], c_prof["authority_distribution"],
    "per-contract β")
row("", "Responsibility alloc.",
    a_prof["responsibility_allocation"], c_prof["responsibility_allocation"], "")
row("", "Information visibility",
    a_prof["info_visibility"], c_prof["info_visibility"],
    "per-contract α")
row("", "Sharing mode",
    a_prof["sharing_mode"], c_prof["sharing_mode"], "")
row("", "Sharing channels",
    a_prof["sharing_detail"], c_prof["sharing_detail"], "")
row("", "Incentive mechanism",
    a_prof["incentive_types"], c_prof["incentive_types"], "")

separator()

# Protocol
row("Protocol", "Failure trigger",
    a_prof["failure_trigger"], c_prof["failure_trigger"], "")
row("", "Replanning flow",
    a_prof["replan_flow"], c_prof["replan_flow"], "")
row("", "Timeout / fallback",
    a_prof["fallback_policy"], c_prof["fallback_policy"], "")

separator()

# Algorithm Binding
row("Algorithm Binding", "Central planner",
    a_prof["central_planner"], c_prof["central_planner"],
    "← implementation, not governance")
row("", "Local planner",
    a_prof["local_planner"], c_prof["local_planner"],
    "← implementation, not governance")

separator()

# Derived Metrics
def fmt_param(val):
    return f"{val:.2f}" if val is not None else "N/A"

row("Derived Metrics", "Avg α (transparency)",
    fmt_param(a_prof["avg_alpha"]),
    fmt_param(c_prof["avg_alpha"]),
    "0=local only, 1=full sharing")
row("", "Avg β (centralization)",
    fmt_param(a_prof["avg_beta"]),
    fmt_param(c_prof["avg_beta"]),
    "0=distributed, 1=centralized")
row("", "Avg λ (incentive align.)",
    fmt_param(a_prof["avg_lambda"]),
    fmt_param(c_prof["avg_lambda"]),
    "0=directive, 1=market")

separator()

# Modes / Regimes
row("Modes", "Operational modes",
    a_prof["regime_names"], c_prof["regime_names"], "")
row("", "Transitions",
    a_prof["regime_transitions_summary"],
    c_prof["regime_transitions_summary"], "")

separator()

# Interpretation
row("Interpretation", "SoS type rationale",
    a_prof["type_rationale"], c_prof["type_rationale"], "")

# --- Structural summary ---

print()
print("  Structural Summary")
print("  " + "─" * 70)
print(textwrap.dedent("""\
      Layer 1 — Institution (who decides / who knows / who is responsible):
        A-SoS: Single decision holder (DISPATCHER) with high authority (β≈0.85).
               Robots report upward; dispatcher broadcasts commands downward.
               Responsibility is vertically separated: planner vs executor.
        C-SoS: Every TAXI decides independently (β≈0.08). TRAFFIC_CENTER only
               aggregates — it cannot command. Information flows peer-to-peer.
               Each taxi bears its own responsibility for routing and safety.

      Layer 2 — Protocol (how actors interact and handle failures):
        A-SoS: Failure triggers upward escalation (ROBOT→DISPATCHER).
               Dispatcher replans centrally and broadcasts new routes.
               Fallback: robots stop-in-place and wait for instructions.
        C-SoS: Conflict triggers bilateral negotiation (TAXI↔TAXI).
               Each taxi replans locally. No central replanning exists.
               Fallback: the yielding taxi waits; passengers retry with wider radius.

      Layer 3 — Algorithm Binding (what runs where):
        A-SoS: Central=ECBS (optimal MAPF solver), Local=tracking_only.
               The algorithm matches the governance: central authority ↔ central solver.
        C-SoS: Central=aggregation_only, Local=LRA* + conflict_avoidance.
               The algorithm matches the governance: distributed authority ↔ local solver.

      Why this is a governance comparison, not just an algorithm comparison:
        The same algorithm (e.g. ECBS) could run in either A-SoS or C-SoS,
        but the institutional design determines WHO invokes it, WHAT information
        it receives, and HOW its output is enforced. The 3-layer IR makes this
        distinction explicit: Layer 1 (Institution) defines the decision
        structure; Layer 3 (Algorithm Binding) is an implementation choice
        that should be consistent with — but is logically separate from —
        the governance design.
  """))

print("Done.")
