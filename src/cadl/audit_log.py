"""Tamper-evident audit log of the agent governance extension (Appendix G,
G.8, "Integrity of the log").

An audit log is a file of JSON Lines. Its first line is a **header
record**, then come the audit and event records of G.8 in the order in
which the enforcement point wrote them, and a complete log ends with an
**end record**. Every line carries three integrity fields after the
fields of its record:

``seq``     the number of the line, from 0 for the header;
``prev``    the digest of the previous line; 64 zeros in the header;
``digest``  the digest of this line: SHA-256, or HMAC-SHA256 with the
            key of the log, over the canonical JSON of the line without
            its ``digest`` field, as lowercase hexadecimal.

Canonical JSON: the object with its keys sorted, no whitespace outside
strings (separators ``,`` and ``:``), strings as UTF-8 without ``\\u``
escapes for non-ASCII characters, numbers as Python's ``json`` module
writes them, NaN and infinities refused (``allow_nan=False``).

A line is written as ``json.dumps(line, ensure_ascii=False)``: the
fields of the record in the order of G.8, then ``seq``, ``prev`` and
``digest``. A verifier reads the line, recomputes the digest from its
values, and also requires the line to be exactly this written form, so
that a change of whitespace or of an escape that leaves the values equal
is detected as well.

Without a key the chain detects accidental and partial changes: any
change of a byte, a deleted, inserted or reordered line, and a changed
header. It does not protect against someone who rewrites the whole log
from the changed line on, because the digests can be recomputed by
anyone. With a key (HMAC-SHA256) only a holder of the key can do that.
Truncation of the tail is detected by the missing end record; someone
who can recompute the chain can also write a new end record, so with
no key, truncation is detected only when the last digest was kept
elsewhere.

Standard library only (`hashlib`, `hmac`, `json`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

FORMAT = "cadl-audit/1"
GENESIS = "0" * 64
INTEGRITY_FIELDS = ("seq", "prev", "digest")
ALGORITHMS = ("sha256", "hmac-sha256")
MIN_KEY_BYTES = 16
_KEY_ID_LABEL = b"cadl-audit key id"


class AuditKeyError(ValueError):
    """A key file that cannot be used."""


def canonical(obj: Any) -> bytes:
    """The canonical JSON of a value, as UTF-8 bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def written(obj: Any) -> str:
    """The written form of a line (without the newline)."""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)


