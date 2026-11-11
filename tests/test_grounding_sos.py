"""The quantities of the SoS (Appendix J, J.4, "The quantities of the
SoS"): α̂, β̂ and χ over all contracts of a file, in `cadl
norms-indices` (key `sos`) and in the IR (`institution.grounding`). An
item is qualified by its contract; β̂ counts the powers of one single
actor over all contracts."""

from __future__ import annotations

import copy
import json
import random
from fractions import Fraction as F

import yaml

from cadl import grounding as G
from cadl.cli import _ir_to_dict, main
from cadl.parser import parse
from cadl.sim import lower_to_ir
from cadl.type_checker import type_check

from .test_grounding_extension import ACTORS, EXAMPLE, _random_contract

HEAD = """sos:
  name: T
  extensions:
    - norms: 0.1
    - grounding: 0.1
  actors:
    - {id: D, role: d}
    - {id: "R[1..3]", role: r}
    - {id: X, role: x}
    - {id: Y, role: y}
  contracts:
"""

C1 = """    - id: C1
      parties: [D, "R[*]"]
      information:
        sharing:
          - "D -> R[*] : route"
      grounding:
        items: {route: D, status: "R[*]"}
      norms:
        - {id: p1, kind: power, bearer: D, content: "D -> R[i] : stop", effects: {discharge: [o1]}}
        - {id: p2, kind: power, bearer: D, content: "D -> R[i] : go", effects: {activate: [o1]}}
        - {id: o1, kind: obligation, bearer: "R[i]", content: "R[i] -> D : status",
           deadline: 5s, on_violation: {sanction: fine}}
"""
C2 = """    - id: C2
      parties: [D, X]
      grounding:
        items: {route: X}
      norms:
        - {id: p3, kind: power, bearer: X, content: "X -> D : halt", effects: {suspend: [q1]}}
        - {id: q1, kind: prohibition, bearer: X, content: "X -> D : spam",
           on_violation: {reparation: o2}}
        - {id: o2, kind: obligation, bearer: X, content: "X -> D : sorry", deadline: 5s,
           on_violation: {magnitude: 2}}
"""
C3 = """    - id: C3
      parties: [X, Y]
      information:
        sharing:
          - "Y -> X : route"
      grounding:
        items: {route: Y}
      norms:
        - {id: p4, kind: power, bearer: X, content: "X -> Y : halt", effects: {discharge: [o3]}}
        - {id: p5, kind: power, bearer: Y, content: "Y -> X : halt", effects: {discharge: [o3]}}
        - {id: o3, kind: obligation, bearer: Y, content: "Y -> X : report", deadline: 5s}
"""


def sos_of(*contracts: str):
    sos = parse(HEAD + "".join(contracts))
    assert type_check(sos).ok, [str(e) for e in type_check(sos).errors]
    return sos


def values(si: G.SoSIndices) -> tuple:
    return (si.alpha, si.beta, si.coverage)


# === Hand-computed examples ============================================================

class TestHandComputed:
    def test_two_contracts(self):
        si = G.sos_indices(sos_of(C1, C2))
        # C1: (route, R) given by the entry, (status, D) not; C2: (route@C2, D) not.
        assert (si.given, si.pairs, si.by("sharing")) == (1, 3, 1)
        # D bears p1 and p2, X bears p3
        assert (si.largest, si.powers, si.top) == (2, 3, ["D"])
        # o1 (sanction), q1 (none, reparation only), o2 (magnitude 2)
        assert (si.incentive, si.norms, si.reparation_only) == (2, 3, 1)
        assert values(si) == (F(1, 3), F(2, 3), F(2, 3))

    def test_three_contracts(self):
        si = G.sos_indices(sos_of(C1, C2, C3))
        # C3: (route@C3, X) given by the entry "Y -> X : route"
        assert (si.given, si.pairs) == (2, 4)
        # X bears p3 in C2 and p4 in C3: K(X) = 2 = K(D); both are named
        assert si.singles == {"D": 2, "X": 2, "Y": 1}
        assert (si.largest, si.powers, si.top) == (2, 5, ["D", "X"])
        assert values(si) == (F(1, 2), F(2, 5), F(2, 4))
        # the per-contract β̂ are 1, 1 and 1/2: β̂_SoS lies below all of them
        betas = [ci.beta.value for ci in si.contracts]
        assert betas == [1, 1, F(1, 2)] and si.beta < min(betas)

    def test_same_item_in_two_contracts_counts_twice(self):
        # `route` is declared in C1 (origin D) and C2 (origin X); the entry of
        # C1 that gives route to R gives nothing in C2
        si = G.sos_indices(sos_of(C1, C2))
        assert [ci.alpha.pairs for ci in si.contracts] == [
            [("route", "R"), ("status", "D")], [("route", "D")]]
        assert si.pairs == 3

    def test_one_contract_or_no_extension_has_none(self):
        assert G.sos_indices(sos_of(C1)) is None
        plain = parse((HEAD + C1 + C2).replace("    - grounding: 0.1\n", "")
                      .replace("      grounding:\n        items: {route: D, status: \"R[*]\"}\n", "")
                      .replace("      grounding:\n        items: {route: X}\n", "")
                      .replace(", on_violation: {magnitude: 2}", ""))
        assert type_check(plain).ok
        assert G.sos_indices(plain) is None

    def test_undefined(self):
        empty = """    - id: E{n}
      parties: [D]
"""
        si = G.sos_indices(sos_of(empty.format(n=1), empty.format(n=2)))
        assert values(si) == (None, None, None)
        rep = G.sos_report(si)
        assert (rep["alpha"]["defined"], rep["alpha"]["undefined"]) == (
            False, "no contract has a pair (item, party)")
        assert rep["beta"]["undefined"] == "no contract has a power"
        assert rep["coverage"]["undefined"] == "no contract has an obligation or a prohibition"


