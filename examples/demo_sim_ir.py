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
# 5a. Governance Structure Comparison (Layer 1)
# =========================================================================

heading("Step 5a: Governance Structure Comparison (Layer 1 — Institution)")


def _extract_gov_profile(ir, sos):
    """Extract governance-level profile from IR + original AST."""
    p = {}

    # --- SoS type: declared vs inferred ---
    p["type_declared"] = ir.sos_type

    # Infer from beta
    betas = [c.governance.beta for c in ir.institution.contracts
             if c.governance.beta is not None]
    avg_beta = sum(betas) / len(betas) if betas else None
    if avg_beta is not None:
        if avg_beta >= 0.5:
            p["type_inferred"] = "Acknowledged"
            p["type_rationale"] = (
                f"β_avg={avg_beta:.2f} (≥0.5): central authority exists "
                f"and is acknowledged by constituents"
            )
        elif avg_beta < 0.2:
            p["type_inferred"] = "Collaborative"
            p["type_rationale"] = (
                f"β_avg={avg_beta:.2f} (<0.2): no central command; "
                f"each actor decides autonomously"
            )
        else:
            p["type_inferred"] = "Hybrid"
            p["type_rationale"] = f"β_avg={avg_beta:.2f}: mixed authority"
    else:
        p["type_inferred"] = "Unknown"
        p["type_rationale"] = "no β values defined"

    # --- Decision holders ---
    holders = sorted({c.governance.decision_holder
                      for c in ir.institution.contracts
                      if c.governance.decision_holder})
    p["decision_holders"] = ", ".join(holders) if holders else "N/A"

    # Authority distribution
    if betas:
        if avg_beta >= 0.6:
            label = "centralized"
        elif avg_beta <= 0.2:
            label = "distributed"
        else:
            label = "hybrid"
        detail = ", ".join(f"β={b}" for b in betas)
        p["authority_dist"] = f"{label} ({detail})"
    else:
        p["authority_dist"] = "N/A"

    # --- Responsibility: actual obligations from contracts ---
    resp_lines = []
    for actor in ir.institution.actors:
        # Gather obligations from: (1) AST responsibilities, (2) guarantees
        obligations: list[str] = []
        # From AST responsibilities blocks
        for ct in sos.contracts:
            for rg in ct.responsibilities:
                a_name = rg.actor.name
                if a_name == actor.id:
                    for item in rg.items:
                        s = item if isinstance(item, str) else item.name
                        obligations.append(s)
        # If no explicit responsibilities, derive from capabilities + guarantees
        if not obligations:
            obligations = list(actor.capabilities[:3])
        if obligations:
            obs = "; ".join(obligations[:3])
            if len(obligations) > 3:
                obs += " ..."
            resp_lines.append(f"{actor.id}: {obs}")
    p["responsibilities"] = resp_lines

    # Information visibility
    alphas = [c.governance.alpha for c in ir.institution.contracts
              if c.governance.alpha is not None]
    avg_alpha = sum(alphas) / len(alphas) if alphas else None
    if alphas:
        if avg_alpha >= 0.7:
            label = "high"
        elif avg_alpha >= 0.4:
            label = "moderate"
        else:
            label = "low"
        detail = ", ".join(f"α={a}" for a in alphas)
        p["info_visibility"] = f"{label} ({detail})"
    else:
        p["info_visibility"] = "N/A"

    # Sharing mode
    sharing_patterns: list[str] = []
    for c in ir.institution.contracts:
        if c.governance.sharing_mode:
            for part in c.governance.sharing_mode.split(" ; "):
                arrow = part.split(" -> ")
                if len(arrow) == 2:
                    src = arrow[0].strip().split("[")[0]
                    rest = arrow[1].strip()
                    tgt = rest.split(" : ")[0].split("[")[0] if " : " in rest else rest.split("[")[0]
                    d = f"{src}→{tgt}"
                    if d not in sharing_patterns:
                        sharing_patterns.append(d)
    peer = [sp for sp in sharing_patterns if sp.split("→")[0] == sp.split("→")[1]]
    uplink, downlink = [], []
    for pat in sharing_patterns:
        s, t = pat.split("→")
        if s == t:
            continue
        sa = next((a for a in ir.institution.actors if a.id == s), None)
        ta = next((a for a in ir.institution.actors if a.id == t), None)
        if sa and ta:
            if sa.autonomy == "high" and ta.autonomy == "low":
                uplink.append(pat)
            elif sa.autonomy == "low" and ta.autonomy == "high":
                downlink.append(pat)
    parts = []
    if peer:
        parts.append("peer-to-peer")
    if uplink:
        parts.append("uplink")
    if downlink:
        parts.append("broadcast")
    p["sharing_mode"] = " + ".join(parts) if parts else ", ".join(sharing_patterns[:3])
    p["sharing_channels"] = ", ".join(sharing_patterns)

    # Incentive
    itypes = sorted({c.governance.incentive_type
                     for c in ir.institution.contracts
                     if c.governance.incentive_type})
    p["incentive"] = ", ".join(itypes) if itypes else "N/A"

    # Derived
    p["avg_alpha"] = avg_alpha
    p["avg_beta"] = avg_beta
    lambdas = [c.governance.lambda_ for c in ir.institution.contracts
               if c.governance.lambda_ is not None]
    p["avg_lambda"] = sum(lambdas) / len(lambdas) if lambdas else None

    # Modes
    regimes = sorted({t.from_regime for t in ir.transitions} |
                     {t.to_regime for t in ir.transitions})
    p["modes"] = " / ".join(regimes) if regimes else "N/A"
    escalation = [f"{t.from_regime}→{t.to_regime}" for t in ir.transitions]
    p["transitions"] = "; ".join(escalation) if escalation else "N/A"

    return p


