"""CADL Code Emitter — utilities for generating Python source code."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import List, Optional


def sanitize_id(name: str) -> str:
    """Convert a CADL identifier to a Python class name.

    Examples:
        DELIVERY_SLA -> DeliverySla
        FAILURE_REPLAN -> FailureReplan
        ROBOT -> Robot
    """
    parts = name.split("_")
    return "".join(_capitalize_part(part) for part in parts)


def _capitalize_part(part: str) -> str:
    """Capitalize one ``_``-separated part, keeping existing camel case.

    ``SLA`` -> ``Sla`` and ``robot`` -> ``Robot``, but a mixed-case part such
    as ``RobotDeliverySystem`` is already a class name and is kept as is.
    """
    if part.isupper() or part.islower():
        return part.capitalize()
    return part[:1].upper() + part[1:]


def snake_case(name: str) -> str:
    """Convert a CADL identifier to snake_case.

    Examples:
        DELIVERY_SLA -> delivery_sla
        FailureReplan -> failure_replan
    """
    # Handle already-uppercase names like DELIVERY_SLA
    if name.isupper() or "_" in name:
        return name.lower()
    # Handle CamelCase
    s = re.sub(r"([A-Z])", r"_\1", name).lstrip("_")
    return s.lower()


def indent(code: str, level: int = 1, width: int = 4) -> str:
    """Indent a block of code by the given level."""
    prefix = " " * (level * width)
    return textwrap.indent(code, prefix)


def make_class(
    name: str,
    bases: List[str],
    body: str,
    docstring: Optional[str] = None,
) -> str:
    """Generate a class definition."""
    base_str = ", ".join(bases) if bases else ""
    header = f"class {name}({base_str}):" if base_str else f"class {name}:"

    parts = [header]
    if docstring:
        parts.append(indent(f'"""{docstring}"""'))
    if body.strip():
        parts.append(indent(body))
    else:
        parts.append(indent("pass"))

    return "\n".join(parts)


def make_method(
    name: str,
    params: str = "self",
    body: str = "pass",
    docstring: Optional[str] = None,
    decorators: Optional[List[str]] = None,
    is_async: bool = False,
) -> str:
    """Generate a method definition."""
    parts = []
    if decorators:
        for dec in decorators:
            parts.append(f"@{dec}")

    prefix = "async def" if is_async else "def"
    parts.append(f"{prefix} {name}({params}):")

    if docstring:
        parts.append(indent(f'"""{docstring}"""'))
    if body.strip():
        parts.append(indent(body))
    else:
        parts.append(indent("pass"))

    return "\n".join(parts)


def make_import(module: str, names: List[str]) -> str:
    """Generate an import statement."""
    if not names:
        return f"import {module}"
    return f"from {module} import {', '.join(names)}"


def write_module(
    path: Path,
    imports: List[str],
    body: str,
    header_comment: Optional[str] = None,
) -> None:
    """Write a complete Python module to a file."""
    parts = []

    if header_comment:
        parts.append(f'"""{header_comment}"""')
        parts.append("")

    parts.append("from __future__ import annotations")
    parts.append("")

    if imports:
        parts.extend(imports)
        parts.append("")
        parts.append("")

    if body.strip():
        parts.append(body)
    else:
        parts.append("")

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
