"""Certified regime maps of the stochastic model extension (Appendix H,
H.9): the relaxation of a design on a cell, the exact solution of the
relaxation, the classification of a cell, the refinement, and the
command. Every `decided` and `infeasible` cell is checked against the
exact point evaluator, and the bounds are compared with Storm when
`stormpy` is installed."""

from __future__ import annotations

import copy
import itertools
import json
import random
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from cadl.cli import main
from cadl.parser import parse, parse_file
from cadl.stochastic import checked_model, decimal_text, export_prism, parse_param_expr, property_text
from cadl.stochastic_eval import (
    ESTIMATE_NOTE,
    INFINITY,
    EvaluationError,
    build_chain,
    evaluate,
    evaluate_map_point,
    valuation,
)
from cadl.stochastic_regions import (
    CERTIFIED_NOTE,
    METHOD,
    _p_interval,
    _polytope_vertices,
    _canonical,
    bounded_until,
    build_parametric_chain,
    cell_bounds,
    certified_regime_map,
    classify,
    design_bounds,
    graph_preserving,
    max_until,
    min_total_reward,
    min_until,
    polynomial,
    relax,
    reward_bounds,
)
from cadl.type_checker import type_check

ROOT = Path(__file__).parent
EXAMPLE = ROOT.parent / "examples" / "stochastic_robot_delivery.cadl"
REGIONS = ROOT / "data" / "stochastic_regions.cadl"

F = Fraction
H = F(1, 2)


def load(path: Path, mutate=None):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if mutate is not None:
        data = copy.deepcopy(data)
        mutate(data["sos"]["model"], data["sos"])
    sos = parse(yaml.safe_dump(data, sort_keys=False))
    result = type_check(sos)
    assert result.ok, [str(e) for e in result.errors]
    return checked_model(sos)


@pytest.fixture(scope="module")
def example():
    return load(EXAMPLE)


@pytest.fixture(scope="module")
def regions():
    return load(REGIONS)


MAPS = [
    (EXAMPLE, "best_for_delivery"),
    (REGIONS, "fastest"),
    (REGIONS, "fastest_free"),
    (REGIONS, "cheapest"),
    (REGIONS, "most_reliable"),
    (REGIONS, "eventually"),
    (REGIONS, "eventually_tie"),
    (REGIONS, "reliable_3d"),
]
MAP_IDS = [m for _, m in MAPS]


@pytest.fixture(scope="module")
def computed():
    """Every map of the two files, computed once."""
    out = {}
    for path, map_id in MAPS:
        model = load(path)
        out[map_id] = (model, certified_regime_map(model, map_id))
    return out


# ---------------------------------------------------------------------------
# Polynomials and the relaxation
# ---------------------------------------------------------------------------

class TestPolynomials:
    def test_expansion(self):
        tree = parse_param_expr("b * (1 - p) * (1 - q) + (1 - b) * 0.5")
        poly = polynomial(tree, {"b": F("0.9")}, ["p", "q"])
        assert poly == {(0, 0): F("0.95"), (1, 0): F("-0.9"), (0, 1): F("-0.9"),
                        (1, 1): F("0.9")}
        assert polynomial(parse_param_expr("p - p"), {}, ["p"]) == {}
        assert polynomial(parse_param_expr("-(p * p)"), {}, ["p"]) == {(2,): F(-1)}

    def test_interval_encloses(self):
        poly = _canonical(polynomial(parse_param_expr("p * p - p * q + 0.25"), {}, ["p", "q"]))
        box = ((F(-1), F(1, 2)), (F(0), F(2)))
        lo, hi = _p_interval(poly, box)
        rnd = random.Random(1)
        for _ in range(200):
            p = F(rnd.randint(-100, 50), 100)
            q = F(rnd.randint(0, 200), 100)
            assert lo <= p * p - p * q + F(1, 4) <= hi
        # an even power of an interval around zero starts at zero
        assert _p_interval(_canonical({(2,): F(1)}), ((F(-1), F(2)),)) == (0, 4)
        assert _p_interval(_canonical({(3,): F(1)}), ((F(-1), F(2)),)) == (-1, 8)

    def test_polytope_vertices(self):
        vs = _polytope_vertices([(F(0), H), (H, F(1))])
        assert sorted(vs) == [(F(0), F(1)), (H, H)]
        vs = _polytope_vertices([(F(1, 10), F(3, 10)), (F(1, 5), F(2, 5)), (F(2, 5), F(3, 5))])
        assert vs and all(sum(v) == 1 for v in vs)
        assert all(F(1, 10) <= a <= F(3, 10) and F(1, 5) <= b <= F(2, 5) for a, b, _ in vs)
        # a degenerate polytope: one point
        assert _polytope_vertices([(H, H), (H, H)]) == [(H, H)]


