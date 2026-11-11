"""Grounding extension (Appendix J, grounding 0.1): the norms side.

The extension adds to a contract the key ``grounding:`` (``items``,
``tolerance``), to a norm the keys ``shares:`` and ``on_fulfilment:``
(``reward``, ``magnitude``) and to ``on_violation:`` the key
``magnitude:``. From them and from the norms of the contract this module
computes the two derived indices and the incentive coverage of J.4:

- :func:`alpha_index`: information sharing (given pairs / pairs);
- :func:`beta_index`: concentration of powers (largest number of powers
  of one single actor / powers);
- :func:`coverage`: the obligations and prohibitions that carry an
  incentive / obligations and prohibitions.

and applies the rules J-1 to J-9 of J.8 (:func:`check`), each in a
function of its own. :func:`report` is what `cadl norms-indices` prints,
:func:`ir_contract` the IR addition of J.10, :func:`diff_contract` the
comparison of two versions (J-13), and :func:`norm_payoff_link` what a
payoff term ``sanction_of:`` / ``reward_of:`` of a strategic model
(Appendix I; J.7, J-10) needs to know about the norm it names.

The keys are annotations: they change no verdict of Appendix F. They are
kept apart from the AST of the norms (``SoSDefinition.grounding`` holds
the values as the YAML loader read them), so the evaluation of norms,
the agent boundary and the generated monitor do not see them.

All counts are integers and all ratios :class:`fractions.Fraction`; a
declared value is the decimal that is written.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Optional

from .ast_nodes import ContractDef, NormDef, RangeExpr, SoSDefinition

GROUNDING_EXTENSION = ("grounding", "0.1")
DEFAULT_TOLERANCE = Fraction(1, 4)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NORM_REF_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
_YAML_NAME_NOTE = (
    "a YAML 1.1 loader reads an unquoted yes, no, on, off, true, false "
    "or null as a value, not as a name; quote it"
)
_BLOCK_KEYS = ("items", "tolerance")
_FULFILMENT_KEYS = ("reward", "magnitude")


# --- The keys as written -----------------------------------------------------

class ContractRaw:
    """The keys of the extension in one contract, as the YAML loader read
    them. ``norms`` is aligned with ``ContractDef.norms``; each entry maps
    the keys the norm has, among ``shares``, ``on_fulfilment`` and
    ``magnitude`` (of ``on_violation:``), to their values."""

    __slots__ = ("has_block", "block", "norms")

    def __init__(self, has_block: bool, block: Any, norms: list) -> None:
        self.has_block = has_block
        self.block = block
        self.norms = norms

    def used(self) -> list:
        keys = []
        if self.has_block:
            keys.append("grounding:")
        for name, label in (("shares", "shares:"), ("on_fulfilment", "on_fulfilment:"),
                            ("magnitude", "on_violation.magnitude")):
            if any(name in n for n in self.norms):
                keys.append(label)
        return keys


class GroundingRaw:
    """``SoSDefinition.grounding``: one :class:`ContractRaw` per contract,
    aligned with ``SoSDefinition.contracts``. Not a dataclass on purpose:
    the values are never written into generated code, and the input check
    of the code generators does not walk them."""

    __slots__ = ("contracts",)

    def __init__(self, contracts: list) -> None:
        self.contracts = contracts

    def __repr__(self) -> str:
        return f"GroundingRaw({[c.used() for c in self.contracts]})"


def read_raw(contracts_data: Any) -> Optional[GroundingRaw]:
    """Parser hook: the keys of the extension in ``contracts:``, or None
    when no contract uses one. A key whose value is null counts as absent
    (J.3). Read whether or not ``grounding: 0.1`` is declared."""
    if not isinstance(contracts_data, list):
        return None
    out = []
    for c in contracts_data:
        if not isinstance(c, dict):
            continue
        norms = []
        norms_data = c.get("norms")
        for n in norms_data if isinstance(norms_data, list) else []:
            if not isinstance(n, dict):
                continue
            raw = {}
            for key in ("shares", "on_fulfilment"):
                if n.get(key) is not None:
                    raw[key] = n[key]
            ov = n.get("on_violation")
            if isinstance(ov, dict) and ov.get("magnitude") is not None:
                raw["magnitude"] = ov["magnitude"]
            norms.append(raw)
        block = c.get("grounding")
        out.append(ContractRaw(block is not None, block, norms))
    if not any(c.used() for c in out):
        return None
    return GroundingRaw(out)


def grounding_declared(sos: SoSDefinition) -> bool:
    """True when the file declares ``grounding: 0.1`` under ``extensions:``."""
    return GROUNDING_EXTENSION in sos.extensions


def grounding_active(sos: SoSDefinition) -> bool:
    """True when the keys of the extension are interpreted: ``grounding:
    0.1`` is declared together with ``norms: 0.1``, which it requires (J-1)."""
    from .norms import norms_declared
    return grounding_declared(sos) and norms_declared(sos)


def keys_used(sos: SoSDefinition) -> list:
    """The keys of the extension that the file uses, in a fixed order."""
    raw = getattr(sos, "grounding", None)
    if raw is None:
        return []
    used = [k for c in raw.contracts for k in c.used()]
    order = ("grounding:", "shares:", "on_fulfilment:", "on_violation.magnitude")
    return [k for k in order if k in used]


def _contract_raw(sos: SoSDefinition, contract: ContractDef) -> ContractRaw:
    raw = getattr(sos, "grounding", None)
    if raw is not None:
        for i, c in enumerate(sos.contracts):
            if c is contract and i < len(raw.contracts):
                return raw.contracts[i]
        for i, c in enumerate(sos.contracts):
            if c.id == contract.id and i < len(raw.contracts):
                return raw.contracts[i]
    return ContractRaw(False, None, [{} for _ in contract.norms])


# --- Values --------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_magnitude(value: Any) -> bool:
    """J-4: a finite number that is not negative."""
    return _is_number(value) and math.isfinite(value) and value >= 0


def exact(value: Any) -> Fraction:
    """A number of the file as the decimal that is written (J-5, H.2)."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(repr(float(value)))


