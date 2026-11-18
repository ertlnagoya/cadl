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
REGIMES_PART = Path(__file__).with_name("norms_monitor_regimes.rego")

# The lines of the library that the monitor of a contract not in force in
# some regime reads differently (Appendix A, A.7.1; F.5): the time of the
# core is the clock of the contract (input.clock), `now` and the reported
# times are times of the trace, the lifecycle state given while the
# contract was not in force is the state before its first observation, and
# `result` is the step of the part that reads the regime.
_REGIME_EDITS = [
    ("tq := dec(obs.t)\n",
     "tq := input.clock if {\n\tis_object(input.clock)\n} else := dec(obs.t)\n"),
    ("lc_before := lc.initial if {\n\tlc != null\n\tfirst\n}",
     "lc_before := lc.initial if {\n\tlc != null\n\tfirst\n\tS.lc == null\n}"),
    ('entry := {"i": n, "t": tq, "values": V, "st": lc_before}',
     'entry := {"i": n, "t": tq, "now": dec(obs.t), "values": V, "st": lc_before}'),
    ('} else := buf[p].t if {\n\tnm == "now"', '} else := buf[p].now if {\n\tnm == "now"'),
    ("\nresult := {", "\ncore_result := {"),
]


def _library(regimes: dict | None) -> str:
    text = LIBRARY.read_text(encoding="utf-8").rstrip("\n")
    if regimes is None:
        return text
    for old, new in _REGIME_EDITS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    part = REGIMES_PART.read_text(encoding="utf-8").rstrip("\n")
    data = {"regimes": regimes["regimes"], "out": regimes["out"], "initial": regimes["initial"]}
    return "\n".join([
        text, "",
        "# The regimes of the file: those that do not have the contract in force, and",
        "# the initial regime (A.7.1).",
        f"_REGIMES := json.unmarshal({_lit(json.dumps(data, ensure_ascii=True, sort_keys=True))})",
        "", part])


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


def _expr_functions(nodes: list[dict], prefix: str = "x") -> list[str]:
    out = []
    for k, node in enumerate(nodes):
        op = node["op"]

        def x(i: int, at: str = "p") -> str:
            return f"{prefix}{i}({at})"
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
        out.append(f"{prefix}{k}({arg}) := {body}")
    return out


def generate_norms_monitor_rego(sos: SoSDefinition, contract: ContractDef) -> str:
    """The Rego text of the monitor of one contract. Raises
    `NormsTargetUnsupported` for a contract that cannot be monitored."""
    check_contract_id(contract.id)
    from ..norms_ir import regime_data
    regimes = regime_data(sos, contract)
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
    lines.append(_library(regimes))
    lines.append("")
    return "\n".join(lines)


__all__ = ["generate_norms_monitor_rego", "monitor_filename", "monitor_package", "rego_ir",
           "rego_model"]


# --- Versions of a run (Appendix K, K.11; F.6) -------------------------------------

VERSIONS_PART = Path(__file__).with_name("norms_monitor_versions.rego")

