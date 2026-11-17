# Template of the norms monitor that `cadl codegen -t python` generates.
#
# cadl does not import this file. `norms_monitor_gen.py` copies its two
# parts verbatim into the generated `norms_monitor.py`, around the engine
# of `cadl/norms_eval.py`: part 1 goes before the engine and part 2 after
# it. The names the parts use without defining them (the AST classes,
# `_Engine`, `Trace`, `_TraceReader`, `MODEL`, ...) are defined in the
# generated file. Nothing from a .cadl file is ever written into this
# text; the generated file carries it as data (`MODEL`).
#
# The code must run on Python 3.9 with the standard library only.

# >>> part 1

class MonitorError(ValueError):
    """The monitor cannot be used in this way."""


def _decode(data: Any) -> Any:
    """An expression or definition of `MODEL` as objects of the AST
    classes above. The class is looked up in a fixed table."""
    if isinstance(data, list):
        return [_decode(x) for x in data]
    if isinstance(data, dict):
        cls = _NODE_TYPES[data["#"]]
        return cls(**{k: _decode(v) for k, v in data.items() if k != "#"})
    return data


# What the parser of cadl returned at generation time for every text the
# engine parses: kind ("rule", "expr", "actor") -> text -> encoded result,
# or None when the text did not parse.
_PARSED: dict[str, dict[str, Any]] = {"rule": {}, "expr": {}, "actor": {}}
_PARSE_MISSES: list[str] = []


def _parsed(kind: str, text: str) -> Any:
    table = _PARSED[kind]
    if text not in table:
        _PARSE_MISSES.append(text)
        raise MonitorError(f"no parsed form of {text!r} in the model")
    data = table[text]
    if data is None:
        raise ValueError("it does not parse")
    return _decode(data)          # a new object each time: the engine changes some


def _parse_rule(text: str) -> Any:
    return _parsed("rule", text)


def _parse_expr(text: str) -> Any:
    return _parsed("expr", text)


def _parse_actor(text: str) -> Any:
    return _parsed("actor", text)


def item_text(expr: Any) -> str:
    """An ``assume:`` / ``guarantee:`` item as text (kept in the model)."""
    return expr._item_text


class _Actor:
    """A declared actor: its `id` is the actor reference of the declaration."""

    def __init__(self, ref: Any) -> None:
        self.id = ref


class _Model:
    """The declared actors and the contracts of one version of the file."""

    def __init__(self, model: dict) -> None:
        if not isinstance(model, dict) or model.get("norms_version") != "0.1":
            raise MonitorError("not a model of a norms 0.1 monitor")
        for kind, table in model["parsed"].items():
            _PARSED[kind].update(table)
        self.actors = [_Actor(_decode(a)) for a in model["actors"]]
        self.contracts = [self._contract(c) for c in model["contracts"]]

    @staticmethod
    def _contract(c: dict) -> Any:
        items = {}
        for key in ("assume", "guarantee"):
            items[key] = []
            for item in c[key]:
                expr = _decode(item["expr"])
                expr._item_text = item["text"]
                items[key].append(expr)
        lc = c["lifecycle"]
        return ContractDef(
            id=c["id"], parties=[], assume=items["assume"], guarantee=items["guarantee"],
            lifecycle=None if lc is None else LifecycleSpec(
                states=list(lc["states"]), initial=lc["initial"], terminal=list(lc["terminal"])),
            norms=_decode(c["norms"]), counts_as=_decode(c["counts_as"]),
        )

# <<< part 1

# >>> part 2
# === The online monitor =====================================================

def _after(x: Any) -> float:
    """The smallest float whose decimal value (as Python writes it) is
    later than the exact time x."""
    c = float(x)
    while Fraction(repr(c)) <= x:
        c = math.nextafter(c, math.inf)
    return c


class ObservationError(MonitorError, NormsEvalError):
    """An observation was refused for a reason other than its form (for
    that, see `TraceError`): an amendment that cannot be applied. The
    monitor is as it was before the observation."""


class _Versions:
    """The models of the versions an ``amend`` key can name (F.6)."""

    def __init__(self, versions: Any) -> None:
        self.models = {}
        for name, model in dict(versions or {}).items():
            try:
                _Model(model)
            except (KeyError, TypeError, AttributeError, ValueError) as e:
                raise MonitorError(
                    f"versions[{name!r}] is not the MODEL of a norms monitor "
                    f"({type(e).__name__}: {e})") from None
            self.models[name] = copy.deepcopy(model)

    def __call__(self, name: str) -> Any:
        if name not in self.models:
            raise NormsEvalError(
                f"the monitor has no version '{name}' of the contract; give the MODEL of the "
                f"monitor generated from it with versions=")
        return _Model(self.models[name])


