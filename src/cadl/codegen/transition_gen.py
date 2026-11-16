"""CADL Transition Code Generator — generates regime controller."""

from __future__ import annotations

from typing import List

from ..ast_nodes import TransitionDef
from .emitter import sanitize_id


def generate_transitions_module(
    transitions: List[TransitionDef],
    bindings: list | None = None,
    declared_params: dict | None = None,
    initial: str | None = None,
) -> str:
    """Generate the transitions.py module with a RegimeController class.

    ``bindings`` are the resolved regime bindings of `regimes:` (Appendix A,
    A.7.1, `cadl.regimes.checked`), or None for a file without the block; the
    module is then the same as without them. ``declared_params`` maps each
    bound parameter to its declared value. ``initial`` is the regime in
    which a run starts (A.7.1, `cadl.regimes.initial_regime`); it is used
    with bindings only, so that a file without them keeps its module."""
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
    if bindings is not None and initial is not None:
        first_regime = initial          # the initial regime of A.7.1
    lines.append(f"    def __init__(self, initial_regime: str = '{first_regime}') -> None:")
    if bindings is not None:
        lines.append("        if initial_regime not in self.REGIMES:")
        lines.append("            raise ValueError(f'{initial_regime!r} is not a regime of the file "
                     "({\", \".join(self.REGIMES)})')")
    lines.append("        self.current_regime = initial_regime")
    lines.append("        self.transition_history: list[tuple[str, str]] = []")
    lines.append("        # (from, to, reason) of each transition not taken because its")
    lines.append("        # safety invariant did not hold or could not be checked (A.7.1)")
    lines.append("        self.refused_transitions: list[tuple[str, str, str]] = []")
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

    _emit_checked_execute(lines, transitions, bindings is not None)

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def _emit_checked_execute(lines: list, transitions: List[TransitionDef], bound: bool) -> None:
    """execute_transition (A.7.1, "Runs"):
    the safety invariant is checked explicitly (not with assert, which
    `python -O` removes); when it does not hold, or cannot be compiled, the
    transition is not taken, the regime stays, and the refusal is
    recorded in `refused_transitions` and the log."""
    lines.append("    def execute_transition(self, target_regime: str, ctx: RuntimeContext) -> bool:")
    lines.append('        """Take a regime transition if its safety invariant holds (A.7.1).')
    lines.append("        Returns False, and keeps the current regime, when it does not hold")
    lines.append('        or cannot be checked."""')
    if bound:
        lines.append("        if target_regime not in self.REGIMES:")
        lines.append("            raise ValueError(f'{target_regime!r} is not a regime of the file "
                     "({\", \".join(self.REGIMES)})')")
    lines.append("        old = self.current_regime")
    for t in transitions:
        if not t.safety_invariant:
            continue
        text = t.safety_invariant.replace("\n", " ").strip()
        py_inv = _compile_expr_str(t.safety_invariant)
        lines.append(f"        if old == {t.from_regime!r} and target_regime == {t.to_regime!r}:")
        if py_inv:
            lines.append("            try:")
            lines.append(f"                holds = bool({py_inv})")
            lines.append("            except Exception:")
            lines.append("                holds = None")
            lines.append("            if holds is not True:")
            lines.append(f"                reason = ('safety invariant does not hold: ' if holds is False")
            lines.append(f"                          else 'safety invariant cannot be evaluated: ') + {text!r}")
            pad = "                "
        else:
            lines.append(f"            reason = 'safety invariant cannot be checked (not compiled): ' + {text!r}")
            pad = "            "
        lines.append(f"{pad}self.refused_transitions.append((old, target_regime, reason))")
        lines.append(f"{pad}ctx.record_log(f'Regime transition refused: {{old}} -> {{target_regime}} ({{reason}})')")
        lines.append(f"{pad}return False")
    lines.append("        self.current_regime = target_regime")
    lines.append("        self.transition_history.append((old, target_regime))")
    if bound:
        lines.append("        self.apply_bindings(ctx)")
    lines.append("        ctx.record_log(f'Regime transition: {old} -> {target_regime}')")
    lines.append("        return True")
    lines.append("")


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