# === norms-indices and the IR ============================================================

class TestOutputs:
    def test_report_has_sos_only_with_two_contracts(self):
        rep = G.report(sos_of(C1, C2))
        assert rep["sos"]["contracts"] == ["C1", "C2"]
        assert rep["sos"]["beta"]["exact"] == "2/3"
        assert rep["sos"]["beta"]["bearers"] == {"D": 2, "X": 1}
        assert "sos" not in G.report(sos_of(C1))
        assert "sos" not in G.report(sos_of(C1, C2), "C1")       # one contract asked for

    def test_per_contract_output_unchanged(self):
        sos = sos_of(C1, C2, C3)
        rep = G.report(sos)
        for c in rep["contracts"]:
            assert c == G.report(sos, c["id"])["contracts"][0]
            alone = sos_of({"C1": C1, "C2": C2, "C3": C3}[c["id"]])
            assert c == G.report(alone)["contracts"][0]

    def test_text(self, tmp_path, capsys):
        path = tmp_path / "f.cadl"
        path.write_text(HEAD + C1 + C2)
        assert main(["norms-indices", str(path)]) == 0
        out = capsys.readouterr().out
        assert "SoS: all 2 contracts (C1, C2)" in out
        assert "alpha^_SoS (information sharing) = 1/3" in out
        assert "beta^_SoS (concentration of powers) = 2/3" in out
        assert "single actor(s) with the most powers: D" in out
        assert "chi_SoS (incentive coverage) = 2/3" in out
        assert main(["norms-indices", str(path), "--contract", "C2"]) == 0
        assert "SoS" not in capsys.readouterr().out

    def test_ir(self):
        d = _ir_to_dict(lower_to_ir(sos_of(C1, C2)))
        assert d["institution"]["grounding"] == {
            "contracts": ["C1", "C2"],
            "indices": {
                "alpha": {"given": 1, "pairs": 3, "by_sharing": 1, "by_obligation": 0,
                          "by_permission": 0, "member_specific": 0},
                "beta": {"largest": 2, "powers": 3, "bearers": {"D": 2, "X": 1}},
                "coverage": {"incentive": 2, "norms": 3, "reparation_only": 1,
                             "transition": 0}}}
        assert "grounding" not in _ir_to_dict(lower_to_ir(sos_of(C1)))["institution"]
        assert "grounding" not in _ir_to_dict(lower_to_ir(parse(EXAMPLE.read_text())))[
            "institution"]


# === Lemmas and counterexamples ===========================================================

def _random_file(rng: random.Random, n: int | None = None) -> list:
    contracts = []
    for k in range(n if n is not None else rng.randint(2, 4)):
        c = _random_contract(rng)
        c["id"] = f"C{k + 1}"
        contracts.append(c)
    return contracts


def _sos(contracts: list):
    return parse(yaml.safe_dump({"sos": {
        "name": "T", "extensions": [{"norms": 0.1}, {"grounding": 0.1}],
        "actors": [{"id": n if r is None else f"{n}[{r}]", "role": "r"}
                   for n, r in ACTORS.items()],
        "contracts": contracts}}, sort_keys=False))


def _si(contracts: list) -> G.SoSIndices:
    return G.sos_indices(_sos(contracts))


