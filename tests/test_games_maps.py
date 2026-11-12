"""Equilibrium maps of the strategic model extension (Appendix I, I.9):
the maps of I.12, the conditions N, E1 and E2, cells on which nothing is
established, soundness fuzzing against a brute-force oracle with
deliberate soundness bugs, and a comparison of best-response values with
Storm when `stormpy` is installed."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from cadl.cli import main
from cadl.games_eval import (
    EqCell,
    EvaluationError,
    METHOD,
    build_game,
    build_process,
    cell_distributions,
    cell_verdict,
    condition_e1,
    condition_e2,
    condition_n,
    equilibrium_check,
    equilibrium_map,
    face_centres,
    improper_states,
    merge_cells,
    optimal_values,
    player_at_point,
    player_on_cell,
    point_distributions,
    point_reward,
    prepare_map,
    _all,
    _profile,
)
from cadl.parser import parse, parse_file
from cadl.stochastic import checked_model
from cadl.type_checker import type_check

from . import games_fuzz

ROOT = Path(__file__).parent
EXAMPLES = ROOT.parent / "examples"
ROBOT = EXAMPLES / "games_robot_delivery.cadl"
TWO = EXAMPLES / "games_two_players.cadl"
F = Fraction

ROBOT_MODEL = checked_model(parse_file(ROBOT))
TWO_MODEL = checked_model(parse_file(TWO))

LAMBDA_STAR = {"CENTRALIZED": F(480347064349, 3200000000000),
               "DISTRIBUTED": F(193194567781, 3200000000000)}
KAPPA_STAR = F(368638137651, 3200000000000)


def model_from(text: str):
    sos = parse(text)
    result = type_check(sos)
    assert result.ok, [str(e) for e in result.errors]
    return checked_model(sos)


def cells_of(result: dict, design: str) -> list:
    d = next(d for d in result["designs"] if d["design"] == design)
    return [(tuple(c.box.values()), c.status, c.deviators, c.from_every_state, c.reason)
            for c in d["cells"]]


def lambda_star(s: Fraction) -> Fraction:
    """λ* = max(0, T_1, T_2, T_3) of I.12."""
    g, c, m = 10, 1, 2
    v = [F(0)]
    for _ in range(3):
        v.append(s * g - c + (1 - s) * v[-1])
    return max([F(0)] + [(c - (s / 2) * (g - v[k])) / m for k in range(3)])


def s_of(design: str, p_fail: Fraction) -> Fraction:
    beta = F(9, 10) if design == "CENTRALIZED" else F(1, 10)
    return beta * F(9, 10) * (1 - p_fail) * F(9, 10) + (1 - beta) * F(6, 10)


# ---------------------------------------------------------------------------
# The conditions
# ---------------------------------------------------------------------------

class TestConditions:
    def test_n(self):
        assert condition_n(F(3), F(2), F(0)) and not condition_n(F(2), F(2), F(0))
        assert not condition_n(F(3), F(2), F(1)) and condition_n(F(3), F(2), F(1, 2))

    def test_e1(self):
        assert condition_e1(F(2), F(2), F(0)) and not condition_e1(F(3), F(2), F(0))
        assert condition_e1(F(3), F(2), F(1))

    def test_e2(self):
        assert condition_e2([]) and condition_e2([(F(1), F(1)), (F(0), F(2))])
        assert not condition_e2([(F(1), F(1)), (F(3), F(2))])

    def test_face_centres(self):
        assert face_centres({"a": (F(0), F(1))}) == [(0,), (F(1, 2),), (1,)]
        assert len(face_centres({"a": (F(0), F(1)), "b": (F(0), F(2)), "c": (F(1), F(2))})) == 27

    def test_merge(self):
        def cell(lo, hi, status, **kw):
            return EqCell(box={"x": (F(lo), F(hi))}, status=status, **kw)
        merged = merge_cells([cell(2, 3, "equilibrium"), cell(0, 1, "undecided", reason="bounds"),
                              cell(1, 2, "equilibrium"), cell(3, 4, "equilibrium",
                                                              from_every_state=True)], ["x"])
        assert [(c.box["x"], c.status) for c in merged] == [
            ((0, 1), "undecided"), ((1, 3), "equilibrium"), ((3, 4), "equilibrium")]

    def test_merge_in_two_dimensions_keeps_boxes(self):
        def cell(x, y, status):
            return EqCell(box={"x": (F(x), F(x + 1)), "y": (F(y), F(y + 1))}, status=status)
        cells = [cell(0, 0, "equilibrium"), cell(1, 0, "equilibrium"), cell(0, 1, "equilibrium"),
                 cell(1, 1, "undecided")]
        merged = merge_cells(cells, ["x", "y"])
        assert sum((c.box["x"][1] - c.box["x"][0]) * (c.box["y"][1] - c.box["y"][0])
                   for c in merged) == 4
        assert len(merged) == 3


# ---------------------------------------------------------------------------
# The maps of I.12
# ---------------------------------------------------------------------------

class TestMapOverLambda:
    def test_cells(self):
        result = equilibrium_map(ROBOT_MODEL, "comply_by_lambda")
        assert cells_of(result, "CENTRALIZED") == [
            (((0, F(1, 8)),), "not_equilibrium", ["R"], None, None),
            (((F(1, 8), F(5, 32)),), "undecided", [], None, "bounds"),
            (((F(5, 32), 1),), "equilibrium", [], True, None),
        ]
        assert cells_of(result, "DISTRIBUTED") == [
            (((0, F(1, 32)),), "not_equilibrium", ["R"], None, None),
            (((F(1, 32), F(1, 16)),), "undecided", [], None, "bounds"),
            (((F(1, 16), 1),), "equilibrium", [], True, None),
        ]
        for d in result["designs"]:
            assert d["undecided_fraction"] == F(1, 32) and d["resolution_reached"]
            # The bounds of I.9, and the end points of I.9 for the cells they
            # leave undecided (one open parameter, only in weights).
            assert d["method"].startswith(METHOD) and "end points (I.9)" in d["method"]
            assert "lambda" not in d["parameters"]
            assert d["parameters"]["p_fail"] == F(1, 20)
            star = LAMBDA_STAR[d["design"]]
            undecided = next(c for c in d["cells"] if c.status == "undecided")
            assert undecided.box["lambda"][0] < star < undecided.box["lambda"][1]

    def test_deviation_and_gain_bounds(self):
        result = equilibrium_map(ROBOT_MODEL, "comply_by_lambda", "CENTRALIZED")
        cell = result["designs"][0]["cells"][0]
        assert cell.deviations == {"R": [
            {"state": {"task": "Pending", "attempts": 0, "settled": False}, "action": "careless"}]}
        lo, hi = cell.gain_bounds["R"]
        star = LAMBDA_STAR["CENTRALIZED"]
        # gain_R = 2 (λ* − λ): between its values at the two ends.
        assert lo == 2 * (star - F(1, 8)) and hi == 2 * star
        assert cell.gain_bounds["D"] == (0, 0)

    def test_e1_alone_decides_the_equilibrium_cells_here(self):
        """U_R is constant in λ, so E1 holds on [5/32, 1] (I.12)."""
        design = ROBOT_MODEL.design("CENTRALIZED")
        game = prepare_map(ROBOT_MODEL, ROBOT_MODEL.equilibria[2], design, None)
        cell = ((F(5, 32), F(1)),)
        proc = build_process(game, "R", "comply", cell_distributions(game, cell))
        from cadl.games_eval import reward_bounds
        lo, hi = reward_bounds(game, proc, cell)
        b = optimal_values(proc, _all(proc), hi)[0]
        l = optimal_values(proc, _profile(proc), lo, maximize=False)[0]
        assert b == l == F(6831840915651, 800000000000) and condition_e1(b, l, F(0))
        on = player_on_cell(game, "comply", "R", cell, cell_distributions(game, cell), F(0))
        assert on.e2

    def test_command(self, capsys):
        status = main(["game-check", str(ROBOT), "--certify", "--equilibrium",
                       "comply_by_lambda", "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert status == 0 and data["checks"] == [] and data["maps_not_computed"] == []
        result = data["maps"][0]
        assert {k: result[k] for k in ("id", "profile", "epsilon", "over", "resolution")} == {
            "id": "comply_by_lambda", "profile": "comply", "epsilon": "0",
            "over": {"lambda": ["0", "1"]}, "resolution": "1/20"}
        central = result["designs"][0]
        assert central["design"] == "CENTRALIZED" and central["method"].startswith(METHOD)
        assert central["undecided_fraction"] == "1/32" and central["resolution_reached"] is True
        assert central["parameters"] == {"p_fail": "1/20", "p_loss": "1/10", "alpha": "9/10",
                                         "beta": "9/10", "q_local": "3/5"}
        cells = central["cells"]
        assert [(c["box"], c["status"]) for c in cells] == [
            ({"lambda": ["0", "1/8"]}, "not_equilibrium"),
            ({"lambda": ["1/8", "5/32"]}, "undecided"),
            ({"lambda": ["5/32", "1"]}, "equilibrium")]
        assert cells[0]["deviators"] == ["R"] and cells[1]["reason"] == "bounds"
        assert cells[2]["from_every_state"] is True
        assert cells[0]["gain_bounds"]["D"] == ["0", "0"]

    def test_text(self, capsys):
        status = main(["game-check", str(ROBOT), "--certify", "--equilibrium",
                       "comply_by_lambda", "--regime", "DISTRIBUTED"])
        out = capsys.readouterr().out
        assert status == 0
        assert "lambda in [0, 0.03125]: not_equilibrium, deviators: R" in out
        assert "lambda in [0.03125, 0.0625]: undecided (bounds)" in out
        assert "lambda in [0.0625, 1]: equilibrium, from_every_state: true" in out
        assert "undecided fraction: 0.03125 (exact: 1/32)" in out
        assert "The resolution is reached." in out and "CENTRALIZED" not in out

    def test_exit_status_1_when_the_resolution_is_not_reached(self, capsys):
        status = main(["game-check", str(ROBOT), "--certify", "--equilibrium",
                       "comply_by_lambda", "--max-cells", "2"])
        out = capsys.readouterr().out
        assert status == 1 and "The resolution is NOT reached" in out
        status = main(["game-check", str(ROBOT), "--certify", "--equilibrium",
                       "comply_by_lambda", "--max-depth", "1", "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert status == 1 and data["maps"][0]["designs"][0]["stopped"] == "max_depth"

    def test_set_moves_the_threshold(self):
        """With p_fail = 0.3 the centralised regime needs λ >= 0.032907."""
        result = equilibrium_map(ROBOT_MODEL, "comply_by_lambda", "CENTRALIZED",
                                 {"p_fail": F(3, 10)})
        d = result["designs"][0]
        assert d["parameters"]["p_fail"] == F(3, 10)
        star = lambda_star(s_of("CENTRALIZED", F(3, 10)))
        assert round(star, 6) == F("0.032907")
        for c in d["cells"]:
            lo, hi = c.box["lambda"]
            assert c.status == ("not_equilibrium" if hi < star else
                                "equilibrium" if lo >= star else "undecided")


def check_against_closed_form(design: dict) -> dict:
    """Every decided cell of the map over λ and p_fail against λ*(s):
    T_3 increases with s on the region, so the extreme values of λ* on a
    cell are at the two ends of p_fail."""
    volumes = {"equilibrium": F(0), "not_equilibrium": F(0), "undecided": F(0)}
    for c in design["cells"]:
        (l0, l1), (p0, p1) = c.box["lambda"], c.box["p_fail"]
        stars = [lambda_star(s_of(design["design"], p)) for p in (p0, p1)]
        volumes[c.status] += (l1 - l0) * (p1 - p0)
        if c.status == "equilibrium":
            assert l0 >= max(stars) and c.from_every_state is True
        elif c.status == "not_equilibrium":
            assert l1 < min(stars) and c.deviators == ["R"]
        else:
            assert c.reason == "bounds"
    return {k: v / F(1, 5) for k, v in volumes.items()}


def design_map_cells(game, query) -> list:
    """The equilibrium cells of a map as analysed (not merged), as tuples."""
    from cadl.games_eval import design_map
    d = design_map(game, query, merge=False)
    return [tuple(c.box[n] for n in game.names) for c in d["cells"]
            if c.status == "equilibrium"]


class TestMapOverLambdaAndFailure:
    def test_distributed(self):
        result = equilibrium_map(ROBOT_MODEL, "comply_by_lambda_and_failure", "DISTRIBUTED")
        d = result["designs"][0]
        assert d["resolution_reached"] and d["undecided_fraction"] <= F(1, 20)
        assert d["multi_affine"]
        share = check_against_closed_form(d)
        assert share["undecided"] == d["undecided_fraction"]
        assert share["equilibrium"] > F(4, 5) and share["not_equilibrium"] > F(1, 12)

    def test_centralized_within_a_budget(self):
        result = equilibrium_map(ROBOT_MODEL, "comply_by_lambda_and_failure", "CENTRALIZED",
                                 max_cells=150)
        d = result["designs"][0]
        assert not d["resolution_reached"] and d["stopped"] == "max_cells"
        share = check_against_closed_form(d)
        assert share["equilibrium"] > F(1, 3) and share["not_equilibrium"] > F(1, 20)

    def test_centralized_reaches_the_resolution(self):
        result = equilibrium_map(ROBOT_MODEL, "comply_by_lambda_and_failure", "CENTRALIZED")
        d = result["designs"][0]
        assert d["resolution_reached"]
        share = check_against_closed_form(d)
        # I.14: 0.8118 of the region certified equilibrium, 0.1387
        # not_equilibrium, 0.0496 undecided (203/4096), 567 cells analysed.
        assert [round(share[k], 4) for k in ("equilibrium", "not_equilibrium", "undecided")] \
            == [F("0.8118"), F("0.1387"), F("0.0496")]
        assert d["undecided_fraction"] == F(203, 4096) and d["cells_analysed"] == 567

    @pytest.mark.skipif(not os.environ.get("CADL_GAMES_SLOW"),
                        reason="about 6 s; set CADL_GAMES_SLOW=1")
    def test_centralized_without_the_advantages(self, monkeypatch):
        # The map of cadl 0.3.26, without PD+ and PD−.
        import cadl.games_eval as ge
        monkeypatch.setattr(ge, "USE_ADVANTAGES", False)
        result = equilibrium_map(ROBOT_MODEL, "comply_by_lambda_and_failure", "CENTRALIZED")
        d = result["designs"][0]
        share = check_against_closed_form(d)
        assert [round(share[k], 4) for k in ("equilibrium", "not_equilibrium", "undecided")] \
            == [F("0.8088"), F("0.1412"), F("0.0499")]
        assert d["undecided_fraction"] == F(409, 8192) and d["cells_analysed"] == 3849

    def test_e1_decides_no_equilibrium_cell_here(self):
        """U_R varies with p_fail: E1 fails and E2 holds (I.12)."""
        from cadl.games_eval import reward_bounds
        design = ROBOT_MODEL.design("DISTRIBUTED")
        query = ROBOT_MODEL.equilibria[3]
        game = prepare_map(ROBOT_MODEL, query, design, None)
        cells = design_map_cells(game, query)
        assert cells
        for cell in cells:
            on = player_on_cell(game, "comply", "R", cell, cell_distributions(game, cell), F(0))
            assert on.e2 and not on.e1 and not on.n
            proc = build_process(game, "R", "comply", cell_distributions(game, cell))
            lo, hi = reward_bounds(game, proc, cell)
            b = optimal_values(proc, _all(proc), hi)[0]
            l = optimal_values(proc, _profile(proc), lo, maximize=False)[0]
            assert not condition_e1(b, l, F(0))


TABLE = """
0.3 | R,D | D   | D   | D
0.2 | R,D | D   | D   | D
0.1 | R   | und | und | und
0   | R   | und | eq  | eq
"""


class TestSecondExampleMap:
    def table(self) -> dict:
        want = {}
        for line in TABLE.strip().splitlines():
            row = [x.strip() for x in line.split("|")]
            for i, entry in enumerate(row[1:]):
                want[(F(i, 10), F(row[0]))] = entry
        return want

    def test_the_16_cells_of_the_table(self):
        result = equilibrium_map(TWO_MODEL, "watched_map", partition=4, merge=False)
        d = result["designs"][0]
        assert d["design"] == "CENTRALIZED" and len(d["cells"]) == 16
        assert d["cells_analysed"] == 16
        assert d["undecided_fraction"] == F(1, 4) and d["resolution_reached"]
        want = self.table()
        for c in d["cells"]:
            entry = want[(c.box["lambda"][0], c.box["kappa"][0])]
            if entry == "eq":
                assert (c.status, c.from_every_state) == ("equilibrium", True)
            elif entry == "und":
                assert (c.status, c.reason) == ("undecided", "bounds")
            else:
                assert (c.status, c.deviators) == ("not_equilibrium", entry.split(","))
        watched = {"watch": "On", "task": "Pending", "attempts": 0, "settled": False}
        first = d["cells"][3]                 # λ in [0, 0.1], κ in [0.3, 0.4]
        assert first.deviations == {
            "R": [{"state": watched, "action": "careless"}],
            "D": [{"state": dict(watched, watch="Open"), "action": "trust"}]}

    def test_e2_for_d_and_e1_for_r(self):
        """In the cell λ in [0.2, 0.3], κ in [0, 0.1]: E2 holds for both
        players; E1 holds for R and fails for D (I.12)."""
        design = TWO_MODEL.design("CENTRALIZED")
        game = prepare_map(TWO_MODEL, TWO_MODEL.equilibria[2], design, None)
        cell = ((F(1, 5), F(3, 10)), (F(0), F(1, 10)))
        from cadl.games_eval import reward_bounds
        got = {}
        for p in ("R", "D"):
            dists = cell_distributions(game, cell)
            on = player_on_cell(game, "comply_if_watched", p, cell, dists, F(0))
            proc = build_process(game, p, "comply_if_watched", dists)
            lo, hi = reward_bounds(game, proc, cell)
            b = optimal_values(proc, _all(proc), hi)[0]
            l = optimal_values(proc, _profile(proc), lo, maximize=False)[0]
            got[p] = (on.e2, condition_e1(b, l, F(0)), l)
        assert got["R"][:2] == (True, True) and got["D"][:2] == (True, False)
        assert round(got["D"][2], 6) == F("4.824241")       # the profile at κ = 0.1

    def test_by_bisection(self):
        """The map as the command computes it: the same claims, on cells
        that are not those of the table."""
        result = equilibrium_map(TWO_MODEL, "watched_map")
        d = result["designs"][0]
        assert d["undecided_fraction"] == F(1, 4) and d["resolution_reached"]
        want = self.table()
        star = LAMBDA_STAR["CENTRALIZED"]
        for (l0, k0), entry in want.items():
            c = next(c for c in d["cells"]
                     if c.box["lambda"][0] <= l0 and l0 + F(1, 10) <= c.box["lambda"][1]
                     and c.box["kappa"][0] <= k0 and k0 + F(1, 10) <= c.box["kappa"][1])
            status = {"eq": "equilibrium", "und": "undecided"}.get(entry, "not_equilibrium")
            assert c.status == status
            # A coarser cell lists the deviators it establishes: fewer or the same.
            assert set(c.deviators) <= set(entry.split(","))
            if "R" in c.deviators:
                assert c.box["lambda"][1] < star
            if "D" in c.deviators:
                assert c.box["kappa"][0] > KAPPA_STAR

    def test_command(self, capsys):
        status = main(["game-check", str(TWO), "--certify", "--partition", "4", "--no-merge"])
        out = capsys.readouterr().out
        assert status == 0
        assert "lambda in [0, 0.1], kappa in [0.2, 0.3]: not_equilibrium, deviators: R, D" in out
        assert "lambda in [0.1, 0.2], kappa in [0, 0.1]: undecided (bounds)" in out
        assert "lambda in [0.3, 0.4], kappa in [0, 0.1]: equilibrium, from_every_state: true" in out
        assert "cells: 16 (2 equilibrium, 10 not_equilibrium, 4 undecided)" in out


# ---------------------------------------------------------------------------
# Cells on which nothing is established
# ---------------------------------------------------------------------------

CHAIN = """
sos:
  name: "Chain"
  type: Acknowledged
  extensions: [ { stochastic: 0.1 }, { games: 0.1 } ]
  actors: [ { id: ACT, role: "x" } ]
  model:
    kind: smg
    parameters:
      - { id: p, range: [0, 1], value: 0.5 }
    variables: [ { id: loc, type: "{S0, S1, Goal}", init: S0 } ]
    labels: { goal: "loc == Goal" }
    players: [ { id: A, actors: [ACT] } ]
    commands:
      - action: fast
        owner: A
        guard: "loc == S0"
        updates: [ { prob: "PROB", set: { loc: S1 } }, { prob: rest, set: { loc: Goal } } ]
      - action: slow
        owner: A
        guard: "loc == S0"
        updates: [ { prob: "PROB", set: { loc: S1 } }, { prob: rest, set: { loc: Goal } } ]
      - action: retry
        guard: "loc == S1"
        updates: [ { prob: "PROB", set: { loc: Goal } }, { prob: rest, set: { loc: S1 } } ]
    rewards:
      - { id: cost, items: [ { action: slow, value: 1 } ] }
    profiles: { sigma: [ { choose: fast } ] }
    compliance: sigma
    payoffs: [ { player: A, until: "goal", terms: [ { reward: cost, weight: -1 } ] } ]
    equilibria: [ { id: m, over: { p: [0, 1] }, resolution: 0.1 } ]
