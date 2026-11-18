"""Unity C# code generator for the SoS-DSL extension (Appendix E).

Emits a small, self-contained set of C# files under a target directory
that, when dropped into a Unity project's ``Assets/Scripts/SoSDsl/``,
implement the per-instance contract lifecycle and declarative monitors
described by the SoS-DSL extension.

The semantics of the generated runtime mirror the Python reference
runtime in ``cadl-raspimouse-simulator/cadl/runtime/engine.py``:

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
    versions: list | None = None,
) -> list[Path]:
    """Generate C# files into ``output_dir``.

    Returns the list of files written. Existing files at the target
    paths are overwritten.
    """
    regimes = regime_part(sos)
    runs = _version_part(sos, list(versions or []))
    if runs is not None:
        # versions with regime bindings (K.11, "Versions and regimes", from cadl 0.3.36)
        runs["regimes"] = any(info.regimes is not None for info in runs["chain"].infos)
        regimes = None
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
        bound = regimes is not None and any(contract.id in out for out in regimes["out"].values())
        if runs is not None:
            files = list(_versioned_files(contract, runs, namespace))
        else:
            files = contract_emitter.emit_contract(contract, namespace=namespace,
                                                   regime_bound=bound)
        for filename, source in files:
            path = generated_dir / filename
            path.write_text(source, encoding="utf-8")
            written.append(path)
    if runs is not None:
        path = generated_dir / "CadlVersions.cs"
        path.write_text(contract_emitter.version_body_interface(namespace, runs["regimes"]),
                        encoding="utf-8")
        written.append(path)
        if runs["regimes"]:
            path = generated_dir / "CadlRegimes.cs"
            path.write_text(contract_emitter.regimes_versions_file(
                runs["chain"], [c.id for c in _eligible_contracts(sos)], namespace),
                encoding="utf-8")
            written.append(path)
    if regimes is not None:
        path = generated_dir / "CadlRegimes.cs"
        path.write_text(contract_emitter.regimes_file(regimes, namespace), encoding="utf-8")
        written.append(path)

    # Top-level README documenting the Unity import flow.
    readme = output_dir / "README.md"
    readme.write_text(_readme(sos, namespace, bool(runs and runs["regimes"])),
                      encoding="utf-8")
    written.append(readme)

    return written


def _version_part(sos: SoSDefinition, versions: list) -> dict | None:
    """The versions of a run (Appendix K, K.11) in the order of previous:,
    with the transitional rule of each contract, or None without versions."""
    if not versions:
        return None
    from ...norms_eval import NormsEvalError, VersionChain, version_info
    try:
        chain = VersionChain([version_info(s) for s in [sos, *versions]])
    except NormsEvalError as e:
        raise ValueError(f"target 'unity-csharp': the versions of the run: {e}") from None
    return {"chain": chain}


def _versioned_files(contract, runs: dict, namespace: str):
    chain = runs["chain"]
    names, rules, bodies = chain.names(), [], []
    for k, info in enumerate(chain.infos):
        c = next((c for c in info.sos.contracts if c.id == contract.id), None)
        if c is None or (c.lifecycle is None and not c.monitors):
            raise ValueError(
                f"target 'unity-csharp': version {info.name} has no contract {contract.id} "
                f"with lifecycle: or monitors:; the instance follows it through every version")
        rule, states = info.rule_for(contract.id)
        rules.append({"rule": rule, "states": list(states or [])})
        bodies.append(k)
        yield from contract_emitter.emit_version_body(c, k, namespace, runs["regimes"])
    make = (contract_emitter.versioned_class_regimes if runs["regimes"]
            else contract_emitter.versioned_class)
    yield (f"{contract_emitter._csharp_class_name(contract.id)}Contract.cs",
           make(contract.id, names, rules, bodies, namespace))


def regime_part(sos: SoSDefinition) -> dict | None:
    """The regimes of a file whose regime bindings (`regimes:`, Appendix A,
    A.7.1) take a generated contract out of force in some regime:
    ``{"regimes", "out": {regime: [contract ids]}, "initial"}``; None
    otherwise (the code is then that of the file without the block). The
    values a regime binds (alpha, beta, lambda, environment entries) are
    not part of the generated code."""
    if not sos.transitions:
        return None
    from ...regimes import checked, initial_regime
    bindings = checked(sos)
    if not bindings:
        return None
    ids = [c.id for c in _eligible_contracts(sos)]
    out = {b.name: [c for c in ids if c not in b.active] for b in bindings}
    if not any(out.values()):
        return None
    return {"regimes": [b.name for b in bindings], "out": out, "initial": initial_regime(sos)}


def _eligible_contracts(sos: SoSDefinition) -> Iterable[ContractDef]:
    """Yield contracts that declare the SoS-DSL extension."""
    for c in sos.contracts:
        if c.lifecycle is not None or c.monitors:
            yield c


def _readme(sos: SoSDefinition, namespace: str, versions_regimes: bool = False) -> str:
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
`cadl-raspimouse-simulator/cadl/runtime/engine.py` — a generated
contract that passes the Python tests behaves the same in Unity.

Reward and sanction *execution* is intentionally out of scope (v0.1):
violation events are recorded but not actuated.
""" + (_versions_regimes_readme() if versions_regimes else _regimes_readme(sos))


