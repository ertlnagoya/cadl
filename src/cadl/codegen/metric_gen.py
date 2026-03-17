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
        lines.append(f"        # TODO: implement formula: {metric.formula or 'N/A'}")
        lines.append(f"        raise NotImplementedError('{method_name}')")
        lines.append("")

    # Target check methods
    for metric in metrics:
        if metric.target:
            method_name = f"check_{snake_case(metric.id)}_target"
            lines.append(f"    def {method_name}(self, value: float) -> bool:")
            lines.append(f'        """Check if {metric.id} meets target: {metric.target}."""')
            lines.append(f"        # TODO: implement target check: {metric.target}")
            lines.append(f"        return True  # placeholder")
            lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def _empty_module() -> str:
    return (
        '"""Generated metrics collector — no metrics defined."""\n'
        "\n"
        "from __future__ import annotations\n"
    )
