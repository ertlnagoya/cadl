"""An A2A gateway for the agent governance extension (Appendix G, G.11).

`cadl a2a-gateway` is an HTTP server in front of one A2A agent, the actor
``Y`` of a CADL file (``--agent-actor``), and passes requests on to that
agent (``--upstream URL``). It speaks the JSON-RPC binding of A2A 1.0.0
(https://a2a-protocol.org/v1.0.0/specification/, sections 3, 4, 5.4 and
9): JSON-RPC 2.0 over HTTP POST with ``Content-Type: application/json``,
the methods ``SendMessage``, ``SendStreamingMessage``, ``GetTask``,
``CancelTask`` and ``SubscribeToTask``, and the service parameter
``A2A-Version: 1.0`` as an HTTP header.

Sender. Every request is authenticated before its body is read as JSON:
the header ``Authorization: CADL-HMAC actor="X", nonce="N", mac="M"``
carries the HMAC-SHA256, under the key of the actor ``X`` in
``--client-keys``, of the canonical JSON (G.8) of the actor, the run id
of the gateway, the nonce, the HTTP method, the path and the SHA-256 of
the body (`request_mac`). A nonce is used once per run, and the run id
binds a request to one run of the gateway. Every response carries the
run id in the header ``CADL-Run-Id``; a 401 response also names it in
``WWW-Authenticate``. A request that is not authenticated is answered
with HTTP 401, is recorded as the event ``unauthenticated_request``, and
is neither decided nor passed on: without an authenticated identity
there is no sender (G.11).

Message. A2A messages have no name of an operation. A ``SendMessage``
whose ``message.extensions`` lists ``EXTENSION_URI`` and whose
``message.metadata[EXTENSION_URI]`` is ``{"name": e, "args": [...]}`` is
the message ``X -> Y : e`` at the time of its arrival, decided by the
online runtime (:class:`cadl.agents_runtime.AgentsRuntime`):

- ``allow``: the request is passed on, re-serialised, with an id of the
  gateway; the response of the agent comes back with the id of the
  client.
- ``deny``: the gateway answers with a task of its own in the state
  ``TASK_STATE_REJECTED`` whose status message names the reason, the
  norms, the hold, the fallback and the actors to notify.
- ``escalate``: the request is held. A blocking request waits until the
  hold closes; with ``configuration.returnImmediately`` the gateway
  answers at once with a task of its own in ``TASK_STATE_SUBMITTED`` and
  settles it later. A released request is passed on; any other closing
  answers with a rejected task. A client that closes the connection of a
  blocking held request, or cancels the task of a held request, withdraws
  it (G.6).

A message without the extension has no name and is rejected. An approval
or a rejection does not come as an A2A message: approvals come through
the inbox directory, as for `cadl mcp-gateway` (`cadl mcp-approve`).

A client sees only its own tasks: ``GetTask``, ``CancelTask`` and a
``SendMessage`` that names a task or a context are refused with
``TaskNotFoundError`` (-32001) unless the task or context was created for
the same actor, and recorded as ``task_refused``. ``ListTasks``,
``GetExtendedAgentCard`` and push notifications are refused
(``UnsupportedOperationError`` -32004, ``PushNotificationNotSupportedError``
-32003) and recorded as ``method_refused``; any other method gets -32601.

Streams. ``SendStreamingMessage`` is decided once, as ``SendMessage``,
before anything is passed on; its answer is a stream of Server-Sent
Events, each a JSON-RPC response re-serialised by the gateway. A denial is
a stream of one rejected task. A held stream sends SSE comments until the
hold closes (a client that goes withdraws it), then the agent's events of
the released request or one rejected task; with ``returnImmediately`` it
is one event, the submitted task of the gateway. Every event of the agent
is parsed strictly, has to answer this request and belong to one task or
message of the same client, or the stream ends with
``InvalidAgentResponseError``. ``SubscribeToTask`` follows a task of the
client, as ``GetTask``, without a decision.

The trace and the audit log are those of `cadl mcp-gateway`
(:class:`cadl.gateway_core.EnforcementPoint`): `cadl agents-run` on the
trace makes the same decisions and writes the same audit log, byte for
byte.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import itertools
import json
import os
import queue
import re
import secrets
import signal
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import a2a_rest as A2ARest
from . import agents as G
from .audit_log import AuditKeyError, canonical, loads_unique, read_key
from .gateway_core import (
    _IDENT_RE,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    EnforcementPoint,
    GatewayError,
    _error,
    _Held,
    _valid_id,
    denial_text,
    parse_json,
    read_keyring,
    wire,
)
from .gateway_core import log as _core_log
from .norms_eval import TraceError

EXTENSION_URI = "https://www.ertl.jp/cadl-spec/a2a/agents-message/v0.1"
A2A_VERSION = "1.0"
AUTH_SCHEME = "CADL-HMAC"
# Separates the MAC of a request from the MAC of an inbox file, which has
# no field `scheme`, also when an actor uses one key for both.
REQUEST_SCHEME = "cadl-a2a-request/1"
AGENT_CARD_PATH = "/.well-known/agent-card.json"

# A2A 1.0.0, 5.4 and 9.5.
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
INVALID_AGENT_RESPONSE = -32006
VERSION_NOT_SUPPORTED = -32009

REFUSED_METHODS = {
    "ListTasks": UNSUPPORTED_OPERATION,
    "GetExtendedAgentCard": UNSUPPORTED_OPERATION,
    "CreateTaskPushNotificationConfig": PUSH_NOT_SUPPORTED,
    "GetTaskPushNotificationConfig": PUSH_NOT_SUPPORTED,
    "ListTaskPushNotificationConfigs": PUSH_NOT_SUPPORTED,
    "DeleteTaskPushNotificationConfig": PUSH_NOT_SUPPORTED,
}

MAX_BODY = 1 << 20
MAX_RESPONSE = 16 << 20
MAX_ARGS = 16
MAX_ARG_LEN = 256
UNAUTH_PER_SECOND = 20         # records of refused requests a second, per event
MAX_NONCES = 1_000_000         # authenticated requests of one actor in one run
MAX_HANDLERS = 64              # authenticated requests handled at the same time
MAX_READERS = 64               # connections whose request is being read
MAX_READERS_PER_ADDRESS = 4    # of those, from one remote address
MAX_READERS_PER_PREFIX = 16    # of those, from one IPv4 /24 or IPv6 /48
READER_WAIT = 1.0              # seconds a connection waits for a free reader slot
MAX_HANDLERS_PER_ACTOR = 16    # authenticated requests of one actor at the same time
MAX_HOLDS_PER_ACTOR = 64       # holds of one actor, blocking or not
READ_TIMEOUT = 10.0            # seconds to read the request line, headers and body
MAX_HANDSHAKES = 256           # TLS handshakes at the same time, with --client-ca

_AUTH_RE = re.compile(r'CADL-HMAC actor="([A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]{1,9}\])?)", '
                      r'nonce="([0-9a-f]{32})", mac="([0-9a-f]{64})"')
_VERSION_RE = re.compile(r"1\.0(?:\.[0-9]+)?")
_TOKEN_RE = re.compile(r"[\x21-\x7e]+")
_MESSAGE_KEYS = {"messageId", "contextId", "taskId", "role", "parts", "metadata",
                 "extensions", "referenceTaskIds"}
_PART_KINDS = ("text", "raw", "url", "data")
_PART_KEYS = set(_PART_KINDS) | {"metadata", "filename", "mediaType"}
_CONFIG_KEYS = {"acceptedOutputModes", "taskPushNotificationConfig", "historyLength",
                "returnImmediately"}


def _log(text: str) -> None:
    _core_log("cadl a2a-gateway", text)


def request_mac(key: bytes, actor: str, run_id: str, nonce: str, method: str, path: str,
                body: bytes) -> str:
    """The MAC of a request: HMAC-SHA256 over the canonical JSON (G.8) of
    the scheme `REQUEST_SCHEME`, the actor, the run, the nonce, the HTTP
    method, the path and the SHA-256 of the body."""
    return hmac.new(key, canonical({
        "scheme": REQUEST_SCHEME, "actor": actor, "run_id": run_id, "nonce": nonce,
        "method": method, "path": path, "body_sha256": hashlib.sha256(body).hexdigest()}),
        hashlib.sha256).hexdigest()


def authorization(key: bytes, actor: str, run_id: str, body: bytes, path: str = "/",
                  method: str = "POST", nonce: str | None = None) -> str:
    """The value of the header `Authorization` for a request of `actor`."""
    nonce = nonce or secrets.token_hex(16)
    mac = request_mac(key, actor, run_id, nonce, method, path, body)
    return f'{AUTH_SCHEME} actor="{actor}", nonce="{nonce}", mac="{mac}"'


@dataclass
class Reply:
    status: int
    body: Any = None                    # a JSON value, or None
    headers: dict = field(default_factory=dict)
    stream: Any = None                  # a _Stream of SSE events (text/event-stream)


STREAM_KEYS = ("task", "message", "statusUpdate", "artifactUpdate")
KEEPALIVE = 1.0                         # seconds between comments of a held stream


def _rest_chunk(chunk: bytes) -> bytes:
    """One event of a stream of the gateway (`sse`) as an event of the
    HTTP+JSON binding (11.7): the `result` of the JSON-RPC response as data,
    an error as a `google.rpc.Status` in an `error` event. Comments pass."""
    error = chunk.startswith(b"event: error\n")
    head = b"event: error\ndata: " if error else b"data: "
    if not chunk.startswith(head):
        return chunk
    obj = json.loads(chunk[len(head):])
    if "result" in obj:
        return b"data: " + wire(obj["result"]).encode("ascii") + b"\n\n"
    err = obj.get("error") or {}
    _, body = A2ARest.error(err.get("code"), str(err.get("message")))
    return b"event: error\ndata: " + wire(body).encode("ascii") + b"\n\n"


def sse(obj: dict, error: bool = False) -> bytes:
    """One event of the stream: the compact ASCII JSON of a JSON-RPC
    response, which has no line end, on one `data:` line."""
    return (b"event: error\n" if error else b"") + b"data: " \
        + wire(obj).encode("ascii") + b"\n\n"


class _Stream:
    """The events of a streamed response, with a function that runs once
    when the stream ends or the client goes (`close`)."""

    def __init__(self, gen, on_close: Callable[[], None] | None = None) -> None:
        self.gen, self.on_close, self.closed = gen, on_close, False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return next(self.gen)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.gen.close()
        finally:
            if self.on_close is not None:
                self.on_close()


class _NotSent(Exception):
    """The request could not be sent to the agent."""


class _NoResponse(Exception):
    """The request was sent, and no valid response came back."""


@dataclass
class _A2AHeld(_Held):
    actor: str = ""
    upstream_id: int = 0
    return_immediately: bool = False
    task_id: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    outcome: tuple | None = None


@dataclass
class _Task:
    """A task of the gateway: a rejection, or a held request."""
    id: str
    owner: str
    context_id: str
    state: str
    text: str | None = None
    meta: dict = field(default_factory=dict)
    hold_id: str | None = None
    upstream_id: str | None = None      # the task of the agent after a release
    final: dict | None = None           # the message of the agent after a release
    timestamp: str = ""


def _result_meta(rec: dict) -> dict:
    return {k: rec.get(k) for k in ("decision", "reason", "hold_id", "outcome", "norms",
                                    "contracts", "fallback", "notify")
            if rec.get(k) not in (None, [])}


def _rewrite(obj: Any, old: str, new: str, top: bool = True) -> Any:
    """The task of the agent under the id of the gateway's task: the `id`
    of the task and every `taskId` and `referenceTaskIds` entry `old`."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if (k == "taskId" or (top and k == "id")) and v == old:
                out[k] = new
            elif k == "referenceTaskIds" and isinstance(v, list):
                out[k] = [new if x == old else x for x in v]
            else:
                out[k] = _rewrite(v, old, new, top=False)
        return out
    if isinstance(obj, list):
        return [_rewrite(v, old, new, top=False) for v in obj]
    return obj