class TestRelaxation:
    def chain(self, model, map_id, regime):
        query = next(q for q in model.regime_maps if q.id == map_id)
        design = model.design(regime)
        return build_parametric_chain(model, design, list(query.over),
                                      valuation(model, design))

    def test_example_chain(self, example):
        chain = self.chain(example, "best_for_delivery", "CENTRALIZED")
        assert len(chain.states) == 7 and len(chain.rows) == 1
        assert chain.rows[0].multi_affine and chain.rows[0].occurring == (0, 1)
        box = {"p_fail": (F(0), F(1, 4)), "p_loss": (F(0), F(1, 4))}
        acts = relax(chain, box)
        # s = 0.81 x + 0.06 with x = (1 - p_fail)(1 - p_loss): 4 corners, 3 values of x
        values = sorted({a[j] for a in acts[0] for j in a if chain.states[j][0] == "Delivered"})
        assert values == [F("0.81") * x + F("0.06") for x in (F(9, 16), F(3, 4), F(1))]
        assert graph_preserving(acts)
        # the absorbing states have one action
        deadlocks = [i for i, r in enumerate(chain.row_of) if r is None]
        assert all(acts[i] == [{i: F(1)}] for i in deadlocks) and len(deadlocks) == 4

    def test_bounds_are_tight_for_the_example(self, example):
        # x is monotone in both parameters, so the corners give the range
        box = {"p_fail": (F(0), F(1, 4)), "p_loss": (F(1, 8), F(1, 4))}
        lo, hi = design_bounds(example, "best_for_delivery", "CENTRALIZED", box)
        s_lo = F("0.81") * F(3, 4) * F(3, 4) + F("0.06")
        s_hi = F("0.81") * F(7, 8) + F("0.06")
        assert (lo, hi) == (1 - (1 - s_lo) ** 3, 1 - (1 - s_hi) ** 3)

    def test_not_graph_preserving_cell(self, regions):
        chain = self.chain(regions, "fastest", "DIRECT")
        acts = relax(chain, {"p": (F(0), H), "q": (F(0), F(1))})
        assert not graph_preserving(acts)
        assert sorted(sorted(a.items()) for a in acts[0]) == [
            [(0, H), (1, H)], [(0, F(1))]]
        assert graph_preserving(relax(chain, {"p": (F(1, 4), H), "q": (F(0), F(1))}))

    def test_non_multi_affine_row_uses_intervals(self, regions):
        chain = self.chain(regions, "most_reliable", "LAZY")
        assert chain.non_affine and not chain.rows[0].multi_affine
        acts = relax(chain, {"p": (H, F(1)), "q": (F(0), F(1))})
        done = next(i for i, s in enumerate(chain.states) if s == ("Done",))
        # 0.5 p^2 lies in [1/8, 1/2]; drop is fixed at 0.1
        assert {a[done] for a in acts[0]} == {F(1, 8), H}
        assert all(sum(a.values()) == 1 for a in acts[0])

    def test_well_definedness_not_established(self):
        def mutate(model, sos):
            model["commands"][3]["updates"][0]["prob"] = "(p - 0.5) * (p - 0.5)"
        model = load(REGIONS, mutate)
        whole = {"p": (F(0), F(1)), "q": (F(0), F(1))}
        info = cell_bounds(model, "most_reliable", whole)
        assert "interval arithmetic" in info["LAZY"]["reason"]
        assert "objective" in info["DIRECT"]
        assert design_bounds(model, "most_reliable", "LAZY", whole) is None
        # away from p = 0.5 the intervals do establish it
        away = {"p": (F(9, 10), F(1)), "q": (F(0), F(1))}
        assert "objective" in cell_bounds(model, "most_reliable", away)["LAZY"]
        result = certified_regime_map(model, "most_reliable", max_cells=60)
        reasons = {c.reason for c in result["cells"] if c.reason}
        assert len(reasons) == 1 and "command 4 of design LAZY" in reasons.pop()
        assert all(c.status == "undecided" for c in result["cells"] if c.reason)
        assert_sound(model, "most_reliable", result)

    def test_open_parameter_cannot_be_set(self, example):
        with pytest.raises(EvaluationError, match="open in the regime map"):
            certified_regime_map(example, "best_for_delivery", {"p_fail": F(0)})

    def test_cell_must_lie_in_the_region(self, example):
        with pytest.raises(EvaluationError, match="not inside the region"):
            design_bounds(example, "best_for_delivery", "CENTRALIZED",
                          {"p_fail": (F(0), F(1)), "p_loss": (F(0), H)})


# ---------------------------------------------------------------------------
# The exact solution of the relaxation
# ---------------------------------------------------------------------------

