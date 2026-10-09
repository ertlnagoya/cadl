"""Tests for the CADL SMT verifier."""

from __future__ import annotations

import pytest

from cadl.ast_nodes import (
    ActorRef,
    AuthorityBlock,
    BinaryOp,
    BoolLiteral,
    ContractDef,
    FloatLiteral,
    FunctionCall,
    Identifier,
    InformationBlock,
    IntLiteral,
    MemberAccess,
    SoSDefinition,
    StringLiteral,
    TransitionDef,
    UnaryOp,
)
from cadl.verifier import (
    VerificationResult,
    Z3Context,
    expr_to_z3,
    verify,
    _check_contract_consistency,
    _check_cross_contract_consistency,
    _check_assumption_satisfiability,
    _check_guarantee_entailment,
    _check_transition_exclusivity,
)


# === Helper to build minimal contracts ===

def _make_contract(
    id: str,
    parties: list[str],
    assume: list = None,
    guarantee: list = None,
) -> ContractDef:
    return ContractDef(
        id=id,
        parties=[ActorRef(name=p) for p in parties],
        assume=assume or [],
        guarantee=guarantee or [],
    )


def _make_sos(
    contracts: list[ContractDef] = None,
    transitions: list[TransitionDef] = None,
) -> SoSDefinition:
    return SoSDefinition(
        name="TestSoS",
        contracts=contracts or [],
        transitions=transitions or [],
    )


# === expr_to_z3 tests ===

class TestExprToZ3:
    """Test AST-to-Z3 translation."""

    def test_bool_literal_true(self):
        import z3
        ctx = Z3Context()
        result = expr_to_z3(BoolLiteral(True), ctx)
        assert z3.is_true(result)

    def test_bool_literal_false(self):
        import z3
        ctx = Z3Context()
        result = expr_to_z3(BoolLiteral(False), ctx)
        assert z3.is_false(result)

    def test_int_literal(self):
        import z3
        ctx = Z3Context()
        result = expr_to_z3(IntLiteral(42), ctx)
        assert result.as_long() == 42

    def test_float_literal(self):
        import z3
        ctx = Z3Context()
        result = expr_to_z3(FloatLiteral(3.14), ctx)
        assert z3.is_rational_value(result)

    def test_identifier(self):
        import z3
        ctx = Z3Context()
        result = expr_to_z3(Identifier("x"), ctx)
        assert z3.is_bool(result)
        assert str(result) == "x"

    def test_binary_and(self):
        import z3
        ctx = Z3Context()
        expr = BinaryOp("AND", Identifier("a"), Identifier("b"))
        result = expr_to_z3(expr, ctx)
        assert z3.is_and(result)

    def test_binary_or(self):
        import z3
        ctx = Z3Context()
        expr = BinaryOp("OR", Identifier("a"), Identifier("b"))
        result = expr_to_z3(expr, ctx)
        assert z3.is_or(result)

    def test_unary_not(self):
        import z3
        ctx = Z3Context()
        expr = UnaryOp("NOT", Identifier("a"))
        result = expr_to_z3(expr, ctx)
        assert z3.is_not(result)

    def test_comparison_operators(self):
        import z3
        ctx = Z3Context()
        for op in ["==", "!=", "<", "<=", ">", ">="]:
            expr = BinaryOp(op, IntLiteral(5), IntLiteral(10))
            result = expr_to_z3(expr, ctx)
            assert result is not None

    def test_member_access(self):
        import z3
        ctx = Z3Context()
        expr = MemberAccess(obj=ActorRef(name="DISPATCHER"), member="is_operational")
        result = expr_to_z3(expr, ctx)
        assert z3.is_bool(result)
        assert str(result) == "DISPATCHER_is_operational"

    def test_function_call_no_args(self):
        import z3
        ctx = Z3Context()
        expr = FunctionCall(name="all_routes_ok", args=[])
        result = expr_to_z3(expr, ctx)
        assert z3.is_bool(result)

    def test_string_literal_as_predicate(self):
        import z3
        ctx = Z3Context()
        expr = StringLiteral("some_predicate")
        result = expr_to_z3(expr, ctx)
        assert z3.is_bool(result)


