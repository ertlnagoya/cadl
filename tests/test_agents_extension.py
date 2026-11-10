"""Agent governance extension (Appendix G, agents 0.1): parsing, rules G-1
to G-16 of G.9, the IR (G.13), the reference decision function (G.5, G.10)
and the code targets agent-rego and agent-cedar (G.10)."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from cadl.agents import (
    AgentsInputError,
    build_policy,
    decide,
    decide_message,
    is_static,
    parse_message_event,
    parse_ref,
)
from cadl.cli import _ir_to_dict, main
from cadl.codegen import generate
from cadl.codegen.agent_policy.cedar_gen import generate_agent_cedar_text
from cadl.codegen.agent_policy.rego_gen import generate_agent_rego_text
from cadl.parser import parse, parse_file
from cadl.sim import lower_to_ir
from cadl.type_checker import type_check

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "agents_tool_governance.cadl"
INPUTS = ROOT / "examples" / "agents_inputs"

BOTH = ("norms: 0.1", "agents: 0.1")

ACTORS = """
- {id: OP, role: operator, agent: {kind: human}}
- {id: SUP, role: supervisor, agent: {kind: human}}
- {id: AG, role: agent, agent: {kind: llm_agent, protocol: a2a}}
- {id: SUB, role: sub-agent, agent: {kind: llm_agent}}
- {id: T, role: tools, agent: {kind: tool_server, protocol: mcp, tools: [read, write, wipe]}}
- {id: "R[1..3]", role: robots, agent: {kind: service}}
- {id: X, role: outsider}
"""

PARTIES = '[OP, SUP, AG, SUB, T, "R[*]"]'


def indent(text: str, n: int) -> str:
    return textwrap.indent(textwrap.dedent(text).strip("\n") + "\n", " " * n)


def source(*blocks: str, declare=BOTH, actors: str = ACTORS,
           enforcement: str | None = "enforcement: {}", parties: str = PARTIES,
           more: str = "") -> str:
    """A file with the actors above and a contract C whose further keys are
    the given blocks. `more` holds further contracts or top-level keys."""
    ext = "  extensions:\n" + "".join(f"    - {d}\n" for d in declare) if declare else ""
    head = ("sos:\n  name: T\n" + ext + "  actors:\n" + indent(actors, 4)
            + "  contracts:\n    - id: C\n      parties: " + parties + "\n")
    body = "".join(indent(b, 6) for b in ((enforcement,) if enforcement else ()) + blocks)
    return head + body + (indent(more, 2) if more else "")


def check(*blocks: str, **kw):
    return type_check(parse(source(*blocks, **kw)))


def with_rule(items, rule: str) -> list[str]:
    return [str(i) for i in items if f"({rule})" in str(i)]


def g_diagnostics(res) -> list[str]:
    return [str(i) for i in res.errors + res.warnings if "(G-" in str(i)]


def norms(*entries: str) -> str:
    return "norms:\n" + "".join(indent(e, 2) for e in entries)


def perm(nid: str, bearer: str, content: str, extra: str = "") -> str:
    return (f'- id: {nid}\n  kind: permission\n  bearer: "{bearer}"\n'
            f'  content: "{content}"\n' + indent(extra, 2) if extra else
            f'- id: {nid}\n  kind: permission\n  bearer: "{bearer}"\n'
            f'  content: "{content}"\n')


def proh(nid: str, bearer: str, content: str, extra: str = "") -> str:
    base = (f'- id: {nid}\n  kind: prohibition\n  bearer: "{bearer}"\n'
            f'  content: "{content}"\n')
    return base + (indent(extra, 2) if extra else "")


APPROVAL = """
counts_as:
  - { when: "OP -> AG : approve", creates: approval }
  - { when: "OP -> AG : reject", creates: rejection }
