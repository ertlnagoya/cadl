"""Regime bindings at run time (Appendix A, A.7.1, "Runs"; Appendix F,
F.5, "Contracts not in force"), cadl 0.3.33.

- `cadl norms-eval` and the norms monitor of the Python target read the
  key ``regime`` of an observation and judge a contract instance on the
  projection of the trace: the observations at which the contract is in
  force, with deadlines and intervals on the clock of the contract;
- the generated Solidity and Rego read the current regime;
- the targets that cannot read it refuse what the bindings would change;
- `cadl regime-map` shows the bindings.
"""

from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import textwrap
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from cadl.cli import main
from cadl.codegen import generate, norms_monitors
from cadl.codegen.norms_monitor_gen import generate_norms_monitor_module
from cadl.norms_eval import (
    NormsEvalError, TraceError, _Engine, evaluate, parse_trace, parse_trace_regimes,
    regime_bindings, select_contract,
)
from cadl.parser import parse, parse_file
from cadl.regime_map import RegimeMap
from cadl.regimes import initial_regime
from cadl.type_checker import type_check

from tests import norms_targets_support as S
from tests.test_norms_monitor import (
    _NO_CADL, _RICH, _ROBOT, EXAMPLE, RICH, _isolated, _memoize_parser, _random_trace,
    _write_package,
)
from tests.test_regime_bindings import BASE, GOOD

ROOT = Path(__file__).parent.parent
TRACES = ROOT / "examples" / "traces"
HERE = Path(__file__).parent

REGIMES = textwrap.dedent("""
      transitions:
        - { from: NORMAL, to: STORM, condition: "wind >= 20" }
        - { from: STORM, to: NORMAL, condition: "wind < 20" }

      regimes:
        STORM:
          contracts: { suspended: [%s] }
    """)


def _with_regimes(path: Path, contract: str) -> str:
    """The file with two regimes; STORM does not have `contract` in force."""
    text = path.read_text(encoding="utf-8").rstrip("\n") + "\n"
    return text + textwrap.indent(REGIMES % contract, "  ")


@pytest.fixture(scope="module")
def robot_sos():
    sos = parse(_with_regimes(EXAMPLE, "DELIVERY_SLA"))
    assert type_check(sos).ok
    return sos


@pytest.fixture(scope="module")
def rich_sos():
    sos = parse(_with_regimes(RICH, "C"))
    assert type_check(sos).ok
    return sos


def _eval(sos, lines: list, contract=None) -> dict:
    trace = parse_trace_regimes(lines, bindings=regime_bindings(sos))
    return json.loads(json.dumps(evaluate(sos, trace, contract)))


def _lines(*records) -> list:
    return [json.dumps(r) for r in records]


ASSIGN = {"from": "DISPATCHER", "to": "ROBOT[2]", "name": "route_assignment"}
ACK = {"from": "ROBOT[2]", "to": "DISPATCHER", "name": "ack", "args": ["accepted"]}
START = {"t": 0, "bindings": {"i": 2}, "values": {
    "DISPATCHER.is_operational": True, "ROBOT[2].battery": 80, "ROBOT[2].status": "Idle"}}


def _norm(result: dict, nid: str) -> dict:
    return next(n for n in result["norms"] if n["id"] == nid)


# === cadl norms-eval ================================================================

class TestTracesWithoutRegime:
    @pytest.mark.parametrize("path", sorted(TRACES.glob("norms_robot_delivery_*.jsonl")),
                             ids=lambda p: p.stem)
    def test_judged_as_before(self, path, robot_sos):
        """A trace that names no regime: the result of the file without
        regimes:, also through `cadl norms-eval` (which reads the trace with
        parse_trace_regimes for a file with the block)."""
        lines = path.read_text(encoding="utf-8").splitlines()
        plain = parse_file(EXAMPLE)
        want = json.loads(json.dumps(evaluate(plain, parse_trace(lines))))
        assert _eval(robot_sos, lines) == want

    @pytest.mark.parametrize("path", sorted(TRACES.glob("norms_robot_delivery_*.jsonl")),
                             ids=lambda p: p.stem)
    def test_in_force_throughout(self, path, robot_sos):
        """A trace that names only a regime that has the contract in force."""
        lines = path.read_text(encoding="utf-8").splitlines()
        named = [json.dumps(dict(json.loads(l), regime="NORMAL")) for l in lines if l.strip()]
        want = json.loads(json.dumps(evaluate(parse_file(EXAMPLE), parse_trace(lines))))
        assert _eval(robot_sos, named) == want


