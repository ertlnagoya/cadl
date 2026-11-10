"""Norms extension (Appendix F, norms 0.1): parsing, rules N-1 to N-12 of
F.7, the temporal operators of F.4, the verifier (F.7) and the IR (F.9)."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cadl.ast_nodes import FunctionCall
from cadl.cli import _ir_to_dict
from cadl.parser import parse, parse_file
from cadl.sim import lower_to_ir
from cadl.type_checker import type_check
from cadl.verifier import verify

EXAMPLE = Path(__file__).parent.parent / "examples" / "norms_robot_delivery.cadl"


def source(*blocks: str, declare: str | None = "norms: 0.1") -> str:
    """A file with actors D, R[1..3], X and a contract C between D and R[*],
    whose further keys are the given blocks, each written flush left."""
    ext = f"  extensions:\n    - {declare}\n" if declare else ""
    head = (
        "sos:\n  name: T\n" + ext +
        "  actors:\n"
        "    - {id: D, role: d}\n"
        "    - {id: \"R[1..3]\", role: r}\n"
        "    - {id: X, role: x}\n"
        "  contracts:\n"
        "    - id: C\n"
        "      parties: [D, \"R[*]\"]\n"
    )
    return head + "".join(textwrap.indent(textwrap.dedent(b), "      ") for b in blocks)


def check(*blocks: str, declare: str | None = "norms: 0.1"):
    return type_check(parse(source(*blocks, declare=declare)))


def with_rule(items, rule: str) -> list[str]:
    return [str(i) for i in items if f"({rule})" in str(i)]


def norms(*entries: str) -> str:
    return "norms:\n" + "".join(
        textwrap.indent(textwrap.dedent(e).strip("\n") + "\n", "  ") for e in entries
    )


OBLIGATION = """
- id: ack
  kind: obligation
  bearer: "R[i]"
  counterparty: D
  trigger: "D -> R[i] : assignment"
  content: "R[i] -> D : ack"
  deadline: 5s
"""

LIFECYCLE = """
lifecycle:
  states: [Open, Done, Failed]
  initial: Open
  terminal: [Done, Failed]
  transitions:
    - { id: finish, from: Open, to: Done, on: "R[i].status == Delivered" }
"""

COUNTS_AS = """
counts_as:
  - when: "D -> R[i] : replan"
    creates: reassignment
"""


def norm(**fields) -> str:
    """A norm entry as YAML; values are written as given."""
    lines = [f"{k}: {v}" for k, v in fields.items()]
    return "- " + "\n  ".join(lines)


# --- Parsing ---------------------------------------------------------------

class TestParsing:
    def test_norm_fields(self):
        sos = parse(source(norms(OBLIGATION + """
  until: "R[i].status == Delivered"
  on_violation:
    severity: Critical
    sanction: "a fine"
    reparation: other
    transition: Failed
  on_amendment: { transform: ack2 }
""")))
        n = sos.contracts[0].norms[0]
        assert (n.id, n.kind, n.bearer, n.counterparty) == ("ack", "obligation", "R[i]", "D")
        assert n.trigger == "D -> R[i] : assignment"
        assert n.until == "R[i].status == Delivered"
        assert n.content == "R[i] -> D : ack"
        assert n.deadline_ms == 5000
        assert n.on_violation.severity == "Critical"
        assert n.on_violation.sanction == "a fine"
        assert n.on_violation.reparation == "other"
        assert n.on_violation.transition == "Failed"
        assert (n.on_amendment, n.transform_to) == ("transform", "ack2")

    def test_defaults(self):
        sos = parse(source(norms("""
- id: p
  kind: obligation
  bearer: D
  content: done
  on_violation: {}
""")))
        n = sos.contracts[0].norms[0]
        assert n.on_violation.severity == "Major"
        assert n.on_amendment == "carry_over"
        assert n.transform_to is None and n.effects is None and n.trigger is None

    def test_power_effects(self):
        sos = parse(source(norms("""
- id: pw
  kind: power
  bearer: D
  content: reassignment
  effects:
    activate: a
    suspend: [b, c]
    resume: [d]
    discharge: [e]
""")))
        eff = sos.contracts[0].norms[0].effects
        assert (eff.activate, eff.suspend, eff.resume, eff.discharge) == (
            ["a"], ["b", "c"], ["d"], ["e"])

    def test_counts_as_with_unquoted_if(self):
        sos = parse(source(COUNTS_AS + "    if: R[i].battery > 20\n"))
        r = sos.contracts[0].counts_as[0]
        assert (r.when, r.creates, r.if_) == (
            "D -> R[i] : replan", "reassignment", "R[i].battery > 20")

    def test_yaml_booleans_kept_as_written(self):
        sos = parse(source(norms("""
- id: p
  kind: obligation
  bearer: D
  content: yes
  on_amendment: no
