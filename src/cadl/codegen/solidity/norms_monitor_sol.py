"""Norms monitor of the Solidity target (Appendix F, F.5; Appendix D).

For each contract of a file that declares ``norms: 0.1``,
`cadl codegen -t solidity` writes ``<Contract>NormsMonitor.sol``: a
Solidity contract that keeps the state of a monitor of the norms, the
counts-as rules and the ``assume:`` / ``guarantee:`` items of one contract
instance. A host records each observation with ``observe`` and reads the
verdicts with ``verdicts`` or from the events the contract emits.

The file consists of the base contract ``norms_monitor_base.sol``, the
same for every contract (an interpreter of the model and the order of
one observation of F.5), and a contract derived from it whose constructor
fills the model (`norms_ir.build_ir`). The constructor is the only
generated code: every text from the .cadl file is written as a hex
literal (``string(hex"...")``) and every number as an integer literal.

Numbers are fixed-point with 6 decimals (SCALE = 10**6), so a literal
with more decimals, and the operators ``*`` and ``/`` (whose results the
fixed point would round), are not supported; neither is a contract the
IR refuses (quantifiers, aggregates, wildcard references).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from ...ast_nodes import ContractDef, SoSDefinition
from ..emitter import sanitize_id
from ..norms_ir import EFFECT_KINDS, NormsTargetUnsupported, build_ir, check_contract_id

BASE = Path(__file__).with_name("norms_monitor_base.sol")
SCALE = 10 ** 6
_OPS = {"lit": 0, "name": 1, "sym": 2, "mem": 3, "req": 4, "not": 5, "and": 6, "or": 7,
        "bin": 8, "in": 9, "within": 10, "throughout": 11, "until": 12}
_BOPS = {"==": 0, "!=": 1, "<": 2, "<=": 3, ">": 4, ">=": 5, "+": 6, "-": 7}
_KINDS = {"obligation": 0, "permission": 1, "prohibition": 2, "power": 3}
_TERMS = {"event": 1, "message": 2, "predicate": 3}
_INT256 = 2 ** 255


def monitor_name(contract_id: str) -> str:
    return f"{sanitize_id(contract_id)}NormsMonitor"


def _s(text: Any) -> str:
    """A text of the .cadl file as a Solidity expression of type string."""
    data = str(text).encode("utf-8", errors="surrogatepass")
    return f'string(hex"{data.hex()}")' if data else '""'


def scaled(value: Any, where: str) -> int:
    """A number as an int256 times SCALE; refuses what the fixed point cannot hold."""
    d = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    v = d * SCALE
    if v != v.to_integral_value():
        raise NormsTargetUnsupported([
            f"{where}: the number {value!r} has more than 6 decimals (the Solidity "
            f"monitor uses fixed-point numbers with 6 decimals)"])
    v = int(v)
    if not -_INT256 < v < _INT256:
        raise NormsTargetUnsupported([f"{where}: the number {value!r} is too large"])
    return v


def _index(idx: dict | None) -> tuple[int, str]:
    if idx is None:
        return 0, '""'
    if "lit" in idx:
        return 1, _s(idx["lit"])
    return 2, _s(idx["var"])


def _pat_args(p: dict) -> str:
    idx = p["index"]
    if idx is not None and idx.get("any"):
        rng = idx.get("range")
        lo, hi = (rng if rng else (0, 0))
        return f'{_s(p["name"])}, 3, "", {"true" if rng else "false"}, {int(lo)}, {int(hi)}'
    ik, text = _index(idx)
    return f'{_s(p["name"])}, {ik}, {text}, false, 0, 0'


def _node_lines(nodes: list[dict]) -> list[str]:
    out = []
    for k, nd in enumerate(nodes):
        where = f"node {k}"
        op = nd["op"]
        bop, a, b, d, ik = 0, 0, 0, 0, 0
        s1 = s2 = s3 = '""'
        lk, ln, ls = 0, 0, '""'
        items: list[int] = []
        if op == "lit":
            v = nd["v"]
            if isinstance(v, bool):
                lk, ln = 2, int(v)
            elif isinstance(v, (int, float)):
                lk, ln = 3, scaled(v, where)
            else:
                lk, ls = 4, _s(v)
        elif op == "name":
            s1 = _s(nd["n"])
        elif op == "sym":
            s1 = _s(nd["a"])
            ik, s2 = _index(nd["i"])
        elif op == "mem":
            s1, s3 = _s(nd["a"]), _s(nd["m"])
            ik, s2 = _index(nd["i"])
        elif op == "req":
            s1 = _s(".".join(nd["path"]))
        elif op == "not":
            a = nd["x"]
        elif op in ("and", "or"):
            a, b = nd["l"], nd["r"]
        elif op == "bin":
            if nd["o"] not in _BOPS:
                raise NormsTargetUnsupported([
                    f"the operator {nd['o']} (the Solidity monitor uses fixed-point numbers "
                    f"and supports + and - only)"])
            bop, a, b = _BOPS[nd["o"]], nd["l"], nd["r"]
        elif op == "in":
            a, items = nd["l"], nd["items"]
        elif op in ("within", "throughout"):
            a, d = nd["x"], scaled(nd["d"], where)
        elif op == "until":
            a, b, d = nd["p"], nd["q"], scaled(nd["d"], where)
        out.append(f"        _node({_OPS[op]}, {bop}, {a}, {b}, {d}, {ik}, {s1}, {s2}, {s3}, "
                   f"{lk}, {ln}, {ls});")
        out += [f"        _item({i});" for i in items]
    return out


def generate_norms_monitor_sol(sos: SoSDefinition, contract: ContractDef) -> str:
    """The Solidity text of the monitor of one contract. Raises
    `NormsTargetUnsupported` for a contract that cannot be monitored."""
    check_contract_id(contract.id)
    ir = build_ir(sos, contract)
    body: list[str] = []
    body.append(f"        contractId = {_s(ir['contract'])};")
    for name, (indexed, lo, hi) in ir["actors"].items():
        body.append(f"        _actor({_s(name)}, {str(bool(indexed)).lower()}, "
                    f"{str(lo is not None).lower()}, {int(lo or 0)}, "
                    f"{str(hi is not None).lower()}, {int(hi or 0)});")
    lc = ir["lifecycle"]
    if lc is not None:
        body.append("        data.hasLifecycle = true;")
        body.append(f"        lcInitial = {_s(lc['initial'] or '')};")
        for s in lc["states"]:
            body.append(f"        lcStates[keccak256(bytes({_s(s)}))] = true;")
        for s in lc["terminal"]:
            body.append(f"        lcTerminal[keccak256(bytes({_s(s)}))] = true;")
    body += _node_lines(ir["exprs"])

    terms: list[str] = []
    count = [0]

    def term(t: dict | None) -> int:
        if t is None:
            return -1
        j = count[0]
        count[0] += 1
        if t.get("error"):
            terms.append(f"        _term(4, {_s(t['text'])}, 0, \"\", false);")
            return j
        kind = _TERMS[t["kind"]]
        if t["kind"] == "message":
            has_args = t["args"] is not None
            terms.append(f"        tm = _term({kind}, {_s(t['text'])}, 0, {_s(t['name'])}, "
                         f"{str(has_args).lower()});")
            terms.append(f"        _pat(tm.sender, {_pat_args(t['sender'])});")
            terms.append(f"        _pat(tm.receiver, {_pat_args(t['receiver'])});")
            terms.extend(f"        tm.args.push({a});" for a in (t["args"] or []))
        else:
            root = t.get("expr") or 0
            terms.append(f"        _term({kind}, {_s(t['text'])}, {root}, \"\", false);")
        return j

    norm_lines: list[str] = []
    for nd in ir["norms"]:
        trig, until, content = term(nd["trigger"]), term(nd["until"]), term(nd["content"])
        if nd["frozen"] is not None:
            content = -1
        deadline = nd["deadline"]
        norm_lines.append(
            f"        d = _norm({_s(nd['id'])}, {_KINDS[nd['kind']]}, "
            f"{str(nd['frozen'] is not None).lower()}, {trig}, {until}, {content}, "
            f"{str(deadline is not None).lower()}, "
            f"{scaled(deadline, 'deadline') if deadline is not None else 0}, "
            f"{str(nd['target']).lower()});")
        if nd["frozen"] is not None:
            continue
        if nd["bearer"] is not None:
            norm_lines.append("        d.hasBearer = true;")
            norm_lines.append(f"        _pat(d.bearer, {_pat_args(nd['bearer'])});")
        norm_lines.append(f"        d.severity = {_s(nd['severity'])};")
        if nd["sanction"] is not None:
            norm_lines.append("        d.hasSanction = true;")
            norm_lines.append(f"        d.sanction = {_s(nd['sanction'])};")
        if nd["reparation"] is not None:
            norm_lines.append(f"        d.reparation = {nd['reparation']};")
        if nd["reparation_id"] is not None:
            norm_lines.append("        d.hasReparationId = true;")
            norm_lines.append(f"        d.reparationId = {_s(nd['reparation_id'])};")
        if nd["transition"] is not None:
            norm_lines.append("        d.hasTransition = true;")
            norm_lines.append(f"        d.transition = {_s(nd['transition'])};")
        for kind in EFFECT_KINDS:
            norm_lines += [f"        d.{kind}.push({x});" for x in nd["effects"][kind]]
    rule_lines = []
    for r in ir["rules"]:
        when = -1 if r["error"] else term(r["when"])
        cond = -1 if r["error"] else term(r["if"])
        rule_lines.append(f"        rules.push(Rule({_s(r['creates'])}, {str(r['error']).lower()}, "
                          f"{when}, {cond}));")
    item_lines = []
    for key, fn in (("assume", "_assume"), ("guarantee", "_guarantee")):
        for it in ir["items"][key]:
            root = it["expr"] if it["expr"] is not None else 0
            item_lines.append(
                f"        {fn}({_s(it['text'])}, {str(it['temporal']).lower()}, "
                f"{str(bool(it['error'])).lower()}, {str(bool(it.get('unsupported'))).lower()}, "
                f"{root});")
    body += ["        Term storage tm;"] if any("tm = " in line for line in terms) else []
    body += terms
    if norm_lines:
        body.append("        NormDef storage d;")
    body += norm_lines
    body += rule_lines
    body += item_lines

    name = monitor_name(contract.id)
    base = BASE.read_text(encoding="utf-8").rstrip("\n")
    lines = [
        "// SPDX-License-Identifier: Apache-2.0",
        f"// Norms monitor of contract {sanitize_id(contract.id)} (Appendix F of the CADL",
        "// specification, norms 0.1). Generated by `cadl codegen -t solidity`. Do not edit.",
        "pragma solidity ^0.8.24;",
        "",
        base,
        "",
        "// The model of the contract: the constructor fills it.",
        f"contract {name} is CadlNormsMonitorBase {{",
        "    constructor() {",
        *body,
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)
