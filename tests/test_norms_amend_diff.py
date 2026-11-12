"""Differential test of amendments (Appendix F, F.6): what `cadl norms-diff`
reports for two versions against what `cadl norms-eval` does at an
amendment on a trace, for every norm instance (carried over with its
elapsed time, discharged, transformed, removed, added). See
tests/norms_amend_diff.py.

CADL_NORMS_AMEND_PAIRS sets the number of random pairs of versions with a
trace each (default 150; the long run of the report used 3000). The
mutation checks inject errors into both commands and require that the
comparison finds each of them on the same pairs.
"""

from __future__ import annotations

import inspect
import os
import random

import pytest

from cadl import norms_diff, norms_eval
from cadl.norms_check import PrismModel, all_paths, build_model, compare_path
from cadl.norms_diff import diff_versions
from cadl.norms_eval import evaluate, parse_trace
from cadl.parser import parse
from cadl.type_checker import type_check

from .norms_amend_diff import compare, make_pair, make_trace, run_at_amendment

PAIRS = int(os.environ.get("CADL_NORMS_AMEND_PAIRS", "150"))


@pytest.fixture(scope="module")
def corpus():
    """The hand-made pairs below, which reach the rare rows of F.6 for
    certain, and the random pairs."""
    out = []
    for name, (v1, v2, lines, k) in HAND.items():
        old, new = parse(HEAD + v1), parse(HEAD + v2)
        assert type_check(old).ok and type_check(new).ok, name
        out.append((name, old, new, lines, k, [n.id for n in old.contracts[0].norms], [name]))
    rng = random.Random(2026)
    for _ in range(PAIRS):
        name, kw, old, new, log, old_ids = make_pair(rng)
        lines, k = make_trace(rng, old, new, kw)
        out.append((name, old, new, lines, k, old_ids, log))
    return out


def _failures(corpus, first: bool = False) -> list:
    bad = []
    for name, old, new, lines, k, old_ids, log in corpus:
        try:
            d = compare(name, old, new, lines, k, old_ids)
        except Exception as e:             # a mutant may also crash
            d = [f"{type(e).__name__}: {e}"]
        if d:
            bad.append((name, log, k, d))
            if first:
                break
    return bad


def test_norms_diff_predicts_norms_eval(corpus):
    bad = _failures(corpus)
    assert not bad, bad[:3]


def test_the_corpus_reaches_the_cases_of_f6(corpus):
    """The pairs and traces exercise every row of the table of F.6."""
    seen = set()
    for _name, old, new, lines, k, _ids, _log in corpus:
        for e in run_at_amendment(old, new, lines, k)["effects"]:
            seen.add((e["effect"], e["from_state"], e["to_state"]))
    need = {("carry_over", "active", "active"), ("carry_over", "active", "discharged"),
            ("discharge", "active", "discharged"), ("transform", "active", "discharged"),
            ("activated", "inactive", "active"), ("added", None, "active"),
            ("added", None, "inactive"), ("removed", "inactive", None),
            ("new_definition", "inactive", "inactive")}
    assert need <= seen, need - seen


# --- Mutation checks ------------------------------------------------------------------

def _patch(monkeypatch, owner, name: str, old: str, new: str) -> None:
    """Replace `owner.name` by a copy of its source with `old` replaced by
    `new` (exactly once)."""
    func = getattr(owner, name)
    src = inspect.getsource(func)
    assert src.count(old) == 1, (name, old)
    src = src.replace(old, new)
    if src.startswith(" "):                  # a method: keep its indentation
        src = "if True:\n" + src
    module = inspect.getmodule(func)
    scope: dict = {}
    exec(compile(src, module.__file__, "exec"), vars(module), scope)
    monkeypatch.setattr(owner, name, scope[name])


