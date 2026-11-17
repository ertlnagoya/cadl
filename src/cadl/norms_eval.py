"""Offline verdict engine for the norms extension (Appendix F, norms 0.1).

`evaluate` reads one contract of a file that declares ``norms: 0.1`` and a
recorded timed trace of one contract instance, and computes what F.5 of
the specification defines: the state of every norm instance over time,
the reported violations, the institutional events of the ``counts_as:``
rules, unauthorised acts, and the verdicts (``pending``, ``fulfilled``,
``violated``, ``excused``) of the norms and of the ``assume:`` and
``guarantee:`` items.

The engine is an offline monitor: it processes the observations in order
and, at each one, uses only the observations up to it, so every state
change carries the time of the observation at which it was decided.

Trace format (JSON Lines, one observation per line, non-decreasing ``t``)::

    {"t": 0.0, "bindings": {"i": 2}, "request": {"deadline": 300}}
    {"t": 1.0, "values": {"ROBOT[2].battery": 80}}
    {"t": 1.5, "messages": [{"from": "DISPATCHER", "to": "ROBOT[2]",
                             "name": "route_assignment", "args": []}]}
    {"t": 2.0, "state": "Assigned"}
    {"t": 9.0, "end": true}

``t``         time (required): a finite number of at most 2**53 in
              absolute value, so that it is exact; seconds for the
              durations of the file.
``bindings``  index bindings of the instance (first line only).
``request``   fields of ``request`` (first line only).
``values``    updates of the snapshot; a value stays until it is changed.
``messages``  the messages sent at this observation.
``state``     the lifecycle state of the instance after the lifecycle
              transitions of this observation (step 6 of F.5); it stays
              until changed. Predicates read at an observation see the
              state before it. The engine does not run the lifecycle
              state machine.
``amend``     path of a new version of the CADL file, relative to the
              trace file: the contract is amended at this observation,
              before its other steps (F.6).
``end``       ``true`` ends the instance at this observation. The
              instance also ends at the first observation whose state is
              terminal, and otherwise at the last observation, unless
              that one says ``"end": false`` (the trace is then a prefix
              and nothing is settled).

Readings where the specification leaves a choice are listed in the
README under ``cadl norms-eval``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from .ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    Comprehension,
    ContractDef,
    DurationLiteral,
    FloatLiteral,
    FunctionCall,
    IntLiteral,
    MemberAccess,
    NormDef,
    QuantifiedExpr,
    RangeExpr,
    SoSDefinition,
    StringLiteral,
    UnaryOp,
)
from .norms import TEMPORAL_OPERATORS, contains_temporal, item_text, norms_declared


# The engine reaches the parser only through these three functions. The
# monitor that `cadl codegen -t python` generates (codegen/norms_monitor_gen.py)
# contains the text between the "monitor-runtime" marks of this file
# verbatim, and replaces these functions by lookups in a table of the
# expressions parsed at generation time, so that it runs without cadl.

def _parse_rule(text: str) -> Any:
    from .parser import parse_rule
    return parse_rule(text)


def _parse_expr(text: str) -> Any:
    from .parser import parse_expr
    return parse_expr(text)


def _parse_actor(text: str) -> ActorRef:
    from .parser import _parse_actor_ref_str
    return _parse_actor_ref_str(text)


# >>> monitor-runtime
# Numbers are exact (F.5): a number of a trace is the decimal value it is
# written as, a time is compared without tolerance, and arithmetic is on
# rational numbers (`Fraction`). Results are written as floats.
_UNIT_SECONDS = {"ms": Fraction(1, 1000), "s": 1, "min": 60, "h": 3600}
_NUMBER = (int, float, Fraction)
_MESSAGE_EVENT_RE = re.compile(r"^(.+?)\s*->\s*(.+?)\s*:\s*(.+)$")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_ACTOR_RE = re.compile(r"^([A-Za-z_]\w*)(?:\[(.*)\])?$")
# A concrete actor of a message and a message name, as written: no
# whitespace is removed, so `PLAN NER` is not read as `PLANNER`.
_TRACE_ACTOR_RE = re.compile(r"[A-Za-z_]\w*(?:\[(?:0|[1-9][0-9]*)\])?")
_TRACE_NAME_RE = re.compile(r"[A-Za-z_]\w*")
# Largest |t| of a trace: up to it every integer is an exact float, so
# epoch seconds and epoch milliseconds are accepted.
MAX_T = float(2 ** 53)
_TRACE_KEYS = {"t", "bindings", "request", "values", "messages", "state", "end", "amend"}
_MESSAGE_KEYS = {"from", "to", "name", "args"}
FINAL_STATES = ("fulfilled", "violated", "discharged")
_STATE_IGNORED = "the trace gives 'state' but the contract has no lifecycle:; it is ignored"
_END_LAST = "last observation of the trace"
# What is often written for a duration, a string or an operator of A.10
# and is not: `2.5s`, `'k'`, `and`.
_FRACTIONAL_DURATION_RE = re.compile(
    r"(?<![\w.])(\d*\.\d+|\d+\.)\s*(ms|s|min|h)(?![A-Za-z0-9_])")
_LOWER_KEYWORD_RE = re.compile(r"(?<![\w.])(and|or|not)(?![\w(])")


def _outside_strings(text: str) -> str:
    """The text with the contents of its string literals left out."""
    return re.sub(r'"[^"]*"', '""', text)


def duration_hint(text: str) -> str:
    """How to write a fractional duration such as `2.5s` as a duration
    literal (an integer followed by a unit, A.1), or ''."""
    m = _FRACTIONAL_DURATION_RE.search(_outside_strings(text))
    if m is None:
        return ""
    value = Fraction(Decimal(m.group(1))) * _UNIT_SECONDS[m.group(2)]
    for unit in ("h", "min", "s", "ms"):
        n = value / _UNIT_SECONDS[unit]
        if n.denominator == 1:
            return (f"a duration is an integer followed by ms, s, min or h (A.1), "
                    f"so write {m.group(0).replace(' ', '')} as {n.numerator}{unit}")
    return (f"a duration is an integer followed by ms, s, min or h (A.1), and "
            f"{m.group(0).replace(' ', '')} is not a whole number of milliseconds")


def syntax_hint(text: str) -> str:
    """Why a text is probably not a predicate of A.10, for the forms that
    are often written by mistake, or ''."""
    hints = []
    text = _outside_strings(text)
    d = duration_hint(text)
    if d:
        hints.append(d)
    if "'" in text:
        hints.append("a string is written in double quotes (A.1), as in \"k\"; "
                     "single quotes do not delimit a string")
    m = _LOWER_KEYWORD_RE.search(text)
    if m:
        hints.append(f"the operators are written AND, OR and NOT in capitals, "
                     f"not '{m.group(1)}'")
    return "; ".join(hints)


class TraceError(ValueError):
    """The trace is malformed."""


class NormsEvalError(ValueError):
    """The file or the request cannot be evaluated (a usage error)."""


class _Marker:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


# A value that cannot be computed because the trace gives no value for a
# name (final), and a value that later observations will decide.
UNKNOWN = _Marker("UNKNOWN")
UNDECIDED = _Marker("UNDECIDED")


class _Sym(str):
    """A bare name without a value in the trace. Compared with a string
    value by ``==``, ``!=`` or ``IN`` it is an enum-like literal
    (``status == Delivered``); anywhere else it is an undefined quantity."""


class _NotEvaluable(Exception):
    pass


@dataclass
class Observation:
    t: float
    values: dict[str, Any]             # snapshot after this line's updates
    messages: list[dict]
    state: str | None                  # lifecycle state seen by predicates
    state_after: str | None            # lifecycle state after this observation
    end: bool | None = None
    amend: str | None = None           # new version of the file (F.6)


@dataclass
class Trace:
    observations: list[Observation]
    bindings: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)
    path: str | None = None


def _canon_actor(text: str) -> str:
    return re.sub(r"\s+", "", str(text))


def _not_json_number(text: str):
    raise ValueError(f"{text} is not a JSON number")


class _Written(float):
    """A JSON number with a fraction or an exponent: a float that keeps the
    text it was written as, which `_exact` reads exactly."""
    __slots__ = ("text",)


# The range of the exact numbers (F.5): at most NUMBER_LIMIT significant
# digits, at most NUMBER_LIMIT digits after the decimal point, and an
# absolute value of at most MAX_ABS, the largest finite double (every float
# of JSON and of the API is inside it). A number outside it
# is refused: written exactly, 1e-100000000 takes time and memory without
# bound.
NUMBER_LIMIT = 400
MAX_ABS = "1.7976931348623157e308"
_MAX_ABS = Decimal(MAX_ABS)
_RANGE_TEXT = (f"at most {NUMBER_LIMIT} significant digits, at most {NUMBER_LIMIT} "
               f"decimals and an absolute value of at most {MAX_ABS}")


class NumberRangeError(ValueError):
    """A number of a trace is outside the range of the exact numbers."""


def _in_range(d: Decimal) -> bool:
    sign, digits, exp = d.as_tuple()
    if not isinstance(exp, int):
        return False
    while len(digits) > 1 and digits[0] == 0:
        digits = digits[1:]
    return (len(digits) <= NUMBER_LIMIT and exp >= -NUMBER_LIMIT
            and (digits == (0,) or (len(digits) + exp <= 309 and abs(d) <= _MAX_ABS)))


def _checked(text: str) -> Decimal:
    d = Decimal(text)
    if not _in_range(d):
        raise NumberRangeError(
            f"the number {text if len(text) <= 40 else text[:37] + '...'} is outside the "
            f"range of exact numbers ({_RANGE_TEXT})")
    return d


def _finite(text: str) -> float:
    _checked(text)
    value = _Written(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite number")
    value.text = text
    return value


def _integer(text: str) -> int:
    _checked(text)
    return int(text)


def _deadline(d: Any) -> Any:
    """The deadline of a norm in seconds, exactly: a numeric `deadline:`
    as written (deadline_ms rounds it to milliseconds)."""
    if d.deadline_ms is None:
        return None
    text = getattr(d, "deadline_text", None)
    if text is not None:
        try:
            return Fraction(Decimal(text))
        except ArithmeticError:
            pass
    return Fraction(d.deadline_ms, 1000)


def _exact(v: Any) -> Any:
    """A value with its numbers exact: a float is the decimal number that
    Python writes for it (the one it was written as), a Decimal its value."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, str, Fraction)):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return v
        text = getattr(v, "text", None)
        return Fraction(Decimal(text)) if text is not None else Fraction(repr(v))
    if isinstance(v, Decimal):
        return Fraction(v)
    if isinstance(v, dict):
        return {k: _exact(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_exact(x) for x in v]
    return v


def _plain(v: Any) -> Any:
    """A result with its exact numbers written as floats (JSON)."""
    if isinstance(v, Fraction):
        return float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, tuple):
        return tuple(_plain(x) for x in v)
    return v


def _literal_number(expr: Any) -> Fraction:
    """A number literal of a predicate exactly as written (in range: the
    engine does not evaluate a predicate with another, see `unsupported`)."""
    text = getattr(expr, "text", None)
    if text is not None:
        try:
            return Fraction(_checked(text))
        except (ArithmeticError, ValueError):
            pass
    return _exact(float(expr.value))


def _literal_in_range(expr: Any) -> bool:
    text = getattr(expr, "text", None)
    if isinstance(expr, IntLiteral):
        if expr.value.bit_length() > 4 * NUMBER_LIMIT:
            return False
        text = str(expr.value)
    if text is None:
        return True
    try:
        return _in_range(Decimal(text))
    except (ArithmeticError, ValueError):
        return False


def _duration(expr: Any) -> Any:
    return _exact(expr.value) * _UNIT_SECONDS.get(expr.unit, 1)


def _integral(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, Fraction):
        return v.denominator == 1
    return isinstance(v, float) and v.is_integer()


def _show(v: Any) -> str:
    """A value for a message: an exact number as Python writes a float."""
    if isinstance(v, Fraction):
        return repr(int(v)) if v.denominator == 1 else repr(float(v))
    return repr(v)


_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _no_lone_surrogates(obj: Any) -> None:
    if isinstance(obj, str):
        if _LONE_SURROGATE_RE.search(obj):
            raise ValueError("a string has a lone surrogate (\\ud800 to \\udfff), "
                             "which is not UTF-8")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _no_lone_surrogates(k)
            _no_lone_surrogates(v)
    elif isinstance(obj, list):
        for v in obj:
            _no_lone_surrogates(v)


def loads_strict(line: str) -> Any:
    """`json.loads` that rejects NaN, Infinity, numbers that overflow to
    infinity, and lone surrogates in strings; raises ValueError."""
    obj = json.loads(line, parse_constant=_not_json_number, parse_float=_finite,
                     parse_int=_integer)
    _no_lone_surrogates(obj)
    return obj


def declared_actors(sos: SoSDefinition) -> dict[str, tuple]:
    """The declared actors: name -> (indexed, lo, hi, symbolic); a bound
    that is not an integer is None, and `symbolic` is the name of a
    symbolic upper bound (`A[1..N]` gives lo 1, hi None and "N")."""
    out: dict[str, tuple] = {}
    for a in sos.actors:
        index = a.id.index
        lo = hi = symbolic = None
        if isinstance(index, RangeExpr):
            lo = index.start if isinstance(index.start, int) else None
            hi = index.end if isinstance(index.end, int) else None
            if isinstance(index.end, str):
                symbolic = index.end
        out[a.id.name] = (index is not None, lo, hi, symbolic)
    return out


def _is_set(name: str, actors: dict) -> bool:
    """`name` is declared as a set of actors (`A[lo..hi]`)."""
    decl = actors.get(name)
    return decl is not None and decl[0]


def _not_a_set(name: str, actors: dict) -> str:
    what = "a single actor" if name in actors else "not declared"
    return (f"'{name}' is {what}; a quantifier, an aggregate, a wildcard, a range or "
            f"an index variable needs a set declared as {name}[lo..hi] (F.5)")


def _uses_variable(index: Any, bound: dict) -> bool:
    """The index expression refers to a variable of an enclosing
    quantifier or comprehension."""
    if isinstance(index, str):
        return index in bound
    if isinstance(index, ActorRef):
        return (index.index is None and index.name in bound) or \
            _uses_variable(index.index, bound)
    if isinstance(index, BinaryOp):
        return _uses_variable(index.left, bound) or _uses_variable(index.right, bound)
    if isinstance(index, UnaryOp):
        return _uses_variable(index.operand, bound)
    return False


# `A[i]` written in a norm or a counts-as rule: with a binding of `i` to v,
# the contract instance names the member A[v] (F.5, domains of quantifiers).
_INDEXED_REF_RE = re.compile(r"([A-Za-z_]\w*)\s*\[\s*([A-Za-z_]\w*)\s*\]")
# A value key or a message actor that names a member: `A[3].x` or `A[3]`.
_MEMBER_KEY_RE = re.compile(r"^([A-Za-z_]\w*)\[(0|[1-9][0-9]*)\](?:\.|$)")
# The largest domain of a quantifier or comprehension that is enumerated;
# a larger one is evaluated as an open domain (F.5).
MAX_DOMAIN = 10000
# The aggregate functions of a comprehension (A.10) that the engine evaluates.
AGGREGATES = ("sum", "count", "min", "max", "avg")


def _bound_refs(contract: ContractDef) -> set:
    """The pairs (set, index variable) of the references `A[i]` in the
    norms and the counts-as rules of a contract."""
    out: set = set()
    for d in contract.norms:
        for text in (d.bearer, d.counterparty, d.trigger, d.until, d.content):
            if text:
                out.update(_INDEXED_REF_RE.findall(text))
    for r in contract.counts_as:
        for text in (r.when, r.if_):
            if text:
                out.update(_INDEXED_REF_RE.findall(text))
    return out


# === Wildcard and range references in predicates (F.5) ======================
#
# `A[*]` or `A[lo..hi]` outside the domain of a quantifier or comprehension
# is shorthand (F.5, "Wildcard and range references"):
#
# 1. The argument e of an aggregate f(e) that is not a comprehension binds
#    every such reference in e outside a nested aggregate call: f(e) is
#    f(e' for x in D), with all references of one set and index form
#    replaced by the member A[x]. Two sets or forms there: not evaluable.
# 2. Every other reference is bound by its smallest enclosing comparison,
#    or, outside every comparison, by the operand of AND, OR, NOT, the body
#    of a quantifier or the predicate argument of a temporal operator that
#    contains it (the whole predicate at the top): the site s becomes
#    `for all x1 in D1: ... for all xn in Dn: s'`, one variable per set and
#    index form, in the order of their first occurrence.
#
# The variables are named `*1`, `*2`, ...: no written name can hide them.

_COMPARISON_OPS = ("==", "!=", "<", "<=", ">", ">=")
# Aggregates whose value moves one way when the domain grows (F.5): a
# comparison of one of them with an operand that binds no variable is
# decided from the known members when every completion agrees.
_MONOTONE_AGGREGATES = ("count", "min", "max")
_FLIPPED = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}


def _has_binder(node: Any) -> bool:
    """Whether `node` contains a quantifier or a comprehension."""
    if isinstance(node, (QuantifiedExpr, Comprehension)):
        return True
    if isinstance(node, ActorRef):
        return node.index is not None and not isinstance(node.index, (str, int)) \
            and _has_binder(node.index)
    if isinstance(node, MemberAccess):
        return _has_binder(node.obj)
    if isinstance(node, UnaryOp):
        return _has_binder(node.operand)
    if isinstance(node, BinaryOp):
        return _has_binder(node.left) or _has_binder(node.right)
    if isinstance(node, FunctionCall):
        return any(_has_binder(a) for a in node.args)
    return False


def _monotone_operands(expr: Any) -> Any:
    """(aggregate call, other operand, operator with the call on the left)
    of a comparison `count|min|max(e for x in D) op b`, or `b op ...`,
    whose other operand binds no variable; None for any other node."""
    if not (isinstance(expr, BinaryOp) and expr.op in _COMPARISON_OPS):
        return None
    for call, other, op in ((expr.left, expr.right, expr.op),
                            (expr.right, expr.left, _FLIPPED[expr.op])):
        if (isinstance(call, FunctionCall) and call.name in _MONOTONE_AGGREGATES
                and len(call.args) == 1 and isinstance(call.args[0], Comprehension)
                and not (isinstance(other, FunctionCall) and other.name == "__in__")
                and not _has_binder(other)):
            return call, other, op
    return None


def _interval_decides(op: str, lo: Any, hi: Any, b: Any, integral: bool) -> Any:
    """`v op b` for every v in [lo, hi] (bounds may be infinite; with
    `integral`, v ranges over the integers): True when it holds for all,
    False when for none, None otherwise."""
    if op in ("==", "!="):
        inside = lo <= b <= hi and (not integral or _integral(b))
        if lo == hi == b:
            equal = True
        elif not inside:
            equal = False
        else:
            return None
        return equal if op == "==" else not equal
    cmp = {"<": lambda v: v < b, "<=": lambda v: v <= b,
           ">": lambda v: v > b, ">=": lambda v: v >= b}[op]
    at_lo, at_hi = cmp(lo), cmp(hi)
    if at_lo and at_hi:
        return True
    if not at_lo and not at_hi:
        return False
    return None


def _wildcard_key(node: Any) -> Any:
    """(set, "*") for `A[*]`, (set, lo, hi) for `A[lo..hi]`, else None."""
    if isinstance(node, ActorRef):
        if node.index == "*":
            return (node.name, "*")
        if isinstance(node.index, RangeExpr):
            return (node.name, node.index.start, node.index.end)
    return None


def wildcard_text(key: tuple) -> str:
    """`A[*]` or `A[lo..hi]` for a key of `_wildcard_key`."""
    if len(key) == 2:
        return f"{key[0]}[*]"
    return f"{key[0]}[{key[1]}..{key[2]}]"


def _wildcard_domain(key: tuple) -> ActorRef:
    if len(key) == 2:
        return ActorRef(name=key[0], index="*")
    return ActorRef(name=key[0], index=RangeExpr(start=key[1], end=key[2]))


def _is_aggregate_shorthand(node: Any) -> bool:
    """An aggregate call whose one argument is not a comprehension."""
    return (isinstance(node, FunctionCall) and node.name in AGGREGATES
            and len(node.args) == 1 and not isinstance(node.args[0], Comprehension))


