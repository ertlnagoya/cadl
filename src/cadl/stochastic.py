"""Stochastic model extension (Appendix H, stochastic 0.1).

A file that declares ``stochastic: 0.1`` under ``extensions:`` may carry a
``model:`` block: a parametric discrete-time Markov chain written as
guarded probabilistic commands, with properties and regime map queries.
This module

- parses the three expression sub-languages of H.2 (``state_pred``,
  ``param_expr``, ``property``) with small parsers of their own; the
  expression grammar of A.10 (``grammar.lark``) is not used and not changed;
- applies the static rules S-1 to S-15 of H.10 (`analyse`);
- lowers the block to the IR of H.13 (`model_to_ir`);
- exports each design to the PRISM modelling language by the rules of
  H.11 (`export_prism`).

All numbers are exact (`fractions.Fraction`): a YAML number is read as the
shortest decimal that round-trips the floating-point value (H.2).
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Iterator

from .ast_nodes import ModelBlock, SoSDefinition

STOCHASTIC_EXTENSION = ("stochastic", "0.1")

# S-7 and S-9 are decided by enumerating the full state space S (the
# cartesian product of the variable domains). This processor refuses a
# model with more states than this; the appendix sets no limit.
STATE_SPACE_LIMIT = 200_000

# S-2: keywords of the expression sub-language of A.11, `IN`, and the
# keywords of the sub-languages of Appendix H.
MODEL_KEYWORDS = frozenset({
    "AND", "OR", "NOT", "true", "false", "in", "exists", "IN",
    "rest", "F", "U", "P", "R",
})

# H.11: the 55 reserved keywords of the PRISM language (PRISM manual, "The
# PRISM Language", Modules And Variables; the same list as the appendix).
PRISM_KEYWORDS = frozenset({
    "A", "bool", "clock", "const", "ctmc", "C", "double", "dtmc", "E",
    "endinit", "endinvariant", "endmodule", "endobservables", "endrewards",
    "endsystem", "false", "formula", "filter", "func", "F", "global", "G",
    "init", "invariant", "I", "int", "label", "max", "mdp", "min",
    "module", "X", "nondeterministic", "observable", "observables", "of",
    "Pmax", "Pmin", "P", "pomdp", "popta", "probabilistic", "prob", "pta",
    "rate", "rewards", "Rmax", "Rmin", "R", "S", "stochastic", "system",
    "true", "U", "W",
})

# H.11: built-in function names of PRISM, escaped like the keywords
# (`min`, `max` and `func` are keywords already).
PRISM_FUNCTIONS = frozenset({"floor", "ceil", "round", "pow", "mod", "log"})

# Labels that PRISM defines in every model; they cannot be defined again (S-2).
PRISM_BUILTIN_LABELS = frozenset({"init", "deadlock"})

# H.1: reserved for a later version (S-15).
RESERVED_MODEL_KEYS = ("players", "payoffs", "equilibria")
RESERVED_SOURCES = ("motivation.rho", "motivation.kappa")

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_YAML11_HINT = (
    "a YAML 1.1 loader reads an unquoted ON, No, Off, Yes, null or ~ as a "
    "boolean or null, not as a name: write the name in quotes"
)
_COMP_OPS = ("==", "!=", "<=", ">=", "<", ">")


def stochastic_declared(sos: SoSDefinition) -> bool:
    """True when the file declares ``stochastic: 0.1`` under ``extensions:``."""
    return STOCHASTIC_EXTENSION in sos.extensions


# ---------------------------------------------------------------------------
# Numbers (H.2: exact; a YAML number is its shortest round-trip decimal)
# ---------------------------------------------------------------------------

def yaml_number(value: Any) -> Fraction | None:
    """A YAML number as an exact rational, or None when it is not a finite number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float) and math.isfinite(value):
        return Fraction(repr(value))
    return None


def decimal_text(value: Fraction) -> str:
    """The shortest decimal of an exact number, without an exponent (H.11).

    A number that has no finite decimal expansion (it can only come from
    ``--set`` of the reference evaluator) is written as ``n/d``.
    """
    den = value.denominator
    for p in (2, 5):
        while den % p == 0:
            den //= p
    if den != 1:
        return f"{value.numerator}/{value.denominator}"
    scale = 0
    scaled = value
    while scaled.denominator != 1:
        scaled *= 10
        scale += 1
    digits = str(abs(scaled.numerator))
    if scale:
        digits = digits.rjust(scale + 1, "0")
        digits = digits[:-scale] + "." + digits[-scale:]
    return ("-" if value < 0 else "") + digits


def _json_number(value: Fraction | None) -> Any:
    if value is None:
        return None
    return int(value) if value.denominator == 1 else float(value)


# ---------------------------------------------------------------------------
# The expression sub-languages of H.2
# ---------------------------------------------------------------------------

class StochasticSyntaxError(ValueError):
    """A string does not conform to a sub-language of H.2."""


