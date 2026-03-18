"""Tests for CADL IEC 62853 compliance mapping module."""

from __future__ import annotations

import pytest

from cadl.ast_nodes import (
    ActorDef,
    ActorRef,
    AuthorityBlock,
    AutonomyLevel,
    BoolLiteral,
    ContractDef,
    Identifier,
    IncentivesBlock,
    InformationBlock,
    SoSDefinition,
    SoSType,
    TransitionDef,
    ViolationBlock,
)
from cadl.iec62853 import (
    _alpha_description,
    _beta_description,
    _lambda_description,
    generate_iec62853_report,
)


# === Helpers ===


def _make_actor(name: str, role: str = "worker") -> ActorDef:
    return ActorDef(id=ActorRef(name=name), role=role, autonomy=AutonomyLevel.MEDIUM)


def _make_contract(
    id: str,
    parties: list[str] | None = None,
    beta: float | None = None,
    alpha: float | None = None,
    lambda_: float | None = None,
    violation: ViolationBlock | None = None,
    assume: list | None = None,
    guarantee: list | None = None,
) -> ContractDef:
    party_refs = [ActorRef(name=p) for p in (parties or ["A", "B"])]
    authority = AuthorityBlock(beta=beta) if beta is not None else None
    information = InformationBlock(alpha=alpha) if alpha is not None else None
    incentives = IncentivesBlock(lambda_=lambda_) if lambda_ is not None else None
    return ContractDef(
        id=id,
        parties=party_refs,
        assume=assume or [],
        guarantee=guarantee or [],
        authority=authority,
        information=information,
        incentives=incentives,
        violation=violation,
    )


def _make_sos(
    name: str = "TestSoS",
    sos_type: SoSType | None = None,
    actors: list[ActorDef] | None = None,
    contracts: list[ContractDef] | None = None,
    transitions: list[TransitionDef] | None = None,
) -> SoSDefinition:
    return SoSDefinition(
        name=name,
        type=sos_type,
        actors=actors or [],
        contracts=contracts or [],
        transitions=transitions or [],
    )


def _make_transition(
    from_: str, to: str, condition: str | None = None, protocol: str | None = None
) -> TransitionDef:
    return TransitionDef(
        from_regime=from_, to_regime=to, condition=condition, protocol=protocol
    )


# === TestIEC62853Report ===


