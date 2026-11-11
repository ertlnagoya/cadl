"""Wildcard and range references in predicates (Appendix F, F.5,
"Wildcard and range references"): `A[*]` or `A[lo..hi]` outside the
domain of a quantifier or comprehension is shorthand. In the argument of
an aggregate it ranges over the members as a comprehension; elsewhere
its smallest enclosing comparison (or boolean operand) is quantified
universally over the members, one variable per set and index form.

The tests check the rewriting itself, that a predicate with wildcards and
its quantified form written by hand give the same values and the same
verdicts (offline, in the generated monitor and in the agents runtime),
and the warnings of N-15."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest

from cadl.norms_eval import UNKNOWN, desugar_wildcards, evaluate, parse_trace
from cadl.parser import parse_file, parse_rule
from cadl.type_checker import type_check
from cadl.unparse import expr_to_source

from .test_norms_quantifiers import evaluator

DATA = Path(__file__).parent / "data"
WILD = DATA / "norms_wildcards.cadl"
QUANTIFIED = DATA / "norms_wildcards_quantified.cadl"


def rewritten(text: str, notes: list | None = None) -> str:
    return expr_to_source(desugar_wildcards(parse_rule(text), notes))


# === The rewriting =================================================================

class TestRewriting:
    @pytest.mark.parametrize("text, expected", [
        ("R[*].x > 10", "for all *1 in R[*]: R[*1].x > 10"),
        ("R[1..2].x > 10", "for all *1 in R[1..2]: R[*1].x > 10"),
        ("S[2..N].x > 10", "for all *1 in S[2..N]: S[*1].x > 10"),
        # several references of one set and form: one member
        ("R[*].x > R[*].y", "for all *1 in R[*]: R[*1].x > R[*1].y"),
        # two sets, or two forms of one set: every combination
        ("R[*].x == S[*].y", "for all *1 in R[*]: for all *2 in S[*]: R[*1].x == S[*2].y"),
        ("R[*].x < R[1..2].x",
         "for all *1 in R[*]: for all *2 in R[1..2]: R[*1].x < R[*2].x"),
        # the comparison is the scope: NOT, AND, OR stay outside
        ("NOT R[*].x > 3", "NOT (for all *1 in R[*]: R[*1].x > 3)"),
        ("R[*].x > 3 AND D.up", "(for all *1 in R[*]: R[*1].x > 3) AND D.up"),
        ("R[*].x > 3 OR R[*].y > 3",
         "(for all *1 in R[*]: R[*1].x > 3) OR (for all *2 in R[*]: R[*2].y > 3)"),
        ("R[*].x IN [a, b]", "for all *1 in R[*]: R[*1].x == __in__(a, b)"),
        # outside every comparison: the boolean operand
        ("R[*].ok", "for all *1 in R[*]: R[*1].ok"),
        ("NOT R[*].ok", "NOT (for all *1 in R[*]: R[*1].ok)"),
        ("within(5s, R[*].ok)", "within(5s, for all *1 in R[*]: R[*1].ok)"),
        ("(R[*].x > 1) == true", "(for all *1 in R[*]: R[*1].x > 1) == true"),
        # inside a quantifier: the comparison of its body
        ("for all i in S: R[*].x > S[i].y",
         "for all i in S: for all *1 in R[*]: R[*1].x > S[i].y"),
        # an aggregate argument ranges over the members
        ("count(R[*].status == Collision) >= 2",
         "count(R[*1].status == Collision for *1 in R[*]) >= 2"),
        ("sum(R[*].x * R[*].y) > 1", "sum(R[*1].x * R[*1].y for *1 in R[*]) > 1"),
        ("avg(S[2..N].x) > 1", "avg(S[*1].x for *1 in S[2..N]) > 1"),
        ("sum(R[*].x) > S[*].y",
         "for all *2 in S[*]: sum(R[*1].x for *1 in R[*]) > S[*2].y"),
        ("sum(R[*].x + max(S[*].y)) > 3",
         "sum(R[*2].x + max(S[*1].y for *1 in S[*]) for *2 in R[*]) > 3"),
        # a comprehension: its element is no aggregate shorthand
        ("count(R[*].x > S[k].y for k in S) > 1",
         "count((for all *1 in R[*]: R[*1].x > S[k].y) for k in S) > 1"),
        # an aggregate over two sets is left as written (not evaluated)
        ("sum(R[*].x * S[*].y) > 1", "sum(R[*].x * S[*].y) > 1"),
    ])
    def test_rewriting(self, text, expected):
        assert rewritten(text) == expected

    @pytest.mark.parametrize("text", [
        "R[1].x > 10", "for all j in R[*]: R[j].x > 1", "sum(r.x for r in R[1..2]) > 1",
        "exists j in S[2..N]: S[j].x == 1", "D.up AND within(3s, R[i].x == 0)",
    ])
    def test_without_wildcards_the_tree_is_kept(self, text):
        expr = parse_rule(text)
        assert desugar_wildcards(expr) is expr

    def test_the_tree_is_not_changed(self):
        expr = parse_rule("NOT R[*].x > S[*].y AND count(R[*].ok) > 1")
        before = repr(expr)
        desugar_wildcards(expr)
        assert repr(expr) == before

    @pytest.mark.parametrize("text, kinds", [
        ("R[*].x > 1", []),
        ("R[*].x > R[*].y", []),
        ("NOT R[*].x > 1", ["not"]),
        ("NOT (D.up AND R[*].ok)", ["not"]),
        ("NOT NOT R[*].ok", []),
        ("R[*].x == S[*].y", ["pairs"]),
        ("R[*].x > 1 OR R[*].y > 1", ["or"]),
        ("R[*].x > 1 OR S[*].y > 1", []),
        ("R[*].x > 1 AND R[*].y > 1", []),
        ("sum(R[*].x * S[*].y) > 1", ["aggregate"]),
        ("count(R[*].ok) > 1", []),
    ])
    def test_notes(self, text, kinds):
        notes: list = []
        rewritten(text, notes)
        assert [k for k, _ in notes] == kinds


# === Same values as the quantified form ============================================

PAIRS = [
    ("R[*].x > 1", "for all j in R: R[j].x > 1"),
    ("NOT R[*].x > 1", "NOT (for all j in R: R[j].x > 1)"),
    ("R[*].x == S[*].y", "for all j in R: for all k in S: R[j].x == S[k].y"),
    ("R[*].x >= R[*].y", "for all j in R: R[j].x >= R[j].y"),
    ("S[*].x == 1 OR R[*].y == 0",
     "(for all j in S: S[j].x == 1) OR (for all j in R: R[j].y == 0)"),
    ("S[2..N].x < 2", "for all j in S[2..N]: S[j].x < 2"),
    ("R[2..3].x != S[1..2].x", "for all j in R[2..3]: for all k in S[1..2]: R[j].x != S[k].x"),
    ("count(S[*].x == 1) >= 1", "count(S[j].x == 1 for j in S) >= 1"),
    ("sum(R[*].x) < 3", "sum(R[j].x for j in R) < 3"),
    ("max(R[1..2].x) > min(S[*].y)",
     "max(R[j].x for j in R[1..2]) > min(S[j].y for j in S)"),
    ("avg(R[*].x) > S[*].y", "for all k in S: avg(R[j].x for j in R) > S[k].y"),
    ("for all i in S: R[*].x <= S[i].x",
     "for all i in S: for all j in R: R[j].x <= S[i].x"),
]


def test_values_equal_the_quantified_form():
    """At random observations (members of S named or not, N with or
    without a value, quantities 0, 1, 2 or none), each predicate and its
    quantified form have the same three-valued value."""
    rng = random.Random(20261111)
    decided = 0
    pairs = [(desugar_wildcards(parse_rule(a)), parse_rule(b)) for a, b in PAIRS]
    for _ in range(400):
        values = {}
        for name in [f"R[{i}]" for i in (1, 2, 3)] + [f"S[{i}]" for i in (1, 2, 3, 5)]:
            if name.startswith("S") and rng.random() < 0.3:
                continue                                    # not named
            for q in ("x", "y"):
                v = rng.choice((0, 1, 2, None, "none"))
                if v != "none":
                    values[f"{name}.{q}"] = v
            values.setdefault(f"{name}.z", 0)
        if rng.random() < 0.5:
            values["N"] = rng.choice((2, 3, 5))
        obs = [{"t": 0, "values": values}]
        ev = evaluator(obs)
        for (star, quant), (text, _) in zip(pairs, PAIRS):
            a, b = ev.holds(star, 0, 0), ev.holds(quant, 0, 0)
            assert a is b or a == b, (text, values, a, b)
            decided += a is not UNKNOWN
    assert decided >= 1500, decided


# === Same verdicts as the quantified form, offline and in the monitor ==============

_VOCAB = {
    "bindings": {"i": 2},
    "messages": [
        ("D", "S[1]", "assign", None), ("D", "S[2]", "assign", None),
        ("D", "S[5]", "assign", None), ("S[2]", "D", "ack", None),
        ("S[5]", "D", "ack", None), ("D", "S[2]", "release", None),
    ],
    "values": {
        "D.up": [True, True, True, False],
        "R[1].load": [0, 3, 6, 9], "R[2].load": [0, 2, 9], "R[3].load": [1, 5, 12], "R[1].job": [0, 7], "R[2].job": [0, 7, 7],
        "R[3].job": [0, 7, None], "S[1].job": [0, 7, 7, None], "S[2].job": [0, 0, 7, None],
        "S[3].job": [0, 7], "S[5].job": [0, 7], "S[2].load": [1, 5, 9, None],
        "S[3].load": [1, 5, 9], "S[1].load": [1, 3, 9], "N": [3, 3, 3, 5, None],
    },
    "states": ["Open", "Open", "Closed"],
}


def _text_map() -> dict:
    """The predicate texts of WILD -> those of QUANTIFIED, line by line."""
    pairs = {}
    quoted = re.compile(r'"([^"]*)"')
    def body(path: Path) -> list:
        return [line for line in path.read_text().splitlines() if not line.startswith("#")]
    for a, b in zip(body(WILD), body(QUANTIFIED)):
        if a != b:
            for x, y in zip(quoted.findall(a), quoted.findall(b)):
                if x != y:
                    pairs[x] = y
    return pairs


def _as_quantified(result: dict, texts: dict) -> dict:
    """A result of WILD with its texts replaced by those of QUANTIFIED;
    the warnings, which name the predicates by their own text, are left out."""
    out = json.dumps({k: v for k, v in result.items() if k != "warnings"}, sort_keys=True)
    for a in sorted(texts, key=len, reverse=True):
        out = out.replace(json.dumps(a)[1:-1], json.dumps(texts[a])[1:-1])
    return json.loads(out)


def _random_traces(count: int, seed: int) -> list:
    from .test_norms_monitor import _random_trace
    rng = random.Random(seed)
    return [_random_trace(rng, _VOCAB) for _ in range(count)]


def test_files_are_checked():
    for path in (WILD, QUANTIFIED):
        assert type_check(parse_file(path)).ok
    texts = _text_map()
    assert len(texts) == 12 and all("[*]" in a or ".." in a for a in texts)


def test_verdicts_equal_the_quantified_form(monkeypatch):
    """On random traces the contract with wildcards and the contract with
    the quantified forms give the same result: norm states and their
    history, violations, institutional events, item verdicts."""
    from .test_norms_monitor import _memoize_parser
    _memoize_parser(monkeypatch)
    wild, quant, texts = parse_file(WILD), parse_file(QUANTIFIED), _text_map()
    seen: set = set()
    for lines in _random_traces(300, 20261117):
        a = evaluate(wild, parse_trace(lines))
        b = evaluate(quant, parse_trace(lines))
        assert _as_quantified(a, texts) == {k: v for k, v in b.items() if k != "warnings"}, lines
        seen.update((n["id"], n["verdict"]) for n in a["norms"])
        seen.update((g["text"], g["verdict"]) for g in a["guarantees"])
    # the traces decide each norm and each guarantee both ways
    for norm in ("same_job", "light", "settle"):
        assert {(norm, "violated"), (norm, "fulfilled")} <= seen, (norm, seen)
    assert ("busy_ack", "violated") in seen and ("may_assign", "fulfilled") in seen
    for text in ("S[*].load < 9", "within(5s, S[*].job == 0)", "sum(R[*].load) < 20"):
        assert {(text, "violated"), (text, "fulfilled")} & seen, (text, seen)


def test_monitor_equals_offline(tmp_path, monkeypatch):
    """The generated monitor of the contract with wildcards gives the
    result of `cadl norms-eval` on random traces: replayed, fed line by
    line, and after every observation."""
    from cadl.codegen.norms_monitor_gen import generate_norms_monitor_module
    from .test_norms_monitor import _batch, _memoize_parser, _norms_eval, _prefix, _strip
    _memoize_parser(monkeypatch)
    sos = parse_file(WILD)
    source = generate_norms_monitor_module(sos)
    cases = [{"source": source, "steps": True, "lines": lines}
             for lines in _random_traces(120, 20261118)]
    for case, got in zip(cases, _batch(tmp_path, cases)):
        expected = _norms_eval(sos, case["lines"])
        assert got.get("result") == expected["result"], case["lines"]
        assert got["manual"] == expected["result"], case["lines"]
        nonblank = [line for line in case["lines"] if line.strip()]
        for k, at_k in enumerate(got["prefixes"]):
            want = _norms_eval(sos, _prefix(nonblank, k))["result"]
            assert _strip(at_k) == _strip(want), (k, case["lines"])


# === N-15 ============================================================================

def _warnings(path_or_text) -> list:
    sos = parse_file(path_or_text)
    result = type_check(sos)
    assert result.ok, result.errors
    return [str(w) for w in result.warnings if "(N-15)" in str(w)]


def test_n15_on_the_example_file():
    found = _warnings(WILD)
    assert len(found) == 3, found
    joined = "\n".join(found)
    assert "content: of norm 'same_job'" in joined and "R[*] and S[1..2]" in joined
    assert "until: of norm 'may_assign'" not in joined        # two sets under OR: no note
    assert "content: of norm 'light'" in joined and "under NOT" in joined
    assert "guarantee: item 4" in joined
    assert _warnings(QUANTIFIED) == []


# === The agents runtime ===============================================================

AGENT_CASES = [
    # (the predicates with wildcards, the same written as quantifiers)
    ({"until": "R[*].load > 5", "trigger": "count(R[*].load > 2) >= 1"},
     {"until": "for all j in R: R[j].load > 5",
      "trigger": "count(R[j].load > 2 for j in R) >= 1"}),
    ({"until": "NOT R[1..2].load < 9", "trigger": "sum(R[*].load) > 3"},
     {"until": "NOT (for all j in R[1..2]: R[j].load < 9)",
      "trigger": "sum(R[j].load for j in R) > 3"}),
]


def _agents_source(pred: dict) -> str:
    from .test_agents_extension import norms, perm, proh, source
    return source(norms(
        perm("p_read", "AG", "AG -> T : read", f'trigger: "{pred["trigger"]}"\n'
                                                f'until: "{pred["until"]}"'),
        proh("q_write", "AG", "AG -> T : write", f'trigger: "{pred["until"]}"')))


def test_agents_runtime_equals_the_quantified_form():
    """G.13: the runtime judges norms whose trigger: and until: use
    wildcards, as it judges the quantified forms. Up to cadl 0.3.23 such a
    norm was not judged and its in-force flag was unknown for the whole run."""
    from cadl.agents_runtime import parse_run_trace, run
    from cadl.parser import parse
    from .test_agents_runtime import decisions, m, trace
    rng = random.Random(20261119)
    allowed = denied = 0
    for star, quant in AGENT_CASES:
        a, b = parse(_agents_source(star)), parse(_agents_source(quant))
        assert type_check(a).ok and type_check(b).ok
        for _ in range(20):
            rows = []
            for t in range(6):
                values = {f"R[{i}].load": rng.choice((0, 3, 6, 9)) for i in (1, 2, 3)
                          if rng.random() < 0.6}
                rows.append((t, [m("AG", "T", "read"), m("AG", "T", "write")],
                             {"values": values}))
            lines = trace(*rows, instances=[{"instance": "c1", "contract": "C", "bindings": {}}])
            da = decisions(run(a, parse_run_trace(lines)))
            db = decisions(run(b, parse_run_trace(lines)))
            assert da == db, lines
            allowed += sum(d[2] == "allow" for d in da)
            denied += sum(d[2] == "deny" for d in da)
    assert allowed >= 50 and denied >= 50, (allowed, denied)
