"""Strategic model extension (Appendix I, games 0.1): the static rules
I-1 to I-11, the model under a profile, the IR, the export, the
equilibrium checks of I.8 with the numbers of I.12, and `cadl game-check`."""

from __future__ import annotations

import copy
import itertools
import json
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from cadl.cli import main
from cadl.games import (
    NotEvaluated,
    game_to_ir,
    induced_model,
    payoff_of,
    term_weight,
)
from cadl.games_eval import (
    EvaluationError,
    admissibility_witness,
    build_game,
    build_process,
    canonical_best_response,
    equilibrium_check,
    optimal_values,
    player_at_point,
    point_distributions,
    point_reward,
    _all,
)
from cadl.parser import parse, parse_file
from cadl.sim.lower import lower_to_ir
from cadl.stochastic import (
    analyse,
    checked_model,
    compile_state_expr,
    export_prism,
    iter_states,
    parse_param_expr,
)
from cadl.stochastic_eval import build_chain, evaluate
from cadl.type_checker import type_check

ROOT = Path(__file__).parent
EXAMPLES = ROOT.parent / "examples"
ROBOT = EXAMPLES / "games_robot_delivery.cadl"
TWO = EXAMPLES / "games_two_players.cadl"
CE_B = """
sos:
  name: "CornerStop"
  type: Acknowledged
  extensions: [ { stochastic: 0.1 }, { games: 0.1 } ]
  actors: [ { id: ACT, role: "x" } ]
  model:
    kind: smg
    parameters: [ { id: p, range: [0, 1], value: 0 } ]
    variables: [ { id: loc, type: "{S0, S1, Goal, Trap}", init: S0 } ]
    labels: { goal: "loc == Goal" }
    players: [ { id: A, actors: [ACT] } ]
    commands:
      - action: a
        guard: "loc == S0"
        updates: [ { prob: "p", set: { loc: S1 } }, { prob: rest, set: { loc: Goal } } ]
      - action: b
        guard: "loc == S1"
        updates: [ { prob: "1 - p", set: { loc: Trap } }, { prob: rest, set: { loc: Goal } } ]
    rewards: [ { id: steps, items: [ { value: 1 } ] } ]
    profiles: { sigma: [] }
    compliance: sigma
    payoffs: [ { player: A, until: "goal", terms: [ { reward: steps } ] } ]
    equilibria: [ { id: m, over: { p: [0, 1] } } ]
"""

F = Fraction


def tiny() -> dict:
    """A game of two players with a cycle: A chooses in S0, B in S1."""
    return yaml.safe_load("""
sos:
  name: "Tiny"
  type: Acknowledged
  extensions: [ { stochastic: 0.1 }, { games: 0.1 } ]
  actors: [ { id: ALICE, role: "x" }, { id: "BOB[1..N]", role: "y" } ]
  model:
    kind: smg
    parameters:
      - { id: p, range: [0, 1], value: 0.5 }
      - { id: lam, range: [0, 2], value: 1 }
    variables: [ { id: loc, type: "{S0, S1, Goal}", init: S0 } ]
    labels: { goal: "loc == Goal" }
    players: [ { id: A, actors: [ALICE] }, { id: B, actors: ["BOB[*]"] } ]
    commands:
      - { action: go, owner: A, guard: "loc == S0",
          updates: [ { prob: "p", set: { loc: Goal } }, { prob: rest, set: { loc: S1 } } ] }
      - { action: wait, owner: A, guard: "loc == S0", updates: [ { set: { loc: S1 } } ] }
      - { action: stop, owner: B, guard: "loc == S1", updates: [ { set: { loc: Goal } } ] }
      - { action: back, owner: B, guard: "loc == S1",
          updates: [ { prob: 0.5, set: { loc: S0 } }, { prob: rest, set: { loc: Goal } } ] }
    rewards:
      - { id: win, items: [ { action: go, value: 1 } ] }
      - { id: cost, items: [ { action: back, value: 1 }, { action: wait, value: 1 } ] }
    profiles: { sigma: [ { choose: go }, { choose: stop } ] }
    compliance: sigma
    payoffs:
      - { player: A, until: "goal", terms: [ { reward: win, weight: 2 }, { reward: cost, weight: "-lam" } ] }
      - { player: B, until: "goal", terms: [ { reward: cost, weight: -1 }, { reward: win } ] }
    properties: [ { id: REACH, expr: "P=? [ F goal ]" } ]
    equilibria: [ { id: eq }, { id: eqmap, over: { lam: [0, 2] }, resolution: 0.1 } ]
""")


def build(mutate=None, base=tiny) -> str:
    data = base()
    if mutate is not None:
        mutate(data["sos"]["model"], data["sos"])
    return yaml.safe_dump(data, sort_keys=False)


def check(mutate=None, base=tiny):
    return type_check(parse(build(mutate, base)))


def with_rule(items, rule: str) -> list:
    return [str(i) for i in items if f"({rule})" in str(i)]


def errors(mutate=None, rule: str = "", base=tiny) -> list:
    return with_rule(check(mutate, base).errors, rule)


def model_of(mutate=None, base=tiny):
    sos = parse(build(mutate, base))
    result = type_check(sos)
    assert result.ok, [str(e) for e in result.errors]
    return checked_model(sos)


def robot() -> dict:
    return yaml.safe_load(ROBOT.read_text(encoding="utf-8"))


def cli(capsys, *args):
    status = main([str(a) for a in args])
    out = capsys.readouterr()
    return status, out.out, out.err


# ---------------------------------------------------------------------------
# Declaration (I-1, I.15) and structure (I-2)
# ---------------------------------------------------------------------------

class TestDeclaration:
    def test_examples_have_no_diagnostics(self):
        for path in (ROBOT, TWO):
            result = type_check(parse_file(path))
            assert result.ok and not result.warnings and not result.infos, path

    def test_tiny_passes(self):
        result = check()
        assert result.ok and not result.warnings and not result.infos

    def test_i1_games_needs_stochastic(self):
        def only_games(model, sos):
            sos["extensions"] = [{"games": 0.1}]
        result = check(only_games)
        assert with_rule(result.errors, "Appendix I, I-1")
        # ... and the keys are not interpreted: no other error of I.10.
        assert len(result.errors) == 1

    def test_i1_also_without_a_model(self):
        def no_model(model, sos):
            sos["extensions"] = [{"games": 0.1}]
            del sos["model"]
        assert with_rule(check(no_model).errors, "Appendix I, I-1")

    def test_undeclared_use_is_read_by_appendix_h(self):
        def no_games(model, sos):
            sos["extensions"] = [{"stochastic": 0.1}]
        result = check(no_games)
        assert with_rule(result.errors, "S-1")            # kind: smg
        assert with_rule(result.errors, "S-9")            # two commands of one player
        assert any("does not declare 'games: 0.1'" in str(i) for i in result.infos)
        assert not result.warnings
        assert checked_model(parse(build(no_games))) is None

    def test_extension_is_known(self):
        assert not any("not known" in str(i) for i in check().infos)

    def test_another_version_of_games_is_not_this_extension(self):
        def v2(model, sos):
            sos["extensions"] = [{"stochastic": 0.1}, {"games": 0.2}]
        result = check(v2)
        assert any("declared with version" in str(i) for i in result.infos)
        assert with_rule(result.errors, "S-1")


