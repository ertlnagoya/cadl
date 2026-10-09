"""CADL Expression Compiler — translates Expression AST nodes to Python source.

Analogous to verifier.py:expr_to_z3(), but emits Python code strings
instead of Z3 objects.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, Iterator, Optional, Set

from ..ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    Comprehension,
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


# Actor names declared by the SoS being generated. The expression grammar
# reads every bare name as an actor reference; generators use this set to
# tell a declared actor (``DISPATCHER``) from a state variable
# (``delivery_time``). ``None`` means "unknown": treat every name as an actor.
_DECLARED_ACTORS: ContextVar[Optional[FrozenSet[str]]] = ContextVar(
    "cadl_declared_actors", default=None
)


@contextmanager
def declared_actors(names: Iterable[str]) -> Iterator[None]:
    """Compile expressions knowing which names are actors."""
    token = _DECLARED_ACTORS.set(frozenset(names))
    try:
        yield
    finally:
        _DECLARED_ACTORS.reset(token)


def is_state_variable(ref: ActorRef, actor_names: Optional[FrozenSet[str]]) -> bool:
    """True if a bare name is known not to be a declared actor."""
    return (
        ref.index is None
        and actor_names is not None
        and ref.name not in actor_names
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
    # Declared actor names, or None if unknown
    actor_names: Optional[FrozenSet[str]] = field(
        default_factory=_DECLARED_ACTORS.get
    )


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
        return _actor_ref_str(expr, ctx)

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

    if isinstance(expr, Comprehension):
        prev_locals = ctx.locals.copy()
        ctx.locals.add(expr.variable)
        element = expr_to_python(expr.element, ctx)
        ctx.locals = prev_locals

        if isinstance(expr.domain, RangeExpr):
            end = expr.domain.end
            if isinstance(end, str):
                end = f"{ctx.state_prefix}['{end}']"
            domain = f"range({expr.domain.start}, {end} + 1)"
        else:
            domain = expr_to_python(expr.domain, ctx)
        return f"{element} for {expr.variable} in {domain}"

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
    if ref.index is None:
        # A bound variable (e.g. the ``r`` of ``for all r in ROBOT[*]``)
        # is the element itself, not an entry of the actor table.
        if name in ctx.locals:
            return name
        if is_state_variable(ref, ctx.actor_names):
            return f"{ctx.state_prefix}['{name}']"
        return f"{ctx.actor_prefix}['{name}']"
    if ref.index == "*" or isinstance(ref.index, RangeExpr):
        return f"{ctx.actor_prefix}['{name}']"
    return f"{ctx.actor_prefix}['{name}'][{_index_str(ref.index, ctx)}]"


def _index_str(index: Any, ctx: CompilerContext) -> str:
    """Render an actor index; a bare name is an index variable."""
    if isinstance(index, (str, int)):
        return str(index)
    if isinstance(index, (ActorRef, Identifier)) and getattr(index, "index", None) is None:
        return index.name
    return expr_to_python(index, ctx)
