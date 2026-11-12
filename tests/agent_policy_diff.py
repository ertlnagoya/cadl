"""Inputs and drivers for the comparison of the generated boundary
policies (agent-rego, agent-cedar) with the decision function of G.5
(`cadl agents-decide`). Not a test module: tests/test_agent_policy_diff.py
uses it, and it can be run as a script for the long run.

The input space has two parts:

- the file RICH of tests/test_agents_extension.py in three variants (the
  `enforcement:` of C1 empty, `default: allow`, `default: deny`): every
  message of a set of senders, receivers and names, each without
  `instances` and with each of a list of instance sets (contract
  instances, bindings, in-force flags, derived instances with valid and
  invalid chains);
- random files: one to three enforced contracts (and sometimes one that
  is not enforced) with static and non-static permissions and
  prohibitions, `default: allow`, `deny` or omitted, escalation,
  fallbacks and delegations, each with random messages and random
  instance sets.

For Cedar, a request carries the message only. The Cedar answer is
checked in two ways (G.10): it MUST equal the decision of G.5 for the
input with only `message` on the contracts without their non-static
norms (the semantics of the Cedar target), and it MUST equal the
decision of G.5 for the full input wherever G.10 says that the Cedar
answer is the decision (`cedar_decides`).
"""

from __future__ import annotations

import copy
import json
import random
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from cadl.agents import build_policy, decide, governs, message_kind, parse_concrete_actor, ref_match
from cadl.codegen import generate
from cadl.parser import parse
from cadl.type_checker import type_check

OPA = shutil.which("opa")
CEDAR = shutil.which("cedar")


# --- The fixed file and its instance sets ---------------------------------------

def rich_variants(rich: str) -> dict[str, str]:
    return {name: rich.replace("    enforcement:\n", f"    enforcement:{d}\n", 1)
            for name, d in (("omitted", ""), ("allow", " {default: allow}"),
                            ("deny", " {default: deny}"))}


def _inst(contract, name="i1", **fields):
    return {"contract": contract, "instance": name, **fields}


RICH_SENDERS = ["OP", "SUP", "AG", "SUB", "M", "X", "T", "R[1]", "R[2]", "R[7]", "R", "GHOST"]
RICH_RECEIVERS = ["T", "U", "FREE", "AG", "M", "OP", "SUB", "R[2]", "NOBODY"]
RICH_NAMES = ["read", "write", "wipe", "bogus", "ping", "hello", "approve",
              "grant_write", "freeze"]

D_SUB = {"norm": "p_op_write", "holder": "SUB", "chain": ["OP", "AG", "SUB"]}
D_AG = {"norm": "p_op_write", "holder": "AG", "chain": ["OP", "AG"]}
D_BAD = [  # each fails one test of G.10
    {"norm": "p_op_write", "holder": "SUB", "chain": ["OP", "M", "SUB"]},     # M not in to:
    {"norm": "p_read", "holder": "SUB", "chain": ["AG", "SUB"]},              # not delegable
    {"norm": "p_op_write", "holder": "AG", "chain": ["OP", "SUB", "AG", "AG"]},  # too deep
    {"norm": "p_op_write", "holder": "SUB", "chain": ["AG", "SUB"]},          # not the bearer
    {"norm": "p_op_write", "holder": "AG", "chain": ["OP", "SUB"]},           # holder not last
]

RICH_INSTANCE_SETS = [
    None, [], [_inst("C1")], [_inst("C2")], [_inst("C1"), _inst("C2")],
    [_inst("C1", bindings={"i": 2}), _inst("C2")],
    [_inst("C1", bindings={"i": 2}, in_force={"r_write": True, "no_r_wipe": True}), _inst("C2")],
    [_inst("C1", bindings={"i": 2}, in_force={"r_write": True, "no_r_wipe": False}), _inst("C2")],
    [_inst("C1", bindings={"i": 1}, in_force={"r_write": True}),
     _inst("C1", "i2", bindings={"i": 2}, in_force={"r_write": False, "frozen": False,
                                                    "may_report": True}), _inst("C2")],
    [_inst("C1", in_force={"frozen": True, "may_report": True}), _inst("C2")],
    [_inst("C1", in_force={"frozen": False, "may_report": False, "no_r_wipe": False}),
     _inst("C2")],
    [_inst("C1", in_force={"frozen": False, "may_report": True}, derived=[D_SUB, D_AG])],
    [_inst("C1", in_force={"frozen": True, "may_report": True}, derived=[D_SUB, D_AG]),
     _inst("C2")],
    [_inst("C1", derived=D_BAD), _inst("C2")],
    [_inst("C1", bindings={"i": 7}, in_force={"no_r_wipe": True, "r_write": True},
           derived=[D_SUB]), _inst("C2")],
    [_inst("C1", bindings={"i": 3}), _inst("C1", "i2", bindings={"i": 1},
                                           in_force={"no_r_wipe": False})],
    [_inst("C2"), _inst("C2", "i2")],
]


