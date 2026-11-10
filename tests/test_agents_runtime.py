"""Runtime of the agent governance extension (Appendix G, G.5 to G.8):
`cadl agents-run`. Decisions on the state left by earlier messages, holds
and their resolution, delegation with provenance, audit records.

The expected outcomes of the scenario tests were derived by hand from the
text of the appendix; each test names the passage it follows."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cadl.agents import build_policy, decide
from cadl.agents_runtime import audit_lines, format_text, parse_run_trace, run, run_files
from cadl.cli import main
from cadl.codegen import generate
from cadl.norms_eval import NormsEvalError, TraceError
from cadl.parser import parse, parse_file
from cadl.type_checker import type_check

from .test_agents_extension import ESCALATION, RICH, norms, perm, proh, source

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "agents_tool_governance.cadl"
CHANGE = ROOT / "examples" / "agents_change_control.cadl"
TRACES = ROOT / "examples" / "traces"
EXAMPLE_TRACES = sorted(TRACES.glob("agents_tool_governance_*.jsonl"))
CHANGE_TRACES = sorted(TRACES.glob("agents_change_control_*.jsonl"))


def m(sender: str, receiver: str, name: str, *args) -> dict:
    out = {"from": sender, "to": receiver, "name": name}
    if args:
        out["args"] = list(args)
    return out


def trace(*rows, **first) -> list[str]:
    """Lines of a trace: each row is (t, message or list of messages or
    None, further keys)."""
    out = []
    for k, row in enumerate(rows):
        t, msgs = row[0], row[1]
        line = {"t": t}
        if k == 0:
            line.update(first)
        if msgs:
            line["messages"] = msgs if isinstance(msgs, list) else [msgs]
        for more in row[2:]:
            line.update(more)
        out.append(json.dumps(line))
    return out


def replay(src, lines, versions=None) -> dict:
    sos = parse(src) if isinstance(src, str) else src
    res = type_check(sos)
    assert res.ok, [str(e) for e in res.errors]
    return run(sos, parse_run_trace(lines), versions)


def short(rec: dict) -> tuple:
    """An audit record reduced to what the scenarios compare."""
    msg = rec["message"]
    text = None if msg is None else f"{msg['from']}>{msg['to']}:{msg['name']}"
    if "decision" in rec:
        norm_ids = [n.split(".", 1)[1] for n in rec["norms"]]
        out = (rec["time"], text, rec["decision"], rec["reason"], norm_ids)
        if rec["hold_id"] is not None:
            out += (rec["hold_id"], rec["outcome"])
        return out
    detail = rec["reason"] if rec["reason"] else rec["holders"]
    return (rec["time"], text, rec["event"], detail)


def decisions(result: dict) -> list[tuple]:
    return [short(r) for r in result["audit"] if "decision" in r]


def events(result: dict) -> list[tuple]:
    return [short(r) for r in result["audit"] if "event" in r]


def norm_states(result: dict, instance: int = 0) -> dict:
    return {n["id"]: (n["state"], n["verdict"]) for n in result["instances"][instance]["norms"]}


# === The bundled example (G.12) ===============================================

class TestExampleOfTheAppendix:
    """examples/agents_tool_governance.cadl on the traces of examples/traces."""

    def run(self, name: str) -> dict:
        return run_files(EXAMPLE, TRACES / name)

    def test_a_three_calls_of_g12(self):
        # G.12: grants, call 1 through a chain, call 2 escalated and
        # approved, call 3 denied after a revocation.
        r = self.run("agents_tool_governance_a_approved.jsonl")
        T = "2026-11-10T09:"
        assert [short(x) for x in r["audit"]] == [
            (T + "00:00Z", "OPERATOR>PLANNER:grant_read", "allow", "default", []),
            (T + "00:00Z", "OPERATOR>PLANNER:grant_read", "grant", ["PLANNER"]),
            (T + "00:02Z", "PLANNER>SUMMARIZER:grant_read", "allow", "default", []),
            (T + "00:02Z", "PLANNER>SUMMARIZER:grant_read", "grant", ["SUMMARIZER"]),
            # "A further SUMMARIZER -> PLANNER : grant_read would be recorded
            # as unauthorised: PLANNER occurs in the chain (condition 3)".
            (T + "00:03Z", "SUMMARIZER>PLANNER:grant_read", "allow", "default", []),
            (T + "00:03Z", "SUMMARIZER>PLANNER:grant_read", "unauthorised_grant", "in_chain"),
            (T + "00:05Z", "SUMMARIZER>FILES:read_file", "allow", "permission", ["may_read"]),
            (T + "03:00Z", "PLANNER>FILES:delete_file", "escalate", "prohibition",
             ["no_delete"], "h-1", None),
            (T + "03:40Z", "OPERATOR>PLANNER:approve", "allow", "default", []),
            (T + "03:40Z", "PLANNER>FILES:delete_file", "allow", "approved",
             ["no_delete"], "h-1", "approved"),
            (T + "10:00Z", "OPERATOR>PLANNER:revoke_read", "allow", "default", []),
            (T + "10:00Z", "OPERATOR>PLANNER:revoke_read", "revoke", ["PLANNER", "SUMMARIZER"]),
            (T + "10:30Z", "SUMMARIZER>FILES:read_file", "deny", "default", []),
        ]
        by_time = {x["time"]: x for x in r["audit"] if "decision" in x}
        assert by_time[T + "00:05Z"]["provenance"] == [["OPERATOR", "PLANNER", "SUMMARIZER"]]
        # The audit record written out in G.12, field by field and in order.
        assert json.dumps(r["audit"][7]) == json.dumps({
            "time": "2026-11-10T09:03:00Z",
            "message": {"from": "PLANNER", "to": "FILES", "name": "delete_file"},
            "instances": [{"contract": "ASSISTED_REPORTING", "instance": "default"}],
            "decision": "escalate", "reason": "prohibition",
            "contracts": ["ASSISTED_REPORTING"],
            "norms": ["ASSISTED_REPORTING.no_delete"], "provenance": [],
            "escalate_to": "OPERATOR", "timeout": 600000,
            "fallback": ["PLANNER -> TICKETS : create_ticket"], "notify": ["OPERATOR"],
            "hold_id": "h-1", "outcome": None})
        # "The hold h-1 is created with the approver OPERATOR and the
        # deadline 09:13:00."
        assert r["holds"] == [{
            "hold_id": "h-1", "state": "approved",
            "message": {"from": "PLANNER", "to": "FILES", "name": "delete_file"},
            "time": T + "03:00Z", "approver": "OPERATOR", "deadline": T + "13:00Z",
            "contracts": ["ASSISTED_REPORTING"], "norms": ["ASSISTED_REPORTING.no_delete"],
            "closed": T + "03:40Z", "released": True}]
        # "It is not an occurrence of the content of no_delete, which stays
        # active": no violation; at the end of the instance it is fulfilled.
        assert norm_states(r)["no_delete"] == ("fulfilled", "fulfilled")
        assert r["instances"][0]["violations"] == []
        assert [(d["holder"], d["state"]) for d in r["delegations"]] == [
            ("PLANNER", "discharged"), ("SUMMARIZER", "discharged")]

    def test_b_rejected_then_fallback(self):
        # G.12: "Had the operator sent reject("h-1") ... the closing record
        # would be a deny with the reason rejected; the planner would find
        # the fallback in the result, and may_file_ticket allows that call."
        r = self.run("agents_tool_governance_b_rejected.jsonl")
        T = "2026-11-10T09:00:"
        assert decisions(r) == [
            (T + "00Z", "PLANNER>FILES:delete_file", "escalate", "prohibition",
             ["no_delete"], "h-1", None),
            (T + "10Z", "OPERATOR>PLANNER:reject", "allow", "default", []),
            (T + "10Z", "PLANNER>FILES:delete_file", "deny", "rejected",
             ["no_delete"], "h-1", "rejected"),
            (T + "20Z", "PLANNER>TICKETS:create_ticket", "allow", "permission",
             ["may_file_ticket"]),
            # "An approval releases one message; a later message with the
            # same sender, receiver, and name is decided again." The trace
            # ends here: the instance ends and the hold is cancelled.
            (T + "30Z", "PLANNER>FILES:delete_file", "escalate", "prohibition",
             ["no_delete"], "h-2", None),
            (T + "30Z", "PLANNER>FILES:delete_file", "deny", "cancelled",
             ["no_delete"], "h-2", "cancelled"),
        ]
        closing = r["audit"][2]
        assert closing["fallback"] == ["PLANNER -> TICKETS : create_ticket"]
        assert closing["notify"] == ["OPERATOR"] and closing["timeout"] is None
        assert closing["escalate_to"] == "OPERATOR"
        assert events(r) == []

    def test_c_timeout_and_undeclared_tool(self):
        # G.6 Expiry: "Before each decision and each observation whose time
        # is later than the deadline of a held hold, that hold becomes
        # expired"; closing record "deny, reason expired, with the deadline
        # as time".
        r = self.run("agents_tool_governance_c_timeout.jsonl")
        T = "2026-11-10T09:"
        assert [short(x) for x in r["audit"]] == [
            (T + "00:00Z", "PLANNER>FILES:delete_file", "escalate", "prohibition",
             ["no_delete"], "h-1", None),
            (T + "00:05Z", "PLANNER>FILES:format_disk", "deny", "undeclared_tool", []),
            (T + "10:00Z", "PLANNER>FILES:delete_file", "deny", "expired",
             ["no_delete"], "h-1", "expired"),
            (T + "10:01Z", "PLANNER>TICKETS:search_tickets", "allow", "permission",
             ["may_search"]),
            # An approval that names a hold that is not held "changes no
            # hold and SHOULD be recorded as unauthorised".
            (T + "11:40Z", "OPERATOR>PLANNER:approve", "allow", "default", []),
            (T + "11:40Z", "OPERATOR>PLANNER:approve", "unauthorised_approval", "not_held"),
            (T + "11:50Z", "PLANNER>TICKETS:create_ticket", "allow", "permission",
             ["may_file_ticket"]),
        ]
        assert r["audit"][1]["contracts"] == [] and r["audit"][1]["instances"] == [
            {"contract": "ASSISTED_REPORTING", "instance": "default"}]
        assert r["audit"][2]["fallback"] == ["PLANNER -> TICKETS : create_ticket"]
        assert r["summary"]["holds"]["expired"] == 1 and r["summary"]["released"] == 0

    def test_approval_at_exactly_the_deadline_is_in_time(self):
        # G.6: "An approval at exactly the deadline is in time."
        r = replay(parse_file(EXAMPLE), trace(
            (0, m("PLANNER", "FILES", "delete_file")),
            (600, m("OPERATOR", "PLANNER", "approve", "h-1")),
            (600.5, None, {"end": False})))
        assert decisions(r)[-1] == (600, "PLANNER>FILES:delete_file", "allow", "approved",
                                    ["no_delete"], "h-1", "approved")

    def test_grant_conditions_on_the_example(self):
        # G.7, conditions 1, 5 and 2 of a grant.
        r = replay(parse_file(EXAMPLE), trace(
            (0, m("SUMMARIZER", "PLANNER", "grant_read")),      # 1: no authority
            (1, m("OPERATOR", "PLANNER", "grant_read")),
            (2, m("OPERATOR", "PLANNER", "grant_read")),        # 5 fails: a no-op
            (3, m("PLANNER", "SUMMARIZER", "grant_read")),
            (4, m("SUMMARIZER", "OPERATOR", "grant_read")),     # 2: not in to:
            (5, m("PLANNER", "FILES", "read_file")),
            (6, m("SUMMARIZER", "PLANNER", "revoke_read")),     # not before PLANNER
            (7, m("PLANNER", "SUMMARIZER", "revoke_read")),     # one link only
            (8, m("SUMMARIZER", "FILES", "read_file")),
            (9, m("PLANNER", "FILES", "read_file"))))
        assert events(r) == [
            (0, "SUMMARIZER>PLANNER:grant_read", "unauthorised_grant", "no_authority"),
            (1, "OPERATOR>PLANNER:grant_read", "grant", ["PLANNER"]),
            (2, "OPERATOR>PLANNER:grant_read", "grant", "holder_exists"),
            (3, "PLANNER>SUMMARIZER:grant_read", "grant", ["SUMMARIZER"]),
            (4, "SUMMARIZER>OPERATOR:grant_read", "unauthorised_grant", "not_in_to"),
            (6, "SUMMARIZER>PLANNER:revoke_read", "revoke", "no_effect"),
            (7, "PLANNER>SUMMARIZER:revoke_read", "revoke", ["SUMMARIZER"]),
        ]
        d = decisions(r)
        assert d[5][2:4] == ("allow", "permission")
        assert d[8][2:4] == ("deny", "default")       # SUMMARIZER after the revocation
        assert d[9][2:4] == ("allow", "permission")   # the granter keeps its instance
        assert r["audit"][-1]["provenance"] == [["OPERATOR", "PLANNER"]]


# === The second example: a non-static prohibition, max_depth, until ===============

class TestChangeControl:
    def test_example_checks(self):
        res = type_check(parse_file(CHANGE))
        assert res.ok
        assert len(res.warnings) == 1 and "(N-12)" in str(res.warnings[0])

    def test_a_freeze(self):
        # G.5: a prohibition is in force while its instance is active: from
        # its trigger to its until. G.6: approval, rejection, expiry.
        r = run_files(CHANGE, TRACES / "agents_change_control_a_freeze.jsonl")
        push = "LEAD>REPO:push_change"
        assert decisions(r) == [
            (0, push, "allow", "permission", ["may_push"]),
            (10, "OWNER>LEAD:freeze", "allow", "default", []),
            (20, push, "escalate", "prohibition", ["no_push_in_freeze"], "h-1", None),
            (30, "LEAD>REPO:force_push", "deny", "prohibition", ["no_force_push"]),
            (40, "OWNER>LEAD:approve", "allow", "default", []),
            (40, push, "allow", "approved", ["no_push_in_freeze"], "h-1", "approved"),
            (50, push, "escalate", "prohibition", ["no_push_in_freeze"], "h-2", None),
            (60, "OWNER>LEAD:reject", "allow", "default", []),
            (60, push, "deny", "rejected", ["no_push_in_freeze"], "h-2", "rejected"),
            (70, push, "escalate", "prohibition", ["no_push_in_freeze"], "h-3", None),
            (370, push, "deny", "expired", ["no_push_in_freeze"], "h-3", "expired"),
            (400, "OWNER>LEAD:thaw", "allow", "default", []),
            (410, push, "allow", "permission", ["may_push"]),
            (420, "LEAD>REPO:open_issue", "allow", "permission", ["may_open_issue"]),
            (430, "WORKER[1]>REPO:delete_branch", "deny", "undeclared_tool", []),
        ]
        assert events(r) == []
        esc = r["audit"][2]
        assert (esc["escalate_to"], esc["timeout"], esc["fallback"], esc["notify"]) == \
            ("OWNER", 300000, ["LEAD -> REPO : open_issue"], ["OWNER"])
        plain = r["audit"][3]
        assert (plain["escalate_to"], plain["timeout"], plain["fallback"]) == (None, None, [])
        # The released push did not violate the prohibition; `until:` ended it.
        assert norm_states(r)["no_push_in_freeze"] == ("discharged", "fulfilled")
        assert r["instances"][0]["violations"] == []
        # The in-force flag of the only norm that is not static, per step.
        flags = [s["input"]["instances"][0]["in_force"] for s in r["steps"]]
        assert flags == [{"no_push_in_freeze": v} for v in
                         [False, False, True, True, True, True, True, True, True, False,
                          False, False]]

    def test_b_delegation(self):
        # G.7: max_depth 1 (condition 4), a revocation by an actor that is
        # not in the chain, and the `until:` of the entry.
        r = run_files(CHANGE, TRACES / "agents_change_control_b_delegation.jsonl")
        read = ">REPO:read_repo"
        assert [x[1:4] for x in decisions(r)] == [
            ("WORKER[1]" + read, "deny", "default"),
            ("OWNER>LEAD:grant_read", "allow", "default"),
            ("LEAD>WORKER[1]:grant_read", "allow", "default"),
            ("OWNER>WORKER[1]:grant_read", "allow", "default"),
            ("WORKER[1]" + read, "allow", "permission"),
            ("WORKER[2]" + read, "deny", "default"),
            ("LEAD>WORKER[1]:revoke_read", "allow", "default"),
            ("WORKER[1]" + read, "allow", "permission"),
            ("OWNER>LEAD:close_review", "allow", "default"),
            ("WORKER[1]" + read, "deny", "default"),
            ("LEAD" + read, "deny", "default"),
            ("OWNER>WORKER[1]:grant_read", "allow", "default"),
            ("WORKER[1]" + read, "allow", "permission"),
        ]
        assert events(r) == [
            (1, "OWNER>LEAD:grant_read", "grant", ["LEAD"]),
            (2, "LEAD>WORKER[1]:grant_read", "unauthorised_grant", "max_depth"),
            (3, "OWNER>WORKER[1]:grant_read", "grant", ["WORKER[1]"]),
            (6, "LEAD>WORKER[1]:revoke_read", "revoke", "no_effect"),
            # A discharged instance is not revived; a new grant creates a new one.
            (11, "OWNER>WORKER[1]:grant_read", "grant", ["WORKER[1]"]),
        ]
        assert r["audit"][7]["provenance"] == [["OWNER", "WORKER[1]"]]
        ended = [(d["holder"], d["state"], d["ended"], d["end_reason"])
                 for d in r["delegations"]]
        assert ended[:2] == [
            ("LEAD", "discharged", 8, "until occurred: OWNER -> LEAD : close_review"),
            ("WORKER[1]", "discharged", 8, "until occurred: OWNER -> LEAD : close_review")]
        assert ended[2][:3] == ("WORKER[1]", "discharged", 12)    # the instance ended


# === Two contracts, actor sets, bindings (the RICH file) ==========================

class TestTwoContracts:
    def test_one_instance_each(self):
        # Derived by hand from G.5 for the file RICH with i = 2.
        r = replay(RICH, trace(
            (0, m("AG", "T", "read")),
            (1, m("AG", "M", "ping")),
            (2, m("OP", "AG", "freeze")),
            (3, m("AG", "M", "ping")),
            (4, m("AG", "T", "write")),
            (5, m("R[2]", "U", "write")),
            (6, m("R[1]", "U", "write")),
            (7, m("R[2]", "U", "wipe")),
            (8, m("AG", "T", "wipe")),
            (9, m("OP", "AG", "grant_write")),
            (10, m("AG", "SUB", "grant_write")),
            (11, m("SUB", "U", "write")),
            (12, m("OP", "AG", "stop")),
            (13, m("OP", "SUB", "revoke_write")),
            (14, m("SUB", "U", "write")),
            (15, m("AG", "U", "write")),
            (16, m("AG", "T", "write")),
            bindings={"i": 2}))
        got = [(x["time"], x["decision"], x["reason"], x["contracts"], x["norms"])
               for x in r["audit"] if "decision" in x]
        assert got == [
            (0, "allow", "permission", ["C1", "C2"], ["C1.p_read", "C2.p_read2"]),
            (1, "allow", "default", ["C1"], []),          # `frozen` not yet triggered
            (2, "allow", "default", ["C1"], []),
            (3, "deny", "prohibition", ["C1"], ["C1.frozen"]),
            (4, "deny", "default", ["C2"], []),           # C1 allows, C2 has default: deny
            (5, "allow", "permission", ["C1"], ["C1.r_write"]),
            (6, "deny", "default", ["C1"], []),           # R[1] is not R[i]
            (7, "deny", "prohibition", ["C1"], ["C1.no_r_wipe"]),
            # C1 escalates to OP and C2 to SUP: a hold has a single approver.
            (8, "deny", "prohibition", ["C1", "C2"], ["C1.no_wipe", "C2.no_wipe2"]),
            (9, "allow", "default", ["C1"], []),
            (10, "allow", "default", ["C1"], []),
            (11, "allow", "permission", ["C1"], ["C1.p_op_write"]),
            (12, "allow", "default", ["C1"], []),
            (13, "allow", "default", ["C1"], []),
            (14, "deny", "default", ["C1"], []),
            (15, "allow", "permission", ["C1"], ["C1.p_op_write"]),
            # may_report was discharged by its until: at t=12; C2 denies anyway.
            (16, "deny", "default", ["C1", "C2"], []),
        ]
        both = r["audit"][8]
        assert (both["escalate_to"], both["timeout"], both["fallback"], both["notify"]) == \
            (None, 60000, ["AG -> T : read"], ["OP", "SUP"])
        assert r["holds"] == []
        chains = {x["time"]: x["provenance"] for x in r["audit"] if x.get("provenance")}
        assert chains == {11: [["OP", "AG", "SUB"]], 15: [["OP", "AG"]]}
        assert events(r) == [
            (9, "OP>AG:grant_write", "grant", ["AG"]),
            (10, "AG>SUB:grant_write", "grant", ["SUB"]),
            (13, "OP>SUB:revoke_write", "revoke", ["SUB"]),
        ]
        # The flags the runtime derived from the norms engine.
        c1 = [s["input"]["instances"][0] for s in r["steps"]]
        assert c1[0]["bindings"] == {"i": 2}
        assert [i["in_force"]["frozen"] for i in c1] == [False] * 3 + [True] * 14
        assert [i["in_force"]["may_report"] for i in c1] == [True] * 13 + [False] * 4
        assert all(i["in_force"]["r_write"] and i["in_force"]["no_r_wipe"] for i in c1)
        assert [len(i["derived"]) for i in c1] == [0] * 10 + [1, 2, 2, 2, 1, 1, 1]

    def test_host_created_instances(self):
        # G.5: "the running instances are those it has created and not
        # ended"; step 3.1: no running instance denies a tool call.
        r = replay(RICH, trace(
            (0, m("AG", "T", "read"), {"instances": [
                {"contract": "C1", "instance": "a", "bindings": {"i": 1}},
                {"contract": "C1", "instance": "b", "bindings": {"i": 2}}]}),
            (1, m("R[1]", "U", "write")),
            (2, m("R[3]", "U", "write")),
            (3, m("R[1]", "U", "write"), {"instances": [
                {"contract": "C2", "instance": "x"}]}),
            (4, None, {"instances": [{"contract": "C1", "instance": "a", "end": True}]}),
            (5, m("R[1]", "U", "write")),
            (6, m("R[2]", "U", "wipe")),
            (7, m("AG", "T", "read"))))
        got = [(x["time"], x["decision"], x["reason"], x["contracts"],
                [i["instance"] for i in x["instances"]])
               for x in r["audit"] if "decision" in x]
        assert got == [
            (0, "deny", "no_instance", ["C2"], ["a", "b"]),     # C2 has no instance yet
            (1, "allow", "permission", ["C1"], ["a", "b"]),
            (2, "deny", "default", ["C1"], ["a", "b"]),
            (3, "allow", "permission", ["C1"], ["a", "b"]),
            (5, "deny", "default", ["C1"], ["b"]),              # instance a has ended
            (6, "deny", "prohibition", ["C1"], ["b"]),
            (7, "allow", "permission", ["C1", "C2"], ["b", "x"]),
        ]
        ends = {(i["contract"], i["instance"]): (i["start"], i["end"]) for i in r["instances"]}
        assert ends == {("C1", "a"): (0, 4), ("C1", "b"): (0, 7), ("C2", "x"): (3, 7)}

    def test_escalation_in_one_of_two_contracts(self):
        # C2 has no running instance for a message it does not govern; the
        # hold has its ground in C1 only.
        old = 'content: "AG -> T : wipe"\n          on_denied: { escalate_to: OP'
        assert RICH.count(old) == 1
        src = RICH.replace(old, old.replace("AG -> T", "AG -> U"))
        r = replay(src, trace(
            (0, m("AG", "U", "wipe")),
            (1, m("SUP", "AG", "approve", "h-1")),      # C2 denies ordinary messages
            (2, m("AG", "OP", "approve", "h-1")),       # not the rule's message
            (3, m("OP", "AG", "approve", "h-2")),       # no such hold
            (4, m("OP", "AG", "approve", "h-1")),
            (5, None, {"end": False})))
        assert [short(x) for x in r["audit"]] == [
            (0, "AG>U:wipe", "escalate", "prohibition", ["no_wipe"], "h-1", None),
            (1, "SUP>AG:approve", "deny", "default", []),
            (2, "AG>OP:approve", "allow", "default", []),
            (3, "OP>AG:approve", "allow", "default", []),
            (3, "OP>AG:approve", "unauthorised_approval", "unknown_hold"),
            (4, "OP>AG:approve", "allow", "default", []),
            (4, "AG>U:wipe", "allow", "approved", ["no_wipe"], "h-1", "approved"),
        ]


# === Holds (G.6) =====================================================================

HOLDS = source(
    """
    counts_as:
      - { when: "OP -> AG : approve", creates: approval }
      - { when: "OP -> AG : reject", creates: rejection }
      - { when: "SUP -> AG : approve", creates: approval }
    """,
    norms(
        perm("p_write", "AG", "AG -> T : write"),
        proh("no_wipe", "AG", "AG -> T : wipe", "on_denied: { escalate_to: OP, timeout: 1min }"),
        proh("no_read", "AG", "AG -> T : read", "on_denied: { escalate_to: OP }"),
        proh("later", "AG", "AG -> T : wipe", 'trigger: "SUP -> AG : lock"'),
        proh("sub_gate", "SUB", "SUB -> T : read", "on_denied: { escalate_to: SUP }"),
        """
        - id: must_write
          kind: obligation
          bearer: AG
          trigger: "OP -> AG : go"
          deadline: 30s
          content: "AG -> T : wipe"
        """,
    ),
)


class TestHolds:
    def test_only_the_approver_approves(self):
        # G.6: "whose performer is the approver of that hold".
        r = replay(HOLDS, trace(
            (0, m("AG", "T", "wipe")),
            (1, m("SUP", "AG", "approve", "h-1")),
            (2, m("OP", "AG", "approve")),               # no hold id
            (3, m("OP", "AG", "approve", "h-1")),
            (4, m("OP", "AG", "reject", "h-1")),
            (5, None, {"end": False})))
        assert events(r) == [
            (1, "SUP>AG:approve", "unauthorised_approval", "not_approver"),
            (2, "OP>AG:approve", "unauthorised_approval", "unknown_hold"),
            (4, "OP>AG:reject", "unauthorised_rejection", "not_held"),
        ]
        assert [h["state"] for h in r["holds"]] == ["approved"]

    def test_hold_without_timeout_and_end_of_instance(self):
        # G.6: no deadline; "when a contract instance ends, the held holds
        # with a ground in it become cancelled".
        r = replay(HOLDS, trace(
            (0, m("AG", "T", "read")),
            (100000, None),
            (100001, m("AG", "T", "write"), {"end": True})))
        assert r["holds"][0]["deadline"] is None
        assert decisions(r) == [
            (0, "AG>T:read", "escalate", "prohibition", ["no_read"], "h-1", None),
            (100001, "AG>T:write", "allow", "permission", ["p_write"]),
            (100001, "AG>T:read", "deny", "cancelled", ["no_read"], "h-1", "cancelled"),
        ]

    def test_a_prefix_leaves_the_hold_held(self):
        r = replay(HOLDS, trace((0, m("AG", "T", "read")), (1, None, {"end": False})))
        assert [h["state"] for h in r["holds"]] == ["held"]
        assert r["holds"][0]["closed"] is None
        assert len(r["audit"]) == 1 and r["instances"][0]["running"]

    def test_holds_expire_in_the_order_of_their_deadlines(self):
        # h-1 (grounds a and b, 2 min) is older than h-2 (ground g, 1 s) but
        # expires later. Found by the random traces: the runtime first wrote
        # the two closing records in the order of creation.
        r = replay(ESCALATION, trace(
            (0, [m("AG", "T", "wipe"), m("R[2]", "T", "read")]),
            (1000, None, {"end": False}), bindings={"i": 2}))
        assert [(x["hold_id"], x["time"]) for x in r["audit"] if x.get("outcome")] == [
            ("h-2", 1), ("h-1", 120)]

    def test_recheck_denies_on_a_new_prohibition(self):
        # G.6 Release: denied with "prohibition (with that prohibition in
        # norms) if a prohibition outside the grounds of the hold now
        # applies"; the outcome of the hold is still `approved`.
        r = replay(HOLDS, trace(
            (0, m("AG", "T", "wipe")),
            (1, m("SUP", "AG", "lock")),
            (2, m("OP", "AG", "approve", "h-1")),
            (3, None, {"end": False})))
        assert decisions(r)[-1] == (2, "AG>T:wipe", "deny", "prohibition", ["later"],
                                    "h-1", "approved")
        last = r["audit"][-1]
        assert (last["escalate_to"], last["timeout"]) == (None, None)
        assert (r["holds"][0]["state"], r["holds"][0]["released"]) == ("approved", False)

    def test_two_holds_are_released_in_the_order_of_creation(self):
        r = replay(HOLDS, trace(
            (0, [m("AG", "T", "read"), m("AG", "T", "wipe")]),
            (1, [m("OP", "AG", "approve", "h-2"), m("OP", "AG", "approve", "h-1")]),
            (2, None, {"end": False})))
        assert [x[1:] for x in decisions(r)[2:]] == [
            ("OP>AG:approve", "allow", "default", []),
            ("AG>T:wipe", "allow", "approved", ["no_wipe"], "h-2", "approved"),
            ("OP>AG:approve", "allow", "default", []),
            ("AG>T:read", "allow", "approved", ["no_read"], "h-1", "approved"),
        ]

    def test_released_message_is_an_observation_and_fulfils(self):
        # G.6: the released message "takes effect as the immediately
        # following observation"; here it fulfils an obligation, and it
        # does not violate its ground.
        r = replay(HOLDS, trace(
            (0, m("OP", "AG", "go")),
            (5, m("AG", "T", "wipe")),
            (10, m("OP", "AG", "approve", "h-1")),
            (100, None)))
        states = norm_states(r)
        assert states["must_write"] == ("fulfilled", "fulfilled")
        assert states["no_wipe"] == ("fulfilled", "fulfilled")
        assert r["instances"][0]["violations"] == []

    @pytest.mark.parametrize("answer, verdict", [
        (None, "excused"),                                    # held until the deadline
        (m("OP", "AG", "reject", "h-1"), "excused"),          # rejected
    ])
    def test_obligation_the_contract_itself_blocked_is_excused(self, answer, verdict):
        # G.5: a violated obligation is excused when "a message of its
        # bearer that matches its content was denied with the obligation's
        # own contract among the determining contracts, or was held on a
        # ground of that contract and not released before the violation".
        r = replay(HOLDS, trace(
            (0, m("OP", "AG", "go")),
            (5, m("AG", "T", "wipe")),
            (10, answer),
            (100, None)))
        assert norm_states(r)["must_write"] == ("violated", verdict)
        assert [v["verdict"] for v in r["instances"][0]["violations"]] == [verdict]

    def test_obligation_not_attempted_is_violated(self):
        r = replay(HOLDS, trace((0, m("OP", "AG", "go")), (100, m("AG", "T", "wipe")),
                                (101, None)))
        # The attempt at t=100 comes after the deadline (t=30): no excuse.
        assert norm_states(r)["must_write"] == ("violated", "violated")

    def test_denied_message_is_not_an_observation(self):
        # G.5: a denied message "triggers no norm".
        src = source(norms(
            proh("no_go", "AG", "AG -> SUB : go"),
            proh("armed", "SUB", "SUB -> T : read", 'trigger: "AG -> SUB : go"'),
            perm("p", "SUB", "SUB -> T : read")))
        r = replay(src, trace((0, m("AG", "SUB", "go")), (1, m("SUB", "T", "read"))))
        assert [x[2:4] for x in decisions(r)] == [("deny", "prohibition"),
                                                  ("allow", "permission")]
        assert norm_states(r)["armed"] == ("discharged", "fulfilled")    # never activated

    def test_violated_prohibition_stays_in_force(self):
        # G.5: "When the instance becomes violated, the prohibition stays
        # in force ... until the first of: the time at which its deadline
        # window would have closed". The trigger of `once` is its own
        # content, so the triggering message violates it.
        src = source(norms(
            """
            - id: once
              kind: prohibition
              bearer: AG
              trigger: "AG -> SUB : ping"
              deadline: 60s
              content: "AG -> SUB : ping"
            """))
        r = replay(src, trace(
            (0, m("AG", "SUB", "ping")),
            (30, m("AG", "SUB", "ping")),
            (60, m("AG", "SUB", "ping")),
            (61, m("AG", "SUB", "ping"))))
        assert [x[2:4] for x in decisions(r)] == [
            ("allow", "default"), ("deny", "prohibition"), ("deny", "prohibition"),
            ("allow", "default")]
        assert norm_states(r)["once"] == ("violated", "violated")
        assert len(r["instances"][0]["violations"]) == 1      # reported once (F.5)

    def test_window_of_a_prohibition_closes_before_the_decision(self):
        # The time of a line is observed before its messages are decided.
        src = source(norms(
            """
            - id: quiet
              kind: prohibition
              bearer: AG
              deadline: 10s
              content: "AG -> SUB : ping"
            """))
        r = replay(src, trace((0, None), (10, m("AG", "SUB", "ping")),
                              (11, m("AG", "SUB", "ping"))))
        assert [x[2:4] for x in decisions(r)] == [("deny", "prohibition"),
                                                  ("allow", "default")]
        assert norm_states(r)["quiet"] == ("fulfilled", "fulfilled")


# === Delegation (G.7) ==================================================================

DELEG = source(
    """
    counts_as:
      - { when: "OP -> AG : halt", creates: halted }
    """,
    norms(
        perm("p_read", "OP", "OP -> T : read"),
        """
        - id: p_write
          kind: permission
          bearer: OP
          until: "OP -> AG : halt"
          content: "OP -> T : write"
        - id: pause
          kind: power
          bearer: SUP
          content: "SUP -> OP : pause"
          effects: { suspend: [p_write] }
        - id: unpause
          kind: power
          bearer: SUP
          content: "SUP -> OP : unpause"
          effects: { resume: [p_write] }
        """,
    ),
    """
    delegations:
      - { norm: p_read, to: [AG, SUB, "R[*]"], grant: grant_read, revoke: revoke_read, max_depth: 3 }
      - { norm: p_write, to: [AG], grant: grant_write }
    """,
)


class TestDelegation:
    def test_revoking_one_link_ends_everything_derived_through_it(self):
        r = replay(DELEG, trace(
            (0, m("OP", "AG", "grant_read")),
            (1, m("AG", "SUB", "grant_read")),
            (2, m("SUB", "R[2]", "grant_read")),
            (3, m("R[2]", "R[1]", "grant_read")),       # condition 4: three links already
            (4, m("OP", "R[3]", "grant_read")),
            (5, m("R[2]", "T", "read")),
            (6, m("AG", "SUB", "revoke_read")),
            (7, m("R[2]", "T", "read")),
            (8, m("AG", "T", "read")),
            (9, m("R[3]", "T", "read")),
            (10, m("R[7]", "T", "read"))))
        assert events(r) == [
            (0, "OP>AG:grant_read", "grant", ["AG"]),
            (1, "AG>SUB:grant_read", "grant", ["SUB"]),
            (2, "SUB>R[2]:grant_read", "grant", ["R[2]"]),
            (3, "R[2]>R[1]:grant_read", "unauthorised_grant", "max_depth"),
            (4, "OP>R[3]:grant_read", "grant", ["R[3]"]),
            (6, "AG>SUB:revoke_read", "revoke", ["SUB", "R[2]"]),
        ]
        d = {x[0]: x[2:4] for x in decisions(r) if x[1].endswith(">T:read")}
        assert d == {5: ("allow", "permission"), 7: ("deny", "default"),
                     8: ("allow", "permission"), 9: ("allow", "permission"),
                     10: ("deny", "default")}
        assert r["audit"][10]["provenance"] == [["OP", "AG", "SUB", "R[2]"]]

    def test_suspended_original_and_discharge(self):
        # G.7: "While the original is suspended, its derived instances are
        # not valid"; end (c): "the original permission becomes discharged".
        r = replay(DELEG, trace(
            (0, m("OP", "AG", "grant_write")),
            (1, m("AG", "T", "write")),
            (2, m("SUP", "OP", "pause")),
            (3, m("AG", "T", "write")),
            (4, m("OP", "AG", "grant_write")),          # the bearer, not in force
            (5, m("SUP", "OP", "unpause")),
            (6, m("AG", "T", "write")),
            (7, m("OP", "AG", "halt")),
            (8, m("AG", "T", "write")),
            (9, m("OP", "AG", "grant_write"))))
        d = {x[0]: x[2:4] for x in decisions(r) if x[1] == "AG>T:write"}
        assert d == {1: ("allow", "permission"), 3: ("deny", "default"),
                     6: ("allow", "permission"), 8: ("deny", "default")}
        assert events(r) == [
            (0, "OP>AG:grant_write", "grant", ["AG"]),
            (4, "OP>AG:grant_write", "unauthorised_grant", "no_authority"),
            (9, "OP>AG:grant_write", "unauthorised_grant", "no_authority"),
        ]
        only = r["delegations"][0]
        assert (only["state"], only["ended"]) == ("discharged", 7)
        assert only["end_reason"] == "the original permission was discharged"

    def test_revoke_without_a_revoke_name_is_an_ordinary_message(self):
        r = replay(DELEG, trace((0, m("OP", "AG", "grant_write")),
                                (1, m("OP", "AG", "revoke_write")),
                                (2, m("AG", "T", "write"))))
        assert [e[2] for e in events(r)] == ["grant"]
        assert decisions(r)[-1][2:4] == ("allow", "permission")


# === Amendment (F.6, G.7) =================================================================

class TestAmendment:
    def test_amendment_cancels_holds_and_tests_delegations(self):
        # G.7: "(1) every held hold with a ground in that contract becomes
        # cancelled, and (2) ... an instance whose permission has no
        # delegations: entry there ... is discharged".
        v1 = parse(DELEG.replace(
            "norms:\n", "norms:\n"
            "        - { id: no_wipe, kind: prohibition, bearer: AG, "
            'content: "AG -> T : wipe", on_denied: { escalate_to: OP } }\n', 1).replace(
            "counts_as:\n", "counts_as:\n"
            '        - { when: "OP -> AG : approve", creates: approval }\n', 1))
        assert type_check(v1).ok, [str(e) for e in type_check(v1).errors]
        v2 = parse(DELEG.replace(
            '      - { norm: p_write, to: [AG], grant: grant_write }\n', ""))
        assert type_check(v2).ok
        r = replay(v1, trace(
            (0, [m("OP", "AG", "grant_write"), m("OP", "AG", "grant_read")]),
            (1, [m("AG", "T", "wipe"), m("AG", "T", "write")]),
            (2, [m("AG", "T", "write"), m("AG", "T", "read"), m("AG", "T", "wipe")],
             {"amend": "v2"})), versions={"v2": v2})
        assert decisions(r) == [
            (0, "OP>AG:grant_write", "allow", "default", []),
            (0, "OP>AG:grant_read", "allow", "default", []),
            (1, "AG>T:wipe", "escalate", "prohibition", ["no_wipe"], "h-1", None),
            (1, "AG>T:write", "allow", "permission", ["p_write"]),
            (2, "AG>T:wipe", "deny", "cancelled", ["no_wipe"], "h-1", "cancelled"),
            (2, "AG>T:write", "deny", "default", []),
            (2, "AG>T:read", "allow", "permission", ["p_read"]),
            (2, "AG>T:wipe", "deny", "default", []),      # no_wipe is gone in v2
        ]
        assert r["amendments"] == [{"time": 2, "file": "v2", "contracts": ["C"]}]
        ended = {d["norm"]: d["state"] for d in r["delegations"]
                 if d["ended"] == 2 and "amendment" in d["end_reason"]}
        assert ended == {"C.p_write": "discharged"}

    def test_enforced_contracts_cannot_be_added_or_removed(self):
        v2 = parse(source(enforcement=None))
        with pytest.raises(NormsEvalError, match="other enforced contracts"):
            replay(DELEG, trace((0, None), (1, None, {"amend": "v2"})), versions={"v2": v2})


# === Agreement with the decision function and with the generated Rego ========================

def scenario_results() -> list[tuple[str, object, dict]]:
    """Runs whose steps are replayed through `decide` and through OPA."""
    out = []
    for path in EXAMPLE_TRACES:
        out.append((path.name, parse_file(EXAMPLE), run_files(EXAMPLE, path)))
    for path in CHANGE_TRACES:
        out.append((path.name, parse_file(CHANGE), run_files(CHANGE, path)))
    rich = parse(RICH)
    out.append(("rich", rich, run(rich, parse_run_trace(trace(
        (0, [m("AG", "T", "read"), m("AG", "M", "ping"), m("OP", "AG", "freeze"),
             m("AG", "M", "ping"), m("AG", "T", "write"), m("R[2]", "U", "write"),
             m("R[1]", "U", "write"), m("R[2]", "U", "wipe"), m("AG", "T", "wipe"),
             m("OP", "AG", "grant_write"), m("AG", "SUB", "grant_write"),
             m("SUB", "U", "write"), m("OP", "AG", "stop"), m("OP", "SUB", "revoke_write"),
             m("SUB", "U", "write"), m("AG", "U", "write"), m("X", "T", "read"),
             m("GHOST", "FREE", "read"), m("AG", "T", "bogus")]),
        bindings={"i": 2})))))
    out.append(("rich-instances", rich, run(rich, parse_run_trace(trace(
        (0, [m("AG", "T", "read"), m("R[1]", "U", "write")], {"instances": [
            {"contract": "C1", "instance": "a", "bindings": {"i": 1}},
            {"contract": "C1", "instance": "b", "bindings": {"i": 2}}]}),
        (1, [m("R[2]", "U", "wipe"), m("R[3]", "U", "write"), m("AG", "T", "wipe")],
         {"instances": [{"contract": "C2", "instance": "x"}]}),
        (2, [m("R[1]", "U", "write"), m("OP", "AG", "freeze"), m("AG", "M", "ping")],
         {"instances": [{"contract": "C1", "instance": "a", "end": True}]}))))))
    esc = parse(ESCALATION)
    out.append(("escalation", esc, run(esc, parse_run_trace(trace(
        (0, [m("AG", "T", "wipe"), m("AG", "T", "write"), m("AG", "T", "read"),
             m("R[2]", "T", "read"), m("R[1]", "T", "read")]),
        (1, [m("OP", "AG", "approve", "h-1"), m("R[1]", "AG", "approve", "h-2")]),
        bindings={"i": 2})))))
    holds = parse(HOLDS)
    out.append(("holds", holds, run(holds, parse_run_trace(trace(
        (0, [m("OP", "AG", "go"), m("AG", "T", "wipe"), m("SUP", "AG", "lock"),
             m("AG", "T", "wipe"), m("OP", "AG", "approve", "h-1")]))))))
    return out


class TestAgreement:
    def test_every_step_agrees_with_the_decision_function(self):
        # The runtime decides with `decide`; a step replayed with the
        # state the runtime computed gives the decision of the audit log.
        total = 0
        for name, sos, result in scenario_results():
            policy = build_policy(sos)
            records = [r for r in result["audit"]
                       if "decision" in r and r["outcome"] is None]
            assert len(records) == len(result["steps"]), name
            for step, rec in zip(result["steps"], records):
                again = decide(policy, json.loads(json.dumps(step["input"])))
                assert again == step["result"], (name, step["message"])
                assert {k: rec[k] for k in again} == again, (name, step["message"])
                assert rec["hold_id"] == step["hold_id"]
                total += 1
        assert total >= 80

    def test_decisions_do_not_depend_on_supplied_state_only(self):
        # The same messages decided statelessly differ: the state matters.
        r = run_files(EXAMPLE, TRACES / "agents_tool_governance_a_approved.jsonl")
        policy = build_policy(parse_file(EXAMPLE))
        stateless = [decide(policy, {"message": s["input"]["message"]})["decision"]
                     for s in r["steps"]]
        assert stateless != [s["result"]["decision"] for s in r["steps"]]


OPA = shutil.which("opa")


@pytest.mark.skipif(OPA is None, reason="the opa binary is not on PATH")
class TestAgreementWithOpa:
    """The policy inputs that the runtime builds, evaluated by the
    generated Rego policy under `opa eval`."""

    def test_generated_rego_returns_the_decisions_of_the_runtime(self, tmp_path):
        total = 0
        for k, (name, sos, result) in enumerate(scenario_results()):
            out = tmp_path / str(k)
            generate(sos, out, target="agent-rego")
            inputs = [s["input"] for s in result["steps"]]
            f = out / "inputs.json"
            f.write_text(json.dumps(inputs))
            proc = subprocess.run(
                [OPA, "eval", "--format", "json", "--data", str(out / "agent_policy.rego"),
                 "--input", str(f),
                 "[r | x := input[_]; r := data.cadl.agent_policy.result with input as x]"],
                capture_output=True, text=True, timeout=120)
            assert proc.returncode == 0, proc.stderr + proc.stdout
            values = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
            assert len(values) == len(inputs), name
            for step, value in zip(result["steps"], values):
                assert value == step["result"], (name, step["message"])
                total += 1
        assert total >= 80


# === Audit log (G.8) ========================================================================

DECISION_KEYS = ["time", "message", "instances", "decision", "reason", "contracts", "norms",
                 "provenance", "escalate_to", "timeout", "fallback", "notify", "hold_id",
                 "outcome"]
EVENT_KEYS = ["time", "event", "message", "instances", "norm", "chain", "holders",
              "hold_id", "reason"]


class TestAuditLog:
    @pytest.mark.parametrize("cadl, path", [(EXAMPLE, p) for p in EXAMPLE_TRACES]
                             + [(CHANGE, p) for p in CHANGE_TRACES],
                             ids=lambda v: v.name)
    def test_format_and_pairs(self, cadl, path):
        r = run_files(cadl, path)
        assert r["audit"]
        for rec in r["audit"]:
            assert list(rec) == (DECISION_KEYS if "decision" in rec else EVENT_KEYS)
            assert list(rec["message"]) == ["from", "to", "name"]
        # "An escalate decision gives two records with the same hold_id."
        for h in r["holds"]:
            pair = [x for x in r["audit"] if x.get("hold_id") == h["hold_id"]
                    and "decision" in x]
            assert [x["decision"] for x in pair][0] == "escalate"
            assert len(pair) == (1 if h["state"] == "held" else 2)
            if len(pair) == 2:
                assert pair[1]["outcome"] == h["state"]
                assert pair[1]["message"] == pair[0]["message"]
        times = [x["time"] for x in r["audit"]]
        assert times == sorted(times)

    def test_the_log_is_reproducible(self, tmp_path):
        path = TRACES / "agents_tool_governance_a_approved.jsonl"
        first = audit_lines(run_files(EXAMPLE, path))
        assert first == audit_lines(run_files(EXAMPLE, path))
        assert all(json.loads(line) for line in first)

    def test_time_without_epoch_is_the_number_of_the_trace(self):
        r = replay(parse_file(EXAMPLE), trace((1.5, m("PLANNER", "FILES", "delete_file")),
                                              (2, None, {"end": False})))
        assert r["audit"][0]["time"] == 1.5 and r["steps"][0]["input"]["now"] == 1.5
        assert r["holds"][0]["deadline"] == 601.5


# === Trace format and CLI =====================================================================

class TestTrace:
    @pytest.mark.parametrize("lines, text", [
        (['{"t": 0, "epoch": "tomorrow"}'], "ISO 8601"),
        (['{"t": 0}', '{"t": 1, "epoch": "2026-01-01T00:00:00Z"}'], "first line"),
        (['{"t": 0, "instances": {}}'], "must be a list"),
        (['{"t": 0, "instances": [{"contract": "C"}]}'], "'instance'"),
        (['{"t": 0, "instances": [{"contract": "C", "instance": "a", "end": false}]}'],
         "must be true"),
        (['{"t": 0, "bindings": {"i": "x"}}'], "integers"),
        (['{"t": 0, "bogus": 1}'], "unknown key"),
        (['{"t": 1}', '{"t": 0}'], "before the previous"),
        (['{"t": 0, "instances": [{"contract": "NOPE", "instance": "a"}]}'],
         "not an enforced contract"),
        (['{"t": 0, "instances": [{"contract": "C", "instance": "a"},'
          ' {"contract": "C", "instance": "default"}]}'], "reserved"),
        (['{"t": 0, "instances": [{"contract": "C", "instance": "a", "end": true}]}'],
         "never created"),
        (['{"t": 0, "instances": [{"contract": "C", "instance": "a"}]}',
          '{"t": 1, "instances": [{"contract": "C", "instance": "a", "end": true}]}',
          '{"t": 2, "instances": [{"contract": "C", "instance": "a"}]}',
          '{"t": 3}'], "has ended"),
        (['{"t": 0, "messages": [{"from": "R[*]", "to": "T", "name": "read"}]}'],
         "concrete actor"),
    ])
    def test_malformed_traces(self, lines, text):
        with pytest.raises(TraceError, match=text):
            replay(HOLDS, lines)

    def test_lines_after_the_end_are_ignored(self):
        r = replay(HOLDS, ['{"t": 0, "end": true}',
                           '{"t": 1, "messages": [{"from": "AG", "to": "T", "name": "write"}]}'])
        assert r["steps"] == [] and "1 line(s) after the end" in r["warnings"][0]

    def test_file_without_the_extension(self):
        with pytest.raises(NormsEvalError, match="agent governance extension"):
            run(parse_file(ROOT / "examples" / "norms_robot_delivery.cadl"),
                parse_run_trace(['{"t": 0}']))


class TestCli:
    def test_text_report(self, capsys):
        status = main(["agents-run", str(EXAMPLE), "--trace",
                       str(TRACES / "agents_tool_governance_a_approved.jsonl")])
        out = capsys.readouterr().out
        assert status == 0
        assert "PLANNER -> FILES : delete_file" in out
        assert "escalate (prohibition)" in out and "closes hold h-1: approved" in out
        assert "OPERATOR > PLANNER > SUMMARIZER" in out
        assert "Result: 8 message(s): 6 allow, 1 deny, 1 escalate; holds: 1 approved; " \
               "13 audit record(s)" in out

    @pytest.mark.parametrize("cadl, path", [(EXAMPLE, p) for p in EXAMPLE_TRACES]
                             + [(CHANGE, p) for p in CHANGE_TRACES],
                             ids=lambda v: v.name)
    def test_json_and_audit_file(self, capsys, tmp_path, cadl, path):
        log = tmp_path / "audit.jsonl"
        status = main(["agents-run", str(cadl), "--trace", str(path), "--format", "json",
                       "--audit", str(log)])
        result = json.loads(capsys.readouterr().out)
        assert status == 0
        assert [json.loads(x) for x in log.read_text().splitlines()] == result["audit"]
        assert format_text(result)
        assert result["summary"]["messages"] == len(result["steps"])

    def test_example_traces_exist(self):
        assert len(EXAMPLE_TRACES) == 3 and len(CHANGE_TRACES) == 2

    def test_usage_errors(self, capsys, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text("{not json\n")
        ok = TRACES / "agents_tool_governance_a_approved.jsonl"
        undeclared = ROOT / "examples" / "norms_robot_delivery.cadl"
        for args in ((EXAMPLE, "--trace", bad), (EXAMPLE, "--trace", tmp_path / "none.jsonl"),
                     (tmp_path / "none.cadl", "--trace", ok), (undeclared, "--trace", ok)):
            status = main(["agents-run", *map(str, args)])
            captured = capsys.readouterr()
            assert status == 2, args
            assert captured.err and not captured.out


# === Lifecycle states, predicates and values =====================================================

LIFECYCLE = source(
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
    """,
    norms(
        perm("p_write", "AG", "AG -> T : write"),
        proh("no_wipe", "AG", "AG -> T : wipe", "on_denied: { escalate_to: OP }"),
        """
        - id: hot
          kind: prohibition
          bearer: AG
          trigger: "T.load > 5"
          until: "T.load <= 5"
          content: "AG -> T : write"
        """,
    ),
)


