"""CADL Actor Code Generator — generates actor classes from ActorDef nodes."""

from __future__ import annotations

from typing import List

from ..ast_nodes import ActorDef, RangeExpr
from .emitter import indent, sanitize_id, snake_case


def generate_actor_class(actor: ActorDef) -> str:
    """Generate a Python class for an ActorDef."""
    base_name = actor.id.name
    class_name = sanitize_id(base_name) + "Actor"
    is_parameterized = actor.id.index is not None

    lines = []

    # Class header
    lines.append(f"class {class_name}(ActorBase):")
    lines.append(f'    """Actor: {base_name} (role={actor.role}, autonomy={actor.autonomy.value})."""')
    lines.append("")
    lines.append(f'    ROLE = "{actor.role}"')
    lines.append(f"    AUTONOMY = AutonomyLevel.{actor.autonomy.value.upper()}")
    lines.append("")

    # Constructor
    if is_parameterized:
        lines.append("    def __init__(self, index: int) -> None:")
        lines.append(f'        super().__init__(actor_id=f"{base_name}[{{index}}]", role=self.ROLE)')
        lines.append("        self.index = index")
    else:
        lines.append("    def __init__(self) -> None:")
        lines.append(f'        super().__init__(actor_id="{base_name}", role=self.ROLE)')

    lines.append("        self.state: dict = {}")
    lines.append("")

    # Capability method stubs
    if actor.capabilities:
        for cap in actor.capabilities:
            method_name = snake_case(cap)
            lines.append(f"    def {method_name}(self, *args, **kwargs):")
            lines.append(f'        """Capability: {cap}. Implement in subclass."""')
            lines.append(f'        raise NotImplementedError("{method_name}")')
            lines.append("")

    return "\n".join(lines)


def generate_actors_module(actors: List[ActorDef]) -> str:
    """Generate the complete actors.py module content."""
    imports = [
        "from cadl.codegen.runtime_support import ActorBase, AutonomyLevel",
    ]

    classes = []
    for actor in actors:
        classes.append(generate_actor_class(actor))

    body = "\n\n".join(classes)

    return _assemble_module(
        header="Generated actor classes for CADL SoS definition.",
        imports=imports,
        body=body,
    )


def _assemble_module(header: str, imports: List[str], body: str) -> str:
    """Assemble a Python module from parts."""
    parts = [f'"""{header}"""', "", "from __future__ import annotations", ""]
    parts.extend(imports)
    parts.append("")
    parts.append("")
    parts.append(body)
    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"
    return content
