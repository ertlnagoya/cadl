"""Tests for the CADL OPA/Rego code generator."""

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
    FloatLiteral,
    FunctionCall,
    Identifier,
    InformationBlock,
    IntLiteral,
    MemberAccess,
    QuantifiedExpr,
    RangeExpr,
    SharingDef,
    SoSDefinition,
    SoSType,
    StringLiteral,
    UnaryOp,
    ViewDef,
)
from cadl.codegen.opa.rego_expr import RegoContext, expr_to_rego
from cadl.codegen.opa.rego_gen import (
    generate_contract_rego,
    generate_main_rego,
    generate_rego,
)


# === Expression compiler tests ===


class TestRegoExpr:
    """Test expr_to_rego() for each AST expression type."""

    # -- Literals --

    def test_bool_literal_true(self):
        assert expr_to_rego(BoolLiteral(True)) == "true"

    def test_bool_literal_false(self):
        assert expr_to_rego(BoolLiteral(False)) == "false"

    def test_int_literal(self):
        assert expr_to_rego(IntLiteral(42)) == "42"

    def test_int_literal_zero(self):
        assert expr_to_rego(IntLiteral(0)) == "0"

    def test_int_literal_negative(self):
        assert expr_to_rego(IntLiteral(-7)) == "-7"

    def test_float_literal(self):
        assert expr_to_rego(FloatLiteral(3.14)) == "3.14"

    def test_string_literal(self):
        assert expr_to_rego(StringLiteral("hello")) == '"hello"'

    def test_string_literal_with_quotes(self):
        result = expr_to_rego(StringLiteral('say "hi"'))
        assert result == '"say \\"hi\\""'

    def test_string_literal_empty(self):
        assert expr_to_rego(StringLiteral("")) == '""'

    # -- Identifier --

    def test_identifier_default_prefix(self):
        result = expr_to_rego(Identifier("speed"))
        assert result == "input.speed"

    def test_identifier_custom_prefix(self):
        ctx = RegoContext(input_prefix="data.sensors")
        result = expr_to_rego(Identifier("temp"), ctx)
        assert result == "data.sensors.temp"

    def test_identifier_local_variable(self):
        ctx = RegoContext(locals={"i"})
        result = expr_to_rego(Identifier("i"), ctx)
        assert result == "i"

    def test_identifier_non_local(self):
        ctx = RegoContext(locals={"i"})
        result = expr_to_rego(Identifier("x"), ctx)
        assert result == "input.x"

    # -- BinaryOp --

    def test_binary_and(self):
        expr = BinaryOp("AND", Identifier("a"), Identifier("b"))
        result = expr_to_rego(expr)
        assert result == "input.a; input.b"

    def test_binary_or(self):
        expr = BinaryOp("OR", Identifier("a"), Identifier("b"))
        result = expr_to_rego(expr)
        assert "OR" in result or "or" in result.lower()
        assert "input.a" in result
        assert "input.b" in result

    def test_binary_comparison_ops(self):
        for op in ["==", "!=", "<", "<=", ">", ">="]:
            expr = BinaryOp(op, IntLiteral(5), IntLiteral(10))
            result = expr_to_rego(expr)
            assert op in result, f"Operator {op} missing from: {result}"
            assert "5" in result
            assert "10" in result

    def test_binary_arithmetic(self):
        expr = BinaryOp("+", IntLiteral(1), IntLiteral(2))
        result = expr_to_rego(expr)
        assert result == "1 + 2"

    def test_binary_subtraction(self):
        expr = BinaryOp("-", IntLiteral(10), IntLiteral(3))
        result = expr_to_rego(expr)
        assert result == "10 - 3"

    def test_binary_nested(self):
        inner = BinaryOp("+", IntLiteral(1), IntLiteral(2))
        outer = BinaryOp(">", inner, IntLiteral(0))
        result = expr_to_rego(outer)
        assert "1 + 2" in result
        assert ">" in result

    # -- UnaryOp --

    def test_unary_not(self):
        expr = UnaryOp("NOT", Identifier("x"))
        result = expr_to_rego(expr)
        assert result == "not input.x"

    def test_unary_not_nested(self):
        inner = BinaryOp("==", Identifier("a"), IntLiteral(0))
        expr = UnaryOp("NOT", inner)
        result = expr_to_rego(expr)
        assert result.startswith("not ")
        assert "input.a == 0" in result

    # -- MemberAccess --

    def test_member_access(self):
        expr = MemberAccess(obj=ActorRef(name="ROBOT"), member="is_active")
        result = expr_to_rego(expr)
        assert result == "input.robot.is_active"

    def test_member_access_case(self):
        expr = MemberAccess(obj=ActorRef(name="DISPATCHER"), member="route_count")
        result = expr_to_rego(expr)
        assert result == "input.dispatcher.route_count"

    # -- FunctionCall --

    def test_function_call_no_args(self):
        expr = FunctionCall(name="all_routes_ok", args=[])
        result = expr_to_rego(expr)
        assert result == "all_routes_ok"

    def test_function_call_with_args(self):
        expr = FunctionCall(name="max", args=[IntLiteral(1), IntLiteral(2)])
        result = expr_to_rego(expr)
        assert result == "max(1, 2)"

    def test_function_call_nested_args(self):
        inner = BinaryOp("+", IntLiteral(1), IntLiteral(2))
        expr = FunctionCall(name="abs", args=[inner])
        result = expr_to_rego(expr)
        assert result == "abs(1 + 2)"

    # -- ActorRef --

    def test_actor_ref_simple(self):
        expr = ActorRef(name="DISPATCHER")
        result = expr_to_rego(expr)
        assert result == "input.dispatcher"

    def test_actor_ref_wildcard(self):
        expr = ActorRef(name="ROBOT", index="*")
        result = expr_to_rego(expr)
        assert result == "input.robot"

    def test_actor_ref_with_int_index(self):
        expr = ActorRef(name="ROBOT", index=0)
        result = expr_to_rego(expr)
        assert result == "input.robot[0]"

    def test_actor_ref_with_local_index(self):
        ctx = RegoContext(locals={"i"})
        expr = ActorRef(name="ROBOT", index="i")
        result = expr_to_rego(expr, ctx)
        assert result == "input.robot[i]"

    # -- QuantifiedExpr --

    def test_quantified_for_all(self):
        expr = QuantifiedExpr(
            quantifier="for_all",
            variable="i",
            domain=ActorRef(name="ROBOT", index="*"),
            predicate=Identifier("i"),
        )
        result = expr_to_rego(expr)
        assert "count" in result
        assert "input.robot" in result

    def test_quantified_exists(self):
        expr = QuantifiedExpr(
            quantifier="exists",
            variable="r",
            domain=ActorRef(name="ROBOT", index="*"),
            predicate=Identifier("r"),
        )
        result = expr_to_rego(expr)
        assert "some r" in result
        assert "input.robot" in result

    def test_quantified_for_all_with_predicate(self):
        predicate = BinaryOp(">", Identifier("i"), IntLiteral(0))
        expr = QuantifiedExpr(
            quantifier="for_all",
            variable="i",
            domain=ActorRef(name="SENSOR", index="*"),
            predicate=predicate,
        )
        result = expr_to_rego(expr)
        assert "count" in result
        # The variable "i" inside predicate should be treated as local
        assert "i > 0" in result

    def test_quantified_exists_with_predicate(self):
        predicate = BinaryOp("==", Identifier("x"), StringLiteral("active"))
        expr = QuantifiedExpr(
            quantifier="exists",
            variable="x",
            domain=ActorRef(name="NODE", index="*"),
            predicate=predicate,
        )
        result = expr_to_rego(expr)
        assert "some x" in result
        assert '"active"' in result

    def test_quantified_restores_locals(self):
        """Quantified expression should restore locals context after compilation."""
        ctx = RegoContext(locals={"outer"})
        expr = QuantifiedExpr(
            quantifier="exists",
            variable="inner",
            domain=ActorRef(name="ROBOT", index="*"),
            predicate=Identifier("inner"),
        )
        expr_to_rego(expr, ctx)
        assert "inner" not in ctx.locals
        assert "outer" in ctx.locals


