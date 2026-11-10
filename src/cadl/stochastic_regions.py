"""Certified regime maps for the stochastic model extension (Appendix H, H.9).

A regime map is a set of cells (boxes of the open parameters) with a
status. `decided` and `infeasible` are claims about every valuation of a
cell, so they need a method that is sound for the whole cell. This module
implements one, with exact rational arithmetic (`fractions.Fraction`) and
no external tool: **parameter lifting** (Quatmann et al., ATVA 2016).

For one design and one cell, the parametric Markov chain of H.7 is relaxed
to a Markov decision process (MDP): every state chooses, independently of
the other states, a corner of the cell for the open parameters that occur
in its outgoing probabilities. When those probabilities are multi-affine
in the open parameters (degree at most one in each), the distribution of
a state at any valuation of the cell is a convex combination of its
distributions at the corners, so the chain at that valuation is the MDP
under one randomised memoryless policy. The minimal and the maximal value
of the MDP therefore enclose the solution function on the whole cell.

A state whose probabilities are not multi-affine gets another sound
relaxation: exact interval arithmetic encloses each probability on the
cell, and the state chooses a vertex of the polytope of distributions
within those intervals. Such a design counts as well-defined on a cell
only when the intervals prove it; otherwise the cell stays `undecided`.

The MDPs are solved exactly: bounded properties by backward induction,
unbounded ones by policy iteration with exact linear solves. Cells that
are not graph-preserving (H.5) are handled soundly, not declined: the
probability bounds hold for any MDP; an expected reward gets a finite upper
bound only when every policy reaches the target with probability 1, and
its lower bound is the least expected reward of any policy.

A cell that the bounds do not decide is bisected, until the undecided
fraction is at most `resolution:` or a budget is exhausted.
"""

from __future__ import annotations

import heapq
import itertools
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
)
from .stochastic_eval import INFINITY, EvaluationError, _design, _solve, valuation

METHOD = ("parameter lifting: each design is relaxed on a cell to a Markov decision "
          "process over the corners of the cell, solved with exact rational arithmetic")
CERTIFIED_NOTE = ("certified regime map: 'decided' and 'infeasible' hold at every "
                  "valuation of the cell")

DEFAULT_MAX_DEPTH = 24
DEFAULT_MAX_CELLS = 20_000
# A state whose probabilities are not multi-affine chooses a vertex of an
# interval polytope; with more successors than this the design is not
# analysed on the cell.
POLYTOPE_SUCCESSOR_LIMIT = 6

ZERO = Fraction(0)
ONE = Fraction(1)


# ---------------------------------------------------------------------------
# Polynomials in the open parameters
# ---------------------------------------------------------------------------
# A polynomial is a dict from an exponent tuple (one entry per open
# parameter) to a non-zero Fraction.

def _p_add(a: dict, b: dict, sign: int = 1) -> dict:
    out = dict(a)
    for e, c in b.items():
        v = out.get(e, ZERO) + sign * c
        if v == 0:
            out.pop(e, None)
        else:
            out[e] = v
    return out


def _p_mul(a: dict, b: dict) -> dict:
    out: dict = {}
    for e1, c1 in a.items():
        for e2, c2 in b.items():
            e = tuple(x + y for x, y in zip(e1, e2))
            v = out.get(e, ZERO) + c1 * c2
            if v == 0:
                out.pop(e, None)
            else:
                out[e] = v
    return out


def polynomial(tree: tuple, theta: dict, names: list) -> dict:
    """A `param_expr` as a polynomial in the parameters `names`; every
    other parameter has its value in `theta`."""
    kind = tree[0]
    zero = (0,) * len(names)
    if kind == "num":
        return {zero: tree[1]} if tree[1] != 0 else {}
    if kind == "id":
        if tree[1] in names:
            e = [0] * len(names)
            e[names.index(tree[1])] = 1
            return {tuple(e): ONE}
        value = theta[tree[1]]
        return {zero: value} if value != 0 else {}
    if kind == "par":
        return polynomial(tree[1], theta, names)
    if kind == "neg":
        return _p_add({}, polynomial(tree[1], theta, names), -1)
    left = polynomial(tree[2], theta, names)
    right = polynomial(tree[3], theta, names)
    if tree[1] == "+":
        return _p_add(left, right)
    if tree[1] == "-":
        return _p_add(left, right, -1)
    return _p_mul(left, right)


def _p_eval(poly: tuple, point: tuple) -> Fraction:
    total = ZERO
    for exps, c in poly:
        for x, e in zip(point, exps):
            if e:
                c = c * x ** e
        total += c
    return total


