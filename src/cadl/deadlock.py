"""CADL Deadlock Detector - analyzes protocols for potential deadlocks.

Checks:
1. Circular waits between the parallel branches of a protocol
2. Barrier reachability (all required actors can reach the barrier)
3. Circular fallback chains across protocols
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .ast_nodes import (
    ActorRef,
    BarrierStep,
    ComputeStep,
    ConditionalStep,
    MessageStep,
    ParallelStep,
    ProtocolDef,
    SoSDefinition,
    Step,
)


@dataclass
class DeadlockResult:
    """Result of a deadlock check."""
    check_name: str
    status: str  # "passed", "failed", "warning"
    message: str
    details: Optional[List[str]] = None

    def __str__(self) -> str:
        icon = {"passed": "PASS", "failed": "FAIL", "warning": "WARN"}[self.status]
        s = f"[{icon}] {self.check_name}: {self.message}"
        if self.details:
            for d in self.details:
                s += f"\n        - {d}"
        return s


def _actor_name(ref: ActorRef) -> str:
    """Get a normalized actor name from an ActorRef."""
    name = ref.name
    if ref.index == "*":
        return f"{name}[*]"
    if ref.index is not None:
        return f"{name}[{ref.index}]"
    return name


def _collect_steps_flat(steps: List[Step]) -> List[Step]:
    """Flatten nested step structures into a linear list."""
    result = []
    for step in steps:
        result.append(step)
        if isinstance(step, ConditionalStep):
            result.extend(_collect_steps_flat(step.then_steps))
            result.extend(_collect_steps_flat(step.else_steps))
        elif isinstance(step, ParallelStep):
            result.extend(_collect_steps_flat(step.steps))
    return result


def _collect_actors_in_steps(steps: List[Step]) -> Set[str]:
    """Collect all actor names referenced in protocol steps."""
    actors = set()
    for step in _collect_steps_flat(steps):
        if isinstance(step, MessageStep):
            actors.add(_actor_name(step.sender))
            actors.add(_actor_name(step.receiver))
        elif isinstance(step, ComputeStep):
            actors.add(_actor_name(step.actor))
    return actors


def _build_wait_for_graph(steps: List[Step]) -> Dict[str, Set[str]]:
    """Build a wait-for graph from protocol steps.

    An edge A -> B means "A is blocked until B acts". Steps listed in
    sequence are totally ordered, so a request followed by its reply
    (A -> B, then B -> A) is an exchange, not a circular wait, and adds no
    edge. Waits can only become circular between the branches of a
    ``parallel`` block, which run without a fixed order.
    """
    graph: Dict[str, Set[str]] = defaultdict(set)

    for step in _collect_steps_flat(steps):
        if isinstance(step, ParallelStep):
            _analyze_parallel(step, graph)

    return dict(graph)


def _branch_sends(step: Step) -> Set[Tuple[str, str]]:
    """Collect the (sender, receiver) pairs of one parallel branch."""
    return {
        (_actor_name(s.sender), _actor_name(s.receiver))
        for s in _collect_steps_flat([step])
        if isinstance(s, MessageStep)
    }


def _analyze_parallel(parallel: ParallelStep, graph: Dict[str, Set[str]]) -> None:
    """Analyze parallel branches for potential deadlocks.

    If branch 1 has A->B and branch 2 has B->A, each side may be waiting
    to receive before it sends: a potential deadlock.
    """
    branch_sends = [_branch_sends(step) for step in parallel.steps]

    # Check for cross-branch circular dependencies
    for i, sends_i in enumerate(branch_sends):
        for j, sends_j in enumerate(branch_sends):
            if i >= j:
                continue
            for s1, r1 in sends_i:
                for s2, r2 in sends_j:
                    if s1 == r2 and s2 == r1:
                        # Mutual dependency: A sends to B in one branch,
                        # B sends to A in another
                        graph[r1].add(s1)
                        graph[r2].add(s2)


def _find_cycles(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Find all cycles in a directed graph using DFS."""
    cycles = []
    visited: Set[str] = set()
    rec_stack: Set[str] = set()
    path: List[str] = []

    def dfs(node: str) -> None:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in rec_stack:
                # Found a cycle
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                cycles.append(cycle)

        path.pop()
        rec_stack.discard(node)

    for node in graph:
        if node not in visited:
            dfs(node)

    return cycles