_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<float>\d+\.\d+)
      | (?P<int>\d+)
      | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<str>"[^"]*")
      | (?P<op>==|!=|<=|>=|=\?|[<>+\-*()\[\]{}])
    )""",
    re.VERBOSE,
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    end = len(text.rstrip())
    while pos < end:
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            rest = text[pos:].strip()
            raise StochasticSyntaxError(f"unexpected text '{rest[:20]}'")
        kind = m.lastgroup
        tok = m.group(kind)
        if kind in ("float", "int") and m.end() < len(text) and (
                text[m.end()].isalnum() or text[m.end()] in "_."):
            raise StochasticSyntaxError(f"malformed number near '{text[m.start():m.end() + 1]}'")
        tokens.append((kind, tok))
        pos = m.end()
    tokens.append(("end", ""))
    return tokens


class _Parser:
    """Recursive-descent parser for `state_pred`, `param_expr` and `property`.

    A numeric literal is digits with an optional fractional part; an
    exponent form (`1e-3`) is a syntax error.

    Trees are tuples. ``state_pred``::

        ("int", value, text) | ("bool", value) | ("id", name)
        ("par", e) | ("neg", e) | ("not", e) | ("bin", op, left, right)

    ``param_expr``::

        ("num", Fraction, text) | ("id", name)
        ("par", e) | ("neg", e) | ("bin", op, left, right)

    Parentheses are kept as nodes because the export of H.11 keeps them.
    """

    def __init__(self, text: str) -> None:
        self.tokens = _tokenize(text)
        self.i = 0

    # -- token helpers --
    def peek(self) -> tuple[str, str]:
        return self.tokens[self.i]

    def at(self, kind: str, text: str | None = None) -> bool:
        k, t = self.tokens[self.i]
        return k == kind and (text is None or t == text)

    def accept(self, kind: str, text: str | None = None) -> str | None:
        if self.at(kind, text):
            self.i += 1
            return self.tokens[self.i - 1][1]
        return None

    def expect(self, kind: str, text: str | None = None) -> str:
        tok = self.accept(kind, text)
        if tok is None:
            wanted = f"'{text}'" if text is not None else {
                "int": "an integer literal", "str": "a quoted name",
                "id": "an identifier", "end": "the end of the text"}.get(kind, kind)
            raise StochasticSyntaxError(f"expected {wanted}, found {self.found()}")
        return tok

    def found(self) -> str:
        kind, tok = self.peek()
        return "the end of the text" if kind == "end" else f"'{tok}'"

    def end(self) -> None:
        if not self.at("end"):
            raise StochasticSyntaxError(f"unexpected {self.found()}")

    # -- state_pred --
    def state_pred(self) -> tuple:
        left = self.s_and()
        while self.accept("id", "OR"):
            left = ("bin", "OR", left, self.s_and())
        return left

    def s_and(self) -> tuple:
        left = self.s_not()
        while self.accept("id", "AND"):
            left = ("bin", "AND", left, self.s_not())
        return left

    def s_not(self) -> tuple:
        if self.accept("id", "NOT"):
            return ("not", self.s_not())
        return self.s_cmp()

    def s_cmp(self) -> tuple:
        left = self.s_sum()
        kind, tok = self.peek()
        if kind == "op" and tok in _COMP_OPS:
            self.i += 1
            return ("bin", tok, left, self.s_sum())
        return left

    def s_sum(self) -> tuple:
        left = self.s_term()
        while self.at("op", "+") or self.at("op", "-"):
            op = self.tokens[self.i][1]
            self.i += 1
            left = ("bin", op, left, self.s_term())
        return left

    def s_term(self) -> tuple:
        left = self.s_factor()
        while self.accept("op", "*"):
            left = ("bin", "*", left, self.s_factor())
        return left

    def s_factor(self) -> tuple:
        if self.accept("op", "("):
            inner = self.state_pred()
            self.expect("op", ")")
            return ("par", inner)
        if self.accept("op", "-"):
            return ("neg", self.s_factor())
        kind, tok = self.peek()
        if kind == "int":
            self.i += 1
            return ("int", int(tok), tok)
        if kind == "float":
            raise StochasticSyntaxError(
                f"a state predicate has no floating-point literal ('{tok}')")
        if kind == "id":
            if tok in ("true", "false"):
                self.i += 1
                return ("bool", tok == "true")
            if tok in MODEL_KEYWORDS:
                raise StochasticSyntaxError(f"unexpected keyword '{tok}'")
            self.i += 1
            return ("id", tok)
        raise StochasticSyntaxError(f"expected an operand, found {self.found()}")

    # -- param_expr --
    def param_expr(self) -> tuple:
        left = self.p_term()
        while self.at("op", "+") or self.at("op", "-"):
            op = self.tokens[self.i][1]
            self.i += 1
            left = ("bin", op, left, self.p_term())
        return left

    def p_term(self) -> tuple:
        left = self.p_factor()
        while self.accept("op", "*"):
            left = ("bin", "*", left, self.p_factor())
        return left

    def p_factor(self) -> tuple:
        if self.accept("op", "("):
            inner = self.param_expr()
            self.expect("op", ")")
            return ("par", inner)
        if self.accept("op", "-"):
            return ("neg", self.p_factor())
        kind, tok = self.peek()
        if kind in ("int", "float"):
            self.i += 1
            return ("num", Fraction(tok), tok)
        if kind == "id":
            if tok in MODEL_KEYWORDS:
                raise StochasticSyntaxError(f"unexpected keyword '{tok}'")
            self.i += 1
            return ("id", tok)
        raise StochasticSyntaxError(f"expected an operand, found {self.found()}")

    # -- property --
    def step_bound(self) -> int | None:
        if self.accept("op", "<="):
            return int(self.expect("int"))
        return None

    def property(self) -> "PropertyExpr":
        prop = PropertyExpr()
        if self.accept("id", "P"):
            prop.quantity = "P"
        elif self.accept("id", "R"):
            prop.quantity = "R"
            self.expect("op", "{")
            prop.reward = self.expect("str")[1:-1]
            self.expect("op", "}")
        else:
            raise StochasticSyntaxError(f"expected P or R{{\"...\"}}, found {self.found()}")
        if self.accept("op", "=?"):
            prop.op = "=?"
        else:
            kind, tok = self.peek()
            if not (kind == "op" and tok in (">=", ">", "<=", "<")):
                raise StochasticSyntaxError(
                    f"expected =? or a threshold (>=, >, <=, <), found {self.found()}")
            self.i += 1
            prop.op = tok
            kind, tok = self.peek()
            if kind not in ("int", "float"):
                raise StochasticSyntaxError(
                    f"expected a number after '{prop.op}', found {self.found()}")
            self.i += 1
            prop.threshold = Fraction(tok)
            prop.threshold_text = tok
        self.expect("op", "[")
        if self.accept("id", "F"):
            prop.path = "F"
            prop.bound = self.step_bound()
            prop.right = self.state_pred()
        else:
            prop.path = "U"
            prop.left = self.state_pred()
            if not self.accept("id", "U"):
                raise StochasticSyntaxError(
                    f"expected U (a path is 'F phi' or 'phi1 U phi2'), found {self.found()}")
            prop.bound = self.step_bound()
            prop.right = self.state_pred()
        self.expect("op", "]")
        self.end()
        return prop


@dataclass
class PropertyExpr:
    """A parsed `property` of H.2."""
    quantity: str = "P"                # "P" | "R"
    reward: str | None = None          # name of the reward structure, with R
    op: str = "=?"                     # "=?" | ">=" | ">" | "<=" | "<"
    threshold: Fraction | None = None
    threshold_text: str = ""
    path: str = "F"                    # "F" | "U"
    bound: int | None = None           # step bound
    left: tuple | None = None          # phi1 of U
    right: tuple | None = None         # phi of F, phi2 of U

    @property
    def is_query(self) -> bool:
        return self.op == "=?"


def parse_state_pred(text: str) -> tuple:
    """Parse a `state_pred` of H.2. Raises StochasticSyntaxError."""
    parser = _Parser(text)
    tree = parser.state_pred()
    parser.end()
    return tree


def parse_param_expr(text: str) -> tuple:
    """Parse a `param_expr` of H.2. Raises StochasticSyntaxError."""
    parser = _Parser(text)
    tree = parser.param_expr()
    parser.end()
    return tree


def parse_property(text: str) -> PropertyExpr:
    """Parse a `property` of H.2. Raises StochasticSyntaxError."""
    return _Parser(text).property()


def param_names(tree: tuple) -> set[str]:
    """The identifiers of a `param_expr`."""
    kind = tree[0]
    if kind == "id":
        return {tree[1]}
    if kind in ("par", "neg"):
        return param_names(tree[1])
    if kind == "bin":
        return param_names(tree[2]) | param_names(tree[3])
    return set()


def eval_param_expr(tree: tuple, theta: dict[str, Fraction]) -> Fraction:
    """The exact value of a `param_expr` under a parameter valuation."""
    kind = tree[0]
    if kind == "num":
        return tree[1]
    if kind == "id":
        return theta[tree[1]]
    if kind == "par":
        return eval_param_expr(tree[1], theta)
    if kind == "neg":
        return -eval_param_expr(tree[1], theta)
    left = eval_param_expr(tree[2], theta)
    right = eval_param_expr(tree[3], theta)
    if tree[1] == "+":
        return left + right
    if tree[1] == "-":
        return left - right
    return left * right


# ---------------------------------------------------------------------------
# The checked model
# ---------------------------------------------------------------------------

@dataclass
class Param:
    id: str
    source: str | None = None          # "C.alpha", "environment.X", or None (free)
    institutional: bool = False
    lo: Fraction | None = None
    hi: Fraction | None = None
    value: Fraction | None = None      # the declared value of H.3
    open: bool = False                 # listed in the over: of a regime map
    open_in_equilibria: bool = False   # listed in the over: of an equilibrium map (I.13)


@dataclass
class Var:
    id: str
    kind: str = "bool"                 # "bool" | "int" | "enum"
    lo: int | None = None
    hi: int | None = None
    literals: tuple = ()
    init: Any = None                   # bool | int | literal name

    @property
    def domain(self) -> list:
        if self.kind == "bool":
            return [False, True]
        if self.kind == "int":
            return list(range(self.lo, self.hi + 1))
        return list(self.literals)

    @property
    def type(self) -> Any:
        return ("enum", self.literals) if self.kind == "enum" else self.kind


@dataclass
class Update:
    prob: tuple | None = None          # param_expr tree; None for `rest`
    prob_text: str = "rest"
    sets: list = field(default_factory=list)   # (variable, state tree, text), as written


@dataclass
class Command:
    index: int = 0                     # position in model.commands:, from 1
    action: str | None = None
    when: list = field(default_factory=list)   # [] means every design
    guard: tuple = ("bool", True)
    guard_text: str = "true"
    updates: list = field(default_factory=list)
    owner: str | None = None           # the player who chooses it (Appendix I, I.3)

    def exists_in(self, design: str) -> bool:
        return not self.when or design in self.when


@dataclass
class RewardItem:
    action: str | None = None
    guard: tuple = ("bool", True)
    guard_text: str = "true"
    value: tuple = ("num", Fraction(0), "0")
    value_text: str = "0"


@dataclass
class Reward:
    id: str = ""
    items: list = field(default_factory=list)


@dataclass
class Prop:
    id: str = ""
    regimes: list = field(default_factory=list)     # the designs it concerns
    text: str = ""
    expr: PropertyExpr | None = None
    profile: str | None = None         # strategic model: evaluated under this profile (I.5)


@dataclass
class RegimeMapQuery:
    id: str = ""
    over: dict = field(default_factory=dict)        # parameter -> (lo, hi)
    regimes: list = field(default_factory=list)
    direction: str = "maximize"
    objective_text: str = ""
    objective: PropertyExpr | None = None
    constraint_text: str | None = None
    constraint: PropertyExpr | None = None
    resolution: Fraction = Fraction(1, 20)
    profile: str | None = None         # strategic model: evaluated under this profile (I.5)


@dataclass
class Design:
    name: str = "base"
    assign: dict = field(default_factory=dict)      # parameter -> Fraction


@dataclass
class Model:
    """A `model:` block that satisfies the rules of H.10."""
    sos_name: str = ""
    kind: str = "dtmc"
    params: list = field(default_factory=list)
    variables: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)      # name -> (state tree, text)
    commands: list = field(default_factory=list)
    rewards: list = field(default_factory=list)
    properties: list = field(default_factory=list)
    regime_maps: list = field(default_factory=list)
    designs: list = field(default_factory=list)
    # Strategic model (Appendix I, kind "smg"); empty for a model of Appendix H.
    players: list = field(default_factory=list)
    profiles: list = field(default_factory=list)
    compliance: str | None = None
    payoffs: list = field(default_factory=list)
    equilibria: list = field(default_factory=list)
    # Weights of payoff terms that are defined outside Appendix I (a term
    # with sanction_of: or reward_of:, Appendix J): (player, reward) -> param_expr tree.
    term_weights: dict = field(default_factory=dict)

    def design(self, name: str) -> Design | None:
        return next((d for d in self.designs if d.name == name), None)

    def fixed_values(self, design: Design) -> dict:
        """The fixed value of every parameter under a design (H.3)."""
        return {p.id: design.assign.get(p.id, p.value) for p in self.params}

    def var_index(self) -> dict:
        return {v.id: i for i, v in enumerate(self.variables)}

    def initial_state(self) -> tuple:
        return tuple(v.init for v in self.variables)


@dataclass
class Analysis:
    """Result of applying H.10 to a file."""
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    infos: list = field(default_factory=list)
    # The checked model; None when the block has an error.
    model: Model | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def compile_state_expr(tree: tuple, index: dict, labels: dict) -> Callable:
    """A resolved state expression as a function of a state tuple.

    ``index`` maps a variable to its position; ``labels`` maps a label to
    its compiled predicate.
    """
    kind = tree[0]
    if kind in ("int", "bool"):
        value = tree[1]
        return lambda s: value
    if kind == "var":
        i = index[tree[1]]
        return lambda s: s[i]
    if kind == "lit":
        name = tree[1]
        return lambda s: name
    if kind == "label":
        return labels[tree[1]]
    if kind == "par":
        return compile_state_expr(tree[1], index, labels)
    if kind == "neg":
        f = compile_state_expr(tree[1], index, labels)
        return lambda s: -f(s)
    if kind == "not":
        f = compile_state_expr(tree[1], index, labels)
        return lambda s: not f(s)
    op = tree[1]
    a = compile_state_expr(tree[2], index, labels)
    b = compile_state_expr(tree[3], index, labels)
    return {
        "OR": lambda s: a(s) or b(s),
        "AND": lambda s: a(s) and b(s),
        "==": lambda s: a(s) == b(s),
        "!=": lambda s: a(s) != b(s),
        "<": lambda s: a(s) < b(s),
        "<=": lambda s: a(s) <= b(s),
        ">": lambda s: a(s) > b(s),
        ">=": lambda s: a(s) >= b(s),
        "+": lambda s: a(s) + b(s),
        "-": lambda s: a(s) - b(s),
        "*": lambda s: a(s) * b(s),
    }[op]


def update_probabilities(command: Command, theta: dict) -> list:
    """The probability of each update of a command under a valuation (H.5)."""
    values = [None if u.prob is None else eval_param_expr(u.prob, theta)
              for u in command.updates]
    others = sum((v for v in values if v is not None), Fraction(0))
    return [1 - others if v is None else v for v in values]


def ill_defined(model: Model, design: str, theta: dict) -> str | None:
    """Why a design is not well-defined at a valuation (H.5), or None."""
    for c in model.commands:
        if not c.exists_in(design):
            continue
        for u, p in zip(c.updates, update_probabilities(c, theta)):
            if not 0 <= p <= 1:
                what = "'rest'" if u.prob is None else f"'{u.prob_text}'"
                return (f"the probability {what} of command {c.index} is "
                        f"{decimal_text(p)}, outside [0, 1]")
    for r in model.rewards:
        for item in r.items:
            v = eval_param_expr(item.value, theta)
            if v < 0:
                return (f"the value '{item.value_text}' of reward '{r.id}' is "
                        f"{decimal_text(v)}, which is negative")
    return None


def regimes_of(sos: SoSDefinition) -> list[str]:
    """The regimes of a file: the names used in from: and to: of transitions (H.6)."""
    out: list[str] = []
    for t in sos.transitions:
        for name in (t.from_regime, t.to_regime):
            if name and name not in out:
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# Static semantics (H.10)
# ---------------------------------------------------------------------------

class _TypeErr(ValueError):
    pass


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


class _Analyser:
    def __init__(self, sos: SoSDefinition) -> None:
        self.sos = sos
        self.block: ModelBlock = sos.model  # type: ignore[assignment]
        self.out = Analysis()
        self.model = Model(sos_name=sos.name)
        self.ns: dict[str, str] = {}          # shared namespace of S-2: name -> kind
        self.params: dict[str, Param] = {}
        self.vars: dict[str, Var] = {}
        self.literal_names: set[str] = set()
        self.regimes = regimes_of(sos)
        self.design_names = self.regimes or ["base"]
        # Commands whose guard and updates are well typed (for S-7, S-9).
        self.enumerable: list[Command] = []
        self.vars_ok = True

    # -- diagnostics --
    def error(self, message: str, rule: str) -> None:
        self.out.errors.append(f"{message} ({rule})")

    def warn(self, message: str, rule: str) -> None:
        self.out.warnings.append(f"{message} ({rule})")

    def info(self, message: str, rule: str) -> None:
        self.out.infos.append(f"{message} ({rule})")

    # -- names --
    def ident(self, value: Any, what: str, rule: str = "S-2") -> str | None:
        """A name that must be an identifier and no keyword (S-2)."""
        if value is None or isinstance(value, bool):
            if value is None:
                self.error(f"{what} is missing or null; {_YAML11_HINT}", rule)
            else:
                self.error(f"{what} is the boolean '{_as_text(value)}'; {_YAML11_HINT}", rule)
            return None
        if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
            self.error(f"{what} '{_as_text(value)}' is not an identifier", rule)
            return None
        if value in MODEL_KEYWORDS:
            self.error(f"{what} '{value}' is a keyword and cannot be used as a name", rule)
            return None
        return value

    def declare(self, name: str, kind: str) -> bool:
        old = self.ns.get(name)
        if old is None:
            self.ns[name] = kind
            return True
        if old == kind == "enumeration literal":
            return True
        if old == kind:
            self.error(f"The {kind} '{name}' is declared twice", "S-2")
        else:
            self.error(f"'{name}' is declared as a {old} and as a {kind}; parameters, "
                       f"variables, labels and enumeration literals share one namespace", "S-2")
        return False

    def name_list(self, value: Any, what: str, rule: str) -> list[str] | None:
        """A sequence of names (when:, regimes:); None when malformed."""
        if not isinstance(value, list):
            self.error(f"{what} must be a sequence of names", rule)
            return None
        out = []
        ok = True
        for item in value:
            if item is None or isinstance(item, bool):
                self.error(f"{what} has the entry '{_as_text(item)}', a boolean or null; "
                           f"{_YAML11_HINT}", rule)
                ok = False
            elif not isinstance(item, str):
                self.error(f"{what} has the entry '{item}', which is not a name", rule)
                ok = False
            else:
                out.append(item)
        return out if ok else None

    # -- the block --
    def run(self) -> Analysis:
        block = self.block
        for rule, message in block.malformed:
            self.error(message, rule)
        self.check_reserved()
        self.check_structure()
        self.check_parameters()
        self.check_variables()
        self.check_labels()
        self.check_commands()
        self.check_regimes()
        self.check_rewards()
        self.check_properties()
        self.check_regime_maps()
        self.check_reward_parameters()
        self.check_states()
        self.check_well_defined()
        if self.out.ok:
            self.out.model = self.model
        return self.out

    def check_reserved(self) -> None:
        block = self.block
        for key in RESERVED_MODEL_KEYS:
            if key in block.keys:
                self.info(f"model.{key} is reserved for a later version of the "
                          f"stochastic extension (H.1) and is not interpreted", "S-15")
        for i, v in enumerate(block.variables, start=1):
            if "owner" in v.keys:
                self.info(f"'owner' of variable {i} of the model is reserved for a later "
                          f"version (H.1) and is not interpreted", "S-15")
        for i, c in enumerate(block.commands, start=1):
            for key in ("owner", "sync"):
                if key in c.keys:
                    self.info(f"'{key}' of command {i} of the model is reserved for a later "
                              f"version (H.1) and is not interpreted", "S-15")
        if block.kind == "mdp":
            self.info("kind 'mdp' is reserved for a later version (H.1) and is "
                      "given no meaning", "S-15")
        for p in block.parameters:
            if isinstance(p.source, str) and p.source.strip() in RESERVED_SOURCES:
                self.info(f"the parameter source '{p.source.strip()}' is reserved for a "
                          f"later version (H.1) and is given no meaning", "S-15")

    def check_structure(self) -> None:
        block = self.block
        if block.kind is not None and block.kind != "dtmc":
            self.error(f"model.kind is '{_as_text(block.kind)}'; stochastic 0.1 has "
                       f"only 'dtmc'", "S-1")
        if "variables" not in block.keys or not block.variables:
            self.error("model: must have variables: with at least one variable", "S-1")
        if "commands" not in block.keys:
            self.error("model: must have commands:", "S-1")

    # -- S-2, S-3, S-4: parameters --
    def check_parameters(self) -> None:
        sources: dict[str, str] = {}
        contracts = {c.id: c for c in self.sos.contracts}
        env = {}
        if self.sos.context and self.sos.context.environment:
            env = self.sos.context.environment.raw
        for i, d in enumerate(self.block.parameters, start=1):
            name = self.ident(d.id, f"The id of parameter {i} of the model")
            where = f"parameter '{name}'" if name else f"parameter {i}"
            p = Param(id=name or f"#{i}")
            source_value: Fraction | None = None
            if "from" in d.keys:
                src = d.source.strip() if isinstance(d.source, str) else None
                m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", src or "")
                if m is None:
                    self.error(f"'from' of {where} is '{_as_text(d.source)}'; it must be a "
                               f"string of the form C.alpha, C.beta, C.lambda or "
                               f"environment.X", "S-3")
                elif src in RESERVED_SOURCES:
                    self.error(f"'from' of {where} is '{src}', a reserved source that has "
                               f"no meaning in stochastic 0.1", "S-3")
                elif m.group(1) == "environment":
                    key = m.group(2)
                    p.source = src
                    if key not in env:
                        self.error(f"'from' of {where} is '{src}', but context.environment "
                                   f"has no entry '{key}'", "S-3")
                    else:
                        source_value = yaml_number(env[key])
                        if source_value is None:
                            self.error(f"'from' of {where} is '{src}', but the entry "
                                       f"'{key}' of context.environment is "
                                       f"'{_as_text(env[key])}', not a YAML number", "S-3")
                else:
                    cid, attr = m.group(1), m.group(2)
                    if attr not in ("alpha", "beta", "lambda"):
                        self.error(f"'from' of {where} is '{src}'; after the contract id "
                                   f"it must name alpha, beta or lambda", "S-3")
                    elif cid not in contracts:
                        self.error(f"'from' of {where} is '{src}', but '{cid}' is not the "
                                   f"id of a declared contract", "S-3")
                    else:
                        p.source = src
                        p.institutional = True
                        c = contracts[cid]
                        raw = {
                            "alpha": c.information.alpha if c.information else None,
                            "beta": c.authority.beta if c.authority else None,
                            "lambda": c.incentives.lambda_ if c.incentives else None,
                        }[attr]
                        source_value = yaml_number(raw)
                if "value" in d.keys:
                    self.error(f"The {where} has 'from' and 'value'; a bound parameter "
                               f"takes its value from its source", "S-3")
                if p.source is not None:
                    if p.source in sources:
                        self.warn(f"The parameters '{sources[p.source]}' and '{p.id}' have "
                                  f"the same source '{p.source}'", "S-3")
                    else:
                        sources[p.source] = p.id
                p.value = source_value
            # S-4: range
            if "range" in d.keys and d.range is not None:
                r = d.range
                nums = [yaml_number(x) for x in r] if isinstance(r, list) else []
                if len(nums) != 2 or None in nums:
                    self.error(f"'range' of {where} is '{_as_text(r)}'; it must be a "
                               f"sequence of two finite numbers [lo, hi], written "
                               f"without an exponent", "S-4")
                elif nums[0] > nums[1]:
                    self.error(f"'range' of {where} is [{decimal_text(nums[0])}, "
                               f"{decimal_text(nums[1])}]; lo must not be above hi", "S-4")
                else:
                    p.lo, p.hi = nums
                    if p.institutional and not (0 <= p.lo and p.hi <= 1):
                        self.error(f"'range' of the institutional {where} is "
                                   f"[{decimal_text(p.lo)}, {decimal_text(p.hi)}]; it must "
                                   f"lie within [0, 1]", "S-4")
            elif p.institutional:
                p.lo, p.hi = Fraction(0), Fraction(1)
            elif "from" not in d.keys or p.source is not None:
                self.error(f"The {where} has no 'range'; it is required except for a "
                           f"parameter bound to alpha, beta or lambda of a contract", "S-4")
            # value
            if "value" in d.keys and "from" not in d.keys:
                v = yaml_number(d.value)
                if v is None:
                    self.error(f"'value' of {where} is '{_as_text(d.value)}', not a "
                               f"finite number", "S-4")
                else:
                    p.value = v
            if p.value is not None and p.lo is not None and not p.lo <= p.value <= p.hi:
                origin = (f"the number {decimal_text(p.value)} of its source '{p.source}'"
                          if p.source else f"'value' {decimal_text(p.value)}")
                self.error(f"For {where}, {origin} is outside the declared range "
                           f"[{decimal_text(p.lo)}, {decimal_text(p.hi)}]", "S-4")
            if name and self.declare(name, "parameter"):
                self.params[name] = p
                self.model.params.append(p)

    # -- S-5: variables --
    def check_variables(self) -> None:
        for i, d in enumerate(self.block.variables, start=1):
            name = self.ident(d.id, f"The id of variable {i} of the model")
            where = f"variable '{name}'" if name else f"variable {i}"
            v = Var(id=name or f"#{i}")
            ok = self.var_type(d.type, v, where)
            if ok:
                ok = self.var_init(d, v, where)
            if not (name and self.declare(name, "variable")):
                ok = False
            if ok:
                self.vars[v.id] = v
                self.model.variables.append(v)
            else:
                self.vars_ok = False
        # Enumeration literals share the namespace (a literal may occur in
        # several types).
        for v in self.model.variables:
            for lit in v.literals:
                self.declare(lit, "enumeration literal")
                self.literal_names.add(lit)

    def var_type(self, t: Any, v: Var, where: str) -> bool:
        quoted = ("a type other than bool must be a quoted string, for example "
                  "\"[0..3]\" or \"{A, B}\"")
        if t is None:
            self.error(f"The {where} has no 'type'", "S-5")
            return False
        if not isinstance(t, str):
            self.error(f"'type' of {where} is '{_as_text(t)}', which YAML did not read "
                       f"as a string; {quoted}", "S-5")
            return False
        text = t.strip()
        if text == "bool":
            v.kind = "bool"
            return True
        m = re.fullmatch(r"\[\s*(-?\s*\d+)\s*\.\.\s*(-?\s*\d+)\s*\]", text)
        if m:
            lo = int(m.group(1).replace(" ", ""))
            hi = int(m.group(2).replace(" ", ""))
            if lo > hi:
                self.error(f"'type' of {where} is '{text}'; lo must not be above hi", "S-5")
                return False
            v.kind, v.lo, v.hi = "int", lo, hi
            return True
        m = re.fullmatch(r"\{(.*)\}", text, re.DOTALL)
        if m:
            lits = [x.strip() for x in m.group(1).split(",")]
            if lits == [""]:
                self.error(f"'type' of {where} is '{text}'; an enumeration needs at "
                           f"least one literal", "S-5")
                return False
            ok = True
            for lit in lits:
                if not _IDENT_RE.fullmatch(lit):
                    self.error(f"The enumeration literal '{lit}' of {where} is not an "
                               f"identifier", "S-2")
                    ok = False
                elif lit in MODEL_KEYWORDS:
                    self.error(f"The enumeration literal '{lit}' of {where} is a keyword "
                               f"and cannot be used as a name", "S-2")
                    ok = False
            dup = sorted({x for x in lits if lits.count(x) > 1})
            if dup:
                self.error(f"'type' of {where} lists the literal '{dup[0]}' twice", "S-5")
                ok = False
            if ok:
                v.kind, v.literals = "enum", tuple(lits)
            return ok
        self.error(f"'type' of {where} is '{text}'; expected bool, \"[lo..hi]\" or "
                   f"\"{{A, B, ...}}\"", "S-5")
        return False

    def var_init(self, d: Any, v: Var, where: str) -> bool:
        init = d.init
        if "init" not in d.keys:
            self.error(f"The {where} has no 'init'", "S-5")
            return False
        shown = "null" if init is None else _as_text(init)
        if v.kind == "bool":
            if isinstance(init, str) and init in ("true", "false"):
                init = init == "true"
            if not isinstance(init, bool):
                self.error(f"'init' of the bool {where} is '{shown}'; expected true "
                           f"or false", "S-5")
                return False
        elif v.kind == "int":
            if isinstance(init, str) and re.fullmatch(r"-?\d+", init.strip()):
                init = int(init)
            if isinstance(init, bool) or not isinstance(init, int):
                self.error(f"'init' of {where} is '{shown}'; expected an integer of "
                           f"[{v.lo}..{v.hi}]", "S-5")
                return False
            if not v.lo <= init <= v.hi:
                self.error(f"'init' of {where} is {init}, outside [{v.lo}..{v.hi}]", "S-5")
                return False
        else:
            if init is None or isinstance(init, bool):
                self.error(f"'init' of {where} is the boolean or null '{shown}', not a "
                           f"literal of its type; {_YAML11_HINT}", "S-5")
                return False
            if init not in v.literals:
                self.error(f"'init' of {where} is '{shown}', which is not a literal of "
                           f"its type {{{', '.join(v.literals)}}}", "S-5")
                return False
        v.init = init
        return True

    # -- S-6: state predicates --
    def pred_text(self, value: Any) -> str | None:
        """A state expression as text; a YAML integer or boolean is allowed."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            return value
        return None

    def typed(self, tree: tuple, labels_ok: bool) -> tuple:
        """Resolve and type a state expression: (type, resolved tree).

        A type is "int", "bool", ("enum", literals), or ("lit", name) for an
        enumeration literal whose type the context has yet to fix.
        """
        kind = tree[0]
        if kind == "int":
            return "int", tree
        if kind == "bool":
            return "bool", tree
        if kind == "id":
            name = tree[1]
            what = self.ns.get(name)
            if what == "variable" and name in self.vars:
                return self.vars[name].type, ("var", name)
            if what == "enumeration literal":
                return ("lit", name), ("lit", name, None)
            if what == "label":
                if not labels_ok:
                    raise _TypeErr(f"the label '{name}' may be used only in a property")
                return "bool", ("label", name)
            if what == "parameter":
                raise _TypeErr(f"the parameter '{name}' must not appear in a state predicate")
            raise _TypeErr(f"'{name}' is not a declared variable, enumeration literal"
                           + (" or label" if labels_ok else ""))
        if kind == "par":
            t, inner = self.typed(tree[1], labels_ok)
            return t, ("par", inner)
        if kind == "neg":
            t, inner = self.typed(tree[1], labels_ok)
            if t != "int":
                raise _TypeErr("unary '-' needs an integer")
            return "int", ("neg", inner)
        if kind == "not":
            t, inner = self.typed(tree[1], labels_ok)
            if t != "bool":
                raise _TypeErr("NOT needs a boolean")
            return "bool", ("not", inner)
        op = tree[1]
        lt, left = self.typed(tree[2], labels_ok)
        rt, right = self.typed(tree[3], labels_ok)
        if op in ("+", "-", "*"):
            if lt != "int" or rt != "int":
                raise _TypeErr(f"'{op}' needs two integers")
            return "int", ("bin", op, left, right)
        if op in ("<", "<=", ">", ">="):
            if lt != "int" or rt != "int":
                raise _TypeErr(f"'{op}' compares two integers")
            return "bool", ("bin", op, left, right)
        if op in ("AND", "OR"):
            if lt != "bool" or rt != "bool":
                raise _TypeErr(f"{op} needs two booleans")
            return "bool", ("bin", op, left, right)
        # == and !=
        l_lit, r_lit = _is_lit(lt), _is_lit(rt)
        if l_lit and r_lit:
            raise _TypeErr(f"'{op}' compares the two enumeration literals '{lt[1]}' and "
                           f"'{rt[1]}'; one side must be a variable")
        if l_lit or r_lit:
            lit_t, other_t = (lt, rt) if l_lit else (rt, lt)
            if not _is_enum(other_t):
                raise _TypeErr(f"the enumeration literal '{lit_t[1]}' is compared with "
                               f"a value that is not of an enumeration type")
            if lit_t[1] not in other_t[1]:
                raise _TypeErr(f"'{lit_t[1]}' is not a literal of the type "
                               f"{{{', '.join(other_t[1])}}}")
            var = _root_var(right if l_lit else left)
            if l_lit:
                left = _bind_literal(left, var)
            else:
                right = _bind_literal(right, var)
            return "bool", ("bin", op, left, right)
        if lt != rt or lt not in ("int", "bool") and not _is_enum(lt):
            raise _TypeErr(f"'{op}' compares two integers, two booleans, or two values "
                           f"of the same enumeration type")
        return "bool", ("bin", op, left, right)

    def boolean_pred(self, value: Any, where: str, labels_ok: bool = False,
                     rule: str = "S-6") -> tuple | None:
        """Parse and type a state predicate that must be boolean: (tree, text)."""
        text = self.pred_text(value)
        if text is None:
            self.error(f"{where} is '{_as_text(value)}', not a state predicate", rule)
            return None
        try:
            return self.boolean_tree(parse_state_pred(text), labels_ok), text
        except (StochasticSyntaxError, _TypeErr) as e:
            self.error(f"{where} '{text}' is not a well-typed state predicate: {e}", rule)
            return None

    def boolean_tree(self, tree: tuple, labels_ok: bool) -> tuple:
        t, resolved = self.typed(tree, labels_ok)
        if t != "bool":
            raise _TypeErr("it must be boolean")
        return resolved

    def check_labels(self) -> None:
        labels = self.block.labels
        if labels is None:
            return
        if not isinstance(labels, dict):
            self.error("model.labels must be a mapping from label names to state "
                       "predicates", "S-6")
            return
        pending = []
        for key, value in labels.items():
            name = self.ident(key, "The label name")
            if name in PRISM_BUILTIN_LABELS:
                self.error(f"The label name '{name}' cannot be used: the exported "
                           f"model always has the labels \"init\" and \"deadlock\" "
                           f"(H.11)", "S-2")
                continue
            if name and self.declare(name, "label"):
                pending.append((name, value))
        self.check_generated_names()
        for name, value in pending:
            # A label's own predicate refers to variables only (H.4).
            res = self.boolean_pred(value, f"The predicate of label '{name}'")
            if res:
                self.model.labels[name] = res

    def check_generated_names(self) -> None:
        """S-2: the exported name x_L of a literal must differ from every
        declared name and from every other generated name."""
        generated: dict[str, tuple] = {}
        for v in self.model.variables:
            for lit in v.literals:
                gen = f"{v.id}_{lit}"
                if gen in self.ns:
                    self.error(f"The {self.ns[gen]} '{gen}' has the name that the export "
                               f"gives to the literal '{lit}' of variable '{v.id}' (H.11)",
                               "S-2")
                if gen in generated:
                    ov, ol = generated[gen]
                    self.error(f"The literal '{ol}' of variable '{ov}' and the literal "
                               f"'{lit}' of variable '{v.id}' both get the name '{gen}' in "
                               f"the export (H.11)", "S-2")
                generated[gen] = (v.id, lit)

    # -- S-7 (static part), S-8, S-10: commands --
    def check_commands(self) -> None:
        if self.block.labels is None:
            self.check_generated_names()
        actions: set[str] = set()
        for i, d in enumerate(self.block.commands, start=1):
            c = Command(index=i)
            where = f"command {i} of the model"
            ok = True
            if "action" in d.keys:
                name = self.ident(d.action, f"The action label of {where}")
                if name:
                    c.action = name
                    actions.add(name)
                else:
                    ok = False
            if "when" in d.keys and d.when is not None:
                names = self.name_list(d.when, f"'when' of {where}", "S-10")
                if names is None:
                    ok = False
                else:
                    c.when = names
                    if not self.regimes:
                        self.error(f"'when' of {where} cannot appear in a file without "
                                   f"transitions:, which has the single design 'base'",
                                   "S-10")
                        ok = False
                    else:
                        for n in names:
                            if n not in self.regimes:
                                self.error(f"'when' of {where} names '{n}', which is not "
                                           f"a regime of the file", "S-10")
                                ok = False
            if "guard" in d.keys and d.guard is not None:
                res = self.boolean_pred(d.guard, f"The guard of {where}")
                if res:
                    c.guard, c.guard_text = res
                else:
                    ok = False
            if not self.check_updates(d, c, where):
                ok = False
            self.model.commands.append(c)
            if ok:
                self.enumerable.append(c)
        self.actions = actions

    def check_updates(self, d: Any, c: Command, where: str) -> bool:
        if not isinstance(d.updates, list) or not d.updates:
            self.error(f"The {where} must have 'updates' with at least one update", "S-8")
            return False
        ok = True
        for j, u in enumerate(d.updates, start=1):
            uwhere = f"update {j} of {where}"
            upd = Update()
            c.updates.append(upd)
            if not hasattr(u, "keys") or isinstance(u, dict):
                self.error(f"The {uwhere} is not a mapping", "S-8")
                ok = False
                continue
            # prob
            prob = u.prob
            if prob is None or (isinstance(prob, str) and prob.strip() == "rest"):
                pass
            else:
                res = self.param_value(prob, f"'prob' of {uwhere}", "S-8",
                                       " or 'rest'")
                if res is None:
                    ok = False
                    upd.prob, upd.prob_text = ("num", Fraction(0), "0"), "0"
                else:
                    upd.prob, upd.prob_text = res
                    if not param_names(upd.prob):
                        v = eval_param_expr(upd.prob, {})
                        if not 0 <= v <= 1:
                            self.error(f"'prob' of {uwhere} is {decimal_text(v)}, "
                                       f"outside [0, 1]", "S-8")
                            ok = False
            # set
            if u.set is not None:
                if not isinstance(u.set, dict):
                    self.error(f"'set' of {uwhere} must be a mapping from variables to "
                               f"state expressions", "S-7")
                    ok = False
                    continue
                for key, value in u.set.items():
                    if key is None or isinstance(key, bool):
                        self.error(f"'set' of {uwhere} has the key '{_as_text(key)}', a "
                                   f"boolean or null; {_YAML11_HINT}", "S-7")
                        ok = False
                        continue
                    if key not in self.vars:
                        self.error(f"'set' of {uwhere} assigns '{key}', which is not a "
                                   f"declared variable", "S-7")
                        ok = False
                        continue
                    var = self.vars[key]
                    text = self.pred_text(value)
                    if text is None:
                        if value is None:
                            hint = f"; {_YAML11_HINT}"
                        else:
                            hint = ""
                        self.error(f"'set' of {uwhere} gives '{key}' the value "
                                   f"'{_as_text(value)}', not a state expression{hint}", "S-7")
                        ok = False
                        continue
                    if var.kind == "enum" and isinstance(value, bool):
                        self.error(f"'set' of {uwhere} gives the enumeration variable "
                                   f"'{key}' the boolean '{text}'; {_YAML11_HINT}", "S-7")
                        ok = False
                        continue
                    try:
                        t, tree = self.typed(parse_state_pred(text), False)
                    except StochasticSyntaxError as e:
                        self.error(f"'set' of {uwhere}: the value '{text}' of '{key}' is "
                                   f"not a state expression: {e}", "S-6")
                        ok = False
                        continue
                    except _TypeErr as e:
                        self.error(f"'set' of {uwhere}: the value '{text}' of '{key}' is "
                                   f"not well typed: {e}", "S-6")
                        ok = False
                        continue
                    if _is_lit(t):
                        if var.kind != "enum" or t[1] not in var.literals:
                            self.error(f"'set' of {uwhere} gives '{key}' the literal "
                                       f"'{t[1]}', which is not a value of its type", "S-7")
                            ok = False
                            continue
                        tree = _bind_literal(tree, key)
                    elif t != var.type:
                        self.error(f"'set' of {uwhere} gives '{key}' the value '{text}', "
                                   f"which does not have the type of the variable", "S-7")
                        ok = False
                        continue
                    upd.sets.append((key, tree, text))
        if not ok:
            return False
        rests = [u for u in c.updates if u.prob is None]
        if len(rests) > 1:
            self.error(f"The {where} has {len(rests)} updates with 'rest' (written or by "
                       f"omission); at most one is allowed", "S-8")
            return False
        if not rests:
            with_params = [u.prob_text for u in c.updates if param_names(u.prob)]
            if with_params:
                self.error(f"The {where} has no 'rest', but its probability "
                           f"'{with_params[0]}' contains a parameter", "S-8")
                return False
            total = sum((eval_param_expr(u.prob, {}) for u in c.updates), Fraction(0))
            if total != 1:
                self.error(f"The probabilities of {where} sum to {decimal_text(total)}, "
                           f"not to exactly 1, and it has no 'rest'", "S-8")
                return False
        return True

    def param_value(self, value: Any, where: str, rule: str, extra: str = "") -> tuple | None:
        """A number or a `param_expr` over declared parameters: (tree, text)."""
        num = yaml_number(value)
        if num is not None:
            text = decimal_text(num)
            if num < 0:
                return ("neg", ("num", -num, text[1:])), text
            return ("num", num, text), text
        if not isinstance(value, str):
            self.error(f"{where} is '{_as_text(value)}'; expected a number{extra} or an "
                       f"expression over parameters", rule)
            return None
        try:
            tree = parse_param_expr(value)
        except StochasticSyntaxError as e:
            self.error(f"{where} '{value}' is not an expression over parameters: {e}", rule)
            return None
        for name in sorted(param_names(tree)):
            if name not in self.params:
                self.error(f"{where} '{value}' uses '{name}', which is not a declared "
                           f"parameter", rule)
                return None
        return tree, value

    # -- S-10: regimes --
    def check_regimes(self) -> None:
        regs = self.block.regimes
        assign: dict[str, dict] = {name: {} for name in self.design_names}
        if "regimes" in self.block.keys and regs is not None:
            if not self.regimes:
                self.error("model.regimes cannot appear in a file without transitions:, "
                           "which has the single design 'base'", "S-10")
            elif not isinstance(regs, dict):
                self.error("model.regimes must be a mapping from regimes to "
                           "{parameters: {...}}", "S-10")
            else:
                for key, entry in regs.items():
                    if key is None or isinstance(key, bool):
                        self.error(f"model.regimes has the key '{_as_text(key)}', a boolean "
                                   f"or null; {_YAML11_HINT}", "S-10")
                        continue
                    if key not in self.regimes:
                        self.error(f"model.regimes has the key '{key}', which is not a "
                                   f"regime of the file", "S-10")
                        continue
                    params = entry.get("parameters") if isinstance(entry, dict) else None
                    if entry is not None and not isinstance(entry, dict) or (
                            params is not None and not isinstance(params, dict)):
                        self.error(f"The entry '{key}' of model.regimes must be a mapping "
                                   f"with an optional 'parameters' mapping", "S-10")
                        continue
                    for pname, value in (params or {}).items():
                        if pname not in self.params:
                            self.error(f"model.regimes.{key} assigns '{_as_text(pname)}', "
                                       f"which is not a declared parameter", "S-10")
                            continue
                        num = yaml_number(value)
                        if num is None:
                            self.error(f"model.regimes.{key} gives '{pname}' the value "
                                       f"'{_as_text(value)}', not a number", "S-10")
                            continue
                        p = self.params[pname]
                        if p.lo is not None and not p.lo <= num <= p.hi:
                            self.error(f"model.regimes.{key} gives '{pname}' the value "
                                       f"{decimal_text(num)}, outside its declared range "
                                       f"[{decimal_text(p.lo)}, {decimal_text(p.hi)}]", "S-4")
                            continue
                        assign[key][pname] = num
                for name in self.regimes:
                    if name not in regs:
                        self.warn(f"The regime '{name}' has no entry in model.regimes",
                                  "S-10")
        self.model.designs = [Design(name=n, assign=assign[n]) for n in self.design_names]

    # -- S-12: rewards --
    def check_rewards(self) -> None:
        seen: set[str] = set()
        for i, d in enumerate(self.block.rewards, start=1):
            name = self.ident(d.id, f"The id of reward structure {i} of the model")
            where = f"reward structure '{name}'" if name else f"reward structure {i}"
            r = Reward(id=name or f"#{i}")
            if name:
                if name in seen:
                    self.error(f"The reward structure '{name}' is declared twice", "S-2")
                seen.add(name)
            if not isinstance(d.items, list) or not d.items:
                self.error(f"The {where} must have 'items' with at least one item", "S-12")
            else:
                for j, it in enumerate(d.items, start=1):
                    iwhere = f"item {j} of {where}"
                    if isinstance(it, dict) or not hasattr(it, "keys"):
                        self.error(f"The {iwhere} is not a mapping", "S-12")
                        continue
                    item = RewardItem()
                    if "action" in it.keys:
                        if not isinstance(it.action, str) or it.action not in self.actions:
                            self.error(f"'action' of {iwhere} is '{_as_text(it.action)}', "
                                       f"which is not the action label of a command", "S-12")
                        else:
                            item.action = it.action
                    if "guard" in it.keys and it.guard is not None:
                        res = self.boolean_pred(it.guard, f"The guard of {iwhere}")
                        if res:
                            item.guard, item.guard_text = res
                    if "value" not in it.keys or it.value is None:
                        self.error(f"The {iwhere} has no 'value'", "S-12")
                    else:
                        res = self.param_value(it.value, f"'value' of {iwhere}", "S-12")
                        if res:
                            item.value, item.value_text = res
                    r.items.append(item)
            self.model.rewards.append(r)
        self.reward_ids = seen

    def check_reward_parameters(self) -> None:
        """S-12: a reward value mentions no parameter that a regime map opens
        (PRISM allows parameters only in probabilities)."""
        for r in self.model.rewards:
            for j, item in enumerate(r.items, start=1):
                for name in sorted(param_names(item.value)):
                    if name in self.params and self.params[name].open:
                        self.error(f"'value' of item {j} of reward structure '{r.id}' "
                                   f"mentions '{name}', which occurs in the 'over' of a "
                                   f"regime map; an open parameter may appear only in "
                                   f"probabilities", "S-12")

    # -- S-13: properties --
    def property_expr(self, value: Any, where: str) -> PropertyExpr | None:
        if not isinstance(value, str):
            self.error(f"{where} is '{_as_text(value)}'; a property is a quoted "
                       f"string", "S-13")
            return None
        try:
            prop = parse_property(value)
        except StochasticSyntaxError as e:
            self.error(f"{where} '{value}' is not a property: {e}", "S-13")
            return None
        ok = True
        if prop.quantity == "R":
            if prop.reward not in self.reward_ids:
                self.error(f"{where} '{value}' names the reward structure "
                           f"'{prop.reward}', which is not declared", "S-13")
                ok = False
            if prop.path != "F" or prop.bound is not None:
                self.error(f"{where} '{value}': with R the path must be 'F phi' without "
                           f"a step bound", "S-13")
                ok = False
        elif prop.threshold is not None and not 0 <= prop.threshold <= 1:
            self.error(f"{where} '{value}' has the probability threshold "
                       f"{prop.threshold_text}, outside [0, 1]", "S-13")
            ok = False
        for side in ("left", "right"):
            tree = getattr(prop, side)
            if tree is None:
                continue
            try:
                setattr(prop, side, self.boolean_tree(tree, True))
            except _TypeErr as e:
                self.error(f"{where} '{value}' has a state predicate that is not well "
                           f"typed: {e}", "S-6")
                ok = False
        return prop if ok else None

    def check_properties(self) -> None:
        self.query_ids: set[str] = set()
        for i, d in enumerate(self.block.properties, start=1):
            name = self.ident(d.id, f"The id of property {i} of the model")
            where = f"property '{name}'" if name else f"property {i}"
            self.declare_query(name, "property")
            p = Prop(id=name or f"#{i}", regimes=list(self.design_names))
            if "regimes" in d.keys and d.regimes is not None:
                names = self.name_list(d.regimes, f"'regimes' of {where}", "S-13")
                if names is not None:
                    self.check_regime_names(names, f"'regimes' of {where}",
                                            self.design_names, "a design", "S-13")
                    p.regimes = names
            if "expr" not in d.keys or d.expr is None:
                self.error(f"The {where} has no 'expr'", "S-13")
            else:
                p.expr = self.property_expr(d.expr, f"'expr' of {where}")
                p.text = d.expr if isinstance(d.expr, str) else _as_text(d.expr)
            self.model.properties.append(p)

    def declare_query(self, name: str | None, kind: str) -> None:
        if not name:
            return
        if name in self.query_ids:
            self.error(f"The id '{name}' is used twice among the properties and regime "
                       f"maps (this {kind})", "S-2")
        self.query_ids.add(name)

    def check_regime_names(self, names: list, where: str, known: list, what: str,
                           rule: str) -> bool:
        ok = True
        for n in names:
            if n not in known:
                self.error(f"{where} names '{n}', which is not {what} of the file", rule)
                ok = False
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            self.error(f"{where} names '{dup[0]}' more than once", rule)
            ok = False
        return ok

    # -- S-14: regime maps --
    def check_regime_maps(self) -> None:
        if "regime_maps" in self.block.keys and self.block.regime_maps and not self.regimes:
            self.error("model.regime_maps cannot appear in a file without transitions:, "
                       "which has the single design 'base'", "S-10")
        assigned = {p for d in self.model.designs for p in d.assign}
        for i, d in enumerate(self.block.regime_maps, start=1):
            name = self.ident(d.id, f"The id of regime map {i} of the model")
            where = f"regime map '{name}'" if name else f"regime map {i}"
            self.declare_query(name, "regime map")
            q = RegimeMapQuery(id=name or f"#{i}", regimes=list(self.regimes))
            # over
            if not isinstance(d.over, dict) or not 1 <= len(d.over) <= 4:
                self.error(f"'over' of {where} must be a mapping with one to four "
                           f"parameters, each with an interval [lo, hi]", "S-14")
            else:
                for key, rng in d.over.items():
                    if key is None or isinstance(key, bool):
                        self.error(f"'over' of {where} has the key '{_as_text(key)}', a "
                                   f"boolean or null; {_YAML11_HINT}", "S-14")
                        continue
                    if key not in self.params:
                        self.error(f"'over' of {where} lists '{key}', which is not a "
                                   f"declared parameter", "S-14")
                        continue
                    nums = [yaml_number(x) for x in rng] if isinstance(rng, list) else []
                    if len(nums) != 2 or None in nums:
                        self.error(f"'over' of {where} gives '{key}' the interval "
                                   f"'{_as_text(rng)}'; it must be a sequence of two "
                                   f"numbers [lo, hi]", "S-14")
                        continue
                    lo, hi = nums
                    p = self.params[key]
                    if not lo < hi:
                        self.error(f"'over' of {where} gives '{key}' the interval "
                                   f"[{decimal_text(lo)}, {decimal_text(hi)}]; lo must be "
                                   f"below hi", "S-14")
                        continue
                    if p.lo is not None and not (p.lo <= lo and hi <= p.hi):
                        self.error(f"'over' of {where} gives '{key}' the interval "
                                   f"[{decimal_text(lo)}, {decimal_text(hi)}], which is not "
                                   f"within its declared range [{decimal_text(p.lo)}, "
                                   f"{decimal_text(p.hi)}]", "S-14")
                        continue
                    if key in assigned:
                        self.error(f"'over' of {where} lists '{key}', which a regime of "
                                   f"model.regimes assigns", "S-14")
                        continue
                    q.over[key] = (lo, hi)
                    p.open = True
            # regimes
            if "regimes" in d.keys and d.regimes is not None:
                names = self.name_list(d.regimes, f"'regimes' of {where}", "S-14")
                if names is not None:
                    self.check_regime_names(names, f"'regimes' of {where}", self.regimes,
                                            "a regime", "S-14")
                    q.regimes = names
            if len(q.regimes) < 2 and self.regimes:
                self.error(f"The {where} compares {len(q.regimes)} regime(s); at least "
                           f"two are needed", "S-14")
            # objective
            obj = d.objective
            keys = [k for k in ("maximize", "minimize") if isinstance(obj, dict) and k in obj]
            if not isinstance(obj, dict) or len(keys) != 1 or len(obj) != 1:
                self.error(f"'objective' of {where} must have exactly one of 'maximize' "
                           f"and 'minimize', with a query", "S-14")
            else:
                q.direction = keys[0]
                text = obj[keys[0]]
                q.objective_text = text if isinstance(text, str) else _as_text(text)
                prop = self.property_expr(text, f"'objective.{keys[0]}' of {where}")
                if prop is not None and not prop.is_query:
                    self.error(f"'objective.{keys[0]}' of {where} is '{text}', a "
                               f"threshold property; a query (=?) is needed", "S-14")
                else:
                    q.objective = prop
            # constraint
            if "constraint" in d.keys and d.constraint is not None:
                text = d.constraint
                q.constraint_text = text if isinstance(text, str) else _as_text(text)
                prop = self.property_expr(text, f"'constraint' of {where}")
                if prop is not None and prop.is_query:
                    self.error(f"'constraint' of {where} is '{text}', a query; a "
                               f"threshold property is needed", "S-14")
                else:
                    q.constraint = prop
            # resolution
            if "resolution" in d.keys and d.resolution is not None:
                r = yaml_number(d.resolution)
                if r is None or not 0 < r < 1:
                    self.error(f"'resolution' of {where} is '{_as_text(d.resolution)}'; it "
                               f"must be a number r with 0 < r < 1", "S-14")
                else:
                    q.resolution = r
            self.model.regime_maps.append(q)

    # -- S-7, S-9: enumeration of the state space --
    def check_states(self) -> None:
        if not self.vars_ok or not self.model.variables:
            return
        size = 1
        for v in self.model.variables:
            size *= len(v.domain)
        if size > STATE_SPACE_LIMIT:
            self.error(f"The model has {size} states; this processor decides S-7 and S-9 "
                       f"by enumerating the state space and refuses more than "
                       f"{STATE_SPACE_LIMIT} states (an implementation limit)", "S-7, S-9")
            return
        index = self.model.var_index()
        cmds = [c for c in self.enumerable
                if any(c.exists_in(d) for d in self.design_names)]
        if not cmds:
            return
        guards = [compile_state_expr(c.guard, index, {}) for c in cmds]
        sets = [
            [(self.vars[name], compile_state_expr(tree, index, {}), j)
             for j, u in enumerate(c.updates, start=1) for name, tree, _ in u.sets]
            for c in cmds
        ]
        domains = {v.id: set(v.domain) for v in self.model.variables}
        reported7: set = set()
        reported9: set = set()
        for state in itertools.product(*(v.domain for v in self.model.variables)):
            enabled = [k for k, g in enumerate(guards) if g(state)]
            for k in enabled:
                for var, fn, j in sets[k]:
                    if (k, var.id, j) in reported7:
                        continue
                    value = fn(state)
                    if value not in domains[var.id]:
                        reported7.add((k, var.id, j))
                        self.error(
                            f"Update {j} of command {cmds[k].index} of the model gives "
                            f"'{var.id}' the value {_as_text(value)}, outside its domain, "
                            f"in the state {self.show_state(state)}, where its guard is "
                            f"true", "S-7")
            self.check_enabled(state, enabled, cmds, reported9)

    def check_enabled(self, state: tuple, enabled: list, cmds: list, reported9: set) -> None:
        """S-9 in one state: `enabled` are the positions in `cmds` of the
        commands whose guard is true."""
        if len(enabled) > 1:
            for a, b in itertools.combinations(enabled, 2):
                if (a, b) in reported9:
                    continue
                both = [d for d in self.design_names
                        if cmds[a].exists_in(d) and cmds[b].exists_in(d)]
                if both:
                    reported9.add((a, b))
                    self.error(
                        f"Commands {cmds[a].index} and {cmds[b].index} of the model "
                        f"are both enabled in the state {self.show_state(state)} under "
                        f"the design {both[0]}; at most one command may be enabled in "
                        f"a state", "S-9")

    def show_state(self, state: tuple) -> str:
        return "(" + ", ".join(f"{v.id}={_as_text(x)}"
                               for v, x in zip(self.model.variables, state)) + ")"

    # -- S-11 and the well-definedness part of S-14 --
    def check_well_defined(self) -> None:
        model = self.model
        missing: dict[str, list] = {}
        fixed_ok: dict[str, dict] = {}
        for d in model.designs:
            theta = model.fixed_values(d)
            lacking = [p for p, v in theta.items() if v is None]
            for p in lacking:
                missing.setdefault(p, []).append(d.name)
            if not lacking:
                fixed_ok[d.name] = theta
        for p, designs in missing.items():
            self.error(f"The parameter '{p}' has no fixed value under the design(s) "
                       f"{', '.join(designs)}: it has no declared value and the regime "
                       f"assigns none", "S-11")
        if self.out.errors:
            # Probabilities and rewards can be evaluated only in a model
            # whose expressions are all well formed.
            return
        for d in model.designs:
            if d.name in fixed_ok:
                why = ill_defined(model, d.name, fixed_ok[d.name])
                if why:
                    self.error(f"The design {d.name} is not well-defined at its fixed "
                               f"values: {why}", "S-11")
        for q in model.regime_maps:
            names = list(q.over)
            for regime in q.regimes:
                if regime not in fixed_ok:
                    continue
                for corner in itertools.product(*(q.over[n] for n in names)):
                    theta = dict(fixed_ok[regime])
                    theta.update(zip(names, corner))
                    why = ill_defined(model, regime, theta)
                    if why:
                        at = ", ".join(f"{n}={decimal_text(x)}" for n, x in zip(names, corner))
                        self.error(f"In regime map '{q.id}', the design {regime} is not "
                                   f"well-defined at the corner point {at} of the region: "
                                   f"{why}", "S-14")
                        break


