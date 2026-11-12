"""Comparison of the generated boundary policies with the decision function
of G.5 on one input space (Appendix G, G.10, G.13): the Cedar policies
with the cedar CLI, the Rego policy with OPA. See tests/agent_policy_diff.py
for the inputs and the rule of G.10 for Cedar.

Environment variables: CADL_AGENT_POLICY_FILES, the number of random files
(default 3; the long run of the report used 300); CADL_AGENT_POLICY_OPA_ALL=1
evaluates every input with OPA (by default every 25th, which keeps the
run short: OPA takes about 1.5 ms per input).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from cadl.agents import build_policy, decide
from cadl.codegen.agent_policy.cedar_gen import generate_agent_cedar_text
from cadl.parser import parse

from . import agent_policy_diff as D
from .test_agents_extension import RICH, msg

FILES = int(os.environ.get("CADL_AGENT_POLICY_FILES", "3"))
OPA_STRIDE = 1 if os.environ.get("CADL_AGENT_POLICY_OPA_ALL") == "1" else 25

needs_cedar = pytest.mark.skipif(D.CEDAR is None, reason="the cedar binary is not on PATH")
needs_opa = pytest.mark.skipif(D.OPA is None, reason="the opa binary is not on PATH")


def test_input_space_of_the_fixed_file():
    # three variants, every message of 12 senders, 9 receivers and 9 names,
    # without `instances` and with 16 instance sets
    assert len(D.rich_inputs()) == 12 * 9 * 9 * 17 == 16524


@needs_cedar
def test_cedar_on_the_fixed_file(tmp_path):
    total: dict = {}
    for name, src in D.rich_variants(RICH).items():
        D.add_counts(total, D.compare_cedar(parse(src), D.rich_inputs(), tmp_path / name))
    assert total["inputs"] == 3 * 16524
    assert set(total["clauses"]) == {"a: ungoverned", "a: undeclared tool",
                                     "b: static prohibition", "c: the assumptions hold"}
    # Every reason for which G.10 leaves an input to the runtime is needed:
    # for each, some input of that reason has a decision that differs from
    # the Cedar answer.
    assert set(total["left"]) == {
        "a governing contract has no running instance",
        "a permission that is not static can match",
        "a prohibition that is not static can match",
        "a derived instance can match"}
    assert all(total["left_differ"][k] > 0 for k in total["left"]), total


@needs_cedar
def test_cedar_on_random_files():
    total = D.run_random(seed=7, n_files=FILES, n_sets=4, opa=False)
    assert total["files"] == FILES
    assert total["cedar"]["inputs"] == FILES * len(D.RANDOM_SENDERS) * len(
        D.RANDOM_RECEIVERS) * len(D.RANDOM_NAMES) * 4


@needs_opa
def test_opa_on_the_fixed_file(tmp_path):
    for name, src in D.rich_variants(RICH).items():
        inputs = D.rich_inputs()[::OPA_STRIDE]
        assert D.compare_opa(parse(src), inputs, tmp_path / name) == len(inputs)


@needs_opa
def test_opa_on_random_files():
    import random
    rng = random.Random(7)
    for _ in range(max(1, FILES // 3)):
        text = D.random_valid_file(rng)
        sos = parse(text)
        inputs = D.random_inputs(rng, build_policy(sos), 4)[::OPA_STRIDE]
        with tempfile.TemporaryDirectory() as d:
            D.compare_opa(sos, inputs, Path(d))


# --- The case that the comparison found -------------------------------------------

NOT_RUNNING = """\
sos:
  name: N
  extensions:
    - norms: 0.1
    - agents: 0.1
  actors:
    - {id: AG, role: agent, agent: {kind: llm_agent}}
    - {id: M, role: monitor, agent: {kind: service}}
  contracts:
    - id: C
      parties: [AG, M]
      enforcement: {}
      norms:
        - {id: quiet, kind: prohibition, bearer: AG, content: "AG -> M : report"}
"""


@needs_cedar
def test_static_prohibition_of_a_contract_without_instance(tmp_path):
    """Up to cadl 0.3.26 the generated README said that a Deny by the
    forbid of a static prohibition is final. A contract without a running
    instance decides an ordinary message by its default (G.5, step 3.1),
    so the decision is allow while Cedar denies; G.10 does not let Cedar
    decide this input (clause b)."""
    sos = parse(NOT_RUNNING)
    policy = build_policy(sos)
    answers = D.cedar_answers(sos, policy, [("AG", "M", "report")], tmp_path)
    answer = answers["answers"][("AG", "M", "report")]
    assert answer == ("deny", ["C.quiet"])
    inp = msg("AG", "M", "report", instances=[])
    assert decide(policy, inp)["decision"] == "allow"
    assert D.cedar_decides(policy, answers["table"], inp, answer) == (
        False, "a governing contract has no running instance")
    assert D.cedar_decides(policy, answers["table"], msg("AG", "M", "report"), answer) == (
        True, "b: static prohibition")


def test_readme_states_the_rule():
    text, _ = generate_agent_cedar_text(parse(NOT_RUNNING))
    readme = text[:text.index("\n\n")]
    assert "is final" not in readme
    for part in ("(a) Allow by `ungoverned`", "(b) Deny by the forbid of a static prohibition",
                 "when its contract has a", "(c) any answer"):
        assert part in readme
