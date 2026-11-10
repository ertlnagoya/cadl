"""Stochastic model extension (Appendix H, stochastic 0.1): the three
expression sub-languages of H.2, rules S-1 to S-15 of H.10, the IR of
H.13, the PRISM export of H.11 and the internal reference evaluator."""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from cadl.cli import _ir_to_dict, main
from cadl.parser import parse, parse_file
from cadl.sim import lower_to_ir
from cadl.stochastic import (
    PRISM_KEYWORDS,
    STATE_SPACE_LIMIT,
    ExportError,
    StochasticSyntaxError,
    analyse,
    checked_model,
    decimal_text,
    export_prism,
    iter_states,
    parse_param_expr,
    parse_property,
    parse_state_pred,
    eval_param_expr,
    yaml_number,
)
from cadl.stochastic_eval import (
    ESTIMATE_NOTE,
    INFINITY,
    EvaluationError,
    build_chain,
    check_properties,
    evaluate,
    evaluate_map_point,
    regime_map_estimate,
    valuation,
    verdict,
)
from cadl.type_checker import type_check

EXAMPLE = Path(__file__).parent.parent / "examples" / "stochastic_robot_delivery.cadl"
BASE = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def build(mutate=None, declare=True, transitions=True) -> str:
    """The example of H.12 as source text, after `mutate(model, sos)`."""
    data = copy.deepcopy(BASE)
    sos = data["sos"]
    if not declare:
        del sos["extensions"]
    if not transitions:
        del sos["transitions"]
        model = sos["model"]
        del model["regimes"], model["regime_maps"]
        model["properties"] = [p for p in model["properties"] if "regimes" not in p]
        model["parameters"][3] = {"id": "beta", "range": [0, 1], "value": 0.9}
    if mutate is not None:
        mutate(sos["model"], sos)
    return yaml.safe_dump(data, sort_keys=False)


def check(mutate=None, **kw):
    return type_check(parse(build(mutate, **kw)))


def with_rule(items, rule: str) -> list:
    return [str(i) for i in items if f"({rule})" in str(i)]


def errors(mutate=None, rule: str = "", **kw) -> list:
    return with_rule(check(mutate, **kw).errors, rule)


def model_of(mutate=None, **kw):
    sos = parse(build(mutate, **kw))
    result = type_check(sos)
    assert result.ok, [str(e) for e in result.errors]
    return checked_model(sos)


def F(text: str) -> Fraction:
    return Fraction(text)


def r6(value) -> Fraction:
    return Fraction(round(value * 10 ** 6), 10 ** 6)


# ---------------------------------------------------------------------------
# The sub-languages of H.2
# ---------------------------------------------------------------------------

class TestStatePred:
    def test_precedence_and_parentheses(self):
        tree = parse_state_pred("a == 1 OR NOT b AND c < 2 + 3 * d")
        assert tree[:2] == ("bin", "OR")
        assert tree[3][:2] == ("bin", "AND")
        assert tree[3][2][0] == "not"
        cmp_ = tree[3][3]
        assert cmp_[1] == "<" and cmp_[3][1] == "+" and cmp_[3][3][1] == "*"
        assert parse_state_pred("(a OR b) AND c")[2][0] == "par"

    def test_literals_and_unary_minus(self):
        assert parse_state_pred("true") == ("bool", True)
        assert parse_state_pred("-3") == ("neg", ("int", 3, "3"))
        assert parse_state_pred("x - -1")[3] == ("neg", ("int", 1, "1"))

    def test_keyword_boundaries(self):
        assert parse_state_pred("ANDx") == ("id", "ANDx")
        assert parse_state_pred("NOTE == 1")[2] == ("id", "NOTE")

    @pytest.mark.parametrize("text", [
        "x / 2", "x == 1.5", "f(x)", "a.b == 1", "x[1] == 0", 'x == "s"', "x == 5s",
        "a == b == c", "forall i in 1..3 : x", "x ==", "", "a U b", "x AND", "3x",
    ])
    def test_rejects_what_h2_excludes(self, text):
        with pytest.raises(StochasticSyntaxError):
            parse_state_pred(text)


class TestParamExpr:
    def test_polynomial(self):
        tree = parse_param_expr("beta * alpha * (1 - p) + (1 - beta) * 0.6")
        theta = {"beta": F("0.9"), "alpha": F("0.9"), "p": F("0.1")}
        assert eval_param_expr(tree, theta) == F("0.789")

    def test_literal_is_the_decimal_written(self):
        assert eval_param_expr(parse_param_expr("0.1 + 0.2"), {}) == F("0.3")
        assert eval_param_expr(parse_param_expr("-0.5 * 2"), {}) == -1

    @pytest.mark.parametrize("text", ["a / b", "a < b", "f(a)", "a +", "rest + 1", "a b", "",
                                      "1e-3", "2E5", "1.5e2", ".5", "3."])
    def test_rejects(self, text):
        with pytest.raises(StochasticSyntaxError):
            parse_param_expr(text)

    def test_yaml_number_is_shortest_round_trip_decimal(self):
        assert yaml_number(0.1) == F("0.1")
        assert yaml_number(1) == 1
        assert yaml_number(True) is None and yaml_number("0.1") is None
        assert yaml_number(float("inf")) is None
        assert decimal_text(F("0.001")) == "0.001"
        assert decimal_text(Fraction(1)) == "1"
        assert decimal_text(yaml_number(1e-05)) == "0.00001"
        assert decimal_text(F("-12.50")) == "-12.5"


class TestProperty:
    def test_query_and_threshold(self):
        p = parse_property("P=? [ F done ]")
        assert (p.quantity, p.op, p.path, p.bound, p.is_query) == ("P", "=?", "F", None, True)
        p = parse_property("P>=0.95 [ F<=3 done ]")
        assert (p.op, p.threshold, p.bound, p.is_query) == (">=", F("0.95"), 3, False)

    def test_until(self):
        p = parse_property("P<0.1 [ x < 2 U y == 1 ]")
        assert (p.path, p.bound, p.left[1], p.right[1]) == ("U", None, "<", "==")
        assert parse_property("P=? [ a U<=7 b ]").bound == 7

    def test_reward(self):
        p = parse_property('R{"delay"}<=1.5 [ F done OR given_up ]')
        assert (p.quantity, p.reward, p.threshold, p.path) == ("R", "delay", F("1.5"), "F")
        assert p.right[:2] == ("bin", "OR")

    def test_f_directly_followed_by_le_is_a_step_bound(self):
        assert parse_property("P=? [ F x <= 3 ]").bound is None
        assert parse_property("P=? [ F<=3 x ]").bound == 3

    @pytest.mark.parametrize("text", [
        "P=? F done", "P=? [ done ]", "P=? [ G done ]", "P [ F done ]", "Q=? [ F done ]",
        "P>=-0.5 [ F done ]", "P=? [ F P>=1 [ F done ] ]", "R=? [ F done ]",
        "P>=1e-1 [ F done ]", "P=? [ F x == 1e2 ]",
        "R{delay}=? [ F done ]", "P=? [ F done ] extra", "P=? [ a U b U c ]", "P==1 [ F a ]",
    ])
    def test_rejects(self, text):
        with pytest.raises(StochasticSyntaxError):
            parse_property(text)


# ---------------------------------------------------------------------------
# The example, declared and undeclared
# ---------------------------------------------------------------------------

