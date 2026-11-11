"""The norms of one contract as plain data, for the norms monitors of the
OPA and Solidity targets (Appendix F, F.5; Appendix D).

`cadl codegen -t opa` and `-t solidity` generate a monitor of the norms,
the counts-as rules and the ``assume:`` / ``guarantee:`` items of each
contract of a file that declares ``norms: 0.1``. Both start from the
description built here: `build_ir` reads a contract the way the engine of
`cadl norms-eval` (cadl/norms_eval.py) reads it, by building that engine
on an empty trace, and writes down what the engine decided at that point
(the kind of every trigger, until, content, when and if, the norms that
are not evaluable, the reparations and the targets of powers) together
with every predicate as a tree of plain dicts.

What a target cannot monitor is reported as `NormsTargetUnsupported`
with the reasons; the generator then leaves the contract without a
monitor (or, for a guarantee item, leaves the item out), and says so. A
construct is never monitored in a way that differs from norms-eval.

The tree of a predicate is a list of nodes; a node refers to its
children by their position in the list:

``{"op": "lit", "v": value}``           a number, boolean or string literal;
                                        a duration is its number of seconds
``{"op": "name", "n": name}``           a bare name (``demand``, ``now``,
                                        ``state``, a binding, an enum literal)
``{"op": "sym", "a": A, "i": index}``   the actor ``A[index]`` as a value
``{"op": "mem", "a": A, "i": index|None, "m": member}``
                                        the quantity ``A.member`` / ``A[index].member``
``{"op": "req", "path": [...]}``        a field of ``request``
``{"op": "not", "x": id}``
``{"op": "and"|"or", "l": id, "r": id}``
``{"op": "bin", "o": op, "l": id, "r": id}``
                                        == != < <= > >= + - * /
``{"op": "in", "l": id, "items": [id, ...]}``
``{"op": "within"|"throughout", "d": seconds, "x": id}``
``{"op": "until", "p": id, "q": id, "d": seconds}``

An index is ``{"lit": text}`` (``A[2]``) or ``{"var": name}`` (``A[i]``, bound
by the bindings of the instance).
"""

from __future__ import annotations

import math
import re
from typing import Any

from ..ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    ContractDef,
    DurationLiteral,
    FloatLiteral,
    FunctionCall,
    IntLiteral,
    MemberAccess,
    QuantifiedExpr,
    RangeExpr,
    SoSDefinition,
    StringLiteral,
    UnaryOp,
)
from ..norms import TEMPORAL_OPERATORS

# Effects of a power, in the order in which the engine applies them.
EFFECT_KINDS = ("activate", "suspend", "resume", "discharge")
_COMPARE = ("==", "!=", "<", "<=", ">", ">=")
_ARITH = ("+", "-", "*", "/")


