"""Tests for the CADL expression parser (grammar.lark + ExprTransformer)."""

import pytest

from cadl.ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    Comprehension,
    FloatLiteral,
    FunctionCall,
    IntLiteral,
    MemberAccess,
    QuantifiedExpr,
    RangeExpr,
    UnaryOp,
)
from cadl.parser import parse_expr


def _name(n):
    return ActorRef(name=n)


class TestArithmetic:

    @pytest.mark.parametrize("op", ["+", "-", "*", "/"])
    def test_binary_operator_is_kept(self, op):
        assert parse_expr(f"a {op} b") == BinaryOp(op, _name("a"), _name("b"))

    def test_multiplication_binds_tighter_than_addition(self):
        assert parse_expr("a + b * c") == BinaryOp(
            "+", _name("a"), BinaryOp("*", _name("b"), _name("c"))
        )

    def test_left_associative(self):
        assert parse_expr("a - b - c") == BinaryOp(
            "-", BinaryOp("-", _name("a"), _name("b")), _name("c")
        )

    def test_arithmetic_inside_comparison(self):
        assert parse_expr("active_tasks > capacity * 0.8") == BinaryOp(
            ">",
            _name("active_tasks"),
            BinaryOp("*", _name("capacity"), FloatLiteral(0.8)),
        )

    def test_ratio_of_calls(self):
        assert parse_expr("count(a) / count(b)") == BinaryOp(
            "/",
            FunctionCall("count", [_name("a")]),
            FunctionCall("count", [_name("b")]),
        )

    def test_grouping(self):
        assert parse_expr("(a + b) * c") == BinaryOp(
            "*", BinaryOp("+", _name("a"), _name("b")), _name("c")
        )


class TestLogic:

    def test_and(self):
        assert parse_expr("a AND b") == BinaryOp("AND", _name("a"), _name("b"))

    def test_or(self):
        assert parse_expr("a OR b") == BinaryOp("OR", _name("a"), _name("b"))

    def test_and_binds_tighter_than_or(self):
        expected = BinaryOp("OR", BinaryOp("AND", _name("a"), _name("b")), _name("c"))
        assert parse_expr("a AND b OR c") == expected
        assert parse_expr("c OR a AND b") == BinaryOp(
            "OR", _name("c"), BinaryOp("AND", _name("a"), _name("b"))
        )

    def test_not_binds_tighter_than_and(self):
        assert parse_expr("NOT a AND b") == BinaryOp(
            "AND", UnaryOp("NOT", _name("a")), _name("b")
        )

    def test_not_with_parentheses_is_not_a_call(self):
        assert parse_expr("NOT (a OR b)") == UnaryOp(
            "NOT", BinaryOp("OR", _name("a"), _name("b"))
        )

    def test_comparisons_joined_by_and(self):
        assert parse_expr("flag == false AND level <= 0.4") == BinaryOp(
            "AND",
            BinaryOp("==", _name("flag"), BoolLiteral(False)),
            BinaryOp("<=", _name("level"), FloatLiteral(0.4)),
        )

    def test_keyword_prefix_is_still_an_identifier(self):
        assert parse_expr("ORDER > 1") == BinaryOp(">", _name("ORDER"), IntLiteral(1))
        assert parse_expr("android") == _name("android")


class TestQuantifier:

    def test_body_extends_to_the_end(self):
        expr = parse_expr("for all r in ROBOT[*]: r.battery > 20 AND ok")
        assert isinstance(expr, QuantifiedExpr)
        assert expr.quantifier == "for_all"
        assert expr.predicate == BinaryOp(
            "AND",
            BinaryOp(">", MemberAccess(_name("r"), "battery"), IntLiteral(20)),
            _name("ok"),
        )


class TestMemberAccess:

    def test_plain(self):
        assert parse_expr("DISPATCHER.is_operational") == MemberAccess(
            _name("DISPATCHER"), "is_operational"
        )

    def test_indexed_object(self):
        assert parse_expr("ROBOT[i].battery > 20") == BinaryOp(
            ">",
            MemberAccess(ActorRef("ROBOT", index=_name("i")), "battery"),
            IntLiteral(20),
        )

    def test_wildcard_object(self):
        assert parse_expr("ROBOT[*].status") == MemberAccess(
            ActorRef("ROBOT", index="*"), "status"
        )

    def test_inside_call(self):
        expr = parse_expr("all(ROBOT[*].status != Collision)")
        assert expr == FunctionCall("all", [BinaryOp(
            "!=", MemberAccess(ActorRef("ROBOT", index="*"), "status"), _name("Collision"),
        )])


class TestComprehension:

    def test_numeric_range(self):
        assert parse_expr("sum(ROBOT[i].goal_count for i in 1..5)") == FunctionCall(
            "sum",
            [Comprehension(
                element=MemberAccess(ActorRef("ROBOT", index=_name("i")), "goal_count"),
                variable="i",
                domain=RangeExpr(start=1, end=5),
            )],
        )

    def test_symbolic_range_end(self):
        expr = parse_expr("min(ROBOT[i].goal_count for i in 1..N)")
        assert expr.args[0].domain == RangeExpr(start=1, end="N")

    def test_over_a_set(self):
        expr = parse_expr("sum(r.load for r in ROBOT[*])")
        assert expr.args[0] == Comprehension(
            element=MemberAccess(_name("r"), "load"),
            variable="r",
            domain=ActorRef("ROBOT", index="*"),
        )


class TestQuantifierPosition:
    """A quantifier may follow AND / OR / NOT (spec A.10)."""

    def test_after_and(self):
        expr = parse_expr("a AND for all x in X: p(x)")
        assert expr.op == "AND" and isinstance(expr.right, QuantifiedExpr)

    def test_after_not(self):
        expr = parse_expr("NOT exists x in X: p(x)")
        assert expr.op == "NOT" and isinstance(expr.operand, QuantifiedExpr)
        assert expr.operand.quantifier == "exists"

    def test_scope_extends_to_the_right_after_and(self):
        expr = parse_expr("a AND for all x in X: p(x) OR q")
        assert expr.op == "AND"
        assert expr.right.predicate == BinaryOp(
            "OR", FunctionCall("p", [_name("x")]), _name("q")
        )

    def test_parenthesised_quantifier_can_be_followed(self):
        expr = parse_expr("(for all x in X: p(x)) AND q")
        assert expr.op == "AND" and isinstance(expr.left, QuantifiedExpr)
        assert expr.right == _name("q")


class TestReservedKeywords:

    @pytest.mark.parametrize("word", ["in", "exists", "IN", "AND", "OR", "NOT"])
    def test_keyword_is_not_an_identifier(self, word):
        with pytest.raises(Exception):
            parse_expr(f"{word} > 3")

    @pytest.mark.parametrize("word", ["inside", "existsx", "INDEX", "all", "format"])
    def test_words_containing_a_keyword_are_identifiers(self, word):
        assert parse_expr(f"{word} > 3") == BinaryOp(">", _name(word), IntLiteral(3))
