"""The norms monitor that `cadl codegen -t python` generates (Appendix F,
F.5): `norms_monitor.py` of the generated package.

The monitor contains the engine of `cadl norms-eval` and runs without
cadl. The tests here run it in a separate Python process in which cadl
cannot be imported, and compare its result with `cadl norms-eval` on the
example traces, on the cases of tests/test_norms_eval.py, and on random
traces."""

from __future__ import annotations

import ast
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from cadl.codegen import generate
from cadl.codegen import norms_monitor_gen
from cadl.codegen.norms_monitor_gen import (
    NormsMonitorError, assemble_module, assemble_runtime, build_model, check_module,
    generate_norms_monitor_module, has_norms_monitor, runtime_source,
)
from cadl.codegen.safety import UnsafeCodegenInput
from cadl.norms_eval import NormsEvalError, TraceError, evaluate, parse_trace
from cadl.parser import parse, parse_file
from cadl.type_checker import type_check

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
TRACES = EXAMPLES / "traces"
EXAMPLE = EXAMPLES / "norms_robot_delivery.cadl"
V1 = EXAMPLES / "norms_amendment_v1.cadl"
V2 = EXAMPLES / "norms_amendment_v2.cadl"
RICH = Path(__file__).parent / "data" / "norms_monitor_rich.cadl"

# Runs in a process started with -I -S: no site-packages, no PYTHONPATH, no
# current directory, so cadl (and lark, yaml, z3) cannot be imported.
_NO_CADL = """
import sys
sys.path.insert(0, sys.argv[1])
for name in ("cadl", "lark", "yaml"):
    try:
        __import__(name)
    except ImportError:
        continue
    raise SystemExit(name + " is importable in the monitor process")
"""

_RUN_MODULE = _NO_CADL + """
import runpy
sys.argv = sys.argv[2:]
runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
"""

# Replays the cases of a JSON file. For each case: `result`, the result of
# replay() or `error`; with "steps", also `manual` (observe() line by
# line, then end(), with the quantities named) and `prefixes` (result()
# after every observation of a monitor that is told nothing in advance)
# and `changes` (what observe() and end() returned).
_BATCH = _NO_CADL + """
import importlib, json
with open(sys.argv[2], encoding="utf-8") as f:
    cases = json.load(f)
out = []
for case in cases:
    mod = importlib.import_module(case["package"] + ".norms_monitor")
    versions = case.get("versions") or None
    r = {}
    try:
        r["result"] = mod.replay(case["lines"], case.get("contract"), versions)
    except (mod.TraceError, mod.NormsEvalError, mod.MonitorError) as e:
        r["error"] = [type(e).__name__, str(e)]
    if case.get("steps") and "result" in r:
        recs = [json.loads(l) for l in case["lines"] if l.strip()]
        # the names of the quantities and the amendments of the instance
        # (what follows its end is not part of it)
        probe = mod.NormsMonitor(case.get("contract"), versions=versions)
        inside = []
        for rec in recs:
            probe.observe(rec)
            inside.append(rec)
            if probe.ended:
                break
        names = set()
        for rec in inside:
            names.update("".join(k.split()) for k in rec.get("values", {}))
        m = mod.NormsMonitor(case.get("contract"), quantities=names, versions=versions)
        m._warn_state = not any(rec.get("amend") for rec in inside)
        changes = [m.observe(rec) for rec in recs]
        if not m.ended and recs[-1].get("end") is not False:
            changes.append(m.end())
        r["manual"] = m.result()
        r["changes"] = changes
        m = mod.NormsMonitor(case.get("contract"), versions=versions)
        r["prefixes"] = []
        for rec in recs:
            m.observe(rec)
            r["prefixes"].append(m.result())
    out.append(r)
with open(sys.argv[3], "w", encoding="utf-8") as f:
    json.dump(out, f)
"""


def _isolated(script: str, *args, python: str = sys.executable) -> subprocess.CompletedProcess:
    return subprocess.run([python, "-I", "-S", "-c", script, *map(str, args)],
                          capture_output=True, text=True)


def _run_module(pkg_dir: Path, module: str, *args) -> subprocess.CompletedProcess:
    return _isolated(_RUN_MODULE, pkg_dir, module, *args)


def _write_package(root: Path, name: str, source: str) -> None:
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "norms_monitor.py").write_text(source, encoding="utf-8")


