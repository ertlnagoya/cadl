"""Pairs of versions, traces with an amendment, and the comparison of
`cadl norms-diff` with what `cadl norms-eval` does at the amendment
(Appendix F, F.6). Not a test module: tests/test_norms_amend_diff.py uses
it, and it can be run as a script for the long run.

A pair of versions is made from a bundled norms example: the old version
gets random `on_amendment:` policies, and the new version is the old one
with random mutations of its norms (deadlines and contents changed,
kinds changed, norms added and removed). A trace is made of random
environment choices of the models of `cadl norms-check` (F.12): the
observations before the amendment from the model of the old version, the
amendment and the observations after it from the model of the new one.

At the amendment the engine of `cadl norms-eval` is stopped twice: right
after the new version takes effect (before step 1 of F.5) and after the
whole observation. For every norm instance the state and the time
elapsed against its deadline are read and compared with the prediction
that `predict` derives from the findings of `diff_versions` alone and the
states before the amendment.
"""

from __future__ import annotations

import copy
import json
import math
import random
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import yaml

from cadl import norms_eval
from cadl.norms_check import Environment, Translator, _parse_domains, random_paths, trace_lines
from cadl.norms_diff import diff_versions
from cadl.norms_eval import FINAL_STATES, _Engine, declared_actors, evaluate, parse_trace, select_contract
from cadl.parser import parse
from cadl.type_checker import type_check

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"

# The bundled examples with norms, and the bindings and members of the
# models whose random choices make the traces.
BASES = [
    ("norms_robot_delivery.cadl", {"bindings": {"i": 1}}),
    ("norms_amendment_v1.cadl", {"bindings": {"i": 1}}),
    ("norms_amendment_v2.cadl", {"bindings": {"i": 1}}),
    ("norms_no_double_assignment.cadl", {"bindings": {"i": 1}}),
    ("norms_check_conflict.cadl", {"bindings": {"i": 1}}),
    ("norms_check_instances.cadl", {"bindings": {"i": 1}}),
    ("norms_check_value_types.cadl", {"bindings": {"i": 1}}),
    ("norms_check_quantifiers.cadl", {"members": {"ROBOT": 2}}),
    ("norms_check_arithmetic.cadl", {"members": {"ROBOT": 2},
                                     "domains": ["ROBOT[*].load=0..3", "DEPOT.capacity=2..4"]}),
]

DEADLINES = ["1s", "2s", "3s", "4s", "6s", "10s"]


# --- Versions ---------------------------------------------------------------------

def _contract(doc: dict) -> dict:
    cs = doc["sos"]["contracts"]
    return next(c for c in cs if c.get("norms"))


def _refs(c: dict) -> set:
    """Norm ids that other parts of the contract name."""
    out = set()
    for n in c.get("norms", []):
        ov = n.get("on_violation") or {}
        if ov.get("reparation"):
            out.add(ov["reparation"])
        for ids in (n.get("effects") or {}).values():
            out.update(ids or [])
    return out