def _is_lit(t: Any) -> bool:
    return isinstance(t, tuple) and t[0] == "lit"


def _is_enum(t: Any) -> bool:
    return isinstance(t, tuple) and t[0] == "enum"


def _root_var(tree: tuple) -> str | None:
    """The variable of an enumeration-typed expression (a variable in parentheses)."""
    while tree[0] == "par":
        tree = tree[1]
    return tree[1] if tree[0] == "var" else None


def _bind_literal(tree: tuple, var: str | None) -> tuple:
    """Record the variable an enumeration literal is compared with or assigned to."""
    if tree[0] == "par":
        return ("par", _bind_literal(tree[1], var))
    return ("lit", tree[1], var)


def analyse(sos: SoSDefinition) -> Analysis:
    """Apply the rules S-1 to S-15 of H.10 to the `model:` block of a file.

    The file must declare ``stochastic: 0.1`` and have a `model:` block;
    the caller decides that (H.15).
    """
    if sos.model is None:
        return Analysis()
    from .games import games_declared, analyse_game_file
    if games_declared(sos):
        return analyse_game_file(sos)         # Appendix I, I.10
    out = _Analyser(sos).run()
    from .games import undeclared_use
    used = undeclared_use(sos.model)
    if used:
        # Informational: S-1, S-9 and S-15 already report what Appendix H
        # makes of these keys.
        out.infos.append(
            f"The model uses {', '.join(used)}, which belong to the strategic model "
            f"extension, but the file does not declare 'games: 0.1' under extensions:; "
            f"the block is read by Appendix H (Appendix I, I.15)")
    return out