class TestSolver:
    def test_bounded_until(self):
        # state 0 retries with success probability 1/4 or 1/2; 1 is the target
        acts = [[{1: F(1, 4), 0: F(3, 4)}, {1: H, 0: H}], [{1: F(1)}]]
        t, f = [True, True], [False, True]
        assert bounded_until(acts, t, f, 0) == (0, 0)
        assert bounded_until(acts, t, f, 1) == (F(1, 4), H)
        assert bounded_until(acts, t, f, 3) == (1 - F(3, 4) ** 3, 1 - H ** 3)
        # phi1 false in the initial state: no step is taken
        assert bounded_until(acts, [False, True], f, 3) == (0, 0)

    def test_until_with_an_end_component(self):
        # 0 may loop for ever or toss a coin between the target 1 and the sink 2
        acts = [[{0: F(1)}, {1: H, 2: H}], [{1: F(1)}], [{2: F(1)}]]
        t, target = [True] * 3, [False, True, False]
        assert max_until(acts, t, target)[0] == H     # the first policy has value 0
        assert min_until(acts, t, target)[0] == 0

    def test_until_values(self):
        # 0 -> 1 or 2 by a coin; 1 reaches the target 3 with 1/4 or 3/4, else the sink 4;
        # 2 returns to 0 or goes to the target with 1/2.
        acts = [
            [{1: H, 2: H}],
            [{3: F(1, 4), 4: F(3, 4)}, {3: F(3, 4), 4: F(1, 4)}],
            [{0: H, 3: H}],
            [{3: F(1)}],
            [{4: F(1)}],
        ]
        t, target = [True] * 5, [False, False, False, True, False]
        # x0 = (x1 + x2) / 2, x2 = (x0 + 1) / 2  =>  x0 = (2 x1 + 1) / 3
        assert min_until(acts, t, target)[0] == (2 * F(1, 4) + 1) / 3
        assert max_until(acts, t, target)[0] == (2 * F(3, 4) + 1) / 3
        # phi1 forbids state 2: only the way through 1 counts
        phi1 = [True, True, False, True, True]
        assert min_until(acts, phi1, target)[0] == F(1, 8)
        assert max_until(acts, phi1, target)[0] == F(3, 8)

    def test_reward_bounds(self):
        rew = [F(1), F(0)]
        target = [False, True]
        retry = [[{1: F(1, 4), 0: F(3, 4)}, {1: H, 0: H}], [{1: F(1)}]]
        assert reward_bounds(retry, target, rew) == (2, 4)
        # one action never leaves: no finite upper bound, the lower bound stays
        stuck = [[{0: F(1)}, {1: H, 0: H}], [{1: F(1)}]]
        assert reward_bounds(stuck, target, rew) == (2, INFINITY)
        # no action reaches the target: +inf on the whole cell
        assert reward_bounds([[{0: F(1)}], [{1: F(1)}]], target, rew) == (INFINITY, INFINITY)
        # the initial state is a target
        assert reward_bounds(retry, [True, True], rew) == (0, 0)

    def test_min_total_reward_with_a_free_loop(self):
        # looping in 0 costs nothing, so the least accumulated reward is 0,
        # and the value of H.8 is only known to lie in [0, +inf]
        acts = [[{0: F(1)}, {1: F(1)}], [{1: F(1)}]]
        assert min_total_reward(acts, [False, True], [F(0), F(0)]) == 0
        acts = [[{1: F(1)}], [{1: F(1)}, {2: F(1)}], [{2: F(1)}]]
        rew = [F(3), F(0), F(0)]
        assert min_total_reward(acts, [False, False, True], rew) == 3
        assert reward_bounds(acts, [False, False, True], rew) == (3, INFINITY)

    def test_min_total_reward_infinite(self):
        # every policy pays for ever in state 0
        acts = [[{0: F(1)}, {0: H, 1: H}], [{1: F(1)}], [{2: F(1)}]]
        assert min_total_reward(acts, [False, False, True], [F(1), F(1), F(0)]) == INFINITY

    def test_bounds_enclose_a_design_with_cycles(self, regions):
        box = {"p": (F(1, 4), H), "q": (F(1, 3), F(2, 3))}
        assert design_bounds(regions, "fastest", "DIRECT", box) == (2, 4)
        assert design_bounds(regions, "fastest", "RELAY", box) == (F(5, 2), 4)
        assert design_bounds(regions, "fastest", "LAZY", box) == (INFINITY, INFINITY)
        # at p = 0 DIRECT never arrives
        zero = {"p": (F(0), F(1, 8)), "q": (F(1, 3), F(2, 3))}
        assert design_bounds(regions, "fastest", "DIRECT", zero) == (8, INFINITY)
        assert design_bounds(regions, "eventually", "DIRECT", zero) == (0, 1)
        assert design_bounds(regions, "eventually", "DIRECT", box) == (1, 1)


# ---------------------------------------------------------------------------
# The classification of a cell (H.9)
# ---------------------------------------------------------------------------

