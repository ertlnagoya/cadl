"""A fake A2A agent for the tests of `cadl a2a-gateway`: the JSON-RPC
binding of A2A 1.0.0 over HTTP, standard library only, run in a thread of
the test. It records every request it receives (method, params and
headers), so that a test can check that a denied or held message never
reached it.

It also speaks the HTTP+JSON binding (A2A 1.0.0, 11) at the routes of
11.3, with the same answers: a request of the binding is recorded under
the name of its JSON-RPC method, with `binding` "rest", and its answer is
the `result` of the JSON-RPC response, an error of 11.6, or SSE events
with `StreamResponse` data.

SendMessage answers with a completed task, or with what the text of the
first part asks for: "reply-with-message" (a Message), "garbage" (not
JSON), "foreign-task" (the task of another client), "slow" (after one
second), "error" (a JSON-RPC error).

Push notifications: the agent keeps the configurations it is given
(CreateTaskPushNotificationConfig, or the configuration of a SendMessage)
and `notify` POSTs a `StreamResponse` to them, with their token and
credentials, as an agent does (A2A 1.0.0, 4.3.3).
"""

from __future__ import annotations

import http.client
import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

_REASONS = {-32001: (404, "TASK_NOT_FOUND"), -32002: (400, "TASK_NOT_CANCELABLE"),
            -32004: (400, "UNSUPPORTED_OPERATION"), -32601: (404, "METHOD_NOT_FOUND")}


def rest_request(method: str, target: str, body: bytes) -> dict | None:
    """The JSON-RPC request that a request of the HTTP+JSON binding stands
    for (id 0), or None."""
    url = urlsplit(target)
    path = url.path
    params = json.loads(body) if body else {}
    if method == "POST" and path == "/message:send":
        return {"id": 0, "method": "SendMessage", "params": params}
    if method == "POST" and path == "/message:stream":
        return {"id": 0, "method": "SendStreamingMessage", "params": params}
    if path.startswith("/tasks/"):
        rest = path[len("/tasks/"):]
        if "/pushNotificationConfigs" in rest:
            tid, _, cid = rest.partition("/pushNotificationConfigs")
            if method == "POST":
                return {"id": 0, "method": "CreateTaskPushNotificationConfig",
                        "params": dict(params, taskId=unquote(tid))}
            if method == "DELETE":
                return {"id": 0, "method": "DeleteTaskPushNotificationConfig",
                        "params": {"taskId": unquote(tid), "id": unquote(cid[1:])}}
            return None
        if rest.endswith(":cancel") and method == "POST":
            return {"id": 0, "method": "CancelTask",
                    "params": dict(params, id=unquote(rest[:-7]))}
        if rest.endswith(":subscribe") and method in ("POST", "GET"):
            return {"id": 0, "method": "SubscribeToTask", "params": {"id": unquote(rest[:-10])}}
        if method == "GET" and "/" not in rest:
            p = {"id": unquote(rest)}
            p.update({k: int(v) for k, v in parse_qsl(url.query)})
            return {"id": 0, "method": "GetTask", "params": p}
    return None


