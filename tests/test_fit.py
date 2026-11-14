"""`cadl fit`: the fit of a contract transplanted into a destination."""

import json

import pytest
import yaml

from cadl.cli import main
from cadl.fit import fit_check, parse_role_map, transplant


def _sos(actors, contracts):
    return {"sos": {"name": "T", "type": "Collaborative", "version": "1.0.0",
                    "actors": [{"id": a, "role": "r"} for a in actors],
                    "contracts": contracts}}


def _kinds(report, condition="F3"):
    return [(f.status, f.kind) for f in report.findings if f.condition == condition]


DEST = _sos(["TASK_OWNER", "ROBOT[1..N]"], [
    {"id": "ASSIGNMENT", "parties": ["TASK_OWNER", "ROBOT[*]"],
     "guarantee": ["all_claim_before_assignment()"]},
])
SOURCE = _sos(["DISPATCHER", "ROBOT[1..N]"], [
    {"id": "SLA", "parties": ["DISPATCHER", "ROBOT[*]"],
     "assume": ["DISPATCHER.is_operational == true",
                "NOT all_claim_before_assignment()"],
     "guarantee": ["delivery_time <= 300s"],
     "authority": {"decision_scope": "assignment", "decision_holder": "DISPATCHER"},
     "violation": {"detect": "delivery_time > 300s", "action": "notify(DISPATCHER)"}},
])


class TestTransplant:

    def test_naive_transplant_reports_missing_roles(self):
        r = fit_check(transplant(SOURCE, DEST, "SLA", {}), "SLA")
        f1 = [f for f in r.findings if f.condition == "F1" and f.status == "failed"]
        assert f1 and all("DISPATCHER" in f.message for f in f1)
        assert r.verdict == "misfit"

    def test_mapped_transplant_is_vacuous(self):
        data = transplant(SOURCE, DEST, "SLA", {"DISPATCHER": "TASK_OWNER"})
        r = fit_check(data, "SLA")
        assert not [f for f in r.findings if f.condition == "F1" and f.status == "failed"]
        assert _kinds(r) == [("failed", "vacuous")]
        assert r.neighbourhood == ["ASSIGNMENT"]

    def test_mapping_renames_unresolved_positions_too(self):
        data = transplant(SOURCE, DEST, "SLA", {"DISPATCHER": "TASK_OWNER"})
        sla = data["sos"]["contracts"][-1]
        assert sla["violation"]["action"] == "notify(TASK_OWNER)"

    def test_unmapped_name_in_violation_is_reviewed(self):
        src = yaml.safe_load(yaml.safe_dump(SOURCE))
        src["sos"]["contracts"][0]["assume"] = ["ROBOT[i].battery > 20"]
        src["sos"]["contracts"][0]["parties"] = ["ROBOT[*]"]
        src["sos"]["contracts"][0].pop("authority")
        r = fit_check(transplant(src, DEST, "SLA", {}), "SLA")
        review = [f for f in r.findings if f.condition == "review"]
        assert review and "DISPATCHER" in review[0].message

    def test_existing_id_is_refused(self):
        with pytest.raises(ValueError):
            transplant(SOURCE, _sos(["X"], [{"id": "SLA", "parties": ["X"]}]), "SLA", {})

    def test_role_map_syntax(self):
        assert parse_role_map(["A=B", " C = D "]) == {"A": "B", "C": "D"}
        with pytest.raises(ValueError):
            parse_role_map(["A"])


