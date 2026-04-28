"""Tests for the SoS-DSL extension parser additions (Appendix E)."""

from pathlib import Path

import pytest

from cadl.parser import parse
from cadl.ast_nodes import (
    ContractDef,
    LifecycleSpec,
    LifecycleTransition,
    MonitorDef,
    OnMatchSpec,
    OnViolationSpec,
    SamplingSpec,
)


EXAMPLE_PATH = (
    Path(__file__).parent.parent
    / "examples"
    / "sos_dsl_robot_delivery.cadl"
)


def _example_contract() -> ContractDef:
    src = EXAMPLE_PATH.read_text(encoding="utf-8")
    sos = parse(src)
    assert sos.contracts, "expected at least one contract"
    return sos.contracts[0]


class TestLifecycleParse:
    def test_lifecycle_present(self):
        c = _example_contract()
        assert c.lifecycle is not None
        assert isinstance(c.lifecycle, LifecycleSpec)

    def test_states_and_initial(self):
        c = _example_contract()
        lc = c.lifecycle
        assert "Proposed" in lc.states
        assert "Completed" in lc.states
        assert "Violated" in lc.states
        assert lc.initial == "Proposed"
        assert "Completed" in lc.terminal
        assert "Violated" in lc.terminal
        assert "Terminated" in lc.terminal

    def test_simple_transition(self):
        c = _example_contract()
        lc = c.lifecycle
        assign = next(t for t in lc.transitions if t.id == "assign")
        assert isinstance(assign, LifecycleTransition)
        # from: scalar -> normalized to list
        assert assign.from_states == ["Proposed"]
        assert assign.to_state == "Assigned"
        assert "route_assignment" in assign.on
        assert assign.deadline_ms is None
        assert assign.on_violation is None

    def test_transition_with_deadline_and_on_violation(self):
        c = _example_contract()
        lc = c.lifecycle
        accept = next(t for t in lc.transitions if t.id == "accept")
        # 5s -> 5000 ms
        assert accept.deadline_ms == 5000
        assert isinstance(accept.on_violation, OnViolationSpec)
        assert accept.on_violation.transition == "Violated"
        assert accept.on_violation.severity == "Major"

    def test_transition_with_state_set_from(self):
        c = _example_contract()
        lc = c.lifecycle
        late = next(t for t in lc.transitions if t.id == "late_failure")
        # from: [Assigned, Accepted, Delivering] -> list preserved
        assert late.from_states == ["Assigned", "Accepted", "Delivering"]
        assert late.to_state == "Violated"

    def test_lifecycle_invariants_L1_L2(self):
        """Static-semantic checks L-1 / L-2 (Appendix E.4)."""
        c = _example_contract()
        lc = c.lifecycle
        # L-1: initial and terminal states are declared
        assert lc.initial in lc.states
        for term in lc.terminal:
            assert term in lc.states
        # L-2: every transition references declared states
        for t in lc.transitions:
            for f in t.from_states:
                assert f in lc.states, f"undeclared from-state: {f}"
            assert t.to_state in lc.states, (
                f"undeclared to-state: {t.to_state}"
            )


class TestMonitorParse:
    def test_monitors_present(self):
        c = _example_contract()
        assert len(c.monitors) >= 3

    def test_periodic_sampling_normalized(self):
        c = _example_contract()
        bg = next(m for m in c.monitors if m.id == "battery_guard")
        assert isinstance(bg, MonitorDef)
        assert isinstance(bg.sampling, SamplingSpec)
        assert bg.sampling.kind == "periodic"
        # 500ms -> 500
        assert bg.sampling.period_ms == 500
        assert "battery" in bg.rule.lower()
        assert isinstance(bg.on_match, OnMatchSpec)
        assert bg.on_match.transition == "Violated"
        assert bg.on_match.severity == "Major"

    def test_observe_list(self):
        c = _example_contract()
        cw = next(m for m in c.monitors if m.id == "collision_watch")
        # observe is normalized to a list of strings
        assert len(cw.observe) >= 1
        # critical severity
        assert cw.on_match is not None
        assert cw.on_match.severity == "Critical"


class TestBackwardCompatibility:
    def test_contract_without_extension_still_parses(self):
        """Contracts without lifecycle/monitors should remain valid."""
        src = '''\
sos:
  name: "PlainSoS"
  contracts:
    - id: PLAIN
      parties: [A, B]
      assume: ["A.alive == true"]
      guarantee: ["service_time <= 100ms"]
'''
        sos = parse(src)
        c = sos.contracts[0]
        assert c.id == "PLAIN"
        assert c.lifecycle is None
        assert c.monitors == []
