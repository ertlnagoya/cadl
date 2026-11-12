"""Design-time checking of norms by a model checker (Appendix F, F.12):
`cadl norms-check` and `cadl.norms_check`.

The differential tests run the generated PRISM text (with the interpreter
of `cadl.norms_check`) and `cadl norms-eval` on the same paths of
environment choices and compare the state of every norm after every
observation. CADL_NORMS_CHECK_PATHS sets the number of random paths per
contract and sampling mode (default 20; the long run of the report used
1000). The tests that need PRISM are skipped when `prism` is not on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cadl.norms_check import (
    evaluate_path,
    ACTIVE,
    INACTIVE,
    NormsCheckError,
    PrismModel,
    Translator,
    _IR,
    _compile,
    _tstep,
    all_paths,
    build_model,
    check,
    compare_path,
    base_file,
    base_version,
    ABSENT,
    instance_span,
    format_text,
    prism_expr,
    questions,
    random_paths,
    run_path,
    trace_lines,
)
from cadl.norms_eval import evaluate, load_version, parse_trace
from cadl.parser import parse
from cadl.type_checker import type_check

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
ROBOT = EXAMPLES / "norms_robot_delivery.cadl"
DOUBLE = EXAMPLES / "norms_no_double_assignment.cadl"
CONFLICT = EXAMPLES / "norms_check_conflict.cadl"
PRISM = shutil.which("prism")
needs_prism = pytest.mark.skipif(PRISM is None, reason="PRISM (prism) is not on PATH")
PATHS = int(os.environ.get("CADL_NORMS_CHECK_PATHS", "20"))

HEAD = """\
sos:
  name: T
  extensions:
    - norms: 0.1
  actors:
    - {id: D, role: d}
    - {id: "R[1..3]", role: r}
    - {id: X, role: x}
  contracts:
    - id: C
      parties: [D, "R[*]"]
"""

# within and throughout, suspension and resumption by powers, a
# reparation, a predicate trigger (rising edge) and an until
TEMPORAL = """\
counts_as:
  - when: "D -> R[i] : go"
    creates: started
norms:
  - id: o_within
    kind: obligation
    bearer: "R[i]"
    trigger: started
    content: "within(2s, R[i].level >= 8)"
    deadline: 3s
    until: "D -> R[i] : cancel"
    on_violation: {reparation: o_rep}
  - id: o_rep
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : sorry"
    deadline: 1s
    until: "R[i].level < 3"
  - id: p_through
    kind: prohibition
    bearer: "R[i]"
    trigger: "R[i].level > 5"
    content: "throughout(2s, R[i].level > 5)"
    deadline: 4s
  - id: pw
    kind: power
    bearer: D
    content: "D -> R[i] : pause"
    effects:
      suspend: [o_within, p_through]
      resume: [o_rep]
  - id: pw2
    kind: power
    bearer: D
    content: "D -> R[i] : resume"
    effects:
      resume: [o_within, p_through]
      suspend: [o_rep]
"""

# until, enum and IN, a counts-as rule with if:, a temporal assumption
# (excused verdicts), an until that is a predicate, activate and
# discharge effects
UNTIL = """\
assume:
  - "throughout(3s, D.ok == true)"
counts_as:
  - when: "R[i].level > 5"
    creates: high
    if: "D.mode IN [Auto, Manual]"
norms:
  - id: o_until
    kind: obligation
    bearer: "R[i]"
    trigger: "D.mode == Manual"
    content: "until(R[i].level < 3, D.mode == Auto, 2s)"
    deadline: 1s
  - id: p_high
    kind: prohibition
    bearer: "R[i]"
    trigger: high
    content: "R[i] -> D : boost"
    deadline: 2s
    until: "D.mode == Off"
  - id: late
    kind: obligation
    bearer: D
    content: "D -> R[i] : go"
    deadline: 1s
  - id: waker
    kind: power
    bearer: D
    content: "D -> R[i] : wake"
    effects:
      activate: [late]
      discharge: [o_until]
"""

# a conflict, a reparation shared by two obligations, a reparation that
# cannot be kept, an obligation violated in every instance and a power
# that is never exercised
CONFLICTS = """\
counts_as:
  - when: "R[i] -> D : report"
    creates: reported
norms:
  - id: must_report
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : order"
    content: "R[i] -> D : report"
    deadline: 2s
    on_violation: {reparation: hurry}
  - id: no_report
    kind: prohibition
    bearer: "R[i]"
    content: "R[i] -> D : report"
  - id: must_ack
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : ping"
    content: "R[i] -> D : ack"
    deadline: 2s
    on_violation: {reparation: hurry}
  - id: hurry
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : apology"
    deadline: 0s
  - id: impossible
    kind: obligation
    bearer: "R[i]"
    content: "R[7] -> D : x"
  - id: x_power
    kind: power
    bearer: X
    content: reported
    effects:
      discharge: [must_report]
"""

# wildcard message events, institutional events as content and until,
# a power over an institutional event that suspends and resumes
EVENTS = """\
counts_as:
  - when: "R[*] -> D : done"
    creates: finished
  - when: "D -> R[*] : stop"
    creates: halt
norms:
  - id: all_done
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[*] : start"
    content: finished
    deadline: 3s
    until: halt
  - id: no_early
    kind: prohibition
    bearer: "R[i]"
    trigger: "D -> R[*] : start"
    content: "R[1] -> D : done(early)"
    deadline: 1s
  - id: may_stop
    kind: power
    bearer: D
    content: halt
    effects:
      suspend: [all_done]
      resume: [no_early]
  - id: perm
    kind: permission
    bearer: "R[i]"
    trigger: "R[2] -> D : done"
    until: "D -> R[1] : stop"
    content: "R[1] -> D : anything"
"""


# quantifiers over a set and over its members, a wildcard, an index
# expression j+1, count compared with a literal and with a quantity, max
QUANT = """\
assume:
  - "for all r in R: r.ok != false"
counts_as:
  - when: "exists j in R: R[j].alarm == true"
    creates: alarm
norms:
  - id: stop_all
    kind: obligation
    bearer: D
    trigger: alarm
    content: "count(R[*].moving == false) >= 2"
    deadline: 2s
  - id: no_overload
    kind: prohibition
    bearer: D
    content: "max(R[*].load) > 5"
  - id: capacity
    kind: prohibition
    bearer: D
    content: "R[1].cap < count(r.moving == true for r in R)"
  - id: keep_one
    kind: obligation
    bearer: D
    content: "within(2s, exists j in R: R[j].load == 0 AND R[j+1].load == 0)"
"""


# arithmetic and sum (with --domain), orders and equalities between
# quantities, an order against a text literal
ARITH = """\
norms:
  - id: sum_ok
    kind: obligation
    bearer: D
    trigger: "D -> R[1] : go"
    content: "R[1].x + R[1].y > 3"
    deadline: 1s
  - id: order
    kind: prohibition
    bearer: D
    content: "R[1].a < R[1].b AND R[1].b != R[2].b"
  - id: load
    kind: prohibition
    bearer: D
    content: "sum(R[*].load) > 2"
  - id: names
    kind: prohibition
    bearer: D
    content: "R[1].s < \\"m\\" OR R[1].s >= R[2].s"
  - id: names2
    kind: prohibition
    bearer: D
    content: "R[1].s < R[2].s"
  - id: names3
    kind: prohibition
    bearer: D
    content: "R[1].s == R[2].s"
  - id: objects
    kind: prohibition
    bearer: D
    content: "R[1].o < R[2].o"
  - id: objects2
    kind: prohibition
    bearer: D
    content: "R[1].o == R[2].o"
"""
ARITH_DOMAINS = ["R[1].x=0..2", "R[1].y=0..2", "R[*].load=0..1"]


# one bearer, two instances: docking one robot is forbidden while another
# is docked
MULTI = """\
norms:
  - id: dock_when_low
    kind: obligation
    bearer: D
    trigger: "R[i] -> D : low"
    content: "D -> R[i] : dock"
    deadline: 1s
  - id: one_at_a_time
    kind: prohibition
    bearer: D
    trigger: "R[i] -> D : docked"
    content: "D -> R[*] : dock"
"""
MULTI_KW = {"members": ["R=2"], "instances": [{"i": 1}, {"i": 2}], "starts": "together"}
MULTI_ANY = {"members": ["R=2"], "instances": [{"i": 1}, {"i": 2}]}

# instances that start at observations of their own (F.12): a norm active
# at the start of each instance, a rising edge of a predicate of the
# instance, and a temporal assumption whose interval begins at the start
# of the instance
STAGGER = """\
assume:
  - "within(2s, R[i].ok == true)"
