"""Quantified predicates and aggregates in the dynamic semantics of the
norms extension (Appendix F, F.5, "Quantifiers and aggregates"):
`exists j in A: p`, `for all j in A: p` and `sum(e for j in A)` over a
declared set of actors, with Kleene's three-valued logic, a domain that
is closed (integer bounds, or a symbolic bound with a value) or open (a
symbolic bound without a value: only the members named so far are
known)."""

from __future__ import annotations

import functools
import itertools
import json
import random
import textwrap
from pathlib import Path

import pytest

from cadl.norms_eval import (
    UNKNOWN, _Evaluator, declared_actors, desugar_wildcards, evaluate, parse_trace,
)
from cadl.parser import parse, parse_rule
from cadl.type_checker import type_check

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "norms_no_double_assignment.cadl"
TRACES = ROOT / "examples" / "traces"

ACTORS = """
sos:
  name: Q
  extensions:
    - norms: 0.1
  actors:
    - {id: D, role: d}
    - {id: "R[1..3]", role: r}
    - {id: "S[1..N]", role: s}
  contracts:
    - id: C
      parties: [D, "R[*]", "S[*]"]
"""


def make_sos(blocks: str = "norms:\n  - {id: p, kind: permission, bearer: D, content: \"D -> D : x\"}\n"):
    sos = parse(ACTORS + textwrap.indent(textwrap.dedent(blocks), "      "))
    result = type_check(sos)
    assert result.ok, result.errors
    return sos


ACTOR_TABLE = declared_actors(make_sos())


def evaluator(observations: list, bindings: dict | None = None) -> _Evaluator:
    obs = [dict(o) for o in observations]
    if bindings is not None:
        obs[0]["bindings"] = bindings
    ev = _Evaluator(parse_trace(json.dumps(o) for o in obs), False, [])
    ev.actors = ACTOR_TABLE
    return ev


@functools.lru_cache(maxsize=None)
def _rule(text: str):
    return parse_rule(text)          # the evaluator does not change the tree


def value(text: str, observations: list, k: int = -1, bindings: dict | None = None):
    """The truth value of a predicate at observation k (default: the last),
    as True, False or None for undefined."""
    ev = evaluator(observations, bindings)
    k = k % len(ev.obs)
    v = ev.holds(_rule(text), k, k)
    return None if v is UNKNOWN else v


def number(text: str, observations: list, bindings: dict | None = None):
    from cadl.parser import parse_expr
    ev = evaluator(observations, bindings)
    k = len(ev.obs) - 1
    v = ev.value(parse_expr(text), k, k)
    return None if v is UNKNOWN else v


def kleene_exists(vals: list, closed: bool = True):
    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals) or not closed:
        return None
    return False


def kleene_forall(vals: list, closed: bool = True):
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals) or not closed:
        return None
    return True


def r_values(xs: tuple) -> dict:
    """Values of R[1].x .. R[3].x: 1 (true), 0 (false) or absent (None)."""
    return {f"R[{i}].x": x for i, x in enumerate(xs, start=1) if x is not None}


# === Integer bounds: a closed domain ============================================

