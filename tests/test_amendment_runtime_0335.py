"""Versions while a system runs, from cadl 0.3.35 (Appendix K, K.11):
`previous:` inferred from the effective times, the key ``version`` of a
file without versions, versions together with regime bindings in
`cadl norms-eval` and the norms monitor of the Python target, the target
checks of `metrics.py` for entries from another version, and effective
times in `cadl agents-run` and the gateways."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadl.norms_eval import NormsEvalError, TimeBase, evaluate_files

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
TRACES = EXAMPLES / "traces"
V1 = EXAMPLES / "amendment_norms_v1.cadl"
V2 = EXAMPLES / "amendment_norms_v2.cadl"
HOUR = TimeBase(3600)


def _version(tmp: Path, name: str, version: str, previous: str | None, effective: str | None,
             transitional: str = "", pickup: str = "90s", base: Path = V2,
             extra: str = "") -> Path:
    """A version of amendment_norms with the given header (`previous` and
    `effective` may be left out) and rules."""
    text = base.read_text(encoding="utf-8")
    text = text.replace('version: "2"', f'version: "{version}"')
    head = text.index("  amendment:\n")
    tail = text.index("    layers:\n")
    block = "  amendment:\n"
    if previous is not None:
        block += f'    previous: "{previous}"\n'
    if effective is not None:
        block += f"    effective: {effective}\n"
    text = text[:head] + extra + block + transitional + text[tail:]
    text = text.replace("deadline: 90s", f"deadline: {pickup}")
    text = text.replace("        value: 90s", f"        value: {pickup}")
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


def _trace(tmp: Path, lines: list, name: str = "t.jsonl") -> Path:
    path = tmp / name
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def _norms(result: dict) -> dict:
    return {n["id"]: (n["state"], n["verdict"]) for n in result["norms"]}


def _same(a: dict, b: dict) -> bool:
    a, b = dict(a), dict(b)
    for r in (a, b):
        r.pop("file", None), r.pop("trace", None)
    return a == b


# --- A2: previous: inferred from the effective times --------------------------------

class TestPreviousInferred:
    SWITCH = TRACES / "amendment_norms_v1_a_switch.jsonl"

    def test_same_result_as_explicit_previous(self, tmp_path):
        v2 = _version(tmp_path, "v2.cadl", "2", None, "{ period: 2 }",
                      "    transitional:\n      contracts:\n        - { contract: DELIVERY_SLA, "
                      "rule: switch, in_states: [Assigned] }\n")
        v3 = _version(tmp_path, "v3.cadl", "3", None, "{ period: 3 }", pickup="60s")
        e2 = _version(tmp_path, "e2.cadl", "2", "1", "{ period: 2 }",
                      "    transitional:\n      contracts:\n        - { contract: DELIVERY_SLA, "
                      "rule: switch, in_states: [Assigned] }\n")
        e3 = _version(tmp_path, "e3.cadl", "3", "2", "{ period: 3 }", pickup="60s")
        for base in (HOUR, None):
            got = evaluate_files(V1, self.SWITCH, None, [v3, v2], base)
            ref = evaluate_files(V1, self.SWITCH, None, [e2, e3], base)
            assert got["versions"]["sequence"] == ["1", "2", "3"]
            assert _same(got, ref)

    def test_first_version_without_effective(self, tmp_path):
        first = _version(tmp_path, "f1.cadl", "1", None, None, base=V2)
        v2 = _version(tmp_path, "v2.cadl", "2", None, "{ period: 2 }")
        r = evaluate_files(first, self.SWITCH, None, [v2], HOUR)
        assert r["versions"]["sequence"] == ["1", "2"]

    def test_ambiguous_same_time(self, tmp_path):
        a = _version(tmp_path, "a.cadl", "2", "1", "{ period: 2 }")
        b = _version(tmp_path, "b.cadl", "2b", "1", "{ period: 2 }")
        c = _version(tmp_path, "c.cadl", "3", None, "{ period: 3 }")
        with pytest.raises(NormsEvalError, match="version 3 gives no previous:, and versions "
                                                 "2, 2b take effect at the same time"):
            evaluate_files(V1, self.SWITCH, None, [a, b, c], HOUR)

    def test_cannot_be_ordered(self, tmp_path):
        v2 = _version(tmp_path, "v2.cadl", "2", None, '{ at: "2026-01-01" }')
        with pytest.raises(NormsEvalError, match="cannot be ordered .* give previous:"):
            evaluate_files(V1, self.SWITCH, None, [v2])
        with pytest.raises(NormsEvalError, match="cannot be ordered"):
            evaluate_files(V1, self.SWITCH, None, [v2], HOUR)

    def test_explicit_previous_against_the_order_is_refused(self, tmp_path):
        v2 = _version(tmp_path, "v2.cadl", "2", "1", "{ period: 3 }")
        v3 = _version(tmp_path, "v3.cadl", "3", "2", "{ period: 2 }")
        with pytest.raises(NormsEvalError, match="not after version 2"):
            evaluate_files(V1, self.SWITCH, None, [v2, v3], HOUR)
        # the inferred one follows the order, so the explicit one conflicts
        v3 = _version(tmp_path, "v3.cadl", "3", None, "{ period: 2 }")
        with pytest.raises(NormsEvalError, match="name version 1 as previous"):
            evaluate_files(V1, self.SWITCH, None, [v2, v3], HOUR)

    def test_one_version_without_previous_is_0334(self, tmp_path):
        # one version without previous: the sequence of 0.3.34, also when
        # its effective time cannot be compared with the others
        v2 = _version(tmp_path, "v2.cadl", "2", "1", '{ at: "2026-01-01" }')
        r = evaluate_files(V1, self.SWITCH, None, [v2])
        assert r["versions"]["sequence"] == ["1", "2"]


# --- A3: the key `version` without versions -----------------------------------------

class TestVersionKeyWithoutVersions:
    FILE = EXAMPLES / "norms_amendment_v1.cadl"

    def test_own_version_accepted(self, tmp_path):
        got = evaluate_files(self.FILE, _trace(tmp_path, [{"t": 0, "version": "1.0.0"},
                                                          {"t": 5, "version": "1.0.0"}]))
        ref = evaluate_files(self.FILE, _trace(tmp_path, [{"t": 0}, {"t": 5}], "r.jsonl"))
        assert _same(got, ref) and "versions" not in got

    @pytest.mark.parametrize("value", ["2", 1, None])
    def test_other_value_refused(self, tmp_path, value):
        from cadl.norms_eval import TraceError
        with pytest.raises(TraceError, match="line 2: 'version' names .*declares no versions "
                                             "to switch to"):
            evaluate_files(self.FILE, _trace(tmp_path, [{"t": 0}, {"t": 1, "version": value}]))

    def test_monitor(self, tmp_path):
        import importlib.util
        import sys
        from cadl.codegen.norms_monitor_gen import generate_norms_monitor_module
        from cadl.parser import parse_file
        text = generate_norms_monitor_module(parse_file(self.FILE))
        file = tmp_path / "mon_a3.py"
        file.write_text(text, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("mon_a3", file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["mon_a3"] = mod
        spec.loader.exec_module(mod)
        m = mod.NormsMonitor()
        m.observe({"t": 0, "version": "1.0.0"})
        with pytest.raises(mod.TraceError, match="declares no versions to switch to"):
            m.observe({"t": 1, "version": "2"})
        m.observe({"t": 2})
