"""Code target ``agent-cedar``: Cedar policies for the statically decidable
part of the boundary policy of a file (Appendix G, G.10), with the side
table from which a runtime computes the other fields of the result.

The mapping follows the informative mapping of G.10: undeclared tools and
prohibitions become ``forbid`` policies, permissions ``permit`` policies,
and the default for tool calls is the default denial of Cedar. Cedar has
two outcomes, so ``escalate`` is returned as a denial and resolved by the
runtime from the table. Norms that are not static, derived permissions
and contract instances are not covered; the generated file says so.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...agents import build_policy
from ...ast_nodes import SoSDefinition

POLICY_FILENAME = "agent_policy.cedar"
TABLE_FILENAME = "agent_policy_table.json"
NAMESPACE = "CADL"


def generate_agent_cedar(sos: SoSDefinition, output_dir: Path) -> None:
    """Write ``agent_policy.cedar`` and ``agent_policy_table.json``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    text, table = generate_agent_cedar_text(sos)
    (output_dir / POLICY_FILENAME).write_text(text, encoding="utf-8")
    (output_dir / TABLE_FILENAME).write_text(
        json.dumps(table, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- Cedar expressions ----------------------------------------------------------

def _actor(text: str) -> str:
    return f"{NAMESPACE}::Actor::{json.dumps(text, ensure_ascii=False)}"


def _actor_set(name: str) -> str:
    return f"{NAMESPACE}::ActorSet::{json.dumps(name, ensure_ascii=False)}"


def _action(name: str) -> str:
    return f"{NAMESPACE}::Action::{json.dumps(name, ensure_ascii=False)}"


def _ref_cond(ref: dict, var: str) -> str:
    """A reference of the file as a test on `principal` or `resource`.
    Only `==`, `in` and set membership are used, and no attribute is read,
    so the test cannot raise an evaluation error."""
    kind = ref["kind"]
    if kind == "plain":
        return f"{var} == {_actor(ref['name'])}"
    if kind == "index":
        return f"{var} == {_actor(_member(ref['name'], ref['index']))}"
    if kind == "range":
        members = ", ".join(
            _actor(_member(ref["name"], i)) for i in range(ref["lo"], ref["hi"] + 1))
        return f"[{members}].contains({var})"
    # A wildcard, a range with a symbolic bound, or an index bound by the
    # contract instance (an unknown match, which counts): the whole set.
    return f"{var} in {_actor_set(ref['name'])}"


def _member(name: str, index: int) -> str:
    return f"{name}[{index}]"


def _any(conds: list[str]) -> str:
    conds = list(dict.fromkeys(conds))
    if not conds:
        return "false"
    if len(conds) == 1:
        return conds[0]
    return "(" + " || ".join(conds) + ")"


def _governed(contract: dict, is_tool_server: str | None) -> str:
    """Sender and receiver each match a reference in parties: (G.5); a
    message to a tool server that is a party is governed whatever its
    sender, so the test on the principal is dropped for it."""
    sender = _any([_ref_cond(p, "principal") for p in contract["parties"]])
    receiver = _any([_ref_cond(p, "resource") for p in contract["parties"]])
    if is_tool_server:
        return f"({receiver} && ({sender} || {is_tool_server}))"
    return f"({sender} && {receiver})"


def _server_ref(name: str, decl: dict) -> dict:
    return {"name": name, "kind": "all" if decl["indexed"] else "plain"}


def _norm_cond(norm: dict) -> str:
    """A static norm applies: the content matches and the sender is the bearer."""
    conds = [_ref_cond(norm["sender"], "principal")]
    if norm["bearer"]["text"] != norm["sender"]["text"]:
        conds.append(_ref_cond(norm["bearer"], "principal"))
    conds.append(f"action == {_action(norm['name'])}")
    conds.append(_ref_cond(norm["receiver"], "resource"))
    return " && ".join(conds)


def _policy(pid: str, effect: str, when: str | None = None,
            unless: str | None = None, comment: str = "") -> list[str]:
    out = [f"// {line}" for line in comment.splitlines() if line]
    out.append(f"@id({json.dumps(pid, ensure_ascii=False)})")
    out.append(f"{effect} (principal, action, resource)")
    if when:
        out.append(f"when {{ {when} }}")
    if unless:
        out.append(f"unless {{ {unless} }}")
    out[-1] += ";"
    return out


# --- Generator ------------------------------------------------------------------

def generate_agent_cedar_text(sos: SoSDefinition) -> tuple[str, dict]:
    """The Cedar policy text and the side table."""
    policy = build_policy(sos)
    contracts = policy["contracts"]
    servers = {name: d for name, d in policy["actors"].items()
               if d["kind"] == "tool_server"}
    is_tool_server = _any(
        [_ref_cond(_server_ref(name, d), "resource") for name, d in servers.items()])
    several = len(contracts) > 1

    def governed(c: dict) -> str:
        return _governed(c, is_tool_server if servers else None)

    blocks: list[list[str]] = []
    roles: dict[str, dict] = {}

    def emit(pid: str, role: str, effect: str, when=None, unless=None,
             comment: str = "", **info) -> None:
        blocks.append(_policy(pid, effect, when, unless, comment))
        roles[pid] = {"effect": effect, "role": role, **info}

    # Step 1 of G.5: a message that no enforced contract governs is allowed.
    governed_any = _any([governed(c) for c in contracts])
    emit("ungoverned", "ungoverned", "permit",
         unless=governed_any if contracts else None,
         comment="G.5, step 1: no enforced contract governs the message.")

    # Step 2: a call of an undeclared tool of a governed tool server.
    for name, decl in servers.items():
        governing = [c for c in contracts
                     if any(p["name"] == name for p in c["parties"])]
        if not governing:
            continue
        tools = ", ".join(_action(t) for t in decl["tools"])
        emit(f"undeclared_tool.{name}", "undeclared_tool", "forbid",
             when=(_ref_cond(_server_ref(name, decl), "resource") + " && "
                   + _any([governed(c) for c in governing])),
             unless=f"[{tools}].contains(action)",
             comment=f"G.5, step 2: a call of an undeclared tool of {name}.",
             tool_server=name)

    for c in contracts:
        party_texts = {p["text"] for p in c["parties"]}
        static_permissions = []
        for n in c["norms"]:
            if not n["static"]:
                continue
            cond = _norm_cond(n)
            if n["kind"] == "permission":
                static_permissions.append(cond)
            # The norm of a contract applies only to a message it governs.
            if not {n["sender"]["text"], n["receiver"]["text"]} <= party_texts:
                cond = f"{cond} && {governed(c)}"
            emit(f"{c['id']}.{n['id']}", n["kind"],
                 "forbid" if n["kind"] == "prohibition" else "permit",
                 when=cond,
                 comment=f"{c['id']}.{n['id']}: {n['kind']} (static)\n"
                         f"{' '.join(n['content'].split())}",
                 contract=c["id"], norm=n["id"])

        default = c["default_decision"]
        if default == "allow":
            emit(f"default.{c['id']}", "default", "permit", when=governed(c),
                 comment=f"{c['id']}: default: allow.", contract=c["id"])
        elif default is None:
            emit(f"default.{c['id']}", "default", "permit", when=governed(c),
                 unless=is_tool_server if servers else None,
                 comment=f"{c['id']}: default: omitted. Other messages than tool "
                         f"calls are allowed;\ntool calls without a permission fall "
                         f"to the default denial of Cedar.",
                 contract=c["id"])
        # With several enforced contracts a message has to be allowed by
        # every contract that governs it; a permit of another contract must
        # not allow what this contract denies by default.
        if several and default != "allow":
            when = governed(c)
            if default is None:
                when += f" && {is_tool_server}"
            emit(f"closed.{c['id']}", "closed", "forbid",
                 when=when, unless=_any(static_permissions) if static_permissions else None,
                 comment=f"{c['id']}: the default denial of this contract, as a forbid, "
                         f"because the file has\nseveral enforced contracts (deny "
                         f"overrides, G.5 step 4).",
                 contract=c["id"])

    non_static = [f"{c['id']}.{nid}" for c in contracts for nid in c["non_static"]]
    no_effect = [f"{c['id']}.{nid}" for c in contracts for nid in c["no_effect"]]
    delegable = [f"{c['id']}.{nid}" for c in contracts for nid in c["delegations"]]

    table = {
        "generated_by": "cadl codegen -t agent-cedar",
        "extension": "agents 0.1",
        "sos": policy["sos"],
        "cedar": {
            "principal": f"{NAMESPACE}::Actor", "resource": f"{NAMESPACE}::Actor",
            "action": f"{NAMESPACE}::Action", "actor_set": f"{NAMESPACE}::ActorSet",
        },
        "contracts": {
            c["id"]: {"default": c["default_decision"], "point": c["point"]}
            for c in contracts
        },
        "policies": roles,
        # Per prohibition (G.10): what the runtime needs to compute the
        # reason, the norms and the escalation data of a denial.
        "prohibitions": {
            f"{c['id']}.{n['id']}": {
                "contract": c["id"], "norm": n["id"], "static": n["static"],
                "content": n["content"],
                "escalate_to": n["escalate_to"], "timeout": n["timeout_ms"],
                "fallback": n["fallback"], "notify": n["notify"],
            }
            for c in contracts for n in c["norms"] if n["kind"] == "prohibition"
        },
        "permissions": {
            f"{c['id']}.{n['id']}": {
                "contract": c["id"], "norm": n["id"], "static": n["static"],
                "content": n["content"],
            }
            for c in contracts for n in c["norms"] if n["kind"] == "permission"
        },
        "non_static": non_static,
        "no_effect": no_effect,
        "delegations": {
            f"{c['id']}.{nid}": {
                "to": [r["text"] for r in d["to"]], "grant": d["grant"],
                "revoke": d["revoke"], "max_depth": d["max_depth"], "until": d["until"],
            }
            for c in contracts for nid, d in c["delegations"].items()
        },
        "entities": _entities(policy),
    }

    lines = _readme(policy, non_static, no_effect, delegable, several)
    for block in blocks:
        lines.append("")
        lines.extend(block)
    lines.append("")
    return "\n".join(lines), table


def _entities(policy: dict) -> dict:
    """The entities the policies refer to: single actors, and the sets
    whose members must be stored with the set as a parent."""
    singles, sets = [], []
    for name, d in policy["actors"].items():
        if not d["indexed"]:
            singles.append({"uid": {"type": f"{NAMESPACE}::Actor", "id": name},
                            "attrs": {}, "parents": []})
            continue
        entry = {"uid": {"type": f"{NAMESPACE}::ActorSet", "id": name},
                 "member_id": f"{name}[<index>]",
                 "members": None}
        if d["bounded"]:
            entry["members"] = [
                {"uid": {"type": f"{NAMESPACE}::Actor", "id": _member(name, i)},
                 "attrs": {},
                 "parents": [{"type": f"{NAMESPACE}::ActorSet", "id": name}]}
                for i in range(d["lo"], d["hi"] + 1)
            ]
        sets.append(entry)
    return {"actors": singles, "sets": sets}


def _readme(policy, non_static, no_effect, delegable, several) -> list[str]:
    def listed(items):
        return [f"//   {i}" for i in items] or ["//   (none)"]

    out = [
        "// README",
        "//",
        "// Cedar policies generated by `cadl codegen -t agent-cedar` from the SoS",
        f"// {' '.join(policy['sos'].split())} (CADL agent governance extension, agents 0.1,",
        "// Appendix G of the specification, G.10).",
        "//",
        "// Request: principal = CADL::Actor::\"<sender>\", action =",
        "// CADL::Action::\"<message name>\", resource = CADL::Actor::\"<receiver>\".",
        "// A member of an actor set is CADL::Actor::\"NAME[3]\" and MUST be stored",
        "// with the parent CADL::ActorSet::\"NAME\"; a policy about the set does not",
        f"// match a member stored without it. {TABLE_FILENAME} lists the",
        "// entities under \"entities\".",
        "//",
        "// Mapping. The mapping follows the authorization semantics of Cedar as",
        "// documented in the Cedar Policy Language Reference Guide",
        "// (https://docs.cedarpolicy.com/auth/authorization.html): default deny,",
        "// forbid overrides permit, and a policy whose evaluation errors is skipped.",
        "//   - a call of an undeclared tool          -> forbid",
        "//   - a static prohibition                  -> forbid",
        "//   - a static permission                   -> permit",
        "//   - a tool call without a permission      -> no policy: default deny",
        "//   - other messages, default: not deny     -> permit (default.<contract>)",
        "//   - a message no enforced contract governs -> permit (ungoverned)",
        "// A message to a tool server that is a party of an enforced contract is",
        "// governed whatever its principal: a party, another actor, or an actor the",
        "// file does not declare.",
    ]
    if several:
        out += [
            "//   - the file has several enforced contracts: the default denial of each",
            "//     is also a forbid (closed.<contract>), so that a permit of one",
            "//     contract does not allow what another contract denies (deny overrides)",
        ]
    out += [
        "// Because a forbid that errors would be skipped and would not deny, the",
        "// policies read no attribute: they use only ==, in and set membership on",
        "// principal, action and resource, which do not raise evaluation errors.",
        "//",
        "// Cedar has two outcomes. Where the decision function of G.5 gives",
        "// escalate, these policies give Deny; the runtime reads the determining",
        f"// policies of the response and resolves the escalation from {TABLE_FILENAME}",
        "// (per prohibition: escalate_to, timeout, fallback, notify; per policy id:",
        "// its role, contract and norm).",
        "//",
        "// NOT covered by these policies; a runtime is needed for them:",
        "//   - Norms that are not static (G.10): their in-force flag is state of the",
        "//     runtime. A prohibition among them is NOT enforced here, and a",
        "//     permission among them allows nothing here. They are:",
    ]
    out += ["//  " + line[2:] for line in listed(non_static)]
    out += [
        "//   - Derived permissions (delegations, G.7): a holder of a derived instance",
        "//     is denied here by default. Delegable permissions:",
    ]
    out += ["//  " + line[2:] for line in listed(delegable)]
    out += [
        "//   - Contract instances: one running instance of each enforced contract is",
        "//     assumed, with no index bound (no reason no_instance).",
        "//   - Holds, release, expiry (G.6), grants and revocations (G.7), audit",
        "//     records (G.8).",
        "// These policies decide a message as the decision function of G.5 decides",
        "// an input with only `message` on the contracts without the norms that are",
        "// not static. Their answer (Deny for deny and escalate) is therefore the",
        "// decision for a policy input of G.10 in these cases, and only in these:",
        "//   (a) Allow by `ungoverned`, or Deny by `undeclared_tool.<server>`: for",
        "//       every input;",
        "//   (b) Deny by the forbid of a static prohibition: when its contract has a",
        "//       running instance or the message is a tool call (a contract without",
        "//       a running instance decides another message by its default);",
        "//   (c) any answer: when every enforced contract that governs the message",
        "//       has a running instance, no norm of these contracts that is not",
        "//       static can match the message (content and bearer; an index bound",
        "//       by the instance counts as a match), and no derived instance of these",
        "//       contracts is of a permission of this message name held by the sender.",
        "// In every other case a runtime decides with the reference decision function",
        "// (`cadl agents-decide`, or the policy of `-t agent-rego`).",
    ]
    if no_effect:
        out += [
            "//",
            "// Permissions and prohibitions whose content is not a message event have",
            "// no effect at the boundary (G.5):",
        ] + listed(no_effect)
    out += [
        "//",
        "// The policies are generated for one version of the contracts; an amendment",
        "// requires new policies.",
    ]
    return out