class TestIntegerDomain:
    @pytest.mark.parametrize("xs", list(itertools.product((0, 1, None), repeat=3)))
    def test_kleene_tables(self, xs):
        obs = [{"t": 0, "values": r_values(xs)}]
        truth = [None if x is None else x == 1 for x in xs]
        assert value("exists j in R: R[j].x == 1", obs) is kleene_exists(truth)
        assert value("for all j in R: R[j].x == 1", obs) is kleene_forall(truth)
        assert value("for all r in R[*]: r.x == 1", obs) is kleene_forall(truth)
        # A[lo..hi]: the members of the range only
        assert value("exists j in R[2..3]: R[j].x == 1", obs) is kleene_exists(truth[1:])
        assert value("for all j in R[1..2]: R[j].x == 1", obs) is kleene_forall(truth[:2])

    def test_not_and_or_around_a_quantifier(self):
        obs = [{"t": 0, "values": {"R[1].x": 1, "R[2].x": 0, "R[3].x": 0}}]
        assert value("NOT (exists j in R: R[j].x == 1)", obs) is False
        assert value("R[1].x == 0 OR exists j in R: R[j].x == 1", obs) is True
        assert value("R[1].x == 1 AND for all j in R: R[j].x == 1", obs) is False

    def test_variable_is_the_index_of_the_member(self):
        obs = [{"t": 0, "values": {"R[1].x": 7, "R[2].x": 7, "R[3].x": 1}}]
        two = "exists j in R: exists k in R: j != k AND R[j].x == 7 AND R[k].x == 7"
        assert value(two, obs) is True
        assert value(two.replace("== 7", "== 1"), obs) is False
        assert value("exists j in R: j == 3 AND R[j].x == 1", obs) is True
        assert value("for all j in R: j >= 1 AND j <= 3", obs) is True
        # an index expression with the variable
        assert value("exists j in R[1..2]: R[j].x == R[j+1].x", obs) is True

    def test_members_outside_the_declared_set_are_not_in_the_domain(self):
        obs = [{"t": 0, "values": {"R[1].x": 0, "R[2].x": 0, "R[3].x": 0, "R[7].x": 1,
                                   "R[0].x": 1}}]
        assert value("exists j in R: R[j].x == 1", obs) is False
        assert value("exists j in R[2..9]: R[j].x == 1", obs) is False

    def test_empty_domain(self):
        obs = [{"t": 0}]
        assert value("exists j in R[5..9]: R[j].x == 1", obs) is False
        assert value("for all j in R[5..9]: R[j].x == 1", obs) is True
        assert number("count(R[j].x == 1 for j in R[5..9])", obs) == 0
        assert number("sum(R[j].x for j in R[5..9])", obs) == 0
        assert number("min(R[j].x for j in R[5..9])", obs) is None

    def test_a_set_that_is_not_declared_or_not_a_set_is_undefined(self):
        obs = [{"t": 0, "values": {"R[1].x": 1}}]
        assert value("exists j in Z: Z[j].x == 1", obs) is None
        assert value("exists j in D: D.x == 1", obs) is None


# === Nested quantifiers, shadowing, bindings ======================================

class TestScopes:
    OBS = [{"t": 0, "values": {"R[1].x": 1, "R[2].x": 0, "R[3].x": 0,
                               "R[1].y": 0, "R[2].y": 0, "R[3].y": 1}}]

    def test_nested(self):
        assert value("for all j in R: exists k in R: R[k].y == R[j].x", self.OBS) is True
        assert value("exists j in R: for all k in R: R[k].y <= R[j].x", self.OBS) is True
        assert value("for all j in R: for all k in R: R[j].x + R[k].y <= 1", self.OBS) is False

    def test_inner_variable_hides_the_outer_and_is_restored(self):
        # inner j over R[3..3] hides the outer j; after it, the outer j is back
        assert value("exists j in R: (exists j in R[3..3]: R[j].y == 1) AND R[j].x == 1",
                     self.OBS) is True
        assert value("exists j in R[2..3]: (exists j in R[1..1]: R[j].x == 1) AND R[j].x == 1",
                     self.OBS) is False

    def test_variable_hides_a_binding_and_a_quantity(self):
        obs = [{"t": 0, "values": {"R[1].x": 1, "R[2].x": 0, "R[3].x": 0, "j": 2}}]
        assert value("R[j].x == 1", obs, bindings={"j": 2}) is False
        assert value("exists j in R: R[j].x == 1", obs, bindings={"j": 2}) is True
        assert value("for all j in R: j != 5", obs) is True

    def test_temporal_operator_inside_a_quantifier(self):
        obs = [{"t": 0, "values": {"R[1].x": 0, "R[2].x": 0, "R[3].x": 0}},
               {"t": 1, "values": {"R[2].x": 1}},
               {"t": 9}]
        ev = evaluator(obs)
        within = parse_rule("exists j in R: within(5s, R[j].x == 1)")
        assert ev.holds(within, 0, 0) is not True        # undecided on the first observation
        assert ev.holds(within, 0, 1) is True
        never = parse_rule("exists j in R[3..3]: within(5s, R[j].x == 1)")
        assert ev.holds(never, 0, 2) is False


# === Symbolic bounds and the open domain =========================================