def rich_inputs() -> list[dict]:
    out = []
    for s in RICH_SENDERS:
        for r in RICH_RECEIVERS:
            for n in RICH_NAMES:
                for instances in RICH_INSTANCE_SETS:
                    inp = {"message": {"from": s, "to": r, "name": n}}
                    if instances is not None:
                        inp["instances"] = copy.deepcopy(instances)
                    out.append(inp)
    return out


# --- Random files -----------------------------------------------------------------

RANDOM_ACTORS = """
- {id: OP, role: operator, agent: {kind: human}}
- {id: SUP, role: supervisor, agent: {kind: human}}
- {id: AG, role: agent, agent: {kind: llm_agent}}
- {id: SUB, role: sub-agent, agent: {kind: llm_agent}}
- {id: T, role: tools, agent: {kind: tool_server, tools: [read, write, wipe]}}
- {id: U, role: more tools, agent: {kind: tool_server, tools: [read, write]}}
- {id: FREE, role: free tools, agent: {kind: tool_server, tools: [read]}}
- {id: M, role: monitor, agent: {kind: service}}
- {id: "R[1..3]", role: robots, agent: {kind: service}}
- {id: "W[1..N]", role: workers, agent: {kind: service}}
- {id: X, role: outsider}
"""
TOOLS = {"T": ["read", "write", "wipe"], "U": ["read", "write"], "FREE": ["read"]}
ORDINARY = ["ping", "hello", "report"]
HUMANS = ["OP", "SUP"]
RANDOM_SENDERS = ["OP", "SUP", "AG", "SUB", "M", "X", "T", "R[1]", "R[2]", "R[3]", "R[7]",
                  "W[1]", "W[5]", "W[0]", "R", "GHOST"]
RANDOM_RECEIVERS = ["T", "U", "FREE", "AG", "SUB", "M", "OP", "SUP", "R[2]", "W[4]", "NOBODY"]
RANDOM_NAMES = ["read", "write", "wipe", "ping", "hello", "report", "approve", "freeze",
                "stop", "bogus"]


def _q(text: str) -> str:
    return json.dumps(text)


def random_file(rng: random.Random) -> str:
    """The text of a random file: one to three enforced contracts, and with
    some probability a contract that is not enforced."""
    contracts = []
    k = rng.choice([1, 1, 2, 2, 3])
    for ci in range(k):
        contracts.append(_random_contract(rng, f"C{ci + 1}", enforced=True))
    if rng.random() < .25:
        contracts.append(_random_contract(rng, "P", enforced=False))
    body = "contracts:\n" + "".join(textwrap.indent(c, "  ") for c in contracts)
    return ("sos:\n  name: Random\n  extensions:\n    - norms: 0.1\n    - agents: 0.1\n"
            "  actors:\n" + textwrap.indent(textwrap.dedent(RANDOM_ACTORS).strip() + "\n", "    ")
            + textwrap.indent(body, "  "))


