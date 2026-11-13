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
# The gateway sends itself SIGTERM at the moment it writes the port file,
# as a test that stops it as soon as the file appears may do.
BOOT_TERM_AT_PORT_FILE = """
import os, signal, sys
import cadl.a2a_gateway as g
class _Os:
    def __getattr__(self, name):
        return getattr(os, name)
    def replace(self, src, dst):
        os.replace(src, dst)
        if os.path.basename(str(dst)) == "port":
            os.kill(os.getpid(), signal.SIGTERM)
g.os = _Os()
from cadl.cli import main
sys.exit(main(sys.argv[1:]))
"""
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
                 extra=(), name="gw", boot=BOOT, wait=True):
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
        self.proc = subprocess.Popen([sys.executable, "-c", boot, *args],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not wait:
            return
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

    def test_sigterm_as_soon_as_the_port_file_appears(self, tmp_path):
        """The handlers of SIGTERM and SIGINT are installed before the port
        file is written: a SIGTERM at that moment stops the gateway in
        order (status 0). Up to 0.3.29 it killed it (-15), which made the
        tests that stop a gateway right after a few calls fail now and then."""
        r = Run(tmp_path, boot=BOOT_TERM_AT_PORT_FILE, wait=False)
        try:
            code = r.proc.wait(timeout=60)
            err = r.proc.stderr.read().decode()
        finally:
            r.agent.close()
        assert code == 0, err
        assert r.port_file.exists() and "listening on" in err

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
        # Bound but never listening: connections are refused, and the port
        # cannot be taken by another server while the test runs (closing
        # it first was a race under load).
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        try:
            r = Run(tmp_path, upstream=f"http://127.0.0.1:{port}/")
            body = r.call("PLANNER", "SendMessage", params("summarize"))
            assert body["error"]["code"] == -32603
            r.stop()
        finally:
            sock.close()
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
        for method, code in (("ListTasks", -32004), ("GetExtendedAgentCard", -32004),
                             ("CreateTaskPushNotificationConfig", -32003),
                             ("message/send", -32601), ("tasks/get", -32601)):
            assert loc.post(rpc(method, p)).body["error"]["code"] == code
        assert loc.gw.handle_post("/other", headers(), b"{}").status == 404
        assert loc.gw.handle_get("/.well-known/agent-card.json").status == 404
        assert loc.sent == []
        assert [r["reason"] for r in read_records(loc.audit)
                if r.get("event") == "method_refused"] == [
            "ListTasks", "GetExtendedAgentCard",
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

    def test_set_receiver_without_the_gateways_actor(self, tmp_path):
        """A receiver range without the gateway's actor resolves to its first
        member: the inbox approval is given to the runtime only."""
        f = workers_file(tmp_path, receiver="SUMMARIZER[2..2]")
        approvers = {"OPERATOR": APPROVER_KEYS["OPERATOR"]}
        loc = Local(tmp_path, cadl=f, agent="SUMMARIZER[1]", inbox=tmp_path / "inbox",
                    approver_keys=approvers)
        assert {to for _, _, to, _ in loc.gw.approval_rules} == {"SUMMARIZER[2]"}
        loc.post(rpc("SendMessage", params("publish", returnImmediately=True)))
        from cadl.gateway_core import _drop
        _drop(tmp_path / "inbox", approval_file(tmp_path / "inbox", "h-1", "approve",
                                                "OPERATOR", approvers["OPERATOR"]))
        loc.gw.tick()
        assert loc.gw.runtime.hold_state("h-1") == "approved"
        wait_for(lambda: loc.sent, "the release")
        loc.stop_and_replay()

    @pytest.mark.parametrize("variant", ["summarizer_set", "planner_set"])
    def test_mcp_set_receivers(self, tmp_path, variant):
        """Two files that 0.3.24 accepted: `OPERATOR -> SUMMARIZER[*] :
        approve` in front of FILES, and `OPERATOR -> PLANNER[*] : approve`
        with the client PLANNER[1]. Both start, and an approval through the
        inbox releases the call, bound to its arguments."""
        from cadl.gateway_core import _drop
        from cadl.mcp_gateway import Gateway
        text = ROOT.joinpath("examples", "agents_tool_governance.cadl").read_text()
        agent = "PLANNER"
        if variant == "summarizer_set":
            text = (text.replace("- id: SUMMARIZER\n", "- id: SUMMARIZER[1..2]\n")
                    .replace("PLANNER, SUMMARIZER, FILES", 'PLANNER, "SUMMARIZER[*]", FILES')
                    .replace("to: [PLANNER, SUMMARIZER]", 'to: [PLANNER, "SUMMARIZER[*]"]')
                    .replace("OPERATOR -> PLANNER : approve", "OPERATOR -> SUMMARIZER[*] : approve")
                    .replace("OPERATOR -> PLANNER : reject", "OPERATOR -> SUMMARIZER[*] : reject"))
            want = "SUMMARIZER[1]"
        else:
            text = (text.replace("- id: PLANNER\n", "- id: PLANNER[1..2]\n")
                    .replace("parties: [OPERATOR, PLANNER, SUMMARIZER, FILES, TICKETS]",
                             'parties: [OPERATOR, "PLANNER[*]", SUMMARIZER, FILES, TICKETS]')
                    .replace("-> PLANNER :", "-> PLANNER[*] :")
                    .replace("bearer: PLANNER", "bearer: PLANNER[1]")
                    .replace('"PLANNER ->', '"PLANNER[1] ->')
                    .replace("to: [PLANNER, SUMMARIZER]", 'to: ["PLANNER[1]", SUMMARIZER]'))
            agent, want = "PLANNER[1]", "PLANNER[1]"
        g = tmp_path / "mcp.cadl"
        g.write_text(text)
        out: list = []
        inbox = tmp_path / "inbox"
        gw = Gateway(g, agent, "FILES", tmp_path / "m.jsonl", tmp_path / "m.trace.jsonl",
                     to_client=out.append, to_server=out.append, inbox=inbox,
                     approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]})
        assert {to for _, _, to, _ in gw.approval_rules} == {want}
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "delete_file",
                                              "arguments": {"path": "/x"}}}))
        assert list(gw.held) == ["h-1"]
        _drop(inbox, approval_file(inbox, "h-1", "approve", "OPERATOR",
                                   APPROVER_KEYS["OPERATOR"]))
        gw.tick()
        assert gw.runtime.hold_state("h-1") == "approved"
        assert any('"tools/call"' in x for x in out)
        gw.stop()
        replayed = tmp_path / "r.jsonl"
        assert main(["agents-run", str(g), "--trace", str(tmp_path / "m.trace.jsonl"),
                     "--audit", str(replayed)]) == 0
        assert replayed.read_bytes() == (tmp_path / "m.jsonl").read_bytes()

    def test_escalation_without_approval_rule_refused(self, tmp_path):
        f = tmp_path / "noapproval.cadl"
        text = CADL.read_text()
        text = text.replace('        - { when: "OPERATOR -> SUMMARIZER : approve", creates: approval }\n', "")
        f.write_text(text)
        # G-9 makes the file an error; the gateway's own check is a second line.
        with pytest.raises(GatewayError, match="counts_as rule that creates '?approval"):
            Local(tmp_path, cadl=f)
        assert not (tmp_path / "audit.jsonl").exists()

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
        """The cap is per actor: PLANNER reaching it does not lock out
        OPERATOR."""
        loc = Local(tmp_path, max_nonces=2)
        assert loc.post(rpc("SendMessage", params("summarize"))).status == 200
        assert loc.post(rpc("SendMessage", params("summarize"), 2)).status == 200
        assert loc.post(rpc("SendMessage", params("summarize"), 3)).status == 401
        assert host_events(loc.audit)[-1] == ("unauthenticated_request", "nonce_limit")
        assert loc.post(rpc("SendMessage", params("translate"), 4),
                        actor="OPERATOR").status == 200
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