class TestSymbolicBound:
    def test_bound_with_a_value_closes_the_domain(self):
        obs = [{"t": 0, "values": {"S[1].x": 0, "S[2].x": 0}}]
        assert value("exists j in S: S[j].x == 1", obs, bindings={"N": 2}) is False
        assert value("for all j in S: S[j].x == 0", obs, bindings={"N": 2}) is True
        # N as an observed quantity, at that observation
        obs2 = obs + [{"t": 1, "values": {"N": 2}}]
        assert value("exists j in S: S[j].x == 1", obs2, k=0) is None
        assert value("exists j in S: S[j].x == 1", obs2, k=1) is False
        # a member named beyond N keeps the domain open (F.3 makes it a
        # member); N = 0 is an empty domain
        obs3 = [{"t": 0, "values": {"S[1].x": 0, "S[2].x": 0, "S[3].x": 1, "N": 2}}]
        assert value("exists j in S: S[j].x == 1", obs3) is True
        assert value("for all j in S: S[j].x == 0", obs3) is False
        assert value("exists j in S: S[j].x == 7", obs3) is None
        assert value("for all j in S: S[j].x == 1", [{"t": 0, "values": {"N": 0}}]) is True

    def test_bound_that_is_not_an_integer_leaves_the_domain_open(self):
        obs = [{"t": 0, "values": {"S[1].x": 0, "N": "many"}}]
        assert value("exists j in S: S[j].x == 1", obs) is None

    def test_open_domain_decides_only_by_a_known_member(self):
        obs = [{"t": 0, "values": {"S[1].x": 0, "S[2].x": 1}}]
        assert value("exists j in S: S[j].x == 1", obs) is True
        assert value("for all j in S: S[j].x == 1", obs) is False
        assert value("exists j in S: S[j].x == 7", obs) is None       # S[9] might
        assert value("for all j in S: S[j].x <= 1", obs) is None      # S[9] might not
        assert number("sum(S[j].x for j in S)", obs) is None
        assert number("count(S[j].x == 1 for j in S)", obs) is None
        assert value("exists j in S[1..2]: S[j].x == 7", obs) is None  # N may be 1

    def test_open_domain_grows_with_the_observations(self):
        obs = [{"t": 0, "values": {"S[1].x": 0}},
               {"t": 1, "messages": [{"from": "S[4]", "to": "D", "name": "hi"}]},
               {"t": 2, "values": {"S[4].x": 1}},
               {"t": 3, "values": {"S[6].x": 1}}]
        text = "exists j in S: exists k in S: j != k AND S[j].x == 1 AND S[k].x == 1"
        assert [value(text, obs, k) for k in range(4)] == [None, None, None, True]
        ev = evaluator(obs)
        assert [sorted(ev.known_members(k).get("S", ())) for k in range(4)] == [
            [1], [1, 4], [1, 4], [1, 4, 6]]
        # the value at an observation does not depend on later observations
        assert value("exists j in S: S[j].x == 1", obs[:3], k=0) is None
        assert value("exists j in S: S[j].x == 1", obs, k=0) is None

    def test_binding_names_a_member(self):
        obs = [{"t": 0, "values": {"S[1].x": 0}}]
        ev = evaluator(obs, bindings={"i": 5})
        ev.bound_refs = {("S", "i")}
        assert sorted(ev.known_members(0)["S"]) == [1, 5]


# === Review: large domains, bounds, members beyond the bound ====================

