"""Decision records (Appendix L): `cadl.decisions` and `cadl decisions-verify`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadl.audit_log import AuditKeyError, file_sha256
from cadl.cli import main
from cadl.decisions import FORMAT, FORMAT_1, FORMAT_2, DecisionChain, verify

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
V1 = EXAMPLES / "amendment_robot_town_v1.cadl"
V2 = EXAMPLES / "amendment_robot_town_v2.cadl"
EXAMPLE = EXAMPLES / "decisions_robot_town.jsonl"
RUN_ID = "3f2a0c4e9b1d47a8a6c5e2f1d0b9a8c7"
KEY = b"0123456789abcdef-test-key"
T = "2026-11-18T09:00:00Z"


def _party(pid, kind, roles, name=None, actor=None, model=None, on_behalf_of=None,
           layers=None):
    return {"id": pid, "kind": kind, "roles": roles, "name": name, "actor": actor,
            "model": model, "on_behalf_of": on_behalf_of, "layers": layers}


PARTIES = [
    _party("operator", "human", ["owner", "decider"], "service operator", "OPERATOR",
           layers=["rule", "contract"]),
    _party("regulator", "human", ["decider"], "regulator", layers=["regulation"]),
    _party("llm", "llm", ["proposer", "advisor"], model="claude-opus-5-5"),
    _party("steward", "tool", ["assessor", "decider"], "town.steward",
           model="cadl-robot-town (main)", layers=["rule"]),
    _party("A", "agent", ["voter"], "company A"),
    _party("B", "agent", ["voter"], "company B"),
    _party("C", "agent", ["voter"], "company C"),
    _party("customers", "organisation", ["affected"]),
]


def _e(eid, type_, by, point=1, basis=(), fulfils=(), attachments=(), generated=None, **f):
    return {"id": eid, "type": type_, "point": {"period": point}, "written": T, "by": by,
            "basis": list(basis), "fulfils": list(fulfils), "attachments": list(attachments),
            "generated": generated, **f}


def _ev(reason, assumptions=(), unknowns=(), predictions=()):
    return {"reason": reason, "assumptions": list(assumptions), "unknowns": list(unknowns),
            "predictions": list(predictions)}


def _pred(option, subject, expected, note=None):
    return {"option": option, "subject": subject, "expected": expected, "note": note}


GEN = {"model": "claude-opus-5-5", "request_digest": "5be0a1c2d3e4f506"}


def period1(v2_attach: str = "amendment_robot_town_v2.cadl") -> list:
    """Period 1 of the English record of Course D (Appendix L, L.9)."""
    v2 = {"path": v2_attach, "sha256": file_sha256(V2), "media_type": "application/yaml"}
    return [
        _e("K1", "objective", "operator",
           text="Deliver as many orders as possible within the times we promised customers.",
           measure=[{"metric": "on_time_deliveries", "role": "goal", "direction": "increase",
                     "target": None}]),
        _e("P1.m0", "measurement", "steward", of="in_force", version="1",
           method={"runs": 5, "window": "1h", "description": "seeds 1-5"},
           values={"on_time_deliveries": 121.8, "late_deliveries": 10.4, "blocked_s": 2404},
           per_party={"A": {"utility": 38.4}, "B": {"utility": 38.9}, "C": {"utility": 28.8},
                      "customers": {"late_deliveries": 10.4}},
           compared_with=None, runs_better=None),
        _e("P1.2", "proposal", "llm", generated=GEN, kind="amendment",
           title="Lower blocking_guard threshold from 3 to 2 waiting robots",
           changes=[{"parameter": "block_k", "from": "3", "to": "2", "layer": "rule"}],
           objective=None, measure=None, affected=["customers"],
           **_ev("Only 2.8 blocking_guard evictions fire per period, yet 2404 s of blocked "
                 "time accumulates.",
                 assumptions=["Queues of two robots are common, queues of three are rare"],
                 predictions=[_pred("P1.2", "metric:blocked_s", "decrease"),
                              _pred("P1.2", "vote:A", "yes"),
                              _pred("P1.2", "vote:B", "yes", "probably"),
                              _pred("P1.2", "vote:C", "no")])),
        _e("P1.2.m", "measurement", "steward", of="P1.2", version=None,
           method={"runs": 5, "window": "1h", "description": "seeds 1-5"},
           values={"on_time_deliveries": 131.0, "late_deliveries": 1.2, "blocked_s": 985},
           per_party={"A": {"utility": 45.4}, "B": {"utility": 37.2}, "C": {"utility": 19.0},
                      "customers": {"late_deliveries": 1.2}},
           compared_with="P1.m0", runs_better=5),
        _e("P1.2.c", "check", "steward", proposal="P1.2",
           results=[{"name": "cadl check", "passed": True, "note": None},
                    {"name": "constitution", "passed": True, "note": None},
                    {"name": "gain", "passed": True, "note": "+9.2"}]),
        _e("P1.2.vA", "vote", "A", proposal="P1.2", value="yes", reason=None),
        _e("P1.2.vB", "vote", "B", proposal="P1.2", value="no", reason=None),
        _e("P1.2.vC", "vote", "C", proposal="P1.2", value="no", reason=None),
        _e("D1.req", "request", "steward", to="operator",
           question="Should block_k be lowered from 3 to 2 against the votes of B and C?",
           options=["P1.2", "keep"],
           why=[{"option": "P1.2", "reason": "the main goal improves by +9.2, but B and C "
                                             "voted it down"}]),
        _e("D1.rec", "recommendation", "llm", generated=GEN, request="D1.req",
           options=[{"option": "P1.2", "summary": "+9.2 on 5/5 seeds", "affected": "C loses"},
                    {"option": "keep", "summary": "respects the vote", "affected": ""}],
           recommends="keep",
           **_ev("Adopting it means overriding a majority vote.",
                 unknowns=["Why C's utility drops sharply"])),
        _e("D1", "decision", "operator", basis=["K1", "P1.m0", "P1.2", "P1.2.m", "P1.2.c",
                                                "P1.2.vA", "P1.2.vB", "P1.2.vC", "D1.req",
                                                "D1.rec"],
           question="Should block_k be lowered from 3 to 2?", options=["P1.2", "keep"],
           choice="P1.2", procedure="override", rule=None, affected=["C", "customers"],
           result="2",
           **_ev("The intersections are a road shared by the three companies; waiting there "
                 "is no company's right.")),
        _e("D1.f1", "follow_up", "operator", text="Re-measure C's utility and the road "
                                                  "violations", due={"period": 2},
           decision="D1",
           fulfilled_by={"type": "measurement", "of": "in_force", "same_method_as": "P1.m0",
                         "subjects": ["metric:utility@C", "metric:late_deliveries"]}),
        _e("V2", "version", "steward", attachments=[v2], version="2", previous="1",
           effective={"period": 2}, source_sha256=file_sha256(V2), decision="D1"),
        _e("P2.m0", "measurement", "steward", point=2, fulfils=["D1.f1"], of="in_force",
           version="2", method={"runs": 5, "window": "1h", "description": "seeds 101-105"},
           values={"on_time_deliveries": 223.8, "late_deliveries": 4.2, "blocked_s": 936},
           per_party={"C": {"utility": 56.3}}, compared_with=None, runs_better=None),
        _e("F2.D1.f1", "measurement", "steward", point=2, fulfils=["D1.f1"], of="in_force",
           version="2", method={"runs": 5, "window": "1h", "description": "seeds 1-5"},
           values={"on_time_deliveries": 131.0, "late_deliveries": 1.2, "blocked_s": 985},
           per_party={"C": {"utility": 19.0}}, compared_with="P1.m0", runs_better=5),
    ]


def header(chain: DecisionChain) -> str:
    return chain.header({"name": "RobotTown", "version": "1", "source_sha256": file_sha256(V1)},
                        PARTIES, T, "one simulated hour of the robot town")


def write(path: Path, entries: list, key=None, end=True, run_id=RUN_ID) -> str:
    chain = DecisionChain(key=key, run_id=run_id)
    lines = [header(chain)] + [chain.entry(e) for e in entries]
    if end:
        lines.append(chain.end())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return chain.last_digest


def record(tmp_path, entries=None, **kw) -> Path:
    import shutil
    shutil.copy(V2, tmp_path / V2.name)
    p = tmp_path / "decisions.jsonl"
    write(p, period1() if entries is None else entries, **kw)
    return p


def kinds(result) -> list:
    return sorted({f["finding"] for f in result["findings"]})


def rules(result) -> list:
    return sorted({e["rule"] for e in result["errors"]})


# --- the record of L.9 ---------------------------------------------------------

def test_period1_is_valid_with_its_findings(tmp_path):
    r = verify(record(tmp_path), versions=[V1])
    assert r["valid"], r["errors"]
    assert r["entries"] == 15 and r["complete"]
    assert kinds(r) == ["against recommendation", "fulfilment not shown", "override",
                        "prediction missed", "unknowns"]
    missed = [f for f in r["findings"] if f["finding"] == "prediction missed"]
    assert [f["entry"] for f in missed] == ["P1.2"] and "B" in missed[0]["text"]
    assert r["predictions"] == {"total": 4, "checked": 4, "missed": 1, "not_checked": {
        "keep": 0, "no later entry": 0, "cannot compare": 0}}
    assert sorted(Path(v).name for v in r["versions_checked"]) == [V1.name, V2.name]
    assert not [n for n in r["not_checked"] if n.startswith("L-1")]


def test_example_file_is_the_record_of_period1(tmp_path):
    """examples/decisions_robot_town.jsonl is written by this module."""
    expected = tmp_path / "x.jsonl"
    write(expected, period1())
    assert EXAMPLE.read_text(encoding="utf-8") == expected.read_text(encoding="utf-8")
    r = verify(EXAMPLE, versions=[V1])
    assert r["valid"], r["errors"]


def test_cli(tmp_path, capsys):
    p = record(tmp_path)
    assert main(["decisions-verify", str(p), "--version", str(V1)]) == 0
    out = capsys.readouterr().out
    assert "valid" in out and "override [D1]" in out and "prediction missed [P1.2]" in out
    assert main(["decisions-verify", str(p), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["valid"] and data["run_id"] == RUN_ID
    assert main(["decisions-verify", str(tmp_path / "none.jsonl")]) == 2


# --- integrity (L-2) ---------------------------------------------------------------

def test_a_changed_reason_breaks_the_chain(tmp_path):
    p = record(tmp_path)
    text = p.read_text(encoding="utf-8").replace("no company's right", "every company's right")
    p.write_text(text, encoding="utf-8")
    r = verify(p)
    assert not r["valid"] and rules(r) == ["L-2"]
    assert r["errors"][0]["line"] == 12          # header + 10 entries before D1


def test_a_deleted_line_and_a_cut_tail(tmp_path):
    p = record(tmp_path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    (tmp_path / "del.jsonl").write_text("".join(lines[:5] + lines[6:]), encoding="utf-8")
    assert rules(verify(tmp_path / "del.jsonl")) == ["L-2"]
    (tmp_path / "cut.jsonl").write_text("".join(lines[:-1]), encoding="utf-8")
    r = verify(tmp_path / "cut.jsonl")
    assert r["valid"] and "open record" in kinds(r)


def test_anchor_detects_a_rewritten_record(tmp_path):
    p = tmp_path / "decisions.jsonl"
    anchor = write(p, period1())
    entries = period1()
    entries[10]["reason"] = "Rewritten afterwards."
    write(p, entries)                            # the whole chain recomputed
    assert verify(p)["valid"]
    r = verify(p, anchor=anchor)
    assert not r["valid"] and rules(r) == ["L-2"]


def test_keyed_record(tmp_path):
    p = tmp_path / "decisions.jsonl"
    write(p, period1(), key=KEY)
    assert verify(p, key=KEY)["valid"]
    with pytest.raises(AuditKeyError):
        verify(p)
    assert rules(verify(p, key=b"another-key-of-16-bytes")) == ["L-2"]


# --- structure and evidence (L-1 to L-8) --------------------------------------------

def test_a_reason_is_required_from_every_party(tmp_path):
    entries = period1()
    entries[2]["reason"] = ""                    # the LLM's proposal
    entries[10]["reason"] = " "                  # the human's decision
    r = verify(record(tmp_path, entries))
    assert rules(r) == ["L-8"]
    assert {e["entry"] for e in r["errors"]} == {"P1.2", "D1"}


def test_evidence_fields_are_required(tmp_path):
    entries = period1()
    del entries[9]["assumptions"]
    r = verify(record(tmp_path, entries))
    assert "L-1" in rules(r) and r["errors"][0]["entry"] == "D1.rec"


def test_an_automatic_decision_gives_a_rule_and_is_made_by_a_tool(tmp_path):
    entries = period1()[:8]
    entries.append(_e("D1", "decision", "steward", basis=["P1.2", "P1.2.vA", "P1.2.vB",
                                                         "P1.2.vC"],
                      question=None, options=["P1.2", "keep"], choice="keep",
                      procedure="automatic", rule="adopt the best proposal that passes every "
                                                  "gate and the vote",
                      affected=[], result="1", **_ev(None)))
    assert verify(record(tmp_path, entries))["valid"]
    entries[-1]["by"] = "operator"
    r = verify(record(tmp_path, entries))
    assert rules(r) == ["L-6"]


def test_roles_and_references(tmp_path):
    entries = period1()
    entries[5]["by"] = "llm"                     # a vote by a party that is not a voter
    entries[10]["basis"].append("V2")            # a later entry
    r = verify(record(tmp_path, entries))
    assert rules(r) == ["L-4", "L-5", "L-6"] or rules(r) == ["L-4", "L-5"]
    assert any(e["rule"] == "L-5" and e["entry"] == "P1.2.vA" for e in r["errors"])


def test_llm_and_tool_parties_give_their_model(tmp_path):
    import cadl.decisions as d
    parties = [dict(p) for p in PARTIES]
    parties[2]["model"] = None
    p = tmp_path / "decisions.jsonl"
    chain = DecisionChain(run_id=RUN_ID)
    lines = [chain.header({"name": "RobotTown", "version": "1", "source_sha256": None},
                          parties, T)]
    lines += [chain.entry(e) for e in period1()[:1]] + [chain.end()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = d.verify(p)
    assert rules(r) == ["L-8"]


def test_version_entry_names_its_decision(tmp_path):
    entries = period1()
    entries[10]["result"] = "3"
    r = verify(record(tmp_path, entries))
    assert rules(r) == ["L-7"]


# --- an LLM that decides in place of the operator --------------------------------

def test_llm_deciding_on_behalf_of_the_operator(tmp_path):
    parties = PARTIES + [_party("llm-decider", "llm", ["decider"], model="claude-opus-5-5",
                                on_behalf_of="operator", layers=["rule", "contract"])]
    entries = period1()
    d = entries[10]
    d.update(by="llm-decider", choice="keep", procedure="vote", result="1", generated=GEN,
             **_ev("Adopting it means overriding a majority vote.",
                   assumptions=["The companies keep cooperating"],
                   unknowns=["Why C's utility drops sharply"],
                   predictions=[_pred("keep", "metric:late_deliveries", "unchanged")]))
    entries = entries[:11]                       # no follow-up, no new version
    p = tmp_path / "decisions.jsonl"
    chain = DecisionChain(run_id=RUN_ID)
    lines = [chain.header({"name": "RobotTown", "version": "1",
                           "source_sha256": file_sha256(V1)}, parties, T)]
    lines += [chain.entry(e) for e in entries] + [chain.end()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = verify(p, versions=[V1])
    assert r["valid"], r["errors"]
    assert "delegated" in kinds(r) and "override" not in kinds(r)
    assert "against recommendation" not in kinds(r)


def test_contract_change_without_the_customers_measured(tmp_path):
    entries = period1()
    entries[2]["changes"] = [{"parameter": "pickup_deadline", "from": "120s", "to": "150s",
                              "layer": "contract"}]
    entries[2]["affected"] = []
    del entries[3]["per_party"]["customers"]
    r = verify(record(tmp_path, entries), versions=[V1])
    assert "affected not measured" in kinds(r)
    # the operator is the authority of the contract layer: no L-11 error
    assert "L-11" not in rules(r)


# --- against the model (L-9 to L-11) ----------------------------------------------

def test_amendability_and_authority_against_the_model(tmp_path):
    entries = period1()
    entries[2]["changes"] = [{"parameter": "block_k", "from": "3", "to": "1", "layer": "rule"}]
    r = verify(record(tmp_path, entries), versions=[V1])
    assert "L-10" in rules(r)
    entries = period1()
    entries[10]["procedure"] = "single"          # the operator is not the council
    r = verify(record(tmp_path, entries), versions=[V1])
    assert "L-11" in rules(r)


def test_a_proposal_outside_k3_that_no_decision_adopts_is_a_finding(tmp_path):
    entries = period1()
    other = dict(entries[2], id="P1.3", predictions=[],
                 changes=[{"parameter": "block_k", "from": "3", "to": "9", "layer": "rule"}])
    entries.insert(3, other)
    r = verify(record(tmp_path, entries), versions=[V1])
    assert r["valid"], r["errors"]
    assert [f["entry"] for f in r["findings"] if f["finding"] == "not amendable"] == ["P1.3"]


def test_changed_version_file(tmp_path):
    p = record(tmp_path)
    (tmp_path / V2.name).write_text(V2.read_text(encoding="utf-8") + "\n# changed\n",
                                    encoding="utf-8")
    r = verify(p, versions=[V1])
    assert "L-9" in rules(r)


def test_without_the_model_l9_to_l11_are_not_checked(tmp_path):
    p = tmp_path / "decisions.jsonl"
    write(p, period1())                          # v2 is not next to the record
    r = verify(p)
    assert r["valid"]
    assert any("L-9 to L-11" in n for n in r["not_checked"])


# --- findings ----------------------------------------------------------------

def test_open_follow_up(tmp_path):
    entries = period1()
    entries[-1]["fulfils"] = []
    r = verify(record(tmp_path, entries))
    assert "open follow-up" in kinds(r)


def test_reading_not_decided_and_corrected(tmp_path):
    entries = period1()
    entries[0]["measure"] = None
    entries.append(_e("C1", "correction", "operator", point=2, corrects="P1.2.c",
                      text="the gain was +9.2 on on_time_deliveries"))
    r = verify(record(tmp_path, entries))
    assert {"reading not decided", "corrected"} <= set(kinds(r))


def test_unmeasured_choice(tmp_path):
    entries = period1()
    entries[10]["basis"].remove("P1.2.m")
    r = verify(record(tmp_path, entries))
    assert "unmeasured choice" in kinds(r)


def test_header_format():
    chain = DecisionChain(run_id=RUN_ID)
    obj = json.loads(chain.header({"name": "x", "version": None, "source_sha256": None},
                                  PARTIES, T))
    assert obj["decisions_format"] == FORMAT
    assert list(obj)[-3:] == ["seq", "prev", "digest"]


# --- review: malformed input, votes, versions ---------------------------------

BAD_VALUES = [None, 5, True, "x", ["x"], [["x"]], {"a": 1}, [5], [{"a": 1}]]


def test_malformed_fields_are_errors_never_crashes(tmp_path):
    """Every field of every entry, replaced by values of the wrong type."""
    import copy
    import shutil
    shutil.copy(V2, tmp_path / V2.name)
    base = period1()
    p = tmp_path / "decisions.jsonl"
    for i, e in enumerate(base):
        for field in list(e):
            for bad in BAD_VALUES:
                entries = copy.deepcopy(base)
                entries[i][field] = bad
                write(p, entries)
                r = verify(p, versions=[V1])      # must not raise
                assert isinstance(r["errors"], list)


def test_malformed_parties_are_errors(tmp_path):
    for field in ("roles", "on_behalf_of", "kind", "id", "model"):
        for bad in BAD_VALUES:
            parties = [dict(x) for x in PARTIES]
            parties[0][field] = bad
            chain = DecisionChain(run_id=RUN_ID)
            lines = [chain.header({"name": "R", "version": None, "source_sha256": None},
                                  parties, T)]
            lines += [chain.entry(e) for e in period1()] + [chain.end()]
            p = tmp_path / "d.jsonl"
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            verify(p)


def test_override_whose_votes_pass_is_an_error(tmp_path):
    entries = period1()
    for j in (6, 7):
        entries[j]["value"] = "yes"
    r = verify(record(tmp_path, entries), versions=[V1])
    assert "L-6" in rules(r)


def test_keep_by_vote_needs_the_votes_to_reject(tmp_path):
    entries = period1()[:8]
    entries[6]["value"] = "yes"                  # A and B vote yes: the change passes
    entries.append(_e("D1", "decision", "operator",
                      basis=["P1.2", "P1.2.m", "P1.2.vA", "P1.2.vB", "P1.2.vC"],
                      question=None, options=["P1.2", "keep"], choice="keep", procedure="vote",
                      rule=None, affected=[], result="1", **_ev("keep the rules")))
    r = verify(record(tmp_path, entries), versions=[V1])
    assert "L-6" in rules(r)
    entries[-1]["procedure"] = "override"
    r = verify(record(tmp_path, entries), versions=[V1])
    assert r["valid"], r["errors"]
    assert "override" in kinds(r)


def test_basis_must_hold_every_vote(tmp_path):
    entries = period1()
    entries[10]["basis"] = [b for b in entries[10]["basis"] if b not in ("P1.2.vB", "P1.2.vC")]
    r = verify(record(tmp_path, entries))
    assert "L-6" in rules(r)


def test_vote_outcome_not_checked_without_the_model(tmp_path):
    p = tmp_path / "decisions.jsonl"
    write(p, period1())
    r = verify(p)
    assert any("outcome of the votes" in n for n in r["not_checked"])


def test_wrong_source_sha256_of_an_attached_version(tmp_path):
    entries = period1()
    entries[12]["source_sha256"] = "0" * 64
    r = verify(record(tmp_path, entries), versions=[V1])
    assert "L-9" in rules(r)


def test_a_later_version_names_its_decision(tmp_path):
    entries = period1()
    entries[12]["decision"] = None
    r = verify(record(tmp_path, entries))
    assert "L-7" in rules(r)


def test_the_predictions_no_later_entry_checks_are_counted(tmp_path, capsys):
    entries = period1()
    entries[2]["predictions"] = [
        _pred("P1.2", "metric:blocked_s", "decrease"),            # checked, met
        _pred("P1.2", "vote:B", "yes"),                           # checked, missed
        _pred("keep", "metric:blocked_s", "unchanged"),           # about keep
        _pred("P1.2", "metric:utility@D", "increase"),            # no value of D
        _pred("P9", "metric:blocked_s", "decrease"),              # nothing measures P9
        _pred("P1.2", "vote:A", "probably"),                      # not yes or no
    ]
    r = verify(record(tmp_path, entries))
    assert r["predictions"] == {"total": 6, "checked": 2, "missed": 1, "not_checked": {
        "keep": 1, "no later entry": 1, "cannot compare": 2}}
    assert main(["decisions-verify", str(tmp_path / "decisions.jsonl")]) in (0, 1)
    out = capsys.readouterr().out
    assert ("predictions: 6; checked by a later entry: 2 (missed: 1); not checked: 4 "
            "(keep: 1, no later entry: 1, cannot compare: 2)") in out


def test_a_small_rise_is_not_a_missed_prediction(tmp_path):
    entries = period1()
    entries[2]["predictions"] = [_pred("P1.2", "metric:on_time_deliveries", "increase")]
    entries[3]["values"]["on_time_deliveries"] = 122.0       # 121.8 before
    r = verify(record(tmp_path, entries))
    assert not [f for f in r["findings"] if f["finding"] == "prediction missed"]
    entries[3]["values"]["on_time_deliveries"] = 121.8
    r = verify(record(tmp_path, entries))
    assert [f for f in r["findings"] if f["finding"] == "prediction missed"]


def test_points_at_instants_are_compared_in_utc(tmp_path):
    entries = period1()[:3]
    entries[0]["point"] = {"at": "2026-11-18T10:00:00+09:00"}   # 01:00 UTC
    entries[1]["point"] = {"at": "2026-11-18T02:00:00Z"}
    entries[2]["point"] = {"at": "2026-11-18T03:00:00+01:00"}   # 02:00 UTC
    assert verify(record(tmp_path, entries))["valid"]
    entries[2]["point"] = {"period": 0}
    assert "L-1" in rules(verify(record(tmp_path, entries)))


def test_reading_replaced_by_a_newer_objective(tmp_path):
    entries = period1()
    entries[0]["measure"] = None
    entries.insert(1, _e("K1b", "objective", "operator", text="A newer objective.",
                         measure=[{"metric": "late_deliveries", "role": "goal",
                                   "direction": "decrease", "target": None}]))
    entries[11]["basis"] = ["K1b" if b == "K1" else b for b in entries[11]["basis"]]
    r = verify(record(tmp_path, entries))
    assert "reading not decided" not in kinds(r)


def test_end_record_count_is_not_a_boolean(tmp_path):
    p = tmp_path / "d.jsonl"
    write(p, period1()[:1])
    lines = p.read_text(encoding="utf-8").splitlines()
    from cadl.audit_log import written
    import cadl.decisions as d
    obj = json.loads(lines[-1])
    body = {"decisions_end": True, "entries": True, "seq": obj["seq"], "prev": obj["prev"]}
    body["digest"] = d._digest(body, None)
    lines[-1] = written(body)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert "L-1" in rules(verify(p))


def test_given_version_that_does_not_exist(tmp_path):
    r = verify(record(tmp_path), versions=[tmp_path / "missing.cadl"])
    assert any("not found" in n for n in r["not_checked"])


# --- decisions 0.2: layers and fulfilment ----------------------------------------

def test_period1_fulfilment_by_other_seeds_is_not_shown(tmp_path):
    r = verify(record(tmp_path))
    shown = [f for f in r["findings"] if f["finding"] == "fulfilment not shown"]
    assert [f["entry"] for f in shown] == ["P2.m0"]
    assert "seeds 101-105" in shown[0]["text"] and "P1.m0" in shown[0]["text"]
    assert "open follow-up" not in kinds(r)       # F2.D1.f1 fulfils it


def test_only_a_wrong_fulfilment_leaves_the_follow_up_open(tmp_path):
    entries = period1()[:-1]
    r = verify(record(tmp_path, entries))
    assert {"fulfilment not shown", "open follow-up"} <= set(kinds(r))


def test_fulfilment_conditions(tmp_path):
    import copy
    base = period1()
    # a decision cannot fulfil a promise to re-measure
    entries = copy.deepcopy(base)[:-2] + [
        _e("D2", "decision", "operator", point=2, fulfils=["D1.f1"], basis=["D1.f1"],
           question=None, options=["keep"], choice="keep", procedure="single", rule=None,
           affected=[], result="2", **_ev("keep the rules"))]
    r = verify(record(tmp_path, entries))
    assert any("decision entry" in f["text"] for f in r["findings"]
               if f["finding"] == "fulfilment not shown")
    # a measurement without a promised subject
    entries = copy.deepcopy(base)
    entries[-1]["per_party"] = {"B": {"utility": 37.2}}
    r = verify(record(tmp_path, entries))
    assert any("metric:utility@C" in f["text"] for f in r["findings"]
               if f["finding"] == "fulfilment not shown")
    # a measurement of something else
    entries = copy.deepcopy(base)
    entries[-1]["of"] = "P1.2"
    r = verify(record(tmp_path, entries))
    assert any("measures P1.2" in f["text"] for f in r["findings"])
    # fulfilled_by null: any later entry fulfils it, as in decisions 0.1
    entries = copy.deepcopy(base)[:-1]
    next(e for e in entries if e["id"] == "D1.f1")["fulfilled_by"] = None
    r = verify(record(tmp_path, entries))
    assert not {"fulfilment not shown", "open follow-up"} & set(kinds(r))


def test_malformed_fulfilled_by(tmp_path):
    import copy
    for bad in ({"type": "nothing", "of": None, "same_method_as": None, "subjects": []},
                {"type": "decision", "of": "in_force", "same_method_as": None, "subjects": []},
                {"type": "measurement", "of": "P9", "same_method_as": None, "subjects": []},
                {"type": "measurement", "of": None, "same_method_as": "D1", "subjects": []},
                {"type": "measurement", "of": None, "same_method_as": None,
                 "subjects": ["vote:A"]},
                {"type": "measurement", "of": None, "same_method_as": None,
                 "subjects": ["metric:x@nobody"]},
                {"type": "measurement"}):
        entries = copy.deepcopy(period1())
        next(e for e in entries if e["id"] == "D1.f1")["fulfilled_by"] = bad
        r = verify(record(tmp_path, entries))
        assert {"L-1", "L-4"} & set(rules(r)), bad


def test_a_decider_adopts_only_changes_of_its_layers(tmp_path):
    entries = period1()
    entries[2]["changes"][0]["layer"] = "regulation"
    r = verify(record(tmp_path, entries))
    assert any(e["rule"] == "L-5" and "regulation" in e["what"] for e in r["errors"])
    entries[2]["changes"][0]["layer"] = "contract"
    assert "L-5" not in rules(verify(record(tmp_path, entries)))


def test_a_delegate_decides_within_the_layers_of_its_principal(tmp_path):
    for layers, ok in ((["rule"], True), (["rule", "contract"], True),
                       (["regulation"], False), (None, False)):
        parties = PARTIES + [_party("llm-decider", "llm", ["decider"], model="m",
                                    on_behalf_of="operator", layers=layers)]
        chain = DecisionChain(run_id=RUN_ID)
        p = tmp_path / "d.jsonl"
        lines = [chain.header({"name": "x", "version": None, "source_sha256": None},
                              parties, T)] + [chain.end()]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert ("L-8" in rules(verify(p))) is not ok, layers


def test_bad_layers_of_a_party(tmp_path):
    for bad in (["law"], ["rule", "rule"], "rule", [1]):
        parties = [dict(x) for x in PARTIES]
        parties[0]["layers"] = bad
        chain = DecisionChain(run_id=RUN_ID)
        p = tmp_path / "d.jsonl"
        lines = [chain.header({"name": "x", "version": None, "source_sha256": None},
                              parties, T)] + [chain.end()]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert "L-1" in rules(verify(p)), bad


def test_writer_fills_the_fields_of_0_2(tmp_path):
    """A writer of decisions 0.1 that names no layers and no fulfilled_by still writes a
    valid record of decisions 0.2."""
    parties = [{k: v for k, v in x.items() if k != "layers"} for x in PARTIES]
    entries = [{k: v for k, v in e.items() if k != "fulfilled_by"} for e in period1()]
    chain = DecisionChain(run_id=RUN_ID)
    lines = [chain.header({"name": "RobotTown", "version": "1",
                           "source_sha256": file_sha256(V1)}, parties, T)]
    lines += [chain.entry(e) for e in entries] + [chain.end()]
    import shutil
    shutil.copy(V2, tmp_path / V2.name)
    p = tmp_path / "d.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = verify(p)
    assert r["valid"], r["errors"]
    assert json.loads(lines[0])["parties"][0]["layers"] is None


def test_records_of_decisions_0_1_are_read_as_before(tmp_path):
    parties = [{k: v for k, v in x.items() if k != "layers"} for x in PARTIES]
    entries = [{k: v for k, v in e.items() if k != "fulfilled_by"} for e in period1()]
    chain = DecisionChain(run_id=RUN_ID, format=FORMAT_1)
    lines = [chain.header({"name": "RobotTown", "version": "1",
                           "source_sha256": file_sha256(V1)}, parties, T)]
    lines += [chain.entry(e) for e in entries] + [chain.end()]
    import shutil
    shutil.copy(V2, tmp_path / V2.name)
    p = tmp_path / "d.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = verify(p)
    assert r["valid"], r["errors"]
    assert json.loads(lines[0])["decisions_format"] == FORMAT_1
    assert "fulfilment not shown" not in kinds(r)
    # the fields of 0.2 are not fields of a record of 0.1
    chain = DecisionChain(run_id=RUN_ID, format=FORMAT_1)
    lines = [chain.header({"name": "RobotTown", "version": "1",
                           "source_sha256": file_sha256(V1)}, PARTIES, T)]
    lines += [chain.entry(e) for e in period1()] + [chain.end()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert "L-1" in rules(verify(p))


# --- decisions 0.3: delegations -----------------------------------------------

def _deleg(eid="G1", by="operator", to="llm-decider", layers=("rule", "contract"),
           procedures=("single", "vote"), until=None, point=1, reason="The operator lets "
           "the LLM answer the decision requests."):
    return _e(eid, "delegation", by, point=point, to=to,
              layers=None if layers is None else list(layers),
              procedures=None if procedures is None else list(procedures),
              conditions=["answer the requests of the steward"], until=until, reason=reason)


def _delegated_record(tmp_path, delegation, procedure="vote", choice="keep", fmt=FORMAT,
                      to_layers=("rule", "contract"), decision_period=None):
    parties = PARTIES + [_party("llm-decider", "llm", ["decider"], model="claude-opus-5-5",
                                on_behalf_of="operator", layers=list(to_layers))]
    entries = period1()
    d = entries[10]
    d.update(by="llm-decider", choice=choice, procedure=procedure,
             result="1" if choice == "keep" else d["result"], generated=GEN,
             **_ev("Adopting it means overriding a majority vote."))
    if decision_period is not None:
        d["point"] = {"period": decision_period}
    entries = ([] if delegation is None else [delegation]) + entries[:11]
    import shutil
    shutil.copy(V2, tmp_path / V2.name)
    p = tmp_path / "decisions.jsonl"
    chain = DecisionChain(run_id=RUN_ID, format=fmt)
    lines = [chain.header({"name": "RobotTown", "version": "1",
                           "source_sha256": file_sha256(V1)}, parties, T)]
    lines += [chain.entry(e) for e in entries] + [chain.end()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return verify(p, versions=[V1])


def test_a_decision_within_a_delegation_is_not_reported_as_delegated(tmp_path):
    r = _delegated_record(tmp_path, _deleg())
    assert r["valid"], r["errors"]
    assert "delegated" not in kinds(r)
    assert r["delegated_under"] == {"D1": "G1"}
    from cadl.decisions import format_verify_text
    assert "made within a delegation: G1: D1" in format_verify_text(r)


def test_a_decision_outside_its_delegation_is_reported(tmp_path):
    r = _delegated_record(tmp_path, None)
    assert [f["text"] for f in r["findings"] if f["finding"] == "delegated"] == [
        "'llm-decider' (llm) decided on behalf of 'operator', with no delegation"]
    r = _delegated_record(tmp_path, _deleg(procedures=["single"]))
    assert any("G1 does not give the procedure vote" in f["text"] for f in r["findings"])
    r = _delegated_record(tmp_path, _deleg(layers=["contract"]), procedure="override",
                          choice="P1.2")
    assert any("G1 does not give the layer rule" in f["text"] for f in r["findings"])
    r = _delegated_record(tmp_path, _deleg(layers=[]))
    assert any("G1 gives no layers" in f["text"] for f in r["findings"])
    r = _delegated_record(tmp_path, _deleg(point=1, until={"period": 1}))
    assert r["delegated_under"] == {"D1": "G1"}


def test_a_delegation_covers_the_points_up_to_its_until(tmp_path):
    # D1 is made at period 1: a delegation until period 3 covers it, one until period 0
    # has ended when D1 is made at period 2
    r = _delegated_record(tmp_path, _deleg(point=1, until={"period": 3}))
    assert r["delegated_under"] == {"D1": "G1"}
    r = _delegated_record(tmp_path, _deleg(point=1, until={"period": 1}), decision_period=2)
    assert r["delegated_under"] == {}
    assert any("G1 ended at period 1" in f["text"] for f in r["findings"])


def test_a_later_delegation_replaces_an_earlier_one(tmp_path):
    parties = PARTIES + [_party("llm-decider", "llm", ["decider"], model="claude-opus-5-5",
                                on_behalf_of="operator", layers=["rule", "contract"])]
    entries = period1()
    d = entries[10]
    d.update(by="llm-decider", choice="keep", procedure="vote", result="1", generated=GEN,
             **_ev("Keep."))
    entries = [_deleg("G1"), _deleg("G2", layers=[])] + entries[:11]
    import shutil
    shutil.copy(V2, tmp_path / V2.name)
    p = tmp_path / "decisions.jsonl"
    chain = DecisionChain(run_id=RUN_ID)
    lines = [chain.header({"name": "RobotTown", "version": "1",
                           "source_sha256": file_sha256(V1)}, parties, T)]
    lines += [chain.entry(e) for e in entries] + [chain.end()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = verify(p, versions=[V1])
    assert r["valid"], r["errors"]
    assert any("G2 gives no layers" in f["text"] for f in r["findings"])


def test_a_bad_delegation_is_an_error(tmp_path):
    assert "L-8" in rules(_delegated_record(tmp_path, _deleg(by="regulator")))
    assert "L-5" in rules(_delegated_record(tmp_path, _deleg(by="llm")))
    assert "L-8" in rules(_delegated_record(tmp_path, _deleg(layers=["regulation"])))
    assert "L-1" in rules(_delegated_record(tmp_path, _deleg(layers=["road"])))
    assert "L-1" in rules(_delegated_record(tmp_path, _deleg(procedures=["ask"])))
    assert "L-1" in rules(_delegated_record(tmp_path, _deleg(to="nobody")))
    assert "L-8" in rules(_delegated_record(tmp_path, _deleg(reason=" ")))
    assert "L-1" in rules(_delegated_record(tmp_path, _deleg(until={"period": 0})))


def test_a_delegation_is_not_an_entry_of_decisions_0_2(tmp_path):
    r = _delegated_record(tmp_path, _deleg(), fmt=FORMAT_2)
    assert "L-1" in rules(r)
    r = _delegated_record(tmp_path, None, fmt=FORMAT_2)
    assert r["valid"], r["errors"]
    assert "delegated" in kinds(r)