def _versions_regimes_readme() -> str:
    return """
## Versions and regimes

`Generated/CadlRegimes.cs` holds the regimes of every version of the run and
the contracts each does not have in force (Appendix K, K.11, "Versions and
regimes"). Create one `CadlRegimes` per runtime (`new CadlRegimes(rt,
version)`: the run starts in the initial regime of that version), pass it as
the last argument of the constructors of the versioned contract classes, and
call `Set(regime, tMs)` when the regime controller of the run takes a
transition (before `SetVersion` at the same time). A contract is in force by
the regime bindings of the version in force. An instance starts at the first
time its contract is in force, under the version in force then; while the
contract is not in force it sees no event and no tick and its clock stops; a
version that takes effect meanwhile is applied when the contract is in force
again, before anything else, and the `<version>` Lifecycle event of a switch
it makes has the time at which the version took effect (the `tMs` of
`SetVersion`); a waiting switch with `in_states:` has the time at which it
happens. `SetVersion` and `Set` throw for a regime that is not a regime of
every version the instance has had in force.
""" + _ticks_readme(True)


def _ticks_readme(versions: bool) -> str:
    import textwrap
    applied = (" (after the versions that took effect meanwhile are applied)"
               if versions else "")
    text = ("`Set` does not call `Tick`. The clock of a contract runs up to the time at "
            "which it leaves force; a deadline that has passed on that clock and that no "
            "`Tick` decided before is decided by the first `Tick` after the contract is in "
            f"force again{applied}, as `cadl norms-eval` decides it at the next step at "
            "which the contract is in force. So at a time at which a contract is in force "
            "again, call `Set(regime, tMs)`, then `Tick(tMs)`, then post the events of that "
            "time. A `Tick(tMs)` before a `Set(regime, tMs)` that takes a contract out of "
            "force decides its deadlines at that time, as a trace with a step at that time "
            "at which the contract is still in force does.")
    return "\n" + textwrap.fill(text, 76, break_on_hyphens=False) + "\n"


def _regimes_readme(sos: SoSDefinition) -> str:
    regimes = regime_part(sos)
    if regimes is None:
        return ""
    bound = sorted({c for out in regimes["out"].values() for c in out})
    return f"""
## Regimes

`Generated/CadlRegimes.cs` holds the regimes of the file
({', '.join(regimes['regimes'])}; initial: {regimes['initial']}). Create one
`CadlRegimes` per runtime, pass it to the constructors of
{', '.join(f'`{c}`' for c in bound)}, and call `Set(regime, tMs)` when the
regime controller of the run takes a transition (Appendix A, A.7.1). While
the current regime does not have such a contract in force, its instances see
no event and no tick, and their deadlines and periodic monitors stop; they go
on from where they stopped when the contract is in force again (Appendix F,
F.5, "Contracts not in force").
""" + _ticks_readme(False)


__all__ = ["generate_unity_csharp"]
