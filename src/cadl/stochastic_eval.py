"""Internal reference evaluator for the stochastic model extension.

Appendix H says the reference implementation runs no model checker: the
properties and regime map queries of a `model:` block are checked
statically and exported to PRISM. This module is a small *reference
evaluator*, meant for checking the export and for tests, not a model
checker: it builds the Markov chain M(D, θ) of H.7 for one design with
every parameter fixed, by explicit enumeration, and evaluates the
properties of H.8 with exact rational arithmetic (`fractions.Fraction`).

For a regime map it evaluates the objective and the constraint at sample
points only. That gives exact values at those points and nothing about
the cells between them, so it is never a certified regime map (H.9).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .stochastic import (
    STATE_SPACE_LIMIT,
    Design,
    Model,
    PropertyExpr,
    RegimeMapQuery,
    compile_state_expr,
    decimal_text,
    eval_param_expr,
    ill_defined,
    update_probabilities,
)

# The value of an expected reward when the target is not reached with
# probability 1 (H.8). It compares correctly with a Fraction.
INFINITY = math.inf

# Unbounded properties are solved by Gaussian elimination over Fractions;
# this evaluator refuses a linear system with more unknowns.
LINEAR_SYSTEM_LIMIT = 2_000
# Sample points of one `--regime-map` evaluation.
GRID_POINT_LIMIT = 10_000

NOTE = ("internal reference evaluator of cadl (explicit enumeration, exact "
        "rational arithmetic); not a model checker")
ESTIMATE_NOTE = "estimated at sample points; not a certified regime map"


class EvaluationError(ValueError):
    """The evaluator cannot evaluate (a limit, or a design that is not
    well-defined at the valuation)."""


@dataclass
class Chain:
    """M(D, θ) restricted to the states reachable from the initial state."""
    model: Model
    design: str
    theta: dict
    states: list = field(default_factory=list)       # state tuples; 0 is initial
    succ: list = field(default_factory=list)         # per state: {state index: probability}
    action: list = field(default_factory=list)       # per state: action label taken, or None
    labels: dict = field(default_factory=dict)       # compiled label predicates
    index: dict = field(default_factory=dict)        # variable -> position


def build_chain(model: Model, design: Design, theta: dict) -> Chain:
    """The regime model of a design at a valuation (H.7), by enumeration
    from the initial state."""
    size = 1
    for v in model.variables:
        size *= len(v.domain)
    if size > STATE_SPACE_LIMIT:
        raise EvaluationError(
            f"the model has {size} states; the limit of this processor is "
            f"{STATE_SPACE_LIMIT}")
    why = ill_defined(model, design.name, theta)
    if why:
        raise EvaluationError(f"the design {design.name} is not well-defined here: {why}")
    index = model.var_index()
    labels: dict = {}
    for name, (tree, _) in model.labels.items():
        labels[name] = compile_state_expr(tree, index, {})
    commands = []
    for c in model.commands:
        if not c.exists_in(design.name):
            continue
        probs = update_probabilities(c, theta)
        updates = [
            (p, [(index[name], compile_state_expr(tree, index, {})) for name, tree, _ in u.sets])
            for u, p in zip(c.updates, probs)
        ]
        commands.append((compile_state_expr(c.guard, index, {}), c.action, updates))
    chain = Chain(model=model, design=design.name, theta=theta, labels=labels, index=index)
    start = model.initial_state()
    number = {start: 0}
    chain.states.append(start)
    chain.succ.append(None)
    chain.action.append(None)
    todo = [start]
    while todo:
        state = todo.pop()
        out: dict = {}
        action = None
        taken = False
        for guard, act, updates in commands:
            if not guard(state):
                continue
            # S-9: at most one command is enabled.
            taken = True
            action = act
            for p, sets in updates:
                if p == 0:
                    continue
                nxt = list(state)
                for i, fn in sets:
                    nxt[i] = fn(state)
                nxt_t = tuple(nxt)
                if nxt_t not in number:
                    number[nxt_t] = len(chain.states)
                    chain.states.append(nxt_t)
                    chain.succ.append(None)
                    chain.action.append(None)
                    todo.append(nxt_t)
                out[number[nxt_t]] = out.get(number[nxt_t], Fraction(0)) + p
            break
        i = number[state]
        if not taken:
            out = {i: Fraction(1)}     # deadlock: stays with probability 1 (H.5)
        chain.succ[i] = out
        chain.action[i] = action
    return chain


def _sat(chain: Chain, tree: tuple) -> list:
    fn = compile_state_expr(tree, chain.index, chain.labels)
    return [bool(fn(s)) for s in chain.states]


def _solve(rows: dict, rhs: dict) -> dict:
    """Solve x_i = rhs_i + sum_j rows[i][j] * x_j exactly (Gaussian
    elimination over Fractions). The system must have a unique solution."""
    unknowns = sorted(rows)
    if len(unknowns) > LINEAR_SYSTEM_LIMIT:
        raise EvaluationError(
            f"the linear system has {len(unknowns)} unknowns; the limit of this "
            f"reference evaluator is {LINEAR_SYSTEM_LIMIT}")
    pos = {u: k for k, u in enumerate(unknowns)}
    n = len(unknowns)
    # (I - A) x = b
    a = [[Fraction(0)] * n for _ in range(n)]
    b = [Fraction(0)] * n
    for u in unknowns:
        k = pos[u]
        a[k][k] += 1
        for j, p in rows[u].items():
            a[k][pos[j]] -= p
        b[k] = Fraction(rhs[u])
    for col in range(n):
        pivot = next((r for r in range(col, n) if a[r][col] != 0), None)
        if pivot is None:
            raise EvaluationError("internal error: singular linear system")
        a[col], a[pivot] = a[pivot], a[col]
        b[col], b[pivot] = b[pivot], b[col]
        inv = 1 / a[col][col]
        for r in range(col + 1, n):
            f = a[r][col]
            if f == 0:
                continue
            f *= inv
            row, prow = a[r], a[col]
            for c in range(col, n):
                if prow[c] != 0:
                    row[c] -= f * prow[c]
            b[r] -= f * b[col]
    x = [Fraction(0)] * n
    for r in range(n - 1, -1, -1):
        total = b[r]
        row = a[r]
        for c in range(r + 1, n):
            if row[c] != 0:
                total -= row[c] * x[c]
        x[r] = total / row[r]
    return {u: x[pos[u]] for u in unknowns}


def _until_probabilities(chain: Chain, phi1: list, phi2: list, bound: int | None) -> list:
    """Per state, the probability of `phi1 U phi2` (bounded when given)."""
    n = len(chain.states)
    if bound is not None:
        x = [Fraction(1) if phi2[i] else Fraction(0) for i in range(n)]
        for _ in range(bound):
            x = [
                Fraction(1) if phi2[i] else Fraction(0) if not phi1[i]
                else sum((p * x[j] for j, p in chain.succ[i].items()), Fraction(0))
                for i in range(n)
            ]
        return x
    # States from which phi2 can be reached through phi1-states.
    pred: list = [[] for _ in range(n)]
    for i in range(n):
        if phi1[i] and not phi2[i]:
            for j in chain.succ[i]:
                pred[j].append(i)
    can = [bool(phi2[i]) for i in range(n)]
    todo = [i for i in range(n) if can[i]]
    while todo:
        j = todo.pop()
        for i in pred[j]:
            if not can[i]:
                can[i] = True
                todo.append(i)
    x = [Fraction(1) if phi2[i] else Fraction(0) for i in range(n)]
    maybe = [i for i in range(n) if can[i] and not phi2[i]]
    maybe_set = set(maybe)
    rows = {}
    rhs = {}
    for i in maybe:
        rows[i] = {j: p for j, p in chain.succ[i].items() if j in maybe_set}
        rhs[i] = sum((p for j, p in chain.succ[i].items() if phi2[j]), Fraction(0))
    for i, v in _solve(rows, rhs).items():
        x[i] = v
    return x


def _step_rewards(chain: Chain, reward_id: str) -> list:
    """Per state, the reward of the step taken from it (H.8)."""
    structure = next(r for r in chain.model.rewards if r.id == reward_id)
    out = [Fraction(0)] * len(chain.states)
    for item in structure.items:
        value = eval_param_expr(item.value, chain.theta)
        guard = compile_state_expr(item.guard, chain.index, chain.labels)
        for i, s in enumerate(chain.states):
            if item.action is not None and chain.action[i] != item.action:
                continue
            if guard(s):
                out[i] += value
    return out


def evaluate(chain: Chain, prop: PropertyExpr) -> Any:
    """The value of the quantity of a property in the initial state: a
    Fraction, or INFINITY for an expected reward whose target is not
    reached with probability 1."""
    n = len(chain.states)
    phi2 = _sat(chain, prop.right)
    if prop.quantity == "P":
        phi1 = [True] * n if prop.path == "F" else _sat(chain, prop.left)
        return _until_probabilities(chain, phi1, phi2, prop.bound)[0]
    reach = _until_probabilities(chain, [True] * n, phi2, None)
    if reach[0] != 1:
        return INFINITY
    rew = _step_rewards(chain, prop.reward)
    # States visited before the target is first reached; from each of
    # them the target is reached with probability 1.
    seen = set()
    todo = [0]
    while todo:
        i = todo.pop()
        if i in seen or phi2[i]:
            continue
        seen.add(i)
        todo.extend(chain.succ[i])
    if not seen:
        return Fraction(0)
    rows = {i: {j: p for j, p in chain.succ[i].items() if j in seen} for i in seen}
    return _solve(rows, {i: rew[i] for i in seen})[0]


def verdict(prop: PropertyExpr, value: Any) -> str | None:
    """`satisfied` or `violated` for a threshold property; None for a query."""
    if prop.is_query:
        return None
    b = prop.threshold
    holds = {">=": value >= b, ">": value > b, "<=": value <= b, "<": value < b}[prop.op]
    return "satisfied" if holds else "violated"


def value_text(value: Any, digits: int = 6) -> str:
    """A value rounded to a number of decimals; `+inf` for INFINITY."""
    if value == INFINITY:
        return "+inf"
    scaled = round(value * 10 ** digits)
    text = decimal_text(Fraction(scaled, 10 ** digits))
    return text


def exact_text(value: Any) -> str:
    if value == INFINITY:
        return "+inf"
    return str(value)


def parse_number(text: str) -> Fraction:
    """A number of `--set`: a decimal or a fraction `n/d`, read exactly."""
    try:
        return Fraction(text.strip())
    except (ValueError, ZeroDivisionError) as e:
        raise EvaluationError(f"'{text}' is not a number (a decimal or n/d)") from e


def valuation(model: Model, design: Design, overrides: dict | None = None) -> dict:
    """The fixed values under a design, with overrides (which must name
    declared parameters and lie in their declared range)."""
    theta = model.fixed_values(design)
    known = {p.id: p for p in model.params}
    for name, value in (overrides or {}).items():
        if name not in known:
            raise EvaluationError(f"'{name}' is not a parameter of the model")
        p = known[name]
        if not p.lo <= value <= p.hi:
            raise EvaluationError(
                f"the value {decimal_text(value)} of '{name}' is outside its declared "
                f"range [{decimal_text(p.lo)}, {decimal_text(p.hi)}]")
        theta[name] = value
    return theta


def _design(model: Model, name: str) -> Design:
    d = model.design(name)
    if d is None:
        raise EvaluationError(
            f"'{name}' is not a design of the file "
            f"(designs: {', '.join(x.name for x in model.designs)})")
    return d


def check_properties(model: Model, regime: str | None = None,
                     overrides: dict | None = None) -> list:
    """Evaluate every entry of `model.properties:` per design it concerns.

    Returns, per design, a dict with `design`, `parameters` (the valuation)
    and `properties`: entries with `id`, `expr`, `value` (Fraction or
    INFINITY) and `result` (`satisfied`, `violated`, or None for a query).
    """
    designs = [_design(model, regime)] if regime is not None else model.designs
    out = []
    for d in designs:
        theta = valuation(model, d, overrides)
        chain = build_chain(model, d, theta)
        results = []
        for p in model.properties:
            if d.name not in p.regimes:
                continue
            value = evaluate(chain, p.expr)
            results.append({"id": p.id, "expr": p.text, "value": value,
                            "result": verdict(p.expr, value)})
        out.append({"design": d.name, "parameters": theta,
                    "states": len(chain.states), "properties": results})
    return out


def evaluate_map_point(model: Model, query: RegimeMapQuery, point: dict,
                       overrides: dict | None = None) -> dict:
    """The objective and the constraint of a regime map query for each
    compared regime at one valuation of the open parameters, and opt(v)
    of H.9. Exact at this point; it says nothing about any cell."""
    regimes = {}
    for name in query.regimes:
        d = _design(model, name)
        theta = valuation(model, d, overrides)
        theta.update(point)
        chain = build_chain(model, d, theta)
        value = evaluate(chain, query.objective)
        sat = True
        if query.constraint is not None:
            sat = verdict(query.constraint, evaluate(chain, query.constraint)) == "satisfied"
        constraint_ok = sat
        if query.direction == "minimize" and value == INFINITY:
            sat = False
        regimes[name] = {"value": value, "constraint": constraint_ok, "sat": sat}
    feasible = [n for n in query.regimes if regimes[n]["sat"]]
    best = []
    if feasible:
        values = [regimes[n]["value"] for n in feasible]
        top = max(values) if query.direction == "maximize" else min(values)
        best = [n for n in feasible if regimes[n]["value"] == top]
    return {"point": dict(point), "regimes": regimes, "best": best}


def regime_map_estimate(model: Model, map_id: str, grid: int,
                        overrides: dict | None = None) -> dict:
    """Point evaluations of a regime map query at the corner points of a
    uniform grid with `grid` intervals per open parameter.

    These are estimates in the sense of H.9: claims about the sample
    points only. No cell is decided.
    """
    query = next((q for q in model.regime_maps if q.id == map_id), None)
    if query is None:
        raise EvaluationError(
            f"'{map_id}' is not a regime map of the model "
            f"(regime maps: {', '.join(q.id for q in model.regime_maps) or 'none'})")
    if grid < 1:
        raise EvaluationError("--grid must be at least 1")
    names = list(query.over)
    if (grid + 1) ** len(names) > GRID_POINT_LIMIT:
        raise EvaluationError(
            f"a grid of {grid} intervals over {len(names)} parameter(s) has "
            f"{(grid + 1) ** len(names)} points; the limit is {GRID_POINT_LIMIT}")
    for name in overrides or {}:
        if name in query.over:
            raise EvaluationError(f"'{name}' is open in the regime map and cannot be set")
    axes = [
        [lo + (hi - lo) * Fraction(i, grid) for i in range(grid + 1)]
        for lo, hi in (query.over[n] for n in names)
    ]
    points = [
        evaluate_map_point(model, query, dict(zip(names, combo)), overrides)
        for combo in itertools.product(*axes)
    ]
    return {
        "id": query.id,
        "note": ESTIMATE_NOTE,
        "direction": query.direction,
        "objective": query.objective_text,
        "constraint": query.constraint_text,
        "open": names,
        "regimes": list(query.regimes),
        "grid": grid,
        "points": points,
    }
