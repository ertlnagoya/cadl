"""Code generation rejects values that would escape a literal or a path."""

import pytest

from cadl.codegen import generate
from cadl.codegen.safety import UnsafeCodegenInput, check_codegen_input
from cadl.parser import parse


def _sos(role="worker", contract_id="C1", name="Demo"):
    return parse(f"""
sos:
  name: {name!r}
  type: Directed
  actors:
    - id: A
      role: {role!r}
  contracts:
    - id: {contract_id!r}
      parties: [A]
""")


def test_plain_definition_is_accepted():
    check_codegen_input(_sos())


@pytest.mark.parametrize("role", [
    'x"\n    import os\n    Z = "',
    "it's",
    'say "hi"',
    "trailing\\",
    "end */ of comment",
])
def test_text_that_ends_a_literal_or_comment_is_rejected(role):
    with pytest.raises(UnsafeCodegenInput):
        check_codegen_input(_sos(role=role))


@pytest.mark.parametrize("contract_id", ["../../ESCAPED", "a/b", "C 1", "1C", ""])
def test_ids_must_be_identifiers(contract_id):
    with pytest.raises(UnsafeCodegenInput):
        check_codegen_input(_sos(contract_id=contract_id))


@pytest.mark.parametrize("target", ["python", "solidity", "opa"])
def test_generate_writes_nothing_for_unsafe_input(tmp_path, target):
    out = tmp_path / "deep" / "out"
    with pytest.raises(UnsafeCodegenInput):
        generate(_sos(contract_id="../../ESCAPED"), out, target=target)
    assert not list(tmp_path.rglob("*.*"))
