"""CADL OPA/Rego Policy Generator.

Generates Open Policy Agent Rego policies (.rego) from CADL SoS definitions.
Each ContractDef becomes a Rego package with assume/guarantee rules,
violation detection, and authority/information policy rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ...ast_nodes import (
    ContractDef,
    Expression,
    SoSDefinition,
)
from ...norms import omitted_comment, omitted_in_codegen
from ..emitter import sanitize_id, snake_case
from .rego_expr import RegoContext, expr_to_rego


def generate_rego(sos: SoSDefinition, output_dir: Path) -> None:
    """Generate OPA Rego policies from a CADL SoS definition.

    Creates one .rego file per contract, plus a main policy file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for contract in sos.contracts:
        code = generate_contract_rego(contract, sos, _bindings(sos))
        filename = f"{snake_case(contract.id)}.rego"
        (output_dir / filename).write_text(code, encoding="utf-8")

    # Generate main policy
    code = generate_main_rego(sos, _bindings(sos))
    (output_dir / "main.rego").write_text(code, encoding="utf-8")


def _bindings(sos: SoSDefinition) -> list | None:
    """The regime bindings of `regimes:` (Appendix A, A.7.1), or None for a
    file without the block or without transitions: the policies are then
    the same as before the block existed."""
    if not sos.transitions:
        return None
    from ...regimes import checked
    return checked(sos)


def _rego_value(value) -> str:
    """A scalar of a regime binding as a Rego term (JSON)."""
    import json
    return json.dumps(value, ensure_ascii=False)


def _per_regime(lines: list, name: str, per_regime: dict, default: str,
                guard: str | None = None) -> None:
    """`name` with the value of the current regime, else `default` (only
    when `guard` holds, if given)."""
    first = True
    for regime, value in per_regime.items():
        head = f"{name} := " if first else "} else := "
        lines.append(f"{head}{value} if {{")
        lines.append(f"    input.regime == {_rego_value(regime)}")
        first = False
    if guard is None:
        lines.append(f"}} else := {default}")
    else:
        lines.append(f"}} else := {default} if {{")
        lines.append(f"    {guard}")
        lines.append("}")


def _regime_rules(lines: list, contract: ContractDef, bindings: list) -> None:
    """Which regime is current, and whether the contract is in force in it."""
    regimes = [b.name for b in bindings]
    suspended = [b.name for b in bindings if contract.id not in b.active]
    lines.append("# Regime bindings (regimes:, Appendix A, A.7.1): input.regime names the")
    lines.append("# current regime, whose bindings apply. Without it, or with a name that is")
    lines.append("# not a regime of the file, nothing is allowed and regime_error says why.")
    lines.append(f"regimes := {_rego_list(regimes)}")
    lines.append("")
    lines.append("regime_error := msg if {")
    lines.append("    not input.regime")
    lines.append('    msg := "input.regime is missing: it names the current regime"')
    lines.append("} else := msg if {")
    lines.append("    not input.regime in regimes")
    lines.append('    msg := sprintf("input.regime %v is not a regime of the file", [input.regime])')
    lines.append("}")
    lines.append("")
    if suspended:
        lines.append(f"# The regimes that do not have {contract.id} in force")
        lines.append(f"suspended_in := {_rego_list(suspended)}")
        lines.append("")
    lines.append("in_force if {")
    lines.append("    not regime_error")
    if suspended:
        lines.append("    not input.regime in suspended_in")
    lines.append("}")
    lines.append("")


