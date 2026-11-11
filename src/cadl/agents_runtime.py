"""Reference runtime of the agent governance extension (Appendix G, agents 0.1).

`run` replays a recorded sequence of messages through one logical
enforcement point and computes what G.5 to G.8 of the specification
define on top of the stateless decision function:

- the decision for every message (:func:`cadl.agents.decide`), on the
  state left by the earlier observations: the in-force flags of the norms
  come from the verdict engine of the norms extension
  (:mod:`cadl.norms_eval`), the valid derived instances from the grants
  and revocations seen so far (G.7);
- holds (G.6): an `escalate` creates a hold; an `approval` or a
  `rejection` of the approver, the timeout, the end of a contract
  instance or an amendment closes it; an approved message is decided
  again at the time of the approval and released when nothing denies it;
- the effect of a decision on the trace (G.5): only an allowed or a
  released message is an observation of the norms engine, and only for
  the instances of the contracts that govern it;
- the audit records (G.8).

The runtime is deterministic: it does not run agents or tools, and its
only clock is the `t` of the trace. `run` replays a whole trace offline;
`AgentsRuntime.open` gives the same engine for online use, one line at a
time (`observe`, `close`), as `cadl mcp-gateway` drives it.

Trace format: the JSON Lines of `cadl norms-eval` (keys ``t``,
``bindings``, ``request``, ``values``, ``messages``, ``state``, ``amend``,
``end``; see :mod:`cadl.norms_eval`) with two more keys:

``epoch``      first line only: the time of ``t = 0`` as an ISO 8601 time
               stamp. With it the times of the audit records and ``now``
               are time stamps; without it they are the numbers of ``t``.
``quantities`` first line only: the names that are quantities, as a list.
               With it exactly these names are quantities from the start;
               without it a name is a quantity from the first line on that
               gives it a value, and a symbolic value before (F.5).
``host_instances`` first line only: the ids of the contracts whose
               instances a host creates, when the first instance comes on a
               later line. Whether a contract has host-created instances is
               fixed on the first line; see ``instances``.
``instances``  host-created contract instances. Each entry has
               ``contract`` and ``instance`` and may have ``bindings``,
               ``request``, ``state`` and ``"end": true``. An entry that
               names a new instance creates it at this line. A contract
               that the first line neither names here with an instance other
               than ``default`` nor lists under ``host_instances`` has the
               single instance ``default`` from the first line; a later
               entry that creates an instance of it is a trace error.

One line is processed in this order: holds whose deadline lies before
``t`` expire; instances are created; the time and the ``values`` of the
line are observed (one observation without message, which carries an
``amend``); the messages are decided one at a time, and each allowed
message is one observation; a ``state`` change and an ``end`` are a last
observation without message. Messages after an ``end`` are still decided.

Readings where the specification leaves a choice are listed in the README
under ``cadl agents-run``.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import agents as G
from .ast_nodes import ActorRef, SoSDefinition
from .norms_eval import (
    _EPS,
    UNKNOWN,
    NormsEvalError,
    Observation,
    Trace,
    TraceError,
    _Engine,
    _TraceReader,
    _fmt_t,
    _table,
    _Term,
    declared_actors,
    load_version,
    loads_strict,
    parse_trace,
)

DEFAULT_INSTANCE = "default"
HOLD_STATES = ("held", "approved", "rejected", "expired", "cancelled")
_INSTANCE_KEYS = {"contract", "instance", "bindings", "request", "state", "end"}


# === Trace ==================================================================

@dataclass
class Line:
    """One line of the trace: the observation as `cadl norms-eval` reads
    it, and the keys that this runtime adds."""
    obs: Observation
    state: str | None = None            # `state` as written on this line
    instances: list[dict] = field(default_factory=list)
    lineno: int = 0


@dataclass
class RunTrace:
    lines: list[Line]
    bindings: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)
    epoch: datetime | None = None
    path: str | None = None
    # The quantity names of the run when the trace gives them, else None.
    quantities: list[str] | None = None
    # Contracts whose instances a host creates, from the first line.
    host_instances: list[str] | None = None


def _parse_epoch(text: Any, where: str) -> datetime:
    if not isinstance(text, str):
        raise TraceError(f"{where}: 'epoch' must be an ISO 8601 time stamp")
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise TraceError(f"{where}: 'epoch' must be an ISO 8601 time stamp, "
                         f"got {text!r}") from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _check_bindings(value: Any, where: str) -> dict:
    if not isinstance(value, dict) or any(
            isinstance(v, bool) or not isinstance(v, int) for v in value.values()):
        raise TraceError(f"{where}: 'bindings' must map index names to integers")
    return value


def _check_instances(value: Any, where: str) -> list[dict]:
    if not isinstance(value, list):
        raise TraceError(f"{where}: 'instances' must be a list")
    out = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - _INSTANCE_KEYS:
            raise TraceError(f"{where}: an entry of 'instances' is an object with contract, "
                             f"instance and optional bindings, request, state, end")
        for key in ("contract", "instance"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise TraceError(f"{where}: an entry of 'instances' needs a string '{key}'")
        if "bindings" in entry:
            _check_bindings(entry["bindings"], f"{where}: instances")
        if "request" in entry and not isinstance(entry["request"], dict):
            raise TraceError(f"{where}: 'request' of an instance must be an object")
        if "state" in entry and not isinstance(entry["state"], str):
            raise TraceError(f"{where}: 'state' of an instance must be a string")
        if "end" in entry and entry["end"] is not True:
            raise TraceError(f"{where}: 'end' of an instance must be true")
        out.append(dict(entry, contract=entry["contract"].strip(),
                        instance=entry["instance"].strip()))
    return out


def _first_line_keys(rec: dict, where: str, first: bool) -> tuple:
    """Take the keys of the first line (`epoch`, `quantities`,
    `host_instances`) and `instances` out of one record and check them.
    Returns (epoch, quantities, host_instances, entries, state); the first
    three are None when the line does not give them."""
    epoch = quantities = hosted = None
    if "epoch" in rec:
        if not first:
            raise TraceError(f"{where}: 'epoch' is allowed only on the first line")
        epoch = _parse_epoch(rec.pop("epoch"), where)
    if "quantities" in rec:
        if not first:
            raise TraceError(f"{where}: 'quantities' is allowed only on the first line")
        quantities = rec.pop("quantities")
        if not isinstance(quantities, list) or any(
                not isinstance(q, str) or not q.strip() for q in quantities):
            raise TraceError(f"{where}: 'quantities' must be a list of names")
        quantities = [q.strip() for q in quantities]
    if "host_instances" in rec:
        if not first:
            raise TraceError(f"{where}: 'host_instances' is allowed only on the "
                             f"first line")
        hosted = rec.pop("host_instances")
        if not isinstance(hosted, list) or any(
                not isinstance(c, str) or not c.strip() for c in hosted):
            raise TraceError(f"{where}: 'host_instances' must be a list of contract ids")
        hosted = [c.strip() for c in hosted]
    entries = _check_instances(rec.pop("instances", []), where)
    if "bindings" in rec and isinstance(rec["bindings"], dict):
        _check_bindings(rec["bindings"], where)
    state = rec.get("state") if isinstance(rec.get("state"), str) else None
    return epoch, quantities, hosted, entries, state


def _check_epoch_range(epoch: datetime | None, t: float, lineno: int) -> None:
    # Only with an epoch does a time have to be an instant that an
    # ISO 8601 time stamp can express.
    if epoch is None:
        return
    try:
        epoch + timedelta(seconds=t)
    except OverflowError:
        raise TraceError(f"line {lineno}: t={_fmt_t(t)} seconds after 'epoch' "
                         f"is not a time that an ISO 8601 time stamp can "
                         f"express") from None


def parse_run_trace(lines, path: str | None = None) -> RunTrace:
    """Read a trace from an iterable of JSON Lines. The keys of
    `cadl norms-eval` are read by its own parser."""
    plain: list[str] = []
    extras: list[tuple[str | None, list[dict]]] = []
    epoch = None
    quantities = None
    hosted = None
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            plain.append("")
            continue
        where = f"line {lineno}"
        try:
            rec = loads_strict(line)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            plain.append(line)              # reported by parse_trace
            continue
        e, q, h, entries, state = _first_line_keys(rec, where, not extras)
        epoch = e if e is not None else epoch
        quantities = q if q is not None else quantities
        hosted = h if h is not None else hosted
        extras.append((state, entries, lineno))
        plain.append(json.dumps(rec))
    base = parse_trace(plain, path=path)
    for o, (_, _, lineno) in zip(base.observations, extras):
        _check_epoch_range(epoch, o.t, lineno)
    return RunTrace(
        lines=[Line(obs=o, state=s, instances=e, lineno=n)
               for o, (s, e, n) in zip(base.observations, extras)],
        bindings=base.bindings, request=base.request, epoch=epoch, path=path,
        quantities=quantities, host_instances=hosted)


class RunTraceReader:
    """Reads a trace one line at a time, for the online runtime. A line is
    a dict with the keys of a trace line; it is checked as
    `parse_run_trace` checks it, and nothing changes when it is
    malformed, except that a time outside the range of the `epoch` is
    found after the line was read (the reader is then not used again).
    The lines read so far are `trace.lines`."""

    def __init__(self, path: str | None = None) -> None:
        self.trace = RunTrace(lines=[], path=path)
        self._base = Trace(observations=[], path=path)
        self._reader = _TraceReader(self._base)
        self._lineno = 0

    def add(self, rec: Any) -> Line:
        lineno = self._lineno + 1
        where = f"line {lineno}"
        if not isinstance(rec, dict):
            raise TraceError(f"{where}: an observation must be a JSON object")
        try:
            # The line as `parse_run_trace` would read it from a file.
            rec = loads_strict(json.dumps(rec, allow_nan=False))
        except ValueError as e:
            raise TraceError(f"{where}: not JSON ({e})") from None
        first = not self.trace.lines
        epoch, quantities, hosted, entries, state = _first_line_keys(rec, where, first)
        obs = self._reader.add(rec, where)
        self._lineno = lineno
        trace = self.trace
        if first:
            trace.epoch, trace.quantities, trace.host_instances = epoch, quantities, hosted
            trace.bindings, trace.request = self._base.bindings, self._base.request
        _check_epoch_range(trace.epoch, obs.t, lineno)
        line = Line(obs=obs, state=state, instances=entries, lineno=lineno)
        trace.lines.append(line)
        return line


def load_run_trace(path) -> RunTrace:
    with open(path, encoding="utf-8") as f:
        return parse_run_trace(f, path=str(path))


# === State ==================================================================

@dataclass
class _Derived:
    """A derived instance of a delegable permission (G.7)."""
    norm: str
    holder: str
    chain: list[str]
    granted_at: float
    state: str = "active"               # active | discharged
    ended_at: float | None = None
    end_reason: str | None = None


@dataclass
class _Hold:
    """A held message (G.6)."""
    hold_id: str
    index: int
    message: dict
    time: float
    approver: str
    deadline: float | None
    contracts: list[str]
    norms: list[str]
    fallback: list[str]
    notify: list[str]
    # The grounds: (contract id, norm id, instance), one entry per contract
    # instance in which a prohibition applied when the hold was created.
    grounds: list[tuple[str, str, str]]
    instances: list[tuple[str, str]]    # (contract id, instance) of the grounds
    state: str = "held"
    closed_at: float | None = None
    released: bool = False              # the message took effect


class _Instance:
    """A running or ended instance of an enforced contract."""

    def __init__(self, contract: str, name: str, bindings: dict, request: dict,
                 started: float) -> None:
        self.contract = contract
        self.name = name
        self.bindings = bindings
        self.request = request
        self.started = started
        self.ended: float | None = None
        self.running = True
        self.state: str | None = None
        self.engine: _BoundaryEngine = None       # type: ignore[assignment]
        self.derived: dict[str, list[_Derived]] = {}
        self.until_terms: dict[str, _Term] = {}

    @property
    def key(self) -> tuple[str, str]:
        return self.contract, self.name


class _BoundaryTerm(_Term):
    """A message event of an enforced contract. Where the contract
    instance does not bind an index of the sender or the receiver, the
    occurrence is unknown (G.4), and `unknown_as` says how the use of the
    event resolves it fail-closed (G.5): an unknown occurrence does not
    activate a permission, end a prohibition or create an institutional
    event; it does activate a prohibition and end a permission."""

    unknown_as = True

    def _unbound(self, ref: ActorRef) -> str | None:
        index = ref.index
        if isinstance(index, ActorRef) and index.index is None:
            index = index.name
        if isinstance(index, str) and index != "*" and not index.isdigit() \
                and index not in self.ev.bindings:
            return index
        return None

    def _actor_matches(self, ref: ActorRef, actual: str, k: int) -> bool:
        matched = super()._actor_matches(ref, actual, k)
        name = self._unbound(ref)
        if matched and name is not None:
            self.ev.warn(
                f"the index '{name}' of {ref.name}[{name}] in '{self.text}' is not bound: "
                f"its occurrence is unknown and is resolved fail-closed (G.5), here as "
                f"{'occurring' if self.unknown_as else 'not occurring'}")
            return self.unknown_as
        return matched

    def unbound_in_predicate(self) -> list[str]:
        """The index variables of a predicate that neither the contract
        instance nor a quantifier of the predicate binds."""
        if self.kind != "predicate" or self.expr is None:
            return []
        return sorted(G.predicate_index_vars(self.expr) - set(self.ev.bindings))

    def unbound_in_arguments(self) -> list[str]:
        """The free index variables in the arguments of a message event
        that the contract instance does not bind."""
        if self.kind != "message" or not self.msg_args:
            return []
        names = {v for a in self.msg_args for v in G.predicate_index_vars(a)}
        return sorted(names - set(self.ev.bindings))

    def matching_messages(self, k: int) -> list[dict]:
        names = self.unbound_in_arguments()
        if not names:
            return super().matching_messages(k)
        # Whether the arguments are those of the event is unknown: the
        # messages that match in sender, receiver and name occur, or none.
        args, self.msg_args = self.msg_args, None
        try:
            candidates = super().matching_messages(k)
        finally:
            self.msg_args = args
        if candidates:
            self.ev.warn(
                f"the index '{names[0]}' in the arguments of '{self.text}' is not bound: "
                f"its occurrence is unknown and is resolved fail-closed (G.5), here as "
                f"{'occurring' if self.unknown_as else 'not occurring'}")
        return candidates if self.unknown_as else []

    def _unknown_predicate(self) -> bool:
        names = self.unbound_in_predicate()
        if names:
            self.ev.warn(
                f"the index '{names[0]}' in the predicate '{self.text}' is not bound: "
                f"whether it holds is unknown and is resolved fail-closed (G.5), here as "
                f"{'occurring' if self.unknown_as else 'not occurring'}")
        return bool(names)

    def occurs(self, k: int, created: list[dict]) -> bool:
        if self._unknown_predicate():
            return self.unknown_as
        return super().occurs(k, created)

    def content(self, k: int, n: int, created: list[dict]) -> Any:
        if self._unknown_predicate():
            return self.unknown_as
        return super().content(k, n, created)


def _resolve_unknown(term, value: bool) -> None:
    if term is not None and term.kind in ("message", "predicate"):
        term.__class__ = _BoundaryTerm
        term.unknown_as = value


class _BoundaryEngine(_Engine):
    """The verdict engine of the norms extension, driven one observation
    at a time, with the additions of Appendix G to F.5: the in-force flag
    of a prohibition after its violation (G.5), the verdict `excused` for
    an obligation whose content the contract itself denied or held (G.5),
    and the point after step 1 at which holds, grants and revocations are
    applied (G.6)."""

    def __init__(self, contract, trace: Trace, loader, owner: "AgentsRuntime",
                 inst: _Instance) -> None:
        super().__init__(contract, trace, loader)
        self.owner = owner
        self.inst = inst
        self.created: list[dict] = []
        # Violated prohibitions that are still in force at the boundary.
        self.kept: dict[str, dict] = {}
        # Norm id -> (key, predicate) while the flag is unknown for missing data.
        self.pending: dict[str, tuple] = {}
        self._mark = 0
        self._harden()

    def _harden(self) -> None:
        """Resolve unknown occurrences fail-closed (see `_BoundaryTerm`):
        an unknown occurrence never has an effect that lets a message
        through and always has the effects that block. A permission is not
        activated and is ended; a prohibition is activated and not ended;
        no institutional event is created; a power is not activated, is
        ended, and is not exercised; an obligation is activated, not ended
        and not fulfilled."""
        for _, when, cond in self.rules:
            _resolve_unknown(when, False)
            if cond is not None and not cond.error and cond.kind == "predicate" \
                    and G.predicate_index_vars(cond.expr) - set(self.ev.bindings):
                # An `if:` that depends on an unbound index: no event.
                cond.error = "it depends on an index that the instance does not bind"
                self.ev.warn(f"the if: '{cond.text}' of a counts_as rule depends on an "
                             f"index that is not bound: the rule creates no event (G.5)")
        for norm in self.norms:
            kind = norm.d.kind
            if kind in ("permission", "power"):
                _resolve_unknown(norm.trigger, False)
                _resolve_unknown(norm.until, True)
            else:                       # prohibition, obligation
                _resolve_unknown(norm.trigger, True)
                _resolve_unknown(norm.until, False)
            if kind in ("power", "obligation"):
                _resolve_unknown(norm.content, False)

    def _is_bearer(self, norm, performer, n: int) -> bool:
        """The performer of a power: when the bearer has an index that the
        instance does not bind, who the bearer is is unknown, and nobody
        exercises the power (F.3 would take any member)."""
        if norm.bearer_ref is not None and performer is not None \
                and not self.ev.actor(norm.bearer_ref, n)[1]:
            return False
        return super()._is_bearer(norm, performer, n)

    def push(self, o: Observation) -> bool:
        """Append one observation and process it; True when the contract
        instance ended at it."""
        n = len(self.obs)
        self.obs.append(o)
        if self.lifecycle is not None:
            self._fill_states(n)
        self.created = []
        self._mark = len(self.timeline)
        ended = self._step(n, False)
        self._after(n)
        return ended

    # --- hooks ----------------------------------------------------------------

    def _counts_as(self, n: int) -> list[dict]:
        # A rule with a message `when:` and an `if:`: the message is a
        # one-off. When it arrives while the `if:` has no value, no event
        # is created and none will be: there is no flag to hold unknown.
        # Say so when the event would bring a prohibition into force or
        # end a permission.
        for rule, when, cond in self.rules:
            if cond is None or cond.error or when.error or when.kind != "message":
                continue
            if rule.creates not in self.blocking_events():
                continue
            if when.matching_messages(n) and self.undefined(cond, n):
                names = ", ".join(self.missing(cond, n)) or "a quantity it reads"
                self.ev.warn(
                    f"the message '{when.text}' arrived while the if: '{cond.text}' of "
                    f"the counts_as rule that creates '{rule.creates}' had no value "
                    f"({names} has no value yet): the event is not created, and the "
                    f"norms that it would block are not affected (a known limit, G.13)")
        self.created = super()._counts_as(n)
        return self.created

    def blocking_events(self) -> set[str]:
        """The institutional events that are the trigger of a prohibition
        or the `until:` of a permission."""
        out = set()
        for norm in self.norms:
            term = norm.trigger if norm.d.kind == "prohibition" else (
                norm.until if norm.d.kind == "permission" else None)
            if term is not None and term.kind == "event":
                out.add(term.text)
        return out

    def _powers(self, n: int, created: list[dict]) -> None:
        self.owner._after_step1(self.inst, n, created)        # G.6, "after step 1"
        super()._powers(n, created)

    def _set(self, norm, n: int, state: str, reason: str, log: bool = True) -> None:
        t = self.obs[n].t
        excused = False
        if state == "violated" and norm.d.kind == "prohibition":
            remaining = None
            if norm.deadline is not None:
                remaining = max(0.0, norm.deadline - norm.elapsed(t))
            self.kept[norm.d.id] = {"on": True, "suspended": False, "since": t,
                                    "remaining": remaining, "from": n}
        elif state == "violated" and norm.d.kind == "obligation":
            due = t
            if norm.deadline is not None and norm.elapsed(t) > norm.deadline:
                due = t - (norm.elapsed(t) - norm.deadline)
            excused = self.owner._blocked(self.inst, norm, due)
        super()._set(norm, n, state, reason, log)
        if excused and norm.verdict == "violated":
            norm.verdict = "excused"
            norm.reason += "; excused: the contract denied or held the content (G.5)"

    def _amend(self, n: int, name: str) -> None:
        old = {nid: self.by_id[nid].d for nid in self.kept}
        super()._amend(n, name)
        self._harden()
        new = {d.id: d for d in self.contract.norms}
        for nid, k in self.kept.items():
            nd = new.get(nid)
            if nd is None or nd.kind != "prohibition" or old[nid].on_amendment != "carry_over":
                k["on"] = False                 # not carried over (F.6)

    def _after(self, n: int) -> None:
        """Keep the in-force flag of the violated prohibitions (G.5)."""
        t = self.obs[n].t
        for nid, k in self.kept.items():
            if not k["on"]:
                continue
            if n > k["from"]:
                for e in self.timeline[self._mark:]:
                    power = self.by_id.get(e.get("norm")) if e["kind"] == "power" else None
                    eff = power.d.effects if power is not None else None
                    if eff is None:
                        continue
                    if nid in eff.suspend and not k["suspended"]:
                        k["suspended"] = True
                        if k["remaining"] is not None:
                            k["remaining"] -= t - k["since"]
                    if nid in eff.resume and k["suspended"]:
                        k["suspended"] = False
                        k["since"] = t
                    if nid in eff.discharge:
                        k["on"] = False
                if (k["on"] and not k["suspended"] and k["remaining"] is not None
                        and t - k["since"] > k["remaining"] + _EPS):
                    k["on"] = False             # the window would have closed
            norm = self.by_id.get(nid)
            if (k["on"] and norm is not None and norm.until is not None
                    and norm.until.occurs(n, self.created)):
                k["on"] = False

    def sources(self, term) -> list:
        """The predicates on which the occurrence of a trigger or an
        `until:` depends: the term itself when it is a predicate; when it
        is an institutional event, the `when:` (and `if:`) of every
        `counts_as:` rule that creates the event from a predicate. A rule
        with a message `when:` is a one-off and is not a source."""
        if term is None or term.error:
            return []
        if term.kind == "predicate":
            return [term]
        if term.kind != "event":
            return []
        out = []
        for rule, when, cond in self.rules:
            if rule.creates == term.text and when.kind == "predicate" and not when.error:
                out.append(when)
                if cond is not None and not cond.error:
                    out.append(cond)
        return out

    def undefined_sources(self, term, n: int) -> list:
        return [t for t in self.sources(term) if self.undefined(t, n)]

    def undefined(self, term, n: int) -> bool:
        """Whether a predicate has no value at observation n because a
        quantity it reads has none (F.5: undefined)."""
        if term is None or term.kind != "predicate" or term.error or n < 0:
            return False
        if isinstance(term, _BoundaryTerm) and term.unbound_in_predicate():
            return False                # unknown by an unbound index: resolved there
        return self.ev.holds(term.expr, n, n) is UNKNOWN

    def missing(self, term, n: int) -> list[str]:
        """The quantities that a predicate reads and that have no value at
        observation n."""
        import dataclasses
        from .ast_nodes import Comprehension, MemberAccess, QuantifiedExpr
        values, out = self.obs[n].values, []

        def domain(dom: Any) -> Any:
            # an open domain: the members that the trace has not named yet
            d = self.ev.domain(dom, n)
            if d is not UNKNOWN and not d[2]:
                out.append(f"a member of {d[0]} not yet named" if d[0] is not None
                           else "the bound of a range")
            return d

        def walk(node: Any, bound: dict) -> None:
            if isinstance(node, (QuantifiedExpr, Comprehension)):
                d = domain(node.domain)
                inner = node.predicate if isinstance(node, QuantifiedExpr) else node.element
                walk(inner, {**bound, node.variable: d})
            elif isinstance(node, MemberAccess) and isinstance(node.obj, ActorRef):
                if node.obj.name == "request" and node.obj.index is None:
                    return
                index = node.obj.index
                if isinstance(index, ActorRef) and index.index is None:
                    index = index.name
                var = node.obj.name if node.obj.index is None and node.obj.name in bound \
                    else index if isinstance(index, str) and index in bound else None
                if var is not None:
                    # a quantity of each member of a closed domain: name the
                    # keys without a value (an open domain: see domain())
                    d = bound[var]
                    if d is UNKNOWN or not d[2]:
                        return
                    group = d[0] if var == node.obj.name else node.obj.name
                    for m in d[1]:
                        key = f"{group}[{m}].{node.member}"
                        if values.get(key) is None and \
                                values.get(f"{group}.{node.member}") is None:
                            out.append(key)
                    return
                if node.obj.name in bound or self._scoped(index, bound):
                    return              # an index expression with a variable
                key = f"{self.ev.actor(node.obj, n)[0]}.{node.member}"
                plain = f"{node.obj.name}.{node.member}"
                if values.get(key) is None and values.get(plain) is None:
                    out.append(key)
            elif isinstance(node, ActorRef):
                if node.index is None and node.name in self.ev.known \
                        and node.name not in self.ev.bindings and node.name not in bound \
                        and values.get(node.name) is None:
                    out.append(node.name)
            elif dataclasses.is_dataclass(node) and not isinstance(node, type):
                for f in dataclasses.fields(node):
                    if f.name != "loc":
                        walk(getattr(node, f.name), bound)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item, bound)

        walk(term.expr, {})
        return list(dict.fromkeys(out))

    @staticmethod
    def _scoped(index: Any, bound: dict) -> bool:
        """Whether an index expression uses a variable of `bound`."""
        import dataclasses
        if isinstance(index, str):
            return index in bound
        if isinstance(index, ActorRef) and index.index is None:
            return index.name in bound
        if dataclasses.is_dataclass(index) and not isinstance(index, type):
            return any(_BoundaryEngine._scoped(getattr(index, f.name), bound)
                       for f in dataclasses.fields(index) if f.name != "loc")
        return False

    def in_force(self, nid: str):
        """The in-force flag of G.5: True, False, or None for unknown."""
        norm = self.by_id.get(nid)
        if norm is None:
            return False
        if norm.frozen:
            return None
        # Missing data: while the predicate that would bring a prohibition
        # into force, or end a permission, has no value, the enforcement
        # point cannot determine the flag. Nothing is latched: the state of
        # the norm is not changed.
        kind, last = norm.d.kind, len(self.obs) - 1
        key, term = None, None
        if kind == "prohibition" and norm.state == "inactive":
            key, term = "trigger", norm.trigger
        elif kind == "permission" and norm.state == "active":
            key, term = "until", norm.until
        open_ = self.undefined_sources(term, last) if term is not None else []
        if open_:
            text = f"'{term.text}'" if term.kind == "predicate" else (
                f"'{term.text}' (an event that the counts_as rule with "
                f"'{open_[0].text}' creates)")
            names = [q for t in open_ for q in self.missing(t, last)]
            self.pending[nid] = (key, text, list(dict.fromkeys(names)))
            return None
        if norm.state == "active":
            return True
        if norm.d.kind == "prohibition" and norm.state == "violated":
            k = self.kept.get(nid)
            return bool(k and k["on"] and not k["suspended"])
        return False


def _plain(obj: Any) -> Any:
    """A definition without source locations, for comparison."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return (type(obj).__name__,) + tuple(
            (f.name, _plain(getattr(obj, f.name)))
            for f in dataclasses.fields(obj) if f.name != "loc")
    if isinstance(obj, (list, tuple)):
        return tuple(_plain(x) for x in obj)
    if isinstance(obj, dict):
        return tuple((k, _plain(v)) for k, v in obj.items())
    return obj