class TestLifecycleAndValues:
    def test_terminal_state_ends_the_instance(self):
        # F.5: an instance ends when it reaches a terminal state; G.6: its
        # holds are cancelled; G.5, step 3.1: afterwards there is no instance.
        r = replay(LIFECYCLE, trace(
            (0, m("AG", "T", "wipe")),
            (1, m("OP", "AG", "go"), {"state": "Busy"}),
            (2, m("OP", "AG", "done"), {"state": "Closed"}),
            (3, m("AG", "T", "write")),
            (4, m("OP", "AG", "hello"))))
        assert decisions(r) == [
            (0, "AG>T:wipe", "escalate", "prohibition", ["no_wipe"], "h-1", None),
            (1, "OP>AG:go", "allow", "default", []),
            (2, "OP>AG:done", "allow", "default", []),
            (2, "AG>T:wipe", "deny", "cancelled", ["no_wipe"], "h-1", "cancelled"),
            (3, "AG>T:write", "deny", "no_instance", []),
            (4, "OP>AG:hello", "allow", "default", []),
        ]
        inst = r["instances"][0]
        assert (inst["state"], inst["running"], inst["end"]) == ("Closed", False, 2)
        assert r["audit"][4]["instances"] == [] and r["audit"][4]["contracts"] == ["C"]

    def test_predicate_trigger_follows_the_values_of_the_trace(self):
        # The values of a line are observed before its messages are decided.
        r = replay(LIFECYCLE, trace(
            (0, m("AG", "T", "write"), {"values": {"T.load": 1}}),
            (1, m("AG", "T", "write"), {"values": {"T.load": 9}}),
            (2, m("AG", "T", "write")),
            (3, m("AG", "T", "write"), {"values": {"T.load": 2}})))
        assert [x[2:4] for x in decisions(r)] == [
            ("allow", "permission"), ("deny", "prohibition"), ("deny", "prohibition"),
            ("allow", "permission")]
        assert [s["input"]["instances"][0]["in_force"] for s in r["steps"]] == [
            {"hot": False}, {"hot": True}, {"hot": True}, {"hot": False}]