class TestStructure:
    def test_i2_kind(self):
        assert errors(lambda m, s: m.update(kind="ctmc"), "I-2")
        result = check(lambda m, s: m.update(kind="mdp"))
        assert with_rule(result.errors, "I-2") and with_rule(result.infos, "I-11")

    @pytest.mark.parametrize("key", ["players", "profiles", "compliance", "payoffs"])
    def test_i2_smg_needs(self, key):
        found = errors(lambda m, s: m.pop(key), "I-2")
        assert any(f"model.{key}" in e for e in found)

    def test_i2_at_least_one_player(self):
        assert errors(lambda m, s: m.update(players=[]), "I-2")

    @pytest.mark.parametrize("kind", ["dtmc", None])
    def test_i2_a_model_of_appendix_h_has_no_game_keys(self, kind):
        def mutate(model, sos):
            if kind is None:
                del model["kind"]
            else:
                model["kind"] = kind
            model["properties"][0]["profile"] = "sigma"
        found = errors(mutate, "I-2")
        for what in ("model.players", "model.profiles", "model.compliance", "model.payoffs",
                     "model.equilibria", "'owner' of a command", "'profile' of a property"):
            assert any(what in e for e in found), what
        # It is a model of Appendix H: S-9 applies.
        assert errors(mutate, "S-9")

    def test_a_dtmc_model_in_a_file_that_declares_games(self):
        def mutate(model, sos):
            for key in ("players", "profiles", "compliance", "payoffs", "equilibria"):
                del model[key]
            model["kind"] = "dtmc"
            model["commands"] = [model["commands"][0], model["commands"][2]]
            for c in model["commands"]:
                del c["owner"]
            model["rewards"] = [model["rewards"][0]]
        model = model_of(mutate)
        assert model.kind == "dtmc" and "players" not in lower_to_ir(parse(build(mutate))).stochastic_model


# ---------------------------------------------------------------------------
# I-3, I-4: players and ownership
# ---------------------------------------------------------------------------

class TestPlayers:
    @pytest.mark.parametrize("name", ["1x", "AND", "rest", "a-b", True, None])
    def test_i3_bad_id(self, name):
        assert errors(lambda m, s: m["players"][0].update(id=name), "I-3")

    def test_i3_duplicate(self):
        assert errors(lambda m, s: m["players"][1].update(id="A"), "I-3")

    def test_i3_property_keywords_are_admitted_as_player_ids(self):
        # The examples of I.12 name a player R, which S-2 excludes; see
        # PLAYER_ID_ADMITTED_KEYWORDS.
        def rename(model, sos):
            model["players"][0]["id"] = "R"
            for c in model["commands"][:2]:
                c["owner"] = "R"
            model["payoffs"][0]["player"] = "R"
        assert check(rename).ok

    def test_i3_actors(self):
        assert errors(lambda m, s: m["players"][0].update(actors=[]), "I-3")
        assert errors(lambda m, s: m["players"][0].pop("actors"), "I-3")
        assert errors(lambda m, s: m["players"][0].update(actors=["NOBODY"]), "I-3")
        assert errors(lambda m, s: m["players"][0].update(actors=["ALICE[1"]), "I-3")

    def test_i3_an_actor_stands_behind_one_player(self):
        found = errors(lambda m, s: m["players"][0].update(actors=["BOB[1]"]), "I-3")
        assert any("at most one player" in e for e in found)
        found = errors(lambda m, s: m["players"][1].update(actors=["BOB[1]", "BOB[2]"]), "I-3")
        assert any("more than once" in e for e in found)

    def test_i4_owner_is_a_player(self):
        assert errors(lambda m, s: m["commands"][0].update(owner="Z"), "I-4")

    def test_i4_owned_command_needs_an_action(self):
        def mutate(model, sos):
            del model["commands"][3]["action"]
            model["rewards"][1]["items"].pop(0)
        assert any("no 'action'" in e for e in errors(mutate, "I-4"))

    def test_i4_one_label_one_owner(self):
        def mutate(model, sos):
            model["commands"].append({"action": "go", "owner": "B", "guard": "loc == Goal",
                                      "updates": [{"set": {"loc": "Goal"}}]})
        assert any("different owners" in e for e in errors(mutate, "I-4"))

        def unowned(model, sos):
            model["commands"].append({"action": "go", "guard": "loc == Goal",
                                      "updates": [{"set": {"loc": "Goal"}}]})
        assert any("different owners" in e for e in errors(unowned, "I-4"))


# ---------------------------------------------------------------------------
# I-5: at most one player chooses in a state
# ---------------------------------------------------------------------------

class TestTurns:
    def test_i5_replaces_s9(self):
        result = check()
        assert not with_rule(result.errors, "S-9") and not with_rule(result.errors, "I-5")

    def test_i5_unowned_command_is_enabled_alone(self):
        def mutate(model, sos):
            model["commands"].append({"action": "tick", "guard": "loc == S0",
                                      "updates": [{"set": {"loc": "Goal"}}]})
        assert any("unowned" in e for e in errors(mutate, "I-5"))

    def test_i5_two_players_in_one_state(self):
        found = errors(lambda m, s: m["commands"][3].update(guard="loc != Goal"), "I-5")
        assert any("'A' and 'B'" in e and "loc=S0" in e for e in found)

    def test_i5_same_label_twice(self):
        def mutate(model, sos):
            model["commands"].append({"action": "go", "owner": "A", "guard": "loc == S0",
                                      "updates": [{"set": {"loc": "Goal"}}]})
        assert any("action label 'go'" in e for e in errors(mutate, "I-5"))

    def test_i5_concerns_unreachable_states_and_every_design(self):
        def mutate(model, sos):
            model["variables"].append({"id": "x", "type": "[0..1]", "init": 0})
            model["commands"].append({"action": "tick", "guard": "x == 1 AND loc == S0",
                                      "updates": [{"set": {"loc": "Goal"}}]})
        assert any("x=1" in e for e in errors(mutate, "I-5"))


# ---------------------------------------------------------------------------
# I-6: profiles
# ---------------------------------------------------------------------------

class TestProfiles:
    @pytest.mark.parametrize("name", ["AND", "P", "1a", True])
    def test_i6_name(self, name):
        def mutate(model, sos):
            model["profiles"][name] = model["profiles"]["sigma"]
        assert errors(mutate, "I-6")

    def test_i6_choose_is_the_label_of_an_owned_command(self):
        assert errors(lambda m, s: m["profiles"]["sigma"][0].update(choose="fly"), "I-6")

        def unowned(model, sos):
            model["commands"].append({"action": "tick", "guard": "loc == Goal",
                                      "updates": [{"set": {"loc": "Goal"}}]})
            model["profiles"]["sigma"].append({"choose": "tick"})
        assert any("unowned" in e for e in errors(unowned, "I-6"))

    def test_i6_guard_has_no_label_and_is_boolean(self):
        found = errors(lambda m, s: m["profiles"]["sigma"][0].update(guard="goal"), "I-6")
        assert any("label" in e for e in found)
        assert errors(lambda m, s: m["profiles"]["sigma"][0].update(guard="loc"), "I-6")
        assert not errors(lambda m, s: m["profiles"]["sigma"][0].update(guard=True), "I-6")

    def test_i6_every_choice_is_covered(self):
        found = errors(lambda m, s: m["profiles"]["sigma"].pop(), "I-6")
        assert any("No rule of the profile 'sigma' applies" in e and "loc=S1" in e
                   for e in found)
        found = errors(lambda m, s: m["profiles"]["sigma"][0].update(guard="loc == S1"), "I-6")
        assert any("loc=S0" in e for e in found)

    def test_i6_empty_profile_when_nobody_has_a_choice(self):
        def mutate(model, sos):
            model["commands"] = [model["commands"][0], model["commands"][2]]
            model["rewards"][1]["items"] = [{"action": "stop", "value": 1}]
            model["profiles"] = {"sigma": []}
        assert check(mutate).ok

    def test_i6_compliance_names_a_profile(self):
        assert errors(lambda m, s: m.update(compliance="other"), "I-6")

    def test_i6_warns_about_a_rule_that_is_never_first(self):
        def mutate(model, sos):
            model["profiles"]["sigma"].insert(1, {"choose": "wait"})
        result = check(mutate)
        assert result.ok
        assert any("Rule 2 of the profile 'sigma'" in w for w in with_rule(result.warnings, "I-6"))

    def test_first_applicable_rule_selects(self):
        model = model_of(lambda m, s: m["profiles"].update(
            other=[{"guard": "loc == S1", "choose": "go"}, {"choose": "wait"},
                   {"choose": "back"}]))
        game = build_game(model, model.designs[0], [], model.fixed_values(model.designs[0]))
        picked = {game.states[i]: game.commands[i][k].command.action
                  for i, k in enumerate(game.selection("other")) if k is not None}
        assert picked == {("S0",): "wait", ("S1",): "back"}


