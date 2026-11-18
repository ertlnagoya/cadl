"""`cadl agents-run` and the gateways follow the current regime, from
cadl 0.3.36 (Appendix A, A.7.1, "Runs"; Appendix F, F.5, "Contracts not
in force"; Appendix K, K.11, "Versions and regimes").

A trace line names the current regime with the key ``regime``, as a trace
of `cadl norms-eval` does; before the first one the run is in the initial
regime. A contract that the current regime does not have in force governs
no message (the decision of `cadl agents-decide` on the same input, which
carries the regime), and its instances see nothing: their deadlines and
windows are measured on the clock of the contract, and an instance that
ends while its contract is not in force is settled on the stopped clock.
A gateway takes the regime of its first line from --init and later
changes from its inbox (`cadl mcp-regime`).

The differential tests replay random traces through the runtime and
compare every decision with `agents.decide` and the verdicts of every
contract instance with `cadl norms-eval` on the observations that the
enforcement point made (the messages that took effect), with the regimes
of the trace."""

from __future__ import annotations

import json
import random
import textwrap
from pathlib import Path

import pytest

from cadl import agents_runtime as AR
from cadl.agents import build_policy, decide, governs, parse_concrete_actor
from cadl.agents_runtime import load_run_trace, parse_run_trace, run
from cadl.audit_log import read_records, verify
from cadl.cli import main
from cadl.norms_eval import (
    NormsEvalError, TimeBase, TraceError, evaluate, evaluate_files, load_version,
    parse_trace_regimes, regime_bindings,
)
from cadl.parser import parse
from cadl.type_checker import type_check

from .test_agents_extension import RICH, norms, perm, proh, source
from .test_agents_runtime import DELEG, HOLDS, aim, m, random_rows, trace

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "agents_tool_governance.cadl"

# --- files ------------------------------------------------------------------------------

TIMED = source(
    """
    lifecycle:
      states: [Open, Busy, Closed]
      initial: Open
      terminal: [Closed]
      transitions:
        - { id: begin, from: Open, to: Busy, on: "OP -> AG : go" }
        - { id: finish, from: Busy, to: Closed, on: "OP -> AG : done" }
    counts_as:
      - { when: "OP -> AG : approve", creates: approval }
      - { when: "OP -> AG : reject", creates: rejection }
    """,
    norms(
        perm("p_write", "AG", "AG -> T : write"),
        proh("no_wipe", "AG", "AG -> T : wipe", "on_denied: { escalate_to: OP, timeout: 40s }"),
        """
        - id: must_write
          kind: obligation
          bearer: AG
          trigger: "OP -> AG : go"
          deadline: 30s
          content: "AG -> T : write"
        - id: hushed
          kind: prohibition
          bearer: AG
          trigger: "OP -> AG : hush"
          deadline: 20s
          content: "AG -> T : read"
        - id: hot
          kind: prohibition
          bearer: AG
          trigger: "T.load > 5"
          until: "T.load <= 5"
          content: "AG -> T : wipe"
        """,
    ),
)
TIMED = TIMED.rstrip("\n") + "\n" + textwrap.indent(textwrap.dedent("""
    - id: C2
      parties: [SUP, AG, T]
      enforcement: {}
      counts_as:
        - { when: "SUP -> AG : approve", creates: approval }
        - { when: "SUP -> AG : reject", creates: rejection }
      norms:
        - { id: p_read2, kind: permission, bearer: AG, content: "AG -> T : read" }
        - { id: p_write2, kind: permission, bearer: AG, content: "AG -> T : write" }
        - id: answer
          kind: obligation
          bearer: AG
          trigger: "SUP -> AG : ask"
          deadline: 45s
          content: "AG -> SUP : reply"
        - id: no_wipe2
          kind: prohibition
          bearer: AG
          content: "AG -> T : wipe"
          on_denied: { escalate_to: SUP, timeout: 1min }
    """).strip("\n") + "\n", "    ")

# Three regimes: STORM takes C out of force, DRY takes C2 out of force.
REGIMES = """
transitions:
  - { from: NORMAL, to: STORM, condition: "wind >= 20" }
  - { from: STORM, to: DRY, condition: "wind < 20" }
  - { from: DRY, to: NORMAL, condition: "wind < 10" }
regimes:
  STORM:
    contracts: { suspended: [%s] }
  DRY:
    contracts: { active: [%s] }
"""
NAMES = ("NORMAL", "STORM", "DRY")


def with_regimes(text: str, storm: str, dry: str | None = None) -> str:
    """The file with the three regimes; STORM does not have `storm` in force
    and DRY has only `dry` (all contracts when None)."""
    block = REGIMES % (storm, dry or storm)
    if dry is None:
        block = block.replace("  DRY:\n    contracts: { active: [%s] }\n" % storm, "")
    return text.rstrip("\n") + "\n" + textwrap.indent(block.strip("\n") + "\n", "  ")


def checked(text: str):
    sos = parse(text)
    res = type_check(sos)
    assert res.ok, [str(e) for e in res.errors]
    return sos