norms:
  - id: greet
    kind: obligation
    bearer: D
    content: "D -> R[i] : dock"
    deadline: 1s
  - id: on_fault
    kind: obligation
    bearer: D
    trigger: "R[i].ok == false"
    content: "D -> R[i] : dock"
    deadline: 1s
  - id: one_at_a_time
    kind: prohibition
    bearer: D
    trigger: "R[i] -> D : docked"
    content: "D -> R[*] : dock"
    deadline: 2s
"""
STARTS = EXAMPLES / "norms_check_starts.cadl"


# two versions of a contract (F.6): carry_over with a shorter deadline
# (violated at the amendment, with its reparation), a changed temporal
# content (candidates dropped), transform with the elapsed time, discharge,
# a removed norm, a new norm without trigger, assumptions that continue,
# start anew, and end
AM_V1 = """\
assume:
  - "throughout(2s, D.ok == true)"
norms:
  - id: carry
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : go"
    content: "R[i] -> D : done"
    deadline: 3s
    on_violation: {reparation: repair}
  - id: repair
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : done"
    deadline: 1s
  - id: watch
    kind: prohibition
    bearer: "R[i]"
    trigger: "D -> R[i] : go"
    content: "within(1s, R[i].v > 5)"
    deadline: 2s
  - id: old_duty
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : ask"
    content: "R[i] -> D : answer"
    deadline: 3s
    on_amendment: {transform: new_duty}
  - id: drop_me
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : ask"
    content: "R[i] -> D : done"
    deadline: 2s
    on_amendment: discharge
  - id: gone
    kind: prohibition
    bearer: "R[i]"
    trigger: "D -> R[i] : ask"
    content: "R[i] -> D : answer"
"""
AM_V2 = """\
assume:
  - "within(2s, D.ok == true)"
norms:
  - id: carry
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : go"
    content: "R[i] -> D : done"
    deadline: 1s
    on_violation: {reparation: repair}
  - id: repair
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : done"
    deadline: 2s
  - id: watch
    kind: prohibition
    bearer: "R[i]"
    trigger: "D -> R[i] : go"
    content: "within(1s, R[i].v > 6)"
    deadline: 2s
  - id: new_duty
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : answer"
    deadline: 3s
  - id: drop_me
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : ask"
    content: "R[i] -> D : done"
    deadline: 2s
  - id: fresh
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : answer"
    deadline: 1s
"""

# amendments together with several instances (F.12): two versions with a
# carried-over deadline and reparation, a transform, a new norm without
# trigger, and an assumption replaced by another one
AM_MULTI_V1 = """\
assume:
  - "throughout(2s, R[i].ok == true)"
norms:
  - id: carry
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : go"
    content: "R[i] -> D : done"
    deadline: 2s
    on_violation: {reparation: repair}
  - id: repair
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : done"
    deadline: 1s
  - id: start_duty
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : done"
    deadline: 3s
    on_amendment: {transform: new_duty}
"""
AM_MULTI_V2 = """\
assume:
  - "within(2s, R[i].ok == true)"
norms:
  - id: carry
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : go"
    content: "R[i] -> D : done"
    deadline: 1s
    on_violation: {reparation: repair}
  - id: repair
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : done"
    deadline: 2s
  - id: new_duty
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : done"
    deadline: 2s
  - id: fresh
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : done"
    deadline: 1s
"""


def make_sos(body: str):
    sos = parse(HEAD + textwrap.indent(body, "      "))
    result = type_check(sos)
    assert result.ok, result.errors
    return sos


_MODELS: dict = {}


def model_of(name: str, **kw):
    """The model of an example file or of one of the contracts above,
    built once per test session."""
    key = (name, json.dumps(kw, sort_keys=True, default=str))
    if key not in _MODELS:
        texts = {"TEMPORAL": TEMPORAL, "UNTIL": UNTIL, "CONFLICTS": CONFLICTS, "EVENTS": EVENTS,
                 "DEADLINE_ZERO": DEADLINE_ZERO, "TYPES": TYPES, "QUANT": QUANT,
                 "ARITH": ARITH, "MIXED": MIXED, "LISTS": LISTS,
                 "MULTI": MULTI, "STAGGER": STAGGER}
        sos = make_sos(texts[name]) if name in texts else load_version(name)
        m = build_model(sos, **kw)
        _MODELS[key] = (sos, m, PrismModel(m.prism_text(), {"early_end": True}))
    return _MODELS[key]


CASES = [
    (str(ROBOT), {"bindings": ["i=2"]}),
    (str(DOUBLE), {"bindings": ["i=1"]}),
    (str(CONFLICT), {"bindings": ["i=1"]}),
    ("TEMPORAL", {"bindings": ["i=1"]}),
    ("UNTIL", {"bindings": ["i=1"]}),
    ("CONFLICTS", {"bindings": ["i=1"]}),
    ("EVENTS", {"bindings": ["i=1"], "members": ["R=2"]}),
    ("TYPES", {"bindings": ["i=1"]}),
    ("MIXED", {"members": ["R=2"]}),
    ("LISTS", {}),
    (str(EXAMPLES / "norms_check_value_types.cadl"), {"bindings": ["i=1"]}),
    ("QUANT", {"members": ["R=2"]}),
    ("ARITH", {"domains": ARITH_DOMAINS}),
    (str(EXAMPLES / "norms_check_arithmetic.cadl"),
     {"members": ["ROBOT=2"], "domains": ["ROBOT[*].load=0..3", "DEPOT.capacity=2..4"]}),
    (str(EXAMPLES / "norms_check_quantifiers.cadl"), {"members": ["ROBOT=2"]}),
]


def case_id(case) -> str:
    return Path(case[0]).stem if case[0].endswith(".cadl") else case[0]


# === Translation ===========================================================

class TestTranslation:
    def test_robot_instance(self):
        _sos, m, _pm = model_of(str(ROBOT), bindings=["i=2"])
        tr = m.tr
        assert [a.text() for a in tr.atoms] == [
            "DISPATCHER -> ROBOT[2] : route_assignment",
            'ROBOT[2] -> DISPATCHER : ack("accepted")',
            "ROBOT[2] -> DISPATCHER : failure_report",
            "DISPATCHER -> ROBOT[2] : replan_command",
        ]
        reps = {q.key: q.reps for q in tr.quantities.values()}
        # one value per class: a class holds the values of every JSON type
        # on which each comparison with a literal has the same value
        assert reps == {"ROBOT[2].battery": [None, 0, 10],
                        "ROBOT[2].status": [None, "InTransit", "OTHER"],
                        "DISPATCHER.is_operational": [None, 0, True]}
        # 2**4 message sets, 3 classes of the prohibition's content (true,
        # false, undefined) and 3 of the assumption
        assert len(tr.choices) == 16 * 3 * 3
        assert tr.step_ms == 1000
        assert m.H == 2 + (5 + 2) + (2 + 2) + 2
        assert all(n.status == "model" for n in tr.norms)
        assert tr.members["ROBOT"] == [1, 2]
        assert tr.bindings == {"i": 2, "N": 2, "M": 1}      # N, M: the members
        # the sets with a symbolic bound and no --members: one note each
        assert len(tr.notes) == 2 and all("give --members" in n for n in tr.notes)

    def test_default_binding_is_reported(self):
        _sos, m, _pm = model_of(str(ROBOT))
        assert m.tr.bindings["i"] == 1
        assert any("index i is not bound" in n for n in m.tr.notes)

    def test_quantified_norm_is_not_translated(self):
        _sos, m, _pm = model_of(str(DOUBLE), bindings=["i=1"])
        n = m.tr.by_id["no_double_assignment"]
        assert n.status == "untranslated"
        assert "request fields are not translated" in n.reason
        assert m.tr.by_id["acknowledge"].status == "model"

    def test_dependants_of_untranslated_norms(self):
        sos = make_sos("""\
