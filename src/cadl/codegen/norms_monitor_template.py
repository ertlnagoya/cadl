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


class _Model:
    """The contracts of one version of the file."""

    def __init__(self, model: dict) -> None:
        if not isinstance(model, dict) or model.get("norms_version") != "0.1":
            raise MonitorError("not a model of a norms 0.1 monitor")
        for kind, table in model["parsed"].items():
            _PARSED[kind].update(table)
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
    norms-eval` trace: ``t`` (seconds, required, non-decreasing),
    ``values`` (updates of the snapshot), ``messages``, ``state`` (the
    lifecycle state after this observation) and ``end``. ``bindings`` and
    ``request`` may be given to the constructor or in the first
    observation.

    `contract` names the contract; it may be left out when the file has
    one contract with ``norms:`` or ``counts_as:``. `quantities` names the
    observed quantities that are written without an actor (``load``, not
    ``ROBOT[1].load``) and get their first value after the first use of a
    predicate: until then such a name is undefined, while an undeclared
    one is read as an enum-like literal (``status == Delivered``). `versions` maps the name
    used by an ``amend`` key to the ``MODEL`` of the monitor generated
    from the new version of the file (F.6); without it an observation with
    ``amend`` is refused.

    The monitor decides with the observations it has received. A deadline
    that passes is detected at the first observation after it, so a host
    with nothing to report sends a time-only observation ``{"t": ...}``
    (see `next_deadline`). The monitor keeps the observations of its
    instance; make one monitor per contract instance and drop it when the
    instance has ended. It is not thread-safe.
    """

    def __init__(self, contract: str | None = None, bindings: dict | None = None,
                 request: dict | None = None, quantities: Any = None,
                 versions: dict | None = None) -> None:
        self._contract = select_contract(_Model(MODEL), contract)
        self._trace = Trace(observations=[], bindings=dict(bindings or {}),
                            request=dict(request or {}))
        self._reader = _TraceReader(self._trace)
        self._quantities = {_canon_actor(q) for q in (quantities or ())}
        self._versions = dict(versions or {})
        self._warn_state = not self._versions
        self._engine: Any = None
        self._ignored = 0
        self._broken: str | None = None

    # --- input ----------------------------------------------------------------

    def observe(self, observation: dict, where: str | None = None) -> list:
        """Process one observation and return the changes it made: a list
        of dicts, one per norm instance whose state or verdict changed and
        per ``assume:`` / ``guarantee:`` item that was decided (see
        `changes` in the module documentation). A malformed observation
        raises `TraceError` and leaves the monitor as it was. Observations
        after the end of the instance are ignored."""
        self._usable()
        obs = self._trace.observations
        n = len(obs)
        reader = self._reader
        saved = (dict(reader.snapshot), reader.state, reader.prev_t,
                 self._trace.bindings, self._trace.request)
        o = reader.add(observation, where or f"observation {n + 1}")
        eng = self._engine
        try:
            if eng is None:
                eng = _Engine(self._contract, self._trace, self._load_version)
                self._check_parsed()
            elif eng.lifecycle is not None:
                eng._fill_states(n)
            if not o.amend and eng.ended_at is None:
                eng._check_state(n)
        except Exception:
            obs.pop()
            (reader.snapshot, reader.state, reader.prev_t,
             self._trace.bindings, self._trace.request) = saved
            raise
        if self._engine is None:
            self._engine = eng
            eng.ev.known.update(self._quantities)
        eng.ev.known.update(o.values)
        if (self._warn_state and eng.lifecycle is None and o.state_after is not None
                and _STATE_IGNORED not in eng.warnings):
            eng.warnings.insert(0, _STATE_IGNORED)
        if eng.ended_at is not None:
            self._ignored += 1
            return []
        before = self._marks()
        try:
            eng._step(n, False)
            self._check_parsed()
        except Exception as e:
            self._broken = str(e)
            raise
        # Also when the instance goes on: it gives a decided guarantee its
        # verdict and the undecided items the reason of a prefix.
        eng._finish_items()
        return self._changes(before, o.t)

    def end(self) -> list:
        """End the contract instance at the last observation received and
        settle its norms (F.5, item 6). Returns the changes, as `observe`.
        To end the instance at a later time, send an observation with
        ``"end": True`` instead."""
        self._usable()
        eng = self._engine
        if eng is None:
            raise MonitorError("the contract instance has no observation")
        if eng.ended_at is not None:
            return []
        n = len(self._trace.observations) - 1
        before = self._marks()
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
        or the window of an active prohibition is over, or None. The
        monitor judges it at the first observation after that time. The
        intervals of temporal operators are not included."""
        eng = self._started()
        if eng.ended_at is not None:
            return None
        t = self._trace.observations[-1].t
        due = []
        for x in eng.norms:
            if (x.state == "active" and not x.frozen and x.deadline is not None
                    and x.d.kind in ("obligation", "prohibition")):
                left = x.deadline - x.elapsed(t)
                if left >= -_EPS:
                    due.append(t + max(left, 0.0))
        return min(due) if due else None

    # --- internals ----------------------------------------------------------------

    def _usable(self) -> None:
        if self._broken is not None:
            raise MonitorError(f"the monitor stopped at an error: {self._broken}")

    def _started(self) -> Any:
        self._usable()
        if self._engine is None:
            raise MonitorError("the contract instance has no observation")
        return self._engine

    def _check_parsed(self) -> None:
        if _PARSE_MISSES:
            missing = ", ".join(repr(x) for x in _PARSE_MISSES)
            del _PARSE_MISSES[:]
            raise MonitorError(f"the model has no parsed form of {missing}; generate the "
                               f"monitors of all versions with the same cadl")

    def _load_version(self, name: str) -> Any:
        if name not in self._versions:
            raise NormsEvalError(
                f"the monitor has no version '{name}' of the contract; give the MODEL of the "
                f"monitor generated from it with versions=")
        return _Model(self._versions[name])

    @staticmethod
    def _item_mark(key: str, item: dict) -> str:
        if key == "guarantee":
            return item["verdict"]
        v = item["value"]
        return "pending" if v in (None, "undefined") else "holds" if v else "failed"

    def _marks(self) -> dict:
        eng = self._engine
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
           path: str | None = None) -> dict:
    """Feed a recorded trace (an iterable of JSON Lines, as for `cadl
    norms-eval`) to a new monitor, one observation at a time, and return
    its result. As `cadl norms-eval` does, the names of the quantities are
    taken from the whole trace, and the instance ends at the last line
    unless that line says ``"end": false``."""
    lines = list(lines)
    trace = parse_trace(lines, path=path)        # the whole trace is well-formed
    names = set()
    for o in trace.observations:
        names.update(o.values)
    m = NormsMonitor(contract, quantities=names, versions=versions)
    m._trace.path = path
    m._warn_state = not any(o.amend for o in trace.observations)
    rec: Any = None
    for lineno, line in enumerate(lines, start=1):
        if line.strip():
            rec = json.loads(line)
            m.observe(rec, where=f"line {lineno}")
    if not m.ended and rec.get("end") is not False:
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
    ap.add_argument("--version", action="append", default=[], metavar="NAME=MODEL.json",
                    help="the model of a new version of the file, for a line with "
                         "\"amend\": \"NAME\" (write it with --dump-model of the monitor "
                         "generated from that version)")
    ap.add_argument("--dump-model", action="store_true",
                    help="print the model of this monitor as JSON and exit")
    args = ap.parse_args(argv)
    if args.dump_model:
        print(json.dumps(MODEL, ensure_ascii=True))
        return 0
    if args.trace is None:
        ap.error("the trace file is required")
    versions = {}
    try:
        for spec in args.version:
            name, sep, file = spec.partition("=")
            if not sep:
                raise NormsEvalError(f"--version needs NAME=MODEL.json, not '{spec}'")
            with open(file, encoding="utf-8") as f:
                versions[name] = json.load(f)
        try:
            with open(args.trace, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            raise NormsEvalError(f"File not found: {args.trace}") from None
        result = replay(lines, args.contract, versions, path=str(args.trace))
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