def _wildcards(node: Any, out: list, nested_aggregates: bool) -> None:
    """The free wildcard and range references of `node`, in the order in
    which they are written: not in the domain of a quantifier or
    comprehension, and not in the argument of an aggregate shorthand that
    could not be rewritten. With `nested_aggregates` False, nothing inside
    a nested aggregate call either (rule 1)."""
    key = _wildcard_key(node)
    if key is not None:
        out.append(key)
    elif isinstance(node, ActorRef):
        if node.index is not None and not isinstance(node.index, (str, int)):
            _wildcards(node.index, out, nested_aggregates)
    elif isinstance(node, MemberAccess):
        _wildcards(node.obj, out, nested_aggregates)
    elif isinstance(node, UnaryOp):
        _wildcards(node.operand, out, nested_aggregates)
    elif isinstance(node, BinaryOp):
        _wildcards(node.left, out, nested_aggregates)
        _wildcards(node.right, out, nested_aggregates)
    elif isinstance(node, QuantifiedExpr):
        _wildcards(node.predicate, out, nested_aggregates)
    elif isinstance(node, Comprehension):
        _wildcards(node.element, out, nested_aggregates)
    elif isinstance(node, FunctionCall):
        if node.name in AGGREGATES and (not nested_aggregates or _is_aggregate_shorthand(node)):
            return
        for a in node.args:
            _wildcards(a, out, nested_aggregates)


def _substitute(node: Any, names: dict, nested_aggregates: bool) -> Any:
    """`node` with every free reference of `_wildcards` whose key is in
    `names` replaced by the member `A[x]` of the variable x = names[key]."""
    def sub(n: Any) -> Any:
        return _substitute(n, names, nested_aggregates)
    key = _wildcard_key(node)
    if key is not None:
        if key in names:
            return ActorRef(name=node.name, index=ActorRef(name=names[key]))
        return node
    if isinstance(node, ActorRef):
        if node.index is not None and not isinstance(node.index, (str, int)):
            return replace(node, index=sub(node.index))
        return node
    if isinstance(node, MemberAccess):
        return replace(node, obj=sub(node.obj))
    if isinstance(node, UnaryOp):
        return replace(node, operand=sub(node.operand))
    if isinstance(node, BinaryOp):
        return replace(node, left=sub(node.left), right=sub(node.right))
    if isinstance(node, QuantifiedExpr):
        return replace(node, predicate=sub(node.predicate))
    if isinstance(node, Comprehension):
        return replace(node, element=sub(node.element))
    if isinstance(node, FunctionCall):
        if node.name in AGGREGATES and (not nested_aggregates or _is_aggregate_shorthand(node)):
            return node
        return replace(node, args=[sub(a) for a in node.args])
    return node


def _compared_with_itself(node: Any, key: tuple) -> bool:
    """Whether a comparison in `node` (outside nested aggregate calls) has
    a reference of the group `key` on both sides."""
    if isinstance(node, BinaryOp):
        if node.op in _COMPARISON_OPS:
            left: list = []
            right: list = []
            _wildcards(node.left, left, False)
            _wildcards(node.right, right, False)
            if key in left and key in right:
                return True
        return _compared_with_itself(node.left, key) or _compared_with_itself(node.right, key)
    if isinstance(node, UnaryOp):
        return _compared_with_itself(node.operand, key)
    if isinstance(node, QuantifiedExpr):
        return _compared_with_itself(node.predicate, key)
    if isinstance(node, FunctionCall) and node.name not in AGGREGATES:
        return any(_compared_with_itself(a, key) for a in node.args)
    return False


class _Desugar:
    """The rewriting of `desugar_wildcards`; `notes` collects the readings
    that may surprise, as (kind, text) with kind `not`, `pairs`, `or`,
    `same`, `temporal` or `aggregate`."""

    def __init__(self, notes: list | None) -> None:
        self.count = 0
        self.notes = notes
        self.temporal: str | None = None      # the temporal operator being walked

    def note(self, kind: str, text: str) -> None:
        if self.notes is not None and (kind, text) not in self.notes:
            self.notes.append((kind, text))

    def fresh(self) -> str:
        self.count += 1
        return f"*{self.count}"

    # rule 1, innermost aggregate first
    def aggregates(self, node: Any) -> Any:
        if isinstance(node, ActorRef):
            if node.index is not None and not isinstance(node.index, (str, int)) \
                    and _wildcard_key(node) is None:
                return replace(node, index=self.aggregates(node.index))
            return node
        if isinstance(node, MemberAccess):
            return replace(node, obj=self.aggregates(node.obj))
        if isinstance(node, UnaryOp):
            return replace(node, operand=self.aggregates(node.operand))
        if isinstance(node, BinaryOp):
            return replace(node, left=self.aggregates(node.left),
                           right=self.aggregates(node.right))
        if isinstance(node, QuantifiedExpr):
            return replace(node, predicate=self.aggregates(node.predicate))
        if isinstance(node, Comprehension):
            return replace(node, element=self.aggregates(node.element))
        if not isinstance(node, FunctionCall):
            return node
        node = replace(node, args=[self.aggregates(a) for a in node.args])
        if not _is_aggregate_shorthand(node):
            return node
        keys: list = []
        _wildcards(node.args[0], keys, False)
        keys = list(dict.fromkeys(keys))
        if len(keys) != 1:
            if keys:
                self.note("aggregate", f"the argument of {node.name}() refers to "
                          f"{' and '.join(wildcard_text(k) for k in keys)}, so it is not "
                          f"evaluated; write a comprehension")
            return node
        if _compared_with_itself(node.args[0], keys[0]):
            t = wildcard_text(keys[0])
            self.note("same", f"{t} on both sides of a comparison in the argument of "
                      f"{node.name}(): each member is compared with itself; to compare two "
                      f"members, write a comprehension with a quantifier over a second "
                      f"member, as in {node.name}((exists k in {t}: k != j AND ...) "
                      f"for j in {t})")
        var = self.fresh()
        element = _substitute(node.args[0], {keys[0]: var}, False)
        return replace(node, args=[Comprehension(element=element, variable=var,
                                                 domain=_wildcard_domain(keys[0]))])

    # rule 2
    def bind(self, node: Any, negated: bool) -> tuple:
        keys: list = []
        _wildcards(node, keys, True)
        keys = list(dict.fromkeys(keys))
        if not keys:
            return node, set()
        names = {k: self.fresh() for k in keys}
        body = _substitute(node, names, True)
        for k in reversed(keys):
            body = QuantifiedExpr(quantifier="for_all", variable=names[k],
                                  domain=_wildcard_domain(k), predicate=body)
        texts = " and ".join(wildcard_text(k) for k in keys)
        if negated:
            self.note("not", f"{texts} under NOT: NOT applies to 'for all' over the members, "
                      f"so the predicate holds when some member does not satisfy the "
                      f"comparison")
        if len(keys) > 1:
            self.note("pairs", f"{texts} in one comparison: it must hold for every "
                      f"combination of their members")
        if isinstance(node, BinaryOp) and node.op in _COMPARISON_OPS:
            left: list = []
            right: list = []
            _wildcards(node.left, left, True)
            _wildcards(node.right, right, True)
            for k in [k for k in keys if k in left and k in right]:
                t = wildcard_text(k)
                self.note("same", f"{t} on both sides of a comparison: each member is "
                          f"compared with itself; to compare two members, write "
                          f"for all j in {t}: for all k in {t}: j == k OR ... "
                          f"with {k[0]}[j] and {k[0]}[k]")
        if self.temporal is not None:
            first = wildcard_text(keys[0])
            self.note("temporal", f"{texts} inside {self.temporal}(): the comparison must "
                      f"hold for all members at the same observation; for each member on "
                      f"its own, write the quantifier outside, as in for all j in {first}: "
                      f"{self.temporal}(...) with {keys[0][0]}[j]")
        return body, set(keys)

    def walk(self, node: Any, pred: bool, negated: bool) -> tuple:
        """(node rewritten, keys bound in it); `pred`: the node stands where
        a predicate is expected."""
        if isinstance(node, BinaryOp) and node.op in ("AND", "OR"):
            left, kl = self.walk(node.left, True, negated)
            right, kr = self.walk(node.right, True, negated)
            if node.op == "OR":
                for k in sorted(kl & kr, key=str):
                    self.note("or", f"{wildcard_text(k)} in both operands of OR: each "
                              f"comparison quantifies over the members on its own")
            return replace(node, left=left, right=right), kl | kr
        if isinstance(node, UnaryOp) and node.op == "NOT":
            operand, keys = self.walk(node.operand, True, not negated)
            return replace(node, operand=operand), keys
        if isinstance(node, QuantifiedExpr):
            body, keys = self.walk(node.predicate, True, negated)
            return replace(node, predicate=body), keys
        if isinstance(node, FunctionCall) and node.name in TEMPORAL_OPERATORS:
            args, keys = [], set()
            outer, self.temporal = self.temporal, node.name
            try:
                for a in node.args:
                    if isinstance(a, DurationLiteral):
                        args.append(a)
                    else:
                        a, k = self.walk(a, True, negated)
                        args.append(a)
                        keys |= k
            finally:
                self.temporal = outer
            return replace(node, args=args), keys
        keys: set = set()
        if isinstance(node, ActorRef):
            if node.index is not None and not isinstance(node.index, (str, int)) \
                    and _wildcard_key(node) is None:
                index, keys = self.walk(node.index, False, negated)
                node = replace(node, index=index)
        elif isinstance(node, MemberAccess):
            obj, keys = self.walk(node.obj, False, negated)
            node = replace(node, obj=obj)
        elif isinstance(node, UnaryOp):
            operand, keys = self.walk(node.operand, False, negated)
            node = replace(node, operand=operand)
        elif isinstance(node, BinaryOp):
            left, kl = self.walk(node.left, False, negated)
            right, kr = self.walk(node.right, False, negated)
            node, keys = replace(node, left=left, right=right), kl | kr
        elif isinstance(node, Comprehension):
            element, keys = self.walk(node.element, False, negated)
            node = replace(node, element=element)
        elif isinstance(node, FunctionCall) and not _is_aggregate_shorthand(node):
            # (an aggregate shorthand that rule 1 left is not evaluated)
            args = []
            for a in node.args:
                a, k = self.walk(a, False, negated)
                args.append(a)
                keys |= k
            node = replace(node, args=args)
        if pred or (isinstance(node, BinaryOp) and node.op in _COMPARISON_OPS):
            node, k = self.bind(node, negated)
            keys |= k
        return node, keys


def _has_wildcard(node: Any) -> bool:
    """Whether a wildcard or range reference stands in `node` outside the
    domains of its quantifiers and comprehensions."""
    if _wildcard_key(node) is not None:
        return True
    if isinstance(node, ActorRef):
        return node.index is not None and not isinstance(node.index, (str, int)) \
            and _has_wildcard(node.index)
    if isinstance(node, MemberAccess):
        return _has_wildcard(node.obj)
    if isinstance(node, UnaryOp):
        return _has_wildcard(node.operand)
    if isinstance(node, BinaryOp):
        return _has_wildcard(node.left) or _has_wildcard(node.right)
    if isinstance(node, QuantifiedExpr):
        return _has_wildcard(node.predicate)
    if isinstance(node, Comprehension):
        return _has_wildcard(node.element)
    if isinstance(node, FunctionCall):
        return any(_has_wildcard(a) for a in node.args)
    return False


def desugar_wildcards(expr: Any, notes: list | None = None) -> Any:
    """A predicate with its wildcard and range references outside the
    domains of quantifiers and comprehensions rewritten as quantifiers and
    comprehensions (F.5); `expr` itself when it has none. `notes`, when
    given, receives the readings that may surprise (N-15)."""
    if not _has_wildcard(expr):
        return expr
    d = _Desugar(notes)
    return d.walk(d.aggregates(expr), True, False)[0]


class _TraceReader:
    """Reads the observations of one contract instance into a `Trace`, one
    at a time."""

    def __init__(self, trace: Trace, own_version: Any = False) -> None:
        self.trace = trace
        self.snapshot: dict[str, Any] = {}
        self.state: str | None = None
        self.prev_t: float | None = None
        # The key ``version`` in a file that declares no versions of a run
        # (Appendix K, K.11, from cadl 0.3.35): `own_version` is the
        # sos.version of the file (None: it has none). False: the key is
        # not read here (the trace of `cadl agents-run`).
        self.own_version = own_version

    def add(self, rec: Any, where: str) -> Observation:
        """Check one observation and append it to the trace. Nothing is
        changed when it is malformed."""
        trace = self.trace
        if not isinstance(rec, dict):
            raise TraceError(f"{where}: an observation must be a JSON object")
        if "version" in rec and self.own_version is not False:
            named, own = rec["version"], self.own_version
            if own is None or not isinstance(named, str) or named != own:
                got = f"'{named}'" if isinstance(named, str) else "a value that is not a string"
                mine = ("and gives no sos.version" if own is None
                        else f"and has one version, its sos.version '{own}'")
                raise TraceError(
                    f"{where}: 'version' names {got}, but the file declares no versions to "
                    f"switch to (it has no amendment: block, Appendix K, K.11) {mine}")
            rec = {k: v for k, v in rec.items() if k != "version"}
        unknown = sorted(set(rec) - _TRACE_KEYS)
        if unknown:
            raise TraceError(f"{where}: unknown key(s) {', '.join(unknown)}")
        t = _exact(rec.get("t"))
        if isinstance(t, bool) or not isinstance(t, (int, Fraction)):
            raise TraceError(f"{where}: 't' must be a number (seconds)")
        if abs(t) > MAX_T or not math.isfinite(t):
            raise TraceError(f"{where}: 't' must be a finite number of at most "
                             f"2**53 in absolute value")
        prev_t = self.prev_t
        if prev_t is not None and t < prev_t:
            raise TraceError(f"{where}: time {_fmt_t(t)} is before the previous observation "
                             f"({_fmt_t(prev_t)})")
        first = not trace.observations
        for key in ("bindings", "request"):
            if key in rec:
                if not first:
                    raise TraceError(f"{where}: '{key}' is allowed only on the first line")
                if not isinstance(rec[key], dict):
                    raise TraceError(f"{where}: '{key}' must be an object")
        values = rec.get("values", {})
        if not isinstance(values, dict):
            raise TraceError(f"{where}: 'values' must be an object")
        messages = rec.get("messages", [])
        if not isinstance(messages, list):
            raise TraceError(f"{where}: 'messages' must be a list")
        msgs = []
        for m in messages:
            if not isinstance(m, dict) or set(m) - _MESSAGE_KEYS:
                raise TraceError(f"{where}: a message is an object with from, to, name and optional args")
            for key in ("from", "to", "name"):
                if not isinstance(m.get(key), str) or not m[key].strip():
                    raise TraceError(f"{where}: a message needs a string '{key}'")
            for key in ("from", "to"):
                if not _TRACE_ACTOR_RE.fullmatch(m[key]):
                    raise TraceError(
                        f"{where}: '{key}' of a message must be a concrete actor such as "
                        f"PLANNER or ROBOT[2], got {m[key]!r}")
            if not _TRACE_NAME_RE.fullmatch(m["name"]):
                raise TraceError(f"{where}: 'name' of a message must be a message name "
                                 f"(an identifier), got {m['name']!r}")
            args = m.get("args", [])
            if not isinstance(args, list):
                raise TraceError(f"{where}: 'args' of a message must be a list")
            msgs.append({"from": _canon_actor(m["from"]), "to": _canon_actor(m["to"]),
                         "name": m["name"].strip(), "args": _exact(args)})
        if "state" in rec and not isinstance(rec["state"], str):
            raise TraceError(f"{where}: 'state' must be a string")
        end = rec.get("end")
        if end is not None and not isinstance(end, bool):
            raise TraceError(f"{where}: 'end' must be true or false")
        amend = rec.get("amend")
        if amend is not None and (not isinstance(amend, str) or not amend.strip()):
            raise TraceError(f"{where}: 'amend' must be the path of a CADL file")
        # The observation is well-formed: from here on the reader changes.
        self.prev_t = t
        for key in ("bindings", "request"):
            if key in rec:
                setattr(trace, key, _exact(rec[key]))
        for k, v in values.items():
            self.snapshot[_canon_actor(k)] = _exact(v)
        before = self.state
        if "state" in rec:
            self.state = rec["state"]
        obs = Observation(
            t=Fraction(t), values=dict(self.snapshot), messages=msgs,
            state=before, state_after=self.state, end=end, amend=amend,
        )
        trace.observations.append(obs)
        return obs


def parse_trace(lines, path: str | None = None, own_version: Any = False) -> Trace:
    """Read a trace from an iterable of JSON Lines. The lifecycle state is
    left as written; `evaluate` fills in the initial state. `own_version`:
    see `_TraceReader`."""
    trace = Trace(observations=[], path=path)
    reader = _TraceReader(trace, own_version)
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        where = f"line {lineno}"
        try:
            rec = loads_strict(line)
        except NumberRangeError as e:
            raise TraceError(f"{where}: {e}") from None
        except json.JSONDecodeError as e:
            raise TraceError(f"{where}: not JSON ({e.msg})") from None
        except ValueError as e:
            raise TraceError(f"{where}: not JSON ({e})") from None
        reader.add(rec, where)
    if not trace.observations:
        raise TraceError("the trace has no observation")
    return trace


def load_trace(path, own_version: Any = False) -> Trace:
    with open(path, encoding="utf-8") as f:
        return parse_trace(f, path=str(path), own_version=own_version)


def _fmt_t(t: Any) -> str:
    return "-" if t is None else f"{float(t):g}"


def _fmt_d(ms: int | None) -> str:
    return "none" if ms is None else f"{ms / 1000:g}s"


def _targets_of(contract: ContractDef) -> set[str]:
    """Norms that only a violation or a power activates (F.5, item 1)."""
    targets: set[str] = set()
    for d in contract.norms:
        if d.on_violation and d.on_violation.reparation:
            targets.add(d.on_violation.reparation)
        if d.effects:
            targets.update(d.effects.activate)
    return targets


def _definition(d: NormDef) -> NormDef:
    """A norm definition without its source location, for comparison."""
    return replace(d, loc=None)


# === Predicate evaluation =================================================