# ---------------------------------------------------------------------------
# I-7: payoffs
# ---------------------------------------------------------------------------

class TestPayoffs:
    def test_i7_one_payoff_per_player(self):
        assert any("0 entries" in e for e in errors(lambda m, s: m["payoffs"].pop(), "I-7"))
        assert any("2 entries" in e for e in errors(
            lambda m, s: m["payoffs"].append(copy.deepcopy(m["payoffs"][0])), "I-7"))
        assert errors(lambda m, s: m["payoffs"][0].update(player="Z"), "I-7")

    def test_i7_until(self):
        assert errors(lambda m, s: m["payoffs"][0].pop("until"), "I-7")
        assert errors(lambda m, s: m["payoffs"][0].update(until="loc"), "I-7")
        assert not errors(lambda m, s: m["payoffs"][0].update(until="goal OR loc == S1"), "I-7")

    def test_i7_terms(self):
        assert errors(lambda m, s: m["payoffs"][0].update(terms=[]), "I-7")
        assert errors(lambda m, s: m["payoffs"][0]["terms"][0].update(reward="none"), "I-7")
        assert any("more than one term" in e for e in errors(
            lambda m, s: m["payoffs"][0]["terms"][1].update(reward="win"), "I-7"))
        assert errors(lambda m, s: m["payoffs"][0]["terms"][0].update(weight="2 * x"), "I-7")
        assert errors(lambda m, s: m["payoffs"][0]["terms"][0].update(weight="2 /"), "I-7")
        assert errors(lambda m, s: m["payoffs"][0]["terms"][0].update(weight=[1]), "I-7")

    def test_weights_have_any_sign_and_default_one(self):
        model = model_of()
        a, b = model.payoffs
        assert [t.weight_text for t in a.terms] == ["2", "-lam"]
        assert [t.weight_text for t in b.terms] == ["-1", "1"]
        assert term_weight(model, b, b.terms[1]) == parse_param_expr("1")

    def test_i7_warns_when_the_target_holds_initially(self):
        result = check(lambda m, s: m["payoffs"][1].update(until="loc == S0 OR goal"))
        assert any("holds in the initial state" in w for w in with_rule(result.warnings, "I-7"))

    def test_i7_grounded_term_needs_the_grounding_declaration(self):
        def mutate(model, sos):
            model["payoffs"][0]["terms"][1] = {"reward": "cost", "sanction_of": "C.n"}
        assert any("sanction_of" in e and "grounding" in e for e in errors(mutate, "I-7"))

    def grounded(self, key="sanction_of"):
        def mutate(model, sos):
            # grounding 0.1 requires norms 0.1 (J-1).
            sos["extensions"] += [{"norms": 0.1}, {"grounding": 0.1}]
            model["payoffs"][0]["terms"][1] = {"reward": "cost", key: "C.n"}
        return mutate

    @pytest.mark.parametrize("key", ["sanction_of", "reward_of"])
    def test_i7_grounded_term_is_not_evaluated(self, key, tmp_path, capsys):
        result = check(self.grounded(key))
        assert result.ok
        assert any(key in i and "not evaluated" in i for i in with_rule(result.infos, "I-7"))
        model = checked_model(parse(build(self.grounded(key))))
        term = model.payoffs[0].terms[1]
        assert term.grounded and term.weight is None
        with pytest.raises(NotEvaluated, match=key):
            term_weight(model, model.payoffs[0], term)
        with pytest.raises(NotEvaluated):
            equilibrium_check(model, "eq")
        path = tmp_path / "g.cadl"
        path.write_text(build(self.grounded(key)), encoding="utf-8")
        status, out, _ = cli(capsys, "game-check", path, "--certify", "--format", "json")
        data = json.loads(out)
        assert status == 1
        assert [c["status"] for c in data["checks"]] == ["not_evaluated"]
        assert [m["status"] for m in data["maps"]] == ["not_evaluated"]
        assert key in data["checks"][0]["diagnostic"]
        # A property does not need the payoff.
        status, out, _ = cli(capsys, "model-check", path)
        assert status == 0 and "REACH" in out

    def test_a_supplied_weight_is_used(self):
        model = checked_model(parse(build(self.grounded())))
        model.term_weights[("A", "cost")] = parse_param_expr("-1 * lam")
        with_link = equilibrium_check(model, "eq")["designs"][0]["players"]
        plain = equilibrium_check(model_of(), "eq")["designs"][0]["players"]
        assert with_link == plain

    def test_ir_of_a_grounded_term(self):
        ir = game_to_ir(checked_model(parse(build(self.grounded()))))
        assert ir["payoffs"][0]["terms"][1] == {
            "reward": "cost", "weight": None, "sanction_of": "C.n", "reward_of": None}


# ---------------------------------------------------------------------------
# I-8: admissibility at the fixed values
# ---------------------------------------------------------------------------

def loop_game(profile: str = "both_stop") -> dict:
    """Two players who each may pass the move to the other or stop."""
    return yaml.safe_load(f"""
sos:
  name: "PassPass"
  type: Acknowledged
  extensions: [ {{ stochastic: 0.1 }}, {{ games: 0.1 }} ]
  actors: [ {{ id: ALICE, role: "x" }}, {{ id: BOB, role: "y" }} ]
  model:
    kind: smg
    variables: [ {{ id: loc, type: "{{TA, TB, End}}", init: TA }} ]
    players: [ {{ id: A, actors: [ALICE] }}, {{ id: B, actors: [BOB] }} ]
    commands:
      - {{ action: a_stop, owner: A, guard: "loc == TA", updates: [ {{ set: {{ loc: End }} }} ] }}
      - {{ action: a_pass, owner: A, guard: "loc == TA", updates: [ {{ set: {{ loc: TB }} }} ] }}
      - {{ action: b_stop, owner: B, guard: "loc == TB", updates: [ {{ set: {{ loc: End }} }} ] }}
      - {{ action: b_pass, owner: B, guard: "loc == TB", updates: [ {{ set: {{ loc: TA }} }} ] }}
    rewards: [ {{ id: passes, items: [ {{ action: a_pass, value: 1 }}, {{ action: b_pass, value: 1 }} ] }} ]
    profiles:
      both_stop: [ {{ choose: a_stop }}, {{ choose: b_stop }} ]
      both_pass: [ {{ choose: a_pass }}, {{ choose: b_pass }} ]
    compliance: {profile}
    payoffs:
      - {{ player: A, until: "loc == End", terms: [ {{ reward: passes }} ] }}
      - {{ player: B, until: "loc == End", terms: [ {{ reward: passes, weight: -1 }} ] }}
    equilibria: [ {{ id: eq }} ]
""")


