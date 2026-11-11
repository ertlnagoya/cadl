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

``t``         time in seconds (required): a finite number of at most
              1e10 in absolute value (about 317 years).
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
from pathlib import Path
from typing import Any

from .ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    ContractDef,
    DurationLiteral,
    FloatLiteral,
    FunctionCall,
    IntLiteral,
    MemberAccess,
    NormDef,
    RangeExpr,
    SoSDefinition,
    StringLiteral,
    UnaryOp,
)
from .norms import TEMPORAL_OPERATORS, contains_temporal, item_text, norms_declared

_EPS = 1e-9
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0}
_MESSAGE_EVENT_RE = re.compile(r"^(.+?)\s*->\s*(.+?)\s*:\s*(.+)$")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_ACTOR_RE = re.compile(r"^([A-Za-z_]\w*)(?:\[(.*)\])?$")
# A concrete actor of a message and a message name, as written: no
# whitespace is removed, so `PLAN NER` is not read as `PLANNER`.
_TRACE_ACTOR_RE = re.compile(r"[A-Za-z_]\w*(?:\[(?:0|[1-9][0-9]*)\])?")
_TRACE_NAME_RE = re.compile(r"[A-Za-z_]\w*")
# Largest |t| of a trace, in seconds: far inside the range in which a time
# can be added to an ISO 8601 epoch without overflow.
MAX_T = 1e10
_TRACE_KEYS = {"t", "bindings", "request", "values", "messages", "state", "end", "amend"}
_MESSAGE_KEYS = {"from", "to", "name", "args"}
FINAL_STATES = ("fulfilled", "violated", "discharged")


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


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite number")
    return value


def loads_strict(line: str) -> Any:
    """`json.loads` that rejects NaN, Infinity and numbers that overflow
    to infinity; raises ValueError."""
    return json.loads(line, parse_constant=_not_json_number, parse_float=_finite)


def declared_actors(sos: SoSDefinition) -> dict[str, tuple]:
    """The declared actors: name -> (indexed, lo, hi); `lo` and `hi` are
    None when the set has no integer bounds."""
    out: dict[str, tuple] = {}
    for a in sos.actors:
        index = a.id.index
        if isinstance(index, RangeExpr) and isinstance(index.start, int) \
                and isinstance(index.end, int):
            out[a.id.name] = (True, index.start, index.end)
        else:
            out[a.id.name] = (index is not None, None, None)
    return out


def parse_trace(lines, path: str | None = None) -> Trace:
    """Read a trace from an iterable of JSON Lines. The lifecycle state is
    left as written; `evaluate` fills in the initial state."""
    trace = Trace(observations=[], path=path)
    snapshot: dict[str, Any] = {}
    state: str | None = None
    prev_t: float | None = None
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        where = f"line {lineno}"
        try:
            rec = loads_strict(line)
        except json.JSONDecodeError as e:
            raise TraceError(f"{where}: not JSON ({e.msg})") from None
        except ValueError as e:
            raise TraceError(f"{where}: not JSON ({e})") from None
        if not isinstance(rec, dict):
            raise TraceError(f"{where}: an observation must be a JSON object")
        unknown = sorted(set(rec) - _TRACE_KEYS)
        if unknown:
            raise TraceError(f"{where}: unknown key(s) {', '.join(unknown)}")
        t = rec.get("t")
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise TraceError(f"{where}: 't' must be a number (seconds)")
        if not math.isfinite(t) or abs(t) > MAX_T:
            raise TraceError(f"{where}: 't' must be a finite number of at most "
                             f"{MAX_T:g} seconds in absolute value")
        if prev_t is not None and t < prev_t:
            raise TraceError(f"{where}: time {t:g} is before the previous observation ({prev_t:g})")
        prev_t = t
        first = not trace.observations
        for key in ("bindings", "request"):
            if key in rec:
                if not first:
                    raise TraceError(f"{where}: '{key}' is allowed only on the first line")
                if not isinstance(rec[key], dict):
                    raise TraceError(f"{where}: '{key}' must be an object")
                setattr(trace, key, rec[key])
        values = rec.get("values", {})
        if not isinstance(values, dict):
            raise TraceError(f"{where}: 'values' must be an object")
        for k, v in values.items():
            snapshot[_canon_actor(k)] = v
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
                         "name": m["name"].strip(), "args": args})
        before = state
        if "state" in rec:
            if not isinstance(rec["state"], str):
                raise TraceError(f"{where}: 'state' must be a string")
            state = rec["state"]
        end = rec.get("end")
        if end is not None and not isinstance(end, bool):
            raise TraceError(f"{where}: 'end' must be true or false")
        amend = rec.get("amend")
        if amend is not None and (not isinstance(amend, str) or not amend.strip()):
            raise TraceError(f"{where}: 'amend' must be the path of a CADL file")
        trace.observations.append(Observation(
            t=float(t), values=dict(snapshot), messages=msgs,
            state=before, state_after=state, end=end, amend=amend,
        ))
    if not trace.observations:
        raise TraceError("the trace has no observation")
    return trace


