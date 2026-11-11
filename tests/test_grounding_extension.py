"""Grounding extension (Appendix J, grounding 0.1), the norms side: the new
keys, the derived indices of J.4 with their counts, the rules J-1 to J-9
and J-13 of J.8, the IR of J.10, `cadl norms-indices`, the comparison of
two versions in `cadl norms-diff`, the function a payoff term of a
strategic model calls (J-10), and the invariants of J.12."""

from __future__ import annotations

import copy
import inspect
import json
import random
import subprocess
import sys
import textwrap
from fractions import Fraction as F
from pathlib import Path

import pytest
import yaml

from cadl import grounding as G
from cadl.cli import _ir_to_dict, main
from cadl.norms_diff import diff_versions, format_diff_text
from cadl.norms_eval import evaluate, parse_trace
from cadl.parser import parse, parse_file
from cadl.sim import lower_to_ir
from cadl.type_checker import type_check

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
TRACES = EXAMPLES / "traces"
EXAMPLE = EXAMPLES / "grounding_robot_delivery.cadl"
DECL = ("norms: 0.1", "grounding: 0.1")


def source(*blocks: str, declare=DECL, parties: str = '[D, "R[*]", X]',
           actors: str = "") -> str:
    """A file with actors D, R[1..3], X (and `actors`) and a contract C
    whose further keys are the given blocks, each written flush left."""
    ext = "  extensions:\n" + "".join(f"    - {d}\n" for d in declare) if declare else ""
    head = (
        "sos:\n  name: T\n" + ext +
        "  actors:\n"
        "    - {id: D, role: d}\n"
        "    - {id: \"R[1..3]\", role: r}\n"
        "    - {id: X, role: x}\n" + actors +
        "  contracts:\n"
        "    - id: C\n"
        f"      parties: {parties}\n"
    )
    return head + "".join(textwrap.indent(textwrap.dedent(b), "      ") for b in blocks)


def check(*blocks: str, **kw):
    return type_check(parse(source(*blocks, **kw)))


def rule(items, name: str) -> list:
    return [str(i) for i in items if f"({name})" in str(i)]


def indices(*blocks: str, **kw) -> G.ContractIndices:
    sos = parse(source(*blocks, **kw))
    return G.contract_indices(sos, sos.contracts[0])


ITEMS = """
grounding:
  items:
    route: D
    status: "R[*]"
"""

SHARE_NORM = """
- id: tell
  kind: {kind}
  bearer: "R[i]"
  content: "R[i] -> {to} : report"
  shares: [status]
"""


def norms(*entries: str) -> str:
    return "norms:\n" + "".join(
        textwrap.indent(textwrap.dedent(e).strip("\n") + "\n", "  ") for e in entries)


def power(pid: str, bearer: str, effects: str = "") -> str:
    return (f'- id: {pid}\n  kind: power\n  bearer: "{bearer}"\n'
            f'  content: "{bearer} -> D : {pid}_msg"\n'
            + (f"  effects: {effects}\n" if effects else ""))


def duty(nid: str, kind: str = "obligation", ov: str = "", ful: str = "") -> str:
    content = ('"R[i] -> D : ' + nid + '_msg"' if kind != "prohibition"
               else '"R[i].speed > 5"')
    return (f'- id: {nid}\n  kind: {kind}\n  bearer: "R[i]"\n  content: {content}\n'
            + ("  deadline: 5s\n" if kind == "obligation" else "")
            + (f"  on_violation: {ov}\n" if ov else "")
            + (f"  on_fulfilment: {ful}\n" if ful else ""))


# --- Declaration, undeclared use (J-1, J.12) -----------------------------------

class TestDeclaration:
    def test_keys_are_read_apart_from_the_norms(self):
        sos = parse_file(EXAMPLE)
        assert G.keys_used(sos) == ["grounding:", "shares:", "on_violation.magnitude"]
        raw = sos.grounding.contracts[0]
        assert raw.block["items"]["route"] == "DISPATCHER"
        assert [n for n in raw.norms if n] == [
            {"shares": ["failure"]}, {"magnitude": 4}, {"shares": ["battery"]}]
        # the AST of the norms does not carry the keys
        assert not hasattr(sos.contracts[0].norms[0], "shares")

    def test_file_without_keys_has_no_raw_data(self):
        assert parse_file(EXAMPLES / "norms_robot_delivery.cadl").grounding is None

    def test_null_values_count_as_absent(self):
        sos = parse(source("grounding:\n", norms(
            duty("a", ov="{ sanction: null, magnitude: null }") + "  shares:\n"
            "  on_fulfilment:\n")))
        assert sos.grounding is None
        assert type_check(sos).ok

    def test_j1_grounding_without_norms(self):
        r = check(ITEMS, declare=("grounding: 0.1",))
        assert len(rule(r.errors, "J-1")) == 1
        # the keys are not interpreted: a bad value gives no further error
        r = check("grounding: { tolerance: 7 }\n", declare=("grounding: 0.1",))
        assert len(r.errors) == 1 and rule(r.errors, "J-1")
        sos = parse(source(ITEMS, declare=("grounding: 0.1",)))
        assert G.file_indices(sos) == []
        with pytest.raises(G.GroundingError, match="J-1"):
            G.report(sos)

    def test_undeclared_use_is_one_warning_and_nothing_is_interpreted(self):
        src = source("grounding: { items: { 3: NOBODY }, tolerance: 7 }\n", norms(
            duty("a", ov="{ magnitude: -1 }", ful="{ reward: x }") + "  shares: [zzz]\n"),
            declare=("norms: 0.1",))
        r = type_check(parse(src))
        assert r.ok
        w = [str(x) for x in r.warnings if "J.12" in str(x)]
        assert len(w) == 1
        for key in ("grounding:", "shares:", "on_fulfilment:", "on_violation.magnitude"):
            assert key in w[0]
        sos = parse(src)
        assert G.file_indices(sos) == []
        d = _ir_to_dict(lower_to_ir(sos))
        c = d["institution"]["contracts"][0]
        assert "grounding" not in c and "shares" not in c["norms"][0]
        with pytest.raises(G.GroundingError, match="does not declare"):
            G.report(sos)

    def test_declared_is_known_and_other_version_is_not_interpreted(self):
        r = check(ITEMS)
        assert not [i for i in r.infos if "grounding" in str(i)]
        r = check("grounding: { tolerance: 7 }\n", declare=("norms: 0.1", "grounding: 0.2"))
        assert r.ok and [i for i in r.infos if "'grounding' is declared with version" in str(i)]
        assert [w for w in r.warnings if "J.12" in str(w)]


# --- J-2, J-3, J-4: the new keys ---------------------------------------------------

