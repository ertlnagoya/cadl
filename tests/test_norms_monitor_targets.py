"""The norms monitors of the OPA and Solidity targets (Appendix F, F.5;
Appendix D): `cadl codegen -t opa` writes norms_monitor_<contract>.rego, a
step function of the monitor, and `-t solidity` writes
<Contract>NormsMonitor.sol, a contract that keeps the state of the
monitor.

Both are compared with the engine of `cadl norms-eval` after every
observation (tests/norms_targets_support.py): on the example traces, on
the traces of tests/test_norms_eval.py and on random traces. The Rego is
run with `opa eval` (skipped without the opa binary); the Solidity is
compiled with solc (skipped without it: CADL_SOLC, solc on PATH or
~/.solcx) and run on an EVM in a separate Python that has eth-tester and
py-evm (CADL_EVM_PYTHON; skipped without it). CADL_NORMS_TARGET_TRACES
sets the number of random traces of each set (default 150 for OPA, 6
for the EVM, which interprets py-evm in Python and is slow)."""

from __future__ import annotations

import base64
import copy
import dataclasses
import functools
import json
import os
import pickle
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cadl.codegen import generate, norms_monitors
from cadl.codegen.norms_ir import NormsTargetUnsupported, build_ir
from cadl.codegen.opa.norms_monitor_rego import (
    generate_norms_monitor_rego, monitor_filename, monitor_package,
)
from cadl.codegen.safety import UnsafeCodegenInput
from cadl.codegen.solidity.norms_monitor_sol import generate_norms_monitor_sol, monitor_name
from cadl.norms_eval import NormsEvalError, select_contract
from cadl.parser import parse, parse_file

from tests import norms_targets_support as S
from tests.test_norms_monitor import (
    _HOSTILE_TRACE, _PAYLOADS, _RICH, _ROBOT, EXAMPLE, RICH, _hostile_sos, _random_trace,
)

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
TRACES = EXAMPLES / "traces"
EXAMPLE_TRACES = sorted(TRACES.glob("norms_robot_delivery_*.jsonl"))
NO_DOUBLE = EXAMPLES / "norms_no_double_assignment.cadl"

needs_opa = pytest.mark.skipif(S.opa_binary() is None, reason="the opa binary is not on PATH")
needs_solc = pytest.mark.skipif(S.solc_binary() is None, reason="no solc (CADL_SOLC, PATH, ~/.solcx)")
# CADL_REQUIRE_EVM=1 (CI): the EVM tests run, and fail when there is no EVM
REQUIRE_EVM = os.environ.get("CADL_REQUIRE_EVM") == "1"
needs_evm = pytest.mark.skipif(
    not REQUIRE_EVM and (S.solc_binary() is None or S.evm_python() is None),
    reason="no solc, or no EVM (CADL_EVM_PYTHON: a Python with eth-tester[py-evm])")


def test_evm_is_there_when_required():
    """With CADL_REQUIRE_EVM=1 a missing EVM or solc is a failure, not a skip."""
    if not REQUIRE_EVM:
        pytest.skip("CADL_REQUIRE_EVM is not set")
    assert S.solc_binary() is not None, "no solc (CADL_SOLC, PATH or ~/.solcx)"
    assert S.evm_python() is not None, "no EVM (CADL_EVM_PYTHON: eth-tester[py-evm])"


def _count(default: int) -> int:
    return int(os.environ.get("CADL_NORMS_TARGET_TRACES", default))


def _memoize_parser(monkeypatch) -> None:
    import cadl.norms_eval as ne
    for name in ("_parse_rule", "_parse_expr"):
        cached = functools.lru_cache(maxsize=None)(getattr(ne, name))
        monkeypatch.setattr(ne, name, lambda text, cached=cached: copy.deepcopy(cached(text)))


def _small(blocks: str, actors: str = "    - {id: A, role: a}\n    - {id: B, role: b}\n",
           parties: str = "[A, B]"):
    return parse(
        "sos:\n  name: Q\n  extensions:\n    - sos-dsl: 0.1\n    - norms: 0.1\n"
        "  actors:\n" + actors +
        "  contracts:\n    - id: C\n      parties: " + parties + "\n" + blocks)


def _lines(*records) -> list:
    return [json.dumps(r) for r in records]


# === What is generated ===========================================================