class A2AGateway(EnforcementPoint):
    """The decisions of the gateway, without HTTP: `handle_post` and
    `handle_get` take a request and return the reply, `tick` observes the
    time and the inbox, and requests to the agent go out through
    `post_upstream`."""

    LOG_PREFIX = "cadl a2a-gateway"

    def __init__(self, file: Path, agent_actor: str, upstream: str,
                 client_keys: dict[str, bytes], audit: Path, trace_out: Path,
                 key: bytes | None = None, inbox: Path | None = None,
                 init: dict | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: datetime | None = None,
                 approver_keys: dict[str, bytes] | None = None,
                 insecure_approvals: bool = False,
                 run_id: str | None = None,
                 upstream_token: str | None = None,
                 upstream_timeout: float = 300.0,
                 agent_card: dict | None = None,
                 post_upstream: Callable[[str], bytes] | None = None,
                 max_nonces: int = MAX_NONCES,
                 max_handlers: int = MAX_HANDLERS,
                 max_handlers_per_actor: int = MAX_HANDLERS_PER_ACTOR,
                 max_holds_per_actor: int = MAX_HOLDS_PER_ACTOR,
                 hold_key: bytes | None = None,
                 upstream_binding: str = "jsonrpc") -> None:
        self._load(file)
        self.receiver = self._actor(agent_actor, "--agent-actor")
        decl = next((a for a in self.sos.actors if a.id.name == self.receiver["name"]), None)
        if decl is None or decl.agent is None or decl.agent.kind != "llm_agent" \
                or not G.is_member(self.policy, self.receiver["name"], self.receiver):
            raise GatewayError(f"--agent-actor {agent_actor} is not an agent of the file "
                               f"(agent: {{ kind: llm_agent, protocol: a2a }})")
        if decl.agent.protocol != "a2a":
            raise GatewayError(f"{agent_actor} does not declare protocol: a2a")
        self.approval_rules = self._approval_rules(self.receiver)
        self._check_approvable(self.receiver)
        if not client_keys:
            raise GatewayError("--client-keys names no actor")
        for actor in client_keys:
            who = G.parse_concrete_actor(actor)
            if not any(a.id.name == who["name"] for a in self.sos.actors) \
                    or not G.is_member(self.policy, who["name"], who):
                raise GatewayError(f"--client-keys: {actor} is not an actor of the file")
            if key is not None and hmac.compare_digest(client_keys[actor], key):
                raise GatewayError("a client key is the key of the audit log; use other keys")
        if hold_key is not None and any(hmac.compare_digest(hold_key, k)
                                        for k in client_keys.values()):
            raise GatewayError("the hold key is a client key; use another")
        self.client_keys = client_keys
        self.approval_names = self._approval_names()
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname \
                or parts.query or parts.fragment or parts.username or parts.password:
            raise GatewayError(f"--upstream {upstream!r} is not an http or https URL "
                               f"without query, fragment or user")
        if not _TOKEN_RE.fullmatch(parts.path or "/") or not _TOKEN_RE.fullmatch(parts.netloc):
            raise GatewayError(f"--upstream {upstream!r} has a character that is not "
                               f"printable ASCII")
        try:
            parts.port
        except ValueError:
            raise GatewayError(f"--upstream {upstream!r} has an invalid port") from None
        self.upstream = parts
        if upstream_token is not None and not _TOKEN_RE.fullmatch(upstream_token):
            raise GatewayError("the upstream token has a character that is not printable "
                               "ASCII")
        if upstream_token is not None and parts.scheme == "http" \
                and not _is_loopback(parts.hostname):
            raise GatewayError("--upstream-token needs an https upstream or a loopback "
                               "address (the token would cross the network in the clear)")
        if agent_card is not None:
            caps = agent_card.get("capabilities")
            exts = caps.get("extensions") if isinstance(caps, dict) else None
            if not isinstance(exts, list) or not any(
                    isinstance(e, dict) and e.get("uri") == EXTENSION_URI for e in exts):
                raise GatewayError(f"--agent-card does not declare the extension "
                                   f"{EXTENSION_URI} under capabilities.extensions")
        if upstream_binding not in ("jsonrpc", "rest"):
            raise GatewayError(f"--upstream-binding {upstream_binding!r} is jsonrpc or rest")
        self.upstream_binding = upstream_binding
        self.rest_ids = itertools.count(1)
        self.upstream_token = upstream_token
        self.upstream_timeout = upstream_timeout
        self.agent_card = agent_card
        self.post_upstream = post_upstream or self._post
        self.client_nonces: dict[str, set] = {a: set() for a in client_keys}
        self.max_nonces = max_nonces
        self.slots = threading.BoundedSemaphore(max_handlers)
        self.max_handlers_per_actor = max_handlers_per_actor
        self.max_holds_per_actor = max_holds_per_actor
        self.actor_handlers: dict[str, int] = {}
        self.refusals: dict[str, list] = {}     # event -> [second, count, not recorded]
        self.tasks: dict[str, _Task] = {}          # the tasks of the gateway
        self.task_owner: dict[str, str] = {}       # tasks of the agent -> actor
        self.context_owner: dict[str, str] = {}    # contexts -> actor
        self.upstream_ids = itertools.count(1)
        self._open(audit, trace_out, key, inbox, init, clock, wall, approver_keys,
                   insecure_approvals, run_id, hold_key=hold_key)

    def _step(self, line: dict, close: bool = False) -> dict:
        """As in the enforcement point; nothing more is decided or recorded
        once the gateway has stopped and closed its logs."""
        if self.stopped and not close:
            raise GatewayError("the gateway has stopped")
        return super()._step(line, close)

    def _host_event(self, event: str, reason: str | None = None,
                    hold_id: str | None = None, message: dict | None = None) -> None:
        if not self.stopped:
            super()._host_event(event, reason, hold_id, message)

    def cert_actors(self) -> set:
        """The actors whose client certificates the gateway accepts (with
        --client-ca): the actors of --client-keys."""
        return set(self.client_keys)

    # --- replies -----------------------------------------------------------------

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"CADL-Run-Id": self.run_id}
        h.update(extra or {})
        return h

    def _rpc(self, obj: dict) -> Reply:
        return Reply(200, obj, self._headers())

    def _rpc_error(self, id_: Any, code: int, message: str) -> Reply:
        return self._rpc(_error(id_, code, message))

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _task_json(self, t: _Task) -> dict:
        status: dict[str, Any] = {"state": t.state, "timestamp": t.timestamp}
        if t.final is not None:
            status["message"] = t.final
        elif t.text is not None:
            status["message"] = {"messageId": f"{t.id}-status", "role": "ROLE_AGENT",
                                 "parts": [{"text": t.text}], "taskId": t.id,
                                 "contextId": t.context_id}
        out: dict[str, Any] = {"id": t.id, "contextId": t.context_id, "status": status}
        if t.meta:
            out["metadata"] = {EXTENSION_URI: t.meta}
        return out

    def _new_task(self, actor: str, context_id: str | None, state: str, text: str | None,
                  meta: dict, hold_id: str | None = None) -> _Task:
        if context_id is None:
            context_id = f"cadl-ctx-{secrets.token_hex(16)}"
            self.context_owner[context_id] = actor
        t = _Task(f"cadl-task-{secrets.token_hex(16)}", actor, context_id, state, text,
                  meta, hold_id, timestamp=self._stamp())
        self.tasks[t.id] = t
        return t

    def _rejected(self, actor: str, id_: Any, context_id: str | None, m: dict | None,
                  rec: dict) -> Reply:
        with self.lock:
            t = self._new_task(actor, context_id, "TASK_STATE_REJECTED",
                               denial_text(m, rec, "message"), _result_meta(rec))
            return self._rpc({"jsonrpc": "2.0", "id": id_,
                              "result": {"task": self._task_json(t)}})

    # --- HTTP entry points -----------------------------------------------------------

    def handle_get(self, path: str) -> Reply:
        if path == AGENT_CARD_PATH and self.agent_card is not None:
            return Reply(200, self.agent_card, self._headers())
        return Reply(404, None, self._headers())

    def handle_post(self, path: str, headers, body: bytes,
                    closed: Callable[[], bool] = lambda: False,
                    peer: str | None = None) -> Reply | None:
        """One HTTP POST. `headers` has `get_all`; `closed` tells whether
        the client has closed the connection; `peer` is the actor that the
        client certificate names (mutual TLS), or None. Returns the reply,
        or None when the client has gone."""
        if self.stopped:
            return Reply(503, None, self._headers())
        if path != "/":
            return Reply(404, None, self._headers())
        actor, why = self._authenticate(headers.get_all("Authorization") or [], path, body,
                                        peer)
        if actor is None:
            with self.lock:
                self._unauthenticated(why)
            return Reply(401, _error(None, INVALID_REQUEST, "Unauthorized"),
                         self._headers({"WWW-Authenticate":
                                        f'{AUTH_SCHEME} run="{self.run_id}"'}))
        return self._in_slot(actor, lambda: self._handle_authenticated(actor, headers, body,
                                                                       closed))

    def _in_slot(self, actor: str, handle: Callable[[], Reply | None]) -> Reply | None:
        """Run `handle` for an authenticated request in a handler slot.
        A slot only for an authenticated request: unauthenticated peers
        cannot take the slots of the clients. One actor takes at most
        `max_handlers_per_actor` of them, so that one client cannot take
        every slot (with held requests, or with requests to a slow agent)."""
        with self.lock:
            if self.actor_handlers.get(actor, 0) >= self.max_handlers_per_actor:
                return self.overloaded(f"handlers_per_actor {actor}")
            if not self.slots.acquire(blocking=False):
                return self.overloaded("handlers")
            self.actor_handlers[actor] = self.actor_handlers.get(actor, 0) + 1

        def release() -> None:
            with self.lock:
                self.actor_handlers[actor] -= 1
            self.slots.release()
        streaming = False
        try:
            reply = handle()
            if reply is not None and reply.stream is not None:
                # A stream keeps its slot until it ends.
                inner = reply.stream
                reply.stream = _Stream(inner, lambda: (inner.close(), release()))
                streaming = True
            return reply
        finally:
            if not streaming:
                release()

    # --- the HTTP+JSON binding ------------------------------------------------------

    def handle_request(self, method: str, target: str, headers, body: bytes,
                       closed: Callable[[], bool] = lambda: False,
                       peer: str | None = None) -> Reply | None:
        """One HTTP request of any binding: the agent card, a JSON-RPC
        request at `/`, or a request of the HTTP+JSON binding."""
        if method == "GET" and target == AGENT_CARD_PATH:
            return self.handle_get(target)
        if method == "POST" and target == "/":
            return self.handle_post(target, headers, body, closed, peer)
        return self.handle_rest(method, target, headers, body, closed, peer)

    def _rest_error(self, code: int, message: str, status: int | None = None,
                    headers: dict | None = None) -> Reply:
        http_status, obj = A2ARest.error(code, message, status)
        return Reply(http_status, obj, self._headers(headers))

    def handle_rest(self, method: str, target: str, headers, body: bytes,
                    closed: Callable[[], bool] = lambda: False,
                    peer: str | None = None) -> Reply | None:
        """A request of the HTTP+JSON binding (A2A 1.0.0, 11): authenticated
        as a JSON-RPC request, with the HTTP method and the request target
        (path and query) in the MAC; then translated into the JSON-RPC
        request that the gateway decides, and its answer back."""
        if self.stopped:
            return Reply(503, None, self._headers())
        actor, why = self._authenticate(headers.get_all("Authorization") or [], target, body,
                                        peer, method)
        if actor is None:
            with self.lock:
                self._unauthenticated(why)
            return self._rest_error(INVALID_REQUEST, "Unauthorized", 401,
                                    {"WWW-Authenticate": f'{AUTH_SCHEME} run="{self.run_id}"'})
        return self._in_slot(actor, lambda: self._rest_authenticated(
            actor, method, target, headers, body, closed))

    def _rest_authenticated(self, actor: str, method: str, target: str, headers, body: bytes,
                            closed: Callable[[], bool]) -> Reply | None:
        found = A2ARest.route(method, target)
        if found is None:
            with self.lock:
                self._host_event("method_refused", reason=f"{method} {target}"[:200])
            return self._rest_error(METHOD_NOT_FOUND, f"Method not found: {method} "
                                                      f"{target[:200]} is not a route of the "
                                                      f"gateway")
        op, path_params = found
        version = headers.get_all("A2A-Version") or []
        if len(version) != 1 or not _VERSION_RE.fullmatch(version[0].strip()):
            return self._rest_error(VERSION_NOT_SUPPORTED,
                                    "VersionNotSupportedError: the gateway speaks A2A 1.0 "
                                    "only (header A2A-Version: 1.0)")
        allowed = {"historyLength": "int"} if op == "GetTask" else {}
        q, why = A2ARest.query(target, allowed)
        if q is None:
            return self._rest_error(INVALID_PARAMS, f"Invalid params: {why}")
        obj: Any = {}
        if body:
            ctype = headers.get_all("Content-Type") or []
            if len(ctype) != 1 or ctype[0].split(";")[0].strip().lower() \
                    not in A2ARest.MEDIA_TYPES:
                return self._rest_error(INVALID_REQUEST, "Content-Type is application/json "
                                                         "or application/a2a+json", 415)
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                return self._rest_error(PARSE_ERROR, "Parse error: not UTF-8")
            obj, code, why = parse_json(text)
            if code is not None:
                return self._rest_error(code, ("Parse error" if code == PARSE_ERROR
                                               else "Invalid Request") + f": {why}")
            if not isinstance(obj, dict):
                return self._rest_error(INVALID_REQUEST, "Invalid Request: the body is a "
                                                         "JSON object")
        elif op in ("SendMessage", "SendStreamingMessage", "CreateTaskPushNotificationConfig"):
            return self._rest_error(INVALID_PARAMS, f"Invalid params: {op} has a body")
        if op in ("GetTask", "ListTasks", "GetExtendedAgentCard",
                  "ListTaskPushNotificationConfigs", "GetTaskPushNotificationConfig",
                  "DeleteTaskPushNotificationConfig") and body:
            return self._rest_error(INVALID_REQUEST, f"Invalid Request: {op} has no body")
        # The parameters of the path win only when the body agrees with them.
        for k, v in path_params.items():
            if k in obj and obj[k] != v:
                return self._rest_error(INVALID_PARAMS, f"Invalid params: {k} of the body "
                                                        f"differs from the path")
        params = dict(obj, **path_params, **q)
        with self.lock:
            id_ = f"rest-{next(self.rest_ids)}"
        reply = self._dispatch(actor, op, id_, params, closed)
        return self._to_rest(reply)

    def _to_rest(self, reply: Reply | None) -> Reply | None:
        """The answer of the gateway to a JSON-RPC request as the answer of
        the HTTP+JSON binding: a result as the body, an error as in 11.6,
        a stream with `StreamResponse` objects as data."""
        if reply is None:
            return None
        if reply.stream is not None:
            inner = reply.stream
            reply.stream = _Stream((_rest_chunk(c) for c in inner), inner.close)
            return reply
        body = reply.body
        if isinstance(body, dict) and "result" in body and reply.status == 200:
            return Reply(200, body["result"], reply.headers)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            err = body["error"]
            status, obj = A2ARest.error(err.get("code"), str(err.get("message")),
                                        reply.status)
            return Reply(status, obj, reply.headers)
        return reply

    def _dispatch(self, actor: str, method: str, id_: Any, params: Any,
                  closed: Callable[[], bool]) -> Reply | None:
        """A JSON-RPC request of an authenticated actor, after its envelope
        and its version have been checked."""
        if method == "SendMessage":
            return self._send_message(actor, id_, params, closed)
        if method == "SendStreamingMessage":
            return self._send_message(actor, id_, params, closed, stream=True)
        if method == "SubscribeToTask":
            return self._subscribe(actor, id_, params, closed)
        if method == "GetTask":
            return self._get_task(actor, id_, params)
        if method == "CancelTask":
            return self._cancel_task(actor, id_, params)
        with self.lock:
            self._host_event("method_refused", reason=method[:200])
        if method in REFUSED_METHODS:
            code = REFUSED_METHODS[method]
            name = ("PushNotificationNotSupportedError" if code == PUSH_NOT_SUPPORTED
                    else "UnsupportedOperationError")
            return self._rpc_error(id_, code, f"{name}: {method} is not supported by the "
                                              f"CADL gateway")
        return self._rpc_error(id_, METHOD_NOT_FOUND,
                               f"Method not found: {method[:200]} is not permitted by the "
                               f"gateway")

    def _handle_authenticated(self, actor: str, headers, body: bytes,
                              closed: Callable[[], bool]) -> Reply | None:
        ctype = headers.get_all("Content-Type") or []
        if len(ctype) != 1 or ctype[0].split(";")[0].strip().lower() != "application/json":
            return Reply(415, _error(None, INVALID_REQUEST,
                                     "Invalid Request: Content-Type is application/json"),
                         self._headers())
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return self._rpc_error(None, PARSE_ERROR, "Parse error: not UTF-8")
        obj, code, why = parse_json(text)
        if code is not None:
            return self._rpc_error(None, code, ("Parse error" if code == PARSE_ERROR
                                                else "Invalid Request") + f": {why}")
        if isinstance(obj, list):
            return self._rpc_error(None, INVALID_REQUEST,
                                   "Invalid Request: JSON-RPC batches are not supported")
        if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0" \
                or set(obj) - {"jsonrpc", "id", "method", "params"}:
            return self._rpc_error(None, INVALID_REQUEST, "Invalid Request")
        id_ = obj.get("id")
        if "id" not in obj or not _valid_id(id_):
            return self._rpc_error(None, INVALID_REQUEST,
                                   "Invalid Request: a request with a string or integer id")
        method = obj.get("method")
        if not isinstance(method, str):
            return self._rpc_error(id_, INVALID_REQUEST, "Invalid Request: method")
        version = headers.get_all("A2A-Version") or []
        if len(version) != 1 or not _VERSION_RE.fullmatch(version[0].strip()):
            return self._rpc_error(id_, VERSION_NOT_SUPPORTED,
                                   "VersionNotSupportedError: the gateway speaks A2A 1.0 "
                                   "only (header A2A-Version: 1.0)")
        return self._dispatch(actor, method, id_, obj.get("params"), closed)

    def _unauthenticated(self, why: str) -> None:
        self._refusal("unauthenticated_request", why)

    def _refusal(self, event: str, why: str) -> None:
        """Record a request that is refused before it has an identity: at
        most `UNAUTH_PER_SECOND` records of `event` in a second of the
        gateway's clock; the next record counts the requests that were not
        recorded."""
        second = int(self.now())
        entry = self.refusals.setdefault(event, [-1, 0, 0])
        if second != entry[0]:
            entry[0], entry[1] = second, 0
        if entry[1] >= UNAUTH_PER_SECOND:
            entry[2] += 1
            return
        entry[1] += 1
        reason = why
        if entry[2]:
            reason += f";not_recorded_before={entry[2]}"
            entry[2] = 0
        self._host_event(event, reason=reason)

    def refused(self, event: str, why: str) -> None:
        """Record a refusal of the HTTP layer at the capped rate."""
        with self.lock:
            if not self.stopped:
                self._refusal(event, why)

    def overloaded(self, why: str = "handlers") -> Reply:
        """A request beyond a limit of the gateway: 503."""
        with self.lock:
            self._refusal("overloaded", why)
        return Reply(503, _error(None, INTERNAL_ERROR, "the gateway is busy"),
                     self._headers({"Retry-After": "1"}))

    def _authenticate(self, values: list, path: str, body: bytes, peer: str | None = None,
                      method: str = "POST") -> tuple[str | None, str]:
        """The actor of a request, or None and the reason. With mutual TLS
        (`peer` is the actor of the client certificate) the header has to
        name the same actor; its MAC and nonce are checked as without."""
        if not values:
            return None, "missing"
        if len(values) != 1:
            return None, "malformed"
        match = _AUTH_RE.fullmatch(values[0].strip())
        if match is None:
            return None, "malformed"
        actor, nonce, mac = match.groups()
        who = G.parse_concrete_actor(actor)
        key = self.client_keys.get(who["text"]) if who else None
        if key is None:
            return None, "unknown_actor"
        want = request_mac(key, who["text"], self.run_id, nonce, method, path, body)
        if not hmac.compare_digest(mac, want):
            return None, "bad_mac"
        if peer is not None and peer != who["text"]:
            return None, "certificate_mismatch"
        with self.lock:
            nonces = self.client_nonces[who["text"]]
            if nonce in nonces:
                return None, "replayed"
            if len(nonces) >= self.max_nonces:
                return None, "nonce_limit"
            nonces.add(nonce)
        return who["text"], ""

    # --- SendMessage ------------------------------------------------------------------

    def _check_message(self, params: Any) -> tuple[dict | None, str | None]:
        """The checked params of SendMessage, or an error text."""
        if isinstance(params, dict) and "tenant" in params:
            return None, ("tenant is not supported by the CADL gateway (the tasks of a "
                          "client are kept by their id alone)")
        if not isinstance(params, dict) or "message" not in params \
                or set(params) - {"message", "configuration", "metadata"}:
            return None, "params has message and optionally configuration, metadata"
        if "metadata" in params and not isinstance(params["metadata"], dict):
            return None, "metadata is an object"
        msg = params["message"]
        if not isinstance(msg, dict) or set(msg) - _MESSAGE_KEYS:
            return None, "message has the fields of an A2A 1.0 Message"
        if not isinstance(msg.get("messageId"), str) or not msg["messageId"]:
            return None, "message.messageId is a non-empty string"
        if msg.get("role") != "ROLE_USER":
            return None, "message.role is ROLE_USER"
        parts = msg.get("parts")
        if not isinstance(parts, list) or not parts:
            return None, "message.parts is a non-empty list"
        for p in parts:
            if not isinstance(p, dict) or set(p) - _PART_KEYS \
                    or sum(k in p for k in _PART_KINDS) != 1:
                return None, "a part has exactly one of text, raw, url, data"
            for k in ("text", "raw", "url", "filename", "mediaType"):
                if k in p and not isinstance(p[k], str):
                    return None, f"part.{k} is a string"
            if "metadata" in p and not isinstance(p["metadata"], dict):
                return None, "part.metadata is an object"
        for k in ("contextId", "taskId"):
            if k in msg and not isinstance(msg[k], str):
                return None, f"message.{k} is a string"
        for k in ("extensions", "referenceTaskIds"):
            if k in msg and (not isinstance(msg[k], list)
                             or any(not isinstance(x, str) for x in msg[k])):
                return None, f"message.{k} is a list of strings"
        if "metadata" in msg and not isinstance(msg["metadata"], dict):
            return None, "message.metadata is an object"
        conf = params.get("configuration", {})
        if not isinstance(conf, dict) or set(conf) - _CONFIG_KEYS:
            return None, "configuration has the fields of SendMessageConfiguration"
        if "returnImmediately" in conf and not isinstance(conf["returnImmediately"], bool):
            return None, "configuration.returnImmediately is a boolean"
        h = conf.get("historyLength")
        if "historyLength" in conf and (not isinstance(h, int) or isinstance(h, bool) or h < 0):
            return None, "configuration.historyLength is a non-negative integer"
        if "acceptedOutputModes" in conf and (
                not isinstance(conf["acceptedOutputModes"], list)
                or any(not isinstance(x, str) for x in conf["acceptedOutputModes"])):
            return None, "configuration.acceptedOutputModes is a list of strings"
        return params, None

    @staticmethod
    def _name(msg: dict) -> tuple[str | None, list, str | None]:
        """(name, args, error) of the extension; name None without it."""
        if EXTENSION_URI not in msg.get("extensions", []):
            return None, [], None
        ext = msg.get("metadata", {}).get(EXTENSION_URI)
        if ext is None:
            return None, [], None
        if not isinstance(ext, dict) or set(ext) - {"name", "args"} \
                or not isinstance(ext.get("name"), str) or not _IDENT_RE.fullmatch(ext["name"]) \
                or len(ext["name"]) > 64:
            return None, [], (f"metadata[{EXTENSION_URI}] is "
                              f"{{\"name\": identifier, \"args\": [strings]}}")
        args = ext.get("args", [])
        if not isinstance(args, list) or len(args) > MAX_ARGS \
                or any(not isinstance(a, str) or len(a) > MAX_ARG_LEN for a in args):
            return None, [], (f"args of metadata[{EXTENSION_URI}] is a list of at most "
                              f"{MAX_ARGS} strings of at most {MAX_ARG_LEN} characters")
        return ext["name"], list(args), None

    def _task_refused(self, method: str, id_: Any, what: str) -> Reply:
        self._host_event("task_refused", reason=f"{method}: {what}")
        return self._rpc_error(id_, TASK_NOT_FOUND, "TaskNotFoundError: no such task")

    def _send_message(self, actor: str, id_: Any, params: Any,
                      closed: Callable[[], bool], stream: bool = False) -> Reply | None:
        """SendMessage, and with `stream` SendStreamingMessage: the same
        checks and the same decision, taken once before anything is passed
        on; a streamed answer is an SSE stream of JSON-RPC responses."""
        reply = self._send_message_inner(actor, id_, params, closed, stream)
        return self._single(reply) if stream else reply

    def _single(self, reply: Reply | None) -> Reply | None:
        """A JSON-RPC result of a streamed request as a stream of one event;
        errors stay plain JSON, as before a stream begins."""
        if reply is None or reply.stream is not None or not isinstance(reply.body, dict) \
                or "result" not in reply.body or reply.status != 200:
            return reply
        body = reply.body
        return Reply(200, None, reply.headers, _Stream(iter([sse(body)])))

    def _send_message_inner(self, actor: str, id_: Any, params: Any,
                            closed: Callable[[], bool], stream: bool) -> Reply | None:
        method = "SendStreamingMessage" if stream else "SendMessage"
        params, why = self._check_message(params)
        if params is None:
            return self._rpc_error(id_, INVALID_PARAMS, f"Invalid params: {why}")
        conf = params.get("configuration", {})
        if "taskPushNotificationConfig" in conf:
            with self.lock:
                self._host_event("method_refused",
                                 reason=f"{method}: taskPushNotificationConfig")
            return self._rpc_error(id_, PUSH_NOT_SUPPORTED,
                                   "PushNotificationNotSupportedError: push notifications "
                                   "are not supported by the CADL gateway")
        msg = params["message"]
        name, args, why = self._name(msg)
        if why is not None:
            return self._rpc_error(id_, INVALID_PARAMS, f"Invalid params: {why}")
        forwarded = json.loads(json.dumps(params))       # a copy, ids of the agent
        with self.lock:
            # Only the client's own tasks and contexts.
            fmsg = forwarded["message"]
            for k in ("taskId",):
                if k in msg:
                    up = self._own_task(actor, msg[k])
                    if up is None:
                        return self._task_refused(method, id_, "taskId")
                    fmsg[k] = up
            if "referenceTaskIds" in msg:
                ups = [self._own_task(actor, x) for x in msg["referenceTaskIds"]]
                if any(u is None for u in ups):
                    return self._task_refused(method, id_, "referenceTaskIds")
                fmsg["referenceTaskIds"] = ups
            # A context only when the gateway has seen it given to this
            # client, by the agent or by the gateway; never claimed here.
            ctx = msg.get("contextId")
            if ctx is not None and self.context_owner.get(ctx) != actor:
                return self._task_refused(method, id_, "contextId")
        m = {"from": actor, "to": self.receiver["text"], "name": name, "args": args}
        if name is None:
            with self.lock:
                self._host_event("unauthorised_message", reason="no_name")
            return self._rejected(actor, id_, ctx, None, {
                "decision": "deny", "reason": "no_name: the message does not name a "
                                              f"CADL message (extension {EXTENSION_URI})"})
        if name in self.approval_names:
            with self.lock:
                self._host_event("unauthorised_message", reason="approval_as_message",
                                 message=m)
            return self._rejected(actor, id_, ctx, m, {
                "decision": "deny", "reason": "approval_as_message: approvals and "
                                              "rejections come through the inbox "
                                              "(cadl mcp-approve)"})
        # Everything that can fail is done before the decision. A streamed
        # request that returns at once is passed on, when released, as
        # SendMessage: its client follows the task of the gateway.
        immediate = params.get("configuration", {}).get("returnImmediately") is True
        with self.lock:
            upstream_id = next(self.upstream_ids)
        try:
            text = wire({"jsonrpc": "2.0", "id": upstream_id,
                         "method": "SendStreamingMessage" if stream else "SendMessage",
                         "params": forwarded})
            released_text = text if not (stream and immediate) else wire(
                {"jsonrpc": "2.0", "id": upstream_id, "method": "SendMessage",
                 "params": forwarded})
            params_sha256 = hashlib.sha256(canonical(params)).hexdigest()
        except (ValueError, TypeError) as e:
            return self._rpc_error(id_, INVALID_PARAMS, f"Invalid params: not "
                                                        f"representable in canonical JSON "
                                                        f"({e})")
        with self.lock:
            if sum(1 for h in self.held.values() if h.actor == actor) \
                    >= self.max_holds_per_actor:
                # Before the decision: one client cannot create holds without end.
                return self.overloaded(f"holds_per_actor {actor}")
            try:
                out = self._step({"t": self.now(), "messages": [m]})
                step = out["steps"][-1]
                decision = step["result"]["decision"]
            except Exception:       # noqa: BLE001 - fail-closed
                if self.stopped:
                    return self._rpc_error(id_, INTERNAL_ERROR,
                                           "Internal error: the gateway has stopped")
                self._fail_held()
                return self._rejected(actor, id_, ctx, m, {
                    "decision": "deny",
                    "reason": "the enforcement point failed (fail-closed)"})
            self._settle(out)
            if decision == "deny":
                return self._rejected(actor, id_, ctx, m, step["result"])
            if decision == "escalate":
                hold_id = step["hold_id"]
                h = _A2AHeld(id_, {"jsonrpc": "2.0", "id": id_, "method": method,
                                   "params": params}, m, released_text, params_sha256,
                             actor=actor, upstream_id=upstream_id,
                             return_immediately=immediate)
                self.held[hold_id] = h
                self._publish_hold(hold_id)
                _log(f"{method} {id_!r} ({actor} -> {m['to']} : {name}) is held as "
                     f"{hold_id} for {step['result']['escalate_to']}")
                if immediate:
                    t = self._new_task(actor, ctx, "TASK_STATE_SUBMITTED",
                                       f"Held by the CADL enforcement point as {hold_id} "
                                       f"for the approval of "
                                       f"{step['result']['escalate_to']}.",
                                       _result_meta(dict(step["result"], hold_id=hold_id)),
                                       hold_id)
                    h.task_id = t.id
                    return self._rpc({"jsonrpc": "2.0", "id": id_,
                                      "result": {"task": self._task_json(t)}})
        if decision == "allow":
            if stream:
                return self._stream_reply(self._relay(actor, id_, text, upstream_id, m,
                                                      closed=closed))
            return self._forward(actor, id_, text, upstream_id, m)
        if stream:
            return self._stream_reply(self._wait_stream(hold_id, h, closed))
        return self._wait(hold_id, h, closed)

    # --- streams ------------------------------------------------------------------------

    def _stream_reply(self, gen) -> Reply:
        return Reply(200, None, self._headers(), _Stream(gen))

    def _wait_stream(self, hold_id: str, h: "_A2AHeld", closed: Callable[[], bool]):
        """A held stream: comments until the hold closes (a client that
        goes withdraws the request), then the stream of the agent for a
        released request, or one rejected task."""
        done = False
        try:
            while not h.done.wait(KEEPALIVE):
                if closed():
                    return
                yield f": held as {hold_id}\n\n".encode("ascii")
            done = True
        finally:
            if not done:
                with self.lock:
                    if self.held.get(hold_id) is h:
                        self._withdraw(hold_id)
                        _log(f"the client closed the stream of the held request "
                             f"{h.id!r} ({hold_id}); it is withdrawn")
        kind, rec = h.outcome
        if kind == "allow":
            yield from self._relay(h.actor, h.id, h.wire, h.upstream_id, h.message,
                                   closed=closed)
        elif kind == "stopped":
            yield sse(_error(h.id, INTERNAL_ERROR, f"Internal error: the gateway stopped "
                                                   f"while the request was held "
                                                   f"({hold_id})"), error=True)
        else:
            reply = self._rejected(h.actor, h.id,
                                   h.request["params"]["message"].get("contextId"),
                                   h.message, rec)
            yield sse(reply.body)

    def _connect(self, method: str, target: str, body: bytes | None, accept: str):
        """Send one request to the agent: (connection, response); raises
        _NotSent or _NoResponse."""
        u = self.upstream
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        try:
            conn = cls(u.hostname, u.port, timeout=self.upstream_timeout)
        except Exception as e:      # noqa: BLE001
            raise _NotSent(f"{type(e).__name__}: {e}") from None
        headers = {"A2A-Version": A2A_VERSION, "Accept": accept}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.upstream_token is not None:
            headers["Authorization"] = f"Bearer {self.upstream_token}"
        try:
            conn.request(method, target, body=body, headers=headers)
        except (OSError, ValueError, http.client.HTTPException) as e:
            conn.close()
            raise _NotSent(f"{type(e).__name__}: {e}") from None
        try:
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise _NoResponse(f"{type(e).__name__}: {e}") from None
        return conn, resp

    def _rest_request(self, text: str) -> tuple[str, str, bytes | None, int]:
        """The request of the HTTP+JSON binding for a JSON-RPC request of
        the gateway (`text`, which the gateway wrote): method, target, body
        and the JSON-RPC id that the answer is given."""
        obj = json.loads(text)
        method, target, body = A2ARest.path_of(self.upstream.path or "/", obj["method"],
                                               obj["params"])
        data = None if body is None else wire(body).encode("ascii")
        if method == "POST" and data is None:
            data = b""
        return method, target, data, obj["id"]

    def _open_stream(self, text: str):
        """POST a streamed request to the agent: (connection, response, the
        JSON-RPC id of a request of the HTTP+JSON binding or None), or
        _NotSent or _NoResponse."""
        if self.upstream_binding == "rest":
            method, target, data, upstream_id = self._rest_request(text)
            conn, resp = self._connect(method, target, data, "text/event-stream")
            return conn, resp, upstream_id
        conn, resp = self._connect("POST", self.upstream.path or "/", text.encode("ascii"),
                                   "text/event-stream")
        if resp.status != 200:
            conn.close()
            raise _NoResponse(f"HTTP status {resp.status}")
        return conn, resp, None

    @staticmethod
    def _sse_events(resp):
        """(event, data) of an SSE response, read line by line; a line with
        a carriage return other than at its end, or an event larger than
        16 MiB, ends the stream with _NoResponse."""
        event, data, size = "message", [], 0
        while True:
            try:
                raw = resp.readline(MAX_RESPONSE + 1)
            except (OSError, http.client.HTTPException) as e:
                raise _NoResponse(f"{type(e).__name__}: {e}") from None
            if not raw:
                return
            if len(raw) > MAX_RESPONSE:
                raise _NoResponse("an SSE line is larger than 16 MiB")
            line = raw[:-1] if raw.endswith(b"\n") else raw
            if line.endswith(b"\r"):
                line = line[:-1]
            if b"\r" in line:
                raise _NoResponse("a carriage return inside an SSE line")
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                raise _NoResponse("an SSE line that is not UTF-8") from None
            if not text:
                if data:
                    yield event, "\n".join(data)
                event, data, size = "message", [], 0
                continue
            if text.startswith(":"):
                continue
            key, _, value = text.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if key == "event":
                event = value
            elif key == "data":
                size += len(value)
                if size > MAX_RESPONSE:
                    raise _NoResponse("an SSE event is larger than 16 MiB")
                data.append(value)

    @staticmethod
    def _json_answer(resp) -> str:
        """A plain JSON answer of the agent to a streamed request, read as
        `_post` reads one: at most 16 MiB, strict UTF-8."""
        try:
            data = resp.read(MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException) as e:
            raise _NoResponse(f"{type(e).__name__}: {e}") from None
        if len(data) > MAX_RESPONSE:
            raise _NoResponse("the response is larger than 16 MiB")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise _NoResponse("the response is not UTF-8") from None

    @staticmethod
    def _reader(events, stop: threading.Event) -> "queue.Queue":
        """The events of the agent, read in a thread of their own, so that
        the stream can watch its client while the agent is silent."""
        q: queue.Queue = queue.Queue(maxsize=64)

        def put(item) -> bool:
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.5)
                    return True
                except queue.Full:
                    continue
            return False

        def run() -> None:
            try:
                for item in events:
                    if not put(("event", item)):
                        return
                put(("end", None))
            except _NoResponse as e:
                put(("error", e))
            except Exception as e:      # noqa: BLE001
                put(("error", _NoResponse(f"{type(e).__name__}: {e}")))
        threading.Thread(target=run, daemon=True).start()
        return q

    def _relay(self, actor: str, id_: Any, text: str, upstream_id: int, m: dict | None,
               own: str | None = None, upstream_task: str | None = None,
               closed: Callable[[], bool] = lambda: False):
        """The stream of the agent for an allowed or released request (or a
        subscription to a task of the client), event by event: every event
        is parsed strictly, has to be a response to this request, has to
        belong to one task or message of this client, and is passed on
        re-serialised with the client's id (and, with `own`, the task of the
        gateway in place of `upstream_task`)."""
        try:
            conn, resp, rest_id = self._open_stream(text)
        except _NotSent as e:
            _log(f"a stream was decided but could not be sent to the agent: {e}")
            with self.lock:
                self._host_event("forward_failed", reason=str(e)[:200], message=m)
            yield sse(_error(id_, INTERNAL_ERROR, "Internal error: the request could not "
                                                  "be sent to the agent"), error=True)
            return
        except _NoResponse as e:
            yield sse(self._invalid_body(id_, str(e)[:200], m), error=True)
            return
        task_id = upstream_task
        stop = threading.Event()
        try:
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if rest_id is not None and (resp.status != 200 or ctype in A2ARest.MEDIA_TYPES):
                # An answer of the HTTP+JSON binding that is not a stream:
                # an error of 11.6 before the stream began.
                answer = A2ARest.to_jsonrpc(rest_id, resp.status, self._json_answer(resp))
                if answer is None:
                    raise _NoResponse(f"HTTP status {resp.status} without an A2A error")
                events = iter([("message", answer)])
            elif ctype == "application/json":
                events = iter([("message", self._json_answer(resp))])
            elif ctype == "text/event-stream":
                events = self._sse_events(resp)
                if rest_id is not None:
                    events = ((name, A2ARest.event_to_jsonrpc(rest_id, name, data))
                              for name, data in events)
            else:
                yield sse(self._invalid_body(id_, f"Content-Type {ctype!r}", m), error=True)
                return
            q = self._reader(events, stop)
            next_comment = time.monotonic() + KEEPALIVE
            while True:
                try:
                    kind, item = q.get(timeout=0.1)
                except queue.Empty:
                    # The agent is silent: a client that has gone ends the
                    # stream and the request to the agent; one that stays
                    # gets a comment now and then, whose writing also fails
                    # when it has gone.
                    if closed():
                        return
                    if time.monotonic() >= next_comment:
                        next_comment = time.monotonic() + KEEPALIVE
                        yield b": waiting for the agent\n\n"
                    continue
                if kind == "end":
                    return
                if kind == "error":
                    raise item
                name, data = item
                obj, code, why = parse_json(data)
                if code is not None or not isinstance(obj, dict) \
                        or obj.get("jsonrpc") != "2.0" or obj.get("id") != upstream_id \
                        or type(obj.get("id")) is not int \
                        or ("result" in obj) == ("error" in obj) \
                        or set(obj) - {"jsonrpc", "id", "result", "error"}:
                    yield sse(self._invalid_body(id_, f"a stream event that is not a "
                                                      f"JSON-RPC response ({why or 'shape'})",
                                                 m), error=True)
                    return
                if "error" in obj:
                    err = obj["error"]
                    if not isinstance(err, dict) or type(err.get("code")) is not int \
                            or not isinstance(err.get("message"), str):
                        yield sse(self._invalid_body(id_, "an error without code and "
                                                          "message", m), error=True)
                        return
                    yield sse({"jsonrpc": "2.0", "id": id_, "error": err}, error=True)
                    return
                result = obj["result"]
                ok, task_id = self._stream_result(actor, result, task_id)
                if not ok:
                    yield sse(self._invalid_body(id_, "a stream event that is not of a "
                                                      "task or message of this client", m),
                              error=True)
                    return
                if own is not None:
                    key = next(iter(result))
                    result = {key: _rewrite(result[key], upstream_task, own,
                                            top=(key == "task"))}
                yield sse({"jsonrpc": "2.0", "id": id_, "result": result})
        except _NoResponse as e:
            yield sse(self._invalid_body(id_, str(e)[:200], m), error=True)
        finally:
            stop.set()
            conn.close()

    def _stream_result(self, actor: str, result: Any, task_id: str | None):
        """Whether one event belongs to the stream's task of `actor`, and the
        task id of the stream."""
        if not isinstance(result, dict) or len(result) != 1 \
                or next(iter(result)) not in STREAM_KEYS \
                or not isinstance(result[next(iter(result))], dict):
            return False, task_id
        key = next(iter(result))
        body = result[key]
        tid = body.get("id") if key == "task" else body.get("taskId")
        ctx = body.get("contextId")
        if key != "message" and not isinstance(tid, str):
            return False, task_id
        if tid is not None and task_id is not None and tid != task_id:
            return False, task_id
        with self.lock:
            if not self._claim(actor, tid, ctx):
                return False, task_id
        return True, tid if tid is not None else task_id

    def _invalid_body(self, id_: Any, why: str, m: dict | None) -> dict:
        reply = self._invalid(why, m)
        reply.body["id"] = id_
        return reply.body

    def _subscribe(self, actor: str, id_: Any, params: Any,
                   closed: Callable[[], bool]) -> Reply:
        """SubscribeToTask for a task of the client: the stream of the agent
        for its task; for a held task of the gateway, comments until the
        hold closes, then that stream or the final task. Not decided and not
        recorded, as GetTask."""
        params, err = self._task_params("SubscribeToTask", id_, params, {"id"})
        if err is not None:
            return err
        tid = params["id"]
        with self.lock:
            t = self.tasks.get(tid)
            if t is not None and t.owner == actor:
                pass
            elif t is None and self.task_owner.get(tid) == actor:
                return self._stream_reply(self._subscribe_upstream(actor, id_, tid, None,
                                                                   closed))
            else:
                return self._task_refused("SubscribeToTask", id_, "not a task of the client")
        return self._stream_reply(self._subscribe_gateway(actor, id_, t, closed))

    def _subscribe_upstream(self, actor: str, id_: Any, upstream: str, own: str | None,
                            closed: Callable[[], bool] = lambda: False):
        with self.lock:
            upstream_id = next(self.upstream_ids)
        text = wire({"jsonrpc": "2.0", "id": upstream_id, "method": "SubscribeToTask",
                     "params": {"id": upstream}})
        yield from self._relay(actor, id_, text, upstream_id, None, own=own,
                               upstream_task=upstream, closed=closed)

    def _subscribe_gateway(self, actor: str, id_: Any, t: "_Task",
                           closed: Callable[[], bool]):
        next_comment = 0.0
        while True:
            with self.lock:
                settled = t.hold_id is None or t.hold_id not in self.held
                upstream = t.upstream_id
                state = t.state
            if settled and (upstream is not None or state != "TASK_STATE_SUBMITTED"
                            or t.hold_id is None):
                break
            if closed() or self.stopped:
                return
            if time.monotonic() >= next_comment:
                yield f": waiting for task {t.id}\n\n".encode("ascii")
                next_comment = time.monotonic() + KEEPALIVE
            time.sleep(0.02)
        if upstream is not None:
            yield from self._subscribe_upstream(actor, id_, upstream, t.id, closed)
        else:
            with self.lock:
                body = self._task_json(t)
            yield sse({"jsonrpc": "2.0", "id": id_, "result": {"task": body}})

    def _own_task(self, actor: str, task_id: str) -> str | None:
        """The id of the agent's task that `task_id` names for `actor`, or
        None when it is not a task of `actor` that reached the agent."""
        t = self.tasks.get(task_id)
        if t is not None:
            return t.upstream_id if t.owner == actor else None
        return task_id if self.task_owner.get(task_id) == actor else None

    def _wait(self, hold_id: str, h: _A2AHeld, closed: Callable[[], bool]) -> Reply | None:
        """A blocking request that is held: wait until the hold closes. A
        client that closes the connection withdraws the request."""
        while not h.done.wait(0.05):
            if closed():
                with self.lock:
                    if self.held.get(hold_id) is h:
                        self._withdraw(hold_id)
                        _log(f"the client closed the connection of the held request "
                             f"{h.id!r} ({hold_id}); it is withdrawn")
                        return None
        kind, rec = h.outcome
        if kind == "allow":
            return self._forward(h.actor, h.id, h.wire, h.upstream_id, h.message)
        if kind == "stopped":
            return self._rpc_error(h.id, INTERNAL_ERROR,
                                   f"Internal error: the gateway stopped while the request "
                                   f"was held ({hold_id})")
        return self._rejected(h.actor, h.id, h.request["params"]["message"].get("contextId"),
                              h.message, rec)

    def _withdraw(self, hold_id: str, events: list | None = None) -> None:
        """Withdraw a held request (G.6); with the lock held. The hold
        stays in `held` until the step closes it, so that the answer comes
        from its closing record: `withdrawn`, or `expired` when its deadline
        passed before the ticker observed it."""
        line: dict[str, Any] = {"t": self.now(), "withdraw": [hold_id]}
        if events:
            line["host_events"] = events
        try:
            self._settle(self._step(line))
        except Exception:       # noqa: BLE001
            self._fail_held()

    # --- holds that close ------------------------------------------------------------

    def _released(self, hold_id: str, h: _A2AHeld, rec: dict) -> None:
        h.outcome = ("allow", rec)
        if h.return_immediately:
            t = self.tasks.get(h.task_id)
            if t is not None:
                t.meta = _result_meta(rec)
            threading.Thread(target=self._forward_released, args=(h,), daemon=True).start()
        h.done.set()

    def _closed_denied(self, hold_id: str, h: _A2AHeld, rec: dict) -> None:
        h.outcome = ("deny", rec)
        if rec.get("outcome") == "withdrawn":
            self._settle_task(h, "TASK_STATE_CANCELED",
                              f"Withdrawn by the client while held ({hold_id}).",
                              _result_meta(rec))
        else:
            self._settle_task(h, "TASK_STATE_REJECTED",
                              denial_text(h.message, rec, "message"), _result_meta(rec))
        h.done.set()

    def _failed_answer(self, hold_id: str, h: _A2AHeld) -> None:
        rec = {"decision": "deny", "reason": "the enforcement point failed (fail-closed)",
               "hold_id": hold_id}
        h.outcome = ("deny", rec)
        self._settle_task(h, "TASK_STATE_REJECTED", denial_text(h.message, rec, "message"),
                          _result_meta(rec))
        h.done.set()

    def _settle_task(self, h: _A2AHeld, state: str, text: str, meta: dict) -> None:
        t = self.tasks.get(h.task_id) if h.task_id else None
        if t is not None:
            t.state, t.text, t.meta, t.timestamp = state, text, meta, self._stamp()

    def _forward_released(self, h: _A2AHeld) -> None:
        """Pass on a released request whose client has the task of the
        gateway (returnImmediately), and keep what the agent answered."""
        try:
            reply = self._forward(h.actor, h.id, h.wire, h.upstream_id, h.message)
        except Exception as e:      # noqa: BLE001 - the task never stays submitted
            _log(f"the released request {h.id!r} failed: {type(e).__name__}: {e}")
            with self.lock:
                self._host_event("forward_failed", reason=f"{type(e).__name__}: {e}"[:200],
                                 message=h.message)
                t = self.tasks.get(h.task_id)
                if t is not None:
                    t.state, t.timestamp = "TASK_STATE_FAILED", self._stamp()
                    t.text = "The request was released and could not be passed on."
            return
        with self.lock:
            t = self.tasks.get(h.task_id)
            if t is None:
                return
            body = reply.body if reply is not None else None
            result = body.get("result") if isinstance(body, dict) else None
            if isinstance(result, dict) and isinstance(result.get("task"), dict):
                t.upstream_id = result["task"]["id"]
                t.state, t.text = "TASK_STATE_WORKING", None
            elif isinstance(result, dict) and isinstance(result.get("message"), dict):
                t.final = result["message"]
                t.state = "TASK_STATE_COMPLETED"
            else:
                err = body.get("error") if isinstance(body, dict) else None
                t.state = "TASK_STATE_FAILED"
                t.text = ("The request was released, and the agent answered with an "
                          f"error: {err.get('message') if isinstance(err, dict) else '-'}")
            t.timestamp = self._stamp()

    # --- the agent ---------------------------------------------------------------------

    def _post(self, text: str) -> bytes:
        """Send one JSON-RPC request to the agent, or its counterpart of the
        HTTP+JSON binding; raises _NotSent or _NoResponse. Returns the
        JSON-RPC response (for the binding, the one its answer stands
        for)."""
        rest_id = None
        if self.upstream_binding == "rest":
            method, target, data, rest_id = self._rest_request(text)
            conn, resp = self._connect(method, target, data, "application/json")
        else:
            conn, resp = self._connect("POST", self.upstream.path or "/",
                                       text.encode("ascii"), "application/json")
        try:
            try:
                data = resp.read(MAX_RESPONSE + 1)
            except (OSError, http.client.HTTPException) as e:
                raise _NoResponse(f"{type(e).__name__}: {e}") from None
            if len(data) > MAX_RESPONSE:
                raise _NoResponse("the response is larger than 16 MiB")
            if rest_id is not None:
                try:
                    answer = A2ARest.to_jsonrpc(rest_id, resp.status, data.decode("utf-8"))
                except UnicodeDecodeError:
                    raise _NoResponse("the response is not UTF-8") from None
                if answer is None:
                    raise _NoResponse(f"HTTP status {resp.status} without an A2A answer")
                return answer.encode("ascii")
            if resp.status != 200:
                raise _NoResponse(f"HTTP status {resp.status}")
            return data
        finally:
            conn.close()

    def _call_agent(self, upstream_id: int, text: str, m: dict | None) -> tuple[dict | None, Reply | None]:
        """Send a request to the agent. Returns (the parsed response, None)
        or (None, the reply for the client) after a failure, which is
        recorded."""
        try:
            raw = self.post_upstream(text)
        except _NotSent as e:
            _log(f"a request was decided but could not be sent to the agent: {e}")
            with self.lock:
                self._host_event("forward_failed", reason=str(e)[:200], message=m)
            return None, Reply(200, _error(None, INTERNAL_ERROR, "Internal error: the "
                                           "request could not be sent to the agent"))
        except Exception as e:      # noqa: BLE001 - _NoResponse and anything else
            return None, self._invalid(f"{type(e).__name__}: {e}"[:200], m)
        try:
            obj, code, why = parse_json(raw.decode("utf-8"))
        except UnicodeDecodeError:
            obj, code, why = None, PARSE_ERROR, "not UTF-8"
        if code is not None or not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0" \
                or obj.get("id") != upstream_id or type(obj.get("id")) is not int \
                or ("result" in obj) == ("error" in obj) \
                or set(obj) - {"jsonrpc", "id", "result", "error"}:
            return None, self._invalid(f"not a JSON-RPC response to the request "
                                       f"({why or 'shape'})", m)
        if "error" in obj:
            err = obj["error"]
            if not isinstance(err, dict) or type(err.get("code")) is not int \
                    or not isinstance(err.get("message"), str):
                return None, self._invalid("an error without code and message", m)
        return obj, None

    def _invalid(self, why: str, m: dict | None) -> Reply:
        _log(f"the agent gave no valid response: {why}")
        with self.lock:
            self._host_event("invalid_response", reason=why, message=m)
        return Reply(200, _error(None, INVALID_AGENT_RESPONSE,
                                 "InvalidAgentResponseError: the agent gave no valid "
                                 "response"))

    def _claim(self, actor: str, task_id: Any, context_id: Any) -> bool:
        """Record the task and context that the agent gives `actor`; False
        when one of them is another actor's."""
        for table, value in ((self.task_owner, task_id), (self.context_owner, context_id)):
            if value is None:
                continue
            if not isinstance(value, str) or table.get(value, actor) != actor \
                    or value in self.tasks:
                return False
        if task_id is not None:
            self.task_owner[task_id] = actor
        if context_id is not None:
            self.context_owner[context_id] = actor
        return True

    def _forward(self, actor: str, id_: Any, text: str, upstream_id: int,
                 m: dict) -> Reply:
        """Pass on an allowed or released SendMessage; the answer of the
        agent goes to the client with the client's id."""
        obj, failed = self._call_agent(upstream_id, text, m)
        if failed is not None:
            failed.body["id"] = id_
            failed.headers = self._headers()
            return failed
        if "error" in obj:
            return self._rpc({"jsonrpc": "2.0", "id": id_, "error": obj["error"]})
        result = obj["result"]
        ok = isinstance(result, dict) and len(result) == 1
        if ok and isinstance(result.get("task"), dict):
            task = result["task"]
            ok = isinstance(task.get("id"), str)
            with self.lock:
                ok = ok and self._claim(actor, task.get("id"), task.get("contextId"))
        elif ok and isinstance(result.get("message"), dict):
            msg = result["message"]
            with self.lock:
                ok = self._claim(actor, msg.get("taskId"), msg.get("contextId"))
        else:
            ok = False
        if not ok:
            reply = self._invalid("the result is not a task or message of this client", m)
            reply.body["id"] = id_
            reply.headers = self._headers()
            return reply
        return self._rpc({"jsonrpc": "2.0", "id": id_, "result": result})

    # --- GetTask and CancelTask ----------------------------------------------------------

    def _task_params(self, method: str, id_: Any, params: Any,
                     keys: set) -> tuple[dict | None, Reply | None]:
        if isinstance(params, dict) and "tenant" in params:
            return None, self._rpc_error(id_, INVALID_PARAMS, "Invalid params: tenant is "
                                                              "not supported by the CADL "
                                                              "gateway")
        if not isinstance(params, dict) or not isinstance(params.get("id"), str) \
                or set(params) - keys:
            return None, self._rpc_error(id_, INVALID_PARAMS, f"Invalid params: {method} "
                                         f"has id and optionally {', '.join(sorted(keys - {'id'}))}")
        h = params.get("historyLength")
        if "historyLength" in params and (not isinstance(h, int) or isinstance(h, bool)
                                          or h < 0):
            return None, self._rpc_error(id_, INVALID_PARAMS, "Invalid params: historyLength")
        if "metadata" in params and not isinstance(params["metadata"], dict):
            return None, self._rpc_error(id_, INVALID_PARAMS, "Invalid params: metadata")
        return params, None

    def _task_call(self, actor: str, id_: Any, method: str, params: dict,
                   upstream: str, own: str) -> Reply:
        """Pass GetTask or CancelTask on to the agent for the agent's task
        `upstream`, shown to the client as `own`."""
        p = dict(params, id=upstream)
        with self.lock:
            upstream_id = next(self.upstream_ids)
        try:
            text = wire({"jsonrpc": "2.0", "id": upstream_id, "method": method, "params": p})
        except (ValueError, TypeError) as e:
            return self._rpc_error(id_, INVALID_PARAMS, f"Invalid params: {e}")
        obj, failed = self._call_agent(upstream_id, text, None)
        if failed is not None:
            failed.body["id"] = id_
            failed.headers = self._headers()
            return failed
        if "error" in obj:
            return self._rpc({"jsonrpc": "2.0", "id": id_, "error": obj["error"]})
        task = obj["result"]
        if not isinstance(task, dict) or task.get("id") != upstream:
            reply = self._invalid(f"{method}: the result is not the task asked for", None)
            reply.body["id"] = id_
            reply.headers = self._headers()
            return reply
        if own != upstream:
            task = _rewrite(task, upstream, own)
        return self._rpc({"jsonrpc": "2.0", "id": id_, "result": task})

    def _get_task(self, actor: str, id_: Any, params: Any) -> Reply:
        params, err = self._task_params("GetTask", id_, params,
                                        {"id", "historyLength"})
        if err is not None:
            return err
        tid = params["id"]
        with self.lock:
            t = self.tasks.get(tid)
            if t is not None and t.owner == actor:
                if t.upstream_id is None:
                    return self._rpc({"jsonrpc": "2.0", "id": id_,
                                      "result": self._task_json(t)})
                upstream = t.upstream_id
            elif t is None and self.task_owner.get(tid) == actor:
                upstream = tid
            else:
                return self._task_refused("GetTask", id_, "not a task of the client")
        return self._task_call(actor, id_, "GetTask", params, upstream, tid)

    def _cancel_task(self, actor: str, id_: Any, params: Any) -> Reply:
        params, err = self._task_params("CancelTask", id_, params,
                                        {"id", "metadata"})
        if err is not None:
            return err
        tid = params["id"]
        with self.lock:
            t = self.tasks.get(tid)
            if t is not None and t.owner == actor:
                h = self.held.get(t.hold_id) if t.hold_id else None
                if h is not None:
                    self._withdraw(t.hold_id, [{
                        "event": "task_cancel", "reason": f"gateway task {tid}",
                        "hold_id": t.hold_id,
                        "message": {k: h.message[k] for k in ("from", "to", "name")}}])
                    _log(f"the client cancelled the held request {h.id!r} ({t.hold_id})")
                    return self._rpc({"jsonrpc": "2.0", "id": id_,
                                      "result": self._task_json(t)})
                if t.upstream_id is None:
                    return self._rpc_error(id_, TASK_NOT_CANCELABLE,
                                           "TaskNotCancelableError: the task is not held "
                                           "and was not passed on to the agent")
                upstream = t.upstream_id
            elif t is None and self.task_owner.get(tid) == actor:
                upstream = tid
            else:
                return self._task_refused("CancelTask", id_, "not a task of the client")
            # Recorded before it is passed on: it changes the state of the agent.
            self._host_event("task_cancel", reason=f"agent task {upstream} ({actor})"[:200])
        return self._task_call(actor, id_, "CancelTask", params, upstream, tid)

    # --- end ----------------------------------------------------------------------------

    def stop(self) -> None:
        """End the run: the last line of the trace, the end of the instances,
        the end record of the audit log. Held requests are answered with an
        internal error and stay held in the log."""
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
            if self.held:
                _log(f"{len(self.held)} held request(s) were not answered: "
                     + ", ".join(sorted(self.held)))
            for h in self.held.values():
                h.outcome = ("stopped", None)
                h.done.set()
            self._close_logs()