""")))
        n = sos.contracts[0].norms[0]
        assert n.content == "true"
        assert n.invalid_on_amendment == "false"

    def test_without_blocks(self):
        c = parse(source()).contracts[0]
        assert c.norms == [] and c.counts_as == []

    def test_parsed_without_declaration(self):
        c = parse(source(norms(OBLIGATION), declare=None)).contracts[0]
        assert c.norms[0].id == "ack"


# --- Extension declaration (F.11) ------------------------------------------

class TestDeclaration:
    def test_declared_norms_is_known(self):
        res = check()
        assert not res.infos

    def test_other_version_gets_info(self):
        res = check(declare="norms: 0.2")
        assert any("version '0.2'" in str(i) for i in res.infos)

    def test_undeclared_keys_warn(self):
        res = check(norms(OBLIGATION), COUNTS_AS, declare=None)
        assert res.ok
        assert any("norms: and counts_as:" in str(w) and "F.11" in str(w)
                   for w in res.warnings)

    def test_undeclared_keys_not_checked(self):
        res = check(norms(norm(id="AND", kind="duty", bearer="GHOST", content="x")),
                    declare=None)
        assert res.ok

    def test_other_version_is_undeclared(self):
        res = check(norms(OBLIGATION), declare="norms: 0.2")
        assert any("F.11" in str(w) for w in res.warnings)

    def test_temporal_names_are_ordinary_without_declaration(self):
        res = check(LIFECYCLE.replace('"R[i].status == Delivered"',
                                      '"within(R[i].status)"'), declare=None)
        assert res.ok and not res.warnings
        res = check('assume:\n  - "until(a, b)"\n', declare=None)
        assert res.ok


# --- N-1 to N-12 -----------------------------------------------------------

class TestRules:
    def test_valid_contract_has_no_diagnostic(self):
        res = check(LIFECYCLE, COUNTS_AS, norms(OBLIGATION, """
- id: pw
  kind: power
  bearer: D
  content: reassignment
  effects: { discharge: [ack] }