class NormsMonitor:
    """Monitor of one contract instance (Appendix F, F.5).

    ::

        m = NormsMonitor("DELIVERY_SLA", bindings={"i": 2})
        changes = m.observe({"t": 0.0, "values": {"ROBOT[2].battery": 80}})
        changes = m.observe({"t": 1.0, "messages": [
            {"from": "DISPATCHER", "to": "ROBOT[2]", "name": "route_assignment"}]})
        ...
        changes = m.end()
        m.verdicts()        # the current verdicts
        m.result()          # everything, as `cadl norms-eval --format json`

    An observation is a dict with the keys of one line of a `cadl
    norms-eval` trace: ``t`` (seconds, required, non-decreasing, finite),
    ``values`` (updates of the snapshot), ``messages``, ``state`` (the
    lifecycle state after this observation), ``end`` and ``amend``.
    ``bindings`` and ``request`` may be given to the constructor or in the
    first observation (the first observation wins). The monitor copies
    what it is given.

    `contract` names the contract; it may be left out when the file has
    one contract with ``norms:`` or ``counts_as:``.

    `quantities` names the observed quantities that are written without
    an actor (``load``, not ``ROBOT[1].load``). Such a name is undefined
    until its first value. A name that is not given here is a quantity
    only from its first value on; before that it is read as a symbolic
    value (``status == Delivered``), which is the one way in which the
    monitor can differ from `cadl norms-eval` on the same observations.
    Give the names whenever a predicate compares such a quantity with a
    text value.

    `versions` maps the name used by an ``amend`` key to the ``MODEL`` of
    the monitor generated from the new version of the file (F.6).

    The monitor decides with the observations it has received. A deadline
    that passes, and an interval of a temporal operator that ends, is
    detected at the first observation after it, so a host with nothing to
    report sends a time-only observation ``{"t": ...}`` at the time
    `next_check` gives. The monitor keeps the observations of its
    instance; make one monitor per contract instance and drop it when the
    instance has ended. It is not thread-safe.

    A refused observation leaves the monitor exactly as it was, and the
    monitor stays usable: `observe` raises `TraceError` for an observation
    that is malformed (wrong types, time going back, a state the lifecycle
    does not have) and `ObservationError` for an amendment that cannot be
    applied (no model of that version, no such contract in it, the
    current lifecycle state missing in it).
    """

    def __init__(self, contract: str | None = None, bindings: dict | None = None,
                 request: dict | None = None, quantities: Any = None,
                 versions: dict | None = None, _option: str = "contract=") -> None:
        model = _Model(MODEL)
        self._contract = select_contract(model, contract, _option)
        self._actors = declared_actors(model)
        self._trace = Trace(observations=[], bindings=copy.deepcopy(dict(bindings or {})),
                            request=copy.deepcopy(dict(request or {})))
        self._reader = _TraceReader(self._trace, MODEL.get("sos_version"))
        self._quantities = {_canon_actor(q) for q in (quantities or ())}
        self._versions = _Versions(versions)
        # False only in `replay`, for a trace with an amendment: `cadl
        # norms-eval` then says nothing about a state without lifecycle.
        self._warn_state = True
        self._engine: Any = None
        self._ignored = 0

    # --- input ----------------------------------------------------------------

    def observe(self, observation: dict, where: str | None = None) -> list:
        """Process one observation and return the changes it made: a list
        of dicts, one per norm instance whose state or verdict changed and
        per ``assume:`` / ``guarantee:`` item that was decided (see the
        module documentation). A refused observation raises `TraceError`
        or `ObservationError` and leaves the monitor as it was. An
        observation after the end of the instance has no effect: it is
        checked for its form, counted, and returns no change."""
        obs = self._trace.observations
        n = len(obs)
        if isinstance(observation, dict):
            observation = copy.deepcopy(observation)
        reader = self._reader
        saved = (dict(reader.snapshot), reader.state, reader.prev_t,
                 self._trace.bindings, self._trace.request)
        o = reader.add(observation, where or f"observation {n + self._ignored + 1}")
        if self.ended:
            # not part of the instance (F.5): only the order of time goes on
            obs.pop()
            reader.snapshot, reader.state = saved[0], saved[1]
            self._ignored += 1
            return []
        touched = False
        try:
            eng = self._prepare(self._engine, n)
            before = self._marks(eng)
            touched = True
            self._step(eng, n)
        except Exception as e:
            obs.pop()
            (reader.snapshot, reader.state, reader.prev_t,
             self._trace.bindings, self._trace.request) = saved
            if touched:
                self._engine = self._rebuilt()
            if isinstance(e, (TraceError, ObservationError)):
                raise
            if isinstance(e, (NormsEvalError, MonitorError)):
                raise ObservationError(str(e)) from None
            raise
        self._engine = eng
        return self._changes(before, o.t)

    def end(self) -> list:
        """End the contract instance at the last observation received and
        settle its norms (F.5, item 6). Returns the changes, as `observe`.
        To end the instance at a later time, send an observation with
        ``"end": True`` instead."""
        eng = self._started()
        if eng.ended_at is not None:
            return []
        n = len(self._trace.observations) - 1
        before = self._marks(eng)
        eng._settle(n, _END_LAST)
        eng._finish_items()
        return self._changes(before, self._trace.observations[n].t)

    # --- state ------------------------------------------------------------------

    @property
    def contract(self) -> str:
        return self._contract.id

    @property
    def started(self) -> bool:
        return self._engine is not None

    @property
    def ended(self) -> bool:
        return self._engine is not None and self._engine.ended_at is not None

    def verdicts(self) -> dict:
        """The current verdicts: ``{"norms": {id: {"state", "verdict"}},
        "assumptions": [{"index", "text", "status"}], "guarantees":
        [{"index", "text", "verdict"}], "ok"}``. ``ok`` is false when a norm
        or a guarantee is ``violated``."""
        eng = self._started()
        norms = {x.d.id: {"state": x.state, "verdict": x.verdict} for x in eng.norms}
        out: dict = {"norms": norms, "assumptions": [], "guarantees": []}
        for key, name, field_ in (("assume", "assumptions", "status"),
                                  ("guarantee", "guarantees", "verdict")):
            for pos, item in enumerate(eng.items[key], start=1):
                out[name].append({"index": pos, "text": item["text"],
                                  field_: self._item_mark(key, item)})
        out["ok"] = (all(v["verdict"] != "violated" for v in norms.values())
                     and all(g["verdict"] != "violated" for g in out["guarantees"]))
        return out

    def result(self) -> dict:
        """Everything decided so far, with the keys of `cadl norms-eval
        --format json` (without ``file``). Before the instance has ended
        it is the result for a trace that is a prefix."""
        eng = self._started()
        eng._finish_items()
        result = eng.result()
        if self._ignored:
            result["warnings"] = result["warnings"] + [eng._ignored_warning(self._ignored)]
        return copy.deepcopy(result)                 # the caller may change it

    def next_deadline(self) -> float | None:
        """The earliest time at which the deadline of an active obligation
        or the window of an active prohibition is over, or None. This is
        the deadline itself: an observation at exactly this time is still
        in time, and the monitor judges the deadline at the first
        observation after it (times are compared exactly). To know when to
        observe, use `next_check`."""
        eng = self._started()
        if eng.ended_at is not None:
            return None
        t = self._trace.observations[-1].t
        due = [t + max(x.deadline - x.elapsed(t), 0) for x in self._timed(eng, t)]
        return float(min(due)) if due else None

    def next_check(self) -> float | None:
        """The earliest time stamp at which an observation is certain to
        decide something that now only waits for time: a deadline of an
        active obligation that has passed, the window of an active
        prohibition that has closed, or a temporal operator (in an
        ``assume:`` / ``guarantee:`` item or in the content of a norm)
        whose interval is over. None when nothing waits for time, and
        after the end.

        A host with nothing else to report sends ``{"t": m.next_check()}``
        when that time has come. It is the smallest float whose decimal
        value is later than the deadline or the end of the interval,
        because an observation at the deadline itself is still in time
        (times are compared exactly). For nested temporal operators it is the time at which
        the outermost one is decided. A norm without deadline whose
        content has a temporal operator is evaluated at every observation,
        so each observation gives it a new time to check."""
        eng = self._started()
        if eng.ended_at is not None:
            return None
        obs = self._trace.observations
        t = obs[-1].t
        times = []
        for x in self._timed(eng, t):
            times.append(_after(t + max(x.deadline - x.elapsed(t), 0)))
        for x in eng.norms:
            if not x.frozen and x.state == "active" and x.content.kind == "predicate":
                times.extend(self._decided_after(eng, obs[k].t, x.content.expr)
                             for k in x.candidates)
        for key in ("assume", "guarantee"):
            for item in eng.items[key]:
                if (item["temporal"] and not item["error"] and not item["closed"]
                        and item["value"] is None and item["start"] < len(obs)):
                    times.append(self._decided_after(eng, obs[item["start"]].t, item["expr"]))
        if not times:
            return None
        return max(min(times), _after(t))

    # --- internals ----------------------------------------------------------------

    def _started(self) -> Any:
        if self._engine is None:
            raise MonitorError("the contract instance has no observation")
        return self._engine

    @staticmethod
    def _timed(eng: Any, t: float) -> list:
        """The active obligations and prohibitions whose deadline has not
        yet been found to have passed."""
        return [x for x in eng.norms
                if x.state == "active" and not x.frozen and x.deadline is not None
                and x.d.kind in ("obligation", "prohibition")
                and x.elapsed(t) <= x.deadline]

    @classmethod
    def _span(cls, eng: Any, expr: Any) -> tuple:
        """(the longest sum of the durations of nested temporal operators
        in `expr`, their nesting depth)."""
        if isinstance(expr, FunctionCall) and expr.name in TEMPORAL_OPERATORS:
            dpos = TEMPORAL_OPERATORS[expr.name][1]
            spans = [cls._span(eng, a) for i, a in enumerate(expr.args) if i != dpos]
            return (eng.ev.value(expr.args[dpos], 0, 0) + max(s[0] for s in spans),
                    1 + max(s[1] for s in spans))
        parts = []
        if isinstance(expr, BinaryOp):
            parts = [expr.left, expr.right]
        elif isinstance(expr, UnaryOp):
            parts = [expr.operand]
        elif isinstance(expr, QuantifiedExpr):
            parts = [expr.predicate]
        spans = [cls._span(eng, p) for p in parts] or [(0, 0)]
        return max(s[0] for s in spans), max(s[1] for s in spans)

    @classmethod
    def _decided_after(cls, eng: Any, start: float, expr: Any) -> float:
        """A time stamp after every interval of `expr` evaluated at `start`."""
        span, _ = cls._span(eng, expr)
        return _after(start + span)

    def _prepare(self, eng: Any, n: int) -> Any:
        """The engine ready for observation n, which is the last of the
        trace. Nothing of an existing engine is changed when this raises."""
        o = self._trace.observations[n]
        if eng is None:
            eng = _Engine(self._contract, self._trace, self._versions, self._actors)
            self._check_parsed()
            if _STATE_IGNORED in eng.warnings:   # said in _step, see there
                eng.warnings.remove(_STATE_IGNORED)
            if not o.amend:
                eng._check_state(n)
            eng.ev.known.update(self._quantities)
            return eng
        if eng.lifecycle is not None:
            eng._fill_states(n)
        if not o.amend:
            eng._check_state(n)
        eng.ev.known.update(o.values)
        return eng

    def _step(self, eng: Any, n: int) -> None:
        o = self._trace.observations[n]
        eng._step(n, False)
        self._check_parsed()
        # `cadl norms-eval` says this once, first, for a trace without
        # amendment; here it is said when the first such state arrives.
        if (self._warn_state and eng.lifecycle is None and o.state_after is not None
                and _STATE_IGNORED not in eng.warnings):
            eng.warnings.insert(0, _STATE_IGNORED)
        # Also when the instance goes on: it gives a decided guarantee its
        # verdict and the undecided items the reason of a prefix.
        eng._finish_items()

    def _rebuilt(self) -> Any:
        """A new engine in the state the observations kept so far lead to:
        they are processed again, as they were the first time."""
        obs = self._trace.observations
        kept = obs[:]
        del obs[:]
        eng = None
        for n, o in enumerate(kept):
            obs.append(o)
            eng = self._prepare(eng, n)
            self._step(eng, n)
        return eng

    def _check_parsed(self) -> None:
        if _PARSE_MISSES:
            missing = ", ".join(repr(x) for x in _PARSE_MISSES)
            del _PARSE_MISSES[:]
            raise MonitorError(f"the model has no parsed form of {missing}; generate the "
                               f"monitors of all versions with the same cadl")

    @staticmethod
    def _item_mark(key: str, item: dict) -> str:
        if key == "guarantee":
            return item["verdict"]
        v = item["value"]
        return "pending" if v in (None, "undefined") else "holds" if v else "failed"

    def _marks(self, eng: Any) -> dict:
        marks: dict = {}
        for x in eng.norms:
            marks[("norm", x.d.id)] = (x.state, x.verdict)
        for key in ("assume", "guarantee"):
            for item in eng.items[key]:
                marks[(key, id(item))] = self._item_mark(key, item)
        return marks

    def _changes(self, before: dict, t: Any) -> list:
        t = float(t)
        eng = self._engine
        out = []
        for x in eng.norms:
            old = before.get(("norm", x.d.id), (None, None))
            if old != (x.state, x.verdict):
                out.append({"t": t, "kind": "norm", "id": x.d.id, "state": x.state,
                            "verdict": x.verdict, "previous_state": old[0],
                            "previous_verdict": old[1], "reason": x.reason})
        for key, old in before.items():           # an amendment removed the norm
            if key[0] == "norm" and key[1] not in eng.by_id:
                out.append({"t": t, "kind": "norm", "id": key[1], "state": None,
                            "verdict": None, "previous_state": old[0],
                            "previous_verdict": old[1],
                            "reason": "removed by the amendment"})
        for key, kind, field_ in (("assume", "assumption", "status"),
                                  ("guarantee", "guarantee", "verdict")):
            for pos, item in enumerate(eng.items[key], start=1):
                now = self._item_mark(key, item)
                old = before.get((key, id(item)), "pending")
                if old != now:
                    out.append({"t": t, "kind": kind, "index": pos, "text": item["text"],
                                field_: now, "previous": old, "reason": item["reason"]})
        return out


