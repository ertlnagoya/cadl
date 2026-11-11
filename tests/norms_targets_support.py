"""Differential testing of the norms monitors of the OPA and Solidity
targets against the engine of `cadl norms-eval` (tests/test_norms_monitor_targets.py).

`reference` runs the engine of norms-eval (cadl/norms_eval.py) on a
trace one observation at a time, as the Python monitor does, and records
after every observation what both monitors report: the state and verdict
of every norm, the status of every assume: item, the verdict of every
guarantee: item, the violations, institutional events and unauthorised
acts of that observation, and whether the instance has ended. The names
of the quantities are those of the observations of the instance, given to
the monitors in advance (as `cadl norms-eval` knows them from the whole
trace).

`run_opa` replays traces through the generated Rego with one `opa eval`
(a driver policy chains the steps with `with input as`); `run_evm`
deploys the generated Solidity on an EVM in a separate Python (see
tests/norms_evm_driver.py) and calls observe() per observation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

from cadl.norms_eval import (
    NormsEvalError, TraceError, _Engine, declared_actors, parse_trace, select_contract,
)

HERE = Path(__file__).parent
MAX_STEPS = 16
STATES = ["inactive", "active", "suspended", "fulfilled", "violated", "discharged"]
VERDICTS = ["pending", "fulfilled", "violated", "excused"]
STATUS = ["pending", "holds", "failed"]


# --- the reference ------------------------------------------------------------------

def _item_mark(key: str, item: dict) -> str:
    if key == "guarantee":
        return item["verdict"]
    v = item["value"]
    return "pending" if v in (None, "undefined") else "holds" if v else "failed"


_POWER_RE = re.compile(r"^(.*) exercises the power (\S+) \((.*)\)$")


def _next_deadline(eng, t: float):
    """next_deadline() of the Python monitor, from the engine."""
    if eng.ended_at is not None:
        return None
    due = [t + max(x.deadline - x.elapsed(t), 0.0) for x in eng.norms
           if x.state == "active" and not x.frozen and x.deadline is not None
           and x.d.kind in ("obligation", "prohibition") and x.elapsed(t) <= x.deadline + 1e-9]
    return _round(min(due)) if due else None


def _round(x):
    return None if x is None else round(float(x), 6)


def _changes(before: dict, after: dict) -> list:
    """The changes from one snapshot to the next, as sorted tuples."""
    out = []
    for nid, now in after["norms"].items():
        old = before["norms"].get(nid, ["inactive", "pending"])
        if old != now:
            out.append(["norm", nid, old[0], old[1], now[0], now[1]])
    for key, kind in (("assume", "assumption"), ("guarantee", "guarantee")):
        for pos, (old, now) in enumerate(zip(before[key], after[key]), start=1):
            if old != now and now != "-":
                out.append([kind, pos, old, now])
    return sorted(out, key=json.dumps)


def _marks(eng, skip: set) -> dict:
    return {
        "norms": {x.d.id: [x.state, x.verdict] for x in eng.norms},
        "assume": [_item_mark("assume", i) for i in eng.items["assume"]],
        "guarantee": [_item_mark("guarantee", i) if pos not in skip else "-"
                      for pos, i in enumerate(eng.items["guarantee"])],
    }


def _snapshot(eng, nv: int, ne: int, nu: int, nt: int, skip: set, before: dict, t: float) -> dict:
    marks = _marks(eng, skip)
    powers = []
    for entry in eng.timeline[nt:]:
        if entry["kind"] == "power":
            m = _POWER_RE.match(entry["text"])
            powers.append([m.group(2), m.group(1), m.group(3)])
    return dict(marks, **{
        "violations": [[v["norm"], v["verdict"], v["reparation_activated"], v["transition"],
                        v["severity"], v["sanction"]] for v in eng.violations[nv:]],
        "events": [[e["name"], e["performer"]] for e in eng.inst_events[ne:]],
        "unauthorised": [[u["act"], u["performer"]] for u in eng.unauthorised[nu:]],
        "powers": powers,
        "changes": _changes(before, marks),
        "next_deadline": _next_deadline(eng, t),
        "ended": eng.ended_at is not None,
    })


def reference(sos, lines: list, contract: str | None = None, skip: set = frozenset()) -> dict:
    """{"steps": [snapshot after each observation of the instance],
    "quantities": [...]} or {"error": text}. `skip` lists guarantee
    positions (from 0) that a monitor leaves out."""
    c = select_contract(sos, contract)
    actors = declared_actors(sos)
    try:
        trace = parse_trace(lines)
        probe = _Engine(c, trace, None, actors)
        probe.run()
    except (TraceError, NormsEvalError) as e:
        return {"error": str(e)}
    end = probe.ended_at
    inside = trace.observations if end is None else trace.observations[:end + 1]
    names: set = set()
    for o in inside:
        names.update(o.values)
    trace = parse_trace(lines)
    eng = _Engine(c, trace, None, actors)
    eng.ev.known = set(names)
    steps = []
    nv = ne = nu = nt = 0
    last = len(trace.observations) - 1
    before = _marks(eng, skip)
    for n in range(len(trace.observations)):
        ended = eng._step(n, n == last)
        eng._finish_items()
        steps.append(_snapshot(eng, nv, ne, nu, nt, skip, before, trace.observations[n].t))
        before = {k: steps[-1][k] for k in ("norms", "assume", "guarantee")}
        nv, ne, nu = len(eng.violations), len(eng.inst_events), len(eng.unauthorised)
        nt = len(eng.timeline)
        if ended:
            break
    return {"steps": steps, "quantities": sorted(names)}


def compare(refs: list, got: list, count_steps: list | None = None) -> list:
    """Mismatches between the reference and the snapshots of a monitor
    (lists of {"steps": [...], "finish": snapshot or None})."""
    bad = []
    for i, (ref, g) in enumerate(zip(refs, got)):
        if "error" in ref:
            if not any(st.get("error") for st in g["steps"]):
                bad.append((i, "the monitor accepted the trace", ref["error"]))
            continue
        errors = [st for st in g["steps"] if st.get("error")]
        if errors:
            bad.append((i, "the monitor refused an observation", errors[0]))
            continue
        for k, want in enumerate(ref["steps"]):
            have = dict(g["steps"][k])
            if (k == len(ref["steps"]) - 1 and g.get("finish") is not None
                    and not have["ended"]):
                f = dict(g["finish"])
                f["violations"] = have["violations"] + f["violations"]
                for key in ("events", "unauthorised", "powers"):
                    f[key] = have[key]
                f["changes"] = _merge(have["changes"], f["changes"])
                have = f
            if count_steps is not None:
                count_steps.append(1)
            if have != want:
                bad.append((i, k, want, have))
                break
    return bad


def _merge(first: list, second: list) -> list:
    """The changes of two consecutive steps as the changes of one."""
    def key(c):
        return (c[0], c[1])
    state = {}
    for c in first + second:
        k = key(c)
        old = state[k][0] if k in state else (c[2:4] if c[0] == "norm" else [c[2]])
        new = c[4:6] if c[0] == "norm" else [c[3]]
        state[k] = (old, new)
    out = [[k[0], k[1], *old, *new] for k, (old, new) in state.items() if old != new]
    return sorted(out, key=json.dumps)


def records(lines: list) -> list:
    return [json.loads(line) for line in lines if line.strip()]


def finishes(lines: list) -> bool:
    """Whether the instance ends at the last line unless an earlier one ends it."""
    recs = records(lines)
    return recs[-1].get("end") is not False


# --- OPA ---------------------------------------------------------------------------------

def opa_binary() -> str | None:
    return shutil.which("opa")


_DRIVER = """package driver

