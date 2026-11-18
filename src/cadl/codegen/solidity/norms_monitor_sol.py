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
IR refuses (quantifiers, aggregates, wildcard references), nor a
temporal operator inside another one outside the content of a norm.

The content of a norm with temporal operators goes to the queues of
CadlNormsWindows (one operator), to the segment tree of CadlNormsMulti
(two to five, or a nested `within` / `throughout` fed in the order of the
observations), or, from cadl 0.3.32, to `General` in CadlNormsMulti, which
decides it per observation (six or more operators, `until` in a nesting,
an operand that combines a nested operator with other parts).

From cadl 0.3.36, `generate_norms_monitor_sol_versions` writes the monitor
of a contract that follows the versions of a run (Appendix K, K.11;
`cadl codegen -t solidity FILE --version OTHER ...`): the constructor fills
the definitions of every version, the base is read through the norms and
items of the instance (`_versions_base`), and norms_monitor_versions.sol
adds the version in force (set by the orchestrator) and the amendment of
F.6.
"""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Any

from ...ast_nodes import ContractDef, SoSDefinition
from ..emitter import sanitize_id
from ..norms_ir import (
    EFFECT_KINDS, NormsTargetUnsupported, apply_limits, build_ir, check_contract_id,
)

BASE = Path(__file__).with_name("norms_monitor_base.sol")
REGIMES_PART = Path(__file__).with_name("norms_monitor_regimes.sol")

# The lines of the base that the monitor of a contract not in force in some
# regime reads differently (Appendix A, A.7.1; F.5): the base runs on the
# clock of the contract, `now` and the times of the events are times of the
# trace (the clock plus `shift`), the lifecycle state given while the
# contract was not in force is the state before its first observation, and
# observe, finish and nextDeadline are those of norms_monitor_regimes.sol.
_REGIME_EDITS = [
    ("    int256[] obsT;\n",
     "    int256[] obsT;\n    int256[] obsNow;         // the times of the trace (now); obsT: the clock\n"),
    ('if (h == keccak256("now")) return Val(K_N, data.obsT[p], "");',
     'if (h == keccak256("now")) return Val(K_N, data.obsNow[p], "");'),
    ("    int256 failedAt;\n}",
     "    int256 failedAt;\n    int256 shift;              // the time of the trace minus the clock\n}"),
    ("emit InstitutionalEvent(buf[i].name, buf[i].performer, buf[i].source, t);",
     "emit InstitutionalEvent(buf[i].name, buf[i].performer, buf[i].source, t + m.shift);"),
    ("emit PowerExercised(d.id, act.performer, act.desc, t);",
     "emit PowerExercised(d.id, act.performer, act.desc, t + m.shift);"),
    ("emit Unauthorised(act.desc, act.performer, t);",
     "emit Unauthorised(act.desc, act.performer, t + m.shift);"),
    ("repActivated, transition, t);", "repActivated, transition, t + m.shift);"),
    ("ns[i], nv[i], t);", "ns[i], nv[i], t + m.shift);"),
    ("emit ItemChanged(false, i + 1, k, am[i], t);", "emit ItemChanged(false, i + 1, k, am[i], t + m.shift);"),
    ("emit ItemChanged(true, i + 1, k, gm[i], t);", "emit ItemChanged(true, i + 1, k, gm[i], t + m.shift);"),
    ("        m.data.obsT.push(t);\n",
     "        m.data.obsT.push(t);\n        m.data.obsNow.push(t + m.shift);\n"),
    ("string memory before = n == 0 ? m.lcInitial : m.lc;",
     "string memory before = n == 0 && bytes(m.lc).length == 0 ? m.lcInitial : m.lc;"),
    ("bool hasState, string calldata state, bool end) external onlyReporter {\n"
     "        require(t >= -MAX_T",
     "bool hasState, string calldata state, bool end) internal {\n        require(t >= -MAX_T"),
    ("    function observe(int256 t, ValIn[] calldata values, Msg[] calldata msgs,\n"
     "                     bool hasState, string calldata state, bool end) internal {",
     "    function observeCore(int256 t, ValIn[] calldata values, Msg[] calldata msgs,\n"
     "                         bool hasState, string calldata state, bool end) internal {"),
    ("function finish() external onlyReporter {", "function finishCore() internal {"),
    ("function nextDeadline() external view returns (bool has, int256 due) {",
     "function nextDeadlineCore() internal view returns (bool has, int256 due) {"),
]

# What the regime part adds to the runtime code of the monitor (bytes),
# measured with solc 0.8.24 and 0.8.37 and rounded up.
REGIMES_RUNTIME = 3300


def _base(regimes: dict | None) -> str:
    text = BASE.read_text(encoding="utf-8").rstrip("\n")
    if regimes is None:
        return text
    for old, new in _REGIME_EDITS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text + "\n\n" + REGIMES_PART.read_text(encoding="utf-8").rstrip("\n")
SCALE = 10 ** 6
_OPS = {"lit": 0, "name": 1, "sym": 2, "mem": 3, "req": 4, "not": 5, "and": 6, "or": 7,
        "bin": 8, "in": 9, "within": 10, "throughout": 11, "until": 12}
_BOPS = {"==": 0, "!=": 1, "<": 2, "<=": 3, ">": 4, ">=": 5, "+": 6, "-": 7}
_KINDS = {"obligation": 0, "permission": 1, "prohibition": 2, "power": 3}
_TERMS = {"event": 1, "message": 2, "predicate": 3}
_INT256 = 2 ** 255


# EIP-3860: the creation code of a contract is at most 49152 bytes.
INITCODE_LIMIT = 49152
# EIP-170: the runtime code of a contract (and of each library) is at most
# 24576 bytes.
RUNTIME_LIMIT = 24576
# The solc versions the generated file has been compiled and run with.
SOLC_CHECKED = ("0.8.24", "0.8.37")
# Upper estimates of the runtime code of the contract and the libraries:
# they do not depend on the CADL contract (its model is data written by
# the constructor). Measured with the versions above, the optimizer with 1
# and with 200 runs, plus 3 %; tests/test_norms_monitor_targets.py checks
# them against solc.
RUNTIME_ESTIMATE = {"monitor": 17250, "CadlNormsValues": 7810, "CadlNormsEval": 11550,
                    "CadlNormsItems": 2370, "CadlNormsWindows": 5490, "CadlNormsActs": 12200,
                    "CadlNormsMulti": 16270}


def estimated_initcode(text: str) -> int:
    """An upper estimate of the creation code of the generated contract
    (solc 0.8.24 or later, with the optimizer, 1 or 200 runs): the runtime code and the constructor,
    which grows with its statements and its texts. Fitted to compiled
    monitors (1.8 to 3.6 x 10^4 bytes) and kept above each of them."""
    ctor = text.rsplit("    constructor(address reporter_", 1)[1]
    statements = sum(1 for line in ctor.splitlines() if line.strip().endswith(";"))
    texts = sum(len(h) // 2 for h in re.findall(r'hex"([0-9a-f]*)"', ctor))
    # fitted with --optimize-runs 1; the factor covers the default of 200 runs,
    # whose code is up to 6 % larger (the generated files ask for 1 run)
    # the regime part (norms_monitor_regimes.sol) adds to the runtime code
    base = 17500 + (REGIMES_RUNTIME if " is CadlNormsMonitorRegimes {" in text else 0)
    versions = [VERSIONS_RUNTIME if " is CadlNormsMonitorVersions {" in text else None,
                VERSIONS_RUNTIME + REGIMES_VERSIONS_RUNTIME
                if " is CadlNormsMonitorRegimesVersions {" in text else None]
    added = next((x for x in versions if x is not None), None)
    if added is not None:
        # a monitor with versions (norms_monitor_versions.sol): fitted to
        # compiled monitors of two and three versions, with and without
        # regimes (3.2 to 4.3 x 10^4 bytes), and kept 3 % above each of them
        return int(1.03 * (RUNTIME_ESTIMATE["monitor"] + added + 58 * statements
                           + 1.3 * texts + 3000))
    return int(1.08 * (base + 72 * statements + 1.3 * texts))


def monitor_name(contract_id: str) -> str:
    return f"{sanitize_id(contract_id)}NormsMonitor"


def _s(text: Any) -> str:
    """A text of the .cadl file as a Solidity expression of type string."""
    data = str(text).encode("utf-8", errors="surrogatepass")
    return f'string(hex"{data.hex()}")' if data else '""'


def scaled(value: Any, where: str) -> int:
    """A number as an int256 times SCALE; refuses what the fixed point cannot hold."""
    if isinstance(value, float):
        value = Fraction(repr(value))
    v = Fraction(value) * SCALE
    if v.denominator != 1:
        raise NormsTargetUnsupported([
            f"{where}: the number {float(value)!r} has more than 6 decimals (the Solidity "
            f"monitor uses fixed-point numbers with 6 decimals)"])
    v = int(v)
    if not -_INT256 < v < _INT256:
        raise NormsTargetUnsupported([f"{where}: the number {float(value)!r} is too large"])
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


def _children(node: dict) -> list[int]:
    op = node["op"]
    if op in ("not", "within", "throughout"):
        return [node["x"]]
    if op in ("and", "or", "bin"):
        return [node["l"], node["r"]]
    if op == "in":
        return [node["l"], *node["items"]]
    if op == "until":
        return [node["p"], node["q"]]
    return []


def _subtree(nodes: list[dict], root: int) -> list[int]:
    out, todo = [], [root]
    while todo:
        k = todo.pop()
        out.append(k)
        todo.extend(_children(nodes[k]))
    return out


def _temporal_inside(nodes: list[dict], root: int) -> bool:
    return any(nodes[k]["op"] in ("within", "throughout", "until") for k in _subtree(nodes, root))


def _problem(nodes: list[dict], root: int, content: bool = False) -> str | None:
    """Why the Solidity monitor cannot evaluate a predicate, or None.
    `content`: the predicate is the content of a norm, where a temporal
    operator inside another one is checked by `_nesting`."""
    for k in _subtree(nodes, root):
        nd = nodes[k]
        if nd["op"] == "bin" and nd["o"] not in _BOPS:
            return (f"the operator {nd['o']} (the Solidity monitor uses fixed-point numbers "
                    f"and supports + and - only)")
        if not content and nd["op"] in ("within", "throughout", "until") and any(
                _temporal_inside(nodes, c) for c in _children(nd)):
            return ("a temporal operator inside another one outside the content of a norm "
                    "(the Solidity monitor scans such a window once, which needs an operand "
                    "without a temporal operator)")
        try:
            if nd["op"] == "lit" and isinstance(nd["v"], (int, float, Fraction)) \
                    and not isinstance(nd["v"], bool):
                scaled(nd["v"], "a literal")
            if nd["op"] in ("within", "throughout", "until"):
                scaled(nd["d"], "a duration")
        except NormsTargetUnsupported as e:
            return e.reasons[0]
    return None


def _temporal_nodes(nodes: list[dict], root: int) -> list[int]:
    return [k for k in _subtree(nodes, root)
            if nodes[k]["op"] in ("within", "throughout", "until")]


def _top_temporal(nodes: list[dict], root: int) -> list[int]:
    """The temporal operators of a predicate that are not inside another one."""
    k = root
    if nodes[k]["op"] in ("within", "throughout", "until"):
        return [k]
    return [x for c in _children(nodes[k]) for x in _top_temporal(nodes, c)]


# The most temporal operators in one content that the segment tree decides
# (D.5). CadlNormsEval.table keeps the 4^m values of a content in 4^(m - 4)
# words of 256 bits for m > 4, and a node of the segment tree has
# 5^(m - 3) storage words for m > 3: 25 for five operators. With six, a
# node would have 125 words and an observation would cost about five times
# as much as with five (up to 10^7 gas measured), of the order of the gas
# limit of a block; from cadl 0.3.32 such a content is decided per
# observation (General), whose cost grows linearly with the operators.
MAX_TEMPORAL = 5


def _nesting(nodes: list[dict], k: int) -> tuple[dict | None, str | None]:
    """A temporal operator k of a content with a temporal operator inside:
    its record for `_nested` (the outer operator and its levels, the first
    over an operand p without a temporal operator), or why the segment
    tree cannot decide it (the content is then decided per observation)."""
    outer = nodes[k]
    if outer["op"] == "until":
        return None, ("until with a temporal operator in an operand (the decided values of "
                      "such an operand do not reach until in the order of the observations, "
                      "see D.5)")
    levels = []                      # from the outer operator inwards: (mode, d, negated)
    x = outer["x"]
    while True:
        negations = 0
        while nodes[x]["op"] == "not":
            x, negations = nodes[x]["x"], negations + 1
        inner = nodes[x]
        if inner["op"] not in ("within", "throughout", "until"):
            return None, ("an operand of within or throughout that has a temporal operator and "
                          "is more than that operator or its negation (its value can be decided "
                          "before that of an earlier observation, and the decided values can "
                          "alternate along the candidates of one observation, see D.5)")
        if inner["op"] == "until":
            return None, ("until inside within or throughout (until decides a later observation "
                          "before an earlier one, see D.5)")
        levels.append((inner["op"], inner["d"], negations % 2 == 1))
        if not _temporal_inside(nodes, inner["x"]):
            p = inner["x"]
            break
        x = inner["x"]
    # each level reads the one inside it, negated when NOT stands between them
    negs = [neg for _, _, neg in levels]
    return {"node": k, "outer": _WINDOW_MODES[outer["op"]], "d": outer["d"], "p": p,
            "oneg": negs[0],
            "levels": [(_WINDOW_MODES[op], negs[j + 1] if j + 1 < len(levels) else False, d)
                       for j, (op, d, _) in enumerate(levels)][::-1]}, None


def _content_roots(ir: dict) -> list[int]:
    """The roots of the contents of norms that have a temporal operator."""
    out = []
    for nd in ir["norms"]:
        c = nd.get("content")
        if (c and c.get("kind") == "predicate" and not c.get("error")
                and _temporal_nodes(ir["exprs"], c["expr"])):
            out.append(c["expr"])
    return out


def temporal_names(ir: dict, skip: set = frozenset()) -> list[str]:
    """The bare names read inside a temporal operator, and anywhere in the
    content of a norm with one (the rest of such a content is decided at
    the observation of a candidate), which can be a quantity or a symbol
    (not `now`, nor `state` with a lifecycle)."""
    nodes = ir["exprs"]
    roots = [k for k, nd in enumerate(nodes) if k not in skip
             and nd["op"] in ("within", "throughout", "until")] + _content_roots(ir)
    names = set()
    for k in roots:
        for j in _subtree(nodes, k):
            if nodes[j]["op"] == "name":
                names.add(nodes[j]["n"])
    names.discard("now")
    if ir["lifecycle"] is not None:
        names.discard("state")
    return sorted(names)


def _path_to(nodes: list[dict], root: int, target: int) -> list[int] | None:
    if root == target:
        return [root]
    for c in _children(nodes[root]):
        p = _path_to(nodes, c, target)
        if p is not None:
            return [root] + p
    return None


def solidity_ir(sos: SoSDefinition, contract: ContractDef) -> dict:
    """`build_ir` with what the Solidity monitor cannot evaluate on top:
    a guarantee item is marked unsupported (left out), anything else
    refuses the contract."""
    ir = build_ir(sos, contract)
    contents = {nd["content"]["expr"] for nd in ir["norms"]
                if nd.get("content") and nd["content"].get("kind") == "predicate" and not nd["content"].get("error")}
    ir = apply_limits(ir, lambda nodes, root: _problem(nodes, root, root in contents))

    nodes = ir["exprs"]
    for nd in ir["norms"]:
        c = nd.get("content")
        if not (c and c.get("kind") == "predicate" and not c.get("error")):
            continue
        ops = sorted(_top_temporal(nodes, c["expr"]))
        if not ops:
            continue
        specs, nested, general = [], False, len(ops) > MAX_TEMPORAL
        for k in ops:
            if general:
                break
            if any(_temporal_inside(nodes, ch) for ch in _children(nodes[k])):
                spec, why = _nesting(nodes, k)
                if why:
                    # until in a nesting, or an operand that combines a
                    # nested operator with other parts (from cadl 0.3.32)
                    general = True
                    break
                specs.append(spec)
                nested = True
            else:
                specs.append({"node": k})
        if general:
            nd["general"] = _general_spec(nodes, c["expr"])
        elif len(ops) >= 2 or nested:
            nd["multi"] = specs
        else:
            nd["temporal"] = {"node": ops[0]}
    return ir


def _general_spec(nodes: list[dict], root: int) -> dict:
    """The temporal operators of a content decided per observation
    (`General` of the base contract): each after those inside it, with the
    operators directly inside its operands, and those of the content that
    are not inside another."""
    order: list[int] = []
    kids: dict[int, list[int]] = {}

    def visit(k: int) -> None:
        inner = [x for c in _children(nodes[k]) for x in _top_temporal(nodes, c)]
        for x in inner:
            visit(x)
        order.append(k)
        kids[k] = inner

    tops = sorted(_top_temporal(nodes, root))
    for k in tops:
        visit(k)
    pos = {k: i for i, k in enumerate(order)}
    return {"ops": [{"node": k, "kids": [pos[x] for x in kids[k]]} for k in order],
            "tops": [pos[k] for k in tops]}


_WINDOW_MODES = {"within": 1, "throughout": 2, "until": 3}


def _op_args(ir: dict, k: int) -> str:
    """mode, p, q, duration and node of a temporal operator."""
    node = ir["exprs"][k]
    p, q = (node["p"], node["q"]) if node["op"] == "until" else (node["x"], 0)
    return (f"{_WINDOW_MODES[node['op']]}, {p}, {q}, {scaled(node['d'], 'a duration')}, {k}")


def _op_args_general(ir: dict, k: int) -> str:
    """mode, node, p, q and duration of a temporal operator of a General content."""
    node = ir["exprs"][k]
    p, q = (node["p"], node["q"]) if node["op"] == "until" else (node["x"], 0)
    return f"{_WINDOW_MODES[node['op']]}, {k}, {p}, {q}, {scaled(node['d'], 'a duration')}"


def node_words(m: int) -> int:
    """The storage words of a node of the segment tree for m operators."""
    return 1 if m <= 3 else 5 ** (m - 3)


def _window_lines(ir: dict, nd: dict) -> list[str]:
    """The temporal operators of the content of a norm: one (`_temporal`),
    or two or more, or a nested one (`_multi`)."""
    if nd.get("general"):
        spec = nd["general"]
        out = [f"        _general({nd['content']['expr']});"]
        for op in spec["ops"]:
            out.append(f"        _gop({_op_args_general(ir, op['node'])});")
            out += [f"        _gkid({j});" for j in op["kids"]]
        out += [f"        _gtop({j});" for j in spec["tops"]]
        return out
    if nd.get("multi"):
        specs = nd["multi"]
        out = [f"        _multi({nd['content']['expr']}, {node_words(len(specs))});"]
        for sp in specs:
            if "outer" not in sp:
                out.append(f"        _op({_op_args(ir, sp['node'])});")
                continue
            out.append(f"        _nested({sp['outer']}, {scaled(sp['d'], 'a duration')}, "
                       f"{sp['node']}, {sp['p']}, {str(sp['oneg']).lower()});")
            out += [f"        _level({mode}, {str(neg).lower()}, {scaled(d, 'a duration')});"
                    for mode, neg, d in sp["levels"]]
        return out
    tm = nd.get("temporal")
    if tm is None:
        return []
    return [f"        _temporal({_op_args(ir, tm['node'])}, {nd['content']['expr']});"]


# The operator code of a temporal operator of a content decided per
# observation (OP_GVAL of the base contract: its value is read from the
# values that `General` keeps).
_OP_GVAL = 13


def _node_lines(nodes: list[dict], skip: set, general: frozenset = frozenset()) -> list[str]:
    """The `_node` calls of the expressions; `general`: the temporal
    operators of contents decided per observation."""
    out = []
    for k, nd in enumerate(nodes):
        where = f"node {k}"
        if k in skip:              # a node of an item that is left out
            out.append('        _node(0, 0, 0, 0, 0, 0, "", "", "", 0, 0, "");')
            continue
        op = nd["op"]
        bop, a, b, d, ik = 0, 0, 0, 0, 0
        s1 = s2 = s3 = '""'
        lk, ln, ls = 0, 0, '""'
        items: list[int] = []
        if op == "lit":
            v = nd["v"]
            if isinstance(v, bool):
                lk, ln = 2, int(v)
            elif isinstance(v, (int, float, Fraction)):
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
        code = _OP_GVAL if k in general else _OPS[op]
        out.append(f"        _node({code}, {bop}, {a}, {b}, {d}, {ik}, {s1}, {s2}, {s3}, "
                   f"{lk}, {ln}, {ls});")
        out += [f"        _item({i});" for i in items]
    return out


def _header(name: str, regimes: dict | None) -> list[str]:
    if regimes is None:
        return [f"contract {name} is CadlNormsMonitorBase {{",
                "    /// reporter_: the address that records the observations (the deployer when zero)",
                "    constructor(address reporter_) CadlNormsMonitorBase(reporter_) {"]
    return [f"contract {name} is CadlNormsMonitorRegimes {{",
            "    /// reporter_: the address that records the observations (the deployer when zero);",
            "    /// orchestrator_: the address that sets the current regime (the deployer when zero)",
            "    constructor(address reporter_, address orchestrator_)",
            "            CadlNormsMonitorRegimes(reporter_, orchestrator_) {"]


def generate_norms_monitor_sol(sos: SoSDefinition, contract: ContractDef) -> str:
    """The Solidity text of the monitor of one contract. Raises
    `NormsTargetUnsupported` for a contract that cannot be monitored."""
    check_contract_id(contract.id)
    from ..norms_ir import regime_data
    regimes = regime_data(sos, contract)
    ir = solidity_ir(sos, contract)
    skip = {k for it in ir["items"]["guarantee"] if it.get("unsupported") and it["expr"] is not None
            for k in _subtree(ir["exprs"], it["expr"])}
    body: list[str] = []
    body.append(f"        contractId = {_s(ir['contract'])};")
    for name, (indexed, lo, hi) in ir["actors"].items():
        body.append(f"        _actor({_s(name)}, {str(bool(indexed)).lower()}, "
                    f"{str(lo is not None).lower()}, {int(lo or 0)}, "
                    f"{str(hi is not None).lower()}, {int(hi or 0)});")
    lc = ir["lifecycle"]
    if lc is not None:
        body.append("        m.data.hasLifecycle = true;")
        body.append(f"        m.lcInitial = {_s(lc['initial'] or '')};")
        for s in lc["states"]:
            body.append(f"        m.lcStates[keccak256(bytes({_s(s)}))] = true;")
        for s in lc["terminal"]:
            body.append(f"        m.lcTerminal[keccak256(bytes({_s(s)}))] = true;")
    general = frozenset(op["node"] for nd in ir["norms"] if nd.get("general")
                        for op in nd["general"]["ops"])
    body += _node_lines(ir["exprs"], skip, general)
    for name in temporal_names(ir, skip):
        body.append(f"        m.temporalName[keccak256(bytes({_s(name)}))] = true;")

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
        deadline = nd["deadline"]
        norm_lines.append(
            f"        d = _norm({_s(nd['id'])}, {_KINDS[nd['kind']]}, "
            f"{trig}, {until}, {content}, "
            f"{str(deadline is not None).lower()}, "
            f"{scaled(deadline, 'deadline') if deadline is not None else 0}, "
            f"{str(nd['target']).lower()});")
        norm_lines += _window_lines(ir, nd)
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
        rule_lines.append(f"        m.rules.push(Rule({_s(r['creates'])}, {str(r['error']).lower()}, "
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

    if regimes is not None:
        body.append(f"        regime = {_s(regimes['initial'])};")
        body += [f"        _regime({_s(r)}, {str(r in regimes['out']).lower()});"
                 for r in regimes["regimes"]]
    name = monitor_name(contract.id)
    base = _base(regimes)
    lines = [
        "// SPDX-License-Identifier: Apache-2.0",
        f"// Norms monitor of contract {sanitize_id(contract.id)} (Appendix F of the CADL",
        "// specification, norms 0.1). Generated by `cadl codegen -t solidity`. Do not edit.",
        "// Compile with solc 0.8.24 to 0.8.37 and --optimize --optimize-runs 1 (without the",
        "// optimizer the runtime code exceeds the limit of EIP-170); deploy and link the",
        "// libraries CadlNormsValues, CadlNormsEval, CadlNormsWindows, CadlNormsMulti,",
        "// CadlNormsItems and CadlNormsActs, in this order.",
        f"// Checked with solc {' and '.join(SOLC_CHECKED)}.",
        f"pragma solidity >={SOLC_CHECKED[0]} <={SOLC_CHECKED[1]};",
        "",
        base,
        "",
        "// The model of the contract: the constructor fills it.",
        *_header(name, regimes),
        *body,
        "    }",
        "}",
        "",
    ]
    text = "\n".join(lines)
    estimate = dict(RUNTIME_ESTIMATE)
    if regimes is not None:
        estimate["monitor"] += REGIMES_RUNTIME
    big = {k: v for k, v in estimate.items() if v > RUNTIME_LIMIT}
    if big:
        raise NormsTargetUnsupported([
            f"the runtime code of {', '.join(big)} is estimated above the limit of "
            f"{RUNTIME_LIMIT} bytes of EIP-170"])
    size = estimated_initcode(text)
    if size > INITCODE_LIMIT:
        raise NormsTargetUnsupported([
            f"the monitor is too large: its creation code is estimated at {size} bytes, above "
            f"the limit of {INITCODE_LIMIT} bytes of EIP-3860"])
    return text


# --- Versions of a run (Appendix K, K.11; F.6) -------------------------------------

VERSIONS_PART = Path(__file__).with_name("norms_monitor_versions.sol")
_FILL_MARK = ("    // =========================================================================\n"
              "    // Filling the model")
_ITEMS_LIB = re.compile(r"library CadlNormsItems \{.*?\n\}\n", re.S)

# CadlNormsItems of the monitor with versions: the items of the instance,
# each with its definition (def), the observation it starts at (start) and
# whether an amendment closed it (closed).
_VERSION_ITEMS = """library CadlNormsItems {
    /// assume: before guarantee: (an assumption that fails excuses a
    /// guarantee that fails at the same observation). Returns whether an
    /// assumption has failed, now or before.
    function step(Monitor storage m, uint256 n, bool failed) public returns (bool) {
        for (uint256 i = 0; i < m.assumeRecs.length; i++) {
            ItemRec storage r = m.assumeRecs[i];
            if (itemStep(m.assumeItems[r.def], r, m.data, n) == 2) failed = true;
        }
        for (uint256 i = 0; i < m.guaranteeRecs.length; i++) {
            ItemRec storage r = m.guaranteeRecs[i];
            if (itemStep(m.guaranteeItems[r.def], r, m.data, n) == 2) r.verdict = failed ? 3 : 2;
        }
        return failed;
    }

    /// 2 when the item has just been found false
    function itemStep(Item storage it, ItemRec storage r, EvalData storage data, uint256 n) internal returns (uint8) {
        if (it.broken || it.unsupported || r.closed || r.value != 0) return 0;
        if (it.temporal) {
            Val memory v = CadlNormsEval.evaluate(data, it.root, r.start, n);
            if (v.k == K_D) return 0;
            if (v.k != K_B && v.k != K_N) { r.value = 3; return 0; }
            r.value = v.n != 0 ? 1 : 2;
            return r.value;
        }
        Val memory w = CadlNormsEval.evaluate(data, it.root, n, n);
        if ((w.k == K_B || w.k == K_N) && w.n != 0) { r.seen = true; return 0; }
        if (w.k == K_B || w.k == K_N) { r.value = 2; return 2; }
        return 0;
    }

    function finish(Monitor storage m, bool endHere) public {
        for (uint256 i = 0; i < m.assumeRecs.length; i++) finishItem(m.assumeItems[m.assumeRecs[i].def], m.assumeRecs[i], endHere);
        for (uint256 i = 0; i < m.guaranteeRecs.length; i++) finishItem(m.guaranteeItems[m.guaranteeRecs[i].def], m.guaranteeRecs[i], endHere);
    }

    function finishItem(Item storage it, ItemRec storage r, bool endHere) internal {
        if (it.broken || it.unsupported || r.closed) return;
        if (r.value == 0 && !it.temporal && endHere && r.seen) r.value = 1;
        if (r.value == 1) r.verdict = 1;
    }

    /// The first observation that a temporal item not decided reads (its
    /// start), or n.
    function low(Monitor storage m, uint256 n) public view returns (uint256) {
        n = lowIn(m.assumeItems, m.assumeRecs, n);
        return lowIn(m.guaranteeItems, m.guaranteeRecs, n);
    }

    function lowIn(Item[] storage items, ItemRec[] storage rs, uint256 n) internal view returns (uint256) {
        for (uint256 i = 0; i < rs.length; i++) {
            Item storage it = items[rs[i].def];
            if (it.temporal && !it.broken && !it.unsupported && !rs[i].closed && rs[i].value == 0
                && rs[i].start < n) n = rs[i].start;
        }
        return n;
    }
}
"""

# The lines of the base that the monitor with versions reads differently
# (after _versions_base has read the norms and their temporal contents
# through the definitions of the instance, Monitor.dp and Monitor.sl).
_VERSION_EDITS = [
    ("struct ItemRec { uint8 value; bool seen; uint8 verdict; }",
     "struct ItemRec { uint8 value; bool seen; uint8 verdict; bool closed; uint256 start; uint256 def; uint256 uid; }"),
    ("    // (3, see `generals`), or none (0)\n    uint8 tmode;\n}",
     "    // (3, see `generals`), or none (0)\n    uint8 tmode;\n"
     "    // on_amendment (0 carry_over, 1 discharge, 2 transform to transformTo)\n"
     "    uint8 onAm; string transformTo;\n}"),
    ("    bool failed;               // an assumption has failed\n    int256 failedAt;\n",
     "    bool failed;               // an assumption has failed\n    int256 failedAt;\n"
     "    // --- the versions of the run (see norms_monitor_versions.sol)\n"
     "    mapping(uint256 => uint256) dp;      // a norm of the instance -> its definition\n"
     "    mapping(uint256 => uint256) sl;      // a definition -> the slot of its temporal content\n"
     "    mapping(uint256 => bytes32) idh;     // a definition -> keccak256 of its id\n"
     "    mapping(bytes32 => uint256) vdef;    // keccak256(version, id hash) -> definition + 1\n"
     "    uint256[] vnlo;                      // the first definition of each version (and the end)\n"
     "    uint256[] valo;                      // the same for assume: and guarantee: items and rules\n"
     "    uint256[] vglo;\n"
     "    uint256[] vrlo;\n"
     "    mapping(uint256 => uint8) vrule;     // the transitional rule of the contract in a version\n"
     "    mapping(bytes32 => bool) vstate;     // keccak256(version, state hash): in_states\n"
     "    uint256 rlo;                         // the counts-as rules of the version followed\n"
     "    uint256 rhi;\n"
     "    uint256 vin;                         // the version in force (set by the orchestrator)\n"
     "    uint256 vcur;                        // the version in force at the last observation\n"
     "    uint256 vfol;                        // the version the instance follows\n"
     "    uint256 vpend;                       // a waiting switch (when vpendSet)\n"
     "    bool vpendSet;\n"
     "    uint256[] vpath;                     // the versions of the amendments of an observation\n"
     "    uint256 uid;                         // the items created so far\n"
     "    bytes32[] amendViolated;             // the norms violated by the amendments of an observation\n"),
    # the counts-as rules of the version followed
    ("Ev[] memory buf = new Ev[](m.rules.length * (msgs.length + 1));",
     "Ev[] memory buf = new Ev[]((m.rhi - m.rlo) * (msgs.length + 1));"),
    ("for (uint256 i = 0; i < m.rules.length; i++) {",
     "for (uint256 i = m.rlo; i < m.rhi; i++) {"),
    # the version and the amendments before the observation is recorded
    ("        bool endHere = record(t, values, hasState, state, end);\n",
     "        planVersions();\n        bool endHere = record(t, values, hasState, state, end);\n"),
    ("""    function runStep(uint256 n, int256 t, Msg[] calldata msgs, bool endHere) internal {
        (uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm) = marks();
        step(n, t, msgs, endHere);
        emitChanges(ns, nv, am, gm, t);
        // the first observation that a later step can read: this one, and
        // the first while a temporal item is not decided (the candidates of
        // a temporal content keep what they need in their window)
        uint256 keep = n > 0 && CadlNormsItems.pending(m.assumeItems, m.assumeRecs, m.guaranteeItems,
                                                       m.guaranteeRecs) ? 0 : n;""",
     """    /// The version the instance starts with, or the versions it is
    /// amended to, before an observation is recorded (norms_monitor_versions.sol).
    function planVersions() internal virtual;

    function runStep(uint256 n, int256 t, Msg[] calldata msgs, bool endHere) internal {
        VMarks memory before = CadlNormsVersions.marks(m);
        if (n > 0) CadlNormsVersions.amendPath(m, n, t);
        step(n, t, msgs, endHere);
        CadlNormsVersions.changes(m, before, t);
        // the first observation that a later step can read: this one, and
        // the start of a temporal item that is not decided (the candidates of
        // a temporal content keep what they need in their window)
        uint256 keep = CadlNormsItems.low(m, n);"""),
    ("""        (uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm) = marks();
        uint256 n = m.observations - 1;
        settle(n, m.data.obsT[n]);
        finishItems(true);
        emitChanges(ns, nv, am, gm, m.data.obsT[n]);""",
     """        VMarks memory before = CadlNormsVersions.marks(m);
        uint256 n = m.observations - 1;
        settle(n, m.data.obsT[n]);
        finishItems(true);
        CadlNormsVersions.changes(m, before, m.data.obsT[n]);"""),
    ("m.failed = CadlNormsItems.step(m.assumeItems, m.assumeRecs, m.guaranteeItems, m.guaranteeRecs, m.data, n, was);",
     "m.failed = CadlNormsItems.step(m, n, was);"),
    ("uint256[] memory violated = judge(n, t, msgs, created);",
     "uint256[] memory violated = vfirst(m, judge(n, t, msgs, created));"),
    ("CadlNormsItems.finish(m.assumeItems, m.assumeRecs, m.guaranteeItems, m.guaranteeRecs, endHere);",
     "CadlNormsItems.finish(m, endHere);"),
    # the reparation by its id, among the norms of the instance (F.5)
    ("""        bool repActivated = false;
        if (activate && d.reparation >= 0) {
            uint256 rp = uint256(d.reparation);""",
     """        bool repActivated = false;
        int256 rep = activate && d.hasReparationId ? vfind(m, keccak256(bytes(d.reparationId))) : int256(-1);
        if (rep >= 0) {
            uint256 rp = uint256(rep);"""),
    ("""        if (repActivated) {
            uint256 rp = uint256(d.reparation);""",
     """        if (repActivated) {
            uint256 rp = uint256(rep);"""),
    # the slot of the content, once
    ("""    function judgeWindow(uint256 i, uint256 n, int256 t, bool open) internal returns (bool hit, bool closed) {
        bool empty;
        if (m.normDefs[m.dp[i]].tmode == 1) (hit, empty) = CadlNormsWindows.judge(m.windows[m.sl[m.dp[i]]], m.data, m.temporals[m.sl[m.dp[i]]], n, t, open);
        else if (m.normDefs[m.dp[i]].tmode == 2) (hit, empty) = CadlNormsMulti.judge(m.multis[m.sl[m.dp[i]]], m.data, n, t, open);
        else (hit, empty) = CadlNormsMulti.gjudge(m.generals[m.sl[m.dp[i]]], m.data, n, open);""",
     """    function judgeWindow(uint256 i, uint256 n, int256 t, bool open) internal returns (bool hit, bool closed) {
        bool empty;
        uint256 s = m.sl[m.dp[i]];
        uint8 mode = m.normDefs[m.dp[i]].tmode;
        if (mode == 1) (hit, empty) = CadlNormsWindows.judge(m.windows[s], m.data, m.temporals[s], n, t, open);
        else if (mode == 2) (hit, empty) = CadlNormsMulti.judge(m.multis[s], m.data, n, t, open);
        else (hit, empty) = CadlNormsMulti.gjudge(m.generals[s], m.data, n, open);"""),
    # filling: definitions only (the instance has its own norms and items)
    ("""        d.reparation = -1;
        NormRec storage r = m.recs.push();
        r.act = -1;
    }""", """        d.reparation = -1;
        uint256 i = m.normDefs.length - 1;
        m.sl[i] = i;
        m.idh[i] = keccak256(bytes(id));
        m.vdef[keccak256(abi.encode(m.vnlo.length - 1, m.idh[i]))] = i + 1;
    }"""),
    ("""        m.assumeItems.push(Item(text, temporal_, broken, unsupported, root));
        m.assumeRecs.push(ItemRec(0, false, V_PENDING));""",
     "        m.assumeItems.push(Item(text, temporal_, broken, unsupported, root));"),
    ("""        m.guaranteeItems.push(Item(text, temporal_, broken, unsupported, root));
        m.guaranteeRecs.push(ItemRec(0, false, V_PENDING));""",
     "        m.guaranteeItems.push(Item(text, temporal_, broken, unsupported, root));"),
]


REGIMES_VERSIONS_PART = Path(__file__).with_name("norms_monitor_regimes_versions.sol")

# The times of the changes of the monitor with versions and regimes are
# times of the trace (as in the regime part, see _REGIME_EDITS).
_REGIMES_VERSIONS_EDITS = [
    ("    function changes(Monitor storage m, VMarks memory b, int256 t) public {\n",
     "    function changes(Monitor storage m, VMarks memory b, int256 t) public {\n"
     "        t += m.shift;\n"),
]


def _versions_base(regimes: bool = False) -> str:
    """The base as the monitor with versions reads it, and the versions part;
    with `regimes`, also as the monitor of a contract not in force reads it,
    and the part that combines them."""
    text = BASE.read_text(encoding="utf-8").rstrip("\n")
    if regimes:
        for old, new in _REGIME_EDITS:
            assert text.count(old) == 1, old
            text = text.replace(old, new)
    assert text.count(_FILL_MARK) == 1
    head, tail = text.split(_FILL_MARK, 1)
    assert len(_ITEMS_LIB.findall(head)) == 1
    head = _ITEMS_LIB.sub(lambda _: _VERSION_ITEMS, head)
    # the norms of the instance, by their definitions and the slots of their contents
    head = head.replace("m.normDefs.length", "m.recs.length")
    head = re.sub(r"m\.normDefs\[(\w+)\]", r"m.normDefs[m.dp[\1]]", head)
    head = re.sub(r"m\.(windows|temporals|multis|generals)\[(\w+)\]", r"m.\1[m.sl[m.dp[\2]]]", head)
    text = head + _FILL_MARK + tail
    for old, new in _VERSION_EDITS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    text += "\n\n" + VERSIONS_PART.read_text(encoding="utf-8").rstrip("\n")
    if regimes:
        for old, new in _REGIMES_VERSIONS_EDITS:
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        text += "\n\n" + REGIMES_VERSIONS_PART.read_text(encoding="utf-8").rstrip("\n")
    return text


_RULES = {"keep": 0, "switch": 1}
_ON_AMENDMENT = {None: 0, "carry_over": 0, "discharge": 1, "transform": 2}


def _shift_term(t: dict | None, off: int) -> None:
    if t is None or t.get("error"):
        return
    if t.get("expr") is not None:
        t["expr"] += off
    if t.get("args") is not None:
        t["args"] = [a + off for a in t["args"]]


def _shift_ir(ir: dict, off: int) -> dict:
    """A copy of `ir` whose node indices start at `off` (the nodes of the
    versions before it come first)."""
    import copy
    ir = copy.deepcopy(ir)
    for nd in ir["exprs"]:
        for key in _NODE_KEYS.get(nd["op"], ()):
            nd[key] += off
        if nd["op"] == "in":
            nd["items"] = [i + off for i in nd["items"]]
    for nd in ir["norms"]:
        for key in ("trigger", "until", "content"):
            _shift_term(nd[key], off)
        if nd.get("temporal"):
            nd["temporal"]["node"] += off
        for sp in nd.get("multi") or []:
            sp["node"] += off
            if "p" in sp:
                sp["p"] += off
        if nd.get("general"):
            for op in nd["general"]["ops"]:
                op["node"] += off
    for r in ir["rules"]:
        if not r["error"]:
            _shift_term(r["when"], off)
            _shift_term(r["if"], off)
    for key in ("assume", "guarantee"):
        for it in ir["items"][key]:
            if it["expr"] is not None:
                it["expr"] += off
    return ir


_NODE_KEYS = {"not": ("x",), "within": ("x",), "throughout": ("x",), "and": ("l", "r"),
              "or": ("l", "r"), "bin": ("l", "r"), "in": ("l",), "until": ("p", "q")}


def _shape(nodes: list[dict], root: int) -> str:
    """The subtree of a node with its node indices relative to the root,
    to compare a content across versions."""
    import json as _json
    order = sorted(_subtree(nodes, root))
    pos = {k: i for i, k in enumerate(order)}
    out = []
    for k in order:
        nd = dict(nodes[k])
        for key in _NODE_KEYS.get(nd["op"], ()):
            nd[key] = pos[nd[key]]
        if nd["op"] == "in":
            nd["items"] = [pos[i] for i in nd["items"]]
        out.append(nd)
    return _json.dumps(out, sort_keys=True, default=str)


def _window_shape(nd: dict, nodes: list[dict]) -> str:
    """How the Solidity monitor decides a temporal content (the specs with
    node indices relative to the content)."""
    import json as _json
    root = nd["content"]["expr"]
    order = sorted(_subtree(nodes, root))
    pos = {k: i for i, k in enumerate(order)}

    def rel(x):
        if isinstance(x, dict):
            return {k: (pos[v] if k in ("node", "p") and isinstance(v, int) else rel(v))
                    for k, v in x.items()}
        if isinstance(x, list):
            return [rel(v) for v in x]
        return x
    return _json.dumps({k: rel(nd.get(k)) for k in ("temporal", "multi", "general")},
                       sort_keys=True, default=str)


def versions_chain(sos_list: list, contract_id: str) -> tuple:
    """(the chain of the versions, the contract in each, in the order of the
    sequence by previous:). Raises `NormsTargetUnsupported`."""
    from ...norms_eval import NormsEvalError, VersionChain, version_info
    try:
        chain = VersionChain([version_info(s) for s in sos_list])
    except NormsEvalError as e:
        raise NormsTargetUnsupported([f"the versions of the run: {e}"]) from None
    contracts = []
    for info in chain.infos:
        c = next((c for c in info.sos.contracts if c.id == contract_id), None)
        if c is None:
            raise NormsTargetUnsupported([
                f"version {info.name} has no contract {contract_id}; the monitor follows "
                f"the contract through every version of the run"])
        contracts.append(c)
    return chain, contracts


# What the versions part adds to the runtime code of the monitor (bytes),
# and the runtime code of the libraries it changes or adds, measured with
# solc 0.8.24 and 0.8.37, the optimizer with 1 and with 200 runs, plus 3 %.
VERSIONS_RUNTIME = 3500
# and what the part for versions with regimes adds to that
REGIMES_VERSIONS_RUNTIME = 3600
VERSIONS_LIBRARIES = {"CadlNormsItems": 2550, "CadlNormsVersions": 13700}


def generate_norms_monitor_sol_versions(sos: SoSDefinition, contract: ContractDef,
                                        versions: list) -> str:
    """The Solidity monitor of one contract that follows the versions of a
    run (Appendix K, K.11): `sos` and `versions` (parsed files) are the
    versions; they form one sequence by previous:. The orchestrator sets the
    version in force with setVersion."""
    check_contract_id(contract.id)
    chain, contracts = versions_chain([sos, *versions], contract.id)
    names = chain.names()
    # versions with regime bindings (K.11, "Versions and regimes")
    regimes = any(info.regimes is not None for info in chain.infos)
    irs = []
    for info, c in zip(chain.infos, contracts):
        ir = solidity_ir(info.sos, c)
        if any(it.get("unsupported") for it in ir["items"]["guarantee"]):
            raise NormsTargetUnsupported([
                f"version {info.name} has a guarantee: item that the Solidity monitor leaves "
                f"out; with versions the items continue by their text across versions, so "
                f"every item must be monitored"])
        irs.append(ir)
    for v in range(1, len(irs)):
        if irs[v]["lifecycle"] != irs[0]["lifecycle"]:
            raise NormsTargetUnsupported([
                f"the lifecycle of {contract.id} in version {names[v]} differs from that in "
                f"version {names[0]}; the Solidity monitor with versions keeps one lifecycle "
                f"(its states, initial and terminal states) for every version"])
        if irs[v]["actors"] != irs[0]["actors"]:
            raise NormsTargetUnsupported([
                f"the actors declared in version {names[v]} differ from those in version "
                f"{names[0]}; the Solidity monitor with versions keeps one declaration of "
                f"the actors for every version"])
    # the nodes of every version, one after the other
    offsets, exprs = [], []
    shifted = []
    for ir in irs:
        offsets.append(len(exprs))
        s = _shift_ir(ir, len(exprs))
        exprs.extend(s["exprs"])
        shifted.append(s)
    for s in shifted:
        s["exprs"] = exprs
    # the slots of the temporal contents: a norm shares the slot of the same
    # norm in an earlier version whose content is the same text (F.6 keeps its
    # candidates); it must then read the same way
    defs_src = [{d.id: d for d in c.norms} for c in contracts]
    shares: list[dict] = []
    for v, s in enumerate(shifted):
        sh = {}
        for nd in s["norms"]:
            c = nd["content"]
            if not (c and c.get("kind") == "predicate" and not c.get("error")) or not (
                    nd.get("temporal") or nd.get("multi") or nd.get("general")):
                continue
            text = (defs_src[v][nd["id"]].content or "").strip()
            for u in range(v):
                prev = next((x for x in shifted[u]["norms"] if x["id"] == nd["id"]), None)
                if prev is None or (defs_src[u][nd["id"]].content or "").strip() != text:
                    continue
                pc = prev["content"]
                same = (pc and pc.get("kind") == "predicate" and not pc.get("error")
                        and _shape(exprs, pc["expr"]) == _shape(exprs, c["expr"])
                        and _window_shape(prev, exprs) == _window_shape(nd, exprs))
                if not same:
                    raise NormsTargetUnsupported([
                        f"norm {nd['id']} has the same content in versions {names[u]} and "
                        f"{names[v]}, but the Solidity monitor reads it differently in them; "
                        f"F.6 keeps its pending candidates across the amendment"])
                sh[nd["id"]] = (u, prev)
                break
        shares.append(sh)

    body: list[str] = []
    body.append(f"        contractId = {_s(irs[0]['contract'])};")
    for name, (indexed, lo, hi) in irs[0]["actors"].items():
        body.append(f"        _actor({_s(name)}, {str(bool(indexed)).lower()}, "
                    f"{str(lo is not None).lower()}, {int(lo or 0)}, "
                    f"{str(hi is not None).lower()}, {int(hi or 0)});")
    lc = irs[0]["lifecycle"]
    if lc is not None:
        body.append("        m.data.hasLifecycle = true;")
        body.append(f"        m.lcInitial = {_s(lc['initial'] or '')};")
        for st in lc["states"]:
            body.append(f"        m.lcStates[keccak256(bytes({_s(st)}))] = true;")
        for st in lc["terminal"]:
            body.append(f"        m.lcTerminal[keccak256(bytes({_s(st)}))] = true;")
    general = frozenset(op["node"] for s in shifted for nd in s["norms"] if nd.get("general")
                        for op in nd["general"]["ops"])
    body += _node_lines(exprs, set(), general)
    tnames = sorted({n for s in shifted for n in temporal_names(s)})
    for name in tnames:
        body.append(f"        m.temporalName[keccak256(bytes({_s(name)}))] = true;")

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

    version_lines: list[str] = []
    def_pos = 0
    pos_of: list[dict] = []
    for v, (info, s) in enumerate(zip(chain.infos, shifted)):
        rule, states = info.rule_for(contract.id)
        code = 0 if rule == "keep" else (2 if states else 1)
        if v == 0:
            code = 0
        if regimes:
            b = info.regimes
            version_lines.append(f"        _version({_s(info.name)}, {code}, "
                                 f"{_s(b.initial or '') if b is not None else chr(34) * 2});")
            for r in (b.regimes if b is not None else []):
                version_lines.append(f"        _versionRegime({_s(r)}, "
                                     f"{str(not b.in_force(contract.id, r)).lower()});")
        else:
            version_lines.append(f"        _version({_s(info.name)}, {code});")
        if v > 0 and rule != "keep":
            version_lines += [f"        _versionState({_s(st)});" for st in (states or [])]
        pos_of.append({})
        src = defs_src[v]
        for nd in s["norms"]:
            pos_of[v][nd["id"]] = def_pos
            def_pos += 1
            trig, until, content = term(nd["trigger"]), term(nd["until"]), term(nd["content"])
            deadline = nd["deadline"]
            version_lines.append(
                f"        d = _norm({_s(nd['id'])}, {_KINDS[nd['kind']]}, "
                f"{trig}, {until}, {content}, "
                f"{str(deadline is not None).lower()}, "
                f"{scaled(deadline, 'deadline') if deadline is not None else 0}, "
                f"{str(nd['target']).lower()});")
            if nd["id"] in shares[v]:
                u, _ = shares[v][nd["id"]]
                version_lines.append(f"        _shareContent({pos_of[u][nd['id']]});")
            else:
                version_lines += _window_lines(s, nd)
            if nd["bearer"] is not None:
                version_lines.append("        d.hasBearer = true;")
                version_lines.append(f"        _pat(d.bearer, {_pat_args(nd['bearer'])});")
            version_lines.append(f"        d.severity = {_s(nd['severity'])};")
            if nd["sanction"] is not None:
                version_lines.append("        d.hasSanction = true;")
                version_lines.append(f"        d.sanction = {_s(nd['sanction'])};")
            if nd["reparation_id"] is not None:
                version_lines.append("        d.hasReparationId = true;")
                version_lines.append(f"        d.reparationId = {_s(nd['reparation_id'])};")
            if nd["transition"] is not None:
                version_lines.append("        d.hasTransition = true;")
                version_lines.append(f"        d.transition = {_s(nd['transition'])};")
            for kind in EFFECT_KINDS:
                version_lines += [f"        d.{kind}.push({x});" for x in nd["effects"][kind]]
            d = src[nd["id"]]
            how = d.on_amendment or "carry_over"
            if _ON_AMENDMENT.get(how, 0):
                version_lines.append(f"        _onAmendment({_ON_AMENDMENT[how]}, "
                                     f"{_s(d.transform_to or '')});")
        for r in s["rules"]:
            when = -1 if r["error"] else term(r["when"])
            cond = -1 if r["error"] else term(r["if"])
            version_lines.append(f"        m.rules.push(Rule({_s(r['creates'])}, "
                                 f"{str(r['error']).lower()}, {when}, {cond}));")
        for key, fn in (("assume", "_assume"), ("guarantee", "_guarantee")):
            for it in s["items"][key]:
                root = it["expr"] if it["expr"] is not None else 0
                version_lines.append(
                    f"        {fn}({_s(it['text'])}, {str(it['temporal']).lower()}, "
                    f"{str(bool(it['error'])).lower()}, {str(bool(it.get('unsupported'))).lower()}, "
                    f"{root});")
    version_lines.append("        _versionsEnd();")
    body += ["        Term storage tm;"] if any("tm = " in line for line in terms) else []
    body += terms
    body.append("        NormDef storage d;")
    body += version_lines

    name = monitor_name(contract.id)
    parent = "CadlNormsMonitorRegimesVersions" if regimes else "CadlNormsMonitorVersions"
    lines = [
        "// SPDX-License-Identifier: Apache-2.0",
        f"// Norms monitor of contract {sanitize_id(contract.id)} (Appendix F of the CADL",
        "// specification, norms 0.1) that follows the versions of a run (Appendix K, K.11):",
        f"// versions {', '.join(names)}. Generated by `cadl codegen -t solidity --version ...`.",
        "// Do not edit.",
        "// Compile with solc 0.8.24 to 0.8.37 and --optimize --optimize-runs 1 (without the",
        "// optimizer the runtime code exceeds the limit of EIP-170); deploy and link the",
        "// libraries CadlNormsValues, CadlNormsEval, CadlNormsWindows, CadlNormsMulti,",
        "// CadlNormsItems, CadlNormsActs and CadlNormsVersions, in this order.",
        f"// Checked with solc {' and '.join(SOLC_CHECKED)}.",
        f"pragma solidity >={SOLC_CHECKED[0]} <={SOLC_CHECKED[1]};",
        "",
        _versions_base(regimes),
        "",
        "// The model of the contract in every version: the constructor fills it.",
        f"contract {name} is {parent} {{",
        "    /// reporter_: the address that records the observations (the deployer when zero);",
        "    /// orchestrator_: the address that sets the version in force"
        + (" and the current regime" if regimes else "") + " (the deployer when zero)",
        "    constructor(address reporter_, address orchestrator_)",
        f"            {parent}(reporter_, orchestrator_) {{",
        *body,
        "    }",
        "}",
        "",
    ]
    text = "\n".join(lines)
    estimate = dict(RUNTIME_ESTIMATE)
    estimate["monitor"] += VERSIONS_RUNTIME + (REGIMES_VERSIONS_RUNTIME if regimes else 0)
    estimate.update(VERSIONS_LIBRARIES)
    big = {k: v for k, v in estimate.items() if v > RUNTIME_LIMIT}
    if big:
        raise NormsTargetUnsupported([
            f"the runtime code of {', '.join(big)} is estimated above the limit of "
            f"{RUNTIME_LIMIT} bytes of EIP-170"])
    size = estimated_initcode(text)
    if size > INITCODE_LIMIT:
        raise NormsTargetUnsupported([
            f"the monitor is too large: its creation code is estimated at {size} bytes, above "
            f"the limit of {INITCODE_LIMIT} bytes of EIP-3860"])
    return text