class TestExampleAndDeclaration:
    def test_example_has_no_diagnostics(self):
        result = type_check(parse_file(EXAMPLE))
        assert result.ok and not result.warnings and not result.infos

    def test_ast(self):
        sos = parse_file(EXAMPLE)
        m = sos.model
        assert m.kind == "dtmc"
        assert [p.id for p in m.parameters] == ["p_fail", "p_loss", "alpha", "beta", "q_local"]
        assert m.parameters[2].source == "DELIVERY.alpha"
        assert m.variables[0].type == "{Pending, Delivered}"
        assert m.commands[0].action == "attempt" and len(m.commands[0].updates) == 2
        assert m.commands[0].updates[1].prob == "rest"
        assert m.rewards[0].items[0].value == 1
        assert m.properties[2].regimes == ["CENTRALIZED"]
        assert m.regime_maps[0].objective == {"maximize": "P=? [ F<=3 delivered ]"}

    def test_undeclared_is_one_warning_and_not_interpreted(self):
        def broken(m, sos):
            m["kind"] = "nonsense"
            m["variables"] = []
        result = check(broken, declare=False)
        assert result.ok
        assert len(result.warnings) == 1
        assert "stochastic: 0.1" in str(result.warnings[0])
        assert "H.15" in str(result.warnings[0])
        sos = parse(build(declare=False))
        assert checked_model(sos) is None
        assert _ir_to_dict(lower_to_ir(sos))["stochastic_model"] is None

    def test_undeclared_is_not_exported_or_evaluated(self, tmp_path, capsys):
        path = tmp_path / "u.cadl"
        path.write_text(build(declare=False), encoding="utf-8")
        assert main(["model-export", str(path), "-o", str(tmp_path / "out")]) == 1
        assert main(["model-check", str(path)]) == 2
        assert not (tmp_path / "out").exists()
        assert "does not declare" in capsys.readouterr().err

    def test_file_without_model_is_unchanged(self):
        sos = parse("sos:\n  name: T\n  actors:\n    - {id: A, role: a}\n")
        assert sos.model is None
        assert type_check(sos).ok
        assert _ir_to_dict(lower_to_ir(sos))["stochastic_model"] is None

    def test_extension_is_known(self):
        infos = [str(i) for i in check().infos]
        assert not any("not known" in i for i in infos)


# ---------------------------------------------------------------------------
# Rules S-1 to S-15 (the positive case of each rule is the example, which
# has no diagnostics, plus the variants below)
# ---------------------------------------------------------------------------

class TestS1:
    def test_needs_variables(self):
        assert errors(lambda m, s: m.pop("variables"), "S-1")
        assert errors(lambda m, s: m.update(variables=[]), "S-1")

    def test_needs_commands(self):
        assert errors(lambda m, s: m.pop("commands"), "S-1")

    def test_kind(self):
        assert errors(lambda m, s: m.update(kind="ctmc"), "S-1")

    def test_kind_may_be_omitted_and_commands_may_be_empty(self):
        def f(m, s):
            del m["kind"]
        assert check(f).ok
        def g(m, s):
            m["commands"] = []
            m["rewards"] = []
            m["properties"] = [m["properties"][0]]
        assert not errors(g, "S-1")


class TestS2:
    @pytest.mark.parametrize("name", ["rest", "F", "U", "P", "R", "AND", "true", "in", "IN", "9x", "a-b"])
    def test_bad_parameter_id(self, name):
        assert errors(lambda m, s: m["parameters"].append(
            {"id": name, "range": [0, 1], "value": 0}), "S-2")

    def test_structure_keys_are_not_reserved(self):
        # alpha and beta are parameter ids of the example
        assert check().ok

    def test_shared_namespace(self):
        assert errors(lambda m, s: m["parameters"].append(
            {"id": "task", "range": [0, 1], "value": 0}), "S-2")
        assert errors(lambda m, s: m["labels"].update(Pending="attempts == 0"), "S-2")
        assert errors(lambda m, s: m["labels"].update(alpha="attempts == 0"), "S-2")
        assert errors(lambda m, s: m["variables"].append(
            {"id": "attempts", "type": "bool", "init": False}), "S-2")

    def test_literal_may_occur_in_several_types(self):
        def f(m, s):
            m["variables"].append({"id": "other", "type": "{Idle, Pending}", "init": "Idle"})
        assert check(f).ok

    def test_generated_name_must_not_be_declared(self):
        assert errors(lambda m, s: m["variables"].append(
            {"id": "task_Pending", "type": "bool", "init": False}), "S-2")
        assert errors(lambda m, s: m["labels"].update(task_Delivered="attempts == 0"), "S-2")

    def test_generated_names_must_differ_from_each_other(self):
        def f(m, s):
            m["variables"].append({"id": "a", "type": "{b_c}", "init": "b_c"})
            m["variables"].append({"id": "a_b", "type": "{c}", "init": "c"})
        msgs = errors(f, "S-2")
        assert msgs and "a_b_c" in msgs[0]

    @pytest.mark.parametrize("name", ["init", "deadlock"])
    def test_builtin_prism_labels_cannot_be_declared(self, name):
        assert errors(lambda m, s: m["labels"].update({name: "attempts == 3"}), "S-2")

    def test_properties_and_regime_maps_share_a_namespace(self):
        assert errors(lambda m, s: m["properties"].append(
            {"id": "best_for_delivery", "expr": "P=? [ F delivered ]"}), "S-2")
        assert errors(lambda m, s: m["properties"].append(
            {"id": "P_EVENTUAL", "expr": "P=? [ F delivered ]"}), "S-2")

    def test_namespaces_of_their_own(self):
        def f(m, s):
            # a reward, a property and an action may reuse a variable name
            m["rewards"].append({"id": "task", "items": [{"value": 1}]})
            m["properties"].append({"id": "attempts", "expr": "P=? [ F delivered ]"})
            m["commands"][0]["action"] = "alpha"
            m["rewards"][0]["items"][0]["action"] = "alpha"
        assert check(f).ok

    def test_action_shared_by_several_commands(self):
        def f(m, s):
            m["commands"].append({"action": "attempt", "guard": "task == Delivered AND attempts == 0",
                                  "updates": [{"set": {"attempts": 0}}]})
        assert check(f).ok

    def test_action_label_and_reward_id(self):
        assert errors(lambda m, s: m["commands"][0].update(action="rest"), "S-2")
        assert errors(lambda m, s: m["rewards"].append(
            {"id": "delay", "items": [{"value": 1}]}), "S-2")

    def test_yaml_boolean_as_label_key(self):
        src = build().replace("    delivered: task == Delivered", "    ON: task == Delivered")
        assert "    ON: " in src
        msgs = with_rule(type_check(parse(src)).errors, "S-2")
        assert msgs and "YAML 1.1" in msgs[0]


class TestS3:
    @pytest.mark.parametrize("source", [
        "DELIVERY", "DELIVERY.gamma", "NOPE.alpha", "environment.missing",
        "DELIVERY.alpha.x", 5,
    ])
    def test_bad_source(self, source):
        assert errors(lambda m, s: m["parameters"][2].update({"from": source}), "S-3")

    def test_from_with_value(self):
        assert errors(lambda m, s: m["parameters"][2].update(value=0.9), "S-3")

    def test_environment_source(self):
        def f(m, s):
            s["context"] = {"environment": {"loss": 0.1, "name": "x", "flag": True}}
            m["parameters"][1] = {"id": "p_loss", "from": "environment.loss", "range": [0, 0.5]}
        result = check(f)
        assert result.ok, [str(e) for e in result.errors]
        model = checked_model(parse(build(f)))
        p = model.params[1]
        assert (p.source, p.value, p.institutional) == ("environment.loss", F("0.1"), False)

    @pytest.mark.parametrize("key", ["name", "flag"])
    def test_environment_entry_must_be_a_number(self, key):
        def f(m, s):
            s["context"] = {"environment": {"loss": 0.1, "name": "x", "flag": True}}
            m["parameters"][1] = {"id": "p_loss", "from": f"environment.{key}", "range": [0, 0.5]}
        assert errors(f, "S-3")

    def test_lambda_source(self):
        def f(m, s):
            s["contracts"][0]["incentives"] = {"lambda": 0.4}
            m["parameters"].append({"id": "lam", "from": "DELIVERY.lambda"})
        assert check(f).ok
        assert checked_model(parse(build(f))).params[-1].value == F("0.4")

    def test_same_source_twice_is_a_warning(self):
        result = check(lambda m, s: m["parameters"].append(
            {"id": "alpha2", "from": "DELIVERY.alpha"}))
        assert result.ok and with_rule(result.warnings, "S-3")