def _batch(tmp_path: Path, cases: list) -> list:
    """Replay `cases` (dicts with source, lines, contract, versions, steps)
    in one process without cadl; one result per case."""
    root = tmp_path / "monitors"
    packages: dict[str, str] = {}
    jobs = []
    for case in cases:
        source = case["source"]
        if source not in packages:
            packages[source] = f"m{len(packages)}"
            _write_package(root, packages[source], source)
        jobs.append({"package": packages[source], "lines": case["lines"],
                     "contract": case.get("contract"), "versions": case.get("versions"),
                     "steps": case.get("steps", False)})
    (tmp_path / "cases.json").write_text(json.dumps(jobs), encoding="utf-8")
    proc = _isolated(_BATCH, root, tmp_path / "cases.json", tmp_path / "out.json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))


def _norms_eval(sos, lines, contract=None, versions=None) -> dict:
    """`cadl norms-eval` on trace lines: {"result": ...} or {"error": ...}."""
    try:
        result = evaluate(sos, parse_trace(lines), contract, versions=versions)
    except (TraceError, NormsEvalError) as e:
        return {"error": [type(e).__name__, str(e)]}
    return {"result": json.loads(json.dumps(result))}


def _same_error(expected: list, got: list) -> bool:
    if expected[0] == "NormsEvalError" and got[0] == "ObservationError":
        got = ["NormsEvalError", got[1]]     # a NormsEvalError that names the cause
    if expected == got:
        return True
    # A version the test did not give: norms-eval looks for a file of that
    # name, the monitor for a model of that name.
    return (expected[0] == got[0] == "NormsEvalError"
            and "File not found" in expected[1] and "has no version" in got[1])


# === Structure of the generated module =======================================

class TestGeneratedModule:
    def test_only_for_a_file_that_declares_norms(self, tmp_path):
        generate(parse_file(EXAMPLES / "robot_delivery.cadl"), tmp_path / "plain", target="python")
        assert not (tmp_path / "plain" / "norms_monitor.py").exists()
        assert "NormsMonitor" not in (tmp_path / "plain" / "__init__.py").read_text()
        generate(parse_file(EXAMPLE), tmp_path / "norms", target="python")
        assert (tmp_path / "norms" / "norms_monitor.py").exists()
        for target in ("solidity", "opa", "unity-csharp"):
            generate(parse_file(EXAMPLE), tmp_path / target, target=target)
            assert not list((tmp_path / target).rglob("norms_monitor*"))

    def test_imports_only_the_standard_library(self):
        tree = ast.parse(generate_norms_monitor_module(parse_file(EXAMPLE)))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "relative import"
                modules.add(node.module.split(".")[0])
        assert modules <= {"__future__", "argparse", "copy", "dataclasses", "json", "re",
                           "math", "sys", "typing"}, modules

    def test_is_python_3_9_syntax(self):
        source = generate_norms_monitor_module(parse_file(EXAMPLE))
        assert source.splitlines()[0].startswith('"""')
        ast.parse(source, feature_version=(3, 9))
        tree = ast.parse(source)
        first = next(n for n in tree.body if not isinstance(n, ast.Expr))
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__"

    def test_engine_is_the_text_of_norms_eval(self):
        """The evaluation code is not a second implementation: every line
        between the monitor-runtime marks of norms_eval.py is in the
        generated module, in order."""
        text = (ROOT / "src" / "cadl" / "norms_eval.py").read_text(encoding="utf-8")
        runtime = runtime_source()
        inside, regions, current = False, [], []
        for line in text.splitlines():
            if line.strip() == "# >>> monitor-runtime":
                inside, current = True, []
            elif line.strip() == "# <<< monitor-runtime":
                inside = False
                regions.append("\n".join(current).strip("\n"))
            elif inside:
                current.append(line)
        assert len(regions) == 2 and not inside
        pos = 0
        for region in regions:
            assert len(region.splitlines()) > 50
            pos = runtime.index(region, pos) + len(region)
        for name in ("class _Engine", "class _Evaluator", "class _TraceReader",
                     "def select_contract", "def format_text"):
            assert name in regions[0] + regions[1]
        # what stays outside needs the parser of cadl
        assert "def load_version" not in runtime and "from .parser" not in runtime

    def test_model_is_plain_data_and_is_what_the_module_holds(self, tmp_path):
        sos = parse_file(EXAMPLE)
        model = build_model(sos)
        assert json.loads(json.dumps(model)) == model
        _write_package(tmp_path, "pkg", generate_norms_monitor_module(sos))
        proc = _run_module(tmp_path, "pkg.norms_monitor", "--dump-model")
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == model

    def test_output_is_identical_across_hash_seeds(self, tmp_path):
        script = ("import sys; from pathlib import Path; from cadl.parser import parse_file; "
                  "from cadl.codegen.norms_monitor_gen import generate_norms_monitor_module as g; "
                  "sys.stdout.write(g(parse_file(Path(sys.argv[1]))))")
        texts = set()
        for seed in ("0", "1", "2"):
            proc = subprocess.run([sys.executable, "-c", script, str(RICH)], capture_output=True,
                                  text=True, env=dict(os.environ, PYTHONHASHSEED=seed), check=True)
            texts.add(proc.stdout)
        assert len(texts) == 1

    def test_package_imports_without_cadl(self, tmp_path):
        generate(parse_file(EXAMPLE), tmp_path / "pkg", target="python")
        script = _NO_CADL + """
import pkg
from pkg import NormsMonitor
m = NormsMonitor(bindings={"i": 2})
assert m.contract == "DELIVERY_SLA" and not m.started
try:
    pkg.RobotDeliveryNormsRuntime
except ImportError as e:
    assert "cadl" in str(e)
else:
    raise SystemExit("the runtime does not need cadl?")
try:
    pkg.no_such_name
except AttributeError:
    pass
print("ok")
"""
        proc = _isolated(script, tmp_path)
        assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stdout + proc.stderr

    def test_package_with_cadl_exports_everything(self, tmp_path):
        generate(parse_file(EXAMPLE), tmp_path / "pkg", target="python")
        script = ("import sys; sys.path.insert(0, sys.argv[1]); import pkg; "
                  "from pkg import *; "
                  "print(NormsMonitor.__name__, RobotDeliveryNormsRuntime.__name__, "
                  "DeliverySlaMonitor.__name__)")
        proc = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["NormsMonitor", "RobotDeliveryNormsRuntime",
                                       "DeliverySlaMonitor"]

    def test_contracts_module_points_to_the_monitor(self, tmp_path):
        generate(parse_file(EXAMPLE), tmp_path, target="python")
        text = (tmp_path / "contracts.py").read_text(encoding="utf-8")
        assert text.count("# (norms_monitor.py monitors it)") == 2

    @pytest.mark.parametrize("target", ["solidity", "opa"])
    def test_cli_warning_of_other_targets_names_the_python_target(self, target, tmp_path):
        proc = subprocess.run(
            [sys.executable, "-m", "cadl.cli", "codegen", str(EXAMPLE), "-t", target,
             "-o", str(tmp_path / "out")], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        warnings = [l for l in proc.stderr.splitlines() if "temporal operator" in l]
        assert len(warnings) == 2
        assert all("is not generated (only the python target generates a monitor for it)" in w
                   for w in warnings)

    def test_cli_python_target_reports_the_monitor(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, "-m", "cadl.cli", "codegen", str(EXAMPLE), "-t", "python",
             "-o", str(tmp_path / "out")], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "Norms monitor: norms_monitor.py (5 norm(s), 1 counts-as rule(s))" in proc.stdout
        warnings = [l for l in proc.stderr.splitlines() if "temporal operator" in l]
        assert len(warnings) == 2
        assert all("norms_monitor.py monitors it" in w and "not generated" not in w
                   for w in warnings)


# === The examples, through the command line ====================================

def _codegen(cadl_file: Path, out: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "cadl.cli", "codegen", str(cadl_file), "-t", "python",
         "-o", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _cli_norms_eval(cadl_file: Path, trace: Path, fmt: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cadl.cli", "norms-eval", str(cadl_file), "--trace", str(trace),
         "--format", fmt], capture_output=True, text=True)


EXAMPLE_TRACES = sorted(TRACES.glob("norms_robot_delivery_*.jsonl"))


@pytest.fixture(scope="module")
def packages(tmp_path_factory):
    root = tmp_path_factory.mktemp("generated")
    for f in (EXAMPLE, V1, V2):
        _codegen(f, root / f.stem)
    return root


class TestExamples:
    def test_there_are_example_traces(self):
        assert len(EXAMPLE_TRACES) >= 5

    @pytest.mark.parametrize("trace", EXAMPLE_TRACES, ids=lambda p: p.stem)
    def test_same_json_as_norms_eval(self, packages, trace):
        expected = _cli_norms_eval(EXAMPLE, trace, "json")
        got = _run_module(packages, "norms_robot_delivery.norms_monitor", trace)
        assert got.stderr == ""
        assert got.returncode == expected.returncode
        want = json.loads(expected.stdout)
        assert want.pop("file") == str(EXAMPLE)
        assert json.loads(got.stdout) == want
        # byte for byte, apart from the last key ("file")
        head = expected.stdout[:expected.stdout.rindex(',\n  "file"')]
        assert got.stdout.startswith(head)

    @pytest.mark.parametrize("trace", EXAMPLE_TRACES, ids=lambda p: p.stem)
    def test_same_text_as_norms_eval(self, packages, trace):
        expected = _cli_norms_eval(EXAMPLE, trace, "text")
        got = _run_module(packages, "norms_robot_delivery.norms_monitor", trace,
                          "--format", "text")
        assert (got.returncode, got.stdout) == (expected.returncode, expected.stdout)

    def test_amendment_trace(self, packages, tmp_path):
        trace = TRACES / "norms_amendment_v1_to_v2.jsonl"
        expected = _cli_norms_eval(V1, trace, "json")
        dump = _run_module(packages, "norms_amendment_v2.norms_monitor", "--dump-model")
        assert dump.returncode == 0, dump.stderr
        (tmp_path / "v2.json").write_text(dump.stdout, encoding="utf-8")
        got = _run_module(packages, "norms_amendment_v1.norms_monitor", trace,
                          "--model-version", f"../norms_amendment_v2.cadl={tmp_path / 'v2.json'}")
        want = json.loads(expected.stdout)
        del want["file"]
        assert got.returncode == expected.returncode, got.stderr
        assert json.loads(got.stdout) == want
        assert want["amendments"] and want["amendments"][0]["effects"]

    def test_amendment_without_the_new_version_is_refused(self, packages):
        got = _run_module(packages, "norms_amendment_v1.norms_monitor",
                          TRACES / "norms_amendment_v1_to_v2.jsonl")
        assert got.returncode == 2
        assert "has no version '../norms_amendment_v2.cadl'" in got.stderr

    def test_usage_errors(self, packages, tmp_path):
        mod = "norms_robot_delivery.norms_monitor"
        got = _run_module(packages, mod, tmp_path / "missing.jsonl")
        assert got.returncode == 2 and "File not found" in got.stderr
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"t": 1}\n{"t": 0}\n', encoding="utf-8")
        got = _run_module(packages, mod, bad)
        expected = _cli_norms_eval(EXAMPLE, bad, "json")
        assert got.returncode == expected.returncode == 2
        assert got.stderr == expected.stderr
        got = _run_module(packages, mod, EXAMPLE_TRACES[0], "--contract", "NOPE")
        assert got.returncode == 2 and "no contract 'NOPE'" in got.stderr

    @pytest.mark.parametrize("cadl_file", sorted(EXAMPLES.glob("*.cadl")), ids=lambda p: p.stem)
    def test_every_example_generates_and_loads(self, cadl_file, tmp_path):
        """Every example still generates; where it declares norms 0.1 the
        monitor loads without cadl and can be made for every contract."""
        sos = parse_file(cadl_file)
        generate(sos, tmp_path / "pkg", target="python")
        monitor = tmp_path / "pkg" / "norms_monitor.py"
        assert monitor.exists() == has_norms_monitor(sos)
        if not monitor.exists():
            return
        script = _NO_CADL + """
import json
from pkg.norms_monitor import NormsMonitor, MODEL
for c in MODEL["contracts"]:
    m = NormsMonitor(c["id"])
    m.observe({"t": 0})
    m.end()
    print(json.dumps([c["id"], m.result()["instance"]["ended"]]))
"""
        proc = _isolated(script, tmp_path)
        assert proc.returncode == 0, proc.stderr
        seen = [json.loads(l) for l in proc.stdout.splitlines()]
        assert seen == [[c.id, True] for c in sos.contracts]


# === The cases of tests/test_norms_eval.py ======================================

def test_cases_of_test_norms_eval(tmp_path):
    """Every `evaluate` call of tests/test_norms_eval.py, replayed through
    the monitor generated from the same file (and the models of the same
    new versions): the same result, or the same error."""
    recorded = tmp_path / "recorded.json"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_norms_eval.py", "-q", "-x",
         "-p", "tests.norms_monitor_cases", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, CADL_NORMS_CASES=str(recorded)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(recorded.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 120
    assert sum(1 for c in cases if c["versions"]) >= 15
    assert all("does not declare 'norms: 0.1'" in s for s in data["skipped"])
    got = _batch(tmp_path, cases)
    mismatches = []
    for i, (case, g) in enumerate(zip(cases, got)):
        if "error" in case:
            if "error" not in g or not _same_error(case["error"], g["error"]):
                mismatches.append((i, case["error"], g.get("error", "a result")))
        elif g.get("result") != case["result"]:
            mismatches.append((i, case["lines"], g.get("error", "another result")))
    assert not mismatches, mismatches[:3]


# === Random traces ================================================================

_TIMES = [0, 0, 0.25, 0.5, 1, 1, 1.5, 2, 2, 2.5, 3, 4, 4.999, 5, 5.001, 6, 8, 13, 100, 295, 300.5]

_ROBOT = {
    "bindings": {"i": 2},
    "messages": [
        ("DISPATCHER", "ROBOT[2]", "route_assignment", None),
        ("DISPATCHER", "ROBOT[2]", "replan_command", None),
        ("DISPATCHER", "ROBOT[3]", "route_assignment", None),
        ("ROBOT[2]", "DISPATCHER", "ack", ["accepted"]),
        ("ROBOT[2]", "DISPATCHER", "ack", ["rejected"]),
        ("ROBOT[2]", "DISPATCHER", "ack", None),
        ("ROBOT[3]", "DISPATCHER", "ack", ["accepted"]),
        ("ROBOT[2]", "DISPATCHER", "failure_report", None),
        ("ROBOT[2]", "DISPATCHER", "help_request", None),
        ("ROBOT[2]", "DISPATCHER", "charging_request", None),
        ("ROBOT[2]", "DISPATCHER", "swap_request", None),
        ("ROBOT[2]", "ROBOT[2]", "replan_command", None),
        ("INTRUDER", "ROBOT[2]", "route_assignment", None),      # not declared
        ("DISPATCHER", "ROBOT", "replan_command", None),         # the set, not a member
        ("ROBOT[0]", "DISPATCHER", "failure_report", None),
    ],
    "values": {
        "DISPATCHER.is_operational": [True, True, True, False, None],
        "ROBOT[2].battery": [80, 40, 14, 12, 9, 5, None],
        "ROBOT[2].status": ["Idle", "InTransit", "InTransit", "Delivered", None],
        "ROBOT[3].status": ["InTransit", "Delivered"],
    },
    "states": ["Proposed", "Assigned", "Delivering", "Delivering", "Completed", "Violated"],
}

_RICH = {
    "bindings": {"i": 1},
    "request": [{"limit": 5}, {"limit": 5}, {"limit": 2}, {}],
    "messages": [
        ("D", "R[1]", "order", None), ("D", "R[1]", "pause", None), ("D", "R[1]", "go", None),
        ("D", "R[1]", "hush", None), ("D", "R[1]", "arm", None), ("D", "R[1]", "abort", None),
        ("R[1]", "D", "done", None), ("R[1]", "D", "sorry", [1]), ("R[1]", "D", "sorry", [2]),
        ("R[1]", "D", "sorry", None), ("R[1]", "D", "noise", None), ("R[1]", "D", "rest", None),
        ("R[1]", "D", "stop", None), ("R[2]", "D", "stop", None), ("R[2]", "D", "done", None),
        ("R[1]", "R[1]", "abort", None), ("R[1]", "D", "pause", None), ("D", "R[2]", "pause", None),
        # actor ranges: R is R[1..3]; `pinged` is created by R[2..3] only
        ("R[1]", "D", "ping", None), ("R[2]", "D", "ping", None), ("R[3]", "D", "ping", None),
        ("R[7]", "D", "ping", None), ("R[7]", "D", "stop", None), ("Z", "D", "stop", None),
        ("R", "D", "stop", None), ("D", "R[2]", "pong", None), ("D", "R[7]", "pong", None),
        ("D", "R", "pong", None), ("R[7]", "D", "done", None),
    ],
    "values": {
        "D.up": [True, True, True, False, None],
        "D.override": [True, False, None],
        "R[1].load": [0, 3, 6, 9, 10, 12, None],
        "R[1].temp": [20, 60, 85, 95, None],
        "R[1].mode": ["Idle", "Busy", "Parked", "Broken", None],
        "R[1].done": [True, False, False, None],
        "R[1].ok": [True, False, False, None],
        "R[2].ok": [True],
        "demand": [0, 2, 4, 6, None],
    },
    "states": ["Open", "Busy", "Busy", "Closed"],
}


def _random_trace(rng: random.Random, vocab: dict, amend: str | None = None) -> list:
    """One trace: random messages in random order, time steps around the
    deadlines, values that appear late or become null, state changes, and
    sometimes an early end or a prefix."""
    n = rng.randint(1, 14)
    amend_at = rng.randrange(n) if amend and rng.random() < 0.6 else None
    late = {k for k in vocab["values"] if rng.random() < 0.3}      # no value at the start
    t = float(rng.choice([0, 0, 0, 1.5, 10]))
    lines = []
    for k in range(n):
        rec: dict = {"t": t}
        if k == 0:
            if rng.random() < 0.9:
                rec["bindings"] = vocab["bindings"]
            if "request" in vocab:
                request = rng.choice(vocab["request"])
                if request:
                    rec["request"] = request
        values = {}
        for name, choices in vocab["values"].items():
            p = 0.7 if k == 0 and name not in late else 0.25
            if rng.random() < p:
                values[name] = rng.choice(choices)
        if values:
            rec["values"] = values
        msgs = []
        while rng.random() < 0.45:
            a, b, name, args = rng.choice(vocab["messages"])
            m = {"from": a, "to": b, "name": name}
            if args is not None:
                m["args"] = args
            msgs.append(m)
        if msgs:
            rec["messages"] = msgs
        if rng.random() < 0.15:
            rec["state"] = rng.choice(vocab["states"])
        if k == amend_at:
            rec["amend"] = amend
        if rng.random() < 0.04:
            rec["end"] = True
        elif k == n - 1 and rng.random() < 0.25:
            rec["end"] = False
        lines.append(json.dumps(rec))
        t = round(t + rng.choice(_TIMES), 3)
    return lines


def _prefix(lines: list, k: int) -> list:
    """The first k + 1 lines as a trace that is a prefix."""
    out = list(lines[:k + 1])
    rec = json.loads(out[-1])
    rec.setdefault("end", False)
    out[-1] = json.dumps(rec)
    return out


def _strip(result: dict) -> dict:
    return {k: v for k, v in result.items() if k != "warnings"}


def _memoize_parser(monkeypatch) -> None:
    """norms-eval parses the predicates of the contract for every trace;
    here it does so some thousand times for the same few texts. Keep what
    the parser returned and hand out copies."""
    import copy
    import functools
    import cadl.norms_eval as ne
    for name in ("_parse_rule", "_parse_expr"):
        cached = functools.lru_cache(maxsize=None)(getattr(ne, name))
        monkeypatch.setattr(ne, name, lambda text, cached=cached: copy.deepcopy(cached(text)))


RANDOM_SETS = [
    # name, file, vocabulary, new version, number of traces, seed
    ("robot_delivery", EXAMPLE, _ROBOT, None, 400, 20261111),
    ("amendment", V1, _ROBOT, V2, 400, 20261112),
    ("rich", RICH, _RICH, None, 700, 20261113),
]


@pytest.mark.parametrize("name,cadl_file,vocab,new,count,seed", RANDOM_SETS,
                         ids=[s[0] for s in RANDOM_SETS])
def test_random_traces(name, cadl_file, vocab, new, count, seed, tmp_path, monkeypatch):
    """Random traces: the monitor gives the result of `cadl norms-eval`
    (a) when the trace is replayed, (b) when it is fed line by line, and
    (c) after every observation, the result of norms-eval on the prefix up
    to it (warnings aside: norms-eval reports a name without value once
    for the whole trace, the monitor when it first meets it)."""
    sos = parse_file(cadl_file)
    assert type_check(sos).ok
    source = generate_norms_monitor_module(sos)
    _memoize_parser(monkeypatch)
    versions = {"v2": parse_file(new)} if new else None
    models = {"v2": build_model(versions["v2"])} if new else None
    rng = random.Random(seed)
    cases = [{"source": source, "versions": models, "steps": True,
              "lines": _random_trace(rng, vocab, "v2" if new else None)}
             for _ in range(count)]
    got = _batch(tmp_path, cases)

    seen = {"verdicts": set(), "states": set(), "errors": 0, "prefix": 0, "ended_early": 0,
            "amended": 0, "unauthorised": 0, "undecided_at_end": 0}
    for case, g in zip(cases, got):
        lines = case["lines"]
        expected = _norms_eval(sos, lines, versions=versions)
        if "error" in expected:
            assert "error" in g and _same_error(expected["error"], g["error"]), lines
            seen["errors"] += 1
            continue
        assert g.get("result") == expected["result"], lines
        assert g["manual"] == expected["result"], lines
        r = expected["result"]
        # what observe() and end() returned is the history of the result
        changes = [c for step in g["changes"] for c in step]
        for norm in r["norms"]:
            mine = [c for c in changes if c["kind"] == "norm" and c["id"] == norm["id"]]
            # one change per observation: the state it left the norm in
            # (a norm can be activated and judged at one observation)
            history = [(h["t"], h["state"]) for h in norm["history"]]
            rest = iter(history)
            for c in mine:
                if c["state"] != c["previous_state"] and not r["amendments"]:
                    assert (c["t"], c["state"]) in rest, lines
            assert bool(mine) == bool(history) or r["amendments"], lines
            if mine:
                assert (mine[-1]["state"], mine[-1]["verdict"]) == (norm["state"], norm["verdict"])
            seen["verdicts"].add(norm["verdict"])
            seen["states"].add(norm["state"])
        for key, kind, field in (("guarantees", "guarantee", "verdict"),
                                 ("assumptions", "assumption", "status")):
            for item in r[key]:
                mine = [c for c in changes if c["kind"] == kind and c["text"] == item["text"]]
                assert len(mine) == (0 if item[field] == "pending" else 1), lines
                if mine:
                    assert mine[0][field] == item[field] and mine[0]["t"] == item["time"]
                seen["verdicts"].add(item[field])
        # after each observation: norms-eval on the prefix
        nonblank = [l for l in lines if l.strip()]
        assert len(g["prefixes"]) == len(nonblank)
        for k, at_k in enumerate(g["prefixes"]):
            want = _norms_eval(sos, _prefix(nonblank, k), versions=versions)["result"]
            assert _strip(at_k) == _strip(want), (k, lines)
        seen["prefix"] += not r["instance"]["ended"]
        seen["ended_early"] += r["instance"]["observations"] < len(nonblank)
        seen["amended"] += bool(r["amendments"])
        seen["unauthorised"] += bool(r["unauthorised"])
        seen["undecided_at_end"] += any(
            n["state"] == "active" and r["instance"]["ended"] for n in r["norms"])

    # the random traces reach what they are meant to exercise
    assert {"pending", "fulfilled", "violated", "excused", "holds", "failed"} <= seen["verdicts"]
    assert {"inactive", "active", "fulfilled", "violated", "discharged"} <= seen["states"]
    assert seen["prefix"] and seen["ended_early"]
    if new:
        assert seen["amended"] >= count // 4
    if name == "rich":
        assert "suspended" in seen["states"] and seen["undecided_at_end"]
        assert seen["unauthorised"]


# === The API =======================================================================

@pytest.fixture(scope="module")
def robot(tmp_path_factory):
    """The monitor module of the example, loaded in this process."""
    return _load(tmp_path_factory.mktemp("api"), "robot_api", parse_file(EXAMPLE))


def _load(tmp: Path, name: str, sos, check: bool = True):
    import importlib.util
    if check:
        assert type_check(sos).ok
    path = tmp / f"{name}.py"
    path.write_text(generate_norms_monitor_module(sos), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ASSIGN = {"from": "DISPATCHER", "to": "ROBOT[2]", "name": "route_assignment"}
ACK = {"from": "ROBOT[2]", "to": "DISPATCHER", "name": "ack", "args": ["accepted"]}


class TestApi:
    def test_changes_of_each_observation(self, robot):
        m = robot.NormsMonitor("DELIVERY_SLA", bindings={"i": 2})
        assert not m.started and not m.ended
        first = m.observe({"t": 0.0, "values": {"DISPATCHER.is_operational": True}})
        assert m.started
        assert [(c["id"], c["previous_state"], c["state"]) for c in first] == [
            ("no_low_battery_driving", "inactive", "active"),
            ("may_return_to_charge", "inactive", "active"),
            ("reassign", "inactive", "active")]
        (c,) = m.observe({"t": 1.0, "messages": [ASSIGN], "state": "Assigned"})
        assert (c["kind"], c["id"], c["state"], c["verdict"], c["t"]) == (
            "norm", "acknowledge", "active", "pending", 1.0)
        assert m.observe({"t": 2.0}) == []
        late = m.observe({"t": 6.5})
        assert [(c["id"], c["state"], c["verdict"]) for c in late] == [
            ("acknowledge", "violated", "violated"), ("report_failure", "active", "pending")]
        assert late[0]["previous_verdict"] == "pending"
        assert late[0]["reason"] == "deadline of 5s passed at t=6 without the content"
        v = m.verdicts()
        assert v["norms"]["acknowledge"] == {"state": "violated", "verdict": "violated"}
        assert v["guarantees"] == [{"index": 1, "verdict": "pending",
                                    "text": "within(300s, ROBOT[i].status == Delivered)"}]
        assert v["assumptions"][0]["status"] == "pending" and v["ok"] is False

    def test_temporal_items_are_decided_when_the_interval_is_over(self, robot):
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0, "values": {"DISPATCHER.is_operational": True,
                                      "ROBOT[2].status": "Idle"}})
        assert m.observe({"t": 300}) == []          # the interval is not over yet
        decided = m.observe({"t": 300.5})
        assert [(c["kind"], c.get("status"), c.get("verdict")) for c in decided] == [
            ("assumption", "holds", None), ("guarantee", None, "violated")]
        assert decided[1]["previous"] == "pending" and decided[1]["index"] == 1

    def test_excused_after_a_failed_assumption(self, robot):
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0, "values": {"DISPATCHER.is_operational": True}})
        m.observe({"t": 1, "messages": [ASSIGN]})
        (failed,) = m.observe({"t": 2, "values": {"DISPATCHER.is_operational": False}})
        assert (failed["kind"], failed["status"]) == ("assumption", "failed")
        changes = m.observe({"t": 7})
        assert (changes[0]["id"], changes[0]["verdict"]) == ("acknowledge", "excused")
        assert m.verdicts()["ok"] is True
        assert m.result()["violations"][0]["verdict"] == "excused"

    def test_end_settles_at_the_last_observation(self, robot):
        m = robot.NormsMonitor(bindings={"i": 2})
        with pytest.raises(robot.MonitorError, match="no observation"):
            m.end()
        with pytest.raises(robot.MonitorError, match="no observation"):
            m.result()
        m.observe({"t": 0})
        m.observe({"t": 4, "messages": [ASSIGN]})
        m.observe({"t": 5, "messages": [ACK]})
        before = m.result()
        assert before["instance"]["ended"] is False and before["instance"]["end"] is None
        changes = m.end()
        assert m.ended and {c["t"] for c in changes} == {5.0}
        assert {c["id"]: c["state"] for c in changes} == {
            "report_failure": "discharged", "no_low_battery_driving": "fulfilled",
            "may_return_to_charge": "discharged", "reassign": "discharged"}
        assert m.end() == []
        result = m.result()
        assert result["instance"]["end"] == 5.0
        assert result["instance"]["end_reason"] == "last observation of the trace"
        # observations after the end are ignored and counted
        assert m.observe({"t": 6, "messages": [ASSIGN]}) == []
        assert m.observe({"t": 7}) == []
        after = m.result()
        assert after["warnings"][-1] == ("2 observation(s) after the end of the contract "
                                         "instance (t=5) are ignored")
        del after["warnings"], result["warnings"]
        assert after == result

    def test_end_in_an_observation_and_terminal_state(self, robot):
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0})
        m.observe({"t": 3, "end": True})
        assert m.ended and m.result()["instance"]["end_reason"] == "end of the instance in the trace"
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0})
        m.observe({"t": 3, "state": "Completed"})
        assert m.ended and m.result()["instance"]["end_reason"] == "terminal state Completed"

    def test_malformed_observation_changes_nothing(self, robot):
        m = robot.NormsMonitor(bindings={"i": 2})
        with pytest.raises(robot.TraceError, match="observation 1: 't' must be a number"):
            m.observe({"values": {"ROBOT[2].battery": 5}})
        assert not m.started
        m.observe({"t": 1, "values": {"ROBOT[2].battery": 50, "ROBOT[2].status": "InTransit"}})
        snapshot = m.result()
        for bad, message in [
            ({"t": 0.5}, "observation 2: time 0.5 is before"),
            ({"t": 2, "bindings": {"i": 3}}, "'bindings' is allowed only on the first line"),
            ({"t": 2, "values": {"ROBOT[2].battery": 5}, "messages": [{"from": "X"}]},
             "a message needs a string 'to'"),
            ({"t": 2, "values": {"ROBOT[2].battery": 5}, "state": "Nowhere"},
             "state 'Nowhere' at t=2 is not a state of the lifecycle"),
            ({"t": 2, "x": 1}, "unknown key"),
            ("not a dict", "must be a JSON object"),
        ]:
            with pytest.raises(robot.TraceError, match=message):
                m.observe(bad)
            assert m.result() == snapshot
        # the battery of the refused observations did not reach the snapshot
        assert m.observe({"t": 2}) == []
        (c,) = m.observe({"t": 3, "values": {"ROBOT[2].battery": 5}})
        assert (c["id"], c["state"]) == ("no_low_battery_driving", "violated")

    def test_bindings_and_request(self, robot):
        m = robot.NormsMonitor(bindings={"i": 3})
        m.observe({"t": 0, "bindings": {"i": 2}})        # the observation wins
        assert m.result()["instance"]["bindings"] == {"i": 2}
        m = robot.NormsMonitor(bindings={"i": 3})
        m.observe({"t": 0})
        m.observe({"t": 1, "messages": [ASSIGN]})        # to ROBOT[2], not ROBOT[3]
        assert m.verdicts()["norms"]["acknowledge"]["state"] == "inactive"

    def test_next_deadline(self, robot):
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0})
        assert m.next_deadline() is None
        m.observe({"t": 1, "messages": [ASSIGN]})
        assert m.next_deadline() == 6.0
        assert m.observe({"t": 6.0}) == []               # at the deadline: still in time
        assert m.next_deadline() == 6.0
        m.observe({"t": 6.25})                           # the first observation after it
        assert m.verdicts()["norms"]["acknowledge"]["state"] == "violated"
        assert m.next_deadline() == 8.25                 # the reparation, 2 s
        m.end()
        assert m.next_deadline() is None

    def test_result_is_a_copy(self, robot):
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0})
        r = m.result()
        r["norms"][0]["history"].append("x")
        r["timeline"].clear()
        assert m.result() != r and m.result()["timeline"]

    def test_unknown_contract(self, robot):
        with pytest.raises(robot.NormsEvalError, match="no contract 'X'"):
            robot.NormsMonitor("X")

    def test_amend_needs_the_model_of_the_new_version(self, tmp_path):
        v1 = _load(tmp_path, "amend_v1", parse_file(V1))
        m = v1.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0})
        with pytest.raises(v1.ObservationError, match="has no version 'v2'"):
            m.observe({"t": 1, "amend": "v2"})
        assert m.observe({"t": 2}) == []            # refused, and the monitor goes on
        m = v1.NormsMonitor(bindings={"i": 2}, versions={"v2": build_model(parse_file(V2))})
        m.observe({"t": 0})
        m.observe({"t": 1, "messages": [ASSIGN]})
        changes = m.observe({"t": 5, "amend": "v2"})     # 4 s elapsed, new deadline 3 s
        assert ("acknowledge", "violated") in [(c["id"], c["state"]) for c in changes]
        assert "request_help" in m.verdicts()["norms"]

    def test_quantities_name_bare_names_that_get_a_value_later(self, tmp_path):
        """A name without an actor and without a value is read as an
        enum-like literal when it is compared with a string. A monitor
        cannot know that a value will come; `quantities` tells it."""
        sos = parse("""
sos:
  name: Q
  extensions:
    - norms: 0.1
  actors:
    - {id: A, role: a}
  contracts:
    - id: C
      parties: [A]
      norms:
        - id: n
          kind: prohibition
          bearer: A
          content: "A.mode != target"
""")
        q = _load(tmp_path, "quantities", sos)
        trace = [{"t": 0, "values": {"A.mode": "fast"}}, {"t": 1, "values": {"target": "fast"}},
                 {"t": 2, "end": True}]
        told = q.NormsMonitor(quantities=["target"])
        untold = q.NormsMonitor()
        for rec in trace:
            told.observe(rec)
            untold.observe(rec)
        # untold: at t=0 `target` is the literal "target", so the content holds
        assert untold.verdicts()["norms"]["n"]["state"] == "violated"
        assert told.verdicts()["norms"]["n"]["state"] == "fulfilled"
        lines = [json.dumps(r) for r in trace]
        assert told.result() == q.replay(lines) == _norms_eval(sos, lines)["result"]

    def test_bare_name_is_a_quantity_from_its_first_value(self, tmp_path):
        sos = parse("""
sos:
  name: Q
  extensions:
    - norms: 0.1
  actors:
    - {id: A, role: a}
  contracts:
    - id: C
      parties: [A]
      norms:
        - id: n
          kind: obligation
          bearer: A
          content: "level > 3"
""")
        mod = _load(tmp_path, "bare", sos)
        m = mod.NormsMonitor()
        m.observe({"t": 0})
        assert m.observe({"t": 1, "values": {"level": 2}}) == []
        (c,) = m.observe({"t": 2, "values": {"level": 5}})
        assert (c["id"], c["state"], c["reason"]) == ("n", "fulfilled", "content holds: level > 3")

    def test_state_without_lifecycle_is_reported_once(self, tmp_path):
        sos = parse("""
sos:
  name: Q
  extensions:
    - norms: 0.1
  actors:
    - {id: A, role: a}
  contracts:
    - id: C
      parties: [A]
      guarantee:
        - "A.ok == true"
        - "A.missing > 1"
""")
        mod = _load(tmp_path, "no_lifecycle", sos)
        lines = [json.dumps(r) for r in (
            {"t": 0, "values": {"A.ok": True}}, {"t": 1, "state": "S"}, {"t": 2, "state": "T"})]
        expected = _norms_eval(sos, lines)["result"]
        assert mod.replay(lines) == expected
        # norms-eval puts it first; the monitor meets the other one first
        assert len(expected["warnings"]) == 2
        assert expected["warnings"][0].startswith("the trace gives 'state'")
        assert "A.missing" in expected["warnings"][1]
        assert expected["guarantees"][0]["verdict"] == "fulfilled"