def checked_model(sos: SoSDefinition) -> Model | None:
    """The interpreted model of a file, or None (no block, the extension is
    not declared, or the block has an error)."""
    if sos.model is None or not stochastic_declared(sos):
        return None
    return analyse(sos).model


# ---------------------------------------------------------------------------
# IR (H.13)
# ---------------------------------------------------------------------------

def model_to_ir(model: Model) -> dict:
    """The `stochastic_model` field of the IR (H.13)."""
    designs = [d.name for d in model.designs]
    if model.kind == "smg":
        from .games import game_to_ir
        return game_to_ir(model)              # Appendix I, I.13
    return {
        "kind": model.kind,
        "parameters": [
            {
                "id": p.id,
                "source": p.source,
                "range": [_json_number(p.lo), _json_number(p.hi)],
                "value": _json_number(p.value),
                "open": p.open,
            }
            for p in model.params
        ],
        "designs": [
            {"name": d.name,
             "parameters": {k: _json_number(v) for k, v in d.assign.items()}}
            for d in model.designs
        ],
        "properties": [
            {"id": p.id, "regimes": list(p.regimes) or designs, "expr": p.text}
            for p in model.properties
        ],
        "variables": [
            {"id": v.id, "kind": v.kind, "lo": v.lo, "hi": v.hi,
             "literals": list(v.literals), "init": v.init}
            for v in model.variables
        ],
        "labels": {name: text for name, (_, text) in model.labels.items()},
        "commands": [
            {
                "index": c.index,
                "action": c.action,
                "when": list(c.when),
                "guard": c.guard_text,
                "updates": [
                    {"prob": u.prob_text, "set": {name: text for name, _, text in u.sets}}
                    for u in c.updates
                ],
            }
            for c in model.commands
        ],
        "rewards": [
            {"id": r.id,
             "items": [{"action": i.action, "guard": i.guard_text, "value": i.value_text}
                       for i in r.items]}
            for r in model.rewards
        ],
        "regime_maps": [
            {
                "id": q.id,
                "over": {k: [_json_number(lo), _json_number(hi)]
                         for k, (lo, hi) in q.over.items()},
                "regimes": list(q.regimes),
                "direction": q.direction,
                "objective": q.objective_text,
                "constraint": q.constraint_text,
                "resolution": _json_number(q.resolution),
            }
            for q in model.regime_maps
        ],
    }