class TestKeys:
    def test_j2_valid(self):
        r = check("grounding:\n  items: { route: D, status: \"R[*]\", one: \"R[2]\", "
                  "v: \"R[i]\", r: \"R[1..3]\" }\n  tolerance: 0.1\n")
        assert r.ok and not rule(r.warnings, "J-2")

    @pytest.mark.parametrize("block, text", [
        ("grounding: 5\n", "not a mapping"),
        ("grounding: { items: [a, b] }\n", "not a mapping from item names"),
        ("grounding: { items: { \"a-b\": D } }\n", "is not an identifier"),
        ("grounding: { items: { 3: D } }\n", "is not an identifier"),
        ("grounding: { items: { in: D } }\n", "is a reserved word"),
        ("grounding: { items: { on: D } }\n", "not as a name"),
        ("grounding: { items: { a: NOBODY } }\n", "not a party"),
        ("grounding: { items: { a: 5 } }\n", "not an actor reference"),
        ("grounding: { items: { a: \"D -> X\" } }\n", "not an actor reference"),
        ("grounding: { tolerance: 7 }\n", "[0, 1]"),
        ("grounding: { tolerance: -0.1 }\n", "[0, 1]"),
        ("grounding: { tolerance: \"0.1\" }\n", "[0, 1]"),
        ("grounding: { tolerance: true }\n", "[0, 1]"),
        ("grounding: { tolerance: .nan }\n", "[0, 1]"),
    ])
    def test_j2_errors(self, block, text):
        e = rule(check(block).errors, "J-2")
        assert len(e) == 1 and text in e[0]

    def test_j2_origin_must_be_a_party_not_only_an_actor(self):
        assert rule(check("grounding: { items: { a: X } }\n", parties='[D, "R[*]"]').errors,
                    "J-2")

    def test_j2_invalid_values_are_left_out_of_the_indices(self):
        ci = indices("grounding:\n  items: { ok: D, bad: NOBODY }\n  tolerance: 7\n")
        assert list(ci.ann.items) == ["ok"] and ci.tolerance == F(1, 4)

    def test_unknown_key_of_the_block_is_a_warning(self):
        r = check("grounding: { tolerence: 0.5 }\n")
        assert r.ok and "tolerence" in rule(r.warnings, "J.3")[0]

    def test_j3_valid(self):
        for kind in ("obligation", "permission"):
            n = SHARE_NORM.format(kind=kind, to="D") + ("  deadline: 5s\n" if kind == "obligation" else "")
            r = check(ITEMS, norms(n))
            assert r.ok and not rule(r.warnings, "J-3"), [str(x) for x in r.errors]

    @pytest.mark.parametrize("norm, text", [
        ('- {id: p, kind: prohibition, bearer: "R[i]", content: "R[i] -> D : m", shares: [status]}',
         "only for an obligation or a permission"),
        ('- {id: p, kind: power, bearer: D, content: "D -> R[i] : m", shares: [route]}',
         "only for an obligation or a permission"),
        ('- {id: p, kind: permission, bearer: "R[i]", content: "R[i].speed > 1", shares: [status]}',
         "content is a message event"),
        ('- {id: p, kind: permission, bearer: "R[i]", content: "R[i] -> D : m", shares: []}',
         "at least one"),
        ('- {id: p, kind: permission, bearer: "R[i]", content: "R[i] -> D : m", shares: status}',
         "at least one"),
        ('- {id: p, kind: permission, bearer: "R[i]", content: "R[i] -> D : m", shares: [nope]}',
         "does not declare"),
        ('- {id: p, kind: permission, bearer: "R[i]", content: "R[i] -> D : m", shares: [status, status]}',
         "more than once"),
        ('- {id: p, kind: permission, bearer: "R[i]", content: "R[i] -> D : m", shares: [3]}',
         "not the name"),
        ('- {id: p, kind: permission, bearer: "R[i]", content: "R[i] -> D : m", shares: [on]}',
         "YAML 1.1"),
    ])
    def test_j3_errors(self, norm, text):
        e = rule(check(ITEMS, norms(norm)).errors, "J-3")
        assert len(e) == 1 and text in e[0], e

    def test_j3_content_that_is_an_institutional_event_is_not_a_message(self):
        r = check(ITEMS, 'counts_as:\n  - { when: "R[i] -> D : m", creates: told }\n', norms(
            '- {id: p, kind: permission, bearer: "R[i]", content: told, shares: [status]}'))
        assert rule(r.errors, "J-3")

    def test_j3_warnings(self):
        r = check(ITEMS, norms(SHARE_NORM.format(kind="permission", to="R[j]")))
        w = rule(r.warnings, "J-3")
        assert r.ok and len(w) == 1 and "the origin of the item" in w[0]
        r = check(ITEMS, norms(SHARE_NORM.format(kind="permission", to="Y")),
                  actors="    - {id: Y, role: y}\n")
        w = rule(r.warnings, "J-3")
        assert len(w) == 1 and "not a party" in w[0]
        r = check(ITEMS, 'information:\n  sharing:\n    - "D -> Y : route"\n'
                         '    - "D -> X : other"\n    - "D -> R : route"\n',
                  actors="    - {id: Y, role: y}\n")
        w = rule(r.warnings, "J-3")
        assert r.ok and len(w) == 3
        assert "'Y', which is not a party" in w[0]
        assert "matches no member" in w[1] and "'R'" in w[1]
        assert "'other', which grounding.items does not declare" in w[2]

    def test_j3_no_warning_about_sharing_entries_without_items(self):
        r = check('information:\n  sharing:\n    - "D -> Y : route"\n',
                  actors="    - {id: Y, role: y}\n")
        assert r.ok and not rule(r.warnings, "J-3")

    def test_j4_valid(self):
        r = check(norms(
            duty("a", ov="{ sanction: fine, magnitude: 2.5 }", ful="{ reward: bonus, magnitude: 1 }"),
            duty("b", "prohibition", ov="{ magnitude: 0 }", ful="{ magnitude: 3 }"),
            duty("c", ful="{ reward: thanks }")))
        assert r.ok and not rule(r.warnings, "J-4"), [str(x) for x in r.errors]

    @pytest.mark.parametrize("value", ["-1", ".inf", "-.inf", ".nan", '"4"', "true", "[1]", "abc"])
    def test_j4_magnitude_errors(self, value):
        e = rule(check(norms(duty("a", ov="{ magnitude: %s }" % value))).errors, "J-4")
        assert len(e) == 1 and "on_violation.magnitude" in e[0]
        e = rule(check(norms(duty("a", ful="{ magnitude: %s }" % value))).errors, "J-4")
        assert len(e) == 1 and "on_fulfilment.magnitude" in e[0]

    @pytest.mark.parametrize("kind", ["permission", "power"])
    def test_j4_on_fulfilment_only_for_obligation_or_prohibition(self, kind):
        n = (f'- {{id: p, kind: {kind}, bearer: D, content: "D -> R[i] : m", '
             f'on_fulfilment: {{ reward: x }} }}')
        e = rule(check(norms(n)).errors, "J-4")
        assert len(e) == 1 and "only an obligation or a prohibition" in e[0]

    @pytest.mark.parametrize("ful, text", [
        ("{}", "at least one"), ('{ reward: "" }', "at least one"),
        ("{ reward: null }", "at least one"), ("5", "not a mapping"),
        ("[a]", "not a mapping"),
    ])
    def test_j4_on_fulfilment_errors(self, ful, text):
        e = rule(check(norms(duty("a", ful=ful))).errors, "J-4")
        assert len(e) == 1 and text in e[0]

    def test_j4_zero_magnitude_next_to_a_text_is_a_warning(self):
        r = check(norms(duty("a", ov="{ sanction: fine, magnitude: 0 }",
                             ful="{ reward: bonus, magnitude: 0 }")))
        assert r.ok and len(rule(r.warnings, "J-4")) == 2
        r = check(norms(duty("a", ov='{ sanction: "", magnitude: 0 }')))
        assert r.ok and not rule(r.warnings, "J-4")

    def test_unknown_key_of_on_fulfilment_is_a_warning(self):
        r = check(norms(duty("a", ful="{ reward: x, magnitud: 3 }")))
        assert r.ok and "magnitud" in rule(r.warnings, "J.3")[0]


# --- J.4: alpha^ ----------------------------------------------------------------------

class TestAlpha:
    def test_example(self):
        sos = parse_file(EXAMPLE)
        a = G.contract_indices(sos, sos.contracts[0]).alpha
        assert a.value == F(5, 8)
        assert (len(a.by("sharing")), len(a.by("obligation")), len(a.by("permission"))) == (3, 1, 1)
        assert a.not_given == [("route", "CUSTOMER"), ("battery", "CUSTOMER"),
                               ("failure", "CUSTOMER")]
        assert a.member_specific == []

    def test_example_member_specific_variant_of_j9(self):
        text = EXAMPLE.read_text().replace('"DISPATCHER -> ROBOT[*] : route"',
                                           '"DISPATCHER -> ROBOT[3] : route"')
        sos = parse(text)
        a = G.contract_indices(sos, sos.contracts[0]).alpha
        assert a.value == F(4, 8)
        assert a.member_specific == ["DISPATCHER -> ROBOT[3] : route"]

    def test_undefined_without_items_and_with_a_single_party_name(self):
        a = indices('information:\n  sharing:\n    - "D -> R[*] : route"\n').alpha
        assert a.value is None and "no information item" in a.undefined_reason
        a = indices('grounding: { items: { status: "R[*]" } }\n', parties='["R[*]"]').alpha
        assert a.value is None and "single party name" in a.undefined_reason
        a = indices('grounding: { items: { status: "R[1]" } }\n', parties='["R[1]", "R[2]"]').alpha
        assert a.value is None

    def test_zero_and_one(self):
        a = indices(ITEMS).alpha
        assert a.value == 0 and len(a.pairs) == 4
        a = indices(ITEMS, 'information:\n  sharing:\n    - "D -> R[*] : route"\n'
                    '    - "D -> X : route"\n    - "R[*] -> D : status"\n'
                    '    - "R[*] -> X : status"\n').alpha
        assert a.value == 1 and a.not_given == []

    def test_origin_is_excluded_and_the_source_is_not_examined(self):
        a = indices(ITEMS, 'information:\n  sharing:\n    - "X -> D : route"\n'
                    '    - "X -> R[*] : route"\n').alpha
        assert a.given_pairs == [("route", "R")]      # D is the origin of route

    def test_three_ways_overlap_and_a_pair_counts_once(self):
        ci = indices(ITEMS, 'information:\n  sharing:\n    - "R[*] -> D : status"\n', norms(
            SHARE_NORM.format(kind="obligation", to="D") + "  deadline: 5s\n",
            SHARE_NORM.format(kind="permission", to="D").replace("tell", "tell2")))
        a = ci.alpha
        assert a.value == F(1, 4)
        assert (len(a.by("sharing")), len(a.by("obligation")), len(a.by("permission"))) == (1, 1, 1)
        assert [w for w, _ in a.given[("status", "D")]] == ["sharing", "obligation", "permission"]

    def test_state_of_the_norm_is_ignored_and_a_prohibition_removes_nothing(self):
        n = SHARE_NORM.format(kind="permission", to="D") + '  trigger: "D -> R[i] : ask"\n'
        p = '- {id: no, kind: prohibition, bearer: "R[i]", content: "R[i] -> D : report"}'
        assert indices(ITEMS, norms(n, p)).alpha.given_pairs == [("status", "D")]

    @pytest.mark.parametrize("target, cls", [
        ("R[*]", "whole"), ("R[i]", "whole"), ("R[winner]", "whole"), ("R[1..3]", "whole"),
        ("R[1..N]", "whole"), ("D", "whole"), ("D[*]", "whole"),
        ("R[2]", "member"), ("R[1..2]", "member"), ("R[2..3]", "member"),
        ("R", "bare_set"), ("Y", "non_party"),
    ])
    def test_target_classes(self, target, cls):
        sos = parse(source(ITEMS, actors="    - {id: Y, role: y}\n"))
        got = G.target_class(G._ref(target), sos.contracts[0], G._declared_actors(sos))
        assert got == cls

    def test_integer_target_is_whole_only_when_equal_to_the_only_party_entry(self):
        def cls(parties, target):
            sos = parse(source(parties=parties))
            return G.target_class(G._ref(target), sos.contracts[0], G._declared_actors(sos))
        assert cls('[D, "R[2]"]', "R[2]") == "whole"
        assert cls('[D, "R[2]"]', "R[1]") == "member"
        assert cls('[D, "R[1]", "R[2]"]', "R[2]") == "member"
        assert cls('[D, "R[*]"]', "R[2]") == "member"

    def test_member_specific_and_bare_set_give_no_pair(self):
        blocks = (ITEMS, 'information:\n  sharing:\n    - "D -> R[3] : route"\n'
                  '    - "D -> R : route"\n', norms(
                      SHARE_NORM.format(kind="permission", to="D")))
        a = indices(*blocks).alpha
        assert a.given_pairs == [("status", "D")]
        assert a.member_specific == ["D -> R[3] : route"]
        assert a.bare_set == [("D -> R : route", "R")]

    def test_member_specific_counts_entries_and_norms_not_pairs(self):
        n = ('- {id: tell, kind: permission, bearer: D, content: "D -> R[2] : m", '
             'shares: [route, plan]}')
        a = indices("grounding: { items: { route: D, plan: D } }\n", norms(n)).alpha
        assert a.member_specific == ["tell"] and a.value == 0

    def test_non_party_target_gives_no_pair(self):
        a = indices(ITEMS, 'information:\n  sharing:\n    - "D -> Y : route"\n',
                    actors="    - {id: Y, role: y}\n").alpha
        assert a.value == 0 and a.non_party == [("D -> Y : route", "Y")]

    def test_white_space_in_a_receiver_is_ignored(self):
        n = SHARE_NORM.format(kind="permission", to="D")
        a = indices('grounding: { items: { status: " R[ * ] " } }\n', norms(n)).alpha
        assert a.given_pairs == [("status", "D")]