class TestReviewFixes:
    def test_huge_bound_is_evaluated_as_an_open_domain(self):
        import time
        obs = [{"t": 0, "values": {"N": 10 ** 7, "S[2].x": 1, "S[3].x": 0}}]
        ev = evaluator(obs)
        start = time.perf_counter()
        assert ev.holds(_rule("exists j in S: S[j].x == 1"), 0, 0) is True
        assert ev.holds(_rule("for all j in S: S[j].x == 1"), 0, 0) is False
        assert ev.holds(_rule("exists j in S: S[j].x == 7"), 0, 0) is UNKNOWN
        assert ev.holds(_rule("sum(S[j].x for j in S) > 0"), 0, 0) is UNKNOWN
        assert time.perf_counter() - start < 2
        assert any("10000000 members, more than 10000" in w for w in ev.warnings)
        # MAX_DOMAIN members are still a closed domain
        values = {f"S[{i}].x": 0 for i in range(1, 10 ** 4 + 1)}
        assert value("exists j in S: S[j].x == 1", [{"t": 0, "values": dict(values, N=10 ** 4)}]) is False
        assert value("exists j in S: S[j].x == 1",
                     [{"t": 0, "values": dict(values, N=10 ** 4 + 1)}]) is None

    def test_variable_does_not_hide_a_declared_bound(self):
        obs = [{"t": 0, "values": {"S[1].x": 0, "S[2].x": 0, "S[3].x": 1}}]
        assert value("for all N in S: exists j in S: S[j].x == 1", obs, bindings={"N": 3}) is True
        assert value("for all N in S: N <= 3", obs, bindings={"N": 3}) is True
        sos = parse(ACTORS.replace("parties: [D, \"R[*]\", \"S[*]\"]",
                                   "parties: [D, \"R[*]\", \"S[*]\"]\n      guarantee:\n"
                                   "        - \"for all N in S: S[N].x == 1\""))
        warnings = [str(w) for w in type_check(sos).warnings if "N-14" in str(w)]
        assert len(warnings) == 1 and "'N'" in warnings[0]

    def test_member_named_beyond_the_bound_keeps_the_domain_open(self):
        obs = [{"t": 0, "values": {"N": 3, "S[1].x": 0, "S[2].x": 0, "S[3].x": 0},
                "messages": [{"from": "S[5]", "to": "D", "name": "hi"}]}]
        assert value("exists j in S: j == 5", obs) is True
        assert value("exists j in S: S[j].x == 1", obs) is None
        ev = evaluator(obs)
        ev.holds(_rule("exists j in S: j == 5"), 0, 0)
        assert any("S[5] is named but the bound 'N' of S is 3" in w for w in ev.warnings)
        assert value("exists j in S: S[j].x == 1",
                     [{"t": 0, "values": {"N": 3, "S[1].x": 0, "S[2].x": 0, "S[3].x": 0}}]) is False

    def test_index_outside_the_set_is_undefined(self):
        obs = [{"t": 0, "values": {"R[1].x": 1, "R[2].x": 1, "R[3].x": 1, "R[4].x": 1}}]
        assert value("for all j in R: R[j+1].x > 0", obs) is None
        assert value("for all j in R[1..2]: R[j+1].x > 0", obs) is True

    def test_count_counts_true_and_nonzero_elements(self):
        obs = [{"t": 0, "values": {"R[1].x": 5, "R[2].x": 0, "R[3].x": 7}}]
        assert number("count(R[j].x for j in R)", obs) == 2


# === Aggregates =================================================================

class TestAggregates:
    OBS = [{"t": 0, "values": {"R[1].x": 2, "R[2].x": 3, "R[3].x": 7}}]

    @pytest.mark.parametrize("text, expected", [
        ("sum(R[j].x for j in R)", 12),
        ("sum(r.x for r in R[*])", 12),
        ("sum(R[j].x for j in 1..2)", 5),
        ("sum(R[j].x for j in R[2..3])", 10),
        ("count(R[j].x > 2 for j in R)", 2),
        ("min(R[j].x for j in R)", 2),
        ("max(R[j].x for j in R)", 7),
        ("avg(R[j].x for j in R)", 4),
    ])
    def test_values(self, text, expected):
        assert number(text, self.OBS) == expected

    def test_in_a_predicate(self):
        assert value("sum(R[j].x for j in R) > 10", self.OBS) is True
        assert value("count(R[j].x == 3 for j in R) >= 2", self.OBS) is False

    def test_undefined_element_or_bound(self):
        obs = [{"t": 0, "values": {"R[1].x": 2, "R[2].x": 3}}]
        assert number("sum(R[j].x for j in R)", obs) is None
        assert number("count(R[j].x > 2 for j in R)", obs) is None
        assert number("sum(R[j].x for j in 1..M)", obs) is None
        assert number("sum(R[j].x for j in 1..M)", obs, bindings={"M": 2}) == 5
        assert number("sum(R[j].x for j in R)", [{"t": 0, "values": {
            "R[1].x": 2, "R[2].x": "a", "R[3].x": 1}}]) is None


# === What stays not evaluable ===================================================