def _mutate_new(rng: random.Random, c: dict, log: list, k: int | None = None) -> None:
    norms = c["norms"]
    k = rng.choice([0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8]) if k is None else k
    timed = [n for n in norms if n["kind"] in ("obligation", "prohibition")]
    if k == 0 and timed:                                   # deadline changed
        n = rng.choice(timed)
        if n["kind"] == "prohibition" and "deadline" in n and rng.random() < .25:
            del n["deadline"]
        else:
            n["deadline"] = rng.choice(["1s"] + DEADLINES)
        log.append(f"deadline of {n['id']}: {n.get('deadline')}")
    elif k == 1 and len(norms) > 1:                        # content changed
        n, m = rng.sample(norms, 2)
        if n["kind"] != "power" and m["kind"] != "power":
            n["content"] = m["content"]
            log.append(f"content of {n['id']} from {m['id']}")
    elif k == 2:                                           # norm added
        n = copy.deepcopy(rng.choice(norms))
        n["id"] = f"added_{len(norms)}_{rng.randrange(100)}"
        n.pop("on_amendment", None)
        if rng.random() < .4:
            n.pop("trigger", None)
        elif not n.get("trigger"):
            n["trigger"] = rng.choice([m for m in norms if m.get("trigger")] or [n]).get(
                "trigger") or n["content"]
        if n["kind"] == "power":
            n.pop("effects", None)
            n["kind"] = "permission"
        norms.append(n)
        log.append(f"added {n['id']}")
    elif k == 3 and len(norms) > 1:                        # norm removed
        free = [n for n in norms if n["id"] not in _refs(c)]
        if rng.random() < .6:
            free = [n for n in free if n.get("trigger")] or free
        if free:
            n = rng.choice(free)
            norms.remove(n)
            log.append(f"removed {n['id']}")
    elif k == 4 and timed:                                 # kind changed
        n = rng.choice(timed)
        n["kind"] = "prohibition" if n["kind"] == "obligation" else "obligation"
        n.pop("on_violation", None) if n["kind"] == "prohibition" else None
        log.append(f"kind of {n['id']}: {n['kind']}")
    elif k == 5:                                           # trigger removed or added
        n = rng.choice(norms)
        if n.get("trigger"):
            n.pop("trigger")
            log.append(f"trigger of {n['id']} removed")
    elif k == 6:                                           # a new reparation
        duties = [n for n in norms if n["kind"] == "obligation"]
        if duties:
            n = rng.choice(duties)
            rep = {"id": f"repair_{rng.randrange(1000)}", "kind": "obligation",
                   "bearer": n["bearer"], "content": n["content"],
                   "deadline": rng.choice(DEADLINES)}
            n.setdefault("on_violation", {})["reparation"] = rep["id"]
            norms.append(rep)
            log.append(f"added {rep['id']} as the reparation of {n['id']}")
    elif k == 7:                                           # a power that suspends
        msgs = [n for n in norms if "->" in str(n.get("content", ""))]
        others = [n for n in norms if n["kind"] != "power"]
        if msgs and others:
            m = rng.choice(msgs)
            sender = m["content"].split("->")[0].strip()
            target = rng.choice(others)["id"]
            p = {"id": f"pause_{rng.randrange(1000)}", "kind": "power", "bearer": sender,
                 "content": m["content"],
                 "effects": {rng.choice(["suspend", "suspend", "resume"]): [target]}}
            norms.append(p)
            log.append(f"added the power {p['id']} over {target}")
    else:                                                  # a deadline of a new norm
        n = rng.choice(norms)
        if n["kind"] in ("obligation", "prohibition"):
            n["deadline"] = rng.choice(DEADLINES)
            log.append(f"deadline of {n['id']}: {n['deadline']}")


def _policies(rng: random.Random, old: dict, new: dict, log: list) -> None:
    """Random on_amendment: of the old version, with transform targets that
    the new version has, or not."""
    new_ids = [n["id"] for n in _contract(new)["norms"]]
    for n in _contract(old)["norms"]:
        x = rng.random()
        if x < .3:
            n["on_amendment"] = "discharge"
        elif x < .55:
            target = rng.choice(new_ids + ["nowhere"]) if rng.random() < .9 else "nowhere"
            n["on_amendment"] = {"transform": target}
        elif x < .7:
            n["on_amendment"] = "carry_over"
        else:
            n.pop("on_amendment", None)
        if "on_amendment" in n:
            log.append(f"{n['id']}: on_amendment {n['on_amendment']}")


def _valid(doc: dict):
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    try:
        sos = parse(text)
    except Exception:
        return None
    if not type_check(sos).ok:
        return None
    return sos


def make_pair(rng: random.Random, tries: int = 100):
    """(name, kw, old sos, new sos, log) for a random base example."""
    for _ in range(tries):
        name, kw = rng.choice(BASES)
        base = yaml.safe_load((EXAMPLES / name).read_text())
        old = copy.deepcopy(base)
        log: list = []
        for _ in range(rng.choice([0, 1, 2])):             # the old version mutated too
            _mutate_new(rng, _contract(old), log, 7 if rng.random() < .5 else None)
        new = copy.deepcopy(old)
        for _ in range(rng.randint(1, 4)):
            _mutate_new(rng, _contract(new), log)
        _policies(rng, old, new, log)
        old_sos, new_sos = _valid(old), _valid(new)
        if old_sos is None or new_sos is None:
            continue
        old_ids = [n.id for n in select_contract(old_sos).norms]
        # a norm whose kind changes is not translated by norms-check: the
        # models are built per version, so this does not restrict the pair
        return name, kw, old_sos, new_sos, log, old_ids
    raise AssertionError("no valid pair of versions")


