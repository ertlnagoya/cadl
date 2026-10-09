"""Tests for the CADL expression parser (grammar.lark + ExprTransformer)."""

import pytest

from cadl.ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    FloatLiteral,
    FunctionCall,
    IntLiteral,
    MemberAccess,
    QuantifiedExpr,
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