def _p_interval(poly: tuple, box: tuple) -> tuple:
    """A sound enclosure of a polynomial on a box, by exact interval
    arithmetic on its monomials."""
    lo = hi = ZERO
    for exps, c in poly:
        m_lo = m_hi = c
        for (a, b), e in zip(box, exps):
            if not e:
                continue
            if e % 2 or a >= 0:
                f_lo, f_hi = a ** e, b ** e
            elif b <= 0:
                f_lo, f_hi = b ** e, a ** e
            else:
                f_lo, f_hi = ZERO, max(a ** e, b ** e)
            products = (m_lo * f_lo, m_lo * f_hi, m_hi * f_lo, m_hi * f_hi)
            m_lo, m_hi = min(products), max(products)
        lo += m_lo
        hi += m_hi
    return lo, hi


def _canonical(poly: dict) -> tuple:
    return tuple(sorted(poly.items()))


def _multi_affine(poly: tuple) -> bool:
    return all(e <= 1 for exps, _ in poly for e in exps)


# ---------------------------------------------------------------------------
# The parametric chain of one design
# ---------------------------------------------------------------------------

class _Unknown(Exception):
    """The design cannot be analysed on a cell; the reason is the message."""


@dataclass
class Row:
    """The outgoing probabilities of a state, one polynomial per successor."""
    polys: tuple = ()
    command: int = 0                   # index of the command (for messages)
    occurring: tuple = ()              # positions of the open parameters that occur
    multi_affine: bool = True


@dataclass
class ParametricChain:
    """The regime model of a design with the open parameters left open,
    restricted to the states that some valuation can reach."""
    model: Model
    design: str
    names: list                                      # the open parameters
    theta: dict                                      # the values of the others
    states: list = field(default_factory=list)       # 0 is the initial state
    targets: list = field(default_factory=list)      # per state: successor indices
    row_of: list = field(default_factory=list)       # per state: index into rows, or None (deadlock)
    rows: list = field(default_factory=list)
    action: list = field(default_factory=list)       # per state: action label taken, or None
    labels: dict = field(default_factory=dict)
    index: dict = field(default_factory=dict)
    # Per command with a probability that is not multi-affine: (index, polynomials).
    non_affine: list = field(default_factory=list)
    _sat: dict = field(default_factory=dict)
    _rew: dict = field(default_factory=dict)

    def sat(self, tree: tuple) -> list:
        key = repr(tree)
        if key not in self._sat:
            fn = compile_state_expr(tree, self.index, self.labels)
            self._sat[key] = [bool(fn(s)) for s in self.states]
        return self._sat[key]

    def step_rewards(self, reward_id: str) -> list:
        """Per state, the reward of the step taken from it (H.8). S-12
        keeps the open parameters out of reward values."""
        if reward_id not in self._rew:
            structure = next(r for r in self.model.rewards if r.id == reward_id)
            out = [ZERO] * len(self.states)
            for item in structure.items:
                value = eval_param_expr(item.value, self.theta)
                guard = compile_state_expr(item.guard, self.index, self.labels)
                for i, s in enumerate(self.states):
                    if item.action is not None and self.action[i] != item.action:
                        continue
                    if guard(s):
                        out[i] += value
            self._rew[reward_id] = out
        return self._rew[reward_id]

    def signature(self) -> tuple:
        """Two designs with the same signature have the same solution
        functions: the same chain as a function of the open parameters,
        and the same rewards."""
        rows = tuple((r.polys) for r in self.rows)
        rewards = tuple((r.id, tuple(self.step_rewards(r.id))) for r in self.model.rewards)
        non_affine = tuple(tuple(polys) for _, polys in self.non_affine)
        return (tuple(self.states), tuple(map(tuple, self.targets)), tuple(self.row_of),
                rows, rewards, non_affine)