def _random_contract(rng: random.Random, cid: str, enforced: bool) -> str:
    pool = ["OP", "SUP", "SUB", "T", "U", "M"]
    parties = ["AG"] + [p for p in pool if rng.random() < .6]
    robots = rng.choice([None, "R[i]", "R[*]", "R[1..2]", "R[2]"])
    if robots:
        parties.append(robots)
    if rng.random() < .3:
        parties.append(rng.choice(["W[*]", "W[j]"]))
    rng.shuffle(parties)
    tools = [p for p in parties if p in TOOLS]
    agents = [p for p in parties if p not in TOOLS]

    def senders():
        out = list(agents)
        out = [a for a in out if a not in ("R[*]", "R[1..2]", "W[*]")]
        if "W[*]" in parties:
            out.append("W[3]")
        if robots in ("R[*]", "R[1..2]"):
            out += ["R[1]", "R[2]"]
        return out

    def message(sender: str, receivers=None) -> str:
        r = rng.choice(receivers or parties)
        name = rng.choice(TOOLS[r]) if r in TOOLS else rng.choice(ORDINARY)
        return f"{sender} -> {r} : {name}"

    lines = [f"- id: {cid}", f"  parties: [{', '.join(_q(p) for p in parties)}]"]
    if enforced:
        lines.append("  enforcement: " + rng.choice(
            ["{}", "{default: allow}", "{default: deny}", "{point: caller}"]))
    norms, approvers, delegations = [], [], []
    for k in range(rng.randint(1, 6)):
        kind = rng.choice(["permission", "prohibition"])
        bearer = rng.choice(senders())
        nid = f"{'p' if kind == 'permission' else 'n'}{k}"
        entry = [f"- id: {nid}", f"  kind: {kind}", f"  bearer: {_q(bearer)}",
                 f"  content: {_q(message(bearer))}"]
        static = "[i]" not in bearer and "[j]" not in bearer
        x = rng.random() if "OP" in parties else 1
        if x < .15:
            entry.append(f"  trigger: {_q(message('OP', [a for a in agents if a != 'OP'] or None))}")
            static = False
        elif x < .3:
            entry.append(f"  until: {_q(message('OP', [a for a in agents if a != 'OP'] or None))}")
            static = False
        elif x < .35 and kind == "prohibition":
            entry.append("  deadline: 10s")
            static = False
        if enforced and kind == "prohibition" and rng.random() < .55:
            od = []
            humans = [h for h in HUMANS if h in parties and h != bearer]
            if humans and rng.random() < .8:
                approver = rng.choice(humans)
                approvers.append(approver)
                od += [f"escalate_to: {approver}", f"timeout: {rng.choice(['1s', '5min'])}"]
            if rng.random() < .5 and "[" not in bearer:
                r = rng.choice(["T", "U", "AG", "M"])
                name = rng.choice(TOOLS[r]) if r in TOOLS else "report"
                content = entry[3]
                if f"-> {r} : {name}" not in content:
                    od.append(f"fallback: {_q(f'{bearer} -> {r} : {name}')}")
            if rng.random() < .4:
                od.append(f"notify: {rng.choice([p for p in parties if p in HUMANS + ['M']] or ['AG'])}")
            if od:
                entry.append("  on_denied: { " + ", ".join(od) + " }")
        if enforced and kind == "permission" and "[" not in bearer and rng.random() < .4:
            to = [a for a in ["AG", "SUB", "M", "R[*]"] if a != bearer and rng.random() < .6]
            if to:
                delegations.append(
                    f"  - {{ norm: {nid}, to: [{', '.join(_q(t) for t in to)}], "
                    f"grant: grant_{nid}, revoke: revoke_{nid}, "
                    f"max_depth: {rng.choice([1, 2, 3])} }}")
        del static
        norms.append("\n".join(entry))
    rules = [f"  - {{ when: \"{a} -> AG : approve\", creates: approval }}"
             for a in dict.fromkeys(approvers)]
    if rules:
        lines.append("  counts_as:")
        lines += rules
    lines.append("  norms:")
    lines += [textwrap.indent(n, "    ") for n in norms]
    if delegations:
        lines.append("  delegations:")
        lines += delegations
    return "\n".join(lines) + "\n"


def random_valid_file(rng: random.Random, tries: int = 200) -> str:
    for _ in range(tries):
        text = random_file(rng)
        try:
            sos = parse(text)
        except Exception:
            continue
        if type_check(sos).ok:
            return text
    raise AssertionError("no valid random file")


