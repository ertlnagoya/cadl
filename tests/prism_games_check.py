"""Run the export of I.11 through PRISM-games and compare its answers with
the equilibrium checks of `cadl` (Appendix I, I.8).

The exported model file of a design is passed to PRISM-games unchanged,
followed by

- a module `cadl_profile_` that synchronises on the action labels of the
  players and lets a player take only the command that the profile
  selects, unless the constant `cadl_free_` is the player's number
  (1, 2, ... in declaration order; 0: every player follows the profile);
- per player p, a reward structure `cadl_shifted_<p>` that is not
  negative, because PRISM-games rejects negative rewards and a payoff
  has weights of either sign.

The payoff under the profile, V_p(s), is Σ_k w_k · E[r_k] from the
exported reward structures, each checked by
`<<p>> R{"r_k"}max=? [ F φ_p ]` with every player following the profile.
The best-response value V*_p(s) uses the shifted structure: on a state s
of Reach_p in which φ_p does not hold and a command c allowed in s, it has
the reward ρ_p(s, c) + h(s) − Σ_s′ P(s, c, s′) · h(s′) with h(s) = m · D(s),
D(s) the largest expected number of steps from s until φ_p and m the
largest −ρ_p(s, c). Along every play that reaches φ_p the added terms
telescope to h(s), whatever h is, so the maximum of the shifted structure
from s is V*_p(s) + h(s); this choice of h only makes every reward not
negative. ρ_p is evaluated here from the reward items; h, D and P are
those of `cadl`. A wrong probability in the export or in `cadl` breaks
the telescoping sum and shows up as a difference.

PRISM-games has no exact engine for games (`-exact` is refused for an
SMG), so its values are floating-point numbers from value iteration and
are compared with a relative tolerance.

Run as a script::

    python tests/prism_games_check.py [--examples] [--fuzz COUNT] [--seed S]
                                      [--prism PATH]
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from cadl import stochastic
from cadl.games import payoff_of, term_weight
from cadl.games_eval import (
    _all,
    _profile,
    build_game,
    build_process,
    equilibrium_check,
    optimal_values,
    player_at_point,
    point_distributions,
    point_reward,
)
from cadl.stochastic import (
    _prism_name,
    compile_state_expr,
    decimal_text,
    eval_param_expr,
    prism_model_text,
    tr,
)
from cadl.stochastic_eval import valuation

ROOT = Path(__file__).parent
EXAMPLES = sorted((ROOT.parent / "examples").glob("games_*.cadl"))
TOL = 1e-6
PRISM_OPTIONS = ("-e", "1e-12", "-maxiters", "1000000")


def find_prism_games() -> str | None:
    """The PRISM-games binary: $CADL_PRISM_GAMES, or `prism-games` on PATH."""
    path = os.environ.get("CADL_PRISM_GAMES")
    if path:
        return path if Path(path).exists() else None
    return shutil.which("prism-games")


class Games:
    """While active, names are renamed with the keywords of PRISM-games (I.11)."""

    def __enter__(self):
        self.saved = stochastic._extra_keywords
        stochastic._extra_keywords = stochastic.PRISM_GAMES_KEYWORDS

    def __exit__(self, *exc):
        stochastic._extra_keywords = self.saved


def _number(x: Fraction) -> str:
    d = x.denominator
    while d % 2 == 0:
        d //= 2
    while d % 5 == 0:
        d //= 5
    return decimal_text(x) if d == 1 else repr(float(x))


def _state_formula(model, state) -> str:
    parts = []
    for v, x in zip(model.variables, state):
        name = _prism_name(v.id)
        if v.kind == "bool":
            parts.append(name if x else f"!{name}")
        elif v.kind == "int":
            parts.append(f"{name}={x}")
        else:
            parts.append(f"{name}={_prism_name(f'{v.id}_{x}')}")
    return "(" + " & ".join(parts) + ")"


def _parse_state(model, text: str) -> tuple:
    out = []
    for v, x in zip(model.variables, text.split(",")):
        if v.kind == "bool":
            out.append(x == "true")
        elif v.kind == "int":
            out.append(int(x))
        else:
            out.append(v.literals[int(x)])
    return tuple(out)


def run_prism(prism: str, text: str, props: list, consts: str = "",
              options: tuple = PRISM_OPTIONS) -> str:
    with tempfile.TemporaryDirectory() as d:
        model_file = Path(d) / "model.prism"
        model_file.write_text(text)
        cmd = [prism, str(model_file)]
        if props:
            props_file = Path(d) / "model.props"
            props_file.write_text("\n".join(props) + "\n")
            cmd.append(str(props_file))
        cmd += list(options)
        if consts:
            cmd += ["-const", consts]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=900).stdout
    if re.search(r"^Error", out, re.M):
        raise RuntimeError(f"PRISM-games reports an error:\n{out[-4000:]}")
    return out


class DesignRun:
    """One design and one profile: the inputs for PRISM-games and its answers."""

    def __init__(self, model, design, profile: str, prism: str) -> None:
        self.model, self.design, self.profile, self.prism = model, design, profile, prism
        self.theta = valuation(model, design)
        self.game = build_game(model, design, [], self.theta)
        with Games():
            self.exported = prism_model_text(model, design)
            # A parameter that is open in a regime map is written without a value.
            self.consts = ",".join(f"{_prism_name(p.id)}={decimal_text(self.theta[p.id])}"
                                   for p in model.params if p.open)
            self.written = [p.id for p in model.players
                            if f"player {_prism_name(p.id)}\n" in self.exported]
            for name in ("cadl_profile_", "cadl_free_"):
                if re.search(rf"\b{name}\b", self.exported):
                    raise RuntimeError(f"the model uses the name {name}")

    # -- the exported file as it is --

    def states_of_export(self) -> int:
        """The number of states that PRISM-games builds from the exported
        file alone."""
        out = run_prism(self.prism, self.exported, [], self.consts)
        return int(re.search(r"States:\s+(\d+)", out).group(1))

    # -- additions --

    def restriction(self) -> list:
        model, game = self.model, self.game
        sel = game.selection(self.profile)
        number = {p.id: i + 1 for i, p in enumerate(model.players)}
        by_action: dict = {}
        for g, cmds in enumerate(game.commands):
            if cmds and game.owner[g] is not None:
                action = cmds[sel[g]].command.action
                by_action.setdefault(action, []).append(_state_formula(model, game.states[g]))
        lines = ["", "const int cadl_free_;", "", "module cadl_profile_"]
        done = set()
        for c in model.commands:
            if c.owner is None or not c.exists_in(self.design.name) or c.action in done:
                continue
            done.add(c.action)
            states = by_action.get(c.action, [])
            guard = " | ".join([f"cadl_free_={number[c.owner]}"] + states)
            lines.append(f"  [{_prism_name(c.action)}] {guard} -> true;")
        lines.append("endmodule")
        return lines

    def rho(self, player: str, g: int, c) -> Fraction:
        model, theta = self.model, self.theta
        payoff = payoff_of(model, player)
        state = self.game.states[g]
        total = Fraction(0)
        for term in payoff.terms:
            w = eval_param_expr(term_weight(model, payoff, term), theta)
            structure = next(r for r in model.rewards if r.id == term.reward)
            for item in structure.items:
                if item.action is not None and item.action != c.command.action:
                    continue
                if compile_state_expr(item.guard, self.game.index, self.game.labels)(state):
                    total += w * eval_param_expr(item.value, theta)
        return total

    def shifted(self, player: str):
        """The structures `cadl_shifted_<p>` and `cadl_pinned_<p>` (the
        same with a penalty B on every command of p that the profile does
        not select), and h on Reach_p."""
        game, model = self.game, self.model
        proc = build_process(game, player, self.profile, point_distributions(game, ()))
        steps = optimal_values(proc, _all(proc), lambda i, k: Fraction(1))
        rows, low = [], Fraction(0)
        for i, g in enumerate(proc.states):
            if proc.target[i]:
                continue
            for k, ds in proc.options[i]:
                if k is None:
                    continue
                c = game.commands[g][k]
                r = self.rho(player, g, c)
                rows.append((i, g, k, c, r, ds[0]))
                low = min(low, r)
        h = [-low * x for x in steps]
        items, penalties = [], []
        values = []
        for i, g, k, c, r, d in rows:
            value = r + h[i] - sum((p * h[j] for j, p in d.items()), Fraction(0))
            assert value >= 0, value
            values.append(value)
            action = _prism_name(c.command.action) if c.command.action else ""
            where = f"[{action}] {_state_formula(model, game.states[g])}"
            if value:
                items.append(f"  {where} : {_number(value)};")
            if proc.mine[i] and k != proc.selected[i]:
                penalties.append(where)
        # Under the profile, the shifted values are at most B − 1, so a
        # command of p that the profile does not select never attains the
        # minimum: the minimum over p of the pinned structure is the value
        # of the profile (the one-step test of I.7, for a minimum).
        bound = int(sum(float(x) for x in values) * max(1.0, max(float(x) for x in steps))
                    + 1) + 1 if values else 1
        name = _prism_name(player)
        lines = ["", f'rewards "cadl_shifted_{name}"'] + (items or ["  true : 0;"])
        lines += ["endrewards", "", f'rewards "cadl_pinned_{name}"'] + (items or ["  true : 0;"])
        lines += [f"  {where} : {bound};" for where in penalties] + ["endrewards"]
        h_of = {proc.states[i]: h[i] for i in range(len(proc.states))}
        return lines, h_of, proc

    def solve(self) -> dict:
        """Per player: V_p(s) and V*_p(s) from PRISM-games on Reach_p,
        keyed by the state index of the game."""
        model = self.model
        with Games():
            text = self.exported + "\n".join(self.restriction())
            props, plan, extra = [], [], {}
            coalition = _prism_name(self.written[0]) if self.written else ""
            for p in model.players:
                payoff = payoff_of(model, p.id)
                lines, h_of, proc = self.shifted(p.id)
                text += "\n".join(lines)
                extra[p.id] = (h_of, proc)
                who = _prism_name(p.id) if p.id in self.written else coalition
                target = tr(payoff.until)
                for term in payoff.terms:
                    w = eval_param_expr(term_weight(model, payoff, term), self.theta)
                    props.append(f'filter(printall, <<{who}>> R{{"{_prism_name(term.reward)}"}}'
                                 f'max=? [ F ({target}) ], true)')
                    plan.append((p.id, "term", w))
                props.append(f'filter(printall, <<{who}>> R{{"cadl_shifted_{_prism_name(p.id)}"}}'
                             f'max=? [ F ({target}) ], true)')
                plan.append((p.id, "shifted", None))
                props.append(f'filter(printall, <<{who}>> R{{"cadl_pinned_{_prism_name(p.id)}"}}'
                             f'min=? [ F ({target}) ], true)')
                plan.append((p.id, "pinned", None))
            text += "\n"
        n = len(model.players)
        consts = ",".join(x for x in (self.consts, f"cadl_free_=0:{n}") if x)
        out = run_prism(self.prism, text, props, consts)
        blocks = out.split("\nModel checking: ")[1:]
        assert len(blocks) == len(props) * (n + 1), (len(blocks), len(props))
        index = {s: g for g, s in enumerate(self.game.states)}
        values: dict = {}
        for b, block in enumerate(blocks):
            free = int(re.search(r"cadl_free_=(\d+)", block).group(1))
            player, kind, w = plan[b % len(props)]
            per_state = {}
            for m in re.finditer(r"^\d+:\((.*)\)=(\S+)$", block, re.M):
                state = _parse_state(model, m.group(1))
                if state in index:
                    per_state[index[state]] = float(m.group(2))
            values[(free, b % len(props))] = (player, kind, w, per_state)
        result = {}
        for i, p in enumerate(model.players):
            h_of, proc = extra[p.id]
            # On the states that PRISM-games builds when every player follows
            # the profile: V_p from the exported reward structures, and from
            # the shifted one. On all of Reach_p (the player is free): V_p
            # from the pinned structure, V*_p from the shifted one.
            v_terms: dict = {}
            v_shift, v, v_star = {}, {}, {}
            for (free, k), (player, kind, w, per_state) in values.items():
                if player != p.id:
                    continue
                if free == 0:
                    here = [g for g in proc.states if g in per_state]
                    for g in here:
                        if kind == "term":
                            v_terms[g] = v_terms.get(g, 0.0) + float(w) * per_state[g]
                        elif kind == "shifted":
                            v_shift[g] = per_state[g] - float(h_of[g])
                elif free == i + 1:
                    for g in proc.states:
                        if kind == "shifted":
                            v_star[g] = per_state[g] - float(h_of[g])
                        elif kind == "pinned":
                            v[g] = per_state[g] - float(h_of[g])
            result[p.id] = {"terms": v_terms, "shifted": v_shift, "pinned": v,
                            "star": v_star}
        return result


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= TOL * max(1.0, abs(a), abs(b))


def compare_model(model, prism: str, stats: dict | None = None) -> list:
    """Compare every equilibrium check (an entry of `equilibria:` without
    `over:`) of a model with PRISM-games. Returns the differences found."""
    stats = stats if stats is not None else {}
    problems = []

    def count(key: str, by: int = 1) -> None:
        stats[key] = stats.get(key, 0) + by

    runs: dict = {}
    for q in model.equilibria:
        if q.is_map:
            continue
        for d in equilibrium_check(model, q.id)["designs"]:
            design = model.design(d["design"])
            key = (d["design"], q.profile)
            if key not in runs:
                run = DesignRun(model, design, q.profile, prism)
                n = run.states_of_export()
                count("designs")
                if n != len(run.game.states):
                    problems.append(f"{key}: PRISM-games builds {n} states, cadl "
                                    f"{len(run.game.states)}")
                runs[key] = (run, run.solve())
            run, solved = runs[key]
            game = run.game
            gains, sgains = {}, {}
            for e in d["players"]:
                p = e["player"]
                got = solved[p]
                at = player_at_point(game, q.profile, p, ())
                v, v_star = got["pinned"], got["star"]
                checks = [("payoff", e["payoff"], got["terms"][0]),
                          ("payoff (shifted)", e["payoff"], got["shifted"][0]),
                          ("payoff (pinned)", e["payoff"], v[0]),
                          ("best-response value", e["best_response_value"], v_star[0])]
                for name, mine, theirs in checks:
                    count("values")
                    if not _close(float(mine), theirs):
                        problems.append(f"{q.id} {d['design']} {p}: {name} cadl "
                                        f"{float(mine)!r}, PRISM-games {theirs!r}")
                proc = build_process(game, p, q.profile, point_distributions(game, ()))
                profile_values = optimal_values(proc, _profile(proc),
                                                point_reward(game, proc, ()))
                mine_v = {g: profile_values[i] for i, g in enumerate(proc.states)}
                for g, x in got["terms"].items():
                    count("state values")
                    if not (_close(float(mine_v[g]), x) and _close(x, got["shifted"][g])):
                        problems.append(f"{q.id} {d['design']} {p}: V_p at {game.states[g]} "
                                        f"cadl {float(mine_v[g])!r}, PRISM-games {x!r} "
                                        f"(shifted {got['shifted'][g]!r})")
                state_gain = 0.0
                for g in v:
                    count("state values", 2)
                    if not (_close(float(mine_v[g]), v[g])
                            and _close(float(at.state_gains[g]), v_star[g] - v[g])):
                        problems.append(f"{q.id} {d['design']} {p}: at {game.states[g]} "
                                        f"V_p cadl {float(mine_v[g])!r}, PRISM-games {v[g]!r}; "
                                        f"V*-V cadl {float(at.state_gains[g])!r}, "
                                        f"PRISM-games {v_star[g] - v[g]!r}")
                    state_gain = max(state_gain, v_star[g] - v[g])
                gains[p] = v_star[0] - got["terms"][0]
                sgains[p] = state_gain
            eps = float(q.epsilon)
            measure = sgains if q.concept == "every_reachable_state" else gains
            exact = {e["player"]: (e["state_gain"] if q.concept == "every_reachable_state"
                                   else e["gain"]) for e in d["players"]}
            near = [p for p, x in measure.items() if abs(x - eps) <= TOL * max(1.0, eps)]
            if near:
                # A floating-point gain within the tolerance of ε does not
                # decide the verdict; the exact gain of cadl must then be ε.
                count("verdicts with a gain equal to epsilon")
                if any(exact[p] != q.epsilon for p in near):
                    count("verdicts with a gain within the tolerance of epsilon")
            if not near or all(exact[p] == q.epsilon for p in near):
                verdict = "not_equilibrium" if any(
                    x > eps and p not in near for p, x in measure.items()) else "equilibrium"
                count("verdicts")
                if verdict != d["status"]:
                    problems.append(f"{q.id} {d['design']}: cadl {d['status']}, "
                                    f"PRISM-games {verdict}")
                deviators = sorted(p for p, x in measure.items() if x > eps and p not in near)
                if deviators != sorted(d["deviators"]):
                    problems.append(f"{q.id} {d['design']}: deviators cadl {d['deviators']}, "
                                    f"PRISM-games {deviators}")
            if d.get("from_every_state") is not None:
                pg_every = all(x <= TOL for x in sgains.values())
                count("from_every_state")
                if pg_every != d["from_every_state"]:
                    problems.append(f"{q.id} {d['design']}: from_every_state cadl "
                                    f"{d['from_every_state']}, PRISM-games {pg_every}")
    return problems


def check_examples(prism: str, stats: dict) -> list:
    from cadl.parser import parse_file
    from cadl.stochastic import analyse
    problems = []
    for path in EXAMPLES:
        model = analyse(parse_file(str(path))).model
        stats["files"] = stats.get("files", 0) + 1
        problems += [f"{path.name}: {x}" for x in compare_model(model, prism, stats)]
    return problems


def check_fuzz(prism: str, seed: int, count: int, stats: dict) -> list:
    """`count` accepted models of tests/games_fuzz.py, a quarter of each
    kind: two or three players (nash), every_reachable_state, three or four
    players, and the one-parameter games of the end points."""
    sys.path.insert(0, str(ROOT))
    from games_fuzz import generate, to_cadl
    from cadl.parser import parse
    from cadl.stochastic import analyse
    rng = random.Random(seed)
    kinds = [(None, "nash"), (None, "every"), ("scale", "both"), ("endpoints", "both")]
    problems = []
    done = tries = 0
    while done < count and tries < 50 * count:
        mode, concept = kinds[done % len(kinds)]
        tries += 1
        g = generate(rng, mode=mode, concept=concept)
        analysis = analyse(parse(to_cadl(g)))
        if not analysis.ok:
            continue
        done += 1
        stats["fuzz models"] = stats.get("fuzz models", 0) + 1
        found = compare_model(analysis.model, prism, stats)
        if found:
            problems += [f"fuzz model {done} (seed {seed}): {x}" for x in found]
            problems.append(to_cadl(g))
    return problems


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prism", default=None, help="the PRISM-games binary")
    parser.add_argument("--examples", action="store_true")
    parser.add_argument("--fuzz", type=int, default=0, metavar="COUNT")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    prism = args.prism or find_prism_games()
    if prism is None:
        print("PRISM-games not found (set CADL_PRISM_GAMES or put prism-games on PATH)")
        return 2
    start = time.time()
    stats: dict = {}
    problems = []
    if args.examples:
        problems += check_examples(prism, stats)
    if args.fuzz:
        problems += check_fuzz(prism, args.seed, args.fuzz, stats)
    print(stats, f"{time.time() - start:.1f}s")
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