class TestAdmissibility:
    def test_unilateral_deviations_only(self):
        """Each player alone cannot keep the game going against
        `both_stop`: the other one stops. That both together could is
        no matter (I.6). Against `both_pass` each can."""
        def drop(model, sos):
            del model["profiles"]["both_pass"]
        result = check(drop, base=loop_game)
        assert result.ok, [str(e) for e in result.errors]
        found = errors(base=loop_game, rule="I-8")
        assert len(found) == 2 and all("'both_pass'" in e for e in found)

    def test_pass_pass_check(self):
        def drop(model, sos):
            del model["profiles"]["both_pass"]
        model = model_of(drop, base=loop_game)
        players = equilibrium_check(model, "eq")["designs"][0]["players"]
        # A gains one by passing once: B then stops.
        assert [(p["payoff"], p["best_response_value"]) for p in players] == [(0, 1), (0, 0)]
        assert players[0]["best_response"] == [{"state": {"loc": "TA"}, "action": "a_pass"}]

    def test_target_need_not_be_absorbing(self):
        """What happens after the target has held does not count: a cycle
        or a deadlock behind it does no harm."""
        def mutate(model, sos):
            model["variables"][0]["type"] = "{TA, TB, End, After}"
            model["commands"] += [
                {"action": "on", "guard": "loc == End", "updates": [{"set": {"loc": "After"}}]},
                {"action": "spin", "guard": "loc == After", "updates": [{"set": {"loc": "After"}}]},
            ]
            del model["profiles"]["both_pass"]
        assert check(mutate, base=loop_game).ok

    def test_i8_target_that_can_be_left_and_avoided(self):
        """B may spin in TB for ever. Against `both_stop` that state is
        not in Reach_B (A stops before it), so the profile is admissible;
        against `a_passes` it is, and the profile is not."""
        def mutate(model, sos):
            model["commands"][3]["updates"] = [{"set": {"loc": "TB"}}]    # B may spin
            del model["profiles"]["both_pass"]
            model["profiles"]["a_passes"] = [{"choose": "a_pass"}, {"choose": "b_stop"}]
        found = errors(mutate, "I-8", base=loop_game)
        assert len(found) == 1
        assert "'a_passes' is not admissible for the player 'B'" in found[0]
        assert "loc=TA" in found[0]

    def test_i8_deadlock_outside_the_target(self):
        def mutate(model, sos):
            model["payoffs"][0]["until"] = "loc == TB"     # A's target; End is a deadlock
            del model["profiles"]["both_pass"]
        found = errors(mutate, "I-8", base=loop_game)
        assert found and all("player 'A'" in e for e in found)

    def test_witness_function(self):
        model = model_of(lambda m, s: m["profiles"].pop("both_pass"), base=loop_game)
        game = build_game(model, model.designs[0], [], {})
        assert admissibility_witness(game, "both_stop", "A", ()) is None
        assert admissibility_witness(game, "both_stop", "B", ()) is None
        full = model_of(lambda m, s: m["profiles"].pop("both_pass"), base=loop_game)
        full.profiles.append(type(full.profiles[0])(id="both_pass", rules=[
            type(full.profiles[0].rules[0])(choose="a_pass"),
            type(full.profiles[0].rules[0])(choose="b_pass")]))
        game = build_game(full, full.designs[0], [], {})
        assert admissibility_witness(game, "both_pass", "A", ()) == ("TA",)

    def test_i9_corners_are_fine_and_the_interior_is_not(self):
        """The reviewer's counterexample: the profile is admissible at
        p = 0 and at p = 1 and at no valuation between them. The centre
        of the face 0 < p < 1 shows it."""
        result = type_check(parse(CE_B))
        found = with_rule(result.errors, "I-9")
        assert len(result.errors) == 1 and "face centre p=0.5" in found[0]
        assert "not admissible" in found[0]

    def test_i9_failure_at_an_end_of_the_region(self):
        text = CE_B.replace("over: { p: [0, 1] }", "over: { p: [0, 0.5] }")
        found = with_rule(type_check(parse(text)).errors, "I-9")
        assert found and "p=0.25" in found[0]
        text = CE_B.replace("over: { p: [0, 1] }", "over: { p: [0.5, 1] }")
        assert "p=0.5" in with_rule(type_check(parse(text)).errors, "I-9")[0]


# ---------------------------------------------------------------------------
# I-9, I-10, I-11
# ---------------------------------------------------------------------------

class TestQueries:
    def entry(self, **kw):
        return lambda m, s: m["equilibria"][1].update(**kw)

    def test_i9_id(self):
        assert errors(lambda m, s: m["equilibria"][0].update(id="P"), "I-9")
        assert errors(lambda m, s: m["equilibria"][0].update(id="REACH"), "S-2")
        assert errors(lambda m, s: m["equilibria"][0].update(id="eqmap"), "S-2")

    def test_i9_profile(self):
        assert errors(self.entry(profile="none"), "I-9")

    def test_i9_regimes(self):
        assert errors(self.entry(regimes=["X"]), "I-9")
        assert errors(self.entry(regimes=[]), "I-9")
        assert errors(self.entry(regimes=["base", "base"]), "I-9")
        assert not errors(self.entry(regimes=["base"]), "I-9")

    @pytest.mark.parametrize("value", [-0.1, "x", float("inf")])
    def test_i9_epsilon(self, value):
        assert errors(self.entry(epsilon=value), "I-9")

    @pytest.mark.parametrize("over", [
        {}, {"nope": [0, 1]}, {"lam": [1, 1]}, {"lam": [0, 3]}, {"lam": [0]}, "lam",
    ])
    def test_i9_over(self, over):
        assert errors(self.entry(over=over), "I-9")

    @pytest.mark.parametrize("value", [0, 1, -1, "x"])
    def test_i9_resolution(self, value):
        assert errors(self.entry(resolution=value), "I-9")

    def test_i9_resolution_needs_over(self):
        found = errors(lambda m, s: m["equilibria"][0].update(resolution=0.1), "I-9")
        assert any("without 'over'" in e for e in found)

    def test_i9_a_map_is_allowed_without_transitions(self):
        model = model_of()
        assert [d.name for d in model.designs] == ["base"]
        assert model.equilibria[1].over == {"lam": (0, 2)}
        assert model.equilibria[1].resolution == F(1, 10)
        assert model.equilibria[0].resolution is None and not model.equilibria[0].is_map

    def test_i9_open_parameter_in_a_reward_value(self):
        found = errors(lambda m, s: m["rewards"][0]["items"][0].update(value="lam"), "I-9")
        assert any("'over' of an equilibrium map" in e for e in found)

    def test_i9_open_parameter_assigned_by_a_regime(self):
        def mutate(model, sos):
            model["equilibria"][2]["over"] = {"beta": [0, 1]}
        assert any("assigns" in e for e in errors(mutate, "I-9", base=robot))

    def test_i9_well_defined_at_the_corners(self):
        def mutate(model, sos):
            model["commands"][0]["updates"][0]["prob"] = "p + lam"
            model["parameters"][1]["value"] = 0.25
        found = errors(mutate, "I-9")
        assert any("not well-defined at the corner point lam=2" in e for e in found)

    def test_i10_profile_of_a_property_and_of_a_regime_map(self):
        assert errors(lambda m, s: m["properties"][0].update(profile="nope"), "I-10")

        def mutate(model, sos):
            model["regime_maps"] = [{
                "id": "BEST", "over": {"p_fail": [0, 0.5]}, "profile": "nope",
                "objective": {"maximize": "P=? [ F<=3 delivered ]"}}]
        assert errors(mutate, "I-10", base=robot)

    def test_defaults(self):
        model = checked_model(parse_file(ROBOT))
        assert [p.profile for p in model.properties] == ["comply", "shortcut_first"]
        assert [q.profile for q in model.equilibria] == [
            "comply", "shortcut_first", "comply", "comply"]
        assert model.equilibria[0].regimes == ["CENTRALIZED", "DISTRIBUTED"]
        assert model.equilibria[0].epsilon == 0

    def test_i11_reserved(self):
        def mutate(model, sos):
            model["mediator"] = "x"
            model["observations"] = {}
            model["commands"][0]["sync"] = "x"
            model["variables"][0]["owner"] = "A"
            model["equilibria"][0]["concept"] = "spe"
        infos = with_rule(check(mutate).infos, "I-11")
        assert len(infos) == 5 and check(mutate).ok

    def test_i11_replaces_s15(self):
        assert not with_rule(check().infos, "S-15")
        result = check(lambda m, s: m["parameters"].append(
            {"id": "rho", "from": "motivation.rho"}))
        assert with_rule(result.infos, "I-11") and with_rule(result.errors, "S-3")


# ---------------------------------------------------------------------------
# The model under a profile (I.5)
# ---------------------------------------------------------------------------

