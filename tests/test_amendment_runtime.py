"""Versions while a system runs (Appendix K, K.11): `cadl norms-eval` and
the norms monitor of the Python target with versions, effective times and
transitional rules; `cadl metrics-eval`; the refusals of `norms-check`,
the D.5 monitors and the Unity C# target; `version.py` of the Python
target.

The differential tests compare, on random traces with version switches,
(1) `cadl norms-eval` with versions against `cadl norms-eval` on the same
trace with ``amend`` keys at the observations where a reference written
here says that the instance switches (F.6 is applied exactly there), and
(2) the generated monitor against `cadl norms-eval`."""

from __future__ import annotations

import importlib.util
import json
import random
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from cadl.codegen import generate, norms_monitors
from cadl.codegen.norms_monitor_gen import generate_norms_monitor_module
from cadl.metrics_eval import evaluate_files as metrics_files
from cadl.norms_check import NormsCheckError, build_model
from cadl.norms_eval import (NormsEvalError, TimeBase, TraceError, evaluate, evaluate_files,
                             load_version, parse_trace, version_chain)
from cadl.parser import parse_file

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
TRACES = EXAMPLES / "traces"
V1 = EXAMPLES / "amendment_norms_v1.cadl"
V2 = EXAMPLES / "amendment_norms_v2.cadl"
TOWN1 = EXAMPLES / "amendment_robot_town_v1.cadl"
TOWN2 = EXAMPLES / "amendment_robot_town_v2.cadl"
HOUR = TimeBase(3600)


def _cli(*argv):
    out = subprocess.run([sys.executable, "-c",
                          "import sys;from cadl.cli import main;sys.argv=['cadl',*sys.argv[1:]];"
                          "sys.exit(main())", *map(str, argv)],
                         capture_output=True, text=True, cwd=ROOT)
    return out.returncode, out.stdout, out.stderr


def _variant(tmp: Path, name: str, version: str, previous: str, period: int,
             transitional: str, pickup: str = "90s", extra: str = "") -> Path:
    """A version of amendment_norms with the given header and rules."""
    text = V2.read_text(encoding="utf-8")
    text = text.replace('version: "2"', f'version: "{version}"')
    head = text.index("  amendment:\n")
    tail = text.index("    layers:\n")
    block = (f'  amendment:\n    previous: "{previous}"\n    effective: {{ period: {period} }}\n'
             + transitional)
    text = text[:head] + block + text[tail:]
    text = text.replace("deadline: 90s", f"deadline: {pickup}")
    text = text.replace("        value: 90s", f"        value: {pickup}")
    text += extra
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


def _trace(tmp: Path, lines: list, name: str = "t.jsonl") -> Path:
    path = tmp / name
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def _norms(result: dict) -> dict:
    return {n["id"]: (n["state"], n["verdict"]) for n in result["norms"]}


# --- norms-eval -------------------------------------------------------------------