class TestNotEvaluable:
    @pytest.mark.parametrize("text, reason", [
        ("sum(R[*].x * S[*].x) > 1", "the argument of sum() refers to R[*] and S[*]"),
        ("exists j in 3: R[j].x == 1", "the domain of a quantifier"),
        ("sum(i.x for i in 1..3) > 1", "ranges over integers"),
        ("exists j in R: within(5s, R[j].x == 1)", "the temporal operator within()"),
        ("foo(R[*].x) > 1", "the function call foo() is not evaluable"),
        ("foo(R[j].x for j in R) > 1", "the function call foo() is not evaluable"),
    ])
    def test_reasons(self, text, reason):
        ev = evaluator([{"t": 0}])
        assert reason in ev.unsupported(desugar_wildcards(parse_rule(text)), False)

    @pytest.mark.parametrize("text", [
        "R[*].x > 1", "count(R[*].x == 1) >= 2",
        "sum(R[j].x for j in R[*]) > 1 AND R[1..2].x == 1",
    ])
    def test_wildcards_are_evaluable(self, text):
        # up to cadl 0.3.23 these were not evaluable (F.5, wildcards)
        ev = evaluator([{"t": 0}])
        assert ev.unsupported(desugar_wildcards(parse_rule(text)), False) is None

    def test_norm_is_reported_not_evaluable(self):
        # an aggregate shorthand over two sets is what stays of the
        # wildcards (F.5): `R[*].x > 1` alone is evaluated, see
        # test_norms_wildcards.py
        content = "sum(R[*].x * S[*].x) > 1"
        sos = make_sos('norms:\n  - {id: p, kind: prohibition, bearer: D, content: "%s"}\n'
                       % content)
        r = evaluate(sos, parse_trace([json.dumps({"t": 0}), json.dumps({"t": 1})]))
        n = r["norms"][0]
        assert n["verdict"] == "pending"
        assert n["reason"] == (f"not evaluable: content '{content}': the argument of sum() "
                               f"refers to R[*] and S[*]; an aggregate ranges over one set "
                               f"or range")


# === Soundness of the open domain ==============================================

FORMULAS = [
    "exists j in S: S[j].x == 1",
    "for all j in S: S[j].x == 1",
    "exists j in S: exists k in S: j != k AND S[j].x == 1 AND S[k].x == 1",
    "for all j in S: S[j].x == 1 OR S[j].y == 1",
    "NOT (exists j in S: S[j].x == 0)",
    "(for all j in S: S[j].x >= 0) AND exists j in S: S[j].y == 1",
    "exists j in S[2..N]: S[j].x == S[j].y",
    "for all j in S: exists k in S: S[k].y == S[j].x",
    "count(S[j].x == 1 for j in S) >= 1",
    "sum(S[j].x for j in S) < 3",
]


def test_open_domain_is_sound():
    """Whenever a quantified predicate over the open domain of S[1..N]
    (N without a value) is true or false, it has the same value for every
    extension of the domain: N gets a value (at least the largest known
    index), and every member up to N that the trace has not named gets an
    arbitrary value of x and y (0, 1 or none)."""
    rng = random.Random(20261111)
    checks = decided = 0
    for _ in range(260):
        known = sorted(rng.sample(range(1, 5), rng.randint(0, 3)))
        values = {}
        for i in known:
            for q in ("x", "y"):
                v = rng.choice((0, 1, None))
                if v is not None:
                    values[f"S[{i}].{q}"] = v
                elif rng.random() < 0.5:
                    values[f"S[{i}].{q}"] = None          # named, no value
            values.setdefault(f"S[{i}].z", 0)             # the member is named
        text = rng.choice(FORMULAS)
        base = value(text, [{"t": 0, "values": values}])
        if base is None:
            continue
        decided += 1
        top = max(known, default=0)
        for n in range(max(top, 1), 6):
            fresh = [i for i in range(1, n + 1) if i not in known]
            if len(fresh) > 3:
                break
            for choice in itertools.product((0, 1, None), repeat=2 * len(fresh)):
                ext = dict(values, N=n)
                for (i, (x, y)) in zip(fresh, zip(choice[::2], choice[1::2])):
                    if x is not None:
                        ext[f"S[{i}].x"] = x
                    if y is not None:
                        ext[f"S[{i}].y"] = y
                got = value(text, [{"t": 0, "values": ext}])
                assert got is base, (text, values, ext, base, got)
                checks += 1
    assert decided >= 40 and checks >= 10000, (decided, checks)


