"""Equilibrium checks and equilibrium maps of the strategic model extension
(Appendix I, I.6 to I.9), with exact rational arithmetic.

The game G(D, θ) of one design is built once, with the open parameters of
an equilibrium map left open (`build_game`). From it this module derives

- at one valuation: Reach_p, admissibility (I.6), the payoff, the
  best-response value, the deviation gain, the canonical best response
  and the one-step test (I.7), and with them the equilibrium check of
  I.8 (`equilibrium_check`);
- on a cell of an equilibrium map: the relaxation of I.9 of the game of
  one player against the profile (parameter lifting, on top of
  `stochastic_regions`), the bounds l, u, b, d(τ) and q, bounds of the
  one-step advantages, and the sufficient conditions N, E1, E2, PD+ and
  PD− (`cell_verdict`); and the map itself by bisection
  (`equilibrium_map`).

`equilibrium` and `not_equilibrium` are claims about every valuation of a
cell. A cell gets one of them only when the relaxed process of every
player is proper (every policy reaches the target of the player's payoff
with probability 1), which establishes admissibility on the cell, and
the design is well-defined on the cell. Everything else is `undecided`.
No floating-point number is used.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable

from .games import (
    EquilibriumQuery,
    NotEvaluated,
    link_assumptions,
    payoff_of,
    profile_of,
    term_weight,
    unevaluated_terms,
)
from .stochastic import (
    STATE_SPACE_LIMIT,
    Design,
    Model,
    compile_state_expr,
    decimal_text,
    eval_param_expr,
    ill_defined,
    param_names,
)
from .stochastic_eval import EvaluationError, _design, _solve, valuation, value_text
from .stochastic_regions import (
    DEFAULT_MAX_CELLS,
    DEFAULT_MAX_DEPTH,
    ONE,
    ZERO,
    Row,
    _canonical,
    _multi_affine,
    _names_of,
    _p_add,
    _p_eval,
    _p_interval,
    _row_actions,
    _Unknown,
    _volume,
    polynomial,
)

NOTE = ("equilibrium checks of cadl (explicit enumeration, exact rational arithmetic, "
        "policy iteration)")
METHOD = ("parameter lifting of the game of each player against the profile (I.9): on a "
          "cell, every state chooses a corner distribution for each allowed command; "
          "conditions N, E1 and E2 from the exact optimal values of the relaxed processes, "
          "and PD+ and PD- from bounds of the one-step advantages (performance difference)")
# Whether the conditions PD+ and PD− of I.9 are used (the default; False
# gives the maps of cadl 0.3.26).
USE_ADVANTAGES = True
MAP_NOTE = ("certified equilibrium map: 'equilibrium' and 'not_equilibrium' hold at every "
            "valuation of the cell")


# ---------------------------------------------------------------------------
# The game of one design, with the open parameters left open
# ---------------------------------------------------------------------------

@dataclass
class GameCommand:
    command: Any                       # stochastic.Command
    targets: list                      # successor state indices
    row: Row                           # one probability polynomial per successor


@dataclass
class Game:
    """G(D, θ) of I.5 restricted to the states that some choice of
    commands and some valuation of the open parameters can reach."""
    model: Model
    design: str
    names: list                                      # the open parameters
    theta: dict                                      # the values of the others
    states: list = field(default_factory=list)       # 0 is the initial state
    owner: list = field(default_factory=list)        # per state: player id, or None
    commands: list = field(default_factory=list)     # per state: enabled GameCommands
    labels: dict = field(default_factory=dict)
    index: dict = field(default_factory=dict)
    # Commands of the design with a probability that is not multi-affine
    # in the open parameters: (command index, polynomials).
    non_affine: list = field(default_factory=list)
    # The end-point certification of I.9, when its condition A holds for
    # the map being computed (`endpoint_setup`).
    endpoints: Any = None
    _selection: dict = field(default_factory=dict)
    _target: dict = field(default_factory=dict)
    _rho: dict = field(default_factory=dict)
    _lifting: Any = field(default=False, repr=False)   # False: not yet computed

    @property
    def multi_affine(self) -> bool:
        return not self.non_affine

    def selection(self, profile: str) -> list:
        """Per state, the position in `commands[state]` of the command the
        profile selects (I.4); None in a deadlock state."""
        if profile not in self._selection:
            rules = [(compile_state_expr(r.guard, self.index, {}), r.choose)
                     for r in profile_of(self.model, profile).rules]
            out = []
            for state, cmds in zip(self.states, self.commands):
                if not cmds:
                    out.append(None)
                    continue
                pick = None
                for guard, choose in rules:
                    k = next((k for k, c in enumerate(cmds) if c.command.action == choose), None)
                    if k is not None and guard(state):
                        pick = k
                        break
                if pick is None:
                    if len(cmds) > 1:
                        raise EvaluationError(
                            f"no rule of the profile '{profile}' applies in the state "
                            f"{show_state(self.model, state)} (I-6)")
                    pick = 0
                out.append(pick)
            self._selection[profile] = out
        return self._selection[profile]

    def target(self, player: str) -> list:
        """Per state, whether the target of the player's payoff holds."""
        if player not in self._target:
            fn = compile_state_expr(payoff_of(self.model, player).until, self.index, self.labels)
            self._target[player] = [bool(fn(s)) for s in self.states]
        return self._target[player]

    def rho(self, player: str) -> list:
        """Per state and enabled command, the step payoff ρ_p(s, c) of I.7
        as a polynomial in the open parameters (canonical form)."""
        if player not in self._rho:
            payoff = payoff_of(self.model, player)
            zero = (0,) * len(self.names)
            terms = []
            for term in payoff.terms:
                weight = polynomial(term_weight(self.model, payoff, term), self.theta, self.names)
                structure = next(r for r in self.model.rewards if r.id == term.reward)
                items = [(item.action, compile_state_expr(item.guard, self.index, self.labels),
                          eval_param_expr(item.value, self.theta))
                         for item in structure.items]
                terms.append((weight, items))
            out = []
            for state, cmds in zip(self.states, self.commands):
                per_command = []
                for c in cmds:
                    total: dict = {}
                    for weight, items in terms:
                        reward = ZERO
                        for action, guard, value in items:
                            if action is not None and action != c.command.action:
                                continue
                            if guard(state):
                                reward += value
                        if reward:
                            total = _p_add(total, {e: k * reward for e, k in weight.items()})
                    per_command.append(_canonical(total))
                out.append(per_command)
            self._rho[player] = out
        return self._rho[player]


def show_state(model: Model, state: tuple) -> str:
    def text(x: Any) -> str:
        return str(x).lower() if isinstance(x, bool) else str(x)
    return "(" + ", ".join(f"{v.id}={text(x)}" for v, x in zip(model.variables, state)) + ")"


def state_dict(model: Model, state: tuple) -> dict:
    """A state as written in a result (I.13): every variable, in
    declaration order."""
    return {v.id: x for v, x in zip(model.variables, state)}


def build_game(model: Model, design: Design, names: list, theta: dict) -> Game:
    """Enumerate the states that are reachable from the initial state
    through any enabled command and any transition whose probability is
    not the zero polynomial."""
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
                    f"parameter '{sorted(used)[0]}' (I-9)")
    index = model.var_index()
    labels = {name: compile_state_expr(tree, index, {})
              for name, (tree, _) in model.labels.items()}
    game = Game(model=model, design=design.name, names=list(names), theta=theta,
                labels=labels, index=index)
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
            game.non_affine.append((c.index, canon))
        updates = [
            (p, [(index[name], compile_state_expr(tree, index, {})) for name, tree, _ in u.sets])
            for u, p in zip(c.updates, polys)
        ]
        commands.append((compile_state_expr(c.guard, index, {}), c, updates))
    rows: dict = {}
    start = model.initial_state()
    number = {start: 0}
    game.states.append(start)
    todo = [start]
    per_state: dict = {}
    while todo:
        state = todo.pop()
        enabled = []
        for guard, c, updates in commands:
            if not guard(state):
                continue
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
                    continue               # the zero polynomial: no transition
                if nxt_t not in number:
                    number[nxt_t] = len(game.states)
                    game.states.append(nxt_t)
                    todo.append(nxt_t)
                succ.append(number[nxt_t])
                polys.append(_canonical(p))
            key = tuple(polys)
            if key not in rows:
                occurring = tuple(k for k in range(len(names))
                                  if any(exps[k] for p in key for exps, _ in p))
                rows[key] = Row(polys=key, command=c.index, occurring=occurring,
                                multi_affine=all(_multi_affine(p) for p in key))
            enabled.append(GameCommand(command=c, targets=succ, row=rows[key]))
        owners = {c.command.owner for c in enabled}
        if len(enabled) > 1 and (None in owners or len(owners) > 1):
            raise EvaluationError(
                f"in the state {show_state(model, state)} commands of several owners are "
                f"enabled (I-5)")
        per_state[number[state]] = enabled
    n = len(game.states)
    game.commands = [per_state[i] for i in range(n)]
    game.owner = [cmds[0].command.owner if cmds else None for cmds in game.commands]
    return game


# ---------------------------------------------------------------------------
# The process of one player against a profile (I.6, I.9)
# ---------------------------------------------------------------------------

@dataclass
class Process:
    """The game of one player p against a profile, at a valuation or
    relaxed on a cell. Its states are those reachable from the initial
    state before the target of p's payoff, through the commands allowed
    for p (I.6) and the distributions each command may have: Reach_p at
    a valuation, a set R_C(p) on a cell. Local state 0 is initial.

    `options[i]` lists, for a state in which the target does not hold,
    `(k, distributions)` per allowed command k (a position in
    `game.commands[state]`); a distribution maps local states to
    positive probabilities. A deadlock state has the single option
    `(None, [{i: 1}])`.
    """
    game: Game
    player: str
    states: list = field(default_factory=list)       # local -> state index of the game
    target: list = field(default_factory=list)
    mine: list = field(default_factory=list)         # a state of p
    selected: list = field(default_factory=list)     # the command the profile selects
    options: list = field(default_factory=list)


def build_process(game: Game, player: str, profile: str, dists: Callable) -> Process:
    """`dists(state, k)` gives the distributions of command k of a state:
    a list of dicts from successor state indices to positive probabilities."""
    sel = game.selection(profile)
    target = game.target(player)
    allowed: dict = {}
    seen = {0}
    todo = [0]
    while todo:
        g = todo.pop()
        if target[g]:
            continue
        cmds = game.commands[g]
        if not cmds:
            allowed[g] = [(None, [{g: ONE}])]
            continue
        ks = range(len(cmds)) if game.owner[g] == player else [sel[g]]
        allowed[g] = [(k, dists(g, k)) for k in ks]
        for _, ds in allowed[g]:
            for d in ds:
                for j in d:
                    if j not in seen:
                        seen.add(j)
                        todo.append(j)
    # Number the states in reverse postorder of a depth-first search, so
    # that the linear systems of an acyclic game are triangular.
    order: list = []
    visited = {0}
    stack = [(0, iter(_successors(allowed.get(0))))]
    while stack:
        g, it = stack[-1]
        for j in it:
            if j not in visited:
                visited.add(j)
                stack.append((j, iter(_successors(allowed.get(j)))))
                break
        else:
            order.append(g)
            stack.pop()
    order.reverse()
    local = {g: i for i, g in enumerate(order)}
    proc = Process(game=game, player=player, states=order)
    for g in order:
        proc.target.append(target[g])
        proc.mine.append(game.owner[g] == player)
        proc.selected.append(sel[g])
        proc.options.append([
            (k, [{local[j]: p for j, p in d.items()} for d in ds])
            for k, ds in allowed.get(g, [])
        ])
    return proc


def _successors(options: Any) -> list:
    out = []
    for _, ds in options or []:
        for d in ds:
            for j in d:
                if j not in out:
                    out.append(j)
    return out