# --- J.4: beta^ ------------------------------------------------------------------------

class TestBeta:
    def test_example(self):
        sos = parse_file(EXAMPLE)
        b = G.contract_indices(sos, sos.contracts[0]).beta
        assert b.value == F(1, 2) and b.largest == 1 and b.top == "DISPATCHER"
        assert b.bearers == {"DISPATCHER": 1, "ROBOT[i]": 1}
        assert b.holder_count == 1 and b.holder_share == F(1, 2)
        assert [(p[0], p[3]) for p in b.powers] == [("reassign", 2), ("decline", 1)]

    def test_no_power_is_undefined(self):
        b = indices(norms(duty("a"))).beta
        assert b.value is None and b.undefined_reason == "the contract has no power"

    def test_only_set_borne_powers_give_zero(self):
        b = indices(norms(power("p", "R[i]"), power("q", "R[*]"))).beta
        assert b.value == 0 and b.top is None and b.singles == {}
        assert b.bearers == {"R[i]": 1, "R[*]": 1}

    def test_single_actors_are_integer_indices_and_actors_without_a_range(self):
        b = indices(norms(power("p", "R[2]"), power("q", "R[2]"), power("r", "D"),
                          power("s", "R[i]"), power("t", "R[3]"))).beta
        assert b.singles == {"R[2]": 2, "D": 1, "R[3]": 1}
        assert b.value == F(2, 5) and b.top == "R[2]"

    def test_bare_name_of_a_set_is_not_a_single_actor(self):
        b = indices(norms(power("p", "R"))).beta
        assert b.value == 0

    def test_one_actor_bears_all(self):
        assert indices(norms(power("p", "D"), power("q", "D"))).beta.value == 1

    def test_does_not_depend_on_the_decision_holder(self):
        base = norms(power("p", "D"), power("q", "X"), power("r", "X"))
        values = set()
        for auth in ("", "authority: { decision_holder: D }\n",
                     "authority: { decision_holder: X }\n",
                     'authority: { decision_holder: "R[*]" }\n'):
            values.add(indices(auth, base).beta.value)
        assert values == {F(2, 3)}

    def test_holder(self):
        base = norms(power("p", "D"), power("q", "X"), power("r", "X"))
        assert indices(base).beta.holder_count is None                 # absent
        b = indices('authority: { decision_holder: "R[*]" }\n', base).beta
        assert b.holder == "R[*]" and b.holder_count is None           # a set
        b = indices("authority: { decision_holder: R }\n", base).beta
        assert b.holder_count is None                                  # name of a set
        b = indices("authority: { decision_holder: D }\n", base).beta
        assert b.holder_count == 1 and b.holder_share == F(1, 3)
        b = indices('authority: { decision_holder: "R[1]" }\n', base).beta
        assert b.holder_count == 0 and b.holder_share == 0
        b = indices("authority: { decision_holder: D }\n").beta
        assert b.holder_count == 0 and b.holder_share is None          # no power

    def test_effects_count_each_named_norm_once(self):
        b = indices(norms(duty("a"), duty("b"), power(
            "p", "D", "{ suspend: [a, b], resume: [a], discharge: [b] }"),
            power("q", "D"))).beta
        assert [(p[0], p[3]) for p in b.powers] == [("p", 2), ("q", 0)]


# --- J.4: chi --------------------------------------------------------------------------

class TestCoverage:
    def test_example(self):
        sos = parse_file(EXAMPLE)
        c = G.contract_indices(sos, sos.contracts[0]).coverage
        assert c.value == F(1, 3) and c.incentive == ["no_low_battery_driving"]
        assert c.reparation_only == ["acknowledge"] and c.transition == ["report_failure"]

    def test_undefined_without_obligation_and_prohibition(self):
        c = indices(norms(power("p", "D"), SHARE_NORM.format(kind="permission", to="D")),
                    ITEMS).coverage
        assert c.value is None and "no obligation" in c.undefined_reason

    @pytest.mark.parametrize("ov, ful, carries", [
        ("", "", False),
        ("{ severity: Critical }", "", False),
        ("{ sanction: fine }", "", True),
        ('{ sanction: "" }', "", False),
        ("{ sanction: null }", "", False),
        ("{ magnitude: 0 }", "", False),
        ("{ magnitude: 0.5 }", "", True),
        ("{ sanction: fine, magnitude: 0 }", "", True),
        ('{ sanction: "", magnitude: 0 }', "", False),
        ("", "{ reward: bonus }", True),
        ("", "{ magnitude: 2 }", True),
        ("", "{ magnitude: 0 }", False),
        ("", '{ reward: "", magnitude: 0 }', False),
        ("{ magnitude: -3 }", "", False),            # invalid: reported by J-4, not counted
    ])
    def test_carries_an_incentive(self, ov, ful, carries):
        for kind in ("obligation", "prohibition"):
            c = indices(norms(duty("a", kind, ov, ful), duty("b"))).coverage
            assert c.value == F(int(carries), 2)

    def test_permissions_and_powers_are_not_in_n(self):
        c = indices(norms(duty("a", ov="{ sanction: fine }"), power("p", "D"),
                          '- {id: m, kind: permission, bearer: D, content: "D -> X : m"}')).coverage
        assert c.norms == ["a"] and c.value == 1

    def test_reparation_only_and_transition_counts(self):
        lifecycle = ("lifecycle:\n  states: [Open, Failed]\n  initial: Open\n"
                     "  terminal: [Failed]\n  transitions: []\n")
        c = indices(lifecycle, norms(
            duty("a", ov="{ reparation: b }"),
            duty("b", ov="{ reparation: c, sanction: fine }"),
            duty("c", ov="{ transition: Failed }"),
            duty("d", ov="{ transition: Failed, magnitude: 1, reparation: c }"),
            duty("e", ov="{ severity: Critical }"))).coverage
        assert c.value == F(2, 5) and c.incentive == ["b", "d"]
        assert c.reparation_only == ["a"]           # b and d carry an incentive
        assert c.transition == ["c", "d"]           # whether or not they carry one


# --- J-6, J-7, J-8, J-9: comparison with the declared parameters -----------------

FULL_SHARING = ('    - "D -> R[*] : route"\n    - "D -> X : route"\n'
                '    - "R[*] -> D : status"\n    - "R[*] -> X : status"\n')


def info(alpha, sharing: str = "") -> str:
    return (f"information:\n  alpha: {alpha}\n"
            + (f"  sharing:\n{sharing}" if sharing else ""))