# === Contract consistency tests ===

class TestContractConsistency:
    """Test single contract consistency checks."""

    def test_satisfiable_contract(self):
        """A contract with compatible assume + guarantee should pass."""
        contract = _make_contract(
            "C1", ["A", "B"],
            assume=[BoolLiteral(True)],
            guarantee=[BoolLiteral(True)],
        )
        result = _check_contract_consistency(contract)
        assert result.status == "passed"

    def test_contradictory_contract(self):
        """A contract with contradictory constraints should fail."""
        # x > 10 AND x < 5 is unsatisfiable
        contract = _make_contract(
            "C1", ["A", "B"],
            assume=[BinaryOp(">", Identifier("x"), IntLiteral(10))],
            guarantee=[BinaryOp("<", Identifier("x"), IntLiteral(5))],
        )
        result = _check_contract_consistency(contract)
        assert result.status == "failed"

    def test_empty_contract(self):
        """A contract with no constraints is trivially satisfiable."""
        contract = _make_contract("C1", ["A", "B"])
        result = _check_contract_consistency(contract)
        assert result.status == "passed"

    def test_single_assume(self):
        """A contract with only assumes should be satisfiable."""
        contract = _make_contract(
            "C1", ["A"],
            assume=[BinaryOp(">", IntLiteral(10), IntLiteral(5))],
        )
        result = _check_contract_consistency(contract)
        assert result.status == "passed"


# === Cross-contract consistency tests ===

class TestCrossContractConsistency:
    """Test cross-contract consistency checks."""

    def test_compatible_contracts(self):
        """Two contracts with compatible constraints should pass."""
        c1 = _make_contract(
            "C1", ["A", "B"],
            guarantee=[BinaryOp(">", Identifier("x"), IntLiteral(0))],
        )
        c2 = _make_contract(
            "C2", ["A", "C"],
            guarantee=[BinaryOp("<", Identifier("x"), IntLiteral(100))],
        )
        result = _check_cross_contract_consistency(c1, c2)
        assert result.status == "passed"

    def test_contradictory_contracts(self):
        """Two contracts with contradictory constraints should fail."""
        c1 = _make_contract(
            "C1", ["A", "B"],
            guarantee=[BinaryOp("==", Identifier("x"), IntLiteral(5))],
        )
        c2 = _make_contract(
            "C2", ["A", "C"],
            guarantee=[BinaryOp("==", Identifier("x"), IntLiteral(10))],
        )
        result = _check_cross_contract_consistency(c1, c2)
        assert result.status == "failed"


# === Full verify() tests ===