def generate_contract_rego(contract: ContractDef, sos: SoSDefinition,
                           bindings: list | None = None) -> str:
    """Generate a Rego policy for a CADL ContractDef. With the regime
    bindings of `regimes:` (A.7.1), the policy reads `input.regime`: a
    contract that is not in force in it is satisfied and has no violation,
    and a value that the regime binds has the bound value."""
    pkg_name = snake_case(contract.id)
    ctx = RegoContext()

    lines: list[str] = []
    lines.append(f"# Generated from CADL contract: {contract.id}")
    lines.append(f"package cadl.{pkg_name}")
    lines.append("")
    lines.append("import rego.v1")
    lines.append("")

    # Metadata
    lines.append("# Contract metadata")
    lines.append(f'contract_id := "{contract.id}"')
    party_names = [_party_str(p) for p in contract.parties]
    lines.append(f"parties := {_rego_list(party_names)}")
    lines.append("")

    if bindings is not None:
        _regime_rules(lines, contract, bindings)
    bound: dict = {}
    for b in bindings or []:
        for key, value in b.parameters.items():
            owner, attr = key.split(".", 1)
            if owner == contract.id:
                bound.setdefault(attr, {})[b.name] = value

    # Institutional parameters
    for attr, name, declared in (
            ("beta", "beta", contract.authority.beta if contract.authority else None),
            ("alpha", "alpha", contract.information.alpha if contract.information else None),
            ("lambda", "lambda_", contract.incentives.lambda_ if contract.incentives else None)):
        if declared is None:
            continue
        if attr in bound:
            lines.append(f"# {name} in the current regime (A.7.1); declared {declared}")
            _per_regime(lines, name, bound[attr], str(declared))
        else:
            lines.append(f"{name} := {declared}")
    if contract.authority or contract.information or contract.incentives:
        lines.append("")

    # Default allow
    lines.append("default allow := false")
    lines.append("")
    helpers: list[str] = []      # rules of items with OR (one body per disjunct)

    # assumptions_hold rule
    lines.append("# Assumptions: all must hold")
    lines.append("assumptions_hold if {")
    generated = 0
    for pos, pred in enumerate(contract.assume, start=1):
        if omitted_in_codegen(pred):
            lines.append(f"    # {omitted_comment(pred)}")
            continue
        rego_expr = _compile_item(pred, ctx, "assumption_{}".format(pos), helpers)
        label = _predicate_label(pred)
        lines.append(f"    # {label}")
        lines.append(f"    {rego_expr}")
        generated += 1
    if not generated:
        lines.append("    true")
    lines.append("}")
    lines.append("")

    # guarantees_hold rule
    lines.append("# Guarantees: all must hold")
    lines.append("guarantees_hold if {")
    generated = 0
    for pos, pred in enumerate(contract.guarantee, start=1):
        if omitted_in_codegen(pred):
            lines.append(f"    # {omitted_comment(pred)}")
            continue
        rego_expr = _compile_item(pred, ctx, "guarantee_{}".format(pos), helpers)
        label = _predicate_label(pred)
        lines.append(f"    # {label}")
        lines.append(f"    {rego_expr}")
        generated += 1
    if not generated:
        lines.append("    true")
    lines.append("}")
    lines.append("")

    if helpers:
        lines.append("# Items with OR: one rule body per disjunct")
        lines.extend(helpers)

    # allow rule
    lines.append("# Contract satisfied when assumptions imply guarantees hold")
    lines.append("allow if {")
    if bindings is not None:
        lines.append("    in_force")
    lines.append("    assumptions_hold")
    lines.append("    guarantees_hold")
    lines.append("}")
    lines.append("")
    if bindings is not None:
        lines.append("# A contract that the current regime does not have in force is satisfied")
        lines.append("allow if {")
        lines.append("    not regime_error")
        lines.append("    not in_force")
        lines.append("}")
        lines.append("")

    # violation rule
    lines.append("# Violation: assumptions hold but guarantees do not")
    lines.append("violation contains msg if {")
    if bindings is not None:
        lines.append("    in_force")
    lines.append("    assumptions_hold")
    lines.append("    not guarantees_hold")
    lines.append(f'    msg := "Guarantee violated in {contract.id}"')
    lines.append("}")
    lines.append("")
    if bindings is not None:
        lines.append("violation contains regime_error")
        lines.append("")

    # Authority policy
    if contract.authority:
        lines.append("# Authority policy")
        if contract.authority.decision_scope:
            lines.append(f'decision_scope := "{contract.authority.decision_scope}"')
        if contract.authority.decision_holder:
            holder = contract.authority.decision_holder.name
            lines.append(f'decision_holder := "{holder}"')
        lines.append("")

    # Information sharing policy
    if contract.information and contract.information.sharing:
        lines.append("# Information sharing rules")
        for i, sharing in enumerate(contract.information.sharing):
            src = sharing.source.name.lower()
            tgt = sharing.target.name.lower()
            data = sharing.data
            lines.append(f"sharing_allowed_{i} if {{")
            lines.append(f'    input.source == "{src}"')
            lines.append(f'    input.target == "{tgt}"')
            lines.append(f'    input.data == "{data}"')
            lines.append("}")
        lines.append("")

    return "\n".join(lines)