def load_trace(path) -> Trace:
    with open(path, encoding="utf-8") as f:
        return parse_trace(f, path=str(path))


def _fmt_t(t: float | None) -> str:
    return "-" if t is None else f"{t:g}"


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
        self._cache: dict[tuple[int, int], tuple[Any, Any]] = {}
        # Declared actors (`declared_actors`); None when they are not known.
        self.actors: dict[str, tuple] | None = None

    def is_member(self, name: str, index: str | None) -> bool:
        """Whether `name` (index None) or `name[index]` is a declared actor
        or a member of a declared set, within its integer bounds."""
        if self.actors is None:
            return True
        decl = self.actors.get(name)
        if decl is None:
            return False
        indexed, lo, hi = decl
        if not indexed or index is None:
            return not indexed and index is None
        if not index.isdigit():
            return False
        return lo is None or lo <= int(index) <= hi

    def warn(self, text: str) -> None:
        if text not in self._warned:
            self._warned.add(text)
            self.warnings.append(text)

    # --- static -----------------------------------------------------------

    def unsupported(self, expr: Any, temporal: bool) -> str | None:
        """Why an expression cannot be evaluated, or None."""
        if isinstance(expr, (IntLiteral, FloatLiteral, BoolLiteral, StringLiteral, DurationLiteral)):
            return None
        if isinstance(expr, ActorRef):
            if expr.index == "*" or isinstance(expr.index, RangeExpr):
                return "a wildcard or range actor reference is not evaluable"
            if expr.index is not None and not isinstance(expr.index, (str, int)):
                return self.unsupported(expr.index, False)
            return None
        if isinstance(expr, MemberAccess):
            return self.unsupported(expr.obj, False)
        if isinstance(expr, UnaryOp):
            return self.unsupported(expr.operand, temporal)
        if isinstance(expr, BinaryOp):
            return self.unsupported(expr.left, temporal) or self.unsupported(expr.right, temporal)
        if isinstance(expr, FunctionCall):
            if expr.name == "__in__":
                for a in expr.args:
                    r = self.unsupported(a, False)
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
                        r = self.unsupported(a, True)
                        if r:
                            return r
                return None
            return f"the function call {expr.name}() is not evaluable"
        return f"{type(expr).__name__} is not evaluable"

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
            if name in self.bindings:
                return f"{ref.name}[{self.bindings[name]}]", True
            self.warn(f"the index '{name}' of {ref.name}[{name}] is not bound by the trace "
                      f"('bindings'); it matches any {ref.name}")
            return f"{ref.name}[{name}]", False
        v = self.value(idx, k, k)
        if v is UNKNOWN or v is UNDECIDED:
            return f"{ref.name}[?]", False
        if isinstance(v, float) and v.is_integer():
            v = int(v)
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
        if isinstance(expr, (IntLiteral, FloatLiteral, BoolLiteral, StringLiteral)):
            return expr.value
        if isinstance(expr, DurationLiteral):
            return expr.value * _UNIT_SECONDS.get(expr.unit, 1.0)
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
            text, _ = self.actor(obj, k)
            stripped = f"{obj.name}.{expr.member}" if obj.index is not None else None
            return self._lookup(f"{text}.{expr.member}", k, stripped)
        if isinstance(expr, UnaryOp):
            v = self.truth(self.value(expr.operand, k, n), expr.operand)
            return v if isinstance(v, _Marker) else (not v)
        if isinstance(expr, BinaryOp):
            return self._binary(expr, k, n)
        if isinstance(expr, FunctionCall) and expr.name in TEMPORAL_OPERATORS:
            key = (id(expr), k)
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
        if isinstance(v, (int, float)):
            return bool(v)
        self.warn(f"the value {v!r} is used as a condition but is not a boolean")
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
            if isinstance(left, (bool, str)) or isinstance(right, (bool, str)):
                raise TypeError
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                return left / right
        except (TypeError, ZeroDivisionError):
            self.warn(f"'{op}' cannot be applied to {left!r} and {right!r}; the predicate does not hold")
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

    # --- temporal operators (F.4) -------------------------------------------

    def _window(self, k: int, n: int, d: float) -> tuple[range, bool]:
        """The observations of [t_k, t_k + d] among 0..n, and whether the
        interval is known to be over (an observation after it exists)."""
        limit = self.obs[k].t + d + _EPS
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
        from .parser import parse_expr, parse_rule
        self.text = text.strip()
        self.ev = ev
        self.error: str | None = None
        self.expr: Any = None
        self._edge: dict[int, bool] = {}
        m = _MESSAGE_EVENT_RE.match(self.text) if "->" in self.text else None
        if self.text in events and _IDENTIFIER_RE.fullmatch(self.text):
            self.kind = "event"
        elif m:
            from .parser import _parse_actor_ref_str
            self.kind = "message"
            self.sender = _parse_actor_ref_str(m.group(1).strip())
            self.receiver = _parse_actor_ref_str(m.group(2).strip())
            self.msg_name = m.group(3).strip()
            self.msg_args: list | None = None
            try:
                msg = parse_expr(self.msg_name)
            except Exception:
                msg = None
            if isinstance(msg, FunctionCall):
                self.msg_name = msg.name
                self.msg_args = msg.args or None
                for a in msg.args:
                    self.error = self.error or ev.unsupported(a, False)
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
                self.expr = parse_rule(self.text)
            except Exception:
                self.error = "it is not a predicate (Appendix A.10)"
            else:
                self.error = ev.unsupported(self.expr, temporal)

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
        self.deadline = None if d.deadline_ms is None else d.deadline_ms / 1000.0
        self.frozen: str | None = None
        for key, term in (("trigger", self.trigger), ("until", self.until), ("content", self.content)):
            if term is not None and term.error and not self.frozen:
                self.frozen = f"not evaluable: {key} '{term.text}': {term.error}"
        self.activated_at: float | None = None
        self.activated_index: int | None = None
        self.active_since: float | None = None
        self.elapsed_before = 0.0          # active time before the current stretch
        self.judge_from = 0
        self.candidates: list[int] = []    # observations whose content is undecided
        self.time: float | None = None
        self.reason = self.frozen or "not activated"
        self.history: list[dict] = []
        bearer_ref = None
        if d.bearer:
            from .parser import _parse_actor_ref_str
            bearer_ref = _parse_actor_ref_str(d.bearer)
        self.bearer_ref = bearer_ref

    def elapsed(self, t: float) -> float:
        if self.state == "active" and self.active_since is not None:
            return self.elapsed_before + (t - self.active_since)
        return self.elapsed_before


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
        self.events = {r.creates for r in contract.counts_as if r.creates}
        self.norms = [_NormInst(d, self.events, self.ev) for d in contract.norms]
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

    def _warn_frozen(self, norm: _NormInst) -> None:
        if norm.frozen:
            self.ev.warn(f"norm '{norm.d.id}' is not judged ({norm.frozen})")


    def _build_rules(self) -> None:
        self.rules = []
        for r in self.contract.counts_as:
            when = _Term(r.when, set(), self.ev)
            cond = _Term(r.if_, set(), self.ev) if r.if_ else None
            if cond is not None and cond.kind != "predicate":
                cond.error = cond.error or "it is not a predicate"
            for key, term in (("when", when), ("if", cond)):
                if term is not None and term.error:
                    self.ev.warn(
                        f"counts_as rule creating '{r.creates}' is not evaluated: "
                        f"{key} '{term.text}': {term.error}")
            self.rules.append((r, when, cond))

    def _init_states(self) -> None:
        lc = self.lifecycle
        if lc is None:
            if any(o.state_after is not None for o in self.obs) and not any(
                    o.amend for o in self.obs):
                self.warnings.append(
                    "the trace gives 'state' but the contract has no lifecycle:; it is ignored")
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
        from .parser import parse_rule
        text = item_text(expr)
        if isinstance(expr, StringLiteral):
            # kept as text by the parser; it may still be a rule with IN
            try:
                expr = parse_rule(expr.value)
            except Exception:
                expr = None
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
                table[oid] = _NormInst(nd, self.events, self.ev)
                table[oid].history = o.history
                self._warn_frozen(table[oid])
                if _definition(o.d) != _definition(nd):
                    effect(oid, "new_definition", state, state,
                           "takes the definition of the new version")
            elif o.d.on_amendment == "discharge":
                self._set(o, n, "discharged", "amendment: on_amendment is discharge", log=False)
                table[oid] = o
                effect(oid, "discharge", state, "discharged", "on_amendment is discharge")
            elif o.d.on_amendment == "transform":
                elapsed = o.elapsed(t)
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
                if o.d.deadline_ms != nd.deadline_ms:
                    changes.append(f"deadline changes from {_fmt_d(o.d.deadline_ms)} "
                                   f"to {_fmt_d(nd.deadline_ms)}")
                elapsed = inst.elapsed(t)
                if (nd.kind == "obligation" and inst.deadline is not None and not inst.frozen
                        and elapsed > inst.deadline + _EPS):
                    why = (f"the elapsed time of {_fmt_t(elapsed)}s exceeds the new deadline "
                           f"of {_fmt_t(inst.deadline)}s")
                    self._set(inst, n, "violated", f"amendment: {why}", log=False)
                    self._amend_violated.append(inst)
                    effect(oid, "carry_over", state, "violated", why)
                else:
                    effect(oid, "carry_over", state, state,
                           "; ".join(["continues under the new version"] + changes))
        added = []
        for d in new.norms:
            if d.id not in table and d.id not in old_ids:
                table[d.id] = _NormInst(d, self.events, self.ev)
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
                x.elapsed_before = elapsed if timed else 0.0
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
        inst = _NormInst(nd, self.events, self.ev)
        self._warn_frozen(inst)
        for attr in ("state", "verdict", "activated_at", "activated_index", "active_since",
                     "elapsed_before", "judge_from", "time", "history"):
            setattr(inst, attr, getattr(o, attr))
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
                if n < last:
                    self.warnings.append(
                        f"{last - n} observation(s) after the end of the contract instance "
                        f"(t={_fmt_t(self.obs[n].t)}) are ignored")
                break
        self._finish_items()

    def _step(self, n: int, last: bool) -> bool:
        """Process observation n; `last` says that it is the last one of
        the trace. Returns True when the contract instance ended at it."""
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
        elif last and o.end is not False:
            reason = "last observation of the trace"
        if reason:
            self._settle(n, reason)
            return True
        return False

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
                open_ = norm.deadline is None or elapsed <= norm.deadline + _EPS
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
                elif norm.elapsed(t) <= norm.deadline + _EPS:
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
        return {
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
        }


