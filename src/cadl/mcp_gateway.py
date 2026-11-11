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
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import agents as G
from .audit_log import AuditKeyError, canonical, loads_unique, read_key
from .gateway_core import (  # noqa: F401 - the names of this module before 0.3.25
    _AFTER_DEADLINE,
    _IDENT_HOLD_RE,
    _IDENT_RE,
    _MAC_RE,
    _NONCE_RE,
    INIT_KEYS,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    EnforcementPoint,
    GatewayError,
    _drop,
    _error,
    _Held,
    _id_key,
    _read_published,
    _Refused,
    _secure_dir,
    _valid_id,
    approval_file,
    cli_approve,
    cli_message,
    denial_text,
    message_mac,
    parse_line,
    read_keyring,
    wire,
)
from .gateway_core import log as _core_log
from .norms_eval import TraceError

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


def _log(text: str) -> None:
    _core_log("cadl mcp-gateway", text)


def denial_result(id_: Any, m: dict | None, rec: dict) -> dict:
    """A tool result with isError: true (MCP, tool execution error)."""
    return {"jsonrpc": "2.0", "id": id_, "result": {
        "resultType": "complete",
        "content": [{"type": "text", "text": denial_text(m, rec)}],
        "isError": True}}


class Gateway(EnforcementPoint):
    """The decisions of the gateway, without its input and output: the
    lines of the client and of the server come in through
    `client_line` and `server_line`, the time through `tick`, and the
    lines to send go out through `to_client` and `to_server`."""

    LOG_PREFIX = "cadl mcp-gateway"

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
        self.to_client = to_client
        self.to_server = to_server
        self.filter_tools = filter_tools
        self.inflight: set = set()           # ids of requests sent to the server
        self.server_requests: set = set()    # ids of server requests sent to the client
        self.list_ids: set = set()           # ids of tools/list requests
        self.allowed = set(ALLOWED_METHODS) | set(allow_methods)
        self.allowed_server = set(ALLOWED_SERVER_NOTIFICATIONS) | set(allow_methods)
        self._load(file)
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
        self.approval_rules = self._approval_rules(self.server, self.agent)
        self._check_approvable(self.server)
        self._open(audit, trace_out, key, inbox, init, clock, wall, approver_keys,
                   insecure_approvals, run_id)

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
        # Everything that can fail is done before the decision: the line to
        # send and the digest of the arguments that an approver signs.
        try:
            text = wire(obj)
            params_sha256 = hashlib.sha256(canonical(params)).hexdigest()
        except (ValueError, TypeError) as e:
            self.send_client(_error(id_, INVALID_PARAMS,
                                    f"Invalid params: not representable in canonical "
                                    f"JSON ({e})"))
            return
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
                self._forward(id_, text, m)
            elif decision == "escalate":
                hold_id = step["hold_id"]
                self.held[hold_id] = _Held(id_, obj, m, text, params_sha256)
                self._publish_hold(hold_id)
                _log(f"tools/call {id_!r} ({m['name']}) is held as {hold_id} "
                     f"for {step['result']['escalate_to']}")
            else:
                self.send_client(denial_result(id_, m, step["result"]))

    def _released(self, hold_id: str, h: _Held, rec: dict) -> None:
        self._forward(h.id, h.wire, h.message)

    def _closed_denied(self, hold_id: str, h: _Held, rec: dict) -> None:
        self.send_client(denial_result(h.id, h.message, rec))

    def _failed_answer(self, hold_id: str, h: _Held) -> None:
        self.send_client(denial_result(h.id, h.message, {
            "reason": "the enforcement point failed (fail-closed)", "hold_id": hold_id}))

    def _forward(self, id_: Any, text: str, m: dict) -> None:
        """Send an allowed or released call. The line was made before the
        decision; if sending still fails, the failure is recorded
        (`forward_failed`), so that the log does not show an effect alone."""
        self.inflight.add(_id_key(id_))
        try:
            self.to_server(text)
        except Exception as e:      # noqa: BLE001
            self.inflight.discard(_id_key(id_))
            _log(f"the call {id_!r} was decided but could not be sent: {e}")
            self.send_client(_error(id_, INTERNAL_ERROR,
                                    "Internal error: the call could not be sent to the "
                                    "server"))
            self._host_event("forward_failed", reason=f"{type(e).__name__}: {e}"[:200],
                             message=m)

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
            self._close_logs()


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