def improper_states(proc: Process, choice: Callable | None = None) -> set:
    """The local states from which some policy of the process avoids the
    target for ever: the greatest set of states without the target in
    which every state has an option that stays in the set (I.6). Empty
    exactly when every policy reaches the target with probability 1 from
    every state of the process. `choice(i)` restricts the commands of a
    state (default: all allowed ones)."""
    n = len(proc.states)
    inside = {i for i in range(n) if not proc.target[i]}
    changed = True
    while changed:
        changed = False
        for i in sorted(inside):
            ks = None if choice is None else choice(i)
            stays = any(all(j in inside for j in d)
                        for k, ds in proc.options[i] if ks is None or k in ks
                        for d in ds)
            if not stays:
                inside.discard(i)
                changed = True
    return inside


def optimal_values(proc: Process, choice: Callable, reward: Callable,
                   maximize: bool = True) -> list:
    """Per local state, the optimal expected reward accumulated until the
    target, over the policies that choose in every state i a command of
    `choice(i)` and one of its distributions; `reward(i, k)` is the
    reward of command k in state i. Every policy must be proper
    (`improper_states` is empty); then every policy has a unique value,
    Bellman's equation has a unique solution, and policy iteration with
    exact linear solves finds it (I.7). Rewards may have either sign."""
    n = len(proc.states)
    sign = 1 if maximize else -1
    acts: dict = {}
    for i in range(n):
        if proc.target[i]:
            continue
        ks = choice(i)
        acts[i] = [(sign * reward(i, k), d)
                   for k, ds in proc.options[i] if k in ks for d in ds]
    x = [ZERO] * n
    if not acts:
        return x
    order = sorted(acts)
    policy = {i: 0 for i in order}
    while True:
        rows = {i: {j: p for j, p in acts[i][policy[i]][1].items() if j in acts}
                for i in order}
        solved = _solve(rows, {i: acts[i][policy[i]][0] for i in order})
        improved = False
        for i in order:
            if len(acts[i]) < 2:
                continue
            best, best_v = policy[i], solved[i]
            for a, (r, d) in enumerate(acts[i]):
                v = r
                for j, p in d.items():
                    if j in acts:
                        v += p * solved[j]
                if v > best_v:
                    best, best_v = a, v
            if best != policy[i]:
                policy[i] = best
                improved = True
        if not improved:
            for i in order:
                x[i] = sign * solved[i]
            return x


# -- choices of commands --

def _all(proc: Process) -> Callable:
    return lambda i: [k for k, _ in proc.options[i]]


def _profile(proc: Process) -> Callable:
    """Every player follows the profile."""
    return lambda i: [proc.selected[i]]


def _strategy(proc: Process, tau: dict) -> Callable:
    """The player follows τ (state index of the game -> command) where τ
    is defined, and the profile elsewhere."""
    return lambda i: [tau.get(proc.states[i], proc.selected[i])]


# ---------------------------------------------------------------------------
# At one valuation (I.6 to I.8)
# ---------------------------------------------------------------------------

def point_distributions(game: Game, point: tuple) -> Callable:
    cache: dict = {}

    def dists(g: int, k: int) -> list:
        c = game.commands[g][k]
        key = id(c.row)
        if key not in cache:
            cache[key] = [_p_eval(p, point) for p in c.row.polys]
        return [{j: p for j, p in zip(c.targets, cache[key]) if p != 0}]
    return dists


def point_reward(game: Game, proc: Process, point: tuple) -> Callable:
    rho = game.rho(proc.player)
    cache: dict = {}

    def reward(i: int, k: Any) -> Fraction:
        if k is None:
            return ZERO
        g = proc.states[i]
        if (g, k) not in cache:
            cache[(g, k)] = _p_eval(rho[g][k], point)
        return cache[(g, k)]
    return reward


def admissibility_witness(game: Game, profile: str, player: str, point: tuple) -> Any:
    """None when the profile is admissible for the player at the valuation
    `point` of the open parameters (I.6); otherwise a state of Reach_p
    from which the player can avoid the target of its payoff for ever."""
    proc = build_process(game, player, profile, point_distributions(game, point))
    bad = improper_states(proc)
    if not bad:
        return None
    return game.states[proc.states[min(bad)]]


@dataclass
class PlayerAtPoint:
    """What I.7 defines for one player at one valuation."""
    player: str
    admissible: bool = True
    witness: Any = None                # a state, when not admissible
    payoff: Fraction = ZERO            # u_p
    best_response_value: Fraction = ZERO
    gain: Fraction = ZERO
    one_step: bool = True              # the one-step test: no gain from any state of Reach_p
    # The canonical best response where it differs from the profile, on the
    # states reachable under it: state index of the game -> command position.
    deviation: dict = field(default_factory=dict)
    # Per state of Reach_p (state index of the game): V*_p(s) − V_p(s), 0
    # where the target holds (I.7).
    state_gains: dict = field(default_factory=dict)
    # The largest of them, the first state of S (in the order of S) that
    # attains it when it is positive, and the canonical best response on the
    # states reachable from that state under it.
    state_gain: Fraction = ZERO
    state: Any = None
    state_deviation: dict = field(default_factory=dict)
    # The canonical best response on all of Reach_p, where it differs.
    best_response: dict = field(default_factory=dict)


def canonical_best_response(proc: Process, v_star: list, reward: Callable) -> dict:
    """The canonical best response of I.7, as the commands in which it
    differs from the profile: in a state of the player in Reach_p in
    which the target does not hold, the command of the profile when it
    attains V*, and otherwise the first command, in the order of
    `model.commands:`, that does."""
    tau: dict = {}
    for i in range(len(proc.states)):
        if proc.target[i] or not proc.mine[i]:
            continue
        q = {k: reward(i, k) + sum((p * v_star[j] for j, p in ds[0].items()), ZERO)
             for k, ds in proc.options[i]}
        if q[proc.selected[i]] == v_star[i]:
            continue
        tau[proc.states[i]] = next(k for k, _ in proc.options[i] if q[k] == v_star[i])
    return tau


def reachable_deviation(proc: Process, tau: dict, start: int = 0) -> dict:
    """τ restricted to the states that are reachable from the local state
    `start` when the player follows τ and the others the profile (on a
    cell: through every distribution a command may have)."""
    choose = _strategy(proc, tau)
    seen = {start}
    todo = [start]
    while todo:
        i = todo.pop()
        if proc.target[i]:
            continue
        ks = choose(i)
        for k, ds in proc.options[i]:
            if k in ks:
                for d in ds:
                    for j in d:
                        if j not in seen:
                            seen.add(j)
                            todo.append(j)
    return {proc.states[i]: tau[proc.states[i]] for i in sorted(seen) if proc.states[i] in tau}


def player_at_point(game: Game, profile: str, player: str, point: tuple) -> PlayerAtPoint:
    proc = build_process(game, player, profile, point_distributions(game, point))
    out = PlayerAtPoint(player=player)
    bad = improper_states(proc)
    if bad:
        out.admissible = False
        out.witness = game.states[proc.states[min(bad)]]
        return out
    reward = point_reward(game, proc, point)
    v = optimal_values(proc, _profile(proc), reward)
    out.payoff = v[0]
    out.state_gains = {g: ZERO for g in proc.states}
    if not any(proc.mine[i] and not proc.target[i] and len(proc.options[i]) > 1
               for i in range(len(proc.states))):
        out.best_response_value = v[0]
        return out
    v_star = optimal_values(proc, _all(proc), reward)
    out.best_response_value = v_star[0]
    out.gain = v_star[0] - v[0]
    # The one-step test of I.7: Q_p(s, c) <= V_p(s) for every state s of p
    # in Reach_p and every command c of En(s).
    for i in range(len(proc.states)):
        if proc.target[i] or not proc.mine[i]:
            continue
        for k, ds in proc.options[i]:
            q = reward(i, k) + sum((p * v[j] for j, p in ds[0].items()), ZERO)
            if q > v[i]:
                out.one_step = False
    out.state_gains = {g: v_star[i] - v[i] for i, g in enumerate(proc.states)}
    out.state_gain = max(out.state_gains.values())
    if out.state_gain > 0:
        tau = canonical_best_response(proc, v_star, reward)
        out.best_response = tau
        if out.gain > 0:
            out.deviation = reachable_deviation(proc, tau)
        model = game.model
        out.state = min((g for g, x in out.state_gains.items() if x == out.state_gain),
                        key=lambda g: _state_key(model, game.states[g]))
        out.state_deviation = reachable_deviation(proc, tau, proc.states.index(out.state))
    return out


def _deviation_list(game: Game, deviation: dict) -> list:
    """A strategy in the form of `best_response` (I.13), in the order of
    the states of S."""
    order = sorted(deviation, key=lambda g: _state_key(game.model, game.states[g]))
    return [{"state": state_dict(game.model, game.states[g]),
             "action": game.commands[g][deviation[g]].command.action}
            for g in order]


def _state_key(model: Model, state: tuple) -> tuple:
    return tuple(v.domain.index(x) for v, x in zip(model.variables, state))


def _query(model: Model, query_id: str) -> EquilibriumQuery:
    query = next((q for q in model.equilibria if q.id == query_id), None)
    if query is None:
        raise EvaluationError(
            f"'{query_id}' is not an entry of model.equilibria "
            f"(entries: {', '.join(q.id for q in model.equilibria) or 'none'})")
    return query


def _designs(model: Model, query: EquilibriumQuery, regime: str | None) -> list:
    if regime is None:
        return [_design(model, r) for r in query.regimes]
    design = _design(model, regime)
    return [design] if regime in query.regimes else []


def _require_payoffs(model: Model) -> None:
    missing = unevaluated_terms(model)
    if missing:
        raise NotEvaluated("; ".join(missing))


def structure_reward(game: Game, proc: Process, reward_id: str) -> Callable:
    """`(i, k) -> number`: the reward of the structure for taking command
    k in the local state i of a process (H.8), at the fixed values."""
    structure = next(r for r in game.model.rewards if r.id == reward_id)
    items = [(item.action, compile_state_expr(item.guard, game.index, game.labels),
              eval_param_expr(item.value, game.theta)) for item in structure.items]

    def reward(i: int, k: Any) -> Fraction:
        if k is None:
            return ZERO
        g = proc.states[i]
        action = game.commands[g][k].command.action
        total = ZERO
        for act, guard, value in items:
            if (act is None or act == action) and guard(game.states[g]):
                total += value
        return total
    return reward


def expected_reward_under(model: Model, design: Design, profile: str, player: str,
                          reward_id: str, overrides: dict | None = None) -> Fraction:
    """The expected accumulated value of a reward structure until the
    target of the player's payoff, under a profile at the fixed values
    (J-12, condition (ii) of J.7)."""
    theta = valuation(model, design, overrides)
    game = build_game(model, design, [], theta)
    return _expected_in(game, profile, player, reward_id, ())


def _expected_in(game: Game, profile: str, player: str, reward_id: str,
                 point: tuple) -> Fraction:
    proc = build_process(game, player, profile, point_distributions(game, point))
    if improper_states(proc, _profile(proc)):
        raise EvaluationError(f"the profile '{profile}' does not reach the target of the "
                              f"payoff of '{player}'")
    return optimal_values(proc, _profile(proc), structure_reward(game, proc, reward_id))[0]


# ---------------------------------------------------------------------------
# End points: certification of a cell [a, b] from its end points (I.9)
# ---------------------------------------------------------------------------

ENDPOINT_METHOD = "end points (I.9)"
J14_METHOD = ENDPOINT_METHOD           # J-14 refers to the method of I.9


