"""Validate a SimIR instance and return human-readable error messages."""

from __future__ import annotations

from .ir import SimIR


def validate_ir(ir: SimIR) -> list[str]:
    """Validate the SimIR and return a list of error strings (empty = valid)."""
    errors: list[str] = []
    _check_actors(ir, errors)
    _check_duplicate_ids(ir, errors)
    _check_contract_parties(ir, errors)
    _check_protocol_refs(ir, errors)
    _check_governance_ranges(ir, errors)
    _check_transitions(ir, errors)
    return errors


def _check_actors(ir: SimIR, errors: list[str]) -> None:
    if not ir.institution.actors:
        errors.append("No actors defined — at least one actor is required.")


def _check_duplicate_ids(ir: SimIR, errors: list[str]) -> None:
    # Actor IDs
    seen: set[str] = set()
    for a in ir.institution.actors:
        if a.id in seen:
            errors.append(f"Duplicate actor ID: '{a.id}'.")
        seen.add(a.id)

    # Contract IDs
    seen.clear()
    for c in ir.institution.contracts:
        if c.id in seen:
            errors.append(f"Duplicate contract ID: '{c.id}'.")
        seen.add(c.id)

    # Protocol IDs
    seen.clear()
    for p in ir.protocol.protocols:
        if p.id in seen:
            errors.append(f"Duplicate protocol ID: '{p.id}'.")
        seen.add(p.id)


def _actor_ids(ir: SimIR) -> set[str]:
    ids: set[str] = set()
    for a in ir.institution.actors:
        ids.add(a.id)
        # Also add parameterized variants like ROBOT[*], ROBOT[i]
        ids.add(f"{a.id}[*]")
        ids.add(f"{a.id}[i]")
    return ids


def _strip_index(name: str) -> str:
    """Strip index suffix: 'ROBOT[*]' -> 'ROBOT', 'ROBOT[1]' -> 'ROBOT'."""
    bracket = name.find("[")
    if bracket >= 0:
        return name[:bracket]
    return name


def _check_contract_parties(ir: SimIR, errors: list[str]) -> None:
    actor_base_ids = {a.id for a in ir.institution.actors}
    for c in ir.institution.contracts:
        for party in c.parties:
            base = _strip_index(party)
            if base not in actor_base_ids:
                errors.append(
                    f"Contract '{c.id}': party '{party}' does not match "
                    f"any actor. Known actors: {sorted(actor_base_ids)}."
                )


def _check_protocol_refs(ir: SimIR, errors: list[str]) -> None:
    actor_base_ids = {a.id for a in ir.institution.actors}
    for p in ir.protocol.protocols:
        for step in p.steps:
            if step.sender:
                base = _strip_index(step.sender)
                if base not in actor_base_ids:
                    errors.append(
                        f"Protocol '{p.id}': step references unknown "
                        f"sender '{step.sender}'. Known actors: "
                        f"{sorted(actor_base_ids)}."
                    )
            if step.receiver:
                base = _strip_index(step.receiver)
                if base not in actor_base_ids:
                    errors.append(
                        f"Protocol '{p.id}': step references unknown "
                        f"receiver '{step.receiver}'. Known actors: "
                        f"{sorted(actor_base_ids)}."
                    )


def _check_governance_ranges(ir: SimIR, errors: list[str]) -> None:
    for c in ir.institution.contracts:
        g = c.governance
        for name, val in [
            ("alpha", g.alpha),
            ("beta", g.beta),
            ("lambda", g.lambda_),
        ]:
            if val is not None and not (0.0 <= val <= 1.0):
                errors.append(
                    f"Contract '{c.id}': governance parameter '{name}' = "
                    f"{val} is outside valid range [0.0, 1.0]."
                )


def _check_transitions(ir: SimIR, errors: list[str]) -> None:
    if not ir.transitions:
        return
    regimes: set[str] = set()
    for t in ir.transitions:
        regimes.add(t.from_regime)
        regimes.add(t.to_regime)

    # Check all protocol references in transitions
    protocol_ids = {p.id for p in ir.protocol.protocols}
    for t in ir.transitions:
        if t.protocol and t.protocol not in protocol_ids:
            errors.append(
                f"Transition {t.from_regime} -> {t.to_regime}: references "
                f"unknown protocol '{t.protocol}'. Known protocols: "
                f"{sorted(protocol_ids)}."
            )
