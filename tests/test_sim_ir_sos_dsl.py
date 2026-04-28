"""IR-level tests for the SoS-DSL extension (Appendix E).

Verifies that lower_to_ir() carries lifecycle and monitor information
through to SimIR with the documented normalizations.
"""

from pathlib import Path
from dataclasses import asdict

from cadl.parser import parse
from cadl.sim import lower_to_ir
from cadl.sim.ir import (
    LifecycleSpecIR,
    LifecycleTransitionSpec,
    MonitorSpecIR,
)


EXAMPLE = (
    Path(__file__).parent.parent
    / "examples"
    / "sos_dsl_robot_delivery.cadl"
)


def _delivery_ir():
    sos = parse(EXAMPLE.read_text(encoding="utf-8"))
    ir = lower_to_ir(sos)
    return ir.institution.contracts[0]


class TestLifecycleIR:
    def test_lifecycle_present(self):
        c = _delivery_ir()
        assert isinstance(c.lifecycle, LifecycleSpecIR)
        assert c.lifecycle.initial == "Proposed"
        assert "Violated" in c.lifecycle.terminal

    def test_transitions_normalized(self):
        c = _delivery_ir()
        assert len(c.lifecycle.transitions) == 5

        accept = next(
            t for t in c.lifecycle.transitions if t.id == "accept"
        )
        assert isinstance(accept, LifecycleTransitionSpec)
        # 5s -> 5000 ms in IR
        assert accept.deadline_ms == 5000
        assert accept.on_violation_transition == "Violated"
        assert accept.on_violation_severity == "Major"

    def test_state_set_from_normalized_to_list(self):
        c = _delivery_ir()
        late = next(
            t for t in c.lifecycle.transitions if t.id == "late_failure"
        )
        assert late.from_states == ["Assigned", "Accepted", "Delivering"]
        assert late.to_state == "Violated"


class TestMonitorIR:
    def test_three_monitors(self):
        c = _delivery_ir()
        ids = [m.id for m in c.monitors]
        assert ids == ["battery_guard", "collision_watch", "deadline_watch"]

    def test_periodic_sampling_in_ir(self):
        c = _delivery_ir()
        bg = next(m for m in c.monitors if m.id == "battery_guard")
        assert isinstance(bg, MonitorSpecIR)
        assert bg.sampling_kind == "periodic"
        assert bg.sampling_period_ms == 500
        assert bg.on_match_transition == "Violated"
        assert bg.on_match_severity == "Major"

    def test_collision_severity_critical(self):
        c = _delivery_ir()
        cw = next(m for m in c.monitors if m.id == "collision_watch")
        assert cw.on_match_severity == "Critical"
        assert cw.on_match_violation == "collision"


class TestJSONSerializability:
    def test_full_ir_to_dict(self):
        c = _delivery_ir()
        d = asdict(c)
        assert d["id"] == "DELIVERY_SLA"
        assert d["lifecycle"]["initial"] == "Proposed"
        assert d["lifecycle"]["transitions"][0]["id"] == "assign"
        assert d["monitors"][0]["id"] == "battery_guard"
        # All values must be JSON-friendly primitives / lists / dicts
        import json
        json.dumps(d, ensure_ascii=False)


class TestBackwardCompat:
    def test_plain_contract_has_no_extension(self):
        sos = parse(
            "sos:\n  name: Plain\n  contracts:\n"
            "    - id: P\n      parties: [A]\n"
        )
        ir = lower_to_ir(sos)
        c = ir.institution.contracts[0]
        assert c.lifecycle is None
        assert c.monitors == []
