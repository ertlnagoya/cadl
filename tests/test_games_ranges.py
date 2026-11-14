"""Strategic models (Appendix I): the exact ranges of polynomials that are
not multi-affine on a cell (`poly_range`), which the relaxation of I.9
uses for such probabilities and step payoffs, and the bounds of the
advantages through the Bellman equations of the profile
(`unrolled_advantage`), which PD+ and PD− use before the coupled bounds.
Every deliberate soundness bug of `tests/games_fuzz.py` that concerns them
is detected by fuzzing."""

from __future__ import annotations

import itertools
import random
from fractions import Fraction
from pathlib import Path

import pytest

import cadl.games_eval as ge
from cadl.games_eval import (
    EqCell,
    bound_verdict,
    build_process,
    cell_distributions,
    optimal_values,
    player_at_point,
    point_distributions,
    point_reward,
    poly_range,
    prepare_map,
    reward_bounds,
    unrolled_advantage,
)
from cadl.parser import parse
from cadl.stochastic import checked_model
from cadl.stochastic_regions import _p_eval, _p_interval

from . import games_fuzz

F = Fraction
ROOT = Path(__file__).parent
ROBOT = ROOT.parent / "examples" / "games_robot_delivery.cadl"
TWO = ROOT.parent / "examples" / "games_two_players.cadl"


def poly(**terms) -> tuple:
    """A polynomial in (p, q) from terms such as p1q2=-3."""
    out = {}
    for name, c in terms.items():
        e = (int(name[1]) if "p" in name else 0, int(name[name.index("q") + 1]) if "q" in name
             else 0)
        out[e] = F(c)
    return ge._canonical(out)


def grid(cell: tuple, k: int = 8) -> list:
    return list(itertools.product(*[[a + (b - a) * F(i, k) for i in range(k + 1)]
                                    for a, b in cell]))


class TestPolyRange:
    def test_interior_extremes_are_exact(self):
        f = poly(p1q0=4, p2q0=-4)                       # 4p(1 − p), largest 1 at 1/2
        for cell in [((F(2, 5), F(3, 5)), (F(0), F(0))), ((F(3, 8), F(3, 5)), (F(0), F(0))),
                     ((F(0), F(1)), (F(0), F(0)))]:
            lo, hi = poly_range(f, cell)
            assert hi == 1
            assert lo == min(_p_eval(f, (a, F(0))) for a in cell[0])
            # Interval arithmetic on the monomials is above 1 on each of them.
            assert _p_interval(f, cell)[1] > 1

    def test_corner_extremes_are_exact(self):
        f = poly(p0q0=1, p1q0=-2, p2q0=1)                # (1 − p)², 1 + p² − 2p
        cell = ((F(0), F(1, 2)), (F(0), F(1)))
        assert poly_range(f, cell) == (F(1, 4), F(1))
        assert _p_interval(f, cell)[1] == F(5, 4)
        g = poly(p1q1=1, p1q2=-1)                       # p · q · (1 − q)
        assert poly_range(g, ((F(0), F(1)), (F(1, 4), F(1)))) == (F(0), F(1, 4))

    def test_the_range_contains_the_values(self):
        rng = random.Random(1)
        for _ in range(60):
            f = ge._canonical({(rng.randint(0, 3), rng.randint(0, 3)): F(rng.randint(-5, 5))
                               for _ in range(rng.randint(1, 5))})
            f = tuple((e, c) for e, c in f if c)
            if not f:
                continue
            a = F(rng.randint(-4, 4), 4)
            b = F(rng.randint(-2, 2), 3)
            cell = ((a, a + F(rng.randint(1, 4), 4)), (b, b + F(rng.randint(1, 3), 5)))
            lo, hi = poly_range(f, cell)
            values = [_p_eval(f, x) for x in grid(cell)]
            assert lo <= min(values) and max(values) <= hi
            i_lo, i_hi = _p_interval(f, cell)
            assert i_lo <= lo and hi <= i_hi

    def test_a_cut_off_search_stays_an_enclosure(self, monkeypatch):
        f = poly(p1q0=4, p2q0=-4, p1q1=1, p2q2=-3)
        cell = ((F(1, 7), F(6, 7)), (F(-1, 3), F(1, 2)))
        monkeypatch.setattr(ge, "_RANGE_CACHE", {})
        lo, hi = poly_range(f, cell, boxes=1)
        values = [_p_eval(f, x) for x in grid(cell, 12)]
        assert lo <= min(values) and max(values) <= hi

    def test_without_tight_ranges(self, monkeypatch):
        f = poly(p1q0=4, p2q0=-4)
        cell = ((F(2, 5), F(3, 5)), (F(0), F(1)))
        monkeypatch.setattr(ge, "TIGHT_RANGES", False)
        assert poly_range(f, cell) == _p_interval(f, cell)