TIMED_R = with_regimes(TIMED, "C", "C")
SOURCES = {
    "timed": (TIMED, TIMED_R),
    "rich": (RICH, with_regimes(RICH, "C1", "C1")),
    "holds": (HOLDS, with_regimes(HOLDS, "C")),
    "deleg": (DELEG, with_regimes(DELEG, "C")),
    "example": (EXAMPLE.read_text(encoding="utf-8"),
                with_regimes(EXAMPLE.read_text(encoding="utf-8"), "ASSISTED_REPORTING")),
}
_PARSED: dict = {}


def files(name: str):
    if name not in _PARSED:
        plain, regimes = SOURCES[name]
        _PARSED[name] = (checked(plain), checked(regimes))
    return _PARSED[name]


def go(sos, lines, **kw) -> dict:
    return run(sos, parse_run_trace([json.dumps(x) if isinstance(x, dict) else x
                                     for x in lines]), **kw)


def steps(r: dict) -> list:
    return [(s["message"]["name"], s["result"]["decision"], s["result"]["reason"],
             s["input"].get("regime")) for s in r["steps"]]


def norm_states(r: dict, contract: str = "C") -> dict:
    inst = next(i for i in r["instances"] if i["contract"] == contract)
    return {n["id"]: (n["state"], n["verdict"]) for n in inst["norms"]}


# === Scenarios ================================================================================

class TestDecisions:
    def test_a_contract_out_of_force_governs_nothing(self):
        sos = files("timed")[1]
        r = go(sos, [
            {"t": 0, "values": {"T.load": 0}, "messages": [m("AG", "T", "wipe")]},
            {"t": 1, "regime": "STORM", "messages": [m("AG", "T", "wipe"), m("AG", "T", "write")]},
            {"t": 2, "regime": "DRY", "messages": [m("AG", "T", "wipe")]},
            {"t": 3, "regime": "NORMAL", "messages": [m("SUP", "AG", "hello")]}])
        assert steps(r) == [
            ("wipe", "deny", "prohibition", "NORMAL"),       # both contracts prohibit
            ("wipe", "escalate", "prohibition", "STORM"),    # C2 alone: escalate to SUP
            ("write", "allow", "permission", "STORM"),       # C2 permits it
            ("wipe", "escalate", "prohibition", "DRY"),      # C alone: escalate to OP
            ("hello", "allow", "default", "NORMAL"),
        ]
        policy = build_policy(sos, regimes=True)
        assert all(decide(policy, s["input"]) == s["result"] for s in r["steps"])
        # the audit record lists the instances of the contracts in force only
        recs = [x for x in r["audit"] if "decision" in x]
        assert [x["instances"] for x in recs][1] == [{"contract": "C2", "instance": "default"}]

    def test_initial_regime_and_a_first_line_that_names_one(self):
        sos = files("timed")[1]
        r = go(sos, [{"t": 0, "messages": [m("AG", "T", "write")]}])
        assert steps(r) == [("write", "allow", "permission", "NORMAL")]
        r = go(sos, [{"t": 0, "regime": "STORM", "messages": [m("AG", "T", "write")]}])
        assert steps(r) == [("write", "allow", "permission", "STORM")]

    def test_a_file_whose_regimes_keep_every_enforced_contract(self):
        # the input has no regime and the run is that of the file without regimes
        text = with_regimes(TIMED, "C").replace("suspended: [C]", "suspended: []")
        sos = checked(text)
        lines = [{"t": 0, "messages": [m("AG", "T", "wipe")]},
                 {"t": 5, "regime": "STORM", "messages": [m("AG", "T", "read")]}]
        got = go(sos, lines)
        ref = go(files("timed")[0], [{k: v for k, v in x.items() if k != "regime"}
                                     for x in lines])
        assert got["steps"] == ref["steps"] and got["audit"] == ref["audit"]
        assert "regime" not in got["steps"][0]["input"]

    def test_hold_while_the_contract_leaves_force(self):
        # A hold of C stays held while C is not in force: the approval is a
        # message that C does not govern then, so it approves nothing; the
        # hold expires at its deadline (G.6).
        sos = files("timed")[1]
        r = go(sos, [
            {"t": 0, "values": {"T.load": 0},
             "messages": [m("AG", "T", "wipe")]},                    # C and C2: deny
            {"t": 1, "regime": "DRY", "messages": [m("AG", "T", "wipe")]},   # C: escalate
            {"t": 2, "regime": "STORM", "messages": [m("OP", "AG", "approve", "h-1")]},
            {"t": 50}])
        assert [x[1] for x in steps(r)] == ["deny", "escalate", "allow"]
        assert r["holds"][0]["state"] == "expired"
        assert r["releases"] == []


