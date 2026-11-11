"""An MCP gateway for the agent governance extension (Appendix G, G.11).

`cadl mcp-gateway` is a stdio proxy between an MCP client, the agent
``ACTOR`` of a CADL file, and an MCP server, the tool server
``TOOLSERVER`` of the file, which the gateway starts as a subprocess. It
speaks MCP's JSON-RPC 2.0 over the stdio transport: one JSON-RPC message
per line, UTF-8, no embedded newline (MCP specification 2026-07-28,
"Transports: stdio",
https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio).

Every line is parsed strictly before anything is done with it: a line
with a carriage return other than one trailing CRLF, invalid UTF-8,
duplicate keys at any depth, NaN or infinities, or lone surrogates is
refused. Nothing is forwarded as received: a forwarded message is the
compact JSON of the parsed object, with every non-ASCII character
escaped, so that no reader can split it into other lines.

Methods are allowed by a list (``ALLOWED_METHODS`` and ``--allow-method``),
in both directions; everything else is refused and recorded as the event
``method_refused``. Only ``tools/call`` is decided by the norms: it is
the message event ``ACTOR -> TOOLSERVER : name`` at the time of its
arrival, decided by the online runtime
(:class:`cadl.agents_runtime.AgentsRuntime`):

- ``allow``: the call is forwarded; the response of the server comes back.
- ``deny``: the gateway answers with a tool result with ``isError: true``
  and a text that names the reason, the norms, the fallback and the
  actors to notify (MCP 2026-07-28, "Tools: Error Handling").
- ``escalate``: the call is held; other requests go on. When the hold
  closes (G.6), an approved and released call is forwarded; a rejected,
  expired or cancelled one, or a release that is denied, is answered with
  a denial. A client that cancels a held call withdraws it: the trace
  says ``withdraw`` and the hold closes as ``withdrawn``.

A response is passed on only for a request in flight in the other
direction; any other response is dropped and recorded as
``unsolicited_response``. ``tools/list`` results are filtered to the
tools that the file declares (``--no-filter-tools`` keeps them all).

Approvals, rejections and other messages come through the inbox
directory (``--approvals DIR``), which must be a directory of the user of
the gateway that nobody else can write. Only regular files with one link,
owned by that user, are read. With ``--approver-keys FILE`` every file
carries an HMAC-SHA256, with the key of the actor that speaks, over its
canonical JSON, which binds it to the run (``run_id``), to the hold and
the held message, and to a nonce that is used once; a file that does not
verify is recorded as ``unauthorised_approval`` (or ``_rejection``,
``unauthorised_message``) and changes nothing. Without keys the gateway
refuses an inbox unless ``--insecure-approvals`` is given (for tests).

Time: the trace time ``t`` is the time since the start of the gateway on
a monotonic clock, in seconds, rounded to milliseconds; the first line
of the trace gives the wall-clock time of ``t = 0`` as ``epoch`` and the
``run_id``. Every step of the gateway is written to the trace
(``--trace-out``) before it is decided. `cadl agents-run` on that trace
makes the same decisions and writes the same audit log, byte for byte.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import stat
import subprocess
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
    loads_unique,
    new_run_id,
    read_key,
)
from .norms_eval import NormsEvalError, TraceError, load_version

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Methods that pass without `--allow-method`: the lifecycle and discovery
# of MCP 2026-07-28 (`server/discover`) and of the earlier revisions
# (`initialize`, `notifications/initialized`, `ping`), cancellation and
# progress, and the tools. Only `tools/call` is decided by the norms.
ALLOWED_METHODS = ("initialize", "notifications/initialized", "ping", "server/discover",
                   "notifications/cancelled", "notifications/progress",
                   "tools/list", "tools/call")
# Notifications that a server may send to the client.
ALLOWED_SERVER_NOTIFICATIONS = ("notifications/progress", "notifications/cancelled",
                                "notifications/message", "notifications/tools/list_changed")

INIT_KEYS = {"bindings", "request", "values", "quantities", "instances",
             "host_instances", "state"}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_MAC_RE = re.compile(r"[0-9a-f]{64}")
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


def _log(text: str) -> None:
    try:
        sys.stderr.write(f"cadl mcp-gateway: {text}\n")
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


def denial_text(m: dict | None, rec: dict) -> str:
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
    return (f"Denied by the CADL enforcement point (agents 0.1): the tool call "
            f"{what} is not allowed ({'; '.join(parts)}).")


def denial_result(id_: Any, m: dict | None, rec: dict) -> dict:
    """A tool result with isError: true (MCP, tool execution error)."""
    return {"jsonrpc": "2.0", "id": id_, "result": {
        "resultType": "complete",
        "content": [{"type": "text", "text": denial_text(m, rec)}],
        "isError": True}}


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _id_key(value: Any) -> str:
    return json.dumps(value)


def read_keyring(path) -> dict[str, bytes]:
    """`--approver-keys`: a JSON object from actor to the path of a key file
    (relative to the keyring) or to {"hex": "..."}; keys of at least 16
    bytes."""
    path = Path(path)
    try:
        ring = loads_unique(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise GatewayError(f"cannot read --approver-keys {path}: {e}") from None
    if not isinstance(ring, dict) or not ring:
        raise GatewayError("--approver-keys is a JSON object from actor to key file")
    out = {}
    for actor, entry in ring.items():
        who = G.parse_concrete_actor(actor)
        if who is None:
            raise GatewayError(f"--approver-keys: {actor!r} is not a concrete actor")
        if isinstance(entry, str):
            try:
                key = read_key(path.parent / entry)
            except AuditKeyError as e:
                raise GatewayError(f"--approver-keys: {actor}: {e}") from None
        elif isinstance(entry, dict) and set(entry) == {"hex"} and isinstance(entry["hex"], str):
            try:
                key = bytes.fromhex(entry["hex"])
            except ValueError:
                raise GatewayError(f"--approver-keys: {actor}: not hexadecimal") from None
            if len(key) < 16:
                raise GatewayError(f"--approver-keys: {actor}: a key has at least 16 bytes")
        else:
            raise GatewayError(f"--approver-keys: {actor}: give a key file or {{\"hex\": ...}}")
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


class Gateway:
    """The decisions of the gateway, without its input and output: the
    lines of the client and of the server come in through
    `client_line` and `server_line`, the time through `tick`, and the
    lines to send go out through `to_client` and `to_server`."""

    def __init__(self, file: Path, agent: str, server_actor: str, audit: Path,
                 trace_out: Path, to_client: Callable[[str], None],
                 to_server: Callable[[str], None], key: bytes | None = None,
                 inbox: Path | None = None, init: dict | None = None,
                 filter_tools: bool = True,
                 clock: Callable[[], float] = time.monotonic,
                 wall: datetime | None = None,
                 approver_keys: dict[str, bytes] | None = None,
                 insecure_approvals: bool = False,
                 allow_methods: tuple = (),
                 run_id: str | None = None) -> None:
        self.file = Path(file)
        self.to_client = to_client
        self.to_server = to_server
        self.inbox = None if inbox is None else Path(inbox)
        self.filter_tools = filter_tools
        self.clock = clock
        self.lock = threading.RLock()
        self.failed: str | None = None
        self.stopped = False
        self.held: dict[str, _Held] = {}
        self.inflight: set = set()           # ids of requests sent to the server
        self.server_requests: set = set()    # ids of server requests sent to the client
        self.list_ids: set = set()           # ids of tools/list requests
        self.nonces: set = set()
        self.allowed = set(ALLOWED_METHODS) | set(allow_methods)
        self.allowed_server = set(ALLOWED_SERVER_NOTIFICATIONS) | set(allow_methods)
        self.approver_keys = approver_keys
        self.insecure = insecure_approvals

        try:
            self.sos = load_version(self.file)
        except NormsEvalError as e:
            raise GatewayError(str(e)) from None
        if not G.agents_active(self.sos):
            raise GatewayError("the file does not declare 'norms: 0.1' and 'agents: 0.1'")
        self.policy = G.build_policy(self.sos)
        self.agent = self._actor(agent, "--agent")
        self.server = self._actor(server_actor, "--server-actor")
        decl = next((a for a in self.sos.actors if a.id.name == self.server["name"]), None)
        if decl is None or decl.agent is None or decl.agent.kind != "tool_server" \
                or not G.is_member(self.policy, self.server["name"], self.server):
            raise GatewayError(f"--server-actor {server_actor} is not a tool server of the "
                               f"file (agent: {{ kind: tool_server }})")
        if (decl.agent.protocol or "mcp") != "mcp":
            raise GatewayError(f"{server_actor} speaks {decl.agent.protocol}, not mcp")
        self.tools = list(decl.agent.tools)
        self.approval_rules = self._approval_rules()
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
        self.chain = AuditChain(file_sha256(self.file), epoch_text(self.epoch), key,
                                self.run_id)
        self.audit_f = open(audit, "x", encoding="utf-8")
        self.trace_f = open(trace_out, "x", encoding="utf-8")
        self._write(self.audit_f, self.chain.header())
        # Three digits of the fraction always: Python 3.9 reads no other
        # length with `datetime.fromisoformat`.
        stamp = (self.epoch.strftime("%Y-%m-%dT%H:%M:%S")
                 + f".{self.epoch.microsecond // 1000:03d}Z")
        first = {"t": 0, "run_id": self.run_id, "epoch": stamp, "quantities": []}
        first.update(init or {})
        if self.inbox is not None:          # before anything can be held
            self._publish("run.json", {"run_id": self.run_id,
                                       "approver_keys": approver_keys is not None})
        try:
            self._step(first)
        except Exception as e:      # noqa: BLE001
            raise GatewayError(f"the first line of the trace is not accepted: {e}") from None

    # --- setup ---------------------------------------------------------------

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
            _log(f"the enforcement point failed; every later tool call is denied: "
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

    # --- client -> server -------------------------------------------------------

    def client_line(self, text: str) -> None:
        if not text.strip():
            return
        obj, code, why = parse_line(text)
        if code is not None:
            self.send_client(_error(None, code, ("Parse error" if code == PARSE_ERROR
                                                 else "Invalid Request") + f": {why}"))
            return
        if isinstance(obj, list):
            self.send_client(_error(None, INVALID_REQUEST,
                                    "Invalid Request: JSON-RPC batches are not supported "
                                    "by MCP"))
            return
        if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
            self.send_client(_error(obj.get("id") if isinstance(obj, dict)
                                    and _valid_id(obj.get("id")) else None,
                                    INVALID_REQUEST, "Invalid Request"))
            return
        id_ = obj.get("id")
        if "id" in obj and not _valid_id(id_):
            self.send_client(_error(None, INVALID_REQUEST, "Invalid Request: id"))
            return
        if "method" not in obj:
            self._client_response(obj)
            return
        method = obj["method"]
        if not isinstance(method, str):
            if "id" in obj:
                self.send_client(_error(id_, INVALID_REQUEST, "Invalid Request: method"))
            return
        if method not in self.allowed:
            with self.lock:
                self._host_event("method_refused", reason=method)
            if "id" in obj:
                self.send_client(_error(id_, METHOD_NOT_FOUND,
                                        f"Method not found: {method} is not permitted by "
                                        f"the gateway"))
            return
        if method == "tools/call":
            self._call(obj)
            return
        with self.lock:
            if method == "notifications/cancelled":
                params = obj.get("params")
                rid = params.get("requestId") if isinstance(params, dict) else None
                for hold_id, h in list(self.held.items()):
                    if _valid_id(rid) and h.id == rid and type(h.id) is type(rid):
                        # The server never saw the request: the hold is withdrawn.
                        del self.held[hold_id]
                        self._unpublish_hold(hold_id)
                        try:
                            self._settle(self._step({"t": self.now(),
                                                     "withdraw": [hold_id]}))
                        except Exception:       # noqa: BLE001
                            self._fail_held()
                        _log(f"the client withdrew the held call {rid!r} ({hold_id})")
                        return
            if "id" in obj:
                if _id_key(id_) in self.inflight or self._held_id(id_):
                    self.send_client(_error(id_, INVALID_REQUEST,
                                            "Invalid Request: the id of a request in flight"))
                    return
                self.inflight.add(_id_key(id_))
                if method == "tools/list":
                    self.list_ids.add(_id_key(id_))
            self.send_server(wire(obj))

    def _client_response(self, obj: dict) -> None:
        """A response of the client: passed on only for a request of the
        server that is in flight."""
        id_ = obj.get("id")
        with self.lock:
            key = _id_key(id_)
            if "id" in obj and ("result" in obj or "error" in obj) \
                    and key in self.server_requests:
                self.server_requests.discard(key)
                self.send_server(wire(obj))
                return
            self._host_event("unsolicited_response", reason=f"client: {key}")

    def _held_id(self, id_: Any) -> bool:
        return any(h.id == id_ and type(h.id) is type(id_) for h in self.held.values())

    def _call(self, obj: dict) -> None:
        id_ = obj.get("id")
        if "id" not in obj:
            _log("a tools/call without id was dropped (fail-closed)")
            return
        params = obj.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str):
            self.send_client(_error(id_, INVALID_PARAMS, "Invalid params: tools/call "
                                                         "needs a string 'name'"))
            return
        if not _IDENT_RE.fullmatch(name):
            self.send_client(_error(id_, INVALID_PARAMS,
                                    f"Unknown tool: {name!r} (not a tool of the CADL file)"))
            return
        if "arguments" in params and not isinstance(params["arguments"], dict):
            self.send_client(_error(id_, INVALID_PARAMS, "Invalid params: 'arguments' "
                                                         "must be an object"))
            return
        m = {"from": self.agent["text"], "to": self.server["text"], "name": name}
        with self.lock:
            if self._held_id(id_) or _id_key(id_) in self.inflight:
                self.send_client(_error(id_, INVALID_REQUEST,
                                        "Invalid Request: the id of a request in flight"))
                return
            try:
                out = self._step({"t": self.now(), "messages": [m]})
                step = out["steps"][-1]
                decision = step["result"]["decision"]
            except Exception:       # noqa: BLE001 - fail-closed
                self.send_client(denial_result(id_, m, {
                    "reason": "the enforcement point failed (fail-closed)"}))
                self._fail_held()
                return
            self._settle(out)
            if decision == "allow":
                self.inflight.add(_id_key(id_))
                self.send_server(wire(obj))
            elif decision == "escalate":
                hold_id = step["hold_id"]
                self.held[hold_id] = _Held(id_, obj, m)
                self._publish_hold(hold_id)
                _log(f"tools/call {id_!r} ({m['name']}) is held as {hold_id} "
                     f"for {step['result']['escalate_to']}")
            else:
                self.send_client(denial_result(id_, m, step["result"]))

    def _settle(self, out: dict) -> None:
        """Answer or forward the held calls whose hold this step closed."""
        for rec in out["audit"]:
            if "decision" not in rec or rec.get("outcome") is None:
                continue
            self._unpublish_hold(rec["hold_id"])
            h = self.held.pop(rec["hold_id"], None)
            if h is None:
                continue
            if rec["decision"] == "allow":
                self.inflight.add(_id_key(h.id))
                self.send_server(wire(h.request))
            else:
                self.send_client(denial_result(h.id, h.message, rec))

    # --- server -> client ---------------------------------------------------------

    def server_line(self, text: str) -> None:
        if not text.strip():
            return
        obj, code, why = parse_line(text)
        if code is not None or not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
            _log(f"the server wrote a line that is refused ({why or 'not a JSON-RPC 2.0 '}"
                 f"{'' if why else 'object'}); it was dropped")
            return
        with self.lock:
            if "method" in obj:
                method = obj["method"]
                is_request = "id" in obj
                allowed = self.allowed if is_request else self.allowed_server
                if not isinstance(method, str) or method not in allowed \
                        or method == "tools/call" or (is_request and not _valid_id(obj["id"])):
                    self._host_event("method_refused", reason=f"server: {method}"
                                     if isinstance(method, str) else "server: (no name)")
                    if is_request and _valid_id(obj["id"]):
                        self.send_server(wire(_error(obj["id"], METHOD_NOT_FOUND,
                                                     "Method not found: not permitted by "
                                                     "the gateway")))
                    return
                if is_request:
                    self.server_requests.add(_id_key(obj["id"]))
                self.send_client(wire(obj))
                return
            key = _id_key(obj.get("id"))
            if "id" not in obj or key not in self.inflight:
                self._host_event("unsolicited_response", reason=f"server: {key}")
                return
            self.inflight.discard(key)
            is_list = key in self.list_ids
            self.list_ids.discard(key)
            if is_list and self.filter_tools:
                result = obj.get("result")
                if isinstance(result, dict) and isinstance(result.get("tools"), list):
                    result["tools"] = [t for t in result["tools"]
                                       if isinstance(t, dict) and t.get("name") in self.tools]
            self.send_client(wire(obj))

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
        if entry is None:
            return
        try:
            self._publish(f"holds/{hold_id}.json", {
                "run_id": self.run_id, "hold_id": hold_id, "message": entry["message"],
                "hold_time": entry["time"], "approver": entry["approver"],
                "deadline": entry["deadline"]})
        except OSError as e:
            _log(f"cannot publish hold {hold_id}: {e}")

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
            _log(f"cannot read the inbox: {e}")
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
                    _log(f"inbox file {name} changes nothing: {e}")
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
            return m, [{"event": "approval_evidence", "hold_id": hold_id,
                        "reason": digest, "message": {k: m[k] for k in ("from", "to", "name")}}]
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
                   "nonce", "mac"}
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
            if obj.get("message") != entry["message"] or obj.get("hold_time") != entry["time"]:
                raise _Refused("the file names another held message or time",
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
        if self.approver_keys is not None and any(
                name == m["name"] for _, _, _, name in self.approval_rules):
            raise _Refused("an approval or rejection comes through mcp-approve",
                           event="unauthorised_message", message=out,
                           reason="approval_as_message")
        return out

    def _fail_held(self) -> None:
        """After a failure, the held calls are denied (fail-closed)."""
        for hold_id, h in list(self.held.items()):
            del self.held[hold_id]
            self._unpublish_hold(hold_id)
            self.send_client(denial_result(h.id, h.message, {
                "reason": "the enforcement point failed (fail-closed)", "hold_id": hold_id}))

    # --- output and end -------------------------------------------------------------------

    def send_client(self, obj_or_text: Any) -> None:
        text = obj_or_text if isinstance(obj_or_text, str) else wire(obj_or_text)
        with self.lock:
            self.to_client(text)

    def send_server(self, text: str) -> None:
        with self.lock:
            self.to_server(text)

    def server_gone(self) -> None:
        """The server exited: answer every request it will not answer."""
        with self.lock:
            for key in sorted(self.inflight):
                self.send_client(_error(json.loads(key), INTERNAL_ERROR,
                                        "Internal error: the MCP server exited"))
            self.inflight.clear()
            for hold_id, h in list(self.held.items()):
                del self.held[hold_id]
                self._unpublish_hold(hold_id)
                self.send_client(_error(h.id, INTERNAL_ERROR,
                                        f"Internal error: the MCP server exited while the "
                                        f"call was held ({hold_id})"))

    def stop(self) -> None:
        """End the run: the last line of the trace, the end of the instances,
        the end record of the audit log. Held calls are dropped."""
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
            if self.held:
                _log(f"{len(self.held)} held call(s) were not answered: "
                     + ", ".join(sorted(self.held)))
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


_IDENT_HOLD_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


# === Processes ================================================================

def _strip_line_end(raw: bytes) -> bytes:
    """One line end, LF or CRLF; a lone CR stays and is refused."""
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    return raw


def serve(gw_args: dict, server_cmd: list[str], poll: float = 0.05,
          stdin=None, stdout=None) -> int:
    """Run the gateway between this process's stdin/stdout (the client)
    and a server subprocess. Returns the exit status."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    out_lock = threading.Lock()
    server: dict[str, Any] = {"proc": None}

    def to_client(text: str) -> None:
        with out_lock:
            try:
                stdout.write(text.encode("utf-8") + b"\n")
                stdout.flush()
            except (OSError, ValueError):
                pass

    def to_server(text: str) -> None:
        proc = server["proc"]
        try:
            proc.stdin.write(text.encode("utf-8") + b"\n")
            proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            pass

    # The gateway checks the file and creates the logs before the server runs.
    gw = Gateway(to_client=to_client, to_server=to_server, **gw_args)
    try:
        proc = subprocess.Popen(server_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    except OSError as e:
        gw.stop()
        raise GatewayError(f"cannot start the MCP server {server_cmd[0]!r}: {e}") from None
    server["proc"] = proc
    done = threading.Event()
    status = {"code": 0}

    def finish(code: int) -> None:
        if not done.is_set():
            status["code"] = code
            done.set()

    def decode(raw: bytes) -> str | None:
        try:
            return _strip_line_end(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None

    def client_reader() -> None:
        for raw in iter(stdin.readline, b""):
            text = decode(raw)
            try:
                if text is None:
                    gw.send_client(_error(None, PARSE_ERROR, "Parse error: not UTF-8"))
                else:
                    gw.client_line(text)
            except Exception as e:      # noqa: BLE001
                _log(f"internal error on a client line: {type(e).__name__}: {e}")
        finish(0)

    def server_reader() -> None:
        for raw in iter(proc.stdout.readline, b""):
            text = decode(raw)
            try:
                if text is None:
                    _log("the server wrote a line that is not UTF-8; it was dropped")
                else:
                    gw.server_line(text)
            except Exception as e:      # noqa: BLE001
                _log(f"internal error on a server line: {type(e).__name__}: {e}")
        if not done.is_set():
            _log("the MCP server closed its output")
            gw.server_gone()
            finish(1)

    def ticker() -> None:
        while not done.wait(poll):
            try:
                gw.tick()
            except Exception as e:      # noqa: BLE001
                _log(f"internal error in the timer: {type(e).__name__}: {e}")
            if gw.failed is not None:
                finish(1)

    for fn in (client_reader, server_reader, ticker):
        threading.Thread(target=fn, daemon=True).start()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: finish(0))
    try:
        done.wait()
    except KeyboardInterrupt:
        finish(0)
    gw.stop()
    out_lock.acquire(timeout=2)     # nothing more goes to the client
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return status["code"] if gw.failed is None else 1


def cli_gateway(args) -> int:
    server = list(args.server or [])
    if server and server[0] == "--":
        server = server[1:]
    if not server:
        print("Error: give the command of the MCP server after --", file=sys.stderr)
        return 2
    try:
        key = read_key(args.audit_key) if args.audit_key is not None else None
        ring = read_keyring(args.approver_keys) if args.approver_keys is not None else None
        init = None
        if args.init is not None:
            try:
                init = loads_unique(Path(args.init).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise GatewayError(f"cannot read --init {args.init}: {e}") from None
        trace_out = args.trace_out or Path(str(args.audit)[:-len(".jsonl")]
                                           if str(args.audit).endswith(".jsonl")
                                           else str(args.audit)).with_suffix(".trace.jsonl")
        if Path(trace_out).resolve() == Path(args.audit).resolve():
            raise GatewayError("the trace and the audit log are the same file")
        if args.poll <= 0:
            raise GatewayError("--poll must be positive")
        for name in args.allow_method:
            if name == "tools/call" or not name or any(c.isspace() for c in name):
                raise GatewayError(f"--allow-method {name!r} is not a method name")
        gw_args = dict(file=args.file, agent=args.agent, server_actor=args.server_actor,
                       audit=args.audit, trace_out=trace_out, key=key,
                       inbox=args.approvals, init=init,
                       filter_tools=not args.no_filter_tools, approver_keys=ring,
                       insecure_approvals=args.insecure_approvals,
                       allow_methods=tuple(args.allow_method))
        code = serve(gw_args, server, poll=args.poll)
    except (GatewayError, AuditKeyError, TraceError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    # The readers are daemon threads that may still block on stdin or hold
    # a lock of stdout; the logs are closed, so leave without the shutdown
    # of the interpreter, which can abort on such a lock.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)
    return code     # not reached


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
                   hold_time=hold.get("hold_time"))
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
        key = read_key(args.key) if args.key is not None else None
        obj = approval_file(args.inbox, args.hold_id, args.decision, args.actor, key)
        path = _drop(args.inbox, obj)
    except (GatewayError, AuditKeyError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    m = obj.get("message") or {}
    print(f"{path}: {args.decision} {args.hold_id} "
          f"({m.get('from')} -> {m.get('to')} : {m.get('name')} at {obj.get('hold_time')})"
          + ("" if key else " WITHOUT a MAC"))
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