class TestNeighbourhood:

    def test_three_way_conflict_is_found_jointly(self):
        data = _sos(["CENTER", "DEPOT", "TAXI[1..N]"], [
            {"id": "RULE", "parties": ["CENTER", "TAXI[*]", "DEPOT"],
             "assume": ["shift_hours + commute_hours <= 10"], "guarantee": ["rest_hours >= 8"]},
            {"id": "D1", "parties": ["CENTER", "TAXI[*]"], "guarantee": ["shift_hours >= 9"]},
            {"id": "D2", "parties": ["DEPOT", "TAXI[*]"], "guarantee": ["commute_hours >= 2"]},
        ])
        r = fit_check(data, "RULE")
        assert _kinds(r) == [("failed", "vacuous")]
        f3 = [f for f in r.findings if f.condition == "F3"][0]
        assert f3.contracts == ["D1", "D2"]

    def test_shared_variable_without_shared_party(self):
        data = _sos(["CENTER", "DEPOT"], [
            {"id": "RULE", "parties": ["CENTER"], "assume": ["network_latency <= 200ms"]},
            {"id": "D", "parties": ["DEPOT"], "guarantee": ["network_latency >= 1s"]},
        ])
        assert _kinds(fit_check(data, "RULE")) == [("failed", "vacuous")]
        assert [f.status for f in fit_check(data, "RULE", scope="party").findings
                if f.condition == "F3"] == ["passed"]

    def test_guarantee_conflict(self):
        data = _sos(["A", "B"], [
            {"id": "RULE", "parties": ["A", "B"], "guarantee": ["speed <= 30"]},
            {"id": "D", "parties": ["A"], "guarantee": ["speed >= 50"]},
        ])
        assert _kinds(fit_check(data, "RULE")) == [("failed", "guarantee")]

    def test_class_does_not_depend_on_the_core(self):
        # The assumptions of RULE and D2 exclude each other, and the guarantees
        # of RULE and D1 conflict: a guarantee conflict, whichever core the
        # solver finds.
        for order in ((1, 2), (2, 1)):
            ds = {1: {"id": "D1", "parties": ["A"], "guarantee": ["y <= 0"]},
                  2: {"id": "D2", "parties": ["A"], "assume": ["x <= 0"]}}
            data = _sos(["A"], [
                {"id": "RULE", "parties": ["A"], "assume": ["x >= 5"], "guarantee": ["y >= 5"]},
                ds[order[0]], ds[order[1]]])
            r = fit_check(data, "RULE")
            assert _kinds(r) == [("failed", "guarantee")]
            assert r.verdict == "misfit"
            assert any(f.condition == "info" and "D2" in f.message for f in r.findings)

    def test_modes_with_conflicting_guarantees_are_an_exclusion(self):
        data = _sos(["A"], [
            {"id": "RULE", "parties": ["A"], "assume": ["mode == 1"], "guarantee": ["speed <= 10"]},
            {"id": "D", "parties": ["A"], "assume": ["mode == 2"], "guarantee": ["speed >= 30"]},
        ])
        r = fit_check(data, "RULE")
        assert _kinds(r) == [("warning", "exclusion")]
        assert r.verdict == "fits"

    def test_destination_modes_are_checked_separately(self):
        modes = [{"id": "D1", "parties": ["A"], "assume": ["mode == 1"], "guarantee": ["s <= 10"]},
                 {"id": "D2", "parties": ["A"], "assume": ["mode == 2"], "guarantee": ["s >= 30"]}]
        ok = fit_check(_sos(["A"], [{"id": "RULE", "parties": ["A"], "assume": ["x <= 5"],
                                     "guarantee": ["y >= 0"]}] + modes), "RULE")
        assert ok.verdict == "fits" and _kinds(ok) == [("warning", "exclusion")]
        bad = fit_check(_sos(["A"], [{"id": "RULE", "parties": ["A"], "guarantee": ["s <= 20"]}] + modes), "RULE")
        assert _kinds(bad) == [("failed", "guarantee")]
        assert [f.contracts for f in bad.findings if f.condition == "F3"] == [["D2"]]
        assert any(f.condition == "info" and "D1" in f.message for f in bad.findings)

    def test_exclusion_is_not_a_misfit(self):
        data = _sos(["A", "B"], [
            {"id": "RULE", "parties": ["A", "B"], "assume": ["mode == 1"], "guarantee": ["x <= 3"]},
            {"id": "D", "parties": ["A"], "assume": ["mode == 2"], "guarantee": ["y <= 3"]},
        ])
        r = fit_check(data, "RULE")
        assert _kinds(r) == [("warning", "exclusion")]
        assert r.verdict == "fits"

    def test_destination_already_inconsistent(self):
        data = _sos(["A"], [
            {"id": "RULE", "parties": ["A"], "guarantee": ["z >= 0"]},
            {"id": "D1", "parties": ["A"], "guarantee": ["x <= 1"]},
            {"id": "D2", "parties": ["A"], "guarantee": ["x >= 2"]},
        ])
        assert [s for s, _ in _kinds(fit_check(data, "RULE"))] == ["unknown"]

    def test_pass_over_an_abstraction_is_unknown(self):
        data = _sos(["A", "TAXI[1..N]"], [
            {"id": "RULE", "parties": ["A", "TAXI[*]"],
             "assume": ["for all t in TAXI[*]: TAXI[t].fuel >= 10"]},
            {"id": "D", "parties": ["A"], "guarantee": ["TAXI[1].fuel < 5"]},
        ])
        assert fit_check(data, "RULE").verdict == "unknown"

    def test_map_renames_attributes(self):
        src = _sos(["A"], [{"id": "RULE", "parties": ["A"], "assume": ["delivery_duration <= 300"]}])
        dst = _sos(["A"], [{"id": "D", "parties": ["A"], "guarantee": ["delivery_time >= 600"]}])
        assert fit_check(transplant(src, dst, "RULE", {}), "RULE").verdict == "fits"
        mapped = transplant(src, dst, "RULE", {"delivery_duration": "delivery_time"})
        assert _kinds(fit_check(mapped, "RULE")) == [("failed", "vacuous")]

    def test_quantifier_is_reported(self):
        data = _sos(["A", "TAXI[1..N]"], [
            {"id": "RULE", "parties": ["A", "TAXI[*]"],
             "assume": ["for all t in TAXI[*]: TAXI[t].fuel >= 10"]},
        ])
        enc = [f for f in fit_check(data, "RULE").findings if f.condition == "encoding"]
        assert enc and "quantifier" in enc[0].message

    def test_independent_conflicts_are_all_reported(self):
        data = _sos(["A"], [
            {"id": "RULE", "parties": ["A"], "assume": ["p == true"], "guarantee": ["q == true"]},
            {"id": "D1", "parties": ["A"], "guarantee": ["p == false"]},
            {"id": "D2", "parties": ["A"], "guarantee": ["q == false"]},
            {"id": "D3", "parties": ["A"], "guarantee": ["r == true"]},
        ])
        r = fit_check(data, "RULE")
        f3 = [(f.kind, f.contracts) for f in r.findings if f.condition == "F3"]
        assert f3 == [("vacuous", ["D1"]), ("guarantee", ["D2"])]

    def test_overlapping_conflicts_are_all_reported(self):
        # Two minimal conflicts that share D2: {D1, D2} and {D2, D3}.
        data = _sos(["A"], [
            {"id": "RULE", "parties": ["A"], "assume": ["x + y <= 10", "y + z <= 10"]},
            {"id": "D1", "parties": ["A"], "guarantee": ["x >= 6"]},
            {"id": "D2", "parties": ["A"], "guarantee": ["y >= 6"]},
            {"id": "D3", "parties": ["A"], "guarantee": ["z >= 6"]},
        ])
        r = fit_check(data, "RULE")
        f3 = sorted(f.contracts for f in r.findings if f.condition == "F3")
        assert f3 == [["D1", "D2"], ["D2", "D3"]]

    def test_exclusion_does_not_depend_on_the_order_of_sets(self):
        modes = [{"id": "D1", "parties": ["A"], "assume": ["mode == 1"], "guarantee": ["s <= 10"]},
                 {"id": "D2", "parties": ["A"], "assume": ["mode == 2"], "guarantee": ["s >= 30"]},
                 {"id": "D3", "parties": ["A"], "guarantee": ["t >= 0"]}]
        rule = {"id": "RULE", "parties": ["A"], "assume": ["x <= 5"], "guarantee": ["y >= 0"]}
        for ds in (modes, modes[::-1]):
            r = fit_check(_sos(["A"], [rule] + ds), "RULE")
            assert [f.contracts for f in r.findings if f.condition == "F3"] == [["D1", "D2"]]

    def test_assumptions_not_established_are_named(self):
        data = _sos(["A"], [
            {"id": "RULE", "parties": ["A"], "assume": ["x >= 1", "y >= 1"], "guarantee": ["z >= 0"]},
            {"id": "D", "parties": ["A"], "guarantee": ["x >= 5"]},
        ])
        r = fit_check(data, "RULE")
        assert r.verdict == "fits"
        info = [f.message for f in r.findings if f.condition == "info"]
        assert any("do not establish" in m and "y >= 1" in m and "x >= 1" not in m for m in info)

    def test_no_neighbour(self):
        data = _sos(["A", "B"], [
            {"id": "RULE", "parties": ["A"], "guarantee": ["x <= 1"]},
            {"id": "D", "parties": ["B"], "guarantee": ["y >= 2"]},
        ])
        r = fit_check(data, "RULE")
        assert r.neighbourhood == [] and r.verdict == "fits"


class TestCli:

    def test_exit_codes_and_json(self, tmp_path, capsys):
        src, dst, out = tmp_path / "s.cadl", tmp_path / "d.cadl", tmp_path / "o.cadl"
        src.write_text(yaml.safe_dump(SOURCE), encoding="utf-8")
        dst.write_text(yaml.safe_dump(DEST), encoding="utf-8")
        assert main(["fit", str(dst), "-c", "SLA", "--source", str(src),
                     "--map", "DISPATCHER=TASK_OWNER", "--format", "json",
                     "-o", str(out)]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["verdict"] == "misfit"
        assert any(f.get("kind") == "vacuous" for f in report["findings"])
        assert "SLA" in out.read_text(encoding="utf-8")

    def test_missing_contract(self, tmp_path, capsys):
        dst = tmp_path / "d.cadl"
        dst.write_text(yaml.safe_dump(DEST), encoding="utf-8")
        assert main(["fit", str(dst), "-c", "NOPE"]) == 1

    def test_map_needs_source(self, tmp_path):
        dst = tmp_path / "d.cadl"
        dst.write_text(yaml.safe_dump(DEST), encoding="utf-8")
        assert main(["fit", str(dst), "-c", "ASSIGNMENT", "--map", "A=B"]) == 1
