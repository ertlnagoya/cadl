"""CADL Solidity Contract Generator.

Generates Ethereum smart contracts (.sol) from CADL SoS definitions.
Each ContractDef becomes a Solidity contract with assume/guarantee checks,
violation handling, and regime transition support.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ...ast_nodes import (
    ContractDef,
    Expression,
    ProtocolDef,
    SoSDefinition,
    TransitionDef,
)
from ..emitter import sanitize_id, snake_case
from ..expr_compiler import declared_actors
from .solidity_expr import SolidityContext, expr_to_solidity, predicate_to_solidity


def generate_solidity(sos: SoSDefinition, output_dir: Path) -> None:
    """Generate Solidity smart contracts from a CADL SoS definition.

    Creates one .sol file per contract, plus a main orchestrator contract.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with declared_actors(a.id.name for a in sos.actors):
        # Generate a contract for each ContractDef
        for contract in sos.contracts:
            code = generate_contract_sol(contract, sos)
            filename = f"{sanitize_id(contract.id)}.sol"
            (output_dir / filename).write_text(code, encoding="utf-8")

        # Generate regime controller if transitions exist
        if sos.transitions:
            code = generate_regime_sol(sos)
            (output_dir / "RegimeController.sol").write_text(code, encoding="utf-8")

        # Generate main orchestrator
        code = generate_main_sol(sos)
        (output_dir / f"{sanitize_id(sos.name)}.sol").write_text(code, encoding="utf-8")


def generate_contract_sol(contract: ContractDef, sos: SoSDefinition) -> str:
    """Generate a Solidity contract for a CADL ContractDef."""
    name = sanitize_id(contract.id)
    ctx = SolidityContext()

    lines: list[str] = []
    lines.append("// SPDX-License-Identifier: MIT")
    lines.append(f"// Generated from CADL contract: {contract.id}")
    lines.append("pragma solidity ^0.8.20;")
    lines.append("")
    lines.append(f"contract {name} {{")
    lines.append("")

    # Party addresses
    for party in contract.parties:
        party_name = snake_case(party.name.replace("[", "").replace("]", "").replace("*", "All"))
        if party.index == "*" or party.index is not None:
            lines.append(f"    address[] public {party_name};")
        else:
            lines.append(f"    address public {party_name};")
    lines.append("")

    # State variables
    lines.append("    // Contract state")
    lines.append("    mapping(string => uint256) public stateUint;")
    lines.append("    mapping(string => bool) public stateBool;")
    lines.append("    uint256 public violationCount;")
    lines.append("    bool public suspended;")
    lines.append("")

    # Institutional parameters
    if contract.authority and contract.authority.beta is not None:
        beta_scaled = int(contract.authority.beta * 1000)
        lines.append(f"    uint256 public constant BETA = {beta_scaled}; // {contract.authority.beta} * 1000")
    if contract.information and contract.information.alpha is not None:
        alpha_scaled = int(contract.information.alpha * 1000)
        lines.append(f"    uint256 public constant ALPHA = {alpha_scaled}; // {contract.information.alpha} * 1000")
    if contract.incentives and contract.incentives.lambda_ is not None:
        lambda_scaled = int(contract.incentives.lambda_ * 1000)
        lines.append(f"    uint256 public constant LAMBDA = {lambda_scaled}; // {contract.incentives.lambda_} * 1000")
    lines.append("")

    # Events
    lines.append("    event AssumptionChecked(string predicate, bool result);")
    lines.append("    event GuaranteeChecked(string predicate, bool result);")
    lines.append("    event ViolationDetected(string contractId, string message);")
    lines.append("")

    # checkAssumptions
    lines.append("    function checkAssumptions() public view returns (bool) {")
    if contract.assume:
        for pred in contract.assume:
            sol_expr = _compile_predicate(pred, ctx)
            label = _predicate_label(pred)
            lines.append(f"        // {label}")
            lines.append(f"        if (!({sol_expr})) return false;")
    lines.append("        return true;")
    lines.append("    }")
    lines.append("")

    # checkGuarantees
    lines.append("    function checkGuarantees() public view returns (bool) {")
    if contract.guarantee:
        for pred in contract.guarantee:
            sol_expr = _compile_predicate(pred, ctx)
            label = _predicate_label(pred)
            lines.append(f"        // {label}")
            lines.append(f"        if (!({sol_expr})) return false;")
    lines.append("        return true;")
    lines.append("    }")
    lines.append("")

    # runMonitorCycle
    lines.append("    function runMonitorCycle() public {")
    lines.append("        if (checkAssumptions() && !checkGuarantees()) {")
    lines.append(f'            emit ViolationDetected("{contract.id}", "Guarantee violated");')
    lines.append("            violationCount++;")
    if contract.violation and contract.violation.escalation:
        import re
        m = re.match(r'after (\d+) violations?:\s*(.*)', contract.violation.escalation)
        if m:
            threshold = m.group(1)
            lines.append(f"            if (violationCount >= {threshold}) {{")
            lines.append("                suspended = true;")
            lines.append("            }")
    lines.append("        }")
    lines.append("    }")
    lines.append("")

    # Incentive functions
    if contract.incentives and contract.incentives.rules:
        lines.append("    // Incentive functions")
        lines.append("    mapping(address => int256) public reputation;")
        lines.append("")
        lines.append("    function reward(address actor, int256 amount) internal {")
        lines.append("        reputation[actor] += amount;")
        lines.append("    }")
        lines.append("")
        lines.append("    function penalize(address actor, int256 amount) internal {")
        lines.append("        reputation[actor] += amount; // amount is negative")
        lines.append("    }")
        lines.append("")

    lines.append("}")
    lines.append("")

    return "\n".join(lines)


