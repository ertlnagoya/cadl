"""`cadl a2a-gateway` (Appendix G, G.11): an HTTP gateway in front of an
A2A agent that authenticates the sender of every request, decides every
SendMessage with the online runtime, holds escalated messages, and
writes an audit log and a trace. A fake A2A agent (`a2a_fake_agent.py`)
runs in a thread of the test and a fake client signs its requests; the
gateway runs as a subprocess, or in process with a fake agent function
for the checks of malformed input. Every scenario ends with the key
consistency test: the trace that the gateway wrote, replayed by
`cadl agents-run`, gives the same decisions and the same audit log, byte
for byte. Standard library only, no network outside 127.0.0.1."""

from __future__ import annotations

import concurrent.futures
import email.message
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cadl.a2a_gateway import (
    EXTENSION_URI,
    A2AGateway,
    _NoResponse,
    _NotSent,
    authorization,
    parse_listen,
    serve,
)
from cadl.agents_runtime import load_run_trace, run
from cadl.audit_log import read_records, verify
from cadl.cli import main
from cadl.gateway_core import GatewayError, approval_file, message_mac
from cadl.norms_eval import load_version

from .a2a_fake_agent import FakeAgent

ROOT = Path(__file__).parent.parent
CADL = Path(__file__).parent / "data" / "a2a_delegated_work.cadl"
BOOT = "import sys; from cadl.cli import main; sys.exit(main(sys.argv[1:]))"
KEY = b"a2a-gateway-test-key-0123456789"
CLIENT_KEYS = {"PLANNER": b"planner-client-key-0123456789",
               "OPERATOR": b"operator-client-key-0123456789"}
APPROVER_KEYS = {"OPERATOR": b"operator-approver-key-0123456789"}


def _ring(directory: Path, keys: dict, name: str) -> Path:
    ring = {}
    for actor, key in keys.items():
        (directory / f"{name}-{actor}.key").write_bytes(key)
        ring[actor] = f"{name}-{actor}.key"
    path = directory / f"{name}.json"
    path.write_text(json.dumps(ring))
    return path


def short_timeout(tmp_path, seconds: str = "2s") -> Path:
    f = tmp_path / "short.cadl"
    f.write_text(CADL.read_text().replace("timeout: 10min", f"timeout: {seconds}"))
    return f


def params(name: str | None, text: str = "hello", args=None, ext: bool = True,
           context: str | None = None, task: str | None = None, **conf) -> dict:
    msg: dict = {"messageId": f"msg-{time.monotonic_ns()}", "role": "ROLE_USER",
                 "parts": [{"text": text}]}
    if name is not None:
        value: dict = {"name": name}
        if args is not None:
            value["args"] = args
        msg["metadata"] = {EXTENSION_URI: value}
        if ext:
            msg["extensions"] = [EXTENSION_URI]
    if context is not None:
        msg["contextId"] = context
    if task is not None:
        msg["taskId"] = task
    out: dict = {"message": msg}
    if conf:
        out["configuration"] = conf
    return out


