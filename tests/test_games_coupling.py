"""Strategic models (Appendix I): the coupled bounds of the advantages of
I.9 (`CoupledPlays`), which PD+ and PD− use where the bounds from l_p and
u_p fail. The second example of I.12 with three open parameters (λ, κ,
p_fail) is the case they were made for: the advantage of `trust` for the
dispatcher is the difference of the values of two successors, watched
and not watched. Every deliberate soundness bug of `tests/games_fuzz.py`
that concerns the coupled bounds is detected by fuzzing and on this
example."""

from __future__ import annotations

import itertools
import random
from fractions import Fraction
from pathlib import Path

import pytest

import cadl.games_eval as ge
from cadl.games_eval import (
    CornerTable,
    CoupledPlays,
    EqCell,
    _coupling,
    advantage_bounds,
    bound_verdict,
    build_process,
    cell_distributions,
    equilibrium_map,
    improper_states,
    optimal_values,
    player_at_point,
    player_on_cell,
    point_distributions,
    point_reward,
    prepare_map,
    reward_bounds,
)
from cadl.parser import parse
from cadl.stochastic import analyse, checked_model

from . import games_fuzz

ROOT = Path(__file__).parent
TWO = ROOT.parent / "examples" / "games_two_players.cadl"
F = Fraction

THREE = """      - id: three
        profile: comply_if_watched
        regimes: [CENTRALIZED]
        over: { lambda: [0, 0.4], kappa: [0, 0.4], p_fail: [0, 0.5] }
        resolution: 0.1
"""


def two_with_three_parameters():
    text = TWO.read_text(encoding="utf-8")
    assert text.endswith("        resolution: 0.25\n")
    return checked_model(parse(text + THREE))


MODEL = two_with_three_parameters()
QUERY = next(q for q in MODEL.equilibria if q.id == "three")
GAME = prepare_map(MODEL, QUERY, MODEL.design("CENTRALIZED"), None)
# λ, κ, p_fail
CELL_D_OPEN = ((F(0), F(1, 10)), (F(1, 10), F(1, 5)), (F(0), F(1, 8)))
CELL_D_DECIDED = ((F(1, 10), F(1, 5)), (F(0), F(1, 10)), (F(1, 4), F(3, 8)))


def exact_advantages(game, player: str, point: tuple) -> dict:
    """A_p(s, c) at a valuation: {(state index of the game, command): value}."""
    proc = build_process(game, player, "comply_if_watched", point_distributions(game, point))
    reward = point_reward(game, proc, point)
    v = optimal_values(proc, lambda i: [proc.selected[i]], reward)
    out = {}
    for i, g in enumerate(proc.states):
        if proc.target[i] or not proc.mine[i]:
            continue
        q = {k: reward(i, k) + sum((p * v[j] for j, p in ds[0].items()), F(0))
             for k, ds in proc.options[i]}
        for k in q:
            if k != proc.selected[i]:
                out[(g, k)] = q[k] - q[proc.selected[i]]
    return out


def bounds_on(cell: tuple, player: str) -> tuple:
    """The bounds of the advantages from l_p and u_p and the coupled ones."""
    dists = cell_distributions(GAME, cell)
    proc = build_process(GAME, player, "comply_if_watched", dists)
    lo, hi = reward_bounds(GAME, proc, cell)
    lower = optimal_values(proc, lambda i: [proc.selected[i]], lo, maximize=False)
    upper = optimal_values(proc, lambda i: [proc.selected[i]], hi, maximize=True)
    separate = advantage_bounds(GAME, proc, cell, lower, upper)
    plays = ge.CoupledPlays(GAME, proc, CornerTable(GAME, cell))
    coupled = {key: (plays.bound(*key, maximize=False), plays.bound(*key, maximize=True))
               for key in separate}
    return proc, separate, coupled


def points_of(cell: tuple, rng: random.Random, extra: int = 6) -> list:
    out = list(itertools.product(*[(a, (a + b) / 2, b) for a, b in cell]))
    out += [tuple(a + (b - a) * F(rng.randint(0, 64), 64) for a, b in cell)
            for _ in range(extra)]
    return out


