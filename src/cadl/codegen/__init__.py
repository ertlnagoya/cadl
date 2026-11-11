"""CADL Code Generator — generates runtime code from CADL definitions.

Public API:
    generate(sos, output_dir, target="python") -> None

Supported targets: python, solidity, opa, unity-csharp, agent-rego,
agent-cedar
"""

from __future__ import annotations

from pathlib import Path

from ..ast_nodes import SoSDefinition

SUPPORTED_TARGETS = {"python", "solidity", "opa", "unity-csharp",
                     "agent-rego", "agent-cedar"}
# Boundary policies of the agent governance extension (Appendix G, G.10)
AGENT_TARGETS = {"agent-rego", "agent-cedar"}


def generate(sos: SoSDefinition, output_dir: Path, target: str = "python") -> None:
    """Generate runtime code from a CADL SoS definition.

    Args:
        sos: Parsed SoS definition.
        output_dir: Directory to write generated files.
        target: Code generation target — "python", "solidity", "opa",
            "unity-csharp" (Unity C# for the SoS-DSL extension,
            Appendix E), or "agent-rego" / "agent-cedar" (boundary policy
            of the agent governance extension, Appendix G; the file must
            declare `agents: 0.1`).
    """
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"Unsupported target: {target!r}. Choose from: {sorted(SUPPORTED_TARGETS)}")

    if target in AGENT_TARGETS:
        from ..agents import agents_active
        if not agents_active(sos):
            raise ValueError(
                f"target {target!r} generates the boundary policy of the agent "
                f"governance extension, but the file does not declare it: add "
                f"'norms: 0.1' and 'agents: 0.1' under extensions: (Appendix G)"
            )

    if target != "unity-csharp":
        from .safety import check_codegen_input
        check_codegen_input(sos)

    # With norms 0.1 declared, assume / guarantee items that contain a
    # temporal operator are left out, with a comment (Appendix F, F.7).
    # The norms monitor monitors them, and the norms: norms_monitor.py of
    # the Python target, and for the OPA and Solidity targets one monitor
    # per contract (`norms_monitors`).
    from ..norms import norms_declared, temporal_items_omitted
    with temporal_items_omitted(norms_declared(sos)):
        _generate_target(sos, output_dir, target)


def omitted_temporal_items(sos: SoSDefinition) -> list[tuple[str, str, str]]:
    """The (contract id, key, text) of each assume / guarantee item that
    `generate` leaves out because it has a temporal operator (norms 0.1)."""
    from ..norms import norms_declared, split_temporal_items
    if not norms_declared(sos):
        return []
    return [
        (contract.id, key, text)
        for contract in sos.contracts
        for key, text in split_temporal_items(contract)[1]
    ]


def norms_monitors(sos: SoSDefinition, target: str) -> list[dict]:
    """The norms monitors that the OPA or Solidity target generates for a
    file that declares norms 0.1 (Appendix F, F.5; Appendix D), one entry
    per contract: ``{"contract", "file", "text", "reasons", "left_out"}``.
    ``text`` is None when the contract cannot be monitored (``reasons``
    says why); ``left_out`` lists the (key, position, text, reason) of the
    items the monitor leaves out. An empty list for other files and
    targets."""
    from ..norms import norms_declared
    from .norms_ir import NormsTargetUnsupported, build_ir, unsupported_items
    if target not in ("opa", "solidity") or not norms_declared(sos):
        return []
    ir_of = build_ir
    if target == "opa":
        from .opa.norms_monitor_rego import generate_norms_monitor_rego as gen
        from .opa.norms_monitor_rego import monitor_filename
    else:
        from .solidity.norms_monitor_sol import generate_norms_monitor_sol as gen
        from .solidity.norms_monitor_sol import monitor_name, solidity_ir as ir_of

        def monitor_filename(contract_id: str) -> str:
            return f"{monitor_name(contract_id)}.sol"
    out = []
    for contract in sos.contracts:
        entry = {"contract": contract.id, "file": monitor_filename(contract.id),
                 "text": None, "reasons": [], "left_out": []}
        try:
            entry["left_out"] = unsupported_items(ir_of(sos, contract))
            entry["text"] = gen(sos, contract)
        except NormsTargetUnsupported as e:
            entry["reasons"] = e.reasons
        out.append(entry)
    return out


