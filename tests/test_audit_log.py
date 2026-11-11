"""Integrity of the audit log (Appendix G, G.8): the hash chain written by
`cadl agents-run --audit` and `cadl mcp-gateway`, and `cadl audit-verify`."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from cadl.agents_runtime import audit_lines, run_files
from cadl.audit_log import (
    GENESIS,
    AuditKeyError,
    canonical,
    chain_lines,
    file_sha256,
    key_id,
    read_key,
    read_records,
    verify,
)
from cadl.cli import main

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "agents_tool_governance.cadl"
CHANGE = ROOT / "examples" / "agents_change_control.cadl"
TRACES = ROOT / "examples" / "traces"
REJECTED = TRACES / "agents_tool_governance_b_rejected.jsonl"
APPROVED = TRACES / "agents_tool_governance_a_approved.jsonl"
KEY = b"0123456789abcdef-test-key"


def write_log(tmp_path, trace=REJECTED, cadl=EXAMPLE, key=None, name="audit.jsonl") -> Path:
    log = tmp_path / name
    args = ["agents-run", str(cadl), "--trace", str(trace), "--audit", str(log)]
    if key is not None:
        kf = tmp_path / f"{name}.key"
        kf.write_bytes(key + b"\n")
        args += ["--audit-key", str(kf)]
    assert main(args) == 0
    return log


def check(path, key=None, **kw) -> dict:
    return verify(path, key=key, **kw)


class TestChainFormat:
    def test_header_records_end(self, tmp_path, capsys):
        log = write_log(tmp_path)
        capsys.readouterr()
        lines = [json.loads(x) for x in log.read_text().splitlines()]
        head, end = lines[0], lines[-1]
        assert head["audit_format"] == "cadl-audit/1" and head["hash"] == "sha256"
        assert head["key_id"] is None
        assert head["source_sha256"] == hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()
        assert head["epoch"] == "2026-11-10T09:00:00Z"
        assert head["seq"] == 0 and head["prev"] == GENESIS
        assert end == {"audit_end": True, "records": len(lines) - 2, "seq": len(lines) - 1,
                       "prev": lines[-2]["digest"], "digest": end["digest"]}
        for k, line in enumerate(lines):
            assert list(line)[-3:] == ["seq", "prev", "digest"] and line["seq"] == k
            body = {f: v for f, v in line.items() if f != "digest"}
            assert line["digest"] == hashlib.sha256(canonical(body)).hexdigest()
            if k:
                assert line["prev"] == lines[k - 1]["digest"]

    def test_record_fields_unchanged(self, tmp_path, capsys):
        """The records of G.8 keep their fields, order and bytes: a line is
        the old line with the three integrity fields appended."""
        log = write_log(tmp_path, APPROVED)
        capsys.readouterr()
        old = audit_lines(run_files(EXAMPLE, APPROVED))
        new = log.read_text().splitlines()[1:-1]
        assert len(old) == len(new)
        for a, b in zip(old, new):
            assert b.startswith(a[:-1] + ', "seq": ')
        assert read_records(log) == [json.loads(x) for x in old]

    def test_canonical_form(self):
        assert canonical({"b": 1, "a": "é", "c": [1.5, None]}) == \
            '{"a":"é","b":1,"c":[1.5,null]}'.encode("utf-8")
        with pytest.raises(ValueError):
            canonical({"x": float("nan")})

    def test_canonical_numbers_and_strings(self):
        """G.8: integers in decimal, other numbers as the shortest decimal
        without exponent; keys by code point; only the escapes of JSON."""
        assert canonical([1e-07, 2.0, 0.1, 123.456, -0.5, 10 ** 15 + 0.5]) == \
            b"[0.0000001,2,0.1,123.456,-0.5,1000000000000000.5]"
        assert canonical({"\u00e9": 1, "z": 2, "Z": 3, "a\u2028": 4}) == \
            '{"Z":3,"a\u2028":4,"z":2,"\u00e9":1}'.encode("utf-8")
        assert canonical("a\"\\\n\x01/\u0085") == '"a\\"\\\\\\n\\u0001/\u0085"'.encode()
        for bad in (2 ** 53 + 1, -(2 ** 53) - 1, 1e300, "\ud800", {1: 2}):
            with pytest.raises(ValueError):
                canonical(bad)
        assert canonical(2 ** 53) == b"9007199254740992"

    def test_deterministic(self, tmp_path, capsys):
        """With a run id on the first line of the trace, the log is the same
        bytes every time; without it, agents-run draws a random run id, and
        only the header and the digests differ."""
        trace = tmp_path / "t.jsonl"
        lines = REJECTED.read_text().splitlines()
        first = json.loads(lines[0])
        first["run_id"] = "0123456789abcdef0123456789abcdef"
        trace.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n")
        a = write_log(tmp_path, trace, name="a.jsonl").read_bytes()
        b = write_log(tmp_path, trace, name="b.jsonl").read_bytes()
        assert a == b and b"0123456789abcdef0123456789abcdef" in a
        k1 = write_log(tmp_path, trace, key=KEY, name="k1.jsonl").read_bytes()
        k2 = write_log(tmp_path, trace, key=KEY, name="k2.jsonl").read_bytes()
        assert k1 == k2 and k1 != a
        r1 = write_log(tmp_path, name="r1.jsonl")
        r2 = write_log(tmp_path, name="r2.jsonl")
        h1, h2 = (json.loads(p.read_text().splitlines()[0]) for p in (r1, r2))
        assert h1["run_id"] != h2["run_id"]
        assert read_records(r1) == read_records(r2) == read_records(tmp_path / "a.jsonl")
        capsys.readouterr()

    def test_keyed(self, tmp_path, capsys):
        log = write_log(tmp_path, key=KEY)
        capsys.readouterr()
        lines = [json.loads(x) for x in log.read_text().splitlines()]
        assert lines[0]["hash"] == "hmac-sha256" and lines[0]["key_id"] == key_id(KEY)
        body = {f: v for f, v in lines[3].items() if f != "digest"}
        assert lines[3]["digest"] == hmac.new(KEY, canonical(body), hashlib.sha256).hexdigest()
        assert KEY.decode() not in log.read_text()
        assert check(log, KEY)["valid"]
        with pytest.raises(AuditKeyError, match="keyed"):
            check(log)                                  # usage error: no key
        r = check(log, b"another key of 16+ bytes")
        assert not r["valid"] and r["error"]["line"] == 1 and "key id" in r["error"]["what"]
        # an unkeyed log does not pass as keyed (a rewrite without the key)
        plain = write_log(tmp_path, name="plain.jsonl")
        capsys.readouterr()
        r = check(plain, KEY)
        assert not r["valid"] and "not keyed" in r["error"]["what"]

    def test_key_file(self, tmp_path):
        short = tmp_path / "short"
        short.write_bytes(b"tooshort\n")
        with pytest.raises(AuditKeyError, match="at least 16"):
            read_key(short)
        with pytest.raises(AuditKeyError, match="cannot read"):
            read_key(tmp_path / "missing")
        crlf = tmp_path / "crlf"
        crlf.write_bytes(KEY + b"\r\n")
        assert read_key(crlf) == KEY


def lines_of(path: Path) -> list[bytes]:
    return path.read_bytes().split(b"\n")[:-1]


def put(path: Path, lines: list[bytes]) -> None:
    path.write_bytes(b"".join(x + b"\n" for x in lines))


class TestTamperingIsDetected:
    @pytest.mark.parametrize("keyed", [False, True], ids=["sha256", "hmac"])
    def test_every_single_byte_change(self, tmp_path, capsys, keyed):
        key = KEY if keyed else None
        log = write_log(tmp_path, key=key)
        capsys.readouterr()
        data = log.read_bytes()
        assert check(log, key)["valid"]
        bad = tmp_path / "bad.jsonl"
        missed = []
        for i in range(len(data)):
            for delta in (0x01, 0x20):
                changed = bytearray(data)
                changed[i] ^= delta
                bad.write_bytes(bytes(changed))
                try:
                    ok = check(bad, key)["valid"]
                except AuditKeyError:
                    ok = False              # the header was made to say "keyed"
                if ok:
                    missed.append((i, delta))
        assert missed == []

    def test_deletion_insertion_reordering(self, tmp_path, capsys):
        log = write_log(tmp_path, APPROVED)
        other = write_log(tmp_path, REJECTED, name="other.jsonl")
        capsys.readouterr()
        lines = lines_of(log)
        bad = tmp_path / "bad.jsonl"
        n = len(lines)
        for k in range(n):                                  # deletion
            put(bad, lines[:k] + lines[k + 1:])
            assert not check(bad)["valid"], k
        for k in range(1, n):                               # insertion
            for extra in (lines_of(other)[3], lines[k - 1]):
                put(bad, lines[:k] + [extra] + lines[k:])
                r = check(bad)
                assert not r["valid"] and r["error"]["line"] == k + 1, k
        for k in range(n - 1):                              # swap of neighbours
            swapped = list(lines)
            swapped[k], swapped[k + 1] = swapped[k + 1], swapped[k]
            put(bad, swapped)
            assert not check(bad)["valid"], k

    def test_truncation(self, tmp_path, capsys):
        log = write_log(tmp_path)
        capsys.readouterr()
        lines = lines_of(log)
        bad = tmp_path / "bad.jsonl"
        for k in range(1, len(lines)):
            put(bad, lines[:k])
            r = check(bad)
            assert not r["valid"] and "no end record" in r["error"]["what"]
            r = check(bad, allow_open=True)                 # a running writer
            assert r["valid"] and not r["complete"] and r["lines"] == k
        bad.write_bytes(log.read_bytes()[:-5])              # cut inside a line
        assert "line end" in check(bad)["error"]["what"]
        bad.write_bytes(b"")
        assert not check(bad)["valid"]

    def test_rewritten_header(self, tmp_path, capsys):
        log = write_log(tmp_path)
        capsys.readouterr()
        lines = lines_of(log)
        head = json.loads(lines[0])
        head["epoch"] = "2030-01-01T00:00:00Z"
        bad = tmp_path / "bad.jsonl"
        put(bad, [json.dumps(head, ensure_ascii=False).encode()] + lines[1:])
        r = check(bad)
        assert not r["valid"] and r["error"]["line"] == 1 and "digest" in r["error"]["what"]

    def test_whitespace_and_escapes(self, tmp_path, capsys):
        """A change that keeps the values but not the bytes is detected."""
        log = write_log(tmp_path)
        capsys.readouterr()
        lines = lines_of(log)
        bad = tmp_path / "bad.jsonl"
        for text in (lines[2].replace(b", ", b",  ", 1), lines[2].replace(b"h", b"\\u0068", 1),
                     lines[2][:-1] + b', "seq": 99}'):
            put(bad, lines[:2] + [text] + lines[3:])
            r = check(bad)
            assert not r["valid"] and r["error"]["line"] == 3

    def test_a_full_rewrite_without_key_is_not_detected(self, tmp_path, capsys):
        """The limit of the unkeyed chain, stated in G.8: anyone can
        recompute it. With a key, the rewrite is detected."""
        log = write_log(tmp_path)
        keyed = write_log(tmp_path, key=KEY, name="keyed.jsonl")
        capsys.readouterr()
        records = read_records(log)
        records[1]["reason"] = "rewritten"
        head = json.loads(lines_of(log)[0])
        forged = chain_lines(records, head["source_sha256"], head["epoch"])
        fake = tmp_path / "forged.jsonl"
        fake.write_text("".join(x + "\n" for x in forged))
        assert check(fake)["valid"]
        k_head = json.loads(lines_of(keyed)[0])
        forged_k = chain_lines(records, k_head["source_sha256"], k_head["epoch"],
                               b"guessed key, not the real one")
        fake.write_text("".join(x + "\n" for x in forged_k))
        assert not check(fake, KEY)["valid"]

    def test_log_of_another_file(self, tmp_path, capsys):
        log = write_log(tmp_path)
        other = write_log(tmp_path, TRACES / "agents_change_control_a_freeze.jsonl", CHANGE,
                          name="change.jsonl")
        capsys.readouterr()
        assert check(log, source_sha256=file_sha256(EXAMPLE))["valid"]
        r = check(other, source_sha256=file_sha256(EXAMPLE))
        assert not r["valid"] and "another CADL file" in r["error"]["what"]
        # A line of another log does not fit into this chain.
        bad = tmp_path / "bad.jsonl"
        put(bad, lines_of(log)[:2] + [lines_of(other)[2]] + lines_of(log)[3:])
        assert check(bad)["error"]["line"] == 3


class TestAuditVerifyCommand:
    def test_exit_status_and_formats(self, tmp_path, capsys):
        log = write_log(tmp_path, key=KEY)
        kf = tmp_path / "audit.jsonl.key"
        capsys.readouterr()
        assert main(["audit-verify", str(log), "--key", str(kf)]) == 0
        out = capsys.readouterr().out
        assert "valid" in out and "complete" in out and "hmac-sha256" in out
        assert main(["audit-verify", str(log), "--key", str(kf), "--source", str(EXAMPLE),
                     "--format", "json"]) == 0
        r = json.loads(capsys.readouterr().out)
        assert r["valid"] and r["complete"] and r["records"] == len(read_records(log))
        assert main(["audit-verify", str(log), "--key", str(kf), "--source",
                     str(CHANGE)]) == 1
        assert "another CADL file" in capsys.readouterr().out
        data = bytearray(log.read_bytes())
        data[len(data) // 2] ^= 1
        log.write_bytes(bytes(data))
        assert main(["audit-verify", str(log), "--key", str(kf), "--format", "json"]) == 1
        r = json.loads(capsys.readouterr().out)
        assert not r["valid"] and r["error"]["line"] >= 2
        assert {"line", "what", "expected", "found"} == set(r["error"])

    def test_usage_errors(self, tmp_path, capsys):
        log = write_log(tmp_path, key=KEY)
        capsys.readouterr()
        assert main(["audit-verify", str(log)]) == 2                # keyed, no key
        assert main(["audit-verify", str(tmp_path / "none.jsonl")]) == 2
        assert main(["audit-verify", str(log), "--key", str(tmp_path / "none")]) == 2
        err = capsys.readouterr().err
        assert "give its key" in err and "File not found" in err
        assert main(["agents-run", str(EXAMPLE), "--trace", str(REJECTED),
                     "--audit-key", str(tmp_path / "audit.jsonl.key")]) == 2


class TestLimitsAndAnchors:
    """What the chain does not show by itself (G.8), and the run id and the
    anchor that close the gaps."""

    def test_a_keyed_log_rewritten_as_plain_verifies_without_key(self, tmp_path, capsys):
        """The header's `hash` is not authenticated: a verifier has to know
        from outside the log that it is keyed. With the key it fails."""
        keyed = write_log(tmp_path, key=KEY)
        capsys.readouterr()
        head = json.loads(lines_of(keyed)[0])
        records = read_records(keyed)
        records[1]["reason"] = "rewritten"
        fake = tmp_path / "fake.jsonl"
        fake.write_text("".join(x + "\n" for x in chain_lines(
            records, head["source_sha256"], head["epoch"], None, head["run_id"])))
        assert check(fake)["valid"]
        assert not check(fake, KEY)["valid"]

    def test_run_id_and_anchor(self, tmp_path, capsys):
        a = write_log(tmp_path, name="a.jsonl")
        b = write_log(tmp_path, name="b.jsonl")
        capsys.readouterr()
        ra, rb = check(a), check(b)
        assert ra["run_id"] != rb["run_id"]
        assert check(a, run_id=ra["run_id"], anchor=ra["last_digest"])["valid"]
        assert not check(a, run_id=rb["run_id"])["valid"]
        # a log that ends earlier, with a recomputed end record, verifies
        # without a key; the anchor shows it
        records = read_records(a)[:-2]
        head = json.loads(lines_of(a)[0])
        cut = tmp_path / "cut.jsonl"
        cut.write_text("".join(x + "\n" for x in chain_lines(
            records, head["source_sha256"], head["epoch"], None, head["run_id"])))
        assert check(cut)["valid"]
        r = check(cut, anchor=ra["last_digest"])
        assert not r["valid"] and "anchor" in r["error"]["what"]
        # the lines of another run do not fit: a different run id in the
        # header changes every digest after it
        put(tmp_path / "splice.jsonl", lines_of(a)[:3] + lines_of(b)[3:])
        assert not check(tmp_path / "splice.jsonl")["valid"]

    def test_header_needs_a_run_id_and_no_duplicate_keys(self, tmp_path, capsys):
        log = write_log(tmp_path)
        capsys.readouterr()
        lines = lines_of(log)
        dup = lines[2][:-1] + b', "time": "x"}'
        put(tmp_path / "dup.jsonl", lines[:2] + [dup] + lines[3:])
        r = check(tmp_path / "dup.jsonl")
        assert not r["valid"] and "duplicate" in r["error"]["what"]
        head = json.loads(lines[0])
        del head["run_id"]
        put(tmp_path / "norun.jsonl", [json.dumps(head).encode()] + lines[1:])
        assert "run id" in check(tmp_path / "norun.jsonl")["error"]["what"]
