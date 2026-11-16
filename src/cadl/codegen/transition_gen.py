"""CADL Transition Code Generator — generates regime controller."""

from __future__ import annotations

from typing import List

from ..ast_nodes import TransitionDef
from .emitter import sanitize_id


def generate_transitions_module(
    transitions: List[TransitionDef],
    bindings: list | None = None,
    declared_params: dict | None = None,
) -> str:
    """Generate the transitions.py module with a RegimeController class.

    ``bindings`` are the resolved regime bindings of `regimes:` (Appendix A,
    A.7.1, `cadl.regimes.checked`), or None for a file without the block; the
    module is then the same as without them. ``declared_params`` maps each
    bound parameter to its declared value."""
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
    if bindings is not None:
        _emit_bindings(lines, bindings, declared_params or {})

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
        lines.append(f"        # {t.from_regime} -> {t.to_regime}")
        lines.append(f"        if self.current_regime == '{t.from_regime}':")
        if t.condition:
            py_cond = _compile_expr_str(t.condition)
            if py_cond:
                lines.append(f"            if {py_cond}:")
                lines.append(f"                return '{t.to_regime}'")
            else:
                cond_comment = t.condition.replace("\n", " ").strip()
                lines.append(f"            # condition: {cond_comment}")
                lines.append(f"            pass  # could not compile condition")
        else:
            lines.append(f"            return '{t.to_regime}'")
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
                py_inv = _compile_expr_str(t.safety_invariant)
                lines.append(f"        if old == '{t.from_regime}' and target_regime == '{t.to_regime}':")
                if py_inv:
                    lines.append(f"            assert {py_inv}, 'Safety invariant violated: {t.safety_invariant}'")
                else:
                    lines.append(f"            # safety_invariant: {t.safety_invariant} (could not compile)")

    lines.append("        self.current_regime = target_regime")
    lines.append("        self.transition_history.append((old, target_regime))")
    if bindings is not None:
        lines.append("        self.apply_bindings(ctx)")
    lines.append(f"        ctx.record_log(f'Regime transition: {{old}} -> {{target_regime}}')")
    lines.append("        return True")
    lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def _emit_bindings(lines: list, bindings: list, declared_params: dict) -> None:
    """The regime bindings of `regimes:` as class data and three methods."""
    lines.append("    # Regime bindings (regimes:, Appendix A, A.7.1)")
    lines.append(f"    DECLARED_PARAMETERS = {dict(declared_params)!r}")
    regime_params = {b.name: dict(b.parameters) for b in bindings}
    suspended = {b.name: list(b.suspended) for b in bindings}
    lines.append(f"    REGIME_PARAMETERS = {regime_params!r}")
    lines.append(f"    SUSPENDED_CONTRACTS = {suspended!r}")
    lines.append("")
    lines.append("    def parameters(self) -> dict:")
    lines.append('        """The value of every bound parameter in the current regime."""')
    lines.append("        values = dict(self.DECLARED_PARAMETERS)")
    lines.append("        values.update(self.REGIME_PARAMETERS.get(self.current_regime, {}))")
    lines.append("        return values")
    lines.append("")
    lines.append("    def contract_active(self, contract_id: str) -> bool:")
    lines.append('        """Whether a contract is in force in the current regime."""')
    lines.append("        return contract_id not in self.SUSPENDED_CONTRACTS.get(self.current_regime, [])")
    lines.append("")
    lines.append("    def apply_bindings(self, ctx: RuntimeContext) -> None:")
    lines.append('        """Publish the parameter values of the current regime in ctx.parameters."""')
    lines.append("        ctx.parameters = self.parameters()")
    lines.append("")


def _compile_expr_str(condition: str) -> str | None:
    """Try to parse and compile a condition string to Python source."""
    try:
        from ..parser import parse_expr
        from .expr_compiler import CompilerContext, expr_to_python
        expr = parse_expr(condition)
        ctx = CompilerContext()
        return expr_to_python(expr, ctx)
    except Exception:
        return None


def _empty_module() -> str:
    return (
        '"""Generated regime controller — no transitions defined."""\n'
        "\n"
        "from __future__ import annotations\n"
    )