def _extract_proto_profile(ir):
    """Extract protocol & algorithm binding profile from IR."""
    p = {}
    FAILURE_KW = ("fail", "obstacle", "collision", "incident", "conflict", "emergency")

    failure_protos = [proto for proto in ir.protocol.protocols
                      if any(kw in proto.trigger.lower() for kw in FAILURE_KW)]

    # Replanning: structured as Trigger / Computation / Enforcement
    replan_rows = []
    for fp in failure_protos:
        trigger = fp.trigger.split("(")[0]
        comp_steps = [s for s in fp.steps if s.type == "compute"]
        msg_steps = [s for s in fp.steps if s.type == "message"]

        comp_actors = sorted({s.sender.split("[")[0]
                              for s in comp_steps if s.sender})
        comp_loc = ", ".join(comp_actors) if comp_actors else "N/A"

        # Enforcement: how is the computed result distributed?
        # Find the last message(s) in the protocol — these carry the result.
        if msg_steps:
            last_msg = msg_steps[-1]
            last_sender = last_msg.sender.split("[")[0] if last_msg.sender else "?"
            last_receiver = last_msg.receiver.split("[")[0] if last_msg.receiver else "?"
            is_broadcast = last_msg.receiver and "[*]" in last_msg.receiver
            if last_sender in comp_actors and last_sender != last_receiver:
                enforce = f"broadcast → {last_receiver}" if is_broadcast else f"notify → {last_receiver}"
            elif last_receiver in comp_actors:
                # The computing actor receives a report — enforcement is local
                enforce = "local (self-enforced)"
            else:
                enforce = f"{last_sender} → {last_receiver}"
        elif fp.postcondition:
            enforce = "local (self-enforced)"
        else:
            enforce = "local (self-enforced)"

        replan_rows.append({
            "proto": fp.id,
            "trigger": trigger,
            "computation": comp_loc,
            "enforcement": enforce,
        })
    p["replan_rows"] = replan_rows

    # Timeout / fallback
    fb_parts = []
    for proto in ir.protocol.protocols:
        for key, val in proto.fallback.items():
            actor = val.split(":")[0].strip().split("[")[0] if ":" in val else "?"
            action = val.split(":")[-1].strip() if ":" in val else val
            # Clean action
            action = action.strip().rstrip(")").split("(")[0].strip()
            fb_parts.append(f"{actor}: {action}")
    p["fallback"] = "; ".join(fb_parts) if fb_parts else "N/A"

    # Algorithm binding
    p["central"] = ", ".join(
        f"{a.name}={a.central}" for a in ir.algorithm.algorithms
        if a.central and a.central.lower() != "none") or "none"
    p["local"] = ", ".join(
        f"{a.name}={a.local}" for a in ir.algorithm.algorithms
        if a.local and a.local.lower() != "none") or "none"

    return p


