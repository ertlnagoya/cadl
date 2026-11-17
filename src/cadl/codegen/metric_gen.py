"""CADL Metrics Code Generator — generates metrics collector."""

from __future__ import annotations

from typing import List

from ..ast_nodes import MetricDef
from .emitter import snake_case


def generate_metrics_module(metrics: List[MetricDef], schedules: dict | None = None) -> str:
    """Generate the metrics.py module with a MetricsCollector class.

    ``schedules`` maps the id of a metric with ``schedule:`` (amendment 0.1,
    Appendix K, K.5) to its entries, each ``(from, target)`` where `from` is
    ``{"period": k}``, ``{"at": text}``, ``{"effective": dict or None}`` for
    an entry from this version, or None for an entry from another version.
    Its target check then takes the time and the time base (K.11)."""
    if not metrics:
        return _empty_module()
    schedules = schedules or {}

    lines = []
    lines.append('"""Generated metrics collector for CADL SoS definition."""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from cadl.codegen.runtime_support import MetricsCollectorBase")
    lines.append("")
    lines.append("")
    if any(m.id in schedules for m in metrics):
        lines += _SCHEDULE_HELPER
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
        if metric.id in schedules:
            lines += _scheduled_check(metric, schedules[metric.id])
        elif metric.target:
            method_name = f"check_{snake_case(metric.id)}_target"
            lines.append(f"    def {method_name}(self, value: float) -> bool:")
            lines.append(f'        """Check if {metric.id} meets target: {metric.target}."""')
            py_target = _compile_target(metric.target)
            if py_target:
                lines.append(f"        return {py_target}")
            else:
                # not judged (None): a target that cannot be compiled is never
                # met silently (up to cadl 0.3.33 this returned True)
                lines.append(f"        return None  # could not compile target: {metric.target}")
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


_SCHEDULE_HELPER = [
    "def _from_time(when, period, origin, epoch):",
    '    """The time stamp at which a schedule entry applies, or None when the',
    '    time base has nothing for it (Appendix K, K.11)."""',
    "    import datetime as _dt",
    "    from fractions import Fraction",
    "    if when is None:",
    "        return None                    # an entry from another version",
    "    (form, value), = when.items()",
    "    if form == 'effective':           # an entry from this version",
    "        if value is None:",
    "            return float('-inf')",
    "        return _from_time(value, period, origin, epoch)",
    "    if form == 'period':",
    "        if period is None:",
    "            return None",
    "        return Fraction(origin) + (int(value) - 1) * Fraction(period)",
    "    if not epoch:",
    "        return None",
    "    try:",
    "        t = _dt.datetime.fromisoformat(value)",
    "    except ValueError:",
    "        t = _dt.datetime.combine(_dt.date.fromisoformat(value), _dt.time())",
    "    if t.tzinfo is None:",
    "        t = t.replace(tzinfo=_dt.timezone.utc)",
    "    d = t - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)",
    "    return Fraction(d.days * 86400 + d.seconds) + Fraction(d.microseconds, 1000000)",
    "",
    "",
]


def _lambda(target) -> str:
    py = _compile_target(str(target)) if target is not None else None
    return f"lambda value: {py}" if py else "None"


def _scheduled_check(metric: MetricDef, entries: list) -> list:
    """The target check of a metric with schedule: (Appendix K, K.11)."""
    name = f"check_{snake_case(metric.id)}_target"
    out = [
        f"    def {name}(self, value: float, t=None, period=None, origin=0, epoch=False):",
        f'        """Check {metric.id} against the target in force at time t (schedule:,',
        "        Appendix K, K.11): period and origin give the time base of",
        "        `period:` entries, epoch that time stamps are seconds since",
        "        1970-01-01T00:00:00Z. Returns None (not judged) when t is not given,",
        "        an entry cannot be placed, or the target in force cannot be compiled.",
        "        It assumes that its own version is in force at t: a host runs only the",
        "        code of the version in force (Appendix K, K.11). An entry from another",
        "        version cannot be placed here and gives None. The check is for the one",
        "        time t: whether the target changes inside a measurement window is for",
        '        the caller to check (cadl metrics-eval does)."""',
        "        if t is None:",
        "            return None",
        f"        current = {_lambda(metric.target)}",
    ]
    for when, target in entries:
        out += [
            f"        at = _from_time({when!r}, period, origin, epoch)",
            "        if at is None:",
            "            return None",
            "        if at <= t:",
            f"            current = {_lambda(target)}  # {str(target)}",
        ]
    out += [
        "        return None if current is None else current(value)",
        "",
    ]
    return out


def _empty_module() -> str:
    return (
        '"""Generated metrics collector — no metrics defined."""\n'
        "\n"
        "from __future__ import annotations\n"
    )