"""))
        assert res.ok and not res.warnings and not res.infos, res.errors + res.warnings

    # N-1
    @pytest.mark.parametrize("bad", ["AND", "IN", "yes", "off", '"not-an-id"', '"1st"'])
    def test_n1_norm_id_identifier(self, bad):
        res = check(norms(norm(id=bad, kind="permission", bearer="D", content="x")))
        assert with_rule(res.errors, "N-1")

    def test_n1_missing_id(self):
        res = check(norms(norm(kind="permission", bearer="D", content="x")))
        assert with_rule(res.errors, "N-1")

    def test_n1_duplicate_id(self):
        res = check(norms(OBLIGATION, OBLIGATION))
        assert any("Duplicate norm" in e for e in with_rule(res.errors, "N-1"))

    def test_n1_event_name_identifier(self):
        res = check(COUNTS_AS.replace("reassignment", '"re-assign"'))
        assert with_rule(res.errors, "N-1")

    def test_n1_event_name_equals_norm_id(self):
        res = check(COUNTS_AS.replace("reassignment", "ack"), norms(OBLIGATION))
        assert any("same name as a norm" in e for e in with_rule(res.errors, "N-1"))

    def test_n1_event_name_yaml_boolean(self):
        res = check(COUNTS_AS.replace("reassignment", "on"))
        assert any("reserved word" in e for e in with_rule(res.errors, "N-1"))

    def test_n1_event_name_ok(self):
        assert not with_rule(check(COUNTS_AS).errors, "N-1")

    # N-2
    @pytest.mark.parametrize("kind", ["duty", "Obligation"])
    def test_n2_kind(self, kind):
        res = check(norms(norm(id="n", kind=kind, bearer="D", content="x")))
        assert with_rule(res.errors, "N-2")

    def test_n2_missing_kind(self):
        res = check(norms(norm(id="n", bearer="D", content="x")))
        assert with_rule(res.errors, "N-2")

    @pytest.mark.parametrize("kind", ["obligation", "permission", "prohibition"])
    def test_n2_ok(self, kind):
        res = check(norms(norm(id="n", kind=kind, bearer="D", content="x")))
        assert not with_rule(res.errors, "N-2")

    # N-3
    def test_n3_bearer_undeclared(self):
        res = check(norms(norm(id="n", kind="permission", bearer="GHOST", content="x")))
        assert with_rule(res.errors, "N-3")

    def test_n3_counterparty_undeclared(self):
        res = check(norms(norm(id="n", kind="permission", bearer="D",
                               counterparty='"GHOST[1]"', content="x")))
        assert with_rule(res.errors, "N-3")

    def test_n3_missing_bearer(self):
        res = check(norms(norm(id="n", kind="permission", content="x")))
        assert with_rule(res.errors, "N-3")

    def test_n3_not_a_party_is_warning(self):
        res = check(norms(norm(id="n", kind="permission", bearer="X",
                               counterparty="X", content="x")))
        assert res.ok
        assert len(with_rule(res.warnings, "N-3")) == 2

    @pytest.mark.parametrize("bearer", ['"R[*]"', '"R[1..3]"'])
    def test_n3_wildcard_or_range(self, bearer):
        res = check(norms(norm(id="n", kind="permission", bearer=bearer, content="x")))
        assert any("wildcard or a range" in e for e in with_rule(res.errors, "N-3"))

    def test_n3_counterparty_wildcard(self):
        res = check(norms(norm(id="n", kind="permission", bearer="D",
                               counterparty='"R[*]"', content="x")))
        assert with_rule(res.errors, "N-3")

    def test_n3_indexed_party_ok(self):
        res = check(norms(norm(id="n", kind="permission", bearer='"R[i]"', content="x")))
        assert not with_rule(res.errors + res.warnings, "N-3")

    # N-4
    @pytest.mark.parametrize("bad", ["soon", "yes", "-1", '"5 days"'])
    def test_n4_deadline_not_duration(self, bad):
        res = check(norms(norm(id="n", kind="obligation", bearer="D", content="x",
                               deadline=bad)))
        assert with_rule(res.errors, "N-4")

    @pytest.mark.parametrize("kind", ["permission", "power"])
    def test_n4_deadline_only_obligation_prohibition(self, kind):
        res = check(COUNTS_AS, norms(OBLIGATION, norm(
            id="n", kind=kind, bearer="D", content="reassignment", deadline="5s",
            effects="{discharge: [ack]}")))
        assert any("cannot have a deadline" in e for e in with_rule(res.errors, "N-4"))

    @pytest.mark.parametrize("kind", ["permission", "power"])
    def test_n4_on_violation_only_obligation_prohibition(self, kind):
        res = check(COUNTS_AS, norms(OBLIGATION, norm(
            id="n", kind=kind, bearer="D", content="reassignment",
            on_violation="{severity: Minor}", effects="{discharge: [ack]}")))
        assert any("on_violation" in e for e in with_rule(res.errors, "N-4"))

    @pytest.mark.parametrize("kind", ["obligation", "prohibition"])
    def test_n4_ok(self, kind):
        res = check(norms(norm(id="n", kind=kind, bearer="D", content="x",
                               deadline="500ms", on_violation="{severity: Minor}")))
        assert res.ok, res.errors

    def test_norm_severity(self):
        res = check(norms(norm(id="n", kind="obligation", bearer="D", content="x",
                               on_violation="{severity: Fatal}")))
        assert any("Unknown severity 'Fatal'" in str(e) for e in res.errors)

    # N-5
    def test_n5_effects_only_for_power(self):
        res = check(norms(OBLIGATION, norm(id="n", kind="permission", bearer="D",
                                           content="x", effects="{activate: [ack]}")))
        assert any("only a power" in e for e in with_rule(res.errors, "N-5"))

    @pytest.mark.parametrize("effects", [None, "{}", "{discharge: []}", "[ack]"])
    def test_n5_power_needs_effect(self, effects):
        fields = dict(id="pw", kind="power", bearer="D", content="reassignment")
        if effects is not None:
            fields["effects"] = effects
        res = check(COUNTS_AS, norms(OBLIGATION, norm(**fields)))
        assert any("without effects" in e for e in with_rule(res.errors, "N-5"))

    def test_n5_effect_names_a_norm(self):
        res = check(COUNTS_AS, norms(norm(id="pw", kind="power", bearer="D",
                                          content="reassignment",
                                          effects="{suspend: [ghost]}")))
        assert any("'ghost'" in e for e in with_rule(res.errors, "N-5"))

    def test_n5_effect_not_itself(self):
        res = check(COUNTS_AS, norms(norm(id="pw", kind="power", bearer="D",
                                          content="reassignment",
                                          effects="{resume: [pw]}")))
        assert any("itself" in e for e in with_rule(res.errors, "N-5"))

    # N-6
    def test_n6_reparation_must_exist(self):
        res = check(norms(norm(id="a", kind="obligation", bearer="D", content="x",
                               on_violation="{reparation: ghost}")))
        assert with_rule(res.errors, "N-6")

    def test_n6_reparation_kind(self):
        res = check(norms(
            norm(id="a", kind="obligation", bearer="D", content="x",
                 on_violation="{reparation: b}"),
            norm(id="b", kind="permission", bearer="D", content="y")))
        assert any("obligation or a prohibition" in e for e in with_rule(res.errors, "N-6"))

    def test_n6_reparation_without_trigger(self):
        res = check(norms(
            norm(id="a", kind="obligation", bearer="D", content="x",
                 on_violation="{reparation: b}"),
            norm(id="b", kind="obligation", bearer="D", content="y", trigger="go")))
        assert any("has a trigger" in e for e in with_rule(res.errors, "N-6"))

    def test_n6_cycle(self):
        res = check(norms(
            norm(id="a", kind="obligation", bearer="D", content="x",
                 on_violation="{reparation: b}"),
            norm(id="b", kind="obligation", bearer="D", content="y",
                 on_violation="{reparation: a}")))
        cycles = [e for e in with_rule(res.errors, "N-6") if "cycle" in e]
        assert len(cycles) == 1

    def test_n6_self_cycle(self):
        res = check(norms(norm(id="a", kind="obligation", bearer="D", content="x",
                               on_violation="{reparation: a}")))
        assert any("cycle" in e for e in with_rule(res.errors, "N-6"))

    def test_n6_chain_ok(self):
        res = check(norms(
            norm(id="a", kind="obligation", bearer="D", content="x",
                 on_violation="{reparation: b}"),
            norm(id="b", kind="prohibition", bearer="D", content="y",
                 on_violation="{reparation: c}"),
            norm(id="c", kind="obligation", bearer="D", content="z")))
        assert res.ok, res.errors

    # N-7
    def test_n7_state_of_lifecycle(self):
        res = check(LIFECYCLE, norms(norm(id="a", kind="obligation", bearer="D",
                                          content="x", on_violation="{transition: Gone}")))
        assert with_rule(res.errors, "N-7")

    def test_n7_needs_lifecycle(self):
        res = check(norms(norm(id="a", kind="obligation", bearer="D", content="x",
                               on_violation="{transition: Failed}")))
        assert any("needs a lifecycle" in e for e in with_rule(res.errors, "N-7"))

    def test_n7_ok(self):
        res = check(LIFECYCLE, norms(norm(id="a", kind="obligation", bearer="D",
                                          content="x", on_violation="{transition: Failed}")))
        assert res.ok, res.errors

    # N-8
    @pytest.mark.parametrize("key", ["trigger", "until", "content"])
    def test_n8_norm_id_as_predicate_warns(self, key):
        fields = dict(id="b", kind="obligation", bearer="D", content="y")
        fields[key] = "a"
        res = check(norms(norm(id="a", kind="permission", bearer="D", content="x"),
                          norm(**fields)))
        assert res.ok
        assert with_rule(res.warnings, "N-8")

    def test_n8_event_or_state_variable_ok(self):
        res = check(COUNTS_AS, norms(norm(id="b", kind="obligation", bearer="D",
                                          trigger="reassignment", content="ready")))
        assert not with_rule(res.warnings, "N-8") and res.ok

    # N-9
    @pytest.mark.parametrize("key", ["trigger", "until", "content"])
    def test_n9_message_actors_of_norm(self, key):
        fields = dict(id="b", kind="obligation", bearer="D", content="y")
        fields[key] = '"GHOST -> D : m"'
        res = check(norms(norm(**fields)))
        assert any("sender 'GHOST'" in e for e in with_rule(res.errors, "N-9"))

    def test_n9_message_receiver(self):
        res = check(norms(norm(id="b", kind="obligation", bearer="D",
                               content='"D -> NOBODY[i] : m"')))
        assert any("receiver" in e for e in with_rule(res.errors, "N-9"))

    def test_n9_counts_as_message_actors(self):
        res = check(COUNTS_AS.replace("D -> R[i]", "D -> Q[i]"))
        assert with_rule(res.errors, "N-9")

    def test_n9_power_content_predicate(self):
        res = check(norms(OBLIGATION, norm(id="pw", kind="power", bearer="D",
                                           content='"x > 1"', effects="{discharge: [ack]}")))
        assert with_rule(res.errors, "N-9")

    def test_n9_power_content_not_an_event(self):
        res = check(norms(OBLIGATION, norm(id="pw", kind="power", bearer="D",
                                           content="reassignment",
                                           effects="{discharge: [ack]}")))
        assert with_rule(res.errors, "N-9")

    def test_n9_power_message_from_other_sender(self):
        res = check(norms(OBLIGATION, norm(id="pw", kind="power", bearer="D",
                                           content='"R[i] -> D : replan"',
                                           effects="{discharge: [ack]}")))
        assert any("not by its bearer" in e for e in with_rule(res.errors, "N-9"))

    def test_n9_power_event_from_predicate(self):
        res = check('counts_as:\n  - when: "D.mode == replan"\n    creates: reassignment\n',
                    norms(OBLIGATION, norm(id="pw", kind="power", bearer="D",
                                           content="reassignment",
                                           effects="{discharge: [ack]}")))
        assert any("from a predicate" in e for e in with_rule(res.errors, "N-9"))

    @pytest.mark.parametrize("kind", ["obligation", "prohibition"])
    def test_n9_sender_should_be_bearer(self, kind):
        res = check(norms(norm(id="n", kind=kind, bearer="D",
                               content='"R[i] -> D : m"')))
        assert res.ok
        assert any("not by its bearer" in w for w in with_rule(res.warnings, "N-9"))

    def test_n9_sender_is_bearer_no_warning(self):
        res = check(norms(OBLIGATION))
        assert not with_rule(res.warnings, "N-9")

    @pytest.mark.parametrize("content", ["reassignment", '"D -> R[i] : replan"'])
    def test_n9_power_ok(self, content):
        res = check(COUNTS_AS, norms(OBLIGATION, norm(
            id="pw", kind="power", bearer="D", content=content,
            effects="{discharge: [ack]}")))
        assert res.ok and not res.warnings, res.errors + res.warnings

    # N-10
    @pytest.mark.parametrize("item", [
        "within(5s, p, q)",
        "within(5s)",
        "throughout(p)",
        "until(p, q)",
        "until(p, q, 5s, 1s)",
    ])
    def test_n10_arity(self, item):
        res = check(f'guarantee:\n  - "{item}"\n')
        assert any("arguments" in e for e in with_rule(res.errors, "N-10"))

    @pytest.mark.parametrize("item", [
        "within(5, p)",
        "within(p, 5s)",
        "throughout(d, p)",
        "until(p, q, 300)",
    ])
    def test_n10_duration_literal(self, item):
        res = check(f'guarantee:\n  - "{item}"\n')
        assert any("duration literal" in e for e in with_rule(res.errors, "N-10"))

    @pytest.mark.parametrize("item", ["within(0s, p)", "until(p, q, 0ms)"])
    def test_n10_duration_positive(self, item):
        res = check(f'assume:\n  - "{item}"\n')
        assert any("greater than zero" in e for e in with_rule(res.errors, "N-10"))

    def test_n10_not_a_predicate(self):
        res = check('assume:\n  - "within(1.5s, p)"\n')
        assert with_rule(res.errors, "N-10")

    def test_n10_nested(self):
        res = check('guarantee:\n  - "within(5s, throughout(0s, p))"\n')
        assert len(with_rule(res.errors, "N-10")) == 1

    @pytest.mark.parametrize("item", [
        "within(300s, R[i].status == Delivered)",
        "throughout(1h, a AND NOT b)",
        "until(R[i].status == InTransit, R[i].status == Delivered, 10min)",
        "within(5s, throughout(1s, p)) OR q",
    ])
    def test_n10_valid_items(self, item):
        res = check(f'guarantee:\n  - "{item}"\n')
        assert res.ok and not res.warnings, res.errors

    def test_n10_arguments_in_content(self):
        res = check(norms(norm(id="b", kind="obligation", bearer="D",
                               content='"within(0s, p)"')))
        assert any("greater than zero" in e for e in with_rule(res.errors, "N-10"))

    @pytest.mark.parametrize("content", [
        '"throughout(10s, R[i].speed > 2)"',
        '"within(5s, R[i].status IN [Delivered, Returned])"',
    ])
    def test_n10_in_content_ok(self, content):
        res = check(norms(norm(id="b", kind="prohibition", bearer="D", content=content)))
        assert res.ok and not res.warnings, res.errors + res.warnings

    @pytest.mark.parametrize("key", ["trigger", "until"])
    def test_n10_forbidden_in_trigger_and_until(self, key):
        fields = dict(id="b", kind="obligation", bearer="D", content="y")
        fields[key] = '"within(5s, R[i].ready)"'
        res = check(norms(norm(**fields)))
        assert any("allows only" in e for e in with_rule(res.errors, "N-10"))

    def test_n10_forbidden_in_counts_as(self):
        res = check('counts_as:\n  - when: "throughout(5s, R[i].speed == 0)"\n'
                    '    creates: stopped\n    if: "within(1s, D.ready)"\n')
        assert len(with_rule(res.errors, "N-10")) == 2

    def test_counts_as_predicates_ok(self):
        res = check('counts_as:\n  - when: "R[i].speed == 0 AND state == Open"\n'
                    '    creates: stopped\n    if: "D.mode IN [a, b]"\n', LIFECYCLE)
        assert res.ok and not res.warnings, res.errors + res.warnings

    @pytest.mark.parametrize("block", [
        "  transitions:\n    - {from: A, to: B, condition: \"within(5s, x)\"}\n",
        "  transitions:\n    - {from: A, to: B, condition: x, safety_invariant: \"throughout(1s, y)\"}\n"
        "    - {from: B, to: A, condition: NOT x}\n",
        "  metrics:\n    - {id: m, formula: \"until(a, b, 5s)\"}\n",
        "  verification:\n    - {id: v, type: safety, expr: \"within(5s, x)\"}\n",
        "  protocols:\n    - id: P\n      trigger: go\n      precondition: \"within(5s, x)\"\n",
        "  protocols:\n    - id: P\n      trigger: go\n      steps:\n"
        "        - barrier: \"throughout(5s, ready)\"\n",
    ])
    def test_n10_forbidden_outside_contracts(self, block):
        res = type_check(parse(source() + block))
        assert any("allows only" in e for e in with_rule(res.errors, "N-10"))

    def test_n10_outside_contracts_without_declaration(self):
        block = "  metrics:\n    - {id: m, formula: \"until(a, b, 5s)\"}\n"
        assert type_check(parse(source(declare=None) + block)).ok

    def test_n10_forbidden_in_monitor_rule(self):
        res = check("monitors:\n  - id: m\n    observe: time\n"
                    '    rule: "within(5s, R[i].battery < 20)"\n')
        assert any("allows only" in e for e in with_rule(res.errors, "N-10"))

    @pytest.mark.parametrize("where", ["on", "when"])
    def test_n10_forbidden_in_lifecycle(self, where):
        tr = '{ id: t, from: Open, to: Failed, on: "late == true" }'
        if where == "on":
            tr = '{ id: t, from: Open, to: Failed, on: "throughout(5s, R[i].stopped)" }'
        else:
            tr = ('{ id: t, from: Open, to: Failed, on: "late == true", '
                  'when: "until(a, b, 1s)" }')
        res = check(LIFECYCLE + f"    - {tr}\n")
        assert any("allows only" in e for e in with_rule(res.errors, "N-10"))

    def test_n10_message_event_on_is_not_checked(self):
        res = check(LIFECYCLE + '    - { id: t, from: Open, to: Failed, '
                                'on: "D -> R[i] : until(late)" }\n')
        assert res.ok

    # N-11
    @pytest.mark.parametrize("target", ['"not-an-id"', "AND", "null"])
    def test_n11_transform_identifier(self, target):
        res = check(norms(norm(id="a", kind="obligation", bearer="D", content="x",
                               on_amendment=f"{{transform: {target}}}")))
        assert with_rule(res.errors, "N-11")

    @pytest.mark.parametrize("value", ["carry_over", "discharge", "{transform: a2}"])
    def test_n11_ok(self, value):
        res = check(norms(norm(id="a", kind="obligation", bearer="D", content="x",
                               on_amendment=value)))
        assert res.ok, res.errors

    @pytest.mark.parametrize("value", ["keep", "{transform: a, x: b}", "[discharge]"])
    def test_on_amendment_other_value(self, value):
        res = check(norms(norm(id="a", kind="obligation", bearer="D", content="x",
                               on_amendment=value)))
        assert with_rule(res.errors, "F.3")

    # N-12
    def test_n12_permission_and_prohibition_same_content(self):
        res = check(norms(
            norm(id="p", kind="permission", bearer='"R[i]"', content='"R[i] -> D : m"'),
            norm(id="q", kind="prohibition", bearer='"R[i]"', content='"R[i] -> D : m"')))
        assert res.ok
        assert with_rule(res.warnings, "N-12")

    @pytest.mark.parametrize("bearer2, content2", [
        ("D", '"R[i] -> D : m"'),
        ('"R[i]"', '"R[i] -> D : other"'),
    ])
    def test_n12_differs(self, bearer2, content2):
        res = check(norms(
            norm(id="p", kind="permission", bearer='"R[i]"', content='"R[i] -> D : m"'),
            norm(id="q", kind="prohibition", bearer=bearer2, content=content2)))
        assert not with_rule(res.warnings, "N-12")

    # F.3: required keys
    def test_missing_content(self):
        res = check(norms(norm(id="a", kind="permission", bearer="D")))
        assert with_rule(res.errors, "F.3")

    def test_missing_when(self):
        res = check("counts_as:\n  - creates: e\n")
        assert with_rule(res.errors, "F.3")


# --- Verifier (F.7) --------------------------------------------------------

class TestVerifier:
    BODY = ('assume:\n  - "throughout(300s, D.up == true)"\n  - "x > 0"\n'
            'guarantee:\n  - "within(300s, y == 1)"\n  - "x < 0"\n')

    def test_temporal_items_left_out_and_reported(self):
        results = verify(parse(source(self.BODY)))
        skipped = [r for r in results if r.status == "not_supported"]
        assert len(skipped) == 2
        assert {r.check_name for r in skipped} == {
            "Contract 'C' assume", "Contract 'C' guarantee"}
        assert any("throughout(300s" in r.message for r in skipped)
        # The remaining items x > 0 and x < 0 still contradict each other.
        cons = next(r for r in results if r.check_name == "Contract 'C' consistency")
        assert cons.status == "failed"

    def test_only_temporal_items_pass(self):
        results = verify(parse(source(
            'assume:\n  - "throughout(5s, NOT p)"\nguarantee:\n  - "within(5s, p)"\n')))
        assert not [r for r in results if r.status == "failed"]
        assert len([r for r in results if r.status == "not_supported"]) == 2

    def test_unparsed_temporal_item_left_out(self):
        results = verify(parse(source('guarantee:\n  - "within(1.5s, p)"\n')))
        assert [r for r in results if r.status == "not_supported"]

    def test_without_declaration_nothing_changes(self):
        sos = parse(source(self.BODY, declare=None))
        results = verify(sos)
        assert not [r for r in results if r.status == "not_supported"]
        assert [r.status for r in results] == [
            r.status for r in verify(parse(source(self.BODY, declare="sos-dsl: 0.1")))]

    def test_contract_left_unchanged(self):
        sos = parse(source(self.BODY))
        verify(sos)
        assert len(sos.contracts[0].assume) == 2


# --- IR (F.9) --------------------------------------------------------------

class TestIR:
    def test_norm_ir_shape(self):
        sos = parse(source(LIFECYCLE, COUNTS_AS + '    if: "D.ready"\n', norms(OBLIGATION + """
  on_violation: { reparation: rep }