class _Evaluator:
    """A.10 predicates and the temporal operators of F.4 over the trace."""

    def __init__(self, trace: Trace, has_lifecycle: bool, warnings: list[str]) -> None:
        self.obs = trace.observations
        self.bindings = trace.bindings
        self.request = trace.request
        self.has_lifecycle = has_lifecycle
        self.warnings = warnings
        self._warned: set[str] = set()
        self.known = set()
        for o in self.obs:
            self.known.update(o.values)
        self._cache: dict[tuple, tuple[Any, Any]] = {}
        # Declared actors (`declared_actors`); None when they are not known.
        self.actors: dict[str, tuple] | None = None
        # The variables of the quantifiers and comprehensions being
        # evaluated: name -> (set or None, index).
        self._scope: dict[str, tuple] = {}
        # Observation index -> (observation, set -> indices of the members
        # named up to it); see `known_members`.
        self._members: dict[int, tuple] = {}
        # (set, index variable) of the references `A[i]` of the contract.
        self.bound_refs: set = set()

    def is_member(self, name: str, index: str | None) -> bool:
        """Whether `name` (index None) or `name[index]` is a declared actor
        or a member of a declared set, within its integer bounds."""
        if self.actors is None:
            return True
        decl = self.actors.get(name)
        if decl is None:
            return False
        indexed, lo, hi = decl[:3]
        if not indexed or index is None:
            return not indexed and index is None
        if not index.isdigit():
            return False
        return (lo is None or lo <= int(index)) and (hi is None or int(index) <= hi)

    def warn(self, text: str) -> None:
        if text not in self._warned:
            self._warned.add(text)
            self.warnings.append(text)

    # --- static -----------------------------------------------------------

    @staticmethod
    def unsupported(expr: Any, temporal: bool, bound: dict | None = None,
                    actors: dict | None = None) -> str | None:
        """Why an expression cannot be evaluated, or None. `bound` maps the
        variables of the enclosing quantifiers and comprehensions to their
        set (None for a range of integers). With `actors` (`declared_actors`),
        the domain of a quantifier or comprehension and an actor indexed by
        such a variable must name a declared set."""
        bound = bound or {}
        if isinstance(expr, (IntLiteral, FloatLiteral)) and not _literal_in_range(expr):
            return f"the number is outside the range of exact numbers ({_RANGE_TEXT})"
        if isinstance(expr, (IntLiteral, FloatLiteral, BoolLiteral, StringLiteral, DurationLiteral)):
            return None
        if isinstance(expr, ActorRef):
            if expr.index == "*" or isinstance(expr.index, RangeExpr):
                return "a wildcard or range actor reference is not evaluable"
            if actors is not None and expr.index is not None \
                    and _uses_variable(expr.index, bound) and not _is_set(expr.name, actors):
                return _not_a_set(expr.name, actors)
            if expr.index is not None and not isinstance(expr.index, (str, int)):
                return _Evaluator.unsupported(expr.index, False, bound, actors)
            return None
        if isinstance(expr, MemberAccess):
            obj = expr.obj
            if obj.index is None and obj.name in bound and bound[obj.name] is None:
                return (f"'{obj.name}' ranges over integers, not over the members of a "
                        f"set, so '{obj.name}.{expr.member}' is not evaluable")
            return _Evaluator.unsupported(obj, False, bound, actors)
        if isinstance(expr, UnaryOp):
            return _Evaluator.unsupported(expr.operand, temporal, bound, actors)
        if isinstance(expr, BinaryOp):
            if expr.op in ("+", "-", "*", "/") and any(
                    isinstance(x, (StringLiteral, BoolLiteral)) for x in (expr.left, expr.right)):
                return (f"'{expr.op}' on a text or a truth value is not defined (F.5), "
                        f"so the expression never has a value")
            if expr.op == "/" and isinstance(expr.right, (IntLiteral, FloatLiteral)) \
                    and expr.right.value == 0:
                return "a division by the literal 0 never has a value (F.5)"
            return (_Evaluator.unsupported(expr.left, temporal, bound, actors)
                    or _Evaluator.unsupported(expr.right, temporal, bound, actors))
        if isinstance(expr, QuantifiedExpr):
            return (_Evaluator._domain_error(expr.domain, False, actors)
                    or _Evaluator.unsupported(expr.predicate, temporal,
                                              {**bound, expr.variable: expr.domain.name},
                                              actors))
        if isinstance(expr, FunctionCall):
            if expr.name == "__in__":
                for a in expr.args:
                    r = _Evaluator.unsupported(a, False, bound, actors)
                    if r:
                        return r
                return None
            if expr.name in TEMPORAL_OPERATORS:
                if not temporal:
                    return f"the temporal operator {expr.name}() is not allowed here"
                nargs, dpos = TEMPORAL_OPERATORS[expr.name]
                if len(expr.args) != nargs or not isinstance(expr.args[dpos], DurationLiteral):
                    return f"{expr.name}() does not have the arguments of F.4"
                for i, a in enumerate(expr.args):
                    if i != dpos:
                        r = _Evaluator.unsupported(a, True, bound, actors)
                        if r:
                            return r
                return None
            if (expr.name in AGGREGATES and len(expr.args) == 1
                    and isinstance(expr.args[0], Comprehension)):
                comp = expr.args[0]
                dom = comp.domain
                return (_Evaluator._domain_error(dom, True, actors)
                        or _Evaluator.unsupported(comp.element, False, {
                            **bound,
                            comp.variable: None if isinstance(dom, RangeExpr) else dom.name},
                            actors))
            if _is_aggregate_shorthand(expr):
                keys: list = []
                _wildcards(expr.args[0], keys, False)
                keys = list(dict.fromkeys(keys))
                if len(keys) > 1:
                    # what desugar_wildcards leaves (F.5, rule 1)
                    return (f"the argument of {expr.name}() refers to "
                            f"{' and '.join(wildcard_text(k) for k in keys)}; "
                            f"an aggregate ranges over one set or range")
            return f"the function call {expr.name}() is not evaluable"
        return f"{type(expr).__name__} is not evaluable"

    @staticmethod
    def _domain_error(dom: Any, comprehension: bool, actors: dict | None = None) -> str | None:
        """Why the domain of a quantifier or comprehension is not one the
        engine evaluates (a set `A`, `A[*]` or `A[lo..hi]`, or for a
        comprehension also a range `lo..hi`), or None. With `actors`, `A`
        must be a declared set."""
        if comprehension and isinstance(dom, RangeExpr):
            return None
        if isinstance(dom, ActorRef) and (dom.index is None or dom.index == "*"
                                          or isinstance(dom.index, RangeExpr)):
            if actors is not None and not _is_set(dom.name, actors):
                return _not_a_set(dom.name, actors)
            return None
        return ("the domain of a quantifier or a comprehension must be a set of actors "
                "(A, A[*] or A[lo..hi]), or for a comprehension a range lo..hi")

    # --- names ------------------------------------------------------------

    def actor(self, ref: ActorRef, k: int) -> tuple[str, bool]:
        """An actor reference with its index bound: (text, bound). When the
        index is a name the instance does not bind, it is kept as written
        and `bound` is False."""
        idx = ref.index
        if idx is None:
            return ref.name, True
        if isinstance(idx, int):
            return f"{ref.name}[{idx}]", True
        if isinstance(idx, str) or (isinstance(idx, ActorRef) and idx.index is None):
            name = idx if isinstance(idx, str) else idx.name
            if name.isdigit():
                return f"{ref.name}[{name}]", True
            if name in self._scope:
                return f"{ref.name}[{self._scope[name][1]}]", True
            if name in self.bindings:
                return f"{ref.name}[{self.bindings[name]}]", True
            self.warn(f"the index '{name}' of {ref.name}[{name}] is not bound by the trace "
                      f"('bindings'); it matches any {ref.name}")
            return f"{ref.name}[{name}]", False
        v = self.value(idx, k, k)
        if v is UNKNOWN or v is UNDECIDED:
            return f"{ref.name}[?]", False
        if _integral(v):
            v = int(v)
        elif isinstance(v, Fraction):
            v = float(v)
        return f"{ref.name}[{v}]", True

    def _lookup(self, key: str, k: int, stripped: str | None = None) -> Any:
        values = self.obs[k].values
        for name in (key, stripped):
            if name is not None and name in values:
                v = values[name]
                return UNKNOWN if v is None else v
        if key not in self.known and (stripped is None or stripped not in self.known):
            self.warn(f"the trace has no value for '{key}'; predicates that use it do not hold")
        return UNKNOWN

    def _name(self, name: str, k: int) -> Any:
        if name in self._scope:
            return self._scope[name][1]
        if name in self.bindings:
            return self.bindings[name]
        if name == "now":
            return self.obs[k].t
        if name == "state" and self.has_lifecycle:
            s = self.obs[k].state
            return UNKNOWN if s is None else s
        if name in self.known:
            return self._lookup(name, k)
        return _Sym(name)

    # --- values -----------------------------------------------------------

    def value(self, expr: Any, k: int, n: int) -> Any:
        """The value of `expr` at observation k, given observations 0..n."""
        if isinstance(expr, FloatLiteral):
            return _literal_number(expr)
        if isinstance(expr, (IntLiteral, BoolLiteral, StringLiteral)):
            return expr.value
        if isinstance(expr, DurationLiteral):
            return _duration(expr)
        if isinstance(expr, ActorRef):
            if expr.index is None:
                return self._name(expr.name, k)
            return _Sym(self.actor(expr, k)[0])
        if isinstance(expr, MemberAccess):
            obj = expr.obj
            if obj.index is None and obj.name == "request":
                cur: Any = self.request
                for part in expr.member.split("."):
                    if not isinstance(cur, dict) or part not in cur:
                        self.warn(f"the trace has no value for 'request.{expr.member}'; "
                                  f"predicates that use it do not hold")
                        return UNKNOWN
                    cur = cur[part]
                return UNKNOWN if cur is None else cur
            if obj.index is None and obj.name in self._scope:
                # `r.load` for a variable r over a set: the member's quantity
                group, index = self._scope[obj.name]
                return self._lookup(f"{group}[{index}].{expr.member}", k,
                                    f"{group}.{expr.member}")
            text, _ = self.actor(obj, k)
            if self._scope and self._uses_scope(obj.index):
                # `A[x+1]`: the member with that index, and no member when
                # the index lies outside the set (F.5)
                m = _ACTOR_RE.match(text)
                if not m or not self.is_member(obj.name, m.group(2)):
                    return UNKNOWN
            stripped = f"{obj.name}.{expr.member}" if obj.index is not None else None
            return self._lookup(f"{text}.{expr.member}", k, stripped)
        if isinstance(expr, UnaryOp):
            v = self.truth(self.value(expr.operand, k, n), expr.operand)
            return v if isinstance(v, _Marker) else (not v)
        if isinstance(expr, BinaryOp):
            return self._binary(expr, k, n)
        if isinstance(expr, QuantifiedExpr):
            return self._quantified(expr, k, n)
        if isinstance(expr, FunctionCall) and expr.name in AGGREGATES and expr.args \
                and isinstance(expr.args[0], Comprehension):
            return self._aggregate(expr.name, expr.args[0], k, n)
        if isinstance(expr, FunctionCall) and expr.name in TEMPORAL_OPERATORS:
            key = (id(expr), k)
            if self._scope:      # inside a quantifier: the value depends on its variables
                key += tuple(sorted(self._scope.items()))
            if key in self._cache:
                return self._cache[key][1]
            v = self._temporal(expr, k, n)
            if v is not UNDECIDED:
                self._cache[key] = (expr, v)     # keeps expr alive: its id stays unique
            return v
        raise _NotEvaluable(self.unsupported(expr, True) or "not evaluable")

    def truth(self, v: Any, expr: Any = None) -> Any:
        """A value as a truth value: True, False, UNKNOWN or UNDECIDED."""
        if isinstance(v, (_Marker, bool)):
            return v
        if isinstance(v, _Sym):
            return self._no_value(v)
        if isinstance(v, _NUMBER):
            return bool(v)
        self.warn(f"the value {_show(v)} is used as a condition but is not a boolean")
        return UNKNOWN

    def _binary(self, expr: BinaryOp, k: int, n: int) -> Any:
        op = expr.op
        if op in ("AND", "OR"):
            stop = op == "OR"
            left = self.truth(self.value(expr.left, k, n), expr.left)
            if left is stop:
                return stop
            right = self.truth(self.value(expr.right, k, n), expr.right)
            if right is stop:
                return stop
            for marker in (UNDECIDED, UNKNOWN):
                if left is marker or right is marker:
                    return marker
            return not stop
        refined = self._monotone(expr, k, n)
        if refined is not None:
            return refined
        left = self.value(expr.left, k, n)
        if (op == "==" and isinstance(expr.right, FunctionCall)
                and expr.right.name == "__in__"):
            items = [self.value(a, k, n) for a in expr.right.args]
            if isinstance(left, _Marker):
                return left
            if isinstance(left, _Sym):
                return self._no_value(left)
            if not isinstance(left, str):
                # a valueless name is an enum literal only against a string
                items = [self._no_value(i) if isinstance(i, _Sym) else i for i in items]
            if any(not isinstance(i, _Marker) and self._equal(left, i) for i in items):
                return True
            for marker in (UNDECIDED, UNKNOWN):
                if any(i is marker for i in items):
                    return marker
            return False
        right = self.value(expr.right, k, n)
        for marker in (UNKNOWN, UNDECIDED):
            if left is marker or right is marker:
                return marker
        # A name without a value is an enum-like literal only when it is
        # compared (==, !=, IN) with a string value; anywhere else it is an
        # undefined quantity.
        for side, other in ((left, right), (right, left)):
            if isinstance(side, _Sym) and not (
                    op in ("==", "!=") and isinstance(other, str) and not isinstance(other, _Sym)):
                if isinstance(other, _Sym):
                    self._no_value(other)
                return self._no_value(side)
        if op == "==":
            return self._equal(left, right)
        if op == "!=":
            return not self._equal(left, right)
        try:
            if op == "<":
                return left < right
            if op == "<=":
                return left <= right
            if op == ">":
                return left > right
            if op == ">=":
                return left >= right
            # arithmetic is defined on numbers only (F.5): a text, a truth
            # value, a list or an object gives no value (Python would
            # concatenate or repeat lists)
            if not all(isinstance(v, _NUMBER) and not isinstance(v, bool) for v in (left, right)):
                raise TypeError
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                return Fraction(left) / right
        except (TypeError, ZeroDivisionError):
            self.warn(f"'{op}' cannot be applied to {_show(left)} and {_show(right)}; the predicate does not hold")
            return UNKNOWN
        raise _NotEvaluable(f"the operator {op} is not evaluable")

    def _no_value(self, name: str) -> Any:
        self.warn(f"the trace has no value for '{name}'; predicates that use it do not hold")
        return UNKNOWN

    @staticmethod
    def _equal(a: Any, b: Any) -> bool:
        if isinstance(a, bool) != isinstance(b, bool):
            return False
        return a == b

    def holds(self, expr: Any, k: int, n: int) -> Any:
        return self.truth(self.value(expr, k, n), expr)

    # --- quantifiers and aggregates (F.5) -------------------------------------

    def known_members(self, k: int) -> dict:
        """set -> indices of the members that the instance has named at or
        before observation k: in the key of a value (`A[3].x`), as the
        sender or receiver of a message, or by a binding `i` = v of an
        index that the contract writes as `A[i]`. Later observations are
        not used (F.5)."""
        start = k
        while start >= 0 and not (start in self._members
                                  and self._members[start][0] is self.obs[start]):
            start -= 1
        found: dict = {} if start < 0 else {g: set(v) for g, v in self._members[start][1].items()}
        if start < 0:
            for group, var in self.bound_refs:
                v = self.bindings.get(var)
                if _integral(v):
                    v = int(v)
                if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                    found.setdefault(group, set()).add(v)
        for j in range(start + 1, k + 1):
            o = self.obs[j]
            names = list(o.values) + [a for m in o.messages for a in (m["from"], m["to"])]
            for name in names:
                m = _MEMBER_KEY_RE.match(name)
                if m:
                    found.setdefault(m.group(1), set()).add(int(m.group(2)))
            self._members[j] = (o, {g: frozenset(v) for g, v in found.items()})
        return self._members[k][1] if k >= 0 else {}

    def _bound_value(self, name: str, k: int) -> int | None:
        """The value of a symbolic bound at observation k: a binding of the
        instance or an observed quantity with an integer value; None when
        it has none. A bound is read outside every quantifier: a variable
        of the same name does not hide it."""
        scope, self._scope = self._scope, {}
        try:
            v = self._name(name, k)
        finally:
            self._scope = scope
        if _integral(v):
            v = int(v)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        if not isinstance(v, (_Marker, _Sym)):
            self.warn(f"the bound '{name}' has the value {_show(v)}, which is not an integer; "
                      f"it is treated as a bound without a value")
        return None

    def domain(self, dom: Any, k: int) -> Any:
        """The domain of a quantifier or comprehension at observation k:
        (set or None, indices in increasing order, closed), or UNKNOWN.
        A domain that is not closed is open: it holds the members known so
        far, and further members may exist. A domain is closed when every
        bound has an integer value, no member beyond the value of the
        declared symbolic bound of the set has been named, and it has at
        most MAX_DOMAIN members."""
        declared = None                 # the symbolic upper bound of the declaration
        if isinstance(dom, RangeExpr):
            group, lo, uppers = None, dom.start, [dom.end]
        else:
            group, lo, uppers = dom.name, None, []
            if self.actors is not None:
                decl = self.actors.get(group)
                if decl is None or not decl[0]:
                    self.warn(f"'{group}' is not a declared set of actors; a quantifier or "
                              f"aggregate over it is undefined")
                    return UNKNOWN
                lo = decl[1]
                if decl[2] is not None:
                    uppers.append(decl[2])
                elif decl[3] is not None:
                    declared = decl[3]
            if isinstance(dom.index, RangeExpr):
                lo = dom.index.start if lo is None else max(lo, dom.index.start)
                uppers.append(dom.index.end)
        hi, unknown = None, False
        for b in uppers:
            if isinstance(b, str):
                b = self._bound_value(b, k)
                unknown = unknown or b is None
            if isinstance(b, int):
                hi = b if hi is None else min(hi, b)
        start = 0 if lo is None else lo
        known = None
        if declared is not None:
            value = self._bound_value(declared, k)
            if value is None:
                unknown = True
            else:
                known = self.known_members(k).get(group, ())
                beyond = sorted(i for i in known if i > value and i >= start)
                if beyond:
                    # F.3 makes every index from the lower bound on a member:
                    # a named member beyond the bound keeps the domain open.
                    self.warn(f"{group}[{beyond[0]}] is named but the bound '{declared}' of "
                              f"{group} is {value}; the domain of {group} stays open")
                    unknown = True
                else:
                    hi = value if hi is None else min(hi, value)
        if not unknown and hi is not None:
            if hi - start + 1 <= MAX_DOMAIN:
                return group, list(range(start, hi + 1)), True
            self.warn(f"the domain {group or 'range'} [{start}..{hi}] has "
                      f"{hi - start + 1} members, more than {MAX_DOMAIN}; it is evaluated "
                      f"as an open domain (known members only)")
        if group is None:
            return None, [], False
        if known is None:
            known = self.known_members(k).get(group, ())
        return group, sorted(i for i in known
                             if i >= start and (hi is None or i <= hi)), False

    def _uses_scope(self, node: Any) -> bool:
        """Whether an index expression uses a variable of a quantifier or
        comprehension being evaluated."""
        if isinstance(node, str):
            return node in self._scope
        if isinstance(node, ActorRef):
            return (node.index is None and node.name in self._scope) or (
                node.index is not None and self._uses_scope(node.index))
        if isinstance(node, MemberAccess):
            return self._uses_scope(node.obj)
        if isinstance(node, UnaryOp):
            return self._uses_scope(node.operand)
        if isinstance(node, BinaryOp):
            return self._uses_scope(node.left) or self._uses_scope(node.right)
        if isinstance(node, FunctionCall):
            return any(self._uses_scope(a) for a in node.args)
        return False

    def _with(self, var: str, member: tuple, fn: Any) -> Any:
        """fn() with the variable `var` bound to `member` (set, index);
        an inner variable of the same name hides the outer one."""
        had = var in self._scope
        old = self._scope.get(var)
        self._scope[var] = member
        try:
            return fn()
        finally:
            if had:
                self._scope[var] = old
            else:
                del self._scope[var]

    def _open_domain(self, group: str) -> None:
        self.warn(f"the domain of {group} is open: members that the trace has not named yet "
                  f"may exist, so a quantifier over it that no known member decides, and an "
                  f"aggregate over it, are undefined")

    def _quantified(self, q: QuantifiedExpr, k: int, n: int) -> Any:
        """Kleene's for all / exists over the domain at observation k; on an
        open domain only a known member decides, and otherwise the value is
        undefined."""
        dom = self.domain(q.domain, k)
        if dom is UNKNOWN:
            return UNKNOWN
        group, members, closed = dom
        decisive = q.quantifier == "exists"   # True decides exists, False for all
        vals = []
        for index in members:
            v = self._with(q.variable, (group, index),
                           lambda: self.holds(q.predicate, k, n))
            if v is decisive:
                return decisive
            vals.append(v)
        worst = self._worst(*vals)
        if worst is not None:
            return worst
        if not closed:
            self._open_domain(group)
            return UNKNOWN
        return not decisive

    def _elements(self, name: str, comp: Comprehension, group: Any, members: list,
                  k: int, n: int) -> tuple:
        """The values of the element of an aggregate for the members of its
        domain: (values, markers, invalid). For `count` a value is a truth
        value; for the others a number. An undefined element is a marker.
        `invalid` is True when an element has a value of the wrong kind (a
        text or symbolic value; for min, max, sum and avg also a boolean),
        which makes the aggregate undefined (F.5), with a warning."""
        vals, markers, invalid = [], [], False
        for index in members:
            v = self._with(comp.variable, (group, index),
                           lambda: self.value(comp.element, k, n))
            if name == "count":
                if not isinstance(v, (_Marker, bool) + _NUMBER) or isinstance(v, _Sym):
                    invalid = True
                v = self.truth(v, comp.element)
            elif isinstance(v, _Sym):
                invalid = True
                v = self._no_value(v)
            elif not isinstance(v, _Marker) and (isinstance(v, bool)
                                                  or not isinstance(v, _NUMBER)):
                self.warn(f"{name}() of the value {_show(v)}, which is not a number; "
                          f"the predicate does not hold")
                invalid = True
                v = UNKNOWN
            if isinstance(v, _Marker):
                markers.append(v)
            else:
                vals.append(v)
        return vals, markers, invalid

    def _aggregate(self, name: str, comp: Comprehension, k: int, n: int) -> Any:
        """sum, count, min, max or avg of a comprehension at observation k;
        undefined on an open domain or when an element is undefined.
        `count` counts the members for which the element is true."""
        dom = self.domain(comp.domain, k)
        if dom is UNKNOWN:
            return UNKNOWN
        group, members, closed = dom
        if not closed:
            if group is not None:
                self._open_domain(group)
            else:
                self.warn(f"the range of '{comp.variable}' in {name}() has a bound without "
                          f"a value; the aggregate is undefined")
            return UNKNOWN
        vals, markers, _ = self._elements(name, comp, group, members, k, n)
        worst = self._worst(*markers)
        if worst is not None:
            return worst
        if name == "count":
            return sum(1 for v in vals if v is True)
        if name == "sum":
            return sum(vals)
        if not vals:
            self.warn(f"{name}() over an empty domain has no value; the predicate does not hold")
            return UNKNOWN
        if name == "min":
            return min(vals)
        if name == "max":
            return max(vals)
        return Fraction(sum(vals)) / len(vals)

    def _monotone(self, expr: BinaryOp, k: int, n: int) -> Any:
        """A comparison of `count`, `min` or `max` of a comprehension with
        an operand that binds no variable, decided from the interval of
        values that the aggregate can take (F.5): undefined elements, and
        on an open domain the members not yet named, may take any value.
        count lies in [true, true + undefined] (no upper bound when open),
        max in [largest defined value, +inf), min in (-inf, smallest
        defined value]. True or False when every value of the interval
        gives it; None (evaluate as usual) otherwise, and when the domain
        is closed and every element is defined (the exact value)."""
        found = _monotone_operands(expr)
        if found is None:
            return None
        call, other, op = found
        comp = call.args[0]
        dom = self.domain(comp.domain, k)
        if dom is UNKNOWN:
            return None
        group, members, closed = dom
        if group is None and not closed:
            return None                     # a range whose bound has no value
        b = self.value(other, k, n)
        if isinstance(b, bool) or not isinstance(b, _NUMBER):
            return None
        vals, markers, invalid = self._elements(call.name, comp, group, members, k, n)
        if invalid or any(m is UNDECIDED for m in markers) or (closed and not markers):
            return None         # a value of the wrong kind: undefined, as F.5 says
        inf = float("inf")
        if call.name == "count":
            lo = sum(1 for v in vals if v is True)
            hi = lo + len(markers) if closed else inf
            return _interval_decides(op, lo, hi, b, True)
        if not vals:
            return None
        if call.name == "max":
            return _interval_decides(op, max(vals), inf, b, False)
        return _interval_decides(op, -inf, min(vals), b, False)

    # --- temporal operators (F.4) -------------------------------------------

    def _window(self, k: int, n: int, d: float) -> tuple[range, bool]:
        """The observations of [t_k, t_k + d] among 0..n, and whether the
        interval is known to be over (an observation after it exists)."""
        limit = self.obs[k].t + d
        j = k
        while j <= n and self.obs[j].t <= limit:
            j += 1
        return range(k, j), j <= n

    @staticmethod
    def _worst(*vals: Any) -> Any:
        if any(v is UNDECIDED for v in vals):
            return UNDECIDED
        if any(v is UNKNOWN for v in vals):
            return UNKNOWN
        return None

    def _temporal(self, call: FunctionCall, k: int, n: int) -> Any:
        if call.name == "until":
            return self._until(call, k, n)
        d = self.value(call.args[0], k, n)
        window, over = self._window(k, n, d)
        vals = [self.holds(call.args[1], j, n) for j in window]
        decisive = call.name == "within"      # a True decides within, a False throughout
        if any(v is decisive for v in vals):
            return decisive
        if not over:
            return UNDECIDED
        worst = self._worst(*vals)
        return (not decisive) if worst is None else worst

    def _until(self, call: FunctionCall, k: int, n: int) -> Any:
        p, q = call.args[0], call.args[1]
        window, over = self._window(k, n, self.value(call.args[2], k, n))
        uncertain: Any = None
        prefix: Any = True                   # p on [t_k, t_j): True, UNKNOWN or UNDECIDED
        broken = False
        for j in window:
            qv = self.holds(q, j, n)
            if qv is True:
                if prefix is True:
                    return True
                uncertain = self._worst(uncertain, prefix)
            elif qv is not False:
                uncertain = self._worst(uncertain, qv, prefix)
            pv = self.holds(p, j, n)
            if pv is False:
                broken = True
                break
            if pv is not True:
                prefix = self._worst(prefix, pv)
        if not broken and not over:
            return UNDECIDED
        return False if uncertain is None else uncertain


