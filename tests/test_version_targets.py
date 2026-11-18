"""Norms monitors of the OPA and Solidity targets that follow the versions
of a run (Appendix K, K.11; Appendix F, F.6), cadl 0.3.36: `cadl codegen
-t opa|solidity FILE --version OTHER ...` writes a monitor that contains the
contract of every version; the caller names the version in force (the key
`version` of an observation in Rego, setVersion of the orchestrator in
Solidity, run on an EVM with CADL_EVM_PYTHON). The monitors are compared after every
observation with the norms monitor of the Python target with run_versions
(tests/norms_targets_support.reference_versions) on random runs of
versions (tests/versions_targets_support.py) and random traces.

CADL_VERSION_TARGET_RUNS sets the number of random runs of the Rego test
(default 8, with 12 traces each), CADL_VERSION_SOL_RUNS that of the
Solidity test (default 4)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from cadl.codegen import generate, norms_monitors
from cadl.codegen.norms_ir import NormsTargetUnsupported
from cadl.codegen.opa.norms_monitor_rego import (
    generate_norms_monitor_rego_versions, monitor_package,
)
from cadl.parser import parse_file

from tests import norms_targets_support as S
from tests import versions_targets_support as V
from tests.test_norms_monitor import _ROBOT, _random_trace

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
needs_opa = pytest.mark.skipif(S.opa_binary() is None, reason="the opa binary is not on PATH")
RUNS = int(os.environ.get("CADL_VERSION_TARGET_RUNS", "8"))


def _traces(rng: random.Random, count: int, n_versions: int, strings: bool = False) -> list:
    out = []
    for _ in range(count):
        lines = [l for l in _random_trace(rng, _ROBOT) if l.strip()][:S.MAX_STEPS]
        lines = V.with_versions(rng, lines, n_versions)
        if rng.random() < 0.05 and len(lines) > 2 and "end" not in json.loads(lines[0]):
            # a version that is not of the run (the monitor checks the
            # versions of the observations up to the end of the instance)
            rec = json.loads(lines[1])
            rec["version"] = "9"
            lines[1] = json.dumps(rec)
        lines = _early_end(rng, lines, strings)
        out.append(lines)
    return out


def _early_end(rng: random.Random, lines: list, strings: bool = False) -> list:
    """Sometimes a terminal state early in the trace, and after it, sometimes
    a version that is not of the run (`cadl norms-eval` refuses it also after
    the end of the instance) or one before the version in force (accepted
    after the end). `strings`: only names (setVersion of Solidity takes a
    string)."""
    if len(lines) < 3 or rng.random() >= 0.2:
        return lines
    recs = [json.loads(x) for x in lines]
    k = rng.randrange(len(recs) - 1)
    recs[k]["state"] = rng.choice(["Completed", "Violated"])
    x = rng.random()
    j = rng.randrange(k + 1, len(recs))
    if x < 0.3:
        recs[j]["version"] = "9"
    elif x < 0.5:
        recs[j]["version"] = "1"
    elif x < 0.6 and not strings:
        recs[j]["version"] = 3
    return [json.dumps(r) for r in recs]


@needs_opa
def test_rego_against_the_python_monitor(tmp_path):
    rng = random.Random(36)
    seen = {"traces": 0, "switched": 0, "errors": 0, "effects": set()}
    for r in range(RUNS):
        soses, texts, rules = V.make_run(rng)
        c = soses[0].contracts[0]
        text = generate_norms_monitor_rego_versions(soses[0], c, soses[1:])
        traces = _traces(rng, 12, len(soses))
        refs = [S.reference_versions(soses, t) for t in traces]
        got = S.run_opa(text, monitor_package(c.id), traces, refs, tmp_path / f"r{r}")
        bad = S.compare_versions(refs, got)
        assert bad == [], (texts, traces[bad[0][0]], bad[0])
        seen["traces"] += len(traces)
        seen["switched"] += sum(1 for x in refs if x.get("follows"))
        seen["errors"] += sum(1 for x in refs if "error" in x)
        for x in refs:
            seen["effects"].update(x.get("effects", []))
    assert seen["switched"] > seen["traces"] // 6
    assert seen["errors"] > 0
    assert {"carry_over", "added"} <= seen["effects"]


def test_codegen_takes_the_versions(tmp_path):
    v1, v2 = EXAMPLES / "amendment_norms_v1.cadl", EXAMPLES / "amendment_norms_v2.cadl"
    # without the other version the monitor of version 2 is refused (K.11) ...
    [m] = norms_monitors(parse_file(v2), "opa")
    assert m["text"] is None and "--version" in m["reasons"][0]
    # ... with it, generated, from either file, for the same run
    [m] = norms_monitors(parse_file(v2), "opa", [parse_file(v1)])
    [m1] = norms_monitors(parse_file(v1), "opa", [parse_file(v2)])
    assert m["text"] is not None and m1["text"] is not None
    assert "versions 1, 2" in m["text"]
    proc = subprocess.run(
        [sys.executable, "-c", "import sys;from cadl.cli import main;sys.argv=['cadl',*sys.argv[1:]];"
         "sys.exit(main())", "codegen", str(v2), "-t", "opa", "--version", str(v1),
         "-o", str(tmp_path / "out")], capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out" / "norms_monitor_delivery_sla.rego").exists()
    with pytest.raises(ValueError, match="does not take the other versions"):
        generate(parse_file(v1), tmp_path / "py", "python", versions=[parse_file(v2)])


def test_rego_versions_with_regimes_are_combined():
    from tests.test_regime_runtime import _with_regimes
    from cadl.parser import parse
    v1 = EXAMPLES / "amendment_norms_v1.cadl"
    a = parse(_with_regimes(v1, "DELIVERY_SLA"))
    text = generate_norms_monitor_rego_versions(a, a.contracts[0],
                                                [parse_file(EXAMPLES / "amendment_norms_v2.cadl")])
    assert "# >>> norms-monitor-regimes-versions" in text and "\nv_result := {" in text
    # without regimes the monitor is that of 0.3.35
    b = parse_file(v1)
    plain = generate_norms_monitor_rego_versions(b, b.contracts[0],
                                                 [parse_file(EXAMPLES / "amendment_norms_v2.cadl")])
    assert "norms-monitor-regimes-versions" not in plain and "\nresult := {" in plain


REGO_REGIME_RUNS = int(os.environ.get("CADL_VERSION_REGO_REGIME_RUNS", "8"))


def _regime_traces(rng: random.Random, count: int, n_versions: int) -> list:
    out = []
    for _ in range(count):
        lines = [l for l in _random_trace(rng, _ROBOT) if l.strip()][:S.MAX_STEPS]
        lines = V.with_regimes(rng, V.with_versions(rng, lines, n_versions))
        if rng.random() < 0.5:
            lines = V.with_states(rng, lines)
        out.append(_early_end(rng, lines))
    return out


@needs_opa
def test_rego_versions_with_regimes_against_the_python_monitor(tmp_path):
    """Versions whose regime bindings take the contract out of force (K.11,
    "Versions and regimes"), against the norms monitor of the Python target
    with run_versions, observation by observation. Every fourth run may have
    a version without regimes: (its traces are refused when the run also
    uses a version with them)."""
    rng = random.Random(3638)
    seen = {"traces": 0, "out": 0, "switched": 0, "errors": 0}
    for r in range(REGO_REGIME_RUNS):
        soses, texts, rules = V.make_run(rng, regimes=True, items=r % 2 == 1, mixed=r % 4 == 3)
        c = soses[0].contracts[0]
        text = generate_norms_monitor_rego_versions(soses[0], c, soses[1:])
        traces = _regime_traces(rng, 12, len(soses))
        mod, models = V.python_monitor(soses, tmp_path / f"r{r}", f"pm_rego_vr_{r}")
        refs = [V.reference_monitor(mod, models, t) for t in traces]
        got = S.run_opa(text, monitor_package(c.id), traces, refs, tmp_path / f"r{r}")
        bad = V.compare_never(refs, got)
        assert bad == [], (texts, traces[bad[0][0]], bad[0])
        seen["traces"] += len(traces)
        seen["out"] += sum(1 for x in refs if x.get("out"))
        seen["switched"] += sum(1 for x in refs if x.get("follows"))
        seen["errors"] += sum(1 for x in refs if "error" in x)
    assert seen["out"] > seen["traces"] // 4 and seen["switched"] > 0 and seen["errors"] > 0


def _amendment_run(regimes_v1: bool = True) -> list:
    from tests.test_regime_runtime import _with_regimes
    from cadl.parser import parse
    v1, v2 = EXAMPLES / "amendment_norms_v1.cadl", EXAMPLES / "amendment_norms_v2.cadl"
    return [parse(_with_regimes(v1, "DELIVERY_SLA")) if regimes_v1 else parse_file(v1),
            parse(_with_regimes(v2, "DELIVERY_SLA"))]


# Runs of amendment_norms_v1/v2 with the regimes NORMAL and STORM (STORM does
# not have DELIVERY_SLA in force); version 2 has pick_up within 90 s instead
# of 120 s and switches an instance in state Assigned.
_SCENARIOS = {
    # version 2 takes effect while not in force; the instance saw Assigned
    # last (the state PickedUp given meanwhile is seen only at the return),
    # so it switches, at the return: the deadline is 90 s on the clock
    "frozen state": ['{"t": 0, "bindings": {"i": 1}}', '{"t": 10, "regime": "STORM"}',
                     '{"t": 20, "state": "PickedUp"}', '{"t": 30, "version": "2"}',
                     '{"t": 40, "regime": "NORMAL"}', '{"t": 45, "end": false}'],
    # the switch made while not in force violates pick_up (110 s > 90 s) at
    # the end of the instance, on the stopped clock
    "amendment violation at the end": [
        '{"t": 0, "bindings": {"i": 1}}', '{"t": 100}', '{"t": 110, "regime": "STORM"}',
        '{"t": 120, "version": "2"}', '{"t": 130, "end": true}'],
    # the same, ended by the end of the trace (finish)
    "amendment violation at the finish": [
        '{"t": 0, "bindings": {"i": 1}}', '{"t": 100}', '{"t": 110, "regime": "STORM"}',
        '{"t": 120, "version": "2"}', '{"t": 130}'],
    # the instance starts at the return, under version 2
    "start under a later version": [
        '{"t": 0, "bindings": {"i": 1}, "regime": "STORM"}', '{"t": 5, "version": "2"}',
        '{"t": 10, "regime": "NORMAL"}', '{"t": 20, "end": false}'],
    # starts and ends while not in force
    "never in force, ended": ['{"t": 0, "bindings": {"i": 1}, "regime": "STORM"}',
                              '{"t": 5, "version": "2", "end": true}'],
    "never in force": ['{"t": 0, "bindings": {"i": 1}, "regime": "STORM"}',
                       '{"t": 5, "version": "2", "end": false}'],
    # a version may not go back, also while not in force
    "back": ['{"t": 0, "bindings": {"i": 1}, "version": "2"}', '{"t": 5, "regime": "STORM"}',
             '{"t": 6, "version": "1"}'],
}


@needs_opa
def test_rego_versions_with_regimes_scenarios(tmp_path):
    soses = _amendment_run()
    c = soses[0].contracts[0]
    text = generate_norms_monitor_rego_versions(soses[0], c, soses[1:])
    mod, models = V.python_monitor(soses, tmp_path / "py", "pm_rego_vr_scen")
    traces = list(_SCENARIOS.values())
    refs = [V.reference_monitor(mod, models, t) for t in traces]
    got = S.run_opa(text, monitor_package(c.id), traces, refs, tmp_path / "opa")
    assert V.compare_never(refs, got) == []
    by = dict(zip(_SCENARIOS, refs))
    assert by["frozen state"]["follows"]
    assert by["frozen state"]["steps"][4]["next_deadline"] == 120     # 40 + (90 - 10)
    for name in ("amendment violation at the end", "amendment violation at the finish"):
        assert by[name]["steps"][-1]["violations"][0][:4] == ["pick_up", "violated", False, None]
    assert by["start under a later version"]["steps"][2]["next_deadline"] == 100
    assert by["never in force"].get("never") and "error" in by["back"]


@needs_opa
def test_rego_versions_with_and_without_regimes(tmp_path):
    """A run that uses a version without regimes: and one with them is
    refused (K.11): the run is in no regime of the second."""
    soses = _amendment_run(regimes_v1=False)
    c = soses[0].contracts[0]
    text = generate_norms_monitor_rego_versions(soses[0], c, soses[1:])
    mod, models = V.python_monitor(soses, tmp_path / "py", "pm_rego_vr_mixed")
    traces = [['{"t": 0, "bindings": {"i": 1}}', '{"t": 5, "version": "2"}'],
              ['{"t": 0, "bindings": {"i": 1}}', '{"t": 5}']]
    refs = [V.reference_monitor(mod, models, t) for t in traces]
    assert "error" in refs[0] and "error" not in refs[1]
    got = S.run_opa(text, monitor_package(c.id), traces, refs, tmp_path / "opa")
    assert V.compare_never(refs, got) == []


# === Solidity ======================================================================

from cadl.codegen.solidity.norms_monitor_sol import (
    INITCODE_LIMIT, RUNTIME_LIMIT, estimated_initcode, generate_norms_monitor_sol_versions,
    monitor_name,
)

REQUIRE_EVM = os.environ.get("CADL_REQUIRE_EVM") == "1"
needs_evm = pytest.mark.skipif(
    not REQUIRE_EVM and (S.solc_binary() is None or S.evm_python() is None),
    reason="no solc, or no EVM (CADL_EVM_PYTHON: a Python with eth-tester[py-evm])")
SOL_RUNS = int(os.environ.get("CADL_VERSION_SOL_RUNS", "4"))


def _sol_check(soses, traces, tmp: Path) -> tuple:
    """(mismatches, references) of the Solidity monitor of the run on the traces."""
    c = soses[0].contracts[0]
    text = generate_norms_monitor_sol_versions(soses[0], c, soses[1:])
    compiled = S.compile_solidity(text, monitor_name(c.id), tmp)
    mine = compiled[f"Monitor.sol:{monitor_name(c.id)}"]
    assert len(mine["bin"]) // 2 <= min(estimated_initcode(text), INITCODE_LIMIT)
    assert mine["runtime"] <= RUNTIME_LIMIT
    assert compiled["Monitor.sol:CadlNormsVersions"]["runtime"] <= RUNTIME_LIMIT
    refs = [S.reference_versions(soses, t) for t in traces]
    runs = [V.sol_run_versions(t, r.get("quantities", [])) for t, r in zip(traces, refs)]
    raw = S.run_evm(compiled, f"Monitor.sol:{monitor_name(c.id)}", runs, tmp, ctor_args=2,
                    ids=True)
    return S.compare_versions(refs, V.sol_results_versions(raw, runs)), refs


@needs_evm
def test_solidity_against_the_python_monitor(tmp_path):
    rng = random.Random(3636)
    seen = {"traces": 0, "switched": 0, "errors": 0, "effects": set()}
    for r in range(SOL_RUNS):
        # every other run also changes the assume: and guarantee: items
        soses, texts, rules = V.make_run(rng, items=r % 2 == 1)
        traces = [t for t in _traces(rng, 12, len(soses), strings=True)
                  if S.scalar_values(t) and S.six_decimals(t)]
        bad, refs = _sol_check(soses, traces, tmp_path / f"r{r}")
        assert bad == [], (texts, traces[bad[0][0]], bad[0])
        seen["traces"] += len(traces)
        seen["switched"] += sum(1 for x in refs if x.get("follows"))
        seen["errors"] += sum(1 for x in refs if "error" in x)
        for x in refs:
            seen["effects"].update(x.get("effects", []))
    assert seen["switched"] > seen["traces"] // 6
    assert seen["errors"] > 0
    assert {"carry_over"} <= seen["effects"]


@needs_evm
def test_solidity_only_the_orchestrator_sets_the_version(tmp_path):
    v1, v2 = parse_file(EXAMPLES / "amendment_norms_v1.cadl"), parse_file(EXAMPLES / "amendment_norms_v2.cadl")
    c = v1.contracts[0]
    compiled = S.compile_solidity(generate_norms_monitor_sol_versions(v1, c, [v2]),
                                  monitor_name(c.id), tmp_path)
    first = json.dumps({"t": 0, "bindings": {"i": 2}})
    good = V.sol_run_versions([first, json.dumps({"t": 5, "version": "2"})], [])
    other = json.loads(json.dumps(good))
    other["steps"][1]["version_as"] = 1        # another account than the deployer
    unknown = V.sol_run_versions([first, json.dumps({"t": 5, "version": "9"})], [])
    back = V.sol_run_versions([json.dumps({"t": 0, "bindings": {"i": 2}, "version": "2"}),
                               json.dumps({"t": 5, "version": "1"})], [])
    raw = S.run_evm(compiled, f"Monitor.sol:{monitor_name(c.id)}", [good, other, unknown, back],
                    tmp_path, ctor_args=2, ids=True)
    assert all("error" not in x for x in raw[0])
    assert "only the orchestrator" in raw[1][1]["error"]
    assert "version of the run" in raw[2][1]["error"]
    assert "comes before the version in force" in raw[3][1]["error"]


def test_solidity_codegen_takes_the_versions(tmp_path):
    v1, v2 = EXAMPLES / "amendment_norms_v1.cadl", EXAMPLES / "amendment_norms_v2.cadl"
    [m] = norms_monitors(parse_file(v2), "solidity")
    assert m["text"] is None and "--version" in m["reasons"][0]
    [m] = norms_monitors(parse_file(v2), "solidity", [parse_file(v1)])
    [m1] = norms_monitors(parse_file(v1), "solidity", [parse_file(v2)])
    assert m["text"] is not None and m["text"] == m1["text"]
    assert "versions 1, 2" in m["text"] and "is CadlNormsMonitorVersions {" in m["text"]
    proc = subprocess.run(
        [sys.executable, "-c", "import sys;from cadl.cli import main;sys.argv=['cadl',*sys.argv[1:]];"
         "sys.exit(main())", "codegen", str(v2), "-t", "solidity", "--version", str(v1),
         "-o", str(tmp_path / "out")], capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out" / "DeliverySlaNormsMonitor.sol").exists()


def test_solidity_versions_with_regimes_are_combined():
    from tests.test_regime_runtime import _with_regimes
    from cadl.parser import parse
    a = parse(_with_regimes(EXAMPLES / "amendment_norms_v1.cadl", "DELIVERY_SLA"))
    text = generate_norms_monitor_sol_versions(a, a.contracts[0],
                                               [parse_file(EXAMPLES / "amendment_norms_v2.cadl")])
    assert " is CadlNormsMonitorRegimesVersions {" in text
    assert '_versionRegime(string(hex"' in text


SOL_REGIME_RUNS = int(os.environ.get("CADL_VERSION_SOL_REGIME_RUNS", "4"))


@needs_evm
def test_solidity_versions_with_regimes_against_the_python_monitor(tmp_path):
    """Versions whose regime bindings take the contract out of force (K.11,
    "Versions and regimes"), against the norms monitor of the Python target
    with run_versions, observation by observation."""
    rng = random.Random(3637)
    seen = {"traces": 0, "out": 0, "switched": 0}
    for r in range(SOL_REGIME_RUNS):
        soses, texts, rules = V.make_run(rng, regimes=True, items=r % 2 == 1)
        c = soses[0].contracts[0]
        text = generate_norms_monitor_sol_versions(soses[0], c, soses[1:])
        assert " is CadlNormsMonitorRegimesVersions {" in text
        traces = []
        for _ in range(12):
            lines = [l for l in _random_trace(rng, _ROBOT) if l.strip()][:S.MAX_STEPS]
            lines = V.with_regimes(rng, V.with_versions(rng, lines, len(soses)))
            lines = _early_end(rng, lines, strings=True)
            if S.scalar_values(lines) and S.six_decimals(lines):
                traces.append(lines)
        mod, models = V.python_monitor(soses, tmp_path / f"r{r}", f"pm_vr_{r}")
        refs = [V.reference_monitor(mod, models, t) for t in traces]
        compiled = S.compile_solidity(text, monitor_name(c.id), tmp_path / f"r{r}")
        mine = compiled[f"Monitor.sol:{monitor_name(c.id)}"]
        assert len(mine["bin"]) // 2 <= estimated_initcode(text) and mine["runtime"] <= RUNTIME_LIMIT
        runs = [V.sol_run_versions(t, ref.get("quantities", [])) for t, ref in zip(traces, refs)]
        raw = S.run_evm(compiled, f"Monitor.sol:{monitor_name(c.id)}", runs, tmp_path / f"r{r}",
                        ctor_args=2, ids=True)
        bad = V.compare_never(refs, V.sol_results_versions(raw, runs))
        assert bad == [], (texts, traces[bad[0][0]], bad[0])
        seen["traces"] += len(traces)
        seen["out"] += sum(1 for x in refs if x.get("out"))
        seen["switched"] += sum(1 for x in refs if x.get("follows"))
    assert seen["out"] > seen["traces"] // 4 and seen["switched"] > 0


def test_solidity_versions_with_a_different_lifecycle_are_refused():
    v1 = (EXAMPLES / "amendment_norms_v1.cadl").read_text(encoding="utf-8")
    v2 = (EXAMPLES / "amendment_norms_v2.cadl").read_text(encoding="utf-8")
    from cadl.parser import parse
    a, b = parse(v1), parse(v2)
    if a.contracts[0].lifecycle is None:
        pytest.skip("the example has no lifecycle")
    b.contracts[0].lifecycle.terminal = list(b.contracts[0].lifecycle.terminal)[:0]
    with pytest.raises(NormsTargetUnsupported, match="lifecycle"):
        generate_norms_monitor_sol_versions(a, a.contracts[0], [b])


# === unity-csharp ===================================================================

import shutil

needs_mono = pytest.mark.skipif(not (shutil.which("mcs") and shutil.which("mono")),
                                reason="Mono (mcs, mono) is not installed")
TOWN1 = EXAMPLES / "amendment_robot_town_v1.cadl"
TOWN2 = EXAMPLES / "amendment_robot_town_v2.cadl"


def _town2(tmp: Path, rule: str) -> Path:
    """Version 2 with the release deadline of ROAD_USE at 30 s and the given rule."""
    text = TOWN2.read_text(encoding="utf-8")
    head, tail = text.split("      monitors:\n        - id: blocking_guard", 1)
    head = head[:head.rindex("deadline: 60s")] + "deadline: 30s" + head[head.rindex("deadline: 60s") + 13:]
    text = head + "      monitors:\n        - id: blocking_guard" + tail
    text = text.replace("      - id: hold_limit                # seconds a robot may stop on the road\n"
                        "        layer: rule\n        value: 60s",
                        "      - id: hold_limit\n        layer: rule\n        value: 30s")
    text = text.replace("          rule: switch                # a robot waiting on the road follows the new rule at once",
                        "          " + rule)
    path = tmp / "town2.cadl"
    path.write_text(text, encoding="utf-8")
    return path


_SCENARIO = """
using System; using System.Collections.Generic; using CADL.SosDsl;
class M { static void Main() {
  var rt = new ContractRuntime();
  rt.Events.Subscribe(e => Console.WriteLine(e.Kind + "|" + e.TransitionId + "|" + e.FromState + "|"
                                             + e.ToState + "|" + e.DetectedBy + "|" + e.TMs));
  var c = new RoadUseContract("r1", rt, 0);
  rt.Register(c);
  c.SetVersion("2", 10000, rt);
  Console.WriteLine("follows " + c.Version);
  foreach (var t in new long[] {31000, 61000, 62000, 92000}) rt.Tick(t);
  Console.WriteLine("follows " + c.Version);
}}
"""


def _run_scenario(tmp: Path, rule: str) -> list:
    out = tmp / rule.replace(" ", "_").replace(":", "").replace("[", "").replace("]", "")
    generate(parse_file(TOWN1), out, "unity-csharp", versions=[parse_file(_town2(tmp, rule))])
    (out / "M.cs").write_text(_SCENARIO, encoding="utf-8")
    sources = [str(p) for p in sorted(out.rglob("*.cs"))]
    subprocess.run(["mcs", "-nologo", "-langversion:experimental", f"-out:{out / 'm.exe'}",
                    *sources], check=True, capture_output=True, text=True)
    return subprocess.run(["mono", str(out / "m.exe")], check=True, capture_output=True,
                          text=True).stdout.splitlines()


@needs_mono
@pytest.mark.parametrize("rule,want", [
    # switch: at once, the deadline armed at 0 is now 30 s
    ("rule: switch", ["Lifecycle|<open>||Holding||0", "Lifecycle|<version>|Holding|Holding||10000",
                      "follows 2", "Violation||||deadline:release|31000",
                      "Lifecycle|<jump>|Holding|Overstay||31000",
                      "Violation||||deadline:release|61000",
                      "Lifecycle|<jump>|Overstay|Overstay||61000",
                      "Violation||||deadline:release|92000",
                      "Lifecycle|<jump>|Overstay|Overstay||92000", "follows 2"]),
    # keep: the instance stays on version 1 (60 s)
    ("rule: keep", ["Lifecycle|<open>||Holding||0", "follows 1",
                    "Violation||||deadline:release|61000",
                    "Lifecycle|<jump>|Holding|Overstay||61000", "follows 1"]),
    # in_states: waits in Holding; in Overstay (at 61 s) it switches at the next
    # tick, with the deadline armed at 61 s now 30 s
    ("{ rule: switch, in_states: [Overstay] }",
     ["Lifecycle|<open>||Holding||0", "follows 1", "Violation||||deadline:release|61000",
      "Lifecycle|<jump>|Holding|Overstay||61000", "Lifecycle|<version>|Overstay|Overstay||62000",
      "Violation||||deadline:release|92000", "Lifecycle|<jump>|Overstay|Overstay||92000",
      "follows 2"]),
])
def test_unity_follows_the_versions(rule, want, tmp_path):
    if rule.startswith("{"):
        rule_line = "rule: switch\n          in_states: [Overstay]"
    else:
        rule_line = rule
    got = _run_scenario(tmp_path, rule_line)
    # the monitors of the robot town print nothing without world values
    assert [l for l in got if not l.startswith("Violation|||blocking")] == want


def test_unity_without_versions_still_refuses_a_switching_version(tmp_path):
    with pytest.raises(ValueError, match="cannot be moved to a new version"):
        generate(parse_file(TOWN2), tmp_path / "u", "unity-csharp")


# --- unity-csharp: versions with regime bindings (K.11, "Versions and regimes") ------

def _town_versions(rng: random.Random, regimes: bool) -> list:
    """Two or three versions of the robot town (ROAD_USE: a random release
    deadline, eviction threshold and transitional rule in each); with
    `regimes`, each has the regimes DISTRIBUTED and CENTRALIZED, one of
    which (or none) does not have ROAD_USE in force."""
    import yaml
    from cadl.parser import parse
    from cadl.type_checker import type_check
    base = yaml.safe_load(TOWN2.read_text(encoding="utf-8"))
    count = rng.choice([2, 3])
    rules = [None] + [rng.choice(["switch", "keep", ["Overstay"], ["Holding"]])
                      for _ in range(count - 1)]
    out_regimes = [rng.choice(["CENTRALIZED", "CENTRALIZED", "DISTRIBUTED", None])
                   for _ in range(count)]
    soses = []
    for v in range(count):
        doc = json.loads(json.dumps(base))
        sos = doc["sos"]
        sos["version"] = str(v + 1)
        road = next(c for c in sos["contracts"] if c["id"] == "ROAD_USE")
        dl = rng.choice([30, 60, 90])
        k = rng.choice([2, 3])
        road["lifecycle"]["transitions"][0]["deadline"] = f"{dl}s"
        road["monitors"][0]["rule"] = f"ROBOT.blocking >= {k} AND state IN [Holding, Overstay]"
        am = sos["amendment"]
        for p in am["parameters"]:
            if p["id"] == "hold_limit":
                p["value"] = f"{dl}s"
            if p["id"] == "block_k":
                p["value"] = k
        if v == 0:
            am.pop("previous", None)
            am["effective"] = {"period": 1}
            am["transitional"] = {"default": "keep"}
        else:
            am["previous"] = str(v)
            am["effective"] = {"period": v + 1}
            r = rules[v]
            entry = {"contract": "ROAD_USE", "rule": "keep" if r == "keep" else "switch"}
            if isinstance(r, list):
                entry["in_states"] = r
            am["transitional"] = {"default": "keep", "contracts": [entry]}
        if regimes:
            sos["regimes"] = ({out_regimes[v]: {"contracts": {"suspended": ["ROAD_USE"]}}}
                              if out_regimes[v] else {"DISTRIBUTED": {}})
        s = parse(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        assert type_check(s).ok
        soses.append(s)
    return soses, {str(v + 1): ({out_regimes[v]} if out_regimes[v] else set())
                   for v in range(count)}


UNITY_REGIME_RUNS = int(os.environ.get("CADL_VERSION_UNITY_REGIME_RUNS", "6"))


@needs_mono
def test_unity_versions_with_regimes_against_the_clock_run(tmp_path):
    """The generated code with versions whose regime bindings take ROAD_USE
    out of force (K.11, "Versions and regimes"), against the generated code
    with the same versions and no regimes on the projection of the run: the
    steps in force on the clock of the contract, the instance started at the
    first time in force, and the versions that took effect while not in force
    applied when it is in force again (tests/unity_regimes_support.py)."""
    from tests import unity_regimes_support as U
    from cadl.regimes import initial_regime
    rng = random.Random(3639)
    total = {"compared": 0, "left": 0, "runs": 0}
    for r in range(UNITY_REGIME_RUNS):
        state = rng.getstate()
        soses, out_of = _town_versions(rng, True)
        rng.setstate(state)
        plain, _ = _town_versions(rng, False)
        initial = initial_regime(soses[0])
        names = [s.version for s in soses]
        runs = [U.random_version_run(rng, ["DISTRIBUTED", "CENTRALIZED"], names)
                for _ in range(40)]
        bad, compared, left = U.compare_versions(soses, plain, "ROAD_USE", runs, out_of,
                                                 initial, tmp_path / f"u{r}")
        assert bad == [], bad[0]
        total["compared"] += compared
        total["left"] += left
        total["runs"] += len(runs)
    assert total["left"] > total["runs"] * 3


@needs_mono
def test_unity_versions_with_regimes_refuses_a_regime_of_another_version(tmp_path):
    """A run that has had a version without regimes: in force cannot be in a
    regime, and a version that does not have the current regime cannot be
    in force (K.11)."""
    rng = random.Random(1)
    soses, _ = _town_versions(rng, True)
    rng = random.Random(1)
    plain, _ = _town_versions(rng, False)
    soses[0] = plain[0]                  # version 1 has no regimes:
    out = tmp_path / "gen"
    generate(soses[0], out, "unity-csharp", versions=soses[1:])
    (out / "M.cs").write_text("""
using System; using CADL.SosDsl;
class M { static void Main() {
  var rt = new ContractRuntime();
  var rg = new CadlRegimes(rt);
  var c = new RoadUseContract("r1", rt, 0, null, rg);
  rt.Register(c);
  try { c.SetVersion("2", 10, rt); Console.WriteLine("accepted"); }
  catch (ArgumentException e) { Console.WriteLine("refused: " + e.Message); }
  try { rg.Set("CENTRALIZED", 20); Console.WriteLine("accepted"); }
  catch (ArgumentException e) { Console.WriteLine("refused: " + e.Message); }
  Console.WriteLine(c.Version + " " + c.InForce);
}}
""", encoding="utf-8")
    sources = [str(p) for p in sorted(out.rglob("*.cs"))]
    subprocess.run(["mcs", "-nologo", "-langversion:experimental", f"-out:{out / 'm.exe'}",
                    *sources], check=True, capture_output=True, text=True)
    got = subprocess.run(["mono", str(out / "m.exe")], check=True, capture_output=True,
                         text=True).stdout.splitlines()
    assert got[0].startswith("refused: the run is in regime (none")
    assert got[1].startswith("refused: the run is in regime CENTRALIZED, which is not a regime of version 1")
    assert got[2] == "1 True"