# === Strings of the file cannot become code =======================================

SENTINEL = "CADL_NORMS_MONITOR_SENTINEL"
_PAYLOADS = [
    '"""\nimport os; os.environ["%s"] = "1"\n"""' % SENTINEL,
    "'''\n__import__('os').environ['%s'] = '1'\n'''" % SENTINEL,
    '"; __import__("os").environ.__setitem__("%s", "1"); "' % SENTINEL,
    "'; __import__('os').environ.__setitem__('%s', '1'); '" % SENTINEL,
    "\\",
    "\\\\\"\\'",
    "x\\\n__import__('os').environ['%s'] = '1'" % SENTINEL,
    "\r\nMODEL = None\r\n",
    "})]\n\nraise SystemExit(3)\n#",
    "\x00\x1b  ퟿\U0001f600 {0} %s {{}} \\N{BULLET} \\x41 \\u0041",
]


def _hostile_sos(payload: str):
    """A file whose ids, events, predicates, message names and states carry
    `payload`. It does not pass `cadl check`; `evaluate` and the generator
    of the monitor accept it."""
    doc = {"sos": {
        "name": "Hostile",
        "extensions": [{"norms": 0.1}],
        "actors": [{"id": "A", "role": payload}, {"id": "B", "role": "b"}],
        "contracts": [{
            "id": "C" + payload,
            "parties": ["A", "B"],
            "assume": [payload, "A.ok == true"],
            "guarantee": ["within(2s, A.done == true)", "A.name == \"" + payload + "\"",
                          "within(1s, " + payload + ")"],
            "lifecycle": {"states": ["S", payload], "initial": "S", "terminal": [payload]},
            "counts_as": [
                {"when": "A -> B : " + payload, "creates": payload},
                {"when": "A -> B : go", "creates": "going", "if": payload},
                {"when": payload, "creates": "ev"},
            ],
            "norms": [
                {"id": payload, "kind": "obligation", "bearer": "A",
                 "trigger": "A -> B : m" + payload, "content": "B -> A : " + payload,
                 "deadline": "5s",
                 "on_violation": {"severity": "Major", "sanction": payload, "reparation": "r"}},
                {"id": "r", "kind": "obligation", "bearer": payload, "content": payload,
                 "until": payload, "deadline": "1s"},
                {"id": "p", "kind": "prohibition", "bearer": "A",
                 "content": "A.name == \"" + payload + "\" OR A.x > 3"},
                {"id": "q", "kind": "obligation", "bearer": "A", "trigger": "A -> B : go",
                 "content": "B -> A : done", "deadline": "2s"},
            ],
        }],
    }}
    return parse(yaml.safe_dump(doc, allow_unicode=True))