class TestModelUnderProfile:
    @pytest.mark.parametrize("path", [ROBOT, TWO])
    def test_induced_model_selects_what_the_profile_selects(self, path):
        model = checked_model(parse_file(path))
        index = model.var_index()
        for profile in model.profiles:
            induced = induced_model(model, profile.id)
            assert induced.kind == "dtmc"
            for design in model.designs:
                game = build_game(model, design, [], model.fixed_values(design))
                selected = dict(zip(game.states, game.selection(profile.id)))
                guards = [(compile_state_expr(c.guard, index, {}), c)
                          for c in induced.commands if c.exists_in(design.name)]
                for state in iter_states(model):
                    enabled = [c for g, c in guards if g(state)]
                    assert len(enabled) <= 1                       # S-9 holds
                    if state in selected:
                        i = game.states.index(state)
                        want = selected[state]
                        if want is None:
                            assert not enabled
                        else:
                            assert enabled[0].index == game.commands[i][want].command.index

    def test_properties_have_the_values_of_i12(self):
        from cadl.games import check_properties_game
        got = {(d["design"], p["id"]): (p["value"], p["result"], p["profile"])
               for d in check_properties_game(checked_model(parse_file(ROBOT)))
               for p in d["properties"]}
        s = {"CENTRALIZED": F(15051, 20000), "DISTRIBUTED": F(12339, 20000)}
        for design, x in s.items():
            assert got[(design, "P_DELIVERY")][0] == 1 - (1 - x) ** 3
            assert got[(design, "P_DELIVERY_SHORTCUT")] == (
                1 - (1 - x / 2) * (1 - x) ** 2, None, "shortcut_first")
        assert got[("CENTRALIZED", "P_DELIVERY")][1:] == ("satisfied", "comply")
        assert got[("DISTRIBUTED", "P_DELIVERY")][1:] == ("violated", "comply")

    def test_model_check_command(self, capsys):
        status, out, _ = cli(capsys, "model-check", ROBOT, "--format", "json")
        data = json.loads(out)
        assert status == 1                                  # P_DELIVERY is violated in DISTRIBUTED
        props = data["designs"][0]["properties"]
        assert [(p["id"], p["profile"], p["decimal"]) for p in props] == [
            ("P_DELIVERY", "comply", "0.984848"),
            ("P_DELIVERY_SHORTCUT", "shortcut_first", "0.961808")]
        status, out, _ = cli(capsys, "model-check", ROBOT, "--regime", "DISTRIBUTED")
        assert "0.943796" in out and "0.898534" in out and "(profile shortcut_first)" in out

    def regime_map(self, model, sos):
        model["regime_maps"] = [{
            "id": "BEST", "over": {"p_fail": [0, 0.5]}, "profile": "shortcut_first",
            "objective": {"maximize": "P=? [ F<=3 delivered ]"}, "resolution": 0.1}]

    def test_regime_map_under_a_profile(self, tmp_path, capsys):
        """A regime map of a strategic model compares the regimes under
        one profile (I.5); here CENTRALIZED delivers more often for small
        p_fail and DISTRIBUTED for large p_fail."""
        from cadl.games import model_of_regime_map
        from cadl.stochastic_regions import certified_regime_map
        model = model_of(self.regime_map, base=robot)
        assert model.regime_maps[0].profile == "shortcut_first"
        induced = model_of_regime_map(model, "BEST")
        result = certified_regime_map(induced, "BEST")
        assert result["resolution_reached"]
        for cell in result["cells"]:
            if cell.status != "decided":
                continue
            lo, hi = cell.box["p_fail"]
            for pf in (lo, (lo + hi) / 2, hi):
                values = {}
                for regime, beta in (("CENTRALIZED", F(9, 10)), ("DISTRIBUTED", F(1, 10))):
                    x = beta * F(9, 10) * (1 - pf) * F(9, 10) + (1 - beta) * F(6, 10)
                    values[regime] = 1 - (1 - x / 2) * (1 - x) ** 2
                assert all(values[b] == max(values.values()) for b in cell.best)
        assert {tuple(c.best) for c in result["cells"] if c.status == "decided"} == {
            ("CENTRALIZED",), ("DISTRIBUTED",)}
        path = tmp_path / "m.cadl"
        path.write_text(build(self.regime_map, base=robot), encoding="utf-8")
        status, out, _ = cli(capsys, "model-check", path, "--regime-map", "BEST", "--certify")
        assert status == 0 and "decided, best = CENTRALIZED" in out
        status, out, _ = cli(capsys, "model-check", path, "--regime-map", "BEST", "--grid", "2")
        assert status == 0 and "best at this point" in out


# ---------------------------------------------------------------------------
# IR (I.13) and export (I.11)
# ---------------------------------------------------------------------------

class TestIR:
    def test_fields_and_their_order(self):
        ir = lower_to_ir(parse_file(ROBOT)).stochastic_model
        assert ir["kind"] == "smg"
        assert list(ir)[-5:] == ["players", "profiles", "compliance", "payoffs", "equilibria"]
        assert list(ir)[list(ir).index("regime_maps") + 1] == "players"
        lam = next(p for p in ir["parameters"] if p["id"] == "lambda")
        assert list(lam) == ["id", "source", "range", "value", "open", "open_in_equilibria"]
        assert (lam["open"], lam["open_in_equilibria"]) == (False, True)
        beta = next(p for p in ir["parameters"] if p["id"] == "beta")
        assert beta["open_in_equilibria"] is False
        assert list(ir["commands"][0])[:3] == ["index", "action", "owner"]
        assert [c["owner"] for c in ir["commands"]] == ["R", "R", None]
        assert list(ir["properties"][0]) == ["id", "regimes", "profile", "expr"]
        assert ir["properties"][1]["profile"] == "shortcut_first"

    def test_values_of_i13(self):
        ir = lower_to_ir(parse_file(ROBOT)).stochastic_model
        assert ir["players"] == [{"id": "R", "actors": ["ROBOT[*]"]},
                                 {"id": "D", "actors": ["DISPATCHER"]}]
        assert ir["profiles"] == [
            {"id": "comply", "rules": [{"guard": "true", "choose": "careful"}]},
            {"id": "shortcut_first", "rules": [{"guard": "attempts == 0", "choose": "careless"},
                                               {"guard": "true", "choose": "careful"}]}]
        assert ir["compliance"] == "comply"
        assert ir["payoffs"][0] == {
            "player": "R", "until": "settled",
            "terms": [{"reward": "fee", "weight": "10"}, {"reward": "effort", "weight": "-1"},
                      {"reward": "shortcuts", "weight": "-2 * lambda"}]}
        assert ir["equilibria"][0] == {
            "id": "comply_nash", "profile": "comply", "regimes": ["CENTRALIZED", "DISTRIBUTED"],
            "epsilon": 0, "over": None, "resolution": None}
        assert ir["equilibria"][2] == {
            "id": "comply_by_lambda", "profile": "comply",
            "regimes": ["CENTRALIZED", "DISTRIBUTED"],
            "epsilon": 0, "over": {"lambda": [0, 1]}, "resolution": 0.05}

    def test_regime_map_has_a_profile(self):
        ir = game_to_ir(model_of(TestModelUnderProfile().regime_map, base=robot))
        assert list(ir["regime_maps"][0])[:4] == ["id", "over", "regimes", "profile"]

    def test_ir_of_a_model_of_appendix_h_is_unchanged(self):
        ir = lower_to_ir(parse_file(EXAMPLES / "stochastic_robot_delivery.cadl")).stochastic_model
        assert "players" not in ir and "owner" not in ir["commands"][0]
        assert "profile" not in ir["properties"][0]
        assert "open_in_equilibria" not in ir["parameters"][0]

    def test_sim_ir_command(self, capsys):
        status, out, _ = cli(capsys, "sim-ir", TWO, "--format", "json")
        assert status == 0
        model = json.loads(out)["stochastic_model"]
        assert model["kind"] == "smg" and len(model["equilibria"]) == 3