class TestClock:
    def test_deadline_stops_while_not_in_force(self):
        sos = files("timed")[1]
        go_ = {"t": 0, "values": {"T.load": 0}, "messages": [m("OP", "AG", "go")]}
        # 10 s in force, 100 s not in force, then the write 15 s later: 25 s
        r = go(sos, [go_, {"t": 10, "regime": "STORM"}, {"t": 110, "regime": "NORMAL"},
                     {"t": 125, "messages": [m("AG", "T", "write")]}])
        assert norm_states(r)["must_write"] == ("fulfilled", "fulfilled")
        # 35 s on the clock of C: violated, at the time of the trace t=130
        r = go(sos, [go_, {"t": 10, "regime": "STORM"}, {"t": 110, "regime": "NORMAL"},
                     {"t": 135, "messages": [m("AG", "T", "write")]}])
        assert norm_states(r)["must_write"] == ("violated", "violated")
        def reason(res):
            return next(n["reason"] for n in res["instances"][0]["norms"]
                        if n["id"] == "must_write")
        assert reason(r) == "deadline of 30s passed at t=130 without the content"
        # without regimes: at t=30
        plain = go(files("timed")[0], [go_, {"t": 10}, {"t": 110},
                                       {"t": 125, "messages": [m("AG", "T", "write")]}])
        assert reason(plain) == "deadline of 30s passed at t=30 without the content"

    def test_end_while_not_in_force_is_settled_on_the_stopped_clock(self):
        sos = files("timed")[1]
        r = go(sos, [{"t": 0, "messages": [m("OP", "AG", "go")]},
                     {"t": 20, "regime": "STORM"}, {"t": 500}])
        must = next(n for n in r["instances"][0]["norms"] if n["id"] == "must_write")
        # 20 s on the clock when C left force: before the deadline of 30 s
        assert (must["state"], must["reason"]) == \
            ("discharged", "contract instance ended before the deadline")
        assert r["instances"][0]["end"] == 500
        plain = go(files("timed")[0], [{"t": 0, "messages": [m("OP", "AG", "go")]},
                                       {"t": 20}, {"t": 500}])
        assert norm_states(plain)["must_write"] == ("violated", "violated")

    def test_values_and_states_while_not_in_force(self):
        # A terminal state ends the instance also while C is not in force;
        # another state change is not seen (F.5).
        sos = files("timed")[1]
        r = go(sos, [{"t": 0, "regime": "STORM"}, {"t": 1, "state": "Busy"},
                     {"t": 2, "regime": "NORMAL", "messages": [m("AG", "T", "write")]},
                     {"t": 3, "regime": "STORM", "state": "Closed"},
                     {"t": 4, "regime": "NORMAL", "messages": [m("AG", "T", "write")]}])
        c = next(i for i in r["instances"] if i["contract"] == "C")
        assert (c["state"], c["running"], c["end"]) == ("Closed", False, 3)
        assert steps(r)[-1][1:3] == ("deny", "no_instance")    # C governs, has no instance
        with pytest.raises(TraceError, match="is not a state of the lifecycle of C"):
            go(sos, [{"t": 0, "regime": "STORM"}, {"t": 1, "state": "Nowhere"}])


class TestRefusals:
    def test_unknown_regime_and_a_file_without_regimes(self):
        with pytest.raises(TraceError, match="'regime' must name a regime of the file "
                                             r"\(NORMAL, STORM, DRY\), got 'WINDY'"):
            go(files("timed")[1], [{"t": 0, "regime": "WINDY"}])
        with pytest.raises(TraceError, match="the file has no regimes: block"):
            go(files("timed")[0], [{"t": 0, "regime": "STORM"}])

    def test_amend_with_regimes_is_refused(self, tmp_path):
        (tmp_path / "v2.cadl").write_text(TIMED_R, encoding="utf-8")
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps({"t": 0}) + "\n"
                        + json.dumps({"t": 1, "amend": "v2.cadl"}) + "\n")
        with pytest.raises(NormsEvalError, match="cadl does not apply regime bindings and "
                                                 "amendments with 'amend' together"):
            run(files("timed")[1], load_run_trace(path))

    def test_build_policy_without_the_regime_still_refuses(self):
        with pytest.raises(ValueError, match="build the policy with regimes=True"):
            build_policy(files("timed")[1])


class TestCli:
    def test_agents_run_and_its_audit_log(self, tmp_path, capsys):
        f = tmp_path / "r.cadl"
        f.write_text(TIMED_R, encoding="utf-8")
        t = tmp_path / "t.jsonl"
        t.write_text("\n".join(json.dumps(x) for x in [
            {"t": 0, "regime": "STORM", "messages": [m("AG", "T", "wipe")]},
            {"t": 3, "regime": "NORMAL", "messages": [m("AG", "T", "wipe")]}]) + "\n")
        assert main(["agents-run", str(f), "--trace", str(t), "--format", "json",
                     "--audit", str(tmp_path / "a.jsonl")]) == 0
        out = json.loads(capsys.readouterr().out)
        assert [s["result"]["decision"] for s in out["steps"]] == ["escalate", "deny"]
        assert verify(tmp_path / "a.jsonl")["valid"]


# === Differential: decisions against agents-decide, verdicts against norms-eval ================