# ---------------------------------------------------------------------------
# Export to the PRISM modelling language (H.11)
# ---------------------------------------------------------------------------

class ExportError(ValueError):
    """The model cannot be exported (a name collides after renaming)."""


_TR_OPS = {"AND": "&", "OR": "|", "==": "="}


def _prism_name(name: str) -> str:
    """H.11: a name equal to a PRISM keyword or built-in function name gets
    one trailing underscore."""
    return name + "_" if name in PRISM_KEYWORDS or name in PRISM_FUNCTIONS else name


def tr(tree: tuple) -> str:
    """The translation `tr(e)` of a resolved state expression (H.11)."""
    kind = tree[0]
    if kind == "int":
        return tree[2]
    if kind == "bool":
        return "true" if tree[1] else "false"
    if kind == "var":
        return _prism_name(tree[1])
    if kind == "label":
        return f'"{_prism_name(tree[1])}"'
    if kind == "lit":
        return _prism_name(f"{tree[2]}_{tree[1]}")
    if kind == "par":
        return f"({tr(tree[1])})"
    if kind == "neg":
        # A space keeps two minus signs apart (`- -x`, never `--x`).
        inner = tr(tree[1])
        return f"- {inner}" if inner.startswith("-") else f"-{inner}"
    if kind == "not":
        return f"!{tr(tree[1])}"
    return f"{tr(tree[2])} {_TR_OPS.get(tree[1], tree[1])} {tr(tree[3])}"