def syntactic_degree(tree: tuple, name: str) -> int:
    """The power with which a parameter occurs in a `param_expr`, read on
    the text: the sum over a product, the maximum over a sum, without
    cancellation."""
    kind = tree[0]
    if kind == "id":
        return 1 if tree[1] == name else 0
    if kind in ("par", "neg"):
        return syntactic_degree(tree[1], name)
    if kind == "bin":
        left, right = syntactic_degree(tree[2], name), syntactic_degree(tree[3], name)
        return left + right if tree[1] == "*" else max(left, right)
    return 0


def endpoint_parameter_free_model(model: Model, design: Design, name: str) -> str | None:
    """Condition A of the end-point certification (I.9), first part: ℓ
    occurs in no `prob:` (of a command of the design) and in no reward
    `value:`. None when it holds, otherwise why not."""
    for c in model.commands:
        if not c.exists_in(design.name):
            continue
        for u in c.updates:
            if u.prob is not None and name in param_names(u.prob):
                return f"'{name}' occurs in the probability '{u.prob_text}' of command {c.index}"
    for r in model.rewards:
        for item in r.items:
            if name in param_names(item.value):
                return f"'{name}' occurs in a value of the reward structure '{r.id}'"
    return None


def endpoint_affine_weights(model: Model, name: str) -> str | None:
    """Condition A, second part: in every weight of every player, the
    written ones and those defined by J.7, ℓ occurs with a power of at
    most one, read on the text."""
    for payoff in model.payoffs:
        for term in payoff.terms:
            weight = term_weight(model, payoff, term)
            if syntactic_degree(weight, name) > 1:
                return (f"'{name}' occurs with a power above one, counted on the text (the "
                        f"powers of the factors of a product add up, no cancellation), in the "
                        f"weight of the term '{term.reward}' of the payoff of '{payoff.player}'")
    return None


def endpoint_monotone_states(game: Game, profile: str, player: str, name: str) -> dict:
    """Condition B of the end-point certification (I.9) for p: the states
    s of Reach_p from which the gain of p is non-increasing in ℓ
    (`"decreasing"`) or non-decreasing (`"increasing"`). Under condition
    A the weight of a term is a + c·ℓ at the fixed values of the other
    parameters. B− holds from s when c ≤ 0 for every term and the reward
    structure of every term with c ≠ 0 has the expected accumulated value
    0 from s under the profile; B+ is the same with c ≥ 0. Reach_p and the
    expected values do not depend on ℓ (condition A)."""
    model = game.model
    payoff = payoff_of(model, player)
    point = (game.endpoints["region"][0],)
    signs = set()
    moving = []
    for term in payoff.terms:
        weight = term_weight(model, payoff, term)
        if name not in param_names(weight):
            continue
        poly = polynomial(weight, game.theta, [name])
        coefficient = poly.get((1,), ZERO)
        if any(e not in ((0,), (1,)) for e in poly):
            return {"decreasing": set(), "increasing": set()}     # cannot happen under A
        if coefficient != 0:
            signs.add(coefficient > 0)
            moving.append(term.reward)
    proc = build_process(game, player, profile, point_distributions(game, point))
    zero = set(proc.states)
    for reward_id in moving:
        values = optimal_values(proc, _profile(proc), structure_reward(game, proc, reward_id))
        zero &= {g for g, x in zip(proc.states, values) if x == 0}
    return {"decreasing": zero if True not in signs else set(),
            "increasing": zero if False not in signs else set()}


def endpoint_setup(game: Game, query: EquilibriumQuery, design: Design) -> dict:
    """Whether the end-point certification of I.9 may be used for the map
    in this design (condition A), and, per player, the states from which
    condition B holds. ℓ is the open parameter of the query."""
    out: dict = {"applicable": False, "reason": None, "decreasing": [], "increasing": []}
    if len(query.over) != 1:
        out["reason"] = "the map has more than one open parameter"
        return out
    name = next(iter(query.over))
    why = endpoint_parameter_free_model(game.model, design, name) \
        or endpoint_affine_weights(game.model, name)
    if why:
        out["reason"] = why
        return out
    out.update(applicable=True, parameter=name, region=query.over[name])
    game.endpoints = out
    out["states"] = {p.id: endpoint_monotone_states(game, query.profile, p.id, name)
                     for p in game.model.players}
    # The players whose gain (from the initial state) is monotone.
    for key in ("decreasing", "increasing"):
        out[key] = [p.id for p in game.model.players if 0 in out["states"][p.id][key]]
    return out


def endpoint_verdict(game: Game, query: EquilibriumQuery, cell: "EqCell") -> bool:
    """Decide a cell [a, b] from its end points (I.9) when they allow a
    claim; True when the cell got a claim. The cell is `equilibrium` when
    the profile is an ε-equilibrium (in the sense of the query's concept)
    at a and at b; p is a deviator when condition B− holds for p from a
    state s and the gain from s at b exceeds ε, or B+ holds and the gain
    from s at a exceeds ε (s is the initial state for the concept
    `nash`, any state of Reach_p for `every_reachable_state`)."""
    (a, b), = [cell.box[n] for n in game.names]
    every = query.concept == "every_reachable_state"
    at_a, at_b = {}, {}
    for p in game.model.players:
        at_a[p.id] = player_at_point(game, query.profile, p.id, (a,))
        at_b[p.id] = player_at_point(game, query.profile, p.id, (b,))
        if not (at_a[p.id].admissible and at_b[p.id].admissible):
            return False
    eps = query.epsilon

    def worst(at: PlayerAtPoint) -> Fraction:
        return at.state_gain if every else at.gain

    if all(worst(at_a[p]) <= eps and worst(at_b[p]) <= eps for p in at_a):
        cell.status, cell.method = "equilibrium", ENDPOINT_METHOD
        # Every gain is convex in ℓ (I.9).
        cell.gain_bounds = {p: (ZERO, max(at_a[p].gain, at_b[p].gain)) for p in at_a}
        if every and eps == 0:
            cell.from_every_state = True
        return True
    model = game.model
    states = game.endpoints["states"]
    found = {}
    for p in at_a:
        for key, at, other in (("decreasing", at_b[p], at_a[p]),
                               ("increasing", at_a[p], at_b[p])):
            if every:
                candidates = sorted((g for g in states[p][key] if at.state_gains.get(g, ZERO) > eps),
                                    key=lambda g: _state_key(model, game.states[g]))
            else:
                candidates = [0] if 0 in states[p][key] and at.gain > eps else []
            if candidates and p not in found:
                g = candidates[0]
                found[p] = (g, at, other)
    if not found:
        return False
    deviators = [p for p in at_a if p in found]
    cell.status, cell.deviators, cell.method = "not_equilibrium", deviators, ENDPOINT_METHOD
    cell.gain_bounds, cell.deviations = {}, {}
    for p in deviators:
        g, at, other = found[p]
        if g == 0:
            # The gain is monotone in ℓ (I.9): between its values at the ends.
            cell.gain_bounds[p] = (at.gain, other.gain)
            deviation = at.deviation
        else:
            cell.gain_bounds[p] = (ZERO, max(at.gain, other.gain))
            proc = build_process(game, p, query.profile, point_distributions(game, (a,)))
            deviation = reachable_deviation(proc, at.best_response, proc.states.index(g))
        cell.deviations[p] = _deviation_list(game, deviation)
        if every:
            # The gain from g is monotone in ℓ: at least its value at the end.
            cell.deviation_states[p] = (state_dict(model, game.states[g]), at.state_gains[g])
    return True


def equilibrium_check(model: Model, query_id: str, regime: str | None = None,
                      overrides: dict | None = None) -> dict:
    """The result of an equilibrium check (I.8), per design: the payoff,
    the best-response value, the gain and the canonical best response of
    every player, the status, and `from_every_state`.

    Raises `EvaluationError` when a design is not well-defined or the
    profile not admissible at the values used, and `NotEvaluated` when a
    payoff cannot be evaluated (I-7)."""
    query = _query(model, query_id)
    if query.is_map:
        raise EvaluationError(f"'{query_id}' is an equilibrium map (it has over:)")
    _require_payoffs(model)
    designs = []
    for design in _designs(model, query, regime):
        theta = valuation(model, design, overrides)
        why = ill_defined(model, design.name, theta)
        if why:
            raise EvaluationError(f"the design {design.name} is not well-defined here: {why}")
        game = build_game(model, design, [], theta)
        players = []
        for p in model.players:
            at = player_at_point(game, query.profile, p.id, ())
            if not at.admissible:
                raise EvaluationError(
                    f"the profile '{query.profile}' is not admissible for the player "
                    f"'{p.id}' in the design {design.name} at these values (I.6): from the "
                    f"state {show_state(model, at.witness)} the player can avoid the "
                    f"target of its payoff for ever")
            players.append(at)
        every = query.concept == "every_reachable_state"
        deviators = [at.player for at in players
                     if (at.state_gain if every else at.gain) > query.epsilon]
        status = "not_equilibrium" if deviators else "equilibrium"
        from_every_state = None
        if status == "equilibrium" and query.epsilon == 0:
            from_every_state = all(at.one_step for at in players)
        entries = []
        for at in players:
            entry = {"player": at.player, "payoff": at.payoff,
                     "best_response_value": at.best_response_value, "gain": at.gain}
            if every:
                deviates = at.state_gain > query.epsilon
                entry.update(
                    state_gain=at.state_gain,
                    state=state_dict(model, game.states[at.state]) if deviates else None,
                    best_response=(_deviation_list(game, at.state_deviation)
                                   if deviates else []))
            else:
                entry["best_response"] = (_deviation_list(game, at.deviation)
                                          if at.gain > query.epsilon else [])
            entries.append(entry)
        designs.append({
            "design": design.name,
            "parameters": theta,
            "status": status,
            "deviators": deviators,
            "from_every_state": from_every_state,
            "players": entries,
        })
    return {"id": query.id, "profile": query.profile, "epsilon": query.epsilon,
            "concept": query.concept, "assumptions": link_assumptions(model),
            "designs": designs}


# ---------------------------------------------------------------------------
# On a cell (I.9)
# ---------------------------------------------------------------------------

def face_centres(over: dict) -> list:
    """The centres of the 3^n faces of a region: every open parameter at
    the lower end, the middle, or the upper end of its interval (I.9)."""
    return list(itertools.product(*[(lo, (lo + hi) / 2, hi) for lo, hi in over.values()]))


def cell_distributions(game: Game, cell: tuple) -> Callable:
    """The distributions each command may have on a cell: its values at
    the corners when its probabilities are multi-affine in the open
    parameters, and otherwise the vertices of the set of distributions
    within exact interval enclosures (H.14). Raises `_Unknown` when the
    design is not shown to be well-defined on the cell."""
    for index, polys in game.non_affine:
        for p in polys:
            lo, hi = _p_interval(p, cell)
            if lo < 0 or hi > 1:
                raise _Unknown("not_well_defined")
    cache: dict = {}

    def dists(g: int, k: int) -> list:
        c = game.commands[g][k]
        key = id(c.row)
        if key not in cache:
            try:
                values = _row_actions(game, c.row, cell)
            except EvaluationError as e:
                raise _Unknown("not_well_defined") from e
            if not values:
                raise _Unknown("not_well_defined")
            cache[key] = values
        return [{j: p for j, p in zip(c.targets, v) if p != 0} for v in cache[key]]
    return dists


def reward_bounds(game: Game, proc: Process, cell: tuple) -> tuple:
    """Two functions `(i, k) -> number`: a lower and an upper bound of the
    step payoff ρ_p(s, c) on the cell: its extreme values at the corners
    when it is multi-affine in the open parameters, and an exact
    interval enclosure otherwise."""
    rho = game.rho(proc.player)
    cache: dict = {}

    def bounds(i: int, k: Any) -> tuple:
        if k is None:
            return ZERO, ZERO
        poly = rho[proc.states[i]][k]
        if poly not in cache:
            if _multi_affine(poly):
                occurring = [d for d in range(len(cell)) if any(e[d] for e, _ in poly)]
                values = []
                for corner in itertools.product((0, 1), repeat=len(occurring)):
                    point = [b[0] for b in cell]
                    for d, side in zip(occurring, corner):
                        point[d] = cell[d][side]
                    values.append(_p_eval(poly, point))
                cache[poly] = (min(values), max(values))
            else:
                cache[poly] = _p_interval(poly, cell)
        return cache[poly]

    return (lambda i, k: bounds(i, k)[0]), (lambda i, k: bounds(i, k)[1])