def random_regime_trace(name: str, seed: int, length: int = 120) -> list[str]:
    """A random trace of a file of SOURCES with the key `regime` on some
    lines; for TIMED also values, states and deadlines that pass."""
    plain, sos = files(name)
    rng = random.Random(seed)
    if name == "timed":
        rows, t = [], 0
        vocab = [("OP", "AG", "go"), ("OP", "AG", "hush"), ("AG", "T", "write"),
                 ("AG", "T", "wipe"), ("AG", "T", "read"), ("SUP", "AG", "ask"),
                 ("AG", "SUP", "reply"), ("OP", "AG", "approve"), ("SUP", "AG", "approve"),
                 ("OP", "AG", "reject"), ("OP", "AG", "hello")]
        for k in range(length):
            t += rng.choice([0, 1, 2, 5, 10, 15, 25, 40, 70])
            line: dict = {"t": t}
            if k == 0:
                line["values"] = {"T.load": 0}
            n = rng.choice([0, 1, 1, 1, 2])
            if n:
                line["messages"] = [m(*rng.choice(vocab)) for _ in range(n)]
            if rng.random() < 0.2:
                line.setdefault("values", {})["T.load"] = rng.randint(0, 9)
            x = rng.random()
            if x < 0.006:
                line["state"] = "Closed"
            elif x < 0.06:
                line["state"] = rng.choice(["Open", "Busy"])
            rows.append(line)
        lines = aim(sos, [json.dumps(x) for x in rows], seed)
    else:
        lines = aim(plain, trace(*random_rows(plain, seed, length), bindings={"i": 2}), seed)
    names = [r for r in NAMES if r in regime_bindings(sos).regimes]
    out = []
    for x in lines:
        rec = json.loads(x)
        if rng.random() < 0.15:
            rec["regime"] = rng.choice(names)
        out.append(json.dumps(rec))
    return out


class _Recorder:
    """The observations of the enforcement point, in order: (line, message
    or None, time). Recorded where the runtime makes them, before it asks
    which contract instances see them."""

    def __init__(self, monkeypatch):
        self.calls: list = []
        original = AR.AgentsRuntime._observe

        def observe(rt, msg, t, exempt=None, amend=None):
            self.calls.append((rt._lineno, msg))
            return original(rt, msg, t, exempt, amend)
        monkeypatch.setattr(AR.AgentsRuntime, "_observe", observe)


def reference_trace(lines: list[str], calls: list, policy: dict, cid: str) -> list[str]:
    """The trace of `cadl norms-eval` for the instance `default` of a
    contract: per line its time, values and regime, each message that took
    effect and that the contract governs (whatever the regime: norms-eval
    leaves out what the contract does not see), and the state or the end."""
    contract = next(c for c in policy["contracts"] if c["id"] == cid)
    recs = [json.loads(x) for x in lines]
    out: list[dict] = []
    done: set = set()

    def close(k: int) -> None:
        rec = recs[k - 1]
        last = k == len(recs)
        if "state" in rec or last:
            o: dict = {"t": rec["t"]}
            if "state" in rec:
                o["state"] = rec["state"]
            if last:
                o["end"] = True
            out.append(o)

    previous = None
    for k, msg in calls:
        if previous is not None and k != previous:
            close(previous)
        previous = k
        rec = recs[k - 1]
        if msg is None and k not in done:
            done.add(k)
            o = {"t": rec["t"]}
            for key in ("bindings", "values", "regime"):
                if key in rec:
                    o[key] = rec[key]
            out.append(o)
        elif msg is not None:
            s, r = parse_concrete_actor(msg["from"]), parse_concrete_actor(msg["to"])
            if s and r and governs(policy, contract, s, r, msg["name"]):
                out.append({"t": rec["t"], "messages": [dict(msg)]})
    close(previous)
    return [json.dumps(x) for x in out]


def compare_norms(result: dict, ref: dict, cid: str) -> list:
    """The norms whose state or verdict differ. `excused` (G.5: the
    contract itself denied or held the content) is `violated` for
    norms-eval; a prohibition that is a ground of a released hold is not
    violated by the released message at the boundary (G.6) and is left out."""
    inst = next(i for i in result["instances"] if i["contract"] == cid
                and i["instance"] == "default")
    released = {g["norm"] for rel in result["releases"] if rel["released"]
                for g in rel["grounds"] if g["contract"] == cid}
    bad = []
    assert len(inst["norms"]) == len(ref["norms"])
    for a, b in zip(inst["norms"], ref["norms"]):
        assert a["id"] == b["id"]
        if a["id"] in released:
            continue
        verdict = a["verdict"]
        if verdict == "excused" and "(G.5)" in (a["reason"] or ""):
            verdict = "violated"
        if (a["state"], verdict) != (b["state"], b["verdict"]):
            bad.append((a["id"], a["state"], a["verdict"], b["state"], b["verdict"]))
    return bad


def expected_regimes(lines: list[str], initial: str) -> list:
    """The current regime at each message decision, from the trace alone."""
    out, current = [], initial
    for x in lines:
        rec = json.loads(x)
        current = rec.get("regime", current)
        out += [current] * len(rec.get("messages", []))
    return out


RUNS = [("timed", s) for s in range(220)] + [(n, 1000 + s) for n in ("rich", "holds",
                                                                     "deleg", "example")
                                             for s in range(45)]
TALLY = {"runs": 0, "instances": 0, "changed": 0}