class TestProjection:
    def test_deadline_clock_stops_while_not_in_force(self, robot_sos):
        r = _eval(robot_sos, _lines(
            dict(START, regime="NORMAL"),
            {"t": 1, "messages": [ASSIGN], "state": "Assigned"},
            {"t": 3, "regime": "STORM"},              # 2 s of the 5 s used
            {"t": 60, "regime": "NORMAL"},
            {"t": 62.5, "messages": [ACK]},           # 4.5 s on the clock
            {"t": 70, "end": True}))
        ack = _norm(r, "acknowledge")
        assert (ack["state"], ack["time"]) == ("fulfilled", 62.5)
        assert r["instance"]["observations"] == 5

    def test_deadline_passes_after_the_return(self, robot_sos):
        r = _eval(robot_sos, _lines(
            dict(START, regime="NORMAL"),
            {"t": 1, "messages": [ASSIGN], "state": "Assigned"},
            {"t": 3, "regime": "STORM"},
            {"t": 60, "regime": "NORMAL"},
            {"t": 66, "messages": [ACK]}))
        ack = _norm(r, "acknowledge")
        assert ack["state"] == "violated" and ack["time"] == 66
        # reported on the time of the trace: 2 s before the storm, 3 s after
        assert ack["reason"] == "deadline of 5s passed at t=63 without the content"
        assert r["violations"][0]["t"] == 66

    def test_messages_while_not_in_force_are_not_seen(self, robot_sos):
        r = _eval(robot_sos, _lines(
            dict(START, regime="STORM"),
            {"t": 1, "messages": [ASSIGN], "state": "Assigned"},
            {"t": 2, "regime": "NORMAL"},
            {"t": 20, "end": True}))
        assert _norm(r, "acknowledge")["state"] == "discharged"
        assert _norm(r, "acknowledge")["activated_at"] is None
        # the instance starts at its first observation in force
        assert r["timeline"][0] == {"t": 2.0, "kind": "start",
                                    "text": "contract instance started, state Assigned"}

    def test_values_given_while_not_in_force_are_seen_on_return(self, robot_sos):
        r = _eval(robot_sos, _lines(
            dict(START, regime="NORMAL"),
            {"t": 5, "regime": "STORM",
             "values": {"ROBOT[2].battery": 5, "ROBOT[2].status": "InTransit"}},
            {"t": 9, "regime": "NORMAL"},
            {"t": 10, "end": True}))
        n = _norm(r, "no_low_battery_driving")
        assert (n["state"], n["time"]) == ("violated", 9)

    def test_window_of_a_temporal_item_on_the_clock(self, robot_sos):
        # within(300s, ROBOT[i].status == Delivered): 1000 s of storm do not count
        r = _eval(robot_sos, _lines(
            dict(START, regime="NORMAL"),
            {"t": 100, "regime": "STORM"},
            {"t": 1100, "regime": "NORMAL"},
            {"t": 1250, "values": {"ROBOT[2].status": "Delivered"}},
            {"t": 1300, "end": True}))
        assert r["guarantees"][0]["verdict"] == "fulfilled"

    def test_end_while_not_in_force_is_settled_on_the_stopped_clock(self, robot_sos):
        r = _eval(robot_sos, _lines(
            dict(START, regime="NORMAL"),
            {"t": 1, "messages": [ASSIGN], "state": "Assigned"},
            {"t": 3, "regime": "STORM"},
            {"t": 90, "end": True}))
        ack = _norm(r, "acknowledge")
        assert (ack["state"], ack["reason"]) == ("discharged",
                                                 "contract instance ended before the deadline")
        assert r["instance"]["end_reason"] == ("end of the instance in the trace, while the "
                                               "contract is not in force")
        assert r["instance"]["end"] == 90

    def test_deadline_passed_before_leaving_force(self, robot_sos):
        r = _eval(robot_sos, _lines(
            dict(START, regime="NORMAL"),
            {"t": 1, "messages": [ASSIGN], "state": "Assigned"},
            {"t": 7, "regime": "STORM"},              # the deadline (t=6) passed in force
            {"t": 8}))                                # last line: the instance ends
        ack = _norm(r, "acknowledge")
        assert (ack["state"], ack["reason"]) == (
            "violated", "contract instance ended after the deadline without the content")
        assert r["instance"]["end_reason"] == ("last observation of the trace, while the "
                                               "contract is not in force")

    def test_terminal_state_while_not_in_force_ends_the_instance(self, robot_sos):
        r = _eval(robot_sos, _lines(
            dict(START, regime="NORMAL"),
            {"t": 1, "regime": "STORM", "state": "Completed"},
            {"t": 5, "regime": "NORMAL"}))
        assert r["instance"]["end_reason"] == ("terminal state Completed, while the contract "
                                               "is not in force")
        assert any("1 observation(s) after the end" in w for w in r["warnings"])

    def test_never_in_force(self, robot_sos):
        r = _eval(robot_sos, _lines(dict(START, regime="STORM"), {"t": 5, "end": True}))
        assert {n["state"] for n in r["norms"]} == {"discharged"}
        assert r["instance"]["observations"] == 1
        with pytest.raises(NormsEvalError, match="not in force at any observation"):
            _eval(robot_sos, _lines(dict(START, regime="STORM"), {"t": 5, "end": False}))

    def test_before_the_first_regime_the_run_is_in_the_initial_regime(self):
        """A.7.1: a trace that names no regime is in the initial regime
        (NORMAL here), which does not have the contract in force; naming it
        gives the same result."""
        text = _with_regimes(EXAMPLE, "DELIVERY_SLA").replace("STORM:\n", "NORMAL:\n")
        sos = parse(text)
        assert regime_bindings(sos).initial == "NORMAL"
        recs = [START, {"t": 1, "messages": [ASSIGN], "state": "Assigned"}, {"t": 10, "end": True}]
        plain = _eval(sos, _lines(*recs))
        named = _eval(sos, _lines(dict(START, regime="NORMAL"), *recs[1:]))
        assert plain == named
        assert _norm(plain, "acknowledge")["activated_at"] is None
        assert plain["instance"]["observations"] == 1
        # an amendment is refused for such a contract, also without `regime`
        with pytest.raises(NormsEvalError, match="cannot also amend"):
            _eval(sos, _lines(START, {"t": 1, "amend": "v2.cadl"}))

    def test_only_observation_is_an_end_while_not_in_force(self, robot_sos):
        """The start activations happen there too, as for an instance that
        is in force at its only observation."""
        out = _eval(robot_sos, _lines(dict(START, regime="STORM", end=True)))
        inside = _eval(robot_sos, _lines(dict(START, regime="NORMAL", end=True)))
        assert [(n["id"], n["state"], n["reason"]) for n in out["norms"]] == \
               [(n["id"], n["state"], n["reason"]) for n in inside["norms"]]
        assert out["timeline"][0]["kind"] == "start"

    def test_prefix_that_ends_while_not_in_force_is_not_settled(self, robot_sos):
        r = _eval(robot_sos, _lines(
            dict(START, regime="NORMAL"),
            {"t": 1, "messages": [ASSIGN], "state": "Assigned"},
            {"t": 3, "regime": "STORM", "end": False}))
        assert not r["instance"]["ended"]
        assert _norm(r, "acknowledge")["state"] == "active"


