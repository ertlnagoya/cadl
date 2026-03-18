"""Tests for CADL regime map construction and graph analysis."""

from __future__ import annotations

import json

import pytest

from cadl.ast_nodes import SoSDefinition, TransitionDef
from cadl.regime_map import RegimeMap, RegimeState


# === Helpers ===

def _make_transition(from_: str, to: str, condition: str | None = None,
                     protocol: str | None = None,
                     safety_invariant: str | None = None) -> TransitionDef:
    return TransitionDef(
        from_regime=from_,
        to_regime=to,
        condition=condition,
        protocol=protocol,
        safety_invariant=safety_invariant,
    )


def _make_sos(transitions: list[TransitionDef] | None = None) -> SoSDefinition:
    return SoSDefinition(name="TestSoS", transitions=transitions or [])


# === Construction tests ===

class TestRegimeMapConstruction:
    def test_from_transitions_basic(self):
        transitions = [
            _make_transition("IDLE", "ACTIVE", "request_pending"),
            _make_transition("ACTIVE", "IDLE", "work_done"),
        ]
        rm = RegimeMap.from_transitions(transitions)

        assert len(rm.states) == 2
        assert "IDLE" in rm.states
        assert "ACTIVE" in rm.states
        assert rm.initial_state == "IDLE"
        assert len(rm.transitions) == 2

    def test_from_sos(self):
        sos = _make_sos([
            _make_transition("A", "B"),
            _make_transition("B", "C"),
        ])
        rm = RegimeMap.from_sos(sos)

        assert len(rm.states) == 3
        assert rm.initial_state == "A"

    def test_empty_transitions(self):
        rm = RegimeMap.from_transitions([])
        assert len(rm.states) == 0
        assert rm.initial_state is None

    def test_initial_state_inference(self):
        """Initial state is the first from_regime that never appears as to_regime."""
        transitions = [
            _make_transition("B", "C"),
            _make_transition("A", "B"),
            _make_transition("C", "A"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        # B appears first as from_regime but B is also a to_regime (A->B)
        # A appears as to_regime (C->A), so it's not initial
        # Only need first from_regime not in to_regimes: none qualify, so fallback to "B"
        # Actually: to_regimes = {C, B, A}, from_regimes = {B, A, C}
        # All from_regimes appear in to_regimes → fallback to first from_regime = "B"
        assert rm.initial_state == "B"

    def test_initial_state_fallback(self):
        """When all states are targets, use first from_regime as fallback."""
        transitions = [
            _make_transition("X", "Y"),
            _make_transition("Y", "X"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        # Both appear as to_regime → fallback to first from_regime
        assert rm.initial_state == "X"

    def test_outgoing_incoming_indices(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("A", "C"),
            _make_transition("B", "C"),
        ]
        rm = RegimeMap.from_transitions(transitions)

        assert rm.states["A"].outgoing == [0, 1]
        assert rm.states["A"].incoming == []
        assert rm.states["B"].outgoing == [2]
        assert rm.states["B"].incoming == [0]
        assert rm.states["C"].outgoing == []
        assert rm.states["C"].incoming == [1, 2]


# === Reachability tests ===

class TestReachability:
    def test_all_reachable(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "C"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        reachable = rm.reachable_states()
        assert reachable == {"A", "B", "C"}

    def test_unreachable_state(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("C", "D"),  # C is disconnected from A
        ]
        rm = RegimeMap.from_transitions(transitions)
        assert rm.initial_state == "A"
        unreachable = rm.find_unreachable_states()
        assert "C" in unreachable
        assert "D" in unreachable

    def test_reachable_from_custom_start(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "C"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        reachable = rm.reachable_states("B")
        assert reachable == {"B", "C"}
        assert "A" not in reachable

    def test_empty_map_reachability(self):
        rm = RegimeMap.from_transitions([])
        assert rm.reachable_states() == set()
        assert rm.find_unreachable_states() == set()


# === Dead state tests ===

class TestDeadStates:
    def test_dead_end_detected(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("A", "C"),
            # B and C are dead ends
        ]
        rm = RegimeMap.from_transitions(transitions)
        dead = rm.find_dead_states()
        assert dead == {"B", "C"}

    def test_no_dead_ends_in_cycle(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "A"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        dead = rm.find_dead_states()
        assert dead == set()

    def test_single_state_not_dead(self):
        """A single state with no outgoing is NOT considered dead."""
        transitions = [
            _make_transition("A", "A"),  # self-loop to create single-state
        ]
        rm = RegimeMap.from_transitions(transitions)
        assert len(rm.states) == 1
        dead = rm.find_dead_states()
        assert dead == set()


# === Cycle detection tests ===

class TestCycleDetection:
    def test_simple_cycle(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "C"),
            _make_transition("C", "A"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        cycles = rm.find_cycles()
        assert len(cycles) == 1
        assert set(cycles[0]) == {"A", "B", "C"}

    def test_no_cycle(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "C"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        cycles = rm.find_cycles()
        assert cycles == []

    def test_self_loop(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "B"),  # self-loop
        ]
        rm = RegimeMap.from_transitions(transitions)
        cycles = rm.find_cycles()
        assert len(cycles) == 1
        assert cycles[0] == ["B"]

    def test_multiple_cycles(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "A"),
            _make_transition("C", "D"),
            _make_transition("D", "C"),
            _make_transition("A", "C"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        cycles = rm.find_cycles()
        assert len(cycles) == 2


# === Shortest path tests ===

class TestShortestPath:
    def test_direct_path(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "C"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        path = rm.shortest_path("A", "C")
        assert path == ["A", "B", "C"]

    def test_same_state(self):
        transitions = [_make_transition("A", "B")]
        rm = RegimeMap.from_transitions(transitions)
        path = rm.shortest_path("A", "A")
        assert path == ["A"]

    def test_unreachable_target(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("C", "D"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        path = rm.shortest_path("A", "D")
        assert path is None

    def test_nonexistent_state(self):
        transitions = [_make_transition("A", "B")]
        rm = RegimeMap.from_transitions(transitions)
        path = rm.shortest_path("A", "Z")
        assert path is None

    def test_multi_hop_shortest(self):
        transitions = [
            _make_transition("A", "B"),
            _make_transition("B", "C"),
            _make_transition("C", "D"),
            _make_transition("A", "D"),  # shortcut
        ]
        rm = RegimeMap.from_transitions(transitions)
        path = rm.shortest_path("A", "D")
        assert path == ["A", "D"]


# === Export tests ===

class TestDotExport:
    def test_dot_format(self):
        transitions = [
            _make_transition("IDLE", "ACTIVE", "request_pending", "START_PROTO"),
            _make_transition("ACTIVE", "IDLE", "work_done"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        dot = rm.to_dot()

        assert "digraph RegimeMap" in dot
        assert '"IDLE"' in dot
        assert '"ACTIVE"' in dot
        assert '"IDLE" -> "ACTIVE"' in dot
        assert "request_pending" in dot
        assert "START_PROTO" in dot

    def test_dot_marks_initial_state(self):
        transitions = [_make_transition("START", "END")]
        rm = RegimeMap.from_transitions(transitions)
        dot = rm.to_dot()
        assert 'penwidth=2' in dot
        assert '"START"' in dot

    def test_dot_marks_dead_state(self):
        transitions = [_make_transition("A", "B")]
        rm = RegimeMap.from_transitions(transitions)
        dot = rm.to_dot()
        assert 'color=red' in dot


class TestJsonExport:
    def test_json_structure(self):
        transitions = [
            _make_transition("A", "B", "cond1", "P1", "inv1"),
            _make_transition("B", "A"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        data = rm.to_json()

        assert data["initial_state"] == "A"
        assert sorted(data["states"]) == ["A", "B"]
        assert len(data["transitions"]) == 2
        assert data["transitions"][0]["from"] == "A"
        assert data["transitions"][0]["condition"] == "cond1"
        assert data["transitions"][0]["safety_invariant"] == "inv1"
        assert "analysis" in data
        assert "reachable" in data["analysis"]

    def test_json_serializable(self):
        transitions = [_make_transition("X", "Y")]
        rm = RegimeMap.from_transitions(transitions)
        # Should not raise
        json_str = json.dumps(rm.to_json())
        assert json_str


class TestTextExport:
    def test_text_output(self):
        transitions = [
            _make_transition("NORMAL", "ALERT", "threshold_exceeded"),
            _make_transition("ALERT", "NORMAL", "resolved"),
        ]
        rm = RegimeMap.from_transitions(transitions)
        text = rm.to_text()

        assert "Regime Map:" in text
        assert "2 states" in text
        assert "2 transitions" in text
        assert "NORMAL" in text
        assert "ALERT" in text
        assert "threshold_exceeded" in text


# === Regime verification integration tests ===

class TestRegimeVerification:
    def test_reachability_check_passes(self):
        from cadl.verifier import _check_regime_reachability

        sos = _make_sos([
            _make_transition("A", "B"),
            _make_transition("B", "C"),
        ])
        results = _check_regime_reachability(sos)
        assert len(results) == 1
        assert results[0].status == "passed"

    def test_reachability_check_fails(self):
        from cadl.verifier import _check_regime_reachability

        sos = _make_sos([
            _make_transition("A", "B"),
            _make_transition("C", "D"),
        ])
        results = _check_regime_reachability(sos)
        assert len(results) == 1
        assert results[0].status == "failed"
        assert "C" in results[0].message or "D" in results[0].message

    def test_dead_state_check(self):
        from cadl.verifier import _check_regime_dead_states

        sos = _make_sos([
            _make_transition("A", "B"),
            _make_transition("A", "C"),
        ])
        results = _check_regime_dead_states(sos)
        assert len(results) == 1
        assert results[0].status == "failed"
        assert "dead-end" in results[0].message.lower() or "Dead-end" in results[0].message

    def test_safety_invariant_check(self):
        from cadl.verifier import _check_regime_safety_invariants

        sos = _make_sos([
            _make_transition("A", "B", "x > 0", safety_invariant="x > 0"),
        ])
        results = _check_regime_safety_invariants(sos)
        assert len(results) == 1
        assert results[0].status == "passed"

    def test_verify_includes_regime_checks(self):
        """Full verify() includes regime checks when transitions are present."""
        from cadl.verifier import verify

        sos = _make_sos([
            _make_transition("A", "B", "x > 0"),
            _make_transition("B", "A", "x <= 0"),
        ])
        results = verify(sos)
        check_names = [r.check_name for r in results]
        assert any("reachability" in n.lower() for n in check_names)
        assert any("dead" in n.lower() for n in check_names)


# === Integration test with smart_city_traffic example ===

class TestSmartCityTrafficIntegration:
    def test_parse_and_build_regime_map(self):
        from cadl.parser import parse_file
        from pathlib import Path

        cadl_file = Path(__file__).parent.parent / "examples" / "smart_city_traffic.cadl"
        if not cadl_file.exists():
            pytest.skip("smart_city_traffic.cadl not found")

        sos = parse_file(cadl_file)
        rm = RegimeMap.from_sos(sos)

        assert len(rm.states) == 3  # NORMAL, CONGESTED, EMERGENCY
        assert "NORMAL" in rm.states
        assert "CONGESTED" in rm.states
        assert "EMERGENCY" in rm.states
        assert rm.initial_state == "NORMAL"
        assert len(rm.transitions) == 6

        # All states should be reachable
        assert rm.find_unreachable_states() == set()
        # No dead states (all have outgoing)
        assert rm.find_dead_states() == set()
        # Should have cycles
        assert len(rm.find_cycles()) > 0

    def test_verify_smart_city(self):
        from cadl.parser import parse_file
        from cadl.verifier import verify
        from pathlib import Path

        cadl_file = Path(__file__).parent.parent / "examples" / "smart_city_traffic.cadl"
        if not cadl_file.exists():
            pytest.skip("smart_city_traffic.cadl not found")

        sos = parse_file(cadl_file)
        results = verify(sos)

        # Check that regime verification ran
        regime_checks = [r for r in results if "regime" in r.check_name.lower() or "safety" in r.check_name.lower()]
        assert len(regime_checks) > 0

        # Reachability and dead-state should pass
        for r in results:
            if "reachability" in r.check_name.lower():
                assert r.status == "passed"
            if "dead" in r.check_name.lower():
                assert r.status == "passed"