# === The bundled example and the traces of the exercise ===========================

def _example(trace_name: str) -> dict:
    from cadl.norms_eval import evaluate_files
    return evaluate_files(EXAMPLE, TRACES / trace_name)


class TestExample:
    def test_no_double_assignment(self):
        r = _example("norms_no_double_assignment_a_ok.jsonl")
        n = next(x for x in r["norms"] if x["id"] == "no_double_assignment")
        assert (n["state"], n["verdict"]) == ("fulfilled", "fulfilled")
        r = _example("norms_no_double_assignment_b_double.jsonl")
        n = next(x for x in r["norms"] if x["id"] == "no_double_assignment")
        assert (n["state"], n["verdict"], n["time"]) == ("violated", "violated", 3.0)
        assert [v["norm"] for v in r["violations"]] == ["no_double_assignment"]
        r = _example("norms_no_double_assignment_c_reassign.jsonl")
        n = next(x for x in r["norms"] if x["id"] == "no_double_assignment")
        assert (n["state"], n["verdict"]) == ("fulfilled", "fulfilled")
        assert r["ok"]


    def test_generated_monitor_gives_the_same_result(self, tmp_path):
        from tests.test_norms_monitor import _cli_norms_eval, _codegen, _run_module
        _codegen(EXAMPLE, tmp_path / EXAMPLE.stem)
        traces = sorted(TRACES.glob("norms_no_double_assignment_*.jsonl"))
        assert len(traces) == 3
        for trace in traces:
            expected = _cli_norms_eval(EXAMPLE, trace, "text")
            got = _run_module(tmp_path, f"{EXAMPLE.stem}.norms_monitor", trace,
                              "--format", "text")
            assert (got.returncode, got.stdout) == (expected.returncode, expected.stdout)


# === Causality: the prefix property ===============================================

QUANT = ROOT / "tests" / "data" / "norms_monitor_quant.cadl"
QUANT_V2 = ROOT / "tests" / "data" / "norms_monitor_quant_v2.cadl"

@pytest.mark.parametrize("amend", [False, True], ids=["plain", "amended"])
def test_prefix_property(amend, monkeypatch):
    """Random traces over a contract with quantified triggers, contents and
    untils over a set with a symbolic bound, also amended to a version
    whose references `S[i]` are gone (the members a binding names and the
    member cache are rebuilt): the verdicts after every prefix are those
    of the full trace (a decided verdict never changes, and the history of
    a prefix is a prefix of the full history)."""
    from cadl.parser import parse_file
    from tests.test_norms_monitor import _QUANT, _memoize_parser, _prefix, _random_trace
    _memoize_parser(monkeypatch)
    sos = parse_file(QUANT)
    assert type_check(sos).ok
    versions = {"v2": parse_file(QUANT_V2)} if amend else None
    rng = random.Random(20261114 + amend)
    seen = set()
    compared = amended = 0
    for _ in range(100 if amend else 150):
        lines = _random_trace(rng, _QUANT, "v2" if amend else None)
        full = evaluate(sos, parse_trace(lines), versions=versions)
        amended += bool(full["amendments"])
        ended = full["instance"]["observations"]
        for k in range(min(len(lines), ended) - 1):
            part = evaluate(sos, parse_trace(_prefix(lines, k)), versions=versions)
            for p in part["norms"]:
                f = next(x for x in full["norms"] if x["id"] == p["id"])
                assert f["history"][:len(p["history"])] == p["history"], (k, lines)
                if p["verdict"] != "pending":
                    assert (p["verdict"], p["time"]) == (f["verdict"], f["time"]), (k, lines)
                compared += 1
        for f in full["norms"]:
            seen.add((f["id"], f["state"]))
    assert compared >= 1000
    if amend:
        assert amended >= 25
        return
    # the quantified norms reach every state they can have
    for nid, state in [("double", "violated"), ("double", "fulfilled"),
                       ("busy_ack", "violated"), ("busy_ack", "fulfilled"),
                       ("may_assign", "discharged"), ("all_light", "fulfilled"),
                       ("all_light", "violated")]:
        assert (nid, state) in seen, (nid, state, sorted(seen))


