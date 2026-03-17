"""CADL Runtime Orchestrator Generator — generates the top-level wiring module."""

from __future__ import annotations

from typing import List

from ..ast_nodes import ActorDef, ContractDef, MetricDef, ProtocolDef, RangeExpr, SoSDefinition
from .emitter import sanitize_id, snake_case


def generate_runtime_module(sos: SoSDefinition) -> str:
    """Generate the runtime.py module that wires everything together."""
    # Preserve CamelCase if present, otherwise sanitize via underscore splitting
    clean_name = sos.name.replace(" ", "")
    if "_" in sos.name:
        clean_name = sanitize_id(sos.name)
    sos_class_name = clean_name + "Runtime"

    lines = []
    lines.append(f'"""Generated runtime orchestrator for {sos.name}."""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from cadl.codegen.runtime_support import RuntimeContext")
    lines.append("")

    # Import generated modules
    actor_classes = []
    for actor in sos.actors:
        cls = sanitize_id(actor.id.name) + "Actor"
        actor_classes.append(cls)
    if actor_classes:
        lines.append(f"from .actors import {', '.join(actor_classes)}")

    contract_classes = []
    for contract in sos.contracts:
        cls = sanitize_id(contract.id) + "Monitor"
        contract_classes.append(cls)
    if contract_classes:
        lines.append(f"from .contracts import {', '.join(contract_classes)}")

    protocol_classes = []
    for protocol in sos.protocols:
        cls = sanitize_id(protocol.id) + "Protocol"
        protocol_classes.append(cls)
    if protocol_classes:
        lines.append(f"from .protocols import {', '.join(protocol_classes)}")

    if sos.transitions:
        lines.append("from .transitions import RegimeController")

    if sos.metrics:
        lines.append("from .metrics import MetricsCollector")

    lines.append("")
    lines.append("")

    # Class definition
    lines.append(f"class {sos_class_name}:")
    lines.append(f'    """Runtime orchestrator for SoS: {sos.name}."""')
    lines.append("")

    # Constructor
    params = _collect_params(sos.actors)
    param_str = ", ".join(f"{p}: int = 1" for p in params)
    if param_str:
        lines.append(f"    def __init__(self, {param_str}) -> None:")
    else:
        lines.append("    def __init__(self) -> None:")

    lines.append("        self.ctx = RuntimeContext()")
    lines.append("")

    # Instantiate actors
    lines.append("        # Actors")
    for actor in sos.actors:
        cls = sanitize_id(actor.id.name) + "Actor"
        var = snake_case(actor.id.name)
        if _is_parameterized(actor):
            param = _get_param_name(actor)
            lines.append(f"        self.{var}s = [{cls}(i) for i in range(1, {param} + 1)]")
            lines.append(f"        for a in self.{var}s:")
            lines.append(f"            self.ctx.register_actor(a)")
        else:
            lines.append(f"        self.{var} = {cls}()")
            lines.append(f"        self.ctx.register_actor(self.{var})")
    lines.append("")

    # Instantiate contract monitors
    if sos.contracts:
        lines.append("        # Contract monitors")
        for contract in sos.contracts:
            cls = sanitize_id(contract.id) + "Monitor"
            var = snake_case(contract.id) + "_monitor"
            lines.append(f"        self.{var} = {cls}()")
        lines.append("")

    # Instantiate protocol executors
    if sos.protocols:
        lines.append("        # Protocol executors")
        for protocol in sos.protocols:
            cls = sanitize_id(protocol.id) + "Protocol"
            var = snake_case(protocol.id)
            lines.append(f"        self.{var} = {cls}()")
        lines.append("")

    # Instantiate regime controller
    if sos.transitions:
        lines.append("        # Regime controller")
        lines.append("        self.regime_controller = RegimeController()")
        lines.append("")

    # Instantiate metrics
    if sos.metrics:
        lines.append("        # Metrics collector")
        lines.append("        self.metrics = MetricsCollector()")
        lines.append("")

    # run_monitor_cycle method
    lines.append("    def run_monitor_cycle(self) -> list:")
    lines.append('        """Run all contract monitors and return violations."""')
    lines.append("        all_violations = []")
    for contract in sos.contracts:
        var = snake_case(contract.id) + "_monitor"
        lines.append(f"        all_violations.extend(self.{var}.run_checks(self.ctx))")
    lines.append("        for v in all_violations:")
    lines.append("            self.ctx.record_violation(v)")
    lines.append("        return all_violations")
    lines.append("")

    # get_protocols method
    lines.append("    def get_protocols(self) -> list:")
    lines.append('        """Return all protocol executors."""')
    proto_vars = [snake_case(p.id) for p in sos.protocols]
    if proto_vars:
        lines.append(f"        return [{', '.join('self.' + v for v in proto_vars)}]")
    else:
        lines.append("        return []")
    lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def _is_parameterized(actor: ActorDef) -> bool:
    return actor.id.index is not None


def _get_param_name(actor: ActorDef) -> str:
    """Get the constructor parameter name for a parameterized actor."""
    idx = actor.id.index
    if isinstance(idx, RangeExpr):
        end = idx.end
        if isinstance(end, str):
            return f"num_{snake_case(actor.id.name)}s"
    return f"num_{snake_case(actor.id.name)}s"


def _collect_params(actors: List[ActorDef]) -> List[str]:
    """Collect constructor parameters for parameterized actors."""
    params = []
    for actor in actors:
        if _is_parameterized(actor):
            params.append(f"num_{snake_case(actor.id.name)}s")
    return params