class TestExport:
    def test_header_and_no_properties_file(self):
        files = export_prism(checked_model(parse_file(TWO)))
        assert sorted(files) == ["RobotDeliveryMonitoring_CENTRALIZED.prism",
                                 "RobotDeliveryMonitoring_DISTRIBUTED.prism"]
        text = files["RobotDeliveryMonitoring_CENTRALIZED.prism"]
        # R is a keyword of PRISM: renamed by the rule of H.11.
        assert text.startswith(
            "smg\n\nplayer R_\n  [careful], [careless]\nendplayer\n\n"
            "player D\n  [monitor], [trust]\nendplayer\n\nconst double p_fail = 0.05;\n")
        assert "dtmc" not in text
        # Open in an equilibrium map only: written with its fixed value.
        assert "const double lambda = 0.1;" in text and "const double kappa = 0.05;" in text
        # All commands are written, whatever a profile selects.
        for label in ("[monitor]", "[trust]", "[careful]", "[careless]", "[settle]"):
            assert f"  {label} " in text

    def test_player_without_a_command_is_not_written(self):
        text = export_prism(checked_model(parse_file(ROBOT)))["RobotDeliveryGame_CENTRALIZED.prism"]
        assert "player R_" in text and "player D" not in text

    def test_parameter_open_in_a_regime_map_stays_open(self):
        model = model_of(TestModelUnderProfile().regime_map, base=robot)
        text = export_prism(model, "CENTRALIZED")["RobotDeliveryGame_CENTRALIZED.prism"]
        assert "const double p_fail;" in text and "const double lambda = 0.1;" in text

    def test_command(self, tmp_path, capsys):
        status, out, _ = cli(capsys, "model-export", ROBOT, "-o", tmp_path)
        assert status == 0
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "RobotDeliveryGame_CENTRALIZED.prism", "RobotDeliveryGame_DISTRIBUTED.prism"]

    def test_reports_a_player_named_like_another_identifier(self, tmp_path, capsys):
        def mutate(model, sos):
            model["players"][0]["id"] = "loc"
            for c in model["commands"][:2]:
                c["owner"] = "loc"
            model["payoffs"][0]["player"] = "loc"
        path = tmp_path / "m.cadl"
        path.write_text(build(mutate), encoding="utf-8")
        status, _, err = cli(capsys, "model-export", path, "-o", tmp_path)
        assert status == 0 and "the player 'loc' has the name of the variable 'loc'" in err


# ---------------------------------------------------------------------------
# Equilibrium checks: the numbers of I.12
# ---------------------------------------------------------------------------

ROBOT_MODEL = checked_model(parse_file(ROBOT))
TWO_MODEL = checked_model(parse_file(TWO))

def players_of(result: dict, design: str) -> dict:
    d = next(d for d in result["designs"] if d["design"] == design)
    return {p["player"]: p for p in d["players"]}, d


class TestFirstExample:
    @pytest.fixture()
    def model(self):
        return ROBOT_MODEL

    def test_the_game(self, model):
        assert len(list(iter_states(model))) == 16
        game = build_game(model, model.designs[0], [], model.fixed_values(model.designs[0]))
        assert len(game.states) == 11
        assert sum(1 for o in game.owner if o == "R") == 3
        assert "D" not in game.owner

    def test_comply_nash(self, model):
        result = equilibrium_check(model, "comply_nash")
        players, d = players_of(result, "CENTRALIZED")
        assert players["R"]["payoff"] == F(6831840915651, 800000000000)
        assert players["R"]["best_response_value"] == F(13824028895651, 1600000000000)
        assert players["R"]["gain"] == F(160347064349, 1600000000000)
        assert players["R"]["best_response"] == [
            {"state": {"task": "Pending", "attempts": 0, "settled": False}, "action": "careless"}]
        assert players["D"]["payoff"] == players["D"]["best_response_value"] \
            == F(7878786117651, 1600000000000)
        assert players["D"]["gain"] == 0 and players["D"]["best_response"] == []
        assert (d["status"], d["deviators"], d["from_every_state"]) == (
            "not_equilibrium", ["R"], None)
        assert d["parameters"]["lambda"] == F(1, 10) and d["parameters"]["beta"] == F(9, 10)
        players, d = players_of(result, "DISTRIBUTED")
        assert players["R"]["payoff"] == players["R"]["best_response_value"] \
            == F(6326547012219, 800000000000)
        assert players["R"]["gain"] == 0
        assert round(players["D"]["payoff"], 6) == F("4.718981")
        assert (d["status"], d["deviators"], d["from_every_state"]) == ("equilibrium", [], True)

    def test_shortcut_nash(self, model):
        result = equilibrium_check(model, "shortcut_nash")
        players, d = players_of(result, "CENTRALIZED")
        assert round(players["R"]["payoff"], 6) == F("8.640018")
        assert round(players["D"]["payoff"], 6) == F("4.809042")
        assert players["R"]["gain"] == 0
        assert (d["status"], d["from_every_state"]) == ("equilibrium", True)
        players, d = players_of(result, "DISTRIBUTED")
        assert round(players["R"]["payoff"], 6) == F("7.828930")
        assert round(players["D"]["payoff"], 6) == F("4.492672")
        assert players["R"]["gain"] == F(126805432219, 1600000000000)
        assert (d["status"], d["deviators"]) == ("not_equilibrium", ["R"])
        assert players["R"]["best_response"] == [
            {"state": {"task": "Pending", "attempts": 0, "settled": False}, "action": "careful"}]

    def test_expected_rewards_under_the_profiles(self, model):
        """The row of I.12 with the expected fee, effort and shortcuts."""
        want = {
            ("comply", "CENTRALIZED"): ("0.984848", "1.308682", "0"),
            ("comply", "DISTRIBUTED"): ("0.943796", "1.529777", "0"),
            ("shortcut_first", "CENTRALIZED"): ("0.961808", "0.778066", "1"),
            ("shortcut_first", "DISTRIBUTED"): ("0.898534", "0.956414", "1"),
        }
        from cadl.stochastic import parse_property
        from cadl.stochastic import _Analyser  # noqa: F401  (the labels are resolved below)
        for (profile, design), numbers in want.items():
            induced = induced_model(model, profile)
            d = model.design(design)
            chain = build_chain(induced, d, model.fixed_values(d))
            for reward, number in zip(("fee", "effort", "shortcuts"), numbers):
                prop = parse_property(f'R{{"{reward}"}}=? [ F settled ]')
                prop.right = ("var", "settled")
                assert round(evaluate(chain, prop), 6) == F(number)

    def test_threshold_lambda_star(self, model):
        """gain_R = max(0, 2 (λ* − λ)) with λ* = T_3 (I.12)."""
        for design, star in (("CENTRALIZED", F(480347064349, 3200000000000)),
                             ("DISTRIBUTED", F(193194567781, 3200000000000))):
            for lam in (F(0), F(1, 20), star, star + F(1, 1000), F(1)):
                result = equilibrium_check(model, "comply_nash", design, {"lambda": lam})
                players, d = players_of(result, design)
                assert players["R"]["gain"] == max(0, 2 * (star - lam))
                assert d["status"] == ("equilibrium" if lam >= star else "not_equilibrium")
                # Both profiles are equilibria exactly at λ*.
                other = equilibrium_check(model, "shortcut_nash", design, {"lambda": lam})
                assert (other["designs"][0]["status"] == "equilibrium") == (lam <= star)

    def test_table_of_lambda_star(self, model):
        table = {
            "0": ("0.173313", "0.062908"), "0.05": ("0.150108", "0.060373"),
            "0.1": ("0.125857", "0.057859"), "0.2": ("0.077118", "0.052897"),
            "0.3": ("0.032907", "0.048028"), "0.4": ("0", "0.043261"),
            "0.5": ("0", "0.038604"),
        }
        for pf, stars in table.items():
            for design, star in zip(("CENTRALIZED", "DISTRIBUTED"), stars):
                result = equilibrium_check(model, "comply_nash", design,
                                           {"lambda": F(0), "p_fail": F(pf)})
                gain = result["designs"][0]["players"][0]["gain"]
                assert round(gain / 2, 6) == F(star)        # at λ = 0, gain_R = 2 λ*

    def test_small_s_where_t1_is_the_largest(self, model):
        """s = 0.07695 (DISTRIBUTED with q_local = 0): λ* = T_1 = 0.307625."""
        star = F("0.307625")
        for lam, status in ((star, "equilibrium"), (star - F(1, 10 ** 6), "not_equilibrium")):
            result = equilibrium_check(model, "comply_nash", "DISTRIBUTED",
                                       {"q_local": F(0), "lambda": lam})
            assert result["designs"][0]["status"] == status

    def test_epsilon(self):
        def eps(model, sos):
            model["equilibria"][0]["epsilon"] = 0.2
        result = equilibrium_check(model_of(eps, base=robot), "comply_nash")
        players, d = players_of(result, "CENTRALIZED")
        # The gain 0.100217 is at most ε: an ε-equilibrium, and nothing is
        # said about every state (I.8).
        assert (d["status"], d["deviators"], d["from_every_state"]) == ("equilibrium", [], None)
        assert players["R"]["best_response"] == []


