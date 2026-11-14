"""The link between the strategic model (Appendix I) and the grounding
extension (Appendix J): payoff terms `sanction_of:` / `reward_of:` (J.7),
the rules J-10, J-11 and J-12, the assumption that every result carries,
the end-point certification of J-14 (with soundness fuzzing and
deliberate soundness bugs), and the keywords of PRISM-games in the export
of a strategic model (I.11)."""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from cadl.cli import main
from cadl.games import game_to_ir, term_weight
from cadl.games_eval import (
    EqCell,
    J14_METHOD,
    build_game,
    endpoint_verdict,
    equilibrium_check,
    equilibrium_map,
    endpoint_setup,
    prepare_map,
    syntactic_degree,
)
from cadl.parser import parse, parse_file
from cadl.stochastic import ExportError, checked_model, export_prism, parse_param_expr
from cadl.type_checker import type_check

from . import games_fuzz

ROOT = Path(__file__).parent
EXAMPLES = ROOT.parent / "examples"
LINKED = EXAMPLES / "games_grounding_robot_delivery.cadl"
PLAIN = EXAMPLES / "games_robot_delivery.cadl"
F = Fraction
LAMBDA_STAR = {"CENTRALIZED": F(480347064349, 3200000000000),
               "DISTRIBUTED": F(193194567781, 3200000000000)}
ASSUMPTION = ("the reward structure shortcuts is taken to count the violations of "
              "DELIVERY.no_shortcut")


def linked() -> dict:
    return yaml.safe_load(LINKED.read_text(encoding="utf-8"))


def build(mutate=None) -> str:
    data = linked()
    if mutate is not None:
        mutate(data["sos"]["model"], data["sos"])
    return yaml.safe_dump(data, sort_keys=False)


def check(mutate=None):
    return type_check(parse(build(mutate)))


def with_rule(items, rule: str) -> list:
    return [str(i) for i in items if f"({rule})" in str(i)]


def model_of(mutate=None):
    sos = parse(build(mutate))
    result = type_check(sos)
    assert result.ok, [str(e) for e in result.errors]
    return checked_model(sos)


def r_terms(model):
    return model["payoffs"][0]["terms"]


def norm(sos):
    return sos["contracts"][0]["norms"][0]


def cli(capsys, *args):
    status = main([str(a) for a in args])
    out = capsys.readouterr()
    return status, out.out, out.err


LINKED_MODEL = model_of()
PLAIN_MODEL = checked_model(parse_file(PLAIN))


# ---------------------------------------------------------------------------
# The example of J.7
# ---------------------------------------------------------------------------

