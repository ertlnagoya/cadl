"""Tests for the CADL Solidity code generator."""

from __future__ import annotations

from pathlib import Path

import pytest

from cadl.ast_nodes import (
    ActorDef,
    ActorRef,
    AuthorityBlock,
    AutonomyLevel,
    BinaryOp,
    BoolLiteral,
    ContractDef,
    DurationLiteral,
    FloatLiteral,
    FunctionCall,
    Identifier,
    IncentivesBlock,
    IncentiveRule,
    InformationBlock,
    IntLiteral,
    MemberAccess,
    QuantifiedExpr,
    RangeExpr,
    SoSDefinition,
    SoSType,
    StringLiteral,
    TransitionDef,
    UnaryOp,
    ViolationBlock,
)
from cadl.codegen.solidity.solidity_expr import SolidityContext, expr_to_solidity
from cadl.codegen.solidity.solidity_gen import (
    generate_contract_sol,
    generate_main_sol,
    generate_regime_sol,
    generate_solidity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_sos(
    name: str = "TestSoS",
    contracts: list | None = None,
    transitions: list | None = None,
    actors: list | None = None,
) -> SoSDefinition:
    """Build a minimal SoSDefinition for testing."""
    return SoSDefinition(
        name=name,
        type=SoSType.DIRECTED,
        version="1.0.0",
        actors=actors or [],
        contracts=contracts or [],
        transitions=transitions or [],
    )


def _minimal_contract(
    cid: str = "TEST_SLA",
    parties: list | None = None,
    assume: list | None = None,
    guarantee: list | None = None,
    authority: AuthorityBlock | None = None,
    information: InformationBlock | None = None,
    incentives: IncentivesBlock | None = None,
    violation: ViolationBlock | None = None,
) -> ContractDef:
    return ContractDef(
        id=cid,
        parties=parties or [ActorRef(name="A"), ActorRef(name="B")],
        assume=assume or [],
        guarantee=guarantee or [],
        authority=authority,
        information=information,
        incentives=incentives,
        violation=violation,
    )


# ===========================================================================
# TestSolidityExpr
# ===========================================================================

class TestSolidityExpr:
    """Tests for expr_to_solidity()."""

    # --- Literals ---

    def test_bool_literal_true(self):
        assert expr_to_solidity(BoolLiteral(True)) == "true"

    def test_bool_literal_false(self):
        assert expr_to_solidity(BoolLiteral(False)) == "false"

    def test_int_literal(self):
        assert expr_to_solidity(IntLiteral(42)) == "42"

    def test_int_literal_zero(self):
        assert expr_to_solidity(IntLiteral(0)) == "0"

    def test_int_literal_negative(self):
        assert expr_to_solidity(IntLiteral(-5)) == "-5"

    def test_float_literal_scaled(self):
        result = expr_to_solidity(FloatLiteral(3.14))
        # 3.14 * 1000 = 3140
        assert "3140" in result
        assert "3.14" in result
        assert "1000" in result

    def test_float_literal_small(self):
        result = expr_to_solidity(FloatLiteral(0.5))
        assert "500" in result

    def test_string_literal(self):
        assert expr_to_solidity(StringLiteral("hello")) == '"hello"'

    def test_string_literal_with_quotes(self):
        result = expr_to_solidity(StringLiteral('say "hi"'))
        assert '\\"' in result

    def test_duration_literal_seconds(self):
        result = expr_to_solidity(DurationLiteral(5, "s"))
        assert result == "5000"

    def test_duration_literal_ms(self):
        result = expr_to_solidity(DurationLiteral(200, "ms"))
        assert result == "200"

    def test_duration_literal_minutes(self):
        result = expr_to_solidity(DurationLiteral(2, "min"))
        assert result == "120000"

    def test_duration_literal_hours(self):
        result = expr_to_solidity(DurationLiteral(1, "h"))
        assert result == "3600000"

    # --- Identifiers ---

    def test_identifier_maps_to_state(self):
        result = expr_to_solidity(Identifier("x"))
        assert result == "state.x"

    def test_identifier_local(self):
        ctx = SolidityContext(locals={"i"})
        result = expr_to_solidity(Identifier("i"), ctx)
        assert result == "i"

    def test_identifier_custom_prefix(self):
        ctx = SolidityContext(state_prefix="s")
        result = expr_to_solidity(Identifier("count"), ctx)
        assert result == "s.count"

    # --- ActorRef ---

    def test_actor_ref_simple(self):
        result = expr_to_solidity(ActorRef(name="DISPATCHER"))
        assert result == "actors_dispatcher"

    def test_actor_ref_wildcard(self):
        result = expr_to_solidity(ActorRef(name="ROBOT", index="*"))
        assert result == "actors_robot"

    def test_actor_ref_indexed(self):
        result = expr_to_solidity(ActorRef(name="ROBOT", index=3))
        assert result == "actors_robot[3]"

    def test_actor_ref_variable_index(self):
        ctx = SolidityContext(locals={"i"})
        result = expr_to_solidity(ActorRef(name="ROBOT", index="i"), ctx)
        assert result == "actors_robot[i]"

    # --- BinaryOp ---

    def test_binary_and(self):
        expr = BinaryOp("AND", Identifier("a"), Identifier("b"))
        result = expr_to_solidity(expr)
        assert "&&" in result
        assert "state.a" in result
        assert "state.b" in result

    def test_binary_or(self):
        expr = BinaryOp("OR", Identifier("a"), Identifier("b"))
        result = expr_to_solidity(expr)
        assert "||" in result

    def test_binary_comparison_ops(self):
        for op in ["==", "!=", "<", "<=", ">", ">="]:
            expr = BinaryOp(op, IntLiteral(5), IntLiteral(10))
            result = expr_to_solidity(expr)
            assert op in result

    def test_binary_arithmetic(self):
        for op in ["+", "-", "*", "/"]:
            expr = BinaryOp(op, IntLiteral(1), IntLiteral(2))
            result = expr_to_solidity(expr)
            assert op in result

    def test_binary_nested(self):
        inner = BinaryOp("AND", Identifier("x"), Identifier("y"))
        outer = BinaryOp("OR", inner, Identifier("z"))
        result = expr_to_solidity(outer)
        assert "&&" in result
        assert "||" in result

    # --- UnaryOp ---

    def test_unary_not(self):
        expr = UnaryOp("NOT", Identifier("x"))
        result = expr_to_solidity(expr)
        assert result == "(!state.x)"

    def test_unary_not_nested(self):
        inner = BinaryOp("AND", BoolLiteral(True), BoolLiteral(False))
        expr = UnaryOp("NOT", inner)
        result = expr_to_solidity(expr)
        assert "!" in result
        assert "&&" in result

    # --- MemberAccess ---

    def test_member_access(self):
        expr = MemberAccess(obj=ActorRef(name="DISPATCHER"), member="is_operational")
        result = expr_to_solidity(expr)
        assert result == "actors_dispatcher.is_operational"

    def test_member_access_lowercase(self):
        expr = MemberAccess(obj=ActorRef(name="ROBOT"), member="battery")
        result = expr_to_solidity(expr)
        assert "actors_robot.battery" == result

    # --- FunctionCall ---

    def test_function_call_no_args(self):
        expr = FunctionCall(name="all_routes_ok", args=[])
        result = expr_to_solidity(expr)
        assert result == "all_routes_ok()"

    def test_function_call_with_args(self):
        expr = FunctionCall(name="max", args=[IntLiteral(1), IntLiteral(2)])
        result = expr_to_solidity(expr)
        assert result == "max(1, 2)"

    def test_function_call_nested_args(self):
        inner = BinaryOp("+", IntLiteral(1), IntLiteral(2))
        expr = FunctionCall(name="abs", args=[inner])
        result = expr_to_solidity(expr)
        assert "abs(" in result
        assert "+" in result

    # --- QuantifiedExpr ---

    def test_quantified_for_all(self):
        expr = QuantifiedExpr(
            quantifier="for_all",
            variable="i",
            domain=ActorRef(name="ROBOT", index="*"),
            predicate=Identifier("i"),
        )
        result = expr_to_solidity(expr)
        assert result.startswith("true")
        assert "for_all" in result
        assert "i" in result

    def test_quantified_exists(self):
        expr = QuantifiedExpr(
            quantifier="exists",
            variable="i",
            domain=ActorRef(name="ROBOT", index="*"),
            predicate=Identifier("i"),
        )
        result = expr_to_solidity(expr)
        assert result.startswith("false")
        assert "exists" in result

    def test_quantified_variable_scoping(self):
        """Quantified variable should be treated as local inside predicate."""
        expr = QuantifiedExpr(
            quantifier="for_all",
            variable="r",
            domain=ActorRef(name="ROBOT", index="*"),
            predicate=BinaryOp(">", Identifier("r"), IntLiteral(0)),
        )
        ctx = SolidityContext()
        result = expr_to_solidity(expr, ctx)
        # 'r' should appear without state prefix inside the comment
        assert "for_all r" in result
        # After call, 'r' should NOT be in ctx.locals (scoping restored)
        assert "r" not in ctx.locals

    # --- Unsupported fallback ---

    def test_unsupported_expr_returns_comment(self):
        result = expr_to_solidity(RangeExpr(start=1, end=10))
        assert "unsupported" in result
        assert "RangeExpr" in result


# ===========================================================================
# TestSolidityGen
# ===========================================================================

class TestSolidityGen:
    """Tests for generate_contract_sol(), generate_main_sol(), generate_regime_sol()."""

    # --- generate_contract_sol ---

    def test_contract_sol_pragma(self):
        contract = _minimal_contract()
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "pragma solidity ^0.8.20;" in code

    def test_contract_sol_name(self):
        contract = _minimal_contract(cid="DELIVERY_SLA")
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "contract DeliverySla {" in code

    def test_contract_sol_spdx(self):
        contract = _minimal_contract()
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "SPDX-License-Identifier: Apache-2.0" in code

    def test_contract_sol_party_addresses(self):
        contract = _minimal_contract(
            parties=[ActorRef(name="DISPATCHER"), ActorRef(name="ROBOT", index="*")]
        )
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "address public dispatcher;" in code
        assert "address[] public robot;" in code

    def test_contract_sol_check_assumptions_empty(self):
        contract = _minimal_contract(assume=[])
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "function checkAssumptions()" in code
        assert "return true;" in code

    def test_contract_sol_check_assumptions(self):
        contract = _minimal_contract(assume=[BoolLiteral(True)])
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "function checkAssumptions()" in code
        assert "if (!(true)) return false;" in code

    def test_contract_sol_check_guarantees(self):
        contract = _minimal_contract(
            guarantee=[BinaryOp(">", IntLiteral(10), IntLiteral(5))]
        )
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "function checkGuarantees()" in code
        assert "if (!((10 > 5)))" in code

    def test_contract_sol_run_monitor_cycle(self):
        contract = _minimal_contract(
            assume=[BoolLiteral(True)],
            guarantee=[BoolLiteral(True)],
        )
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "function runMonitorCycle()" in code
        assert "checkAssumptions()" in code
        assert "checkGuarantees()" in code
        assert "ViolationDetected" in code

    def test_contract_sol_events(self):
        contract = _minimal_contract()
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "event AssumptionChecked" in code
        assert "event GuaranteeChecked" in code
        assert "event ViolationDetected" in code

    def test_contract_sol_state_variables(self):
        contract = _minimal_contract()
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "mapping(string => uint256) public stateUint;" in code
        assert "mapping(string => bool) public stateBool;" in code
        assert "uint256 public violationCount;" in code
        assert "bool public suspended;" in code

    # --- Institutional blocks ---

    def test_contract_sol_authority_beta(self):
        contract = _minimal_contract(
            authority=AuthorityBlock(beta=0.8)
        )
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "BETA = 800" in code
        assert "0.8" in code

    def test_contract_sol_information_alpha(self):
        contract = _minimal_contract(
            information=InformationBlock(alpha=0.6)
        )
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "ALPHA = 600" in code
        assert "0.6" in code

    def test_contract_sol_incentives_lambda(self):
        contract = _minimal_contract(
            incentives=IncentivesBlock(lambda_=0.3)
        )
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "LAMBDA = 300" in code
        assert "0.3" in code

    def test_contract_sol_incentive_rules(self):
        contract = _minimal_contract(
            incentives=IncentivesBlock(
                rules=[IncentiveRule(description="reward on time delivery")]
            )
        )
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "mapping(address => int256) public reputation;" in code
        assert "function reward(" in code
        assert "function penalize(" in code

    def test_contract_sol_violation_escalation(self):
        contract = _minimal_contract(
            assume=[BoolLiteral(True)],
            guarantee=[BoolLiteral(True)],
            violation=ViolationBlock(
                escalation="after 3 violations: suspend"
            ),
        )
        sos = _minimal_sos(contracts=[contract])
        code = generate_contract_sol(contract, sos)
        assert "violationCount >= 3" in code
        assert "suspended = true" in code

    # --- generate_regime_sol ---

    def test_regime_sol_basic(self):
        transitions = [
            TransitionDef(from_regime="normal", to_regime="degraded", condition="load > 0.9"),
            TransitionDef(from_regime="degraded", to_regime="normal", condition="load < 0.5"),
        ]
        sos = _minimal_sos(transitions=transitions)
        code = generate_regime_sol(sos)
        assert "pragma solidity ^0.8.20;" in code
        assert "contract RegimeController {" in code
        assert "enum Regime {" in code
        assert "evaluateTransitions" in code
        assert "getCurrentRegime" in code

    def test_regime_sol_regime_names(self):
        transitions = [
            TransitionDef(from_regime="normal", to_regime="emergency"),
        ]
        sos = _minimal_sos(transitions=transitions)
        code = generate_regime_sol(sos)
        # Both regime names should appear in the enum
        assert "Emergency" in code
        assert "Normal" in code

    def test_regime_sol_transition_without_condition(self):
        transitions = [
            TransitionDef(from_regime="normal", to_regime="degraded", condition=None),
        ]
        sos = _minimal_sos(transitions=transitions)
        code = generate_regime_sol(sos)
        assert "RegimeTransition" in code

    def test_regime_sol_events(self):
        transitions = [
            TransitionDef(from_regime="a", to_regime="b"),
        ]
        sos = _minimal_sos(transitions=transitions)
        code = generate_regime_sol(sos)
        assert "event RegimeTransition" in code

    # --- generate_main_sol ---

    def test_main_sol_orchestrator(self):
        contract = _minimal_contract(cid="MY_CONTRACT")
        sos = _minimal_sos(name="TestSystem", contracts=[contract])
        code = generate_main_sol(sos)
        assert "pragma solidity ^0.8.20;" in code
        assert "contract TestSystem {" in code

    def test_main_sol_imports_contracts(self):
        c1 = _minimal_contract(cid="ALPHA")
        c2 = _minimal_contract(cid="BETA")
        sos = _minimal_sos(contracts=[c1, c2])
        code = generate_main_sol(sos)
        assert 'import "./Alpha.sol";' in code
        assert 'import "./Beta.sol";' in code

    def test_main_sol_imports_regime_controller(self):
        contract = _minimal_contract()
        transitions = [TransitionDef(from_regime="a", to_regime="b")]
        sos = _minimal_sos(contracts=[contract], transitions=transitions)
        code = generate_main_sol(sos)
        assert 'import "./RegimeController.sol";' in code
        assert "RegimeController public regimeController;" in code

    def test_main_sol_no_regime_without_transitions(self):
        contract = _minimal_contract()
        sos = _minimal_sos(contracts=[contract])
        code = generate_main_sol(sos)
        assert "RegimeController" not in code

    def test_main_sol_constructor(self):
        contract = _minimal_contract(cid="SLA_ONE")
        sos = _minimal_sos(contracts=[contract])
        code = generate_main_sol(sos)
        assert "constructor()" in code
        assert "new SlaOne()" in code

    def test_main_sol_run_monitor_cycle(self):
        c1 = _minimal_contract(cid="C_ONE")
        c2 = _minimal_contract(cid="C_TWO")
        sos = _minimal_sos(contracts=[c1, c2])
        code = generate_main_sol(sos)
        assert "function runMonitorCycle()" in code
        assert "c_one.runMonitorCycle();" in code
        assert "c_two.runMonitorCycle();" in code

    def test_main_sol_run_monitor_with_transitions(self):
        contract = _minimal_contract()
        transitions = [TransitionDef(from_regime="x", to_regime="y")]
        sos = _minimal_sos(contracts=[contract], transitions=transitions)
        code = generate_main_sol(sos)
        assert "regimeController.evaluateTransitions();" in code


# ===========================================================================
# TestSolidityIntegration
# ===========================================================================

def _names(directory):
    """File names as written; Path.exists() ignores case on some systems."""
    return {p.name for p in directory.iterdir()}


class TestSolidityIntegration:
    """End-to-end tests using generate_solidity()."""

    def test_generate_solidity_minimal(self, tmp_path):
        contract = _minimal_contract(
            assume=[BoolLiteral(True)],
            guarantee=[BoolLiteral(True)],
        )
        sos = _minimal_sos(name="MinimalSoS", contracts=[contract])
        output = tmp_path / "sol"

        generate_solidity(sos, output)

        # Contract file
        assert (output / "TestSla.sol").exists()
        # Main orchestrator
        assert "MinimalSoS.sol" in _names(output)
        # No regime controller without transitions
        assert not (output / "RegimeController.sol").exists()

    def test_generate_solidity_with_transitions(self, tmp_path):
        contract = _minimal_contract()
        transitions = [
            TransitionDef(from_regime="normal", to_regime="degraded"),
        ]
        sos = _minimal_sos(
            name="TransSoS", contracts=[contract], transitions=transitions
        )
        output = tmp_path / "sol"

        generate_solidity(sos, output)

        assert (output / "TestSla.sol").exists()
        assert "TransSoS.sol" in _names(output)
        assert (output / "RegimeController.sol").exists()

    def test_generate_solidity_creates_output_dir(self, tmp_path):
        sos = _minimal_sos(name="DirTest")
        output = tmp_path / "nested" / "dir" / "sol"

        generate_solidity(sos, output)

        assert output.exists()
        assert "DirTest.sol" in _names(output)

    def test_generate_solidity_multiple_contracts(self, tmp_path):
        c1 = _minimal_contract(cid="ALPHA_SLA")
        c2 = _minimal_contract(cid="BETA_SLA")
        sos = _minimal_sos(name="MultiSoS", contracts=[c1, c2])
        output = tmp_path / "sol"

        generate_solidity(sos, output)

        assert (output / "AlphaSla.sol").exists()
        assert (output / "BetaSla.sol").exists()
        assert "MultiSoS.sol" in _names(output)

    def test_robot_delivery_solidity(self, tmp_path):
        """Parse robot_delivery.cadl and generate Solidity files."""
        from cadl.parser import parse_file

        cadl_file = Path(__file__).parent.parent / "examples" / "robot_delivery.cadl"
        if not cadl_file.exists():
            pytest.skip("robot_delivery.cadl not found")

        sos = parse_file(cadl_file)
        output = tmp_path / "robot_sol"

        generate_solidity(sos, output)

        # Should have at least the main orchestrator
        main_file = output / "RobotDeliverySystem.sol"
        assert main_file.name in _names(output), f"Expected main file; got: {list(output.iterdir())}"

        # All .sol files should contain valid Solidity pragma
        for f in output.glob("*.sol"):
            content = f.read_text()
            assert "pragma solidity" in content, f"{f.name} missing pragma"

    def test_supply_chain_solidity(self, tmp_path):
        """Parse supply_chain.cadl and generate Solidity files."""
        from cadl.parser import parse_file

        cadl_file = Path(__file__).parent.parent / "examples" / "supply_chain.cadl"
        if not cadl_file.exists():
            pytest.skip("supply_chain.cadl not found")

        sos = parse_file(cadl_file)
        output = tmp_path / "supply_sol"

        generate_solidity(sos, output)

        # Should produce .sol files
        sol_files = list(output.glob("*.sol"))
        assert len(sol_files) > 0, "No .sol files generated"

        for f in sol_files:
            content = f.read_text()
            assert "pragma solidity" in content, f"{f.name} missing pragma"

    def test_generated_contracts_contain_expected_structure(self, tmp_path):
        """Verify generated contract files have key Solidity constructs."""
        contract = _minimal_contract(
            cid="QUALITY_SLA",
            assume=[BinaryOp(">=", Identifier("score"), IntLiteral(80))],
            guarantee=[BinaryOp("==", Identifier("status"), BoolLiteral(True))],
            authority=AuthorityBlock(beta=0.7),
            information=InformationBlock(alpha=0.5),
            incentives=IncentivesBlock(
                lambda_=0.4,
                rules=[IncentiveRule(description="bonus on completion")],
            ),
            violation=ViolationBlock(escalation="after 5 violations: suspend"),
        )
        sos = _minimal_sos(name="FullSoS", contracts=[contract])
        output = tmp_path / "sol"

        generate_solidity(sos, output)

        code = (output / "QualitySla.sol").read_text()

        # Pragma and contract declaration
        assert "pragma solidity ^0.8.20;" in code
        assert "contract QualitySla {" in code

        # Institutional parameters
        assert "BETA = 700" in code
        assert "ALPHA = 500" in code
        assert "LAMBDA = 400" in code

        # Core functions
        assert "function checkAssumptions()" in code
        assert "function checkGuarantees()" in code
        assert "function runMonitorCycle()" in code

        # Violation escalation
        assert "violationCount >= 5" in code

        # Incentive functions
        assert "function reward(" in code
        assert "function penalize(" in code
