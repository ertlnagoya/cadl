"""`cadl a2a-gateway` beyond the JSON-RPC binding (Appendix G, G.11 and
G.13): mutual TLS (`--client-ca`), the HTTP+JSON binding of A2A 1.0.0,
push notifications with their SSRF controls (`--push-allow`), and the
gRPC binding. The gateway runs as a subprocess in front of the fake agent
of `a2a_fake_agent.py`, as in `test_a2a_gateway.py`, and every scenario
ends with the replay check: `cadl agents-run` on the gateway's trace
writes the same audit log, byte for byte. Standard library only, no
network outside 127.0.0.1; the certificates come from the `openssl`
command."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time

import pytest

from cadl.a2a_gateway import authorization, certificate_actor
from cadl.audit_log import read_records

from .a2a_certs import PKI
from .test_a2a_gateway import (
    CLIENT_KEYS,
    Client,
    Run,
    decisions,
    params,
    rpc,
    state,
    wait_for,
)


def events(path, *names) -> list[tuple]:
    return [(r["event"], r["reason"]) for r in read_records(path)
            if r.get("event") in names]


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return PKI(tmp_path_factory.mktemp("pki"),
               clients={"planner": "PLANNER", "operator": "OPERATOR",
                        "intruder": "INTRUDER", "two": "PLANNER/CN=OPERATOR"},
               foreign={"forged": "PLANNER"})


class TLSClient(Client):
    """The fake client of `test_a2a_gateway.py` over TLS, with the client
    certificate `cert` (none for None)."""

    def __init__(self, port: int, run_id: str, pki: PKI, cert: str | None):
        super().__init__(port, run_id)
        self.ctx = pki.context(cert)

    def raw(self, body: bytes, headers: dict, path: str = "/", method: str = "POST",
            timeout: float = 30.0):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ctx,
                                           timeout=timeout)
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
            return resp.status, resp.headers, json.loads(data) if data else None
        finally:
            conn.close()


def _fails(client: TLSClient) -> None:
    """A request that does not get an HTTP answer: the handshake fails (in
    TLS 1.3 the client sees it at its first read)."""
    with pytest.raises((ssl.SSLError, OSError, http.client.HTTPException)):
        client.post("PLANNER", rpc("SendMessage", params("summarize")))


# === 1. Mutual TLS =================================================================

class TestMutualTLS:

    def test_certificate_and_header_name_one_actor(self, tmp_path, pki, capsys):
        r = Run(tmp_path, extra=pki.gateway_args())
        port = r.client.port
        good = TLSClient(port, r.run_id, pki, "planner")
        body = good.call("PLANNER", "SendMessage", params("summarize"))
        assert body["result"]["task"]["id"] == "up-task-1"
        # The same nonce again: the replay protection of CADL-HMAC stays.
        req = json.dumps(rpc("SendMessage", params("summarize"))).encode()
        nonce = "ab" * 16
        assert good.post("PLANNER", req, nonce=nonce)[0] == 200
        assert good.post("PLANNER", req, nonce=nonce)[0] == 401
        # The certificate of OPERATOR with the header of PLANNER.
        other = TLSClient(port, r.run_id, pki, "operator")
        status, _, err = other.post("PLANNER", rpc("SendMessage", params("summarize")))
        assert status == 401 and err["error"]["code"] == -32600
        # A valid certificate and no header, or a header without a key.
        assert other.post(None, rpc("SendMessage", params("summarize")))[0] == 401
        # No certificate, a certificate of another CA, one that names no
        # actor of the gateway, one with two common names: no HTTP answer.
        for cert in (None, "forged", "intruder", "two"):
            _fails(TLSClient(port, r.run_id, pki, cert))
        # Plain HTTP on the TLS port.
        with pytest.raises((ssl.SSLError, OSError, http.client.HTTPException)):
            r.client.post("PLANNER", rpc("SendMessage", params("summarize")))
        r.stop()
        assert r.agent.sent() == ["summarize", "summarize"]
        assert decisions(r.replay_check()) == [
            ("PLANNER", "summarize", "allow", "permission")] * 2
        refused = events(r.audit, "unauthenticated_request")
        assert [why for _, why in refused] == ["replayed", "certificate_mismatch", "missing"]
        failed = [why for _, why in events(r.audit, "handshake_failed")]
        assert failed.count("unknown_certificate") == 2, failed      # intruder, two
        assert len(failed) >= 4, failed     # and no certificate, the other CA, plain HTTP
        capsys.readouterr()

    def test_peers_without_certificate_take_no_reader(self, tmp_path, pki, capsys):
        """With one reader, three connections that never finish their
        handshake do not keep a client with a certificate out; without
        --client-ca one such connection does (503 after a second)."""
        r = Run(tmp_path, extra=(*pki.gateway_args(), "--max-readers", "1",
                                 "--read-timeout", "3"))
        idle = [socket.create_connection(("127.0.0.1", r.client.port)) for _ in range(3)]
        time.sleep(0.2)
        good = TLSClient(r.client.port, r.run_id, pki, "planner")
        t0 = time.monotonic()
        assert good.call("PLANNER", "SendMessage", params("summarize"))["result"]["task"]
        assert time.monotonic() - t0 < 1.0
        # A slow handshake is cut by the read deadline, recorded once.
        wait_for(lambda: len(events(r.audit, "read_timeout")) >= 3, "the read deadlines",
                 timeout=15)
        for s in idle:
            s.close()
        r.stop()
        assert [why for _, why in events(r.audit, "read_timeout")] == ["deadline"] * 3
        assert events(r.audit, "handshake_failed") == []
        assert decisions(r.replay_check()) == [("PLANNER", "summarize", "allow", "permission")]
        # The same without --client-ca: the one reader is taken.
        r = Run(tmp_path, extra=("--tls-cert", str(pki.cert), "--tls-key", str(pki.key),
                                 "--max-readers", "1", "--read-timeout", "3"), name="plain")
        idle = socket.create_connection(("127.0.0.1", r.client.port))
        time.sleep(0.2)
        status, _, _ = TLSClient(r.client.port, r.run_id, pki, None).post(
            "PLANNER", rpc("SendMessage", params("summarize")))
        assert status == 503
        idle.close()
        r.stop()
        assert r.agent.sent() == []
        r.replay_check()
        capsys.readouterr()

    def test_handshake_cap(self, tmp_path, pki, capsys):
        """--max-handshakes: a connection beyond it waits a second for a
        place, then is closed and recorded as `overloaded` (`handshakes`)."""
        r = Run(tmp_path, extra=(*pki.gateway_args(), "--max-handshakes", "1",
                                 "--read-timeout", "4"))
        idle = socket.create_connection(("127.0.0.1", r.client.port))
        time.sleep(0.2)
        _fails(TLSClient(r.client.port, r.run_id, pki, "planner"))
        idle.close()
        wait_for(lambda: events(r.audit, "overloaded"), "the record")
        body = TLSClient(r.client.port, r.run_id, pki, "planner").call(
            "PLANNER", "SendMessage", params("summarize"))
        assert state(body) == "TASK_STATE_COMPLETED"
        r.stop()
        assert events(r.audit, "overloaded") == [("overloaded", "handshakes")]
        r.replay_check()
        capsys.readouterr()

    def test_client_ca_needs_tls(self, tmp_path, pki, capsys):
        from cadl.cli import main
        assert main(["a2a-gateway", "x.cadl", "--agent-actor", "SUMMARIZER", "--upstream",
                     "http://127.0.0.1:1/", "--client-keys", "k.json", "--audit",
                     str(tmp_path / "a.jsonl"), "--client-ca", str(pki.ca)]) == 2
        capsys.readouterr()


def test_certificate_actor():
    def cert(*cns):
        return {"subject": tuple((("commonName", cn),) for cn in cns)}
    assert certificate_actor(cert("PLANNER")) == "PLANNER"
    assert certificate_actor(cert("WORKER[2]")) == "WORKER[2]"
    assert certificate_actor(cert("PLANNER", "OPERATOR")) is None
    assert certificate_actor(cert("not an actor")) is None
    assert certificate_actor({"subject": ((("organizationName", "x"),),)}) is None
    assert certificate_actor({"subject": "garbage"}) is None
    assert certificate_actor(None) is None and certificate_actor({}) is None
    assert authorization(CLIENT_KEYS["PLANNER"], "PLANNER", "r", b"").startswith("CADL-HMAC ")


# === 2. The HTTP+JSON binding ======================================================

def rest(r, actor: str | None, method: str, target: str, body=None, sign: str | None = None,
         version: str | None = "1.0", ctype="application/json", timeout=30.0, client=None):
    """A request of the HTTP+JSON binding: (status, headers, parsed body,
    or for a stream (comments, events))."""
    data = b"" if body is None else (body if isinstance(body, bytes)
                                     else json.dumps(body).encode())
    headers = {}
    if data:
        headers["Content-Type"] = ctype
        headers["Content-Length"] = str(len(data))
    elif method == "POST":
        headers["Content-Length"] = "0"
    if version is not None:
        headers["A2A-Version"] = version
    if actor is not None:
        headers["Authorization"] = authorization(CLIENT_KEYS[actor], actor, r.run_id, data,
                                                 path=sign or target, method=method)
    c = client or r.client
    if isinstance(c, TLSClient):
        conn = http.client.HTTPSConnection("127.0.0.1", c.port, context=c.ctx,
                                           timeout=timeout)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=timeout)
    try:
        conn.putrequest(method, target, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        if data:
            conn.send(data)
        resp = conn.getresponse()
        raw = resp.read().decode()
    finally:
        conn.close()
    if (resp.getheader("Content-Type") or "").startswith("text/event-stream"):
        return resp.status, resp.headers, sse_events(raw)
    return resp.status, resp.headers, json.loads(raw) if raw else None


def sse_events(raw: str) -> tuple[list, list]:
    comments, out, name, lines = [], [], "message", []
    for line in raw.split("\n"):
        if not line:
            if lines:
                out.append((name, json.loads("\n".join(lines))))
            name, lines = "message", []
        elif line.startswith(":"):
            comments.append(line)
        elif line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            lines.append(line[6:])
    return comments, out


def reason(body) -> tuple[int, str]:
    info = [d for d in body["error"]["details"]
            if d["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"]
    assert len(info) == 1 and info[0]["domain"] == "a2a-protocol.org"
    return body["error"]["code"], info[0]["reason"]


class TestREST:

    def test_rest_scenarios(self, tmp_path, capsys):
        r = Run(tmp_path, key=True)
        status, headers, body = rest(r, "PLANNER", "POST", "/message:send",
                                     params("summarize", "zq-secret"))
        assert status == 200 and body["task"]["id"] == "up-task-1"
        assert headers["CADL-Run-Id"] == r.run_id
        status, _, body = rest(r, "PLANNER", "POST", "/message:send", params("translate"),
                               ctype="application/a2a+json")
        assert status == 200 and body["task"]["status"]["state"] == "TASK_STATE_REJECTED"
        gw_task = body["task"]["id"]
        _, _, body = rest(r, "PLANNER", "POST", "/message:send", params("summarize", ext=False))
        assert "no_name" in body["task"]["status"]["message"]["parts"][0]["text"]
        # The client's own tasks, with the parameters of the query (11.5).
        status, _, body = rest(r, "PLANNER", "GET", "/tasks/up-task-1?historyLength=1")
        assert status == 200 and body["id"] == "up-task-1"
        assert rest(r, "PLANNER", "GET", f"/tasks/{gw_task}")[2]["status"]["state"] == \
            "TASK_STATE_REJECTED"
        assert reason(rest(r, "OPERATOR", "GET", "/tasks/up-task-1")[2]) == \
            (404, "TASK_NOT_FOUND")
        assert reason(rest(r, "OPERATOR", "POST", "/tasks/up-task-1:cancel")[2]) == \
            (404, "TASK_NOT_FOUND")
        assert reason(rest(r, "PLANNER", "POST", f"/tasks/{gw_task}:cancel")[2]) == \
            (400, "TASK_NOT_CANCELABLE")
        status, _, body = rest(r, "PLANNER", "POST", "/tasks/up-task-1:cancel",
                               {"id": "up-task-1"})
        assert status == 200 and body["status"]["state"] == "TASK_STATE_CANCELED"
        # Refused operations and routes (fail closed).
        assert reason(rest(r, "PLANNER", "GET", "/tasks")[2]) == (400, "UNSUPPORTED_OPERATION")
        assert reason(rest(r, "PLANNER", "GET", "/extendedAgentCard")[2]) == \
            (400, "UNSUPPORTED_OPERATION")
        assert reason(rest(r, "PLANNER", "POST", "/tasks/up-task-1/pushNotificationConfigs",
                           {"url": "https://x.example/"})[2]) == \
            (400, "PUSH_NOTIFICATION_NOT_SUPPORTED")
        for target in ("/other", "/t1/message:send", "/tasks/a/b", "/message:send/x"):
            assert reason(rest(r, "PLANNER", "POST", target, params("summarize"))[2]) == \
                (404, "METHOD_NOT_FOUND")
        assert reason(rest(r, "PLANNER", "DELETE", "/tasks/up-task-1")[2]) == \
            (404, "METHOD_NOT_FOUND")
        assert reason(rest(r, "PLANNER", "GET", "/tasks/up-task-1?x=1")[2]) == \
            (400, "INVALID_PARAMS")
        assert reason(rest(r, "PLANNER", "GET", "/tasks/up-task-1?historyLength=-1")[2]) == \
            (400, "INVALID_PARAMS")
        assert reason(rest(r, "PLANNER", "GET",
                           "/tasks/up-task-1?historyLength=1&historyLength=2")[2]) == \
            (400, "INVALID_PARAMS")
        assert reason(rest(r, "PLANNER", "POST", "/tasks/up-task-1:cancel",
                           {"id": "other"})[2]) == (400, "INVALID_PARAMS")
        assert reason(rest(r, "PLANNER", "POST", "/message:send", {"message": 1})[2]) == \
            (400, "INVALID_PARAMS")
        assert reason(rest(r, "PLANNER", "POST", "/message:send",
                           dict(params("summarize"), tenant="t"))[2]) == (400, "INVALID_PARAMS")
        assert reason(rest(r, "PLANNER", "POST", "/message:send")[2]) == (400, "INVALID_PARAMS")
        assert reason(rest(r, "PLANNER", "POST", "/message:send", b"[1]")[2]) == \
            (400, "INVALID_REQUEST")
        assert reason(rest(r, "PLANNER", "POST", "/message:send", b'{"a":1,"a":2}')[2]) == \
            (400, "INVALID_REQUEST")
        assert reason(rest(r, "PLANNER", "POST", "/message:send", params("summarize"),
                           version=None)[2]) == (400, "VERSION_NOT_SUPPORTED")
        assert reason(rest(r, "PLANNER", "POST", "/message:send", params("summarize"),
                           version="0.3")[2]) == (400, "VERSION_NOT_SUPPORTED")
        assert rest(r, "PLANNER", "POST", "/message:send", params("summarize"),
                    ctype="text/plain")[0] == 415
        # The MAC covers the method and the whole request target.
        status, headers, body = rest(r, "PLANNER", "POST", "/message:stream",
                                     params("summarize"), sign="/message:send")
        assert status == 401 and body["error"]["status"] == "UNAUTHENTICATED"
        assert headers["WWW-Authenticate"] == f'CADL-HMAC run="{r.run_id}"'
        assert rest(r, "PLANNER", "GET", "/tasks/up-task-1?historyLength=1",
                    sign="/tasks/up-task-1")[0] == 401
        assert rest(r, None, "GET", "/tasks/up-task-1")[0] == 401
        # A GET with a body is refused before it is read as a request.
        assert r.client.raw(b"{}", {"Content-Length": "2"}, path="/tasks/up-task-1",
                            method="GET")[0] == 400
        r.stop()
        assert r.agent.sent() == ["summarize"]
        assert [x["params"] for x in r.agent.requests if x["method"] == "GetTask"] == [
            {"id": "up-task-1", "historyLength": 1}]
        assert r.agent.sent("CancelTask") == ["up-task-1"]
        assert decisions(r.replay_check()) == [
            ("PLANNER", "summarize", "allow", "permission"),
            ("PLANNER", "translate", "deny", "default")]
        assert [why for _, why in events(r.audit, "unauthenticated_request")] == [
            "bad_mac", "bad_mac", "missing"]
        refused = [why for _, why in events(r.audit, "method_refused")]
        assert refused[:3] == ["ListTasks", "GetExtendedAgentCard",
                               "CreateTaskPushNotificationConfig"]
        assert refused[3:] == ["POST /other", "POST /t1/message:send", "POST /tasks/a/b",
                               "POST /message:send/x", "DELETE /tasks/up-task-1"]
        assert "zq-secret" not in r.trace.read_text() + r.audit.read_text()
        capsys.readouterr()

    def test_rest_holds(self, tmp_path, capsys):
        r = Run(tmp_path)
        fut = r.pool.submit(rest, r, "PLANNER", "POST", "/message:send", params("publish"))
        r.approve("h-1")
        status, _, body = fut.result(timeout=30)
        assert status == 200 and body["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        fut = r.pool.submit(rest, r, "PLANNER", "POST", "/message:send", params("publish"))
        r.approve("h-2", "reject")
        assert fut.result(timeout=30)[2]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
        _, _, body = rest(r, "PLANNER", "POST", "/message:send",
                          params("publish", returnImmediately=True))
        own = body["task"]["id"]
        assert body["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
        assert rest(r, "PLANNER", "GET", f"/tasks/{own}")[2]["status"]["state"] == \
            "TASK_STATE_SUBMITTED"
        status, _, body = rest(r, "PLANNER", "POST", f"/tasks/{own}:cancel")
        assert status == 200 and body["status"]["state"] == "TASK_STATE_CANCELED"
        r.stop()
        assert r.agent.sent() == ["publish"]
        assert [h["state"] for h in r.replay_check()["holds"]] == [
            "approved", "rejected", "withdrawn"]
        capsys.readouterr()

    def test_rest_streams(self, tmp_path, capsys):
        r = Run(tmp_path)
        status, headers, (comments, evs) = rest(r, "PLANNER", "POST", "/message:stream",
                                                params("summarize"))
        assert status == 200 and comments == []
        assert [next(iter(e)) for _, e in evs] == [
            "task", "statusUpdate", "artifactUpdate", "statusUpdate"]
        _, _, (_, evs) = rest(r, "PLANNER", "POST", "/message:stream", params("translate"))
        assert [e["task"]["status"]["state"] for _, e in evs] == ["TASK_STATE_REJECTED"]
        # A held stream: comments, then the events of the released request.
        fut = r.pool.submit(rest, r, "PLANNER", "POST", "/message:stream", params("publish"))
        r.approve("h-1")
        _, _, (comments, evs) = fut.result(timeout=30)
        assert evs[-1][1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
        # A bad event of the agent ends the stream with an error event.
        _, _, (_, evs) = rest(r, "PLANNER", "POST", "/message:stream",
                              params("summarize", "stream-foreign"))
        assert evs[-1][0] == "error" and reason(evs[-1][1]) == (500, "INVALID_AGENT_RESPONSE")
        assert len(evs) == 2
        # Subscriptions, with POST (11.3.2) and GET (a2a.proto).
        for method in ("POST", "GET"):
            _, _, (_, evs) = rest(r, "PLANNER", method, "/tasks/up-task-1:subscribe")
            assert evs[0][1]["task"]["id"] == "up-task-1"
        assert reason(rest(r, "OPERATOR", "POST", "/tasks/up-task-1:subscribe")[2]) == \
            (404, "TASK_NOT_FOUND")
        # returnImmediately, then a subscription to the gateway's task.
        _, _, (_, evs) = rest(r, "PLANNER", "POST", "/message:stream",
                              params("publish", returnImmediately=True))
        own = evs[0][1]["task"]["id"]
        fut = r.pool.submit(rest, r, "PLANNER", "POST", f"/tasks/{own}:subscribe")
        time.sleep(0.3)
        r.approve("h-2")
        _, _, (_, evs) = fut.result(timeout=30)
        ids = {e.get("task", {}).get("id") or e.get("statusUpdate", e.get(
            "artifactUpdate", {})).get("taskId") for _, e in evs}
        assert ids == {own}
        r.stop()
        assert r.agent.sent("SendStreamingMessage") == ["summarize", "publish", "summarize"]
        assert r.agent.sent("SubscribeToTask") == ["up-task-1", "up-task-1", "up-task-4"]
        assert decisions(r.replay_check()) == [
            ("PLANNER", "summarize", "allow", "permission"),
            ("PLANNER", "translate", "deny", "default"),
            ("PLANNER", "publish", "escalate", "prohibition"),
            ("OPERATOR", "approve", "allow", "permission"),
            ("PLANNER", "summarize", "allow", "permission"),
            ("PLANNER", "publish", "escalate", "prohibition"),
            ("OPERATOR", "approve", "allow", "permission")]
        capsys.readouterr()

    def test_agent_of_the_rest_binding(self, tmp_path, capsys):
        """--upstream-binding rest: the gateway speaks the HTTP+JSON binding
        to the agent, for clients of either binding, and checks its answers
        as strictly as those of JSON-RPC."""
        r = Run(tmp_path, extra=("--upstream-binding", "rest"))
        body = r.call("PLANNER", "SendMessage", params("summarize"))
        assert body["result"]["task"]["id"] == "up-task-1"
        _, _, body = rest(r, "PLANNER", "POST", "/message:send", params("summarize"))
        assert body["task"]["id"] == "up-task-2"
        assert r.call("PLANNER", "GetTask", {"id": "up-task-1", "historyLength": 2})[
            "result"]["id"] == "up-task-1"
        assert r.call("PLANNER", "SendMessage", params("summarize", "error"))["error"] == {
            "code": -32004, "message": "not today"}
        assert reason(rest(r, "PLANNER", "POST", "/message:send",
                           params("summarize", "error"))[2]) == (400, "UNSUPPORTED_OPERATION")
        assert r.call("PLANNER", "SendMessage", params("summarize", "garbage"))["error"][
            "code"] == -32006
        _, _, (_, evs) = rest(r, "PLANNER", "POST", "/message:stream", params("summarize"))
        assert [next(iter(e)) for _, e in evs] == [
            "task", "statusUpdate", "artifactUpdate", "statusUpdate"]
        _, _, (_, evs) = rest(r, "PLANNER", "POST", "/message:stream",
                              params("summarize", "stream-foreign"))
        assert reason(evs[-1][1]) == (500, "INVALID_AGENT_RESPONSE")
        _, _, (_, evs) = rest(r, "PLANNER", "GET", "/tasks/up-task-1:subscribe")
        assert evs[0][1]["task"]["id"] == "up-task-1"
        assert r.call("PLANNER", "CancelTask", {"id": "up-task-2"})["result"]["status"][
            "state"] == "TASK_STATE_CANCELED"
        r.stop()
        targets = [x["target"] for x in r.agent.requests]
        assert targets == [
            "POST /message:send", "POST /message:send", "GET /tasks/up-task-1?historyLength=2",
            "POST /message:send", "POST /message:send", "POST /message:send",
            "POST /message:stream", "POST /message:stream", "POST /tasks/up-task-1:subscribe",
            "POST /tasks/up-task-2:cancel"]
        assert all(x["binding"] == "rest" and "Authorization" not in x["headers"]
                   for x in r.agent.requests)
        r.replay_check()
        assert [why for _, why in events(r.audit, "invalid_response")][0].startswith(
            "_NoResponse")
        capsys.readouterr()


def test_rest_routes_and_errors():
    from cadl import a2a_rest as R
    assert R.route("POST", "/message:send") == ("SendMessage", {})
    assert R.route("GET", "/message:send") is None
    assert R.route("GET", "/tasks/a%2Fb") is None                # no slash in an id
    assert R.route("GET", "/tasks/a%20b") is None
    assert R.route("GET", "/tasks/t-1?historyLength=3") == ("GetTask", {"id": "t-1"})
    assert R.route("POST", "/tasks/t%3A1:cancel") == ("CancelTask", {"id": "t:1"})
    assert R.route("GET", "/tasks/t:subscribe") == ("SubscribeToTask", {"id": "t"})
    assert R.route("DELETE", "/tasks/t/pushNotificationConfigs/c") == (
        "DeleteTaskPushNotificationConfig", {"taskId": "t", "id": "c"})
    assert R.route("PUT", "/tasks/t/pushNotificationConfigs") is None
    assert R.route("POST", "//message:send") is None
    assert R.route("POST", "http://x/message:send") is None
    assert R.query("/t?historyLength=10", {"historyLength": "int"}) == ({"historyLength": 10},
                                                                         None)
    assert R.query("/t?historyLength=1e3", {"historyLength": "int"})[0] is None
    assert R.query("/t?historyLength", {"historyLength": "int"})[0] is None
    status, body = R.error(-32001, "x")
    assert status == 404 and body["error"]["details"][0]["reason"] == "TASK_NOT_FOUND"
    assert R.error(12345, "x")[0] == 500
    assert R.error_from_rest(404, body) == {"code": -32001, "message": "x"}
    assert R.error_from_rest(404, {"error": {"message": "x", "details": [
        {"@type": R.ERROR_INFO, "reason": "OTHER", "domain": R.DOMAIN}]}}) is None
    assert R.error_from_rest(404, {"error": {"message": "x", "details": []}}) is None
    assert R.to_jsonrpc(3, 502, "<html>") is None
    assert json.loads(R.to_jsonrpc(3, 200, '{"task": {}}')) == {
        "jsonrpc": "2.0", "id": 3, "result": {"task": {}}}
