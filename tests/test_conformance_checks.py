"""`cadl check`: identifier rules, sharing entries, codegen targets, extensions.

These are the points where the reference implementation used to accept a
file silently although Appendix A, C or D of the specification asks for a
diagnostic.
"""

import json
import subprocess
import sys

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


@pytest.mark.parametrize("name", ["AND", "OR", "NOT", "IN", "in", "exists", "true", "false"])
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
    assert any("version '0.9'" in i for i in _messages(result.infos))


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


# --- review follow-ups -------------------------------------------------------

@pytest.mark.parametrize("word", ["true", "false"])
def test_unquoted_boolean_id_is_reported_as_a_reserved_word(word):
    source = _source().replace('- id: "DISPATCHER"', f"- id: {word}")
    errors = _messages(_check(source).errors)
    assert any(f"Actor ID '{word}' is a reserved word" in e for e in errors), errors


def test_metric_ids_and_regime_names_are_identifiers():
    extra = (
        "  transitions:\n"
        '    - from: "通常"\n'
        "      to: BUSY\n"
        '      condition: "load > 3"\n'
        "  metrics:\n"
        '    - id: "AND"\n'
        '      formula: "load"\n'
    )
    errors = _messages(_check(_source(extra=extra)).errors)
    assert any("Regime name '通常'" in e for e in errors), errors
    assert any("Metric ID 'AND' is a reserved word" in e for e in errors), errors


def test_codegen_entry_without_target_defaults_to_python():
    sos = parse(_source(extra="  codegen:\n    - target:\n    - output: out\n"))
    assert [c.target for c in sos.codegen] == ["python", "python"]
    assert not type_check(sos).warnings


def test_non_ascii_or_reserved_sharing_item_is_an_error():
    for item in ("位置", "2pos"):
        result = _check(_source(sharing=[f'"ROBOT[*] -> DISPATCHER : {item}"']))
        assert any("Invalid sharing entry" in e for e in _messages(result.errors)), item


@pytest.mark.parametrize("shape", [
    "  extensions:\n    sos-dsl: 0.1\n",          # mapping
    "  extensions:\n    - sos-dsl\n",             # plain string
    "  extensions:\n",                            # null
    "  extensions: []\n",                         # empty
])
def test_extension_declaration_shapes_are_accepted(shape):
    result = _check(_source(extra=shape))
    assert result.ok and not result.infos


@pytest.mark.parametrize("block", [
    "  motivation:\n    governance:\n      budget_base: .inf\n",
    "  motivation:\n    governance:\n      rho: .nan\n      kappa: " + "9" * 400 + "\n",
    "  motivation:\n    agent:\n      values: [0.1, x, .inf]\n",
    "  motivation: just some text\n",
    "  motivation: {}\n",
    "  motivation:\n    since: 2026-01-01\n",
])
def test_no_motivation_block_can_invalidate_a_file(block):
    sos = parse(_source(extra=block))          # must not raise
    result = type_check(sos)
    assert result.ok
    assert any("motivation: block" in i for i in _messages(result.infos))
    assert _ir_to_dict(lower_to_ir(sos))["motivation_block"] is not None


def test_motivation_block_keeps_key_order_and_nested_values():
    block = (
        "  motivation:\n"
        "    zeta: 1\n"
        "    agent:\n"
        "      values: [0.2, 0.9]\n"
        "      profile: custom\n"
        "    alpha_note: {nested: [1, {deep: true}]}\n"
    )
    ir = _ir_to_dict(lower_to_ir(parse(_source(extra=block))))
    kept = ir["motivation_block"]
    assert list(kept) == ["zeta", "agent", "alpha_note"]
    assert list(kept["agent"]) == ["values", "profile"]
    assert kept["alpha_note"] == {"nested": [1, {"deep": True}]}


# --- command line ------------------------------------------------------------

def _run(tmp_path, source, *args):
    path = tmp_path / "t.cadl"
    path.write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "cadl.cli", args[0], str(path), *args[1:]],
        capture_output=True, text=True,
    )


def test_cli_check_reports_infos_and_warnings_without_failing(tmp_path):
    extra = MOTIVATION + "  codegen:\n    - target: ros2\n"
    proc = _run(tmp_path, _source(extra=extra), "check")
    assert proc.returncode == 0
    assert "Type check passed" in proc.stdout and "(1 warning(s))" in proc.stdout
    assert "[INFO]" in proc.stderr and "[WARNING]" in proc.stderr


def test_cli_check_fails_on_a_bad_identifier(tmp_path):
    proc = _run(tmp_path, _source(actor_id="my-actor"), "check")
    assert proc.returncode == 1
    assert "not a valid identifier" in proc.stderr


def test_cli_verify_json_lists_infos(tmp_path):
    proc = _run(tmp_path, _source(extra=MOTIVATION), "verify", "--format", "json")
    report = json.loads(proc.stdout)
    assert len(report["type_check"]["infos"]) == 1


def test_cli_sim_ir_json_is_strict_json_for_odd_motivation_values(tmp_path):
    block = "  motivation:\n    since: 2026-01-01\n    governance:\n      kappa: .inf\n"
    proc = _run(tmp_path, _source(extra=block), "sim-ir", "--format", "json")
    assert proc.returncode == 0, proc.stderr
    ir = json.loads(proc.stdout, parse_constant=lambda c: pytest.fail(f"non-strict JSON: {c}"))
    assert ir["motivation_block"] == {"since": "2026-01-01", "governance": {"kappa": "inf"}}


def test_cli_sim_ir_without_motivation_has_no_extra_key(tmp_path):
    proc = _run(tmp_path, _source(), "sim-ir", "--format", "json")
    assert "motivation_block" not in json.loads(proc.stdout)
