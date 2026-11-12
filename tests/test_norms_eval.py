"""Offline verdict engine of the norms extension (Appendix F, F.4 and F.5):
`cadl norms-eval` and `cadl.norms_eval`."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cadl.norms_eval import NormsEvalError, TraceError, evaluate, parse_trace
from cadl.parser import parse
from cadl.type_checker import type_check

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "norms_robot_delivery.cadl"
TRACES = ROOT / "examples" / "traces"
V1 = ROOT / "examples" / "norms_amendment_v1.cadl"
V2 = ROOT / "examples" / "norms_amendment_v2.cadl"


def make_sos(blocks, declare: bool = True, check: bool = True):
    """A file with actors D, R[1..3], X and a contract C between D and
    R[*], whose keys are `blocks` (one text or several, each written flush
    left)."""
    ext = "  extensions:\n    - norms: 0.1\n" if declare else ""
    src = (
        "sos:\n  name: T\n" + ext +
        "  actors:\n"
        "    - {id: D, role: d}\n"
        "    - {id: \"R[1..3]\", role: r}\n"
        "    - {id: X, role: x}\n"
        "  contracts:\n"
        "    - id: C\n"
        "      parties: [D, \"R[*]\"]\n"
        + "".join(textwrap.indent(textwrap.dedent(b), "      ")
                  for b in ([blocks] if isinstance(blocks, str) else blocks))
    )
    sos = parse(src)
    if declare and check:
        result = type_check(sos)
        assert result.ok, result.errors
    return sos


def run(blocks, observations: list, declare: bool = True, versions=None) -> dict:
    """Evaluate the contract C of `make_sos(blocks)` on a trace given as a
    list of observations. The index i is bound to 1 unless the first
    observation binds it. `versions` maps the names used by "amend" to the
    blocks of new versions."""
    obs = [dict(o) for o in observations]
    obs[0].setdefault("bindings", {"i": 1})
    new = {k: make_sos(v) for k, v in (versions or {}).items()}
    return evaluate(make_sos(blocks, declare), parse_trace(json.dumps(o) for o in obs),
                    versions=new)


def msg(sender: str, receiver: str, name: str, *args) -> dict:
    return {"from": sender, "to": receiver, "name": name, "args": list(args)}


def norm(result: dict, norm_id: str) -> dict:
    return next(n for n in result["norms"] if n["id"] == norm_id)


def states(result: dict, norm_id: str) -> list:
    return [(h["t"], h["state"]) for h in norm(result, norm_id)["history"]]


LIFECYCLE = """
lifecycle:
  states: [Open, Done, Failed]
  initial: Open
  terminal: [Done, Failed]
  transitions:
    - { id: finish, from: Open, to: Done, on: "done == true" }
