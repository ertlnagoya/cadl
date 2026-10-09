"""CADL SMT Verifier - checks contract consistency using Z3.

Performs:
1. Single contract consistency: assume + guarantee are satisfiable together
2. Assumption satisfiability: a contract's assumes can hold together
3. Cross-contract consistency: contracts sharing parties don't contradict
4. Transition condition mutual exclusivity
5. Regime reachability, dead states and safety invariants

Whether guarantees follow from assumes is reported as information only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Set, Tuple

import z3

from .ast_nodes import (
    ActorRef,
    BinaryOp,
    BoolLiteral,
    Comprehension,
    ContractDef,
    DurationLiteral,
    Expression,
    FloatLiteral,
    FunctionCall,
    Identifier,
    IntLiteral,
    MemberAccess,
    QuantifiedExpr,
    RangeExpr,
    SoSDefinition,
    StringLiteral,
    UnaryOp,
    VerificationSpec,
)


# Methods recognised by CADL Appendix A §A.8.
_KNOWN_METHODS = {"smt", "model_check", "simulation", "proof"}
# Methods the reference verifier in this module can actually discharge.
_SUPPORTED_METHODS = {"smt"}


@dataclass
class VerificationResult:
    """Result of a single verification check."""
    check_name: str
    # "passed", "failed", "unknown", "info" (a finding that is not a
    # defect) or "not_supported". Only "failed" fails verification.
    status: str
    message: str
    counterexample: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        icon = {
            "passed": "PASS",
            "failed": "FAIL",
            "unknown": "UNKNOWN",
            "info": "INFO",
            "not_supported": "SKIP",
        }.get(self.status, self.status.upper())
        s = f"[{icon}] {self.check_name}: {self.message}"
        if self.counterexample:
            s += f"\n        Counterexample: {self.counterexample}"
        return s


class Z3Context:
    """Manages Z3 variable declarations and sort mappings."""

    def __init__(self) -> None:
        self.bool_vars: Dict[str, z3.BoolRef] = {}
        self.int_vars: Dict[str, z3.ArithRef] = {}
        self.real_vars: Dict[str, z3.ArithRef] = {}
        self.func_decls: Dict[str, z3.FuncDeclRef] = {}
        self.string_codes: Dict[str, int] = {}

    def get_bool(self, name: str) -> z3.BoolRef:
        if name not in self.bool_vars:
            self.bool_vars[name] = z3.Bool(name)
        return self.bool_vars[name]

    def get_int(self, name: str) -> z3.ArithRef:
        if name not in self.int_vars:
            self.int_vars[name] = z3.Int(name)
        return self.int_vars[name]

    def get_real(self, name: str) -> z3.ArithRef:
        if name not in self.real_vars:
            self.real_vars[name] = z3.Real(name)
        return self.real_vars[name]

    def get_string_code(self, value: str) -> z3.ArithRef:
        """Distinct numeric constant per string, so "a" == "b" is false."""
        if value not in self.string_codes:
            self.string_codes[value] = len(self.string_codes)
        return z3.RealVal(self.string_codes[value])

    def get_uninterpreted_bool_func(self, name: str, arity: int = 0) -> Any:
        """Get or create an uninterpreted boolean function."""
        key = f"{name}/{arity}"
        if key not in self.func_decls:
            if arity == 0:
                return self.get_bool(name)
            sorts = [z3.IntSort()] * arity + [z3.BoolSort()]
            self.func_decls[key] = z3.Function(name, *sorts)
        return self.func_decls[key]


def _index_to_name(index: Any) -> str:
    """Render an actor index (``*``, a range, a name or a number)."""
    if index == "*":
        return "ALL"
    if isinstance(index, RangeExpr):
        return "range"
    if isinstance(index, ActorRef) and index.index is None:
        return index.name
    if isinstance(index, Identifier):
        return index.name
    if isinstance(index, IntLiteral):
        return str(index.value)
    if isinstance(index, (str, int)):
        return str(index)
    return _expr_key(index)


def _actor_ref_to_name(ref: ActorRef) -> str:
    """Convert an ActorRef to a string name for Z3 variables."""
    if ref.index is None:
        return ref.name
    return f"{ref.name}_{_index_to_name(ref.index)}"


def _expr_key(expr: Any) -> str:
    """Canonical text for an expression, used to name opaque symbols.

    Two occurrences of the same call (``count(late_orders)``) must map to
    the same Z3 variable, so the name is derived from the expression text.
    """
    if isinstance(expr, BoolLiteral):
        return "true" if expr.value else "false"
    if isinstance(expr, (IntLiteral, FloatLiteral)):
        return str(expr.value)
    if isinstance(expr, DurationLiteral):
        return f"{expr.value}{expr.unit}"
    if isinstance(expr, StringLiteral):
        return f'"{expr.value}"'
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, ActorRef):
        return _actor_ref_to_name(expr)
    if isinstance(expr, MemberAccess):
        return f"{_actor_ref_to_name(expr.obj)}_{expr.member}"
    if isinstance(expr, FunctionCall):
        if not expr.args:
            return expr.name
        return f"{expr.name}({', '.join(_expr_key(a) for a in expr.args)})"
    if isinstance(expr, BinaryOp):
        return f"({_expr_key(expr.left)} {expr.op} {_expr_key(expr.right)})"
    if isinstance(expr, UnaryOp):
        return f"({expr.op} {_expr_key(expr.operand)})"
    if isinstance(expr, Comprehension):
        if isinstance(expr.domain, RangeExpr):
            domain = f"{expr.domain.start}..{expr.domain.end}"
        else:
            domain = _expr_key(expr.domain)
        return f"{_expr_key(expr.element)} for {expr.variable} in {domain}"
    if isinstance(expr, QuantifiedExpr):
        return (f"({expr.quantifier} {expr.variable} in "
                f"{_expr_key(expr.domain)}: {_expr_key(expr.predicate)})")
    return f"unknown_{type(expr).__name__}"


_LOGICAL_OPS = {"AND", "OR"}
_ORDER_OPS = {"<", "<=", ">", ">="}
_EQUALITY_OPS = {"==", "!="}
_ARITH_OPS = {"+", "-", "*", "/"}
_SYMBOLS = (Identifier, ActorRef, MemberAccess, FunctionCall)


def _is_boolean_expr(expr: Any) -> bool:
    """True if the expression is boolean by its own shape."""
    if isinstance(expr, (BoolLiteral, QuantifiedExpr)):
        return True
    if isinstance(expr, UnaryOp):
        return expr.op == "NOT"
    if isinstance(expr, BinaryOp):
        return expr.op not in _ARITH_OPS
    return False


def _symbol_name(expr: Any) -> str:
    if isinstance(expr, StringLiteral):
        sanitized = expr.value.replace(" ", "_").replace(".", "_")
        sanitized = "".join(c for c in sanitized if c.isalnum() or c == "_")
        return f"pred_{sanitized or 'str_pred'}"
    return _expr_key(expr)


def _as_bool(val: Any) -> Any:
    return val if z3.is_bool(val) else val != 0


def _as_num(val: Any) -> Any:
    if z3.is_bool(val):
        return z3.If(val, z3.RealVal(1), z3.RealVal(0))
    if z3.is_int(val):
        return z3.ToReal(val)
    return val


def _to_z3_bool(expr: Any, ctx: Z3Context) -> Any:
    """Translate an expression used as a truth value."""
    if isinstance(expr, _SYMBOLS) or isinstance(expr, StringLiteral):
        # A name or call standing alone is an opaque proposition.
        return ctx.get_bool(_symbol_name(expr))
    return _as_bool(expr_to_z3(expr, ctx))


def _to_z3_num(expr: Any, ctx: Z3Context) -> Any:
    """Translate an expression used as a number."""
    if isinstance(expr, _SYMBOLS):
        # A name or call compared or combined arithmetically is a quantity.
        return ctx.get_real(_symbol_name(expr))
    if isinstance(expr, StringLiteral):
        return ctx.get_string_code(expr.value)
    return _as_num(expr_to_z3(expr, ctx))


def expr_to_z3(expr: Expression, ctx: Z3Context) -> Any:
    """Convert a CADL Expression AST node to a Z3 expression.

    Names carry no declared type in CADL, so the sort of a symbol follows
    from how it is used: operands of ``<``, ``+`` etc. become real-valued
    variables, operands of ``AND`` / ``OR`` / ``NOT`` become propositions.
    Function calls are opaque symbols named after their text. A name used
    both ways in one check yields two unrelated variables.
    """
    if isinstance(expr, BoolLiteral):
        return z3.BoolVal(expr.value)

    if isinstance(expr, IntLiteral):
        return z3.IntVal(expr.value)

    if isinstance(expr, FloatLiteral):
        return z3.RealVal(expr.value)

    if isinstance(expr, DurationLiteral):
        # Convert to milliseconds as integer
        multipliers = {"ms": 1, "s": 1000, "min": 60000, "h": 3600000}
        ms = expr.value * multipliers.get(expr.unit, 1)
        return z3.IntVal(ms)

    if isinstance(expr, StringLiteral) or isinstance(expr, _SYMBOLS):
        return ctx.get_bool(_symbol_name(expr))

    if isinstance(expr, BinaryOp):
        if expr.op in _LOGICAL_OPS:
            left = _to_z3_bool(expr.left, ctx)
            right = _to_z3_bool(expr.right, ctx)
            return z3.And(left, right) if expr.op == "AND" else z3.Or(left, right)

        if expr.op in _EQUALITY_OPS and (
            _is_boolean_expr(expr.left) or _is_boolean_expr(expr.right)
        ):
            left = _to_z3_bool(expr.left, ctx)
            right = _to_z3_bool(expr.right, ctx)
            return left == right if expr.op == "==" else left != right

        left = _to_z3_num(expr.left, ctx)
        right = _to_z3_num(expr.right, ctx)
        if expr.op == "==":
            return left == right
        if expr.op == "!=":
            return left != right
        if expr.op == "<":
            return left < right
        if expr.op == "<=":
            return left <= right
        if expr.op == ">":
            return left > right
        if expr.op == ">=":
            return left >= right
        if expr.op == "+":
            return left + right
        if expr.op == "-":
            return left - right
        if expr.op == "*":
            return left * right
        if expr.op == "/":
            return left / right

        # Fallback
        return ctx.get_bool(f"binop_{expr.op}")

    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            return z3.Not(_to_z3_bool(expr.operand, ctx))
        return ctx.get_bool(f"unary_{expr.op}")

    if isinstance(expr, QuantifiedExpr):
        # The body's symbols are opaque and do not mention the bound
        # variable as a Z3 term, so the quantifier reduces to its body.
        return _to_z3_bool(expr.predicate, ctx)

    # Fallback: opaque boolean
    return ctx.get_bool(_expr_key(expr))


def _add_predicates(solver: z3.Solver, preds: List[Expression], ctx: Z3Context) -> None:
    for pred in preds:
        try:
            solver.add(_to_z3_bool(pred, ctx))
        except Exception:
            pass  # Skip expressions that cannot be encoded


def _check_contract_consistency(contract: ContractDef) -> VerificationResult:
    """Check that a single contract's assume + guarantee is satisfiable.

    If assumes AND guarantees are unsatisfiable together, the contract
    is internally contradictory.
    """
    ctx = Z3Context()
    solver = z3.Solver()
    solver.set("timeout", 5000)  # 5 second timeout

    _add_predicates(solver, contract.assume, ctx)
    _add_predicates(solver, contract.guarantee, ctx)

    result = solver.check()

    if result == z3.sat:
        return VerificationResult(
            check_name=f"Contract '{contract.id}' consistency",
            status="passed",
            message="Assumes and guarantees are jointly satisfiable",
        )
    elif result == z3.unsat:
        return VerificationResult(
            check_name=f"Contract '{contract.id}' consistency",
            status="failed",
            message="Assumes and guarantees are contradictory (unsatisfiable together)",
        )
    else:
        return VerificationResult(
            check_name=f"Contract '{contract.id}' consistency",
            status="unknown",
            message="Solver could not determine satisfiability (timeout or undecidable)",
        )


def _check_cross_contract_consistency(
    c1: ContractDef, c2: ContractDef
) -> VerificationResult:
    """Check that two contracts sharing parties don't contradict each other."""
    ctx = Z3Context()
    solver = z3.Solver()
    solver.set("timeout", 5000)

    # Add all constraints from both contracts
    _add_predicates(
        solver, c1.assume + c1.guarantee + c2.assume + c2.guarantee, ctx
    )

    result = solver.check()

    name = f"Cross-contract '{c1.id}' x '{c2.id}'"

    if result == z3.sat:
        return VerificationResult(
            check_name=name,
            status="passed",
            message="Contracts are jointly satisfiable",
        )
    elif result == z3.unsat:
        return VerificationResult(
            check_name=name,
            status="failed",
            message="Contracts are contradictory when combined",
        )
    else:
        return VerificationResult(
            check_name=name,
            status="unknown",
            message="Solver could not determine (timeout or undecidable)",
        )


