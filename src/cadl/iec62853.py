"""Dependability summary of a CADL definition, organised around the themes
of IEC 62853 (Open Systems Dependability).

The indicator names below are defined by CADL. They are not terms of
IEC 62853, and the report is informative: it does not assess conformance
to the standard.

CADL-defined indicators:
    alpha (information sharing)  → Information Transparency Level
    beta (authority)             → Governance Centralization Index
    lambda (incentives)          → Stakeholder Alignment Metric
    ContractDef                  → Service Level Agreement (SLA)
    ViolationBlock               → Failure Response Specification
    TransitionDef                → Operational State Machine
    SoS type (D/A/C/V)          → System Integration Level
"""

from __future__ import annotations

from .ast_nodes import SoSDefinition, SoSType


DISCLAIMER = (
    "Indicator names are defined by CADL, not by IEC 62853; this report "
    "is informative and does not assess conformance to the standard."
)


# CADL-defined integration level per SoS type (not an IEC 62853 scale)
_SOS_TYPE_TO_INTEGRATION = {
    SoSType.DIRECTED: "Level 4 — Centrally Managed",
    SoSType.ACKNOWLEDGED: "Level 3 — Acknowledged Integration",
    SoSType.COLLABORATIVE: "Level 2 — Collaborative Integration",
    SoSType.VIRTUAL: "Level 1 — Emergent / Virtual",
}


def generate_iec62853_report(sos: SoSDefinition) -> dict:
    """Generate an IEC 62853-oriented dependability summary of a CADL SoS.

    The ``iec62853_concept`` keys hold CADL-defined indicator names; the key
    name is kept for backward compatibility.

    Returns a dict suitable for JSON serialization or text formatting.
    """
    report: dict = {
        "disclaimer": DISCLAIMER,
        "sos_name": sos.name,
        "sos_type": sos.type.value if sos.type else "Unspecified",
        "system_integration_level": _SOS_TYPE_TO_INTEGRATION.get(
            sos.type, "Unspecified"
        ) if sos.type else "Unspecified",
    }

    # Institutional parameters
    params = []
    betas = []
    alphas = []
    lambdas = []

    for contract in sos.contracts:
        if contract.authority and contract.authority.beta is not None:
            betas.append(contract.authority.beta)
            params.append({
                "contract_id": contract.id,
                "cadl_concept": f"beta (authority centralization)",
                "value": contract.authority.beta,
                "iec62853_concept": "Governance Centralization Index",
                "description": _beta_description(contract.authority.beta),
            })
        if contract.information and contract.information.alpha is not None:
            alphas.append(contract.information.alpha)
            params.append({
                "contract_id": contract.id,
                "cadl_concept": f"alpha (information sharing)",
                "value": contract.information.alpha,
                "iec62853_concept": "Information Transparency Level",
                "description": _alpha_description(contract.information.alpha),
            })
        if contract.incentives and contract.incentives.lambda_ is not None:
            lambdas.append(contract.incentives.lambda_)
            params.append({
                "contract_id": contract.id,
                "cadl_concept": f"lambda (incentive alignment)",
                "value": contract.incentives.lambda_,
                "iec62853_concept": "Stakeholder Alignment Metric",
                "description": _lambda_description(contract.incentives.lambda_),
            })

    report["institutional_parameters"] = params

    # Service Level Agreements
    slas = []
    for contract in sos.contracts:
        sla: dict = {
            "contract_id": contract.id,
            "parties": [_party_str(p) for p in contract.parties],
            "assumption_count": len(contract.assume),
            "guarantee_count": len(contract.guarantee),
        }
        if contract.violation:
            failure = []
            if contract.violation.detect:
                failure.append(f"detect: {contract.violation.detect}")
            if contract.violation.action:
                failure.append(f"action: {contract.violation.action}")
            if contract.violation.escalation:
                failure.append(f"escalation: {contract.violation.escalation}")
            sla["failure_response"] = "; ".join(failure) if failure else None
        else:
            sla["failure_response"] = None
        slas.append(sla)

    report["service_level_agreements"] = slas

    # Operational State Machine
    if sos.transitions:
        from .regime_map import RegimeMap
        rm = RegimeMap.from_sos(sos)
        report["operational_state_machine"] = {
            "state_count": len(rm.states),
            "transition_count": len(rm.transitions),
            "states": sorted(rm.states.keys()),
            "initial_state": rm.initial_state,
            "has_cycles": len(rm.find_cycles()) > 0,
            "dead_states": sorted(rm.find_dead_states()),
            "unreachable_states": sorted(rm.find_unreachable_states()),
        }
    else:
        report["operational_state_machine"] = None

    # Dependability summary
    report["dependability_summary"] = {
        "total_contracts": len(sos.contracts),
        "total_actors": len(sos.actors),
        "total_protocols": len(sos.protocols),
        "governance_index": round(sum(betas) / len(betas), 3) if betas else "N/A",
        "transparency_level": round(sum(alphas) / len(alphas), 3) if alphas else "N/A",
        "alignment_metric": round(sum(lambdas) / len(lambdas), 3) if lambdas else "N/A",
    }

    return report


def _beta_description(beta: float) -> str:
    if beta >= 0.8:
        return "Highly centralized governance"
    if beta >= 0.5:
        return "Moderately centralized governance"
    if beta >= 0.2:
        return "Distributed governance"
    return "Highly distributed governance"


def _alpha_description(alpha: float) -> str:
    if alpha >= 0.8:
        return "High information transparency"
    if alpha >= 0.5:
        return "Moderate information transparency"
    if alpha >= 0.2:
        return "Limited information sharing"
    return "Minimal information sharing"


def _lambda_description(lambda_: float) -> str:
    if lambda_ >= 0.8:
        return "Strong incentive alignment"
    if lambda_ >= 0.5:
        return "Moderate incentive alignment"
    if lambda_ >= 0.2:
        return "Weak incentive alignment"
    return "Misaligned incentives"


def _party_str(party) -> str:
    from .ast_nodes import ActorRef
    if isinstance(party, ActorRef):
        if party.index == "*":
            return f"{party.name}[*]"
        if party.index is not None:
            return f"{party.name}[{party.index}]"
        return party.name
    return str(party)