norms:
  - id: a
    kind: obligation
    bearer: "R[i]"
    content: "R[i].x + 1 == R[i].y"
    on_violation: {reparation: b}
  - id: b
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : m"
""")
        tr = build_model(sos, bindings={"i": 1}).tr
        assert tr.by_id["a"].status == "untranslated"
        assert "--domain" in tr.by_id["a"].reason
        assert tr.by_id["b"].status == "untranslated"
        assert "reparation of a" in tr.by_id["b"].reason

    @pytest.mark.parametrize("kw, message", [
        ({"step": "3s"}, "does not divide"),
        ({"horizon": 5000}, "refuses more than"),
        ({"members": ["ROBOT=0"]}, "at least one member"),
        ({"members": ["ROBOT=1"], "bindings": ["i=2"]}, "not among the members"),
        ({"members": ["PLANE=2"]}, "not a declared set"),
        ({"bindings": ["i:2"]}, "name=integer"),
    ])
    def test_refusals(self, kw, message):
        with pytest.raises(NormsCheckError, match=message):
            build_model(load_version(ROBOT), **kw)

    def test_too_many_choices_is_refused(self):
        sos = make_sos("norms:\n" + "".join(
            f"  - id: n{k}\n    kind: prohibition\n    bearer: D\n"
            f"    content: \"D -> R[*] : m{k}\"\n" for k in range(5)))
        with pytest.raises(NormsCheckError, match="distinguishable choices"):
            build_model(sos, bindings={"i": 1})

    def test_predicates_are_evaluated_by_norms_eval(self):
        _sos, m, _pm = model_of(str(ROBOT), bindings=["i=2"])
        tr = m.tr
        p = next(x.expr for x in tr.preds if "battery" in x.text)
        ev = tr._oracle
        assert ev([p], {"ROBOT[2].battery": 9, "ROBOT[2].status": "InTransit"}) == ["T"]
        assert ev([p], {"ROBOT[2].battery": 11, "ROBOT[2].status": None}) == ["F"]
        assert ev([p], {"ROBOT[2].battery": 9, "ROBOT[2].status": None}) == ["U"]
        # values of another type: a text is not ordered against 10, a
        # boolean is (as 0 or 1), a list is neither
        assert ev([p], {"ROBOT[2].battery": "low", "ROBOT[2].status": "InTransit"}) == ["U"]
        assert ev([p], {"ROBOT[2].battery": True, "ROBOT[2].status": "InTransit"}) == ["T"]
        assert ev([p], {"ROBOT[2].battery": 5, "ROBOT[2].status": 3}) == ["U"]

    def test_temporal_steps(self):
        # within: decided true at once, false only after the interval
        assert _tstep("within", 1, False, ("T",)) == (0, "T")
        assert _tstep("within", 1, False, ("U",)) == (2, None)
        assert _tstep("within", 2, True, ()) == (0, "U")
        assert _tstep("within", 1, True, ()) == (0, "F")
        assert _tstep("throughout", 1, False, ("F",)) == (0, "F")
        assert _tstep("throughout", 1, True, ()) == (0, "T")
        # until: q with an undefined prefix is not enough
        assert _tstep("until", 1, False, ("U", "F")) == (2, None)
        assert _tstep("until", 2, False, ("T", "T")) == (4, None)
        assert _tstep("until", 4, True, ()) == (0, "U")
        assert _tstep("until", 1, False, ("F", "F")) == (0, "F")


# === Expressions ===========================================================

class TestExpressions:
    def test_folding(self):
        ir = _IR({"s": (0, 7), "e": (0, 4)}, {"b"})
        s = ir.V("s")
        x = ir.ITE(ir.V("b"), ir.C(ACTIVE), s)
        assert ir.EQ(x, INACTIVE) is ir.AND(ir.NOT(ir.V("b")), ir.EQ(s, 0))
        assert ir.EQ(ir.C(3), 3) is ir.TRUE
        assert ir.LE(ir.ADD(ir.V("e"), 1), 4) is ir.LE(ir.V("e"), 3)
        assert ir.LE(ir.V("e"), 4) is ir.TRUE
        assert ir.OR(ir.V("b"), ir.NOT(ir.V("b"))) is ir.TRUE
        assert prism_expr(ir.ITE(ir.V("b"), ir.C(1), s)) == "(b ? 1 : s)"

    def test_split_and_tidy_keep_the_value(self):
        ir = _IR({"s": (0, 3)}, {"a", "b"})
        s = ir.V("s")
        e = ir.ITE(ir.AND(ir.V("a"), ir.EQ(s, 1)), ir.C(2),
                   ir.ITE(ir.OR(ir.V("b"), ir.EQ(s, 2)), ir.C(3), s))
        f = ir.tidy(ir.split(e, limit=1))
        for sv in range(4):
            for a in (False, True):
                for b in (False, True):
                    env = {"s": sv, "a": a, "b": b}
                    assert _compile(prism_expr(e))(env) == _compile(prism_expr(f))(env)


# === The PRISM text ========================================================

class TestPrismText:
    def test_header_labels_and_ranges(self):
        _sos, m, pm = model_of(str(ROBOT), bindings=["i=2"])
        text = m.prism_text()
        assert "\nmdp\n" in text and "const bool early_end;" in text
        assert 'label "v_0_acknowledge" = st_0_acknowledge=4 | st_0_acknowledge=5;' in text
        assert 'label "x_4_reassign" = ex_4_reassign;' in text
        assert pm.initial()["step"] == 0
        # two commands per choice and end, one for the first observation
        # and one for the others, and the loop of the ended instance
        assert len(pm.commands) == 4 * len(m.tr.choices) + 1

    def test_exported_props(self):
        _sos, m, _pm = model_of(str(CONFLICT), bindings=["i=1"])
        from cadl.norms_check import props_text
        any_end, full = props_text(m, "any"), props_text(m, "full")
        assert 'Pmax=? [ F "v_0_report_status" ]' in any_end
        assert 'Pmin=? [ F "v_0_report_status" ]' in full
        assert "P>=1 [ F (" in full
        assert {q.key for q in questions(m)} == {
            "activatable", "violable", "fulfillable", "exercisable", "reparation",
            "unavoidable", "trap", "conflict"}


# === Differential tests against norms-eval =================================

@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_differential_random_paths(case):
    """The generated model and norms-eval agree, at every observation, on
    the state of every translated norm, and at the end on the verdicts,
    the exercises of powers and the activations of reparations."""
    name, kw = case
    _sos, m, pm = model_of(name, **kw)
    paths = (random_paths(m.tr, PATHS, seed=11)
             + random_paths(m.tr, PATHS // 2, seed=12, p_message=0.6, p_end=0.05)
             + random_paths(m.tr, PATHS // 2, seed=13, p_message=0.15, full_length=True)
             + random_paths(m.tr, PATHS // 2, seed=14, p_message=0.1, p_end=0.03, p_keep=0.8))
    bad = []
    for p in paths:
        d = compare_path(m, pm, p)
        if d:
            bad.append((d, p))
    assert not bad, bad[:3]


def test_differential_all_short_paths():
    """Every path of at most two observations of DEADLINE_ZERO (16 choices)."""
    for name, kw in (("DEADLINE_ZERO", {"bindings": ["i=1"]}),):
        _sos, m, pm = model_of(name, **kw)
        paths = all_paths(m.tr, 2)
        assert len(paths) == len(m.tr.choices) * (len(m.tr.choices) + 1)
        for p in paths:
            assert not compare_path(m, pm, p), p


def test_until_predicate_is_a_rising_edge():
    """A reparation activated while its until: predicate already holds is
    not discharged by it at the next observation (the predicate must
    become true, F.5): a path that random sampling seldom produces."""
    _sos, m, pm = model_of("TEMPORAL", bindings=["i=1"])
    tr = m.tr
    go = next(i for i, a in enumerate(tr.atoms) if a.name == "go")

    def choice(msgs, test):
        return next(c.index for c in tr.choices
                    if c.messages == msgs and test(c.values.get("R[1].level")))

    def low_number(v):
        return v is True or (type(v) in (int, float) and v < 3)     # true < 3 as well

    none, low = choice((), lambda v: v is None), choice((), low_number)
    path = ([(choice((go,), lambda v: v is None), False)] + [(none, False)] * 5
            + [(low, False), (low, False), (none, True)])
    assert not compare_path(m, pm, path)
    states = run_path(pm, path)
    assert states[7]["st_1_o_rep"] == ACTIVE


def test_quantifiers_over_the_members():
    """Quantifiers, aggregates and wildcards range over the members of the
    instance; a member beyond --members of a set with integer bounds is in
    the domain without a value, and the report says so."""
    _sos, m, pm = model_of("QUANT", members=["R=2"])
    tr = m.tr
    assert {k for k in tr.quantities} == {f"R[{j}].{q}" for j in (1, 2)
                                          for q in ("ok", "alarm", "moving", "load")} | {"R[1].cap"}
    assert any("R[3..3] belong to the domain" in n for n in tr.notes)
    assert not any("index j" in n for n in tr.notes)      # a quantifier variable
    for p in all_paths(tr, 1):
        assert not compare_path(m, pm, p), p


@needs_prism
def test_conflict_through_quantifiers():
    sos, m, _pm = model_of(str(EXAMPLES / "norms_check_quantifiers.cadl"), members=["ROBOT=2"])
    report = check(m, PRISM)
    assert [c["norms"] for c in report["conflicts"]] == [["halt_fleet", "keep_patrol"]]
    assert all(w["confirmed"] for c in report["conflicts"] for w in c["witnesses"])
    for c in report["conflicts"]:
        for w in c["witnesses"]:
            r = evaluate(sos, parse_trace(w["trace"]))
            states = {n["id"]: n["state"] for n in r["norms"]}
            assert states[w["keeps"]] != "violated"


def test_arithmetic_needs_a_domain():
    """Arithmetic, sum and avg need --domain; orders and equalities between
    quantities and orders against texts do not. The report says for which
    traces the answers hold."""
    tr = build_model(make_sos(ARITH), bindings={"i": 1}).tr
    assert tr.by_id["sum_ok"].status == "untranslated"
    assert "--domain R[1].x=0..10" in tr.by_id["sum_ok"].reason
    assert tr.by_id["load"].status == "untranslated"
    assert tr.by_id["order"].status == "model" and tr.by_id["names"].status == "model"
    tr = model_of("ARITH", domains=ARITH_DOMAINS)[1].tr
    assert all(n.status == "model" for n in tr.norms)
    assert tr.quantities["R[1].x"].reps[1:4] == [0, 1, 2]
    assert any("R[1].x takes the integers 0..2" in n for n in tr.notes)
    # three quantities ordered against each other: three values in every gap
    assert tr.quantities["R[1].a"].order == 3
    with pytest.raises(NormsCheckError, match="--domain takes"):
        build_model(make_sos(ARITH), domains=["R[1].x=3..1"])


def test_several_instances_differential():
    """Two instances over the same observations: each instance's trace, up
    to its own end, is evaluated by norms-eval with its bindings and
    compared with the model at every observation."""
    _sos, m, pm = model_of("MULTI", **MULTI_KW)
    tr = m.tr
    assert [n.uid for n in m.norms] == ["dock_when_low@1", "one_at_a_time@1",
                                        "dock_when_low@2", "one_at_a_time@2"]
    paths = (random_paths(tr, PATHS * 2, seed=21, instances=2)
             + random_paths(tr, PATHS, seed=22, p_message=0.15, p_end=0.05, instances=2)
             + random_paths(tr, PATHS, seed=23, full_length=True, instances=2)
             + all_paths(tr, 1, instances=2))
    for p in paths:
        assert not compare_path(m, pm, p), p
    # an instance that ends first does not change afterwards
    p = [(0, 1)] + [(0, 0)] * 2 + [(0, 2)]
    states = run_path(pm, p)
    assert states[0]["k1_ended"] and not states[0]["k2_ended"]
    assert len({s["st_k1_0_dock_when_low"] for s in states}) == 1


def test_instances_start_at_observations_of_their_own():
    """--starts any (the default): the environment chooses the observation
    at which each instance starts; before it and after its end the
    instance does not change, its trace begins at its start, and the norms
    active at the start, rising edges and the intervals of assumptions
    count from there."""
    _sos, m, pm = model_of("STAGGER", **MULTI_ANY)
    tr = m.tr
    assert m.staggered and m.has("k1_started") and m.has("k2_ob")
    paths = (random_paths(tr, PATHS * 2, seed=41, instances=2)
             + random_paths(tr, PATHS, seed=42, p_message=0.15, p_end=0.05, p_start=0.2,
                            instances=2)
             + random_paths(tr, PATHS, seed=43, full_length=True, p_keep=0.7, instances=2)
             + all_paths(tr, 1, instances=2))
    late = 0
    for p in paths:
        assert not compare_path(m, pm, p), p
        late += any(instance_span(t, p)[0] > 0 for t in m.trs)
    assert late > PATHS
    # every order of starts and ends over four observations, with the
    # choice without messages and values and one with a fault of R[2]
    fault = next(c.index for c in tr.choices if not c.messages
                 and c.values.get("R[2].ok") is False and c.values.get("R[1].ok") is None)
    short = all_paths(tr, 4, instances=2, choices=[0, fault])
    assert len(short) > 600
    for p in short:
        assert not compare_path(m, pm, p), p
    # instance 2 starts at observation 2: its trace begins there, at t=2
    p = [(0, 1 << 2), (0, 0), (fault, 2 << 2), (0, 3)]
    lines = trace_lines(m.trs[1], p)
    assert [json.loads(x)["t"] for x in lines] == [2, 3]
    assert not compare_path(m, pm, p)
    states = run_path(pm, p)
    assert states[1]["st_k2_0_greet"] == INACTIVE and states[2]["st_k2_0_greet"] == ACTIVE


def test_starts_together_or_any():
    """--starts together keeps the instances starting at the first
    observation; the model has fewer commands."""
    _sos, any_, _pm = model_of("MULTI", **MULTI_ANY)
    _sos, tog, _pm = model_of("MULTI", **MULTI_KW)
    assert any_.staggered and not tog.staggered
    assert len(tog.commands) < len(any_.commands)
    assert len(any_.commands) == len(any_.tr.choices) * (8 + 16)
    with pytest.raises(NormsCheckError, match="--starts"):
        build_model(make_sos(MULTI), members=["R=2"], instances=[{"i": 1}, {"i": 2}],
                    starts="later")


@needs_prism
def test_conflict_that_needs_staggered_starts():
    """A reparation of one instance in force while another instance
    starts: a conflict that only instances with starts of their own have."""
    sos = load_version(str(STARTS))
    report = check(build_model(sos, members=["ROBOT=2"], instances=2), PRISM)
    pairs = {tuple(c["norms"]) for c in report["conflicts"]}
    assert pairs == {("grant_on_start@1", "lockout@2"), ("lockout@1", "grant_on_start@2")}
    assert report["unconfirmed"] == []
    for c in report["conflicts"]:
        for w in c["witnesses"]:
            starts = {k: json.loads(lines[0])["t"] for k, lines in w["traces"].items()}
            assert len(set(starts.values())) == 2           # different starts
            for k, lines in w["traces"].items():
                r = evaluate(sos, parse_trace(lines))
                states = {n["id"]: n["state"] for n in r["norms"]}
                keep_id, keep_k = w["keeps"].split("@")
                if keep_k == k:
                    assert states[keep_id] != "violated"
    together = check(build_model(sos, members=["ROBOT=2"], instances=2, starts="together"),
                     PRISM)
    assert together["conflicts"] == []


@needs_prism
def test_conflict_across_instances():
    sos, m, _pm = model_of("MULTI", **MULTI_KW)
    report = check(m, PRISM)
    pairs = {tuple(c["norms"]) for c in report["conflicts"]}
    assert ("dock_when_low@1", "one_at_a_time@2") in pairs
    assert ("one_at_a_time@1", "dock_when_low@2") in pairs
    for c in report["conflicts"]:
        for w in c["witnesses"]:
            assert w["confirmed"] and set(w["traces"]) == {"1", "2"}
            for k, lines in w["traces"].items():
                r = evaluate(sos, parse_trace(lines))
                assert r["instance"]["bindings"]["i"] == int(k)
                states = {n["id"]: n["state"] for n in r["norms"]}
                keep_id, keep_k = w["keeps"].split("@")
                if keep_k == k:
                    assert states[keep_id] != "violated"


def _amend_model():
    key = ("AMEND", "")
    if key not in _MODELS:
        v1, v2 = make_sos(AM_V1), make_sos(AM_V2)
        m = build_model(v1, bindings={"i": 1}, versions=[("v2.cadl", v2)])
        _MODELS[key] = ((v1, v2), m, PrismModel(m.prism_text(), {"early_end": True}))
    return _MODELS[key]


def test_amendments_differential():
    """Amendments as a choice of the environment (F.6): every path is
    compared with norms-eval reading the same trace with its amend line."""
    (v1, v2), m, pm = _amend_model()
    tr = m.tr
    assert [n.id for n in m.norms] == ["carry", "repair", "watch", "old_duty", "drop_me",
                                       "gone", "new_duty", "fresh"]
    paths = (random_paths(tr, PATHS * 2, seed=31, p_amend=0.3, p_message=0.35)
             + random_paths(tr, PATHS, seed=32, p_keep=0.8, p_message=0.15, p_end=0.04,
                            p_amend=0.2)
             + random_paths(tr, PATHS // 2, seed=33, full_length=True, p_amend=0.15))
    effects = set()
    for p in paths:
        assert not compare_path(m, pm, p), p
        for a in evaluate_path(tr, p)["amendments"]:
            effects.update((e["norm"], e["effect"]) for e in a["effects"])
    # the paths reach the cases of F.6
    assert {("carry", "carry_over"), ("drop_me", "discharge"), ("gone", "removed"),
            ("old_duty", "transform"), ("new_duty", "activated"),
            ("fresh", "added")} <= effects
    for p in all_paths(tr, 1):
        assert not compare_path(m, pm, p), p


def test_amendment_refusals():
    v1 = make_sos(AM_V1)
    other = make_sos(AM_V2.replace("kind: prohibition", "kind: obligation", 1))
    with pytest.raises(NormsCheckError, match="changes its kind"):
        build_model(v1, bindings={"i": 1}, versions=[("o.cadl", other)])



def _amend_multi_model():
    key = ("AMEND_MULTI", "")
    if key not in _MODELS:
        v1, v2 = make_sos(AM_MULTI_V1), make_sos(AM_MULTI_V2)
        m = build_model(v1, members=["R=2"], instances=[{"i": 1}, {"i": 2}],
                        versions=[("v2.cadl", v2)])
        _MODELS[key] = ((v1, v2), m, PrismModel(m.prism_text(), {"early_end": True}))
    return _MODELS[key]


def test_amendments_of_several_instances_differential():
    """--version with several instances: one amendment applies to every
    running instance at the same observation (each trace has the amend
    line at that observation); an instance that starts after it is an
    instance of the new version (its trace is evaluated with that file)."""
    (v1, v2), m, pm = _amend_multi_model()
    tr = m.tr
    assert m.versioned and m.staggered
    assert [n.uid for n in m.norms] == [
        "carry@1", "repair@1", "start_duty@1", "new_duty@1", "fresh@1",
        "carry@2", "repair@2", "start_duty@2", "new_duty@2", "fresh@2"]
    paths = (random_paths(tr, PATHS * 3, seed=51, instances=2, p_amend=0.3)
             + random_paths(tr, PATHS * 2, seed=52, p_message=0.15, p_end=0.05, instances=2,
                            p_amend=0.2, p_keep=0.7)
             + random_paths(tr, PATHS, seed=53, full_length=True, instances=2, p_amend=0.2,
                            p_start=0.15))
    late, effects = 0, set()
    for p in paths:
        assert not compare_path(m, pm, p), p
        late += any(base_version(t, p) > 0 for t in m.trs)
        for t in m.trs:
            for a in evaluate_path(t, p).get("amendments", []):
                effects.update((e["norm"], e["effect"]) for e in a["effects"])
    assert late >= PATHS
    assert {("carry", "carry_over"), ("start_duty", "transform"), ("new_duty", "activated"),
            ("fresh", "added")} <= effects
    # the amendment at observation 1 reaches instance 1 (running) and not
    # instance 2, which starts at observation 2 as an instance of v2
    a = len(tr.choices)
    p = [(0, 1 << 2), (a, 0), (0, 2 << 2), (0, 3)]
    assert not compare_path(m, pm, p)
    assert [json.loads(x).get("amend") for x in trace_lines(m.trs[0], p)] == [
        None, "v2.cadl", None, None]
    assert all("amend" not in x for x in trace_lines(m.trs[1], p))
    assert base_file(m.trs[1], p) == "v2.cadl" and base_file(m.trs[0], p) is None
    states = run_path(pm, p)
    assert states[2]["st_k2_2_start_duty"] == ABSENT          # not a norm of v2
    assert states[2]["st_k2_4_fresh"] == ACTIVE               # active at the start


def test_three_versions_of_several_instances():
    """A sequence of amendments with two instances: an instance that
    starts after the first amendment is an instance of version 2, and
    its trace names only the later amendment."""
    v1 = load_version(str(EXAMPLES / "norms_check_amend_v1.cadl"))
    v2 = load_version(str(EXAMPLES / "norms_check_amend_v2.cadl"))
    m = build_model(v1, members=["ROBOT=2"], instances=2,
                    versions=[("v2.cadl", v2), ("v3.cadl", v1)])
    pm = PrismModel(m.prism_text(), {"early_end": True})
    tr = m.tr
    paths = (random_paths(tr, PATHS * 2, seed=61, instances=2, p_amend=0.35)
             + random_paths(tr, PATHS, seed=62, instances=2, p_amend=0.25, p_start=0.15,
                            p_end=0.05))
    late = 0
    for p in paths:
        assert not compare_path(m, pm, p), p
        late += any(base_version(t, p) == 1 and any(
            "amend" in x for x in trace_lines(t, p)) for t in m.trs)
    assert late >= 2

@needs_prism
def test_conflicts_after_one_amendment_of_several_instances():
    v1 = load_version(str(EXAMPLES / "norms_check_amend_v1.cadl"))
    v2 = str(EXAMPLES / "norms_check_amend_v2.cadl")
    m = build_model(v1, members=["ROBOT=2"], instances=2, versions=[v2])
    report = check(m, PRISM)
    assert report["unconfirmed"] == []
    pairs = {tuple(c["norms"]) for c in report["conflicts"]}
    assert {("serve@1", "curfew@2"), ("curfew@1", "serve@2")} <= pairs
    for c in report["conflicts"]:
        for w in c["witnesses"]:
            assert w["confirmed"]
            amends = {k: [json.loads(x).get("amend") for x in lines]
                      for k, lines in w["traces"].items()}
            # the instances running at the amendment are amended at the same time
            times = {k: [json.loads(x)["t"] for x, a in zip(w["traces"][k], v) if a]
                     for k, v in amends.items()}
            assert len({tuple(t) for t in times.values() if t}) == 1


AM_SMALL_V1 = """\
norms:
  - id: carry
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : go"
    content: "R[i] -> D : done"
    deadline: 2s
  - id: old_duty
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : ask"
    content: "R[i] -> D : answer"
    deadline: 2s
    on_amendment: {transform: new_duty}
