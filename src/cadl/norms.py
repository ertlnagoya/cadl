"""Helpers for the norms extension (Appendix F, norms 0.1).

The temporal operators of F.4 are written as function calls, so the
expression grammar is unchanged. They have a temporal meaning only in a
file that declares ``norms: 0.1`` under ``extensions:``; elsewhere
``within``, ``throughout`` and ``until`` are ordinary function names.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, Iterator

from .ast_nodes import (
    ActorRef,
    BinaryOp,
    Comprehension,
    ContractDef,
    FunctionCall,
    MemberAccess,
    QuantifiedExpr,
    SoSDefinition,
    StringLiteral,
    UnaryOp,
)

NORMS_EXTENSION = ("norms", "0.1")
# Operator name -> (number of arguments, position of the duration argument)
TEMPORAL_OPERATORS = {"within": (2, 0), "throughout": (2, 0), "until": (3, 2)}
NORM_KINDS = ("obligation", "permission", "prohibition", "power")

# A temporal call in text that is not a predicate (kept as an opaque item).
_TEMPORAL_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_.])(?:within|throughout|until)\s*\(")


def norms_declared(sos: SoSDefinition) -> bool:
    """True when the file declares ``norms: 0.1`` under ``extensions:``."""
    return NORMS_EXTENSION in sos.extensions


def temporal_calls(expr: Any) -> Iterator[FunctionCall]:
    """Every call of a temporal operator in an expression, outermost first."""
    if isinstance(expr, FunctionCall):
        if expr.name in TEMPORAL_OPERATORS:
            yield expr
        for arg in expr.args:
            yield from temporal_calls(arg)
    elif isinstance(expr, BinaryOp):
        yield from temporal_calls(expr.left)
        yield from temporal_calls(expr.right)
    elif isinstance(expr, UnaryOp):
        yield from temporal_calls(expr.operand)
    elif isinstance(expr, QuantifiedExpr):
        yield from temporal_calls(expr.domain)
        yield from temporal_calls(expr.predicate)
    elif isinstance(expr, Comprehension):
        yield from temporal_calls(expr.element)
        yield from temporal_calls(expr.domain)
    elif isinstance(expr, MemberAccess):
        yield from temporal_calls(expr.obj)
    elif isinstance(expr, ActorRef):
        if expr.index is not None and not isinstance(expr.index, (str, int)):
            yield from temporal_calls(expr.index)


def has_temporal_text(text: str) -> bool:
    """True when text that is not a predicate contains a temporal call."""
    return bool(_TEMPORAL_TEXT_RE.search(text))


def contains_temporal(expr: Any) -> bool:
    """True when an ``assume:`` / ``guarantee:`` item has a temporal operator.

    An item that is not a predicate of A.10 is kept as a string literal;
    for it the text is searched for a call of a temporal operator.
    """
    if isinstance(expr, StringLiteral):
        return has_temporal_text(expr.value)
    return next(temporal_calls(expr), None) is not None


def item_text(expr: Any) -> str:
    """An ``assume:`` / ``guarantee:`` item as text, for diagnostics."""
    if isinstance(expr, StringLiteral):
        return expr.value
    from .unparse import expr_to_source
    return expr_to_source(expr)


def split_temporal_items(contract: ContractDef) -> tuple[ContractDef, list[tuple[str, str]]]:
    """Leave out the ``assume:`` / ``guarantee:`` items that contain a
    temporal operator (F.4): they are not state predicates, so the SMT
    checks and the code generators cannot use them (F.7).

    Returns a copy of the contract without them (the contract itself when
    there are none) and the left-out items as (key, text) pairs.
    """
    omitted: list[tuple[str, str]] = []
    kept: dict[str, list] = {}
    for key in ("assume", "guarantee"):
        kept[key] = []
        for expr in getattr(contract, key):
            if contains_temporal(expr):
                omitted.append((key, item_text(expr)))
            else:
                kept[key].append(expr)
    if not omitted:
        return contract, omitted
    return replace(contract, assume=kept["assume"], guarantee=kept["guarantee"]), omitted


# Code generation: whether temporal items are left out of the generated
# checks. Set by `cadl.codegen.generate` for a file that declares norms 0.1.
_omit_temporal: ContextVar[bool] = ContextVar("omit_temporal", default=False)


@contextmanager
def temporal_items_omitted(enabled: bool):
    token = _omit_temporal.set(enabled)
    try:
        yield
    finally:
        _omit_temporal.reset(token)


def omitted_in_codegen(expr: Any) -> bool:
    """True when a generator must leave this item out (norms 0.1 declared
    and the item has a temporal operator)."""
    return _omit_temporal.get() and contains_temporal(expr)


def omitted_comment(expr: Any) -> str:
    """Text of the comment a generator writes in place of a left-out item."""
    return (f"not generated: the item has a temporal operator (norms 0.1): "
            f"{item_text(expr)}")
