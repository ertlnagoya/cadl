"""CADL Protocol Code Generator — generates protocol state machine classes."""

from __future__ import annotations

from typing import List

from ..ast_nodes import (
    BarrierStep,
    ComputeStep,
    ConditionalStep,
    MessageStep,
    ParallelStep,
    ProtocolDef,
    Step,
)
from .emitter import sanitize_id, snake_case


def generate_protocol_class(protocol: ProtocolDef) -> str:
    """Generate a protocol executor class from a ProtocolDef."""
    class_name = sanitize_id(protocol.id) + "Protocol"

    lines = []
    lines.append(f"class {class_name}(ProtocolExecutorBase):")
    lines.append(f'    """Protocol: {protocol.id}."""')
    lines.append("")
    lines.append(f'    PROTOCOL_ID = "{protocol.id}"')
    lines.append(f'    TRIGGER = "{protocol.trigger}"')
    lines.append("")

    # Constructor with timing and fallback
    lines.append("    def __init__(self) -> None:")
    lines.append("        super().__init__()")
    if protocol.timing and protocol.timing.entries:
        for key, val in protocol.timing.entries.items():
            lines.append(f"        self.timing['{key}'] = '{val}'")
    if protocol.fallback and protocol.fallback.entries:
        for key, val in protocol.fallback.entries.items():
            lines.append(f"        self.fallback['{key}'] = '{val}'")
    lines.append("")

    # Generate _run_steps method
    lines.append("    async def _run_steps(self, ctx, trigger_event=None) -> None:")
    if protocol.precondition:
        lines.append(f"        # Precondition: {protocol.precondition}")

    step_code = _generate_steps(protocol.steps, indent_level=2)
    if step_code.strip():
        lines.append(step_code)
    else:
        lines.append("        pass")

    if protocol.postcondition:
        lines.append(f"        # Postcondition: {protocol.postcondition}")
    lines.append("")

    return "\n".join(lines)


def _generate_steps(steps: List[Step], indent_level: int = 2) -> str:
    """Generate Python code for a list of protocol steps."""
    prefix = "    " * indent_level
    lines = []

    for i, step in enumerate(steps):
        if isinstance(step, MessageStep):
            sender = _actor_ref_str(step.sender)
            receiver = _actor_ref_str(step.receiver)
            msg = _message_str(step.message)
            lines.append(f"{prefix}# Step {i}: {sender} -> {receiver} : {msg}")
            lines.append(f"{prefix}ctx.send_message(Message(")
            lines.append(f"{prefix}    sender='{sender}',")
            lines.append(f"{prefix}    receiver='{receiver}',")
            lines.append(f"{prefix}    content='{msg}',")
            lines.append(f"{prefix}))")
            lines.append(f"{prefix}self.current_step = {i}")
            lines.append(f"{prefix}self._check_max_total()")
            lines.append("")

        elif isinstance(step, ComputeStep):
            actor = _actor_ref_str(step.actor)
            comp = _message_str(step.computation)
            lines.append(f"{prefix}# Step {i}: {actor} computes {comp}")
            lines.append(f"{prefix}ctx.record_log('{actor}: {comp}')")
            lines.append(f"{prefix}self.current_step = {i}")
            lines.append(f"{prefix}self._check_max_total()")
            lines.append("")

        elif isinstance(step, ConditionalStep):
            cond_str = _expr_label(step.condition)
            lines.append(f"{prefix}# Step {i}: conditional")
            py_cond = _compile_condition(step.condition)
            lines.append(f"{prefix}if {py_cond}:")
            then_code = _generate_steps(step.then_steps, indent_level + 1)
            if then_code.strip():
                lines.append(then_code)
            else:
                lines.append(f"{prefix}    pass")
            if step.else_steps:
                lines.append(f"{prefix}else:")
                else_code = _generate_steps(step.else_steps, indent_level + 1)
                if else_code.strip():
                    lines.append(else_code)
                else:
                    lines.append(f"{prefix}    pass")
            lines.append("")

        elif isinstance(step, ParallelStep):
            lines.append(f"{prefix}# Step {i}: parallel execution")
            lines.append(f"{prefix}await asyncio.gather(")
            for j, sub_step in enumerate(step.steps):
                sub_code = _step_as_coroutine(sub_step, indent_level + 1)
                lines.append(sub_code + ",")
            lines.append(f"{prefix})")
            lines.append("")

        elif isinstance(step, BarrierStep):
            cond_str = _expr_label(step.condition)
            lines.append(f"{prefix}# Step {i}: barrier — {cond_str}")
            lines.append(f"{prefix}ctx.record_log('Barrier: {cond_str}')")
            lines.append("")

    return "\n".join(lines)


def _step_as_coroutine(step: Step, indent_level: int) -> str:
    """Convert a step to an inline coroutine for asyncio.gather."""
    prefix = "    " * indent_level
    if isinstance(step, MessageStep):
        sender = _actor_ref_str(step.sender)
        receiver = _actor_ref_str(step.receiver)
        msg = _message_str(step.message)
        return (
            f"{prefix}_as_coro(lambda: ctx.send_message("
            f"Message(sender='{sender}', receiver='{receiver}', content='{msg}')))"
        )
    if isinstance(step, ComputeStep):
        actor = _actor_ref_str(step.actor)
        comp = _message_str(step.computation)
        return f"{prefix}_as_coro(lambda: ctx.record_log('{actor}: {comp}'))"
    return f"{prefix}asyncio.sleep(0)"


def _compile_condition(expr) -> str:
    """Compile a condition expression to Python source."""
    from .expr_compiler import CompilerContext, expr_to_python
    try:
        ctx = CompilerContext()
        return expr_to_python(expr, ctx)
    except Exception:
        label = _expr_label(expr)
        return f"True  # could not compile: {label}"


def _actor_ref_str(ref) -> str:
    """Convert an ActorRef to a readable string."""
    from ..ast_nodes import ActorRef
    if isinstance(ref, ActorRef):
        if ref.index == "*":
            return f"{ref.name}[*]"
        if ref.index is not None:
            return f"{ref.name}[{ref.index}]"
        return ref.name
    return str(ref)


def _message_str(expr) -> str:
    """Convert a message expression to a readable string."""
    from ..ast_nodes import StringLiteral, Identifier, ActorRef
    if isinstance(expr, StringLiteral):
        return expr.value
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, ActorRef):
        return expr.name
    return str(type(expr).__name__)


def _expr_label(expr) -> str:
    """Get a label for an expression."""
    from ..ast_nodes import StringLiteral, Identifier
    if isinstance(expr, StringLiteral):
        return expr.value
    if isinstance(expr, Identifier):
        return expr.name
    return type(expr).__name__


def generate_protocols_module(protocols: List[ProtocolDef]) -> str:
    """Generate the complete protocols.py module content."""
    imports = [
        "import asyncio",
        "",
        "from cadl.codegen.runtime_support import (",
        "    Message,",
        "    ProtocolExecutorBase,",
        "    ProtocolState,",
        "    RuntimeContext,",
        ")",
    ]

    classes = []
    for protocol in protocols:
        classes.append(generate_protocol_class(protocol))

    body = "\n\n".join(classes)

    parts = ['"""Generated protocol executors for CADL SoS definition."""', ""]
    parts.append("from __future__ import annotations")
    parts.append("")
    parts.extend(imports)
    parts.append("")
    parts.append("")
    parts.append("async def _as_coro(fn):")
    parts.append('    """Wrap a synchronous callable as a coroutine for asyncio.gather."""')
    parts.append("    return fn()")
    parts.append("")
    parts.append("")
    parts.append(body)

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"
    return content
