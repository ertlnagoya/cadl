"""CADL NL-to-CADL Pipeline — generates CADL from natural language via Claude API."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from ..ast_nodes import SoSDefinition
from ..parser import parse
from ..type_checker import TypeCheckResult, type_check
from .llm_client import ClaudeClient, ClientConfig
from .prompts import build_retry_prompt, build_system_prompt, build_user_prompt


@dataclass
class NlToCadlResult:
    """Result of NL-to-CADL generation."""
    cadl_source: str
    sos: Optional[SoSDefinition] = None
    validation: Optional[TypeCheckResult] = None
    errors: List[str] = field(default_factory=list)
    retried: bool = False


def extract_cadl(text: str) -> str:
    """Extract CADL source from LLM output, stripping markdown fences if present."""
    # Strip markdown code fences
    # Match ```yaml ... ``` or ``` ... ```
    fence_pattern = re.compile(
        r"```(?:yaml|cadl)?\s*\n(.*?)```", re.DOTALL
    )
    match = fence_pattern.search(text)
    if match:
        return match.group(1).strip()

    # If no fences, look for the sos: key and take everything from there
    sos_match = re.search(r"^(sos:.*)", text, re.DOTALL | re.MULTILINE)
    if sos_match:
        return sos_match.group(1).strip()

    # Fallback: return stripped text
    return text.strip()


def generate_cadl(
    description: str,
    *,
    client: Optional[ClaudeClient] = None,
    config: Optional[ClientConfig] = None,
    verify: bool = False,
    max_retries: int = 1,
) -> NlToCadlResult:
    """Generate a CADL definition from a natural language description.

    Args:
        description: Natural language description of the SoS (Japanese or English).
        client: Pre-configured ClaudeClient (created from config if not provided).
        config: Client configuration (used only if client is not provided).
        verify: If True, also run SMT verification and deadlock detection.
        max_retries: Number of retry attempts on validation failure.

    Returns:
        NlToCadlResult with the generated CADL source and validation results.
    """
    if client is None:
        client = ClaudeClient(config)

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(description)

    # Initial generation
    raw_output = client.generate(system_prompt, user_prompt)
    cadl_source = extract_cadl(raw_output)

    result = _validate_cadl(cadl_source, verify=verify)

    # Retry if there are errors
    retries = 0
    while result.errors and retries < max_retries:
        retries += 1
        retry_prompt = build_retry_prompt(cadl_source, result.errors)

        # Use conversation history for better context
        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": raw_output},
            {"role": "user", "content": retry_prompt},
        ]
        raw_output = client.generate_with_history(system_prompt, messages)
        cadl_source = extract_cadl(raw_output)
        result = _validate_cadl(cadl_source, verify=verify)
        result.retried = True

    return result


def _validate_cadl(cadl_source: str, *, verify: bool = False) -> NlToCadlResult:
    """Parse and validate generated CADL source."""
    errors = []
    sos = None
    validation = None

    # Parse
    try:
        sos = parse(cadl_source)
    except Exception as e:
        errors.append(f"Parse error: {e}")
        return NlToCadlResult(
            cadl_source=cadl_source, errors=errors
        )

    # Type check
    try:
        validation = type_check(sos)
        if not validation.ok:
            for err in validation.errors:
                errors.append(f"Type error: {err.message}")
    except Exception as e:
        errors.append(f"Type check error: {e}")

    # Optional verification
    if verify and not errors:
        try:
            from ..verifier import verify as smt_verify
            vresult = smt_verify(sos)
            if not vresult.consistent:
                for issue in vresult.issues:
                    errors.append(f"Verification: {issue}")
        except Exception as e:
            errors.append(f"Verification error: {e}")

        try:
            from ..deadlock import check_deadlocks
            deadlocks = check_deadlocks(sos)
            if deadlocks:
                for dl in deadlocks:
                    errors.append(f"Deadlock: {dl}")
        except Exception as e:
            errors.append(f"Deadlock check error: {e}")

    return NlToCadlResult(
        cadl_source=cadl_source,
        sos=sos,
        validation=validation,
        errors=errors,
    )