class TestS4:
    @pytest.mark.parametrize("rng", [[0.5, 0], [0], [0, 1, 2], "0..1", [0, "1e-3"], [0, True]])
    def test_bad_range(self, rng):
        assert errors(lambda m, s: m["parameters"][0].update(range=rng), "S-4")

    def test_range_required_for_free_and_environment_parameters(self):
        assert errors(lambda m, s: m["parameters"][0].pop("range"), "S-4")
        def f(m, s):
            s["context"] = {"environment": {"loss": 0.1}}
            m["parameters"][1] = {"id": "p_loss", "from": "environment.loss"}
        assert errors(f, "S-4")

    def test_institutional_range_within_unit_interval(self):
        assert errors(lambda m, s: m["parameters"][2].update(range=[0, 2]), "S-4")
        assert check(lambda m, s: m["parameters"][2].update(range=[0.5, 1])).ok

    def test_value_in_range(self):
        assert errors(lambda m, s: m["parameters"][0].update(value=0.6), "S-4")
        assert check(lambda m, s: m["parameters"][0].update(value=0.5)).ok

    def test_source_number_in_range(self):
        # alpha of the contract is 0.9
        assert errors(lambda m, s: m["parameters"][2].update(range=[0, 0.5]), "S-4")

    def test_regime_value_in_range(self):
        def f(m, s):
            m["parameters"][3]["range"] = [0.2, 1]
        msgs = errors(f, "S-4")
        assert msgs and "DISTRIBUTED" in msgs[0]


class TestS5:
    @pytest.mark.parametrize("vtype", ["[3..0]", "{}", "{A, A}", "int", "[0..x]", "{A B}"])
    def test_bad_type(self, vtype):
        msgs = check(lambda m, s: m["variables"].append(
            {"id": "v", "type": vtype, "init": 0})).errors
        assert with_rule(msgs, "S-5") or with_rule(msgs, "S-2")

    @pytest.mark.parametrize("vtype,init", [
        ("bool", 1), ("[0..3]", 4), ("[0..3]", True), ("{A, B}", "C"), ("[-2..2]", -3),
    ])
    def test_init_must_be_in_the_domain(self, vtype, init):
        assert errors(lambda m, s: m["variables"].append(
            {"id": "v", "type": vtype, "init": init}), "S-5")

    def test_init_required(self):
        assert errors(lambda m, s: m["variables"].append({"id": "v", "type": "bool"}), "S-5")

    @pytest.mark.parametrize("vtype,init", [
        ("bool", True), ("[-2..2]", -2), ("[5..5]", 5), ("{A}", "A"),
    ])
    def test_good_types(self, vtype, init):
        assert check(lambda m, s: m["variables"].append(
            {"id": "v", "type": vtype, "init": init})).ok

    def test_unquoted_types_are_reported(self):
        src = build().replace("type: '[0..3]'", "type: [0..3]")
        assert "type: [0..3]" in src
        msgs = with_rule(type_check(parse(src)).errors, "S-5")
        assert msgs and "quoted" in msgs[0]
        src = build().replace("type: '{Pending, Delivered}'", "type: {Pending, Delivered}")
        assert "type: {Pending, Delivered}" in src
        msgs = with_rule(type_check(parse(src)).errors, "S-5")
        assert msgs and "quoted" in msgs[0]

    def test_yaml_boolean_as_init(self):
        src = build(lambda m, s: m["variables"].append(
            {"id": "sw", "type": "{ON, OFF}", "init": "ZZZ"})).replace("init: ZZZ", "init: ON")
        msgs = with_rule(type_check(parse(src)).errors, "S-5")
        assert msgs and "YAML 1.1" in msgs[0]
        assert type_check(parse(src.replace("init: ON", 'init: "ON"'))).ok


class TestS6:
    @pytest.mark.parametrize("guard", [
        "task == Pending AND",            # grammar
        "task == 1",                      # enum against integer
        "attempts == Pending",            # literal against integer
        "attempts",                       # not boolean
        "alpha == 1",                     # a parameter
        "delivered",                      # a label outside a property
        "nope == 1",                      # undeclared
        "Pending == Delivered",           # two literals
        "task < Delivered",               # enumerations are not ordered
        "attempts + true == 1",
        "attempts / 2 == 1",
        "task == Unknown",
    ])
    def test_bad_guard(self, guard):
        msgs = errors(lambda m, s: m["commands"][0].update(guard=guard), "S-6")
        assert msgs, guard

    def test_not_kept_as_opaque(self):
        result = check(lambda m, s: m["commands"][0].update(guard="count(x) > 0"))
        assert not result.ok
        assert checked_model(parse(build(
            lambda m, s: m["commands"][0].update(guard="count(x) > 0")))) is None

    def test_label_predicate_refers_to_variables_only(self):
        assert errors(lambda m, s: m["labels"].update(both="delivered OR given_up"), "S-6")

    def test_property_may_use_labels_but_not_parameters(self):
        assert check(lambda m, s: m["properties"].append(
            {"id": "X", "expr": "P=? [ NOT given_up U delivered AND attempts <= 2 ]"})).ok
        assert errors(lambda m, s: m["properties"].append(
            {"id": "X", "expr": "P=? [ F alpha == 1 ]"}), "S-6")

    def test_well_typed_variants(self):
        def f(m, s):
            m["variables"].append({"id": "flag", "type": "bool", "init": False})
            m["variables"].append({"id": "prev", "type": "{Pending, Delivered}", "init": "Pending"})
            m["commands"][0]["guard"] = (
                "(task == Pending) AND attempts * 2 - 1 < 5 AND flag == false "
                "AND Pending == prev AND task == prev AND NOT (attempts != 0 AND flag)")
        result = check(f)
        assert result.ok, [str(e) for e in result.errors]

    def test_yaml_boolean_guard(self):
        def f(m, s):
            m["variables"] = [{"id": "n", "type": "[0..1]", "init": 0}]
            m["labels"] = {"delivered": "n == 1", "given_up": "n == 2"}
            m["commands"] = [{"action": "attempt", "guard": False, "updates": [{"set": {"n": 1}}]}]
        assert check(f).ok


class TestS7:
    def test_set_key_must_be_a_variable(self):
        assert errors(lambda m, s: m["commands"][0]["updates"][0]["set"].update(nope=1), "S-7")
        assert errors(lambda m, s: m["commands"][0]["updates"][0]["set"].update(alpha=1), "S-7")

    @pytest.mark.parametrize("var,value", [
        ("task", 1), ("attempts", "Delivered"), ("attempts", True), ("task", "attempts"),
        ("attempts", 1.5),
    ])
    def test_right_hand_side_has_the_type_of_the_variable(self, var, value):
        assert errors(lambda m, s: m["commands"][0]["updates"][0]["set"].update(
            {var: value}), "S-7")

    def test_value_must_lie_in_the_domain_where_the_guard_is_true(self):
        msgs = errors(lambda m, s: m["commands"][0].update(guard="task == Pending"), "S-7")
        assert msgs and "attempts=3" in msgs[0]

    def test_unreachable_states_count(self):
        # attempts == 3 with task == Delivered is unreachable, but it is in S
        def f(m, s):
            m["commands"].append({"guard": "task == Delivered AND attempts == 3",
                                  "updates": [{"set": {"attempts": "attempts + 1"}}]})
        assert errors(f, "S-7")

    def test_guard_keeps_the_update_in_the_domain(self):
        assert not errors(None, "S-7")

    def test_only_designs_in_which_the_command_exists(self):
        def f(m, s):
            m["commands"].append({"when": ["DISTRIBUTED"], "guard": "task == Delivered AND attempts < 3",
                                  "updates": [{"set": {"attempts": "attempts + 1"}}]})
        assert check(f).ok

    def test_state_space_limit(self):
        def f(m, s):
            for i in range(4):
                m["variables"].append({"id": f"big{i}", "type": "[0..99]", "init": 0})
        msgs = errors(f, "S-7, S-9")
        assert msgs and str(STATE_SPACE_LIMIT) in msgs[0] and "800000000 states" in msgs[0]

    def test_yaml_boolean_as_set_key(self):
        src = build(lambda m, s: m["variables"].append(
            {"id": "ZZZ", "type": "bool", "init": False}))
        src = src.replace("id: ZZZ", 'id: "ON"').replace(
            "      - prob: rest\n        set:\n", "      - prob: rest\n        set:\n          ON: true\n")
        assert "          ON: true" in src
        msgs = with_rule(type_check(parse(src)).errors, "S-7")
        assert msgs and "YAML 1.1" in msgs[0]
        assert type_check(parse(src.replace("          ON: true", '          "ON": true'))).ok