def read_key(path) -> bytes:
    """The key of a keyed log: the bytes of the file, without one trailing
    line end. It has to have at least 16 bytes."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise AuditKeyError(f"cannot read the key file {path}: {e.strerror or e}") from None
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    if len(data) < MIN_KEY_BYTES:
        raise AuditKeyError(f"the key file {path} has {len(data)} byte(s); a key has at "
                            f"least {MIN_KEY_BYTES}")
    return data


def key_id(key: bytes) -> str:
    """An identifier of a key that does not reveal it: the first 16 hex
    digits of HMAC-SHA256(key, "cadl-audit key id")."""
    return hmac.new(key, _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:16]


def file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(body: dict, key: bytes | None) -> str:
    data = canonical(body)
    if key is None:
        return hashlib.sha256(data).hexdigest()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


class AuditChain:
    """Writes the lines of one log. `header()` first, then `record()` for
    every record of G.8, then `end()` for a complete log. Each call returns
    one line without the newline."""

    def __init__(self, source_sha256: str, epoch: str | None, key: bytes | None = None):
        self.key = key
        self.source_sha256 = source_sha256
        self.epoch = epoch
        self.seq = 0
        self.prev = GENESIS
        self.records = 0
        self.ended = False

    def _line(self, fields: dict) -> str:
        if self.ended:
            raise ValueError("the log has ended")
        for name in INTEGRITY_FIELDS:
            if name in fields:
                raise ValueError(f"a record of the log has no field '{name}'")
        body = dict(fields)
        body["seq"] = self.seq
        body["prev"] = self.prev
        body["digest"] = _digest(body, self.key)
        self.prev = body["digest"]
        self.seq += 1
        return written(body)

    def header(self) -> str:
        if self.seq != 0:
            raise ValueError("the header is the first line")
        return self._line({
            "audit_format": FORMAT,
            "hash": "sha256" if self.key is None else "hmac-sha256",
            "key_id": None if self.key is None else key_id(self.key),
            "source_sha256": self.source_sha256,
            "epoch": self.epoch,
        })

    def record(self, rec: dict) -> str:
        if self.seq == 0:
            raise ValueError("the header comes first")
        self.records += 1
        return self._line(rec)

    def end(self) -> str:
        line = self._line({"audit_end": True, "records": self.records})
        self.ended = True
        return line

    @property
    def last_digest(self) -> str:
        return self.prev


def epoch_text(epoch) -> str | None:
    """The epoch of a trace as the header writes it (UTC, ISO 8601)."""
    if epoch is None:
        return None
    from datetime import timezone
    at = epoch.astimezone(timezone.utc)
    text = at.strftime("%Y-%m-%dT%H:%M:%S")
    if at.microsecond:
        text += f".{at.microsecond:06d}".rstrip("0")
    return text + "Z"


def chain_lines(records: list[dict], source_sha256: str, epoch: str | None,
                key: bytes | None = None) -> list[str]:
    """A complete log: header, records, end record."""
    chain = AuditChain(source_sha256, epoch, key)
    return [chain.header()] + [chain.record(r) for r in records] + [chain.end()]


def strip_integrity(line_obj: dict) -> dict:
    """A line without its integrity fields."""
    return {k: v for k, v in line_obj.items() if k not in INTEGRITY_FIELDS}


def read_records(path) -> list[dict]:
    """The records of G.8 of a log, without the header, the end record and
    the integrity fields. Does not verify the log."""
    out = []
    for text in Path(path).read_text(encoding="utf-8").splitlines():
        obj = json.loads(text)
        if "audit_format" in obj or "audit_end" in obj:
            continue
        out.append(strip_integrity(obj))
    return out


# === Verification ===========================================================

class _Invalid(Exception):
    def __init__(self, line: int, what: str, expected: Any = None, found: Any = None):
        super().__init__(what)
        self.line, self.what, self.expected, self.found = line, what, expected, found


def _loads(text: str) -> Any:
    def bad_constant(name: str):
        raise ValueError(f"{name} is not JSON")
    return json.loads(text, parse_constant=bad_constant)


def verify(path, key: bytes | None = None, source_sha256: str | None = None,
           allow_open: bool = False) -> dict:
    """Verify a log. Returns a dict with `valid`, `error` (None, or `line`
    (1-based), `what`, `expected`, `found`), and what was read: `lines`,
    `records`, `hash`, `key_id`, `source_sha256`, `epoch`, `complete`
    (an end record closes the log) and `last_digest`.

    Raises AuditKeyError when the log is keyed and no key is given (the
    chain cannot be checked: a usage error)."""
    data = Path(path).read_bytes()
    out: dict[str, Any] = {
        "file": str(path), "valid": False, "error": None, "lines": 0, "records": 0,
        "hash": None, "key_id": None, "source_sha256": None, "epoch": None,
        "complete": False, "last_digest": None,
    }
    try:
        _verify(data, key, source_sha256, allow_open, out)
        out["valid"] = True
    except _Invalid as e:
        out["error"] = {"line": e.line, "what": e.what, "expected": e.expected,
                        "found": e.found}
    return out


def _verify(data: bytes, key, source_sha256, allow_open, out) -> None:
    if not data:
        raise _Invalid(1, "the log is empty: it has no header")
    parts = data.split(b"\n")
    if parts[-1] != b"":
        raise _Invalid(len(parts), "the last line has no line end: the log was cut "
                                   "inside a line")
    parts = parts[:-1]
    prev = GENESIS
    algorithm = None
    ended = False
    for n, raw in enumerate(parts, start=1):
        if ended:
            raise _Invalid(n, "a line after the end record")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise _Invalid(n, f"not UTF-8 ({e.reason})") from None
        try:
            obj = _loads(text)
        except ValueError as e:
            raise _Invalid(n, f"not JSON ({e})") from None
        if not isinstance(obj, dict):
            raise _Invalid(n, "not a JSON object")
        try:
            form = written(obj)
        except ValueError as e:
            raise _Invalid(n, f"not in the written form ({e})") from None
        if form != text:
            raise _Invalid(n, "the line is not in the written form (its bytes differ from "
                              "json.dumps of its values: whitespace, escapes, a number "
                              "format or a duplicate key were changed)")
        for name in INTEGRITY_FIELDS:
            if name not in obj:
                raise _Invalid(n, f"the field '{name}' is missing")
        if list(obj)[-3:] != list(INTEGRITY_FIELDS):
            raise _Invalid(n, "the integrity fields are not the last three fields")
        seq = n - 1
        if obj["seq"] != seq or isinstance(obj["seq"], bool):
            raise _Invalid(n, "wrong sequence number (a line was deleted, inserted or "
                              "moved)", seq, obj["seq"])
        if obj["prev"] != prev:
            raise _Invalid(n, "'prev' is not the digest of the previous line (a line was "
                              "deleted, inserted, moved or changed)", prev, obj["prev"])
        if n == 1:
            if obj.get("audit_format") != FORMAT:
                raise _Invalid(1, "the first line is not a header of " + FORMAT,
                               FORMAT, obj.get("audit_format"))
            algorithm = obj.get("hash")
            if algorithm not in ALGORITHMS:
                raise _Invalid(1, "unknown hash algorithm", " or ".join(ALGORITHMS),
                               algorithm)
            out.update(hash=algorithm, key_id=obj.get("key_id"),
                       source_sha256=obj.get("source_sha256"), epoch=obj.get("epoch"))
            if algorithm == "hmac-sha256" and key is None:
                raise AuditKeyError("the log is keyed (hmac-sha256, key id "
                                    f"{obj.get('key_id')}); give its key with --key")
            if algorithm == "sha256" and key is not None:
                raise _Invalid(1, "a key was given, but the log is not keyed: it may have "
                                  "been rewritten without the key", "hmac-sha256", "sha256")
            if key is not None and obj.get("key_id") != key_id(key):
                raise _Invalid(1, "the key id of the header is not that of the given key "
                                  "(another key, or the header was changed)",
                               key_id(key), obj.get("key_id"))
        elif "audit_format" in obj:
            raise _Invalid(n, "a second header")
        body = {k: v for k, v in obj.items() if k != "digest"}
        expected = _digest(body, key if algorithm == "hmac-sha256" else None)
        if obj["digest"] != expected:
            raise _Invalid(n, "the digest does not match the content of the line (it was "
                              "changed)", expected, obj["digest"])
        if n == 1 and source_sha256 is not None and obj["source_sha256"] != source_sha256:
            raise _Invalid(1, "the log was written for another CADL file",
                           source_sha256, obj["source_sha256"])
        if "audit_end" in obj:
            if obj["audit_end"] is not True or obj.get("records") != seq - 1:
                raise _Invalid(n, "the end record does not count the records before it",
                               seq - 1, obj.get("records"))
            ended = True
        prev = obj["digest"]
        out["lines"] = n
        out["records"] = max(0, seq - 1) if ended else seq
        out["last_digest"] = prev
    out["complete"] = ended
    if not ended and not allow_open:
        raise _Invalid(len(parts) + 1, "no end record: the log was truncated at its tail, "
                                       "or its writer did not finish", "an end record",
                       "end of file")


def format_verify_text(result: dict) -> str:
    if result["valid"]:
        state = "complete" if result["complete"] else \
            "open (no end record; truncation of the tail is not detectable)"
        return "\n".join([
            f"{result['file']}: valid",
            f"  lines: {result['lines']} (header, {result['records']} record(s)"
            + (", end record)" if result["complete"] else ")"),
            f"  hash: {result['hash']}" + (f" (key id {result['key_id']})"
                                           if result["key_id"] else ""),
            f"  source sha256: {result['source_sha256']}",
            f"  epoch: {result['epoch']}",
            f"  state: {state}",
            f"  last digest: {result['last_digest']}",
        ])
    e = result["error"]
    lines = [f"{result['file']}: INVALID at line {e['line']}: {e['what']}"]
    if e["expected"] is not None or e["found"] is not None:
        lines.append(f"  expected: {e['expected']}")
        lines.append(f"  found:    {e['found']}")
    if result["lines"]:
        lines.append(f"  lines 1 to {result['lines']} are intact")
    return "\n".join(lines)
