"""Amendment extension (Appendix K, amendment 0.1): amendable parameters,
versions of a model, and how a metric is measured over time.

The extension adds to ``sos:`` the key ``amendment:`` and to a metric the
keys ``schedule:`` and ``measure:``. They declare

- which parameters of the model may be amended, the layer each belongs to
  (``rule``, ``contract`` or ``regulation``), who decides an amendment
  (an actor or a role of the model, alone or by a vote), the allowed range,
  the step and the largest change of one amendment, and the places of the
  model that hold the value (``at:``);
- the identity of this version of the model (``sos.version``), the version
  it replaces (``previous:``), when it takes effect (``effective:``) and
  what happens to contract instances that are running at that time
  (``transitional:``);
- the records of human decisions that the version rests on
  (``decisions:``), which a parameter, a target of a schedule and a
  measurement method may name;
- targets of a metric that change over time or from one version to the
  next (``schedule:``), and how the metric is measured (``measure:``).

The keys are declarations: no verdict of the core, of Appendix E or of
Appendix F depends on them. :func:`check` applies the rules K-1 to K-13
of K.6, :func:`ir` gives the IR addition of K.8, and :func:`diff_versions`
compares two versions of a model (``cadl amend-diff``, K.7).

All values are exact: a number is the decimal that is written and a
duration is a whole number of milliseconds, both as
:class:`fractions.Fraction`.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field, fields, is_dataclass
from fractions import Fraction
from typing import Any, Optional

from .ast_nodes import DurationLiteral, FloatLiteral, IntLiteral, SoSDefinition

AMENDMENT_EXTENSION = ("amendment", "0.1")

LAYERS = ("rule", "contract", "regulation")
PROCEDURES = ("single", "vote")
TRANSITIONAL_RULES = ("keep", "switch")
DECISION_KINDS = ("gsn", "record")
AGGREGATES = ("mean", "median", "min", "max", "sum", "last")

_BLOCK_KEYS = ("previous", "effective", "transitional", "decisions", "layers", "parameters")
_LAYER_KEYS = ("decided_by", "procedure", "quorum", "voters")
_PARAM_KEYS = ("id", "layer", "value", "min", "max", "step", "max_steps", "unit", "safety",
               "at", "decision") + _LAYER_KEYS
_TRANSITIONAL_KEYS = ("default", "contracts")
_TRANSITIONAL_ENTRY_KEYS = ("contract", "rule", "in_states")
_DECISION_KEYS = ("id", "ref", "kind", "text")
_SCHEDULE_KEYS = ("from", "target", "decision")
_MEASURE_KEYS = ("window", "every", "aggregate", "runs", "across_runs", "description",
                 "decision")
_TIME_FORMS = ("period", "at")
_FROM_FORMS = ("period", "at", "version")

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ACTOR_TEXT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^\[\]]*\])?")
_RESERVED = frozenset({"AND", "OR", "NOT", "IN", "true", "false", "in", "exists"})
_UNIT_MS = {"ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000}

# The sections of `sos:` whose change is not a change of the rules (a
# warning of `amend-diff` when no parameter explains it); a change of any
# other section (actors, contracts, protocols, transitions, regimes, ...)
# is an error. `version:` and `amendment:` describe the version itself and
# are not compared.
_OTHER_SECTIONS = ("name", "description", "metrics", "codegen", "motivation")
_NOT_COMPARED = ("version", "amendment")
# The short names of A.7.1 and H.3: C.alpha, C.beta, C.lambda of a contract,
# and environment.X.
_SHORT = {"alpha": ("information", "alpha"), "beta": ("authority", "beta"),
          "lambda": ("incentives", "lambda")}


# --- The keys as written -----------------------------------------------------

class AmendmentRaw:
    """``SoSDefinition.amendment``: the keys of the extension as the YAML
    loader read them. ``metrics`` is aligned with ``SoSDefinition.metrics``;
    each entry maps the keys the metric has, among ``schedule`` and
    ``measure``, to their values. ``sos`` is the whole ``sos:`` mapping,
    which the locations of ``at:`` (K.3) refer to. Not a dataclass on
    purpose: the code generators do not walk it."""

    __slots__ = ("has_block", "block", "metrics", "sos")

    def __init__(self, has_block: bool, block: Any, metrics: list, sos: dict) -> None:
        self.has_block = has_block
        self.block = block
        self.metrics = metrics
        self.sos = sos

    def used(self) -> list:
        keys = []
        if self.has_block:
            keys.append("amendment:")
        for name in ("schedule", "measure"):
            if any(name in m for m in self.metrics):
                keys.append(f"{name}:")
        return keys

    def __repr__(self) -> str:
        return f"AmendmentRaw({self.used()})"


def read_raw(sos_data: Any) -> Optional[AmendmentRaw]:
    """Parser hook: the keys of the extension, or None when the file uses
    none of them. A key whose value is null counts as absent. Read whether
    or not ``amendment: 0.1`` is declared."""
    if not isinstance(sos_data, dict):
        return None
    block = sos_data.get("amendment")
    metrics = []
    metrics_data = sos_data.get("metrics")
    for m in metrics_data if isinstance(metrics_data, list) else []:
        if not isinstance(m, dict):
            continue
        metrics.append({k: m[k] for k in ("schedule", "measure") if m.get(k) is not None})
    raw = AmendmentRaw(block is not None, block, metrics, sos_data)
    return raw if raw.used() else None


def amendment_declared(sos: SoSDefinition) -> bool:
    """True when the file declares ``amendment: 0.1`` under ``extensions:``."""
    return AMENDMENT_EXTENSION in sos.extensions


# --- Values --------------------------------------------------------------------

@dataclass(frozen=True)
class Quantity:
    """A number or a duration, exactly (a duration in milliseconds)."""
    kind: str          # "number" or "duration"
    value: Fraction

    def text(self) -> str:
        if self.kind == "duration":
            ms = self.value
            if ms.denominator == 1 and ms % 1000 == 0:
                return f"{ms // 1000}s"
            return f"{_decimal(ms)}ms"
        return _decimal(self.value)

    def json(self):
        v = self.value
        return int(v) if v.denominator == 1 else float(v)


def _decimal(v: Fraction) -> str:
    """A finite decimal fraction as text, exactly (a fraction that is not
    one is written as p/q)."""
    sign = "-" if v < 0 else ""
    v = abs(v)
    k = 0
    while (v * 10 ** k).denominator != 1:
        k += 1
        if k > 30:
            return f"{sign}{v.numerator}/{v.denominator}"
    digits = str((v * 10 ** k).numerator).rjust(k + 1, "0")
    if k == 0:
        return sign + digits
    whole, frac = digits[:-k], digits[-k:].rstrip("0")
    return sign + whole + ("." + frac if frac else "")


def _parse_expr(text: str):
    """The expression of a string scalar, or None. A rule of Appendix E may
    use ``x IN [a, b]``, which only :func:`parse_rule` reads."""
    from .parser import parse_expr, parse_rule
    for parse in (parse_expr, parse_rule):
        try:
            return parse(text)
        except Exception:
            continue
    return None


def _literal_quantity(node) -> Optional[Quantity]:
    if isinstance(node, IntLiteral):
        return Quantity("number", Fraction(node.value))
    if isinstance(node, FloatLiteral):
        text = node.text if node.text is not None else repr(node.value)
        try:
            return Quantity("number", Fraction(text))
        except (ValueError, ZeroDivisionError):
            return None
    if isinstance(node, DurationLiteral):
        return Quantity("duration", Fraction(node.value) * _UNIT_MS.get(node.unit, 0))
    return None


def quantity(value: Any) -> Optional[Quantity]:
    """Read a YAML value as a number or a duration (``60s``), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Quantity("number", Fraction(value))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return Quantity("number", Fraction(repr(value)))
    if isinstance(value, str):
        return _literal_quantity(_parse_expr(value.strip()))
    return None


