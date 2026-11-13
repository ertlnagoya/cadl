"""The HTTP+JSON binding of A2A 1.0.0 at `cadl a2a-gateway` (Appendix G,
G.13): the routes of the binding, its errors, and the translation between
it and the JSON-RPC requests and responses that the gateway decides and
passes on.

A2A 1.0.0 (https://a2a-protocol.org/v1.0.0/specification/):

- 11.3 gives the routes: ``POST /message:send``, ``POST /message:stream``,
  ``GET /tasks/{id}``, ``GET /tasks``, ``POST /tasks/{id}:cancel``,
  ``POST /tasks/{id}:subscribe``, the push notification configurations
  under ``/tasks/{id}/pushNotificationConfigs``, and
  ``GET /extendedAgentCard``. The normative ``a2a.proto`` (10.1) maps
  ``SubscribeToTask`` to ``GET /tasks/{id}:subscribe``; the gateway takes
  both, as the `a2a-sdk` server does.
- 11.4: bodies are the JSON of the Protocol Buffer messages, the same
  objects as the ``params`` and ``result`` of the JSON-RPC binding.
- 11.5: a ``GET`` carries its parameters in the path and the query, with
  the camelCase names of the fields (``historyLength``).
- 11.6 and 5.4: an error is a ``google.rpc.Status`` object with the HTTP
  status of 5.4 and a ``google.rpc.ErrorInfo`` whose ``reason`` is the A2A
  error in UPPER_SNAKE_CASE without the suffix ``Error`` and whose
  ``domain`` is ``a2a-protocol.org``.
- 11.7: a stream is Server-Sent Events whose ``data`` are
  ``StreamResponse`` objects.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from .gateway_core import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    parse_json,
    wire,
)

ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
DOMAIN = "a2a-protocol.org"
MEDIA_TYPES = ("application/json", "application/a2a+json")

# JSON-RPC code -> (HTTP status, gRPC status name, ErrorInfo reason): 5.4.
CODES: dict[int, tuple[int, str, str]] = {
    -32001: (404, "NOT_FOUND", "TASK_NOT_FOUND"),
    -32002: (400, "FAILED_PRECONDITION", "TASK_NOT_CANCELABLE"),
    -32003: (400, "FAILED_PRECONDITION", "PUSH_NOTIFICATION_NOT_SUPPORTED"),
    -32004: (400, "FAILED_PRECONDITION", "UNSUPPORTED_OPERATION"),
    -32005: (400, "INVALID_ARGUMENT", "CONTENT_TYPE_NOT_SUPPORTED"),
    -32006: (500, "INTERNAL", "INVALID_AGENT_RESPONSE"),
    -32007: (400, "FAILED_PRECONDITION", "EXTENDED_AGENT_CARD_NOT_CONFIGURED"),
    -32008: (400, "FAILED_PRECONDITION", "EXTENSION_SUPPORT_REQUIRED"),
    -32009: (400, "FAILED_PRECONDITION", "VERSION_NOT_SUPPORTED"),
    INVALID_PARAMS: (400, "INVALID_ARGUMENT", "INVALID_PARAMS"),
    INVALID_REQUEST: (400, "INVALID_ARGUMENT", "INVALID_REQUEST"),
    PARSE_ERROR: (400, "INVALID_ARGUMENT", "INVALID_REQUEST"),
    METHOD_NOT_FOUND: (404, "NOT_FOUND", "METHOD_NOT_FOUND"),
    INTERNAL_ERROR: (500, "INTERNAL", "INTERNAL_ERROR"),
}
# ErrorInfo reason -> JSON-RPC code, for the answers of an agent.
REASONS: dict[str, int] = {reason: code for code, (_, _, reason) in CODES.items()
                           if code != PARSE_ERROR}
# HTTP statuses of the gateway that have no JSON-RPC code of their own.
_PLAIN = {401: "UNAUTHENTICATED", 404: "NOT_FOUND", 411: "INVALID_ARGUMENT",
          413: "INVALID_ARGUMENT", 415: "INVALID_ARGUMENT", 501: "UNIMPLEMENTED",
          503: "UNAVAILABLE"}

_ID_RE = re.compile(r"[^\x00-\x20\x7f/]{1,256}")


def error(code: int, message: str, status: int | None = None) -> tuple[int, dict]:
    """The HTTP status and the body of an error of the JSON-RPC code `code`.
    With `status` (401, 415, 503), the error is one of HTTP, not of A2A."""
    if status is not None and status != 200 and status in _PLAIN:
        return status, {"error": {"code": status, "status": _PLAIN[status],
                                  "message": message}}
    http, grpc, reason = CODES.get(code, (500, "INTERNAL", "INTERNAL_ERROR"))
    info: dict[str, Any] = {"@type": ERROR_INFO, "reason": reason, "domain": DOMAIN}
    if code not in CODES:
        info["metadata"] = {"code": str(code)}
    return http, {"error": {"code": http, "status": grpc, "message": message,
                            "details": [info]}}


def error_from_rest(status: int, body: Any) -> dict | None:
    """The JSON-RPC error of an error answer of an agent (11.6): a
    `google.rpc.Status` with an ErrorInfo of the domain of A2A whose reason
    is known. None for anything else."""
    if not isinstance(body, dict) or set(body) != {"error"} \
            or not isinstance(body["error"], dict):
        return None
    err = body["error"]
    details = err.get("details")
    if not isinstance(err.get("message"), str) or not isinstance(details, list):
        return None
    for d in details:
        if isinstance(d, dict) and d.get("@type") == ERROR_INFO:
            reason = d.get("reason")
            if d.get("domain") != DOMAIN or reason not in REASONS:
                return None
            return {"code": REASONS[reason], "message": err["message"]}
    return None


def segment(text: str) -> str | None:
    """A percent-decoded path segment that names a task or a configuration,
    or None."""
    try:
        value = unquote(text, errors="strict")
    except UnicodeDecodeError:
        return None
    return value if _ID_RE.fullmatch(value) else None


def route(method: str, target: str) -> tuple[str, dict] | None:
    """The operation of a request of the binding and the parameters in its
    path (`id`, `taskId`), or None for a path that the binding does not
    have. The query is left to the operation."""
    path = urlsplit(target).path
    if not target.startswith("/") or "//" in path:
        return None
    parts = path[1:].split("/")
    if parts == ["message:send"] and method == "POST":
        return "SendMessage", {}
    if parts == ["message:stream"] and method == "POST":
        return "SendStreamingMessage", {}
    if parts == ["tasks"] and method == "GET":
        return "ListTasks", {}
    if parts == ["extendedAgentCard"] and method == "GET":
        return "GetExtendedAgentCard", {}
    if len(parts) == 2 and parts[0] == "tasks":
        seg = parts[1]
        for verb, op, methods in ((":cancel", "CancelTask", ("POST",)),
                                  (":subscribe", "SubscribeToTask", ("POST", "GET"))):
            if seg.endswith(verb) and method in methods:
                tid = segment(seg[:-len(verb)])
                return (op, {"id": tid}) if tid is not None else None
        if method == "GET":
            tid = segment(seg)
            return ("GetTask", {"id": tid}) if tid is not None else None
        return None
    if len(parts) in (3, 4) and parts[0] == "tasks" and parts[2] == "pushNotificationConfigs":
        tid = segment(parts[1])
        if tid is None:
            return None
        if len(parts) == 3:
            op = {"POST": "CreateTaskPushNotificationConfig",
                  "GET": "ListTaskPushNotificationConfigs"}.get(method)
            return (op, {"taskId": tid}) if op else None
        cid = segment(parts[3])
        op = {"GET": "GetTaskPushNotificationConfig",
              "DELETE": "DeleteTaskPushNotificationConfig"}.get(method)
        return (op, {"taskId": tid, "id": cid}) if op and cid is not None else None
    return None


def query(target: str, allowed: dict[str, str]) -> tuple[dict | None, str | None]:
    """The query parameters of a request: each at most once, only the names
    of `allowed` (name -> "int" or "str"); an "int" is a decimal integer of
    at most nine digits, a "str" printable ASCII of at most 256 characters.
    Returns (parameters, None) or (None, the error text)."""
    text = urlsplit(target).query
    if not text:
        return {}, None
    try:
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                          errors="strict")
    except (ValueError, UnicodeDecodeError):
        return None, "the query is malformed"
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k not in allowed:
            return None, f"the query parameter {k[:40]!r} is not supported"
        if k in out:
            return None, f"the query parameter {k} is given twice"
        if allowed[k] == "str":
            if not re.fullmatch(r"[\x20-\x7e]{0,256}", v):
                return None, f"{k} is printable ASCII"
            out[k] = v
            continue
        if not re.fullmatch(r"[0-9]{1,9}", v):
            return None, f"{k} is a non-negative decimal integer"
        out[k] = int(v)
    return out, None


def path_of(base: str, op: str, params: dict) -> tuple[str, str, dict | None]:
    """The HTTP method, the request target and the body of a JSON-RPC
    request of the gateway to an agent of the binding at the path `base`."""
    base = base.rstrip("/")

    def q(value: str) -> str:
        return quote(value, safe="")
    if op == "SendMessage":
        return "POST", f"{base}/message:send", params
    if op == "SendStreamingMessage":
        return "POST", f"{base}/message:stream", params
    if op == "GetTask":
        target = f"{base}/tasks/{q(params['id'])}"
        if "historyLength" in params:
            target += f"?historyLength={int(params['historyLength'])}"
        return "GET", target, None
    if op == "CancelTask":
        return "POST", f"{base}/tasks/{q(params['id'])}:cancel", params
    if op == "SubscribeToTask":
        return "POST", f"{base}/tasks/{q(params['id'])}:subscribe", None
    if op == "CreateTaskPushNotificationConfig":
        return "POST", f"{base}/tasks/{q(params['taskId'])}/pushNotificationConfigs", params
    if op == "DeleteTaskPushNotificationConfig":
        return ("DELETE", f"{base}/tasks/{q(params['taskId'])}/pushNotificationConfigs/"
                          f"{q(params['id'])}", None)
    raise ValueError(f"no route for {op}")


def to_jsonrpc(upstream_id: int, status: int, text: str) -> str | None:
    """The JSON-RPC response that a non-streamed answer of an agent of the
    binding stands for: the body of a 200 as `result`, an error of 11.6 as
    `error`; None when it is neither."""
    obj, code, _ = parse_json(text) if text else ({}, None, None)
    if code is not None:
        return None
    if status == 200:
        return wire({"jsonrpc": "2.0", "id": upstream_id, "result": obj})
    err = error_from_rest(status, obj)
    if err is None:
        return None
    return wire({"jsonrpc": "2.0", "id": upstream_id, "error": err})


def event_to_jsonrpc(upstream_id: int, name: str, data: str) -> str:
    """One SSE event of an agent of the binding as the JSON-RPC response
    that the relay checks: a `StreamResponse` as `result`, an `error`
    event as `error`. Data that is not JSON is passed as it is, so that the
    relay refuses it."""
    obj, code, _ = parse_json(data)
    if code is not None:
        return data
    if name == "error":
        err = error_from_rest(0, obj)
        return wire({"jsonrpc": "2.0", "id": upstream_id,
                     "error": err if err is not None else {"code": "invalid"}})
    return wire({"jsonrpc": "2.0", "id": upstream_id, "result": obj})