@pytest.mark.parametrize("name,seed", RUNS)
def test_runtime_with_regimes_against_decide_and_norms_eval(name, seed, monkeypatch):
    plain, sos = files(name)
    lines = random_regime_trace(name, seed)
    rec = _Recorder(monkeypatch)
    result = run(sos, parse_run_trace(lines))
    monkeypatch.undo()
    policy = build_policy(sos, regimes=True)
    bindings = regime_bindings(sos)
    # every decision is that of agents-decide on its input, with the regime
    # that the trace names
    assert [s["input"]["regime"] for s in result["steps"]] == \
        expected_regimes(lines, bindings.initial)
    for s in result["steps"]:
        assert decide(policy, s["input"]) == s["result"]
    # every instance has the verdicts of norms-eval on what it saw
    for c in policy["contracts"]:
        ref_lines = reference_trace(lines, rec.calls, policy, c["id"])
        try:
            ref = evaluate(sos, parse_trace_regimes(ref_lines, bindings=bindings), c["id"])
        except NormsEvalError as e:
            assert "is not in force at any observation" in str(e)
            continue
        assert compare_norms(result, ref, c["id"]) == []
        blind = evaluate(plain, parse_trace_regimes(
            [json.dumps({k: v for k, v in json.loads(x).items() if k != "regime"})
             for x in ref_lines]), c["id"])
        TALLY["instances"] += 1
        TALLY["changed"] += [(n["state"], n["verdict"]) for n in blind["norms"]] != \
            [(n["state"], n["verdict"]) for n in ref["norms"]]
    TALLY["runs"] += 1


def test_the_regimes_changed_verdicts():
    """Run after the differential test: the regimes changed the verdicts of
    a good part of the instances (the projection is exercised)."""
    if TALLY["runs"] < len(RUNS):
        pytest.skip("the differential test did not run in this session")
    assert TALLY["changed"] >= TALLY["instances"] // 5, TALLY


# === Versions and regimes (K.11) ===================================================================

def _versioned(tmp: Path) -> tuple[Path, Path]:
    """TIMED with regimes as version 1 and version 2 (period 2): other
    deadlines, and STORM takes both contracts out of force."""
    text = TIMED_R.replace("    - agents: 0.1\n", "    - agents: 0.1\n    - amendment: 0.1\n")
    text = text.replace("sos:\n  name: T\n", 'sos:\n  name: T\n  version: "1"\n')
    v1 = text.rstrip("\n") + "\n  amendment:\n    transitional: { default: switch }\n"
    v2 = text.replace('version: "1"', 'version: "2"').replace("deadline: 30s", "deadline: 50s")
    v2 = v2.replace("deadline: 20s", "deadline: 8s").replace("deadline: 45s", "deadline: 25s")
    v2 = v2.replace("suspended: [C]", "suspended: [C, C2]")
    v2 = v2.rstrip("\n") + '\n  amendment:\n    previous: "1"\n    effective: { period: 2 }\n'
    p1, p2 = tmp / "v1.cadl", tmp / "v2.cadl"
    p1.write_text(v1, encoding="utf-8")
    p2.write_text(v2, encoding="utf-8")
    return p1, p2


@pytest.mark.parametrize("seed", range(120))
def test_versions_and_regimes_against_norms_eval(tmp_path, seed, monkeypatch):
    p1, p2 = _versioned(tmp_path)
    s1, s2 = load_version(p1), load_version(p2)
    base = TimeBase(100)
    lines = random_regime_trace("timed", 5000 + seed, 60)
    rec = _Recorder(monkeypatch)
    result = run(s1, parse_run_trace(lines), run_versions=[s2], base=base)
    monkeypatch.undo()
    for s in result["steps"]:
        live = s1 if s["time"] < 100 else s2
        assert decide(build_policy(live, regimes=True), s["input"]) == s["result"]
    policy = build_policy(s1, regimes=True)
    for c in policy["contracts"]:
        ref_lines = reference_trace(lines, rec.calls, policy, c["id"])
        path = tmp_path / f"{c['id']}.jsonl"
        path.write_text("\n".join(ref_lines) + "\n", encoding="utf-8")
        try:
            ref = evaluate_files(p1, path, c["id"], [p2], base)
        except NormsEvalError as e:
            assert "is not in force at any observation" in str(e)
            continue
        assert compare_norms(result, ref, c["id"]) == []


def test_version_while_not_in_force_reaches_the_instance_on_its_return(tmp_path):
    p1, p2 = _versioned(tmp_path)
    s1, s2 = load_version(p1), load_version(p2)
    # go at t=0 (deadline 30 s in version 1); C is out of force from t=10 to
    # t=300; version 2 (deadline 50 s) takes effect at t=100, while C is not
    # in force, and reaches the instance at t=300. 10 + 35 = 45 s on the clock.
    lines = [{"t": 0, "values": {"T.load": 0}, "messages": [m("OP", "AG", "go")]},
             {"t": 10, "regime": "STORM"},
             {"t": 150}, {"t": 300, "regime": "NORMAL"},
             {"t": 335, "messages": [m("AG", "T", "write")]}]
    r = go(s1, lines, run_versions=[s2], base=TimeBase(100))
    assert norm_states(r)["must_write"] == ("fulfilled", "fulfilled")
    assert [a["file"] for a in r["amendments"]] == ["version 2"]
    r = go(s1, lines[:4] + [{"t": 345, "messages": [m("AG", "T", "write")]}],
           run_versions=[s2], base=TimeBase(100))
    assert norm_states(r)["must_write"] == ("violated", "violated")