class TestComparisons:
    def test_every_comparison_is_a_warning(self):
        r = parse_file(EXAMPLE)
        r = type_check(r)
        assert r.ok and len(rule(r.warnings, "J-7")) == 1 and "beta: 0.9" in rule(r.warnings, "J-7")[0]

    def test_j6_alpha_0_with_a_given_pair(self):
        r = check(ITEMS, info(0, '    - "D -> R[*] : route"\n'))
        w = rule(r.warnings, "J-6")
        assert r.ok and len(w) == 1 and "(route, R)" in w[0] and "D -> R[*] : route" in w[0]
        assert not rule(r.warnings, "J-7")
        assert not rule(check(ITEMS, info(0)).warnings, "J-6")
        # a member-specific entry gives no pair and is no counterexample
        assert not rule(check(ITEMS, info(0, '    - "D -> R[2] : route"\n')).warnings, "J-6")

    def test_j6_alpha_0_with_sharing_and_undefined_index(self):
        w = rule(check(info(0, '    - "D -> R[*] : route"\n')).warnings, "J-6")
        assert len(w) == 1 and "information.sharing is not empty" in w[0]
        assert not rule(check(info(0)).warnings, "J-6")

    def test_j6_alpha_1(self):
        w = rule(check(ITEMS, info(1, '    - "D -> R[*] : route"\n')).warnings, "J-6")
        assert len(w) == 1 and "3 of 4" in w[0] and "(route, X)" in w[0]
        r = check(ITEMS, info(1, FULL_SHARING))
        assert not rule(r.warnings, "J-6") and not rule(r.warnings, "J-7")
        assert not rule(check(info(1)).warnings, "J-6")       # undefined index

    def test_j6_beta_1(self):
        auth = "authority: { beta: 1 }\n"
        w = rule(check(auth, norms(power("p", "D"), power("q", "R[i]"), power("r", "X"))).warnings, "J-6")
        assert len(w) == 1 and "'q' is borne by R[i]" in w[0] and "r" in w[0] and "not D" in w[0]
        # every power when no single actor bears one
        w = rule(check(auth, norms(power("p", "R[i]"), power("q", "R[*]"))).warnings, "J-6")
        assert len(w) == 1 and "'p'" in w[0] and "q" in w[0] and "not a single actor" in w[0]
        # the first in file order when several share the largest count
        w = rule(check(auth, norms(power("p", "X"), power("q", "D"))).warnings, "J-6")
        assert "'q' is borne by D, which is not X" in w[0]
        assert not rule(check(auth, norms(power("p", "D"), power("q", "D"))).warnings, "J-6")
        assert not rule(check(auth, norms(duty("a"))).warnings, "J-6")      # undefined

    def test_j6_beta_0(self):
        auth = "authority: { beta: 0 }\n"
        # only above an even share among the single actors that bear a power
        three = norms(power("p", "D"), power("q", "X"), power("r", "R[1]"))
        r = check(auth, three)
        assert not rule(r.warnings, "J-6")
        assert len(rule(r.warnings, "J-7")) == 1          # 1/3 is farther than 0.25
        w = rule(check(auth, norms(power("p", "D"), power("q", "D"), power("r", "X"))).warnings, "J-6")
        assert len(w) == 1 and "D bears 2 of the 3" in w[0]
        assert not rule(check(auth, norms(power("p", "D"), power("q", "R[i]"))).warnings, "J-6")
        r = check(auth, norms(power("p", "R[i]")))
        assert not rule(r.warnings, "J-6") and not rule(r.warnings, "J-7")

    def test_j7_tolerance(self):
        powers = norms(power("p", "D"), power("q", "R[i]"))           # beta^ = 1/2
        assert not rule(check("authority: { beta: 0.75 }\n", powers).warnings, "J-7")
        assert not rule(check("authority: { beta: 0.25 }\n", powers).warnings, "J-7")
        assert rule(check("authority: { beta: 0.76 }\n", powers).warnings, "J-7")
        assert rule(check("authority: { beta: 0.24 }\n", powers).warnings, "J-7")
        tol = "grounding: { tolerance: 0.5 }\n"
        assert not rule(check("authority: { beta: 0.99 }\n", tol, powers).warnings, "J-7")
        tol = "grounding: { tolerance: 0 }\n"
        assert rule(check("authority: { beta: 0.51 }\n", tol, powers).warnings, "J-7")
        assert not rule(check("authority: { beta: 0.5 }\n", tol, powers).warnings, "J-7")

    def test_j7_is_exact(self):
        # 0.1 + 0.2 != 0.3 in floating point; the comparison is in decimals
        third = norms(power("p", "D"), power("q", "R[i]"), power("r", "R[i]"),
                      power("s", "R[i]"), power("t", "R[i]"), power("u", "R[i]"),
                      power("v", "R[i]"), power("w", "R[i]"), power("x", "R[i]"),
                      power("y", "R[i]"))                                # beta^ = 1/10
        tol = "grounding: { tolerance: 0.2 }\n"
        assert not rule(check("authority: { beta: 0.3 }\n", tol, third).warnings, "J-7")
        assert rule(check("authority: { beta: 0.30000001 }\n", tol, third).warnings, "J-7")

    def test_j7_alpha(self):
        w = rule(check(ITEMS, info(0.9, '    - "D -> R[*] : route"\n')).warnings, "J-7")
        assert len(w) == 1 and "alpha: 0.9" in w[0] and "1/4" in w[0]
        assert not rule(check(info(0.9)).warnings, "J-7")              # undefined index

    def test_j7_no_warning_when_j6_warns(self):
        r = check("authority: { beta: 1 }\n", norms(power("p", "R[i]")))
        assert rule(r.warnings, "J-6") and not rule(r.warnings, "J-7")

    def test_j7_holder_with_a_smaller_share(self):
        powers = norms(power("p", "D"), power("q", "X"), power("r", "X"))
        w = rule(check("authority: { decision_holder: D }\n", powers).warnings, "J-7")
        assert len(w) == 1 and "decision holder D" in w[0] and "X (2)" in w[0]
        assert not rule(check("authority: { decision_holder: X }\n", powers).warnings, "J-7")
        assert not rule(check('authority: { decision_holder: "R[*]" }\n', powers).warnings, "J-7")
        assert not rule(check("authority: { decision_holder: D }\n",
                              norms(power("p", "R[i]"))).warnings, "J-7")

    def test_j8(self):
        lam = "incentives: { lambda: %s }\n"
        w = rule(check(lam % 0.5, norms(duty("a"))).warnings, "J-8")
        assert len(w) == 1 and "nothing to scale" in w[0]
        w = rule(check(lam % 0.5, norms(power("p", "D"))).warnings, "J-8")      # N is empty
        assert len(w) == 1 and "no obligation and no prohibition" in w[0]
        assert not rule(check(lam % 0.5, norms(duty("a", ov="{ sanction: fine }"))).warnings, "J-8")
        w = rule(check(lam % 0, norms(duty("a", ov="{ magnitude: 2 }"))).warnings, "J-8")
        assert len(w) == 1 and "lambda: 0" in w[0]
        assert not rule(check(lam % 0, norms(duty("a"))).warnings, "J-8")
        assert not rule(check(norms(duty("a"))).warnings, "J-8")               # no lambda
        # a magnitude of zero alone carries no incentive
        assert rule(check(lam % 0.5, norms(duty("a", ov="{ magnitude: 0 }"))).warnings, "J-8")

    def test_j8_only_for_a_contract_with_norms(self):
        assert not rule(check("incentives: { lambda: 0.5 }\n").warnings, "J-8")

    def test_j9_designs(self):
        text = (EXAMPLES / "stochastic_robot_delivery.cadl").read_text()
        text = text.replace("    - stochastic: 0.1\n",
                            "    - stochastic: 0.1\n    - norms: 0.1\n    - grounding: 0.1\n")
        text = text.replace(
            "      authority:   { decision_holder: DISPATCHER, beta: 0.9 }\n",
            "      authority:   { decision_holder: DISPATCHER, beta: 0.9 }\n"
            "      grounding: { items: { route: DISPATCHER } }\n"
            "      norms:\n"
            '        - { id: deliver, kind: obligation, bearer: "ROBOT[i]", deadline: 60s, '
            'content: "ROBOT[i] -> DISPATCHER : delivered" }\n'
            '        - { id: replan, kind: power, bearer: DISPATCHER, '
            'content: "DISPATCHER -> ROBOT[i] : replan", effects: { discharge: [deliver] } }\n')
        r = type_check(parse(text))
        assert r.ok, [str(e) for e in r.errors]
        infos = rule(r.infos, "J-9")
        # beta^ = 1: CENTRALIZED (0.9) is within the tolerance, DISTRIBUTED (0.1) is not;
        # alpha^ = 0/1: alpha = 0.9 in both designs
        assert len(infos) == 3
        assert sum("DISTRIBUTED" in i and "'beta'" in i for i in infos) == 1
        assert sum("'alpha'" in i for i in infos) == 2
        assert not rule(r.warnings, "J-9") and not rule(r.errors, "J-9")
        # a larger tolerance silences them
        r = type_check(parse(text.replace("grounding: { items", "grounding: { tolerance: 1, items")))
        assert not rule(r.infos, "J-9")

    def test_j9_needs_the_stochastic_extension(self):
        assert not rule(check(ITEMS, info(0.9)).infos, "J-9")


# --- Report and command (J-5) ---------------------------------------------------