def random_instance_set(rng: random.Random, policy: dict):
    """None (no `instances`) or a list of running instances with random
    bindings, in-force flags and derived entries."""
    if rng.random() < .2:
        return None
    out = []
    actors = ["OP", "SUP", "AG", "SUB", "M", "R[1]", "R[2]", "W[3]", "X"]
    for c in policy["contracts"]:
        for k in range(rng.choice([0, 1, 1, 1, 2])):
            entry = {"contract": c["id"], "instance": f"{c['id']}.{k}"}
            if rng.random() < .7:
                entry["bindings"] = {v: rng.choice([1, 2, 3, 7]) for v in ("i", "j")
                                     if rng.random() < .8}
            flags = {nid: rng.choice([True, False]) for nid in c["non_static"]
                     if rng.random() < .7}
            if flags:
                entry["in_force"] = flags
            derived = []
            perms = [n for n in c["norms"] if n["kind"] == "permission"]
            for _ in range(rng.choice([0, 0, 1, 2, 3])):
                if not perms:
                    break
                n = rng.choice(perms)
                d = c["delegations"].get(n["id"])
                if d and rng.random() < .7:
                    # mostly valid chains: bearer, then actors of to:
                    tos = [t["text"] for t in d["to"]]
                    tos = [t.replace("[*]", "[2]") for t in tos]
                    chain = [n["bearer"]["text"]] + [
                        rng.choice(tos) for _ in range(rng.randint(1, d["max_depth"] + 1))]
                else:
                    chain = [rng.choice(actors) for _ in range(rng.randint(1, 3))]
                holder = chain[-1] if rng.random() < .85 else rng.choice(actors)
                derived.append({"norm": n["id"], "holder": holder, "chain": chain})
            if derived:
                entry["derived"] = derived
            out.append(entry)
    rng.shuffle(out)
    return out


def random_inputs(rng: random.Random, policy: dict, n_sets: int) -> list[dict]:
    sets = [random_instance_set(rng, policy) for _ in range(n_sets)]
    out = []
    for s in RANDOM_SENDERS:
        for r in RANDOM_RECEIVERS:
            for n in RANDOM_NAMES:
                for instances in sets:
                    inp = {"message": {"from": s, "to": r, "name": n}}
                    if instances is not None:
                        inp["instances"] = instances
                    out.append(inp)
    return out


# --- Cedar ----------------------------------------------------------------------

def outcome(decision: str) -> str:
    """Cedar has two outcomes: deny stands for deny and escalate."""
    return "allow" if decision == "allow" else "deny"


def static_only(policy: dict) -> dict:
    """The contracts without their norms that are not static: what the
    Cedar target implements (G.10)."""
    out = copy.deepcopy(policy)
    for c in out["contracts"]:
        c["norms"] = [n for n in c["norms"] if n["static"]]
    return out


def cedar_entities(table: dict, messages) -> list:
    """The entities of the side table, and for a set declared with a
    symbolic bound, every member that a message names, with its parent."""
    out = list(table["entities"]["actors"])
    known = set()
    for s in table["entities"]["sets"]:
        out.append({"uid": s["uid"], "attrs": {}, "parents": []})
        for m in s.get("members") or []:
            out.append(m)
            known.add(m["uid"]["id"])
    return out, known


def _members_of_unbounded(policy: dict, actor: str):
    a = parse_concrete_actor(actor)
    if a is None or a["index"] is None:
        return None
    d = policy["actors"].get(a["name"])
    if d is None or not d["indexed"] or d["bounded"]:
        return None
    if d["lo"] is not None and a["index"] < d["lo"]:
        return None
    return {"uid": {"type": "CADL::Actor", "id": actor}, "attrs": {},
            "parents": [{"type": "CADL::ActorSet", "id": a["name"]}]}


