"""Norms monitor of the OPA target (Appendix F, F.5; Appendix D).

For each contract of a file that declares ``norms: 0.1``,
`cadl codegen -t opa` writes ``norms_monitor_<contract>.rego``: a Rego
policy whose rule ``result`` is the step function of a monitor of the
norms, the counts-as rules and the ``assume:`` / ``guarantee:`` items of
the contract. Rego keeps no state: the host passes the state the previous
step returned together with one observation, and stores the new state
the step returns (see Appendix D for its schema).

The file consists of

1. ``model``: the contract as data (`norms_ir.build_ir`, with the
   triggers, untils, contents and counts-as terms in one list): one JSON
   text, written as a Rego string with `json.dumps` and read with
   ``json.unmarshal``;
2. the predicates of the contract as Rego functions ``x0``, ``x1``, ...
   (one per node of the expression tree) and the rules ``pred_now``,
   ``pred_prev``, ``msg_args``, ``content_vals`` and ``item_val`` that
   evaluate them. Every literal from the .cadl file is written with
   `json.dumps` (a JSON string is a Rego string); names of functions and
   keys are numbers;
3. the library ``norms_monitor_lib.rego``, the same for every contract:
   the order of one observation of F.5, as `cadl norms-eval` implements it.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from ...ast_nodes import ContractDef, SoSDefinition
from ..norms_ir import (
    NormsTargetUnsupported, apply_limits, build_ir, check_contract_id, decimal_parts, subtree,
)

LIBRARY = Path(__file__).with_name("norms_monitor_lib.rego")


def monitor_filename(contract_id: str) -> str:
    from ..emitter import snake_case
    return f"norms_monitor_{snake_case(contract_id)}.rego"


def monitor_package(contract_id: str) -> str:
    from ..emitter import snake_case
    return f"cadl.norms_monitor.{snake_case(contract_id)}"


def _q(value: Any) -> dict:
    """An exact decimal number as the Rego library writes it."""
    n, k = decimal_parts(value)
    return {"$q": [n, k]}


# The scales (decimals) the library computes with: an input number has at
# most INPUT_SCALE decimals, and pow10 is defined up to MAX_SCALE.
INPUT_SCALE = 400
MAX_SCALE = 4095


def _scale(nodes: list[dict], k: int) -> int:
    """The largest number of decimals the value of node k can have."""
    nd = nodes[k]
    op = nd["op"]
    if op == "lit":
        v = nd["v"]
        if isinstance(v, bool) or not isinstance(v, (int, float, Fraction)):
            return 0
        return decimal_parts(v)[1]
    if op in ("name", "mem", "req", "sym"):
        return INPUT_SCALE
    if op == "bin" and nd["o"] == "*":
        return _scale(nodes, nd["l"]) + _scale(nodes, nd["r"])
    if op == "bin":
        return max(_scale(nodes, nd["l"]), _scale(nodes, nd["r"]))
    if op == "in":
        return max(_scale(nodes, i) for i in [nd["l"], *nd["items"]])
    return 0


def _problem(nodes: list[dict], root: int) -> str | None:
    """What the Rego monitor does not evaluate: division, whose result is
    not always a decimal number, and a product whose exact value could have
    more than MAX_SCALE decimals (the monitor computes exactly)."""
    for k in subtree(nodes, root):
        if nodes[k]["op"] == "bin" and nodes[k]["o"] == "/":
            return ("the operator / (the Rego monitor computes with exact decimal numbers, "
                    "and a quotient is not always one)")
        if _scale(nodes, k) > MAX_SCALE:
            return (f"a product whose exact value can have more than {MAX_SCALE} decimals "
                    f"(each number has at most {INPUT_SCALE})")
    return None


def rego_ir(sos: SoSDefinition, contract: ContractDef) -> dict:
    """`build_ir` with what the Rego monitor cannot evaluate on top."""
    return apply_limits(build_ir(sos, contract), _problem)


def _num(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        return value
    return _q(value)


def _lit(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def rego_model(ir: dict) -> dict:
    """The data of the monitor: the IR with its terms in one list."""
    terms: list[dict] = []

    def term(t: dict | None) -> int | None:
        if t is None:
            return None
        entry = {k: v for k, v in t.items() if k != "expr"}
        entry["error"] = bool(t.get("error"))
        entry["root"] = t.get("expr")
        terms.append(entry)
        return len(terms) - 1

    norms = []
    for nd in ir["norms"]:
        entry = {k: v for k, v in nd.items() if k not in ("trigger", "until", "content")}
        if entry["deadline"] is not None:
            entry["deadline"] = _q(entry["deadline"])
        for key in ("trigger", "until", "content"):
            entry[key] = term(nd[key])
        norms.append(entry)
    rules = []
    for r in ir["rules"]:
        rules.append({"creates": r["creates"], "error": r["error"],
                      "when": term(r["when"]), "if": term(r["if"])})
    items = {key: [{"text": it["text"], "temporal": it["temporal"], "error": it["error"],
                    "unsupported": bool(it.get("unsupported")), "root": it["expr"]}
                   for it in ir["items"][key]] for key in ("assume", "guarantee")}
    return {"contract": ir["contract"], "actors": ir["actors"], "lifecycle": ir["lifecycle"],
            "terms": terms, "norms": norms, "rules": rules, "items": items}


def _expr_functions(nodes: list[dict]) -> list[str]:
    out = []
    for k, node in enumerate(nodes):
        op = node["op"]

        def x(i: int, at: str = "p") -> str:
            return f"x{i}({at})"
        if op == "lit":
            body, arg = _lit(_num(node["v"])), "_"
        elif op == "name":
            body, arg = f"name_val({_lit(node['n'])}, p)", "p"
        elif op == "sym":
            body, arg = f"{{\"$sym\": actor_text({_lit(node['a'])}, {_lit(node['i'])})}}", "_"
        elif op == "mem":
            body, arg = f"mem_val({_lit(node['a'])}, {_lit(node['i'])}, {_lit(node['m'])}, p)", "p"
        elif op == "req":
            body, arg = f"req_val({_lit(node['path'])})", "_"
        elif op == "not":
            body, arg = f"not3(truth({x(node['x'])}))", "p"
        elif op in ("and", "or"):
            body, arg = f"{op}3(truth({x(node['l'])}), truth({x(node['r'])}))", "p"
        elif op == "bin":
            body, arg = f"binop({_lit(node['o'])}, {x(node['l'])}, {x(node['r'])})", "p"
        elif op == "in":
            items = ", ".join(x(i) for i in node["items"])
            body, arg = f"in3({x(node['l'])}, [{items}])", "p"
        elif op in ("within", "throughout"):
            d = _lit(_q(node["d"]))
            dec = "true" if op == "within" else "false"
            body = (f"temporal([truth({x(node['x'], 'j')}) | some j in window(p, {d})], "
                    f"over_v(p, {d}), {dec})")
            arg = "p"
        elif op == "until":
            d = _lit(_q(node["d"]))
            body = (f"until3([truth({x(node['q'], 'j')}) | some j in window(p, {d})], "
                    f"[truth({x(node['p'], 'j')}) | some j in window(p, {d})], over_v(p, {d}))")
            arg = "p"
        else:                                   # pragma: no cover - build_ir makes no other
            raise NormsTargetUnsupported([f"node {op}"])
        out.append(f"x{k}({arg}) := {body}")
    return out


def generate_norms_monitor_rego(sos: SoSDefinition, contract: ContractDef) -> str:
    """The Rego text of the monitor of one contract. Raises
    `NormsTargetUnsupported` for a contract that cannot be monitored."""
    check_contract_id(contract.id)
    ir = rego_ir(sos, contract)
    model = rego_model(ir)
    nodes = ir["exprs"]
    lines = [
        f"# Norms monitor of contract {contract.id} (Appendix F of the CADL specification,",
        "# norms 0.1). Generated by `cadl codegen -t opa`. Do not edit.",
        "#",
        "# A step function: query `result` with {\"state\": S, \"observation\": O}",
        "# (S null for the first observation) or {\"state\": S, \"finish\": true};",
        "# store result.state for the next step. See Appendix D for the schema.",
        f"package {monitor_package(contract.id)}",
        "",
        "import rego.v1",
        "",
        "# The contract as data: one JSON text (read with json.unmarshal, so that",
        "# the type checker of OPA does not hold its shape against the library).",
        f"model := json.unmarshal({_lit(json.dumps(model, ensure_ascii=True, allow_nan=False, sort_keys=True))})",
        "",
        "# The predicates of the contract: x<n>(p) is the value of node n at",
        "# position p of the buffer of observations.",
    ]
    lines += _expr_functions(nodes)
    lines.append("")
    lines.append("# The terms and items, evaluated at this observation.")
    pred, args, content, items = [], [], [], []
    for j, t in enumerate(model["terms"]):
        if t["error"]:
            continue
        if t["kind"] == "predicate":
            pred.append(f"pred_now[{j}] := truth(x{t['root']}(last))")
            pred.append(f"pred_prev[{j}] := truth(x{t['root']}(last - 1)) if {{\n\tlast >= 1\n}}")
        elif t["kind"] == "message" and t["args"] is not None:
            vals = ", ".join(f"x{a}(last)" for a in t["args"])
            args.append(f"msg_args[{j}] := [{vals}]")
    for i, nd in enumerate(model["norms"]):
        c = nd["content"]
        if model["terms"][c]["kind"] == "predicate":
            root = model["terms"][c]["root"]
            content.append(f"content_vals[{i}] := {{k: c | some k in content_set({i}); "
                           f"c := content_of(truth(x{root}(pos(k))))}}")
    for key, prefix in (("assume", "a"), ("guarantee", "g")):
        for i, it in enumerate(model["items"][key]):
            if it["error"] or it["unsupported"]:
                continue
            if it["temporal"]:
                items.append(f"item_val[\"{prefix}{i}\"] := truth(x{it['root']}(0)) if {{\n"
                             f"\tbase == 0\n}}")
            else:
                items.append(f"item_val[\"{prefix}{i}\"] := truth(x{it['root']}(last))")
    for name, rules in (("pred_now", [r for r in pred if r.startswith("pred_now")]),
                        ("pred_prev", [r for r in pred if r.startswith("pred_prev")]),
                        ("msg_args", args), ("content_vals", content), ("item_val", items)):
        lines += rules or [f"{name}[-1] := null if {{\n\tfalse\n}}"]
    lines.append("")
    library = LIBRARY.read_text(encoding="utf-8")
    lines.append(library.rstrip("\n"))
    lines.append("")
    return "\n".join(lines)


__all__ = ["generate_norms_monitor_rego", "monitor_filename", "monitor_package", "rego_ir",
           "rego_model"]
