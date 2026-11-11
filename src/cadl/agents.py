"""Agent governance extension (Appendix G, agents 0.1).

Helpers shared by `cadl check`, the IR, the code generators and the
reference decision function:

- whether the extension is declared (G-1: it requires ``norms: 0.1``);
- message events and actor references as the boundary reads them (G.4);
- which norms are static (G.10);
- the *boundary policy* of a file: the enforced contracts reduced to what
  a decision needs, as plain JSON-serializable data;
- :func:`decide`, the decision function of G.5 over the policy input of
  G.10.

The decision function is stateless. Holds, release and expiry (G.6), the
bookkeeping of grants, chains and revocations (G.7) and audit records
(G.8) belong to a runtime; the in-force flags of the norms that are not
static and the valid derived instances come in through the input.
:mod:`cadl.agents_runtime` is the reference runtime: it replays a recorded
trace and calls :func:`decide` for every message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .ast_nodes import ActorDef, ContractDef, NormDef, RangeExpr, SoSDefinition

AGENTS_EXTENSION = ("agents", "0.1")
AGENT_KINDS = ("llm_agent", "tool_server", "human", "service")
ENFORCEMENT_DEFAULTS = ("deny", "allow")
ENFORCEMENT_POINTS = ("caller", "receiver", "gateway")
DEFAULT_POINT = "gateway"
# Institutional events of G.6, reserved in an enforced contract.
HOLD_EVENTS = ("approval", "rejection")

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HYPHEN_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*")
_MESSAGE_EVENT_RE = re.compile(r"^(.+?)\s*->\s*(.+?)\s*:\s*(.+)$", re.S)
_MESSAGE_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?$", re.S)
# A concrete actor of a message: `PLANNER` or `ROBOT[2]`.
_CONCRETE_ACTOR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(0|[1-9][0-9]*)\])?$")


# --- Declaration -------------------------------------------------------------

def agents_declared(sos: SoSDefinition) -> bool:
    """True when the file declares ``agents: 0.1`` under ``extensions:``."""
    return AGENTS_EXTENSION in sos.extensions


def agents_active(sos: SoSDefinition) -> bool:
    """True when the keys of the extension are interpreted: ``agents: 0.1``
    is declared together with ``norms: 0.1``, which it requires (G-1)."""
    from .norms import norms_declared
    return agents_declared(sos) and norms_declared(sos)


def agent_keys_used(sos: SoSDefinition) -> list[str]:
    """The keys of the extension that the file uses, in a fixed order."""
    used = []
    if any(a.agent is not None for a in sos.actors):
        used.append("agent:")
    if any(c.enforcement is not None for c in sos.contracts):
        used.append("enforcement:")
    if any(c.delegations or c.invalid_delegations is not None for c in sos.contracts):
        used.append("delegations:")
    if any(n.on_denied is not None for c in sos.contracts for n in c.norms):
        used.append("on_denied:")
    return used


def is_hyphen_name(text: str) -> bool:
    return bool(_HYPHEN_NAME_RE.fullmatch(text))


# --- Actor references and message events (G.4) -------------------------------

def parse_ref(text: str) -> dict:
    """An actor reference as data.

    ``kind`` is ``plain`` (``A``), ``index`` (``A[3]``), ``all`` (``A[*]``
    and a range with a symbolic bound, whose bounds are not compared),
    ``range`` (``A[1..3]``) or ``var`` (``A[i]``, an index bound by the
    contract instance).
    """
    from .parser import _parse_actor_ref_str
    from .unparse import actor_ref_to_source
    ref = _parse_actor_ref_str(text)
    out: dict[str, Any] = {"text": actor_ref_to_source(ref), "name": ref.name}
    index = ref.index
    if index is None:
        out["kind"] = "plain"
    elif isinstance(index, RangeExpr):
        if isinstance(index.end, int):
            out.update(kind="range", lo=index.start, hi=index.end)
        else:
            out["kind"] = "all"
    elif index == "*":
        out["kind"] = "all"
    elif isinstance(index, int) or str(index).isdigit():
        out.update(kind="index", index=int(index))
    else:
        out.update(kind="var", var=str(index))
    return out


def ref_text(text: str) -> str:
    """An actor reference in canonical form, for textual comparison."""
    return parse_ref(text)["text"]


@dataclass
class MessageEvent:
    """A `message_event` ``S -> R : e`` as the boundary reads it (G.4)."""
    sender: dict
    receiver: dict
    name: str | None        # None when `e` is not an identifier or a call
    has_args: bool
    text: str


def parse_message_event(text: str | None) -> MessageEvent | None:
    """Read a message event; None when the text is not one."""
    if not text:
        return None
    t = text.strip()
    m = _MESSAGE_EVENT_RE.match(t)
    if "->" not in t or not m:
        return None
    nm = _MESSAGE_NAME_RE.match(m.group(3).strip())
    return MessageEvent(
        sender=parse_ref(m.group(1).strip()),
        receiver=parse_ref(m.group(2).strip()),
        name=nm.group(1) if nm else None,
        has_args=bool(nm and nm.group(2) is not None and nm.group(2).strip()),
        text=t,
    )


def norm_message(contract: ContractDef, norm: NormDef) -> MessageEvent | None:
    """The content of a norm when it is a message event, else None. A
    content that is the name of an institutional event is not one (F.3)."""
    events = {r.creates for r in contract.counts_as if r.creates}
    if norm.content.strip() in events:
        return None
    return parse_message_event(norm.content)


def takes_part(contract: ContractDef, norm: NormDef) -> bool:
    """G.5: only a permission or a prohibition whose content is a message
    event takes part in a decision."""
    if norm.kind not in ("permission", "prohibition"):
        return False
    msg = norm_message(contract, norm)
    return msg is not None and msg.name is not None


def is_static(contract: ContractDef, norm: NormDef) -> bool:
    """G.10: a static norm is in force in every running instance."""
    if not takes_part(contract, norm):
        return False
    if norm.trigger or norm.until or norm.deadline_ms is not None \
            or norm.invalid_deadline is not None:
        return False
    for other in contract.norms:
        eff = other.effects
        if eff and norm.id in (eff.activate + eff.suspend + eff.resume + eff.discharge):
            return False
        if other.on_violation and other.on_violation.reparation == norm.id:
            return False
    msg = norm_message(contract, norm)
    refs = [parse_ref(norm.bearer), msg.sender, msg.receiver]
    if norm.counterparty:
        refs.append(parse_ref(norm.counterparty))
    od = norm.on_denied
    if od is not None:
        refs += [parse_ref(t) for t in (od.escalate_to, od.notify) if t]
        fb = parse_message_event(od.fallback)
        if fb is not None:
            refs += [fb.sender, fb.receiver]
    return all(r["kind"] != "var" for r in refs)


def ref_covered(ref: dict, parties: list, actors: dict | None = None) -> bool:
    """Whether every actor that an actor reference can name matches one of
    the party references (as `parse_ref` data), as G.5 reads `parties:`:
    a party `A[i]` or `A[*]` stands for every member of `A`. With the
    declared `actors` (as in the boundary policy), a reference to a whole
    set with integer bounds is read as the range of its declaration."""
    decl = (actors or {}).get(ref["name"])
    if ref["kind"] in ("all", "var") and decl and decl["bounded"]:
        ref = dict(ref, kind="range", lo=decl["lo"], hi=decl["hi"])
    for p in parties:
        if p["name"] != ref["name"]:
            continue
        if p["kind"] == "plain" or ref["kind"] == "plain":
            if p["kind"] == ref["kind"]:
                return True
        elif p["kind"] in ("all", "var"):
            return True
        elif p["kind"] == "index":
            if ref["kind"] == "index" and ref["index"] == p["index"]:
                return True
            if ref["kind"] == "range" and ref["lo"] == ref["hi"] == p["index"]:
                return True
        elif ref["kind"] == "index":
            if p["lo"] <= ref["index"] <= p["hi"]:
                return True
        elif ref["kind"] == "range" and p["lo"] <= ref["lo"] and ref["hi"] <= p["hi"]:
            return True
    return False


# --- Boundary policy ----------------------------------------------------------

def _actor_entry(actor: ActorDef) -> dict:
    index = actor.id.index
    entry: dict[str, Any] = {
        "indexed": index is not None,
        "bounded": False, "lo": None, "hi": None,
        "kind": None, "tools": [],
    }
    if isinstance(index, RangeExpr) and isinstance(index.end, int):
        entry.update(bounded=True, lo=index.start, hi=index.end)
    elif isinstance(index, RangeExpr) and isinstance(index.start, int):
        entry["lo"] = index.start       # symbolic upper bound: `A[1..N]`
    if actor.agent is not None:
        entry["kind"] = actor.agent.kind or None
        entry["tools"] = list(actor.agent.tools)
    return entry


def build_policy(sos: SoSDefinition) -> dict:
    """The boundary policy of a file: its actors and enforced contracts,
    reduced to what the decision function reads. Plain data, so that the
    generators can embed it. Requires the extension to be declared.
    """
    if not agents_active(sos):
        raise ValueError(
            "the file does not declare the agent governance extension: add "
            "'norms: 0.1' and 'agents: 0.1' under extensions: (Appendix G)"
        )
    contracts = []
    for c in sos.contracts:
        if c.enforcement is None:
            continue
        norms, non_static, no_effect = [], [], []
        for n in c.norms:
            if not takes_part(c, n):
                if n.kind in ("permission", "prohibition"):
                    no_effect.append(n.id)
                continue
            msg = norm_message(c, n)
            static = is_static(c, n)
            if not static:
                non_static.append(n.id)
            od = n.on_denied if n.kind == "prohibition" else None
            norms.append({
                "id": n.id,
                "kind": n.kind,
                "static": static,
                "content": msg.text,
                "bearer": parse_ref(n.bearer),
                "sender": msg.sender,
                "receiver": msg.receiver,
                "name": msg.name,
                "escalate": parse_ref(od.escalate_to) if od and od.escalate_to else None,
                "escalate_to": od.escalate_to if od else None,
                "timeout_ms": od.timeout_ms if od else None,
                "fallback": od.fallback.strip() if od and od.fallback else None,
                "notify": od.notify if od else None,
            })
        ids = {n["id"] for n in norms if n["kind"] == "permission"}
        delegations = {
            d.norm: {
                "to": [parse_ref(t) for t in d.to],
                "grant": d.grant,
                "revoke": d.revoke,
                "max_depth": d.max_depth,
                "until": d.until,
            }
            for d in c.delegations if d.norm in ids
        }
        contracts.append({
            "id": c.id,
            "parties": [_party_ref(p) for p in c.parties],
            "default_decision": c.enforcement.default,
            "point": c.enforcement.point or DEFAULT_POINT,
            "norms": norms,
            "delegations": delegations,
            "non_static": non_static,
            "no_effect": no_effect,
        })
    return {
        "sos": sos.name,
        "actors": {a.id.name: _actor_entry(a) for a in sos.actors},
        "contracts": contracts,
    }


def _party_ref(ref) -> dict:
    from .unparse import actor_ref_to_source
    return parse_ref(actor_ref_to_source(ref))


# --- Three-valued matching (G.4) ----------------------------------------------
# True, False, or None for unknown.

def kleene_and(*values):
    """False when any is False, otherwise unknown when any is unknown."""
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    return True


def parse_concrete_actor(text) -> dict | None:
    """A concrete actor of a message (`PLANNER`, `ROBOT[2]`), or None."""
    if not isinstance(text, str):
        return None
    m = _CONCRETE_ACTOR_RE.match(text)
    if not m:
        return None
    return {"name": m.group(1), "index": None if m.group(2) is None else int(m.group(2)),
            "text": text}


def is_member(policy: dict, name: str, actor: dict) -> bool:
    """Whether a concrete actor is the declared actor `name` or a member
    of the declared set `name`."""
    decl = policy["actors"].get(name)
    if decl is None or actor["name"] != name:
        return False
    if not decl["indexed"]:
        return actor["index"] is None
    if actor["index"] is None:
        return False
    if decl["bounded"]:
        return decl["lo"] <= actor["index"] <= decl["hi"]
    # A symbolic upper bound: every index from the lower bound on (G.4).
    return decl["lo"] is None or actor["index"] >= decl["lo"]


def ref_match(policy: dict, ref: dict, actor: dict, bindings: dict):
    """Match an actor reference against a concrete actor (table of G.4)."""
    if not is_member(policy, ref["name"], actor):
        return False
    kind, index = ref["kind"], actor["index"]
    if kind == "plain":
        return index is None
    if index is None:
        return False
    if kind == "index":
        return index == ref["index"]
    if kind == "all":
        return True
    if kind == "range":
        return ref["lo"] <= index <= ref["hi"]
    value = bindings.get(ref["var"])
    if value is None:
        return None
    return index == value


# --- Decision function (G.5, G.10) ---------------------------------------------

class AgentsInputError(ValueError):
    """The policy input does not have the form of G.10."""


def _check_input(inp) -> tuple[dict, dict, str, list | None]:
    if not isinstance(inp, dict):
        raise AgentsInputError("the policy input must be a JSON object")
    msg = inp.get("message")
    if not isinstance(msg, dict):
        raise AgentsInputError("the policy input needs 'message' with from, to and name")
    actors = []
    for key in ("from", "to"):
        actor = parse_concrete_actor(msg.get(key))
        if actor is None:
            raise AgentsInputError(
                f"message.{key} must be a concrete actor such as PLANNER or "
                f"ROBOT[2], got {msg.get(key)!r}"
            )
        actors.append(actor)
    name = msg.get("name")
    if not isinstance(name, str) or not name:
        raise AgentsInputError(f"message.name must be a message name, got {name!r}")
    now = inp.get("now")
    if now is not None and not isinstance(now, (str, int, float)):
        raise AgentsInputError("'now' must be a time stamp (text or number)")
    instances = inp.get("instances")
    if instances is None:
        return actors[0], actors[1], name, None
    if not isinstance(instances, list):
        raise AgentsInputError("'instances' must be a list")
    out = []
    for k, entry in enumerate(instances):
        where = f"instances[{k}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("contract"), str):
            raise AgentsInputError(f"{where} must be an object with 'contract'")
        bindings = entry.get("bindings") or {}
        in_force = entry.get("in_force") or {}
        derived = entry.get("derived") or []
        if not isinstance(bindings, dict) or any(
                isinstance(v, bool) or not isinstance(v, int) for v in bindings.values()):
            raise AgentsInputError(f"{where}.bindings must map index names to integers")
        if not isinstance(in_force, dict) or any(
                not isinstance(v, bool) for v in in_force.values()):
            raise AgentsInputError(f"{where}.in_force must map norm ids to true or false")
        if not isinstance(derived, list):
            raise AgentsInputError(f"{where}.derived must be a list")
        for d in derived:
            if not (isinstance(d, dict) and isinstance(d.get("norm"), str)
                    and isinstance(d.get("holder"), str)
                    and isinstance(d.get("chain"), list)
                    and all(isinstance(a, str) for a in d["chain"])):
                raise AgentsInputError(
                    f"each entry of {where}.derived needs norm, holder and chain")
        out.append({"contract": entry["contract"], "instance": entry.get("instance"),
                    "bindings": bindings, "in_force": in_force, "derived": derived})
    return actors[0], actors[1], name, out


def _in_force(norm: dict, inst: dict):
    """The in-force flag: a static norm is in force; for the others the
    flag comes from the input and a missing entry is unknown (G.10)."""
    if norm["static"]:
        return True
    return inst["in_force"].get(norm["id"])


def _applies(policy, norm, sender, receiver, name, inst):
    """G.5: a norm instance in force applies when its content matches the
    message and the sender of the message is its bearer."""
    if norm["name"] != name:
        return False
    b = inst["bindings"]
    return kleene_and(
        ref_match(policy, norm["sender"], sender, b),
        ref_match(policy, norm["receiver"], receiver, b),
        ref_match(policy, norm["bearer"], sender, b),
        _in_force(norm, inst),
    )


def _derived_applies(policy, contract, norm, entry, sender, receiver, name, inst) -> bool:
    """A derived instance of the input applies when the content matches
    with the holder as sender (G.5). The input lists the instances the
    runtime found valid (G.7); what can be read from the file is tested
    again here, and an entry that fails a test does not apply."""
    deleg = contract["delegations"].get(norm["id"])
    if deleg is None or entry["norm"] != norm["id"]:
        return False
    if entry["holder"] != sender["text"] or norm["name"] != name:
        return False
    b = inst["bindings"]
    if ref_match(policy, norm["receiver"], receiver, b) is not True:
        return False
    if _in_force(norm, inst) is not True:
        return False
    chain = entry["chain"]
    if len(chain) < 2 or chain[-1] != entry["holder"] or len(chain) - 1 > deleg["max_depth"]:
        return False
    actors = [parse_concrete_actor(a) for a in chain]
    if any(a is None for a in actors):
        return False
    if ref_match(policy, norm["bearer"], actors[0], b) is not True:
        return False
    return all(
        any(ref_match(policy, ref, a, {}) is True for ref in deleg["to"])
        for a in actors[1:]
    )


def _approver(norm: dict, inst: dict) -> str | None:
    """The concrete actor named by `escalate_to:`, or None when the norm
    has none or its index is not bound."""
    ref = norm["escalate"]
    if ref is None:
        return None
    if ref["kind"] == "plain":
        return ref["name"]
    if ref["kind"] == "index":
        return f"{ref['name']}[{ref['index']}]"
    if ref["kind"] == "var":
        value = inst["bindings"].get(ref["var"])
        return None if value is None else f"{ref['name']}[{value}]"
    return None


def _default(contract: dict, tool_call: bool) -> str:
    """Step 3.4: `default:` or, when omitted, deny for a tool call and
    allow for every other message."""
    if contract["default_decision"] is not None:
        return contract["default_decision"]
    return "deny" if tool_call else "allow"


def _result(decision, reason, contracts=(), norms=(), provenance=(), escalate_to=None,
            timeout=None, fallback=(), notify=()) -> dict:
    return {
        "decision": decision, "reason": reason,
        "contracts": list(contracts), "norms": list(norms),
        "provenance": list(provenance), "escalate_to": escalate_to,
        "timeout": timeout, "fallback": list(fallback), "notify": list(notify),
    }


def _unique(items) -> list:
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def message_kind(policy: dict, receiver: dict, name: str) -> str:
    """G.4: ``tool_call``, ``undeclared_tool`` or ``ordinary``."""
    decl = policy["actors"].get(receiver["name"])
    if (decl is None or decl["kind"] != "tool_server"
            or not is_member(policy, receiver["name"], receiver)):
        return "ordinary"
    return "tool_call" if name in decl["tools"] else "undeclared_tool"


def governs(policy: dict, contract: dict, sender: dict, receiver: dict, name: str) -> bool:
    """G.5: whether an enforced contract governs a message. A party
    reference with an index bound by the contract instance is an unknown
    match, which counts. A message to a tool server that is a party is
    governed whatever its sender, also when the sender is not a party or
    not a declared actor."""
    def party(actor):
        return any(ref_match(policy, ref, actor, {}) is not False
                   for ref in contract["parties"])

    to_tool_server = message_kind(policy, receiver, name) != "ordinary"
    return party(receiver) and (to_tool_server or party(sender))


def _running(contract: dict, instances: list | None) -> list:
    """The running instances of a contract in a checked input. Without
    `instances` in the input: one running instance, no index bound, no
    derived instance (G.10)."""
    if instances is None:
        return [{"contract": contract["id"], "instance": None,
                 "bindings": {}, "in_force": {}, "derived": []}]
    return [i for i in instances if i["contract"] == contract["id"]]


def _grounds(policy, contract, running, sender, receiver, name) -> list:
    """Step 3.2: the prohibitions of a contract that apply to the message,
    each with the running instances in which it applies. Unknown is
    fail-closed: the prohibition applies."""
    out = []
    for n in contract["norms"]:
        if n["kind"] != "prohibition":
            continue
        where = [i for i in running
                 if _applies(policy, n, sender, receiver, name, i) is not False]
        if where:
            out.append((n, where))
    return out


def applying_prohibitions(policy: dict, inp: Any) -> list[dict]:
    """The prohibitions that apply to the message of a policy input, over
    all governing contracts and whatever the decision is: per prohibition
    its contract, its norm entry of the policy, and the names of the
    running instances in which it applies. In the order of declaration.

    :func:`decide` lists the grounds of the determining contracts only; a
    runtime needs all of them when it creates a hold and when it re-checks
    a released message (G.6)."""
    sender, receiver, name, instances = _check_input(inp)
    out = []
    for c in policy["contracts"]:
        if not governs(policy, c, sender, receiver, name):
            continue
        for n, where in _grounds(policy, c, _running(c, instances),
                                 sender, receiver, name):
            out.append({"contract": c["id"], "norm": n,
                        "instances": [i["instance"] for i in where]})
    return out


def decide(policy: dict, inp: Any) -> dict:
    """The decision function of G.5 for one message.

    `policy` is the result of :func:`build_policy`; `inp` is the policy
    input of G.10. Returns the result object of G.10. Raises
    :class:`AgentsInputError` for an input that does not have that form.
    """
    sender, receiver, name, instances = _check_input(inp)

    kind = message_kind(policy, receiver, name)
    tool_server = kind != "ordinary"
    tool_call = kind == "tool_call"

    # Step 1: ungoverned.
    governing = [c for c in policy["contracts"]
                 if governs(policy, c, sender, receiver, name)]
    if not governing:
        return _result("allow", "ungoverned")

    # Step 2: undeclared tool.
    if tool_server and not tool_call:
        return _result("deny", "undeclared_tool")

    # Step 3: each governing contract decides over its running instances.
    outcomes = []
    for c in governing:
        running = _running(c, instances)
        out = {"id": c["id"], "norms": [], "grounds": [], "provenance": [],
               "approvers": set()}
        outcomes.append(out)
        if not running:
            if tool_call:
                out.update(decision="deny", reason="no_instance")
            else:
                out.update(decision=_default(c, tool_call), reason="default")
            continue
        # 3.2 Prohibitions: unknown is fail-closed, the prohibition applies.
        escalable = True
        for n, where in _grounds(policy, c, running, sender, receiver, name):
            out["grounds"].append(n)
            for inst in where:
                approver = _approver(n, inst)
                if approver is None:
                    escalable = False
                else:
                    out["approvers"].add(approver)
        if out["grounds"]:
            out["reason"] = "prohibition"
            out["norms"] = [n["id"] for n in out["grounds"]]
            approvers = out["approvers"]
            if escalable and len(approvers) == 1 and sender["text"] not in approvers:
                out["decision"] = "escalate"
            else:
                out["decision"] = "deny"
            continue
        # 3.3 Permissions, original or derived: unknown does not apply.
        for n in c["norms"]:
            if n["kind"] != "permission":
                continue
            applied = any(_applies(policy, n, sender, receiver, name, i) is True
                          for i in running)
            for inst in running:
                for entry in inst["derived"]:
                    if _derived_applies(policy, c, n, entry, sender, receiver, name, inst):
                        applied = True
                        out["provenance"].append(list(entry["chain"]))
            if applied:
                out["norms"].append(n["id"])
        if out["norms"]:
            out.update(decision="allow", reason="permission")
        else:
            out.update(decision=_default(c, tool_call), reason="default")

    # Step 4: combination, deny overrides.
    denying = [o for o in outcomes if o["decision"] == "deny"]
    escalating = [o for o in outcomes if o["decision"] == "escalate"]
    escalate_to = None
    if denying:
        decision, determining = "deny", denying
    elif escalating:
        determining = escalating
        approvers = set().union(*(o["approvers"] for o in escalating))
        if len(approvers) == 1:
            decision, escalate_to = "escalate", next(iter(approvers))
        else:
            decision = "deny"    # a hold has a single approver
    else:
        decision, determining = "allow", outcomes

    reasons = {o["reason"] for o in determining}
    if decision == "allow":
        reason = "permission" if "permission" in reasons else "default"
    else:
        reason = next(r for r in ("no_instance", "prohibition", "default") if r in reasons)

    norms, provenance, grounds = [], [], []
    if reason in ("prohibition", "permission"):
        for o in determining:
            if o["reason"] != reason:
                continue
            norms += [f"{o['id']}.{nid}" for nid in o["norms"]]
            provenance += o["provenance"]
            grounds += o["grounds"]
    timeouts = [g["timeout_ms"] for g in grounds if g["timeout_ms"] is not None]
    return _result(
        decision, reason,
        contracts=[o["id"] for o in determining],
        norms=norms,
        provenance=_unique(provenance),
        escalate_to=escalate_to,
        timeout=min(timeouts) if timeouts else None,
        fallback=_unique(g["fallback"] for g in grounds if g["fallback"]),
        notify=_unique(g["notify"] for g in grounds if g["notify"]),
    )


def decide_message(sos: SoSDefinition, inp: Any) -> dict:
    """:func:`decide` for a parsed file."""
    return decide(build_policy(sos), inp)


def format_result(result: dict) -> str:
    """The result of a decision as text, one field per line."""
    def items(values) -> str:
        return ", ".join(values) if values else "-"
    chains = "; ".join(" -> ".join(c) for c in result["provenance"]) or "-"
    timeout = "-" if result["timeout"] is None else f"{result['timeout']} ms"
    return "\n".join([
        f"decision:    {result['decision']}",
        f"reason:      {result['reason']}",
        f"contracts:   {items(result['contracts'])}",
        f"norms:       {items(result['norms'])}",
        f"provenance:  {chains}",
        f"escalate_to: {result['escalate_to'] or '-'}",
        f"timeout:     {timeout}",
        f"fallback:    {items(result['fallback'])}",
        f"notify:      {items(result['notify'])}",
    ])