class TestNormsEval:
    def test_switch_in_state(self):
        r = evaluate_files(V1, TRACES / "amendment_norms_v1_a_switch.jsonl", None, [V2], HOUR)
        assert _norms(r)["pick_up"] == ("violated", "violated")
        assert r["versions"]["start"] == "1" and r["versions"]["follows"] == "2"
        assert [e["event"] for e in r["versions"]["events"]] == ["switch"]
        assert [a["t"] for a in r["amendments"]] == [3600]
        assert "deadline of 90s passed at t=3640" in dict(
            (n["id"], n["reason"]) for n in r["norms"])["pick_up"]

    def test_without_versions_the_file_alone(self):
        r = evaluate_files(V1, TRACES / "amendment_norms_v1_a_switch.jsonl")
        assert _norms(r)["pick_up"] == ("fulfilled", "fulfilled")
        assert "versions" not in r and r["amendments"] == []

    def test_keep_when_not_in_state(self):
        r = evaluate_files(V1, TRACES / "amendment_norms_v1_b_keep.jsonl", None, [V2], HOUR)
        assert r["ok"] and r["versions"]["follows"] == "1"
        assert [e["event"] for e in r["versions"]["events"]] == ["wait"]
        assert r["amendments"] == []

    def test_named_version_without_time_base(self, tmp_path):
        lines = [json.loads(x) for x in
                 (TRACES / "amendment_norms_v1_a_switch.jsonl").read_text().splitlines()]
        lines[1]["version"] = "2"
        named = evaluate_files(V1, _trace(tmp_path, lines), None, [V2])
        timed = evaluate_files(V1, TRACES / "amendment_norms_v1_a_switch.jsonl", None, [V2],
                               HOUR)
        for r in (named, timed):
            r.pop("trace"), r.pop("file")
        assert named == timed
        # both, and they agree
        both = evaluate_files(V1, _trace(tmp_path, lines), None, [V2], HOUR)
        both.pop("trace"), both.pop("file")
        assert both == timed

    def test_named_version_must_agree_and_not_go_back(self, tmp_path):
        lines = [{"t": 3550, "bindings": {"i": 2}}, {"t": 3590, "version": "2"}]
        with pytest.raises(TraceError, match="by the effective times version 1"):
            evaluate_files(V1, _trace(tmp_path, lines), None, [V2], HOUR)
        lines = [{"t": 3550, "bindings": {"i": 2}, "version": "2"}, {"t": 3590, "version": "1"}]
        with pytest.raises(TraceError, match="comes before version 2"):
            evaluate_files(V1, _trace(tmp_path, lines), None, [V2])
        lines = [{"t": 1, "version": "7"}]
        with pytest.raises(TraceError, match="must name a version of the run"):
            evaluate_files(V1, _trace(tmp_path, lines), None, [V2])

    def test_starts_under_the_version_in_force(self, tmp_path):
        lines = [{"t": 3600, "bindings": {"i": 2}}, {"t": 3695}]
        r = evaluate_files(V1, _trace(tmp_path, lines), None, [V2], HOUR)
        assert r["versions"]["start"] == "2" and r["versions"]["events"] == []
        assert _norms(r)["pick_up"] == ("violated", "violated")      # 90 s

    def test_before_the_first_version(self, tmp_path):
        with pytest.raises(TraceError, match="no version is in force"):
            evaluate_files(V2, _trace(tmp_path, [{"t": 10}]), None, [], HOUR)

    def test_amend_key_is_refused(self, tmp_path):
        lines = [{"t": 0, "bindings": {"i": 2}}, {"t": 1, "amend": "x.cadl"}]
        with pytest.raises(NormsEvalError, match="cannot also amend"):
            evaluate_files(V1, _trace(tmp_path, lines), None, [V2], HOUR)

    def test_version_key_without_the_extension(self, tmp_path):
        # from cadl 0.3.35 the key is read also for a file without the
        # extension: its own sos.version is accepted, another value refused
        f = EXAMPLES / "norms_amendment_v1.cadl"
        with pytest.raises(TraceError, match="declares no versions to switch to .* "
                                             "sos.version '1.0.0'"):
            evaluate_files(f, _trace(tmp_path, [{"t": 0, "version": "1"}]))
        same = evaluate_files(f, _trace(tmp_path, [{"t": 0, "version": "1.0.0"}], "s.jsonl"))
        plain = evaluate_files(f, _trace(tmp_path, [{"t": 0}], "p.jsonl"))
        same.pop("trace"), plain.pop("trace")
        assert same == plain
        with pytest.raises(NormsEvalError, match="does not declare 'amendment: 0.1'"):
            evaluate_files(f, _trace(tmp_path, [{"t": 0}]), None, [V2])

    def test_chain_errors(self, tmp_path):
        no_prev = _variant(tmp_path, "np.cadl", "3", "9", 3, "")
        with pytest.raises(NormsEvalError, match="one sequence by previous"):
            evaluate_files(V1, TRACES / "amendment_norms_v1_a_switch.jsonl", None,
                           [V2, no_prev], HOUR)
        same = _variant(tmp_path, "same.cadl", "2", "1", 3, "")
        with pytest.raises(NormsEvalError, match="distinct sos.version"):
            evaluate_files(V1, TRACES / "amendment_norms_v1_a_switch.jsonl", None,
                           [V2, same], HOUR)
        early = _variant(tmp_path, "early.cadl", "3", "2", 2, "")
        with pytest.raises(NormsEvalError, match="not after version 2"):
            evaluate_files(V1, TRACES / "amendment_norms_v1_a_switch.jsonl", None,
                           [V2, early], HOUR)
        with pytest.raises(NormsEvalError, match="no length of a period"):
            evaluate_files(V1, TRACES / "amendment_norms_v1_a_switch.jsonl", None, [V2],
                           TimeBase(epoch=True))

    def test_keep_then_switch_amends_from_the_kept_version(self, tmp_path):
        v2 = _variant(tmp_path, "k2.cadl", "2", "1", 2, "    transitional: { default: keep }\n")
        v3 = _variant(tmp_path, "s3.cadl", "3", "2", 3, "", pickup="60s")
        lines = [{"t": 3550, "bindings": {"i": 2}}, {"t": 3600}, {"t": 7200}, {"t": 7300}]
        r = evaluate_files(V1, _trace(tmp_path, lines), None, [v2, v3], HOUR)
        ev = [(e["event"], e["version"]) for e in r["versions"]["events"]]
        assert ev == [("keep", "2"), ("switch", "3")]
        assert [a["file"] for a in r["amendments"]] == ["version 3"]
        effect = r["amendments"][0]["effects"][0]
        # the deadline of version 1 (120 s, at 3670) passed, and the
        # amendment at 7200 comes first (F.6): the elapsed time exceeds 60 s
        assert effect["to_state"] == "violated" and "new deadline of 60s" in effect["reason"]

    def test_waiting_switch_is_dropped(self, tmp_path):
        v3 = _variant(tmp_path, "k3.cadl", "3", "2", 3, "    transitional: { default: keep }\n")
        lines = [{"t": 3500, "bindings": {"i": 2}, "values": {"ROBOT[2].status": "PickedUp"},
                  "state": "PickedUp"}, {"t": 3600}, {"t": 7200}, {"t": 7300}]
        r = evaluate_files(V1, _trace(tmp_path, lines), None, [V2, v3], HOUR)
        ev = [(e["event"], e["version"]) for e in r["versions"]["events"]]
        assert ev == [("wait", "2"), ("dropped", "2"), ("keep", "3")]
        assert r["versions"]["follows"] == "1"

    def test_waiting_switch_is_made_later(self, tmp_path):
        v2 = _variant(tmp_path, "w2.cadl", "2", "1", 2,
                      "    transitional:\n      contracts:\n        - { contract: DELIVERY_SLA,"
                      " rule: switch, in_states: [PickedUp] }\n")
        lines = [{"t": 3550, "bindings": {"i": 2}}, {"t": 3600},
                 {"t": 3610, "values": {"ROBOT[2].status": "PickedUp"}, "state": "PickedUp"},
                 {"t": 3620}]
        r = evaluate_files(V1, _trace(tmp_path, lines), None, [v2], HOUR)
        ev = [(e["t"], e["event"]) for e in r["versions"]["events"]]
        assert ev == [(3600, "wait"), (3620, "switch")]
        assert [a["t"] for a in r["amendments"]] == [3620]

    def test_waiting_switch_is_made_before_a_new_version(self, tmp_path):
        # K.11: a waiting switch whose state holds before the observation is
        # made first, then the version that takes effect there applies
        v2 = _variant(tmp_path, "w2.cadl", "2", "1", 2,
                      "    transitional:\n      contracts:\n        - { contract: DELIVERY_SLA,"
                      " rule: switch, in_states: [PickedUp] }\n")
        v3 = _variant(tmp_path, "k3.cadl", "3", "2", 3, "    transitional: { default: keep }\n")
        lines = [{"t": 3550, "bindings": {"i": 2}}, {"t": 3600},
                 {"t": 7000, "values": {"ROBOT[2].status": "PickedUp"}, "state": "PickedUp"},
                 {"t": 7200}, {"t": 7210}]
        r = evaluate_files(V1, _trace(tmp_path, lines), None, [v2, v3], HOUR)
        ev = [(e["t"], e["event"], e["version"]) for e in r["versions"]["events"]]
        assert ev == [(3600, "wait", "2"), (7200, "switch", "2"), (7200, "keep", "3")]
        assert r["versions"]["follows"] == "2"

    def test_two_versions_at_one_observation(self, tmp_path):
        v2 = _variant(tmp_path, "a2.cadl", "2", "1", 2, "")
        v3 = _variant(tmp_path, "a3.cadl", "3", "2", 3, "", pickup="60s")
        lines = [{"t": 3550, "bindings": {"i": 2}}, {"t": 7300}]
        r = evaluate_files(V1, _trace(tmp_path, lines), None, [v2, v3], HOUR)
        assert [a["file"] for a in r["amendments"]] == ["version 2", "version 3"]
        assert all(a["t"] == 7300 for a in r["amendments"])

    def test_regimes(self, tmp_path):
        # refused up to cadl 0.3.34; from 0.3.35 versions and regime bindings
        # combine (K.11, "Versions and regimes"; tests/test_amendment_runtime_0335.py)
        extra = ("  transitions:\n    - { from: A, to: B, condition: \"x > 1\" }\n"
                 "  regimes:\n    B:\n      contracts: { suspended: [DELIVERY_SLA] }\n")
        v1 = tmp_path / "r1.cadl"
        v1.write_text(V1.read_text().replace("  amendment:\n", extra + "  amendment:\n"))
        v2 = tmp_path / "r2.cadl"
        v2.write_text(V2.read_text().replace("  amendment:\n", extra + "  amendment:\n"))
        lines = [{"t": 3550, "bindings": {"i": 2}}, {"t": 3600, "regime": "B"}]
        r = evaluate_files(v1, _trace(tmp_path, lines), None, [v2], HOUR)
        assert r["versions"]["follows"] == "2"
        assert _norms(r)["pick_up"] == ("discharged", "fulfilled")
        # no regime named: the initial regime A has the contract in force
        lines = [{"t": 3550, "bindings": {"i": 2}}, {"t": 3600}, {"t": 3660}]
        r = evaluate_files(v1, _trace(tmp_path, lines), None, [v2], HOUR)
        assert _norms(r)["pick_up"] == ("violated", "violated")
        # version 2 does not have the contract in force in A: its clock stops
        bad = tmp_path / "r3.cadl"
        bad.write_text(V2.read_text().replace(
            "  amendment:\n", extra.replace("B:\n", "A:\n") + "  amendment:\n"))
        r = evaluate_files(v1, _trace(tmp_path, lines), None, [bad], HOUR)
        assert _norms(r)["pick_up"] == ("discharged", "fulfilled")

    def test_cli_text_and_exit(self):
        code, out, err = _cli("norms-eval", V1, "--version", V2, "--period", "1h", "--trace",
                              TRACES / "amendment_norms_v1_a_switch.jsonl")
        assert code == 1, err
        assert "Versions\n  sequence: 1, 2" in out
        code, out, err = _cli("norms-eval", V1, "--version", V2, "--trace",
                              TRACES / "amendment_norms_v1_a_switch.jsonl", "--origin", "5")
        assert code == 2 and "--origin needs --period" in err