def _check_protocol_deadlock(protocol: ProtocolDef) -> DeadlockResult:
    """Check a single protocol for circular waits between parallel branches."""
    graph = _build_wait_for_graph(protocol.steps)
    cycles = _find_cycles(graph)

    if cycles:
        cycle_strs = [" -> ".join(c) for c in cycles]
        return DeadlockResult(
            check_name=f"Protocol '{protocol.id}' deadlock",
            status="failed",
            message=f"Circular dependency detected ({len(cycles)} cycle(s))",
            details=cycle_strs,
        )

    return DeadlockResult(
        check_name=f"Protocol '{protocol.id}' deadlock",
        status="passed",
        message="No circular dependencies found",
    )


def _check_barrier_reachability(protocol: ProtocolDef) -> List[DeadlockResult]:
    """Check that all actors referenced in barriers can reach the barrier point."""
    results = []
    all_steps = _collect_steps_flat(protocol.steps)
    actors_in_protocol = _collect_actors_in_steps(protocol.steps)

    for step in all_steps:
        if isinstance(step, BarrierStep):
            # Barrier requires all participating actors to synchronize
            # Check: are there actors in the protocol that might not reach the barrier
            # (e.g., actors only in conditional branches)
            conditional_only_actors = _find_conditional_only_actors(protocol.steps)
            if conditional_only_actors:
                results.append(DeadlockResult(
                    check_name=f"Protocol '{protocol.id}' barrier reachability",
                    status="warning",
                    message="Some actors appear only in conditional branches and may not reach barrier",
                    details=list(conditional_only_actors),
                ))

    if not results:
        # No barriers or all checks passed
        has_barriers = any(isinstance(s, BarrierStep) for s in all_steps)
        if has_barriers:
            results.append(DeadlockResult(
                check_name=f"Protocol '{protocol.id}' barrier reachability",
                status="passed",
                message="All actors can reach barrier points",
            ))

    return results


def _find_conditional_only_actors(steps: List[Step]) -> Set[str]:
    """Find actors that appear only in conditional branches (not unconditionally)."""
    unconditional_actors = set()
    conditional_actors = set()

    for step in steps:
        if isinstance(step, MessageStep):
            unconditional_actors.add(_actor_name(step.sender))
            unconditional_actors.add(_actor_name(step.receiver))
        elif isinstance(step, ComputeStep):
            unconditional_actors.add(_actor_name(step.actor))
        elif isinstance(step, ConditionalStep):
            then_actors = _collect_actors_in_steps(step.then_steps)
            else_actors = _collect_actors_in_steps(step.else_steps)
            conditional_actors |= then_actors | else_actors
        elif isinstance(step, ParallelStep):
            # Actors in parallel are considered reachable
            for s in step.steps:
                if isinstance(s, MessageStep):
                    unconditional_actors.add(_actor_name(s.sender))
                    unconditional_actors.add(_actor_name(s.receiver))
                elif isinstance(s, ComputeStep):
                    unconditional_actors.add(_actor_name(s.actor))

    return conditional_actors - unconditional_actors


def _check_fallback_chains(sos: SoSDefinition) -> List[DeadlockResult]:
    """Check for circular fallback chains across protocols."""
    results = []

    # Build fallback graph: protocol -> set of protocols it falls back to
    protocol_ids = {p.id for p in sos.protocols}
    fallback_graph: Dict[str, Set[str]] = defaultdict(set)

    for proto in sos.protocols:
        if proto.fallback:
            for _, action in proto.fallback.entries.items():
                action_str = str(action)
                # Look for references to other protocols
                for pid in protocol_ids:
                    if pid in action_str:
                        fallback_graph[proto.id].add(pid)

    # Check for cycles in fallback graph
    cycles = _find_cycles(dict(fallback_graph))

    if cycles:
        cycle_strs = [" -> ".join(c) for c in cycles]
        results.append(DeadlockResult(
            check_name="Fallback chain cycle",
            status="failed",
            message=f"Circular fallback chain detected ({len(cycles)} cycle(s))",
            details=cycle_strs,
        ))
    elif fallback_graph:
        results.append(DeadlockResult(
            check_name="Fallback chain cycle",
            status="passed",
            message="No circular fallback chains",
        ))

    return results


def detect_deadlocks(sos: SoSDefinition) -> List[DeadlockResult]:
    """Run all deadlock detection checks on a CADL SoS definition.

    Returns a list of DeadlockResult objects.
    """
    results: List[DeadlockResult] = []

    # 1. Check each protocol for circular dependencies
    for protocol in sos.protocols:
        results.append(_check_protocol_deadlock(protocol))

    # 2. Check barrier reachability
    for protocol in sos.protocols:
        results.extend(_check_barrier_reachability(protocol))

    # 3. Check for circular fallback chains
    results.extend(_check_fallback_chains(sos))

    return results
