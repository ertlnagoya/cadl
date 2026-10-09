"""CADL Code Generator — generates runtime code from CADL definitions.

Public API:
    generate(sos, output_dir, target="python") -> None

Supported targets: python, solidity, opa, unity-csharp
"""

from __future__ import annotations

from pathlib import Path

from ..ast_nodes import SoSDefinition

SUPPORTED_TARGETS = {"python", "solidity", "opa", "unity-csharp"}


def generate(sos: SoSDefinition, output_dir: Path, target: str = "python") -> None:
    """Generate runtime code from a CADL SoS definition.

    Args:
        sos: Parsed SoS definition.
        output_dir: Directory to write generated files.
        target: Code generation target — "python", "solidity", "opa",
            or "unity-csharp" (Unity C# for the SoS-DSL extension,
            Appendix E).
    """
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"Unsupported target: {target!r}. Choose from: {sorted(SUPPORTED_TARGETS)}")

    if target != "unity-csharp":
        from .safety import check_codegen_input
        check_codegen_input(sos)

    if target == "python":
        _generate_python(sos, output_dir)
    elif target == "solidity":
        from .solidity import generate_solidity
        generate_solidity(sos, output_dir)
    elif target == "opa":
        from .opa import generate_rego
        generate_rego(sos, output_dir)
    elif target == "unity-csharp":
        from .unity_csharp import generate_unity_csharp
        generate_unity_csharp(sos, output_dir)


def _generate_python(sos: SoSDefinition, output_dir: Path) -> None:
    """Generate Python runtime code from a CADL SoS definition.

    Creates a Python package in output_dir with:
      - actors.py          Actor base classes
      - contracts.py       Contract monitor classes
      - protocols.py       Protocol state machine classes
      - transitions.py     Regime controller
      - metrics.py         Metrics collector
      - runtime.py         Top-level orchestrator
      - __init__.py        Package exports
    """
    from .actor_gen import generate_actors_module
    from .expr_compiler import declared_actors
    from .contract_gen import generate_contracts_module
    from .metric_gen import generate_metrics_module
    from .protocol_gen import generate_protocols_module
    from .runtime_gen import generate_runtime_module
    from .transition_gen import generate_transitions_module

    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate each module
    with declared_actors(a.id.name for a in sos.actors):
        if sos.actors:
            _write(output_dir / "actors.py", generate_actors_module(sos.actors))

        if sos.contracts:
            _write(output_dir / "contracts.py", generate_contracts_module(sos.contracts))

        if sos.protocols:
            _write(output_dir / "protocols.py", generate_protocols_module(sos.protocols))

        _write(output_dir / "transitions.py", generate_transitions_module(sos.transitions))
        _write(output_dir / "metrics.py", generate_metrics_module(sos.metrics))
        _write(output_dir / "runtime.py", generate_runtime_module(sos))

    # Generate __init__.py
    _write(output_dir / "__init__.py", _generate_init(sos))


def _write(path: Path, content: str) -> None:
    """Write content to a file."""
    path.write_text(content, encoding="utf-8")


def _generate_init(sos: SoSDefinition) -> str:
    """Generate the __init__.py that exports all generated classes."""
    from .emitter import sanitize_id, snake_case

    lines = []
    lines.append(f'"""Generated CADL runtime package for {sos.name}."""')
    lines.append("")

    # Re-export key classes
    exports = []

    if sos.actors:
        actor_classes = [sanitize_id(a.id.name) + "Actor" for a in sos.actors]
        lines.append(f"from .actors import {', '.join(actor_classes)}")
        exports.extend(actor_classes)

    if sos.contracts:
        monitor_classes = [sanitize_id(c.id) + "Monitor" for c in sos.contracts]
        lines.append(f"from .contracts import {', '.join(monitor_classes)}")
        exports.extend(monitor_classes)

    if sos.protocols:
        proto_classes = [sanitize_id(p.id) + "Protocol" for p in sos.protocols]
        lines.append(f"from .protocols import {', '.join(proto_classes)}")
        exports.extend(proto_classes)

    if sos.transitions:
        lines.append("from .transitions import RegimeController")
        exports.append("RegimeController")

    if sos.metrics:
        lines.append("from .metrics import MetricsCollector")
        exports.append("MetricsCollector")

    clean_name = sos.name.replace(" ", "")
    if "_" in sos.name:
        clean_name = sanitize_id(sos.name)
    runtime_class = clean_name + "Runtime"
    lines.append(f"from .runtime import {runtime_class}")
    exports.append(runtime_class)

    lines.append("")
    lines.append(f"__all__ = {exports!r}")
    lines.append("")

    return "\n".join(lines)