a_gov = _extract_gov_profile(a_ir, a_sos)
c_gov = _extract_gov_profile(c_ir, c_sos)
a_proto = _extract_proto_profile(a_ir)
c_proto = _extract_proto_profile(c_ir)

# --- Table rendering ---

COL_PROP = 28
COL_A = 40
COL_C = 40
TABLE_W = COL_PROP + COL_A + COL_C + 2


def _trunc(s: str, w: int) -> str:
    if len(s) <= w:
        return s.ljust(w)
    return s[:w - 1] + "…"


def tbl_header(a_label="A-SoS", c_label="C-SoS"):
    print(f"  {'Property'.ljust(COL_PROP)}{a_label.ljust(COL_A)}{c_label.ljust(COL_C)}")
    tbl_sep()


def tbl_sep():
    print(f"  {'─' * TABLE_W}")


def tbl_row(prop, a_val, c_val):
    print(f"  {prop.ljust(COL_PROP)}{_trunc(str(a_val), COL_A)}{_trunc(str(c_val), COL_C)}")


def tbl_row_note(prop, a_val, c_val, note):
    tbl_row(prop, a_val, c_val)
    if note:
        print(f"  {''.ljust(COL_PROP)}  └ {note}")


# --- Table (a): Governance Structure ---
print()
tbl_header("A-SoS (MAPFRobotDelivery)", "C-SoS (AutonomousTaxiFleet)")

tbl_row("SoS type (declared)", a_gov["type_declared"], c_gov["type_declared"])
tbl_row("SoS type (inferred)", a_gov["type_inferred"], c_gov["type_inferred"])
tbl_row_note("", a_gov["type_rationale"], c_gov["type_rationale"],
             "inferred from avg β across contracts")

tbl_sep()
tbl_row("Decision holder(s)", a_gov["decision_holders"], c_gov["decision_holders"])
tbl_row("Authority distribution", a_gov["authority_dist"], c_gov["authority_dist"])
tbl_row("Information visibility", a_gov["info_visibility"], c_gov["info_visibility"])
tbl_row("Sharing mode", a_gov["sharing_mode"], c_gov["sharing_mode"])
tbl_row("Sharing channels", a_gov["sharing_channels"], c_gov["sharing_channels"])
tbl_row("Incentive mechanism", a_gov["incentive"], c_gov["incentive"])

tbl_sep()
print(f"  {'Responsibilities'.ljust(COL_PROP)}{'A-SoS'.ljust(COL_A)}{'C-SoS'.ljust(COL_C)}")
max_resp = max(len(a_gov["responsibilities"]), len(c_gov["responsibilities"]))
for i in range(max_resp):
    a_r = a_gov["responsibilities"][i] if i < len(a_gov["responsibilities"]) else ""
    c_r = c_gov["responsibilities"][i] if i < len(c_gov["responsibilities"]) else ""
    prop = "" if i > 0 else "(per-actor obligations)"
    tbl_row(prop, a_r, c_r)

tbl_sep()
print(f"  {'Derived Metrics'.ljust(COL_PROP)}{'A-SoS'.ljust(COL_A)}{'C-SoS'.ljust(COL_C)}")


def fmt(v):
    return f"{v:.2f}" if v is not None else "N/A"


tbl_row_note("Avg α (transparency)", fmt(a_gov["avg_alpha"]), fmt(c_gov["avg_alpha"]),
             "0 = local only, 1 = full sharing")
tbl_row_note("Avg β (centralization)", fmt(a_gov["avg_beta"]), fmt(c_gov["avg_beta"]),
             "0 = distributed, 1 = centralized")