class TestS8:
    def upd(self, *updates):
        return lambda m, s: m["commands"][0].update(updates=[
            dict(u, set={"attempts": "attempts + 1"}) for u in updates])

    def test_needs_an_update(self):
        assert errors(lambda m, s: m["commands"][0].update(updates=[]), "S-8")
        assert errors(lambda m, s: m["commands"][0].pop("updates"), "S-8")

    def test_at_most_one_rest(self):
        assert errors(self.upd({"prob": "rest"}, {}), "S-8")
        assert errors(self.upd({}, {}), "S-8")

    def test_without_rest_no_parameter_and_sum_one(self):
        assert errors(self.upd({"prob": "alpha"}, {"prob": "1 - alpha"}), "S-8")
        assert errors(self.upd({"prob": 0.5}, {"prob": 0.4}), "S-8")
        assert check(self.upd({"prob": 0.1}, {"prob": 0.2}, {"prob": "0.7"})).ok

    def test_sum_is_exact(self):
        # 0.7 + 0.2 + 0.1 is not 1 in binary floating point, but it is exactly
        assert 0.7 + 0.2 + 0.1 != 1.0
        assert check(self.upd({"prob": 0.7}, {"prob": 0.2}, {"prob": 0.1})).ok

    def test_constant_probability_in_unit_interval(self):
        assert errors(self.upd({"prob": 1.5}, {"prob": "rest"}), "S-8")
        assert errors(self.upd({"prob": "0.5 - 1"}, {}), "S-8")

    @pytest.mark.parametrize("prob", ["alpha / 2", "nope", "task", "alpha <", True, [1]])
    def test_bad_probability(self, prob):
        assert errors(self.upd({"prob": prob}, {}), "S-8")

    def test_single_update_by_omission(self):
        assert check(self.upd({})).ok


class TestS9:
    def overlap(self, **extra):
        def f(m, s):
            m["commands"].append(dict(
                {"guard": "task == Pending AND attempts == 0", "updates": [{}]}, **extra))
        return f

    def test_two_enabled_commands(self):
        msgs = errors(self.overlap(), "S-9")
        assert msgs and "Commands 1 and 2" in msgs[0]

    def test_unreachable_states_count(self):
        def f(m, s):
            m["commands"].append({"guard": "task == Delivered AND attempts == 0", "updates": [{}]})
            m["commands"].append({"guard": "task == Delivered AND attempts < 2", "updates": [{}]})
        assert errors(f, "S-9")

    def test_per_design(self):
        def f(m, s):
            m["commands"][0]["when"] = ["CENTRALIZED"]
            self.overlap(when=["DISTRIBUTED"])(m, s)
        assert check(f).ok
        def g(m, s):
            m["commands"][0]["when"] = ["CENTRALIZED", "DISTRIBUTED"]
            self.overlap(when=["DISTRIBUTED"])(m, s)
        msgs = errors(g, "S-9")
        assert msgs and "DISTRIBUTED" in msgs[0]

    def test_command_in_no_design_is_only_the_s10_error(self):
        result = check(self.overlap(when=["NOWHERE"]))
        assert with_rule(result.errors, "S-10")
        assert not with_rule(result.warnings, "S-9")
        assert not with_rule(result.errors, "S-9")


class TestS10:
    def test_regime_key_must_be_a_regime(self):
        assert errors(lambda m, s: m["regimes"].update(HYBRID={"parameters": {"beta": 0.5}}), "S-10")

    def test_when_must_name_regimes(self):
        assert errors(lambda m, s: m["commands"][0].update(when=["HYBRID"]), "S-10")
        assert check(lambda m, s: m["commands"][0].update(when=["CENTRALIZED", "DISTRIBUTED"])).ok

    def test_regime_parameters(self):
        assert errors(lambda m, s: m["regimes"]["CENTRALIZED"]["parameters"].update(nope=1), "S-10")
        assert errors(lambda m, s: m["regimes"]["CENTRALIZED"]["parameters"].update(beta="high"), "S-10")

    def test_without_transitions(self):
        result = check(transitions=False)
        assert result.ok and not result.warnings, [str(e) for e in result.errors]
        assert errors(lambda m, s: m.update(regimes={"A": {}}), "S-10", transitions=False)
        assert errors(lambda m, s: m["commands"][0].update(when=["A"]), "S-10", transitions=False)
        assert errors(lambda m, s: m.update(regime_maps=[copy.deepcopy(
            BASE["sos"]["model"]["regime_maps"][0])]), "S-10", transitions=False)

    def test_regime_without_entry_is_a_warning(self):
        def f(m, s):
            del m["regimes"]["DISTRIBUTED"]
        result = check(f)
        assert result.ok and with_rule(result.warnings, "S-10")

    def test_entry_without_parameters_assigns_nothing(self):
        def f(m, s):
            m["regimes"]["DISTRIBUTED"] = {}
        result = check(f)
        assert result.ok and not result.warnings

    def test_yaml_boolean_as_regime_key(self):
        src = build().replace("      DISTRIBUTED:\n        parameters:", "      ON:\n        parameters:")
        assert "      ON:\n" in src
        msgs = with_rule(type_check(parse(src)).errors, "S-10")
        assert msgs and "YAML 1.1" in msgs[0]


class TestS11:
    def test_every_parameter_needs_a_fixed_value(self):
        msgs = errors(lambda m, s: m["parameters"][4].pop("value"), "S-11")
        assert msgs and "q_local" in msgs[0]

    def test_bound_parameter_without_number_in_the_contract(self):
        def f(m, s):
            del s["contracts"][0]["information"]
        msgs = errors(f, "S-11")
        assert msgs and "alpha" in msgs[0]

    def test_regime_may_supply_the_value(self):
        def f(m, s):
            del s["contracts"][0]["authority"]["beta"]
        assert check(f).ok
        def g(m, s):
            f(m, s)
            del m["regimes"]["DISTRIBUTED"]["parameters"]["beta"]
        msgs = errors(g, "S-11")
        assert msgs and "DISTRIBUTED" in msgs[0] and "CENTRALIZED" not in msgs[0]

    def test_probability_outside_unit_interval_at_fixed_values(self):
        msgs = errors(lambda m, s: m["commands"][0]["updates"][0].update(
            prob="alpha + q_local"), "S-11")
        assert msgs and "1.5" in msgs[0]

    def test_rest_outside_unit_interval(self):
        def f(m, s):
            m["commands"][0]["updates"].insert(
                0, {"prob": "alpha", "set": {"attempts": "attempts + 1"}})
        msgs = errors(f, "S-11")
        assert msgs and "rest" in msgs[0]

    def test_negative_reward(self):
        assert errors(lambda m, s: m["rewards"][0]["items"][0].update(value="alpha - 1"), "S-11")
        assert check(lambda m, s: m["rewards"][0]["items"][0].update(value="1 - alpha")).ok


class TestS12:
    def test_needs_an_item(self):
        assert errors(lambda m, s: m["rewards"].append({"id": "r", "items": []}), "S-12")
        assert errors(lambda m, s: m["rewards"].append({"id": "r"}), "S-12")

    def test_action_must_label_a_command(self):
        assert errors(lambda m, s: m["rewards"][0]["items"][0].update(action="nope"), "S-12")

    @pytest.mark.parametrize("value", ["task", "alpha / 2", "nope", None, True])
    def test_value(self, value):
        assert errors(lambda m, s: m["rewards"][0]["items"][0].update(value=value), "S-12")

    def test_state_reward_with_guard_and_parameter(self):
        assert check(lambda m, s: m["rewards"].append({"id": "cost", "items": [
            {"guard": "task == Pending", "value": "2 * alpha"}, {"value": 0.5}]})).ok

    def test_value_must_not_mention_an_open_parameter(self):
        msgs = errors(lambda m, s: m["rewards"][0]["items"][0].update(value="1 + p_fail"), "S-12")
        assert msgs and "p_fail" in msgs[0]
        # the same parameter is fine when no regime map opens it
        def f(m, s):
            m["rewards"][0]["items"][0]["value"] = "1 + p_fail"
            m["regime_maps"][0]["over"] = {"p_loss": [0, 0.5]}
        assert check(f).ok