""", """
- id: rep
  kind: prohibition
  bearer: "R[i]"
  content: "R[i].speed > 3"
  on_violation: { severity: Minor, sanction: "fine", transition: Failed }
  on_amendment: { transform: rep2 }
""", """
- id: pw
  kind: power
  bearer: D
  until: "D.closed"
  content: reassignment
  effects: { activate: [rep], suspend: [ack], resume: [ack], discharge: [ack, rep] }
""")))
        c = _ir_to_dict(lower_to_ir(sos))["institution"]["contracts"][0]
        ack, rep, pw = c["norms"]
        assert list(ack) == [
            "id", "kind", "bearer", "counterparty", "trigger", "until", "content",
            "deadline_ms", "on_violation_severity", "on_violation_sanction",
            "on_violation_reparation", "on_violation_transition",
            "effects_activate", "effects_suspend", "effects_resume",
            "effects_discharge", "on_amendment", "transform_to",
        ]
        assert ack["deadline_ms"] == 5000
        assert ack["on_violation_severity"] == "Major"  # block without severity
        assert ack["on_violation_reparation"] == "rep"
        assert ack["on_amendment"] == "carry_over" and ack["transform_to"] is None
        assert rep["on_violation_severity"] == "Minor"
        assert rep["on_violation_sanction"] == "fine"
        assert rep["on_violation_transition"] == "Failed"
        assert rep["on_amendment"] == "transform" and rep["transform_to"] == "rep2"
        assert pw["on_violation_severity"] is None  # no block
        assert pw["until"] == "D.closed"
        assert pw["effects_activate"] == ["rep"]
        assert pw["effects_suspend"] == ["ack"] and pw["effects_resume"] == ["ack"]
        assert pw["effects_discharge"] == ["ack", "rep"]
        assert c["counts_as"] == [
            {"when": "D -> R[i] : replan", "creates": "reassignment", "if": "D.ready"}
        ]
        json.dumps(c)

    def test_undeclared_blocks_not_lowered(self):
        sos = parse(source(COUNTS_AS, norms(OBLIGATION), declare=None))
        c = _ir_to_dict(lower_to_ir(sos))["institution"]["contracts"][0]
        assert c["norms"] == [] and c["counts_as"] == []

    def test_empty_lists_without_norms(self):
        c = _ir_to_dict(lower_to_ir(parse(source())))["institution"]["contracts"][0]
        assert c["norms"] == [] and c["counts_as"] == []

    def test_example_ir_matches_f9(self):
        c = _ir_to_dict(lower_to_ir(parse_file(EXAMPLE)))["institution"]["contracts"][0]
        assert c["norms"][0] == {
            "id": "acknowledge",
            "kind": "obligation",
            "bearer": "ROBOT[i]",
            "counterparty": "DISPATCHER",
            "trigger": "DISPATCHER -> ROBOT[i] : route_assignment",
            "until": None,
            "content": "ROBOT[i] -> DISPATCHER : ack(accepted)",
            "deadline_ms": 5000,
            "on_violation_severity": "Major",
            "on_violation_sanction": None,
            "on_violation_reparation": "report_failure",
            "on_violation_transition": None,
            "effects_activate": [],
            "effects_suspend": [],
            "effects_resume": [],
            "effects_discharge": [],
            "on_amendment": "carry_over",
            "transform_to": None,
        }
        assert c["counts_as"] == [{
            "when": "DISPATCHER -> ROBOT[i] : replan_command",
            "creates": "reassignment",
            "if": None,
        }]
        assert [n["id"] for n in c["norms"]] == [
            "acknowledge", "report_failure", "no_low_battery_driving",
            "may_return_to_charge", "reassign"]


# --- The example of F.8 ----------------------------------------------------

class TestExample:
    def test_check_without_diagnostic(self):
        res = type_check(parse_file(EXAMPLE))
        assert res.ok and not res.warnings and not res.infos, (
            res.errors + res.warnings + res.infos)

    def test_verify_passes_and_skips_temporal_items(self):
        results = verify(parse_file(EXAMPLE))
        assert not [r for r in results if r.status == "failed"]
        assert len([r for r in results if r.status == "not_supported"]) == 2

    def test_temporal_calls_parse_as_function_calls(self):
        c = parse_file(EXAMPLE).contracts[0]
        assert isinstance(c.assume[0], FunctionCall) and c.assume[0].name == "throughout"
        assert isinstance(c.guarantee[0], FunctionCall) and c.guarantee[0].name == "within"

    @pytest.mark.parametrize("cmd", [["check"], ["verify"], ["sim-ir", "--format", "json"]])
    def test_cli(self, cmd):
        proc = subprocess.run(
            [sys.executable, "-m", "cadl.cli", cmd[0], str(EXAMPLE), *cmd[1:]],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        if cmd[0] == "sim-ir":
            c = json.loads(proc.stdout)["institution"]["contracts"][0]
            assert len(c["norms"]) == 5 and len(c["counts_as"]) == 1
            assert "if" in c["counts_as"][0] and "if_" not in c["counts_as"][0]
        if cmd[0] == "verify":
            assert "[SKIP]" in proc.stdout


# --- Code generation and verify messages ------------------------------------

TARGETS = ["python", "solidity", "opa", "unity-csharp"]
_TEMPORAL_CALL = __import__("re").compile(r"(?<![\w.])(within|throughout|until)\s*\(")


def _generated_text(out: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(out.rglob("*")) if p.is_file())


class TestCodegen:
    @pytest.mark.parametrize("target", TARGETS)
    def test_no_temporal_calls_generated(self, target, tmp_path):
        from cadl.codegen import generate
        generate(parse_file(EXAMPLE), tmp_path, target=target)
        text = _generated_text(tmp_path)
        # Only the comments that record the left-out items mention them.
        code_lines = [l for l in text.splitlines()
                      if _TEMPORAL_CALL.search(l) and "not generated" not in l]
        assert not code_lines, code_lines
        if target != "unity-csharp":
            assert text.count("not generated: the item has a temporal operator "
                              "(norms 0.1)") == 2

    @pytest.mark.parametrize("target", ["python", "solidity", "opa"])
    def test_without_declaration_nothing_changes(self, target, tmp_path):
        from cadl.codegen import generate
        src = EXAMPLE.read_text(encoding="utf-8").replace("    - norms: 0.1\n", "")
        generate(parse(src), tmp_path, target=target)
        text = _generated_text(tmp_path)
        assert "not generated" not in text
        assert _TEMPORAL_CALL.search(text)

    def test_rego_rule_without_items_is_true(self, tmp_path):
        from cadl.codegen import generate
        generate(parse_file(EXAMPLE), tmp_path, target="opa")
        rego = _generated_text(tmp_path)
        body = rego.split("assumptions_hold if {", 1)[1].split("}", 1)[0]
        assert "\n    true\n" in body

    @pytest.mark.parametrize("target", TARGETS)
    def test_cli_warns_per_item(self, target, tmp_path):
        proc = subprocess.run(
            [sys.executable, "-m", "cadl.cli", "codegen", str(EXAMPLE),
             "-t", target, "-o", str(tmp_path / "out")],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        warnings = [l for l in proc.stderr.splitlines()
                    if "[WARNING]" in l and "temporal operator" in l]
        assert len(warnings) == 2
        assert any("assume item of contract 'DELIVERY_SLA'" in w for w in warnings)
        assert any("guarantee item" in w and "within(300s" in w for w in warnings)


class TestVerifyMessages:
    def test_all_assumptions_left_out(self):
        results = verify(parse_file(EXAMPLE))
        msgs = {r.check_name: r.message for r in results}
        assert "temporal operators were left out" in msgs["Contract 'DELIVERY_SLA' assumptions"]
        assert "unconditionally" not in msgs["Contract 'DELIVERY_SLA' assumptions"]
        assert "temporal operators were left out" in msgs["Contract 'DELIVERY_SLA' entailment"]

    def test_plain_contract_keeps_messages(self):
        results = verify(parse(source()))
        msgs = {r.check_name: r.message for r in results}
        assert "unconditionally" in msgs["Contract 'C' assumptions"]
        assert msgs["Contract 'C' entailment"] == "No assumes or guarantees to check entailment"


def test_n12_message_says_conflict():
    res = check(norms(
        norm(id="p", kind="permission", bearer="D", content="x"),
        norm(id="q", kind="prohibition", bearer="D", content="x")))
    assert any("the two conflict" in w for w in with_rule(res.warnings, "N-12"))
