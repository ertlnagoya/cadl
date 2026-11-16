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
from ...norms import omitted_comment, omitted_in_codegen
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
            code = generate_contract_sol(contract, sos, _bindings(sos))
            filename = f"{sanitize_id(contract.id)}.sol"
            (output_dir / filename).write_text(code, encoding="utf-8")

        # Generate regime controller if transitions exist
        if sos.transitions:
            code = generate_regime_sol(sos, _bindings(sos))
            (output_dir / "RegimeController.sol").write_text(code, encoding="utf-8")

        # Generate main orchestrator
        code = generate_main_sol(sos, _bindings(sos))
        (output_dir / f"{sanitize_id(sos.name)}.sol").write_text(code, encoding="utf-8")


def _bindings(sos: SoSDefinition) -> list | None:
    """The regime bindings of `regimes:` (Appendix A, A.7.1), or None for a
    file without the block or without transitions: the code is then the
    same as before the block existed."""
    if not sos.transitions:
        return None
    from ...regimes import checked
    return checked(sos)


def _bound_institutional(contract: ContractDef, bindings: list | None) -> dict:
    """attr ("alpha", "beta", "lambda") -> {regime: value} for each value of
    the contract that some regime binds."""
    out: dict = {}
    for b in bindings or []:
        for key, value in b.parameters.items():
            owner, attr = key.split(".", 1)
            if owner == contract.id:
                out.setdefault(attr, {})[b.name] = value
    return out


def _needs_controller(contract: ContractDef, bindings: list | None) -> bool:
    """Whether the Solidity contract reads the regime controller: some regime
    does not have it in force or binds one of its values."""
    if bindings is None:
        return False
    return (any(contract.id not in b.active for b in bindings)
            or bool(_bound_institutional(contract, bindings)))


def generate_contract_sol(contract: ContractDef, sos: SoSDefinition,
                          bindings: list | None = None) -> str:
    """Generate a Solidity contract for a CADL ContractDef. With the regime
    bindings of `regimes:` (A.7.1), a contract that some regime does not
    have in force, or whose value some regime binds, reads the regime
    controller it is given in its constructor."""
    name = sanitize_id(contract.id)
    ctx = SolidityContext()
    regimed = _needs_controller(contract, bindings)
    bound = _bound_institutional(contract, bindings) if regimed else {}
    stepped = bindings is not None      # a step is one call of the orchestrator (A.7.1)

    lines: list[str] = []
    lines.append("// SPDX-License-Identifier: Apache-2.0")
    lines.append(f"// Generated from CADL contract: {contract.id}")
    lines.append("pragma solidity ^0.8.20;")
    lines.append("")
    if regimed:
        lines.append('import "./RegimeController.sol";')
        lines.append("")
    lines.append(f"contract {name} {{")
    lines.append("")
    if regimed:
        lines.append("    // The regime controller: the bindings of the current regime apply")
        lines.append("    // (regimes:, Appendix A, A.7.1)")
        lines.append("    RegimeController public regimeController;")
        lines.append("")
    if stepped:
        _orchestrated(lines, "RegimeController controller" if regimed else "",
                      "regimeController = controller;" if regimed else None)

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
        if "beta" in bound:
            _regime_value(lines, "BETA", contract.authority.beta, bound["beta"])
        else:
            beta_scaled = int(contract.authority.beta * 1000)
            lines.append(f"    uint256 public constant BETA = {beta_scaled}; // {contract.authority.beta} * 1000")
    if contract.information and contract.information.alpha is not None:
        if "alpha" in bound:
            _regime_value(lines, "ALPHA", contract.information.alpha, bound["alpha"])
        else:
            alpha_scaled = int(contract.information.alpha * 1000)
            lines.append(f"    uint256 public constant ALPHA = {alpha_scaled}; // {contract.information.alpha} * 1000")
    if contract.incentives and contract.incentives.lambda_ is not None:
        if "lambda" in bound:
            _regime_value(lines, "LAMBDA", contract.incentives.lambda_, bound["lambda"])
        else:
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
            if omitted_in_codegen(pred):
                lines.append(f"        // {omitted_comment(pred)}")
                continue
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
            if omitted_in_codegen(pred):
                lines.append(f"        // {omitted_comment(pred)}")
                continue
            sol_expr = _compile_predicate(pred, ctx)
            label = _predicate_label(pred)
            lines.append(f"        // {label}")
            lines.append(f"        if (!({sol_expr})) return false;")
    lines.append("        return true;")
    lines.append("    }")
    lines.append("")

    # runMonitorCycle
    lines.append("    function runMonitorCycle() public {" if not stepped
                 else "    function runMonitorCycle() public onlyOrchestrator {")
    if regimed and any(contract.id not in b.active for b in bindings):
        lines.append("        // not checked in a regime that does not have the contract in force")
        lines.append(f'        if (!regimeController.contractActive("{contract.id}")) return;')
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