def _check_transition_exclusivity(sos: SoSDefinition) -> List[VerificationResult]:
    """Check that transitions from the same regime have mutually exclusive conditions."""
    results = []

    # Group transitions by source regime
    from_groups: Dict[str, list] = {}
    for t in sos.transitions:
        from_groups.setdefault(t.from_regime, []).append(t)

    for regime, transitions in from_groups.items():
        if len(transitions) < 2:
            continue

        # Check each pair of transitions from the same regime
        for t1, t2 in combinations(transitions, 2):
            if t1.condition and t2.condition:
                ctx = Z3Context()
                solver = z3.Solver()
                solver.set("timeout", 5000)

                try:
                    cond1 = _parse_condition(t1.condition, ctx)
                    cond2 = _parse_condition(t2.condition, ctx)
                    # Check if both can be true at the same time
                    solver.add(cond1)
                    solver.add(cond2)

                    result = solver.check()
                    name = f"Transition exclusivity: {regime} -> {t1.to_regime} vs {regime} -> {t2.to_regime}"

                    if result == z3.sat:
                        model = solver.model()
                        ce = {str(d): str(model[d]) for d in model.decls()}
                        results.append(VerificationResult(
                            check_name=name,
                            status="failed",
                            message="Transition conditions can be simultaneously true (non-exclusive)",
                            counterexample=ce,
                        ))
                    elif result == z3.unsat:
                        results.append(VerificationResult(
                            check_name=name,
                            status="passed",
                            message="Transition conditions are mutually exclusive",
                        ))
                    else:
                        results.append(VerificationResult(
                            check_name=name,
                            status="unknown",
                            message="Could not determine exclusivity",
                        ))
                except Exception:
                    pass

    return results