# -- the sufficient conditions of I.9 --

def condition_n(d_tau: Fraction, u: Fraction, epsilon: Fraction) -> bool:
    """N: p is a deviator on the cell when d_p(τ) − u_p > ε, where d_p(τ)
    is a lower bound of the payoff of p when p plays τ and u_p an upper
    bound of its payoff under the profile."""
    return d_tau - u > epsilon


def condition_e1(b: Fraction, l: Fraction, epsilon: Fraction) -> bool:
    """E1: gain_p ≤ ε on the cell when b_p − l_p ≤ ε, where b_p is an
    upper bound of the best-response value and l_p a lower bound of the
    payoff under the profile."""
    return b - l <= epsilon


def condition_e2(pairs: list) -> bool:
    """E2: no gain of p from any state of Reach_p on the cell when
    q_p(s, c) ≤ l_p(s) for every state s of p in R_C(p) in which the
    target does not hold and every command c other than the profile's.
    `pairs` lists `(q_p(s, c), l_p(s))`."""
    return all(q <= l for q, l in pairs)


def one_step_upper(proc: Process, i: int, k: int, hi: Callable, upper: list) -> Fraction:
    """q_p(s, c) of I.9: an upper bound, on the cell, of the payoff of p
    when the command k is taken once in the local state i and the profile
    is followed afterwards: the upper step payoff plus the largest
    expectation of the upper bounds u_p(s') over the distributions the
    command can have."""
    ds = next(ds for k2, ds in proc.options[i] if k2 == k)
    return hi(i, k) + max(sum((p * upper[j] for j, p in d.items()), ZERO) for d in ds)


def deviation_lower(proc: Process, tau: dict, lo: Callable) -> Fraction:
    """d_p(τ) of I.9: a lower bound, on the cell, of the payoff of p when
    p plays τ and the others follow the profile: the least value of the
    relaxed process with the lower step payoffs."""
    return optimal_values(proc, _strategy(proc, tau), lo, maximize=False)[0]


def poly_bounds(poly: tuple, cell: tuple) -> tuple:
    """Exact bounds of a polynomial on a cell: its extreme values at the
    corners when it is multi-affine in the open parameters (its value at a
    valuation of the box is a convex combination of its values at the
    corners), and an exact interval enclosure otherwise."""
    if not poly:
        return ZERO, ZERO
    if _multi_affine(poly):
        occurring = [d for d in range(len(cell)) if any(e[d] for e, _ in poly)]
        values = []
        for corner in itertools.product((0, 1), repeat=len(occurring)):
            point = [b[0] for b in cell]
            for d, side in zip(occurring, corner):
                point[d] = cell[d][side]
            values.append(_p_eval(poly, point))
        return min(values), max(values)
    return _p_interval(poly, cell)


def _interval_product(a: tuple, b: tuple) -> tuple:
    products = (a[0] * b[0], a[0] * b[1], a[1] * b[0], a[1] * b[1])
    return min(products), max(products)


def advantage_bounds(game: Game, proc: Process, cell: tuple, lower: list,
                     upper: list) -> dict | None:
    """Bounds of the one-step advantage A_p(s, c, v) = Q_p(s, c, v) − V_p(s, v)
    of I.9 on a cell, for every state s of p in R_C(p) in which the target
    does not hold and every command c other than the one the profile
    selects: `{(i, k): (lower, upper)}` over local states i and command
    positions k. With σ(s) the command of the profile, V_p(s, v) =
    Q_p(s, σ(s), v), so

        A_p(s, c, v) = ρ_p(s, c, v) − ρ_p(s, σ(s), v)
                       + Σ_s′ (P(s, c, s′, v) − P(s, σ(s), s′, v)) · (V_p(s′, v) − m)

    for any number m, since both distributions sum to 1. The differences
    of step payoffs and of probabilities are polynomials in the open
    parameters and are bounded exactly on the cell (`poly_bounds`), V_p(s′)
    by the bounds l_p(s′) and u_p(s′) of the relaxation, and the products
    by interval arithmetic; m is the middle of the hull of these bounds.
    A successor with a probability that is not zero at some valuation of
    the cell is in R_C(p); the result is None if one is not."""
    rho = game.rho(proc.player)
    local = {g: i for i, g in enumerate(proc.states)}
    out: dict = {}
    for i, g in enumerate(proc.states):
        if proc.target[i] or not proc.mine[i]:
            continue
        sel = proc.selected[i]
        mine = game.commands[g][sel]
        for k, _ in proc.options[i]:
            if k is None or k == sel:
                continue
            c = game.commands[g][k]
            d_rho = _canonical(_p_add(dict(rho[g][k]), dict(rho[g][sel]), -1))
            lo, hi = poly_bounds(d_rho, cell)
            diff: dict = {}
            for j, poly in zip(c.targets, c.row.polys):
                diff[j] = _p_add(diff.get(j, {}), dict(poly))
            for j, poly in zip(mine.targets, mine.row.polys):
                diff[j] = _p_add(diff.get(j, {}), dict(poly), -1)
            terms = []
            for j, poly in diff.items():
                bounds = poly_bounds(_canonical(poly), cell)
                if bounds == (ZERO, ZERO):
                    continue
                if j not in local:
                    return None
                terms.append((bounds, lower[local[j]], upper[local[j]]))
            if terms:
                m = (min(t[1] for t in terms) + max(t[2] for t in terms)) / 2
                for bounds, l, u in terms:
                    a, b = _interval_product(bounds, (l - m, u - m))
                    lo += a
                    hi += b
            out[(i, k)] = (lo, hi)
    return out


def cell_corners(cell: tuple) -> list:
    """The 2^n corners of a cell, as valuations of the open parameters."""
    return list(itertools.product(*cell))


# The largest number of coordinates of a lifted cell (open parameters and
# lifted functions, I.9); a game that needs more does not use the coupled
# bounds.
LIFT_LIMIT = 10
# Whether designs with probabilities that are not multi-affine use the
# coupled bounds through the lifting of I.9 (False gives the maps of cadl
# 0.3.28).
USE_LIFTING = True
# Whether the interval of a lifted coordinate is narrowed by the Bernstein
# coefficients (`lifted_interval`).
LIFT_BERNSTEIN = True


class CouplingUnavailable(Exception):
    """A coupled bound cannot be computed on this cell (I.9): a corner of
    the lifted cell gives a command a distribution that is not one the
    relaxed process may choose. Leaving the bound out is sound."""


@dataclass
class Lifting:
    """The lifting of I.9 of the probabilities of a game. Each probability
    that is not multi-affine in the open parameters x is written
    a + b · R(x), with R one of the *lifted functions* (no constant term,
    first coefficient 1), and R is replaced by a new coordinate y. A
    probability of a row is then a multi-affine polynomial L in (x, y)
    with L(x, R(x)) equal to it, and the polynomials of a row sum to 1
    identically in (x, y): when the forms a + b · y do not, the last
    probability that is not multi-affine is replaced by 1 minus the
    others. The coordinates are shared by all rows, so that two commands
    with the same probability have the same value at every corner of the
    lifted cell."""
    functions: list                    # canonical polynomials R in x
    rows: dict                         # id(row) -> tuple of lifted polynomials

    def dimension(self, n: int) -> int:
        return n + len(self.functions)


def _lifted_form(poly: tuple, n: int) -> tuple:
    """(a, b, R) with poly = a + b · R, R without constant term and with
    first coefficient 1 (canonical)."""
    zero = (0,) * n
    a = ZERO
    rest = []
    for e, c in poly:
        if e == zero:
            a = c
        else:
            rest.append((e, c))
    b = rest[0][1]
    return a, b, tuple((e, c / b) for e, c in rest)


def lift_game(game: Game) -> "Lifting | None":
    """The lifting of the probabilities of the game (`Lifting`), computed
    once; None when the lifted cell would have more than LIFT_LIMIT
    coordinates."""
    if game._lifting is not False:
        return game._lifting
    n = len(game.names)
    zero = (0,) * n
    rows = {}
    for cmds in game.commands:
        for c in cmds:
            rows.setdefault(id(c.row), c.row)
    keys: dict = {}                    # R -> position among the lifted functions
    forms: dict = {}
    for key, row in rows.items():
        if row.multi_affine:
            continue
        form = []
        for p in row.polys:
            if _multi_affine(p):
                form.append((dict(p), None))
            else:
                a, b, r = _lifted_form(p, n)
                form.append(({zero: a} if a else {}, (b, r)))
        # Whether the forms sum to 1 identically in (x, y).
        affine: dict = {}
        ys: dict = {}
        for x_part, y_part in form:
            affine = _p_add(affine, x_part)
            if y_part is not None:
                ys[y_part[1]] = ys.get(y_part[1], ZERO) + y_part[0]
        if _canonical(affine) != ((zero, ONE),) or any(ys.values()):
            last = max(j for j, (_, y) in enumerate(form) if y is not None)
            form[last] = ("rest", None)
        forms[key] = form
        for x_part, y_part in form:
            if y_part is not None and y_part[1] not in keys:
                keys[y_part[1]] = len(keys)
    if n + len(keys) > LIFT_LIMIT:
        game._lifting = None
        return None
    m = n + len(keys)
    pad = (0,) * len(keys)

    def extend(p: dict) -> dict:
        return {e + pad: c for e, c in p.items() if c}

    lifted: dict = {}
    for key, row in rows.items():
        if row.multi_affine:
            lifted[key] = tuple(_canonical(extend(dict(p))) for p in row.polys)
            continue
        out = []
        for x_part, y_part in forms[key]:
            if x_part == "rest":
                out.append(None)
                continue
            q = extend(x_part)
            if y_part is not None:
                b, r = y_part
                e = [0] * m
                e[n + keys[r]] = 1
                q = _p_add(q, {tuple(e): b})
            out.append(q)
        if None in out:
            j = out.index(None)
            rest: dict = {(0,) * m: ONE}
            for q in out:
                if q is not None:
                    rest = _p_add(rest, q, -1)
            out[j] = rest
        lifted[key] = tuple(_canonical(q) for q in out)
    game._lifting = Lifting(functions=list(keys), rows=lifted)
    return game._lifting


def bernstein_interval(poly: tuple, cell: tuple) -> tuple:
    """An exact enclosure of a polynomial on a box from its Bernstein
    coefficients: with x_k = lo_k + (hi_k − lo_k) · t_k, the polynomial
    in t on [0, 1]^n is Σ_I b_I · Π_k B(d_k, i_k, t_k), the Bernstein
    basis functions are not negative and sum to 1, so every value lies
    between the least and the largest b_I."""
    n = len(cell)
    degrees = [max((e[k] for e, _ in poly), default=0) for k in range(n)]
    # The coefficients in t: expand c · Π (lo + w t)^e by the binomial theorem.
    coeffs: dict = {}
    for e, c in poly:
        terms = {(): c}
        for k in range(n):
            lo, hi = cell[k]
            w = hi - lo
            out: dict = {}
            for j in range(e[k] + 1):
                f = Fraction(_binomial(e[k], j)) * lo ** (e[k] - j) * w ** j
                if f:
                    for idx, x in terms.items():
                        out[idx + (j,)] = out.get(idx + (j,), ZERO) + x * f
            terms = out
        for idx, x in terms.items():
            coeffs[idx] = coeffs.get(idx, ZERO) + x
    values = []
    for index in itertools.product(*[range(d + 1) for d in degrees]):
        b = ZERO
        for j, a in coeffs.items():
            if all(jk <= ik for jk, ik in zip(j, index)):
                f = a
                for jk, ik, d in zip(j, index, degrees):
                    f *= Fraction(_binomial(ik, jk), _binomial(d, jk))
                b += f
        values.append(b)
    return min(values), max(values)