# --- the reference and the differential tests -------------------------------------

def _reference(times, rules, obs):
    """Independent of norms_eval: the indices of the observations at which
    the instance is amended, with the version (K.11). `times[v]` is the
    effective time of version v (None for the first), `rules[v]` (rule,
    states) for v >= 1, `obs` (t, state before)."""
    def in_force(t):
        return max(v for v, tv in enumerate(times) if tv is None or tv <= t)
    out = []
    cur = in_force(obs[0][0])
    waiting = None
    for n, (t, state) in enumerate(obs[1:], start=1):
        k = in_force(t)
        if waiting and state in waiting[1]:       # first the waiting switch
            out.append((n, waiting[0]))
            waiting = None
        for v in range(cur + 1, k + 1):
            waiting = None
            rule, states = rules[v]
            if rule == "switch":
                waiting = (v, states)
                if states is None or state in states:
                    out.append((n, v))
                    waiting = None
        cur = k
    return out


_RULES = [("keep", None), ("switch", None), ("switch", ["Assigned"]),
          ("switch", ["PickedUp"])]


def _random_versions(rng, tmp: Path):
    paths, rules = [], [None]
    for v in range(2, 2 + rng.randint(1, 3)):
        rule, states = rng.choice(_RULES)
        if rule == "keep":
            tr = "    transitional: { default: keep }\n"
        elif states is None:
            tr = ""
        else:
            tr = ("    transitional:\n      contracts:\n        - { contract: DELIVERY_SLA, "
                  f"rule: switch, in_states: [{states[0]}] }}\n")
        pickup = rng.choice(["60s", "90s", "120s", "150s"])
        paths.append(_variant(tmp, f"v{v}.cadl", str(v), str(v - 1), v, tr, pickup=pickup))
        rules.append((rule, states))
    return paths, rules