def generate_main_rego(sos: SoSDefinition, bindings: list | None = None) -> str:
    """Generate the main Rego policy that aggregates all contracts. With the
    regime bindings of `regimes:` (A.7.1) it gives the values of the bound
    parameters in the regime that `input.regime` names."""
    pkg_name = snake_case(sos.name)

    lines: list[str] = []
    lines.append(f"# Generated CADL main policy: {sos.name}")
    lines.append(f"package cadl.{pkg_name}")
    lines.append("")
    lines.append("import rego.v1")
    lines.append("")

    # Import all contract packages
    for contract in sos.contracts:
        cpkg = snake_case(contract.id)
        lines.append(f"import data.cadl.{cpkg}")
    lines.append("")

    # SoS metadata
    lines.append(f'sos_name := "{sos.name}"')
    if sos.type:
        lines.append(f'sos_type := "{sos.type.value}"')
    if sos.version:
        lines.append(f'sos_version := "{sos.version}"')
    lines.append("")

    # Actor list
    if sos.actors:
        actor_names = [a.id.name for a in sos.actors]
        lines.append(f"actors := {_rego_list(actor_names)}")
        lines.append("")

    if bindings is not None:
        from ...regimes import declared_value, initial_regime
        keys = list(dict.fromkeys(k for b in bindings for k in b.parameters))
        declared = {k: declared_value(sos, k) for k in keys}
        lines.append("# Regime bindings (regimes:, Appendix A, A.7.1): the regimes, the one in")
        lines.append("# which a run starts, and the value of every bound parameter in the")
        lines.append("# regime that input.regime names (C.alpha, C.beta, C.lambda,")
        lines.append("# environment.X)")
        lines.append(f"regimes := {_rego_list([b.name for b in bindings])}")
        lines.append(f'initial_regime := "{initial_regime(sos)}"')
        lines.append("")
        default = "{" + ", ".join(f"{_rego_value(k)}: {_rego_value(v)}"
                                  for k, v in declared.items()) + "}"
        per_regime = {}
        for b in bindings:
            values = dict(declared)
            values.update(b.parameters)
            if values != declared:
                per_regime[b.name] = "{" + ", ".join(
                    f"{_rego_value(k)}: {_rego_value(v)}" for k, v in values.items()) + "}"
        # undefined without input.regime or with a name that is not a regime
        if per_regime:
            _per_regime(lines, "parameters", per_regime, default, "input.regime in regimes")
        else:
            lines.append(f"parameters := {default} if {{")
            lines.append("    input.regime in regimes")
            lines.append("}")
        lines.append("")

    # Aggregate compliance check
    lines.append("default all_contracts_satisfied := false")
    lines.append("")
    lines.append("all_contracts_satisfied if {")
    for contract in sos.contracts:
        cpkg = snake_case(contract.id)
        lines.append(f"    data.cadl.{cpkg}.allow")
    if not sos.contracts:
        lines.append("    true")
    lines.append("}")
    lines.append("")

    # Aggregate violations
    # one rule per contract: the violations of any contract (up to 0.3.32 one
    # rule assigned msg once per contract, which OPA rejects for two or more)
    if sos.contracts:
        for contract in sos.contracts:
            cpkg = snake_case(contract.id)
            lines.append("all_violations contains msg if {")
            lines.append(f"    some msg in data.cadl.{cpkg}.violation")
            lines.append("}")
            lines.append("")
    else:
        lines.append("all_violations contains msg if {")
        lines.append('    msg := "no contracts"')
        lines.append("    false")
        lines.append("}")
        lines.append("")

    return "\n".join(lines)


_MAX_DISJUNCTS = 64
_REGO_OPERATORS = frozenset({"==", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "AND", "OR"})