def generate_regime_sol(sos: SoSDefinition) -> str:
    """Generate a Solidity contract for regime transitions."""
    regimes = sorted({t.from_regime for t in sos.transitions} | {t.to_regime for t in sos.transitions})
    ctx = SolidityContext()

    lines: list[str] = []
    lines.append("// SPDX-License-Identifier: MIT")
    lines.append("// Generated CADL regime controller")
    lines.append("pragma solidity ^0.8.20;")
    lines.append("")
    lines.append("contract RegimeController {")
    lines.append("")

    # Regime enum
    lines.append("    enum Regime {")
    for i, r in enumerate(regimes):
        comma = "," if i < len(regimes) - 1 else ""
        lines.append(f"        {sanitize_id(r)}{comma}")
    lines.append("    }")
    lines.append("")
    lines.append(f"    Regime public currentRegime = Regime.{sanitize_id(regimes[0])};")
    lines.append("")

    # Events
    lines.append("    event RegimeTransition(Regime from_, Regime to_);")
    lines.append("")

    # State for conditions
    lines.append("    mapping(string => uint256) public stateUint;")
    lines.append("    mapping(string => bool) public stateBool;")
    lines.append("")

    # evaluateTransitions
    lines.append("    function evaluateTransitions() public {")
    for t in sos.transitions:
        from_id = sanitize_id(t.from_regime)
        to_id = sanitize_id(t.to_regime)
        if t.condition:
            cond = _compile_condition(t.condition, ctx)
            lines.append(f"        if (currentRegime == Regime.{from_id} && ({cond})) {{")
        else:
            lines.append(f"        if (currentRegime == Regime.{from_id}) {{")
        lines.append(f"            emit RegimeTransition(currentRegime, Regime.{to_id});")
        lines.append(f"            currentRegime = Regime.{to_id};")
        lines.append("            return;")
        lines.append("        }")
    lines.append("    }")
    lines.append("")

    # getCurrentRegime
    lines.append("    function getCurrentRegime() public view returns (Regime) {")
    lines.append("        return currentRegime;")
    lines.append("    }")
    lines.append("")

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def generate_main_sol(sos: SoSDefinition) -> str:
    """Generate the main orchestrator Solidity contract."""
    name = sanitize_id(sos.name)

    lines: list[str] = []
    lines.append("// SPDX-License-Identifier: MIT")
    lines.append(f"// Generated CADL orchestrator: {sos.name}")
    lines.append("pragma solidity ^0.8.20;")
    lines.append("")

    # Import contracts
    for contract in sos.contracts:
        cname = sanitize_id(contract.id)
        lines.append(f'import "./{cname}.sol";')
    if sos.transitions:
        lines.append('import "./RegimeController.sol";')
    lines.append("")

    lines.append(f"contract {name} {{")
    lines.append("")

    # Contract references
    for contract in sos.contracts:
        cname = sanitize_id(contract.id)
        var_name = snake_case(contract.id)
        lines.append(f"    {cname} public {var_name};")
    if sos.transitions:
        lines.append("    RegimeController public regimeController;")
    lines.append("")

    # Constructor
    lines.append("    constructor() {")
    for contract in sos.contracts:
        cname = sanitize_id(contract.id)
        var_name = snake_case(contract.id)
        lines.append(f"        {var_name} = new {cname}();")
    if sos.transitions:
        lines.append("        regimeController = new RegimeController();")
    lines.append("    }")
    lines.append("")

    # runMonitorCycle
    lines.append("    function runMonitorCycle() public {")
    for contract in sos.contracts:
        var_name = snake_case(contract.id)
        lines.append(f"        {var_name}.runMonitorCycle();")
    if sos.transitions:
        lines.append("        regimeController.evaluateTransitions();")
    lines.append("    }")
    lines.append("")

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _compile_predicate(expr: Expression, ctx: SolidityContext) -> str:
    """Compile a predicate expression to Solidity."""
    return predicate_to_solidity(expr, ctx)


def _compile_condition(cond_str: str, ctx: SolidityContext) -> str:
    """Compile a condition string to Solidity."""
    from ...parser import parse_expr
    try:
        expr = parse_expr(cond_str)
        return predicate_to_solidity(expr, ctx)
    except Exception:
        # Fallback: use as boolean state variable
        sanitized = cond_str.replace(" ", "_").replace(".", "_")[:40]
        sanitized = "".join(c for c in sanitized if c.isalnum() or c == "_")
        return f"stateBool[\"{sanitized}\"]"


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