def _random_trace(rng, versions: int):
    t = Fraction(rng.choice([3400, 3500, 3550, 3590]))
    status, state = "Waiting", "Assigned"
    lines = [{"t": float(t), "bindings": {"i": 2}}]
    obs = [(t, "Assigned")]
    for _ in range(rng.randint(2, 9)):
        t += rng.choice([10, 30, 60, 100, 600, 1800, 3600])
        line = {"t": float(t)}
        before = state
        r = rng.random()
        if state == "Assigned" and r < 0.3:
            status, state = "PickedUp", "PickedUp"
            line.update(values={"ROBOT[2].status": status}, state=state)
        elif state == "PickedUp" and r < 0.2:
            status, state = "Delivered", "Delivered"
            line.update(values={"ROBOT[2].status": status}, state=state)
        lines.append(line)
        obs.append((t, before))
        if state == "Delivered":
            break
    return lines, obs


@pytest.mark.parametrize("seed", range(60))
def test_versions_equal_amend_at_the_reference(tmp_path, seed):
    rng = random.Random(seed)
    paths, rules = _random_versions(rng, tmp_path)
    lines, obs = _random_trace(rng, len(paths) + 1)
    times = [None] + [Fraction(3600 * (v - 1)) for v in range(2, len(paths) + 2)]
    expected = _reference(times, rules, obs)
    got = evaluate_files(V1, _trace(tmp_path, lines), None, paths, HOUR)
    amended = [(a["t"], a["file"]) for a in got["amendments"]]
    assert amended == [(float(obs[n][0]), f"version {v + 1}") for n, v in expected]
    # The same with `amend` at those observations (F.6 directly): one
    # amendment per observation, so only when no two fall on one.
    if len({n for n, _ in expected}) == len(expected):
        plain = [dict(x) for x in lines]
        for n, v in expected:
            plain[n]["amend"] = paths[v - 1].name
        ref = evaluate(load_version(V1), parse_trace(
            [json.dumps(x) for x in plain], path=str(tmp_path / "p.jsonl")))
        for key in ("norms", "violations", "guarantees", "assumptions", "ok"):
            assert got[key] == ref[key], key


