"""Generate Go simulator configuration (JSON) from SimIR."""

from __future__ import annotations

import json

from .ir import SimIR


def generate_go_config(ir: SimIR) -> str:
    """Generate a JSON config for a Go-based simulator."""
    config = _build_config(ir)
    return json.dumps(config, indent=2, ensure_ascii=False)


def _build_config(ir: SimIR) -> dict:
    cfg: dict = {}

    cfg["name"] = ir.name
    cfg["sos_type"] = ir.sos_type.lower()
    cfg["description"] = ir.description

    if ir.environment:
        cfg["env"] = dict(ir.environment)

    # Agents
    algo_map = {a.name: a for a in ir.algorithm.algorithms}
    agents = []
    for actor in ir.institution.actors:
        agent: dict = {
            "id": actor.id,
            "role": actor.role,
            "autonomy": actor.autonomy,
        }
        if actor.count is not None:
            agent["count"] = actor.count
        if actor.capabilities:
            agent["capabilities"] = actor.capabilities
        # Algorithm binding
        algo = _find_algo_go(actor, algo_map)
        if algo:
            agent["algorithm"] = algo
        iface: dict = {}
        if actor.inputs:
            iface["in"] = actor.inputs
        if actor.outputs:
            iface["out"] = actor.outputs
        if iface:
            agent["interface"] = iface
        agents.append(agent)
    cfg["agents"] = agents

    # Contracts
    contracts = []
    for c in ir.institution.contracts:
        contract: dict = {
            "id": c.id,
            "parties": c.parties,
        }
        if c.assume:
            contract["assume"] = c.assume
        if c.guarantee:
            contract["guarantee"] = c.guarantee
        gov: dict = {}
        if c.governance.alpha is not None:
            gov["alpha"] = c.governance.alpha
        if c.governance.beta is not None:
            gov["beta"] = c.governance.beta
        if c.governance.lambda_ is not None:
            gov["lambda"] = c.governance.lambda_
        if c.governance.decision_holder:
            gov["decision_holder"] = c.governance.decision_holder
        if gov:
            contract["governance"] = gov
        contracts.append(contract)
    cfg["contracts"] = contracts

    # Protocols
    protocols = []
    for p in ir.protocol.protocols:
        proto: dict = {
            "id": p.id,
            "trigger": p.trigger,
            "steps": [_step_go(s) for s in p.steps],
        }
        if p.timing:
            proto["timing"] = p.timing
        if p.fallback:
            proto["fallback"] = p.fallback
        protocols.append(proto)
    cfg["protocols"] = protocols

    # Transitions
    if ir.transitions:
        cfg["transitions"] = [
            _transition_go(t) for t in ir.transitions
        ]

    # Metrics
    if ir.metrics:
        cfg["metrics"] = [
            {"id": m.id, "formula": m.formula, "target": m.target}
            for m in ir.metrics
        ]

    return cfg


def _find_algo_go(actor, algo_map: dict) -> dict | None:
    if not algo_map:
        return None
    first = next(iter(algo_map.values()))
    central = first.central
    local = first.local
    if actor.autonomy == "low":
        return {"central": central, "local": None}
    if actor.autonomy == "high":
        return {"central": None, "local": local}
    return {"central": central, "local": local}


def _step_go(step) -> dict:
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


def _transition_go(t) -> dict:
    d: dict = {"from_regime": t.from_regime, "to_regime": t.to_regime}
    if t.condition:
        d["condition"] = t.condition
    if t.protocol:
        d["protocol"] = t.protocol
    return d