class TestClassify:
    def query(self, model, direction="maximize"):
        q = copy.copy(next(q for q in model.regime_maps if q.id == "most_reliable"))
        q.direction = direction
        return q

    def test_maximize(self, regions):
        q = self.query(regions)
        b = {"DIRECT": (F(3, 4), F(1), True), "RELAY": (H, F(3, 4), True),
             "LAZY": (F(0), F(1, 4), None)}
        # not strict: 3/4 >= 3/4 suffices
        assert classify(q, b) == ("decided", ["DIRECT"])
        b["RELAY"] = (H, F(4, 5), True)
        assert classify(q, b) == ("undecided", [])
        # a regime that violates the constraint on the whole cell is no rival
        b["RELAY"] = (None, None, False)
        assert classify(q, b) == ("decided", ["DIRECT"])
        # a regime whose constraint is open cannot be best, but is a rival
        b = {"DIRECT": (F(3, 4), F(1), None), "RELAY": (H, F(3, 5), True),
             "LAZY": (None, None, False)}
        assert classify(q, b) == ("undecided", [])

    def test_ties_and_infeasible(self, regions):
        q = self.query(regions)
        one = (F(1), F(1), True)
        b = {"DIRECT": one, "RELAY": one, "LAZY": (F(0), H, True)}
        assert classify(q, b) == ("decided", ["DIRECT", "RELAY"])
        out = (None, None, False)
        assert classify(q, {"DIRECT": out, "RELAY": out, "LAZY": out}) == ("infeasible", [])
        # the same solution function: a tie although the bounds are wide
        b = {"DIRECT": (F(0), F(1), True), "RELAY": (F(0), F(1), True), "LAZY": out}
        assert classify(q, b) == ("undecided", [])
        same = {"DIRECT": 0, "RELAY": 0, "LAZY": 1}
        assert classify(q, b, same) == ("decided", ["DIRECT", "RELAY"])

    def test_minimize_and_infinity(self, regions):
        q = self.query(regions, "minimize")
        inf = (INFINITY, INFINITY, True)
        b = {"DIRECT": (F(2), F(4), True), "RELAY": (F(4), F(6), True), "LAZY": inf}
        assert classify(q, b) == ("decided", ["DIRECT"])
        # +inf on the whole cell for every regime: no regime satisfies sat_R
        assert classify(q, {"DIRECT": inf, "RELAY": inf, "LAZY": inf}) == ("infeasible", [])
        # a value that may be +inf is not known to satisfy sat_R
        b = {"DIRECT": (F(2), INFINITY, True), "RELAY": inf, "LAZY": inf}
        assert classify(q, b) == ("undecided", [])
        # under maximize +inf is simply the best value
        q = self.query(regions)
        b = {"DIRECT": inf, "RELAY": (F(4), F(6), True), "LAZY": inf}
        assert classify(q, b) == ("decided", ["DIRECT", "LAZY"])


# ---------------------------------------------------------------------------
# The maps: structure and soundness
# ---------------------------------------------------------------------------

def sample_points(cell, rnd, extra=6):
    """The corners of a cell, its centre, and random rational points of it."""
    names = list(cell.box)
    points = [dict(zip(names, c)) for c in itertools.product(*[cell.box[n] for n in names])]
    points.append({n: (lo + hi) / 2 for n, (lo, hi) in cell.box.items()})
    for _ in range(extra):
        points.append({n: lo + (hi - lo) * F(rnd.randint(0, 997), 997)
                       for n, (lo, hi) in cell.box.items()})
    return points


def assert_sound(model, map_id, result, overrides=None, seed=0):
    """Every claim of the map holds at sample points of its cell, by the
    exact point evaluator. Returns the number of point evaluations."""
    query = next(q for q in model.regime_maps if q.id == map_id)
    rnd = random.Random(seed)
    checked = 0
    for cell in result["cells"]:
        if cell.status == "undecided":
            assert cell.best == []
            continue
        for point in sample_points(cell, rnd):
            at = evaluate_map_point(model, query, point, overrides)
            if cell.status == "decided":
                assert cell.best and set(cell.best) <= set(at["best"]), (cell.box, point, at)
            else:
                assert cell.status == "infeasible" and cell.best == []
                assert at["best"] == [], (cell.box, point, at)
            checked += 1
    return checked


def volume(box):
    v = F(1)
    for lo, hi in box.values():
        v *= hi - lo
    return v