def _tr_param(tree: tuple) -> str:
    kind = tree[0]
    if kind == "num":
        return tree[2]
    if kind == "id":
        return _prism_name(tree[1])
    if kind == "par":
        return f"({_tr_param(tree[1])})"
    if kind == "neg":
        inner = _tr_param(tree[1])
        return f"- {inner}" if inner.startswith("-") else f"-{inner}"
    return f"{_tr_param(tree[2])} {tree[1]} {_tr_param(tree[3])}"


def _check_export_names(model: Model) -> None:
    """H.11: report a name that collides with another after renaming."""
    names: dict[str, str] = {}

    def add(written: str, what: str) -> None:
        other = names.get(written)
        if other is not None:
            raise ExportError(
                f"cannot export: {what} and {other} are both written '{written}' in "
                f"the PRISM model (H.11)")
        names[written] = what

    for p in model.params:
        add(_prism_name(p.id), f"the parameter '{p.id}'")
    for v in model.variables:
        add(_prism_name(v.id), f"the variable '{v.id}'")
    for v in model.variables:
        for lit in v.literals:
            add(_prism_name(f"{v.id}_{lit}"), f"the literal '{lit}' of variable '{v.id}'")
    # PRISM always defines the labels "init" and "deadlock" (PRISM manual,
    # property specification); a model must not define them again.
    for name in model.labels:
        if name in PRISM_BUILTIN_LABELS:
            raise ExportError(
                f"cannot export: the label '{name}' has the name of a label that "
                f"PRISM always defines (\"init\", \"deadlock\"); rename it")
    # A quoted label or reward name must still be an identifier that is no
    # keyword: PRISM 4.11 and Storm reject `label "max"` and `rewards "max"`,
    # and Storm also the built-in function names. They are renamed like the
    # other names; labels and reward structures each have their own
    # namespace in PRISM.
    for kind, ids in (("label", list(model.labels)),
                      ("reward structure", [r.id for r in model.rewards])):
        written_as: dict[str, str] = {}
        for name in ids:
            written = _prism_name(name)
            if written_as.setdefault(written, name) != name:
                raise ExportError(
                    f"cannot export: the {kind}s '{written_as[written]}' and '{name}' "
                    f"are both written '{written}' in the PRISM model (H.11)")
    actions: dict[str, str] = {}
    for c in model.commands:
        if c.action:
            written = _prism_name(c.action)
            if actions.setdefault(written, c.action) != c.action:
                raise ExportError(
                    f"cannot export: the action labels '{actions[written]}' and "
                    f"'{c.action}' are both written '{written}' in the PRISM model (H.11)")