E = norms_eval._Engine
MUTANTS = {
    # norms-eval
    "carry over restarts the elapsed time": (
        E, "_carry", '"active_since",\n                     "elapsed_before", ',
        '\n                     '),
    "transform without the elapsed time": (
        E, "_amend", "x.elapsed_before = elapsed if timed else 0", "x.elapsed_before = 0"),
    "discharge read as carry_over": (
        E, "_amend", 'elif o.d.on_amendment == "discharge":', "elif False:"),
    "a new kind carries over": (
        E, "_amend", "elif nd is None or nd.kind != o.d.kind:", "elif nd is None:"),
    "new norms are not activated": (
        E, "_amend", "and x.d.id not in self.targets):", "and False):"),
    "an inactive norm of no version stays": (
        E, "_amend",
        'effect(oid, "removed", state, None, "the new version has no norm with this id")\n'
        "                    continue",
        "table[oid] = o\n                    continue"),
    "a transform target that is not inactive is activated": (
        E, "_amend", 'elif x.state != "inactive" or x.frozen:', "elif x.frozen:"),
    "a late prohibition stays in force": (
        E, "_amend", 'elif (nd.kind == "prohibition" and inst.deadline is not None',
        'elif (False and inst.deadline is not None'),
    "a late obligation stays in force": (
        E, "_amend", "and elapsed > inst.deadline):\n                    why = (f\"the elapsed",
        "and False):\n                    why = (f\"the elapsed"),
    # norms-diff
    "removed norms are not reported": (
        norms_diff, "_diff_contract", '"norm_removed", f"{nid}: removed',
        '"norm_kept", f"{nid}: removed'),
    "discharge is not reported": (
        norms_diff, "_diff_contract", '"info", "discharge",', '"info", "discharge_",'),
    "kind changes are not reported": (
        norms_diff, "_diff_contract", '"warning", "kind_changed",', '"warning", "kind_",'),
    "the kind of a transform target is wrong": (
        norms_diff, "_diff_contract", "target_kind=new_norms[target].kind",
        'target_kind="permission"'),
    "the new deadline is not reported": (
        norms_diff, "_diff_contract", '"old_s": old_s, "new_s": new_s}',
        '"old_s": old_s, "new_s": old_s}'),
    "a new reparation target counts as active at once": (
        norms_diff, "_diff_contract", "at_once = n.trigger is None and nid not in new_targets",
        "at_once = n.trigger is None"),
}


@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_mutants_are_detected(mutant, corpus, monkeypatch):
    _patch(monkeypatch, *MUTANTS[mutant])
    assert _failures(corpus, first=True), f"the mutant '{mutant}' is not detected"


# --- Cases that the comparison found ----------------------------------------------

HEAD = """\
sos:
  name: T
  extensions:
    - norms: 0.1
  actors:
    - {id: D, role: d}
    - {id: R, role: r}
  contracts:
    - id: C
      parties: [D, R]
      norms:
"""
QUIET = '        - {id: quiet, kind: prohibition, bearer: R, content: "R -> D : noise"%s}\n'
HOLD = """\
        - id: hold
          kind: power
          bearer: D
          content: "D -> R : pause"
          effects: {suspend: [quiet]}
"""


def _norm(nid, kind, content, extra=""):
    return (f"        - {{id: {nid}, kind: {kind}, bearer: R, content: \"{content}\""
            + (f", {extra}" if extra else "") + "}\n")


ASK = 'trigger: "D -> R : ask"'
PAUSE = """\
        - id: hold
          kind: power
          bearer: D
          content: "D -> R : pause"
          effects: {suspend: [duty]}
"""
HAND = {
    "late obligation": (
        _norm("duty", "obligation", "R -> D : done", f"{ASK}, deadline: 5s"),
        _norm("duty", "obligation", "R -> D : done", f"{ASK}, deadline: 1s"),
        ['{"t": 0}', '{"t": 1, "messages": [{"from": "D", "to": "R", "name": "ask"}]}',
         '{"t": 3, "amend": "new"}', '{"t": 4}'], 2),
    "late suspended prohibition": (
        QUIET % "" + HOLD, QUIET % ", deadline: 2s" + HOLD,
        ['{"t": 0}', '{"t": 3, "messages": [{"from": "D", "to": "R", "name": "pause"}]}',
         '{"t": 5, "amend": "new"}', '{"t": 6}'], 2),
    "inactive norm removed": (
        _norm("later", "obligation", "R -> D : done", f"{ASK}, deadline: 5s")
        + _norm("keep", "permission", "R -> D : hello"),
        _norm("keep", "permission", "R -> D : hello"),
        ['{"t": 0}', '{"t": 2, "amend": "new"}', '{"t": 3}'], 1),
    "kind changed": (
        _norm("duty", "obligation", "R -> D : done", "deadline: 10s"),
        _norm("duty", "prohibition", "R -> D : done", "deadline: 10s"),
        ['{"t": 0}', '{"t": 2, "amend": "new"}', '{"t": 3}'], 1),
    "suspended instance transformed": (
        _norm("duty", "obligation", "R -> D : done",
              "deadline: 10s, on_amendment: {transform: duty2}") + PAUSE,
        _norm("duty2", "obligation", "R -> D : report", f"{ASK}, deadline: 10s")
        + PAUSE.replace("[duty]", "[duty2]"),
        ['{"t": 0}', '{"t": 2, "messages": [{"from": "D", "to": "R", "name": "pause"}]}',
         '{"t": 4, "amend": "new"}', '{"t": 5}'], 2),
    "transform target not inactive": (
        _norm("a", "obligation", "R -> D : done", "deadline: 10s, on_amendment: {transform: b}")
        + _norm("b", "obligation", "R -> D : report", "deadline: 10s"),
        _norm("b", "obligation", "R -> D : report", "deadline: 10s"),
        ['{"t": 0}', '{"t": 2, "amend": "new"}', '{"t": 3}'], 1),
}


