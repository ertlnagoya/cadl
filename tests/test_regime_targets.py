"""Targets that follow the current regime (Appendix A, A.7.1; Appendix F,
F.5, "Contracts not in force"), cadl 0.3.36.

- the norms monitors of the OPA and Solidity targets for a contract that
  some regime does not have in force read the key ``regime`` (Rego) or the
  regime the orchestrator sets (Solidity) and give, after every
  observation, what the norms monitor of the Python target gives
  (tests/norms_targets_support.reference_regimes), on random traces with
  regimes: `opa eval` and an EVM (CADL_EVM_PYTHON);
- the Unity C# target: a run with regimes gives the events of the same
  code without regimes on the projection of the run, on the clock of the
  contract (tests/unity_regimes_support.py; Mono);
- the boundary policies (`cadl agents-decide`, agent-rego, agent-cedar)
  read ``regime`` from the policy input; a contract that the regime does
  not have in force governs no message (tests/agent_policy_diff.py).

CADL_REGIME_TARGET_TRACES sets the number of random traces of each set
(default 150 for OPA, 6 for the EVM, 60 for Mono, 2 files for the
boundary policies).
"""

from __future__ import annotations

import json
import os
import random
import shutil
import textwrap
from pathlib import Path

import pytest

from cadl.codegen import generate
from cadl.codegen.norms_ir import unsupported_items
from cadl.codegen.opa.norms_monitor_rego import (
    generate_norms_monitor_rego, monitor_package, rego_ir,
)
from cadl.codegen.solidity.norms_monitor_sol import (
    generate_norms_monitor_sol, monitor_name, solidity_ir,
)
from cadl.norms_eval import evaluate, parse_trace_regimes, regime_bindings
from cadl.parser import parse, parse_file

from tests import norms_targets_support as S
from tests.test_norms_monitor import _RICH, _ROBOT, EXAMPLE, RICH, _random_trace
from tests.test_regime_runtime import REGIMES, _with_random_regimes, _with_regimes

ROOT = Path(__file__).parent.parent

needs_opa = pytest.mark.skipif(S.opa_binary() is None, reason="the opa binary is not on PATH")
REQUIRE_EVM = os.environ.get("CADL_REQUIRE_EVM") == "1"
needs_evm = pytest.mark.skipif(
    not REQUIRE_EVM and (S.solc_binary() is None or S.evm_python() is None),
    reason="no solc, or no EVM (CADL_EVM_PYTHON: a Python with eth-tester[py-evm])")
needs_mono = pytest.mark.skipif(not (shutil.which("mcs") and shutil.which("mono")),
                                reason="Mono (mcs, mono) is not installed")


def _count(default: int) -> int:
    return int(os.environ.get("CADL_REGIME_TARGET_TRACES", default))


SETS = {"robot": (EXAMPLE, "DELIVERY_SLA", _ROBOT), "rich": (RICH, "C", _RICH)}


def _sos(which: str):
    path, cid, vocab = SETS[which]
    sos = parse(_with_regimes(path, cid))
    return sos, next(c for c in sos.contracts if c.id == cid), vocab


def _traces(rng: random.Random, vocab, count: int, keep=lambda t: True) -> list:
    """Random traces with regimes; some name an unknown regime, end the
    instance or say that the trace goes on."""
    out = []
    while len(out) < count:
        lines = []
        for line in _with_random_regimes(rng, _random_trace(rng, vocab), ["NORMAL", "STORM"]):
            if not line.strip():
                continue
            rec = json.loads(line)
            x = rng.random()
            if x < .01:
                rec["regime"] = "FOG"
            elif x < .04:
                rec["end"] = True
            elif x < .06:
                rec["end"] = False
            lines.append(json.dumps(rec))
        lines = lines[:S.MAX_STEPS]
        if keep(lines):
            out.append(lines)
    return out


def _out_ends(sos, cid: str, traces: list) -> int:
    n = 0
    for t in traces:
        try:
            res = evaluate(sos, parse_trace_regimes(t, bindings=regime_bindings(sos)), cid)
        except Exception:
            continue
        n += "while the contract is not in force" in (res["instance"]["end_reason"] or "")
    return n


def _check(refs: list, got: list) -> list:
    """Mismatches; for a trace that never has the contract in force and does
    not end it, the monitor reports nothing and refuses nothing."""
    bad = []
    for i, (r, g) in enumerate(zip(refs, got)):
        if r.get("never") and (any(st.get("error") for st in g["steps"])
                               or any(st["changes"] or st["ended"] for st in g["steps"])):
            bad.append((i, "never", g))
    return bad + S.compare([{"steps": []} if r.get("never") else r for r in refs], got)


