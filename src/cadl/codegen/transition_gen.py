"""CADL Transition Code Generator — generates regime controller."""

from __future__ import annotations

from typing import List

from ..ast_nodes import TransitionDef
from .emitter import sanitize_id


def generate_transitions_module(transitions: List[TransitionDef]) -> str:
    """Generate the transitions.py module with a RegimeController class."""
    if not transitions:
        return _empty_module()

    lines = []
    lines.append('"""Generated regime controller for CADL SoS definition."""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from cadl.codegen.runtime_support import RuntimeContext")
    lines.append("")
    lines.append("")
    lines.append("class RegimeController:")
    lines.append('    """Controls regime transitions based on environmental conditions."""')
    lines.append("")

    # Collect all regime names
    regimes = set()
    for t in transitions:
        regimes.add(t.from_regime)
        regimes.add(t.to_regime)

    lines.append(f"    REGIMES = {sorted(regimes)!r}")
    lines.append("")

    # Constructor
    first_regime = transitions[0].from_regime if transitions else "UNKNOWN"
    lines.append(f"    def __init__(self, initial_regime: str = '{first_regime}') -> None:")
    lines.append("        self.current_regime = initial_regime")
    lines.append("        self.transition_history: list[tuple[str, str]] = []")
    lines.append("")

    # Evaluate transitions
    lines.append("    def evaluate_transitions(self, ctx: RuntimeContext) -> str | None:")
    lines.append('        """Check if any transition from current regime should fire."""')
    for t in transitions:
        cond_str = t.condition or "True"
        cond_comment = cond_str.replace("\n", " ").strip()
        lines.append(f"        # {t.from_regime} -> {t.to_regime}")
        lines.append(f"        if self.current_regime == '{t.from_regime}':")
        lines.append(f"            # condition: {cond_comment}")
        lines.append(f"            pass  # TODO: evaluate condition")
        lines.append("")
    lines.append("        return None")
    lines.append("")

    # Execute transition
    lines.append("    def execute_transition(self, target_regime: str, ctx: RuntimeContext) -> bool:")
    lines.append('        """Execute a regime transition, checking safety invariants."""')
    lines.append("        old = self.current_regime")

    # Safety invariants
    has_invariants = any(t.safety_invariant for t in transitions)
    if has_invariants:
        lines.append("        # Check safety invariants")
        for t in transitions:
            if t.safety_invariant:
                lines.append(f"        if old == '{t.from_regime}' and target_regime == '{t.to_regime}':")
                lines.append(f"            # safety_invariant: {t.safety_invariant}")
                lines.append("            pass  # TODO: verify invariant")

    lines.append("        self.current_regime = target_regime")
    lines.append("        self.transition_history.append((old, target_regime))")
    lines.append(f"        ctx.record_log(f'Regime transition: {{old}} -> {{target_regime}}')")
    lines.append("        return True")
    lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def _empty_module() -> str:
    return (
        '"""Generated regime controller — no transitions defined."""\n'
        "\n"
        "from __future__ import annotations\n"
    )