def _operand(tree: tuple) -> str:
    """An operand of F or U: in parentheses unless it is a single identifier
    or quoted label, or is already one parenthesised expression."""
    if tree[0] in ("var", "label", "par"):
        return tr(tree)
    return f"({tr(tree)})"


def property_text(prop: PropertyExpr) -> str:
    """A property as written in the properties file (H.11)."""
    head = "P" if prop.quantity == "P" else f'R{{"{_prism_name(prop.reward)}"}}'
    head += "=?" if prop.is_query else f"{prop.op}{prop.threshold_text}"
    bound = "" if prop.bound is None else f"<={prop.bound}"
    if prop.path == "F":
        path = f"F{bound} {_operand(prop.right)}"
    else:
        path = f"{_operand(prop.left)} U{bound} {_operand(prop.right)}"
    return f"{head} [ {path} ]"


def prism_model_text(model: Model, design: Design) -> str:
    """The model file of a design (H.11)."""
    _check_export_names(model)
    fixed = model.fixed_values(design)
    out = ["dtmc", ""]
    if model.kind == "smg":
        from .games import prism_games_header
        out = prism_games_header(model, design)   # Appendix I, I.11
    for p in model.params:
        if p.open:
            out.append(f"const double {_prism_name(p.id)};")
        else:
            out.append(f"const double {_prism_name(p.id)} = {decimal_text(fixed[p.id])};")
    for v in model.variables:
        for i, lit in enumerate(v.literals):
            out.append(f"const int {_prism_name(f'{v.id}_{lit}')} = {i};")
    out += ["", "module main"]
    for v in model.variables:
        name = _prism_name(v.id)
        if v.kind == "bool":
            out.append(f"  {name} : bool init {'true' if v.init else 'false'};")
        elif v.kind == "int":
            out.append(f"  {name} : [{v.lo}..{v.hi}] init {v.init};")
        else:
            out.append(f"  {name} : [0..{len(v.literals) - 1}] init "
                       f"{v.literals.index(v.init)};")
    order = model.var_index()
    guards = []
    for c in model.commands:
        if not c.exists_in(design.name):
            continue
        guard = tr(c.guard)
        guards.append(guard)
        others = [f"({_tr_param(u.prob)})" for u in c.updates if u.prob is not None]
        parts = []
        for u in c.updates:
            if u.prob is not None:
                prob = f"({_tr_param(u.prob)})"
            elif others:
                prob = f"(1 - ({' + '.join(others)}))"
            else:
                prob = "(1)"
            sets = sorted(u.sets, key=lambda s: order[s[0]])
            update = " & ".join(f"({_prism_name(n)}'={tr(t)})" for n, t, _ in sets) or "true"
            parts.append(f"{prob} : {update}")
        action = _prism_name(c.action) if c.action else ""
        out.append(f"  [{action}] {guard} -> {' + '.join(parts)};")
    if guards:
        out.append(f"  [] !({' | '.join(f'({g})' for g in guards)}) -> true;")
    else:
        out.append("  [] true -> true;")
    out.append("endmodule")
    if model.labels:
        out.append("")
        for name, (tree, _) in model.labels.items():
            out.append(f'label "{_prism_name(name)}" = ({tr(tree)});')
    actions = {c.action for c in model.commands if c.exists_in(design.name)}
    for r in model.rewards:
        out += ["", f'rewards "{_prism_name(r.id)}"']
        written = 0
        for item in r.items:
            if item.action and item.action not in actions:
                # No command of this design has the action, so the item is
                # never earned here; PRISM and Storm reject an item that
                # names an action the model does not have.
                continue
            action = f"[{_prism_name(item.action)}] " if item.action else ""
            out.append(f"  {action}{tr(item.guard)} : ({_tr_param(item.value)});")
            written += 1
        if not written:
            out.append("  true : (0);")    # Storm rejects an empty structure
        out.append("endrewards")
    return "\n".join(out) + "\n"


