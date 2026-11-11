"""A fake A2A agent for the tests of `cadl a2a-gateway`: the JSON-RPC
binding of A2A 1.0.0 over HTTP, standard library only, run in a thread of
the test. It records every request it receives (method, params and
headers), so that a test can check that a denied or held message never
reached it.

SendMessage answers with a completed task, or with what the text of the
first part asks for: "reply-with-message" (a Message), "garbage" (not
JSON), "foreign-task" (the task of another client), "slow" (after one
second), "error" (a JSON-RPC error).
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAgent:
    def __init__(self):
        self.requests: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        agent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):      # noqa: N802
                n = int(self.headers["Content-Length"])
                raw = self.rfile.read(n)
                req = json.loads(raw)
                with agent.lock:
                    agent.requests.append({"method": req.get("method"),
                                           "params": req.get("params"),
                                           "raw": raw.decode("ascii"),
                                           "headers": dict(self.headers)})
                body = agent.answer(req)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def sent(self, method: str = "SendMessage") -> list[str]:
        """The CADL names of the requests of `method` that reached the agent."""
        out = []
        with self.lock:
            for r in self.requests:
                if r["method"] != method:
                    continue
                if method == "SendMessage":
                    meta = r["params"]["message"].get("metadata", {})
                    ext = next(iter(meta.values()), {}) if meta else {}
                    out.append(ext.get("name") if isinstance(ext, dict) else None)
                else:
                    out.append(r["params"]["id"])
        return out

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
                task = {"id": task_id, "contextId": msg.get("contextId", f"up-ctx-{n}"),
                        "status": {"state": "TASK_STATE_COMPLETED", "message": {
                            "messageId": f"s-{n}", "role": "ROLE_AGENT", "taskId": task_id,
                            "parts": [{"text": f"done: {text}"}]}},
                        "history": [dict(msg, taskId=task_id)]}
                self.tasks[task_id] = task
                return {"jsonrpc": "2.0", "id": id_, "result": {"task": task}}
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