class TestMaps:
    @pytest.mark.parametrize("map_id", MAP_IDS)
    def test_cells_partition_the_region(self, computed, map_id):
        model, result = computed[map_id]
        query = next(q for q in model.regime_maps if q.id == map_id)
        cells = result["cells"]
        region = {n: tuple(query.over[n]) for n in query.over}
        assert sum(volume(c.box) for c in cells) == volume(region)
        for c in cells:
            assert all(region[n][0] <= lo < hi <= region[n][1] for n, (lo, hi) in c.box.items())
        for a, b in itertools.combinations(cells, 2):
            # the interiors are disjoint: separated along some parameter
            assert any(a.box[n][1] <= b.box[n][0] or b.box[n][1] <= a.box[n][0]
                       for n in region)
        undecided = sum(volume(c.box) for c in cells if c.status == "undecided")
        assert result["undecided_fraction"] == undecided / volume(region)
        assert result["resolution_reached"] == (result["undecided_fraction"] <= query.resolution)
        assert result["counts"] == {s: sum(1 for c in cells if c.status == s)
                                    for s in ("decided", "infeasible", "undecided")}

    @pytest.mark.parametrize("map_id", MAP_IDS)
    def test_claims_hold_at_sample_points(self, computed, map_id):
        model, result = computed[map_id]
        assert result["resolution_reached"] and result["stopped"] == "resolution"
        assert assert_sound(model, map_id, result) > 0

    def test_example_boundary(self, computed):
        """H.12: CENTRALIZED is optimal where (1 - p_fail)(1 - p_loss) >= 2/3,
        DISTRIBUTED where it is <= 2/3; a cell is certified by its corners."""
        _, result = computed["best_for_delivery"]
        seen = set()
        near = 0            # undecided cells that the curve does not cross
        for c in result["cells"]:
            (f1, f2), (l1, l2) = c.box["p_fail"], c.box["p_loss"]
            x_min, x_max = (1 - f2) * (1 - l2), (1 - f1) * (1 - l1)
            crossed = x_min < F(2, 3) < x_max
            # H.12: parameter lifting gives the exact range of each design
            # on a cell, so the bounds decide CENTRALIZED when s_C at x_min
            # is at least s_D at x_max, and DISTRIBUTED the other way round.
            # (A reported cell may be the union of decided halves.)
            central = F("0.81") * x_min - F("0.09") * x_max >= F("0.48")
            distributed = F("0.81") * x_max - F("0.09") * x_min <= F("0.48")
            if c.status == "decided":
                assert not crossed
                assert c.best == (["CENTRALIZED"] if x_min >= F(2, 3) else ["DISTRIBUTED"])
                assert not (distributed if x_min >= F(2, 3) else central)
            else:
                assert c.status == "undecided" and not central and not distributed
                near += 0 if crossed else 1
            seen.update(c.best)
        assert seen == {"CENTRALIZED", "DISTRIBUTED"} and near == 4
        # points of the curve p_loss = 1 - 2 / (3 (1 - p_fail))
        for k in range(0, 34):
            f = F(k, 100)
            point = {"p_fail": f, "p_loss": 1 - F(2, 3) / (1 - f)}
            inside = [c for c in result["cells"]
                      if all(c.box[n][0] < point[n] < c.box[n][1] for n in point)]
            assert all(c.status == "undecided" for c in inside)
        assert result["counts"]["infeasible"] == 0
        assert result["undecided_fraction"] <= F(1, 20)

    def test_example_numbers(self, computed):
        # the numbers quoted in H.14 and in the README
        _, result = computed["best_for_delivery"]
        assert result["counts"] == {"decided": 60, "infeasible": 0, "undecided": 35}
        assert result["undecided_fraction"] == F(51, 1024)
        assert result["fraction_by_best"] == {"CENTRALIZED": F(235, 1024),
                                              "DISTRIBUTED": F(369, 512)}
        assert (result["cells_before_merging"], result["cells_analysed"],
                result["deepest"]) == (121, 241, 10)

    def test_infeasible_regions(self, computed):
        # fastest: no regime within 4 steps where p < 1/4 and q < 1/3
        _, result = computed["fastest"]
        cells = [c for c in result["cells"] if c.status == "infeasible"]
        assert cells
        assert all(c.box["p"][1] <= F(1, 4) and c.box["q"][1] <= F(1, 3) for c in cells)
        assert any(c.box["p"][0] == 0 and c.box["q"][0] == 0 for c in cells)
        # most_reliable: 1 - (1-p)^4 < 0.5 and 1 - (1-q)^3 < 0.5
        _, result = computed["most_reliable"]
        cells = [c for c in result["cells"] if c.status == "infeasible"]
        assert cells
        assert all(1 - (1 - c.box["p"][1]) ** 4 < H and 1 - (1 - c.box["q"][1]) ** 3 < H
                   for c in cells)

    def test_infinite_reward_is_never_best_under_minimize(self, computed):
        for map_id in ("fastest", "fastest_free"):
            _, result = computed[map_id]
            best = {r for c in result["cells"] for r in c.best}
            assert best == {"DIRECT", "RELAY"}
        # without a constraint nothing is infeasible on a whole cell
        assert computed["fastest_free"][1]["counts"]["infeasible"] == 0

    def test_decided_regions_of_fastest(self, computed):
        # DIRECT where 1/p <= 1 + 1/q, RELAY where 1/p >= 1 + 1/q
        _, result = computed["fastest_free"]
        for c in result["cells"]:
            (p1, p2), (q1, q2) = c.box["p"], c.box["q"]
            if c.best == ["DIRECT"]:
                assert q2 == 0 or 1 / p1 <= 1 + 1 / q2
            elif c.best == ["RELAY"]:
                assert p2 == 0 or 1 / p2 >= 1 + 1 / q1

    def test_tie_on_a_whole_cell(self, computed):
        _, result = computed["eventually_tie"]
        assert [(c.status, c.best) for c in result["cells"]] == [
            ("decided", ["DIRECT", "RELAY"])]
        assert result["undecided_fraction"] == 0 and result["cells_analysed"] == 1

    def test_identical_designs_tie(self):
        def mutate(model, sos):
            model["regimes"]["DISTRIBUTED"]["parameters"]["beta"] = 0.9
        model = load(EXAMPLE, mutate)
        result = certified_regime_map(model, "best_for_delivery")
        assert result["resolution_reached"]
        statuses = {c.status for c in result["cells"]}
        assert statuses == {"decided", "infeasible", "undecided"}
        assert all(c.best == ["CENTRALIZED", "DISTRIBUTED"]
                   for c in result["cells"] if c.status == "decided")
        # both violate P>=0.9 where s = 0.81 x + 0.06 < 1 - 0.1^(1/3)
        assert_sound(model, "best_for_delivery", result)

    def test_three_parameters(self, computed):
        _, result = computed["reliable_3d"]
        assert result["open"] == ["p", "q", "drop"]
        assert {r for c in result["cells"] for r in c.best} == {"DIRECT", "RELAY"}

    def test_overrides(self, example):
        over = {"alpha": F(1, 2)}
        result = certified_regime_map(example, "best_for_delivery", over)
        # s_C = 0.45 x + 0.06 < s_D = 0.05 x + 0.54 everywhere: one cell
        assert [(c.status, c.best) for c in result["cells"]] == [("decided", ["DISTRIBUTED"])]
        assert_sound(example, "best_for_delivery", result, over)

    def test_budget(self, example):
        result = certified_regime_map(example, "best_for_delivery", max_cells=10)
        assert not result["resolution_reached"] and result["stopped"] == "max_cells"
        assert result["cells_before_merging"] == 10
        assert result["undecided_fraction"] > F(1, 20)
        assert_sound(example, "best_for_delivery", result)
        result = certified_regime_map(example, "best_for_delivery", max_depth=4)
        assert not result["resolution_reached"] and result["stopped"] == "max_depth"
        assert result["deepest"] == 4
        result = certified_regime_map(example, "best_for_delivery", max_depth=0)
        assert [(c.status, c.best) for c in result["cells"]] == [("undecided", [])]
        assert result["undecided_fraction"] == 1
        with pytest.raises(EvaluationError):
            certified_regime_map(example, "best_for_delivery", max_cells=0)
        with pytest.raises(EvaluationError):
            certified_regime_map(example, "nope")

    @pytest.mark.parametrize("map_id", MAP_IDS)
    def test_bounds_enclose_point_values(self, computed, map_id):
        """The bounds of every design on sampled cells enclose the exact
        value at points of the cell, for the objective and the constraint."""
        model, result = computed[map_id]
        query = next(q for q in model.regime_maps if q.id == map_id)
        rnd = random.Random(7)
        cells = result["cells"]
        for cell in rnd.sample(cells, min(len(cells), 12)):
            info = cell_bounds(model, map_id, cell.box)
            for regime, entry in info.items():
                design = model.design(regime)
                for point in sample_points(cell, rnd, 3):
                    theta = valuation(model, design)
                    theta.update(point)
                    chain = build_chain(model, design, theta)
                    lo, hi = entry["objective"]
                    assert lo <= evaluate(chain, query.objective) <= hi
                    if query.constraint is not None:
                        lo, hi = entry["constraint"]
                        assert lo <= evaluate(chain, query.constraint) <= hi


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