QUARTIC = TWO.read_text(encoding="utf-8").replace(
    "(1 - p_fail)", "(1 - 4 * p_fail * (1 - p_fail))")
THREE = """      - id: three
        profile: comply_if_watched
        regimes: [CENTRALIZED]
        over: { lambda: [0, 0.4], kappa: [0, 0.4], p_fail: [0, 0.5] }
        resolution: 0.1
"""


class TestRelaxation:
    def test_a_cell_around_the_largest_value_is_well_defined(self):
        # 1 − 4 p_fail (1 − p_fail) is 0 at p_fail = 1/2; interval arithmetic
        # on the monomials gives it a negative lower end near there, and the
        # cell was undecided (not_well_defined) up to cadl 0.3.30.
        model = checked_model(parse(QUARTIC + THREE))
        query = next(q for q in model.equilibria if q.id == "three")
        game = prepare_map(model, query, model.design("CENTRALIZED"), None)
        box = {"lambda": (F(0), F(1, 10)), "kappa": (F(0), F(1, 10)),
               "p_fail": (F(3, 8), F(1, 2))}
        cell = EqCell(box=box)
        bound_verdict(game, query, cell)
        assert cell.reason != "not_well_defined"
        saved = ge.TIGHT_RANGES
        ge.TIGHT_RANGES = False
        try:
            old = EqCell(box=box)
            bound_verdict(game, query, old)
        finally:
            ge.TIGHT_RANGES = saved
        assert old.status == "undecided" and old.reason == "not_well_defined"


ROBOT_MODEL = checked_model(parse(ROBOT.read_text(encoding="utf-8")))
ROBOT_QUERY = next(q for q in ROBOT_MODEL.equilibria if q.id == "comply_by_lambda_and_failure")
ROBOT_GAME = prepare_map(ROBOT_MODEL, ROBOT_QUERY, ROBOT_MODEL.design("CENTRALIZED"), None)
# λ, p_fail: the robot gains by skipping the monitoring at every valuation.
ROBOT_CELL = ((F(0), F(1, 30)), (F(5, 24), F(1, 4)))


def advantages_at(game, player: str, point: tuple) -> dict:
    proc = build_process(game, player, "comply", point_distributions(game, point))
    reward = point_reward(game, proc, point)
    v = optimal_values(proc, ge._profile(proc), reward)
    out = {}
    for i, g in enumerate(proc.states):
        if proc.target[i] or not proc.mine[i]:
            continue
        for k, ds in proc.options[i]:
            out[(g, k)] = reward(i, k) + sum((p * v[j] for j, p in ds[0].items()), F(0)) - v[i]
    return out


