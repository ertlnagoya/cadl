"""Generate Unity simulator configuration (JSON) from SimIR."""

from __future__ import annotations

import json

from .ir import SimIR


def generate_unity_config(ir: SimIR) -> str:
    """Generate a JSON config for a Unity-based simulator."""
    config = _build_config(ir)
    return json.dumps(config, indent=2, ensure_ascii=False)


_AUTONOMY_PREFAB = {
    "low": "Agent_LowAutonomy",
    "medium": "Agent_MediumAutonomy",
    "high": "Agent_HighAutonomy",
}


def _build_config(ir: SimIR) -> dict:
    cfg: dict = {}

    # Simulator config
    cfg["simulatorConfig"] = {
        "name": ir.name,
        "sosType": ir.sos_type.lower(),
        "description": ir.description,
        "environment": dict(ir.environment) if ir.environment else {},
    }

    # Agent templates
    algo_map = {a.name: a for a in ir.algorithm.algorithms}
    templates = []
    for actor in ir.institution.actors:
        tmpl: dict = {
            "templateId": actor.id,
            "prefab": _AUTONOMY_PREFAB.get(actor.autonomy, "Agent_Default"),
            "role": actor.role,
            "autonomy": actor.autonomy,
        }
        if actor.count is not None:
            tmpl["count"] = actor.count
        if actor.capabilities:
            tmpl["capabilities"] = actor.capabilities
        # Planner binding
        planner = _find_planner_unity(actor, algo_map)
        if planner:
            tmpl["planner"] = planner
        if actor.inputs:
            tmpl["inputs"] = actor.inputs
        if actor.outputs:
            tmpl["outputs"] = actor.outputs
        templates.append(tmpl)
    cfg["agentTemplates"] = templates

    # Communication setup
    channels = _extract_channels(ir)
    governance = _governance_unity(ir)
    cfg["communicationSetup"] = {
        "channels": channels,
        "governance": governance,
    }

    # Protocols
    cfg["protocols"] = [
        _protocol_unity(p) for p in ir.protocol.protocols
    ]

    # Regime transitions
    if ir.transitions:
        cfg["regimeTransitions"] = [
            _transition_unity(t) for t in ir.transitions
        ]

    # Metrics
    if ir.metrics:
        cfg["metrics"] = [
            {"id": m.id, "formula": m.formula, "target": m.target}
            for m in ir.metrics
        ]

    return cfg


def _find_planner_unity(actor, algo_map: dict) -> dict | None:
    if not algo_map:
        return None
    first = next(iter(algo_map.values()))
    if actor.autonomy == "low" and first.central:
        return {"type": "central", "algorithm": first.central}
    if actor.autonomy == "high" and first.local:
        return {"type": "local", "algorithm": first.local}
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
                            "channelName": data,
                            "fromTemplate": src,
                            "toTemplate": tgt,
                        })
    return channels


def _governance_unity(ir: SimIR) -> dict:
    result: dict = {}
    for c in ir.institution.contracts:
        g = c.governance
        if g.decision_holder:
            result["decisionHolder"] = g.decision_holder
        if g.alpha is not None:
            result["alpha"] = g.alpha
        if g.beta is not None:
            result["beta"] = g.beta
        if g.lambda_ is not None:
            result["lambda"] = g.lambda_
    return result


def _protocol_unity(p) -> dict:
    proto: dict = {
        "protocolId": p.id,
        "trigger": p.trigger,
        "steps": [_step_unity(s) for s in p.steps],
    }
    if p.timing:
        proto["timing"] = p.timing
    if p.precondition:
        proto["precondition"] = p.precondition
    if p.postcondition:
        proto["postcondition"] = p.postcondition
    return proto


def _step_unity(step) -> dict:
    d: dict = {"stepType": step.type}
    if step.sender:
        d["sender"] = step.sender
    if step.receiver:
        d["receiver"] = step.receiver
    if step.content:
        d["content"] = step.content
    if step.condition:
        d["condition"] = step.condition
    return d


def _transition_unity(t) -> dict:
    d: dict = {"fromRegime": t.from_regime, "toRegime": t.to_regime}
    if t.condition:
        d["condition"] = t.condition
    if t.protocol:
        d["triggerProtocol"] = t.protocol
    return d
