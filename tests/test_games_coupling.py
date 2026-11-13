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


def multilinear_weights(box: tuple, point: tuple) -> list:
    """w_e(point) for the corners e of a box, in the order of
    `itertools.product` (a point coordinate of a degenerate interval
    gets the single corner)."""
    weights = [F(1)]
    for (lo, hi), x in zip(box, point):
        if lo == hi:
            continue
        t = (x - lo) / (hi - lo)
        weights = [w * f for w in weights for f in (1 - t, t)]
    return weights


SQUARED = TWO.read_text(encoding="utf-8").replace("(1 - p_fail)", "(1 - p_fail) * (1 - p_fail)")


def squared_game():
    model = checked_model(parse(SQUARED + THREE))
    query = next(q for q in model.equilibria if q.id == "three")
    return model, query, prepare_map(model, query, model.design("CENTRALIZED"), None)


class TestLifting:
    """Probabilities that are not multi-affine: the lifted cell of I.9."""

    def test_the_lifted_rows(self):
        # L(x, R(x)) = P(x), the lifted polynomials are multi-affine, and
        # the polynomials of a row sum to 1 identically.
        rng = random.Random(21)
        checked = 0
        for g, model in fuzz_games(13, 60, mode="nonaffine"):
            query = next(q for q in model.equilibria if q.id == "on_region")
            game = prepare_map(model, query, model.designs[0], None)
            lifting = ge.lift_game(game)
            if game.multi_affine or lifting is None:
                continue
            n = len(game.names)
            m = lifting.dimension(n)
            rows = {id(c.row): c.row for cmds in game.commands for c in cmds}
            for key, row in rows.items():
                lifted = lifting.rows[key]
                assert all(ge._multi_affine(p) for p in lifted)
                total: dict = {}
                for p in lifted:
                    total = ge._p_add(total, dict(p))
                assert ge._canonical(total) == (((0,) * m, F(1)),)
                for _ in range(3):
                    x = tuple(F(rng.randint(0, 40), 40) for _ in range(n))
                    u = x + tuple(ge._p_eval(r, x) for r in lifting.functions)
                    assert [ge._p_eval(p, u) for p in lifted] == \
                        [ge._p_eval(p, x) for p in row.polys]
                checked += 1
        assert checked > 50

    def test_every_distribution_is_a_mixture_of_the_corners(self):
        # P(·, v) = Σ_e w_e(v, R(v)) · D_e with the same weights for every
        # command, and every D_e is a distribution of the relaxed process.
        rng = random.Random(22)
        checked = 0
        for g, model in fuzz_games(14, 40, mode="nonaffine"):
            query = next(q for q in model.equilibria if q.id == "on_region")
            game = prepare_map(model, query, model.designs[0], None)
            if game.multi_affine or not ge.coupling_applies(game):
                continue
            for box in random_cells(rng, g["region"], 2):
                cell = tuple(box[n] for n in game.names)
                try:
                    cell_distributions(game, cell)
                except ge._Unknown:
                    continue
                table = CornerTable(game, cell)
                for _ in range(2):
                    x = tuple(a + (b - a) * F(rng.randint(0, 16), 16) for a, b in cell)
                    u = x + tuple(ge._p_eval(r, x) for r in table.lifting.functions)
                    w = multilinear_weights(table.box, u)
                    assert len(w) == len(table.corners) and sum(w) == 1
                    assert all(y >= 0 for y in w)
                    for gi, cmds in enumerate(game.commands):
                        for k, c in enumerate(cmds):
                            try:
                                corner = [table.distribution(gi, k, e)
                                          for e in range(len(table.corners))]
                            except ge.CouplingUnavailable:
                                continue
                            mixed: dict = {}
                            for we, d in zip(w, corner):
                                for j, p in d.items():
                                    mixed[j] = mixed.get(j, F(0)) + we * p
                            exact: dict = {}
                            for j, p in zip(c.targets, c.row.polys):
                                exact[j] = exact.get(j, F(0)) + ge._p_eval(p, x)
                            assert {j: p for j, p in mixed.items() if p} == \
                                {j: p for j, p in exact.items() if p}
                            checked += 1
        assert checked > 100

    def test_the_bernstein_enclosure(self):
        # It contains the values on the box, and it is within the
        # enclosure by interval arithmetic for (1 − p)² on small boxes.
        rng = random.Random(25)
        for _ in range(400):
            n = rng.randint(1, 3)
            poly: dict = {}
            for _ in range(rng.randint(1, 5)):
                e = tuple(rng.randint(0, 3) for _ in range(n))
                poly[e] = poly.get(e, F(0)) + F(rng.randint(-5, 5), rng.randint(1, 4))
            poly = tuple(sorted((e, c) for e, c in poly.items() if c))
            if not poly:
                continue
            cell = []
            for _ in range(n):
                a = F(rng.randint(-8, 8), 8)
                cell.append((a, a + F(rng.randint(1, 8), 8)))
            cell = tuple(cell)
            lo, hi = ge.bernstein_interval(poly, cell)
            a, b = ge.lifted_interval(poly, cell)
            assert max(lo, ge._p_interval(poly, cell)[0]) == a
            for x in itertools.product(*[(a_, (a_ + b_) / 2, b_) for a_, b_ in cell]):
                assert lo <= ge._p_eval(poly, x) <= hi
        square = (((0,), F(1)), ((1,), F(-2)), ((2,), F(1)))        # (1 − p)²
        cell = ((F(1, 4), F(5, 16)),)
        lo, hi = ge.bernstein_interval(square, cell)
        a, b = ge._p_interval(square, cell)
        assert a < lo and hi < b
        assert (lo, hi) == (F(11, 16) ** 2, F(3, 4) ** 2)

    def test_a_corner_outside_the_relaxed_process_is_not_used(self, monkeypatch):
        model, query, game = squared_game()
        cell = ((F(0), F(1, 10)), (F(1, 10), F(1, 5)), (F(0), F(1, 8)))
        dists = cell_distributions(game, cell)
        proc = build_process(game, "D", query.profile, dists)
        table = CornerTable(game, cell)
        # Intervals of the relaxed process that exclude the corners.
        monkeypatch.setattr(CornerTable, "_relaxed",
                            lambda self, row: [(F(0), F(0))] * len(row.polys))
        plays = CoupledPlays(game, proc, table)
        keys = [(i, k) for i in range(len(proc.states))
                if proc.mine[i] and not proc.target[i]
                for k, _ in proc.options[i] if k is not None and k != proc.selected[i]]
        assert keys
        with pytest.raises(ge.CouplingUnavailable):
            plays.bound(*keys[0], maximize=True)
        assert ge._coupled_bound(plays, keys[0], True) is None

    def test_too_many_coordinates(self, monkeypatch):
        _, _, game = squared_game()
        monkeypatch.setattr(ge, "LIFT_LIMIT", len(game.names))
        game._lifting = False
        try:
            assert not ge.coupling_applies(game)
            with pytest.raises(ValueError):
                CornerTable(game, tuple(tuple(QUERY.over[n]) for n in game.names))
        finally:
            game._lifting = False

    def test_the_bounds_on_the_squared_example(self):
        # The second example with (1 − p_fail)²: the coupled bounds of the
        # dispatcher are narrower than those from l_p and u_p, and hold
        # at points of the cell.
        model, query, game = squared_game()
        assert not game.multi_affine and ge.coupling_applies(game)
        rng = random.Random(23)
        for cell in (CELL_D_OPEN, CELL_D_DECIDED):
            dists = cell_distributions(game, cell)
            for player in ("R", "D"):
                proc = build_process(game, player, query.profile, dists)
                lo, hi = reward_bounds(game, proc, cell)
                lower = optimal_values(proc, lambda i: [proc.selected[i]], lo, maximize=False)
                upper = optimal_values(proc, lambda i: [proc.selected[i]], hi, maximize=True)
                separate = advantage_bounds(game, proc, cell, lower, upper)
                plays = CoupledPlays(game, proc, CornerTable(game, cell))
                for key, (a, b) in separate.items():
                    c, d = plays.bound(*key, maximize=False), plays.bound(*key, maximize=True)
                    if player == "D":
                        assert a < c and d < b
                    for point in points_of(cell, rng, 2):
                        exact = exact_advantages(game, player, point)
                        assert c <= exact[(proc.states[key[0]], key[1])] <= d

    def test_the_map_of_the_squared_example(self):
        _, query, game = squared_game()
        with_lifting = ge.design_map(game, query, max_cells=300)
        saved = ge.USE_LIFTING
        ge.USE_LIFTING = False
        try:
            without = ge.design_map(game, query, max_cells=300)
        finally:
            ge.USE_LIFTING = saved
        assert with_lifting["undecided_fraction"] < without["undecided_fraction"]
        rng = random.Random(24)
        for cell in with_lifting["cells"]:
            if cell.status == "undecided":
                continue
            box = tuple(cell.box[n] for n in game.names)
            for point in points_of(box, rng, 1):
                gains = {p: player_at_point(game, query.profile, p, point).gain
                         for p in ("R", "D")}
                if cell.status == "equilibrium":
                    assert all(g == 0 for g in gains.values())
                else:
                    assert all(gains[p] > 0 for p in cell.deviators)


