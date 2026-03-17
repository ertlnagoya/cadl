"""CADL AI Integration — natural language to CADL generation via Claude API.

Public API:
    generate_cadl(description, ...) -> NlToCadlResult
"""

from __future__ import annotations

from .llm_client import ClaudeClient, ClientConfig
from .nl_to_cadl import NlToCadlResult, extract_cadl, generate_cadl

__all__ = [
    "generate_cadl",
    "extract_cadl",
    "NlToCadlResult",
    "ClaudeClient",
    "ClientConfig",
]