import rego.v1

st(prev) := null if {
	prev == null
} else := prev.state

lastok(r, prev) := prev if {
	r == null
} else := prev if {
	r.error
} else := r

s(tr, prev, i) := null if {
	i >= count(tr.obs)
} else := r if {
	r := data.%(pkg)s.result with input as {"state": st(prev), "observation": tr.obs[i], "quantities": tr.q}
}

fin(tr, prev) := null if {
	prev == null
} else := null if {
	not tr.finish
} else := null if {
	prev.state.ended
} else := r if {
	r := data.%(pkg)s.result with input as {"state": prev.state, "finish": true}
}

snap(r) := null if {
	r == null
} else := {"error": r.error} if {
	r.error
} else := {
	"norms": {id: [x.state, x.verdict] | some id, x in r.verdicts.norms},
	"assume": [a.status | some a in r.verdicts.assumptions],
	"guarantee": [g_mark(g) | some g in r.verdicts.guarantees],
	"violations": [[v.norm, v.verdict, v.reparation_activated, v.transition, v.severity, v.sanction] |
		some v in r.violations
	],
	"events": [[e.name, e.performer] | some e in r.institutional_events],
	"unauthorised": [[u.act, u.performer] | some u in r.unauthorised],
	"powers": [[p.power, p.performer, p.act] | some p in r.powers_exercised],
	"changes": [change(c) | some c in r.changes],
	"next_deadline": r.next_deadline,
	"ended": r.ended,
}

change(c) := ["norm", c.id, c.previous_state, c.previous_verdict, c.state, c.verdict] if {
	c.kind == "norm"
} else := ["assumption", c.index, c.previous, c.status] if {
	c.kind == "assumption"
} else := ["guarantee", c.index, c.previous, c.verdict]