def _slow(port: int, n: int) -> list:
    """`n` unauthenticated connections that send headers and one byte of a
    body of 1000000 bytes."""
    out = []
    for _ in range(n):
        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     b"Content-Length: 1000000\r\n\r\n{")
        out.append(sock)
    return out


def _events(path: Path, event: str) -> list:
    return [r["reason"] for r in read_records(path) if r.get("event") == event]


class TestThirdReview:
    """Findings of the third review."""

    def test_slow_loris_does_not_lock_out_a_client(self, tmp_path, capsys):
        """Slow unauthenticated connections take no handler slot: with two
        handler slots and ten slow connections, an authenticated client is
        served at once, and the slow connections are closed at the read
        deadline."""
        r = Run(tmp_path, extra=("--max-handlers", "2", "--read-timeout", "1",
                                 "--max-readers-per-address", "64"))
        socks = _slow(r.client.port, 10)
        time.sleep(0.2)
        body = r.call("PLANNER", "SendMessage", params("summarize"))
        assert body["result"]["task"]["id"] == "up-task-1"
        wait_for(lambda: len(_events(r.audit, "read_timeout")) >= 10, "the read deadlines")
        for sock in socks:
            sock.settimeout(5)
            assert sock.recv(100) == b""                # closed by the gateway
            sock.close()
        r.stop()
        r.replay_check()
        capsys.readouterr()

    def test_reader_cap_and_trickle(self, tmp_path, capsys):
        """More slow connections than readers: the next connection waits a
        second for a reader slot and gets 503; a slow connection that keeps sending a byte now and then is
        still closed at the deadline, and the client is served after it."""
        r = Run(tmp_path, extra=("--max-readers", "3", "--read-timeout", "3",
                                 "--max-readers-per-address", "64"))
        socks = _slow(r.client.port, 3)
        time.sleep(0.2)
        status, _, _ = r.client.post("PLANNER", rpc("SendMessage", params("summarize")))
        assert status == 503
        for _ in range(3):
            time.sleep(0.4)
            for sock in socks:
                try:
                    sock.sendall(b" ")
                except OSError:
                    pass
        wait_for(lambda: len(_events(r.audit, "read_timeout")) >= 3, "the read deadlines")
        body = r.call("PLANNER", "SendMessage", params("summarize"), id_=2)
        assert body["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        for sock in socks:
            sock.close()
        r.stop()
        assert "readers" in _events(r.audit, "overloaded")
        assert r.agent.sent() == ["summarize"]
        r.replay_check()
        capsys.readouterr()

    def test_handlers_per_actor(self, tmp_path, capsys):
        """One actor uses at most --max-handlers-per-actor slots (a held
        blocking request keeps its slot); other actors are served."""
        r = Run(tmp_path, extra=("--max-handlers-per-actor", "1"))
        fut = r.later("PLANNER", "SendMessage", params("publish"))
        r.wait_hold("h-1")
        status, _, _ = r.client.post("PLANNER", rpc("SendMessage", params("summarize"), 2))
        assert status == 503                         # refused before a decision
        body = r.call("OPERATOR", "SendMessage", params("translate"), 3)
        assert body["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        r.approve("h-1", "reject")
        assert state(fut.result(timeout=30)) == "TASK_STATE_REJECTED"
        assert r.call("PLANNER", "SendMessage", params("summarize"), 4)["result"]["task"]
        r.stop()
        assert _events(r.audit, "overloaded") == ["handlers_per_actor PLANNER"]
        assert r.agent.sent() == ["translate", "summarize"]
        r.replay_check()
        capsys.readouterr()

    def test_holds_per_actor(self, tmp_path):
        """Holds of returnImmediately requests count too: beyond
        --max-holds-per-actor the actor gets 503 before any decision."""
        loc = Local(tmp_path, max_holds_per_actor=2)
        for i in (1, 2):
            reply = loc.post(rpc("SendMessage", params("publish", returnImmediately=True), i))
            assert reply.body["result"]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
        reply = loc.post(rpc("SendMessage", params("publish", returnImmediately=True), 3))
        assert reply.status == 503
        assert loc.post(rpc("SendMessage", params("summarize"), 4)).status == 503
        assert loc.post(rpc("SendMessage", params("translate"), 5),
                        actor="OPERATOR").status == 200
        assert len(loc.gw.held) == 2 and len(loc.gw.runtime.steps) == 3
        assert _events(loc.audit, "overloaded") == ["holds_per_actor PLANNER"] * 2
        loc.stop_and_replay()

    def test_reconnecting_peer_of_one_address(self, tmp_path):
        """Sixteen threads of one address that reconnect whenever the gateway
        closes them hold at most four reader slots; the clients of other
        addresses are served. (All connections come from 127.0.0.1, so the
        test names the attacker's address by its source ports.)"""
        import threading
        from cadl.a2a_gateway import _Server
        loc = Local(tmp_path)
        loc.gw.clock = time.monotonic
        loc.gw.t0 = time.monotonic()
        attacker_ports: set = set()
        srv = _Server(("127.0.0.1", 0), loc.gw, socket.AF_INET, max_readers=8,
                      read_timeout=1.0)
        srv.peer_key = lambda addr: "attacker" if addr[1] in attacker_ports else addr
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        stop = threading.Event()

        def attacker():
            while not stop.is_set():
                try:
                    s = socket.socket()
                    s.bind(("127.0.0.1", 0))
                    attacker_ports.add(s.getsockname()[1])
                    s.connect(("127.0.0.1", port))
                    s.sendall(b"POST / HTTP/1.1\r\nContent-Length: 10\r\n\r\n")
                    s.settimeout(3)
                    s.recv(1)
                    s.close()
                except OSError:
                    pass

        threads = [threading.Thread(target=attacker, daemon=True) for _ in range(16)]
        for t in threads:
            t.start()
        time.sleep(0.5)
        client = Client(port, loc.gw.run_id)
        statuses = [client.post("PLANNER", rpc("SendMessage", params("summarize"), i))[0]
                    for i in range(20)]
        stop.set()
        for t in threads:
            t.join(timeout=10)
        srv.shutdown()
        srv.server_close()
        assert statuses == [200] * 20
        assert "address" in _events(loc.audit, "overloaded")
        loc.stop_and_replay()


def test_peer_key_groups_ipv6_by_prefix():
    from cadl.a2a_gateway import _Server
    k = _Server.peer_key
    assert k(("2001:db8::1", 1, 0, 0)) == k(("2001:db8::ffff:2", 2, 0, 0))
    assert k(("2001:db8::1", 1, 0, 0)) != k(("2001:db8:0:1::1", 1, 0, 0))
    assert k(("::ffff:192.0.2.7", 1, 0, 0)) == k(("192.0.2.7", 5)) == "192.0.2.7"
    assert k(("192.0.2.7", 1)) != k(("192.0.2.8", 1))
    assert k(("fe80::1%lo0", 1, 0, 0)) == "fe80::/64"


def _open_stream(r: "Run", actor: str, p, id_=1, method="SendStreamingMessage"):
    """A streamed request on a raw socket, read by the test as it comes."""
    body = json.dumps(rpc(method, p, id_)).encode()
    sock = socket.create_connection(("127.0.0.1", r.client.port))
    auth = authorization(CLIENT_KEYS[actor], actor, r.run_id, body)
    sock.sendall((f"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  f"A2A-Version: 1.0\r\nContent-Length: {len(body)}\r\n"
                  f"Authorization: {auth}\r\n\r\n").encode() + body)
    sock.settimeout(30)
    return sock


def read_sse(r: "Run", actor: str, method: str, p, id_=1, timeout=30.0):
    """A streamed request: the status, the content type, the comments and
    the events (event name, JSON) of the answer."""
    body = json.dumps(rpc(method, p, id_)).encode()
    conn = http.client.HTTPConnection("127.0.0.1", r.client.port, timeout=timeout)
    try:
        conn.request("POST", "/", body=body, headers={
            "Content-Type": "application/json", "A2A-Version": "1.0",
            "Authorization": authorization(CLIENT_KEYS[actor], actor, r.run_id, body)})
        resp = conn.getresponse()
        ctype = resp.getheader("Content-Type")
        data = resp.read().decode()
    finally:
        conn.close()
    if not ctype.startswith("text/event-stream"):
        return resp.status, ctype, [], [("json", json.loads(data))]
    comments, events, name, lines = [], [], "message", []
    for line in data.split("\n"):
        if not line:
            if lines:
                events.append((name, json.loads("\n".join(lines))))
            name, lines = "message", []
        elif line.startswith(":"):
            comments.append(line)
        elif line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            lines.append(line[6:])
    return resp.status, ctype, comments, events


class TestStreaming:
    """SendStreamingMessage and SubscribeToTask (SSE, JSON-RPC binding)."""

    def test_allow_deny_and_events(self, tmp_path, capsys):
        r = Run(tmp_path)
        status, ctype, comments, events = read_sse(r, "PLANNER", "SendStreamingMessage",
                                                   params("summarize"), id_="s1")
        assert status == 200 and ctype.startswith("text/event-stream")
        assert comments == []                   # the agent's comment is not passed on
        assert [next(iter(e["result"])) for _, e in events] == [
            "task", "statusUpdate", "artifactUpdate", "statusUpdate"]
        assert {e["id"] for _, e in events} == {"s1"}
        _, _, _, events = read_sse(r, "PLANNER", "SendStreamingMessage", params("translate"))
        assert len(events) == 1
        assert events[0][1]["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
        # Own tasks only, for subscriptions too.
        _, _, _, events = read_sse(r, "OPERATOR", "SubscribeToTask", {"id": "up-task-1"})
        assert events[0][1]["error"]["code"] == -32001
        _, _, _, events = read_sse(r, "PLANNER", "SubscribeToTask", {"id": "up-task-1"}, 7)
        assert [e["result"]["task"]["id"] for _, e in events[:1]] == ["up-task-1"]
        r.stop()
        assert r.agent.sent("SendStreamingMessage") == ["summarize"]
        assert r.agent.sent("SubscribeToTask") == ["up-task-1"]
        result = r.replay_check()
        assert decisions(result) == [("PLANNER", "summarize", "allow", "permission"),
                                     ("PLANNER", "translate", "deny", "default")]
        capsys.readouterr()

    def test_held_stream_approved_and_rejected(self, tmp_path, capsys):
        r = Run(tmp_path)
        sock = _open_stream(r, "PLANNER", params("publish", "x"))
        seen = b""
        while b": held as h-1" not in seen:              # wait for state, not time
            chunk = sock.recv(4096)
            assert chunk, "the stream ended while held"
            seen += chunk
        assert r.agent.sent("SendStreamingMessage") == []
        r.approve("h-1")
        while not seen.endswith(b"0\r\n\r\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            seen += chunk
        sock.close()
        events = [(None, json.loads(line[6:])) for line in seen.decode().split("\n")
                  if line.startswith("data: ")]
        assert events[-1][1]["result"]["statusUpdate"]["status"]["state"] == \
            "TASK_STATE_COMPLETED"
        fut = r.pool.submit(read_sse, r, "PLANNER", "SendStreamingMessage", params("publish"))
        r.approve("h-2", "reject")
        _, _, _, events = fut.result(timeout=30)
        assert [e["result"]["task"]["status"]["state"] for _, e in events] == [
            "TASK_STATE_REJECTED"]
        r.stop()
        assert r.agent.sent("SendStreamingMessage") == ["publish"]
        assert [h["state"] for h in r.replay_check()["holds"]] == ["approved", "rejected"]
        capsys.readouterr()

    def test_held_stream_withdrawn_on_disconnect(self, tmp_path, capsys):
        r = Run(tmp_path)
        body = json.dumps(rpc("SendStreamingMessage", params("publish"))).encode()
        sock = socket.create_connection(("127.0.0.1", r.client.port))
        auth = authorization(CLIENT_KEYS["PLANNER"], "PLANNER", r.run_id, body)
        sock.sendall((f"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      f"A2A-Version: 1.0\r\nContent-Length: {len(body)}\r\n"
                      f"Authorization: {auth}\r\n\r\n").encode() + body)
        hold = r.wait_hold("h-1")
        late = approval_file(r.inbox, "h-1", "approve", "OPERATOR", APPROVER_KEYS["OPERATOR"])
        sock.close()
        wait_for(lambda: not hold.exists(), "the withdrawal")
        (r.inbox / "late.json").write_text(json.dumps(late))
        r.wait_inbox_done()
        r.stop()
        assert r.agent.sent("SendStreamingMessage") == []
        assert [h["state"] for h in r.replay_check()["holds"]] == ["withdrawn"]
        capsys.readouterr()

    def test_returns_immediately_and_subscription(self, tmp_path, capsys):
        r = Run(tmp_path)
        _, _, _, events = read_sse(r, "PLANNER", "SendStreamingMessage",
                                   params("publish", returnImmediately=True))
        task = events[0][1]["result"]["task"]
        assert len(events) == 1 and task["status"]["state"] == "TASK_STATE_SUBMITTED"
        fut = r.pool.submit(read_sse, r, "PLANNER", "SubscribeToTask", {"id": task["id"]}, 2)
        time.sleep(0.3)
        r.approve("h-1")
        _, _, _, events = fut.result(timeout=30)
        ids = {(e["result"].get("task") or {}).get("id") for _, e in events if "task" in
               e["result"]} | {e["result"][k]["taskId"] for _, e in events
                               for k in ("statusUpdate", "artifactUpdate") if k in e["result"]}
        assert ids == {task["id"]}               # the gateway's id, not the agent's
        r.stop()
        assert r.agent.sent() == ["publish"] and r.agent.sent("SubscribeToTask") == ["up-task-1"]
        r.replay_check()
        capsys.readouterr()

    @pytest.mark.parametrize("text,why,good", [("stream-foreign", "not of a task", 1),
                                               ("stream-cr", "carriage return", 2)])
    def test_bad_events_end_the_stream(self, tmp_path, capsys, text, why, good):
        r = Run(tmp_path)
        _, _, _, events = read_sse(r, "PLANNER", "SendStreamingMessage",
                                   params("summarize", text))
        name, last = events[-1]
        assert name == "error" and last["error"]["code"] == -32006
        assert all("result" in e for _, e in events[:-1])
        assert len(events) == good + 1         # nothing after the bad event
        r.stop()
        assert any(why in (x["reason"] or "") for x in read_records(r.audit)
                   if x.get("event") == "invalid_response")
        r.replay_check()
        capsys.readouterr()


HOLD_KEY = b"gateway-hold-key-0123456789abcdef"


class TestSignedHolds:
    """--hold-key: the gateway signs every published hold, and mcp-approve
    verifies it before it shows or signs it."""

    def _gw(self, tmp_path, hold_key=HOLD_KEY):
        return Local(tmp_path, inbox=tmp_path / "inbox",
                     approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]},
                     hold_key=hold_key)

    def _hold(self, loc, tmp_path):
        loc.post(rpc("SendMessage", params("publish", "real", returnImmediately=True)))
        path = tmp_path / "inbox" / "holds" / "h-1.json"
        return path, json.loads(path.read_text())

    def _approve(self, loc, tmp_path, hold_key):
        from cadl.gateway_core import _drop
        obj = approval_file(tmp_path / "inbox", "h-1", "approve", "OPERATOR",
                            APPROVER_KEYS["OPERATOR"], hold_key)
        _drop(tmp_path / "inbox", obj)
        loc.gw.tick()

    def test_signed_hold_verifies(self, tmp_path):
        from cadl.gateway_core import hold_mac
        loc = self._gw(tmp_path)
        path, hold = self._hold(loc, tmp_path)
        assert hold["kind"] == "hold" and hold["hold_mac"] == hold_mac(HOLD_KEY, hold)
        run = json.loads((tmp_path / "inbox" / "run.json").read_text())
        assert run["holds_signed"] is True
        self._approve(loc, tmp_path, HOLD_KEY)
        assert loc.gw.runtime.hold_state("h-1") == "approved"
        loc.stop_and_replay()

    @pytest.mark.parametrize("forge", ["params", "strip_mac", "other_key"])
    def test_forged_hold_is_refused(self, tmp_path, forge):
        """Someone who can write the inbox replaces the published hold: other
        arguments with their own SHA-256, the MAC removed, or a MAC under
        another key. mcp-approve refuses to show or sign it."""
        import hashlib

        from cadl.audit_log import canonical
        from cadl.gateway_core import hold_mac
        loc = self._gw(tmp_path)
        path, hold = self._hold(loc, tmp_path)
        hold["params"]["message"]["parts"] = [{"text": "harmless"}]
        hold["params_sha256"] = hashlib.sha256(canonical(hold["params"])).hexdigest()
        if forge == "strip_mac":
            del hold["hold_mac"]
        elif forge == "other_key":
            hold["hold_mac"] = hold_mac(b"x" * 32, hold)
        path.write_text(json.dumps(hold))
        with pytest.raises(GatewayError, match="not signed"):
            approval_file(tmp_path / "inbox", "h-1", "approve", "OPERATOR",
                          APPROVER_KEYS["OPERATOR"], HOLD_KEY)
        if forge != "strip_mac":                       # a signed hold needs the key
            with pytest.raises(GatewayError, match="give --hold-key"):
                approval_file(tmp_path / "inbox", "h-1", "approve", "OPERATOR",
                              APPROVER_KEYS["OPERATOR"], None)
        (tmp_path / "op.key").write_bytes(APPROVER_KEYS["OPERATOR"])
        (tmp_path / "hold.key").write_bytes(HOLD_KEY)
        assert main(["mcp-approve", str(tmp_path / "inbox"), "h-1", "approve", "--as",
                     "OPERATOR", "--key", str(tmp_path / "op.key"), "--hold-key",
                     str(tmp_path / "hold.key")]) == 2
        assert loc.gw.runtime.hold_state("h-1") == "held"
        loc.stop_and_replay()

    def test_shown_arguments_must_match_their_hash(self, tmp_path):
        """Without --hold-key, mcp-approve still checks that the arguments
        it shows have the SHA-256 it signs; a forged hold whose arguments
        and hash agree is signed, and the gateway refuses the approval
        (wrong_message), because the hash is not that of the held request."""
        import hashlib

        from cadl.audit_log import canonical
        loc = self._gw(tmp_path, hold_key=None)
        path, hold = self._hold(loc, tmp_path)
        assert "hold_mac" not in hold
        hold["params"]["message"]["parts"] = [{"text": "harmless"}]
        path.write_text(json.dumps(hold))
        with pytest.raises(GatewayError, match="do not have the SHA-256"):
            approval_file(tmp_path / "inbox", "h-1", "approve", "OPERATOR",
                          APPROVER_KEYS["OPERATOR"])
        hold["params_sha256"] = hashlib.sha256(canonical(hold["params"])).hexdigest()
        path.write_text(json.dumps(hold))
        self._approve(loc, tmp_path, None)
        assert loc.gw.runtime.hold_state("h-1") == "held"
        assert ("unauthorised_approval", "wrong_message") in host_events(loc.audit)
        loc.stop_and_replay()

    def test_hold_key_options(self, tmp_path):
        with pytest.raises(GatewayError, match="approver key"):
            self._gw(tmp_path, hold_key=APPROVER_KEYS["OPERATOR"])
        with pytest.raises(GatewayError, match="give --approvals"):
            Local(tmp_path, hold_key=HOLD_KEY)

    def test_mcp_gateway_signs_holds(self, tmp_path):
        from cadl.gateway_core import _drop
        from cadl.mcp_gateway import Gateway
        out: list = []
        inbox = tmp_path / "inbox"
        gw = Gateway(ROOT / "examples" / "agents_tool_governance.cadl", "PLANNER", "FILES",
                     tmp_path / "m.jsonl", tmp_path / "m.trace.jsonl", to_client=out.append,
                     to_server=out.append, inbox=inbox,
                     approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]}, hold_key=HOLD_KEY)
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "delete_file",
                                              "arguments": {"path": "/x"}}}))
        assert "hold_mac" in json.loads((inbox / "holds" / "h-1.json").read_text())
        _drop(inbox, approval_file(inbox, "h-1", "approve", "OPERATOR",
                                   APPROVER_KEYS["OPERATOR"], HOLD_KEY))
        gw.tick()
        assert gw.runtime.hold_state("h-1") == "approved"
        gw.stop()