class TestS13:
    def prop(self, expr, **kw):
        return lambda m, s: m["properties"].append(dict({"id": "X", "expr": expr}, **kw))

    @pytest.mark.parametrize("expr", [
        "P=? [ G delivered ]", "delivered", "P>=0.9 [ F delivered", 5,
    ])
    def test_grammar(self, expr):
        assert errors(self.prop(expr), "S-13")

    def test_reward_structure_must_be_declared(self):
        assert errors(self.prop('R{"nope"}=? [ F delivered ]'), "S-13")

    def test_reward_path_is_f_without_bound(self):
        assert errors(self.prop('R{"delay"}=? [ F<=3 delivered ]'), "S-13")
        assert errors(self.prop('R{"delay"}=? [ true U delivered ]'), "S-13")
        assert check(self.prop('R{"delay"}=? [ F delivered ]')).ok

    def test_thresholds(self):
        assert errors(self.prop("P>=1.5 [ F delivered ]"), "S-13")
        assert check(self.prop("P<=1 [ F delivered ]")).ok
        assert check(self.prop('R{"delay"}>=7.5 [ F delivered ]')).ok
        # a negative threshold is not in the grammar
        assert errors(self.prop('R{"delay"}>=-1 [ F delivered ]'), "S-13")

    def test_regimes_name_designs_once(self):
        assert errors(self.prop("P=? [ F delivered ]", regimes=["HYBRID"]), "S-13")
        assert errors(self.prop("P=? [ F delivered ]",
                                regimes=["CENTRALIZED", "CENTRALIZED"]), "S-13")
        assert check(self.prop("P=? [ F delivered ]", regimes=["DISTRIBUTED"])).ok

    def test_base_design(self):
        assert check(self.prop("P=? [ F delivered ]", regimes=["base"]), transitions=False).ok
        assert errors(self.prop("P=? [ F delivered ]", regimes=["base"]), "S-13")


class TestS14:
    def rm(self, **kw):
        def f(m, s):
            for k, v in kw.items():
                if v is None:
                    m["regime_maps"][0].pop(k, None)
                else:
                    m["regime_maps"][0][k] = v
        return f

    @pytest.mark.parametrize("over", [
        {}, {"nope": [0, 1]}, {"p_fail": [0.5, 0.5]}, {"p_fail": [0, 0.6]},
        {"beta": [0, 1]}, {"p_fail": [0]}, [0, 1],
        {"p_fail": [0, 0.5], "p_loss": [0, 0.5], "alpha": [0, 1], "q_local": [0, 1],
         "extra": [0, 1]},
    ])
    def test_over(self, over):
        def f(m, s):
            m["parameters"].append({"id": "extra", "range": [0, 1], "value": 0})
            self.rm(over=over)(m, s)
        assert errors(f, "S-14")

    def test_institutional_parameter_may_be_opened(self):
        result = check(self.rm(over={"alpha": [0, 1]}))
        assert result.ok, [str(e) for e in result.errors]

    @pytest.mark.parametrize("regimes", [
        ["CENTRALIZED"], ["CENTRALIZED", "CENTRALIZED"], ["CENTRALIZED", "HYBRID"], [],
    ])
    def test_regimes(self, regimes):
        assert errors(self.rm(regimes=regimes), "S-14")

    def test_regimes_default_to_all(self):
        assert check(self.rm(regimes=None)).ok
        model = model_of(self.rm(regimes=None))
        assert model.regime_maps[0].regimes == ["CENTRALIZED", "DISTRIBUTED"]

    @pytest.mark.parametrize("objective", [
        {}, {"maximize": "P=? [ F delivered ]", "minimize": "P=? [ F delivered ]"},
        {"maximize": "P>=0.5 [ F delivered ]"}, "P=? [ F delivered ]",
    ])
    def test_objective(self, objective):
        assert errors(self.rm(objective=objective), "S-14")

    def test_minimize(self):
        assert check(self.rm(objective={"minimize": 'R{"delay"}=? [ F delivered OR given_up ]'},
                             constraint=None)).ok

    def test_constraint_is_a_threshold(self):
        assert errors(self.rm(constraint="P=? [ F delivered ]"), "S-14")
        assert check(self.rm(constraint=None)).ok

    @pytest.mark.parametrize("r", [0, 1, 1.5, -0.1, "fine"])
    def test_resolution(self, r):
        assert errors(self.rm(resolution=r), "S-14")

    def test_resolution_default(self):
        model = model_of(self.rm(resolution=None))
        assert model.regime_maps[0].resolution == F("0.05")

    def test_well_defined_at_the_corners(self):
        # s = beta*alpha*(1-p_fail)*(1-p_loss)*k + ... leaves [0, 1] at a corner only
        def f(m, s):
            m["commands"][0]["updates"][0]["prob"] = (
                "beta * alpha * (1 - p_fail) * (1 - p_loss) + (1 - beta) * q_local + 0.2 - p_fail")
        msgs = errors(f, "S-14")
        assert msgs and "corner" in msgs[0] and "p_fail=0" in msgs[0]
        assert not errors(f, "S-11")

    def test_yaml_boolean_as_over_key(self):
        src = build().replace("        p_loss:\n        - 0\n", "        ON:\n        - 0\n")
        assert "        ON:\n" in src
        msgs = with_rule(type_check(parse(src)).errors, "S-14")
        assert msgs and "YAML 1.1" in msgs[0]


class TestS15:
    def test_reserved_keys_are_informational(self):
        def f(m, s):
            m["players"] = ["DISPATCHER"]
            m["payoffs"] = {}
            m["equilibria"] = []
            m["variables"][0]["owner"] = "DISPATCHER"
            m["commands"][0]["owner"] = "DISPATCHER"
            m["commands"][0]["sync"] = "x"
        result = check(f)
        assert result.ok and not result.warnings
        assert len(with_rule(result.infos, "S-15")) == 6
        # no meaning: the model is the same
        assert export_prism(checked_model(parse(build(f)))) == export_prism(model_of())

    def test_reserved_values_have_no_meaning(self):
        result = check(lambda m, s: m.update(kind="mdp"))
        assert with_rule(result.infos, "S-15") and with_rule(result.errors, "S-1")
        result = check(lambda m, s: m["parameters"].append(
            {"id": "rho", "from": "motivation.rho", "range": [0, 1]}))
        assert with_rule(result.infos, "S-15") and with_rule(result.errors, "S-3")

    def test_no_diagnostic_without_reserved_keys(self):
        assert not check().infos


# ---------------------------------------------------------------------------
# IR (H.13)
# ---------------------------------------------------------------------------

class TestIR:
    def ir(self, **kw):
        return _ir_to_dict(lower_to_ir(parse(build(**kw))))["stochastic_model"]

    def test_shape_of_h13(self):
        m = self.ir()
        assert list(m) == ["kind", "parameters", "designs", "properties", "variables",
                           "labels", "commands", "rewards", "regime_maps"]
        assert m["kind"] == "dtmc"
        assert m["parameters"][0] == {
            "id": "p_fail", "source": None, "range": [0, 0.5], "value": 0.05, "open": True}
        assert m["parameters"][2] == {
            "id": "alpha", "source": "DELIVERY.alpha", "range": [0, 1], "value": 0.9,
            "open": False}
        assert m["designs"] == [
            {"name": "CENTRALIZED", "parameters": {"beta": 0.9}},
            {"name": "DISTRIBUTED", "parameters": {"beta": 0.1}},
        ]
        assert m["properties"][0] == {
            "id": "P_DELIVERY", "regimes": ["CENTRALIZED", "DISTRIBUTED"],
            "expr": "P>=0.95 [ F<=3 delivered ]"}
        assert m["properties"][2]["regimes"] == ["CENTRALIZED"]
        assert m["variables"] == [
            {"id": "task", "kind": "enum", "lo": None, "hi": None,
             "literals": ["Pending", "Delivered"], "init": "Pending"},
            {"id": "attempts", "kind": "int", "lo": 0, "hi": 3, "literals": [], "init": 0},
        ]
        assert m["labels"] == {"delivered": "task == Delivered",
                               "given_up": "task == Pending AND attempts == 3"}
        assert m["commands"] == [{
            "index": 1, "action": "attempt", "when": [],
            "guard": "task == Pending AND attempts < 3",
            "updates": [
                {"prob": "beta * alpha * (1 - p_fail) * (1 - p_loss) + (1 - beta) * q_local",
                 "set": {"task": "Delivered", "attempts": "attempts + 1"}},
                {"prob": "rest", "set": {"attempts": "attempts + 1"}},
            ],
        }]
        assert m["rewards"] == [{"id": "delay", "items": [
            {"action": "attempt", "guard": "true", "value": "1"}]}]
        assert m["regime_maps"] == [{
            "id": "best_for_delivery", "over": {"p_fail": [0, 0.5], "p_loss": [0, 0.5]},
            "regimes": ["CENTRALIZED", "DISTRIBUTED"], "direction": "maximize",
            "objective": "P=? [ F<=3 delivered ]", "constraint": "P>=0.9 [ F<=3 delivered ]",
            "resolution": 0.05}]
        json.dumps(m)

    def test_defaults_are_filled_in(self):
        def f(m, s):
            del m["kind"]
            del m["commands"][0]["guard"], m["commands"][0]["action"]
            m["commands"][0]["updates"] = [{}]
            m["rewards"] = []
            m["properties"] = m["properties"][:2]
            del m["regime_maps"][0]["resolution"], m["regime_maps"][0]["constraint"]
            del m["regime_maps"][0]["regimes"]
            m["variables"][1] = {"id": "attempts", "type": "bool", "init": False}
            m["labels"] = {"delivered": "task == Delivered"}
        m = self.ir(mutate=f)
        assert m["kind"] == "dtmc"
        c = m["commands"][0]
        assert (c["action"], c["guard"], c["when"]) == (None, "true", [])
        assert c["updates"] == [{"prob": "rest", "set": {}}]
        q = m["regime_maps"][0]
        assert (q["resolution"], q["constraint"]) == (0.05, None)
        assert q["regimes"] == ["CENTRALIZED", "DISTRIBUTED"]
        assert m["variables"][1] == {"id": "attempts", "kind": "bool", "lo": None, "hi": None,
                                     "literals": [], "init": False}

    def test_base_design(self):
        m = self.ir(transitions=False)
        assert m["designs"] == [{"name": "base", "parameters": {}}]
        assert m["properties"][0]["regimes"] == ["base"]
        assert m["regime_maps"] == []
        assert m["parameters"][0]["open"] is False

    def test_null_when_the_block_has_an_error(self):
        assert self.ir(mutate=lambda m, s: m.update(kind="ctmc")) is None

    def test_sim_ir_command(self, capsys):
        assert main(["sim-ir", str(EXAMPLE), "--format", "json"]) == 0
        d = json.loads(capsys.readouterr().out)
        assert d["stochastic_model"]["kind"] == "dtmc"


