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
  `stochastic_regions`), the bounds l, u, b, d(τ) and q, and the
  sufficient conditions N, E1 and E2 (`cell_verdict`); and the map
  itself by bisection (`equilibrium_map`).

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
          "conditions N, E1 and E2 from the exact optimal values of the relaxed processes")
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
    # The end-point certification of J-14, when its conditions hold for the
    # map being computed (`j14_setup`).
    j14: Any = None
    _selection: dict = field(default_factory=dict)
    _target: dict = field(default_factory=dict)
    _rho: dict = field(default_factory=dict)

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


def reachable_deviation(proc: Process, tau: dict) -> dict:
    """τ restricted to the states that are reachable when the player
    follows τ and the others the profile."""
    choose = _strategy(proc, tau)
    seen = {0}
    todo = [0]
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
    if out.gain > 0:
        tau = canonical_best_response(proc, v_star, reward)
        out.deviation = reachable_deviation(proc, tau)
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
# J-14: certification of a cell [a, b] from its end points
# ---------------------------------------------------------------------------

J14_METHOD = "endpoints (J-14)"


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


def j14_parameter_free_model(model: Model, design: Design, name: str) -> str | None:
    """Condition 2 of J-14, first part: ℓ occurs in no `prob:` (of a
    command of the design) and in no reward `value:`. None when it holds,
    otherwise why not."""
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


def j14_affine_weights(model: Model, name: str) -> str | None:
    """Condition 2 of J-14, second part: in every weight of every player,
    the written ones and those defined by J.7, ℓ occurs with a power of
    at most one, read on the text."""
    for payoff in model.payoffs:
        for term in payoff.terms:
            weight = term_weight(model, payoff, term)
            if syntactic_degree(weight, name) > 1:
                return (f"'{name}' occurs with a power above one in the weight of the term "
                        f"'{term.reward}' of the payoff of '{payoff.player}'")
    return None


def j14_condition_ii(game: Game, profile: str, player: str, name: str) -> bool:
    """Condition 5 of J-14 (condition (ii) of J.7 for p): every term of p
    in whose weight ℓ occurs has a weight −m·ℓ with m ≥ 0, and its reward
    structure has the expected value 0 under the profile, computed
    exactly."""
    model = game.model
    payoff = payoff_of(model, player)
    point = (game.j14["region"][0],)
    for term in payoff.terms:
        weight = term_weight(model, payoff, term)
        if name not in param_names(weight):
            continue
        poly = polynomial(weight, game.theta, [name])
        if any(e != (1,) for e in poly) or poly.get((1,), ZERO) > 0:
            return False
        if _expected_in(game, profile, player, term.reward, point) != 0:
            return False
    return True


def j14_setup(game: Game, query: EquilibriumQuery, design: Design) -> dict:
    """Whether J-14 may be used for the map in this design, and the
    players for whom condition 5 holds."""
    out: dict = {"applicable": False, "reason": None, "condition_ii": []}
    if len(query.over) != 1:
        out["reason"] = "the query has more than one open parameter"
        return out
    name = next(iter(query.over))
    why = j14_parameter_free_model(game.model, design, name) \
        or j14_affine_weights(game.model, name)
    if why:
        out["reason"] = why
        return out
    out.update(applicable=True, parameter=name, region=query.over[name])
    game.j14 = out
    out["condition_ii"] = [p.id for p in game.model.players
                           if j14_condition_ii(game, query.profile, p.id, name)]
    return out