g_mark(g) := g.verdict if {
	g.monitored
} else := "-"

run(tr) := out if {
%(unrolled)s
	rs := [%(names)s]
	out := {"steps": [snap(r) | some r in rs; r != null], "finish": snap(fin(tr, %(last)s))}
}

results := [run(tr) | some tr in input.traces]
"""


def opa_driver(package: str, steps: int | None = None) -> str:
    steps = steps or MAX_STEPS
    lines, prev = [], "null"
    for i in range(steps):
        lines.append(f"\tr{i} := s(tr, {prev}, {i})")
        lines.append(f"\tp{i} := lastok(r{i}, {prev})")
        prev = f"p{i}"
    return _DRIVER % {"pkg": package, "unrolled": "\n".join(lines),
                      "names": ", ".join(f"r{i}" for i in range(steps)), "last": prev}


def run_opa(rego_text: str, package: str, traces: list, refs: list, tmp: Path,
            steps: int | None = None) -> list:
    """Replay traces (lists of lines) through the Rego monitor."""
    steps = steps or MAX_STEPS
    inputs = []
    for lines, ref in zip(traces, refs):
        recs = records(lines)
        assert len(recs) <= steps
        inputs.append({"obs": recs, "q": ref.get("quantities", []), "finish": finishes(lines)})
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "monitor.rego").write_text(rego_text, encoding="utf-8")
    (tmp / "driver.rego").write_text(opa_driver(package, steps), encoding="utf-8")
    (tmp / "input.json").write_text(json.dumps({"traces": inputs}), encoding="utf-8")
    proc = subprocess.run(
        [opa_binary(), "eval", "-d", str(tmp / "monitor.rego"), "-d", str(tmp / "driver.rego"),
         "-i", str(tmp / "input.json"), "--format", "json", "data.driver.results"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return [_normal(r) for r in json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]]


def _normal(run: dict) -> dict:
    for st in run["steps"] + ([run["finish"]] if run.get("finish") else []):
        if "changes" in st:
            st["changes"] = sorted(st["changes"], key=json.dumps)
            st["next_deadline"] = _round(st["next_deadline"])
    return run


def opa_step(rego_text: str, package: str, input_: dict, tmp: Path) -> dict:
    """One query of `result`."""
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "monitor.rego").write_text(rego_text, encoding="utf-8")
    (tmp / "input.json").write_text(json.dumps(input_), encoding="utf-8")
    proc = subprocess.run(
        [opa_binary(), "eval", "-d", str(tmp / "monitor.rego"), "-i", str(tmp / "input.json"),
         "--format", "json", f"data.{package}.result"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]


# --- Solidity -----------------------------------------------------------------------------

def solc_binary() -> str | None:
    env = os.environ.get("CADL_SOLC")
    if env:
        return env if Path(env).exists() else None
    found = shutil.which("solc")
    if found:
        return found
    solcx = Path.home() / ".solcx"
    if solcx.is_dir():
        cands = sorted((p for p in solcx.glob("solc-v0.8.*") if p.is_file()),
                       key=lambda p: tuple(int(x) for x in p.name[len("solc-v"):].split(".")))
        if cands:
            return str(cands[-1])
    return None


def evm_python() -> str | None:
    """A Python with eth-tester and py-evm (CADL_EVM_PYTHON), or None."""
    env = os.environ.get("CADL_EVM_PYTHON")
    candidates = [env] if env else []
    for py in candidates:
        if py and Path(py).exists():
            proc = subprocess.run([py, "-c", "import eth_tester, eth_abi, eth.vm"],
                                  capture_output=True, text=True)
            if proc.returncode == 0:
                return py
    return None


def compile_solidity(source: str, name: str, tmp: Path, runs: int = 1) -> dict:
    """solc --combined-json abi,bin of the generated file."""
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "Monitor.sol"
    path.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [solc_binary(), "--optimize", "--optimize-runs", str(runs), "--combined-json",
         "abi,bin,bin-runtime", "Monitor.sol"], cwd=tmp, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-3000:]
    data = json.loads(proc.stdout)["contracts"]
    out = {}
    for key, value in data.items():
        abi = value["abi"] if isinstance(value["abi"], list) else json.loads(value["abi"])
        out[key] = {"abi": abi, "bin": value["bin"], "runtime": len(value["bin-runtime"]) // 2}
    return out


def fixed(value, what: str) -> int:
    d = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    v = d * 10 ** 6
    assert v == v.to_integral_value(), f"{what} {value!r} is not a fixed-point number"
    return int(v)


def sol_value(name: str, v) -> list:
    if v is None:
        return [name, 0, 0, ""]
    if isinstance(v, bool):
        return [name, 2, int(v), ""]
    if isinstance(v, (int, float)):
        return [name, 3, fixed(v, name), ""]
    assert isinstance(v, str), v
    return [name, 4, 0, v]


def _flat(prefix: str, obj: dict, out: list) -> None:
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            _flat(key + ".", v, out)
        else:
            out.append(sol_value(key, v))


def sol_run(lines: list, quantities: list) -> dict:
    """A trace as the calls of the Solidity monitor."""
    recs = records(lines)
    first = recs[0]
    # the host's part: a binding written with a fraction or an exponent (1.0)
    # is not an integer; it is passed with a kind the contract refuses
    bindings = [[k, 1, 0, ""] if isinstance(v, float) else sol_value(k, v)
                for k, v in first.get("bindings", {}).items()]
    request: list = []
    _flat("", first.get("request", {}), request)
    steps = []
    for rec in recs:
        values = [sol_value(k, v) for k, v in rec.get("values", {}).items()]
        msgs = [[m["from"], m["to"], m["name"], [sol_value("", a) for a in m.get("args", [])]]
                for m in rec.get("messages", [])]
        steps.append({"observe": [fixed(rec["t"], "t"), values, msgs, "state" in rec,
                                  rec.get("state", ""), rec.get("end") is True]})
    if finishes(lines):
        steps.append({"finish": True})
    return {"begin": [bindings, request, list(quantities)], "steps": steps}


def run_evm(compiled: dict, monitor: str, runs: list, tmp: Path) -> list:
    libs = [k for k in ("Monitor.sol:CadlNormsValues", "Monitor.sol:CadlNormsEval")]
    job = {"contracts": compiled, "libraries": libs, "monitor": monitor, "runs": runs}
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "job.json").write_text(json.dumps(job), encoding="utf-8")
    proc = subprocess.run([evm_python(), "-I", str(HERE / "norms_evm_driver.py"),
                           str(tmp / "job.json"), str(tmp / "out.json")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    return json.loads((tmp / "out.json").read_text(encoding="utf-8"))


def sol_snapshot(r: dict, ids: list, skip: set) -> dict:
    if "error" in r:
        return {"error": r["error"]}
    states, verdicts, assume, guarantee = r["verdicts"]
    snap = {
        "norms": {i: [STATES[s], VERDICTS[v]] for i, s, v in zip(ids, states, verdicts)},
        "assume": [STATUS[a] for a in assume],
        "guarantee": [VERDICTS[g] if pos not in skip else "-" for pos, g in enumerate(guarantee)],
        "violations": [], "events": [], "unauthorised": [], "powers": [], "changes": [],
        "next_deadline": (r["deadline"][1] / 10 ** 6) if r["deadline"][0] else None,
        "ended": r["ended"],
    }
    snap["next_deadline"] = _round(snap["next_deadline"])
    for name, vals in r["logs"]:
        if name == "Violation":
            snap["violations"].append([vals[0], VERDICTS[vals[2]], vals[5], vals[6] or None,
                                       vals[1], vals[3] or None])
        elif name == "PowerExercised":
            snap["powers"].append([vals[0], vals[1], vals[2]])
        elif name == "NormChanged":
            snap["changes"].append(["norm", vals[1], STATES[vals[4]], VERDICTS[vals[5]],
                                    STATES[vals[2]], VERDICTS[vals[3]]])
        elif name == "ItemChanged":
            names = VERDICTS if vals[0] else STATUS
            if not (vals[0] and (vals[1] - 1) in skip):
                snap["changes"].append(["guarantee" if vals[0] else "assumption", vals[1],
                                        names[vals[3]], names[vals[2]]])
        elif name == "InstitutionalEvent":
            snap["events"].append([vals[0], vals[1] or None])
        elif name == "Unauthorised":
            snap["unauthorised"].append([vals[0], vals[1]])
    snap["changes"] = sorted(snap["changes"], key=json.dumps)
    return snap


def sol_results(raw: list, runs: list, ids: list, skip: set = frozenset()) -> list:
    """The EVM results as the snapshots `compare` reads."""
    out = []
    for results, run in zip(raw, runs):
        if any("error" in r for r in results[:1]) and run["begin"] is not None \
                and len(results) == len(run["steps"]) + 1:
            out.append({"steps": [{"error": results[0]["error"]}], "finish": None})
            continue
        snaps = [sol_snapshot(r, ids, skip) for r in results]
        if run["steps"] and "finish" in run["steps"][-1]:
            out.append({"steps": snaps[:-1], "finish": snaps[-1]})
        else:
            out.append({"steps": snaps, "finish": None})
    return out