def test_a_regime_of_one_version_only_is_refused(tmp_path):
    p1, p2 = _versioned(tmp_path)
    s1 = load_version(p1)
    other = p2.read_text().replace("DRY", "CALM")
    p3 = tmp_path / "v3.cadl"
    p3.write_text(other, encoding="utf-8")
    with pytest.raises(NormsEvalError, match="which is not a regime of version 2"):
        go(s1, [{"t": 0, "regime": "DRY"}, {"t": 150}], run_versions=[load_version(p3)],
           base=TimeBase(100))


# === Gateways ===========================================================================================

KEY = b"regime-key-0123456789abcdef"
APPROVER_KEYS = {"OPERATOR": b"operator-key-0123456789abcdef"}


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _example(tmp_path: Path) -> Path:
    f = tmp_path / "governance.cadl"
    f.write_text(SOURCES["example"][1], encoding="utf-8")
    return f


def _call(id_, name) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": "tools/call",
                       "params": {"name": name}})


def _mcp(tmp_path, **kw):
    from cadl.mcp_gateway import Gateway
    client, server = [], []
    clock = _Clock()
    gw = Gateway(_example(tmp_path), "PLANNER", "FILES", tmp_path / "a.jsonl",
                 tmp_path / "a.trace.jsonl", client.append, server.append,
                 inbox=tmp_path / "in", clock=clock, approver_keys=dict(APPROVER_KEYS),
                 **kw)
    return gw, client, server, clock


def _replay_equal(tmp_path) -> dict:
    out = tmp_path / "replayed.jsonl"
    cadl = tmp_path / "governance.cadl"
    assert main(["agents-run", str(cadl), "--trace", str(tmp_path / "a.trace.jsonl"),
                 "--audit", str(out)]) == 0
    assert out.read_bytes() == (tmp_path / "a.jsonl").read_bytes()
    assert verify(tmp_path / "a.jsonl")["valid"]
    return run(load_version(cadl), load_run_trace(tmp_path / "a.trace.jsonl"))