def _strings(data):
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for k, v in data.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(data, list):
        for v in data:
            yield from _strings(v)


_HOSTILE_TRACE = [
    {"t": 0, "values": {"A.ok": True, "A.x": 1}},
    {"t": 1, "messages": [{"from": "A", "to": "B", "name": "go"}]},
    {"t": 2, "values": {"A.x": 5}},
    {"t": 4, "values": {"A.ok": False}},
    {"t": 5, "end": True},
]


class TestHostileStrings:
    @pytest.mark.parametrize("payload", _PAYLOADS, ids=range(len(_PAYLOADS)))
    def test_embedded_as_data(self, payload, tmp_path):
        """Without the input check: the strings are in MODEL as written,
        nothing of them is executed, and the monitor still agrees with
        norms-eval."""
        try:
            sos = _hostile_sos(payload)
        except Exception:
            pytest.skip("YAML cannot carry this string")
        source = generate_norms_monitor_module(sos)
        ast.parse(source)
        model = build_model(sos)
        assert model["contracts"][0]["id"] == "C" + payload
        lines = [json.dumps(r) for r in _HOSTILE_TRACE]
        expected = _norms_eval(sos, lines)
        assert "result" in expected, expected
        states = {n["id"]: n["state"] for n in expected["result"]["norms"]}
        # q does not use the payload; p does, and is judged where the
        # payload leaves its content a predicate
        assert states["q"] == "violated" and states["p"] in ("violated", "inactive")

        _write_package(tmp_path, "hostile", source)
        script = _NO_CADL + """
import json, os
from hostile import norms_monitor as nm
assert %r not in os.environ
result = nm.replay(json.loads(sys.argv[2]))
assert %r not in os.environ
print(json.dumps({"model": nm.MODEL, "result": result}))
""" % (SENTINEL, SENTINEL)
        proc = _isolated(script, tmp_path, json.dumps(lines))
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["model"] == model
        assert out["result"] == expected["result"]
        # the command line too
        trace = tmp_path / "t.jsonl"
        trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc = _run_module(tmp_path, "hostile.norms_monitor", trace)
        assert proc.returncode == (0 if expected["result"]["ok"] else 1), proc.stderr
        assert json.loads(proc.stdout) == dict(expected["result"], trace=str(trace))

    @pytest.mark.parametrize("payload", _PAYLOADS[:5], ids=range(5))
    def test_rejected_by_the_input_check(self, payload, tmp_path):
        """`cadl codegen` itself does not get that far: the input check of
        the code generators refuses these strings, and nothing is written."""
        try:
            sos = _hostile_sos(payload)
        except Exception:
            pytest.skip("YAML cannot carry this string")
        with pytest.raises(UnsafeCodegenInput):
            generate(sos, tmp_path / "out", target="python")
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("where", ["norm_id", "content", "trigger", "creates", "sanction",
                                       "message"])
    def test_each_norms_field_is_checked(self, where, tmp_path):
        bad = 'x" + __import__("os").system("true") + "'
        norm = {"id": "n", "kind": "obligation", "bearer": "A", "content": "A.ok == true",
                "trigger": "A -> B : go", "on_violation": {"sanction": "s"}}
        rule = {"when": "A -> B : go", "creates": "going"}
        if where == "norm_id":
            norm["id"] = bad
        elif where == "content":
            norm["content"] = "A.name == \"" + bad + "\""
        elif where == "trigger":
            norm["trigger"] = bad
        elif where == "creates":
            rule["creates"] = bad
        elif where == "sanction":
            norm["on_violation"]["sanction"] = bad
        else:
            rule["when"] = "A -> B : " + bad
        doc = {"sos": {"name": "H", "extensions": [{"norms": 0.1}],
                       "actors": [{"id": "A", "role": "a"}, {"id": "B", "role": "b"}],
                       "contracts": [{"id": "C", "parties": ["A", "B"], "norms": [norm],
                                      "counts_as": [rule]}]}}
        sos = parse(yaml.safe_dump(doc))
        with pytest.raises(UnsafeCodegenInput):
            generate(sos, tmp_path / "out", target="python")
        assert not (tmp_path / "out").exists()
        # and when it is embedded, it is data
        source = generate_norms_monitor_module(sos)
        assert any(bad in text for text in _strings(build_model(sos)))
        _write_package(tmp_path, "h", source)
        proc = _isolated(_NO_CADL + "from h.norms_monitor import NormsMonitor, MODEL\n"
                         "m = NormsMonitor(); m.observe({'t': 0}); m.end(); print('ok')",
                         tmp_path)
        assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