class TestGeneration:
    @pytest.mark.parametrize("target", ["opa", "solidity"])
    def test_files_without_norms_get_no_monitor(self, target, tmp_path):
        """For a file without norms 0.1 the targets write what they wrote
        before: the same files, from the same generators."""
        for path in sorted(EXAMPLES.glob("*.cadl")):
            sos = parse_file(path)
            if ("norms", "0.1") in sos.extensions:
                continue
            assert norms_monitors(sos, target) == []
            out = tmp_path / path.stem
            generate(sos, out, target=target)
            assert not [p for p in out.iterdir() if "orms_monitor" in p.name.lower()
                        or "NormsMonitor" in p.name]

    @pytest.mark.parametrize("target,name", [("opa", "norms_monitor_delivery_sla.rego"),
                                             ("solidity", "DeliverySlaNormsMonitor.sol")])
    def test_one_monitor_per_contract(self, target, name, tmp_path):
        generate(parse_file(EXAMPLE), tmp_path, target=target)
        assert (tmp_path / name).exists()
        assert monitor_filename("DELIVERY_SLA") == "norms_monitor_delivery_sla.rego"
        assert monitor_name("DELIVERY_SLA") == "DeliverySlaNormsMonitor"

    @pytest.mark.parametrize("target", ["opa", "solidity"])
    def test_cli_names_the_monitor_and_what_it_monitors(self, target, tmp_path):
        proc = subprocess.run(
            [sys.executable, "-m", "cadl.cli", "codegen", str(EXAMPLE), "-t", target,
             "-o", str(tmp_path / "out")], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        name = "norms_monitor_delivery_sla.rego" if target == "opa" else "DeliverySlaNormsMonitor.sol"
        assert f"Norms monitor: {name} (5 norm(s), 1 counts-as rule(s))" in proc.stdout
        warnings = [l for l in proc.stderr.splitlines() if "temporal operator" in l]
        assert len(warnings) == 2
        assert all(f"{name} monitors it" in w for w in warnings)

    @pytest.mark.parametrize("target", ["opa", "solidity"])
    def test_contract_with_a_quantifier_is_refused(self, target, tmp_path):
        """Nothing is monitored in a way that differs from norms-eval: the
        contract gets no monitor, and the warning says so and why."""
        sos = parse_file(NO_DOUBLE)
        with pytest.raises(NormsTargetUnsupported, match="quantifier"):
            build_ir(sos, sos.contracts[0])
        [m] = norms_monitors(sos, target)
        assert m["text"] is None and "quantifier" in m["reasons"][0]
        proc = subprocess.run(
            [sys.executable, "-m", "cadl.cli", "codegen", str(NO_DOUBLE), "-t", target,
             "-o", str(tmp_path / "out")], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "no norms monitor is generated" in proc.stderr
        assert "no_double_assignment" in proc.stderr
        assert not list((tmp_path / "out").glob("*orms*onitor*"))

    @pytest.mark.parametrize("text,match", [
        ("count(S[*].x == 1) > 0", "aggregate"),
        ("S[*].x == 1", "wildcard"),
        ("S[i+1].x == 1", "index of an actor reference"),
        ("exists j in S: S[j].x == 1", "quantifier"),
    ])
    def test_unsupported_predicates(self, text, match):
        sos = _small(f"      norms:\n        - {{id: n, kind: prohibition, bearer: A, "
                     f"content: \"{text}\"}}\n",
                     actors="    - {id: A, role: a}\n    - {id: \"S[1..3]\", role: s}\n",
                     parties="[A, \"S[*]\"]")
        with pytest.raises(NormsTargetUnsupported, match=match):
            build_ir(sos, sos.contracts[0])

    def test_unsupported_guarantee_item_is_left_out(self, tmp_path):
        """A guarantee item does not act on anything else: it is left out
        (and reported), and the rest is monitored."""
        sos = _small("      guarantee:\n        - \"exists j in S: S[j].x == 1\"\n"
                     "        - \"A.ok == true\"\n"
                     "      norms:\n        - {id: n, kind: obligation, bearer: A, "
                     "content: \"A -> B : done\", deadline: 2s}\n",
                     actors="    - {id: A, role: a}\n    - {id: B, role: b}\n"
                            "    - {id: \"S[1..3]\", role: s}\n",
                     parties="[A, B, \"S[*]\"]")
        for target in ("opa", "solidity"):
            [m] = norms_monitors(sos, target)
            assert m["text"] is not None
            assert [(k, p) for k, p, _, _ in m["left_out"]] == [("guarantee", 1)]
        path = tmp_path / "q.cadl"
        path.write_text(
            "sos:\n  name: Q\n  extensions:\n    - sos-dsl: 0.1\n    - norms: 0.1\n"
            "  actors:\n    - {id: A, role: a}\n    - {id: B, role: b}\n"
            "    - {id: \"S[1..3]\", role: s}\n"
            "  contracts:\n    - id: C\n      parties: [A, B, \"S[*]\"]\n"
            "      guarantee:\n        - \"exists j in S: S[j].x == 1\"\n", encoding="utf-8")
        proc = subprocess.run([sys.executable, "-m", "cadl.cli", "codegen", str(path), "-t", "opa",
                               "-o", str(tmp_path / "out")], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "guarantee item 1 of contract 'C' is not monitored by norms_monitor_c.rego" \
            in proc.stderr

    @pytest.mark.parametrize("content,match", [
        ("A.x * 2 > 3", "the operator \\*"), ("A.x / 2 > 3", "the operator /"),
        ("A.x > 0.0000001", "more than 6 decimals"),
        ("within(5s, until(A.x > 3, A.y > 1, 2s))", "until inside within or throughout"),
    ])
    def test_solidity_refuses_what_its_fixed_point_cannot_hold(self, content, match):
        sos = _small(f"      norms:\n        - {{id: n, kind: prohibition, bearer: A, "
                     f"content: \"{content}\"}}\n")
        if "/" not in content:
            generate_norms_monitor_rego(sos, sos.contracts[0])      # OPA monitors it
        with pytest.raises(NormsTargetUnsupported, match=match):
            generate_norms_monitor_sol(sos, sos.contracts[0])

    @pytest.mark.parametrize("item", ["A.x * 2 > 3", "within(5s, throughout(2s, A.x > 3))"])
    def test_solidity_leaves_out_such_a_guarantee_item(self, item):
        """As for OPA: a guarantee item that the Solidity monitor cannot
        evaluate is left out, and the rest is monitored."""
        sos = _small(f"      guarantee:\n        - \"{item}\"\n        - \"A.ok == true\"\n"
                     "      norms:\n        - {id: n, kind: prohibition, bearer: A, "
                     "content: \"A.x > 3\"}\n")
        [m] = norms_monitors(sos, "solidity")
        assert m["text"] is not None
        assert [(k, p) for k, p, _, _ in m["left_out"]] == [("guarantee", 1)]
        assert norms_monitors(sos, "opa")[0]["left_out"] == []      # OPA evaluates it

    def test_solidity_refuses_a_monitor_above_the_initcode_limit(self):
        """EIP-3860: a monitor whose creation code would exceed 49152 bytes
        is not generated (the estimate is checked against solc below)."""
        norms = "".join(
            f"        - {{id: o{i}, kind: obligation, bearer: A, trigger: \"B -> A : req{i}\", "
            f"content: \"A.x{i} >= {i} AND A.y{i} == true\", deadline: 5s}}\n"
            for i in range(40))
        sos = _small("      norms:\n" + norms)
        generate_norms_monitor_rego(sos, sos.contracts[0])
        with pytest.raises(NormsTargetUnsupported, match="EIP-3860"):
            generate_norms_monitor_sol(sos, sos.contracts[0])
        [m] = norms_monitors(sos, "solidity")
        assert m["text"] is None and "too large" in m["reasons"][0]

    def test_solidity_names_the_bare_names_of_temporal_operators(self):
        from cadl.codegen.solidity.norms_monitor_sol import solidity_ir, temporal_names
        sos = _small("      guarantee:\n        - \"throughout(10s, A.s == flag OR now > 3)\"\n"
                     "        - \"other == 1\"\n")
        assert temporal_names(solidity_ir(sos, sos.contracts[0])) == ["flag"]
        assert "temporalName[keccak256" in generate_norms_monitor_sol(sos, sos.contracts[0])

    def test_solidity_refuses_five_temporal_operators_in_a_content(self):
        """The Solidity monitor decides the candidates of a content from at
        most four temporal operators (the 4^m values of the content in one
        word); OPA monitors such a content too."""
        content = ("within(3s, A.x > 1) OR throughout(2s, A.y == 1) OR within(1s, A.z == 1) "
                   "OR throughout(1s, A.x > 0) OR within(2s, A.y == 0)")
        sos = _small(f"      norms:\n        - {{id: n, kind: prohibition, bearer: A, "
                     f"content: \"{content}\"}}\n")
        generate_norms_monitor_rego(sos, sos.contracts[0])
        with pytest.raises(NormsTargetUnsupported, match="5 temporal operators .* at most 4"):
            generate_norms_monitor_sol(sos, sos.contracts[0])

    @pytest.mark.parametrize("content,match", [
        ("until(A.x > 1, within(2s, A.y == 1), 3s)", "until with a temporal operator in an operand"),
        ("within(3s, until(A.x > 1, A.y == 1, 2s))", "until inside within or throughout"),
        ("within(3s, throughout(2s, A.y == 1) AND A.x > 1)", "more than that operator or its negation"),
        ("throughout(3s, throughout(2s, A.y == 1) == true)", "more than that operator or its negation"),
        ("within(3s, throughout(2s, within(1s, A.y == 1)))", "three nested temporal operators"),
    ])
    def test_solidity_refuses_nested_operators_it_does_not_feed_in_order(self, content, match):
        """A temporal operator inside another one is fed in the order of the
        observations only when it is the operand (or its negation) and is
        within or throughout over an operand without one (D.5)."""
        sos = _small(f"      norms:\n        - {{id: n, kind: prohibition, bearer: A, "
                     f"content: \"{content}\"}}\n")
        generate_norms_monitor_rego(sos, sos.contracts[0])
        with pytest.raises(NormsTargetUnsupported, match=match):
            generate_norms_monitor_sol(sos, sos.contracts[0])

    def test_solidity_nested_operators_outside_contents(self):
        """Outside the content of a norm a window is scanned once, so a
        nested operator refuses an assumption and leaves out a guarantee."""
        item = "within(3s, throughout(1s, A.x > 1))"
        sos = _small(f"      assume:\n        - \"{item}\"\n")
        with pytest.raises(NormsTargetUnsupported, match="outside the content of a norm"):
            generate_norms_monitor_sol(sos, sos.contracts[0])
        sos = _small(f"      guarantee:\n        - \"{item}\"\n")
        assert "_guarantee(" in generate_norms_monitor_sol(sos, sos.contracts[0])

    @pytest.mark.parametrize("content,call", [
        ("within(3s, A.x > 1) OR A.y == 1", "_temporal(1, "),
        ("NOT throughout(2s, A.x > 1) AND A.ok == true", "_temporal(2, "),
        ("NOT (within(2s, A.x == 0) OR A.z == 1)", "_temporal(1, "),
        ("NOT until(A.x > 1, A.y == 1, 3s)", "_temporal(3, "),
        ("within(3s, A.x > 1) == true", "_temporal(1, "),
        ("until(A.x > 1, A.y == 1, 3s) <= A.z", "_temporal(3, "),
        ("within(3s, A.x > 1) OR throughout(2s, A.y == 1)", "_multi("),
        ("within(3s, A.x > 1) == throughout(2s, A.y == 1)", "_multi("),
        ("within(3s, A.x > 1) OR throughout(2s, A.y == 1) OR within(1s, A.z == 1)", "_multi(13, 1);"),
        ("within(3s, A.x > 1) OR throughout(2s, A.y == 1) OR within(1s, A.z == 1) "
         "OR NOT until(A.x > 0, A.y == 0, 2s)", "_multi(22, 5);"),
        ("within(3s, throughout(1s, A.x > 1))", "_nested(1, 3000000, 4, 2, false, false, 1000000);"),
        ("throughout(3s, NOT within(1s, A.x > 1))", "_nested(2, 3000000, 5, 2, true, false, 1000000);"),
        ("within(3s, NOT NOT within(1s, A.x > 1)) OR A.z == 1",
         "_nested(1, 3000000, 6, 2, true, true, 1000000);"),
    ])
    def test_solidity_temporal_contents_it_decides(self, content, call):
        """One temporal operator anywhere in a content (under NOT, in a
        comparison) goes to the queues of CadlNormsWindows; two to four,
        and a nested one, to the segment tree of CadlNormsMulti (a node of
        one word up to three operators, of five words for four)."""
        sos = _small(f"      norms:\n        - {{id: n, kind: prohibition, bearer: A, "
                     f"content: \"{content}\"}}\n")
        assert call in generate_norms_monitor_sol(sos, sos.contracts[0])

    def test_rego_carries_the_contract_as_one_json_text(self):
        """No text of the .cadl file is Rego code: the model is one JSON
        string, and the other literals are JSON strings too."""
        sos = parse_file(RICH)
        text = generate_norms_monitor_rego(sos, sos.contracts[0])
        [line] = [l for l in text.splitlines() if l.startswith("model := ")]
        inner = json.loads(line[len("model := json.unmarshal("):-1])
        model = json.loads(inner)
        assert [n["id"] for n in model["norms"]] == [n.id for n in sos.contracts[0].norms]
        code = text.replace(line, "")
        for n in sos.contracts[0].norms:
            assert re.search(rf"(?<![\w\"]){re.escape(n.id)}(?![\w\"])", code.split(
                "# >>> norms-monitor-library")[0]) is None

    def test_solidity_writes_texts_as_hex(self):
        sos = parse_file(RICH)
        text = generate_norms_monitor_sol(sos, sos.contracts[0])
        ctor = text.split("contract CNormsMonitor is CadlNormsMonitorBase {")[1]
        assert "deliver" not in ctor and "apologise" not in ctor
        assert 'string(hex"' in ctor
        rest = re.sub(r'string\(hex"[0-9a-f]*"\)', "", ctor).replace('""', "")
        assert '"' not in rest

    def test_generators_check_the_contract_id(self):
        sos = parse_file(EXAMPLE)
        bad = dataclasses.replace(sos.contracts[0], id="X\npackage evil")
        for gen in (generate_norms_monitor_rego, generate_norms_monitor_sol):
            with pytest.raises(UnsafeCodegenInput):
                gen(sos, bad)


# === Strings of the file cannot become code =======================================

_TARGET_PAYLOADS = _PAYLOADS + [
    '"}\npackage evil\nresult := {"ok": true}\n#',
    '`; default allow := true #',
    '"); selfdestruct(payable(msg.sender)); //',
    'hex"00" unicode"\u2028" /* x',
    "\u2028\u2029\U0001f600 \\u0022",
]


def _hostile(payload: str):
    try:
        sos = _hostile_sos(payload)
    except Exception:
        return None
    contract = dataclasses.replace(sos.contracts[0], id="C")
    return dataclasses.replace(sos, contracts=[contract])


class TestHostileStrings:
    @pytest.mark.parametrize("payload", _TARGET_PAYLOADS, ids=range(len(_TARGET_PAYLOADS)))
    def test_rejected_by_the_input_check(self, payload, tmp_path):
        """Through `generate`, the input check refuses these strings
        before anything is written (the lifecycle states, which it does not
        check, are data in both monitors)."""
        sos = _hostile(payload)
        if sos is None:
            pytest.skip("YAML cannot carry this string")
        for target in ("opa", "solidity"):
            try:
                generate(sos, tmp_path / target, target=target)
            except UnsafeCodegenInput:
                assert not (tmp_path / target).exists()
                continue
            # what passed the check: the payload is in no file but as data
            for path in (tmp_path / target).glob("*orms*onitor*"):
                assert SENTINEL_CHECK(payload, path.read_text(encoding="utf-8"))

    @needs_opa
    @pytest.mark.parametrize("payload", _TARGET_PAYLOADS, ids=range(len(_TARGET_PAYLOADS)))
    def test_rego_embeds_them_as_data(self, payload, tmp_path):
        """Without the input check: the policy is valid Rego, carries the
        strings as data, and agrees with norms-eval on a trace."""
        sos = _hostile(payload)
        if sos is None:
            pytest.skip("YAML cannot carry this string")
        try:
            text = generate_norms_monitor_rego(sos, sos.contracts[0])
        except NormsTargetUnsupported:
            pytest.skip("the payload makes a predicate the monitor does not support")
        assert SENTINEL_CHECK(payload, text)
        lines = [json.dumps(r) for r in _HOSTILE_TRACE]
        refs = [S.reference(sos, lines)]
        got = S.run_opa(text, monitor_package("C"), [lines], refs, tmp_path)
        assert S.compare(refs, got) == []

    @needs_solc
    def test_solidity_embeds_them_as_data(self, tmp_path):
        """Without the input check: every monitor compiles (one solc run
        for all payloads), with the strings as hex literals."""
        files = []
        for k, payload in enumerate(_TARGET_PAYLOADS):
            sos = _hostile(payload)
            if sos is None:
                continue
            try:
                text = generate_norms_monitor_sol(sos, sos.contracts[0])
            except NormsTargetUnsupported:
                continue
            assert SENTINEL_CHECK(payload, text)
            (tmp_path / f"p{k}.sol").write_text(text, encoding="utf-8")
            files.append(f"p{k}.sol")
        assert len(files) >= 8
        proc = subprocess.run([S.solc_binary(), "--optimize", "--combined-json", "abi", *files],
                              cwd=tmp_path, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[-3000:]
        names = json.loads(proc.stdout)["contracts"]
        assert all(f"{f}:CNormsMonitor" in names for f in files)


def SENTINEL_CHECK(payload: str, text: str) -> bool:
    """The payload (when it has a character that matters in code) occurs
    in the generated text only inside string literals (JSON strings in the
    Rego, hex literals in the Solidity)."""
    code = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', text)
    special = set('"\'\\\n\r`{}$;#/') & set(payload)
    return not special or len(payload) < 4 or payload not in code


# === The step function of the OPA target =============================================

@pytest.fixture(scope="module")
def robot_rego():
    sos = parse_file(EXAMPLE)
    return generate_norms_monitor_rego(sos, sos.contracts[0])


PKG = monitor_package("DELIVERY_SLA")


@needs_opa
class TestRegoStep:
    def test_state_of_appendix_d(self, robot_rego, tmp_path):
        r = S.opa_step(robot_rego, PKG, {"observation": {
            "t": 0, "bindings": {"i": 2}, "values": {"ROBOT[2].battery": 80}}}, tmp_path)
        st = r["state"]
        assert set(st) == {"format", "contract", "n", "t", "ended", "end_index", "ignored",
                           "bindings", "request", "known", "values", "lc", "buffer", "norms",
                           "items", "failed_at"}
        assert st["format"] == "cadl-norms-monitor-state/1" and st["n"] == 1
        assert st["lc"] == "Proposed" and st["known"] == ["ROBOT[2].battery"]
        assert [n["id"] for n in st["norms"]] == ["acknowledge", "report_failure",
                                                 "no_low_battery_driving",
                                                 "may_return_to_charge", "reassign"]
        assert {c["id"] for c in r["changes"]} == {"no_low_battery_driving",
                                                   "may_return_to_charge", "reassign"}
        # the assume item is temporal: the observation is kept for it
        assert [e["i"] for e in st["buffer"]] == [0]
        r2 = S.opa_step(robot_rego, PKG, {"state": st, "observation": {
            "t": 1, "messages": [{"from": "DISPATCHER", "to": "ROBOT[2]",
                                  "name": "route_assignment"}]}}, tmp_path)
        assert r2["verdicts"]["norms"]["acknowledge"] == {"state": "active", "verdict": "pending"}
        assert r2["next_deadline"] == 6
        r3 = S.opa_step(robot_rego, PKG, {"state": r2["state"], "observation": {"t": 7}},
                        tmp_path)
        assert r3["violations"][0]["norm"] == "acknowledge"
        assert r3["violations"][0]["reparation_activated"] is True
        assert r3["verdicts"]["norms"]["report_failure"]["state"] == "active"

    @pytest.mark.parametrize("obs,match", [
        ({"t": "x"}, "'t' must be a number"),
        ({"t": -1}, "before the previous observation"),
        ({"t": 2, "amend": "v2.cadl"}, "not supported"),
        ({"t": 2, "state": "Nowhere"}, "not a state of the lifecycle"),
        ({"t": 2, "bindings": {"i": 3}}, "only on the first line"),
        ({"t": 2, "values": {"x": [1]}}, "null, a boolean, a number or a string"),
        ({"t": 2, "messages": [{"from": "A B", "to": "C", "name": "m"}]}, "concrete actor"),
        ({"t": 2, "colour": 1}, "unknown key"),
    ])
    def test_refused_observation_keeps_the_state(self, obs, match, robot_rego, tmp_path):
        r = S.opa_step(robot_rego, PKG, {"observation": {"t": 0, "bindings": {"i": 2}}}, tmp_path)
        bad = S.opa_step(robot_rego, PKG, {"state": r["state"], "observation": obs}, tmp_path)
        assert match in bad["error"]
        assert bad["state"] == r["state"]

    def test_finish_and_after_the_end(self, robot_rego, tmp_path):
        r = S.opa_step(robot_rego, PKG, {"observation": {"t": 0, "bindings": {"i": 2}}}, tmp_path)
        r = S.opa_step(robot_rego, PKG, {"state": r["state"], "observation": {"t": 1}}, tmp_path)
        f = S.opa_step(robot_rego, PKG, {"state": r["state"], "finish": True}, tmp_path)
        assert f["ended"] and f["state"]["ended"] and f["state"]["buffer"] == []
        assert f["verdicts"]["norms"]["no_low_battery_driving"]["state"] == "fulfilled"
        again = S.opa_step(robot_rego, PKG, {"state": f["state"], "finish": True}, tmp_path)
        assert again["changes"] == [] and again["state"] == f["state"]
        late = S.opa_step(robot_rego, PKG, {"state": f["state"], "observation": {"t": 5}},
                          tmp_path)
        assert late["changes"] == [] and late["state"]["ignored"] == 1
        assert late["state"]["t"] == {"$q": [5, 0]}           # exact: 5 / 10^0
        none = S.opa_step(robot_rego, PKG, {"finish": True}, tmp_path)
        assert "no observation" in none["error"]

    def test_state_of_another_contract_is_refused(self, robot_rego, tmp_path):
        r = S.opa_step(robot_rego, PKG, {"observation": {"t": 0}}, tmp_path)
        st = dict(r["state"], contract="OTHER")
        assert "not made by the monitor" in S.opa_step(
            robot_rego, PKG, {"state": st, "observation": {"t": 1}}, tmp_path)["error"]


# === Differential tests: OPA ========================================================

def _ex_traces() -> list:
    return [p.read_text(encoding="utf-8").splitlines() for p in EXAMPLE_TRACES]


@needs_opa
def test_opa_example_traces(tmp_path):
    sos = parse_file(EXAMPLE)
    traces = _ex_traces()
    refs = [S.reference(sos, l) for l in traces]
    got = S.run_opa(generate_norms_monitor_rego(sos, sos.contracts[0]), PKG, traces, refs,
                    tmp_path)
    assert S.compare(refs, got) == []


RANDOM = [("robot_delivery", EXAMPLE, _ROBOT, 20261121), ("rich", RICH, _RICH, 20261122)]


def _seen(refs: list) -> dict:
    seen = {"verdicts": set(), "states": set(), "violations": 0, "unauthorised": 0,
            "errors": 0, "prefix": 0}
    for ref in refs:
        if "error" in ref:
            seen["errors"] += 1
            continue
        for st in ref["steps"]:
            for state, verdict in st["norms"].values():
                seen["states"].add(state)
                seen["verdicts"].add(verdict)
            seen["verdicts"].update(st["assume"] + st["guarantee"])
            seen["violations"] += len(st["violations"])
            seen["unauthorised"] += len(st["unauthorised"])
        seen["prefix"] += not ref["steps"][-1]["ended"]
    return seen


@needs_opa
@pytest.mark.parametrize("name,path,vocab,seed", RANDOM, ids=[r[0] for r in RANDOM])
def test_opa_random_traces(name, path, vocab, seed, tmp_path, monkeypatch):
    """Random traces (the generator of tests/test_norms_monitor.py): the
    monitor reports what norms-eval reports after every observation."""
    _memoize_parser(monkeypatch)
    sos = parse_file(path)
    rng = random.Random(seed)
    traces = [_random_trace(rng, vocab) for _ in range(_count(150))]
    refs = [S.reference(sos, l) for l in traces]
    got = S.run_opa(generate_norms_monitor_rego(sos, sos.contracts[0]),
                    monitor_package(sos.contracts[0].id), traces, refs, tmp_path)
    steps: list = []
    assert S.compare(refs, got, steps) == []
    seen = _seen(refs)
    assert {"pending", "fulfilled", "violated", "excused", "holds", "failed"} <= seen["verdicts"]
    assert {"inactive", "active", "fulfilled", "violated", "discharged"} <= seen["states"]
    assert seen["violations"] and seen["prefix"]
    if name == "rich":
        assert "suspended" in seen["states"] and seen["unauthorised"]


@needs_opa
def test_opa_cases_of_test_norms_eval(norms_eval_cases, tmp_path, monkeypatch):
    """The traces of tests/test_norms_eval.py, for each contract the OPA
    monitor supports (all but those with an amendment, which the monitor
    refuses at the observation that has it)."""
    _memoize_parser(monkeypatch)
    groups: dict = {}
    for case in norms_eval_cases["cases"]:
        if case["versions"] or any('"amend"' in l for l in case["lines"]):
            continue
        sos = pickle.loads(base64.b64decode(case["sos"]))
        try:
            contract = select_contract(sos, case["contract"])
            text = generate_norms_monitor_rego(sos, contract)
        except (NormsEvalError, NormsTargetUnsupported):
            continue
        assert len(S.records(case["lines"])) <= S.MAX_STEPS
        groups.setdefault(text, (sos, contract, []))[2].append(case)
    assert sum(len(g[2]) for g in groups.values()) >= 100
    steps: list = []
    for k, (text, (sos, contract, cases)) in enumerate(groups.items()):
        refs = [S.reference(sos, case["lines"], case["contract"]) for case in cases]
        got = S.run_opa(text, monitor_package(contract.id), [c["lines"] for c in cases], refs,
                        tmp_path / f"m{k}")
        assert S.compare(refs, got, steps) == [], cases[0]["lines"]
    assert len(steps) >= 300


# === Solidity ========================================================================

@needs_solc
def test_every_solidity_monitor_compiles_within_the_size_limit(tmp_path):
    """Every monitor generated from the examples and the test files
    compiles, its runtime code (and that of the two libraries) is within
    the limit of EIP-170, and its creation code within that of EIP-3860
    and below the estimate that `cadl codegen` checks."""
    from cadl.codegen.solidity.norms_monitor_sol import estimated_initcode
    seen = 0
    for path in sorted(EXAMPLES.glob("*.cadl")) + [RICH]:
        sos = parse_file(path)
        for m in norms_monitors(sos, "solidity"):
            if m["text"] is None:
                continue
            compiled = S.compile_solidity(m["text"], m["file"], tmp_path / f"{path.stem}")
            for name, c in compiled.items():
                if c["bin"]:
                    assert c["runtime"] <= 24576, (path, name, c["runtime"])
                    assert len(c["bin"]) // 2 <= 49152, (path, name)
            init = len(compiled[f"Monitor.sol:{m['file'][:-4]}"]["bin"]) // 2
            assert init <= estimated_initcode(m["text"]), (path, init)
            if path == RICH:            # the default of solc, 200 runs: larger code
                big = S.compile_solidity(m["text"], m["file"], tmp_path / "runs200", runs=200)
                init = len(big[f"Monitor.sol:{m['file'][:-4]}"]["bin"]) // 2
                assert init <= estimated_initcode(m["text"]), (path, init)
            seen += 1
    assert seen >= 6


def _evm(sos, traces: list, tmp: Path, skip: set = frozenset()) -> tuple:
    contract = sos.contracts[0]
    refs = [S.reference(sos, l, skip=skip) for l in traces]
    compiled = S.compile_solidity(generate_norms_monitor_sol(sos, contract),
                                  monitor_name(contract.id), tmp)
    runs = [S.sol_run(l, r.get("quantities", [])) for l, r in zip(traces, refs)]
    raw = S.run_evm(compiled, f"Monitor.sol:{monitor_name(contract.id)}", runs, tmp)
    _RAW.append(raw)
    got = S.sol_results(raw, runs, [n.id for n in contract.norms], skip)
    return refs, got


_RAW: list = []          # the last raw results of _evm, for the gas


@needs_evm
def test_evm_example_traces(tmp_path):
    refs, got = _evm(parse_file(EXAMPLE), _ex_traces(), tmp_path)
    assert S.compare(refs, got) == []


@needs_evm
@pytest.mark.parametrize("name,path,vocab,seed", RANDOM, ids=[r[0] for r in RANDOM])
def test_evm_random_traces(name, path, vocab, seed, tmp_path, monkeypatch):
    _memoize_parser(monkeypatch)
    sos = parse_file(path)
    rng = random.Random(seed + 100)
    traces = [_random_trace(rng, vocab) for _ in range(_count(6))]
    refs, got = _evm(sos, traces, tmp_path)
    assert S.compare(refs, got) == []


@needs_evm
def test_evm_refuses_a_malformed_observation(tmp_path):
    """A reverted call leaves the contract as it was: the next
    observation is processed as if the refused one had not been sent."""
    sos = parse_file(EXAMPLE)
    good = _lines({"t": 0, "bindings": {"i": 2}},
                  {"t": 1, "messages": [{"from": "DISPATCHER", "to": "ROBOT[2]",
                                         "name": "route_assignment"}]},
                  {"t": 7})
    refs = [S.reference(sos, good)]
    run = S.sol_run(good, [])
    bad = {"observe": [0, [], [], False, "", False]}                 # time goes back
    bad2 = {"observe": [2 * 10 ** 6, [], [], True, "Nowhere", False]}  # no such state
    run["steps"] = run["steps"][:2] + [bad, bad2] + run["steps"][2:]
    contract = sos.contracts[0]
    compiled = S.compile_solidity(generate_norms_monitor_sol(sos, contract),
                                  monitor_name(contract.id), tmp_path)
    [raw] = S.run_evm(compiled, f"Monitor.sol:{monitor_name(contract.id)}", [run], tmp_path)
    assert "error" in raw[2] and "error" in raw[3]
    kept = [r for i, r in enumerate(raw) if i not in (2, 3)]
    run["steps"] = [s for i, s in enumerate(run["steps"]) if i not in (2, 3)]
    got = S.sol_results([kept], [run], [n.id for n in contract.norms])
    assert S.compare(refs, got) == []


# === Long traces ===================================================================

def _long_trace(n: int) -> list:
    """The robot example at one observation per second for n seconds: the
    assumption throughout(300s, ...) stays open for 300 of them."""
    lines = [json.dumps({"t": 0, "bindings": {"i": 2},
                         "values": {"DISPATCHER.is_operational": True, "ROBOT[2].battery": 80,
                                    "ROBOT[2].status": "Idle"}})]
    for k in range(1, n):
        rec: dict = {"t": k, "values": {"ROBOT[2].battery": 80 - (k % 60)}}
        if k % 50 == 1:
            rec["messages"] = [{"from": "DISPATCHER", "to": "ROBOT[2]",
                                "name": "route_assignment"}]
        if k % 50 == 3:
            rec["messages"] = [{"from": "ROBOT[2]", "to": "DISPATCHER", "name": "ack",
                                "args": ["accepted"]}]
        lines.append(json.dumps(rec))
    rec = json.loads(lines[-1])
    rec["end"] = False
    lines[-1] = json.dumps(rec)
    return lines


@needs_opa
def test_opa_long_trace(tmp_path):
    """Beyond the 16 observations of the other traces: 40, with the
    buffer of the state kept small."""
    sos = parse_file(EXAMPLE)
    lines = _long_trace(40)
    refs = [S.reference(sos, lines)]
    got = S.run_opa(generate_norms_monitor_rego(sos, sos.contracts[0]), PKG, [lines], refs,
                    tmp_path, steps=40)
    assert S.compare(refs, got) == []


@needs_evm
def test_evm_long_trace_has_bounded_gas(tmp_path):
    """320 observations: each observation costs about the same, while the
    300 s window of the assumption is open and after it (the windows are
    scanned once and the values pruned)."""
    sos = parse_file(EXAMPLE)
    lines = _long_trace(320)
    refs, got = _evm(sos, [lines], tmp_path)
    assert S.compare(refs, got) == []
    gas = [r["gas"] for r in _RAW[-1][0] if "gas" in r]
    early = sum(gas[20:70]) / 50
    late = sum(gas[-50:]) / 50
    assert late < 1.25 * early, (early, late)
    assert max(gas[1:]) < 1_500_000, max(gas[1:])


@needs_evm
def test_evm_only_the_reporter_records(tmp_path):
    sos = parse_file(EXAMPLE)
    lines = _lines({"t": 0, "bindings": {"i": 2}}, {"t": 1})
    run = S.sol_run(lines, [])
    run["steps"] = [dict(st, **{"as": 1}) for st in run["steps"]][:1] + run["steps"]
    contract = sos.contracts[0]
    compiled = S.compile_solidity(generate_norms_monitor_sol(sos, contract),
                                  monitor_name(contract.id), tmp_path)
    [raw] = S.run_evm(compiled, f"Monitor.sol:{monitor_name(contract.id)}", [run], tmp_path)
    assert "error" in raw[0]
    assert all("error" not in r for r in raw[1:])


@needs_evm
def test_evm_cases_of_test_norms_eval(norms_eval_cases, tmp_path, monkeypatch):
    """The traces of tests/test_norms_eval.py on the EVM, for each contract
    the Solidity monitor supports."""
    _memoize_parser(monkeypatch)
    groups: dict = {}
    for case in norms_eval_cases["cases"]:
        if case["versions"] or any('"amend"' in l for l in case["lines"]):
            continue
        sos = pickle.loads(base64.b64decode(case["sos"]))
        if not S.six_decimals(case["lines"]):
            continue                    # its host cannot pass such a number
        try:
            contract = select_contract(sos, case["contract"])
            text = generate_norms_monitor_sol(sos, contract)
        except (NormsEvalError, NormsTargetUnsupported):
            continue
        groups.setdefault(text, (sos, contract, []))[2].append(case)
    assert sum(len(g[2]) for g in groups.values()) >= 100
    # CADL_NORMS_EVM_CASES: only the first so many traces (CI)
    limit = int(os.environ.get("CADL_NORMS_EVM_CASES", "0")) or None
    if limit:
        kept: dict = {}
        for text, (sos, contract, cases) in groups.items():
            room = limit - sum(len(g[2]) for g in kept.values())
            if room > 0:
                kept[text] = (sos, contract, cases[:room])
        groups = kept
    steps: list = []
    from cadl.codegen.norms_ir import unsupported_items
    from cadl.codegen.solidity.norms_monitor_sol import solidity_ir
    for k, (text, (sos, contract, cases)) in enumerate(groups.items()):
        # guarantee items the Solidity monitor leaves out (a nested temporal operator)
        skip = {pos - 1 for key, pos, _, _ in unsupported_items(solidity_ir(sos, contract))
                if key == "guarantee"}
        refs = [S.reference(sos, c["lines"], c["contract"], skip=skip) for c in cases]
        compiled = S.compile_solidity(text, monitor_name(contract.id), tmp_path / f"m{k}")
        runs = [S.sol_run(c["lines"], r.get("quantities", [])) for c, r in zip(cases, refs)]
        raw = S.run_evm(compiled, f"Monitor.sol:{monitor_name(contract.id)}", runs,
                        tmp_path / f"m{k}")
        got = S.sol_results(raw, runs, [n.id for n in contract.norms], skip)
        assert S.compare(refs, got, steps) == [], cases[0]["lines"]
    assert len(steps) >= (300 if not limit else limit)


@needs_evm
def test_evm_refuses_a_temporal_name_that_becomes_known_late(tmp_path):
    """A bare name read inside a temporal operator must be given in
    begin(): the scans keep the values they have read, so a name that
    becomes a quantity later would leave them stale. The observation that
    would make it known is refused; with the name given in begin() the
    monitor agrees with norms-eval."""
    sos = _small("      assume:\n        - \"throughout(10s, A.s == flag)\"\n"
                 "      guarantee:\n        - \"throughout(10s, A.s == flag)\"\n")
    lines = _lines({"t": 0, "values": {"A.s": "flag"}}, {"t": 1, "values": {"flag": "flag"}},
                   {"t": 20, "end": True})
    refs = [S.reference(sos, lines)]
    contract = sos.contracts[0]
    compiled = S.compile_solidity(generate_norms_monitor_sol(sos, contract),
                                  monitor_name(contract.id), tmp_path)
    late = S.sol_run(lines, [])                        # flag not given in begin()
    given = S.sol_run(lines, refs[0]["quantities"])
    raw = S.run_evm(compiled, f"Monitor.sol:{monitor_name(contract.id)}", [late, given],
                    tmp_path)
    assert "error" not in raw[0][0] and "error" in raw[0][1]
    got = S.sol_results(raw[1:], [given], [n.id for n in contract.norms])
    assert S.compare(refs, got) == []


# === Exact numbers and times (F.5) ===================================================

from tests.test_norms_eval import _EXACT   # noqa: E402

_M = '"messages": [{"from": "A", "to": "B", "name": "%s"}]'
EXACT_TRACES = [
    ['{"t": 0, "values": {"A.x": 0.1, "A.y": 0.2}}', '{"t": 1, "end": true}'],
    ['{"t": 1700000000.1, %s}' % (_M % "req"),
     '{"t": 1700000000.2, "values": {"A.z": 1}, %s}' % (_M % "ack")],
    ['{"t": 1700000000.1, %s}' % (_M % "req"),
     '{"t": 1700000000.200001, "values": {"A.z": 1}, %s}' % (_M % "ack")],
    ['{"t": 1700000000.123456, "values": {"A.x": 0.000001, "A.y": 0.299999}}',
     '{"t": 1700000000.223456, "values": {"A.z": 1}}', '{"t": 1700000000.223457}'],
]


@needs_opa
def test_opa_numbers_and_times_are_exact(tmp_path):
    """As norms-eval: 0.1 + 0.2 == 0.3, and epoch seconds with fractions
    are compared exactly, also with more digits than a float holds."""
    sos = parse(_EXACT)
    traces = EXACT_TRACES + [[
        '{"t": 1700000000.1, %s}' % (_M % "req"),
        '{"t": 1700000000.2000001, %s}' % (_M % "ack")]]
    refs = [S.reference(sos, l) for l in traces]
    assert [r["steps"][-1]["norms"]["o"][0] for r in refs] == [
        "discharged", "fulfilled", "violated", "discharged", "violated"]
    assert refs[0]["steps"][0]["guarantee"][0] == "pending"
    assert refs[0]["steps"][-1]["guarantee"][0] == "fulfilled"
    got = S.run_opa(generate_norms_monitor_rego(sos, sos.contracts[0]),
                    monitor_package("C"), traces, refs, tmp_path)
    assert S.compare(refs, got) == []


@needs_evm
def test_evm_numbers_and_times_are_exact(tmp_path):
    """With at most six decimals the Solidity monitor agrees too."""
    refs, got = _evm(parse(_EXACT), EXACT_TRACES, tmp_path)
    assert S.compare(refs, got) == []


def test_rego_refuses_division():
    sos = _small("      norms:\n        - {id: n, kind: prohibition, bearer: A, "
                 "content: \"A.x / 2 > 3\"}\n")
    with pytest.raises(NormsTargetUnsupported, match="the operator /"):
        generate_norms_monitor_rego(sos, sos.contracts[0])
    sos = _small("      guarantee:\n        - \"A.x / 2 > 3\"\n        - \"A.x > 3\"\n")
    [m] = norms_monitors(sos, "opa")
    assert m["text"] is not None and [p for _, p, _, _ in m["left_out"]] == [1]


# === Temporal contents: bounded cost per observation (Solidity) ======================

WINDOWS = """\
      norms:
        - {id: w, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(3s, A.x > 5)", deadline: 6s}
        - {id: th, kind: prohibition, bearer: A, content: "throughout(2s, A.x > 8)"}
        - {id: un, kind: obligation, bearer: A, trigger: "B -> A : start",
           content: "until(A.ok == true, A.done == true, 4s)", deadline: 5s}
        - {id: th2, kind: obligation, bearer: A, content: "throughout(1s, A.y == 1)",
           deadline: 8s}
        - {id: wz, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(3s, A.x > 5) OR A.z == 1", deadline: 4s}
        - {id: nt, kind: prohibition, bearer: A, trigger: "B -> A : start",
           content: "NOT throughout(2s, A.x > 2) AND A.ok == true", deadline: 6s}
        - {id: ua, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "until(A.ok == true, A.done == true, 3s) AND A.y == 1", deadline: 5s}
        - {id: nw, kind: obligation, bearer: A, content: "NOT (within(2s, A.x == 0) OR A.z == 1)",
           deadline: 7s}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [w, th, un, th2, wz, nt, ua, nw]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [w, th, un, th2, wz, nt, ua, nw]}}
"""


def _window_trace(rng: random.Random) -> list:
    t, lines = 0.0, []
    for k in range(rng.randint(2, 24)):
        rec: dict = {"t": t}
        values = {}
        for name, choices in (("A.x", [0, 3, 6, 9, 10, None]), ("A.ok", [True, True, False, None]),
                              ("A.done", [False, False, True, None]), ("A.y", [1, 1, 1, 0, None]),
                              ("A.z", [0, 0, 1, None])):
            if rng.random() < (0.8 if k == 0 else 0.35):
                values[name] = rng.choice(choices)
        if values:
            rec["values"] = values
        msgs = [{"from": "B", "to": "A", "name": rng.choice(["go", "start", "pause", "resume"])}
                for _ in range(rng.choice([0, 0, 0, 1, 1, 2]))]
        if msgs:
            rec["messages"] = msgs
        lines.append(json.dumps(rec))
        t = round(t + rng.choice([0, 0.5, 1, 1, 1.5, 2, 3, 4, 7]), 3)
    if rng.random() < 0.3:
        rec = json.loads(lines[-1])
        rec["end"] = False
        lines[-1] = json.dumps(rec)
    return lines


@needs_evm
def test_evm_temporal_contents(tmp_path, monkeypatch):
    """within, throughout and until as contents, with deadlines and
    suspension: the candidates decided from the summaries of their
    windows give what norms-eval gives."""
    _memoize_parser(monkeypatch)
    sos = _small(WINDOWS)
    rng = random.Random(20261201)
    traces = [_window_trace(rng) for _ in range(_count(12))]
    refs, got = _evm(sos, traces, tmp_path)
    assert S.compare(refs, got) == []


@needs_opa
def test_opa_temporal_contents(tmp_path, monkeypatch):
    _memoize_parser(monkeypatch)
    sos = _small(WINDOWS)
    rng = random.Random(20261202)
    traces = [_window_trace(rng) for _ in range(_count(150))]
    refs = [S.reference(sos, l) for l in traces]
    got = S.run_opa(generate_norms_monitor_rego(sos, sos.contracts[0]), monitor_package("C"),
                    traces, refs, tmp_path, steps=24)
    assert S.compare(refs, got) == []
    seen = _seen(refs)
    assert {"fulfilled", "violated", "suspended", "discharged"} <= seen["states"]


@needs_evm
def test_evm_gas_of_a_temporal_content_is_bounded(tmp_path):
    """gas2 of the review: a prohibition with content throughout(1000s, ...)
    at one observation per second. Each observation adds a candidate; the
    cost of an observation stays the same (it grew by 4 x 10^4 gas per
    candidate before)."""
    sos = _small("      norms:\n        - {id: p, kind: prohibition, bearer: A, "
                 "content: \"throughout(1000s, A.x >= 0)\"}\n")
    contract = sos.contracts[0]
    lines = [json.dumps({"t": k, "values": {"A.x": k}}) for k in range(320)]
    compiled = S.compile_solidity(generate_norms_monitor_sol(sos, contract),
                                  monitor_name(contract.id), tmp_path)
    run = S.sol_run(lines, ["A.x"])
    run["steps"] = [s for s in run["steps"] if "finish" not in s]
    [raw] = S.run_evm(compiled, f"Monitor.sol:{monitor_name(contract.id)}", [run], tmp_path)
    gas = [r["gas"] for r in raw]
    assert max(gas[20:]) < 1.05 * min(gas[20:]) and max(gas) < 500_000, (min(gas), max(gas))


# === NOT until, comparisons and two temporal operators (Solidity) ====================

# NOT until(...) is the release on the dual queue; an operator inside a
# comparison gives each candidate its own queue; two operators in one
# content share a segment tree of candidates. Reparations, suspension,
# undefined values and epoch timestamps.
RELEASE = """\
      norms:
        - {id: nu, kind: prohibition, bearer: A, trigger: "B -> A : start",
           content: "NOT until(A.ok == true, A.done == true, 3s)", deadline: 6s,
           on_violation: {severity: Major, reparation: rp}}
        - {id: nt, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "NOT until(A.ok == true, A.done == true, 2s) OR A.z == 1", deadline: 5s}
        - {id: na, kind: obligation, bearer: A,
           content: "NOT (until(A.ok == true, A.done == true, 2500ms) OR A.z == 1)", deadline: 7s}
        - {id: rp, kind: obligation, bearer: A, content: "NOT until(A.ok == true, A.y == 0, 1s)",
           deadline: 3s}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [nu, nt, na, rp]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [nu, nt, na, rp]}}
"""

COMPARE = """\
      norms:
        - {id: ce, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(3s, A.x > 5) == (A.z == 1)", deadline: 5s,
           on_violation: {severity: Major, reparation: cr}}
        - {id: cn, kind: prohibition, bearer: A, content: "throughout(2s, A.x > 2) != true"}
        - {id: cl, kind: obligation, bearer: A, trigger: "B -> A : start",
           content: "until(A.ok == true, A.done == true, 3s) <= A.y", deadline: 6s}
        - {id: ci, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(2s, A.x == 0) IN [A.ok, false]", deadline: 4s}
        - {id: cr, kind: prohibition, bearer: A, content: "NOT until(A.ok == true, A.done == true, 2s) == A.ok",
           deadline: 4s}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [ce, cn, cl, ci, cr]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [ce, cn, cl, ci, cr]}}
"""

TWO = """\
      norms:
        - {id: wa, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(3s, A.x > 5) AND throughout(2s, A.ok == true)", deadline: 5s,
           on_violation: {severity: Major, reparation: tr}}
        - {id: wo, kind: prohibition, bearer: A,
           content: "within(1s, A.x == 0) OR NOT until(A.ok == true, A.done == true, 2s)"}
        - {id: ua, kind: obligation, bearer: A, trigger: "B -> A : start",
           content: "until(A.ok == true, A.done == true, 3s) AND NOT within(2s, A.x > 8) OR A.z == 1",
           deadline: 6s}
        - {id: eq, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(2s, A.x > 5) == throughout(1s, A.y == 1)", deadline: 4s}
        - {id: tr, kind: obligation, bearer: A,
           content: "throughout(1s, A.y == 1) OR within(2s, A.done == true)", deadline: 3s}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [wa, wo, ua, eq, tr]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [wa, wo, ua, eq, tr]}}
"""


# Three and four temporal operators in one content (CadlNormsMulti with
# nodes of one and of five storage words; `fr` rarely holds, so that its
# tree grows over a long trace), and a temporal operator inside
# another one (W_NESTED): within and throughout, negated or not, inner
# windows shorter and longer than the outer ones, with reparation,
# suspension and deadlines.
THREE = """\
      norms:
        - {id: t3, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(3s, A.x > 5) AND throughout(2s, A.ok == true) OR within(1s, A.done == true)",
           deadline: 5s, on_violation: {severity: Major, reparation: tr}}
        - {id: t4, kind: prohibition, bearer: A,
           content: "within(1s, A.x == 0) OR NOT until(A.ok == true, A.done == true, 2s) AND throughout(1s, A.y == 1) OR within(2s, A.z == 1)"}
        - {id: tq, kind: obligation, bearer: A, trigger: "B -> A : start",
           content: "until(A.ok == true, A.done == true, 3s) == (NOT within(2s, A.x > 8)) AND throughout(1s, A.y == 1)",
           deadline: 6s}
        - {id: tr, kind: obligation, bearer: A,
           content: "throughout(1s, A.y == 1) OR within(2s, A.done == true) OR within(0s, A.x == 9)", deadline: 3s}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [t3, t4, tq, tr]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [t3, t4, tq, tr]}}
"""

FOUR = """\
      norms:
        - {id: f4, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(3s, A.x > 5) AND throughout(2s, A.ok == true) OR within(1s, A.done == true) AND NOT until(A.ok == true, A.y == 0, 2s)",
           deadline: 5s, on_violation: {severity: Major, reparation: fp}}
        - {id: fp, kind: prohibition, bearer: A,
           content: "(within(1s, A.x == 0) == throughout(2s, A.y == 1)) OR (within(2s, A.z == 1) AND within(2s, throughout(1s, A.ok == true)))"}
        - {id: fm, kind: obligation, bearer: A, trigger: "B -> A : start",
           content: "throughout(1s, NOT within(1s, A.x > 8)) OR within(2s, A.done == true) AND A.z == 0 OR until(A.ok == true, A.done == true, 3s) AND within(1s, A.y == 0)",
           deadline: 6s}
        - {id: fr, kind: prohibition, bearer: A,
           content: "within(1s, A.x == 9) AND within(2s, A.y == 0) AND within(1s, A.z == 1) AND NOT until(A.ok == true, A.done == true, 2s)"}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [f4, fp, fm, fr]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [f4, fp, fm, fr]}}
"""

NESTED = """\
      norms:
        - {id: wt, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(3s, throughout(1s, A.ok == true))", deadline: 5s,
           on_violation: {severity: Major, reparation: tw}}
        - {id: tw, kind: prohibition, bearer: A, content: "throughout(3s, within(1s, A.x > 5))"}
        - {id: nn, kind: obligation, bearer: A, trigger: "B -> A : start",
           content: "within(2s, NOT within(1s, A.done == true)) OR A.z == 1", deadline: 6s}
        - {id: tt, kind: prohibition, bearer: A, trigger: "B -> A : go",
           content: "NOT throughout(2s, NOT throughout(1s, A.y == 1)) AND within(1s, A.x > 2)",
           deadline: 4s}
        - {id: wl, kind: prohibition, bearer: A, trigger: "B -> A : start",
           content: "within(1s, throughout(3s, A.ok == true))", deadline: 5s}
        - {id: tl, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "throughout(1s, NOT within(3s, A.x > 5))", deadline: 3s}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [wt, tw, nn, tt, wl, tl]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [wt, tw, nn, tt, wl, tl]}}
"""


# Windows of 0 s, inside and outside (ZERO); two nested operators in one
# content, and four operators of which two are nested (MIX).
ZERO = """\
      norms:
        - {id: a, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(0s, throughout(0s, A.ok == true))", deadline: 4s}
        - {id: b, kind: prohibition, bearer: A,
           content: "throughout(0s, NOT within(1s, A.x > 5))"}
        - {id: c, kind: prohibition, bearer: A, trigger: "B -> A : start",
           content: "within(2s, throughout(0s, A.y == 1))", deadline: 3s}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [a, b, c]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [a, b, c]}}
"""

MIX = """\
      norms:
        - {id: m1, kind: obligation, bearer: A, trigger: "B -> A : go",
           content: "within(2s, throughout(1s, A.ok == true)) AND throughout(3s, NOT within(1s, A.x > 5)) OR within(1s, A.done == true) OR A.z == 1",
           deadline: 5s}
        - {id: m2, kind: prohibition, bearer: A, trigger: "B -> A : start",
           content: "(within(1s, NOT throughout(2s, A.y == 1)) == within(2s, A.x > 8)) OR NOT until(A.ok == true, A.done == true, 2s) AND throughout(1s, within(0s, A.z == 1))",
           deadline: 6s}
        - {id: m3, kind: obligation, bearer: A,
           content: "NOT within(3s, NOT NOT throughout(2s, A.ok == true))", deadline: 3s}
        - {id: m4, kind: prohibition, bearer: A, trigger: "B -> A : go",
           content: "throughout(1s, throughout(1s, A.y == 1)) OR within(1s, within(3s, A.x == 9))"}
        - {id: pa, kind: power, bearer: B, content: "B -> A : pause",
           effects: {suspend: [m1, m2, m3, m4]}}
        - {id: re, kind: power, bearer: B, content: "B -> A : resume",
           effects: {resume: [m1, m2, m3, m4]}}
"""


def _epoch_trace(rng: random.Random, length: int | None = None) -> list:
    """Like _window_trace, with values that are often undefined (null),
    and in half of the traces epoch timestamps with fractions of a second."""
    epoch = rng.random() < 0.5
    t = 1767225600 + rng.randint(0, 10 ** 6) / 10 ** 6 if epoch else 0.0
    lines = []
    for k in range(length or rng.randint(2, 24)):
        rec: dict = {"t": t}
        values = {}
        for name, choices in (("A.x", [0, 3, 6, 9, 10, None, None]),
                              ("A.ok", [True, True, False, None, None]),
                              ("A.done", [False, False, True, None]), ("A.y", [1, 1, 0, None]),
                              ("A.z", [0, 0, 1, None])):
            if rng.random() < (0.8 if k == 0 else 0.35):
                values[name] = rng.choice(choices)
        if values:
            rec["values"] = values
        msgs = [{"from": "B", "to": "A", "name": rng.choice(["go", "start", "pause", "resume"])}
                for _ in range(rng.choice([0, 0, 0, 1, 1, 2]))]
        if msgs:
            rec["messages"] = msgs
        lines.append(json.dumps(rec))
        step = rng.choice([0, 0.5, 1, 1, 1.5, 2, 2.5, 3, 4, 7, 0.000001, 0.25])
        t = round(t + step, 6)
    if rng.random() < 0.3:
        rec = json.loads(lines[-1])
        rec["end"] = False
        lines[-1] = json.dumps(rec)
    return lines


def _edge(*recs) -> list:
    return [json.dumps(r) for r in recs]


_GO = [{"from": "B", "to": "A", "name": "go"}]
_START = [{"from": "B", "to": "A", "name": "start"}]
_PAUSE = [{"from": "B", "to": "A", "name": "pause"}]
_RESUME = [{"from": "B", "to": "A", "name": "resume"}]
E0 = 1767225600.25

# Traces at the boundaries of the summaries: p false and q not false at
# the same observation, a window that ends exactly at an observation, a
# decision while the norm is suspended, epoch times.
EDGES = [
    _edge({"t": 0, "values": {"A.ok": True, "A.done": False, "A.x": 3, "A.y": 1, "A.z": 0},
           "messages": _START + _GO},
          {"t": 1, "values": {"A.ok": False, "A.done": True}}, {"t": 2}, {"t": 9}),
    _edge({"t": 0, "values": {"A.ok": True, "A.done": False, "A.x": 3, "A.y": 1, "A.z": 0},
           "messages": _START + _GO},
          {"t": 1, "values": {"A.ok": False, "A.done": None}}, {"t": 2}, {"t": 9}),
    _edge({"t": E0, "values": {"A.ok": True, "A.done": False, "A.x": 3, "A.y": 1, "A.z": 0},
           "messages": _START + _GO},
          {"t": E0 + 1}, {"t": E0 + 2, "values": {"A.ok": False}}, {"t": E0 + 9}),
    _edge({"t": E0, "values": {"A.ok": True, "A.done": False, "A.x": 3, "A.y": 1, "A.z": 0},
           "messages": _START + _GO},
          {"t": E0 + 1}, {"t": E0 + 3}, {"t": E0 + 3.000001}, {"t": E0 + 9}),
    _edge({"t": 0, "values": {"A.ok": True, "A.done": False, "A.x": 3, "A.y": 1, "A.z": 0},
           "messages": _START + _GO},
          {"t": 0.5, "messages": _PAUSE}, {"t": 1, "values": {"A.ok": False, "A.x": 9}},
          {"t": 1.5, "values": {"A.done": True, "A.y": 0}},
          {"t": 2, "messages": _RESUME}, {"t": 9}),
    _edge({"t": 0, "values": {"A.ok": None, "A.done": False, "A.x": 3, "A.y": 1, "A.z": 0},
           "messages": _START + _GO},
          {"t": 1, "values": {"A.ok": True, "A.x": 6}}, {"t": 2, "values": {"A.done": True}},
          {"t": 3, "values": {"A.ok": False, "A.x": None}}, {"t": 9}),
    _edge({"t": 0, "values": {"A.ok": True, "A.done": False, "A.x": 0, "A.y": 1, "A.z": 0},
           "messages": _START + _GO},
          *[{"t": 0.5 * k, "values": {"A.x": [6, 0, 9, 3][k % 4], "A.ok": k % 3 != 0,
                                      "A.y": k % 2}} for k in range(1, 12)],
          {"t": 20}),
    _edge({"t": 0, "values": {"A.ok": None, "A.done": False, "A.x": 3, "A.y": 1, "A.z": 0},
           "messages": _START + _GO},
          {"t": 0.5, "messages": _PAUSE}, {"t": 1, "values": {"A.ok": True}},
          {"t": 2, "values": {"A.done": True}},
          {"t": 2.5, "values": {"A.x": 9}, "messages": _RESUME}, {"t": 9}),
]


TEMPORAL_SETS = [("release", RELEASE, 20261301), ("compare", COMPARE, 20261302),
                 ("two", TWO, 20261303), ("three", THREE, 20261304), ("four", FOUR, 20261305),
                 ("nested", NESTED, 20261306), ("zero", ZERO, 20261307), ("mix", MIX, 20261308)]


@needs_evm
@pytest.mark.parametrize("name,blocks,seed", TEMPORAL_SETS, ids=[s[0] for s in TEMPORAL_SETS])
def test_evm_more_temporal_contents(name, blocks, seed, tmp_path, monkeypatch):
    """The EVM gives what norms-eval gives, observation by observation."""
    _memoize_parser(monkeypatch)
    sos = _small(blocks)
    rng = random.Random(seed)
    # and a few long traces (on the EVM only: the OPA driver unrolls 24
    # steps), so that the tree of two operators has several levels
    traces = EDGES + [_epoch_trace(rng) for _ in range(_count(16))] \
        + [_epoch_trace(rng, 70) for _ in range(3)]
    refs, got = _evm(sos, traces, tmp_path)
    assert S.compare(refs, got) == []


@needs_opa
@pytest.mark.parametrize("name,blocks,seed", TEMPORAL_SETS, ids=[s[0] for s in TEMPORAL_SETS])
def test_opa_more_temporal_contents(name, blocks, seed, tmp_path, monkeypatch):
    """The same contents on the Rego monitor (unchanged), as a reference."""
    _memoize_parser(monkeypatch)
    sos = _small(blocks)
    rng = random.Random(seed)
    traces = EDGES + [_epoch_trace(rng) for _ in range(_count(150))]
    refs = [S.reference(sos, l) for l in traces]
    got = S.run_opa(generate_norms_monitor_rego(sos, sos.contracts[0]), monitor_package("C"),
                    traces, refs, tmp_path, steps=24)
    assert S.compare(refs, got) == []
    seen = _seen(refs)
    assert {"fulfilled", "violated", "suspended"} <= seen["states"]


GAS_CONTENTS = ["NOT until(A.x >= 0, A.x < 0, 1000s)", "throughout(1000s, A.x >= 0) == true",
                "within(1000s, A.x < 0) OR throughout(1000s, A.x >= 0)",
                "within(1000s, A.x >= 0) AND throughout(1000s, A.x >= 0)",
                "within(1000s, A.x < 0) OR throughout(1000s, A.x >= 0) OR within(1000s, A.x < -1)",
                "within(1000s, A.x < 0) OR throughout(1000s, A.x >= 0) OR within(1000s, A.x < -1) "
                "OR throughout(1000s, A.x >= -1)",
                "within(1000s, throughout(500s, A.x < 0))", "throughout(1000s, within(10s, A.x < 0))"]
# the largest gas of an observation in these tests (four operators: nodes of five words)
GAS_MAX = {GAS_CONTENTS[4]: 1_500_000, GAS_CONTENTS[5]: 3_000_000}


@needs_evm
@pytest.mark.parametrize("content", GAS_CONTENTS)
def test_evm_gas_of_more_temporal_contents_is_bounded(content, tmp_path):
    """NOT until, a comparison, two, three and four temporal operators,
    and nested ones: 320 observations at one per second into windows of
    1000 s, each a candidate that stays undecided (in the last, the inner
    values decided from the eleventh second on decide the candidates
    false one at a time). The mean cost of the
    last 50 observations stays within 15 % of that of observations 20 to
    70; with two operators the cost grows with the depth of the tree, a
    step at each power of two of the candidates."""
    sos = _small("      norms:\n        - {id: p, kind: prohibition, bearer: A, "
                 f"content: \"{content}\"}}\n")
    contract = sos.contracts[0]
    lines = [json.dumps({"t": k, "values": {"A.x": k}}) for k in range(320)]
    compiled = S.compile_solidity(generate_norms_monitor_sol(sos, contract),
                                  monitor_name(contract.id), tmp_path)
    run = S.sol_run(lines, ["A.x"])
    run["steps"] = [s for s in run["steps"] if "finish" not in s]
    [raw] = S.run_evm(compiled, f"Monitor.sol:{monitor_name(contract.id)}", [run], tmp_path)
    gas = [r["gas"] for r in raw]
    early, late = sum(gas[20:70]) / 50, sum(gas[-50:]) / 50
    assert late < 1.15 * early and max(gas) < GAS_MAX.get(content, 1_000_000), (early, late, max(gas))


# === The range of exact numbers, exact deadlines, runtime size =======================

RANGE_ITEMS = ("      guarantee:\n        - \"A.x > 0\"\n        - \"A.x == 0.0000001\"\n"
               "        - \"A.x * 10 == 0.000001\"\n        - \"A.x + 1000 > 1000\"\n")


@needs_opa
@pytest.mark.parametrize("number", ["1e-7", "1E-7", "-0.0", "1e400", "1e-400", "1e-700",
                                    "123456789012345678901234567890.5", "0e5000",
                                    "1" + "0" * 300, "1" + "0" * 400, "1e308",
                                    "1.7976931348623157e308", "-1.7976931348623157e308",
                                    "1.7976931348623158e308", "1e309", "1" + "0" * 308])
def test_opa_range_of_exact_numbers(number, tmp_path):
    """p12 of the review: the scales of 10^-309 and below used to compare as
    equal in Rego. Inside the range of F.5 OPA agrees with norms-eval,
    outside it both refuse the observation."""
    sos = _small(RANGE_ITEMS)
    lines = ['{"t": 0, "values": {"A.x": %s}}' % number, '{"t": 1e0, "end": true}']
    refs = [S.reference(sos, lines)]
    got = S.run_opa(generate_norms_monitor_rego(sos, sos.contracts[0]), monitor_package("C"),
                    [lines], refs, tmp_path)
    assert S.compare(refs, got) == []


@needs_opa
@pytest.mark.parametrize("number", ["1e-5", "1e-700", "1.7976931348623157e308", "1e309",
                                    "1" + "0" * 308, "1" + "0" * 309])
def test_opa_range_of_request_and_bindings(number, tmp_path):
    """p15 of the review: the request and the bindings are checked like the
    observation, whether they come with the first observation or as
    input.request / input.bindings."""
    sos = _small("      guarantee:\n        - \"request.x > 0\"\n        - \"A.z == 1\"\n")
    rego = generate_norms_monitor_rego(sos, sos.contracts[0])
    lines = ['{"t": 0, "request": {"x": %s}, "values": {"A.z": 1}}' % number,
             '{"t": 1, "end": true}']
    refs = [S.reference(sos, lines)]
    got = S.run_opa(rego, monitor_package("C"), [lines], refs, tmp_path)
    assert S.compare(refs, got) == []
    refused = "error" in refs[0]
    assert refused == (number in ("1e-700", "1e309", "1" + "0" * 309))
    # a binding is an integer: the other numbers are refused for that
    for key in ("request", "bindings") if number.isdigit() else ("request",):
        tmp = tmp_path / key
        tmp.mkdir()
        (tmp / "m.rego").write_text(rego, encoding="utf-8")
        (tmp / "in.json").write_text(
            '{"state": null, "observation": {"t": 0, "values": {"A.z": 1}, "end": true}, '
            '"%s": {"x": %s}}' % (key, number), encoding="utf-8")
        proc = subprocess.run([S.opa_binary(), "eval", "-d", str(tmp / "m.rego"), "-i",
                               str(tmp / "in.json"), "--format", "json",
                               f"data.{monitor_package('C')}.result"],
                              capture_output=True, text=True)
        out = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
        assert ("outside the range" in (out.get("error") or "")) == refused, (key, out)


def test_rego_refuses_a_product_beyond_its_scales():
    product = " * ".join(["A.x"] * 11)
    sos = _small(f"      norms:\n        - {{id: n, kind: prohibition, bearer: A, "
                 f"content: \"{product} > 1\"}}\n")
    with pytest.raises(NormsTargetUnsupported, match="more than 4095 decimals"):
        generate_norms_monitor_rego(sos, sos.contracts[0])


DEADLINE = ("      norms:\n        - {id: o, kind: obligation, bearer: A, trigger: \"B -> A : req\",\n"
            "           content: \"B -> A : ack\", deadline: 0.0005}\n")


@needs_opa
def test_opa_numeric_deadline_is_exact(tmp_path):
    sos = _small(DEADLINE)
    m = '"messages": [{"from": "B", "to": "A", "name": "%s"}]'
    traces = [['{"t": 0, %s}' % (m % "req"), '{"t": %s, %s}' % (at, m % "ack")]
              for at in ("0.0005", "0.0006")]
    refs = [S.reference(sos, l) for l in traces]
    assert [r["steps"][-1]["norms"]["o"][0] for r in refs] == ["fulfilled", "violated"]
    got = S.run_opa(generate_norms_monitor_rego(sos, sos.contracts[0]), monitor_package("C"),
                    traces, refs, tmp_path)
    assert S.compare(refs, got) == []


@needs_evm
def test_evm_numeric_deadline_is_exact(tmp_path):
    m = '"messages": [{"from": "B", "to": "A", "name": "%s"}]'
    traces = [['{"t": 0, %s}' % (m % "req"), '{"t": %s, %s}' % (at, m % "ack")]
              for at in ("0.0005", "0.0006")]
    refs, got = _evm(_small(DEADLINE), traces, tmp_path)
    assert S.compare(refs, got) == []


@needs_solc
def test_runtime_estimate_is_above_solc(tmp_path):
    """The runtime code does not depend on the CADL contract; cadl codegen
    refuses a monitor whose estimate exceeds EIP-170, and the estimate is
    above what solc writes with 1 and with 200 runs."""
    from cadl.codegen.solidity.norms_monitor_sol import RUNTIME_ESTIMATE, RUNTIME_LIMIT
    sos = parse_file(RICH)
    text = generate_norms_monitor_sol(sos, sos.contracts[0])
    assert "pragma solidity >=0.8.24 <=0.8.37;" in text
    for runs in (1, 200):
        compiled = S.compile_solidity(text, "CNormsMonitor", tmp_path / str(runs), runs=runs)
        for name, c in compiled.items():
            if not c["bin"]:
                continue
            short = name.split(":")[1]
            key = "monitor" if short == "CNormsMonitor" else short
            assert c["runtime"] <= RUNTIME_ESTIMATE[key] <= RUNTIME_LIMIT, (runs, short)