def _orchestrated(lines: list, params: str, init: str | None) -> None:
    """With regime bindings (A.7.1) one step of a run is one call of the
    orchestrator: the contract it deploys is driven only by it, and it
    sets the observed state that the predicates read."""
    lines.append("    // Only the orchestrator that deploys this contract runs its step and")
    lines.append("    // sets the observed state (Appendix A, A.7.1)")
    lines.append("    address public orchestrator;")
    lines.append("")
    lines.append("    modifier onlyOrchestrator() {")
    lines.append('        require(msg.sender == orchestrator, "only the orchestrator");')
    lines.append("        _;")
    lines.append("    }")
    lines.append("")
    lines.append(f"    constructor({params}) {{")
    lines.append("        orchestrator = msg.sender;")
    if init:
        lines.append(f"        {init}")
    lines.append("    }")
    lines.append("")
    lines.append("    function setUint(string calldata key, uint256 value) external onlyOrchestrator {")
    lines.append("        stateUint[key] = value;")
    lines.append("    }")
    lines.append("")
    lines.append("    function setBool(string calldata key, bool value) external onlyOrchestrator {")
    lines.append("        stateBool[key] = value;")
    lines.append("    }")
    lines.append("")


def _hex_string(text: str) -> str:
    """A text of the file as a Solidity hex literal (no text is code)."""
    return f'string(hex"{text.encode("utf-8").hex()}")'


def _regime_value(lines: list, name: str, declared: float, per_regime: dict) -> None:
    """A value of the contract that some regime binds, as a view function
    with the name of the constant it replaces (same getter)."""
    lines.append(f"    // {name} in the current regime (regimes:, Appendix A, A.7.1), * 1000;")
    lines.append(f"    // declared {declared}")
    lines.append(f"    function {name}() public view returns (uint256) {{")
    lines.append("        RegimeController.Regime r = regimeController.currentRegime();")
    for regime, value in per_regime.items():
        lines.append(f"        if (r == RegimeController.Regime.{sanitize_id(regime)}) "
                     f"return {int(value * 1000)}; // {value} * 1000")
    lines.append(f"        return {int(declared * 1000)}; // {declared} * 1000")
    lines.append("    }")