"""
AM_SMALL_V2 = """\
norms:
  - id: carry
    kind: obligation
    bearer: "R[i]"
    trigger: "D -> R[i] : go"
    content: "R[i] -> D : done"
    deadline: 1s
  - id: new_duty
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : answer"
    deadline: 2s
  - id: fresh
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : done"
    deadline: 1s
"""


@needs_prism
def test_questions_across_an_amendment():
    v1, v2 = make_sos(AM_SMALL_V1), make_sos(AM_SMALL_V2)
    m = build_model(v1, bindings={"i": 1}, versions=[("v2.cadl", v2)])
    report = check(m, PRISM)
    assert report["unconfirmed"] == []
    norms = {n["id"]: n for n in report["norms"]}
    assert norms["new_duty"]["versions"] == [2]
    assert norms["fresh"]["activatable"]["answer"]          # only after an amendment
    w = norms["fresh"]["activatable"]["witness"]
    assert any(line.find('"amend"') >= 0 for line in w["trace"])
    r = evaluate(v1, parse_trace(w["trace"]), versions={"v2.cadl": v2})
    assert {n["id"]: n for n in r["norms"]}["fresh"]["activated_at"] is not None


def test_paths_cover_the_semantics():
    """The random paths of TEMPORAL reach the cases that make the
    translation hard: suspended norms with pending candidates, contents
    decided after the observation of the candidate, settlement with
    undecided contents, violations excused by a failed assumption."""
    seen = set()
    for name in ("TEMPORAL", "UNTIL"):
        sos, m, pm = model_of(name, bindings=["i=1"])
        for p in random_paths(m.tr, 70, seed=5, p_message=0.4, p_end=0.08):
            lines = trace_lines(m.tr, p)
            r = evaluate(sos, parse_trace(lines))
            for n in r["norms"]:
                for h in n["history"]:
                    if h["state"] == "suspended":
                        seen.add("suspended")
                    if " at t=" in h["reason"] and "held" in h["reason"]:
                        seen.add("decided later")
                if n["reason"].startswith("not decided"):
                    seen.add("undecided at the end")
                if n["verdict"] == "excused":
                    seen.add("excused")
    assert seen == {"suspended", "decided later", "undecided at the end", "excused"}


# === Model checking with PRISM =============================================

def _explicit(m, early_end: bool):
    """The reachable states of the model and the questions of F.12
    answered by explicit fixpoints over them, without PRISM."""
    pm = PrismModel(m.prism_text(), {"early_end": early_end})
    init = pm.key(pm.initial())
    succ, todo = {}, [init]
    while todo:
        k = todo.pop()
        if k in succ:
            continue
        s = pm.from_key(k)
        succ[k] = [pm.key(pm.apply(c, s)) for c in pm.enabled(s)]
        todo += succ[k]
    lab = {name: {k for k in succ if fn(pm.from_key(k))} for name, fn in pm.labels.items()}

    def ef(target):
        out = set(target)
        changed = True
        while changed:
            changed = False
            for k, nxt in succ.items():
                if k not in out and any(x in out for x in nxt):
                    out.add(k)
                    changed = True
        return out

    def af(target):
        out = set(target)
        changed = True
        while changed:
            changed = False
            for k, nxt in succ.items():
                if k not in out and nxt and all(x in out for x in nxt) and set(nxt) != {k}:
                    out.add(k)
                    changed = True
        return out

    def eg(safe):
        out = set(safe)
        changed = True
        while changed:
            changed = False
            for k in list(out):
                if not any(x in out for x in succ[k]):
                    out.discard(k)
                    changed = True
        return out

    _explicit.succ = succ
    return init, set(succ), lab, ef, af, eg


@needs_prism
def test_prism_answers_agree_with_explicit_fixpoints():
    sos, m, _pm = model_of("CONFLICTS", bindings=["i=1"])
    report = check(m, PRISM)
    norms = {n["id"]: n for n in report["norms"]}
    init, states, lab, ef, af, eg = _explicit(m, True)
    for n in m.norms:
        assert norms[n.id]["activatable"]["answer"] == (init in ef(lab[f"a_{n.stem}"]))
        if n.judged:
            assert norms[n.id]["violable"]["answer"] == (init in ef(lab[f"v_{n.stem}"]))
            assert norms[n.id]["fulfillable"]["answer"] == (init in ef(lab[f"f_{n.stem}"]))
        if n.kind == "power":
            assert norms[n.id]["exercisable"]["answer"] == (init in ef(lab[f"x_{n.stem}"]))
    init, states, lab, ef, af, eg = _explicit(m, False)
    judged = [n for n in m.norms if n.judged]
    for n in judged:
        v = lab[f"v_{n.stem}"]
        assert norms[n.id]["unavoidable"]["answer"] == (init in af(v))
        j, doomed = lab[f"j_{n.stem}"], v | af(v)
        succ = _explicit.succ
        # some choice activates n, and every choice that activates it leads
        # to a state from which every continuation violates it
        trap = {k for k, nxt in succ.items()
                if k not in lab["end"] and any(x in j for x in nxt)
                and all(x in doomed for x in nxt if x in j)}
        assert norms[n.id]["keepable"]["answer"] == (init not in ef(trap))
    found = set()
    for i, a in enumerate(judged):
        for b in judged[i + 1:]:
            va, vb = lab[f"v_{a.stem}"], lab[f"v_{b.stem}"]
            cs = (states - va - vb) & eg(states - va) & eg(states - vb) & af(va | vb)
            if init in ef(cs):
                found.add((a.id, b.id))
    assert found == {tuple(c["norms"]) for c in report["conflicts"]}
    assert ("must_report", "no_report") in found
    assert norms["impossible"]["unavoidable"]["answer"]
    assert not norms["hurry"]["keepable"]["answer"]
    assert not norms["x_power"]["exercisable"]["answer"]
    assert report["unconfirmed"] == []


def _claims(report: dict):
    """(trace lines, check) for every witness of a report."""
    for n in report["norms"]:
        for key, entry in n.items():
            w = entry.get("witness") if isinstance(entry, dict) else None
            if w:
                yield n["id"], key, w
    for c in report["conflicts"]:
        for w in c["witnesses"]:
            yield c["norms"], "conflict", w


@needs_prism
def test_every_witness_is_confirmed_by_norms_eval():
    """Each "possible" answer comes with a trace; norms-eval, run here
    through its public API on the trace text, gives the claimed verdict."""
    sos, m, _pm = model_of(str(CONFLICT), bindings=["i=1"])
    report = check(m, PRISM)
    count = 0
    for subject, key, w in _claims(report):
        r = evaluate(sos, parse_trace(w["trace"]))
        norms = {n["id"]: n for n in r["norms"]}
        if key == "conflict":
            assert norms[w["keeps"]]["state"] != "violated"
            other = [x for x in subject if x != w["keeps"]][0]
            assert norms[other]["state"] == "violated"
        elif key in ("violable", "unavoidable", "keepable"):
            assert norms[subject]["state"] == "violated"
        elif key == "fulfillable":
            assert norms[subject]["state"] == "fulfilled"
        elif key == "activatable":
            assert norms[subject]["activated_at"] is not None
        elif key == "reparation":
            assert any(v["norm"] == subject and v["reparation_activated"]
                       for v in r["violations"])
        elif key == "exercisable":
            assert any(e["kind"] == "power" and e.get("norm") == subject
                       for e in r["timeline"])
        assert w["confirmed"]
        count += 1
    assert count >= 12
    assert report["findings"] == [
        "apologise can be activated in a state from which every continuation violates it",
        "the power audit is never exercised",
        "report_status and radio_silence are in conflict",
        "apologise and radio_silence are in conflict",
    ]
    text = format_text(report)
    assert "report_status / radio_silence" in text and "NOT CONFIRMED" not in text


@needs_prism
def test_interpreter_agrees_with_the_prism_simulator(tmp_path):
    """Paths that the PRISM simulator generates are replayed by the
    interpreter: the same commands give the same states."""
    for name, kw in (("TEMPORAL", {"bindings": ["i=1"]}), (str(ROBOT), {"bindings": ["i=2"]})):
        _sos, m, pm = model_of(name, **kw)
        mp = tmp_path / "m.prism"
        mp.write_text(m.prism_text(), encoding="utf-8")
        out = tmp_path / "path.csv"
        subprocess.run([PRISM, str(mp), "-const", "early_end=true", "-simpath",
                        f"{m.H + 3},sep=comma,loopcheck=false", str(out)],
                       capture_output=True, text=True, timeout=120, check=True)
        rows = [r.split(",") for r in out.read_text().splitlines()]
        head = rows[0]
        s = pm.initial()
        steps = 0
        for row in rows[2:]:
            label = row[0].strip("[]")
            if label in ("instance", ""):
                break
            s = pm.apply(pm.by_label(s, label), s)
            for name_, val in zip(head[2:], row[2:]):
                want = {"true": True, "false": False}.get(val, None)
                want = int(val) if want is None else want
                assert s[name_] == want, (label, name_)
            steps += 1
        assert steps >= 1


# === The command ============================================================

def cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "cadl.cli", "norms-check", *args],
                          capture_output=True, text=True, env=env, timeout=300)


@needs_prism
def test_cli_export_and_json(tmp_path):
    p = cli(str(CONFLICT), "--bindings", "i=1", "--export", str(tmp_path), "--format", "json")
    assert p.returncode == 1, p.stderr                  # findings
    report = json.loads(p.stdout)
    assert report["checked"] and report["unconfirmed"] == []
    names = {Path(f).name for f in report["exported"]}
    assert {"STATUS_REPORTING.prism", "STATUS_REPORTING_any.props",
            "STATUS_REPORTING_full.props"} <= names
    w = tmp_path / "witness_conflict_report_status_radio_silence_keep_radio_silence.jsonl"
    assert w.exists()
    ev = subprocess.run([sys.executable, "-m", "cadl.cli", "norms-eval", str(CONFLICT),
                         "--trace", str(w), "--format", "json"],
                        capture_output=True, text=True, timeout=120)
    r = json.loads(ev.stdout)
    states = {n["id"]: n["state"] for n in r["norms"]}
    assert states["report_status"] == "violated" and states["radio_silence"] != "violated"


def test_cli_without_model_checker(tmp_path):
    env = dict(os.environ, PATH=str(Path(sys.executable).parent))
    p = cli(str(CONFLICT), "--bindings", "i=1", "--export", str(tmp_path), env=env)
    assert p.returncode == 0
    assert "Not checked: no model checker found" in p.stdout
    assert "norms-check: no model checker found" in p.stderr
    assert (tmp_path / "STATUS_REPORTING.prism").exists()
    pm = PrismModel((tmp_path / "STATUS_REPORTING.prism").read_text(), {"early_end": True})
    assert run_path(pm, [(0, True)])[-1]["ended"] is True


DEADLINE_ZERO = """\
norms:
  - id: answer
    kind: obligation
    bearer: "R[1]"
    trigger: "D -> R[1] : ask"
    content: "R[1] -> D : reply"
    deadline: 0s
  - id: at_once
    kind: obligation
    bearer: "R[1]"
    content: "R[1] -> D : hello"
    deadline: 0s
  - id: owed
    kind: obligation
    bearer: "R[1]"
    trigger: "D -> R[1] : ask"
    content: "R[1] -> D : reply"
    deadline: 1s
    on_violation: {reparation: sorry}
  - id: sorry
    kind: obligation
    bearer: "R[1]"
    content: "R[1] -> D : apology"
    deadline: 0s
