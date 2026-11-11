"""An MCP gateway for the agent governance extension (Appendix G, G.11).

`cadl mcp-gateway` is a stdio proxy between an MCP client, the agent
``ACTOR`` of a CADL file, and an MCP server, the tool server
``TOOLSERVER`` of the file, which the gateway starts as a subprocess. It
speaks MCP's JSON-RPC 2.0 over the stdio transport: one JSON-RPC message
per line, UTF-8, no embedded newline (MCP specification 2026-07-28,
"Transports: stdio",
https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio).
The gateway does not depend on the protocol version: it forwards every
message unchanged, in both directions, with these exceptions.

``tools/call`` (a request from the client) is the message event
``ACTOR -> TOOLSERVER : name`` at the time of its arrival and is decided
by the online runtime (:class:`cadl.agents_runtime.AgentsRuntime`):

- ``allow``: the request is forwarded unchanged; the response of the
  server goes back unchanged.
- ``deny``: the gateway answers with a tool result with ``isError:
  true`` and a text that names the reason, the norms, the fallback and
  the actors to notify. MCP lets a client give such a result to the
  model, which can then send the fallback; a protocol error is meant
  for requests that the model cannot correct (MCP 2026-07-28, "Tools:
  Error Handling").
- ``escalate``: the request is held; the client gets nothing yet, and
  other requests go on. When the hold closes (G.6), an approved and
  released request is forwarded; a rejected, expired or cancelled one,
  or a release that is denied, is answered with a denial as above.

``tools/list`` results are filtered to the tools that the file declares
for the tool server (``--no-filter-tools`` keeps them all); a call of
another tool is denied by G.5 anyway.

Malformed input never reaches the server: a line that is not JSON is
answered with the JSON-RPC error -32700, a batch or a request without a
method with -32600, a ``tools/call`` without a string ``name`` or with a
name that is not a CADL identifier (no message of a file can name it)
with -32602. When deciding fails (an exception of the runtime, or the
audit log or the trace cannot be written), the call is denied and so is
every later call: the enforcement point has failed and is not trusted
again (fail-closed). The audit log of such a run has no end record.

Approvals, rejections and other messages (for example a grant from
another agent) come through the inbox directory (``--approvals DIR``):
`cadl mcp-approve` and `cadl mcp-message` drop a JSON file into it; the
gateway reads the files in the order of their names, decides the message
at that time, and moves the file to ``DIR/done`` or, when it cannot be
read or turned into a message, to ``DIR/invalid`` (with the reason in a
``.why`` file). An unreadable file is never an approval: the hold stays
held until a valid approval, a rejection or its deadline. Whoever can
write into the directory can speak for any actor; protect it with the
permissions of the file system.

Time: the trace time ``t`` is the time since the start of the gateway on
a monotonic clock, in seconds, rounded to milliseconds; the first line
of the trace gives the wall-clock time of ``t = 0`` as ``epoch``. Every
step of the gateway is written to the trace (``--trace-out``) before it
is decided: the first line, every decided message, a line ``{"t": t}``
when the deadline of a hold has passed, and a last line ``{"t": t}``
when the gateway stops. `cadl agents-run` on that trace makes the same
decisions and writes the same audit log, byte for byte.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import agents as G
from .agents_runtime import AgentsRuntime
from .audit_log import AuditChain, AuditKeyError, epoch_text, file_sha256, read_key
from .norms_eval import NormsEvalError, TraceError, load_version

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INIT_KEYS = {"bindings", "request", "values", "quantities", "instances",
             "host_instances", "state"}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Seconds after a deadline at which the expiry is observed: the runtime
# expires a hold at a time strictly later than its deadline.
_AFTER_DEADLINE = 0.001


class GatewayError(ValueError):
    """A usage error: the gateway does not start."""


def _log(text: str) -> None:
    try:
        sys.stderr.write(f"cadl mcp-gateway: {text}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


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


@dataclass
class _Held:
    id: Any
    raw: str
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
                 wall: datetime | None = None) -> None:
        self.file = Path(file)
        self.to_client = to_client
        self.to_server = to_server
        self.inbox = inbox
        self.filter_tools = filter_tools
        self.clock = clock
        self.lock = threading.RLock()
        self.failed: str | None = None
        self.stopped = False
        self.held: dict[str, _Held] = {}
        self.inflight: set = set()           # ids forwarded to the server
        self.list_ids: set = set()           # ids of tools/list requests

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

        for path, what in ((audit, "audit log"), (trace_out, "trace")):
            if Path(path).exists():
                raise GatewayError(f"the {what} {path} exists; the gateway does not "
                                   f"overwrite or extend a log")
        if inbox is not None:
            for sub in ("", "done", "invalid"):
                (Path(inbox) / sub).mkdir(parents=True, exist_ok=True)

        wall = wall or datetime.now(timezone.utc)
        wall = wall.replace(microsecond=wall.microsecond // 1000 * 1000)
        self.epoch = wall.astimezone(timezone.utc)
        self.t0 = clock()
        self.last_t = 0.0
        self.runtime = AgentsRuntime.open(self.sos, path=str(trace_out))
        self.chain = AuditChain(file_sha256(self.file), epoch_text(self.epoch), key)
        self.audit_f = open(audit, "x", encoding="utf-8")
        self.trace_f = open(trace_out, "x", encoding="utf-8")
        self._write(self.audit_f, self.chain.header())
        # Three digits of the fraction always: Python 3.9 reads no other
        # length with `datetime.fromisoformat`.
        stamp = (self.epoch.strftime("%Y-%m-%dT%H:%M:%S")
                 + f".{self.epoch.microsecond // 1000:03d}Z")
        first = {"t": 0, "epoch": stamp, "quantities": []}
        first.update(init or {})
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

    # --- client -> server -------------------------------------------------------

    def client_line(self, text: str) -> None:
        if not text.strip():
            return
        try:
            obj = json.loads(text)
        except ValueError:
            self.send_client(_error(None, PARSE_ERROR, "Parse error"))
            return
        if isinstance(obj, list):
            self.send_client(_error(None, INVALID_REQUEST,
                                    "Invalid Request: JSON-RPC batches are not supported "
                                    "by MCP"))
            return
        if not isinstance(obj, dict):
            self.send_client(_error(None, INVALID_REQUEST, "Invalid Request"))
            return
        if "method" not in obj:
            if "id" in obj and ("result" in obj or "error" in obj):
                self.send_server(text)              # a response to the server
            else:
                self.send_client(_error(obj.get("id") if _valid_id(obj.get("id")) else None,
                                        INVALID_REQUEST, "Invalid Request: no method"))
            return
        method = obj["method"]
        id_ = obj.get("id")
        if not isinstance(method, str) or obj.get("jsonrpc") != "2.0":
            if _valid_id(id_):
                self.send_client(_error(id_, INVALID_REQUEST, "Invalid Request"))
            return
        if method == "tools/call":
            self._call(obj, text)
            return
        if method == "tools/list" and _valid_id(id_):
            with self.lock:
                self.list_ids.add(id_)
        if method == "notifications/cancelled":
            params = obj.get("params")
            rid = params.get("requestId") if isinstance(params, dict) else None
            with self.lock:
                for hold_id, h in list(self.held.items()):
                    if h.id == rid:
                        # The server never saw the request: nothing to cancel there.
                        del self.held[hold_id]
                        _log(f"the client cancelled the held call {rid!r} ({hold_id}); "
                             f"it will not be sent")
                        return
        if _valid_id(id_):
            with self.lock:
                self.inflight.add(id_)
        self.send_server(text)

    def _call(self, obj: dict, raw: str) -> None:
        id_ = obj.get("id")
        if not _valid_id(id_):
            _log("a tools/call without a valid id was dropped (fail-closed)")
            if "id" in obj:
                self.send_client(_error(None, INVALID_REQUEST, "Invalid Request: id"))
            return
        params = obj.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str):
            self.send_client(_error(id_, INVALID_PARAMS, "Invalid params: tools/call "
                                                         "needs a string 'name'"))
            return
        if not _IDENT_RE.match(name):
            self.send_client(_error(id_, INVALID_PARAMS,
                                    f"Unknown tool: {name} (not a tool of the CADL file)"))
            return
        if "arguments" in params and not isinstance(params["arguments"], dict):
            self.send_client(_error(id_, INVALID_PARAMS, "Invalid params: 'arguments' "
                                                         "must be an object"))
            return
        m = {"from": self.agent["text"], "to": self.server["text"], "name": name}
        with self.lock:
            if any(h.id == id_ for h in self.held.values()) or id_ in self.inflight:
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
                self.inflight.add(id_)
                self.send_server(raw)
            elif decision == "escalate":
                self.held[step["hold_id"]] = _Held(id_, raw, m)
                _log(f"tools/call {id_!r} ({m['name']}) is held as {step['hold_id']} "
                     f"for {step['result']['escalate_to']}")
            else:
                self.send_client(denial_result(id_, m, step["result"]))

    def _settle(self, out: dict) -> None:
        """Answer or forward the held calls whose hold this step closed."""
        for rec in out["audit"]:
            if "decision" not in rec or rec.get("outcome") is None:
                continue
            h = self.held.pop(rec["hold_id"], None)
            if h is None:
                continue
            if rec["decision"] == "allow":
                self.inflight.add(h.id)
                self.send_server(h.raw)
            else:
                self.send_client(denial_result(h.id, h.message, rec))

    # --- server -> client ---------------------------------------------------------

    def server_line(self, text: str) -> None:
        if not text.strip():
            return
        try:
            obj = json.loads(text)
        except ValueError:
            _log("the server wrote a line that is not JSON; it was dropped")
            return
        if isinstance(obj, dict) and "method" not in obj and "id" in obj:
            with self.lock:
                self.inflight.discard(obj["id"])
                is_list = obj["id"] in self.list_ids
                self.list_ids.discard(obj["id"])
            if is_list and self.filter_tools:
                result = obj.get("result")
                if isinstance(result, dict) and isinstance(result.get("tools"), list):
                    result["tools"] = [t for t in result["tools"]
                                       if isinstance(t, dict) and t.get("name") in self.tools]
                    text = json.dumps(obj, ensure_ascii=False)
        self.send_client(text)

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

    def _read_inbox(self) -> None:
        names = sorted(p for p in Path(self.inbox).iterdir()
                       if p.is_file() and p.suffix == ".json" and not p.name.startswith("."))
        for path in names:
            with self.lock:
                if self.failed is not None or self.stopped:
                    return
                try:
                    m = self._inbox_message(path)
                except ValueError as e:
                    self._move(path, "invalid", str(e))
                    _log(f"inbox file {path.name} is not used: {e}")
                    continue
                self._move(path, "done")
                try:
                    self._settle(self._step({"t": self.now(), "messages": [m]}))
                except Exception:       # noqa: BLE001
                    self._fail_held()
                    return

    def _move(self, path: Path, where: str, why: str | None = None) -> None:
        target = Path(self.inbox) / where / path.name
        try:
            os.replace(path, target)
            if why is not None:
                target.with_suffix(".why").write_text(why + "\n", encoding="utf-8")
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass

    def _inbox_message(self, path: Path) -> dict:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raise ValueError(f"cannot be read as JSON ({e})") from None
        if not isinstance(obj, dict):
            raise ValueError("not a JSON object")
        kind = obj.get("kind")
        if kind == "approval":
            hold_id, decision, actor = obj.get("hold_id"), obj.get("decision"), obj.get("as")
            if not isinstance(hold_id, str) or not hold_id:
                raise ValueError("no hold_id")
            if decision not in ("approve", "reject"):
                raise ValueError("decision is neither approve nor reject")
            who = G.parse_concrete_actor(actor)
            if who is None:
                raise ValueError(f"'as' is not a concrete actor: {actor!r}")
            event = "approval" if decision == "approve" else "rejection"
            for ev, sender, to, name in self.approval_rules:
                if ev == event and G.ref_match(self.policy, sender, who, {}) is True:
                    return {"from": who["text"], "to": to, "name": name, "args": [hold_id]}
            raise ValueError(f"no counts_as rule of an enforced contract creates {event} "
                             f"from a message of {who['text']} to a concrete receiver")
        if kind == "message":
            m = obj.get("message")
            if not isinstance(m, dict) or set(m) - {"from", "to", "name", "args"}:
                raise ValueError("'message' is an object with from, to, name and args")
            for k in ("from", "to"):
                if G.parse_concrete_actor(m.get(k)) is None:
                    raise ValueError(f"'{k}' is not a concrete actor")
            if not isinstance(m.get("name"), str) or not _IDENT_RE.match(m["name"]):
                raise ValueError("'name' is not an identifier")
            args = m.get("args", [])
            if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
                raise ValueError("'args' is a list of strings")
            return {"from": m["from"], "to": m["to"], "name": m["name"], "args": list(args)}
        raise ValueError("'kind' is neither approval nor message")

    def _fail_held(self) -> None:
        """After a failure, the held calls are denied (fail-closed)."""
        for hold_id, h in list(self.held.items()):
            del self.held[hold_id]
            self.send_client(denial_result(h.id, h.message, {
                "reason": "the enforcement point failed (fail-closed)", "hold_id": hold_id}))

    # --- output and end -------------------------------------------------------------------

    def send_client(self, obj_or_text: Any) -> None:
        text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(
            obj_or_text, ensure_ascii=False)
        with self.lock:
            self.to_client(text)

    def send_server(self, text: str) -> None:
        with self.lock:
            self.to_server(text)

    def server_gone(self) -> None:
        """The server exited: answer every request it will not answer."""
        with self.lock:
            for id_ in sorted(self.inflight, key=str):
                self.send_client(_error(id_, INTERNAL_ERROR,
                                        "Internal error: the MCP server exited"))
            self.inflight.clear()
            for hold_id, h in list(self.held.items()):
                del self.held[hold_id]
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


# === Processes ================================================================

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

    def client_reader() -> None:
        for raw in iter(stdin.readline, b""):
            try:
                gw.client_line(raw.rstrip(b"\r\n").decode("utf-8"))
            except UnicodeDecodeError:
                gw.send_client(_error(None, PARSE_ERROR, "Parse error: not UTF-8"))
            except Exception as e:      # noqa: BLE001
                _log(f"internal error on a client line: {type(e).__name__}: {e}")
        finish(0)

    def server_reader() -> None:
        for raw in iter(proc.stdout.readline, b""):
            try:
                gw.server_line(raw.rstrip(b"\r\n").decode("utf-8"))
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
        init = None
        if args.init is not None:
            try:
                init = json.loads(Path(args.init).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise GatewayError(f"cannot read --init {args.init}: {e}") from None
        trace_out = args.trace_out or Path(str(args.audit)[:-len(".jsonl")]
                                           if str(args.audit).endswith(".jsonl")
                                           else str(args.audit)).with_suffix(".trace.jsonl")
        if Path(trace_out).resolve() == Path(args.audit).resolve():
            raise GatewayError("the trace and the audit log are the same file")
        if args.poll <= 0:
            raise GatewayError("--poll must be positive")
        gw_args = dict(file=args.file, agent=args.agent, server_actor=args.server_actor,
                       audit=args.audit, trace_out=trace_out, key=key,
                       inbox=args.approvals, init=init,
                       filter_tools=not args.no_filter_tools)
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
    """Write a file into the inbox atomically."""
    inbox = Path(inbox)
    if not inbox.is_dir():
        raise GatewayError(f"the inbox {inbox} is not a directory")
    name = f"{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    tmp = inbox / f".{name}.tmp"
    tmp.write_text(json.dumps(obj, ensure_ascii=False) + "\n", encoding="utf-8")
    target = inbox / name
    os.replace(tmp, target)
    return target


def cli_approve(args) -> int:
    if G.parse_concrete_actor(args.actor) is None:
        print(f"Error: --as {args.actor!r} is not a concrete actor", file=sys.stderr)
        return 2
    try:
        path = _drop(args.inbox, {"kind": "approval", "hold_id": args.hold_id,
                                  "decision": args.decision, "as": args.actor})
    except (GatewayError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(path)
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
        path = _drop(args.inbox, {"kind": "message", "message": {
            "from": sender["text"], "to": receiver["text"], "name": ev.name,
            "args": list(args.arg)}})
    except (GatewayError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(path)
    return 0