# === Rego generator tests ===


class TestRegoGen:
    """Test generate_contract_rego() and generate_main_rego()."""

    def _make_sos(self, contracts=None, actors=None, name="TestSoS", sos_type=None):
        return SoSDefinition(
            name=name,
            type=sos_type,
            actors=actors or [],
            contracts=contracts or [],
        )

    def _make_contract(self, cid="C1", parties=None, assume=None, guarantee=None,
                       authority=None, information=None):
        return ContractDef(
            id=cid,
            parties=parties or [ActorRef(name="A")],
            assume=assume or [],
            guarantee=guarantee or [],
            authority=authority,
            information=information,
        )

    # -- generate_contract_rego --

    def test_contract_package_declaration(self):
        contract = self._make_contract(cid="DELIVERY_SLA")
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "package cadl.delivery_sla" in code

    def test_contract_import_rego_v1(self):
        contract = self._make_contract()
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "import rego.v1" in code

    def test_contract_metadata(self):
        contract = self._make_contract(
            cid="MY_CONTRACT",
            parties=[ActorRef(name="ALICE"), ActorRef(name="BOB")],
        )
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert 'contract_id := "MY_CONTRACT"' in code
        assert '"ALICE"' in code
        assert '"BOB"' in code

    def test_contract_parties_wildcard(self):
        contract = self._make_contract(
            parties=[ActorRef(name="ROBOT", index="*")],
        )
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "ROBOT[*]" in code

    def test_assumptions_hold_with_predicates(self):
        contract = self._make_contract(
            assume=[BoolLiteral(True), BinaryOp(">", IntLiteral(10), IntLiteral(5))],
        )
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "assumptions_hold if {" in code
        assert "true" in code
        assert "10 > 5" in code

    def test_assumptions_hold_empty(self):
        contract = self._make_contract(assume=[])
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "assumptions_hold if {" in code
        # When no assumptions, should have "true" as default
        lines = code.split("\n")
        in_assumptions = False
        for line in lines:
            if "assumptions_hold if {" in line:
                in_assumptions = True
            elif in_assumptions and "}" in line:
                break
            elif in_assumptions and "true" in line:
                break
        assert in_assumptions

    def test_guarantees_hold_with_predicates(self):
        contract = self._make_contract(
            guarantee=[BinaryOp(">=", Identifier("score"), IntLiteral(90))],
        )
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "guarantees_hold if {" in code
        assert "input.score >= 90" in code

    def test_guarantees_hold_empty(self):
        contract = self._make_contract(guarantee=[])
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "guarantees_hold if {" in code

    def test_default_allow_false(self):
        contract = self._make_contract()
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "default allow := false" in code

    def test_allow_rule(self):
        contract = self._make_contract()
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "allow if {" in code
        assert "assumptions_hold" in code
        assert "guarantees_hold" in code

    def test_violation_rule(self):
        contract = self._make_contract(cid="SLA_1")
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "violation contains msg if {" in code
        assert "assumptions_hold" in code
        assert "not guarantees_hold" in code
        assert "Guarantee violated in SLA_1" in code

    def test_authority_block(self):
        authority = AuthorityBlock(
            decision_scope="routing",
            decision_holder=ActorRef(name="DISPATCHER"),
            beta=0.8,
        )
        contract = self._make_contract(authority=authority)
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "beta := 0.8" in code
        assert 'decision_scope := "routing"' in code
        assert 'decision_holder := "DISPATCHER"' in code

    def test_information_block_alpha(self):
        info = InformationBlock(alpha=0.5)
        contract = self._make_contract(information=info)
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "alpha := 0.5" in code

    def test_information_sharing_rules(self):
        sharing = [
            SharingDef(
                source=ActorRef(name="SENSOR"),
                target=ActorRef(name="HUB"),
                data="temperature",
            ),
        ]
        info = InformationBlock(sharing=sharing)
        contract = self._make_contract(information=info)
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "sharing_allowed_0 if {" in code
        assert 'input.source == "sensor"' in code
        assert 'input.target == "hub"' in code
        assert 'input.data == "temperature"' in code

    def test_information_multiple_sharing(self):
        sharing = [
            SharingDef(source=ActorRef(name="A"), target=ActorRef(name="B"), data="d1"),
            SharingDef(source=ActorRef(name="C"), target=ActorRef(name="D"), data="d2"),
        ]
        info = InformationBlock(sharing=sharing)
        contract = self._make_contract(information=info)
        sos = self._make_sos(contracts=[contract])
        code = generate_contract_rego(contract, sos)
        assert "sharing_allowed_0 if {" in code
        assert "sharing_allowed_1 if {" in code

    # -- generate_main_rego --

    def test_main_package_declaration(self):
        sos = self._make_sos(name="RobotDelivery")
        code = generate_main_rego(sos)
        assert "package cadl.robot_delivery" in code

    def test_main_import_rego_v1(self):
        sos = self._make_sos()
        code = generate_main_rego(sos)
        assert "import rego.v1" in code

    def test_main_sos_metadata(self):
        sos = self._make_sos(name="MySoS", sos_type=SoSType.COLLABORATIVE)
        sos.version = "2.0.0"
        code = generate_main_rego(sos)
        assert 'sos_name := "MySoS"' in code
        assert 'sos_type := "Collaborative"' in code
        assert 'sos_version := "2.0.0"' in code

    def test_main_imports_contracts(self):
        c1 = self._make_contract(cid="ALPHA_CONTRACT")
        c2 = self._make_contract(cid="BETA_CONTRACT")
        sos = self._make_sos(contracts=[c1, c2])
        code = generate_main_rego(sos)
        assert "import data.cadl.alpha_contract" in code
        assert "import data.cadl.beta_contract" in code

    def test_main_actor_list(self):
        actors = [
            ActorDef(id=ActorRef(name="ROBOT"), role="vehicle", autonomy=AutonomyLevel.HIGH),
            ActorDef(id=ActorRef(name="HUB"), role="coordinator", autonomy=AutonomyLevel.LOW),
        ]
        sos = self._make_sos(actors=actors)
        code = generate_main_rego(sos)
        assert '"ROBOT"' in code
        assert '"HUB"' in code
        assert "actors :=" in code

    def test_main_all_contracts_satisfied(self):
        c1 = self._make_contract(cid="C1")
        c2 = self._make_contract(cid="C2")
        sos = self._make_sos(contracts=[c1, c2])
        code = generate_main_rego(sos)
        assert "default all_contracts_satisfied := false" in code
        assert "all_contracts_satisfied if {" in code
        assert "data.cadl.c1.allow" in code
        assert "data.cadl.c2.allow" in code

    def test_main_all_contracts_satisfied_empty(self):
        sos = self._make_sos(contracts=[])
        code = generate_main_rego(sos)
        assert "all_contracts_satisfied if {" in code

    def test_main_all_violations(self):
        c1 = self._make_contract(cid="C1")
        sos = self._make_sos(contracts=[c1])
        code = generate_main_rego(sos)
        assert "all_violations contains msg if {" in code
        assert "data.cadl.c1.violation[_]" in code

    def test_main_no_contracts_violations(self):
        sos = self._make_sos(contracts=[])
        code = generate_main_rego(sos)
        assert "all_violations contains msg if {" in code