# --- A comparable form of `sos:` ----------------------------------------------

def _canon(node):
    """An expression as nested tuples, without source locations; a literal
    number is ("num", value) and a duration ("dur", milliseconds)."""
    q = _literal_quantity(node)
    if q is not None:
        return ("num" if q.kind == "number" else "dur", q.value)
    if is_dataclass(node):
        return (type(node).__name__,
                tuple((f.name, _canon(getattr(node, f.name)))
                      for f in fields(node) if f.name not in ("loc", "text")))
    if isinstance(node, (list, tuple)):
        return tuple(_canon(x) for x in node)
    return node


def normalize(value):
    """The YAML value with every string that is an expression replaced by
    its form of :func:`_canon`, and every number by ("num", value): two
    values are equal when they mean the same (``2min`` and ``120s``,
    ``0.50`` and ``0.5``)."""
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        q = quantity(value)
        return ("num", q.value) if q is not None else ("text", repr(value))
    if isinstance(value, str):
        expr = _parse_expr(value.strip())
        if expr is not None:
            return ("expr", _canon(expr))
        return ("text", value)
    return ("text", str(value))


def _literal_key(q: Quantity) -> tuple:
    return ("num" if q.kind == "number" else "dur", q.value)


def _count(node, key: tuple, name: str) -> int:
    """Occurrences of the literal ``key`` and of the identifier ``name``
    in a normalized value."""
    if node == key:
        return 1
    if _is_actor_ref(node):
        # The name of the parameter counts; an index is not an occurrence.
        f = dict(node[1])
        return 1 if f.get("name") == name and f.get("index") is None else 0
    if isinstance(node, dict):
        return sum(_count(v, key, name) for v in node.values())
    if isinstance(node, (list, tuple)):
        return sum(_count(v, key, name) for v in node)
    return 0


def _is_actor_ref(node) -> bool:
    return isinstance(node, tuple) and len(node) == 2 and node[0] == "ActorRef"


def _substitute(tree, subs: list):
    """Replace, in one pass over the old tree, the old literal of each
    changed parameter by its new literal below each of its paths. Returns
    the new tree and the clashes: two parameters that map the same old
    literal at the same place to different new literals. The index of an
    actor reference is never replaced."""
    clashes: list = []
    by_path: dict = {}
    for pid, path, old, new in subs:
        by_path.setdefault(path, []).append((pid, old, new))

    prefixes = {path[:i] for path in by_path for i in range(len(path) + 1)}

    def walk(node, path: tuple, active: dict):
        here = by_path.get(path)
        if here:
            active = dict(active)
            for pid, old, new in here:
                prev = active.get(old)
                if prev is not None and prev[1] != new:
                    clashes.append((prev[0], pid, ".".join(str(x) for x in path)))
                active[old] = (pid, new)
        if not active and path not in prefixes:
            return node
        if isinstance(node, tuple) and node in active:
            return active[node][1]
        if _is_actor_ref(node):
            return node
        if isinstance(node, dict):
            return {k: walk(v, path + (k,), active) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, path + (i,), active) for i, v in enumerate(node)]
        if isinstance(node, tuple):
            # Inside an expression the paths of `at:` end; keep the mapping.
            return tuple(walk(v, path + ("~",), active) for v in node)
        return node

    seen = set()
    uniq = []
    for c in clashes:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return walk(tree, (), {}), uniq


# Lists whose order the semantics use: the norms of a contract (processed in
# declaration order, F.5 step 2) and the actors (file order, Appendix J).
def _order_matters(path: list) -> bool:
    p = [str(x) for x in path]
    return p == ["actors"] or (len(p) == 3 and p[0] == "contracts" and p[2] == "norms")


# --- Locations (K.3) -----------------------------------------------------------

def _item_label(item) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    if item.get("id") is not None:
        return str(item["id"])
    if item.get("from") is not None and item.get("to") is not None:
        return f"{item['from']}->{item['to']}"
    return None


def _split_location(text: str) -> Optional[list]:
    """The segments of a location. A segment in double quotes is taken as
    written, dots included (``regimes.CENTRALIZED.parameters."ROUTING.beta"``).
    None when a quote is not closed or does not span a whole segment."""
    segs, cur, i, quoted = [], "", 0, False
    while i < len(text):
        ch = text[i]
        if ch == '"':
            if cur.strip():
                return None
            j = text.find('"', i + 1)
            if j < 0:
                return None
            cur, i, quoted = text[i + 1:j], j + 1, True
            rest = text[i:].lstrip()
            if rest and not rest.startswith("."):
                return None
            continue
        if ch == ".":
            segs.append(cur if quoted else cur.strip())
            cur, quoted = "", False
        else:
            if quoted and not ch.isspace():
                return None
            if not quoted:
                cur += ch
        i += 1
    segs.append(cur if quoted else cur.strip())
    return segs


def resolve(sos_raw: Any, location: str):
    """The concrete path (keys and indices) of a location in ``sos:`` and
    the value there, or a reason why it does not resolve.

    A location is a dot-separated path of keys from ``sos:``. In a
    sequence, a segment selects the item whose ``id:`` equals it, for a
    regime transition the item ``FROM->TO``, and a segment of digits the
    item at that index (from 0). When the first segment is not a key of
    ``sos:``, ``C.alpha``, ``C.beta`` and ``C.lambda`` name the value of
    the contract ``C`` and ``environment.X`` the entry of
    ``context.environment``, as in the regime bindings (A.7.1)."""
    if not isinstance(location, str) or not location.strip():
        return None, None, "a location must be a non-empty string"
    segs = _split_location(location.strip())
    if segs is None:
        return None, None, "an unclosed or misplaced quote"
    if any(not x for x in segs):
        return None, None, "an empty segment"
    if isinstance(sos_raw, dict) and segs[0] not in sos_raw:
        # The short names: C.alpha, C.beta, C.lambda and environment.X.
        if len(segs) == 2 and segs[0] == "environment":
            segs = ["context", "environment", segs[1]]
        elif len(segs) == 2 and segs[1] in _SHORT:
            segs = ["contracts", segs[0], *_SHORT[segs[1]]]
    node = sos_raw
    path: list = []
    for seg in segs:
        if isinstance(node, dict):
            if seg not in node:
                where = ".".join(str(p) for p in _labels(sos_raw, path)) or "sos"
                return None, None, f"'{where}' has no key '{seg}'"
            path.append(seg)
            node = node[seg]
        elif isinstance(node, list):
            if seg.isdigit():
                i = int(seg)
                if i >= len(node):
                    return None, None, f"index {i} is out of range"
                path.append(i)
                node = node[i]
                continue
            hits = [i for i, item in enumerate(node) if _item_label(item) == seg]
            if not hits:
                return None, None, f"no item '{seg}'"
            if len(hits) > 1:
                return None, None, f"more than one item '{seg}'"
            path.append(hits[0])
            node = node[hits[0]]
        else:
            return None, None, f"'{seg}' goes past a value"
    return path, node, None


def _labels(sos_raw, path) -> list:
    """The path with every index replaced by the label of its item."""
    out, node = [], sos_raw
    for p in path:
        if isinstance(p, int):
            label = _item_label(node[p])
            out.append(label if label is not None else str(p))
        else:
            out.append(p)
        node = node[p]
    return out


def _get_path(node, path):
    for p in path:
        node = node[p]
    return node


