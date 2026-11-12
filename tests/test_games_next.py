"""Strategic models (Appendix I), continued: the end-point certification
of I.9 in any strategic model, the concept `every_reachable_state` of an
equilibrium query (I.7 to I.9), and soundness fuzzing with three and four
players and three open parameters. Every deliberate soundness bug of
`tests/games_fuzz.py` that concerns these methods is detected by a fixed
file in `tests/data/games_next`, not only by random fuzzing."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from cadl.cli import main
from cadl.games_eval import (
    ENDPOINT_METHOD,
    EqCell,
    build_game,
    endpoint_setup,
    endpoint_verdict,
    equilibrium_check,
    equilibrium_map,
    check_json,
    map_json,
    player_at_point,
    prepare_map,
)
from cadl.parser import parse, parse_file
from cadl.stochastic import checked_model

from . import games_fuzz

ROOT = Path(__file__).parent
DATA = ROOT / "data" / "games_next"
ROBOT = ROOT.parent / "examples" / "games_robot_delivery.cadl"
HUMP = DATA / "hump.cadl"
PROB_HUMP = DATA / "prob_hump.cadl"
NONMONOTONE = DATA / "nonmonotone.cadl"
DETOUR = DATA / "detour.cadl"
ENTRY = DATA / "entry.cadl"
BSTATE = DATA / "bstate.cadl"                  # from the independent review
NS_AFFINE_ZERO = DATA / "ns_affine_zero.cadl"  # from the independent review
F = Fraction


def load(path: Path):
    return checked_model(parse_file(path))


def design_of(path: Path, query: str, mutant: str | None = None, **options) -> dict:
    with games_fuzz.mutation(mutant):
        return equilibrium_map(load(path), query, **options)["designs"][0]


def claims(cells: list) -> list:
    return [(c.box, c.status, c.deviators, c.from_every_state, c.reason) for c in cells]


def cli(capsys, *args) -> tuple:
    status = main([str(a) for a in args])
    out = capsys.readouterr()
    return status, out.out, out.err


# ---------------------------------------------------------------------------
# The end-point certification of I.9 in a file without grounding
# ---------------------------------------------------------------------------

# The gain of hump.cadl is max(0, -1/2 + 4λ - 4λ²) (in prob_hump.cadl through
# the probabilities); it is positive exactly between (1 ± 1/√2)/2.
ROOT_LO = (F(1464, 10000), F(1465, 10000))
ROOT_HI = (F(8535, 10000), F(8536, 10000))


def check_hump(d: dict) -> None:
    """The map of the hump: no equilibrium cell reaches into the hump and
    no not_equilibrium cell out of it."""
    assert [c.status for c in d["cells"]] == [
        "equilibrium", "undecided", "not_equilibrium", "undecided", "equilibrium"]
    for c in d["cells"]:
        a, b = c.box["lam"]
        if c.status == "equilibrium":
            assert b <= ROOT_LO[0] or a >= ROOT_HI[1], (a, b)
        if c.status == "not_equilibrium":
            assert a >= ROOT_LO[0] and b <= ROOT_HI[1], (a, b)


def check_nonmonotone(d: dict) -> None:
    """gain(λ) = |1 − 2λ| with ε = 1/4: an ε-equilibrium exactly on
    [3/8, 5/8]. The gain is convex, so a claim holds on a cell when it
    holds at its ends and, for not_equilibrium, the cell does not meet
    [3/8, 5/8]."""
    eps = F(1, 4)
    for c in d["cells"]:
        a, b = c.box["lam"]
        if c.status == "equilibrium":
            assert abs(1 - 2 * a) <= eps and abs(1 - 2 * b) <= eps, (a, b)
        if c.status == "not_equilibrium":
            assert b < F(3, 8) or a > F(5, 8), (a, b)


class TestEndPoints:
    def test_claims_of_the_bounds_are_kept(self):
        """The end points decide only what the bounds leave undecided:
        every cell that the bounds decide keeps its claim (the first
        example of I.12, without grounding)."""
        model = load(ROBOT)
        with_ends = equilibrium_map(model, "comply_by_lambda")
        without = equilibrium_map(model, "comply_by_lambda", use_endpoints=False)
        for d1, d2 in zip(with_ends["designs"], without["designs"]):
            assert d1["endpoints"]["applicable"] and d2["endpoints"] is None
            assert claims(d1["cells"]) == claims(d2["cells"])

    def test_the_hump_is_refused(self):
        d = design_of(HUMP, "mp")
        assert not d["endpoints"]["applicable"]
        assert "power above one, counted on the text" in d["endpoints"]["reason"]
        check_hump(d)
        d = design_of(PROB_HUMP, "mp")
        assert "occurs in the probability 'lam'" in d["endpoints"]["reason"]
        check_hump(d)
        assert all(c.method is None for c in d["cells"])

    @pytest.mark.parametrize("path, mutant", [
        (HUMP, "endpoints_drop_affine"), (PROB_HUMP, "endpoints_drop_probability")])
    def test_without_condition_a_the_hump_is_lost(self, path, mutant):
        d = design_of(path, "mp", mutant)
        assert d["endpoints"]["applicable"]
        with pytest.raises(AssertionError):
            check_hump(d)
        assert any(c.method == ENDPOINT_METHOD and c.status == "equilibrium"
                   and c.box["lam"][0] < ROOT_HI[0] and c.box["lam"][1] > ROOT_LO[1]
                   for c in d["cells"])

    def test_a_gain_that_is_not_monotone(self):
        d = design_of(NONMONOTONE, "mp")
        ends = d["endpoints"]
        assert ends["applicable"] and ends["decreasing"] == [] and ends["increasing"] == []
        assert d["resolution_reached"]
        check_nonmonotone(d)

    def test_without_condition_b_a_deviator_is_claimed_where_none_is(self):
        d = design_of(NONMONOTONE, "mp", "endpoints_drop_condition_b")
        with pytest.raises(AssertionError):
            check_nonmonotone(d)
        assert d["cells"][0].status == "not_equilibrium" and d["cells"][0].box["lam"] == (0, 1)

    @staticmethod
    def one_term(weight: str):
        text = NONMONOTONE.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        data["sos"]["model"]["payoffs"][0]["terms"] = [{"reward": "rd", "weight": weight}]
        data["sos"]["model"]["equilibria"][0]["epsilon"] = 0
        model = checked_model(parse(yaml.safe_dump(data, sort_keys=False)))
        query = model.equilibria[0]
        game = prepare_map(model, query, model.designs[0], None)
        return game, query, endpoint_setup(game, query, model.designs[0])

    @pytest.mark.parametrize("weight, decreasing, increasing", [
        ("-2 * lam", ["A"], []),
        ("1 - 2 * lam", ["A"], []),            # a constant term does not matter
        ("2 * lam - 1", [], ["A"]),            # B+
        ("0 * lam + 3", ["A"], ["A"]),         # ℓ occurs, with the coefficient 0
        ("3", ["A"], ["A"]),
    ])
    def test_condition_b(self, weight, decreasing, increasing):
        _, _, ends = self.one_term(weight)
        assert ends["applicable"]
        assert (ends["decreasing"], ends["increasing"]) == (decreasing, increasing)

    def test_condition_b_needs_the_expected_value_zero(self):
        """`down` under the profile: the reward of the term with λ is 1."""
        text = NONMONOTONE.read_text(encoding="utf-8").replace(
            "s: [ { choose: good } ]", "s: [ { choose: down } ]")
        model = checked_model(parse(text))
        query = model.equilibria[0]
        game = prepare_map(model, query, model.designs[0], None)
        ends = endpoint_setup(game, query, model.designs[0])
        assert ends["decreasing"] == [] and ends["increasing"] == []

    def test_a_non_decreasing_gain(self):
        """gain(λ) = max(0, 2λ − 1): under B+ a gain above ε at the lower
        end makes a deviator on the cell, with the bounds [g(a), g(b)]."""
        game, query, _ = self.one_term("2 * lam - 1")
        cell = EqCell(box={"lam": (F(3, 5), F(9, 10))})
        assert endpoint_verdict(game, query, cell)
        assert (cell.status, cell.deviators, cell.method) == (
            "not_equilibrium", ["A"], ENDPOINT_METHOD)
        assert cell.gain_bounds["A"] == (F(1, 5), F(4, 5))
        assert cell.deviations["A"] == [{"state": {"loc": 0}, "action": "down"}]
        # Across λ = 1/2: no claim (the gain is 0 at the lower end).
        assert not endpoint_verdict(game, query, EqCell(box={"lam": (F(1, 4), F(3, 4))}))
        cell = EqCell(box={"lam": (F(0), F(1, 2))})
        assert endpoint_verdict(game, query, cell) and cell.status == "equilibrium"

    def test_text_and_json(self, capsys):
        status, out, _ = cli(capsys, "game-check", NONMONOTONE, "--certify")
        assert status == 0
        assert ("end points (I.9): condition A holds for 'lam'; the gain is non-increasing "
                "for: no player; non-decreasing for: no player") in out
        status, out, _ = cli(capsys, "game-check", HUMP, "--certify", "--format", "json")
        ends = json.loads(out)["maps"][0]["designs"][0]["endpoints"]
        assert not ends["applicable"] and "power above one" in ends["reason"]


# ---------------------------------------------------------------------------
# The concept every_reachable_state
# ---------------------------------------------------------------------------

class TestEveryReachableState:
    def test_checks(self):
        model = load(DETOUR)
        nash = equilibrium_check(model, "nash_check")["designs"][0]
        assert (nash["status"], nash["from_every_state"]) == ("equilibrium", False)
        every = equilibrium_check(model, "every_check")
        d = every["designs"][0]
        assert every["concept"] == "every_reachable_state"
        assert (d["status"], d["deviators"]) == ("not_equilibrium", ["A"])
        a = d["players"][0]
        assert (a["gain"], a["state_gain"], a["state"]) == (0, 5, {"loc": 1})
        assert a["best_response"] == [{"state": {"loc": 1}, "action": "ok"}]
        data = check_json(every)
        assert data["concept"] == "every_reachable_state"
        assert data["designs"][0]["players"][0]["state_gain"] == "5"
        assert "concept" not in check_json(equilibrium_check(model, "nash_check"))

    @pytest.mark.parametrize("epsilon, status", [(F(4), "not_equilibrium"), (F(5), "equilibrium")])
    def test_epsilon(self, epsilon, status):
        model = load(DETOUR)
        model.equilibria[1].epsilon = epsilon
        d = equilibrium_check(model, "every_check")["designs"][0]
        assert d["status"] == status and d["from_every_state"] is None

    @staticmethod
    def check_every_map(d: dict) -> None:
        """At p = 0 the state loc = 1 is not reachable and the profile is an
        equilibrium from every reachable state; at p > 0 A gains 5 there."""
        assert d["resolution_reached"]
        for c in d["cells"]:
            lo, hi = c.box["p"]
            if c.status == "equilibrium":
                assert hi == 0
            if c.status == "not_equilibrium":
                assert lo > 0 and c.deviators == ["A"]
                state, gain = c.deviation_states["A"]
                assert state == {"loc": 1} and gain == 5

    def test_maps(self):
        nash = design_of(DETOUR, "nash_map")
        assert [(c.status, c.from_every_state) for c in nash["cells"]] == [("equilibrium", None)]
        every = design_of(DETOUR, "every_map")
        self.check_every_map(every)
        assert [c.status for c in every["cells"]] == ["undecided", "not_equilibrium"]
        assert every["cells"][0].box["p"] == (0, F(1, 32))
        cell = map_json({"id": "m", "profile": "s", "epsilon": 0, "concept":
                         "every_reachable_state", "over": {"p": (0, 1)}, "resolution": F(1, 20),
                         "max_depth": 0, "max_cells": 1, "designs": [every]})
        out = cell["designs"][0]["cells"][1]
        assert out["deviation_states"] == {"A": {"state": {"loc": 1}, "gain_lower": "5"}}
        assert out["deviations"] == {"A": [{"state": {"loc": 1}, "action": "ok"}]}

    def test_without_the_reachability_certificate(self):
        """A state that is reachable at the centre of the cell, but not at
        p = 0, is not a witness for the whole cell."""
        d = design_of(DETOUR, "every_map", "drop_reach_certificate")
        with pytest.raises(AssertionError):
            self.check_every_map(d)
        assert d["cells"][0].status == "not_equilibrium" and d["cells"][0].box["p"] == (0, 1)

    def test_with_the_conditions_of_nash(self):
        d = design_of(DETOUR, "every_map", "every_state_as_nash")
        with pytest.raises(AssertionError):
            self.check_every_map(d)
        assert d["cells"][0].status == "equilibrium" and d["cells"][0].box["p"] == (0, 1)

    def test_a_threat_that_is_not_credible(self):
        """Entry deterrence: (out; fight) is an equilibrium from every
        reachable state, although B would not fight in loc = 1. That state
        is in Reach_A, where B does not choose, and not in Reach_B."""
        model = load(ENTRY)
        d = equilibrium_check(model, "deter_every")["designs"][0]
        assert (d["status"], d["from_every_state"]) == ("equilibrium", True)
        game = build_game(model, model.designs[0], [], {})
        loc1 = game.states.index((1,))
        assert loc1 in player_at_point(game, "deter", "A", ()).state_gains
        assert loc1 not in player_at_point(game, "deter", "B", ()).state_gains
        # Started in loc = 1, B gains 3 by sharing: not subgame perfect.
        start = {"loc": 1}
        text = ENTRY.read_text(encoding="utf-8").replace("init: 0", "init: 1")
        moved = checked_model(parse(text))
        b = equilibrium_check(moved, "deter_every")["designs"][0]["players"][1]
        assert b["state"] == start and b["state_gain"] == 3

    def test_text(self, capsys):
        status, out, _ = cli(capsys, "game-check", DETOUR, "--certify")
        assert status == 0
        assert "epsilon = 0, concept every_reachable_state;" in out
        assert "largest gain from a state of Reach_p = 5  (exact: 5), from (loc=1)" in out
        assert "canonical best response from that state: ok in (loc=1)" in out
        assert "p in [0.03125, 1]: not_equilibrium, deviators: A (from (loc=1))" in out


# ---------------------------------------------------------------------------
# Files of the independent review of this version
# ---------------------------------------------------------------------------

def check_bstate(d: dict) -> None:
    """From loc = 1, A gains max(0, λ − 1/2) (y instead of x); the term of
    x has the expected value 0 from s₀ under the profile but 1 from
    loc = 1, so condition B holds from s₀ only. An equilibrium from every
    reachable state exactly for λ ≤ 1/2."""
    assert d["resolution_reached"]
    for c in d["cells"]:
        a, b = c.box["lam"]
        if c.status == "equilibrium":
            assert b <= F(1, 2), (a, b)
        if c.status == "not_equilibrium":
            assert a > F(1, 2) and c.deviation_states["A"][0] == {"loc": 1}, (a, b)


def check_ns_affine_zero(d: dict) -> None:
    """At p = 0 the state loc = 1 is not reachable and the profile is an
    equilibrium from every reachable state; the cell that contains p = 0
    stays undecided."""
    assert d["resolution_reached"]
    for c in d["cells"]:
        if c.box["p"][0] == 0:
            assert c.status == "undecided", c.box
        elif c.status == "not_equilibrium":
            assert c.deviators == ["A"]


class TestReviewFiles:
    def test_condition_b_is_checked_per_state(self):
        d = design_of(BSTATE, "mp")
        check_bstate(d)
        assert [c.status for c in d["cells"]] == ["equilibrium", "undecided", "not_equilibrium"]
        assert F(49, 100) < d["cells"][0].box["lam"][1] <= F(1, 2)
        assert F(1, 2) < d["cells"][2].box["lam"][0] < F(51, 100)
        nash = design_of(BSTATE, "mpn")
        assert [(c.box["lam"], c.status) for c in nash["cells"]] == [((F(1, 4), 1), "equilibrium")]

    def test_condition_b_from_the_initial_state_only_is_unsound(self):
        d = design_of(BSTATE, "mp", "endpoints_b_from_initial")
        with pytest.raises(AssertionError):
            check_bstate(d)

    @pytest.mark.parametrize("query", ["mp", "mp2"])
    def test_the_cell_at_p_zero_stays_undecided(self, query):
        d = design_of(NS_AFFINE_ZERO, query)
        check_ns_affine_zero(d)
        assert d["cells"][0].status == "undecided"
        assert all(c.status == "not_equilibrium" for c in d["cells"][1:])

    @pytest.mark.parametrize("query", ["mp", "mp2"])
    def test_without_the_reachability_certificate(self, query):
        d = design_of(NS_AFFINE_ZERO, query, "drop_reach_certificate")
        with pytest.raises(AssertionError):
            check_ns_affine_zero(d)


# ---------------------------------------------------------------------------
# Soundness fuzzing: three and four players, three open parameters, the
# concept every_reachable_state, and the end points without grounding
# ---------------------------------------------------------------------------

def assert_sound(stats: dict) -> None:
    assert "failure" not in stats, stats["failure"] + "\n" + stats.get("failing_model", "")


class TestFuzz:
    def test_three_and_four_players_three_parameters(self):
        stats = games_fuzz.run(23, 12, mode="scale", concept="both")
        assert_sound(stats)
        assert stats["players_3"] > 0 and stats["players_4"] > 0 and stats["open_3"] > 0
        assert stats["cells_equilibrium"] > 0 and stats["cells_not_equilibrium"] > 0

    def test_every_reachable_state(self):
        stats = games_fuzz.run(21, 25, concept="every")
        assert_sound(stats)
        assert stats["cells_deviation_from_a_later_state"] > 0
        assert stats["cells_from_every_state"] > 0

    def test_end_points_without_grounding(self):
        stats = games_fuzz.run(22, 25, mode="endpoints", concept="both")
        assert_sound(stats)
        assert stats["endpoints_applicable"] > 0 and stats["endpoints_not_applicable"] > 0
        assert stats["direct_endpoints_equilibrium"] > 0
        assert stats["direct_endpoints_not_equilibrium"] > 0

    @pytest.mark.parametrize("name, seed, count, options", [
        ("drop_reach_certificate", 3, 10, {"concept": "every"}),
        ("every_state_as_nash", 3, 5, {"concept": "every"}),
        ("endpoints_drop_condition_b", 2, 5, {"mode": "endpoints", "concept": "both"}),
        ("endpoints_drop_affine", 5, 20, {"mode": "endpoints"}),
        ("endpoints_drop_probability", 1, 30, {"mode": "endpoints"}),
    ])
    def test_a_deliberate_soundness_bug_is_detected(self, name, seed, count, options):
        stats = games_fuzz.run(seed, count, name, **options)
        assert "failure" in stats, f"the mutant {name} survived {stats}"

    @pytest.mark.parametrize("name", games_fuzz.ADVANTAGE_MUTATIONS)
    @pytest.mark.parametrize("concept", ["nash", "every"])
    def test_a_soundness_bug_of_pd_is_detected(self, name, concept):
        # PD+ and PD− of I.9: the advantages without the successors, the
        # lower and upper bounds exchanged, or the differences taken at the
        # centre of the cell only.
        stats = games_fuzz.run(1, 40, name, concept=concept)
        assert "failure" in stats, f"the mutant {name} survived {stats}"

    def test_pd_decides_more_cells(self):
        # On the same games, the maps with PD+ and PD− leave at most the
        # undecided volume of the maps without them, at the same budget.
        import random
        from cadl.games_eval import equilibrium_map
        from cadl.parser import parse
        from cadl.stochastic import analyse
        import cadl.games_eval as ge
        rng = random.Random(5)
        better = 0
        for _ in range(12):
            g = games_fuzz.generate(rng, mode="scale")
            analysis = analyse(parse(games_fuzz.to_cadl(g)))
            if not analysis.ok:
                continue
            with_pd = equilibrium_map(analysis.model, "on_region", max_cells=24)
            saved = ge.USE_ADVANTAGES
            ge.USE_ADVANTAGES = False
            try:
                without = equilibrium_map(analysis.model, "on_region", max_cells=24)
            finally:
                ge.USE_ADVANTAGES = saved
            a = with_pd["designs"][0]["undecided_fraction"]
            b = without["designs"][0]["undecided_fraction"]
            better += a < b
        assert better > 0