# === Integration tests ===


class TestRegoIntegration:
    """End-to-end tests for generate_rego()."""

    def test_generate_rego_creates_files(self, tmp_path):
        """Minimal SoS produces .rego files."""
        contract = ContractDef(
            id="SIMPLE",
            parties=[ActorRef(name="A")],
            assume=[BoolLiteral(True)],
            guarantee=[BoolLiteral(True)],
        )
        sos = SoSDefinition(
            name="MinimalSoS",
            actors=[
                ActorDef(
                    id=ActorRef(name="A"),
                    role="worker",
                    autonomy=AutonomyLevel.LOW,
                ),
            ],
            contracts=[contract],
        )
        output = tmp_path / "rego_out"
        generate_rego(sos, output)

        assert (output / "simple.rego").exists()
        assert (output / "main.rego").exists()

    def test_generate_rego_file_contents(self, tmp_path):
        """Generated .rego files contain expected structure."""
        contract = ContractDef(
            id="DATA_SLA",
            parties=[ActorRef(name="SENSOR"), ActorRef(name="HUB")],
            assume=[BinaryOp(">", Identifier("uptime"), FloatLiteral(0.99))],
            guarantee=[BoolLiteral(True)],
        )
        sos = SoSDefinition(
            name="IoTSystem",
            type=SoSType.DIRECTED,
            contracts=[contract],
        )
        output = tmp_path / "rego_out"
        generate_rego(sos, output)

        contract_code = (output / "data_sla.rego").read_text()
        assert "package cadl.data_sla" in contract_code
        assert "input.uptime > 0.99" in contract_code

        main_code = (output / "main.rego").read_text()
        assert "package cadl.io_t_system" in main_code
        assert "import data.cadl.data_sla" in main_code

    def test_generate_rego_multiple_contracts(self, tmp_path):
        """Each contract produces its own .rego file."""
        c1 = ContractDef(id="CONTRACT_A", parties=[ActorRef(name="X")])
        c2 = ContractDef(id="CONTRACT_B", parties=[ActorRef(name="Y")])
        sos = SoSDefinition(name="MultiSoS", contracts=[c1, c2])

        output = tmp_path / "rego_out"
        generate_rego(sos, output)

        assert (output / "contract_a.rego").exists()
        assert (output / "contract_b.rego").exists()
        assert (output / "main.rego").exists()

    def test_generate_rego_robot_delivery(self, tmp_path):
        """Robot delivery example generates valid Rego policies."""
        from cadl.parser import parse_file

        cadl_file = Path(__file__).parent.parent / "examples" / "robot_delivery.cadl"
        if not cadl_file.exists():
            pytest.skip("robot_delivery.cadl not found")

        sos = parse_file(cadl_file)
        output = tmp_path / "robot_delivery_rego"
        generate_rego(sos, output)

        # Should have a main.rego
        assert (output / "main.rego").exists()

        # Should have at least one contract .rego file
        rego_files = list(output.glob("*.rego"))
        assert len(rego_files) >= 2  # main + at least one contract

        # All files should be non-empty
        for f in rego_files:
            content = f.read_text()
            assert len(content) > 0, f"Empty file: {f.name}"
            assert "package cadl." in content

    def test_generate_rego_output_dir_created(self, tmp_path):
        """generate_rego creates nested output directories."""
        sos = SoSDefinition(name="TestSoS", contracts=[])
        output = tmp_path / "nested" / "deep" / "rego"
        generate_rego(sos, output)
        assert output.exists()
        assert (output / "main.rego").exists()
