"""Differential testing of the Unity C# target for a contract that some
regime does not have in force (regimes:, Appendix A, A.7.1; F.5,
"Contracts not in force"), for tests/test_unity_csharp_codegen.py.

The oracle is the clock trace: a run with regimes gives the events of the
same generated code without regimes on the projection of the run, the
steps at which the contract is in force with their times on the clock of
the contract (the time of the run without the time not in force), and
each event of that run with its time moved back to the time of the run.
Both runs are compiled with Mono (mcs) and run once per set of runs.
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

from cadl.codegen import generate
from cadl.codegen.unity_csharp.contract_emitter import _csharp_class_name

EVENTS = ["DISPATCHER -> ROBOT[i] : route_assignment",
          "ROBOT[i] -> DISPATCHER : ack(accepted)",
          "ROBOT[i].status == InTransit", "ROBOT[i].status == Delivered",
          "now > request.deadline", "bogus"]
# the evaluator walks a dotted name through nested dictionaries
WORLD = [("ROBOT.battery", [10, 50]), ("now", [1, 5]), ("request.deadline", [3])]


def random_run(rng: random.Random, regimes: list, steps: int = 30) -> list:
    """Steps: ["tick", t], ["event", name, t], ["world", key, value], ["regime", r, t]."""
    out, t = [], 0
    for _ in range(steps):
        t += rng.choice([0, 50, 100, 250, 600, 1000, 3000])
        x = rng.random()
        if x < .35:
            out.append(["tick", t])
        elif x < .7:
            out.append(["event", rng.choice(EVENTS), t])
        elif x < .8:
            key, vals = rng.choice(WORLD)
            out.append(["world", key, rng.choice(vals)])
        else:
            out.append(["regime", rng.choice(regimes), t])
    return out


def project(run: list, out_regimes: set, initial: str) -> tuple:
    """(the steps in force with clock times, the offset of each of them)."""
    regime, offset, out_since = initial, 0, None
    if regime in out_regimes:
        out_since = 0
    steps, offsets = [], []
    for st in run:
        if st[0] == "world":
            steps.append(st)
            offsets.append(offset)
            continue
        t = st[-1]
        if st[0] == "regime":
            regime = st[1]
            if regime in out_regimes and out_since is None:
                out_since = t
            elif regime not in out_regimes and out_since is not None:
                offset += t - out_since
                out_since = None
            continue
        if out_since is not None:
            continue
        steps.append(st[:-1] + [t - offset])
        offsets.append(offset)
    return steps, offsets


def _cs(s: str) -> str:
    return json.dumps(s)


def main_cs(contract_id: str, runs: list, with_regimes: bool) -> str:
    cls = _csharp_class_name(contract_id)
    body = []
    for run in runs:
        body.append("        { var rt = new ContractRuntime();")
        body.append("          rt.Events.Subscribe(e => Console.WriteLine(\"E|\" + e.Kind + \"|\" "
                    "+ e.TransitionId + \"|\" + e.FromState + \"|\" + e.ToState + \"|\" + e.Norm "
                    "+ \"|\" + e.DetectedBy + \"|\" + e.TMs));")
        if with_regimes:
            body.append("          var rg = new CadlRegimes(rt);")
            body.append(f"          var c = new {cls}Contract(\"i1\", rt, 0, rg);")
        else:
            body.append(f"          var c = new {cls}Contract(\"i1\", rt, 0);")
        body.append("          rt.Register(c); Console.WriteLine(\"S\");")
        for st in run:
            if st[0] == "tick":
                body.append(f"          rt.Tick({st[1]}L); Console.WriteLine(\"S\");")
            elif st[0] == "event":
                body.append(f"          rt.PostEvent({_cs(st[1])}, {st[2]}L); Console.WriteLine(\"S\");")
            elif st[0] == "world":
                head, _, member = st[1].partition(".")
                value = f"(object){st[2]}L"
                if member:
                    value = (f"(object)new Dictionary<string, object>{{{{{_cs(member)}, "
                             f"{value}}}}}")
                body.append(f"          rt.UpdateWorld(new Dictionary<string, object>{{{{{_cs(head)}, "
                            f"{value}}}}}); Console.WriteLine(\"S\");")
            else:
                body.append(f"          rg.Set({_cs(st[1])}, {st[2]}L);")
        body.append("          Console.WriteLine(\"R\"); }")
    return ("using System; using System.Collections.Generic; using CADL.SosDsl;\n"
            "class M { static void Main() {\n" + "\n".join(body) + "\n}}\n")


def run_mono(sos, contract_id: str, runs: list, with_regimes: bool, tmp: Path) -> list:
    """Per run: the events of the construction, then per step that is not a
    regime change the events it emitted."""
    out_dir = tmp / "gen"
    generate(sos, out_dir, target="unity-csharp")
    main = tmp / "Main.cs"
    main.write_text(main_cs(contract_id, runs, with_regimes), encoding="utf-8")
    exe = tmp / "m.exe"
    sources = [str(p) for p in sorted(out_dir.rglob("*.cs"))]
    subprocess.run(["mcs", "-nologo", "-langversion:experimental", f"-out:{exe}", str(main), *sources], check=True,
                   capture_output=True, text=True)
    lines = subprocess.run(["mono", str(exe)], check=True, capture_output=True,
                           text=True).stdout.splitlines()
    results, run, step = [], [], []
    for line in lines:
        if line.startswith("E|"):
            step.append(line.split("|")[1:])
        elif line == "S":
            run.append(step)
            step = []
        elif line == "R":
            results.append(run)
            run, step = [], []
    return results


def compare(sos_regimes, sos_plain, contract_id: str, runs: list, out_regimes: set,
            initial: str, tmp: Path) -> tuple:
    """(mismatches, steps compared, steps left out)."""
    got = run_mono(sos_regimes, contract_id, runs, True, tmp / "r")
    projected = [project(r, out_regimes, initial) for r in runs]
    want_raw = run_mono(sos_plain, contract_id, [p[0] for p in projected], False, tmp / "p")
    bad, compared, left = [], 0, 0
    for k, (run, g, (steps, offsets), w) in enumerate(zip(runs, got, projected, want_raw)):
        if g[0] != w[0]:
            bad.append((k, "open", g[0], w[0]))
            continue
        # the steps of the run that are not regime changes, in order, and
        # whether each is in force
        regime, out_since = initial, (0 if initial in out_regimes else None)
        j = 0
        gi = 1
        for st in run:
            if st[0] == "regime":
                regime = st[1]
                t = st[2]
                if regime in out_regimes and out_since is None:
                    out_since = t
                elif regime not in out_regimes and out_since is not None:
                    out_since = None
                continue
            have = g[gi]
            gi += 1
            if st[0] != "world" and out_since is not None:
                left += 1
                if have:
                    bad.append((k, "not in force", st, have))
                    break
                continue
            want = [e[:-1] + [str(int(e[-1]) + offsets[j])] for e in w[1 + j]]
            j += 1
            compared += 1
            if have != want:
                bad.append((k, st, have, want))
                break
    return bad, compared, left


# === Versions with regime bindings (Appendix K, K.11, "Versions and regimes") =====

TOWN_EVENTS = ["ROBOT[i] -> TOWN_STEWARD : node_released", "bogus"]
TOWN_WORLD = [("ROBOT.blocking", [0, 0, 2])]


def random_version_run(rng: random.Random, regimes: list, versions: list,
                       steps: int = 30) -> tuple:
    """(the version in force at the start, the steps): ["tick", t],
    ["event", name, t], ["world", key, value], ["regime", r, t] and
    ["version", name, t] (the versions in order, each at most once)."""
    start = rng.randrange(len(versions)) if rng.random() < 0.3 else 0
    later = list(versions[start + 1:])
    out, t = [], 0
    for _ in range(steps):
        t += rng.choice([0, 500, 1000, 5000, 10000, 20000, 40000])
        x = rng.random()
        if x < .3:
            out.append(["tick", t])
        elif x < .55:
            out.append(["event", rng.choice(TOWN_EVENTS), t])
        elif x < .65:
            key, vals = rng.choice(TOWN_WORLD)
            out.append(["world", key, rng.choice(vals)])
        elif x < .85 or not later:
            out.append(["regime", rng.choice(regimes), t])
        else:
            out.append(["version", later.pop(0), t])
    return versions[start], out


def project_versions(start: str, run: list, versions: list, out_of: dict, initial: str) -> tuple:
    """The run of the generated code without regimes that a run with
    versions and regimes is equal to: (its steps, per step of the run the
    indices of the steps of that run it gives, the offsets of those steps).
    `out_of[v]`: the regimes in which version v does not have the contract
    in force. The instance starts at the first time the contract is in force,
    under the version in force then; the steps not in force are left out; a
    version that takes effect while the contract is not in force is applied
    when it is in force again, and the switch it makes is reported at the time
    at which it took effect (K.11, "Versions and regimes", item 4; the offset
    of that step is then (offset, cause, time)); the times are on the clock of
    the contract."""
    vin, regime, offset = start, initial, 0
    out = regime in out_of[vin]
    out_since = 0 if out else None
    started = not out
    steps, offsets = [], []
    per_step = []          # per step of the run (the first is the construction)
    waiting: list = []

    def add(st, off):
        steps.append(st)
        offsets.append(off)
        return len(steps) - 1

    per_step.append([add(["new", vin, 0], 0)] if started else [])

    def follow(t, mine):
        nonlocal out, out_since, offset, started
        o = regime in out_of[vin]
        if o == out:
            return
        if o:
            out, out_since = True, t
            return
        out = False
        offset += t - out_since
        out_since = None
        c = t - offset
        if not started:
            started = True
            waiting.clear()
            mine.append(add(["new", vin, c], offset))
            return
        for name, tv in waiting:
            mine.append(add(["version", name, c], (offset, "version " + name, tv)))
        waiting.clear()

    for st in run:
        mine: list = []
        if st[0] == "world":
            mine.append(add(st, offset))
        elif st[0] == "regime":
            regime = st[1]
            follow(st[2], mine)
        elif st[0] == "version":
            vin = st[1]
            if not started:
                follow(st[2], mine)
            elif out or regime in out_of[vin]:
                waiting.append((st[1], st[2]))
                follow(st[2], mine)
            else:
                mine.append(add(["version", st[1], st[2] - offset], offset))
        elif not out and started:
            mine.append(add(st[:-1] + [st[-1] - offset], offset))
        per_step.append(mine)
    return steps, per_step, offsets


def main_versions_cs(contract_id: str, runs: list, with_regimes: bool) -> str:
    """runs: (the version at the start, steps); with_regimes: the steps of the
    run with regimes (the construction first), else the projected steps."""
    cls = _csharp_class_name(contract_id)
    body = []
    for start, run in runs:
        body.append("        { var rt = new ContractRuntime();")
        body.append("          rt.Events.Subscribe(e => Console.WriteLine(\"E|\" + e.Kind + \"|\" "
                    "+ e.TransitionId + \"|\" + e.FromState + \"|\" + e.ToState + \"|\" + e.Norm "
                    "+ \"|\" + e.DetectedBy + \"|\" + e.Cause + \"|\" + e.TMs));")
        body.append(f"          {cls}Contract c = null;")
        if with_regimes:
            body.append(f"          var rg = new CadlRegimes(rt, {_cs(start)});")
            body.append(f"          c = new {cls}Contract(\"i1\", rt, 0, {_cs(start)}, rg);")
            body.append("          rt.Register(c); Console.WriteLine(\"S\");")
        for st in run:
            if st[0] == "new":
                body.append(f"          c = new {cls}Contract(\"i1\", rt, {st[2]}L, {_cs(st[1])}); "
                            "rt.Register(c);")
            elif st[0] == "tick":
                body.append(f"          rt.Tick({st[1]}L);")
            elif st[0] == "event":
                body.append(f"          rt.PostEvent({_cs(st[1])}, {st[2]}L);")
            elif st[0] == "world":
                head, _, member = st[1].partition(".")
                value = f"(object){st[2]}L"
                if member:
                    value = (f"(object)new Dictionary<string, object>{{{{{_cs(member)}, "
                             f"{value}}}}}")
                body.append(f"          rt.UpdateWorld(new Dictionary<string, object>{{{{{_cs(head)}, "
                            f"{value}}}}});")
            elif st[0] == "regime":
                body.append(f"          rg.Set({_cs(st[1])}, {st[2]}L);")
            else:
                body.append(f"          c.SetVersion({_cs(st[1])}, {st[2]}L, rt);")
            body.append("          Console.WriteLine(\"S\");")
        body.append("          Console.WriteLine(\"R\"); }")
    return ("using System; using System.Collections.Generic; using CADL.SosDsl;\n"
            "class M { static void Main() {\n" + "\n".join(body) + "\n}}\n")


def run_mono_versions(soses: list, contract_id: str, runs: list, with_regimes: bool,
                      tmp: Path) -> list:
    """Per run, per step, the events it emitted."""
    out_dir = tmp / "gen"
    generate(soses[0], out_dir, target="unity-csharp", versions=soses[1:])
    main = tmp / "Main.cs"
    main.write_text(main_versions_cs(contract_id, runs, with_regimes), encoding="utf-8")
    exe = tmp / "m.exe"
    sources = [str(p) for p in sorted(out_dir.rglob("*.cs"))]
    subprocess.run(["mcs", "-nologo", "-langversion:experimental", f"-out:{exe}", str(main),
                    *sources], check=True, capture_output=True, text=True)
    lines = subprocess.run(["mono", str(exe)], check=True, capture_output=True,
                           text=True).stdout.splitlines()
    results, run, step = [], [], []
    for line in lines:
        if line.startswith("E|"):
            step.append(line.split("|")[1:])
        elif line == "S":
            run.append(step)
            step = []
        elif line == "R":
            results.append(run)
            run, step = [], []
    return results


def _stamp(e: list, off) -> str:
    """The time of an event of the projected run on the time of the run: its
    time plus the offset of its step, or, for the switch of a version that
    took effect while the contract was not in force, the time it took effect."""
    if isinstance(off, tuple):
        off, cause, tv = off
        if e[1] == "<version>" and e[6] == cause:
            return str(tv)
    return str(int(e[-1]) + off)


def compare_versions(soses_regimes: list, soses_plain: list, contract_id: str, runs: list,
                     out_of: dict, initial: str, tmp: Path) -> tuple:
    """(mismatches, steps compared, steps not in force) of the generated code
    with versions and regimes against that without regimes on the projection."""
    names = [s.version for s in soses_plain]
    got = run_mono_versions(soses_regimes, contract_id, runs, True, tmp / "r")
    projected = [project_versions(start, run, names, out_of, initial) for start, run in runs]
    want_raw = run_mono_versions(soses_plain, contract_id,
                                 [(None, p[0]) for p in projected], False, tmp / "p")
    bad, compared, left = [], 0, 0
    for k, ((start, run), g, (steps, per_step, offsets), w) in enumerate(
            zip(runs, got, projected, want_raw)):
        for i, idx in enumerate(per_step):
            want = [e[:-1] + [_stamp(e, offsets[j])] for j in idx for e in w[j]]
            if not idx:
                left += 1
            compared += 1
            if g[i] != want:
                bad.append((k, i, ([None] + run)[i], g[i], want))
                break
    return bad, compared, left