# === Random traces: invariants, and agreement with OPA on many inputs ==========================

def random_trace(sos, seed: int, length: int = 300) -> list[str]:
    """A reproducible trace over the vocabulary of a file. Most messages
    are the message events the file names (contents, triggers, `until:`,
    `counts_as:`), grants and revocations between its actors, and answers
    to recent holds; the rest are arbitrary. Some gaps let holds expire."""
    import random
    from cadl.agents import parse_message_event
    rng = random.Random(seed)
    policy = build_policy(sos)
    actors, agents = ["GHOST"], []
    for name, decl in policy["actors"].items():
        if decl["indexed"]:
            hi = decl["hi"] if decl["bounded"] else decl["lo"] + 2
            members = [f"{name}[{k}]" for k in range(decl["lo"], hi + 1)]
        else:
            members = [name]
        actors += members
        if decl["kind"] != "tool_server":
            agents += members
    names = {"hello"}
    for decl in policy["actors"].values():
        names.update(decl["tools"])
    events, answers, deleg = [], [], []
    for c in sos.contracts:
        texts = []
        for r in c.counts_as:
            event = parse_message_event(r.when)
            if event is not None and r.creates in ("approval", "rejection"):
                answers.append(event)
            else:
                texts.append(r.when)
        for n in c.norms:
            texts += [n.content, n.trigger, n.until]
        for d in c.delegations:
            deleg += [x for x in (d.grant, d.revoke) if x]
            texts.append(d.until)
        events += [e for e in map(parse_message_event, texts) if e is not None and e.name]
    names.update(e.name for e in events)
    names = sorted(names)

    def concrete(ref: dict) -> str:
        if ref["kind"] == "plain":
            return ref["name"]
        if ref["kind"] == "index":
            return f"{ref['name']}[{ref['index']}]"
        return f"{ref['name']}[{rng.randint(1, 3)}]"

    rows, t = [], 0.0
    for k in range(length):
        t += rng.choice([0, 1, 1, 1, 2, 2, 5, 30, 400])
        kind = rng.random()
        if kind < 0.45 and events:
            e = rng.choice(events)
            rows.append((t, m(concrete(e.sender), concrete(e.receiver), e.name)))
        elif kind < 0.6 and deleg:
            rows.append((t, m(rng.choice(agents), rng.choice(agents), rng.choice(deleg))))
        elif kind < 0.8 and answers:
            e = rng.choice(answers)
            # Hold ids near the number of holds a trace of this length has.
            est = k // 8
            rows.append((t, m(concrete(e.sender), concrete(e.receiver), e.name,
                              f"h-{rng.randint(max(1, est - 3), est + 2)}")))
        else:
            rows.append((t, m(rng.choice(actors), rng.choice(actors), rng.choice(names))))
    return trace(*rows, bindings={"i": 2})