@pytest.fixture(scope="module")
def monitors(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mon")
    out = {}
    for name, path in (("m1", V1), ("m2", V2)):
        text = generate_norms_monitor_module(parse_file(path))
        file = tmp / f"am_{name}.py"
        file.write_text(text, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(f"am_{name}", file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"am_{name}"] = mod
        spec.loader.exec_module(mod)
        out[name] = mod
    return out


def _monitor_model(tmp: Path, path: Path) -> dict:
    ns: dict = {}
    text = generate_norms_monitor_module(parse_file(path))
    line = next(x for x in text.splitlines() if x.startswith("MODEL = "))
    exec("import json\n" + line, ns)
    return ns["MODEL"]


@pytest.mark.parametrize("seed", range(40))
def test_monitor_equals_norms_eval(tmp_path, monitors, seed):
    rng = random.Random(1000 + seed)
    paths, _ = _random_versions(rng, tmp_path)
    lines, _ = _random_trace(rng, len(paths) + 1)
    named = rng.random() < 0.4
    if named:          # name the versions instead of the time base
        for x in lines:
            x["version"] = str(1 + max(0, min(len(paths), int(x["t"] // 3600))))
    trace = _trace(tmp_path, lines)
    ref = evaluate_files(V1, trace, None, paths, None if named else HOUR)
    models = {str(i + 2): _monitor_model(tmp_path, p) for i, p in enumerate(paths)}
    got = monitors["m1"].replay(trace.read_text().splitlines(keepends=True),
                                run_versions=models, path=str(trace),
                                period=None if named else 3600)
    ref.pop("file")
    assert got == json.loads(json.dumps(ref))


class TestMonitor:
    def test_online(self, monitors, tmp_path):
        m = monitors["m1"].NormsMonitor("DELIVERY_SLA", bindings={"i": 2},
                                        run_versions=[_monitor_model(tmp_path, V2)],
                                        period=3600)
        m.observe({"t": 3550})
        assert m.version == "1"
        changes = m.observe({"t": 3600})
        assert m.version == "2"
        assert any(c["id"] == "pick_up" for c in changes) or changes == []
        with pytest.raises(monitors["m1"].ObservationError, match="cannot also amend"):
            m.observe({"t": 3601, "amend": "x"})
        assert m.version == "2"
        m.observe({"t": 3641})
        assert m.verdicts()["norms"]["pick_up"]["state"] == "violated"

    def test_refused_named_version_leaves_the_monitor(self, monitors, tmp_path):
        m = monitors["m1"].NormsMonitor("DELIVERY_SLA", bindings={"i": 2},
                                        run_versions=[_monitor_model(tmp_path, V2)],
                                        period=3600)
        m.observe({"t": 3550})
        with pytest.raises(monitors["m1"].TraceError, match="by the effective times"):
            m.observe({"t": 3590, "version": "2"})
        assert m.version == "1" and len(m.result()["timeline"]) == 2
        m.observe({"t": 3600, "version": "2"})
        assert m.version == "2"

    def test_without_versions_unchanged(self, monitors):
        m = monitors["m1"].NormsMonitor("DELIVERY_SLA", bindings={"i": 2})
        m.observe({"t": 3550})
        m.observe({"t": 3660})
        assert "versions" not in m.result()

    def test_versions_keeps_its_0333_meaning(self, monitors, tmp_path):
        # a model given for `amend` is not a version of the run, also when
        # its file declares amendment 0.1: the result is that of
        # norms-eval without --version
        trace = _trace(tmp_path, [{"t": 3550, "bindings": {"i": 2}},
                                  {"t": 3560, "amend": V2.name}, {"t": 3700}])
        (tmp_path / V2.name).write_text(V2.read_text())
        ref = evaluate_files(V1, trace)
        got = monitors["m1"].replay(trace.read_text().splitlines(keepends=True),
                                    versions={V2.name: _monitor_model(tmp_path, V2)},
                                    path=str(trace))
        ref.pop("file")
        assert got == json.loads(json.dumps(ref)) and "versions" not in got
        assert [a["t"] for a in got["amendments"]] == [3560]

    def test_named_version_after_amend_is_refused(self, monitors, tmp_path):
        (tmp_path / V2.name).write_text(V2.read_text())
        m = monitors["m1"].NormsMonitor("DELIVERY_SLA", bindings={"i": 2},
                                        versions={V2.name: _monitor_model(tmp_path, V2)})
        m.observe({"t": 0})
        m.observe({"t": 10, "amend": V2.name})
        with pytest.raises(monitors["m1"].ObservationError, match="cannot name a version"):
            m.observe({"t": 15, "version": "1"})
        assert [a["t"] for a in m.result()["amendments"]] == [10]
        trace = _trace(tmp_path, [{"t": 0, "bindings": {"i": 2}}, {"t": 10, "amend": V2.name},
                                  {"t": 15, "version": "1"}])
        with pytest.raises(NormsEvalError, match="cannot also amend"):
            evaluate_files(V1, trace)

    def test_run_versions_must_carry_a_version(self, monitors, tmp_path):
        model = _monitor_model(tmp_path, EXAMPLES / "norms_amendment_v2.cadl")
        with pytest.raises(monitors["m1"].MonitorError, match="run_versions"):
            monitors["m1"].NormsMonitor("DELIVERY_SLA", run_versions=[model])

    def test_named_later_activates(self, monitors, tmp_path):
        trace = _trace(tmp_path, [{"t": 3550, "bindings": {"i": 2}}, {"t": 3560},
                                  {"t": 3600, "version": "1"}, {"t": 3700}])
        ref = evaluate_files(V1, trace)
        got = monitors["m1"].replay(trace.read_text().splitlines(keepends=True),
                                    path=str(trace))
        ref.pop("file")
        assert got == json.loads(json.dumps(ref)) and "versions" in got


# --- norms-check, codegen ----------------------------------------------------------

class TestRefusals:
    def test_norms_check_in_states(self):
        with pytest.raises(NormsCheckError, match="in_states"):
            build_model(load_version(V1), versions=[V2])

    def test_norms_check_keep(self, tmp_path):
        v2 = _variant(tmp_path, "k.cadl", "2", "1", 2, "    transitional: { default: keep }\n")
        with pytest.raises(NormsCheckError, match="is keep"):
            build_model(load_version(V1), versions=[v2])

    def test_norms_check_switch_accepted(self, tmp_path):
        v2 = _variant(tmp_path, "s.cadl", "2", "1", 2, "")
        build_model(load_version(V1), versions=[v2])

    @pytest.mark.parametrize("target", ["solidity", "opa"])
    def test_d5_monitor_of_a_switching_contract(self, target, tmp_path):
        [m] = norms_monitors(parse_file(V2), target)
        assert m["text"] is None and "cannot take over a running instance" in m["reasons"][0]
        [m] = norms_monitors(parse_file(V1), target)          # no previous:
        assert m["text"] is not None
        keep = _variant(tmp_path, "k.cadl", "2", "1", 2, "    transitional: { default: keep }\n")
        [m] = norms_monitors(parse_file(keep), target)
        assert m["text"] is not None

    def test_unity(self, tmp_path):
        with pytest.raises(ValueError, match="cannot be moved to a new version"):
            generate(parse_file(TOWN2), tmp_path / "u2", "unity-csharp")
        generate(parse_file(TOWN1), tmp_path / "u1", "unity-csharp")

    def test_version_module(self, tmp_path):
        generate(parse_file(TOWN2), tmp_path / "py", "python")
        ns: dict = {}
        exec((tmp_path / "py" / "version.py").read_text(), ns)
        assert ns["VERSION"] == "2" and ns["PREVIOUS"] == "1"
        assert ns["PARAMETERS"]["block_k"] == 2 and ns["PARAMETERS"]["hold_limit"] == 60
        assert ns["transitional_rule"]("ROAD_USE") == ("switch", None)
        assert ns["transitional_rule"]("DELIVERY_SLA") == ("keep", None)
        assert ns["effective_time"](3600) == 3600 and ns["effective_time"]() is None
        generate(parse_file(EXAMPLES / "norms_amendment_v1.cadl"), tmp_path / "py0", "python")
        assert not (tmp_path / "py0" / "version.py").exists()


# --- metrics-eval ------------------------------------------------------------------

class TestMetricsEval:
    def test_example(self):
        r = metrics_files(TOWN1, EXAMPLES / "measurements" / "amendment_robot_town.jsonl",
                          [TOWN2], HOUR)
        assert [m["verdict"] for m in r["measurements"]] == [
            "met", "not met", "not met", "met", "not judged", "met"]
        assert r["measurements"][5]["target"] == ">= 0.85 * count(orders)"
        assert r["measurements"][2]["version"] == "2"
        assert r["counts"] == {"met": 3, "not met": 2, "not judged": 1} and not r["ok"]

    def _file(self, tmp: Path, measure: str, schedule: str = "", target: str = ">= 10") -> Path:
        text = TOWN1.read_text()
        start = text.index("  metrics:\n")
        end = text.index("  amendment:\n")
        metrics = (f"  metrics:\n    - id: m\n      formula: \"x\"\n      target: \"{target}\"\n"
                   + measure + schedule)
        path = tmp / "m.cadl"
        path.write_text(text[:start] + metrics + text[end:])
        return path

    def test_every_and_median(self, tmp_path):
        f = self._file(tmp_path, "      measure: { window: 1000s, every: 300s, aggregate: median,"
                                 " runs: 2, across_runs: min }\n")
        recs = tmp_path / "r.jsonl"
        recs.write_text(json.dumps({"metric": "m", "t": 0, "runs": [[1, 30, 20, 5],
                                                                    [12, 11, 13, 10]]}) + "\n")
        r = metrics_files(f, recs)
        m = r["measurements"][0]
        assert m["value"] == 11.5 and m["verdict"] == "met"        # min(12.5, 11.5)
        recs.write_text(json.dumps({"metric": "m", "t": 0, "runs": [[1, 2, 3], [1, 2, 3]]})
                        + "\n")
        with pytest.raises(TraceError, match="4 samples per run"):
            metrics_files(f, recs)

    def test_not_judged(self, tmp_path):
        f = self._file(tmp_path, "", target=">= 0.5 * count(orders)")
        recs = tmp_path / "r.jsonl"
        recs.write_text(json.dumps({"metric": "m", "t": 0, "value": 3}) + "\n"
                        + json.dumps({"metric": "nope", "t": 0, "value": 3}) + "\n")
        r = metrics_files(f, recs)
        assert [m["verdict"] for m in r["measurements"]] == ["not judged", "not judged"]
        assert "no value for 'count(orders)'" in r["measurements"][0]["reason"]
        assert "has no metric 'nope'" in r["measurements"][1]["reason"]
        assert r["ok"]
        f = self._file(tmp_path, "", target="high")
        recs.write_text(json.dumps({"metric": "m", "t": 0, "value": 3}) + "\n")
        assert "not a comparison" in metrics_files(f, recs)["measurements"][0]["reason"]

    def test_schedule_without_time_base(self, tmp_path):
        f = self._file(tmp_path, "", "      schedule:\n        - { from: { period: 2 }, "
                                     "target: \">= 20\" }\n")
        recs = tmp_path / "r.jsonl"
        recs.write_text(json.dumps({"metric": "m", "t": 0, "value": 30}) + "\n")
        r = metrics_files(f, recs)
        assert "give --period" in r["measurements"][0]["reason"]
        r = metrics_files(f, recs, [], HOUR)
        assert r["measurements"][0]["verdict"] == "met"

    def test_exact_numbers(self, tmp_path):
        f = self._file(tmp_path, "", target="== 0.3")
        recs = tmp_path / "r.jsonl"
        recs.write_text(json.dumps({"metric": "m", "t": 0, "runs": [0.1, 0.2]}) + "\n")
        with pytest.raises(TraceError, match="has no runs"):
            metrics_files(f, recs)
        f = self._file(tmp_path, "      measure: { runs: 2, across_runs: sum }\n", target="== 0.3")
        assert metrics_files(f, recs)["measurements"][0]["verdict"] == "met"

    def test_cli(self):
        code, out, err = _cli("metrics-eval", TOWN1, "--version", TOWN2, "--period", "3600s",
                              "--measurements",
                              EXAMPLES / "measurements" / "amendment_robot_town.jsonl")
        assert code == 1, err
        assert "Result: 3 met, 2 not met, 1 not judged" in out


def test_files_without_the_extension_have_no_chain():
    sos = load_version(EXAMPLES / "norms_amendment_v1.cadl")
    assert version_chain(sos) is None


def test_generated_metric_checks(tmp_path):
    """metrics.py applies schedule: at a given time, and an uncompilable
    target is not judged (None), not met (K.11)."""
    text = TOWN1.read_text()
    start, end = text.index("  metrics:\n"), text.index("  amendment:\n")
    metrics = ("  metrics:\n    - id: m\n      formula: \"x\"\n      target: \"<= 600s\"\n"
               "      schedule:\n        - { from: { period: 2 }, target: \"<= 300s\" }\n"
               "        - { from: { period: 3 }, target: \">= 0.5 * count(x)\" }\n"
               "    - id: n\n      formula: \"x\"\n      target: \">= 0.9 * count(orders)\"\n")
    f = tmp_path / "m.cadl"
    f.write_text(text[:start] + metrics + text[end:])
    generate(parse_file(f), tmp_path / "pkg", "python")
    spec = importlib.util.spec_from_file_location("am_metrics", tmp_path / "pkg" / "metrics.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod.MetricsCollector.__new__(mod.MetricsCollector)
    assert m.check_m_target(500) is None                      # no time
    assert m.check_m_target(500, t=100) is None               # no time base
    assert m.check_m_target(500, t=100, period=3600) is True
    assert m.check_m_target(500, t=3600, period=3600) is False
    assert m.check_m_target(1, t=7200, period=3600) is None   # not compiled
    assert m.check_n_target(1) is None
    ns: dict = {}
    exec((tmp_path / "pkg" / "version.py").read_text(), ns)
    assert ns["SCHEDULES"]["m"][0] == ({"period": 2}, "<= 300s")


class TestReviewFixes:
    def test_version_change_inside_window_not_judged(self, tmp_path):
        # M3: a new version with another target inside the window is judged
        # like a schedule entry there
        text = TOWN1.read_text()
        start, end = text.index("  metrics:\n"), text.index("  amendment:\n")
        m1 = "  metrics:\n    - id: w\n      formula: \"x\"\n      target: \">= 40\"\n" \
             "      measure: { window: 3600s }\n"
        v1 = tmp_path / "v1.cadl"
        v1.write_text(text[:start] + m1 + text[end:])
        v2 = tmp_path / "v2.cadl"
        v2.write_text(TOWN2.read_text().replace(
            TOWN2.read_text()[TOWN2.read_text().index("  metrics:\n"):
                              TOWN2.read_text().index("  amendment:\n")],
            m1.replace(">= 40", ">= 45")))
        recs = tmp_path / "r.jsonl"
        recs.write_text("".join(json.dumps(x) + "\n" for x in (
            {"metric": "w", "t": 0, "value": 42}, {"metric": "w", "t": 1, "value": 42},
            {"metric": "w", "t": 3600, "value": 42}, {"metric": "w", "t": 2, "value": 42,
                                                         "values": {"a": None}})))
        r = metrics_files(v1, recs, [v2], HOUR)
        assert [m["verdict"] for m in r["measurements"]] == [
            "met", "not judged", "not met", "not judged"]
        assert "version 2, in force from t=3600" in r["measurements"][1]["reason"]

    def test_null_term_is_not_judged(self, tmp_path):
        f = TestMetricsEval()._file(tmp_path, "", target=">= 0.5 * count(orders)")
        recs = tmp_path / "r.jsonl"
        recs.write_text(json.dumps({"metric": "m", "t": 0, "value": 3,
                                    "values": {"count(orders)": None}}) + "\n")
        m = metrics_files(f, recs)["measurements"][0]
        assert m["verdict"] == "not judged" and "no value" in m["reason"]

    @pytest.mark.parametrize("origin", ["Infinity", "1e400", "nan"])
    def test_bad_origin_is_a_usage_error(self, origin):
        code, _, err = _cli("norms-eval", V1, "--version", V2, "--period", "3600s", "--origin",
                            origin, "--trace", TRACES / "amendment_norms_v1_a_switch.jsonl")
        assert code == 2 and "Error:" in err and "Traceback" not in err

    def test_versions_without_time_base_warn(self):
        code, _, err = _cli("norms-eval", V1, "--version", V2, "--trace",
                            TRACES / "amendment_norms_v1_a_switch.jsonl")
        assert code == 0 and "no time base" in err

    def test_amend_to_a_keep_version_warns(self, tmp_path):
        (tmp_path / V2.name).write_text(V2.read_text())
        trace = _trace(tmp_path, [{"t": 0, "bindings": {"i": 2}}, {"t": 10, "amend": V2.name}])
        notes: list = []
        r = evaluate_files(V1, trace, notes=notes)
        assert [a["t"] for a in r["amendments"]] == [10]
        assert len(notes) == 1 and "switch in Assigned" in notes[0]
        code, _, err = _cli("norms-eval", V1, "--trace", trace)
        assert "Warning: 'amend' to amendment_norms_v2.cadl" in err

    def test_monitor_amend_warning(self, monitors, tmp_path):
        m = monitors["m1"].NormsMonitor("DELIVERY_SLA", bindings={"i": 2},
                                        versions={"v2": _monitor_model(tmp_path, V2)})
        m.observe({"t": 0})
        m.observe({"t": 10, "amend": "v2"})
        assert len(m.amend_warnings) == 1 and "switch in Assigned" in m.amend_warnings[0]
        assert "warnings" in m.result() and not any(
            "transitional" in w for w in m.result()["warnings"])