# ---------------------------------------------------------------------------
# Export (H.11): the listings of H.12, character for character
# ---------------------------------------------------------------------------

H12_MODEL_CENTRALIZED = """\
dtmc

const double p_fail;
const double p_loss;
const double alpha = 0.9;
const double beta = 0.9;
const double q_local = 0.6;
const int task_Pending = 0;
const int task_Delivered = 1;

module main
  task : [0..1] init 0;
  attempts : [0..3] init 0;
  [attempt] task = task_Pending & attempts < 3 -> (beta * alpha * (1 - p_fail) * (1 - p_loss) + (1 - beta) * q_local) : (task'=task_Delivered) & (attempts'=attempts + 1) + (1 - ((beta * alpha * (1 - p_fail) * (1 - p_loss) + (1 - beta) * q_local))) : (attempts'=attempts + 1);
  [] !((task = task_Pending & attempts < 3)) -> true;
endmodule

label "delivered" = (task = task_Delivered);
label "given_up" = (task = task_Pending & attempts = 3);

rewards "delay"
  [attempt] true : (1);
endrewards
"""

H12_PROPS_CENTRALIZED = """\
// const p_fail = 0.05
// const p_loss = 0.1
// P_DELIVERY
P>=0.95 [ F<=3 "delivered" ]
// P_EVENTUAL
P=? [ F "delivered" ]
// P_DELAY
R{"delay"}<=1.5 [ F ("delivered" | "given_up") ]
// best_for_delivery objective
P=? [ F<=3 "delivered" ]
// best_for_delivery constraint
P>=0.9 [ F<=3 "delivered" ]
"""