"""


@needs_prism
def test_deadline_zero_obligations_can_be_kept_when_triggered():
    """An obligation with a deadline of 0 s can be kept at every activation
    by a trigger or by the start of the instance (the content may come in
    the same observation); as a reparation, first judged at the next
    observation, it cannot."""
    sos = make_sos(DEADLINE_ZERO)
    m = build_model(sos, bindings={"i": 1})
    report = check(m, PRISM)
    norms = {n["id"]: n for n in report["norms"]}
    assert norms["answer"]["keepable"]["answer"] is True
    assert norms["at_once"]["keepable"]["answer"] is True
    assert norms["owed"]["keepable"]["answer"] is True
    assert norms["sorry"]["keepable"]["answer"] is False
    w = norms["sorry"]["keepable"]["witness"]
    assert w["confirmed"] and w["activating_choices"] >= 1
    assert w["choices_checked"] == len(m.tr.choices) and not w["sampled"]
    assert "rests on the model checker" in w["confirms"]
    r = evaluate(sos, parse_trace(w["trace"]))
    assert {n["id"]: n["state"] for n in r["norms"]}["sorry"] == "violated"
    assert report["findings"] == [
        "sorry can be activated in a state from which every continuation violates it"]
    init, states, lab, ef, af, eg = _explicit(m, False)
    succ = _explicit.succ
    for n in m.norms:
        v, j = lab[f"v_{n.stem}"], lab[f"j_{n.stem}"]
        doomed = v | af(v)
        trap = {k for k, nxt in succ.items()
                if k not in lab["end"] and any(x in j for x in nxt)
                and all(x in doomed for x in nxt if x in j)}
        assert norms[n.id]["keepable"]["answer"] == (init not in ef(trap))


@needs_prism
@pytest.mark.parametrize("body", [
    "norms:\n  - id: o\n    kind: obligation\n    bearer: D\n    content: \"D.a + 1 > 2\"\n",
    "norms:\n  - id: pw\n    kind: power\n    bearer: D\n    content: \"D -> R[1] : cancel\"\n"
    "    effects: {discharge: [o]}\n  - id: o\n    kind: obligation\n    bearer: D\n"
    "    content: \"D.a + 1 > 2\"\n",
], ids=["nothing", "power only"])
def test_nothing_or_only_powers_translated(body):
    m = build_model(make_sos(body), bindings={"i": 1})
    report = check(m, PRISM)
    assert report["checked"] and report["unconfirmed"] == []
    assert "arithmetic" in {n["id"]: n for n in report["norms"]}["o"]["reason"]
    if "pw" in body:
        assert [r["reading"] for r in report["runs"]] == ["any end"]
        assert {n["id"]: n for n in report["norms"]}["pw"]["exercisable"]["answer"]
    else:
        assert report["runs"] == []
        assert "Nothing to ask the model checker" in format_text(report)


def test_notes_on_bindings_and_value_types():
    _sos, m, _pm = model_of(str(ROBOT), bindings=["i=2", "k=1"])
    assert any("--bindings k" in n for n in m.tr.notes)
    assert not any("type of the literals" in n for n in m.tr.notes)


def test_temporal_operator_positions_have_exact_reasons():
    tr = build_model(make_sos("""\