class TestVerify:
    """Test the top-level verify function."""

    def test_empty_sos(self):
        """An SoS with no contracts produces no results."""
        sos = _make_sos()
        results = verify(sos)
        assert results == []

    def test_single_consistent_contract(self):
        """Single satisfiable contract produces consistency, assumption and entailment results."""
        c = _make_contract("C1", ["A"], guarantee=[BoolLiteral(True)])
        sos = _make_sos(contracts=[c])
        results = verify(sos)
        # 1 consistency + 1 assumptions + 1 entailment
        assert len(results) == 3
        assert all(r.status == "passed" for r in results)

    def test_cross_contract_with_shared_parties(self):
        """Cross-contract check runs for contracts sharing parties."""
        c1 = _make_contract("C1", ["A", "B"], guarantee=[BoolLiteral(True)])
        c2 = _make_contract("C2", ["A", "C"], guarantee=[BoolLiteral(True)])
        sos = _make_sos(contracts=[c1, c2])
        results = verify(sos)
        # 2 consistency + 2 assumptions + 2 entailment + 1 cross-contract (A is shared)
        assert len(results) == 7
        assert all(r.status == "passed" for r in results)

    def test_no_cross_check_without_shared_parties(self):
        """No cross-contract check when parties don't overlap."""
        c1 = _make_contract("C1", ["A", "B"], guarantee=[BoolLiteral(True)])
        c2 = _make_contract("C2", ["C", "D"], guarantee=[BoolLiteral(True)])
        sos = _make_sos(contracts=[c1, c2])
        results = verify(sos)
        # 2 consistency + 2 assumptions + 2 entailment, no cross-contract
        assert len(results) == 6

    def test_verification_result_str(self):
        """VerificationResult __str__ formatting."""
        r = VerificationResult(
            check_name="Test check",
            status="passed",
            message="All good",
        )
        assert "[PASS]" in str(r)
        assert "Test check" in str(r)

    def test_verification_result_with_counterexample(self):
        """VerificationResult with counterexample includes it in string."""
        r = VerificationResult(
            check_name="Test",
            status="failed",
            message="Bad",
            counterexample={"x": "5"},
        )
        s = str(r)
        assert "Counterexample" in s
        assert "x" in s


# === Robot delivery integration test ===

class TestRobotDeliveryVerification:
    """Integration test: verify the robot delivery example."""

    def test_robot_delivery_parse_and_verify(self):
        """The robot delivery example should parse and verify without failures."""
        from cadl.parser import parse_file
        from pathlib import Path

        cadl_file = Path(__file__).parent.parent / "examples" / "robot_delivery.cadl"
        if not cadl_file.exists():
            pytest.skip("robot_delivery.cadl not found")

        sos = parse_file(cadl_file)
        results = verify(sos)

        # Guarantees are obligations, not consequences of the assumes, so
        # entailment is reported as "info" and nothing may fail.
        for r in results:
            assert r.status != "failed", f"Unexpected failure: {r}"


# === Guarantee entailment tests ===

class TestGuaranteeEntailment:
    """Test guarantee entailment checking (assume => guarantee)."""

    def test_entailment_passes_when_assume_implies_guarantee(self):
        """If assume x > 10, then guarantee x > 5 should be entailed."""
        contract = _make_contract(
            "C1", ["A"],
            assume=[BinaryOp(">", Identifier("x"), IntLiteral(10))],
            guarantee=[BinaryOp(">", Identifier("x"), IntLiteral(5))],
        )
        result = _check_guarantee_entailment(contract)
        assert result.status == "passed"

    def test_entailment_is_informational_when_not_implied(self):
        """If assume x > 0, guarantee x > 100 is NOT entailed: info, not failure."""
        contract = _make_contract(
            "C1", ["A"],
            assume=[BinaryOp(">", Identifier("x"), IntLiteral(0))],
            guarantee=[BinaryOp(">", Identifier("x"), IntLiteral(100))],
        )
        result = _check_guarantee_entailment(contract)
        assert result.status == "info"
        assert result.counterexample is not None

    def test_entailment_empty_assumes(self):
        """No assumes means entailment check passes trivially."""
        contract = _make_contract(
            "C1", ["A"],
            assume=[],
            guarantee=[BoolLiteral(True)],
        )
        result = _check_guarantee_entailment(contract)
        assert result.status == "passed"

    def test_entailment_empty_guarantees(self):
        """No guarantees means entailment check passes trivially."""
        contract = _make_contract(
            "C1", ["A"],
            assume=[BoolLiteral(True)],
            guarantee=[],
        )
        result = _check_guarantee_entailment(contract)
        assert result.status == "passed"

    def test_entailment_with_multiple_assumes(self):
        """Multiple assumes combined should entail the guarantee."""
        contract = _make_contract(
            "C1", ["A"],
            assume=[
                BinaryOp(">", Identifier("x"), IntLiteral(5)),
                BinaryOp("<", Identifier("x"), IntLiteral(15)),
            ],
            guarantee=[BinaryOp("<", Identifier("x"), IntLiteral(20))],
        )
        result = _check_guarantee_entailment(contract)
        assert result.status == "passed"

    def test_entailment_trivially_true_guarantee(self):
        """Guarantee of 'true' is always entailed."""
        contract = _make_contract(
            "C1", ["A"],
            assume=[BinaryOp(">", Identifier("x"), IntLiteral(0))],
            guarantee=[BoolLiteral(True)],
        )
        result = _check_guarantee_entailment(contract)
        assert result.status == "passed"