# === HTTP ======================================================================

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cadl-a2a-gateway"
    sys_version = ""
    timeout = 60

    def log_message(self, format, *args):     # noqa: A002 - no access log
        pass

    def _send(self, reply: Reply) -> None:
        if reply.stream is not None:
            self._send_stream(reply)
            return
        data = b"" if reply.body is None else wire(reply.body).encode("ascii")
        try:
            self.send_response(reply.status)
            for k, v in reply.headers.items():
                self.send_header(k, v)
            if reply.body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    def _send_stream(self, reply: Reply) -> None:
        """An SSE response in chunked transfer coding; when the client goes,
        the stream is closed, which withdraws a held request."""
        stream = reply.stream
        try:
            self.send_response(200)
            for k, v in reply.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            for chunk in stream:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass
        finally:
            stream.close()
        self.close_connection = True

    def _closed(self) -> bool:
        """Whether the client has closed the connection. The gateway reads
        one request per connection, so whatever the client sends while its
        request is held is read here and dropped. Plain TCP: a read that
        ends the connection; a half-close (SHUT_WR) counts as closed. TLS: a read that ends the connection
        (close_notify, or the end of the stream); TLS 1.3 encrypts the
        alert, so the raw bytes do not tell."""
        conn = self.connection
        if isinstance(conn, ssl.SSLSocket):
            old = conn.gettimeout()
            try:
                conn.settimeout(0)
                return conn.recv(1) == b""
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                return False
            except OSError:
                return True
            finally:
                try:
                    conn.settimeout(old)
                except OSError:
                    pass
        try:
            s = socket.fromfd(conn.fileno(), conn.family, socket.SOCK_STREAM)
        except OSError:
            return True
        try:
            s.setblocking(False)
            for _ in range(256):            # at most 1 MiB in one look
                if s.recv(4096) == b"":
                    return True             # closed, or half-closed (SHUT_WR)
            return False
        except BlockingIOError:
            return False
        except OSError:
            return True
        finally:
            s.close()

    def setup(self):
        super().setup()
        self._rlock = threading.Lock()
        self._reading = True
        self._reader_slot = False
        self._address_key = None
        self._prefix_key = None
        self.peer = None
        self._timer = self.server.deadlines.add(self.server.read_timeout, self._read_expired)

    def _read_expired(self) -> None:
        """The request was not read within the deadline: closed, and
        recorded at the capped rate (`read_timeout`)."""
        with self._rlock:
            if not self._reading:
                return
            self._reading = False
        self.server.gw.refused("read_timeout", "deadline")
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _done_reading(self) -> bool:
        """The request has been read: the deadline and the reader slot end.
        False when the deadline had already closed the connection."""
        with self._rlock:
            in_time = self._reading
            self._reading = False
            self._timer.cancel()
            if self._reader_slot:
                self._reader_slot = False
                self.server.readers.release()
            srv = self.server
            with srv.address_lock:
                for attr, table in (("_address_key", srv.address_count),
                                    ("_prefix_key", srv.prefix_count)):
                    k = getattr(self, attr)
                    if k is not None:
                        n = table.get(k, 1) - 1
                        if n > 0:
                            table[k] = n
                        else:
                            table.pop(k, None)
                        setattr(self, attr, None)
            return in_time

    def _refuse(self, why: str) -> None:
        """503 before the request is read, recorded at the capped rate."""
        self._done_reading()
        self.server.gw.refused("overloaded", why)
        try:
            self.connection.settimeout(2)
            self.connection.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                    b"Retry-After: 1\r\nContent-Length: 0\r\n"
                                    b"Connection: close\r\n\r\n")
        except OSError:
            pass

    def parse_request(self) -> bool:
        """The request line and the headers, refused with 400 when a header
        line is malformed: whitespace before the colon (which the parser of
        the standard library takes as the end of the headers, so that a
        following `Transfer-Encoding` would be lost), a folded line, or a
        bare carriage return."""
        if not super().parse_request():
            return False
        h = self.headers
        bad = bool(h.defects) or bool(h.get_payload()) or any(
            any(c in k for c in " \t\r\n") or "\r" in v or "\n" in v
            for k, v in h.raw_items())
        if bad:
            self.close_connection = True
            self.send_error(400, "malformed header line (whitespace before a colon, "
                                 "a folded line or a bare carriage return)")
            return False
        return True

    def handle(self):
        """At most `max_readers_per_address` connections of one remote
        address, `max_readers_per_prefix` of one IPv4 /24 or IPv6 /48, and
        `max_readers` in all are being read at the same time. Beyond the
        first two the connection gets 503 at once (`overloaded`, `address`
        or `prefix`); when all readers are busy it waits up to
        `READER_WAIT` seconds for one, then gets 503 (`readers`)."""
        srv = self.server
        key = srv.peer_key(self.client_address)
        prefix = srv.prefix_key(self.client_address)
        with srv.address_lock:
            if srv.address_count.get(key, 0) >= srv.max_readers_per_address:
                full = "address"
            elif srv.prefix_count.get(prefix, 0) >= srv.max_readers_per_prefix:
                full = "prefix"
            else:
                full = None
                srv.address_count[key] = srv.address_count.get(key, 0) + 1
                srv.prefix_count[prefix] = srv.prefix_count.get(prefix, 0) + 1
                self._address_key, self._prefix_key = key, prefix
        if full is not None:
            if srv.mtls:
                self._drop("overloaded", full)
            else:
                self._refuse(full)
            return
        if srv.mtls and not self._mutual_tls():
            return
        if not srv.readers.acquire(timeout=READER_WAIT):
            self._refuse("readers")
            return
        self._reader_slot = True
        try:
            super().handle()
        finally:
            self._done_reading()

    def _drop(self, event: str, why: str) -> None:
        """Close a connection before its TLS handshake has ended, recorded
        at the capped rate; nothing can be answered on it. Not recorded
        again when the read deadline has closed it (`read_timeout`)."""
        if self._done_reading():
            self.server.gw.refused(event, why)

    def _mutual_tls(self) -> bool:
        """The TLS handshake of a connection to a gateway with --client-ca,
        before the connection takes a reader slot: the client has to present
        a certificate of the CA whose subject has exactly one common name,
        which is an actor of the gateway (`cert_actors`). The handshake runs
        under the read deadline and takes one of the `max_handshakes`
        places. False when the connection was dropped."""
        srv = self.server
        if not srv.handshakes.acquire(timeout=READER_WAIT):
            self._drop("overloaded", "handshakes")
            return False
        try:
            try:
                self.connection.do_handshake()
            except ssl.SSLError as e:
                self._drop("handshake_failed", _tls_reason(e))
                return False
            except OSError as e:
                self._drop("handshake_failed", type(e).__name__)
                return False
        finally:
            srv.handshakes.release()
        actor = certificate_actor(self.connection.getpeercert())
        if actor is None or actor not in srv.gw.cert_actors():
            self._drop("handshake_failed", "unknown_certificate")
            return False
        self.peer = actor
        return True

    def do_GET(self):       # noqa: N802
        self._request("GET")

    def do_DELETE(self):    # noqa: N802
        self._request("DELETE")

    def do_POST(self):      # noqa: N802
        self._request("POST")

    def _request(self, method: str) -> None:
        """Read one request: the body of `Content-Length` (none without the
        header, and none for GET and DELETE), never `Transfer-Encoding`."""
        gw = self.server.gw
        bad = None
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get_all("Transfer-Encoding"):
            bad = (501, "Transfer-Encoding is not supported; send Content-Length")
        elif len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]{1,9}",
                                                               lengths[0].strip())):
            bad = (411, "one Content-Length is required")
        elif lengths and int(lengths[0]) > MAX_BODY:
            bad = (413, "the body is larger than 1 MiB")
        elif lengths and int(lengths[0]) and method != "POST":
            bad = (400, f"a {method} request has no body")
        elif not lengths and method == "POST" and self.path == "/":
            bad = (411, "one Content-Length is required")
        if bad is not None:
            self._done_reading()
            self._send(Reply(bad[0], _error(None, INVALID_REQUEST,
                                            f"Invalid Request: {bad[1]}"),
                             {"CADL-Run-Id": gw.run_id}))
            return
        n = int(lengths[0]) if lengths else 0
        try:
            body = self.rfile.read(n)
        except OSError:
            self.close_connection = True
            return
        if len(body) != n:
            self.close_connection = True
            return
        if not self._done_reading():        # the deadline closed the connection
            self.close_connection = True
            return
        try:
            reply = gw.handle_request(method, self.path, self.headers, body, self._closed,
                                      self.peer)
        except Exception as e:      # noqa: BLE001
            _log(f"internal error on a request: {type(e).__name__}: {e}")
            reply = Reply(500, _error(None, INTERNAL_ERROR, "Internal error"),
                          {"CADL-Run-Id": gw.run_id})
        if reply is None:
            self.close_connection = True
            return
        self._send(reply)


