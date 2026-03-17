"""CADL Expression Compiler — translates Expression AST nodes to Python source.

Analogous to verifier.py:expr_to_z3(), but emits Python code strings
instead of Z3 objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Set

from ..ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    DurationLiteral,
    Expression,
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


@dataclass
class CompilerContext:
    """Tracks compilation context for expression translation."""
    # How to resolve bare identifiers
    state_prefix: str = "ctx.state"
    actor_prefix: str = "ctx.actors"
    # Track which timedelta import is needed
    needs_timedelta: bool = False
    # Local variables (e.g., quantifier variables)
    locals: Set[str] = field(default_factory=set)


def expr_to_python(expr: Expression, ctx: CompilerContext = None) -> str:
    """Convert a CADL Expression AST node to a Python source string.

    Mirrors the structure of verifier.py:expr_to_z3() (line 100-208).
    """
    if ctx is None:
        ctx = CompilerContext()

    if isinstance(expr, BoolLiteral):
        return "True" if expr.value else "False"

    if isinstance(expr, IntLiteral):
        return str(expr.value)

    if isinstance(expr, FloatLiteral):
        return str(expr.value)

    if isinstance(expr, StringLiteral):
        # Escape single quotes within the string
        escaped = expr.value.replace("'", "\\'")
        return f"'{escaped}'"

    if isinstance(expr, DurationLiteral):
        ctx.needs_timedelta = True
        unit_map = {
            "ms": "milliseconds",
            "s": "seconds",
            "min": "minutes",
            "h": "hours",
        }
        unit = unit_map.get(expr.unit, "milliseconds")
        return f"timedelta({unit}={expr.value})"

    if isinstance(expr, Identifier):
        if expr.name in ctx.locals:
            return expr.name
        return f"{ctx.state_prefix}['{expr.name}']"

    if isinstance(expr, ActorRef):
        name = expr.name
        if expr.index == "*":
            return f"{ctx.actor_prefix}['{name}']"
        if isinstance(expr.index, RangeExpr):
            return f"{ctx.actor_prefix}['{name}']"
        if expr.index is not None:
            idx = expr.index
            if isinstance(idx, str) and idx in ctx.locals:
                return f"{ctx.actor_prefix}['{name}'][{idx}]"
            return f"{ctx.actor_prefix}['{name}'][{idx}]"
        return f"{ctx.actor_prefix}['{name}']"

    if isinstance(expr, MemberAccess):
        obj = _actor_ref_str(expr.obj, ctx)
        return f"{obj}.state['{expr.member}']"

    if isinstance(expr, FunctionCall):
        args = ", ".join(expr_to_python(a, ctx) for a in expr.args)
        return f"ctx.{expr.name}({args})"

    if isinstance(expr, BinaryOp):
        left = expr_to_python(expr.left, ctx)
        right = expr_to_python(expr.right, ctx)

        op_map = {
            "AND": "and",
            "OR": "or",
            "==": "==",
            "!=": "!=",
            "<": "<",
            "<=": "<=",
            ">": ">",
            ">=": ">=",
            "+": "+",
            "-": "-",
            "*": "*",
            "/": "/",
        }
        py_op = op_map.get(expr.op, expr.op)
        return f"({left} {py_op} {right})"

    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            operand = expr_to_python(expr.operand, ctx)
            return f"(not {operand})"
        operand = expr_to_python(expr.operand, ctx)
        return f"({expr.op} {operand})"

    if isinstance(expr, QuantifiedExpr):
        # Save and extend locals
        prev_locals = ctx.locals.copy()
        ctx.locals.add(expr.variable)

        domain = expr_to_python(expr.domain, ctx)
        predicate = expr_to_python(expr.predicate, ctx)

        ctx.locals = prev_locals

        if expr.quantifier == "for_all":
            return f"all({predicate} for {expr.variable} in {domain})"
        else:
            return f"any({predicate} for {expr.variable} in {domain})"

    # Fallback: represent as a string comment
    return f"None  # unsupported expression: {type(expr).__name__}"


def _actor_ref_str(ref: ActorRef, ctx: CompilerContext) -> str:
    """Convert an ActorRef to a Python accessor string."""
    name = ref.name
    if ref.index == "*":
        return f"{ctx.actor_prefix}['{name}']"
    if ref.index is not None:
        idx = ref.index
        if isinstance(idx, str) and idx in ctx.locals:
            return f"{ctx.actor_prefix}['{name}'][{idx}]"
        return f"{ctx.actor_prefix}['{name}'][{idx}]"
    return f"{ctx.actor_prefix}['{name}']"