# === Events, contents and conditions ======================================

class _Term:
    """A trigger, until, content, when or if: an institutional event, a
    message event or a predicate (F.3)."""

    def __init__(self, text: str, events: set[str], ev: _Evaluator, temporal: bool = False) -> None:
        self.text = text.strip()
        self.ev = ev
        self.error: str | None = None
        self.expr: Any = None
        self._edge: dict[int, bool] = {}
        m = _MESSAGE_EVENT_RE.match(self.text) if "->" in self.text else None
        if self.text in events and _IDENTIFIER_RE.fullmatch(self.text):
            self.kind = "event"
        elif m:
            self.kind = "message"
            self.sender = _parse_actor(m.group(1).strip())
            self.receiver = _parse_actor(m.group(2).strip())
            self.msg_name = m.group(3).strip()
            self.msg_args: list | None = None
            try:
                msg = _parse_expr(self.msg_name)
            except Exception:
                msg = None
            if isinstance(msg, FunctionCall):
                self.msg_name = msg.name
                self.msg_args = msg.args or None
                for a in msg.args:
                    self.error = self.error or ev.unsupported(a, False, None, ev.actors)
            elif not (isinstance(msg, ActorRef) and msg.index is None):
                # not a name or a call (E.3): it would never occur (N-16)
                hint = syntax_hint(self.msg_name)
                self.error = ("the message is not a name or a call name(args) (E.3)"
                              + (f": {hint}" if hint else ""))
            # A range with integer bounds matches its members only; one with
            # a symbolic bound matches every member of the set.
            self._ranges: dict[int, tuple[int, int]] = {}
            for ref in (self.sender, self.receiver):
                if isinstance(ref.index, RangeExpr):
                    if isinstance(ref.index.start, int) and isinstance(ref.index.end, int):
                        self._ranges[id(ref)] = (ref.index.start, ref.index.end)
                    ref.index = "*"
        else:
            self.kind = "predicate"
            try:
                self.expr = desugar_wildcards(_parse_rule(self.text))
            except Exception:
                hint = syntax_hint(self.text)
                self.error = ("it is not a predicate (Appendix A.10)"
                              + (f": {hint}" if hint else ""))
            else:
                self.error = ev.unsupported(self.expr, temporal, None, ev.actors)

    # --- messages ---------------------------------------------------------

    def _actor_matches(self, ref: ActorRef, actual: str, k: int) -> bool:
        """An actor reference against a concrete actor: `A` matches the
        actor A, `A[3]` the member A[3], `A[*]` every member of the declared
        set A, `A[1..3]` the members A[1] to A[3], and `A[i]` the member the
        instance binds, or any member when it binds none. An actor that is
        not declared, or lies outside the bounds of its set, never matches."""
        m = _ACTOR_RE.match(actual)
        if not m or m.group(1) != ref.name or not self.ev.is_member(ref.name, m.group(2)):
            return False
        index = m.group(2)
        if ref.index == "*":
            bounds = self._ranges.get(id(ref))
            if index is None:
                return self.ev.actors is None and bounds is None
            return bounds is None or (index.isdigit()
                                      and bounds[0] <= int(index) <= bounds[1])
        text, bound = self.ev.actor(ref, k)
        if bound:
            return actual == text
        return True

    def matching_messages(self, k: int) -> list[dict]:
        out = []
        for msg in self.ev.obs[k].messages:
            if msg["name"] != self.msg_name:
                continue
            if not (self._actor_matches(self.sender, msg["from"], k)
                    and self._actor_matches(self.receiver, msg["to"], k)):
                continue
            if self.msg_args is not None:
                want = [self.ev.value(a, k, k) for a in self.msg_args]
                if len(want) != len(msg["args"]) or any(
                    isinstance(w, _Marker) or not _Evaluator._equal(w, a)
                    for w, a in zip(want, msg["args"])
                ):
                    continue
            out.append(msg)
        return out

    # --- occurrence ---------------------------------------------------------

    def _becomes_true(self, k: int) -> bool:
        """Whether a predicate became true at observation k: true at k, and
        false (or undefined) at the previous observation of the instance,
        or k is its first observation (F.5). This does not depend on when
        the norm that uses it was activated."""
        if k not in self._edge:
            self._edge[k] = (self.ev.holds(self.expr, k, k) is True
                             and (k == 0 or self.ev.holds(self.expr, k - 1, k - 1) is not True))
        return self._edge[k]

    def occurs(self, k: int, created: list[dict]) -> bool:
        """Whether the event occurs at observation k (a trigger, an until,
        a when, or a content that is an event)."""
        if self.error:
            return False
        if self.kind == "event":
            return any(e["name"] == self.text for e in created)
        if self.kind == "message":
            return bool(self.matching_messages(k))
        return self._becomes_true(k)

    def content(self, k: int, n: int, created: list[dict]) -> Any:
        """The value of a content at observation k, given observations
        0..n. An event or a message is decided at its own observation."""
        if self.kind == "predicate":
            v = self.ev.holds(self.expr, k, n)
            return False if v is UNKNOWN else v
        return self.occurs(k, created) if k == n else False


class _NormInst:
    def __init__(self, d: NormDef, events: set[str], ev: _Evaluator) -> None:
        self.d = d
        self.state = "inactive"
        self.verdict = "pending"
        self.trigger = _Term(d.trigger, events, ev) if d.trigger else None
        self.until = _Term(d.until, events, ev) if d.until else None
        self.content = _Term(d.content, events, ev, temporal=True)
        self.deadline = _deadline(d)
        self.frozen: str | None = None
        bad = getattr(d, "invalid_deadline", None)
        if bad is not None:
            hint = duration_hint(str(bad))
            self.frozen = (f"deadline '{bad}' is not a duration (N-4)"
                           + (f": {hint}" if hint else ""))
        for key, term in (("trigger", self.trigger), ("until", self.until), ("content", self.content)):
            if term is not None and term.error and not self.frozen:
                self.frozen = f"not evaluable: {key} '{term.text}': {term.error}"
        self.activated_at: float | None = None
        self.activated_index: int | None = None
        self.active_since: float | None = None
        self.elapsed_before: Any = 0       # active time before the current stretch
        self.elapsed_start: Any = 0        # active time counted at activation (transform)
        self.judge_from = 0
        self.candidates: list[int] = []    # observations whose content is undecided
        self.time: float | None = None
        self.reason = self.frozen or "not activated"
        self.history: list[dict] = []
        bearer_ref = None
        if d.bearer:
            bearer_ref = _parse_actor(d.bearer)
        self.bearer_ref = bearer_ref

    def elapsed(self, t: float) -> float:
        if self.state == "active" and self.active_since is not None:
            return self.elapsed_before + (t - self.active_since)
        return self.elapsed_before

    def closed_at(self, t: float) -> Any:
        """The time at which the active time reached the deadline, from the
        stretches of the history (also those before an amendment and a
        suspension); None if it has not."""
        acc, start = self.elapsed_start, None
        if acc >= self.deadline:
            first = next((h["t"] for h in self.history if h["state"] == "active"), None)
            return first
        for h in self.history + [{"t": t, "from": self.state, "state": None}]:
            if start is not None and h["from"] == "active" and h["state"] != "active":
                if acc + (h["t"] - start) >= self.deadline:
                    return start + (self.deadline - acc)
                acc += h["t"] - start
                start = None
            if h["state"] == "active" and start is None:
                start = h["t"]
        return None


# === The engine ============================================================