class TestExport:
    def test_golden_h12(self):
        files = export_prism(checked_model(parse_file(EXAMPLE)))
        assert list(files) == [
            "RobotDeliveryStochastic_CENTRALIZED.prism",
            "RobotDeliveryStochastic_CENTRALIZED.props",
            "RobotDeliveryStochastic_DISTRIBUTED.prism",
            "RobotDeliveryStochastic_DISTRIBUTED.props",
        ]
        assert files["RobotDeliveryStochastic_CENTRALIZED.prism"] == H12_MODEL_CENTRALIZED
        assert files["RobotDeliveryStochastic_CENTRALIZED.props"] == H12_PROPS_CENTRALIZED

    def test_distributed_differs_only_in_beta(self):
        files = export_prism(checked_model(parse_file(EXAMPLE)))
        assert files["RobotDeliveryStochastic_DISTRIBUTED.prism"] == H12_MODEL_CENTRALIZED.replace(
            "const double beta = 0.9;", "const double beta = 0.1;")
        # P_DELAY is restricted to CENTRALIZED
        assert files["RobotDeliveryStochastic_DISTRIBUTED.props"] == H12_PROPS_CENTRALIZED.replace(
            '// P_DELAY\nR{"delay"}<=1.5 [ F ("delivered" | "given_up") ]\n', "")

    def test_command(self, tmp_path, capsys):
        assert main(["model-export", str(EXAMPLE), "--output", str(tmp_path),
                     "--regime", "CENTRALIZED"]) == 0
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "RobotDeliveryStochastic_CENTRALIZED.prism",
            "RobotDeliveryStochastic_CENTRALIZED.props"]
        text = (tmp_path / "RobotDeliveryStochastic_CENTRALIZED.prism").read_text(encoding="utf-8")
        assert text == H12_MODEL_CENTRALIZED
        assert main(["model-export", str(EXAMPLE), "--output", str(tmp_path),
                     "--regime", "HYBRID"]) == 1

    def test_command_refuses_a_file_with_errors(self, tmp_path):
        path = tmp_path / "bad.cadl"
        path.write_text(build(lambda m, s: m.update(kind="ctmc")), encoding="utf-8")
        assert main(["model-export", str(path), "-o", str(tmp_path / "out")]) == 1
        assert not (tmp_path / "out").exists()

    def test_rules_of_h11(self):
        def f(m, s):
            m["variables"] = [
                {"id": "flag", "type": "bool", "init": True},
                {"id": "n", "type": "[-1..2]", "init": 0},
                {"id": "mode", "type": "{Idle, Busy}", "init": "Busy"},
            ]
            m["labels"] = {"delivered": "NOT flag OR (n == 2 AND Busy != mode)"}
            m["commands"] = [
                {"guard": "flag AND n < 2", "when": ["CENTRALIZED"], "updates": [
                    {"prob": 0.25, "set": {"mode": "Idle", "n": "n + 1"}},
                    {"prob": "alpha * 0.5", "set": {"flag": False}},
                    {"prob": "rest"},
                ]},
                {"action": "go", "guard": "NOT flag AND n == -1",
                 "updates": [{"set": {"n": "-n", "flag": "n > 0"}}]},
            ]
            m["rewards"] = [{"id": "cost", "items": [
                {"guard": "mode == Busy", "value": "2 * alpha"}, {"action": "go", "value": 0.5}]}]
            m["properties"] = [
                {"id": "A", "expr": "P=? [ flag U<=4 (n == 2) ]"},
                {"id": "B", "regimes": ["DISTRIBUTED"],
                 "expr": 'R{"cost"}=? [ F NOT flag ]'},
            ]
            m["regime_maps"][0]["objective"] = {"maximize": "P=? [ F delivered ]"}
            del m["regime_maps"][0]["constraint"]
            m["parameters"][0].pop("value")
            m["regimes"]["CENTRALIZED"]["parameters"]["p_fail"] = 0.1
            m["regimes"]["DISTRIBUTED"]["parameters"]["p_fail"] = 0.1
            m["regime_maps"][0]["over"] = {"p_loss": [0, 0.5]}
        files = export_prism(model_of(f))
        assert files["RobotDeliveryStochastic_CENTRALIZED.prism"] == """\
dtmc

const double p_fail = 0.1;
const double p_loss;
const double alpha = 0.9;
const double beta = 0.9;
const double q_local = 0.6;
const int mode_Idle = 0;
const int mode_Busy = 1;

module main
  flag : bool init true;
  n : [-1..2] init 0;
  mode : [0..1] init 1;
  [] flag & n < 2 -> (0.25) : (n'=n + 1) & (mode'=mode_Idle) + (alpha * 0.5) : (flag'=false) + (1 - ((0.25) + (alpha * 0.5))) : true;
  [go] !flag & n = -1 -> (1) : (flag'=n > 0) & (n'=-n);
  [] !((flag & n < 2) | (!flag & n = -1)) -> true;
endmodule

label "delivered" = (!flag | (n = 2 & mode_Busy != mode));

rewards "cost"
  mode = mode_Busy : (2 * alpha);
  [go] true : (0.5);
endrewards
"""
        distributed = files["RobotDeliveryStochastic_DISTRIBUTED.prism"]
        assert "[] flag & n < 2" not in distributed
        assert "  [] !((!flag & n = -1)) -> true;" in distributed
        assert files["RobotDeliveryStochastic_CENTRALIZED.props"] == """\
// const p_loss = 0.1
// A
P=? [ flag U<=4 (n = 2) ]
// best_for_delivery objective
P=? [ F "delivered" ]
"""
        assert '// B\nR{"cost"}=? [ F (!flag) ]\n' in files["RobotDeliveryStochastic_DISTRIBUTED.props"]

    def test_design_without_commands_and_base_design(self):
        def f(m, s):
            m["commands"] = []
            m["rewards"] = []
        files = export_prism(model_of(f, transitions=False))
        assert list(files) == ["RobotDeliveryStochastic_base.prism",
                               "RobotDeliveryStochastic_base.props"]
        text = files["RobotDeliveryStochastic_base.prism"]
        assert "  [] true -> true;\nendmodule\n" in text
        # no regime map: no open parameter
        assert "const double p_fail = 0.05;" in text

    def test_prism_keywords_get_one_underscore(self):
        assert len(PRISM_KEYWORDS) == 55
        def f(m, s):
            m["parameters"].append({"id": "rate", "range": [0, 1], "value": 0.1})
            m["variables"].append({"id": "S", "type": "{init, max}", "init": "init"})
            m["variables"].append({"id": "min", "type": "bool", "init": False})
            m["commands"][0]["action"] = "prob"
            m["rewards"][0]["items"][0]["action"] = "prob"
            m["commands"][0]["updates"][1]["prob"] = "rate"
            m["commands"][0]["updates"].append({"set": {"min": "S == max"}})
        text = export_prism(model_of(f))["RobotDeliveryStochastic_CENTRALIZED.prism"]
        assert "const double rate_ = 0.1;" in text
        assert "const int S_init = 0;" in text and "const int S_max = 1;" in text
        assert "  S_ : [0..1] init 0;" in text and "  min_ : bool init false;" in text
        assert "[prob_] " in text and "(min_'=S_ = S_max)" in text and ": (rate_) :" not in text
        assert "+ (rate_) : (attempts'=attempts + 1)" in text

    def test_two_minus_signs_are_kept_apart(self):
        from cadl.stochastic import tr
        assert tr(parse_state_pred("- -3")) == "- -3"
        assert tr(parse_state_pred("1 - -3")) == "1 - -3"

    def test_collision_after_renaming_is_an_error(self):
        def f(m, s):
            m["parameters"].append({"id": "rate", "range": [0, 1], "value": 0.5})
            m["parameters"].append({"id": "rate_", "range": [0, 1], "value": 0.5})
        with pytest.raises(ExportError, match="rate_"):
            export_prism(model_of(f))

    def test_builtin_function_names_are_escaped(self):
        def f(m, s):
            m["parameters"].append({"id": "log", "range": [0, 1], "value": 0.1})
            m["variables"].append({"id": "mod", "type": "{floor}", "init": "floor"})
            m["commands"][0]["action"] = "pow"
            m["rewards"][0]["items"][0]["action"] = "pow"
        text = export_prism(model_of(f))["RobotDeliveryStochastic_CENTRALIZED.prism"]
        assert "const double log_ = 0.1;" in text and "const int mod_floor = 0;" in text
        assert "  mod_ : [0..0] init 0;" in text and "  [pow_] " in text

    def test_operand_of_f_and_u(self):
        from cadl.stochastic import property_text
        def text(expr):
            m = model_of(lambda m, s: m["properties"].append({"id": "X", "expr": expr}))
            return property_text(m.properties[-1].expr)
        assert text("P=? [ F (attempts == 2) ]") == "P=? [ F (attempts = 2) ]"
        assert text("P=? [ F attempts == 2 ]") == "P=? [ F (attempts = 2) ]"
        assert text("P=? [ F (attempts == 2) OR delivered ]") == \
            'P=? [ F ((attempts = 2) | "delivered") ]'
        assert text("P=? [ (NOT given_up) U<=2 delivered ]") == \
            'P=? [ (!"given_up") U<=2 "delivered" ]'
        assert text("P=? [ F true ]") == "P=? [ F (true) ]"


# ---------------------------------------------------------------------------
# The reference evaluator: the numbers of H.12
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def model():
    return checked_model(parse_file(EXAMPLE))


def cube(s: Fraction) -> Fraction:
    return 1 - (1 - s) ** 3