class TestExample:
    def test_check(self):
        result = type_check(parse_file(LINKED))
        assert result.ok and not result.warnings
        infos = with_rule(result.infos, "J-12")
        assert len(infos) == 2 and all("is 0" in i and "condition (ii)" in i for i in infos)

    def test_the_weight_is_minus_magnitude_times_lambda(self):
        payoff = LINKED_MODEL.payoffs[0]
        term = payoff.terms[2]
        assert term.weight_text == "-2 * lambda"
        assert term_weight(LINKED_MODEL, payoff, term) == parse_param_expr("-2 * lambda")
        assert term.link.magnitude == 2 and term.link.lambda_source == "DELIVERY.lambda"

    @pytest.mark.parametrize("query", ["comply_nash", "shortcut_nash"])
    def test_same_results_as_the_written_weight(self, query):
        a = equilibrium_check(LINKED_MODEL, query)
        b = equilibrium_check(PLAIN_MODEL, query)
        assert a["designs"] == b["designs"]
        assert a["assumptions"] == [ASSUMPTION] and b["assumptions"] == []

    def test_same_maps_as_the_written_weight(self):
        a = equilibrium_map(LINKED_MODEL, "comply_by_lambda")
        b = equilibrium_map(PLAIN_MODEL, "comply_by_lambda")
        for da, db in zip(a["designs"], b["designs"]):
            assert [c.claim() for c in da["cells"]] == [c.claim() for c in db["cells"]]
            assert [c.box for c in da["cells"]] == [c.box for c in db["cells"]]
            # The end-point certification of I.9 is used in both files.
            assert da["endpoints"]["applicable"] and db["endpoints"]["applicable"]
        assert a["assumptions"] == [ASSUMPTION]

    @pytest.mark.parametrize("design, least", [
        ("CENTRALIZED", F(480347064349, 160000000000)),
        ("DISTRIBUTED", F(193194567781, 160000000000)),
    ])
    def test_least_magnitude_at_the_declared_lambda(self, design, least):
        """J.7: at λ = 0.1 compliance is an equilibrium exactly when the
        magnitude is at least 20·λ*."""
        assert least == 20 * LAMBDA_STAR[design]
        for magnitude, status in ((least, "equilibrium"),
                                  (least - F(1, 10 ** 14), "not_equilibrium"),
                                  (least + F(1, 10 ** 14), "equilibrium")):
            text = format(float(magnitude), ".15g")
            assert F(text) == magnitude          # the decimal is exact here

            def mutate(model, sos, text=text):
                norm(sos)["on_violation"]["magnitude"] = float(text)
            result = equilibrium_check(model_of(mutate), "comply_nash", design)
            assert result["designs"][0]["status"] == status, (magnitude, design)
        def three(model, sos):
            norm(sos)["on_violation"]["magnitude"] = 3.002
        statuses = [d["status"] for d in equilibrium_check(model_of(three), "comply_nash")["designs"]]
        assert statuses == ["not_equilibrium", "equilibrium"]   # "3.002 is not enough"

    def test_command_text_and_json(self, capsys):
        status, out, _ = cli(capsys, "game-check", LINKED, "--certify", "--equilibrium",
                             "comply_nash", "--equilibrium", "comply_by_lambda")
        assert status == 0
        assert out.count(f"assumption (J.7): {ASSUMPTION}") == 2
        assert ("end points (I.9): condition A holds for 'lambda'; the gain is non-increasing "
                "for: R, D; non-decreasing for: D") in out
        status, out, _ = cli(capsys, "game-check", LINKED, "--certify", "--format", "json",
                             "--equilibrium", "shortcut_nash", "--equilibrium",
                             "comply_by_lambda")
        data = json.loads(out)
        assert status == 0
        for result in data["checks"] + data["maps"]:
            assert result["assumptions"] == [ASSUMPTION]
        assert data["maps"][0]["designs"][0]["endpoints"] == {
            "applicable": True, "parameter": "lambda", "non_increasing": ["R", "D"],
            "non_decreasing": ["D"]}

    def test_j14_does_not_apply_to_the_map_of_two_parameters(self):
        _, _, j14 = game_for(LINKED_MODEL, "comply_by_lambda_and_failure", "CENTRALIZED")
        assert j14 == {"applicable": False, "reason": "the map has more than one open "
                       "parameter", "decreasing": [], "increasing": []}

    def test_ir(self, capsys):
        status, out, _ = cli(capsys, "sim-ir", LINKED, "--format", "json")
        terms = json.loads(out)["stochastic_model"]["payoffs"][0]["terms"]
        assert terms[2] == {"reward": "shortcuts", "weight": "-2 * lambda",
                            "sanction_of": "DELIVERY.no_shortcut", "reward_of": None}
        assert terms[0] == {"reward": "fee", "weight": "10", "sanction_of": None,
                            "reward_of": None}

    def test_reward_of_has_a_positive_weight(self):
        def mutate(model, sos):
            norm(sos)["on_fulfilment"] = {"reward": "bonus", "magnitude": 0.5}
            r_terms(model).append({"reward": "fee_bonus", "reward_of": "DELIVERY.no_shortcut"})
            model["rewards"].append({"id": "fee_bonus",
                                     "items": [{"action": "settle", "guard": "task == Delivered",
                                                "value": 1}]})
        m = model_of(mutate)
        term = m.payoffs[0].terms[3]
        assert term.weight_text == "0.5 * lambda"
        assert term.assumption() == ("the reward structure fee_bonus is taken to count the "
                                     "fulfilments of DELIVERY.no_shortcut")
        assert equilibrium_check(m, "comply_nash")["assumptions"] == [
            ASSUMPTION, term.assumption()]


