"""CADL SMT Verifier - checks contract consistency using Z3.

Performs:
1. Single contract consistency: assume + guarantee are satisfiable together
2. Cross-contract consistency: contracts sharing parties don't contradict
3. Transition condition mutual exclusivity
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
)


@dataclass
class VerificationResult:
    """Result of a single verification check."""
    check_name: str
    status: str  # "passed", "failed", "unknown"
    message: str
    counterexample: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        icon = {"passed": "PASS", "failed": "FAIL", "unknown": "UNKNOWN"}[self.status]
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

    def get_uninterpreted_bool_func(self, name: str, arity: int = 0) -> Any:
        """Get or create an uninterpreted boolean function."""
        key = f"{name}/{arity}"
        if key not in self.func_decls:
            if arity == 0:
                return self.get_bool(name)
            sorts = [z3.IntSort()] * arity + [z3.BoolSort()]
            self.func_decls[key] = z3.Function(name, *sorts)
        return self.func_decls[key]


def _actor_ref_to_name(ref: ActorRef) -> str:
    """Convert an ActorRef to a string name for Z3 variables."""
    name = ref.name
    if ref.index == "*":
        return f"{name}_ALL"
    if isinstance(ref.index, RangeExpr):
        return f"{name}_range"
    if ref.index is not None:
        return f"{name}_{ref.index}"
    return name


def expr_to_z3(expr: Expression, ctx: Z3Context) -> Any:
    """Convert a CADL Expression AST node to a Z3 expression.

    Uses a best-effort approach: expressions that can't be fully encoded
    are represented as opaque boolean variables or uninterpreted functions.
    """
    if isinstance(expr, BoolLiteral):
        return z3.BoolVal(expr.value)

    if isinstance(expr, IntLiteral):
        return z3.IntVal(expr.value)

    if isinstance(expr, FloatLiteral):
        return z3.RealVal(expr.value)

    if isinstance(expr, StringLiteral):
        # Treat string predicates as opaque boolean variables
        sanitized = expr.value.replace(" ", "_").replace(".", "_")
        sanitized = "".join(c for c in sanitized if c.isalnum() or c == "_")
        if not sanitized:
            sanitized = "str_pred"
        return ctx.get_bool(f"pred_{sanitized}")

    if isinstance(expr, Identifier):
        # Unknown identifier → boolean variable
        return ctx.get_bool(expr.name)

    if isinstance(expr, DurationLiteral):
        # Convert to milliseconds as integer
        multipliers = {"ms": 1, "s": 1000, "min": 60000, "h": 3600000}
        ms = expr.value * multipliers.get(expr.unit, 1)
        return z3.IntVal(ms)

    if isinstance(expr, ActorRef):
        return ctx.get_bool(_actor_ref_to_name(expr))

    if isinstance(expr, MemberAccess):
        # e.g. DISPATCHER.is_operational → uninterpreted bool
        name = f"{expr.obj.name}_{expr.member}"
        return ctx.get_bool(name)

    if isinstance(expr, FunctionCall):
        # Encode function calls as uninterpreted functions
        if not expr.args:
            return ctx.get_bool(expr.name)
        # With args: create uninterpreted function
        z3_args = [expr_to_z3(a, ctx) for a in expr.args]
        func = ctx.get_uninterpreted_bool_func(expr.name, len(z3_args))
        if callable(func) and not isinstance(func, z3.BoolRef):
            return func(*z3_args)
        return func

    if isinstance(expr, BinaryOp):
        left = expr_to_z3(expr.left, ctx)
        right = expr_to_z3(expr.right, ctx)

        # Logical operators
        if expr.op == "AND":
            return z3.And(left, right)
        if expr.op == "OR":
            return z3.Or(left, right)

        # Comparison operators — need arithmetic sorts
        left_arith = _to_arith(left, ctx)
        right_arith = _to_arith(right, ctx)

        if expr.op == "==":
            return left_arith == right_arith
        if expr.op == "!=":
            return left_arith != right_arith
        if expr.op == "<":
            return left_arith < right_arith
        if expr.op == "<=":
            return left_arith <= right_arith
        if expr.op == ">":
            return left_arith > right_arith
        if expr.op == ">=":
            return left_arith >= right_arith

        # Arithmetic operators
        if expr.op == "+":
            return left_arith + right_arith
        if expr.op == "-":
            return left_arith - right_arith
        if expr.op == "*":
            return left_arith * right_arith
        if expr.op == "/":
            return left_arith / right_arith

        # Fallback
        return ctx.get_bool(f"binop_{expr.op}")

    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            operand = expr_to_z3(expr.operand, ctx)
            return z3.Not(operand)
        return ctx.get_bool(f"unary_{expr.op}")

    if isinstance(expr, QuantifiedExpr):
        # Create a Z3 integer variable for the quantified variable
        qvar = z3.Int(expr.variable)
        body = expr_to_z3(expr.predicate, ctx)
        if expr.quantifier == "for_all":
            return z3.ForAll([qvar], body)
        else:  # exists
            return z3.Exists([qvar], body)

    # Fallback: opaque boolean
    return ctx.get_bool(f"unknown_{id(expr)}")


def _to_arith(val: Any, ctx: Z3Context) -> Any:
    """Coerce a Z3 value to arithmetic sort if it's boolean."""
    if z3.is_bool(val):
        # Convert bool to int (0/1) for arithmetic
        return z3.If(val, z3.IntVal(1), z3.IntVal(0))
    return val


def _check_contract_consistency(contract: ContractDef) -> VerificationResult:
    """Check that a single contract's assume + guarantee is satisfiable.

    If assumes AND guarantees are unsatisfiable together, the contract
    is internally contradictory.
    """
    ctx = Z3Context()
    solver = z3.Solver()
    solver.set("timeout", 5000)  # 5 second timeout

    # Add assume constraints
    for pred in contract.assume:
        try:
            z3_expr = expr_to_z3(pred, ctx)
            solver.add(z3_expr)
        except Exception:
            pass  # Skip unparseable expressions

    # Add guarantee constraints
    for pred in contract.guarantee:
        try:
            z3_expr = expr_to_z3(pred, ctx)
            solver.add(z3_expr)
        except Exception:
            pass

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
    for pred in c1.assume + c1.guarantee + c2.assume + c2.guarantee:
        try:
            z3_expr = expr_to_z3(pred, ctx)
            solver.add(z3_expr)
        except Exception:
            pass

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
        return expr_to_z3(expr, ctx)
    except Exception:
        # Fallback: treat as opaque boolean
        sanitized = cond_str.replace(" ", "_")[:40]
        sanitized = "".join(c for c in sanitized if c.isalnum() or c == "_")
        return ctx.get_bool(f"cond_{sanitized}")


def verify(sos: SoSDefinition) -> List[VerificationResult]:
    """Run all SMT verification checks on a CADL SoS definition.

    Returns a list of VerificationResult objects.
    """
    results: List[VerificationResult] = []

    # 1. Check each contract for internal consistency
    for contract in sos.contracts:
        results.append(_check_contract_consistency(contract))

    # 2. Check cross-contract consistency for contracts sharing parties
    for c1, c2 in combinations(sos.contracts, 2):
        parties1 = {p.name for p in c1.parties}
        parties2 = {p.name for p in c2.parties}
        if parties1 & parties2:  # shared parties
            results.append(_check_cross_contract_consistency(c1, c2))

    # 3. Check transition condition mutual exclusivity
    results.extend(_check_transition_exclusivity(sos))

    return results