class TestSecondExample:
    @pytest.fixture()
    def model(self):
        return TWO_MODEL

    def test_the_game(self, model):
        assert len(list(iter_states(model))) == 48
        game = build_game(model, model.designs[0], [], model.fixed_values(model.designs[0]))
        assert game.owner[0] == "D"

    def test_comply_nash(self, model):
        result = equilibrium_check(model, "comply_nash")
        players, d = players_of(result, "CENTRALIZED")
        assert players["R"]["payoff"] == F(6831840915651, 800000000000)
        assert players["D"]["payoff"] == F(7798786117651, 1600000000000)
        assert players["R"]["gain"] == F(160347064349, 1600000000000)
        assert players["D"]["gain"] == F(1, 20)
        assert round(players["D"]["best_response_value"], 6) == F("4.924241")
        assert (d["status"], d["deviators"]) == ("not_equilibrium", ["R", "D"])
        assert players["D"]["best_response"] == [{
            "state": {"watch": "Open", "task": "Pending", "attempts": 0, "settled": False},
            "action": "trust"}]
        players, d = players_of(result, "DISTRIBUTED")
        assert players["D"]["payoff"] == F(7470368854219, 1600000000000)
        assert (players["R"]["gain"], players["D"]["gain"]) == (0, F(1, 20))
        assert (d["status"], d["deviators"]) == ("not_equilibrium", ["D"])

    def test_watched_nash(self, model):
        result = equilibrium_check(model, "watched_nash")
        players, d = players_of(result, "CENTRALIZED")
        assert (players["R"]["gain"], players["D"]["gain"]) == (
            F(160347064349, 1600000000000), 0)
        assert (d["status"], d["deviators"]) == ("not_equilibrium", ["R"])
        players, d = players_of(result, "DISTRIBUTED")
        assert (players["R"]["gain"], players["D"]["gain"]) == (0, 0)
        assert (d["status"], d["from_every_state"]) == ("equilibrium", True)

    def test_kappa_star(self, model):
        """gain_D = max(0, κ − κ*) against comply_if_watched."""
        for design, star in (("CENTRALIZED", F(368638137651, 3200000000000)),
                             ("DISTRIBUTED", F(724187274219, 3200000000000))):
            for kappa in (F(0), star, star + F(1, 100), F(2, 5)):
                result = equilibrium_check(model, "watched_nash", design,
                                           {"kappa": kappa, "lambda": F(1, 2)})
                players, d = players_of(result, design)
                assert players["D"]["gain"] == max(0, kappa - star)
                assert d["status"] == ("equilibrium" if kappa <= star else "not_equilibrium")

    def test_d_trusting(self, model):
        """u_D when D trusts, against the two profiles (I.12)."""
        def trusting(profile):
            def mutate(m, s):
                rules = copy.deepcopy(m["profiles"][profile])
                rules[0] = {"choose": "trust"}
                m["profiles"]["t"] = rules
                m["equilibria"].append({"id": "t_eq", "profile": "t"})
            return mutate
        two = lambda: yaml.safe_load(TWO.read_text(encoding="utf-8"))  # noqa: E731
        for profile, values in (("comply", ("4.924241", "4.718981")),
                                ("comply_if_watched", ("4.809042", "4.492672"))):
            result = equilibrium_check(model_of(trusting(profile), base=two), "t_eq")
            got = [round(d["players"][1]["payoff"], 6) for d in result["designs"]]
            assert got == [F(v) for v in values]


# ---------------------------------------------------------------------------
# Best responses
# ---------------------------------------------------------------------------

def brute_force(game, profile: str, player: str) -> tuple:
    """(payoff, best-response value) by evaluating every memoryless
    deterministic strategy of the player with the solver of Appendix H:
    each strategy is written as a profile of its own."""
    proc = build_process(game, player, profile, point_distributions(game, ()))
    reward = point_reward(game, proc, ())
    mine = [i for i in range(len(proc.states)) if proc.mine[i] and not proc.target[i]]
    values = []
    for combo in itertools.product(*[[k for k, _ in proc.options[i]] for i in mine]):
        pick = dict(zip(mine, combo))
        choice = lambda i: [pick.get(i, proc.selected[i])]   # noqa: E731
        values.append(optimal_values(proc, choice, reward)[0])
    own = optimal_values(proc, lambda i: [proc.selected[i]], reward)[0]
    return own, max(values)


class TestBestResponse:
    @pytest.mark.parametrize("path", [ROBOT, TWO])
    def test_against_all_strategies(self, path):
        model = checked_model(parse_file(path))
        for design in model.designs:
            game = build_game(model, design, [], model.fixed_values(design))
            for profile in model.profiles:
                for player in model.players:
                    at = player_at_point(game, profile.id, player.id, ())
                    assert (at.payoff, at.best_response_value) == brute_force(
                        game, profile.id, player.id)

    def canon(self, weights: dict, sigma: str = "a"):
        """One state of A with the commands a, b, c (in this order) whose
        payoffs are the weights, then a second state reached only by c."""
        doc = yaml.safe_load(f"""
sos:
  name: "Canon"
  type: Acknowledged
  extensions: [ {{ stochastic: 0.1 }}, {{ games: 0.1 }} ]
  actors: [ {{ id: ALICE, role: "x" }} ]
  model:
    kind: smg
    variables: [ {{ id: loc, type: "{{S0, S1, End}}", init: S0 }} ]
    players: [ {{ id: A, actors: [ALICE] }} ]
    commands:
      - {{ action: a, owner: A, guard: "loc == S0", updates: [ {{ set: {{ loc: End }} }} ] }}
      - {{ action: b, owner: A, guard: "loc == S0", updates: [ {{ set: {{ loc: End }} }} ] }}
      - {{ action: c, owner: A, guard: "loc == S0", updates: [ {{ set: {{ loc: S1 }} }} ] }}
      - {{ action: x, owner: A, guard: "loc == S1", updates: [ {{ set: {{ loc: End }} }} ] }}
      - {{ action: y, owner: A, guard: "loc == S1", updates: [ {{ set: {{ loc: End }} }} ] }}
    rewards:
      - {{ id: ra, items: [ {{ action: a, value: 1 }} ] }}
      - {{ id: rb, items: [ {{ action: b, value: 1 }} ] }}
      - {{ id: rc, items: [ {{ action: c, value: 1 }} ] }}
      - {{ id: rx, items: [ {{ action: x, value: 1 }} ] }}
      - {{ id: ry, items: [ {{ action: y, value: 1 }} ] }}
    profiles: {{ sigma: [ {{ choose: {sigma} }}, {{ choose: x }} ] }}
    compliance: sigma
    payoffs:
      - player: A
        until: "loc == End"
        terms: []
    equilibria: [ {{ id: eq }} ]
""")
        doc["sos"]["model"]["payoffs"][0]["terms"] = [
            {"reward": f"r{k}", "weight": w} for k, w in weights.items()]
        sos = parse(yaml.safe_dump(doc, sort_keys=False))
        assert type_check(sos).ok
        d = equilibrium_check(checked_model(sos), "eq")["designs"][0]
        return d, [(e["state"]["loc"], e["action"]) for e in d["players"][0]["best_response"]]

    def test_keeps_the_command_of_the_profile_when_it_is_optimal(self):
        d, response = self.canon({"a": 1, "b": 1, "c": 0, "x": 0, "y": 0})
        assert d["status"] == "equilibrium" and response == []
        d, response = self.canon({"a": 1, "b": 1, "c": 0, "x": 0, "y": 0}, sigma="b")
        assert d["status"] == "equilibrium" and response == []

    def test_first_optimal_command_in_declaration_order(self):
        d, response = self.canon({"a": 0, "b": 2, "c": 2, "x": 0, "y": 0})
        assert d["players"][0]["gain"] == 2 and response == [("S0", "b")]
        d, response = self.canon({"a": 2, "b": 2, "c": 1, "x": 0, "y": 0}, sigma="c")
        assert response == [("S0", "a")]

    def test_only_reachable_differences_are_listed(self):
        # y is better than x in S1, but the best response does not go there.
        d, response = self.canon({"a": 0, "b": 5, "c": 1, "x": 0, "y": 1})
        assert response == [("S0", "b")]
        # ... and when it does, both differences are listed, in the order of S.
        d, response = self.canon({"a": 0, "b": 1, "c": 1, "x": 0, "y": 3})
        assert d["players"][0]["gain"] == 4 and response == [("S0", "c"), ("S1", "y")]

    def test_nash_but_not_from_every_state(self):
        """The profile is a Nash equilibrium although A could gain in S1,
        which only A's own deviation reaches (I.7)."""
        d, response = self.canon({"a": 5, "b": 0, "c": 1, "x": 0, "y": 1})
        assert (d["status"], d["from_every_state"], response) == ("equilibrium", False, [])

    def test_canonical_function_on_the_tiny_game(self):
        model = model_of()
        game = build_game(model, model.designs[0], [], model.fixed_values(model.designs[0]))
        proc = build_process(game, "A", "sigma", point_distributions(game, ()))
        reward = point_reward(game, proc, ())
        v_star = optimal_values(proc, _all(proc), reward)
        assert canonical_best_response(proc, v_star, reward) == {}