# ---------------------------------------------------------------------------
# J-10 (errors)
# ---------------------------------------------------------------------------

class TestJ10:
    def test_passes(self):
        assert not with_rule(check().errors, "J-10")

    def test_at_most_one_of_weight_sanction_of_reward_of(self):
        found = with_rule(check(lambda m, s: r_terms(m)[2].update(weight=-1)).errors, "J-10")
        assert found and "at most one of" in found[0]
        found = with_rule(check(lambda m, s: r_terms(m)[2].update(
            reward_of="DELIVERY.no_shortcut")).errors, "J-10")
        assert found and "at most one of" in found[0]

    @pytest.mark.parametrize("ref, message", [
        ("DELIVERY", "is not a string of the form C.n"),
        ("OTHER.no_shortcut", "is not declared"),
        ("DELIVERY.nothing", "has no norm"),
    ])
    def test_the_reference_resolves(self, ref, message):
        found = with_rule(check(lambda m, s: r_terms(m)[2].update(sanction_of=ref)).errors,
                          "J-10")
        assert found and message in found[0]

    def test_an_obligation_or_prohibition_with_a_magnitude(self):
        def permission(model, sos):
            norm(sos)["kind"] = "permission"
            del norm(sos)["on_violation"]
        found = with_rule(check(permission).errors, "J-10")
        assert found and "only an obligation or a prohibition" in found[0]
        found = with_rule(check(lambda m, s: norm(s)["on_violation"].pop("magnitude")).errors,
                          "J-10")
        assert found and "declares no on_violation.magnitude" in found[0]
        found = with_rule(check(lambda m, s: norm(s)["on_violation"].update(magnitude=0)).errors,
                          "J-10")
        assert found and "greater than zero" in found[0]
        found = with_rule(check(lambda m, s: r_terms(m)[2].update(
            sanction_of=None, reward_of="DELIVERY.no_shortcut") or r_terms(m)[2].pop(
            "sanction_of")).errors, "J-10")
        assert found and "on_fulfilment.magnitude" in found[0]

    def test_exactly_one_lambda_parameter(self):
        def none(model, sos):
            p = next(p for p in model["parameters"] if p["id"] == "lambda")
            del p["from"]
            p.update(range=[0, 1], value=0.1)
        found = with_rule(check(none).errors, "J-10")
        assert found and "exactly one parameter bound by" in found[0] and "has 0" in found[0]

        def two(model, sos):
            model["parameters"].append({"id": "lambda2", "from": "DELIVERY.lambda"})
        found = with_rule(check(two).errors, "J-10")
        assert found and "has 2 ('lambda', 'lambda2')" in found[0]

    def test_the_player_covers_the_bearer(self):
        def moved(model, sos):
            model["payoffs"][1]["terms"].append(r_terms(model).pop(2))
        found = with_rule(check(moved).errors, "J-10")
        assert found and "covers" in found[0] and "player 'D'" in found[0]

        def one_robot(model, sos):
            model["players"][0]["actors"] = ["ROBOT[1]"]
        assert not with_rule(check(one_robot).errors, "J-10")

        def other_index(model, sos):
            model["players"][0]["actors"] = ["ROBOT[1]"]
            norm(sos)["bearer"] = "ROBOT[2]"
        assert with_rule(check(other_index).errors, "J-10")

    def test_a_norm_is_named_once_per_key(self):
        def twice(model, sos):
            r_terms(model)[1] = {"reward": "effort", "sanction_of": "DELIVERY.no_shortcut"}
        found = with_rule(check(twice).errors, "J-10")
        assert found and "more than one 'sanction_of' term" in found[0]

        def both_keys(model, sos):
            norm(sos)["on_fulfilment"] = {"magnitude": 1}
            r_terms(model)[1] = {"reward": "effort", "reward_of": "DELIVERY.no_shortcut"}
        assert not with_rule(check(both_keys).errors, "J-10")

    def test_without_the_declaration_it_is_an_i7_error(self):
        def undeclared(model, sos):
            sos["extensions"] = [e for e in sos["extensions"] if "grounding" not in e]
            del norm(sos)["on_violation"]["magnitude"]
        found = with_rule(check(undeclared).errors, "I-7")
        assert found and "does not declare 'grounding: 0.1'" in found[0]