# === The runtime =============================================================

class AgentsRuntime:
    """One logical enforcement point over the enforced contracts of a file."""

    def __init__(self, sos: SoSDefinition, trace: RunTrace | None = None,
                 versions: dict | None = None, check: bool = True,
                 path: str | None = None) -> None:
        if not G.agents_active(sos):
            raise NormsEvalError(
                "the file does not declare the agent governance extension: add "
                "'norms: 0.1' and 'agents: 0.1' under extensions: (Appendix G)")
        # `check=False` is unsafe and meant for tests of the fail-closed
        # resolution: it runs a file that `cadl check` rejects and creates
        # instances that do not bind the indices of their parties.
        self.check = check
        if check:
            _require_valid(sos, "the file")
        self.sos = sos
        self.policy = G.build_policy(sos)
        # Offline, `trace` holds every line; online (`open`), the reader
        # fills it one line at a time and `path` is where versions named by
        # `amend` are looked up.
        self._reader: RunTraceReader | None = None
        if trace is None:
            self._reader = RunTraceReader(path)
            trace = self._reader.trace
        self.trace = trace
        self._started = False
        self._last_t = 0.0
        self._closed = False
        self._failed: str | None = None
        self.versions = versions or {}
        self.instances: list[_Instance] = []
        self.holds: list[_Hold] = []
        self.audit: list[dict] = []
        self.steps: list[dict] = []
        self.releases: list[dict] = []
        self.warnings: list[str] = []
        self.amendments: list[dict] = []
        # Denied and held messages, for the verdict `excused` (G.5).
        self.blocks: list[dict] = []
        self._events: list[tuple] = []
        self._current: dict | None = None
        self._snapshot: dict[str, Any] = {}
        self._lineno = 0
        self._checked: set[str] = set()
        self._warned_missing: set[tuple] = set()
        # Names that predicates of the enforced contracts use as values,
        # and the names that have had a value so far: for the warning about
        # a name whose first value comes after a decision.
        self._bare: set[str] = set()
        for c in sos.contracts:
            if c.enforcement is not None:
                for _, expr, _ in G.contract_expressions(c):
                    self._bare |= G.bare_names(expr)
        self._bare -= set(self.policy["actors"]) | {"now", "state", "request"}
        self._valued: set[str] = set()
        self.host_managed: set[str] = set()
        if trace.lines:
            self._begin(trace.lines[0])

    def _begin(self, head: Line) -> None:
        """Which contracts have host-created instances is known at the first
        line and not later: a contract that the first line names in
        `instances` with another instance than `default`, or lists under
        `host_instances`. Every other enforced contract has the instance
        `default` from the start. Nothing here reads a later line."""
        ids = [c["id"] for c in self.policy["contracts"]]
        self.host_managed = {e["contract"] for e in head.instances
                             if e["instance"] != DEFAULT_INSTANCE}
        for cid in self.trace.host_instances or []:
            if cid not in ids:
                raise TraceError(f"line {head.lineno}: 'host_instances' names '{cid}', "
                                 f"which is not an enforced contract of the file")
            self.host_managed.add(cid)

    # --- small helpers --------------------------------------------------------

    def _contract(self, cid: str) -> dict:
        return next(c for c in self.policy["contracts"] if c["id"] == cid)

    def _ast(self, cid: str):
        return next(c for c in self.sos.contracts if c.id == cid)

    def _time(self, t: float) -> Any:
        """A time of the trace as the audit log writes it."""
        if self.trace.epoch is None:
            return int(t) if float(t).is_integer() else t
        try:
            at = (self.trace.epoch + timedelta(seconds=t)).astimezone(timezone.utc)
        except OverflowError:
            raise TraceError(
                f"line {self._lineno}: t={_fmt_t(t)} seconds after 'epoch' (a time of "
                f"the trace or the deadline of a hold) is not a time that an ISO 8601 "
                f"time stamp can express") from None
        text = at.strftime("%Y-%m-%dT%H:%M:%S")
        if at.microsecond:
            text += f".{at.microsecond:06d}".rstrip("0")
        return text + "Z"

    def _running(self, cid: str | None = None) -> list[_Instance]:
        return [i for i in self.instances if i.running and cid in (None, i.contract)]

    @staticmethod
    def _actors(m: dict) -> tuple[dict | None, dict | None]:
        return G.parse_concrete_actor(m["from"]), G.parse_concrete_actor(m["to"])

    @staticmethod
    def _head(m: dict | None) -> dict | None:
        return None if m is None else {"from": m["from"], "to": m["to"], "name": m["name"]}

    def _governing_instances(self, m: dict | None, contracts=None) -> list[dict]:
        """`instances` of an audit record: the running instances of the
        governing contracts (or of the given contracts)."""
        out = []
        sender, receiver = self._actors(m) if m else (None, None)
        for c in self.policy["contracts"]:
            if contracts is not None:
                if c["id"] not in contracts:
                    continue
            elif not (sender and receiver
                      and G.governs(self.policy, c, sender, receiver, m["name"])):
                continue
            out += [{"contract": i.contract, "instance": i.name}
                    for i in self._running(c["id"])]
        return out

    # --- audit records (G.8) ----------------------------------------------------

    def _record(self, t: float, m: dict, result: dict, hold: _Hold | None = None,
                outcome: str | None = None, closing: bool = False) -> dict:
        """A decision record. A closing record lists the contract instances
        of the grounds of its hold, running or not."""
        hold_id = None if hold is None else hold.hold_id
        if closing:
            instances = [{"contract": c, "instance": i} for c, i in hold.instances]
        else:
            instances = self._governing_instances(m)
        rec = {"time": self._time(t), "message": self._head(m), "instances": instances}
        rec.update(result)
        rec["hold_id"] = hold_id
        rec["outcome"] = outcome
        self.audit.append(rec)
        return rec

    def _event(self, t: float, event: str, m: dict | None, instances: list[dict],
               norm: str | None = None, chain: list[str] | None = None,
               holders=(), hold_id: str | None = None, reason: str | None = None) -> None:
        self.audit.append({
            "time": self._time(t), "event": event, "message": self._head(m),
            "instances": instances, "norm": norm,
            "chain": None if chain is None else list(chain),
            "holders": list(holders), "hold_id": hold_id, "reason": reason,
        })

    # --- instances --------------------------------------------------------------

    def _require_bindings(self, cid: str, name: str, bindings: dict) -> None:
        """A contract instance binds every index variable of its parties."""
        if not self.check:
            return
        party_vars = set()
        for ref in self._contract(cid)["parties"]:
            if ref["kind"] != "var":
                continue
            var = ref["var"]
            party_vars.add(var)
            if var not in bindings:
                raise TraceError(
                    f"line {self._lineno}: instance {name} of {cid} has no binding for "
                    f"the index '{var}' of its party {ref['text']}; an instance "
                    f"of an enforced contract binds every index of its parties "
                    f"('bindings')")
            value = bindings[var]
            # Every declared set that the variable indexes in the contract:
            # the party's own, and any other (`SUP[i]` beside a party `W[i]`).
            sets = [ref["name"]] + sorted(
                G.contract_index_sets(self._ast(cid)).get(var, set()) - {ref["name"]})
            for set_name in sets:
                member = {"name": set_name, "index": value, "text": f"{set_name}[{value}]"}
                if isinstance(value, bool) or not isinstance(value, int) \
                        or not G.is_member(self.policy, set_name, member):
                    raise TraceError(
                        f"line {self._lineno}: instance {name} of {cid} binds the index "
                        f"'{var}' of its party {ref['text']} to {value!r}, which is not "
                        f"a member of the declared set {set_name}")
        for var in bindings:
            if var not in party_vars:
                text = (f"{cid}/{name}: the binding of '{var}' is ignored for the "
                        f"parties: no party of {cid} is written with [{var}]")
                if text not in self.warnings:
                    self.warnings.append(text)

    def _create(self, cid: str, name: str, bindings: dict, request: dict, t: float,
                state: str | None = None) -> _Instance:
        self._require_bindings(cid, name, bindings)
        inst = _Instance(cid, name, dict(bindings), dict(request), t)
        inst.state = state
        trace = Trace(observations=[], bindings=inst.bindings, request=inst.request,
                      path=self.trace.path)
        inst.engine = _BoundaryEngine(self._ast(cid), trace, self._load, self, inst)
        inst.engine.ev.known = self._known_names(inst)
        inst.engine.ev.actors = declared_actors(self.sos)
        self._build_until(inst)
        self.instances.append(inst)
        self._event(t, "instance_start", None, [self._where(inst)])
        return inst

    @staticmethod
    def _where(inst: _Instance) -> dict:
        return {"contract": inst.contract, "instance": inst.name}

    def _known_names(self, inst: _Instance) -> set[str]:
        """The quantity names of a new instance (F.5). The runtime decides
        every message on what was observed up to it, so without the names
        from the trace (`quantities`) a name is a quantity from the first
        observation on that gives it a value: this set grows with every
        observation of the instance (`_push`) and never looks ahead."""
        if self.trace.quantities is not None:
            return set(self.trace.quantities)
        return set(self._snapshot)

    def _build_until(self, inst: _Instance) -> None:
        inst.until_terms = {}
        for nid, d in self._contract(inst.contract)["delegations"].items():
            if not d["until"]:
                continue
            term = _Term(d["until"], inst.engine.events, inst.engine.ev)
            _resolve_unknown(term, True)        # unknown: the derived instances end
            if term.error:
                inst.engine.ev.warn(f"until of the delegation of '{nid}' is not evaluated: "
                                    f"'{term.text}': {term.error}")
            inst.until_terms[nid] = term

    def _load(self, name: str) -> SoSDefinition:
        if name not in self.versions:
            base = Path(self.trace.path).parent if self.trace.path else Path(".")
            self.versions[name] = load_version(base / name)
        elif self.check and name not in self._checked:
            _require_valid(self.versions[name], name)
        self._checked.add(name)
        return self.versions[name]

    def _ended(self, inst: _Instance, t: float) -> None:
        """G.6, step 6: the holds with a ground in an instance that ends
        are cancelled; G.7 (c): its derived instances are discharged."""
        inst.running = False
        inst.ended = t
        terminal = (inst.engine.end_reason or "").startswith("terminal state")
        self._event(t, "instance_end", None, [self._where(inst)],
                    reason="terminal_state" if terminal else "end")
        for hold in self.holds:
            if hold.state == "held" and inst.key in hold.instances:
                self._close(hold, "cancelled", t)
        for items in inst.derived.values():
            self._end_derived(inst, items, t, "instance_end", "the contract instance ended")

    # --- observations -------------------------------------------------------------

    def _push(self, inst: _Instance, o: Observation, exempt=()) -> None:
        eng = inst.engine
        n = len(eng.obs)
        if exempt:
            eng._exempt[n] = set(exempt)
        before = len(eng.unauthorised)
        if self.trace.quantities is None:
            eng.ev.known.update(o.values)       # names valued up to now
        ended = eng.push(o)
        inst.state = eng.obs[n].state_after
        for u in eng.unauthorised[before:]:          # F.5, item 7
            self._event(o.t, "unauthorised_act", self._current,
                        [{"contract": inst.contract, "instance": inst.name}],
                        reason="no_power")
        if ended:
            self._ended(inst, o.t)
            return
        for nid, items in inst.derived.items():      # G.7, end (c)
            norm = eng.by_id.get(nid)
            if norm is None or norm.state == "discharged":
                self._end_derived(inst, items, o.t, "original_discharged",
                                  "the original permission was discharged")

    def _observe(self, m: dict | None, t: float, exempt: dict | None = None,
                 amend: dict | None = None) -> None:
        """One observation of the enforcement point. An observation
        without message (the time of a line) reaches every running
        instance; a message reaches the instances of the contracts that
        govern it (G.5). Then the holds approved at it are released."""
        self._events = []
        self._current = m
        sender, receiver = self._actors(m) if m else (None, None)
        for inst in list(self.instances):
            if not inst.running:
                continue
            if m is not None and not (sender and receiver and G.governs(
                    self.policy, self._contract(inst.contract), sender, receiver,
                    m["name"])):
                continue
            o = Observation(t=t, values=self._snapshot, messages=[m] if m else [],
                            state=inst.state, state_after=inst.state, end=None,
                            amend=(amend or {}).get(inst.contract))
            self._push(inst, o, (exempt or {}).get(inst.key, ()))
        self._current = None
        for hold in self._resolve(m, t):             # G.6, "after step 6"
            self._release(hold, t)

    def _after_step1(self, inst: _Instance, n: int, created: list[dict]) -> None:
        """G.6, "after step 1": collect the approvals and rejections of
        this observation, then apply grants, revocations and `until:`."""
        m = self._current
        t = inst.engine.obs[n].t
        for e in created:
            if e["name"] in G.HOLD_EVENTS:
                hold_id = None
                if m is not None and e["performer"] is not None and m["args"] \
                        and isinstance(m["args"][0], str):
                    hold_id = m["args"][0]          # the first argument (G.6)
                self._events.append((e["name"], hold_id, e["performer"], inst.key))
        c = self._contract(inst.contract)
        if m is not None and c["delegations"]:
            sender, receiver = self._actors(m)
            if sender and receiver and G.governs(self.policy, c, sender, receiver, m["name"]):
                for nid, d in c["delegations"].items():
                    if m["name"] == d["grant"]:
                        self._grant(inst, c, nid, d, sender, receiver, m, t)
                    elif d["revoke"] and m["name"] == d["revoke"]:
                        self._revoke(inst, c, nid, sender, receiver, m, t)
        for nid, term in inst.until_terms.items():
            if term.occurs(n, created):
                self._end_derived(inst, inst.derived.get(nid, []), t, "until",
                                  f"until occurred: {term.text}")

    # --- delegation (G.7) -----------------------------------------------------------

    @staticmethod
    def _norm(c: dict, nid: str) -> dict | None:
        return next((n for n in c["norms"] if n["id"] == nid), None)

    def _original_in_force(self, inst: _Instance, norm: dict) -> bool:
        return True if norm["static"] else inst.engine.in_force(norm["id"]) is True

    def _valid(self, inst: _Instance, c: dict, d: _Derived) -> bool:
        """G.7: the original is in force, the derived instance is active,
        the chain has at most `max_depth` links, and every actor after the
        first matches a reference in `to:`."""
        norm, deleg = self._norm(c, d.norm), c["delegations"].get(d.norm)
        if norm is None or deleg is None or d.state != "active":
            return False
        if not self._original_in_force(inst, norm):
            return False
        if len(d.chain) - 1 > deleg["max_depth"]:
            return False
        actors = [G.parse_concrete_actor(a) for a in d.chain[1:]]
        return all(a is not None and any(G.ref_match(self.policy, ref, a, {}) is True
                                         for ref in deleg["to"]) for a in actors)

    @staticmethod
    def _discharge(items: list[_Derived], t: float, reason: str) -> list[str]:
        out = []
        for d in items:
            if d.state == "active":
                d.state, d.ended_at, d.end_reason = "discharged", t, reason
                out.append(d.holder)
        return out

    def _end_derived(self, inst: _Instance, items: list[_Derived], t: float,
                     code: str, text: str) -> None:
        """End derived instances for another cause than a revoke message,
        with one event record for each."""
        for d in items:
            if d.state == "active":
                self._discharge([d], t, text)
                self._event(t, "delegation_end", self._current, [self._where(inst)],
                            norm=f"{inst.contract}.{d.norm}", chain=d.chain,
                            holders=[d.holder], reason=code)

    def _grant(self, inst, c, nid, deleg, sender, receiver, m, t) -> None:
        norm = self._norm(c, nid)
        x, y = sender["text"], receiver["text"]
        items = inst.derived.setdefault(nid, [])
        active = {d.holder: d for d in items if d.state == "active"}
        where = [{"contract": inst.contract, "instance": inst.name}]
        label = f"{c['id']}.{nid}"
        chain = None
        if G.ref_match(self.policy, norm["bearer"], sender, inst.bindings) is True:
            if self._original_in_force(inst, norm):
                chain = [x]
        elif x in active and self._valid(inst, c, active[x]):
            chain = active[x].chain
        if chain is None:
            reason = "no_authority"                                 # condition 1
        elif not any(G.ref_match(self.policy, ref, receiver, {}) is True
                     for ref in deleg["to"]):
            reason = "not_in_to"                                    # condition 2
        elif y in chain:
            reason = "in_chain"                                     # condition 3
        elif len(chain) - 1 >= deleg["max_depth"]:
            reason = "max_depth"                                    # condition 4
        else:
            reason = None
        if reason is not None:
            self._event(t, "unauthorised_grant", m, where, norm=label, reason=reason)
        elif y in active:                                           # condition 5
            self._event(t, "grant", m, where, norm=label, chain=active[y].chain,
                        reason="holder_exists")
        else:
            d = _Derived(norm=nid, holder=y, chain=chain + [y], granted_at=t)
            items.append(d)
            self._event(t, "grant", m, where, norm=label, chain=d.chain, holders=[y])

    def _revoke(self, inst, c, nid, sender, receiver, m, t) -> None:
        x, y = sender["text"], receiver["text"]
        items = inst.derived.get(nid, [])
        target = next((d for d in items if d.state == "active" and d.holder == y), None)
        where = [{"contract": inst.contract, "instance": inst.name}]
        label = f"{c['id']}.{nid}"
        if target is None or x not in target.chain[:-1]:
            self._event(t, "revoke", m, where, norm=label, reason="no_effect")
            return
        chain = list(target.chain)
        holders = self._discharge([d for d in items if y in d.chain], t,
                                  f"revoked by {x}")
        self._event(t, "revoke", m, where, norm=label, chain=chain, holders=holders)

    # --- decisions (G.5) ----------------------------------------------------------------

    def policy_input(self, m: dict, t: float) -> dict:
        """The policy input of G.10 for a message on the current state."""
        instances = []
        for c in self.policy["contracts"]:
            for inst in self._running(c["id"]):
                in_force = {}
                inst.engine.pending.clear()
                for nid in c["non_static"]:
                    flag = inst.engine.in_force(nid)
                    if flag is not None:           # a missing entry is unknown
                        in_force[nid] = flag
                for nid, (key, text, names) in inst.engine.pending.items():
                    kind = "prohibition" if key == "trigger" else "permission"
                    effect = ("the prohibition is taken as in force" if key == "trigger"
                              else "the permission does not apply")
                    warning = (
                        f"{c['id']}/{inst.name}: a message was decided while the {key}: "
                        f"{text} of the {kind} '{nid}' had no value "
                        f"({', '.join(names) or 'a quantity it reads'} has no value yet); "
                        f"the flag is unknown and {effect} (G.5). "
                        + ("Give the bound of the set a value (a binding or a quantity)"
                           if any(" not yet named" in q or q == "the bound of a range"
                                  for q in names)
                           else "Give initial values on the first line"))
                    if (inst.key, nid) not in self._warned_missing:    # once per norm
                        self._warned_missing.add((inst.key, nid))
                        self.warnings.append(warning)
                derived = [{"norm": d.norm, "holder": d.holder, "chain": list(d.chain)}
                           for nid in c["delegations"]
                           for d in inst.derived.get(nid, []) if self._valid(inst, c, d)]
                instances.append({"contract": c["id"], "instance": inst.name,
                                  "bindings": dict(inst.bindings),
                                  "in_force": in_force, "derived": derived})
        return {"message": self._head(m), "now": self._time(t), "instances": instances}

    def _message(self, m: dict, t: float) -> None:
        inp = self.policy_input(m, t)
        try:
            result = G.decide(self.policy, inp)
        except G.AgentsInputError as e:
            raise TraceError(f"t={_fmt_t(t)}: {e}") from None
        step = {"time": self._time(t), "message": dict(m), "input": inp,
                "result": result, "hold_id": None}
        self.steps.append(step)
        if result["decision"] == "allow":
            self._record(t, m, result)
            self._observe(m, t)
        elif result["decision"] == "deny":
            self._record(t, m, result)
            self.blocks.append({"t": t, "message": m, "contracts": result["contracts"],
                                "hold": None})
        else:
            hold = self._hold(m, t, inp, result)
            step["hold_id"] = hold.hold_id
            self._record(t, m, result, hold=hold)
            self.blocks.append({"t": t, "message": m, "contracts": hold.contracts,
                                "hold": hold})

    # --- holds (G.6) -----------------------------------------------------------------------

    def _hold(self, m: dict, t: float, inp: dict, result: dict) -> _Hold:
        applying = G.applying_prohibitions(self.policy, inp)
        index = len(self.holds) + 1
        deadline = None if result["timeout"] is None else t + result["timeout"] / 1000.0
        if deadline is not None:
            self._time(deadline)            # a trace error when it cannot be written
        hold = _Hold(
            hold_id=f"h-{index}", index=index, message=dict(m), time=t,
            approver=result["escalate_to"], deadline=deadline,
            contracts=list(result["contracts"]), norms=list(result["norms"]),
            fallback=list(result["fallback"]), notify=list(result["notify"]),
            grounds=[(p["contract"], p["norm"]["id"], name) for p in applying
                     for name in p["instances"]],
            instances=_unique((p["contract"], name) for p in applying
                              for name in p["instances"]))
        self.holds.append(hold)
        return hold

    def _close(self, hold: _Hold, state: str, t: float) -> None:
        """The closing record of a hold that was not approved: the result
        keeps the contracts, norms and approver of the hold, and the
        `fallback` and `notify` of its grounds."""
        hold.state, hold.closed_at = state, t
        result = G._result("deny", state, contracts=hold.contracts, norms=hold.norms,
                           escalate_to=hold.approver, fallback=hold.fallback,
                           notify=hold.notify)
        self._record(t, hold.message, result, hold=hold, outcome=state, closing=True)

    def _expire(self, t: float) -> None:
        """Before each decision and observation later than the deadline of
        a held hold, the hold expires; the record carries the deadline.
        Holds that expire together are closed in the order of their
        deadlines, so the times of the log do not decrease."""
        due = [h for h in self.holds if h.state == "held" and h.deadline is not None
               and t > h.deadline + _EPS]
        for hold in sorted(due, key=lambda h: (h.deadline, h.index)):
            self._close(hold, "expired", hold.deadline)

    def _resolve(self, m: dict | None, t: float) -> list[_Hold]:
        """Apply the approvals and rejections of one observation; returns
        the holds that became approved, in the order of creation."""
        groups: dict[tuple, list[tuple[str, str]]] = {}
        for name, hold_id, performer, key in self._events:
            groups.setdefault((name, hold_id, performer), []).append(key)
        self._events = []
        approved = []
        for (name, hold_id, performer), keys in groups.items():
            hold = next((h for h in self.holds if h.hold_id == hold_id), None)
            if hold is None:
                reason = "unknown_hold"
            elif hold.state != "held":
                reason = "not_held"
            elif performer != hold.approver:
                reason = "not_approver"
            elif not set(keys) & set(hold.instances):
                reason = "other_instance"       # not a contract instance of a ground
            else:
                if name == "approval":
                    hold.state = "approved"
                    approved.append(hold)
                else:
                    self._close(hold, "rejected", t)
                continue
            self._event(t, f"unauthorised_{name}", m,
                        [{"contract": c, "instance": i} for c, i in _unique(keys)],
                        hold_id=hold_id if isinstance(hold_id, str) else None,
                        reason=reason)
        return sorted(approved, key=lambda h: h.index)

    def _recheck(self, hold: _Hold, t: float) -> dict | None:
        """Decide an approved message again at the time of the approval
        (G.6, Release); the denial, or None when it may take effect.

        In a governing contract that has a ground of the hold, the approval
        stands in for the permission and for the grounds: the contract
        denies when a contract instance of a ground no longer runs, or when
        a prohibition applies in a contract instance in which it is not a
        ground. Every other governing contract decides in full by G.5,
        step 3, and has to allow."""
        m = hold.message
        sender, receiver = self._actors(m)
        if G.message_kind(self.policy, receiver, m["name"]) == "undeclared_tool":
            return G._result("deny", "undeclared_tool")
        inp = self.policy_input(m, t)
        running = {i.key for i in self._running()}
        grounds = set(hold.grounds)
        applying = G.applying_prohibitions(self.policy, inp)
        denials = []
        for c in self.policy["contracts"]:
            cid = c["id"]
            if any(cc == cid for cc, _ in hold.instances):
                if any(key not in running for key in hold.instances if key[0] == cid):
                    denials.append(G._result("deny", "no_instance", contracts=[cid]))
                    continue
                extra = [p["norm"] for p in applying if p["contract"] == cid and any(
                    (cid, p["norm"]["id"], name) not in grounds for name in p["instances"])]
                if extra:
                    timeouts = [n["timeout_ms"] for n in extra if n["timeout_ms"] is not None]
                    denials.append(G._result(
                        "deny", "prohibition", contracts=[cid],
                        norms=[f"{cid}.{n['id']}" for n in extra],
                        timeout=min(timeouts) if timeouts else None,
                        fallback=_unique(n["fallback"] for n in extra if n["fallback"]),
                        notify=_unique(n["notify"] for n in extra if n["notify"])))
            elif G.governs(self.policy, c, sender, receiver, m["name"]):
                # The decision of this contract alone: `decide` over a policy
                # that has no other contract.
                alone = G.decide(dict(self.policy, contracts=[c]), inp)
                if alone["decision"] != "allow":
                    denials.append(dict(alone, decision="deny", escalate_to=None))
        if not denials:
            return None
        # Combination as in G.5, step 4: every denying contract is listed.
        reason = next(r for r in ("no_instance", "prohibition", "default")
                      if any(d["reason"] == r for d in denials))
        same = [d for d in denials if d["reason"] == reason == "prohibition"]
        timeouts = [d["timeout"] for d in same if d["timeout"] is not None]
        return G._result(
            "deny", reason,
            contracts=[cid for d in denials for cid in d["contracts"]],
            norms=[n for d in same for n in d["norms"]],
            timeout=min(timeouts) if timeouts else None,
            fallback=_unique(f for d in same for f in d["fallback"]),
            notify=_unique(a for d in same for a in d["notify"]))

    def _release(self, hold: _Hold, t: float) -> None:
        """Let an approved message take effect, or deny it (G.6, Release)."""
        m = hold.message
        hold.closed_at = t
        denial = self._recheck(hold, t)
        self.releases.append({
            "time": self._time(t), "hold_id": hold.hold_id, "message": dict(m),
            "input": self.policy_input(m, t),
            "grounds": [{"contract": c, "norm": n, "instance": i}
                        for c, n, i in hold.grounds],
            "released": denial is None})
        if denial is not None:
            self._record(t, m, denial, hold=hold, outcome="approved", closing=True)
            self.blocks.append({"t": t, "message": m, "contracts": denial["contracts"],
                                "hold": None})
            return
        hold.released = True
        result = G._result("allow", "approved", contracts=hold.contracts,
                           norms=hold.norms, escalate_to=hold.approver)
        self._record(t, m, result, hold=hold, outcome="approved", closing=True)
        exempt: dict[tuple[str, str], set[str]] = {}
        for c, nid, name in hold.grounds:
            exempt.setdefault((c, name), set()).add(nid)
        # The released message is the immediately following observation,
        # with the time of the approving one; it does not violate its grounds.
        self._observe(m, t, exempt=exempt)

    def _blocked(self, inst: _Instance, norm, due: float) -> bool:
        """G.5: whether, between the activation of an obligation and its
        violation, a message of its bearer that matches its content was
        denied by the obligation's own contract, or held on a ground of
        it and not released."""
        event = G.parse_message_event(norm.d.content)
        if event is None or event.name is None or norm.activated_at is None \
                or not norm.d.bearer:
            return False
        bearer = G.parse_ref(norm.d.bearer)
        for b in self.blocks:
            m = b["message"]
            if inst.contract not in b["contracts"] or m["name"] != event.name:
                continue
            if b["hold"] is not None and b["hold"].released:
                continue
            if not norm.activated_at - _EPS <= b["t"] <= due + _EPS:
                continue
            sender, receiver = self._actors(m)
            if sender and receiver and G.kleene_and(
                    G.ref_match(self.policy, event.sender, sender, inst.bindings),
                    G.ref_match(self.policy, event.receiver, receiver, inst.bindings),
                    G.ref_match(self.policy, bearer, sender, inst.bindings)) is True:
                return True
        return False

    # --- amendments (F.6, G.7) ----------------------------------------------------------------

    def _prepare_amendment(self, name: str, t: float):
        new = self._load(name)
        if not G.agents_active(new):
            raise NormsEvalError(f"{name} does not declare 'norms: 0.1' and 'agents: 0.1'")
        policy = G.build_policy(new)
        old_ids = [c["id"] for c in self.policy["contracts"]]
        if [c["id"] for c in policy["contracts"]] != old_ids:
            raise NormsEvalError(
                f"amendment at t={_fmt_t(t)}: {name} has other enforced contracts "
                f"({', '.join(c['id'] for c in policy['contracts']) or 'none'}) than the "
                f"current version ({', '.join(old_ids)}); the runtime amends contracts but "
                f"does not add or remove enforced contracts")
        changed = [cid for cid in old_ids
                   if _plain(self._ast(cid)) != _plain(next(c for c in new.contracts
                                                            if c.id == cid))]
        return new, policy, changed

    def _finish_amendment(self, name: str, new, policy, changed: list[str], t: float) -> None:
        self.sos, self.policy = new, policy
        for inst in self._running():
            self._require_bindings(inst.contract, inst.name, inst.bindings)
        self.amendments.append({"time": self._time(t), "file": name, "contracts": changed})
        self._event(t, "amendment", None, self._governing_instances(None, changed),
                    reason=name)
        for hold in self.holds:                    # G.7, rule (1)
            if hold.state == "held" and any(c in changed for c, _ in hold.instances):
                self._close(hold, "cancelled", t)
        for inst in self._running():               # G.7, rule (2)
            inst.engine.ev.actors = declared_actors(new)
            if inst.contract not in changed:
                continue
            c = self._contract(inst.contract)
            self._build_until(inst)
            for nid, items in inst.derived.items():
                norm = inst.engine.by_id.get(nid)
                if nid not in c["delegations"]:
                    self._end_derived(inst, items, t, "amendment",
                                      "amendment: the permission is not delegable in the "
                                      "new version")
                elif norm is None or norm.state == "discharged":
                    self._end_derived(inst, items, t, "amendment",
                                      "amendment: the original permission was discharged")

    # --- one line of the trace ---------------------------------------------------------------------

    def _line(self, line: Line, first: bool, last: bool) -> None:
        o = line.obs
        t = o.t
        self._lineno = line.lineno
        self._snapshot = o.values
        self._expire(t)
        amendment = self._prepare_amendment(o.amend, t) if o.amend else None

        entries = {(e["contract"], e["instance"]): e for e in line.instances}
        if first:
            for c in self.policy["contracts"]:
                if c["id"] in self.host_managed:
                    continue
                e = entries.get((c["id"], DEFAULT_INSTANCE), {})
                self._create(c["id"], DEFAULT_INSTANCE,
                             e.get("bindings", self.trace.bindings),
                             e.get("request", self.trace.request), t)
        by_key = {i.key: i for i in self.instances}
        ids = [c["id"] for c in self.policy["contracts"]]
        for key, e in entries.items():
            if key[0] not in ids:
                raise TraceError(f"line {line.lineno}: 'instances' names '{key[0]}', "
                                 f"which is not an enforced contract of the file")
            hosted = key[0] in self.host_managed
            if hosted and key[1] == DEFAULT_INSTANCE:
                raise TraceError(
                    f"line {line.lineno}: the instances of {key[0]} are created by the "
                    f"trace; the name '{DEFAULT_INSTANCE}' is reserved for a contract "
                    f"without host-created instances")
            if not hosted and key[1] != DEFAULT_INSTANCE:
                raise TraceError(
                    f"line {line.lineno}: 'instances' creates instance {key[1]} of "
                    f"{key[0]}, but {key[0]} has had its instance '{DEFAULT_INSTANCE}' "
                    f"since the first line. Declare on the first line that a host creates "
                    f"the instances of {key[0]}: name it in 'instances' there, or list it "
                    f"under 'host_instances'")
            inst = by_key.get(key)
            if inst is None:
                if e.get("end"):
                    raise TraceError(f"t={_fmt_t(t)}: instance {key[1]} of {key[0]} is ended "
                                     f"but was never created")
                self._create(key[0], key[1], e.get("bindings", {}), e.get("request", {}), t)
            elif not inst.running:
                raise TraceError(f"t={_fmt_t(t)}: instance {key[1]} of {key[0]} has ended")
            elif not (first and key[1] == DEFAULT_INSTANCE) and (
                    "bindings" in e or "request" in e):
                raise TraceError(f"t={_fmt_t(t)}: 'bindings' and 'request' of instance "
                                 f"{key[1]} of {key[0]} are given when it is created")

        # The time and the values of the line, and an amendment (F.6).
        amend = {c: o.amend for c in amendment[2]} if amendment else None
        self._observe(None, t, amend=amend)
        if amendment:
            self._finish_amendment(o.amend, *amendment, t)

        for m in o.messages:
            self._message(m, t)

        # A change of lifecycle state and the end of instances.
        end_all = o.end is True or (last and o.end is not False)
        by_key = {i.key: i for i in self.instances}
        for inst in list(self.instances):
            if not inst.running:
                continue
            e = entries.get(inst.key, {})
            state = e.get("state")
            if state is None and line.state is not None and inst.engine.lifecycle is not None:
                state = line.state
            end = end_all or bool(e.get("end"))
            if state is None and not end:
                continue
            self._current = None
            self._events = []
            closing = Observation(t=t, values=self._snapshot, messages=[],
                                  state=inst.state, state_after=state or inst.state,
                                  end=True if end else None)
            self._push(inst, closing)

    def run(self) -> dict:
        """Replay every line. Messages after an `end` are still decided:
        an ended instance does not come back, so a tool call of its
        contract is denied with `no_instance`."""
        lines = self.trace.lines
        for k, line in enumerate(lines):
            self._feed(line, k == len(lines) - 1)
        return self.result()

    def _feed(self, line: Line, last: bool) -> None:
        """Process one line of the trace; `last` ends the instances that
        are still running (unless the line says `"end": false`)."""
        first = not self._started
        self._started = True
        self._last_t = line.obs.t
        self._late_names(line)
        self._line(line, first, last)

    # --- online ------------------------------------------------------------------------------

    @classmethod
    def open(cls, sos, versions: dict | None = None, check: bool = True,
             path: str | None = None) -> "AgentsRuntime":
        """An enforcement point that is driven one line at a time:
        `observe` for each line as it happens, `close` at the end. `sos` is
        a parsed file or the path of one. `path` is the path that a trace of
        the run will have (a file named by `amend` is looked up beside it).

        Feeding the lines of a trace to `observe` and then calling `close`
        with the time of a last line gives the records, holds and results
        that `run` gives for the trace that has these lines followed by the
        line ``{"t": time}``: `run` is a loop over the same steps."""
        if not isinstance(sos, SoSDefinition):
            sos = load_version(sos)
        return cls(sos, None, versions, check, path=path)

    def observe(self, line: dict) -> dict:
        """Process one trace line (a dict with the keys of a trace line) at
        its time `t`. Returns what the line produced: `steps` (the decisions,
        as in the result of `run`), `audit` (the records of G.8, in order)
        and `holds` (the holds created or closed, as in the result of
        `run`). Raises TraceError or NormsEvalError for a line that cannot
        be processed; the enforcement point is then failed and refuses
        every further line (fail-closed)."""
        return self._online(line, last=False)

    def close(self, t: float | None = None) -> dict:
        """End the run with the line ``{"t": t}`` (the time of the last line
        when `t` is None): the contract instances that are still running
        end, as at the last line of a trace. Returns what `observe` does."""
        if t is None:
            t = self._last_t
        out = self._online({"t": t}, last=True)
        self._closed = True
        return out

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def failed(self) -> str | None:
        """Why the online enforcement point failed, or None."""
        return self._failed

    def _online(self, rec: dict, last: bool) -> dict:
        if self._reader is None:
            raise NormsEvalError("this runtime replays a trace; use AgentsRuntime.open "
                                 "for an online enforcement point")
        if self._closed:
            raise NormsEvalError("the enforcement point is closed")
        if self._failed is not None:
            raise NormsEvalError(f"the enforcement point has failed and decides nothing "
                                 f"more: {self._failed}")
        steps, audit = len(self.steps), len(self.audit)
        holds = {h.hold_id: h.state for h in self.holds}
        try:
            line = self._reader.add(rec)
            if not self._started:
                self._begin(line)
            self._feed(line, last)
        except Exception as e:
            self._failed = f"{type(e).__name__}: {e}"
            raise
        changed = [h for h in self.holds if holds.get(h.hold_id) != h.state]
        return {"steps": self.steps[steps:], "audit": self.audit[audit:],
                "holds": [self._hold_entry(h) for h in changed]}

    def _late_names(self, line: Line) -> None:
        """Warn when a name that a predicate uses as a value gets its first
        value after a decision: until then it was read as a symbolic value."""
        new = set(line.obs.values) - self._valued
        self._valued |= new
        if self.trace.quantities is not None or not self.steps:
            return
        late = sorted(new & self._bare)
        if late:
            self.warnings.append(
                f"line {line.lineno}: {', '.join(late)} get(s) a first value after "
                f"earlier messages were decided; until then a predicate read the name as "
                f"a symbolic value (F.5). Give the quantity names of the run with "
                f"'quantities' on the first line to make them quantities from the start")

    # --- result ---------------------------------------------------------------------------------------

    def _hold_entry(self, h: _Hold) -> dict:
        return {
            "hold_id": h.hold_id, "state": h.state, "message": self._head(h.message),
            "time": self._time(h.time), "approver": h.approver,
            "deadline": None if h.deadline is None else self._time(h.deadline),
            "contracts": h.contracts, "norms": h.norms,
            "closed": None if h.closed_at is None or h.state == "held"
            else self._time(h.closed_at),
            "released": h.released,
        }

    def held(self) -> list[dict]:
        """The holds that are `held`, with their deadline as a time of the
        trace (`t`), for a host that has to observe their expiry."""
        return [{"hold_id": h.hold_id, "deadline_t": h.deadline,
                 "message": self._head(h.message), "approver": h.approver}
                for h in self.holds if h.state == "held"]

    def result(self) -> dict:
        instances = []
        for inst in self.instances:
            eng = inst.engine
            eng._finish_items()
            r = eng.result()
            taking_part = {n["id"] for n in self._contract(inst.contract)["norms"]}
            for n in r["norms"]:
                n["in_force"] = (eng.in_force(n["id"]) if inst.running else False) \
                    if n["id"] in taking_part else None
            for w in r["warnings"]:
                # The norms engine says "it matches any X" (F.3); in an
                # enforced contract that is not what happens.
                w = w.replace("; it matches any " + w.rsplit(" ", 1)[-1],
                              "; in an enforced contract what depends on it is "
                              "unknown (G.5)") if "('bindings'); it matches any " in w else w
                text = f"{inst.contract}/{inst.name}: {w}"
                if text not in self.warnings:
                    self.warnings.append(text)
            instances.append({
                "contract": inst.contract, "instance": inst.name,
                "bindings": inst.bindings, "running": inst.running,
                "start": self._time(inst.started),
                "end": None if inst.ended is None else self._time(inst.ended),
                "state": r["instance"]["state"],
                "norms": r["norms"], "violations": r["violations"],
                "institutional_events": r["institutional_events"],
                "unauthorised": r["unauthorised"],
                "ok": r["ok"],
            })
        holds = [self._hold_entry(h) for h in self.holds]
        delegations = [{
            "contract": inst.contract, "instance": inst.name,
            "norm": f"{inst.contract}.{d.norm}", "holder": d.holder, "chain": d.chain,
            "state": d.state, "valid": inst.running and self._valid(
                inst, self._contract(inst.contract), d),
            "granted": self._time(d.granted_at),
            "ended": None if d.ended_at is None else self._time(d.ended_at),
            "end_reason": d.end_reason,
        } for inst in self.instances for items in inst.derived.values() for d in items]
        decisions = [s["result"]["decision"] for s in self.steps]
        summary = {
            "messages": len(self.steps),
            "allow": decisions.count("allow"), "deny": decisions.count("deny"),
            "escalate": decisions.count("escalate"),
            "holds": {s: sum(1 for h in self.holds if h.state == s) for s in HOLD_STATES},
            "released": sum(1 for h in self.holds if h.released),
            "audit_records": len(self.audit),
        }
        return {
            "agents_version": "0.1",
            "trace": self.trace.path,
            "contracts": [c["id"] for c in self.policy["contracts"]],
            "summary": summary,
            "audit": self.audit,
            "steps": self.steps,
            "releases": self.releases,
            "holds": holds,
            "delegations": delegations,
            "instances": instances,
            "amendments": self.amendments,
            "warnings": self.warnings,
        }