class TestRegimeErrors:
    def test_unknown_regime(self, robot_sos):
        with pytest.raises(TraceError, match="line 2: 'regime' must name a regime of the "
                                             "file \\(NORMAL, STORM\\), got 'CALM'"):
            _eval(robot_sos, _lines(START, {"t": 1, "regime": "CALM"}))
        with pytest.raises(TraceError, match="not a string"):
            _eval(robot_sos, _lines(dict(START, regime=1)))

    def test_file_without_block(self):
        with pytest.raises(TraceError, match="no regimes: block"):
            parse_trace_regimes(_lines(dict(START, regime="NORMAL")), bindings=None)
        # parse_trace reads the key as unknown, as before
        with pytest.raises(TraceError, match="unknown key"):
            parse_trace(_lines(dict(START, regime="NORMAL")))

    def test_regimes_and_amendments_together_are_refused(self, robot_sos):
        with pytest.raises(NormsEvalError, match="cannot also amend"):
            _eval(robot_sos, _lines(dict(START, regime="NORMAL"), {"t": 1, "amend": "v2.cadl"}))

    def test_cli(self, tmp_path, robot_sos):
        cadl = tmp_path / "m.cadl"
        cadl.write_text(_with_regimes(EXAMPLE, "DELIVERY_SLA"), encoding="utf-8")
        trace = tmp_path / "t.jsonl"
        trace.write_text("\n".join(_lines(
            dict(START, regime="NORMAL"),
            {"t": 1, "messages": [ASSIGN], "state": "Assigned"},
            {"t": 3, "regime": "STORM"}, {"t": 60, "regime": "NORMAL"},
            {"t": 66, "messages": [ACK]})) + "\n", encoding="utf-8")
        code, out, _ = _cli(["norms-eval", str(cadl), "--trace", str(trace)])
        assert code == 1 and "deadline of 5s passed at t=63" in out
        trace.write_text(json.dumps(dict(START, regime="CALM")) + "\n", encoding="utf-8")
        code, _, err = _cli(["norms-eval", str(cadl), "--trace", str(trace)])
        assert code == 2 and "'regime' must name a regime" in err


def _cli(argv: list) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    saved = sys.argv
    sys.argv = ["cadl", *argv]
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main()
            except SystemExit as e:
                code = e.code
    finally:
        sys.argv = saved
    return code, out.getvalue(), err.getvalue()


# === Random traces ==================================================================

def _with_random_regimes(rng: random.Random, lines: list, regimes: list) -> list:
    out = []
    for line in lines:
        rec = json.loads(line)
        if rng.random() < 0.35:
            rec["regime"] = rng.choice(regimes)
        out.append(json.dumps(rec))
    return out


def _clock_trace(sos, contract, lines: list):
    """The projection of a trace written as a trace without regimes, on the
    clock of the contract: an oracle that does not use the engine for
    regimes, since the engine of norms-eval judges it as any trace. None
    when the projection cannot be written so: the instance ends at an
    observation at which the contract is not in force (only the settlement
    applies there), an observation left out gives a lifecycle state (seen
    from the next one on), or one after the last observation in force
    names a quantity first."""
    from decimal import Decimal
    b = regime_bindings(sos)
    regime, out_since, offset = b.initial, None, Decimal(0)
    recs = [json.loads(l, parse_float=Decimal) for l in lines]
    out, snapshot, late_names = [], {}, False
    for k, rec in enumerate(recs):
        regime = rec.pop("regime", regime)
        t = Decimal(rec["t"])
        if b.in_force(contract, regime):
            if out_since is not None:
                offset += t - out_since
                out_since = None
            snapshot.update(rec.get("values", {}))
            new = {key: v for key, v in rec.items() if key not in ("bindings", "request")}
            new["t"] = t - offset
            if snapshot:
                new["values"] = dict(snapshot)
            if not out:
                new.update({x: recs[0][x] for x in ("bindings", "request") if x in recs[0]})
            out.append(new)
            late_names = False
            continue
        if out_since is None:
            out_since = t
        if "state" in rec:
            return None
        if any(name not in snapshot for name in rec.get("values", {})):
            late_names = True
        snapshot.update(rec.get("values", {}))
        last = k == len(recs) - 1
        if rec.get("end") is True or (last and rec.get("end") is not False):
            return None
    if not out or late_names:
        return None
    if recs[-1] is not None and not b.in_force(contract, regime):
        out[-1].setdefault("end", False)       # the trace goes on: a prefix
    return [_dump(r) for r in out]