# The lines of the library that the monitor with versions reads differently:
# items that start at an amendment (their start, kept in the item), the
# norms violated by the amendment (step 5 of F.5), and `result`, which is the
# part of norms_monitor_versions.rego.
_VERSION_EDITS = [
    ("keep_from := min(({n} | {k | some rec in N5; some k in rec.cands}) | {0 | undecided_temporal_item})",
     "keep_from := min(({n} | {k | some rec in N5; some k in rec.cands}) | undecided_starts)\n\n"
     "undecided_starts contains object.get(S.items.assume[i], \"start\", 0) if {\n"
     "\tsome i, md in model.items.assume\n\tmd.temporal\n\tmd.error == null\n"
     "\titems_now.assume[i].value == null\n}\n\n"
     "undecided_starts contains object.get(S.items.guarantee[i], \"start\", 0) if {\n"
     "\tsome i, md in model.items.guarantee\n\tmd.temporal\n\tmd.error == null\n"
     "\tnot md.unsupported\n\titems_now.guarantee[i].value == null\n}"),
    ('viol := [i | some i, _ in model.norms; N3[i].state == "violated"; N2[i].state != "violated"]',
     'viol := array.concat(amend_viol, [i | some i, _ in model.norms; N3[i].state == "violated"; '
     'N2[i].state != "violated"])\n\n'
     '# the norms violated by an amendment at this observation (F.6), first\n'
     'amend_viol := [i | some id in object.get(input, "amend_viol", []); some i, nd in model.norms; '
     'nd.id == id]'),
    ("\nresult := {", "\ncore_result := {"),
    # the acts of powers (step 2) follow the norms of the instance in their
    # order, each with the definition it has (an instance kept from an
    # earlier version keeps the definition of that version: a ghost)
    ("""event_power_texts := {model.terms[model.norms[i].content].text |
	some i in powers
	model.terms[model.norms[i].content].kind == "event"
}""", """act_terms := [power_term(e) | some e in input.power_seq] if {
	is_array(input.power_seq)
} else := [model.norms[i].content | some i in powers]

power_term(e) := model.norms[e.cur].content if {
	has(e, "cur")
} else := model.ghosts[e.ghost].term

event_power_texts := {model.terms[j].text |
	some j in act_terms
	model.terms[j].kind == "event"
}"""),
    ("""msg_act_cands := [{"c": model.terms[model.norms[p].content].text, "perf": m.from, "d": desc(m)} |
	some p in powers
	model.terms[model.norms[p].content].kind == "message"
	some m in msg_matches[model.norms[p].content]
]""", """msg_act_cands := [{"c": model.terms[j].text, "perf": m.from, "d": desc(m)} |
	some j in act_terms
	model.terms[j].kind == "message"
	some m in msg_matches[j]
]"""),
]


def versions_model(sos_list: list, contract_id: str) -> tuple:
    """(the models of the contract in each version, the data of the versions:
    names and the transitional rule of the contract in each). Raises
    `NormsTargetUnsupported` for what the monitor cannot follow."""
    from ...norms_eval import NormsEvalError, VersionChain, version_info
    try:
        chain = VersionChain([version_info(s) for s in sos_list])
    except NormsEvalError as e:
        raise NormsTargetUnsupported([f"the versions of the run: {e}"]) from None
    models, irs, rules = [], [], []
    for info in chain.infos:
        c = next((c for c in info.sos.contracts if c.id == contract_id), None)
        if c is None:
            raise NormsTargetUnsupported([
                f"version {info.name} has no contract {contract_id}; the monitor follows "
                f"the contract through every version of the run"])
        ir = rego_ir(info.sos, c)
        if any(it.get("unsupported") for it in ir["items"]["guarantee"]):
            raise NormsTargetUnsupported([
                f"version {info.name} has a guarantee: item that the Rego monitor leaves "
                f"out; with versions the items continue by their text across versions, "
                f"so every item must be monitored"])
        m = rego_model(ir)
        defs = {d.id: d for d in c.norms}
        for nd in m["norms"]:
            d = defs[nd["id"]]
            nd["on_amendment"] = d.on_amendment or "carry_over"
            nd["transform_to"] = d.transform_to
            nd["content_src"] = (d.content or "").strip()
        models.append(m)
        irs.append(ir)
        rule, states = info.rule_for(contract_id)
        rules.append({"rule": rule, "states": list(states or [])})
    # the powers of the other versions, as ghosts: an instance kept from that
    # version (in a final state) still makes acts with its content (F.6)
    for v, m in enumerate(models):
        m["ghosts"] = []
        for u, mu in enumerate(models):
            if u == v:
                continue
            for nd in mu["norms"]:
                if nd["kind"] != "power":
                    continue
                term = dict(mu["terms"][nd["content"]])
                term["xv"] = u            # its arguments are predicates of version u
                m["terms"].append(term)
                m["ghosts"].append({"u": u, "id": nd["id"], "term": len(m["terms"]) - 1})
    return chain, models, irs, rules