class _Engine:
    def __init__(self, contract: ContractDef, trace: Trace, loader=None,
                 actors: dict | None = None) -> None:
        self.contract = contract
        self.loader = loader
        self.trace = trace
        self.obs = trace.observations
        self.warnings: list[str] = []
        lc = contract.lifecycle
        self.lifecycle = lc
        self._init_states()
        self.ev = _Evaluator(trace, lc is not None, self.warnings)
        self.ev.actors = actors
        self.ev.bound_refs = _bound_refs(contract)
        self.events = {r.creates for r in contract.counts_as if r.creates}
        self.norms = [self._norm(d) for d in contract.norms]
        self.by_id = {n.d.id: n for n in self.norms}
        self._build_rules()
        for n in self.norms:
            self._warn_frozen(n)
        self.targets = _targets_of(contract)
        self.items = {
            "assume": [self._item(e, 0) for e in contract.assume],
            "guarantee": [self._item(e, 0) for e in contract.guarantee],
        }
        self.timeline: list[dict] = []
        self.violations: list[dict] = []
        self.inst_events: list[dict] = []
        self.unauthorised: list[dict] = []
        self.amendments: list[dict] = []
        self._amend_violated: list[_NormInst] = []
        # Observation index -> ids of norms whose content does not count as
        # occurring there. Empty in `cadl norms-eval`; the runtime of the
        # agent governance extension fills it for a released message (G.6).
        self._exempt: dict[int, set[str]] = {}
        self.assumption_failed_at: float | None = None
        self.ended_at: int | None = None
        self.end_reason: str | None = None

    def _norm(self, d: NormDef) -> _NormInst:
        """A new norm instance of the definition `d`."""
        return _NormInst(d, self.events, self.ev)

    def _now(self, n: int) -> Any:
        """The time against which deadlines are measured at observation n."""
        return self.obs[n].t

    def _warn_frozen(self, norm: _NormInst) -> None:
        """A norm that could never be judged is refused, not left inactive:
        `cadl check` reports the same text as an error (N-16), so this is
        reached only through the API with a file that was not checked."""
        if norm.frozen:
            raise NormsEvalError(f"norm '{norm.d.id}' cannot be judged ({norm.frozen}); "
                                 f"cadl check reports it as an error (N-16)")

    def _build_rules(self) -> None:
        self.rules = []
        for r in self.contract.counts_as:
            when = _Term(r.when, set(), self.ev)
            cond = _Term(r.if_, set(), self.ev) if r.if_ else None
            if cond is not None and cond.kind != "predicate":
                cond.error = cond.error or "it is not a predicate"
            for key, term in (("when", when), ("if", cond)):
                if term is not None and term.error:
                    raise NormsEvalError(
                        f"counts_as rule creating '{r.creates}' cannot be evaluated: "
                        f"{key} '{term.text}': {term.error}; cadl check reports it as "
                        f"an error (N-16)")
            self.rules.append((r, when, cond))

    def _init_states(self) -> None:
        lc = self.lifecycle
        if lc is None:
            if any(o.state_after is not None for o in self.obs) and not any(
                    o.amend for o in self.obs):
                self.warnings.append(_STATE_IGNORED)
            return
        self._fill_states(0)

    def _fill_states(self, start: int) -> None:
        """Before the trace first gives a state, it is the initial one."""
        for o in self.obs[start:]:
            if o.state is None:
                o.state = self.lifecycle.initial
            if o.state_after is None:
                o.state_after = self.lifecycle.initial

    def _check_state(self, n: int) -> None:
        o = self.obs[n]
        if self.lifecycle and o.state_after not in self.lifecycle.states:
            raise TraceError(
                f"state '{o.state_after}' at t={_fmt_t(o.t)} is not a state of the "
                f"lifecycle of {self.contract.id}")

    def _item(self, expr: Any, start: int) -> dict:
        text = item_text(expr)
        if isinstance(expr, StringLiteral):
            # kept as text by the parser; it may still be a rule with IN
            try:
                expr = _parse_rule(expr.value)
            except Exception:
                expr = None
        if expr is not None:
            expr = desugar_wildcards(expr)
        temporal = expr is not None and contains_temporal(expr)
        error = ("it is not a predicate (Appendix A.10)" if expr is None
                 else self.ev.unsupported(expr, True))
        origin = ("the start of the contract instance" if start == 0
                  else f"the amendment at t={_fmt_t(self.obs[start].t)}")
        return {"index": 0, "text": text, "expr": expr, "temporal": temporal,
                "start": start, "origin": origin, "closed": False,
                "error": error, "value": None, "time": None, "seen_true": False,
                "undefined": 0, "verdict": "pending",
                "reason": f"not evaluable: {error}" if error else ""}

    # --- records ------------------------------------------------------------

    def _log(self, n: int, kind: str, text: str, **more: Any) -> None:
        self.timeline.append({"t": self.obs[n].t, "kind": kind, "text": text, **more})

    def _set(self, norm: _NormInst, n: int, state: str, reason: str, log: bool = True) -> None:
        t = self.obs[n].t
        old = norm.state
        if old == "active" and norm.active_since is not None:
            norm.elapsed_before += t - norm.active_since
        norm.state = state
        norm.time = t
        norm.reason = reason
        if state == "active":
            norm.active_since = t
            if norm.activated_at is None:
                norm.activated_at = t
                norm.activated_index = n
        if state == "violated":
            norm.verdict = "excused" if self.assumption_failed_at is not None else "violated"
        elif state in ("fulfilled", "discharged"):
            norm.verdict = "fulfilled"
        if state in FINAL_STATES:
            norm.candidates = []
        norm.history.append({"t": t, "from": old, "state": state, "reason": reason})
        if log:
            self._log(n, "norm", f"{norm.d.id}: {old} -> {state} ({reason})",
                      norm=norm.d.id, state=state)

    def _is_bearer(self, norm: _NormInst, performer: str | None, n: int) -> bool:
        if performer is None or norm.bearer_ref is None:
            return False
        text, bound = self.ev.actor(norm.bearer_ref, n)
        if bound:
            return performer == text
        m = _ACTOR_RE.match(performer)
        return bool(m) and m.group(1) == norm.bearer_ref.name \
            and self.ev.is_member(m.group(1), m.group(2))

    # --- amendments (F.6) -----------------------------------------------------

    def _amend(self, n: int, name: str) -> None:
        """Replace the contract by its new version at observation n."""
        t = self.obs[n].t
        sos = self.loader(name)
        if self.ev.actors is not None:
            self.ev.actors = declared_actors(sos)
        new = next((c for c in sos.contracts if c.id == self.contract.id), None)
        if new is None:
            raise NormsEvalError(f"amendment at t={_fmt_t(t)}: {name} has no contract "
                                 f"'{self.contract.id}'")
        self.lifecycle = new.lifecycle
        if new.lifecycle is not None:
            self._fill_states(n)
            if self.obs[n].state not in new.lifecycle.states:
                raise NormsEvalError(
                    f"amendment at t={_fmt_t(t)}: the contract instance is in state "
                    f"'{self.obs[n].state}', which is not a state of the lifecycle in {name}")
        old_norms = self.norms
        old_ids = set(self.by_id)
        self.contract = new
        self.ev.has_lifecycle = new.lifecycle is not None
        self.ev.bound_refs = _bound_refs(new)
        self.ev._members = {}
        self.events = {r.creates for r in new.counts_as if r.creates}
        self._build_rules()
        self.targets = _targets_of(new)
        effects: list[dict] = []
        self.amendments.append({"t": t, "file": name, "effects": effects})
        self._log(n, "amend", f"contract amended: {name}")

        def effect(norm_id: str, what: str, before: str | None, after: str | None,
                   reason: str) -> None:
            effects.append({"norm": norm_id, "effect": what, "from_state": before,
                            "to_state": after, "reason": reason})
            self._log(n, "amend", f"{norm_id}: {what}, {before or '-'} -> {after or '-'} ({reason})",
                      norm=norm_id)

        new_defs = {d.id: d for d in new.norms}
        table: dict[str, _NormInst] = {}
        transforms: list[tuple[_NormInst, str, float]] = []
        for o in old_norms:
            oid, state, nd = o.d.id, o.state, new_defs.get(o.d.id)
            if state in FINAL_STATES:                 # not affected
                table[oid] = o
            elif state == "inactive":
                if nd is None:
                    effect(oid, "removed", state, None, "the new version has no norm with this id")
                    continue
                table[oid] = self._norm(nd)
                table[oid].history = o.history
                if hasattr(o, "clocks"):
                    table[oid].clocks = o.clocks
                self._warn_frozen(table[oid])
                if _definition(o.d) != _definition(nd):
                    effect(oid, "new_definition", state, state,
                           "takes the definition of the new version")
            elif o.d.on_amendment == "discharge":
                self._set(o, n, "discharged", "amendment: on_amendment is discharge", log=False)
                table[oid] = o
                effect(oid, "discharge", state, "discharged", "on_amendment is discharge")
            elif o.d.on_amendment == "transform":
                elapsed = o.elapsed(self._now(n))
                self._set(o, n, "discharged",
                          f"amendment: on_amendment is transform: {o.d.transform_to}", log=False)
                table[oid] = o
                transforms.append((o, state, elapsed))
            elif nd is None or nd.kind != o.d.kind:
                why = ("the new version has no norm with this id" if nd is None else
                       f"the kind changes from {o.d.kind} to {nd.kind}")
                self._set(o, n, "discharged", f"amendment: {why}", log=False)
                table[oid] = o
                effect(oid, "carry_over", state, "discharged", why)
            else:
                inst = table[oid] = self._carry(o, nd)
                changes = []
                if o.d.content.strip() != nd.content.strip():
                    changes.append("content changes")
                if _deadline(o.d) != _deadline(nd):
                    changes.append(f"deadline changes from {_fmt_d(o.d.deadline_ms)} "
                                   f"to {_fmt_d(nd.deadline_ms)}")
                elapsed = inst.elapsed(self._now(n))
                late = (nd.kind in ("obligation", "prohibition") and inst.deadline is not None
                        and not inst.frozen and elapsed > inst.deadline)
                # F.6: the window of a late instance has closed. Without a
                # pending candidate it is decided now, before step 1, also
                # when suspended; with one, it stays in force and step 4
                # decides it when its candidates are decided, as under the
                # new version from the start.
                if late and inst.candidates:
                    changes.append(f"the elapsed time of {_fmt_t(elapsed)}s exceeds the new "
                                   f"deadline of {_fmt_t(inst.deadline)}s; "
                                   f"{len(inst.candidates)} pending candidate(s) decide it")
                    late = False
                if late and nd.kind == "obligation":
                    why = (f"the elapsed time of {_fmt_t(elapsed)}s exceeds the new deadline "
                           f"of {_fmt_t(inst.deadline)}s")
                    self._set(inst, n, "violated", f"amendment: {why}", log=False)
                    self._amend_violated.append(inst)
                    effect(oid, "carry_over", state, "violated", why)
                elif late:
                    why = (f"the elapsed time of {_fmt_t(elapsed)}s exceeds the new deadline "
                           f"of {_fmt_t(inst.deadline)}s; the window has closed")
                    self._set(inst, n, "fulfilled", f"amendment: {why}", log=False)
                    effect(oid, "carry_over", state, "fulfilled", why)
                else:
                    effect(oid, "carry_over", state, state,
                           "; ".join(["continues under the new version"] + changes))
        added = []
        for d in new.norms:
            if d.id not in table and d.id not in old_ids:
                table[d.id] = self._norm(d)
                self._warn_frozen(table[d.id])
                added.append(table[d.id])
        self.norms = ([table[d.id] for d in new.norms if d.id in table]
                      + [o for o in old_norms if o.d.id not in new_defs and o.d.id in table])
        self.by_id = {x.d.id: x for x in self.norms}

        for o, state, elapsed in transforms:
            target = o.d.transform_to
            x = self.by_id.get(target) if target in new_defs else None
            if x is None:
                effect(o.d.id, "transform", state, "discharged",
                       f"the new version has no norm '{target}'; nothing more happens")
            elif x.state != "inactive" or x.frozen:
                what = "not evaluable" if x.frozen else x.state
                effect(o.d.id, "transform", state, "discharged",
                       f"'{target}' is {what}; nothing more happens")
            else:
                effect(o.d.id, "transform", state, "discharged", f"transformed to {target}")
                timed = x.d.kind in ("obligation", "prohibition")
                x.elapsed_before = elapsed if timed else 0
                x.elapsed_start = x.elapsed_before
                self._set(x, n, "active", f"amendment: transformed from {o.d.id}", log=False)
                note = f" with {_fmt_t(elapsed)}s already elapsed" if timed else ""
                effect(target, "activated", "inactive", "active",
                       f"transform of {o.d.id}{note}")
        for x in added:
            if (x.state == "inactive" and not x.frozen and x.trigger is None
                    and x.d.id not in self.targets):
                self._set(x, n, "active", "amendment: new norm without trigger", log=False)
                effect(x.d.id, "added", None, "active",
                       "new in the new version; no trigger, so active at once")
            elif x.state == "inactive":
                effect(x.d.id, "added", None, "inactive", "new in the new version")

        self._amend_items(n)

    def _carry(self, o: _NormInst, nd: NormDef) -> _NormInst:
        """The instance `o` continued under the definition `nd`: its state
        and the time elapsed against its deadline are kept."""
        inst = self._norm(nd)
        self._warn_frozen(inst)
        for attr in ("state", "verdict", "activated_at", "activated_index", "active_since",
                     "elapsed_before", "judge_from", "time", "history", "elapsed_start"):
            setattr(inst, attr, getattr(o, attr))
        if hasattr(o, "clocks"):
            inst.clocks = o.clocks
        if not inst.frozen:
            inst.reason = o.reason
        if o.d.content.strip() == nd.content.strip():
            inst.candidates = list(o.candidates)
        return inst

    def _amend_items(self, n: int) -> None:
        """assume: / guarantee: of the new version. An item with the same
        text continues; a new one is evaluated from this observation on; an
        old one that is not decided is closed, a decided one is kept."""
        t = self.obs[n].t
        for key, exprs in (("assume", self.contract.assume), ("guarantee", self.contract.guarantee)):
            old = list(self.items[key])
            items = []
            for expr in exprs:
                text = item_text(expr)
                same = next((i for i in old if i["text"] == text and not i["closed"]), None)
                if same is not None:
                    old.remove(same)
                    items.append(same)
                else:
                    items.append(self._item(expr, n))
            for i in old:
                if not i["closed"] and not i["error"] and i["value"] in (None, "undefined"):
                    i["closed"] = True
                    i["value"] = None
                    i["reason"] = (f"not decided: removed by the amendment at t={_fmt_t(t)}")
                items.append(i)
            self.items[key] = items

    # --- one observation (F.5, "Order within one observation") ---------------

    def run(self) -> None:
        last = len(self.obs) - 1
        for n in range(len(self.obs)):
            if self._step(n, n == last):
                break                 # run_trace handles what follows the end
        self._finish_items()

    def _step(self, n: int, is_last: bool) -> bool:
        """Process observation n, given the observations before it. Returns
        True when the contract instance ended at it. `is_last` says that the
        trace stops here, which ends the instance unless the observation
        says ``"end": false``."""
        o = self.obs[n]
        if n == 0:
            self._start()
        self._amend_violated = []
        if o.amend:
            self._amend(n, o.amend)                   # F.6, before step 1
        self._check_state(n)
        self._contract_items(n)
        created = self._counts_as(n)                  # step 1
        self._powers(n, created)                      # step 2
        self._triggers(n, created)                    # step 3
        violated = self._amend_violated + self._judge(n, created)   # step 4
        for norm in violated:                         # step 5
            self._on_violation(norm, n, activate=True)
        reason = None                                 # step 6
        if self.lifecycle and o.state_after in self.lifecycle.terminal:
            reason = f"terminal state {o.state_after}"
        elif o.end is True:
            reason = "end of the instance in the trace"
        elif is_last and o.end is not False:
            reason = _END_LAST
        if reason:
            self._settle(n, reason)
        return bool(reason)

    def _ignored_warning(self, count: int) -> str:
        return (f"{count} observation(s) after the end of the contract instance "
                f"(t={_fmt_t(self.obs[self.ended_at].t)}) are ignored")

    def _start(self) -> None:
        state = f", state {self.obs[0].state}" if self.lifecycle else ""
        self._log(0, "start", f"contract instance started{state}")
        for norm in self.norms:
            if norm.frozen:
                continue
            if norm.trigger is None and norm.d.id not in self.targets:
                self._set(norm, 0, "active", "contract instance started")

    def _contract_items(self, n: int) -> None:
        """assume: before guarantee:, so that a guarantee that fails at the
        observation at which an assumption fails is excused."""
        for key in ("assume", "guarantee"):
            for item in self.items[key]:
                if item["error"] or item["closed"] or item["value"] is not None:
                    continue
                expr = item["expr"]
                if item["temporal"]:
                    # at the start of the instance (F.4), or at the amendment
                    v = self.ev.holds(expr, item["start"], n)
                else:
                    v = self.ev.holds(expr, n, n)     # a state predicate, per observation
                    if v is True:
                        item["seen_true"] = True
                        continue
                    if v is UNKNOWN:
                        item["undefined"] += 1
                        continue
                if v is UNDECIDED:
                    continue
                if v is UNKNOWN:
                    item["value"] = "undefined"
                    item["reason"] = "not decided: the trace gives no value for a name it uses"
                    continue
                item["value"] = v
                item["time"] = self.obs[n].t
                label = "assumption" if key == "assume" else "guarantee"
                if v:
                    item["reason"] = f"holds (evaluated at {item['origin']})"
                    self._log(n, key, f"{label} holds: {item['text']}")
                    continue
                item["reason"] = (f"does not hold (evaluated at {item['origin']})"
                                  if item["temporal"] else "false at this observation")
                if key == "assume":
                    if self.assumption_failed_at is None:
                        self.assumption_failed_at = self.obs[n].t
                    self._log(n, key, f"assumption failed: {item['text']}")
                else:
                    excused = self.assumption_failed_at is not None
                    item["verdict"] = "excused" if excused else "violated"
                    self._log(n, key, f"guarantee {item['verdict']}: {item['text']}")

    def _counts_as(self, n: int) -> list[dict]:
        created: list[dict] = []
        for rule, when, cond in self.rules:
            if when.error or (cond is not None and cond.error):
                continue
            if cond is not None and self.ev.holds(cond.expr, n, n) is not True:
                continue
            if when.kind == "message":
                sources = [(m["from"], f"{m['from']} -> {m['to']} : {m['name']}")
                           for m in when.matching_messages(n)]
            else:
                sources = [(None, when.text)] if when.occurs(n, []) else []
            for performer, source in sources:
                e = {"t": self.obs[n].t, "name": rule.creates, "performer": performer,
                     "source": source}
                created.append(e)
                self.inst_events.append(e)
                by = f" performed by {performer}" if performer else ""
                self._log(n, "event", f"institutional event {rule.creates}{by} ({source})")
        return created

    def _powers(self, n: int, created: list[dict]) -> None:
        powers = [p for p in self.norms if p.d.kind == "power" and not p.frozen]
        # The acts of this observation: (content text, performer, description)
        acts: list[tuple[str, str | None, str]] = []
        seen: set[tuple[str, str]] = set()
        for e in created:
            if e["performer"] is not None and any(
                p.content.kind == "event" and p.content.text == e["name"] for p in powers
            ):
                acts.append((e["name"], e["performer"], f"institutional event {e['name']}"))
        for p in powers:
            if p.content.kind != "message":
                continue
            for m in p.content.matching_messages(n):
                desc = f"{m['from']} -> {m['to']} : {m['name']}"
                if (p.content.text, desc) not in seen:
                    seen.add((p.content.text, desc))
                    acts.append((p.content.text, m["from"], f"message {desc}"))
        active = {p.d.id for p in powers if p.state == "active"}
        for content, performer, desc in acts:
            holders = [p for p in powers if p.content.text == content
                       and p.d.id in active and self._is_bearer(p, performer, n)]
            if not holders:
                rec = {"t": self.obs[n].t, "act": desc, "performer": performer,
                       "reason": "no matching power of the performer is active"}
                self.unauthorised.append(rec)
                self._log(n, "unauthorised",
                          f"unauthorised act: {desc} by {performer} (no matching power is active)")
                continue
            for p in holders:
                self._log(n, "power", f"{performer} exercises the power {p.d.id} ({desc})",
                          norm=p.d.id)
                eff = p.d.effects
                if eff is None:
                    continue
                why = f"power {p.d.id} exercised by {performer}"
                for names, source, target in (
                    (eff.activate, ("inactive",), "active"),
                    (eff.suspend, ("active",), "suspended"),
                    (eff.resume, ("suspended",), "active"),
                    (eff.discharge, ("active", "suspended"), "discharged"),
                ):
                    for name in names:
                        norm = self.by_id.get(name)
                        if norm is not None and not norm.frozen and norm.state in source:
                            self._set(norm, n, target, why)

    def _triggers(self, n: int, created: list[dict]) -> None:
        for norm in self.norms:
            if norm.frozen or norm.trigger is None or not norm.trigger.occurs(n, created):
                continue
            if norm.state == "inactive":
                self._set(norm, n, "active", f"trigger occurred: {norm.trigger.text}")
            else:
                self._log(n, "ignored",
                          f"{norm.d.id}: trigger ignored, the norm is {norm.state}", norm=norm.d.id)

    def _judge(self, n: int, created: list[dict]) -> list[_NormInst]:
        t = self.obs[n].t
        violated: list[_NormInst] = []
        for norm in self.norms:
            if norm.frozen or n < norm.judge_from:
                continue
            kind = norm.d.kind
            if norm.state == "active" and kind in ("obligation", "prohibition"):
                elapsed = norm.elapsed(t)
                open_ = norm.deadline is None or elapsed <= norm.deadline
                if open_:
                    norm.candidates.append(n)
                hit = None
                for k in list(norm.candidates):
                    v = norm.content.content(k, n, created)
                    if v is True and norm.d.id in self._exempt.get(k, ()):
                        v = False
                    if v is True:
                        hit = k
                        break
                    if v is not UNDECIDED:
                        norm.candidates.remove(k)
                if hit is not None:
                    at = "" if hit == n else f" at t={_fmt_t(self.obs[hit].t)}"
                    verb = "holds" if norm.content.kind == "predicate" else "occurred"
                    if hit != n and norm.content.kind == "predicate":
                        verb = "held"
                    if kind == "obligation":
                        self._set(norm, n, "fulfilled", f"content {verb}{at}: {norm.content.text}")
                    else:
                        self._set(norm, n, "violated", f"content {verb}{at}: {norm.content.text}")
                        violated.append(norm)
                elif not open_ and not norm.candidates:
                    due = norm.closed_at(t)
                    if due is None:
                        due = t - (elapsed - norm.deadline)
                    if kind == "obligation":
                        self._set(norm, n, "violated",
                                  f"deadline of {_fmt_t(norm.deadline)}s passed at "
                                  f"t={_fmt_t(due)} without the content")
                        violated.append(norm)
                    else:
                        self._set(norm, n, "fulfilled",
                                  f"window of {_fmt_t(norm.deadline)}s closed at "
                                  f"t={_fmt_t(due)} without the content")
            if (norm.state in ("active", "suspended") and norm.until is not None
                    and norm.until.occurs(n, created)):
                self._set(norm, n, "discharged", f"until occurred: {norm.until.text}")
        return violated

    def _on_violation(self, norm: _NormInst, n: int, activate: bool) -> None:
        ov = norm.d.on_violation
        rec = {
            "t": self.obs[n].t, "norm": norm.d.id,
            "severity": ov.severity if ov else "Major",
            "verdict": norm.verdict,
            "sanction": ov.sanction if ov else None,
            "reparation": ov.reparation if ov else None,
            "reparation_activated": False,
            "transition": None,
        }
        notes = []
        if ov and ov.reparation and activate:
            rep = self.by_id.get(ov.reparation)
            if rep is not None and not rep.frozen and rep.state == "inactive":
                rec["reparation_activated"] = True
        terminal = bool(self.lifecycle and self.obs[n].state in self.lifecycle.terminal)
        if ov and ov.transition and activate and not terminal:
            rec["transition"] = ov.transition
            notes.append(f"requests transition to {ov.transition}")
        if rec["sanction"]:
            notes.append(f"sanction: {rec['sanction']}")
        self.violations.append(rec)
        tail = f"; {'; '.join(notes)}" if notes else ""
        self._log(n, "violation",
                  f"violation {norm.d.id} ({rec['severity']}, {rec['verdict']}){tail}",
                  norm=norm.d.id)
        if rec["reparation_activated"]:
            rep = self.by_id[ov.reparation]
            rep.judge_from = n + 1         # first judged at the next observation
            self._set(rep, n, "active", f"reparation of {norm.d.id}")

    def _settle(self, n: int, reason: str) -> None:
        """End of the contract instance (F.5, item 6)."""
        self.ended_at = n
        self.end_reason = reason
        self._log(n, "end", f"contract instance ended ({reason})")
        t = self.obs[n].t
        why = "contract instance ended"
        late: list[_NormInst] = []
        for norm in self.norms:
            if norm.frozen or norm.state in FINAL_STATES:
                continue
            kind = norm.d.kind
            in_force = norm.state in ("active", "suspended")
            if in_force and norm.activated_index == n:
                self._set(norm, n, "discharged",
                          f"{why} at the observation at which the norm was activated")
                continue
            if in_force and kind in ("obligation", "prohibition") and norm.candidates:
                norm.reason = ("not decided: the contract instance ended before the temporal "
                               "content was decided")
                continue
            if in_force and kind == "obligation":
                if norm.deadline is None:
                    self._set(norm, n, "violated", f"{why} without the content")
                    late.append(norm)
                elif norm.elapsed(t) <= norm.deadline:
                    self._set(norm, n, "discharged", f"{why} before the deadline")
                else:
                    self._set(norm, n, "violated", f"{why} after the deadline without the content")
                    late.append(norm)
            elif in_force and kind == "prohibition":
                self._set(norm, n, "fulfilled", f"{why} without the content")
            else:
                self._set(norm, n, "discharged", why)
        # The instance has ended: the violation is reported, but no
        # reparation is activated and no transition requested.
        for norm in late:
            self._on_violation(norm, n, activate=False)

    def _finish_items(self) -> None:
        ended = self.ended_at is not None
        last = self.ended_at if ended else len(self.obs) - 1
        for key in ("assume", "guarantee"):
            for item in self.items[key]:
                if item["error"] or item["closed"]:
                    continue
                v = item["value"]
                if v is None and not item["temporal"] and ended and item["seen_true"]:
                    v = item["value"] = True
                    item["time"] = self.obs[self.ended_at].t
                    item["reason"] = "true at every observation of the contract instance"
                    if item["undefined"]:
                        item["reason"] += f" at which it has a value ({item['undefined']} without)"
                elif v is None:
                    if item["temporal"] and item["start"] > last:
                        item["reason"] = "not evaluated: the amendment did not take effect"
                    elif item["temporal"]:
                        item["reason"] = ("not decided: the trace ended before the interval was over"
                                          if not ended else
                                          "not decided: the contract instance ended before the "
                                          "interval was over")
                    elif not item["seen_true"]:
                        item["reason"] = "not decided: the trace gives no value for a name it uses"
                    else:
                        item["reason"] = "not decided: the trace is a prefix (\"end\": false)"
                if key == "guarantee" and v is True:
                    item["verdict"] = "fulfilled"

    # --- result ---------------------------------------------------------------

    def result(self) -> dict:
        obs = self.obs
        ended = self.ended_at is not None
        last = self.ended_at if ended else len(obs) - 1

        def subst(text: str | None) -> str | None:
            if not text:
                return text
            for name, value in self.trace.bindings.items():
                text = re.sub(rf"\[\s*{re.escape(str(name))}\s*\]", f"[{value}]", text)
            return text

        norms = [{
            "id": n.d.id, "kind": n.d.kind, "bearer": subst(n.d.bearer),
            "state": n.state, "verdict": n.verdict,
            "activated_at": n.activated_at, "time": n.time, "reason": n.reason,
            "evaluable": n.frozen is None, "history": n.history,
        } for n in self.norms]
        for key in ("assume", "guarantee"):
            for pos, i in enumerate(self.items[key], start=1):
                i["index"] = pos
        assumptions = [{
            "index": i["index"], "text": i["text"], "temporal": i["temporal"],
            "status": ("pending" if i["value"] in (None, "undefined")
                       else "holds" if i["value"] else "failed"),
            "time": i["time"], "reason": i["reason"],
        } for i in self.items["assume"]]
        guarantees = [{
            "index": i["index"], "text": i["text"], "temporal": i["temporal"],
            "verdict": i["verdict"], "time": i["time"], "reason": i["reason"],
        } for i in self.items["guarantee"]]
        failed = ([n["id"] for n in norms if n["verdict"] == "violated"]
                  + [f"guarantee {g['index']}" for g in guarantees if g["verdict"] == "violated"])
        return _plain({
            "contract": self.contract.id,
            "norms_version": "0.1",
            "trace": self.trace.path,
            "instance": {
                "bindings": self.trace.bindings,
                "start": obs[0].t,
                "end": obs[last].t if ended else None,
                "ended": ended,
                "end_reason": self.end_reason,
                "state": obs[last].state_after if self.lifecycle else None,
                "observations": last + 1,
            },
            "norms": norms,
            "violations": self.violations,
            "institutional_events": self.inst_events,
            "unauthorised": self.unauthorised,
            "amendments": self.amendments,
            "assumptions": assumptions,
            "guarantees": guarantees,
            "timeline": self.timeline,
            "warnings": self.warnings,
            "violated": failed,
            "ok": not failed,
        })