class TestMcpGateway:
    def test_init_regime_and_changes_from_the_inbox(self, tmp_path):
        from cadl.gateway_core import _drop, regime_file
        gw, client, server, clock = _mcp(tmp_path, init={"regime": "STORM"},
                                         regime_key=KEY)
        gw.client_line(_call(1, "delete_file"))          # not governed in STORM
        assert len(server) == 1 and gw.held == {}
        _drop(tmp_path / "in", regime_file(tmp_path / "in", "NORMAL", KEY))
        clock.t += 1
        gw.tick()
        gw.client_line(_call(2, "delete_file"))          # held for OPERATOR
        assert "h-1" in gw.held and len(server) == 1
        # refused: no MAC, another key, an unknown regime
        for obj in (regime_file(tmp_path / "in", "STORM", None),
                    regime_file(tmp_path / "in", "STORM", b"x" * 20),
                    regime_file(tmp_path / "in", "WINDY", KEY)):
            _drop(tmp_path / "in", obj)
            clock.t += 1
            gw.tick()
        assert gw.runtime.regime == "NORMAL" and gw.failed is None
        gw.stop()
        r = _replay_equal(tmp_path)
        events = [(x["event"], x["reason"]) for x in r["audit"]
                  if x.get("event", "").startswith("regime")]
        assert events == [("regime_change", "NORMAL"), ("regime_refused", "unauthenticated"),
                          ("regime_refused", "unauthenticated"),
                          ("regime_refused", "unknown_regime")]
        trace = [json.loads(x) for x in (tmp_path / "a.trace.jsonl").read_text().splitlines()]
        assert trace[0]["regime"] == "STORM"
        assert [x.get("regime") for x in trace if "regime" in x] == ["STORM", "NORMAL"]

    def test_replayed_nonce_and_options(self, tmp_path):
        from cadl.gateway_core import GatewayError, _drop, regime_file
        gw, client, server, clock = _mcp(tmp_path, regime_key=KEY)
        obj = regime_file(tmp_path / "in", "STORM", KEY)
        _drop(tmp_path / "in", obj)
        gw.tick()
        _drop(tmp_path / "in", obj)
        clock.t += 1
        gw.tick()
        assert [x["reason"] for x in gw.runtime.audit if x.get("event") == "regime_refused"] \
            == ["replayed"]
        gw.stop()
        with pytest.raises(GatewayError, match="--regime-key authenticates"):
            from cadl.mcp_gateway import Gateway
            Gateway(_example(tmp_path), "PLANNER", "FILES", tmp_path / "b.jsonl",
                    tmp_path / "b.t", list().append, list().append, regime_key=KEY)
        other = tmp_path / "x"
        other.mkdir()
        with pytest.raises(GatewayError, match="the regime key is"):
            _mcp(other, regime_key=APPROVER_KEYS["OPERATOR"])

    def test_without_a_regime_key_the_authenticated_inbox_refuses(self, tmp_path):
        from cadl.gateway_core import _drop, regime_file
        gw, client, server, clock = _mcp(tmp_path)
        _drop(tmp_path / "in", regime_file(tmp_path / "in", "STORM", KEY))
        gw.tick()
        assert gw.runtime.regime == "NORMAL"
        gw.stop()
        assert [(x["event"], x["reason"]) for x in _replay_equal(tmp_path)["audit"]
                if x.get("event") == "regime_refused"] == [("regime_refused", "unauthenticated")]

    def test_mcp_regime_command(self, tmp_path, capsys):
        inbox = tmp_path / "in"
        inbox.mkdir()
        (inbox / "run.json").write_text(json.dumps({"run_id": "a" * 32}))
        (tmp_path / "k").write_bytes(KEY)
        assert main(["mcp-regime", str(inbox), "STORM", "--key", str(tmp_path / "k")]) == 0
        written = [p for p in inbox.iterdir() if p.name != "run.json"]
        obj = json.loads(written[0].read_text())
        assert (obj["kind"], obj["regime"], obj["run_id"]) == ("regime", "STORM", "a" * 32)
        assert "mac" in obj
        assert main(["mcp-regime", str(tmp_path / "nowhere"), "STORM"]) == 2
        capsys.readouterr()


    def test_unauthenticated_files_are_refused_before_their_regime(self, tmp_path):
        """The MAC is checked before the regime: an unauthenticated file is
        recorded as unauthenticated, never as unknown_regime."""
        from cadl.gateway_core import _drop, regime_file
        gw, client, server, clock = _mcp(tmp_path, regime_key=KEY)
        for i in range(2):
            _drop(tmp_path / "in", {"kind": "regime", "regime": f"NOPE{i}"})
            clock.t += 1
            gw.tick()
        _drop(tmp_path / "in", regime_file(tmp_path / "in", "NOPE", KEY))
        clock.t += 1
        gw.tick()
        assert gw.failed is None
        gw.stop()
        assert [x["reason"] for x in _replay_equal(tmp_path)["audit"]
                if x.get("event") == "regime_refused"] \
            == ["unauthenticated", "unauthenticated", "unknown_regime"]

    def test_insecure_approvals_with_a_regime_key_verify_the_mac(self, tmp_path):
        from cadl.gateway_core import _drop, regime_file
        from cadl.mcp_gateway import Gateway
        clock = _Clock()
        gw = Gateway(_example(tmp_path), "PLANNER", "FILES", tmp_path / "a.jsonl",
                     tmp_path / "a.trace.jsonl", [].append, [].append,
                     inbox=tmp_path / "in", clock=clock, insecure_approvals=True,
                     regime_key=KEY)
        assert json.loads((tmp_path / "in" / "run.json").read_text())["regimes_signed"] is True
        _drop(tmp_path / "in", {"kind": "regime", "regime": "STORM"})
        clock.t += 1
        gw.tick()
        assert gw.runtime.regime == "NORMAL"
        _drop(tmp_path / "in", regime_file(tmp_path / "in", "STORM", KEY))
        clock.t += 1
        gw.tick()
        assert gw.runtime.regime == "STORM"
        gw.stop()
        assert [(x["event"], x["reason"]) for x in _replay_equal(tmp_path)["audit"]
                if x.get("event", "").startswith("regime")] \
            == [("regime_refused", "unauthenticated"), ("regime_change", "STORM")]

    def test_mcp_regime_without_a_key_against_an_authenticated_gateway(self, tmp_path,
                                                                       capsys):
        inbox = tmp_path / "in"
        inbox.mkdir()
        for run in ({"run_id": "a" * 32, "approver_keys": True},
                    {"run_id": "a" * 32, "approver_keys": False, "regimes_signed": True}):
            (inbox / "run.json").write_text(json.dumps(run))
            assert main(["mcp-regime", str(inbox), "STORM"]) == 2
            assert "give --key" in capsys.readouterr().err
            assert [p.name for p in inbox.iterdir()] == ["run.json"]
        (inbox / "run.json").write_text(json.dumps({"run_id": "a" * 32,
                                                    "approver_keys": False}))
        assert main(["mcp-regime", str(inbox), "STORM"]) == 0
        capsys.readouterr()


def _gateway_versions(tmp_path: Path):
    """The example as version 1 and version 2 (period 2), where version 2
    renames STORM to WINDY."""
    text = SOURCES["example"][1]
    text = text.replace("    - agents: 0.1\n", "    - agents: 0.1\n    - amendment: 0.1\n", 1)
    text = text.replace('sos:\n  name: "AssistedReporting"\n',
                        'sos:\n  name: "AssistedReporting"\n  version: "1"\n', 1)
    v1 = text.rstrip("\n") + "\n  amendment:\n    transitional: { default: switch }\n"
    v2 = text.replace('version: "1"', 'version: "2"').replace("STORM", "WINDY")
    v2 = v2.rstrip("\n") + '\n  amendment:\n    previous: "1"\n    effective: { period: 2 }\n'
    checked(v1)
    checked(v2)
    (tmp_path / "governance.cadl").write_text(v1, encoding="utf-8")
    (tmp_path / "v2.cadl").write_text(v2, encoding="utf-8")
    return load_version(tmp_path / "v2.cadl")


