"""CADL Simulator IR and Config Generation.

Public API:
    lower_to_ir(sos) -> SimIR
    validate_ir(ir) -> list[str]
    generate_config(ir, target) -> str
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .ir import SimIR
from .lower import lower_to_ir
from .validate import validate_ir

if TYPE_CHECKING:
    from ..ast_nodes import SoSDefinition

SUPPORTED_TARGETS = {"python", "unity", "go"}


def generate_config(ir: SimIR, target: str) -> str:
    """Generate simulator config string for the given target.

    Args:
        ir: A validated SimIR instance.
        target: One of "python", "unity", "go".

    Returns:
        Config string (YAML for python, JSON for unity/go).
    """
    if target not in SUPPORTED_TARGETS:
        raise ValueError(
            f"Unknown target '{target}'. Supported: {sorted(SUPPORTED_TARGETS)}"
        )
    if target == "python":
        from .gen_python import generate_python_config
        return generate_python_config(ir)
    elif target == "unity":
        from .gen_unity import generate_unity_config
        return generate_unity_config(ir)
    else:
        from .gen_go import generate_go_config
        return generate_go_config(ir)


__all__ = [
    "SimIR",
    "lower_to_ir",
    "validate_ir",
    "generate_config",
    "SUPPORTED_TARGETS",
]
