"""Amendment extension (Appendix K, amendment 0.1): amendable parameters,
versions and their transitional provisions, measured and scheduled
metrics, decision records, and `cadl amend-diff`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadl.amendment import diff_versions, format_diff_text, ir, load
from cadl.cli import _ir_to_dict, main
from cadl.parser import parse, parse_file
from cadl.sim import lower_to_ir
from cadl.type_checker import type_check

EX = Path(__file__).resolve().parent.parent / "examples"
V1 = (EX / "amendment_robot_town_v1.cadl").read_text(encoding="utf-8")
V2 = (EX / "amendment_robot_town_v2.cadl").read_text(encoding="utf-8")


def edit(text: str, *pairs) -> str:
    for old, new in pairs:
        assert old in text, old
        text = text.replace(old, new)
    return text


def result(text: str):
    return type_check(parse(text))


def errors(text: str) -> list:
    return [str(e) for e in result(text).errors]


def has(items, *needles) -> bool:
    return any(all(n in str(x) for n in needles) for x in items)


def diff(old_text: str, new_text: str, tmp_path) -> dict:
    a, b = tmp_path / "old.cadl", tmp_path / "new.cadl"
    a.write_text(old_text, encoding="utf-8")
    b.write_text(new_text, encoding="utf-8")
    old, new = load(a), load(b)
    assert type_check(old).ok and type_check(new).ok
    return diff_versions(old, new)


def kinds(r: dict, level: str = "error") -> set:
    out = {f["kind"] for f in r["findings"] if f["level"] == level}
    for c in r["contracts"]:
        out |= {f["kind"] for f in c["findings"] if f["level"] == level}
    return out


# --- The examples -----------------------------------------------------------------

class TestExamples:
    def test_both_versions_check(self):
        for text in (V1, V2):
            r = result(text)
            assert r.ok, r.errors
            assert not r.warnings

    def test_amend_diff_of_the_examples(self, capsys):
        code = main(["amend-diff", str(EX / "amendment_robot_town_v1.cadl"),
                     str(EX / "amendment_robot_town_v2.cadl"), "--format", "json"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["ok"]
        changed = [p for p in out["parameters"] if p["changed"]]
        assert [(p["id"], p["old"], p["new"]) for p in changed] == [("block_k", "3", "2")]
        assert [(c["id"], c["status"], c["by"]) for c in out["contracts"]] == [
            ("ROAD_USE", "changed", ["block_k"])]
        assert out["contracts"][0]["transitional"]["rule"] == "switch"
        assert out["effective"] == {"period": 2} and out["previous"] == "1"
        assert "D1" in {d["id"] for d in out["decisions"]}

    def test_text_output(self, capsys):
        main(["amend-diff", str(EX / "amendment_robot_town_v1.cadl"),
              str(EX / "amendment_robot_town_v2.cadl")])
        out = capsys.readouterr().out
        assert "changed  block_k: 3 → 2" in out
        assert "respect the declared amendability" in out

    def test_ir(self):
        d = _ir_to_dict(lower_to_ir(parse(V1)))
        a = d["amendment"]
        assert a["version"] == "1" and a["effective"] == {"period": 1}
        p = {x["id"]: x for x in a["parameters"]}
        assert p["hold_limit"]["kind"] == "duration" and p["hold_limit"]["value"] == 60000
        assert p["hold_limit"]["decided_by"] == "COUNCIL"
        assert p["hold_limit"]["procedure"] == "vote" and p["hold_limit"]["quorum"] == 2
        assert p["to_central"]["decided_by"] == "OPERATOR" and p["to_central"]["safety"]
        assert p["pickup_deadline"]["layer"] == "contract"
        assert a["transitional"] == {"default": "keep", "contracts": [
            {"contract": "ROAD_USE", "rule": "switch", "in_states": []}]}
        m = a["metrics"][0]
        assert m["measure"] == {"window_ms": 3600000, "across_runs": "mean", "runs": 5,
                                "decision": "K1"}
        assert m["schedule"] == [{"from": {"period": 5}, "target": ">= 0.85 * count(orders)",
                                  "decision": "K5"}]
        json.dumps(d)   # serializable

    def test_metrics_of_the_core_ir_unchanged(self):
        d = _ir_to_dict(lower_to_ir(parse(V1)))
        assert d["metrics"][0] == {"id": "on_time_deliveries", "formula": "count(Delivered)",
                                   "target": ">= 0.9 * count(orders)"}


# --- Backward compatibility (K.10) ---------------------------------------------------

class TestWithoutTheExtension:
    @pytest.mark.parametrize("path", sorted(EX.glob("*.cadl")), ids=lambda p: p.name)
    def test_other_examples_have_no_ir_addition(self, path):
        if path.name.startswith("amendment_"):
            pytest.skip("uses the extension")
        sos = parse_file(path)
        assert sos.amendment is None
        assert "amendment" not in _ir_to_dict(lower_to_ir(sos))
        assert not any("K-" in str(m) for m in type_check(sos).warnings)

    def test_undeclared_use_is_a_warning(self):
        text = edit(V1, ("    - amendment: 0.1\n", ""))
        r = result(text)
        assert r.ok
        assert has(r.warnings, "amendment:", "schedule:", "measure:", "K-13")
        assert ir(parse(text)) is None
        assert "amendment" not in _ir_to_dict(lower_to_ir(parse(text)))

    def test_undeclared_errors_are_not_reported(self):
        text = edit(V1, ("    - amendment: 0.1\n", ""), ("decided_by: OPERATOR }", "decided_by: NOBODY }"))
        assert result(text).ok


# --- Rules K-1 to K-12 -------------------------------------------------------------------

class TestRules:
    def test_k1_unknown_key(self):
        assert has(errors(edit(V1, ("    effective: { period: 1 }", "    effective: { period: 1 }\n    when: now"))),
                   "unknown key 'when'", "K-1")

    def test_k2_version_required(self):
        assert has(errors(edit(V1, ('  version: "1"\n', ""))), "sos.version", "K-2")

    def test_k2_previous_is_not_this_version(self):
        assert has(errors(edit(V1, ("    effective: { period: 1 }", '    previous: "1"\n    effective: { period: 1 }'))),
                   "previous", "K-2")

    @pytest.mark.parametrize("value", ["{ period: 0 }", "{ period: 1, at: 2026-01-01 }",
                                       "{ at: tomorrow }", "3"])
    def test_k3_effective(self, value):
        assert has(errors(edit(V1, ("effective: { period: 1 }", f"effective: {value}"))), "K-3")

    def test_k3_effective_at(self):
        assert not errors(edit(V1, ("effective: { period: 1 }",
                                    "effective: { at: 2026-12-01T09:00:00+09:00 }")))
        a = ir(parse(edit(V1, ("effective: { period: 1 }", "effective: { at: 2026-12-01 }"))))
        assert a["effective"] == {"at": "2026-12-01"}

    def test_k4_unknown_contract(self):
        assert has(errors(edit(V1, ("- contract: ROAD_USE", "- contract: ROAD"))),
                   "unknown contract 'ROAD'", "K-4")

    def test_k4_rule(self):
        assert has(errors(edit(V1, ("rule: switch    ", "rule: later     "))), "K-4")
        assert has(errors(edit(V1, ("default: keep", "default: never"))), "K-4")

    def test_k4_in_states(self):
        ok = edit(V1, ("rule: switch    ", "rule: switch\n          in_states: [Holding]\n         "))
        assert not errors(ok)
        assert ir(parse(ok))["transitional"]["contracts"][0]["in_states"] == ["Holding"]
        assert has(errors(edit(V1, ("rule: switch    ", "rule: switch\n          in_states: [Released]\n         "))),
                   "terminal", "K-4")
        assert has(errors(edit(V1, ("rule: switch    ", "rule: switch\n          in_states: [Parked]\n         "))),
                   "not a state", "K-4")
        assert has(errors(edit(V1, ("rule: switch    ", "rule: keep\n          in_states: [Holding]\n         "))),
                   "only with rule: switch", "K-4")

    def test_k5_decisions(self):
        assert has(errors(edit(V1, ('ref: "gsn.yaml#G5"', 'ref: "gsn yaml"'))), "URI", "K-5")
        assert has(errors(edit(V1, ("- id: K5", "- id: K1"))), "more than once", "K-5")
        assert has(errors(edit(V1, ("kind: gsn", "kind: minutes"))), "K-5")

    def test_k6_unknown_actor(self):
        assert has(errors(edit(V1, ("contract: { decided_by: OPERATOR }", "contract: { decided_by: CUSTOMER }"))),
                   "'CUSTOMER' is neither an actor nor a role", "K-6")

    def test_k6_role_is_accepted(self):
        assert not errors(edit(V1, ("contract: { decided_by: OPERATOR }",
                                    "contract: { decided_by: service_operator }")))

    def test_k6_vote_needs_quorum(self):
        assert has(errors(edit(V1, (", quorum: 2 }", " }"))), "needs quorum", "K-6")

    def test_k6_quorum_and_voters(self):
        assert has(errors(edit(V1, ("procedure: vote, quorum: 2 }", "procedure: vote, quorum: 3, voters: [TOWN_STEWARD, COUNCIL] }"))),
                   "larger than the number of voters", "K-6")
        assert has(errors(edit(V1, ("procedure: vote, quorum: 2 }", "procedure: vote, quorum: 2, voters: [CITY] }"))),
                   "'CITY' is not an actor", "K-6")
        assert not errors(edit(V1, ("procedure: vote, quorum: 2 }", 'procedure: vote, quorum: 2, voters: ["ROBOT[*]"] }')))

    def test_k6_empty_layer(self):
        assert has(errors(edit(V1, ("contract: { decided_by: OPERATOR }", "contract: {}"))),
                   "amendment.layers.contract: decided_by: is missing", "K-6")

    def test_k6_nobody_decides(self):
        text = edit(V1, ("      contract: { decided_by: OPERATOR }\n", ""))
        assert has(errors(text), "parameter 'pickup_deadline'", "nobody decides", "K-6")

    def test_k7_layer_and_value(self):
        assert has(errors(edit(V1, ("layer: contract\n        value: 120s\n        min: 60s\n        max: 180s\n        step: 30s\n        at:\n          - contracts.DELIVERY_SLA.lifecycle.transitions.pickup",
                                    "layer: promise\n        value: 120s\n        min: 60s\n        max: 180s\n        step: 30s\n        at:\n          - contracts.DELIVERY_SLA.lifecycle.transitions.pickup"))),
                   "layer must be one of", "K-7")
        assert has(errors(edit(V1, ("value: 60s", "value: soon"))), "value must be a number or a duration", "K-7")
        assert has(errors(edit(V1, ("- id: block_k ", "- id: hold_limit "))), "more than once", "K-7")

    def test_k8_range(self):
        assert has(errors(edit(V1, ("value: 60s", "value: 130s"))), "above max", "K-8")
        assert has(errors(edit(V1, ("value: 60s", "value: 65s"))), "whole number of steps", "K-8")
        assert has(errors(edit(V1, ("min: 10s", "min: 10"))), "min is a number", "K-8")
        assert has(errors(edit(V1, ("step: 10s", "step: 0s"))), "greater than 0", "K-8")

    def test_k8_max_steps(self):
        assert not errors(edit(V1, ("step: 10s\n", "step: 10s\n        max_steps: 3\n")))
        assert has(errors(edit(V1, ("value: 22\n", "value: 22\n        max_steps: 0\n"))), "max_steps", "K-8")

    def test_k9_location_must_resolve(self):
        assert has(errors(edit(V1, ("transitions.release.deadline", "transitions.hold.deadline"))),
                   "does not resolve", "no item 'hold'", "K-9")
        assert has(errors(edit(V1, ("guarantee.1", "guarantee.7"))), "out of range", "K-9")

    def test_k9_location_must_hold_the_value(self):
        text = edit(V1, ("deadline: 60s", "deadline: 70s"))
        assert has(errors(text), "parameter 'hold_limit'", "holds neither the value 60s", "K-9")

    def test_k9_short_names(self):
        text = edit(V1, ("at: [contracts.ROAD_USE.incentives.lambda]", "at: [ROAD_USE.lambda]"))
        assert not errors(text)
        assert has(errors(edit(V1, ("at: [contracts.ROAD_USE.incentives.lambda]", "at: [ROAD_USE.alpha]"))),
                   "does not resolve", "K-9")
        text = edit(V1, ("  actors:\n", "  context:\n    environment:\n      quota: 22\n  actors:\n"),
                    ('at: ["transitions.DISTRIBUTED->CENTRALIZED.condition"]',
                     'at: ["transitions.DISTRIBUTED->CENTRALIZED.condition", environment.quota]'))
        assert not errors(text)

    def test_k9_name_of_the_parameter_counts(self):
        assert not errors(edit(V1, ("at: [contracts.ROAD_USE.lifecycle.transitions.release.deadline]",
                                    "at: [contracts.ROAD_USE.lifecycle.transitions.release.deadline, contracts.ROAD_USE.guarantee]")))

    def test_k9_ambiguous_locations(self):
        text = edit(V1, ("contracts.DELIVERY_SLA.guarantee.0", "contracts.DELIVERY_SLA.guarantee"),
                    ("contracts.DELIVERY_SLA.guarantee.1", "contracts.DELIVERY_SLA.guarantee"))
        r = result(text)
        assert r.ok and has(r.warnings, "'pickup_deadline' and 'dropoff_deadline'", "K-9")

    def test_k9_rule_with_membership(self):
        text = edit(V1, ('"ROBOT.blocking >= 3 AND', '"ROBOT.blocking >= 4 AND'))
        assert has(errors(text), "parameter 'block_k'", "K-9")

    def test_k10_unknown_decision(self):
        assert has(errors(edit(V1, ("decision: K5", "decision: K9"))), "'K9'", "K-10")

    def test_k11_schedule(self):
        two = ('''          decision: K5
    - id: blocked_time''', '''          decision: K5
        - from: { period: 3 }
          target: ">= 0.8 * count(orders)"
    - id: blocked_time''')
        assert has(errors(edit(V1, two)), "come after", "K-11")
        mixed = (two[0], two[1].replace("{ period: 3 }", '{ version: "4" }'))
        assert has(errors(edit(V1, mixed)), "same form", "K-11")
        later = (two[0], two[1].replace("{ period: 3 }", "{ period: 6 }"))
        assert not errors(edit(V1, later))
        assert has(errors(edit(V1, ('target: ">= 0.85 * count(orders)"', "target: [1, 2]"))),
                   "target must be a scalar", "K-11")

    def test_k11_by_version(self):
        text = edit(V1, ("- from: { period: 5 }", '- from: { version: "3" }'))
        assert not errors(text)
        assert ir(parse(text))["metrics"][0]["schedule"][0]["from"] == {"version": "3"}

    def test_k12_measure(self):
        assert has(errors(edit(V1, ("across_runs: mean", "aggregate: mean"))), "needs every", "K-12")
        assert has(errors(edit(V1, ("window: 3600s", "window: 1h\n        every: 2h"))),
                   "longer than window", "K-12")
        assert has(errors(edit(V1, ("window: 3600s", "window: 3600"))), "positive duration", "K-12")
        assert has(errors(edit(V1, ("across_runs: mean", "across_runs: mode"))), "K-12")
        ok = edit(V1, ("window: 3600s", "window: 1h\n        every: 300s\n        aggregate: max"))
        assert not errors(ok)
        assert ir(parse(ok))["metrics"][0]["measure"]["every_ms"] == 300000
        dflt = edit(V1, ("window: 3600s", "window: 1h\n        every: 300s"), ("        across_runs: mean\n", ""))
        meas = ir(parse(dflt))["metrics"][0]["measure"]
        assert meas["aggregate"] == "mean" and meas["across_runs"] == "mean"


# --- `cadl amend-diff` (K.7) ---------------------------------------------------------

class TestAmendDiff:
    def test_identical(self, tmp_path):
        r = diff(V1, V1, tmp_path)
        assert not r["contracts"] and not any(p["changed"] for p in r["parameters"])
        assert "same_version" in kinds(r, "warning")
        assert kinds(r) == {"effective_not_later"}

    def test_two_steps_at_once(self, tmp_path):
        new = edit(V2, ('"ROBOT.blocking >= 2 AND', '"ROBOT.blocking >= 1 AND'),
                   ("        value: 2\n        min: 1", "        value: 1\n        min: 1"))
        r = diff(V1, new, tmp_path)
        assert not r["ok"] and "outside_amendability" in kinds(r)
        assert has(r["errors"], "more than 1 step of 1")

    def test_max_steps_allows_it(self, tmp_path):
        old = edit(V1, ("        step: 1\n        at: [contracts.ROAD_USE.monitors",
                        "        step: 1\n        max_steps: 2\n        at: [contracts.ROAD_USE.monitors"))
        new = edit(old, ('version: "1"', 'version: "2"'), ("{ period: 1 }", "{ period: 2 }"),
                   ('"ROBOT.blocking >= 3 AND', '"ROBOT.blocking >= 1 AND'),
                   ("        value: 3\n        min: 1", "        value: 1\n        min: 1"))
        r = diff(old, new, tmp_path)
        assert r["ok"], r["errors"]

    def test_out_of_the_old_range(self, tmp_path):
        old = edit(V1, ("max: 120s\n        step: 10s", "max: 60s\n        step: 10s"))
        new = edit(V1, ('version: "1"', 'version: "2"'), ("deadline: 60s", "deadline: 70s"),
                   ("value: 60s", "value: 70s"), ("{ period: 1 }", "{ period: 2 }"))
        r = diff(old, new, tmp_path)
        assert has(r["errors"], "hold_limit", "above max 60s")
        assert "amendability_changed" in kinds(r, "warning")

    def test_unexplained_change(self, tmp_path):
        new = edit(V2, ("severity: Minor", "severity: Major"))
        r = diff(V1, new, tmp_path)
        assert not r["ok"]
        assert has(r["errors"], "contracts.ROAD_USE.lifecycle.transitions.release.on_violation",
                   "no declared parameter explains it")

    def test_value_changed_without_the_declaration(self, tmp_path):
        new = edit(V1, ('version: "1"', 'version: "2"'), ("lambda: 0.5", "lambda: 1.0"))
        # The declaration still says 0.5: the new version does not check.
        p = tmp_path / "n.cadl"
        p.write_text(new, encoding="utf-8")
        assert not type_check(load(p)).ok

    def test_unknown_section_change_is_an_error(self, tmp_path):
        new = edit(V2, ("  metrics:\n", "  regimes:\n    CENTRALIZED:\n      parameters: { ROAD_USE.beta: 0.9 }\n  metrics:\n"))
        r = diff(V1, new, tmp_path)
        assert has(r["errors"], "regimes is added")

    def test_metric_change_is_a_warning(self, tmp_path):
        new = edit(V2, ('target: "<= 600s"', 'target: "<= 300s"'))
        r = diff(V1, new, tmp_path)
        assert r["ok"] and has([f["text"] for f in r["findings"] if f["level"] == "warning"],
                               "metrics.blocked_time.target")

    def test_equal_values_written_differently(self, tmp_path):
        new = edit(V2, ("deadline: 60s", "deadline: 1min"), ("lambda: 0.5", "lambda: 0.50"))
        r = diff(V1, new, tmp_path)
        assert r["ok"], r["errors"]

    def test_effective_not_later(self, tmp_path):
        new = edit(V2, ("effective: { period: 2 }", "effective: { period: 1 }"))
        r = diff(V1, new, tmp_path)
        assert "effective_not_later" in kinds(r)

    def test_previous_mismatch(self, tmp_path):
        new = edit(V2, ('previous: "1"', 'previous: "0"'))
        assert "previous_mismatch" in kinds(diff(V1, new, tmp_path), "warning")

    def test_switch_with_a_removed_state(self, tmp_path):
        def drop_overstay(t):
            return edit(t, ("states: [Holding, Overstay, Released, Evicted]",
                            "states: [Holding, Released, Evicted]"),
                        ("from: [Holding, Overstay]", "from: [Holding]"),
                        ("transition: Overstay, severity: Minor", "transition: Evicted, severity: Minor"),
                        ("state IN [Holding, Overstay]", "state IN [Holding]"))
        new = drop_overstay(V2)
        r = diff(V1, new, tmp_path)
        assert "state_removed" in kinds(r)
        # Switching only in a state that remains is allowed (the other
        # changes are still unexplained).
        new2 = edit(new, ("rule: switch    ", "rule: switch\n          in_states: [Holding]\n         "))
        assert "state_removed" not in kinds(diff(V1, new2, tmp_path))
        new3 = edit(new, ("rule: switch    ", "rule: keep      "))
        r3 = diff(V1, new3, tmp_path)
        assert "state_removed" not in kinds(r3)
        assert r3["contracts"][0]["transitional"]["rule"] == "keep"

    def test_parameter_dropped_from_the_declaration(self, tmp_path):
        new = edit(V2, ('''      - id: sanction_lambda
        layer: rule
        value: 0.5
        min: 0.0
        max: 2.0
        step: 0.5
        at: [contracts.ROAD_USE.incentives.lambda]
''', ""))
        assert "parameter_not_declared" in kinds(diff(V1, new, tmp_path))

    def test_old_version_without_the_extension(self, tmp_path):
        old = edit(V1, ("    - amendment: 0.1\n", ""))
        r = diff(old, V2, tmp_path)
        assert "no_declaration" in kinds(r, "warning")
        assert not r["ok"]

    def test_cli_exit_codes(self, tmp_path, capsys):
        bad = tmp_path / "bad.cadl"
        bad.write_text(edit(V2, ("value: 60s", "value: 61s")), encoding="utf-8")
        assert main(["amend-diff", str(EX / "amendment_robot_town_v1.cadl"), str(bad)]) == 1
        assert main(["amend-diff", str(bad), str(EX / "amendment_robot_town_v2.cadl")]) == 2
        assert main(["amend-diff", str(tmp_path / "none.cadl"), str(bad)]) == 2
        capsys.readouterr()

    def test_text_format(self, tmp_path):
        new = edit(V2, ("severity: Minor", "severity: Major"))
        r = diff(V1, new, tmp_path)
        assert "do not respect" in format_diff_text(r)


# --- Relation to the norms extension (K.4, Appendix F, F.6) ---------------------------

NORMS_V1 = '''
sos:
  name: "Desk"
  version: "1"
  extensions:
    - norms: 0.1
    - amendment: 0.1
  actors:
    - { id: D, role: dispatcher }
    - { id: R, role: robot }
    - { id: H, role: operator }
  contracts:
    - id: JOB
      parties: [D, R]
      norms:
        - id: report
          kind: obligation
          bearer: R
          counterparty: D
          content: "R -> D : report"
          deadline: 30s
          on_amendment: carry_over
  amendment:
    transitional:
      contracts:
        - { contract: JOB, rule: keep }
    layers:
      contract: { decided_by: H }
    parameters:
      - id: report_deadline
        layer: contract
        value: 30s
        min: 10s
        max: 60s
        step: 10s
        at: [contracts.JOB.norms.report.deadline]
'''


class TestNorms:
    def test_keep_is_reported_against_on_amendment(self):
        r = result(NORMS_V1)
        assert r.ok, r.errors
        assert has(r.infos, "'JOB'", "on_amendment:", "F.6")

    def test_switch_includes_the_norms_diff(self, tmp_path):
        old = edit(NORMS_V1, ("rule: keep", "rule: switch"))
        new = edit(old, ('version: "1"', 'version: "2"'), ("deadline: 30s", "deadline: 20s"),
                   ("value: 30s", "value: 20s"))
        r = diff(old, new, tmp_path)
        assert r["ok"], r["errors"]
        texts = [f["text"] for f in r["contracts"][0]["findings"]]
        assert has(texts, "norms (F.6)", "report")

    def test_keep_skips_the_norms_diff(self, tmp_path):
        new = edit(NORMS_V1, ('version: "1"', 'version: "2"'), ("deadline: 30s", "deadline: 20s"),
                   ("value: 30s", "value: 20s"))
        r = diff(NORMS_V1, new, tmp_path)
        texts = [f["text"] for f in r["contracts"][0]["findings"]]
        assert not has(texts, "norms (F.6)") and has(texts, "on_amendment")
