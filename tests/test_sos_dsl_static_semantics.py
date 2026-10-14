"""Checks that follow the specification where cadl 0.3.9 deviates from it.

Appendix A (A.3 autonomy, A.5 steps) and Appendix E (E.4 static
semantics, rules L-1 to M-3).
"""

import textwrap

import pytest

from cadl.ast_nodes import BarrierStep, ConditionalStep
from cadl.parser import parse, parse_rule
from cadl.sim import lower_to_ir
from cadl.type_checker import type_check


def check(body: str):
    return type_check(parse("sos:\n  name: T\n" + textwrap.indent(textwrap.dedent(body), "  ")))


def messages(items) -> str:
    return "\n".join(str(i) for i in items)


ACTORS = """
actors:
  - id: A
    role: a
  - id: "R[1..3]"
    role: r
"""


def contract(*blocks: str) -> str:
    """A contract C whose body is the given blocks, each written flush left."""
    return ACTORS + textwrap.dedent("""
        contracts:
          - id: C
            parties: [A, "R[*]"]
        """) + "".join(textwrap.indent(textwrap.dedent(b), "    ") for b in blocks)


def transition_keys(keys: str) -> str:
    """Further keys of the last transition of LIFECYCLE."""
    return LIFECYCLE + textwrap.indent(textwrap.dedent(keys), "      ")


LIFECYCLE = """
lifecycle:
  states: [Open, Done, Failed]
  initial: Open
  terminal: [Done, Failed]
  transitions:
    - id: finish
      from: Open
      to: Done
      on: "A -> R[i] : done"
"""


# --- Actors (A.3) ---

class TestAutonomy:
    def test_unknown_autonomy_is_an_error(self):
        result = check("actors:\n  - id: A\n    role: r\n    autonomy: total\n")
        assert "Unknown autonomy 'total'" in messages(result.errors)


# --- Steps (A.5) ---

class TestSteps:
    SOURCE = ACTORS + textwrap.dedent("""
        protocols:
          - id: P
            trigger: t
            steps:
              - "if ready == true":
                  - "A -> R[*] : go"
                else:
                  - "A -> R[*] : wait"
              - barrier: "all_ready == true"
        """)

    def test_else_branch_is_kept(self):
        sos = parse("sos:\n  name: T\n" + textwrap.indent(self.SOURCE, "  "))
        step = sos.protocols[0].steps[0]
        assert isinstance(step, ConditionalStep)
        assert len(step.then_steps) == 1 and len(step.else_steps) == 1

    def test_barrier_condition_is_kept(self):
        sos = parse("sos:\n  name: T\n" + textwrap.indent(self.SOURCE, "  "))
        step = sos.protocols[0].steps[1]
        assert isinstance(step, BarrierStep)
        contents = [s.content for s in lower_to_ir(sos).protocol.protocols[0].steps]
        assert "all_ready == true" in contents
        assert "else" in contents

    def test_undeclared_actor_in_else_branch_is_reported(self):
        result = check(self.SOURCE.replace("A -> R[*] : wait", "GHOST -> R[*] : wait"))
        assert "Undefined actor 'GHOST'" in messages(result.errors)


# --- SoS-DSL static semantics (E.4) ---

class TestLifecycleRules:
    def test_valid_lifecycle_passes(self):
        result = check(contract(LIFECYCLE))
        assert result.ok and not result.warnings

    def test_l1_initial_and_terminal_are_declared_states(self):
        result = check(contract(LIFECYCLE.replace("initial: Open", "initial: Start")
                                         .replace("[Done, Failed]\n", "[Done, Gone]\n")))
        text = messages(result.errors)
        assert "initial state 'Start'" in text and "(L-1)" in text
        assert "terminal state 'Gone'" in text

    def test_l2_from_and_to_are_declared_states(self):
        result = check(contract(LIFECYCLE.replace("from: Open", "from: [Open, Nowhere]")
                                         .replace("to: Done", "to: Finished")))
        text = messages(result.errors)
        assert "leaves 'Nowhere'" in text and "enters 'Finished'" in text

    def test_l3_needs_a_terminal_state(self):
        result = check(contract(LIFECYCLE.replace("terminal: [Done, Failed]", "terminal: []")))
        assert "lists no terminal state (L-3)" in messages(result.errors)

    def test_l3_a_terminal_state_is_reachable(self):
        result = check(contract(LIFECYCLE.replace("to: Done", "to: Open")))
        assert "is reachable" in messages(result.errors)

    def test_l3_a_monitor_can_reach_the_terminal_state(self):
        result = check(contract(LIFECYCLE.replace("to: Done", "to: Open"), """
            monitors:
              - id: guard
                observe: "R[i].battery"
                rule: "R[i].battery < 20"
                on_match:
                  transition: Failed
            """))
        assert result.ok

    def test_l4_deadline_without_on_violation_is_a_warning(self):
        result = check(contract(transition_keys("deadline: 5s\n")))
        assert result.ok
        assert "(L-4)" in messages(result.warnings)

    def test_l5_on_violation_names_a_state(self):
        result = check(contract(transition_keys("""\
            deadline: 5s
            on_violation:
              transition: finish
            """)))
        assert "(L-5)" in messages(result.errors)

    def test_malformed_rule_is_a_warning(self):
        result = check(contract(transition_keys("when: \"now <= <= 3\"\n")))
        assert result.ok
        assert "is not a valid rule" in messages(result.warnings)

    def test_undeclared_actor_in_message_event(self):
        result = check(contract(LIFECYCLE.replace("A -> R[i] : done", "GHOST -> R[i] : done")))
        assert "Undefined actor 'GHOST'" in messages(result.errors)


