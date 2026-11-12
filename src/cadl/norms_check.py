"""Design-time checking of norms by a model checker (Appendix F, F.12).

`build_model` translates the norms of one contract instance of a file
that declares ``norms: 0.1`` into a finite Markov decision process in the
PRISM language: at each observation the environment chooses which message
events occur, which values the quantities of the contract take (one value
per class of values that the predicates of the contract distinguish) and
whether the instance ends there. The transition of one observation is the
step semantics of F.5 (`cadl.norms_eval`) unrolled for that choice.

`check` runs the questions of F.12 with PRISM (``prism`` on PATH) and
returns, for every "possible" answer, a trace that `cadl norms-eval`
evaluates to the claimed verdict; `check` replays every such trace through
`norms_eval` itself and reports whether it was confirmed.

The module also contains a small interpreter of the PRISM text it
generates (`PrismModel`), which the differential tests use to run the
generated model on paths of environment choices and compare the norm
states at every observation with those of `norms_eval`.
"""

from __future__ import annotations

import itertools
import json
import os
import math
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from .ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    ContractDef,
    FloatLiteral,
    FunctionCall,
    IntLiteral,
    MemberAccess,
    RangeExpr,
    SoSDefinition,
    StringLiteral,
    UnaryOp,
)
from .norms import TEMPORAL_OPERATORS, norms_declared
from .norms_eval import (
    _ACTOR_RE,
    _IDENTIFIER_RE,
    _MESSAGE_EVENT_RE,
    NormsEvalError,
    Observation,
    Trace,
    _bound_refs,
    _Engine,
    _targets_of,
    declared_actors,
    desugar_wildcards,
    parse_trace,
    run_trace,
    select_contract,
)

NORMS_CHECK_VERSION = "0.1"

# Norm states of the model (F.5); EXCUSED is the state `violated` with
# the verdict `excused`, UNUSED the state `discharged` of a norm that the
# end of the instance found inactive.
INACTIVE, ACTIVE, SUSPENDED, FULFILLED, VIOLATED, EXCUSED, DISCHARGED, UNUSED, ABSENT = \
    range(9)
# ABSENT: a norm of another version of the contract that the instance does
# not have (not yet added by an amendment, or removed by one, F.6)
STATE_NAMES = ("inactive", "active", "suspended", "fulfilled", "violated", "violated",
               "discharged", "discharged", "absent")
FINAL = (FULFILLED, VIOLATED, EXCUSED, DISCHARGED, UNUSED)
IN_FORCE = (ACTIVE, SUSPENDED)

# Limits of v0.1 (F.12): larger models are refused.
MAX_HORIZON = 1000
MAX_CHOICES = 8192
MAX_VALUATIONS = 50000
MAX_ATOMS = 16
MAX_INSTANCES = 4
MAX_COMMANDS = 32768

# The time by which the model must be built (--timeout covers the
# generation too); None: no bound.
_DEADLINE: list = [None]


def _generation_time() -> None:
    import time
    d = _DEADLINE[0]
    if d is not None and time.monotonic() > d:
        raise NormsCheckTimeout(
            "building the model did not finish within --timeout; the time grows with the "
            "choices of the environment, the instances and the versions: use fewer --members, "
            "a smaller --domain, a smaller --horizon or a coarser --step")

_UNIT_MS = {"ms": 1, "s": 1000, "min": 60000, "h": 3600000}


AGGREGATES = ("sum", "count", "min", "max", "avg")


class NormsCheckError(NormsEvalError):
    """The request cannot be translated (a usage error or a refusal)."""


class NormsCheckTimeout(NormsCheckError):
    """The model checker did not finish within --timeout."""


# === Expressions of the generated model ====================================
#
# A small expression language with folding constructors: ('c', value),
# ('v', name), ('not', a), ('and', ...), ('or', ...), ('eq', a, k),
# ('le', a, k), ('add', a, k), ('ite', c, a, b). The step of one
# observation is built with the environment's choice as constants, so that
# most conditions fold away and every command of the model stays small.

class _IR:
    """Folding constructors of interned expressions: equal expressions are
    the same object, so that the memo tables below key on identity."""

    def __init__(self, ranges: dict, bools: set) -> None:
        self.ranges = ranges          # int variable -> (lo, hi)
        self.bools = bools            # boolean variables
        self._table: dict = {}
        self._subst_memo: dict = {}
        self._split_memo: dict = {}
        self.TRUE = self.C(True)
        self.FALSE = self.C(False)

    def _mk(self, key: tuple, node: tuple) -> tuple:
        got = self._table.get(key)
        if got is None:
            self._table[key] = got = node
        return got

    def C(self, v: Any) -> tuple:
        return self._mk(("c", type(v).__name__, v), ("c", v))

    def V(self, name: str) -> tuple:
        return self._mk(("v", name), ("v", name))

    def node(self, kind: str, *args: Any) -> tuple:
        key = (kind,) + tuple(id(a) if isinstance(a, tuple) else a for a in args)
        return self._mk(key, (kind,) + args)

    def is_bool(self, e: tuple) -> bool:
        k = e[0]
        if k == "c":
            return isinstance(e[1], bool)
        if k == "v":
            return e[1] in self.bools
        if k == "ite":
            return self.is_bool(e[2])
        return k in ("not", "and", "or", "eq", "le")

    def NOT(self, a: tuple) -> tuple:
        if a[0] == "c":
            return self.C(not a[1])
        if a[0] == "not":
            return a[1]
        return self.node("not", a)

    def _junction(self, kind: str, xs: tuple) -> tuple:
        unit = kind == "and"            # the neutral constant
        out: list = []
        ids: set = set()
        for x in xs:
            if x[0] == "c":
                if x[1] is not unit:
                    return self.C(not unit)
                continue
            for i in (x[1:] if x[0] == kind else (x,)):
                if id(i) not in ids:
                    ids.add(id(i))
                    out.append(i)
        if not out:
            return self.C(unit)
        if len(out) == 1:
            return out[0]
        for i in out:
            if i[0] == "not" and (id(i[1]) in ids or (
                    i[1][0] == kind and all(id(z) in ids for z in i[1][1:]))):
                return self.C(not unit)
        out.sort(key=id)
        return self.node(kind, *out)

    def AND(self, *xs: tuple) -> tuple:
        return self._junction("and", xs)

    def OR(self, *xs: tuple) -> tuple:
        return self._junction("or", xs)

    def EQ(self, a: tuple, k: int) -> tuple:
        if a[0] == "c":
            return self.C(a[1] == k)
        if a[0] == "ite":
            return self.ITE(a[1], self.EQ(a[2], k), self.EQ(a[3], k))
        if a[0] == "v" and a[1] in self.ranges:
            lo, hi = self.ranges[a[1]]
            if not lo <= k <= hi:
                return self.FALSE
        return self.node("eq", a, k)

    def IN(self, a: tuple, ks) -> tuple:
        return self.OR(*(self.EQ(a, k) for k in ks))

    def LE(self, a: tuple, k: int) -> tuple:
        if a[0] == "c":
            return self.C(a[1] <= k)
        if a[0] == "ite":
            return self.ITE(a[1], self.LE(a[2], k), self.LE(a[3], k))
        if a[0] == "add":
            return self.LE(a[1], k - a[2])
        if a[0] == "v" and a[1] in self.ranges:
            lo, hi = self.ranges[a[1]]
            if k >= hi:
                return self.TRUE
            if k < lo:
                return self.FALSE
        return self.node("le", a, k)

    def ADD(self, a: tuple, k: int) -> tuple:
        if k == 0:
            return a
        if a[0] == "c":
            return self.C(a[1] + k)
        if a[0] == "add":
            return self.ADD(a[1], a[2] + k)
        if a[0] == "ite":
            return self.ITE(a[1], self.ADD(a[2], k), self.ADD(a[3], k))
        return self.node("add", a, k)

    def ITE(self, c: tuple, a: tuple, b: tuple) -> tuple:
        if c[0] == "c":
            return a if c[1] else b
        if a is b:
            return a
        if self.is_bool(a):
            if a is self.TRUE:
                return self.OR(c, b)
            if a is self.FALSE:
                return self.AND(self.NOT(c), b)
            if b is self.TRUE:
                return self.OR(self.NOT(c), a)
            if b is self.FALSE:
                return self.AND(c, a)
        if c[0] == "not":
            return self.node("ite", c[1], b, a)
        return self.node("ite", c, a, b)

    # --- substitution and case splits ---------------------------------------

    def subst(self, e: tuple, target: tuple, value: tuple) -> tuple:
        """e with the node `target` (a variable or a test) replaced by the
        constant node `value`, folded."""
        return self.subst_many(e, {id(target): value}, (id(target), id(value)))

    def subst_many(self, e: tuple, mapping: dict, tag: Any) -> tuple:
        """e with every node whose id is a key of `mapping` replaced by its
        value, folded. Results are kept per `tag` (which names the
        mapping), so that the same expression under the same substitution
        is rewritten once."""
        memo = self._subst_memo.setdefault(tag, {})
        return self._subst(e, mapping, memo)

    def _subst(self, e: tuple, mapping: dict, memo: dict) -> tuple:
        got = mapping.get(id(e))
        if got is not None:
            return got
        got = memo.get(id(e))
        if got is not None:
            return got
        k = e[0]
        if k in ("c", "v"):
            r = e
        elif k == "not":
            r = self.NOT(self._subst(e[1], mapping, memo))
        elif k in ("and", "or"):
            args = [self._subst(x, mapping, memo) for x in e[1:]]
            r = self.AND(*args) if k == "and" else self.OR(*args)
        elif k == "eq":
            r = self.EQ(self._subst(e[1], mapping, memo), e[2])
        elif k == "le":
            r = self.LE(self._subst(e[1], mapping, memo), e[2])
        elif k == "add":
            r = self.ADD(self._subst(e[1], mapping, memo), e[2])
        else:
            r = self.ITE(self._subst(e[1], mapping, memo), self._subst(e[2], mapping, memo),
                         self._subst(e[3], mapping, memo))
        memo[id(e)] = r
        return r

    def size(self, e: tuple, memo: dict | None = None) -> int:
        """The number of nodes of e written as a tree."""
        if memo is None:
            memo = {}
        got = memo.get(id(e))
        if got is None:
            if e[0] in ("c", "v"):
                got = 1
            else:
                got = 1 + sum(self.size(x, memo) for x in e[1:] if isinstance(x, tuple))
            memo[id(e)] = got
        return got

    def _splittable(self, e: tuple, out: dict, seen: set) -> None:
        """The variables with few values and the tests of the other
        variables in e, with their number of occurrences."""
        if id(e) in seen:
            return
        seen.add(id(e))
        k = e[0]
        if k == "v":
            n = e[1]
            if n in self.bools or (n in self.ranges and self.ranges[n][1] - self.ranges[n][0] <= 8):
                out[e] = out.get(e, 0) + 1
            return
        if k in ("eq", "le") and e[1][0] == "v" and e[1][1] in self.ranges \
                and self.ranges[e[1][1]][1] - self.ranges[e[1][1]][0] > 7:
            out[e] = out.get(e, 0) + 1
            return
        if k != "c":
            for x in e[1:]:
                if isinstance(x, tuple):
                    self._splittable(x, out, seen)

    def split(self, e: tuple, limit: int = 24, order: list | None = None) -> tuple:
        """e rewritten as a case distinction on its small variables (the
        state of a norm, flags) and on the tests of its other variables,
        while it is larger than `limit` nodes: each case is folded, which
        removes the repeated tests that the steps of F.5 make on the same
        variable."""
        key = (id(e), limit, tuple(order or ()))
        got = self._split_memo.get(key)
        if got is None:
            got = self._split_memo[key] = self._split(e, limit, order)
        return got

    def _split(self, e: tuple, limit: int, order: list | None) -> tuple:
        if self.size(e) <= limit:
            return e
        found: dict = {}
        self._splittable(e, found, set())
        if not found:
            return e
        pick = None
        for name in order or ():
            node = self.V(name)
            if node in found:
                pick = node
                break
        if pick is None:
            pick = max(found, key=lambda n: (found[n], -self.size(n)))
        if pick[0] != "v" or pick[1] in self.bools:
            return self.ITE(pick, self.split(self.subst(e, pick, self.TRUE), limit, order),
                            self.split(self.subst(e, pick, self.FALSE), limit, order))
        lo, hi = self.ranges[pick[1]]
        cases: list = []
        for v in range(lo, hi + 1):
            r = self.split(self.subst(e, pick, self.C(v)), limit, order)
            for c in cases:
                if c[1] is r:
                    c[0].append(v)
                    break
            else:
                cases.append([[v], r])
        out = cases[-1][1]
        for vals, r in reversed(cases[:-1]):
            rest = [v for v in range(lo, hi + 1) if v not in vals]
            test = (self.IN(pick, vals) if len(vals) <= len(rest)
                    else self.NOT(self.IN(pick, rest)))
            out = self.ITE(test, r, out)
        return out

    def _facts(self, c: tuple, truth: bool) -> list:
        """(node, constant) pairs that hold when the test c has the value
        `truth`."""
        if c[0] == "not":
            return self._facts(c[1], not truth)
        if c[0] == "v":
            return [(c, self.C(truth))]
        if c[0] == "eq":
            out = [(c, self.C(truth))]
            if truth and c[1][0] == "v":
                out.append((c[1], self.C(c[2])))
            return out
        if c[0] == "le":
            return [(c, self.C(truth))]
        if (c[0] == "or" and not truth) or (c[0] == "and" and truth):
            out = []
            for x in c[1:]:
                out += self._facts(x, truth)
            return out
        return [(c, self.C(truth))]

    def tidy(self, e: tuple, memo: dict | None = None) -> tuple:
        """e with the tests that an enclosing condition decides folded."""
        if memo is None:
            memo = {}
        got = memo.get(id(e))
        if got is not None:
            return got
        k = e[0]
        if k in ("c", "v"):
            r = e
        elif k == "ite":
            c = self.tidy(e[1], memo)
            a, b = self.tidy(e[2], memo), self.tidy(e[3], memo)
            for node, val in self._facts(c, True):
                a = self.subst(a, node, val)
            for node, val in self._facts(c, False):
                b = self.subst(b, node, val)
            r = self.ITE(c, a, b)
        elif k == "not":
            r = self.NOT(self.tidy(e[1], memo))
        elif k in ("and", "or"):
            args = [self.tidy(x, memo) for x in e[1:]]
            r = self.AND(*args) if k == "and" else self.OR(*args)
        elif k == "eq":
            r = self.EQ(self.tidy(e[1], memo), e[2])
        elif k == "le":
            r = self.LE(self.tidy(e[1], memo), e[2])
        else:
            r = self.ADD(self.tidy(e[1], memo), e[2])
        memo[id(e)] = r
        return r


def prism_expr(e: tuple) -> str:
    """An expression in PRISM syntax, fully parenthesized."""
    k = e[0]
    if k == "c":
        v = e[1]
        return ("true" if v else "false") if isinstance(v, bool) else str(v)
    if k == "v":
        return e[1]
    if k == "not":
        return f"!({prism_expr(e[1])})" if e[1][0] not in ("v", "c") else f"!{prism_expr(e[1])}"
    if k in ("and", "or"):
        sep = " & " if k == "and" else " | "
        return sep.join(_paren(x) for x in e[1:])
    if k == "eq":
        return f"{prism_expr(e[1])}={e[2]}"
    if k == "le":
        return f"{prism_expr(e[1])}<={e[2]}"
    if k == "add":
        sign = "+" if e[2] >= 0 else "-"
        return f"{_paren(e[1])}{sign}{abs(e[2])}"
    if k == "ite":
        return f"({prism_expr(e[1])} ? {prism_expr(e[2])} : {prism_expr(e[3])})"
    raise ValueError(k)


def _paren(e: tuple) -> str:
    text = prism_expr(e)
    return text if e[0] in ("c", "v", "ite") else f"({text})"


# === Translation ===========================================================

@dataclass
class Atom:
    """A concrete message event of the instance."""
    sender: str
    receiver: str
    name: str
    args: tuple

    def message(self) -> dict:
        return {"from": self.sender, "to": self.receiver, "name": self.name,
                "args": list(self.args)}

    def text(self) -> str:
        args = f"({', '.join(json_text(a) for a in self.args)})" if self.args else ""
        return f"{self.sender} -> {self.receiver} : {self.name}{args}"


@dataclass
class Term:
    """A trigger, until, content, when or if of the contract, translated:
    kind is "event" (an institutional event), "message" (the atoms that
    match it), "pred" (a state predicate), or "temporal" (a bounded
    temporal operator over state predicates)."""
    text: str
    kind: str
    event: str | None = None
    atoms: tuple = ()
    pred: int | None = None
    op: str | None = None
    window: int = 0                    # steps of the interval of a temporal operator
    preds: tuple = ()                  # p (within, throughout) or (p, q) (until)
    window_ms: int = 0


@dataclass
class NormInfo:
    index: int
    id: str
    kind: str
    status: str = "model"              # model | frozen | untranslated
    reason: str = ""
    trigger: Term | None = None
    until: Term | None = None
    content: Term | None = None
    deadline_ms: int | None = None
    deadline: int | None = None        # steps
    reparation: str | None = None
    starts: bool = False               # active at the start of the instance
    bearer: Any = None                 # ("exact", actor) | ("set", name) | None
    effects: list = field(default_factory=list)
    content_text: str = ""
    px: str = ""                       # prefix of the instance ("k2_"), "" for one
    inst: int = 0                      # index of the instance
    slot: int = -1                     # its variables, the same in every version
    on_amendment: str = "carry_over"
    transform_to: str | None = None

    @property
    def judged(self) -> bool:
        return self.kind in ("obligation", "prohibition")

    @property
    def stem(self) -> str:
        index = self.slot if self.slot >= 0 else self.index
        return f"{self.px}{index}_{re.sub(r'[^A-Za-z0-9_]', '_', self.id)}"

    @property
    def uid(self) -> str:
        """The norm in the report: its id, and its instance when there are
        several ("acknowledge@2")."""
        return f"{self.id}@{self.inst + 1}" if self.px else self.id


@dataclass
class RuleInfo:
    creates: str
    when: Term | None
    cond: int | None
    status: str = "model"              # model | skipped (not evaluable) | untranslated
    reason: str = ""


@dataclass
class Quantity:
    key: str
    consts: set = field(default_factory=set)    # literals compared with it
    reps: list = field(default_factory=list)    # one value per class
    atoms: set = field(default_factory=set)     # (op, literal), ("in", literals), ("truth",)
    raw: bool = False      # its value is used otherwise than in an atom
    domain: tuple | None = None     # (lo, hi) of --domain, for arithmetic
    order: int = 1         # quantities compared with each other in its group
    list_order: int = 1    # quantities ordered (<, <=, >, >=) against each other


class _Name(str):
    """A name without a value used as a literal (a symbolic value)."""


@dataclass
class Pred:
    """A state predicate (after desugaring) and the quantities it reads."""
    expr: Any
    text: str
    keys: frozenset
    owner: Any = None          # the Translator of its instance


# Lists for quantities ordered against each other. Two lists compare
# lexicographically in norms-eval (Python), and two elements of different
# kinds make them not comparable (undefined): [1, "a"] < [1, 2] is
# undefined while [1] < [1, "a"] and [1] < [1, 2] are true. The pairwise
# outcomes (equal, less, greater, not comparable) of n lists form one of
# 4 patterns for n = 2 and 29 for n = 3; these lists realise all of them
# (an exhaustive enumeration of lists of length at most 2 over 0, 1, 2,
# "a", "b", two objects and null, which suffices because the outcomes of
# three lists are decided at the at most two points where their elements
# first differ, F.12).
_LIST_VALUES = {2: [[], [0], ["a"]],
                3: [[], [0], [1], ["a"], ["b"], [{"d": 0}], [0, 0], [0, "a"]]}