def _unique(items) -> list:
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _require_valid(sos: SoSDefinition, what: str) -> None:
    """Refuse a file with errors of `cadl check` (among them the rules G-1
    to G-18, on which the runtime relies)."""
    from .type_checker import type_check
    result = type_check(sos)
    if not result.ok:
        errors = "\n".join(f"  {e}" for e in result.errors)
        raise NormsEvalError(f"{what} has {len(result.errors)} type-check error(s); the "
                             f"runtime does not run it:\n{errors}")


def run(sos: SoSDefinition, trace: RunTrace, versions: dict | None = None,
        check: bool = True) -> dict:
    """Replay a trace through the enforcement point of a file (G.5 to
    G.8). `versions` maps the value of an ``amend`` key to a parsed file;
    a value that is not in it is read as a path relative to the trace
    file. Returns a JSON-serializable dict; see the README for its keys.

    The file and every version it is amended to must pass `cadl check`,
    and every contract instance must bind the indices of its parties;
    otherwise NormsEvalError or TraceError is raised. ``check=False``
    switches both tests off. It is **unsafe**: the runtime then relies on
    its fail-closed resolution of unknown occurrences alone. It exists for
    the tests of that resolution."""
    return AgentsRuntime(sos, trace, versions, check).run()


def run_files(cadl_path, trace_path) -> dict:
    """Parse and type-check a CADL file, read a trace and replay it."""
    if not Path(trace_path).exists():
        raise NormsEvalError(f"File not found: {trace_path}")
    sos = load_version(cadl_path)
    result = run(sos, load_run_trace(trace_path))
    return {"file": str(cadl_path), **result}