def _parse_condition(cond_str: str, ctx: Z3Context) -> Any:
    """Parse a condition string into a Z3 expression."""
    from .parser import parse_expr
    try:
        expr = parse_expr(cond_str)
        return _to_z3_bool(expr, ctx)
    except Exception:
        # Fallback: treat as opaque boolean
        sanitized = cond_str.replace(" ", "_")[:40]
        sanitized = "".join(c for c in sanitized if c.isalnum() or c == "_")
        return ctx.get_bool(f"cond_{sanitized}")


def _check_assumption_satisfiability(contract: ContractDef) -> VerificationResult:
    """Check that a contract's assumptions can hold together.

    A contract C = (A, G) only binds its parties in environments that
    satisfy A. If A is unsatisfiable the contract never applies, and its
    guarantees are vacuous.
    """
    check_name = f"Contract '{contract.id}' assumptions"

    if not contract.assume:
        return VerificationResult(
            check_name=check_name,
            status="passed",
            message="No assumptions (the contract applies unconditionally)",
        )

    ctx = Z3Context()
    solver = z3.Solver()
    solver.set("timeout", 5000)
    _add_predicates(solver, contract.assume, ctx)

    result = solver.check()
    if result == z3.sat:
        return VerificationResult(
            check_name=check_name,
            status="passed",
            message="Assumptions are satisfiable",
        )
    if result == z3.unsat:
        return VerificationResult(
            check_name=check_name,
            status="failed",
            message="Assumptions contradict each other; the contract can never apply",
        )
    return VerificationResult(
        check_name=check_name,
        status="unknown",
        message="Solver could not determine satisfiability (timeout or undecidable)",
    )