norms:
  - id: o
    kind: obligation
    bearer: "R[1]"
    content: "NOT within(1s, R[1].b)"
  - id: q
    kind: prohibition
    bearer: "R[1]"
    content: "within(2s, throughout(1s, R[1].b))"
"""), bindings={"i": 1}).tr
    assert "under NOT" in tr.by_id["o"].reason
    assert "nested in within()" in tr.by_id["q"].reason


def test_comments_cannot_carry_model_text():
    sos = make_sos("""\
norms:
  - id: p
    kind: prohibition
    bearer: "R[1]"
    content: "R[1].m == 'x\nlabel \\"evil\\" = true; //'"
""")
    text = build_model(sos, bindings={"i": 1}).prism_text()
    assert all("evil" not in line.split("//")[0] for line in text.splitlines())
    PrismModel(text, {"early_end": True})


def test_cli_require_checker_and_timeout(tmp_path):
    env = dict(os.environ, PATH=str(Path(sys.executable).parent))
    p = cli(str(CONFLICT), "--bindings", "i=1", "--require-checker", env=env)
    assert p.returncode == 2 and "--require-checker" in p.stderr
    if PRISM is not None:
        p = cli(str(CONFLICT), "--bindings", "i=1", "--timeout", "0.01",
                "--export", str(tmp_path))
        assert p.returncode == 2 and "--timeout" in p.stderr      # building or checking

    for bad in ("0", "-5", "nan", "inf"):
        p = cli(str(CONFLICT), "--bindings", "i=1", "--timeout", bad)
        assert p.returncode == 2 and "--timeout" in p.stderr, bad
        assert "Traceback" not in p.stderr


def _random_json(rng, consts, domain=None):
    pool = [None, True, False, [], {}, [1], [0, 2], {"a": 1}, {"b": 2}, "", "x", "OTHER",
            [1, "a"], [1, 2], ["a"], [None], [[0]], [{"x": 1}], [0, [1, "b"]], [True, 0.5],
            rng.choice(["a", "b", "zz", "m", "ma", "l\uffff"]),
            rng.randint(-30, 30), rng.uniform(-30, 30), rng.choice([0, 0.0, 1, 1.0, -0.0])]
    if domain is not None:
        pool = [v for v in pool if type(v) not in (int, float)]
        return rng.choice(pool + [rng.randint(*domain)] * len(pool))
    for c in consts:
        pool.append(c)
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            pool += [c - 0.5, c + 0.5, c - 1, c + 1, float(c)]
        if isinstance(c, str):
            pool.append(c + "x")
    return rng.choice(pool)


def _vectors_read_back(m):
    """Every choice of the model, written as an observation of a trace and
    read back by norms-eval, gives its predicates the values the model has
    for that choice: the values of the classes are written exactly."""
    from cadl.norms_eval import _Evaluator
    tr = m.tr
    exprs = {i: p for i, p in enumerate(tr.preds)}
    for ch in tr.choices:
        line = trace_lines(tr, [(ch.index, True)])[0]
        obs = parse_trace([line]).observations[0]
        for i, p in exprs.items():
            t = p.owner
            from cadl.norms_eval import Trace as _Trace, UNKNOWN
            ev = _Evaluator(_Trace(observations=[obs], bindings=t.bindings),
                            t.contract.lifecycle is not None, [])
            ev.actors = t.actors
            v = ev.holds(p.expr, 0, 0)
            got = "T" if v is True else "F" if v is False else "U"
            assert got == ch.vector[i], (line, p.text, got, ch.vector[i])


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_witness_values_read_back_exactly(case):
    name, kw = case
    _sos, m, _pm = model_of(name, **kw)
    _vectors_read_back(m)


def _decimal(rng, places):
    v = rng.randint(-10 ** (places + 1), 10 ** (places + 1))
    return f"{v / 10 ** places:.{places}f}"


def test_decimal_literals_round_trip():
    """Pairs of decimal literals with 1 to 6 places, negative values and
    values close together: every class, written into a trace, is read by
    norms-eval as the value the model gives it, and a gap between two
    close literals has a value strictly inside it."""
    import random as _random
    rng = _random.Random(11)
    pairs = [("0.1", "0.2"), ("0.1", "0.10001"), ("-0.3", "-0.29999"), ("1e-3", "0.5"),
             ("-1.5", "-1.25"), ("0.000001", "0.000002"),
             ("0.1", "0.100000000000000000001")]          # beyond a float
    pairs += [tuple(sorted((_decimal(rng, p), _decimal(rng, p)), key=float))
              for p in range(1, 7) for _ in range(3)]
    for a, b in pairs:
        if a == b:
            continue
        sos = make_sos(f"""\