class TestCoupling:
    def test_the_marginals_of_the_joint_distribution(self):
        rng = random.Random(3)
        for _ in range(200):
            def dist():
                states = rng.sample(range(6), rng.randint(1, 4))
                w = [F(rng.randint(1, 9)) for _ in states]
                return {s: x / sum(w) for s, x in zip(states, w)}
            d1, d2 = dist(), dist()
            joint = _coupling(d1, d2)
            assert all(x > 0 for x in joint.values())
            for side, d in ((0, d1), (1, d2)):
                got: dict = {}
                for pair, x in joint.items():
                    got[pair[side]] = got.get(pair[side], F(0)) + x
                assert got == d
            for j in set(d1) & set(d2):
                assert joint.get((j, j), 0) == min(d1[j], d2[j])

    def test_commands_with_the_same_probabilities_move_together(self):
        # Two states with the same row: update by update, no mixed pairs.
        joint = _coupling({1: F(1, 3), 2: F(2, 3)}, {5: F(1, 3), 6: F(2, 3)})
        assert joint == {(1, 5): F(1, 3), (2, 6): F(2, 3)}

    @pytest.mark.parametrize("cell", [CELL_D_OPEN, CELL_D_DECIDED])
    def test_the_bounds_of_the_dispatcher(self, cell):
        proc, separate, coupled = bounds_on(cell, "D")
        (key,) = separate
        a, b = separate[key]
        c, d = coupled[key]
        assert a < c and d < b            # narrower on both sides
        rng = random.Random(1)
        values = [exact_advantages(GAME, "D", p)[(proc.states[key[0]], key[1])]
                  for p in points_of(cell, rng)]
        assert c <= min(values) and max(values) <= d

    def test_the_bounds_hold_for_both_players(self):
        rng = random.Random(2)
        region = [tuple(QUERY.over[n]) for n in GAME.names]
        for _ in range(6):
            cell = []
            for lo, hi in region:
                k = rng.choice([1, 2, 4, 8])
                i = rng.randrange(k)
                cell.append((lo + (hi - lo) * F(i, k), lo + (hi - lo) * F(i + 1, k)))
            cell = tuple(cell)
            for player in ("R", "D"):
                proc, separate, coupled = bounds_on(cell, player)
                for point in points_of(cell, rng, 2):
                    exact = exact_advantages(GAME, player, point)
                    for (i, k), (lo, hi) in coupled.items():
                        assert lo <= exact[(proc.states[i], k)] <= hi

    def test_a_cell_decided_only_with_the_coupled_bounds(self):
        dists = cell_distributions(GAME, CELL_D_DECIDED)
        on = player_on_cell(GAME, "comply_if_watched", "D", CELL_D_DECIDED, dists, F(0))
        assert on.coupled and on.e2 and on.gain_upper == 0
        off = player_on_cell(GAME, "comply_if_watched", "D", CELL_D_DECIDED, dists, F(0),
                             use_coupling=False)
        assert not (off.e1 or off.e2 or off.n)
        # D gains nothing anywhere in the cell.
        for point in points_of(CELL_D_DECIDED, random.Random(4)):
            assert player_at_point(GAME, "comply_if_watched", "D", point).gain == 0

    def test_the_map_with_three_parameters(self):
        with_coupling = ge.design_map(GAME, QUERY, max_cells=300)
        saved = ge.USE_COUPLING
        ge.USE_COUPLING = False
        try:
            without = ge.design_map(GAME, QUERY, max_cells=300)
        finally:
            ge.USE_COUPLING = saved
        assert with_coupling["undecided_fraction"] < without["undecided_fraction"]
        rng = random.Random(5)
        for cell in with_coupling["cells"]:
            if cell.status == "undecided":
                continue
            box = tuple(cell.box[n] for n in GAME.names)
            for point in points_of(box, rng, 1):
                gains = {p: player_at_point(GAME, "comply_if_watched", p, point).gain
                         for p in ("R", "D")}
                if cell.status == "equilibrium":
                    assert all(g == 0 for g in gains.values())
                else:
                    assert all(gains[p] > 0 for p in cell.deviators)


def random_cells(rng: random.Random, region: dict, count: int) -> list:
    out = []
    for _ in range(count):
        box = {}
        for n, (lo, hi) in region.items():
            k = rng.choice([1, 2, 4])
            i = rng.randrange(k)
            box[n] = (lo + (hi - lo) * F(i, k), lo + (hi - lo) * F(i + 1, k))
        out.append(box)
    return out


def fuzz_games(seed: int, count: int, **options) -> list:
    rng = random.Random(seed)
    out = []
    while len(out) < count:
        g = games_fuzz.generate(rng, **options)
        analysis = analyse(parse(games_fuzz.to_cadl(g)))
        if analysis.ok:
            out.append((g, analysis.model))
    return out