# === Found by an independent review ================================================
# Each test here failed before its fix.

def _small(blocks: str, actors: str = "    - {id: A, role: a}\n", parties: str = "[A]"):
    return parse(
        "sos:\n  name: Q\n  extensions:\n    - sos-dsl: 0.1\n    - norms: 0.1\n"
        "  actors:\n" + actors +
        "  contracts:\n    - id: C\n      parties: " + parties + "\n" + blocks)


Q_BLOCKS = """\
      guarantee:
        - "within(2s, throughout(5s, A.ok == true))"
        - "A.mode == target"
      norms:
        - id: n
          kind: prohibition
          bearer: A
          content: "A.mode != target"
"""


def _lines(*records) -> list:
    return [json.dumps(r) for r in records]


class TestModelAsJson:
    def test_model_is_one_json_string(self):
        source = generate_norms_monitor_module(parse_file(RICH))
        (assign,) = [n for n in ast.parse(source).body if isinstance(n, ast.Assign)
                     and n.targets[0].id == "MODEL"]
        call = assign.value
        assert ast.unparse(call.func) == "json.loads" and len(call.args) == 1
        assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        assert json.loads(call.args[0].value) == build_model(parse_file(RICH))
        assert call.args[0].value.isascii()
        assert assign.lineno == assign.end_lineno

    @pytest.mark.parametrize("predicate", [
        " AND ".join(f"A.x > {i}" for i in range(250)),
        "(" * 120 + "A.x > 0" + ")" * 120,
        "A.x < " + "9" * 400,
    ], ids=["and250", "paren120", "int400"])
    def test_large_predicates(self, predicate, tmp_path):
        """A deep or long predicate was written as a nested Python literal
        that Python could not compile, or only slowly."""
        sos = _small(f"""\
      guarantee:
        - "{predicate}"
      norms:
        - id: n
          kind: obligation
          bearer: A
          content: "{predicate}"
""")
        lines = _lines({"t": 0, "values": {"A.x": 5}}, {"t": 1, "values": {"A.x": 1000}})
        expected = _norms_eval(sos, lines)
        (got,) = _batch(tmp_path, [{"source": generate_norms_monitor_module(sos),
                                    "lines": lines}])
        assert got == expected

    def test_number_too_large_is_not_a_predicate(self, tmp_path):
        """`1e999` was read as infinity and written as `inf`, a NameError
        when the module was imported."""
        from cadl.parser import parse_expr
        with pytest.raises(Exception, match="too large"):
            parse_expr("A.x < 1e999")
        with pytest.raises(Exception, match="too large"):
            parse_expr("A.x > -1e999")
        assert parse_expr("A.x < 1e308").right.value == 1e308
        sos = _small("""\
      norms:
        - id: n
          kind: obligation
          bearer: A
          content: "A.x < 1e999"
""")
        lines = _lines({"t": 0, "values": {"A.x": 5}})
        expected = _norms_eval(sos, lines)
        assert expected["result"]["norms"][0]["evaluable"] is False
        (got,) = _batch(tmp_path, [{"source": generate_norms_monitor_module(sos),
                                    "lines": lines}])
        assert got == expected

    @pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_number_in_the_model_is_a_generation_error(self, value, tmp_path):
        from cadl.ast_nodes import BinaryOp, FloatLiteral
        sos = _small("      guarantee:\n        - \"A.x < 1.5\"\n")
        (item,) = sos.contracts[0].guarantee
        assert isinstance(item, BinaryOp)
        item.right = FloatLiteral(value=value)
        with pytest.raises(NormsMonitorError, match="not finite .*guarantee"):
            generate_norms_monitor_module(sos)
        with pytest.raises(NormsMonitorError):
            generate(sos, tmp_path / "out", target="python")
        assert not (tmp_path / "out").exists()


