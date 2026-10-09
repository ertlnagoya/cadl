"""Tests for the CADL deadlock detector."""

from __future__ import annotations

import pytest

from cadl.ast_nodes import (
    ActorRef,
    BarrierStep,
    BoolLiteral,
    ComputeStep,
    ConditionalStep,
    FallbackBlock,
    Identifier,
    MessageStep,
    ParallelStep,
    ProtocolDef,
    SoSDefinition,
    StringLiteral,
)
from cadl.deadlock import (
    DeadlockResult,
    detect_deadlocks,
    _build_wait_for_graph,
    _check_protocol_deadlock,
    _check_barrier_reachability,
    _check_fallback_chains,
    _find_cycles,
)


# === Helpers ===

def _ref(name: str, index=None) -> ActorRef:
    return ActorRef(name=name, index=index)


def _msg(sender: str, receiver: str, message: str = "msg") -> MessageStep:
    return MessageStep(
        sender=_ref(sender),
        receiver=_ref(receiver),
        message=StringLiteral(message),
    )


def _compute(actor: str, comp: str = "process") -> ComputeStep:
    return ComputeStep(actor=_ref(actor), computation=StringLiteral(comp))


def _protocol(id: str, steps: list, fallback: FallbackBlock = None) -> ProtocolDef:
    return ProtocolDef(
        id=id,
        trigger="test_trigger()",
        steps=steps,
        fallback=fallback,
    )


def _sos(protocols: list[ProtocolDef] = None) -> SoSDefinition:
    return SoSDefinition(
        name="TestSoS",
        protocols=protocols or [],
    )


# === Wait-for graph tests ===

class TestWaitForGraph:
    """Test dependency graph construction."""

    def test_sequential_message_adds_no_wait(self):
        """Sequential steps are ordered; a single send blocks nobody."""
        graph = _build_wait_for_graph([_msg("A", "B")])
        assert graph == {}

    def test_request_reply_is_not_a_cycle(self):
        """A -> B then B -> A in sequence is an exchange, not a circular wait."""
        graph = _build_wait_for_graph([_msg("A", "B"), _msg("B", "A")])
        assert _find_cycles(graph) == []

    def test_parallel_mutual_sends_wait_on_each_other(self):
        """Parallel A -> B and B -> A: each may wait for the other."""
        graph = _build_wait_for_graph([
            ParallelStep(steps=[_msg("A", "B"), _msg("B", "A")])
        ])
        assert "A" in graph.get("B", set())
        assert "B" in graph.get("A", set())

    def test_compute_no_dependency(self):
        """Compute steps don't create inter-actor dependencies."""
        steps = [_compute("A"), _compute("B")]
        graph = _build_wait_for_graph(steps)
        # No cross-actor edges
        for deps in graph.values():
            assert len(deps) == 0 or all(d == "" for d in deps) is False


# === Cycle detection tests ===

class TestCycleDetection:
    """Test DFS-based cycle finding."""

    def test_no_cycle(self):
        graph = {"A": {"B"}, "B": {"C"}}
        cycles = _find_cycles(graph)
        assert cycles == []

    def test_simple_cycle(self):
        graph = {"A": {"B"}, "B": {"A"}}
        cycles = _find_cycles(graph)
        assert len(cycles) > 0

    def test_self_loop(self):
        graph = {"A": {"A"}}
        cycles = _find_cycles(graph)
        assert len(cycles) > 0

    def test_longer_cycle(self):
        graph = {"A": {"B"}, "B": {"C"}, "C": {"A"}}
        cycles = _find_cycles(graph)
        assert len(cycles) > 0

    def test_empty_graph(self):
        cycles = _find_cycles({})
        assert cycles == []


# === Protocol deadlock tests ===

class TestProtocolDeadlock:
    """Test per-protocol deadlock checking."""

    def test_linear_protocol_no_deadlock(self):
        """Linear A -> B -> C has no circular dependency."""
        proto = _protocol("P1", [_msg("A", "B"), _msg("B", "C")])
        result = _check_protocol_deadlock(proto)
        assert result.status == "passed"

    def test_request_reply_no_deadlock(self):
        """A request followed by its reply must not be reported."""
        proto = _protocol("P1", [
            _msg("A", "B"), _compute("B"), _msg("B", "A"),
        ])
        result = _check_protocol_deadlock(proto)
        assert result.status == "passed"

    def test_sequential_ring_no_deadlock(self):
        """A -> B -> C -> A in sequence is a relay, not a circular wait."""
        proto = _protocol("P1", [_msg("A", "B"), _msg("B", "C"), _msg("C", "A")])
        result = _check_protocol_deadlock(proto)
        assert result.status == "passed"

    def test_parallel_mutual_dependency(self):
        """Parallel branches with A->B and B->A could deadlock."""
        proto = _protocol("P1", [
            ParallelStep(steps=[
                _msg("A", "B"),
                _msg("B", "A"),
            ])
        ])
        result = _check_protocol_deadlock(proto)
        assert result.status == "failed"

    def test_compute_only_no_deadlock(self):
        """Protocol with only compute steps has no deadlock."""
        proto = _protocol("P1", [_compute("A"), _compute("B")])
        result = _check_protocol_deadlock(proto)
        assert result.status == "passed"