# === The generated text ==========================================================

def test_rego_monitor_reads_the_regime():
    sos, c, _ = _sos("robot")
    text = generate_norms_monitor_rego(sos, c)
    assert "core_result := {" in text and "\nresult := {" in text
    assert '_REGIMES := json.unmarshal("{\\"initial\\": \\"NORMAL\\", \\"out\\": [\\"STORM\\"]' in text
    # without the block, the library is copied unchanged
    plain = generate_norms_monitor_rego(parse_file(EXAMPLE), parse_file(EXAMPLE).contracts[0])
    assert "core_result" not in plain and "input.clock" not in plain


def test_solidity_monitor_reads_the_regime():
    sos, c, _ = _sos("robot")
    text = generate_norms_monitor_sol(sos, c)
    assert "contract DeliverySlaNormsMonitor is CadlNormsMonitorRegimes {" in text
    assert 'regime = string(hex"' in text and "_regime(" in text
    assert "function observeCore(" in text and "function setRegime(" in text
    plain = generate_norms_monitor_sol(parse_file(EXAMPLE), parse_file(EXAMPLE).contracts[0])
    assert "CadlNormsMonitorRegimes" not in plain and "obsNow" not in plain


# === OPA ===========================================================================

@needs_opa
@pytest.mark.parametrize("which,seed", [("robot", 3), ("rich", 4)])
def test_rego_monitor_against_the_python_monitor(which, seed, tmp_path):
    sos, c, vocab = _sos(which)
    rng = random.Random(seed)
    traces = _traces(rng, vocab, _count(150))
    skip = {pos - 1 for key, pos, _, _ in unsupported_items(rego_ir(sos, c)) if key == "guarantee"}
    refs = [S.reference_regimes(sos, t, c.id, skip) for t in traces]
    got = S.run_opa(generate_norms_monitor_rego(sos, c), monitor_package(c.id), traces, refs,
                    tmp_path)
    assert _check(refs, got) == []
    assert _out_ends(sos, c.id, traces) > len(traces) // 10


@needs_opa
def test_rego_reported_times_are_times_of_the_trace(tmp_path):
    """The deadline of the obligation runs on the clock of the contract;
    next_deadline, the changes and the violations give times of the trace."""
    sos, c, _ = _sos("robot")
    text = generate_norms_monitor_rego(sos, c)
    pkg = monitor_package(c.id)
    assign = {"from": "DISPATCHER", "to": "ROBOT[2]", "name": "route_assignment"}
    steps = [{"t": 0, "bindings": {"i": 2}, "values": {"ROBOT[2].battery": 80}},
             {"t": 1, "messages": [assign]},
             {"t": 3, "regime": "STORM"},
             {"t": 100, "regime": "NORMAL"},
             {"t": 102}]
    state, results = None, []
    for o in steps:
        r = S.opa_step(text, pkg, {"state": state, "observation": o}, tmp_path)
        assert "error" not in r, r
        state = r["state"]
        results.append(r)
    # acknowledge: 5 s from t = 1 on the clock; 2 s in force before STORM
    assert results[1]["next_deadline"] == 6
    assert results[2]["next_deadline"] is None and results[2]["in_force"] is False
    assert results[3]["next_deadline"] == 103
    late = S.opa_step(text, pkg, {"state": state, "observation": {"t": 104}}, tmp_path)
    assert [v["norm"] for v in late["violations"]] == ["acknowledge"]
    assert {ch["t"] for ch in late["changes"]} == {104}


# === Solidity ======================================================================

@needs_evm
@pytest.mark.parametrize("which,seed", [("robot", 11), ("rich", 12)])
def test_solidity_monitor_on_an_evm(which, seed, tmp_path):
    sos, c, vocab = _sos(which)
    rng = random.Random(seed)
    traces = _traces(rng, vocab, _count(6),
                     keep=lambda t: S.scalar_values(t) and S.six_decimals(t))
    skip = {pos - 1 for key, pos, _, _ in unsupported_items(solidity_ir(sos, c))
            if key == "guarantee"}
    refs = [S.reference_regimes(sos, t, c.id, skip) for t in traces]
    text = generate_norms_monitor_sol(sos, c)
    compiled = S.compile_solidity(text, monitor_name(c.id), tmp_path)
    from cadl.codegen.solidity.norms_monitor_sol import RUNTIME_LIMIT, estimated_initcode
    mine = compiled[f"Monitor.sol:{monitor_name(c.id)}"]
    assert len(mine["bin"]) // 2 <= estimated_initcode(text) and mine["runtime"] <= RUNTIME_LIMIT
    runs = [S.sol_run_regimes(t, r.get("quantities", [])) for t, r in zip(traces, refs)]
    raw = S.run_evm(compiled, f"Monitor.sol:{monitor_name(c.id)}", runs, tmp_path, ctor_args=2)
    got = S.sol_results(raw, runs, [n.id for n in c.norms], skip)
    assert _check(refs, got) == []