def _binomial(n: int, k: int) -> int:
    out = 1
    for i in range(k):
        out = out * (n - i) // (i + 1)
    return out


def lifted_interval(poly: tuple, cell: tuple) -> tuple:
    """The interval of a lifted coordinate (I.9): the intersection of the
    enclosure by interval arithmetic (that of the relaxation) and that of
    the Bernstein coefficients. Both contain the values on the cell."""
    a, b = _p_interval(poly, cell)
    if not LIFT_BERNSTEIN:
        return a, b
    c, d = bernstein_interval(poly, cell)
    return max(a, c), min(b, d)


def coupling_applies(game: Game) -> bool:
    """Whether the coupled bounds of I.9 can be computed for the game: its
    probabilities are multi-affine in the open parameters, or the lifted
    cell has at most LIFT_LIMIT coordinates."""
    return game.multi_affine or (USE_LIFTING and lift_game(game) is not None)


class CornerTable:
    """Exact values of the probabilities and step payoffs of a game at the
    corners of one lifted cell (I.9), computed when first asked for. When
    the probabilities of every command are multi-affine in the open
    parameters, the lifted cell is the cell. Otherwise each lifted
    function R (`lift_game`) adds the coordinate y with the interval of an
    exact enclosure of R on the cell, which contains R(v) for every v of
    the cell; a probability that is not multi-affine is evaluated at a
    corner by its lifted polynomial. Its value there lies in its own
    enclosure, the set of distributions of the relaxed process, only when
    it is checked (`distribution` raises `CouplingUnavailable` otherwise).
    A step payoff that is not multi-affine is enclosed by `poly_bounds`."""

    def __init__(self, game: Game, cell: tuple):
        lifting = lift_game(game)
        if lifting is None:
            raise ValueError(f"the coupled bounds of I.9 need at most {LIFT_LIMIT} "
                             f"coordinates of the lifted cell")
        self.game = game
        self.cell = cell
        self.lifting = lifting
        # The lifted cell B; a lifted function whose interval is a point
        # adds no corners.
        self.box = tuple(cell) + tuple(lifted_interval(r, cell) for r in lifting.functions)
        self.corners = list(itertools.product(*[sorted({a, b}) for a, b in self.box]))
        self._rows: dict = {}
        self._rho: dict = {}
        self._checked: dict = {}

    def _relaxed(self, row: Row) -> list:
        """The intervals of the relaxed process for the probabilities of a
        row that is not multi-affine (as `_row_actions`)."""
        key = id(row)
        if key not in self._checked:
            out = []
            for p in row.polys:
                lo, hi = _p_interval(p, self.cell)
                out.append((max(lo, ZERO), min(hi, ONE)))
            self._checked[key] = out
        return self._checked[key]

    def distribution(self, g: int, k: int, c: int) -> dict:
        """The distribution of command k of the game state g at corner c,
        as a dict from successor state indices to positive probabilities.
        Raises `CouplingUnavailable` when it is not a distribution of the
        relaxed process on the cell."""
        cmd = self.game.commands[g][k]
        key = (id(cmd.row), c)
        if key not in self._rows:
            polys = self.lifting.rows[id(cmd.row)]
            values = [_p_eval(p, self.corners[c]) for p in polys]
            if not cmd.row.multi_affine:
                if sum(values, ZERO) != ONE or any(
                        not lo <= x <= hi for x, (lo, hi) in zip(values, self._relaxed(cmd.row))):
                    raise CouplingUnavailable(
                        f"command {cmd.row.command}: a corner of the lifted cell is not a "
                        f"distribution of the relaxed process")
            self._rows[key] = values
        out: dict = {}
        for j, p in zip(cmd.targets, self._rows[key]):
            if p != 0:
                out[j] = out.get(j, ZERO) + p
        return out

    def reward(self, poly: tuple, c: int) -> tuple:
        """Lower and upper bound of a step payoff at corner c: its value
        there when it is multi-affine, its bounds on the cell otherwise."""
        key = (poly, c)
        if key not in self._rho:
            if not poly:
                self._rho[key] = (ZERO, ZERO)
            elif _multi_affine(poly):
                x = _p_eval(poly, self.corners[c][:len(self.cell)])
                self._rho[key] = (x, x)
            else:
                self._rho[key] = poly_bounds(poly, self.cell)
        return self._rho[key]


def _coupling(d1: dict, d2: dict) -> dict:
    """A joint distribution with the marginals d1 and d2 (dicts from
    states to positive probabilities, in the order of the updates of their
    commands), as a dict from pairs to positive probabilities: the mass
    min(d1(j), d2(j)) on the pair (j, j), and the rest matched in the
    order of the updates (the north-west corner rule), so that two
    commands with the same probabilities move two plays update by update
    together."""
    joint: dict = {}
    rest1: list = []
    rest2: list = []
    for j, a in d1.items():
        m = min(a, d2.get(j, ZERO))
        if m > 0:
            joint[(j, j)] = m
        if a > m:
            rest1.append([j, a - m])
    for j, b in d2.items():
        m = min(b, d1.get(j, ZERO))
        if b > m:
            rest2.append([j, b - m])
    x = y = 0
    while x < len(rest1) and y < len(rest2):
        m = min(rest1[x][1], rest2[y][1])
        key = (rest1[x][0], rest2[y][0])
        joint[key] = joint.get(key, ZERO) + m
        rest1[x][1] -= m
        rest2[y][1] -= m
        if rest1[x][1] == 0:
            x += 1
        if rest2[y][1] == 0:
            y += 1
    return joint


# Whether the coupled bounds of the advantages are used (I.9) where PD+ and
# PD− with the bounds of `advantage_bounds` fail; False gives the maps of
# cadl 0.3.27.
USE_COUPLING = True


class CoupledPlays:
    """The coupled relaxation of I.9 of the advantages of one player on one
    cell, over the corners of the lifted cell of a `CornerTable` (the
    corners of the cell when the probabilities of the game are
    multi-affine in the open parameters).

    A_p(s, c, v) = Q_p(s, c, v) − Q_p(s, σ(s), v) is the difference of the
    expected payoffs of two plays that start in s, one with c and one with
    σ(s), and follow the profile afterwards. Write w_e(v) for the
    multilinear weights of v with respect to the corners e of the cell
    (non-negative, sum 1); every multi-affine function f satisfies f(v) =
    Σ_e w_e(v) · f(e). Run the two plays together: in each step choose a
    corner e with probability w_e(v) and move the pair by a joint
    distribution whose marginals are the distributions of the two plays at
    e (`_coupling`). Each play alone is then the Markov chain at v, and its
    expected step payoff in each step is that at v, so the expected
    difference of the accumulated payoffs is A_p(s, c, v). This joint
    process is a randomised policy of the process on pairs in which every
    pair chooses a corner, so the largest and least values of that
    process bound A_p(s, c, v) at every valuation of the cell. A pair of
    one state with itself moves to pairs of one state (the joint
    distribution puts all mass on them) and adds 0, as does a pair of two
    states in which the target holds; such pairs end the process. A step
    payoff that is not multi-affine is replaced by its bound on the cell
    in the direction of the bound sought.

    With probabilities that are not multi-affine, the same holds over the
    lifted cell: w_e is taken at the lifted point (v, R(v)), every lifted
    polynomial L satisfies L(v, R(v)) = Σ_e w_e · L(e), and a corner e
    is used only when the distribution of each command there is one of
    the relaxed process (`CornerTable.distribution`).

    Every policy of the process on pairs ends with probability 1, since
    each play alone follows a policy of the relaxed process of I.9 (a
    mixture of its distributions), all of whose policies reach the
    target. Values of pairs are kept, so the advantages of different
    commands share them. `bound` raises `CouplingUnavailable` when a
    corner is not usable."""

    def __init__(self, game: Game, proc: Process, table: CornerTable):
        if table.game is not game:
            raise ValueError("the coupled bounds of I.9 need a CornerTable of the same game")
        self.game = game
        self.proc = proc
        self.table = table
        self.rho = game.rho(proc.player)
        self.local = {g: i for i, g in enumerate(proc.states)}
        self._components: dict = {}
        self._options: dict = {}
        self._values: tuple = ({}, {})            # least, largest

    def _component(self, i: int, k: Any) -> tuple:
        """The distinct distributions (over local states) of command k in
        the local state i at the corners of the cell, and per corner the
        position of its distribution among them and the bounds of the
        step payoff there. A state in which the target holds stays where
        it is and adds 0."""
        key = (i, k)
        if key in self._components:
            return self._components[key]
        proc = self.proc
        if proc.target[i] or k is None:
            out = ([{i: ONE}], [(0, ZERO, ZERO)] * len(self.table.corners))
        else:
            g = proc.states[i]
            dists: list = []
            index: dict = {}
            per_corner = []
            for c in range(len(self.table.corners)):
                d = self.table.distribution(g, k, c)
                if any(j not in self.local for j in d):
                    raise CouplingUnavailable("a successor outside the relaxed process")
                d = {self.local[j]: p for j, p in d.items()}
                sig = tuple(d.items())
                if sig not in index:
                    index[sig] = len(dists)
                    dists.append(d)
                lo, hi = self.table.reward(self.rho[g][k], c)
                per_corner.append((index[sig], lo, hi))
            out = (dists, per_corner)
        self._components[key] = out
        return out

    def options(self, x: tuple) -> list:
        """The options of x, a pair of local states or `("start", i, k)`
        for the first step: `(least step payoff, largest step payoff,
        joint distribution)`, one per distinct pair of distributions over
        the corners; empty for a pair that ends the process."""
        if x in self._options:
            return self._options[x]
        proc = self.proc
        if x[0] == "start":
            i, k = x[1], x[2]
            a, b = (i, k), (i, proc.selected[i])
        elif x[0] == x[1] or (proc.target[x[0]] and proc.target[x[1]]):
            self._options[x] = []
            return []
        else:
            a, b = (x[0], proc.selected[x[0]]), (x[1], proc.selected[x[1]])
        dists1, corners1 = self._component(*a)
        dists2, corners2 = self._component(*b)
        groups: dict = {}
        for (n1, lo1, hi1), (n2, lo2, hi2) in zip(corners1, corners2):
            lo, hi = lo1 - hi2, hi1 - lo2
            g = groups.get((n1, n2))
            if g is None:
                groups[(n1, n2)] = [lo, hi]
            else:
                if lo < g[0]:
                    g[0] = lo
                if hi > g[1]:
                    g[1] = hi
        out = [(lo, hi, _coupling(dists1[n1], dists2[n2]))
               for (n1, n2), (lo, hi) in groups.items()]
        self._options[x] = out
        return out

    def may_narrow(self, i: int, k: int, lower: list, upper: list) -> bool:
        """False when every pair of different states that the first step
        can reach (a successor of c and one of σ(s) in the relaxed
        process) has a state whose value is known exactly on the cell
        (l_p = u_p, also where the target holds): then the coupled bound
        is not expected to be narrower than that of `advantage_bounds`,
        and it is not computed (leaving it out is always sound)."""
        proc = self.proc
        def open_successors(command: Any) -> set:
            return {j for k2, ds in proc.options[i] if k2 == command for d in ds for j in d
                    if lower[j] != upper[j]}
        first, second = open_successors(k), open_successors(proc.selected[i])
        return any(x != y for x in first for y in second)

    def bound(self, i: int, k: int, maximize: bool) -> Fraction:
        """The largest (or least) value of the process on pairs from the
        first step of the commands k and σ(s) in the local state i: an
        upper (lower) bound of A_p(s, k, v) on the cell."""
        start = ("start", i, k)
        values = self._values[1 if maximize else 0]
        pick = max if maximize else min
        side = 1 if maximize else 0
        # Values in the postorder of a depth-first search; a pair that is
        # met again while it is on the stack means a cycle (then policy
        # iteration, `_bound_cyclic`).
        state = {start: 1}
        stack = [(start, iter([y for _, _, d in self.options(start) for y in d]))]
        while stack:
            x, it = stack[-1]
            for y in it:
                if y in values:
                    continue
                if state.get(y) == 1:
                    return self._bound_cyclic(start, maximize)
                state[y] = 1
                stack.append((y, iter([z for _, _, d in self.options(y) for z in d])))
                break
            else:
                state[x] = 2
                stack.pop()
                values[x] = pick((o[side] + sum((p * values[y] for y, p in o[2].items()), ZERO)
                                  for o in self.options(x)), default=ZERO)
        return values.pop(start)

    def _bound_cyclic(self, start: tuple, maximize: bool) -> Fraction:
        """As `bound`, by policy iteration with exact linear solves
        (`optimal_values`) on the pairs reachable from `start`."""
        side = 1 if maximize else 0
        keys = [start]
        local = {start: 0}
        for x in keys:
            for _, _, d in self.options(x):
                for y in d:
                    if y not in local:
                        local[y] = len(keys)
                        keys.append(y)
        opts = [self.options(x) for x in keys]
        pairs = Process(game=self.game, player=self.proc.player, states=keys,
                        target=[not o for o in opts], mine=[True] * len(keys),
                        selected=[None] * len(keys),
                        options=[[(a, [{local[y]: p for y, p in o[2].items()}])
                                  for a, o in enumerate(os_)] for os_ in opts])
        return optimal_values(pairs, lambda i: range(len(opts[i])),
                              lambda i, a: opts[i][a][side], maximize)[0]