class TestEvaluator:
    def test_properties_at_the_fixed_values(self, model):
        res = {d["design"]: {p["id"]: p for p in d["properties"]}
               for d in check_properties(model)}
        c, d = res["CENTRALIZED"], res["DISTRIBUTED"]
        # x = 0.855, s_C = 0.75255, s_D = 0.61695
        assert c["P_DELIVERY"]["value"] == 1 - F("0.24745") ** 3
        assert d["P_DELIVERY"]["value"] == 1 - F("0.38305") ** 3
        assert r6(c["P_DELIVERY"]["value"]) == F("0.984848")
        assert r6(d["P_DELIVERY"]["value"]) == F("0.943796")
        assert c["P_DELIVERY"]["result"] == "satisfied"
        assert d["P_DELIVERY"]["result"] == "violated"
        assert c["P_EVENTUAL"]["value"] == c["P_DELIVERY"]["value"]
        assert d["P_EVENTUAL"]["value"] == d["P_DELIVERY"]["value"]
        assert c["P_EVENTUAL"]["result"] is None
        assert c["P_DELAY"]["value"] == 1 + F("0.24745") + F("0.24745") ** 2
        assert str(float(c["P_DELAY"]["value"])).startswith("1.308681")
        assert c["P_DELAY"]["result"] == "satisfied"
        assert "P_DELAY" not in d                      # restricted to CENTRALIZED

    def test_seven_reachable_states_of_eight(self, model):
        d = model.design("CENTRALIZED")
        chain = build_chain(model, d, valuation(model, d))
        assert len(chain.states) == 7
        assert len(list(iter_states(model))) == 8

    def test_delay_on_distributed_is_above_the_bound(self, model):
        d = model.design("DISTRIBUTED")
        chain = build_chain(model, d, valuation(model, d))
        prop = model.properties[2].expr
        value = evaluate(chain, prop)
        assert value == 1 + F("0.38305") + F("0.38305") ** 2
        assert str(float(value)).startswith("1.529777")
        assert verdict(prop, value) == "violated"

    def test_reward_is_infinite_when_the_target_is_not_reached_surely(self, model):
        analysed = model_of(lambda m, s: m["properties"].append(
            {"id": "INF", "expr": 'R{"delay"}<=100 [ F delivered ]'}))
        results = check_properties(analysed, "CENTRALIZED")[0]["properties"]
        assert results[-1]["value"] == INFINITY
        assert results[-1]["result"] == "violated"     # +inf is above every number

    @pytest.mark.parametrize("p_fail,p_loss,pc,pd,best", [
        ("0", "0", "0.997803", "0.949347", ["CENTRALIZED"]),
        ("0.1", "0.1", "0.977118", "0.941994", ["CENTRALIZED"]),
        ("0.2", "1/6", "0.936", "0.936", ["CENTRALIZED", "DISTRIBUTED"]),
        ("0.3", "0.3", "0.839809", "0.928061", ["DISTRIBUTED"]),
        ("0.5", "0.5", "0.598869", "0.916260", ["DISTRIBUTED"]),
    ])
    def test_points_of_h12(self, model, p_fail, p_loss, pc, pd, best):
        q = model.regime_maps[0]
        point = {"p_fail": F(p_fail), "p_loss": F(p_loss)}
        res = evaluate_map_point(model, q, point)
        x = (1 - point["p_fail"]) * (1 - point["p_loss"])
        assert res["regimes"]["CENTRALIZED"]["value"] == cube(F("0.81") * x + F("0.06"))
        assert res["regimes"]["DISTRIBUTED"]["value"] == cube(F("0.09") * x + F("0.54"))
        assert r6(res["regimes"]["CENTRALIZED"]["value"]) == F(pc)
        assert r6(res["regimes"]["DISTRIBUTED"]["value"]) == F(pd)
        assert res["best"] == best
        assert res["regimes"]["DISTRIBUTED"]["constraint"] is True
        # CENTRALIZED violates the constraint exactly at the last two points
        assert res["regimes"]["CENTRALIZED"]["constraint"] is (x >= F("0.6"))

    def test_same_points_through_set(self, model):
        res = check_properties(model, overrides={"p_fail": F("0.2"), "p_loss": Fraction(1, 6)})
        for d in res:
            assert d["properties"][1]["value"] == F("0.936")

    def test_best_regime_flips_across_the_boundary(self, model):
        q = model.regime_maps[0]
        for p_fail in (F(0), F("0.1"), F("0.2"), F("0.3"), Fraction(1, 3)):
            on = 1 - Fraction(2, 3) / (1 - p_fail)     # (1 - p_fail)(1 - p_loss) = 2/3
            eps = F("0.001")
            best = lambda l: evaluate_map_point(model, q, {"p_fail": p_fail, "p_loss": l})["best"]
            assert best(on) == ["CENTRALIZED", "DISTRIBUTED"]
            assert best(on + eps) == ["DISTRIBUTED"]
            if on - eps >= 0:
                assert best(on - eps) == ["CENTRALIZED"]

    def test_constraint_removes_a_candidate_and_infeasible_points(self, model):
        strict = model_of(lambda m, s: m["regime_maps"][0].update(
            constraint="P>=0.95 [ F<=3 delivered ]"))
        q = strict.regime_maps[0]
        # DISTRIBUTED never reaches 0.95; CENTRALIZED does near the origin
        assert evaluate_map_point(strict, q, {"p_fail": F(0), "p_loss": F(0)})["best"] == ["CENTRALIZED"]
        assert evaluate_map_point(strict, q, {"p_fail": F("0.5"), "p_loss": F("0.5")})["best"] == []

    def test_minimize_excludes_infinite_values(self, model):
        m = model_of(lambda m, s: m["regime_maps"][0].update(
            objective={"minimize": 'R{"delay"}=? [ F delivered ]'}, constraint=None))
        res = evaluate_map_point(m, m.regime_maps[0], {"p_fail": F(0), "p_loss": F(0)})
        assert res["regimes"]["CENTRALIZED"]["value"] == INFINITY
        assert res["best"] == []

    def test_grid_estimate_is_labelled(self, model):
        est = regime_map_estimate(model, "best_for_delivery", 2)
        assert est["note"] == ESTIMATE_NOTE == "estimated at sample points; not a certified regime map"
        assert len(est["points"]) == 9
        assert est["points"][0]["point"] == {"p_fail": 0, "p_loss": 0}
        assert est["points"][0]["best"] == ["CENTRALIZED"]
        assert est["points"][-1]["best"] == ["DISTRIBUTED"]
        with pytest.raises(EvaluationError):
            regime_map_estimate(model, "nope", 2)
        with pytest.raises(EvaluationError):
            regime_map_estimate(model, "best_for_delivery", 2, {"p_fail": F("0.1")})

    def test_overrides_are_checked(self, model):
        with pytest.raises(EvaluationError, match="range"):
            check_properties(model, overrides={"p_fail": F("0.9")})
        with pytest.raises(EvaluationError, match="not a parameter"):
            check_properties(model, overrides={"nope": F(0)})
        with pytest.raises(EvaluationError, match="not a design"):
            check_properties(model, "HYBRID")

    def test_cyclic_model_until_and_state_rewards(self):
        # A retry loop: from n == 0, succeed with 1/4, fail for good with 1/4,
        # stay with 1/2. Solved by the linear system, not by unrolling.
        def f(m, s):
            m["variables"] = [{"id": "n", "type": "[0..2]", "init": 0}]
            m["labels"] = {"delivered": "n == 1", "given_up": "n == 2"}
            m["commands"] = [{"action": "attempt", "guard": "n == 0", "updates": [
                {"prob": 0.25, "set": {"n": 1}}, {"prob": 0.25, "set": {"n": 2}},
                {"prob": "rest"}]}]
            m["rewards"] = [{"id": "delay", "items": [
                {"action": "attempt", "value": 1}, {"guard": "n == 0", "value": "2 * alpha"}]}]
            m["properties"] = [
                {"id": "A", "expr": "P=? [ F delivered ]"},
                {"id": "B", "expr": "P=? [ F<=2 delivered ]"},
                {"id": "C", "expr": "P=? [ NOT given_up U delivered ]"},
                {"id": "D", "expr": "P=? [ n == 0 U<=1 given_up ]"},
                {"id": "E", "expr": 'R{"delay"}=? [ F n != 0 ]'},
                {"id": "G", "expr": 'R{"delay"}=? [ F delivered ]'},
                {"id": "H", "expr": "P=? [ F n == 0 ]"},
                {"id": "I", "expr": "P>0.5 [ F delivered ]"},
            ]
            del m["regime_maps"]
        res = {p["id"]: p for p in check_properties(model_of(f), "CENTRALIZED")[0]["properties"]}
        assert res["A"]["value"] == F("1/2")
        assert res["B"]["value"] == F("1/4") + F("1/2") * F("1/4")
        assert res["C"]["value"] == F("1/2")
        assert res["D"]["value"] == F("1/4")
        # two steps expected, each with reward 1 + 2 * 0.9
        assert res["E"]["value"] == 2 * F("2.8")
        assert res["G"]["value"] == INFINITY
        assert res["H"]["value"] == 1
        assert res["I"]["result"] == "violated"       # 1/2 is not above 0.5

    def test_command_text_and_json(self, capsys):
        # DISTRIBUTED violates P_DELIVERY
        assert main(["model-check", str(EXAMPLE)]) == 1
        out = capsys.readouterr().out
        assert "not a model checker" in out
        assert "0.984848" in out and "0.943796" in out and "violated" in out
        assert main(["model-check", str(EXAMPLE), "--format", "json", "--regime", "CENTRALIZED",
                     "--set", "p_fail=0.2", "--set", "p_loss=1/6"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert [d["design"] for d in data["designs"]] == ["CENTRALIZED"]
        assert data["designs"][0]["properties"][1]["value"] == "117/125"
        assert data["designs"][0]["properties"][1]["decimal"] == "0.936"
        assert data["designs"][0]["parameters"]["p_loss"] == "1/6"

    def test_command_regime_map(self, capsys):
        assert main(["model-check", str(EXAMPLE), "--regime-map", "best_for_delivery",
                     "--grid", "1"]) == 0
        out = capsys.readouterr().out
        assert ESTIMATE_NOTE in out and "No cell is decided" in out
        assert "p_fail=0, p_loss=0: CENTRALIZED=0.997803, DISTRIBUTED=0.949347; " \
               "best at this point: CENTRALIZED" in out
        assert main(["model-check", str(EXAMPLE), "--regime-map", "best_for_delivery",
                     "--grid", "1", "--format", "json"]) == 0
        data = json.loads(capsys.readouterr().out)["regime_map_estimate"]
        assert data["note"] == ESTIMATE_NOTE
        assert data["points"][3]["best_at_point"] == ["DISTRIBUTED"]
        assert data["points"][3]["regimes"]["CENTRALIZED"]["constraint"] is False

    def test_command_errors(self, capsys):
        assert main(["model-check", str(EXAMPLE), "--set", "p_fail"]) == 2
        assert main(["model-check", str(EXAMPLE), "--set", "p_fail=abc"]) == 2
        assert main(["model-check", str(EXAMPLE), "--regime-map", "nope"]) == 2
        assert main(["model-check", str(EXAMPLE), "--regime", "HYBRID"]) == 2

    def test_exit_status_counts_thresholds_only(self, capsys):
        # every threshold on CENTRALIZED is satisfied; the query does not count
        assert main(["model-check", str(EXAMPLE), "--regime", "CENTRALIZED"]) == 0
        assert main(["model-check", str(EXAMPLE), "--regime", "DISTRIBUTED"]) == 1
        capsys.readouterr()
        capsys.readouterr()