@needs_evm
def test_solidity_only_the_orchestrator_sets_the_regime(tmp_path):
    sos, c, _ = _sos("robot")
    compiled = S.compile_solidity(generate_norms_monitor_sol(sos, c), monitor_name(c.id), tmp_path)
    good = S.sol_run_regimes([json.dumps({"t": 0, "bindings": {"i": 2}, "regime": "STORM"})], [])
    other = json.loads(json.dumps(good))
    other["steps"][0]["regime_as"] = 1        # another account than the deployer
    unknown = S.sol_run_regimes([json.dumps({"t": 0, "bindings": {"i": 2}, "regime": "FOG"})],
                                [])
    raw = S.run_evm(compiled, f"Monitor.sol:{monitor_name(c.id)}", [good, other, unknown],
                    tmp_path, ctor_args=2)
    assert "error" not in raw[0][0] and raw[0][0]["deadline"] == [False, 0]
    assert "only the orchestrator" in raw[1][0]["error"]
    assert "regime of the file" in raw[2][0]["error"]


# === Unity C# ======================================================================

@needs_mono
def test_unity_against_the_clock_run(tmp_path):
    from tests import unity_regimes_support as U
    path = ROOT / "examples" / "sos_dsl_robot_delivery.cadl"
    text = path.read_text(encoding="utf-8").rstrip("\n") + "\n" + textwrap.indent(
        REGIMES % "DELIVERY_SLA", "  ")
    rng = random.Random(2)
    runs = [U.random_run(rng, ["NORMAL", "STORM"]) for _ in range(_count(60))]
    bad, compared, left = U.compare(parse(text), parse_file(path), "DELIVERY_SLA", runs,
                                    {"STORM"}, "NORMAL", tmp_path)
    assert bad == []
    assert compared > len(runs) * 5 and left > len(runs) * 3


def test_unity_regimes_file(tmp_path):
    path = ROOT / "examples" / "sos_dsl_robot_delivery.cadl"
    text = path.read_text(encoding="utf-8").rstrip("\n") + "\n" + textwrap.indent(
        REGIMES % "DELIVERY_SLA", "  ")
    generate(parse(text), tmp_path, target="unity-csharp")
    regimes = (tmp_path / "Generated" / "CadlRegimes.cs").read_text()
    assert 'Initial = "NORMAL"' in regimes
    assert '{ "STORM", new HashSet<string> { "DELIVERY_SLA" } },' in regimes
    contract = (tmp_path / "Generated" / "DeliverySlaContract.cs").read_text()
    assert "IContractInstance, IRegimeBound" in contract
    assert "if (Closed || _out) return;" in contract
    assert "_monitors.Shift(d);" in contract


# === Boundary policies ===============================================================

def test_decide_reads_the_regime():
    from cadl.agents import AgentsInputError, build_policy, decide
    path = ROOT / "examples" / "agents_tool_governance.cadl"
    sos = parse(_with_regimes(path, "ASSISTED_REPORTING"))
    policy = build_policy(sos, regimes=True)
    msg = {"from": "PLANNER", "to": "FILES", "name": "delete_file"}
    with pytest.raises(AgentsInputError, match="'regime' must name the current regime"):
        decide(policy, {"message": msg})
    with pytest.raises(AgentsInputError, match="'regime' must name the current regime"):
        decide(policy, {"message": msg, "regime": "FOG"})
    normal = decide(policy, {"message": msg, "regime": "NORMAL"})
    storm = decide(policy, {"message": msg, "regime": "STORM"})
    plain = decide(build_policy(parse_file(path)), {"message": msg})
    assert normal == plain
    assert storm["reason"] == "ungoverned" and storm["decision"] == "allow"


def test_boundary_policies_against_the_decision_function():
    from tests import agent_policy_diff as D
    if not (D.OPA or D.CEDAR):
        pytest.skip("neither opa nor cedar is on PATH")
    total = D.run_random(seed=36, n_files=_count(2), n_sets=3, regimes=True)
    assert total["files"] == _count(2)
    if D.OPA:
        assert total["opa_inputs"] > 0
    if D.CEDAR:
        assert total["cedar"]["inputs"] > 0