def build_parametric_chain(model: Model, design: Design, names: list,
                           theta: dict) -> ParametricChain:
    """Enumerate the states that are reachable through transitions whose
    probability is not the zero polynomial."""
    size = 1
    for v in model.variables:
        size *= len(v.domain)
    if size > STATE_SPACE_LIMIT:
        raise EvaluationError(
            f"the model has {size} states; the limit of this processor is "
            f"{STATE_SPACE_LIMIT}")
    for r in model.rewards:
        for item in r.items:
            used = _names_of(item.value) & set(names)
            if used:
                raise EvaluationError(
                    f"the value '{item.value_text}' of reward '{r.id}' mentions the open "
                    f"parameter '{sorted(used)[0]}' (S-12)")
    index = model.var_index()
    labels: dict = {}
    for name, (tree, _) in model.labels.items():
        labels[name] = compile_state_expr(tree, index, {})
    chain = ParametricChain(model=model, design=design.name, names=list(names),
                            theta=theta, labels=labels, index=index)
    zero = (0,) * len(names)
    commands = []
    for c in model.commands:
        if not c.exists_in(design.name):
            continue
        polys = [None if u.prob is None else polynomial(u.prob, theta, names)
                 for u in c.updates]
        others: dict = {}
        for p in polys:
            if p is not None:
                others = _p_add(others, p)
        polys = [_p_add({zero: ONE}, others, -1) if p is None else p for p in polys]
        canon = [_canonical(p) for p in polys]
        if not all(_multi_affine(p) for p in canon):
            chain.non_affine.append((c.index, canon))
        updates = [
            (p, [(index[name], compile_state_expr(tree, index, {})) for name, tree, _ in u.sets])
            for u, p in zip(c.updates, polys)
        ]
        commands.append((compile_state_expr(c.guard, index, {}), c, updates))
    row_ids: dict = {}
    start = model.initial_state()
    number = {start: 0}
    chain.states.append(start)
    todo = [start]
    targets: dict = {}
    row_of: dict = {}
    action: dict = {}
    while todo:
        state = todo.pop()
        i = number[state]
        targets[i], row_of[i], action[i] = [i], None, None   # deadlock (H.5)
        for guard, c, updates in commands:
            if not guard(state):
                continue
            # S-9: at most one command is enabled.
            out: dict = {}
            for p, sets in updates:
                nxt = list(state)
                for k, fn in sets:
                    nxt[k] = fn(state)
                nxt_t = tuple(nxt)
                out[nxt_t] = _p_add(out.get(nxt_t, {}), p)
            succ, polys = [], []
            for nxt_t, p in out.items():
                if not p:
                    continue           # the zero polynomial: no transition
                if nxt_t not in number:
                    number[nxt_t] = len(chain.states)
                    chain.states.append(nxt_t)
                    todo.append(nxt_t)
                succ.append(number[nxt_t])
                polys.append(_canonical(p))
            key = tuple(polys)
            if key not in row_ids:
                row_ids[key] = len(chain.rows)
                occurring = tuple(k for k in range(len(names))
                                  if any(exps[k] for p in key for exps, _ in p))
                chain.rows.append(Row(polys=key, command=c.index, occurring=occurring,
                                      multi_affine=all(_multi_affine(p) for p in key)))
            targets[i], row_of[i], action[i] = succ, row_ids[key], c.action
            break
    n = len(chain.states)
    chain.targets = [targets[i] for i in range(n)]
    chain.row_of = [row_of[i] for i in range(n)]
    chain.action = [action[i] for i in range(n)]
    return chain


def _names_of(tree: tuple) -> set:
    kind = tree[0]
    if kind == "id":
        return {tree[1]}
    if kind in ("par", "neg"):
        return _names_of(tree[1])
    if kind == "bin":
        return _names_of(tree[2]) | _names_of(tree[3])
    return set()


# ---------------------------------------------------------------------------
# The relaxation of a chain on a cell
# ---------------------------------------------------------------------------

def _polytope_vertices(intervals: list) -> list:
    """The vertices of {d : lo_k <= d_k <= hi_k, sum d = 1}."""
    n = len(intervals)
    out = []
    seen = set()
    for free in range(n):
        rest = [k for k in range(n) if k != free]
        for combo in itertools.product((0, 1), repeat=n - 1):
            d = [ZERO] * n
            for k, side in zip(rest, combo):
                d[k] = intervals[k][side]
            d[free] = ONE - sum(d, ZERO)
            if intervals[free][0] <= d[free] <= intervals[free][1]:
                t = tuple(d)
                if t not in seen:
                    seen.add(t)
                    out.append(t)
    return out


def _row_actions(chain: ParametricChain, row: Row, box: tuple) -> list:
    """The distributions a state with this row may choose on the cell:
    probability tuples aligned with the successors of the state."""
    if row.multi_affine:
        out = []
        seen = set()
        for corner in itertools.product(*[(0, 1)] * len(row.occurring)):
            point = [b[0] for b in box]
            for k, side in zip(row.occurring, corner):
                point[k] = box[k][side]
            values = tuple(_p_eval(p, point) for p in row.polys)
            if any(not 0 <= v <= 1 for v in values):
                at = ", ".join(f"{n}={decimal_text(x)}" for n, x in zip(chain.names, point))
                raise EvaluationError(
                    f"the design {chain.design} is not well-defined at {at}: a probability "
                    f"of command {row.command} is outside [0, 1]")
            if values not in seen:
                seen.add(values)
                out.append(values)
        return out
    if len(row.polys) > POLYTOPE_SUCCESSOR_LIMIT:
        raise _Unknown(
            f"command {row.command} of design {chain.design} has probabilities that are "
            f"not multi-affine in the open parameters and more than "
            f"{POLYTOPE_SUCCESSOR_LIMIT} successors")
    intervals = []
    for p in row.polys:
        lo, hi = _p_interval(p, box)
        # The design is well-defined on the cell (checked by the caller),
        # so the true values also lie in [0, 1].
        intervals.append((max(lo, ZERO), min(hi, ONE)))
    return _polytope_vertices(intervals)


