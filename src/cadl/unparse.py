"""Render Expression AST nodes back to CADL source text.

Code generators and the simulator IR carry expressions as text (labels,
comments, config strings). They all go through :func:`expr_to_source` so
the text matches what the author wrote, including indices and grouping.
"""

from __future__ import annotations

from typing import Any

from .ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    Comprehension,
    DurationLiteral,
    FloatLiteral,
    FunctionCall,
    Identifier,
    IntLiteral,
    MemberAccess,
    QuantifiedExpr,
    RangeExpr,
    StringLiteral,
    UnaryOp,
)

# Binding strength, loosest first (mirrors grammar.lark).
_PREC_QUANTIFIER = 0
_PREC_OR = 1
_PREC_AND = 2
_PREC_NOT = 3
_PREC_COMPARISON = 4
_PREC_ADD = 5
_PREC_MUL = 6
_PREC_ATOM = 7

_BINARY_PREC = {
    "OR": _PREC_OR,
    "AND": _PREC_AND,
    "==": _PREC_COMPARISON,
    "!=": _PREC_COMPARISON,
    "<": _PREC_COMPARISON,
    "<=": _PREC_COMPARISON,
    ">": _PREC_COMPARISON,
    ">=": _PREC_COMPARISON,
    "+": _PREC_ADD,
    "-": _PREC_ADD,
    "*": _PREC_MUL,
    "/": _PREC_MUL,
}


def actor_ref_to_source(ref: ActorRef) -> str:
    """Render an actor reference such as ``ROBOT[*]`` or ``TAXI[1..N]``."""
    index = ref.index
    if index is None:
        return ref.name
    if isinstance(index, RangeExpr):
        return f"{ref.name}[{index.start}..{index.end}]"
    if isinstance(index, (str, int)):
        return f"{ref.name}[{index}]"
    return f"{ref.name}[{expr_to_source(index)}]"


def _precedence(expr: Any) -> int:
    if isinstance(expr, QuantifiedExpr):
        return _PREC_QUANTIFIER
    if isinstance(expr, BinaryOp):
        return _BINARY_PREC.get(expr.op, _PREC_ATOM)
    if isinstance(expr, UnaryOp):
        return _PREC_NOT
    return _PREC_ATOM


def _child(expr: Any, minimum: int) -> str:
    text = _render(expr, nested=True)
    return f"({text})" if _precedence(expr) < minimum else text


def _render(expr: Any, nested: bool) -> str:
    if isinstance(expr, BoolLiteral):
        return "true" if expr.value else "false"
    if isinstance(expr, (IntLiteral, FloatLiteral)):
        return str(expr.value)
    if isinstance(expr, DurationLiteral):
        return f"{expr.value}{expr.unit}"
    if isinstance(expr, StringLiteral):
        # A top-level string is free text the parser could not read as an
        # expression; inside an expression it is a quoted literal.
        return f'"{expr.value}"' if nested else expr.value
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, ActorRef):
        return actor_ref_to_source(expr)
    if isinstance(expr, MemberAccess):
        return f"{actor_ref_to_source(expr.obj)}.{expr.member}"
    if isinstance(expr, FunctionCall):
        args = ", ".join(_render(a, nested=True) for a in expr.args)
        return f"{expr.name}({args})"
    if isinstance(expr, BinaryOp):
        prec = _precedence(expr)
        if prec == _PREC_COMPARISON:
            # Comparisons do not chain.
            left = _child(expr.left, prec + 1)
        else:
            left = _child(expr.left, prec)
        right = _child(expr.right, prec + 1)
        return f"{left} {expr.op} {right}"
    if isinstance(expr, UnaryOp):
        return f"{expr.op} {_child(expr.operand, _PREC_NOT)}"
    if isinstance(expr, Comprehension):
        if isinstance(expr.domain, RangeExpr):
            domain = f"{expr.domain.start}..{expr.domain.end}"
        else:
            domain = _child(expr.domain, _PREC_ADD)
        element = _child(expr.element, _PREC_OR)
        return f"{element} for {expr.variable} in {domain}"
    if isinstance(expr, QuantifiedExpr):
        keyword = "for all" if expr.quantifier == "for_all" else "exists"
        domain = _render(expr.domain, nested=True)
        body = _render(expr.predicate, nested=True)
        return f"{keyword} {expr.variable} in {domain}: {body}"
    return str(expr)


def expr_to_source(expr: Any) -> str:
    """Render an expression as CADL source text."""
    return _render(expr, nested=False)