class _Deadlines:
    """The read deadlines of all connections in one thread and one heap,
    in place of a timer thread per connection. `add` returns an entry
    whose `cancel` makes it never fire."""

    class Entry:
        __slots__ = ("cancelled",)

        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    def __init__(self) -> None:
        import heapq
        self._heapq = heapq
        self._heap: list = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._stopped = False
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, seconds: float, fn: Callable[[], None]) -> "_Deadlines.Entry":
        entry = self.Entry()
        with self._cond:
            self._heapq.heappush(self._heap, (time.monotonic() + seconds, next(self._seq),
                                              entry, fn))
            self._cond.notify()
        return entry

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stopped and (not self._heap or
                                             self._heap[0][0] > time.monotonic()):
                    wait = None if not self._heap else self._heap[0][0] - time.monotonic()
                    self._cond.wait(wait)
                if self._stopped:
                    return
                _, _, entry, fn = self._heapq.heappop(self._heap)
            if not entry.cancelled:
                try:
                    fn()
                except Exception as e:      # noqa: BLE001
                    _log(f"internal error at a read deadline: {type(e).__name__}: {e}")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, gw, family, max_readers: int = MAX_READERS,
                 read_timeout: float = READ_TIMEOUT,
                 max_readers_per_address: int = MAX_READERS_PER_ADDRESS,
                 max_readers_per_prefix: int = MAX_READERS_PER_PREFIX,
                 mtls: bool = False, max_handshakes: int = MAX_HANDSHAKES):
        self.address_family = family
        self.gw = gw
        self.mtls = mtls
        self.handshakes = threading.BoundedSemaphore(max_handshakes)
        self.readers = threading.BoundedSemaphore(max_readers)
        self.read_timeout = read_timeout
        self.max_readers_per_address = max_readers_per_address
        self.max_readers_per_prefix = max_readers_per_prefix
        self.address_count: dict = {}
        self.prefix_count: dict = {}
        self.address_lock = threading.Lock()
        self.deadlines = _Deadlines()
        super().__init__(address, _Handler)

    @staticmethod
    def peer_key(client_address) -> Any:
        """The remote address that the cap per address counts: an IPv4
        address, or the /64 prefix of an IPv6 address (a single host
        usually owns a whole /64). An IPv4-mapped IPv6 address counts as
        its IPv4 address."""
        host = client_address[0]
        try:
            ip = ipaddress.ip_address(str(host).split("%", 1)[0])
        except ValueError:
            return host
        if ip.version == 6:
            if ip.ipv4_mapped is not None:
                return str(ip.ipv4_mapped)
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
        return str(ip)

    @staticmethod
    def prefix_key(client_address) -> Any:
        """The network that the cap per prefix counts: the /24 of an IPv4
        address (also IPv4-mapped), the /48 of an IPv6 address."""
        host = client_address[0]
        try:
            ip = ipaddress.ip_address(str(host).split("%", 1)[0])
        except ValueError:
            return host
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        bits = 24 if ip.version == 4 else 48
        return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))

    def server_close(self):
        self.deadlines.stop()
        super().server_close()

    def handle_error(self, request, client_address):
        _log(f"a connection failed: {sys.exc_info()[0].__name__}: {sys.exc_info()[1]}")