def relax(chain: ParametricChain, box: dict) -> list:
    """The MDP of a design on a cell: per state, a list of actions, each a
    dict from a successor to a positive probability. Raises `_Unknown`
    when the design cannot be analysed on the cell."""
    cell = tuple(box[n] for n in chain.names)
    for index, polys in chain.non_affine:
        for p in polys:
            lo, hi = _p_interval(p, cell)
            if lo < 0 or hi > 1:
                # The enclosure proves nothing here. A value outside [0, 1]
                # at the centre or a corner of the cell shows that the map
                # violates S-14, which is an error of the file and not an
                # undecided cell.
                centre = tuple((b[0] + b[1]) / 2 for b in cell)
                for point in (centre,) + tuple(itertools.product(*cell)):
                    if not 0 <= _p_eval(p, point) <= 1:
                        at = ", ".join(f"{n}={decimal_text(x)}"
                                       for n, x in zip(chain.names, point))
                        raise EvaluationError(
                            f"the design {chain.design} is not well-defined at {at}: a "
                            f"probability of command {index} is outside [0, 1] (S-14)")
                raise _Unknown(
                    f"command {index} of design {chain.design} has a probability that is "
                    f"not multi-affine in the open parameters, and interval arithmetic "
                    f"does not establish that the design is well-defined on the cell")
    per_row = [_row_actions(chain, row, cell) for row in chain.rows]
    acts = []
    for i, targets in enumerate(chain.targets):
        r = chain.row_of[i]
        if r is None:
            acts.append([{i: ONE}])
            continue
        acts.append([{j: p for j, p in zip(targets, values) if p != 0}
                     for values in per_row[r]])
    return acts


# ---------------------------------------------------------------------------
# Exact solution of the MDP
# ---------------------------------------------------------------------------

def _value(action: dict, x: list) -> Fraction:
    total = ZERO
    for j, p in action.items():
        if x[j]:
            total += p * x[j]
    return total


def bounded_until(acts: list, phi1: list, phi2: list, bound: int) -> tuple:
    """Minimal and maximal probability of `phi1 U<=bound phi2` in the
    initial state, by backward induction."""
    n = len(acts)
    lo = [ONE if phi2[i] else ZERO for i in range(n)]
    hi = list(lo)
    for _ in range(bound):
        new_lo, new_hi = list(lo), list(hi)
        for i in range(n):
            if phi2[i] or not phi1[i]:
                continue
            new_lo[i] = min(_value(a, lo) for a in acts[i])
            new_hi[i] = max(_value(a, hi) for a in acts[i])
        lo, hi = new_lo, new_hi
    return lo[0], hi[0]


def _policy_until(succ: list, open_: list, phi2: list) -> list:
    """Per state, the probability of reaching phi2 through open states in
    the Markov chain `succ` (the least solution)."""
    n = len(succ)
    pred: list = [[] for _ in range(n)]
    for i in range(n):
        if open_[i]:
            for j in succ[i]:
                pred[j].append(i)
    can = [bool(phi2[i]) for i in range(n)]
    todo = [i for i in range(n) if can[i]]
    while todo:
        j = todo.pop()
        for i in pred[j]:
            if not can[i]:
                can[i] = True
                todo.append(i)
    x = [ONE if phi2[i] else ZERO for i in range(n)]
    maybe = {i for i in range(n) if open_[i] and can[i]}
    if maybe:
        rows = {i: {j: p for j, p in succ[i].items() if j in maybe} for i in maybe}
        rhs = {i: sum((p for j, p in succ[i].items() if phi2[j]), ZERO) for i in maybe}
        for i, v in _solve(rows, rhs).items():
            x[i] = v
    return x


def max_until(acts: list, phi1: list, phi2: list) -> list:
    """Per state, the maximal probability of `phi1 U phi2`: policy
    iteration that evaluates each policy by its least solution and
    switches an action only for a strict improvement."""
    n = len(acts)
    open_ = [bool(phi1[i]) and not phi2[i] for i in range(n)]
    policy = [0] * n
    while True:
        succ = [acts[i][policy[i]] if open_[i] else {} for i in range(n)]
        x = _policy_until(succ, open_, phi2)
        improved = False
        for i in range(n):
            if not open_[i] or len(acts[i]) < 2:
                continue
            best, best_v = policy[i], x[i]
            for k, a in enumerate(acts[i]):
                v = _value(a, x)
                if v > best_v:
                    best, best_v = k, v
            if best != policy[i]:
                policy[i] = best
                improved = True
        if not improved:
            return x