def generate_norms_monitor_rego_versions(sos: SoSDefinition, contract: ContractDef,
                                         versions: list) -> str:
    """The Rego monitor of one contract that follows the versions of a run
    (Appendix K, K.11): `sos` and `versions` (parsed files) are the versions;
    they form one sequence by previous:. When a version binds regimes, the
    monitor also follows the current regime (K.11, "Versions and regimes",
    from cadl 0.3.36; norms_monitor_regimes_versions.rego)."""
    check_contract_id(contract.id)
    chain, models, irs, rules = versions_model([sos, *versions], contract.id)
    # versions with regime bindings (K.11, "Versions and regimes", from cadl 0.3.36)
    combined = any(info.regimes is not None for info in chain.infos)
    lines = [
        f"# Norms monitor of contract {contract.id} (Appendix F of the CADL specification,",
        "# norms 0.1) that follows the versions of a run (Appendix K, K.11): versions "
        + ", ".join(chain.names()) + ".",
        "# Generated by `cadl codegen -t opa --version ...`. Do not edit.",
        "#",
        "# A step function: query `result` with {\"state\": S, \"observation\": O}",
        "# (S null for the first observation; O may name the version in force with",
        "# \"version\""
        + (" and the current regime with \"regime\"" if combined else "")
        + ") or {\"state\": S, \"finish\": true}; store result.state.",
        f"package {monitor_package(contract.id)}",
        "",
        "import rego.v1",
        "",
        "# The contract in each version, as data (read with json.unmarshal).",
        "models := [" + ", ".join(
            f"json.unmarshal({_lit(json.dumps(m, ensure_ascii=True, allow_nan=False, sort_keys=True))})"
            for m in models) + "]",
        "",
        "# The version whose body the core runs (the wrapper below sets it).",
        "default cv := 0",
        "",
        "cv := input.cv if is_number(input.cv)",
        "",
        "model := models[cv]",
        "",
        "_VERSIONS := json.unmarshal("
        + _lit(json.dumps({"names": chain.names(), "rules": rules}, ensure_ascii=True,
                          sort_keys=True)) + ")",
        "",
    ]
    pred, args, content, items = [], [], [], []
    for v, (ir, model) in enumerate(zip(irs, models)):
        px = f"x{v}_"
        lines.append(f"# The predicates of version {chain.infos[v].name}.")
        lines += _expr_functions(ir["exprs"], px)
        lines.append("")
        for j, t in enumerate(model["terms"]):
            if t["error"]:
                continue
            if t["kind"] == "predicate":
                pred.append(f"pred_now[{j}] := truth({px}{t['root']}(last)) if {{\n\tcv == {v}\n}}")
                pred.append(f"pred_prev[{j}] := truth({px}{t['root']}(last - 1)) if {{\n"
                            f"\tcv == {v}\n\tlast >= 1\n}}")
            elif t["kind"] == "message" and t["args"] is not None:
                xp = f"x{t['xv']}_" if "xv" in t else px
                vals = ", ".join(f"{xp}{a}(last)" for a in t["args"])
                args.append(f"msg_args[{j}] := [{vals}] if {{\n\tcv == {v}\n}}")
        for i, nd in enumerate(model["norms"]):
            c = nd["content"]
            if model["terms"][c]["kind"] == "predicate":
                root = model["terms"][c]["root"]
                content.append(f"content_vals[{i}] := {{k: c | some k in content_set({i}); "
                               f"c := content_of(truth({px}{root}(pos(k))))}} if {{\n"
                               f"\tcv == {v}\n}}")
        for key, prefix in (("assume", "a"), ("guarantee", "g")):
            for i, it in enumerate(model["items"][key]):
                if it["error"] or it["unsupported"]:
                    continue
                if it["temporal"]:
                    items.append(f"item_val[\"{prefix}{i}\"] := truth({px}{it['root']}(pos(st))) if {{\n"
                                 f"\tcv == {v}\n\tst := object.get(S.items.{key}[{i}], \"start\", 0)\n"
                                 f"\tst >= base\n}}")
                else:
                    items.append(f"item_val[\"{prefix}{i}\"] := truth({px}{it['root']}(last)) if {{\n"
                                 f"\tcv == {v}\n}}")
    lines.append("# The terms and items, evaluated at this observation.")
    for name, rules_ in (("pred_now", [r for r in pred if r.startswith("pred_now")]),
                         ("pred_prev", [r for r in pred if r.startswith("pred_prev")]),
                         ("msg_args", args), ("content_vals", content), ("item_val", items)):
        lines += rules_ or [f"{name}[-1] := null if {{\n\tfalse\n}}"]
    text = LIBRARY.read_text(encoding="utf-8").rstrip("\n")
    for old, new in _VERSION_EDITS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    vpart = VERSIONS_PART.read_text(encoding="utf-8").rstrip("\n")
    if combined:
        # the core runs on the clock of the contract (the edits of the regime
        # part but the last, which the versions made already); the part for
        # versions is called by the part for regimes
        for old, new in _REGIME_EDITS[:-1]:
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        for old, new in _COMBINED_EDITS:
            assert vpart.count(old) == 1, old
            vpart = vpart.replace(old, new)
    # the amendments of one observation, one after the other (Rego has no
    # recursion): at most one per version that takes effect, and a waiting switch
    chain_fn = ["", "# the amendments along the path of versions of one observation",
                "w_chain(st0, path, tt, k) := s{0} if {{".format(len(models)), "\ts0 := st0"]
    for i in range(1, len(models) + 1):
        chain_fn.append(f"\ts{i} := w_amend_at(s{i - 1}, path, {i}, tt, k)")
    chain_fn.append("}")
    lines += ["", text, "", vpart]
    lines += chain_fn + [""]
    if combined:
        data = []
        for info in chain.infos:
            b = info.regimes
            data.append(None if b is None else {
                "regimes": list(b.regimes), "initial": b.initial,
                "out": [r for r in b.regimes if not b.in_force(contract.id, r)]})
        lines += [
            "# The regimes of each version (null: the version has no regimes:), those",
            "# that do not have the contract in force (\"out\"), and the initial regime.",
            "_RV := json.unmarshal("
            + _lit(json.dumps({"versions": data}, ensure_ascii=True, sort_keys=True)) + ")",
            "", REGIMES_VERSIONS_PART.read_text(encoding="utf-8").rstrip("\n"), ""]
    return "\n".join(lines)