def certificate_actor(cert: dict | None) -> str | None:
    """The actor that a verified client certificate names: the one common
    name of its subject, when it is a concrete actor; None otherwise."""
    if not cert:
        return None
    try:
        names = [value for rdn in cert.get("subject", ()) for key, value in rdn
                 if key == "commonName"]
    except (TypeError, ValueError):
        return None
    if len(names) != 1:
        return None
    who = G.parse_concrete_actor(names[0])
    return who["text"] if who and who["text"] == names[0] else None


def _tls_reason(e: ssl.SSLError) -> str:
    """A short reason for a failed handshake, for the log."""
    reason = getattr(e, "reason", None) or type(e).__name__
    return str(reason)[:80].lower()


def parse_listen(text: str) -> tuple[str, int]:
    m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:([0-9]{1,5})|([^:\[\]]+):([0-9]{1,5})", text)
    if m is None:
        raise GatewayError(f"--listen {text!r} is HOST:PORT or [IPV6]:PORT")
    host = m.group(1) or m.group(3)
    port = int(m.group(2) or m.group(4))
    if port > 65535:
        raise GatewayError(f"--listen: port {port} is out of range")
    return host, port


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(gw_args: dict, listen: str = "127.0.0.1:8470", port_file: Path | None = None,
          tls_cert: Path | None = None, tls_key: Path | None = None,
          poll: float = 0.05, max_readers: int = MAX_READERS,
          read_timeout: float = READ_TIMEOUT,
          max_readers_per_address: int = MAX_READERS_PER_ADDRESS,
          max_readers_per_prefix: int = MAX_READERS_PER_PREFIX,
          client_ca: Path | None = None, max_handshakes: int = MAX_HANDSHAKES) -> int:
    """Run the gateway until SIGTERM or SIGINT. Returns the exit status."""
    host, port = parse_listen(listen)
    if not _is_loopback(host) and tls_cert is None:
        raise GatewayError(f"--listen {listen} is not a loopback address; give --tls-cert "
                           f"and --tls-key (the bodies are not encrypted otherwise)")
    if (tls_cert is None) != (tls_key is None):
        raise GatewayError("--tls-cert and --tls-key go together")
    if client_ca is not None and tls_cert is None:
        raise GatewayError("--client-ca needs --tls-cert and --tls-key")
    context = None
    if tls_cert is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(tls_cert, tls_key)
        except (OSError, ssl.SSLError) as e:
            raise GatewayError(f"cannot load the TLS certificate: {e}") from None
        if client_ca is not None:
            # Mutual TLS: a client without a certificate of the CA fails the
            # handshake, before it can take a reader slot.
            context.verify_mode = ssl.CERT_REQUIRED
            try:
                context.load_verify_locations(cafile=str(client_ca))
            except (OSError, ssl.SSLError) as e:
                raise GatewayError(f"cannot load --client-ca: {e}") from None
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        socket.getaddrinfo(host, port, family)
    except OSError as e:
        raise GatewayError(f"--listen {listen}: {e}") from None
    gw = A2AGateway(**gw_args)
    try:
        server = _Server((host, port), gw, family, max_readers, read_timeout,
                         max_readers_per_address, max_readers_per_prefix,
                         mtls=client_ca is not None, max_handshakes=max_handshakes)
    except OSError as e:
        gw.stop()
        raise GatewayError(f"cannot listen on {listen}: {e}") from None
    if context is not None:
        # The handshake happens in the thread of the connection, so that a
        # client that does not finish it does not stop the others.
        server.socket = context.wrap_socket(server.socket, server_side=True,
                                            do_handshake_on_connect=False)
    actual = server.server_address[1]
    scheme = "https" if context is not None else "http"
    shown = f"[{host}]" if ":" in host else host
    if port_file is not None:
        tmp = Path(str(port_file) + f".{secrets.token_hex(4)}.tmp")
        tmp.write_text(f"{actual}\n", encoding="utf-8")
        os.replace(tmp, port_file)
    _log(f"listening on {scheme}://{shown}:{actual}/ (run {gw.run_id})")
    done = threading.Event()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()

    def ticker() -> None:
        while not done.wait(poll):
            try:
                gw.tick()
            except Exception as e:      # noqa: BLE001
                _log(f"internal error in the timer: {type(e).__name__}: {e}")
            if gw.failed is not None:
                done.set()

    threading.Thread(target=ticker, daemon=True).start()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: done.set())
        signal.signal(signal.SIGINT, lambda *_: done.set())
    try:
        done.wait()
    except KeyboardInterrupt:
        pass
    done.set()
    gw.stop()
    server.shutdown()
    server.server_close()
    return 1 if gw.failed is not None else 0