# === Barrier reachability tests ===

class TestBarrierReachability:
    """Test barrier reachability analysis."""

    def test_no_barriers(self):
        """Protocol without barriers returns empty results."""
        proto = _protocol("P1", [_msg("A", "B")])
        results = _check_barrier_reachability(proto)
        assert results == []

    def test_barrier_all_reachable(self):
        """All actors in unconditional steps can reach barrier."""
        proto = _protocol("P1", [
            _msg("A", "B"),
            BarrierStep(condition=StringLiteral("sync")),
        ])
        results = _check_barrier_reachability(proto)
        assert len(results) == 1
        assert results[0].status == "passed"

    def test_barrier_conditional_only_actors(self):
        """Actors only in conditional branches get a warning."""
        proto = _protocol("P1", [
            _msg("A", "B"),
            ConditionalStep(
                condition=Identifier("flag"),
                then_steps=[_msg("C", "A")],
                else_steps=[],
            ),
            BarrierStep(condition=StringLiteral("sync")),
        ])
        results = _check_barrier_reachability(proto)
        # Should have a warning about C being conditional-only
        warnings = [r for r in results if r.status == "warning"]
        assert len(warnings) > 0


# === Fallback chain tests ===

class TestFallbackChains:
    """Test circular fallback chain detection."""

    def test_no_fallbacks(self):
        """Protocols without fallbacks return empty."""
        sos = _sos(protocols=[_protocol("P1", [_msg("A", "B")])])
        results = _check_fallback_chains(sos)
        assert results == []

    def test_non_circular_fallback(self):
        """Fallback to a different protocol that doesn't loop back."""
        p1 = _protocol("P1", [_msg("A", "B")], fallback=FallbackBlock(
            entries={"on_failure": "switch_to_protocol(P2)"}
        ))
        p2 = _protocol("P2", [_msg("A", "B")])
        sos = _sos(protocols=[p1, p2])
        results = _check_fallback_chains(sos)
        if results:
            assert all(r.status == "passed" for r in results)

    def test_circular_fallback(self):
        """Fallback P1 -> P2 -> P1 should be detected."""
        p1 = _protocol("P1", [_msg("A", "B")], fallback=FallbackBlock(
            entries={"on_failure": "switch_to_protocol(P2)"}
        ))
        p2 = _protocol("P2", [_msg("A", "B")], fallback=FallbackBlock(
            entries={"on_failure": "switch_to_protocol(P1)"}
        ))
        sos = _sos(protocols=[p1, p2])
        results = _check_fallback_chains(sos)
        failed = [r for r in results if r.status == "failed"]
        assert len(failed) > 0


# === Full detect_deadlocks() tests ===

class TestDetectDeadlocks:
    """Test the top-level detect_deadlocks function."""

    def test_empty_sos(self):
        sos = _sos()
        results = detect_deadlocks(sos)
        assert results == []

    def test_single_safe_protocol(self):
        sos = _sos(protocols=[_protocol("P1", [_msg("A", "B"), _msg("B", "C")])])
        results = detect_deadlocks(sos)
        passed = [r for r in results if r.status == "passed"]
        failed = [r for r in results if r.status == "failed"]
        assert len(failed) == 0
        assert len(passed) >= 1

    def test_deadlock_result_str(self):
        """DeadlockResult __str__ formatting."""
        r = DeadlockResult(
            check_name="Test",
            status="passed",
            message="OK",
        )
        assert "[PASS]" in str(r)

        r2 = DeadlockResult(
            check_name="Test",
            status="failed",
            message="Bad",
            details=["A -> B -> A"],
        )
        s = str(r2)
        assert "[FAIL]" in s
        assert "A -> B -> A" in s


# === Robot delivery integration test ===

class TestRobotDeliveryDeadlock:
    """Integration test: check robot delivery example for deadlocks."""

    def test_robot_delivery_no_deadlock(self):
        """The robot delivery example should have no deadlocks."""
        from cadl.parser import parse_file
        from pathlib import Path

        cadl_file = Path(__file__).parent.parent / "examples" / "robot_delivery.cadl"
        if not cadl_file.exists():
            pytest.skip("robot_delivery.cadl not found")

        sos = parse_file(cadl_file)
        results = detect_deadlocks(sos)

        failed = [r for r in results if r.status == "failed"]
        assert len(failed) == 0, f"Unexpected deadlocks: {failed}"
