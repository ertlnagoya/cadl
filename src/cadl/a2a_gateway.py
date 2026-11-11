"""An A2A gateway for the agent governance extension (Appendix G, G.11).

`cadl a2a-gateway` is an HTTP server in front of one A2A agent, the actor
``Y`` of a CADL file (``--agent-actor``), and passes requests on to that
agent (``--upstream URL``). It speaks the JSON-RPC binding of A2A 1.0.0
(https://a2a-protocol.org/v1.0.0/specification/, sections 3, 4, 5.4 and
9): JSON-RPC 2.0 over HTTP POST with ``Content-Type: application/json``,
the methods ``SendMessage``, ``GetTask`` and ``CancelTask``, and the
service parameter ``A2A-Version: 1.0`` as an HTTP header.

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
the same actor, and recorded as ``task_refused``. Streaming
(``SendStreamingMessage``, ``SubscribeToTask``), ``ListTasks``,
``GetExtendedAgentCard`` and push notifications are refused
(``UnsupportedOperationError`` -32004, ``PushNotificationNotSupportedError``
-32003) and recorded as ``method_refused``; any other method gets -32601.

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
    "SendStreamingMessage": UNSUPPORTED_OPERATION,
    "SubscribeToTask": UNSUPPORTED_OPERATION,
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
MAX_NONCES = 1_000_000         # authenticated requests in one run
MAX_HANDLERS = 64              # requests handled at the same time

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
                 max_nonces: int = MAX_NONCES) -> None:
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
        self.upstream_token = upstream_token
        self.upstream_timeout = upstream_timeout
        self.agent_card = agent_card
        self.post_upstream = post_upstream or self._post
        self.client_nonces: set = set()
        self.max_nonces = max_nonces
        self.refusals: dict[str, list] = {}     # event -> [second, count, not recorded]
        self.tasks: dict[str, _Task] = {}          # the tasks of the gateway
        self.task_owner: dict[str, str] = {}       # tasks of the agent -> actor
        self.context_owner: dict[str, str] = {}    # contexts -> actor
        self.upstream_ids = itertools.count(1)
        self._open(audit, trace_out, key, inbox, init, clock, wall, approver_keys,
                   insecure_approvals, run_id)

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
                    closed: Callable[[], bool] = lambda: False) -> Reply | None:
        """One HTTP POST. `headers` has `get_all`; `closed` tells whether
        the client has closed the connection. Returns the reply, or None
        when the client has gone."""
        if self.stopped:
            return Reply(503, None, self._headers())
        if path != "/":
            return Reply(404, None, self._headers())
        actor, why = self._authenticate(headers.get_all("Authorization") or [], path, body)
        if actor is None:
            with self.lock:
                self._unauthenticated(why)
            return Reply(401, _error(None, INVALID_REQUEST, "Unauthorized"),
                         self._headers({"WWW-Authenticate":
                                        f'{AUTH_SCHEME} run="{self.run_id}"'}))
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
        params = obj.get("params")
        if method == "SendMessage":
            return self._send_message(actor, id_, params, closed)
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

    def overloaded(self) -> Reply:
        """A request beyond `MAX_HANDLERS` at the same time: 503."""
        with self.lock:
            self._refusal("overloaded", "handlers")
        return Reply(503, _error(None, INTERNAL_ERROR, "the gateway is busy"),
                     self._headers({"Retry-After": "1"}))

    def _authenticate(self, values: list, path: str, body: bytes) -> tuple[str | None, str]:
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
        want = request_mac(key, who["text"], self.run_id, nonce, "POST", path, body)
        if not hmac.compare_digest(mac, want):
            return None, "bad_mac"
        with self.lock:
            if nonce in self.client_nonces:
                return None, "replayed"
            if len(self.client_nonces) >= self.max_nonces:
                return None, "nonce_limit"
            self.client_nonces.add(nonce)
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
                      closed: Callable[[], bool]) -> Reply | None:
        params, why = self._check_message(params)
        if params is None:
            return self._rpc_error(id_, INVALID_PARAMS, f"Invalid params: {why}")
        conf = params.get("configuration", {})
        if "taskPushNotificationConfig" in conf:
            with self.lock:
                self._host_event("method_refused",
                                 reason="SendMessage: taskPushNotificationConfig")
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
                        return self._task_refused("SendMessage", id_, "taskId")
                    fmsg[k] = up
            if "referenceTaskIds" in msg:
                ups = [self._own_task(actor, x) for x in msg["referenceTaskIds"]]
                if any(u is None for u in ups):
                    return self._task_refused("SendMessage", id_, "referenceTaskIds")
                fmsg["referenceTaskIds"] = ups
            # A context only when the gateway has seen it given to this
            # client, by the agent or by the gateway; never claimed here.
            ctx = msg.get("contextId")
            if ctx is not None and self.context_owner.get(ctx) != actor:
                return self._task_refused("SendMessage", id_, "contextId")
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
        # Everything that can fail is done before the decision.
        with self.lock:
            upstream_id = next(self.upstream_ids)
        try:
            text = wire({"jsonrpc": "2.0", "id": upstream_id, "method": "SendMessage",
                         "params": forwarded})
            params_sha256 = hashlib.sha256(canonical(params)).hexdigest()
        except (ValueError, TypeError) as e:
            return self._rpc_error(id_, INVALID_PARAMS, f"Invalid params: not "
                                                        f"representable in canonical JSON "
                                                        f"({e})")
        immediate = conf.get("returnImmediately") is True
        with self.lock:
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
                h = _A2AHeld(id_, {"jsonrpc": "2.0", "id": id_, "method": "SendMessage",
                                   "params": params}, m, text, params_sha256,
                             actor=actor, upstream_id=upstream_id,
                             return_immediately=immediate)
                self.held[hold_id] = h
                self._publish_hold(hold_id)
                _log(f"SendMessage {id_!r} ({actor} -> {m['to']} : {name}) is held as "
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
            return self._forward(actor, id_, text, upstream_id, m)
        return self._wait(hold_id, h, closed)

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
        """POST one JSON-RPC request to the agent; raises _NotSent or
        _NoResponse."""
        u = self.upstream
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        try:
            conn = cls(u.hostname, u.port, timeout=self.upstream_timeout)
        except Exception as e:      # noqa: BLE001
            raise _NotSent(f"{type(e).__name__}: {e}") from None
        headers = {"Content-Type": "application/json", "A2A-Version": A2A_VERSION,
                   "Accept": "application/json"}
        if self.upstream_token is not None:
            headers["Authorization"] = f"Bearer {self.upstream_token}"
        try:
            try:
                conn.request("POST", u.path or "/", body=text.encode("ascii"),
                             headers=headers)
            except (OSError, ValueError, http.client.HTTPException) as e:
                raise _NotSent(f"{type(e).__name__}: {e}") from None
            try:
                resp = conn.getresponse()
                data = resp.read(MAX_RESPONSE + 1)
            except (OSError, http.client.HTTPException) as e:
                raise _NoResponse(f"{type(e).__name__}: {e}") from None
            if resp.status != 200:
                raise _NoResponse(f"HTTP status {resp.status}")
            if len(data) > MAX_RESPONSE:
                raise _NoResponse("the response is larger than 16 MiB")
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
            self._host_event("task_cancel", reason=f"agent task {upstream} ({actor})")
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

    def do_GET(self):       # noqa: N802
        self._limited(lambda: self._send(self.server.gw.handle_get(self.path)))

    def do_POST(self):      # noqa: N802
        self._limited(self._post)

    def _limited(self, fn) -> None:
        """At most `max_handlers` requests at the same time (a held
        blocking request counts); beyond that 503, recorded as
        `overloaded` at the rate of refused requests."""
        slots = self.server.slots
        if not slots.acquire(blocking=False):
            self._send(self.server.gw.overloaded())
            return
        try:
            fn()
        finally:
            slots.release()

    def _post(self) -> None:
        gw = self.server.gw
        bad = None
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get_all("Transfer-Encoding"):
            bad = (501, "Transfer-Encoding is not supported; send Content-Length")
        elif len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0].strip()):
            bad = (411, "one Content-Length is required")
        elif int(lengths[0]) > MAX_BODY:
            bad = (413, "the body is larger than 1 MiB")
        if bad is not None:
            self._send(Reply(bad[0], _error(None, INVALID_REQUEST,
                                            f"Invalid Request: {bad[1]}"),
                             {"CADL-Run-Id": gw.run_id}))
            return
        n = int(lengths[0])
        try:
            body = self.rfile.read(n)
        except OSError:
            self.close_connection = True
            return
        if len(body) != n:
            self.close_connection = True
            return
        try:
            reply = gw.handle_post(self.path, self.headers, body, self._closed)
        except Exception as e:      # noqa: BLE001
            _log(f"internal error on a request: {type(e).__name__}: {e}")
            reply = Reply(500, _error(None, INTERNAL_ERROR, "Internal error"),
                          {"CADL-Run-Id": gw.run_id})
        if reply is None:
            self.close_connection = True
            return
        self._send(reply)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, gw, family, max_handlers: int = MAX_HANDLERS):
        self.address_family = family
        self.gw = gw
        self.slots = threading.BoundedSemaphore(max_handlers)
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address):
        _log(f"a connection failed: {sys.exc_info()[0].__name__}: {sys.exc_info()[1]}")


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
          poll: float = 0.05, max_handlers: int = MAX_HANDLERS) -> int:
    """Run the gateway until SIGTERM or SIGINT. Returns the exit status."""
    host, port = parse_listen(listen)
    if not _is_loopback(host) and tls_cert is None:
        raise GatewayError(f"--listen {listen} is not a loopback address; give --tls-cert "
                           f"and --tls-key (the bodies are not encrypted otherwise)")
    if (tls_cert is None) != (tls_key is None):
        raise GatewayError("--tls-cert and --tls-key go together")
    context = None
    if tls_cert is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(tls_cert, tls_key)
        except (OSError, ssl.SSLError) as e:
            raise GatewayError(f"cannot load the TLS certificate: {e}") from None
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        socket.getaddrinfo(host, port, family)
    except OSError as e:
        raise GatewayError(f"--listen {listen}: {e}") from None
    gw = A2AGateway(**gw_args)
    try:
        server = _Server((host, port), gw, family, max_handlers)
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
        if args.poll <= 0 or args.upstream_timeout <= 0 or args.max_handlers < 1:
            raise GatewayError("--poll, --upstream-timeout and --max-handlers must be "
                               "positive")
        gw_args = dict(file=args.file, agent_actor=args.agent_actor, upstream=args.upstream,
                       client_keys=clients, audit=args.audit, trace_out=trace_out, key=key,
                       inbox=args.approvals, init=init, approver_keys=ring,
                       insecure_approvals=args.insecure_approvals, upstream_token=token,
                       upstream_timeout=args.upstream_timeout, agent_card=card)
        code = serve(gw_args, listen=args.listen, port_file=args.port_file,
                     tls_cert=args.tls_cert, tls_key=args.tls_key, poll=args.poll,
                     max_handlers=args.max_handlers)
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