def cedar_answers(sos, policy: dict, messages: list[tuple], workdir: Path) -> dict:
    """Authorize each (sender, receiver, name) with the cedar CLI: the
    decision and the ids of the determining policies. All requests run in
    one `cedar run-tests` call: one test per request for the decision, one
    per request and policy for the determining policies (a test that
    expects a reason passes when that policy determines the answer)."""
    generate(sos, workdir, target="agent-cedar")
    table = json.loads((workdir / "agent_policy_table.json").read_text())
    policy_file = workdir / "agent_policy.cedar"
    parsed = subprocess.run([CEDAR, "check-parse", "--policies", str(policy_file)],
                            capture_output=True, text=True, timeout=120)
    assert parsed.returncode == 0, parsed.stderr
    entities, known = cedar_entities(table, messages)
    for s, r, _ in messages:
        for a in (s, r):
            e = _members_of_unbounded(policy, a)
            if e is not None and a not in known:
                entities.append(e)
                known.add(a)
    pids = list(table["policies"])
    requests = [{"principal": f"CADL::Actor::{json.dumps(s)}",
                 "action": f"CADL::Action::{json.dumps(n)}",
                 "resource": f"CADL::Actor::{json.dumps(r)}", "context": {}}
                for s, r, n in messages]
    # First pass: the decisions; a test that expects allow fails on Deny.
    status = _run_tests(policy_file, workdir, [
        {"name": f"{k}", "request": req, "entities": entities,
         "decision": "allow", "reason": [], "num_errors": 0}
        for k, req in enumerate(requests)])
    decisions = []
    for k in range(len(messages)):
        res = status[f"{k}"]
        assert res in ("ok", "fail: expected Allow, got Deny"), res
        decisions.append("allow" if res == "ok" else "deny")
    # Second pass: with the decision right, a test that expects the reason
    # [p] passes exactly when p is one of the determining policies.
    status = _run_tests(policy_file, workdir, [
        {"name": f"{k}|{p}", "request": req, "entities": entities,
         "decision": decisions[k], "reason": [p], "num_errors": 0}
        for k, req in enumerate(requests) for p in pids])
    out = {}
    for k, m in enumerate(messages):
        reasons = []
        for p in pids:
            res = status[f"{k}|{p}"]
            assert res == "ok" or res.startswith("fail: missing reason"), res
            if res == "ok":
                reasons.append(p)
        out[m] = (decisions[k], reasons)
    return {"answers": out, "table": table}


def _run_tests(policy_file: Path, workdir: Path, tests: list) -> dict:
    """`cedar run-tests`: test name -> "ok" or the failure text."""
    (workdir / "tests.json").write_text(json.dumps(tests))
    proc = subprocess.run([CEDAR, "run-tests", "--policies", str(policy_file),
                           "--tests", str(workdir / "tests.json")],
                          capture_output=True, text=True, timeout=1800)
    status = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("test ") and " ... " in line:
            name, res = line[5:].split(" ... ", 1)
            status[name] = res
    assert len(status) == len(tests), proc.stdout[-2000:] + proc.stderr[-2000:]
    return status


def cedar_decides(policy: dict, table: dict, inp: dict, answer: tuple) -> tuple[bool, str]:
    """The rule of G.10: (True, clause) when the Cedar answer is the
    decision of G.5 for this input by that clause, (False, reason) where
    G.10 leaves the decision to the runtime, with the first reason that
    applies."""
    decision, reasons = answer
    m = inp["message"]
    sender, receiver = parse_concrete_actor(m["from"]), parse_concrete_actor(m["to"])
    name = m["name"]
    instances = inp.get("instances")
    roles = table["policies"]
    governing = [c for c in policy["contracts"]
                 if governs(policy, c, sender, receiver, name)]

    def running(cid):
        return instances is None or any(i["contract"] == cid for i in instances)

    tool_call = message_kind(policy, receiver, name) == "tool_call"
    # (a) for every input
    if decision == "allow" and "ungoverned" in reasons:
        return True, "a: ungoverned"
    if decision == "deny" and any(roles[p]["role"] == "undeclared_tool" for p in reasons):
        return True, "a: undeclared tool"
    # (b) a static prohibition of a contract with a running instance
    if decision == "deny" and any(
            roles[p]["role"] == "prohibition" and (tool_call or running(roles[p]["contract"]))
            for p in reasons):
        return True, "b: static prohibition"
    # (c) the assumptions of the Cedar target hold for this message
    if not all(running(c["id"]) for c in governing):
        return False, "a governing contract has no running instance"
    for c in governing:
        for n in c["norms"]:
            if n["static"] or n["name"] != name:
                continue
            if all(ref_match(policy, ref, actor, {}) is not False
                   for ref, actor in ((n["sender"], sender), (n["bearer"], sender),
                                      (n["receiver"], receiver))):
                return False, f"a {n['kind']} that is not static can match"
    for c in governing:
        for i in instances or []:
            if i["contract"] != c["id"]:
                continue
            for d in i.get("derived") or []:
                n = next((n for n in c["norms"] if n["id"] == d["norm"]), None)
                if n is not None and n["kind"] == "permission" and n["name"] == name \
                        and d["holder"] == m["from"]:
                    return False, "a derived instance can match"
    return True, "c: the assumptions hold"