def audit_lines(result: dict) -> list[str]:
    """The audit log as JSON Lines: one record per line, in the order in
    which the enforcement point wrote them."""
    return [json.dumps(rec, ensure_ascii=False, allow_nan=False) for rec in result["audit"]]


# === Text report ===========================================================

def _msg(m: dict | None) -> str:
    return "-" if m is None else f"{m['from']} -> {m['to']} : {m['name']}"


def _when(value: Any) -> str:
    return value if isinstance(value, str) else f"t={_fmt_t(value)}"


def format_text(result: dict) -> str:
    s = result["summary"]
    lines = [f"Enforcement point (agents {result['agents_version']}): "
             + ", ".join(result["contracts"])]
    lines += ["", "Audit log"]
    rows = []
    for rec in result["audit"]:
        if "decision" in rec:
            what = f"{rec['decision']} ({rec['reason']})"
            notes = []
            if rec["norms"]:
                notes.append(", ".join(rec["norms"]))
            if rec["provenance"]:
                notes.append("chain " + "; ".join(" > ".join(c) for c in rec["provenance"]))
            if rec["hold_id"] and rec["outcome"] is None:
                limit = "" if rec["timeout"] is None else f", timeout {rec['timeout']} ms"
                notes.append(f"hold {rec['hold_id']} for {rec['escalate_to']}{limit}")
            elif rec["hold_id"]:
                notes.append(f"closes hold {rec['hold_id']}: {rec['outcome']}")
            if rec["fallback"]:
                notes.append("fallback " + ", ".join(rec["fallback"]))
        else:
            what = rec["event"]
            notes = [x for x in (rec["norm"], rec["reason"]) if x]
            if rec["chain"]:
                notes.append("chain " + " > ".join(rec["chain"]))
            if rec["event"] == "revoke" and rec["holders"]:
                notes.append("discharged " + ", ".join(rec["holders"]))
            if rec["hold_id"]:
                notes.append(f"hold {rec['hold_id']}")
        rows.append([_when(rec["time"]), _msg(rec["message"]), what, "; ".join(notes)])
    lines += _table(rows) if rows else ["  (none)"]

    if result["holds"]:
        lines += ["", "Holds"]
        rows = [["id", "state", "message", "created", "approver", "deadline", "closed"]]
        for h in result["holds"]:
            rows.append([h["hold_id"], h["state"], _msg(h["message"]), _when(h["time"]),
                         h["approver"],
                         "-" if h["deadline"] is None else _when(h["deadline"]),
                         "-" if h["closed"] is None else _when(h["closed"])])
        lines += _table(rows)

    if result["delegations"]:
        lines += ["", "Derived instances"]
        rows = [["norm", "instance", "chain", "state", "granted", "ended"]]
        for d in result["delegations"]:
            ended = "-" if d["ended"] is None else f"{_when(d['ended'])} ({d['end_reason']})"
            rows.append([d["norm"], d["instance"], " > ".join(d["chain"]), d["state"],
                         _when(d["granted"]), ended])
        lines += _table(rows)

    lines += ["", "Norms"]
    for inst in result["instances"]:
        span = _when(inst["start"]) + " .. " + (
            "running" if inst["end"] is None else _when(inst["end"]))
        bound = "".join(f", {k}={v}" for k, v in inst["bindings"].items())
        lines.append(f"  {inst['contract']} / {inst['instance']} ({span}{bound})")
        rows = [["id", "kind", "state", "verdict", "reason"]]
        rows += [[n["id"], n["kind"], n["state"], n["verdict"], n["reason"]]
                 for n in inst["norms"]]
        lines += _table(rows, indent="    ")
        for v in inst["violations"]:
            lines.append(f"    violation t={_fmt_t(v['t'])} {v['norm']} "
                         f"({v['severity']}, {v['verdict']})")

    if result["warnings"]:
        lines += ["", "Warnings"]
        lines += [f"  {w}" for w in result["warnings"]]

    held = s["holds"]
    closed = ", ".join(f"{held[k]} {k}" for k in HOLD_STATES if held[k])
    lines += ["", f"Result: {s['messages']} message(s): {s['allow']} allow, {s['deny']} deny, "
                  f"{s['escalate']} escalate"
                  + (f"; holds: {closed}" if closed else "")
                  + f"; {s['audit_records']} audit record(s)"]
    return "\n".join(lines)
