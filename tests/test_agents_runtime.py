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


def housekeeping(rec: dict) -> bool:
    """The records of the start and the end of contract instances, and of
    the derived instances that end with their contract instance."""
    return rec.get("event") in ("instance_start", "instance_end") or (
        rec.get("event") == "delegation_end" and rec["reason"] == "instance_end")


def audit(result: dict) -> list[dict]:
    """The audit log without the housekeeping records, which the scenario
    tests do not list; TestLifecycleRecords covers them."""
    return [r for r in result["audit"] if not housekeeping(r)]


def run_unchecked(sos, run_trace, versions=None) -> dict:
    """The runtime on a file that `cadl check` rejects, or with instances
    that do not bind the indices of their parties: only its fail-closed
    resolution of unknown occurrences protects such a run."""
    return run(sos, run_trace, versions, check=False)


def decisions(result: dict) -> list[tuple]:
    return [short(r) for r in result["audit"] if "decision" in r]


def events(result: dict) -> list[tuple]:
    return [short(r) for r in audit(result) if "event" in r]


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
        assert [short(x) for x in audit(r)] == [
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
        assert json.dumps(audit(r)[7]) == json.dumps({
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
        closing = audit(r)[2]
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
        assert [short(x) for x in audit(r)] == [
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
        assert audit(r)[1]["contracts"] == [] and audit(r)[1]["instances"] == [
            {"contract": "ASSISTED_REPORTING", "instance": "default"}]
        assert audit(r)[2]["fallback"] == ["PLANNER -> TICKETS : create_ticket"]
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
        assert audit(r)[-1]["provenance"] == [["OPERATOR", "PLANNER"]]


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
        esc = audit(r)[2]
        assert (esc["escalate_to"], esc["timeout"], esc["fallback"], esc["notify"]) == \
            ("OWNER", 300000, ["LEAD -> REPO : open_issue"], ["OWNER"])
        plain = audit(r)[3]
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
            # The `until:` of the entry: one record per derived instance.
            (8, "OWNER>LEAD:close_review", "delegation_end", "until"),
            (8, "OWNER>LEAD:close_review", "delegation_end", "until"),
            # A discharged instance is not revived; a new grant creates a new one.
            (11, "OWNER>WORKER[1]:grant_read", "grant", ["WORKER[1]"]),
        ]
        assert audit(r)[7]["provenance"] == [["OWNER", "WORKER[1]"]]
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
        both = audit(r)[8]
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
            (5, None, {"end": False}), bindings={"i": 2}))
        assert [short(x) for x in audit(r)] == [
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
        assert len(audit(r)) == 1 and r["instances"][0]["running"]

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
        last = audit(r)[-1]
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
        assert audit(r)[10]["provenance"] == [["OP", "AG", "SUB", "R[2]"]]

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
            (7, "OP>AG:halt", "delegation_end", "original_discharged"),
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
            assert rec["message"] is None or list(rec["message"]) == ["from", "to", "name"]
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

    def test_messages_after_the_end_are_decided(self):
        # Nothing is skipped: after `"end": true` the instance does not come
        # back, so a tool call is denied (G.5, step 3.1) and recorded.
        r = replay(HOLDS, trace((0, None, {"end": True}), (1, m("AG", "T", "write")),
                                (2, m("OP", "AG", "hello")), (3, m("AG", "T", "bogus"))))
        assert [x[1:4] for x in decisions(r)] == [
            ("AG>T:write", "deny", "no_instance"), ("OP>AG:hello", "allow", "default"),
            ("AG>T:bogus", "deny", "undeclared_tool")]
        assert r["warnings"] == [] and r["instances"][0]["end"] == 0

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
               "15 audit record(s)" in out

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
        assert audit(r)[4]["instances"] == [] and audit(r)[4]["contracts"] == ["C"]
        # A closing record lists the contract instances of the grounds of
        # its hold, also when they have ended.
        assert audit(r)[3]["instances"] == [{"contract": "C", "instance": "default"}]

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

def random_rows(sos, seed: int, length: int = 300) -> list[tuple]:
    """A reproducible trace over the vocabulary of a file. Most messages
    are the message events the file names (contents, triggers, `until:`,
    `counts_as:`), grants and revocations between its actors, and answers
    to recent holds; the rest are arbitrary. Some gaps let holds expire."""
    import random
    from cadl.agents import parse_message_event, parse_ref
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
    events, answers, deleg, chains, held = [], [], [], [], []
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
            bearer = next((n.bearer for n in c.norms if n.id == d.norm), None)
            if bearer:
                chains.append((parse_ref(bearer), [parse_ref(x) for x in d.to], d))
        held += [e for e in (parse_message_event(n.content) for n in c.norms
                             if n.kind == "prohibition" and n.on_denied
                             and n.on_denied.escalate_to) if e is not None]
        events += [e for e in map(parse_message_event, texts) if e is not None and e.name]
    names.update(e.name for e in events)
    names = sorted(names)

    def concrete(ref: dict) -> str:
        # Now and then an actor that the reference must not match: a member
        # outside the range or the declared set, or an undeclared actor.
        odd = rng.random()
        if odd < 0.04:
            return "GHOST"
        if ref["kind"] == "plain":
            return ref["name"]
        if odd < 0.12:
            return f"{ref['name']}[{rng.randint(4, 9)}]"
        if ref["kind"] == "index" and odd > 0.25:
            return f"{ref['name']}[{ref['index']}]"
        return f"{ref['name']}[{rng.randint(1, 3)}]"

    rows, t = [], 0.0
    while len(rows) < length:
        k = len(rows)
        t += rng.choice([0, 1, 1, 1, 2, 2, 5, 30, 400])
        kind = rng.random()
        if kind < 0.07 and chains and held and answers:
            # A delegation, a message that is held, something that changes
            # the state (a revocation or any event of the file), an answer.
            bearer, to, d = rng.choice(chains)
            x, y = concrete(bearer), concrete(rng.choice(to))
            e, a = rng.choice(held), rng.choice(answers)
            rows.append((t, m(x, y, d.grant)))
            rows.append((t + 1, m(concrete(e.sender), concrete(e.receiver), e.name)))
            if d.revoke and rng.random() < 0.6:
                rows.append((t + 2, m(x, y, d.revoke)))
            elif events:
                other = rng.choice(events)
                rows.append((t + 2, m(concrete(other.sender), concrete(other.receiver),
                                      other.name)))
            rows.append((t + 3, m(concrete(a.sender), concrete(a.receiver), a.name, "h-1")))
            t += 3
        elif kind < 0.45 and events:
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
    return rows[:length]


def aim(sos, lines: list[str], seed: int, versions=None) -> list[str]:
    """Point most approvals and rejections of a generated trace at a hold
    that exists when they are sent: replay the trace once, count the holds
    created before each message, and rewrite the hold id."""
    import random
    rng = random.Random(seed + 13)
    first = run(sos, parse_run_trace(lines), dict(versions or {}), check=False)
    created, out, k = 0, [], 0
    for line in map(json.loads, lines):
        for msg_ in line.get("messages", []):
            if msg_["name"] in ("approve", "reject") and created and rng.random() < 0.8:
                msg_["args"] = [f"h-{rng.randint(max(1, created - 1), created)}"]
            created += first["steps"][k]["result"]["decision"] == "escalate"
            k += 1
        out.append(json.dumps(line))
    return out


def random_trace(sos, seed: int, length: int = 300) -> list[str]:
    return aim(sos, trace(*random_rows(sos, seed, length), bindings={"i": 2}), seed)


def random_host_trace(sos, seed: int, length: int = 300, amend=()) -> list[str]:
    """`random_rows` with host-created instances that have different
    bindings and are created and ended during the trace, and with
    amendments to the given versions."""
    import random
    rng = random.Random(seed + 7)
    contracts = [c["id"] for c in build_policy(sos)["contracts"]]
    alive: dict = {}
    count = 0
    rows = []
    for t, message in random_rows(sos, seed, length):
        more = {}
        if not rows or rng.random() < 0.08:
            entries = []
            cid = rng.choice(contracts)
            running = [k for k in alive if k[0] == cid and alive[k]]
            if running and rng.random() < 0.45:
                key = rng.choice(running)
                alive[key] = False
                entries.append({"contract": cid, "instance": key[1], "end": True})
            else:
                count += 1
                alive[(cid, f"n{count}")] = True
                entries.append({"contract": cid, "instance": f"n{count}",
                                "bindings": {"i": rng.randint(1, 3)}})
            more["instances"] = entries
        if amend and rows and rng.random() < 0.02:
            more["amend"] = rng.choice(amend)
        rows.append((t, message, more))
    return aim(sos, trace(*rows), seed, {n: review(n) for n in amend})


def random_runs() -> list[tuple[str, object, dict]]:
    out = []
    sources = [("example", parse_file(EXAMPLE)), ("change", parse_file(CHANGE)),
               ("rich", parse(RICH)), ("holds", parse(HOLDS)), ("deleg", parse(DELEG)),
               ("escalation", parse(ESCALATION)), ("lifecycle", parse(LIFECYCLE))]
    for k, (name, sos) in enumerate(sources):
        for seed in (1, 2):
            lines = random_trace(sos, 100 * k + seed)
            out.append((f"{name}-{seed}", sos, run(sos, parse_run_trace(lines))))
    # What hid the defects of the first version: instances with different
    # bindings, actors outside the declared sets, a ground in one of two
    # contracts, and revocation or the end of an instance before an approval.
    more = [("x1", review("x1.cadl"), True), ("x2", review("x2.cadl"), False),
            ("x2-host", review("x2.cadl"), True), ("x3", review("x3.cadl"), False),
            ("rich-host", parse(RICH), True), ("c5", parse(C5_SOURCE), False)]
    for k, (name, sos, host) in enumerate(more):
        for seed in (1, 2):
            make = random_host_trace if host else random_trace
            lines = make(sos, 1000 + 100 * k + seed)
            # C5_SOURCE is rejected by G-17; it is run as an unchecked file.
            out.append((f"{name}-{seed}", sos,
                        run(sos, parse_run_trace(lines), check=name != "c5")))
    return out


def release_is_sound(policy: dict, rel: dict) -> bool:
    """G.6, Release, checked from the recorded input alone: no prohibition
    applies outside the grounds, every contract instance of a ground runs,
    and every governing contract without a ground allows on its own."""
    from cadl.agents import applying_prohibitions
    inp = rel["input"]
    grounds = {(g["contract"], g["norm"], g["instance"]) for g in rel["grounds"]}
    running = {(i["contract"], i["instance"]) for i in inp["instances"]}
    if any((c, i) not in running for c, _, i in grounds):
        return False
    for p in applying_prohibitions(policy, inp):
        if any((p["contract"], p["norm"]["id"], i) not in grounds for i in p["instances"]):
            return False
    with_ground = {c for c, _, _ in grounds}
    for c in policy["contracts"]:
        if c["id"] not in with_ground:
            alone = decide(dict(policy, contracts=[c]), inp)
            if alone["decision"] != "allow":
                return False
    return decide(policy, inp)["reason"] != "undeclared_tool"


def oracle_trace(rng, spec: dict, n: int) -> list[dict]:
    """The trace generator of the reviewer's stress run (stress2.py)."""
    t, lines, nh, live, made = 0, [], 0, [], 0
    host = spec.get("host", {})
    for k in range(n):
        t += rng.choice([0, 0, 1, 1, 2, 5, 30, 100, 250, 301, 599, 600, 601])
        if rng.random() < .1:
            t += rng.random()
        line, entries = {"t": t}, []
        if host and (k == 0 or rng.random() < .12) and made < 6:
            cid = rng.choice(sorted(host))
            made += 1
            entries.append({"contract": cid, "instance": f"i{made}",
                            "bindings": rng.choice(host[cid])})
            live.append((cid, f"i{made}", k))
        older = [x for x in live if x[2] < k]
        if older and rng.random() < .07:
            x = rng.choice(older)
            live.remove(x)
            entries.append({"contract": x[0], "instance": x[1], "end": True})
        if entries:
            line["instances"] = entries
        msgs = []
        for _ in range(rng.choice([1, 1, 1, 2, 3])):
            if rng.random() < spec.get("p_script", .65):
                sender, receiver, name = rng.choice(spec["likely"])
            else:
                sender, receiver = rng.choice(spec["actors"]), rng.choice(spec["actors"])
                name = rng.choice(spec["names"])
            msg_ = {"from": sender, "to": receiver, "name": name}
            if name in ("approve", "reject", "ok"):
                x = rng.random()
                if x < .78 and nh:
                    msg_["args"] = [f"h-{rng.randint(max(1, nh - 3), nh + 1)}"]
                elif x > .9:
                    msg_["args"] = [rng.choice([1, None, "h-0", "H-1", ["h-1"]])]
            msgs.append(msg_)
            nh += 1 if rng.random() < .12 else 0
        line["messages"] = msgs
        lines.append(line)
    lines.append({"t": t + 1, "end": False})
    return lines


class TestRandomTraces:
    def test_invariants(self):
        seen, releases = set(), {True: 0, False: 0}
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
            # A message is released only when the release is sound, and a
            # sound release is never denied.
            for rel in r["releases"]:
                assert release_is_sound(policy, rel) == rel["released"], (name, rel)
                releases[rel["released"]] += 1
            # Every derived instance that ended has a record that says why.
            ended = sum(1 for d in r["delegations"] if d["state"] == "discharged")
            explained = sum(len(x["holders"]) for x in r["audit"]
                            if x.get("event") in ("revoke", "delegation_end"))
            assert ended == explained, name
            starts = [x for x in r["audit"] if x.get("event") == "instance_start"]
            assert len(starts) == len(r["instances"]), name
        assert {"approved", "rejected", "expired", "cancelled"} <= seen
        assert releases[True] >= 50 and releases[False] >= 10, releases

    def test_amendments_between_hold_and_approval(self):
        # Random amendments of x2 to its variants: after each, the policy
        # the invariants use is the one in force.
        names = ["x2_nodeleg.cadl", "x2_to.cadl", "x2_newproh.cadl", "x2_actor.cadl",
                 "x2.cadl"]
        versions = {n: review(n) for n in names}
        denied = amended = 0
        for seed in range(6):
            sos = review("x2.cadl")
            lines = random_host_trace(sos, 2000 + seed, 250, amend=names)
            r = run(sos, parse_run_trace(lines), dict(versions))
            times = [x["time"] for x in r["audit"]]
            assert times == sorted(times)
            amended += len(r["amendments"])
            assert [x["reason"] for x in r["audit"] if x.get("event") == "amendment"] == \
                [a["file"] for a in r["amendments"]]
            # Replay the releases in order, switching the policy at each
            # amendment that precedes them.
            policy = build_policy(sos)
            changes = [(a["time"], a["file"]) for a in r["amendments"]]
            for rel in r["releases"]:
                while changes and changes[0][0] <= rel["time"]:
                    policy = build_policy(versions[changes.pop(0)[1]])
                assert release_is_sound(policy, rel) == rel["released"], (seed, rel)
                denied += not rel["released"]
        assert amended >= 10 and denied >= 3

    def test_runtime_agrees_with_an_independent_model(self):
        # tests/agents_oracle.py reads the YAML itself and shares no code
        # with cadl. The traces and the vocabularies are those of the
        # reviewer's stress run: host-created instances with different or
        # no bindings, actors outside the declared sets, undeclared actors,
        # malformed hold ids. y2 and y3 do not pass `cadl check` (G-17,
        # G-18): on them the runtime is compared as a host that skips the
        # check would run it.
        import random
        from .agents_oracle import Model
        specs = json.loads((REVIEW / "stress_specs.json").read_text())
        # With full bindings the runtime runs as `cadl agents-run` does.
        # Instances without bindings (`{}`) and the rejected files y2 and y3
        # need the unsafe mode: there the comparison is about the
        # fail-closed resolution of unknown occurrences.
        variants = []
        for name, spec in specs.items():
            partial = any(b == {} for bs in spec.get("host", {}).values() for b in bs)
            variants.append((name, spec, not partial and name not in ("Y2", "Y3")))
            if partial and name not in ("Y2", "Y3"):
                bound = dict(spec, host={c: [b for b in bs if b] or bs
                                         for c, bs in spec["host"].items()})
                if all(b for bs in bound["host"].values() for b in bs):
                    variants.append((name + "-bound", bound, True))
        assert [v[0] for v in variants if v[2]] == ["TG", "CC", "X2", "X3", "X1-bound"]
        total, kinds = 0, set()
        for name, spec, checked in variants:
            path = {"TG": EXAMPLE, "CC": CHANGE}.get(name, REVIEW / spec["path"])
            sos = parse_file(path)
            for seed in range(12):
                lines = oracle_trace(random.Random(seed), spec, 45)
                r = run(sos, parse_run_trace([json.dumps(x) for x in lines]), check=checked)
                model = Model(path.read_text(), host=tuple(spec.get("host", {})))
                for line in lines:
                    t = line["t"]
                    model.tick(t)
                    for e in line.get("instances", []):
                        if not e.get("end"):
                            model.start(e["contract"], e["instance"], e.get("bindings", {}))
                    for msg_ in line.get("messages", []):
                        model.message(msg_["from"], msg_["to"], msg_["name"],
                                      msg_.get("args", []), t)
                    for e in line.get("instances", []):
                        if e.get("end"):
                            model.end(e["contract"], e["instance"], t)
                got = []
                for a in r["audit"]:
                    if "decision" not in a:
                        continue
                    h = a["message"]
                    rec = (a["time"], (h["from"], h["to"], h["name"]), a["decision"],
                           a["reason"], a["hold_id"], a["outcome"])
                    if a["provenance"]:
                        rec += (tuple(map(tuple, a["provenance"])),)
                    got.append(rec)
                    kinds.add((a["decision"], a["reason"], a["outcome"]))
                assert got == [tuple(x) for x in model.out], (name, seed)
                total += len(got)
        assert total >= 7000, total
        assert {("deny", "default", "approved"),
                ("allow", "approved", "approved"), ("deny", "rejected", "rejected"),
                ("deny", "expired", "expired"), ("deny", "cancelled", "cancelled"),
                ("deny", "no_instance", None)} <= kinds, kinds

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
        assert total == 26 * 300
        assert len(kinds) >= 8


# === Findings of the review of the first version of the runtime ================================
# Each test fails on the runtime as first committed (7a4a5b3). The files
# under tests/data/agents_review are the reviewer's reproductions.

REVIEW = Path(__file__).parent / "data" / "agents_review"


def review(name: str):
    sos = parse_file(REVIEW / name)
    assert type_check(sos).ok, name
    return sos


def c1(name: str, **more) -> dict:
    return dict(contract="C1", instance=name, **more)


class TestReleaseIsDecidedAgain:
    """G.6, Release: fail-open defects A1 and C1 of the review."""

    def test_a1_prohibition_in_an_instance_created_after_the_hold(self):
        # Instance a (i = 1) holds the message for OP[1]. Instance b (i = 2)
        # is created later; there the same prohibition applies and names
        # OP[2]. The approval of OP[1] covers instance a only.
        r = replay(review("x1.cadl"), trace(
            (0, None, {"instances": [c1("a", bindings={"i": 1})]}),
            (1, m("AG", "TOOL", "act")),
            (2, None, {"instances": [c1("b", bindings={"i": 2})]}),
            (4, m("OP[1]", "AG", "approve", "h-1")),
            (5, None, {"end": False})))
        assert decisions(r) == [
            (1, "AG>TOOL:act", "escalate", "prohibition", ["no_act"], "h-1", None),
            (4, "OP[1]>AG:approve", "allow", "default", []),
            (4, "AG>TOOL:act", "deny", "prohibition", ["no_act"], "h-1", "approved"),
        ]
        assert (r["holds"][0]["state"], r["holds"][0]["released"]) == ("approved", False)
        # B3: the closing record lists the instance of the ground, not b.
        assert r["audit"][-1]["instances"] == [{"contract": "C1", "instance": "a"}]
        assert all(n["state"] == "active" for i in r["instances"] for n in i["norms"])

    def test_a1_release_when_no_other_instance_prohibits(self):
        r = replay(review("x1.cadl"), trace(
            (0, None, {"instances": [c1("a", bindings={"i": 1})]}),
            (1, m("AG", "TOOL", "act")),
            (2, m("OP[2]", "AG", "approve", "h-1")),
            (3, m("OP[1]", "AG", "approve", "h-1")),
            (4, None, {"end": False})))
        # The rule `OP[i] -> AG : approve` with i = 1 does not match OP[2]:
        # its message creates no approval at all.
        assert events(r) == []
        assert decisions(r)[-1] == (3, "AG>TOOL:act", "allow", "approved", ["no_act"],
                                    "h-1", "approved")

    def test_a1_approval_counts_in_a_contract_instance_of_a_ground(self):
        # The approver's message is observed in both instances, but only
        # instance a has a ground; the hold is approved once.
        r = replay(review("x1.cadl"), trace(
            (0, None, {"instances": [c1("a", bindings={"i": 1})]}),
            (1, m("AG", "TOOL", "act"), {"instances": [c1("a", end=True)]}),
            (2, None, {"instances": [c1("b", bindings={"i": 1})]}),
            (3, m("OP[1]", "AG", "approve", "h-1")),
            (4, None, {"end": False})))
        assert decisions(r)[1] == (1, "AG>TOOL:act", "deny", "cancelled", ["no_act"],
                                   "h-1", "cancelled")
        assert events(r) == [(3, "OP[1]>AG:approve", "unauthorised_approval", "not_held")]

    X2_START = ((0, m("OWNER", "AG", "grant_act")), (1, m("AG", "TOOL", "act")))
    APPROVE = (4, m("OP", "AG", "approve", "h-1"))

    def closing(self, r: dict) -> tuple:
        rec = next(x for x in r["audit"] if x.get("outcome"))
        return rec["decision"], rec["reason"], rec["contracts"], rec["outcome"]

    def test_c1_release_while_the_other_contract_still_permits(self):
        r = replay(review("x2.cadl"), trace(*self.X2_START, self.APPROVE,
                                            (5, None, {"end": False})))
        assert self.closing(r) == ("allow", "approved", ["CA"], "approved")
        assert r["holds"][0]["released"]

    def test_c1_permission_of_the_other_contract_revoked_before_the_approval(self):
        # G.7: "an actor cannot obtain ... an authority that its originator
        # ... no longer has". CB has no ground; it decides in full and denies.
        r = replay(review("x2.cadl"), trace(
            *self.X2_START, (2, m("OWNER", "AG", "revoke_act")), self.APPROVE,
            (5, None, {"end": False})))
        assert self.closing(r) == ("deny", "default", ["CB"], "approved")
        assert not r["holds"][0]["released"]
        assert r["audit"][-1]["escalate_to"] is None

    def test_c1_instance_of_the_other_contract_ended_before_the_approval(self):
        cb = dict(contract="CB", instance="b1")
        r = replay(review("x2.cadl"), trace(
            (0, m("OWNER", "AG", "grant_act"), {"instances": [cb]}),
            (1, m("AG", "TOOL", "act")),
            (2, None, {"instances": [dict(cb, end=True)]}),
            self.APPROVE, (5, None, {"end": False})))
        assert self.closing(r) == ("deny", "no_instance", ["CB"], "approved")

    @pytest.mark.parametrize("version, expected", [
        ("x2_nodeleg.cadl", ("deny", "default", ["CB"], "approved")),
        ("x2_to.cadl", ("deny", "default", ["CB"], "approved")),
        # Only an actor's role text changed: no contract is amended.
        ("x2_actor.cadl", ("allow", "approved", ["CA"], "approved")),
        # The contract of the ground is amended: the hold is cancelled (G.7).
        ("x2_newproh.cadl", ("deny", "cancelled", ["CA"], "cancelled")),
        ("x2_noact.cadl", ("deny", "cancelled", ["CA"], "cancelled")),
    ])
    def test_c1_amendment_between_hold_and_approval(self, version, expected):
        r = replay(review("x2.cadl"), trace(
            *self.X2_START, (2, None, {"amend": version}), self.APPROVE,
            (5, None, {"end": False})), versions={version: review(version)})
        assert self.closing(r) == expected

    def test_c1_other_contract_that_would_escalate_denies_the_release(self):
        # Both contracts prohibit; CB's prohibition starts after the hold
        # and names another approver. No second hold is created.
        head = "- id: no_wipe2\n          kind: prohibition\n          bearer: AG\n"
        assert RICH.count(head) == 1 and RICH.count("enforcement: { default: deny }") == 1
        src = RICH.replace("enforcement: { default: deny }", "enforcement:")
        src = src.replace(head, '- { id: p_wipe2, kind: permission, bearer: AG, '
                                'content: "AG -> T : wipe" }\n        ' + head
                          + '          trigger: "SUP -> AG : arm"\n')
        r = replay(src, trace(
            (0, m("AG", "T", "wipe")), (1, m("SUP", "AG", "arm")),
            (2, m("OP", "AG", "approve", "h-1")), (3, None, {"end": False}),
            bindings={"i": 2}))
        last = r["audit"][-1]
        assert (last["decision"], last["reason"], last["contracts"], last["norms"]) == \
            ("deny", "prohibition", ["C2"], ["C2.no_wipe2"])
        assert (last["escalate_to"], last["notify"], last["outcome"]) == (None, ["SUP"],
                                                                         "approved")
        assert len(r["holds"]) == 1

    def test_release_inputs_are_recorded(self):
        r = replay(review("x2.cadl"), trace(*self.X2_START, self.APPROVE,
                                            (5, None, {"end": False})))
        rel = r["releases"][0]
        assert (rel["hold_id"], rel["released"], rel["time"]) == ("h-1", True, 4)
        assert rel["grounds"] == [{"contract": "CA", "norm": "no_act", "instance": "default"}]


class TestActorReferencesInEvents:
    """A2 and C5: actor references in triggers, `until:` and `counts_as`
    match by the table of G.4, and a contract observes only the messages
    it governs."""

    E = (9, None, {"end": False})

    def run(self, *rows) -> dict:
        return replay(review("x3.cadl"), trace(*rows, self.E))

    @pytest.mark.parametrize("sender, reason", [("SUP[3]", "default"), ("SUP[9]", "ungoverned")])
    def test_trigger_with_a_range_matches_its_members_only(self, sender, reason):
        r = self.run((0, m(sender, "AG", "enable")), (1, m("AG", "TOOL", "read")))
        assert [x[2:4] for x in decisions(r)] == [("allow", reason), ("deny", "default")]

    def test_trigger_by_a_member_of_the_range(self):
        r = self.run((0, m("SUP[2]", "AG", "enable")), (1, m("AG", "TOOL", "read")))
        assert decisions(r)[1][2:4] == ("allow", "permission")

    @pytest.mark.parametrize("sender", ["SUP[3]", "SUP[9]"])
    def test_until_with_a_range_does_not_lift_a_prohibition_for_others(self, sender):
        r = self.run((0, m("SUP[1]", "AG", "freeze")), (1, m("AG", "TOOL", "act")),
                     (2, m(sender, "AG", "thaw")), (3, m("AG", "TOOL", "act")))
        assert [x[2] for x in decisions(r)] == ["allow", "escalate", "allow", "escalate"]

    def test_until_of_a_delegation_with_a_range(self):
        r = self.run((0, m("AG", "SUB", "grant_act")), (1, m("SUP[3]", "AG", "endgrant")),
                     (2, m("SUB", "TOOL", "act")), (3, m("SUP[2]", "AG", "endgrant")),
                     (4, m("SUB", "TOOL", "act")))
        assert [x[2:4] for x in decisions(r) if x[1] == "SUB>TOOL:act"] == [
            ("allow", "permission"), ("deny", "default")]

    def test_c5_a_contract_observes_only_what_it_governs(self):
        # `OP -> AG : freeze` is governed by C1 (OP and AG are parties) but
        # not by C2 (OP is no party of it); a trigger of C2 written with the
        # same message would be a G.5 leak. Here: X is a declared actor and
        # no party, so C1 does not govern `X -> AG : stop`; the `until:` of
        # may_report names OP, and the ungoverned message changes nothing.
        r = replay(RICH, trace(
            (0, m("X", "AG", "freeze")), (1, m("AG", "M", "ping")),
            (2, m("X", "AG", "approve", "h-1")), (3, None, {"end": False}),
            bindings={"i": 2}))
        assert [x[2:4] for x in decisions(r)] == [
            ("allow", "ungoverned"), ("allow", "default"), ("allow", "ungoverned")]
        assert events(r) == []                      # no approval event was created
        for inst in r["instances"]:
            assert inst["institutional_events"] == []


class TestApproverMessageIsGoverned:
    """C4 of the first review, now part of G-17: the message of a rule that
    creates `approval` or `rejection` goes from a party to a party."""

    def test_c4_receiver_of_an_approval_rule_must_be_a_party(self):
        from .test_agents_extension import check, with_rule
        res = check(
            """
            counts_as:
              - { when: "OP -> X : approve", creates: approval }
            """,
            norms(proh("no_wipe", "AG", "AG -> T : wipe", "on_denied: { escalate_to: OP }")))
        errors = with_rule(res.errors, "G-17")
        assert len(errors) == 1 and "receiver 'X'" in errors[0]
        assert "not covered by one reference in the parties" in errors[0]

    def test_c4_member_outside_an_indexed_party(self):
        from .test_agents_extension import check, with_rule
        blocks = (
            """
            counts_as:
              - { when: "OP -> R[3] : approve", creates: approval }
            """,
            norms(proh("no_wipe", "AG", "AG -> T : wipe", "on_denied: { escalate_to: OP }")))
        assert not with_rule(check(*blocks).errors, "G-17")
        res = check(*blocks, parties='[OP, SUP, AG, SUB, T, "R[1..2]"]')
        assert len(with_rule(res.errors, "G-17")) == 1


class TestTraceValues:
    """A3, D1, D2 and D4: values that are not well-formed are trace errors."""

    D = '"messages": [{"from": "PLANNER", "to": "FILES", "name": "delete_file"}]'

    @pytest.mark.parametrize("line, text", [
        ('{"t": NaN, %s}', "not JSON"),
        ('{"t": Infinity, %s}', "not JSON"),
        ('{"t": -Infinity, %s}', "not JSON"),
        ('{"t": 1e999, %s}', "not JSON"),
        ('{"t": true, %s}', "must be a number"),
        ('{"t": 1e18, %s}', "at most"),
        ('{"t": 9.1e15, %s}', "at most"),
        ('{"t": 1, "values": {"x": NaN}, %s}', "not JSON"),
        ('{"t": 1, "messages": [{"from": "PLAN NER", "to": "FILES", "name": "delete_file"}]}',
         "concrete actor"),
        ('{"t": 1, "messages": [{"from": "PLANNER", "to": " FILES", "name": "delete_file"}]}',
         "concrete actor"),
        ('{"t": 1, "messages": [{"from": "PLANNER", "to": "FILES[01]", "name": "x"}]}',
         "concrete actor"),
        ('{"t": 1, "messages": [{"from": "PLANNER", "to": "FILES", "name": "delete file"}]}',
         "message name"),
        ('{"t": 1, "messages": [{"from": "PLANNER", "to": "FILES", "name": "x", '
         '"args": [1e999]}]}', "not JSON"),
    ])
    def test_rejected_by_both_trace_readers(self, line, text):
        from cadl.norms_eval import parse_trace
        lines = ['{"t": 0}', line % self.D if "%s" in line else line]
        with pytest.raises(TraceError, match=text):
            parse_run_trace(lines)
        with pytest.raises(TraceError, match=text):
            parse_trace(lines)

    def test_epoch_that_overflows(self):
        with pytest.raises(TraceError, match="line 2: .*ISO 8601 time stamp"):
            parse_run_trace(['{"t": 0, "epoch": "9990-01-01T00:00:00Z"}', '{"t": 9e9}'])

    def test_d4_failed_run_writes_no_audit_file(self, capsys, tmp_path):
        bad = tmp_path / "nan.jsonl"
        bad.write_text('{"t": 0, %s}\n{"t": NaN, %s}\n' % (self.D, self.D))
        log = tmp_path / "audit.jsonl"
        status = main(["agents-run", str(EXAMPLE), "--trace", str(bad), "--audit", str(log)])
        captured = capsys.readouterr()
        assert status == 2 and not log.exists() and not captured.out
        assert "No audit file was written" in captured.err
        # A failure after the first decisions: still nothing is written.
        late = tmp_path / "late.jsonl"
        late.write_text('{"t": 0, %s}\n{"t": 1, "state": "Nowhere", "instances": '
                        '[{"contract": "NOPE", "instance": "a"}]}\n' % self.D)
        status = main(["agents-run", str(EXAMPLE), "--trace", str(late), "--audit", str(log)])
        assert status == 2 and not log.exists()
        assert "No audit file was written" in capsys.readouterr().err

    def test_no_nan_in_any_output(self, capsys, tmp_path):
        for path in EXAMPLE_TRACES:
            assert "NaN" not in "".join(audit_lines(run_files(EXAMPLE, path)))


class TestLifecycleRecords:
    """B2 and B3: every change of what a later decision can return has a
    record."""

    def test_instances_start_and_end(self):
        r = replay(RICH, trace(
            (0, None, {"instances": [dict(contract="C1", instance="a", bindings={"i": 1})]}),
            (1, m("OP", "AG", "grant_write"), {"instances": [dict(contract="C2", instance="x")]}),
            (2, None, {"instances": [dict(contract="C1", instance="a", end=True)]}),
            (3, None)))
        got = [(x["time"], x["event"], x["instances"][0]["instance"], x["reason"])
               for x in r["audit"] if "event" in x and x["event"] != "grant"]
        assert got == [
            (0, "instance_start", "a", None), (1, "instance_start", "x", None),
            (2, "instance_end", "a", "end"), (2, "delegation_end", "a", "instance_end"),
            (3, "instance_end", "x", "end")]
        ended = next(x for x in r["audit"] if x.get("event") == "delegation_end")
        assert (ended["norm"], ended["chain"], ended["holders"], ended["message"]) == \
            ("C1.p_op_write", ["OP", "AG"], ["AG"], None)

    def test_terminal_state_and_amendment(self):
        r = replay(LIFECYCLE, trace((0, None), (1, m("OP", "AG", "done"), {"state": "Closed"})))
        assert [(x["event"], x["reason"]) for x in r["audit"] if "event" in x] == [
            ("instance_start", None), ("instance_end", "terminal_state")]
        v2 = review("x2_nodeleg.cadl")
        r = replay(review("x2.cadl"), trace(
            (0, m("OWNER", "AG", "grant_act")), (2, None, {"amend": "v2"}),
            (3, None, {"end": False})), versions={"v2": v2})
        assert [(x["time"], x["event"], x["reason"], x["instances"]) for x in audit(r)
                if "event" in x and x["event"] != "grant"] == [
            (2, "amendment", "v2", [{"contract": "CB", "instance": "default"}]),
            (2, "delegation_end", "amendment", [{"contract": "CB", "instance": "default"}])]

    def test_b3_closing_records_list_the_instances_of_the_grounds(self):
        r = replay(review("x1.cadl"), trace(
            (0, None, {"instances": [c1("a", bindings={"i": 1})]}),
            (1, [m("AG", "TOOL", "act"), m("AG", "TOOL", "act")]),
            (2, None, {"instances": [c1("a", end=True), c1("b", bindings={"i": 3})]}),
            (700, None, {"end": False})))
        closing = [(x["reason"], x["instances"]) for x in r["audit"] if x.get("outcome")]
        assert closing == [("cancelled", [{"contract": "C1", "instance": "a"}])] * 2
        r = replay(review("x1.cadl"), trace(
            (0, None, {"instances": [c1("a", bindings={"i": 1})]}),
            (1, m("AG", "TOOL", "act")),
            (2, None, {"instances": [c1("b", bindings={"i": 1})]}),
            (700, None, {"end": False})))
        closing = [(x["reason"], x["time"], x["instances"]) for x in r["audit"]
                   if x.get("outcome")]
        assert closing == [("expired", 601, [{"contract": "C1", "instance": "a"}])]


C5_SOURCE = source(norms(
    perm("p_read", "AG", "AG -> T : read"),
    """
    - id: locked
      kind: prohibition
      bearer: AG
      trigger: "R[*] -> AG : lock"
      until: "X -> AG : unlock"
      content: "AG -> T : read"
    """), parties='[OP, SUP, AG, SUB, T, "R[1..2]"]')


class TestGovernedObservations:
    """C5: an instance of an enforced contract observes a message only when
    the contract governs it (G.5)."""

    def test_trigger_from_a_declared_actor_that_is_no_party(self):
        # R[3] is a member of the declared set R[1..3] and matches `R[*]`,
        # but the parties are R[1..2]: the contract does not govern the
        # message, so it is ungoverned and no observation for the contract.
        # `cadl check` rejects such a file (G-17); the runtime is run on it
        # without the check, as a host that skips the check would.
        errors = [str(e) for e in type_check(parse(C5_SOURCE)).errors]
        assert len(errors) == 2 and all("(G-17)" in e for e in errors)
        r = run_unchecked(parse(C5_SOURCE), parse_run_trace(trace(
            (0, m("R[3]", "AG", "lock")), (1, m("AG", "T", "read")),
            (2, m("R[2]", "AG", "lock")), (3, m("AG", "T", "read")),
            (4, m("X", "AG", "unlock")), (5, m("AG", "T", "read")),
            (6, None, {"end": False}))))
        assert [x[2:4] for x in decisions(r)] == [
            ("allow", "ungoverned"), ("allow", "permission"),
            ("allow", "default"), ("deny", "prohibition"),
            # X is declared but no party: its `unlock` is not observed either.
            ("allow", "ungoverned"), ("deny", "prohibition")]
        assert norm_states(r)["locked"][0] == "active"


# === Findings of the second review ==============================================================

def unchecked(name: str):
    """A reproduction that `cadl check` now rejects, as a host that skips
    the check would load it."""
    return parse_file(REVIEW / name)


def rule_errors(sos, rule: str) -> list[str]:
    return [str(e) for e in type_check(sos).errors if f"({rule})" in str(e)]


class TestEventsAreCoveredByTheParties:
    """N1, rule G-17: with governed observations (G.5) a message event that
    a non-party sends or receives could never occur, silently."""

    def test_y2_non_party_in_trigger_until_and_content(self):
        errors = rule_errors(unchecked("y2.cadl"), "G-17")
        assert len(errors) == 4
        for part in ("sender 'ADMIN' of the trigger: of norm 'freeze'",
                     "sender 'ADMIN' of the until: of norm 'may_x'",
                     "receiver 'EXT' of the content: of norm 'must'",
                     "receiver 'EXT' of the content: of norm 'no_leak'"):
            assert any(part in e for e in errors), part

    def test_y3_member_outside_the_party_range(self):
        # parties: OPS[1..2]; the set is declared as OPS[1..3].
        errors = rule_errors(unchecked("y3.cadl"), "G-17")
        assert len(errors) == 2
        assert any("'OPS[*]' of the trigger: of norm 'q_read'" in e for e in errors)
        assert any("'OPS[1..3]' of the until: of norm 'no_act'" in e for e in errors)

    def test_what_the_rule_prevents_at_run_time(self):
        # The last scenario of u2.py on the unchecked file: the non-party
        # ADMIN cannot freeze, and `stop` does not end may_x.
        r = run_unchecked(unchecked("y2.cadl"), parse_run_trace(trace(
            (0, None), (1, m("ADMIN", "AG", "freeze")), (2, m("AG", "TOOL", "xx")),
            (3, m("ADMIN", "AG", "stop")), (4, m("AG", "TOOL", "xx")),
            (5, None, {"end": False}))))
        assert [x[2:4] for x in decisions(r)] == [
            ("allow", "ungoverned"), ("allow", "permission"),
            ("allow", "ungoverned"), ("allow", "permission")]
        assert norm_states(r)["freeze"][0] == "inactive"

    @pytest.mark.parametrize("block, count", [
        ('counts_as:\n  - { when: "X -> AG : note", creates: noted }\n', 1),
        ('counts_as:\n  - { when: "OP -> R[3] : note", creates: noted }\n', 0),
    ])
    def test_counts_as_rules(self, block, count):
        from .test_agents_extension import check, with_rule
        assert len(with_rule(check(block).errors, "G-17")) == count

    def test_delegation_until_and_ranges(self):
        from .test_agents_extension import check, with_rule
        blocks = (norms(perm("p", "OP", "OP -> T : read")),
                  'delegations:\n  - { norm: p, to: [AG], grant: g, until: "%s -> OP : end" }\n')
        def errors(sender, **kw):
            return with_rule(check(blocks[0], blocks[1] % sender, **kw).errors, "G-17")
        assert len(errors("X")) == 1 and not errors("SUP") and not errors("R[2]")
        two = '[OP, SUP, AG, SUB, T, "R[1..2]"]'
        assert not errors("R[1..2]", parties=two) and not errors("R[2]", parties=two)
        assert len(errors("R[1..3]", parties=two)) == 1
        assert len(errors("R[*]", parties=two)) == 1 and len(errors("R[3]", parties=two)) == 1
        # A party that is one member covers a range of that member only.
        one = '[OP, SUP, AG, SUB, T, "R[2]"]'
        assert not errors("R[2..2]", parties=one) and len(errors("R[*]", parties=one)) == 1

    def test_fallback_to_a_non_party_is_not_an_event(self):
        # G-10 asks for a declared receiver only: a fallback is data for the
        # sender; a message to a non-party is ungoverned and allowed.
        from .test_agents_extension import check, with_rule
        res = check(APPROVAL_RULES, norms(proh(
            "no_wipe", "AG", "AG -> T : wipe",
            'on_denied: { escalate_to: OP, fallback: "AG -> X : ask" }')))
        assert not with_rule(res.errors, "G-17") and res.ok


APPROVAL_RULES = """
counts_as:
  - { when: "OP -> AG : approve", creates: approval }
"""


class TestUnboundIndexInEvents:
    """N2: an index that the contract instance does not bind."""

    def test_g18_index_that_no_party_binds(self):
        errors = rule_errors(unchecked("y2.cadl"), "G-18")
        assert len(errors) == 1 and "'k'" in errors[0] and "may_read" in errors[0]
        assert len(rule_errors(unchecked("y2_u.cadl"), "G-18")) == 2
        # `W[i]` is a party: the index i is bound by the contract instance.
        assert not any("'i'" in e for e in errors)
        assert not rule_errors(review("x1.cadl"), "G-18")

    def test_u1_unbound_index_does_not_activate_a_permission(self):
        r = run_unchecked(unchecked("y2.cadl"), parse_run_trace(trace(
            (0, None), (1, m("SUP[2]", "AG", "enable")), (2, m("AG", "TOOL", "read")),
            (3, None, {"end": False}))))
        assert decisions(r)[-1][2:4] == ("deny", "default")
        assert norm_states(r)["may_read"][0] == "inactive"
        assert any("'k'" in w and "fail-closed" in w for w in r["warnings"])

    def test_u4_unbound_index_does_not_lift_a_prohibition(self):
        r = run_unchecked(unchecked("y2_u.cadl"), parse_run_trace(trace(
            (0, None, {"instances": [dict(contract="C", instance="a", bindings={"i": 1})]}),
            (1, m("AG", "TOOL", "act")), (2, m("SUP[2]", "AG", "lift")),
            (3, m("AG", "TOOL", "act")), (4, None, {"end": False}))))
        assert [x[2] for x in decisions(r)] == ["escalate", "allow", "escalate"]

    def test_bound_index_works_as_before(self):
        r = run_unchecked(unchecked("y2_u.cadl"), parse_run_trace(trace(
            (0, None, {"instances": [dict(contract="C", instance="a",
                                          bindings={"i": 1, "k": 2})]}),
            (1, m("SUP[1]", "AG", "lift")), (2, m("AG", "TOOL", "act")),
            (3, m("SUP[2]", "AG", "lift")), (4, m("AG", "TOOL", "act")),
            (5, m("SUP[2]", "AG", "enable")), (6, m("AG", "TOOL", "read")),
            (7, None, {"end": False}))))
        assert [x[2:4] for x in decisions(r) if x[1].startswith("AG>TOOL")] == [
            ("escalate", "prohibition"), ("allow", "permission"), ("allow", "permission")]

    FAIL_CLOSED = source(norms(
        perm("p_read", "AG", "AG -> T : read", 'until: "R[k] -> AG : stop"'),
        proh("no_write", "AG", "AG -> T : write", 'trigger: "R[k] -> AG : lock"'),
        perm("p_write", "AG", "AG -> T : write"),
        perm("p_op", "OP", "OP -> T : read"),
    ), """
    delegations:
      - { norm: p_op, to: [SUB], grant: g, until: "R[k] -> OP : end" }
    """)

    def test_unknown_ends_a_permission_and_activates_a_prohibition(self):
        # G.5: unknown is fail-closed in both directions.
        sos = parse(self.FAIL_CLOSED)
        assert len(rule_errors(sos, "G-18")) == 3
        r = run_unchecked(sos, parse_run_trace(trace(
            (0, [m("AG", "T", "read"), m("AG", "T", "write"), m("OP", "SUB", "g"),
                 m("SUB", "T", "read")]),
            (1, [m("R[3]", "AG", "stop"), m("R[3]", "AG", "lock"), m("R[3]", "OP", "end")]),
            (2, [m("AG", "T", "read"), m("AG", "T", "write"), m("SUB", "T", "read")]),
            (3, None, {"end": False}))))
        tool = [x[2:4] for x in decisions(r) if ">T:" in x[1]]
        assert tool == [("allow", "permission")] * 3 + [
            ("deny", "default"), ("deny", "prohibition"), ("deny", "default")]
        assert (1, "R[3]>OP:end", "delegation_end", "until") in events(r)

    def test_norms_eval_keeps_f3_for_contracts_that_are_not_enforced(self):
        from cadl.norms_eval import evaluate, parse_trace
        sos = parse(source(norms(perm("p_read", "AG", "AG -> T : read",
                                      'until: "R[k] -> AG : stop"')), enforcement=None))
        r = evaluate(sos, parse_trace(trace((0, None), (1, m("R[3]", "AG", "stop")),
                                            (2, None, {"end": False}))), "C")
        states = {n["id"]: n["state"] for n in r["norms"]}
        assert states["p_read"] == "discharged"       # any member, with a warning


SYMBOLIC = """
sos:
  name: Sym
  extensions:
    - norms: 0.1
    - agents: 0.1
  actors:
    - { id: OP, role: o, agent: { kind: human } }
    - { id: "R[1..N]", role: r, agent: { kind: llm_agent } }
    - { id: T, role: t, agent: { kind: tool_server, tools: [read] } }
  contracts:
    - id: C
      parties: [OP, "R[*]", T]
      enforcement: {}
      norms:
        - { id: may_read, kind: permission, bearer: "R[1]", trigger: "R[1..N] -> OP : ready", content: "R[1] -> T : read" }
        - { id: two_read, kind: permission, bearer: "R[2]", content: "R[2] -> T : read" }
"""


class TestSymbolicUpperBound:
    """N3: `R[1..N]` has every index from the lower bound on, not R[0]."""

    def test_is_member(self):
        from cadl.agents import is_member, parse_concrete_actor
        policy = build_policy(parse(SYMBOLIC))
        member = {a: is_member(policy, "R", parse_concrete_actor(a))
                  for a in ("R[0]", "R[1]", "R[77]", "R")}
        assert member == {"R[0]": False, "R[1]": True, "R[77]": True, "R": False}

    def test_runtime_and_generated_rego(self, tmp_path):
        sos = parse(SYMBOLIC)
        assert type_check(sos).ok
        r = run(sos, parse_run_trace(trace(
            (0, [m("R[0]", "OP", "ready"), m("R[0]", "T", "read"), m("R[2]", "T", "read"),
                 m("R[0]", "OP", "hello"), m("R[5]", "OP", "hello")]),
            (1, None, {"end": False}))))
        assert [x[2:4] for x in decisions(r)] == [
            ("allow", "ungoverned"), ("deny", "default"), ("allow", "permission"),
            ("allow", "ungoverned"), ("allow", "default")]
        assert norm_states(r)["may_read"][0] == "inactive"
        if OPA is None:
            pytest.skip("the opa binary is not on PATH")
        generate(sos, tmp_path, target="agent-rego")
        f = tmp_path / "inputs.json"
        f.write_text(json.dumps([s["input"] for s in r["steps"]]))
        proc = subprocess.run(
            [OPA, "eval", "--format", "json", "--data", str(tmp_path / "agent_policy.rego"),
             "--input", str(f),
             "[r | x := input[_]; r := data.cadl.agent_policy.result with input as x]"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        values = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
        assert values == [s["result"] for s in r["steps"]]

    def test_u3_grant_to_a_non_member(self):
        r = run_unchecked(unchecked("y3.cadl"), parse_run_trace(trace(
            (0, [m("R[1]", "R[0]", "g"), m("R[0]", "TOOL", "act"), m("R[1]", "R[2]", "g"),
                 m("R[2]", "TOOL", "act")]), (1, None, {"end": False}))))
        assert [x[2:4] for x in decisions(r)] == [
            ("allow", "ungoverned"), ("deny", "default"), ("allow", "default"),
            ("allow", "permission")]


class TestTimeBase:
    """N4: a trace may use epoch milliseconds; only with `epoch` does a
    time have to be an ISO 8601 instant."""

    def test_epoch_milliseconds_without_epoch(self):
        base = 1700000000000
        r = replay(parse_file(EXAMPLE), trace(
            (base, m("PLANNER", "FILES", "delete_file")),
            (base + 5, m("OPERATOR", "PLANNER", "approve", "h-1")),
            (base + 6, None, {"end": False})))
        assert decisions(r)[-1][:4] == (base + 5, "PLANNER>FILES:delete_file", "allow",
                                        "approved")
        assert r["holds"][0]["deadline"] == base + 600

    def test_largest_time(self):
        parse_run_trace(['{"t": %d}' % 2 ** 53])
        with pytest.raises(TraceError, match="2\\*\\*53"):
            parse_run_trace(['{"t": %d}' % (2 ** 53 + 2)])

    def test_with_epoch_every_time_and_deadline_is_an_instant(self):
        late = '"epoch": "9999-12-31T23:55:00Z"'
        call = '"messages": [{"from": "PLANNER", "to": "FILES", "name": "delete_file"}]'
        with pytest.raises(TraceError, match="line 2: .*ISO 8601"):
            parse_run_trace(['{"t": 0, %s}' % late, '{"t": 1700000000000}'])
        # The time is an instant, the deadline of the hold (10 min later) is not.
        with pytest.raises(TraceError, match="line 2: .*deadline of a hold"):
            replay(parse_file(EXAMPLE), ['{"t": 0, %s}' % late, '{"t": 1, %s}' % call,
                                         '{"t": 2, "end": false}'])

    def test_norms_eval_on_a_trace_in_milliseconds(self):
        from cadl.norms_eval import evaluate_files
        cadl = ROOT / "examples" / "norms_robot_delivery.cadl"
        ms = evaluate_files(cadl, REVIEW / "ms_ne.jsonl")
        ref = evaluate_files(cadl, TRACES / "norms_robot_delivery_a_fulfilled.jsonl")
        assert [(n["id"], n["state"], n["verdict"]) for n in ms["norms"]] == \
            [(n["id"], n["state"], n["verdict"]) for n in ref["norms"]]
        assert ms["ok"] and ms["instance"]["start"] == 1700000000000.0

    def test_names_are_not_canonicalised(self, capsys):
        status = main(["norms-eval", str(ROOT / "examples" / "norms_robot_delivery.cadl"),
                       "--trace", str(REVIEW / "ws_ne.jsonl")])
        err = capsys.readouterr().err
        assert status == 2 and "ROBOT[ 2 ]" in err and "concrete actor" in err


# === Findings of the third review ================================================================
# Root cause: an instance of an enforced contract that runs with an index
# variable unbound. G-18 and the runtime now exclude it; the fail-closed
# resolution stays as a second line for the unsafe mode of the API.

E_ = (50, None, {"end": False})


class TestEveryIndexIsBoundByAParty:
    """P1 (1): G-18 for every index variable of an enforced contract."""

    def test_g_g_power_with_an_index_that_no_party_binds(self):
        errors = rule_errors(unchecked("g_g.cadl"), "G-18")
        assert len(errors) == 2
        assert any("the bearer: of norm 'pw'" in e for e in errors)
        assert any("the content: of norm 'pw'" in e for e in errors)

    def test_files_with_the_index_as_a_party_pass(self):
        for name in ("w1.cadl", "g_ob.cadl", "w2.cadl", "x1.cadl", "lb.cadl"):
            assert type_check(unchecked(name)).ok, name

    @pytest.mark.parametrize("extra, part", [
        ('counterparty: "R[j]"', "counterparty:"),
        ('on_denied: { notify: "R[j]" }', "on_denied.notify:"),
        ('on_denied: { escalate_to: "R[j]" }', "on_denied.escalate_to:"),
        ('on_denied: { escalate_to: OP, fallback: "AG -> R[j] : ask" }', "on_denied.fallback:"),
    ])
    def test_references_outside_events(self, extra, part):
        from .test_agents_extension import check, with_rule
        blocks = (APPROVAL_RULES, norms(proh("no_wipe", "AG", "AG -> T : wipe", extra)))
        errors = with_rule(check(*blocks).errors, "G-18")
        assert len(errors) == 1 and part in errors[0] and "'j'" in errors[0]
        fixed = [b.replace("R[j]", "R[i]") for b in blocks]
        bound = '[OP, SUP, AG, SUB, T, "R[i]"]'
        assert not with_rule(check(*fixed, parties=bound).errors, "G-18")

    def test_bearer_and_content_of_every_kind(self):
        from .test_agents_extension import check, with_rule
        for kind in ("permission", "prohibition", "obligation", "power"):
            entry = (f'- id: n\n  kind: {kind}\n  bearer: "R[i]"\n'
                     f'  content: "R[i] -> T : read"\n')
            errors = with_rule(check(norms(entry)).errors, "G-18")
            assert len(errors) == 2, kind                 # bearer and content
            ok = check(norms(entry), parties='[OP, SUP, AG, SUB, T, "R[i]"]')
            assert not with_rule(ok.errors, "G-18"), kind

    def test_the_former_form_of_the_rich_file_is_rejected(self):
        # Parties `R[*]` with norms of `R[i]`: nothing in the file binds i.
        old = RICH.replace('M, "R[i]"]', 'M, "R[*]"]')
        assert old != RICH and len(rule_errors(parse(old), "G-18")) == 4
        assert type_check(parse(RICH)).ok


class TestInstancesBindTheIndicesOfTheirParties:
    """P1 (2): the root fix in the runtime."""

    ACT = m("AG", "TOOL", "act")

    def test_default_instance_without_bindings_is_a_trace_error(self):
        for name, var in (("w1.cadl", "k"), ("g_ob.cadl", "k"), ("w2.cadl", "i")):
            with pytest.raises(TraceError) as err:
                replay(review(name), trace((0, None), (1, self.ACT), E_))
            text = str(err.value)
            assert "line 1" in text and "instance default of C" in text
            assert f"index '{var}'" in text, text

    def test_host_created_instance_without_bindings(self):
        with pytest.raises(TraceError, match="line 2: instance b of C has no binding"):
            replay(review("w1.cadl"), trace(
                (0, None, {"instances": [dict(contract="C", instance="a", bindings={"k": 1})]}),
                (1, self.ACT, {"instances": [dict(contract="C", instance="b")]}), E_))

    def test_cli_exit_status(self, capsys, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(trace((0, None), (1, self.ACT))) + "\n")
        log = tmp_path / "audit.jsonl"
        status = main(["agents-run", str(REVIEW / "w1.cadl"), "--trace", str(f),
                       "--audit", str(log)])
        err = capsys.readouterr().err
        assert status == 2 and "has no binding for the index 'k'" in err and not log.exists()

    def test_w1_with_the_binding_only_that_member_has_the_power(self):
        # v2.py, P4: k = 1.
        r = replay(review("w1.cadl"), trace(
            (0, None), (1, m("SUP[2]", "AG", "pause")), (2, self.ACT),
            (3, m("SUP[1]", "AG", "pause")), (4, self.ACT),
            (5, m("SUP[2]", "AG", "bless")), (6, m("AG", "TOOL", "wipe")),
            (7, m("SUP[1]", "AG", "bless")), (8, m("AG", "TOOL", "wipe")), E_,
            bindings={"k": 1}))
        tool = [x[2:4] for x in decisions(r) if x[1].startswith("AG>TOOL")]
        assert tool == [("escalate", "prohibition"), ("allow", "permission"),
                        ("deny", "default"), ("allow", "permission")]
        # `SUP[2] -> AG : pause` is not the content `SUP[1] -> AG : pause` of
        # the power: it is no act at all.
        assert events(r) == []

    def test_amendment_that_adds_an_index_to_the_parties(self):
        v1 = source(norms(perm("p", "AG", "AG -> T : read")))
        v2 = parse(source(norms(perm("p", "AG", "AG -> T : read")),
                          parties='[OP, SUP, AG, SUB, T, "R[i]"]'))
        with pytest.raises(TraceError, match="no binding for the index 'i'"):
            replay(v1, trace((0, None), (1, None, {"amend": "v2"}), E_), versions={"v2": v2})


class TestUnknownNeverLetsThrough:
    """P1 (3) and P2: the fail-closed resolution for powers and obligations,
    reachable only in the unsafe mode."""

    ACT = m("AG", "TOOL", "act")

    def w1(self, *rows) -> dict:
        return run_unchecked(unchecked("w1.cadl"), parse_run_trace(trace((0, None), *rows, E_)))

    def test_p1_unknown_performer_does_not_suspend_a_prohibition(self):
        r = self.w1((1, self.ACT), (2, m("SUP[2]", "AG", "pause")), (3, self.ACT))
        assert [x[2] for x in decisions(r)] == ["escalate", "allow", "escalate"]
        assert norm_states(r)["no_act"][0] == "active"

    def test_p1_unknown_performer_does_not_activate_a_permission(self):
        r = self.w1((1, m("AG", "TOOL", "read")), (2, m("SUP[3]", "AG", "arm")),
                    (3, m("AG", "TOOL", "read")))
        assert [x[2:4] for x in decisions(r)] == [
            ("deny", "default"), ("allow", "default"), ("deny", "default")]

    def test_p1_no_institutional_event_and_no_power_through_it(self):
        r = self.w1((1, m("SUP[2]", "AG", "halt")), (2, self.ACT),
                    (3, m("SUP[2]", "AG", "bless")), (4, m("AG", "TOOL", "wipe")))
        assert [x[2:4] for x in decisions(r) if x[1].startswith("AG>TOOL")] == [
            ("escalate", "prohibition"), ("deny", "default")]
        assert r["instances"][0]["institutional_events"] == []

    def test_g_g_unchecked_file(self):
        # The host binds k although no party does: then only SUP[1] has the
        # power. Without the binding nobody has it.
        for bindings, pausers in (({"k": 1}, {"SUP[1]"}), ({}, set())):
            for who in ("SUP[1]", "SUP[3]"):
                r = run_unchecked(unchecked("g_g.cadl"), parse_run_trace(trace(
                    (0, None), (1, m(who, "AG", "pause")), (2, self.ACT), E_,
                    bindings=bindings)))
                paused = decisions(r)[-1][2:4] == ("allow", "permission")
                assert paused == (who in pausers), (bindings, who)

    def test_p2_unknown_occurrence_does_not_fulfil_an_obligation(self):
        # g_ob.cadl: the content `AG -> SUP[k] : answer` with k unbound. The
        # obligation is violated and its reparation, a prohibition, starts.
        r = run_unchecked(unchecked("g_ob.cadl"), parse_run_trace(trace(
            (0, None), (1, m("OP", "AG", "ask")), (2, m("AG", "SUP[3]", "answer")),
            (20, m("AG", "TOOL", "read")), E_)))
        assert decisions(r)[-1][2:5] == ("deny", "prohibition", ["lock"])
        assert norm_states(r)["ob"] == ("violated", "violated")

    def test_p2_with_the_binding_the_obligation_is_fulfilled(self):
        r = replay(review("g_ob.cadl"), trace(
            (0, None), (1, m("OP", "AG", "ask")), (2, m("AG", "SUP[3]", "answer")),
            (3, m("AG", "SUP[2]", "answer")), (20, m("AG", "TOOL", "read")), E_,
            bindings={"k": 2}))
        assert norm_states(r)["ob"][0] == "fulfilled"
        assert decisions(r)[-1][2:4] == ("allow", "permission")

    def test_v4_w2_unchecked(self):
        # Approvals that can never be created, a permission any member ends.
        r = run_unchecked(unchecked("w2.cadl"), parse_run_trace(trace(
            (0, None), (1, self.ACT), (2, m("OP", "W[1]", "approve", "h-1")),
            (4, m("AG", "TOOL", "read")), (5, m("W[3]", "AG", "stop")),
            (6, m("AG", "TOOL", "read")), (700, None, {"end": False}))))
        assert [x[2:4] for x in decisions(r)] == [
            ("escalate", "prohibition"), ("allow", "default"), ("allow", "permission"),
            ("allow", "default"), ("deny", "default"), ("deny", "expired")]


class TestEventsNameDeclaredMembers:
    """P3 and the wording of G-17."""

    def test_g_a_literal_index_outside_the_declared_set(self):
        errors = rule_errors(unchecked("g_a.cadl"), "G-17")
        assert len(errors) == 1 and "'SUP[7]'" in errors[0]
        assert "outside the declared set" in errors[0]

    @pytest.mark.parametrize("ref, count", [
        ("R[3]", 0), ("R[4]", 1), ("R[0]", 1), ("R[2..3]", 0), ("R[2..4]", 1), ("R[0..2]", 1),
    ])
    def test_literal_indices_and_ranges(self, ref, count):
        from .test_agents_extension import check, with_rule
        res = check(norms(perm("p", "AG", "AG -> T : read", f'trigger: "{ref} -> AG : go"')))
        errors = with_rule(res.errors, "G-17")
        assert len(errors) == count
        assert all("outside the declared set" in e for e in errors)

    def test_g_c_coverage_is_by_one_party_reference(self):
        # parties SUP[1..2] and SUP[3] together do not cover SUP[1..3].
        errors = rule_errors(unchecked("g_c.cadl"), "G-17")
        assert len(errors) == 2
        assert all("one reference in the parties" in e for e in errors)

    def test_tool_call_of_an_outsider_as_content(self):
        from .test_agents_extension import check, with_rule
        res = check(norms(proh("no_x", "X", "X -> T : wipe")))
        errors = with_rule(res.errors, "G-17")
        assert len(errors) == 1 and "sender 'X'" in errors[0]
        assert "tool call of an actor that is not a party is governed" in errors[0]
        assert "could never occur" not in errors[0]


class TestApiRefusesInvalidFiles:
    """N1 through the API: `run` and `run_files` check the file."""

    def test_run_refuses_a_file_with_errors(self):
        with pytest.raises(NormsEvalError, match=r"(?s)type-check error.*\(G-17\)"):
            run(unchecked("y2.cadl"), parse_run_trace(['{"t": 0}']))
        with pytest.raises(NormsEvalError, match=r"\(G-18\)"):
            run(unchecked("g_g.cadl"), parse_run_trace(['{"t": 0}']))

    def test_run_files(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text('{"t": 0}\n')
        with pytest.raises(NormsEvalError, match="type-check error"):
            run_files(REVIEW / "y2.cadl", f)

    def test_amendment_to_an_invalid_version(self):
        with pytest.raises(NormsEvalError, match="bad has 2 type-check"):
            replay(review("x2.cadl"), trace((0, None), (1, None, {"amend": "bad"}), E_),
                   versions={"bad": unchecked("g_c.cadl")})

    def test_unsafe_mode_is_explicit(self):
        r = run(unchecked("y2.cadl"), parse_run_trace(['{"t": 0}']), check=False)
        assert r["contracts"] == ["C"]


POWERS = REVIEW / "w1.cadl"


class TestRandomPowersAndObligations:
    """Generated traces with powers and obligations, which the reviewer's
    model does not have."""

    VOCAB = [("AG", "TOOL", "act"), ("AG", "TOOL", "read"), ("AG", "TOOL", "wipe"),
             ("OP", "AG", "approve"), ("OP", "AG", "reject"), ("OP", "AG", "ask"),
             ("SUP[k]", "AG", "pause"), ("SUP[k]", "AG", "arm"), ("SUP[k]", "AG", "halt"),
             ("SUP[k]", "AG", "bless"), ("AG", "SUP[k]", "answer")]

    def lines(self, seed: int, bindings) -> list[str]:
        import random
        rng = random.Random(seed)
        rows, t, holds = [], 0, 0
        for _ in range(200):
            t += rng.choice([0, 1, 1, 2, 5, 30])
            sender, receiver, name = rng.choice(self.VOCAB)
            k = rng.choice(["1", "2", "3", "3", "9"])
            args = [f"h-{rng.randint(max(1, holds - 1), holds + 1)}"] \
                if name in ("approve", "reject") else []
            holds += name == "act"
            rows.append((t, m(sender.replace("k", k), receiver.replace("k", k), name, *args)))
        return trace(*rows, (t + 1, None, {"end": False}), bindings=bindings)

    def power_uses(self, r: dict) -> list[str]:
        return [h["reason"] for n in r["instances"][0]["norms"] for h in n["history"]
                if h["reason"].startswith("power ")]

    @pytest.mark.parametrize("name", ["w1.cadl", "g_ob.cadl"])
    def test_no_effect_through_a_performer_the_instance_does_not_determine(self, name):
        effects = 0
        for seed in range(10):
            # Unbound: nobody exercises a power, no event, nothing fulfilled.
            r = run_unchecked(unchecked(name), parse_run_trace(self.lines(seed, {})))
            assert self.power_uses(r) == [], seed
            inst = r["instances"][0]
            assert all(e["name"] in ("approval", "rejection")
                       for e in inst["institutional_events"])
            assert all(n["state"] != "fulfilled" for n in inst["norms"]
                       if n["kind"] == "obligation"), seed
            assert all(x["message"]["name"] != "wipe" for x in r["audit"]
                       if x.get("decision") == "allow")
            # Bound: only SUP[k] acts, and an obligation is fulfilled only by
            # a message to SUP[k] that was allowed.
            for k in (1, 3):
                r = replay(review(name), self.lines(seed, {"k": k}))
                uses = self.power_uses(r)
                assert all(u.endswith(f"exercised by SUP[{k}]") for u in uses), (seed, k)
                effects += len(uses)
                inst = r["instances"][0]
                assert all(e["performer"] == f"SUP[{k}]" for e in inst["institutional_events"]
                           if e["name"] in ("halted", "blessed"))
                answered = any(x.get("decision") == "allow"
                               and x["message"] == {"from": "AG", "to": f"SUP[{k}]",
                                                    "name": "answer"}
                               for x in r["audit"])
                for n in inst["norms"]:
                    if n["kind"] == "obligation" and n["state"] == "fulfilled":
                        assert answered, (seed, k)
                        effects += 1
        assert effects >= (10 if name == "w1.cadl" else 1)


class TestLowerBoundsUnderOpa:
    """lb.cadl of the review: sets `R[2..N]`, `B[2..4]`, `Z[0..N]`, tool
    servers `T[1..N]`; `decide` and the generated Rego agree."""

    @pytest.mark.skipif(OPA is None, reason="the opa binary is not on PATH")
    def test_decide_and_rego(self, tmp_path):
        import itertools
        import random
        sos = review("lb.cadl")
        policy = build_policy(sos)
        actors = ["OP", "R", "T", "X[1]"] + [
            f"{n}[{k}]" for n in "RBZT" for k in (0, 1, 2, 3, 4, 5, 10 ** 30)]
        rng = random.Random(1)
        inputs = []
        for sender, receiver in itertools.product(actors, actors):
            for name in ("act", "read", "g", "zz"):
                if rng.random() > .12:
                    continue
                inp = {"message": {"from": sender, "to": receiver, "name": name}}
                if rng.random() < .7:
                    inp["instances"] = [
                        {"contract": c, "instance": f"n{j}", "bindings": b, "in_force": {},
                         "derived": [{"norm": "may_act", "holder": sender,
                                      "chain": ["OP", sender]}] if c == "C" else []}
                        for j, (c, b) in enumerate(
                            (c, b) for c in ("C", "D")
                            for b in rng.sample([{}, {"i": 0}, {"i": 2}, {"i": 5}],
                                                rng.choice([0, 1, 2])))]
                inputs.append(inp)
        expected = [decide(policy, i) for i in inputs]
        assert len(inputs) >= 400 and len({e["reason"] for e in expected}) >= 5
        generate(sos, tmp_path, target="agent-rego")
        f = tmp_path / "inputs.json"
        f.write_text(json.dumps(inputs))
        proc = subprocess.run(
            [OPA, "eval", "--format", "json", "--data", str(tmp_path / "agent_policy.rego"),
             "--input", str(f),
             "[r | x := input[_]; r := data.cadl.agent_policy.result with input as x]"],
            capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["result"][0]["expressions"][0]["value"] == expected


# === Findings of the fourth review ===============================================================

class TestIndexVariablesInPredicates:
    """Q1: G-18 also for predicates, and their fail-closed resolution."""

    def test_q_pred2_is_rejected(self):
        errors = rule_errors(unchecked("q_pred2.cadl"), "G-18")
        assert len(errors) == 2
        assert any("until: of norm 'may_read'" in e and "'j'" in e for e in errors)
        assert any("trigger: of norm 'stop_act'" in e for e in errors)

    @pytest.mark.parametrize("blocks, count", [
        (('counts_as:\n  - { when: "R[j].load > 5", creates: hot }\n',), 1),
        (('counts_as:\n  - { when: "OP -> AG : go", creates: hot, if: "R[j].load > 5" }\n',), 1),
        (('assume:\n  - "R[j].load < 9"\n',), 1),
        (('guarantee:\n  - "R[j].load < 9 AND R[q].load > 0"\n',), 2),
        (('guarantee:\n  - "for all j in R: R[j].load < 9"\n',), 0),
        (('guarantee:\n  - "R[2].load < 9"\n',), 0),
    ])
    def test_predicates_of_the_contract(self, blocks, count):
        from .test_agents_extension import check, with_rule
        assert len(with_rule(check(*blocks).errors, "G-18")) == count
        bound = '[OP, SUP, AG, SUB, T, "R[j]"]'
        expected = 1 if "R[q]" in blocks[0] else 0
        assert len(with_rule(check(*blocks, parties=bound).errors, "G-18")) == expected

    def test_predicates_of_norms_and_delegations(self):
        from .test_agents_extension import check, with_rule
        entry = ('- id: n\n  kind: obligation\n  bearer: AG\n  trigger: "R[j].load > 5"\n'
                 '  until: "R[j].load < 1"\n  content: "R[j].done == true"\n')
        assert len(with_rule(check(norms(entry)).errors, "G-18")) == 3
        res = check(norms(perm("p", "OP", "OP -> T : read")),
                    'delegations:\n  - { norm: p, to: [AG], grant: g, until: "R[j].load > 5" }\n')
        assert len(with_rule(res.errors, "G-18")) == 1

    def test_runtime_resolves_an_unknown_predicate_fail_closed(self):
        # qp.jsonl: the prohibition is triggered and the permission ended,
        # although no `W[j].alarm` becomes true for a bound j.
        sos = unchecked("q_pred2.cadl")
        lines = (REVIEW / "qp.jsonl").read_text().splitlines()
        r = run_unchecked(sos, parse_run_trace(lines))
        assert [x[1:5] for x in decisions(r)] == [
            ("AG>TOOL:act", "deny", "prohibition", ["stop_act"]),
            ("AG>TOOL:read", "deny", "default", [])]
        assert r["warnings"] and all("matches any" not in w for w in r["warnings"])
        assert any("unknown and is resolved fail-closed" in w for w in r["warnings"])
        with pytest.raises(NormsEvalError, match=r"\(G-18\)"):
            run(sos, parse_run_trace(lines))

    def test_unknown_predicate_creates_no_event_and_fulfils_nothing(self):
        src = source(
            """
            counts_as:
              - { when: "R[j].load > 5", creates: hot }
              - { when: "OP -> AG : go", creates: gone, if: "R[j].load > 5" }
            """,
            norms(perm("p_hot", "AG", "AG -> T : read", "trigger: hot"),
                  perm("p_gone", "AG", "AG -> T : write", "trigger: gone"),
                  """
                  - id: must
                    kind: obligation
                    bearer: AG
                    deadline: 5s
                    content: "R[j].done == true"
                  """))
        r = run_unchecked(parse(src), parse_run_trace(trace(
            (0, None, {"values": {"R[j].load": 1, "R[j].done": False}}),
            (1, m("OP", "AG", "go"), {"values": {"R[j].load": 9, "R[j].done": True,
                                                 "R[1].load": 9, "R[1].done": True}}),
            (2, [m("AG", "T", "read"), m("AG", "T", "write")]),
            (9, None, {"end": False}))))
        assert [x[2:4] for x in decisions(r)] == [
            ("allow", "default"), ("deny", "default"), ("deny", "default")]
        assert r["instances"][0]["institutional_events"] == []
        assert norm_states(r)["must"][0] == "violated"


class TestNamesOfAnInstance:
    """R1: the quantity names of an instance come from its own observations
    (F.5); a line after its end has no effect on it."""

    Q = REVIEW / "q_names.cadl"
    BASE = ['{"t":0,"values":{"A.mode":"fast"}}', '{"t":2,"end":true}']

    def state(self, lines) -> tuple:
        r = replay(parse_file(self.Q), lines)
        return norm_states(r)["n"]

    def test_a_line_after_the_end_does_not_change_the_verdict(self):
        after = self.BASE + ['{"t":3,"values":{"target":"fast"}}']
        assert self.state(self.BASE) == ("violated", "violated")
        assert self.state(after) == ("violated", "violated")

    def test_a_name_is_a_quantity_from_its_first_value_on(self):
        # The runtime decides causally (fifth review, D1): `target` is a
        # symbolic value at t=0, so `A.mode != target` holds there and the
        # prohibition is violated, although the trace gives `target` a
        # value at t=1. With `quantities` the name is a quantity from the
        # start: undefined at t=0, and equal to A.mode from t=1 on.
        during = [self.BASE[0], '{"t":1,"values":{"target":"fast"}}', self.BASE[1]]
        assert self.state(during)[0] == "violated"
        given = ['{"t":0,"quantities":["target"],"values":{"A.mode":"fast"}}'] + during[1:]
        assert self.state(given)[0] == "fulfilled"
        before = ['{"t":0,"values":{"A.mode":"fast","target":"fast"}}', self.BASE[1]]
        assert self.state(before)[0] == "fulfilled"

    def test_host_created_instances_have_their_own_names(self):
        sos = parse_file(self.Q)
        entry = dict(contract="C")
        r = replay(sos, trace(
            (0, None, {"values": {"A.mode": "fast"},
                       "instances": [dict(entry, instance="a")]}),
            (1, None, {"instances": [dict(entry, instance="a", end=True)]}),
            (2, None, {"values": {"target": "fast"},
                       "instances": [dict(entry, instance="b")]}),
            (3, None, {"end": True})))
        assert [norm_states(r, k)["n"][0] for k in (0, 1)] == ["violated", "fulfilled"]


class TestBindingsAreMembers:
    """R2: the bindings of an instance against the declared sets."""

    ACT = m("AG", "TOOL", "act")

    @pytest.mark.parametrize("value", [9, 0, -1])
    def test_value_outside_the_declared_set(self, value):
        with pytest.raises(TraceError) as err:
            replay(review("x1.cadl"), trace(
                (0, None, {"instances": [c1("a", bindings={"i": value})]}), (1, self.ACT)))
        text = str(err.value)
        assert "line 1: instance a of C1 binds the index 'i'" in text
        assert f"to {value}, which is not a member of the declared set OP" in text

    def test_default_instance_and_symbolic_upper_bound(self):
        with pytest.raises(TraceError, match="binds the index 'k'.* to 4"):
            replay(review("w1.cadl"), trace((0, None), (1, self.ACT), bindings={"k": 4}))
        lb = review("lb.cadl")          # party R[i], declared as R[2..N]
        d = dict(contract="D", instance="d")
        for value, ok in ((1, False), (2, True), (500, True)):
            lines = trace((0, None, {"instances": [
                dict(contract="C", instance="c", bindings={"i": value}), d]}),
                (1, None, {"end": False}))
            if ok:
                assert replay(lb, lines)["instances"][0]["bindings"] == {"i": value}
            else:
                with pytest.raises(TraceError, match="not a member of the declared set R"):
                    replay(lb, lines)

    def test_binding_of_a_variable_that_no_party_has(self):
        r = replay(review("w1.cadl"), trace((0, None), (1, self.ACT), E_,
                                            bindings={"k": 1, "z": 7}))
        assert r["warnings"] == [
            "C/default: the binding of 'z' is ignored for the parties: no party of C is "
            "written with [z]"]
        assert decisions(r)[0][2] == "escalate"


# === Findings of the fifth review ================================================================

class TestDecisionsAreCausal:
    """D1: a decision depends only on what was observed at or before it."""

    def lines(self, name: str, **first) -> list[str]:
        lines = (REVIEW / name).read_text().splitlines()
        head = json.loads(lines[0])
        head.update(first)
        return [json.dumps(head)] + lines[1:]

    def test_a_later_line_does_not_change_an_earlier_decision(self):
        # ql2 gives `locked` a value at t=2. At t=1 the name has no value
        # yet and is a symbolic value in both traces: `AG.mode == locked`
        # holds, the prohibition is in force.
        sos = review("q_look.cadl")
        one, two = replay(sos, self.lines("ql1.jsonl")), replay(sos, self.lines("ql2.jsonl"))
        expected = (1, "AG>TOOL:act", "deny", "prohibition", ["stop_act"])
        assert decisions(one)[0] == expected and decisions(two)[0] == expected
        assert one["warnings"] == []
        assert len(two["warnings"]) == 1 and "line 3: locked" in two["warnings"][0]
        assert "'quantities'" in two["warnings"][0]

    def test_with_the_quantity_names_given(self):
        # `locked` is a quantity from the start and has no value at t=1: the
        # predicate is undefined, an undefined predicate does not occur
        # (F.5), the prohibition is not triggered, and may_act allows. The
        # same in both traces, and no warning.
        sos = review("q_look.cadl")
        for name in ("ql1.jsonl", "ql2.jsonl"):
            r = replay(sos, self.lines(name, quantities=["locked"]))
            assert decisions(r)[0][2:5] == ("allow", "permission", ["may_act"]), name
            assert not any("first value" in w for w in r["warnings"])

    def test_quantities_key(self):
        with pytest.raises(TraceError, match="only on the first line"):
            parse_run_trace(['{"t": 0}', '{"t": 1, "quantities": ["x"]}'])
        with pytest.raises(TraceError, match="list of names"):
            parse_run_trace(['{"t": 0, "quantities": "x"}'])

    def prefix_holds(self, sos, lines: list[str], step: int, **kw) -> int:
        """The decisions of the run on a prefix are the first decisions of
        the run on the whole trace."""
        def outcome(r):
            return [(s["time"], s["message"], s["result"], s["hold_id"]) for s in r["steps"]]
        full = outcome(run(sos, parse_run_trace(lines), **kw))
        checked = 0
        for cut in range(1, len(lines), step):
            part = outcome(run(sos, parse_run_trace(lines[:cut]), **kw))
            assert part == full[:len(part)], cut
            checked += len(part)
        return checked

    def test_prefix_property_with_names_that_get_values_late(self):
        import random
        sos = review("q_look.cadl")
        names = ["AG.mode", "locked", "open", "x"]
        values = ["locked", "open", 0, 1, "x"]
        total = 0
        for seed in range(12):
            rng = random.Random(seed)
            rows = []
            for t in range(40):
                more = {}
                if rng.random() < 0.4:
                    more["values"] = {rng.choice(names): rng.choice(values)}
                msg_ = m("AG", "TOOL", "act") if rng.random() < 0.7 else None
                rows.append((t, msg_, more))
            total += self.prefix_holds(sos, trace(*rows), 1)
            given = trace(*rows, quantities=["locked", "open"])
            total += self.prefix_holds(sos, given, 3)
        assert total >= 5000

    def test_prefix_property_on_generated_traces(self):
        total = 0
        for name, sos in (("lifecycle", parse(LIFECYCLE)), ("holds", parse(HOLDS)),
                          ("change", parse_file(CHANGE)), ("x3", review("x3.cadl"))):
            lines = random_trace(sos, 77, 120)
            total += self.prefix_holds(sos, lines, 7)
        sos = review("x1.cadl")
        total += self.prefix_holds(sos, random_host_trace(sos, 78, 120), 7)
        assert total >= 3000


class TestEveryFreeIndexVariable:
    """D2: G-18 for a variable inside an index expression and inside the
    arguments of a message event; binders are a party, a quantifier and a
    comprehension."""

    @pytest.mark.parametrize("name, count", [
        ("q_e1.cadl", 2), ("q_arg.cadl", 2), ("q_pred2.cadl", 2),
        ("q_e2.cadl", 0), ("q_e3.cadl", 0), ("q_two.cadl", 0),
    ])
    def test_reproductions(self, name, count):
        sos = unchecked(name)
        assert len(rule_errors(sos, "G-18")) == count
        assert type_check(sos).ok == (count == 0)

    @pytest.mark.parametrize("predicate, variables", [
        ("R[j+1].load > 5", ["j"]),
        ("R[R[k].next].load > 5", ["k"]),
        ("R[2*(a+b)].load > 5", ["a", "b"]),
        ("sum(R[j].load for j in R) > 5", []),
        ("sum(R[j].load for j in R) > R[q].load", ["q"]),
        ("exists j in R: R[j].load > R[j+1].load", []),
        ("for all j in R: R[j].load > R[z].load", ["z"]),
        ("R[1].load > limit", []),            # a bare name is a value, no index
        ("R[*].load > 5", []),
    ])
    def test_free_variables_of_a_predicate(self, predicate, variables):
        from .test_agents_extension import check, with_rule
        res = check(f'guarantee:\n  - "{predicate}"\n')
        errors = with_rule(res.errors, "G-18")
        assert len(errors) == len(variables)
        for var in variables:
            assert any(f"'{var}'" in e for e in errors), var

    def test_arguments_of_message_events(self):
        from .test_agents_extension import check, with_rule
        blocks = ('counts_as:\n  - { when: "OP -> AG : note(R[j].load)", creates: noted }\n',
                  norms(perm("p", "AG", "AG -> T : read",
                             'trigger: "OP -> AG : go(R[j+1].load, 3)"')))
        errors = with_rule(check(*blocks).errors, "G-18")
        assert len(errors) == 2 and all("arguments" in e for e in errors)
        bound = '[OP, SUP, AG, SUB, T, "R[j]"]'
        assert not with_rule(check(*blocks, parties=bound).errors, "G-18")

    def test_runtime_unknown_arguments_follow_the_rule(self):
        # q_arg.cadl in the unsafe mode: the trigger of the prohibition is
        # unknown and occurs; the until of the permission is unknown and
        # occurs. Before, both were resolved as not occurring.
        r = run_unchecked(unchecked("q_arg.cadl"), parse_run_trace(trace(
            (0, [m("AG", "TOOL", "act"), m("AG", "TOOL", "read")]),
            (1, [m("OP", "AG", "lock", 5), m("OP", "AG", "stop", 5)]),
            (2, [m("AG", "TOOL", "act"), m("AG", "TOOL", "read")]), E_)))
        tool = [x[2:4] for x in decisions(r) if x[1].startswith("AG>TOOL")]
        assert tool == [("allow", "permission"), ("allow", "permission"),
                        ("deny", "prohibition"), ("deny", "default")]
        assert sum("in the arguments of" in w for w in r["warnings"]) == 2

    def test_runtime_unknown_index_expression(self):
        r = run_unchecked(unchecked("q_e1.cadl"), parse_run_trace(trace(
            (0, None, {"values": {"W[1].alarm": False}}),
            (1, [m("AG", "TOOL", "act"), m("AG", "TOOL", "read")]), E_)))
        assert [x[2:4] for x in decisions(r)] == [("deny", "prohibition"), ("deny", "default")]


TWO_SETS = """
sos:
  name: Two
  extensions:
    - norms: 0.1
    - agents: 0.1
  actors:
    - { id: OP, role: o, agent: { kind: human } }
    - { id: "W[1..3]", role: w, agent: { kind: llm_agent } }
    - { id: "S[1..2]", role: s, agent: { kind: human } }
    - { id: AG, role: a, agent: { kind: llm_agent } }
    - { id: TOOL, role: t, agent: { kind: tool_server, tools: [act] } }
  contracts:
    - id: C
      parties: [OP, "W[i]", "S[*]", AG, TOOL]
      enforcement: {}
      norms:
        - { id: may_act, kind: permission, bearer: AG, content: "AG -> TOOL : act" }
        - { id: stop, kind: prohibition, bearer: AG, trigger: "S[i] -> AG : lock", content: "AG -> TOOL : act" }
        - { id: watch, kind: permission, bearer: AG, until: "S[i].alarm == true", content: "AG -> TOOL : act" }
"""


class TestBindingAgainstEverySet:
    def test_index_of_a_party_used_with_another_set(self):
        sos = parse(TWO_SETS)
        assert type_check(sos).ok
        from cadl.agents import contract_index_sets
        assert contract_index_sets(sos.contracts[0]) == {"i": {"W", "S"}}
        with pytest.raises(TraceError) as err:
            replay(sos, trace((0, None), E_, bindings={"i": 3}))
        assert "binds the index 'i' of its party W[i] to 3" in str(err.value)
        assert "not a member of the declared set S" in str(err.value)
        r = replay(sos, trace((0, None), (1, m("S[1]", "AG", "lock")),
                              (2, m("AG", "TOOL", "act")),
                              (3, m("S[2]", "AG", "lock")), (4, m("AG", "TOOL", "act")), E_,
                              bindings={"i": 2}))
        assert [x[2] for x in decisions(r) if x[1] == "AG>TOOL:act"] == ["allow", "deny"]

    def test_q_two_binds_both_parties(self):
        sos = review("q_two.cadl")
        replay(sos, trace((0, None), E_, bindings={"i": 1}))
        with pytest.raises(TraceError, match="not a member of the declared set"):
            replay(sos, trace((0, None), E_, bindings={"i": 99}))