class TestReport:
    def test_report_of_the_example(self):
        rep = G.report(parse_file(EXAMPLE))
        (c,) = rep["contracts"]
        a, b, x = c["alpha"], c["beta"], c["coverage"]
        assert (a["numerator"], a["denominator"], a["exact"], a["value"]) == (5, 8, "5/8", 0.625)
        assert (a["declared"], a["difference"]) == ("0.6", "-0.025")
        assert (a["by_sharing"], a["by_obligation"], a["by_permission"]) == (3, 1, 1)
        assert len(a["given"]) == 5 and len(a["not_given"]) == 3 and a["member_specific"] == []
        assert (b["numerator"], b["denominator"]) == (1, 2)
        assert (b["declared"], b["difference"]) == ("0.9", "0.4")
        assert b["bearers"] == {"DISPATCHER": 1, "ROBOT[i]": 1}
        assert [(p["id"], p["effects"], p["single_actor"]) for p in b["powers"]] == [
            ("reassign", 2, True), ("decline", 1, False)]
        assert (b["decision_holder"], b["holder_powers"], b["holder_share"]) == ("DISPATCHER", 1, "1/2")
        assert (x["numerator"], x["denominator"], x["declared_lambda"]) == (1, 3, "0.5")
        assert "difference" not in x
        assert x["reparation_only"] == ["acknowledge"] and x["transition"] == ["report_failure"]
        assert [d["level"] for d in c["diagnostics"]] == ["warning"]
        assert c["tolerance"] == "0.25"
        json.dumps(rep)

    def test_undefined_quantities_have_a_reason(self):
        rep = G.report(parse(source()))
        c = rep["contracts"][0]
        for key in ("alpha", "beta", "coverage"):
            assert c[key]["defined"] is False and c[key]["undefined"]
        text = G.format_report_text(rep)
        assert text.count("undefined (") == 3

    def test_cli_text_and_json(self, capsys):
        assert main(["norms-indices", str(EXAMPLE)]) == 0
        out = capsys.readouterr().out
        assert "alpha^ (information sharing) = 5/8 = 0.625" in out
        assert "beta^ (concentration of powers) = 1/2 = 0.5" in out
        assert "chi (incentive coverage) = 1/3 = 0.333333" in out
        assert "(J-7)" in out
        assert main(["norms-indices", str(EXAMPLE), "--format", "json",
                     "--contract", "DELIVERY_SLA"]) == 0
        d = json.loads(capsys.readouterr().out)
        assert d["file"] == str(EXAMPLE) and d["contracts"][0]["alpha"]["exact"] == "5/8"

    def test_cli_usage_errors(self, capsys, tmp_path):
        assert main(["norms-indices", str(tmp_path / "none.cadl")]) == 2
        assert main(["norms-indices", str(EXAMPLES / "norms_robot_delivery.cadl")]) == 2
        assert "does not declare 'grounding: 0.1'" in capsys.readouterr().err
        assert main(["norms-indices", str(EXAMPLE), "--contract", "NOPE"]) == 2
        bad = tmp_path / "bad.cadl"
        bad.write_text(source("grounding: { tolerance: 7 }\n"))
        assert main(["norms-indices", str(bad)]) == 2
        assert "J-2" in capsys.readouterr().err

    def test_cli_through_the_module(self):
        p = subprocess.run([sys.executable, "-m", "cadl.cli", "norms-indices", str(EXAMPLE)],
                           capture_output=True, text=True)
        assert p.returncode == 0 and "Contract DELIVERY_SLA (tolerance 0.25)" in p.stdout


# --- IR (J.10) ----------------------------------------------------------------------

class TestIR:
    def test_example_is_the_ir_of_j10(self):
        d = _ir_to_dict(lower_to_ir(parse_file(EXAMPLE)))
        c = d["institution"]["contracts"][0]
        assert c["grounding"] == {
            "items": {"route": "DISPATCHER", "status": "ROBOT[*]", "battery": "ROBOT[*]",
                      "failure": "ROBOT[*]"},
            "tolerance": 0.25,
            "indices": {
                "alpha": {"given": 5, "pairs": 8, "by_sharing": 3, "by_obligation": 1,
                          "by_permission": 1, "member_specific": 0},
                "beta": {"largest": 1, "powers": 2, "holder": 1,
                         "bearers": {"DISPATCHER": 1, "ROBOT[i]": 1}},
                "coverage": {"incentive": 1, "norms": 3, "reparation_only": 1, "transition": 1},
            },
        }
        by = {n["id"]: n for n in c["norms"]}
        n = by["no_low_battery_driving"]
        assert (n["shares"], n["on_violation_magnitude"], n["on_fulfilment_reward"],
                n["on_fulfilment_magnitude"]) == ([], 4, None, None)
        assert by["report_failure"]["shares"] == ["failure"]
        assert "grounding_extension" not in d
        json.dumps(d)

    def test_no_block_gives_empty_items_default_tolerance_and_nulls(self):
        d = _ir_to_dict(lower_to_ir(parse(source(norms(
            duty("a", ov='{ sanction: "", magnitude: 0 }', ful="{ reward: bonus, magnitude: 1.5 }"))))))
        c = d["institution"]["contracts"][0]
        assert c["grounding"]["items"] == {} and c["grounding"]["tolerance"] == 0.25
        assert c["grounding"]["indices"]["alpha"] is None
        assert c["grounding"]["indices"]["beta"] is None
        assert c["grounding"]["indices"]["coverage"] == {
            "incentive": 1, "norms": 1, "reparation_only": 0, "transition": 0}
        n = c["norms"][0]
        assert n["on_violation_magnitude"] == 0 and n["on_fulfilment_reward"] == "bonus"
        assert n["on_fulfilment_magnitude"] == 1.5 and n["shares"] == []

    def test_holder_is_null_unless_a_single_actor(self):
        d = _ir_to_dict(lower_to_ir(parse(source(
            'authority: { decision_holder: "R[*]" }\n', norms(power("p", "D"))))))
        assert d["institution"]["contracts"][0]["grounding"]["indices"]["beta"] == {
            "largest": 1, "powers": 1, "holder": None, "bearers": {"D": 1}}

    @pytest.mark.parametrize("declare", [("norms: 0.1",), (), ("grounding: 0.1",)])
    def test_fields_are_absent_unless_the_extension_is_active(self, declare):
        d = _ir_to_dict(lower_to_ir(parse(source(ITEMS, norms(duty("a")), declare=declare))))
        c = d["institution"]["contracts"][0]
        assert "grounding" not in c
        for n in c["norms"]:
            assert not {"shares", "on_violation_magnitude", "on_fulfilment_reward",
                        "on_fulfilment_magnitude"} & set(n)

    def test_sim_ir_command(self):
        p = subprocess.run([sys.executable, "-m", "cadl.cli", "sim-ir", str(EXAMPLE),
                            "--format", "json"], capture_output=True, text=True)
        assert p.returncode == 0
        c = json.loads(p.stdout)["institution"]["contracts"][0]
        assert c["grounding"]["indices"]["alpha"]["given"] == 5


# --- Two versions (J-13) ----------------------------------------------------------

def _versions(old_blocks, new_blocks):
    return parse(source(*old_blocks)), parse(source(*new_blocks))