# === Assumption satisfiability tests ===

class TestAssumptionSatisfiability:
    """A contract whose assumes cannot hold together never applies."""

    def test_satisfiable_assumptions(self):
        contract = _make_contract(
            "C1", ["A"],
            assume=[BinaryOp(">", Identifier("x"), IntLiteral(0))],
        )
        assert _check_assumption_satisfiability(contract).status == "passed"

    def test_contradictory_assumptions(self):
        contract = _make_contract(
            "C1", ["A"],
            assume=[
                BinaryOp(">", Identifier("x"), IntLiteral(10)),
                BinaryOp("<", Identifier("x"), IntLiteral(5)),
            ],
        )
        assert _check_assumption_satisfiability(contract).status == "failed"

    def test_no_assumptions(self):
        contract = _make_contract("C1", ["A"], guarantee=[BoolLiteral(True)])
        assert _check_assumption_satisfiability(contract).status == "passed"


# === Numeric encoding tests ===

class TestNumericEncoding:
    """Names used in comparisons are quantities, not 0/1 truth values."""

    def _sat(self, *exprs):
        import z3
        from cadl.parser import parse_expr
        from cadl.verifier import _to_z3_bool
        ctx = Z3Context()
        solver = z3.Solver()
        for e in exprs:
            solver.add(_to_z3_bool(parse_expr(e), ctx))
        return solver.check() == z3.sat

    def test_threshold_above_one_is_satisfiable(self):
        assert self._sat("battery > 20")

    def test_contradictory_bounds_are_unsatisfiable(self):
        assert not self._sat("battery > 20", "battery < 10")

    def test_arithmetic_on_both_sides(self):
        assert self._sat("active_tasks > capacity * 0.8")
        assert not self._sat(
            "active_tasks > capacity * 0.8", "active_tasks <= capacity * 0.5",
            "capacity > 0",
        )

    def test_boolean_equality(self):
        assert not self._sat("emergency_active == true", "emergency_active == false")

    def test_same_call_is_same_symbol(self):
        assert not self._sat("count(late) > 3", "count(late) < 2")

    def test_distinct_strings_differ(self):
        assert not self._sat('mode == "auto"', 'mode == "manual"')

    def test_conjunction_and_negation(self):
        assert not self._sat("a AND NOT a")
        assert self._sat("a OR NOT a")


# === Transition exclusivity tests ===

class TestTransitionExclusivity:

    def _results(self, *conds):
        transitions = [
            TransitionDef(from_regime="S", to_regime=f"T{i}", condition=c)
            for i, c in enumerate(conds)
        ]
        return _check_transition_exclusivity(_make_sos(transitions=transitions))

    def test_disjoint_thresholds_are_exclusive(self):
        (r,) = self._results("level > 0.7", "level <= 0.4")
        assert r.status == "passed"

    def test_independent_conditions_overlap(self):
        (r,) = self._results("level > 0.7", "emergency_active == true")
        assert r.status == "failed"
        assert r.counterexample is not None

    def test_explicit_priority_makes_them_exclusive(self):
        (r,) = self._results(
            "level > 0.7 AND emergency_active == false",
            "emergency_active == true",
        )
        assert r.status == "passed"

    def test_conjunctions_with_complementary_bounds(self):
        (r,) = self._results(
            "emergency_active == false AND level <= 0.4",
            "emergency_active == false AND level > 0.4",
        )
        assert r.status == "passed"