def _generate_target(sos: SoSDefinition, output_dir: Path, target: str) -> None:
    if target == "python":
        _generate_python(sos, output_dir)
    elif target == "solidity":
        from .solidity import generate_solidity
        monitors = norms_monitors(sos, target)
        generate_solidity(sos, output_dir)
        _write_monitors(output_dir, monitors)
    elif target == "opa":
        from .opa import generate_rego
        monitors = norms_monitors(sos, target)
        generate_rego(sos, output_dir)
        _write_monitors(output_dir, monitors)
    elif target == "unity-csharp":
        from .unity_csharp import generate_unity_csharp
        generate_unity_csharp(sos, output_dir)
    elif target == "agent-rego":
        from .agent_policy import generate_agent_rego
        generate_agent_rego(sos, output_dir)
    elif target == "agent-cedar":
        from .agent_policy import generate_agent_cedar
        generate_agent_cedar(sos, output_dir)


def _write_monitors(output_dir: Path, monitors: list[dict]) -> None:
    for m in monitors:
        if m["text"] is not None:
            _write(output_dir / m["file"], m["text"])


def _generate_python(sos: SoSDefinition, output_dir: Path) -> None:
    """Generate Python runtime code from a CADL SoS definition.

    Creates a Python package in output_dir with:
      - actors.py          Actor base classes
      - contracts.py       Contract monitor classes
      - protocols.py       Protocol state machine classes
      - transitions.py     Regime controller
      - metrics.py         Metrics collector
      - runtime.py         Top-level orchestrator
      - norms_monitor.py   Monitor of the norms extension (Appendix F), for
                           a file that declares norms 0.1; it does not
                           import cadl
      - __init__.py        Package exports
    """
    from .actor_gen import generate_actors_module
    from .expr_compiler import declared_actors
    from .contract_gen import generate_contracts_module
    from .metric_gen import generate_metrics_module
    from .norms_monitor_gen import (
        MODULE_NAME, generate_norms_monitor_module, has_norms_monitor,
    )
    from .protocol_gen import generate_protocols_module
    from .runtime_gen import generate_runtime_module
    from .transition_gen import generate_transitions_module

    # Before anything is written: it fails for a contract that cannot be read.
    monitor = generate_norms_monitor_module(sos) if has_norms_monitor(sos) else None

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

    if monitor is not None:
        _write(output_dir / f"{MODULE_NAME}.py", monitor)

    # Generate __init__.py
    _write(output_dir / "__init__.py", _generate_init(sos, norms_monitor=monitor is not None))


def _write(path: Path, content: str) -> None:
    """Write content to a file."""
    path.write_text(content, encoding="utf-8")


def _generate_init(sos: SoSDefinition, norms_monitor: bool = False) -> str:
    """Generate the __init__.py that exports all generated classes.

    With `norms_monitor`, the package also exports `NormsMonitor`, and it
    can be imported where cadl is not installed: the norms monitor needs
    only the standard library, while the other modules import
    `cadl.codegen.runtime_support`. Their names then raise the import
    error when they are used."""
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
    if norms_monitor:
        head, imports = lines[:2], [l for l in lines[2:] if l]
        lines = head + [
            "# The modules below need cadl (cadl.codegen.runtime_support). The norms",
            "# monitor (norms_monitor.py) does not: where cadl is not installed the",
            "# package can still be imported for it, and the other names raise the",
            "# import error when they are used.",
            "_import_error = None",
            "try:",
            *("    " + l for l in imports),
            "except ModuleNotFoundError as _e:",
            "    if (_e.name or \"\").split(\".\")[0] != \"cadl\":",
            "        raise",
            "    _import_error = _e",
            "",
        ]
        exports.append("NormsMonitor")
    lines.append(f"__all__ = {exports!r}")
    lines.append("")
    if norms_monitor:
        lines += [
            "",
            "def __getattr__(name):",
            "    if name == \"NormsMonitor\":",
            "        from .norms_monitor import NormsMonitor",
            "        return NormsMonitor",
            "    if _import_error is not None and name in __all__:",
            "        raise _import_error",
            "    raise AttributeError(f\"module {__name__!r} has no attribute {name!r}\")",
            "",
        ]

    return "\n".join(lines)