def endpoint_verdict(game: Game, query: EquilibriumQuery, cell: "EqCell") -> bool:
    """Decide a cell [a, b] by J-14 when the end points allow a claim;
    True when the cell got a claim. The cell is `equilibrium` when the
    profile is an ε-equilibrium at a and at b; a player for whom
    condition 5 holds is a deviator when its gain at b exceeds ε."""
    (a, b), = [cell.box[n] for n in game.names]
    at_a, at_b = {}, {}
    for p in game.model.players:
        at_a[p.id] = player_at_point(game, query.profile, p.id, (a,))
        at_b[p.id] = player_at_point(game, query.profile, p.id, (b,))
        if not (at_a[p.id].admissible and at_b[p.id].admissible):
            return False
    eps = query.epsilon
    if all(at_a[p].gain <= eps and at_b[p].gain <= eps for p in at_a):
        cell.status, cell.method = "equilibrium", J14_METHOD
        # gain_p is convex in ℓ (J.7, proposition, part 1).
        cell.gain_bounds = {p: (ZERO, max(at_a[p].gain, at_b[p].gain)) for p in at_a}
        return True
    deviators = [p for p in at_a
                 if p in game.j14["condition_ii"] and at_b[p].gain > eps]
    if not deviators:
        return False
    cell.status, cell.deviators, cell.method = "not_equilibrium", deviators, J14_METHOD
    # gain_p is non-increasing in ℓ under (ii) (part 2): [gain(b), gain(a)].
    cell.gain_bounds = {p: (at_b[p].gain, at_a[p].gain) for p in deviators}
    cell.deviations = {p: _deviation_list(game, at_b[p].deviation) for p in deviators}
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
        deviators = [at.player for at in players if at.gain > query.epsilon]
        status = "not_equilibrium" if deviators else "equilibrium"
        from_every_state = None
        if status == "equilibrium" and query.epsilon == 0:
            from_every_state = all(at.one_step for at in players)
        designs.append({
            "design": design.name,
            "parameters": theta,
            "status": status,
            "deviators": deviators,
            "from_every_state": from_every_state,
            "players": [
                {"player": at.player, "payoff": at.payoff,
                 "best_response_value": at.best_response_value, "gain": at.gain,
                 "best_response": (_deviation_list(game, at.deviation)
                                   if at.gain > query.epsilon else [])}
                for at in players
            ],
        })
    return {"id": query.id, "profile": query.profile, "epsilon": query.epsilon,
            "assumptions": link_assumptions(model), "designs": designs}


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


@dataclass
class PlayerOnCell:
    player: str
    e1: bool = False
    e2: bool = False
    n: bool = False
    deviation: dict = field(default_factory=dict)    # the τ of N
    gain_lower: Fraction = ZERO
    gain_upper: Fraction | None = None


def player_on_cell(game: Game, profile: str, player: str, cell: tuple, dists: Callable,
                   epsilon: Fraction) -> PlayerOnCell | None:
    """The conditions of I.9 for one player on a cell, or None when the
    relaxed process of the player has a policy that misses the target
    (then nothing is established for the cell)."""
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
    b = optimal_values(proc, _all(proc), hi, maximize=True)[0]    # b_p
    out.gain_upper = b - lower[0]
    out.e1 = condition_e1(b, lower[0], epsilon)
    if out.e1:
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
    return out


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
    method: str | None = None          # J14_METHOD for a cell certified by J-14

    def claim(self) -> tuple:
        return (self.status, tuple(self.deviators), self.from_every_state, self.reason,
                repr(self.deviations), self.method)


def cell_verdict(game: Game, query: EquilibriumQuery, cell: EqCell) -> None:
    """Decide one cell by the conditions of I.9; when the conditions of
    J-14 hold for the map, a cell that they leave undecided, and deviators
    they do not establish, are decided from the end points (J-14)."""
    bound_verdict(game, query, cell)
    if game.j14 is None or cell.status == "equilibrium":
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
            cell.method = f"{METHOD_SHORT} and {J14_METHOD}"
    order = [p.id for p in game.model.players]
    cell.deviators.sort(key=order.index)


METHOD_SHORT = "parameter lifting (I.9)"


def _copy_claim(src: EqCell, dst: EqCell) -> None:
    dst.status, dst.deviators, dst.method = src.status, list(src.deviators), src.method
    dst.from_every_state, dst.reason = src.from_every_state, src.reason
    dst.deviations, dst.gain_bounds = dict(src.deviations), dict(src.gain_bounds)


