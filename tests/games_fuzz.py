"""Soundness fuzzing of the equilibrium maps of the strategic model
extension (Appendix I, I.9).

`generate` writes a small random parametric turn-based game as a CADL
file: 2 or 3 players (3 or 4 with `mode="scale"`, with three open
parameters in most maps), cycles, signed payoff weights with parameters,
parameters in probabilities (also with a power above one), and regions
that touch probability 0 or 1. With `concept="every"` (or `"both"`) the
queries ask for an equilibrium from every reachable state. `Oracle` evaluates the same game
directly from the generated data, sharing no code with `cadl`: it
enumerates every memoryless deterministic strategy of a player and
solves each Markov chain by Gaussian elimination over fractions.

`check_model` computes the equilibrium map with `cadl` and checks every
decided cell at exact rational points (corners, face centres, the
centre, random points):

- the design is well-defined and the profile admissible at the point;
- `equilibrium`: no player gains more than ε by any deviation (for the
  concept every_reachable_state: from any state of Reach_p);
- `from_every_state: true`: no player gains from any state of Reach_p;
- `not_equilibrium`: every listed deviator gains more than ε, and so
  does the reported deviation (for every_reachable_state: from the
  reported state, which must be in Reach_p at the point), within the
  reported bounds.

Run as a script for a long run::

    python tests/games_fuzz.py --seed 0 --count 400 [--mode scale] [--concept every]
                                [--mutation NAME]

`MUTATIONS` are deliberate soundness bugs; the fuzzer must detect each.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import random
import sys
import time
from fractions import Fraction

import yaml

F = Fraction

# (text, function of the valuation); every one lies in [0, 1] for p, q in [0, 1].
PROBS = [
    ("0.5", lambda v: F(1, 2)),
    ("0.25", lambda v: F(1, 4)),
    ("p", lambda v: v["p"]),
    ("q", lambda v: v["q"]),
    ("0.5 * p", lambda v: v["p"] / 2),
    ("0.3 + 0.4 * p", lambda v: F(3, 10) + F(2, 5) * v["p"]),
    ("p * q", lambda v: v["p"] * v["q"]),
    ("p * p", lambda v: v["p"] ** 2),                       # not multi-affine
    ("p * (1 - p)", lambda v: v["p"] * (1 - v["p"])),       # not multi-affine
    ("1 - p * q", lambda v: 1 - v["p"] * v["q"]),
]
WEIGHTS = [
    ("1", lambda v: F(1)),
    ("-1", lambda v: F(-1)),
    ("2", lambda v: F(2)),
    ("-3", lambda v: F(-3)),
    ("lam", lambda v: v["lam"]),
    ("-2 * lam", lambda v: -2 * v["lam"]),
    ("1 - lam", lambda v: 1 - v["lam"]),
    ("lam * lam", lambda v: v["lam"] ** 2),                 # not multi-affine
    ("p - lam", lambda v: v["p"] - v["lam"]),
    ("-p", lambda v: -v["p"]),
]
# mode="nonaffine": more probabilities that are not multi-affine (the
# lifting of the coupled bounds, I.9); every one lies in [0, 1].
PROBS_NONAFFINE = [
    ("p * p", lambda v: v["p"] ** 2),
    ("1 - p * p", lambda v: 1 - v["p"] ** 2),
    ("4 * p * (1 - p)", lambda v: 4 * v["p"] * (1 - v["p"])),
    ("p * q * (1 - q)", lambda v: v["p"] * v["q"] * (1 - v["q"])),
    ("q * q", lambda v: v["q"] ** 2),
    ("p * p * q", lambda v: v["p"] ** 2 * v["q"]),
    ("(1 - p) * (1 - p)", lambda v: (1 - v["p"]) ** 2),
    ("0.5 + 0.5 * p * (1 - q) * q", lambda v: F(1, 2) + v["p"] * (1 - v["q"]) * v["q"] / 2),
]
# J-14 mode: λ (`lam`, in [0, 2]) in a probability (violates condition 2)
PROBS_LAM = [
    ("0.5 * lam", lambda v: v["lam"] / 2),
    ("0.25 + 0.25 * lam", lambda v: F(1, 4) + v["lam"] / 4),
    ("0.25 * lam * lam", lambda v: v["lam"] ** 2 / 4),
    ("0.5 * lam * (2 - lam)", lambda v: v["lam"] * (2 - v["lam"]) / 2),
    ("lam * (2 - lam)", lambda v: v["lam"] * (2 - v["lam"])),
]
# J-14 mode: weights affine in λ, sanction-like weights, and some that are
# not affine (violate condition 2).
WEIGHTS_J14 = [
    ("1", lambda v: F(1)),
    ("-1", lambda v: F(-1)),
    ("3", lambda v: F(3)),
    ("-2 * lam", lambda v: -2 * v["lam"]),
    ("-0.5 * lam", lambda v: -v["lam"] / 2),
    ("lam", lambda v: v["lam"]),                            # a reward: (ii) fails
    ("1 - lam", lambda v: 1 - v["lam"]),
    ("2 * p - lam", lambda v: 2 * v["p"] - v["lam"]),
]
WEIGHTS_NOT_AFFINE = [
    ("-1 * lam * lam", lambda v: -v["lam"] ** 2),
    ("lam * (2 - lam)", lambda v: v["lam"] * (2 - v["lam"])),
    ("lam * lam - lam", lambda v: v["lam"] ** 2 - v["lam"]),
]
REGIONS = {
    "p": [(F(0), F(1)), (F(0), F(1, 2)), (F(1, 4), F(3, 4)), (F(1, 2), F(1)), (F(1, 10), F(9, 10))],
    "q": [(F(0), F(1)), (F(1, 4), F(1))],
    "lam": [(F(0), F(1)), (F(0), F(2)), (F(1, 2), F(3, 2))],
}
RANGES = {"p": (0, 1), "q": (0, 1), "lam": (0, 2)}


def generate(rng: random.Random, j14: bool = False, mode: str | None = None,
             concept: str = "nash") -> dict:
    """A random game, as plain data. With `j14` (or `mode="endpoints"`), a
    game for the end-point certification of I.9: λ is the only open
    parameter, the weights are mostly affine in λ, and some games break
    condition A (λ in a probability, a weight that is not affine in λ);
    `j14` writes a file that declares grounding 0.1, `mode="endpoints"`
    one that does not. `mode="scale"`: 3 or 4 players and, in most games,
    three open parameters."""
    if mode == "endpoints":
        j14 = True
    lam_in_prob = j14 and rng.random() < 0.3
    not_affine = j14 and rng.random() < 0.3
    probs = PROBS_LAM + PROBS_LAM + PROBS[:4] if lam_in_prob else PROBS
    nonaffine = mode == "nonaffine"
    if nonaffine:
        probs = PROBS_NONAFFINE + PROBS_NONAFFINE + PROBS
    weights = WEIGHTS_NOT_AFFINE + WEIGHTS_NOT_AFFINE + WEIGHTS_J14 if not_affine else \
        WEIGHTS_J14 if j14 else WEIGHTS
    n = rng.choice([3, 4, 4, 5])                 # states 0..n-1; n is the goal
    players = ["A", "B"] if rng.random() < 0.6 else ["A", "B", "C"]
    if mode == "scale":
        n = rng.choice([4, 5, 5, 6])
        players = ["A", "B", "C"] if rng.random() < 0.5 else ["A", "B", "C", "D"]
    states = []
    for i in range(n):
        owner = rng.choice(players + players + [None])
        count = 1 if owner is None else rng.choice([1, 2, 2, 3])
        cmds = []
        for k in range(count):
            forward = rng.randrange(i + 1, n + 1)
            any_ = rng.randrange(0, n + 1)
            shape = rng.random()
            if shape < 0.15:
                updates = [(None, forward if rng.random() < 0.7 else any_)]
            elif shape < 0.7:
                prob = rng.choice(probs)
                targets = [forward, any_]
                rng.shuffle(targets)
                updates = [(prob, targets[0]), (None, targets[1])]
            else:
                second = rng.choice(PROBS_NONAFFINE) if nonaffine and rng.random() < 0.6 \
                    else rng.choice(PROBS[2:6]) if rng.random() < 0.8 else PROBS[4]
                updates = [(("0.2", lambda v: F(1, 5)), forward),
                           (second, rng.randrange(0, n + 1)),
                           (None, any_)]
                # 0.2 + e <= 1 needs e <= 0.8: halve the second probability.
                text, fn = updates[1][0]
                updates[1] = ((f"0.5 * ({text})", lambda v, fn=fn: fn(v) / 2), updates[1][1])
            if nonaffine and k > 0 and rng.random() < 0.5:
                # The probabilities of the first command of the state, with
                # other successors (as the attempts with and without care).
                first = cmds[0]["updates"]
                succs = [u[1] for u in updates] + [rng.randrange(0, n + 1) for _ in first]
                updates = [(prob, succ) for (prob, _), succ in zip(first, succs)]
            cmds.append({"action": f"a{i}_{k}", "updates": updates})
        states.append({"owner": owner, "cmds": cmds})
    actions = [c["action"] for s in states for c in s["cmds"]]
    rewards = []
    for r in range(rng.choice([2, 3, 4])):
        items = []
        for _ in range(rng.choice([1, 2, 3])):
            if rng.random() < 0.8:
                items.append({"action": rng.choice(actions), "loc": None,
                              "value": rng.choice([1, 1, 2, 3])})
            else:
                items.append({"action": None, "loc": rng.randrange(n),
                              "value": rng.choice([1, 2])})
        rewards.append({"id": f"r{r}", "items": items})
    payoffs = {}
    for pl in players:
        target = {n}
        if rng.random() < 0.25 and n > 2:
            target.add(rng.randrange(1, n))
        ids = rng.sample([r["id"] for r in rewards], rng.choice([1, 2, min(3, len(rewards))]))
        payoffs[pl] = {"target": target,
                       "terms": [(rid, rng.choice(weights)) for rid in ids]}
    sigma = [rng.randrange(len(s["cmds"])) for s in states]
    open_ = ["p", "lam"]
    kind = rng.random()
    if j14:
        open_ = ["lam"]
        region = {"lam": rng.choice([(F(0), F(2)), (F(0), F(2)), (F(0), F(1)),
                                     (F(1, 2), F(3, 2))])}
    elif kind < 0.2:
        open_ = ["lam"]
    elif kind < 0.35:
        open_ = ["p"]
    elif kind < 0.5:
        open_ = ["p", "q", "lam"]
    if mode == "scale" and rng.random() < 0.7:
        open_ = ["p", "q", "lam"]
    if nonaffine:
        open_ = rng.choice([["p", "q", "lam"], ["p", "q", "lam"], ["p", "lam"], ["p", "q"],
                            ["p"]])
    if not j14:
        region = {name: rng.choice(REGIONS[name]) for name in open_}
    fixed = {"p": rng.choice([F(1, 2), F(3, 10), F(1)]), "q": rng.choice([F(1, 2), F(4, 5)]),
             "lam": rng.choice([F(1, 2), F(1), F(0)])}
    for name, (lo, hi) in region.items():
        if not lo <= fixed[name] <= hi:
            fixed[name] = (lo + hi) / 2
    epsilon = rng.choice([F(0), F(0), F(0), F(1, 4), F(1)])
    if concept == "both":
        concept = "every" if rng.random() < 0.5 else "nash"
    return {"j14": j14, "grounding": j14 and mode != "endpoints", "n": n, "players": players,
            "states": states, "rewards": rewards, "payoffs": payoffs, "sigma": sigma,
            "region": region, "fixed": fixed, "epsilon": epsilon,
            "concept": concept}


def _num(x: Fraction):
    return int(x) if x.denominator == 1 else float(x)


def to_cadl(g: dict) -> str:
    """The game as a CADL file (games 0.1)."""
    n = g["n"]
    commands = []
    for i, s in enumerate(g["states"]):
        for c in s["cmds"]:
            updates = []
            for prob, succ in c["updates"]:
                u = {"set": {"loc": succ}}
                if prob is not None:
                    u["prob"] = prob[0]
                elif len(c["updates"]) > 1:
                    u["prob"] = "rest"
                updates.append(u)
            cmd = {"action": c["action"], "guard": f"loc == {i}", "updates": updates}
            if s["owner"] is not None:
                cmd["owner"] = s["owner"]
            commands.append(cmd)
    rewards = []
    for r in g["rewards"]:
        items = []
        for it in r["items"]:
            item = {"value": it["value"]}
            if it["action"] is not None:
                item["action"] = it["action"]
            if it["loc"] is not None:
                item["guard"] = f"loc == {it['loc']}"
            items.append(item)
        rewards.append({"id": r["id"], "items": items})
    payoffs = []
    for pl in g["players"]:
        pay = g["payoffs"][pl]
        until = " OR ".join(f"loc == {t}" for t in sorted(pay["target"]))
        payoffs.append({"player": pl, "until": until,
                        "terms": [{"reward": rid, "weight": w[0]} for rid, w in pay["terms"]]})
    rules = [{"choose": s["cmds"][k]["action"]}
             for s, k in zip(g["states"], g["sigma"]) if s["owner"] is not None]
    over = {name: [_num(lo), _num(hi)] for name, (lo, hi) in g["region"].items()}
    model = {
        "kind": "smg",
        "parameters": [{"id": name, "range": list(RANGES[name]), "value": _num(g["fixed"][name])}
                       for name in ("p", "q", "lam")],
        "variables": [{"id": "loc", "type": f"[0..{n}]", "init": 0}],
        "players": [{"id": pl, "actors": [f"ACT_{pl}"]} for pl in g["players"]],
        "commands": commands,
        "rewards": rewards,
        "profiles": {"sigma": rules},
        "compliance": "sigma",
        "payoffs": payoffs,
        "equilibria": [
            {"id": "at_fixed", "epsilon": _num(g["epsilon"])},
            {"id": "on_region", "epsilon": _num(g["epsilon"]), "over": over,
             "resolution": 0.05},
        ],
    }
    if g.get("concept") == "every":
        for entry in model["equilibria"]:
            entry["concept"] = "every_reachable_state"
    sos = {"sos": {
        "name": "FuzzGame", "type": "Acknowledged",
        "extensions": [{"stochastic": 0.1}, {"games": 0.1}]
        + ([{"norms": 0.1}, {"grounding": 0.1}] if g.get("grounding", g.get("j14")) else []),
        "actors": [{"id": f"ACT_{pl}", "role": "x"} for pl in g["players"]],
        "model": model,
    }}
    return yaml.safe_dump(sos, sort_keys=False)


# ---------------------------------------------------------------------------
# The oracle: brute force, independent of cadl
# ---------------------------------------------------------------------------

def _gauss(a: list, b: list) -> list:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = next(r for r in range(c, n) if m[r][c] != 0)
        m[c], m[piv] = m[piv], m[c]
        pv = m[c][c]
        m[c] = [x / pv for x in m[c]]
        for r in range(n):
            if r != c and m[r][c] != 0:
                f = m[r][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] for i in range(n)]


class Oracle:
    """The game at one valuation."""

    def __init__(self, g: dict, v: dict) -> None:
        self.g, self.v, self.n = g, v, g["n"]
        self.well_defined = True
        self.trans = []                       # per state, per command: {succ: prob > 0}
        for s in g["states"]:
            per = []
            for c in s["cmds"]:
                probs = [None if prob is None else prob[1](v) for prob, _ in c["updates"]]
                rest = 1 - sum(x for x in probs if x is not None)
                probs = [rest if x is None else x for x in probs]
                if any(not 0 <= x <= 1 for x in probs):
                    self.well_defined = False
                d: dict = {}
                for x, (_, succ) in zip(probs, c["updates"]):
                    if x != 0:
                        d[succ] = d.get(succ, 0) + x
                per.append(d)
            self.trans.append(per)

    def step(self, pl: str, i: int, k: int) -> Fraction:
        action = self.g["states"][i]["cmds"][k]["action"]
        total = F(0)
        for rid, (_, w) in self.g["payoffs"][pl]["terms"]:
            reward = next(r for r in self.g["rewards"] if r["id"] == rid)
            got = sum(it["value"] for it in reward["items"]
                      if (it["action"] is None or it["action"] == action)
                      and (it["loc"] is None or it["loc"] == i))
            total += w(self.v) * got
        return total

    def strategies(self, pl: str) -> list:
        """Every profile in which pl deviates in any way and the others
        follow sigma: lists of one command per state."""
        g = self.g
        options = [range(len(s["cmds"])) if s["owner"] == pl else [g["sigma"][i]]
                   for i, s in enumerate(g["states"])]
        return [list(c) for c in itertools.product(*options)]

    def reach(self, pl: str, start: int = 0) -> set:
        """Reach_p from `start`: through every command of pl and sigma elsewhere."""
        g, target = self.g, self.g["payoffs"][pl]["target"]
        seen, todo = {start}, [start]
        while todo:
            i = todo.pop()
            if i in target or i >= self.n:
                continue
            ks = range(len(g["states"][i]["cmds"])) if g["states"][i]["owner"] == pl \
                else [g["sigma"][i]]
            for k in ks:
                for j in self.trans[i][k]:
                    if j not in seen:
                        seen.add(j)
                        todo.append(j)
        return seen

    def value(self, pl: str, choice: list, start: int = 0):
        """The payoff of pl from `start` under one command per state, or
        None when the target is not reached with probability 1."""
        target = self.g["payoffs"][pl]["target"]
        if start in target:
            return F(0)
        seen, todo = {start}, [start]
        while todo:
            i = todo.pop()
            if i in target:
                continue
            if i >= self.n:
                return None                   # the goal is a deadlock outside the target
            for j in self.trans[i][choice[i]]:
                if j not in seen:
                    seen.add(j)
                    todo.append(j)
        inner = sorted(i for i in seen if i not in target)
        # proper: every inner state reaches the target
        can = set(i for i in seen if i in target)
        changed = True
        while changed:
            changed = False
            for i in inner:
                if i not in can and any(j in can for j in self.trans[i][choice[i]]):
                    can.add(i)
                    changed = True
        if any(i not in can for i in inner):
            return None
        pos = {i: k for k, i in enumerate(inner)}
        a = [[F(0)] * len(inner) for _ in inner]
        b = []
        for i in inner:
            a[pos[i]][pos[i]] += 1
            for j, x in self.trans[i][choice[i]].items():
                if j in pos:
                    a[pos[i]][pos[j]] -= x
            b.append(self.step(pl, i, choice[i]))
        return _gauss(a, b)[pos[start]]

    def analyse(self, pl: str) -> dict:
        """Admissibility, payoff, best-response value, and whether pl gains
        from some state of Reach_p."""
        sigma = list(self.g["sigma"])
        strategies = self.strategies(pl)
        states = sorted(self.reach(pl))
        values = {s: [self.value(pl, c, s) for c in strategies] for s in states}
        if any(x is None for xs in values.values() for x in xs):
            return {"admissible": False}
        index = strategies.index(sigma)
        return {
            "admissible": True,
            "payoff": values[0][index],
            "best": max(values[0]),
            "gain": max(values[0]) - values[0][index],
            "gain_somewhere": any(max(values[s]) > values[s][index] for s in states),
            # The largest gain from a state of Reach_p (every_reachable_state).
            "state_gain": max(max(values[s]) - values[s][index] for s in states),
            "reach": set(states),
            "profile_values": {s: values[s][index] for s in states},
        }


# ---------------------------------------------------------------------------
# Checking a map
# ---------------------------------------------------------------------------

class Unsound(AssertionError):
    pass


def sample_points(box: dict, rng: random.Random, extra: int) -> list:
    names = list(box)
    axes = [(lo, (lo + hi) / 2, hi) for lo, hi in (box[n] for n in names)]
    points = [dict(zip(names, combo)) for combo in itertools.product(*axes)]
    for _ in range(extra):
        points.append({n: lo + (hi - lo) * F(rng.randint(0, 97), 97)
                       for n, (lo, hi) in box.items()})
    return points


def _tau_choice(g: dict, deviation: list) -> list:
    choice = list(g["sigma"])
    for entry in deviation:
        i = entry["state"]["loc"]
        choice[i] = next(k for k, c in enumerate(g["states"][i]["cmds"])
                         if c["action"] == entry["action"])
    return choice


def check_cell(g: dict, cell, epsilon: Fraction, rng: random.Random, extra: int) -> int:
    """Check one decided cell at sample points; returns the number of points."""
    every = g.get("concept") == "every"
    points = sample_points(cell.box, rng, extra)
    for point in points:
        v = dict(g["fixed"])
        v.update(point)
        oracle = Oracle(g, v)
        where = f"cell {cell.box} ({cell.status}) at {point}"
        if not oracle.well_defined:
            raise Unsound(f"not well-defined in {where}")
        for pl in g["players"]:
            a = oracle.analyse(pl)
            if not a["admissible"]:
                raise Unsound(f"profile not admissible for {pl} in {where}")
            if cell.status == "equilibrium":
                if a["gain"] > epsilon:
                    raise Unsound(f"{pl} gains {a['gain']} > {epsilon} in {where}")
                if every and a["state_gain"] > epsilon:
                    raise Unsound(f"{pl} gains {a['state_gain']} > {epsilon} from a state of "
                                  f"Reach_p in {where}")
                if cell.from_every_state and a["gain_somewhere"]:
                    raise Unsound(f"{pl} gains from some state of Reach_p in {where}, "
                                  f"but from_every_state is true")
                hi = cell.gain_bounds.get(pl, (0, None))[1]
                if hi is not None and a["gain"] > hi:
                    raise Unsound(f"gain bound {hi} of {pl} misses {a['gain']} in {where}")
            elif pl in cell.deviators:
                tau = _tau_choice(g, cell.deviations[pl])
                lo, hi = cell.gain_bounds[pl]
                if not lo <= a["gain"] <= hi:
                    raise Unsound(f"gain bounds [{lo}, {hi}] of {pl} miss {a['gain']} in {where}")
                if every and pl in cell.deviation_states:
                    state, lower = cell.deviation_states[pl]
                    s = state["loc"]
                    if s not in a["reach"]:
                        raise Unsound(f"the state {s} of {pl} is not in Reach_p in {where}")
                    got = oracle.value(pl, tau, s)
                    if got is None or not got - a["profile_values"][s] > epsilon \
                            or got - a["profile_values"][s] < lower:
                        raise Unsound(f"the reported deviation of {pl} from {s} does not gain "
                                      f"more than {epsilon} (at least {lower}) in {where}")
                    continue
                if not a["gain"] > epsilon:
                    raise Unsound(f"the deviator {pl} gains only {a['gain']} in {where}")
                got = oracle.value(pl, tau)
                if got is None or not got - a["payoff"] > epsilon:
                    raise Unsound(f"the reported deviation of {pl} does not gain more "
                                  f"than {epsilon} in {where}")
        if cell.status == "not_equilibrium" and not cell.deviators:
            raise Unsound(f"no deviator in {where}")
    return len(points)


def check_endpoint_cells(g: dict, model, rng: random.Random, extra: int,
                         stats: dict) -> int:
    """The end-point certification of I.9 directly: the end-point verdict
    on the whole region, its halves and quarters, and random
    sub-intervals, each claim checked at sample points (the map itself
    uses the end points only where the bounds leave a cell open)."""
    from cadl.games_eval import EqCell, endpoint_setup, endpoint_verdict, prepare_map
    query = next(q for q in model.equilibria if q.id == "on_region")
    design = model.designs[0]
    game = prepare_map(model, query, design, None)
    endpoint_setup(game, query, design)
    if game.endpoints is None:
        return 0
    name, = g["region"]
    lo, hi = g["region"][name]
    cuts = [lo + (hi - lo) * F(k, 4) for k in range(5)]
    boxes = [(lo, hi), (cuts[0], cuts[2]), (cuts[2], cuts[4])]
    boxes += [(cuts[k], cuts[k + 1]) for k in range(4)]
    for _ in range(3):
        a, b = sorted(lo + (hi - lo) * F(rng.randint(0, 64), 64) for _ in range(2))
        if a < b:
            boxes.append((a, b))
    points = 0
    for a, b in boxes:
        cell = EqCell(box={name: (a, b)})
        if endpoint_verdict(game, query, cell):
            key = f"direct_endpoints_{cell.status}"
            stats[key] = stats.get(key, 0) + 1
            points += check_cell(g, cell, g["epsilon"], rng, extra)
    return points


def check_coupled_advantages(g: dict, model, rng: random.Random, extra: int,
                             stats: dict) -> int:
    """The coupled bounds of the advantages of I.9 directly: on the whole
    region and on random sub-boxes, every bound of `CoupledPlays` is
    checked against the advantage Q_p(s, c) − V_p(s) that the oracle
    computes at sample points of the box at which s is in Reach_p.
    Returns the number of comparisons."""
    import cadl.games_eval as ge
    query = next(q for q in model.equilibria if q.id == "on_region")
    game = ge.prepare_map(model, query, model.designs[0], None)
    if not ge.coupling_applies(game):
        return 0
    region = {n: g["region"][n] for n in game.names}
    boxes = [dict(region)]
    for _ in range(2):
        box = {}
        for n, (lo, hi) in region.items():
            parts = rng.choice([1, 2, 4])
            k = rng.randrange(parts)
            box[n] = (lo + (hi - lo) * F(k, parts), lo + (hi - lo) * F(k + 1, parts))
        boxes.append(box)
    compared = lifted = 0
    for box in boxes:
        cell = tuple(box[n] for n in game.names)
        try:
            dists = ge.cell_distributions(game, cell)
        except ge._Unknown:
            continue
        table = ge.CornerTable(game, cell)
        points = sample_points(box, rng, extra)
        oracles = []
        for point in points:
            v = dict(g["fixed"])
            v.update(point)
            oracles.append((point, Oracle(g, v)))
        for pl in g["players"]:
            proc = ge.build_process(game, pl, query.profile, dists)
            if ge.improper_states(proc):
                continue
            plays = ge.CoupledPlays(game, proc, table)
            bounds = {}
            for i, gi in enumerate(proc.states):
                if proc.target[i] or not proc.mine[i]:
                    continue
                for k, _ in proc.options[i]:
                    if k is None or k == proc.selected[i]:
                        continue
                    loc = game.states[gi][0]
                    action = game.commands[gi][k].command.action
                    c = next(c for c, cmd in enumerate(g["states"][loc]["cmds"])
                             if cmd["action"] == action)
                    try:
                        bounds[(loc, c)] = (plays.bound(i, k, False), plays.bound(i, k, True))
                    except ge.CouplingUnavailable:
                        stats["advantages_unavailable"] = \
                            stats.get("advantages_unavailable", 0) + 1
            for point, oracle in oracles:
                a = oracle.analyse(pl)
                if not a["admissible"]:
                    raise Unsound(f"profile not admissible for {pl} at {point} in {box}, "
                                  f"but the relaxed process is proper")
                values = a["profile_values"]
                for (s, c), (lo, hi) in bounds.items():
                    if s not in a["reach"]:
                        continue
                    sigma = g["sigma"][s]
                    q = {k: oracle.step(pl, s, k) + sum(
                        (x * (values[j] if j in values else oracle.value(pl, g["sigma"], j))
                         for j, x in oracle.trans[s][k].items()), F(0))
                        for k in (c, sigma)}
                    adv = q[c] - q[sigma]
                    compared += 1
                    if not game.multi_affine:
                        lifted += 1
                    if not lo <= adv <= hi:
                        raise Unsound(f"coupled bounds [{lo}, {hi}] of the advantage of "
                                      f"{pl} for command {c} in state {s} miss {adv} "
                                      f"at {point} in {box}")
    stats["advantages_compared"] = stats.get("advantages_compared", 0) + compared
    stats["advantages_compared_lifted"] = stats.get("advantages_compared_lifted", 0) + lifted
    return compared


def check_model(g: dict, rng: random.Random, max_cells: int = 24, extra: int = 4,
                stats: dict | None = None, direct: bool = True) -> bool:
    """Run cadl on the game and check its results against the oracle.
    With `direct`, the coupled bounds of the advantages are also checked
    directly (`check_coupled_advantages`).
    Returns False when the file is rejected by the static rules (for
    example, the profile is not admissible)."""
    from cadl.games_eval import EvaluationError, equilibrium_check, equilibrium_map
    from cadl.parser import parse
    from cadl.stochastic import analyse

    stats = stats if stats is not None else {}

    def count(key: str, by: int = 1) -> None:
        stats[key] = stats.get(key, 0) + by

    analysis = analyse(parse(to_cadl(g)))
    if not analysis.ok:
        count("rejected")
        # The static rules must not reject a file whose profile is
        # admissible at the fixed values and at every face centre.
        return False
    model = analysis.model
    count("models")
    count(f"players_{len(g['players'])}")
    count(f"open_{len(g['region'])}")
    # The check at the fixed values against the oracle.
    oracle = Oracle(g, dict(g["fixed"]))
    result = equilibrium_check(model, "at_fixed")["designs"][0]
    for entry in result["players"]:
        a = oracle.analyse(entry["player"])
        assert a["admissible"]
        if (entry["payoff"], entry["best_response_value"], entry["gain"]) != \
                (a["payoff"], a["best"], a["gain"]):
            raise Unsound(f"check at the fixed values: {entry} against {a}")
        if entry["best_response"] and g.get("concept") != "every":
            got = oracle.value(entry["player"], _tau_choice(g, entry["best_response"]))
            if got != a["best"]:
                raise Unsound(f"the reported best response of {entry['player']} has the "
                              f"payoff {got}, not {a['best']}")
    every = g.get("concept") == "every"
    if every:
        for entry in result["players"]:
            a = oracle.analyse(entry["player"])
            if entry["state_gain"] != a["state_gain"]:
                raise Unsound(f"check at the fixed values: largest gain from a state "
                              f"{entry['state_gain']}, expected {a['state_gain']}")
            if entry["state"] is not None:
                s = entry["state"]["loc"]
                got = oracle.value(entry["player"], _tau_choice(g, entry["best_response"]), s)
                if s not in a["reach"] or got is None or \
                        got - a["profile_values"][s] != a["state_gain"]:
                    raise Unsound(f"check at the fixed values: the best response of "
                                  f"{entry['player']} from {s} is wrong")
    gains = [oracle.analyse(pl) for pl in g["players"]]
    key = "state_gain" if every else "gain"
    expected = "equilibrium" if all(a[key] <= g["epsilon"] for a in gains) \
        else "not_equilibrium"
    if result["status"] != expected:
        raise Unsound(f"check at the fixed values: {result['status']}, expected {expected}")
    if result["from_every_state"] is not None and \
            result["from_every_state"] != (not any(a["gain_somewhere"] for a in gains)):
        raise Unsound("check at the fixed values: from_every_state is wrong")
    count("checks")
    # The map; players decided by PD+ or PD− with the coupled bounds are
    # counted.
    import cadl.games_eval as ge
    original_on = ge.player_on_cell

    def counting(*args, **kwargs):
        on = original_on(*args, **kwargs)
        if on is not None and on.coupled:
            count("players_decided_by_coupling")
            if not args[0].multi_affine:
                count("players_decided_by_coupling_lifted")
        return on
    ge.player_on_cell = counting
    try:
        design = equilibrium_map(model, "on_region", max_cells=max_cells)["designs"][0]
    except EvaluationError as e:
        raise Unsound(f"the map raised: {e}") from e
    finally:
        ge.player_on_cell = original_on
    total = F(0)
    ends = design.get("endpoints")
    if ends is not None:
        count("endpoints_applicable" if ends["applicable"] else "endpoints_not_applicable")
    for cell in design["cells"]:
        if cell.method and "end points" in cell.method:
            count(f"cells_endpoints_{cell.status}")
        if cell.deviation_states and any(st["loc"] != 0 for st, _ in
                                         cell.deviation_states.values()):
            count("cells_deviation_from_a_later_state")
        volume = F(1)
        for lo, hi in cell.box.values():
            volume *= hi - lo
        total += volume
        count(f"cells_{cell.status}")
        if cell.status == "undecided":
            count(f"reason_{cell.reason}")
            continue
        if cell.status == "equilibrium" and cell.from_every_state:
            count("cells_from_every_state")
        count("points", check_cell(g, cell, g["epsilon"], rng, extra))
    if ends is not None and ends["applicable"]:
        count("points", check_endpoint_cells(g, model, rng, extra, stats))
    if direct and ge.USE_ADVANTAGES and ge.USE_COUPLING:
        check_coupled_advantages(g, model, rng, extra, stats)
    region = F(1)
    for lo, hi in g["region"].values():
        region *= hi - lo
    if total != region:
        raise Unsound(f"the cells have the volume {total}, the region {region}")
    if not design["multi_affine"]:
        count("models_not_multi_affine")
    return True


# ---------------------------------------------------------------------------
# Deliberate soundness bugs
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def mutation(name: str | None):
    """Install one deliberate soundness bug in `cadl.games_eval`."""
    import cadl.games_eval as ge
    saved = {k: getattr(ge, k) for k in
             ("deviation_lower", "improper_states", "one_step_upper", "cell_distributions",
              "reward_bounds", "endpoint_parameter_free_model", "endpoint_affine_weights",
              "endpoint_monotone_states", "certified_reach", "player_on_cell",
              "USE_ADVANTAGES", "advantage_bounds", "advantage_reward", "poly_bounds",
              "CoupledPlays", "_coupling", "CornerTable")}
    if name is not None and name not in ADVANTAGE_MUTATIONS + COUPLING_MUTATIONS \
            + LIFTING_MUTATIONS:
        # The other mutants concern N, E1, E2, N_s and the end points: the
        # conditions PD+ and PD− are switched off, so that the cells reach
        # the mutated condition as they did before PD+ and PD− existed.
        ge.USE_ADVANTAGES = False
    if name == "swap_bound_direction":
        # d(τ): the best case over the cell instead of the worst case.
        ge.deviation_lower = lambda proc, tau, lo: ge.optimal_values(
            proc, ge._strategy(proc, tau), lo, maximize=True)[0]
    elif name == "drop_admissibility":
        # Only the profile itself is required to reach the target.
        original = saved["improper_states"]
        ge.improper_states = lambda proc, choice=None: original(proc, ge._profile(proc))
    elif name == "profile_command_for_deviator":
        # q(s, c) is computed with the command of the profile.
        original_q = saved["one_step_upper"]
        ge.one_step_upper = lambda proc, i, k, hi, upper: original_q(
            proc, i, proc.selected[i], hi, upper)
    elif name == "lower_corner_only":
        # No relaxation of the probabilities: the lower corner of the cell.
        ge.cell_distributions = lambda game, cell: ge.point_distributions(
            game, tuple(lo for lo, _ in cell))
    elif name == "swap_reward_bounds":
        original_rb = saved["reward_bounds"]

        def swapped(game, proc, cell):
            lo, hi = original_rb(game, proc, cell)
            return hi, lo
        ge.reward_bounds = swapped
    elif name == "endpoints_drop_probability":
        # Condition A without "ℓ occurs in no prob:".
        ge.endpoint_parameter_free_model = lambda model, design, name: None
    elif name == "endpoints_drop_affine":
        # Condition A without "ℓ occurs with a power of at most one".
        ge.endpoint_affine_weights = lambda model, name: None
    elif name == "endpoints_drop_condition_b":
        # Condition B assumed for every player from every state.
        def everywhere(game, profile, player, name):
            states = set(range(len(game.states)))
            return {"decreasing": states, "increasing": set(states)}
        ge.endpoint_monotone_states = everywhere
    elif name == "endpoints_b_from_initial":
        # Condition B checked from s0 only and taken for every state.
        original_ms = saved["endpoint_monotone_states"]

        def from_initial(game, profile, player, name):
            sets = original_ms(game, profile, player, name)
            states = set(range(len(game.states)))
            return {k: (states if 0 in v else set()) for k, v in sets.items()}
        ge.endpoint_monotone_states = from_initial
    elif name == "drop_reach_certificate":
        # N_s: every state of R_C(p) taken to be in Reach_p on the whole cell.
        ge.certified_reach = lambda game, profile, player, cell: set(range(len(game.states)))
    elif name == "every_state_as_nash":
        # The conditions of I.9 for the concept nash also for
        # every_reachable_state (E1 at the initial state only, no N_s).
        original_on = saved["player_on_cell"]
        ge.player_on_cell = lambda game, profile, player, cell, dists, epsilon, every=False, \
            **options: original_on(game, profile, player, cell, dists, epsilon, **options)
    elif name == "advantage_drop_successors":
        # A(s, c) bounded by the difference of the step payoffs alone,
        # without the term Σ ΔP(s′) · V(s′).
        original_ab = saved["advantage_bounds"]

        def without_successors(game, proc, cell, lower, upper):
            zero = [F(0)] * len(lower)
            return original_ab(game, proc, cell, zero, zero)
        ge.advantage_bounds = without_successors
    elif name == "advantage_swap_sides":
        # PD+ with the lower bounds of the advantages, PD− with the upper.
        original_ar = saved["advantage_reward"]
        ge.advantage_reward = lambda proc, bounds, side: original_ar(proc, bounds, 1 - side)
    elif name == "advantage_centre_only":
        # The differences of payoffs and probabilities at the centre of the
        # cell only, not bounded on the cell.
        def at_centre(poly, cell):
            x = ge._p_eval(poly, tuple((a + b) / 2 for a, b in cell)) if poly else F(0)
            return x, x
        ge.poly_bounds = at_centre
    elif name == "coupling_swap_sides":
        # The coupled upper bound from the least value, the lower bound
        # from the largest.
        class Swapped(saved["CoupledPlays"]):
            def bound(self, i, k, maximize):
                return super().bound(i, k, not maximize)
        ge.CoupledPlays = Swapped
    elif name == "coupling_drop_first_payoff":
        # The difference of the step payoffs of c and σ(s) left out.
        class WithoutFirst(saved["CoupledPlays"]):
            def options(self, x):
                opts = super().options(x)
                if x[0] == "start":
                    return [(F(0), F(0), d) for _, _, d in opts]
                return opts
        ge.CoupledPlays = WithoutFirst
    elif name == "coupling_drop_later_payoffs":
        # The step payoffs after the first step left out.
        class FirstOnly(saved["CoupledPlays"]):
            def options(self, x):
                opts = super().options(x)
                if x[0] != "start":
                    return [(F(0), F(0), d) for _, _, d in opts]
                return opts
        ge.CoupledPlays = FirstOnly
    elif name == "lift_corners_only":
        # No lifting: a probability that is not multi-affine evaluated at the
        # corners of the cell, as if it were multi-affine.
        class CornersOnly(saved["CornerTable"]):
            def distribution(self, g, k, c):
                cmd = self.game.commands[g][k]
                point = self.corners[c][:len(self.cell)]
                out = {}
                for j, poly in zip(cmd.targets, cmd.row.polys):
                    x = ge._p_eval(poly, point)
                    if x != 0:
                        out[j] = out.get(j, F(0)) + x
                return out
        ge.CornerTable = CornersOnly
    elif name in ("lift_centre_range", "lift_lower_range"):
        # The interval of a lifted coordinate: the value of the lifted
        # function at the centre of the cell, or the lower half of its
        # enclosure, instead of the whole enclosure.
        class Narrow(saved["CornerTable"]):
            def __init__(self, game, cell):
                super().__init__(game, cell)
                centre = tuple((a + b) / 2 for a, b in cell)
                ranges = []
                for r in self.lifting.functions:
                    lo, hi = ge._p_interval(r, cell)
                    x = ge._p_eval(r, centre)
                    ranges.append((x, x) if name == "lift_centre_range" else (lo, (lo + hi) / 2))
                self.corners = list(itertools.product(
                    *[sorted({a, b}) for a, b in tuple(cell) + tuple(ranges)]))
        ge.CornerTable = Narrow
    elif name is not None:
        raise ValueError(name)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(ge, k, v)


MUTATIONS = ("swap_bound_direction", "drop_admissibility", "profile_command_for_deviator",
             "lower_corner_only", "swap_reward_bounds")
ENDPOINT_MUTATIONS = ("endpoints_drop_probability", "endpoints_drop_affine",
                      "endpoints_drop_condition_b", "endpoints_b_from_initial")
EVERY_STATE_MUTATIONS = ("drop_reach_certificate", "every_state_as_nash")
ADVANTAGE_MUTATIONS = ("advantage_drop_successors", "advantage_swap_sides",
                       "advantage_centre_only")
COUPLING_MUTATIONS = ("coupling_swap_sides", "coupling_drop_first_payoff",
                      "coupling_drop_later_payoffs")
# Bugs of the lifting of probabilities that are not multi-affine (I.9);
# they are found with mode="nonaffine".
LIFTING_MUTATIONS = ("lift_corners_only", "lift_centre_range", "lift_lower_range")


def run(seed: int, count: int, mutant: str | None = None, max_cells: int = 24,
        extra: int = 4, j14: bool = False, mode: str | None = None,
        concept: str = "nash", direct: bool = True) -> dict:
    """Generate and check `count` accepted models. Returns statistics;
    `stats["failure"]` describes the first violation, if any (also an
    exception raised by a mutant)."""
    rng = random.Random(seed)
    stats: dict = {}
    with mutation(mutant):
        done = 0
        tries = 0
        while done < count and tries < 50 * count:
            tries += 1
            g = generate(rng, j14, mode, concept)
            try:
                if check_model(g, rng, max_cells, extra, stats, direct):
                    done += 1
            except Unsound as e:
                stats["failure"] = f"model {tries} of seed {seed}: {e}"
                stats["failing_model"] = to_cadl(g)
                break
            except Exception as e:           # a mutant may also crash the solver
                if mutant is None:
                    raise
                stats["failure"] = f"model {tries} of seed {seed}: {type(e).__name__}: {e}"
                break
    return stats


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--max-cells", type=int, default=24)
    parser.add_argument("--extra", type=int, default=4)
    parser.add_argument("--mutation", default=None,
                        choices=MUTATIONS + ENDPOINT_MUTATIONS + EVERY_STATE_MUTATIONS
                        + ADVANTAGE_MUTATIONS + COUPLING_MUTATIONS + LIFTING_MUTATIONS)
    parser.add_argument("--no-direct", action="store_true",
                        help="do not check the coupled bounds of the advantages directly "
                             "(only the claims of the maps)")
    parser.add_argument("--j14", action="store_true",
                        help="one-parameter maps over lam in files that declare grounding "
                             "0.1 (the end-point certification of I.9, through J-14)")
    parser.add_argument("--mode", choices=("endpoints", "scale", "nonaffine"), default=None,
                        help="endpoints: one-parameter maps over lam in files without "
                             "grounding; scale: 3 or 4 players, mostly 3 open parameters; "
                             "nonaffine: mostly probabilities that are not multi-affine")
    parser.add_argument("--concept", choices=("nash", "every", "both"), default="nash")
    args = parser.parse_args(argv)
    start = time.time()
    stats = run(args.seed, args.count, args.mutation, args.max_cells, args.extra, args.j14,
                args.mode, args.concept, not args.no_direct)
    model = stats.pop("failing_model", None)
    print({k: stats[k] for k in sorted(stats)}, f"{time.time() - start:.1f}s")
    if model and args.mutation is None:
        print(model)
    return 1 if "failure" in stats else 0


if __name__ == "__main__":
    sys.exit(main())