def cli_gateway(args) -> int:
    try:
        key = read_key(args.audit_key) if args.audit_key is not None else None
        ring = read_keyring(args.approver_keys) if args.approver_keys is not None else None
        clients = read_keyring(args.client_keys, "--client-keys")
        init = None
        if args.init is not None:
            try:
                init = loads_unique(Path(args.init).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise GatewayError(f"cannot read --init {args.init}: {e}") from None
        card = None
        if args.agent_card is not None:
            try:
                card = loads_unique(Path(args.agent_card).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise GatewayError(f"cannot read --agent-card {args.agent_card}: {e}") \
                    from None
            if not isinstance(card, dict):
                raise GatewayError("--agent-card is a JSON object")
        token = None
        if args.upstream_token is not None:
            try:
                token = Path(args.upstream_token).read_text(encoding="utf-8").rstrip("\n")
            except (OSError, UnicodeDecodeError) as e:
                raise GatewayError(f"cannot read --upstream-token: {e}") from None
        trace_out = args.trace_out or Path(str(args.audit)[:-len(".jsonl")]
                                           if str(args.audit).endswith(".jsonl")
                                           else str(args.audit)).with_suffix(".trace.jsonl")
        if Path(trace_out).resolve() == Path(args.audit).resolve():
            raise GatewayError("the trace and the audit log are the same file")
        if args.poll <= 0 or args.upstream_timeout <= 0 or args.read_timeout <= 0 \
                or min(args.max_handlers, args.max_readers, args.max_readers_per_address,
                       args.max_readers_per_prefix, args.max_handshakes,
                       args.max_handlers_per_actor, args.max_holds_per_actor) < 1:
            raise GatewayError("--poll, --upstream-timeout, --read-timeout and the --max-* "
                               "options must be positive")
        gw_args = dict(file=args.file, agent_actor=args.agent_actor, upstream=args.upstream,
                       client_keys=clients, audit=args.audit, trace_out=trace_out, key=key,
                       inbox=args.approvals, init=init, approver_keys=ring,
                       hold_key=read_key(args.hold_key) if args.hold_key else None,
                       insecure_approvals=args.insecure_approvals, upstream_token=token,
                       upstream_timeout=args.upstream_timeout, agent_card=card,
                       max_handlers=args.max_handlers,
                       max_handlers_per_actor=args.max_handlers_per_actor,
                       max_holds_per_actor=args.max_holds_per_actor,
                       upstream_binding=args.upstream_binding)
        code = serve(gw_args, listen=args.listen, port_file=args.port_file,
                     tls_cert=args.tls_cert, tls_key=args.tls_key, poll=args.poll,
                     max_readers=args.max_readers, read_timeout=args.read_timeout,
                     max_readers_per_address=args.max_readers_per_address,
                     max_readers_per_prefix=args.max_readers_per_prefix,
                     client_ca=args.client_ca, max_handshakes=args.max_handshakes)
    except (GatewayError, AuditKeyError, TraceError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)
    return code     # not reached