class TestUnrolledAdvantages:
    def bounds(self, cell: tuple) -> tuple:
        dists = cell_distributions(ROBOT_GAME, cell)
        proc = build_process(ROBOT_GAME, "R", "comply", dists)
        lo, hi = reward_bounds(ROBOT_GAME, proc, cell)
        follow = ge._profile(proc)
        lower = optimal_values(proc, follow, lo, maximize=False)
        upper = optimal_values(proc, follow, hi, maximize=True)
        plain = ge.advantage_bounds(ROBOT_GAME, proc, cell, lower, upper)
        unrolled = {key: unrolled_advantage(ROBOT_GAME, proc, cell, *key, lower, upper)
                    for key in plain}
        return proc, plain, unrolled

    def test_the_bounds_contain_the_advantages_and_are_narrower(self):
        proc, plain, unrolled = self.bounds(ROBOT_CELL)
        rng = random.Random(2)
        points = grid(ROBOT_CELL, 4) + [
            tuple(a + (b - a) * F(rng.randint(0, 99), 99) for a, b in ROBOT_CELL)
            for _ in range(10)]
        for point in points:
            exact = advantages_at(ROBOT_GAME, "R", point)
            for (i, k), (lo, hi) in unrolled.items():
                assert lo <= exact[(proc.states[i], k)] <= hi
        # The advantage of skipping the monitoring in the initial state:
        # positive on the cell, which the bounds from l_p and u_p miss.
        key = (0, 1)
        assert plain[key][0] < 0 < unrolled[key][0]
        assert unrolled[key][1] < plain[key][1]

    def test_a_cell_decided_by_the_unrolled_bounds(self):
        dists = cell_distributions(ROBOT_GAME, ROBOT_CELL)
        on = ge.player_on_cell(ROBOT_GAME, "comply", "R", ROBOT_CELL, dists, F(0))
        assert on.n and on.unrolled and on.gain_lower > 0
        saved = ge.UNROLL_ADVANTAGES
        ge.UNROLL_ADVANTAGES = False
        try:
            off = ge.player_on_cell(ROBOT_GAME, "comply", "R", ROBOT_CELL, dists, F(0))
        finally:
            ge.UNROLL_ADVANTAGES = saved
        assert not off.n
        for point in grid(ROBOT_CELL, 3):
            assert player_at_point(ROBOT_GAME, "comply", "R", point).gain > 0

    def test_the_map_of_the_example_needs_fewer_cells(self):
        new = ge.design_map(ROBOT_GAME, ROBOT_QUERY)
        saved = ge.UNROLL_ADVANTAGES
        ge.UNROLL_ADVANTAGES = False
        try:
            old = ge.design_map(ROBOT_GAME, ROBOT_QUERY)
        finally:
            ge.UNROLL_ADVANTAGES = saved
        assert new["resolution_reached"] and old["resolution_reached"]
        assert new["cells_analysed"] < old["cells_analysed"]
        # No decided cell of either map contradicts the other.
        def claims(result):
            return [(c.box, c.status, tuple(c.deviators)) for c in result["cells"]
                    if c.status != "undecided"]
        for box, status, deviators in claims(old):
            for box2, status2, deviators2 in claims(new):
                overlap = all(max(box[n][0], box2[n][0]) < min(box[n][1], box2[n][1])
                              for n in box)
                if overlap:
                    assert status == status2


def assert_sound(stats: dict) -> None:
    assert "failure" not in stats, stats["failure"] + "\n" + stats.get("failing_model", "")


class TestFuzz:
    @pytest.mark.parametrize("mode", [None, "nonaffine"])
    def test_maps_and_bounds(self, mode):
        stats = games_fuzz.run(41, 25, mode=mode)
        assert_sound(stats)
        assert stats["advantages_compared"] > 0
        assert stats.get("players_decided_by_unrolled", 0) > 0
        assert "reason_not_well_defined" not in stats

    @pytest.mark.parametrize("name", games_fuzz.RANGE_MUTATIONS)
    @pytest.mark.parametrize("direct", [True, False])
    def test_a_bug_is_detected(self, name, direct):
        stats = games_fuzz.run(2, 100, name, mode="nonaffine", direct=direct)
        assert "failure" in stats, f"the mutant {name} survived {stats}"
