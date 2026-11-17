"""Versions while a system runs, from cadl 0.3.35 (Appendix K, K.11):
`previous:` inferred from the effective times, the key ``version`` of a
file without versions, versions together with regime bindings in
`cadl norms-eval` and the norms monitor of the Python target, the target
checks of `metrics.py` for entries from another version, and effective
times in `cadl agents-run` and the gateways."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadl.norms_eval import NormsEvalError, TimeBase, evaluate_files

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
TRACES = EXAMPLES / "traces"
V1 = EXAMPLES / "amendment_norms_v1.cadl"
V2 = EXAMPLES / "amendment_norms_v2.cadl"
HOUR = TimeBase(3600)


def _version(tmp: Path, name: str, version: str, previous: str | None, effective: str | None,
             transitional: str = "", pickup: str = "90s", base: Path = V2,
             extra: str = "") -> Path:
    """A version of amendment_norms with the given header (`previous` and
    `effective` may be left out) and rules."""
    text = base.read_text(encoding="utf-8")
    text = text.replace('version: "2"', f'version: "{version}"')
    head = text.index("  amendment:\n")
    tail = text.index("    layers:\n")
    block = "  amendment:\n"
    if previous is not None:
        block += f'    previous: "{previous}"\n'
    if effective is not None:
        block += f"    effective: {effective}\n"
    text = text[:head] + extra + block + transitional + text[tail:]
    text = text.replace("deadline: 90s", f"deadline: {pickup}")
    text = text.replace("        value: 90s", f"        value: {pickup}")
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


def _trace(tmp: Path, lines: list, name: str = "t.jsonl") -> Path:
    path = tmp / name
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def _norms(result: dict) -> dict:
    return {n["id"]: (n["state"], n["verdict"]) for n in result["norms"]}


def _same(a: dict, b: dict) -> bool:
    a, b = dict(a), dict(b)
    for r in (a, b):
        r.pop("file", None), r.pop("trace", None)
    return a == b


# --- A2: previous: inferred from the effective times --------------------------------

class TestPreviousInferred:
    SWITCH = TRACES / "amendment_norms_v1_a_switch.jsonl"

    def test_same_result_as_explicit_previous(self, tmp_path):
        v2 = _version(tmp_path, "v2.cadl", "2", None, "{ period: 2 }",
                      "    transitional:\n      contracts:\n        - { contract: DELIVERY_SLA, "
                      "rule: switch, in_states: [Assigned] }\n")
        v3 = _version(tmp_path, "v3.cadl", "3", None, "{ period: 3 }", pickup="60s")
        e2 = _version(tmp_path, "e2.cadl", "2", "1", "{ period: 2 }",
                      "    transitional:\n      contracts:\n        - { contract: DELIVERY_SLA, "
                      "rule: switch, in_states: [Assigned] }\n")
        e3 = _version(tmp_path, "e3.cadl", "3", "2", "{ period: 3 }", pickup="60s")
        for base in (HOUR, None):
            got = evaluate_files(V1, self.SWITCH, None, [v3, v2], base)
            ref = evaluate_files(V1, self.SWITCH, None, [e2, e3], base)
            assert got["versions"]["sequence"] == ["1", "2", "3"]
            assert _same(got, ref)

    def test_first_version_without_effective(self, tmp_path):
        first = _version(tmp_path, "f1.cadl", "1", None, None, base=V2)
        v2 = _version(tmp_path, "v2.cadl", "2", None, "{ period: 2 }")
        r = evaluate_files(first, self.SWITCH, None, [v2], HOUR)
        assert r["versions"]["sequence"] == ["1", "2"]

    def test_ambiguous_same_time(self, tmp_path):
        a = _version(tmp_path, "a.cadl", "2", "1", "{ period: 2 }")
        b = _version(tmp_path, "b.cadl", "2b", "1", "{ period: 2 }")
        c = _version(tmp_path, "c.cadl", "3", None, "{ period: 3 }")
        with pytest.raises(NormsEvalError, match="version 3 gives no previous:, and versions "
                                                 "2, 2b take effect at the same time"):
            evaluate_files(V1, self.SWITCH, None, [a, b, c], HOUR)

    def test_cannot_be_ordered(self, tmp_path):
        v2 = _version(tmp_path, "v2.cadl", "2", None, '{ at: "2026-01-01" }')
        with pytest.raises(NormsEvalError, match="cannot be ordered .* give previous:"):
            evaluate_files(V1, self.SWITCH, None, [v2])
        with pytest.raises(NormsEvalError, match="cannot be ordered"):
            evaluate_files(V1, self.SWITCH, None, [v2], HOUR)

    def test_explicit_previous_against_the_order_is_refused(self, tmp_path):
        v2 = _version(tmp_path, "v2.cadl", "2", "1", "{ period: 3 }")
        v3 = _version(tmp_path, "v3.cadl", "3", "2", "{ period: 2 }")
        with pytest.raises(NormsEvalError, match="not after version 2"):
            evaluate_files(V1, self.SWITCH, None, [v2, v3], HOUR)
        # the inferred one follows the order, so the explicit one conflicts
        v3 = _version(tmp_path, "v3.cadl", "3", None, "{ period: 2 }")
        with pytest.raises(NormsEvalError, match="name version 1 as previous"):
            evaluate_files(V1, self.SWITCH, None, [v2, v3], HOUR)

    def test_one_version_without_previous_is_0334(self, tmp_path):
        # one version without previous: the sequence of 0.3.34, also when
        # its effective time cannot be compared with the others
        v2 = _version(tmp_path, "v2.cadl", "2", "1", '{ at: "2026-01-01" }')
        r = evaluate_files(V1, self.SWITCH, None, [v2])
        assert r["versions"]["sequence"] == ["1", "2"]


# --- A3: the key `version` without versions -----------------------------------------

class TestVersionKeyWithoutVersions:
    FILE = EXAMPLES / "norms_amendment_v1.cadl"

    def test_own_version_accepted(self, tmp_path):
        got = evaluate_files(self.FILE, _trace(tmp_path, [{"t": 0, "version": "1.0.0"},
                                                          {"t": 5, "version": "1.0.0"}]))
        ref = evaluate_files(self.FILE, _trace(tmp_path, [{"t": 0}, {"t": 5}], "r.jsonl"))
        assert _same(got, ref) and "versions" not in got

    @pytest.mark.parametrize("value", ["2", 1, None])
    def test_other_value_refused(self, tmp_path, value):
        from cadl.norms_eval import TraceError
        with pytest.raises(TraceError, match="line 2: 'version' names .*declares no versions "
                                             "to switch to"):
            evaluate_files(self.FILE, _trace(tmp_path, [{"t": 0}, {"t": 1, "version": value}]))

    def test_monitor(self, tmp_path):
        import importlib.util
        import sys
        from cadl.codegen.norms_monitor_gen import generate_norms_monitor_module
        from cadl.parser import parse_file
        text = generate_norms_monitor_module(parse_file(self.FILE))
        file = tmp_path / "mon_a3.py"
        file.write_text(text, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("mon_a3", file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["mon_a3"] = mod
        spec.loader.exec_module(mod)
        m = mod.NormsMonitor()
        m.observe({"t": 0, "version": "1.0.0"})
        with pytest.raises(mod.TraceError, match="declares no versions to switch to"):
            m.observe({"t": 1, "version": "2"})
        m.observe({"t": 2})


# --- A1: versions and regime bindings ---------------------------------------------

REGIMES = ("  transitions:\n    - { from: A, to: B, condition: \"x > 1\" }\n"
           "    - { from: B, to: A, condition: \"x < 1\" }\n"
           "  regimes:\n    B:\n      contracts: { suspended: [DELIVERY_SLA] }\n")


def _with_regimes(tmp: Path, path: Path, name: str, extra: str = REGIMES) -> Path:
    out = tmp / name
    out.write_text(path.read_text(encoding="utf-8").replace("  amendment:\n",
                                                            extra + "  amendment:\n"),
                   encoding="utf-8")
    return out


_RULES = [("keep", None), ("switch", None), ("switch", ["Assigned"]),
          ("switch", ["PickedUp"])]


def _transitional(rule, states) -> str:
    if rule == "keep":
        return "    transitional: { default: keep }\n"
    if states is None:
        return ""
    return ("    transitional:\n      contracts:\n        - { contract: DELIVERY_SLA, "
            f"rule: switch, in_states: [{states[0]}] }}\n")


def _bindings(suspended: str | None) -> str:
    """The regimes A and B; `suspended` names the regime that does not
    have DELIVERY_SLA in force (None: both have it)."""
    text = REGIMES.split("  regimes:\n")[0] + "  regimes:\n"
    if suspended is None:
        return text + "    A: {}\n"
    return text + f"    {suspended}:\n      contracts: {{ suspended: [DELIVERY_SLA] }}\n"


def _random_versions(rng, tmp: Path, vary: bool = False):
    """Versions 1 to 2-4 with random rules and pickup deadlines. With
    `vary`, the versions differ in the regime that suspends the contract.
    Returns (first, later paths, rules, the suspending regime per version)."""
    pick = (lambda: rng.choice(["B", "B", None, "A"])) if vary else (lambda: "B")
    susp = [pick()]
    first = _with_regimes(tmp, V1, "r1.cadl", _bindings(susp[0]))
    paths, rules = [], [None]
    for v in range(2, 2 + rng.randint(1, 3)):
        rule, states = rng.choice(_RULES)
        pickup = rng.choice(["60s", "90s", "120s", "150s"])
        susp.append(pick())
        paths.append(_version(tmp, f"r{v}.cadl", str(v), str(v - 1), f"{{ period: {v} }}",
                              _transitional(rule, states), pickup=pickup,
                              extra=_bindings(susp[-1])))
        rules.append((rule, states))
    return first, paths, rules, susp


def _random_regime_trace(rng, free: bool = False):
    """Observations of one order with regimes A (DELIVERY_SLA in force) and
    B (not in force). The lifecycle state and the values change only at
    observations in A, and the last observation is in A, unless `free`."""
    t = 3400 + rng.choice([0, 100, 150, 190])
    status, state, regime = "Waiting", "Assigned", rng.choice(["A", "A", "B"])
    lines = [{"t": t, "bindings": {"i": 2}}]
    if regime == "B" or rng.random() < 0.3:
        lines[0]["regime"] = regime
    for _ in range(rng.randint(2, 10)):
        t += rng.choice([10, 30, 60, 100, 600, 1800, 3600])
        line = {"t": t}
        if rng.random() < 0.35:
            regime = "B" if regime == "A" else "A"
            line["regime"] = regime
        elif rng.random() < 0.2:
            line["regime"] = regime          # names the current one again
        r = rng.random()
        here = regime == "A" or free
        if here and state == "Assigned" and r < 0.3:
            status, state = "PickedUp", "PickedUp"
            line.update(values={"ROBOT[2].status": status}, state=state)
        elif here and state == "PickedUp" and r < 0.2:
            status, state = "Delivered", "Delivered"
            line.update(values={"ROBOT[2].status": status}, state=state)
        lines.append(line)
        if state == "Delivered":
            break
    if regime == "B" and not free:
        t += rng.choice([10, 60, 600])
        lines.append({"t": t, "regime": "A"})
    return lines


def _regime_reference(times, rules, susp, lines):
    """Independent of norms_eval (K.11, "Versions and regimes"): the
    projection of the trace (the indices of the observations at which the
    instance is judged, with their clock), per index of the projection the
    versions the instance is amended to there, the version it starts
    under, and whether the trace is one that a file without regimes can
    replay (no value or state given while the contract is not in force,
    and the last observation in force). `times[v]` is the effective time
    of version v (None for the first), `susp[v]` the regime of version v
    that does not have the contract in force."""
    def in_force_version(t):
        return max(v for v, tv in enumerate(times) if tv is None or tv <= t)
    regime, offset, out_since = None, 0, None
    kept, amend = [], {}
    started, cur, waiting, seen, deferred = False, None, None, "Assigned", []
    start, plain = None, True
    state = "Assigned"                # the state in the trace
    last = len(lines) - 1
    for i, x in enumerate(lines):
        t = x["t"]
        k = in_force_version(t)
        regime = x.get("regime", regime if regime is not None else "A")
        before = state
        state = x.get("state", state)
        inside = regime != susp[k]
        if inside:
            if out_since is not None:
                offset += t - out_since
                out_since = None
            clock = t - offset
        else:
            if out_since is None:
                out_since = t
            clock = out_since - offset
            if "values" in x or "state" in x or i == last:
                plain = False
        member = inside or i == last
        if not started:
            if member:
                started, cur, start = True, k, k
                kept.append((i, clock))
                seen = state
            continue
        look = before if inside else seen
        if waiting and look in waiting[1]:
            deferred.append(waiting[0])
            waiting = None
        for v in range(cur + 1, k + 1):
            waiting = None
            rule, states = rules[v]
            if rule == "switch":
                if states is None or look in states:
                    deferred.append(v)
                else:
                    waiting = (v, states)
        cur = k
        if member:
            kept.append((i, clock))
            if deferred:
                amend[len(kept) - 1] = deferred
            deferred = []
            seen = state
        if state == "Delivered" and inside:
            break
    return kept, amend, start, plain


def _plain_projection(lines, kept, amend, paths):
    """The projection as a trace of a file without regimes: the clock as
    the time stamp, the values of the left-out observations moved to the
    next one, and `amend` at the reference's switches."""
    out = []
    values: dict = {}
    pos = 0
    for i, x in enumerate(lines):
        values.update(x.get("values", {}))
        if pos < len(kept) and kept[pos][0] == i:
            line = {"t": kept[pos][1]}
            if not out:
                line["bindings"] = lines[0]["bindings"]
            if values:
                line["values"] = dict(values)
            if "state" in x:
                line["state"] = x["state"]
            if pos in amend:
                line["amend"] = paths[amend[pos][0] - 1].name
            out.append(line)
            values = {}
            pos += 1
    return out