def _mutated(name: str, old: str, new: str, count: int = 1) -> dict:
    sources = dict(norms_monitor_gen._sources())
    assert sources[name].count(old) == count, (name, old)
    sources[name] = sources[name].replace(old, new)
    return sources


_HELPERS = '''
def _rv_open(elapsed, deadline):
    return deadline is None or elapsed <= deadline + 1e-9


def _rv_describe(op, left, right):
    return f"'{op}' cannot be applied to {left!r} and {right!r}; the predicate does not hold"


# >>> monitor-runtime
_EPS = 1e-9
'''


class TestSelfCheck:
    """The generator refuses to write a module that would not work. The
    mutations are applied to copies of the source texts of cadl."""

    def _refused(self, sources: dict, match: str) -> None:
        with pytest.raises(NormsMonitorError, match=match):
            assemble_module(build_model(parse_file(EXAMPLE)), assemble_runtime(sources))

    def test_unchanged_sources_pass(self):
        text = assemble_module(build_model(parse_file(EXAMPLE)),
                               assemble_runtime(norms_monitor_gen._sources()))
        assert text == generate_norms_monitor_module(parse_file(EXAMPLE))
        check_module(text)

    def test_m1_helper_outside_the_copied_region(self):
        sources = _mutated("norms_eval", "# >>> monitor-runtime\n_EPS = 1e-9\n", _HELPERS.lstrip("\n"))
        old = "open_ = norm.deadline is None or elapsed <= norm.deadline + _EPS"
        assert sources["norms_eval"].count(old) == 1
        sources["norms_eval"] = sources["norms_eval"].replace(
            old, "open_ = _rv_open(elapsed, norm.deadline)")
        self._refused(sources, "does not define: _rv_open")

    def test_m2_helper_used_on_a_rare_path(self):
        """No test of the monitor reaches this warning, so only the check
        of the names finds it."""
        sources = _mutated("norms_eval", "# >>> monitor-runtime\n_EPS = 1e-9\n", _HELPERS.lstrip("\n"))
        old = ("self.warn(f\"'{op}' cannot be applied to {left!r} and {right!r}; "
               "the predicate does not hold\")")
        assert sources["norms_eval"].count(old) == 1
        sources["norms_eval"] = sources["norms_eval"].replace(
            old, "self.warn(_rv_describe(op, left, right))")
        self._refused(sources, "does not define: _rv_describe")

    def test_m3_evaluate_changes_the_result(self):
        """`evaluate` is not copied: it may not do anything to the result."""
        sources = _mutated(
            "norms_eval",
            "    return run_trace(contract, trace, loader, declared_actors(sos))\n",
            "    result = run_trace(contract, trace, loader, declared_actors(sos))\n"
            "    for v in result['violations']:\n"
            "        if v['severity'] == 'Minor':\n"
            "            v['verdict'] = 'excused'\n"
            "    return result\n")
        with pytest.raises(NormsMonitorError, match="`evaluate` .* must end with"):
            assemble_runtime(sources)

    def test_m4_copied_code_uses_a_module_that_is_not_imported(self):
        sources = _mutated("norms_eval", "        sos = self.loader(name)\n",
                           "        name = Path(name).name\n        sos = self.loader(name)\n")
        self._refused(sources, "does not define: Path")

    def test_b3_helper_of_a_copied_class_is_copied(self, tmp_path):
        """A default_factory or a decorator defined in ast_nodes.py."""
        sources = _mutated(
            "ast_nodes",
            "@dataclass\nclass PowerEffects:",
            "def _no_names() -> list:\n    return []\n\n\n"
            "def _marked(cls):\n    cls.marked = _MARK\n    return cls\n\n\n"
            "_MARK = \"x\"\n\n\n"
            "@_marked\n@dataclass\nclass PowerEffects:")
        old = "    activate: list[str] = field(default_factory=list)"
        assert sources["ast_nodes"].count(old) == 1
        sources["ast_nodes"] = sources["ast_nodes"].replace(
            old, "    activate: list[str] = field(default_factory=_no_names)")
        runtime = assemble_runtime(sources)
        for helper in ("def _no_names()", "def _marked(cls)", "_MARK = \"x\"",
                       "@_marked\n@dataclass"):
            assert helper in runtime
        text = assemble_module(build_model(parse_file(EXAMPLE)), runtime)
        _write_package(tmp_path, "b3", text)
        proc = _isolated(_NO_CADL + "from b3 import norms_monitor as nm\n"
                         "assert nm.PowerEffects.marked == 'x'\n"
                         "assert nm.PowerEffects().activate == []\n"
                         "assert nm.NormsMonitor(bindings={'i': 2}).observe({'t': 0})\n", tmp_path)
        assert proc.returncode == 0, proc.stderr

    def test_b3_helper_that_cannot_be_copied_is_refused(self):
        """A name that ast_nodes.py imports is not in the generated module."""
        sources = _mutated(
            "ast_nodes", "@dataclass\nclass PowerEffects:",
            "import collections\n\n\n@dataclass\nclass PowerEffects:")
        old = "    activate: list[str] = field(default_factory=list)"
        sources["ast_nodes"] = sources["ast_nodes"].replace(
            old, "    activate: list[str] = field(default_factory=collections.deque)")
        self._refused(sources, "does not define: collections")

    def test_text_that_does_not_compile_is_refused(self):
        with pytest.raises(NormsMonitorError, match="does not compile"):
            check_module("def f(:\n    pass\n")
        with pytest.raises(NormsMonitorError, match="does not define: nowhere"):
            check_module("import json\n\ndef f(x):\n    return [nowhere(y) for y in x]\n")
        check_module("from __future__ import annotations\nimport json\n\n"
                     "def f(x: Missing) -> AlsoMissing:\n    return json.dumps(len(x))\n")

    def test_generate_writes_nothing_when_the_check_fails(self, tmp_path, monkeypatch):
        sources = _mutated("norms_eval", "        sos = self.loader(name)\n",
                           "        name = Path(name).name\n        sos = self.loader(name)\n")
        monkeypatch.setattr(norms_monitor_gen, "runtime_source",
                            lambda: assemble_runtime(sources))
        with pytest.raises(NormsMonitorError):
            generate(parse_file(EXAMPLE), tmp_path / "out", target="python")
        assert not (tmp_path / "out").exists()


@pytest.fixture(scope="module")
def amend_v1(tmp_path_factory):
    return _load(tmp_path_factory.mktemp("atomic"), "atomic_v1", parse_file(V1))


class TestAtomicObserve:
    """A refused observation leaves the monitor as it was, and the monitor
    goes on."""

    GOOD = [
        {"t": 0.0, "bindings": {"i": 2},
         "values": {"DISPATCHER.is_operational": True, "ROBOT[2].battery": 80}},
        {"t": 1.0, "messages": [ASSIGN], "state": "Assigned"},
        {"t": 6.5, "values": {"ROBOT[2].battery": 79}},
        {"t": 7.5, "amend": "v2"},
        {"t": 9.0, "messages": [{"from": "ROBOT[2]", "to": "DISPATCHER", "name": "help_request"}]},
        {"t": 30.0, "values": {"ROBOT[2].status": "InTransit", "ROBOT[2].battery": 14},
         "state": "Delivering"},
        {"t": 110.0, "values": {"ROBOT[2].status": "Delivered"}, "state": "Completed"},
    ]

    @staticmethod
    def _bad(t: float) -> list:
        return [
            ("TraceError", {"t": "x"}),
            ("TraceError", {"t": t - 100, "values": {"ROBOT[2].battery": 1}}),
            ("TraceError", {"t": 10 ** 400, "values": {"ROBOT[2].battery": 1}}),
            ("TraceError", {"t": float("nan"), "values": {"ROBOT[2].battery": 1}}),
            ("TraceError", {"t": float("inf"), "messages": [ASSIGN]}),
            ("TraceError", {"t": True}),
            ("TraceError", {"t": t, "values": {"ROBOT[2].battery": 1}, "messages": "no"}),
            ("TraceError", {"t": t, "values": {"ROBOT[2].battery": 1}, "state": "Nowhere"}),
            ("TraceError", {"t": t, "messages": [{"from": "ROBOT [2]", "to": "DISPATCHER",
                                                  "name": "ack"}]}),
            ("ObservationError", {"t": t, "amend": "nope", "values": {"ROBOT[2].battery": 1}}),
            ("ObservationError", {"t": t, "amend": "other", "messages": [ASSIGN]}),
            ("TraceError", {"t": t, "amend": "v2", "state": "Nowhere",
                            "values": {"ROBOT[2].battery": 1}}),
            ("ObservationError", {"t": t, "amend": "few_states", "messages": [ASSIGN]}),
        ]

    def _versions(self):
        v2 = build_model(parse_file(V2))
        other = json.loads(json.dumps(v2))
        other["contracts"][0]["id"] = "OTHER"
        few = json.loads(json.dumps(v2))
        few["contracts"][0]["lifecycle"]["states"] = ["Nowhere"]
        return {"v2": v2, "other": other, "few_states": few}

    def test_continuing_gives_the_result_without_the_refused_lines(self, amend_v1):
        versions = self._versions()
        clean = amend_v1.NormsMonitor(versions=versions)
        tried = amend_v1.NormsMonitor(versions=versions)
        refused = 0
        for rec in self.GOOD:
            for kind, bad in self._bad(rec["t"]):
                if not tried.started and bad.get("t") == rec["t"] - 100:
                    continue                 # no earlier observation yet
                state = tried.result() if tried.started else None
                with pytest.raises(getattr(amend_v1, kind)):
                    tried.observe(bad)
                refused += 1
                assert (tried.result() if tried.started else None) == state, bad
            assert tried.observe(rec) == clean.observe(rec)
            assert tried.result() == clean.result()
            assert tried.next_check() == clean.next_check()
        assert tried.ended and refused >= 80
        assert clean.result()["amendments"][0]["file"] == "v2"

    def test_refused_first_observation(self, amend_v1):
        m = amend_v1.NormsMonitor(bindings={"i": 2}, versions=self._versions())
        for bad in ({"t": "x", "bindings": {"i": 1}}, {"t": 0, "bindings": {"i": 1}, "state": "Q"},
                    {"t": 0, "bindings": {"i": 1}, "amend": "nope"},
                    {"t": 0, "bindings": {"i": 1}, "amend": "v2", "state": "Q"}):
            with pytest.raises((amend_v1.TraceError, amend_v1.ObservationError)):
                m.observe(bad)
            assert not m.started and m._trace.bindings == {"i": 2}
        m.observe({"t": 0})
        m.observe({"t": 1, "messages": [ASSIGN]})
        assert m.verdicts()["norms"]["acknowledge"]["state"] == "active"

    def test_errors_are_documented_classes(self, amend_v1):
        assert issubclass(amend_v1.ObservationError, amend_v1.MonitorError)
        assert issubclass(amend_v1.ObservationError, amend_v1.NormsEvalError)
        assert issubclass(amend_v1.TraceError, ValueError)
        assert not hasattr(amend_v1.NormsMonitor(), "_broken")

    def test_broken_version_model_is_refused_at_once(self, amend_v1):
        for broken in ({"norms_version": "0.1"}, {}, None, "v2",
                       {"norms_version": "0.1", "parsed": {}, "actors": [],
                        "contracts": [{"id": "X"}]}):
            with pytest.raises(amend_v1.MonitorError, match=r"versions\['v2'\] is not the MODEL"):
                amend_v1.NormsMonitor(versions={"v2": broken})

    def test_reader_is_not_corrupted_by_a_huge_time(self, robot):
        """`math.isfinite(10 ** 400)` raises OverflowError; the time must
        be refused before anything is kept."""
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0})
        m.observe({"t": 1, "messages": [ASSIGN]})
        before = m.result()
        for t in (10 ** 400, -10 ** 400, 1e11, float("nan"), float("inf"), True, None, "1"):
            with pytest.raises(robot.TraceError, match="'t' must be"):
                m.observe({"t": t, "values": {"ROBOT[2].battery": 1}})
            assert m._reader.prev_t == 1 and "ROBOT[2].battery" not in m._reader.snapshot
        assert m.result() == before
        assert m.observe({"t": 2}) == []

    @pytest.mark.parametrize("line", ['{"t": 1' + "0" * 400 + "}", '{"t": 1e400}', '{"t": NaN}',
                                      '{"t": Infinity}', '{"t": -1e11}', '{"t": true}'])
    def test_offline_reader_refuses_the_same_times(self, line, robot):
        with pytest.raises(TraceError):
            parse_trace(['{"t": 0}', line])
        with pytest.raises(robot.TraceError):
            robot.replay(['{"t": 0}', line])


