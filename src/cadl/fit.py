"""Fit check for a contract transplanted into another SoS description.

A contract ``c`` fits a destination ``D`` if

  (F1) every role reference of ``c`` resolves to an actor of ``D``;
  (F2) ``A_c and G_c`` is satisfiable; and
  (F3) ``A_c and G_c`` is satisfiable together with the assumptions and
       guarantees of the destination contracts in the neighbourhood of
       ``c``: those that share a party with ``c`` or mention one of its
       variables.

Unlike ``cadl verify``, which checks every pair of contracts that share a
party, (F3) is checked jointly over the neighbourhood, so a conflict that
only several destination contracts create together is found, and a
failure is classified:

  vacuous           the destination's guarantees exclude ``A_c``, so the
                    transplanted rule never applies (or ``G_c`` excludes
                    the assumption of a destination contract)
  guarantee         ``G_c`` contradicts the destination's guarantees
  exclusion         only the assumptions exclude each other: the contracts
                    govern disjoint situations, which need not be a defect
  other             none of the above

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

from .ast_nodes import ContractDef, QuantifiedExpr, SoSDefinition
from .parser import parse
from .type_checker import type_check
from .unparse import expr_to_source
from .verifier import Z3Context, _to_z3_bool

TIMEOUT_MS = 5000

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


def _translate(preds: List[Any], ctx: Z3Context) -> _Part:
    parts, skipped, quantified = [], [], []
    for p in preds:
        try:
            parts.append(_to_z3_bool(p, ctx))
        except Exception:
            skipped.append(_source(p))
            continue
        if _has_quantifier(p):
            quantified.append(_source(p))
    f = z3.And(parts) if parts else z3.BoolVal(True)
    return _Part(f, _variables(f), skipped, quantified)


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


# === main entry ============================================================

def fit_check(data: dict, contract_id: str, scope: str = "both") -> FitReport:
    """Check the fit of contract ``contract_id`` in the description ``data``
    (a loaded YAML mapping). ``scope`` selects the neighbourhood:
    ``party`` (shared parties, as in ``cadl verify``), ``variable`` (shared
    variables) or ``both``."""
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
    cparties = {p.name for p in c.parties}
    cvars = Ac.variables | Gc.variables
    neigh = []
    for d in sos.contracts:
        if d.id == contract_id:
            continue
        shares_party = bool(cparties & {p.name for p in d.parties})
        Ad, Gd = parts[d.id]
        shares_var = bool(cvars & (Ad.variables | Gd.variables))
        if (scope in ("party", "both") and shares_party) or (scope in ("variable", "both") and shares_var):
            neigh.append(d.id)
    report.neighbourhood = neigh
    if not neigh:
        report.findings.append(Finding("F3", "passed", "No destination contract shares a party or a variable"))
        return report

    AN = z3.And([parts[d][0].formula for d in neigh])
    GN = z3.And([parts[d][1].formula for d in neigh])
    if _check(AN, GN) == "unsat":
        core = _core({f"{d}.{k}": parts[d][i].formula for d in neigh
                      for i, k in ((0, "assume"), (1, "guarantee"))})
        report.findings.append(Finding("F3", "unknown",
            "The destination contracts in the neighbourhood already contradict each other",
            contracts=sorted({n.split(".")[0] for n in core or []})))
        return report

    r = _check(Ac.formula, Gc.formula, AN, GN)
    if r == "sat":
        report.findings.append(Finding("F3", "passed",
            f"Jointly satisfiable with the {len(neigh)} contract(s) of the neighbourhood"))
        # Informational: does the destination establish the assumption?
        if _check(GN, z3.Not(Ac.formula)) == "unsat":
            report.findings.append(Finding("info", "info",
                "The destination's guarantees establish the rule's assumptions"))
        return report
    if r == "unknown":
        report.findings.append(Finding("F3", "unknown", "Solver gave no answer"))
        return report

    tracked = {f"{contract_id}.assume": Ac.formula, f"{contract_id}.guarantee": Gc.formula}
    for d in neigh:
        tracked[f"{d}.assume"] = parts[d][0].formula
        tracked[f"{d}.guarantee"] = parts[d][1].formula
    core = _core(tracked) or []
    involved = sorted({n.split(".")[0] for n in core} - {contract_id})
    # Classified against the whole neighbourhood (Definition 1); the core
    # only names the contracts to report.
    if _check(Ac.formula, GN) == "unsat":
        kind = "vacuous"
        msg = "The destination's guarantees exclude the rule's assumption: the rule never applies"
    elif any(_check(parts[d][0].formula, Gc.formula) == "unsat" for d in neigh):
        kind = "vacuous"
        msg = "The rule's guarantees exclude the assumption of a destination contract, which never applies"
    elif _check(Gc.formula, GN) == "unsat":
        kind = "guarantee"
        msg = "The rule's guarantees contradict the destination's guarantees"
    elif _check(Ac.formula, AN) == "unsat":
        kind = "exclusion"
        msg = "Only the assumptions exclude each other: the contracts govern disjoint situations (need not be a defect)"
    else:
        kind = "other"
        msg = "The rule and the destination contracts cannot hold together"
    if len(involved) > 1:
        msg += f" (only jointly: {', '.join(involved)})"
    report.findings.append(Finding("F3", "failed" if kind != "exclusion" else "warning",
                                   msg, kind=kind, contracts=involved))
    return report


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
