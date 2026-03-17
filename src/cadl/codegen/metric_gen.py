"""CADL Metrics Code Generator — generates metrics collector."""

from __future__ import annotations

from typing import List

from ..ast_nodes import MetricDef
from .emitter import snake_case


def generate_metrics_module(metrics: List[MetricDef]) -> str:
    """Generate the metrics.py module with a MetricsCollector class."""
    if not metrics:
        return _empty_module()

    lines = []
    lines.append('"""Generated metrics collector for CADL SoS definition."""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from cadl.codegen.runtime_support import MetricsCollectorBase")
    lines.append("")
    lines.append("")
    lines.append("class MetricsCollector(MetricsCollectorBase):")
    lines.append('    """Collects and evaluates SoS performance metrics."""')
    lines.append("")

    for metric in metrics:
        method_name = f"compute_{snake_case(metric.id)}"
        lines.append(f"    def {method_name}(self, ctx) -> float:")
        lines.append(f'        """Metric: {metric.id}')
        if metric.formula:
            lines.append(f"")
            lines.append(f"        Formula: {metric.formula}")
        if metric.target:
            lines.append(f"        Target: {metric.target}")
        lines.append(f'        """')
        if metric.formula:
            py_formula = _compile_formula(metric.formula)
            if py_formula:
                lines.append(f"        return {py_formula}")
            else:
                lines.append(f"        # formula: {metric.formula}")
                lines.append(f"        raise NotImplementedError('{method_name}')")
        else:
            lines.append(f"        raise NotImplementedError('{method_name}')")
        lines.append("")

    # Target check methods
    for metric in metrics:
        if metric.target:
            method_name = f"check_{snake_case(metric.id)}_target"
            lines.append(f"    def {method_name}(self, value: float) -> bool:")
            lines.append(f'        """Check if {metric.id} meets target: {metric.target}."""')
            py_target = _compile_target(metric.target)
            if py_target:
                lines.append(f"        return {py_target}")
            else:
                lines.append(f"        return True  # could not compile target: {metric.target}")
            lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def _compile_formula(formula: str) -> str | None:
    """Try to compile a formula string to Python source."""
    try:
        from ..parser import parse_expr
        from .expr_compiler import CompilerContext, expr_to_python
        expr = parse_expr(formula)
        ctx = CompilerContext()
        return expr_to_python(expr, ctx)
    except Exception:
        return None


def _compile_target(target: str) -> str | None:
    """Compile a target string like '>= 0.95' to a Python expression using 'value'."""
    import re
    m = re.match(r'([<>=!]+)\s*(.+)', target.strip())
    if not m:
        return None
    op, val = m.group(1), m.group(2).strip()
    # Try to parse val as a number or duration
    try:
        float(val)
        return f"value {op} {val}"
    except ValueError:
        pass
    # Try as duration (e.g., "30min") — convert to seconds for comparison
    m2 = re.match(r'(\d+(?:\.\d+)?)(ms|s|min|h)$', val)
    if m2:
        amount, unit = float(m2.group(1)), m2.group(2)
        unit_to_seconds = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0}
        seconds = amount * unit_to_seconds.get(unit, 1.0)
        return f"value {op} {seconds}"
    return None


def _empty_module() -> str:
    return (
        '"""Generated metrics collector — no metrics defined."""\n'
        "\n"
        "from __future__ import annotations\n"
    )