tbl_row_note("Avg λ (incentive)", fmt(a_gov["avg_lambda"]), fmt(c_gov["avg_lambda"]),
             "0 = directive, 1 = market")

tbl_sep()
tbl_row("Operational modes", a_gov["modes"], c_gov["modes"])
tbl_row("Transitions", a_gov["transitions"], c_gov["transitions"])

# =========================================================================
# 5b. Protocol & Algorithm Binding Comparison (Layer 2 + 3)
# =========================================================================

heading("Step 5b: Protocol & Algorithm Binding Comparison (Layer 2 + 3)")

# Replanning structure: Trigger / Computation Location / Enforcement
print()
print("  Replanning Structure (normalized)")
tbl_sep()
print(f"  {'Protocol'.ljust(20)}{'Trigger'.ljust(28)}{'Computation'.ljust(18)}{'Enforcement'.ljust(28)}")
tbl_sep()

for label, prof in [("A-SoS", a_proto), ("C-SoS", c_proto)]:
    for i, rr in enumerate(prof["replan_rows"]):
        prefix = label if i == 0 else ""
        print(f"  {prefix.ljust(20)}"
              f"{_trunc(rr['trigger'], 28)}"
              f"{_trunc(rr['computation'], 18)}"
              f"{_trunc(rr['enforcement'], 28)}")
    if not prof["replan_rows"]:
        print(f"  {label.ljust(20)}(no failure protocols)")

tbl_sep()

# Timeout / fallback
print()
tbl_header("A-SoS", "C-SoS")
tbl_row("Timeout / fallback", a_proto["fallback"], c_proto["fallback"])

tbl_sep()
print(f"  {'Algorithm Binding'.ljust(COL_PROP)}{'A-SoS'.ljust(COL_A)}{'C-SoS'.ljust(COL_C)}")
tbl_sep()
tbl_row_note("Central planner", a_proto["central"], c_proto["central"],
             "← implementation choice, not governance")
tbl_row_note("Local planner", a_proto["local"], c_proto["local"],
             "← implementation choice, not governance")

# --- Structural Summary ---

print()
heading("Structural Summary")
print(textwrap.dedent("""\
  Layer 1 — Institution (who decides / who knows / who is responsible):
    A-SoS: Single decision holder (DISPATCHER) with high authority (β≈0.85).
           Robots report upward; dispatcher broadcasts commands downward.
           Responsibility is vertically separated: planner vs executor.
    C-SoS: Every TAXI decides independently (β≈0.08). TRAFFIC_CENTER only
           aggregates — it cannot command. Information flows peer-to-peer.
           Each taxi bears full responsibility for routing, pickup, and safety.

  Layer 2 — Protocol (how actors interact and handle failures):
    A-SoS: Failure triggers upward escalation (ROBOT→DISPATCHER).
           Dispatcher replans centrally and broadcasts new routes.
           Fallback: robots stop-in-place and wait for instructions.
    C-SoS: Conflict triggers bilateral negotiation (TAXI↔TAXI).
           Each taxi replans locally. No central replanning authority exists.
           Fallback: yielding taxi waits; passengers retry with wider radius.

  Layer 3 — Algorithm Binding (what runs where):
    A-SoS: Central=ECBS (optimal MAPF solver), Local=tracking_only.
           Governance ↔ algorithm alignment: central authority ↔ central solver.
    C-SoS: Central=aggregation_only, Local=LRA* + conflict_avoidance.
           Governance ↔ algorithm alignment: distributed authority ↔ local solver.

  Why governance comparison ≠ algorithm comparison:
    The same algorithm (e.g. ECBS) could in principle run in a C-SoS, but the
    institutional design determines WHO invokes it, WHAT information it receives,
    and HOW its output is enforced. The 3-layer IR makes this distinction explicit:
    Layer 1 defines the decision structure (governance); Layer 3 is an
    implementation binding that should be consistent with — but is logically
    separate from — the governance design.
"""))

print("Done.")