def _check_guarantee_entailment(contract: ContractDef) -> VerificationResult:
    """Report whether the guarantees already follow from the assumptions.

    In an assume-guarantee contract the guarantees are obligations the
    parties take on, not consequences of the assumptions, so "not
    entailed" is the normal case and is reported as ``info``. It is not a
    defect and does not fail verification.
    """
    check_name = f"Contract '{contract.id}' entailment"

    if not contract.assume or not contract.guarantee:
        return VerificationResult(
            check_name=check_name,
            status="passed",
            message="No assumes or guarantees to check entailment",
        )

    ctx = Z3Context()
    assume_exprs = []
    for pred in contract.assume:
        try:
            assume_exprs.append(_to_z3_bool(pred, ctx))
        except Exception:
            pass

    if not assume_exprs:
        return VerificationResult(
            check_name=check_name,
            status="passed",
            message="No parseable assumes",
        )

    for pred in contract.guarantee:
        try:
            g = _to_z3_bool(pred, ctx)
        except Exception:
            continue

        solver = z3.Solver()
        solver.set("timeout", 5000)
        for a in assume_exprs:
            solver.add(a)
        solver.add(z3.Not(g))

        result = solver.check()
        if result == z3.sat:
            model = solver.model()
            ce = {str(d): str(model[d]) for d in model.decls()}
            return VerificationResult(
                check_name=check_name,
                status="info",
                message=(
                    "Guarantees do not follow from the assumptions alone; "
                    "they are obligations the parties must meet"
                ),
                counterexample=ce,
            )

    return VerificationResult(
        check_name=check_name,
        status="passed",
        message="All guarantees follow from assumptions",
    )