class TestCoupledProcess:
    def test_backward_induction_and_policy_iteration_agree(self):
        # The fuzzed games have cycles; on the pairs of an acyclic part
        # both ways must give the same value.
        compared = 0
        for g, model in fuzz_games(7, 25):
            query = next(q for q in model.equilibria if q.id == "on_region")
            game = prepare_map(model, query, model.designs[0], None)
            if not game.multi_affine:
                continue
            cell = tuple(g["region"][n] for n in game.names)
            try:
                dists = cell_distributions(game, cell)
            except ge._Unknown:
                continue
            for p in g["players"]:
                proc = build_process(game, p, "sigma", dists)
                if improper_states(proc):
                    continue
                plays = CoupledPlays(game, proc, CornerTable(game, cell))
                for i in range(len(proc.states)):
                    if proc.target[i] or not proc.mine[i]:
                        continue
                    for k, _ in proc.options[i]:
                        if k is None or k == proc.selected[i]:
                            continue
                        for maximize in (False, True):
                            fresh = CoupledPlays(game, proc, CornerTable(game, cell))
                            assert plays.bound(i, k, maximize) == \
                                fresh._bound_cyclic(("start", i, k), maximize)
                            compared += 1
        assert compared > 20

    def test_the_coupled_bounds_only_add_decisions(self):
        # A cell that N, E1, E2, PD+ and PD− decide keeps its status and
        # its deviators (the coupled bounds may establish more of them);
        # the coupled bounds decide some cells that they leave open.
        rng = random.Random(9)
        more = 0
        for g, model in fuzz_games(11, 30, mode="scale"):
            query = next(q for q in model.equilibria if q.id == "on_region")
            game = prepare_map(model, query, model.designs[0], None)
            for box in random_cells(rng, g["region"], 4):
                with_c = EqCell(box=box)
                bound_verdict(game, query, with_c)
                saved = ge.USE_COUPLING
                ge.USE_COUPLING = False
                try:
                    without = EqCell(box=box)
                    bound_verdict(game, query, without)
                finally:
                    ge.USE_COUPLING = saved
                if without.status != "undecided":
                    assert with_c.status == without.status
                    assert set(without.deviators) <= set(with_c.deviators)
                    more += len(with_c.deviators) > len(without.deviators)
                elif with_c.status != "undecided":
                    more += 1
        assert more > 0


def assert_sound(stats: dict) -> None:
    assert "failure" not in stats, stats["failure"] + "\n" + stats.get("failing_model", "")


class TestFuzz:
    def test_maps_and_bounds(self):
        stats = games_fuzz.run(31, 30)
        assert_sound(stats)
        assert stats["players_decided_by_coupling"] > 0
        assert stats["advantages_compared"] > 0

    def test_three_and_four_players_both_concepts(self):
        stats = games_fuzz.run(32, 10, mode="scale", concept="both")
        assert_sound(stats)
        assert stats["advantages_compared"] > 0

    @pytest.mark.parametrize("name", games_fuzz.COUPLING_MUTATIONS)
    @pytest.mark.parametrize("concept, seed", [("nash", 1), ("every", 4)])
    def test_a_soundness_bug_is_detected_by_the_maps(self, name, concept, seed):
        # Without the direct check of the bounds: a wrong claim of a map.
        stats = games_fuzz.run(seed, 80, name, concept=concept, direct=False)
        assert "failure" in stats, f"the mutant {name} survived {stats}"

    @pytest.mark.parametrize("name", games_fuzz.COUPLING_MUTATIONS)
    def test_a_soundness_bug_is_detected_by_the_direct_check(self, name):
        stats = games_fuzz.run(2, 20, name)
        assert "failure" in stats, f"the mutant {name} survived {stats}"

    @pytest.mark.parametrize("name", games_fuzz.COUPLING_MUTATIONS)
    def test_a_soundness_bug_is_detected_on_the_example(self, name):
        # On the example: a bound that misses an exact advantage at a point.
        with games_fuzz.mutation(name):
            wrong = False
            rng = random.Random(6)
            for cell in (CELL_D_OPEN, CELL_D_DECIDED):
                for player in ("R", "D"):
                    proc, _, coupled = bounds_on(cell, player)
                    for point in points_of(cell, rng, 2):
                        exact = exact_advantages(GAME, player, point)
                        wrong |= any(not lo <= exact[(proc.states[i], k)] <= hi
                                     for (i, k), (lo, hi) in coupled.items())
        assert wrong
