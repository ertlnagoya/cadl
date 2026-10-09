"""Input checks applied before code generation.

Generated source embeds names and text taken from the .cadl file: ids become
class and file names, and other strings end up in string literals and
comments. The Python, Solidity and OPA emitters write them verbatim, so a
crafted value could add code to the generated module or write a file outside
the output directory. This module rejects such values up front.

The Unity C# emitter sanitises names and escapes string literals itself and
is not covered here.
"""

from __future__ import annotations

import dataclasses
import re
from enum import Enum
from typing import Any, List

from ..ast_nodes import LifecycleSpec, MonitorDef, SoSDefinition


class UnsafeCodegenInput(ValueError):
    """A value in the definition cannot be emitted safely as source code."""


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SOS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]*\Z")

# (class name, field name) pairs whose values become names in generated code.
_NAME_FIELDS = {
    ("ActorRef", "name"),
    ("ContractDef", "id"),
    ("ProtocolDef", "id"),
    ("MetricDef", "id"),
    ("VerificationSpec", "id"),
    ("TransitionDef", "from_regime"),
    ("TransitionDef", "to_regime"),
    ("MemberAccess", "member"),
    ("QuantifiedExpr", "variable"),
    ("Comprehension", "variable"),
}

# FunctionCall.name and Identifier.name are not listed: a protocol step keeps
# free text there
# (e.g. "broadcast_claim_request() to all active ROBOT[*]"), which the
# emitters write into a string literal, so the text check below applies.

# Characters and sequences that end a string literal or a comment in at
# least one target language.
_FORBIDDEN = ('"', "'", "\\", "`", "*/")

# Blocks that only the Unity C# emitter reads.
_SKIPPED_TYPES = (LifecycleSpec, MonitorDef)


def check_codegen_input(sos: SoSDefinition) -> None:
    """Raise :class:`UnsafeCodegenInput` listing every unsafe value in *sos*."""
    problems: List[str] = []
    if not _SOS_NAME.match(sos.name or ""):
        problems.append(
            f"sos.name {sos.name!r}: use letters, digits, '_' and spaces, "
            f"starting with a letter or '_'"
        )
    _walk(sos, "sos", problems)
    if problems:
        shown = "\n  ".join(problems[:10])
        more = f"\n  ... and {len(problems) - 10} more" if len(problems) > 10 else ""
        raise UnsafeCodegenInput(
            "the definition contains values that cannot be emitted as "
            f"source code:\n  {shown}{more}"
        )


def _walk(value: Any, path: str, problems: List[str]) -> None:
    if isinstance(value, _SKIPPED_TYPES) or isinstance(value, Enum):
        return
    if isinstance(value, str):
        _check_text(value, path, problems)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        cls = type(value).__name__
        for f in dataclasses.fields(value):
            if f.name == "loc":
                continue
            item = getattr(value, f.name)
            child = f"{path}.{f.name}"
            if isinstance(item, str) and (cls, f.name) in _NAME_FIELDS:
                if cls == "SoSDefinition":
                    continue
                if not _IDENTIFIER.match(item):
                    problems.append(
                        f"{child} {item!r}: must be an identifier "
                        f"(letters, digits, '_')"
                    )
                continue
            _walk(item, child, problems)
    elif isinstance(value, dict):
        for key, item in value.items():
            _walk(key, f"{path}[key]", problems)
            _walk(item, f"{path}[{key!r}]", problems)
    elif isinstance(value, (list, tuple, set)):
        for i, item in enumerate(value):
            _walk(item, f"{path}[{i}]", problems)


def _check_text(text: str, path: str, problems: List[str]) -> None:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in "  " for ch in text):
        problems.append(f"{path} {text!r}: contains a control character")
        return
    for token in _FORBIDDEN:
        if token in text:
            problems.append(f"{path} {text!r}: contains {token!r}")
            return