N = 150


class TestLemmas:
    def test_ratios_of_sums_and_bounds(self):
        """α̂_SoS and χ_SoS lie between the least and the largest value of
        the contracts where they are defined; β̂_SoS is at most the mean of
        the β̂ of the contracts weighted by their numbers of powers, with
        equality exactly when one single actor has the largest count in
        every contract."""
        rng = random.Random(20261120)
        seen = {"alpha": 0, "chi": 0, "beta_eq": 0, "beta_lt": 0}
        for _ in range(N):
            si = _si(_random_file(rng))
            for value, per, key in ((si.alpha, [c.alpha.value for c in si.contracts], "alpha"),
                                    (si.coverage, [c.coverage.value for c in si.contracts],
                                     "chi")):
                defined = [v for v in per if v is not None]
                assert (value is None) == (not defined)
                if value is not None:
                    assert 0 <= value <= 1 and min(defined) <= value <= max(defined)
                    seen[key] += 1
            if si.beta is None:
                continue
            assert 0 <= si.beta <= 1
            mean = F(sum(c.beta.largest for c in si.contracts), si.powers)
            assert si.beta <= mean <= max(c.beta.value for c in si.contracts
                                          if c.beta.value is not None)
            one = any(all(c.beta.singles.get(a, 0) == c.beta.largest for c in si.contracts)
                      for a in si.singles)
            assert (si.beta == mean) == (one or mean == 0)
            seen["beta_eq" if si.beta == mean else "beta_lt"] += 1
        assert min(seen.values()) > 10, seen

    def test_lemma_1_alpha_sos(self):
        """Adding a sharing entry to a contract never lowers α̂_SoS."""
        rng = random.Random(21)
        raised = 0
        for _ in range(N):
            cs = _random_file(rng)
            before = _si(cs)
            if before.alpha is None:
                continue
            ds = copy.deepcopy(cs)
            c = rng.choice([c for c in ds if c["grounding"]["items"]] or ds)
            if not c["grounding"]["items"]:
                continue
            c["information"]["sharing"].append(
                f"A -> {rng.choice(c['parties'])} : {rng.choice(list(c['grounding']['items']))}")
            after = _si(ds)
            assert after.pairs == before.pairs and after.alpha >= before.alpha
            raised += after.alpha > before.alpha
        assert raised > 5

    def test_lemma_2_beta_sos(self):
        """A power of the single actor with the largest K never lowers
        β̂_SoS; a power whose bearer is not a single actor never raises it."""
        rng = random.Random(22)
        seen = [0, 0]
        for _ in range(N):
            cs = _random_file(rng)
            before = _si(cs)
            if before.beta is None:
                continue
            if before.top:
                top = rng.choice(before.top)
                ds = copy.deepcopy(cs)
                c = rng.choice(ds)
                c["norms"].append({"id": "added", "kind": "power", "bearer": top,
                                   "content": f"{top} -> A : madd"})
                after = _si(ds)
                assert after.beta >= before.beta and after.largest == before.largest + 1
                seen[0] += 1
            ds = copy.deepcopy(cs)
            c = rng.choice(ds)
            sets = [p for p in c["parties"] if "[" in p]
            if sets:
                bearer = rng.choice(sets).split("[")[0] + "[i]"
                c["norms"].append({"id": "added", "kind": "power", "bearer": bearer,
                                   "content": f"{bearer} -> A : madd"})
                after = _si(ds)
                assert after.beta <= before.beta and after.largest == before.largest
                seen[1] += 1
        assert min(seen) > 10, seen

    def test_lemma_3_coverage_sos(self):
        """Giving a norm of N without incentive a sanction raises χ_SoS by
        1/|N_SoS|."""
        rng = random.Random(23)
        seen = 0
        for _ in range(N):
            cs = _random_file(rng)
            before = _si(cs)
            if before.coverage is None:
                continue
            ds = copy.deepcopy(cs)
            sis = before.contracts
            cands = [(k, n) for k, (c, ci) in enumerate(zip(ds, sis)) for n in c["norms"]
                     if n["id"] in ci.coverage.norms and n["id"] not in ci.coverage.incentive]
            if not cands:
                continue
            k, n = rng.choice(cands)
            n.setdefault("on_violation", {})["sanction"] = "fine"
            after = _si(ds)
            assert after.coverage == before.coverage + F(1, before.norms)
            seen += 1
        assert seen > 10

    def test_adding_a_contract_moves_alpha_and_chi_towards_it(self):
        """α̂_SoS and χ_SoS after adding a contract lie between the value
        before and the value of the added contract; so adding a contract can
        lower them (counterexample below)."""
        rng = random.Random(24)
        lowered = 0
        for _ in range(N):
            cs = _random_file(rng, 2)
            new = _random_contract(rng)
            new["id"] = "C9"
            before, after = _si(cs), _si(cs + [new])
            added = after.contracts[-1]
            for b, a, v in ((before.alpha, after.alpha, added.alpha.value),
                            (before.coverage, after.coverage, added.coverage.value)):
                if b is not None and v is not None:
                    assert min(b, v) <= a <= max(b, v)
                    lowered += a < b
        assert lowered > 5

    def test_counterexample_adding_a_contract_lowers_alpha(self):
        one = """    - id: C0
      parties: [D, X]
      information:
        sharing: ["D -> X : route"]
      grounding:
        items: {route: D}
"""
        two = one.replace("C0", "C9").replace('        sharing: ["D -> X : route"]\n',
                                             "        sharing: []\n")
        a = G.contract_indices(sos_of(one), sos_of(one).contracts[0]).alpha.value
        assert a == 1 and G.sos_indices(sos_of(one, two)).alpha == F(1, 2)

    def test_counterexample_lemma_2_per_contract_does_not_carry_over(self):
        """The condition of Lemma 2 within a contract is not enough: a power
        of the actor with the largest count in its contract (Y in C2) lowers
        β̂_SoS when another actor has the largest K over the SoS."""
        c1 = """    - id: C1
      parties: [X, D]
      norms:
        - {id: a1, kind: power, bearer: X, content: "X -> D : a", effects: {discharge: [o]}}
        - {id: a2, kind: power, bearer: X, content: "X -> D : b", effects: {discharge: [o]}}
        - {id: o, kind: obligation, bearer: D, content: "D -> X : c", deadline: 5s}
"""
        c2 = """    - id: C2
      parties: [Y, D]
      norms:
        - {id: b1, kind: power, bearer: Y, content: "Y -> D : a", effects: {discharge: [o]}}
        - {id: o, kind: obligation, bearer: D, content: "D -> Y : c", deadline: 5s}
"""
        more = c2.replace(
            "        - {id: o, kind",
            '        - {id: b2, kind: power, bearer: Y, content: "Y -> D : b", '
            'effects: {discharge: [o]}}\n        - {id: o, kind')
        before, after = G.sos_indices(sos_of(c1, c2)), G.sos_indices(sos_of(c1, more))
        assert [c.beta.value for c in before.contracts] == [1, 1]
        assert [c.beta.value for c in after.contracts] == [1, 1]
        assert (before.beta, after.beta) == (F(2, 3), F(2, 4))

    def test_beta_sos_is_not_a_function_of_the_contracts_betas(self):
        # each contract: one power, β̂ = 1; one bearer for both or two bearers
        c = """    - id: C{n}
      parties: [{a}, D]
      norms:
        - {{id: p, kind: power, bearer: {a}, content: "{a} -> D : a", effects: {{discharge: [o]}}}}
        - {{id: o, kind: obligation, bearer: D, content: "D -> {a} : c", deadline: 5s}}
"""
        same = G.sos_indices(sos_of(c.format(n=1, a="X"), c.format(n=2, a="X")))
        other = G.sos_indices(sos_of(c.format(n=1, a="X"), c.format(n=2, a="Y")))
        assert [ci.beta.value for ci in same.contracts] == [1, 1] == [
            ci.beta.value for ci in other.contracts]
        assert (same.beta, other.beta) == (1, F(1, 2))

    def test_invariant_under_reordering_contracts(self):
        """The whole report of the SoS, and its text, are the same for every
        order of the contracts; tied actors are all named, sorted."""
        rng = random.Random(25)
        ties = 0
        for _ in range(80):
            cs = _random_file(rng)
            a = G.sos_report(_si(cs))
            shuffled = cs[:]
            rng.shuffle(shuffled)
            assert G.sos_report(_si(shuffled)) == a
            ties += len(a["beta"]["largest_bearers"]) > 1
            assert a["beta"]["largest_bearers"] == sorted(a["beta"]["largest_bearers"])
            ra, rb = G.report(_sos(cs)), G.report(_sos(shuffled))
            for r in (ra, rb):
                r["file"] = "f"
                r["contracts"] = []          # per contract: in file order, as before
                r["designs"] = []
            assert G.format_report_text(ra) == G.format_report_text(rb)
        assert ties > 5, ties