# ---------------------------------------------------------------------------
# J-11 (warnings) and J-12 (information)
# ---------------------------------------------------------------------------

class TestJ11:
    def test_passes(self):
        assert not with_rule(check().warnings, "J-11")

    def test_a_norm_with_a_magnitude_that_no_term_names(self):
        def extra(model, sos):
            n2 = copy.deepcopy(norm(sos))
            n2["id"] = "no_speeding"
            n2["content"] = "ROBOT[i].speed == Fast"
            sos["contracts"][0]["norms"].append(n2)
        found = with_rule(check(extra).warnings, "J-11")
        assert len(found) == 1 and "'DELIVERY.no_speeding'" in found[0]

        def not_covered(model, sos):
            extra(model, sos)
            sos["contracts"][0]["norms"][1]["bearer"] = "DISPATCHER"
            model["players"][1]["actors"] = ["DISPATCHER"]
            sos["actors"].append({"id": "AUDITOR", "role": "x"})
            sos["contracts"][0]["parties"].append("AUDITOR")
            sos["contracts"][0]["norms"][1]["bearer"] = "AUDITOR"
            sos["contracts"][0]["norms"][1]["counterparty"] = "DISPATCHER"
        assert not with_rule(check(not_covered).warnings, "J-11")

    def test_a_reward_that_no_reward_of_term_names(self):
        found = with_rule(check(lambda m, s: norm(s).update(
            on_fulfilment={"reward": "bonus", "magnitude": 1})).warnings, "J-11")
        assert len(found) == 1 and "on_fulfilment.magnitude" in found[0] and "'reward_of'" in found[0]

    def test_a_written_weight_with_the_lambda_parameter(self):
        found = with_rule(check(lambda m, s: m["payoffs"][1]["terms"].append(
            {"reward": "shortcuts", "weight": "2 * lambda"})).warnings, "J-11")
        assert len(found) == 1 and "'lambda', bound to DELIVERY.lambda" in found[0]


class TestJ12:
    def test_compliance_incurs_no_sanction(self):
        infos = with_rule(check().infos, "J-12")
        assert ["Design CENTRALIZED" in infos[0], "Design DISTRIBUTED" in infos[1]] == [True, True]
        assert all("'shortcuts'" in i and "is 0" in i for i in infos)

    def test_reward_of_is_reported_and_breaks_condition_ii(self):
        def mutate(model, sos):
            norm(sos)["on_fulfilment"] = {"reward": "bonus", "magnitude": 0.5}
            r_terms(model).append({"reward": "fee", "reward_of": "DELIVERY.no_shortcut"})
            r_terms(model).pop(0)
        infos = with_rule(check(mutate).infos, "J-12")
        rewards = [i for i in infos if "reward_of: DELIVERY.no_shortcut" in i]
        assert len(rewards) == 2 and len(infos) == 4
        assert all("breaks condition (ii) of J.7 by the sign of its weight" in i
                   for i in rewards)
        assert "is 0.984848 (exact: 7878786117651/8000000000000)" in rewards[0]

    def test_compliance_incurs_a_sanction(self):
        infos = with_rule(check(lambda m, s: m.update(compliance="shortcut_first")).infos,
                          "J-12")
        assert len(infos) == 2
        assert all("is 1 (exact: 1)" in i and "does not hold" in i for i in infos)

    def test_no_report_without_a_link(self):
        assert not with_rule(type_check(parse_file(PLAIN)).infos, "J-12")


# ---------------------------------------------------------------------------
# J-14
# ---------------------------------------------------------------------------

def game_for(model, query_id: str, design: str):
    query = next(q for q in model.equilibria if q.id == query_id)
    d = model.design(design)
    game = prepare_map(model, query, d, None)
    return game, query, endpoint_setup(game, query, d)