def _set_path(node, path, value):
    if not path:
        return value
    *head, last = path
    parent = _get_path(node, head)
    parent[last] = value
    return node


# --- The declaration, interpreted ------------------------------------------------

@dataclass
class Authority:
    decided_by: Optional[str] = None
    procedure: str = "single"
    quorum: Optional[int] = None
    voters: list = field(default_factory=list)
    source: str = ""          # "parameter" or "layer <name>"

    def text(self) -> str:
        if not self.decided_by:
            return "nobody"
        if self.procedure == "vote":
            q = f", quorum {self.quorum}" if self.quorum is not None else ""
            v = f" of {', '.join(self.voters)}" if self.voters else ""
            return f"{self.decided_by} by vote{v}{q}"
        return self.decided_by


@dataclass
class Parameter:
    id: str
    layer: Optional[str] = None
    value: Optional[Quantity] = None
    min: Optional[Quantity] = None
    max: Optional[Quantity] = None
    step: Optional[Quantity] = None
    max_steps: int = 1
    unit: Optional[str] = None
    safety: bool = False
    at: list = field(default_factory=list)
    paths: list = field(default_factory=list)   # resolved concrete paths, aligned with at
    decision: Optional[str] = None
    authority: Authority = field(default_factory=Authority)

    def max_change(self) -> Optional[Fraction]:
        return None if self.step is None else self.step.value * self.max_steps

    def comparable(self) -> dict:
        """The declaration apart from the value (what `amend-diff` compares)."""
        a = self.authority
        return {"layer": self.layer, "min": self.min, "max": self.max, "step": self.step,
                "max_steps": self.max_steps, "unit": self.unit, "safety": self.safety,
                "at": list(self.at), "decided_by": a.decided_by, "procedure": a.procedure,
                "quorum": a.quorum, "voters": list(a.voters)}


@dataclass
class Analysis:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    infos: list = field(default_factory=list)
    version: Optional[str] = None
    previous: Optional[str] = None
    effective: Optional[dict] = None
    transitional_default: str = "switch"
    transitional: dict = field(default_factory=dict)   # contract id -> (rule, in_states)
    decisions: list = field(default_factory=list)       # dicts id, ref, kind, text
    layers: dict = field(default_factory=dict)          # name -> Authority
    parameters: list = field(default_factory=list)      # Parameter
    metrics: list = field(default_factory=list)         # dicts id, schedule, measure