# --- Traces -----------------------------------------------------------------------

def _translator(sos, kw, step_ms=None, horizon=None):
    """The environment choices of the model of `cadl norms-check` (F.12)
    for one version, without the model itself."""
    tr = Translator(sos, select_contract(sos), kw.get("bindings", {}), kw.get("members", {}),
                    step_ms, horizon, _parse_domains(kw.get("domains")))
    Environment([tr], step_ms, horizon)
    return tr


def make_trace(rng: random.Random, old_sos, new_sos, kw) -> tuple[list[str], int]:
    """Trace lines with the amendment at a random observation k >= 1."""
    m_old = _translator(old_sos, kw)
    m_new = _translator(new_sos, kw)
    if m_old.step_ms != m_new.step_ms:                    # one step for both
        step = math.gcd(m_old.step_ms, m_new.step_ms)
        horizon = max(m_old.horizon, m_new.horizon) * max(m_old.step_ms, m_new.step_ms) // step
        m_old = _translator(old_sos, kw, step, min(horizon, 40))
        m_new = _translator(new_sos, kw, step, min(horizon, 40))
    seed = rng.randrange(10 ** 9)
    p = dict(p_message=rng.choice([.15, .3, .5]), p_keep=rng.choice([0, .5, .8]),
             full_length=True)
    a = trace_lines(m_old, random_paths(m_old, 1, seed=seed, **p)[0])
    b = trace_lines(m_new, random_paths(m_new, 1, seed=seed + 1, **p)[0])
    length = min(len(a), len(b))
    k = rng.randint(1, length - 1)
    lines = []
    for n in range(length):
        obs = json.loads(a[n] if n < k else b[n])
        obs.pop("end", None)
        if n == 0:
            obs["bindings"] = json.loads(a[0]).get("bindings", {})
        if n == k:
            obs["amend"] = "new"
        lines.append(json.dumps(obs))
    return lines, k


# --- What norms-eval does ---------------------------------------------------------

def _snap(engine, t) -> dict:
    return {x.d.id: {"state": x.state, "kind": x.d.kind, "elapsed": x.elapsed(t),
                     "frozen": x.frozen is not None, "deadline": x.deadline}
            for x in engine.norms}


def run_at_amendment(old_sos, new_sos, lines: list[str], k: int) -> dict:
    """The states of the norm instances before the amendment at observation
    k, right after the new version takes effect, and after observation k;
    and the result of the run, which must equal `cadl norms-eval`."""
    trace = parse_trace(lines)
    contract = select_contract(old_sos)
    engine = _Engine(contract, trace, lambda name: new_sos, declared_actors(old_sos))
    t = trace.observations[k].t
    out: dict = {}
    original = engine._amend

    def amend(n, name):
        out["before"] = _snap(engine, t)
        original(n, name)
        out["after_amend"] = _snap(engine, t)
    engine._amend = amend
    last = len(trace.observations) - 1
    for n in range(last + 1):
        if engine._step(n, n == last):
            break
        if n == k:
            out["after_step"] = _snap(engine, t)
    engine._finish_items()
    out["result"] = engine.result()
    out["effects"] = engine.amendments[0]["effects"] if engine.amendments else []
    return out


# --- What norms-diff reports, and the prediction from it -----------------------------