class TestMonitorRules:
    def monitor(self, body: str) -> str:
        return contract(
            LIFECYCLE, "monitors:\n  - id: m\n" + textwrap.indent(textwrap.dedent(body), "    ")
        )

    def test_m1_observe_an_undeclared_actor(self):
        result = check(self.monitor("""
            observe: ["R[i].battery", "OBSTACLES.positions", time, state, position_report]
            rule: "R[i].battery < 20"
            """))
        assert not result.ok
        text = messages(result.errors)
        assert "'OBSTACLES' is not a declared actor (M-1)" in text
        assert text.count("(M-1)") == 1

    def test_m1_observe_is_an_attribute_or_a_name(self):
        result = check(self.monitor("""
            observe: ["R[i].battery < 20"]
            rule: "R[i].battery < 20"
            """))
        assert "neither an attribute of an actor nor a name (M-1)" in messages(result.errors)

    def test_bundled_example_has_no_diagnostic(self):
        from pathlib import Path
        path = Path(__file__).parent.parent / "examples" / "sos_dsl_robot_delivery.cadl"
        result = type_check(parse(path.read_text(encoding="utf-8")))
        assert result.ok and not result.warnings

    def test_m3_on_match_names_a_state(self):
        result = check(self.monitor("""
            observe: "R[i].battery"
            rule: "R[i].battery < 20"
            on_match:
              transition: finish
            """))
        assert "(M-3)" in messages(result.errors)

    def test_m3_needs_a_lifecycle(self):
        result = check(contract("""
            monitors:
              - id: m
                observe: "R[i].battery"
                rule: "R[i].battery < 20"
                on_match:
                  transition: Failed
            """))
        assert "(M-3)" in messages(result.errors)

    def test_membership_rule(self):
        result = check(self.monitor("""
            observe: state
            rule: "state IN [Open, Done] AND now > request.deadline"
            """))
        assert result.ok and not result.warnings

    def test_parse_rule_reads_membership(self):
        parse_rule("state IN [Assigned, Accepted]")
        with pytest.raises(Exception):
            parse_rule("state IN")

    def test_monitor_without_rule(self):
        result = check(self.monitor("observe: state\n"))
        assert "has no rule" in messages(result.errors)

    def test_state_and_monitor_names_are_identifiers(self):
        result = check(contract(LIFECYCLE.replace("Failed", "IN"), """
            monitors:
              - id: "battery-guard"
                observe: state
                rule: "state == Open"
            """))
        text = messages(result.errors)
        assert "'IN' is a reserved word" in text
        assert "'battery-guard' is not a valid identifier" in text


# --- Duration values (E.3): a value that cannot be read is an error ---

class TestDurationValues:
    @pytest.mark.parametrize("value", ["5s", "500ms", "2min", "1h", "0s", "10"])
    def test_readable_deadline_is_accepted(self, value):
        result = check(contract(transition_keys(
            f"deadline: {value}\non_violation: {{transition: Failed}}\n")))
        assert result.ok, messages(result.errors)

    @pytest.mark.parametrize("value, shown", [
        ("soon", "soon"), ("5sec", "5sec"), ("1.5s", "1.5s"),
        ("-5s", "-5s"), ("-5", "-5"), ("yes", "true"),
    ])
    def test_unreadable_deadline_is_an_error(self, value, shown):
        result = check(contract(transition_keys(
            f"deadline: {value}\non_violation: {{transition: Failed}}\n")))
        assert f"has deadline '{shown}', which is not a duration" in messages(result.errors)

    def test_unreadable_deadline_is_not_also_an_l4_warning(self):
        result = check(contract(transition_keys("deadline: soon\n")))
        assert "not a duration" in messages(result.errors)
        assert "(L-4)" not in messages(result.warnings)

    def test_ir_of_a_valid_deadline_is_unchanged(self):
        source = "sos:\n  name: T\n" + textwrap.indent(textwrap.dedent(contract(transition_keys(
            "deadline: 5s\non_violation: {transition: Failed}\n"))), "  ")
        transition = lower_to_ir(parse(source)).institution.contracts[0].lifecycle.transitions[0]
        assert transition.deadline_ms == 5000

    def _monitor(self, sampling: str):
        return check(contract(
            LIFECYCLE,
            "monitors:\n  - id: m\n    rule: \"x > 1\"\n"
            f"    sampling: {sampling}\n",
        ))

    @pytest.mark.parametrize("value", [
        "event", "periodic(500ms)", '"periodic(2s)"', "{kind: periodic, period_ms: 500}",
        "{kind: event}",
    ])
    def test_readable_sampling_is_accepted(self, value):
        result = self._monitor(value)
        assert "sampling" not in messages(result.errors), messages(result.errors)

    @pytest.mark.parametrize("value", [
        "periodic(soon)", '"every 500ms"', "sometimes",
        "{kind: periodic, period_ms: fast}", "{kind: sometimes}",
    ])
    def test_unreadable_sampling_is_an_error(self, value):
        result = self._monitor(value)
        assert "which is neither 'event' nor 'periodic(<duration>)'" in messages(result.errors)
