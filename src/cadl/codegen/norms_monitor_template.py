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
        self._reader = _TraceReader(self._trace)
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
        observation after it (more than 1e-9 s after it, the tolerance of
        all comparisons of times). To know when to observe, use
        `next_check`."""
        eng = self._started()
        if eng.ended_at is not None:
            return None
        t = self._trace.observations[-1].t
        due = [t + max(x.deadline - x.elapsed(t), 0.0) for x in self._timed(eng, t)]
        return min(due) if due else None

    def next_check(self) -> float | None:
        """The earliest time stamp at which an observation is certain to
        decide something that now only waits for time: a deadline of an
        active obligation that has passed, the window of an active
        prohibition that has closed, or a temporal operator (in an
        ``assume:`` / ``guarantee:`` item or in the content of a norm)
        whose interval is over. None when nothing waits for time, and
        after the end.

        A host with nothing else to report sends ``{"t": m.next_check()}``
        when that time has come. The time is later than the deadline or
        the end of the interval by a little more than the tolerance of
        1e-9 s (and by at least one step of the floating-point numbers at
        that time), because an observation at the deadline itself is still
        in time. For nested temporal operators it is the time at which
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
            c = t + max(x.deadline - x.elapsed(t), 0.0) + 2 * _EPS
            for _ in range(64):
                if x.elapsed(c) > x.deadline + _EPS:
                    break
                c = math.nextafter(c, math.inf)
            times.append(c)
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
        return max(min(times), math.nextafter(t, math.inf))

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
                and x.elapsed(t) <= x.deadline + _EPS]

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
        spans = [cls._span(eng, p) for p in parts] or [(0.0, 0)]
        return max(s[0] for s in spans), max(s[1] for s in spans)

    @classmethod
    def _decided_after(cls, eng: Any, start: float, expr: Any) -> float:
        """A time stamp after every interval of `expr` evaluated at `start`."""
        span, depth = cls._span(eng, expr)
        c = start + span + (depth + 1) * _EPS
        for _ in range(4):                      # rounding of the sums of times
            c = math.nextafter(c, math.inf)
        return c

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

    def _changes(self, before: dict, t: float) -> list:
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
    trace = parse_trace(lines, path=path)        # the whole trace is well-formed
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