def min_until(acts: list, phi1: list, phi2: list) -> list:
    """Per state, the minimal probability of `phi1 U phi2`."""
    n = len(acts)
    open_ = [bool(phi1[i]) and not phi2[i] for i in range(n)]
    # The states from which some policy avoids phi2 for ever: the greatest
    # set of non-target states in which every open state has an action
    # that stays in the set.
    zero = {i for i in range(n) if not phi2[i]}
    changed = True
    while changed:
        changed = False
        for i in list(zero):
            if open_[i] and not any(all(j in zero for j in a) for a in acts[i]):
                zero.discard(i)
                changed = True
    x = [ONE if phi2[i] else ZERO for i in range(n)]
    unknown = [i for i in range(n) if open_[i] and i not in zero]
    if not unknown:
        return x
    inside = set(unknown)
    policy = {i: 0 for i in unknown}
    while True:
        rows = {i: {j: p for j, p in acts[i][policy[i]].items() if j in inside}
                for i in unknown}
        rhs = {i: sum((p for j, p in acts[i][policy[i]].items() if phi2[j]), ZERO)
               for i in unknown}
        for i, v in _solve(rows, rhs).items():
            x[i] = v
        improved = False
        for i in unknown:
            best, best_v = policy[i], x[i]
            for k, a in enumerate(acts[i]):
                v = _value(a, x)
                if v < best_v:
                    best, best_v = k, v
            if best != policy[i]:
                policy[i] = best
                improved = True
        if not improved:
            return x


def _total_reward(acts: list, inside: set, rew: list) -> Fraction:
    """The maximal expected reward accumulated in the states `inside`
    from the initial state, when every policy leaves them with
    probability 1 (so every policy has a unique finite value)."""
    order = sorted(inside)
    policy = {i: 0 for i in order}
    while True:
        rows = {i: {j: p for j, p in acts[i][policy[i]].items() if j in inside}
                for i in order}
        x = _solve(rows, {i: rew[i] for i in order})
        improved = False
        for i in order:
            best, best_v = policy[i], x[i]
            for k, a in enumerate(acts[i]):
                v = rew[i] + sum((p * x[j] for j, p in a.items() if j in inside), ZERO)
                if v > best_v:
                    best, best_v = k, v
            if best != policy[i]:
                policy[i] = best
                improved = True
        if not improved:
            return x[0]


def min_total_reward(acts: list, phi2: list, rew: list) -> Any:
    """The least expected reward that a policy accumulates before phi2,
    in the initial state, where a policy that never reaches phi2 simply
    keeps what it has accumulated: the least fixed point of the Bellman
    operator. A Fraction, or INFINITY when every policy accumulates an
    infinite expected reward. Rewards are not negative.

    This is a lower bound of the expected reward of H.8 under every
    policy, also for an MDP in which some policies miss phi2."""
    n = len(acts)
    # The states with value 0: the greatest set in which every state is
    # a target, or earns nothing and has an action that stays in the set.
    zero = {i for i in range(n) if phi2[i] or rew[i] == 0}
    changed = True
    while changed:
        changed = False
        for i in sorted(zero):
            if not phi2[i] and not any(all(j in zero for j in a) for a in acts[i]):
                zero.discard(i)
                changed = True
    # The states with a finite value: those with a policy that reaches
    # `zero` with probability 1. `choice` is such a policy.
    finite = set(range(n))
    while True:
        reach = set(zero)
        choice: dict = {}
        grew = True
        while grew:
            grew = False
            for i in sorted(finite - reach):
                for k, a in enumerate(acts[i]):
                    if all(j in finite for j in a) and any(j in reach for j in a):
                        reach.add(i)
                        choice[i] = k
                        grew = True
                        break
        if reach == finite:
            break
        finite = reach
    if 0 not in finite:
        return INFINITY
    if 0 in zero:
        return ZERO
    # Policy iteration from a policy that reaches `zero` with probability
    # 1; a policy that does not has an infinite value somewhere, so a
    # strict improvement never leads to one.
    order = sorted(finite - zero)
    inside = set(order)
    allowed = {i: [k for k, a in enumerate(acts[i]) if all(j in finite for j in a)]
               for i in order}
    policy = dict(choice)
    while True:
        rows = {i: {j: p for j, p in acts[i][policy[i]].items() if j in inside}
                for i in order}
        x = _solve(rows, {i: rew[i] for i in order})
        improved = False
        for i in order:
            best, best_v = policy[i], x[i]
            for k in allowed[i]:
                v = rew[i] + sum((p * x[j] for j, p in acts[i][k].items() if j in inside),
                                 ZERO)
                if v < best_v:
                    best, best_v = k, v
            if best != policy[i]:
                policy[i] = best
                improved = True
        if not improved:
            return x[0]