def select_contract(sos: SoSDefinition, contract_id: str | None = None,
                    option: str = "--contract") -> ContractDef:
    """The contract to evaluate: the named one, else the only contract with
    norms: or counts_as:, else the only contract of the file. `option` is
    how the caller's user names a contract, for the error message."""
    if contract_id is not None:
        for c in sos.contracts:
            if c.id == contract_id:
                return c
        raise NormsEvalError(f"no contract '{contract_id}' in the file")
    with_norms = [c for c in sos.contracts if c.norms or c.counts_as]
    pool = with_norms or sos.contracts
    if len(pool) == 1:
        return pool[0]
    if not pool:
        raise NormsEvalError("the file has no contract")
    raise NormsEvalError(
        f"the file has several contracts; choose one with {option} "
        f"({', '.join(c.id for c in pool)})")


def run_trace(contract: ContractDef, trace: Trace, loader=None,
              actors: dict | None = None) -> dict:
    """The result of one contract on a whole trace. An observation after
    the end of the contract instance is not part of the instance and has
    no effect on it (F.5): when the trace has such observations, the
    instance is evaluated on the trace up to its end, so that they give no
    name of a quantity, and they are counted in a warning."""
    engine = _Engine(contract, trace, loader, actors)
    engine.run()
    after = len(trace.observations) - 1 - (engine.ended_at or 0)
    if engine.ended_at is not None and after > 0:
        instance = Trace(observations=trace.observations[:engine.ended_at + 1],
                         bindings=trace.bindings, request=trace.request, path=trace.path)
        engine = _Engine(contract, instance, loader, actors)
        engine.run()
        engine.warnings.append(engine._ignored_warning(after))
    return engine.result()


# <<< monitor-runtime


# >>> monitor-regimes
# === Contracts not in force (Appendix A, A.7.1; Appendix F, F.5) ===========
#
# A file with `regimes:` binds, per regime, the contracts in force. A trace
# names the current regime with the key ``regime``; from that observation
# on the named regime is current, and before the first one the initial
# regime of A.7.1 is. A contract instance whose
# contract is not in force sees nothing of an observation: it is judged on
# the projection of the trace (F.5, "Contracts not in force"), the
# observations at which the contract is in force. Their deadlines and the
# intervals of the temporal operators are measured on the clock of the
# contract, the time without the time it spent not in force; every time
# that is reported is the time of the trace. An instance that ends at an
# observation at which the contract is not in force is settled there
# (F.5, item 6) on the clock as it stood when the contract left force.
#
# A trace for a file without `regimes:` is judged by `run_trace` above, and
# so is one that names no regime when the initial regime has the contract
# in force (the projection is then the trace). The norms monitor of the Python target contains this
# part only when the file has `regimes:`.

REGIME_KEY = "regime"
_REGIMES_NO_AMEND = (
    "a trace that names a regime cannot also amend the contract ('amend'); "
    "cadl does not apply regime bindings and amendments together")


class RegimeBindings:
    """The regimes of a file, per regime the contracts not in force, and the
    initial regime (A.7.1), in which a trace is until it names a regime."""

    def __init__(self, regimes: Any, suspended: Any, initial: str) -> None:
        self.regimes = list(regimes)
        self.suspended = {str(r): list(c) for r, c in dict(suspended).items()}
        self.initial = initial

    def in_force(self, contract_id: str, regime: str | None) -> bool:
        return regime is None or contract_id not in self.suspended.get(regime, ())

    def check(self, regime: Any, where: str) -> str:
        if not isinstance(regime, str) or regime not in self.regimes:
            got = f"'{regime}'" if isinstance(regime, str) else "a value that is not a string"
            raise TraceError(f"{where}: 'regime' must name a regime of the file "
                             f"({', '.join(self.regimes)}), got {got}")
        return regime


def split_regime(rec: Any, where: str, bindings: RegimeBindings | None) -> tuple:
    """(the observation without its key ``regime``, the regime it names or None)."""
    if not isinstance(rec, dict) or REGIME_KEY not in rec:
        return rec, None
    if bindings is None:
        raise TraceError(f"{where}: 'regime' names the current regime, but the file has "
                         f"no regimes: block (Appendix A, A.7.1)")
    regime = bindings.check(rec[REGIME_KEY], where)
    return {k: v for k, v in rec.items() if k != REGIME_KEY}, regime


class _ContractClock:
    """Whether a contract is in force, and its clock: the time of the trace
    without the time the contract spent not in force before it."""

    def __init__(self, bindings: RegimeBindings, contract_id: str) -> None:
        self.bindings = bindings
        self.contract_id = contract_id
        self.regime: str | None = bindings.initial
        self.out_since: Any = None        # when the contract left force
        self.offset: Any = 0              # time not in force, before out_since

    def save(self) -> tuple:
        return self.regime, self.out_since, self.offset

    def restore(self, saved: tuple) -> None:
        self.regime, self.out_since, self.offset = saved

    @property
    def in_force_now(self) -> bool:
        return self.out_since is None

    def step(self, t: Any, regime: str | None) -> tuple:
        """(in force, clock) at an observation at time t that names `regime`
        (None: the current regime goes on)."""
        if regime is not None:
            self.regime = regime
        if self.bindings.in_force(self.contract_id, self.regime):
            if self.out_since is not None:
                self.offset += t - self.out_since
                self.out_since = None
            return True, (t - self.offset if self.offset else t)
        if self.out_since is None:
            self.out_since = t
        return False, self.out_since - self.offset


def _regime_ends_at(o: Observation, lifecycle: Any, is_last: bool) -> bool:
    """Whether the contract instance ends at an observation (F.5, item 6)."""
    after = o.state_after
    if after is None and lifecycle is not None:
        after = lifecycle.initial
    return (o.end is True or (lifecycle is not None and after in lifecycle.terminal)
            or (is_last and o.end is not False))


def _regime_check_state(o: Observation, contract: ContractDef) -> None:
    lc = contract.lifecycle
    if lc is not None and o.state_after is not None and o.state_after not in lc.states:
        raise TraceError(
            f"state '{o.state_after}' at t={_fmt_t(o.t)} is not a state of the "
            f"lifecycle of {contract.id}")


def parse_trace_regimes(lines, path: str | None = None,
                        bindings: RegimeBindings | None = None,
                        own_version: Any = False) -> Trace:
    """`parse_trace` for a trace that may name regimes: each observation
    gets the attribute ``regime``, the regime it names or None."""
    trace = Trace(observations=[], path=path)
    reader = _TraceReader(trace, own_version)
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        where = f"line {lineno}"
        try:
            rec = loads_strict(line)
        except NumberRangeError as e:
            raise TraceError(f"{where}: {e}") from None
        except json.JSONDecodeError as e:
            raise TraceError(f"{where}: not JSON ({e.msg})") from None
        except ValueError as e:
            raise TraceError(f"{where}: not JSON ({e})") from None
        rec, regime = split_regime(rec, where, bindings)
        reader.add(rec, where).regime = regime
    if not trace.observations:
        raise TraceError("the trace has no observation")
    return trace


def names_regime(trace: Trace) -> bool:
    return any(getattr(o, "regime", None) is not None for o in trace.observations)


class _RegimeNormInst(_NormInst):
    """A norm instance whose deadline is measured on the contract's clock:
    `clocks` holds the clock of each entry of `history`."""

    def closed_at(self, t: Any) -> Any:
        """The clock at which the active time reached the deadline (t is the
        clock now); None if it has not."""
        acc, start = self.elapsed_start, None
        stretches = list(zip(self.history, self.clocks))
        if acc >= self.deadline:
            return next((c for h, c in stretches if h["state"] == "active"), None)
        for h, c in stretches + [({"from": self.state, "state": None}, t)]:
            if start is not None and h["from"] == "active" and h["state"] != "active":
                if acc + (c - start) >= self.deadline:
                    return start + (self.deadline - acc)
                acc += c - start
                start = None
            if h["state"] == "active" and start is None:
                start = c
        return None


class _RegimeEvaluator(_Evaluator):
    def _window(self, k: int, n: int, d: float) -> tuple[range, bool]:
        """The observations of [c_k, c_k + d] on the clock of the contract."""
        limit = self.obs[k].c + d
        j = k
        while j <= n and self.obs[j].c <= limit:
            j += 1
        return range(k, j), j <= n


class _RegimeEngine(_Engine):
    """The engine on the projection of a trace (F.5, "Contracts not in
    force"). Every observation has the attributes ``c`` (the clock of the
    contract) and ``out`` (True only for an observation at which the
    contract is not in force and the instance ends)."""

    def __init__(self, contract: ContractDef, trace: Trace, loader=None,
                 actors: dict | None = None) -> None:
        super().__init__(contract, trace, loader, actors)
        self.ev.__class__ = _RegimeEvaluator
        # False when the trace goes on after the last observation of the
        # projection (it was left out), so that it does not end there.
        self.ends_at_last = True

    def _norm(self, d: NormDef) -> _NormInst:
        norm = _NormInst(d, self.events, self.ev)
        norm.__class__ = _RegimeNormInst
        norm.clocks = []
        return norm

    def _now(self, n: int) -> Any:
        return self.obs[n].c

    def _real(self, x: Any, n: int) -> Any:
        """The time of the trace at clock x (x at most the clock of
        observation n): on the stretch in force that contains it."""
        k = n
        while k > 0 and (self.obs[k].out or self.obs[k].c >= x):
            k -= 1
        return self.obs[k].t + (x - self.obs[k].c)

    def run(self) -> None:
        last = len(self.obs) - 1
        for n in range(len(self.obs)):
            if self._step(n, n == last and self.ends_at_last):
                break
        self._finish_items()

    def _step(self, n: int, is_last: bool) -> bool:
        o = self.obs[n]
        if not o.out:
            return super()._step(n, is_last)
        # The instance ends at an observation at which the contract is not
        # in force: only the settlement (F.5, item 6) applies.
        self._amend_violated = []
        self._check_state(n)
        if n == 0:
            self._start()          # the instance starts and ends here
        if self.lifecycle and o.state_after in self.lifecycle.terminal:
            reason = f"terminal state {o.state_after}"
        elif o.end is True:
            reason = "end of the instance in the trace"
        else:
            reason = _END_LAST
        self._settle(n, f"{reason}, while the contract is not in force")
        return True

    def _amend(self, n: int, name: str) -> None:
        raise NormsEvalError(_REGIMES_NO_AMEND)

    def _set(self, norm: _NormInst, n: int, state: str, reason: str, log: bool = True) -> None:
        o = self.obs[n]
        t, c = o.t, o.c
        old = norm.state
        if old == "active" and norm.active_since is not None:
            norm.elapsed_before += c - norm.active_since
        norm.state = state
        norm.time = t
        norm.reason = reason
        if state == "active":
            norm.active_since = c
            if norm.activated_at is None:
                norm.activated_at = t
                norm.activated_index = n
        if state == "violated":
            norm.verdict = "excused" if self.assumption_failed_at is not None else "violated"
        elif state in ("fulfilled", "discharged"):
            norm.verdict = "fulfilled"
        if state in FINAL_STATES:
            norm.candidates = []
        norm.history.append({"t": t, "from": old, "state": state, "reason": reason})
        norm.clocks.append(c)
        if log:
            self._log(n, "norm", f"{norm.d.id}: {old} -> {state} ({reason})",
                      norm=norm.d.id, state=state)

    def _judge(self, n: int, created: list[dict]) -> list[_NormInst]:
        c = self.obs[n].c
        violated: list[_NormInst] = []
        for norm in self.norms:
            if norm.frozen or n < norm.judge_from:
                continue
            kind = norm.d.kind
            if norm.state == "active" and kind in ("obligation", "prohibition"):
                elapsed = norm.elapsed(c)
                open_ = norm.deadline is None or elapsed <= norm.deadline
                if open_:
                    norm.candidates.append(n)
                hit = None
                for k in list(norm.candidates):
                    v = norm.content.content(k, n, created)
                    if v is True and norm.d.id in self._exempt.get(k, ()):
                        v = False
                    if v is True:
                        hit = k
                        break
                    if v is not UNDECIDED:
                        norm.candidates.remove(k)
                if hit is not None:
                    at = "" if hit == n else f" at t={_fmt_t(self.obs[hit].t)}"
                    verb = "holds" if norm.content.kind == "predicate" else "occurred"
                    if hit != n and norm.content.kind == "predicate":
                        verb = "held"
                    if kind == "obligation":
                        self._set(norm, n, "fulfilled", f"content {verb}{at}: {norm.content.text}")
                    else:
                        self._set(norm, n, "violated", f"content {verb}{at}: {norm.content.text}")
                        violated.append(norm)
                elif not open_ and not norm.candidates:
                    due = norm.closed_at(c)
                    if due is None:
                        due = c - (elapsed - norm.deadline)
                    due = self._real(due, n)
                    if kind == "obligation":
                        self._set(norm, n, "violated",
                                  f"deadline of {_fmt_t(norm.deadline)}s passed at "
                                  f"t={_fmt_t(due)} without the content")
                        violated.append(norm)
                    else:
                        self._set(norm, n, "fulfilled",
                                  f"window of {_fmt_t(norm.deadline)}s closed at "
                                  f"t={_fmt_t(due)} without the content")
            if (norm.state in ("active", "suspended") and norm.until is not None
                    and norm.until.occurs(n, created)):
                self._set(norm, n, "discharged", f"until occurred: {norm.until.text}")
        return violated

    def _settle(self, n: int, reason: str) -> None:
        """End of the contract instance (F.5, item 6), on the clock."""
        self.ended_at = n
        self.end_reason = reason
        self._log(n, "end", f"contract instance ended ({reason})")
        c = self.obs[n].c
        why = "contract instance ended"
        late: list[_NormInst] = []
        for norm in self.norms:
            if norm.frozen or norm.state in FINAL_STATES:
                continue
            kind = norm.d.kind
            in_force = norm.state in ("active", "suspended")
            if in_force and norm.activated_index == n:
                self._set(norm, n, "discharged",
                          f"{why} at the observation at which the norm was activated")
                continue
            if in_force and kind in ("obligation", "prohibition") and norm.candidates:
                norm.reason = ("not decided: the contract instance ended before the temporal "
                               "content was decided")
                continue
            if in_force and kind == "obligation":
                if norm.deadline is None:
                    self._set(norm, n, "violated", f"{why} without the content")
                    late.append(norm)
                elif norm.elapsed(c) <= norm.deadline:
                    self._set(norm, n, "discharged", f"{why} before the deadline")
                else:
                    self._set(norm, n, "violated", f"{why} after the deadline without the content")
                    late.append(norm)
            elif in_force and kind == "prohibition":
                self._set(norm, n, "fulfilled", f"{why} without the content")
            else:
                self._set(norm, n, "discharged", why)
        for norm in late:
            self._on_violation(norm, n, activate=False)


def project_trace(trace: Trace, contract: ContractDef, bindings: RegimeBindings) -> tuple:
    """(the projection of a trace for a contract, the index in the trace of
    each of its observations, whether the trace goes on after it).
    Sets ``c`` and ``out`` on the observations of the trace."""
    clock = _ContractClock(bindings, contract.id)
    obs = trace.observations
    last = len(obs) - 1
    kept: list[Observation] = []
    index: list[int] = []
    for i, o in enumerate(obs):
        if o.amend:
            raise NormsEvalError(_REGIMES_NO_AMEND)
        inside, o.c = clock.step(o.t, getattr(o, "regime", None))
        o.out = not inside
        if not inside:
            _regime_check_state(o, contract)
            if not _regime_ends_at(o, contract.lifecycle, i == last):
                continue
        kept.append(o)
        index.append(i)
        if o.out:
            break
    goes_on = not index or index[-1] != last
    projection = Trace(observations=kept, bindings=trace.bindings, request=trace.request,
                       path=trace.path)
    return projection, index, goes_on


def run_regime_trace(contract: ContractDef, trace: Trace, bindings: RegimeBindings,
                     actors: dict | None = None) -> dict:
    """`run_trace` for a trace that names regimes (F.5, "Contracts not in
    force")."""
    projection, index, goes_on = project_trace(trace, contract, bindings)
    if not projection.observations:
        raise NormsEvalError(
            f"the contract {contract.id} is not in force at any observation of the trace, "
            f"which does not end the contract instance: there is nothing to judge")

    def engine_for(observations: list, through: int, ends_at_last: bool) -> _RegimeEngine:
        part = Trace(observations=observations, bindings=trace.bindings,
                     request=trace.request, path=trace.path)
        eng = _RegimeEngine(contract, part, None, actors)
        eng.ends_at_last = ends_at_last
        # the names of the quantities: those of every observation of the
        # instance, also of one at which the contract is not in force
        for o in trace.observations[:through + 1]:
            eng.ev.known.update(o.values)
        return eng

    last = len(trace.observations) - 1
    engine = engine_for(projection.observations, last, not goes_on)
    engine.run()
    if engine.ended_at is not None:
        end = index[engine.ended_at]
        if end < last:
            engine = engine_for(projection.observations[:engine.ended_at + 1], end, True)
            engine.run()
            engine.warnings.append(engine._ignored_warning(last - end))
    return engine.result()

# <<< monitor-regimes


# >>> monitor-versions
# === Versions while a system runs (Appendix K, K.11) =======================
#
# The versions of a run are files that declare `amendment: 0.1` and form one
# sequence by `previous:`. The version in force at an observation is given
# by the effective times on a time base (the length of a period and the
# time at which period 1 starts, or time stamps in seconds since
# 1970-01-01T00:00:00Z), or named by the observation with the key
# ``version``, or both, and then they must agree. A contract instance
# follows the version in force at its first observation; when a version
# takes effect, the instance follows the transitional rule of its contract
# in that version: `keep` leaves it as it is, `switch` amends it (F.6) at
# that observation, and `switch` with `in_states:` at the first observation
# at which its state is one of them. The amendment itself is the one of
# `_Engine._amend`. The norms monitor of the Python target contains this
# part only when the file declares `amendment: 0.1` with an `amendment:`
# block.

VERSION_KEY = "version"
_VERSIONS_NO_AMEND = (
    "a run with versions cannot also amend the contract with 'amend': the transitional "
    "rules decide when an instance is amended (Appendix K, K.11)")


def _version_label(name: str) -> str:
    return f"version {name}"


def _epoch_seconds(text: str) -> Fraction:
    """An ISO 8601 date or date-time as exact seconds since
    1970-01-01T00:00:00Z; a date is 00:00 UTC, a date-time without an
    offset is UTC (K.4)."""
    import datetime as _dt
    try:
        t = _dt.datetime.fromisoformat(text)
    except ValueError:
        t = _dt.datetime.combine(_dt.date.fromisoformat(text), _dt.time())
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    d = t - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    return Fraction(d.days * 86400 + d.seconds) + Fraction(d.microseconds, 1000000)