class TestDiff:
    def test_changes_of_the_keys_are_reported(self):
        old = (ITEMS, norms(
            duty("a", ov="{ sanction: fine, magnitude: 4 }", ful="{ reward: bonus }"),
            SHARE_NORM.format(kind="permission", to="D")))
        new = (ITEMS.replace('status: "R[*]"', 'status: "R[*]"\n    load: "R[*]"'), norms(
            duty("a", ov="{ sanction: fine, magnitude: 9 }", ful="{ reward: bonus, magnitude: 2 }"),
            SHARE_NORM.format(kind="permission", to="D").replace("[status]", "[status, load]")))
        res = diff_versions(*_versions(old, new))
        kinds = [f["kind"] for f in res["contracts"][0]["findings"]]
        for kind in ("items_changed", "magnitude_changed", "on_fulfilment_changed",
                     "shares_changed"):
            assert kinds.count(kind) == 1, kinds
        assert res["ok"]
        by = {f["kind"]: f for f in res["contracts"][0]["findings"]}
        assert (by["magnitude_changed"]["old"], by["magnitude_changed"]["new"]) == (4, 9)
        assert by["items_changed"]["added"] == ["load"]
        assert by["shares_changed"]["new"] == ["status", "load"]
        text = format_diff_text(res)
        assert "a: on_violation.magnitude changes from 4 to 9" in text
        json.dumps(res)

    def test_magnitude_added_and_removed(self):
        res = diff_versions(*_versions((norms(duty("a", ov="{ sanction: fine }")),),
                                       (norms(duty("a", ov="{ sanction: fine, magnitude: 1 }")),)))
        (f,) = [f for f in res["contracts"][0]["findings"] if f["kind"] == "magnitude_changed"]
        assert "from none to 1" in f["text"]
        # the norms themselves are unchanged: no other norm finding
        assert not [f for f in res["contracts"][0]["findings"] if f["kind"] == "norm_changed"]

    def test_j13_opposite_directions_beta(self):
        # beta is raised while an amendment gives a power to a robot
        old = ("authority: { beta: 0.8 }\n", norms(power("p", "D")))
        new = ("authority: { beta: 0.9 }\n", norms(power("p", "D"), power("q", "R[i]")))
        res = diff_versions(*_versions(old, new))
        (w,) = [f for f in res["contracts"][0]["findings"] if f["kind"] == "index_opposite"]
        assert w["level"] == "warning" and w["parameter"] == "beta" and "(J-13)" in w["text"]
        assert res["ok"]

    def test_j13_opposite_directions_alpha(self):
        old = (ITEMS, info(0.5, '    - "D -> R[*] : route"\n'))
        new = (ITEMS, info(0.4, FULL_SHARING))
        res = diff_versions(*_versions(old, new))
        # contracts without norms are not compared by norms-diff; ask for it
        res = diff_versions(*_versions(old, new), contract_id="C")
        (w,) = [f for f in res["contracts"][0]["findings"] if f["kind"] == "index_opposite"]
        assert w["parameter"] == "alpha"

    @pytest.mark.parametrize("b0, b1, extra", [
        (0.8, 0.7, True),      # both fall
        (0.8, 0.8, True),      # declared unchanged
        (0.8, 0.9, False),     # index unchanged
    ])
    def test_j13_no_warning(self, b0, b1, extra):
        old = (f"authority: {{ beta: {b0} }}\n", norms(power("p", "D")))
        new_norms = [power("p", "D")] + ([power("q", "R[i]")] if extra else [])
        new = (f"authority: {{ beta: {b1} }}\n", norms(*new_norms))
        res = diff_versions(*_versions(old, new))
        assert not [f for f in res["contracts"][0]["findings"] if f["kind"] == "index_opposite"]

    def test_j13_needs_the_index_defined_in_both_versions(self):
        old = ("authority: { beta: 0.8 }\n", norms(duty("a")))
        new = ("authority: { beta: 0.9 }\n", norms(duty("a"), power("q", "R[i]")))
        res = diff_versions(*_versions(old, new))
        assert not [f for f in res["contracts"][0]["findings"] if f["kind"] == "index_opposite"]

    def test_nothing_is_added_without_the_extension(self):
        only_norms = ("norms: 0.1",)
        old = parse(source(norms(duty("a", ov="{ magnitude: 1 }")), declare=only_norms))
        new = parse(source(norms(duty("a", ov="{ magnitude: 2 }")), declare=only_norms))
        assert diff_versions(old, new)["contracts"][0]["findings"] == []

    def test_a_version_without_the_extension_has_no_index(self):
        old = parse(source("authority: { beta: 0.8 }\n", norms(power("p", "D")),
                           declare=("norms: 0.1",)))
        new = parse(source("authority: { beta: 0.9 }\n",
                           norms(power("p", "D"), power("q", "R[i]"),
                                 duty("a", ov="{ magnitude: 2 }"))))
        kinds = [f["kind"] for f in diff_versions(old, new)["contracts"][0]["findings"]]
        assert "magnitude_changed" not in kinds        # a is new: reported as added
        assert "index_opposite" not in kinds and "index_changed" not in kinds

    def test_cli(self, tmp_path, capsys):
        a, b = tmp_path / "a.cadl", tmp_path / "b.cadl"
        a.write_text(EXAMPLE.read_text())
        b.write_text(EXAMPLE.read_text().replace("magnitude: 4", "magnitude: 9")
                     .replace("          shares: [battery]\n", ""))
        assert main(["norms-diff", str(a), str(b)]) == 0
        out = capsys.readouterr().out
        assert "no_low_battery_driving: on_violation.magnitude changes from 4 to 9" in out
        assert "may_return_to_charge: shares changes from [battery] to []" in out
        assert "alpha^ changes from 5/8 = 0.625 to 4/8" not in out      # ratios are reduced
        assert "alpha^ changes from 5/8 = 0.625 to 1/2 = 0.5" in out


# --- The norm a payoff term names (J-10) -------------------------------------------

LINKED = (norms(
    duty("no_shortcut", "prohibition", ov="{ sanction: deduction, magnitude: 2 }",
         ful="{ reward: bonus, magnitude: 0.5 }"),
    duty("plain", ov="{ sanction: fine }"),
    duty("zero", ov="{ magnitude: 0 }"),
    power("p", "D")),)


class TestNormPayoffLink:
    def test_sanction_and_reward(self):
        sos = parse(source(*LINKED))
        link = G.norm_payoff_link(sos, "C.no_shortcut")
        assert link.error is None
        assert (link.contract, link.norm, link.kind) == ("C", "no_shortcut", "prohibition")
        assert link.magnitude == 2 and link.magnitude_text == "2"
        assert link.bearer == "R[i]" and link.lambda_source == "C.lambda"
        assert link.weight_text("lambda") == "-2 * lambda"
        link = G.norm_payoff_link(sos, "C.no_shortcut", "reward_of")
        assert link.error is None and link.magnitude == F(1, 2)
        assert link.weight_text("lam") == "0.5 * lam"

    def test_example_of_j7(self):
        text = (EXAMPLES / "grounding_robot_delivery.cadl").read_text()
        link = G.norm_payoff_link(parse(text), "DELIVERY_SLA.no_low_battery_driving")
        assert link.magnitude == 4 and link.bearer == "ROBOT[i]"
        assert link.lambda_source == "DELIVERY_SLA.lambda"

    @pytest.mark.parametrize("ref, key, text", [
        ("C.nope", "sanction_of", "has no norm 'nope'"),
        ("Z.no_shortcut", "sanction_of", "contract 'Z', which is not declared"),
        ("no_shortcut", "sanction_of", "not a string of the form C.n"),
        ("C.no_shortcut.x", "sanction_of", "not a string of the form C.n"),
        (5, "sanction_of", "not a string of the form C.n"),
        (None, "sanction_of", "not a string of the form C.n"),
        ("C.p", "sanction_of", "is a power"),
        ("C.plain", "sanction_of", "declares no on_violation.magnitude"),
        ("C.plain", "reward_of", "declares no on_fulfilment.magnitude"),
        ("C.zero", "sanction_of", "greater than zero"),
    ])
    def test_errors(self, ref, key, text):
        link = G.norm_payoff_link(parse(source(*LINKED)), ref, key)
        assert link.magnitude is None and text in link.error

    def test_error_keeps_what_is_known(self):
        link = G.norm_payoff_link(parse(source(*LINKED)), "C.plain")
        assert (link.contract, link.norm, link.bearer, link.lambda_source) == (
            "C", "plain", "R[i]", "C.lambda")

    def test_extension_not_active(self):
        sos = parse(source(*LINKED, declare=("norms: 0.1",)))
        assert "does not declare" in G.norm_payoff_link(sos, "C.no_shortcut").error

    def test_invalid_key(self):
        with pytest.raises(ValueError):
            G.norm_payoff_link(parse(source(*LINKED)), "C.no_shortcut", "weight")

    def test_covers(self):
        assert G.covers("ROBOT[*]", "ROBOT[i]") and G.covers("ROBOT[1]", "ROBOT[i]")
        assert G.covers("ROBOT[2]", "ROBOT[2]") and G.covers("ROBOT", "ROBOT[2]")
        assert not G.covers("ROBOT[1]", "ROBOT[2]") and not G.covers("D", "ROBOT[i]")

    def test_lambda_parameters(self):
        from cadl.stochastic import Param
        params = [Param(id="l", source="C.lambda"), Param(id="a", source="C.alpha"),
                  Param(id="l2", source="C.lambda"), Param(id="m", source="B.lambda"),
                  Param(id="free")]
        assert G.lambda_parameters(params, "C") == ["l", "l2"]
        assert G.lambda_parameters(params, "Z") == []


# --- Annotations change no verdict (J.1, J.12) -----------------------------------

def _annotated(text: str) -> str:
    """The norms example with the keys of the extension added."""
    text = text.replace("    - norms: 0.1\n", "    - norms: 0.1\n    - grounding: 0.1\n")
    out = []
    for line in text.splitlines(keepends=True):
        out.append(line)
        indent = line[:len(line) - len(line.lstrip())]
        if line.strip().startswith("kind: obligation") or line.strip().startswith("kind: prohibition"):
            out.append(f'{indent}on_fulfilment: {{ reward: "bonus", magnitude: 3 }}\n')
        if line.strip().startswith("severity:") and len(indent) >= 12:
            out.append(f"{indent}magnitude: 2\n")
    return "".join(out)


NORM_TRACES = sorted(TRACES.glob("norms_robot_delivery_*.jsonl"))


