"""Fit check for a contract transplanted into another SoS description.

A contract ``c`` fits a destination ``D`` if

  (F1) every role reference of ``c`` resolves to an actor of ``D``;
  (F2) ``A_c and G_c`` is satisfiable; and
  (F3) ``A_c and G_c`` is satisfiable together with the assumptions and
       guarantees of every set of destination contracts in the
       neighbourhood of ``c`` (the contracts linked to ``c`` through a chain
       of shared parties or variables; ``reach="direct"`` keeps only those
       linked to ``c`` itself) that can be in force together with
       ``c``: maximal sets whose assumptions hold together with ``A_c``.
       Usually this is the whole neighbourhood; destination contracts for
       different operating modes give one set per mode.

Unlike ``cadl verify``, which checks every pair of contracts that share a
party, (F3) is checked jointly over the neighbourhood, so a conflict that
only several destination contracts create together is found, and a
failure is classified:

  vacuous           the destination's guarantees exclude ``A_c``, so the
                    transplanted rule never applies
  disabling         ``G_c`` excludes the assumption of destination
                    contracts, which then never apply (the rule does)
  guarantee         ``G_c`` contradicts the destination's guarantees
  other             none of the above

Neighbours whose assumptions exclude the rule's govern other situations and
are reported as an exclusion, a warning that does not make a misfit.

The encoding is that of the verifier (verifier.expr_to_z3), so the same
abstraction applies; predicates whose translation fails and quantifiers
reduced to their bodies are reported instead of being dropped silently.

With a source description and a role mapping, the contract is first copied
from the source into the destination and its actor names are renamed.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml
import z3

from .ast_nodes import ActorRef, ContractDef, QuantifiedExpr, SoSDefinition
from .parser import parse
from .type_checker import type_check
from .unparse import expr_to_source
from .verifier import Z3Context, _to_z3_bool

TIMEOUT_MS = 5000
MAX_SETS = 64
MAX_CORES = 16

# Words of the expression language that look like actor names.
_KEYWORDS = {"AND", "OR", "NOT", "IN", "TRUE", "FALSE", "ALL", "N", "M"}
_NAME = re.compile(r"\b[A-Z][A-Z0-9_]*\b")


# === transplant ============================================================

def parse_role_map(items: Iterable[str]) -> Dict[str, str]:
    """``["DISPATCHER=ROBOT", ...]`` -> ``{"DISPATCHER": "ROBOT"}``."""
    mapping: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"role mapping '{item}' is not of the form OLD=NEW")
        old, new = (s.strip() for s in item.split("=", 1))
        if not old or not new:
            raise ValueError(f"role mapping '{item}' has an empty side")
        mapping[old] = new
    return mapping


def _rename(value: Any, mapping: Dict[str, str]) -> Any:
    """Rename actor names in every string of a YAML subtree."""
    if not mapping:
        return value
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in mapping) + r")\b")
    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return pattern.sub(lambda m: mapping[m.group(1)], v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {walk(k) if isinstance(k, str) else k: walk(x) for k, x in v.items()}
        return v
    return walk(value)


def transplant(source: dict, dest: dict, contract_id: str,
               role_map: Dict[str, str]) -> dict:
    """Return a copy of ``dest`` with contract ``contract_id`` of ``source``
    added after renaming its actor names by ``role_map``."""
    contracts = (source.get("sos") or {}).get("contracts") or []
    found = [c for c in contracts if isinstance(c, dict) and c.get("id") == contract_id]
    if not found:
        raise ValueError(f"contract '{contract_id}' not found in the source")
    result = copy.deepcopy(dest)
    sos = result.setdefault("sos", {})
    dest_contracts = sos.setdefault("contracts", []) or []
    if any(isinstance(c, dict) and c.get("id") == contract_id for c in dest_contracts):
        raise ValueError(f"the destination already has a contract '{contract_id}'")
    dest_contracts.append(_rename(copy.deepcopy(found[0]), role_map))
    sos["contracts"] = dest_contracts
    return result


# === report ================================================================

@dataclass
class Finding:
    condition: str          # "F1", "F2", "F3", "review", "encoding", "info"
    status: str             # "failed", "passed", "unknown", "warning", "info"
    message: str
    kind: Optional[str] = None          # F3 class, see module docstring
    contracts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"condition": self.condition, "status": self.status, "message": self.message}
        if self.kind:
            d["kind"] = self.kind
        if self.contracts:
            d["contracts"] = self.contracts
        return d


@dataclass
class FitReport:
    contract: str
    neighbourhood: List[str] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if any(f.status == "failed" for f in self.findings):
            return "misfit"
        if any(f.status == "unknown" for f in self.findings):
            return "unknown"
        return "fits"

    def to_dict(self) -> dict:
        return {"contract": self.contract, "verdict": self.verdict,
                "neighbourhood": self.neighbourhood,
                "findings": [f.to_dict() for f in self.findings]}


# === encoding ==============================================================

def _has_quantifier(expr: Any) -> bool:
    if isinstance(expr, QuantifiedExpr):
        return True
    if hasattr(expr, "__dataclass_fields__"):
        return any(_has_quantifier(getattr(expr, f)) for f in expr.__dataclass_fields__
                   if f != "loc")
    if isinstance(expr, list):
        return any(_has_quantifier(x) for x in expr)
    return False


def _indexed(expr: Any) -> bool:
    """An actor indexed by a variable, ``*`` or a range (``TAXI[i].x``):
    the term is encoded as one variable for every member, the same in every
    contract that writes it."""
    if isinstance(expr, ActorRef):
        i = expr.index
        if i is not None and not isinstance(i, int) and not (isinstance(i, str) and i.isdigit()):
            return True
    if hasattr(expr, "__dataclass_fields__"):
        return any(_indexed(getattr(expr, f)) for f in expr.__dataclass_fields__ if f != "loc")
    if isinstance(expr, list):
        return any(_indexed(x) for x in expr)
    return False


def _variables(f: Any) -> Set[str]:
    out: Set[str] = set()
    seen: Set[int] = set()
    stack = [f]
    while stack:
        e = stack.pop()
        if e.get_id() in seen:
            continue
        seen.add(e.get_id())
        if z3.is_const(e) and e.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            out.add(e.decl().name())
        stack.extend(e.children())
    return out


@dataclass
class _Part:
    """Assumptions or guarantees of one contract, translated."""
    formula: Any
    variables: Set[str]
    skipped: List[str]
    quantified: List[str]
    terms: List[Tuple[str, Any]] = field(default_factory=list)
    indexed: List[str] = field(default_factory=list)


def _translate(preds: List[Any], ctx: Z3Context) -> _Part:
    parts, skipped, quantified, terms, indexed = [], [], [], [], []
    for p in preds:
        if _indexed(p) and not _has_quantifier(p):
            indexed.append(_source(p))
        try:
            z = _to_z3_bool(p, ctx)
        except Exception:
            skipped.append(_source(p))
            continue
        parts.append(z)
        terms.append((_source(p), z))
        if _has_quantifier(p):
            quantified.append(_source(p))
    f = z3.And(parts) if parts else z3.BoolVal(True)
    return _Part(f, _variables(f), skipped, quantified, terms, indexed)


def _source(expr: Any) -> str:
    try:
        return expr_to_source(expr)
    except Exception:
        return repr(expr)


def _check(*formulas: Any) -> str:
    s = z3.Solver()
    s.set("timeout", TIMEOUT_MS)
    s.add(*formulas)
    r = s.check()
    return "sat" if r == z3.sat else "unsat" if r == z3.unsat else "unknown"


def _core(tracked: Dict[str, Any]) -> Optional[List[str]]:
    """Names of a minimal subset of ``tracked`` formulas that is unsat."""
    s = z3.Solver()
    s.set("timeout", TIMEOUT_MS)
    s.set("core.minimize", True)
    labels = {}
    for name, f in tracked.items():
        lit = z3.Bool(f"__track_{len(labels)}")
        labels[str(lit)] = name
        s.assert_and_track(f, lit)
    if s.check() != z3.unsat:
        return None
    return sorted({labels[str(c)] for c in s.unsat_core()})


# === F1 ====================================================================

def _review_names(raw: dict, declared: Set[str], contract_id: str) -> List[str]:
    """All-caps names in positions the type checker does not resolve that
    are not declared actors: monitor rules, lifecycle guards and predicate
    triggers, violation strings, and incentive rules."""
    texts: List[str] = []
    for m in raw.get("monitors") or []:
        if isinstance(m, dict) and isinstance(m.get("rule"), str):
            texts.append(m["rule"])
    lc = raw.get("lifecycle") or {}
    for t in (lc.get("transitions") if isinstance(lc, dict) else None) or []:
        if isinstance(t, dict):
            for key in ("when", "on", True):   # `on:` may load as True
                v = t.get(key)
                if isinstance(v, str) and "->" not in v:
                    texts.append(v)
    viol = raw.get("violation") or {}
    if isinstance(viol, dict):
        texts += [v for v in viol.values() if isinstance(v, str)]
    inc = raw.get("incentives") or {}
    if isinstance(inc, dict):
        texts += [r for r in inc.get("rules") or [] if isinstance(r, str)]
    names = set()
    for t in texts:
        names |= {n for n in _NAME.findall(t)
                  if n not in declared and n not in _KEYWORDS and len(n) > 1}
    return sorted(names)


def _partial(report: "FitReport", abstracted: int) -> None:
    """A pass over an abstraction is not a pass: with dropped predicates or
    reduced quantifiers, (F2) and (F3) hold only for what was translated."""
    if abstracted:
        report.findings.append(Finding("encoding", "unknown",
            f"Satisfiable only for the translated fragment: {abstracted} predicate(s) "
            "were dropped or had a quantifier reduced"))


# === main entry ============================================================

def fit_check(data: dict, contract_id: str, scope: str = "both",
              reach: str = "component") -> FitReport:
    """Check the fit of contract ``contract_id`` in the description ``data``
    (a loaded YAML mapping). ``scope`` selects the links between contracts:
    ``party`` (shared parties, as in ``cadl verify``), ``variable`` (shared
    variables) or ``both``. ``reach`` selects the neighbourhood: the
    contracts linked to the rule directly (``direct``) or through any chain
    of links (``component``, the default). With variable links, contracts
    outside the component share no variable with the rule or with any
    contract in it, so they cannot change the satisfiability of (F3) unless
    they are inconsistent among themselves."""
    sos = parse(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    report = FitReport(contract=contract_id)
    by_id = {c.id: c for c in sos.contracts}
    if contract_id not in by_id:
        raise ValueError(f"contract '{contract_id}' not found in the destination")
    c = by_id[contract_id]
    declared = {a.id.name for a in sos.actors}

    # F1 -- the type checker's errors that name the contract.
    tc = type_check(sos)
    mine = [e for e in tc.errors if f"'{contract_id}'" in e.message]
    for e in mine:
        report.findings.append(Finding("F1", "failed", e.message))
    if not mine:
        report.findings.append(Finding("F1", "passed", "Every role reference in a resolved position names a declared actor"))
    others = [e for e in tc.errors if e not in mine]
    if others:
        report.findings.append(Finding("info", "info",
            f"{len(others)} type error(s) elsewhere in the description; run cadl check"))
    raw = next((x for x in (data.get("sos") or {}).get("contracts") or []
                if isinstance(x, dict) and x.get("id") == contract_id), {})
    review = _review_names(raw, declared, contract_id)
    if review:
        report.findings.append(Finding("review", "warning",
            "Names in unresolved positions that are not declared actors: "
            + ", ".join(review) + " (rules, guards, violation, incentives)"))

    # Encoding, one shared context so that equal names are equal variables.
    ctx = Z3Context()
    parts: Dict[str, Tuple[_Part, _Part]] = {
        d.id: (_translate(d.assume, ctx), _translate(d.guarantee, ctx)) for d in sos.contracts
    }
    Ac, Gc = parts[contract_id]
    for label, part in (("assume", Ac), ("guarantee", Gc)):
        for s in part.skipped:
            report.findings.append(Finding("encoding", "warning",
                f"{label} '{s}' could not be translated and is not checked"))
        for s in part.quantified:
            report.findings.append(Finding("encoding", "warning",
                f"{label} '{s}': the quantifier is reduced to its body"))

    # F2
    r = _check(Ac.formula, Gc.formula)
    report.findings.append(Finding("F2", {"sat": "passed", "unsat": "failed"}.get(r, "unknown"),
        {"sat": "Assumptions and guarantees are jointly satisfiable",
         "unsat": "Assumptions and guarantees contradict each other"}.get(r, "Solver gave no answer")))

    # Neighbourhood
    def linked(x, y):
        if scope in ("party", "both") and \
                {p.name for p in x.parties} & {p.name for p in y.parties}:
            return True
        return scope in ("variable", "both") and bool(
            (parts[x.id][0].variables | parts[x.id][1].variables)
            & (parts[y.id][0].variables | parts[y.id][1].variables))

    others = [d for d in sos.contracts if d.id != contract_id]
    reached = {contract_id}
    frontier = [c]
    while frontier:
        x = frontier.pop()
        for d in others:
            if d.id not in reached and linked(x, d):
                reached.add(d.id)
                if reach == "component":
                    frontier.append(d)
    neigh = [d.id for d in others if d.id in reached]
    report.neighbourhood = neigh
    for d in neigh:
        for label, part in zip(("assume", "guarantee"), parts[d]):
            for t in part.skipped:
                report.findings.append(Finding("encoding", "warning",
                    f"{d}.{label} '{t}' could not be translated and is not checked"))
            for t in part.quantified:
                report.findings.append(Finding("encoding", "warning",
                    f"{d}.{label} '{t}': the quantifier is reduced to its body"))
    abstracted = sum(len(p.skipped) + len(p.quantified)
                     for x in [contract_id] + neigh for p in parts[x])
    if not neigh:
        report.findings.append(Finding("F3", "passed", "No destination contract shares a party or a variable"))
        _partial(report, abstracted)
        return report

    def conj(ds, k):
        return [parts[d][k].formula for d in ds]

    # Destination contracts whose assumptions exclude the rule's are never in
    # force together with it; a destination contract inconsistent by itself
    # makes the check meaningless.
    excluded = [d for d in neigh if _check(Ac.formula, *conj([d], 0)) == "unsat"]
    broken = [d for d in neigh if d not in excluded
              and _check(*conj([d], 0), *conj([d], 1)) == "unsat"]
    if broken:
        report.findings.append(Finding("F3", "unknown",
            "Destination contract(s) inconsistent by themselves", contracts=broken))
        return report
    cand = [d for d in neigh if d not in excluded]

    # The sets of destination contracts that can be in force together with
    # the rule: maximal subsets whose assumptions hold together with A_c.
    # Usually this is all of them; contracts for different operating modes
    # give one set per mode.
    if _check(Ac.formula, *conj(cand, 0)) == "sat":
        sets = [cand]
    else:
        sets = _maximal_sets(Ac.formula, cand, {d: parts[d][0].formula for d in cand})
        if sets is None:
            report.findings.append(Finding("F3", "unknown",
                f"The assumptions of the {len(cand)} neighbours exclude each other in more "
                f"than {MAX_SETS} ways (sets that can be in force together); not enumerated"))
            return report
    for S in sets:
        if _check(*conj(S, 0), *conj(S, 1)) == "unsat":
            core = _core({f"{d}.{k}": parts[d][i].formula for d in S
                          for i, k in ((0, "assume"), (1, "guarantee"))})
            report.findings.append(Finding("F3", "unknown",
                "Destination contracts that can be in force together contradict each other",
                contracts=sorted({n.split(".")[0] for n in core or []})))
            return report

    failing = None
    for S in sets:
        r = _check(Ac.formula, Gc.formula, *conj(S, 0), *conj(S, 1))
        if r == "unknown":
            report.findings.append(Finding("F3", "unknown", "Solver gave no answer"))
            return report
        if r == "unsat":
            failing = S
            break

    if failing is None:
        if excluded or len(sets) > 1:
            msg = ("The assumptions exclude each other: the contracts govern disjoint "
                   "situations (need not be a defect)")
            if len(sets) > 1:
                msg += f"; checked against {len(sets)} sets of destination contracts that can be in force together"
            # Neighbours missing from some set: never, or only in some
            # situations, in force together with the rule (order-free).
            report.findings.append(Finding("F3", "warning", msg, kind="exclusion",
                contracts=sorted(d for d in neigh if not all(d in S for S in sets))))
        else:
            report.findings.append(Finding("F3", "passed",
                f"Jointly satisfiable with the {len(neigh)} contract(s) of the neighbourhood"))
        # Established only if every set that can be in force with the rule
        # implies it.
        def implied(z):
            return all(_check(z3.And(conj(S, 1)) if S else z3.BoolVal(True), z3.Not(z)) == "unsat"
                       for S in (sets or [[]]))
        if implied(Ac.formula):
            report.findings.append(Finding("info", "info",
                "The destination's guarantees establish the rule's assumptions"))
        else:
            # Consistent is not established: name the assumptions that no
            # destination guarantee implies, which the transplant must bring
            # along or the destination must add.
            open_ = [t for t, z in Ac.terms if not implied(z)]
            if open_:
                report.findings.append(Finding("info", "info",
                    "Assumptions the destination's guarantees do not establish "
                    "(supply them with the rule or in the destination): " + "; ".join(open_)))
        _partial(report, abstracted)
        return report

    # Every minimal conflict, over every set that can be in force with the
    # rule: a minimal core K is found, and the search branches on removing
    # each contract of K, so every other minimal conflict, which must miss
    # some contract of K, is reached (bounded by MAX_CORES).
    failing_sets = [S for S in sets
                    if _check(Ac.formula, Gc.formula, *conj(S, 0), *conj(S, 1)) == "unsat"]
    cores: List[List[str]] = []
    seen: Set[frozenset] = set()
    stack = [list(S) for S in failing_sets]
    truncated = None
    while stack:
        S = stack.pop()
        if frozenset(S) in seen:
            continue
        if len(seen) >= 64 * MAX_CORES:
            truncated = f"after examining {64 * MAX_CORES} sets of contracts"
            break
        seen.add(frozenset(S))
        if _check(Ac.formula, Gc.formula, *conj(S, 0), *conj(S, 1)) != "unsat":
            continue
        tracked = {f"{contract_id}.assume": Ac.formula, f"{contract_id}.guarantee": Gc.formula}
        for d in S:
            tracked[f"{d}.assume"] = parts[d][0].formula
            tracked[f"{d}.guarantee"] = parts[d][1].formula
        K = sorted({n.split(".")[0] for n in _core(tracked) or []} - {contract_id})
        if not K:
            # The rule contradicts itself (F2); (F3) fails with it.
            report.findings.append(Finding("F3", "failed",
                "Fails because the rule itself is unsatisfiable (F2)", kind="other"))
            break
        if K not in cores:
            if len(cores) == MAX_CORES:
                truncated = f"at {MAX_CORES} minimal conflicts"
                break
            cores.append(K)
        for k in K:
            stack.append([d for d in S if d != k])
    order = {"vacuous": 0, "disabling": 1, "guarantee": 2, "other": 3}
    found = sorted((_classify(contract_id, Ac, Gc, K, parts) + (K,) for K in cores),
                   key=lambda x: (order[x[0]], len(x[2]), x[2]))
    def exact(p):
        # The translated terms of a part that are not abstracted.
        bad = set(p.quantified) | set(p.indexed)
        return [z for t, z in p.terms if t not in bad]

    def abstracted_in(ids):
        # The conflict depends on the abstraction unless it remains when
        # the abstracted terms are left out.
        if not any(p.quantified or p.indexed for x in ids for p in parts[x]):
            return False
        return _check(*[z for x in ids for p in parts[x] for z in exact(p)]) != "unsat"
    for kind, msg, K in found:
        if len(K) > 1:
            msg += f" (only jointly: {', '.join(K)})"
        if abstracted_in([contract_id] + K):
            # Exact only on the supported fragment: a reduced quantifier or
            # an index variable can make the conflict spurious.
            msg += "; involves abstracted predicates (quantifier or index variable), so it can be spurious: review"
        report.findings.append(Finding("F3", "failed", msg, kind=kind, contracts=K))
    if truncated:
        # The conflicts above are genuine (in the encoding), so the verdict
        # stays a misfit, but the list is not complete.
        report.findings.append(Finding("F3", "warning",
            f"The search for minimal conflicts stopped {truncated}; "
            "the conflicts reported are not a complete list", kind="truncated"))
    # Exclusions, as on the fit path (neighbours missing from some set),
    # reported as information next to the misfit.
    missing = sorted(d for d in neigh if not all(d in S for S in sets))
    if missing:
        report.findings.append(Finding("info", "info",
            "Set aside as exclusions (never, or only in some situations, in force with the rule): "
            + ", ".join(missing)))
    return report


def _maximal_sets(Ac: Any, cand: List[str], assume: Dict[str, Any]) -> Optional[List[List[str]]]:
    """The maximal subsets of ``cand`` whose assumptions hold together with
    ``Ac``. Each round asks the solver for a valuation that satisfies ``Ac``
    and the assumptions of a set not contained in any set found so far, and
    grows that set to a maximal one; the number of solver calls grows with
    the number of maximal sets times the number of candidates, not with the
    number of subsets. None if there are more than MAX_SETS."""
    sel = {d: z3.Bool(f"__in_{i}") for i, d in enumerate(cand)}
    s = z3.Solver()
    s.set("timeout", TIMEOUT_MS)
    s.add(Ac)
    for d in cand:
        s.add(z3.Implies(sel[d], assume[d]))
    sets: List[List[str]] = []
    while True:
        if s.check() != z3.sat:
            break
        m = s.model()
        S = [d for d in cand if z3.is_true(m.eval(sel[d], model_completion=True))]
        for d in cand:          # grow to a maximal set
            if d not in S and _check(Ac, *[assume[x] for x in S + [d]]) == "sat":
                S.append(d)
        S = [d for d in cand if d in S]
        sets.append(S)
        if len(sets) > MAX_SETS:
            return None
        # The next set must contain a candidate outside S.
        out = [sel[d] for d in cand if d not in S]
        if not out:
            break
        s.add(z3.Or(out))
    return sets


def _classify(contract_id, Ac, Gc, K, parts):
    """Class of the minimal conflict of the rule with the contracts ``K``
    (Definition 1). Minimal conflicts are unique, so the class does not
    depend on which core the solver returns first."""
    AK = z3.And([parts[d][0].formula for d in K]) if K else z3.BoolVal(True)
    GK = z3.And([parts[d][1].formula for d in K]) if K else z3.BoolVal(True)
    if _check(Ac.formula, GK) == "unsat":
        return ("vacuous", "The destination's guarantees exclude the rule's assumption: the rule "
                "never applies while these destination contracts are in force")
    if _check(AK, Gc.formula) == "unsat":
        return ("disabling", "The rule's guarantees exclude the assumption of destination contracts: "
                "the rule still applies, but they never apply while it is in force")
    if _check(Gc.formula, GK) == "unsat":
        return ("guarantee", "The rule's guarantees contradict the destination's guarantees: the "
                "rule applies but cannot be met together with them")
    return ("other", "The rule and the destination contracts cannot hold together")


def format_text(report: FitReport) -> str:
    sym = {"passed": "PASS", "failed": "FAIL", "unknown": "UNKNOWN",
           "warning": "WARN", "info": "INFO"}
    lines = [f"Fit of contract '{report.contract}'",
             f"  neighbourhood: {', '.join(report.neighbourhood) or '(none)'}"]
    for f in report.findings:
        tag = f"{f.condition}" + (f" {f.kind}" if f.kind else "")
        extra = f" [{', '.join(f.contracts)}]" if f.contracts else ""
        lines.append(f"  [{sym.get(f.status, f.status)}] {tag}: {f.message}{extra}")
    lines.append(f"Verdict: {report.verdict.upper()}")
    return "\n".join(lines)