def _list_values(q: Any) -> list:
    if q.list_order not in _LIST_VALUES:
        raise NormsCheckError(
            f"{q.key} is ordered (<, <=, >, >=) against {q.list_order - 1} other quantities; "
            f"norms-check translates such orders of at most 3 quantities, since a trace may "
            f"give them lists, which are not always comparable")
    return [list(v) for v in _LIST_VALUES[q.list_order]]


# Numbers of the classes of values (F.12) are exact decimals: norms-eval
# reads a number of a trace exactly as it is written (F.5), so a value of a
# class is written into a witness with the digits of its exact value, and
# the evaluator of norms-eval reads it back as the same value.

def _is_decimal(f: Fraction) -> bool:
    d = f.denominator
    for p in (2, 5):
        while d % p == 0:
            d //= p
    return d == 1


def _decimal_text(f: Fraction) -> str:
    """The exact decimal text of a number with a finite decimal expansion."""
    if f.denominator == 1:
        return str(f.numerator)
    k = 0
    while (10 ** k) % f.denominator:
        k += 1
    n = abs(f.numerator) * (10 ** k // f.denominator)
    digits = str(n).rjust(k + 1, "0")
    text = f"{digits[:-k]}.{digits[-k:]}".rstrip("0")
    return ("-" if f < 0 else "") + text


def _num(f: Fraction) -> Any:
    """A value of a class: an int, or a float that keeps its exact decimal
    text (norms_eval reads it exactly, and `json_text` writes it)."""
    from .norms_eval import _Written
    f = Fraction(f)
    if f.denominator == 1:
        return int(f)
    text = _decimal_text(f)
    w = _Written(text)
    w.text = text
    return w


def exact_value(v: Any) -> Fraction:
    """The exact value of a number of a class or a literal."""
    text = getattr(v, "text", None)
    if text is not None:
        return Fraction(Decimal(text))
    if isinstance(v, float):
        return Fraction(repr(v))
    return Fraction(v)


def _decimals_between(a: Fraction, b: Fraction, k: int) -> list:
    """k distinct short decimals strictly between a and b (a < b)."""
    if b - a >= k + 1 and a.denominator == 1:
        return [int(a) + j for j in range(1, k + 1)]
    out: list = []
    for j in range(1, k + 1):
        t = a + (b - a) * Fraction(j, k + 1)
        for digits in range(0, 80):
            c = Fraction(round(t * 10 ** digits), 10 ** digits)
            if a < c < b and c not in out:
                out.append(c)
                break
    return [_num(c) for c in sorted(out)]


def _report_value(v: Any) -> Any:
    """A value of a class for the JSON report: a number that a float holds
    exactly as a float, else its exact decimal text."""
    if isinstance(v, float) and getattr(v, "text", None) is not None:
        return float(v) if Fraction(repr(float(v))) == exact_value(v) else v.text
    return v


def json_text(v: Any) -> str:
    """JSON text of a value, numbers of the classes with their exact
    decimal digits (json.dumps would round them to a float)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        text = getattr(v, "text", None)
        return text if text is not None else json.dumps(v)
    if isinstance(v, Fraction):
        return _decimal_text(v) if _is_decimal(v) else json.dumps(float(v))
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(json_text(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))}: {json_text(x)}"
                               for k, x in v.items()) + "}"
    return json.dumps(v)


def _expr_text(expr: Any) -> str:
    from .norms import item_text
    try:
        return item_text(expr)
    except Exception:
        return repr(expr)


@dataclass
class Choice:
    """One choice of the environment at one observation (without the end)."""
    index: int
    messages: tuple                    # atom indices, in atom order
    vector: dict                       # predicate id -> "T" | "F" | "U"
    values: dict                       # quantity key -> value (None: no value)


class _Refuse(Exception):
    pass








def _parse_duration_ms(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*(ms|s|min|h)\s*", text)
    if not m:
        raise NormsCheckError(f"--step must be a duration such as 1s or 500ms, got {text!r}")
    ms = int(m.group(1)) * _UNIT_MS[m.group(2)]
    if ms <= 0:
        raise NormsCheckError("--step must be positive")
    return ms


def _parse_assignments(items, what: str) -> dict:
    out: dict = {}
    for item in items or []:
        for part in str(item).split(","):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(-?\d+)", part)
            if not m:
                raise NormsCheckError(f"{what} takes name=integer, got {part!r}")
            out[m.group(1)] = int(m.group(2))
    return out


class Translator:
    """The translation of one contract instance (F.12)."""

    def __init__(self, sos: SoSDefinition, contract: ContractDef, bindings: dict,
                 members: dict, step_ms: int | None, horizon: int | None,
                 domains: dict | None = None) -> None:
        self.sos = sos
        self.contract = contract
        self.domain_patterns = dict(domains or {})     # pattern -> (lo, hi)
        self.links: list = []                           # keys compared with each other
        self.order_links: list = []                     # ... by <, <=, >, >=
        self.notes: list[str] = []
        self.actors = declared_actors(sos)
        self.bindings = dict(bindings)
        self.given_bindings = set(bindings)
        self.symbolic_bounds: dict = {}
        self._setup_bindings(members)
        self.events = {r.creates for r in contract.counts_as if r.creates}
        self.atoms: list[Atom] = []
        self._atom_index: dict = {}
        self.quantities: dict[str, Quantity] = {}
        self.preds: list = []            # resolved predicates
        self._pred_index: dict = {}
        self.edge_preds: set = set()     # predicates whose rising edge is an event
        self._static_engine()
        self.norms = [self._norm(i, d) for i, d in enumerate(contract.norms)]
        self.by_id = {n.id: n for n in self.norms}
        self.rules = [self._rule(k, r) for k, r in enumerate(contract.counts_as)]
        self.items = [self._item(k, e) for k, e in enumerate(contract.assume)]
        self._propagate()
        self.step_request = (step_ms, horizon)

    # --- the instance -------------------------------------------------------

    def _setup_bindings(self, members: dict) -> None:
        texts = [t for d in self.contract.norms
                 for t in (d.bearer, d.counterparty, d.trigger, d.until, d.content) if t]
        texts += [t for r in self.contract.counts_as for t in (r.when, r.if_) if t]
        from .norms import item_text
        texts += [item_text(e) for e in self.contract.assume]
        # variables of quantifiers and comprehensions are not indices of the
        # instance
        bound_vars = set(re.findall(r"(?:exists|for\s+all|for)\s+([A-Za-z_]\w*)\s+in\b",
                                    " ".join(texts)))
        refs = {(s_, v) for s_, v in _bound_refs(self.contract) if v not in bound_vars}
        self.index_refs = refs
        self.index_vars = {v for _s, v in refs if not v.isdigit()}
        for set_name, var in sorted(refs):
            if var not in self.bindings and not var.isdigit():
                decl = self.actors.get(set_name)
                lo = decl[1] if decl and decl[1] is not None else 1
                self.bindings[var] = lo
                self.notes.append(f"index {var} is not bound; it is bound to {lo} "
                                  f"(use --bindings {var}=n)")
        self.members: dict[str, list[int]] = {}
        for name, (indexed, lo, hi, symbolic) in self.actors.items():
            if not indexed:
                continue
            lo = 1 if lo is None else lo
            want = members.get(name)
            if want is not None:
                if want < 1:
                    raise NormsCheckError(f"--members {name}={want}: a set has at least one member")
                if hi is not None and lo + want - 1 > hi:
                    raise NormsCheckError(f"--members {name}={want}: {name} has only "
                                          f"{hi - lo + 1} member(s)")
                top = lo + want - 1
                if hi is not None and top < hi:
                    self._note(f"--members {name}={want}: {name} is declared with "
                               f"{hi - lo + 1} members; the answers (\"violable: no\" among "
                               f"them) hold for instances with {want} of them only")
            elif hi is not None:
                top = hi
            else:
                used = [v for (s, var), v in ((k, self.bindings.get(k[1])) for k in refs)
                        if s == name and isinstance(v, int)]
                top = max([lo] + used)
                self._note(f"{name}[{lo}..{symbolic}] has {top - lo + 1} member(s) in the "
                           f"instance because --members does not give its size; the answers "
                           f"hold for that size only: give --members {name}=n to check more")
            self.members[name] = list(range(lo, top + 1))
        for name in members:
            if name not in self.members:
                raise NormsCheckError(f"--members {name}: {name} is not a declared set of actors")
        for set_name, var in sorted(refs):
            v = self.bindings.get(var)
            if set_name in self.members and isinstance(v, int) and v not in self.members[set_name]:
                raise NormsCheckError(
                    f"the binding {var}={v} names {set_name}[{v}], which is not among the "
                    f"members of the instance ({set_name}[{self.members[set_name][0]}.."
                    f"{self.members[set_name][-1]}]); use --members {set_name}=n")
        texts = [t for d in self.contract.norms
                 for t in (d.bearer, d.counterparty, d.trigger, d.until, d.content) if t]
        texts += [t for r in self.contract.counts_as for t in (r.when, r.if_) if t]
        for name in sorted(self.given_bindings):
            if not any(re.search(rf"\b{re.escape(name)}\b", t) for t in texts):
                self.notes.append(f"--bindings {name}: the contract does not use the name "
                                  f"{name}; the binding has no effect")
        # a set A[lo..N] has n members in the instance: N is bound to its last
        # index, as a trace that binds N does (F.5), so that quantifiers and
        # aggregates over the set range over these members
        for name, (indexed, lo, hi, symbolic) in self.actors.items():
            if indexed and symbolic and name in self.members:
                top = self.members[name][-1]
                if self.bindings.get(symbolic, top) != top:
                    raise NormsCheckError(
                        f"{symbolic} is bound to {self.bindings[symbolic]}, but the instance "
                        f"has {name}[{self.members[name][0]}..{top}]; give --members or "
                        f"--bindings consistently")
                if symbolic not in self.bindings:
                    self.bindings[symbolic] = top
                    self.symbolic_bounds[symbolic] = top
        self.universe: list[str] = []
        for name, (indexed, *_rest) in self.actors.items():
            if indexed:
                self.universe += [f"{name}[{i}]" for i in self.members[name]]
            else:
                self.universe.append(name)

    def _static_engine(self) -> None:
        """What `norms_eval` decides before reading a trace: the norms it
        does not judge (frozen), the counts-as rules and contract items it
        does not evaluate, and the targets of reparations and powers."""
        obs = Observation(t=0.0, values={}, messages=[], state=None, state_after=None)
        engine = _Engine(self.contract, Trace(observations=[obs], bindings=self.bindings),
                         None, self.actors)
        self.frozen = {n.d.id: n.frozen for n in engine.norms if n.frozen}
        self.rule_errors = []
        for _r, when, cond in engine.rules:
            self.rule_errors.append(bool(when.error or (cond is not None and cond.error)))
        self.item_errors = [bool(i["error"]) for i in engine.items["assume"]]
        self.targets = _targets_of(self.contract)

    # --- actors and messages ------------------------------------------------

    def _index_value(self, idx: Any) -> Any:
        """The value of an index as written: an int, or None when unbound."""
        if isinstance(idx, int) and not isinstance(idx, bool):
            return idx
        if isinstance(idx, IntLiteral):
            return idx.value
        if isinstance(idx, ActorRef) and idx.index is None:
            idx = idx.name
        if isinstance(idx, str):
            if idx.isdigit():
                return int(idx)
            if idx in self.bindings:
                return self.bindings[idx]
            return None
        raise _Refuse(f"the index {idx!r} is not translated")

    def _actor_candidates(self, ref: ActorRef) -> list[str]:
        """The concrete actors of the instance that an actor reference of a
        message event matches (the rules of norms_eval._Term._actor_matches)."""
        decl = self.actors.get(ref.name)
        if decl is None:
            return []
        indexed = decl[0]
        idx = ref.index
        if idx is None:
            return [] if indexed else [ref.name]
        if not indexed:
            return []
        members = self.members[ref.name]
        if idx == "*":
            return [f"{ref.name}[{i}]" for i in members]
        if isinstance(idx, RangeExpr):
            if isinstance(idx.start, int) and isinstance(idx.end, int):
                return [f"{ref.name}[{i}]" for i in members if idx.start <= i <= idx.end]
            return [f"{ref.name}[{i}]" for i in members]
        v = self._index_value(idx)
        if v is None:
            self.notes.append(f"the index of {ref.name}[{idx}] is not bound: it matches "
                              f"every member, as in norms-eval")
            return [f"{ref.name}[{i}]" for i in members]
        lo, hi = decl[1], decl[2]
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return []
        return [f"{ref.name}[{v}]"] if v in members else []

    def _arg_value(self, a: Any) -> Any:
        if isinstance(a, FloatLiteral):
            from .norms_eval import _literal_number
            f = Fraction(_literal_number(a))          # exactly as written
            return _num(f) if _is_decimal(f) else float(a.value)
        if isinstance(a, (IntLiteral, BoolLiteral, StringLiteral)):
            return a.value
        if isinstance(a, ActorRef) and a.index is None:
            if a.name in self.bindings:
                return self.bindings[a.name]
            if a.name in ("now", "state"):
                raise _Refuse(f"the argument '{a.name}' of a message event is not translated")
            return a.name
        raise _Refuse("an argument of a message event that is not a literal or a name "
                      "is not translated")

    def _message_term(self, text: str, m: re.Match) -> Term:
        from .parser import _parse_actor_ref_str, parse_expr
        sender = _parse_actor_ref_str(m.group(1).strip())
        receiver = _parse_actor_ref_str(m.group(2).strip())
        name = m.group(3).strip()
        args = None
        try:
            msg = parse_expr(name)
        except Exception:
            msg = None
        if isinstance(msg, FunctionCall):
            name = msg.name
            args = tuple(self._arg_value(a) for a in msg.args) if msg.args else None
        atoms = []
        for s in self._actor_candidates(sender):
            for r in self._actor_candidates(receiver):
                atoms.append((s, r, name, args))
        return Term(text=text, kind="message", atoms=tuple(atoms))

    # --- predicates -----------------------------------------------------------
    #
    # A predicate is evaluated by the evaluator of norms-eval itself, on one
    # valuation of the quantities it reads (`_oracle`). The translation
    # only decides which quantities a predicate reads and which values
    # stand for all others: the literals compared with a quantity cut each
    # type of value (numbers, texts) into classes on which every
    # comparison of the predicate has the same value (F.12).

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def _quantity(self, key: str) -> Quantity:
        q = self.quantities.get(key)
        if q is None:
            q = self.quantities[key] = Quantity(key)
        return q

    def _scope_values(self, scope: dict, names: set) -> list:
        """The assignments of the quantifier variables `names`."""
        names = sorted(n for n in names if n in scope)
        out = [{}]
        for n in names:
            out = [dict(a, **{n: v}) for a in out for v in scope[n][1]]
        return out

    def _index_values(self, idx: Any, scope: dict) -> list:
        """The integer values an index expression takes, over the variables
        of the enclosing quantifiers; None for a value that is not an
        integer member index."""
        if isinstance(idx, int) and not isinstance(idx, bool):
            return [idx]
        if isinstance(idx, IntLiteral):
            return [idx.value]
        if isinstance(idx, str):
            idx = ActorRef(name=idx)
        if isinstance(idx, ActorRef) and idx.index is None:
            n = idx.name
            if n.isdigit():
                return [int(n)]
            if n in scope:
                return list(scope[n][1])
            if n in self.bindings:
                return [self.bindings[n]]
            raise _Refuse(f"the index {n} is not bound (use --bindings {n}=n)")
        if isinstance(idx, BinaryOp) and idx.op in ("+", "-", "*"):
            out = []
            for a in self._index_values(idx.left, scope):
                for b in self._index_values(idx.right, scope):
                    out.append(a + b if idx.op == "+" else a - b if idx.op == "-" else a * b)
            return out
        raise _Refuse("an index that is not an integer, a bound name or a quantifier variable "
                      "is not translated")

    def _member_keys(self, e: MemberAccess, scope: dict) -> set:
        obj = e.obj
        if obj.index is None:
            if obj.name in scope:
                group, values = scope[obj.name]
                if group is None:
                    raise _Refuse(f"'{obj.name}' ranges over integers, so "
                                  f"'{obj.name}.{e.member}' is not evaluable")
                return {f"{group}[{v}].{e.member}" for v in values}
            if obj.name == "request":
                raise _Refuse("request fields are not translated")
            return {f"{obj.name}.{e.member}"}
        if obj.index == "*" or isinstance(obj.index, RangeExpr):
            raise _Refuse("a wildcard or range reference here is not translated")
        members = self.members.get(obj.name)
        keys = set()
        for v in self._index_values(obj.index, scope):
            if members is None or v in members:
                keys.add(f"{obj.name}[{v}].{e.member}")
        return keys

    def _domain_values(self, dom: Any, comprehension: bool) -> tuple:
        """(set or None, values) of the domain of a quantifier or
        comprehension in the instance."""
        if isinstance(dom, RangeExpr):
            lo = self._index_values(dom.start, {})[0]
            hi = self._index_values(dom.end, {})[0]
            return None, list(range(lo, hi + 1))
        if isinstance(dom, ActorRef):
            members = self.members.get(dom.name)
            if members is None:
                raise _Refuse(f"{dom.name} is not a declared set of actors")
            # the domain is the set as norms-eval sees it: its declared
            # members when its bounds are integers, else the members of the
            # instance (whose last index the trace binds); members beyond
            # --members have no value in the model
            decl = self.actors[dom.name]
            if decl[2] is not None and decl[2] > members[-1]:
                self._note(
                    f"{dom.name}[{members[-1] + 1}..{decl[2]}] belong to the domain of a "
                    f"quantifier or aggregate but not to the instance (--members): in the model "
                    f"they have no value")
            if isinstance(dom.index, RangeExpr):
                lo = self._index_values(dom.index.start, {})[0]
                end = dom.index.end
                hi = (self._index_values(end, {})[0]
                      if not (isinstance(end, str) and end not in self.bindings
                              and not end.isdigit()) else members[-1])
                return dom.name, [m for m in members if lo <= m <= hi]
            return dom.name, list(members)
        raise _Refuse("the domain of a quantifier or comprehension is not a set or a range")

    def _lit(self, value: Any) -> tuple:
        return ("lit", (value,))

    def _shape(self, e: Any, scope: dict, info: dict) -> tuple:
        """The shape of an expression: ("lit", values), ("qty", keys),
        ("count", n), ("minmax", keys), ("num", keys) for arithmetic and
        sum/avg, or ("bool",) for a condition. Collects the literals
        compared with each quantity in info["consts"] and the quantities
        that arithmetic or another quantity is compared with in
        info["domain"]."""
        from .ast_nodes import Comprehension, QuantifiedExpr
        if isinstance(e, BoolLiteral):
            return self._lit(e.value)
        if isinstance(e, FloatLiteral):
            from .norms_eval import _literal_number
            return self._lit(Fraction(_literal_number(e)))      # as written, exactly
        if isinstance(e, (IntLiteral, StringLiteral)):
            return self._lit(e.value)
        if isinstance(e, UnaryOp) and e.op == "-":
            s = self._shape(e.operand, scope, info)
            if s[0] == "lit" and all(isinstance(v, (int, float, Fraction))
                                     and not isinstance(v, bool) for v in s[1]):
                return ("lit", tuple(-v for v in s[1]))
            return ("num", self._keys(s))
        if isinstance(e, UnaryOp) and e.op == "NOT":
            self._cond(e.operand, scope, info)
            return ("bool",)
        if isinstance(e, ActorRef) and e.index is None:
            n = e.name
            if n in scope:
                return ("lit", tuple(scope[n][1]))
            if n in self.bindings:
                return self._lit(self.bindings[n])
            if n == "now":
                raise _Refuse("'now' is not translated")
            if n == "state" and self.contract.lifecycle is not None:
                raise _Refuse("'state' (the lifecycle state) is not translated")
            return self._lit(_Name(n))              # a name without a value
        if isinstance(e, ActorRef):
            return self._lit(_Name(e.name))
        if isinstance(e, MemberAccess):
            keys = self._member_keys(e, scope)
            for k in keys:
                self._quantity(k)
            info["keys"].update(keys)
            return ("qty", frozenset(keys))
        if isinstance(e, BinaryOp) and e.op in ("AND", "OR"):
            self._cond(e.left, scope, info)
            self._cond(e.right, scope, info)
            return ("bool",)
        if isinstance(e, BinaryOp) and e.op == "==" and isinstance(e.right, FunctionCall) \
                and e.right.name == "__in__":
            left = self._shape(e.left, scope, info)
            items = [self._shape(a, scope, info) for a in e.right.args]
            for it in items:
                self._compare(left, it, info, None)
            if left[0] == "qty" and all(it[0] == "lit" for it in items):
                vals = tuple(v for it in items for v in it[1])
                for k in left[1]:
                    info["atoms"].setdefault(k, set()).add(("in", vals))
            return ("bool",)
        if isinstance(e, BinaryOp) and e.op in ("==", "!=", "<", "<=", ">", ">="):
            self._compare(self._shape(e.left, scope, info), self._shape(e.right, scope, info),
                          info, e.op)
            return ("bool",)
        if isinstance(e, BinaryOp) and e.op in ("+", "-", "*", "/"):
            a, b = self._shape(e.left, scope, info), self._shape(e.right, scope, info)
            if a[0] == "lit" and b[0] == "lit":
                vals = []
                for x in a[1]:
                    for y in b[1]:
                        try:
                            if isinstance(x, (bool, str)) or isinstance(y, (bool, str)):
                                raise TypeError
                            x, y = Fraction(x), Fraction(y)       # exact, as norms-eval
                            vals.append(x + y if e.op == "+" else x - y if e.op == "-"
                                        else x * y if e.op == "*" else x / y)
                        except Exception:
                            vals.append(None)
                return ("arith-lit", tuple(vals))
            info["raw"].update(self._keys(a) | self._keys(b))
            return ("num", self._keys(a) | self._keys(b))
        if isinstance(e, QuantifiedExpr):
            group, values = self._domain_values(e.domain, False)
            self._cond(e.predicate, {**scope, e.variable: (group, values)}, info)
            return ("bool",)
        if isinstance(e, FunctionCall) and e.name in AGGREGATES and len(e.args) == 1 \
                and isinstance(e.args[0], Comprehension):
            comp = e.args[0]
            group, values = self._domain_values(comp.domain, True)
            el = self._shape(comp.element, {**scope, comp.variable: (group, values)}, info)
            if e.name == "count":
                if el[0] == "qty":
                    for k in el[1]:
                        info["atoms"].setdefault(k, set()).add(("truth",))
                return ("count", len(values))
            info["raw"].update(self._keys(el))
            if e.name in ("min", "max") and el[0] in ("qty", "lit"):
                return ("minmax", self._keys(el)) if el[0] == "qty" else el
            return ("num", self._keys(el))
        if isinstance(e, FunctionCall) and e.name in TEMPORAL_OPERATORS:
            raise _Refuse("a temporal operator is translated only as the whole content of an "
                          "obligation or prohibition or as a whole assume: item")
        what = (f"the function call {e.name}()" if isinstance(e, FunctionCall)
                else f"{type(e).__name__}")
        raise _Refuse(f"{what} in a predicate is not translated")

    @staticmethod
    def _keys(s: tuple) -> frozenset:
        return frozenset(s[1]) if s[0] in ("qty", "minmax", "num") else frozenset()

    def _cond(self, e: Any, scope: dict, info: dict) -> None:
        s = self._shape(e, scope, info)
        if s[0] in ("qty",):
            for k in s[1]:
                info["consts"].setdefault(k, set()).add(0)       # truth: zero or not
                info["atoms"].setdefault(k, set()).add(("truth",))
        elif s[0] in ("num", "minmax"):
            info["domain"].update(s[1])

    _FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}

    def _compare(self, a: tuple, b: tuple, info: dict, op: str | None) -> None:
        """Record what a comparison of two shapes needs: the literals compared
        with a quantity, the comparisons themselves (atoms), and the
        quantities that need a domain."""
        for x, y, o in ((a, b, op), (b, a, self._FLIP.get(op))):
            if x[0] in ("qty", "minmax"):
                if y[0] in ("lit", "arith-lit"):
                    for k in x[1]:
                        info["consts"].setdefault(k, set()).update(y[1])
                        if x[0] == "qty" and o is not None:
                            info["atoms"].setdefault(k, set()).update(
                                (o, v) for v in y[1] if v is not None)
                elif y[0] == "count":
                    for k in x[1]:
                        info["consts"].setdefault(k, set()).update(range(y[1] + 1))
                        if x[0] == "qty" and o is not None:
                            info["atoms"].setdefault(k, set()).update(
                                (o, v) for v in range(y[1] + 1))
                elif y[0] in ("qty", "minmax"):
                    # an order or equality between quantities: exact with
                    # enough values between the literals (F.12)
                    info["links"].append(frozenset(x[1]) | frozenset(y[1]))
                    if o in ("<", "<=", ">", ">=") and x[0] == y[0] == "qty":
                        # lists of a trace are ordered too, and not always
                        # comparable: see _list_values
                        info["order_links"].append(frozenset(x[1]) | frozenset(y[1]))
                    info["raw"].update(x[1])
                    info["raw"].update(y[1])
                elif y[0] == "num":
                    info["domain"].update(x[1])
                    info["domain"].update(y[1])
            elif x[0] == "num":
                info["domain"].update(x[1])
                info["domain"].update(self._keys(y))

    def _pred_id(self, expr: Any) -> int:
        """The id of a state predicate (its AST after desugaring)."""
        key = repr(expr)
        if key in self._pred_index:
            return self._pred_index[key]
        info: dict = {"consts": {}, "domain": set(), "keys": set(), "atoms": {}, "raw": set(),
                      "links": [], "order_links": []}
        self._cond(expr, {}, info)
        if info["domain"]:
            self._needs_domain(info["domain"])
        self.links += info["links"]
        self.order_links += info["order_links"]
        for k, cs in info["consts"].items():
            self._quantity(k).consts.update(c for c in cs if c is not None)
        for k, atoms in info["atoms"].items():
            self._quantity(k).atoms.update(atoms)
        for k in info["raw"] | info["domain"]:
            self._quantity(k).raw = True
        self._pred_index[key] = len(self.preds)
        self.preds.append(Pred(expr=expr, text=_expr_text(expr), keys=frozenset(info["keys"])))
        return self._pred_index[key]

    def domain_of(self, key: str) -> tuple | None:
        """The --domain of a quantity: given for the key or for a pattern
        with [*] in place of the index of a member."""
        for pat, dom in self.domain_patterns.items():
            rx = re.escape(pat).replace(r"\[\*\]", r"\[\d+\]")
            if re.fullmatch(rx, key):
                return dom
        return None

    def _needs_domain(self, keys: set) -> None:
        missing = sorted(k for k in keys if self.domain_of(k) is None)
        if missing:
            raise _Refuse(
                f"arithmetic, sum or avg uses {', '.join(missing)} without a finite domain: "
                f"give one with --domain (such as --domain {missing[0]}=0..10); the answers "
                f"then hold for traces whose numbers for it are integers in that range")
        for k in keys:
            self._quantity(k).domain = self.domain_of(k)

    def _parse_pred(self, text: str) -> Any:
        from .parser import parse_rule
        try:
            expr = desugar_wildcards(parse_rule(text))
        except _Refuse:
            raise
        except Exception:
            raise _Refuse("it is not a predicate (Appendix A.10)")
        return expr

    @staticmethod
    def _check_temporal_position(expr: Any) -> None:
        """Refuse, with the exact reason, a temporal operator that is not the
        whole predicate (norms-eval does not allow one in a trigger, an
        until: or a counts-as rule, so those norms are not judged at all)."""
        from .norms import contains_temporal
        if isinstance(expr, FunctionCall) and expr.name in TEMPORAL_OPERATORS:
            if any(contains_temporal(a) for a in expr.args):
                raise _Refuse(f"a temporal operator nested in {expr.name}() is not translated")
            return
        if contains_temporal(expr):
            what = ("under NOT" if isinstance(expr, UnaryOp)
                    else "combined with AND or OR" if isinstance(expr, BinaryOp)
                    and expr.op in ("AND", "OR") else "inside a comparison or call")
            raise _Refuse(f"a temporal operator {what} is not translated (only a content or an "
                          f"assume: item that is one temporal operator is)")

    def _term(self, text: str, events: set, temporal: bool = False, edge: bool = True) -> Term:
        text = text.strip()
        m = _MESSAGE_EVENT_RE.match(text) if "->" in text else None
        if text in events and _IDENTIFIER_RE.fullmatch(text):
            return Term(text=text, kind="event", event=text)
        if m:
            return self._message_term(text, m)
        expr = self._parse_pred(text)
        self._check_temporal_position(expr)
        if temporal and isinstance(expr, FunctionCall) and expr.name in TEMPORAL_OPERATORS:
            nargs, dpos = TEMPORAL_OPERATORS[expr.name]
            dur = expr.args[dpos]
            ms = dur.value * _UNIT_MS.get(dur.unit, 1000)
            if ms != int(ms):
                raise _Refuse("a duration that is not a whole number of milliseconds")
            preds = tuple(self._pred_id(a) for i, a in enumerate(expr.args)
                          if i != dpos)
            return Term(text=text, kind="temporal", op=expr.name, preds=preds,
                        window_ms=int(ms))
        pid = self._pred_id(expr)
        if edge:
            self.edge_preds.add(pid)
        return Term(text=text, kind="pred", pred=pid)

    # --- norms, rules, items --------------------------------------------------

    def _bearer(self, text: str) -> Any:
        if not text:
            return None
        from .parser import _parse_actor_ref_str
        ref = _parse_actor_ref_str(text)
        if ref.index is None:
            return ("exact", ref.name)
        if isinstance(ref.index, RangeExpr):
            raise _Refuse("a bearer with a range of members is not translated")
        if ref.index == "*":
            return ("set", ref.name)
        v = self._index_value(ref.index)
        if v is None:
            return ("set", ref.name)
        return ("exact", f"{ref.name}[{v}]")

    def _norm(self, i: int, d: Any) -> NormInfo:
        n = NormInfo(index=i, id=d.id, kind=d.kind, content_text=(d.content or "").strip(),
                     on_amendment=d.on_amendment, transform_to=d.transform_to)
        if d.id in self.frozen:
            n.status, n.reason = "frozen", f"norms-eval does not judge it ({self.frozen[d.id]})"
            return n
        n.deadline_ms = d.deadline_ms
        if d.on_violation and d.on_violation.reparation:
            n.reparation = d.on_violation.reparation
        if d.effects:
            n.effects = [(list(d.effects.activate), (INACTIVE,), ACTIVE),
                         (list(d.effects.suspend), (ACTIVE,), SUSPENDED),
                         (list(d.effects.resume), (SUSPENDED,), ACTIVE),
                         (list(d.effects.discharge), (ACTIVE, SUSPENDED), DISCHARGED)]
        n.starts = d.trigger is None and d.id not in self.targets
        try:
            if d.trigger:
                n.trigger = self._term(d.trigger, self.events)
            if d.until:
                n.until = self._term(d.until, self.events)
            if d.kind in ("obligation", "prohibition", "power"):
                n.content = self._term(d.content, self.events, temporal=True, edge=False)
                if d.kind == "power" and n.content.kind not in ("event", "message"):
                    # norms-eval: such a power is never exercised
                    n.content = Term(text=n.content.text, kind="none")
            if d.kind == "power":
                n.bearer = self._bearer(d.bearer)
        except _Refuse as e:
            n.status, n.reason = "untranslated", str(e)
        return n

    def _rule(self, k: int, r: Any) -> RuleInfo:
        info = RuleInfo(creates=r.creates, when=None, cond=None)
        if self.rule_errors[k] or not r.creates:
            info.status, info.reason = "skipped", "norms-eval does not evaluate it"
            return info
        try:
            # norms-eval reads a when with no institutional events (F.3)
            info.when = self._term(r.when, set())
            if r.if_:
                info.cond = self._pred_id(self._parse_pred(r.if_))
        except _Refuse as e:
            info.status, info.reason = "untranslated", str(e)
        return info

    def _item(self, k: int, e: Any) -> dict:
        from .norms import item_text
        from .parser import parse_rule
        item = {"text": item_text(e), "status": "model", "reason": "", "term": None}
        if self.item_errors[k]:
            item["status"], item["reason"] = "skipped", "norms-eval does not evaluate it"
            return item
        try:
            expr = parse_rule(e.value) if isinstance(e, StringLiteral) else e
            expr = desugar_wildcards(expr)
            self._check_temporal_position(expr)
            if isinstance(expr, FunctionCall) and expr.name in TEMPORAL_OPERATORS:
                nargs, dpos = TEMPORAL_OPERATORS[expr.name]
                dur = expr.args[dpos]
                ms = dur.value * _UNIT_MS.get(dur.unit, 1000)
                if ms != int(ms):
                    raise _Refuse("a duration that is not a whole number of milliseconds")
                preds = tuple(self._pred_id(a) for i, a in enumerate(expr.args)
                              if i != dpos)
                item["term"] = Term(text=item["text"], kind="temporal", op=expr.name,
                                    preds=preds, window_ms=int(ms))
            else:
                item["term"] = Term(text=item["text"], kind="pred",
                                    pred=self._pred_id(expr))
        except _Refuse as e:
            item["status"], item["reason"] = "untranslated", str(e)
        except Exception:
            item["status"], item["reason"] = "untranslated", "it is not a predicate"
        return item

    def _propagate(self) -> None:
        """A norm whose behaviour depends on something not translated is not
        translated either (F.12)."""
        changed = True
        while changed:
            changed = False
            unknown = {r.creates for r in self.rules if r.status == "untranslated"}

            def drop(n: NormInfo, why: str) -> None:
                nonlocal changed
                if n.status == "model":
                    n.status, n.reason, changed = "untranslated", why, True

            for n in self.norms:
                if n.status != "model":
                    continue
                for key in ("trigger", "until", "content"):
                    t = getattr(n, key)
                    if t is not None and t.kind == "event" and t.event in unknown:
                        drop(n, f"its {key} is the institutional event {t.event}, created by "
                                f"a counts_as rule that is not translated")
            for n in self.norms:
                if n.status == "untranslated":
                    if n.reparation in self.by_id and n.kind in ("obligation", "prohibition"):
                        drop(self.by_id[n.reparation],
                             f"it is the reparation of {n.id}, which is not translated")
                    if n.kind == "power":
                        for names, _src, _tgt in n.effects:
                            for name in names:
                                if name in self.by_id:
                                    drop(self.by_id[name], f"the power {n.id}, which is not "
                                                           f"translated, has an effect on it")
        for n in self.norms:
            if n.status != "model":
                n.trigger = n.until = n.content = None

    def model_norms(self) -> list[NormInfo]:
        return [n for n in self.norms if n.status == "model"]

    # --- the choices of the environment -----------------------------------------

    def remap_preds(self, offset: int) -> None:
        """Number the predicates of this instance from `offset` on."""
        if not offset:
            return

        def term(t: Any) -> None:
            if t is None:
                return
            if t.pred is not None:
                t.pred += offset
            t.preds = tuple(p + offset for p in t.preds)

        for n in self.norms:
            for t in (n.trigger, n.until, n.content):
                term(t)
        for r in self.rules:
            term(r.when)
            if r.cond is not None:
                r.cond += offset
        for it in self.items:
            term(it.get("term"))
        self.edge_preds = {p + offset for p in self.edge_preds}

    def _reps(self, q: Quantity) -> list:
        """One value per class of values of a quantity (F.12): no value,
        true, false, a list (any value of another JSON type), the numbers
        around the numeric literals compared with it (and 0, the boundary
        of a number used as a condition, and 1 when a boolean literal is
        compared with it, since an ordering compares true as 1), the text
        literals and names compared with it, and one other text."""
        nums = {exact_value(c) for c in q.consts
                if isinstance(c, (int, float, Fraction)) and not isinstance(c, bool)}
        nums.add(Fraction(0))
        if any(isinstance(c, bool) for c in q.consts):
            nums.update((Fraction(0), Fraction(1)))
        cs = sorted(nums)
        k = q.order
        if q.domain is not None:
            numbers = list(range(q.domain[0], q.domain[1] + 1))
        else:
            # k values below, between and above the literals, so that k
            # quantities compared with each other can take every order
            # (computed exactly; a value of a trace is an integer or a float,
            # so the values are k integers or floats strictly inside each gap,
            # or all of them when the gap holds fewer)
            # (exact decimals: a literal that has no finite decimal, such as
            # 1/3 from arithmetic, is not a value of a trace)
            numbers = [math.floor(cs[0]) - j for j in range(k, 0, -1)]
            for i, c in enumerate(cs):
                if _is_decimal(c):
                    numbers.append(_num(c))
                if i + 1 < len(cs):
                    numbers += _decimals_between(c, cs[i + 1], k)
            numbers += [math.ceil(cs[-1]) + j for j in range(1, k + 1)]
        lits = sorted({str(c) for c in q.consts if isinstance(c, str)})
        ordered = k > 1 or any(a[0] in ("<", "<=", ">", ">=") and isinstance(a[1], str)
                               for a in q.atoms)
        if ordered:
            # texts are ordered too: k texts below, between and above the
            # text literals
            texts, others = self._text_grid(q.key, lits, k), []
        else:
            texts, others = lits, []
            while len(others) < k:
                other = "OTHER" + ("_" * len(others))
                while other in texts:
                    other += "_"
                others.append(other)
        # lists are ordered among themselves, objects only equal or not
        objects = (_list_values(q) if q.list_order > 1
                   else [[]] if k == 1 else [[j] for j in range(k)])
        objects += [] if k == 1 else [{}] + [{"k": j} for j in range(1, k)]
        # plain values first, so that a witness shows them when they will do
        reps = ([None] + sorted(numbers, key=lambda x: (abs(exact_value(x)), exact_value(x)))
                + texts + others
                + [True, False] + objects)
        if q.raw:
            return reps
        # the quantity occurs only in comparisons with literals and as a
        # condition: two values that give every such comparison the same
        # value give every predicate the same value
        atoms = sorted(q.atoms, key=repr)
        exprs = [self._atom_expr(a) for a in atoms]
        out, seen = [], set()
        for v in reps:
            sig = tuple(self._oracle(exprs, {"__Q.v": v}, keys=("__Q.v",)))
            if sig not in seen:
                seen.add(sig)
                out.append(v)
        return out

    @staticmethod
    def _text_grid(key: str, lits: list, k: int) -> list:
        """k texts below, at, between and above the text literals, in the
        order of norms-eval (Python's order of strings)."""
        if any("\x00" in t for t in lits):
            raise _Refuse(f"{key} is ordered against a text with the character U+0000, "
                          f"which is not translated")
        out = []
        lo = lits[0] if lits else None
        below = ["\x00" * j for j in range(0, 2 * k + 1)]
        out += [t for t in below if lo is None or t < lo][:k]
        for i, t in enumerate(lits):
            out.append(t)
            nxt = lits[i + 1] if i + 1 < len(lits) else None
            between = [t + "\x00" * j for j in range(1, k + 1)]
            out += [b for b in between if nxt is None or b < nxt]
        top = lits[-1] if lits else ""
        out += [top + "\uffff" * j for j in range(1, k + 1)]
        res = []
        for t in out:
            if t not in res:
                res.append(t)
        return res

    @staticmethod
    def _lit_node(v: Any) -> Any:
        if isinstance(v, bool):
            return BoolLiteral(v)
        if isinstance(v, int):
            return IntLiteral(v)
        if isinstance(v, Fraction):
            if v.denominator == 1:
                return IntLiteral(int(v))
            return FloatLiteral(float(v), text=_decimal_text(v) if _is_decimal(v) else None)
        if isinstance(v, float):
            return FloatLiteral(v, text=getattr(v, "text", None) or repr(v))
        if isinstance(v, _Name):
            return ActorRef(name=str(v))
        return StringLiteral(v)

    def _atom_expr(self, atom: tuple) -> Any:
        q = MemberAccess(obj=ActorRef(name="__Q"), member="v")
        if atom[0] == "truth":
            return q
        if atom[0] == "in":
            return BinaryOp(op="==", left=q, right=FunctionCall(
                name="__in__", args=[self._lit_node(v) for v in atom[1]]))
        return BinaryOp(op=atom[0], left=q, right=self._lit_node(atom[1]))

    def _oracle(self, exprs: list, values: dict, keys=None) -> list:
        """The values (T, F, U) of state predicates for one valuation of the
        quantities, computed by the evaluator of norms-eval."""
        from .norms_eval import UNKNOWN, _Evaluator, _exact
        keys = self.quantities if keys is None else keys
        # as the trace reader of norms-eval stores them: numbers exact
        obs = Observation(t=0.0, values={k: _exact(values.get(k)) for k in keys},
                          messages=[], state=None, state_after=None)
        trace = Trace(observations=[obs], bindings=self.bindings)
        ev = _Evaluator(trace, self.contract.lifecycle is not None, [])
        ev.actors = self.actors
        ev.bound_refs = _bound_refs(self.contract)
        out = []
        for e in exprs:
            v = ev.holds(e, 0, 0)
            out.append("T" if v is True else "F" if v is False else "U")
            assert v is True or v is False or v is UNKNOWN, v
        return out



class Environment:
    """What the contract instances of one model share (F.12): the message
    events, the quantities and their classes of values, the predicates,
    the choices of the environment, the step and the horizon. One
    Translator per instance translates its norms; the environment then
    numbers the message events and predicates of all of them."""

    def __init__(self, trs: list, step_ms: int | None, horizon: int | None) -> None:
        self.trs = trs
        self.notes: list[str] = []
        # predicates: one list, each predicate evaluated with the bindings of
        # its instance
        self.preds: list = []
        for tr in trs:
            offset = len(self.preds)
            for p in tr.preds:
                self.preds.append(Pred(expr=p.expr, text=p.text, keys=p.keys, owner=tr))
            tr.remap_preds(offset)
        # quantities: the same key is the same quantity in every instance
        self.quantities: dict = {}
        self.links: list = []
        self.order_links: list = []
        for tr in trs:
            self.links += tr.links
            self.order_links += tr.order_links
            for key, q in tr.quantities.items():
                g = self.quantities.setdefault(key, Quantity(key))
                g.consts |= q.consts
                g.atoms |= q.atoms
                g.raw = g.raw or q.raw
                g.domain = g.domain or q.domain
        self._collect_atoms()
        self._time(step_ms, horizon)
        self._choices()
        for tr in trs:
            for name in ("atoms", "preds", "quantities", "components", "constant_preds",
                         "choices", "step_ms", "horizon"):
                setattr(tr, name, getattr(self, name))
            for note in self.notes:
                tr._note(note)

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def _value(self, pid: int, vals: dict) -> str:
        p = self.preds[pid]
        return p.owner._oracle([p.expr], vals)[0]

    def _collect_atoms(self) -> None:
        terms = []
        for tr in self.trs:
            for n in tr.model_norms():
                terms += [t for t in (n.trigger, n.until, n.content) if t is not None]
            terms += [r.when for r in tr.rules if r.status == "model"]
        raw: list = []
        for t in terms:
            if t.kind == "message":
                for s, r, name, args in t.atoms:
                    a = (s, r, name, args if args is not None else ())
                    if a not in raw:
                        raw.append(a)
        if len(raw) > MAX_ATOMS:
            raise NormsCheckError(
                f"the contract instance has {len(raw)} message events; norms-check 0.1 "
                f"translates at most {MAX_ATOMS} (use smaller --members)")
        self.atoms = [Atom(*a) for a in raw]

        def same(want: tuple, got: tuple) -> bool:
            return len(want) == len(got) and all(
                isinstance(w, bool) == isinstance(g, bool) and w == g for w, g in zip(want, got))

        for t in terms:
            if t.kind == "message":
                idx = set()
                for s, r, name, args in t.atoms:
                    for i, a in enumerate(self.atoms):
                        if (a.sender, a.receiver, a.name) == (s, r, name) and (
                                args is None or same(args, a.args)):
                            idx.add(i)
                t.atoms = tuple(sorted(idx))

    # --- time -------------------------------------------------------------------

    def _durations(self) -> list[int]:
        out = []
        for tr in self.trs:
            for n in tr.model_norms():
                if n.judged and n.deadline_ms is not None:
                    out.append(n.deadline_ms)
                if n.content is not None and n.content.kind == "temporal":
                    out.append(n.content.window_ms)
            for item in tr.items:
                if item["status"] == "model" and item["term"].kind == "temporal":
                    out.append(item["term"].window_ms)
        return out

    def _time(self, step_ms: int | None, horizon: int | None) -> None:
        durations = self._durations()
        positive = [d for d in durations if d > 0]
        if step_ms is None:
            step_ms = 0
            for d in positive:
                step_ms = math.gcd(step_ms, d)
            step_ms = step_ms or 1000
        else:
            bad = [d for d in positive if d % step_ms]
            if bad:
                raise NormsCheckError(
                    f"--step {step_ms}ms does not divide the duration(s) "
                    f"{', '.join(f'{d}ms' for d in sorted(set(bad)))}; the model would not "
                    f"be exact (F.12)")
        self.step_ms = step_ms
        defaults = []
        for tr in self.trs:
            for n in tr.model_norms():
                if n.judged and n.deadline_ms is not None:
                    n.deadline = n.deadline_ms // step_ms
                if n.content is not None and n.content.kind == "temporal":
                    n.content.window = n.content.window_ms // step_ms
            for item in tr.items:
                if item["status"] == "model" and item["term"].kind == "temporal":
                    item["term"].window = item["term"].window_ms // step_ms
            d = 2
            for n in tr.model_norms():
                if n.judged:
                    d += (n.deadline or 0) + (n.content.window if n.content.kind == "temporal"
                                              else 0) + 2
            defaults.append(d)
        default = max(defaults)
        self.horizon = default if horizon is None else horizon
        if self.horizon < 1:
            raise NormsCheckError("--horizon must be at least 1")
        if self.horizon > MAX_HORIZON:
            raise NormsCheckError(
                f"the horizon is {self.horizon} observations; norms-check 0.1 refuses more than "
                f"{MAX_HORIZON} (use a coarser --step or a smaller --horizon)")

    # --- the choices of the environment -----------------------------------------

    def _choices(self) -> None:
        # quantities compared with each other: groups, their sizes, and the
        # values of --domain quantities as literals of the others
        parent: dict = {}

        def root(k: str) -> str:
            while parent.setdefault(k, k) != k:
                k = parent[k]
            return k

        lparent: dict = {}

        def lroot(k: str) -> str:
            while lparent.setdefault(k, k) != k:
                k = lparent[k]
            return k

        for link in self.order_links:
            link = sorted(link)
            for k in link[1:]:
                lparent[lroot(k)] = lroot(link[0])
        lgroups: dict = {}
        for k in lparent:
            lgroups.setdefault(lroot(k), []).append(k)
        for keys in lgroups.values():
            for k in keys:
                self.quantities[k].list_order = len(keys)
        for link in self.links:
            link = sorted(link)
            for k in link[1:]:
                parent[root(k)] = root(link[0])
        groups: dict = {}
        for k in parent:
            groups.setdefault(root(k), []).append(k)
        for keys in groups.values():
            # every quantity of the group gets the literals of all of them, so
            # that its values cover every literal comparison of the others
            # (F.12: an order of the group with the literals is realised)
            union: set = set()
            for k in keys:
                union |= self.quantities[k].consts
            for k in keys:
                q = self.quantities[k]
                q.order = len(keys)
                q.consts |= union
                q.consts.update((0, 1))         # an ordering compares true as 1
                for other in keys:
                    dom = self.quantities[other].domain
                    if dom is not None and other != k:
                        q.consts.update(range(dom[0], dom[1] + 1))
        for q in self.quantities.values():
            q.reps = self.trs[0]._reps(q)
        for q in self.quantities.values():
            if q.domain is not None:
                self._note(f"{q.key} takes the integers {q.domain[0]}..{q.domain[1]} of --domain "
                           f"and the values that are not numbers: the answers hold for traces in "
                           f"which its numbers are such integers")
        # components of predicates that share quantities
        parent = list(range(len(self.preds)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        owner: dict = {}
        for i, p in enumerate(self.preds):
            for key in p.keys:
                if key in owner:
                    parent[find(i)] = find(owner[key])
                else:
                    owner[key] = i
        groups: dict = {}
        for i in range(len(self.preds)):
            groups.setdefault(find(i), []).append(i)
        self.constant_preds: dict = {}
        self.components: list = []      # (pred ids, [(vector, valuation)])
        for ids in groups.values():
            keys = sorted(set().union(*(self.preds[i].keys for i in ids)))
            if not keys:
                for i in ids:
                    self.constant_preds[i] = self._value(i, {})
                continue
            size = 1
            for k in keys:
                size *= len(self.quantities[k].reps)
            if size > MAX_VALUATIONS:
                sizes = ", ".join(f"{k} ({len(self.quantities[k].reps)} classes)" for k in keys)
                sets = sorted({m.group(1) for k in keys
                               for m in [re.match(r"([A-Za-z_]\w*)\[\d+\]", k)] if m})
                raise NormsCheckError(
                    f"the predicates {', '.join(repr(self.preds[i].text) for i in ids)} read "
                    f"{sizes}: {size} combinations together, and norms-check refuses more than "
                    f"{MAX_VALUATIONS}. Use fewer --members"
                    + (f" ({', '.join(sets)})" if sets else "")
                    + " or, for quantities used in arithmetic, a smaller --domain")
            seen: dict = {}
            for count_, combo in enumerate(
                    itertools.product(*(self.quantities[k].reps for k in keys))):
                if count_ % 512 == 0:
                    _generation_time()
                vals = dict(zip(keys, combo))
                vec = tuple(self._value(i, vals) for i in ids)
                if vec not in seen:
                    seen[vec] = vals
            self.components.append((ids, list(seen.items())))
        count = 2 ** len(self.atoms)
        for _ids, vecs in self.components:
            count *= len(vecs)
        if count > MAX_CHOICES:
            raise NormsCheckError(
                f"the environment has {count} distinguishable choices per observation; "
                f"norms-check 0.1 refuses more than {MAX_CHOICES}")
        self.choices: list[Choice] = []
        subsets = [tuple(i for i in range(len(self.atoms)) if mask >> i & 1)
                   for mask in range(2 ** len(self.atoms))]
        for msgs in subsets:
            for combo in itertools.product(*(vecs for _ids, vecs in self.components)):
                vector = dict(self.constant_preds)
                values: dict = {}
                for (ids, _vecs), (vec, vals) in zip(self.components, combo):
                    vector.update(zip(ids, vec))
                    values.update(vals)
                self.choices.append(Choice(len(self.choices), msgs, vector, values))






# === The model =============================================================

def _tstep(op: str, code: int, over: bool, vals: tuple) -> tuple:
    """One observation of a temporal operator evaluated from an earlier
    observation k (F.4 as norms_eval._Evaluator._temporal computes it):
    (pending code or 0, decided value "T"/"F"/"U" or None). For within and
    throughout the pending codes are 1 (no undefined value yet) and 2 (an
    undefined value seen); for until 1 + prefix-undefined + 2 *
    uncertain. `over` says that the observation lies after the interval
    (k + window + 1): it is not part of the interval and decides it."""
    if op in ("within", "throughout"):
        decisive = "T" if op == "within" else "F"
        if over:
            return 0, ("U" if code == 2 else ("F" if op == "within" else "T"))
        v = vals[0]
        if v == decisive:
            return 0, decisive
        return (2 if code == 2 or v == "U" else 1), None
    pref_u = code in (2, 4)
    unc_u = code in (3, 4)
    if over:
        return 0, ("U" if unc_u else "F")
    vp, vq = vals
    if vq == "T":
        if not pref_u:
            return 0, "T"
        unc_u = True
    elif vq == "U":
        unc_u = True
    if vp == "F":
        return 0, ("U" if unc_u else "F")
    if vp == "U":
        pref_u = True
    return 1 + pref_u + 2 * unc_u, None


def _codes(op: str) -> tuple:
    return (1, 2) if op in ("within", "throughout") else (1, 2, 3, 4)


@dataclass
class Var:
    name: str
    lo: int | None                     # None: boolean
    hi: int | None
    init: Any
    note: str = ""


class NormsModel:
    """The finite model of one contract instance (F.12)."""

    def __init__(self, tr, versions: list | None = None) -> None:
        trs = tr if isinstance(tr, list) else [tr]
        self.trs = trs
        self.tr = trs[0]
        self.multi = len(trs) > 1
        # the versions of the contract (F.6): a translator per version of
        # the one instance; the environment amends to the next one
        self.vtrs = [self.tr] + list(versions or [])
        self.versioned = len(self.vtrs) > 1
        self.H = self.tr.horizon
        self.vars: list[Var] = []
        self._add("step", 0, self.H, 0, "index of the next observation")
        self.norms = []
        self.items = []
        self.excuse_known = True
        for k, t in enumerate(trs):
            t.px = f"k{k + 1}_" if self.multi else ""
            t.inst = k
            for n in t.norms:
                n.px, n.inst = t.px, k
        if self.versioned:
            self._init_versions()
            return
        for t in trs:
            px = t.px
            self._add(f"{px}ended", None, None, False,
                      f"instance {t.inst + 1} has ended (settled)" if self.multi
                      else "the instance has ended (settled)")
            t.model_items = [it for it in t.items if it["status"] == "model"]
            self.items += t.model_items
            self.excuse_known = self.excuse_known and all(
                it["status"] != "untranslated" for it in t.items)
            if t.model_items:
                self._add(f"{px}af", None, None, False,
                          "an assumption has failed (verdicts are excused)")
            for k, it in enumerate(t.items):
                if it["status"] == "model" and it["term"].kind == "temporal":
                    it["var"] = f"{px}as_{k}"
                    self._add(it["var"], 0, max(_codes(it["term"].op)), 1,
                              f"assumption {k + 1} (0: decided)")
        self.edge_preds = sorted(set().union(*(t.edge_preds for t in trs)))
        for pid in self.edge_preds:
            self._add(f"pe_{pid}", None, None, False,
                      f"predicate {pid} held at the previous observation")
        for t in trs:
            for n in t.model_norms():
                self.norms.append(n)
                self._add_norm(t, n)
        self._finish_init()

    def _finish_init(self) -> None:
        names = {v.name for v in self.vars}
        self.ir = _IR({v.name: (v.lo, v.hi) for v in self.vars if v.lo is not None},
                      {v.name for v in self.vars if v.lo is None})
        self.var_names = [v.name for v in self.vars]
        assert len(names) == len(self.vars)
        self._simplified: dict = {}
        self._init_map = {id(self.ir.V(v.name)): self.ir.C(v.init) for v in self.vars}
        self.commands = self._commands()

    def _init_versions(self) -> None:
        """Variables of a model with amendments: one set per norm id of any
        version (its slot), sized for every version; assumptions by text,
        with their age; the version in force."""
        vt = self.vtrs
        self._add("ended", None, None, False, "the instance has ended (settled)")
        self._add("ver", 0, len(vt) - 1, 0, "the version of the contract in force")
        self.slot_ids: list = []
        self.slot_defs: dict = {}               # id -> [NormInfo or None per version]
        for v, t in enumerate(vt):
            t.px, t.inst = "", 0
            for n in t.model_norms():
                if n.id not in self.slot_defs:
                    self.slot_ids.append(n.id)
                    self.slot_defs[n.id] = [None] * len(vt)
                self.slot_defs[n.id][v] = n
        for k, nid in enumerate(self.slot_ids):
            for n in self.slot_defs[nid]:
                if n is not None:
                    n.slot = k
        self.items = []
        self.item_slots: dict = {}              # text -> {"var", "age", "window", "op"}
        for v, t in enumerate(vt):
            t.model_items = [it for it in t.items if it["status"] == "model"]
            for it in t.model_items:
                term = it["term"]
                slot = self.item_slots.get(it["text"])
                if slot is None:
                    j = len(self.item_slots)
                    slot = self.item_slots[it["text"]] = {
                        "temporal": term.kind == "temporal", "var": f"as_{j}",
                        "age": f"ag_{j}", "window": 0, "op": term.op, "in": [False] * len(vt)}
                slot["in"][v] = True
                if term.kind == "temporal":
                    slot["window"] = max(slot["window"], term.window)
                    if slot["op"] != term.op:
                        raise NormsCheckError(f"the assumption {it['text']!r} changes its "
                                              f"operator between versions")
                it["var"] = slot["var"]
        if any(t.model_items for t in vt):
            self._add("af", None, None, False, "an assumption has failed (verdicts are excused)")
        for text, slot in self.item_slots.items():
            if slot["temporal"]:
                self._add(slot["var"], 0, max(_codes(slot["op"])), 1 if slot["in"][0] else 0,
                          f"assumption {text!r} (0: decided or not in force)")
                self._add(slot["age"], 0, slot["window"] + 1, 0,
                          f"observations since assumption {text!r} began")
        self.edge_preds = sorted(set().union(*(t.edge_preds for t in vt)))
        for pid in self.edge_preds:
            self._add(f"pe_{pid}", None, None, False,
                      f"predicate {pid} held at the previous observation")
        self.el_cap: dict = {}
        for nid in self.slot_ids:
            defs = [n for n in self.slot_defs[nid] if n is not None]
            n0 = defs[0]
            self.norms.append(n0)
            judged = n0.judged
            init = INACTIVE if self.slot_defs[nid][0] is not None else ABSENT
            self._add(f"st_{n0.stem}", 0, ABSENT, init, f"state of {nid}")
            if judged:
                self._add(f"jn_{n0.stem}", None, None, False,
                          f"{nid} was activated at the last observation")
            dls = [n.deadline for n in defs if n.deadline is not None]
            if judged and dls:
                self.el_cap[nid] = max(dls)
                self._add(f"el_{n0.stem}", 0, max(dls) + 1, 0,
                          f"active steps of {nid} (saturated)")
            temps = [n.content for n in defs if n.content is not None
                     and n.content.kind == "temporal"]
            if judged and temps:
                top = max(max(_codes(c.op)) for c in temps)
                for a in range(max(c.window for c in temps) + 1):
                    self._add(f"sl_{n0.stem}_{a}", 0, top, 0,
                              f"pending candidate of {nid} of age {a}")
                self._add(f"ht_{n0.stem}", None, None, False,
                          f"a candidate of {nid} decided true, not yet judged")
                self._add(f"sf_{n0.stem}", None, None, False,
                          f"a candidate of {nid} decided not true, not yet judged")
            if n0.kind == "power":
                self._add(f"ex_{n0.stem}", None, None, False, f"{nid} has been exercised")
            if any(n.judged and n.reparation and t.by_id.get(n.reparation) is not None
                   and t.by_id[n.reparation].status == "model"
                   for t in vt for n in t.model_norms() if n.id == nid):
                self._add(f"rp_{n0.stem}", None, None, False,
                          f"the reparation of {nid} has been activated")
        self.excuse_known = all(it["status"] != "untranslated" for t in vt for it in t.items)
        self._finish_init()

    def _add_norm(self, tr: Translator, n: NormInfo) -> None:
        self._add(f"st_{n.stem}", 0, 7, INACTIVE, f"state of {n.uid}")
        if n.judged:
            self._add(f"jn_{n.stem}", None, None, False,
                      f"{n.uid} was activated at the last observation")
        if n.judged and n.deadline is not None:
            self._add(f"el_{n.stem}", 0, n.deadline + 1, 0,
                      f"active steps of {n.uid} (saturated)")
        if n.judged and n.content.kind == "temporal":
            top = max(_codes(n.content.op))
            for a in range(n.content.window + 1):
                self._add(f"sl_{n.stem}_{a}", 0, top, 0,
                          f"pending candidate of {n.uid} of age {a}")
            self._add(f"ht_{n.stem}", None, None, False,
                      f"a candidate of {n.uid} decided true, not yet judged")
            self._add(f"sf_{n.stem}", None, None, False,
                      f"a candidate of {n.uid} decided not true, not yet judged")
        if n.kind == "power":
            self._add(f"ex_{n.stem}", None, None, False, f"{n.uid} has been exercised")
        rep = tr.by_id.get(n.reparation) if n.reparation and n.judged else None
        if rep is not None and rep.status == "model":
            self._add(f"rp_{n.stem}", None, None, False,
                      f"the reparation of {n.uid} has been activated")

    def _add(self, name: str, lo, hi, init, note: str) -> None:
        self.vars.append(Var(name, lo, hi, init, note))

    def has(self, name: str) -> bool:
        return name in self.ir.ranges or name in self.ir.bools

    # --- one observation ----------------------------------------------------

    def _tstep_ir(self, code: tuple, op: str, over: Any, vals: tuple) -> tuple:
        ir = self.ir
        if not isinstance(over, bool):
            a = self._tstep_ir(code, op, True, vals)
            b = self._tstep_ir(code, op, False, vals)
            return tuple(ir.ITE(over, x, y) for x, y in zip(a, b))
        pend = ir.C(0)
        dec = {"T": [], "F": [], "U": []}
        for c in reversed(_codes(op)):
            p, d = _tstep(op, c, over, vals)
            pend = ir.ITE(ir.EQ(code, c), ir.C(p), pend)
            if d is not None:
                dec[d].append(ir.EQ(code, c))
        return (pend, ir.OR(*dec["T"]), ir.OR(*dec["F"]), ir.OR(*dec["U"]))

    def _vals(self, term: Term, ch: Choice) -> tuple:
        return tuple(ch.vector[p] for p in term.preds)

    def step(self, ch: Choice, end: Any, first: bool = False, cur: Any = None,
             old: Any = None) -> dict:
        """The updates of the command for choice `ch` and the instances that
        end at this observation (`end`: a boolean for one instance, a mask
        of instances for several): the steps of F.5 in their order within
        one observation, for every instance. `first`: the command of the
        first observation, from the initial state (its updates are
        constants)."""
        _generation_time()
        ir = self.ir
        step = ir.V("step")
        out: dict = {}
        if self.versioned:
            out.update(self._inst_step(cur, ch, bool(end), old))
        for t in ([] if self.versioned else self.trs):
            ends = bool(int(end) >> t.inst & 1)
            part = self._inst_step(t, ch, ends)
            if self.multi:
                # an ended instance does not change
                done = ir.V(f"{t.px}ended")
                part = {k: ir.ITE(done, ir.V(k), v) for k, v in part.items()}
            out.update(part)
        for pid in self.edge_preds:
            out[f"pe_{pid}"] = ir.C(ch.vector[pid] == "T")
        if self.multi:
            # the observations go on while an instance runs
            after = [ir.OR(ir.V(f"{t.px}ended"), ir.C(bool(int(end) >> t.inst & 1)))
                     for t in self.trs]
            out["step"] = ir.ITE(ir.AND(*after), step, ir.ADD(step, 1))
        else:
            out["step"] = step if end else ir.ADD(step, 1)
        return self._simplify(out, first)

    def _inst_step(self, tr: Translator, ch: Choice, end: bool, old: Any = None) -> dict:
        """The updates of one instance (F.5, one observation). With
        versions, `tr` is the version in force after this observation's
        amendment and `old` the version before it (None: no amendment)."""
        ir = self.ir
        px = tr.px
        msgs = set(ch.messages)
        step = ir.V("step")
        pre = old or tr                   # the version at the start of the observation
        if self.versioned:
            norms = tr.model_norms()
            pre_norms = pre.model_norms()
            slot_norms = self.norms       # one per norm id of any version
        else:
            norms = pre_norms = slot_norms = [n for n in self.norms if n.inst == tr.inst]
        S = {n.id: ir.V(f"st_{n.stem}") for n in slot_norms}
        stems = {n.id: n.stem for n in slot_norms}
        out: dict = {}
        # active time up to this observation (the state between the previous
        # observation and this one is the state after the previous one)
        el = {}
        for n in slot_norms:
            cap = self.el_cap.get(n.id) if self.versioned else (
                n.deadline if n.judged else None)
            if cap is not None:
                e = ir.V(f"el_{n.stem}")
                el[n.id] = ir.ITE(ir.AND(ir.EQ(S[n.id], ACTIVE), ir.LE(e, cap)),
                                  ir.ADD(e, 1), e)
        # pending candidates of temporal contents: one more observation
        slots, ht, sf = {}, {}, {}
        for n in pre_norms:
            if n.judged and n.content.kind == "temporal":
                t = n.content
                vals = self._vals(t, ch)
                new = [ir.C(0)] * (t.window + 1)
                dts, dos = [], []
                for a in range(1, t.window + 2):
                    p, dt, df, du = self._tstep_ir(ir.V(f"sl_{n.stem}_{a - 1}"), t.op,
                                                   a == t.window + 1, vals)
                    if a <= t.window:
                        new[a] = p
                    dts.append(dt)
                    dos += [df, du]
                slots[n.id] = new
                ht[n.id] = ir.OR(ir.V(f"ht_{n.stem}"), *dts)
                sf[n.id] = ir.OR(ir.V(f"sf_{n.stem}"), *dos)
        # start of the instance (F.5, item 1)
        for n in pre_norms:
            if n.starts:
                S[n.id] = ir.ITE(ir.EQ(step, 0), ir.C(ACTIVE), S[n.id])
        amend_viol: dict = {}
        if old is not None:
            amend_viol = self._amend_ir(old, tr, S, el, slots, ht, sf, stems)
        # assume: items
        if self.versioned:
            af = self._items_versioned(old, tr, ch, out)
        else:
            af = ir.V(f"{px}af") if tr.model_items else ir.FALSE
        for k, it in enumerate([] if self.versioned else tr.items):
            if it["status"] != "model":
                continue
            t = it["term"]
            if t.kind == "pred":
                if ch.vector[t.pred] == "F":
                    af = ir.TRUE
            else:
                code = ir.V(it["var"])
                p, dt, df, du = self._tstep_ir(code, t.op, ir.EQ(step, t.window + 1),
                                               self._vals(t, ch))
                out[it["var"]] = p
                af = ir.OR(af, df)
        # step 1: counts-as
        occ_ev: dict = {}
        created: list = []                   # (event, performer) of message rules
        for r in tr.rules:
            if r.status != "model":
                continue
            if r.cond is not None and ch.vector[r.cond] != "T":
                continue
            if r.when.kind == "message":
                for i in r.when.atoms:
                    if i in msgs:
                        created.append((r.creates, tr.atoms[i].sender))
                        occ_ev[r.creates] = ir.TRUE
            else:
                e = ir.AND(ir.C(ch.vector[r.when.pred] == "T"), ir.NOT(ir.V(f"pe_{r.when.pred}")))
                occ_ev[r.creates] = ir.OR(occ_ev.get(r.creates, ir.FALSE), e)

        def occurs(term: Term) -> tuple:
            if term.kind == "event":
                return occ_ev.get(term.event, ir.FALSE)
            if term.kind == "message":
                return ir.C(any(i in msgs for i in term.atoms))
            if term.kind == "pred":
                return ir.AND(ir.C(ch.vector[term.pred] == "T"), ir.NOT(ir.V(f"pe_{term.pred}")))
            return ir.FALSE

        # step 2: powers
        powers = [n for n in tr.norms if n.kind == "power" and n.status != "frozen"]
        acts: list = []
        for name, performer in created:
            if any(p.content is not None and p.content.kind == "event"
                   and p.content.event == name for p in powers):
                acts.append((name, performer))
        seen = set()
        for p in powers:
            if p.content is None or p.content.kind != "message":
                continue
            for i in p.content.atoms:
                if i in msgs:
                    a = tr.atoms[i]
                    key = (p.content_text, (a.sender, a.receiver, a.name))
                    if key not in seen:
                        seen.add(key)
                        acts.append((p.content_text, a.sender))
        model_powers = [p for p in norms if p.kind == "power"]
        start_active = {p.id: ir.EQ(S[p.id], ACTIVE) for p in model_powers}
        ex = {p.id: ir.V(f"ex_{p.stem}") for p in model_powers}
        for text, performer in acts:
            for p in model_powers:
                ptext = p.content.event if p.content.kind == "event" else p.content_text
                if ptext != text or not self._is_bearer(p, performer):
                    continue
                cond = start_active[p.id]
                ex[p.id] = ir.OR(ex[p.id], cond)
                for names, src, tgt in p.effects:
                    for name in names:
                        if name in S:
                            S[name] = ir.ITE(ir.AND(cond, ir.IN(S[name], src)), ir.C(tgt), S[name])
        # step 3: triggers
        for n in norms:
            if n.trigger is not None:
                S[n.id] = ir.ITE(ir.AND(occurs(n.trigger), ir.EQ(S[n.id], INACTIVE)),
                                 ir.C(ACTIVE), S[n.id])
        # step 4: judge, then until
        for n in norms:
            if n.judged and n.content.kind == "temporal" and n.id not in slots:
                # a content that became temporal at this amendment: no candidate
                slots[n.id] = [ir.C(0)] * (n.content.window + 1)
                ht[n.id] = sf[n.id] = ir.FALSE
        viol_code = ir.ITE(af, ir.C(EXCUSED), ir.C(VIOLATED))
        newviol: dict = {}
        cands: dict = {}
        for n in norms:
            s = S[n.id]
            if n.judged:
                act = ir.EQ(s, ACTIVE)
                open_ = ir.TRUE if n.deadline is None else ir.LE(el[n.id], n.deadline)
                t = n.content
                if t.kind == "temporal":
                    p0, d0 = _tstep(t.op, 1, False, self._vals(t, ch))
                    hit = ir.AND(act, ir.OR(ir.AND(open_, ir.C(d0 == "T")), ht[n.id]))
                    old_pending = ir.OR(*(ir.NOT(ir.EQ(x, 0)) for x in slots[n.id][1:]))
                    pending = ir.OR(old_pending, ir.AND(open_, ir.C(p0 != 0)))
                    fail = ir.AND(act, ir.NOT(hit), ir.NOT(open_), ir.NOT(pending))
                    slots[n.id][0] = ir.ITE(ir.AND(act, open_), ir.C(p0), ir.C(0))
                    ht[n.id] = ir.ITE(act, ir.FALSE, ht[n.id])
                    sf[n.id] = ir.ITE(act, ir.FALSE, sf[n.id])
                else:
                    now = (ir.C(t.kind == "pred" and ch.vector[t.pred] == "T")
                           if t.kind == "pred" else occurs(t))
                    hit = ir.AND(act, open_, now)
                    fail = ir.AND(act, ir.NOT(open_))
                if n.kind == "obligation":
                    s = ir.ITE(hit, ir.C(FULFILLED), ir.ITE(fail, viol_code, s))
                    newviol[n.id] = fail
                else:
                    s = ir.ITE(hit, viol_code, ir.ITE(fail, ir.C(FULFILLED), s))
                    newviol[n.id] = hit
            if n.until is not None:
                s = ir.ITE(ir.AND(occurs(n.until), ir.IN(s, IN_FORCE)), ir.C(DISCHARGED), s)
            S[n.id] = s
        # step 5: violations activate reparations
        for nid, cond in amend_viol.items():
            newviol[nid] = ir.OR(newviol.get(nid, ir.FALSE), cond)
        rp: dict = {}
        for n in norms:
            if n.judged and self.has(f"rp_{n.stem}") and n.reparation in tr.by_id \
                    and tr.by_id[n.reparation].status == "model":
                r = tr.by_id[n.reparation]
                cond = ir.AND(newviol.get(n.id, ir.FALSE), ir.EQ(S[r.id], INACTIVE))
                rp[n.id] = ir.OR(ir.V(f"rp_{n.stem}"), cond)
                S[r.id] = ir.ITE(cond, ir.C(ACTIVE), S[r.id])
        # candidates left (for the settlement and to clear final states)
        for n in norms:
            if n.judged and n.content.kind == "temporal":
                cands[n.id] = ir.OR(ht[n.id], sf[n.id],
                                    *(ir.NOT(ir.EQ(x, 0)) for x in slots[n.id]))
        # step 6: the end of the instance settles every norm (F.5, item 6)
        if end:
            for n in norms:
                s = S[n.id]
                # activated at this observation
                fresh = ir.IN(ir.V(f"st_{n.stem}"), (INACTIVE, ABSENT))
                if n.kind == "obligation":
                    if n.deadline is None:
                        late = viol_code
                    else:
                        late = ir.ITE(ir.LE(el[n.id], n.deadline), ir.C(DISCHARGED), viol_code)
                    kind_part = late
                elif n.kind == "prohibition":
                    kind_part = ir.C(FULFILLED)
                else:
                    kind_part = ir.C(DISCHARGED)
                left = cands.get(n.id, ir.FALSE) if n.judged else ir.FALSE
                settled = ir.ITE(fresh, ir.C(DISCHARGED), ir.ITE(left, s, kind_part))
                S[n.id] = ir.ITE(ir.IN(s, FINAL), s,
                                 ir.ITE(ir.IN(s, IN_FORCE), settled, ir.C(UNUSED)))
        # the next state
        for n in slot_norms:
            s = S[n.id]
            out[f"st_{n.stem}"] = s
            if self.has(f"jn_{n.stem}"):
                # activated at this observation, whatever F.5 then made of it
                out[f"jn_{n.stem}"] = ir.AND(ir.IN(ir.V(f"st_{n.stem}"), (INACTIVE, ABSENT)),
                                             ir.NOT(ir.IN(s, (INACTIVE, UNUSED, ABSENT))))
            final = ir.IN(s, FINAL)
            if n.id in el:
                out[f"el_{n.stem}"] = ir.ITE(final, ir.C(0), el[n.id])
            if n.id in slots:
                for a, x in enumerate(slots[n.id]):
                    out[f"sl_{n.stem}_{a}"] = ir.ITE(final, ir.C(0), x)
                out[f"ht_{n.stem}"] = ir.AND(ir.NOT(final), ht[n.id])
                out[f"sf_{n.stem}"] = ir.AND(ir.NOT(final), sf[n.id])
            if n.id in ex:
                out[f"ex_{n.stem}"] = ex[n.id]
            if n.id in rp:
                out[f"rp_{n.stem}"] = rp[n.id]
        if self.has(f"{px}af"):
            out[f"{px}af"] = af
        out[f"{px}ended"] = ir.C(end)
        return out

    def _amend_ir(self, old: Translator, new: Translator, S: dict, el: dict, slots: dict,
                  ht: dict, sf: dict, stems: dict) -> dict:
        """The amendment from version `old` to `new` at this observation,
        before step 1 (F.6, as norms_eval._Engine._amend applies it). Changes
        S, el, slots, ht, sf in place; returns the conditions under which a
        norm is violated by the amendment (its reparation applies)."""
        ir = self.ir
        af = ir.V("af") if self.has("af") else ir.FALSE
        viol_code = ir.ITE(af, ir.C(EXCUSED), ir.C(VIOLATED))
        before = dict(S)
        amend_viol: dict = {}
        transforms: list = []
        for o in old.model_norms():
            st = before[o.id]
            nd = new.by_id.get(o.id)
            nd = nd if nd is not None and nd.status == "model" else None
            inforce = ir.IN(st, IN_FORCE)
            if o.on_amendment == "discharge":
                forced = ir.C(DISCHARGED)
            elif o.on_amendment == "transform":
                forced = ir.C(DISCHARGED)
                transforms.append((o, inforce, el.get(o.id, ir.C(0))))
            elif nd is None or nd.kind != o.kind:
                forced = ir.C(DISCHARGED)
            else:
                forced = st
                if nd.content_text != o.content_text and o.id in slots:
                    # the candidates are kept only when the content is the same
                    slots[o.id] = [ir.ITE(inforce, ir.C(0), x) for x in slots[o.id]]
                    ht[o.id] = ir.AND(ir.NOT(inforce), ht[o.id])
                    sf[o.id] = ir.AND(ir.NOT(inforce), sf[o.id])
                if nd.kind == "obligation" and nd.deadline is not None and o.id in el:
                    late = ir.AND(inforce, ir.NOT(ir.LE(el[o.id], nd.deadline)))
                    forced = ir.ITE(late, viol_code, st)
                    amend_viol[o.id] = late
                elif nd.kind == "prohibition" and nd.deadline is not None and o.id in el:
                    # the window has closed, also for a suspended instance
                    late = ir.AND(inforce, ir.NOT(ir.LE(el[o.id], nd.deadline)))
                    forced = ir.ITE(late, ir.C(FULFILLED), st)
            inactive_to = ir.C(ABSENT) if nd is None else ir.C(INACTIVE)
            S[o.id] = ir.ITE(ir.IN(st, FINAL), st,
                             ir.ITE(ir.EQ(st, INACTIVE), inactive_to, forced))
        # norms of the new version that the instance does not have yet
        added = {}
        for x in new.model_norms():
            if x.id in S:
                added[x.id] = ir.EQ(before[x.id], ABSENT)
                S[x.id] = ir.ITE(added[x.id], ir.C(INACTIVE), S[x.id])
        for o, cond, elapsed in transforms:
            x = new.by_id.get(o.transform_to)
            if x is None or x.status != "model":
                continue
            ok = ir.AND(cond, ir.EQ(S[x.id], INACTIVE))
            S[x.id] = ir.ITE(ok, ir.C(ACTIVE), S[x.id])
            if x.id in el:
                timed = x.kind in ("obligation", "prohibition")
                el[x.id] = ir.ITE(ok, elapsed if timed else ir.C(0), el[x.id])
        for x in new.model_norms():
            if x.trigger is None and x.id not in new.targets and x.id in added:
                S[x.id] = ir.ITE(ir.AND(added[x.id], ir.EQ(S[x.id], INACTIVE)),
                                 ir.C(ACTIVE), S[x.id])
        return amend_viol

    def _items_versioned(self, old: Any, tr: Translator, ch: Choice, out: dict) -> tuple:
        """assume: items by text across versions (F.6): an item of the same
        text continues, a new one is evaluated from the amendment on, an old
        one is closed. Returns whether an assumption has failed."""
        ir = self.ir
        af = ir.V("af") if self.has("af") else ir.FALSE
        v_new = self.vtrs.index(tr)
        v_old = self.vtrs.index(old) if old is not None else v_new
        terms = {it["text"]: it["term"] for it in tr.model_items}
        for text, slot in self.item_slots.items():
            in_old, in_new = slot["in"][v_old], slot["in"][v_new]
            if not slot["temporal"]:
                if in_new and ch.vector[terms[text].pred] == "F":
                    af = ir.TRUE
                continue
            code, age = ir.V(slot["var"]), ir.V(slot["age"])
            if old is not None and in_new and not in_old:
                code, age = ir.C(1), ir.C(0)
            elif old is not None and in_old and not in_new:
                code = ir.C(0)
            if in_new:
                t = terms[text]
                p, dt, df, du = self._tstep_ir(code, t.op, ir.EQ(age, t.window + 1),
                                               self._vals(t, ch))
                out[slot["var"]] = p
                out[slot["age"]] = ir.ITE(ir.LE(age, slot["window"]), ir.ADD(age, 1), age)
                af = ir.OR(af, df)
            else:
                out[slot["var"]] = code
        return af

    def _simplify(self, out: dict, first: bool) -> dict:
        ir = self.ir
        step = ir.V("step")
        order = [v.name for v in self.vars if v.name.startswith("st_")]
        res = {}
        for k, raw in out.items():
            # the same raw expression (interned) for another choice: same result
            key = (k, first, id(raw))
            v = self._simplified.get(key)
            if v is None:
                own = [k.replace(k.split("_", 1)[0], "st", 1)] if "_" in k else []
                v = raw
                if first:
                    v = ir.subst_many(v, self._init_map, "init")
                else:
                    v = ir.subst(v, ir.EQ(step, 0), ir.FALSE)
                v = ir.tidy(ir.split(v, order=[x for x in own if self.has(x)] + order))
                self._simplified[key] = v
            if v is not ir.V(k):
                res[k] = v
        return res

    def _is_bearer(self, p: NormInfo, performer: str) -> bool:
        if p.bearer is None:
            return False
        kind, what = p.bearer
        if kind == "exact":
            return performer == what
        m = _ACTOR_RE.match(performer)
        return bool(m) and m.group(1) == what

    def command_count(self) -> int:
        """The number of commands before they are built (F.12 limits)."""
        a = len(self.tr.choices)
        if self.versioned:
            return 2 * a * 2 * (2 * len(self.vtrs) - 1)
        return 2 * a * (2 ** len(self.trs) if self.multi else 2)

    def _commands(self) -> list:
        """(label, choice, end, guard, updates) for every command."""
        count = self.command_count()
        if count > MAX_COMMANDS:
            raise NormsCheckError(
                f"the model would have {count} commands ({len(self.tr.choices)} choices per "
                f"observation, times the ends of the instances and the versions); norms-check "
                f"refuses more than {MAX_COMMANDS}")
        out = []
        if self.versioned:
            A = len(self.tr.choices)
            V = len(self.vtrs)
            for first in (True, False):
                for w in range(V):
                    if first and w > 0:
                        continue
                    for amend in (False, True):
                        if amend and w == V - 1:
                            continue
                        cur = self.vtrs[w + 1] if amend else self.vtrs[w]
                        old = self.vtrs[w] if amend else None
                        for ch in self.tr.choices:
                            for end in (False, True):
                                guard = "!ended & step=0" if first else "!ended & step>0"
                                guard += f" & ver={w}"
                                guard += " & step<H" if not end else " & (early_end | step=H)"
                                j = ch.index + (A if amend else 0)
                                label = f"{'e' if end else 'o'}{j}"
                                upd = self.step(ch, end, first, cur, old)
                                if amend:
                                    upd = dict(upd, ver=self.ir.C(w + 1))
                                out.append((label, ch, end, guard, upd))
            return out
        if not self.multi:
            for first in (True, False):
                for ch in self.tr.choices:
                    for end in (False, True):
                        guard = "!ended & step=0" if first else "!ended & step>0"
                        guard += " & step<H" if not end else " & (early_end | step=H)"
                        label = f"{'e' if end else 'o'}{ch.index}"
                        out.append((label, ch, end, guard, self.step(ch, end, first)))
            return out
        n = len(self.trs)
        alive = " | ".join(f"!{t.px}ended" for t in self.trs)
        for first in (True, False):
            for ch in self.tr.choices:
                for mask in range(2 ** n):
                    ending = [t for t in self.trs if mask >> t.inst & 1]
                    others = [t for t in self.trs if not mask >> t.inst & 1]
                    parts = ["step=0" if first else "step>0", f"({alive})"]
                    parts += [f"!{t.px}ended" for t in ending]
                    if ending:
                        parts.append("(early_end | step=H)")
                    # at the horizon every running instance ends
                    if others:
                        parts.append("(step<H | " + " & ".join(f"{t.px}ended" for t in others)
                                     + ")")
                    out.append((f"c{ch.index}_{mask}", ch, mask, " & ".join(parts),
                                self.step(ch, mask, first)))
        return out

    def all_ended(self, s: dict) -> bool:
        return all(s[f"{t.px}ended"] for t in self.trs)

    def ended_expr(self) -> str:
        return " & ".join(f"{t.px}ended" for t in self.trs)

    # --- PRISM text -----------------------------------------------------------

    def legend(self) -> list[str]:
        tr = self.tr
        lines = [f"// atom {i}: {self._comment(a.text())}" for i, a in enumerate(tr.atoms)]
        for pid, p in enumerate(tr.preds):
            lines.append(f"// predicate {pid}: {self._comment(p.text)}")
        for q in tr.quantities.values():
            lines.append(self._comment(f"// quantity {q.key}: values "
                                       f"{', '.join(json_text(v) for v in q.reps)}"))
        return lines

    @staticmethod
    def _comment(text: str) -> str:
        """Text for a // comment: no line break or other control character."""
        return re.sub(r"[\x00-\x1f\x7f\u2028\u2029]", " ", str(text))

    def prism_text(self, init: dict | None = None) -> str:
        tr = self.tr
        init = init or {}
        out = [
            f"// Generated by cadl norms-check {NORMS_CHECK_VERSION} (Appendix F, F.12).",
            f"// Contract {self._comment(tr.contract.id)}, bindings "
            f"{', '.join(f'{k}={v}' for k, v in sorted(tr.bindings.items())) or 'none'}, members "
            f"{', '.join(f'{k}[{v[0]}..{v[-1]}]' for k, v in sorted(tr.members.items())) or 'none'}.",
            f"// One command per choice of the environment at one observation (o: the",
            f"// instance continues, e: it ends there); step {tr.step_ms} ms, horizon {self.H}.",
        ]
        out += self.legend()
        # an update that several commands share is written once, as a formula
        uses: dict = {}
        for _label, _ch, _end, _guard, upd in self.commands:
            for e in upd.values():
                uses[id(e)] = uses.get(id(e), 0) + 1
        names: dict = {}
        formulas = []
        for _label, _ch, _end, _guard, upd in self.commands:
            for k, e in upd.items():
                if id(e) not in names and uses[id(e)] > 1 and e[0] not in ("c", "v"):
                    text = prism_expr(e)
                    if len(text) > 12:
                        names[id(e)] = f"u{len(formulas)}"
                        formulas.append(f"formula {names[id(e)]} = {text};")
        out += ["", "mdp", "",
                "// true: the instance may end at every observation; false: only at the horizon",
                "const bool early_end;",
                f"const int H = {self.H};", ""]
        out += formulas
        out += ["", "module instance"]
        for v in self.vars:
            val = init.get(v.name, v.init)
            if v.lo is None:
                text = f"  {v.name} : bool init {'true' if val else 'false'};"
            elif v.name == "step":
                text = f"  {v.name} : [0..H] init {val};"
            else:
                text = f"  {v.name} : [{v.lo}..{v.hi}] init {val};"
            out.append(f"{text}  // {self._comment(v.note)}")
        out.append("")
        for label, ch, end, guard, upd in self.commands:
            out.append(f"  // {self._comment(self.choice_text(ch, end))}")
            body = " & ".join(f"({k}'={names.get(id(e)) or prism_expr(e)})"
                              for k, e in upd.items()) or "true"
            out.append(f"  [{label}] {guard} -> {body};")
        out.append(f"  [] {self.ended_expr()} -> true;")
        out.append("endmodule")
        out.append("")
        for n in self.norms:
            st = f"st_{n.stem}"
            out.append(f'label "a_{n.stem}" = {st}>={ACTIVE} & {st}<={DISCHARGED};'
                       f'  // {self._comment(n.id)} has been activated')
            if n.judged:
                out.append(f'label "v_{n.stem}" = {st}={VIOLATED} | {st}={EXCUSED};'
                           f'  // {self._comment(n.id)} is violated')
                out.append(f'label "f_{n.stem}" = {st}={FULFILLED};  // {self._comment(n.id)} is fulfilled')
                out.append(f'label "j_{n.stem}" = jn_{n.stem};'
                           f'  // {self._comment(n.id)} was activated at the last observation')
            if n.kind == "power":
                out.append(f'label "x_{n.stem}" = ex_{n.stem};  // {self._comment(n.id)} has been exercised')
            if self.has(f"rp_{n.stem}"):
                out.append(f'label "r_{n.stem}" = rp_{n.stem};'
                           f'  // the reparation of {self._comment(n.id)} has been activated')
        out.append(f'label "end" = {self.ended_expr()};')
        return "\n".join(out) + "\n"

    def step_text(self, j: int, end: Any) -> str:
        ch, amend = split_choice(self.tr, j)
        return ("amend; " if amend else "") + self.choice_text(ch, end)

    def choice_text(self, ch: Choice, end: bool) -> str:
        msgs = ", ".join(self.tr.atoms[i].text() for i in ch.messages) or "no message"
        vals = ", ".join(f"{k}={json_text(v)}" for k, v in sorted(ch.values.items())
                         if v is not None)
        return f"{msgs}{'; ' + vals if vals else ''}{'; end' if end else ''}"




# === An interpreter of the generated PRISM text ============================
#
# It reads the subset of the PRISM language that `NormsModel.prism_text`
# writes (one module, bounded integer and boolean variables, guarded
# commands with deterministic updates, labels) and runs it. The
# differential tests run the generated text with it, so that what is
# compared with norms_eval is the model PRISM checks, not the Python
# objects it was printed from.

_TOKEN_RE = re.compile(r"\s*(?:(\d+)|([A-Za-z_]\w*)|(<=|>=|!=|=|<|>|&|\||!|\?|:|\(|\)|\+|-|,))")


class _ExprParser:
    def __init__(self, text: str, formulas: dict | None = None) -> None:
        self.formulas = formulas or {}
        self.toks = []
        pos = 0
        text = text.strip()
        while pos < len(text):
            m = _TOKEN_RE.match(text, pos)
            if not m or m.end() == pos:
                raise ValueError(f"cannot read {text[pos:pos + 20]!r}")
            pos = m.end()
            self.toks.append(m.group(1) or m.group(2) or m.group(3))
        self.i = 0

    def peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, want: str | None = None) -> str:
        t = self.peek()
        if t is None or (want is not None and t != want):
            raise ValueError(f"expected {want!r}, got {t!r}")
        self.i += 1
        return t

    def parse(self) -> str:
        e = self.ite()
        if self.peek() is not None:
            raise ValueError(f"unexpected {self.peek()!r}")
        return e

    def ite(self) -> str:
        c = self.disj()
        if self.peek() == "?":
            self.take()
            a = self.ite()
            self.take(":")
            b = self.ite()
            return f"(({a}) if ({c}) else ({b}))"
        return c

    def disj(self) -> str:
        e = self.conj()
        while self.peek() == "|":
            self.take()
            e = f"(({e}) or ({self.conj()}))"
        return e

    def conj(self) -> str:
        e = self.neg()
        while self.peek() == "&":
            self.take()
            e = f"(({e}) and ({self.neg()}))"
        return e

    def neg(self) -> str:
        if self.peek() == "!":
            self.take()
            return f"(not ({self.neg()}))"
        return self.rel()

    def rel(self) -> str:
        e = self.add()
        if self.peek() in ("=", "!=", "<", "<=", ">", ">="):
            op = self.take()
            op = "==" if op == "=" else op
            e = f"(({e}) {op} ({self.add()}))"
        return e

    def add(self) -> str:
        e = self.unary()
        while self.peek() in ("+", "-"):
            op = self.take()
            e = f"(({e}) {op} ({self.unary()}))"
        return e

    def unary(self) -> str:
        if self.peek() == "-":
            self.take()
            return f"(-({self.unary()}))"
        return self.primary()

    def primary(self) -> str:
        t = self.take()
        if t == "(":
            e = self.ite()
            self.take(")")
            return e
        if t.isdigit():
            return t
        if t in ("true", "false"):
            return "True" if t == "true" else "False"
        if t in ("min", "max") and self.peek() == "(":
            self.take("(")
            args = [self.ite()]
            while self.peek() == ",":
                self.take()
                args.append(self.ite())
            self.take(")")
            return f"{t}({', '.join(args)})"
        if t in self.formulas:
            return f"({_ExprParser(self.formulas[t], self.formulas).parse()})"
        return f"s[{t!r}]"


def _compile(text: str, formulas: dict | None = None):
    return eval(f"lambda s: {_ExprParser(text, formulas).parse()}")   # noqa: S307 (generated text)


class PrismModel:
    """A model in the PRISM subset written by `NormsModel.prism_text`."""

    def __init__(self, text: str, consts: dict | None = None) -> None:
        self.consts = dict(consts or {})
        self.vars: list[tuple] = []         # (name, lo, hi, init)
        self.commands: list[tuple] = []     # (label, guard, [(var, fn)])
        self.labels: dict = {}
        self.formulas: dict = {}
        self._cache: dict = {}
        in_module = False
        for raw in text.splitlines():
            line = raw.split("//", 1)[0].strip()
            if not line or line == "mdp":
                continue
            m = re.fullmatch(r"formula\s+(\w+)\s*=\s*(.+);", line)
            if m:
                self.formulas[m.group(1)] = m.group(2)
                continue
            m = re.fullmatch(r"const\s+(bool|int)\s+(\w+)(?:\s*=\s*(.+))?;", line)
            if m:
                if m.group(3) is not None:
                    self.consts[m.group(2)] = _compile(m.group(3))(self.consts)
                elif m.group(2) not in self.consts:
                    raise ValueError(f"constant {m.group(2)} has no value")
                continue
            if line.startswith("module "):
                in_module = True
                continue
            if line == "endmodule":
                in_module = False
                continue
            m = re.fullmatch(r'label\s+"(\w+)"\s*=\s*(.+);', line)
            if m:
                self.labels[m.group(1)] = _compile(m.group(2), self.formulas)
                continue
            if not in_module:
                raise ValueError(f"cannot read {line!r}")
            m = re.fullmatch(r"(\w+)\s*:\s*bool\s+init\s+(true|false);", line)
            if m:
                self.vars.append((m.group(1), None, None, m.group(2) == "true"))
                continue
            m = re.fullmatch(r"(\w+)\s*:\s*\[(.+)\.\.(.+)\]\s+init\s+(.+);", line)
            if m:
                lo, hi, init = (_compile(m.group(k))(self.consts) for k in (2, 3, 4))
                self.vars.append((m.group(1), lo, hi, init))
                continue
            m = re.fullmatch(r"\[(\w*)\]\s*(.+?)\s*->\s*(.+);", line)
            if m:
                ups = []
                if m.group(3).strip() != "true":
                    for u in re.findall(r"\((\w+)'=(.*?)\)(?=\s*&\s*\(\w+'=|\s*$)", m.group(3)):
                        ups.append((u[0], self._compiled(u[1])))
                self.commands.append((m.group(1), self._compiled(m.group(2)), ups))
                continue
            raise ValueError(f"cannot read {line!r}")
        self.names = [v[0] for v in self.vars]

    def _compiled(self, text: str):
        fn = self._cache.get(text)
        if fn is None:
            fn = self._cache[text] = _compile(text, self.formulas)
        return fn

    def initial(self) -> dict:
        s = dict(self.consts)
        s.update({v[0]: v[3] for v in self.vars})
        return s

    def enabled(self, s: dict) -> list:
        return [c for c in self.commands if c[1](s)]

    def apply(self, cmd: tuple, s: dict) -> dict:
        new = dict(s)
        for name, fn in cmd[2]:
            new[name] = fn(s)
        for name, lo, hi, _ in self.vars:
            v = new[name]
            if lo is not None and not (lo <= v <= hi):
                raise ValueError(f"{name}={v} is out of [{lo}..{hi}] after [{cmd[0]}]")
        return new

    def by_label(self, s: dict, label: str) -> tuple:
        for c in self.commands:
            if c[0] == label and c[1](s):
                return c
        raise ValueError(f"no enabled command [{label}]")

    def key(self, s: dict) -> tuple:
        return tuple(s[n] for n in self.names)

    def from_key(self, key: tuple) -> dict:
        s = dict(self.consts)
        s.update(zip(self.names, key))
        return s


# === Traces and paths ======================================================

def _t(step_ms: int, n: int) -> Any:
    v = Fraction(step_ms * n, 1000)
    return int(v) if v.denominator == 1 else float(v)


def _ends(tr: Any, end: Any) -> bool:
    """Whether the instance of `tr` ends at an observation of a path: `end`
    is a boolean (one instance) or a mask of instances."""
    return bool(int(end) >> getattr(tr, "inst", 0) & 1)


def instance_path(tr: Any, path: list) -> list:
    """The observations of a path that belong to the instance of `tr`: up
    to the one at which it ends."""
    out = []
    for j, end in path:
        out.append((j, end))
        if _ends(tr, end):
            break
    return out


def trace_lines(tr: Translator, path: list) -> list[str]:
    """The trace of a path of choices [(choice index, end), ...] as JSON
    Lines for `cadl norms-eval`, for the instance of `tr`: one observation
    per step, at multiples of the step, with the messages and the values
    of the choice, up to the end of the instance."""
    lines = []
    amended = 0
    for n, (j, end) in enumerate(instance_path(tr, path)):
        ch, amend = split_choice(tr, j)
        obs: dict = {"t": _t(tr.step_ms, n)}
        if n == 0:
            obs["bindings"] = dict(sorted(tr.bindings.items()))
        if amend:
            obs["amend"] = tr.version_names[amended]
            amended += 1
        if tr.quantities:
            obs["values"] = {k: ch.values.get(k) for k in sorted(tr.quantities)}
        if ch.messages:
            obs["messages"] = [tr.atoms[i].message() for i in ch.messages]
        if _ends(tr, end):
            obs["end"] = True
        lines.append(json_text(obs))
    return lines


def split_choice(tr: Any, j: int) -> tuple:
    """(choice, amend): with versions, a choice index j >= the number of
    choices also amends the contract to the next version (F.6)."""
    a = len(tr.choices)
    return tr.choices[j % a], j >= a


def parse_label(label: str) -> tuple:
    """(choice, end): end is a boolean for one instance (labels o<j>, e<j>)
    and a mask of the instances that end for several (c<j>_<mask>)."""
    if label[0] == "c":
        j, mask = label[1:].split("_")
        return int(j), int(mask)
    return int(label[1:]), label[0] == "e"


def make_label(j: int, end: Any) -> str:
    if isinstance(end, bool):
        return f"{'e' if end else 'o'}{j}"
    return f"c{j}_{end}"


def run_path(pm: PrismModel, path: list) -> list[dict]:
    """The states of the model after each command of the path."""
    s = pm.initial()
    out = []
    for j, end in path:
        s = pm.apply(pm.by_label(s, make_label(j, end)), s)
        out.append(s)
    return out


def evaluate_path(tr: Translator, path: list) -> dict:
    trace = parse_trace(trace_lines(tr, path))
    versions = getattr(tr, "version_sos", {})
    return run_trace(tr.contract, trace, versions.get if versions else None, tr.actors)


def compare_path(model: NormsModel, pm: PrismModel, path: list) -> list[str]:
    """The differences between the model and norms_eval on one path: the
    state of every translated norm after every observation, the final
    verdicts (when the assumptions are translated), the first exercise of
    every power and the first activation of every reparation."""
    all_states = run_path(pm, path)
    times = {_t(model.tr.step_ms, n): n for n in range(len(path))}
    diffs = []
    results = {}
    for t in model.trs:
        results[t.inst] = evaluate_path(t, path)
    for n in model.norms:
        tr = model.trs[n.inst]
        result = results[n.inst]
        length = len(instance_path(tr, path))
        states = all_states[:length]
        by_id = {r["id"]: r for r in result["norms"]}
        if n.id not in by_id:
            # not a norm of the instance (removed by an amendment, or of a
            # version never in force): absent in the model at the end
            got = STATE_NAMES[states[-1][f"st_{n.stem}"]]
            if got != "absent":
                diffs.append(f"{n.uid}: model {got} at the end, norms-eval has no such norm")
            continue
        r = by_id[n.id]
        seq = ["inactive"] * length
        for h in r["history"]:
            k = times[h["t"]]
            for m in range(k, length):
                seq[m] = h["state"]
        # a norm added by an amendment: absent in the model before it, and
        # present from it on (norms-eval keeps no state for it before)
        added = []
        if n.id not in {d.id for d in tr.contract.norms}:
            vs = getattr(tr, "version_sos", {})
            for a in result.get("amendments", []):
                c = next((c for c in vs[a["file"]].contracts if c.id == tr.contract.id), None)
                if c is not None and n.id in {d.id for d in c.norms}:
                    added.append(times[a["t"]])
                    break
        for k, s in enumerate(states):
            got = STATE_NAMES[s[f"st_{n.stem}"]]
            if added and (got == "absent") != (k < added[0]):
                diffs.append(f"{n.uid} at observation {k}: model {got}, added by the "
                             f"amendment at observation {added[0]}")
                break
            if got == "absent" and added:
                got = "inactive"
            if got != seq[k]:
                diffs.append(f"{n.uid} at observation {k}: model {got}, norms-eval {seq[k]}")
                break
        code = states[-1][f"st_{n.stem}"]
        verdict = ("violated" if code == VIOLATED else "excused" if code == EXCUSED
                   else "fulfilled" if code in (FULFILLED, DISCHARGED, UNUSED) else "pending")
        if model.excuse_known and verdict != r["verdict"]:
            diffs.append(f"{n.id}: verdict model {verdict}, norms-eval {r['verdict']}")
        elif not model.excuse_known and (verdict in ("violated", "excused")) != (
                r["verdict"] in ("violated", "excused")):
            diffs.append(f"{n.id}: verdict model {verdict}, norms-eval {r['verdict']}")
        if n.kind == "power":
            first = next((k for k, s in enumerate(states) if s[f"ex_{n.stem}"]), None)
            ev = [times[e["t"]] for e in result["timeline"]
                  if e["kind"] == "power" and e.get("norm") == n.id]
            if first != (min(ev) if ev else None):
                diffs.append(f"{n.id}: first exercised model {first}, norms-eval "
                             f"{min(ev) if ev else None}")
        if model.has(f"rp_{n.stem}"):
            first = next((k for k, s in enumerate(states) if s[f"rp_{n.stem}"]), None)
            ev = [times[v["t"]] for v in result["violations"]
                  if v["norm"] == n.id and v["reparation_activated"]]
            if first != (min(ev) if ev else None):
                diffs.append(f"{n.id}: reparation activated model {first}, norms-eval "
                             f"{min(ev) if ev else None}")
    return diffs


def random_paths(tr: Translator, count: int, seed: int = 0, p_message: float = 0.3,
                 p_end: float = 0.12, full_length: bool = False,
                 p_keep: float = 0.0, instances: int = 1,
                 p_amend: float = 0.0) -> list[list]:
    """Random paths of environment choices: each message of a choice with
    probability p_message, the classes of values of the previous
    observation kept with probability p_keep and otherwise drawn
    uniformly, and the end of the instance with probability p_end at each
    observation (only at the horizon when full_length)."""
    rng = random.Random(seed)
    index: dict = {}
    by_values: dict = {}
    for ch in tr.choices:
        index.setdefault(ch.messages, []).append(ch.index)
        by_values[(ch.messages, json_text(dict(sorted(ch.values.items()))))] = ch.index
    paths = []
    for _ in range(count):
        path: list = []
        for n in range(tr.horizon + 1):
            msgs = tuple(i for i in range(len(tr.atoms)) if rng.random() < p_message)
            j = rng.choice(index[msgs])
            if path and rng.random() < p_keep:
                prev = split_choice(tr, path[-1][0])[0].values
                j = by_values[(msgs, json_text(dict(sorted(prev.items()))))]
            amended = sum(1 for jj, _e in path if jj >= len(tr.choices))
            if amended < len(getattr(tr, "version_names", [])) and rng.random() < p_amend:
                j += len(tr.choices)
            if instances == 1:
                end = n == tr.horizon or (not full_length and rng.random() < p_end)
                path.append((j, end))
                if end:
                    break
                continue
            # several instances: each running one ends on its own
            done = 0
            for _j, m in path:
                done |= m
            mask = 0
            for k in range(instances):
                if not done >> k & 1 and (n == tr.horizon or (
                        not full_length and rng.random() < p_end)):
                    mask |= 1 << k
            path.append((j, mask))
            if (done | mask) == 2 ** instances - 1:
                break
        paths.append(path)
    return paths


def all_paths(tr: Translator, length: int, instances: int = 1) -> list[list]:
    """Every path of at most `length` observations (the last one ends the
    instance); with several instances, every order in which they end."""
    out = []
    if instances == 1:
        # with versions, every choice also with an amendment (at most one
        # amendment per version)
        n = len(tr.choices) * (2 if getattr(tr, "version_names", None) else 1)
        for k in range(1, length + 1):
            for combo in itertools.product(range(n), repeat=k):
                if sum(j >= len(tr.choices) for j in combo) > len(
                        getattr(tr, "version_names", [])):
                    continue
                out.append([(j, i == k - 1) for i, j in enumerate(combo)])
        return out
    full = 2 ** instances - 1

    def extend(path: list, done: int) -> None:
        if done == full:
            out.append(path)
            return
        if len(path) == length:
            return
        for j in range(len(tr.choices)):
            for mask in range(full + 1):
                if mask & done:
                    continue
                if len(path) + 1 == length and (done | mask) != full:
                    continue
                extend(path + [(j, mask)], done | mask)

    extend([], 0)
    return out


# === Building ==============================================================

def _parse_domains(items) -> dict:
    out: dict = {}
    for item in items or []:
        for part in str(item).split(","):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"([A-Za-z_][\w\[\]*.]*)\s*=\s*(-?\d+)\s*\.\.\s*(-?\d+)", part)
            if not m or int(m.group(2)) > int(m.group(3)):
                raise NormsCheckError(f"--domain takes QUANTITY=lo..hi with integers lo <= hi, "
                                      f"such as ROBOT[*].load=0..5, got {part!r}")
            out[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return out


def build_model(sos: SoSDefinition, contract_id: str | None = None, bindings=None,
                members=None, step: str | int | None = None,
                horizon: int | None = None, domains=None, instances=None,
                versions=None) -> NormsModel:
    """Translate one contract instance (F.12). `bindings` and `members`
    map names to integers (or are lists of "name=n" texts), `step` is a
    duration text or milliseconds."""
    if not norms_declared(sos):
        raise NormsCheckError("the file does not declare 'norms: 0.1' under extensions:")
    contract = select_contract(sos, contract_id)
    if not isinstance(bindings, dict):
        bindings = _parse_assignments(bindings, "--bindings")
    if not isinstance(members, dict):
        members = _parse_assignments(members, "--members")
    step_ms = _parse_duration_ms(step) if isinstance(step, str) else step
    if not isinstance(domains, dict):
        domains = _parse_domains(domains)
    if versions and instances:
        raise NormsCheckError("--version and several instances are not translated together")
    if isinstance(instances, int):
        instances = _numbered_instances(sos, contract, bindings, members, instances)
    if instances:
        sets = [i if isinstance(i, dict) else _parse_assignments([i], "--instance")
                for i in instances]
        if len(sets) > MAX_INSTANCES:
            raise NormsCheckError(f"norms-check translates at most {MAX_INSTANCES} instances "
                                  f"together")
        trs = [Translator(sos, contract, {**bindings, **b}, members, step_ms, horizon, domains)
               for b in sets]
        Environment(trs, step_ms, horizon)
        return NormsModel(trs)
    tr = Translator(sos, contract, bindings, members, step_ms, horizon, domains)
    if versions:
        vtrs = _version_translators(tr, sos, contract, versions, bindings, members, step_ms,
                                    horizon, domains)
        Environment([tr] + vtrs, step_ms, horizon)
        return NormsModel(tr, versions=vtrs)
    Environment([tr], step_ms, horizon)
    return NormsModel(tr)


def _version_translators(tr: Translator, sos: SoSDefinition, contract: ContractDef,
                         versions: list, bindings: dict, members: dict, step_ms, horizon,
                         domains) -> list:
    """The later versions of the contract (F.6), in the order in which the
    environment may amend to them. Refuses what the model of amendments
    does not translate exactly."""
    from .norms_eval import load_version
    names, soses = [], []
    paths_of: dict = {}
    for v in versions:
        if isinstance(v, tuple):
            name, vs = v
        else:
            name, vs = Path(v).name, load_version(v)
            paths_of[name] = str(Path(v).resolve())
        names.append(name)
        soses.append(vs)
    out = []
    prev = sos
    for name, vs in zip(names, soses):
        new = next((c for c in vs.contracts if c.id == contract.id), None)
        if new is None:
            raise NormsCheckError(f"--version {name}: it has no contract {contract.id}")
        if declared_actors(vs) != declared_actors(prev):
            raise NormsCheckError(f"--version {name}: the actors differ from the previous "
                                  f"version; amendments are translated only between versions "
                                  f"with the same actors")
        old_c = next(c for c in prev.contracts if c.id == contract.id)
        if repr(old_c.lifecycle) != repr(new.lifecycle):
            raise NormsCheckError(f"--version {name}: the lifecycle differs from the previous "
                                  f"version; amendments are translated only between versions "
                                  f"with the same lifecycle")
        vt = Translator(vs, new, bindings, members, step_ms, horizon, domains)
        out.append(vt)
        prev = vs
    every = [tr] + out
    for k, t in enumerate(every):
        bad = [n for n in t.norms if n.status == "untranslated"]
        bad += [r for r in t.rules if r.status == "untranslated"]
        if bad:
            what = bad[0].id if hasattr(bad[0], "id") else f"counts_as -> {bad[0].creates}"
            raise NormsCheckError(
                f"amendments are translated only when every norm and counts-as rule of every "
                f"version is translated; in version {k + 1}, {what} is not: {bad[0].reason}")
    kinds: dict = {}
    for t in every:
        for n in t.norms:
            if kinds.setdefault(n.id, n.kind) != n.kind:
                raise NormsCheckError(
                    f"the norm {n.id} changes its kind between versions, which norms-check "
                    f"does not translate (F.6 discharges it)")
    if len(set(names)) != len(names):
        raise NormsCheckError("--version: two versions have the same file name")
    tr.version_names = names
    tr.version_sos = dict(zip(names, soses))
    tr.version_paths = paths_of
    return out


def _numbered_instances(sos: SoSDefinition, contract: ContractDef, bindings: dict,
                        members: dict, count: int) -> list:
    """--instances N: N instances that bind the one index of the contract
    that --bindings leaves open to the first N members of its set."""
    probe = Translator(sos, contract, bindings, members, None, None)
    free = sorted({v for v in probe.index_vars if v not in bindings})
    if len(free) != 1:
        raise NormsCheckError(
            f"--instances needs exactly one index that --bindings leaves open; the contract "
            f"has {', '.join(free) or 'none'}: give the instances with --instance i=1 ...")
    var = free[0]
    sets = sorted(s_ for s_, v in probe.index_refs if v == var)
    values = probe.members.get(sets[0], [])[:count] if sets else []
    if len(values) < count:
        raise NormsCheckError(f"--instances {count}: {sets[0] if sets else var} has only "
                              f"{len(values)} member(s) in the instance (use --members)")
    return [{var: v} for v in values]


# === The questions (F.12) ==================================================

@dataclass
class Question:
    key: str                           # e.g. "violable"
    subject: tuple                     # norm ids
    mode: str                          # "any": the instance may end at any observation;
    #                                    "full": only at the horizon
    prop: str                          # PRISM property
    target: str | None = None          # state formula whose states are printed
    target_label: str | None = None    # label of the target (when it is one)


def questions(model: NormsModel) -> list[Question]:
    out = []
    judged = [n for n in model.norms if n.judged]
    for n in model.norms:
        s = n.stem
        out.append(Question("activatable", (n.uid,), "any", f'Pmax=? [ F "a_{s}" ]',
                            target_label=f"a_{s}"))
        if n.judged:
            out.append(Question("violable", (n.uid,), "any", f'Pmax=? [ F "v_{s}" ]',
                                target_label=f"v_{s}"))
            out.append(Question("fulfillable", (n.uid,), "any", f'Pmax=? [ F "f_{s}" ]',
                                target_label=f"f_{s}"))
        if n.kind == "power":
            out.append(Question("exercisable", (n.uid,), "any", f'Pmax=? [ F "x_{s}" ]',
                                target_label=f"x_{s}"))
        if model.has(f"rp_{s}"):
            out.append(Question("reparation", (n.uid,), "any", f'Pmax=? [ F "r_{s}" ]',
                                target_label=f"r_{s}"))
    for n in judged:
        s = n.stem
        out.append(Question("unavoidable", (n.uid,), "full", f'Pmin=? [ F "v_{s}" ]'))
        # a state from which some choice activates n and every choice that
        # activates it leads to a state from which every continuation
        # violates it (an MDP: P<=0 [ X a ] says that no choice leads to a)
        trap = (f'!"end" & !(P<=0 [ X "j_{s}" ]) & '
                f'P<=0 [ X ("j_{s}" & !"v_{s}" & !(P>=1 [ F "v_{s}" ])) ]')
        out.append(Question("trap", (n.uid,), "full", f"Pmax=? [ F ({trap}) ]", target=trap))
    for a, b in itertools.combinations(judged, 2):
        va, vb = f'"v_{a.stem}"', f'"v_{b.stem}"'
        cs = (f"!{va} & !{vb} & !(P<1 [ G !{va} ]) & !(P<1 [ G !{vb} ]) & "
              f"P>=1 [ F ({va} | {vb}) ]")
        out.append(Question("conflict", (a.uid, b.uid), "full", f"Pmax=? [ F ({cs}) ]",
                            target=cs))
    return out


def find_checker() -> str | None:
    """The path of the PRISM executable on PATH, or None."""
    return shutil.which("prism")


def _value(text: str) -> Any:
    text = text.strip()
    if text == "true":
        return True
    if text == "false":
        return False
    return int(text)


def _parse_prism_output(out: str, count: int) -> list[dict]:
    if "Error:" in out:
        line = next(ln for ln in out.splitlines() if "Error:" in ln)
        raise NormsCheckError(f"PRISM reported an error: {line.strip()}")
    sections = out.split("\nModel checking: ")[1:]
    if len(sections) != count:
        raise NormsCheckError(f"PRISM checked {len(sections)} of {count} properties")
    results = []
    for sec in sections:
        m = re.search(r"\nResult: (\S+)", sec)
        if not m:
            raise NormsCheckError("PRISM gave no result for a property")
        raw = m.group(1)
        value: Any = raw == "true" if raw in ("true", "false") else float(raw)
        strat: dict = {}
        states: list = []
        if "Exporting strategy as actions below:" in sec:
            body = sec.split("Exporting strategy as actions below:", 1)[1]
            for line in body.splitlines():
                mm = re.fullmatch(r"\((.*)\)=(\w*)", line.strip())
                if mm:
                    strat[tuple(_value(v) for v in mm.group(1).split(","))] = mm.group(2)
                elif line.startswith("---"):
                    break
        if "Satisfying states that are also in filter" in sec:
            body = sec.split("Satisfying states that are also in filter", 1)[1]
            for line in body.splitlines()[1:]:
                mm = re.fullmatch(r"\d+:\((.*)\)", line.strip())
                if not mm:
                    break
                states.append(tuple(_value(v) for v in mm.group(1).split(",")))
        sm = re.search(r"\nStates:\s+(\d+)", sec)
        results.append({"value": value, "strategy": strat, "states": states,
                        "model_states": int(sm.group(1)) if sm else None})
    return results


def _java_memory() -> str:
    """The Java heap for PRISM: 4g, or half the physical memory when that
    is smaller (at least 1g)."""
    try:
        phys = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return "1g"
    gib = phys // 2 // (1 << 30)
    return f"{max(1, min(4, gib))}g"


def run_prism(prism: str, model_text: str, props: list[str], early_end: bool,
              timeout: float = 600, workdir: str | None = None) -> list[dict]:
    """Check `props` on the model with PRISM (explicit engine, with the
    strategies of the Pmax properties exported)."""
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        mp = Path(tmp) / "model.prism"
        pp = Path(tmp) / "questions.props"
        mp.write_text(model_text, encoding="utf-8")
        pp.write_text("\n".join(props) + "\n", encoding="utf-8")
        cmd = [prism, str(mp), str(pp), "-const", f"early_end={'true' if early_end else 'false'}",
               "-explicit", "-exportstrat", "stdout",
               "-javamaxmem", os.environ.get("CADL_PRISM_JAVAMAXMEM") or _java_memory()]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)   # the shell script and its JVM
            except OSError:
                proc.kill()
            proc.communicate()
            raise NormsCheckTimeout(
                f"the model checker did not finish within --timeout (PRISM stopped after "
                f"{timeout:.0f} s); the time grows with the number of commands (choices per "
                f"observation) and of reachable states: use a coarser --step, a smaller "
                f"--horizon or fewer --members") from None
    out = stdout + stderr
    if proc.returncode != 0 and "Error" not in out:
        raise NormsCheckError(f"PRISM failed (exit status {proc.returncode})")
    return _parse_prism_output(out, len(props))


# === Witnesses =============================================================

def _default_choice(model: NormsModel) -> int:
    """The choice with no message and no value (the first one)."""
    return 0


def replay(model: NormsModel, pm: PrismModel, strategy: dict, start: dict | None = None,
           stop=None, limit: int | None = None) -> tuple:
    """Follow a strategy from `start` (the initial state): returns the path
    [(choice, end), ...] and the states after each step. It stops at the
    end of the instance, at the first state for which `stop(state)` holds,
    or where the strategy gives no choice (then the instance continues
    with no message and no value until the horizon, or ends when it may)."""
    s = dict(start) if start is not None else pm.initial()
    path, states = [], []
    limit = limit or model.H + 2
    while not model.all_ended(s) and len(path) < limit:
        if stop is not None and stop(s):
            break
        label = strategy.get(pm.key(s))
        if not label:
            break
        cmd = pm.by_label(s, label)
        s = pm.apply(cmd, s)
        path.append(parse_label(label))
        states.append(s)
    return path, states


def finish(model: NormsModel, pm: PrismModel, s: dict) -> tuple:
    """Continue from s with no message and no value: the instance ends at
    the next observation when early_end allows it, else at the horizon."""
    path, states = [], []
    j = _default_choice(model)
    while not model.all_ended(s):
        if model.multi:
            alive = sum(1 << t.inst for t in model.trs if not s[f"{t.px}ended"])
            labels = [make_label(j, alive), make_label(j, 0)]
        else:
            labels = [f"e{j}", f"o{j}"]
        for label in labels:
            try:
                cmd = pm.by_label(s, label)
                break
            except ValueError:
                continue
        s = pm.apply(cmd, s)
        path.append(parse_label(label))
        states.append(s)
    return path, states


def _norm_result(result: dict, norm_id: str) -> dict:
    return next((n for n in result["norms"] if n["id"] == norm_id),
                {"id": norm_id, "state": "absent", "activated_at": None})


def confirm(model: NormsModel, path: list, claim: tuple) -> bool:
    """Whether norms-eval, on the trace of the path, agrees with the claim:
    ("violated", id), ("not violated", id), ("fulfilled", id), ("activated",
    id), ("exercised", id) or ("reparation", id)."""
    kind, nid = claim
    norm = next(n for n in model.norms if n.uid == nid)
    tr = model.trs[norm.inst]
    result = evaluate_path(tr, path)
    nid = norm.id
    if kind == "exercised":
        return any(e["kind"] == "power" and e.get("norm") == nid for e in result["timeline"])
    if kind == "reparation":
        return any(v["norm"] == nid and v["reparation_activated"] for v in result["violations"])
    r = _norm_result(result, nid)
    if kind == "violated":
        return r["state"] == "violated"
    if kind == "not violated":
        return r["state"] != "violated"
    if kind == "fulfilled":
        return r["state"] == "fulfilled"
    if kind == "activated":
        return r["activated_at"] is not None
    raise ValueError(kind)


_CLAIM = {"activatable": "activated", "violable": "violated", "fulfillable": "fulfilled",
          "exercisable": "exercised", "reparation": "reparation"}


def _witness(model: NormsModel, path: list, claim: tuple, explain: str) -> dict:
    norm = next(n for n in model.norms if n.uid == claim[1])
    w = {"claim": f"{claim[1]} {claim[0]}", "explanation": explain,
         "choices": [model.step_text(j, end) for j, end in path],
         "trace": trace_lines(model.trs[norm.inst], path),
         "confirmed": confirm(model, path, claim)}
    if model.multi:
        # the trace of every instance over the same observations
        w["instance"] = norm.inst + 1
        w["traces"] = {str(t.inst + 1): trace_lines(t, path) for t in model.trs}
    return w


# === Check =================================================================

def check(model: NormsModel, prism: str | None = None, timeout: float = 600) -> dict:
    """Run the questions of F.12 with PRISM and build the report;
    `timeout` (seconds) bounds all runs of PRISM together."""
    tr = model.tr
    report = _report_head(model)
    prism = prism or find_checker()
    if prism is None:
        report["checked"] = False
        report["skipped"] = ("no model checker found: put PRISM (prism) on PATH; the model can "
                             "still be written with --export")
        return report
    qs = questions(model)
    text = model.prism_text()
    answers: dict = {}
    budget = _Budget(timeout)
    report["runs"] = []
    for mode in ("any", "full"):
        mq = [q for q in qs if q.mode == mode]
        if not mq:
            # nothing to ask in this reading (no norm, or no obligation or
            # prohibition, is translated)
            continue
        props = []
        for q in mq:
            props.append(q.prop)
            if q.target is not None:
                props.append(f"filter(print, {q.target}, ({q.target}) | \"init\")")
        res = run_prism(prism, text, props, mode == "any", budget.left())
        report["runs"].append({"reading": "any end" if mode == "any" else "full",
                               "properties": len(props)})
        report["model"]["states"] = res[0]["model_states"] or report["model"]["states"]
        it = iter(res)
        for q in mq:
            r = next(it)
            if q.target is not None:
                r["states"] = next(it)["states"]
            answers[(q.key, q.subject)] = (q, r)
    pms = {mode: PrismModel(text, {"early_end": mode == "any"}) for mode in ("any", "full")}
    by_norm = {n["id"]: n for n in report["norms"]}
    for (key, subject), (q, r) in answers.items():
        pm = pms[q.mode]
        if key == "unavoidable":
            value = r["value"] >= 1.0
            entry = {"answer": value}
            if value:
                path, _ = finish(model, pm, pm.initial())
                entry["witness"] = _witness(model, path, ("violated", subject[0]),
                                            "every instance that runs to the horizon violates "
                                            "the norm; this is the one without messages and "
                                            "without values")
            by_norm[subject[0]]["unavoidable"] = entry
            continue
        possible = r["value"] > 0
        if key in _CLAIM:
            entry = {"answer": possible}
            if possible:
                lab = pm.labels[q.target_label]
                path, states = replay(model, pm, r["strategy"], stop=lambda s: bool(lab(s)))
                last = states[-1] if states else pm.initial()
                tail, _ = finish(model, pm, last)
                entry["witness"] = _witness(model, path + tail, (_CLAIM[key], subject[0]),
                                            "replayed from the strategy of PRISM")
            by_norm[subject[0]][key] = entry
        elif key == "trap":
            entry = {"answer": not possible}     # keepable: no trap is reachable
            if possible:
                targets = set(r["states"])
                path, states = replay(model, pm, r["strategy"],
                                      stop=lambda s: pm.key(s) in targets)
                last = states[-1] if states else pm.initial()
                if pm.key(last) in targets:
                    entry["witness"] = _trap_witness(model, pm, path, last, subject[0])
                else:
                    entry["witness"] = {"confirmed": False,
                                        "explanation": "the strategy did not reach the state"}
            by_norm[subject[0]]["keepable"] = entry
        elif key == "conflict":
            if not possible:
                continue
            targets = set(r["states"])
            path, states = replay(model, pm, r["strategy"], stop=lambda s: pm.key(s) in targets)
            last = states[-1] if states else pm.initial()
            item = {"norms": list(subject), "prefix": len(path)}
            if pm.key(last) not in targets:
                item["confirmed"] = False
                item["explanation"] = "the strategy did not reach the state"
            else:
                item.update(_conflict_witness(model, prism, text, pm, path, last, subject,
                                              budget.left()))
            report["conflicts"].append(item)
    _summary(report)
    return report


MAX_TRAP_CHOICES = 512


def _trap_witness(model: NormsModel, pm: PrismModel, path: list, state: dict,
                  nid: str) -> dict:
    """The witness of an activation that cannot be kept, from the state
    before that observation. norms-eval confirms, for every choice of the
    environment at that observation (at most MAX_TRAP_CHOICES of them, a
    seeded sample beyond), that it activates the norm exactly when the
    model says so, and, for every activating choice followed by
    observations without messages and values up to the horizon, that the
    norm is violated. That every other continuation violates it too rests
    on the model checker."""
    n = next(x for x in model.norms if x.uid == nid)
    tr = model.trs[n.inst]
    k = len(path)
    t_k = _t(model.tr.step_ms, k)
    cmds = pm.enabled(state)
    sampled = len(cmds) > MAX_TRAP_CHOICES
    if sampled:
        cmds = random.Random(0).sample(cmds, MAX_TRAP_CHOICES)
    activating, agree, violated, first = 0, True, True, None
    for cmd in cmds:
        after = pm.apply(cmd, state)
        step = [parse_label(cmd[0])]
        tail, _ = finish(model, pm, after)
        full = path + step + tail
        result = evaluate_path(tr, full)
        r = _norm_result(result, n.id)
        model_j = bool(after[f"jn_{n.stem}"])
        eval_j = r["activated_at"] is not None and r["activated_at"] == t_k
        agree = agree and model_j == eval_j
        if model_j:
            activating += 1
            violated = violated and r["state"] == "violated"
            if first is None:
                first = full
    w = _witness(model, first, ("violated", nid),
                 f"at observation {k} every choice of the environment that activates the norm "
                 f"leads to a state from which every continuation that runs to the horizon "
                 f"violates it; this trace takes one of them and continues without messages "
                 f"and values")
    w["prefix"] = k
    w["choices_checked"] = len(cmds)
    w["sampled"] = sampled
    w["activating_choices"] = activating
    w["confirmed"] = bool(w["confirmed"] and agree and violated and activating)
    w["confirms"] = (f"norms-eval: of the {len(cmds)} choice(s) at observation {k}"
                     f"{' (a sample)' if sampled else ''}, exactly the {activating} that the "
                     f"model says activate the norm do so, and each of them, continued without "
                     f"messages and values, violates it; that every other continuation violates "
                     f"it rests on the model checker")
    return w


def _conflict_witness(model: NormsModel, prism: str, text: str, pm: PrismModel, path: list,
                      state: dict, subject: tuple, timeout: int) -> dict:
    """From a state of conflict, two continuations: one that keeps the first
    norm and violates the second, and one the other way round."""
    init = {name: state[name] for name in pm.names}
    sub = model.prism_text(init)
    stems = {n.uid: n.stem for n in model.norms}
    props = [f'Pmax=? [ G !"v_{stems[nid]}" ]' for nid in subject]
    res = run_prism(prism, sub, props, False, timeout)
    out: dict = {"witnesses": []}
    for keep, other, r in ((subject[0], subject[1], res[0]), (subject[1], subject[0], res[1])):
        cont, states = replay(model, pm, r["strategy"], start=state)
        last = states[-1] if states else state
        tail, _ = finish(model, pm, last)
        full = path + cont + tail
        w = _witness(model, full, ("violated", other),
                     f"after {len(path)} observation(s) both norms are in force; keeping "
                     f"{keep} violates {other}")
        w["confirmed"] = w["confirmed"] and confirm(model, full, ("not violated", keep))
        w["keeps"] = keep
        out["witnesses"].append(w)
    out["confirmed"] = all(w["confirmed"] for w in out["witnesses"])
    return out


class _Budget:
    """The time left of --timeout for all runs of the model checker."""

    def __init__(self, seconds: float) -> None:
        import time
        self.seconds = seconds
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        import time
        left = self.end - time.monotonic()
        if left <= 0:
            raise NormsCheckTimeout(
                f"the model checker did not finish within --timeout {self.seconds:g} s; the "
                f"time grows with the number of commands (choices per observation) and of "
                f"reachable states: use a coarser --step, a smaller --horizon or fewer "
                f"--members")
        return left


def _report_head(model: NormsModel) -> dict:
    tr = model.tr
    norms = []
    if model.versioned:
        for n in model.norms:
            versions = [k + 1 for k, d in enumerate(model.slot_defs[n.id]) if d is not None]
            norms.append({"id": n.id, "kind": n.kind, "status": n.status, "reason": n.reason,
                          "versions": versions})
    for t in ([] if model.versioned else model.trs):
        for n in t.norms:
            entry = {"id": n.uid, "kind": n.kind, "status": n.status, "reason": n.reason}
            if model.multi:
                entry["instance"] = t.inst + 1
            norms.append(entry)
    return {
        "contract": tr.contract.id,
        "norms_check_version": NORMS_CHECK_VERSION,
        "instance": {
            "bindings": dict(sorted(tr.bindings.items())),
            "instances": [dict(sorted(t.bindings.items())) for t in model.trs],
            "versions": [tr.contract.id] and getattr(tr, "version_names", []),
            "members": {k: [v[0], v[-1]] for k, v in sorted(tr.members.items())},
            "step_ms": tr.step_ms,
            "horizon": model.H,
            "messages": [a.text() for a in tr.atoms],
            "quantities": {q.key: [_report_value(v) for v in q.reps]
                           for q in tr.quantities.values()},
            "choices": len(tr.choices),
            "domains": {q.key: list(q.domain) for q in tr.quantities.values()
                        if q.domain is not None},
        },
        "model": {"variables": len(model.vars), "commands": len(model.commands), "states": None},
        "norms": norms,
        "assumptions": [{"text": it["text"], "status": it["status"], "reason": it["reason"]}
                        for it in tr.items],
        "rules": [{"creates": r.creates, "status": r.status, "reason": r.reason}
                  for r in tr.rules],
        "excuse_known": model.excuse_known,
        "notes": list(dict.fromkeys(
            [x for t in model.trs for x in t.notes]
            + ([f"the {len(model.trs)} instances start together at the first observation and "
                f"share the observations, the message events and the quantities; each ends at "
                f"an observation of its own (traces whose instances start at different "
                f"observations are not covered)"] if model.multi else []))),
        "conflicts": [],
        "checked": True,
    }


def _summary(report: dict) -> None:
    findings = []
    unconfirmed = []

    def walk(w: dict, what: str) -> None:
        if w and not w.get("confirmed", True):
            unconfirmed.append(what)

    for n in report["norms"]:
        if n["status"] != "model":
            continue
        for key in ("activatable", "violable", "fulfillable", "exercisable", "reparation",
                    "keepable", "unavoidable"):
            if key in n:
                walk(n[key].get("witness"), f"{n['id']}: {key}")
        if "activatable" in n and not n["activatable"]["answer"]:
            findings.append(f"{n['id']} is never activated")
        if n.get("unavoidable", {}).get("answer"):
            findings.append(f"{n['id']} is violated in every instance that runs to the horizon")
        elif "keepable" in n and not n["keepable"]["answer"]:
            findings.append(f"{n['id']} can be activated in a state from which every "
                            f"continuation violates it")
        if "exercisable" in n and not n["exercisable"]["answer"]:
            findings.append(f"the power {n['id']} is never exercised")
    for c in report["conflicts"]:
        findings.append(f"{c['norms'][0]} and {c['norms'][1]} are in conflict")
        if not c.get("confirmed", False):
            unconfirmed.append(f"conflict {c['norms'][0]} / {c['norms'][1]}")
    report["findings"] = findings
    report["unconfirmed"] = unconfirmed


# === Export and report =====================================================

def props_text(model: NormsModel, mode: str) -> str:
    head = ("// early_end=true: the instance may end at every observation"
            if mode == "any" else
            "// early_end=false: the instance ends at the horizon")
    out = [head, f"// check with: prism <model>.prism <this file> -const "
                 f"early_end={'true' if mode == 'any' else 'false'}"]
    for q in questions(model):
        if q.mode == mode:
            out.append(f"// {q.key} {' / '.join(q.subject)}")
            out.append(q.prop)
    return "\n".join(out) + "\n"


def _file_stem(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


def export(model: NormsModel, report: dict | None, directory) -> list[str]:
    """Write the model, the two property files and the witness traces."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    stem = _file_stem(model.tr.contract.id)
    paths = getattr(model.tr, "version_paths", {})

    def _lines(lines: list) -> str:
        # the amend line names the version file relative to the trace file
        out = []
        for line in lines:
            if '"amend"' in line:
                from .norms_eval import loads_strict
                obs = loads_strict(line)       # keeps the digits of the numbers
                if obs.get("amend") in paths:
                    obs["amend"] = os.path.relpath(paths[obs["amend"]], d.resolve())
                line = json_text(obs)
            out.append(line)
        return "\n".join(out)

    files = {f"{stem}.prism": model.prism_text(),
             f"{stem}_any.props": props_text(model, "any"),
             f"{stem}_full.props": props_text(model, "full")}
    if report is not None and report.get("checked"):
        for n in report["norms"]:
            for key, entry in n.items():
                w = entry.get("witness") if isinstance(entry, dict) else None
                if w and "trace" in w:
                    name = f"witness_{_file_stem(n['id'])}_{key}.jsonl"
                    files[name] = _lines(w["trace"]) + "\n"
                    w["file"] = str(d / name)
                    for k, lines in w.get("traces", {}).items():
                        files[name.replace(".jsonl", f"_instance{k}.jsonl")] = _lines(lines) + "\n"
        for c in report["conflicts"]:
            for w in c.get("witnesses", []):
                name = (f"witness_conflict_{_file_stem(c['norms'][0])}_"
                        f"{_file_stem(c['norms'][1])}_keep_{_file_stem(w['keeps'])}.jsonl")
                files[name] = _lines(w["trace"]) + "\n"
                w["file"] = str(d / name)
                for k, lines in w.get("traces", {}).items():
                    files[name.replace(".jsonl", f"_instance{k}.jsonl")] = _lines(lines) + "\n"
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    if report is not None:
        report["exported"] = [str(d / name) for name in files]
    return [str(d / name) for name in files]


_QUESTION_TEXT = {
    "activatable": "activated",
    "violable": "violated",
    "fulfillable": "fulfilled",
    "keepable": "activated where every continuation violates it",
    "unavoidable": "violated (as in every full-length instance)",
    "exercisable": "exercised",
    "reparation": "violated, reparation activated",
}


def _yn(entry: dict | None) -> str:
    if entry is None:
        return "-"
    return "yes" if entry["answer"] else "no"


def _brief(w: dict, limit: int = 6) -> str:
    """The observations of a witness that carry something, in short."""
    parts = []
    for k, text in enumerate(w["choices"]):
        if text == "no message":
            continue
        parts.append(f"#{k}: {text}")
    if len(parts) > limit:
        parts = parts[:limit - 1] + ["...", parts[-1]]
    return "; ".join(parts)


def format_text(report: dict) -> str:
    inst = report["instance"]
    lines = [f"Contract {report['contract']} (norms-check {report['norms_check_version']})"]
    b = ", ".join(f"{k}={v}" for k, v in inst["bindings"].items()) or "none"
    if len(inst.get("instances", [])) > 1:
        b = "; ".join(f"instance {k + 1}: " + (", ".join(f"{x}={v}" for x, v in bs.items())
                                               or "none")
                      for k, bs in enumerate(inst["instances"]))
    mem = ", ".join(f"{k}[{lo}..{hi}]" for k, (lo, hi) in inst["members"].items()) or "none"
    lines.append(f"  instance: bindings {b}; members {mem}")
    lines.append(f"  time: one observation every {inst['step_ms'] / 1000:g}s, "
                 f"at most {inst['horizon'] + 1} observations (horizon {inst['horizon']})")
    lines.append(f"  environment: {len(inst['messages'])} message event(s), "
                 f"{len(inst['quantities'])} quantit{'y' if len(inst['quantities']) == 1 else 'ies'}, "
                 f"{inst['choices']} choices per observation")
    st = report["model"].get("states")
    lines.append(f"  model: {report['model']['variables']} variables, "
                 f"{report['model']['commands']} commands"
                 + (f", {st} reachable states" if st else ""))
    for note in report["notes"]:
        lines.append(f"  note: {note}")
    if not report.get("checked"):
        lines.append("")
        lines.append(f"Not checked: {report.get('skipped')}")
    elif not report.get("runs"):
        lines.append("")
        lines.append("Nothing to ask the model checker: no norm is translated")
    else:
        lines.append("")
        lines.append("Norms (any end: the instance may end at every observation; "
                     "full: it runs to the horizon)")
        rows = [["id", "kind", "activated", "violable", "fulfillable", "kept", "unavoidable",
                 "exercised", "reparation"]]
        for n in report["norms"]:
            if n["status"] != "model":
                continue
            rows.append([n["id"], n["kind"], _yn(n.get("activatable")), _yn(n.get("violable")),
                         _yn(n.get("fulfillable")), _yn(n.get("keepable")),
                         _yn(n.get("unavoidable")), _yn(n.get("exercisable")),
                         _yn(n.get("reparation"))])
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for r in rows:
            lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
        lines.append("")
        lines.append("Conflicts (full)")
        if not report["conflicts"]:
            lines.append("  (none)")
        for c in report["conflicts"]:
            lines.append(f"  {c['norms'][0]} / {c['norms'][1]}: after {c.get('prefix', '?')} "
                         f"observation(s) keeping one violates the other")
            for w in c.get("witnesses", []):
                lines.append(f"    keep {w['keeps']}: {w['claim']} "
                             f"[{'confirmed' if w['confirmed'] else 'NOT CONFIRMED'} by norms-eval]")
                lines.append(f"      {w.get('file') or _brief(w)}")
        lines.append("")
        lines.append("Witnesses (each one a trace that norms-eval evaluates)")
        count = 0
        for n in report["norms"]:
            for key, entry in n.items():
                w = entry.get("witness") if isinstance(entry, dict) else None
                if not w:
                    continue
                count += 1
                ok = "confirmed" if w.get("confirmed") else "NOT CONFIRMED"
                lines.append(f"  {n['id']} {_QUESTION_TEXT[key]} [{ok}]")
                if "choices" in w:
                    lines.append(f"    {w.get('file') or _brief(w)}")
                if "confirms" in w:
                    lines.append(f"    {w['confirms']}")
        if not count:
            lines.append("  (none)")
    untranslated = [n for n in report["norms"] if n["status"] != "model"]
    untranslated += [{"id": f"assume: {a['text']}", "reason": a["reason"]}
                     for a in report["assumptions"] if a["status"] == "untranslated"]
    untranslated += [{"id": f"counts_as -> {r['creates']}", "reason": r["reason"]}
                     for r in report["rules"] if r["status"] == "untranslated"]
    if untranslated:
        lines.append("")
        lines.append("Not translated")
        for n in untranslated:
            lines.append(f"  {n['id']}: {n['reason']}")
    if not report.get("excuse_known", True):
        lines.append("  (an assumption is not translated: a violation may be excused)")
    if report.get("checked"):
        lines.append("")
        lines.append("Findings")
        for f in report["findings"] or ["(none)"]:
            lines.append(f"  {f}")
        if report["unconfirmed"]:
            lines.append("")
            lines.append("Witnesses not confirmed by norms-eval (an error of norms-check):")
            for u in report["unconfirmed"]:
                lines.append(f"  {u}")
    if report.get("exported"):
        lines.append("")
        lines.append("Written: " + ", ".join(report["exported"]))
    return "\n".join(lines)


def check_file(path, contract_id=None, bindings=None, members=None, step=None,
               horizon=None, export_dir=None, prism=None, timeout: float = 600,
               domains=None, instances=None, versions=None) -> dict:
    """`cadl norms-check`: build the model of a file, check it, export."""
    import time
    from .norms_eval import load_version
    start = time.monotonic()
    sos = load_version(path)
    _DEADLINE[0] = start + timeout
    try:
        model = build_model(sos, contract_id, bindings, members, step, horizon, domains,
                            instances, versions)
    finally:
        _DEADLINE[0] = None
    built = time.monotonic() - start
    if export_dir is not None:
        export(model, None, export_dir)       # the model, also when the check fails
    left = timeout - (time.monotonic() - start)
    if left <= 0:
        raise NormsCheckTimeout("building the model used up --timeout")
    report = check(model, prism, left)
    report["model"]["generation_s"] = round(built, 1)
    report["file"] = str(path)
    if export_dir is not None:
        export(model, report, export_dir)
    return report
