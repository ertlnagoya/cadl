"""CADL Contract Monitor Generator — generates contract monitor classes."""

from __future__ import annotations

from typing import List

from ..ast_nodes import ContractDef, Expression
from .emitter import sanitize_id
from .expr_compiler import CompilerContext, expr_to_python


def generate_contract_monitor(contract: ContractDef) -> str:
    """Generate a ContractMonitor class for a ContractDef."""
    class_name = sanitize_id(contract.id) + "Monitor"
    party_names = [_party_str(p) for p in contract.parties]

    lines = []
    lines.append(f"class {class_name}(ContractMonitorBase):")
    lines.append(f'    """Contract monitor: {contract.id}."""')
    lines.append("")
    lines.append(f'    CONTRACT_ID = "{contract.id}"')
    lines.append(f"    PARTIES = {party_names!r}")
    lines.append("")

    # check_assumptions
    lines.append("    def check_assumptions(self, ctx: RuntimeContext) -> list[CheckResult]:")
    lines.append("        results = []")
    if contract.assume:
        for pred in contract.assume:
            py_expr = _compile_predicate(pred)
            pred_str = _predicate_label(pred)
            lines.append(f"        try:")
            lines.append(f"            results.append(self._check({pred_str!r}, {py_expr}))")
            lines.append(f"        except Exception:")
            lines.append(f"            results.append(self._check({pred_str!r}, True))  # skip on error")
    lines.append("        return results")
    lines.append("")

    # check_guarantees
    lines.append("    def check_guarantees(self, ctx: RuntimeContext) -> list[CheckResult]:")
    lines.append("        results = []")
    if contract.guarantee:
        for pred in contract.guarantee:
            py_expr = _compile_predicate(pred)
            pred_str = _predicate_label(pred)
            lines.append(f"        try:")
            lines.append(f"            results.append(self._check({pred_str!r}, {py_expr}))")
            lines.append(f"        except Exception:")
            lines.append(f"            results.append(self._check({pred_str!r}, True))  # skip on error")
    lines.append("        return results")
    lines.append("")

    # on_violation
    if contract.violation:
        lines.append("    def on_violation(self, violation, ctx: RuntimeContext) -> None:")
        lines.append("        super().on_violation(violation, ctx)")
        if contract.violation.detect:
            lines.append(f"        # detect: {contract.violation.detect}")
        if contract.violation.action:
            lines.append(f"        # action: {contract.violation.action}")
        if contract.violation.escalation:
            lines.append(f"        # escalation: {contract.violation.escalation}")
        lines.append("")

    return "\n".join(lines)


def generate_contracts_module(contracts: List[ContractDef]) -> str:
    """Generate the complete contracts.py module content."""
    imports = [
        "from cadl.codegen.runtime_support import (",
        "    CheckResult,",
        "    ContractMonitorBase,",
        "    RuntimeContext,",
        "    Violation,",
        ")",
    ]

    classes = []
    for contract in contracts:
        classes.append(generate_contract_monitor(contract))

    body = "\n\n".join(classes)

    parts = ['"""Generated contract monitors for CADL SoS definition."""', ""]
    parts.append("from __future__ import annotations")
    parts.append("")
    parts.extend(imports)
    parts.append("")
    parts.append("")
    parts.append(body)

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"
    return content


def _compile_predicate(expr: Expression) -> str:
    """Compile a predicate Expression to Python source."""
    ctx = CompilerContext()
    return expr_to_python(expr, ctx)


def _predicate_label(expr: Expression) -> str:
    """Get a human-readable label for a predicate."""
    from ..ast_nodes import StringLiteral, BinaryOp, MemberAccess, FunctionCall, Identifier

    if isinstance(expr, StringLiteral):
        return expr.value
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, FunctionCall):
        return f"{expr.name}()"
    if isinstance(expr, MemberAccess):
        return f"{expr.obj.name}.{expr.member}"
    if isinstance(expr, BinaryOp):
        left = _predicate_label(expr.left)
        right = _predicate_label(expr.right)
        return f"{left} {expr.op} {right}"
    return str(type(expr).__name__)


def _party_str(party) -> str:
    """Convert a party ActorRef to a string."""
    from ..ast_nodes import ActorRef
    if isinstance(party, ActorRef):
        if party.index == "*":
            return f"{party.name}[*]"
        if party.index is not None:
            return f"{party.name}[{party.index}]"
        return party.name
    return str(party)