def _is_text(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _scalar_text(v) -> Optional[str]:
    if isinstance(v, bool) or v is None or isinstance(v, (dict, list)):
        return None
    text = str(v).strip()
    return text or None


def _time_point(v) -> Optional[str]:
    """An ISO 8601 date or date-time as text, or None. YAML reads an
    unquoted timestamp as a date or datetime."""
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if not isinstance(v, str):
        return None
    text = v.strip()
    for parse in (_dt.datetime.fromisoformat, _dt.date.fromisoformat):
        try:
            parse(text)
            return text
        except ValueError:
            continue
    return None


def _time_order(text: str):
    """A key that orders time points; dates count as midnight UTC, and a
    date-time without an offset as UTC."""
    try:
        t = _dt.datetime.fromisoformat(text)
    except ValueError:
        t = _dt.datetime.combine(_dt.date.fromisoformat(text), _dt.time())
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t


def _pos_int(v) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return None
    return v


class _Checker:
    """Applies K-1 to K-13 to one version and builds the :class:`Analysis`."""

    def __init__(self, sos: SoSDefinition) -> None:
        self.sos = sos
        self.raw: AmendmentRaw = getattr(sos, "amendment", None) or AmendmentRaw(
            False, None, [{} for _ in sos.metrics], {})
        self.a = Analysis()
        self.actor_names = {a.id.name for a in sos.actors}
        self.roles = {str(a.role) for a in sos.actors if a.role}
        self.contracts = {c.id: c for c in sos.contracts}

    def err(self, rule: str, text: str) -> None:
        self.a.errors.append(f"{text} ({rule})")

    def warn(self, rule: str, text: str) -> None:
        self.a.warnings.append(f"{text} ({rule})")

    def info(self, text: str) -> None:
        self.a.infos.append(text)

    def unknown_keys(self, data: dict, allowed, where: str) -> None:
        for k in data:
            if str(k) not in allowed:
                self.err("K-1", f"{where}: unknown key '{k}' (allowed: {', '.join(allowed)})")

    # K-1, K-2
    def run(self) -> Analysis:
        a = self.a
        a.version = _scalar_text(self.sos.version) if self.sos.version is not None else None
        block = self.raw.block if self.raw.has_block else None
        if self.raw.has_block and not isinstance(block, dict):
            self.err("K-1", "amendment: must be a mapping")
            block = None
        block = block or {}
        if self.raw.has_block:
            self.unknown_keys(block, _BLOCK_KEYS, "amendment")
            if a.version is None:
                self.err("K-2", "a file with amendment: must give the identity of its "
                                "version in sos.version")
        if block.get("previous") is not None:
            prev = _scalar_text(block["previous"])
            if prev is None:
                self.err("K-2", "amendment.previous must be a version (a scalar)")
            elif prev == a.version:
                self.err("K-2", f"amendment.previous '{prev}' is the version of this file")
            a.previous = prev
        if block.get("effective") is not None:
            a.effective = self.time_form(block["effective"], _TIME_FORMS,
                                         "amendment.effective", "K-3")
        self.decisions(block.get("decisions"))
        self.transitional(block.get("transitional"))
        self.layers(block.get("layers"))
        self.parameters(block.get("parameters"))
        self.metrics()
        return a

    # K-3
    def time_form(self, v, forms, where: str, rule: str) -> Optional[dict]:
        if not isinstance(v, dict):
            self.err(rule, f"{where} must be a mapping with one of {', '.join(forms)}")
            return None
        self.unknown_keys(v, forms, where)
        given = [k for k in forms if v.get(k) is not None]
        if len(given) != 1:
            self.err(rule, f"{where} must have exactly one of {', '.join(forms)}")
            return None
        k = given[0]
        if k == "period":
            n = _pos_int(v[k])
            if n is None:
                self.err(rule, f"{where}.period must be a positive integer, not {v[k]!r}")
                return None
            return {"period": n}
        if k == "at":
            t = _time_point(v[k])
            if t is None:
                self.err(rule, f"{where}.at must be an ISO 8601 date or date-time, "
                               f"not {v[k]!r}")
                return None
            return {"at": t}
        text = _scalar_text(v[k])
        if text is None:
            self.err(rule, f"{where}.version must be a version (a scalar)")
            return None
        return {"version": text}

    # K-4
    def transitional(self, v) -> None:
        if v is None:
            return
        if not isinstance(v, dict):
            self.err("K-4", "amendment.transitional must be a mapping")
            return
        self.unknown_keys(v, _TRANSITIONAL_KEYS, "amendment.transitional")
        d = v.get("default")
        if d is not None:
            if d not in TRANSITIONAL_RULES:
                self.err("K-4", f"amendment.transitional.default must be one of "
                                f"{', '.join(TRANSITIONAL_RULES)}, not {d!r}")
            else:
                self.a.transitional_default = d
        entries = v.get("contracts")
        if entries is None:
            return
        if not isinstance(entries, list):
            self.err("K-4", "amendment.transitional.contracts must be a sequence")
            return
        for i, e in enumerate(entries):
            where = f"amendment.transitional.contracts[{i}]"
            if not isinstance(e, dict):
                self.err("K-4", f"{where} must be a mapping")
                continue
            self.unknown_keys(e, _TRANSITIONAL_ENTRY_KEYS, where)
            cid = _scalar_text(e.get("contract"))
            if cid is None:
                self.err("K-4", f"{where} has no contract:")
                continue
            if cid not in self.contracts:
                self.err("K-4", f"{where}: unknown contract '{cid}'")
                continue
            if cid in self.a.transitional:
                self.err("K-4", f"{where}: contract '{cid}' is listed more than once")
                continue
            rule = e.get("rule")
            if rule not in TRANSITIONAL_RULES:
                self.err("K-4", f"{where}: rule must be one of {', '.join(TRANSITIONAL_RULES)}, "
                                f"not {rule!r}")
                continue
            states = e.get("in_states")
            in_states: list = []
            if states is not None:
                if rule != "switch":
                    self.err("K-4", f"{where}: in_states: is allowed only with rule: switch")
                    continue
                if not isinstance(states, list) or not states:
                    self.err("K-4", f"{where}: in_states must be a non-empty sequence of states")
                    continue
                lc = self.contracts[cid].lifecycle
                if lc is None:
                    self.err("K-4", f"{where}: contract '{cid}' has no lifecycle:, so "
                                    f"in_states: names no state")
                    continue
                ok = True
                for s in states:
                    s = str(s)
                    if s not in lc.states:
                        self.err("K-4", f"{where}: '{s}' is not a state of the lifecycle of "
                                        f"'{cid}'")
                        ok = False
                    elif s in (lc.terminal or []):
                        self.err("K-4", f"{where}: '{s}' is a terminal state; an instance "
                                        f"there has ended and is not amended")
                        ok = False
                    in_states.append(s)
                if not ok:
                    continue
            self.a.transitional[cid] = (rule, in_states)
        # The relation to F.6 (informative): a kept instance never applies
        # on_amendment:.
        from .norms import norms_declared
        if norms_declared(self.sos):
            for cid, (rule, _) in self.a.transitional.items():
                if rule == "keep" and self.contracts[cid].norms:
                    self.info(f"amendment: running instances of '{cid}' keep the version "
                              f"they started under; the on_amendment: of its norms "
                              f"(Appendix F, F.6) does not apply to them (K.4)")

    # K-5
    def decisions(self, v) -> None:
        if v is None:
            return
        if not isinstance(v, list):
            self.err("K-5", "amendment.decisions must be a sequence")
            return
        seen = set()
        for i, d in enumerate(v):
            where = f"amendment.decisions[{i}]"
            if not isinstance(d, dict):
                self.err("K-5", f"{where} must be a mapping")
                continue
            self.unknown_keys(d, _DECISION_KEYS, where)
            did = _scalar_text(d.get("id"))
            if did is None or not _IDENTIFIER_RE.fullmatch(did) or did in _RESERVED:
                self.err("K-5", f"{where}: id must be an identifier, not {d.get('id')!r}")
                continue
            if did in seen:
                self.err("K-5", f"{where}: decision '{did}' is declared more than once")
                continue
            seen.add(did)
            ref = d.get("ref")
            if not _is_text(ref) or re.search(r"\s", ref.strip()):
                self.err("K-5", f"{where}: ref must be a URI reference without spaces "
                                f"(e.g. gsn.yaml#S2), not {ref!r}")
                continue
            kind = d.get("kind")
            if kind is not None and kind not in DECISION_KINDS:
                self.err("K-5", f"{where}: kind must be one of {', '.join(DECISION_KINDS)}, "
                                f"not {kind!r}")
                continue
            text = d.get("text")
            if text is not None and not isinstance(text, str):
                self.err("K-5", f"{where}: text must be a string")
                continue
            entry = {"id": did, "ref": ref.strip()}
            if kind is not None:
                entry["kind"] = kind
            if text is not None:
                entry["text"] = text
            self.a.decisions.append(entry)

    def decision_ref(self, v, where: str) -> Optional[str]:
        """K-10: a reference to a declared decision."""
        if v is None:
            return None
        did = _scalar_text(v)
        if did is None or did not in {d["id"] for d in self.a.decisions}:
            self.err("K-10", f"{where}: decision {v!r} is not declared under "
                             f"amendment.decisions")
            return None
        return did

    # K-6
    def actor_text(self, v, where: str) -> Optional[str]:
        text = _scalar_text(v)
        if text is None:
            self.err("K-6", f"{where} must name an actor or a role")
            return None
        m = _ACTOR_TEXT_RE.fullmatch(text)
        if m and m.group(1) in self.actor_names:
            return text
        if text in self.roles:
            return text
        self.err("K-6", f"{where}: '{text}' is neither an actor nor a role of the model")
        return None

    def authority(self, d: dict, where: str, base: Optional[Authority]) -> Authority:
        """The authority a layer or a parameter gives; a parameter without
        any of the keys of an authority takes that of its layer."""
        if not any(d.get(k) is not None for k in _LAYER_KEYS):
            if base is None:
                return Authority()
            return Authority(decided_by=base.decided_by, procedure=base.procedure,
                             quorum=base.quorum, voters=list(base.voters), source=base.source)
        a = Authority(source="parameter")
        if d.get("decided_by") is not None:
            a.decided_by = self.actor_text(d["decided_by"], f"{where}.decided_by")
        proc = d.get("procedure")
        if proc is not None:
            if proc not in PROCEDURES:
                self.err("K-6", f"{where}.procedure must be one of {', '.join(PROCEDURES)}, "
                                f"not {proc!r}")
            else:
                a.procedure = proc
        if d.get("quorum") is not None:
            a.quorum = _pos_int(d["quorum"])
            if a.quorum is None:
                self.err("K-6", f"{where}.quorum must be a positive integer")
        voters = d.get("voters")
        if voters is not None:
            if not isinstance(voters, list) or not voters:
                self.err("K-6", f"{where}.voters must be a non-empty sequence of actors")
            else:
                for i, x in enumerate(voters):
                    t = _scalar_text(x)
                    m = _ACTOR_TEXT_RE.fullmatch(t or "")
                    if not m or m.group(1) not in self.actor_names:
                        self.err("K-6", f"{where}.voters[{i}]: '{x}' is not an actor of the "
                                        f"model")
                    else:
                        a.voters.append(t)
        if a.procedure == "vote" and d.get("quorum") is None:
            self.err("K-6", f"{where}: procedure: vote needs quorum:")
        if a.procedure != "vote" and (d.get("quorum") is not None or voters is not None):
            self.err("K-6", f"{where}: quorum: and voters: belong to procedure: vote")
        if a.procedure == "vote" and a.quorum is not None and a.voters and \
                all("[" not in v for v in a.voters) and a.quorum > len(a.voters):
            self.err("K-6", f"{where}: quorum {a.quorum} is larger than the number of "
                            f"voters ({len(a.voters)})")
        if d.get("decided_by") is None:
            self.err("K-6", f"{where}: decided_by: is missing")
        return a

    def layers(self, v) -> None:
        if v is None:
            return
        if not isinstance(v, dict):
            self.err("K-6", "amendment.layers must be a mapping from a layer to its authority")
            return
        for name, d in v.items():
            where = f"amendment.layers.{name}"
            if name not in LAYERS:
                self.err("K-6", f"{where}: unknown layer (allowed: {', '.join(LAYERS)})")
                continue
            if not isinstance(d, dict):
                self.err("K-6", f"{where} must be a mapping")
                continue
            self.unknown_keys(d, _LAYER_KEYS, where)
            auth = self.authority(d, where, None)
            if not any(d.get(k) is not None for k in _LAYER_KEYS):
                self.err("K-6", f"{where}: decided_by: is missing")
            auth.source = f"layer {name}"
            self.a.layers[name] = auth

    # K-7 to K-9
    def parameters(self, v) -> None:
        if v is None:
            return
        if not isinstance(v, list):
            self.err("K-7", "amendment.parameters must be a sequence")
            return
        seen = set()
        for i, d in enumerate(v):
            where = f"amendment.parameters[{i}]"
            if not isinstance(d, dict):
                self.err("K-7", f"{where} must be a mapping")
                continue
            self.unknown_keys(d, _PARAM_KEYS, where)
            pid = _scalar_text(d.get("id"))
            if pid is None or not _IDENTIFIER_RE.fullmatch(pid) or pid in _RESERVED:
                self.err("K-7", f"{where}: id must be an identifier, not {d.get('id')!r}")
                continue
            where = f"parameter '{pid}'"
            if pid in seen:
                self.err("K-7", f"{where} is declared more than once")
                continue
            seen.add(pid)
            p = Parameter(id=pid)
            layer = d.get("layer")
            if layer not in LAYERS:
                self.err("K-7", f"{where}: layer must be one of {', '.join(LAYERS)}, "
                                f"not {layer!r}")
            else:
                p.layer = layer
            base = self.a.layers.get(p.layer) if p.layer else None
            p.authority = self.authority(d, where, base)
            if p.authority.decided_by is None and p.layer is not None and not any(
                    d.get(k) is not None for k in _LAYER_KEYS) and not (
                    base is not None and base.source):
                self.err("K-6", f"{where}: nobody decides its amendment: give decided_by: "
                                f"to the parameter or to amendment.layers.{p.layer}")
            p.value = quantity(d.get("value"))
            if p.value is None:
                self.err("K-7", f"{where}: value must be a number or a duration, "
                                f"not {d.get('value')!r}")
            for key in ("min", "max", "step"):
                if d.get(key) is None:
                    continue
                q = quantity(d[key])
                if q is None:
                    self.err("K-8", f"{where}: {key} must be a number or a duration, "
                                    f"not {d[key]!r}")
                elif p.value is not None and q.kind != p.value.kind:
                    self.err("K-8", f"{where}: {key} is a {q.kind}, the value a "
                                    f"{p.value.kind}")
                else:
                    setattr(p, key, q)
            if d.get("max_steps") is not None:
                n = _pos_int(d["max_steps"])
                if n is None:
                    self.err("K-8", f"{where}: max_steps must be a positive integer")
                elif d.get("step") is None:
                    self.err("K-8", f"{where}: max_steps needs step:")
                else:
                    p.max_steps = n
            if d.get("unit") is not None:
                if not isinstance(d["unit"], str):
                    self.err("K-7", f"{where}: unit must be a string")
                else:
                    p.unit = d["unit"]
            if d.get("safety") is not None:
                if not isinstance(d["safety"], bool):
                    self.err("K-7", f"{where}: safety must be true or false")
                else:
                    p.safety = d["safety"]
            p.decision = self.decision_ref(d.get("decision"), f"{where}.decision")
            self.range(p, where)
            self.locations(p, d.get("at"), where)
            self.a.parameters.append(p)
        self.overlaps()

    def range(self, p: Parameter, where: str) -> None:
        if p.step is not None and p.step.value <= 0:
            self.err("K-8", f"{where}: step must be greater than 0")
            p.step = None
        if p.min is not None and p.max is not None and p.min.value > p.max.value:
            self.err("K-8", f"{where}: min {p.min.text()} is greater than max {p.max.text()}")
            return
        if p.value is None:
            return
        if p.min is not None and p.value.value < p.min.value:
            self.err("K-8", f"{where}: value {p.value.text()} is below min {p.min.text()}")
        if p.max is not None and p.value.value > p.max.value:
            self.err("K-8", f"{where}: value {p.value.text()} is above max {p.max.text()}")
        if p.step is not None and p.min is not None:
            if ((p.value.value - p.min.value) / p.step.value).denominator != 1:
                self.err("K-8", f"{where}: value {p.value.text()} is not min {p.min.text()} "
                                f"plus a whole number of steps of {p.step.text()}")

    def locations(self, p: Parameter, at, where: str) -> None:
        if at is None:
            self.err("K-9", f"{where}: at: must list the places of the model that hold "
                            f"the value")
            return
        if isinstance(at, str):
            at = [at]
        if not isinstance(at, list) or not at:
            self.err("K-9", f"{where}: at: must be a non-empty sequence of locations")
            return
        seen = set()
        for loc in at:
            path, node, why = resolve(self.raw.sos, loc)
            if why is not None:
                self.err("K-9", f"{where}: location {loc!r} does not resolve: {why}")
                continue
            label = ".".join(str(x) for x in _labels(self.raw.sos, path))
            if path and path[0] in ("amendment", "version"):
                self.err("K-9", f"{where}: location {loc!r} is not part of the rules")
                continue
            if tuple(path) in seen:
                self.err("K-9", f"{where}: location {loc!r} is listed more than once")
                continue
            seen.add(tuple(path))
            p.at.append(str(loc).strip())
            p.paths.append(path)
            if p.value is None:
                continue
            norm = normalize(node)
            n_val = _count(norm, _literal_key(p.value), "\0")
            if _count(norm, _literal_key(p.value), p.id) == 0:
                self.err("K-9", f"{where}: location {label} holds neither the value "
                                f"{p.value.text()} nor the name '{p.id}'")
            elif isinstance(node, (dict, list)):
                self.warn("K-9", f"{where}: location {label} is not a scalar or a single "
                                 f"expression; every literal {p.value.text()} in it counts as "
                                 f"the parameter")
            elif n_val > 1:
                self.warn("K-9", f"{where}: location {label} holds the value "
                                 f"{p.value.text()} {n_val} times; every one counts as the "
                                 f"parameter")
            unit = [str(x) for x in path]
            if len(unit) == 4 and unit[0] == "contracts" and (unit[2], unit[3]) in (
                    ("information", "alpha"), ("authority", "beta"), ("incentives", "lambda")):
                lo, hi = p.min, p.max
                if (lo is not None and lo.value < 0) or (hi is not None and hi.value > 1) \
                        or p.value.kind != "number":
                    self.warn("K-8", f"{where}: {label} is a parameter in [0, 1], but the "
                                     f"declared range is outside it")

    def overlaps(self) -> None:
        """Two parameters with the same value at the same place, or one
        inside the other, cannot be told apart (a warning of K-9)."""
        ps = [p for p in self.a.parameters if p.value is not None]
        for i, p in enumerate(ps):
            for q in ps[i + 1:]:
                if p.value != q.value:
                    continue
                for a in p.paths:
                    for b in q.paths:
                        n = min(len(a), len(b))
                        if a[:n] == b[:n]:
                            label = ".".join(str(x) for x in _labels(self.raw.sos,
                                                                       a if len(a) <= len(b) else b))
                            self.warn("K-9", f"parameters '{p.id}' and '{q.id}' have the same "
                                             f"value {p.value.text()} at {label}; an amendment "
                                             f"of one cannot be told from one of the other")

    # K-11, K-12
    def metrics(self) -> None:
        for metric, raw in zip(self.sos.metrics, self.raw.metrics):
            entry: dict = {"id": metric.id}
            if "schedule" in raw:
                entry["schedule"] = self.schedule(metric.id, raw["schedule"])
            if "measure" in raw:
                entry["measure"] = self.measure(metric.id, raw["measure"])
            if len(entry) > 1:
                self.a.metrics.append(entry)

    def schedule(self, mid: str, v) -> list:
        where = f"metric '{mid}': schedule"
        if not isinstance(v, list) or not v:
            self.err("K-11", f"{where} must be a non-empty sequence")
            return []
        out = []
        forms = set()
        last = None
        seen_versions = set()
        for i, e in enumerate(v):
            w = f"{where}[{i}]"
            if not isinstance(e, dict):
                self.err("K-11", f"{w} must be a mapping")
                continue
            self.unknown_keys(e, _SCHEDULE_KEYS, w)
            frm = self.time_form(e.get("from"), _FROM_FORMS, f"{w}.from", "K-11")
            target = e.get("target")
            if isinstance(target, bool) or not isinstance(target, (str, int, float)) or (
                    isinstance(target, float) and target != target):
                self.err("K-11", f"{w}: target must be a string or a number, as the target: "
                                 f"of a metric, not {target!r}")
                frm = None
            decision = self.decision_ref(e.get("decision"), f"{w}.decision")
            if frm is None:
                continue
            (form, point), = frm.items()
            forms.add(form)
            if len(forms) > 1:
                self.err("K-11", f"{w}: the entries of one schedule must all use the same "
                                 f"form of from: (period, at or version)")
                continue
            if form == "version":
                if point in seen_versions:
                    self.err("K-11", f"{w}: version '{point}' is named twice")
                    continue
                seen_versions.add(point)
            else:
                key = point if form == "period" else _time_order(point)
                if last is not None and not key > last:
                    self.err("K-11", f"{w}: from: must come after that of the entry before")
                    continue
                last = key
            item = {"from": frm, "target": target}
            if decision is not None:
                item["decision"] = decision
            out.append(item)
        return out

    def measure(self, mid: str, v) -> dict:
        where = f"metric '{mid}': measure"
        if not isinstance(v, dict):
            self.err("K-12", f"{where} must be a mapping")
            return {}
        self.unknown_keys(v, _MEASURE_KEYS, where)
        out: dict = {}
        for key in ("window", "every"):
            if v.get(key) is None:
                continue
            q = quantity(v[key])
            if q is None or q.kind != "duration" or q.value <= 0:
                self.err("K-12", f"{where}.{key} must be a positive duration, not {v[key]!r}")
            else:
                out[key] = q
        if "every" in out and "window" in out and out["every"].value > out["window"].value:
            self.err("K-12", f"{where}: every: is longer than window:")
        if v.get("every") is not None and v.get("window") is None:
            self.err("K-12", f"{where}: every: needs window:")
        for key, needs in (("aggregate", "every"), ("across_runs", "runs")):
            if v.get(key) is None:
                continue
            if v[key] not in AGGREGATES:
                self.err("K-12", f"{where}.{key} must be one of {', '.join(AGGREGATES)}, "
                                 f"not {v[key]!r}")
            elif v.get(needs) is None:
                self.err("K-12", f"{where}: {key}: needs {needs}:")
            else:
                out[key] = v[key]
        if v.get("runs") is not None:
            n = _pos_int(v["runs"])
            if n is None:
                self.err("K-12", f"{where}.runs must be a positive integer")
            else:
                out["runs"] = n
        if v.get("description") is not None:
            if not isinstance(v["description"], str):
                self.err("K-12", f"{where}.description must be a string")
            else:
                out["description"] = v["description"]
        d = self.decision_ref(v.get("decision"), f"{where}.decision")
        if d is not None:
            out["decision"] = d
        return out


def first_only(effective) -> bool:
    """Whether a version that gives no `previous:` and this `effective:`
    is always a first version of its run (Appendix K, K.11, from cadl
    0.3.35): it gives no effective time, or the first period. Any other
    effective time may come after that of another version, so such a
    version may replace one."""
    return not effective or dict(effective) == {"period": 1}


def may_replace(a) -> bool:
    """Whether the version of an analysis may replace another one: it names
    it with `previous:`, or its effective time may come after another."""
    return a.previous is not None or not first_only(a.effective)


def analyse(sos: SoSDefinition) -> Analysis:
    """Interpret the keys of the extension (whether or not it is declared)."""
    return _Checker(sos).run()


def check(sos: SoSDefinition) -> list:
    """The rules K-1 to K-12 and the undeclared use of the keys (K-13), as
    (level, text) pairs. Nothing for a file that uses none of the keys."""
    raw = getattr(sos, "amendment", None)
    if raw is None:
        return []
    if not amendment_declared(sos):
        return [("warning",
                 f"The file uses {', '.join(raw.used())}, which belong to the amendment "
                 f"extension, but does not declare 'amendment: 0.1' under extensions:; "
                 f"they are undeclared extension keys, and are neither checked nor lowered "
                 f"to the IR (Appendix K, K-13)")]
    a = analyse(sos)
    return ([("error", e) for e in a.errors] + [("warning", w) for w in a.warnings]
            + [("info", i) for i in a.infos])


# --- IR (K.8) ---------------------------------------------------------------------

def _q(q: Optional[Quantity]):
    return None if q is None else q.json()


def ir(sos: SoSDefinition) -> Optional[dict]:
    """The IR addition of K.8, or None when the extension is not declared or
    the file uses none of its keys."""
    if not amendment_declared(sos) or getattr(sos, "amendment", None) is None:
        return None
    a = analyse(sos)
    params = []
    for p in a.parameters:
        kind = p.value.kind if p.value is not None else None
        auth = p.authority
        params.append({
            "id": p.id, "layer": p.layer, "kind": kind,
            "unit": "ms" if kind == "duration" else p.unit,
            "value": _q(p.value), "min": _q(p.min), "max": _q(p.max), "step": _q(p.step),
            "max_steps": p.max_steps, "safety": p.safety,
            "decided_by": auth.decided_by, "procedure": auth.procedure,
            "quorum": auth.quorum, "voters": list(auth.voters),
            "at": list(p.at), "decision": p.decision,
        })
    metrics = []
    for m in a.metrics:
        entry = {"id": m["id"]}
        if "measure" in m:
            meas = {}
            for k, v in m["measure"].items():
                meas[f"{k}_ms" if isinstance(v, Quantity) else k] = (
                    v.json() if isinstance(v, Quantity) else v)
            # The defaults of K.5.
            if "every_ms" in meas:
                meas.setdefault("aggregate", "mean")
            if "runs" in meas:
                meas.setdefault("across_runs", "mean")
            entry["measure"] = meas
        if "schedule" in m:
            entry["schedule"] = [dict(s) for s in m["schedule"]]
        metrics.append(entry)
    contracts = sorted(a.transitional)
    return {
        "version": a.version,
        "previous": a.previous,
        "effective": a.effective,
        "transitional": {
            "default": a.transitional_default,
            "contracts": [{"contract": c, "rule": a.transitional[c][0],
                           "in_states": a.transitional[c][1]} for c in contracts],
        },
        "decisions": list(a.decisions),
        "parameters": params,
        "metrics": metrics,
    }


# --- Two versions (K.7, `cadl amend-diff`) ------------------------------------------

def _finding(level: str, kind: str, text: str, **more) -> dict:
    return {"level": level, "kind": kind, **more, "text": text}


def _diff_paths(ra, rb, na, nb, path: list, out: list, limit: int = 50) -> None:
    """The places where two normalized values differ, named by the raw
    values (ids for items of a sequence)."""
    if len(out) >= limit or na == nb:
        return
    if isinstance(na, dict) and isinstance(nb, dict) and isinstance(ra, dict) \
            and isinstance(rb, dict):
        for k in list(na) + [k for k in nb if k not in na]:
            if k not in nb:
                out.append((path + [k], "removed"))
            elif k not in na:
                out.append((path + [k], "added"))
            else:
                _diff_paths(ra.get(k), rb.get(k), na[k], nb[k], path + [k], out, limit)
        return
    if isinstance(na, list) and isinstance(nb, list) and isinstance(ra, list) \
            and isinstance(rb, list):
        la = [_item_label(x) for x in ra]
        lb = [_item_label(x) for x in rb]
        if all(x is not None for x in la + lb) and len(set(la)) == len(la) \
                and len(set(lb)) == len(lb):
            common_a = [x for x in la if x in lb]
            common_b = [x for x in lb if x in la]
            if common_a != common_b and len(out) < limit:
                out.append((path, "reordered"))
            for i, label in enumerate(la):
                if label not in lb:
                    out.append((path + [label], "removed"))
                else:
                    j = lb.index(label)
                    _diff_paths(ra[i], rb[j], na[i], nb[j], path + [label], out, limit)
            for label in lb:
                if label not in la:
                    out.append((path + [label], "added"))
            return
        if len(na) == len(nb):
            for i in range(len(na)):
                _diff_paths(ra[i], rb[i], na[i], nb[i], path + [str(i)], out, limit)
            return
    out.append((path, "changed"))


def _contract_of(path: list) -> Optional[str]:
    if len(path) >= 2 and path[0] == "contracts":
        return str(path[1])
    return None


def diff_versions(old: SoSDefinition, new: SoSDefinition) -> dict:
    """Compare two versions of a model under the declaration of the old one.

    Returns a JSON-serializable dict: the versions, ``parameters[]`` (each
    declared parameter with its old and new value, its layer and who
    decides), ``contracts[]`` (each contract that changed, the parameters
    that changed it and the transitional rule of the new version),
    ``findings[]``, ``errors`` (texts of the error-level findings) and
    ``ok``. A change is explained when it is the change of a declared
    parameter at the places of its ``at:``; every other change of the
    rules is an error."""
    oa, na = analyse(old), analyse(new)
    if not amendment_declared(old):
        # Without the declaration nothing of the old version is amendable.
        oa.parameters, oa.layers = [], {}
    oraw = getattr(old, "amendment", None)
    nraw = getattr(new, "amendment", None)
    import copy
    o_sos = copy.deepcopy(oraw.sos) if oraw is not None else {}
    n_sos = copy.deepcopy(nraw.sos) if nraw is not None else {}
    findings: list = []

    if not amendment_declared(old):
        findings.append(_finding("warning", "no_declaration",
                                 "the old version does not declare 'amendment: 0.1'; no "
                                 "parameter is amendable, so every change is unexplained"))
    # Versions
    if na.previous is not None and oa.version is None:
        findings.append(_finding(
            "warning", "previous_unchecked",
            f"the new version names '{na.previous}' as previous, but the old version has "
            f"no sos.version, so previous: cannot be checked"))
    elif na.previous is not None and na.previous != oa.version:
        findings.append(_finding(
            "error", "previous_mismatch",
            f"the new version names '{na.previous}' as previous, but the old version is "
            f"'{oa.version}'"))
    if oa.version is not None and na.version == oa.version:
        findings.append(_finding("error", "same_version",
                                 f"both versions are '{oa.version}'"))
    if na.effective is None:
        findings.append(_finding("info", "no_effective",
                                 "the new version does not say when it takes effect"))
    elif oa.effective is not None:
        (ko, vo), = oa.effective.items()
        (kn, vn), = na.effective.items()
        if ko == kn:
            later = (vn > vo) if ko == "period" else (_time_order(vn) > _time_order(vo))
            if not later:
                findings.append(_finding(
                    "error", "effective_not_later",
                    f"the new version takes effect at {kn} {vn}, not after the old one "
                    f"({ko} {vo})"))
        else:
            findings.append(_finding(
                "warning", "effective_not_comparable",
                f"the old version takes effect from {ko} {vo} and the new one from {kn} {vn}; "
                f"a period and a time cannot be compared, so their order is not checked"))

    # Parameters, under the declaration of the old version
    old_params = {p.id: p for p in oa.parameters}
    new_params = {p.id: p for p in na.parameters}
    norm_old = normalize(o_sos)
    params_out = []
    subs: list = []      # (parameter id, path, old literal, new literal)
    touched: dict = {}   # contract id -> [parameter ids]
    for pid, p in old_params.items():
        q = new_params.get(pid)
        entry = {"id": pid, "layer": p.layer, "decided_by": p.authority.text(),
                 "safety": p.safety, "old": p.value.text() if p.value else None,
                 "new": None, "changed": False}
        params_out.append(entry)
        if q is None:
            findings.append(_finding(
                "error", "parameter_not_declared",
                f"parameter '{pid}' of the old version is not declared in the new one; "
                f"its change cannot be checked", parameter=pid))
            continue
        if p.comparable() != q.comparable():
            changed = [k for k, v in p.comparable().items() if q.comparable()[k] != v]
            findings.append(_finding(
                "warning", "amendability_changed",
                f"parameter '{pid}': the declaration changes ({', '.join(changed)}); a change "
                f"of what is amendable is not itself covered by the declaration",
                parameter=pid))
        if p.value is None or q.value is None:
            continue
        entry["new"] = q.value.text()
        if p.value == q.value:
            continue
        entry["changed"] = True
        if p.value.kind != q.value.kind:
            findings.append(_finding(
                "error", "kind_changed",
                f"parameter '{pid}': {p.value.text()} → {q.value.text()} changes a "
                f"{p.value.kind} into a {q.value.kind}", parameter=pid))
            continue
        delta = q.value.value - p.value.value
        what = (f"parameter '{pid}': {p.value.text()} → {q.value.text()} "
                f"({p.layer} layer, decided by {p.authority.text()}"
                f"{', safety' if p.safety else ''})")
        bad = []
        if p.min is not None and q.value.value < p.min.value:
            bad.append(f"below min {p.min.text()}")
        if p.max is not None and q.value.value > p.max.value:
            bad.append(f"above max {p.max.text()}")
        if p.step is not None and p.min is not None and \
                ((q.value.value - p.min.value) / p.step.value).denominator != 1:
            bad.append(f"not on the steps of {p.step.text()} from {p.min.text()}")
        mc = p.max_change()
        if mc is not None and abs(delta) > mc:
            n = p.max_steps
            bad.append(f"moves by {Quantity(p.value.kind, abs(delta)).text()}, more than "
                       f"{n} step{'s' if n > 1 else ''} of {p.step.text()}")
        if bad:
            findings.append(_finding("error", "outside_amendability",
                                     f"{what}: {'; '.join(bad)}", parameter=pid))
        else:
            findings.append(_finding("info", "parameter_changed", what, parameter=pid))
        # The change applies at the places of the old declaration.
        for path in p.paths:
            subs.append((pid, tuple(path), _literal_key(p.value), _literal_key(q.value)))
            n_occ = _count(_get_path(norm_old, path), _literal_key(p.value), "\0")
            if n_occ > 1:
                label = ".".join(str(x) for x in _labels(o_sos, path))
                findings.append(_finding(
                    "error", "ambiguous_location",
                    f"parameter '{pid}': {label} holds {p.value.text()} {n_occ} times; the "
                    f"change of the other occurrences is not explained by it",
                    parameter=pid, location=label))
            labels = _labels(o_sos, path)
            cid = _contract_of(labels)
            if cid is not None:
                touched.setdefault(cid, [])
                if pid not in touched[cid]:
                    touched[cid].append(pid)
    for pid in new_params:
        if pid not in old_params:
            findings.append(_finding(
                "warning", "amendability_changed",
                f"parameter '{pid}' is declared amendable only in the new version",
                parameter=pid))
    if oraw is not None and nraw is not None and oraw.has_block and nraw.has_block:
        ol = {k: v.text() + "|" + v.source for k, v in oa.layers.items()}
        nl = {k: v.text() + "|" + v.source for k, v in na.layers.items()}
        if ol != nl:
            findings.append(_finding("warning", "amendability_changed",
                                     "amendment.layers changes"))

    # Apply every change at once, from the old tree as it is written.
    norm_old, clashes = _substitute(norm_old, subs)
    for a, b, label in clashes:
        findings.append(_finding(
            "error", "ambiguous_location",
            f"parameters '{a}' and '{b}' have the same old value at {label} and change it "
            f"differently; the change cannot be attributed", location=label))

    # Every other change
    norm_new = normalize(n_sos)
    for key in _NOT_COMPARED:
        norm_old.pop(key, None)
        norm_new.pop(key, None)
    diffs: list = []
    _diff_paths(o_sos, n_sos, norm_old, norm_new, [], diffs)
    changed_contracts: dict = {}
    for path, how in diffs:
        label = ".".join(str(x) for x in path) or "sos"
        section = str(path[0]) if path else ""
        level = "warning" if section in _OTHER_SECTIONS else "error"
        if how == "reordered":
            if not _order_matters(path):
                level = "warning"
            findings.append(_finding(
                level, "reordered",
                f"the order of {label} changes" + (
                    "; the semantics use this order" if level == "error" else ""),
                location=label))
        else:
            findings.append(_finding(
                level, "unexplained_change",
                f"{label} is {how} and no declared parameter explains it", location=label))
        cid = _contract_of(path)
        if cid is not None:
            changed_contracts.setdefault(cid, []).append(label)

    # Contracts and the transitional provisions of the new version
    old_ids = [c.id for c in old.contracts]
    new_ids = [c.id for c in new.contracts]
    contracts_out = []
    from .norms import norms_declared
    both_norms = norms_declared(old) and norms_declared(new)
    for cid in old_ids + [c for c in new_ids if c not in old_ids]:
        status = ("removed" if cid not in new_ids else "added" if cid not in old_ids
                  else "changed" if cid in touched or cid in changed_contracts
                  else "unchanged")
        rule, in_states = na.transitional.get(cid, (na.transitional_default, []))
        has_norms = any(c.id == cid and c.norms for c in list(old.contracts) + list(new.contracts))
        # A contract with norms whose instances switch is amended (F.6) even
        # when its text does not change, so it is listed for its norms.
        if status == "unchanged" and not (both_norms and has_norms and rule == "switch"):
            continue
        c_find: list = []
        entry = {"id": cid, "status": status, "by": touched.get(cid, []),
                 "unexplained": changed_contracts.get(cid, []),
                 "transitional": {"rule": rule, "in_states": in_states},
                 "findings": c_find}
        contracts_out.append(entry)
        if status not in ("changed", "unchanged"):
            continue
        oc = next(c for c in old.contracts if c.id == cid)
        nc = next(c for c in new.contracts if c.id == cid)
        if rule == "keep":
            c_find.append(_finding(
                "info", "kept", "running instances finish under the old version; instances "
                "that start after the new version takes effect use it"
                + ("; the on_amendment: of the norms does not apply (F.6)"
                   if oc.norms else "")))
            continue
        # switch: the instance must be in a state of the new version (F.6).
        if oc.lifecycle is not None and status == "changed":
            new_states = set(nc.lifecycle.states) if nc.lifecycle is not None else set()
            terminal = set(oc.lifecycle.terminal or [])
            candidates = in_states or [s for s in oc.lifecycle.states if s not in terminal]
            gone = [s for s in candidates if s not in new_states]
            if gone:
                # The removal itself is an unexplained change (an error above).
                c_find.append(_finding(
                    "warning", "state_removed",
                    f"rule switch: a running instance in {', '.join(gone)} cannot switch, "
                    f"because the new version has no such state (F.6); keep it, or switch "
                    f"only in_states that remain", states=gone))
        when = (f"when it is in {', '.join(in_states)}" if in_states
                else "when the new version takes effect")
        c_find.append(_finding("info", "switched",
                               f"running instances switch to the new version {when}"))
        if both_norms and (oc.norms or nc.norms):
            from .norms_diff import diff_versions as norms_diff
            try:
                nd = norms_diff(old, new, cid)
            except Exception as e:  # noqa: BLE001
                c_find.append(_finding("warning", "norms", f"norms-diff: {e}"))
            else:
                for c in nd["contracts"]:
                    for f in c["findings"]:
                        c_find.append(dict(f, text=f"norms (F.6): {f['text']}"))
    for c in contracts_out:
        for f in c["findings"]:
            if f["level"] == "error":
                findings.append(dict(f, text=f"contract {c['id']}: {f['text']}"))
    errors = [f["text"] for f in findings if f["level"] == "error"]
    return {
        "old_version": oa.version, "new_version": na.version,
        "previous": na.previous, "effective": na.effective,
        "decisions": list(na.decisions),
        "parameters": params_out,
        "contracts": contracts_out,
        "findings": [f for f in findings],
        "errors": errors,
        "ok": not errors,
    }


def load(path) -> SoSDefinition:
    """Parse a file for `amend-diff`. The ``sos:`` mapping is kept also for
    a file that uses none of the keys of the extension, so that its changes
    can be compared; such a file declares no parameter."""
    import yaml
    from pathlib import Path
    from .parser import parse
    text = Path(path).read_text(encoding="utf-8")
    sos = parse(text)
    if getattr(sos, "amendment", None) is None:
        data = yaml.safe_load(text)
        raw = data.get("sos") if isinstance(data, dict) else None
        sos.amendment = AmendmentRaw(False, None, [{} for _ in sos.metrics],
                                     raw if isinstance(raw, dict) else {})
    return sos


def format_diff_text(result: dict) -> str:
    ov = result.get("old_version") or "?"
    nv = result.get("new_version") or "?"
    lines = [f"Amendment from {result.get('old', 'old')} (version {ov}) to "
             f"{result.get('new', 'new')} (version {nv}) (amendment 0.1, K.7)"]
    for e in result.get("invalid", []):
        lines += ["", f"  error    {e}"]
    if result.get("effective"):
        (k, v), = result["effective"].items()
        lines.append(f"  takes effect: {k} {v}")
    if result.get("decisions"):
        lines.append("  decisions: " + ", ".join(f"{d['id']} ({d['ref']})"
                                                  for d in result["decisions"]))
    if result.get("parameters"):
        lines += ["", "Parameters"]
        for p in result["parameters"]:
            mark = "changed  " if p["changed"] else "         "
            new = p["new"] if p["new"] is not None else "?"
            lines.append(f"  {mark}{p['id']}: {p['old']} → {new} ({p['layer']}, "
                         f"{p['decided_by']}{', safety' if p['safety'] else ''})")
    for c in result.get("contracts", []):
        by = f" by {', '.join(c['by'])}" if c["by"] else ""
        t = c["transitional"]
        rule = t["rule"] + (f" in {', '.join(t['in_states'])}" if t["in_states"] else "")
        lines += ["", f"Contract {c['id']}: {c['status']}{by} (transitional: {rule})"]
        for f in c["findings"]:
            lines.append(f"  {f['level']:<8} {f['text']}")
    general = [f for f in result.get("findings", [])
               if not f["text"].startswith("contract ")]
    if general:
        lines += ["", "Findings"]
        for f in general:
            lines.append(f"  {f['level']:<8} {f['text']}")
    n = len(result["errors"])
    lines += ["", "Result: the changes respect the declared amendability" if result["ok"]
              else f"Result: {n} error(s): the changes do not respect the declared "
                   f"amendability"]
    return "\n".join(lines)