# The lifting bugs that narrow the interval of a lifted coordinate make
# wrong claims of maps and wrong bounds on the squared example;
# `lift_corners_only` (no lifting) is found only by the direct comparison
# of the bounds with the advantages (no wrong claim of a map in 4 500
# models of mode="nonaffine", none on the squared example, whose
# probability (1 − p_fail)² is monotone and convex on the region).
LIFTING_RANGE_MUTATIONS = ("lift_centre_range", "lift_lower_range")


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

    def test_probabilities_that_are_not_multi_affine(self):
        stats = games_fuzz.run(33, 30, mode="nonaffine")
        assert_sound(stats)
        assert stats["players_decided_by_coupling_lifted"] > 0
        assert stats["advantages_compared_lifted"] > 0

    @pytest.mark.parametrize("name", games_fuzz.LIFTING_MUTATIONS)
    def test_a_lifting_bug_is_detected_by_the_direct_check(self, name):
        stats = games_fuzz.run(2, 100, name, mode="nonaffine")
        assert "failure" in stats, f"the mutant {name} survived {stats}"

    @pytest.mark.parametrize("name", LIFTING_RANGE_MUTATIONS)
    def test_a_lifting_bug_is_detected_by_the_maps(self, name):
        stats = games_fuzz.run(2, 150, name, mode="nonaffine", direct=False)
        assert "failure" in stats, f"the mutant {name} survived {stats}"

    @pytest.mark.parametrize("name", LIFTING_RANGE_MUTATIONS)
    def test_a_lifting_bug_is_detected_on_the_squared_example(self, name):
        model, query, game = squared_game()
        with games_fuzz.mutation(name):
            wrong = False
            rng = random.Random(6)
            for cell in (CELL_D_OPEN, CELL_D_DECIDED):
                dists = cell_distributions(game, cell)
                for player in ("R", "D"):
                    proc = build_process(game, player, query.profile, dists)
                    plays = ge.CoupledPlays(game, proc, ge.CornerTable(game, cell))
                    keys = [(i, k) for i in range(len(proc.states))
                            if proc.mine[i] and not proc.target[i]
                            for k, _ in proc.options[i]
                            if k is not None and k != proc.selected[i]]
                    for point in points_of(cell, rng, 2):
                        exact = exact_advantages(game, player, point)
                        wrong |= any(
                            not plays.bound(i, k, False) <= exact[(proc.states[i], k)]
                            <= plays.bound(i, k, True) for i, k in keys)
        assert wrong

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