@pytest.mark.parametrize("seed", range(300))
def test_versions_with_regimes_equal_the_reference(tmp_path, seed):
    import random
    from cadl.norms_eval import evaluate, load_version, parse_trace
    rng = random.Random(7000 + seed)
    first, paths, rules, susp = _random_versions(rng, tmp_path, vary=seed % 2 == 1)
    lines = _random_regime_trace(rng, free=seed % 3 == 2)
    times = [None] + [3600 * (v - 1) for v in range(2, len(paths) + 2)]
    kept, amend, start, plain = _regime_reference(times, rules, susp, lines)
    got = evaluate_files(first, _trace(tmp_path, lines), None, paths, HOUR)
    # the amendments at the observations of the projection the reference gives
    want = [(lines[kept[p][0]]["t"], f"version {v + 1}") for p in sorted(amend)
            for v in amend[p]]
    assert [(a["t"], a["file"]) for a in got["amendments"]] == want
    assert got["versions"]["start"] == str(start + 1)
    if not plain or any(len(v) > 1 for v in amend.values()):
        return                      # one `amend` per observation in a plain trace
    # the versions without regimes:, for the plain trace
    bare = []
    for path in [first] + paths:
        text = path.read_text(encoding="utf-8")
        cut = text[text.index("\n  transitions:\n") + 1:text.index("\n  amendment:\n") + 1]
        out = tmp_path / f"bare_{path.name}"
        out.write_text(text.replace(cut, ""), encoding="utf-8")
        bare.append(out)
    plain_lines = _plain_projection(lines, kept, amend, bare[1:])
    ref = evaluate(load_version(bare[start]),
                   parse_trace([json.dumps(x) for x in plain_lines],
                               path=str(tmp_path / "p.jsonl")))
    assert _norms(got) == _norms(ref)
    assert ([(v["norm"], v["verdict"]) for v in got["violations"]]
            == [(v["norm"], v["verdict"]) for v in ref["violations"]])
    assert got["ok"] == ref["ok"]