def coupled_advantage_bounds(game: Game, proc: Process, table: CornerTable,
                             keys: list, sides: tuple = (0, 1),
                             plays: "CoupledPlays | None" = None) -> dict:
    """Bounds of the advantages A_p(s, c, v) of I.9 on a cell from the
    coupled relaxation (`CoupledPlays`), for the pairs `(i, k)` of a local
    state i of the player and a command position k other than the
    profile's: `{(i, k): (lower, upper)}`. With `sides` (0 lower, 1
    upper) only these bounds are computed; the other is None."""
    plays = plays or CoupledPlays(game, proc, table)
    return {(i, k): tuple(plays.bound(i, k, side == 1) if side in sides else None
                          for side in (0, 1))
            for i, k in keys}


def advantage_reward(proc: Process, bounds: dict, side: int) -> Callable:
    """`(i, k) -> number`: the lower (side 0) or upper (side 1) bound of the
    advantage of command k in the local state i, and 0 for the command of
    the profile and in a state that is not the player's."""
    return lambda i, k: bounds[(i, k)][side] if (i, k) in bounds else ZERO


def positive_on_cell(poly: tuple, cell: tuple) -> bool:
    """Whether a probability is positive at every valuation of the cell:
    at every corner of the cell when it is multi-affine (its value at any
    valuation of the box is a convex combination of the values at the
    corners), and by an exact interval enclosure otherwise."""
    if not poly:
        return False
    if _multi_affine(poly):
        occurring = [d for d in range(len(cell)) if any(e[d] for e, _ in poly)]
        for corner in itertools.product((0, 1), repeat=len(occurring)):
            point = [b[0] for b in cell]
            for d, side in zip(occurring, corner):
                point[d] = cell[d][side]
            if _p_eval(poly, point) <= 0:
                return False
        return True
    return _p_interval(poly, cell)[0] > 0


def certified_reach(game: Game, profile: str, player: str, cell: tuple) -> set:
    """States (indices of the game) that belong to Reach_p at every
    valuation of the cell: reachable from the initial state, before the
    target of p's payoff, through commands allowed for p (I.6) and
    transitions whose probability is positive on the whole cell."""
    sel = game.selection(profile)
    target = game.target(player)
    seen = {0}
    todo = [0]
    while todo:
        g = todo.pop()
        if target[g] or not game.commands[g]:
            continue
        cmds = game.commands[g]
        ks = range(len(cmds)) if game.owner[g] == player else [sel[g]]
        for k in ks:
            c = cmds[k]
            for j, poly in zip(c.targets, c.row.polys):
                if j not in seen and positive_on_cell(poly, cell):
                    seen.add(j)
                    todo.append(j)
    return seen


@dataclass
class PlayerOnCell:
    player: str
    e1: bool = False
    e2: bool = False
    n: bool = False
    deviation: dict = field(default_factory=dict)    # the τ of N
    gain_lower: Fraction = ZERO
    gain_upper: Fraction | None = None
    # For the concept every_reachable_state: the state s of condition N_s
    # (index of the game) and the lower bound of the gain from s.
    state: Any = None
    state_gain_lower: Fraction = ZERO
    # Whether PD+ or PD− decided the player with the coupled bounds.
    coupled: bool = False


def player_on_cell(game: Game, profile: str, player: str, cell: tuple, dists: Callable,
                   epsilon: Fraction, every_state: bool = False,
                   use_advantages: bool | None = None, use_coupling: bool | None = None,
                   table: "CornerTable | None" = None) -> PlayerOnCell | None:
    """The conditions of I.9 for one player on a cell, or None when the
    relaxed process of the player has a policy that misses the target
    (then nothing is established for the cell). With `every_state`, the
    conditions for the concept every_reachable_state: E2, E1 at every
    state of R_C(p), and N at the initial state or N_s at a state that is
    in Reach_p at every valuation of the cell. Where E1 and E2 fail, PD+
    is tried before N and PD− after it (`advantage_bounds`), unless
    `use_advantages` (default: `USE_ADVANTAGES`) is False."""
    proc = build_process(game, player, profile, dists)
    if improper_states(proc):
        return None
    lo, hi = reward_bounds(game, proc, cell)
    follow = _profile(proc)
    lower = optimal_values(proc, follow, lo, maximize=False)      # l_p(s)
    upper = optimal_values(proc, follow, hi, maximize=True)       # u_p(s)
    out = PlayerOnCell(player=player)
    # E2: the upper bound of a one-step deviation against the lower bound
    # of the profile in the same state.
    pairs = []
    for i in range(len(proc.states)):
        if proc.target[i] or not proc.mine[i]:
            continue
        for k, ds in proc.options[i]:
            if k == proc.selected[i]:
                continue
            pairs.append((one_step_upper(proc, i, k, hi, upper), lower[i]))
    out.e2 = condition_e2(pairs)
    if out.e2:
        out.gain_upper = ZERO
        return out
    best = optimal_values(proc, _all(proc), hi, maximize=True)    # b_p(s)
    b = best[0]
    out.gain_upper = b - lower[0]
    if every_state:
        out.e1 = all(condition_e1(best[i], lower[i], epsilon)
                     for i in range(len(proc.states)) if not proc.target[i])
    else:
        out.e1 = condition_e1(b, lower[0], epsilon)
    if out.e1:
        return out
    if use_advantages is None:
        use_advantages = USE_ADVANTAGES
    adv = advantage_bounds(game, proc, cell, lower, upper) if use_advantages else None
    if adv is not None and pd_plus(proc, out, adv, epsilon, every_state):
        return out
    # N, with the canonical best response at the centre of the cell as τ.
    centre = tuple((a + b_) / 2 for a, b_ in cell)
    at = player_at_point(game, profile, player, centre)
    if at.admissible and at.deviation:
        d_tau = deviation_lower(proc, at.deviation, lo)
        out.gain_lower = max(ZERO, d_tau - upper[0])
        if condition_n(d_tau, upper[0], epsilon):
            out.n = True
            out.deviation = at.deviation
            if every_state:
                out.state, out.state_gain_lower = 0, d_tau - upper[0]
            return out
        if adv is not None and pd_minus(proc, out, adv, at, epsilon, every_state):
            return out
    if adv is not None and coupling_applies(game) and \
            (USE_COUPLING if use_coupling is None else use_coupling):
        # PD+ and PD− again, with the bounds of the advantages narrowed by
        # the coupled relaxation (I.9): the upper bounds that are positive
        # for PD+, and the lower bounds of the commands of τ for PD−. The
        # bounds hold at the centre of the cell, so PD+ cannot hold when
        # the player gains more than ε there, and PD− cannot hold when it
        # does not; the condition that cannot hold is not tried.
        plays = CoupledPlays(game, proc, table or CornerTable(game, cell))
        adv = dict(adv)
        gain_at_centre = at.state_gain if every_state else at.gain
        if not at.admissible or gain_at_centre <= epsilon:
            narrowed = False
            for key, (a, b) in adv.items():
                if b > 0 and plays.may_narrow(*key, lower, upper):
                    c = _coupled_bound(plays, key, True)
                    if c is not None and c < b:
                        adv[key] = (a, c)
                        narrowed = True
            if narrowed and pd_plus(proc, out, adv, epsilon, every_state):
                out.coupled = True
                return out
        elif at.deviation or every_state:
            narrowed = False
            tau = at.best_response if every_state else at.deviation
            local = {g: i for i, g in enumerate(proc.states)}
            for g, k in tau.items():
                key = (local.get(g), k)
                if key in adv and plays.may_narrow(*key, lower, upper):
                    a, b = adv[key]
                    c = _coupled_bound(plays, key, False)
                    if c is not None and c > a:
                        adv[key] = (c, b)
                        narrowed = True
            if narrowed and at.deviation and \
                    pd_minus(proc, out, adv, at, epsilon, every_state):
                out.coupled = True
                return out
    if every_state and at.admissible and at.state_gain > epsilon:
        state_deviation(game, profile, proc, out, at, lo, upper, cell, epsilon, adv)
    return out


def _coupled_bound(plays: CoupledPlays, key: tuple, maximize: bool) -> Fraction | None:
    """A coupled bound of an advantage, or None when a corner of the lifted
    cell that it needs is not usable (`CouplingUnavailable`)."""
    try:
        return plays.bound(*key, maximize=maximize)
    except CouplingUnavailable:
        return None


def pd_plus(proc: Process, out: PlayerOnCell, adv: dict, epsilon: Fraction,
            every_state: bool) -> bool:
    """PD+ of I.9 with the bounds `adv` of the advantages: the largest sum
    of their upper bounds along a play of the relaxed process bounds
    V*_p(s) − V_p(s). Records the bound of the gain in `out` and returns
    whether the player is shown not to gain more than ε."""
    gain_up = optimal_values(proc, _all(proc), advantage_reward(proc, adv, 1),
                             maximize=True)
    if out.gain_upper is None or max(ZERO, gain_up[0]) < out.gain_upper:
        out.gain_upper = max(ZERO, gain_up[0])
    free = [i for i in range(len(proc.states)) if not proc.target[i]]
    if all(gain_up[i] <= 0 for i in free):
        out.e2 = True          # V*_p = V_p on Reach_p
        out.gain_upper = ZERO
        return True
    if all(gain_up[i] <= epsilon for i in (free if every_state else [0])):
        out.e1 = True
        return True
    return False


def pd_minus(proc: Process, out: PlayerOnCell, adv: dict, at: PlayerAtPoint,
             epsilon: Fraction, every_state: bool) -> bool:
    """PD− of I.9 at the initial state with the bounds `adv` and τ the
    canonical best response at the centre of the cell: the least sum of
    the lower bounds of the advantages of τ along a play."""
    d_adv = optimal_values(proc, _strategy(proc, at.deviation),
                           advantage_reward(proc, adv, 0), maximize=False)[0]
    out.gain_lower = max(out.gain_lower, d_adv)
    if d_adv > epsilon:
        out.n = True
        out.deviation = at.deviation
        if every_state:
            out.state, out.state_gain_lower = 0, d_adv
        return True
    return False