def replay(lines: Any, contract: str | None = None, versions: dict | None = None,
           path: str | None = None, _option: str = "contract=") -> dict:
    """Feed a recorded trace (an iterable of JSON Lines, as for `cadl
    norms-eval`) to a new monitor, one observation at a time, and return
    its result. As `cadl norms-eval` does, the names of the quantities are
    taken from all observations of the contract instance, and the instance
    ends at the last line unless that line says ``"end": false``."""
    lines = list(lines)
    # the whole trace is well-formed
    trace = parse_trace(lines, path=path, own_version=MODEL.get("sos_version"))
    records = [loads_strict(line) for line in lines if line.strip()]
    where = [f"line {i}" for i, line in enumerate(lines, start=1) if line.strip()]
    # First find where the instance ends: what follows is not part of it.
    probe = NormsMonitor(contract, versions=versions, _option=_option)
    count = 0
    for rec, w in zip(records, where):
        probe.observe(rec, where=w)
        count += 1
        if probe.ended:
            break
    inside = trace.observations[:count]
    m = NormsMonitor(contract, quantities=inside[-1].values, versions=versions, _option=_option)
    m._trace.path = path
    m._warn_state = not any(o.amend for o in inside)
    for rec, w in zip(records, where):
        m.observe(rec, where=w)
    if not m.ended and records[-1].get("end") is not False:
        m.end()
    return m.result()