REGIMES_VERSIONS_PART = Path(__file__).with_name("norms_monitor_regimes_versions.rego")

# The lines of norms_monitor_versions.rego that the monitor with versions and
# regimes reads differently: the part for regimes (norms_monitor_regimes_versions.rego)
# gives the version in force ("vk"), the versions to amend to ("vpath") and the
# waiting switch ("vpending"), which it follows also while the contract is not
# in force, and the core state of the first observation ("core0"); the
# amendments are made on the clock of the contract; `result` is `v_result`.
_COMBINED_EDITS = [
    ('w_extra := object.remove(input, ["state", "observation", "finish", "cv", "amend_viol"])',
     'w_extra := object.remove(input, ["state", "observation", "finish", "cv", "amend_viol", '
     '"vk", "vpath", "vpending", "core0", "after", "fsb"])'),
    ("w_k := vindex(wobs.version) if {",
     "w_k := input.vk if {\n\tis_number(input.vk)\n} else := vindex(wobs.version) if {"),
    ("w_path := w_walk.path",
     "w_path := input.vpath if {\n\tis_array(input.vpath)\n} else := w_walk.path\n\n"
     "w_pend_out := input.vpending if {\n\t\"vpending\" in object.keys(input)\n"
     "} else := w_walk.pending\n\n"
     "w_tt := input.clock if {\n\tis_object(input.clock)\n} else := dec(wobs.t)"),
    ('"state": null, "observation": cobs, "cv": w_k}',
     '"state": object.get(input, "core0", null), "observation": cobs, "cv": w_k}'),
    ("st := w_chain(st0, w_path, dec(wobs.t), W.core.n)",
     "st := w_chain(st0, w_path, w_tt, W.core.n)"),
    ("""	r := w_after(r0, g, st.uids, st.retired, st.items, w_walk.pending, w_before,
		{"current": w_k_now, "follows": g, "pending": w_walk.pending, "uid": st.uid})""",
     """	r := w_after(r0, g, st.uids, st.retired, st.items, w_pend_out, w_before,
		{"current": w_k_now, "follows": g, "pending": w_pend_out, "uid": st.uid})"""),
    ("\nresult := {", "\nv_result := {"),
]