def state_deviation(game: Game, profile: str, proc: Process, out: PlayerOnCell,
                    at: PlayerAtPoint, lo: Callable, upper: list, cell: tuple,
                    epsilon: Fraction, adv: dict | None = None) -> None:
    """Condition N_s of I.9 (concept every_reachable_state): a state s
    that is in Reach_p at every valuation of the cell (`certified_reach`)
    and a strategy τ of p with d_p(τ, s) − u_p(s) > ε, where d_p(τ, s) is
    a lower bound of the payoff of p from s when p plays τ and u_p(s) an
    upper bound of V_p(s). τ is the canonical best response at the centre
    of the cell, restricted to the states that the relaxed process reaches
    from s under it; s is the first such state in the order of S."""
    d = optimal_values(proc, _strategy(proc, at.best_response), lo, maximize=False)
    gain = [x - y for x, y in zip(d, upper)]
    if adv is not None:
        # PD− from s: the least sum of the lower bounds of the advantages
        # of τ along a play of the relaxed process from s.
        d_adv = optimal_values(proc, _strategy(proc, at.best_response),
                               advantage_reward(proc, adv, 0), maximize=False)
        gain = [max(x, y) for x, y in zip(gain, d_adv)]
    reach = certified_reach(game, profile, out.player, cell)
    model = game.model
    candidates = sorted((i for i, g in enumerate(proc.states)
                         if g in reach and not proc.target[i] and gain[i] > epsilon),
                        key=lambda i: _state_key(model, game.states[proc.states[i]]))
    if not candidates:
        return
    i = candidates[0]
    out.n = True
    out.state, out.state_gain_lower = proc.states[i], gain[i]
    out.deviation = reachable_deviation(proc, at.best_response, i)


@dataclass
class EqCell:
    box: dict                          # parameter -> (lo, hi)
    depth: int = 0
    status: str = "undecided"          # equilibrium | not_equilibrium | undecided
    deviators: list = field(default_factory=list)
    from_every_state: Any = None
    reason: str | None = None          # of an undecided cell
    deviations: dict = field(default_factory=dict)   # deviator -> strategy (list, I.13)
    gain_bounds: dict = field(default_factory=dict)  # player -> (lower, upper)
    method: str | None = None          # ENDPOINT_METHOD for a cell certified from end points
    # Concept every_reachable_state: deviator -> (the state s of N_s as a
    # dict (I.13), a lower bound of the gain from s on the cell).
    deviation_states: dict = field(default_factory=dict)

    def claim(self) -> tuple:
        return (self.status, tuple(self.deviators), self.from_every_state, self.reason,
                repr(self.deviations), self.method,
                repr({p: g for p, (g, _) in self.deviation_states.items()}))


def cell_verdict(game: Game, query: EquilibriumQuery, cell: EqCell) -> None:
    """Decide one cell by the conditions of I.9; when condition A of the
    end-point certification holds for the map, a cell that they leave
    undecided, and deviators they do not establish, are decided from the
    end points (I.9)."""
    bound_verdict(game, query, cell)
    if game.endpoints is None or cell.status == "equilibrium":
        return
    trial = EqCell(box=cell.box, depth=cell.depth)
    if not endpoint_verdict(game, query, trial):
        return
    if cell.status != "not_equilibrium":
        _copy_claim(trial, cell)
        return
    if trial.status != "not_equilibrium":
        return                         # cannot happen: both methods are sound
    # Deviators from both methods; each with its own deviation and bounds.
    for p in trial.deviators:
        if p not in cell.deviators:
            cell.deviators.append(p)
            cell.deviations[p] = trial.deviations[p]
            cell.gain_bounds[p] = trial.gain_bounds[p]
            if p in trial.deviation_states:
                cell.deviation_states[p] = trial.deviation_states[p]
            cell.method = f"{METHOD_SHORT} and {ENDPOINT_METHOD}"
    order = [p.id for p in game.model.players]
    cell.deviators.sort(key=order.index)


METHOD_SHORT = "parameter lifting (I.9)"


def _copy_claim(src: EqCell, dst: EqCell) -> None:
    dst.status, dst.deviators, dst.method = src.status, list(src.deviators), src.method
    dst.from_every_state, dst.reason = src.from_every_state, src.reason
    dst.deviations, dst.gain_bounds = dict(src.deviations), dict(src.gain_bounds)
    dst.deviation_states = dict(src.deviation_states)


def bound_verdict(game: Game, query: EquilibriumQuery, cell: EqCell) -> None:
    """Decide one cell by the conditions of I.9, or leave it `undecided`."""
    box = tuple(cell.box[n] for n in game.names)
    every = query.concept == "every_reachable_state"
    try:
        dists = cell_distributions(game, box)
        table = CornerTable(game, box) if coupling_applies(game) else None
        players = []
        for p in game.model.players:
            on = player_on_cell(game, query.profile, p.id, box, dists, query.epsilon, every,
                                table=table)
            if on is None:
                # The face-centre check of I-9 settles admissibility on the
                # region for a design with multi-affine probabilities; the
                # relaxation is then only too coarse on this cell.
                cell.status = "undecided"
                cell.reason = "bounds" if game.multi_affine else "not_admissible"
                return
            players.append(on)
    except _Unknown as e:
        cell.status, cell.reason = "undecided", str(e)
        return
    deviators = [on.player for on in players if on.n]
    bounds = {on.player: (on.gain_lower, on.gain_upper) for on in players}
    if deviators:
        cell.gain_bounds = bounds
        cell.status, cell.deviators = "not_equilibrium", deviators
        cell.deviations = {on.player: _deviation_list(game, on.deviation)
                           for on in players if on.n}
        if every:
            cell.deviation_states = {
                on.player: (state_dict(game.model, game.states[on.state]), on.state_gain_lower)
                for on in players if on.n and on.state is not None}
    elif all(on.e1 or on.e2 for on in players):
        cell.status, cell.gain_bounds = "equilibrium", bounds
        cell.from_every_state = True if all(on.e2 for on in players) or \
            (every and query.epsilon == 0) else None
    else:
        cell.status, cell.reason = "undecided", "bounds"


def _split(cell: EqCell, region: dict) -> list:
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
        out.append(EqCell(box=box, depth=cell.depth + 1))
    return out


def merge_cells(cells: list, names: list) -> list:
    """Join cells that carry the same claim and together form a box,
    until none can be joined; the result is in the lexicographic order of
    the lower corners."""
    cells = list(cells)
    changed = True
    while changed:
        changed = False
        for name in reversed(names):
            groups: dict = {}
            for c in cells:
                key = (c.claim(), tuple(c.box[n] for n in names if n != name))
                groups.setdefault(key, []).append(c)
            merged = []
            for group in groups.values():
                group.sort(key=lambda c: c.box[name])
                current = group[0]
                for nxt in group[1:]:
                    if current.box[name][1] == nxt.box[name][0]:
                        box = dict(current.box)
                        box[name] = (current.box[name][0], nxt.box[name][1])
                        bounds = {}
                        for p, (lo, hi) in current.gain_bounds.items():
                            lo2, hi2 = nxt.gain_bounds.get(p, (lo, hi))
                            bounds[p] = (min(lo, lo2), max(hi, hi2))
                        states = {p: (g, min(x, nxt.deviation_states[p][1]))
                                  for p, (g, x) in current.deviation_states.items()}
                        current = EqCell(
                            box=box, depth=min(current.depth, nxt.depth),
                            status=current.status, deviators=current.deviators,
                            from_every_state=current.from_every_state,
                            reason=current.reason, deviations=current.deviations,
                            gain_bounds=bounds, method=current.method,
                            deviation_states=states)
                        changed = True
                    else:
                        merged.append(current)
                        current = nxt
                merged.append(current)
            cells = merged
    cells.sort(key=lambda c: tuple(c.box[n][0] for n in names))
    return cells


def prepare_map(model: Model, query: EquilibriumQuery, design: Design,
                overrides: dict | None) -> Game:
    """The game of a design for an equilibrium map, after the checks of
    I-9 at the values used (I.13): the design is well-defined at the
    corner points of the region and the profile admissible at the centre
    of every face."""
    for name in overrides or {}:
        if name in query.over:
            raise EvaluationError(
                f"'{name}' is open in the equilibrium map '{query.id}' and cannot be set")
    theta = valuation(model, design, overrides)
    names = list(query.over)

    def at_text(point: tuple) -> str:
        return ", ".join(f"{n}={decimal_text(x)}" for n, x in zip(names, point))

    for corner in itertools.product(*[query.over[n] for n in names]):
        at = dict(theta)
        at.update(zip(names, corner))
        why = ill_defined(model, design.name, at)
        if why:
            raise EvaluationError(
                f"the design {design.name} is not well-defined at {at_text(corner)}: {why}")
    game = build_game(model, design, names, theta)
    for point in face_centres(query.over):
        at = dict(theta)
        at.update(zip(names, point))
        why = ill_defined(model, design.name, at)
        if why:
            raise EvaluationError(
                f"the design {design.name} is not well-defined at {at_text(point)}: {why}")
        for p in model.players:
            bad = admissibility_witness(game, query.profile, p.id, point)
            if bad is not None:
                raise EvaluationError(
                    f"the profile '{query.profile}' is not admissible for the player "
                    f"'{p.id}' in the design {design.name} at {at_text(point)} (I-9): from "
                    f"the state {show_state(model, bad)} the player can avoid the target "
                    f"of its payoff for ever")
    return game


def design_map(game: Game, query: EquilibriumQuery, max_depth: int = DEFAULT_MAX_DEPTH,
               max_cells: int = DEFAULT_MAX_CELLS, partition: int | None = None,
               merge: bool = True, use_endpoints: bool = True) -> dict:
    """The equilibrium map of one design (I.9): the largest undecided
    cell is bisected until the undecided fraction is at most the
    resolution or the budget is exhausted. With `partition`, the map
    starts from the uniform partition of the region into that many
    intervals per open parameter. With `use_endpoints`, cells that the
    bounds leave undecided are certified from their end points where
    condition A of I.9 holds."""
    names = game.names
    region = {n: tuple(query.over[n]) for n in names}
    endpoints = None
    if use_endpoints:
        endpoints = endpoint_setup(game, query, game.model.design(game.design))
    total = _volume(region)
    if partition is None or partition == 1:
        roots = [EqCell(box=dict(region))]
    else:
        axes = [[(lo + (hi - lo) * Fraction(i, partition), lo + (hi - lo) * Fraction(i + 1, partition))
                 for i in range(partition)] for lo, hi in (region[n] for n in names)]
        roots = [EqCell(box=dict(zip(names, combo))) for combo in itertools.product(*axes)]
    leaves: list = []
    heap: list = []
    seq = 0
    undecided = ZERO
    analysed = 0
    deepest = 0

    def settle(cell: EqCell) -> None:
        nonlocal seq, undecided, analysed
        cell_verdict(game, query, cell)
        analysed += 1
        if cell.status != "undecided":
            leaves.append(cell)
            return
        v = _volume(cell.box)
        undecided += v
        if cell.depth >= max_depth:
            leaves.append(cell)
            return
        seq += 1
        heapq.heappush(heap, (-v, seq, cell))

    for root in roots:
        settle(root)
    cells = len(roots)
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
            deepest = max(deepest, child.depth)
            settle(child)
        cells += 1
    leaves += [cell for _, _, cell in heap]
    if merge:
        leaves = merge_cells(leaves, names)
    else:
        leaves.sort(key=lambda c: tuple(c.box[n][0] for n in names))
    fractions = {s: sum((_volume(c.box) for c in leaves if c.status == s), ZERO) / total
                 for s in ("equilibrium", "not_equilibrium", "undecided")}
    return {
        "design": game.design,
        "parameters": {k: v for k, v in game.theta.items() if k not in names},
        "method": METHOD + (f"; cells that these conditions leave undecided are certified "
                            f"from exact evaluations at their end points (I.9)"
                            if game.endpoints is not None else ""),
        "endpoints": endpoints,
        "cells": leaves,
        "undecided_fraction": undecided / total,
        "resolution_reached": undecided <= query.resolution * total,
        "stopped": stopped,
        "fractions": fractions,
        "cells_before_merging": cells,
        "cells_analysed": analysed,
        "deepest": deepest,
        "multi_affine": game.multi_affine,
    }