def test_gateway_refuses_a_regime_that_a_version_of_the_run_lacks(tmp_path):
    """A regime that the version in force or a later version does not have
    is refused as unknown_regime, and the gateway stays alive (K.11)."""
    from cadl.gateway_core import _drop, regime_file
    from cadl.mcp_gateway import Gateway
    v2 = _gateway_versions(tmp_path)
    for when in (0, 150):        # before version 2 takes effect, and after
        d = tmp_path / f"r{when}"
        d.mkdir()
        (d / "governance.cadl").write_text((tmp_path / "governance.cadl").read_text())
        clock = _Clock()
        gw = Gateway(d / "governance.cadl", "PLANNER", "FILES", d / "a.jsonl",
                     d / "a.trace.jsonl", [].append, [].append, inbox=d / "in",
                     clock=clock, approver_keys=dict(APPROVER_KEYS), regime_key=KEY,
                     run_versions=[v2], base=TimeBase(100))
        clock.t += when
        gw.tick()
        _drop(d / "in", regime_file(d / "in", "STORM", KEY))
        clock.t += 1
        gw.tick()
        assert gw.failed is None and gw.runtime.regime == "NORMAL"
        clock.t += 200
        gw.tick()
        assert gw.failed is None
        gw.stop()
        assert [(x["event"], x["reason"]) for x in gw.runtime.audit
                if x.get("event") == "regime_refused"] == [("regime_refused", "unknown_regime")]


def test_gateway_refuses_an_init_regime_that_a_version_lacks(tmp_path):
    from cadl.gateway_core import GatewayError
    from cadl.mcp_gateway import Gateway
    v2 = _gateway_versions(tmp_path)
    with pytest.raises(GatewayError, match="--init names regime STORM, which is not a "
                                           "regime of version 2"):
        Gateway(tmp_path / "governance.cadl", "PLANNER", "FILES", tmp_path / "a.jsonl",
                tmp_path / "a.trace.jsonl", [].append, [].append, inbox=tmp_path / "in",
                clock=_Clock(), approver_keys=dict(APPROVER_KEYS), regime_key=KEY,
                init={"regime": "STORM"}, run_versions=[v2], base=TimeBase(100))
    # nothing is left behind: a corrected start creates the logs
    assert not (tmp_path / "a.jsonl").exists() and not (tmp_path / "a.trace.jsonl").exists()
    assert not (tmp_path / "in" / "run.json").exists()
    gw = Gateway(tmp_path / "governance.cadl", "PLANNER", "FILES", tmp_path / "a.jsonl",
                 tmp_path / "a.trace.jsonl", [].append, [].append, inbox=tmp_path / "in",
                 clock=_Clock(), approver_keys=dict(APPROVER_KEYS), regime_key=KEY,
                 run_versions=[v2], base=TimeBase(100))
    assert gw.failed is None
    gw.stop()


def test_gateway_refuses_a_run_in_no_regime_with_a_version_that_has_regimes(tmp_path):
    """Version 1 has no regimes:, version 2 has them: the run would be in no
    regime of version 2 when it takes effect (K.11, item 3)."""
    from cadl.gateway_core import GatewayError
    from cadl.mcp_gateway import Gateway
    plain, reg = SOURCES["example"]
    text = plain.replace("    - agents: 0.1\n", "    - agents: 0.1\n    - amendment: 0.1\n", 1)
    text = text.replace('sos:\n  name: "AssistedReporting"\n',
                        'sos:\n  name: "AssistedReporting"\n  version: "1"\n', 1)
    v1 = text.rstrip("\n") + "\n  amendment:\n    transitional: { default: switch }\n"
    v2 = _gateway_versions(tmp_path)          # writes governance.cadl (with regimes) and v2
    checked(v1)
    (tmp_path / "governance.cadl").write_text(v1, encoding="utf-8")
    with pytest.raises(GatewayError, match="the run starts in no regime .* version 2 of the "
                                           "run binds regimes"):
        Gateway(tmp_path / "governance.cadl", "PLANNER", "FILES", tmp_path / "a.jsonl",
                tmp_path / "a.trace.jsonl", [].append, [].append, inbox=tmp_path / "in",
                clock=_Clock(), approver_keys=dict(APPROVER_KEYS), regime_key=KEY,
                run_versions=[v2], base=TimeBase(100))
    assert not (tmp_path / "a.jsonl").exists() and not (tmp_path / "a.trace.jsonl").exists()


def test_a2a_gateway_follows_the_regime(tmp_path):
    from cadl.a2a_gateway import A2AGateway
    from cadl.gateway_core import _drop, regime_file

    from .test_a2a_gateway import CADL, Local, params, rpc
    text = CADL.read_text(encoding="utf-8")
    f = tmp_path / "work.cadl"
    f.write_text(with_regimes(text, "DELEGATED_WORK"), encoding="utf-8")
    checked(f.read_text())
    loc = Local(tmp_path, cadl=f, init={"regime": "STORM"}, inbox=tmp_path / "in",
                insecure_approvals=True)
    assert isinstance(loc.gw, A2AGateway)
    reply = loc.post(rpc("SendMessage", params("translate")))
    assert "error" not in reply.body and len(loc.sent) == 1      # not governed in STORM
    _drop(tmp_path / "in", regime_file(tmp_path / "in", "NORMAL", None))
    loc.t[0] += 1
    loc.gw.tick()
    reply = loc.post(rpc("SendMessage", params("translate")))
    assert "deny" in json.dumps(reply.body) and len(loc.sent) == 1   # governed: denied
    loc.stop_and_replay()
    assert [x.get("regime") for x in map(json.loads, loc.trace.read_text().splitlines())
            if "regime" in x] == ["STORM", "NORMAL"]
    assert read_records(loc.audit)
