"""Tests for the CADL code generator."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cadl.ast_nodes import (
    ActorDef,
    ActorRef,
    AuthorityBlock,
    AutonomyLevel,
    BinaryOp,
    BoolLiteral,
    ComputeStep,
    ContractDef,
    DurationLiteral,
    FloatLiteral,
    FunctionCall,
    Identifier,
    IntLiteral,
    MemberAccess,
    MessageStep,
    MetricDef,
    ProtocolDef,
    QuantifiedExpr,
    RangeExpr,
    SoSDefinition,
    StringLiteral,
    TransitionDef,
    UnaryOp,
)
from cadl.codegen.emitter import sanitize_id, snake_case
from cadl.codegen.expr_compiler import CompilerContext, expr_to_python
from cadl.codegen.actor_gen import generate_actor_class, generate_actors_module
from cadl.codegen.contract_gen import generate_contract_monitor
from cadl.codegen.protocol_gen import generate_protocol_class
from cadl.codegen import generate


# === Emitter tests ===

class TestEmitter:
    def test_sanitize_id(self):
        assert sanitize_id("DELIVERY_SLA") == "DeliverySla"
        assert sanitize_id("ROBOT") == "Robot"
        assert sanitize_id("FAILURE_REPLAN") == "FailureReplan"

    def test_snake_case(self):
        assert snake_case("DELIVERY_SLA") == "delivery_sla"
        assert snake_case("ROBOT") == "robot"


# === Expression compiler tests ===

class TestExprCompiler:
    def test_bool_literal(self):
        assert expr_to_python(BoolLiteral(True)) == "True"
        assert expr_to_python(BoolLiteral(False)) == "False"

    def test_int_literal(self):
        assert expr_to_python(IntLiteral(42)) == "42"

    def test_float_literal(self):
        assert expr_to_python(FloatLiteral(3.14)) == "3.14"

    def test_string_literal(self):
        result = expr_to_python(StringLiteral("hello"))
        assert result == "'hello'"

    def test_duration_literal(self):
        result = expr_to_python(DurationLiteral(200, "ms"))
        assert "timedelta" in result
        assert "200" in result

    def test_identifier(self):
        result = expr_to_python(Identifier("x"))
        assert "x" in result

    def test_identifier_local(self):
        ctx = CompilerContext(locals={"i"})
        result = expr_to_python(Identifier("i"), ctx)
        assert result == "i"

    def test_member_access(self):
        expr = MemberAccess(obj=ActorRef(name="DISPATCHER"), member="is_operational")
        result = expr_to_python(expr)
        assert "DISPATCHER" in result
        assert "is_operational" in result

    def test_function_call(self):
        expr = FunctionCall(name="all_routes_ok", args=[])
        result = expr_to_python(expr)
        assert "all_routes_ok" in result

    def test_binary_and(self):
        expr = BinaryOp("AND", Identifier("a"), Identifier("b"))
        result = expr_to_python(expr)
        assert "and" in result

    def test_binary_or(self):
        expr = BinaryOp("OR", Identifier("a"), Identifier("b"))
        result = expr_to_python(expr)
        assert "or" in result

    def test_binary_comparison(self):
        for op in ["==", "!=", "<", "<=", ">", ">="]:
            expr = BinaryOp(op, IntLiteral(5), IntLiteral(10))
            result = expr_to_python(expr)
            assert op in result

    def test_binary_arithmetic(self):
        expr = BinaryOp("+", IntLiteral(1), IntLiteral(2))
        result = expr_to_python(expr)
        assert "+" in result

    def test_unary_not(self):
        expr = UnaryOp("NOT", Identifier("x"))
        result = expr_to_python(expr)
        assert "not" in result

    def test_quantified_for_all(self):
        expr = QuantifiedExpr(
            quantifier="for_all",
            variable="i",
            domain=ActorRef(name="ROBOT", index="*"),
            predicate=Identifier("i"),
        )
        result = expr_to_python(expr)
        assert "all(" in result
        assert "for i in" in result

    def test_quantified_exists(self):
        expr = QuantifiedExpr(
            quantifier="exists",
            variable="i",
            domain=ActorRef(name="ROBOT", index="*"),
            predicate=Identifier("i"),
        )
        result = expr_to_python(expr)
        assert "any(" in result

    def test_actor_ref_wildcard(self):
        expr = ActorRef(name="ROBOT", index="*")
        result = expr_to_python(expr)
        assert "ROBOT" in result

    def test_actor_ref_simple(self):
        expr = ActorRef(name="DISPATCHER")
        result = expr_to_python(expr)
        assert "DISPATCHER" in result


# === Actor generation tests ===

class TestActorGen:
    def test_simple_actor(self):
        actor = ActorDef(
            id=ActorRef(name="DISPATCHER"),
            role="planner",
            autonomy=AutonomyLevel.LOW,
            capabilities=["compute_routes"],
        )
        code = generate_actor_class(actor)
        assert "class DispatcherActor" in code
        assert 'ROLE = "planner"' in code
        assert "AutonomyLevel.LOW" in code
        assert "def compute_routes" in code
        compile(code, "<test>", "exec")  # Must be valid Python

    def test_parameterized_actor(self):
        actor = ActorDef(
            id=ActorRef(name="ROBOT", index=RangeExpr(start=1, end="N")),
            role="vehicle",
            autonomy=AutonomyLevel.HIGH,
        )
        code = generate_actor_class(actor)
        assert "class RobotActor" in code
        assert "index: int" in code
        compile(code, "<test>", "exec")

    def test_actors_module(self):
        actors = [
            ActorDef(id=ActorRef(name="A"), role="r1", autonomy=AutonomyLevel.LOW),
            ActorDef(id=ActorRef(name="B"), role="r2", autonomy=AutonomyLevel.HIGH),
        ]
        code = generate_actors_module(actors)
        assert "class AActor" in code
        assert "class BActor" in code
        compile(code, "<test>", "exec")


# === Contract generation tests ===

class TestContractGen:
    def test_contract_monitor(self):
        contract = ContractDef(
            id="TEST_SLA",
            parties=[ActorRef(name="A"), ActorRef(name="B")],
            assume=[BoolLiteral(True)],
            guarantee=[BinaryOp(">", IntLiteral(10), IntLiteral(5))],
        )
        code = generate_contract_monitor(contract)
        assert "class TestSlaMonitor" in code
        assert "check_assumptions" in code
        assert "check_guarantees" in code
        compile(code, "<test>", "exec")


# === Protocol generation tests ===

class TestProtocolGen:
    def test_simple_protocol(self):
        protocol = ProtocolDef(
            id="TEST_PROTO",
            trigger="event()",
            steps=[
                MessageStep(
                    sender=ActorRef(name="A"),
                    receiver=ActorRef(name="B"),
                    message=Identifier("notify"),
                ),
                ComputeStep(
                    actor=ActorRef(name="B"),
                    computation=Identifier("process"),
                ),
            ],
        )
        code = generate_protocol_class(protocol)
        assert "class TestProtoProtocol" in code
        assert "TRIGGER" in code
        assert "send_message" in code
        compile(code, "<test>", "exec")


# === Full generate() integration tests ===

class TestGenerate:
    def test_generate_creates_files(self):
        sos = SoSDefinition(
            name="TestSoS",
            actors=[
                ActorDef(id=ActorRef(name="A"), role="role_a", autonomy=AutonomyLevel.LOW),
            ],
            contracts=[
                ContractDef(
                    id="C1",
                    parties=[ActorRef(name="A")],
                    assume=[BoolLiteral(True)],
                    guarantee=[BoolLiteral(True)],
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "gen"
            generate(sos, output)
            assert (output / "__init__.py").exists()
            assert (output / "actors.py").exists()
            assert (output / "contracts.py").exists()
            assert (output / "runtime.py").exists()

    def test_generated_files_compile(self):
        sos = SoSDefinition(
            name="TestSoS",
            actors=[
                ActorDef(id=ActorRef(name="WORKER"), role="worker", autonomy=AutonomyLevel.MEDIUM),
            ],
            contracts=[
                ContractDef(
                    id="WORK_CONTRACT",
                    parties=[ActorRef(name="WORKER")],
                    guarantee=[BoolLiteral(True)],
                ),
            ],
            protocols=[
                ProtocolDef(
                    id="WORK_PROTO",
                    trigger="start()",
                    steps=[
                        ComputeStep(actor=ActorRef(name="WORKER"), computation=Identifier("do_work")),
                    ],
                ),
            ],
            metrics=[
                MetricDef(id="efficiency", formula="output / input", target=">= 0.9"),
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "gen"
            generate(sos, output)
            for f in output.glob("*.py"):
                code = f.read_text()
                compile(code, str(f), "exec")


# === Robot delivery integration test ===

class TestRobotDeliveryCodegen:
    def test_robot_delivery_codegen(self):
        """Robot delivery example generates compilable code."""
        from cadl.parser import parse_file

        cadl_file = Path(__file__).parent.parent / "examples" / "robot_delivery.cadl"
        if not cadl_file.exists():
            pytest.skip("robot_delivery.cadl not found")

        sos = parse_file(cadl_file)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "robot_delivery"
            generate(sos, output)

            # Check all files exist
            expected = ["__init__.py", "actors.py", "contracts.py",
                        "protocols.py", "runtime.py", "metrics.py", "transitions.py"]
            for fname in expected:
                assert (output / fname).exists(), f"Missing: {fname}"

            # Check all files compile
            for f in output.glob("*.py"):
                code = f.read_text()
                compile(code, str(f), "exec")

            # Check key classes present
            actors_code = (output / "actors.py").read_text()
            assert "class DispatcherActor" in actors_code
            assert "class RobotActor" in actors_code
            assert "class CustomerActor" in actors_code

            contracts_code = (output / "contracts.py").read_text()
            assert "class DeliverySlaMonitor" in contracts_code

            protocols_code = (output / "protocols.py").read_text()
            assert "class FailureReplanProtocol" in protocols_code

            runtime_code = (output / "runtime.py").read_text()
            assert "class RobotDeliverySystemRuntime" in runtime_code


class TestHouseholdChoresCodegen:
    def test_household_chores_codegen(self):
        """Household chores example generates compilable code."""
        from cadl.parser import parse_file

        cadl_file = Path(__file__).parent.parent / "examples" / "household_chores.cadl"
        if not cadl_file.exists():
            pytest.skip("household_chores.cadl not found")

        sos = parse_file(cadl_file)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "household"
            generate(sos, output)

            for f in output.glob("*.py"):
                code = f.read_text()
                compile(code, str(f), "exec")