def equilibrium_map(model: Model, query_id: str, regime: str | None = None,
                    overrides: dict | None = None, max_depth: int = DEFAULT_MAX_DEPTH,
                    max_cells: int = DEFAULT_MAX_CELLS, partition: int | None = None,
                    merge: bool = True, use_endpoints: bool = True) -> dict:
    """The result of an equilibrium map query (I.9): one certified map
    per listed design. Raises `EvaluationError` when the conditions of
    I-9 fail at the values used, and `NotEvaluated` when a payoff cannot
    be evaluated (I-7)."""
    query = _query(model, query_id)
    if not query.is_map:
        raise EvaluationError(f"'{query_id}' is an equilibrium check (it has no over:)")
    if max_depth < 0:
        raise EvaluationError("--max-depth must be at least 0")
    if max_cells < 1:
        raise EvaluationError("--max-cells must be at least 1")
    if partition is not None and partition < 1:
        raise EvaluationError("--partition must be at least 1")
    if partition is not None and partition ** len(query.over) > max_cells:
        raise EvaluationError(
            f"a partition into {partition} intervals per parameter has "
            f"{partition ** len(query.over)} cells, more than --max-cells {max_cells}")
    _require_payoffs(model)
    designs = []
    for design in _designs(model, query, regime):
        game = prepare_map(model, query, design, overrides)
        designs.append(design_map(game, query, max_depth, max_cells, partition, merge,
                                  use_endpoints))
    return {"id": query.id, "profile": query.profile, "epsilon": query.epsilon,
            "concept": query.concept, "assumptions": link_assumptions(model),
            "over": dict(query.over), "resolution": query.resolution,
            "max_depth": max_depth, "max_cells": max_cells, "designs": designs}


# ---------------------------------------------------------------------------
# Results as JSON and text (I.13)
# ---------------------------------------------------------------------------

def exact(value: Fraction) -> str:
    """An exact number as a string: an integer, or `n/d` in lowest terms."""
    from .stochastic import fraction_text
    return fraction_text(value)


def _concept_json(result: dict) -> dict:
    """`concept` is written for a query that names a concept other than
    the default `nash` (I.13)."""
    concept = result.get("concept", "nash")
    return {} if concept == "nash" else {"concept": concept}


def _player_json(p: dict) -> dict:
    out = {"player": p["player"], "payoff": exact(p["payoff"]),
           "best_response_value": exact(p["best_response_value"]),
           "gain": exact(p["gain"])}
    if "state_gain" in p:
        out["state_gain"] = exact(p["state_gain"])
        out["state"] = p["state"]
    out["best_response"] = p["best_response"]
    return out


def check_json(result: dict) -> dict:
    return {
        "id": result["id"], "profile": result["profile"], "epsilon": exact(result["epsilon"]),
        **_concept_json(result),
        **({"assumptions": result["assumptions"]} if result.get("assumptions") else {}),
        "designs": [
            {
                "design": d["design"],
                "parameters": {k: exact(v) for k, v in d["parameters"].items()},
                "status": d["status"],
                "deviators": list(d["deviators"]),
                "from_every_state": d["from_every_state"],
                "players": [_player_json(p) for p in d["players"]],
            }
            for d in result["designs"]
        ],
    }


def _cell_json(cell: EqCell) -> dict:
    out: dict = {"box": {n: [exact(lo), exact(hi)] for n, (lo, hi) in cell.box.items()},
                 "status": cell.status}
    if cell.method:
        out["method"] = cell.method
    if cell.status == "not_equilibrium":
        out["deviators"] = list(cell.deviators)
        out["deviations"] = cell.deviations
        if cell.deviation_states:
            out["deviation_states"] = {
                p: {"state": state, "gain_lower": exact(x)}
                for p, (state, x) in cell.deviation_states.items()}
    elif cell.status == "equilibrium":
        out["from_every_state"] = cell.from_every_state
    else:
        out["reason"] = cell.reason
    if cell.status != "undecided":
        out["gain_bounds"] = {p: [exact(lo), None if hi is None else exact(hi)]
                              for p, (lo, hi) in cell.gain_bounds.items()}
    return out


def _endpoints_json(endpoints: dict) -> dict:
    out = {"applicable": endpoints["applicable"]}
    if endpoints["applicable"]:
        out["parameter"] = endpoints["parameter"]
        out["non_increasing"] = list(endpoints["decreasing"])
        out["non_decreasing"] = list(endpoints["increasing"])
    else:
        out["reason"] = endpoints["reason"]
    return out


def map_json(result: dict) -> dict:
    return {
        "id": result["id"], "profile": result["profile"], "epsilon": exact(result["epsilon"]),
        **_concept_json(result),
        **({"assumptions": result["assumptions"]} if result.get("assumptions") else {}),
        "over": {n: [exact(lo), exact(hi)] for n, (lo, hi) in result["over"].items()},
        "resolution": exact(result["resolution"]),
        "designs": [
            {
                "design": d["design"],
                "parameters": {k: exact(v) for k, v in d["parameters"].items()},
                "method": d["method"],
                **({"endpoints": _endpoints_json(d["endpoints"])} if d.get("endpoints") else {}),
                "cells": [_cell_json(c) for c in d["cells"]],
                "undecided_fraction": exact(d["undecided_fraction"]),
                "resolution_reached": d["resolution_reached"],
                "stopped": d["stopped"],
                "budget": {"max_depth": result["max_depth"], "max_cells": result["max_cells"]},
                "counts": {
                    **{s: sum(1 for c in d["cells"] if c.status == s)
                       for s in ("equilibrium", "not_equilibrium", "undecided")},
                    "cells": len(d["cells"]),
                    "cells_before_merging": d["cells_before_merging"],
                    "cells_analysed": d["cells_analysed"],
                    "deepest_bisection": d["deepest"],
                },
                "volume_fractions": {k: exact(v) for k, v in d["fractions"].items()},
            }
            for d in result["designs"]
        ],
    }


def _state_text(state: dict) -> str:
    def text(x: Any) -> str:
        return str(x).lower() if isinstance(x, bool) else str(x)
    return "(" + ", ".join(f"{k}={text(v)}" for k, v in state.items()) + ")"


def _strategy_text(strategy: list) -> str:
    return "; ".join(f"{s['action']} in {_state_text(s['state'])}" for s in strategy)


def check_text(result: dict) -> list:
    out = [f"--- Equilibrium check '{result['id']}': profile '{result['profile']}', "
           f"epsilon = {decimal_text(result['epsilon'])}{_concept_text(result)} ---"]
    out += [f"  assumption (J.7): {a}" for a in result.get("assumptions", [])]
    for d in result["designs"]:
        params = ", ".join(f"{k}={decimal_text(v)}" for k, v in d["parameters"].items())
        out.append(f"  Design {d['design']} ({params})")
        for p in d["players"]:
            out.append(f"    {p['player']}: payoff = {value_text(p['payoff'])}  (exact: "
                       f"{exact(p['payoff'])})")
            out.append(f"      best-response value = {value_text(p['best_response_value'])}"
                       f"  (exact: {exact(p['best_response_value'])})")
            out.append(f"      gain = {value_text(p['gain'])}  (exact: {exact(p['gain'])})")
            if "state_gain" in p:
                line = (f"      largest gain from a state of Reach_p = "
                        f"{value_text(p['state_gain'])}  (exact: {exact(p['state_gain'])})")
                if p["state"] is not None:
                    line += f", from {_state_text(p['state'])}"
                out.append(line)
            if p["best_response"]:
                out.append(f"      canonical best response"
                           f"{' from that state' if 'state_gain' in p else ''}: "
                           f"{_strategy_text(p['best_response'])}")
        line = f"    status: {d['status']}"
        if d["deviators"]:
            line += f"; deviators: {', '.join(d['deviators'])}"
        if d["from_every_state"] is not None:
            line += f"; from_every_state: {str(d['from_every_state']).lower()}"
        out.append(line)
    if not result["designs"]:
        out.append("  (the query does not concern this design)")
    return out


def _concept_text(result: dict) -> str:
    concept = result.get("concept", "nash")
    return "" if concept == "nash" else f", concept {concept}"


def _box_text(box: dict) -> str:
    return ", ".join(f"{n} in [{decimal_text(lo)}, {decimal_text(hi)}]"
                     for n, (lo, hi) in box.items())


def map_text(result: dict) -> list:
    out = [f"--- Equilibrium map '{result['id']}': profile '{result['profile']}', "
           f"epsilon = {decimal_text(result['epsilon'])}{_concept_text(result)}; "
           f"{MAP_NOTE} ---",
           f"  region: {_box_text(result['over'])}; resolution: "
           f"{decimal_text(result['resolution'])}"]
    out += [f"  assumption (J.7): {a}" for a in result.get("assumptions", [])]
    for d in result["designs"]:
        cells = d["cells"]
        counts = {s: sum(1 for c in cells if c.status == s)
                  for s in ("equilibrium", "not_equilibrium", "undecided")}
        params = ", ".join(f"{k}={decimal_text(v)}" for k, v in d["parameters"].items())
        out.append(f"  Design {d['design']} ({params})")
        out.append(f"    method: {d['method']}")
        ends = d.get("endpoints")
        if ends and ends["applicable"]:
            out.append(f"    end points (I.9): condition A holds for '{ends['parameter']}'; the "
                       f"gain is non-increasing for: {', '.join(ends['decreasing']) or 'no player'}"
                       f"; non-decreasing for: {', '.join(ends['increasing']) or 'no player'}")
        elif ends:
            out.append(f"    end points (I.9) not used: {ends['reason']}")
        out.append(f"    cells: {len(cells)} ({counts['equilibrium']} equilibrium, "
                   f"{counts['not_equilibrium']} not_equilibrium, {counts['undecided']} "
                   f"undecided); {d['cells_analysed']} cell(s) analysed, deepest bisection "
                   f"{d['deepest']}")
        out.append(f"    equilibrium: {value_text(d['fractions']['equilibrium'])} of the "
                   f"region; not_equilibrium: {value_text(d['fractions']['not_equilibrium'])}")
        out.append(f"    undecided fraction: {value_text(d['undecided_fraction'])} (exact: "
                   f"{exact(d['undecided_fraction'])})")
        if d["resolution_reached"]:
            out.append("    The resolution is reached.")
        else:
            why = {"max_depth": f"no undecided cell may be bisected again (--max-depth "
                                f"{result['max_depth']})",
                   "max_cells": f"the partition has {d['cells_before_merging']} cells "
                                f"(--max-cells {result['max_cells']})"}[d["stopped"]]
            out.append(f"    The resolution is NOT reached: {why}.")
        for c in cells:
            if c.status == "not_equilibrium":
                what = f"not_equilibrium, deviators: " + ", ".join(
                    p + (f" (from {_state_text(c.deviation_states[p][0])})"
                         if p in c.deviation_states else "") for p in c.deviators)
            elif c.status == "equilibrium":
                what = "equilibrium" + (", from_every_state: true"
                                        if c.from_every_state else "")
            else:
                what = f"undecided ({c.reason})"
            if c.method:
                what += f" [{c.method}]"
            out.append(f"    {_box_text(c.box)}: {what}")
    if not result["designs"]:
        out.append("  (the query does not concern this design)")
    return out