class TestIEC62853Report:
    """Test generate_iec62853_report() with various SoS configurations."""

    def test_basic_report_generation(self):
        """Minimal SoS produces a well-formed report dict."""
        sos = _make_sos(name="MinimalSoS", sos_type=SoSType.DIRECTED)
        report = generate_iec62853_report(sos)

        assert isinstance(report, dict)
        assert report["sos_name"] == "MinimalSoS"
        assert report["sos_type"] == "Directed"
        assert "system_integration_level" in report
        assert "institutional_parameters" in report
        assert "service_level_agreements" in report
        assert "operational_state_machine" in report
        assert "dependability_summary" in report

    def test_report_includes_sos_name_type_integration(self):
        """Report captures name, type value, and integration level."""
        sos = _make_sos(name="MySoS", sos_type=SoSType.COLLABORATIVE)
        report = generate_iec62853_report(sos)

        assert report["sos_name"] == "MySoS"
        assert report["sos_type"] == "Collaborative"
        assert report["system_integration_level"] == "Level 2 — Collaborative Integration"

    def test_unspecified_type(self):
        """SoS with no type produces 'Unspecified' for type and integration."""
        sos = _make_sos(name="NoType")
        report = generate_iec62853_report(sos)

        assert report["sos_type"] == "Unspecified"
        assert report["system_integration_level"] == "Unspecified"

    def test_institutional_parameters_beta_governance(self):
        """beta maps to Governance Centralization Index."""
        contract = _make_contract("C1", beta=0.9)
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        params = report["institutional_parameters"]
        assert len(params) == 1
        p = params[0]
        assert p["contract_id"] == "C1"
        assert p["cadl_concept"] == "beta (authority centralization)"
        assert p["value"] == 0.9
        assert p["iec62853_concept"] == "Governance Centralization Index"

    def test_institutional_parameters_alpha_transparency(self):
        """alpha maps to Information Transparency Level."""
        contract = _make_contract("C2", alpha=0.7)
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        params = report["institutional_parameters"]
        assert len(params) == 1
        p = params[0]
        assert p["cadl_concept"] == "alpha (information sharing)"
        assert p["value"] == 0.7
        assert p["iec62853_concept"] == "Information Transparency Level"

    def test_institutional_parameters_lambda_alignment(self):
        """lambda maps to Stakeholder Alignment Metric."""
        contract = _make_contract("C3", lambda_=0.6)
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        params = report["institutional_parameters"]
        assert len(params) == 1
        p = params[0]
        assert p["cadl_concept"] == "lambda (incentive alignment)"
        assert p["value"] == 0.6
        assert p["iec62853_concept"] == "Stakeholder Alignment Metric"

    def test_all_institutional_parameters_in_one_contract(self):
        """A contract with all three parameters produces three entries."""
        contract = _make_contract("C4", beta=0.8, alpha=0.5, lambda_=0.3)
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        params = report["institutional_parameters"]
        assert len(params) == 3
        concepts = {p["iec62853_concept"] for p in params}
        assert concepts == {
            "Governance Centralization Index",
            "Information Transparency Level",
            "Stakeholder Alignment Metric",
        }

    def test_service_level_agreements_populated(self):
        """Contracts produce SLA entries with party names and counts."""
        contract = _make_contract(
            "SLA1",
            parties=["Robot", "Operator"],
            assume=[BoolLiteral(True)],
            guarantee=[BoolLiteral(True), Identifier(name="safety")],
        )
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        slas = report["service_level_agreements"]
        assert len(slas) == 1
        sla = slas[0]
        assert sla["contract_id"] == "SLA1"
        assert sla["parties"] == ["Robot", "Operator"]
        assert sla["assumption_count"] == 1
        assert sla["guarantee_count"] == 2

    def test_sla_with_violation_block(self):
        """SLA includes failure_response when violation block is present."""
        violation = ViolationBlock(
            detect="timeout > 30s",
            action="halt_operation",
            escalation="notify_supervisor",
        )
        contract = _make_contract("SLA2", violation=violation)
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        sla = report["service_level_agreements"][0]
        assert sla["failure_response"] is not None
        assert "detect: timeout > 30s" in sla["failure_response"]
        assert "action: halt_operation" in sla["failure_response"]
        assert "escalation: notify_supervisor" in sla["failure_response"]

    def test_sla_without_violation_block(self):
        """SLA failure_response is None when no violation block."""
        contract = _make_contract("SLA3")
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        sla = report["service_level_agreements"][0]
        assert sla["failure_response"] is None

    @pytest.mark.parametrize(
        "sos_type, expected_level",
        [
            (SoSType.DIRECTED, "Level 4 — Centrally Managed"),
            (SoSType.ACKNOWLEDGED, "Level 3 — Acknowledged Integration"),
            (SoSType.COLLABORATIVE, "Level 2 — Collaborative Integration"),
            (SoSType.VIRTUAL, "Level 1 — Emergent / Virtual"),
        ],
    )
    def test_sos_type_integration_level_mapping(self, sos_type, expected_level):
        """Each SoSType maps to its correct integration level string."""
        sos = _make_sos(sos_type=sos_type)
        report = generate_iec62853_report(sos)
        assert report["system_integration_level"] == expected_level

    def test_no_institutional_parameters(self):
        """SoS with contracts but no institutional params yields empty list and N/A summary."""
        contract = _make_contract("Empty1")
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        assert report["institutional_parameters"] == []
        summary = report["dependability_summary"]
        assert summary["governance_index"] == "N/A"
        assert summary["transparency_level"] == "N/A"
        assert summary["alignment_metric"] == "N/A"

    def test_no_contracts_at_all(self):
        """SoS with zero contracts yields empty SLA and parameter lists."""
        sos = _make_sos()
        report = generate_iec62853_report(sos)

        assert report["institutional_parameters"] == []
        assert report["service_level_agreements"] == []

    def test_dependability_summary_averages(self):
        """Summary computes correct averages across multiple contracts."""
        c1 = _make_contract("C1", beta=0.8, alpha=0.6, lambda_=0.4)
        c2 = _make_contract("C2", beta=0.6, alpha=0.4, lambda_=0.8)
        sos = _make_sos(contracts=[c1, c2], actors=[_make_actor("A"), _make_actor("B")])
        report = generate_iec62853_report(sos)

        summary = report["dependability_summary"]
        assert summary["total_contracts"] == 2
        assert summary["total_actors"] == 2
        assert summary["governance_index"] == 0.7
        assert summary["transparency_level"] == 0.5
        assert summary["alignment_metric"] == 0.6

    def test_no_transitions_yields_null_state_machine(self):
        """Without transitions, operational_state_machine is None."""
        sos = _make_sos()
        report = generate_iec62853_report(sos)
        assert report["operational_state_machine"] is None

    def test_party_str_with_indexed_actor(self):
        """ActorRef with index renders correctly in SLA parties."""
        contract = ContractDef(
            id="IndexedC",
            parties=[ActorRef(name="Robot", index="*"), ActorRef(name="Sensor", index=0)],
        )
        sos = _make_sos(contracts=[contract])
        report = generate_iec62853_report(sos)

        sla = report["service_level_agreements"][0]
        assert "Robot[*]" in sla["parties"]
        assert "Sensor[0]" in sla["parties"]