class TestAfterTheEnd:
    AFTEREND = _lines({"t": 0, "values": {"A.mode": "fast", "A.ok": True}},
                      {"t": 2, "end": True}, {"t": 3, "values": {"target": "fast"}})
    NOAFTER = AFTEREND[:2]

    def test_observations_after_the_end_have_no_effect(self, tmp_path):
        """norms-eval took the names of the quantities also from what
        follows the end, so `target` was undefined instead of a symbolic
        value and the prohibition was not judged."""
        sos = _small(Q_BLOCKS)
        with_ = _norms_eval(sos, self.AFTEREND)["result"]
        without = _norms_eval(sos, self.NOAFTER)["result"]
        assert with_["warnings"][-1] == ("1 observation(s) after the end of the contract "
                                         "instance (t=2) are ignored")
        assert with_["warnings"][:-1] == without["warnings"]
        assert _strip(with_) == _strip(without)
        assert {n["id"]: n["state"] for n in with_["norms"]} == {"n": "violated"}
        got = _batch(tmp_path, [
            {"source": generate_norms_monitor_module(sos), "lines": lines, "steps": True}
            for lines in (self.AFTEREND, self.NOAFTER)])
        assert got[0]["result"] == got[0]["manual"] == with_
        assert got[1]["result"] == got[1]["manual"] == without

    def test_state_after_the_end_gives_no_warning(self):
        sos = _small("      guarantee:\n        - \"A.ok == true\"\n")
        lines = _lines({"t": 0, "values": {"A.ok": True}}, {"t": 1, "end": True},
                       {"t": 2, "state": "S"})
        warnings = _norms_eval(sos, lines)["result"]["warnings"]
        assert len(warnings) == 1 and "after the end" in warnings[0]

    def test_monitor_keeps_nothing_of_them(self, tmp_path):
        q = _load(tmp_path, "after_end", _small(Q_BLOCKS), check=False)
        m = q.NormsMonitor()
        m.observe({"t": 0, "values": {"A.mode": "fast", "A.ok": True}})
        m.observe({"t": 2, "end": True})
        before = m.result()
        assert m.observe({"t": 3, "values": {"target": "fast"}, "state": "S"}) == []
        assert len(m._trace.observations) == 2 and "target" not in m._reader.snapshot
        assert "target" not in m._engine.ev.known
        with pytest.raises(q.TraceError, match="observation 4: time 1 is before"):
            m.observe({"t": 1})                # still checked for its form
        after = m.result()
        assert after.pop("warnings")[-1].startswith("1 observation(s) after the end")
        before.pop("warnings")
        assert after == before


class TestWhatAnOnlineMonitorKnows:
    def test_quantities_are_the_one_permitted_difference(self, tmp_path):
        """F.5: the verdicts after an observation are those of the prefix
        when the names of the quantities are fixed for the instance. A
        monitor that is not given the name `target` reads it as a symbolic
        value until its first value: the prohibition `A.mode != target` is
        violated at t=0, where norms-eval on the whole trace, which knows
        that `target` is a quantity, finds it undefined."""
        sos = _small(Q_BLOCKS)
        q = _load(tmp_path, "quantity", sos, check=False)
        full = [{"t": 0, "values": {"A.mode": "fast", "A.ok": True}},
                {"t": 1, "values": {"target": "fast"}}, {"t": 2, "end": True}]
        offline = _norms_eval(sos, _lines(*full))["result"]
        prefix = _norms_eval(sos, _lines(dict(full[0], end=False)))["result"]
        assert [n["state"] for n in offline["norms"]] == ["fulfilled"]
        assert [n["state"] for n in prefix["norms"]] == ["violated"]   # a different instance
        told, untold = q.NormsMonitor(quantities=["target"]), q.NormsMonitor()
        told.observe(full[0])
        untold.observe(full[0])
        assert untold.verdicts()["norms"]["n"]["state"] == "violated"
        assert _strip(untold.result()) == _strip(prefix)
        assert told.verdicts()["norms"]["n"]["state"] == "active"
        assert told.verdicts()["guarantees"][1]["verdict"] == "pending"
        for rec in full[1:]:
            told.observe(rec)
            untold.observe(rec)
        assert told.result() == offline == q.replay(_lines(*full))
        assert untold.verdicts()["norms"]["n"]["state"] == "violated"   # it does not change

    def test_nested_operator_is_decided_later_than_its_own_interval(self, tmp_path):
        """within(2s, throughout(5s, p)) at t=0: the interval of within is
        over at t=3, but its operand at t=0 and t=1 is decided only after
        t=5 and t=6."""
        sos = _small(Q_BLOCKS)
        q = _load(tmp_path, "nested", sos, check=False)
        start = {"t": 0, "values": {"A.mode": "fast", "A.ok": True, "target": "fast"}}
        nest3 = [start, {"t": 1}, {"t": 3, "end": False}]
        nest5 = [start, {"t": 1}, {"t": 3}, {"t": 5.5, "end": False}]
        for records, verdict in ((nest3, "pending"), (nest5, "fulfilled")):
            offline = _norms_eval(sos, _lines(*records))["result"]
            assert offline["guarantees"][0]["verdict"] == verdict
            assert q.replay(_lines(*records)) == offline
        m = q.NormsMonitor()
        for rec in nest3:
            m.observe(rec)
        assert m.verdicts()["guarantees"][0]["verdict"] == "pending"
        (c,) = [c for c in m.observe({"t": 5.5}) if c["kind"] == "guarantee"]
        assert (c["index"], c["verdict"], c["t"]) == (1, "fulfilled", 5.5)

    def test_operator_can_be_undefined(self, tmp_path):
        """A.ok never has a value: the operator is neither true nor false
        after its interval, and the guarantee stays pending."""
        sos = _small("      guarantee:\n        - \"within(2s, A.ok == true)\"\n")
        q = _load(tmp_path, "undefined_op", sos, check=False)
        lines = _lines({"t": 0}, {"t": 1}, {"t": 3}, {"t": 4, "end": True})
        offline = _norms_eval(sos, lines)["result"]
        (g,) = offline["guarantees"]
        assert g["verdict"] == "pending" and "no value" in g["reason"]
        assert q.replay(lines) == offline

    def test_state_warning_does_not_depend_on_versions(self, tmp_path):
        sos = _small("      guarantee:\n        - \"A.ok == true\"\n")
        q = _load(tmp_path, "state_warning", sos, check=False)
        for versions in (None, {"v2": build_model(sos)}):
            m = q.NormsMonitor(versions=versions)
            m.observe({"t": 0})
            m.observe({"t": 1, "state": "S"})
            assert m.result()["warnings"][0].startswith("the trace gives 'state'")


