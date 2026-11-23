"""Decision records (Appendix L, decisions 0.2): writing and verifying.

A decision record is a file of JSON Lines beside a model that records how
its amendments were decided: the parties and their roles, then entries
(objectives, proposals, measurements, checks, votes, requests,
recommendations, decisions, versions, follow-ups, corrections, notes),
each with the earlier entries it rests on. Every line carries the
integrity fields of Appendix G, G.8 (``seq``, ``prev``, ``digest``), over
the canonical JSON of G.8, with SHA-256 or HMAC-SHA256.

The same evidence is asked of every party (L.4, "Evidence fields"): a
proposal, a recommendation and a decision give ``reason``,
``assumptions``, ``unknowns`` and ``predictions`` whether a person, an
LLM or a tool made them.

``verify(path)`` checks L-1 to L-8, and, for the versions whose CADL
files are at hand, L-9 to L-11; it reports the findings of L.6 for
review. ``DecisionChain`` writes a record.

decisions 0.2 (format ``cadl-decisions/2``) adds the ``layers`` of a party
(the layers on which it decides) and the ``fulfilled_by`` of a follow-up
(what a fulfilling entry must be). A record of ``cadl-decisions/1`` is
read as before, without them.

decisions 0.3 (format ``cadl-decisions/3``) adds the entry type
``delegation``: a party lets another decide in its place, on some layers,
by some procedures, until some point. A decision made on behalf of another
party within a delegation in force is not reported as delegated. A record
of ``cadl-decisions/2`` is read as before, without delegations.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Any, Optional

from .audit_log import (
    ALGORITHMS,
    GENESIS,
    INTEGRITY_FIELDS,
    AuditKeyError,
    canonical,
    file_sha256,
    is_run_id,
    key_id,
    loads_unique,
    new_run_id,
    written,
)

FORMAT = "cadl-decisions/3"
FORMAT_2 = "cadl-decisions/2"
FORMAT_1 = "cadl-decisions/1"
FORMATS = (FORMAT_1, FORMAT_2, FORMAT)
KEEP = "keep"

ROLES = ("owner", "proposer", "assessor", "voter", "advisor", "decider", "affected")
PARTY_KINDS = ("human", "organisation", "agent", "llm", "tool")
HEADER_FIELDS = ("decisions_format", "hash", "key_id", "run_id", "model", "time_base",
                 "started", "previous", "parties")
PARTY_FIELDS_1 = ("id", "kind", "roles", "name", "actor", "model", "on_behalf_of")
PARTY_FIELDS = PARTY_FIELDS_1 + ("layers",)
END_FIELDS = ("decisions_end", "entries")
COMMON = ("id", "type", "point", "written", "by", "basis", "fulfils", "attachments",
          "generated")
EVIDENCE = ("reason", "assumptions", "unknowns", "predictions")
TYPES = {
    "objective": ("text", "measure"),
    "proposal": ("kind", "title", "changes", "objective", "measure", "affected") + EVIDENCE,
    "measurement": ("of", "version", "method", "values", "per_party", "compared_with",
                    "runs_better"),
    "check": ("proposal", "results"),
    "vote": ("proposal", "value", "reason"),
    "request": ("to", "question", "options", "why"),
    "recommendation": ("request", "options", "recommends") + EVIDENCE,
    "decision": ("question", "options", "choice", "procedure", "rule", "affected",
                 "result") + EVIDENCE,
    "version": ("version", "previous", "effective", "source_sha256", "decision"),
    "follow_up": ("text", "due", "decision", "fulfilled_by"),
    "delegation": ("to", "layers", "procedures", "conditions", "until", "reason"),
    "correction": ("corrects", "text"),
    "note": ("text",),
}
# the role that `by` must have (L-5); None: any party
ROLE_OF = {"objective": "owner", "proposal": "proposer", "measurement": "assessor",
           "check": "assessor", "vote": "voter", "recommendation": "advisor",
           "decision": "decider", "delegation": "decider"}
PROCEDURES = ("single", "vote", "override", "automatic")
EXPECTED = ("increase", "decrease", "unchanged", "yes", "no")
LAYERS = ("rule", "contract", "regulation")
ROLES_IN_MEASURE = ("goal", "watch", "guard")
FINDINGS = ("override", "against recommendation", "delegated", "unknowns",
            "prediction missed", "affected not measured", "unmeasured choice",
            "reading not decided", "open follow-up", "fulfilment not shown", "not amendable",
            "corrected", "open record")
FULFILLED_BY = ("type", "of", "same_method_as", "subjects")

_ID_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_SUBJECT_RE = re.compile(r"(?:metric:(?P<metric>[^@]+)(?:@(?P<party>.+))?|vote:(?P<voter>.+))")
_COND_RE = re.compile(r"\s*(<=|>=|<|>|==)\s*(-?\d+(?:\.\d+)?)\s*")
# A measured value counts as unchanged within this share of the compared value.
UNCHANGED_TOLERANCE = 0.01


def _digest(body: dict, key: Optional[bytes]) -> str:
    data = canonical(body)
    if key is None:
        return hashlib.sha256(data).hexdigest()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


# === Writing ==================================================================

class DecisionChain:
    """Writes the lines of one record: ``header()`` first, then ``entry()``
    for every entry, then ``end()`` for a complete record. Each call returns
    one line without the newline."""

    def __init__(self, key: Optional[bytes] = None, run_id: Optional[str] = None,
                 format: str = FORMAT):
        run_id = run_id or new_run_id()
        if not is_run_id(run_id):
            raise ValueError("a run id is 32 lowercase hexadecimal digits")
        if format not in FORMATS:
            raise ValueError(f"the format is one of {', '.join(FORMATS)}")
        self.run_id, self.key, self.format = run_id, key, format
        self.seq, self.prev, self.entries, self.ended = 0, GENESIS, 0, False

    def _line(self, fields: dict) -> str:
        if self.ended:
            raise ValueError("the record has ended")
        for name in INTEGRITY_FIELDS:
            if name in fields:
                raise ValueError(f"an entry has no field '{name}'")
        body = dict(fields)
        body["seq"], body["prev"] = self.seq, self.prev
        body["digest"] = _digest(body, self.key)
        self.prev = body["digest"]
        self.seq += 1
        return written(body)

    def header(self, model: dict, parties: list, started: str, time_base: Optional[str] = None,
               previous: Optional[dict] = None) -> str:
        if self.seq != 0:
            raise ValueError("the header is the first line")
        if self.format != FORMAT_1:
            # decisions 0.2: a party without `layers` decides on any layer
            parties = [p if not isinstance(p, dict) or "layers" in p else {**p, "layers": None}
                       for p in parties]
        return self._line({
            "decisions_format": self.format,
            "hash": "sha256" if self.key is None else "hmac-sha256",
            "key_id": None if self.key is None else key_id(self.key),
            "run_id": self.run_id, "model": model, "time_base": time_base,
            "started": started, "previous": previous, "parties": parties,
        })

    def entry(self, fields: dict) -> str:
        if self.seq == 0:
            raise ValueError("the header comes first")
        self.entries += 1
        if self.format != FORMAT_1 and fields.get("type") == "follow_up" \
                and "fulfilled_by" not in fields:
            fields = {**fields, "fulfilled_by": None}    # decisions 0.2: any later entry
        return self._line(fields)

    def end(self) -> str:
        line = self._line({"decisions_end": True, "entries": self.entries})
        self.ended = True
        return line

    @property
    def last_digest(self) -> str:
        return self.prev


# === Verification =============================================================

class _Stop(Exception):
    """A line that breaks L-1 or L-2: the rest cannot be read."""

    def __init__(self, rule: str, line: int, what: str):
        super().__init__(what)
        self.rule, self.line, self.what = rule, line, what


class _Record:
    def __init__(self):
        self.header: dict = {}
        self.parties: dict[str, dict] = {}
        self.entries: list[dict] = []       # without integrity fields
        self.lines: dict[str, int] = {}     # entry id -> line number
        self.format = FORMAT
        self.index: dict[str, int] = {}     # entry id -> position in entries
        self.complete = False


def _at_value(text: str):
    """An ISO 8601 date or date-time as a comparable UTC value, or None."""
    from datetime import datetime, timezone
    t = text.strip()
    if t.endswith("Z") or t.endswith("z"):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _point_key(p: Any):
    """("period", k) or ("at", UTC datetime), or None when p is not a time point."""
    if not isinstance(p, dict) or len(p) != 1:
        return None
    if "period" in p:
        k = p["period"]
        if isinstance(k, int) and not isinstance(k, bool) and k >= 1:
            return ("period", k)
        return None
    if "at" in p and isinstance(p["at"], str):
        v = _at_value(p["at"])
        return None if v is None else ("at", v)
    return None


def _point_text(p: Any) -> str:
    if isinstance(p, dict) and len(p) == 1:
        k, v = next(iter(p.items()))
        return f"period {v}" if k == "period" else str(v)
    return "?"


def _later(a, b) -> Optional[bool]:
    """a after b, for points of the same form; None when they cannot be compared."""
    ka, kb = _point_key(a), _point_key(b)
    if ka is None or kb is None or ka[0] != kb[0]:
        return None
    return ka[1] > kb[1]


def verify(path, key: Optional[bytes] = None, run_id: Optional[str] = None,
           anchor: Optional[str] = None, versions: Optional[list] = None) -> dict:
    """Verify a decision record. Returns a dict with ``valid`` (no error),
    ``errors`` (``rule``, ``line``, ``entry``, ``what``), ``findings``
    (``finding``, ``entry``, ``line``, ``text``), ``not_checked``, and what
    was read: ``lines``, ``entries``, ``parties``, ``complete``, ``hash``,
    ``key_id``, ``run_id``, ``last_digest``, ``versions_checked``,
    ``delegated_under`` (decision id -> the delegation that covers it), and
    ``predictions``: how many predictions there are, how many a later entry
    checked (``checked``, of which ``missed``), and, for the rest, why not
    (``not_checked``: ``keep``, ``no later entry``, ``cannot compare``).

    ``versions`` are CADL files of versions of the model, in addition to the
    files that the ``attachments`` of the ``version`` entries name (relative
    to the record). Raises AuditKeyError when the record is keyed and no key
    is given."""
    path = Path(path)
    out: dict[str, Any] = {
        "file": str(path), "valid": False, "errors": [], "findings": [], "not_checked": [],
        "lines": 0, "entries": 0, "parties": 0, "complete": False, "hash": None,
        "key_id": None, "run_id": None, "last_digest": None, "versions_checked": [],
        "delegated_under": {},
        "predictions": {"total": 0, "checked": 0, "missed": 0, "not_checked": {
            "keep": 0, "no later entry": 0, "cannot compare": 0}},
    }
    rec = _Record()
    try:
        _read(path.read_bytes(), key, rec, out)
    except _Stop as e:
        out["errors"].append({"rule": e.rule, "line": e.line, "entry": None, "what": e.what})
        return out
    if run_id is not None and out["run_id"] != run_id:
        out["errors"].append({"rule": "L-2", "line": 1, "entry": None,
                              "what": f"the record has run id {out['run_id']}, not {run_id}"})
    if anchor is not None and out["last_digest"] != anchor:
        out["errors"].append({"rule": "L-2", "line": out["lines"], "entry": None,
                              "what": "the digest of the last line is not the anchor (the "
                                      "record was cut, extended or rewritten)"})
    v = _Verifier(rec, out)
    v.structure()
    v.versions(path.parent, [Path(p) for p in versions or []])
    v.findings()
    out["valid"] = not out["errors"]
    return out


def _read(data: bytes, key, rec: _Record, out: dict) -> None:
    if not data:
        raise _Stop("L-1", 1, "the record is empty: it has no header")
    parts = data.split(b"\n")
    if parts[-1] != b"":
        raise _Stop("L-2", len(parts), "the last line has no line end: the record was cut "
                                       "inside a line")
    parts = parts[:-1]
    prev, algorithm = GENESIS, None
    for n, raw in enumerate(parts, start=1):
        if rec.complete:
            raise _Stop("L-2", n, "a line after the end record")
        try:
            text = raw.decode("utf-8")
            obj = loads_unique(text)
        except (UnicodeDecodeError, ValueError) as e:
            raise _Stop("L-1", n, f"not a JSON object in UTF-8 ({e})") from None
        if not isinstance(obj, dict):
            raise _Stop("L-1", n, "not a JSON object")
        try:
            form = written(obj)
        except ValueError as e:
            raise _Stop("L-2", n, f"not in the written form of G.8 ({e})") from None
        if form != text:
            raise _Stop("L-2", n, "the line is not in the written form of G.8 (whitespace, "
                                  "escapes or the form of a number were changed)")
        if list(obj)[-3:] != list(INTEGRITY_FIELDS):
            raise _Stop("L-2", n, "the integrity fields seq, prev, digest are not the last "
                                  "three fields")
        if obj["seq"] != n - 1 or isinstance(obj["seq"], bool):
            raise _Stop("L-2", n, f"wrong sequence number {obj['seq']!r}, expected {n - 1} (a "
                                  "line was deleted, inserted or moved)")
        if obj["prev"] != prev:
            raise _Stop("L-2", n, "'prev' is not the digest of the previous line (a line was "
                                  "deleted, inserted, moved or changed)")
        if n == 1:
            if obj.get("decisions_format") not in FORMATS:
                raise _Stop("L-1", 1, "the first line is not a header of "
                                      f"{' or '.join(FORMATS)}")
            rec.format = obj["decisions_format"]
            algorithm = obj.get("hash")
            if algorithm not in ALGORITHMS:
                raise _Stop("L-1", 1, f"unknown hash algorithm {algorithm!r}")
            if not is_run_id(obj.get("run_id")):
                raise _Stop("L-1", 1, "the header has no run id (32 hexadecimal digits)")
            out.update(hash=algorithm, key_id=obj.get("key_id"), run_id=obj.get("run_id"))
            if algorithm == "hmac-sha256" and key is None:
                raise AuditKeyError(f"the record is keyed (key id {obj.get('key_id')}); give "
                                    "its key with --key")
            if algorithm == "sha256" and key is not None:
                raise _Stop("L-2", 1, "a key was given, but the record is not keyed: it may "
                                      "have been rewritten without the key")
            if key is not None and obj.get("key_id") != key_id(key):
                raise _Stop("L-2", 1, "the key id of the header is not that of the given key")
        elif "decisions_format" in obj:
            raise _Stop("L-1", n, "a second header")
        body = {k: v for k, v in obj.items() if k != "digest"}
        if obj["digest"] != _digest(body, key if algorithm == "hmac-sha256" else None):
            raise _Stop("L-2", n, "the digest does not match the content of the line (it "
                                  "was changed)")
        fields = {k: v for k, v in obj.items() if k not in INTEGRITY_FIELDS}
        if n == 1:
            rec.header = fields
        elif "decisions_end" in obj:
            count = fields.get("entries")
            if fields.get("decisions_end") is not True or isinstance(count, bool) \
                    or count != n - 2 or set(fields) != set(END_FIELDS):
                raise _Stop("L-1", n, f"the end record does not count the {n - 2} entries "
                                      "before it")
            rec.complete = True
        else:
            fields["_line"] = n
            rec.entries.append(fields)
        prev = obj["digest"]
        out["lines"], out["last_digest"] = n, prev
    out["complete"] = rec.complete
    out["entries"] = len(rec.entries)


_S, _S0 = "text", "text or null"
_LS, _LD = "list of texts", "list of objects"
_D, _D0, _L0, _I0 = "object", "object or null", "list or null", "integer or null"
_CHECK = {
    _S: lambda v: isinstance(v, str),
    _S0: lambda v: v is None or isinstance(v, str),
    _LS: lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    _LD: lambda v: isinstance(v, list) and all(isinstance(x, dict) for x in v),
    _D: lambda v: isinstance(v, dict),
    _D0: lambda v: v is None or isinstance(v, dict),
    _L0: lambda v: v is None or isinstance(v, list),
    _I0: lambda v: v is None or (isinstance(v, int) and not isinstance(v, bool)),
}
SHAPE_COMMON = {"id": _S, "type": _S, "point": _D, "written": _S, "by": _S, "basis": _LS,
                "fulfils": _LS, "attachments": _LD, "generated": _D0}
SHAPE_EVIDENCE = {"reason": _S0, "assumptions": _LS, "unknowns": _LS, "predictions": _LD}
SHAPE = {
    "objective": {"text": _S, "measure": _L0},
    "proposal": {"kind": _S, "title": _S, "changes": _L0, "objective": _S0, "measure": _L0,
                 "affected": _LS, **SHAPE_EVIDENCE},
    "measurement": {"of": _S, "version": _S0, "method": _D, "values": _D, "per_party": _D0,
                    "compared_with": _S0, "runs_better": _I0},
    "check": {"proposal": _S, "results": _LD},
    "vote": {"proposal": _S, "value": _S, "reason": _S0},
    "request": {"to": _S, "question": _S, "options": _LS, "why": _LD},
    "recommendation": {"request": _S0, "options": _LD, "recommends": _S0, **SHAPE_EVIDENCE},
    "decision": {"question": _S0, "options": _LS, "choice": _S, "procedure": _S, "rule": _S0,
                 "affected": _LS, "result": _S0, **SHAPE_EVIDENCE},
    "version": {"version": _S, "previous": _S0, "effective": _D, "source_sha256": _S,
                "decision": _S0},
    "follow_up": {"text": _S, "due": _D0, "decision": _S0, "fulfilled_by": _D0},
    "delegation": {"to": _S, "layers": _L0, "procedures": _L0, "conditions": _LS,
                   "until": _D0, "reason": _S},
    "correction": {"corrects": _S, "text": _S},
    "note": {"text": _S},
}
SHAPE_PARTY = {"id": _S, "kind": _S, "roles": _LS, "name": _S0, "actor": _S0, "model": _S0,
               "on_behalf_of": _S0, "layers": "list or null"}
# the members of objects inside lists: name -> (keys, member shapes)
_NUM = "number"
_SCALAR = "number, text or null"
_BOOL0 = "true, false or null"
_CHECK.update({
    _NUM: lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    _SCALAR: lambda v: v is None or isinstance(v, str) or (isinstance(v, (int, float))
                                                           and not isinstance(v, bool)),
    _BOOL0: lambda v: v is None or isinstance(v, bool),
})
MEMBERS = {
    "attachments": {"path": _S, "sha256": _S, "media_type": _S0},
    "predictions": {"option": _S, "subject": _S, "expected": _S, "note": _S0},
    "changes": {"parameter": _S, "from": _SCALAR, "to": _SCALAR, "layer": _S0},
    "measure": {"metric": _S, "role": _S, "direction": _S0, "target": _S0},
    "results": {"name": _S, "passed": _BOOL0, "note": _S0},
    "why": {"option": _S, "reason": _S},
    "rec_options": {"option": _S, "summary": _S, "affected": _S},
    "method": {"runs": _I0, "window": _S0, "description": _S0},
    "generated": {"model": _S, "request_digest": _S},
    "fulfilled_by": {"type": _S0, "of": _S0, "same_method_as": _S0, "subjects": _LS},
}


def _actor_matches(pattern: Optional[str], actor: Optional[str]) -> bool:
    """`decided_by:` or a voter of K.3 against the actor of a party: equal, or
    an indexed reference with `[*]` that covers `NAME[i]`."""
    if not pattern or not actor:
        return False
    if pattern == actor:
        return True
    if pattern.endswith("[*]"):
        return actor.startswith(pattern[:-3] + "[") and actor.endswith("]")
    return False


# the analyses of CADL files, by the SHA-256 of their bytes
_ANALYSES: dict = {}


class _Verifier:
    def __init__(self, rec: _Record, out: dict):
        self.rec, self.out = rec, out
        self.models: dict[str, Any] = {}      # sha256 -> (path, analysis)
        self.bad: set = set()                 # ids of entries whose shape is wrong
        self.adopted_ids: set = set()         # choices of the decisions (L-10)

    @property
    def v1(self) -> bool:
        return self.rec.format == FORMAT_1

    def fields_of(self, t: str) -> tuple:
        if self.v1 and t == "follow_up":
            return ("text", "due", "decision")
        return TYPES[t]

    # --- reporting ---------------------------------------------------------
    def err(self, rule: str, e: Optional[dict], what: str, line: Optional[int] = None) -> None:
        self.out["errors"].append({
            "rule": rule, "line": line if line is not None else (e or {}).get("_line"),
            "entry": (e or {}).get("id") if isinstance((e or {}).get("id"), str) else None,
            "what": what})

    def find(self, kind: str, e: dict, text: str) -> None:
        self.out["findings"].append({"finding": kind, "entry": e.get("id"),
                                     "line": e.get("_line"), "text": text})

    def entry(self, eid: Any) -> Optional[dict]:
        """An earlier entry of a good shape, by id."""
        if not isinstance(eid, str) or eid in self.bad:
            return None
        i = self.rec.index.get(eid)
        return None if i is None else self.rec.entries[i]

    def good(self) -> list:
        return [e for e in self.rec.entries if not e.get("_bad")]

    # --- shapes (L-1) ---------------------------------------------------------
    def shape(self, e: dict, shapes: dict, where: str, line: Optional[int] = None) -> bool:
        ok = True
        for f, kind in shapes.items():
            if f in e and not _CHECK[kind](e[f]):
                self.err("L-1", e if line is None else None,
                         f"{where}: {f} is not {kind} ({e[f]!r})"[:300], line=line)
                ok = False
        return ok

    def members(self, e: dict, field: str, items: Any, kind: str) -> bool:
        if items is None:
            return True
        if kind == "method":
            items = [items]
        keys = MEMBERS[kind]
        ok = True
        for x in items:
            if not isinstance(x, dict) or set(x) != set(keys):
                self.err("L-1", e, f"an item of {field} has the members {', '.join(keys)}: "
                                   f"{x!r}"[:300])
                ok = False
            elif not self.shape(x, keys, f"an item of {field}", line=e.get("_line")):
                ok = False
        return ok

    def entry_shape(self, e: dict) -> bool:
        t = e["type"]
        ok = self.shape(e, SHAPE_COMMON, f"a {t} entry", line=e.get("_line"))
        ok = self.shape(e, SHAPE[t], f"a {t} entry", line=e.get("_line")) and ok
        if not ok:
            return False
        ok = self.members(e, "attachments", e["attachments"], "attachments")
        if e["generated"] is not None:
            ok = self.members(e, "generated", [e["generated"]], "generated") and ok
        if e.get("fulfilled_by") is not None:
            ok = self.members(e, "fulfilled_by", [e["fulfilled_by"]], "fulfilled_by") and ok
        if "predictions" in e:
            ok = self.members(e, "predictions", e["predictions"], "predictions") and ok
        if t == "proposal":
            ok = self.members(e, "changes", e["changes"], "changes") and ok
            ok = self.members(e, "measure", e["measure"], "measure") and ok
        elif t == "objective":
            ok = self.members(e, "measure", e["measure"], "measure") and ok
        elif t == "check":
            ok = self.members(e, "results", e["results"], "results") and ok
        elif t == "request":
            ok = self.members(e, "why", e["why"], "why") and ok
        elif t == "recommendation":
            ok = self.members(e, "options", e["options"], "rec_options") and ok
        elif t == "measurement":
            ok = self.members(e, "method", e["method"], "method") and ok
            if not all(_CHECK[_NUM](v) for v in e["values"].values()):
                self.err("L-1", e, "values map metric names to numbers")
                ok = False
            pp = e["per_party"]
            if pp is not None and not all(isinstance(v, dict) and all(_CHECK[_NUM](x)
                                                                      for x in v.values())
                                          for v in pp.values()):
                self.err("L-1", e, "per_party maps parties to maps of numbers")
                ok = False
        return ok

    # --- L-1, L-3 to L-8 ------------------------------------------------------
    def structure(self) -> None:
        h = self.rec.header
        if set(h) != set(HEADER_FIELDS):
            self.err("L-1", None, f"the header has the fields {sorted(h)}, not those of L.3",
                     line=1)
        model = h.get("model")
        if not (isinstance(model, dict) and set(model) == {"name", "version", "source_sha256"}
                and isinstance(model["name"], str) and _CHECK[_S0](model["version"])
                and _CHECK[_S0](model["source_sha256"])):
            self.err("L-1", None, "the header's model has name, version and source_sha256",
                     line=1)
        if not isinstance(h.get("started"), str) or not _CHECK[_S0](h.get("time_base")) \
                or not _CHECK[_D0](h.get("previous")):
            self.err("L-1", None, "started is a time stamp, time_base a text or null, previous "
                                  "an object or null", line=1)
        parties = h.get("parties")
        if not isinstance(parties, list) or not parties:
            self.err("L-1", None, "the header lists no parties", line=1)
            parties = []
        for p in parties:
            self.party(p)
        self.out["parties"] = len(self.rec.parties)
        for p in self.rec.parties.values():
            ob = p["on_behalf_of"]
            if ob is not None and (ob not in self.rec.parties or ob == p["id"]):
                self.err("L-8", None, f"party '{p['id']}': on_behalf_of {ob!r} is not "
                                      "another party", line=1)
            elif ob is not None:
                mine, theirs = p.get("layers"), self.rec.parties[ob].get("layers")
                if theirs is not None and (mine is None or set(mine) - set(theirs)):
                    self.err("L-8", None, f"party '{p['id']}' acts on behalf of '{ob}' on the "
                                          f"layers {', '.join(mine) if mine else 'all'}; "
                                          f"'{ob}' decides on {', '.join(theirs) or 'none'}",
                             line=1)
        last_point = None
        for i, e in enumerate(self.rec.entries):
            eid = e.get("id")
            t = e.get("type")
            if not isinstance(eid, str) or not _ID_RE.fullmatch(eid):
                self.err("L-1", e, f"the entry has no valid id ({eid!r})"[:300])
                e["_bad"] = True
                continue
            if eid in self.rec.index:
                self.err("L-3", e, f"the entry id '{eid}' is used twice")
                e["_bad"] = True
                continue
            self.rec.index[eid] = i
            if not isinstance(t, str) or t not in TYPES or (
                    t == "delegation" and self.rec.format != FORMAT):
                self.err("L-1", e, f"unknown type {t!r}"[:300])
                e["_bad"] = True
                self.bad.add(eid)
                continue
            want = set(COMMON) | set(self.fields_of(t)) | {"_line"}
            if set(e) != want:
                miss, extra = sorted(want - set(e)), sorted(set(e) - want)
                self.err("L-1", e, f"a {t} entry " + "; ".join(
                    x for x in (f"lacks {', '.join(miss)}" if miss else "",
                                f"has fields not of L.4: {', '.join(extra)}" if extra else "")
                    if x))
                e["_bad"] = True
                self.bad.add(eid)
                continue
            if not self.entry_shape(e):
                e["_bad"] = True
                self.bad.add(eid)
                continue
            self.common(e)
            getattr(self, "t_" + t)(e)
            if _point_key(e["point"]) is not None:
                if last_point is not None and _later(last_point, e["point"]):
                    self.err("L-4", e, f"the point {_point_text(e['point'])} is before the "
                                       f"point {_point_text(last_point)} of an earlier entry")
                last_point = e["point"]

    def party(self, p: Any) -> None:
        fields = PARTY_FIELDS_1 if self.v1 else PARTY_FIELDS
        if not isinstance(p, dict) or set(p) != set(fields):
            self.err("L-1", None, f"a party has the fields {', '.join(fields)}: {p!r}"[:300],
                     line=1)
            return
        if not self.shape(p, SHAPE_PARTY, "a party", line=1):
            return
        pid = p["id"]
        if not _ID_RE.fullmatch(pid):
            self.err("L-1", None, f"party id {pid!r} is not valid", line=1)
            return
        if pid in self.rec.parties:
            self.err("L-3", None, f"the party id '{pid}' is used twice", line=1)
            return
        if p["kind"] not in PARTY_KINDS:
            self.err("L-1", None, f"party '{pid}': kind {p['kind']!r} is not one of "
                                  f"{', '.join(PARTY_KINDS)}", line=1)
        if not p["roles"] or any(r not in ROLES for r in p["roles"]):
            self.err("L-1", None, f"party '{pid}': roles {p['roles']!r} are not roles of L.2",
                     line=1)
        layers = p.get("layers")
        if layers is not None and (not all(isinstance(x, str) and x in LAYERS for x in layers)
                                   or len(set(layers)) != len(layers)):
            self.err("L-1", None, f"party '{pid}': layers {layers!r} are not distinct layers of "
                                  f"{', '.join(LAYERS)}", line=1)
            p = {**p, "layers": None}
        if p["kind"] in ("llm", "tool") and not p["model"]:
            self.err("L-8", None, f"party '{pid}' of kind {p['kind']} gives no model", line=1)
        self.rec.parties[pid] = p

    def ref(self, e: dict, field: str, value: Any, types: tuple, keep: bool = False,
            null: bool = False) -> Optional[dict]:
        """An id that names an earlier entry of one of `types` (L-4)."""
        if value is None and null:
            return None
        if keep and value == KEEP:
            return None
        if value in self.bad:
            return None                      # its own error is reported
        target = self.entry(value)
        if target is None:
            self.err("L-4", e, f"{field} {value!r} is not the id of an earlier entry")
            return None
        if types and target.get("type") not in types:
            self.err("L-4", e, f"{field} '{value}' is a {target.get('type')} entry, not "
                               f"{' or '.join(types)}")
        return target

    def ids(self, e: dict, field: str, value: list, types: tuple = (), keep: bool = False) -> list:
        return [self.ref(e, field, v, types, keep) for v in value]

    def parties_of(self, e: dict, field: str, value: list) -> None:
        for a in value:
            if a not in self.rec.parties:
                self.err("L-1", e, f"{field} names {a!r}, which is not a party of the header")

    def common(self, e: dict) -> None:
        if _point_key(e["point"]) is None:
            self.err("L-1", e, f"point {e['point']!r} is neither {{\"period\": k}} (k ≥ 1) nor "
                               "{\"at\": <ISO 8601>}")
        party = self.rec.parties.get(e["by"])
        if party is None:
            self.err("L-5", e, f"by {e['by']!r} is not a party of the header")
        else:
            role = ROLE_OF.get(e["type"])
            if role and role not in party["roles"]:
                self.err("L-5", e, f"a {e['type']} entry is made by a party with the role "
                                   f"{role}; '{e['by']}' has {', '.join(party['roles'])}")
        self.ids(e, "basis", e["basis"])
        self.ids(e, "fulfils", e["fulfils"], ("follow_up",))
        if any(not _HEX64_RE.fullmatch(a["sha256"]) for a in e["attachments"]):
            self.err("L-1", e, "the sha256 of an attachment is 64 hexadecimal digits")

    def evidence(self, e: dict) -> None:
        automatic = e["type"] == "decision" and e["procedure"] == "automatic"
        if not automatic and not (e["reason"] or "").strip():
            self.err("L-8", e, f"a {e['type']} gives a reason, whoever makes it "
                               f"('{e['by']}')")
        for pr in e["predictions"]:
            if pr["option"] != e["id"]:
                self.ref(e, "prediction option", pr["option"], ("proposal",), keep=True)
            m = _SUBJECT_RE.fullmatch(pr["subject"])
            if not m:
                self.err("L-8", e, f"prediction subject {pr['subject']!r} is neither "
                                   "metric:<name>[@<party>] nor vote:<party>")
            else:
                who = m.group("voter") or m.group("party")
                if who is not None and who not in self.rec.parties:
                    self.err("L-8", e, f"prediction subject {pr['subject']!r} names no party")
                elif m.group("voter") and "voter" not in self.rec.parties[who]["roles"]:
                    self.err("L-8", e, f"prediction subject {pr['subject']!r}: '{who}' is not "
                                       "a voter")
            exp = pr["expected"]
            if not (exp in EXPECTED or _COND_RE.fullmatch(exp)):
                self.err("L-8", e, f"prediction expected {exp!r} is not one of "
                                   f"{', '.join(EXPECTED)} or a condition such as \"<= 1\"")

    def options(self, e: dict) -> list:
        if not e["options"]:
            self.err("L-1", e, "options is a non-empty list")
        self.ids(e, "options", e["options"], ("proposal",), keep=True)
        return e["options"]

    def measure_items(self, e: dict, items: list) -> None:
        if any(m["role"] not in ROLES_IN_MEASURE or m["direction"] not in ("increase",
                                                                           "decrease", None)
               for m in items):
            self.err("L-1", e, "a measure item has role goal, watch or guard and direction "
                               "increase, decrease or null")

    # --- types --------------------------------------------------------------
    def t_objective(self, e):
        if not e["text"].strip():
            self.err("L-1", e, "an objective has its text")
        if e["measure"] is not None:
            self.measure_items(e, e["measure"])

    def t_proposal(self, e):
        self.evidence(e)
        if e["kind"] == "amendment":
            if not e["changes"] or any(c["layer"] not in LAYERS + (None,) for c in e["changes"]):
                self.err("L-1", e, "an amendment lists its changes, each with a layer of rule, "
                                   "contract, regulation or null")
        elif e["kind"] == "measure":
            self.ref(e, "objective", e["objective"], ("objective",))
            if e["measure"] is None:
                self.err("L-1", e, "a reading gives its measure items")
            else:
                self.measure_items(e, e["measure"])
        else:
            self.err("L-1", e, f"proposal kind {e['kind']!r} is not amendment or measure")
        self.parties_of(e, "affected", e["affected"])

    def t_measurement(self, e):
        if e["of"] != "in_force":
            self.ref(e, "of", e["of"], ("proposal",))
        self.ref(e, "compared_with", e["compared_with"], ("measurement",), null=True)
        if e["per_party"] is not None:
            self.parties_of(e, "per_party", list(e["per_party"]))

    def t_check(self, e):
        self.ref(e, "proposal", e["proposal"], ("proposal",))

    def t_vote(self, e):
        self.ref(e, "proposal", e["proposal"], ("proposal",))
        if e["value"] not in ("yes", "no", "abstain"):
            self.err("L-1", e, f"a vote is yes, no or abstain, not {e['value']!r}")

    def t_request(self, e):
        to = self.rec.parties.get(e["to"])
        if to is None:
            self.err("L-1", e, f"to {e['to']!r} is not a party of the header")
        elif "decider" not in to["roles"]:
            self.err("L-1", e, f"a request goes to a decider; '{e['to']}' is not one")
        self.options(e)
        for w in e["why"]:
            if w["option"] not in e["options"]:
                self.err("L-1", e, f"why names {w['option']!r}, which is not an option")

    def t_recommendation(self, e):
        self.evidence(e)
        self.ref(e, "request", e["request"], ("request",), null=True)
        for o in e["options"]:
            self.ref(e, "option", o["option"], ("proposal",), keep=True)
        if e["recommends"] is not None:
            self.ref(e, "recommends", e["recommends"], ("proposal",), keep=True)

    def t_decision(self, e):
        self.evidence(e)
        opts = self.options(e)
        if e["choice"] not in opts:
            self.err("L-6", e, f"the choice {e['choice']!r} is not one of the options")
        proc = e["procedure"]
        if proc not in PROCEDURES:
            self.err("L-1", e, f"procedure {proc!r} is not one of {', '.join(PROCEDURES)}")
        if proc == "automatic":
            party = self.rec.parties.get(e["by"], {})
            if not (e["rule"] or "").strip():
                self.err("L-6", e, "an automatic decision states its rule")
            if party.get("kind") != "tool":
                self.err("L-6", e, f"an automatic decision is made by a tool, not by "
                                   f"'{e['by']}' ({party.get('kind')})")
        elif e["rule"] is not None:
            self.err("L-1", e, "rule is null unless the procedure is automatic")
        adopted = self.adopted(e)
        layers = (self.rec.parties.get(e["by"]) or {}).get("layers")
        if adopted is not None and layers is not None:
            outside = sorted({c["layer"] for c in adopted.get("changes") or []
                              if c.get("layer") and c["layer"] not in layers})
            if outside:
                self.err("L-5", e, f"'{e['by']}' decides on the layers "
                                   f"{', '.join(layers) or 'none'}; {adopted['id']} changes "
                                   f"{', '.join(outside)}")
        if proc in ("vote", "override"):
            on = self.voted_on(e)
            given = {x["id"] for x in self.basis(e, "vote") if x["proposal"] == on}
            cast = {x["id"] for x in self.good()[:self.rec.index[e["id"]]]
                    if x.get("type") == "vote" and x.get("proposal") == on}
            if on is None or not given:
                self.err("L-6", e, f"a decision by {proc} has the votes on "
                                   f"{on or 'the proposal'} in its basis")
            elif cast - given:
                self.err("L-6", e, f"the basis leaves out votes on {on}: "
                                   f"{', '.join(sorted(cast - given))}")
        self.parties_of(e, "affected", e["affected"])

    def t_version(self, e):
        d = self.ref(e, "decision", e["decision"], ("decision",), null=True)
        if not e["version"]:
            self.err("L-1", e, "a version entry names its sos.version")
        if _point_key(e["effective"]) is None:
            self.err("L-1", e, "effective is a time point")
        if not _HEX64_RE.fullmatch(e["source_sha256"]):
            self.err("L-1", e, "source_sha256 is 64 hexadecimal digits")
        if e["decision"] is None and e["previous"] is not None:
            self.err("L-7", e, "a version that replaces another names the decision that "
                               "adopted it")
        if d is not None:
            choice = self.entry(d.get("choice"))
            if choice is None or choice.get("type") != "proposal":
                self.err("L-7", e, f"decision '{d['id']}' adopts no proposal")
            if d.get("result") != e["version"]:
                self.err("L-7", e, f"decision '{d['id']}' has result {d.get('result')!r}, "
                                   f"not {e['version']!r}")

    def t_follow_up(self, e):
        self.ref(e, "decision", e["decision"], ("decision",), null=True)
        if e["due"] is not None and _point_key(e["due"]) is None:
            self.err("L-1", e, "due is a time point or null")
        fb = e.get("fulfilled_by")
        if fb is None:
            return
        if fb["type"] is not None and fb["type"] not in TYPES:
            self.err("L-1", e, f"fulfilled_by: type {fb['type']!r} is not a type of L.4")
        measured = ("of", "same_method_as", "subjects")
        if fb["type"] != "measurement" and any(fb[k] for k in measured):
            self.err("L-1", e, "fulfilled_by: of, same_method_as and subjects are for a "
                               "measurement")
        if fb["of"] not in (None, "in_force"):
            self.ref(e, "fulfilled_by of", fb["of"], ("proposal",))
        self.ref(e, "fulfilled_by same_method_as", fb["same_method_as"], ("measurement",),
                 null=True)
        for s in fb["subjects"]:
            m = _SUBJECT_RE.fullmatch(s)
            if not m or m.group("voter"):
                self.err("L-1", e, f"fulfilled_by: subject {s!r} is not metric:<name>[@<party>]")
            elif m.group("party") and m.group("party") not in self.rec.parties:
                self.err("L-1", e, f"fulfilled_by: subject {s!r} names no party")

    def t_delegation(self, e):
        to = self.rec.parties.get(e["to"])
        if to is None:
            self.err("L-1", e, f"to {e['to']!r} is not a party of the header")
            return
        if to.get("on_behalf_of") != e["by"]:
            self.err("L-8", e, f"'{e['to']}' does not act on behalf of '{e['by']}' "
                               f"(its on_behalf_of is {to.get('on_behalf_of')!r})")
        layers = e["layers"]
        if layers is not None:
            if not all(isinstance(x, str) and x in LAYERS for x in layers) \
                    or len(set(layers)) != len(layers):
                self.err("L-1", e, f"layers {layers!r} are not distinct layers of "
                                   f"{', '.join(LAYERS)}")
            elif to.get("layers") is not None and set(layers) - set(to["layers"]):
                self.err("L-8", e, f"'{e['to']}' acts on the layers "
                                   f"{', '.join(to['layers']) or 'none'}; the delegation gives "
                                   f"{', '.join(layers)}")
        elif to.get("layers") is not None:
            pass                                # null: every layer of `to`
        procs = e["procedures"]
        if procs is not None and (not all(isinstance(x, str) and x in PROCEDURES for x in procs)
                                  or len(set(procs)) != len(procs)):
            self.err("L-1", e, f"procedures {procs!r} are not distinct procedures of "
                               f"{', '.join(PROCEDURES)}")
        until = e["until"]
        if until is not None:
            if _point_key(until) is None:
                self.err("L-1", e, f"until {until!r} is not a point")
            elif _later(e["point"], until):
                self.err("L-4", e, f"until {_point_text(until)} is before the point of the "
                                   "delegation")
        if not e["reason"].strip():
            self.err("L-8", e, f"a delegation gives a reason ('{e['by']}')")

    def delegation_for(self, d: dict) -> tuple[Optional[dict], Optional[str]]:
        """The delegation in force that covers a decision made on behalf of another party,
        or None and why the last delegation (if any) does not cover it."""
        party = self.rec.parties.get(d["by"], {})
        last = None
        for e in self.rec.entries:
            if e.get("_line", 0) >= d.get("_line", 0):
                break
            if e.get("type") == "delegation" and not e.get("_bad") \
                    and e["by"] == party.get("on_behalf_of") and e["to"] == d["by"]:
                last = e
        if last is None:
            return None, None
        if last["until"] is not None and _point_key(last["until"]) is not None \
                and _later(d["point"], last["until"]):
            return None, f"{last['id']} ended at {_point_text(last['until'])}"
        layers = last["layers"] if last["layers"] is not None else party.get("layers")
        if layers is not None:
            if not layers:
                return None, f"{last['id']} gives no layers"
            prop = self.adopted(d)
            outside = sorted({c["layer"] for c in (prop or {}).get("changes") or []
                              if c.get("layer") and c["layer"] not in layers})
            if outside:
                return None, f"{last['id']} does not give the layer {', '.join(outside)}"
        if last["procedures"] is not None and d["procedure"] not in last["procedures"]:
            return None, f"{last['id']} does not give the procedure {d['procedure']}"
        return last, None

    def t_correction(self, e):
        self.ref(e, "corrects", e["corrects"], ())

    def t_note(self, e):
        pass

    # --- helpers ------------------------------------------------------------
    def basis(self, e: dict, type_: Optional[str] = None) -> list:
        out = []
        for b in e["basis"]:
            x = self.entry(b)
            if x is not None and (type_ is None or x.get("type") == type_):
                out.append(x)
        return out

    def voted_on(self, d: dict) -> Optional[str]:
        """The proposal whose votes a decision follows or sets aside."""
        if d["choice"] != KEEP:
            return d["choice"]
        for o in d["options"]:
            if o != KEEP:
                return o
        return None

    def adopted(self, d: dict) -> Optional[dict]:
        p = self.entry(d["choice"])
        return p if p is not None and p.get("type") == "proposal" else None

    def votes_on(self, d: dict, pid: Optional[str]) -> dict:
        """The votes in the basis of d on the proposal pid: one per voter, the last one."""
        out: dict = {}
        for v in self.basis(d, "vote"):
            if v["proposal"] == pid:
                out[v["by"]] = v
        return out

    # --- L-9 to L-11 ------------------------------------------------------------
    def versions(self, base: Path, given: list) -> None:
        files = []
        for f in given:
            if f.is_file():
                files.append(f)
            else:
                self.out["not_checked"].append(f"{f}: not found")
        for e in self.good():
            if e["type"] == "version":
                for a in e["attachments"]:
                    if a["path"].endswith(".cadl"):
                        files.append(base / a["path"])
        for f in files:
            if not f.is_file():
                continue
            try:
                from .amendment import analyse, load
                sha = file_sha256(f)
                if sha not in _ANALYSES:
                    _ANALYSES[sha] = analyse(load(f))
                self.models[sha] = (f, _ANALYSES[sha])
                self.out["versions_checked"].append(str(f))
            except Exception as ex:          # a file that does not parse
                self.out["not_checked"].append(f"{f}: cannot be read ({ex})")
        for e in self.good():
            if e["type"] != "version":
                continue
            for att in e["attachments"]:
                f = base / att["path"]
                if f.is_file():
                    sha = file_sha256(f)
                    if sha != att["sha256"]:
                        self.err("L-9", e, f"the file {att['path']} has changed: its SHA-256 is "
                                           "not that of the attachment")
                    elif att["path"].endswith(".cadl") and sha != e["source_sha256"]:
                        self.err("L-9", e, f"the attachment {att['path']} is not the file of "
                                           "source_sha256")
        if not self.models:
            self.out["not_checked"].append("L-9 to L-11, and the outcome of the votes of L-6: "
                                           "no CADL file of a version is at hand")
            return
        model = self.rec.header.get("model") or {}
        sha = model.get("source_sha256") if isinstance(model, dict) else None
        if sha in self.models and model.get("version") is not None:
            a = self.models[sha][1]
            if a.version is not None and a.version != model["version"]:
                self.err("L-9", None, f"the first version's file has sos.version {a.version!r}, "
                                      f"not {model['version']!r}", line=1)
        for e in self.good():
            if e["type"] != "version":
                continue
            got = self.models.get(e["source_sha256"])
            if got is None:
                self.out["not_checked"].append(f"L-9: version {e['version']!r} "
                                               f"(entry {e['id']}): no file with its SHA-256")
                continue
            a = got[1]
            for name, mine, theirs in (("sos.version", a.version, e["version"]),
                                       ("amendment.previous", a.previous, e["previous"]),
                                       ("amendment.effective", a.effective, e["effective"])):
                if mine is not None and mine != theirs:
                    self.err("L-9", e, f"{got[0].name} has {name} {mine!r}; the entry says "
                                       f"{theirs!r}")
        self.adopted_ids = {e["choice"] for e in self.good() if e["type"] == "decision"}
        for e in self.good():
            try:
                if e["type"] == "proposal" and e["kind"] == "amendment":
                    self.amendable(e)
                elif e["type"] == "decision":
                    self.authority(e)
            except Exception as ex:          # a guard: report, never stop
                self.err("L-1", e, f"cannot be checked against the model ({ex})")

    def in_force(self, e: dict):
        """The analysis of the version in force at the point of e, if at hand."""
        model = self.rec.header.get("model")
        sha = model.get("source_sha256") if isinstance(model, dict) else None
        for v in self.good()[:self.rec.index.get(e["id"], 0)]:
            if v["type"] == "version" and _later(v["effective"], e["point"]) is False:
                sha = v["source_sha256"]
        return self.models.get(sha)

    def amendable(self, e: dict) -> None:
        got = self.in_force(e)
        if got is None or not got[1].parameters:
            self.out["not_checked"].append(
                f"L-10: proposal {e['id']}: " + ("the version in force is not at hand"
                                                 if got is None else
                                                 "the version in force declares no "
                                                 "amendable parameters (K.3)"))
            return
        from .amendment import quantity

        def bad_change(what: str) -> None:
            # decisions 0.3: an error only for a proposal that a decision adopts; a proposal
            # that no decision adopts is reported as a finding (its proposer proposed it)
            if e["id"] in self.adopted_ids:
                self.err("L-10", e, what)
            else:
                self.find("not amendable", e, what + "; no decision adopts it")
        params = {p.id: p for p in got[1].parameters}
        for c in e["changes"]:
            p = params.get(c["parameter"])
            if p is None:
                bad_change(f"{c['parameter']!r} is not an amendable parameter of "
                                    "the version in force")
                continue
            old, new = quantity(c["from"]), quantity(c["to"])
            if old is None or new is None or p.value is None or old.kind != p.value.kind \
                    or new.kind != p.value.kind:
                bad_change(f"{p.id}: from and to are "
                                    f"{p.value.kind if p.value else '?'}s")
                continue
            if old.value != p.value.value:
                bad_change(f"{p.id}: from {c['from']} is not its value "
                                    f"{p.value.text()}")
            bad = []
            if p.min is not None and new.value < p.min.value:
                bad.append(f"below min {p.min.text()}")
            if p.max is not None and new.value > p.max.value:
                bad.append(f"above max {p.max.text()}")
            if p.step is not None and p.min is not None and \
                    ((new.value - p.min.value) / p.step.value).denominator != 1:
                bad.append(f"not on the steps of {p.step.text()}")
            mc = p.max_change()
            if mc is not None and abs(new.value - p.value.value) > mc:
                bad.append(f"more than {p.max_steps} step(s)")
            if bad:
                bad_change(f"{p.id} → {c['to']}: {'; '.join(bad)}")

    def authority(self, d: dict) -> None:
        proc = d["procedure"]
        on = self.entry(self.voted_on(d)) if proc in ("vote", "override") else self.adopted(d)
        if on is None or on.get("type") != "proposal" or on.get("kind") != "amendment":
            return
        got = self.in_force(on)
        if got is None or not got[1].parameters:
            self.out["not_checked"].append(f"L-6 and L-11: decision {d['id']}: the authority of "
                                           "K.3 is not at hand")
            return
        params = {p.id: p for p in got[1].parameters}
        decider = self.rec.parties.get(d["by"], {})
        delegate_of = self.rec.parties.get(decider.get("on_behalf_of") or "", {})
        votes = self.votes_on(d, on["id"])
        adopting = d["choice"] == on["id"]
        for c in on["changes"]:
            p = params.get(c["parameter"])
            if p is None:
                continue
            a = p.authority
            if adopting and a.decided_by and proc != "override" and not (
                    _actor_matches(a.decided_by, decider.get("actor"))
                    or _actor_matches(a.decided_by, delegate_of.get("actor"))):
                self.err("L-11", d, f"{p.id} is decided by {a.text()}; '{d['by']}' is "
                                    "neither that party nor acts on its behalf")
            if a.procedure != "vote":
                if proc in ("vote", "override"):
                    self.err("L-6", d, f"{p.id} is decided by {a.text()}, not by a vote")
                continue
            counted = {by: v for by, v in votes.items() if not a.voters or any(
                _actor_matches(x, self.rec.parties.get(by, {}).get("actor")) for x in a.voters)}
            if len(counted) != len(votes):
                self.err("L-11", d, f"{p.id}: a vote in the basis is not by a voter of "
                                    f"{', '.join(a.voters)}")
            if a.quorum is None:
                self.out["not_checked"].append(f"L-6: decision {d['id']}: {p.id} has no quorum")
                continue
            yes = sum(v["value"] == "yes" for v in counted.values())
            passes = yes >= a.quorum
            follows = passes == adopting
            if proc == "vote" and not follows:
                self.err("L-6", d, f"{p.id}: {yes} yes vote(s) against the quorum {a.quorum}; "
                                   f"a decision by vote {'adopts' if passes else 'rejects'} "
                                   f"{on['id']}")
            if proc == "override" and follows:
                self.err("L-6", d, f"{p.id}: {yes} yes vote(s) against the quorum {a.quorum}: "
                                   "the decision follows the votes, so it is not an override")

    # --- findings -------------------------------------------------------------
    def findings(self) -> None:
        entries = self.good()
        fulfilled = set()
        for e in entries:
            for f in e["fulfils"]:
                fu = self.entry(f)
                if fu is None or fu.get("type") != "follow_up":
                    continue
                why = self.unmet(e, fu)
                if why:
                    self.find("fulfilment not shown", e, f"names {f} in fulfils, but {why}")
                else:
                    fulfilled.add(f)
        corrected = {e["corrects"] for e in entries if e["type"] == "correction"}
        for i, e in enumerate(entries):
            try:
                self.entry_findings(i, e, entries, fulfilled, corrected)
            except Exception as ex:          # a guard: report, never stop
                self.err("L-1", e, f"cannot be reviewed ({ex})")
        if not self.rec.complete:
            self.find("open record", {"id": None, "_line": self.out["lines"]},
                      "the record has no end record: it is still being written, or its writer "
                      "did not finish")

    def unmet(self, e: dict, fu: dict) -> Optional[str]:
        """Why e does not meet the fulfilled_by of the follow-up fu, or None."""
        fb = fu.get("fulfilled_by")
        if fb is None:
            return None
        if fb["type"] is not None and e["type"] != fb["type"]:
            return f"it is a {e['type']} entry, and {fu['id']} is fulfilled by a {fb['type']}"
        if e["type"] != "measurement":
            return None
        if fb["of"] is not None and e["of"] != fb["of"]:
            return f"it measures {e['of']}, and {fu['id']} asks for {fb['of']}"
        same = self.entry(fb["same_method_as"])
        if same is not None and same.get("type") == "measurement" \
                and e["method"] != same["method"]:
            return (f"its method ({_method_text(e['method'])}) is not that of "
                    f"{same['id']} ({_method_text(same['method'])})")
        missing = [s for s in fb["subjects"] if _subject_value(e, s) is None]
        if missing:
            return f"it gives no value for {', '.join(missing)}"
        return None

    def entry_findings(self, i, e, entries, fulfilled, corrected) -> None:
        t = e["type"]
        if e["id"] in corrected:
            self.find("corrected", e, "the entry has a correction")
        if t in ("proposal", "recommendation", "decision"):
            self.predictions(i, e, entries)
        if t == "decision":
            self.decision_findings(e)
        if t == "objective" and e["measure"] is None:
            self.reading(i, e, entries)
        if t == "follow_up" and e["id"] not in fulfilled:
            due = e["due"]
            passed = self.rec.complete or (due is not None and any(
                _later(x["point"], due) for x in entries[i + 1:]))
            if passed:
                self.find("open follow-up", e, f"\"{e['text']}\" is not fulfilled"
                          + (f" (due {_point_text(due)})" if due else ""))

    def decision_findings(self, d: dict) -> None:
        if d["procedure"] == "override":
            self.find("override", d, f"'{d['by']}' set aside the outcome of the votes on "
                                     f"{self.voted_on(d)}")
        party = self.rec.parties.get(d["by"], {})
        if party.get("on_behalf_of"):
            g, why = self.delegation_for(d)
            if g is not None:
                self.out["delegated_under"][d["id"]] = g["id"]
            else:
                self.find("delegated", d, f"'{d['by']}' ({party.get('kind')}) decided on behalf "
                                          f"of '{party['on_behalf_of']}'"
                          + (f", outside a delegation: {why}" if why else
                             ", with no delegation"))
        for r in self.basis(d, "recommendation"):
            if r["recommends"] is not None and r["recommends"] != d["choice"]:
                self.find("against recommendation", d,
                          f"chose {d['choice']}; '{r['by']}' recommended "
                          f"{r['recommends']} ({r['id']})")
        n = len(d["unknowns"]) + sum(len(r["unknowns"]) for r in self.basis(d, "recommendation"))
        if n:
            self.find("unknowns", d, f"{n} thing(s) not known by the decider or the advisor")
        prop = self.adopted(d)
        if prop is None or prop["kind"] != "amendment":
            return
        ms = [m for m in self.basis(d, "measurement") if m["of"] == prop["id"]]
        if not ms:
            self.find("unmeasured choice", d, f"{prop['id']} is adopted with no measurement "
                                              "of it in the basis")
        affected = set(prop["affected"])
        if any(c["layer"] == "contract" for c in prop["changes"] or []):
            affected |= {p for p, x in self.rec.parties.items() if "affected" in x["roles"]}
        missing = sorted(a for a in affected
                         if not any(a in (m["per_party"] or {}) for m in ms))
        if missing:
            self.find("affected not measured", d, f"no measurement of {prop['id']} in the "
                                                  f"basis has values for {', '.join(missing)}")

    def reading(self, i: int, o: dict, entries: list) -> None:
        for x in entries[i + 1:]:
            if x["type"] == "objective":
                return                       # a newer objective replaced it
            if x["type"] == "decision":
                p = self.adopted(x)
                if p is not None and p["kind"] == "measure" and p["objective"] == o["id"]:
                    return
            if x["type"] == "measurement":
                self.find("reading not decided", x, f"measured before any decision chose a "
                                                    f"reading of {o['id']}")
                return

    def predictions(self, i: int, e: dict, entries: list) -> None:
        later = entries[i + 1:]
        tally = self.out["predictions"]
        for pr in e["predictions"]:
            tally["total"] += 1
            why = self.prediction(e, pr, later)
            if why is None:
                tally["checked"] += 1
            elif why == "missed":
                tally["checked"] += 1
                tally["missed"] += 1
            else:
                tally["not_checked"][why] += 1

    def prediction(self, e: dict, pr: dict, later: list) -> Optional[str]:
        """Check one prediction against the later entries. Returns None when a later
        entry bears it out, "missed" when one contradicts it (reported), or why no
        later entry checks it: "keep", "no later entry", or "cannot compare"."""
        m = _SUBJECT_RE.fullmatch(pr["subject"])
        opt, exp = pr["option"], pr["expected"]
        if opt == KEEP:
            return "keep"
        if not m:
            return "cannot compare"
        seen, verdicts = False, []
        if m.group("voter"):
            for v in later:
                if v["type"] != "vote" or v["by"] != m.group("voter") or v["proposal"] != opt:
                    continue
                seen = True
                if exp not in ("yes", "no"):
                    continue
                verdicts.append(v["value"] == exp)
                if v["value"] != exp:
                    self.find("prediction missed", e,
                              f"expected {m.group('voter')} to vote {exp} on {opt}; "
                              f"it voted {v['value']} ({v['id']})")
            return _verdict(verdicts, seen)
        name, who = m.group("metric"), m.group("party")
        for x in later:
            if x["type"] != "measurement" or x["of"] != opt:
                continue
            seen = True
            got = _value(x, name, who)
            if got is None:
                continue
            base_m = self.entry(x["compared_with"])
            base = _value(base_m, name, who) if base_m else None
            ok = _meets(exp, got, base)
            verdicts.append(ok)
            if ok is False:
                subject = name + (f" of {who}" if who else "")
                self.find("prediction missed", e,
                          f"expected {subject} to {exp} under {opt}; measured "
                          f"{_num(got)}" + (f" against {_num(base)}" if base is not None
                                            else "") + f" ({x['id']})")
        return _verdict(verdicts, seen)


def _verdict(verdicts: list, seen: bool) -> Optional[str]:
    if False in verdicts:
        return "missed"
    if True in verdicts:
        return None
    return "cannot compare" if seen else "no later entry"


def _method_text(m: dict) -> str:
    return "; ".join(f"{k} {m[k]}" for k in ("runs", "window", "description")
                     if m.get(k) is not None) or "none"


def _subject_value(m: dict, subject: str):
    s = _SUBJECT_RE.fullmatch(subject)
    if not s or s.group("voter"):
        return None
    return _value(m, s.group("metric"), s.group("party"))


def _value(m: Optional[dict], name: str, party: Optional[str]):
    if m is None:
        return None
    src = (m.get("per_party") or {}).get(party) if party else m.get("values")
    v = (src or {}).get(name)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _meets(exp: str, got: float, base: Optional[float]) -> Optional[bool]:
    """False when the measurement contradicts the prediction; None when it cannot say.
    A rise or a fall is contradicted by no change in that direction; `unchanged`
    by a change of more than UNCHANGED_TOLERANCE of the compared value."""
    if exp in ("increase", "decrease", "unchanged"):
        if base is None:
            return None
        if exp == "unchanged":
            return abs(got - base) <= UNCHANGED_TOLERANCE * max(abs(base), 1e-12)
        return got > base if exp == "increase" else got < base
    c = _COND_RE.fullmatch(exp) if isinstance(exp, str) else None
    if not c:
        return None
    op, x = c.group(1), float(c.group(2))
    return {"<=": got <= x, ">=": got >= x, "<": got < x, ">": got > x, "==": got == x}[op]


def _num(x: float) -> str:
    return f"{x:g}"


def format_verify_text(result: dict) -> str:
    lines = [f"{result['file']}: " + ("valid" if result["valid"] else
                                     f"{len(result['errors'])} error(s)")]
    lines.append(f"  lines: {result['lines']} (header, {result['entries']} entries"
                 + (", end record)" if result["complete"] else ", no end record)"))
    lines.append(f"  parties: {result['parties']}; hash: {result['hash']}"
                 + (f" (key id {result['key_id']})" if result["key_id"] else ""))
    lines.append(f"  run id: {result['run_id']}")
    lines.append(f"  last digest: {result['last_digest']} (keep it outside the record as "
                 "an anchor)")
    for e in result["errors"]:
        where = f"line {e['line']}" if e["line"] else "record"
        lines.append(f"  error {e['rule']} {where}" + (f" ({e['entry']})" if e["entry"] else "")
                     + f": {e['what']}")
    if result["findings"]:
        lines.append(f"  findings ({len(result['findings'])}), for review:")
        for f in result["findings"]:
            lines.append(f"    {f['finding']}" + (f" [{f['entry']}]" if f["entry"] else "")
                         + f": {f['text']}")
    under = result.get("delegated_under") or {}
    if under:
        by: dict = {}
        for d, g in under.items():
            by.setdefault(g, []).append(d)
        lines.append("  made within a delegation: " + "; ".join(
            f"{g}: {', '.join(ds)}" for g, ds in by.items()))
    pr = result.get("predictions") or {}
    if pr.get("total"):
        nc = pr["not_checked"]
        rest = sum(nc.values())
        lines.append(f"  predictions: {pr['total']}; checked by a later entry: {pr['checked']} "
                     f"(missed: {pr['missed']}); not checked: {rest}"
                     + (" (" + ", ".join(f"{k}: {v}" for k, v in nc.items() if v) + ")"
                        if rest else ""))
    for n in result["not_checked"]:
        lines.append(f"  not checked: {n}")
    return "\n".join(lines)