def rest_error(err: dict) -> tuple[int, dict]:
    status, reason = _REASONS.get(err["code"], (500, "INTERNAL_ERROR"))
    return status, {"error": {"code": status, "status": "X", "message": err["message"],
                              "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                           "reason": reason, "domain": "a2a-protocol.org"}]}}


class FakeAgent:
    def __init__(self):
        self.requests: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.push: dict[tuple, dict] = {}       # (task id, config id) -> configuration
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        agent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):       # noqa: N802
                self.do_POST("GET")

            def do_DELETE(self):    # noqa: N802
                self.do_POST("DELETE")

            def _json(self, status: int, obj) -> None:
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def do_POST(self, method="POST"):      # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if self.path != "/":
                    return self._rest(method, raw)
                req = json.loads(raw)
                with agent.lock:
                    agent.requests.append({"method": req.get("method"),
                                           "params": req.get("params"),
                                           "raw": raw.decode("ascii"),
                                           "headers": dict(self.headers)})
                text0 = ((req.get("params") or {}).get("message") or {}).get(
                    "parts", [{}])[0].get("text", "")
                if req.get("method") == "SendStreamingMessage" and text0 == "stream-json-bad":
                    data = (b'{"jsonrpc":"2.0","id":%d,"error":{"code":-1,"message":"\xff"}}'
                            % req["id"])
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if req.get("method") in ("SendStreamingMessage", "SubscribeToTask"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in agent.stream(req):
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except OSError:
                            break
                    self.close_connection = True
                    return
                body = agent.answer(req)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _rest(self, method: str, raw: bytes) -> None:
                req = rest_request(method, self.path, raw)
                with agent.lock:
                    agent.requests.append({"method": req and req["method"],
                                           "params": req and req["params"],
                                           "raw": raw.decode("ascii"), "binding": "rest",
                                           "target": f"{method} {self.path}",
                                           "headers": dict(self.headers)})
                if req is None:
                    return self._json(*rest_error({"code": -32601, "message": "no route"}))
                if req["method"] in ("SendStreamingMessage", "SubscribeToTask"):
                    it = agent.stream(req, rest=True)
                    first = next(it, b"")
                    chunks = itertools.chain([first], it)
                    if first.startswith(b"event: error"):
                        err = json.loads(first.split(b"data: ", 1)[1])["error"]
                        return self._json(*rest_error(err))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in chunks:
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except OSError:
                            break
                    self.close_connection = True
                    return
                body = agent.answer(req)
                if isinstance(body, bytes):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True
                    return
                if "error" in body:
                    return self._json(*rest_error(body["error"]))
                self._json(200, body["result"])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def notify(self, task_id: str, payload: dict, config: dict | None = None,
               headers: dict | None = None, context=None) -> list[int]:
        """POST `payload` to every configuration of `task_id` (or to
        `config`), as the agent's notification; the HTTP statuses. An https
        URL is reached with the TLS client context `context`."""
        with self.lock:
            configs = [config] if config is not None else [
                c for (t, _), c in self.push.items() if t == task_id]
        out = []
        for c in configs:
            url = c["url"]
            hostport, path = url.split("://", 1)[1].split("/", 1)
            host, port = hostport.rsplit(":", 1)
            if url.startswith("https:"):
                conn = http.client.HTTPSConnection(host, int(port), timeout=30,
                                                   context=context)
            else:
                conn = http.client.HTTPConnection(host, int(port), timeout=30)
            h = {"Content-Type": "application/json"}
            if c.get("token"):
                h["X-A2A-Notification-Token"] = c["token"]
            auth = c.get("authentication") or {}
            if auth.get("credentials"):
                h["Authorization"] = f"{auth['scheme']} {auth['credentials']}"
            h.update(headers or {})
            conn.request("POST", "/" + path, body=json.dumps(payload).encode(), headers=h)
            out.append(conn.getresponse().status)
            conn.close()
        return out

    def sent(self, method: str = "SendMessage") -> list[str]:
        """The CADL names of the requests of `method` that reached the agent."""
        out = []
        with self.lock:
            for r in self.requests:
                if r["method"] != method:
                    continue
                if method in ("SendMessage", "SendStreamingMessage"):
                    meta = r["params"]["message"].get("metadata", {})
                    ext = next(iter(meta.values()), {}) if meta else {}
                    out.append(ext.get("name") if isinstance(ext, dict) else None)
                else:
                    out.append(r["params"]["id"])
        return out

    def stream(self, req: dict, rest: bool = False):
        """The SSE events of a streamed request; with `rest`, those of the
        HTTP+JSON binding."""
        id_ = req.get("id")
        params = req.get("params") or {}

        def ev(result):
            if rest:
                return ("data: " + json.dumps(result) + "\n\n").encode()
            return ("data: " + json.dumps({"jsonrpc": "2.0", "id": id_, "result": result})
                    + "\n\n").encode()
        if req["method"] == "SubscribeToTask":
            with self.lock:
                task = self.tasks.get(params.get("id"))
            if task is None:
                yield ("event: error\ndata: " + json.dumps({"jsonrpc": "2.0", "id": id_,
                       "error": {"code": -32001, "message": "TaskNotFoundError"}})
                       + "\n\n").encode()
                return
            yield ev({"task": task})
            yield ev({"statusUpdate": {"taskId": task["id"], "contextId": task["contextId"],
                                       "status": {"state": "TASK_STATE_COMPLETED"}}})
            return
        msg = params["message"]
        text = msg["parts"][0].get("text", "")
        with self.lock:
            n = next(self.ids)
        tid, ctx = f"up-task-{n}", msg.get("contextId", f"up-ctx-{n}")
        task = {"id": tid, "contextId": ctx, "status": {"state": "TASK_STATE_SUBMITTED"}}
        with self.lock:
            self.tasks[tid] = task
            self._keep_push(params, tid)
        yield b": a comment\n\n"
        yield ev({"task": task})
        other = "other-task" if text == "stream-foreign" else tid
        yield ev({"statusUpdate": {"taskId": other, "contextId": ctx,
                                   "status": {"state": "TASK_STATE_WORKING"}}})
        if text == "stream-cr":
            yield b"data: {\"jsonrpc\": \"2.0\"}\r{\"x\": 1}\n\n"
        yield ev({"artifactUpdate": {"taskId": tid, "contextId": ctx, "artifact": {
            "artifactId": "a1", "parts": [{"text": f"artifact: {text}"}]}}})
        if text == "stream-wait":
            self.release.wait(30)
        yield ev({"statusUpdate": {"taskId": tid, "contextId": ctx,
                                   "status": {"state": "TASK_STATE_COMPLETED"}}})

    def _keep_push(self, params: dict, task_id: str) -> None:
        cfg = (params.get("configuration") or {}).get("taskPushNotificationConfig")
        if cfg is not None:
            self.push[(task_id, cfg["id"])] = dict(cfg, taskId=task_id)

    def answer(self, req: dict):
        id_ = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        with self.lock:
            if method == "SendMessage":
                msg = params["message"]
                text = msg["parts"][0].get("text", "")
                if text == "garbage":
                    return b"{not json"
                if text == "error":
                    return {"jsonrpc": "2.0", "id": id_,
                            "error": {"code": -32004, "message": "not today"}}
                if text == "slow":
                    time.sleep(1.0)
                if text == "reply-with-message":
                    return {"jsonrpc": "2.0", "id": id_, "result": {"message": {
                        "messageId": f"m-{next(self.ids)}", "role": "ROLE_AGENT",
                        "parts": [{"text": "done"}]}}}
                n = next(self.ids)
                task_id = "foreign-1" if text == "foreign-task" else f"up-task-{n}"
                self._keep_push(params, task_id)
                task = {"id": task_id, "contextId": msg.get("contextId", f"up-ctx-{n}"),
                        "status": {"state": "TASK_STATE_COMPLETED", "message": {
                            "messageId": f"s-{n}", "role": "ROLE_AGENT", "taskId": task_id,
                            "parts": [{"text": f"done: {text}"}]}},
                        "history": [dict(msg, taskId=task_id)]}
                self.tasks[task_id] = task
                return {"jsonrpc": "2.0", "id": id_, "result": {"task": task}}
            if method == "CreateTaskPushNotificationConfig":
                self.push[(params["taskId"], params["id"])] = params
                return {"jsonrpc": "2.0", "id": id_, "result": params}
            if method == "DeleteTaskPushNotificationConfig":
                self.push.pop((params["taskId"], params["id"]), None)
                return {"jsonrpc": "2.0", "id": id_, "result": None}
            if method in ("GetTask", "CancelTask"):
                task = self.tasks.get(params.get("id"))
                if task is None:
                    return {"jsonrpc": "2.0", "id": id_,
                            "error": {"code": -32001, "message": "TaskNotFoundError"}}
                if method == "CancelTask":
                    task = dict(task, status={"state": "TASK_STATE_CANCELED"})
                    self.tasks[task["id"]] = task
                return {"jsonrpc": "2.0", "id": id_, "result": task}
            return {"jsonrpc": "2.0", "id": id_,
                    "error": {"code": -32601, "message": "Method not found"}}