class TimeBase:
    """The time base of a run (K.11): `period` the length of a period in
    seconds and `origin` the time at which period 1 starts, and `epoch`
    true when time stamps are seconds since 1970-01-01T00:00:00Z."""

    def __init__(self, period: Any = None, origin: Any = 0, epoch: bool = False) -> None:
        for name, v in (("period", period), ("origin", origin)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float, Fraction))
                                  or (isinstance(v, float) and not math.isfinite(v))):
                raise NormsEvalError(f"the {name} of the time base must be a finite number "
                                     f"of seconds")
            if v is not None and abs(v) > MAX_T:
                raise NormsEvalError(f"the {name} of the time base must be at most 2**53 "
                                     f"seconds in absolute value, as a time of a trace")
        if period is not None and period <= 0:
            raise NormsEvalError("the length of a period must be positive")
        self.period = None if period is None else _exact(period)
        self.origin = _exact(origin if origin is not None else 0)
        self.epoch = bool(epoch)

    @property
    def given(self) -> bool:
        return self.period is not None or self.epoch

    def time_of(self, effective: Any) -> Any:
        """The time stamp of an effective time ({"period": k} or {"at":
        text}), or None when the base has nothing for its form."""
        if not effective:
            return None
        (form, v), = effective.items()
        if form == "period":
            return None if self.period is None else self.origin + (int(v) - 1) * self.period
        if form == "at":
            return _epoch_seconds(str(v)) if self.epoch else None
        return None


def _describe_effective(effective: Any) -> str:
    (form, v), = effective.items()
    return f"period {v}" if form == "period" else f"{v}"


class VersionInfo:
    """What a run needs of one version: its identity, the version it
    replaces, its effective time and its transitional rules (K.4)."""

    def __init__(self, data: dict, sos: Any = None) -> None:
        self.name = data["version"]
        self.previous = data.get("previous")
        self.effective = data.get("effective")
        self.default = data.get("default") or "switch"
        self.rules = {c: (r, list(s)) for c, (r, s) in dict(data.get("contracts") or {}).items()}
        self.sos = sos                 # the parsed file or the model
        # the regime bindings of the version (RegimeBindings), None without
        # `regimes:` (Appendix A, A.7.1)
        self.regimes: Any = None

    def rule_for(self, contract_id: str) -> tuple:
        """(rule, in_states or None) of the contract in this version."""
        rule, states = self.rules.get(contract_id, (self.default, []))
        return rule, (list(states) or None)


def _effective_order(infos: list, base: Any) -> Any:
    """A function that gives a comparable key of an effective time, or None
    when the effective times of the versions cannot be ordered: with a
    time base, the time stamp; without one, the period or the instant
    itself when all versions use the same form of `effective:`."""
    if base is not None:
        def key(e: Any) -> Any:
            return base.time_of(e)
        if all(key(i.effective) is not None for i in infos if i.effective):
            return key
        return None
    forms = {next(iter(i.effective)) for i in infos if i.effective}
    if len(forms) > 1:
        return None

    def same_form(e: Any) -> Any:
        (form, v), = e.items()
        return int(v) if form == "period" else _epoch_seconds(str(v))
    return same_form


def _infer_previous(infos: list, base: Any) -> None:
    """Give a version without `previous:` the version that it replaces
    (Appendix K, K.11, from cadl 0.3.35): the version with the latest
    effective time strictly earlier than its own, among the versions of the
    run. A version without `effective:`, or with no version effective
    earlier, is a first version and needs none. Refuse when this is
    ambiguous: two candidates with the same effective time, or effective
    times that cannot be ordered."""
    if sum(1 for i in infos if i.previous is None) <= 1:
        return                      # one first version: the sequence of 0.3.34
    missing = [i for i in infos if i.previous is None and i.effective]
    if not missing:
        return
    key = _effective_order(infos, base)

    def ask(i: Any, why: str) -> NormsEvalError:
        return NormsEvalError(
            f"version {i.name} gives no previous:, and {why}; give previous: (Appendix K, "
            f"K.11)")
    if key is None:
        raise ask(missing[0], "the effective times of the versions cannot be ordered "
                              "(a period and an instant, or no time base for them)")
    for i in missing:
        own = key(i.effective)
        earlier = [(key(j.effective), j) for j in infos
                   if j is not i and j.effective and key(j.effective) < own]
        firsts = [j for j in infos if j is not i and not j.effective]
        if not earlier:
            if firsts:
                if len(firsts) > 1:
                    raise ask(i, f"versions {', '.join(j.name for j in firsts)} give no "
                                 f"effective: either")
                i.previous = firsts[0].name
            continue
        latest = max(k for k, _ in earlier)
        at = [j for k, j in earlier if k == latest]
        if len(at) > 1:
            raise ask(i, f"versions {', '.join(j.name for j in at)} take effect at the same "
                         f"time before it")
        i.previous = at[0].name


class VersionChain:
    """The versions of a run, in the order of the sequence (K.11)."""

    def __init__(self, infos: list, base: TimeBase | None = None) -> None:
        names = [i.name for i in infos]
        for name in names:
            if not isinstance(name, str) or names.count(name) > 1:
                raise NormsEvalError(
                    f"two versions of the run have the version '{name}'; the versions of a "
                    f"run have distinct sos.version (Appendix K, K.11)")
        by_name = {i.name: i for i in infos}
        _infer_previous(infos, base if base is not None and base.given else None)
        heads = [i for i in infos if i.previous not in by_name]
        if len(heads) != 1:
            raise NormsEvalError(
                "the versions do not form one sequence by previous: (Appendix K, K.11): "
                + ("every version but the first must name the version before it with "
                   "previous: or give effective: to place it after one"
                   if len(heads) > 1 else "previous: goes round in a circle"))
        order = [heads[0]]
        while len(order) < len(infos):
            nxt = [i for i in infos if i.previous == order[-1].name]
            if len(nxt) != 1:
                raise NormsEvalError(
                    f"the versions do not form one sequence by previous: (Appendix K, K.11): "
                    f"{len(nxt) or 'no'} version(s) name version {order[-1].name} as previous")
            order.append(nxt[0])
        for i in order[1:]:
            if not i.effective:
                raise NormsEvalError(
                    f"version {i.name} gives no effective:; every version of a run but the "
                    f"first says when it takes effect (Appendix K, K.11)")
        self.infos = order
        self.base = base if base is not None and base.given else None
        self.times: list = [None] * len(order)
        if self.base is not None:
            for k, i in enumerate(order):
                t = self.base.time_of(i.effective)
                if t is None and i.effective:
                    form = next(iter(i.effective))
                    raise NormsEvalError(
                        f"version {i.name} takes effect at {_describe_effective(i.effective)}, "
                        f"and the time base has no "
                        + ("length of a period" if form == "period"
                           else "epoch for a date or date-time")
                        + " (Appendix K, K.11)")
                self.times[k] = t
            for k in range(1, len(order)):
                prev = self.times[k - 1]
                if prev is not None and not self.times[k] > prev:
                    raise NormsEvalError(
                        f"version {order[k].name} takes effect at t={_fmt_t(self.times[k])}, "
                        f"not after version {order[k - 1].name} (t={_fmt_t(prev)}); effective "
                        f"times increase along the sequence (Appendix K, K.11)")

    @property
    def timed(self) -> bool:
        return self.base is not None

    def names(self) -> list:
        return [i.name for i in self.infos]

    def index(self, name: str) -> int:
        return self.names().index(name)

    def check_name(self, value: Any, where: str) -> str:
        if not isinstance(value, str) or value not in self.names():
            got = f"'{value}'" if isinstance(value, str) else "a value that is not a string"
            raise TraceError(f"{where}: 'version' must name a version of the run "
                             f"({', '.join(self.names())}), got {got}")
        return value

    def by_time(self, t: Any) -> int:
        k = None
        for j, tj in enumerate(self.times):
            if tj is None or tj <= t:
                k = j
        if k is None:
            raise TraceError(
                f"no version is in force at t={_fmt_t(t)}: version {self.infos[0].name} takes "
                f"effect at t={_fmt_t(self.times[0])} (Appendix K, K.11)")
        return k

    def sos_of(self, label: str) -> Any:
        for i in self.infos:
            if _version_label(i.name) == label:
                return i.sos
        raise NormsEvalError(f"no {label} in the run")


class VersionTracker:
    """The version that a contract instance follows (K.11), observation by
    observation. `step` is called once per observation, in order, and is
    remembered, so that a second call for the same observation returns the
    same amendments."""

    def __init__(self, chain: VersionChain, contract_id: str) -> None:
        self.chain = chain
        self.contract_id = contract_id
        self.current: Any = None       # index of the version in force
        self.follows: Any = None       # index of the version the instance follows
        self.pending: Any = None       # (index, states) of a waiting switch
        self.started: Any = None       # index of the version at the start
        self.memo: dict = {}
        self.events: list = []

    def save(self) -> tuple:
        return (self.current, self.follows, self.pending, self.started, dict(self.memo),
                list(self.events))

    def restore(self, saved: tuple) -> None:
        (self.current, self.follows, self.pending, self.started, memo, events) = saved
        self.memo, self.events = dict(memo), list(events)

    def in_force(self, t: Any, named: Any) -> int:
        return _version_in_force(self.chain, self.current, t, named)

    def start_at(self, k: int) -> int:
        """Start the instance under version k, the version in force."""
        self.current = self.follows = self.started = k
        return k

    def step(self, n: int, t: Any, named: Any, state: Any) -> tuple:
        """(the labels of the versions the instance is amended to at
        observation n, in order; the events of observation n)."""
        if n in self.memo:
            return self.memo[n]
        return self.step_at(n, t, self.in_force(t, named), state)

    def start(self, t: Any, named: Any) -> int:
        """The version in force at the first observation, which the instance follows."""
        return self.start_at(self.in_force(t, named))

    def _event(self, t: Any, v: int, kind: str, text: str) -> dict:
        return {"t": t, "version": self.chain.infos[v].name, "event": kind, "text": text}

    def step_at(self, n: int, t: Any, k: int, state: Any) -> tuple:
        """`step` with the version in force k."""
        if n in self.memo:
            return self.memo[n]
        names: list = []
        events: list = []
        infos = self.chain.infos

        def switch(v: int, why: str) -> None:
            events.append(self._event(
                t, v, "switch", f"{why}the instance switches from version "
                                f"{infos[self.follows].name} to version {infos[v].name}"))
            names.append(_version_label(infos[v].name))
            self.follows = v
            self.pending = None

        # A waiting switch whose state holds before this observation is made
        # first; then the versions that take effect here apply (K.11).
        if self.pending is not None and state in self.pending[1]:
            switch(self.pending[0], f"in state {state}, ")
        for v in range(self.current + 1, k + 1):
            rule, states = infos[v].rule_for(self.contract_id)
            if self.pending is not None:
                p = self.pending[0]
                events.append(self._event(
                    t, p, "dropped", f"the waiting switch to version {infos[p].name} is "
                                     f"dropped: version {infos[v].name} takes effect first"))
                self.pending = None
            head = f"version {infos[v].name} takes effect"
            if rule == "keep":
                events.append(self._event(
                    t, v, "keep", f"{head}; the instance keeps version "
                                  f"{infos[self.follows].name} (transitional: keep)"))
            elif states is None:
                switch(v, f"{head} (transitional: switch); ")
            elif state in states:
                switch(v, f"{head} (transitional: switch, in_states: {', '.join(states)}); "
                          f"in state {state}, ")
            else:
                self.pending = (v, states)
                events.append(self._event(
                    t, v, "wait", f"{head} (transitional: switch, in_states: "
                                  f"{', '.join(states)}); the instance is in state {state} "
                                  f"and switches at the first observation in one of them"))
        self.current = k
        self.events.extend(events)
        self.memo[n] = (names, events)
        return names, events


def _version_in_force(chain: VersionChain, cur: Any, t: Any, named: Any) -> int:
    """The index of the version in force at a step at time t that names the
    version `named` (or None), when `cur` was in force at the step before
    (None at the first step)."""
    if named is not None:
        k = chain.index(named)
        if cur is not None and k < cur:
            raise TraceError(
                f"at t={_fmt_t(t)} the trace names version {named}, which comes before "
                f"version {chain.infos[cur].name}, in force before (Appendix K, K.11)")
        if chain.timed:
            c = chain.by_time(t)
            if c != k:
                raise TraceError(
                    f"at t={_fmt_t(t)} the trace names version {named}, but by the "
                    f"effective times version {chain.infos[c].name} is in force "
                    f"(Appendix K, K.11)")
        return k
    if chain.timed:
        return chain.by_time(t)
    return 0 if cur is None else cur


class _VersionEngine(_Engine):
    """The engine of a run with versions (K.11): before the steps of an
    observation, the instance is amended to the versions that the tracker
    gives, one after the other (F.6)."""

    def __init__(self, contract: ContractDef, trace: Trace, chain: VersionChain,
                 tracker: VersionTracker, actors: dict | None = None) -> None:
        super().__init__(contract, trace, chain.sos_of, actors)
        self.tracker = tracker
        self._logged: set = set()

    def decide(self, n: int) -> tuple:
        """(the versions to amend to at observation n, the events); sets
        ``amend`` of the observation. Logs nothing: `_step` does."""
        o = self.obs[n]
        if n == 0:
            return [], []
        state = o.state if self.lifecycle is not None else None
        if self.lifecycle is not None and state is None:
            state = self.lifecycle.initial
        names, events = self.tracker.step(n, o.t, getattr(o, "version", None), state)
        o.amend = tuple(names) or None
        return names, events

    def _step(self, n: int, is_last: bool) -> bool:
        _, events = self.decide(n)
        if n not in self._logged:
            self._logged.add(n)
            for e in events:
                self._log(n, "version", e["text"], version=e["version"])
        return super()._step(n, is_last)

    def _amend(self, n: int, name: Any) -> None:
        for label in name:
            super()._amend(n, label)


class _RegimeRun:
    """A run with versions and regime bindings (Appendix K, K.11, "Versions
    and regimes", from cadl 0.3.35), one step at a time.

    At the boundary between two steps, the regime transition chosen at the
    end of the step before is taken first (the trace names the regime that
    is current from this step on), and then the versions take effect
    (K.11). Whether the contract is in force at a step is decided by the
    regime bindings of the version in force then, by contract name; the
    version that an instance follows only selects the definition of the
    contract. A regime that is current at some step must be a regime of
    every version that the run uses, and a run that is in no regime (its
    versions have no `regimes:`) may not use a version that has one.

    The clock of the contract stops while it is not in force (F.5). A
    version that takes effect then still reaches the instance at that step
    (`keep`, `switch`, and a waiting `switch` with `in_states:`, judged on
    the lifecycle state that the instance sees, which does not change while
    the contract is not in force); since the clock does not advance, the
    amendment is applied at the next step at which the contract is in
    force (or at which the instance ends), before anything else, and the
    events keep the time of the step at which the version took effect."""

    def __init__(self, chain: VersionChain, contract_id: str) -> None:
        self.chain = chain
        self.cid = contract_id
        self.tracker = VersionTracker(chain, contract_id)
        self.k: Any = None             # the version in force
        self.regime: Any = None        # the current regime (None: no regimes)
        self.seen: list = []           # the regimes current at some step
        self.first: Any = None         # the first version in force
        self.out_since: Any = None     # when the contract left force
        self.offset: Any = 0           # time not in force, before out_since
        self.started = False
        self.deferred: list = []       # amendments made while not in force
        self.events: list = []         # their events, not yet logged
        self.frozen: Any = None        # the state the instance sees meanwhile

    def save(self) -> tuple:
        return (self.k, self.regime, list(self.seen), self.first, self.out_since, self.offset,
                self.started, list(self.deferred), list(self.events), self.frozen,
                self.tracker.save())

    def restore(self, saved: tuple) -> None:
        (self.k, self.regime, seen, self.first, self.out_since, self.offset, self.started,
         deferred, events, self.frozen, tracker) = saved
        self.seen, self.deferred, self.events = list(seen), list(deferred), list(events)
        self.tracker.restore(tracker)

    @property
    def in_force_now(self) -> bool:
        return self.out_since is None

    def _has(self, v: int, regime: Any) -> bool:
        b = self.chain.infos[v].regimes
        return b is None if regime is None else (b is not None and regime in b.regimes)

    def _check(self, t: Any) -> None:
        infos = self.chain.infos
        for v in range(self.first, self.k + 1):
            for r in self.seen:
                if self._has(v, r):
                    continue
                if r is None:
                    other = next(infos[u].name for u in range(self.first, self.k + 1)
                                 if infos[u].regimes is None)
                    raise NormsEvalError(
                        f"at t={_fmt_t(t)}: version {infos[v].name} binds regimes (regimes:) "
                        f"and version {other}, also used by the run, has none, so the run is "
                        f"in no regime of version {infos[v].name}; a regime of a run with "
                        f"versions must be a regime of every version that the run uses "
                        f"(Appendix K, K.11)")
                raise NormsEvalError(
                    f"at t={_fmt_t(t)}: the run is in regime {r}, which is not a regime of "
                    f"version {infos[v].name}"
                    + ("" if infos[v].regimes is not None else " (it has no regimes:)")
                    + "; a regime of a run with versions must be a regime of every version "
                      "that the run uses (Appendix K, K.11)")

    def step(self, t: Any, named: Any, regime: Any) -> tuple:
        """(in force, clock) at the next step of the run, at time t, which
        names the version `named` and the regime `regime` (or None)."""
        k = _version_in_force(self.chain, self.k, t, named)
        if self.k is None:
            self.first = k
            if regime is None:
                b = self.chain.infos[k].regimes
                regime = None if b is None else b.initial
        elif regime is None:
            regime = self.regime
        self.k, self.regime = k, regime
        if regime not in self.seen:
            self.seen.append(regime)
        self._check(t)
        b = self.chain.infos[k].regimes
        if b is None or b.in_force(self.cid, regime):
            if self.out_since is not None:
                self.offset += t - self.out_since
                self.out_since = None
            return True, (t - self.offset if self.offset else t)
        if self.out_since is None:
            self.out_since = t
        return False, self.out_since - self.offset

    def instance(self, n: int, t: Any, state: Any, member: bool) -> tuple:
        """(the labels of the versions the instance is amended to at this
        step, in order; the events to log at it), after `step`. `member`:
        the step is one of the projection (F.5), at which the instance is
        judged; `state` the lifecycle state before it, which the instance
        sees when the contract is in force (otherwise it sees the state
        after the last step of the projection, `frozen`). At another step
        nothing is returned: the amendments and events wait for the next
        step of the projection. The instance starts at the first step of
        the projection."""
        if not self.started:
            return self.flush() if member else ([], [])
        names, events = self.tracker.step_at(n, t, self.k,
                                             state if self.in_force_now else self.frozen)
        self.deferred += names
        self.events += events
        return self.flush() if member else ([], [])

    def flush(self) -> tuple:
        """The amendments and events that wait for a step of the projection,
        at one that has already been stepped; the instance starts there if
        it has not started."""
        if not self.started:
            self.started = True
            self.tracker.start_at(self.k)
            return [], []
        names, events, self.deferred, self.events = self.deferred, self.events, [], []
        return names, events

    def contract_of(self, v: int) -> Any:
        return next((c for c in self.chain.infos[v].sos.contracts if c.id == self.cid), None)


class _VersionRegimeEngine(_RegimeEngine):
    """The engine on the projection of a run with versions and regime
    bindings (`_RegimeRun`): before the steps of an observation, the
    instance is amended to the versions that the run gives (F.6), also at
    an observation at which the contract is not in force and the instance
    ends, before its settlement."""

    def __init__(self, contract: ContractDef, trace: Trace, chain: VersionChain,
                 run: _RegimeRun, actors: dict | None = None) -> None:
        super().__init__(contract, trace, chain.sos_of, actors)
        self.run_ = run
        self.tracker = run.tracker
        self.version_events: dict = {}

    def _amend(self, n: int, name: Any) -> None:
        for label in name:
            _Engine._amend(self, n, label)

    def _step(self, n: int, is_last: bool) -> bool:
        for e in self.version_events.pop(n, ()):
            self.timeline.append({"t": e["t"], "kind": "version", "text": e["text"],
                                  "version": e["version"]})
        o = self.obs[n]
        if not o.out:
            return super()._step(n, is_last)
        self._amend_violated = []
        if n == 0:
            self._start()          # the instance starts and ends here
        if o.amend:
            self._amend(n, o.amend)
        self._check_state(n)
        for norm in self._amend_violated:
            self._on_violation(norm, n, activate=False)
        if self.lifecycle and o.state_after in self.lifecycle.terminal:
            reason = f"terminal state {o.state_after}"
        elif o.end is True:
            reason = "end of the instance in the trace"
        else:
            reason = _END_LAST
        self._settle(n, f"{reason}, while the contract is not in force")
        return True


