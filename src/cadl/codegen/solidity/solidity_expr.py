"""CADL → Solidity expression compiler.

Analogous to expr_compiler.py (Python) but emits Solidity source strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Set

from ..expr_compiler import _DECLARED_ACTORS, is_state_variable
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
    # Declared actor names, or None if unknown (see expr_compiler)
    actor_names: Optional[FrozenSet[str]] = field(
        default_factory=_DECLARED_ACTORS.get
    )


def _index_str(index, ctx: "SolidityContext") -> str:
    """Render an actor index; a bare name is an index variable."""
    if isinstance(index, (str, int)):
        return str(index)
    if isinstance(index, (ActorRef, Identifier)) and getattr(index, "index", None) is None:
        return index.name
    return expr_to_solidity(index, ctx)


def _is_state_name(expr, ctx: "SolidityContext") -> bool:
    return (
        isinstance(expr, ActorRef)
        and expr.name not in ctx.locals
        and is_state_variable(expr, ctx.actor_names)
    )


def predicate_to_solidity(expr: Expression, ctx: "SolidityContext | None" = None) -> str:
    """Compile an expression used as a truth value.

    The generated contracts keep state in ``stateUint`` and ``stateBool``;
    a state variable standing alone as a condition is read from the latter.
    """
    if ctx is None:
        ctx = SolidityContext()
    if _is_state_name(expr, ctx):
        return f'stateBool["{expr.name}"]'
    return expr_to_solidity(expr, ctx)


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
        if _is_state_name(expr, ctx):
            return f'stateUint["{expr.name}"]'
        name = expr.name.lower()
        if expr.index == "*" or isinstance(expr.index, RangeExpr):
            return f"{ctx.actor_prefix}_{name}"
        if expr.index is not None:
            return f"{ctx.actor_prefix}_{name}[{_index_str(expr.index, ctx)}]"
        return f"{ctx.actor_prefix}_{name}"

    if isinstance(expr, MemberAccess):
        obj_name = expr.obj.name.lower()
        return f"{ctx.actor_prefix}_{obj_name}.{expr.member}"

    if isinstance(expr, FunctionCall):
        args = ", ".join(expr_to_solidity(a, ctx) for a in expr.args)
        return f"{expr.name}({args})"

    if isinstance(expr, BinaryOp):
        boolean_operands = expr.op in ("AND", "OR") or (
            expr.op in ("==", "!=")
            and (isinstance(expr.left, BoolLiteral) or isinstance(expr.right, BoolLiteral))
        )
        compile_operand = predicate_to_solidity if boolean_operands else expr_to_solidity
        left = compile_operand(expr.left, ctx)
        right = compile_operand(expr.right, ctx)

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
            operand = predicate_to_solidity(expr.operand, ctx)
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