"""


class TestUndecided:
    def test_improper_relaxation_is_undecided_with_reason_bounds(self):
        """S0 goes to S1 with probability p and S1 leaves with
        probability p. At p = 0, S1 is not reachable, so the profile is
        admissible on the whole region [0, 1] (the face centres show it),
        but on every cell that contains p = 0 the relaxed process has a
        policy that stays in S1: such a cell is undecided, reason
        `bounds`; it is not an error and it is never decided."""
        model = model_from(CHAIN.replace("PROB", "p"))
        result = equilibrium_map(model, "m")
        d = result["designs"][0]
        assert d["multi_affine"] and d["resolution_reached"]
        cells = cells_of(result, "base")
        assert cells[0][1:] == ("undecided", [], None, "bounds") and cells[0][0][0][0] == 0
        assert all(c[1] == "equilibrium" and c[3] is True for c in cells[1:])
        assert len(cells) == 2 and cells[0][0][0][1] == cells[1][0][0][0] <= F(1, 10)
        # The cell [0, x] stays undecided however small it is.
        game = prepare_map(model, model.equilibria[0], model.designs[0], None)
        cell = EqCell(box={"p": (F(0), F(1, 10 ** 6))})
        cell_verdict(game, model.equilibria[0], cell)
        assert (cell.status, cell.reason) == ("undecided", "bounds")
        proc = build_process(game, "A", "sigma",
                             cell_distributions(game, ((F(0), F(1, 10 ** 6)),)))
        assert improper_states(proc)
        # At p = 0 itself the profile is admissible and an equilibrium.
        assert player_at_point(game, "sigma", "A", (F(0),)).admissible

    def test_not_admissible_is_a_reason_only_without_multi_affinity(self):
        model = model_from(CHAIN.replace("PROB", "p * p"))
        d = equilibrium_map(model, "m")["designs"][0]
        assert not d["multi_affine"]
        first = d["cells"][0]
        assert first.box["p"][0] == 0 and (first.status, first.reason) == (
            "undecided", "not_admissible")
        assert all(c.status == "equilibrium" for c in d["cells"][1:])

    def test_not_well_defined_is_a_reason_when_intervals_do_not_show_it(self):
        """p (1 − p) lies in [0, 1/4], but interval arithmetic on a cell
        that contains 0 gives a negative lower end."""
        model = model_from(CHAIN.replace("PROB", "p * (1 - p)")
                           .replace("over: { p: [0, 1] }", "over: { p: [0, 0.5] }"))
        d = equilibrium_map(model, "m")["designs"][0]
        reasons = {c.reason for c in d["cells"] if c.status == "undecided"}
        assert "not_well_defined" in reasons
        assert d["cells"][0].reason == "not_well_defined" and d["cells"][0].box["p"][0] == 0
        assert any(c.status == "equilibrium" for c in d["cells"])

    def test_inadmissible_face_centre_is_an_error_of_the_command(self):
        """--set moves the model to values at which a face centre fails
        (I.13): exit status 2 material, not an undecided cell."""
        text = CHAIN.replace("PROB", "p").replace(
            "- { id: p, range: [0, 1], value: 0.5 }",
            "- { id: p, range: [0, 1], value: 0.5 }\n      - { id: r, range: [0, 1], value: 0.5 }")
        text = text.replace('updates: [ { prob: "p", set: { loc: Goal } }, { prob: rest, set: { loc: S1 } } ]',
                            'updates: [ { prob: "r", set: { loc: Goal } }, { prob: rest, set: { loc: S1 } } ]')
        model = model_from(text)
        assert equilibrium_map(model, "m")["designs"][0]["resolution_reached"]
        with pytest.raises(EvaluationError, match="not admissible"):
            equilibrium_map(model, "m", overrides={"r": F(0)})
        with pytest.raises(EvaluationError, match="open in the equilibrium map"):
            equilibrium_map(model, "m", overrides={"p": F(1, 2)})

    def test_epsilon_on_a_map(self):
        """With ε = 0.25 compliance is an ε-equilibrium from λ* − 0.125 on."""
        data = yaml.safe_load(ROBOT.read_text(encoding="utf-8"))
        data["sos"]["model"]["equilibria"][2]["epsilon"] = 0.25
        model = model_from(yaml.safe_dump(data, sort_keys=False))
        d = equilibrium_map(model, "comply_by_lambda", "CENTRALIZED")["designs"][0]
        star = LAMBDA_STAR["CENTRALIZED"] - F(1, 8)
        assert d["resolution_reached"]
        for c in d["cells"]:
            lo, hi = c.box["lambda"]
            assert c.status == ("not_equilibrium" if hi < star else
                                "equilibrium" if lo >= star else "undecided")
        # E2 fails where a one-step deviation gains a little; E1 decides.
        assert any(c.status == "equilibrium" and c.from_every_state is None for c in d["cells"])


# ---------------------------------------------------------------------------
# Soundness fuzzing (tests/games_fuzz.py)
# ---------------------------------------------------------------------------

class TestFuzz:
    def test_generated_file_is_a_strategic_model(self):
        g = games_fuzz.generate(random.Random(5))
        sos = parse(games_fuzz.to_cadl(g))
        assert ("games", "0.1") in sos.extensions and sos.model.kind == "smg"

    def test_oracle_on_the_tiny_cycle(self):
        """The oracle itself, on a game solved by hand: from state 0 the
        player pays 1 per step and leaves with probability 1/2."""
        g = {"n": 1, "players": ["A"],
             "states": [{"owner": "A", "cmds": [
                 {"action": "a0_0", "updates": [(("0.5", lambda v: F(1, 2)), 1), (None, 0)]},
                 {"action": "a0_1", "updates": [(None, 1)]}]}],
             "rewards": [{"id": "r0", "items": [{"action": "a0_0", "loc": None, "value": 1}]}],
             "payoffs": {"A": {"target": {1}, "terms": [("r0", ("-1", lambda v: F(-1)))]}},
             "sigma": [0], "region": {}, "fixed": {}, "epsilon": F(0)}
        a = games_fuzz.Oracle(g, {}).analyse("A")
        assert (a["payoff"], a["best"], a["gain"], a["gain_somewhere"]) == (-2, 0, 2, True)

    @pytest.mark.parametrize("seed", [11, 12])
    def test_decided_cells_hold_at_every_sampled_point(self, seed):
        stats = games_fuzz.run(seed, 30)
        assert "failure" not in stats, stats["failure"] + "\n" + stats.get("failing_model", "")
        assert stats["models"] == 30
        assert stats["cells_equilibrium"] > 0 and stats["cells_not_equilibrium"] > 0
        assert stats["points"] > 500

    @pytest.mark.parametrize("name", games_fuzz.MUTATIONS)
    def test_a_deliberate_soundness_bug_is_detected(self, name):
        stats = games_fuzz.run(1, 60, name)
        assert "failure" in stats, f"the mutant {name} survived {stats}"

    def test_mutation_is_removed_afterwards(self):
        import cadl.games_eval as ge
        before = ge.deviation_lower
        with games_fuzz.mutation("swap_bound_direction"):
            assert ge.deviation_lower is not before
        assert ge.deviation_lower is before


# ---------------------------------------------------------------------------
# Comparison with Storm
# ---------------------------------------------------------------------------

# Run in a process of its own: stormpy aborts the interpreter on some
# inputs it does not support.
STORM_WORKER = r"""
import json, sys
import stormpy

