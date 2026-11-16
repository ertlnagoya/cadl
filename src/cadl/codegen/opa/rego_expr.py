"""CADL → Rego expression compiler.

Analogous to expr_compiler.py (Python) but emits OPA Rego source strings.
"""

from __future__ import annotations

import re

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


# Functions of A.10 predicates that Rego has with the same meaning.
_REGO_FUNCTIONS = frozenset({"count", "sum", "max", "min", "abs"})


@dataclass
class RegoContext:
    """Compilation context for Rego expression translation."""
    input_prefix: str = "input"
    locals: Set[str] = field(default_factory=set)
    # counter of the variables that negate_rego binds
    fresh: int = 0


# A reference to the input in generated Rego: input.a.b, input.r[x],
# input.calls["TEXT"] (the key a JSON string).
_INPUT_REF_RE = re.compile(r'input(?:\.[A-Za-z_][A-Za-z0-9_]*|\[(?:"(?:[^"\\]|\\.)*"|[^\]"]*)\])+')


def negate_rego(literal: str, ctx: RegoContext) -> str:
    """`not literal`, with every input reference bound to a variable first,
    so that the negation does not hold when a value is missing (an
    undefined reference makes the binding, and so the rule body, fail;
    a bare `not input.y == 3` holds when y is missing). Several literals
    joined by "; " in one body."""
    refs: dict = {}

    def bind(m: "re.Match") -> str:
        ref = m.group(0)
        if ref not in refs:
            ctx.fresh += 1
            refs[ref] = f"neg_{ctx.fresh}"
        return refs[ref]
    body = _INPUT_REF_RE.sub(bind, literal)
    binds = [f"{var} := {ref}" for ref, var in refs.items()]
    return "; ".join(binds + [f"not {body}"])


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
        if isinstance(expr.index, RangeExpr):
            return f"{ctx.input_prefix}.{name}"
        if expr.index is not None:
            idx = expr.index
            if isinstance(idx, (ActorRef, Identifier)) and getattr(idx, "index", None) is None:
                idx = idx.name
            elif not isinstance(idx, (str, int)):
                idx = expr_to_rego(idx, ctx)
            return f"{ctx.input_prefix}.{name}[{idx}]"
        return f"{ctx.input_prefix}.{name}"

    if isinstance(expr, MemberAccess):
        obj_name = expr.obj.name.lower()
        return f"{ctx.input_prefix}.{obj_name}.{expr.member}"

    if isinstance(expr, FunctionCall):
        args = ", ".join(expr_to_rego(a, ctx) for a in expr.args)
        if args and expr.name in _REGO_FUNCTIONS:
            return f"{expr.name}({args})"
        if args:
            # A function that Rego does not have (`length(goal_sequence)`,
            # `collision_resolved_within(30s)`): the host gives the value of
            # the call, keyed by its text. Up to 0.3.32 the call was written
            # as is, which OPA rejects as an undefined function.
            import json
            from ...unparse import expr_to_source
            return f"{ctx.input_prefix}.calls[{json.dumps(expr_to_source(expr))}]"
        # A function without arguments that A.10 does not define, such as
        # all_routes_conflict_free(): a value the host gives in the input, as
        # for a name. Up to 0.3.32 it was written as a bare variable, which
        # OPA rejects as unsafe.
        return f"{ctx.input_prefix}.{expr.name}"

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
            return negate_rego(expr_to_rego(expr.operand, ctx), ctx)
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