# ---------------------------------------------------------------------------
# The command (I.13)
# ---------------------------------------------------------------------------

class TestCommand:
    def test_checks_as_json(self, capsys):
        status, out, _ = cli(capsys, "game-check", ROBOT, "--format", "json")
        data = json.loads(out)
        assert status == 0                    # computed, whatever the answers
        assert data["maps_not_computed"] == ["comply_by_lambda", "comply_by_lambda_and_failure"]
        check_ = data["checks"][0]
        assert (check_["id"], check_["profile"], check_["epsilon"]) == ("comply_nash", "comply", "0")
        central = check_["designs"][0]
        assert list(central) == ["design", "parameters", "status", "deviators",
                                 "from_every_state", "players"]
        assert central["parameters"]["p_fail"] == "1/20" and central["parameters"]["lambda"] == "1/10"
        assert central["status"] == "not_equilibrium" and central["deviators"] == ["R"]
        assert central["from_every_state"] is None
        assert central["players"][0] == {
            "player": "R", "payoff": "6831840915651/800000000000",
            "best_response_value": "13824028895651/1600000000000",
            "gain": "160347064349/1600000000000",
            "best_response": [{"state": {"task": "Pending", "attempts": 0, "settled": False},
                               "action": "careless"}]}
        assert central["players"][1] == {
            "player": "D", "payoff": "7878786117651/1600000000000",
            "best_response_value": "7878786117651/1600000000000", "gain": "0",
            "best_response": []}
        assert check_["designs"][1]["from_every_state"] is True

    def test_text(self, capsys):
        status, out, _ = cli(capsys, "game-check", ROBOT)
        assert status == 0
        assert "gain = 0.100217  (exact: 160347064349/1600000000000)" in out
        assert "status: not_equilibrium; deviators: R" in out
        assert "canonical best response: careless in (task=Pending, attempts=0, settled=false)" in out
        assert "status: equilibrium; from_every_state: true" in out
        assert "Equilibrium maps not computed (give --certify)" in out

    def test_selection(self, capsys):
        status, out, _ = cli(capsys, "game-check", ROBOT, "--equilibrium", "shortcut_nash",
                             "--regime", "DISTRIBUTED", "--format", "json")
        data = json.loads(out)
        assert status == 0 and [c["id"] for c in data["checks"]] == ["shortcut_nash"]
        assert [d["design"] for d in data["checks"][0]["designs"]] == ["DISTRIBUTED"]

    def test_set(self, capsys):
        status, out, _ = cli(capsys, "game-check", ROBOT, "--equilibrium", "comply_nash",
                             "--set", "lambda=0.2", "--format", "json")
        data = json.loads(out)
        assert [d["status"] for d in data["checks"][0]["designs"]] == ["equilibrium"] * 2
        assert data["checks"][0]["designs"][0]["parameters"]["lambda"] == "1/5"

    @pytest.mark.parametrize("args, message", [
        (["--set", "lambda=2"], "outside its declared range"),
        (["--set", "nope=1"], "not a parameter"),
        (["--set", "lambda"], "expects p=v"),
        (["--equilibrium", "nope"], "not an entry of model.equilibria"),
        (["--equilibrium", "comply_by_lambda"], "needs --certify"),
        (["--regime", "nope"], "not a design"),
        (["--max-cells", "10"], "need --certify"),
        (["--certify", "--equilibrium", "comply_by_lambda", "--set", "lambda=0.5"],
         "is open in the equilibrium map"),
        (["--certify", "--equilibrium", "comply_by_lambda", "--partition", "0"],
         "--partition must be at least 1"),
    ])
    def test_exit_status_2_usage(self, capsys, args, message):
        status, out, err = cli(capsys, "game-check", ROBOT, *args)
        assert status == 2 and message in err and not out

    def test_exit_status_2_static_error(self, tmp_path, capsys):
        path = tmp_path / "bad.cadl"
        path.write_text(build(lambda m, s: m.update(compliance="none")), encoding="utf-8")
        status, _, err = cli(capsys, "game-check", path)
        assert status == 2 and "(I-6)" in err

    def test_exit_status_2_not_a_strategic_model(self, capsys):
        status, _, err = cli(capsys, "game-check", EXAMPLES / "stochastic_robot_delivery.cadl")
        assert status == 2 and "not a strategic model" in err

    def test_exit_status_2_not_well_defined_under_set(self, tmp_path, capsys):
        def mutate(model, sos):
            model["commands"][0]["updates"][0]["prob"] = "p + lam"
            model["parameters"][1]["value"] = 0.25
            model["equilibria"].pop()
        path = tmp_path / "m.cadl"
        path.write_text(build(mutate), encoding="utf-8")
        assert cli(capsys, "game-check", path)[0] == 0
        status, _, err = cli(capsys, "game-check", path, "--set", "lam=1")
        assert status == 2 and "not well-defined" in err

    def test_exit_status_2_not_admissible_under_set(self, tmp_path, capsys):
        """With p = 1 the command `back` keeps the game in S1 for ever."""
        def mutate(model, sos):
            model["commands"][0]["updates"][0]["prob"] = 0.5
            model["commands"][3] = {
                "action": "back", "owner": "B", "guard": "loc == S1",
                "updates": [{"prob": "p", "set": {"loc": "S1"}}, {"prob": "rest", "set": {"loc": "Goal"}}]}
            model["equilibria"].pop()
        path = tmp_path / "m.cadl"
        path.write_text(build(mutate), encoding="utf-8")
        assert cli(capsys, "game-check", path)[0] == 0
        status, _, err = cli(capsys, "game-check", path, "--set", "p=1")
        assert status == 2 and "not admissible for the player 'B'" in err
        with pytest.raises(EvaluationError):
            equilibrium_check(checked_model(parse_file(path)), "eq", None, {"p": F(1)})