"""

ESCALATE = 'on_denied: { escalate_to: OP, timeout: 5min }'

# A contract that satisfies every rule: used as the positive case.
GOOD = (
    APPROVAL,
    norms(
        perm("p_read", "OP", "OP -> T : read"),
        perm("p_write", "AG", "AG -> T : write"),
        proh("no_wipe", "AG", "AG -> T : wipe",
             'on_denied: { escalate_to: OP, timeout: 5min, '
             'fallback: "AG -> T : write", notify: SUP }'),
    ),
    """
    delegations:
      - { norm: p_read, to: [AG, SUB, "R[*]"], grant: grant_read, revoke: revoke_read, max_depth: 2 }
    """,
)


# --- Parsing ---------------------------------------------------------------

class TestParsing:
    def test_agent_binding(self):
        sos = parse(source())
        by = {a.id.name: a.agent for a in sos.actors}
        assert by["X"] is None
        assert (by["AG"].kind, by["AG"].protocol, by["AG"].tools) == ("llm_agent", "a2a", [])
        assert by["T"].tools == ["read", "write", "wipe"] and by["T"].has_tools
        assert not by["OP"].has_tools

    @pytest.mark.parametrize("text", ["enforcement:", "enforcement: {}", "enforcement: ~"])
    def test_null_or_empty_enforcement_is_enforced(self, text):
        c = parse(source(enforcement=text)).contracts[0]
        assert c.enforcement is not None
        assert (c.enforcement.default, c.enforcement.point) == (None, None)

    def test_no_enforcement_key(self):
        assert parse(source(enforcement=None)).contracts[0].enforcement is None

    def test_enforcement_fields(self):
        c = parse(source(enforcement="enforcement: {default: allow, point: caller}"))
        assert (c.contracts[0].enforcement.default, c.contracts[0].enforcement.point) == \
            ("allow", "caller")

    def test_on_denied_and_delegations(self):
        c = parse(source(*GOOD)).contracts[0]
        od = c.norms[2].on_denied
        assert (od.escalate_to, od.timeout_ms, od.fallback, od.notify) == \
            ("OP", 300000, "AG -> T : write", "SUP")
        assert c.norms[0].on_denied is None
        d = c.delegations[0]
        assert (d.norm, d.to, d.grant, d.revoke, d.max_depth, d.until) == \
            ("p_read", ["AG", "SUB", "R[*]"], "grant_read", "revoke_read", 2, None)

    def test_max_depth_defaults_to_one(self):
        c = parse(source("delegations:\n  - { norm: p, to: [AG], grant: g }\n"))
        assert c.contracts[0].delegations[0].max_depth == 1

    def test_yaml_booleans_are_kept_apart(self):
        sos = parse(source(
            "delegations:\n  - { norm: p, to: [AG], grant: on, revoke: no }\n",
            actors='- {id: T, role: t, agent: {kind: tool_server, tools: [on, "off", null, ok]}}',
            parties="[T]"))
        ag = sos.actors[0].agent
        assert ag.tools == ["off", "ok"]
        assert ag.tools_not_names == ["true", "null"]
        assert sos.contracts[0].delegations[0].not_names == ["grant", "revoke"]

    def test_message_event(self):
        m = parse_message_event("R[i] -> T : wipe(path, 3)")
        assert (m.sender["text"], m.receiver["text"], m.name, m.has_args) == \
            ("R[i]", "T", "wipe", True)
        assert parse_message_event("AG -> T : read").has_args is False
        assert parse_message_event("AG.busy == true") is None
        assert [parse_ref(t)["kind"] for t in ("A", "A[3]", "A[*]", "A[1..3]", "A[1..N]", "A[i]")] \
            == ["plain", "index", "all", "range", "all", "var"]


# --- Declaration (G-1, G.14) -------------------------------------------------

class TestDeclaration:
    def test_g1_agents_without_norms(self):
        res = check(*GOOD, declare=("agents: 0.1",))
        assert with_rule(res.errors, "G-1")
        # The keys of the extension are not interpreted.
        assert g_diagnostics(res) == with_rule(res.errors, "G-1")

    def test_g1_both_declared(self):
        res = check(*GOOD)
        assert not with_rule(res.errors, "G-1")
        assert res.ok and not res.warnings, [str(i) for i in res.errors + res.warnings]

    def test_undeclared_use_gets_one_warning(self):
        res = check(*GOOD, declare=("norms: 0.1",))
        assert res.ok
        undeclared = [str(w) for w in res.warnings if "G.14" in str(w)]
        assert len(undeclared) == 1
        for key in ("agent:", "enforcement:", "delegations:", "on_denied:"):
            assert key in undeclared[0]
        assert not g_diagnostics(res)

    def test_undeclared_use_is_not_checked(self):
        # The same mistakes that are errors with the declaration.
        bad = (norms(perm("p", "OP", "AG -> T : nothing", "on_denied: {}")),)
        assert not check(*bad, declare=("norms: 0.1",)).errors
        assert check(*bad).errors

    def test_no_keys_no_warning(self):
        res = type_check(parse_file(ROOT / "examples" / "norms_robot_delivery.cadl"))
        assert not [w for w in res.warnings if "G.14" in str(w)]

    def test_agents_is_a_known_extension(self):
        res = check(*GOOD)
        assert not [i for i in res.infos if "agents" in str(i)]


# --- Static semantics, G-2 to G-16 -------------------------------------------

def actors_with(entry: str) -> str:
    return ACTORS + entry + "\n"


class TestActors:
    @pytest.mark.parametrize("agent", [
        "{kind: robot}", "{protocol: mcp}", "llm_agent", '{kind: human, protocol: "a b"}',
        '{kind: human, protocol: "-x"}',
    ])
    def test_g2_negative(self, agent):
        res = check(actors=actors_with(f"- {{id: N, role: n, agent: {agent}}}"))
        assert with_rule(res.errors, "G-2")

    def test_g2_positive(self):
        res = check(actors=actors_with(
            "- {id: N, role: n, agent: {kind: service, protocol: my-proto_2}}"))
        assert not with_rule(res.errors + res.warnings, "G-2")

    @pytest.mark.parametrize("agent", [
        "{kind: llm_agent, tools: [a]}",
        "{kind: human, tools: []}",
        "{kind: tool_server}",
        "{kind: tool_server, tools: []}",
        "{kind: tool_server, tools: [a, a]}",
        '{kind: tool_server, tools: ["9a"]}',
        '{kind: tool_server, tools: ["exists"]}',
        "{kind: tool_server, tools: [on]}",
        "{kind: tool_server, tools: [yes, a]}",
        "{kind: tool_server, tools: [null]}",
        "{kind: tool_server, tools: a}",
    ])
    def test_g3_negative(self, agent):
        res = check(actors=actors_with(f"- {{id: N, role: n, agent: {agent}}}"))
        assert with_rule(res.errors, "G-3")

    def test_g3_positive(self):
        res = check(actors=actors_with(
            '- {id: N, role: n, agent: {kind: tool_server, tools: [a, b_2, "on"]}}'),
            parties='[OP, N]')
        assert not with_rule(res.errors + res.warnings, "G-3")


class TestToolMessages:
    @pytest.mark.parametrize("block", [
        norms(perm("p", "AG", "AG -> T : format")),
        norms('- {id: o, kind: obligation, bearer: AG, trigger: "OP -> T : nope", '
              'content: "AG -> OP : report", deadline: 5s}'),
        norms('- {id: o, kind: obligation, bearer: AG, until: "OP -> T : nope", '
              'content: "AG -> OP : report", deadline: 5s}'),
        'counts_as:\n  - { when: "AG -> T : nope", creates: done }\n',
        'lifecycle:\n  states: [A, B]\n  initial: A\n  terminal: [B]\n'
        '  transitions:\n    - { id: t, from: A, to: B, on: "AG -> T : nope" }\n',
    ])
    def test_g4_negative(self, block):
        assert with_rule(check(block).errors, "G-4")

    def test_g4_fallback(self):
        res = check(APPROVAL, norms(proh(
            "n", "AG", "AG -> T : wipe",
            'on_denied: { escalate_to: OP, timeout: 1s, fallback: "AG -> T : nope" }')))
        assert with_rule(res.errors, "G-4")

    def test_g4_protocol_step_is_a_warning(self):
        proto = """
        protocols:
          - id: P
            trigger: "go"
            steps:
              - "AG -> T : nope(1)"
              - "AG -> T : read"
        """
        res = check(more=proto)
        assert len(with_rule(res.warnings, "G-4")) == 1
        assert not with_rule(res.errors, "G-4")

    def test_g4_positive(self):
        res = check(norms(perm("p", "AG", "AG -> T : read"),
                          perm("q", "AG", "AG -> OP : anything")))
        assert not with_rule(res.errors + res.warnings, "G-4")


class TestEnforcement:
    @pytest.mark.parametrize("text", [
        "enforcement: {default: maybe}", "enforcement: {point: edge}",
        "enforcement: {default: on}", "enforcement: 3", "enforcement: [deny]",
    ])
    def test_g5_negative(self, text):
        assert with_rule(check(enforcement=text).errors, "G-5")

    @pytest.mark.parametrize("text", [
        "enforcement:", "enforcement: {}", "enforcement: {default: deny}",
        "enforcement: {default: allow, point: receiver}", "enforcement: {point: caller}",
    ])
    def test_g5_positive(self, text):
        assert not with_rule(check(enforcement=text).errors, "G-5")

    def test_g6_negative(self):
        res = check(norms(perm("p", "AG", "SUB -> T : read"),
                          proh("q", "AG", "SUB -> T : wipe")))
        assert len(with_rule(res.errors, "G-6")) == 2

    def test_g6_positive(self):
        res = check(norms(perm("p", "AG", "AG -> T : read"),
                          perm("r", "R[i]", "R[i] -> T : read")))
        assert not with_rule(res.errors, "G-6")

    def test_g6_only_in_an_enforced_contract(self):
        res = check(norms(perm("p", "AG", "SUB -> T : read")), enforcement=None)
        assert not with_rule(res.errors, "G-6")


class TestOnDenied:
    def n(self, extra: str, kind=proh, content="AG -> T : wipe", **kw):
        return check(APPROVAL, norms(kind("n", "AG", content, extra)), **kw)

    @pytest.mark.parametrize("extra", [
        "on_denied: {}", "on_denied:", "on_denied: OP",
        "on_denied: { escalate_to: OP, timeout: 0s }",
        "on_denied: { escalate_to: OP, timeout: soon }",
        "on_denied: { escalate_to: OP, timeout: yes }",
        "on_denied: { timeout: 5s }",
        "on_denied: { timeout: 5s, notify: OP }",
    ])
    def test_g7_negative(self, extra):
        assert with_rule(self.n(extra).errors, "G-7")

    def test_g7_only_on_a_prohibition(self):
        assert with_rule(self.n(ESCALATE, kind=perm, content="AG -> T : read").errors, "G-7")

    def test_g7_needs_message_content(self):
        assert with_rule(self.n(ESCALATE, content="AG.speed > 3").errors, "G-7")

    def test_g7_needs_enforced_contract(self):
        assert with_rule(self.n(ESCALATE, enforcement=None).errors, "G-7")

    def test_g7_escalate_without_timeout_warns(self):
        res = self.n("on_denied: { escalate_to: OP }")
        assert with_rule(res.warnings, "G-7") and not with_rule(res.errors, "G-7")

    def test_g7_positive(self):
        res = self.n(ESCALATE)
        assert not with_rule(res.errors + res.warnings, "G-7")
        res = self.n('on_denied: { notify: SUP }')
        assert not with_rule(res.errors + res.warnings, "G-7")

    @pytest.mark.parametrize("extra", [
        "on_denied: { escalate_to: GHOST, timeout: 5s }",
        'on_denied: { escalate_to: "R[*]", timeout: 5s }',
        "on_denied: { escalate_to: X, timeout: 5s }",        # not a party
        "on_denied: { escalate_to: AG, timeout: 5s }",       # the bearer
        "on_denied: { notify: GHOST }",
        'on_denied: { notify: "R[1..2]" }',
    ])
    def test_g8_negative(self, extra):
        assert with_rule(self.n(extra).errors, "G-8")

    def test_g8_not_human_warns(self):
        res = check('counts_as:\n  - { when: "SUB -> AG : approve", creates: approval }\n'
                    '  - { when: "SUB -> AG : reject", creates: rejection }\n',
                    norms(proh("n", "AG", "AG -> T : wipe",
                               "on_denied: { escalate_to: SUB, timeout: 5s }")))
        assert with_rule(res.warnings, "G-8") and not with_rule(res.errors, "G-8")

    def test_g8_positive(self):
        res = self.n("on_denied: { escalate_to: OP, timeout: 5s, notify: X }")
        assert not with_rule(res.errors + res.warnings, "G-8")

    def test_g9_missing_approval_rule(self):
        res = check(norms(proh("n", "AG", "AG -> T : wipe", ESCALATE)))
        assert with_rule(res.errors, "G-9")
        assert with_rule(res.warnings, "G-9")      # and no rejection rule

    def test_g9_missing_rejection_rule_warns(self):
        res = check('counts_as:\n  - { when: "OP -> AG : approve", creates: approval }\n',
                    norms(proh("n", "AG", "AG -> T : wipe", ESCALATE)))
        assert not with_rule(res.errors, "G-9")
        assert len(with_rule(res.warnings, "G-9")) == 1

    @pytest.mark.parametrize("rule", [
        '{ when: "SUP -> AG : approve", creates: approval }',   # not the approver
        '{ when: "OP.ready == true", creates: approval }',      # not a message
        '{ when: "AG -> OP : no", creates: rejection }',
    ])
    def test_g9_other_use_of_the_names(self, rule):
        res = check(APPROVAL + f"  - {rule}\n",
                    norms(proh("n", "AG", "AG -> T : wipe", ESCALATE)))
        assert with_rule(res.errors, "G-9")

    def test_g9_norm_id_is_reserved(self):
        res = check(norms(perm("approval", "AG", "AG -> T : read")))
        assert with_rule(res.errors, "G-9")

    def test_g9_names_are_free_in_a_contract_that_is_not_enforced(self):
        res = check('counts_as:\n  - { when: "AG -> OP : ok", creates: approval }\n',
                    enforcement=None)
        assert not with_rule(res.errors + res.warnings, "G-9")

    def test_g9_positive(self):
        res = self.n(ESCALATE)
        assert not with_rule(res.errors + res.warnings, "G-9")

    @pytest.mark.parametrize("fallback", [
        "AG.busy == false",            # not a message event
        "SUB -> T : read",             # not the bearer
        "AG -> GHOST : read",          # receiver not declared
        "AG -> T : wipe",              # the content itself
        "AG -> T : wipe(all)",         # arguments are not considered
    ])
    def test_g10_negative(self, fallback):
        res = self.n(f'on_denied: {{ escalate_to: OP, timeout: 5s, fallback: "{fallback}" }}')
        assert with_rule(res.errors, "G-10")

    def test_g10_positive(self):
        res = check(APPROVAL, norms(
            perm("p", "AG", "AG -> T : read"),
            proh("n", "AG", "AG -> T : wipe",
                 'on_denied: { escalate_to: OP, timeout: 5s, fallback: "AG -> T : read" }')))
        assert not with_rule(res.errors + res.warnings, "G-10")
        assert not g_diagnostics(res)


class TestDelegations:
    PERMS = norms(perm("p", "OP", "OP -> T : read"), perm("q", "OP", "OP -> T : write"),
                  perm("ri", "R[i]", "R[i] -> T : read"),
                  perm("pred", "OP", "OP.level > 2"),
                  proh("no", "AG", "AG -> T : wipe"))

    def d(self, entry: str, **kw):
        return check(self.PERMS, f"delegations:\n  - {entry}\n", **kw)

    @pytest.mark.parametrize("entry", [
        "{ norm: ghost, to: [AG], grant: g }",
        "{ norm: no, to: [AG], grant: g }",          # a prohibition
        "{ norm: pred, to: [AG], grant: g }",        # content is not a message event
        "{ norm: ri, to: [AG], grant: g }",          # bearer with a bound index
        "{ to: [AG], grant: g }",
        "{ norm: p, to: [AG], grant: g }\n  - { norm: p, to: [SUB], grant: h }",
    ])
    def test_g11_negative(self, entry):
        assert with_rule(self.d(entry).errors, "G-11")

    def test_g11_needs_enforced_contract(self):
        res = self.d("{ norm: p, to: [AG], grant: g }", enforcement=None)
        assert with_rule(res.errors, "G-11")

    def test_g11_not_a_list(self):
        assert with_rule(check(self.PERMS, "delegations: { norm: p }\n").errors, "G-11")

    def test_g11_positive(self):
        res = self.d("{ norm: p, to: [AG], grant: g }\n  - { norm: q, to: [AG], grant: h }")
        assert not with_rule(res.errors + res.warnings, "G-11")

    @pytest.mark.parametrize("entry", [
        "{ norm: p, to: [], grant: g }",
        "{ norm: p, grant: g }",
        "{ norm: p, to: [GHOST], grant: g }",
        '{ norm: p, to: ["R[i]"], grant: g }',
        "{ norm: p, to: [AG, T], grant: g }",        # a tool server
        "{ norm: p, to: [AG], grant: g, max_depth: 0 }",
        "{ norm: p, to: [AG], grant: g, max_depth: two }",
        "{ norm: p, to: [AG], grant: g, max_depth: 1.5 }",
    ])
    def test_g12_negative(self, entry):
        assert with_rule(self.d(entry).errors, "G-12")

    def test_g12_until_follows_appendix_f(self):
        res = self.d('{ norm: p, to: [AG], grant: g, until: "GHOST -> AG : stop" }')
        assert with_rule(res.errors, "N-9")
        res = self.d('{ norm: p, to: [AG], grant: g, until: "within(5s, OP.done)" }')
        assert with_rule(res.errors, "N-10")

    def test_g12_bearer_in_to_warns(self):
        res = self.d("{ norm: p, to: [OP, AG], grant: g }")
        assert with_rule(res.warnings, "G-12") and not with_rule(res.errors, "G-12")

    def test_g12_positive(self):
        res = self.d('{ norm: p, to: [AG, "R[*]", "R[1..2]"], grant: g, max_depth: 3, '
                     'until: "OP -> AG : stop" }')
        assert not with_rule(res.errors + res.warnings, "G-12")
        assert not with_rule(res.errors, "N-9")

    @pytest.mark.parametrize("entry", [
        '{ norm: p, to: [AG], grant: "9x" }',
        '{ norm: p, to: [AG], grant: "exists" }',
        "{ norm: p, to: [AG] }",
        "{ norm: p, to: [AG], grant: g, revoke: g }",
        "{ norm: p, to: [AG], grant: approval }",
        "{ norm: p, to: [AG], grant: g, revoke: rejection }",
        "{ norm: p, to: [AG], grant: read }",          # a tool of the party T
        "{ norm: p, to: [AG], grant: on }",            # YAML 1.1
        "{ norm: p, to: [AG], grant: g, revoke: no }",
        "{ norm: p, to: [AG], grant: g }\n  - { norm: q, to: [AG], grant: h, revoke: g }",
    ])
    def test_g13_negative(self, entry):
        assert with_rule(self.d(entry).errors, "G-13")

    def test_g13_positive(self):
        res = self.d("{ norm: p, to: [AG], grant: g, revoke: r }\n"
                     "  - { norm: q, to: [AG], grant: h }")
        assert not with_rule(res.errors + res.warnings, "G-13")


class TestWarnings:
    def test_g14_content_without_effect(self):
        res = check(norms(perm("p", "AG", "AG.level > 2"),
                          proh("q", "AG", "within(5s, AG.speed > 3)")))
        assert len(with_rule(res.warnings, "G-14")) == 2 and res.ok

    def test_g14_arguments(self):
        res = check(norms(perm("p", "AG", "AG -> T : read(path)")))
        assert len(with_rule(res.warnings, "G-14")) == 1 and res.ok

    def test_g14_positive(self):
        res = check(norms(perm("p", "AG", "AG -> T : read"),
                          '- {id: o, kind: obligation, bearer: AG, trigger: "OP -> AG : go", '
                          'content: "AG.done == true", deadline: 5s}'))
        assert not with_rule(res.warnings, "G-14")
        res = check(norms(perm("p", "AG", "AG.level > 2")), enforcement=None)
        assert not with_rule(res.warnings, "G-14")

    def test_g15a_open_tools_under_default_allow(self):
        res = check(norms(perm("p", "AG", "AG -> T : read"), proh("q", "AG", "AG -> T : wipe")),
                    enforcement="enforcement: {default: allow}")
        (w,) = with_rule(res.warnings, "G-15")
        assert "write" in w and "wipe" not in w

    def test_g15a_positive(self):
        res = check(norms(perm("p", "AG", "AG -> T : read"), proh("q", "AG", "AG -> T : wipe"),
                          proh("r", "SUB", "SUB -> T : write")),
                    enforcement="enforcement: {default: allow}")
        assert not with_rule(res.warnings, "G-15")
        # Without default: allow the tools are closed by default.
        assert not with_rule(check(norms(perm("p", "AG", "AG -> T : read"))).warnings, "G-15")

    def test_g15b_ungoverned_tool_server(self):
        res = check(enforcement=None)
        (w,) = with_rule(res.warnings, "G-15")
        assert "'T'" in w
        res = check(parties="[OP, AG]")
        assert with_rule(res.warnings, "G-15")

    def test_g15b_positive(self):
        assert not with_rule(check().warnings, "G-15")

    OBLIGATION = ('- {id: o, kind: obligation, bearer: AG, trigger: "OP -> AG : go", '
                  'content: "AG -> T : write", deadline: 5s}')

    def test_g16a_obligation_denied_by_default(self):
        res = check(norms(self.OBLIGATION, perm("p", "SUB", "SUB -> T : write")))
        assert len(with_rule(res.warnings, "G-16")) == 1

    def test_g16a_obligation_under_a_prohibition(self):
        res = check(norms(self.OBLIGATION, perm("p", "AG", "AG -> T : write"),
                          proh("q", "AG", "AG -> T : write")))
        assert len(with_rule(res.warnings, "G-16")) == 1

    def test_g16a_prohibition_of_another_bearer_does_not_warn(self):
        res = check(norms(self.OBLIGATION, perm("p", "AG", "AG -> T : write"),
                          proh("q", "SUB", "SUB -> T : write")))
        assert not with_rule(res.warnings, "G-16")

    def test_g16a_fallback(self):
        res = check(APPROVAL, norms(proh(
            "n", "AG", "AG -> T : wipe",
            'on_denied: { escalate_to: OP, timeout: 5s, fallback: "AG -> T : read" }')))
        assert len(with_rule(res.warnings, "G-16")) == 1

    def test_g16a_positive(self):
        res = check(norms(self.OBLIGATION, perm("p", "AG", "AG -> T : write"),
                          '- {id: q, kind: prohibition, bearer: AG, '
                          'trigger: "OP -> AG : freeze", content: "AG -> T : write"}'))
        assert not with_rule(res.warnings, "G-16")
        # default: allow lets the tool call through.
        res = check(norms(self.OBLIGATION, proh("z", "AG", "AG -> T : read"),
                          proh("y", "AG", "AG -> T : wipe")),
                    enforcement="enforcement: {default: allow}")
        assert not with_rule(res.warnings, "G-16")

    DENY = "enforcement: {default: deny}"
    DELEGATION = "delegations:\n  - { norm: p, to: [AG], grant: g, revoke: r }\n"

    def test_g16b_default_deny(self):
        res = check(APPROVAL, norms(perm("p", "OP", "OP -> T : read"),
                                    proh("n", "AG", "AG -> T : wipe", ESCALATE)),
                    self.DELEGATION, enforcement=self.DENY)
        found = with_rule(res.warnings, "G-16")
        assert len(found) == 4      # grant, revoke, approve, reject
        assert res.ok

    def test_g16b_positive(self):
        res = check(APPROVAL, norms(
            perm("p", "OP", "OP -> T : read"),
            perm("pg", "OP", "OP -> AG : g"), perm("pr", "OP", "OP -> AG : r"),
            perm("pa", "OP", "OP -> AG : approve"), perm("pj", "OP", "OP -> AG : reject"),
            proh("n", "AG", "AG -> T : wipe", ESCALATE)),
            self.DELEGATION, enforcement=self.DENY)
        assert not with_rule(res.warnings, "G-16")
        # Without default: deny ordinary messages are allowed by default.
        res = check(APPROVAL, norms(perm("p", "OP", "OP -> T : read"),
                                    proh("n", "AG", "AG -> T : wipe", ESCALATE)),
                    self.DELEGATION)
        assert not with_rule(res.warnings, "G-16")


# --- IR (G.13) ---------------------------------------------------------------

class TestIR:
    def ir(self, sos):
        return _ir_to_dict(lower_to_ir(sos))["institution"]

    def test_example_ir_matches_g13(self):
        inst = self.ir(parse_file(EXAMPLE))
        files = next(a for a in inst["actors"] if a["id"] == "FILES")
        assert files["agent_kind"] == "tool_server"
        assert files["agent_protocol"] == "mcp"
        assert files["agent_tools"] == ["read_file", "delete_file"]
        operator = next(a for a in inst["actors"] if a["id"] == "OPERATOR")
        assert (operator["agent_kind"], operator["agent_protocol"], operator["agent_tools"]) \
            == ("human", None, [])
        c = inst["contracts"][0]
        assert c["enforced"] is True
        assert c["enforcement_default"] is None
        assert c["enforcement_point"] == "gateway"
        assert c["delegations"] == [
            {"norm": "may_read", "to": ["PLANNER", "SUMMARIZER"], "grant": "grant_read",
             "revoke": "revoke_read", "max_depth": 2, "until": None}]
        no_delete = next(n for n in c["norms"] if n["id"] == "no_delete")
        assert {k: v for k, v in no_delete.items() if k.startswith("on_denied")} == {
            "on_denied_escalate_to": "OPERATOR",
            "on_denied_timeout_ms": 600000,
            "on_denied_fallback": "PLANNER -> TICKETS : create_ticket",
            "on_denied_notify": "OPERATOR",
        }
        may_read = next(n for n in c["norms"] if n["id"] == "may_read")
        assert all(v is None for k, v in may_read.items() if k.startswith("on_denied"))

    def test_defaults(self):
        sos = parse(source(norms(perm("p", "OP", "OP -> T : read")),
                           "delegations:\n  - { norm: p, to: [AG], grant: g }\n",
                           enforcement="enforcement:"))
        inst = self.ir(sos)
        x = next(a for a in inst["actors"] if a["id"] == "X")
        assert (x["agent_kind"], x["agent_protocol"], x["agent_tools"]) == (None, None, [])
        c = inst["contracts"][0]
        assert (c["enforced"], c["enforcement_default"], c["enforcement_point"]) == \
            (True, None, "gateway")
        assert c["delegations"][0]["max_depth"] == 1
        assert c["delegations"][0]["revoke"] is None

    def test_contract_that_is_not_enforced(self):
        c = self.ir(parse(source(enforcement=None)))["contracts"][0]
        assert (c["enforced"], c["enforcement_default"], c["enforcement_point"],
                c["delegations"]) == (False, None, None, [])

    def test_explicit_default(self):
        c = self.ir(parse(source(enforcement="enforcement: {default: deny, point: caller}")))
        assert (c["contracts"][0]["enforcement_default"],
                c["contracts"][0]["enforcement_point"]) == ("deny", "caller")

    def test_undeclared_keys_are_not_lowered(self):
        inst = self.ir(parse(source(*GOOD, declare=("norms: 0.1",))))
        assert "agent_kind" not in inst["actors"][0]
        c = inst["contracts"][0]
        assert "enforced" not in c and "delegations" not in c
        assert not [k for k in c["norms"][0] if k.startswith("on_denied")]
        raw = lower_to_ir(parse(source(*GOOD, declare=("norms: 0.1",))))
        assert raw.institution.actors[4].agent_kind is None
        assert raw.institution.contracts[0].enforced is False

    def test_ir_of_other_examples_has_no_agent_fields(self):
        d = _ir_to_dict(lower_to_ir(parse_file(ROOT / "examples" / "norms_robot_delivery.cadl")))
        assert "agents_extension" not in d
        assert "agent_kind" not in d["institution"]["actors"][0]


# --- Decision function (G.5, G.10) --------------------------------------------

def msg(sender: str, receiver: str, name: str, **rest) -> dict:
    return {"message": {"from": sender, "to": receiver, "name": name}, **rest}


def inst(contract: str, **fields) -> dict:
    return {"contract": contract, "instance": "i1", **fields}


class TestExampleDecisions:
    """The sample calls of G.10 and G.12."""

    def run(self, name: str) -> dict:
        return decide_message(parse_file(EXAMPLE), json.loads((INPUTS / name).read_text()))

    def test_example_is_clean(self):
        res = type_check(parse_file(EXAMPLE))
        assert res.ok and not res.warnings and not res.infos

    def test_all_norms_are_static(self):
        c = parse_file(EXAMPLE).contracts[0]
        assert all(is_static(c, n) for n in c.norms)

    def test_search_tickets(self):
        r = self.run("search_tickets.json")
        assert (r["decision"], r["reason"]) == ("allow", "permission")
        assert r["norms"] == ["ASSISTED_REPORTING.may_search"]
        assert r["contracts"] == ["ASSISTED_REPORTING"] and r["provenance"] == []

    @pytest.mark.parametrize("name", ["delete_file.json", "delete_file_call2.json"])
    def test_delete_file_escalates(self, name):
        # The result fields of the audit record written out in G.12.
        assert self.run(name) == {
            "decision": "escalate", "reason": "prohibition",
            "contracts": ["ASSISTED_REPORTING"],
            "norms": ["ASSISTED_REPORTING.no_delete"], "provenance": [],
            "escalate_to": "OPERATOR", "timeout": 600000,
            "fallback": ["PLANNER -> TICKETS : create_ticket"], "notify": ["OPERATOR"],
        }

    def test_format_disk(self):
        r = self.run("format_disk.json")
        assert (r["decision"], r["reason"]) == ("deny", "undeclared_tool")
        assert r["contracts"] == [] and r["norms"] == []

    @pytest.mark.parametrize("name", ["read_file.json", "read_file_after_revocation.json"])
    def test_read_file_without_delegation(self, name):
        r = self.run(name)
        assert (r["decision"], r["reason"]) == ("deny", "default")
        assert r["contracts"] == ["ASSISTED_REPORTING"] and r["norms"] == []

    def test_read_file_through_a_chain(self):
        # The result written out in G.10.
        assert self.run("read_file_delegated.json") == {
            "decision": "allow", "reason": "permission",
            "contracts": ["ASSISTED_REPORTING"],
            "norms": ["ASSISTED_REPORTING.may_read"],
            "provenance": [["OPERATOR", "PLANNER", "SUMMARIZER"]],
            "escalate_to": None, "timeout": None, "fallback": [], "notify": [],
        }

    def test_ordinary_messages_of_the_example(self):
        sos = parse_file(EXAMPLE)
        for sender, receiver, name in (("OPERATOR", "PLANNER", "grant_read"),
                                       ("PLANNER", "SUMMARIZER", "grant_read"),
                                       ("OPERATOR", "PLANNER", "approve"),
                                       ("OPERATOR", "PLANNER", "revoke_read")):
            r = decide_message(sos, msg(sender, receiver, name))
            assert (r["decision"], r["reason"]) == ("allow", "default")
        # The fallback of no_delete is allowed by may_file_ticket.
        r = decide_message(sos, msg("PLANNER", "TICKETS", "create_ticket"))
        assert r["norms"] == ["ASSISTED_REPORTING.may_file_ticket"]
        # The bearer itself.
        r = decide_message(sos, msg("OPERATOR", "FILES", "read_file"))
        assert (r["decision"], r["norms"], r["provenance"]) == \
            ("allow", ["ASSISTED_REPORTING.may_read"], [])


# Two enforced contracts over the same agent and tool server.
RICH_ACTORS = """
- {id: OP, role: operator, agent: {kind: human}}
- {id: SUP, role: supervisor, agent: {kind: human}}
- {id: AG, role: agent, agent: {kind: llm_agent}}
- {id: SUB, role: sub-agent, agent: {kind: llm_agent}}
- {id: T, role: tools, agent: {kind: tool_server, tools: [read, write, wipe]}}
- {id: U, role: more tools, agent: {kind: tool_server, tools: [read, write, wipe]}}
- {id: FREE, role: tools of no enforced contract, agent: {kind: tool_server, tools: [read]}}
- {id: M, role: monitor, agent: {kind: service}}
- {id: "R[1..3]", role: robots, agent: {kind: service}}
- {id: X, role: outsider}
"""

RICH = "sos:\n  name: Rich\n  extensions:\n    - norms: 0.1\n    - agents: 0.1\n" \
    "  actors:\n" + indent(RICH_ACTORS, 4) + indent("""
