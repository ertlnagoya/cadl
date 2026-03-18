"""CADL → Rego expression compiler.

Analogous to expr_compiler.py (Python) but emits OPA Rego source strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Set

from ...ast_nodes import (
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
class RegoContext:
    """Compilation context for Rego expression translation."""
    input_prefix: str = "input"
    locals: Set[str] = field(default_factory=set)


def expr_to_rego(expr: Expression, ctx: RegoContext | None = None) -> str:
    """Convert a CADL Expression AST node to a Rego source string."""
    if ctx is None:
        ctx = RegoContext()

    if isinstance(expr, BoolLiteral):
        return "true" if expr.value else "false"

    if isinstance(expr, IntLiteral):
        return str(expr.value)

    if isinstance(expr, FloatLiteral):
        return str(expr.value)

    if isinstance(expr, StringLiteral):
        escaped = expr.value.replace('"', '\\"')
        return f'"{escaped}"'

    if isinstance(expr, DurationLiteral):
        # Convert to milliseconds as number
        multipliers = {"ms": 1, "s": 1000, "min": 60000, "h": 3600000}
        ms = expr.value * multipliers.get(expr.unit, 1)
        return str(ms)

    if isinstance(expr, Identifier):
        if expr.name in ctx.locals:
            return expr.name
        return f"{ctx.input_prefix}.{expr.name}"

    if isinstance(expr, ActorRef):
        name = expr.name.lower()
        if expr.index == "*":
            return f"{ctx.input_prefix}.{name}"
        if expr.index is not None:
            idx = expr.index
            if isinstance(idx, str) and idx in ctx.locals:
                return f"{ctx.input_prefix}.{name}[{idx}]"
            return f"{ctx.input_prefix}.{name}[{idx}]"
        return f"{ctx.input_prefix}.{name}"

    if isinstance(expr, MemberAccess):
        obj_name = expr.obj.name.lower()
        return f"{ctx.input_prefix}.{obj_name}.{expr.member}"

    if isinstance(expr, FunctionCall):
        args = ", ".join(expr_to_rego(a, ctx) for a in expr.args)
        if args:
            return f"{expr.name}({args})"
        return f"{expr.name}"

    if isinstance(expr, BinaryOp):
        left = expr_to_rego(expr.left, ctx)
        right = expr_to_rego(expr.right, ctx)

        op_map = {
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

        # AND in Rego is implicit (separate lines), but in expressions we use semicolon
        if expr.op == "AND":
            return f"{left}; {right}"
        if expr.op == "OR":
            # OR requires separate rule bodies in Rego; in expressions, not directly supported
            return f"({left}) # OR ({right})"

        rego_op = op_map.get(expr.op, expr.op)
        return f"{left} {rego_op} {right}"

    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            operand = expr_to_rego(expr.operand, ctx)
            return f"not {operand}"
        operand = expr_to_rego(expr.operand, ctx)
        return f"{expr.op} {operand}"

    if isinstance(expr, QuantifiedExpr):
        prev_locals = ctx.locals.copy()
        ctx.locals.add(expr.variable)
        predicate = expr_to_rego(expr.predicate, ctx)
        domain = expr_to_rego(expr.domain, ctx)
        ctx.locals = prev_locals

        if expr.quantifier == "for_all":
            # Rego: count({x | domain[x]; predicate}) == count(domain)
            return f'count({{x | {domain}[x]; {predicate}}}) == count({domain})'
        else:
            return f"some {expr.variable}; {domain}[{expr.variable}]; {predicate}"

    return f"true # unsupported: {type(expr).__name__}"