# === The agent governance runtime (Appendix G) ====================================

AGENTS = """
sos:
  name: QA
  extensions:
    - norms: 0.1
    - agents: 0.1
  actors:
    - { id: OP, role: o, agent: { kind: human } }
    - { id: "R[1..N]", role: r, agent: { kind: llm_agent } }
    - { id: T, role: t, agent: { kind: tool_server, tools: [read, write] } }
  contracts:
    - id: C
      parties: [OP, "R[*]", T]
      enforcement: {}
      norms:
        - { id: may_write, kind: permission, bearer: OP, content: "OP -> T : write" }
        - { id: no_write, kind: prohibition, bearer: OP, trigger: "exists j in R: R[j].alarm == true", content: "OP -> T : write" }
        - { id: may_read, kind: permission, bearer: OP, content: "OP -> T : read", until: "for all j in R: R[j].done == true" }
"""


class TestAgentsRuntime:
    """G.5: an undefined quantified trigger or until is missing data and is
    resolved fail-closed; a defined one is judged as any predicate."""

    def test_quantified_trigger_and_until_at_the_boundary(self):
        from tests.test_agents_runtime import decisions, m, replay, trace
        rw = [m("OP", "T", "write"), m("OP", "T", "read")]
        r = replay(AGENTS, trace(
            # N has no value: no known robot raises an alarm, an unnamed one might
            (0, rw, {"values": {"R[1].alarm": False, "R[1].done": False}}),
            # N = 2: the domain is closed, nobody raises an alarm
            (1, rw, {"values": {"N": 2, "R[2].alarm": False, "R[2].done": True}}),
            (2, rw, {"values": {"R[1].done": True}}),
            (3, [m("OP", "T", "write")], {"values": {"R[2].alarm": True}, "end": False})))
        assert [d[1:4] for d in decisions(r)] == [
            ("OP>T:write", "deny", "prohibition"),      # undefined trigger: missing data
            ("OP>T:read", "allow", "permission"),       # R[1] falsifies the until
            ("OP>T:write", "allow", "permission"),      # trigger false
            ("OP>T:read", "allow", "permission"),
            ("OP>T:write", "allow", "permission"),
            ("OP>T:read", "deny", "default"),           # until true: discharged
            ("OP>T:write", "deny", "prohibition"),      # trigger true: in force
        ]
        flags = [s["input"]["instances"][0]["in_force"] for s in r["steps"]]
        assert flags[0] == {"may_read": True}           # no_write unknown (missing data)
        assert flags[2] == {"no_write": False, "may_read": True}
        assert flags[6] == {"no_write": True, "may_read": False}
        inst = r["instances"][0]
        states = {n["id"]: n["state"] for n in inst["norms"]}
        assert states["no_write"] == "active" and states["may_read"] == "discharged"
        assert any("trigger: 'exists j in R" in w and "a member of R not yet named" in w
                   for w in r["warnings"])

    def test_missing_data_names_the_remedy_and_the_keys(self):
        from tests.test_agents_runtime import m, replay, trace
        r = replay(AGENTS, trace((0, m("OP", "T", "write"), {"values": {"R[1].alarm": False}})))
        assert any("Give the bound of the set a value" in w for w in r["warnings"])
        r = replay(AGENTS, trace((0, m("OP", "T", "write"),
                                  {"values": {"N": 2, "R[1].alarm": False}})))
        assert any("(R[2].alarm has no value yet)" in w and "initial values" in w
                   for w in r["warnings"]), r["warnings"]

    def test_undefined_until_does_not_let_a_permission_apply(self):
        from tests.test_agents_runtime import decisions, m, replay, trace
        r = replay(AGENTS, trace(
            (0, m("OP", "T", "read"), {"values": {"R[1].done": True}}),
            (1, m("OP", "T", "read"), {"values": {"R[2].done": False}})))
        assert [d[2:4] for d in decisions(r)] == [("deny", "default"), ("allow", "permission")]

    def test_prefix_property(self):
        from tests.test_agents_runtime import TestDecisionsAreCausal, m, trace
        sos = parse(AGENTS)
        names = ["R[1].alarm", "R[2].alarm", "R[4].done", "R[1].done", "N"]
        total = 0
        for seed in range(4):
            rng = random.Random(seed)
            rows = []
            for t in range(30):
                more = {}
                if rng.random() < 0.5:
                    name = rng.choice(names)
                    more["values"] = {name: rng.choice([1, 2, 4]) if name == "N"
                                      else rng.random() < 0.4}
                msg_ = m("OP", "T", rng.choice(["read", "write"])) if rng.random() < 0.8 else None
                rows.append((t, msg_, more))
            total += TestDecisionsAreCausal().prefix_holds(sos, trace(*rows), 3)
        assert total >= 400

    def test_agreement_with_opa(self, tmp_path):
        import shutil
        import subprocess
        from cadl.agents_runtime import parse_run_trace, run
        from cadl.codegen import generate
        from tests.test_agents_runtime import m, trace
        opa = shutil.which("opa")
        if opa is None:
            pytest.skip("the opa binary is not on PATH")
        sos = parse(AGENTS)
        rw = [m("OP", "T", "write"), m("OP", "T", "read")]
        r = run(sos, parse_run_trace(trace(
            (0, rw, {"values": {"R[1].alarm": False, "R[1].done": False}}),
            (1, rw, {"values": {"N": 2, "R[2].alarm": False, "R[2].done": True}}),
            (2, rw, {"values": {"R[1].done": True}}),
            (3, rw, {"values": {"R[2].alarm": True}}))))
        generate(sos, tmp_path, target="agent-rego")
        f = tmp_path / "inputs.json"
        f.write_text(json.dumps([s["input"] for s in r["steps"]]))
        proc = subprocess.run(
            [opa, "eval", "--format", "json", "--data", str(tmp_path / "agent_policy.rego"),
             "--input", str(f),
             "[r | x := input[_]; r := data.cadl.agent_policy.result with input as x]"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        values = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
        assert values == [s["result"] for s in r["steps"]]


# === N-13: a prohibition that its own trigger violates ==============================

class TestN13:
    @pytest.mark.parametrize("rule, warned", [
        ('{when: "D -> R[*] : go", creates: going}', True),
        ('{when: "D -> R[*] : go", creates: going, if: "D.up == true"}', False),
        ('{when: "D -> R[1] : go", creates: going}', False),
    ])
    def test_institutional_event_content(self, rule, warned):
        sos = make_sos(f'counts_as:\n  - {rule}\nnorms:\n  - {{id: p, kind: prohibition, '
                       f'bearer: D, trigger: "D -> R[i] : go", content: going}}\n')
        assert any("N-13" in str(w) for w in type_check(sos).warnings) is warned


    @pytest.mark.parametrize("trigger, content, warned", [
        ("D -> R[i] : go", "D -> R[*] : go", True),
        ("D -> R[i] : go", "D -> R[i] : go", True),
        ("D -> R[2] : go", "D -> R[1..3] : go", True),
        ("D -> R[i] : go(1)", "D -> R[i] : go", True),
        ("D -> R[i] : go", "D -> R[i] : go(1)", False),
        ("D -> R[*] : go", "D -> R[i] : go", False),
        ("D -> R[i] : go", "D -> R[i] : stop", False),
        ("D -> R[i] : go", "R[i] -> D : go", False),
        ("R[1].x > 1", "R[1].x > 1", False),
        ("D -> R[i] : go", "D -> R[1..3] : go", True),
        ("D -> R[i] : go", "D -> R[1..2] : go", False),
        ("D -> R[i] : go(1)", "D -> R[i] : go(1.0)", True),
        ("D -> R[i] : go(1)", "D -> R[i] : go(2)", False),
        ("D -> S[i] : go", "D -> S[1..9] : go", False),
    ])
    def test_warning(self, trigger, content, warned):
        sos = make_sos(f'norms:\n  - {{id: p, kind: prohibition, bearer: D, '
                       f'trigger: "{trigger}", content: "{content}"}}\n')
        warnings = [str(w) for w in type_check(sos).warnings if "N-13" in str(w)]
        assert bool(warnings) is warned, warnings