contracts:
  - id: C1
    parties: [OP, AG, SUB, T, U, M, "R[*]"]
    enforcement:
    counts_as:
      - { when: "OP -> AG : approve", creates: approval }
      - { when: "OP -> AG : reject", creates: rejection }
    norms:
      - { id: p_read, kind: permission, bearer: AG, content: "AG -> T : read" }
      - { id: p_op_write, kind: permission, bearer: OP, content: "OP -> U : write" }
      - id: no_wipe
        kind: prohibition
        bearer: AG
        content: "AG -> T : wipe"
        on_denied: { escalate_to: OP, timeout: 5min, fallback: "AG -> T : read", notify: OP }
      - { id: r_write, kind: permission, bearer: "R[i]", content: "R[i] -> U : write" }
      - { id: no_r_wipe, kind: prohibition, bearer: "R[i]", content: "R[i] -> U : wipe" }
      - id: frozen
        kind: prohibition
        bearer: AG
        trigger: "OP -> AG : freeze"
        content: "AG -> M : ping"
      - id: may_report
        kind: permission
        bearer: AG
        until: "OP -> AG : stop"
        content: "AG -> T : write"
    delegations:
      - { norm: p_op_write, to: [AG, SUB], grant: grant_write, revoke: revoke_write, max_depth: 2 }
  - id: C2
    parties: [SUP, AG, T]
    enforcement: { default: deny }
    counts_as:
      - { when: "SUP -> AG : approve", creates: approval }
    norms:
      - { id: p_read2, kind: permission, bearer: AG, content: "AG -> T : read" }
      - id: no_wipe2
        kind: prohibition
        bearer: AG
        content: "AG -> T : wipe"
        on_denied: { escalate_to: SUP, timeout: 1min, notify: SUP }
