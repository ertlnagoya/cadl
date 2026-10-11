"""`cadl check`: identifier rules, sharing entries, codegen targets, extensions.

These are the points where the reference implementation used to accept a
file silently although Appendix A, C or D of the specification asks for a
diagnostic.
"""

import json

import pytest

from cadl.cli import _ir_to_dict
from cadl.parser import parse
from cadl.sim import lower_to_ir
from cadl.type_checker import type_check


def _source(actor_id="DISPATCHER", contract_id="C1", protocol_id="P1", sharing=None, extra=""):
    sharing_block = ""
    if sharing is not None:
        sharing_block = (
            "      information:\n"
            "        alpha: 0.5\n"
            "        sharing:\n" + "".join(f"          - {s}\n" for s in sharing)
        )
    return (
        "sos:\n"
        '  name: "T"\n'
        "  type: Directed\n"
        "  actors:\n"
        f'    - id: "{actor_id}"\n'
        "      role: coordinator\n"
        "    - id: ROBOT[1..N]\n"
        "      role: agent\n"
        "  contracts:\n"
        f'    - id: "{contract_id}"\n'
        f'      parties: ["{actor_id}", "ROBOT[*]"]\n'
        + sharing_block +
        "  protocols:\n"
        f'    - id: "{protocol_id}"\n'
        '      trigger: "start"\n'
        "      steps:\n"
        f'        - "{actor_id} -> ROBOT[*] : go"\n'
        + extra
    )


def _check(source):
    return type_check(parse(source))


def _messages(items):
    return [str(i) for i in items]


# --- identifiers (Appendix A, A.1 and A.11) ---------------------------------

def test_plain_identifiers_pass():
    result = _check(_source())
    assert result.ok and not result.warnings and not result.infos


@pytest.mark.parametrize("name", ["配車係", "my-actor", "2ND"])
def test_actor_id_must_be_an_ascii_identifier(name):
    errors = _messages(_check(_source(actor_id=name)).errors)
    assert any("Actor ID" in e and "not a valid identifier" in e for e in errors), errors


@pytest.mark.parametrize("name", ["AND", "OR", "NOT", "IN", "in", "exists", "true"])
def test_reserved_word_is_not_an_actor_id(name):
    errors = _messages(_check(_source(actor_id=name)).errors)
    assert any("Actor ID" in e and "reserved word" in e for e in errors), errors


def test_contract_and_protocol_ids_are_checked():
    errors = _messages(_check(_source(contract_id="契約-1", protocol_id="OR")).errors)
    assert any("Contract ID" in e and "not a valid identifier" in e for e in errors), errors
    assert any("Protocol ID" in e and "reserved word" in e for e in errors), errors


def test_for_and_all_are_not_reserved():
    assert _check(_source(actor_id="for", contract_id="all")).ok


def test_unicode_text_outside_identifiers_is_fine():
    source = _source().replace('name: "T"', 'name: "ロボット配送"  # 日本語のコメント')
    assert _check(source).ok


# --- sharing entries (Appendix A, A.4) --------------------------------------

def test_quoted_sharing_entry_is_kept():
    sos = parse(_source(sharing=['"ROBOT[*] -> DISPATCHER : position"']))
    info = sos.contracts[0].information
    assert [s.data for s in info.sharing] == ["position"]
    assert not info.invalid_sharing and not info.lenient_sharing
    assert type_check(sos).ok


def test_unquoted_sharing_entry_is_an_error():
    result = _check(_source(sharing=["ROBOT[*] -> DISPATCHER : position"]))
    errors = _messages(result.errors)
    assert any("Invalid sharing entry" in e and "quoted string" in e for e in errors), errors


def test_sharing_entry_without_arrow_is_an_error():
    result = _check(_source(sharing=['"position of every robot"']))
    assert any("Invalid sharing entry" in e for e in _messages(result.errors))


def test_call_style_item_is_accepted_with_a_warning():
    sos = parse(_source(sharing=['"ROBOT[*] -> DISPATCHER : position(period: 500ms)"']))
    assert [s.data for s in sos.contracts[0].information.sharing] == ["position"]
    result = type_check(sos)
    assert result.ok
    assert any("not an identifier" in w for w in _messages(result.warnings))


# --- codegen targets (Appendix D, D.4) --------------------------------------

@pytest.mark.parametrize("target", ["python", "solidity", "opa", "unity-csharp"])
def test_supported_codegen_target_has_no_diagnostic(target):
    result = _check(_source(extra=f"  codegen:\n    - target: {target}\n"))
    assert result.ok and not result.warnings


def test_unsupported_codegen_target_is_reported():
    result = _check(_source(extra="  codegen:\n    - target: ros2\n"))
    assert result.ok  # the file is valid CADL; the processor cannot emit the target
    assert any("'ros2' is not supported by this processor" in w for w in _messages(result.warnings))


def test_simulator_target_in_codegen_points_to_sim_gen():
    result = _check(_source(extra="  codegen:\n    - target: unity\n"))
    assert any("cadl sim-gen -t unity" in w for w in _messages(result.warnings))


# --- extensions and the motivation block (Appendices A.2, C) -----------------

MOTIVATION = (
    "  motivation:\n"
    "    agent:\n"
    "      profile: linear\n"
    "    governance:\n"
    "      model: commitment_budget\n"
    "      rho: 0.5\n"
)


def test_known_extension_has_no_diagnostic():
    result = _check(_source(extra="  extensions:\n    - sos-dsl: 0.1\n"))
    assert result.ok and not result.infos


def test_unknown_extension_gets_an_informational_diagnostic():
    result = _check(_source(extra="  extensions:\n    - my-ext: 1.0\n"))
    assert result.ok and not result.warnings
    assert any("'my-ext' is not known" in i for i in _messages(result.infos))


def test_other_version_of_a_known_extension_is_noted():
    result = _check(_source(extra="  extensions:\n    - sos-dsl: 0.9\n"))
    assert any("version 0.9" in i for i in _messages(result.infos))


def test_motivation_block_is_never_an_error_and_is_noted():
    result = _check(_source(extra=MOTIVATION))
    assert result.ok and not result.warnings
    assert any("motivation: block" in i for i in _messages(result.infos))


def test_motivation_block_is_parsed():
    sos = parse(_source(extra=MOTIVATION))
    assert sos.motivation.agent.profile == "linear"
    assert sos.motivation.governance.model == "commitment_budget"
    assert sos.motivation.governance.rho == 0.5


def test_motivation_block_is_passed_on_verbatim_by_sim_ir():
    ir = _ir_to_dict(lower_to_ir(parse(_source(extra=MOTIVATION))))
    assert ir["motivation_block"] == {
        "agent": {"profile": "linear"},
        "governance": {"model": "commitment_budget", "rho": 0.5},
    }
    json.dumps(ir)  # stays serializable


def test_sim_ir_of_a_file_without_motivation_is_unchanged():
    ir = _ir_to_dict(lower_to_ir(parse(_source())))
    assert "motivation_block" not in ir