def predict(diff: dict, contract_id: str, before: dict, old_ids: list) -> dict:
    """The states right after the amendment, from the findings of
    `diff_versions` and the states before it: per norm id, the expected
    state, elapsed time (None: not checked) and whether a late carried-over
    norm is to be judged at the amendment."""
    findings = next(c["findings"] for c in diff["contracts"] if c["id"] == contract_id)
    by: dict = {}
    for f in findings:
        if "norm" in f:
            by.setdefault(f["norm"], {})[f["kind"]] = f
    out: dict = {}
    transforms = []
    for nid in old_ids:
        b = before[nid]
        fs = by.get(nid, {})
        s = b["state"]
        if s in FINAL_STATES:
            out[nid] = {"state": s, "elapsed": b["elapsed"]}
            continue
        if s == "inactive":
            if "norm_removed" in fs:
                out[nid] = {"state": None}
            else:
                out[nid] = {"state": "inactive", "elapsed": Fraction(0)}
            continue
        # active or suspended: on_amendment of the old version
        if "discharge" in fs:
            out[nid] = {"state": "discharged"}
        elif "transform" in fs or "transform_target_missing" in fs:
            out[nid] = {"state": "discharged"}
            if "transform" in fs and not b["frozen"]:
                f = fs["transform"]
                transforms.append((f["target"], f["target_kind"], b["elapsed"]))
        elif "norm_removed" in fs or "kind_changed" in fs:
            out[nid] = {"state": "discharged"}
        else:
            exp = {"state": s, "elapsed": b["elapsed"]}
            ch = (fs.get("carry_over_changed") or {}).get("changes", {})
            deadline = b["deadline"]
            if "deadline" in ch:
                new_s = ch["deadline"]["new_s"]
                deadline = None if new_s is None else Fraction(Decimal(new_s))
            if deadline is not None and not b["frozen"] and b["elapsed"] > deadline:
                if b["kind"] == "obligation":
                    exp["state"] = "violated"
                elif b["kind"] == "prohibition":
                    exp["state"] = "fulfilled"
            out[nid] = exp
    added = [f for f in findings if f["kind"] == "norm_added"]
    for f in added:
        out[f["norm"]] = {"state": "inactive", "elapsed": Fraction(0)}
    for target, kind, elapsed in transforms:
        x = out.get(target)
        if x is not None and x["state"] == "inactive":
            timed = kind in ("obligation", "prohibition")
            out[target] = {"state": "active", "elapsed": elapsed if timed else Fraction(0),
                           "transformed": True}
    for f in added:
        x = out[f["norm"]]
        if f["active_at_once"] and x["state"] == "inactive":
            out[f["norm"]] = {"state": "active", "elapsed": Fraction(0)}
    return out


def compare(name, old_sos, new_sos, lines, k, old_ids) -> list[str]:
    """The differences between the prediction from norms-diff and what
    norms-eval does at the amendment."""
    diff = diff_versions(old_sos, new_sos)
    contract_id = select_contract(old_sos).id
    run = run_at_amendment(old_sos, new_sos, lines, k)
    full = evaluate(old_sos, parse_trace(lines), versions={"new": new_sos})
    bad = []
    if json.dumps(full, sort_keys=True, default=str) != json.dumps(
            run["result"], sort_keys=True, default=str):
        bad.append("the stepped engine differs from norms-eval")
    if "before" not in run:
        return bad + ["the amendment was not applied"]
    exp = predict(diff, contract_id, run["before"], old_ids)
    after = run["after_amend"]
    for nid in sorted(set(exp) | set(after)):
        e, a = exp.get(nid), after.get(nid)
        if e is None:
            bad.append(f"{nid}: norms-eval has {a['state']}, norms-diff predicts no instance")
            continue
        if a is None:
            if e["state"] is not None:
                bad.append(f"{nid}: norms-diff predicts {e['state']}, norms-eval has no instance")
            continue
        if a["frozen"]:
            continue
        if e["state"] != a["state"]:
            bad.append(f"{nid}: norms-diff predicts {e['state']}, norms-eval has {a['state']} "
                       f"(before: {run['before'].get(nid, {}).get('state')})")
        elif "elapsed" in e and e["state"] in ("active", "suspended") \
                and e["elapsed"] != a["elapsed"]:
            bad.append(f"{nid}: elapsed {a['elapsed']} after the amendment, "
                       f"expected {e['elapsed']}")
    return bad


def run_random(seed: int, count: int) -> dict:
    rng = random.Random(seed)
    stats = {"pairs": 0, "states": {}, "effects": {}}
    for _ in range(count):
        name, kw, old_sos, new_sos, log, old_ids = make_pair(rng)
        lines, k = make_trace(rng, old_sos, new_sos, kw)
        bad = compare(name, old_sos, new_sos, lines, k, old_ids)
        if bad:
            raise AssertionError({"base": name, "mutations": log, "k": k, "bad": bad,
                                  "trace": lines})
        stats["pairs"] += 1
        run = run_at_amendment(old_sos, new_sos, lines, k)
        for e in run["effects"]:
            key = f"{e['effect']}:{e['from_state']}->{e['to_state']}"
            stats["effects"][key] = stats["effects"].get(key, 0) + 1
    return stats


if __name__ == "__main__":
    import sys
    print(run_random(int(sys.argv[1]) if len(sys.argv) > 1 else 0,
                     int(sys.argv[2]) if len(sys.argv) > 2 else 50))
