"""CADL OPA/Rego Policy Generator.

Generates Open Policy Agent Rego policies (.rego) from CADL SoS definitions.
Each ContractDef becomes a Rego package with assume/guarantee rules,
violation detection, and authority/information policy rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ...ast_nodes import (
    ContractDef,
    Expression,
    SoSDefinition,
)
from ..emitter import sanitize_id, snake_case
from .rego_expr import RegoContext, expr_to_rego


def generate_rego(sos: SoSDefinition, output_dir: Path) -> None:
    """Generate OPA Rego policies from a CADL SoS definition.

    Creates one .rego file per contract, plus a main policy file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for contract in sos.contracts:
        code = generate_contract_rego(contract, sos)
        filename = f"{snake_case(contract.id)}.rego"
        (output_dir / filename).write_text(code, encoding="utf-8")

    # Generate main policy
    code = generate_main_rego(sos)
    (output_dir / "main.rego").write_text(code, encoding="utf-8")


def generate_contract_rego(contract: ContractDef, sos: SoSDefinition) -> str:
    """Generate a Rego policy for a CADL ContractDef."""
    pkg_name = snake_case(contract.id)
    ctx = RegoContext()

    lines: list[str] = []
    lines.append(f"# Generated from CADL contract: {contract.id}")
    lines.append(f"package cadl.{pkg_name}")
    lines.append("")
    lines.append("import rego.v1")
    lines.append("")

    # Metadata
    lines.append("# Contract metadata")
    lines.append(f'contract_id := "{contract.id}"')
    party_names = [_party_str(p) for p in contract.parties]
    lines.append(f"parties := {_rego_list(party_names)}")
    lines.append("")

    # Institutional parameters
    if contract.authority and contract.authority.beta is not None:
        lines.append(f"beta := {contract.authority.beta}")
    if contract.information and contract.information.alpha is not None:
        lines.append(f"alpha := {contract.information.alpha}")
    if contract.incentives and contract.incentives.lambda_ is not None:
        lines.append(f"lambda_ := {contract.incentives.lambda_}")
    if contract.authority or contract.information or contract.incentives:
        lines.append("")

    # Default allow
    lines.append("default allow := false")
    lines.append("")

    # assumptions_hold rule
    lines.append("# Assumptions: all must hold")
    lines.append("assumptions_hold if {")
    if contract.assume:
        for pred in contract.assume:
            rego_expr = _compile_predicate(pred, ctx)
            label = _predicate_label(pred)
            lines.append(f"    # {label}")
            lines.append(f"    {rego_expr}")
    else:
        lines.append("    true")
    lines.append("}")
    lines.append("")

    # guarantees_hold rule
    lines.append("# Guarantees: all must hold")
    lines.append("guarantees_hold if {")
    if contract.guarantee:
        for pred in contract.guarantee:
            rego_expr = _compile_predicate(pred, ctx)
            label = _predicate_label(pred)
            lines.append(f"    # {label}")
            lines.append(f"    {rego_expr}")
    else:
        lines.append("    true")
    lines.append("}")
    lines.append("")

    # allow rule
    lines.append("# Contract satisfied when assumptions imply guarantees hold")
    lines.append("allow if {")
    lines.append("    assumptions_hold")
    lines.append("    guarantees_hold")
    lines.append("}")
    lines.append("")

    # violation rule
    lines.append("# Violation: assumptions hold but guarantees do not")
    lines.append("violation contains msg if {")
    lines.append("    assumptions_hold")
    lines.append("    not guarantees_hold")
    lines.append(f'    msg := "Guarantee violated in {contract.id}"')
    lines.append("}")
    lines.append("")

    # Authority policy
    if contract.authority:
        lines.append("# Authority policy")
        if contract.authority.decision_scope:
            lines.append(f'decision_scope := "{contract.authority.decision_scope}"')
        if contract.authority.decision_holder:
            holder = contract.authority.decision_holder.name
            lines.append(f'decision_holder := "{holder}"')
        lines.append("")

    # Information sharing policy
    if contract.information and contract.information.sharing:
        lines.append("# Information sharing rules")
        for i, sharing in enumerate(contract.information.sharing):
            src = sharing.source.name.lower()
            tgt = sharing.target.name.lower()
            data = sharing.data
            lines.append(f"sharing_allowed_{i} if {{")
            lines.append(f'    input.source == "{src}"')
            lines.append(f'    input.target == "{tgt}"')
            lines.append(f'    input.data == "{data}"')
            lines.append("}")
        lines.append("")

    return "\n".join(lines)


def generate_main_rego(sos: SoSDefinition) -> str:
    """Generate the main Rego policy that aggregates all contracts."""
    pkg_name = snake_case(sos.name)

    lines: list[str] = []
    lines.append(f"# Generated CADL main policy: {sos.name}")
    lines.append(f"package cadl.{pkg_name}")
    lines.append("")
    lines.append("import rego.v1")
    lines.append("")

    # Import all contract packages
    for contract in sos.contracts:
        cpkg = snake_case(contract.id)
        lines.append(f"import data.cadl.{cpkg}")
    lines.append("")

    # SoS metadata
    lines.append(f'sos_name := "{sos.name}"')
    if sos.type:
        lines.append(f'sos_type := "{sos.type.value}"')
    if sos.version:
        lines.append(f'sos_version := "{sos.version}"')
    lines.append("")

    # Actor list
    if sos.actors:
        actor_names = [a.id.name for a in sos.actors]
        lines.append(f"actors := {_rego_list(actor_names)}")
        lines.append("")

    # Aggregate compliance check
    lines.append("default all_contracts_satisfied := false")
    lines.append("")
    lines.append("all_contracts_satisfied if {")
    for contract in sos.contracts:
        cpkg = snake_case(contract.id)
        lines.append(f"    data.cadl.{cpkg}.allow")
    if not sos.contracts:
        lines.append("    true")
    lines.append("}")
    lines.append("")

    # Aggregate violations
    lines.append("all_violations contains msg if {")
    if sos.contracts:
        for contract in sos.contracts:
            cpkg = snake_case(contract.id)
            lines.append(f"    msg := data.cadl.{cpkg}.violation[_]")
    else:
        lines.append('    msg := "no contracts"')
        lines.append("    false")
    lines.append("}")
    lines.append("")

    return "\n".join(lines)


def _compile_predicate(expr: Expression, ctx: RegoContext) -> str:
    """Compile a predicate expression to Rego."""
    return expr_to_rego(expr, ctx)


def _predicate_label(expr: Expression) -> str:
    """Get a human-readable label for a predicate."""
    from ...ast_nodes import StringLiteral, BinaryOp, MemberAccess, FunctionCall, Identifier

    if isinstance(expr, StringLiteral):
        return expr.value
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, FunctionCall) and not expr.args:
        return f"{expr.name}()"
    from ...unparse import expr_to_source
    return expr_to_source(expr)


def _party_str(party) -> str:
    """Convert a party ActorRef to a string."""
    from ...ast_nodes import ActorRef
    if isinstance(party, ActorRef):
        if party.index == "*":
            return f"{party.name}[*]"
        if party.index is not None:
            return f"{party.name}[{party.index}]"
        return party.name
    return str(party)


def _rego_list(items: list[str]) -> str:
    """Format a Python list as a Rego array literal."""
    quoted = [f'"{item}"' for item in items]
    return f"[{', '.join(quoted)}]"