class TestAnnotationsChangeNoVerdict:
    def test_the_annotated_example_uses_the_keys(self):
        sos = parse(_annotated((EXAMPLES / "norms_robot_delivery.cadl").read_text()))
        assert type_check(sos).ok
        assert {"on_fulfilment:", "on_violation.magnitude"} <= set(G.keys_used(sos))
        assert G.contract_indices(sos, sos.contracts[0]).coverage.value == 1

    @pytest.mark.parametrize("trace", NORM_TRACES, ids=lambda p: p.stem)
    def test_norms_eval(self, trace):
        plain = parse_file(EXAMPLES / "norms_robot_delivery.cadl")
        annotated = parse(_annotated((EXAMPLES / "norms_robot_delivery.cadl").read_text()))
        lines = trace.read_text()
        assert evaluate(plain, parse_trace(lines.splitlines())) == evaluate(annotated, parse_trace(lines.splitlines()))

    def test_norms_eval_of_the_grounding_example(self):
        # the example of J.9 without its annotations gives the same verdicts
        text = EXAMPLE.read_text()
        bare = "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith(("shares:", "magnitude:")))
        assert G.keys_used(parse(bare)) == ["grounding:"]
        trace = [{"t": 0, "messages": [{"from": "DISPATCHER", "to": "ROBOT[2]",
                                        "name": "route_assignment"}]},
                 {"t": 6, "values": {"ROBOT[2].battery": 5, "ROBOT[2].status": "InTransit"}},
                 {"t": 9, "end": True}]
        lines = "\n".join(json.dumps(o) for o in trace)
        a = evaluate(parse(text), parse_trace(lines.splitlines()))
        b = evaluate(parse(bare), parse_trace(lines.splitlines()))
        assert a == b and not a["ok"]

    @pytest.mark.parametrize("name", ["agents_tool_governance", "agents_change_control"])
    def test_agents_run(self, name, tmp_path):
        from cadl.agents_runtime import run_files
        text = (EXAMPLES / f"{name}.cadl").read_text()
        annotated = tmp_path / f"{name}.cadl"
        annotated.write_text(_annotated(text))
        sos = parse_file(annotated)
        assert type_check(sos).ok and G.grounding_active(sos)
        assert G.keys_used(sos)
        for trace in sorted(TRACES.glob(f"{name}_*.jsonl")):
            a = run_files(EXAMPLES / f"{name}.cadl", trace)
            b = run_files(annotated, trace)
            a.pop("file", None), b.pop("file", None)
            assert a == b

    def test_generated_monitor(self, tmp_path):
        from cadl.codegen.norms_monitor_gen import build_model, generate_norms_monitor_module
        plain = parse_file(EXAMPLES / "norms_robot_delivery.cadl")
        annotated = parse(_annotated((EXAMPLES / "norms_robot_delivery.cadl").read_text()))
        # the monitor does not carry the keys: the same model, the same module
        assert build_model(plain) == build_model(annotated)
        assert generate_norms_monitor_module(plain) == generate_norms_monitor_module(annotated)
        # and the monitor generated from the annotated file agrees with norms-eval
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "norms_monitor.py").write_text(generate_norms_monitor_module(annotated))
        for trace in NORM_TRACES:
            p = subprocess.run([sys.executable, str(pkg / "norms_monitor.py"), str(trace),
                                "--format", "json"], capture_output=True, text=True)
            got = json.loads(p.stdout)
            want = evaluate(annotated, parse_trace(trace.read_text().splitlines()))
            for key in ("file", "trace"):
                want.pop(key, None), got.pop(key, None)
            assert got == want, trace.name

    def test_monitor_of_the_grounding_example_runs(self, tmp_path):
        from cadl.codegen.norms_monitor_gen import generate_norms_monitor_module
        sos = parse_file(EXAMPLE)
        module = tmp_path / "norms_monitor.py"
        module.write_text(generate_norms_monitor_module(sos))
        assert "magnitude" not in module.read_text() and "shares" not in module.read_text()
        trace = tmp_path / "t.jsonl"
        trace.write_text(json.dumps({"t": 0}) + "\n" + json.dumps({"t": 400, "end": True}) + "\n")
        p = subprocess.run([sys.executable, str(module), str(trace), "--format", "json"],
                           capture_output=True, text=True)
        assert p.stdout, p.stderr
        got = json.loads(p.stdout)
        want = evaluate(sos, parse_trace(trace.read_text().splitlines()))
        for key in ("file", "trace"):
            want.pop(key, None), got.pop(key, None)
        assert got == want


# --- Code generation ---------------------------------------------------------------

class TestCodegenInput:
    def test_texts_of_the_extension_are_not_checked_as_source(self):
        from cadl.codegen.safety import UnsafeCodegenInput, check_codegen_input
        quote = "it's \"one\" month's fee */ `x`"
        sos = parse(source(
            "grounding:\n  items: { route: D }\n  note: " + json.dumps(quote) + "\n",
            norms(duty("a", ful="{ reward: %s }" % json.dumps(quote)))))
        assert type_check(sos).ok
        check_codegen_input(sos)        # the reward text is never written into code
        with pytest.raises(UnsafeCodegenInput):
            check_codegen_input(parse(source(norms(duty("a", ov="{ sanction: \"it's\" }")))))

    @pytest.mark.parametrize("target", ["python", "solidity", "opa"])
    def test_example_generates(self, target, tmp_path):
        assert main(["codegen", str(EXAMPLE), "--target", target, "-o", str(tmp_path)]) == 0
        assert [p for p in tmp_path.rglob("*") if p.is_file()]


# --- J.12: declaring the extension never invalidates a file ------------------------

def _errors_gained(sos) -> list:
    """Errors that `cadl check` reports only after `grounding: 0.1` is
    added to the extensions of a parsed file."""
    before = {str(e) for e in type_check(sos).errors}
    declared = copy.deepcopy(sos)
    declared.extensions = list(declared.extensions) + [("grounding", "0.1")]
    return [str(e) for e in type_check(declared).errors if str(e) not in before]


class _Recorded(BaseException):
    pass


def _recorded_fixtures(module, first_only: bool = False) -> list:
    """Every file that the tests of a module parse: the tests that need no
    fixture are run with the module's `parse` replaced by a recorder. With
    `first_only` a test is left as soon as it has parsed its (first) file,
    so that what it does with the file is not run again."""
    seen, original = [], module.parse

    def recorder(text, *args, **kwargs):
        seen.append(text)
        if first_only:
            raise _Recorded()
        return original(text, *args, **kwargs)

    def run(fn):
        params = [p for p in inspect.signature(fn).parameters if p != "self"]
        if not params and "subprocess" not in inspect.getsource(fn):
            try:
                fn()
            except (Exception, _Recorded):
                pass

    module.parse = recorder
    try:
        for name, obj in vars(module).items():
            if inspect.isclass(obj) and name.startswith("Test") \
                    and "CLI" not in name and "Codegen" not in name:
                inst = obj()
                for attr in dir(inst):
                    if attr.startswith("test_"):
                        run(getattr(inst, attr))
            elif inspect.isfunction(obj) and name.startswith("test_"):
                run(obj)
    finally:
        module.parse = original
    return list(dict.fromkeys(seen))


class TestNeverInvalidates:
    @pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.cadl"))
                             + sorted((ROOT / "tests" / "data").rglob("*.cadl")),
                             ids=lambda p: p.stem)
    def test_bundled_files(self, path):
        from cadl.norms import norms_declared
        sos = parse_file(path)
        gained = _errors_gained(sos)
        if norms_declared(sos):
            assert gained == []
        else:
            # J-1 is the one error the declaration can cause, and only here
            assert len(gained) == 1 and "(J-1)" in gained[0]

    @pytest.mark.parametrize("name", ["test_norms_extension", "test_norms_eval"])
    def test_fixtures_of_the_norms_tests(self, name):
        import importlib
        from cadl.norms import norms_declared
        module = importlib.import_module(f"tests.{name}")
        fixtures = _recorded_fixtures(module, first_only=name == "test_norms_eval")
        assert len(fixtures) > 30
        checked = 0
        for text in fixtures:
            try:
                sos = parse(text)
            except Exception:
                continue
            if not norms_declared(sos):
                continue
            checked += 1
            assert _errors_gained(sos) == [], text
        assert checked > 30

    def test_valid_under_f_with_beta_1_stays_valid(self):
        # a contract with beta: 1 and a power of the robots: a warning, not an error
        text = (EXAMPLES / "norms_robot_delivery.cadl").read_text().replace(
            "      parties: [DISPATCHER,", "      authority: { beta: 1 }\n      parties: [DISPATCHER,")
        sos = parse(text)
        assert sos.contracts[0].authority.beta == 1 and type_check(sos).ok
        sos.extensions.append(("grounding", "0.1"))
        r = type_check(sos)
        assert r.ok and not rule(r.warnings, "J-6")       # its one power is the dispatcher's
        sos = parse(text.replace("bearer: DISPATCHER\n          content: reassignment",
                                 'bearer: "ROBOT[i]"\n          content: reassignment'))
        sos.extensions.append(("grounding", "0.1"))
        r = type_check(sos)
        assert not rule(r.errors, "J-6") and len(rule(r.warnings, "J-6")) == 1


# --- The lemmas of J.4 as properties -------------------------------------------------

ACTORS = {"A": None, "B": None, "S": "1..3", "T": "1..4"}