def rpc(method: str, p, id_=1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": p}


class Client:
    """A fake A2A client that signs its requests (CADL-HMAC)."""

    def __init__(self, port: int, run_id: str):
        self.port, self.run_id = port, run_id

    def raw(self, body: bytes, headers: dict, path: str = "/", method: str = "POST",
            timeout: float = 30.0):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for k, v in headers.items():
                for item in (v if isinstance(v, list) else [v]):
                    conn.putheader(k, item)
            conn.endheaders()
            if body:
                conn.send(body)
            resp = conn.getresponse()
            data = resp.read()
            try:
                parsed = json.loads(data) if data else None
            except ValueError:
                parsed = data           # the error page of http.server
            return resp.status, resp.headers, parsed
        finally:
            conn.close()

    def post(self, actor: str | None, obj, key: bytes | None = None,
             run_id: str | None = None, extra: dict | None = None, version="1.0",
             nonce: str | None = None, timeout: float = 30.0):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if version is not None:
            headers["A2A-Version"] = version
        if actor is not None:
            headers["Authorization"] = authorization(key or CLIENT_KEYS[actor], actor,
                                                     run_id or self.run_id, body,
                                                     nonce=nonce)
        headers.update(extra or {})
        return self.raw(body, headers, timeout=timeout)

    def call(self, actor: str, method: str, p, id_=1, **kw) -> dict:
        status, _, body = self.post(actor, rpc(method, p, id_), **kw)
        assert status == 200, (status, body)
        assert body["id"] == id_
        return body


def wait_for(cond, what: str, timeout: float = 30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = cond()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def state(body: dict) -> str:
    return body["result"]["task"]["status"]["state"]


def status_text(body: dict) -> str:
    return body["result"]["task"]["status"]["message"]["parts"][0]["text"]


def host_events(path: Path) -> list[tuple]:
    return [(r["event"], r["reason"]) for r in read_records(path)
            if r.get("event") in ("unauthenticated_request", "task_refused",
                                  "invalid_response", "forward_failed", "method_refused",
                                  "unauthorised_message", "unauthorised_approval",
                                  "approval_evidence")]


def decisions(result) -> list[tuple]:
    return [(s["message"]["from"], s["message"]["name"], s["result"]["decision"],
             s["result"]["reason"]) for s in result["steps"]]


class Run:
    """A gateway subprocess in front of a fake agent, and a fake client."""

    def __init__(self, tmp_path, cadl=CADL, key: bool = False, upstream: str | None = None,
                 extra=(), name="gw"):
        self.dir = tmp_path / name
        self.dir.mkdir()
        self.cadl = cadl
        self.agent = FakeAgent()
        self.audit = self.dir / "audit.jsonl"
        self.trace = self.dir / "audit.trace.jsonl"
        self.inbox = self.dir / "inbox"
        self.port_file = self.dir / "port"
        self.key = None
        clients = _ring(self.dir, CLIENT_KEYS, "clients")
        approvers = _ring(self.dir, APPROVER_KEYS, "approvers")
        args = ["a2a-gateway", str(cadl), "--agent-actor", "SUMMARIZER",
                "--upstream", upstream or self.agent.url, "--client-keys", str(clients),
                "--audit", str(self.audit), "--approvals", str(self.inbox),
                "--approver-keys", str(approvers), "--listen", "127.0.0.1:0",
                "--port-file", str(self.port_file), "--poll", "0.02",
                "--upstream-timeout", "10", *extra]
        if key:
            self.key = self.dir / "key"
            self.key.write_bytes(KEY)
            args += ["--audit-key", str(self.key)]
        self.proc = subprocess.Popen([sys.executable, "-c", BOOT, *args],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Wait for the published files, not only for the trace.
        wait_for(lambda: self.port_file.exists() and self.port_file.read_text().strip(),
                 "the port file")
        run_json = self.inbox / "run.json"
        wait_for(run_json.exists, "the published run.json")
        self.run_id = json.loads(run_json.read_text())["run_id"]
        self.client = Client(int(self.port_file.read_text()), self.run_id)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def call(self, actor, method, p, id_=1, **kw):
        return self.client.call(actor, method, p, id_, **kw)

    def later(self, actor, method, p, id_=1, **kw):
        return self.pool.submit(self.client.call, actor, method, p, id_, **kw)

    def wait_hold(self, hold) -> Path:
        path = self.inbox / "holds" / f"{hold}.json"
        wait_for(path.exists, f"the published hold {hold}")
        return path

    def approve(self, hold, decision="approve", actor="OPERATOR") -> None:
        self.wait_hold(hold)
        (self.dir / f"approve-{actor}.key").write_bytes(APPROVER_KEYS[actor])
        assert main(["mcp-approve", str(self.inbox), hold, decision, "--as", actor,
                     "--key", str(self.dir / f"approve-{actor}.key")]) == 0

    def message(self, text, *args) -> None:
        sender = text.split("->")[0].strip()
        (self.dir / f"msg-{sender}.key").write_bytes(APPROVER_KEYS[sender])
        assert main(["mcp-message", str(self.inbox), text, "--key",
                     str(self.dir / f"msg-{sender}.key"),
                     *[a for x in args for a in ("--arg", x)]]) == 0

    def wait_inbox_done(self) -> None:
        wait_for(lambda: not [p for p in self.inbox.iterdir()
                              if p.suffix == ".json" and p.name != "run.json"],
                 "the inbox to be read")

    def stop(self, expect=0) -> str:
        self.proc.send_signal(signal.SIGTERM)
        assert self.proc.wait(timeout=30) == expect
        self.pool.shutdown(wait=True)
        self.agent.close()
        return self.proc.stderr.read().decode()

    def replay_check(self) -> dict:
        replayed = self.dir / "replayed.jsonl"
        args = ["agents-run", str(self.cadl), "--trace", str(self.trace),
                "--audit", str(replayed), "--format", "json"]
        if self.key:
            args += ["--audit-key", str(self.key)]
        assert main(args) == 0
        assert replayed.read_bytes() == self.audit.read_bytes()
        result = verify(self.audit, key=KEY if self.key else None)
        assert result["valid"] and result["complete"], result
        return run(load_version(self.cadl), load_run_trace(self.trace))


# === Scenarios through the subprocess ==========================================

class TestScenarios:

    def test_allow_deny_and_own_tasks(self, tmp_path, capsys):
        r = Run(tmp_path, key=True)
        body = r.call("PLANNER", "SendMessage", params("summarize", "zq-secret-text"))
        task = body["result"]["task"]
        assert task["id"] == "up-task-1" and task["status"]["state"] == "TASK_STATE_COMPLETED"
        # Denied: the default of the contract.
        body = r.call("PLANNER", "SendMessage", params("translate"), id_="b")
        assert state(body) == "TASK_STATE_REJECTED"
        assert "reason: default" in status_text(body)
        gw_task = body["result"]["task"]["id"]
        # A message without the extension has no name.
        body = r.call("PLANNER", "SendMessage", params("summarize", ext=False), id_=3)
        assert state(body) == "TASK_STATE_REJECTED" and "no_name" in status_text(body)
        # The client's own tasks only.
        assert r.call("PLANNER", "GetTask", {"id": "up-task-1"})["result"]["id"] == "up-task-1"
        assert r.call("PLANNER", "GetTask", {"id": gw_task})["result"]["status"]["state"] \
            == "TASK_STATE_REJECTED"
        assert r.call("OPERATOR", "GetTask", {"id": "up-task-1"})["error"]["code"] == -32001
        assert r.call("OPERATOR", "CancelTask", {"id": "up-task-1"})["error"]["code"] == -32001
        assert r.call("OPERATOR", "GetTask", {"id": gw_task})["error"]["code"] == -32001
        assert r.call("OPERATOR", "SendMessage",
                      params("translate", task="up-task-1"))["error"]["code"] == -32001
        assert r.call("PLANNER", "CancelTask", {"id": "up-task-1"})["result"]["status"][
            "state"] == "TASK_STATE_CANCELED"
        assert r.call("PLANNER", "CancelTask", {"id": gw_task})["error"]["code"] == -32002
        r.stop()
        assert r.agent.sent() == ["summarize"]
        assert r.agent.sent("GetTask") == ["up-task-1"]
        assert r.agent.sent("CancelTask") == ["up-task-1"]
        # The client's credentials are not passed on; the version is.
        headers = r.agent.requests[0]["headers"]
        assert "Authorization" not in headers and headers["A2A-Version"] == "1.0"
        result = r.replay_check()
        assert decisions(result) == [
            ("PLANNER", "summarize", "allow", "permission"),
            ("PLANNER", "translate", "deny", "default")]
        assert [e for e, _ in host_events(r.audit)] == [
            "unauthorised_message", "task_refused", "task_refused", "task_refused",
            "task_refused"]
        assert "zq-secret-text" not in r.trace.read_text() + r.audit.read_text()
        capsys.readouterr()

    def test_escalate_approved_blocking(self, tmp_path, capsys):
        r = Run(tmp_path)
        fut = r.later("PLANNER", "SendMessage", params("publish", "report"))
        r.wait_hold("h-1")
        time.sleep(0.2)
        assert not fut.done() and r.agent.sent() == []
        hold = json.loads((r.inbox / "holds" / "h-1.json").read_text())
        assert hold["params"]["message"]["parts"] == [{"text": "report"}]
        r.approve("h-1")
        body = fut.result(timeout=30)
        assert body["result"]["task"]["id"] == "up-task-1"
        r.stop()
        assert r.agent.sent() == ["publish"]
        result = r.replay_check()
        assert [h["state"] for h in result["holds"]] == ["approved"]
        assert decisions(result) == [("PLANNER", "publish", "escalate", "prohibition"),
                                     ("OPERATOR", "approve", "allow", "permission")]
        assert [e for e, _ in host_events(r.audit)] == ["approval_evidence"]
        capsys.readouterr()

    def test_escalate_rejected(self, tmp_path, capsys):
        r = Run(tmp_path)
        fut = r.later("PLANNER", "SendMessage", params("publish"))
        r.approve("h-1", "reject")
        body = fut.result(timeout=30)
        assert state(body) == "TASK_STATE_REJECTED"
        text = status_text(body)
        assert "rejected" in text and "hold h-1" in text
        assert "fallback: PLANNER -> SUMMARIZER : summarize" in text
        assert "notify: OPERATOR" in text
        meta = body["result"]["task"]["metadata"][EXTENSION_URI]
        assert meta["reason"] == "rejected" and meta["hold_id"] == "h-1"
        r.stop()
        assert r.agent.sent() == []
        assert [h["state"] for h in r.replay_check()["holds"]] == ["rejected"]
        capsys.readouterr()

    def test_escalate_timeout(self, tmp_path, capsys):
        r = Run(tmp_path, cadl=short_timeout(tmp_path))
        fut = r.later("PLANNER", "SendMessage", params("publish"))
        r.wait_hold("h-1")
        late = approval_file(r.inbox, "h-1", "approve", "OPERATOR",
                             APPROVER_KEYS["OPERATOR"])
        body = fut.result(timeout=30)
        assert state(body) == "TASK_STATE_REJECTED" and "expired" in status_text(body)
        assert not (r.inbox / "holds" / "h-1.json").exists()
        (r.inbox / "late.json").write_text(json.dumps(late))
        r.wait_inbox_done()
        r.stop()
        assert r.agent.sent() == []
        result = r.replay_check()
        assert [h["state"] for h in result["holds"]] == ["expired"]
        capsys.readouterr()

    def test_return_immediately_approved(self, tmp_path, capsys):
        r = Run(tmp_path)
        body = r.call("PLANNER", "SendMessage", params("publish", returnImmediately=True))
        task = body["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
        assert task["metadata"][EXTENSION_URI]["hold_id"] == "h-1"
        gw_id = task["id"]
        assert r.call("PLANNER", "GetTask", {"id": gw_id})["result"]["status"]["state"] \
            == "TASK_STATE_SUBMITTED"
        r.approve("h-1")
        got = wait_for(lambda: (lambda b: b if b["result"]["status"]["state"]
                                == "TASK_STATE_COMPLETED" else None)(
            r.call("PLANNER", "GetTask", {"id": gw_id})), "the released task")
        assert got["result"]["id"] == gw_id                    # the id of the gateway
        assert got["result"]["status"]["message"]["taskId"] == gw_id
        assert r.call("OPERATOR", "GetTask", {"id": gw_id})["error"]["code"] == -32001
        # Continue the task: the gateway passes on the agent's id.
        r.call("PLANNER", "SendMessage", params("summarize", task=gw_id), id_=2)
        r.stop()
        assert r.agent.sent() == ["publish", "summarize"]
        assert r.agent.requests[-1]["params"]["message"]["taskId"] == "up-task-1"
        assert r.agent.sent("GetTask") == ["up-task-1"]
        assert [h["state"] for h in r.replay_check()["holds"]] == ["approved"]
        capsys.readouterr()

    def test_cancel_then_approve(self, tmp_path, capsys):
        r = Run(tmp_path)
        body = r.call("PLANNER", "SendMessage", params("publish", returnImmediately=True))
        gw_id = body["result"]["task"]["id"]
        hold_path = r.wait_hold("h-1")
        late = approval_file(r.inbox, "h-1", "approve", "OPERATOR",
                             APPROVER_KEYS["OPERATOR"])
        body = r.call("PLANNER", "CancelTask", {"id": gw_id})
        assert body["result"]["status"]["state"] == "TASK_STATE_CANCELED"
        assert not hold_path.exists()
        (r.inbox / "late.json").write_text(json.dumps(late))   # signed before the cancel
        r.wait_inbox_done()
        assert r.call("PLANNER", "CancelTask", {"id": gw_id})["error"]["code"] == -32002
        r.stop()
        assert r.agent.sent() == []
        result = r.replay_check()
        assert [h["state"] for h in result["holds"]] == ["withdrawn"]
        assert not result["holds"][0]["released"]
        capsys.readouterr()

    def test_client_disconnect_withdraws(self, tmp_path, capsys):
        r = Run(tmp_path)
        body = json.dumps(rpc("SendMessage", params("publish"))).encode()
        sock = socket.create_connection(("127.0.0.1", r.client.port))
        head = (f"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                f"A2A-Version: 1.0\r\nContent-Length: {len(body)}\r\n"
                f"Authorization: "
                f"{authorization(CLIENT_KEYS['PLANNER'], 'PLANNER', r.run_id, body)}"
                f"\r\n\r\n").encode()
        sock.sendall(head + body)
        hold_path = r.wait_hold("h-1")
        late = approval_file(r.inbox, "h-1", "approve", "OPERATOR",
                             APPROVER_KEYS["OPERATOR"])
        sock.close()
        wait_for(lambda: not hold_path.exists(), "the withdrawal")
        (r.inbox / "late.json").write_text(json.dumps(late))
        r.wait_inbox_done()
        r.stop()
        assert r.agent.sent() == []
        assert [h["state"] for h in r.replay_check()["holds"]] == ["withdrawn"]
        capsys.readouterr()

    def test_grant_through_inbox(self, tmp_path, capsys):
        r = Run(tmp_path)
        assert state(r.call("PLANNER", "SendMessage", params("translate"))) \
            == "TASK_STATE_REJECTED"
        r.message("OPERATOR -> PLANNER : grant_translate")
        r.wait_inbox_done()
        body = r.call("PLANNER", "SendMessage", params("translate"), id_=2)
        assert body["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        r.stop()
        assert r.agent.sent() == ["translate"]
        result = r.replay_check()
        assert decisions(result) == [
            ("PLANNER", "translate", "deny", "default"),
            ("OPERATOR", "grant_translate", "allow", "permission"),
            ("PLANNER", "translate", "allow", "permission")]
        capsys.readouterr()

    def test_approval_bound_to_arguments(self, tmp_path, capsys):
        """Two held messages of the same name with different arguments: an
        approval of one, re-addressed to the other, releases nothing."""
        r = Run(tmp_path)
        f1 = r.later("PLANNER", "SendMessage", params("publish", "one"), id_=1)
        r.wait_hold("h-1")
        f2 = r.later("PLANNER", "SendMessage", params("publish", "two"), id_=2)
        r.wait_hold("h-2")
        obj = approval_file(r.inbox, "h-1", "approve", "OPERATOR",
                            APPROVER_KEYS["OPERATOR"])
        obj["hold_id"] = "h-2"
        obj["mac"] = message_mac(APPROVER_KEYS["OPERATOR"], obj)
        (r.inbox / "forged.json").write_text(json.dumps(obj))
        r.wait_inbox_done()
        assert not f2.done()
        r.approve("h-2", "reject")
        r.approve("h-1", "reject")
        assert state(f1.result(timeout=30)) == "TASK_STATE_REJECTED"
        assert state(f2.result(timeout=30)) == "TASK_STATE_REJECTED"
        r.stop()
        assert r.agent.sent() == []
        assert ("unauthorised_approval", "wrong_message") in host_events(r.audit)
        r.replay_check()
        capsys.readouterr()

    def test_authentication_failures(self, tmp_path, capsys):
        r = Run(tmp_path)
        c = r.client
        req = rpc("SendMessage", params("summarize"))
        status, headers, body = c.post(None, req)
        assert status == 401 and headers["WWW-Authenticate"] == f'CADL-HMAC run="{r.run_id}"'
        assert headers["CADL-Run-Id"] == r.run_id
        assert c.post("PLANNER", req, key=CLIENT_KEYS["OPERATOR"])[0] == 401   # wrong key
        assert c.post("SUMMARIZER", req, key=b"x" * 32)[0] == 401              # no key
        assert c.post("PLANNER", req, run_id="0" * 32)[0] == 401               # other run
        good = json.dumps(req).encode()
        auth = authorization(CLIENT_KEYS["PLANNER"], "PLANNER", r.run_id, good)
        hdr = {"Content-Type": "application/json", "A2A-Version": "1.0",
               "Authorization": auth}
        # The MAC covers the body: another body with the same header fails.
        other = json.dumps(rpc("SendMessage", params("publish"))).encode()
        assert c.raw(other, dict(hdr, **{"Content-Length": str(len(other))}))[0] == 401
        assert c.raw(good, dict(hdr, **{"Content-Length": str(len(good))}))[0] == 200
        # The same request again: the nonce was used.
        assert c.raw(good, dict(hdr, **{"Content-Length": str(len(good))}))[0] == 401
        # Two Authorization headers, a header in another form.
        assert c.raw(good, dict(hdr, **{"Content-Length": str(len(good)),
                                        "Authorization": [auth, auth]}))[0] == 401
        assert c.raw(good, dict(hdr, **{"Content-Length": str(len(good)),
                                        "Authorization": "Bearer " + "a" * 40}))[0] == 401
        r.stop()
        assert r.agent.sent() == ["summarize"]
        assert host_events(r.audit) == [
            ("unauthenticated_request", "missing"), ("unauthenticated_request", "bad_mac"),
            ("unauthenticated_request", "unknown_actor"),
            ("unauthenticated_request", "bad_mac"), ("unauthenticated_request", "bad_mac"),
            ("unauthenticated_request", "replayed"),
            ("unauthenticated_request", "malformed"),
            ("unauthenticated_request", "malformed")]
        r.replay_check()
        capsys.readouterr()

    def test_agent_failures(self, tmp_path, capsys):
        r = Run(tmp_path)
        assert r.call("PLANNER", "SendMessage", params("summarize", "garbage"))["error"][
            "code"] == -32006
        assert r.call("PLANNER", "SendMessage", params("summarize", "error"))["error"][
            "code"] == -32004
        assert r.call("PLANNER", "SendMessage", params("summarize", "foreign-task"))["result"][
            "task"]["id"] == "foreign-1"
        # The agent gives the task of PLANNER to OPERATOR: refused.
        r2 = r.call("OPERATOR", "SendMessage", params("translate", "foreign-task"))
        assert r2["error"]["code"] == -32006
        body = r.call("PLANNER", "SendMessage", params("summarize", "reply-with-message"))
        assert body["result"]["message"]["parts"] == [{"text": "done"}]
        r.stop()
        events = [e for e, _ in host_events(r.audit)]
        assert events == ["invalid_response", "invalid_response"]
        r.replay_check()
        capsys.readouterr()

    def test_agent_down(self, tmp_path, capsys):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()                                   # nothing listens there
        r = Run(tmp_path, upstream=f"http://127.0.0.1:{port}/")
        body = r.call("PLANNER", "SendMessage", params("summarize"))
        assert body["error"]["code"] == -32603
        r.stop()
        assert [e for e, _ in host_events(r.audit)] == ["forward_failed"]
        result = r.replay_check()
        assert decisions(result) == [("PLANNER", "summarize", "allow", "permission")]
        capsys.readouterr()

    def test_stop_with_held_request(self, tmp_path, capsys):
        r = Run(tmp_path)
        fut = r.later("PLANNER", "SendMessage", params("publish"))
        r.wait_hold("h-1")
        err = r.stop()
        assert fut.result(timeout=30)["error"]["code"] == -32603
        assert "1 held request(s) were not answered: h-1" in err
        assert r.agent.sent() == []
        r.replay_check()
        capsys.readouterr()


# === In process: malformed input and usage errors ================================

class Headers(email.message.Message):
    pass


def headers(**h) -> Headers:
    out = Headers()
    for k, v in h.items():
        for item in (v if isinstance(v, list) else [v]):
            out[k.replace("_", "-")] = item
    return out


class Local:
    """A gateway in process with a fake agent function and a fake clock."""

    def __init__(self, tmp_path, answer=None, cadl=CADL, agent="SUMMARIZER", **kw):
        self.t = [0.0]
        self.sent: list[dict] = []
        self.answer = answer
        self.audit = tmp_path / "audit.jsonl"
        self.trace = tmp_path / "audit.trace.jsonl"
        self.cadl = cadl
        self.gw = A2AGateway(cadl, agent, "http://127.0.0.1:9/", dict(CLIENT_KEYS),
                             self.audit, self.trace, clock=lambda: self.t[0],
                             post_upstream=self._post, **kw)

    def _post(self, text: str) -> bytes:
        req = json.loads(text)
        self.sent.append(req)
        if self.answer is not None:
            return self.answer(req)
        return json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"task": {
            "id": f"up-{len(self.sent)}", "contextId": "c",
            "status": {"state": "TASK_STATE_WORKING"}}}}).encode()

    def post(self, body, actor="PLANNER", version="1.0", ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        h = {"Authorization": authorization(CLIENT_KEYS[actor], actor, self.gw.run_id, raw)}
        if version is not None:
            h["A2A-Version"] = version
        if ctype is not None:
            h["Content-Type"] = ctype
        return self.gw.handle_post("/", headers(**h), raw)

    def stop_and_replay(self):
        self.gw.stop()
        replayed = self.audit.parent / "replayed.jsonl"
        assert main(["agents-run", str(self.cadl), "--trace", str(self.trace), "--audit",
                     str(replayed)]) == 0
        assert replayed.read_bytes() == self.audit.read_bytes()


class TestMalformed:

    @pytest.mark.parametrize("raw,code", [
        (b"{not json", -32700),
        (b'{"jsonrpc":"2.0","id":1,"id":2,"method":"SendMessage","params":{}}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{"id":"x","historyLength":1e999}}',
         -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{"id":NaN}}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{"id":"\\ud800"}}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{"id":"a\rb"}}', -32700),
        (b'\xff\xfe', -32700),
        (b'[{"jsonrpc":"2.0","id":1,"method":"GetTask","params":{"id":"x"}}]', -32600),
        (b'{"jsonrpc":"2.0","method":"SendMessage","params":{}}', -32600),
        (b'{"jsonrpc":"2.0","id":null,"method":"SendMessage","params":{}}', -32600),
        (b'{"jsonrpc":"2.0","id":true,"method":"SendMessage","params":{}}', -32600),
        (b'{"jsonrpc":"1.0","id":1,"method":"SendMessage","params":{}}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{},"x":1}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":7}', -32600),
        (b'[' * 200 + b']' * 200, -32600),
    ])
    def test_refused_before_decision(self, tmp_path, raw, code):
        loc = Local(tmp_path)
        reply = loc.post(raw)
        assert reply.status == 200 and reply.body["error"]["code"] == code
        assert loc.sent == [] and loc.gw.runtime.steps == []
        loc.stop_and_replay()

    @pytest.mark.parametrize("mutate,code", [
        (lambda p: p["message"].update(role="ROLE_AGENT"), -32602),
        (lambda p: p["message"].update(parts=[]), -32602),
        (lambda p: p["message"].update(parts=[{"text": "a", "data": {}}]), -32602),
        (lambda p: p["message"].update(extra=1), -32602),
        (lambda p: p.update(other=1), -32602),
        (lambda p: p["message"]["metadata"][EXTENSION_URI].update(name="a b"), -32602),
        (lambda p: p["message"]["metadata"][EXTENSION_URI].update(name="a\nb"), -32602),
        (lambda p: p["message"]["metadata"][EXTENSION_URI].update(args=[1]), -32602),
        (lambda p: p["message"]["metadata"][EXTENSION_URI].update(more=1), -32602),
        (lambda p: p["message"]["metadata"].update(big=2 ** 60), -32602),
        (lambda p: p.update(configuration={"returnImmediately": "yes"}), -32602),
        (lambda p: p.update(configuration={"taskPushNotificationConfig": {"url": "x"}}),
         -32003),
    ])
    def test_bad_send_message(self, tmp_path, mutate, code):
        loc = Local(tmp_path)
        p = params("summarize")
        mutate(p)
        reply = loc.post(rpc("SendMessage", p))
        assert reply.body["error"]["code"] == code, reply.body
        assert loc.sent == [] and loc.gw.runtime.steps == []
        loc.stop_and_replay()

    def test_methods_versions_and_content_type(self, tmp_path):
        loc = Local(tmp_path)
        p = params("summarize")
        assert loc.post(rpc("SendMessage", p), version=None).body["error"]["code"] == -32009
        assert loc.post(rpc("SendMessage", p), version="0.3").body["error"]["code"] == -32009
        assert loc.post(rpc("SendMessage", p), ctype="text/plain").status == 415
        assert loc.post(rpc("SendMessage", p), ctype=None).status == 415
        for method, code in (("SendStreamingMessage", -32004), ("SubscribeToTask", -32004),
                             ("ListTasks", -32004), ("GetExtendedAgentCard", -32004),
                             ("CreateTaskPushNotificationConfig", -32003),
                             ("message/send", -32601), ("tasks/get", -32601)):
            assert loc.post(rpc(method, p)).body["error"]["code"] == code
        assert loc.gw.handle_post("/other", headers(), b"{}").status == 404
        assert loc.gw.handle_get("/.well-known/agent-card.json").status == 404
        assert loc.sent == []
        assert [r["reason"] for r in read_records(loc.audit)
                if r.get("event") == "method_refused"] == [
            "SendStreamingMessage", "SubscribeToTask", "ListTasks", "GetExtendedAgentCard",
            "CreateTaskPushNotificationConfig", "message/send", "tasks/get"]
        loc.stop_and_replay()

    def test_approval_as_a2a_message(self, tmp_path):
        loc = Local(tmp_path)
        reply = loc.post(rpc("SendMessage", params("approve", args=["h-1"])), actor="OPERATOR")
        assert reply.body["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
        assert loc.sent == [] and loc.gw.runtime.steps == []
        assert host_events(loc.audit) == [("unauthorised_message", "approval_as_message")]
        loc.stop_and_replay()

    def test_reserialised(self, tmp_path):
        """What reaches the agent is the compact ASCII JSON of the parsed
        object: no carriage return or line feed, no non-ASCII byte."""
        loc = Local(tmp_path)
        raw = (b'{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":'
               b'{"messageId":"m","role":"ROLE_USER","parts":[{"text":"a\\r\\nb \xc3\xa9"}],'
               b'"extensions":["' + EXTENSION_URI.encode() + b'"],"metadata":{"'
               + EXTENSION_URI.encode() + b'":{"name":"summarize"}}}}}')
        reply = loc.post(raw.replace(b",", b",\r\n "))
        assert reply.body["result"]["task"]["id"] == "up-1"
        assert loc.sent[0]["params"]["message"]["parts"][0]["text"] == "a\r\nb é"
        assert loc.sent[0]["id"] == 1 and isinstance(loc.sent[0]["id"], int)
        loc.stop_and_replay()

    def test_failure_is_closed(self, tmp_path, monkeypatch):
        loc = Local(tmp_path)

        def boom(line):
            raise RuntimeError("injected")
        monkeypatch.setattr(loc.gw.runtime, "observe", boom)
        for _ in range(2):
            reply = loc.post(rpc("SendMessage", params("summarize")))
            task = reply.body["result"]["task"]
            assert task["status"]["state"] == "TASK_STATE_REJECTED"
            assert "fail-closed" in task["status"]["message"]["parts"][0]["text"]
        assert loc.sent == [] and loc.gw.failed is not None
        loc.gw.stop()
        assert verify(loc.audit, allow_open=True)["valid"]

    def test_send_failure_after_allow(self, tmp_path):
        def answer(req):
            raise _NotSent("injected")
        loc = Local(tmp_path, answer=answer)
        assert loc.post(rpc("SendMessage", params("summarize"))).body["error"]["code"] \
            == -32603
        recs = [r for r in read_records(loc.audit) if r.get("event") == "forward_failed"]
        assert recs and recs[0]["message"]["name"] == "summarize"
        loc.stop_and_replay()

    def test_no_response(self, tmp_path):
        def answer(req):
            raise _NoResponse("injected")
        loc = Local(tmp_path, answer=answer)
        assert loc.post(rpc("SendMessage", params("summarize"))).body["error"]["code"] \
            == -32006
        loc.stop_and_replay()

    def test_wrong_response_id(self, tmp_path):
        loc = Local(tmp_path, answer=lambda req: json.dumps(
            {"jsonrpc": "2.0", "id": req["id"] + 1, "result": {"task": {"id": "x"}}}).encode())
        assert loc.post(rpc("SendMessage", params("summarize"))).body["error"]["code"] \
            == -32006
        loc.stop_and_replay()

    def test_expiry_with_fake_clock(self, tmp_path):
        loc = Local(tmp_path)
        reply = loc.post(rpc("SendMessage", params("publish", returnImmediately=True)))
        gw_id = reply.body["result"]["task"]["id"]
        loc.t[0] = 600.0
        loc.gw.tick()
        assert loc.gw.tasks[gw_id].state == "TASK_STATE_SUBMITTED"     # at the deadline
        loc.t[0] = 600.002
        loc.gw.tick()
        got = loc.post(rpc("GetTask", {"id": gw_id})).body["result"]
        assert got["status"]["state"] == "TASK_STATE_REJECTED"
        assert "expired" in got["status"]["message"]["parts"][0]["text"]
        assert loc.sent == []
        loc.stop_and_replay()


class TestUsage:

    def test_bad_options(self, tmp_path):
        def make(**kw):
            args = dict(file=CADL, agent_actor="SUMMARIZER", upstream="http://127.0.0.1:9/",
                        client_keys=dict(CLIENT_KEYS), audit=tmp_path / "a.jsonl",
                        trace_out=tmp_path / "a.trace.jsonl")
            args.update(kw)
            return A2AGateway(**args)
        with pytest.raises(GatewayError, match="not an agent"):
            make(agent_actor="OPERATOR")
        with pytest.raises(GatewayError, match="not an actor of the file"):
            make(client_keys={"NOBODY": b"k" * 20})
        with pytest.raises(GatewayError, match="not an http"):
            make(upstream="file:///etc/passwd")
        with pytest.raises(GatewayError, match="not an http"):
            make(upstream="http://user:pw@127.0.0.1/")
        with pytest.raises(GatewayError, match="key of the audit log"):
            make(client_keys={"PLANNER": KEY}, key=KEY)
        with pytest.raises(GatewayError, match="printable"):
            make(upstream_token="a\r\nX-Injected: 1")
        with pytest.raises(GatewayError, match="needs --approver-keys"):
            make(inbox=tmp_path / "inbox")
        assert not (tmp_path / "a.jsonl").exists()

    def test_listen(self, tmp_path):
        assert parse_listen("127.0.0.1:0") == ("127.0.0.1", 0)
        assert parse_listen("[::1]:8470") == ("::1", 8470)
        with pytest.raises(GatewayError):
            parse_listen("127.0.0.1")
        with pytest.raises(GatewayError, match="loopback"):
            serve({}, listen="0.0.0.0:0")
        with pytest.raises(GatewayError, match="loopback"):
            serve({}, listen="192.0.2.1:0")

    def test_cli_usage_error(self, tmp_path, capsys):
        ring = _ring(tmp_path, CLIENT_KEYS, "c")
        assert main(["a2a-gateway", str(CADL), "--agent-actor", "OPERATOR", "--upstream",
                     "http://127.0.0.1:9/", "--client-keys", str(ring), "--audit",
                     str(tmp_path / "a.jsonl"), "--listen", "127.0.0.1:0"]) == 2
        assert "not an agent" in capsys.readouterr().err


def test_http_framing(tmp_path, capsys):
    """Requests without one Content-Length, with Transfer-Encoding, or with
    a body too large are refused before anything is read as JSON."""
    r = Run(tmp_path)
    c = r.client
    body = json.dumps(rpc("SendMessage", params("summarize"))).encode()
    auth = authorization(CLIENT_KEYS["PLANNER"], "PLANNER", r.run_id, body)
    base = {"Content-Type": "application/json", "A2A-Version": "1.0", "Authorization": auth}
    chunked = hex(len(body))[2:].encode() + b"\r\n" + body + b"\r\n0\r\n\r\n"
    assert c.raw(chunked, dict(base, **{"Transfer-Encoding": "chunked"}))[0] == 501
    assert c.raw(body, dict(base, **{"Content-Length": [str(len(body))] * 2}))[0] == 411
    assert c.raw(body, dict(base, **{"Content-Length": "+" + str(len(body))}))[0] == 411
    assert c.raw(b"", dict(base, **{"Content-Length": str(2 << 20)}))[0] == 413
    assert c.raw(b"", base, method="PUT")[0] == 501
    r.stop()
    assert r.agent.sent() == []
    assert r.replay_check()["steps"] == []
    capsys.readouterr()


def test_tls_and_agent_card(tmp_path, capsys):
    """HTTPS with a self-signed certificate (when `openssl` is installed),
    and the agent card of --agent-card."""
    import shutil
    import ssl
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    done = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                           "-keyout", str(key), "-out", str(cert), "-days", "1",
                           "-subj", "/CN=127.0.0.1"], capture_output=True)
    if done.returncode != 0:
        pytest.skip("openssl cannot make a certificate")
    card = tmp_path / "card.json"
    card.write_text(json.dumps({"name": "summarizer", "securitySchemes": {
        "cadl": {"httpAuthSecurityScheme": {"scheme": "CADL-HMAC"}}},
        "capabilities": {"extensions": [{"uri": EXTENSION_URI, "required": True}]}}))
    r = Run(tmp_path, extra=("--tls-cert", str(cert), "--tls-key", str(key),
                             "--agent-card", str(card)))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE         # a self-signed test certificate
    body = json.dumps(rpc("SendMessage", params("summarize"))).encode()
    conn = http.client.HTTPSConnection("127.0.0.1", r.client.port, context=ctx, timeout=30)
    conn.request("POST", "/", body=body, headers={
        "Content-Type": "application/json", "A2A-Version": "1.0",
        "Authorization": authorization(CLIENT_KEYS["PLANNER"], "PLANNER", r.run_id, body)})
    resp = json.loads(conn.getresponse().read())
    conn.close()
    assert resp["result"]["task"]["id"] == "up-task-1"
    conn = http.client.HTTPSConnection("127.0.0.1", r.client.port, context=ctx, timeout=30)
    conn.request("GET", "/.well-known/agent-card.json")
    got = conn.getresponse()
    assert got.status == 200 and json.loads(got.read())["name"] == "summarizer"
    conn.close()
    with pytest.raises((ssl.SSLError, OSError, http.client.HTTPException)):
        r.client.post("PLANNER", rpc("SendMessage", params("summarize")))   # plain HTTP
    # A held blocking request whose client closes the TLS connection with
    # close_notify is withdrawn (a finding of the review).
    body = json.dumps(rpc("SendMessage", params("publish"))).encode()
    raw = socket.create_connection(("127.0.0.1", r.client.port))
    tls = ctx.wrap_socket(raw)
    tls.sendall((f"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                 f"A2A-Version: 1.0\r\nContent-Length: {len(body)}\r\nAuthorization: "
                 f"{authorization(CLIENT_KEYS['PLANNER'], 'PLANNER', r.run_id, body)}"
                 f"\r\n\r\n").encode() + body)
    hold_path = r.wait_hold("h-1")
    tls.settimeout(2)
    try:
        tls.unwrap()                                 # sends close_notify
    except (ssl.SSLError, OSError):
        pass
    raw.close()
    wait_for(lambda: not hold_path.exists(), "the withdrawal over TLS")
    r.stop()
    assert r.agent.sent() == ["summarize"]
    assert [h["state"] for h in r.replay_check()["holds"]] == ["withdrawn"]
    capsys.readouterr()