def reward_bounds(acts: list, phi2: list, rew: list) -> tuple:
    """Bounds of the expected reward accumulated until phi2 (H.8: +inf
    unless phi2 is reached with probability 1)."""
    n = len(acts)
    everywhere = [True] * n
    if min_until(acts, everywhere, phi2)[0] == 1:
        # Every policy reaches the target with probability 1 from every
        # state that can be visited before it, so every policy has a
        # finite value.
        inside: set = set()
        todo = [0]
        while todo:
            i = todo.pop()
            if i in inside or phi2[i]:
                continue
            inside.add(i)
            for a in acts[i]:
                todo.extend(a)
        if not inside:
            return ZERO, ZERO
        hi = _total_reward(acts, inside, rew)
    else:
        # Some policy misses the target with positive probability: no
        # finite upper bound is claimed. If every policy does, the value
        # is +inf on the whole cell.
        hi = INFINITY
        if max_until(acts, everywhere, phi2)[0] < 1:
            return INFINITY, INFINITY
    return min_total_reward(acts, phi2, rew), hi


def property_bounds_in(chain: ParametricChain, acts: list, prop: PropertyExpr) -> tuple:
    """Lower and upper bound of the quantity of a property over a cell,
    given the relaxation `acts` of the chain on that cell."""
    n = len(chain.states)
    phi2 = chain.sat(prop.right)
    if prop.quantity == "R":
        return reward_bounds(acts, phi2, chain.step_rewards(prop.reward))
    phi1 = [True] * n if prop.path == "F" else chain.sat(prop.left)
    if prop.bound is not None:
        return bounded_until(acts, phi1, phi2, prop.bound)
    return min_until(acts, phi1, phi2)[0], max_until(acts, phi1, phi2)[0]


# ---------------------------------------------------------------------------
# Bounds of one design on one cell (public, used by the tests)
# ---------------------------------------------------------------------------

def _query(model: Model, map_id: str) -> RegimeMapQuery:
    query = next((q for q in model.regime_maps if q.id == map_id), None)
    if query is None:
        raise EvaluationError(
            f"'{map_id}' is not a regime map of the model "
            f"(regime maps: {', '.join(q.id for q in model.regime_maps) or 'none'})")
    return query


def _check_box(query: RegimeMapQuery, box: dict) -> None:
    if set(box) != set(query.over):
        raise EvaluationError("a cell gives an interval to each open parameter of the map")
    for name, (lo, hi) in box.items():
        b_lo, b_hi = query.over[name]
        if not b_lo <= lo <= hi <= b_hi:
            raise EvaluationError(f"the interval of '{name}' is not inside the region")


def _chain_for(model: Model, query: RegimeMapQuery, regime: str,
               overrides: dict | None) -> ParametricChain:
    for name in overrides or {}:
        if name in query.over:
            raise EvaluationError(f"'{name}' is open in the regime map and cannot be set")
    design = _design(model, regime)
    theta = valuation(model, design, overrides)
    names = list(query.over)
    # Well-definedness at the corners of the region (S-14); this decides
    # the whole region for the commands with multi-affine probabilities.
    for corner in itertools.product(*[query.over[n] for n in names]):
        at = dict(theta)
        at.update(zip(names, corner))
        why = ill_defined(model, design.name, at)
        if why:
            where = ", ".join(f"{n}={decimal_text(x)}" for n, x in zip(names, corner))
            raise EvaluationError(
                f"the design {design.name} is not well-defined at {where}: {why}")
    return build_parametric_chain(model, design, names, theta)


def design_bounds(model: Model, map_id: str, regime: str, box: dict,
                  prop: PropertyExpr | None = None,
                  overrides: dict | None = None) -> tuple | None:
    """Certified bounds (lower, upper) of a property of one design over a
    cell of a regime map; by default the objective of the map. A bound is
    a Fraction or INFINITY. None when the design cannot be analysed on
    the cell."""
    query = _query(model, map_id)
    _check_box(query, box)
    chain = _chain_for(model, query, regime, overrides)
    try:
        acts = relax(chain, box)
    except _Unknown:
        return None
    return property_bounds_in(chain, acts, prop or query.objective)


def graph_preserving(acts: list) -> bool:
    """Whether the relaxation shows the cell to be graph-preserving for
    the design (H.5): every state has the same successors under each of
    its actions."""
    return all(len({frozenset(a) for a in state}) == 1 for state in acts)