def _random_contract(rng: random.Random) -> dict:
    """A random contract as YAML data over the actors A, B, S[1..3], T[1..4]."""
    names = rng.sample(list(ACTORS), rng.randint(1, 4))

    def ref(name, whole=None):
        if ACTORS[name] is None:
            return name
        forms = ["*", "i", ACTORS[name]] if whole else ["*", "i", ACTORS[name], "2", "1..2"]
        if whole is None and rng.random() < 0.1:
            return name
        return f"{name}[{rng.choice(forms)}]"

    parties = [ref(n, whole=True) for n in names]
    items = {f"x{k}": ref(rng.choice(names), whole=True) for k in range(rng.randint(0, 4))}
    sharing = []
    for _ in range(rng.randint(0, 5)):
        if items:
            sharing.append(f"{ref(rng.choice(names))} -> {ref(rng.choice(names))} : "
                           f"{rng.choice(list(items))}")
    norms_ = []
    for k in range(rng.randint(0, 6)):
        kind = rng.choice(["obligation", "permission", "prohibition", "power"])
        bearer = ref(rng.choice(names))
        n = {"id": f"n{k}", "kind": kind, "bearer": bearer,
             "content": f"{bearer} -> {ref(rng.choice(names))} : m{k}"}
        if kind == "obligation":
            n["deadline"] = "5s"
        if kind in ("obligation", "permission") and items and rng.random() < 0.6:
            n["shares"] = rng.sample(list(items), rng.randint(1, len(items)))
        if kind in ("obligation", "prohibition"):
            ov = {}
            if rng.random() < 0.3:
                ov["sanction"] = rng.choice(["fine", ""])
            if rng.random() < 0.3:
                ov["magnitude"] = rng.choice([0, 1, 2.5])
            if ov:
                n["on_violation"] = ov
            if rng.random() < 0.2:
                n["on_fulfilment"] = rng.choice([{"reward": "bonus"}, {"magnitude": 0},
                                                 {"magnitude": 3}])
        norms_.append(n)
    c = {"id": "C", "parties": parties, "norms": norms_,
         "information": {"sharing": sharing}, "grounding": {"items": items}}
    return c


def _file(contract: dict) -> str:
    return yaml.safe_dump({"sos": {
        "name": "T", "extensions": [{"norms": 0.1}, {"grounding": 0.1}],
        "actors": [{"id": n if r is None else f"{n}[{r}]", "role": "r"}
                   for n, r in ACTORS.items()],
        "contracts": [contract]}}, sort_keys=False)


def _ci(contract: dict) -> G.ContractIndices:
    sos = parse(_file(contract))
    return G.contract_indices(sos, sos.contracts[0])


def _names(contract: dict) -> list:
    return [p.split("[")[0] for p in contract["parties"]]


class TestLemmas:
    N = 100

    def test_random_contracts_have_quantities_in_range(self):
        rng = random.Random(20261111)
        defined = [0, 0, 0]
        for _ in range(self.N):
            ci = _ci(_random_contract(rng))
            for k, q in enumerate((ci.alpha, ci.beta, ci.coverage)):
                if q.value is not None:
                    defined[k] += 1
                    assert 0 <= q.value <= 1
        assert min(defined) > self.N // 5

    def test_lemma_1_alpha(self):
        rng = random.Random(1)
        raised = 0
        for _ in range(self.N):
            c = _random_contract(rng)
            before = _ci(c).alpha
            if before.value is None:
                continue
            names, items = _names(c), list(c["grounding"]["items"])
            change = rng.choice(["entry", "share", "norm"])
            d = copy.deepcopy(c)
            if change == "entry":
                d["information"]["sharing"].append(
                    f"A -> {rng.choice(d['parties'])} : {rng.choice(items)}")
            elif change == "share":
                sharing = [n for n in d["norms"] if "shares" in n and len(n["shares"]) < len(items)]
                if not sharing:
                    continue
                n = rng.choice(sharing)
                n["shares"].append(rng.choice([x for x in items if x not in n["shares"]]))
            else:
                bearer = rng.choice(d["parties"])
                d["norms"].append({
                    "id": "added", "kind": rng.choice(["obligation", "permission"]),
                    "bearer": bearer, "deadline": "5s",
                    "content": f"{bearer} -> {rng.choice(d['parties'])} : madd",
                    "shares": rng.sample(items, rng.randint(1, len(items)))})
                if d["norms"][-1]["kind"] == "permission":
                    del d["norms"][-1]["deadline"]
            after = _ci(d).alpha
            assert after.pairs == before.pairs
            assert set(before.given) <= set(after.given)
            assert after.value >= before.value
            raised += after.value > before.value
        assert raised > 5

    def test_adding_an_item_or_a_party_can_lower_alpha(self):
        base = {"id": "C", "parties": ["A", "B"], "norms": [],
                "information": {"sharing": ["A -> B : x0"]},
                "grounding": {"items": {"x0": "A"}}}
        assert _ci(base).alpha.value == 1
        more_items = copy.deepcopy(base)
        more_items["grounding"]["items"]["x1"] = "A"
        assert _ci(more_items).alpha.value == F(1, 2)
        more_parties = copy.deepcopy(base)
        more_parties["parties"].append("S[*]")
        assert _ci(more_parties).alpha.value == F(1, 2)
        # and a random search finds such instances too
        rng = random.Random(7)
        lowered = 0
        for _ in range(self.N):
            c = _random_contract(rng)
            before = _ci(c).alpha.value
            if before is None:
                continue
            d = copy.deepcopy(c)
            d["grounding"]["items"]["extra"] = rng.choice(d["parties"])
            after = _ci(d).alpha.value
            lowered += after is not None and after < before
        assert lowered > 0

    def test_lemma_2_beta(self):
        rng = random.Random(2)
        seen = [0, 0]
        for _ in range(self.N):
            c = _random_contract(rng)
            before = _ci(c).beta
            if before.value is None:
                continue
            # a power of the single actor with the largest count never lowers
            if before.top is not None:
                d = copy.deepcopy(c)
                d["norms"].append({"id": "added", "kind": "power", "bearer": before.top,
                                   "content": f"{before.top} -> A : madd"})
                after = _ci(d).beta
                assert after.value >= before.value
                assert after.largest == before.largest + 1
                seen[0] += 1
            # a power whose bearer is not a single actor never raises
            sets = [p for p in c["parties"] if "[" in p]
            if sets:
                d = copy.deepcopy(c)
                bearer = rng.choice(sets).split("[")[0] + "[i]"
                d["norms"].append({"id": "added", "kind": "power", "bearer": bearer,
                                   "content": f"{bearer} -> A : madd"})
                after = _ci(d).beta
                assert after.value <= before.value
                assert after.largest == before.largest
                seen[1] += 1
        assert min(seen) > 10

    def test_lemma_3_coverage(self):
        rng = random.Random(3)
        seen = [0, 0, 0, 0]
        for _ in range(self.N):
            c = _random_contract(rng)
            before = _ci(c).coverage
            if before.value is None:
                continue
            size = len(before.norms)
            without = [n for n in c["norms"] if n["id"] in before.norms
                       and n["id"] not in before.incentive]
            if without:
                nid = rng.choice(without)["id"]
                for change, gain in rng.sample([({"sanction": "fine"}, 1), ({"magnitude": 1.5}, 1),
                                                ({"magnitude": 0}, 0), ({"sanction": ""}, 0)], 2):
                    d = copy.deepcopy(c)
                    n = next(x for x in d["norms"] if x["id"] == nid)
                    n["on_violation"] = change
                    n.pop("on_fulfilment", None)
                    after = _ci(d).coverage
                    assert after.value - before.value == F(gain, size)
                seen[0] += 1
            bearer = c["parties"][0]
            for ov, k in rng.sample([({"sanction": "fine"}, 1), ({"magnitude": 2}, 1), (None, 2),
                                     ({"magnitude": 0}, 2)], 2):
                d = copy.deepcopy(c)
                n = {"id": "added", "kind": rng.choice(["obligation", "prohibition"]),
                     "bearer": bearer, "content": f"{bearer} -> A : madd", "deadline": "5s"}
                if n["kind"] == "prohibition":
                    del n["deadline"]
                if ov is not None:
                    n["on_violation"] = ov
                d["norms"].append(n)
                after = _ci(d).coverage
                if k == 1:
                    assert after.value >= before.value       # carries an incentive
                else:
                    assert after.value <= before.value       # carries none
                seen[k] += 1
        assert min(seen[:3]) > 10

    def test_invariant_under_reordering(self):
        rng = random.Random(4)
        for _ in range(60):
            c = _random_contract(rng)
            d = copy.deepcopy(c)
            rng.shuffle(d["norms"])
            rng.shuffle(d["parties"])
            rng.shuffle(d["information"]["sharing"])
            items = list(d["grounding"]["items"].items())
            rng.shuffle(items)
            d["grounding"]["items"] = dict(items)
            a, b = _ci(c), _ci(d)
            assert (a.alpha.value, a.beta.value, a.coverage.value) == (
                b.alpha.value, b.beta.value, b.coverage.value)

    def test_splitting_a_power_changes_beta(self):
        one = {"id": "C", "parties": ["A", "S[*]"], "norms": [
            {"id": "p", "kind": "power", "bearer": "A", "content": "A -> S[i] : m"},
            {"id": "q", "kind": "power", "bearer": "S[i]", "content": "S[i] -> A : m"}]}
        two = copy.deepcopy(one)
        two["norms"].append({"id": "p2", "kind": "power", "bearer": "A",
                             "content": "A -> S[i] : m2"})
        assert (_ci(one).beta.value, _ci(two).beta.value) == (F(1, 2), F(2, 3))