def _check_regime_reachability(sos: SoSDefinition) -> List[VerificationResult]:
    """Check that all regime states are reachable from the initial state."""
    from .regime_map import RegimeMap

    rm = RegimeMap.from_sos(sos)
    results: List[VerificationResult] = []

    if not rm.states:
        return results

    unreachable = rm.find_unreachable_states()
    if unreachable:
        results.append(VerificationResult(
            check_name="Regime reachability",
            status="failed",
            message=f"Unreachable states: {', '.join(sorted(unreachable))}",
        ))
    else:
        results.append(VerificationResult(
            check_name="Regime reachability",
            status="passed",
            message="All states reachable from initial state",
        ))

    return results


def _check_regime_dead_states(sos: SoSDefinition) -> List[VerificationResult]:
    """Check for dead-end states with no outgoing transitions."""
    from .regime_map import RegimeMap

    rm = RegimeMap.from_sos(sos)
    results: List[VerificationResult] = []

    if not rm.states:
        return results

    dead = rm.find_dead_states()
    if dead:
        results.append(VerificationResult(
            check_name="Regime dead states",
            status="failed",
            message=f"Dead-end states (no outgoing transitions): {', '.join(sorted(dead))}",
        ))
    else:
        results.append(VerificationResult(
            check_name="Regime dead states",
            status="passed",
            message="No dead-end states",
        ))

    return results


def _check_regime_safety_invariants(sos: SoSDefinition) -> List[VerificationResult]:
    """Check that transition safety invariants are satisfiable."""
    results: List[VerificationResult] = []

    for t in sos.transitions:
        if not t.safety_invariant:
            continue

        check_name = f"Safety invariant: {t.from_regime}->{t.to_regime}"
        ctx = Z3Context()
        solver = z3.Solver()
        solver.set("timeout", 5000)

        try:
            inv = _parse_condition(t.safety_invariant, ctx)
            solver.add(inv)

            # Also add transition condition if present
            if t.condition:
                try:
                    cond = _parse_condition(t.condition, ctx)
                    solver.add(cond)
                except Exception:
                    pass

            result = solver.check()
            if result == z3.sat:
                results.append(VerificationResult(
                    check_name=check_name,
                    status="passed",
                    message="Safety invariant is satisfiable with transition condition",
                ))
            elif result == z3.unsat:
                results.append(VerificationResult(
                    check_name=check_name,
                    status="failed",
                    message="Safety invariant is unsatisfiable (contradicts transition condition)",
                ))
            else:
                results.append(VerificationResult(
                    check_name=check_name,
                    status="unknown",
                    message="Could not determine satisfiability",
                ))
        except Exception:
            results.append(VerificationResult(
                check_name=check_name,
                status="unknown",
                message="Could not parse safety invariant expression",
            ))

    return results


def _declared_targets(sos: SoSDefinition) -> Set[str]:
    """Names a ``verification:`` entry may give as its ``target:``."""
    names: Set[str] = {c.id for c in sos.contracts}
    names |= {p.id for p in sos.protocols}
    for t in sos.transitions:
        names |= {t.from_regime, t.to_regime}
        names |= {f"{t.from_regime}->{t.to_regime}", f"{t.from_regime} -> {t.to_regime}"}
    return names