def main(argv: list | None = None) -> int:
    """``python -m PACKAGE.norms_monitor TRACE.jsonl``: replay a recorded
    trace and print the result. Exit status as `cadl norms-eval`: 0 when
    no norm or guarantee is ``violated``, 1 otherwise, 2 on a usage
    error."""
    import argparse
    ap = argparse.ArgumentParser(
        prog="norms_monitor",
        description="Replay a recorded trace of one contract instance through the "
                    "generated norms monitor (norms 0.1).")
    ap.add_argument("trace", nargs="?", help="trace file (JSON Lines)")
    ap.add_argument("--contract", help="contract id, when the file has several")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    ap.add_argument("--model-version", action="append", default=[], metavar="NAME=MODEL.json",
                    help="the model of a new version of the file, for a line with "
                         "\"amend\": \"NAME\" (write it with --dump-model of the monitor "
                         "generated from that version)")
    ap.add_argument("--dump-model", action="store_true",
                    help="print the model of this monitor as JSON and exit")
    args = ap.parse_args(argv)
    if args.dump_model:
        print(json.dumps(MODEL, ensure_ascii=True, allow_nan=False))
        return 0
    if args.trace is None:
        ap.error("the trace file is required")
    versions = {}
    try:
        for spec in args.model_version:
            name, sep, file = spec.partition("=")
            if not sep:
                raise NormsEvalError(f"--model-version needs NAME=MODEL.json, not '{spec}'")
            with open(file, encoding="utf-8") as f:
                versions[name] = json.load(f)
        try:
            with open(args.trace, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            raise NormsEvalError(f"File not found: {args.trace}") from None
        result = replay(lines, args.contract, versions, path=str(args.trace),
                        _option="--contract")
    except TraceError as e:
        print(f"Trace error: {args.trace}: {e}", file=sys.stderr)
        return 2
    except (NormsEvalError, MonitorError, OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_text(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

# <<< part 2

# >>> part 3
# === Regime bindings (Appendix A, A.7.1) =====================================
# This part is in the generated module only when the file has `regimes:`.
# It replaces `NormsMonitor` and `replay` above by versions that read the
# key ``regime`` of an observation (F.5, "Contracts not in force").

_REGIMES = MODEL["regimes"]


class NormsMonitor(NormsMonitor):
    __doc__ = (NormsMonitor.__doc__ or "") + """
    Regimes (Appendix A, A.7.1). An observation may name the current
    regime with the key ``regime``; it stays current until another
    observation names one. Before the first such key the run is in the
    initial regime of A.7.1. At an observation of a regime that
    does not have the contract in force, the instance sees nothing: the
    observation returns no change, its values are seen from the next
    observation at which the contract is in force, and the deadlines and
    the intervals of the temporal operators do not advance (F.5,
    "Contracts not in force"). `next_deadline` and `next_check` are then
    None. An instance that ends at such an observation (``"end": true``, a
    terminal state, or `end`) is settled there. A monitor that has been
    given a regime refuses an amendment, and the other way round; so does
    a monitor of a contract that the initial regime does not have in force.
    """

    def __init__(self, contract: str | None = None, bindings: dict | None = None,
                 request: dict | None = None, quantities: Any = None,
                 versions: dict | None = None, _option: str = "contract=") -> None:
        super().__init__(contract, bindings, request, quantities, versions, _option)
        self._regimes = RegimeBindings(_REGIMES["regimes"], _REGIMES["suspended"], _REGIMES["initial"])
        self._clock = _ContractClock(self._regimes, self._contract.id)
        self._held: Any = None       # the last observation, left out
        self._left_out = 0           # observations left out so far
        self._named = False          # an observation has named a regime
        self._amended = False

    def observe(self, observation: dict, where: str | None = None) -> list:
        obs = self._trace.observations
        n = len(obs)
        if isinstance(observation, dict):
            observation = copy.deepcopy(observation)
        where = where or f"observation {n + self._ignored + self._left_out + 1}"
        observation, regime = split_regime(observation, where, self._regimes)
        amend = isinstance(observation, dict) and observation.get("amend") is not None
        apart = not self._regimes.in_force(self._contract.id, self._regimes.initial)
        if not self.ended and ((regime is not None and self._amended)
                               or (amend and (self._named or regime or apart))):
            raise ObservationError(_REGIMES_NO_AMEND)
        reader = self._reader
        saved = (dict(reader.snapshot), reader.state, reader.prev_t,
                 self._trace.bindings, self._trace.request)
        clock, held, left_out = self._clock.save(), self._held, self._left_out
        o = reader.add(observation, where)
        o.regime = regime
        if self.ended:
            # not part of the instance (F.5): only the order of time goes on
            obs.pop()
            reader.snapshot, reader.state = saved[0], saved[1]
            self._ignored += 1
            return []
        inside, o.c = self._clock.step(o.t, regime)
        o.out = not inside
        self._held = None
        try:
            if not inside:
                _regime_check_state(o, self._contract)
                if not _regime_ends_at(o, self._contract.lifecycle, False):
                    obs.pop()
                    self._held = o
                    self._left_out += 1
                    self._named = self._named or regime is not None
                    return []
        except TraceError:
            obs.pop()
            (reader.snapshot, reader.state, reader.prev_t,
             self._trace.bindings, self._trace.request) = saved
            self._clock.restore(clock)
            self._held, self._left_out = held, left_out
            raise
        touched = False
        try:
            eng = self._prepare(self._engine, n)
            before = self._marks(eng)
            touched = True
            self._step(eng, n)
        except Exception as e:
            obs.pop()
            (reader.snapshot, reader.state, reader.prev_t,
             self._trace.bindings, self._trace.request) = saved
            self._clock.restore(clock)
            self._held, self._left_out = held, left_out
            if touched:
                self._engine = self._rebuilt()
            if isinstance(e, (TraceError, ObservationError)):
                raise
            if isinstance(e, (NormsEvalError, MonitorError)):
                raise ObservationError(str(e)) from None
            raise
        self._engine = eng
        self._named = self._named or regime is not None
        self._amended = self._amended or amend
        return self._changes(before, o.t)

    def end(self) -> list:
        """End the contract instance at the last observation received and
        settle its norms (F.5, item 6). Returns the changes, as `observe`.
        When the contract was not in force at that observation, the
        instance is settled there, on the clock as it stood when the
        contract left force."""
        if self._held is None or self.ended:
            return super().end()
        o, self._held = self._held, None
        obs = self._trace.observations
        n = len(obs)
        obs.append(o)
        eng = self._prepare(self._engine, n)
        before = self._marks(eng)
        eng._step(n, True)
        eng._finish_items()
        self._engine = eng
        return self._changes(before, o.t)

    @property
    def in_force(self) -> bool:
        """Whether the contract is in force in the current regime."""
        return self._clock.in_force_now

    def next_deadline(self) -> float | None:
        """As above; None while the contract is not in force, since its
        deadlines do not advance then."""
        eng = self._started()
        if eng.ended_at is not None or not self._clock.in_force_now:
            return None
        o = self._trace.observations[-1]
        due = [o.c + max(x.deadline - x.elapsed(o.c), 0) for x in self._timed(eng, o.c)]
        return float(min(due) - o.c + o.t) if due else None

    def next_check(self) -> float | None:
        """As above, on the clock of the contract; None while the contract
        is not in force."""
        eng = self._started()
        if eng.ended_at is not None or not self._clock.in_force_now:
            return None
        obs = self._trace.observations
        t, c = obs[-1].t, obs[-1].c
        exact = []
        for x in self._timed(eng, c):
            exact.append(c + max(x.deadline - x.elapsed(c), 0))
        for x in eng.norms:
            if not x.frozen and x.state == "active" and x.content.kind == "predicate":
                exact.extend(obs[k].c + self._span(eng, x.content.expr)[0] for k in x.candidates)
        for key in ("assume", "guarantee"):
            for item in eng.items[key]:
                if (item["temporal"] and not item["error"] and not item["closed"]
                        and item["value"] is None and item["start"] < len(obs)):
                    exact.append(obs[item["start"]].c + self._span(eng, item["expr"])[0])
        if not exact:
            return None
        return max(_after(min(exact) - c + t), _after(t))

    def _prepare(self, eng: Any, n: int) -> Any:
        if eng is None:
            eng = _RegimeEngine(self._contract, self._trace, self._versions, self._actors)
            self._check_parsed()
            if _STATE_IGNORED in eng.warnings:   # said in _step, see there
                eng.warnings.remove(_STATE_IGNORED)
            eng._check_state(n)
            eng.ev.known.update(self._quantities)
            return eng
        if eng.lifecycle is not None:
            eng._fill_states(n)
        eng._check_state(n)
        eng.ev.known.update(self._trace.observations[n].values)
        return eng


def replay(lines: Any, contract: str | None = None, versions: dict | None = None,
           path: str | None = None, _option: str = "contract=") -> dict:
    """As above, for a trace that may name regimes."""
    lines = list(lines)
    regimes = RegimeBindings(_REGIMES["regimes"], _REGIMES["suspended"], _REGIMES["initial"])
    trace = parse_trace_regimes(lines, path=path, bindings=regimes,
                                own_version=MODEL.get("sos_version"))
    records = [loads_strict(line) for line in lines if line.strip()]
    where = [f"line {i}" for i, line in enumerate(lines, start=1) if line.strip()]
    probe = NormsMonitor(contract, versions=versions, _option=_option)
    count = 0
    for rec, w in zip(records, where):
        probe.observe(rec, where=w)
        count += 1
        if probe.ended:
            break
    if not probe.ended and records[-1].get("end") is not False:
        probe.end()
    if not probe.started:
        raise NormsEvalError(
            f"the contract {probe.contract} is not in force at any observation of the trace, "
            f"which does not end the contract instance: there is nothing to judge")
    inside = trace.observations[:count]
    m = NormsMonitor(contract, quantities=inside[-1].values, versions=versions, _option=_option)
    m._trace.path = path
    m._warn_state = not any(o.amend for o in inside)
    for rec, w in zip(records, where):
        m.observe(rec, where=w)
    if not m.ended and records[-1].get("end") is not False:
        m.end()
    return m.result()

# <<< part 3

# >>> part 4
# === Versions while a system runs (Appendix K, K.11) =========================
# This part is in the generated module only when the file declares
# `amendment: 0.1` with an `amendment:` block. It replaces `NormsMonitor`
# and `replay` above by versions that apply the versions of a run.

def _version_infos(own: dict, versions: Any) -> list:
    """The versions of a run: this model and the models of `run_versions`
    (a dict of models, or a list), each of which carries the data of a
    version."""
    def info(model: dict) -> Any:
        m = _Model(model)
        m.regimes = model.get("regimes")
        v = VersionInfo(model["amendment"], m)
        r = model.get("regimes")
        if r is not None:
            v.regimes = RegimeBindings(r["regimes"], r["suspended"], r["initial"])
        return v

    infos = [info(own)]
    models = list(versions.values()) if isinstance(versions, dict) else list(versions or ())
    for k, model in enumerate(models):
        if not isinstance(model, dict) or not isinstance(model.get("amendment"), dict):
            raise MonitorError(
                f"run_versions[{k}] is not the MODEL of a norms monitor generated from a file "
                f"that declares amendment 0.1 with an amendment: block")
        infos.append(info(model))
    return infos


class NormsMonitor(NormsMonitor):
    __doc__ = (NormsMonitor.__doc__ or "") + """
    Versions (Appendix K, K.11). `run_versions` gives the ``MODEL`` of the
    monitor generated from each other version of the run (files that
    declare ``amendment: 0.1`` with an ``amendment:`` block); with this
    model they form one sequence by ``previous:``. `versions` keeps its
    meaning: the models that an ``amend`` key names. `period` (seconds), `origin` (the time
    stamp at which period 1 starts) and `epoch` (time stamps are seconds
    since 1970-01-01T00:00:00Z) give the time base of the effective times.
    An observation may name the version in force with the key
    ``version``. With several versions, a time base, or a named version,
    the instance follows the version in force at its first observation,
    and when a version takes effect it follows the transitional rule of
    its contract in that version: ``keep`` leaves it as it is, ``switch``
    amends it (F.6), and ``switch`` with ``in_states`` amends it at the
    first observation in one of those states. Such a monitor refuses an
    observation with ``amend``; once an ``amend`` has been applied, it
    refuses an observation that names a version. When a version of the run
    binds regimes, or an observation names one, the regime and the
    versions combine as `cadl norms-eval` combines them (K.11, "Versions
    and regimes", from cadl 0.3.35): the regime named at an observation is
    current from it on, then the versions take effect; the contract is in
    force by the bindings of the version in force; and a switch made while
    the contract is not in force reaches the instance at the next
    observation at which it is.
    """

    def __init__(self, contract: str | None = None, bindings: dict | None = None,
                 request: dict | None = None, quantities: Any = None,
                 versions: dict | None = None, _option: str = "contract=",
                 period: Any = None, origin: Any = 0, epoch: bool = False,
                 run_versions: Any = None) -> None:
        super().__init__(contract, bindings, request, quantities, versions, _option)
        self._init_bindings = copy.deepcopy(self._trace.bindings)
        self._init_request = copy.deepcopy(self._trace.request)
        base = TimeBase(period, origin, epoch)
        self._chain = VersionChain(_version_infos(MODEL, run_versions), base)
        self._active = len(self._chain.infos) > 1 or self._chain.timed
        self._tracker: Any = None
        self._next_named: Any = None
        self._named_regime = False
        self._amended = False        # an `amend` has been applied
        # An `amend` to a version whose transitional rule is not a plain
        # switch is applied as a switch; the monitor says so (K.11).
        self.amend_warnings: list = []
        lookup = self._versions
        cid = self._contract.id

        def warned(name: str) -> Any:
            model = lookup.models.get(name)
            data = model.get("amendment") if isinstance(model, dict) else None
            if isinstance(data, dict):
                rule, states = VersionInfo(data).rule_for(cid)
                if rule == "keep" or states:
                    what = "keep" if rule == "keep" else f"switch in {', '.join(states)}"
                    note = (f"'amend' to {name} amends {cid}, but the transitional rule of "
                            f"that version for {cid} is {what} (Appendix K, K.4): the "
                            f"amendment is applied as a switch (K.11)")
                    if note not in self.amend_warnings:
                        self.amend_warnings.append(note)
            return lookup(name)
        warned.models = lookup.models
        self._versions = warned
        # versions and regime bindings (K.11, from cadl 0.3.35)
        self._names = chain_regimes(self._chain)
        self._raw: list = []         # every observation given, for a rebuild
        self._run: Any = None
        self._combined = False
        if self._active and self._names is not None:
            self._combine()

    def _combine(self) -> None:
        """From now on the monitor runs the versions with the regime
        bindings, as `run_version_regime_trace` of `cadl norms-eval`."""
        self._combined = True
        self._run = _RegimeRun(self._chain, self._contract.id)
        self._clock = self._run
        self._held = None
        self._left_out = 0

    @property
    def version(self) -> str | None:
        """The version that the contract instance follows, or None before
        its first observation (or when no version applies)."""
        if self._tracker is None or self._tracker.follows is None:
            return None
        return self._chain.infos[self._tracker.follows].name

    def observe(self, observation: dict, where: str | None = None) -> list:
        named = None
        raw = copy.deepcopy(observation) if isinstance(observation, dict) else observation
        if isinstance(observation, dict):
            observation = copy.deepcopy(observation)
            n = len(self._trace.observations) + self._ignored + getattr(self, "_left_out", 0)
            spot = where or f"observation {n + 1}"
            if VERSION_KEY in observation and not self.ended:
                named = self._chain.check_name(observation[VERSION_KEY], spot)
            observation = {k: v for k, v in observation.items() if k != VERSION_KEY}
            if not self.ended:
                regime = observation.get("regime") is not None
                active = self._active or named is not None
                if active and observation.get("amend") is not None:
                    raise ObservationError(_VERSIONS_NO_AMEND)
                if named is not None and not self._active:
                    if self._amended:
                        raise ObservationError(
                            "an observation with 'amend' has amended the contract, so the "
                            "trace cannot name a version (Appendix K, K.11)")
                    self._activate(regime)
        if self._combined:
            changes = self._observe_combined(observation, named, where)
            self._raw.append((raw, where))
            return changes
        self._next_named = named
        before = (self._named_regime, self._tracker.save() if self._tracker else None)
        try:
            changes = super().observe(observation, where)
        except Exception:
            self._named_regime = before[0]
            if self._tracker is not None and before[1] is not None:
                self._tracker.restore(before[1])
            raise
        if isinstance(observation, dict) and observation.get("regime") is not None:
            self._named_regime = True
        if isinstance(observation, dict) and observation.get("amend") is not None \
                and not self.ended:
            self._amended = True
        self._raw.append((raw, where))
        return changes

    def _activate(self, regime: bool = False) -> None:
        """Apply the versions from now on, as from the first observation
        (`cadl norms-eval` does so for a trace that names a version)."""
        combine = self._names is not None and (
            regime or self._named_regime or any(i.regimes is not None for i in self._chain.infos))
        if combine:
            saved = self.__dict__.copy()
            try:
                self._active = True
                self._replay_combined()
            except Exception:
                self.__dict__.clear()
                self.__dict__.update(saved)
                raise
            return
        self._active = True
        if self._engine is not None:
            try:
                self._engine = self._rebuilt()
            except Exception:
                self._active = False
                self._engine = self._rebuilt()
                raise

    # --- versions with regime bindings (K.11, from cadl 0.3.35) --------------------

    def _replay_combined(self) -> None:
        """Process again, in the combined mode, every observation given so
        far (they were accepted before)."""
        raw = list(self._raw)
        self._trace.observations[:] = []
        self._reader = _TraceReader(self._trace, MODEL.get("sos_version"))
        self._trace.bindings, self._trace.request = (copy.deepcopy(self._init_bindings),
                                                     copy.deepcopy(self._init_request))
        self._engine = None
        self._ignored = 0
        self._tracker = None
        self._raw = []
        self._combine()
        for rec, where in raw:
            if isinstance(rec, dict):
                named = rec.get(VERSION_KEY)
                rec = {k: v for k, v in rec.items() if k != VERSION_KEY}
                named = None if named is None else self._chain.check_name(named, where or "")
            else:
                named = None
            self._observe_combined(copy.deepcopy(rec), named, where)
            self._raw.append((rec if named is None else dict(rec, version=named), where))

    def _observe_combined(self, observation: Any, named: Any, where: str | None) -> list:
        obs = self._trace.observations
        n = len(obs)
        where = where or f"observation {n + self._ignored + self._left_out + 1}"
        observation, regime = split_regime(observation, where, self._names)
        reader = self._reader
        saved = (dict(reader.snapshot), reader.state, reader.prev_t,
                 self._trace.bindings, self._trace.request)
        run, held, left_out = self._run.save(), self._held, self._left_out
        o = reader.add(observation, where)
        o.regime, o.version = regime, named
        if self.ended:
            # not part of the instance (F.5): only the order of time goes on
            obs.pop()
            reader.snapshot, reader.state = saved[0], saved[1]
            self._ignored += 1
            return []
        touched = False
        try:
            o.amend = None
            inside, o.c = self._run.step(o.t, named, regime)
            o.out = not inside
            eng = self._engine
            contract = eng.contract if eng is not None else self._run.contract_of(self._run.k)
            if contract is None:
                raise ObservationError(
                    f"version {self._chain.infos[self._run.k].name}, in force at the start of "
                    f"the instance, has no contract '{self._contract.id}'")
            member = inside
            if not inside:
                _regime_check_state(o, contract)
                member = _regime_ends_at(o, contract.lifecycle, False)
            self._held = None
            names, events = self._run.instance(n + self._left_out, o.t,
                                               _state_before(o, contract.lifecycle), member)
            if not member:
                obs.pop()
                self._held = o
                self._left_out += 1
                return []
            o.amend = tuple(names) or None
            o.version_events = events
            eng = self._prepare_combined(eng, n)
            before = self._marks(eng)
            touched = True
            self._step_combined(eng, n, False)
        except Exception as e:
            if obs and obs[-1] is o:
                obs.pop()
            (reader.snapshot, reader.state, reader.prev_t,
             self._trace.bindings, self._trace.request) = saved
            self._run.restore(run)
            self._held, self._left_out = held, left_out
            if touched:
                self._engine = self._rebuilt_combined()
            if isinstance(e, (TraceError, ObservationError)):
                raise
            if isinstance(e, (NormsEvalError, MonitorError)):
                raise ObservationError(str(e)) from None
            raise
        self._engine = eng
        self._tracker = eng.tracker
        self._named_regime = self._named_regime or regime is not None
        return self._changes(before, o.t)

    def _prepare_combined(self, eng: Any, n: int) -> Any:
        o = self._trace.observations[n]
        if eng is None:
            start = self._chain.infos[self._run.k]
            eng = _VersionRegimeEngine(self._run.contract_of(self._run.k), self._trace,
                                       self._chain, self._run, declared_actors(start.sos))
            self._check_parsed()
            if _STATE_IGNORED in eng.warnings:   # said in _step, see there
                eng.warnings.remove(_STATE_IGNORED)
            eng.ev.known.update(self._quantities)
        elif eng.lifecycle is not None:
            eng._fill_states(n)
        eng.ev.known.update(o.values)
        return eng

    def _step_combined(self, eng: Any, n: int, last: bool) -> None:
        o = self._trace.observations[n]
        eng.version_events[n] = getattr(o, "version_events", [])
        eng._step(n, last)
        self._run.frozen = _seen_state(o, eng.lifecycle)
        self._check_parsed()
        if (self._warn_state and eng.lifecycle is None and o.state_after is not None
                and _STATE_IGNORED not in eng.warnings):
            eng.warnings.insert(0, _STATE_IGNORED)
        eng._finish_items()

    def _rebuilt_combined(self) -> Any:
        """The engine of the observations of the projection kept so far,
        processed again (their amendments are on them)."""
        obs = self._trace.observations
        kept = obs[:]
        del obs[:]
        eng = None
        for n, o in enumerate(kept):
            obs.append(o)
            eng = self._prepare_combined(eng, n)
            self._step_combined(eng, n, False)
        return eng

    def end(self) -> list:
        if not self._combined or self._held is None or self.ended:
            return super().end()
        o, self._held = self._held, None
        names, events = self._run.flush()
        o.amend = tuple(names) or None
        o.version_events = events
        obs = self._trace.observations
        n = len(obs)
        obs.append(o)
        eng = self._prepare_combined(self._engine, n)
        before = self._marks(eng)
        self._step_combined(eng, n, True)
        self._engine = eng
        self._tracker = eng.tracker
        return self._changes(before, o.t)

    @property
    def in_force(self) -> bool:
        """Whether the contract is in force at the last observation."""
        if self._combined:
            return self._run.in_force_now
        clock = getattr(self, "_clock", None)
        return True if clock is None else clock.in_force_now

    def next_deadline(self) -> float | None:
        if not self._combined:
            return super().next_deadline()
        eng = self._started()
        if eng.ended_at is not None or not self._run.in_force_now:
            return None
        o = self._trace.observations[-1]
        due = [o.c + max(x.deadline - x.elapsed(o.c), 0) for x in self._timed(eng, o.c)]
        return float(min(due) - o.c + o.t) if due else None

    def next_check(self) -> float | None:
        if not self._combined:
            return super().next_check()
        eng = self._started()
        if eng.ended_at is not None or not self._run.in_force_now:
            return None
        obs = self._trace.observations
        t, c = obs[-1].t, obs[-1].c
        exact = []
        for x in self._timed(eng, c):
            exact.append(c + max(x.deadline - x.elapsed(c), 0))
        for x in eng.norms:
            if not x.frozen and x.state == "active" and x.content.kind == "predicate":
                exact.extend(obs[k].c + self._span(eng, x.content.expr)[0] for k in x.candidates)
        for key in ("assume", "guarantee"):
            for item in eng.items[key]:
                if (item["temporal"] and not item["error"] and not item["closed"]
                        and item["value"] is None and item["start"] < len(obs)):
                    exact.append(obs[item["start"]].c + self._span(eng, item["expr"])[0])
        if not exact:
            return None
        return max(_after(min(exact) - c + t), _after(t))

    def result(self) -> dict:
        result = super().result()
        if self._active and self._tracker is not None:
            result["versions"] = versions_of(self._tracker)
        return result

    def _prepare(self, eng: Any, n: int) -> Any:
        o = self._trace.observations[n]
        if not hasattr(o, "version"):
            o.version = self._next_named
        if not self._active:
            return super()._prepare(eng, n)
        if eng is None:
            tracker = VersionTracker(self._chain, self._contract.id)
            start = self._chain.infos[tracker.start(o.t, o.version)]
            contract = next((c for c in start.sos.contracts if c.id == self._contract.id), None)
            if contract is None:
                raise ObservationError(f"version {start.name}, in force at the start of the "
                                       f"instance, has no contract '{self._contract.id}'")
            o.amend = None
            eng = _VersionEngine(contract, self._trace, self._chain, tracker,
                                 declared_actors(start.sos))
            self._tracker = tracker
            self._check_parsed()
            if _STATE_IGNORED in eng.warnings:   # said in _step, see there
                eng.warnings.remove(_STATE_IGNORED)
            eng._check_state(n)
            eng.ev.known.update(self._quantities)
            return eng
        self._tracker = eng.tracker
        if eng.lifecycle is not None:
            eng._fill_states(n)
        eng.decide(n)            # sets o.amend; the events are logged by its _step
        if not o.amend:
            eng._check_state(n)
        eng.ev.known.update(o.values)
        return eng

    def _rebuilt(self) -> Any:
        eng = super()._rebuilt()
        self._tracker = None if eng is None else getattr(eng, "tracker", None)
        return eng


def replay(lines: Any, contract: str | None = None, versions: dict | None = None,
           path: str | None = None, _option: str = "contract=", period: Any = None,
           origin: Any = 0, epoch: bool = False, run_versions: Any = None) -> dict:
    """As above, for a run that may have versions (Appendix K, K.11)."""
    lines = list(lines)
    kw = {"versions": versions, "_option": _option, "period": period, "origin": origin,
          "epoch": epoch, "run_versions": run_versions}
    probe = NormsMonitor(contract, **kw)
    trace = parse_trace_versions(lines, path=path, chain=probe._chain,
                                 bindings=chain_regimes(probe._chain))
    records = [loads_strict(line) for line in lines if line.strip()]
    where = [f"line {i}" for i, line in enumerate(lines, start=1) if line.strip()]
    count = 0
    for rec, w in zip(records, where):
        probe.observe(rec, where=w)
        count += 1
        if probe.ended:
            break
    if not probe.ended and records[-1].get("end") is not False:
        probe.end()
    if not probe.started:
        raise NormsEvalError(
            f"the contract {probe.contract} is not in force at any observation of the trace, "
            f"which does not end the contract instance: there is nothing to judge")
    inside = trace.observations[:count]
    m = NormsMonitor(contract, quantities=inside[-1].values, **kw)
    m._trace.path = path
    m._warn_state = not any(o.amend for o in inside)
    for rec, w in zip(records, where):
        m.observe(rec, where=w)
    if not m.ended and records[-1].get("end") is not False:
        m.end()
    return m.result()


def main(argv: list | None = None) -> int:
    """As above, with --period, --origin and --epoch for the time base of
    the versions (Appendix K, K.11); give the other versions with
    --model-version NAME=MODEL.json."""
    import argparse
    ap = argparse.ArgumentParser(
        prog="norms_monitor",
        description="Replay a recorded trace of one contract instance through the "
                    "generated norms monitor (norms 0.1).")
    ap.add_argument("trace", nargs="?", help="trace file (JSON Lines)")
    ap.add_argument("--contract", help="contract id, when the file has several")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    ap.add_argument("--model-version", action="append", default=[], metavar="NAME=MODEL.json",
                    help="the model of a new version of the file, for a line with "
                         "\"amend\": \"NAME\" (write it with --dump-model of the monitor "
                         "generated from that version)")
    ap.add_argument("--run-version", action="append", default=[], metavar="MODEL.json",
                    help="the model of another version of the run (Appendix K, K.11); "
                         "may be repeated")
    ap.add_argument("--period", default=None, metavar="SECONDS",
                    help="length of a period, for effective: { period: k }")
    ap.add_argument("--origin", default="0", metavar="SECONDS",
                    help="time stamp at which period 1 starts (default: 0)")
    ap.add_argument("--epoch", action="store_true",
                    help="time stamps are seconds since 1970-01-01T00:00:00Z")
    ap.add_argument("--dump-model", action="store_true",
                    help="print the model of this monitor as JSON and exit")
    args = ap.parse_args(argv)
    if args.dump_model:
        print(json.dumps(MODEL, ensure_ascii=True, allow_nan=False))
        return 0
    if args.trace is None:
        ap.error("the trace file is required")
    versions = {}
    try:
        for spec in args.model_version:
            name, sep, file = spec.partition("=")
            if not sep:
                raise NormsEvalError(f"--model-version needs NAME=MODEL.json, not '{spec}'")
            with open(file, encoding="utf-8") as f:
                versions[name] = json.load(f)
        run_versions = []
        for file in args.run_version:
            with open(file, encoding="utf-8") as f:
                run_versions.append(json.load(f))
        try:
            period = None if args.period is None else Fraction(Decimal(args.period))
            origin = Fraction(Decimal(args.origin))
        except (ArithmeticError, ValueError):
            raise NormsEvalError("--period and --origin are finite numbers of seconds") from None
        try:
            with open(args.trace, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            raise NormsEvalError(f"File not found: {args.trace}") from None
        result = replay(lines, args.contract, versions, path=str(args.trace),
                        _option="--contract", period=period, origin=origin, epoch=args.epoch,
                        run_versions=run_versions)
    except TraceError as e:
        print(f"Trace error: {args.trace}: {e}", file=sys.stderr)
        return 2
    except (NormsEvalError, MonitorError, OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_text_versions(result, format_text(result)))
    return 0 if result["ok"] else 1

# <<< part 4