class TestJ14:
    def test_syntactic_degree(self):
        assert syntactic_degree(parse_param_expr("-2 * lambda"), "lambda") == 1
        assert syntactic_degree(parse_param_expr("lambda * lambda - lambda * lambda"),
                                "lambda") == 2
        assert syntactic_degree(parse_param_expr("(1 - lambda) * (2 + p)"), "lambda") == 1

    def test_applies_to_the_example(self):
        game, query, j14 = game_for(LINKED_MODEL, "comply_by_lambda", "CENTRALIZED")
        assert j14["applicable"] and j14["parameter"] == "lambda"
        assert j14["decreasing"] == ["R", "D"] and j14["increasing"] == ["D"]

    def test_condition_ii_fails_for_a_reward_and_for_a_sanctioned_profile(self):
        def reward(model, sos):
            model["payoffs"][1]["terms"].append({"reward": "shortcuts", "weight": "lambda"})
        _, _, j14 = game_for(model_of(reward), "comply_by_lambda", "CENTRALIZED")
        # The term +λ·shortcuts of D has the expected value 0 under comply:
        # the gain of D is non-decreasing (B+), and no longer non-increasing.
        assert j14["applicable"] and j14["decreasing"] == ["R"] and j14["increasing"] == ["D"]

        def shortcut(model, sos):
            model["equilibria"][2]["profile"] = "shortcut_first"
        _, _, j14 = game_for(model_of(shortcut), "comply_by_lambda", "CENTRALIZED")
        assert j14["applicable"] and j14["decreasing"] == ["D"] == j14["increasing"]

    @pytest.mark.parametrize("mutate, reason", [
        (lambda m, s: m["commands"][1]["updates"][0].update(
            prob="0.5 * lambda * (beta * alpha * (1 - p_fail) * (1 - p_loss) + (1 - beta) * q_local)"),
         "occurs in the probability"),
        (lambda m, s: m["payoffs"][1]["terms"].append(
            {"reward": "shortcuts", "weight": "lambda * lambda - lambda * lambda"}),
         "with a power above one"),
    ])
    def test_does_not_apply(self, mutate, reason):
        _, _, j14 = game_for(model_of(mutate), "comply_by_lambda", "CENTRALIZED")
        assert not j14["applicable"] and reason in j14["reason"]

    def test_also_in_a_file_without_grounding(self, capsys):
        """The end-point certification is a method of I.9; J-14 refers to
        it. It applies to the map of the file without grounding as well."""
        ends = equilibrium_map(PLAIN_MODEL, "comply_by_lambda")["designs"][0]["endpoints"]
        assert ends["applicable"] and ends["decreasing"] == ["R", "D"]
        status, out, _ = cli(capsys, "game-check", PLAIN, "--certify", "--equilibrium",
                             "comply_by_lambda", "--regime", "DISTRIBUTED")
        assert status == 0 and "end points (I.9): condition A holds for 'lambda'" in out

    def test_end_point_claims(self):
        game, query, _ = game_for(LINKED_MODEL, "comply_by_lambda", "CENTRALIZED")
        star = LAMBDA_STAR["CENTRALIZED"]
        cell = EqCell(box={"lambda": (F(1, 5), F(1))})
        assert endpoint_verdict(game, query, cell)
        assert (cell.status, cell.method, cell.from_every_state) == (
            "equilibrium", J14_METHOD, None)
        cell = EqCell(box={"lambda": (F(0), F(1, 10))})
        assert endpoint_verdict(game, query, cell)
        assert (cell.status, cell.deviators) == ("not_equilibrium", ["R"])
        assert cell.gain_bounds["R"] == (2 * (star - F(1, 10)), 2 * star)
        assert cell.deviations["R"] == [
            {"state": {"task": "Pending", "attempts": 0, "settled": False}, "action": "careless"}]
        # Across λ*: no claim.
        assert not endpoint_verdict(game, query, EqCell(box={"lambda": (F(1, 10), F(1, 5))}))

    def test_no_deviator_without_condition_ii(self):
        def shortcut(model, sos):
            model["equilibria"][2]["profile"] = "shortcut_first"
        game, query, j14 = game_for(model_of(shortcut), "comply_by_lambda", "DISTRIBUTED")
        # shortcut_first is an equilibrium exactly up to λ*; R gains above it,
        # but (ii) fails for R, so the end points claim no deviator.
        cell = EqCell(box={"lambda": (F(1, 2), F(1))})
        assert not endpoint_verdict(game, query, cell)