def _seen_state(o: Observation, lifecycle: Any) -> Any:
    """The lifecycle state after an observation, as the instance sees it."""
    if lifecycle is None:
        return None
    return o.state_after if o.state_after is not None else lifecycle.initial


def _state_before(o: Observation, lifecycle: Any) -> Any:
    if lifecycle is None:
        return None
    return o.state if o.state is not None else lifecycle.initial


def run_version_regime_trace(contract_id: str, trace: Trace, chain: VersionChain,
                             actors_of) -> dict:
    """`run_trace` for a run with versions whose versions bind regimes or
    whose trace names a regime (K.11, "Versions and regimes")."""
    if any(o.amend for o in trace.observations):
        raise NormsEvalError(_VERSIONS_NO_AMEND)
    full = trace.observations

    def once(observations: list, through: int) -> tuple:
        run = _RegimeRun(chain, contract_id)
        part = Trace(observations=[], bindings=trace.bindings, request=trace.request,
                     path=trace.path)
        eng: Any = None
        index: list = []
        last = len(observations) - 1
        for i, o in enumerate(observations):
            o.amend = None
            inside, o.c = run.step(o.t, getattr(o, "version", None), getattr(o, "regime", None))
            o.out = not inside
            contract = eng.contract if eng is not None else run.contract_of(run.k)
            if contract is None:
                raise NormsEvalError(
                    f"version {chain.infos[run.k].name}, in force at the start of the "
                    f"instance, has no contract '{contract_id}'")
            member = inside
            if not inside:
                _regime_check_state(o, contract)
                member = _regime_ends_at(o, contract.lifecycle, i == last)
            names, events = run.instance(i, o.t, _state_before(o, contract.lifecycle), member)
            if not member:
                continue
            o.amend = tuple(names) or None
            part.observations.append(o)
            n = len(part.observations) - 1
            if eng is None:
                eng = _VersionRegimeEngine(contract, part, chain, run,
                                           actors_of(chain.infos[run.k].sos))
                for x in full[:through + 1]:
                    eng.ev.known.update(x.values)
            elif eng.lifecycle is not None:
                eng._fill_states(n)
            eng.version_events[n] = events
            index.append(i)
            ended = eng._step(n, i == last)
            run.frozen = _seen_state(o, eng.lifecycle)
            if ended:
                break
        if eng is None:
            raise NormsEvalError(
                f"the contract {contract_id} is not in force at any observation of the trace, "
                f"which does not end the contract instance: there is nothing to judge")
        eng._finish_items()
        return eng, index

    engine, index = once(full, len(full) - 1)
    if engine.ended_at is not None:
        end = index[engine.ended_at]
        if end < len(full) - 1:
            engine, _ = once(full[:end + 1], end)
            engine.warnings.append(engine._ignored_warning(len(full) - 1 - end))
    return version_result(engine)


def chain_regimes(chain: VersionChain) -> Any:
    """The regimes that a trace of a run with versions may name: those of
    any of its versions (`_RegimeRun` checks that the run's regimes are
    regimes of every version it uses). None when no version has
    `regimes:`."""
    names: list = []
    for info in chain.infos:
        if info.regimes is not None:
            names += [r for r in info.regimes.regimes if r not in names]
    if not any(info.regimes is not None for info in chain.infos):
        return None
    return RegimeBindings(names, {}, None)


def parse_trace_versions(lines, path: str | None = None, chain: Any = None,
                         bindings: Any = None) -> Trace:
    """`parse_trace` for a trace that may name versions (and regimes):
    each observation gets the attributes ``version`` and ``regime``, the
    version and the regime it names or None."""
    trace = Trace(observations=[], path=path)
    reader = _TraceReader(trace)
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        where = f"line {lineno}"
        try:
            rec = loads_strict(line)
        except NumberRangeError as e:
            raise TraceError(f"{where}: {e}") from None
        except json.JSONDecodeError as e:
            raise TraceError(f"{where}: not JSON ({e.msg})") from None
        except ValueError as e:
            raise TraceError(f"{where}: not JSON ({e})") from None
        rec, named = split_version(rec, where, chain)
        rec, regime = split_regime(rec, where, bindings)
        o = reader.add(rec, where)
        o.version, o.regime = named, regime
    if not trace.observations:
        raise TraceError("the trace has no observation")
    return trace


def split_version(rec: Any, where: str, chain: Any) -> tuple:
    """(the observation without its key ``version``, the version it names or None)."""
    if not isinstance(rec, dict) or VERSION_KEY not in rec:
        return rec, None
    if chain is None:
        raise TraceError(f"{where}: 'version' names the version in force, but the file does not "
                         f"declare 'amendment: 0.1' with an amendment: block (Appendix K, K.11)")
    named = chain.check_name(rec[VERSION_KEY], where)
    return {k: v for k, v in rec.items() if k != VERSION_KEY}, named


def names_version(trace: Trace) -> bool:
    return any(getattr(o, "version", None) is not None for o in trace.observations)


def run_version_trace(contract_id: str, trace: Trace, chain: VersionChain, actors_of) -> dict:
    """`run_trace` for a run with versions (K.11). `actors_of(sos)` gives the
    declared actors of a version."""
    if any(o.amend for o in trace.observations):
        raise NormsEvalError(_VERSIONS_NO_AMEND)

    def once(observations: list) -> Any:
        part = Trace(observations=observations, bindings=trace.bindings,
                     request=trace.request, path=trace.path)
        tracker = VersionTracker(chain, contract_id)
        first = observations[0]
        start = chain.infos[tracker.start(first.t, getattr(first, "version", None))]
        contract = next((c for c in start.sos.contracts if c.id == contract_id), None)
        if contract is None:
            raise NormsEvalError(f"version {start.name}, in force at the start of the instance, "
                                 f"has no contract '{contract_id}'")
        for o in observations:
            o.amend = None
        eng = _VersionEngine(contract, part, chain, tracker, actors_of(start.sos))
        eng.run()
        return eng

    engine = once(trace.observations)
    after = len(trace.observations) - 1 - (engine.ended_at or 0)
    if engine.ended_at is not None and after > 0:
        engine = once(trace.observations[:engine.ended_at + 1])
        engine.warnings.append(engine._ignored_warning(after))
    return version_result(engine)


def version_result(engine: Any) -> dict:
    """The result of the engine, with the key ``versions``."""
    result = engine.result()
    result["versions"] = versions_of(engine.tracker)
    return result


def versions_of(tracker: VersionTracker) -> dict:
    """The key ``versions`` of a result: the sequence, the version the
    instance started under and the one it follows, and the events."""
    infos = tracker.chain.infos
    return _plain({
        "sequence": [i.name for i in infos],
        "start": None if tracker.started is None else infos[tracker.started].name,
        "follows": None if tracker.follows is None else infos[tracker.follows].name,
        "events": [dict(e) for e in tracker.events],
    })


def format_text_versions(result: dict, text: str) -> str:
    """The text report with a section on the versions of the run."""
    v = result.get("versions")
    if not v:
        return text
    lines = ["Versions",
             f"  sequence: {', '.join(v['sequence'])}; the instance started under version "
             f"{v['start']} and follows version {v['follows']}"]
    for e in v["events"]:
        lines.append(f"  t={_fmt_t(e['t'])}  {e['text']}")
    head, sep, tail = text.rpartition("\n\nResult: ")
    if not sep:
        return text + "\n\n" + "\n".join(lines)
    return head + "\n\n" + "\n".join(lines) + sep + tail

# <<< monitor-versions


def load_version(path) -> SoSDefinition:
    """Parse and type-check a CADL file that declares norms 0.1."""
    from .parser import parse_file
    from .type_checker import type_check

    if not Path(path).exists():
        raise NormsEvalError(f"File not found: {path}")
    try:
        sos = parse_file(path)
    except Exception as e:
        raise NormsEvalError(f"Parse error: {path}: {e}") from None
    if not norms_declared(sos):
        raise NormsEvalError(f"{path} does not declare 'norms: 0.1' under extensions:")
    check = type_check(sos)
    if not check.ok:
        errors = "\n".join(f"  {e}" for e in check.errors)
        raise NormsEvalError(f"{path} has {len(check.errors)} type-check error(s):\n{errors}")
    return sos


def evaluate(sos: SoSDefinition, trace: Trace, contract_id: str | None = None,
             versions: dict | None = None, chain: VersionChain | None = None,
             notes: list | None = None) -> dict:
    """Compute the verdicts of one contract instance on a trace (F.5),
    applying the amendments the trace names (F.6).

    `versions` maps the value of an ``amend`` key to a parsed file; a value
    that is not in it is read as a path relative to the trace file.
    `chain` is the sequence of versions of the run (Appendix K, K.11), whose
    first version is `sos` or comes before it; it applies when it has
    several versions or a time base, or when the trace names a version.
    `notes`, when given, receives a warning for each ``amend`` to a version
    whose transitional rule for the contract is not a plain `switch`.
    Returns a JSON-serializable dict; see the README for its keys."""
    if not norms_declared(sos):
        raise NormsEvalError("the file does not declare 'norms: 0.1' under extensions:")
    contract = select_contract(sos, contract_id)

    def load(name: str) -> SoSDefinition:
        if versions and name in versions:
            new = versions[name]
            if not norms_declared(new):
                raise NormsEvalError(f"{name} does not declare 'norms: 0.1' under extensions:")
            return new
        base = Path(trace.path).parent if trace.path else Path(".")
        return load_version(base / name)

    def loader(name: str) -> SoSDefinition:
        new = load(name)
        note = amend_rule_warning(new, name, contract.id)
        if notes is not None and note is not None and note not in notes:
            notes.append(note)
        return new

    bindings = regime_bindings(sos)
    if names_regime(trace) and bindings is None:
        raise NormsEvalError("the trace names a regime ('regime'), but the file has no "
                             "regimes: block (Appendix A, A.7.1)")
    if chain is not None and (len(chain.infos) > 1 or chain.timed or names_version(trace)):
        if names_regime(trace) or any(i.regimes is not None for i in chain.infos):
            # versions and regime bindings (K.11, from cadl 0.3.35)
            return run_version_regime_trace(contract.id, trace, chain, declared_actors)
        return run_version_trace(contract.id, trace, chain, declared_actors)
    if bindings is not None and (names_regime(trace)
                                 or not bindings.in_force(contract.id, bindings.initial)):
        # before the first `regime` the trace is in the initial regime (A.7.1)
        return run_regime_trace(contract, trace, bindings, declared_actors(sos))
    return run_trace(contract, trace, loader, declared_actors(sos))


def amend_rule_warning(sos: SoSDefinition, name: str, contract_id: str, t: Any = None) -> str | None:
    """The warning for an ``amend`` to a version whose transitional rule for
    the contract is `keep` or `switch` with `in_states:` (Appendix K, K.11):
    the key amends the instance at that observation whatever the rule
    says. None for another rule or a file without an amendment: block."""
    from .amendment import amendment_declared, analyse
    raw = getattr(sos, "amendment", None)
    if not amendment_declared(sos) or raw is None or not raw.has_block:
        return None
    a = analyse(sos)
    rule, states = a.transitional.get(contract_id, (a.transitional_default, []))
    if rule == "switch" and not states:
        return None
    what = "keep" if rule == "keep" else f"switch in {', '.join(states)}"
    at = "" if t is None else f" at t={_fmt_t(t)}"
    return (f"'amend' to {name}{at} amends {contract_id} there, but the transitional rule of "
            f"that version for {contract_id} is {what} (Appendix K, K.4): the amendment is "
            f"applied as a switch; give the versions of the run to apply the rule (K.11)")


def own_version(sos: SoSDefinition) -> str | None:
    """The sos.version of a file as text, or None (Appendix K, K-2)."""
    from .amendment import _scalar_text
    return _scalar_text(getattr(sos, "version", None))


def version_info(sos: SoSDefinition, path: Any = None) -> VersionInfo:
    """The version of a run that a parsed file is (Appendix K, K.11)."""
    from .amendment import amendment_declared, analyse
    raw = getattr(sos, "amendment", None)
    where = path if path is not None else "the file"
    if not amendment_declared(sos) or raw is None or not raw.has_block:
        raise NormsEvalError(
            f"{where} does not declare 'amendment: 0.1' with an amendment: block; a version "
            f"of a run gives sos.version, previous: and effective: (Appendix K, K.11)")
    a = analyse(sos)
    if a.errors:
        raise NormsEvalError(f"{where} has {len(a.errors)} error(s) of the amendment "
                             f"extension; see cadl check")
    if a.version is None:
        raise NormsEvalError(f"{where} gives no sos.version (Appendix K, K-2)")
    info = VersionInfo({"version": a.version, "previous": a.previous,
                        "effective": a.effective, "default": a.transitional_default,
                        "contracts": dict(a.transitional)}, sos)
    info.regimes = regime_bindings(sos)
    return info


def version_chain(sos: SoSDefinition, path: Any = None, version_paths: Any = (),
                  base: TimeBase | None = None) -> VersionChain | None:
    """The versions of a run: the file and the later versions (paths), or
    None for a file without an amendment: block when no version and no
    time base is given."""
    version_paths = list(version_paths or ())
    raw = getattr(sos, "amendment", None)
    from .amendment import amendment_declared
    has_block = amendment_declared(sos) and raw is not None and raw.has_block
    if not has_block and not version_paths and (base is None or not base.given):
        return None
    infos = [version_info(sos, path)]
    for vp in version_paths:
        infos.append(version_info(load_version(vp), vp))
    return VersionChain(infos, base)


def parse_duration_seconds(text: Any) -> Fraction:
    """A duration (`3600s`, `1h`, `500ms`) or a number of seconds, exactly."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|min|h)?\s*", str(text))
    if not m:
        raise NormsEvalError(f"not a duration: {text!r} (write it as 3600s, 60min, 1h or 500ms)")
    return Fraction(Decimal(m.group(1))) * Fraction(_UNIT_SECONDS[m.group(2) or "s"])


def regime_bindings(sos: SoSDefinition) -> RegimeBindings | None:
    """The regimes of a file with `regimes:` and the contracts each does
    not have in force, or None for a file without the block (or with a
    block that breaks R-1 to R-6)."""
    from .regimes import checked
    bindings = checked(sos)
    if bindings is None:
        return None
    from .regimes import initial_regime
    return RegimeBindings([b.name for b in bindings], {b.name: b.suspended for b in bindings},
                          initial_regime(sos))


# >>> monitor-runtime
# === Text report ===========================================================

def _table(rows: list[list[str]], indent: str = "  ") -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    out = []
    for r in rows:
        cells = [c.ljust(w) for c, w in zip(r, widths)] + [r[-1]]
        out.append((indent + "  ".join(cells)).rstrip())
    return out


def format_text(result: dict) -> str:
    inst = result["instance"]
    lines = [f"Contract {result['contract']} (norms {result['norms_version']})"]
    if inst["bindings"]:
        lines.append("  bindings: " + ", ".join(f"{k}={v}" for k, v in inst["bindings"].items()))
    if inst["ended"]:
        lines.append(f"  instance: t={_fmt_t(inst['start'])} .. {_fmt_t(inst['end'])}, "
                     f"{inst['observations']} observation(s), ended: {inst['end_reason']}")
    else:
        lines.append(f"  instance: from t={_fmt_t(inst['start'])}, {inst['observations']} "
                     f"observation(s), not ended (the trace is a prefix)")

    lines += ["", "Timeline"]
    lines += _table([[f"t={_fmt_t(e['t'])}", e["kind"], e["text"]] for e in result["timeline"]])

    lines += ["", "Norms"]
    if result["norms"]:
        rows = [["id", "kind", "state", "verdict", "t", "reason"]]
        rows += [[n["id"], n["kind"], n["state"], n["verdict"], _fmt_t(n["time"]), n["reason"]]
                 for n in result["norms"]]
        lines += _table(rows)
    else:
        lines.append("  (none)")

    lines += ["", "Violations"]
    if result["violations"]:
        rows = []
        for v in result["violations"]:
            extra = []
            if v["reparation"]:
                extra.append(f"reparation {v['reparation']} "
                             + ("activated" if v["reparation_activated"] else "not activated"))
            if v["transition"]:
                extra.append(f"requested transition to {v['transition']}")
            if v["sanction"]:
                extra.append(f"sanction: {v['sanction']}")
            rows.append([f"t={_fmt_t(v['t'])}", v["norm"], v["severity"], v["verdict"],
                         "; ".join(extra)])
        lines += _table(rows)
    else:
        lines.append("  (none)")

    if result["institutional_events"]:
        lines += ["", "Institutional events"]
        for e in result["institutional_events"]:
            by = f" performed by {e['performer']}" if e["performer"] else ""
            lines.append(f"  t={_fmt_t(e['t'])}  {e['name']}{by} ({e['source']})")
    if result["unauthorised"]:
        lines += ["", "Unauthorised acts"]
        for u in result["unauthorised"]:
            lines.append(f"  t={_fmt_t(u['t'])}  {u['act']} by {u['performer']}: {u['reason']}")

    if result["amendments"]:
        lines += ["", "Amendments"]
        for a in result["amendments"]:
            lines.append(f"  t={_fmt_t(a['t'])}  {a['file']}")
            rows = [[e["norm"], e["effect"],
                     f"{e['from_state'] or '-'} -> {e['to_state'] or '-'}", e["reason"]]
                    for e in a["effects"]]
            if rows:
                lines += _table(rows, indent="    ")

    for title, key, field_ in (("Assumptions", "assumptions", "status"),
                               ("Guarantees", "guarantees", "verdict")):
        if not result[key]:
            continue
        lines += ["", title]
        for a in result[key]:
            how = ("temporal, evaluated once" if a["temporal"]
                   else "state predicate, per observation")
            when = f" at t={_fmt_t(a['time'])}" if a["time"] is not None else ""
            why = f": {a['reason']}" if a["reason"] else ""
            lines.append(f"  {a['index']}. {a[field_]}{when}  {a['text']}")
            lines.append(f"     ({how}){why}")

    if result["warnings"]:
        lines += ["", "Warnings"]
        lines += [f"  {w}" for w in result["warnings"]]

    lines.append("")
    if result["ok"]:
        lines.append("Result: no norm or guarantee is violated")
    else:
        lines.append("Result: VIOLATED — " + ", ".join(result["violated"]))
    return "\n".join(lines)


# <<< monitor-runtime

def evaluate_files(cadl_path, trace_path, contract_id: str | None = None,
                   version_paths: Any = (), base: TimeBase | None = None,
                   notes: list | None = None) -> dict:
    """Parse and type-check a CADL file, read a trace and evaluate it.
    `version_paths` are the later versions of the run and `base` its time
    base (Appendix K, K.11)."""
    if not Path(trace_path).exists():
        raise NormsEvalError(f"File not found: {trace_path}")
    sos = load_version(cadl_path)
    bindings = regime_bindings(sos)
    chain = version_chain(sos, cadl_path, version_paths, base)
    if chain is not None:
        with open(trace_path, encoding="utf-8") as f:
            trace = parse_trace_versions(f, path=str(trace_path), chain=chain,
                                         bindings=chain_regimes(chain))
    elif bindings is None:
        trace = load_trace(trace_path, own_version(sos))
    else:
        with open(trace_path, encoding="utf-8") as f:
            trace = parse_trace_regimes(f, path=str(trace_path), bindings=bindings,
                                        own_version=own_version(sos))
    if (notes is not None and version_paths and chain is not None and not chain.timed
            and not names_version(trace)):
        notes.append("--version is given, but there is no time base (--period, --epoch) and "
                     "the trace names no version, so the first version is in force throughout "
                     "(Appendix K, K.11)")
    result = evaluate(sos, trace, contract_id, chain=chain, notes=notes)
    result["file"] = str(cadl_path)
    return result