def _dump(x) -> str:
    """JSON with the decimals written as the numbers they are."""
    from decimal import Decimal
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {_dump(v)}" for k, v in x.items()) + "}"
    if isinstance(x, list):
        return "[" + ", ".join(_dump(v) for v in x) + "]"
    return json.dumps(x)


def _marks(result: dict) -> dict:
    return {
        "norms": [(n["id"], n["state"], n["verdict"]) for n in result["norms"]],
        "assume": [a["status"] for a in result["assumptions"]],
        "guarantee": [g["verdict"] for g in result["guarantees"]],
        "violations": [(v["norm"], v["verdict"], v["reparation_activated"])
                       for v in result["violations"]],
        "events": [e["name"] for e in result["institutional_events"]],
        "ended": result["instance"]["ended"],
    }


ORACLE_SETS = [("robot", EXAMPLE, "DELIVERY_SLA", _ROBOT, 300, 20261116),
               ("rich", RICH, "C", _RICH, 500, 20261117)]


@pytest.mark.parametrize("name,path,contract,vocab,count,seed", ORACLE_SETS,
                         ids=[s[0] for s in ORACLE_SETS])
def test_projection_against_the_clock_trace(name, path, contract, vocab, count, seed,
                                           monkeypatch):
    """The verdicts on a trace with regimes are those of the plain engine on
    the projection written out as a trace on the clock of the contract."""
    # `now` is the time of the trace, not the clock of the contract: the
    # oracle cannot write it, so the trigger of the rich file that reads it
    # reads a quantity here
    text = _with_regimes(path, contract).replace("AND now > 2", "AND demand > 2")
    sos = parse(text)
    plain = parse(path.read_text(encoding="utf-8").replace("AND now > 2", "AND demand > 2"))
    _memoize_parser(monkeypatch)
    rng = random.Random(seed)
    compared = left = 0
    for _ in range(count):
        lines = _with_random_regimes(rng, _random_trace(rng, vocab), ["NORMAL", "STORM"])
        clock = _clock_trace(sos, contract, [l for l in lines if l.strip()])
        try:
            got = _eval(sos, lines)
        except (TraceError, NormsEvalError) as e:
            if clock is not None:
                with pytest.raises(type(e)):
                    evaluate(plain, parse_trace(clock))
            continue
        if clock is None:
            continue
        left += len(lines) - len(clock)
        want = evaluate(plain, parse_trace(clock))
        assert _marks(got) == _marks(want), (lines, clock)
        compared += 1
    assert compared > count // 3 and left > count // 3


# --- the norms monitor of the Python target: the same as norms-eval -----------------

_BATCH = _NO_CADL + """
import importlib, json
with open(sys.argv[2], encoding="utf-8") as f:
    cases = json.load(f)
out = []
for case in cases:
    mod = importlib.import_module(case["package"] + ".norms_monitor")
    r = {}
    try:
        r["result"] = mod.replay(case["lines"], case.get("contract"))
    except (mod.TraceError, mod.NormsEvalError, mod.MonitorError) as e:
        r["error"] = [type(e).__name__, str(e)]
    recs = [json.loads(l) for l in case["lines"] if l.strip()]
    if "result" in r:
        probe = mod.NormsMonitor(case.get("contract"))
        inside = []
        for rec in recs:
            probe.observe(rec)
            inside.append(rec)
            if probe.ended:
                break
        names = set()
        for rec in inside:
            names.update("".join(k.split()) for k in rec.get("values", {}))
        m = mod.NormsMonitor(case.get("contract"), quantities=names)
        checks = []
        for rec in recs:
            m.observe(rec)
            if m.started and not m.ended:
                checks.append([m.in_force, m.next_deadline(), m.next_check()])
        if not m.ended and recs[-1].get("end") is not False:
            m.end()
        r["manual"] = m.result()
        r["checks"] = checks
    m = mod.NormsMonitor(case.get("contract"))
    r["prefixes"] = []
    for rec in recs:
        m.observe(rec)
        try:
            r["prefixes"].append(m.result())
        except mod.MonitorError as e:
            r["prefixes"].append({"error": str(e)})
    out.append(r)
with open(sys.argv[3], "w", encoding="utf-8") as f:
    json.dump(out, f)
"""