def bound_verdict(game: Game, query: EquilibriumQuery, cell: EqCell) -> None:
    """Decide one cell by the conditions of I.9, or leave it `undecided`."""
    box = tuple(cell.box[n] for n in game.names)
    try:
        dists = cell_distributions(game, box)
        players = []
        for p in game.model.players:
            on = player_on_cell(game, query.profile, p.id, box, dists, query.epsilon)
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
    elif all(on.e1 or on.e2 for on in players):
        cell.status, cell.gain_bounds = "equilibrium", bounds
        cell.from_every_state = True if all(on.e2 for on in players) else None
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
                        current = EqCell(
                            box=box, depth=min(current.depth, nxt.depth),
                            status=current.status, deviators=current.deviators,
                            from_every_state=current.from_every_state,
                            reason=current.reason, deviations=current.deviations,
                            gain_bounds=bounds, method=current.method)
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
               merge: bool = True, use_j14: bool | None = None) -> dict:
    """The equilibrium map of one design (I.9): the largest undecided
    cell is bisected until the undecided fraction is at most the
    resolution or the budget is exhausted. With `partition`, the map
    starts from the uniform partition of the region into that many
    intervals per open parameter."""
    names = game.names
    region = {n: tuple(query.over[n]) for n in names}
    j14 = None
    if use_j14 is None:
        # J-14 is a rule of the grounding extension (J.8).
        use_j14 = game.model.grounding
    if use_j14:
        j14 = j14_setup(game, query, game.model.design(game.design))
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
        "method": METHOD + (f"; where the conditions of J-14 hold, cells are certified from "
                            f"exact evaluations at their end points ({J14_METHOD})"
                            if game.j14 is not None else ""),
        "j14": j14,
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
                    merge: bool = True, use_j14: bool | None = None) -> dict:
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
                                  use_j14))
    return {"id": query.id, "profile": query.profile, "epsilon": query.epsilon,
            "assumptions": link_assumptions(model),
            "over": dict(query.over), "resolution": query.resolution,
            "max_depth": max_depth, "max_cells": max_cells, "designs": designs}


# ---------------------------------------------------------------------------
# Results as JSON and text (I.13)
# ---------------------------------------------------------------------------

def exact(value: Fraction) -> str:
    """An exact number as a string: an integer, or `n/d` in lowest terms."""
    return str(Fraction(value))


def check_json(result: dict) -> dict:
    return {
        "id": result["id"], "profile": result["profile"], "epsilon": exact(result["epsilon"]),
        **({"assumptions": result["assumptions"]} if result.get("assumptions") else {}),
        "designs": [
            {
                "design": d["design"],
                "parameters": {k: exact(v) for k, v in d["parameters"].items()},
                "status": d["status"],
                "deviators": list(d["deviators"]),
                "from_every_state": d["from_every_state"],
                "players": [
                    {"player": p["player"], "payoff": exact(p["payoff"]),
                     "best_response_value": exact(p["best_response_value"]),
                     "gain": exact(p["gain"]), "best_response": p["best_response"]}
                    for p in d["players"]
                ],
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
    elif cell.status == "equilibrium":
        out["from_every_state"] = cell.from_every_state
    else:
        out["reason"] = cell.reason
    if cell.status != "undecided":
        out["gain_bounds"] = {p: [exact(lo), None if hi is None else exact(hi)]
                              for p, (lo, hi) in cell.gain_bounds.items()}
    return out


def _j14_json(j14: dict) -> dict:
    out = {"applicable": j14["applicable"]}
    if j14["applicable"]:
        out["parameter"] = j14["parameter"]
        out["condition_ii"] = list(j14["condition_ii"])
    else:
        out["reason"] = j14["reason"]
    return out


def map_json(result: dict) -> dict:
    return {
        "id": result["id"], "profile": result["profile"], "epsilon": exact(result["epsilon"]),
        **({"assumptions": result["assumptions"]} if result.get("assumptions") else {}),
        "over": {n: [exact(lo), exact(hi)] for n, (lo, hi) in result["over"].items()},
        "resolution": exact(result["resolution"]),
        "designs": [
            {
                "design": d["design"],
                "parameters": {k: exact(v) for k, v in d["parameters"].items()},
                "method": d["method"],
                **({"j14": _j14_json(d["j14"])} if d.get("j14") else {}),
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
           f"epsilon = {decimal_text(result['epsilon'])} ---"]
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
            if p["best_response"]:
                out.append(f"      canonical best response: "
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


def _box_text(box: dict) -> str:
    return ", ".join(f"{n} in [{decimal_text(lo)}, {decimal_text(hi)}]"
                     for n, (lo, hi) in box.items())


def map_text(result: dict) -> list:
    out = [f"--- Equilibrium map '{result['id']}': profile '{result['profile']}', "
           f"epsilon = {decimal_text(result['epsilon'])}; {MAP_NOTE} ---",
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
        j14 = d.get("j14")
        if j14 and j14["applicable"]:
            out.append(f"    J-14 applies (open parameter '{j14['parameter']}'; condition (ii) "
                       f"holds for: {', '.join(j14['condition_ii']) or 'no player'})")
        elif j14:
            out.append(f"    J-14 does not apply: {j14['reason']}")
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
                what = f"not_equilibrium, deviators: {', '.join(c.deviators)}"
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