class NormsTargetUnsupported(ValueError):
    """A contract (or an item of it) uses a construct that the monitor of
    a target cannot evaluate as `cadl norms-eval` does."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(dict.fromkeys(reasons))
        super().__init__("; ".join(self.reasons))


def _number(value: float, what: str) -> float:
    if isinstance(value, float) and not math.isfinite(value):
        raise NormsTargetUnsupported([f"{what}: the number {value!r} is not finite"])
    return value


class _Exprs:
    """Converts predicates into the node list."""

    def __init__(self, contract_id: str) -> None:
        self.nodes: list[dict] = []
        self.contract_id = contract_id

    def add(self, node: dict) -> int:
        self.nodes.append(node)
        return len(self.nodes) - 1

    def index(self, idx: Any, where: str) -> dict:
        if isinstance(idx, ActorRef) and idx.index is None:
            return {"var": idx.name}
        if isinstance(idx, str):
            if idx.isdigit():
                return {"lit": idx}
            return {"var": idx}
        if isinstance(idx, (IntLiteral, FloatLiteral, BoolLiteral, StringLiteral)):
            v = idx.value
            if isinstance(v, float):
                _number(v, where)
                if v.is_integer():
                    v = int(v)
            return {"lit": f"{v}"}
        raise NormsTargetUnsupported([
            f"{where}: the index of an actor reference must be a literal or a name "
            f"bound by the instance (an expression such as A[i+1] is not supported)"])

    def convert(self, expr: Any, where: str) -> int:
        if isinstance(expr, BoolLiteral):
            return self.add({"op": "lit", "v": bool(expr.value)})
        if isinstance(expr, (IntLiteral, FloatLiteral)):
            return self.add({"op": "lit", "v": _number(expr.value, where)})
        if isinstance(expr, StringLiteral):
            return self.add({"op": "lit", "v": str(expr.value)})
        if isinstance(expr, DurationLiteral):
            return self.add({"op": "lit", "v": duration_seconds(expr)})
        if isinstance(expr, ActorRef):
            if expr.index is None:
                return self.add({"op": "name", "n": expr.name})
            if expr.index == "*" or isinstance(expr.index, RangeExpr):
                raise NormsTargetUnsupported([f"{where}: a wildcard or range reference ([*], [lo..hi]) is not supported"])
            return self.add({"op": "sym", "a": expr.name, "i": self.index(expr.index, where)})
        if isinstance(expr, MemberAccess):
            obj = expr.obj
            if obj.index is None and obj.name == "request":
                return self.add({"op": "req", "path": expr.member.split(".")})
            if obj.index == "*" or isinstance(obj.index, RangeExpr):
                raise NormsTargetUnsupported([f"{where}: a wildcard or range reference ([*], [lo..hi]) is not supported"])
            idx = None if obj.index is None else self.index(obj.index, where)
            return self.add({"op": "mem", "a": obj.name, "i": idx, "m": expr.member})
        if isinstance(expr, UnaryOp):
            if expr.op != "NOT":
                raise NormsTargetUnsupported([f"{where}: the operator {expr.op}"])
            return self.add({"op": "not", "x": self.convert(expr.operand, where)})
        if isinstance(expr, BinaryOp):
            if expr.op in ("AND", "OR"):
                return self.add({"op": expr.op.lower(), "l": self.convert(expr.left, where),
                                 "r": self.convert(expr.right, where)})
            if (expr.op == "==" and isinstance(expr.right, FunctionCall)
                    and expr.right.name == "__in__"):
                left = self.convert(expr.left, where)
                items = [self.convert(a, where) for a in expr.right.args]
                return self.add({"op": "in", "l": left, "items": items})
            if expr.op in _COMPARE or expr.op in _ARITH:
                return self.add({"op": "bin", "o": expr.op, "l": self.convert(expr.left, where),
                                 "r": self.convert(expr.right, where)})
            raise NormsTargetUnsupported([f"{where}: the operator {expr.op}"])
        if isinstance(expr, FunctionCall) and expr.name in TEMPORAL_OPERATORS:
            nargs, dpos = TEMPORAL_OPERATORS[expr.name]
            d = duration_seconds(expr.args[dpos])
            if expr.name == "until":
                return self.add({"op": "until", "p": self.convert(expr.args[0], where),
                                 "q": self.convert(expr.args[1], where), "d": d})
            return self.add({"op": expr.name, "d": d, "x": self.convert(expr.args[1], where)})
        if isinstance(expr, FunctionCall):
            raise NormsTargetUnsupported([
                f"{where}: the aggregate or function {expr.name}() is not supported"])
        if isinstance(expr, QuantifiedExpr) and expr.variable.startswith("*"):
            raise NormsTargetUnsupported([
                f"{where}: a wildcard or range reference ([*], [lo..hi]) is not supported"])
        if isinstance(expr, QuantifiedExpr):
            raise NormsTargetUnsupported([
                f"{where}: a quantifier ({expr.quantifier} {expr.variable} in ...) is not "
                f"supported"])
        raise NormsTargetUnsupported([f"{where}: {type(expr).__name__} is not supported"])


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def check_contract_id(contract_id: str) -> None:
    """The id of a contract names the generated package or contract; the
    input check of `cadl codegen` (codegen/safety.py) already requires an
    identifier, and the generators check it again."""
    from .safety import UnsafeCodegenInput
    if not _IDENTIFIER.match(contract_id or ""):
        raise UnsafeCodegenInput(f"contract id {contract_id!r}: must be an identifier")


def duration_seconds(expr: DurationLiteral) -> float:
    from ..norms_eval import _UNIT_SECONDS
    return expr.value * _UNIT_SECONDS.get(expr.unit, 1.0)


def _pattern(ref: ActorRef, ranges: dict, where: str, exprs: _Exprs) -> dict:
    """An actor of a message event, as the engine matches it."""
    if ref.index is None:
        return {"name": ref.name, "index": None}
    if ref.index == "*":
        bounds = ranges.get(id(ref))
        return {"name": ref.name, "index": {"any": True,
                                            "range": list(bounds) if bounds else None}}
    return {"name": ref.name, "index": exprs.index(ref.index, where)}


def _term(term: Any, where: str, exprs: _Exprs) -> dict | None:
    """A trigger, until, content, when or if of the engine (`_Term`)."""
    if term is None:
        return None
    out: dict = {"kind": term.kind, "text": term.text}
    if term.error:
        out["error"] = term.error
        return out
    if term.kind == "message":
        ranges = getattr(term, "_ranges", {})
        out["sender"] = _pattern(term.sender, ranges, where, exprs)
        out["receiver"] = _pattern(term.receiver, ranges, where, exprs)
        out["name"] = term.msg_name
        out["args"] = (None if term.msg_args is None
                       else [exprs.convert(a, where) for a in term.msg_args])
    elif term.kind == "predicate":
        out["expr"] = exprs.convert(term.expr, where)
    return out


def build_ir(sos: SoSDefinition, contract: ContractDef) -> dict:
    """The monitor description of one contract. Raises
    `NormsTargetUnsupported` with every reason that keeps the contract
    from being monitored; an unsupported guarantee item is not a reason
    (it is marked ``"unsupported"`` and left out by the generators)."""
    from ..norms_eval import Trace, _Engine, declared_actors

    actors = declared_actors(sos)
    eng = _Engine(contract, Trace(observations=[]), None, actors)
    exprs = _Exprs(contract.id)
    reasons: list[str] = []
    ids = [n.d.id for n in eng.norms]
    by_id = {}
    for i, n in enumerate(eng.norms):
        by_id.setdefault(n.d.id, i)

    norms = []
    for x in eng.norms:
        d = x.d
        where = f"norm '{d.id}'"
        entry: dict = {
            "id": d.id, "kind": d.kind, "frozen": x.frozen,
            "deadline": None if x.deadline is None else _number(x.deadline, where),
            "target": d.id in eng.targets,
        }
        try:
            if x.frozen is None:
                entry["trigger"] = _term(x.trigger, f"{where}, trigger", exprs)
                entry["until"] = _term(x.until, f"{where}, until", exprs)
                entry["content"] = _term(x.content, f"{where}, content", exprs)
            else:
                entry["trigger"] = entry["until"] = entry["content"] = None
            ref = x.bearer_ref
            if ref is None:
                entry["bearer"] = None
            elif isinstance(ref.index, RangeExpr):
                raise NormsTargetUnsupported([f"{where}: a bearer with a range of actors"])
            elif ref.index is None:
                entry["bearer"] = {"name": ref.name, "index": None}
            else:
                entry["bearer"] = {"name": ref.name, "index": exprs.index(ref.index, where)}
        except NormsTargetUnsupported as e:
            reasons.extend(e.reasons)
            continue
        ov = d.on_violation
        entry["severity"] = ov.severity if ov else "Major"
        entry["sanction"] = ov.sanction if ov else None
        entry["reparation"] = (by_id.get(ov.reparation)
                               if ov and ov.reparation else None)
        entry["reparation_id"] = ov.reparation if ov else None
        entry["transition"] = ov.transition if ov else None
        eff = d.effects
        entry["effects"] = {k: ([by_id[name] for name in getattr(eff, k) if name in by_id]
                                if eff else []) for k in EFFECT_KINDS}
        norms.append(entry)
    if len(set(ids)) != len(ids):
        reasons.append("two norms have the same id")

    rules = []
    for rule, when, cond in eng.rules:
        where = f"counts_as rule creating '{rule.creates}'"
        error = bool(when.error or (cond is not None and cond.error))
        entry = {"creates": rule.creates, "error": error}
        try:
            entry["when"] = None if error else _term(when, where + ", when", exprs)
            entry["if"] = None if error or cond is None else _term(cond, where + ", if", exprs)
        except NormsTargetUnsupported as e:
            reasons.extend(e.reasons)
            continue
        rules.append(entry)

    items: dict = {}
    for key in ("assume", "guarantee"):
        items[key] = []
        for pos, item in enumerate(eng.items[key], start=1):
            where = f"{key} item {pos}"
            entry = {"text": item["text"], "temporal": item["temporal"],
                     "error": item["error"], "expr": None}
            if not item["error"]:
                try:
                    entry["expr"] = exprs.convert(item["expr"], where)
                except NormsTargetUnsupported as e:
                    if key == "assume":
                        reasons.extend(e.reasons)
                    else:
                        entry["unsupported"] = "; ".join(e.reasons)
            items[key].append(entry)

    if reasons:
        raise NormsTargetUnsupported(reasons)
    lc = contract.lifecycle
    return {
        "contract": contract.id,
        "actors": {name: [decl[0], decl[1], decl[2]] for name, decl in actors.items()},
        "lifecycle": None if lc is None else {
            "states": list(lc.states), "initial": lc.initial, "terminal": list(lc.terminal)},
        "events": sorted(eng.events),
        "exprs": exprs.nodes,
        "norms": norms,
        "rules": rules,
        "items": items,
    }


def unsupported_items(ir: dict) -> list[tuple[str, int, str, str]]:
    """The (key, position from 1, text, reason) of the items a generator
    leaves out."""
    return [(key, pos, item["text"], item["unsupported"])
            for key in ("assume", "guarantee")
            for pos, item in enumerate(ir["items"][key], start=1)
            if item.get("unsupported")]