norms:
  - id: p
    kind: prohibition
    bearer: D
    content: "R[1].v > {a} AND R[1].v < {b}"
  - id: q
    kind: prohibition
    bearer: D
    content: "R[1].v == {a} OR R[2].v >= {b}"
""")
        m = build_model(sos, members={"R": 2})
        _vectors_read_back(m)
        pid = next(i for i, p in enumerate(m.tr.preds) if "AND" in p.text)
        assert any(ch.vector[pid] == "T" for ch in m.tr.choices), (a, b)


FRACTIONS = """\
norms:
  - id: no_flagged_driving
    kind: prohibition
    bearer: "R[1]"
    content: "R[1].low == true AND R[1].speed > 0.1 AND R[1].speed < 0.2"
  - id: report
    kind: obligation
    bearer: "R[1]"
    trigger: "D -> R[1] : ask"
    content: "R[1].speed >= 0.1 AND R[1].speed <= 0.10001"
    deadline: 1s
"""


@needs_prism
def test_decimal_witnesses_are_confirmed():
    """The reproducer of 0.3.26: the witness of a violation in the gap
    (0.1, 0.2) was written with speed 0.1 and not confirmed."""
    sos = make_sos(FRACTIONS)
    m = build_model(sos, bindings={"i": 1})
    report = check(m, PRISM)
    assert report["unconfirmed"] == []
    norms = {n["id"]: n for n in report["norms"]}
    for nid, key in (("no_flagged_driving", "violable"), ("report", "fulfillable")):
        w = norms[nid][key]["witness"]
        assert w["confirmed"]
        r = evaluate(sos, parse_trace(w["trace"]))
        want = "violated" if key == "violable" else "fulfilled"
        assert {n["id"]: n["state"] for n in r["norms"]}[nid] == want


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_value_classes_cover_every_json_value(case):
    """Exactness of the classes of values (F.12): for random JSON values of
    every type (not only the representatives), the values that norms-eval
    gives the predicates of a group are those of one of the choices."""
    import random as _random
    name, kw = case
    _sos, m, _pm = model_of(name, **kw)
    tr = m.tr
    rng = _random.Random(3)
    for ids, vecs in tr.components:
        keys = sorted(set().union(*(tr.preds[i].keys for i in ids)))
        exprs = [tr.preds[i].expr for i in ids]
        known = {v for v, _vals in vecs}
        # the literals of the whole group, so that a value of one quantity
        # can equal a literal compared with another one
        lits = set().union(*(tr.quantities[k].consts for k in keys))
        for _ in range(80):
            vals = {k: _random_json(rng, lits, tr.quantities[k].domain) for k in keys}
            assert tuple(tr._oracle(exprs, vals)) in known, vals


MIXED = """\
norms:
  - id: same_above
    kind: prohibition
    bearer: D
    content: "R[1].v == R[2].v AND R[2].v > 3"
  - id: order_above
    kind: prohibition
    bearer: D
    content: "R[1].w < R[2].w AND R[1].w > 5"
  - id: texts
    kind: prohibition
    bearer: D
    content: "R[1].s == R[2].s AND R[2].s == \\"zz\\""
  - id: floats
    kind: prohibition
    bearer: D
    content: "R[1].f < 0.5 AND R[1].f > -1e-3 AND R[1].f != 0.25"