def compare_cedar(sos, inputs: list[dict], workdir: Path) -> dict:
    """Compare the Cedar answers with the decision function. Returns
    counts; raises AssertionError with a reproducer on a discrepancy.

    Checked: (1) for every request, the Cedar answer is the decision of
    G.5 for the message alone on the contracts without their non-static
    norms; (2) for every input that G.10 says Cedar decides, the Cedar
    answer is the decision of G.5 for the full input. Counted: per reason
    for which G.10 leaves an input to the runtime, how many inputs there
    are and on how many the Cedar answer differs from the decision."""
    policy = build_policy(sos)
    messages = list(dict.fromkeys((i["message"]["from"], i["message"]["to"],
                                   i["message"]["name"]) for i in inputs))
    res = cedar_answers(sos, policy, messages, workdir)
    answers, table = res["answers"], res["table"]
    reduced = static_only(policy)
    counts = {"requests": len(messages), "inputs": len(inputs), "decided": 0,
              "not_decided": 0, "not_decided_differ": 0, "clauses": {}, "left": {},
              "left_differ": {}}
    for m in messages:
        want = outcome(decide(reduced, {"message": dict(zip(("from", "to", "name"), m))})
                       ["decision"])
        assert answers[m][0] == want, ("semantics of the Cedar target", m, answers[m], want)
    for inp in inputs:
        m = (inp["message"]["from"], inp["message"]["to"], inp["message"]["name"])
        answer = answers[m]
        full = decide(policy, inp)
        decided, why = cedar_decides(policy, table, inp, answer)
        differ = outcome(full["decision"]) != answer[0]
        if not decided:
            counts["not_decided"] += 1
            counts["not_decided_differ"] += differ
            counts["left"][why] = counts["left"].get(why, 0) + 1
            counts["left_differ"][why] = counts["left_differ"].get(why, 0) + differ
            continue
        counts["decided"] += 1
        counts["clauses"][why] = counts["clauses"].get(why, 0) + 1
        assert not differ, ("Cedar decides by G.10 but differs", why, inp, answer, full)
    return counts


def add_counts(total: dict, c: dict) -> None:
    for k, v in c.items():
        if isinstance(v, dict):
            add_counts(total.setdefault(k, {}), v)
        else:
            total[k] = total.get(k, 0) + v


# --- OPA --------------------------------------------------------------------------

BATCH = """package cadl_batch

import rego.v1

results := [one(c) | some c in input.cases]

one(c) := {"decision": d, "result": r} if {
\td := data.cadl.agent_policy.decision with input as c
\tr := data.cadl.agent_policy.result with input as c
} else := {"decision": d, "result": null} if {
\td := data.cadl.agent_policy.decision with input as c
}
"""


def compare_opa(sos, inputs: list[dict], workdir: Path) -> int:
    """Evaluate every input with one `opa eval` call and compare the full
    result with the decision function."""
    generate(sos, workdir, target="agent-rego")
    (workdir / "batch.rego").write_text(BATCH)
    (workdir / "cases.json").write_text(json.dumps({"cases": inputs}))
    proc = subprocess.run(
        [OPA, "eval", "--format", "json", "--data", str(workdir / "agent_policy.rego"),
         "--data", str(workdir / "batch.rego"), "--input", str(workdir / "cases.json"),
         "data.cadl_batch.results"], capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    values = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
    assert len(values) == len(inputs)
    policy = build_policy(sos)
    for inp, value in zip(inputs, values):
        want = decide(policy, inp)
        assert value["result"] == want and value["decision"] == want["decision"], (
            "OPA differs", inp, value, want)
    return len(inputs)


def run_random(seed: int, n_files: int, n_sets: int, cedar=True, opa=True) -> dict:
    rng = random.Random(seed)
    totals: dict = {"files": 0, "cedar": {}, "opa_inputs": 0}
    for _ in range(n_files):
        text = random_valid_file(rng)
        sos = parse(text)
        policy = build_policy(sos)
        inputs = random_inputs(rng, policy, n_sets)
        totals["files"] += 1
        with tempfile.TemporaryDirectory() as d:
            try:
                if cedar and CEDAR:
                    add_counts(totals["cedar"], compare_cedar(sos, inputs, Path(d) / "cedar"))
                if opa and OPA:
                    totals["opa_inputs"] += compare_opa(sos, inputs, Path(d) / "opa")
            except AssertionError as e:
                raise AssertionError(f"{e}\n--- file ---\n{text}") from None
    return totals


if __name__ == "__main__":
    import sys
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    files = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    print(run_random(seed, files, 4))