def generate_regime_sol(sos: SoSDefinition, bindings: list | None = None) -> str:
    """Generate a Solidity contract for regime transitions. With the regime
    bindings of `regimes:` (A.7.1) it starts in the initial regime and
    answers which contracts are in force."""
    regimes = sorted({t.from_regime for t in sos.transitions} | {t.to_regime for t in sos.transitions})
    ctx = SolidityContext()

    lines: list[str] = []
    lines.append("// SPDX-License-Identifier: Apache-2.0")
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
    if bindings is not None:
        from ...regimes import initial_regime
        lines.append("    // The initial regime (Appendix A, A.7.1)")
        lines.append(f"    Regime public currentRegime = Regime.{sanitize_id(initial_regime(sos))};")
    else:
        lines.append(f"    Regime public currentRegime = Regime.{sanitize_id(regimes[0])};")
    lines.append("")

    # Events
    lines.append("    event RegimeTransition(Regime from_, Regime to_);")
    if bindings is not None:
        lines.append("    // a transition whose safety invariant does not hold is not taken (A.7.1)")
        lines.append("    event RegimeTransitionRefused(Regime from_, Regime to_, string invariant);")
    lines.append("")

    # State for conditions
    lines.append("    mapping(string => uint256) public stateUint;")
    lines.append("    mapping(string => bool) public stateBool;")
    lines.append("")

    if bindings is not None:
        _orchestrated(lines, "", None)
        lines.append("    function evaluateTransitions() public onlyOrchestrator {")
        for t in sos.transitions:
            from_id = sanitize_id(t.from_regime)
            to_id = sanitize_id(t.to_regime)
            if t.condition:
                cond = _compile_condition(t.condition, ctx)
                lines.append(f"        if (currentRegime == Regime.{from_id} && ({cond})) {{")
            else:
                lines.append(f"        if (currentRegime == Regime.{from_id}) {{")
            if t.safety_invariant:
                inv = _compile_invariant(t.safety_invariant, ctx)
                lines.append(f"            // safety_invariant: {_comment(t.safety_invariant)}")
                lines.append(f"            if (!({inv})) {{")
                lines.append(f"                emit RegimeTransitionRefused(currentRegime, Regime.{to_id}, "
                             f"{_hex_string(t.safety_invariant)});")
                lines.append("                return;")
                lines.append("            }")
            lines.append(f"            emit RegimeTransition(currentRegime, Regime.{to_id});")
            lines.append(f"            currentRegime = Regime.{to_id};")
            lines.append("            return;")
            lines.append("        }")
        lines.append("    }")
        lines.append("")

    # evaluateTransitions
    if bindings is None:
        lines.append("    function evaluateTransitions() public {")
    for t in sos.transitions if bindings is None else []:
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
    if bindings is None:
        lines.append("    }")
        lines.append("")

    # getCurrentRegime
    lines.append("    function getCurrentRegime() public view returns (Regime) {")
    lines.append("        return currentRegime;")
    lines.append("    }")
    lines.append("")

    if bindings is not None:
        lines.append("    /// Whether a contract is in force in the current regime (regimes:,")
        lines.append("    /// Appendix A, A.7.1).")
        lines.append("    function contractActive(string memory contractId) public view returns (bool) {")
        lines.append("        bytes32 h = keccak256(bytes(contractId));")
        for b in bindings:
            if not b.suspended:
                continue
            ids = " || ".join(f'h == keccak256("{c}")' for c in b.suspended)
            lines.append(f"        if (currentRegime == Regime.{sanitize_id(b.name)}) return !({ids});")
        lines.append("        return true;")
        lines.append("    }")
        lines.append("")

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def generate_main_sol(sos: SoSDefinition, bindings: list | None = None) -> str:
    """Generate the main orchestrator Solidity contract."""
    name = sanitize_id(sos.name)

    lines: list[str] = []
    lines.append("// SPDX-License-Identifier: Apache-2.0")
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

    if bindings is not None:
        lines.append("    // One step of a run (Appendix A, A.7.1) is one call of runMonitorCycle()")
        lines.append("    // by the reporter, the deployer; it also sets the observed state.")
        lines.append("    address public reporter;")
        lines.append("")
        lines.append("    modifier onlyReporter() {")
        lines.append('        require(msg.sender == reporter, "only the reporter");')
        lines.append("        _;")
        lines.append("    }")
        lines.append("")

    # Constructor
    lines.append("    constructor() {")
    if bindings is not None:
        lines.append("        reporter = msg.sender;")
        # the controller first: the contracts that read it get it
        lines.append("        regimeController = new RegimeController();")
    for contract in sos.contracts:
        cname = sanitize_id(contract.id)
        var_name = snake_case(contract.id)
        arg = "regimeController" if _needs_controller(contract, bindings) else ""
        lines.append(f"        {var_name} = new {cname}({arg});")
    if sos.transitions and bindings is None:
        lines.append("        regimeController = new RegimeController();")
    lines.append("    }")
    lines.append("")

    if bindings is not None:
        for fn, typ in (("setUint", "uint256"), ("setBool", "bool")):
            lines.append(f"    /// The observed value of a quantity, for every contract and the controller")
            lines.append(f"    function {fn}(string calldata key, {typ} value) external onlyReporter {{")
            for contract in sos.contracts:
                lines.append(f"        {snake_case(contract.id)}.{fn}(key, value);")
            lines.append(f"        regimeController.{fn}(key, value);")
            lines.append("    }")
            lines.append("")

    # runMonitorCycle
    lines.append("    function runMonitorCycle() public {" if bindings is None
                 else "    function runMonitorCycle() public onlyReporter {")
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


def _compile_invariant(text: str, ctx: SolidityContext) -> str:
    """A safety invariant in Solidity; `false` when it cannot be compiled,
    so that the transition is refused rather than taken unchecked."""
    from ...parser import parse_expr
    try:
        return predicate_to_solidity(parse_expr(text), ctx)
    except Exception:
        return "false"


def _comment(text: str) -> str:
    return " ".join(text.split()).replace("*/", "* /")


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