def cell_bounds(model: Model, map_id: str, box: dict,
                overrides: dict | None = None) -> dict:
    """For each compared regime of a regime map, what the method
    establishes on one cell: `objective` and `constraint` are certified
    bounds `(lower, upper)` of the two quantities (`constraint` is None
    without a constraint), `holds` says whether the constraint holds on
    the whole cell (True), fails on the whole cell (False) or is open
    (None), and `graph_preserving` whether the cell is graph-preserving
    for the design. A regime that cannot be analysed on the cell maps to
    `{"reason": text}`."""
    query = _query(model, map_id)
    _check_box(query, box)
    out: dict = {}
    for r in query.regimes:
        chain = _chain_for(model, query, r, overrides)
        try:
            acts = relax(chain, box)
        except _Unknown as e:
            out[r] = {"reason": str(e)}
            continue
        entry: dict = {
            "objective": property_bounds_in(chain, acts, query.objective),
            "constraint": None,
            "holds": True,
            "graph_preserving": graph_preserving(acts),
            "multi_affine": not chain.non_affine and all(x.multi_affine for x in chain.rows),
        }
        if query.constraint is not None:
            entry["constraint"] = property_bounds_in(chain, acts, query.constraint)
            entry["holds"] = _certain(query.constraint, *entry["constraint"])
        out[r] = entry
    return out


# ---------------------------------------------------------------------------
# The regime map
# ---------------------------------------------------------------------------

@dataclass
class Cell:
    box: dict                          # parameter -> (lo, hi)
    depth: int = 0
    status: str = "undecided"          # decided | infeasible | undecided
    best: list = field(default_factory=list)
    reason: str | None = None          # for an undecided cell, when there is a specific one
    children: list = field(default_factory=list)


def _prop_key(prop: PropertyExpr) -> tuple:
    return (prop.quantity, prop.reward, prop.path, prop.bound,
            repr(prop.left), repr(prop.right))


def _certain(prop: PropertyExpr, lo: Any, hi: Any) -> bool | None:
    """Whether a threshold holds on a whole cell (True), fails on the
    whole cell (False), or is not decided by the bounds (None)."""
    b = prop.threshold
    if prop.op == ">=":
        return True if lo >= b else False if hi < b else None
    if prop.op == ">":
        return True if lo > b else False if hi <= b else None
    if prop.op == "<=":
        return True if hi <= b else False if lo > b else None
    return True if hi < b else False if lo >= b else None


def classify(query: RegimeMapQuery, bounds: dict, same: dict | None = None) -> tuple:
    """Status and `best` of a cell from the certified bounds of each
    compared regime (H.9).

    `bounds` maps a regime to `(objective_lo, objective_hi, constraint)`,
    where `constraint` is True (holds on the whole cell), False (fails on
    the whole cell) or None. `same` maps a regime to a key; two regimes
    with the same key have the same solution functions.
    """
    minimize = query.direction == "minimize"
    sat: dict = {}
    for r in query.regimes:
        lo, hi, constraint = bounds[r]
        if constraint is False or (minimize and lo == INFINITY):
            sat[r] = False
        elif constraint is True and not (minimize and hi == INFINITY):
            sat[r] = True
        else:
            sat[r] = None
    if all(sat[r] is False for r in query.regimes):
        return "infeasible", []
    best = []
    for r in query.regimes:
        if sat[r] is not True:
            continue
        lo, hi, _ = bounds[r]
        for other in query.regimes:
            if other == r or sat[other] is False:
                continue
            if same is not None and same[other] == same[r]:
                continue               # the same function: a tie everywhere
            o_lo, o_hi, _ = bounds[other]
            if not (hi <= o_lo if minimize else lo >= o_hi):
                break
        else:
            best.append(r)
    if best:
        return "decided", best
    return "undecided", []


class _Analyser:
    def __init__(self, model: Model, query: RegimeMapQuery, overrides: dict | None):
        self.query = query
        self.chains = {r: _chain_for(model, query, r, overrides) for r in query.regimes}
        signatures: list = []
        self.same: dict = {}
        for r in query.regimes:
            sig = self.chains[r].signature()
            if sig not in signatures:
                signatures.append(sig)
            self.same[r] = signatures.index(sig)
        self.analysed = 0

    def bounds(self, box: dict) -> dict:
        """Per regime `(objective_lo, objective_hi, constraint)`; raises
        `_Unknown` when a design cannot be analysed on the cell."""
        query = self.query
        out: dict = {}
        done: dict = {}
        for r in query.regimes:
            if self.same[r] in done:
                out[r] = done[self.same[r]]
                continue
            chain = self.chains[r]
            acts = relax(chain, box)
            cache: dict = {}

            def get(prop: PropertyExpr) -> tuple:
                key = _prop_key(prop)
                if key not in cache:
                    cache[key] = property_bounds_in(chain, acts, prop)
                return cache[key]

            constraint: bool | None = True
            if query.constraint is not None:
                constraint = _certain(query.constraint, *get(query.constraint))
            if constraint is False:
                # The regime is out on the whole cell; its objective is not needed.
                out[r] = (None, None, False)
            else:
                lo, hi = get(query.objective)
                out[r] = (lo, hi, constraint)
            done[self.same[r]] = out[r]
        return out

    def analyse(self, cell: Cell) -> None:
        self.analysed += 1
        try:
            bounds = self.bounds(cell.box)
        except _Unknown as e:
            cell.status, cell.best, cell.reason = "undecided", [], str(e)
            return
        cell.status, cell.best = classify(self.query, bounds, self.same)