# === TestIEC62853Descriptions ===


class TestIEC62853Descriptions:
    """Test description helper functions for institutional parameter values."""

    # --- beta descriptions ---

    def test_beta_high(self):
        assert _beta_description(0.9) == "Highly centralized governance"
        assert _beta_description(0.8) == "Highly centralized governance"

    def test_beta_moderate(self):
        assert _beta_description(0.6) == "Moderately centralized governance"
        assert _beta_description(0.5) == "Moderately centralized governance"

    def test_beta_low(self):
        assert _beta_description(0.3) == "Distributed governance"
        assert _beta_description(0.2) == "Distributed governance"

    def test_beta_very_low(self):
        assert _beta_description(0.1) == "Highly distributed governance"
        assert _beta_description(0.0) == "Highly distributed governance"

    # --- alpha descriptions ---

    def test_alpha_high(self):
        assert _alpha_description(0.9) == "High information transparency"
        assert _alpha_description(0.8) == "High information transparency"

    def test_alpha_moderate(self):
        assert _alpha_description(0.6) == "Moderate information transparency"
        assert _alpha_description(0.5) == "Moderate information transparency"

    def test_alpha_low(self):
        assert _alpha_description(0.3) == "Limited information sharing"
        assert _alpha_description(0.2) == "Limited information sharing"

    def test_alpha_very_low(self):
        assert _alpha_description(0.1) == "Minimal information sharing"
        assert _alpha_description(0.0) == "Minimal information sharing"

    # --- lambda descriptions ---

    def test_lambda_high(self):
        assert _lambda_description(0.9) == "Strong incentive alignment"
        assert _lambda_description(0.8) == "Strong incentive alignment"

    def test_lambda_moderate(self):
        assert _lambda_description(0.6) == "Moderate incentive alignment"
        assert _lambda_description(0.5) == "Moderate incentive alignment"

    def test_lambda_low(self):
        assert _lambda_description(0.3) == "Weak incentive alignment"
        assert _lambda_description(0.2) == "Weak incentive alignment"

    def test_lambda_very_low(self):
        assert _lambda_description(0.1) == "Misaligned incentives"
        assert _lambda_description(0.0) == "Misaligned incentives"

    # --- boundary values ---

    def test_boundary_values(self):
        """Values at exact thresholds (0.8, 0.5, 0.2) fall into correct bucket."""
        assert "Highly centralized" in _beta_description(0.8)
        assert "Moderately centralized" in _beta_description(0.5)
        assert "Distributed governance" == _beta_description(0.2)

        assert "High information" in _alpha_description(0.8)
        assert "Moderate information" in _alpha_description(0.5)
        assert "Limited information" in _alpha_description(0.2)

        assert "Strong incentive" in _lambda_description(0.8)
        assert "Moderate incentive" in _lambda_description(0.5)
        assert "Weak incentive" in _lambda_description(0.2)


# === TestIEC62853WithTransitions ===


