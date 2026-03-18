"""CADL Regime Map — directed graph of regime states with transitions.

Builds a regime map from TransitionDef AST nodes and provides graph-theoretic
analysis: reachability, dead-state detection, cycle detection (Tarjan SCC),
shortest path, and export to Graphviz DOT / JSON.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .ast_nodes import SoSDefinition, TransitionDef


@dataclass
class RegimeState:
    """A node in the regime map graph."""
    name: str
    incoming: list[int] = field(default_factory=list)  # indices into transitions
    outgoing: list[int] = field(default_factory=list)   # indices into transitions


@dataclass
class RegimeMap:
    """Directed graph of regime states connected by transitions.

    Build via ``RegimeMap.from_sos(sos)`` or ``RegimeMap.from_transitions(transitions)``.
    """
    states: dict[str, RegimeState] = field(default_factory=dict)
    transitions: list[TransitionDef] = field(default_factory=list)
    initial_state: str | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_sos(cls, sos: SoSDefinition) -> RegimeMap:
        """Build a regime map from an SoS definition."""
        return cls.from_transitions(sos.transitions)

    @classmethod
    def from_transitions(cls, transitions: list[TransitionDef]) -> RegimeMap:
        """Build a regime map from a list of transitions."""
        rm = cls(transitions=list(transitions))

        for idx, t in enumerate(transitions):
            # Ensure states exist
            for name in (t.from_regime, t.to_regime):
                if name not in rm.states:
                    rm.states[name] = RegimeState(name=name)
            rm.states[t.from_regime].outgoing.append(idx)
            rm.states[t.to_regime].incoming.append(idx)

        # Infer initial state: first from_regime that never appears as to_regime
        to_regimes = {t.to_regime for t in transitions}
        for t in transitions:
            if t.from_regime not in to_regimes:
                rm.initial_state = t.from_regime
                break

        # Fallback: first from_regime
        if rm.initial_state is None and transitions:
            rm.initial_state = transitions[0].from_regime

        return rm

    # ------------------------------------------------------------------
    # Graph algorithms
    # ------------------------------------------------------------------

    def reachable_states(self, start: str | None = None) -> set[str]:
        """BFS from *start* (default: initial_state). Returns reachable state names."""
        origin = start or self.initial_state
        if origin is None or origin not in self.states:
            return set()

        visited: set[str] = set()
        queue: deque[str] = deque([origin])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for idx in self.states[current].outgoing:
                target = self.transitions[idx].to_regime
                if target not in visited:
                    queue.append(target)
        return visited

    def find_unreachable_states(self) -> set[str]:
        """States not reachable from the initial state."""
        if not self.states:
            return set()
        reachable = self.reachable_states()
        return set(self.states.keys()) - reachable

    def find_dead_states(self) -> set[str]:
        """States with no outgoing transitions (dead-ends).

        A single-state map with no outgoing is not considered dead.
        """
        if len(self.states) <= 1:
            return set()
        return {
            name for name, state in self.states.items()
            if not state.outgoing
        }

    def find_cycles(self) -> list[list[str]]:
        """Find all strongly connected components with size > 1 using Tarjan's algorithm.

        Returns a list of cycles, where each cycle is a list of state names.
        """
        index_counter = [0]
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        sccs: list[list[str]] = []

        def strongconnect(v: str) -> None:
            indices[v] = index_counter[0]
            lowlinks[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)

            for idx in self.states[v].outgoing:
                w = self.transitions[idx].to_regime
                if w not in indices:
                    strongconnect(w)
                    lowlinks[v] = min(lowlinks[v], lowlinks[w])
                elif w in on_stack:
                    lowlinks[v] = min(lowlinks[v], indices[w])

            if lowlinks[v] == indices[v]:
                scc: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    sccs.append(scc[::-1])  # reverse for natural order
                elif len(scc) == 1 and self._has_self_loop(scc[0]):
                    sccs.append(scc)

        for name in self.states:
            if name not in indices:
                strongconnect(name)

        return sccs

    def _has_self_loop(self, state_name: str) -> bool:
        """Check if a state has a transition back to itself."""
        for idx in self.states[state_name].outgoing:
            if self.transitions[idx].to_regime == state_name:
                return True
        return False

    def shortest_path(self, start: str, end: str) -> list[str] | None:
        """BFS shortest path from *start* to *end*. Returns list of state names or None."""
        if start not in self.states or end not in self.states:
            return None
        if start == end:
            return [start]

        visited: set[str] = set()
        queue: deque[list[str]] = deque([[start]])
        while queue:
            path = queue.popleft()
            current = path[-1]
            if current in visited:
                continue
            visited.add(current)

            for idx in self.states[current].outgoing:
                target = self.transitions[idx].to_regime
                new_path = path + [target]
                if target == end:
                    return new_path
                if target not in visited:
                    queue.append(new_path)
        return None

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def to_dot(self) -> str:
        """Export the regime map as Graphviz DOT format."""
        lines: list[str] = []
        lines.append("digraph RegimeMap {")
        lines.append("  rankdir=LR;")
        lines.append('  node [shape=box, style=rounded, fontname="Helvetica"];')
        lines.append('  edge [fontname="Helvetica", fontsize=10];')
        lines.append("")

        # Mark initial state with double border
        if self.initial_state and self.initial_state in self.states:
            lines.append(f'  "{self.initial_state}" [style="rounded,bold", penwidth=2];')

        # Mark dead states in red
        dead = self.find_dead_states()
        for name in dead:
            lines.append(f'  "{name}" [color=red, fontcolor=red];')

        lines.append("")

        # Edges
        for t in self.transitions:
            label_parts: list[str] = []
            if t.condition:
                label_parts.append(t.condition)
            if t.protocol:
                label_parts.append(f"[{t.protocol}]")
            label = "\\n".join(label_parts) if label_parts else ""
            lines.append(f'  "{t.from_regime}" -> "{t.to_regime}" [label="{label}"];')

        lines.append("}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        """Export the regime map as a JSON-serializable dict."""
        return {
            "initial_state": self.initial_state,
            "states": sorted(self.states.keys()),
            "transitions": [
                {
                    "from": t.from_regime,
                    "to": t.to_regime,
                    "condition": t.condition,
                    "protocol": t.protocol,
                    "safety_invariant": t.safety_invariant,
                }
                for t in self.transitions
            ],
            "analysis": {
                "reachable": sorted(self.reachable_states()),
                "unreachable": sorted(self.find_unreachable_states()),
                "dead_states": sorted(self.find_dead_states()),
                "cycles": self.find_cycles(),
            },
        }

    def to_text(self) -> str:
        """Human-readable text summary of the regime map."""
        lines: list[str] = []
        lines.append(f"Regime Map: {len(self.states)} states, {len(self.transitions)} transitions")
        if self.initial_state:
            lines.append(f"  Initial state: {self.initial_state}")
        lines.append("")

        lines.append("States:")
        for name in sorted(self.states.keys()):
            state = self.states[name]
            markers: list[str] = []
            if name == self.initial_state:
                markers.append("initial")
            if name in self.find_dead_states():
                markers.append("dead-end")
            if name in self.find_unreachable_states():
                markers.append("unreachable")
            suffix = f" ({', '.join(markers)})" if markers else ""
            lines.append(f"  {name}{suffix}")
        lines.append("")

        lines.append("Transitions:")
        for t in self.transitions:
            cond = f" when {t.condition}" if t.condition else ""
            proto = f" via {t.protocol}" if t.protocol else ""
            lines.append(f"  {t.from_regime} -> {t.to_regime}{cond}{proto}")
        lines.append("")

        # Analysis
        reachable = self.reachable_states()
        unreachable = self.find_unreachable_states()
        dead = self.find_dead_states()
        cycles = self.find_cycles()

        lines.append("Analysis:")
        lines.append(f"  Reachable states: {len(reachable)}/{len(self.states)}")
        if unreachable:
            lines.append(f"  Unreachable: {', '.join(sorted(unreachable))}")
        if dead:
            lines.append(f"  Dead-end states: {', '.join(sorted(dead))}")
        if cycles:
            for cycle in cycles:
                lines.append(f"  Cycle: {' -> '.join(cycle)} -> {cycle[0]}")
        else:
            lines.append("  No cycles detected")

        return "\n".join(lines)