""", 2)


@pytest.fixture(scope="module")
def rich():
    sos = parse(RICH)
    assert type_check(sos).ok, [str(e) for e in type_check(sos).errors]
    return build_policy(sos)


def summary(r: dict):
    return r["decision"], r["reason"], r["contracts"], r["norms"]


class TestDecisionFunction:
    def test_static_and_non_static(self):
        c = parse(RICH).contracts[0]
        static = {n.id: is_static(c, n) for n in c.norms}
        assert static == {"p_read": True, "p_op_write": True, "no_wipe": True,
                          "r_write": False, "no_r_wipe": False, "frozen": False,
                          "may_report": False}

    def test_ungoverned(self, rich):
        # An ordinary message is governed only when both ends are parties.
        assert summary(decide(rich, msg("X", "AG", "hi"))) == ("allow", "ungoverned", [], [])
        assert summary(decide(rich, msg("AG", "X", "hi"))) == ("allow", "ungoverned", [], [])
        assert decide(rich, msg("GHOST", "NOBODY", "hi"))["reason"] == "ungoverned"
        # R[7] is not a member of R[1..3]; R without index is not an actor.
        assert decide(rich, msg("R[7]", "AG", "hi"))["reason"] == "ungoverned"
        assert decide(rich, msg("R", "AG", "hi"))["reason"] == "ungoverned"
        # A tool server that is a party of no enforced contract (G-15 warns).
        for sender in ("AG", "X", "GHOST"):
            for name in ("read", "bogus"):
                assert summary(decide(rich, msg(sender, "FREE", name))) == \
                    ("allow", "ungoverned", [], [])

    def test_tool_call_is_governed_whatever_its_sender(self, rich):
        # A declared actor that is no party, an actor the file does not
        # declare, and an index outside the declared set.
        for sender in ("X", "GHOST", "R[7]", "R"):
            assert summary(decide(rich, msg(sender, "U", "read"))) == \
                ("deny", "default", ["C1"], [])
            assert summary(decide(rich, msg(sender, "U", "bogus"))) == \
                ("deny", "undeclared_tool", [], [])
            assert summary(decide(rich, msg(sender, "T", "read"))) == \
                ("deny", "default", ["C1", "C2"], [])
            assert summary(decide(rich, msg(sender, "U", "read", instances=[]))) == \
                ("deny", "no_instance", ["C1"], [])
        # A party of C1 only is governed by C2 as well for the tools of T.
        assert summary(decide(rich, msg("SUB", "T", "read"))) == \
            ("deny", "default", ["C1", "C2"], [])
        # A norm applies only when the sender is its bearer.
        assert summary(decide(rich, msg("X", "T", "wipe"))) == \
            ("deny", "default", ["C1", "C2"], [])

    def test_outsider_under_default_allow(self):
        policy = build_policy(parse(source(
            norms(proh("n", "AG", "AG -> T : wipe")),
            enforcement="enforcement: {default: allow}")))
        assert summary(decide(policy, msg("X", "T", "wipe"))) == ("allow", "default", ["C"], [])
        assert summary(decide(policy, msg("GHOST", "T", "bogus"))) == \
            ("deny", "undeclared_tool", [], [])

    def test_undeclared_tool(self, rich):
        assert summary(decide(rich, msg("AG", "T", "bogus"))) == \
            ("deny", "undeclared_tool", [], [])

    def test_two_contracts_both_allow(self, rich):
        assert summary(decide(rich, msg("AG", "T", "read"))) == \
            ("allow", "permission", ["C1", "C2"], ["C1.p_read", "C2.p_read2"])

    def test_deny_overrides(self, rich):
        # C1 would allow through may_report, C2 denies by default.
        r = decide(rich, msg("AG", "T", "write", instances=[
            inst("C1", in_force={"may_report": True}), inst("C2")]))
        assert summary(r) == ("deny", "default", ["C2"], [])
        # An ordinary message: C1 allows by default, C2 has default: deny.
        assert summary(decide(rich, msg("AG", "T", "read", instances=[inst("C1")]))) == \
            ("deny", "no_instance", ["C2"], [])

    def test_default_by_kind_of_message(self, rich):
        assert summary(decide(rich, msg("OP", "AG", "hello"))) == ("allow", "default", ["C1"], [])
        assert summary(decide(rich, msg("OP", "U", "read"))) == ("deny", "default", ["C1"], [])
        assert summary(decide(rich, msg("SUP", "AG", "hello"))) == ("deny", "default", ["C2"], [])

    def test_no_instance(self, rich):
        r = decide(rich, msg("OP", "U", "write", instances=[]))
        assert summary(r) == ("deny", "no_instance", ["C1"], [])
        # Not a tool call: as the default (step 3.1).
        assert summary(decide(rich, msg("OP", "AG", "hello", instances=[]))) == \
            ("allow", "default", ["C1"], [])
        assert summary(decide(rich, msg("SUP", "AG", "hello", instances=[]))) == \
            ("deny", "default", ["C2"], [])
        # no_instance comes first among the reasons of the determining contracts.
        r = decide(rich, msg("AG", "T", "wipe", instances=[inst("C2")]))
        assert summary(r) == ("deny", "no_instance", ["C1"], [])
        assert r["fallback"] == [] and r["timeout"] is None

    def test_different_approvers_across_contracts_deny(self, rich):
        r = decide(rich, msg("AG", "T", "wipe"))
        assert summary(r) == ("deny", "prohibition", ["C1", "C2"],
                              ["C1.no_wipe", "C2.no_wipe2"])
        assert r["escalate_to"] is None
        assert r["timeout"] == 60000            # the smallest
        assert r["fallback"] == ["AG -> T : read"]
        assert r["notify"] == ["OP", "SUP"]

    def test_unknown_index_is_fail_closed(self, rich):
        # R[i] with no value bound to i: the match is unknown.
        # An unknown prohibition applies ...
        assert summary(decide(rich, msg("R[2]", "U", "wipe"))) == \
            ("deny", "prohibition", ["C1"], ["C1.no_r_wipe"])
        # ... and an unknown permission does not.
        assert summary(decide(rich, msg("R[2]", "U", "write"))) == \
            ("deny", "default", ["C1"], [])

    def test_bound_index(self, rich):
        def run(name, i, **flags):
            return summary(decide(rich, msg("R[2]", "U", name, instances=[
                inst("C1", bindings={"i": i}, in_force=flags)])))
        assert run("write", 2, r_write=True) == ("allow", "permission", ["C1"], ["C1.r_write"])
        assert run("write", 3, r_write=True) == ("deny", "default", ["C1"], [])
        # Bound, but the flag of the norm that is not static is missing: unknown.
        assert run("write", 2) == ("deny", "default", ["C1"], [])
        assert run("wipe", 2) == ("deny", "prohibition", ["C1"], ["C1.no_r_wipe"])
        assert run("wipe", 3) == ("deny", "default", ["C1"], [])
        assert run("wipe", 2, no_r_wipe=False) == ("deny", "default", ["C1"], [])

    def test_several_instances(self, rich):
        r = decide(rich, msg("R[2]", "U", "write", instances=[
            inst("C1", bindings={"i": 1}, in_force={"r_write": True}),
            inst("C1", bindings={"i": 2}, in_force={"r_write": True})]))
        assert summary(r) == ("allow", "permission", ["C1"], ["C1.r_write"])

    def test_in_force_flag_of_a_triggered_prohibition(self, rich):
        def run(**kw):
            return summary(decide(rich, msg("AG", "M", "ping", **kw)))
        assert run() == ("deny", "prohibition", ["C1"], ["C1.frozen"])
        assert run(instances=[inst("C1")]) == ("deny", "prohibition", ["C1"], ["C1.frozen"])
        assert run(instances=[inst("C1", in_force={"frozen": True})])[0] == "deny"
        assert run(instances=[inst("C1", in_force={"frozen": False})]) == \
            ("allow", "default", ["C1"], [])

    def test_derived_permission_with_provenance(self, rich):
        def run(sender, chain, norm="p_op_write", holder=None, **kw):
            return decide(rich, msg(sender, "U", "write", instances=[
                inst("C1", derived=[{"norm": norm, "holder": holder or sender,
                                     "chain": chain}], **kw)]))
        r = run("SUB", ["OP", "AG", "SUB"])
        assert summary(r) == ("allow", "permission", ["C1"], ["C1.p_op_write"])
        assert r["provenance"] == [["OP", "AG", "SUB"]]
        # What the file says is tested again: an entry that fails does not apply.
        assert run("SUB", ["OP", "AG", "SUB", "SUB"])["decision"] == "deny"     # max_depth
        assert run("SUB", ["AG", "SUB"])["decision"] == "deny"                  # not the bearer
        assert run("SUB", ["OP", "M", "SUB"])["decision"] == "deny"             # M not in to:
        assert run("SUB", ["OP", "AG"])["decision"] == "deny"                   # holder
        assert run("SUB", ["OP", "AG"], holder="AG")["decision"] == "deny"      # not the sender
        assert run("SUB", ["OP", "SUB"], norm="p_read")["decision"] == "deny"   # not delegable
        assert run("SUB", ["SUB"])["decision"] == "deny"
        # The derived instance of another message name does not match.
        r = decide(rich, msg("SUB", "U", "read", instances=[inst("C1", derived=[
            {"norm": "p_op_write", "holder": "SUB", "chain": ["OP", "SUB"]}])]))
        assert summary(r) == ("deny", "default", ["C1"], [])

    def test_derived_in_one_of_two_contracts(self, rich):
        # SUB holds a derived instance in C1 for U; T is governed by C2 too,
        # where SUB has no permission.
        derived = [{"norm": "p_op_write", "holder": "SUB", "chain": ["OP", "SUB"]}]
        r = decide(rich, msg("SUB", "T", "write", instances=[
            inst("C1", derived=derived), inst("C2")]))
        assert summary(r) == ("deny", "default", ["C1", "C2"], [])


ESCALATION = source(
    """
    counts_as:
      - { when: "OP -> AG : approve", creates: approval }
      - { when: "SUP -> AG : approve", creates: approval }
      - { when: "R[1] -> AG : approve", creates: approval }
    """,
    norms(
        proh("a", "AG", "AG -> T : wipe", "on_denied: { escalate_to: OP, timeout: 5min }"),
        proh("b", "AG", "AG -> T : wipe",
             'on_denied: { escalate_to: OP, timeout: 2min, fallback: "AG -> T : read" }'),
        proh("c", "AG", "AG -> T : write", "on_denied: { escalate_to: OP, timeout: 1s }"),
        proh("d", "AG", "AG -> T : write", "on_denied: { escalate_to: SUP, timeout: 1s }"),
        proh("e", "AG", "AG -> T : read", "on_denied: { escalate_to: OP, timeout: 1s }"),
        proh("f", "AG", "AG -> T : read", "on_denied: { notify: SUP }"),
        proh("g", "R[i]", "R[i] -> T : read", 'on_denied: { escalate_to: "R[1]", timeout: 1s }'),
        perm("h", "AG", "AG -> T : wipe"),
    ),
)


@pytest.fixture(scope="module")
def policy():
    sos = parse(ESCALATION)
    assert type_check(sos).ok
    return build_policy(sos)


class TestEscalation:

    def test_single_approver(self, policy):
        r = decide(policy, msg("AG", "T", "wipe"))
        # A prohibition overrides the permission h (N-12 warns).
        assert summary(r) == ("escalate", "prohibition", ["C"], ["C.a", "C.b"])
        assert (r["escalate_to"], r["timeout"], r["fallback"], r["notify"]) == \
            ("OP", 120000, ["AG -> T : read"], [])

    def test_different_approvers_deny(self, policy):
        r = decide(policy, msg("AG", "T", "write"))
        assert summary(r) == ("deny", "prohibition", ["C"], ["C.c", "C.d"])
        assert r["escalate_to"] is None

    def test_ground_without_escalate_to_denies(self, policy):
        r = decide(policy, msg("AG", "T", "read"))
        assert summary(r) == ("deny", "prohibition", ["C"], ["C.e", "C.f"])
        assert r["notify"] == ["SUP"]

    def test_an_actor_cannot_approve_its_own_message(self, policy):
        def run(sender, i):
            return decide(policy, msg(sender, "T", "read", instances=[
                inst("C", bindings={"i": i}, in_force={"g": True})]))
        assert summary(run("R[1]", 1)) == ("deny", "prohibition", ["C"], ["C.g"])
        r = run("R[2]", 2)
        assert (r["decision"], r["escalate_to"]) == ("escalate", "R[1]")


class TestInput:
    @pytest.mark.parametrize("bad", [
        [], {}, {"message": {"from": "AG", "to": "T"}},
        {"message": {"from": "AG", "to": "T", "name": 3}},
        {"message": {"from": "R[*]", "to": "T", "name": "read"}},
        {"message": {"from": "R[i]", "to": "T", "name": "read"}},
        msg("AG", "T", "read", instances={}),
        msg("AG", "T", "read", instances=[{"instance": "x"}]),
        msg("AG", "T", "read", instances=[inst("C1", bindings={"i": "2"})]),
        msg("AG", "T", "read", instances=[inst("C1", in_force={"frozen": "yes"})]),
        msg("AG", "T", "read", instances=[inst("C1", derived=[{"norm": "p"}])]),
    ])
    def test_invalid_input(self, rich, bad):
        with pytest.raises(AgentsInputError):
            decide(rich, bad)

    def test_now_does_not_influence_the_decision(self, rich):
        a = decide(rich, msg("AG", "T", "wipe"))
        assert decide(rich, msg("AG", "T", "wipe", now="2026-11-10T09:00:05Z")) == a

    def test_extension_must_be_declared(self):
        with pytest.raises(ValueError, match="agents: 0.1"):
            build_policy(parse(source(*GOOD, declare=("norms: 0.1",))))


class TestCli:
    def run(self, capsys, *args):
        status = main(["agents-decide", *map(str, args)])
        return status, capsys.readouterr()

    @pytest.mark.parametrize("name, status, decision", [
        ("search_tickets.json", 0, "allow"),
        ("read_file_delegated.json", 0, "allow"),
        ("read_file.json", 1, "deny"),
        ("format_disk.json", 1, "deny"),
        ("delete_file.json", 3, "escalate"),
    ])
    def test_exit_status(self, capsys, name, status, decision):
        got, out = self.run(capsys, EXAMPLE, "--input", INPUTS / name, "--format", "json")
        assert got == status
        assert json.loads(out.out)["decision"] == decision

    def test_text_output(self, capsys):
        got, out = self.run(capsys, EXAMPLE, "-i", INPUTS / "delete_file.json")
        assert got == 3
        assert "decision:    escalate" in out.out
        assert "escalate_to: OPERATOR" in out.out
        assert "timeout:     600000 ms" in out.out

    def test_usage_errors(self, capsys, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        empty = tmp_path / "empty.json"
        empty.write_text("{}")
        undeclared = ROOT / "examples" / "norms_robot_delivery.cadl"
        ok = INPUTS / "read_file.json"
        for args in ((EXAMPLE, "-i", bad), (EXAMPLE, "-i", empty),
                     (EXAMPLE, "-i", tmp_path / "missing.json"),
                     (tmp_path / "missing.cadl", "-i", ok), (undeclared, "-i", ok)):
            got, out = self.run(capsys, *args)
            assert got == 2, args
            assert out.err.startswith("Error:") and not out.out
        with pytest.raises(SystemExit) as exc:      # no --input
            main(["agents-decide", str(EXAMPLE)])
        assert exc.value.code == 2

    def test_file_with_errors_is_a_usage_error(self, capsys, tmp_path):
        f = tmp_path / "bad.cadl"
        f.write_text(source(norms(perm("p", "AG", "SUB -> T : read"))))
        got, _ = self.run(capsys, f, "-i", INPUTS / "read_file.json")
        assert got == 2


# --- Code generation (G.10) -----------------------------------------------------

# Inputs for the comparison of the generated Rego with the decision function.
def rich_inputs() -> list[dict]:
    senders = ["OP", "SUP", "AG", "SUB", "M", "X", "R[1]", "R[2]", "R[7]", "GHOST"]
    receivers = ["T", "U", "FREE", "AG", "M", "OP", "R[2]", "NOBODY"]
    names = ["read", "write", "wipe", "bogus", "ping", "hello"]
    out = [msg(s, r, n) for s in senders for r in receivers for n in names]
    extra = [
        [], [inst("C1")], [inst("C2")], [inst("C1"), inst("C2")],
        [inst("C1", bindings={"i": 2}, in_force={"r_write": True, "no_r_wipe": True}),
         inst("C2")],
        [inst("C1", bindings={"i": 1}, in_force={"r_write": True}),
         inst("C1", bindings={"i": 2}, in_force={"r_write": False, "frozen": False,
                                               "may_report": True}), inst("C2")],
        [inst("C1", in_force={"frozen": True, "may_report": True}, derived=[
            {"norm": "p_op_write", "holder": "SUB", "chain": ["OP", "AG", "SUB"]},
            {"norm": "p_op_write", "holder": "AG", "chain": ["OP", "AG"]}]), inst("C2")],
        [inst("C1", derived=[
            {"norm": "p_op_write", "holder": "SUB", "chain": ["OP", "M", "SUB"]},
            {"norm": "p_read", "holder": "SUB", "chain": ["AG", "SUB"]},
            {"norm": "p_op_write", "holder": "AG", "chain": ["OP", "SUB", "AG", "AG"]}])],
    ]
    for instances in extra:
        for s in ("AG", "SUB", "R[2]", "OP", "SUP"):
            for r, n in (("T", "read"), ("T", "write"), ("T", "wipe"), ("M", "ping"),
                         ("U", "read"), ("U", "write"), ("U", "wipe"), ("AG", "hello")):
                out.append(msg(s, r, n, instances=instances))
    return out


def escalation_inputs() -> list[dict]:
    out = [msg("AG", "T", n) for n in ("read", "write", "wipe")]
    for i in (1, 2):
        for s in ("R[1]", "R[2]"):
            out.append(msg(s, "T", "read", instances=[
                inst("C", bindings={"i": i}, in_force={"g": True})]))
    return out


class TestAgentRego:
    def text(self, src=None) -> str:
        return generate_agent_rego_text(parse(src) if src else parse_file(EXAMPLE))

    def test_one_package_with_the_result(self):
        text = self.text()
        assert text.count("\npackage ") == 1
        assert "package cadl.agent_policy" in text and "import rego.v1" in text
        assert '\ndefault decision := "deny"' in text
        assert "\nresult := " in text
        for field in ("decision", "reason", "contracts", "norms", "provenance",
                      "escalate_to", "timeout", "fallback", "notify"):
            assert f'"{field}":' in text
        for reason in ("ungoverned", "undeclared_tool", "no_instance", "prohibition",
                       "permission", "default"):
            assert f'"{reason}"' in text

    def test_every_static_norm_has_its_rule(self):
        text = self.text()
        for nid, head in (("may_read", "permission_applies"),
                          ("may_search", "permission_applies"),
                          ("may_file_ticket", "permission_applies"),
                          ("no_delete", "prohibition_applies")):
            assert f'{head} contains ["ASSISTED_REPORTING", "{nid}"' in text
            assert f"# ASSISTED_REPORTING.{nid}:" in text
        # Static: no in-force flag is read.
        rule = text[text.index('prohibition_applies contains ["ASSISTED_REPORTING"'):]
        rule = rule[:rule.index("}")]
        assert 'input.message.name == "delete_file"' in rule
        assert "in_force_value" not in rule and ">= 1" in rule

    def test_rules_of_all_norms_of_a_larger_file(self):
        sos = parse(RICH)
        text = generate_agent_rego_text(sos)
        policy = build_policy(sos)
        for c in policy["contracts"]:
            for n in c["norms"]:
                head = f'{n["kind"]}_applies contains ["{c["id"]}", "{n["id"]}"'
                assert text.count(head) == 1
                rule = text[text.index(head):]
                rule = rule[:rule.index("\n}")]
                assert ("in_force_value" in rule) == (not n["static"])
                assert ("== 2" in rule) == (n["kind"] == "permission")

    def test_non_static_norms_are_listed(self):
        text = self.text(RICH)
        head = text[:text.index("package ")]
        for name in ("C1.r_write", "C1.no_r_wipe", "C1.frozen", "C1.may_report"):
            assert f"#   {name}\n" in head
        assert "#   C1.p_read\n" not in head
        assert "#   (none)" in self.text()[:2000]

    def test_embedded_policy_is_the_boundary_policy(self):
        text = self.text(RICH)
        start = text.index("json.unmarshal(`") + len("json.unmarshal(`")
        assert json.loads(text[start:text.index("`)", start)]) == build_policy(parse(RICH))

    def test_generate_writes_one_file(self, tmp_path):
        generate(parse_file(EXAMPLE), tmp_path, target="agent-rego")
        assert [p.name for p in tmp_path.iterdir()] == ["agent_policy.rego"]

    def test_deterministic(self):
        assert self.text(RICH) == self.text(RICH)

    def test_balanced(self):
        text = self.text(RICH)
        body = text[text.index("`)") + 2:]
        for a, b in ("{}", "[]", "()"):
            assert body.count(a) == body.count(b)


OPA = shutil.which("opa")


@pytest.mark.skipif(OPA is None, reason="the opa binary is not on PATH: the generated "
                    "Rego is tested structurally only, not evaluated")
class TestAgentRegoWithOpa:
    """Evaluate the generated policy with `opa eval` and compare the result
    with the reference decision function."""

    def evaluate(self, rego: Path, inp: dict, tmp_path: Path):
        f = tmp_path / "input.json"
        f.write_text(json.dumps(inp))
        proc = subprocess.run(
            [OPA, "eval", "--format", "json", "--data", str(rego), "--input", str(f),
             "data.cadl.agent_policy"],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr + proc.stdout
        return json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]

    def compare(self, sos, inputs, tmp_path):
        generate(sos, tmp_path, target="agent-rego")
        policy = build_policy(sos)
        for inp in inputs:
            value = self.evaluate(tmp_path / "agent_policy.rego", inp, tmp_path)
            expected = decide(policy, inp)
            assert value.get("result") == expected, inp
            assert value["decision"] == expected["decision"]

    def test_sample_inputs_of_the_appendix(self, tmp_path):
        inputs = [json.loads(p.read_text()) for p in sorted(INPUTS.glob("*.json"))]
        assert len(inputs) >= 6
        self.compare(parse_file(EXAMPLE), inputs, tmp_path)

    def test_two_contracts_instances_and_chains(self, tmp_path):
        self.compare(parse(RICH), rich_inputs(), tmp_path)

    def test_escalation(self, tmp_path):
        self.compare(parse(ESCALATION), escalation_inputs(), tmp_path)

    def test_invalid_input_is_denied(self, tmp_path):
        generate(parse_file(EXAMPLE), tmp_path, target="agent-rego")
        for inp in ({}, {"message": {"from": "R[*]", "to": "FILES", "name": "read_file"}},
                    msg("PLANNER", "TICKETS", "search_tickets", instances={})):
            value = self.evaluate(tmp_path / "agent_policy.rego", inp, tmp_path)
            assert "result" not in value and value["decision"] == "deny"


class TestAgentCedar:
    def gen(self, src=None):
        return generate_agent_cedar_text(parse(src) if src else parse_file(EXAMPLE))

    @staticmethod
    def policies(text: str) -> dict:
        """Policy id -> policy text (without the README comment)."""
        out = {}
        for block in text.split("\n\n"):
            lines = [line for line in block.splitlines() if not line.startswith("//")]
            if lines:
                assert lines[0].startswith('@id("') and lines[-1].endswith(";")
                out[lines[0][5:-2]] = "\n".join(lines[1:])
        return out

    def test_files(self, tmp_path):
        generate(parse_file(EXAMPLE), tmp_path, target="agent-cedar")
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "agent_policy.cedar", "agent_policy_table.json"]
        table = json.loads((tmp_path / "agent_policy_table.json").read_text())
        assert table == self.gen()[1]

    def test_mapping_of_the_example(self):
        text, table = self.gen()
        p = self.policies(text)
        assert list(p) == [
            "ungoverned", "undeclared_tool.FILES", "undeclared_tool.TICKETS",
            "ASSISTED_REPORTING.may_read", "ASSISTED_REPORTING.may_search",
            "ASSISTED_REPORTING.may_file_ticket", "ASSISTED_REPORTING.no_delete",
            "default.ASSISTED_REPORTING"]
        # forbid for undeclared tools and prohibitions, permit for permissions.
        assert p["undeclared_tool.FILES"].startswith("forbid (principal, action, resource)")
        assert ('unless { [CADL::Action::"read_file", CADL::Action::"delete_file"]'
                ".contains(action) }") in p["undeclared_tool.FILES"]
        assert p["ASSISTED_REPORTING.no_delete"] == (
            "forbid (principal, action, resource)\n"
            'when { principal == CADL::Actor::"PLANNER" && '
            'action == CADL::Action::"delete_file" && resource == CADL::Actor::"FILES" };')
        assert p["ASSISTED_REPORTING.may_search"] == (
            "permit (principal, action, resource)\n"
            'when { principal == CADL::Actor::"PLANNER" && '
            'action == CADL::Action::"search_tickets" && resource == CADL::Actor::"TICKETS" };')
        # Default deny for tool calls: the only other permits exclude tool servers.
        assert p["default.ASSISTED_REPORTING"].startswith("permit")
        assert ('unless { (resource == CADL::Actor::"FILES" || '
                'resource == CADL::Actor::"TICKETS") }') in p["default.ASSISTED_REPORTING"]
        assert p["ungoverned"].startswith("permit") and "unless {" in p["ungoverned"]
        assert {k: v["effect"] for k, v in table["policies"].items()} == {
            k: v.split(" ")[0] for k, v in p.items()}

    def test_no_policy_reads_an_attribute(self):
        # A forbid whose evaluation errors is skipped by Cedar and does not
        # deny, so the policies use only ==, in and set membership.
        for src in (None, RICH, ESCALATION):
            for body in self.policies(self.gen(src)[0]).values():
                assert " has " not in body and " like " not in body
                rest = body.replace(".contains(", "(")
                assert "." not in rest, body

    def test_side_table(self):
        _, table = self.gen()
        assert table["prohibitions"] == {"ASSISTED_REPORTING.no_delete": {
            "contract": "ASSISTED_REPORTING", "norm": "no_delete", "static": True,
            "content": "PLANNER -> FILES : delete_file",
            "escalate_to": "OPERATOR", "timeout": 600000,
            "fallback": "PLANNER -> TICKETS : create_ticket", "notify": "OPERATOR"}}
        assert table["policies"]["ASSISTED_REPORTING.no_delete"] == {
            "effect": "forbid", "role": "prohibition",
            "contract": "ASSISTED_REPORTING", "norm": "no_delete"}
        assert table["non_static"] == []
        assert table["delegations"]["ASSISTED_REPORTING.may_read"]["max_depth"] == 2
        assert table["contracts"] == {
            "ASSISTED_REPORTING": {"default": None, "point": "gateway"}}

    def test_readme_comment(self):
        text, _ = self.gen(RICH)
        readme = text[:text.index("\n\n")]
        assert all(line.startswith("//") for line in readme.splitlines())
        assert "default deny" in readme and "forbid overrides permit" in readme
        assert "errors is skipped" in readme
        assert "Cedar Policy Language Reference Guide" in readme
        assert "to be confirmed" not in readme
        assert "NOT covered" in readme
        for name in ("C1.r_write", "C1.no_r_wipe", "C1.frozen", "C1.may_report"):
            assert f"//     {name}" in readme
        assert "Derived permissions" in readme and "//     C1.p_op_write" in readme

    def test_only_static_norms_become_policies(self):
        text, table = self.gen(RICH)
        p = self.policies(text)
        assert {"C1.p_read", "C1.p_op_write", "C1.no_wipe", "C2.p_read2", "C2.no_wipe2"} <= set(p)
        assert not {"C1.r_write", "C1.no_r_wipe", "C1.frozen", "C1.may_report"} & set(p)
        assert table["non_static"] == ["C1.r_write", "C1.no_r_wipe", "C1.frozen",
                                       "C1.may_report"]
        assert table["prohibitions"]["C1.frozen"]["static"] is False

    def test_several_contracts_and_defaults(self):
        p = self.policies(self.gen(RICH)[0])
        # default: deny has no permit for other messages; default omitted has one.
        assert "default.C1" in p and "default.C2" not in p
        # Deny overrides: the default denial of each contract is a forbid.
        assert p["closed.C1"].startswith("forbid") and p["closed.C2"].startswith("forbid")
        assert 'principal in CADL::ActorSet::"R"' in p["closed.C1"]
        assert "unless {" in p["closed.C2"] and 'CADL::Action::"read"' in p["closed.C2"]
        assert 'resource == CADL::Actor::"T"' in p["closed.C1"]
        # With one enforced contract there is none.
        assert not [k for k in self.policies(self.gen()[0]) if k.startswith("closed.")]

    def test_tool_calls_are_governed_for_any_principal(self):
        p = self.policies(self.gen()[0])
        parties = ('(principal == CADL::Actor::"OPERATOR" || principal == CADL::Actor::"PLANNER"'
                   ' || principal == CADL::Actor::"SUMMARIZER" || principal == CADL::Actor::"FILES"'
                   ' || principal == CADL::Actor::"TICKETS")')
        servers = '(resource == CADL::Actor::"FILES" || resource == CADL::Actor::"TICKETS")'
        # Governed: the receiver is a party, and the sender is a party or the
        # receiver is a tool server (then the principal is not tested).
        governed = f"&& ({parties} || {servers}))"
        for pid in ("ungoverned", "undeclared_tool.FILES", "undeclared_tool.TICKETS",
                    "default.ASSISTED_REPORTING"):
            assert governed in p[pid], pid
        # With several contracts the default denial of a contract, as a
        # forbid, covers any principal for the tools of its tool servers.
        p = self.policies(self.gen(RICH)[0])
        assert '|| (resource == CADL::Actor::"T" || resource == CADL::Actor::"U"' \
            in p["closed.C2"]
        # A tool server that is a party of no enforced contract is not governed.
        assert "undeclared_tool.FREE" not in p and "undeclared_tool.U" in p

    def test_default_allow(self):
        src = source(norms(proh("n", "AG", "AG -> T : wipe")),
                     enforcement="enforcement: {default: allow}")
        p = self.policies(self.gen(src)[0])
        assert p["default.C"].startswith("permit") and "unless" not in p["default.C"]
        assert p["C.n"].startswith("forbid")

    def test_entities(self):
        _, table = self.gen(RICH)
        ents = table["entities"]
        assert {"uid": {"type": "CADL::Actor", "id": "AG"}, "attrs": {}, "parents": []} \
            in ents["actors"]
        (robots,) = ents["sets"]
        assert robots["uid"] == {"type": "CADL::ActorSet", "id": "R"}
        assert [m["uid"]["id"] for m in robots["members"]] == ["R[1]", "R[2]", "R[3]"]
        assert robots["members"][0]["parents"] == [{"type": "CADL::ActorSet", "id": "R"}]


class TestCodegenDeclaration:
    @pytest.mark.parametrize("target", ["agent-rego", "agent-cedar"])
    def test_undeclared_extension_is_an_error(self, target, tmp_path, capsys):
        sos = parse(source(*GOOD, declare=("norms: 0.1",)))
        with pytest.raises(ValueError, match="agents: 0.1"):
            generate(sos, tmp_path / "out", target=target)
        assert not (tmp_path / "out").exists()
        f = tmp_path / "f.cadl"
        f.write_text(source(*GOOD, declare=("norms: 0.1",)))
        assert main(["codegen", str(f), "-t", target, "-o", str(tmp_path / "out")]) == 1
        err = capsys.readouterr().err
        assert "does not declare" in err and "agents: 0.1" in err
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("target", ["agent-rego", "agent-cedar"])
    def test_cli(self, target, tmp_path, capsys):
        assert main(["codegen", str(EXAMPLE), "-t", target, "-o", str(tmp_path)]) == 0
        assert list(tmp_path.iterdir())
        assert not capsys.readouterr().err

    def test_targets_in_a_codegen_block_are_known(self):
        res = check(more="codegen:\n  - { target: agent-rego }\n  - { target: agent-cedar }\n")
        assert not [w for w in res.warnings if "codegen target" in str(w)]

    def test_opa_target_is_independent(self, tmp_path):
        generate(parse_file(EXAMPLE), tmp_path, target="opa")
        assert "agent_policy.rego" not in [p.name for p in tmp_path.iterdir()]