def random_runs() -> list[tuple[str, object, dict]]:
    out = []
    sources = [("example", parse_file(EXAMPLE)), ("change", parse_file(CHANGE)),
               ("rich", parse(RICH)), ("holds", parse(HOLDS)), ("deleg", parse(DELEG)),
               ("escalation", parse(ESCALATION)), ("lifecycle", parse(LIFECYCLE))]
    for k, (name, sos) in enumerate(sources):
        for seed in (1, 2):
            lines = random_trace(sos, 100 * k + seed)
            out.append((f"{name}-{seed}", sos, run(sos, parse_run_trace(lines))))
    return out


class TestRandomTraces:
    def test_invariants(self):
        seen = set()
        for name, sos, r in random_runs():
            policy = build_policy(sos)
            times = [x["time"] for x in r["audit"]]
            assert times == sorted(times), name
            closing = {}
            for rec in r["audit"]:
                assert list(rec) == (DECISION_KEYS if "decision" in rec else EVENT_KEYS)
                if rec.get("outcome") is not None:
                    assert rec["hold_id"] not in closing, name      # closed once
                    closing[rec["hold_id"]] = rec
            for h in r["holds"]:
                assert (h["hold_id"] in closing) == (h["state"] != "held"), name
                if h["state"] != "held":
                    assert closing[h["hold_id"]]["outcome"] == h["state"]
                assert h["released"] == (h["state"] == "approved" and
                                         closing[h["hold_id"]]["decision"] == "allow")
                seen.add(h["state"])
            for step in r["steps"]:
                assert decide(policy, step["input"]) == step["result"], name
                # Every derived instance of an input is valid: `decide` uses it
                # only when its own re-check accepts it.
                for i in step["input"]["instances"]:
                    holders = [(d["norm"], d["holder"]) for d in i["derived"]]
                    assert len(holders) == len(set(holders)), name
            for d in r["delegations"]:
                assert d["chain"][-1] == d["holder"] and len(set(d["chain"])) == len(d["chain"])
            # The same trace gives the same log.
        assert {"approved", "rejected", "expired", "cancelled"} <= seen

    def test_runs_are_reproducible(self):
        sos = parse(RICH)
        lines = random_trace(sos, 7, 120)
        first = run(sos, parse_run_trace(lines))
        second = run(parse(RICH), parse_run_trace(lines))
        assert audit_lines(first) == audit_lines(second)
        assert json.dumps(first) == json.dumps(second)

    @pytest.mark.skipif(OPA is None, reason="the opa binary is not on PATH")
    def test_generated_rego_agrees_on_random_traces(self, tmp_path):
        total, kinds = 0, set()
        for k, (name, sos, result) in enumerate(random_runs()):
            out = tmp_path / str(k)
            generate(sos, out, target="agent-rego")
            f = out / "inputs.json"
            f.write_text(json.dumps([s["input"] for s in result["steps"]]))
            proc = subprocess.run(
                [OPA, "eval", "--format", "json", "--data", str(out / "agent_policy.rego"),
                 "--input", str(f),
                 "[r | x := input[_]; r := data.cadl.agent_policy.result with input as x]"],
                capture_output=True, text=True, timeout=300)
            assert proc.returncode == 0, proc.stderr + proc.stdout
            values = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
            assert len(values) == len(result["steps"]), name
            for step, value in zip(result["steps"], values):
                assert value == step["result"], (name, step["message"])
                kinds.add((value["decision"], value["reason"]))
                total += 1
        assert total == 14 * 300
        assert len(kinds) >= 7