# ---------------------------------------------------------------------------
# The reviewer's files: a gain with a hump (J-14 must be refused)
# ---------------------------------------------------------------------------

HUMP = ROOT / "data" / "games_link" / "hump.cadl"
PROB_HUMP = ROOT / "data" / "games_link" / "prob_hump.cadl"
# The gain is max(0, -1/2 + 4λ - 4λ²) (in prob_hump.cadl through the
# probabilities); it is positive exactly between the roots
# (1 ± 1/√2)/2, 0.14644... and 0.85355...
ROOT_LO = (F(1464, 10000), F(1465, 10000))
ROOT_HI = (F(8535, 10000), F(8536, 10000))


class TestHump:
    def map_of(self, path):
        model = checked_model(parse_file(path))
        return equilibrium_map(model, "mp")["designs"][0]

    def check_hump(self, d):
        assert d["resolution_reached"]
        assert [c.status for c in d["cells"]] == [
            "equilibrium", "undecided", "not_equilibrium", "undecided", "equilibrium"]
        for c in d["cells"]:
            a, b = c.box["lam"]
            if c.status == "equilibrium":
                # No equilibrium cell reaches into the hump.
                assert b <= ROOT_LO[0] or a >= ROOT_HI[1], (a, b)
            if c.status == "not_equilibrium":
                assert a >= ROOT_LO[0] and b <= ROOT_HI[1], (a, b)
            assert c.method is None

    def test_hump_in_a_weight(self):
        d = self.map_of(HUMP)
        assert not d["endpoints"]["applicable"]
        assert "power above one, counted on the text" in d["endpoints"]["reason"]
        self.check_hump(d)
        boxes = [tuple(float(x) for x in c.box["lam"]) for c in d["cells"]]
        assert [round(x, 4) for x in boxes[0] + boxes[2] + boxes[4]] == [
            0, 0.1445, 0.1484, 0.8516, 0.8555, 1]

    def test_hump_in_the_probabilities(self):
        d = self.map_of(PROB_HUMP)
        assert not d["endpoints"]["applicable"]
        assert "occurs in the probability 'lam'" in d["endpoints"]["reason"]
        self.check_hump(d)

    @pytest.mark.parametrize("path, mutant", [
        (HUMP, "endpoints_drop_affine"), (PROB_HUMP, "endpoints_drop_probability")])
    def test_the_gate_is_what_keeps_the_hump(self, path, mutant):
        """With the gate of condition 2 disabled, the end points of the
        whole region are equilibria and the hump is lost: the test above
        fails with the mutant."""
        with games_fuzz.mutation(mutant):
            d = self.map_of(path)
        assert d["endpoints"]["applicable"]
        with pytest.raises(AssertionError):
            self.check_hump(d)
        assert any(c.method == J14_METHOD and c.status == "equilibrium"
                   and c.box["lam"][0] < ROOT_HI[0] and c.box["lam"][1] > ROOT_LO[1]
                   for c in d["cells"])


# ---------------------------------------------------------------------------
# Soundness fuzzing of J-14 (tests/games_fuzz.py --j14)
# ---------------------------------------------------------------------------

class TestJ14Fuzz:
    def test_end_point_cells_hold_at_every_sampled_point(self):
        stats = games_fuzz.run(31, 25, j14=True)
        assert "failure" not in stats, stats["failure"] + "\n" + stats.get("failing_model", "")
        assert stats["endpoints_applicable"] > 0 and stats["endpoints_not_applicable"] > 0
        assert stats.get("cells_endpoints_equilibrium", 0) > 0

    @pytest.mark.parametrize("name, seed, count", [
        ("endpoints_drop_probability", 1, 60),
        ("endpoints_drop_affine", 5, 20),
        ("endpoints_drop_condition_b", 1, 30),
    ])
    def test_a_deliberate_soundness_bug_is_detected(self, name, seed, count):
        stats = games_fuzz.run(seed, count, name, j14=True)
        assert "failure" in stats, f"the mutant {name} survived {stats}"


