"""CADL → Solidity expression compiler.

Analogous to expr_compiler.py (Python) but emits Solidity source strings.
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
class SolidityContext:
    """Compilation context for Solidity expression translation."""
    state_prefix: str = "state"
    actor_prefix: str = "actors"
    locals: Set[str] = field(default_factory=set)


def expr_to_solidity(expr: Expression, ctx: SolidityContext | None = None) -> str:
    """Convert a CADL Expression AST node to a Solidity source string."""
    if ctx is None:
        ctx = SolidityContext()

    if isinstance(expr, BoolLiteral):
        return "true" if expr.value else "false"

    if isinstance(expr, IntLiteral):
        return str(expr.value)

    if isinstance(expr, FloatLiteral):
        # Solidity doesn't have native floats; multiply by scale factor
        # Use integer representation with comment
        scaled = int(expr.value * 1000)
        return f"{scaled} /* {expr.value} * 1000 */"

    if isinstance(expr, StringLiteral):
        escaped = expr.value.replace('"', '\\"')
        return f'"{escaped}"'

    if isinstance(expr, DurationLiteral):
        # Convert to seconds for Solidity
        multipliers = {"ms": 1, "s": 1000, "min": 60000, "h": 3600000}
        ms = expr.value * multipliers.get(expr.unit, 1)
        return str(ms)

    if isinstance(expr, Identifier):
        if expr.name in ctx.locals:
            return expr.name
        return f"{ctx.state_prefix}.{expr.name}"

    if isinstance(expr, ActorRef):
        name = expr.name.lower()
        if expr.index == "*":
            return f"{ctx.actor_prefix}_{name}"
        if expr.index is not None:
            idx = expr.index
            if isinstance(idx, str) and idx in ctx.locals:
                return f"{ctx.actor_prefix}_{name}[{idx}]"
            return f"{ctx.actor_prefix}_{name}[{idx}]"
        return f"{ctx.actor_prefix}_{name}"

    if isinstance(expr, MemberAccess):
        obj_name = expr.obj.name.lower()
        return f"{ctx.actor_prefix}_{obj_name}.{expr.member}"

    if isinstance(expr, FunctionCall):
        args = ", ".join(expr_to_solidity(a, ctx) for a in expr.args)
        return f"{expr.name}({args})"

    if isinstance(expr, BinaryOp):
        left = expr_to_solidity(expr.left, ctx)
        right = expr_to_solidity(expr.right, ctx)

        op_map = {
            "AND": "&&",
            "OR": "||",
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
        sol_op = op_map.get(expr.op, expr.op)
        return f"({left} {sol_op} {right})"

    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            operand = expr_to_solidity(expr.operand, ctx)
            return f"(!{operand})"
        operand = expr_to_solidity(expr.operand, ctx)
        return f"({expr.op} {operand})"

    if isinstance(expr, QuantifiedExpr):
        # Solidity doesn't have quantifiers; emit a helper comment
        prev_locals = ctx.locals.copy()
        ctx.locals.add(expr.variable)
        predicate = expr_to_solidity(expr.predicate, ctx)
        ctx.locals = prev_locals

        if expr.quantifier == "for_all":
            return f"true /* for_all {expr.variable}: {predicate} */"
        else:
            return f"false /* exists {expr.variable}: {predicate} */"

    return f"true /* unsupported: {type(expr).__name__} */"
