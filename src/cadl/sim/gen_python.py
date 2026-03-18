"""Generate Python simulator configuration (YAML) from SimIR."""

from __future__ import annotations

import yaml

from .ir import SimIR


def generate_python_config(ir: SimIR) -> str:
    """Generate a YAML config for a Python-based simulator."""
    config = _build_config(ir)
    return yaml.dump(config, default_flow_style=False, sort_keys=False,
                     allow_unicode=True)


def _build_config(ir: SimIR) -> dict:
    cfg: dict = {}

    # Simulator metadata
    cfg["simulator"] = {
        "name": ir.name,
        "type": ir.sos_type.lower(),
        "description": ir.description,
    }

    # Environment
    if ir.environment:
        cfg["environment"] = dict(ir.environment)

    # Agents (Layer 1 actors + Layer 3 algorithm bindings)
    algo_map = _build_algo_map(ir)
    agents = []
    for actor in ir.institution.actors:
        agent: dict = {
            "template": actor.id,
            "role": actor.role,
            "autonomy": actor.autonomy,
        }
        if actor.count is not None:
            agent["count"] = actor.count
        if actor.capabilities:
            agent["capabilities"] = actor.capabilities
        # Bind planner from Layer 3
        planner = _find_planner(actor, algo_map)
        if planner:
            agent["planner"] = planner
        if actor.inputs:
            agent["inputs"] = actor.inputs
        if actor.outputs:
            agent["outputs"] = actor.outputs
        agents.append(agent)
    cfg["agents"] = agents

    # Communication channels (derived from contract sharing info)
    channels = _extract_channels(ir)
    if channels:
        cfg["communication"] = {"channels": channels}

    # Governance summary
    gov = _governance_summary(ir)
    if gov:
        cfg["governance"] = gov

    # Contracts
    contracts = []
    for c in ir.institution.contracts:
        contract: dict = {
            "id": c.id,
            "parties": c.parties,
        }
        if c.assume:
            contract["assumptions"] = c.assume
        if c.guarantee:
            contract["guarantees"] = c.guarantee
        if c.violation_detect or c.violation_action:
            contract["violation"] = {}
            if c.violation_detect:
                contract["violation"]["detect"] = c.violation_detect
            if c.violation_action:
                contract["violation"]["action"] = c.violation_action
        contracts.append(contract)
    cfg["contracts"] = contracts

    # Events
    if ir.protocol.events:
        cfg["events"] = ir.protocol.events

    # Protocols
    protocols = []
    for p in ir.protocol.protocols:
        proto: dict = {"id": p.id, "trigger": p.trigger}
        if p.steps:
            proto["steps"] = [_step_to_dict(s) for s in p.steps]
        if p.timing:
            proto["timing"] = p.timing
        if p.fallback:
            proto["fallback"] = p.fallback
        protocols.append(proto)
    cfg["protocols"] = protocols

    # Transitions
    if ir.transitions:
        cfg["transitions"] = [
            _transition_dict(t) for t in ir.transitions
        ]

    # Metrics
    if ir.metrics:
        cfg["metrics"] = [
            {"id": m.id, "formula": m.formula, "target": m.target}
            for m in ir.metrics
        ]

    return cfg


def _build_algo_map(ir: SimIR) -> dict[str, dict]:
    """Build name → {central, local} map from algorithm layer."""
    return {
        a.name: {"central": a.central, "local": a.local}
        for a in ir.algorithm.algorithms
    }


def _find_planner(actor, algo_map: dict) -> dict | None:
    """Heuristic: bind algorithm to actor based on autonomy."""
    if not algo_map:
        return None
    # High autonomy → first algo's local; Low autonomy → first algo's central
    first_algo = next(iter(algo_map.values()))
    if actor.autonomy == "low":
        if first_algo.get("central"):
            return {"type": "central", "algorithm": first_algo["central"]}
    elif actor.autonomy == "high":
        if first_algo.get("local"):
            return {"type": "local", "algorithm": first_algo["local"]}
    return None


def _extract_channels(ir: SimIR) -> list[dict]:
    channels: list[dict] = []
    for c in ir.institution.contracts:
        if c.governance.sharing_mode:
            for part in c.governance.sharing_mode.split(" ; "):
                parts = part.split(" -> ")
                if len(parts) == 2:
                    src = parts[0].strip()
                    rest = parts[1].strip()
                    colon = rest.find(" : ")
                    if colon >= 0:
                        tgt = rest[:colon].strip()
                        data = rest[colon + 3:].strip()
                        channels.append({
                            "name": data,
                            "from": src,
                            "to": tgt,
                        })
    return channels


def _governance_summary(ir: SimIR) -> dict:
    """Aggregate governance params across contracts."""
    alphas, betas, lambdas = [], [], []
    holders = set()
    for c in ir.institution.contracts:
        g = c.governance
        if g.alpha is not None:
            alphas.append(g.alpha)
        if g.beta is not None:
            betas.append(g.beta)
        if g.lambda_ is not None:
            lambdas.append(g.lambda_)
        if g.decision_holder:
            holders.add(g.decision_holder)

    result: dict = {}
    if holders:
        result["decision_holders"] = sorted(holders)
    if alphas:
        result["information_transparency"] = round(sum(alphas) / len(alphas), 2)
    if betas:
        result["authority_centralization"] = round(sum(betas) / len(betas), 2)
    if lambdas:
        result["incentive_alignment"] = round(sum(lambdas) / len(lambdas), 2)
    return result


def _step_to_dict(step) -> dict:
    d: dict = {"type": step.type}
    if step.sender:
        d["from"] = step.sender
    if step.receiver:
        d["to"] = step.receiver
    if step.content:
        d["content"] = step.content
    if step.condition:
        d["condition"] = step.condition
    return d


def _transition_dict(t) -> dict:
    d: dict = {"from": t.from_regime, "to": t.to_regime}
    if t.condition:
        d["condition"] = t.condition
    if t.protocol:
        d["protocol"] = t.protocol
    return d