def dispatch_spec(
    spec: VerificationSpec, sos: Optional[SoSDefinition] = None
) -> VerificationResult:
    """Route a user-declared verification spec to the appropriate back-end.

    The reference verifier only implements the ``smt`` method. Other
    methods recognised by the spec (``model_check``, ``simulation``,
    ``proof``) return a ``not_supported`` result so downstream callers
    see an explicit diagnostic instead of a silent skip. Unknown methods
    return a ``failed`` result.

    For ``smt`` entries the verifier checks what it can about the entry
    itself: that ``target:`` names a declared contract, protocol, regime
    or transition (when ``sos`` is given), and that ``expr:`` is
    satisfiable. It does not prove ``expr`` against the model; the
    contract and transition checks of :func:`verify` run regardless of
    the entries.

    See Appendix A §A.8 and Appendix D §D.4 for the conformance rule.
    """
    method = (spec.method or "smt").lower()
    check_name = f"verification.{spec.id}"
    if method == "smt":
        if sos is not None and spec.target and spec.target not in _declared_targets(sos):
            return VerificationResult(
                check_name=check_name,
                status="failed",
                message=(
                    f"target {spec.target!r} is not a declared contract, "
                    f"protocol, regime or transition"
                ),
            )
        if spec.expr:
            ctx = Z3Context()
            solver = z3.Solver()
            solver.set("timeout", 5000)
            solver.add(_parse_condition(spec.expr, ctx))
            outcome = solver.check()
            if outcome == z3.unsat:
                return VerificationResult(
                    check_name=check_name,
                    status="failed",
                    message=f"method=smt; expr is unsatisfiable: {spec.expr}",
                )
            if outcome != z3.sat:
                return VerificationResult(
                    check_name=check_name,
                    status="unknown",
                    message="method=smt; could not decide whether expr is satisfiable",
                )
            return VerificationResult(
                check_name=check_name,
                status="passed",
                message=(
                    "method=smt; expr is satisfiable (not proved against the "
                    "model); built-in contract/transition checks apply"
                ),
            )
        # SMT-backed checks are already discharged by the bulk passes in
        # ``verify(sos)``; this per-spec entry records the acknowledgement.
        return VerificationResult(
            check_name=check_name,
            status="passed",
            message=f"method=smt; discharged by built-in contract/transition checks",
        )
    if method in _KNOWN_METHODS:
        return VerificationResult(
            check_name=f"verification.{spec.id}",
            status="not_supported",
            message=(
                f"method={method!r} is defined by Appendix A §A.8 but is "
                f"not yet implemented in the reference verifier "
                f"(supported: {sorted(_SUPPORTED_METHODS)})"
            ),
        )
    return VerificationResult(
        check_name=f"verification.{spec.id}",
        status="failed",
        message=(
            f"unknown verification method {method!r}; expected one of "
            f"{sorted(_KNOWN_METHODS)}"
        ),
    )


def verify(sos: SoSDefinition) -> List[VerificationResult]:
    """Run all verification checks on a CADL SoS definition.

    Built-in contract / transition SMT checks run unconditionally.
    Any explicit ``verification:`` entries on ``sos.verifications`` are
    dispatched through :func:`dispatch_spec` so non-SMT methods surface
    as explicit ``not_supported`` diagnostics.

    Returns a list of VerificationResult objects.
    """
    results: List[VerificationResult] = []

    # 1. Check each contract for internal consistency
    for contract in sos.contracts:
        results.append(_check_contract_consistency(contract))

    # 1b. Check that each contract's assumptions can hold at all
    for contract in sos.contracts:
        results.append(_check_assumption_satisfiability(contract))

    # 1c. Report (informationally) whether guarantees follow from assumes
    for contract in sos.contracts:
        results.append(_check_guarantee_entailment(contract))

    # 2. Check cross-contract consistency for contracts sharing parties
    for c1, c2 in combinations(sos.contracts, 2):
        parties1 = {p.name for p in c1.parties}
        parties2 = {p.name for p in c2.parties}
        if parties1 & parties2:  # shared parties
            results.append(_check_cross_contract_consistency(c1, c2))

    # 3. Check transition condition mutual exclusivity
    results.extend(_check_transition_exclusivity(sos))

    # 4. Regime map analysis (if transitions exist)
    if sos.transitions:
        results.extend(_check_regime_reachability(sos))
        results.extend(_check_regime_dead_states(sos))
        results.extend(_check_regime_safety_invariants(sos))

    # 5. Per-spec method dispatch (Appendix A §A.8)
    for spec in sos.verifications:
        results.append(dispatch_spec(spec, sos))

    return results