class TestLowFindings:
    """The Low findings of the fifth review."""

    def test_index_variable_receiver_uses_the_hold_bindings(self, tmp_path):
        """`OPERATOR -> SUMMARIZER[i] : approve` with the instance binding i
        to 2, at an MCP gateway in front of FILES: the approval through the
        inbox is the message to SUMMARIZER[2], the receiver that the hold's
        contract instance binds, not the first member of the set."""
        from cadl.gateway_core import _drop
        from cadl.mcp_gateway import Gateway
        text = (ROOT / "examples" / "agents_tool_governance.cadl").read_text()
        text = (text.replace("- id: SUMMARIZER\n", "- id: SUMMARIZER[1..2]\n")
                .replace("PLANNER, SUMMARIZER, FILES", 'PLANNER, "SUMMARIZER[i]", FILES')
                .replace("to: [PLANNER, SUMMARIZER]", 'to: [PLANNER, "SUMMARIZER[*]"]')
                .replace("OPERATOR -> PLANNER : approve", "OPERATOR -> SUMMARIZER[i] : approve")
                .replace("OPERATOR -> PLANNER : reject", "OPERATOR -> SUMMARIZER[i] : reject"))
        g = tmp_path / "bound.cadl"
        g.write_text(text)
        inbox = tmp_path / "inbox"
        out: list = []
        gw = Gateway(g, "PLANNER", "FILES", tmp_path / "m.jsonl", tmp_path / "m.trace.jsonl",
                     to_client=out.append, to_server=out.append, inbox=inbox,
                     approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]},
                     init={"bindings": {"i": 2}})
        assert {to for _, _, to, _ in gw.approval_rules} == {"SUMMARIZER[1]"}   # static
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "delete_file",
                                              "arguments": {"path": "/x"}}}))
        _drop(inbox, approval_file(inbox, "h-1", "approve", "OPERATOR",
                                   APPROVER_KEYS["OPERATOR"]))
        gw.tick()
        assert gw.runtime.hold_state("h-1") == "approved"
        gw.stop()
        names = [(r["message"]["to"], r["message"]["name"]) for r in
                 read_records(tmp_path / "m.jsonl") if "decision" in r]
        assert ("SUMMARIZER[2]", "approve") in names
        replayed = tmp_path / "r.jsonl"
        assert main(["agents-run", str(g), "--trace", str(tmp_path / "m.trace.jsonl"),
                     "--audit", str(replayed)]) == 0
        assert replayed.read_bytes() == (tmp_path / "m.jsonl").read_bytes()

    @pytest.mark.parametrize("head", [
        b"Content-Length: 5\r\nTransfer-Encoding : chunked\r\n",
        b"Content-Length: 5\r\nTransfer-Encoding\t: chunked\r\n",
        b"Content-Length: 5\r\nX-Folded: one\r\n two\r\n",
        b"Content-Length: 5\r\nX-Cr: a\rb\r\n",
    ])
    def test_malformed_header_lines(self, tmp_path, head):
        """A header with whitespace before its colon (which the parser of the
        standard library takes as the end of the headers), a folded header
        or a bare carriage return: 400, nothing read as a request."""
        import threading
        from cadl.a2a_gateway import _Server
        loc = Local(tmp_path)
        srv = _Server(("127.0.0.1", 0), loc.gw, socket.AF_INET)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sock = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        sock.sendall(b"POST / HTTP/1.1\r\nHost: x\r\n" + head + b"\r\n{}\r\n")
        sock.settimeout(10)
        answer = sock.recv(64)
        assert answer.split(b" ")[1] == b"400", answer
        sock.close()
        srv.shutdown()
        srv.server_close()
        assert loc.sent == [] and loc.gw.runtime.steps == []
        assert host_events(loc.audit) == []              # not even authenticated
        loc.stop_and_replay()

    def test_caps_per_address_and_prefix(self, tmp_path):
        """A connection beyond the cap of its address, or of its /24, gets 503
        at once. All connections come from 127.0.0.1, so the test names their
        addresses by their source ports."""
        import threading
        from cadl.a2a_gateway import _Server
        loc = Local(tmp_path)
        srv = _Server(("127.0.0.1", 0), loc.gw, socket.AF_INET, max_readers_per_address=1,
                      max_readers_per_prefix=2, read_timeout=5)
        addresses: dict = {}
        srv.peer_key = lambda addr: addresses.get(addr[1], addr)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def connect(address):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            addresses[s.getsockname()[1]] = address
            s.connect(("127.0.0.1", port))
            s.sendall(b"POST / HTTP/1.1\r\nContent-Length: 10\r\n\r\n")
            s.settimeout(3)
            return s

        def status(s):
            try:
                return s.recv(32)[:12]
            except socket.timeout:
                return b"(waiting)"
        first = connect("10.0.0.1")
        wait_for(lambda: srv.address_count.get("10.0.0.1") == 1, "the first reader")
        same = connect("10.0.0.1")                      # the same address
        assert status(same) == b"HTTP/1.1 503"
        second = connect("10.0.0.2")                    # same /24 (127.0.0.0/24)
        wait_for(lambda: sum(srv.prefix_count.values()) == 2, "the second reader")
        third = connect("10.0.0.3")                     # beyond the /24 cap
        assert status(third) == b"HTTP/1.1 503"
        threads = [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
        assert threads == []                            # one thread for all deadlines
        for s in (first, same, second, third):
            s.close()
        srv.shutdown()
        srv.server_close()
        assert sorted(set(_events(loc.audit, "overloaded"))) == ["address", "prefix"]
        loc.stop_and_replay()



class TestSixthReview:
    """The Low findings of the review of 0.3.26."""

    def test_departed_client_of_a_silent_stream(self, tmp_path, capsys):
        """The agent sends some events and then nothing; the client leaves.
        The gateway sees it, ends the request to the agent and frees the
        place of the client (here the only one, --max-handlers-per-actor 1)."""
        r = Run(tmp_path, extra=("--max-handlers-per-actor", "1"))
        sock = _open_stream(r, "PLANNER", params("summarize", "stream-wait"))
        seen = b""
        while b"artifactUpdate" not in seen:
            chunk = sock.recv(4096)
            assert chunk
            seen += chunk
        sock.close()

        def served():
            status, _, body = r.client.post("PLANNER", rpc("SendMessage",
                                                           params("summarize"), 2))
            return status == 200 and body
        body = wait_for(served, "the place of the client to be free")
        assert body["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        r.agent.release.set()
        r.stop()
        r.replay_check()
        capsys.readouterr()

    def test_json_answer_to_a_stream_is_strict(self, tmp_path, capsys):
        r = Run(tmp_path)
        _, _, _, events = read_sse(r, "PLANNER", "SendStreamingMessage",
                                   params("summarize", "stream-json-bad"))
        assert events[-1][0] == "error" and events[-1][1]["error"]["code"] == -32006
        r.stop()
        assert any("not UTF-8" in (x["reason"] or "") for x in read_records(r.audit)
                   if x.get("event") == "invalid_response")
        r.replay_check()
        capsys.readouterr()

    def test_hold_key_is_not_a_client_key(self, tmp_path):
        with pytest.raises(GatewayError, match="client key"):
            Local(tmp_path, inbox=tmp_path / "inbox",
                  approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]},
                  hold_key=CLIENT_KEYS["PLANNER"])

    def test_unsigned_hold_where_holds_are_signed(self, tmp_path):
        """mac and kind stripped: without --hold-key, mcp-approve refuses it
        when run.json says holds_signed, and with --require-signed-hold."""
        loc = Local(tmp_path, inbox=tmp_path / "inbox",
                    approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]}, hold_key=HOLD_KEY)
        loc.post(rpc("SendMessage", params("publish", returnImmediately=True)))
        path = tmp_path / "inbox" / "holds" / "h-1.json"
        hold = json.loads(path.read_text())
        del hold["hold_mac"], hold["kind"]
        hold["deadline"] = "2099-01-01T00:00:00.000Z"
        path.write_text(json.dumps(hold))
        with pytest.raises(GatewayError, match="signed hold is required"):
            approval_file(tmp_path / "inbox", "h-1", "approve", "OPERATOR",
                          APPROVER_KEYS["OPERATOR"])
        (tmp_path / "op.key").write_bytes(APPROVER_KEYS["OPERATOR"])
        assert main(["mcp-approve", str(tmp_path / "inbox"), "h-1", "approve", "--as",
                     "OPERATOR", "--key", str(tmp_path / "op.key")]) == 2
        loc.stop_and_replay()

    def test_require_signed_hold_option(self, tmp_path, capsys):
        loc = Local(tmp_path, inbox=tmp_path / "inbox",
                    approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]})
        loc.post(rpc("SendMessage", params("publish", returnImmediately=True)))
        (tmp_path / "op.key").write_bytes(APPROVER_KEYS["OPERATOR"])
        args = ["mcp-approve", str(tmp_path / "inbox"), "h-1", "approve", "--as", "OPERATOR",
                "--key", str(tmp_path / "op.key")]
        assert main(args + ["--require-signed-hold"]) == 2
        assert "signed hold is required" in capsys.readouterr().err
        assert main(args) == 0                          # unsigned gateway: a warning
        assert "not signed" in capsys.readouterr().err
        loc.stop_and_replay()

    def test_invisible_characters_are_shown_escaped(self, tmp_path, capsys):
        from cadl.gateway_core import visible_json
        text = "rm \u202ecod.exe \x1b[2J\u200b\u2028\x85\U000F0000"
        shown = visible_json({"t": text})
        for ch in ("\u202e", "\x1b", "\u200b", "\u2028", "\x85", "\U000F0000"):
            assert ch not in shown
        assert "\\u202e" in shown and "\\udb80\\udc00" in shown
        assert json.loads(shown) == {"t": text}           # the same value
        loc = Local(tmp_path, inbox=tmp_path / "inbox",
                    approver_keys={"OPERATOR": APPROVER_KEYS["OPERATOR"]})
        loc.post(rpc("SendMessage", params("publish", text, returnImmediately=True)))
        assert main(["mcp-approve", str(tmp_path / "inbox"), "h-1", "approve", "--as",
                     "OPERATOR", "--show"]) == 0
        out = capsys.readouterr().out
        assert "\u202e" not in out and "\\u202ecod.exe" in out
        loc.stop_and_replay()