class TestNextCheck:
    def test_observing_at_next_check_detects_the_deadline(self, robot):
        """Observing at next_deadline() does not: content at the deadline
        itself is in time."""
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": 0, "values": {"DISPATCHER.is_operational": True}})
        m.observe({"t": 1, "messages": [ASSIGN]})
        assert m.next_deadline() == 6.0
        assert m.observe({"t": m.next_deadline()}) == []
        check = m.next_check()
        assert 6.0 < check < 6.0 + 1e-8
        changes = m.observe({"t": check})
        assert ("acknowledge", "violated") in [(c["id"], c["state"]) for c in changes]

    @pytest.mark.parametrize("t0", [0.0, 12345.678, 1e9, 9.9e9])
    def test_loop_decides_everything_that_waits_for_time(self, t0, robot):
        """Also where the step of the floating-point numbers is larger
        than the tolerance of 1e-9 s."""
        m = robot.NormsMonitor(bindings={"i": 2})
        m.observe({"t": t0, "values": {"DISPATCHER.is_operational": True,
                                       "ROBOT[2].status": "Idle"}})
        m.observe({"t": t0 + 1, "messages": [ASSIGN]})
        seen = []
        for _ in range(10):
            check = m.next_check()
            if check is None:
                break
            assert check > m._trace.observations[-1].t
            seen.extend(m.observe({"t": check}))
        assert m.next_check() is None and m.next_deadline() is None
        done = {c.get("id") or c["kind"]: c.get("verdict") or c.get("status") for c in seen}
        assert done == {"acknowledge": "violated", "report_failure": "violated",
                        "assumption": "holds", "guarantee": "violated"}
        assert len(m._trace.observations) == 5      # one observation per thing decided
        m.end()
        assert m.next_check() is None

    def test_nested_and_temporal_contents(self, tmp_path):
        sos = _small("""\
      assume:
        - "throughout(1s, within(2s, A.ok == true))"
      guarantee:
        - "within(2s, throughout(5s, A.ok == true))"
        - "until(A.ok == true, within(3s, A.done == true), 4s) OR A.x > 9"
      norms:
        - id: n
          kind: obligation
          bearer: A
          content: "within(3s, A.done == true)"
          deadline: 2s
""")
        q = _load(tmp_path, "next_check", sos, check=False)
        m = q.NormsMonitor()
        m.observe({"t": 10, "values": {"A.ok": True, "A.done": False, "A.x": 1}})
        checks = []
        for _ in range(20):
            check = m.next_check()
            if check is None:
                break
            checks.append(check)
            m.observe({"t": check})
        v = m.verdicts()
        assert [a["status"] for a in v["assumptions"]] == ["holds"]
        assert [g["verdict"] for g in v["guarantees"]] == ["fulfilled", "violated"]
        assert v["norms"]["n"]["state"] == "violated"
        assert checks == sorted(checks) and len(checks) <= 6
        assert round(checks[-1], 6) == 17.0     # the guarantees: 10 + 2 + 5 and 10 + 4 + 3

    def test_before_the_first_observation(self, robot):
        m = robot.NormsMonitor(bindings={"i": 2})
        with pytest.raises(robot.MonitorError, match="no observation"):
            m.next_check()


class TestSmallThings:
    def test_bindings_and_request_are_copied(self, robot):
        bindings, request = {"i": 2}, {"deadline": [300]}
        m = robot.NormsMonitor(bindings=bindings, request=request)
        bindings["i"] = 3
        request["deadline"].append(1)
        assert (m._trace.bindings, m._trace.request) == ({"i": 2}, {"deadline": [300]})
        first = {"t": 0, "bindings": {"i": 2}, "request": {"a": {"b": 1}},
                 "values": {"ROBOT[2].battery": 50}}
        m.observe(first)
        first["bindings"]["i"] = 3
        first["request"]["a"]["b"] = 2
        first["values"]["ROBOT[2].battery"] = 1
        assert m.result()["instance"]["bindings"] == {"i": 2}
        assert m._trace.request == {"a": {"b": 1}}
        assert m._reader.snapshot == {"ROBOT[2].battery": 50}

    def test_several_contracts_name_the_argument(self, tmp_path):
        sos = parse("""
sos:
  name: Two
  extensions:
    - norms: 0.1
  actors:
    - {id: A, role: a}
  contracts:
    - id: C1
      parties: [A]
      norms:
        - {id: n, kind: permission, bearer: A, content: "A.ok == true"}
    - id: C2
      parties: [A]
      norms:
        - {id: n, kind: permission, bearer: A, content: "A.ok == true"}
""")
        two = _load(tmp_path, "two_contracts", sos)
        with pytest.raises(two.NormsEvalError, match=r"choose one with contract= \(C1, C2\)"):
            two.NormsMonitor()
        with pytest.raises(two.NormsEvalError, match=r"choose one with contract= \(C1, C2\)"):
            two.replay(['{"t": 0}'])
        assert two.NormsMonitor("C2").contract == "C2"
        _write_package(tmp_path, "two", generate_norms_monitor_module(sos))
        trace = tmp_path / "t.jsonl"
        trace.write_text('{"t": 0}\n', encoding="utf-8")
        proc = _run_module(tmp_path, "two.norms_monitor", trace)
        assert proc.returncode == 2 and "choose one with --contract (C1, C2)" in proc.stderr
        with pytest.raises(NormsEvalError, match="choose one with --contract"):
            evaluate(sos, parse_trace(['{"t": 0}']))

    def test_cli_flag_is_model_version(self, packages):
        proc = _run_module(packages, "norms_amendment_v1.norms_monitor", "--help")
        assert "--model-version NAME=MODEL.json" in proc.stdout
        assert "--version" not in proc.stdout.replace("--model-version", "")
        proc = _run_module(packages, "norms_amendment_v1.norms_monitor",
                           TRACES / "norms_amendment_v1_to_v2.jsonl", "--version", "a=b")
        assert proc.returncode == 2

    def test_first_observation_with_amend(self, tmp_path):
        v1 = _load(tmp_path, "first_amend", parse_file(V1))
        m = v1.NormsMonitor(bindings={"i": 2}, versions={"v2": build_model(parse_file(V2))})
        changes = {c["id"]: c for c in m.observe({"t": 0, "amend": "v2"}) if c["kind"] == "norm"}
        # new in v2: no previous state; gone in v2: no state
        assert changes["request_help"]["previous_state"] is None
        assert changes["may_swap_battery"]["previous_state"] is None
        assert changes["may_swap_battery"]["state"] == "active"
        assert changes["report_failure"]["state"] is None
        assert changes["report_failure"]["previous_state"] == "inactive"
        assert changes["report_failure"]["reason"] == "removed by the amendment"
        # active from the start of the instance, then discharged by its policy
        assert changes["may_return_to_charge"]["state"] == "discharged"
        offline = _norms_eval(parse_file(V1), _lines({"t": 0, "bindings": {"i": 2},
                                                      "amend": "v2", "end": False}),
                              versions={"v2": parse_file(V2)})["result"]
        assert _strip(m.result()) == _strip(offline)


class TestActorRanges:
    """Actor references are matched against the declared actors (F.3):
    the generated model carries them, also for a new version."""

    BLOCKS = """\
      counts_as:
        - {when: "R[2..3] -> D : ping", creates: pinged}
        - {when: "R[*] -> D : stop", creates: stopping}
      norms:
        - id: pong
          kind: obligation
          bearer: D
          trigger: pinged
          content: "D -> R[*] : pong"
          deadline: 3s
        - id: quiet
          kind: prohibition
          bearer: "R[i]"
          content: "R[i] -> D : noise"
          until: stopping
"""
    ACTORS = "    - {id: D, role: d}\n    - {id: \"R[1..3]\", role: r}\n"
    WIDER = "    - {id: D, role: d}\n    - {id: \"R[1..9]\", role: r}\n"

    @staticmethod
    def _msg(a, b, name):
        return {"from": a, "to": b, "name": name}

    def test_model_carries_the_declared_actors(self):
        model = build_model(_small(self.BLOCKS, self.ACTORS, "[D, \"R[*]\"]"))
        assert [a["name"] for a in model["actors"]] == ["D", "R"]
        assert model["actors"][1]["index"] == {"#": "RangeExpr", "start": 1, "end": 3}

    @pytest.mark.parametrize("bindings", [{"i": 1}, {}])
    def test_ranges_members_and_undeclared_actors(self, bindings, tmp_path):
        sos = _small(self.BLOCKS, self.ACTORS, "[D, \"R[*]\"]")
        m = self._msg
        records = [
            {"t": 0, "bindings": bindings},
            {"t": 1, "messages": [m("R[1]", "D", "ping"), m("R[7]", "D", "ping"),
                                  m("Z", "D", "ping"), m("R", "D", "ping")]},
            {"t": 2, "messages": [m("R[7]", "D", "noise"), m("R[7]", "D", "stop"),
                                  m("Z", "D", "stop"), m("R", "D", "stop")]},
            {"t": 3, "messages": [m("R[3]", "D", "ping")]},
            {"t": 4, "messages": [m("D", "R[7]", "pong"), m("D", "R", "pong")]},
            {"t": 5, "messages": [m("D", "R[2]", "pong")]},
            {"t": 6, "messages": [m("R[2]", "D", "stop")]},
            {"t": 7, "end": True},
        ]
        offline = _norms_eval(sos, _lines(*records))["result"]
        history = {n["id"]: [(h["t"], h["state"]) for h in n["history"]] for n in offline["norms"]}
        # R[1] is outside R[2..3]; R[7], Z and the set R are no members
        assert history["pong"] == [(3.0, "active"), (5.0, "fulfilled")]
        assert history["quiet"] == [(0.0, "active"), (6.0, "discharged")]
        assert [e["t"] for e in offline["institutional_events"]] == [3.0, 6.0]
        (got,) = _batch(tmp_path, [{"source": generate_norms_monitor_module(sos),
                                    "lines": _lines(*records), "steps": True}])
        assert got["result"] == got["manual"] == offline
        for k, at_k in enumerate(got["prefixes"]):
            want = _norms_eval(sos, _prefix(_lines(*records), k))["result"]
            assert _strip(at_k) == _strip(want)

    def test_amendment_brings_the_actors_of_the_new_version(self, tmp_path):
        """R[7] is no member in the old version and one in the new."""
        old = _small(self.BLOCKS, self.ACTORS, "[D, \"R[*]\"]")
        new = _small(self.BLOCKS, self.WIDER, "[D, \"R[*]\"]")
        m = self._msg
        records = [{"t": 0, "bindings": {"i": 1}},
                   {"t": 1, "messages": [m("R[7]", "D", "stop")]},
                   {"t": 2, "amend": "wider"},
                   {"t": 3, "messages": [m("R[7]", "D", "stop")]},
                   {"t": 4, "end": True}]
        offline = _norms_eval(old, _lines(*records), versions={"wider": new})["result"]
        assert [e["t"] for e in offline["institutional_events"]] == [3.0]
        (got,) = _batch(tmp_path, [{"source": generate_norms_monitor_module(old),
                                    "versions": {"wider": build_model(new)},
                                    "lines": _lines(*records), "steps": True}])
        assert got["result"] == got["manual"] == offline