class TestCommand:
    ARGS = ["model-check", str(EXAMPLE), "--regime-map", "best_for_delivery", "--certify"]

    def test_text(self, capsys):
        assert main(self.ARGS) == 0
        out = capsys.readouterr().out
        assert CERTIFIED_NOTE in out and METHOD in out
        assert ESTIMATE_NOTE not in out and "sample point" not in out
        assert "cells: 95 (60 decided, 0 infeasible, 35 undecided)" in out
        assert "undecided fraction: 0.049805 (exact: 51/1024); resolution: 0.05" in out
        assert "The resolution is reached." in out
        assert "  p_fail in [0, 0.125], p_loss in [0, 0.125]: decided, best = CENTRALIZED" in out
        assert sum(1 for line in out.splitlines() if line.endswith(": undecided")) == 35

    def test_json(self, capsys):
        assert main(self.ARGS + ["--format", "json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert "regime_map_estimate" not in data
        m = data["regime_map"]
        assert m["certified"] is True and m["method"] == METHOD
        assert m["resolution_reached"] is True and m["stopped"] == "resolution"
        assert m["undecided_fraction"] == "51/1024" and m["resolution"] == "0.05"
        assert m["counts"] == {"decided": 60, "infeasible": 0, "undecided": 35, "cells": 95,
                               "cells_before_merging": 121, "cells_analysed": 241,
                               "deepest_bisection": 10}
        assert m["budget"] == {"max_depth": 24, "max_cells": 20000}
        assert len(m["cells"]) == 95
        assert m["cells"][0] == {"box": {"p_fail": ["0", "0.125"], "p_loss": ["0", "0.125"]},
                                 "status": "decided", "best": ["CENTRALIZED"]}
        assert {c["status"] for c in m["cells"]} == {"decided", "undecided"}
        assert all(c["best"] == [] for c in m["cells"] if c["status"] == "undecided")

    def test_resolution_not_reached(self, capsys):
        assert main(self.ARGS + ["--max-cells", "10"]) == 1
        out = capsys.readouterr().out
        assert "The resolution is NOT reached: the partition has 10 cells (--max-cells 10)." in out
        assert main(self.ARGS + ["--max-depth", "2", "--format", "json"]) == 1
        m = json.loads(capsys.readouterr().out)["regime_map"]
        assert m["resolution_reached"] is False and m["stopped"] == "max_depth"

    def test_infeasible_and_reason_in_output(self, capsys):
        assert main(["model-check", str(REGIONS), "--regime-map", "fastest", "--certify"]) == 0
        out = capsys.readouterr().out
        assert ": infeasible" in out and "infeasible: 0." in out

    def test_set(self, capsys):
        assert main(self.ARGS + ["--set", "alpha=0.5"]) == 0
        assert "cells: 1 (1 decided" in capsys.readouterr().out
        assert main(self.ARGS + ["--set", "p_fail=0.1"]) == 2
        assert "open in the regime map" in capsys.readouterr().err

    def test_usage_errors(self, capsys):
        assert main(["model-check", str(EXAMPLE), "--certify"]) == 2
        assert "--certify needs --regime-map" in capsys.readouterr().err
        assert main(self.ARGS + ["--grid", "3"]) == 2
        assert "give one of them" in capsys.readouterr().err
        assert main(["model-check", str(EXAMPLE), "--regime-map", "best_for_delivery",
                     "--max-cells", "5"]) == 2
        assert "need --certify" in capsys.readouterr().err
        assert main(self.ARGS + ["--regime", "CENTRALIZED"]) == 2
        assert main(["model-check", str(EXAMPLE), "--regime-map", "nope", "--certify"]) == 2
        assert main(self.ARGS + ["--max-cells", "0"]) == 2
        capsys.readouterr()

    def test_estimates_stay_separate(self, capsys):
        # without --certify the command still prints estimates, and no cell
        assert main(["model-check", str(EXAMPLE), "--regime-map", "best_for_delivery"]) == 0
        out = capsys.readouterr().out
        assert ESTIMATE_NOTE in out and "No cell is decided" in out
        assert CERTIFIED_NOTE not in out and "decided, best" not in out
        assert out.count("best at this point") == 25       # the default grid of 4


# ---------------------------------------------------------------------------
# Comparison with Storm
# ---------------------------------------------------------------------------

# Run in a process of its own: stormpy aborts the interpreter on some
# inputs it does not support.
STORM_WORKER = r"""
import json, sys
import stormpy
import stormpy.pars as pars
from stormpy import developer as dev

jobs = json.load(sys.stdin)
engine = pars.RegionCheckEngine.EXACT_PARAMETER_LIFTING
directions = (pars.OptimizationDirection.MINIMIZE, pars.OptimizationDirection.MAXIMIZE)
out = []
for job in jobs:
    program = stormpy.parse_prism_program(job["file"])
    props = stormpy.parse_properties(";".join(job["formulas"]), program)
    if job["consts"]:
        program, props = stormpy.instantiate_constants(program, job["consts"], props)
    model = stormpy.build_model[stormpy.RationalFunction](program, props)
    env = stormpy.environments.default(model)
    parameters = model.collect_all_parameters()
    region = pars.parse_region(
        ",".join(f"{lo}<={p.name}<={hi}" for p in parameters
                 for lo, hi in [job["region"][p.name]]), parameters)
    result = {"bounds": [], "extrema": [], "verdicts": []}
    for prop, kind in zip(props, job["kinds"]):
        task = dev.CheckTask[stormpy.RationalFunction](prop.get_raw_formula(), True)
        checker = pars.developer.initialize_region_model_checker(env, model, task, engine=engine)
        if kind == "threshold":
            result["verdicts"].append(checker.analyze_region(env, region).name)
            continue
        if job["lifting"]:
            result["bounds"].append(
                [str(checker.get_bound_at_init_state(env, region, d)) for d in directions])
        result["extrema"].append([
            str(pars.compute_extremal_value(
                model, prop, region=region, direction=d, precision=0.001,
                absolute_precision=True, engine=engine)[0])
            for d in directions])
    out.append(result)
print("\nRESULT " + json.dumps(out))
"""


def storm_number(text):
    return INFINITY if text in ("inf", "Infinity", "Inf") else Fraction(text)


class TestAgainstStorm:
    """Storm (stormpy) on the exported files of the same designs and the
    same cells, with its exact parameter lifting engine. Storm assumes a
    graph-preserving region, so only such cells are compared.

    - For a design whose probabilities are multi-affine, the bounds of
      cadl on a cell are equal, as rational numbers, to the bounds Storm's
      parameter lifting gives for the same region.
    - The extrema Storm finds on the cell by refinement are values of the
      solution function at valuations of the cell (within 0.001 of the
      true extrema): the bounds of cadl enclose them.
    - Storm's verdict on the constraint for the cell (AllSat, AllViolated,
      or neither) is the one cadl claims."""

    CELLS_PER_MAP = 10

    def jobs(self, model, map_id, result, tmp_path):
        query = next(q for q in model.regime_maps if q.id == map_id)
        files = export_prism(model)
        for name, text in files.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        consts = ",".join(f"{p.id}={decimal_text(p.value)}" for p in model.params
                          if p.open and p.id not in query.over)
        rnd = random.Random(3)
        cells = result["cells"]
        jobs, expect = [], []
        for cell in rnd.sample(cells, min(len(cells), self.CELLS_PER_MAP)):
            info = cell_bounds(model, map_id, cell.box)
            for regime, entry in info.items():
                if not entry.get("graph_preserving"):
                    continue
                formulas, kinds = [property_text(query.objective)], ["query"]
                if query.constraint is not None:
                    c = copy.copy(query.constraint)
                    c.op = "=?"
                    formulas += [property_text(c), property_text(query.constraint)]
                    kinds += ["query", "threshold"]
                file = next(n for n in files if n.endswith(f"_{regime}.prism"))
                jobs.append({
                    "file": str(tmp_path / file), "consts": consts,
                    "formulas": formulas, "kinds": kinds, "lifting": entry["multi_affine"],
                    "region": {n: [str(lo), str(hi)] for n, (lo, hi) in cell.box.items()},
                })
                expect.append((cell, regime, entry))
        return jobs, expect

    @pytest.mark.parametrize("map_id", MAP_IDS)
    def test_bounds_and_verdicts(self, computed, map_id, tmp_path):
        pytest.importorskip("stormpy")
        model, result = computed[map_id]
        jobs, expect = self.jobs(model, map_id, result, tmp_path)
        assert jobs
        run = subprocess.run([sys.executable, "-c", STORM_WORKER], input=json.dumps(jobs),
                             capture_output=True, text=True, timeout=900)
        assert run.returncode == 0, run.stderr[-2000:]
        # Storm writes its warnings to the standard output as well
        answers = json.loads(run.stdout.rsplit("\nRESULT ", 1)[1])
        equal = enclosed = verdicts = 0
        for (cell, regime, entry), answer in zip(expect, answers):
            where = (map_id, regime, cell.box)
            mine = [entry["objective"]] + ([entry["constraint"]] if entry["constraint"] else [])
            for (lo, hi), storm in zip(mine, answer["bounds"]):
                assert (lo, hi) == tuple(map(storm_number, storm)), where
                equal += 1
            for (lo, hi), storm in zip(mine, answer["extrema"]):
                s_min, s_max = map(storm_number, storm)
                assert lo <= s_min <= s_max <= hi, where
                enclosed += 1
            for verdict in answer["verdicts"]:
                claimed = {True: "ALL_SAT", False: "ALL_VIOLATED"}.get(entry["holds"])
                if claimed is None:
                    assert verdict not in ("ALL_SAT", "ALL_VIOLATED"), where
                else:
                    assert verdict == claimed, where
                verdicts += 1
        assert enclosed
        print(f"\nstorm {map_id}: {len(jobs)} design-cells, {equal} bound pairs equal, "
              f"{enclosed} extrema enclosed, {verdicts} verdicts equal")
