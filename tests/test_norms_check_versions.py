"""`cadl norms-check --version` with the versions of a run (Appendix K,
K.11; F.12), cadl 0.3.36: a version whose transitional rule for the
contract is `keep` or `switch` with `in_states:` is modelled with, per
instance, the version it follows, the version of a waiting switch and the
class of its lifecycle state; the next version takes effect at any
observation. Every random path of the model is compared with `cadl
norms-eval` on its trace, which names the version in force (key
`version`) and the lifecycle state (key `state`).

CADL_NORMS_CHECK_K11_PATHS sets the number of random paths per case
(default 60; the long run of 0.3.36 used several hundred)."""

from __future__ import annotations

import json
import os
import textwrap

import pytest

from cadl.norms_check import (
    NormsCheckError, PrismModel, build_model, compare_path, evaluate_path, random_paths,
    trace_lines,
)
from cadl.norms_eval import load_version
from cadl.parser import parse
from cadl.type_checker import type_check
from pathlib import Path

from tests.test_norms_check import AM_MULTI_V1, AM_MULTI_V2, AM_V1, AM_V2

EXAMPLES = Path(__file__).parent.parent / "examples"
PATHS = int(os.environ.get("CADL_NORMS_CHECK_K11_PATHS", "60"))

HEAD = """\
sos:
  name: T
  version: "%s"
  extensions:
    - norms: 0.1
    - amendment: 0.1
  actors:
    - {id: D, role: d}
    - {id: "R[1..3]", role: r}
    - {id: X, role: x}
  contracts:
    - id: C
      parties: [D, "R[*]"]
      lifecycle:
        states: [A, B, Z, E]
        initial: A
        terminal: [E]
        transitions:
          - { id: ab, from: A, to: B, on: "D -> R[i] : go" }
          - { id: bz, from: B, to: Z, on: "D -> R[i] : ask" }
          - { id: ze, from: Z, to: E, on: "R[i] -> D : done" }
"""

RULES = {"keep": "{ contract: C, rule: keep }",
         "inA": "{ contract: C, rule: switch, in_states: [A] }",
         "inB": "{ contract: C, rule: switch, in_states: [B] }",
         "inBZ": "{ contract: C, rule: switch, in_states: [B, Z] }",
         "switch": "{ contract: C, rule: switch }"}


def version(ver: str, body: str, prev: str | None = None, rule: str | None = None,
            amendment: bool = True):
    text = HEAD % ver + textwrap.indent(body, "      ")
    if not amendment:
        text = text.replace("    - amendment: 0.1\n", "")
    elif prev is None:
        text += "  amendment:\n    transitional: {default: switch}\n"
    else:
        text += (f"  amendment:\n    previous: \"{prev}\"\n    effective: {{ period: {int(prev) + 1} }}\n"
                 f"    transitional:\n      default: switch\n      contracts:\n"
                 f"        - {RULES[rule]}\n")
    sos = parse(text)
    result = type_check(sos)
    assert result.ok, result.errors
    return sos


def _model(r2: str, r3: str | None = None, multi: bool = False):
    v1_body, v2_body = (AM_MULTI_V1, AM_MULTI_V2) if multi else (AM_V1, AM_V2)
    vs = [("v2.cadl", version("2", v2_body, "1", r2))]
    if r3:
        vs.append(("v3.cadl", version("3", v1_body, "2", r3)))
    kw = {"members": ["R=2"], "instances": [{"i": 1}, {"i": 2}]} if multi else {"bindings": {"i": 1}}
    m = build_model(version("1", v1_body), versions=vs, **kw)
    return m, PrismModel(m.prism_text(), {"early_end": True})


CASES = [("keep", None, False), ("inA", None, False), ("inB", "switch", False),
         ("inBZ", "keep", False), ("keep", "inB", False), ("keep", None, True)]


@pytest.mark.parametrize("r2,r3,multi", CASES, ids=[f"{a}-{b}-{'multi' if c else 'one'}"
                                                    for a, b, c in CASES])
def test_model_against_norms_eval(r2, r3, multi):
    m, pm = _model(r2, r3, multi)
    assert m.k11 is not None
    inst = 2 if multi else 1
    paths = (random_paths(m.tr, PATHS, seed=7, p_amend=0.3, p_message=0.35, instances=inst)
             + random_paths(m.tr, PATHS, seed=8, p_amend=0.2, p_message=0.15, p_end=0.04,
                            p_keep=0.7, instances=inst))
    events: dict = {}
    for p in paths:
        assert not compare_path(m, pm, p), p
        for t in m.trs:
            for e in evaluate_path(t, p)["versions"]["events"]:
                events[e["event"]] = events.get(e["event"], 0) + 1
    rules = {r2, r3}
    if "keep" in rules:
        assert events.get("keep")
    if rules & {"inA", "inB", "inBZ"}:
        assert events.get("wait") and events.get("switch")


def test_traces_name_the_version_and_the_state():
    m, _pm = _model("inB")
    a = len(m.tr.choices)
    # observation 0 in state B, the version takes effect at observation 1
    p = [(0 + 2 * a * 1, False), (a + 2 * a * 1, False), (0, True)]
    lines = [json.loads(x) for x in trace_lines(m.tr, p)]
    assert [x["version"] for x in lines] == ["1", "2", "2"]
    assert [x["state"] for x in lines] == ["B", "B", "A"]
    r = evaluate_path(m.tr, p)
    assert [e["event"] for e in r["versions"]["events"]] == ["switch"]


def test_example_of_amendment_norms():
    m = build_model(load_version(EXAMPLES / "amendment_norms_v1.cadl"),
                    versions=[EXAMPLES / "amendment_norms_v2.cadl"], bindings={"i": 1})
    pm = PrismModel(m.prism_text(), {"early_end": True})
    assert m.k11 is not None and m.tr.lc_reps == ["Assigned", "PickedUp"]
    for p in random_paths(m.tr, PATHS, seed=3, p_amend=0.15):
        assert not compare_path(m, pm, p), p


def test_refusals():
    v1 = version("1", AM_V1)
    # a later version without the extension next to one with keep
    with pytest.raises(NormsCheckError, match="every version, the file too"):
        build_model(version("1", AM_V1, amendment=False), bindings={"i": 1},
                    versions=[("v2.cadl", version("2", AM_V2, "1", "keep"))])
    # the versions in another order than previous:
    v2 = version("2", AM_V2, "1", "keep")
    v3 = version("3", AM_V1, "2", "keep")
    with pytest.raises(NormsCheckError, match="in the order of their sequence"):
        build_model(v1, bindings={"i": 1}, versions=[("v3.cadl", v3), ("v2.cadl", v2)])
    # in_states with two instances that start on their own: the limit of commands
    with pytest.raises(NormsCheckError, match="commands"):
        build_model(
            version("1", AM_MULTI_V1), members=["R=2"], instances=[{"i": 1}, {"i": 2}],
            versions=[("v2.cadl", version("2", AM_MULTI_V2, "1", "inB")),
                      ("v3.cadl", version("3", AM_MULTI_V1, "2", "inBZ"))])
