"""Checks that follow the specification where cadl 0.3.8 deviated from it.

Appendix A (A.1 identifiers, A.4 sharing entries, A.5 steps, A.11 reserved
words), Appendix C (motivation block), Appendix D (D.4 codegen targets) and
Appendix E (E.4 static semantics, rules L-1 to M-3).
"""

import textwrap

import pytest

from cadl.ast_nodes import BarrierStep, ConditionalStep
from cadl.cli import main
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


# --- Identifiers (A.1, A.11) ---

class TestIdentifiers:
    @pytest.mark.parametrize("name", ["ロボット", "my-actor", "1ST"])
    def test_actor_id_must_be_an_ascii_identifier(self, name):
        result = check(f"actors:\n  - id: \"{name}\"\n    role: r\n")
        assert not result.ok
        assert f"Invalid identifier '{name}'" in messages(result.errors)

    @pytest.mark.parametrize("name", ["AND", "OR", "NOT", "true", "false", "exists", "in"])
    def test_reserved_word_is_not_an_identifier(self, name):
        result = check(f"actors:\n  - id: \"{name}\"\n    role: r\n")
        assert f"Reserved word '{name}'" in messages(result.errors)

    def test_names_that_only_begin_with_a_keyword_are_identifiers(self):
        result = check("actors:\n  - id: NOTIFIER\n    role: r\n  - id: ORDER\n    role: r\n")
        assert result.ok

    def test_contract_protocol_metric_and_regime_names(self):
        result = check(ACTORS + textwrap.dedent("""
            contracts:
              - id: "c-1"
                parties: [A]
            protocols:
              - id: "手順"
                trigger: t
                steps: []
            metrics:
              - id: "m 1"
            transitions:
              - from: "central-mode"
                to: Local
            """))
        text = messages(result.errors)
        for name in ("c-1", "手順", "m 1", "central-mode"):
            assert f"Invalid identifier '{name}'" in text

    def test_unicode_text_outside_identifiers_is_accepted(self):
        result = check("description: \"配送ロボット\"\nactors:\n  - id: A\n    role: \"調整役\"\n")
        assert result.ok


# --- Contracts and actors (A.3, A.4) ---

class TestLenientValues:
    def test_unquoted_sharing_entry_is_an_error(self):
        result = check(contract("""
            information:
              sharing:
                - A -> R[*] : position
            """))
        assert "must be a quoted string" in messages(result.errors)

    def test_quoted_sharing_entry_is_accepted(self):
        result = check(contract("""
            information:
              sharing:
                - "A -> R[*] : position"
            """))
        assert result.ok

    def test_malformed_sharing_entry_is_an_error(self):
        result = check(contract("""
            information:
              sharing:
                - "A shares position"
            """))
        assert "Malformed sharing entry" in messages(result.errors)

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
        assert result.ok
        text = messages(result.warnings)
        assert "'OBSTACLES' is not a declared actor (M-1)" in text
        assert text.count("(M-1)") == 1

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


# --- Codegen targets (D.4) ---

class TestCodegenTargets:
    SOURCE = "sos:\n  name: T\n  actors:\n    - id: A\n      role: r\n  codegen:\n    - target: ros2\n    - target: python\n"

    def test_unsupported_target_is_reported(self):
        result = type_check(parse(self.SOURCE))
        assert result.ok
        text = messages(result.warnings)
        assert "codegen target 'ros2' is not supported" in text
        assert "'python'" not in text

    def test_cli_reports_it(self, tmp_path, capsys):
        path = tmp_path / "t.cadl"
        path.write_text(self.SOURCE)
        assert main(["check", str(path)]) == 0
        assert "codegen target 'ros2' is not supported" in capsys.readouterr().err
        assert main(["codegen", str(path), "-o", str(tmp_path / "out")]) == 0
        assert "codegen target 'ros2' is not supported" in capsys.readouterr().err


# --- Extensions and motivation (A.2, Appendix C) ---

class TestExtensions:
    def test_declared_sos_dsl(self):
        result = check("extensions:\n  - sos-dsl: 0.1\n")
        assert result.ok and not result.warnings

    def test_unknown_extension_and_version(self):
        result = check("extensions:\n  - sos-dsl: 0.9\n  - my-ext: 1\n")
        text = messages(result.warnings)
        assert "'my-ext' is not implemented" in text
        assert "version '0.9'" in text


class TestMotivation:
    BLOCK = """
        motivation:
          agent:
            profile: linear
          governance:
            model: hybrid
            rho: 0.6
        """

    def test_block_is_parsed(self):
        sos = parse("sos:\n  name: T\n" + textwrap.indent(textwrap.dedent(self.BLOCK), "  "))
        assert sos.motivation.agent.profile == "linear"
        assert sos.motivation.governance.model == "hybrid"
        assert sos.motivation.governance.rho == 0.6
        assert sos.motivation.governance.kappa == 5.0

    def test_informational_diagnostic(self):
        result = check(self.BLOCK)
        assert result.ok and not result.warnings
        assert "motivation: block" in messages(result.infos)

    def test_no_diagnostic_without_the_block(self):
        assert not check(ACTORS).infos

    def test_block_is_carried_into_the_ir(self):
        sos = parse("sos:\n  name: T\n" + textwrap.indent(textwrap.dedent(self.BLOCK), "  "))
        assert lower_to_ir(sos).motivation_block == {
            "agent": {"profile": "linear"},
            "governance": {"model": "hybrid", "rho": 0.6},
        }

    def test_enumerations_and_ranges(self):
        result = check("""
            motivation:
              agent:
                profile: random
                values: [0.5, 1.5]
              governance:
                model: fatigue
                rho: 2
                kappa: -1
                budget_base: many
            """)
        text = messages(result.errors)
        for part in ("profile 'random'", "model 'fatigue'", "rho must be in [0, 1]",
                     "kappa must not be negative", "got 1.5",
                     "'budget_base' must be an integer"):
            assert part in text

    def test_custom_profile_needs_values(self):
        result = check("motivation:\n  agent:\n    profile: custom\n")
        assert "requires a 'values' list" in messages(result.errors)