def workers_file(tmp_path, receiver: str = "SUMMARIZER[*]") -> Path:
    """The test file with a set SUMMARIZER[1..2] and approval rules whose
    receiver is `receiver`."""
    f = tmp_path / "workers.cadl"
    text = CADL.read_text().replace("- id: SUMMARIZER\n", "- id: SUMMARIZER[1..2]\n")
    text = text.replace("parties: [OPERATOR, PLANNER, SUMMARIZER]",
                        'parties: [OPERATOR, PLANNER, "SUMMARIZER[*]"]')
    text = text.replace("-> SUMMARIZER :", "-> SUMMARIZER[*] :")
    text = text.replace('"OPERATOR -> SUMMARIZER[*] : approve", creates',
                        f'"OPERATOR -> {receiver} : approve", creates')
    text = text.replace('"OPERATOR -> SUMMARIZER[*] : reject",  creates',
                        f'"OPERATOR -> {receiver} : reject",  creates')
    f.write_text(text)
    return f


class TestReviewFindings:
    """Defects found by an independent review of the first version."""

    def test_unknown_context_is_refused(self, tmp_path):
        """A context id that the gateway has not seen given to the client
        is not passed on (it could be a context of another client, also of
        an earlier run of the gateway)."""
        loc = Local(tmp_path)
        reply = loc.post(rpc("SendMessage", params("translate", context="c")),
                         actor="OPERATOR")
        assert reply.body["error"]["code"] == -32001
        assert loc.post(rpc("SendMessage", params("summarize"))).body["result"]["task"][
            "contextId"] == "c"                      # the agent gives "c" to PLANNER
        assert loc.post(rpc("SendMessage", params("translate", context="c")),
                        actor="OPERATOR").body["error"]["code"] == -32001
        assert loc.post(rpc("SendMessage", params("summarize", context="c"))).body[
            "result"]["task"]["id"] == "up-2"
        assert len(loc.sent) == 2
        loc.stop_and_replay()

    def test_tenant_is_refused(self, tmp_path):
        loc = Local(tmp_path)
        p = params("summarize")
        p["tenant"] = "b"
        assert loc.post(rpc("SendMessage", p)).body["error"]["code"] == -32602
        for method in ("GetTask", "CancelTask"):
            assert loc.post(rpc(method, {"id": "up-1", "tenant": "b"})).body["error"][
                "code"] == -32602
        assert loc.sent == []
        loc.stop_and_replay()

    def test_approval_rule_with_a_set_receiver(self, tmp_path):
        """`OPERATOR -> SUMMARIZER[*] : approve` creates approval. In front
        of SUMMARIZER[1] the gateway names the receiver SUMMARIZER[1]: an
        approval through the inbox closes the hold, bound to the arguments,
        and an A2A message named approve is refused."""
        f = workers_file(tmp_path)
        approvers = {"OPERATOR": APPROVER_KEYS["OPERATOR"]}
        loc = Local(tmp_path, cadl=f, agent="SUMMARIZER[1]", inbox=tmp_path / "inbox",
                    approver_keys=approvers)
        assert [(e, to, n) for e, _, to, n in loc.gw.approval_rules] == [
            ("approval", "SUMMARIZER[1]", "approve"), ("rejection", "SUMMARIZER[1]", "reject")]
        held = loc.post(rpc("SendMessage", params("publish", returnImmediately=True)))
        gw_id = held.body["result"]["task"]["id"]
        reply = loc.post(rpc("SendMessage", params("approve", args=["h-1"])),
                         actor="OPERATOR")
        assert reply.body["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
        assert loc.sent == [] and loc.gw.runtime.hold_state("h-1") == "held"
        from cadl.gateway_core import _drop
        _drop(tmp_path / "inbox", approval_file(tmp_path / "inbox", "h-1", "approve",
                                                "OPERATOR", approvers["OPERATOR"]))
        loc.gw.tick()
        assert loc.gw.runtime.hold_state("h-1") == "approved"
        wait_for(lambda: loc.gw.tasks[gw_id].upstream_id == "up-1", "the release")
        assert [m["method"] for m in loc.sent] == ["SendMessage"]
        events = host_events(loc.audit)
        assert ("unauthorised_message", "approval_as_message") in events
        assert [e for e, _ in events].count("approval_evidence") == 1
        loc.stop_and_replay()

    def test_unapprovable_escalation_refused_at_start(self, tmp_path):
        """A receiver set that does not contain the gateway's actor: the
        holds of the contract could never be approved, so neither gateway
        starts."""
        f = workers_file(tmp_path, receiver="SUMMARIZER[2..2]")
        with pytest.raises(GatewayError, match="could never be approved"):
            Local(tmp_path, cadl=f, agent="SUMMARIZER[1]")
        assert not (tmp_path / "audit.jsonl").exists()
        Local(tmp_path, cadl=f, agent="SUMMARIZER[2]").gw.stop()    # a member: starts
        g = tmp_path / "mcp.cadl"
        g.write_text(ROOT.joinpath("examples", "agents_tool_governance.cadl").read_text()
                     .replace("- id: PLANNER\n", "- id: PLANNER[1..2]\n")
                     .replace("parties: [OPERATOR, PLANNER, SUMMARIZER, FILES, TICKETS]",
                              'parties: [OPERATOR, "PLANNER[*]", SUMMARIZER, FILES, TICKETS]')
                     .replace("-> PLANNER :", "-> PLANNER[*] :")
                     .replace("bearer: PLANNER", "bearer: PLANNER[1]")
                     .replace('"PLANNER ->', '"PLANNER[1] ->')
                     .replace("to: [PLANNER, SUMMARIZER]", 'to: ["PLANNER[1]", SUMMARIZER]'))
        from cadl.mcp_gateway import Gateway
        with pytest.raises(GatewayError, match="could never be approved"):
            Gateway(g, "PLANNER[1]", "FILES", tmp_path / "m.jsonl", tmp_path / "m.trace.jsonl",
                    to_client=lambda _: None, to_server=lambda _: None)

    def test_unauthenticated_records_are_limited(self, tmp_path):
        loc = Local(tmp_path)
        for _ in range(25):
            assert loc.gw.handle_post("/", headers(), b"{}").status == 401
        loc.t[0] = 1.0
        assert loc.gw.handle_post("/", headers(), b"{}").status == 401
        reasons = [r for e, r in host_events(loc.audit) if e == "unauthenticated_request"]
        assert reasons == ["missing"] * 20 + ["missing;not_recorded_before=5"]
        loc.stop_and_replay()

    def test_invalid_upstream_port(self, tmp_path):
        with pytest.raises(GatewayError, match="port"):
            A2AGateway(CADL, "SUMMARIZER", "http://127.0.0.1:99999/", dict(CLIENT_KEYS),
                       tmp_path / "a.jsonl", tmp_path / "a.trace.jsonl")


class TestSecondReview:
    """Defects and hardening from the second review."""

    def test_close_with_junk_and_half_close(self, tmp_path, capsys):
        """A client that sends more bytes and then closes, and a client
        that half-closes (SHUT_WR), both withdraw their held request."""
        r = Run(tmp_path)

        def held_request(id_):
            body = json.dumps(rpc("SendMessage", params("publish"), id_)).encode()
            sock = socket.create_connection(("127.0.0.1", r.client.port))
            auth = authorization(CLIENT_KEYS["PLANNER"], "PLANNER", r.run_id, body)
            sock.sendall((f"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                          f"A2A-Version: 1.0\r\nContent-Length: {len(body)}\r\n"
                          f"Authorization: {auth}\r\n\r\n").encode() + body)
            return sock

        s1 = held_request(1)
        h1 = r.wait_hold("h-1")
        s1.sendall(b"X" * 100)
        s1.close()
        wait_for(lambda: not h1.exists(), "the withdrawal after junk")
        s2 = held_request(2)
        h2 = r.wait_hold("h-2")
        s2.shutdown(socket.SHUT_WR)
        wait_for(lambda: not h2.exists(), "the withdrawal after a half-close")
        s2.close()
        r.stop()
        assert r.agent.sent() == []
        assert [h["state"] for h in r.replay_check()["holds"]] == ["withdrawn", "withdrawn"]
        capsys.readouterr()

    def test_cancel_after_the_deadline(self, tmp_path):
        """CancelTask after the deadline, before the ticker saw it: the hold
        expired, and the answer says so."""
        loc = Local(tmp_path)
        tid = loc.post(rpc("SendMessage", params("publish", returnImmediately=True))).body[
            "result"]["task"]["id"]
        loc.t[0] = 601.0
        task = loc.post(rpc("CancelTask", {"id": tid})).body["result"]
        assert task["status"]["state"] == "TASK_STATE_REJECTED"
        assert "expired" in task["status"]["message"]["parts"][0]["text"]
        assert loc.gw.runtime.hold_state("h-1") == "expired"
        loc.stop_and_replay()

    def test_cancel_is_recorded(self, tmp_path):
        loc = Local(tmp_path)
        up = loc.post(rpc("SendMessage", params("summarize"))).body["result"]["task"]["id"]
        tid = loc.post(rpc("SendMessage", params("publish", returnImmediately=True))).body[
            "result"]["task"]["id"]
        assert loc.post(rpc("CancelTask", {"id": tid})).body["result"]["status"]["state"] \
            == "TASK_STATE_CANCELED"
        loc.post(rpc("CancelTask", {"id": up}))
        recs = [r for r in read_records(loc.audit) if r.get("event") == "task_cancel"]
        assert [(r["reason"], r["hold_id"]) for r in recs] == [
            (f"gateway task {tid}", "h-1"), (f"agent task {up} (PLANNER)", None)]
        assert recs[0]["message"]["name"] == "publish"
        assert [m["method"] for m in loc.sent] == ["SendMessage", "CancelTask"]
        loc.stop_and_replay()

    def test_agent_card_declares_the_extension(self, tmp_path):
        with pytest.raises(GatewayError, match="does not declare the extension"):
            Local(tmp_path, agent_card={"name": "x"})
        loc = Local(tmp_path, agent_card={"capabilities": {"extensions": [
            {"uri": EXTENSION_URI}]}})
        assert loc.gw.handle_get("/.well-known/agent-card.json").status == 200
        loc.gw.stop()

    def test_token_over_plain_http(self, tmp_path):
        with pytest.raises(GatewayError, match="https"):
            A2AGateway(CADL, "SUMMARIZER", "http://192.0.2.1/", dict(CLIENT_KEYS),
                       tmp_path / "a.jsonl", tmp_path / "a.trace.jsonl",
                       upstream_token="secret-token")
        A2AGateway(CADL, "SUMMARIZER", "https://192.0.2.1/", dict(CLIENT_KEYS),
                   tmp_path / "b.jsonl", tmp_path / "b.trace.jsonl",
                   upstream_token="secret-token").stop()

    def test_nonce_limit(self, tmp_path):
        loc = Local(tmp_path, max_nonces=2)
        assert loc.post(rpc("SendMessage", params("summarize"))).status == 200
        assert loc.post(rpc("SendMessage", params("summarize"), 2)).status == 200
        assert loc.post(rpc("SendMessage", params("summarize"), 3)).status == 401
        assert host_events(loc.audit)[-1] == ("unauthenticated_request", "nonce_limit")
        loc.stop_and_replay()

    def test_released_forward_that_raises(self, tmp_path, monkeypatch):
        loc = Local(tmp_path, inbox=tmp_path / "inbox",
                    approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]})
        tid = loc.post(rpc("SendMessage", params("publish", returnImmediately=True))).body[
            "result"]["task"]["id"]

        def boom(*a, **k):
            raise RuntimeError("injected")
        monkeypatch.setattr(loc.gw, "_forward", boom)
        from cadl.gateway_core import _drop
        _drop(tmp_path / "inbox", approval_file(tmp_path / "inbox", "h-1", "approve",
                                                "OPERATOR", APPROVER_KEYS["OPERATOR"]))
        loc.gw.tick()
        wait_for(lambda: loc.gw.tasks[tid].state == "TASK_STATE_FAILED", "the failed task")
        assert ("forward_failed", "RuntimeError: injected") in host_events(loc.audit)
        loc.stop_and_replay()


def test_overloaded(tmp_path, capsys):
    """Beyond --max-handlers requests at the same time (here 1, taken by a
    held blocking request), the gateway answers 503 and records it."""
    r = Run(tmp_path, extra=("--max-handlers", "1"))
    fut = r.later("PLANNER", "SendMessage", params("publish"))
    r.wait_hold("h-1")
    status, headers_, _ = r.client.post("PLANNER", rpc("SendMessage", params("summarize")))
    assert status == 503 and headers_["Retry-After"] == "1"
    r.approve("h-1", "reject")
    assert state(fut.result(timeout=30)) == "TASK_STATE_REJECTED"
    r.stop()
    assert any(e == "overloaded" for e, _ in
               ((x["event"], x["reason"]) for x in read_records(r.audit) if x.get("event")))
    r.replay_check()
    capsys.readouterr()