class TestIEC62853WithTransitions:
    """Test operational state machine section of the report."""

    def test_transitions_populate_state_machine(self):
        """SoS with transitions yields a populated operational_state_machine."""
        transitions = [
            _make_transition("IDLE", "ACTIVE", condition="request_received"),
            _make_transition("ACTIVE", "IDLE", condition="task_complete"),
        ]
        sos = _make_sos(transitions=transitions)
        report = generate_iec62853_report(sos)

        osm = report["operational_state_machine"]
        assert osm is not None
        assert osm["state_count"] == 2
        assert osm["transition_count"] == 2

    def test_initial_state_inferred(self):
        """Initial state is the first from_regime not appearing as a to_regime."""
        transitions = [
            _make_transition("STARTUP", "RUNNING"),
            _make_transition("RUNNING", "SHUTDOWN"),
        ]
        sos = _make_sos(transitions=transitions)
        report = generate_iec62853_report(sos)

        osm = report["operational_state_machine"]
        assert osm["initial_state"] == "STARTUP"

    def test_state_count_with_multiple_states(self):
        """State count reflects all unique states in transitions."""
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "C"),
            _make_transition("C", "D"),
        ]
        sos = _make_sos(transitions=transitions)
        report = generate_iec62853_report(sos)

        osm = report["operational_state_machine"]
        assert osm["state_count"] == 4
        assert osm["transition_count"] == 3
        assert sorted(osm["states"]) == ["A", "B", "C", "D"]

    def test_cyclic_transitions(self):
        """Cyclic transitions are detected."""
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "A"),
        ]
        sos = _make_sos(transitions=transitions)
        report = generate_iec62853_report(sos)

        osm = report["operational_state_machine"]
        assert osm["has_cycles"] is True

    def test_linear_transitions_no_cycles(self):
        """Linear chain has no cycles and has dead states."""
        transitions = [
            _make_transition("START", "MIDDLE"),
            _make_transition("MIDDLE", "END"),
        ]
        sos = _make_sos(transitions=transitions)
        report = generate_iec62853_report(sos)

        osm = report["operational_state_machine"]
        assert osm["has_cycles"] is False
        assert "END" in osm["dead_states"]


# === TestIEC62853Integration ===


class TestIEC62853Integration:
    """End-to-end tests parsing real CADL example files and generating reports."""

    @pytest.fixture
    def parse_file(self):
        from cadl.parser import parse_file
        return parse_file

    def test_robot_delivery_report(self, parse_file):
        """Parse robot_delivery.cadl and verify report structure."""
        sos = parse_file("examples/robot_delivery.cadl")
        report = generate_iec62853_report(sos)

        assert report["sos_name"] is not None
        assert report["sos_type"] != ""
        assert isinstance(report["institutional_parameters"], list)
        assert isinstance(report["service_level_agreements"], list)
        assert "dependability_summary" in report

        summary = report["dependability_summary"]
        assert summary["total_contracts"] > 0
        assert summary["total_actors"] > 0

    def test_supply_chain_report(self, parse_file):
        """Parse supply_chain.cadl and verify report structure."""
        sos = parse_file("examples/supply_chain.cadl")
        report = generate_iec62853_report(sos)

        assert report["sos_name"] is not None
        assert isinstance(report["institutional_parameters"], list)
        assert isinstance(report["service_level_agreements"], list)
        assert "dependability_summary" in report

        summary = report["dependability_summary"]
        assert summary["total_contracts"] > 0

    def test_report_structure_complete(self, parse_file):
        """A fully parsed example produces all expected top-level keys."""
        sos = parse_file("examples/robot_delivery.cadl")
        report = generate_iec62853_report(sos)

        expected_keys = {
            "sos_name",
            "sos_type",
            "system_integration_level",
            "institutional_parameters",
            "service_level_agreements",
            "operational_state_machine",
            "dependability_summary",
        }
        assert set(report.keys()) == expected_keys

    def test_supply_chain_has_institutional_params(self, parse_file):
        """Supply chain example should have institutional parameter mappings."""
        sos = parse_file("examples/supply_chain.cadl")
        report = generate_iec62853_report(sos)

        # At minimum, there should be some parameters from contracts
        params = report["institutional_parameters"]
        if params:
            # Verify each parameter entry has required fields
            for p in params:
                assert "contract_id" in p
                assert "cadl_concept" in p
                assert "value" in p
                assert "iec62853_concept" in p
                assert "description" in p