out = []
for job in json.load(sys.stdin):
    program = stormpy.parse_prism_program(job["file"])
    props = stormpy.parse_properties('R{"rho"}max=? [ F "target" ]', program)
    model = stormpy.build_model[stormpy.Rational](program, props)
    result = stormpy.model_checking(model, props[0])
    out.append(str(result.get_value(model.initial_states[0])))
print("\nRESULT " + json.dumps(out))
"""


def process_as_prism(proc, reward, choice) -> str:
    """The game of one player against the profile at a valuation, as a
    Markov decision process in the PRISM language, with exact rational
    probabilities and rewards. `choice(i)` lists the commands of a state."""
    n = len(proc.states)
    lines = ["mdp", "", "module game", f"  s : [0..{max(n - 1, 1)}] init 0;"]
    rewards = []
    for i in range(n):
        if proc.target[i]:
            lines.append(f"  [] s={i} -> true;")
            continue
        for k, ds in proc.options[i]:
            if k not in choice(i):
                continue
            update = " + ".join(f"{p} : (s'={j})" for j, p in ds[0].items())
            lines.append(f"  [c{i}_{k}] s={i} -> {update};")
            rewards.append(f"  [c{i}_{k}] s={i} : ({reward(i, k)});")
    targets = " | ".join(f"s={i}" for i in range(n) if proc.target[i]) or "false"
    lines += ["endmodule", "", f'label "target" = {targets};', "", 'rewards "rho"']
    lines += rewards or ["  true : 0;"]
    lines += ["endrewards", ""]
    return "\n".join(lines)


class TestAgainstStorm:
    """For fixed parameter values, the best-response value of a player
    against the fixed strategies of the others is the optimal expected
    total reward of a Markov decision process (I.7). Storm (stormpy)
    computes it with exact rational arithmetic on a PRISM file written
    here from the game; the payoff under the profile is the same with the
    player's commands fixed. Rewards have either sign."""

    def jobs(self, tmp_path) -> list:
        jobs = []

        def add(name, model, design, profile):
            game = build_game(model, design, [], model.fixed_values(design))
            for player in model.players:
                at = player_at_point(game, profile, player.id, ())
                assert at.admissible
                proc = build_process(game, player.id, profile, point_distributions(game, ()))
                reward = point_reward(game, proc, ())
                for kind, choice, want in (("br", _all(proc), at.best_response_value),
                                           ("u", _profile(proc), at.payoff)):
                    path = tmp_path / f"{name}_{design.name}_{profile}_{player.id}_{kind}.prism"
                    path.write_text(process_as_prism(proc, reward, choice), encoding="utf-8")
                    jobs.append({"file": str(path), "want": str(want)})

        for name, model in (("robot", ROBOT_MODEL), ("two", TWO_MODEL)):
            for design in model.designs:
                for profile in model.profiles:
                    add(name, model, design, profile.id)
        rng = random.Random(21)
        made = 0
        while made < 12:
            g = games_fuzz.generate(rng)
            sos = parse(games_fuzz.to_cadl(g))
            if not type_check(sos).ok:
                continue
            model = checked_model(sos)
            add(f"fuzz{made}", model, model.designs[0], "sigma")
            made += 1
        return jobs

    def test_best_response_values(self, tmp_path):
        pytest.importorskip("stormpy")
        jobs = self.jobs(tmp_path)
        assert len(jobs) >= 80
        proc = subprocess.run([sys.executable, "-c", STORM_WORKER], input=json.dumps(jobs),
                              capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0, proc.stderr[-2000:]
        got = json.loads(proc.stdout.rsplit("RESULT ", 1)[1])
        assert len(got) == len(jobs)
        for job, value in zip(jobs, got):
            assert Fraction(value) == Fraction(job["want"]), job["file"]
        # Not every value is trivial: some best responses differ from the payoff.
        assert sum(1 for a, b in zip(jobs[::2], jobs[1::2]) if a["want"] != b["want"]) >= 5
