"""What the gateways of the agent governance extension (Appendix G, G.11,
G.13) have in common: the enforcement point behind a gateway, without
the protocol in front of it.

`cadl mcp-gateway` (:mod:`cadl.mcp_gateway`) and `cadl a2a-gateway`
(:mod:`cadl.a2a_gateway`) differ in how a message arrives and how it is
answered. They share:

- the strict reading of JSON (`parse_line`, :func:`cadl.audit_log.loads_unique`)
  and the re-serialised form that is passed on (`wire`);
- the online runtime (:class:`cadl.agents_runtime.AgentsRuntime`), the
  trace that is written before each step, and the tamper-evident audit
  log (:class:`cadl.audit_log.AuditChain`), in :class:`EnforcementPoint`;
- the holds, their deadlines, and the inbox directory for approvals,
  rejections and other messages, authenticated with HMAC-SHA256 under the
  key of the actor that speaks (``--approver-keys``), and the tools that
  write into it (`cadl mcp-approve`, `cadl mcp-message`).

A subclass of :class:`EnforcementPoint` says what happens when a hold
closes (`_released`, `_closed_denied`) and when the enforcement point
fails with calls held (`_failed_answer`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import agents as G
from .agents_runtime import AgentsRuntime
from .audit_log import (
    AuditChain,
    AuditKeyError,
    canonical,
    check_surrogates,
    epoch_text,
    file_sha256,
    is_run_id,
    key_id,
    loads_unique,
    new_run_id,
    read_key,
)
from .norms_eval import NormsEvalError, load_version

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INIT_KEYS = {"bindings", "request", "values", "quantities", "instances",
             "host_instances", "state"}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_MAC_RE = re.compile(r"[0-9a-f]{64}")
_IDENT_HOLD_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Seconds after a deadline at which the expiry is observed: the runtime
# expires a hold at a time strictly later than its deadline.
_AFTER_DEADLINE = 0.001


class GatewayError(ValueError):
    """A usage error: the gateway does not start."""


class _Refused(ValueError):
    """An inbox file that is not used, with the event to record."""

    def __init__(self, why: str, event: str | None = None, hold_id: str | None = None,
                 message: dict | None = None, reason: str | None = None):
        super().__init__(why)
        self.event, self.hold_id, self.message, self.reason = event, hold_id, message, reason


def log(prefix: str, text: str) -> None:
    try:
        sys.stderr.write(f"{prefix}: {text}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def wire(obj: Any) -> str:
    """The line that is sent: compact JSON, every non-ASCII character
    escaped, so that it has no character at which a reader splits lines."""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def parse_line(text: str) -> tuple[Any, int | None, str | None]:
    """Parse one received line strictly. Returns (object, None, None), or
    (None, error code, reason) for a line that is refused."""
    if text.endswith("\r"):
        text = text[:-1]                    # one CRLF line end
    if "\r" in text or "\n" in text:
        return None, INVALID_REQUEST, "a carriage return or line feed inside the line"
    return parse_json(text)


def parse_json(text: str) -> tuple[Any, int | None, str | None]:
    """Parse a JSON text strictly (duplicate keys, NaN, infinities, `1e999`,
    lone surrogates and deep nesting are refused)."""
    try:
        return loads_unique(text), None, None
    except json.JSONDecodeError:
        return None, PARSE_ERROR, "not JSON"
    except ValueError as e:
        return None, INVALID_REQUEST, str(e)


def message_mac(key: bytes, body: dict) -> str:
    """The HMAC-SHA256 of an inbox file: over the canonical JSON (G.8) of
    the file without its field `mac`."""
    return hmac.new(key, canonical({k: v for k, v in body.items() if k != "mac"}),
                    hashlib.sha256).hexdigest()


def denial_text(m: dict | None, rec: dict, what_kind: str = "tool call") -> str:
    """The text of a denial: the message, the reason, and what the result
    of G.10 gives the sender."""
    what = "-" if m is None else f"{m['from']} -> {m['to']} : {m['name']}"
    parts = [f"reason: {rec.get('reason')}"]
    if rec.get("hold_id"):
        parts.append(f"hold {rec['hold_id']}"
                     + (f" ({rec['outcome']})" if rec.get("outcome") else ""))
    if rec.get("norms"):
        parts.append("norms: " + ", ".join(rec["norms"]))
    if rec.get("contracts"):
        parts.append("contracts: " + ", ".join(rec["contracts"]))
    if rec.get("fallback"):
        parts.append("fallback: " + "; ".join(rec["fallback"]))
    if rec.get("notify"):
        parts.append("notify: " + ", ".join(rec["notify"]))
    return (f"Denied by the CADL enforcement point (agents 0.1): the {what_kind} "
            f"{what} is not allowed ({'; '.join(parts)}).")


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _id_key(value: Any) -> str:
    return json.dumps(value)


def read_keyring(path, option: str = "--approver-keys") -> dict[str, bytes]:
    """A keyring: a JSON object from actor to the path of a key file
    (relative to the keyring) or to {"hex": "..."}; keys of at least 16
    bytes."""
    path = Path(path)
    try:
        ring = loads_unique(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise GatewayError(f"cannot read {option} {path}: {e}") from None
    if not isinstance(ring, dict) or not ring:
        raise GatewayError(f"{option} is a JSON object from actor to key file")
    out = {}
    for actor, entry in ring.items():
        who = G.parse_concrete_actor(actor)
        if who is None:
            raise GatewayError(f"{option}: {actor!r} is not a concrete actor")
        if isinstance(entry, str):
            try:
                key = read_key(path.parent / entry)
            except AuditKeyError as e:
                raise GatewayError(f"{option}: {actor}: {e}") from None
        elif isinstance(entry, dict) and set(entry) == {"hex"} and isinstance(entry["hex"], str):
            try:
                key = bytes.fromhex(entry["hex"])
            except ValueError:
                raise GatewayError(f"{option}: {actor}: not hexadecimal") from None
            if len(key) < 16:
                raise GatewayError(f"{option}: {actor}: a key has at least 16 bytes")
        else:
            raise GatewayError(f"{option}: {actor}: give a key file or {{\"hex\": ...}}")
        out[who["text"]] = key
    return out


def _secure_dir(path: Path, create: bool) -> None:
    """A directory of this user that nobody else can write; created with
    mode 0700 when it does not exist."""
    if not path.exists() and not path.is_symlink():
        if not create:
            raise GatewayError(f"{path} does not exist")
        path.mkdir(mode=0o700, parents=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise GatewayError(f"the inbox {path} is not a directory (or is a symbolic link)")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise GatewayError(f"the inbox {path} is not owned by the user of the gateway")
    if st.st_mode & 0o022:
        raise GatewayError(f"the inbox {path} is writable by its group or by others; "
                           f"make it 0700 (chmod go-w)")


@dataclass
class _Held:
    id: Any
    request: dict
    message: dict
    wire: str = ""                  # the line that is sent when it is released
    params_sha256: str = ""         # SHA-256 of the canonical JSON of `params`


class EnforcementPoint:
    """The online runtime behind a gateway, with its trace, its audit log,
    its holds and its inbox. A subclass calls `_load` and then `_open`
    from its constructor and implements `_released`, `_closed_denied` and
    `_failed_answer`."""

    LOG_PREFIX = "cadl gateway"

    def _log(self, text: str) -> None:
        log(self.LOG_PREFIX, text)

    # --- setup ---------------------------------------------------------------

    def _load(self, file: Path) -> None:
        """Read the file: the parsed file, the policy and the approval rules."""
        self.file = Path(file)
        self.lock = threading.RLock()
        self.failed: str | None = None
        self.stopped = False
        self.held: dict[str, _Held] = {}
        self.nonces: set = set()
        try:
            self.sos = load_version(self.file)
        except NormsEvalError as e:
            raise GatewayError(str(e)) from None
        if not G.agents_active(self.sos):
            raise GatewayError("the file does not declare 'norms: 0.1' and 'agents: 0.1'")
        self.policy = G.build_policy(self.sos)
        self.approval_rules = self._approval_rules()

    def _open(self, audit: Path, trace_out: Path, key: bytes | None, inbox: Path | None,
              init: dict | None, clock: Callable[[], float], wall: datetime | None,
              approver_keys: dict[str, bytes] | None, insecure_approvals: bool,
              run_id: str | None, more_first: dict | None = None) -> None:
        """Check the options, create the logs and the inbox, and decide the
        first line of the trace."""
        self.inbox = None if inbox is None else Path(inbox)
        self.clock = clock
        self.approver_keys = approver_keys
        self.insecure = insecure_approvals
        if init is not None:
            if not isinstance(init, dict) or set(init) - INIT_KEYS:
                raise GatewayError("--init is a JSON object with the keys "
                                   + ", ".join(sorted(INIT_KEYS)))
        if self.inbox is not None and approver_keys is None and not insecure_approvals:
            raise GatewayError("--approvals needs --approver-keys (the keys of the "
                               "approvers); --insecure-approvals reads the inbox without "
                               "authentication and is meant for tests only")
        if approver_keys is not None and key is not None \
                and any(hmac.compare_digest(k, key) for k in approver_keys.values()):
            raise GatewayError("an approver key is the key of the audit log; use other keys")
        for path, what in ((audit, "audit log"), (trace_out, "trace")):
            if Path(path).exists():
                raise GatewayError(f"the {what} {path} exists; the gateway does not "
                                   f"overwrite or extend a log")
        if self.inbox is not None:
            _secure_dir(self.inbox, create=True)
            for sub in ("done", "invalid", "holds"):
                _secure_dir(self.inbox / sub, create=True)

        self.run_id = run_id or new_run_id()
        if not is_run_id(self.run_id):
            raise GatewayError("a run id is 32 lowercase hexadecimal digits")
        wall = wall or datetime.now(timezone.utc)
        wall = wall.replace(microsecond=wall.microsecond // 1000 * 1000)
        self.epoch = wall.astimezone(timezone.utc)
        self.t0 = clock()
        self.last_t = 0.0
        self.runtime = AgentsRuntime.open(self.sos, path=str(trace_out))
        self.approvals_mode = ("none" if self.inbox is None else
                               "authenticated" if approver_keys is not None else "insecure")
        self.chain = AuditChain(file_sha256(self.file), epoch_text(self.epoch), key,
                                self.run_id, self.approvals_mode)
        self.audit_f = open(audit, "x", encoding="utf-8")
        self.trace_f = open(trace_out, "x", encoding="utf-8")
        self._write(self.audit_f, self.chain.header())
        # Three digits of the fraction always: Python 3.9 reads no other
        # length with `datetime.fromisoformat`.
        stamp = (self.epoch.strftime("%Y-%m-%dT%H:%M:%S")
                 + f".{self.epoch.microsecond // 1000:03d}Z")
        first = {"t": 0, "run_id": self.run_id, "approvals": self.approvals_mode,
                 "epoch": stamp, "quantities": []}
        first.update(init or {})
        if self.inbox is not None:          # before anything can be held
            self._publish("run.json", {"run_id": self.run_id,
                                       "approver_keys": approver_keys is not None,
                                       **(more_first or {})})
        try:
            self._step(first)
        except Exception as e:      # noqa: BLE001
            raise GatewayError(f"the first line of the trace is not accepted: {e}") from None

    def _actor(self, text: str, option: str) -> dict:
        actor = G.parse_concrete_actor(text)
        if actor is None:
            raise GatewayError(f"{option} {text!r} is not a concrete actor such as "
                               f"PLANNER or ROBOT[2]")
        return actor

    def _approval_rules(self) -> list[tuple[str, dict, str, str]]:
        """(event, sender reference, receiver, message name) of the
        counts_as rules of the enforced contracts that create `approval`
        or `rejection` from a message with a concrete receiver."""
        out = []
        for c in self.sos.contracts:
            if c.enforcement is None:
                continue
            for r in c.counts_as:
                if r.creates not in G.HOLD_EVENTS:
                    continue
                ev = G.parse_message_event(r.when)
                if ev is None or ev.name is None:
                    continue
                recv = ev.receiver
                if recv["kind"] == "plain":
                    to = recv["name"]
                elif recv["kind"] == "index":
                    to = f"{recv['name']}[{recv['index']}]"
                else:
                    continue
                out.append((r.creates, ev.sender, to, ev.name))
        return out

    def _approval_names(self) -> set[str]:
        """The names of the messages from which a counts_as rule of an
        enforced contract creates `approval` or `rejection`, whatever its
        receiver: such a message comes only as an approval file."""
        out = set()
        for c in self.sos.contracts:
            if c.enforcement is None:
                continue
            for r in c.counts_as:
                if r.creates in G.HOLD_EVENTS:
                    ev = G.parse_message_event(r.when)
                    if ev is not None and ev.name is not None:
                        out.add(ev.name)
        return out

    # --- time, trace and audit ------------------------------------------------

    def now(self) -> float:
        t = round(self.clock() - self.t0, 3)
        self.last_t = max(self.last_t, t)
        return self.last_t

    @staticmethod
    def _write(f, line: str) -> None:
        f.write(line + "\n")
        f.flush()

    def _step(self, line: dict, close: bool = False) -> dict:
        """Write one line to the trace, then let the runtime decide it, then
        write the records it produced. Any failure fails the gateway."""
        if self.failed is not None:
            raise NormsEvalError(f"the enforcement point has failed: {self.failed}")
        try:
            self._write(self.trace_f, json.dumps(line, ensure_ascii=False, allow_nan=False))
            if close:
                out = self.runtime.close(line["t"])
            else:
                out = self.runtime.observe(line)
            for rec in out["audit"]:
                self._write(self.audit_f, self.chain.record(rec))
            if close:
                self._write(self.audit_f, self.chain.end())
            return out
        except Exception as e:      # noqa: BLE001
            self.failed = f"{type(e).__name__}: {e}"
            self._log(f"the enforcement point failed; every later tool call is denied: "
                      f"{self.failed}")
            raise

    def _host_event(self, event: str, reason: str | None = None,
                    hold_id: str | None = None, message: dict | None = None) -> None:
        """Record an event of the gateway in the trace and the log."""
        entry: dict[str, Any] = {"event": event, "reason": reason}
        if hold_id is not None:
            entry["hold_id"] = hold_id
        if message is not None:
            entry["message"] = {k: message[k] for k in ("from", "to", "name")}
        try:
            self._settle(self._step({"t": self.now(), "host_events": [entry]}))
        except Exception:       # noqa: BLE001
            self._fail_held()

    def _held_id(self, id_: Any) -> bool:
        return any(h.id == id_ and type(h.id) is type(id_) for h in self.held.values())

    def _settle(self, out: dict) -> None:
        """Answer or release the held calls whose hold this step closed."""
        for rec in out["audit"]:
            if "decision" not in rec or rec.get("outcome") is None:
                continue
            self._unpublish_hold(rec["hold_id"])
            h = self.held.pop(rec["hold_id"], None)
            if h is None:
                continue
            if rec["decision"] == "allow":
                self._released(rec["hold_id"], h, rec)
            else:
                self._closed_denied(rec["hold_id"], h, rec)

    def _released(self, hold_id: str, h: _Held, rec: dict) -> None:
        raise NotImplementedError

    def _closed_denied(self, hold_id: str, h: _Held, rec: dict) -> None:
        raise NotImplementedError

    def _failed_answer(self, hold_id: str, h: _Held) -> None:
        raise NotImplementedError

    # --- time and inbox --------------------------------------------------------------

    def tick(self) -> None:
        """Observe the deadlines that have passed, then the inbox."""
        with self.lock:
            if self.failed is not None or self.stopped:
                return
            t = self.now()
            due = [h for h in self.runtime.held() if h["deadline_t"] is not None
                   and t >= h["deadline_t"] + _AFTER_DEADLINE]
            if due:
                try:
                    self._settle(self._step({"t": t}))
                except Exception:       # noqa: BLE001
                    self._fail_held()
                    return
        if self.inbox is not None:
            self._read_inbox()

    def _publish(self, name: str, obj: dict) -> None:
        target = self.inbox / name
        tmp = target.parent / f".{target.name}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        os.replace(tmp, target)

    def _publish_hold(self, hold_id: str) -> None:
        """What an approver signs: the run, the hold, the held message and
        the time of the hold."""
        if self.inbox is None:
            return
        entry = next((h for h in self.runtime.held() if h["hold_id"] == hold_id), None)
        held = self.held.get(hold_id)
        if entry is None or held is None:
            return
        try:
            self._publish(f"holds/{hold_id}.json", {
                "run_id": self.run_id, "hold_id": hold_id, "message": entry["message"],
                "hold_time": entry["time"], "approver": entry["approver"],
                "deadline": entry["deadline"], "params": held.request.get("params"),
                "params_sha256": held.params_sha256})
        except OSError as e:
            self._log(f"cannot publish hold {hold_id}: {e}")

    def _unpublish_hold(self, hold_id: str) -> None:
        if self.inbox is None or not _IDENT_HOLD_RE.fullmatch(hold_id or ""):
            return
        try:
            (self.inbox / "holds" / f"{hold_id}.json").unlink()
        except OSError:
            pass

    def _read_inbox(self) -> None:
        try:
            entries = sorted(e.name for e in os.scandir(self.inbox))
        except OSError as e:
            self._log(f"cannot read the inbox: {e}")
            return
        for name in entries:
            if name.startswith(".") or not name.endswith(".json") or name == "run.json":
                continue
            path = self.inbox / name
            with self.lock:
                if self.failed is not None or self.stopped:
                    return
                try:
                    m, events = self._inbox_message(self._read_inbox_file(path))
                except _Refused as e:
                    self._log(f"inbox file {name} changes nothing: {e}")
                    if e.event is None:         # not a readable approval or message
                        self._move(path, "invalid", str(e))
                    else:                       # read, and refused: recorded
                        self._move(path, "done")
                        self._host_event(e.event, reason=e.reason, hold_id=e.hold_id,
                                         message=e.message)
                    continue
                self._move(path, "done")
                try:
                    self._settle(self._step({"t": self.now(), "host_events": events,
                                             "messages": [m]}))
                except Exception:       # noqa: BLE001
                    self._fail_held()
                    return

    @staticmethod
    def _read_inbox_file(path: Path) -> bytes:
        """The bytes of an inbox file: a regular file with one link, owned by
        the user of the gateway, opened without following a link."""
        try:
            st = os.lstat(path)
        except OSError as e:
            raise _Refused(f"cannot be read ({e})") from None
        if not stat.S_ISREG(st.st_mode):
            raise _Refused("not a regular file (a symbolic link, a directory or another "
                           "kind of file)")
        if st.st_nlink != 1:
            raise _Refused("the file has more than one link")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise _Refused("not owned by the user of the gateway")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as e:
            raise _Refused(f"cannot be read ({e})") from None
        with os.fdopen(fd, "rb") as f:
            st2 = os.fstat(f.fileno())
            if (st2.st_ino, st2.st_dev) != (st.st_ino, st.st_dev) or st2.st_nlink != 1:
                raise _Refused("the file changed while it was read")
            data = f.read(65536 + 1)
        if len(data) > 65536:
            raise _Refused("larger than 64 KiB")
        return data

    def _move(self, path: Path, where: str, why: str | None = None) -> None:
        target = self.inbox / where / path.name
        try:
            os.replace(path, target)
            if why is not None:
                target.with_suffix(".why").write_text(why + "\n", encoding="utf-8")
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass

    def _inbox_message(self, data: bytes) -> tuple[dict, list]:
        """The message of an inbox file and the host events that go with it.
        Raises _Refused for a file that changes nothing; its `event` is the
        event record to write, if any."""
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        try:
            obj = loads_unique(data.decode("utf-8"))
            check_surrogates(obj)
        except (ValueError, UnicodeDecodeError) as e:
            raise _Refused(f"cannot be read as JSON ({e})") from None
        if not isinstance(obj, dict):
            raise _Refused("not a JSON object")
        kind = obj.get("kind")
        if kind == "approval":
            m, hold_id = self._approval(obj)
            head = {k: m[k] for k in ("from", "to", "name")}
            params = obj.get("params_sha256")
            if self.approver_keys is None:
                reason = f"file={digest};params=" + (f"sha256:{params}" if params else "-")
                return m, [{"event": "approval_unauthenticated", "hold_id": hold_id,
                            "reason": reason, "message": head}]
            reason = (f"file={digest};params=sha256:{params};"
                      f"key_id={key_id(self.approver_keys[m['from']])};nonce={obj['nonce']}")
            return m, [{"event": "approval_evidence", "hold_id": hold_id,
                        "reason": reason, "message": head}]
        if kind == "message":
            return self._other_message(obj), []
        raise _Refused("'kind' is neither approval nor message")

    def _check_mac(self, obj: dict, actor: str, event: str, hold_id: str | None,
                   m: dict | None) -> None:
        """Authenticate a file of `actor`; raises _Refused with the event."""
        def refuse(reason, why):
            raise _Refused(why, event=event, hold_id=hold_id, message=m, reason=reason)
        if self.approver_keys is None:          # --insecure-approvals
            if obj.get("run_id") not in (None, self.run_id):
                refuse("wrong_run", "the file is for another run")
            return
        key = self.approver_keys.get(actor)
        mac = obj.get("mac")
        if key is None:
            refuse("unauthenticated", f"no key for {actor}")
        if not isinstance(mac, str) or not _MAC_RE.fullmatch(mac) \
                or not hmac.compare_digest(mac, message_mac(key, obj)):
            refuse("unauthenticated", "the MAC is missing or does not verify")
        if obj.get("run_id") != self.run_id:
            refuse("wrong_run", "the file is for another run")
        nonce = obj.get("nonce")
        if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
            refuse("unauthenticated", "no nonce")
        if nonce in self.nonces:
            refuse("replayed", "the nonce was used before")
        self.nonces.add(nonce)

    def _approval(self, obj: dict) -> tuple[dict, str]:
        allowed = {"kind", "hold_id", "decision", "as", "run_id", "message", "hold_time",
                   "params_sha256", "nonce", "mac"}
        if set(obj) - allowed:
            raise _Refused("unknown keys: " + ", ".join(sorted(set(obj) - allowed)))
        hold_id, decision, actor = obj.get("hold_id"), obj.get("decision"), obj.get("as")
        if not isinstance(hold_id, str) or not _IDENT_HOLD_RE.fullmatch(hold_id):
            raise _Refused("no valid hold_id")
        if decision not in ("approve", "reject"):
            raise _Refused("decision is neither approve nor reject")
        who = G.parse_concrete_actor(actor)
        if who is None:
            raise _Refused(f"'as' is not a concrete actor: {actor!r}")
        event = "approval" if decision == "approve" else "rejection"
        m = None
        for ev, sender, to, name in self.approval_rules:
            if ev == event and G.ref_match(self.policy, sender, who, {}) is True:
                m = {"from": who["text"], "to": to, "name": name, "args": [hold_id]}
                break
        if m is None:
            raise _Refused(f"no counts_as rule of an enforced contract creates {event} "
                           f"from a message of {who['text']} to a concrete receiver")
        record = f"unauthorised_{event}"
        self._check_mac(obj, who["text"], record, hold_id, m)
        if self.approver_keys is not None and self.runtime.hold_state(hold_id) == "held":
            entry = next(h for h in self.runtime.held() if h["hold_id"] == hold_id)
            held = self.held.get(hold_id)
            if obj.get("message") != entry["message"] or obj.get("hold_time") != entry["time"] \
                    or held is None or obj.get("params_sha256") != held.params_sha256:
                raise _Refused("the file names another held message, time or arguments",
                               event=record, hold_id=hold_id, message=m,
                               reason="wrong_message")
        return m, hold_id

    def _other_message(self, obj: dict) -> dict:
        allowed = {"kind", "message", "run_id", "nonce", "mac"}
        if set(obj) - allowed:
            raise _Refused("unknown keys: " + ", ".join(sorted(set(obj) - allowed)))
        m = obj.get("message")
        if not isinstance(m, dict) or set(m) - {"from", "to", "name", "args"}:
            raise _Refused("'message' is an object with from, to, name and args")
        for k in ("from", "to"):
            if G.parse_concrete_actor(m.get(k)) is None:
                raise _Refused(f"'{k}' is not a concrete actor")
        if not isinstance(m.get("name"), str) or not _IDENT_RE.fullmatch(m["name"]):
            raise _Refused("'name' is not an identifier")
        args = m.get("args", [])
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise _Refused("'args' is a list of strings")
        out = {"from": m["from"], "to": m["to"], "name": m["name"], "args": list(args)}
        self._check_mac(obj, m["from"], "unauthorised_message", None, out)
        if self.approver_keys is not None and m["name"] in self._approval_names():
            raise _Refused("an approval or rejection comes through mcp-approve",
                           event="unauthorised_message", message=out,
                           reason="approval_as_message")
        return out

    def _fail_held(self) -> None:
        """After a failure, the held calls are denied (fail-closed). While
        the enforcement point can still record, the holds are withdrawn
        first, so that the log and the runtime agree with what the client
        is told; after a failure of the enforcement point itself, the log
        has no end record."""
        held = list(self.held.items())
        self.held.clear()
        if held and self.failed is None:
            try:
                self._step({"t": self.now(), "withdraw": [h for h, _ in held]})
            except Exception:       # noqa: BLE001
                pass
        for hold_id, h in held:
            self._unpublish_hold(hold_id)
            self._failed_answer(hold_id, h)

    # --- end -------------------------------------------------------------------------------

    def _close_logs(self) -> None:
        """The last line of the trace, the end of the instances, the end
        record of the audit log; then the files are closed."""
        if self.failed is None:
            try:
                self._step({"t": self.now()}, close=True)
            except Exception:       # noqa: BLE001
                pass
        for f in (self.trace_f, self.audit_f):
            try:
                f.close()
            except OSError:
                pass


# === The inbox tools (mcp-approve, mcp-message) ================================

def _drop(inbox: Path, obj: dict) -> Path:
    """Write a file into the inbox atomically, readable by its owner only."""
    inbox = Path(inbox)
    if not inbox.is_dir():
        raise GatewayError(f"the inbox {inbox} is not a directory")
    name = f"{time.time_ns():020d}-{os.getpid()}-{secrets.token_hex(4)}.json"
    tmp = inbox / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    target = inbox / name
    os.replace(tmp, target)
    return target


def _read_published(path: Path) -> dict:
    try:
        obj = loads_unique(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise GatewayError(f"cannot read {path}: {e}") from None
    if not isinstance(obj, dict):
        raise GatewayError(f"{path} is not a JSON object")
    return obj


def approval_file(inbox: Path, hold_id: str, decision: str, actor: str,
                  key: bytes | None) -> dict:
    """The content of an approval file: bound to the run, the hold, the
    held message and its time (from DIR/holds/HOLD_ID.json, which the
    gateway publishes), with a fresh nonce and, with a key, the MAC."""
    inbox = Path(inbox)
    if not _IDENT_HOLD_RE.fullmatch(hold_id):
        raise GatewayError(f"{hold_id!r} is not a hold id")
    obj: dict[str, Any] = {"kind": "approval", "hold_id": hold_id, "decision": decision,
                           "as": actor}
    published = inbox / "holds" / f"{hold_id}.json"
    if published.exists():
        hold = _read_published(published)
        obj.update(run_id=hold.get("run_id"), message=hold.get("message"),
                   hold_time=hold.get("hold_time"), params_sha256=hold.get("params_sha256"))
    elif key is not None:
        raise GatewayError(f"the gateway has published no held call {hold_id} "
                           f"({published} does not exist)")
    obj["nonce"] = secrets.token_hex(16)
    if key is not None:
        obj["mac"] = message_mac(key, obj)
    return obj


def cli_approve(args) -> int:
    if G.parse_concrete_actor(args.actor) is None:
        print(f"Error: --as {args.actor!r} is not a concrete actor", file=sys.stderr)
        return 2
    try:
        published = Path(args.inbox) / "holds" / f"{args.hold_id}.json"
        hold = _read_published(published) if published.exists() else None
        if hold is not None:
            m = hold.get("message") or {}
            print(f"hold {args.hold_id} of run {hold.get('run_id')}: "
                  f"{m.get('from')} -> {m.get('to')} : {m.get('name')} at "
                  f"{hold.get('hold_time')}, deadline {hold.get('deadline')}")
            print("arguments (params, SHA-256 " + str(hold.get("params_sha256")) + "):")
            print(json.dumps(hold.get("params"), ensure_ascii=False, indent=2))
        elif args.show:
            raise GatewayError(f"the gateway has published no held call {args.hold_id}")
        if args.show:
            return 0
        key = read_key(args.key) if args.key is not None else None
        obj = approval_file(args.inbox, args.hold_id, args.decision, args.actor, key)
        path = _drop(args.inbox, obj)
    except (GatewayError, AuditKeyError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(f"{path}: {args.decision} {args.hold_id}" + ("" if key else " WITHOUT a MAC"))
    return 0


def cli_message(args) -> int:
    ev = G.parse_message_event(args.message)
    sender = ev and G.parse_concrete_actor(re.sub(r"\s+", "", ev.sender["text"]))
    receiver = ev and G.parse_concrete_actor(re.sub(r"\s+", "", ev.receiver["text"]))
    if ev is None or ev.name is None or ev.has_args or not sender or not receiver:
        print(f"Error: {args.message!r} is not a message 'FROM -> TO : name' between "
              f"concrete actors (give arguments with --arg)", file=sys.stderr)
        return 2
    try:
        key = read_key(args.key) if args.key is not None else None
        obj: dict[str, Any] = {"kind": "message", "message": {
            "from": sender["text"], "to": receiver["text"], "name": ev.name,
            "args": list(args.arg)}}
        run = Path(args.inbox) / "run.json"
        if run.exists():
            obj["run_id"] = _read_published(run).get("run_id")
        elif key is not None:
            raise GatewayError(f"{run} does not exist: is the gateway running?")
        obj["nonce"] = secrets.token_hex(16)
        if key is not None:
            obj["mac"] = message_mac(key, obj)
        path = _drop(args.inbox, obj)
    except (GatewayError, AuditKeyError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(path)
    return 0
