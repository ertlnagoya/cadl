"""Regime bindings: parameter values and active contracts per regime.

`sos.regimes:` (Appendix A, A.7.1) binds, per regime, values of declared
parameters (``C.alpha``, ``C.beta``, ``C.lambda`` of a contract, and
``environment.X`` of ``context.environment``) and which contracts are
active. Rules R-1 to R-6 are checked by :func:`check` (R-7 belongs to the
stochastic model extension and is checked there); :func:`checked` returns
the resolved bindings of a file whose block satisfies the rules, or None.
A file without the block has no bindings, and every tool behaves as it did
before the block existed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .ast_nodes import SoSDefinition

_KEY_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*")
_INSTITUTIONAL = ("alpha", "beta", "lambda")
_YAML11_HINT = (
    "a YAML 1.1 loader reads an unquoted ON, No, Off, Yes, null or ~ as a "
    "boolean or null, not as a name: write the name in quotes"
)


@dataclass
class RegimeBinding:
    """The resolved binding of one regime."""
    name: str
    # Parameter key ("C.beta", "environment.X") -> value, in the order written.
    parameters: dict = field(default_factory=dict)
    active: list = field(default_factory=list)      # contract ids, declaration order
    suspended: list = field(default_factory=list)   # contract ids, declaration order


def declared(sos: SoSDefinition) -> bool:
    """Whether the file has a `regimes:` block (a null block counts as none)."""
    return getattr(sos, "regimes_raw", None) is not None


def regimes_of(sos: SoSDefinition) -> list[str]:
    """The regimes of a file, in order of first appearance in transitions:."""
    out: list[str] = []
    for t in sos.transitions:
        for name in (t.from_regime, t.to_regime):
            if name and name not in out:
                out.append(name)
    return out


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def declared_value(sos: SoSDefinition, key: str) -> Any:
    """The declared value of a parameter key, or None."""
    m = _KEY_RE.fullmatch(key)
    if m is None:
        return None
    owner, attr = m.group(1), m.group(2)
    if owner == "environment":
        env = sos.context.environment.raw if (
            sos.context and sos.context.environment) else {}
        return env.get(attr)
    c = next((c for c in sos.contracts if c.id == owner), None)
    if c is None:
        return None
    return {
        "alpha": c.information.alpha if c.information else None,
        "beta": c.authority.beta if c.authority else None,
        "lambda": c.incentives.lambda_ if c.incentives else None,
    }.get(attr)


def _env_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number" if math.isfinite(value) else None
    if isinstance(value, str):
        return "string"
    return None


def _analyse(sos: SoSDefinition) -> tuple[list[str], list[RegimeBinding] | None]:
    """(errors, bindings); bindings is None when there is an error or no block."""
    errors: list[str] = []
    raw = getattr(sos, "regimes_raw", None)
    if raw is None:
        return errors, None

    def err(msg: str, rule: str) -> None:
        errors.append(f"{msg} ({rule})")

    regimes = regimes_of(sos)
    contract_ids = [c.id for c in sos.contracts]
    env = sos.context.environment.raw if (sos.context and sos.context.environment) else {}

    if not isinstance(raw, dict):
        err("regimes: must be a mapping from regime names to "
            "{parameters: {...}, contracts: {...}}", "R-1")
        return errors, None
    if not regimes and raw:
        err("regimes: binds regimes, but the file has no transitions:, so it has "
            "no regimes; a regime is introduced by from: or to: of a transition", "R-1")
        return errors, None

    written: dict[str, dict] = {}
    for key, entry in raw.items():
        if key is None or isinstance(key, bool):
            err(f"regimes: has the key '{_text(key)}', a boolean or null; {_YAML11_HINT}",
                "R-1")
            continue
        name = str(key)
        if name not in regimes:
            err(f"regimes: binds '{name}', which is not a regime of the file (the "
                f"regimes are {', '.join(regimes)}, from transitions:)", "R-1")
            continue
        if entry is None:
            written[name] = {}
            continue
        if not isinstance(entry, dict):
            err(f"regimes.{name} must be a mapping with the optional keys "
                f"'parameters' and 'contracts'", "R-2")
            continue
        unknown = [k for k in entry if k not in ("parameters", "contracts")]
        if unknown:
            err(f"regimes.{name} has the key(s) {', '.join(_text(k) for k in unknown)}; "
                f"only 'parameters' and 'contracts' are allowed", "R-2")
            continue
        written[name] = entry

    bindings: list[RegimeBinding] = []
    for name in regimes:
        b = RegimeBinding(name=name)
        entry = written.get(name, {})
        where = f"regimes.{name}"

        # -- contracts (R-5) --
        contracts = entry.get("contracts")
        active = list(contract_ids)
        if "contracts" in entry:
            if not isinstance(contracts, dict) or not contracts or any(
                    k not in ("active", "suspended") for k in contracts):
                err(f"{where}.contracts must be a mapping with one of the keys "
                    f"'active' or 'suspended'", "R-5")
                contracts = {}
            both = "active" in contracts and "suspended" in contracts
            if both:
                err(f"{where}.contracts gives both 'active' and 'suspended'; give one: "
                    f"'active' lists every contract in force in the regime, "
                    f"'suspended' lists the contracts not in force", "R-5")
            for which in ("active", "suspended"):
                if which not in contracts:
                    continue
                lst = contracts[which]
                if lst is None:
                    lst = []
                if not isinstance(lst, list):
                    err(f"{where}.contracts.{which} must be a sequence of "
                        f"contract ids", "R-5")
                    continue
                seen: set[str] = set()
                ok = True
                for cid in lst:
                    if cid is None or isinstance(cid, bool) or not isinstance(cid, str):
                        err(f"{where}.contracts.{which} has the entry "
                            f"'{_text(cid)}', which is not a contract id", "R-5")
                        ok = False
                    elif cid not in contract_ids:
                        err(f"{where}.contracts.{which} names '{cid}', which is "
                            f"not a declared contract", "R-5")
                        ok = False
                    elif cid in seen:
                        err(f"{where}.contracts.{which} lists '{cid}' twice", "R-5")
                        ok = False
                    else:
                        seen.add(cid)
                if ok and not both:
                    if which == "active":
                        active = [c for c in contract_ids if c in seen]
                    else:
                        active = [c for c in contract_ids if c not in seen]
        b.active = active
        b.suspended = [c for c in contract_ids if c not in active]

        # -- parameters (R-3, R-4, R-6) --
        params = entry.get("parameters")
        if params is not None and not isinstance(params, dict):
            err(f"{where}.parameters must be a mapping from parameters (C.alpha, "
                f"C.beta, C.lambda, environment.X) to values", "R-3")
            params = None
        canon_seen: dict[str, str] = {}
        for pkey, value in (params or {}).items():
            ptext = _text(pkey)
            m = _KEY_RE.fullmatch(ptext) if isinstance(pkey, str) else None
            if m is None:
                err(f"{where}.parameters has the key '{ptext}'; a parameter is written "
                    f"C.alpha, C.beta or C.lambda for a contract C, or environment.X "
                    f"for an entry X of context.environment", "R-3")
                continue
            owner, attr = m.group(1), m.group(2)
            canon = f"{owner}.{attr}"
            if canon in canon_seen:
                err(f"{where}.parameters binds '{canon}' twice (as '{canon_seen[canon]}' "
                    f"and '{ptext}')", "R-6")
                continue
            canon_seen[canon] = ptext
            if owner == "environment":
                if attr not in env:
                    err(f"{where}.parameters binds '{canon}', but context.environment "
                        f"has no entry '{attr}'", "R-3")
                    continue
                kind = _env_kind(env[attr])
                if kind is None:
                    err(f"{where}.parameters binds '{canon}', but the entry '{attr}' of "
                        f"context.environment is not a scalar (boolean, number or "
                        f"string) and cannot be bound", "R-4")
                    continue
                vkind = _env_kind(value)
                if not (vkind == kind or (kind == "number" and vkind == "integer")):
                    art = "an" if kind == "integer" else "a"
                    err(f"{where}.parameters gives '{canon}' the value '{_text(value)}' "
                        f"({vkind or 'not a scalar'}); the entry of context.environment "
                        f"is '{_text(env[attr])}', {art} {kind}, and the value must be "
                        f"{art} {kind}"
                        + (" (an integer or a number)" if kind == "number" else ""),
                        "R-4")
                    continue
            else:
                if attr not in _INSTITUTIONAL:
                    err(f"{where}.parameters has the key '{ptext}'; after the contract id "
                        f"it must name alpha, beta or lambda", "R-3")
                    continue
                if owner not in contract_ids:
                    err(f"{where}.parameters binds '{canon}', but '{owner}' is not a "
                        f"declared contract", "R-3")
                    continue
                if declared_value(sos, canon) is None:
                    block = {"alpha": "information.alpha", "beta": "authority.beta",
                             "lambda": "incentives.lambda"}[attr]
                    err(f"{where}.parameters binds '{canon}', but the contract '{owner}' "
                        f"does not write {block}; a regime binds a value the contract "
                        f"declares", "R-3")
                    continue
                if not _is_number(value):
                    err(f"{where}.parameters gives '{canon}' the value '{_text(value)}', "
                        f"not a number", "R-4")
                    continue
                if not 0 <= value <= 1:
                    err(f"{where}.parameters gives '{canon}' the value {_text(value)}, "
                        f"outside [0, 1]", "R-4")
                    continue
                if owner not in b.active:
                    err(f"{where}.parameters binds '{canon}', but the contract '{owner}' "
                        f"is suspended in the regime '{name}'", "R-6")
                    continue
            b.parameters[canon] = value
        bindings.append(b)

    return errors, (None if errors else bindings)


def check(sos: SoSDefinition) -> list[str]:
    """The errors of rules R-1 to R-6 for the `regimes:` block."""
    return _analyse(sos)[0]


def checked(sos: SoSDefinition) -> list[RegimeBinding] | None:
    """The resolved bindings, one per regime in order of first appearance in
    transitions:, or None when the file has no `regimes:` block or the block
    breaks a rule."""
    return _analyse(sos)[1]


def co_active(bindings: list[RegimeBinding], a: str, b: str) -> list[str]:
    """The regimes in which both contracts are active."""
    return [r.name for r in bindings if a in r.active and b in r.active]


def to_ir(sos: SoSDefinition) -> Any:
    """The `regimes` field of the IR: a list of resolved bindings, or None."""
    bindings = checked(sos)
    if bindings is None:
        return None
    return to_ir_bindings(bindings)


def to_ir_bindings(bindings: list[RegimeBinding]) -> list[dict]:
    """Resolved bindings in the form of the IR (and of `cadl regime-map`)."""
    return [
        {
            "name": b.name,
            "parameters": dict(b.parameters),
            "active_contracts": list(b.active),
            "suspended_contracts": list(b.suspended),
        }
        for b in bindings
    ]


def initial_regime(sos: SoSDefinition) -> str | None:
    """The regime in which a run starts (A.7.1): the first `from:` that is
    not the `to:` of any transition, otherwise the `from:` of the first
    transition; None for a file without transitions. It is the initial
    state of `cadl regime-map`."""
    targets = {t.to_regime for t in sos.transitions}
    for t in sos.transitions:
        if t.from_regime not in targets:
            return t.from_regime
    return sos.transitions[0].from_regime if sos.transitions else None


def suspended_somewhere(bindings: list[RegimeBinding] | None, contract_id: str) -> list[str]:
    """The regimes in which a contract is not in force (empty without bindings)."""
    return [b.name for b in (bindings or []) if contract_id not in b.active]