def prism_properties_text(model: Model, design: Design) -> str:
    """The properties file of a design (H.11)."""
    out = []
    for p in model.params:
        if p.open and p.value is not None:
            out.append(f"// const {_prism_name(p.id)} = {decimal_text(p.value)}")
    for p in model.properties:
        if design.name in p.regimes:
            out += [f"// {p.id}", property_text(p.expr)]
    for q in model.regime_maps:
        if design.name in q.regimes:
            out += [f"// {q.id} objective", property_text(q.objective)]
            if q.constraint is not None:
                out += [f"// {q.id} constraint", property_text(q.constraint)]
    return "\n".join(out) + "\n" if out else ""


def _file_part(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "model"


def export_prism(model: Model, regime: str | None = None) -> dict[str, str]:
    """The exported files, as {file name: text}: per design a model file
    ``<sos>_<design>.prism`` and a properties file ``<sos>_<design>.props``.

    The appendix does not fix the file names; they are this processor's.
    """
    designs = model.designs
    if regime is not None:
        designs = [d for d in designs if d.name == regime]
        if not designs:
            raise ExportError(
                f"'{regime}' is not a design of the file "
                f"(designs: {', '.join(d.name for d in model.designs)})")
    files: dict[str, str] = {}
    for d in designs:
        stem = f"{_file_part(model.sos_name)}_{_file_part(d.name)}"
        files[f"{stem}.prism"] = prism_model_text(model, d)
        if model.kind != "smg":               # I.11 defines no properties file
            files[f"{stem}.props"] = prism_properties_text(model, d)
    return files


def iter_states(model: Model) -> Iterator[tuple]:
    """Every state of S, in the order of the variable declarations."""
    return itertools.product(*(v.domain for v in model.variables))