def _module(tmp: Path, path: Path, name: str):
    import importlib.util
    import sys
    from cadl.codegen.norms_monitor_gen import generate_norms_monitor_module
    from cadl.parser import parse_file
    text = generate_norms_monitor_module(parse_file(path))
    file = tmp / f"{name}.py"
    file.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _model(path: Path) -> dict:
    from cadl.codegen.norms_monitor_gen import build_model
    from cadl.parser import parse_file
    return json.loads(json.dumps(build_model(parse_file(path))))


@pytest.mark.parametrize("seed", range(300))
def test_monitor_with_regimes_equals_norms_eval(tmp_path, seed):
    """The generated monitor (Python target) against `cadl norms-eval` on
    runs with versions and regime bindings: random versions and rules,
    random regimes, with the time base or with the versions named."""
    import random
    rng = random.Random(9000 + seed)
    first, paths, _, _ = _random_versions(rng, tmp_path, vary=rng.random() < 0.5)
    lines = _random_regime_trace(rng, free=rng.random() < 0.5)
    named = rng.random() < 0.3
    if named:
        for x in lines:
            x["version"] = str(1 + max(0, min(len(paths), int(x["t"] // 3600))))
    if rng.random() < 0.2:
        lines[-1]["end"] = False
    trace = _trace(tmp_path, lines)
    mod = _module(tmp_path, first, f"mon_r_{seed}")
    models = [_model(p) for p in paths]
    try:
        ref = evaluate_files(first, trace, None, paths, None if named else HOUR)
    except NormsEvalError as e:
        with pytest.raises(Exception, match=str(e)[:40].replace("(", r"\(")):
            mod.replay(trace.read_text().splitlines(keepends=True), run_versions=models,
                       path=str(trace), period=None if named else 3600)
        return
    got = mod.replay(trace.read_text().splitlines(keepends=True), run_versions=models,
                     path=str(trace), period=None if named else 3600)
    ref.pop("file")
    assert got == json.loads(json.dumps(ref))


# --- B7: metrics.py places entries from another version ----------------------------

def _metrics_block(rng, version: int, versions: int) -> str:
    """Two metrics with numeric or duration targets, random windows and
    schedules by period or by version (in the order of the sequence)."""
    out = "  metrics:\n"
    for mid in ("blocked_time", "wait_time"):
        if rng.random() < 0.15 and version > 1:
            continue                        # this version does not declare it
        target = rng.choice(['"<= 600"', '"<= 500"', '"<= 10min"', '"< 700"'])
        out += f"    - id: {mid}\n      formula: \"1\"\n      target: {target}\n"
        if rng.random() < 0.7:
            out += f"      measure: {{ window: {rng.choice([600, 1800, 3600, 7200])}s }}\n"
        r = rng.random()
        entries = []
        if r < 0.4:
            for k in sorted(rng.sample(range(1, 7), rng.randint(1, 2))):
                entries.append(f"{{ period: {k} }}")
        elif r < 0.8:
            names = [str(v) for v in range(1, versions + 1)] + ["9"]
            pick = sorted(rng.sample(range(len(names)), rng.randint(1, 2)))
            entries = [f'{{ version: "{names[i]}" }}' for i in pick]
        if entries:
            out += "      schedule:\n"
            for e in entries:
                tgt = rng.choice(['"<= 400"', '"<= 600"', '"<= 8min"', '"< 700"'])
                out += f"        - {{ from: {e}, target: {tgt} }}\n"
    return out


def _load_module(name: str, text: str):
    import importlib.util
    import sys
    import types
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    exec(compile(text, name, "exec"), mod.__dict__)
    return mod


@pytest.mark.parametrize("seed", range(150))
def test_metrics_py_equals_metrics_eval(tmp_path, seed):
    import random
    from cadl.codegen import _metric_run, _metric_schedules
    from cadl.codegen.metric_gen import generate_metrics_module
    from cadl.codegen.version_gen import generate_version_module
    from cadl.metrics_eval import evaluate_files as metrics_files
    from cadl.parser import parse_file
    rng = random.Random(500 + seed)
    count = rng.randint(2, 4)
    paths = []
    for v in range(1, count + 1):
        block = _metrics_block(rng, v, count)
        if v == 1:
            text = V1.read_text(encoding="utf-8").replace("  amendment:\n",
                                                          block + "  amendment:\n")
            path = tmp_path / "m1.cadl"
            path.write_text(text, encoding="utf-8")
        else:
            path = _version(tmp_path, f"m{v}.cadl", str(v), str(v - 1), f"{{ period: {v} }}",
                            extra=block)
        paths.append(path)
    records = []
    for _ in range(12):
        rec = {"metric": rng.choice(["blocked_time", "wait_time"]),
               "value": rng.choice([300, 450, 500, 600, 650, 699, 700])}
        if rng.random() < 0.5:
            rec["t"] = rng.choice([0, 1000, 3000, 3600, 5000, 7000, 7200, 9000, 12000, 20000])
        else:
            rec["period"] = rng.randint(1, 7)
        records.append(rec)
    meas = _trace(tmp_path, records, "m.jsonl")
    base = TimeBase(3600)
    try:
        ref = metrics_files(paths[0], meas, paths[1:], base)
    except NormsEvalError as e:
        pytest.skip(f"not a valid run: {e}")
    modules = []
    for v, path in enumerate(paths, start=1):
        sos = parse_file(path)
        version = _load_module(f"ver_{seed}_{v}", generate_version_module(sos))
        metrics = _load_module(f"met_{seed}_{v}", generate_metrics_module(
            sos.metrics, _metric_schedules(sos), _metric_run(sos)))
        modules.append((version, metrics))
    from fractions import Fraction
    versions = [m[0] for m in modules]
    for r in ref["measurements"]:
        k = ref["versions"].index(r["version"])
        cls = getattr(modules[k][1], "MetricsCollector", None)    # no metric at all
        check = None if cls is None else getattr(cls(), f"check_{r['metric']}_target", None)
        if check is None:
            assert r["verdict"] == "not judged"
            continue
        got = check(float(r["value"]), t=Fraction(r["start"]), period=3600, versions=versions)
        want = {"met": True, "not met": False, "not judged": None}[r["verdict"]]
        assert got == want, (r, got)


# --- B6: effective times in agents-run and the gateways ----------------------------

AV1 = ROOT / "tests" / "data" / "agents_versions_v1.cadl"
AV2 = ROOT / "tests" / "data" / "agents_versions_v2.cadl"
EPOCH = "2026-11-10T09:00:00Z"


def _agents_v3(tmp: Path, rule: str = "", effective: str = "{ period: 4 }") -> Path:
    """Version 3: PLANNER may no longer read (as version 1)."""
    text = AV1.read_text(encoding="utf-8").replace('version: "1"', 'version: "3"')
    text = text.replace("  amendment:\n    transitional: { default: switch }\n",
                        f'  amendment:\n    previous: "2"\n    effective: {effective}\n' + rule)
    path = tmp / "av3.cadl"
    path.write_text(text, encoding="utf-8")
    return path


def _read(t):
    return {"t": t, "messages": [{"from": "PLANNER", "to": "FILES", "name": "read_file"}]}


def _decisions(result: dict) -> list:
    return [(s["time"], s["message"]["name"], s["result"]["decision"], s["result"]["reason"])
            for s in result["steps"]]


def _agents(tmp: Path, lines: list, versions=(), base=None, name="a.jsonl") -> dict:
    from cadl.agents_runtime import load_run_trace, run
    from cadl.norms_eval import load_version
    path = _trace(tmp, lines, name)
    return run(load_version(AV1), load_run_trace(path),
               run_versions=[load_version(p) for p in versions], base=base)


class TestAgentsRun:
    BASE = TimeBase(100)

    def test_switch_at_the_effective_time(self, tmp_path):
        lines = [dict(_read(0), quantities=[], epoch=EPOCH), _read(50), _read(100), {"t": 150}]
        r = _agents(tmp_path, lines, [AV2], self.BASE)
        assert [d[2:] for d in _decisions(r)] == [("deny", "default"), ("deny", "default"),
                                                  ("allow", "permission")]
        assert [(a["file"], a["contracts"]) for a in r["amendments"]] == [
            ("version 2", ["ASSISTED_REPORTING"])]
        assert any(a.get("event") == "amendment" and a["reason"] == "version 2"
                   for a in r["audit"])
        # without the versions: as before
        r = _agents(tmp_path, lines, (), None, "b.jsonl")
        assert [d[2] for d in _decisions(r)] == ["deny"] * 3 and r["amendments"] == []

    @pytest.mark.parametrize("seed", range(100))
    def test_equals_amend_at_the_effective_time(self, tmp_path, seed):
        """The scheduled switch gives the decisions and the norms that an
        `amend` at the first line at or after the effective time gives."""
        import random
        import shutil
        rng = random.Random(300 + seed)
        p3 = rng.randint(3, 6)
        v3 = _agents_v3(tmp_path, effective=f"{{ period: {p3} }}")
        shutil.copy(AV2, tmp_path / "av2.cadl")
        t, lines = 0, []
        for k in range(rng.randint(2, 10)):
            line = _read(t) if rng.random() < 0.8 else {"t": t}
            if k == 0:
                line.update(quantities=[], epoch=EPOCH)
            lines.append(line)
            t += rng.choice([10, 40, 60, 100, 150])
        got = _agents(tmp_path, lines, [AV2, v3], self.BASE)
        eff = [100, (p3 - 1) * 100]
        plain = [dict(x) for x in lines]
        for e, name in zip(eff, ("av2.cadl", "av3.cadl")):
            at = next((i for i, x in enumerate(plain) if x["t"] >= e and i > 0), None)
            if at is None:
                break                # this version does not take effect in the trace
            if "amend" in plain[at]:
                return               # one amend per line
            plain[at]["amend"] = name
        ref = _agents(tmp_path, plain, (), None, "p.jsonl")
        assert _decisions(got) == _decisions(ref)
        assert [i["norms"] for i in got["instances"]] == [i["norms"] for i in ref["instances"]]

    def test_first_line_under_a_later_version(self, tmp_path):
        r = _agents(tmp_path, [dict(_read(250), quantities=[], epoch=EPOCH)], [AV2], self.BASE)
        assert _decisions(r)[0][2:] == ("allow", "permission") and r["amendments"] == []

    def test_scheduled_switch_before_amend(self, tmp_path):
        v3 = _agents_v3(tmp_path, effective="{ period: 9 }")
        lines = [dict(_read(0), quantities=[], epoch=EPOCH), dict(_read(100), amend=v3.name)]
        r = _agents(tmp_path, lines, [AV2], self.BASE)
        assert [a["file"] for a in r["amendments"]] == ["version 2", "av3.cadl"]
        assert [d[2] for d in _decisions(r)] == ["deny", "deny"]

    def test_keep_with_a_running_instance_is_refused(self, tmp_path):
        v3 = _agents_v3(tmp_path, "    transitional: { default: keep }\n")
        lines = [dict(_read(0), quantities=[], epoch=EPOCH), _read(150), _read(320)]
        with pytest.raises(NormsEvalError, match="version 3 takes effect and its transitional "
                                                 "rule for ASSISTED_REPORTING is keep"):
            _agents(tmp_path, lines, [AV2, v3], self.BASE)

    def test_epoch(self, tmp_path):
        v2 = tmp_path / "av2e.cadl"
        v2.write_text(AV2.read_text().replace("effective: { period: 2 }",
                                              'effective: { at: "2026-11-10T09:01:00Z" }'))
        lines = [dict(_read(0), quantities=[], epoch=EPOCH), _read(59), _read(60)]
        r = _agents(tmp_path, lines, [v2], TimeBase(epoch=True))
        assert [d[2] for d in _decisions(r)] == ["deny", "deny", "allow"]

    def test_cli(self, tmp_path):
        from cadl.cli import main
        trace = _trace(tmp_path, [dict(_read(0), quantities=[], epoch=EPOCH), _read(100)])
        assert main(["agents-run", str(AV1), "--trace", str(trace), "--version", str(AV2),
                     "--period", "100s", "--format", "json"]) == 0
        assert main(["agents-run", str(AV1), "--trace", str(trace), "--origin", "5"]) == 2


class TestGatewayVersions:
    def test_mcp_gateway_switches_on_its_clock(self, tmp_path):
        from cadl.agents_runtime import load_run_trace, run
        from cadl.audit_log import read_records
        from cadl.cli import main
        from cadl.mcp_gateway import Gateway
        from cadl.norms_eval import load_version

        class Clock:
            t = 1000.0

            def __call__(self):
                return self.t
        clock, client, server = Clock(), [], []
        gw = Gateway(AV1, "PLANNER", "FILES", tmp_path / "a.jsonl", tmp_path / "a.trace.jsonl",
                     client.append, server.append, clock=clock,
                     run_versions=[load_version(AV2)], base=TimeBase(100))

        def call(i):
            gw.client_line(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                       "params": {"name": "read_file"}}))
        call(1)
        clock.t += 120
        call(2)
        gw.stop()
        assert len(server) == 1 and len(client) == 1           # denied, then forwarded
        decisions = [(r["message"]["name"], r["decision"], r["reason"])
                     for r in read_records(tmp_path / "a.jsonl") if "decision" in r]
        assert decisions == [("read_file", "deny", "default"),
                             ("read_file", "allow", "permission")]
        out = tmp_path / "replayed.jsonl"
        assert main(["agents-run", str(AV1), "--trace", str(tmp_path / "a.trace.jsonl"),
                     "--audit", str(out), "--version", str(AV2), "--period", "100s"]) == 0
        assert out.read_bytes() == (tmp_path / "a.jsonl").read_bytes()

    def test_versions_that_change_the_actors_are_refused(self, tmp_path):
        from cadl.mcp_gateway import Gateway, GatewayError
        from cadl.norms_eval import load_version
        v2 = tmp_path / "x2.cadl"
        v2.write_text(AV2.read_text().replace('"File store"', '"Files"'))
        with pytest.raises(GatewayError, match="version 2 of the run changes the actors"):
            Gateway(AV1, "PLANNER", "FILES", tmp_path / "a.jsonl", tmp_path / "a.t",
                    list().append, list().append, run_versions=[load_version(v2)],
                    base=TimeBase(100))
        assert not (tmp_path / "a.jsonl").exists()


# --- `amend` together with versions: one order for every processor ----------------

_OBLIGATION = '''        - id: file_report
          kind: obligation
          bearer: PLANNER
          content: "PLANNER -> TICKETS : create_ticket"
          deadline: {deadline}
'''


def _obligation_versions(tmp: Path):
    """Versions 1 and 2 of the agents example with an obligation whose
    deadline changes (300 s, then 120 s from period 2), and a file
    `amend.cadl` (60 s) that an `amend` line names."""
    def make(src: Path, name: str, deadline: str, header=None) -> Path:
        text = src.read_text(encoding="utf-8").replace(
            "      delegations:\n", _OBLIGATION.format(deadline=deadline) + "      delegations:\n")
        if header is not None:
            text = text.replace(*header)
        out = tmp / name
        out.write_text(text, encoding="utf-8")
        return out
    v1 = make(AV1, "o1.cadl", "300s")
    v2 = make(AV2, "o2.cadl", "120s")
    amend = make(AV1, "amend.cadl", "60s", ('version: "1"', 'version: "9"'))
    return v1, v2, amend


@pytest.mark.parametrize("seed", range(120))
def test_amend_with_versions_agents_run_equals_norms_eval(tmp_path, seed):
    """`cadl agents-run` and `cadl norms-eval` on the same file and trace,
    with versions and an `amend` line (also at the line at which a version
    takes effect): the same amendments, in the same order, and the same
    norm states and verdicts."""
    import random
    from cadl.agents_runtime import load_run_trace, run
    from cadl.norms_eval import load_version
    rng = random.Random(4400 + seed)
    v1, v2, amend = _obligation_versions(tmp_path)
    t, lines = 0, []
    amended = False
    for k in range(rng.randint(2, 9)):
        line = {"t": t}
        if rng.random() < 0.3:
            line["messages"] = [{"from": "PLANNER", "to": "TICKETS", "name": "create_ticket"}]
        if k > 0 and not amended and rng.random() < 0.3:
            line["amend"] = amend.name
            amended = True
        lines.append(line)
        t += rng.choice([10, 30, 60, 100, 150])
    if rng.random() < 0.4:       # the amend at the line at which version 2 takes effect
        at = next((i for i, x in enumerate(lines) if x["t"] >= 100 and i > 0), None)
        if at is not None:
            for x in lines:
                x.pop("amend", None)
            lines[at]["amend"] = amend.name
    agents_lines = [dict(lines[0], quantities=[], epoch=EPOCH)] + lines[1:]
    a_trace = _trace(tmp_path, agents_lines, "a.jsonl")
    n_trace = _trace(tmp_path, lines, "n.jsonl")
    got = run(load_version(v1), load_run_trace(a_trace), run_versions=[load_version(v2)],
              base=TimeBase(100))
    ref = evaluate_files(v1, n_trace, None, [v2], TimeBase(100))
    assert ([(a["file"]) for a in got["amendments"]]
            == [a["file"] for a in ref["amendments"]])
    inst = got["instances"][0]
    assert ({(x["id"], x["state"], x["verdict"]) for x in inst["norms"]}
            == {(x["id"], x["state"], x["verdict"]) for x in ref["norms"]})


@pytest.mark.parametrize("seed", range(80))
def test_amend_with_versions_monitor_equals_norms_eval(tmp_path, seed):
    """The norms monitor against `cadl norms-eval` with versions and an
    `amend` line (the versions first at one observation)."""
    import random
    rng = random.Random(5500 + seed)
    rule, states = rng.choice(_RULES)
    v2 = _version(tmp_path, "w2.cadl", "2", "1", "{ period: 2 }", _transitional(rule, states),
                  pickup=rng.choice(["60s", "90s", "150s"]))
    other = _version(tmp_path, "other.cadl", "7", "1", "{ period: 9 }",
                     pickup=rng.choice(["60s", "180s"]))
    t, state = 3400 + rng.choice([0, 100, 190]), "Assigned"
    lines = [{"t": t, "bindings": {"i": 2}}]
    for _ in range(rng.randint(2, 8)):
        t += rng.choice([10, 30, 60, 100, 600])
        line = {"t": t}
        if state == "Assigned" and rng.random() < 0.3:
            state = "PickedUp"
            line.update(values={"ROBOT[2].status": "PickedUp"}, state=state)
        lines.append(line)
    pick = [i for i, x in enumerate(lines) if i > 0]
    lines[rng.choice(pick)]["amend"] = other.name
    if rng.random() < 0.4:
        at = next((i for i, x in enumerate(lines) if x["t"] >= 3600 and i > 0), None)
        if at is not None:
            for x in lines:
                x.pop("amend", None)
            lines[at]["amend"] = other.name
    trace = _trace(tmp_path, lines)
    ref = evaluate_files(V1, trace, None, [v2], HOUR)
    mod = _module(tmp_path, V1, f"mon_am_{seed}")
    got = mod.replay(trace.read_text().splitlines(keepends=True),
                     versions={other.name: _model(other)}, run_versions=[_model(v2)],
                     path=str(trace), period=3600)
    ref.pop("file")
    assert got == json.loads(json.dumps(ref))


class TestAgentsVersionKey:
    def test_without_versions(self, tmp_path):
        ok = [dict(_read(0), quantities=[], epoch=EPOCH, version="1"), _read(10)]
        r = _agents(tmp_path, ok)
        assert [d[2] for d in _decisions(r)] == ["deny", "deny"]
        from cadl.norms_eval import TraceError
        bad = [dict(_read(0), quantities=[], epoch=EPOCH), dict(_read(10), version="2")]
        with pytest.raises(TraceError, match="line 2: 'version' names '2', but no versions of "
                                             "the run are given"):
            _agents(tmp_path, bad, name="b.jsonl")

    def test_named_versions_as_in_norms_eval(self, tmp_path):
        lines = [dict(_read(0), quantities=[], epoch=EPOCH), dict(_read(10), version="2"),
                 _read(20)]
        r = _agents(tmp_path, lines, [AV2])            # no time base: named only
        assert [d[2] for d in _decisions(r)] == ["deny", "allow", "allow"]
        assert [a["file"] for a in r["amendments"]] == ["version 2"]
        from cadl.norms_eval import TraceError
        with pytest.raises(TraceError, match="by the effective times version 1"):
            _agents(tmp_path, lines, [AV2], TimeBase(100), name="c.jsonl")
        back = [dict(_read(0), quantities=[], epoch=EPOCH, version="2"),
                dict(_read(10), version="1")]
        with pytest.raises(TraceError, match="comes before version 2"):
            _agents(tmp_path, back, [AV2], name="d.jsonl")


class TestPossibleLaterVersion:
    """A version without previous: whose effective time is not period 1 may
    replace another (A2), so the D.5 monitors and Unity C# treat it so."""

    def test_d5_and_unity_refuse(self, tmp_path):
        from cadl.codegen import generate, norms_monitors
        from cadl.parser import parse_file
        later = _version(tmp_path, "l3.cadl", "3", None, "{ period: 3 }")
        sos = parse_file(later)
        for target in ("opa", "solidity"):
            entry = next(m for m in norms_monitors(sos, target) if m["contract"] == "DELIVERY_SLA")
            assert entry["text"] is None
            assert "gives no previous: and takes effect at period 3" in entry["reasons"][0]
        with pytest.raises(ValueError, match="an effective time other than period 1"):
            generate(sos, tmp_path / "u", "unity-csharp")
        kept = _version(tmp_path, "k3.cadl", "3", None, "{ period: 3 }",
                        "    transitional: { default: keep }\n")
        entry = next(m for m in norms_monitors(parse_file(kept), "opa")
                     if m["contract"] == "DELIVERY_SLA")
        assert entry["text"] is not None

    def test_period_one_is_a_first_version(self, tmp_path):
        from cadl.codegen import norms_monitors
        from cadl.parser import parse_file
        for path in (V1, EXAMPLES / "amendment_robot_town_v1.cadl"):
            sos = parse_file(path)
            assert all(m["text"] is not None or "previous" not in " ".join(m["reasons"])
                       for m in norms_monitors(sos, "opa"))
        # and the inference never places it after another version
        first = _version(tmp_path, "f1.cadl", "1", None, None)
        one = _version(tmp_path, "p1.cadl", "2", None, "{ period: 1 }")
        with pytest.raises(NormsEvalError, match="one sequence"):
            evaluate_files(first, TestPreviousInferred.SWITCH, None, [one], HOUR)