def _volume(box: dict) -> Fraction:
    v = ONE
    for lo, hi in box.values():
        v *= hi - lo
    return v


def _split(cell: Cell, region: dict) -> list:
    """Bisect along the parameter whose interval is widest relative to
    the region (the first such parameter)."""
    name = max(cell.box, key=lambda n: ((cell.box[n][1] - cell.box[n][0])
                                        / (region[n][1] - region[n][0])))
    lo, hi = cell.box[name]
    mid = (lo + hi) / 2
    out = []
    for part in ((lo, mid), (mid, hi)):
        box = dict(cell.box)
        box[name] = part
        out.append(Cell(box=box, depth=cell.depth + 1))
    cell.children = out
    return out


def _merge(cell: Cell) -> None:
    """Join two halves that carry the same claim into their parent."""
    stack = [(cell, False)]
    while stack:
        node, visited = stack.pop()
        if not node.children:
            continue
        if not visited:
            stack.append((node, True))
            stack.extend((c, False) for c in node.children)
            continue
        a, b = node.children
        if (not a.children and not b.children and a.status == b.status
                and a.best == b.best and a.reason == b.reason):
            node.status, node.best, node.reason = a.status, a.best, a.reason
            node.children = []


def _leaves(cell: Cell) -> list:
    out = []
    stack = [cell]
    while stack:
        node = stack.pop()
        if node.children:
            stack.extend(reversed(node.children))
        else:
            out.append(node)
    return out


def certified_regime_map(model: Model, map_id: str, overrides: dict | None = None,
                         max_depth: int = DEFAULT_MAX_DEPTH,
                         max_cells: int = DEFAULT_MAX_CELLS) -> dict:
    """The regime map of a query (H.9), with every `decided` and
    `infeasible` cell certified by parameter lifting.

    The largest undecided cell is bisected until the undecided fraction
    is at most `resolution:`, or no cell may be split further: a cell is
    not split beyond `max_depth` bisections, and the partition is not
    refined beyond `max_cells` cells.
    """
    query = _query(model, map_id)
    if max_depth < 0:
        raise EvaluationError("--max-depth must be at least 0")
    if max_cells < 1:
        raise EvaluationError("--max-cells must be at least 1")
    analyser = _Analyser(model, query, overrides)
    region = {n: tuple(query.over[n]) for n in query.over}
    total = _volume(region)
    root = Cell(box=dict(region))
    analyser.analyse(root)
    heap: list = []
    seq = 0
    undecided = ZERO
    waiting = 0                        # undecided cells at the depth limit

    def push(cell: Cell) -> None:
        nonlocal seq, undecided, waiting
        if cell.status != "undecided":
            return
        v = _volume(cell.box)
        undecided += v
        if cell.depth >= max_depth:
            waiting += 1
            return
        seq += 1
        heapq.heappush(heap, (-v, seq, cell))

    push(root)
    cells = 1
    deepest = 0
    stopped = "resolution"
    while undecided > query.resolution * total:
        if not heap:
            stopped = "max_depth"
            break
        if cells + 1 > max_cells:
            stopped = "max_cells"
            break
        neg_v, _, cell = heapq.heappop(heap)
        undecided += neg_v
        for child in _split(cell, region):
            analyser.analyse(child)
            push(child)
            deepest = max(deepest, child.depth)
        cells += 1
    _merge(root)
    leaves = _leaves(root)
    counts = {s: sum(1 for c in leaves if c.status == s)
              for s in ("decided", "infeasible", "undecided")}
    volumes = {s: sum((_volume(c.box) for c in leaves if c.status == s), ZERO) / total
               for s in ("decided", "infeasible", "undecided")}
    by_best: dict = {}
    for c in leaves:
        if c.status == "decided":
            key = ", ".join(c.best)
            by_best[key] = by_best.get(key, ZERO) + _volume(c.box) / total
    return {
        "id": query.id,
        "note": CERTIFIED_NOTE,
        "method": METHOD,
        "direction": query.direction,
        "objective": query.objective_text,
        "constraint": query.constraint_text,
        "open": list(query.over),
        "regimes": list(query.regimes),
        "region": region,
        "resolution": query.resolution,
        "undecided_fraction": undecided / total,
        "resolution_reached": undecided <= query.resolution * total,
        "stopped": stopped,
        "max_depth": max_depth,
        "max_cells": max_cells,
        "counts": counts,
        "fractions": volumes,
        "fraction_by_best": by_best,
        "cells_before_merging": cells,
        "cells_analysed": analyser.analysed,
        "deepest": deepest,
        "cells": leaves,
    }
