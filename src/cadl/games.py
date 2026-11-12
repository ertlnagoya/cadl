"""Strategic model extension (Appendix I, games 0.1): syntax and static semantics.

A file that declares ``stochastic: 0.1`` and ``games: 0.1`` may carry a
``model:`` block with ``kind: smg``: a turn-based stochastic game on the
model of Appendix H. This module

- applies the static rules I-1 to I-11 of I.10 on top of the rules of
  H.10 (`analyse_game_file`);
- gives the Markov chain of a profile, M(D, θ, σ) of I.5, as a model of
  Appendix H (`induced_model`), so that properties and regime maps of a
  strategic model are evaluated by the code of Appendix H;
- lowers the block to the IR of I.13 (`game_to_ir`) and writes the
  additions of I.11 to the PRISM export (`prism_games_header`).

The equilibrium checks and maps of I.7 to I.9 are in `games_eval`.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .ast_nodes import SoSDefinition
from .stochastic import (
    RESERVED_SOURCES,
    Analysis,
    Command,
    Design,
    Model,
    _Analyser,
    _as_text,
    _json_number,
    _prism_name,
    _YAML11_HINT,
    compile_state_expr,
    decimal_text,
    ill_defined,
    model_to_ir,
    param_names,
    parse_param_expr,
    yaml_number,
)

GAMES_EXTENSION = ("games", "0.1")
GROUNDING_EXTENSION = ("grounding", "0.1")

# The keys of `model:` that this appendix defines (I.2).
GAME_MODEL_KEYS = ("players", "profiles", "compliance", "payoffs", "equilibria")
# I.1: reserved for a later version (I-11).
RESERVED_GAME_MODEL_KEYS = ("mediator", "observations")
# The values of `concept:` of an entry of `equilibria:` (I.7, I.8) and the
# ones reserved for a later version (I.1).
CONCEPTS = ("nash", "every_reachable_state")
RESERVED_CONCEPTS = ("subgame_perfect", "correlated")
# Keys of a payoff term that Appendix J defines (I-7, J.7).
GROUNDED_TERM_KEYS = ("sanction_of", "reward_of")

# I-3 says that a player id must not be "one of the keywords that S-2
# excludes", and S-2 excludes `R`; the examples of I.12 name their players
# `R` and `D`. A player id occurs in no expression sub-language, and I.11
# provides for a player whose name is a PRISM keyword, so this processor
# admits the keywords of the property sub-language (`F`, `U`, `P`, `R`) as
# player ids and excludes the others. (Reported as a problem of the
# appendix; set this to the empty set for the rule as written.)
PLAYER_ID_ADMITTED_KEYWORDS = frozenset({"F", "U", "P", "R"})

_ACTOR_REF_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^\[\]]*\])?")


def games_declared(sos: SoSDefinition) -> bool:
    """True when the file declares ``games: 0.1`` under ``extensions:``."""
    return GAMES_EXTENSION in sos.extensions


def undeclared_use(block: Any) -> list:
    """What a `model:` block uses of this appendix (I.15): names for a
    diagnostic, empty when it uses nothing."""
    used = []
    if block.kind == "smg":
        used.append("kind 'smg'")
    used += [f"model.{k}" for k in GAME_MODEL_KEYS if k in block.keys]
    if any("owner" in c.keys for c in block.commands):
        used.append("'owner' of a command")
    if any("profile" in p.keys for p in block.properties):
        used.append("'profile' of a property")
    if any("profile" in q.keys for q in block.regime_maps):
        used.append("'profile' of a regime map")
    return used


# ---------------------------------------------------------------------------
# The checked strategic model (fields of `stochastic.Model`)
# ---------------------------------------------------------------------------

@dataclass
class Player:
    id: str = ""
    actors: list = field(default_factory=list)      # actor references, as written


@dataclass
class ProfileRule:
    guard: tuple = ("bool", True)
    guard_text: str = "true"
    choose: str = ""


@dataclass
class Profile:
    id: str = ""
    rules: list = field(default_factory=list)


@dataclass
class PayoffTerm:
    reward: str = ""
    weight: tuple | None = ("num", Fraction(1), "1")   # None: defined by Appendix J
    weight_text: str = "1"
    sanction_of: Any = None
    reward_of: Any = None
    link: Any = None                   # grounding.NormLink, when the link is usable

    @property
    def grounded(self) -> bool:
        return self.weight is None

    @property
    def link_key(self) -> str | None:
        if self.sanction_of is not None:
            return "sanction_of"
        if self.reward_of is not None:
            return "reward_of"
        return None

    def assumption(self) -> str:
        """The assumption of J.7 that every result computed with the term
        carries (J-12)."""
        what = "violations" if self.link_key == "sanction_of" else "fulfilments"
        ref = self.sanction_of if self.link_key == "sanction_of" else self.reward_of
        return f"the reward structure {self.reward} is taken to count the {what} of {ref}"


@dataclass
class Payoff:
    player: str = ""
    until: tuple = ("bool", True)
    until_text: str = "true"
    terms: list = field(default_factory=list)


@dataclass
class EquilibriumQuery:
    id: str = ""
    profile: str = ""
    regimes: list = field(default_factory=list)
    epsilon: Fraction = Fraction(0)
    over: dict = field(default_factory=dict)        # parameter -> (lo, hi); empty: a check
    resolution: Fraction | None = None
    concept: str = "nash"                           # nash | every_reachable_state

    @property
    def is_map(self) -> bool:
        return bool(self.over)


def profile_of(model: Model, name: str) -> Profile:
    return next(p for p in model.profiles if p.id == name)


def payoff_of(model: Model, player: str) -> Payoff:
    return next(p for p in model.payoffs if p.player == player)


class NotEvaluated(ValueError):
    """A payoff cannot be evaluated by this processor (I-7): it has a term
    whose weight Appendix J defines and nothing supplied that weight."""


def term_weight(model: Model, payoff: Payoff, term: PayoffTerm) -> tuple:
    """The weight of a payoff term as a `param_expr` tree.

    This is the one place that decides the weight of a term. A term of
    I.6 has the weight it is written with (default 1). A term with
    `sanction_of:` or `reward_of:` has the weight that Appendix J defines
    (−magnitude × λ parameter, J.7); an implementation of that appendix
    supplies it in `model.term_weights[(player, reward)]`. Without it the
    payoff is not evaluated, and the term is never read with a default
    weight (I-7).
    """
    if not term.grounded:
        return term.weight
    supplied = model.term_weights.get((payoff.player, term.reward))
    if supplied is None:
        key = term.link_key
        ref = term.sanction_of if key == "sanction_of" else term.reward_of
        raise NotEvaluated(
            f"the term '{term.reward}' of the payoff of player '{payoff.player}' has "
            f"'{key}: {_as_text(ref)}', whose weight Appendix J defines, and the link to "
            f"the norm cannot be used here (the file does not declare 'norms: 0.1' with "
            f"'grounding: 0.1', or the reference is not valid); the payoff is not "
            f"evaluated (I-7)")
    return supplied


def link_assumptions(model: Model) -> list:
    """The assumptions of J.7 for the terms whose weight Appendix J
    defines, in the order of the payoffs (J-12)."""
    return [t.assumption() for p in model.payoffs for t in p.terms
            if t.grounded and (p.player, t.reward) in model.term_weights]


def unevaluated_terms(model: Model) -> list:
    """The diagnostics of the payoffs that cannot be evaluated (I-7)."""
    out = []
    for payoff in model.payoffs:
        for term in payoff.terms:
            try:
                term_weight(model, payoff, term)
            except NotEvaluated as e:
                out.append(str(e))
    return out


# ---------------------------------------------------------------------------
# Static semantics (I.10)
# ---------------------------------------------------------------------------

class _GameAnalyser(_Analyser):
    """H.10 with the changes and additions of I.10."""

    def __init__(self, sos: SoSDefinition) -> None:
        super().__init__(sos)
        self.strategic = self.block.kind == "smg"
        self.raw = self.block.games
        self.players: dict = {}
        self.profiles: dict = {}
        self.action_owner: dict = {}
        self.enumerated = False
        self.rule_first: set = set()          # (profile, rule position) that is first somewhere
        self.reported5: set = set()
        self.reported6: set = set()
        self._rule_fns: dict | None = None
        self.pending_links: list = []

    def run(self) -> Analysis:
        for rule, message in self.block.malformed:
            self.error(message, rule)
        self.check_reserved()
        self.check_structure()
        self.check_parameters()
        self.check_variables()
        self.check_labels()
        self.check_commands()
        if self.strategic:
            self.check_players()
            self.check_owners()
        self.check_regimes()
        self.check_rewards()
        if self.strategic:
            self.check_profiles()
            self.check_payoffs()
            self.check_links()
        self.check_properties()
        self.check_regime_maps()
        if self.strategic:
            self.check_query_profiles()
            self.check_equilibria()
        self.check_reward_parameters()
        self.check_states()
        if self.strategic:
            self.check_unused_rules()
        self.check_well_defined()
        if self.strategic:
            self.model.kind = "smg"
            if self.out.ok:
                self.check_admissible()
            if self.out.ok:
                self.report_compliance_sanctions()
        if self.out.ok:
            self.out.model = self.model
        return self.out

    # -- I-11: reserved names (replaces the list of S-15) --
    def check_reserved(self) -> None:
        block = self.block
        for key in RESERVED_GAME_MODEL_KEYS:
            if key in block.keys:
                self.info(f"model.{key} is reserved for a later version of the strategic "
                          f"model extension (I.1) and is not interpreted", "I-11")
        for i, v in enumerate(block.variables, start=1):
            if "owner" in v.keys:
                self.info(f"'owner' of variable {i} of the model is reserved for a later "
                          f"version (I.1) and is not interpreted", "I-11")
        for i, c in enumerate(block.commands, start=1):
            if "sync" in c.keys:
                self.info(f"'sync' of command {i} of the model is reserved for a later "
                          f"version (I.1) and is not interpreted", "I-11")
        if block.kind == "mdp":
            self.info("kind 'mdp' is reserved for a later version (I.1) and is "
                      "given no meaning", "I-11")
        for p in block.parameters:
            if isinstance(p.source, str) and p.source.strip() in RESERVED_SOURCES:
                self.info(f"the parameter source '{p.source.strip()}' is reserved for a "
                          f"later version (I.1) and is given no meaning", "I-11")
        entries = self.raw.get("equilibria")
        for i, e in enumerate(entries if isinstance(entries, list) else [], start=1):
            concept = e.get("concept") if isinstance(e, dict) else None
            if isinstance(concept, str) and concept in RESERVED_CONCEPTS:
                self.info(f"the concept '{concept}' of entry {i} of model.equilibria is "
                          f"reserved for a later version (I.1) and is given no meaning",
                          "I-11")

    # -- I-2 --
    def check_structure(self) -> None:
        block = self.block
        if block.kind is not None and block.kind not in ("dtmc", "smg"):
            self.error(f"model.kind is '{_as_text(block.kind)}'; it must be 'dtmc' or "
                       f"'smg'", "I-2")
        if "variables" not in block.keys or not block.variables:
            self.error("model: must have variables: with at least one variable", "S-1")
        if "commands" not in block.keys:
            self.error("model: must have commands:", "S-1")
        if self.strategic:
            for key in ("players", "profiles", "compliance", "payoffs"):
                if key not in block.keys or self.raw.get(key) is None:
                    self.error(f"A model with kind 'smg' must have model.{key}", "I-2")
            return
        what = "'dtmc'" if block.kind == "dtmc" else "absent" if block.kind is None \
            else f"'{_as_text(block.kind)}'"
        for use in undeclared_use(block):
            self.error(f"The model uses {use}, but its kind is {what}; only a model with "
                       f"kind 'smg' may use the keys of the strategic model extension",
                       "I-2")

    # -- I-3: players --
    def check_players(self) -> None:
        raw = self.raw.get("players")
        if raw is None:
            return
        if not isinstance(raw, list) or not raw:
            self.error("model.players must be a sequence with at least one player", "I-2")
            return
        declared = {a.id.name for a in self.sos.actors}
        stands: dict = {}
        for i, d in enumerate(raw, start=1):
            if not isinstance(d, dict):
                self.error(f"entry {i} of model.players is not a mapping", "I-3")
                continue
            pid = d.get("id")
            if isinstance(pid, str) and pid in PLAYER_ID_ADMITTED_KEYWORDS:
                name = pid
            else:
                name = self.ident(pid, f"The id of player {i} of the model", "I-3")
            where = f"player '{name}'" if name else f"player {i}"
            player = Player(id=name or f"#{i}")
            if name and name in self.players:
                self.error(f"The player '{name}' is declared twice", "I-3")
                name = None
            actors = d.get("actors")
            if not isinstance(actors, list) or not actors:
                self.error(f"'actors' of {where} must list at least one actor reference",
                           "I-3")
            else:
                for ref in actors:
                    m = _ACTOR_REF_RE.fullmatch(ref.strip()) if isinstance(ref, str) else None
                    if m is None:
                        self.error(f"'actors' of {where} has the entry '{_as_text(ref)}', "
                                   f"which is not an actor reference", "I-3")
                        continue
                    actor = m.group(1)
                    if actor not in declared:
                        self.error(f"'actors' of {where} names '{actor}', which is not a "
                                   f"declared actor", "I-3")
                        continue
                    if actor in stands:
                        other = stands[actor]
                        if other == player.id:
                            self.error(f"'actors' of {where} lists the actor '{actor}' "
                                       f"more than once", "I-3")
                        else:
                            self.error(f"The actor '{actor}' stands behind the players "
                                       f"'{other}' and '{player.id}'; an actor stands "
                                       f"behind at most one player", "I-3")
                        continue
                    stands[actor] = player.id
                    player.actors.append(ref.strip())
            if name:
                self.players[name] = player
                self.model.players.append(player)

    # -- I-4: ownership --
    def check_owners(self) -> None:
        by_action: dict = {}
        for d, c in zip(self.block.commands, self.model.commands):
            where = f"command {c.index} of the model"
            if "owner" in d.keys:
                if not isinstance(d.owner, str) or d.owner not in self.players:
                    self.error(f"'owner' of {where} is '{_as_text(d.owner)}', which is not "
                               f"a declared player", "I-4")
                    continue
                c.owner = d.owner
                if "action" not in d.keys:
                    self.error(f"The {where} has an owner but no 'action'; an owned "
                               f"command must have an action label", "I-4")
            if c.action is not None:
                by_action.setdefault(c.action, []).append(c)
        for action, cmds in by_action.items():
            owners = []
            for c in cmds:
                if c.owner not in owners:
                    owners.append(c.owner)
            if len(owners) > 1:
                shown = ", ".join("no owner" if o is None else f"'{o}'" for o in owners)
                self.error(f"The commands with the action label '{action}' have different "
                           f"owners ({shown}); they must all have the same owner or all "
                           f"be unowned", "I-4")
            else:
                self.action_owner[action] = owners[0]

    # -- I-6: profiles --
    def check_profiles(self) -> None:
        raw = self.raw.get("profiles")
        if raw is not None:
            if not isinstance(raw, dict):
                self.error("model.profiles must be a mapping from profile names to "
                           "sequences of rules", "I-6")
                raw = {}
            for key, rules in raw.items():
                name = self.ident(key, "The profile name", "I-6")
                if name is None:
                    continue
                if name in self.profiles:
                    self.error(f"The profile '{name}' is declared twice", "I-6")
                    continue
                profile = Profile(id=name)
                if rules is not None and not isinstance(rules, list):
                    self.error(f"The profile '{name}' must be a sequence of rules "
                               f"(possibly empty)", "I-6")
                    rules = []
                for j, r in enumerate(rules or [], start=1):
                    where = f"rule {j} of profile '{name}'"
                    if not isinstance(r, dict):
                        self.error(f"The {where} is not a mapping", "I-6")
                        continue
                    rule = ProfileRule()
                    choose = r.get("choose")
                    ok = True
                    if not isinstance(choose, str) or choose not in self.action_owner:
                        self.error(f"'choose' of {where} is '{_as_text(choose)}', which is "
                                   f"not the action label of a command", "I-6")
                        ok = False
                    elif self.action_owner[choose] is None:
                        self.error(f"'choose' of {where} is '{choose}', the action label "
                                   f"of an unowned command; a rule chooses a command of "
                                   f"a player", "I-6")
                        ok = False
                    else:
                        rule.choose = choose
                    if "guard" in r and r["guard"] is not None:
                        res = self.boolean_pred(r["guard"], f"The guard of {where}",
                                                rule="I-6")
                        if res:
                            rule.guard, rule.guard_text = res
                        else:
                            ok = False
                    if ok:
                        profile.rules.append(rule)
                    else:
                        self.profiles_ok = False
                self.profiles[name] = profile
                self.model.profiles.append(profile)
        compliance = self.raw.get("compliance")
        if "compliance" in self.raw and compliance is not None:
            if not isinstance(compliance, str) or compliance not in self.profiles:
                hint = f"; {_YAML11_HINT}" if isinstance(compliance, bool) else ""
                self.error(f"model.compliance is '{_as_text(compliance)}', which is not "
                           f"the name of a profile{hint}", "I-6")
            else:
                self.model.compliance = compliance

    profiles_ok = True

    # -- I-7: payoffs --
    def check_payoffs(self) -> None:
        raw = self.raw.get("payoffs")
        if raw is None:
            return
        if not isinstance(raw, list):
            self.error("model.payoffs must be a sequence of mappings", "I-7")
            return
        grounding = GROUNDING_EXTENSION in self.sos.extensions
        self.model.grounding = grounding
        seen: dict = {}
        for i, d in enumerate(raw, start=1):
            if not isinstance(d, dict):
                self.error(f"entry {i} of model.payoffs is not a mapping", "I-7")
                continue
            player = d.get("player")
            where = f"payoff {i} of the model"
            if not isinstance(player, str) or player not in self.players:
                self.error(f"'player' of {where} is '{_as_text(player)}', which is not a "
                           f"declared player", "I-7")
                player = None
            else:
                where = f"the payoff of player '{player}'"
                seen[player] = seen.get(player, 0) + 1
            payoff = Payoff(player=player or f"#{i}")
            if "until" not in d or d["until"] is None:
                self.error(f"{where[0].upper()}{where[1:]} has no 'until'", "I-7")
            else:
                res = self.boolean_pred(d["until"], f"'until' of {where}", labels_ok=True,
                                        rule="I-7")
                if res:
                    payoff.until, payoff.until_text = res
                    self.warn_initial_target(payoff, where)
            terms = d.get("terms")
            if not isinstance(terms, list) or not terms:
                self.error(f"'terms' of {where} must have at least one term", "I-7")
                terms = []
            used: set = set()
            for j, t in enumerate(terms, start=1):
                twhere = f"term {j} of {where}"
                if not isinstance(t, dict):
                    self.error(f"The {twhere} is not a mapping", "I-7")
                    continue
                term = PayoffTerm()
                reward = t.get("reward")
                if not isinstance(reward, str) or reward not in self.reward_ids:
                    self.error(f"'reward' of {twhere} is '{_as_text(reward)}', which is not "
                               f"a declared reward structure", "I-7")
                    continue
                if reward in used:
                    self.error(f"{where[0].upper()}{where[1:]} names the reward structure "
                               f"'{reward}' in more than one term", "I-7")
                    continue
                used.add(reward)
                term.reward = reward
                grounded = [k for k in GROUNDED_TERM_KEYS if k in t]
                if "weight" in t and t["weight"] is not None:
                    res = self.param_value(t["weight"], f"'weight' of {twhere}", "I-7")
                    if res is None:
                        continue
                    term.weight, term.weight_text = res
                if grounded:
                    if not grounding:
                        self.error(f"The {twhere} has '{grounded[0]}', which Appendix J "
                                   f"defines, but the file does not declare 'grounding: "
                                   f"0.1'", "I-7")
                        continue
                    written = [k for k in ("weight",) + GROUNDED_TERM_KEYS if k in t]
                    if len(written) > 1:
                        self.error(f"The {twhere} has {', '.join(repr(k) for k in written)}; "
                                   f"a term has at most one of 'weight', 'sanction_of' and "
                                   f"'reward_of'", "J-10")
                        continue
                    if grounded[0] == "sanction_of":
                        term.sanction_of = t.get("sanction_of")
                    else:
                        term.reward_of = t.get("reward_of")
                    # Appendix J defines the weight; it is never the default.
                    term.weight, term.weight_text = None, ""
                    self.pending_links.append((payoff, term, twhere))
                payoff.terms.append(term)
            if player:
                self.model.payoffs.append(payoff)
        for name in self.players:
            if seen.get(name, 0) != 1:
                self.error(f"model.payoffs has {seen.get(name, 0)} entries for the player "
                           f"'{name}'; every player has exactly one payoff", "I-7")

    # -- J-10, J-11: the link between a payoff term and a norm (J.7) --
    def check_links(self) -> None:
        """Resolve the terms `sanction_of:` / `reward_of:` and supply their
        weights (−m · ℓ or m · ℓ) through `model.term_weights`."""
        from .grounding import (
            annotations, covers, grounding_active, lambda_parameters, norm_payoff_link,
        )
        if not self.pending_links:
            return
        if not grounding_active(self.sos):
            # J-1 reports the missing 'norms: 0.1'; the terms are not evaluated.
            for payoff, term, twhere in self.pending_links:
                self.info(f"The {twhere} has '{term.link_key}', but the file does not "
                          f"declare 'norms: 0.1' with 'grounding: 0.1' (J-1), so its weight "
                          f"is not defined; the payoff and the equilibrium queries of the "
                          f"model are not evaluated", "I-7")
            return
        named: dict = {}
        reported_lambda: set = set()
        linked_contracts: list = []
        for payoff, term, twhere in self.pending_links:
            key = term.link_key
            ref = term.sanction_of if key == "sanction_of" else term.reward_of
            link = norm_payoff_link(self.sos, ref, key)
            if link.error:
                self.error(f"The {twhere}: {link.error}", "J-10")
                continue
            ok = True
            lams = lambda_parameters(self.model.params, link.contract)
            if len(lams) != 1:
                if link.contract not in reported_lambda:
                    reported_lambda.add(link.contract)
                    shown = ", ".join(f"'{x}'" for x in lams) or "none"
                    self.error(f"The model has a term that names a norm of contract "
                               f"'{link.contract}' ({twhere}), so it must have exactly one "
                               f"parameter bound by from: \"{link.contract}.lambda\"; it has "
                               f"{len(lams)} ({shown})", "J-10")
                ok = False
            player = self.players.get(payoff.player)
            if player is not None and not any(covers(a, link.bearer) for a in player.actors):
                self.error(f"The {twhere} names '{link.contract}.{link.norm}', whose bearer "
                           f"is '{link.bearer}', but the player '{payoff.player}' lists no "
                           f"actor reference that covers it (actors: "
                           f"{', '.join(player.actors)})", "J-10")
                ok = False
            ident = (key, link.contract, link.norm)
            named[ident] = named.get(ident, 0) + 1
            if named[ident] == 2:
                self.error(f"The norm '{link.contract}.{link.norm}' is named by more than one "
                           f"'{key}' term of the model; a norm is named at most once by each "
                           f"key", "J-10")
                ok = False
            if link.contract not in linked_contracts:
                linked_contracts.append(link.contract)
            if not ok:
                continue
            term.link = link
            text = link.weight_text(lams[0])
            term.weight_text = text
            self.model.term_weights[(payoff.player, term.reward)] = parse_param_expr(text)
        # J-11: the coverage of the representation.
        actors = [a for p in self.model.players for a in p.actors]
        for cid in linked_contracts:
            contract = next(c for c in self.sos.contracts if c.id == cid)
            ann = annotations(self.sos, contract)
            for norm, a in zip(contract.norms, ann.norms):
                if norm.kind not in ("obligation", "prohibition"):
                    continue
                if not any(covers(x, norm.bearer) for x in actors):
                    continue
                for key, magnitude in (("sanction_of", a.sanction_magnitude),
                                       ("reward_of", a.reward_magnitude)):
                    if magnitude is not None and magnitude > 0 \
                            and (key, cid, norm.id) not in named:
                        what = "on_violation" if key == "sanction_of" else "on_fulfilment"
                        self.warn(f"The {norm.kind} '{cid}.{norm.id}' has an {what}.magnitude "
                                  f"greater than zero and a bearer that a player stands for, "
                                  f"but no payoff term names it with '{key}'", "J-11")
            bound = set(lambda_parameters(self.model.params, cid))
            for payoff in self.model.payoffs:
                for term in payoff.terms:
                    if term.grounded or term.weight is None:
                        continue
                    used = sorted(param_names(term.weight) & bound)
                    if used:
                        self.warn(f"The term '{term.reward}' of the payoff of player "
                                  f"'{payoff.player}' has the weight '{term.weight_text}', in "
                                  f"which '{used[0]}', bound to {cid}.lambda, occurs; the size "
                                  f"of such a term is not checked against a norm", "J-11")

    # -- J-12: the expected sanctions of the compliance profile --
    def report_compliance_sanctions(self) -> None:
        from .games_eval import EvaluationError, expected_reward_under
        from .stochastic_eval import value_text
        model = self.model
        terms = [(p, t) for p in model.payoffs for t in p.terms if t.link is not None]
        for payoff, term in terms:
            for design in model.designs:
                try:
                    value = expected_reward_under(model, design, model.compliance,
                                                  payoff.player, term.reward)
                except EvaluationError:
                    continue
                shown = "0" if value == 0 else f"{value_text(value)} (exact: {value})"
                if term.link_key == "reward_of":
                    what = (f"{shown}; a reward_of: term has the weight m'·ℓ with m' > 0, so it "
                            f"breaks condition (ii) of J.7 by the sign of its weight, whatever "
                            f"this value")
                    ref = term.reward_of
                elif value == 0:
                    what = ("0: compliance incurs no sanction of this term, so condition "
                            "(ii) of J.7 holds for it")
                    ref = term.sanction_of
                else:
                    what = (f"{shown}: compliance incurs this sanction with a positive "
                            f"expected count, so condition (ii) of J.7 does not hold for it")
                    ref = term.sanction_of
                self.info(f"Design {design.name}: under the compliance profile "
                          f"'{model.compliance}' at the fixed values, the expected value of "
                          f"'{term.reward}' (the term '{term.link_key}: {ref}' of the payoff of "
                          f"player '{payoff.player}') until '{payoff.until_text}' is {what}",
                          "J-12")

    def warn_initial_target(self, payoff: Payoff, where: str) -> None:
        if not self.vars_ok or not self.model.variables:
            return
        index = self.model.var_index()
        labels = {n: compile_state_expr(t, index, {}) for n, (t, _) in self.model.labels.items()}
        if compile_state_expr(payoff.until, index, labels)(self.model.initial_state()):
            self.warn(f"The target '{payoff.until_text}' of {where} holds in the initial "
                      f"state; the payoff is 0 under every profile", "I-7")

    # -- I-10: profile: of a property and of a regime map --
    def check_query_profiles(self) -> None:
        pairs = [(d, p, f"property '{p.id}'")
                 for d, p in zip(self.block.properties, self.model.properties)]
        pairs += [(d, q, f"regime map '{q.id}'")
                  for d, q in zip(self.block.regime_maps, self.model.regime_maps)]
        for d, entry, where in pairs:
            entry.profile = self.profile_name(d.profile if "profile" in d.keys else None,
                                              f"'profile' of {where}", "I-10")

    def profile_name(self, value: Any, where: str, rule: str) -> str | None:
        if value is None:
            return self.model.compliance
        if not isinstance(value, str) or value not in self.profiles:
            hint = f"; {_YAML11_HINT}" if isinstance(value, bool) else ""
            self.error(f"{where} is '{_as_text(value)}', which is not the name of a "
                       f"profile{hint}", rule)
            return None
        return value

    # -- I-9: equilibrium queries --
    def check_equilibria(self) -> None:
        raw = self.raw.get("equilibria")
        if raw is None:
            return
        if not isinstance(raw, list):
            self.error("model.equilibria must be a sequence of mappings", "I-9")
            return
        assigned = {p for d in self.model.designs for p in d.assign}
        for i, d in enumerate(raw, start=1):
            if not isinstance(d, dict):
                self.error(f"entry {i} of model.equilibria is not a mapping", "I-9")
                continue
            name = self.ident(d.get("id"), f"The id of entry {i} of model.equilibria", "I-9")
            where = f"equilibrium query '{name}'" if name else f"entry {i} of model.equilibria"
            self.declare_query(name, "equilibrium query")
            q = EquilibriumQuery(id=name or f"#{i}", regimes=list(self.design_names))
            q.profile = self.profile_name(d.get("profile"), f"'profile' of {where}", "I-9") or ""
            if d.get("regimes") is not None:
                names = self.name_list(d["regimes"], f"'regimes' of {where}", "I-9")
                if names is not None:
                    self.check_regime_names(names, f"'regimes' of {where}",
                                            self.design_names, "a design", "I-9")
                    if not names:
                        self.error(f"'regimes' of {where} must name at least one design",
                                   "I-9")
                    q.regimes = names
            if d.get("epsilon") is not None:
                eps = yaml_number(d["epsilon"])
                if eps is None or eps < 0:
                    self.error(f"'epsilon' of {where} is '{_as_text(d['epsilon'])}'; it "
                               f"must be a finite number that is not negative", "I-9")
                else:
                    q.epsilon = eps
            if d.get("concept") is not None:
                if isinstance(d["concept"], str) and d["concept"] in CONCEPTS:
                    q.concept = d["concept"]
                else:
                    self.error(f"'concept' of {where} is '{_as_text(d['concept'])}'; it "
                               f"must be one of {', '.join(CONCEPTS)}", "I-9")
            if "over" in d and d["over"] is not None:
                self.check_over(d["over"], q, where, assigned)
                q.resolution = Fraction(1, 20)
                if d.get("resolution") is not None:
                    r = yaml_number(d["resolution"])
                    if r is None or not 0 < r < 1:
                        self.error(f"'resolution' of {where} is "
                                   f"'{_as_text(d['resolution'])}'; it must be a number r "
                                   f"with 0 < r < 1", "I-9")
                    else:
                        q.resolution = r
            elif d.get("resolution") is not None:
                self.error(f"The {where} has 'resolution' without 'over'; a resolution "
                           f"belongs to an equilibrium map", "I-9")
            self.model.equilibria.append(q)

    def check_over(self, over: Any, q: EquilibriumQuery, where: str, assigned: set) -> None:
        """`over:` of an equilibrium map: the conditions of S-14."""
        if not isinstance(over, dict) or not 1 <= len(over) <= 4:
            self.error(f"'over' of {where} must be a mapping with one to four "
                       f"parameters, each with an interval [lo, hi]", "I-9")
            return
        for key, rng in over.items():
            if key is None or isinstance(key, bool):
                self.error(f"'over' of {where} has the key '{_as_text(key)}', a "
                           f"boolean or null; {_YAML11_HINT}", "I-9")
                continue
            if key not in self.params:
                self.error(f"'over' of {where} lists '{key}', which is not a "
                           f"declared parameter", "I-9")
                continue
            nums = [yaml_number(x) for x in rng] if isinstance(rng, list) else []
            if len(nums) != 2 or None in nums:
                self.error(f"'over' of {where} gives '{key}' the interval "
                           f"'{_as_text(rng)}'; it must be a sequence of two "
                           f"numbers [lo, hi]", "I-9")
                continue
            lo, hi = nums
            p = self.params[key]
            if not lo < hi:
                self.error(f"'over' of {where} gives '{key}' the interval "
                           f"[{decimal_text(lo)}, {decimal_text(hi)}]; lo must be "
                           f"below hi", "I-9")
                continue
            if p.lo is not None and not (p.lo <= lo and hi <= p.hi):
                self.error(f"'over' of {where} gives '{key}' the interval "
                           f"[{decimal_text(lo)}, {decimal_text(hi)}], which is not "
                           f"within its declared range [{decimal_text(p.lo)}, "
                           f"{decimal_text(p.hi)}]", "I-9")
                continue
            if key in assigned:
                self.error(f"'over' of {where} lists '{key}', which a regime of "
                           f"model.regimes assigns", "I-9")
                continue
            q.over[key] = (lo, hi)
            p.open_in_equilibria = True

    def check_reward_parameters(self) -> None:
        super().check_reward_parameters()
        for r in self.model.rewards:
            for j, item in enumerate(r.items, start=1):
                for name in sorted(param_names(item.value)):
                    if name in self.params and self.params[name].open_in_equilibria:
                        self.error(f"'value' of item {j} of reward structure '{r.id}' "
                                   f"mentions '{name}', which occurs in the 'over' of an "
                                   f"equilibrium map; such a parameter may appear in "
                                   f"probabilities and in weights only", "I-9")

    # -- I-5 (replaces S-9) and the coverage of I-6, by enumerating S --
    def check_enabled(self, state: tuple, enabled: list, cmds: list, reported9: set) -> None:
        if not self.strategic:
            super().check_enabled(state, enabled, cmds, reported9)
            return
        self.enumerated = True
        if self._rule_fns is None:
            index = self.model.var_index()
            self._rule_fns = {
                p.id: [compile_state_expr(r.guard, index, {}) for r in p.rules]
                for p in self.model.profiles
            }
        for design in self.design_names:
            en = [k for k in enabled if cmds[k].exists_in(design)]
            if not en:
                continue
            if len(en) > 1:
                for a, b in itertools.combinations(en, 2):
                    ca, cb = cmds[a], cmds[b]
                    if ca.owner is not None and ca.owner == cb.owner and ca.action != cb.action:
                        continue
                    if (a, b) in self.reported5:
                        continue
                    self.reported5.add((a, b))
                    if ca.owner is None or cb.owner is None:
                        why = "an unowned command may be enabled only alone"
                    elif ca.owner != cb.owner:
                        why = (f"they belong to the players '{ca.owner}' and '{cb.owner}'; "
                               f"in a state at most one player chooses")
                    else:
                        why = (f"both have the action label '{ca.action}'; the commands "
                               f"a player chooses from have pairwise different labels")
                    self.error(
                        f"Commands {ca.index} and {cb.index} of the model are both enabled "
                        f"in the state {self.show_state(state)} under the design {design}: "
                        f"{why}", "I-5")
            actions = {cmds[k].action for k in en}
            for profile in self.model.profiles:
                fns = self._rule_fns[profile.id]
                for j, rule in enumerate(profile.rules):
                    if rule.choose in actions and fns[j](state):
                        self.rule_first.add((profile.id, j))
                        break
                else:
                    if len(en) > 1 and (profile.id, design) not in self.reported6:
                        self.reported6.add((profile.id, design))
                        self.error(
                            f"No rule of the profile '{profile.id}' applies in the state "
                            f"{self.show_state(state)} under the design {design}, where "
                            f"{len(en)} commands are enabled; a profile must select a "
                            f"command in every such state", "I-6")

    def check_unused_rules(self) -> None:
        if not self.enumerated or not self.profiles_ok:
            return
        for profile in self.model.profiles:
            for j, rule in enumerate(profile.rules):
                if (profile.id, j) not in self.rule_first:
                    self.warn(f"Rule {j + 1} of the profile '{profile.id}' (choose: "
                              f"{rule.choose}) is the first applicable rule in no state "
                              f"of any design", "I-6")

    # -- I-8, and the admissibility part of I-9 --
    def check_admissible(self) -> None:
        from .games_eval import (
            EvaluationError, admissibility_witness, build_game, face_centres, show_state,
        )
        model = self.model
        try:
            for design in model.designs:
                theta = model.fixed_values(design)
                game = build_game(model, design, [], theta)
                for profile in model.profiles:
                    for player in model.players:
                        bad = admissibility_witness(game, profile.id, player.id, ())
                        if bad is not None:
                            self.error(
                                f"The profile '{profile.id}' is not admissible for the "
                                f"player '{player.id}' in the design {design.name} at the "
                                f"fixed values: {player.id} can keep the game for ever "
                                f"away from the target '{payoff_of(model, player.id).until_text}' "
                                f"of its payoff, for example from the state "
                                f"{show_state(model, bad)}", "I-8")
            for q in model.equilibria:
                if not q.is_map:
                    continue
                names = list(q.over)
                for regime in q.regimes:
                    design = model.design(regime)
                    if design is None:
                        continue
                    theta = model.fixed_values(design)
                    ok = True
                    for corner in itertools.product(*(q.over[n] for n in names)):
                        at = dict(theta)
                        at.update(zip(names, corner))
                        why = ill_defined(model, regime, at)
                        if why:
                            self.error(
                                f"In equilibrium map '{q.id}', the design {regime} is not "
                                f"well-defined at the corner point {_point_text(names, corner)} "
                                f"of the region: {why}", "I-9")
                            ok = False
                            break
                    if not ok:
                        continue
                    game = build_game(model, design, names, theta)
                    for point in face_centres(q.over):
                        at = dict(theta)
                        at.update(zip(names, point))
                        why = ill_defined(model, regime, at)
                        if why:
                            self.error(
                                f"In equilibrium map '{q.id}', the design {regime} is not "
                                f"well-defined at the face centre {_point_text(names, point)} "
                                f"of the region: {why}", "I-9")
                            break
                        bad = None
                        for player in model.players:
                            state = admissibility_witness(game, q.profile, player.id, point)
                            if state is not None:
                                bad = (player.id, state)
                                break
                        if bad:
                            self.error(
                                f"In equilibrium map '{q.id}', the profile '{q.profile}' is "
                                f"not admissible for the player '{bad[0]}' in the design "
                                f"{regime} at the face centre {_point_text(names, point)} "
                                f"of the region (from the state {show_state(model, bad[1])} "
                                f"the player can avoid the target of its payoff for ever)",
                                "I-9")
                            break
        except EvaluationError as e:
            self.error(f"The strategic model cannot be analysed: {e}", "I-8")


def _point_text(names: list, point: tuple) -> str:
    return ", ".join(f"{n}={decimal_text(x)}" for n, x in zip(names, point))


def analyse_game_file(sos: SoSDefinition) -> Analysis:
    """Apply H.10 and I.10 to the `model:` block of a file that declares
    ``stochastic: 0.1`` and ``games: 0.1``."""
    return _GameAnalyser(sos).run()


# ---------------------------------------------------------------------------
# The model under a profile (I.5)
# ---------------------------------------------------------------------------

def _and(a: tuple, b: tuple) -> tuple:
    return ("bin", "AND", ("par", a), ("par", b))


def _or(a: tuple, b: tuple) -> tuple:
    return ("bin", "OR", ("par", a), ("par", b))


def induced_model(model: Model, profile: str) -> Model:
    """M(D, θ, σ) of I.5 for every design, as a model of Appendix H: the
    commands of each design, each with a guard that is true only where
    the profile σ selects the command (I.4). Properties, regime maps,
    rewards and designs are those of the strategic model."""
    rules = profile_of(model, profile).rules
    false = ("bool", False)
    commands = []
    for design in model.designs:
        existing = [c for c in model.commands if c.exists_in(design.name)]
        enabled: dict = {}                    # action label -> some command has it enabled
        for c in existing:
            if c.owner is not None:
                enabled[c.action] = _or(enabled[c.action], c.guard) \
                    if c.action in enabled else c.guard
        applies = [_and(r.guard, enabled[r.choose]) if r.choose in enabled else false
                   for r in rules]
        none_before = ("bool", True)          # no earlier rule applies
        first: dict = {}                      # action label -> its rule is the first that applies
        for r, a in zip(rules, applies):
            here = _and(none_before, a)
            first[r.choose] = _or(first[r.choose], here) if r.choose in first else here
            none_before = _and(none_before, ("not", ("par", a)))
        for c in existing:
            guard = c.guard
            if c.owner is not None:
                # Selected by the first applicable rule, or the single
                # enabled command of a state in which no rule applies.
                chosen = _or(first[c.action], none_before) if c.action in first else none_before
                guard = _and(c.guard, chosen)
            commands.append(dataclasses.replace(c, guard=guard, when=[design.name], owner=None))
    return dataclasses.replace(model, kind="dtmc", commands=commands, players=[], profiles=[],
                               compliance=None, payoffs=[], equilibria=[])


def check_properties_game(model: Model, regime: str | None = None,
                          overrides: dict | None = None) -> list:
    """`stochastic_eval.check_properties` for a strategic model: every
    property is evaluated in the Markov chain of its profile (I.5)."""
    from .stochastic_eval import _design, build_chain, evaluate, valuation, verdict
    designs = [_design(model, regime)] if regime is not None else model.designs
    induced: dict = {}
    out = []
    for d in designs:
        theta = valuation(model, d, overrides)
        chains: dict = {}
        results = []
        for p in model.properties:
            if d.name not in p.regimes:
                continue
            if p.profile not in chains:
                if p.profile not in induced:
                    induced[p.profile] = induced_model(model, p.profile)
                chains[p.profile] = build_chain(induced[p.profile], d, theta)
            chain = chains[p.profile]
            value = evaluate(chain, p.expr)
            results.append({"id": p.id, "expr": p.text, "value": value, "profile": p.profile,
                            "states": len(chain.states), "result": verdict(p.expr, value)})
        states = max((r["states"] for r in results), default=0)
        out.append({"design": d.name, "parameters": theta, "states": states,
                    "properties": results})
    return out


def model_of_regime_map(model: Model, map_id: str) -> Model:
    """The model of Appendix H in which a regime map of a strategic model
    is evaluated: the Markov chains of the profile of the map (I.5)."""
    query = next((q for q in model.regime_maps if q.id == map_id), None)
    if query is None or query.profile is None:
        return model                           # the caller reports an unknown id
    return induced_model(model, query.profile)


# ---------------------------------------------------------------------------
# IR (I.13)
# ---------------------------------------------------------------------------

def _insert_after(d: dict, after: str, key: str, value: Any) -> dict:
    out = {}
    for k, v in d.items():
        out[k] = v
        if k == after:
            out[key] = value
    return out


def game_to_ir(model: Model) -> dict:
    """The `stochastic_model` field of the IR for a strategic model (I.13)."""
    ir = model_to_ir(dataclasses.replace(model, kind="dtmc"))
    ir["kind"] = "smg"
    ir["parameters"] = [_insert_after(d, "open", "open_in_equilibria", p.open_in_equilibria)
                        for d, p in zip(ir["parameters"], model.params)]
    ir["commands"] = [_insert_after(d, "action", "owner", c.owner)
                      for d, c in zip(ir["commands"], model.commands)]
    ir["properties"] = [_insert_after(d, "regimes", "profile", p.profile)
                        for d, p in zip(ir["properties"], model.properties)]
    ir["regime_maps"] = [_insert_after(d, "regimes", "profile", q.profile)
                         for d, q in zip(ir["regime_maps"], model.regime_maps)]
    ir["players"] = [{"id": p.id, "actors": list(p.actors)} for p in model.players]
    ir["profiles"] = [
        {"id": p.id,
         "rules": [{"guard": r.guard_text, "choose": r.choose} for r in p.rules]}
        for p in model.profiles
    ]
    ir["compliance"] = model.compliance
    payoffs = []
    for p in model.payoffs:
        terms = []
        for t in p.terms:
            term = {"reward": t.reward, "weight": t.weight_text or None}
            if model.grounding:               # J.10: written when the extension is declared
                term["sanction_of"] = t.sanction_of
                term["reward_of"] = t.reward_of
            terms.append(term)
        payoffs.append({"player": p.player, "until": p.until_text, "terms": terms})
    ir["payoffs"] = payoffs
    ir["equilibria"] = [
        {
            "id": q.id,
            "profile": q.profile,
            "regimes": list(q.regimes),
            "epsilon": _json_number(q.epsilon),
            "over": ({k: [_json_number(lo), _json_number(hi)] for k, (lo, hi) in q.over.items()}
                     if q.is_map else None),
            "resolution": _json_number(q.resolution) if q.is_map else None,
            "concept": q.concept,
        }
        for q in model.equilibria
    ]
    return ir


# ---------------------------------------------------------------------------
# Export (I.11, informative)
# ---------------------------------------------------------------------------

def prism_games_header(model: Model, design: Design) -> list:
    """The first lines of the model file of a design of a strategic model:
    `smg` and one `player ... endplayer` construct per player, in
    declaration order. A player that owns no command of the design gets a
    construct without actions: PRISM-games 3.2.4 rejects an `smg` file
    without any player ("SMG model has no player definitions"), for
    example a design in which every command is unowned, and a player that
    is not written cannot be named in a coalition."""
    from .stochastic import ExportError
    written: dict = {}
    for player in model.players:
        name = _prism_name(player.id)
        if written.setdefault(name, player.id) != player.id:
            raise ExportError(
                f"cannot export: the players '{written[name]}' and '{player.id}' are both "
                f"written '{name}' in the PRISM-games model (I.11)")
    out = ["smg", ""]
    for player in model.players:
        labels = []
        for c in model.commands:
            if c.owner == player.id and c.exists_in(design.name) and c.action not in labels:
                labels.append(c.action)
        out.append(f"player {_prism_name(player.id)}")
        if labels:
            out.append("  " + ", ".join(f"[{_prism_name(a)}]" for a in labels))
        out += ["endplayer", ""]
    return out


def export_warnings(model: Model) -> list:
    """I.11: a player whose name equals another identifier of the model,
    after the renaming of H.11 with the keywords of PRISM-games."""
    if model.kind != "smg":
        return []
    from . import stochastic
    saved = stochastic._extra_keywords
    stochastic._extra_keywords = stochastic.PRISM_GAMES_KEYWORDS
    try:
        return _export_warnings(model)
    finally:
        stochastic._extra_keywords = saved


def _export_warnings(model: Model) -> list:
    names: dict = {}
    for p in model.params:
        names[_prism_name(p.id)] = f"the parameter '{p.id}'"
    for v in model.variables:
        names[_prism_name(v.id)] = f"the variable '{v.id}'"
        for lit in v.literals:
            names[_prism_name(f"{v.id}_{lit}")] = f"the literal '{lit}' of variable '{v.id}'"
    for name in model.labels:
        names.setdefault(_prism_name(name), f"the label '{name}'")
    for r in model.rewards:
        names.setdefault(_prism_name(r.id), f"the reward structure '{r.id}'")
    for c in model.commands:
        if c.action:
            names.setdefault(_prism_name(c.action), f"the action label '{c.action}'")
    out = []
    for player in model.players:
        other = names.get(_prism_name(player.id))
        if other:
            out.append(f"the player '{player.id}' has the name of {other}; Appendix I does "
                       f"not determine whether PRISM-games accepts this (I.11)")
    return out
