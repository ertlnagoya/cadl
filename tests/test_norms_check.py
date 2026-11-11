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
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cadl.norms_check import (
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


def make_sos(body: str):
    sos = parse(HEAD + textwrap.indent(body, "      "))
    result = type_check(sos)
    assert result.ok, result.errors
    return sos


_MODELS: dict = {}


def model_of(name: str, **kw):
    """The model of an example file or of one of the contracts above,
    built once per test session."""
    key = (name, json.dumps(kw, sort_keys=True))
    if key not in _MODELS:
        texts = {"TEMPORAL": TEMPORAL, "UNTIL": UNTIL, "CONFLICTS": CONFLICTS, "EVENTS": EVENTS,
                 "DEADLINE_ZERO": DEADLINE_ZERO}
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
        assert reps == {"ROBOT[2].battery": [None, 9, 10, 11],
                        "ROBOT[2].status": [None, "InTransit", "OTHER"],
                        "DISPATCHER.is_operational": [None, True, False]}
        # 2**4 message sets, 3 classes of the prohibition's content (true,
        # false, undefined) and 3 of the assumption
        assert len(tr.choices) == 16 * 3 * 3
        assert tr.step_ms == 1000
        assert m.H == 2 + (5 + 2) + (2 + 2) + 2
        assert all(n.status == "model" for n in tr.norms)
        assert tr.members["ROBOT"] == [1, 2]
        assert [n for n in tr.notes if "type of the literals" not in n] == []

    def test_default_binding_is_reported(self):
        _sos, m, _pm = model_of(str(ROBOT))
        assert m.tr.bindings["i"] == 1
        assert any("index i is not bound" in n for n in m.tr.notes)

    def test_quantified_norm_is_not_translated(self):
        _sos, m, _pm = model_of(str(DOUBLE), bindings=["i=1"])
        n = m.tr.by_id["no_double_assignment"]
        assert n.status == "untranslated"
        assert "a quantifier in a predicate is not translated" in n.reason
        assert m.tr.by_id["acknowledge"].status == "model"

    def test_dependants_of_untranslated_norms(self):
        sos = make_sos("""\
norms:
  - id: a
    kind: obligation
    bearer: "R[i]"
    content: "R[i].x == R[i].y"
    on_violation: {reparation: b}
  - id: b
    kind: obligation
    bearer: "R[i]"
    content: "R[i] -> D : m"
""")
        tr = build_model(sos, bindings={"i": 1}).tr
        assert tr.by_id["a"].status == "untranslated"
        assert "two quantities" in tr.by_id["a"].reason
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

    def test_three_valued_predicates(self):
        p = ("and", ("cmp", "<", "R[1].b", 10), ("cmp", "==", "R[1].s", "InTransit"))
        ev = Translator.evaluate
        assert ev(p, {"R[1].b": 9, "R[1].s": "InTransit"}) == "T"
        assert ev(p, {"R[1].b": 11, "R[1].s": None}) == "F"
        assert ev(p, {"R[1].b": 9, "R[1].s": None}) == "U"
        assert ev(("not", p), {"R[1].b": 9, "R[1].s": None}) == "U"
        assert ev(("cmp", "==", "q", True), {"q": 1}) == "F"      # a boolean is not 1

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

    def choice(msgs, level):
        return next(c.index for c in tr.choices
                    if c.messages == msgs and c.values.get("R[1].level") == level)

    none, low = choice((), None), choice((), 2)
    path = ([(choice((go,), None), False)] + [(none, False)] * 5
            + [(low, False), (low, False), (none, True)])
    assert not compare_path(m, pm, path)
    states = run_path(pm, path)
    assert states[7]["st_1_o_rep"] == ACTIVE


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
    assert any("type of the literals" in n for n in m.tr.notes)


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
        assert p.returncode == 2 and "--timeout" in p.stderr
        assert (tmp_path / "STATUS_REPORTING.prism").exists()

    for bad in ("0", "-5", "nan", "inf"):
        p = cli(str(CONFLICT), "--bindings", "i=1", "--timeout", bad)
        assert p.returncode == 2 and "--timeout" in p.stderr, bad
        assert "Traceback" not in p.stderr


def test_cli_refusal():
    p = cli(str(ROBOT), "--step", "3s")
    assert p.returncode == 2
    assert "does not divide" in p.stderr