"""


# === Activation (F.5, item 1) ===

class TestActivation:
    NORMS = """
    counts_as:
      - when: "D -> R[i] : replan"
        creates: reassignment
    norms:
      - {id: always, kind: prohibition, bearer: "R[i]", content: "speed > 5"}
      - id: first
        kind: obligation
        bearer: "R[i]"
        trigger: "D -> R[i] : go"
        content: "R[i] -> D : ack"
        deadline: 5s
        on_violation: {reparation: repair}
      - {id: repair, kind: obligation, bearer: "R[i]", content: "R[i] -> D : sorry", deadline: 2s}
      - {id: granted, kind: permission, bearer: "R[i]", content: "R[i] -> D : rest"}
      - {id: grant, kind: power, bearer: D, content: "D -> R[i] : allow", effects: {activate: [granted]}}
      - {id: on_event, kind: obligation, bearer: "R[i]", trigger: reassignment, content: "R[i] -> D : ack2", deadline: 9s}
      - {id: on_pred, kind: obligation, bearer: "R[i]", trigger: "R[i].battery < 20", content: "R[i] -> D : low", deadline: 9s}
    """

    def test_triggerless_norms_start_active_unless_targets(self):
        r = run(self.NORMS, [{"t": 2.0}, {"t": 3.0, "end": False}])
        assert norm(r, "always")["state"] == "active"
        assert norm(r, "always")["activated_at"] == 2.0
        assert norm(r, "grant")["state"] == "active"
        # no trigger, but a reparation / an activate: target: stays inactive
        assert norm(r, "repair")["state"] == "inactive"
        assert norm(r, "granted")["state"] == "inactive"
        assert norm(r, "first")["state"] == "inactive"
        assert norm(r, "first")["verdict"] == "pending"

    def test_trigger_by_message_event_and_predicate(self):
        r = run(self.NORMS, [
            {"t": 0, "values": {"R[1].battery": 50}},
            {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            {"t": 2, "messages": [msg("D", "R[1]", "replan")]},
            {"t": 3, "values": {"R[1].battery": 10}},
            {"t": 4, "messages": [msg("D", "R[1]", "allow")]},
            {"t": 5, "end": False},
        ])
        assert norm(r, "first")["activated_at"] == 1
        assert norm(r, "on_event")["activated_at"] == 2
        assert norm(r, "on_pred")["activated_at"] == 3
        assert norm(r, "granted")["activated_at"] == 4
        assert r["institutional_events"] == [
            {"t": 2, "name": "reassignment", "performer": "D", "source": "D -> R[1] : replan"}]

    def test_message_of_another_instance_does_not_trigger(self):
        r = run(self.NORMS, [{"t": 0}, {"t": 1, "messages": [msg("D", "R[2]", "go")]},
                             {"t": 2, "end": False}])
        assert norm(r, "first")["state"] == "inactive"

    def test_predicate_trigger_true_at_first_observation(self):
        r = run(self.NORMS, [{"t": 0, "values": {"R[1].battery": 5}}, {"t": 1, "end": False}])
        assert norm(r, "on_pred")["activated_at"] == 0

    def test_predicate_trigger_needs_an_edge(self):
        # true -> true is not "becomes true"; a second edge is a re-trigger
        r = run(self.NORMS, [
            {"t": 0, "values": {"R[1].battery": 50}},
            {"t": 1, "values": {"R[1].battery": 10}},
            {"t": 2, "messages": [msg("R[1]", "D", "low")]},
            {"t": 3, "values": {"R[1].battery": 50}},
            {"t": 4, "values": {"R[1].battery": 10}},
            {"t": 5, "end": False},
        ])
        assert states(r, "on_pred") == [(1, "active"), (2, "fulfilled")]
        ignored = [e for e in r["timeline"] if e["kind"] == "ignored"]
        assert [(e["t"], e["norm"]) for e in ignored] == [(4, "on_pred")]

    def test_retrigger_ignored_when_not_inactive(self):
        r = run(self.NORMS, [
            {"t": 0},
            {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            {"t": 2, "messages": [msg("D", "R[1]", "go")]},     # active: ignored
            {"t": 3, "messages": [msg("R[1]", "D", "ack")]},
            {"t": 4, "messages": [msg("D", "R[1]", "go")]},     # final: ignored
            {"t": 20},
        ])
        assert states(r, "first") == [(1, "active"), (3, "fulfilled")]
        assert norm(r, "first")["activated_at"] == 1
        assert [e["t"] for e in r["timeline"] if e["kind"] == "ignored"] == [2, 4]


# === Obligations (item 2) ===

class TestObligation:
    NORMS = """
    norms:
      - id: ack
        kind: obligation
        bearer: "R[i]"
        trigger: "D -> R[i] : go"
        content: "R[i] -> D : ack(accepted)"
        deadline: 5s
        on_violation: {severity: Critical, sanction: "fined"}
    """

    def trace(self, *rest):
        return [{"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]}, *rest]

    def test_fulfilled_at_first_content(self):
        r = run(self.NORMS, self.trace(
            {"t": 3, "messages": [msg("R[1]", "D", "ack", "accepted")]}, {"t": 30}))
        n = norm(r, "ack")
        assert (n["state"], n["verdict"], n["time"]) == ("fulfilled", "fulfilled", 3)
        assert r["ok"] and not r["violations"]

    def test_arguments_must_match(self):
        r = run(self.NORMS, self.trace(
            {"t": 2, "messages": [msg("R[1]", "D", "ack", "rejected")]},
            {"t": 3, "messages": [msg("R[1]", "D", "ack")]},
            {"t": 4, "messages": [msg("R[2]", "D", "ack", "accepted")]},
            {"t": 7}))
        assert norm(r, "ack")["state"] == "violated"

    def test_content_at_activation_time_counts(self):
        # step 3 activates, step 4 judges at the same observation
        r = run(self.NORMS, [{"t": 0}, {"t": 1, "messages": [
            msg("D", "R[1]", "go"), msg("R[1]", "D", "ack", "accepted")]}, {"t": 9}])
        assert states(r, "ack") == [(1, "active"), (1, "fulfilled")]

    def test_content_before_activation_does_not_count(self):
        r = run(self.NORMS, [{"t": 0, "messages": [msg("R[1]", "D", "ack", "accepted")]},
                             {"t": 1, "messages": [msg("D", "R[1]", "go")]}, {"t": 9}])
        assert norm(r, "ack")["state"] == "violated"

    def test_content_exactly_at_deadline_is_in_time(self):
        r = run(self.NORMS, self.trace(
            {"t": 6, "messages": [msg("R[1]", "D", "ack", "accepted")]}, {"t": 9}))
        assert norm(r, "ack")["state"] == "fulfilled"

    def test_violated_at_first_observation_after_deadline(self):
        r = run(self.NORMS, self.trace(
            {"t": 6}, {"t": 6.5, "messages": [msg("R[1]", "D", "ack", "accepted")]}, {"t": 9}))
        n = norm(r, "ack")
        assert (n["state"], n["verdict"], n["time"]) == ("violated", "violated", 6.5)
        assert "passed at t=6 " in n["reason"]
        assert r["violations"] == [{
            "t": 6.5, "norm": "ack", "severity": "Critical", "verdict": "violated",
            "sanction": "fined", "reparation": None, "reparation_activated": False,
            "transition": None}]
        assert not r["ok"] and r["violated"] == ["ack"]

    def test_trace_prefix_before_deadline_stays_pending(self):
        r = run(self.NORMS, self.trace({"t": 3, "end": False}))
        assert (norm(r, "ack")["state"], norm(r, "ack")["verdict"]) == ("active", "pending")
        assert r["instance"]["ended"] is False and r["ok"]

    def test_predicate_content(self):
        r = run("""
        norms:
          - {id: charge, kind: obligation, bearer: "R[i]", content: "R[i].battery >= 80", deadline: 10s}
        """, [{"t": 0, "values": {"R[1].battery": 20}}, {"t": 4, "values": {"R[1].battery": 85}},
              {"t": 20}])
        assert states(r, "charge") == [(0, "active"), (4, "fulfilled")]


# === Prohibitions (item 3) ===

class TestProhibition:
    NORMS = """
    norms:
      - id: no_speeding
        kind: prohibition
        bearer: "R[i]"
        trigger: "D -> R[i] : go"
        content: "R[i].speed > 5"
        deadline: 10s
        on_violation: {severity: Minor}
      - {id: never, kind: prohibition, bearer: "R[i]", content: "R[i] -> D : spam"}
    """

    def test_violated_once(self):
        r = run(self.NORMS, [
            {"t": 0, "values": {"R[1].speed": 9}},            # not yet active
            {"t": 1, "messages": [msg("D", "R[1]", "go")], "values": {"R[1].speed": 1}},
            {"t": 2, "values": {"R[1].speed": 7}},
            {"t": 3, "values": {"R[1].speed": 1}},
            {"t": 4, "values": {"R[1].speed": 8}, "messages": [msg("R[1]", "D", "spam")]},
            {"t": 5, "messages": [msg("R[1]", "D", "spam")]},
            {"t": 6},
        ])
        assert states(r, "no_speeding") == [(1, "active"), (2, "violated")]
        assert states(r, "never") == [(0, "active"), (4, "violated")]
        # one violation per norm instance
        assert [(v["t"], v["norm"], v["severity"]) for v in r["violations"]] == [
            (2, "no_speeding", "Minor"), (4, "never", "Major")]

    def test_window_closes(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")], "values": {"R[1].speed": 1}},
            {"t": 11}, {"t": 12, "values": {"R[1].speed": 9}}, {"t": 13, "end": False},
        ])
        # t=11 is the end of the window (inclusive); closed at the next observation
        assert states(r, "no_speeding") == [(1, "active"), (12, "fulfilled")]
        assert norm(r, "no_speeding")["verdict"] == "fulfilled"
        assert norm(r, "never")["state"] == "active"

    def test_content_at_the_end_of_the_window_violates(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")], "values": {"R[1].speed": 1}},
            {"t": 11, "values": {"R[1].speed": 9}}, {"t": 13},
        ])
        assert norm(r, "no_speeding")["state"] == "violated"


# === Suspension, until, powers (items 5 and 7) ===

class TestPowers:
    NORMS = """
    counts_as:
      - when: "D -> R[i] : replan"
        creates: reassignment
      - when: "X -> R[i] : replan"
        creates: reassignment
      - when: "R[i].stuck == true"
        creates: stuck
        if: "R[i].battery > 0"
    norms:
      - {id: ack, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : go", content: "R[i] -> D : ack", deadline: 5s}
      - {id: quiet, kind: prohibition, bearer: "R[i]", content: "R[i] -> D : noise", deadline: 10s}
      - {id: extra, kind: permission, bearer: "R[i]", content: "R[i] -> D : rest"}
      - {id: pause, kind: power, bearer: D, content: "D -> R[i] : pause", effects: {suspend: [ack, quiet]}}
      - {id: resume, kind: power, bearer: D, content: "D -> R[i] : resume", effects: {resume: [ack, quiet]}}
      - {id: reassign, kind: power, bearer: D, content: reassignment, until: "D -> R[i] : lock", effects: {discharge: [ack], activate: [extra]}}
    """

    def test_deadline_does_not_count_suspended_time(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            {"t": 3, "messages": [msg("D", "R[1]", "pause")]},
            # suspended: content does not fulfil, the clock is stopped
            {"t": 20, "messages": [msg("R[1]", "D", "ack")]},
            {"t": 30, "messages": [msg("D", "R[1]", "resume")]},
            {"t": 33, "messages": [msg("R[1]", "D", "ack")]},     # 2s + 3s = 5s: in time
            {"t": 40},
        ])
        assert states(r, "ack") == [(1, "active"), (3, "suspended"), (30, "active"), (33, "fulfilled")]

    def test_deadline_after_resume(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            {"t": 3, "messages": [msg("D", "R[1]", "pause")]},
            {"t": 30, "messages": [msg("D", "R[1]", "resume")]},
            {"t": 33}, {"t": 34}, {"t": 40},
        ])
        assert states(r, "ack")[-1] == (34, "violated")
        assert "passed at t=33 " in norm(r, "ack")["reason"]

    def test_suspended_prohibition_is_not_judged(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 2, "messages": [msg("D", "R[1]", "pause")]},
            {"t": 3, "messages": [msg("R[1]", "D", "noise")]},
            {"t": 50, "messages": [msg("D", "R[1]", "resume")]},
            {"t": 58}, {"t": 59}, {"t": 60, "end": False},
        ])
        # window: 2s before the suspension + 8s after the resumption
        assert states(r, "quiet") == [(0, "active"), (2, "suspended"), (50, "active"), (59, "fulfilled")]

    def test_power_discharges_and_activates(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            {"t": 2, "messages": [msg("D", "R[1]", "replan")]}, {"t": 9, "end": False},
        ])
        assert states(r, "ack") == [(1, "active"), (2, "discharged")]
        assert norm(r, "ack")["verdict"] == "fulfilled"
        assert norm(r, "extra")["activated_at"] == 2
        assert r["unauthorised"] == []

    def test_act_without_the_power_is_unauthorised(self):
        # X sends the message that counts as a reassignment; X holds no power
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            {"t": 2, "messages": [msg("X", "R[1]", "replan")]}, {"t": 3, "end": False},
        ])
        assert norm(r, "ack")["state"] == "active"
        assert norm(r, "extra")["state"] == "inactive"
        assert r["institutional_events"][0]["performer"] == "X"   # the event still exists
        assert [(u["t"], u["performer"]) for u in r["unauthorised"]] == [(2, "X")]

    def test_act_while_the_power_is_not_active_is_unauthorised(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go"), msg("D", "R[1]", "lock")]},
            {"t": 2, "messages": [msg("D", "R[1]", "replan")]}, {"t": 3, "end": False},
        ])
        assert norm(r, "reassign")["state"] == "discharged"       # by until:
        assert norm(r, "ack")["state"] == "active"
        assert [u["performer"] for u in r["unauthorised"]] == ["D"]

    def test_effect_on_a_norm_in_another_state_does_nothing(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "resume")]},
            {"t": 2, "messages": [msg("D", "R[1]", "replan")]}, {"t": 3, "end": False},
        ])
        assert norm(r, "ack")["state"] == "inactive" and norm(r, "ack")["history"] == []
        assert norm(r, "quiet")["state"] == "active"

    def test_power_effects_before_triggers(self):
        # steps 2 and 3: the discharge finds `ack` inactive and does nothing,
        # then the trigger of the same observation activates it
        r = run(self.NORMS, [
            {"t": 0},
            {"t": 1, "messages": [msg("D", "R[1]", "go"), msg("D", "R[1]", "replan")]},
            {"t": 2, "end": False},
        ])
        assert states(r, "ack") == [(1, "active")]

    def test_counts_as_predicate_with_condition(self):
        r = run(self.NORMS, [
            {"t": 0, "values": {"R[1].stuck": True, "R[1].battery": 0}},   # if: fails
            {"t": 1, "values": {"R[1].stuck": False}},
            {"t": 2, "values": {"R[1].stuck": True, "R[1].battery": 30}},
            {"t": 3, "end": False},
        ])
        assert r["institutional_events"] == [
            {"t": 2, "name": "stuck", "performer": None, "source": "R[i].stuck == true"}]


class TestUntil:
    NORMS = """
    counts_as:
      - when: "D -> R[i] : cancel"
        creates: cancelled
    norms:
      - {id: ack, kind: obligation, bearer: "R[i]", content: "R[i] -> D : ack", deadline: 5s, until: cancelled}
      - {id: quiet, kind: prohibition, bearer: "R[i]", content: "R[i] -> D : noise", until: "R[i].docked == true"}
      - {id: may, kind: permission, bearer: "R[i]", content: "R[i] -> D : rest", until: "D -> R[i] : stop"}
    """

    def test_until_discharges(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 2, "messages": [msg("D", "R[1]", "cancel")]},
            {"t": 3, "values": {"R[1].docked": True}, "messages": [msg("D", "R[1]", "stop")]},
            {"t": 4, "messages": [msg("R[1]", "D", "noise")]}, {"t": 9},
        ])
        assert states(r, "ack") == [(0, "active"), (2, "discharged")]
        assert states(r, "quiet") == [(0, "active"), (3, "discharged")]
        assert states(r, "may") == [(0, "active"), (3, "discharged")]
        assert all(n["verdict"] == "fulfilled" for n in r["norms"])

    PREDICATE_UNTIL = """
    norms:
      - {id: ack, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : go", content: "R[i] -> D : ack", until: "R[i].docked == true"}
    """

    def test_predicate_until_already_true_at_activation(self):
        # "becomes true" is about the observations of the instance, not
        # about the activation: true before and at the activation is no edge
        docked = lambda v: {"R[1].docked": v}
        r = run(self.PREDICATE_UNTIL, [
            {"t": 0, "values": docked(True)},
            {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            {"t": 2}, {"t": 3, "end": False},
        ])
        assert states(r, "ack") == [(1, "active")]
        r = run(self.PREDICATE_UNTIL, [
            {"t": 0, "values": docked(True)},
            {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            {"t": 2, "values": docked(False)},
            {"t": 3, "values": docked(True)}, {"t": 4, "end": False},
        ])
        assert states(r, "ack") == [(1, "active"), (3, "discharged")]

    def test_predicate_until_becomes_true_at_activation(self):
        r = run(self.PREDICATE_UNTIL, [
            {"t": 0, "values": {"R[1].docked": False}},
            {"t": 1, "messages": [msg("D", "R[1]", "go")], "values": {"R[1].docked": True}},
            {"t": 2, "end": False},
        ])
        assert states(r, "ack") == [(1, "active"), (1, "discharged")]

    def test_content_and_until_at_one_observation(self):
        # content is judged before until:
        r = run(self.NORMS, [
            {"t": 0}, {"t": 2, "messages": [msg("D", "R[1]", "cancel"), msg("R[1]", "D", "ack")]},
            {"t": 9},
        ])
        assert states(r, "ack")[-1] == (2, "fulfilled")


# === Order within one observation, on_violation (item 8) ===

class TestOrderAndViolation:
    NORMS = [LIFECYCLE, """
    norms:
      - id: ack
        kind: obligation
        bearer: "R[i]"
        trigger: "D -> R[i] : go"
        content: "R[i] -> D : ack"
        deadline: 5s
        on_violation: {reparation: report}
      - id: report
        kind: obligation
        bearer: "R[i]"
        content: "R[i] -> D : report"
        deadline: 2s
        on_violation: {severity: Critical, transition: Failed}
    """]

    def test_reparation_is_first_judged_at_the_next_observation(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]},
            # the violation is detected here; the report sent at this
            # observation is before the reparation is judged
            {"t": 7, "messages": [msg("R[1]", "D", "report")]},
            {"t": 8}, {"t": 9.5},
        ])
        assert states(r, "ack") == [(1, "active"), (7, "violated")]
        assert states(r, "report") == [(7, "active"), (9.5, "violated")]
        assert norm(r, "report")["activated_at"] == 7
        assert [(v["norm"], v["reparation_activated"], v["transition"]) for v in r["violations"]] == [
            ("ack", True, None), ("report", False, "Failed")]

    def test_reparation_met(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]}, {"t": 7},
            {"t": 9, "messages": [msg("R[1]", "D", "report")]}, {"t": 20},
        ])
        assert norm(r, "report")["state"] == "fulfilled"
        assert r["violated"] == ["ack"]

    def test_no_transition_requested_in_a_terminal_state(self):
        r = run(self.NORMS, [
            {"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]}, {"t": 7},
            {"t": 8, "state": "Done"}, {"t": 12},
        ])
        # the instance ended at t=8 with `report` within its deadline
        assert states(r, "report") == [(7, "active"), (8, "discharged")]
        assert r["instance"]["end"] == 8 and r["instance"]["state"] == "Done"
        assert any("after the end" in w for w in r["warnings"])

    def test_state_is_read_before_the_transitions_of_the_observation(self):
        r = run([LIFECYCLE, """
        norms:
          - {id: late, kind: obligation, bearer: "R[i]", trigger: "state == Open AND go == true", content: "state == Open", deadline: 5s}
        """], [{"t": 0, "values": {"go": False}}, {"t": 1, "values": {"go": True}, "state": "Done"}])
        # at t=1 the predicates still see Open; the instance then ends
        assert states(r, "late") == [(1, "active"), (1, "fulfilled")]


# === End of the contract instance (item 6) ===

class TestSettlement:
    NORMS = [LIFECYCLE, """
    norms:
      - {id: no_deadline, kind: obligation, bearer: "R[i]", content: "R[i] -> D : a", on_violation: {reparation: rep, transition: Failed}}
      - {id: in_time, kind: obligation, bearer: "R[i]", content: "R[i] -> D : b", deadline: 100s}
      - {id: never, kind: prohibition, bearer: "R[i]", content: "R[i] -> D : c"}
      - {id: may, kind: permission, bearer: "R[i]", content: "R[i] -> D : d"}
      - {id: untriggered, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : go", content: "R[i] -> D : e"}
      - {id: rep, kind: obligation, bearer: "R[i]", content: "R[i] -> D : f", deadline: 1s}
    """]

    def check(self, r, t):
        got = {n["id"]: (n["state"], n["verdict"], n["time"]) for n in r["norms"]}
        assert got == {
            "no_deadline": ("violated", "violated", t),
            "in_time": ("discharged", "fulfilled", t),
            "never": ("fulfilled", "fulfilled", t),
            "may": ("discharged", "fulfilled", t),
            "untriggered": ("discharged", "fulfilled", t),
            "rep": ("discharged", "fulfilled", t),
        }
        # reported, but nothing is activated or requested after the end
        assert r["violations"] == [{
            "t": t, "norm": "no_deadline", "severity": "Major", "verdict": "violated",
            "sanction": None, "reparation": "rep", "reparation_activated": False,
            "transition": None}]

    def test_terminal_state(self):
        r = run(self.NORMS, [{"t": 0}, {"t": 5, "state": "Done"}])
        self.check(r, 5)
        assert r["instance"]["end_reason"] == "terminal state Done"

    def test_end_marker(self):
        r = run(self.NORMS, [{"t": 0}, {"t": 5, "end": True}, {"t": 6}])
        self.check(r, 5)

    def test_last_observation(self):
        self.check(run(self.NORMS, [{"t": 0}, {"t": 5}]), 5)

    def test_prefix_is_not_settled(self):
        r = run(self.NORMS, [{"t": 0}, {"t": 5, "end": False}])
        assert {n["verdict"] for n in r["norms"]} == {"pending"}
        assert r["instance"]["ended"] is False and r["instance"]["end"] is None


    def test_norm_activated_at_the_final_observation_is_discharged(self):
        # never judged over time, so neither violated nor fulfilled
        r = run([LIFECYCLE, """
        norms:
          - {id: first, kind: obligation, bearer: "R[i]", content: "R[i] -> D : a", deadline: 2s, on_violation: {reparation: rep}}
          - {id: rep, kind: obligation, bearer: "R[i]", content: "R[i] -> D : b"}
          - {id: late, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : go", content: "R[i] -> D : c"}
          - {id: quiet, kind: prohibition, bearer: "R[i]", trigger: "D -> R[i] : go", content: "R[i] -> D : d"}
        """], [{"t": 0}, {"t": 5, "messages": [msg("D", "R[1]", "go")], "state": "Done"}])
        assert states(r, "first") == [(0, "active"), (5, "violated")]
        for name in ("rep", "late", "quiet"):
            assert states(r, name) == [(5, "active"), (5, "discharged")]
            assert norm(r, name)["verdict"] == "fulfilled"
        assert [v["norm"] for v in r["violations"]] == ["first"]
        assert r["violations"][0]["reparation_activated"] is True


# === Verdicts: assumptions, guarantees, excused ===

class TestVerdicts:
    def test_excused_after_an_assumption_failed(self):
        blocks = """
        assume:
          - "D.up == true"
        guarantee:
          - "R[i].lost == false"
        norms:
          - {id: ack, kind: obligation, bearer: "R[i]", content: "R[i] -> D : ack", deadline: 5s, on_violation: {reparation: rep}}
          - {id: rep, kind: obligation, bearer: "R[i]", content: "R[i] -> D : rep", deadline: 1s}
        """
        r = run(blocks, [
            {"t": 0, "values": {"D.up": True, "R[1].lost": False}},
            {"t": 6, "values": {"D.up": False, "R[1].lost": True}},    # same observation
            {"t": 9},
        ])
        assert r["assumptions"][0]["status"] == "failed" and r["assumptions"][0]["time"] == 6
        assert norm(r, "ack")["verdict"] == "excused"
        assert r["guarantees"][0]["verdict"] == "excused" and r["guarantees"][0]["time"] == 6
        # an excused norm still triggers its on_violation
        assert r["violations"][0]["verdict"] == "excused"
        assert r["violations"][0]["reparation_activated"] is True
        assert norm(r, "rep")["verdict"] == "excused"
        assert r["ok"] and r["violated"] == []

    def test_violated_before_the_assumption_fails(self):
        blocks = """
        assume:
          - "D.up == true"
        norms:
          - {id: ack, kind: obligation, bearer: "R[i]", content: "R[i] -> D : ack", deadline: 5s}
        """
        r = run(blocks, [{"t": 0, "values": {"D.up": True}}, {"t": 6},
                         {"t": 7, "values": {"D.up": False}}, {"t": 9}])
        assert norm(r, "ack")["verdict"] == "violated"
        assert r["assumptions"][0]["status"] == "failed"
        assert not r["ok"]

    def test_state_predicates_are_judged_per_observation(self):
        blocks = """
        assume:
          - "D.up == true"
        guarantee:
          - "R[i].speed <= 5"
          - "R[i].load < 10"
        """
        r = run(blocks, [
            {"t": 0, "values": {"D.up": True, "R[1].speed": 1, "R[1].load": 1}},
            {"t": 1, "values": {"R[1].speed": 6}},
            {"t": 2, "values": {"R[1].speed": 1}},
        ])
        g1, g2 = r["guarantees"]
        assert (g1["verdict"], g1["time"], g1["temporal"]) == ("violated", 1, False)
        assert (g2["verdict"], g2["time"]) == ("fulfilled", 2)
        assert r["assumptions"][0]["status"] == "holds"
        assert r["violated"] == ["guarantee 1"] and not r["ok"]

    def test_state_predicates_on_a_prefix_stay_pending(self):
        r = run('guarantee:\n  - "R[i].speed <= 5"\n',
                [{"t": 0, "values": {"R[1].speed": 1}}, {"t": 1, "end": False}])
        assert r["guarantees"][0]["verdict"] == "pending"

    def test_undefined_values_do_not_decide(self):
        r = run("""
        assume:
          - "D.up == true"
        guarantee:
          - "R[i].speed <= 5"
        norms:
          - {id: never, kind: prohibition, bearer: "R[i]", content: "NOT (R[i].ok == true)"}
        """, [{"t": 0}, {"t": 1}])
        assert r["assumptions"][0]["status"] == "pending"
        assert r["guarantees"][0]["verdict"] == "pending"
        assert norm(r, "never")["state"] == "fulfilled"
        assert len([w for w in r["warnings"] if "no value for" in w]) == 3


# === Temporal operators (F.4) ===

class TestTemporal:
    def guarantee(self, text, observations):
        r = run(f'guarantee:\n  - "{text}"\n', observations)
        g = r["guarantees"][0]
        assert g["temporal"] is True
        return g["verdict"], g["time"]

    def test_within(self):
        g = "within(10s, R[i].done == true)"
        on = {"R[1].done": True}
        off = {"R[1].done": False}
        assert self.guarantee(g, [{"t": 0, "values": off}, {"t": 4, "values": on}, {"t": 5}]) == ("fulfilled", 4)
        # the end of the interval is inside it
        assert self.guarantee(g, [{"t": 0, "values": off}, {"t": 10, "values": on}]) == ("fulfilled", 10)
        # decided false only by an observation after the interval
        assert self.guarantee(g, [{"t": 0, "values": off}, {"t": 10}]) == ("pending", None)
        assert self.guarantee(g, [{"t": 0, "values": off}, {"t": 10.5, "values": on}]) == ("violated", 10.5)

    def test_within_is_measured_from_the_start_of_the_instance(self):
        g = "within(10s, R[i].done == true)"
        assert self.guarantee(g, [{"t": 100, "values": {"R[1].done": False}},
                                  {"t": 109, "values": {"R[1].done": True}}]) == ("fulfilled", 109)

    def test_throughout(self):
        g = "throughout(10s, R[i].ok == true)"
        ok = {"R[1].ok": True}
        bad = {"R[1].ok": False}
        assert self.guarantee(g, [{"t": 0, "values": ok}, {"t": 4, "values": bad}, {"t": 5}]) == ("violated", 4)
        assert self.guarantee(g, [{"t": 0, "values": ok}, {"t": 10}]) == ("pending", None)
        assert self.guarantee(g, [{"t": 0, "values": ok}, {"t": 11, "values": bad}]) == ("fulfilled", 11)

    def test_until(self):
        g = "until(R[i].ok == true, R[i].done == true, 10s)"
        v = lambda ok, done: {"R[1].ok": ok, "R[1].done": done}
        assert self.guarantee(g, [{"t": 0, "values": v(True, False)}, {"t": 3, "values": v(False, True)},
                                  {"t": 4}]) == ("fulfilled", 3)
        # p fails before q
        assert self.guarantee(g, [{"t": 0, "values": v(True, False)}, {"t": 2, "values": v(False, False)},
                                  {"t": 3, "values": v(True, True)}, {"t": 4}]) == ("violated", 2)
        # q does not occur in the interval
        assert self.guarantee(g, [{"t": 0, "values": v(True, False)}, {"t": 9}]) == ("pending", None)
        assert self.guarantee(g, [{"t": 0, "values": v(True, False)},
                                  {"t": 11, "values": v(True, True)}]) == ("violated", 11)

    def test_nested(self):
        g = "within(10s, throughout(2s, R[i].ok == true))"
        ok = {"R[1].ok": True}
        bad = {"R[1].ok": False}
        assert self.guarantee(g, [{"t": 0, "values": bad}, {"t": 1, "values": ok}, {"t": 2},
                                  {"t": 3}, {"t": 3.5}]) == ("fulfilled", 3.5)

    def test_combined_with_state_predicate_is_evaluated_at_the_start(self):
        g = "R[i].ready == true AND within(5s, R[i].done == true)"
        assert self.guarantee(g, [{"t": 0, "values": {"R[1].ready": False, "R[1].done": True}},
                                  {"t": 1}]) == ("violated", 0)

    def test_temporal_assumption_fails_when_decided(self):
        r = run("""
        assume:
          - "throughout(10s, D.up == true)"
        guarantee:
          - "within(5s, R[i].done == true)"
        """, [{"t": 0, "values": {"D.up": True, "R[1].done": False}},
              {"t": 3, "values": {"D.up": False}}, {"t": 6}])
        assert (r["assumptions"][0]["status"], r["assumptions"][0]["time"]) == ("failed", 3)
        assert (r["guarantees"][0]["verdict"], r["guarantees"][0]["time"]) == ("excused", 6)

    OBLIGATION = """
    norms:
      - {id: hold, kind: obligation, bearer: "R[i]", content: "throughout(3s, R[i].ok == true)", deadline: 5s}
    """

    def test_temporal_content_is_pending_until_decided(self):
        ok = {"R[1].ok": True}
        bad = {"R[1].ok": False}
        # holds at the observation t=4 (within the deadline), decided at t=8
        r = run(self.OBLIGATION, [{"t": 0, "values": bad}, {"t": 4, "values": ok}, {"t": 6},
                                  {"t": 8}, {"t": 9}])
        assert states(r, "hold") == [(0, "active"), (8, "fulfilled")]
        assert "at t=4" in norm(r, "hold")["reason"]
        # only observations up to the deadline count
        r = run(self.OBLIGATION, [{"t": 0, "values": bad}, {"t": 6, "values": ok}, {"t": 12}])
        assert states(r, "hold") == [(0, "active"), (6, "violated")]
        # undecided at the end of the instance: stays pending
        r = run(self.OBLIGATION, [{"t": 0, "values": bad}, {"t": 4, "values": ok}, {"t": 6}])
        assert (norm(r, "hold")["state"], norm(r, "hold")["verdict"]) == ("active", "pending")
        assert "not decided" in norm(r, "hold")["reason"]


# === Predicates (A.10, E.3, E.4) ===

class TestPredicates:
    def holds(self, text, values, **first):
        r = run(f"""
        norms:
          - {{id: p, kind: prohibition, bearer: "R[i]", content: '{text}'}}
        """, [{"t": 2.0, "values": values, **first}, {"t": 3.0, "end": False}])
        return norm(r, "p")["state"] == "violated", r

    @pytest.mark.parametrize("text, values, expected", [
        ("R[i].battery < 10 AND R[i].status == InTransit", {"R[1].battery": 5, "R[1].status": "InTransit"}, True),
        ("R[i].battery < 10 AND R[i].status == InTransit", {"R[1].battery": 5, "R[1].status": "Idle"}, False),
        ("R[i].battery < 10 OR R[i].status != Idle", {"R[1].battery": 50, "R[1].status": "Idle"}, False),
        ("NOT (a > 1) OR b == 3", {"a": 2, "b": 3}, True),
        ("(a + b) * 2 - a / 2 >= 9", {"a": 2, "b": 3}, True),
        ("-a + 1 < 0", {"a": 2}, True),
        ("elapsed > 1min", {"elapsed": 61}, True),
        ("elapsed > 1500ms", {"elapsed": 1.4}, False),
        ("R[i].status IN [Idle, Charging]", {"R[1].status": "Charging"}, True),
        ("R[i].status IN [Idle, Charging]", {"R[1].status": "Moving"}, False),
        ("a IN [1, 2, 3]", {"a": 2}, True),
        ("R[2].x == 1", {"R[2].x": 1}, True),
        ("flag", {"flag": True}, True),
        ("now >= 2", {}, True),
        ("now > 3", {}, False),
        ('name == "r one"', {"name": "r one"}, True),
        ("a == true", {"a": 1}, False),
    ])
    def test_values(self, text, values, expected):
        assert self.holds(text, values)[0] is expected

    def test_valueless_name_compared_with_a_boolean_is_undefined(self):
        # `ok` is never reported: the assumption is undefined, not failed,
        # so it excuses nothing
        r = run("""
        assume:
          - "ok == true"
        norms:
          - {id: ack, kind: obligation, bearer: "R[i]", content: "R[i] -> D : ack", deadline: 2s}
        """, [{"t": 0}, {"t": 1, "messages": [msg("D", "R[1]", "go")]}, {"t": 5, "end": False}])
        a = r["assumptions"][0]
        assert (a["status"], a["time"]) == ("pending", None)
        assert norm(r, "ack")["verdict"] == "violated" and not r["ok"]
        assert [w for w in r["warnings"] if "'ok'" in w] == [
            "the trace has no value for 'ok'; predicates that use it do not hold"]

    def test_enum_literal_only_against_a_string(self):
        blocks = """
        guarantee:
          - "within(10s, R[i].status == Delivered)"
        """
        verdict = lambda *obs: run(blocks, list(obs))["guarantees"][0]["verdict"]
        assert verdict({"t": 0, "values": {"R[1].status": "Delivered"}}, {"t": 20}) == "fulfilled"
        assert verdict({"t": 0, "values": {"R[1].status": "Idle"}}, {"t": 20}) == "violated"
        # not yet valued: undefined, so not decided false
        assert verdict({"t": 0}, {"t": 20}) == "pending"
        assert verdict({"t": 0}, {"t": 5, "values": {"R[1].status": "Delivered"}}, {"t": 20}) == "fulfilled"

    @pytest.mark.parametrize("text", [
        "flag", "NOT flag", "flag == true", "flag != 3", "flag == Other", "flag > 1",
        "flag + 1 > 0", "n IN [flag, Other]", "flag IN [1, 2]",
    ])
    def test_valueless_name_elsewhere_is_undefined(self, text):
        r = run(f"assume:\n  - '{text}'\nguarantee:\n  - '{text}'\n",
                [{"t": 0, "values": {"n": 7}}, {"t": 1}])
        assert r["assumptions"][0]["status"] == "pending"
        assert r["guarantees"][0]["verdict"] == "pending"
        assert any("'flag'" in w for w in r["warnings"])

    def test_number_in_a_list_with_a_valueless_name(self):
        assert self.holds("n IN [flag, 7]", {"n": 7})[0] is True

    def test_request(self):
        assert self.holds("request.deadline - now < 10s", {}, request={"deadline": 11})[0] is True
        got, r = self.holds("request.other > 1", {}, request={"deadline": 11})
        assert got is False and any("request.other" in w for w in r["warnings"])

    def test_bound_index(self):
        assert self.holds("R[i].x == i", {"R[3].x": 3}, bindings={"i": 3})[0] is True

    def test_undefined_names_are_reported_once(self):
        got, r = self.holds("R[i].missing > 1 OR ghost > 2", {})
        assert got is False
        assert len(r["warnings"]) == 2
        assert "R[1].missing" in r["warnings"][0] and "ghost" in r["warnings"][1]

    def test_value_that_arrives_later_is_no_warning(self):
        r = run("""
        norms:
          - {id: p, kind: prohibition, bearer: "R[i]", content: "R[i].x > 1"}
        """, [{"t": 0}, {"t": 1, "values": {"R[1].x": 5}}, {"t": 2}])
        assert states(r, "p")[-1] == (1, "violated") and r["warnings"] == []

    def test_function_call_is_not_evaluable(self):
        r = run("""
        guarantee:
          - "distance(R[i], D) < 5"
        norms:
          - {id: p, kind: obligation, bearer: "R[i]", content: "max(a, b) > 1", deadline: 1s}
          - {id: q, kind: prohibition, bearer: "R[i]", content: "a > 1"}
        """, [{"t": 0, "values": {"a": 0, "b": 0}}, {"t": 5}])
        p = norm(r, "p")
        assert (p["state"], p["verdict"], p["evaluable"]) == ("inactive", "pending", False)
        assert "max()" in p["reason"]
        assert r["guarantees"][0]["verdict"] == "pending"
        assert "distance()" in r["guarantees"][0]["reason"]
        assert norm(r, "q")["state"] == "fulfilled"
        assert r["ok"]

    def test_state(self):
        r = run([LIFECYCLE, """
        norms:
          - {id: p, kind: prohibition, bearer: "R[i]", content: "state == Done"}
        """], [{"t": 0}, {"t": 1, "state": "Done"}])
        assert norm(r, "p")["state"] == "fulfilled"


# === Traces and usage errors ===

class TestTrace:
    @pytest.mark.parametrize("lines, message", [
        ([], "no observation"),
        (["not json"], "not JSON"),
        (["[1]"], "JSON object"),
        (['{"values": {}}'], "'t' must be a number"),
        (['{"t": 2}', '{"t": 1}'], "before the previous"),
        (['{"t": 1, "valeus": {}}'], "unknown key"),
        (['{"t": 1}', '{"t": 2, "bindings": {"i": 1}}'], "only on the first line"),
        (['{"t": 1, "messages": [{"from": "A", "name": "m"}]}'], "'to'"),
        (['{"t": 1, "end": "yes"}'], "'end'"),
    ])
    def test_malformed(self, lines, message):
        with pytest.raises(TraceError, match=message):
            parse_trace(lines)

    def test_unknown_lifecycle_state(self):
        with pytest.raises(TraceError, match="not a state"):
            run(LIFECYCLE, [{"t": 0, "state": "Nowhere"}])

    def test_values_persist_and_blank_lines_are_skipped(self):
        trace = parse_trace(['{"t": 0, "values": {"R[ 1 ].x": 1}}', "", '{"t": 1, "values": {"y": 2}}'])
        assert trace.observations[1].values == {"R[1].x": 1, "y": 2}

    def test_file_must_declare_norms(self):
        with pytest.raises(NormsEvalError, match="norms: 0.1"):
            run("", [{"t": 0}], declare=False)

    def test_unbound_index_matches_any_and_warns(self):
        r = run("""
        norms:
          - {id: ack, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : go", content: "R[i] -> D : ack"}
        """, [{"t": 0, "bindings": {}}, {"t": 1, "messages": [msg("D", "R[3]", "go")]},
              {"t": 2, "end": False}])
        assert norm(r, "ack")["state"] == "active"
        assert any("not bound" in w for w in r["warnings"])


# === Amendments (F.6) ===

def effects(result: dict, index: int = 0) -> dict:
    return {e["norm"]: (e["effect"], e["from_state"], e["to_state"])
            for e in result["amendments"][index]["effects"]}


class TestAmendment:
    V1 = """
    norms:
      - id: ack
        kind: obligation
        bearer: "R[i]"
        trigger: "D -> R[i] : go"
        content: "R[i] -> D : ack"
        deadline: 10s
        on_violation: {reparation: rep}
      - {id: rep, kind: obligation, bearer: "R[i]", content: "R[i] -> D : rep", deadline: 2s}
      - {id: quiet, kind: prohibition, bearer: "R[i]", content: "R[i].speed > 5"}
      - {id: may, kind: permission, bearer: "R[i]", content: "R[i] -> D : rest", on_amendment: discharge}
      - {id: old_duty, kind: obligation, bearer: "R[i]", content: "R[i] -> D : old", deadline: 20s, on_amendment: {transform: new_duty}}
      - {id: gone, kind: obligation, bearer: "R[i]", content: "R[i] -> D : gone", deadline: 50s}
      - {id: turns, kind: obligation, bearer: "R[i]", content: "R[i] -> D : turns", deadline: 50s}
      - {id: later, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : later", content: "R[i] -> D : x", deadline: 5s}
      - {id: dropped, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : dropped", content: "R[i] -> D : y"}
      - {id: pause, kind: power, bearer: D, content: "D -> R[i] : pause", effects: {suspend: [ack]}}
    """
    V2 = """
    counts_as:
      - when: "D -> R[i] : wave"
        creates: greeting
    norms:
      - id: ack
        kind: obligation
        bearer: "R[i]"
        trigger: "D -> R[i] : go"
        content: "R[i] -> D : ack"
        deadline: 6s
        on_violation: {reparation: rep2}
      - {id: rep2, kind: obligation, bearer: "R[i]", content: "R[i] -> D : rep2", deadline: 2s}
      - {id: quiet, kind: prohibition, bearer: "R[i]", content: "R[i].speed > 3"}
      - {id: may, kind: permission, bearer: "R[i]", content: "R[i] -> D : rest"}
      - {id: new_duty, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : never", content: "R[i] -> D : new", deadline: 12s}
      - {id: turns, kind: prohibition, bearer: "R[i]", content: "R[i] -> D : turns"}
      - {id: later, kind: obligation, bearer: "R[i]", trigger: "D -> R[i] : sooner", content: "R[i] -> D : x", deadline: 5s}
      - {id: fresh, kind: permission, bearer: "R[i]", content: "R[i] -> D : fresh"}
      - {id: greet, kind: obligation, bearer: "R[i]", trigger: greeting, content: "R[i] -> D : hello", deadline: 3s}
      - {id: pause, kind: power, bearer: D, content: "D -> R[i] : pause", effects: {suspend: [ack]}}
    """

    def amend(self, *rest, before=(), at=4, v1=None, v2=None):
        trace = [{"t": 0, "values": {"R[1].speed": 1}},
                 {"t": 1, "messages": [msg("D", "R[1]", "go")]}, *before,
                 {"t": at, "amend": "v2"}, *rest]
        return run(v1 or self.V1, trace, versions={"v2": v2 or self.V2})

    def test_effects_on_every_kind_of_instance(self):
        r = self.amend({"t": 5, "end": False})
        assert effects(r) == {
            "ack": ("carry_over", "active", "active"),
            "rep": ("removed", "inactive", None),
            "quiet": ("carry_over", "active", "active"),
            "may": ("discharge", "active", "discharged"),
            "old_duty": ("transform", "active", "discharged"),
            "new_duty": ("activated", "inactive", "active"),
            "gone": ("carry_over", "active", "discharged"),       # no such id
            "turns": ("carry_over", "active", "discharged"),      # kind differs
            "later": ("new_definition", "inactive", "inactive"),
            "dropped": ("removed", "inactive", None),
            "pause": ("carry_over", "active", "active"),
            "fresh": ("added", None, "active"),                   # no trigger
            "rep2": ("added", None, "inactive"),                  # a reparation target
            "greet": ("added", None, "inactive"),                 # has a trigger
        }
        a = r["amendments"][0]
        assert (a["t"], a["file"]) == (4, "v2")
        assert set(a["effects"][0]) == {"norm", "effect", "from_state", "to_state", "reason"}
        assert "deadline changes from 10s to 6s" in a["effects"][0]["reason"]
        # the new version's norms in its order, then old final instances
        assert [n["id"] for n in r["norms"]] == [
            "ack", "rep2", "quiet", "may", "new_duty", "turns", "later", "fresh", "greet",
            "pause", "old_duty", "gone"]
        assert states(r, "may") == [(0, "active"), (4, "discharged")]
        assert norm(r, "may")["verdict"] == "fulfilled"
        assert states(r, "turns")[-1] == (4, "discharged") and norm(r, "turns")["kind"] == "obligation"
        assert [e["kind"] for e in r["timeline"] if e["t"] == 4].count("amend") == 15

    def test_carry_over_keeps_the_elapsed_time(self):
        # activated at t=1; the new deadline of 6s ends at t=7
        r = self.amend({"t": 7, "messages": [msg("R[1]", "D", "ack")]}, {"t": 9})
        assert states(r, "ack") == [(1, "active"), (7, "fulfilled")]
        r = self.amend({"t": 7}, {"t": 7.5, "messages": [msg("R[1]", "D", "ack")]},
                       {"t": 8, "messages": [msg("R[1]", "D", "rep2")]}, {"t": 20})
        assert states(r, "ack") == [(1, "active"), (7.5, "violated")]
        assert "passed at t=7 " in norm(r, "ack")["reason"]
        # on_violation of the new version
        assert states(r, "rep2") == [(7.5, "active"), (8, "fulfilled")]

    def test_carry_over_violated_when_the_new_deadline_is_already_exceeded(self):
        r = self.amend({"t": 9, "messages": [msg("R[1]", "D", "ack")]}, {"t": 12}, at=8)
        assert effects(r)["ack"] == ("carry_over", "active", "violated")
        assert states(r, "ack") == [(1, "active"), (8, "violated")]
        # an ordinary violation at that observation: step 5 applies
        v = r["violations"][0]
        assert (v["t"], v["norm"], v["reparation"], v["reparation_activated"]) == (8, "ack", "rep2", True)
        assert states(r, "rep2") == [(8, "active"), (12, "violated")]

    def test_carry_over_at_exactly_the_new_deadline_continues(self):
        r = self.amend({"t": 7, "messages": [msg("R[1]", "D", "ack")]}, {"t": 9}, at=7)
        assert states(r, "ack") == [(1, "active"), (7, "fulfilled")]

    def test_suspended_instance_carries_over_suspended(self):
        r = self.amend({"t": 30, "end": False},
                       before=[{"t": 2, "messages": [msg("D", "R[1]", "pause")]}])
        assert effects(r)["ack"] == ("carry_over", "suspended", "suspended")
        assert norm(r, "ack")["state"] == "suspended"

    def test_carried_over_content_is_the_new_one(self):
        r = self.amend({"t": 5, "values": {"R[1].speed": 4}}, {"t": 6})
        assert states(r, "quiet") == [(0, "active"), (5, "violated")]
        assert "speed > 3" in norm(r, "quiet")["reason"]

    def test_transform_counts_the_elapsed_time(self):
        # old_duty has been active since t=0: 4s of the 12s of new_duty are gone
        r = self.amend({"t": 12, "messages": [msg("R[1]", "D", "new")]}, {"t": 13})
        assert states(r, "old_duty") == [(0, "active"), (4, "discharged")]
        assert states(r, "new_duty") == [(4, "active"), (12, "fulfilled")]
        r = self.amend({"t": 12.5, "messages": [msg("R[1]", "D", "new")]}, {"t": 13})
        assert states(r, "new_duty") == [(4, "active"), (12.5, "violated")]
        assert "passed at t=12 " in norm(r, "new_duty")["reason"]

    def test_transform_to_a_norm_that_is_not_inactive_or_missing(self):
        v1 = """
        norms:
          - {id: a, kind: obligation, bearer: "R[i]", content: "R[i] -> D : a", on_amendment: {transform: b}}
          - {id: b, kind: obligation, bearer: "R[i]", content: "R[i] -> D : b", deadline: 9s}
          - {id: c, kind: obligation, bearer: "R[i]", content: "R[i] -> D : c", on_amendment: {transform: nowhere}}
        """
        v2 = """
        norms:
          - {id: b, kind: obligation, bearer: "R[i]", content: "R[i] -> D : b", deadline: 9s}
        """
        r = self.amend({"t": 5, "end": False}, v1=v1, v2=v2)
        assert effects(r) == {
            "a": ("transform", "active", "discharged"),
            "b": ("carry_over", "active", "active"),
            "c": ("transform", "active", "discharged"),
        }
        reasons = {e["norm"]: e["reason"] for e in r["amendments"][0]["effects"]}
        assert "'b' is active; nothing more" in reasons["a"]
        assert "no norm 'nowhere'" in reasons["c"]
        assert states(r, "b") == [(0, "active")]

    def test_inactive_instance_takes_the_new_definition(self):
        r = self.amend({"t": 5, "messages": [msg("D", "R[1]", "later")]},
                       {"t": 6, "messages": [msg("D", "R[1]", "sooner")]}, {"t": 7, "end": False})
        assert states(r, "later") == [(6, "active")]
        assert "dropped" not in [n["id"] for n in r["norms"]]

    def test_final_instances_are_not_affected(self):
        r = self.amend({"t": 5, "end": False},
                       before=[{"t": 2, "messages": [msg("R[1]", "D", "ack")]}])
        assert "ack" not in effects(r)
        assert states(r, "ack") == [(1, "active"), (2, "fulfilled")]

    def test_new_counts_as_rules_apply_from_the_amendment(self):
        r = self.amend({"t": 5, "messages": [msg("D", "R[1]", "wave")]}, {"t": 6, "end": False},
                       before=[{"t": 2, "messages": [msg("D", "R[1]", "wave")]}])
        assert [(e["t"], e["name"]) for e in r["institutional_events"]] == [(5, "greeting")]
        assert norm(r, "greet")["activated_at"] == 5

    def test_assumptions_and_guarantees_of_the_new_version(self):
        v1 = """
        guarantee:
          - "within(100s, R[i].done == true)"
          - "throughout(100s, R[i].ok == true)"
          - "R[i].speed <= 5"
        """
        v2 = """
        guarantee:
          - "within(100s, R[i].done == true)"
          - "within(10s, R[i].charged == true)"
        """
        r = run(v1, [{"t": 0, "values": {"R[1].done": False, "R[1].ok": True, "R[1].speed": 9,
                                         "R[1].charged": False}},
                     {"t": 50, "amend": "v2"},
                     {"t": 60, "values": {"R[1].charged": True, "R[1].ok": False}},
                     {"t": 70, "values": {"R[1].done": True}}], versions={"v2": v2})
        got = [(g["index"], g["text"].split("(")[0], g["verdict"], g["time"]) for g in r["guarantees"]]
        assert got == [
            (1, "within", "fulfilled", 70),         # same text: continues from the start
            (2, "within", "fulfilled", 60),         # new: evaluated from the amendment (t=50)
            (3, "throughout", "pending", None),     # removed before it was decided
            (4, "R[i].speed <= 5", "violated", 0),  # decided before the amendment: kept
        ]
        assert "removed by the amendment" in r["guarantees"][2]["reason"]
        assert "amendment at t=50" in r["guarantees"][1]["reason"]

    def test_lifecycle_of_the_new_version(self):
        v1 = [LIFECYCLE, 'norms:\n  - {id: q, kind: prohibition, bearer: "R[i]", content: "bad == true"}\n']
        v2 = ["""
        lifecycle:
          states: [Open, Working, Done]
          initial: Open
          terminal: [Done]
          transitions:
            - { id: finish, from: Open, to: Done, on: "done == true" }
        """, v1[1]]
        r = run(v1, [{"t": 0}, {"t": 1, "amend": "v2"}, {"t": 2, "state": "Working"},
                     {"t": 3, "state": "Done"}], versions={"v2": v2})
        assert r["instance"]["state"] == "Done" and r["instance"]["end"] == 3
        # a state that only the old version has
        with pytest.raises(TraceError, match="Failed"):
            run(v1, [{"t": 0}, {"t": 1, "amend": "v2"}, {"t": 2, "state": "Failed"}],
                versions={"v2": v2})
        with pytest.raises(TraceError, match="Working"):
            run(v1, [{"t": 0}, {"t": 1, "state": "Working"}], versions={"v2": v2})

    def test_current_state_must_exist_in_the_new_version(self):
        v1 = ["""
        lifecycle:
          states: [Open, Odd, Done]
          initial: Open
          terminal: [Done]
          transitions:
            - { id: finish, from: Open, to: Done, on: "done == true" }
        """]
        with pytest.raises(NormsEvalError, match="'Odd'"):
            run(v1, [{"t": 0, "state": "Odd"}, {"t": 1, "amend": "v2"}], versions={"v2": [LIFECYCLE]})

    def test_new_version_must_have_the_contract_and_declare_norms(self):
        sos = make_sos(self.V1)
        trace = lambda: parse_trace([json.dumps({"t": 0}), json.dumps({"t": 1, "amend": "v2"})])
        other = parse(textwrap.dedent("""
            sos:
              name: T
              extensions:
                - norms: 0.1
              actors:
                - {id: D, role: d}
              contracts:
                - id: OTHER
                  parties: [D]
        """))
        with pytest.raises(NormsEvalError, match="no contract 'C'"):
            evaluate(sos, trace(), versions={"v2": other})
        with pytest.raises(NormsEvalError, match="norms: 0.1"):
            evaluate(sos, trace(), versions={"v2": make_sos(self.V2, declare=False)})
        with pytest.raises(NormsEvalError, match="File not found"):
            evaluate(sos, trace())

    def test_two_amendments(self):
        r = run(self.V1, [{"t": 0}, {"t": 1, "amend": "v2"}, {"t": 2, "amend": "v1"},
                          {"t": 3, "end": False}], versions={"v2": self.V2, "v1": self.V1})
        assert [a["file"] for a in r["amendments"]] == ["v2", "v1"]
        # discharged by the first amendment: final, so the id is not reused
        assert states(r, "may") == [(0, "active"), (1, "discharged")]
        assert effects(r, 1)["quiet"] == ("carry_over", "active", "active")
        assert effects(r, 1)["rep"] == ("added", None, "inactive")

    def test_amend_must_be_a_path(self):
        with pytest.raises(TraceError, match="'amend'"):
            parse_trace(['{"t": 0, "amend": 2}'])


# === cadl norms-diff ===

def diff(old, new, contract_id=None) -> dict:
    from cadl.norms_diff import diff_versions
    return diff_versions(make_sos(old), make_sos(new, check=False), contract_id)


class TestNormsDiff:
    def kinds(self, result):
        return sorted((f["level"], f["kind"], f.get("norm") or f.get("event"))
                      for f in result["contracts"][0]["findings"])

    def test_findings(self):
        r = diff(TestAmendment.V1, TestAmendment.V2)
        assert r["ok"] and r["errors"] == []
        assert self.kinds(r) == sorted([
            ("warning", "carry_over_changed", "ack"),      # deadline 10s -> 6s
            ("info", "norm_changed", "ack"),               # on_violation
            ("warning", "norm_removed", "rep"),
            ("warning", "carry_over_changed", "quiet"),    # content
            ("info", "norm_changed", "may"),               # on_amendment
            ("info", "discharge", "may"),                  # on_amendment: discharge
            ("info", "transform", "old_duty"),
            ("info", "norm_removed", "old_duty"),
            ("warning", "norm_removed", "gone"),
            ("warning", "kind_changed", "turns"),
            ("info", "norm_changed", "turns"),             # content, deadline (not carried over)
            ("info", "norm_changed", "later"),             # trigger
            ("warning", "norm_removed", "dropped"),
            ("info", "norm_added", "rep2"),
            ("info", "norm_added", "new_duty"),
            ("info", "norm_added", "fresh"),
            ("info", "norm_added", "greet"),
            ("info", "counts_as_added", "greeting"),
        ])
        by = {(f["kind"], f.get("norm")): f for f in r["contracts"][0]["findings"]}
        assert by[("carry_over_changed", "ack")]["changes"] == {"deadline": {
            "old": "10s", "new": "6s", "old_s": "10", "new_s": "6"}}
        assert "violated when the new version takes effect" in by[("carry_over_changed", "ack")]["text"]
        assert by[("norm_added", "fresh")]["active_at_once"] is True
        assert by[("norm_added", "rep2")]["active_at_once"] is False

    def test_missing_transform_target_is_an_error(self):
        v1 = """
        norms:
          - {id: a, kind: obligation, bearer: "R[i]", content: "R[i] -> D : a", on_amendment: {transform: b}}
        """
        v2 = """
        norms:
          - {id: a, kind: obligation, bearer: "R[i]", content: "R[i] -> D : a"}
        """
        r = diff(v1, v2)
        assert not r["ok"] and len(r["errors"]) == 1
        assert ("error", "transform_target_missing", "a") in self.kinds(r)

    def test_counts_as_and_lifecycle(self):
        v1 = [LIFECYCLE, """
        counts_as:
          - {when: "D -> R[i] : a", creates: one}
          - {when: "D -> R[i] : b", creates: two}
        """]
        v2 = ["""
        lifecycle:
          states: [Open, Done]
          initial: Open
          terminal: [Done]
          transitions:
            - { id: finish, from: Open, to: Done, on: "done == true" }
        counts_as:
          - {when: "D -> R[i] : a", creates: one, if: "x > 1"}
          - {when: "D -> R[i] : c", creates: three}
        """]
        assert self.kinds(diff(v1, v2)) == [
            ("info", "counts_as_added", "three"), ("info", "counts_as_changed", "one"),
            ("info", "counts_as_removed", "two"), ("warning", "lifecycle_states_removed", None)]

    def test_identical_versions_and_contract_option(self):
        r = diff(TestAmendment.V2, TestAmendment.V2)
        assert r == {"contracts": [{"id": "C", "findings": []}], "errors": [], "ok": True}
        assert diff(TestAmendment.V1, TestAmendment.V2, "C")["contracts"][0]["id"] == "C"
        with pytest.raises(NormsEvalError, match="NOPE"):
            diff(TestAmendment.V1, TestAmendment.V2, "NOPE")


# === CLI on the example traces ===

def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "cadl.cli", "norms-eval", *args],
                          capture_output=True, text=True)


def cli_diff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "cadl.cli", "norms-diff", *args],
                          capture_output=True, text=True)


def cli_json(name: str) -> tuple:
    proc = cli(str(EXAMPLE), "--trace", str(TRACES / f"norms_robot_delivery_{name}.jsonl"),
               "--format", "json")
    result = json.loads(proc.stdout)
    return proc.returncode, result, {n["id"]: (n["state"], n["verdict"]) for n in result["norms"]}


class TestCLI:
    def test_a_all_fulfilled(self):
        code, r, norms = cli_json("a_fulfilled")
        assert code == 0 and r["ok"]
        assert norms == {
            "acknowledge": ("fulfilled", "fulfilled"),
            "report_failure": ("discharged", "fulfilled"),
            "no_low_battery_driving": ("fulfilled", "fulfilled"),
            "may_return_to_charge": ("discharged", "fulfilled"),
            "reassign": ("discharged", "fulfilled"),
        }
        assert r["violations"] == [] and r["unauthorised"] == []
        assert r["guarantees"][0]["verdict"] == "fulfilled"
        assert r["assumptions"][0]["status"] == "pending"
        assert r["instance"]["state"] == "Completed" and r["instance"]["end"] == 120
        assert r["contract"] == "DELIVERY_SLA" and r["instance"]["bindings"] == {"i": 2}

    def test_b_contrary_to_duty(self):
        code, r, norms = cli_json("b_reparation")
        assert code == 1 and r["violated"] == ["acknowledge"]
        assert norms["acknowledge"] == ("violated", "violated")
        assert norms["report_failure"] == ("fulfilled", "fulfilled")
        assert [(v["norm"], v["severity"], v["reparation_activated"], v["transition"])
                for v in r["violations"]] == [("acknowledge", "Major", True, None)]

    def test_c_both_missed_requests_transition(self):
        code, r, norms = cli_json("c_violated")
        assert code == 1
        assert norms["acknowledge"] == norms["report_failure"] == ("violated", "violated")
        assert [(v["t"], v["norm"], v["severity"], v["transition"]) for v in r["violations"]] == [
            (6.5, "acknowledge", "Major", None), (9, "report_failure", "Critical", "Violated")]
        assert r["guarantees"][0]["verdict"] == "pending"

    def test_d_guarantee_excused(self):
        code, r, norms = cli_json("d_excused")
        assert code == 0 and r["ok"]
        assert (r["assumptions"][0]["status"], r["assumptions"][0]["time"]) == ("failed", 150)
        assert (r["guarantees"][0]["verdict"], r["guarantees"][0]["time"]) == ("excused", 320)
        assert norms["acknowledge"] == ("fulfilled", "fulfilled")

    def test_e_reassignment_discharges(self):
        code, r, norms = cli_json("e_reassignment")
        assert code == 0
        assert norms["acknowledge"] == ("discharged", "fulfilled")
        assert norms["report_failure"] == ("discharged", "fulfilled")
        assert [(e["t"], e["name"], e["performer"]) for e in r["institutional_events"]] == [
            (3, "reassignment", "DISPATCHER")]
        assert r["unauthorised"] == []
        # a later route assignment creates no new obligation
        assert [e["t"] for e in r["timeline"] if e["kind"] == "ignored"] == [4]
        assert next(n for n in r["norms"] if n["id"] == "acknowledge")["time"] == 3

    def test_text_output(self):
        proc = cli(str(EXAMPLE), "--trace", str(TRACES / "norms_robot_delivery_b_reparation.jsonl"))
        assert proc.returncode == 1
        for part in ("Contract DELIVERY_SLA (norms 0.1)", "Timeline", "Norms", "Violations",
                     "Assumptions", "Guarantees", "reparation report_failure activated",
                     "Result: VIOLATED — acknowledge"):
            assert part in proc.stdout

    def test_contract_option(self):
        trace = str(TRACES / "norms_robot_delivery_a_fulfilled.jsonl")
        assert cli(str(EXAMPLE), "--trace", trace, "--contract", "DELIVERY_SLA").returncode == 0
        proc = cli(str(EXAMPLE), "--trace", trace, "--contract", "NOPE")
        assert proc.returncode == 2 and "NOPE" in proc.stderr

    def test_amendment_example(self):
        proc = cli(str(V1), "--trace", str(TRACES / "norms_amendment_v1_to_v2.jsonl"),
                   "--format", "json")
        r = json.loads(proc.stdout)
        assert proc.returncode == 1 and r["violated"] == ["acknowledge", "no_low_battery_driving"]
        assert effects(r) == {
            "no_low_battery_driving": ("carry_over", "active", "active"),
            "may_return_to_charge": ("discharge", "active", "discharged"),
            "reassign": ("carry_over", "active", "active"),
            "report_failure": ("transform", "active", "discharged"),
            "request_help": ("activated", "inactive", "active"),
            "may_swap_battery": ("added", None, "active"),
        }
        got = {n["id"]: (n["state"], n["time"]) for n in r["norms"]}
        assert got == {
            "acknowledge": ("violated", 6.5),            # under version 1: not affected
            "request_help": ("fulfilled", 9),            # 1s + 1.5s of its 4s
            "no_low_battery_driving": ("violated", 30),  # battery 14 < 15, the new content
            "may_swap_battery": ("discharged", 110),
            "reassign": ("discharged", 110),
            "report_failure": ("discharged", 7.5),
            "may_return_to_charge": ("discharged", 7.5),
        }
        text = cli(str(V1), "--trace", str(TRACES / "norms_amendment_v1_to_v2.jsonl")).stdout
        assert "Amendments" in text and "transformed to request_help" in text

    def test_amendment_errors(self, tmp_path):
        bad = tmp_path / "bad.cadl"
        bad.write_text(V2.read_text().replace("kind: permission", "kind: wish"))
        plain = ROOT / "examples" / "sos_dsl_robot_delivery.cadl"
        other = tmp_path / "other.cadl"
        other.write_text(V2.read_text().replace("id: DELIVERY_SLA", "id: OTHER_SLA"))
        for target, message in ((bad, "type-check"), (tmp_path / "none.cadl", "File not found"),
                                (plain, "norms: 0.1"), (other, "no contract")):
            trace = tmp_path / "t.jsonl"
            trace.write_text('{"t": 0}\n' + json.dumps({"t": 1, "amend": str(target)}) + "\n")
            proc = cli(str(V1), "--trace", str(trace))
            assert proc.returncode == 2 and message in proc.stderr, proc.stderr

    def test_norms_diff(self, tmp_path):
        proc = cli_diff(str(V1), str(V2))
        assert proc.returncode == 0
        for part in ("Contract DELIVERY_SLA", "acknowledge: carries over, and deadline changes from '5s' to '3s'",
                     "no_low_battery_driving: carries over, and content changes",
                     "request_help (obligation, deadline 4s) is activated",
                     "may_swap_battery: added (permission); it has no trigger",
                     "Result: nothing prevents the amendment"):
            assert part in proc.stdout
        proc = cli_diff(str(V1), str(V2), "--format", "json", "--contract", "DELIVERY_SLA")
        r = json.loads(proc.stdout)
        assert r["ok"] and r["old"] == str(V1) and r["new"] == str(V2)
        assert [c["id"] for c in r["contracts"]] == ["DELIVERY_SLA"]
        # the transform target is missing in the new version: exit 1
        proc = cli_diff(str(V1), str(EXAMPLE))
        assert proc.returncode == 1 and "no norm 'request_help'" in proc.stdout
        # an invalid new version: exit 1
        bad = tmp_path / "bad.cadl"
        bad.write_text(V2.read_text().replace("kind: permission", "kind: wish"))
        proc = cli_diff(str(V1), str(bad))
        assert proc.returncode == 1 and "type-check" in proc.stdout
        # usage errors: exit 2
        assert cli_diff(str(bad), str(V2)).returncode == 2
        assert cli_diff(str(V1), str(tmp_path / "none.cadl")).returncode == 2
        assert cli_diff(str(V1), str(V2), "--contract", "NOPE").returncode == 2
        assert cli_diff(str(V1)).returncode == 2

    def test_usage_errors(self, tmp_path):
        trace = TRACES / "norms_robot_delivery_a_fulfilled.jsonl"
        # a file without norms: 0.1
        proc = cli(str(ROOT / "examples" / "sos_dsl_robot_delivery.cadl"), "--trace", str(trace))
        assert proc.returncode == 2 and "norms: 0.1" in proc.stderr
        # type-check errors
        bad = tmp_path / "bad.cadl"
        bad.write_text(EXAMPLE.read_text().replace("kind: permission", "kind: wish"))
        proc = cli(str(bad), "--trace", str(trace))
        assert proc.returncode == 2 and "type-check" in proc.stderr
        # a malformed trace, a missing trace, a missing --trace
        broken = tmp_path / "broken.jsonl"
        broken.write_text('{"t": 0}\n{"t": "later"}\n')
        proc = cli(str(EXAMPLE), "--trace", str(broken))
        assert proc.returncode == 2 and "line 2" in proc.stderr
        assert cli(str(EXAMPLE), "--trace", str(tmp_path / "none.jsonl")).returncode == 2
        assert cli(str(EXAMPLE)).returncode == 2


# === Actor references in message events (F.3) ==================================

class TestActorReferences:
    """An actor reference in a trigger, an `until:`, a `counts_as` rule or a
    content matches a concrete actor of the trace as F.3 says: a range only
    its members, `R[*]` and an unbound `R[i]` only members of the declared
    set, an undeclared actor never. Before this correction a range matched every
    actor of that name, and `R[*]` also actors outside the declared set."""

    def trigger(self, ref: str, sender: str) -> str:
        r = run(f"""
            norms:
              - id: o
                kind: obligation
                bearer: D
                trigger: "{ref} -> D : go"
                deadline: 5s
                content: "D -> R[1] : ack"
            """, [{"t": 0}, {"t": 1, "messages": [msg(sender, "D", "go")]},
                  {"t": 2, "end": False}])
        return norm(r, "o")["state"]

    @pytest.mark.parametrize("ref, sender, state", [
        ("R[1..2]", "R[1]", "active"), ("R[1..2]", "R[2]", "active"),
        ("R[1..2]", "R[3]", "inactive"), ("R[1..2]", "R[9]", "inactive"),
        ("R[*]", "R[3]", "active"), ("R[*]", "R[9]", "inactive"),
        ("R[*]", "R", "inactive"),
        ("R[i]", "R[1]", "active"), ("R[i]", "R[2]", "inactive"),
        ("R[2]", "R[2]", "active"), ("R[2]", "R[3]", "inactive"),
        ("X", "X", "active"), ("X", "X[1]", "inactive"),
    ])
    def test_trigger(self, ref, sender, state):
        assert self.trigger(ref, sender) == state

    def test_unbound_index_matches_members_only(self):
        blocks = """
            norms:
              - id: o
                kind: obligation
                bearer: D
                trigger: "R[j] -> D : go"
                deadline: 5s
                content: "D -> R[1] : ack"
            """
        def state(sender):
            sos = make_sos(blocks, check=False)
            trace = parse_trace(json.dumps(o) for o in [
                {"t": 0}, {"t": 1, "messages": [msg(sender, "D", "go")]},
                {"t": 2, "end": False}])
            return norm(evaluate(sos, trace), "o")["state"]
        assert state("R[3]") == "active" and state("R[9]") == "inactive"

    def test_until_and_counts_as_with_a_range(self):
        blocks = """
            counts_as:
              - {{ when: "R[1..2] -> D : report", creates: reported }}
            norms:
              - id: p
                kind: prohibition
                bearer: D
                until: "R[2..3] -> D : stop"
                content: "D -> X : leak"
            """.format()
        def result(sender, name):
            return run(blocks, [{"t": 0}, {"t": 1, "messages": [msg(sender, "D", name)]},
                                {"t": 2, "end": False}])
        assert norm(result("R[1]", "stop"), "p")["state"] == "active"
        assert norm(result("R[3]", "stop"), "p")["state"] == "discharged"
        assert len(result("R[2]", "report")["institutional_events"]) == 1
        assert result("R[3]", "report")["institutional_events"] == []


class TestTraceValues:
    @pytest.mark.parametrize("line, text", [
        ('{"t": NaN}', "not JSON"),
        ('{"t": Infinity}', "not JSON"),
        ('{"t": 1e999}', "outside the range"),
        ('{"t": 1e16}', "at most"),
        ('{"t": 1, "messages": [{"from": "D D", "to": "R[1]", "name": "x"}]}', "concrete actor"),
        ('{"t": 1, "messages": [{"from": "D", "to": "R[ 1 ]", "name": "x"}]}', "concrete actor"),
        ('{"t": 1, "messages": [{"from": "D", "to": "R[1]", "name": "x y"}]}', "message name"),
    ])
    def test_malformed_values_are_trace_errors(self, line, text):
        with pytest.raises(TraceError, match=text):
            parse_trace(['{"t": 0}', line])


class TestSymbolicUpperBound:
    """A set declared as `S[1..N]` has every index from 1 on, not `S[0]`
    (z1.cadl is the reviewer's file)."""

    def states(self, sender: str) -> dict:
        from cadl.parser import parse_file
        sos = parse_file(Path(__file__).parent / "data" / "agents_review" / "z1.cadl")
        trace = parse_trace(json.dumps(o) for o in [
            {"t": 0}, {"t": 1, "messages": [msg(sender, "P", "go")]}, {"t": 2, "end": False}])
        result = evaluate(sos, trace)
        return {n["id"]: n["state"] for n in result["norms"]}

    def test_lower_bound_of_a_set_with_a_symbolic_upper_bound(self):
        below, inside, far = self.states("S[0]"), self.states("S[2]"), self.states("S[50]")
        assert (below["n_sym"], below["n_symr"], below["n_symi"]) == ("inactive",) * 3
        assert (inside["n_sym"], inside["n_symr"], inside["n_symi"]) == ("active",) * 3
        assert (far["n_sym"], far["n_symr"], far["n_symi"]) == ("active", "active", "inactive")

    def test_time_in_epoch_milliseconds(self):
        trace = parse_trace(['{"t": 1700000000000}', '{"t": 1700000000001.5}'])
        assert trace.observations[1].t == 1700000000001.5
        with pytest.raises(TraceError, match="at most"):
            parse_trace(['{"t": 9007199254740994}'])


# === Exact numbers and times (F.5) ===============================================

_EXACT = """\
sos:
  name: Exact
  extensions:
    - norms: 0.1
  actors:
    - {id: A, role: a}
    - {id: B, role: b}
  contracts:
    - id: C
      parties: [A, B]
      guarantee:
        - "A.x + A.y == 0.3"
        - "within(100ms, A.z == 1)"
      norms:
        - id: o
          kind: obligation
          bearer: A
          trigger: "A -> B : req"
          content: "A -> B : ack"
          deadline: 100ms
"""


class TestExactNumbers:
    """A number of a trace is the decimal it is written as, times are
    compared without tolerance, and arithmetic is exact: 0.1 + 0.2 == 0.3,
    and epoch seconds with milliseconds compare exactly."""

    def _run(self, *records):
        return evaluate(parse(_EXACT), parse_trace([json.dumps(r) for r in records]))

    def test_sum_of_decimals(self):
        r = self._run({"t": 0, "values": {"A.x": 0.1, "A.y": 0.2}}, {"t": 1, "end": True})
        assert r["guarantees"][0]["verdict"] == "fulfilled"

    def test_epoch_milliseconds_at_the_deadline_are_in_time(self):
        msg = {"from": "A", "to": "B"}
        r = self._run({"t": 1700000000.1, "messages": [dict(msg, name="req")]},
                      {"t": 1700000000.2, "values": {"A.z": 1},
                       "messages": [dict(msg, name="ack")]})
        assert [n["state"] for n in r["norms"]] == ["fulfilled"]
        assert r["guarantees"][1]["verdict"] == "fulfilled"
        assert r["norms"][0]["time"] == 1700000000.2       # written as a float

    def test_one_microsecond_after_the_deadline_is_late(self):
        """Written in the trace with more digits than a float near 1.7e9
        holds (its spacing there is 2.4e-7 s)."""
        m = '"messages": [{"from": "A", "to": "B", "name": "%s"}]'
        lines = ['{"t": 1700000000.1, %s}' % (m % "req"),
                 '{"t": 1700000000.2000001, %s}' % (m % "ack")]
        r = evaluate(parse(_EXACT), parse_trace(lines))
        assert [n["state"] for n in r["norms"]] == ["violated"]
        lines[1] = lines[1].replace("2000001", "2000000")
        r = evaluate(parse(_EXACT), parse_trace(lines))
        assert [n["state"] for n in r["norms"]] == ["fulfilled"]

    def test_digits_beyond_a_float_are_kept(self):
        lines = ['{"t": 0, "values": {"A.x": 0.10000000000000000001, "A.y": 0.2}}',
                 '{"t": 1, "end": true}']
        r = evaluate(parse(_EXACT), parse_trace(lines))
        assert r["guarantees"][0]["verdict"] == "violated"


class TestRangeOfNumbers:
    """Exact numbers have a range (F.5): at most 400 significant digits,
    at most 400 decimals, an absolute value of at most 1.7976931348623157e308. A number of a
    trace outside it is a trace error with its line; written exactly it
    would take time and memory without bound."""

    @pytest.mark.parametrize("number", ["1e-100000000", "1e-401", "1.7976931348623158e308", "1e309", "1e400",
                                        "1" + "0" * 400, "0." + "1" * 401])
    def test_refused_with_the_line(self, number):
        import time
        start = time.time()
        with pytest.raises(TraceError, match="line 2: the number .* outside the range"):
            parse_trace(['{"t": 0}', '{"t": 1, "values": {"A.x": %s}}' % number])
        assert time.time() - start < 1

    @pytest.mark.parametrize("number", ["1e-400", "9.99e307", "1" + "0" * 300, "0e5000", "-0.0",
                                        "1e308", "1.7976931348623157e308",
                                        "-1.7976931348623157e308"])
    def test_accepted(self, number):
        parse_trace(['{"t": 0, "values": {"A.x": %s}}' % number])

    def test_literal_outside_the_range_is_not_evaluated(self):
        sos = parse(_EXACT.replace('"A.x + A.y == 0.3"', '"A.x > 1e-500"'))
        r = evaluate(sos, parse_trace(['{"t": 0, "values": {"A.x": 1}}', '{"t": 1}']))
        assert "outside the range of exact numbers" in r["guarantees"][0]["reason"]


class TestExactDeadline:
    """A numeric deadline in seconds is exact too: 0.0005 is half a
    millisecond (deadline_ms rounds it to 0 for the other checks)."""

    def test_half_a_millisecond(self):
        text = _EXACT.replace("deadline: 100ms", "deadline: 0.0005")
        m = '"messages": [{"from": "A", "to": "B", "name": "%s"}]'
        for at, state in (("0.0005", "fulfilled"), ("0.0006", "violated")):
            r = evaluate(parse(text), parse_trace(
                ['{"t": 0, %s}' % (m % "req"), '{"t": %s, %s}' % (at, m % "ack")]))
            assert r["norms"][0]["state"] == state, at