def _compile_item(expr: Expression, ctx: RegoContext, name: str, helpers: list) -> str:
    """One `assume:` / `guarantee:` item as a literal of a rule body.

    - An item with OR, or with NOT over AND, OR or NOT, is a rule `name`
      with one body per disjunct of its disjunctive normal form (NOT pushed
      down to the comparisons); up to 0.3.32 `A OR B` was written as
      `(A) # OR (B)`, which drops B, and `NOT (A AND B)` as `not A; B`.
    - A text item (an opaque predicate, A.10), and an item with a
      quantifier, a comprehension, or more than 64 disjuncts, is a truth
      value the host gives (`input.items["TEXT"] == true`); up to 0.3.32 a
      text item was a string, which Rego reads as true.
    """
    from ...ast_nodes import StringLiteral
    if isinstance(expr, StringLiteral) or _untranslated(expr):
        return _host_item(expr, ctx)
    if not _needs_dnf(expr):
        return expr_to_rego(expr, ctx)
    dnf = _dnf(expr, False, ctx)
    if dnf is None:
        return _host_item(expr, ctx)
    for conj in dnf:
        helpers.append(f"{name} if {{")
        helpers.extend(f"    {lit}" for lit in conj)
        helpers.append("}")
        helpers.append("")
    return name


def _host_item(expr: Expression, ctx: RegoContext) -> str:
    import json
    from ...ast_nodes import StringLiteral
    text = expr.value if isinstance(expr, StringLiteral) else None
    if text is None:
        from ...unparse import expr_to_source
        text = expr_to_source(expr)
    return f"{ctx.input_prefix}.items[{json.dumps(text)}] == true"


def _needs_dnf(expr) -> bool:
    from ...ast_nodes import BinaryOp, UnaryOp
    if isinstance(expr, BinaryOp):
        if expr.op == "OR":
            return True
        return expr.op == "AND" and (_needs_dnf(expr.left) or _needs_dnf(expr.right))
    if isinstance(expr, UnaryOp) and expr.op == "NOT":
        inner = expr.operand
        return (isinstance(inner, BinaryOp) and inner.op in ("AND", "OR")) or (
            isinstance(inner, UnaryOp) and inner.op == "NOT")
    return False


def _dnf(expr, negated: bool, ctx: RegoContext):
    """The disjunctive normal form as lists of Rego literals, or None when
    it has more than _MAX_DISJUNCTS disjuncts."""
    from ...ast_nodes import BinaryOp, UnaryOp
    if isinstance(expr, UnaryOp) and expr.op == "NOT":
        return _dnf(expr.operand, not negated, ctx)
    if isinstance(expr, BinaryOp) and expr.op in ("AND", "OR"):
        left = _dnf(expr.left, negated, ctx)
        right = _dnf(expr.right, negated, ctx)
        if left is None or right is None:
            return None
        if (expr.op == "OR") != negated:          # a disjunction
            out = left + right
        else:                                      # a conjunction
            out = [a + b for a in left for b in right]
        return out if len(out) <= _MAX_DISJUNCTS else None
    lit = expr_to_rego(expr, ctx)
    return [[f"not {lit}" if negated else lit]]


def _untranslated(expr) -> bool:
    import dataclasses
    from ...ast_nodes import BinaryOp, Comprehension, QuantifiedExpr
    if isinstance(expr, (QuantifiedExpr, Comprehension)):
        return True
    if isinstance(expr, BinaryOp) and expr.op not in _REGO_OPERATORS:
        return True                     # IN of a monitor rule, for example
    if type(expr).__name__ in ("ListLiteral", "DistLiteral"):
        return True                     # written as `true # unsupported`
    if isinstance(expr, list):
        return any(_untranslated(x) for x in expr)
    if dataclasses.is_dataclass(expr) and not isinstance(expr, type):
        return any(_untranslated(getattr(expr, f.name)) for f in dataclasses.fields(expr)
                   if f.name != "loc")
    return False


def _predicate_label(expr: Expression) -> str:
    """Get a human-readable label for a predicate."""
    from ...ast_nodes import StringLiteral, BinaryOp, MemberAccess, FunctionCall, Identifier

    if isinstance(expr, StringLiteral):
        return expr.value
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, FunctionCall) and not expr.args:
        return f"{expr.name}()"
    from ...unparse import expr_to_source
    return expr_to_source(expr)


def _party_str(party) -> str:
    """Convert a party ActorRef to a string."""
    from ...ast_nodes import ActorRef
    if isinstance(party, ActorRef):
        if party.index == "*":
            return f"{party.name}[*]"
        if party.index is not None:
            return f"{party.name}[{party.index}]"
        return party.name
    return str(party)


def _rego_list(items: list[str]) -> str:
    """Format a Python list as a Rego array literal."""
    quoted = [f'"{item}"' for item in items]
    return f"[{', '.join(quoted)}]"