# ---------------------------------------------------------------------------
# Keywords of PRISM-games (I.11)
# ---------------------------------------------------------------------------

GAME = """
sos:
  name: "Keywords"
  type: Acknowledged
  extensions: [ { stochastic: 0.1 }, { games: 0.1 } ]
  actors: [ { id: ALICE, role: "x" }, { id: BOB, role: "y" } ]
  model:
    kind: smg
    variables: [ { id: smg, type: "[0..2]", init: 0 } ]
    players: [ { id: player, actors: [ALICE] }, { id: OTHER, actors: [BOB] } ]
    commands:
      - { action: endplayer, owner: player, guard: "smg == 0", updates: [ { set: { smg: 1 } } ] }
      - { action: tptg, owner: player, guard: "smg == 0", updates: [ { set: { smg: 2 } } ] }
      - { action: csg, guard: "smg == 1", updates: [ { set: { smg: 2 } } ] }
    rewards: [ { id: r, items: [ { action: tptg, value: 1 } ] } ]
    profiles: { sigma: [ { choose: tptg } ] }
    compliance: sigma
    payoffs:
      - { player: player, until: "smg == 2", terms: [ { reward: r } ] }
      - { player: OTHER, until: "smg == 2", terms: [ { reward: r } ] }
"""


class TestPrismGamesKeywords:
    def test_renamed_in_a_strategic_model(self):
        text = export_prism(checked_model(parse(GAME)))["Keywords_base.prism"]
        assert text.startswith("smg\n\nplayer player_\n  [endplayer_], [tptg_]\nendplayer\n")
        assert "  smg_ : [0..2] init 0;" in text
        assert "[csg_] smg_ = 1 -> (1) : (smg_'=2);" in text
        # A player that owns no command of the design is written without
        # actions (PRISM-games 3.2.4 accepts it, and it can be named in a
        # coalition).
        assert "\nplayer OTHER\nendplayer\n" in text

    def test_the_example_is_unchanged(self):
        text = export_prism(LINKED_MODEL)["RobotDeliveryGame_CENTRALIZED.prism"]
        assert text.startswith("smg\n\nplayer R_\n  [careful], [careless]\nendplayer\n")

    def test_not_in_a_model_of_appendix_h(self):
        model = checked_model(parse(GAME))
        from cadl.stochastic import _prism_name
        assert _prism_name("smg") == "smg" and _prism_name("player") == "player"
        export_prism(model)
        assert _prism_name("smg") == "smg"           # restored after the export

    def test_collision_after_renaming(self):
        text = GAME.replace("{ id: OTHER, actors: [BOB] }", "{ id: player_, actors: [BOB] }") \
                   .replace("{ player: OTHER,", "{ player: player_,")
        with pytest.raises(ExportError, match="players 'player' and 'player_'"):
            export_prism(checked_model(parse(text)))
        text = GAME.replace("{ id: smg, type", "{ id: smg_, type").replace(
            "variables: [", "parameters: [ { id: smg, range: [0, 1], value: 0 } ]\n    variables: [") \
            .replace("smg ==", "smg_ ==").replace("smg: ", "smg_: ")
        model = checked_model(parse(text))
        assert model is not None
        with pytest.raises(ExportError, match="both written 'smg_'"):
            export_prism(model)

    def test_reported_name_clash_after_renaming(self, tmp_path, capsys):
        path = tmp_path / "k.cadl"
        path.write_text(GAME.replace("{ id: OTHER,", "{ id: smg,").replace(
            "{ player: OTHER,", "{ player: smg,"), encoding="utf-8")
        status, _, err = cli(capsys, "model-export", path, "-o", tmp_path)
        assert status == 0 and "the player 'smg' has the name of the variable 'smg'" in err