def number_text(value: Any) -> str:
    """A number as the shortest decimal that round-trips (J.10)."""
    if isinstance(value, Fraction):
        value = value.numerator if value.denominator == 1 else float(value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        value = int(value)
    return repr(value)


def _text(value: Any) -> Optional[str]:
    """A sanction or reward text; an empty text and null count as absent (J.3)."""
    if value is None:
        return None
    if isinstance(value, bool):
        value = str(value).lower()
    value = str(value)
    return value if value != "" else None


def _show(value: Any) -> str:
    from .parser import _value_as_text
    return _value_as_text(value)


# --- Actor references (J.2) ---------------------------------------------------

def _ref(text: Any) -> dict:
    """An actor reference as data (see `cadl.agents.parse_ref`); white
    space is ignored."""
    from .agents import parse_ref
    return parse_ref(re.sub(r"\s+", "", str(text)))


def _declared_actors(sos: SoSDefinition) -> dict:
    """Actor name -> the declared index (None, or a RangeExpr / other)."""
    return {a.id.name: a.id.index for a in sos.actors}


def is_single_actor(ref: dict, declared: dict) -> bool:
    """J.2: an integer index, or no index when the actor is declared
    without a range."""
    if ref["kind"] == "index":
        return True
    return (ref["kind"] == "plain" and declared.get(ref["name"]) is None
            and _IDENTIFIER_RE.fullmatch(ref["name"]) is not None)


def party_names(contract: ContractDef) -> list:
    """J.2: the actor names of ``parties:``, each once, in file order."""
    names = []
    for p in contract.parties:
        if p.name not in names:
            names.append(p.name)
    return names


def target_class(target: dict, contract: ContractDef, declared: dict) -> str:
    """How a target of a sharing entry or the receiver of a sharing norm
    stands to the parties of the contract (J.4, by the matching of F.3):

    - ``whole``: it denotes the whole party with its actor name: ``A[*]``,
      ``A[i]`` with an identifier index, the full declared range, a range
      with a symbolic bound, ``A`` for an actor that is not a set, or an
      integer index equal to the only entry of ``parties:`` with that name;
    - ``bare_set``: the name of a declared set without an index, which
      matches no member (F.3);
    - ``member``: member-specific (another integer index or range);
    - ``non_party``: its actor name is not a party name.
    """
    from .unparse import actor_ref_to_source
    name, kind = target["name"], target["kind"]
    if name not in party_names(contract):
        return "non_party"
    if kind in ("all", "var"):
        return "whole"
    decl = declared.get(name)
    if kind == "plain":
        return "whole" if decl is None else "bare_set"
    if kind == "range":
        full = (isinstance(decl, RangeExpr) and isinstance(decl.end, int)
                and (decl.start, decl.end) == (target["lo"], target["hi"]))
        return "whole" if full else "member"
    # an integer index
    entries = [_ref(actor_ref_to_source(p)) for p in contract.parties if p.name == name]
    if len(entries) == 1 and entries[0]["kind"] == "index" \
            and entries[0]["index"] == target["index"]:
        return "whole"
    return "member"


def covers(actor_ref: Any, bearer: Any) -> bool:
    """J-10: an actor reference covers a bearer when the two have the same
    actor name and do not have two different integer indices."""
    a, b = _ref(actor_ref), _ref(bearer)
    if a["name"] != b["name"]:
        return False
    return not (a["kind"] == "index" and b["kind"] == "index" and a["index"] != b["index"])


# --- The interpreted keys -------------------------------------------------------

@dataclass
class NormAnnotations:
    """The keys of the extension of one norm, valid values only."""
    shares: list = field(default_factory=list)
    sanction_magnitude: Any = None     # int | float | None
    reward: Optional[str] = None
    reward_magnitude: Any = None
    has_fulfilment: bool = False


@dataclass
class ContractAnnotations:
    """The keys of the extension of one contract, valid values only."""
    items: dict = field(default_factory=dict)       # item -> origin, as written
    tolerance: Fraction = DEFAULT_TOLERANCE
    norms: list = field(default_factory=list)       # aligned with contract.norms

    def of(self, contract: ContractDef, norm: NormDef) -> NormAnnotations:
        for n, a in zip(contract.norms, self.norms):
            if n is norm:
                return a
        return NormAnnotations()


def annotations(sos: SoSDefinition, contract: ContractDef) -> ContractAnnotations:
    """The keys of the extension of a contract as the indices read them.
    Values that break J-2 to J-4 are left out (`check` reports them). In a
    file in which the extension is not active nothing is interpreted."""
    out = ContractAnnotations(norms=[NormAnnotations() for _ in contract.norms])
    if not grounding_active(sos):
        return out
    raw = _contract_raw(sos, contract)
    names = party_names(contract)
    block = raw.block if isinstance(raw.block, dict) else {}
    items = block.get("items")
    if isinstance(items, dict):
        for key, value in items.items():
            if _item_problem(key) is None and _origin_problem(value, names) is None:
                out.items[key] = re.sub(r"\s+", "", str(value))
    tol = block.get("tolerance")
    if _is_number(tol) and math.isfinite(tol) and 0 <= tol <= 1:
        out.tolerance = exact(tol)
    for norm, nraw, ann in zip(contract.norms, raw.norms, out.norms):
        shares = nraw.get("shares")
        if isinstance(shares, list):
            for x in shares:
                if isinstance(x, str) and x in out.items and x not in ann.shares:
                    ann.shares.append(x)
        if _valid_magnitude(nraw.get("magnitude")):
            ann.sanction_magnitude = nraw["magnitude"]
        ful = nraw.get("on_fulfilment")
        if isinstance(ful, dict) and norm.kind in ("obligation", "prohibition"):
            ann.has_fulfilment = True
            ann.reward = _text(ful.get("reward"))
            if _valid_magnitude(ful.get("magnitude")):
                ann.reward_magnitude = ful["magnitude"]
    return out


def _item_problem(key: Any) -> Optional[str]:
    from .type_checker import RESERVED_WORDS
    if isinstance(key, bool) or key is None:
        return f"is read as {_show(key)}, not as a name ({_YAML_NAME_NOTE})"
    if not isinstance(key, str) or not _IDENTIFIER_RE.fullmatch(key):
        return "is not an identifier"
    if key in RESERVED_WORDS:
        return "is a reserved word"
    return None


def _origin_problem(value: Any, names: list) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return f"its origin {_show(value)!r} is not an actor reference"
    ref = _ref(value)
    if not _IDENTIFIER_RE.fullmatch(ref["name"]):
        return f"its origin '{value}' is not an actor reference"
    if ref["name"] not in names:
        return f"its origin '{value}' is not a party of the contract"
    return None


# --- J.4: information sharing ---------------------------------------------------

@dataclass
class Source:
    """A sharing entry or a sharing norm, with the items it conveys."""
    way: str                    # "sharing" | "obligation" | "permission"
    label: str                  # the entry as text, or the id of the norm
    target: dict                # parsed receiver
    items: list


@dataclass
class AlphaIndex:
    parties: list = field(default_factory=list)
    origins: dict = field(default_factory=dict)       # item -> party name
    pairs: list = field(default_factory=list)         # (item, party)
    given: dict = field(default_factory=dict)         # pair -> [(way, label)]
    member_specific: list = field(default_factory=list)   # labels of entries and norms
    bare_set: list = field(default_factory=list)          # (label, target text)
    non_party: list = field(default_factory=list)         # (label, target text)
    to_origin: list = field(default_factory=list)         # (label, item) of sharing norms
    undeclared_items: list = field(default_factory=list)  # (entry label, item)
    sharing_entries: int = 0

    def by(self, way: str) -> list:
        return [p for p in self.pairs if any(w == way for w, _ in self.given.get(p, []))]

    @property
    def given_pairs(self) -> list:
        return [p for p in self.pairs if p in self.given]

    @property
    def not_given(self) -> list:
        return [p for p in self.pairs if p not in self.given]

    @property
    def value(self) -> Optional[Fraction]:
        return Fraction(len(self.given_pairs), len(self.pairs)) if self.pairs else None

    @property
    def undefined_reason(self) -> Optional[str]:
        if self.pairs:
            return None
        if not self.origins:
            return "the contract declares no information item (grounding.items)"
        return "the contract has a single party name, so no item can be shared with another party"


def sharing_sources(sos: SoSDefinition, contract: ContractDef,
                    ann: ContractAnnotations) -> list:
    """The sharing entries of ``information.sharing`` and the sharing
    norms of a contract (J.2), in file order."""
    from .agents import norm_message
    from .unparse import actor_ref_to_source
    out = []
    if contract.information is not None:
        for s in contract.information.sharing:
            src, tgt = actor_ref_to_source(s.source), actor_ref_to_source(s.target)
            out.append(Source("sharing", f"{src} -> {tgt} : {s.data}", _ref(tgt), [s.data]))
    for norm, a in zip(contract.norms, ann.norms):
        if norm.kind not in ("obligation", "permission") or not a.shares:
            continue
        msg = norm_message(contract, norm)
        if msg is not None:
            out.append(Source(norm.kind, norm.id, _ref(msg.receiver["text"]), list(a.shares)))
    return out


def alpha_index(sos: SoSDefinition, contract: ContractDef,
                ann: Optional[ContractAnnotations] = None) -> AlphaIndex:
    """J.4: the pairs (item, party name other than the origin) and the
    pairs given by a sharing entry, an obligation or a permission whose
    target denotes the whole party."""
    ann = ann if ann is not None else annotations(sos, contract)
    declared = _declared_actors(sos)
    idx = AlphaIndex(parties=party_names(contract))
    idx.origins = {x: _ref(o)["name"] for x, o in ann.items.items()}
    idx.pairs = [(x, q) for x, o in idx.origins.items() for q in idx.parties if q != o]
    pairs = set(idx.pairs)
    idx.sharing_entries = (
        len(contract.information.sharing) + len(contract.information.invalid_sharing)
        if contract.information is not None else 0)
    for src in sharing_sources(sos, contract, ann):
        cls = target_class(src.target, contract, declared)
        name = src.target["name"]
        known = [x for x in src.items if x in idx.origins]
        if src.way == "sharing":
            idx.undeclared_items += [(src.label, x) for x in src.items if x not in idx.origins]
        if cls == "non_party":
            if known or src.way != "sharing":
                idx.non_party.append((src.label, src.target["text"]))
            continue
        if src.way != "sharing":
            idx.to_origin += [(src.label, x) for x in known if idx.origins[x] == name]
        would = [(x, name) for x in known if (x, name) in pairs]
        if not would:
            continue
        if cls == "whole":
            for p in would:
                idx.given.setdefault(p, []).append((src.way, src.label))
        elif cls == "member":
            idx.member_specific.append(src.label)
        else:
            idx.bare_set.append((src.label, src.target["text"]))
    return idx


# --- J.4: concentration of powers ---------------------------------------------

@dataclass
class BetaIndex:
    powers: list = field(default_factory=list)      # (id, bearer text, single, effects)
    bearers: dict = field(default_factory=dict)     # bearer text -> number of powers
    singles: dict = field(default_factory=dict)     # single-actor bearer -> k(a)
    holder: Optional[str] = None                    # decision holder, as written
    holder_count: Optional[int] = None              # k(h), None unless a single actor

    @property
    def largest(self) -> int:
        return max(self.singles.values(), default=0)

    @property
    def top(self) -> Optional[str]:
        """The single actor with the largest count, the first in file
        order when several share it; None when no single actor bears a power."""
        k = self.largest
        return next((a for a, n in self.singles.items() if n == k), None)

    @property
    def value(self) -> Optional[Fraction]:
        return Fraction(self.largest, len(self.powers)) if self.powers else None

    @property
    def holder_share(self) -> Optional[Fraction]:
        if self.holder_count is None or not self.powers:
            return None
        return Fraction(self.holder_count, len(self.powers))

    @property
    def undefined_reason(self) -> Optional[str]:
        return None if self.powers else "the contract has no power"


def power_effects(norm: NormDef) -> list:
    """The norms named in the ``effects:`` of a power, each once."""
    eff = norm.effects
    out = []
    if eff is not None:
        for name in list(eff.activate) + list(eff.suspend) + list(eff.resume) + list(eff.discharge):
            if name not in out:
                out.append(name)
    return out


def beta_index(sos: SoSDefinition, contract: ContractDef) -> BetaIndex:
    """J.4: the powers of a contract per bearer, and the largest number of
    powers borne by one single actor. A power whose bearer is not a single
    actor counts for no single actor."""
    from .unparse import actor_ref_to_source
    declared = _declared_actors(sos)
    idx = BetaIndex()
    for norm in contract.norms:
        if norm.kind != "power":
            continue
        ref = _ref(norm.bearer)
        single = is_single_actor(ref, declared)
        idx.powers.append((norm.id, ref["text"], single, len(power_effects(norm))))
        idx.bearers[ref["text"]] = idx.bearers.get(ref["text"], 0) + 1
        if single:
            idx.singles[ref["text"]] = idx.singles.get(ref["text"], 0) + 1
    holder = contract.authority.decision_holder if contract.authority else None
    if holder is not None:
        ref = _ref(actor_ref_to_source(holder))
        idx.holder = ref["text"]
        if is_single_actor(ref, declared):
            idx.holder_count = idx.singles.get(ref["text"], 0)
    return idx


# --- J.4: incentive coverage ---------------------------------------------------

@dataclass
class Coverage:
    norms: list = field(default_factory=list)             # ids of N
    incentive: list = field(default_factory=list)
    reparation_only: list = field(default_factory=list)
    transition: list = field(default_factory=list)

    @property
    def value(self) -> Optional[Fraction]:
        return Fraction(len(self.incentive), len(self.norms)) if self.norms else None

    @property
    def undefined_reason(self) -> Optional[str]:
        return None if self.norms else "the contract has no obligation and no prohibition"


def carries_incentive(norm: NormDef, ann: NormAnnotations) -> bool:
    """J.4: a sanction text that is not empty, a sanction magnitude greater
    than zero, a reward text that is not empty, or a reward magnitude
    greater than zero."""
    ov = norm.on_violation
    if ov is not None and _text(ov.sanction) is not None:
        return True
    if ann.sanction_magnitude is not None and ann.sanction_magnitude > 0:
        return True
    if ann.reward is not None:
        return True
    return ann.reward_magnitude is not None and ann.reward_magnitude > 0


def coverage(sos: SoSDefinition, contract: ContractDef,
             ann: Optional[ContractAnnotations] = None) -> Coverage:
    """J.4: the obligations and prohibitions that carry an incentive, and
    the two counts that are reported next to it."""
    ann = ann if ann is not None else annotations(sos, contract)
    cov = Coverage()
    for norm, a in zip(contract.norms, ann.norms):
        if norm.kind not in ("obligation", "prohibition"):
            continue
        cov.norms.append(norm.id)
        ov = norm.on_violation
        inc = carries_incentive(norm, a)
        if inc:
            cov.incentive.append(norm.id)
        elif ov is not None and ov.reparation:
            cov.reparation_only.append(norm.id)
        if ov is not None and ov.transition:
            cov.transition.append(norm.id)
    return cov


# --- The quantities of one contract (J-5) -------------------------------------

@dataclass
class ContractIndices:
    contract: ContractDef
    ann: ContractAnnotations
    alpha: AlphaIndex
    beta: BetaIndex
    coverage: Coverage

    @property
    def id(self) -> str:
        return self.contract.id

    @property
    def tolerance(self) -> Fraction:
        return self.ann.tolerance

    @property
    def declared_alpha(self) -> Optional[Fraction]:
        info = self.contract.information
        return exact(info.alpha) if info is not None and info.alpha is not None else None

    @property
    def declared_beta(self) -> Optional[Fraction]:
        auth = self.contract.authority
        return exact(auth.beta) if auth is not None and auth.beta is not None else None

    @property
    def declared_lambda(self) -> Optional[Fraction]:
        inc = self.contract.incentives
        return exact(inc.lambda_) if inc is not None and inc.lambda_ is not None else None


def contract_indices(sos: SoSDefinition, contract: ContractDef) -> ContractIndices:
    """α̂, β̂ and χ of one contract with all their counts (J-5)."""
    ann = annotations(sos, contract)
    return ContractIndices(
        contract=contract, ann=ann,
        alpha=alpha_index(sos, contract, ann),
        beta=beta_index(sos, contract),
        coverage=coverage(sos, contract, ann),
    )


def file_indices(sos: SoSDefinition) -> list:
    """The quantities of every contract of a file in which the extension
    is active; an empty list otherwise (J.12: no quantity is computed)."""
    if not grounding_active(sos):
        return []
    return [contract_indices(sos, c) for c in sos.contracts]


# --- J.8: the rules --------------------------------------------------------------
# Every rule returns diagnostics as (level, text) pairs; level is "error",
# "warning" or "info".

def _frac(value: Fraction) -> str:
    """An exact ratio with its decimal: ``5/8 = 0.625``."""
    return f"{value.numerator}/{value.denominator} = {_dec(value)}"


def _dec(value: Fraction) -> str:
    """A fraction as a decimal: exact when it is short, else six places."""
    if value.denominator == 1:
        return str(value.numerator)
    d = value.denominator
    while d % 2 == 0:
        d //= 2
    while d % 5 == 0:
        d //= 5
    if d == 1:
        text = f"{float(value):.12f}".rstrip("0")
        return text + "0" if text.endswith(".") else text
    return f"{float(value):.6f}"


def rule_j1(sos: SoSDefinition) -> list:
    """J-1: ``grounding: 0.1`` requires ``norms: 0.1``."""
    from .norms import norms_declared
    if grounding_declared(sos) and not norms_declared(sos):
        return [("error",
                 "The file declares 'grounding: 0.1' without 'norms: 0.1', which the "
                 "grounding extension requires; grounding:, shares:, on_fulfilment: and "
                 "on_violation.magnitude are not interpreted and no index is computed (J-1)")]
    return []


def rule_undeclared(sos: SoSDefinition) -> list:
    """J.12: the keys of the extension in a file that does not declare it."""
    if grounding_declared(sos):
        return []
    keys = keys_used(sos)
    if not keys:
        return []
    return [("warning",
             f"The file uses {', '.join(keys)}, which belong{'s' if len(keys) == 1 else ''} "
             f"to the grounding extension, but does not declare 'grounding: 0.1' under "
             f"extensions:; they are undeclared extension keys, and are neither checked "
             f"nor lowered to the IR, and no index is computed (Appendix J, J.12)")]


def rule_j2(sos: SoSDefinition, contract: ContractDef) -> list:
    """J-2: the items and the tolerance of ``grounding:``."""
    raw = _contract_raw(sos, contract)
    if not raw.has_block:
        return []
    ctx = f"contract '{contract.id}'"
    if not isinstance(raw.block, dict):
        return [("error", f"grounding: of {ctx} is not a mapping with the keys items: "
                          f"and tolerance: (J-2)")]
    out = []
    for key in raw.block:
        if key not in _BLOCK_KEYS:
            out.append(("warning", f"grounding: of {ctx} has the key '{_show(key)}', which "
                                   f"grounding 0.1 does not define; it is ignored (J.3)"))
    items = raw.block.get("items")
    names = party_names(contract)
    if items is not None and not isinstance(items, dict):
        out.append(("error", f"grounding.items of {ctx} is not a mapping from item names "
                             f"to their origins (J-2)"))
    elif items:
        for key, value in items.items():
            problem = _item_problem(key)
            if problem:
                out.append(("error", f"The item '{_show(key)}' of grounding.items of {ctx} "
                                     f"{problem} (J-2)"))
                continue
            problem = _origin_problem(value, names)
            if problem:
                out.append(("error", f"The item '{key}' of grounding.items of {ctx}: "
                                     f"{problem} (J-2)"))
    tol = raw.block.get("tolerance")
    if tol is not None and not (_is_number(tol) and math.isfinite(tol) and 0 <= tol <= 1):
        out.append(("error", f"grounding.tolerance of {ctx} must be a number in [0, 1], "
                             f"got {_show(tol)} (J-2)"))
    return out


def rule_j3(sos: SoSDefinition, contract: ContractDef, ci: ContractIndices) -> list:
    """J-3: ``shares:`` (errors), and the warnings about the targets of
    sharing entries and sharing norms."""
    from .agents import norm_message
    raw = _contract_raw(sos, contract)
    ctx = f"contract '{contract.id}'"
    items = ci.ann.items
    out = []
    for norm, nraw in zip(contract.norms, raw.norms):
        if "shares" not in nraw:
            continue
        where = f"shares: of norm '{norm.id}' of {ctx}"
        shares = nraw["shares"]
        if norm.kind not in ("obligation", "permission") \
                or norm_message(contract, norm) is None:
            out.append(("error", f"{where} is not allowed: shares: is only for an "
                                 f"obligation or a permission whose content is a message "
                                 f"event (J-3)"))
            continue
        if not isinstance(shares, list) or not shares:
            out.append(("error", f"{where} must be a list of at least one information "
                                 f"item (J-3)"))
            continue
        seen = []
        for x in shares:
            if not isinstance(x, str):
                note = f" ({_YAML_NAME_NOTE})" if isinstance(x, bool) or x is None else ""
                out.append(("error", f"{where} has the entry {_show(x)}, which is not "
                                     f"the name of an information item{note} (J-3)"))
            elif x in seen:
                out.append(("error", f"{where} lists the item '{x}' more than once (J-3)"))
            elif x not in items:
                out.append(("error", f"{where} lists '{x}', which grounding.items of the "
                                     f"contract does not declare (J-3)"))
            seen.append(x)
    a = ci.alpha
    for label, item in a.to_origin:
        out.append(("warning", f"Norm '{label}' of {ctx} shares the item '{item}' with "
                               f"{a.origins[item]}, the origin of the item; it gives no "
                               f"pair (J-3)"))
    for label, target in a.non_party:
        what = (f"The sharing entry \"{label}\"" if " -> " in label else f"Norm '{label}'")
        out.append(("warning", f"{what} of {ctx} has the target '{target}', which is not a "
                               f"party of the contract; it gives no pair (J-3)"))
    for label, target in a.bare_set:
        what = (f"The sharing entry \"{label}\"" if " -> " in label else f"Norm '{label}'")
        out.append(("warning", f"{what} of {ctx} has the target '{target}', the name of an "
                               f"actor set without an index, which matches no member "
                               f"(F.3); it gives no pair (J-3)"))
    if items:
        for label, item in a.undeclared_items:
            out.append(("warning", f"The sharing entry \"{label}\" of {ctx} names the item "
                                   f"'{item}', which grounding.items does not declare; it "
                                   f"gives no pair (J-3)"))
    return out


def rule_j4(sos: SoSDefinition, contract: ContractDef) -> list:
    """J-4: the magnitudes and ``on_fulfilment:``."""
    raw = _contract_raw(sos, contract)
    ctx = f"contract '{contract.id}'"
    out = []

    def magnitude(value: Any, where: str) -> bool:
        if _valid_magnitude(value):
            return True
        out.append(("error", f"{where} must be a finite number that is not negative, "
                             f"got {_show(value)} (J-4)"))
        return False

    for norm, nraw in zip(contract.norms, raw.norms):
        of = f"of norm '{norm.id}' of {ctx}"
        if "magnitude" in nraw:
            m = nraw["magnitude"]
            if magnitude(m, f"on_violation.magnitude {of}") and m == 0 \
                    and norm.on_violation is not None \
                    and _text(norm.on_violation.sanction) is not None:
                out.append(("warning", f"on_violation {of} has a sanction text and the "
                                       f"magnitude 0: the sanction has no size (J-4)"))
        if "on_fulfilment" not in nraw:
            continue
        ful = nraw["on_fulfilment"]
        if norm.kind not in ("obligation", "prohibition"):
            out.append(("error", f"on_fulfilment: {of} is not allowed: only an obligation "
                                 f"or a prohibition has it, and the norm is a "
                                 f"{norm.kind or 'norm without kind'} (J-4)"))
            continue
        if not isinstance(ful, dict):
            out.append(("error", f"on_fulfilment: {of} is not a mapping with the keys "
                                 f"reward: and magnitude: (J-4)"))
            continue
        for key in ful:
            if key not in _FULFILMENT_KEYS:
                out.append(("warning", f"on_fulfilment: {of} has the key '{_show(key)}', "
                                       f"which grounding 0.1 does not define; it is "
                                       f"ignored (J.3)"))
        reward, m = _text(ful.get("reward")), ful.get("magnitude")
        if reward is None and m is None:
            out.append(("error", f"on_fulfilment: {of} must have at least one of reward: "
                                 f"and magnitude: (an empty text counts as absent) (J-4)"))
        if m is not None and magnitude(m, f"on_fulfilment.magnitude {of}") and m == 0 \
                and reward is not None:
            out.append(("warning", f"on_fulfilment {of} has a reward text and the "
                                   f"magnitude 0: the reward has no size (J-4)"))
    return out


def _pair(p: tuple) -> str:
    return f"({p[0]}, {p[1]})"


def _giver(way: str, label: str) -> str:
    if way == "sharing":
        return f"the sharing entry \"{label}\""
    return f"the {way} '{label}'"


def rule_j6_alpha(ci: ContractIndices) -> list:
    """J-6 for α: a declared end point with a counterexample in the contract."""
    declared, a = ci.declared_alpha, ci.alpha
    ctx = f"contract '{ci.id}'"
    if declared is None:
        return []
    if declared == 0:
        if a.given:
            p = a.given_pairs[0]
            way, label = a.given[p][0]
            more = len(a.given_pairs) - 1
            return [("warning",
                     f"{ctx} declares alpha: 0 (no sharing), but the pair {_pair(p)} is given "
                     f"by {_giver(way, label)}"
                     + (f" (and {more} more given pair(s))" if more else "")
                     + f"; the index of information sharing is {_frac(a.value)} (J-6)")]
        if a.value is None and a.sharing_entries:
            first = ci.contract.information
            entry = None
            if first.sharing:
                from .unparse import actor_ref_to_source
                s = first.sharing[0]
                entry = (f"{actor_ref_to_source(s.source)} -> "
                         f"{actor_ref_to_source(s.target)} : {s.data}")
            elif first.invalid_sharing:
                entry = first.invalid_sharing[0]
            return [("warning",
                     f"{ctx} declares alpha: 0 (no sharing), but information.sharing is not "
                     f"empty (\"{entry}\"); the index of information sharing is undefined "
                     f"(J-6)")]
        return []
    if declared == 1 and a.value is not None and a.value < 1:
        missing = a.not_given
        shown = ", ".join(_pair(p) for p in missing[:6]) + (", ..." if len(missing) > 6 else "")
        return [("warning",
                 f"{ctx} declares alpha: 1 (complete sharing), but {len(missing)} of "
                 f"{len(a.pairs)} pair(s) are not given by a sharing entry or a sharing "
                 f"norm: {shown}; the index of information sharing is {_frac(a.value)} (J-6)")]
    return []


def rule_j6_beta(ci: ContractIndices) -> list:
    """J-6 for β. ``beta: 1``: the witnesses are the powers that are not
    borne by the single actor with the largest count (every power when no
    single actor bears one). ``beta: 0``: a warning only when the index
    exceeds 1 divided by the number of distinct single actors that bear a
    power."""
    declared, b = ci.declared_beta, ci.beta
    ctx = f"contract '{ci.id}'"
    if declared is None or b.value is None:
        return []
    if declared == 1 and b.value < 1:
        top = b.top
        others = [(pid, bearer) for pid, bearer, _, _ in b.powers if bearer != top]
        pid, bearer = others[0]
        more = len(others) - 1
        who = (f"which is not {top}, the single actor with the most powers ({b.largest})"
               if top is not None else "which is not a single actor")
        return [("warning",
                 f"{ctx} declares beta: 1 (a single actor decides all), but the power "
                 f"'{pid}' is borne by {bearer}, {who}"
                 + (f" (and {more} more such power(s): "
                    f"{', '.join(p for p, _ in others[1:])})" if more else "")
                 + f"; the index of concentration of powers is {_frac(b.value)} (J-6)")]
    if declared == 0 and b.singles and b.value > Fraction(1, len(b.singles)):
        return [("warning",
                 f"{ctx} declares beta: 0, but the single actor {b.top} bears {b.largest} of "
                 f"the {len(b.powers)} power(s), more than an even share among the "
                 f"{len(b.singles)} single actor(s) that bear a power; the index of "
                 f"concentration of powers is {_frac(b.value)} (J-6)")]
    return []


def _far(declared: Optional[Fraction], index: Optional[Fraction], tol: Fraction) -> bool:
    return declared is not None and index is not None and abs(declared - index) > tol


def rule_j7_alpha(ci: ContractIndices, j6: bool) -> list:
    """J-7 for α: the declared value is farther from the index than the
    tolerance, and J-6 gives no warning for α."""
    if j6 or not _far(ci.declared_alpha, ci.alpha.value, ci.tolerance):
        return []
    a = ci.alpha
    return [("warning",
             f"contract '{ci.id}' declares alpha: {_dec(ci.declared_alpha)}, but the index of "
             f"information sharing is {_frac(a.value)} ({len(a.given_pairs)} of "
             f"{len(a.pairs)} pairs of an item and a party are given); they differ by "
             f"{_dec(abs(ci.declared_alpha - a.value))}, more than the tolerance "
             f"{_dec(ci.tolerance)} (J-7)")]


def rule_j7_beta(ci: ContractIndices, j6: bool) -> list:
    """J-7 for β, as for α."""
    if j6 or not _far(ci.declared_beta, ci.beta.value, ci.tolerance):
        return []
    b = ci.beta
    return [("warning",
             f"contract '{ci.id}' declares beta: {_dec(ci.declared_beta)}, but the index of "
             f"concentration of powers is {_frac(b.value)} (the largest number of powers "
             f"of one single actor is {b.largest} of {len(b.powers)}); they differ by "
             f"{_dec(abs(ci.declared_beta - b.value))}, more than the tolerance "
             f"{_dec(ci.tolerance)} (J-7)")]


def rule_j7_holder(ci: ContractIndices) -> list:
    """J-7: the decision holder names a single actor whose share of the
    powers is smaller than β̂."""
    b = ci.beta
    share = b.holder_share
    if share is None or b.value is None or share >= b.value:
        return []
    return [("warning",
             f"The decision holder {b.holder} of contract '{ci.id}' bears {b.holder_count} of "
             f"the {len(b.powers)} power(s), fewer than {b.top} ({b.largest}): the powers "
             f"are concentrated in another actor (J-7)")]


def rule_j8(ci: ContractIndices) -> list:
    """J-8, for a contract that has ``norms:``: a ``lambda`` greater than
    zero with nothing to scale, and ``lambda: 0`` with an incentive."""
    lam = ci.declared_lambda
    if lam is None or not ci.contract.norms:
        return []
    cov = ci.coverage
    if lam > 0 and not cov.incentive:
        why = ("it has no obligation and no prohibition" if not cov.norms else
               f"none of its {len(cov.norms)} obligation(s) and prohibition(s) carries a "
               f"sanction or a reward (incentive coverage 0/{len(cov.norms)})")
        return [("warning",
                 f"contract '{ci.id}' declares lambda: {_dec(lam)}, but {why}: lambda has "
                 f"nothing to scale in the norms (J-8)")]
    if lam == 0 and cov.incentive:
        return [("warning",
                 f"contract '{ci.id}' declares lambda: 0, but {len(cov.incentive)} norm(s) "
                 f"carry a sanction or a reward ({', '.join(cov.incentive)}): what they "
                 f"declare has no effect in a model (J-8)")]
    return []


def rule_j9(sos: SoSDefinition, indices: list) -> list:
    """J-9: in a file that also declares ``stochastic: 0.1``, the fixed
    value of a parameter bound to ``C.alpha`` or ``C.beta`` under each
    design against the index of C. Informational."""
    from .stochastic import checked_model, stochastic_declared
    if not stochastic_declared(sos) or sos.model is None:
        return []
    try:
        model = checked_model(sos)
    except Exception:
        model = None
    if model is None:
        return []
    by_id = {ci.id: ci for ci in indices}
    out = []
    for p in model.params:
        m = _NORM_REF_RE.fullmatch(p.source or "")
        if not m or m.group(2) not in ("alpha", "beta") or m.group(1) not in by_id:
            continue
        ci = by_id[m.group(1)]
        attr = m.group(2)
        index = (ci.alpha if attr == "alpha" else ci.beta).value
        if index is None:
            continue
        for design in model.designs:
            value = model.fixed_values(design).get(p.id)
            if value is None or abs(Fraction(value) - index) <= ci.tolerance:
                continue
            out.append(("info",
                        f"In the design {design.name}, the parameter '{p.id}' (from "
                        f"{p.source}) has the fixed value {_dec(Fraction(value))}; the index "
                        f"of {'information sharing' if attr == 'alpha' else 'concentration of powers'} "
                        f"of contract '{ci.id}' is {_frac(index)}; they differ by more than "
                        f"the tolerance {_dec(ci.tolerance)}: the norms of the contract do "
                        f"not describe this design (J-9)"))
    return out


def contract_comparisons(ci: ContractIndices) -> list:
    """J-6, J-7 and J-8 for one contract."""
    j6a, j6b = rule_j6_alpha(ci), rule_j6_beta(ci)
    return (j6a + rule_j7_alpha(ci, bool(j6a)) + j6b + rule_j7_beta(ci, bool(j6b))
            + rule_j7_holder(ci) + rule_j8(ci))


def check(sos: SoSDefinition) -> list:
    """The rules of J.8 that concern norms (J-1 to J-9) and the undeclared
    use of the keys (J.12), as (level, text) pairs. The errors concern the
    keys of the extension only: a file that declares ``norms: 0.1`` and
    uses none of them gets no error from this function (J.12)."""
    out = rule_j1(sos) + rule_undeclared(sos)
    if not grounding_active(sos):
        return out
    indices = []
    for contract in sos.contracts:
        ci = contract_indices(sos, contract)
        indices.append(ci)
        out += rule_j2(sos, contract)
        out += rule_j3(sos, contract, ci)
        out += rule_j4(sos, contract)
        out += contract_comparisons(ci)
    out += rule_j9(sos, indices)
    return out


# --- Report (`cadl norms-indices`, J-5) ----------------------------------------

class GroundingError(ValueError):
    """The indices cannot be reported."""


def _ratio(value: Optional[Fraction], num: int, den: int, reason: Optional[str]) -> dict:
    if value is None:
        return {"defined": False, "undefined": reason}
    return {"defined": True, "numerator": num, "denominator": den,
            "exact": f"{value.numerator}/{value.denominator}", "value": float(value)}


def _declared(declared: Optional[Fraction], index: Optional[Fraction]) -> dict:
    out: dict = {"declared": None if declared is None else _dec(declared)}
    out["difference"] = (None if declared is None or index is None
                         else _dec(declared - index))
    return out


def contract_report(ci: ContractIndices, sos: Optional[SoSDefinition] = None) -> dict:
    """The quantities of one contract as JSON-serializable data (J-5)."""
    a, b, cov = ci.alpha, ci.beta, ci.coverage
    alpha = _ratio(a.value, len(a.given_pairs), len(a.pairs), a.undefined_reason)
    alpha.update(
        by_sharing=len(a.by("sharing")), by_obligation=len(a.by("obligation")),
        by_permission=len(a.by("permission")),
        given=[{"item": p[0], "party": p[1],
                "by": [{"way": w, "source": s} for w, s in a.given[p]]}
               for p in a.given_pairs],
        not_given=[{"item": p[0], "party": p[1]} for p in a.not_given],
        member_specific=list(a.member_specific),
        **_declared(ci.declared_alpha, a.value))
    beta = _ratio(b.value, b.largest, len(b.powers), b.undefined_reason)
    share = b.holder_share
    beta.update(
        largest_bearer=b.top, bearers=dict(b.bearers),
        powers=[{"id": pid, "bearer": bearer, "single_actor": single, "effects": n}
                for pid, bearer, single, n in b.powers],
        decision_holder=b.holder, holder_powers=b.holder_count,
        holder_share=None if share is None else f"{share.numerator}/{share.denominator}",
        **_declared(ci.declared_beta, b.value))
    chi = _ratio(cov.value, len(cov.incentive), len(cov.norms), cov.undefined_reason)
    chi.update(incentive=list(cov.incentive),
               reparation_only=list(cov.reparation_only), transition=list(cov.transition),
               declared_lambda=None if ci.declared_lambda is None else _dec(ci.declared_lambda))
    return {
        "id": ci.id,
        "items": dict(ci.ann.items),
        "tolerance": _dec(ci.tolerance),
        "alpha": alpha, "beta": beta, "coverage": chi,
        "diagnostics": [{"level": level, "text": text}
                        for level, text in contract_comparisons(ci)],
    }


def report(sos: SoSDefinition, contract_id: Optional[str] = None) -> dict:
    """What `cadl norms-indices` prints: the quantities of every contract
    of the file (or of one), with the comparisons of J-6 to J-9."""
    if not grounding_declared(sos):
        raise GroundingError("the file does not declare 'grounding: 0.1' under "
                             "extensions:; no index is computed (Appendix J, J.12)")
    if not grounding_active(sos):
        raise GroundingError("the file declares 'grounding: 0.1' without 'norms: 0.1', "
                             "which it requires (J-1)")
    indices = file_indices(sos)
    if contract_id is not None:
        if not any(ci.id == contract_id for ci in indices):
            raise GroundingError(f"no contract '{contract_id}' in the file")
    shown = [ci for ci in indices if contract_id is None or ci.id == contract_id]
    return {
        "grounding": GROUNDING_EXTENSION[1],
        "contracts": [contract_report(ci) for ci in shown],
        "designs": [{"level": level, "text": text} for level, text in rule_j9(sos, shown)],
    }


def format_report_text(result: dict) -> str:
    lines = [f"Derived indices of {result.get('file', 'the file')} (grounding 0.1, J.4)"]

    def head(name: str, q: dict) -> str:
        if not q["defined"]:
            return f"  {name}: undefined ({q['undefined']})"
        return (f"  {name} = {q['numerator']}/{q['denominator']} = "
                f"{_dec(Fraction(q['numerator'], q['denominator']))}")

    def versus(q: dict, key: str) -> str:
        if q["declared"] is None:
            return f"    declared {key}: none"
        if q["difference"] is None:
            return f"    declared {key}: {q['declared']}"
        return f"    declared {key}: {q['declared']}, declared - index = {q['difference']}"

    def names(xs: list) -> str:
        return ", ".join(xs) if xs else "none"

    for c in result["contracts"]:
        a, b, x = c["alpha"], c["beta"], c["coverage"]
        lines += ["", f"Contract {c['id']} (tolerance {c['tolerance']})"]
        lines.append(head("alpha^ (information sharing)", a)
                     + ("" if not a["defined"] else "  given pairs / pairs"))
        lines.append(versus(a, "alpha"))
        lines.append(f"    pairs given by a sharing entry: {a['by_sharing']}, by an "
                     f"obligation: {a['by_obligation']}, by a permission: {a['by_permission']}")
        for g in a["given"]:
            by = "; ".join(f"{s['way']} {s['source']}" if s["way"] != "sharing"
                           else f"sharing entry \"{s['source']}\"" for s in g["by"])
            lines.append(f"    given      ({g['item']}, {g['party']})  by {by}")
        for g in a["not_given"]:
            lines.append(f"    not given  ({g['item']}, {g['party']})")
        lines.append(f"    member-specific entries and norms (no pair): "
                     f"{len(a['member_specific'])}"
                     + (f" ({'; '.join(a['member_specific'])})" if a["member_specific"] else ""))
        lines.append(head("beta^ (concentration of powers)", b)
                     + ("" if not b["defined"] else
                        "  largest number of powers of one single actor / powers"))
        lines.append(versus(b, "beta"))
        if b["powers"]:
            lines.append("    powers per bearer: " + ", ".join(
                f"{k}: {v}" for k, v in b["bearers"].items()))
            for p in b["powers"]:
                lines.append(f"    power {p['id']}: bearer {p['bearer']}"
                             f"{'' if p['single_actor'] else ' (not a single actor)'}, "
                             f"{p['effects']} norm(s) in effects")
        if b["decision_holder"] is None:
            lines.append("    decision holder: none")
        elif b["holder_powers"] is None:
            lines.append(f"    decision holder: {b['decision_holder']} (not a single actor; "
                         f"no share)")
        else:
            lines.append(f"    decision holder: {b['decision_holder']}, bears "
                         f"{b['holder_powers']} power(s)"
                         + (f", share {b['holder_share']}" if b["holder_share"] else ""))
        lines.append(head("chi (incentive coverage)", x)
                     + ("" if not x["defined"] else
                        "  norms that carry an incentive / obligations and prohibitions"))
        lines.append(f"    declared lambda: "
                     f"{x['declared_lambda'] if x['declared_lambda'] is not None else 'none'}"
                     f" (a scale; not compared with chi)")
        lines.append(f"    carry an incentive: {names(x['incentive'])}")
        lines.append(f"    reparation only: {len(x['reparation_only'])} "
                     f"({names(x['reparation_only'])}); with a transition: "
                     f"{len(x['transition'])} ({names(x['transition'])})")
        for d in c["diagnostics"]:
            lines.append(f"  {d['level']:<8} {d['text']}")
    if not result["contracts"]:
        lines += ["", "The file has no contract"]
    for d in result.get("designs", []):
        lines.append(f"  {d['level']:<8} {d['text']}")
    return "\n".join(lines)


# --- IR (J.10) --------------------------------------------------------------------

def _ir_number(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return int(value)
    return value


def ir_contract(sos: SoSDefinition, contract: ContractDef) -> dict:
    """The field ``grounding`` of a contract of the IR (J.10)."""
    ci = contract_indices(sos, contract)
    a, b, cov = ci.alpha, ci.beta, ci.coverage
    tol = ci.tolerance
    return {
        "items": dict(ci.ann.items),
        "tolerance": tol.numerator if tol.denominator == 1 else float(tol),
        "indices": {
            "alpha": None if a.value is None else {
                "given": len(a.given_pairs), "pairs": len(a.pairs),
                "by_sharing": len(a.by("sharing")),
                "by_obligation": len(a.by("obligation")),
                "by_permission": len(a.by("permission")),
                "member_specific": len(a.member_specific)},
            "beta": None if b.value is None else {
                "largest": b.largest, "powers": len(b.powers),
                "holder": b.holder_count, "bearers": dict(b.bearers)},
            "coverage": None if cov.value is None else {
                "incentive": len(cov.incentive), "norms": len(cov.norms),
                "reparation_only": len(cov.reparation_only),
                "transition": len(cov.transition)},
        },
    }


def ir_norm_fields(ann: NormAnnotations) -> dict:
    """The four fields a norm of the IR gains (J.10)."""
    return {
        "shares": list(ann.shares),
        "on_violation_magnitude": _ir_number(ann.sanction_magnitude),
        "on_fulfilment_reward": ann.reward,
        "on_fulfilment_magnitude": _ir_number(ann.reward_magnitude),
    }


# --- Two versions of a contract (J-13) -----------------------------------------

def _finding(level: str, kind: str, text: str, **more) -> dict:
    return {"level": level, "kind": kind, **more, "text": text}


def _num(value: Any) -> str:
    return "none" if value is None else number_text(value)


def rule_j13_direction(old: ContractIndices, new: ContractIndices) -> list:
    """J-13: for α and for β, the declared value and the index are defined
    in both versions and move in opposite directions."""
    out = []
    for key, what in (("alpha", "information sharing"), ("beta", "concentration of powers")):
        d0 = getattr(old, f"declared_{key}")
        d1 = getattr(new, f"declared_{key}")
        i0, i1 = getattr(old, key).value, getattr(new, key).value
        if None in (d0, d1, i0, i1) or (d1 - d0) * (i1 - i0) >= 0:
            continue
        out.append(_finding(
            "warning", "index_opposite",
            f"{key} is {'raised' if d1 > d0 else 'lowered'} from {_dec(d0)} to {_dec(d1)}, "
            f"while the index of {what} {'falls' if i1 < i0 else 'rises'} from "
            f"{_frac(i0)} to {_frac(i1)}: the declared value and the norms move in "
            f"opposite directions (J-13)",
            parameter=key, declared={"old": _dec(d0), "new": _dec(d1)},
            index={"old": f"{i0.numerator}/{i0.denominator}",
                   "new": f"{i1.numerator}/{i1.denominator}"}))
    return out


def diff_contract(old_sos: SoSDefinition, old: ContractDef,
                  new_sos: SoSDefinition, new: ContractDef) -> list:
    """J-13: what changed in the keys of the extension between two
    versions of a contract, as findings of `cadl norms-diff`. Empty when
    neither version has the extension active."""
    if not (grounding_active(old_sos) or grounding_active(new_sos)):
        return []
    out = []
    o, n = contract_indices(old_sos, old), contract_indices(new_sos, new)
    if o.ann.items != n.ann.items:
        removed = [x for x in o.ann.items if x not in n.ann.items]
        added = [x for x in n.ann.items if x not in o.ann.items]
        moved = [x for x in o.ann.items if x in n.ann.items and o.ann.items[x] != n.ann.items[x]]
        parts = []
        if added:
            parts.append(f"added: {', '.join(added)}")
        if removed:
            parts.append(f"removed: {', '.join(removed)}")
        if moved:
            parts.append("origin changed: " + ", ".join(
                f"{x} ({o.ann.items[x]} to {n.ann.items[x]})" for x in moved))
        out.append(_finding("info", "items_changed",
                            f"grounding.items change ({'; '.join(parts)})",
                            added=added, removed=removed, origin_changed=moved))
    if o.tolerance != n.tolerance:
        out.append(_finding("info", "tolerance_changed",
                            f"grounding.tolerance changes from {_dec(o.tolerance)} to "
                            f"{_dec(n.tolerance)}",
                            old=_dec(o.tolerance), new=_dec(n.tolerance)))
    new_by = {d.id: (d, a) for d, a in zip(new.norms, n.ann.norms)}
    for d, a in zip(old.norms, o.ann.norms):
        if d.id not in new_by:
            continue
        _, b = new_by[d.id]
        if a.sanction_magnitude != b.sanction_magnitude:
            out.append(_finding(
                "info", "magnitude_changed",
                f"{d.id}: on_violation.magnitude changes from {_num(a.sanction_magnitude)} "
                f"to {_num(b.sanction_magnitude)}", norm=d.id,
                old=a.sanction_magnitude, new=b.sanction_magnitude))
        fa = (a.has_fulfilment, a.reward, a.reward_magnitude)
        fb = (b.has_fulfilment, b.reward, b.reward_magnitude)
        if fa != fb:
            def show(x: NormAnnotations) -> str:
                if not x.has_fulfilment:
                    return "none"
                return (f"reward '{x.reward}'" if x.reward is not None else "no reward text") \
                    + f", magnitude {_num(x.reward_magnitude)}"
            out.append(_finding(
                "info", "on_fulfilment_changed",
                f"{d.id}: on_fulfilment changes from ({show(a)}) to ({show(b)})", norm=d.id,
                old={"reward": a.reward, "magnitude": a.reward_magnitude}
                if a.has_fulfilment else None,
                new={"reward": b.reward, "magnitude": b.reward_magnitude}
                if b.has_fulfilment else None))
        if a.shares != b.shares:
            out.append(_finding(
                "info", "shares_changed",
                f"{d.id}: shares changes from [{', '.join(a.shares)}] to "
                f"[{', '.join(b.shares)}]", norm=d.id, old=list(a.shares), new=list(b.shares)))
    if not (grounding_active(old_sos) and grounding_active(new_sos)):
        # No quantity is computed for a version that does not declare the
        # extension (J.12), so the indices are not compared.
        return out
    for key, name in (("alpha", "alpha^"), ("beta", "beta^"), ("coverage", "chi")):
        v0, v1 = getattr(o, key).value, getattr(n, key).value
        if v0 != v1:
            def show_v(v: Optional[Fraction]) -> str:
                return "undefined" if v is None else _frac(v)
            out.append(_finding("info", "index_changed",
                                f"{name} changes from {show_v(v0)} to {show_v(v1)}",
                                index=key))
    out += rule_j13_direction(o, n)
    return out


# --- The norm a payoff term names (J.7, J-10) ----------------------------------

@dataclass
class NormLink:
    """What a payoff term ``sanction_of: "C.n"`` or ``reward_of: "C.n"``
    needs from the norm it names. ``error`` is None when the reference is
    usable; otherwise it says why not and ``magnitude`` is None."""
    ref: str
    key: str                               # "sanction_of" | "reward_of"
    contract: Optional[str] = None
    norm: Optional[str] = None
    kind: Optional[str] = None             # kind of the norm
    bearer: Optional[str] = None           # bearer reference, as written
    magnitude: Optional[Fraction] = None   # exact, greater than zero
    magnitude_text: Optional[str] = None   # shortest decimal that round-trips
    lambda_source: Optional[str] = None    # "C.lambda"
    error: Optional[str] = None

    def weight_text(self, param_id: str) -> str:
        """The defined weight as text (J.10): ``-m * p`` or ``m * p``."""
        sign = "-" if self.key == "sanction_of" else ""
        return f"{sign}{self.magnitude_text} * {param_id}"


def norm_payoff_link(sos: SoSDefinition, ref: Any, key: str = "sanction_of") -> NormLink:
    """Resolve the norm reference of a payoff term (J-10).

    ``key`` is ``"sanction_of"`` (the magnitude of ``on_violation:``) or
    ``"reward_of"`` (the magnitude of ``on_fulfilment:``). The result has
    the magnitude (exact), the bearer of the norm as written and the
    source name ``"C.lambda"`` to which exactly one parameter of a model
    with such a term must be bound; ``error`` describes a reference that
    cannot be used: the extension is not active, the value is not a string
    ``C.n``, the contract or the norm does not exist, the norm is not an
    obligation or a prohibition, or it declares no magnitude greater than
    zero for the key. Whether a player stands for the bearer
    (:func:`covers`) and how many parameters are bound to the source
    (:func:`lambda_parameters`) are for the caller to check."""
    if key not in ("sanction_of", "reward_of"):
        raise ValueError(f"key must be 'sanction_of' or 'reward_of', got {key!r}")
    link = NormLink(ref=ref if isinstance(ref, str) else _show(ref), key=key)
    if not grounding_active(sos):
        link.error = ("the file does not declare 'grounding: 0.1' together with "
                      "'norms: 0.1', so the magnitudes of norms are not interpreted")
        return link
    m = _NORM_REF_RE.fullmatch(ref.strip()) if isinstance(ref, str) else None
    if m is None:
        link.error = (f"{key}: {link.ref!r} is not a string of the form C.n (the id of a "
                      f"contract, a dot, the id of a norm)")
        return link
    cid, nid = m.group(1), m.group(2)
    link.contract, link.norm = cid, nid
    contract = next((c for c in sos.contracts if c.id == cid), None)
    if contract is None:
        link.error = f"{key}: '{cid}.{nid}' names the contract '{cid}', which is not declared"
        return link
    link.lambda_source = f"{cid}.lambda"
    ann = annotations(sos, contract)
    found = next(((d, a) for d, a in zip(contract.norms, ann.norms) if d.id == nid), None)
    if found is None:
        link.error = f"{key}: contract '{cid}' has no norm '{nid}'"
        return link
    norm, a = found
    link.kind, link.bearer = norm.kind, norm.bearer
    if norm.kind not in ("obligation", "prohibition"):
        link.error = (f"{key}: '{cid}.{nid}' is a {norm.kind}; only an obligation or a "
                      f"prohibition has a sanction or a reward")
        return link
    value = a.sanction_magnitude if key == "sanction_of" else a.reward_magnitude
    which = "on_violation.magnitude" if key == "sanction_of" else "on_fulfilment.magnitude"
    if value is None:
        link.error = f"{key}: the norm '{cid}.{nid}' declares no {which}"
        return link
    if not value > 0:
        link.error = (f"{key}: the {which} of the norm '{cid}.{nid}' is 0; it must be "
                      f"greater than zero")
        return link
    link.magnitude = exact(value)
    link.magnitude_text = number_text(value)
    return link


def lambda_parameters(params: Any, contract_id: str) -> list:
    """The ids of the parameters of a model that are bound by
    ``from: "C.lambda"`` (J-10 requires exactly one in a model that has a
    term naming a norm of C). ``params`` are objects with ``id`` and
    ``source``, as `cadl.stochastic.Param`."""
    source = f"{contract_id}.lambda"
    return [p.id for p in params if getattr(p, "source", None) == source]