def _batch(tmp_path: Path, source: str, traces: list) -> list:
    root = tmp_path / "monitors"
    _write_package(root, "rm", source)
    jobs = [{"package": "rm", "lines": lines} for lines in traces]
    (tmp_path / "cases.json").write_text(json.dumps(jobs), encoding="utf-8")
    proc = _isolated(_BATCH, root, tmp_path / "cases.json", tmp_path / "out.json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))


def _prefix(lines: list, k: int) -> list:
    out = list(lines[:k + 1])
    rec = json.loads(out[-1])
    rec.setdefault("end", False)
    out[-1] = json.dumps(rec)
    return out


def _strip(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in ("warnings", "trace")}


MONITOR_SETS = [("robot", EXAMPLE, "DELIVERY_SLA", _ROBOT, 150, 20261118),
                ("rich", RICH, "C", _RICH, 250, 20261119)]


@pytest.mark.parametrize("name,path,contract,vocab,count,seed", MONITOR_SETS,
                         ids=[s[0] for s in MONITOR_SETS])
def test_monitor_against_norms_eval(name, path, contract, vocab, count, seed, tmp_path,
                                    monkeypatch):
    """Random traces with regimes: the generated monitor gives the result of
    `cadl norms-eval` when the trace is replayed, when it is fed line by
    line, and after every observation on the prefix up to it; its
    next_deadline and next_check are None while the contract is not in
    force."""
    sos = parse(_with_regimes(path, contract))
    source = generate_norms_monitor_module(sos)
    assert "class RegimeBindings" in source
    _memoize_parser(monkeypatch)
    rng = random.Random(seed)
    traces = [_with_random_regimes(rng, _random_trace(rng, vocab), ["NORMAL", "STORM"])
              for _ in range(count)]
    got = _batch(tmp_path, source, traces)
    seen = {"error": 0, "out_end": 0, "out_checks": 0}
    for lines, g in zip(traces, got):
        try:
            want = {"result": _eval(sos, lines)}
        except (TraceError, NormsEvalError) as e:
            want = {"error": [type(e).__name__, str(e)]}
        if "error" in want:
            assert g.get("error") == want["error"], lines
            seen["error"] += 1
            continue
        assert _strip(g["result"]) == _strip(want["result"]), lines
        assert _strip(g["manual"]) == _strip(want["result"]), lines
        seen["out_end"] += "while the contract is not in force" in (
            want["result"]["instance"]["end_reason"] or "")
        for in_force, deadline, check in g["checks"]:
            if not in_force:
                assert deadline is None and check is None
                seen["out_checks"] += 1
        nonblank = [l for l in lines if l.strip()]
        for k, at_k in enumerate(g["prefixes"]):
            try:
                w = _strip(_eval(sos, _prefix(nonblank, k)))
            except NormsEvalError:
                assert "error" in at_k, (k, lines)
                continue
            assert _strip(at_k) == w, (k, lines)
    assert seen["out_end"] and seen["out_checks"]


def test_monitor_api(tmp_path):
    """next_deadline on the time of the trace; end() at an observation at
    which the contract is not in force; a refused observation keeps the
    monitor as it was."""
    import importlib.util
    sos = parse(_with_regimes(EXAMPLE, "DELIVERY_SLA"))
    path = tmp_path / "rmon.py"
    path.write_text(generate_norms_monitor_module(sos), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("rmon", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rmon"] = mod
    try:
        spec.loader.exec_module(mod)
        m = mod.NormsMonitor("DELIVERY_SLA")
        m.observe(dict(START, regime="NORMAL"))
        m.observe({"t": 1, "messages": [ASSIGN], "state": "Assigned"})
        assert m.next_deadline() == 6.0
        assert m.observe({"t": 3, "regime": "STORM"}) == []
        assert not m.in_force and m.next_deadline() is None and m.next_check() is None
        with pytest.raises(mod.TraceError):
            m.observe({"t": 2, "regime": "NORMAL"})      # time goes back
        with pytest.raises(mod.TraceError):
            m.observe({"t": 4, "regime": "CALM"})
        assert not m.in_force
        assert m.observe({"t": 50, "regime": "NORMAL"}) == []
        assert m.in_force and m.next_deadline() == 53.0
        with pytest.raises(mod.ObservationError, match="cannot also amend"):
            m.observe({"t": 51, "amend": "v2"})
        m.observe({"t": 51, "regime": "STORM"})
        changes = m.end()
        assert {c["id"]: c["state"] for c in changes}["acknowledge"] == "discharged"
        assert m.result()["instance"]["end_reason"] == (
            "last observation of the trace, while the contract is not in force")
    finally:
        del sys.modules["rmon"]


def test_monitor_of_a_file_without_regimes_is_unchanged():
    from cadl.codegen.norms_monitor_gen import runtime_source
    source = generate_norms_monitor_module(parse_file(EXAMPLE))
    assert source.endswith(runtime_source())
    assert "RegimeBindings" not in source and "monitor-regimes" not in source


# === Python target: the initial regime ===============================================

ORDER = textwrap.dedent("""\
    sos:
      name: Order
      actors: [{id: A, role: a}]
      contracts:
        - {id: C, parties: [A], guarantee: ["x >= 0"]}
      transitions:
        - {from: B, to: C2}
        - {from: A1, to: B}
        - {from: C2, to: B}
    """)


def test_initial_regime():
    from cadl.codegen.transition_gen import generate_transitions_module
    sos = parse(ORDER)
    assert initial_regime(sos) == "A1" == RegimeMap.from_sos(sos).initial_state
    # without regimes: the controller keeps the from: of the first transition
    assert "initial_regime: str = 'B'" in generate_transitions_module(sos.transitions)
    bound = parse(ORDER + "  regimes:\n    B: {contracts: {suspended: [C]}}\n")
    from cadl.regimes import checked
    text = generate_transitions_module(bound.transitions, checked(bound), {}, initial_regime(bound))
    assert "initial_regime: str = 'A1'" in text


# === Solidity ========================================================================

def _solidity(tmp_path: Path, source: str) -> Path:
    out = tmp_path / "sol"
    generate(parse(source), out, target="solidity")
    return out


def test_solidity_text(tmp_path):
    out = _solidity(tmp_path, GOOD)
    routing = (out / "Routing.sol").read_text()
    assert "function BETA() public view returns (uint256)" in routing
    assert "if (r == RegimeController.Regime.Centralized) return 900;" in routing
    assert "contractActive" not in routing                 # in force in every regime
    bay = (out / "BayAssignment.sol").read_text()
    assert 'if (!regimeController.contractActive("BAY_ASSIGNMENT")) return;' in bay
    ctl = (out / "RegimeController.sol").read_text()
    assert "Regime public currentRegime = Regime.Distributed;" in ctl
    assert "function evaluateTransitions() public onlyOrchestrator {" in ctl
    assert "emit RegimeTransitionRefused(currentRegime, Regime.Centralized" in ctl
    assert "function setUint(string calldata key, uint256 value) external onlyOrchestrator" in ctl
    assert "function runMonitorCycle() public onlyOrchestrator {" in bay
    main_ = (out / "RobotTown.sol").read_text()
    assert main_.index("regimeController = new RegimeController();") < main_.index(
        "routing = new Routing(regimeController);")
    # without the block: as before
    plain = _solidity(tmp_path / "p", BASE)
    assert "uint256 public constant BETA = 200;" in (plain / "Routing.sol").read_text()
    assert "contractActive" not in (plain / "RegimeController.sol").read_text()


@pytest.mark.skipif(S.solc_binary() is None or S.evm_python() is None,
                    reason="no solc, or no EVM (CADL_EVM_PYTHON)")
def test_solidity_on_an_evm(tmp_path):
    out = _solidity(tmp_path, GOOD)
    proc = subprocess.run([S.solc_binary(), "--combined-json", "abi,bin", "RobotTown.sol"],
                          cwd=out, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = json.loads(proc.stdout)["contracts"]
    contracts = {}
    for key, value in data.items():
        abi = value["abi"] if isinstance(value["abi"], list) else json.loads(value["abi"])
        contracts[key.split(":")[-1]] = {"abi": abi, "bin": value["bin"]}
    rc = ["regimeController"]
    fp, bay = ["free_passage"], ["bay_assignment"]

    def cycle():
        return [["send", [], "runMonitorCycle", []], ["call", rc, "currentRegime", []],
                ["call", fp, "violationCount", []], ["call", bay, "violationCount", []]]

    def put(key, value):
        return ["send", [], "setUint", [key, value]]

    # Regime enum: Centralized = 0, Distributed = 1 (sorted)
    steps = [
        ["call", ["routing"], "BETA", []],                   # [200]
        ["call", rc, "currentRegime", []],                   # [1]: the initial regime
        ["send", bay, "runMonitorCycle", []],                # only the orchestrator
        ["send", rc, "evaluateTransitions", []],             # only the orchestrator
        put("orders_in_window", 25), put("waiting_on_road", 0), put("robots_on_road", 3),
        *cycle(),                                            # stays: 25 >= 22
        put("orders_in_window", 10),
        *cycle(),                                            # refused: robots_on_road == 3
        put("robots_on_road", 0),
        *cycle(),                                            # taken
        ["call", ["routing"], "BETA", []],                   # [900]
        *cycle(),                                            # FREE_PASSAGE not in force
        put("orders_in_window", 30),
        *cycle(),                                            # back to DISTRIBUTED
    ]
    expect = [[200], [1], "error", "error", "ok", "ok", "ok",
              "ok", [1], [1], [0],
              "ok", "ok", [1], [2], [0],
              "ok", "ok", [0], [3], [0],
              [900],
              "ok", [0], [3], [0],
              "ok", "ok", [1], [3], [0]]
    job = {"contracts": contracts, "main": "RobotTown", "steps": steps}
    (tmp_path / "job.json").write_text(json.dumps(job), encoding="utf-8")
    proc = subprocess.run([S.evm_python(), "-I", str(HERE / "regime_evm_driver.py"),
                           str(tmp_path / "job.json"), str(tmp_path / "out.json")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-3000:]
    got = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    # Regime enum: Centralized = 0, Distributed = 1 (sorted)
    got = ["error" if isinstance(g, dict) and "error" in g else g for g in got]
    assert got == expect, got


# === Rego ============================================================================

@pytest.mark.skipif(S.opa_binary() is None, reason="the opa binary is not on PATH")
def test_rego_policies(tmp_path):
    out = tmp_path / "opa"
    generate(parse(GOOD), out, target="opa")
    files = [str(out / f) for f in ("routing.rego", "bay_assignment.rego", "free_passage.rego")]
    query = ('x := {"bay": data.cadl.bay_assignment.allow, '
             '"bayv": data.cadl.bay_assignment.violation, '
             '"free": data.cadl.free_passage.allow, '
             '"freev": data.cadl.free_passage.violation, "beta": data.cadl.routing.beta}')

    def run(inp: dict) -> dict:
        (tmp_path / "in.json").write_text(json.dumps(inp), encoding="utf-8")
        args = [S.opa_binary(), "eval", "-i", str(tmp_path / "in.json"), "--format", "json"]
        for f in files:
            args += ["-d", f]
        proc = subprocess.run(args + [query], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)["result"][0]["bindings"]["x"]

    road = {"waiting_on_road": 0, "robots_on_road": 1}
    assert run(dict(road, regime="DISTRIBUTED")) == {
        "bay": True, "bayv": [], "free": False,
        "freev": ["Guarantee violated in FREE_PASSAGE"], "beta": 0.2}
    assert run(dict(road, regime="CENTRALIZED")) == {
        "bay": True, "bayv": [], "free": True, "freev": [], "beta": 0.9}
    missing = run(road)
    assert missing["bay"] is False and missing["free"] is False
    assert missing["bayv"] == ["input.regime is missing: it names the current regime"]
    assert run(dict(road, regime="CALM"))["freev"] == [
        "input.regime CALM is not a regime of the file"]


def test_rego_main_parameters(tmp_path):
    out = tmp_path / "opa"
    generate(parse(GOOD), out, target="opa")
    text = (out / "main.rego").read_text()
    assert 'initial_regime := "DISTRIBUTED"' in text
    assert '"ROUTING.beta": 0.9, "environment.bays": 3' in text
    # no values without a regime of the file (nothing is allowed then)
    assert text.rstrip().count("input.regime in regimes") == 1
    plain = tmp_path / "plain"
    generate(parse(BASE), plain, target="opa")
    assert "regime" not in (plain / "routing.rego").read_text()


# === Targets that do not read the regime ============================================

@pytest.mark.parametrize("target", ["opa", "solidity"])
def test_norms_monitors_of_opa_and_solidity(target):
    sos = parse(_with_regimes(EXAMPLE, "DELIVERY_SLA"))
    [m] = norms_monitors(sos, target)
    assert m["text"] is None and "STORM do not have the contract in force" in m["reasons"][0]
    # the public generators refuse it too
    from cadl.codegen.norms_ir import NormsTargetUnsupported
    from cadl.codegen.opa.norms_monitor_rego import generate_norms_monitor_rego
    from cadl.codegen.solidity.norms_monitor_sol import generate_norms_monitor_sol
    gen = generate_norms_monitor_rego if target == "opa" else generate_norms_monitor_sol
    with pytest.raises(NormsTargetUnsupported, match="STORM do not have the contract"):
        gen(sos, sos.contracts[0])
    # a contract that every regime has in force: the monitor of the file
    # without the block (no norm reads a parameter)
    other = _with_regimes(EXAMPLE, "DELIVERY_SLA").replace(
        "suspended: [DELIVERY_SLA]", "suspended: []")
    [m] = norms_monitors(parse(other), target)
    [plain] = norms_monitors(parse_file(EXAMPLE), target)
    assert m["text"] == plain["text"] is not None


def test_unity_csharp(tmp_path):
    sos = parse(_with_regimes(EXAMPLE, "DELIVERY_SLA"))
    with pytest.raises(ValueError, match="'unity-csharp' cannot apply the regime bindings"):
        generate(sos, tmp_path / "u", target="unity-csharp")
    assert not (tmp_path / "u").exists()
    # in force in every regime: the code of the file without the block
    other = parse(_with_regimes(EXAMPLE, "DELIVERY_SLA").replace(
        "suspended: [DELIVERY_SLA]", "suspended: []"))
    generate(other, tmp_path / "a", target="unity-csharp")
    generate(parse_file(EXAMPLE), tmp_path / "b", target="unity-csharp")
    for f in sorted((tmp_path / "b").rglob("*")):
        if f.is_file():
            assert (tmp_path / "a" / f.relative_to(tmp_path / "b")).read_text() == f.read_text()


def test_boundary_policy_of_the_agent_extension():
    from cadl.agents import build_policy
    path = ROOT / "examples" / "agents_tool_governance.cadl"
    sos = parse(_with_regimes(path, "ASSISTED_REPORTING"))
    assert type_check(sos).ok
    with pytest.raises(ValueError, match="boundary policy cannot apply the regime bindings"):
        build_policy(sos)
    kept = parse(_with_regimes(path, "ASSISTED_REPORTING").replace(
        "suspended: [ASSISTED_REPORTING]", "suspended: []"))
    assert build_policy(kept) == build_policy(parse_file(path))


# === cadl regime-map ================================================================

def test_regime_map_shows_the_bindings():
    rm = RegimeMap.from_sos(parse(GOOD))
    text = rm.to_text()
    assert "Regime bindings (A.7.1):\n  DISTRIBUTED:\n    in force: ROUTING, FREE_PASSAGE\n" \
           "    not in force: BAY_ASSIGNMENT\n  CENTRALIZED:\n    ROUTING.beta = 0.9\n" in text
    assert 'environment.mode_name = "steward"' in text
    dot = rm.to_dot()
    assert '"CENTRALIZED" [label="CENTRALIZED\\nROUTING.beta = 0.9\\n' in dot
    assert '\\"steward\\"' in dot
    data = rm.to_json()
    assert data["regimes"][1] == {
        "name": "CENTRALIZED",
        "parameters": {"ROUTING.beta": 0.9, "environment.bays": 3, "environment.edge_grant": 1,
                       "environment.exclusive_edges": True, "environment.mode_name": "steward"},
        "active_contracts": ["ROUTING", "BAY_ASSIGNMENT"],
        "suspended_contracts": ["FREE_PASSAGE"]}
    plain = RegimeMap.from_sos(parse(BASE))
    assert "regimes" not in plain.to_json() and "bindings" not in plain.to_text()
    broken = RegimeMap.from_sos(parse(GOOD.replace("ROUTING.beta: 0.9", "ROUTING.beta: 9")))
    assert broken.to_json()["regimes"] is None
    assert "not shown, the regimes: block has errors" in broken.to_text()


def test_python_controller_checks_the_safety_invariant_and_the_names(tmp_path):
    """A.7.1: a transition whose safety invariant does not hold is not
    taken, the regime stays and the refusal is recorded (no assert, which
    `python -O` removes); a name that is not a regime is a ValueError."""
    pkg = tmp_path / "town_m1"
    generate(parse(GOOD), pkg)
    text = (pkg / "transitions.py").read_text()
    assert "assert " not in text
    script = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[1])
        from town_m1.runtime import RobotTownRuntime
        rt = RobotTownRuntime()
        rc = rt.regime_controller
        rt.ctx.state.update(orders_in_window=10, robots_on_road=3, waiting_on_road=1)
        target = rc.evaluate_transitions(rt.ctx)
        print(target, rc.execute_transition(target, rt.ctx), rc.current_regime,
              rc.refused_transitions)
        rt.ctx.state["robots_on_road"] = 0
        print(rc.execute_transition(target, rt.ctx), rc.current_regime)
        for bad in ("lambda: type(rc)('BOGUS')", "lambda: rc.execute_transition('BOGUS', rt.ctx)"):
            try:
                eval(bad)()
            except ValueError as e:
                print("ValueError", e)
    """)
    proc = subprocess.run([sys.executable, "-O", "-c", script, str(tmp_path)],
                          capture_output=True, text=True,
                          env={"PYTHONPATH": str(ROOT / "src")})
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0] == ("CENTRALIZED False DISTRIBUTED [('DISTRIBUTED', 'CENTRALIZED', "
                        "'safety invariant does not hold: robots_on_road == 0')]")
    assert lines[1] == "True CENTRALIZED"
    assert lines[2:] == ["ValueError 'BOGUS' is not a regime of the file (CENTRALIZED, DISTRIBUTED)"] * 2


@pytest.mark.skipif(S.opa_binary() is None, reason="the opa binary is not on PATH")
@pytest.mark.parametrize("path", sorted((ROOT / "examples").glob("*.cadl")), ids=lambda p: p.stem)
def test_opa_check_of_every_example(path, tmp_path):
    """`opa check` accepts what `cadl codegen -t opa` writes for every example
    (0.3.33 fixes all_violations of main.rego for two or more contracts and
    function calls that Rego does not have)."""
    sos = parse_file(path)
    if not type_check(sos).ok:
        pytest.skip("the example does not pass cadl check")
    generate(sos, tmp_path / "opa", target="opa")
    proc = subprocess.run([S.opa_binary(), "check", str(tmp_path / "opa")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(S.opa_binary() is None, reason="the opa binary is not on PATH")
def test_rego_main_parameters_need_a_regime(tmp_path):
    out = tmp_path / "opa"
    generate(parse(GOOD), out, target="opa")
    files = sorted(str(f) for f in out.glob("*.rego"))

    def params(inp: dict):
        (tmp_path / "in.json").write_text(json.dumps(inp), encoding="utf-8")
        args = [S.opa_binary(), "eval", "-i", str(tmp_path / "in.json"), "--format", "json"]
        for f in files:
            args += ["-d", f]
        proc = subprocess.run(args + ["data.cadl.robot_town.parameters"],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        res = json.loads(proc.stdout).get("result")
        return res[0]["expressions"][0]["value"] if res else None

    assert params({"regime": "CENTRALIZED"})["ROUTING.beta"] == 0.9
    assert params({"regime": "DISTRIBUTED"})["ROUTING.beta"] == 0.2
    assert params({}) is None and params({"regime": "CALM"}) is None