def _versions():
    v1 = parse(HEAD + QUIET % "" + HOLD)
    v2 = parse(HEAD + QUIET % ", deadline: 2s" + HOLD)
    assert type_check(v1).ok and type_check(v2).ok
    return v1, v2


@pytest.mark.parametrize("pause_at", [3, 5])
def test_late_prohibition_is_fulfilled_by_the_amendment(pause_at):
    """F.6: when the elapsed time of a carried-over prohibition exceeds its
    new deadline, its window has closed and it is fulfilled when the new
    version takes effect, also when it is suspended (pause at t=3) and
    when a power suspends it at the same observation (pause at t=5). Up
    to cadl 0.3.26 it stayed suspended."""
    v1, v2 = _versions()
    lines = ['{"t": 0}',
             '{"t": %d, "messages": [{"from": "D", "to": "R", "name": "pause"}]%s}'
             % (pause_at, ', "amend": "v2"' if pause_at == 5 else ""),
             '{"t": 6}']
    if pause_at == 3:
        lines.insert(2, '{"t": 5, "amend": "v2"}')
    r = evaluate(v1, parse_trace(lines), versions={"v2": v2})
    quiet = {n["id"]: n for n in r["norms"]}["quiet"]
    assert (quiet["state"], quiet["time"]) == ("fulfilled", 5)
    (effect,) = [e for e in r["amendments"][0]["effects"] if e["norm"] == "quiet"]
    assert effect["to_state"] == "fulfilled" and "window has closed" in effect["reason"]
    text = diff_versions(v1, v2)["contracts"][0]["findings"][0]["text"]
    assert "fulfilled when the new version takes effect" in text


def test_norms_check_follows_the_late_prohibition():
    """The model of F.12 applies the same rule: every path of at most four
    observations agrees with norms-eval."""
    v1, v2 = _versions()
    m = build_model(v1, versions=[("v2", v2)], step="1s", horizon=4)
    pm = PrismModel(m.prism_text(), {"early_end": True})
    paths = all_paths(m.tr, 4)
    assert len(paths) > 1000
    for p in paths:
        assert not compare_path(m, pm, p), p


def test_discharge_is_reported():
    v1, v2 = _versions()
    v1d = parse((HEAD + QUIET % ", on_amendment: discharge" + HOLD))
    findings = diff_versions(v1d, v2)["contracts"][0]["findings"]
    assert findings[0]["kind"] == "discharge"
    assert "is discharged" in findings[0]["text"]


def test_numeric_deadlines_are_compared_exactly():
    """Two numeric deadlines that round to the same millisecond differ for
    norms-eval (F.5), so norms-diff reports the change."""
    a = parse(HEAD + QUIET % ", deadline: 2.0004" + HOLD)
    b = parse(HEAD + QUIET % ", deadline: 2.0001" + HOLD)
    (f,) = diff_versions(a, b)["contracts"][0]["findings"]
    assert f["kind"] == "carry_over_changed"
    assert f["changes"]["deadline"] == {"old": "2.0004s", "new": "2.0001s",
                                        "old_s": "2.0004", "new_s": "2.0001"}