def select_contract(sos: SoSDefinition, contract_id: str | None = None) -> ContractDef:
    """The contract to evaluate: the named one, else the only contract with
    norms: or counts_as:, else the only contract of the file."""
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
        "the file has several contracts; choose one with --contract "
        f"({', '.join(c.id for c in pool)})")


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
             versions: dict | None = None) -> dict:
    """Compute the verdicts of one contract instance on a trace (F.5),
    applying the amendments the trace names (F.6).

    `versions` maps the value of an ``amend`` key to a parsed file; a value
    that is not in it is read as a path relative to the trace file.
    Returns a JSON-serializable dict; see the README for its keys."""
    if not norms_declared(sos):
        raise NormsEvalError("the file does not declare 'norms: 0.1' under extensions:")
    contract = select_contract(sos, contract_id)

    def loader(name: str) -> SoSDefinition:
        if versions and name in versions:
            new = versions[name]
            if not norms_declared(new):
                raise NormsEvalError(f"{name} does not declare 'norms: 0.1' under extensions:")
            return new
        base = Path(trace.path).parent if trace.path else Path(".")
        return load_version(base / name)

    engine = _Engine(contract, trace, loader, declared_actors(sos))
    engine.run()
    return engine.result()


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


def evaluate_files(cadl_path, trace_path, contract_id: str | None = None) -> dict:
    """Parse and type-check a CADL file, read a trace and evaluate it."""
    if not Path(trace_path).exists():
        raise NormsEvalError(f"File not found: {trace_path}")
    sos = load_version(cadl_path)
    trace = load_trace(trace_path)
    result = evaluate(sos, trace, contract_id)
    result["file"] = str(cadl_path)
    return result