"""


# lists are not always comparable: [1, "a"] < [1, 2] is undefined
LISTS = """\
norms:
  - id: o
    kind: obligation
    bearer: D
    trigger: "D -> R[1] : go"
    content: "R[1].v < R[2].v AND R[1].v < R[3].v"
    deadline: 1s
  - id: p
    kind: prohibition
    bearer: D
    content: "R[2].v < R[3].v OR R[2].v >= R[3].v"
"""


TYPES = """\
norms:
  - id: p
    kind: prohibition
    bearer: "R[1]"
    content: "R[1].v >= 1 AND R[1].v <= 1 AND R[1].v != 1"
  - id: o
    kind: obligation
    bearer: "R[1]"
    trigger: "D -> R[1] : go"
    content: "R[1].w != 3"
    deadline: 1s
  - id: q
    kind: prohibition
    bearer: "R[1]"
    content: "R[1].w >= 3 OR R[1].w < 3"
  - id: r
    kind: prohibition
    bearer: "R[1]"
    content: "R[1].u < true AND R[1].u > 0"
"""


@needs_prism
def test_values_of_another_type():
    """A boolean is ordered as 0 or 1 but is not equal to 1, so p can be
    violated (by true); a text is neither ordered nor equal to 3, so o and q
    are not in conflict: a text fulfils o and leaves q undefined. Both
    answers were wrong while the classes assumed the type of the literals."""
    sos = make_sos(TYPES)
    m = build_model(sos, bindings={"i": 1})
    report = check(m, PRISM)
    norms = {n["id"]: n for n in report["norms"]}
    w = norms["p"]["violable"]["witness"]
    assert w["confirmed"] and "R[1].v=true" in " ".join(w["choices"])
    assert report["conflicts"] == []
    assert norms["o"]["fulfillable"]["answer"] and norms["q"]["violable"]["answer"]


@needs_prism
def test_mixed_comparisons_are_violable():
    """A quantity compared with another one also takes the literals that the
    other is compared with: both values 4 violate same_above, and so on.
    norms-check 0.3.25+ reported these as never violated."""
    sos, m, _pm = model_of("MIXED", members=["R=2"])
    assert 0.25 in m.tr.quantities["R[1].f"].reps or any(
        type(v) is float and -1e-3 < v < 0.5 for v in m.tr.quantities["R[1].f"].reps)
    report = check(m, PRISM)
    for nid in ("same_above", "order_above", "texts", "floats"):
        entry = {n["id"]: n for n in report["norms"]}[nid]["violable"]
        assert entry["answer"] and entry["witness"]["confirmed"], nid


def test_list_orders_realise_every_pattern():
    """The list values of the model realise every pattern of pairwise
    outcomes (equal, less, greater, not comparable) that random lists with
    mixed and nested elements have, for two and three lists."""
    import random as _random
    from cadl.norms_check import _LIST_VALUES

    def outcome(a, b):
        if a == b:
            return "eq"
        try:
            return "lt" if a < b else "gt"
        except TypeError:
            return "inc"

    def sig(vs):
        return tuple(outcome(vs[i], vs[j]) for i in range(len(vs))
                     for j in range(i + 1, len(vs)))

    rng = _random.Random(7)

    def rand_list(depth=0):
        out = []
        for _ in range(rng.randint(0, 3)):
            c = rng.random()
            if c < 0.35:
                out.append(rng.choice([0, 1, 2, 0.5, True, False]))
            elif c < 0.6:
                out.append(rng.choice(["a", "b", "c", ""]))
            elif c < 0.7:
                out.append(None)
            elif c < 0.8:
                out.append(rng.choice([{}, {"x": 1}]))
            elif depth < 2:
                out.append(rand_list(depth + 1))
        return out

    for n, values in _LIST_VALUES.items():
        import itertools
        have = {sig(c) for c in itertools.product(values, repeat=n)}
        assert len(have) == {2: 4, 3: 29}[n]
        for _ in range(5000):
            assert sig([rand_list() for _ in range(n)]) in have


@needs_prism
def test_lists_that_are_not_comparable():
    """A trace may give R[2].v = [1, "a"] and R[3].v = [1, 2], which
    norms-eval cannot order: p stays undefined, so o and p are not in
    conflict (norms-check 0.3.26 before this fix reported a conflict)."""
    sos, m, _pm = model_of("LISTS")
    report = check(m, PRISM)
    assert report["conflicts"] == []
    assert report["unconfirmed"] == []


def test_internal_errors_exit_4(monkeypatch, capsys):
    import argparse
    import cadl.norms_check as nc
    from cadl import cli as cli_module

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(nc, "check_file", boom)
    args = argparse.Namespace(file=ROBOT, contract=None, bindings=[], members=[], step=None,
                              horizon=None, export=None, format="text", timeout=600,
                              require_checker=False, domain=[], instance=[], instances=None,
                              versions=[])
    assert cli_module._cmd_norms_check(args) == 4
    assert "Internal error of norms-check" in capsys.readouterr().err


def test_command_limit_and_java_memory():
    from cadl.norms_check import MAX_COMMANDS, _java_memory
    _sos, m, _pm = model_of(str(ROBOT), bindings=["i=2"])
    assert m.command_count() == len(m.commands) <= MAX_COMMANDS
    assert re.fullmatch(r"[1-4]g", _java_memory())


def test_witness_amend_lines_are_relative(tmp_path):
    v1, v2 = tmp_path / "v1.cadl", tmp_path / "v2.cadl"
    v1.write_text(HEAD + textwrap.indent(AM_SMALL_V1, "      "))
    v2.write_text(HEAD + textwrap.indent(AM_SMALL_V2, "      "))
    m = build_model(load_version(v1), bindings={"i": 1}, versions=[str(v2)])
    assert m.tr.version_names == ["v2.cadl"]
    from cadl.norms_check import random_paths as rp, trace_lines as tl
    p = next(p for p in rp(m.tr, 50, seed=1, p_amend=0.5) if any(j >= len(m.tr.choices)
                                                                for j, _e in p))
    assert '"amend": "v2.cadl"' in "\n".join(tl(m.tr, p))


def test_cli_refusal():
    p = cli(str(ROBOT), "--step", "3s")
    assert p.returncode == 2
    assert "does not divide" in p.stderr

