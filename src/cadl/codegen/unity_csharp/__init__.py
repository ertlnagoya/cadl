"""Unity C# code generator for the SoS-DSL extension (Appendix E).

Emits a small, self-contained set of C# files under a target directory
that, when dropped into a Unity project's ``Assets/Scripts/SoSDsl/``,
implement the per-instance contract lifecycle and declarative monitors
described by the SoS-DSL extension.

The semantics of the generated runtime mirror the Python reference
runtime in ``raspimouse-swarm-simulator/cadl/runtime/engine.py``:

  - Lifecycle state machine driven by ``Tick`` (sim time) and
    ``PostEvent`` (port messages).
  - Deadline timers armed when entering a state that has outgoing
    transitions with ``deadline_ms``; expiry triggers a violation and
    a state lift to ``on_violation_transition``.
  - Periodic and event-driven monitors evaluate a tiny predicate
    expression against a per-instance world snapshot.
  - Violations and lifecycle transitions are emitted on ``EventBus``
    so the host (a MonoBehaviour) can log them or paint visuals.

This module is intentionally **template-string** based — no jinja, no
external dependencies — to keep the generator easy to read and ship.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ...ast_nodes import ContractDef, SoSDefinition
from . import contract_emitter, runtime_template


def generate_unity_csharp(
    sos: SoSDefinition,
    output_dir: Path,
    namespace: str = "CADL.SosDsl",
) -> list[Path]:
    """Generate C# files into ``output_dir``.

    Returns the list of files written. Existing files at the target
    paths are overwritten.
    """
    output_dir = Path(output_dir)
    runtime_dir = output_dir / "Runtime"
    generated_dir = output_dir / "Generated"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    # Runtime support files (constant per generation).
    for filename, source in runtime_template.runtime_files(namespace):
        path = runtime_dir / filename
        path.write_text(source, encoding="utf-8")
        written.append(path)

    # Per-contract generated files.
    for contract in _eligible_contracts(sos):
        for filename, source in contract_emitter.emit_contract(
            contract, namespace=namespace,
        ):
            path = generated_dir / filename
            path.write_text(source, encoding="utf-8")
            written.append(path)

    # Top-level README documenting the Unity import flow.
    readme = output_dir / "README.md"
    readme.write_text(_readme(sos, namespace), encoding="utf-8")
    written.append(readme)

    return written


def _eligible_contracts(sos: SoSDefinition) -> Iterable[ContractDef]:
    """Yield contracts that declare the SoS-DSL extension."""
    for c in sos.contracts:
        if c.lifecycle is not None or c.monitors:
            yield c


def _readme(sos: SoSDefinition, namespace: str) -> str:
    contracts = list(_eligible_contracts(sos))
    contract_lines = "\n".join(
        f"- `{c.id}` — lifecycle states: "
        f"{', '.join((c.lifecycle.states if c.lifecycle else []))}"
        for c in contracts
    )
    return f"""# Generated Unity C# — SoS-DSL Contract Runtime

Source SoS: **{sos.name}**
C# namespace: `{namespace}`

This directory is the output of:

```bash
cadl codegen --target unity-csharp <input.cadl> --output <this-dir>
```

## Layout

```
Runtime/        # generator-emitted runtime support (regenerated each run)
  ContractEvent.cs
  ContractRuntime.cs
  EventBus.cs
  PredicateEvaluator.cs
  Severity.cs

Generated/      # one set of files per contract that declares lifecycle:
{contract_lines}
```

## Usage in Unity

1. Copy this entire directory into `Assets/Scripts/SoSDsl/` of your
   Unity project (preserve the subdirectory layout).
2. Add a `MonoBehaviour` host in your scene that owns one
   `ContractRuntime` instance and forwards relevant Unity events
   (port messages, sim ticks, world snapshots) to it.
3. Subscribe to `runtime.OnLifecycle` / `runtime.OnViolation` to
   render visuals or write logs.

The generator's semantics match the Python runtime in
`raspimouse-swarm-simulator/cadl/runtime/engine.py` — a generated
contract that passes the Python tests behaves the same in Unity.

Reward and sanction *execution* is intentionally out of scope (v0.1):
violation events are recorded but not actuated.
"""


__all__ = ["generate_unity_csharp"]
