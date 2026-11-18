"""Random versions of a run (Appendix K, K.11) and traces that name them,
for the differential tests of the norms monitors of the OPA and Solidity
targets with versions (tests/test_version_targets.py). Not a test module.

A run is made from norms_robot_delivery.cadl: version 1 (sometimes
mutated), version 2 and sometimes version 3, each the one before with
random mutations of its norms (tests/norms_amend_diff.py) and random
`on_amendment:` policies, and an amendment: block with a random
transitional rule for the contract (keep, switch, or switch with
in_states). A trace is a random trace of the example
(tests/test_norms_monitor.py) whose observations name the version in force
from random observations on."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import yaml

from cadl.parser import parse
from cadl.type_checker import type_check

from tests.norms_amend_diff import _contract, _mutate_new, _policies

EXAMPLE = Path(__file__).parent.parent / "examples" / "norms_robot_delivery.cadl"
STATES = ["Proposed", "Assigned", "Delivering"]


def _rule(rng: random.Random) -> dict:
    x = rng.random()
    if x < .3:
        return {"contract": "DELIVERY_SLA", "rule": "keep"}
    if x < .55:
        return {"contract": "DELIVERY_SLA", "rule": "switch"}
    states = sorted(rng.sample(STATES, rng.choice([1, 1, 2])), key=STATES.index)
    return {"contract": "DELIVERY_SLA", "rule": "switch", "in_states": states}


def _regimes(sos: dict, suspends) -> None:
    """Two regimes, NORMAL and STORM; `suspends` names the one that does not
    have DELIVERY_SLA in force (None: both have it)."""
    sos["transitions"] = [{"from": "NORMAL", "to": "STORM", "condition": "wind >= 20"},
                          {"from": "STORM", "to": "NORMAL", "condition": "wind < 20"}]
    sos["regimes"] = ({suspends: {"contracts": {"suspended": ["DELIVERY_SLA"]}}}
                      if suspends else {"NORMAL": {}})


def _text(doc: dict, version: int, rule: dict | None, suspends: Any = False) -> str:
    doc = copy.deepcopy(doc)
    sos = doc["sos"]
    if suspends is not False:
        _regimes(sos, suspends)
    sos["version"] = str(version)
    exts = sos.setdefault("extensions", [])
    if {"amendment": 0.1} not in exts:
        exts.append({"amendment": 0.1})
    block: dict = {}
    if version > 1:
        block["previous"] = str(version - 1)
        block["effective"] = {"period": version}
        block["transitional"] = {"default": "switch", "contracts": [rule]}
    else:
        block["transitional"] = {"default": "switch"}
    sos["amendment"] = block
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _valid(text: str):
    try:
        sos = parse(text)
    except Exception:
        return None
    return sos if type_check(sos).ok else None


# assume: and guarantee: items that a version may have instead of those of
# the example (make_run with items=True)
ITEMS = ["throughout(300s, DISPATCHER.is_operational == true)",
         "within(300s, ROBOT[i].status == Delivered)",
         "within(2s, ROBOT[i].status == Delivered)",
         "throughout(3s, DISPATCHER.is_operational == true)",
         "ROBOT[i].battery > 10", "DISPATCHER.is_operational == true"]


def _mutate_items(rng: random.Random, c: dict) -> None:
    for key in ("assume", "guarantee"):
        if rng.random() < .6:
            c[key] = [rng.choice(ITEMS) for _ in range(rng.choice([0, 1, 2, 2, 3]))]
            if not c[key]:
                del c[key]


def make_run(rng: random.Random, tries: int = 200, check=None, items: bool = False,
             regimes: bool = False, mixed: bool = False) -> tuple:
    """(the versions as parsed files, their texts, the rules): two or
    three versions; `check(soses)` may refuse a run (for a target);
    `items`: the versions also change their assume: and guarantee: items;
    `regimes`: every version has the regimes NORMAL and STORM, with a random
    one of them (or none) that does not have the contract in force;
    `mixed` (with `regimes`): a version may also have no regimes: (a run that
    uses it and one with regimes is refused, K.11)."""
    base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for _ in range(tries):
        log: list = []
        docs = [copy.deepcopy(base)]
        for _ in range(rng.choice([0, 0, 1])):
            _mutate_new(rng, _contract(docs[0]), log)
        if items:
            _mutate_items(rng, _contract(docs[0]))
        for _ in range(rng.choice([1, 1, 2])):
            new = copy.deepcopy(docs[-1])
            for _ in range(rng.randint(1, 3)):
                _mutate_new(rng, _contract(new), log)
            if items:
                _mutate_items(rng, _contract(new))
            _policies(rng, docs[-1], new, log)
            docs.append(new)
        rules = [None] + [_rule(rng) for _ in docs[1:]]
        susp = [rng.choice(["STORM", "STORM", None, "NORMAL"] + ([False] if mixed else []))
                if regimes else False for _ in docs]
        if mixed and susp[0] is not False and rng.random() < 0.5:
            susp[rng.randrange(len(susp))] = False
        texts = [_text(d, v + 1, r, x) for v, (d, r, x) in enumerate(zip(docs, rules, susp))]
        soses = [_valid(t) for t in texts]
        if any(s is None for s in soses):
            continue
        if check is not None and not check(soses):
            continue
        return soses, texts, rules
    raise AssertionError("no valid run")


def with_versions(rng: random.Random, lines: list, count: int) -> list:
    """The trace with the key `version` from random observations on."""
    recs = [json.loads(line) for line in lines if line.strip()]
    marks = sorted(rng.sample(range(len(recs) + 2), min(count - 1, len(recs) + 2)))
    v = 0
    out = []
    for k, rec in enumerate(recs):
        while v < count - 1 and v < len(marks) and marks[v] <= k:
            v += 1
        if v > 0 or rng.random() < 0.3:
            rec["version"] = str(v + 1)
        out.append(json.dumps(rec))
    return out


# --- the Solidity monitor with versions ------------------------------------------------

def sol_run_versions(lines: list, quantities: list) -> dict:
    """A trace that names versions (and regimes) as the calls of the
    Solidity monitor: setRegime and setVersion before the observation that
    names one."""
    from tests import norms_targets_support as S
    recs = [json.loads(line) for line in lines if line.strip()]
    run = S.sol_run([json.dumps({k: v for k, v in r.items() if k not in ("version", "regime")})
                     for r in recs], quantities)
    for st, rec in zip(run["steps"], recs):
        for key in ("version", "regime"):
            if key in rec:
                st[key] = rec[key]
    return run


def with_regimes(rng: random.Random, lines: list) -> list:
    """The trace with the key `regime` (NORMAL or STORM) at random observations."""
    out = []
    for line in lines:
        rec = json.loads(line)
        if rng.random() < 0.35:
            rec["regime"] = rng.choice(["NORMAL", "STORM"])
        out.append(json.dumps(rec))
    return out


def with_states(rng: random.Random, lines: list, p: float = 0.3) -> list:
    """The trace with the key `state` (a state of the lifecycle of the
    example) at more observations, so that the lifecycle state changes also
    while the contract is not in force."""
    from tests.test_norms_monitor import _ROBOT
    out = []
    for line in lines:
        rec = json.loads(line)
        if "state" not in rec and rng.random() < p:
            rec["state"] = rng.choice(_ROBOT["states"])
        out.append(json.dumps(rec))
    return out


def python_monitor(soses: list, tmp: Path, name: str):
    """(the norms monitor module of the Python target for the first version,
    the models of the other versions) of a run."""
    import importlib.util
    import sys
    from cadl.codegen.norms_monitor_gen import build_model, generate_norms_monitor_module
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / f"{name}.py"
    path.write_text(generate_norms_monitor_module(soses[0]), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    models = [json.loads(json.dumps(build_model(s))) for s in soses[1:]]
    return mod, models


def _change(c: dict) -> list:
    if c["kind"] == "norm":
        return ["norm", c["id"], c["previous_state"], c["previous_verdict"], c["state"],
                c["verdict"]]
    field = "status" if c["kind"] == "assumption" else "verdict"
    return [c["kind"], c["index"], c["previous"], c[field]]


def reference_monitor(mod, models: list, lines: list) -> dict:
    """The steps of the norms monitor of the Python target with run_versions
    (`mod`, `models`, see python_monitor) on a trace, observation by
    observation, as reference_versions gives them; the end of the instance at
    the last observation is part of its step. {"error": text} when the
    monitor refuses the trace (replay)."""
    from tests import norms_targets_support as S
    recs = [json.loads(line) for line in lines if line.strip()]
    try:
        mod.replay(lines, run_versions=models)
    except Exception as e:                    # the trace is refused
        return {"error": str(e), "never": "is not in force at any observation" in str(e)}
    from cadl.norms_eval import declared_actors  # noqa: F401  (the engine's module)
    probe = mod.NormsMonitor(run_versions=models)
    count = 0
    for rec in recs:
        probe.observe(rec)
        count += 1
        if probe.ended:
            break
    names = sorted(probe._quantities) if probe._quantities else []
    trace = mod.parse_trace_versions(lines, chain=probe._chain,
                                     bindings=mod.chain_regimes(probe._chain))
    inside = trace.observations[:count]
    names = sorted(inside[-1].values) if inside else []
    m = mod.NormsMonitor(quantities=inside[-1].values if inside else (), run_versions=models)
    steps = []
    counts = [0, 0, 0, 0]
    last = len(recs) - 1

    def snap(changes: list) -> dict:
        eng = m._engine
        if eng is None:
            out = {"norms": {}, "assume": [], "guarantee": [], "violations": [], "events": [],
                   "unauthorised": [], "powers": []}
        else:
            out = {
                "norms": {x.d.id: [x.state, x.verdict] for x in eng.norms},
                "assume": [S._item_mark("assume", i) for i in eng.items["assume"]],
                "guarantee": [S._item_mark("guarantee", i) for i in eng.items["guarantee"]],
                "violations": [[v["norm"], v["verdict"], v["reparation_activated"],
                                v["transition"], v["severity"], v["sanction"]]
                               for v in eng.violations[counts[0]:]],
                "events": [[e["name"], e["performer"]] for e in eng.inst_events[counts[1]:]],
                "unauthorised": [[u["act"], u["performer"]] for u in eng.unauthorised[counts[2]:]],
                "powers": [],
            }
            for entry in eng.timeline[counts[3]:]:
                if entry["kind"] == "power":
                    p = S._POWER_RE.match(entry["text"])
                    out["powers"].append([p.group(2), p.group(1), p.group(3)])
            counts[:] = [len(eng.violations), len(eng.inst_events), len(eng.unauthorised),
                         len(eng.timeline)]
        out["changes"] = sorted((_change(c) for c in changes), key=json.dumps)
        out["next_deadline"] = S._round(m.next_deadline()) if m.started else None
        out["ended"] = m.ended
        return out

    out_steps = 0
    for k, rec in enumerate(recs):
        changes = m.observe(rec)
        out_steps += 0 if m.in_force else 1
        st = snap(changes)
        if k == last and not m.ended and rec.get("end") is not False:
            f = snap(m.end())
            f["violations"] = st["violations"] + f["violations"]
            for key in ("events", "unauthorised", "powers"):
                f[key] = st[key] + f[key]
            f["changes"] = S._merge_v(st["changes"], f["changes"])
            st = f
        steps.append(st)
        if m.ended:
            break
    tracker = m._tracker
    return {"steps": steps, "quantities": names, "out": out_steps,
            "follows": tracker is not None and tracker.follows != tracker.started,
            "effects": sorted({e["effect"] for a in (m._engine.amendments if m._engine else [])
                               for e in a["effects"]})}


def _sol_snapshot(r: dict) -> dict:
    """A result of the EVM as a snapshot of reference_versions: the norms by
    the ids the monitor has after the step; 255 is no state (a norm new in
    the version, or removed by it)."""
    from tests import norms_targets_support as S
    if "error" in r:
        return {"error": r["error"]}
    states = S.STATES + [None] * (256 - len(S.STATES))
    verdicts = S.VERDICTS + [None] * (256 - len(S.VERDICTS))
    snap = S.sol_snapshot(dict(r, logs=[x for x in r["logs"] if x[0] != "NormChanged"]),
                          r["ids"], set())
    for name, vals in r["logs"]:
        if name == "NormChanged":
            snap["changes"].append(["norm", vals[1], states[vals[4]], verdicts[vals[5]],
                                    states[vals[2]], verdicts[vals[3]]])
    snap["changes"] = sorted(snap["changes"], key=json.dumps)
    return snap


def sol_results_versions(raw: list, runs: list) -> list:
    """The EVM results as the snapshots `compare_versions` reads."""
    out = []
    for results, run in zip(raw, runs):
        snaps = [_sol_snapshot(r) for r in results]
        if run["steps"] and "finish" in run["steps"][-1] and len(results) == len(run["steps"]):
            out.append({"steps": snaps[:-1], "finish": snaps[-1]})
        else:
            out.append({"steps": snaps, "finish": None})
    return out


def compare_never(refs: list, got: list) -> list:
    """compare_versions; for a trace that never has the contract in force
    and does not end the instance (an error of the reference, which only the
    end of the trace shows), the monitor reports nothing and refuses
    nothing."""
    from tests import norms_targets_support as S
    bad = []
    for i, (r, g) in enumerate(zip(refs, got)):
        if r.get("never") and (any(st.get("error") for st in g["steps"])
                               or any(st["changes"] or st["ended"] for st in g["steps"])):
            bad.append((i, "never", g))
    return bad + S.compare_versions([{"steps": []} if r.get("never") else r for r in refs], got)
