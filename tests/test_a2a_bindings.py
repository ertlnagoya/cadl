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
                        "summarizer": "SUMMARIZER", "intruder": "INTRUDER",
                        "two": "PLANNER/CN=OPERATOR"},
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

    def test_client_ca_needs_tls(self, pki):
        from cadl.a2a_gateway import serve
        from cadl.gateway_core import GatewayError
        with pytest.raises(GatewayError, match="--client-ca needs --tls-cert"):
            serve({}, listen="127.0.0.1:0", client_ca=pki.ca)


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


# === 3. Push notifications =========================================================

class Hook:
    """A webhook of a client on 127.0.0.1: records every POST; the path
    chooses the answer: /slow (after `slow` seconds), /redirect (302 to
    /moved), /fail (500), anything else 200."""

    def __init__(self, slow: float = 3.0):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.got: list[dict] = []
        hook = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):      # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hook.got.append({"path": self.path, "headers": dict(self.headers),
                                 "body": json.loads(body)})
                if self.path.endswith("/slow"):
                    time.sleep(slow)
                status = {"redirect": 302, "fail": 500}.get(self.path.rsplit("/", 1)[-1], 200)
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/hooks/moved")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def push_cfg(url: str, **kw) -> dict:
    return dict({"url": url, "token": "client-token",
                 "authentication": {"scheme": "Bearer", "credentials": "client-cred"}}, **kw)


def status_event(task: str, state="TASK_STATE_WORKING", ctx=None) -> dict:
    ev = {"taskId": task, "status": {"state": state}}
    if ctx:
        ev["contextId"] = ctx
    return {"statusUpdate": ev}


class TestPush:

    def test_push_end_to_end(self, tmp_path, capsys):
        hook = Hook()
        r = Run(tmp_path, key=True, extra=("--push-allow", f"{hook.url}/hooks/"))
        # A configuration with a SendMessage: the agent gets the gateway's.
        body = r.call("PLANNER", "SendMessage", params(
            "summarize", taskPushNotificationConfig=push_cfg(f"{hook.url}/hooks/a")))
        assert body["result"]["task"]["id"] == "up-task-1"
        (key, cfg), = r.agent.push.items()
        assert key[0] == "up-task-1" and cfg["url"].startswith(
            f"http://127.0.0.1:{r.client.port}/cadl-push/cadl-push-")
        assert cfg["token"] != "client-token" and "client-cred" not in json.dumps(cfg)
        assert r.agent.notify("up-task-1", status_event("up-task-1", ctx="up-ctx-1")) == [204]
        got = hook.got[-1]
        assert got["path"] == "/hooks/a" and got["body"] == status_event("up-task-1",
                                                                         ctx="up-ctx-1")
        assert got["headers"]["X-A2A-Notification-Token"] == "client-token"
        assert got["headers"]["Authorization"] == "Bearer client-cred"
        # Create, Get, List for the client's own task; another client sees none.
        made = r.call("PLANNER", "CreateTaskPushNotificationConfig",
                      dict(push_cfg(f"{hook.url}/hooks/b"), taskId="up-task-1", id="mine"))
        assert made["result"] == dict(push_cfg(f"{hook.url}/hooks/b"), taskId="up-task-1",
                                      id="mine")
        assert r.call("PLANNER", "GetTaskPushNotificationConfig",
                      {"taskId": "up-task-1", "id": "mine"})["result"]["url"].endswith("/b")
        listed = r.call("PLANNER", "ListTaskPushNotificationConfigs", {"taskId": "up-task-1"})
        assert sorted(c["url"][-1] for c in listed["result"]["configs"]) == ["a", "b"]
        assert r.call("OPERATOR", "ListTaskPushNotificationConfigs",
                      {"taskId": "up-task-1"})["error"]["code"] == -32001
        assert r.call("OPERATOR", "CreateTaskPushNotificationConfig",
                      dict(push_cfg(f"{hook.url}/hooks/c"), taskId="up-task-1"))[
            "error"]["code"] == -32001
        # A notification for a task of another client is not relayed.
        r.call("OPERATOR", "SendMessage", params("translate"))         # allowed: up-task-2
        n = len(hook.got)
        assert r.agent.notify("", status_event("up-task-2"), config=cfg) == [400]
        assert r.agent.notify("", status_event("up-task-1"), config=cfg,
                              headers={"X-A2A-Notification-Token": "forged"}) == [401]
        assert r.agent.notify("", status_event("up-task-1"),
                              config=dict(cfg, url=cfg["url"][:-4] + "0000")) == [404]
        assert r.agent.notify("", {"statusUpdate": {}}, config=cfg) == [400]
        assert len(hook.got) == n
        # Delete: the agent's configuration goes, and nothing more is relayed.
        assert r.call("PLANNER", "DeleteTaskPushNotificationConfig",
                      {"taskId": "up-task-1", "id": "mine"})["result"] == {}
        assert len(r.agent.push) == 1
        # URLs that the gateway does not call.
        for url, why in ((f"{hook.url}/other", "no entry"),
                         ("http://example.com/hooks/", "https"),
                         (f"{hook.url}/hooks/x#f", "fragment"),
                         (f"http://user@127.0.0.1:{hook.port}/hooks/", "user")):
            err = r.call("PLANNER", "SendMessage", params(
                "summarize", taskPushNotificationConfig=push_cfg(url)))["error"]
            assert err["code"] == -32602 and why in err["message"], (url, err)
        err = r.call("PLANNER", "SendMessage", params("summarize", taskPushNotificationConfig=
                     push_cfg(f"{hook.url}/hooks/a", token="a\r\nX: y")))["error"]
        assert err["code"] == -32602
        # A denied message's configuration never reaches the agent.
        r.call("PLANNER", "SendMessage", params(
            "translate", taskPushNotificationConfig=push_cfg(f"{hook.url}/hooks/d")))
        # A held request with returnImmediately: the client's task is the
        # gateway's, also in the notifications.
        body = r.call("PLANNER", "SendMessage", params(
            "publish", returnImmediately=True,
            taskPushNotificationConfig=push_cfg(f"{hook.url}/hooks/e")))
        own = body["result"]["task"]["id"]
        hold = json.loads(r.wait_hold("h-1").read_text())
        assert hold["params"]["configuration"]["taskPushNotificationConfig"]["token"] == \
            "(not shown)"
        r.approve("h-1")
        wait_for(lambda: r.call("PLANNER", "GetTask", {"id": own})["result"]["status"][
            "state"] == "TASK_STATE_COMPLETED", "the released task")
        up = [k[0] for k in r.agent.push if k[0] not in ("up-task-1",)][0]
        assert r.agent.notify(up, status_event(up, "TASK_STATE_COMPLETED")) == [204]
        assert hook.got[-1]["path"] == "/hooks/e"
        assert hook.got[-1]["body"]["statusUpdate"]["taskId"] == own
        assert r.call("PLANNER", "ListTaskPushNotificationConfigs", {"taskId": own})[
            "result"]["configs"][0]["taskId"] == own
        r.stop()
        hook.close()
        assert r.agent.sent() == ["summarize", "translate", "publish"]
        assert [x["params"]["configuration"]["taskPushNotificationConfig"]["url"].rsplit(
                    "/", 1)[0] + "/"
                for x in r.agent.requests if x["method"] == "SendMessage"
                and "configuration" in x["params"]][:1] == [
            f"http://127.0.0.1:{r.client.port}/cadl-push/"]
        evs = [e for e, _ in events(r.audit, "push_config", "push_sent", "push_dropped",
                                    "push_refused", "push_failed")]
        assert evs == ["push_config", "push_sent", "push_config", "push_dropped",
                       "push_dropped", "push_dropped", "push_dropped", "push_config",
                       "push_refused", "push_refused", "push_refused", "push_refused",
                       "push_refused", "push_config", "push_sent"], evs
        result = r.replay_check()
        assert decisions(result)[:3] == [("PLANNER", "summarize", "allow", "permission"),
                                         ("OPERATOR", "translate", "allow", "permission"),
                                         ("PLANNER", "translate", "deny", "default")]
        assert "client-cred" not in r.trace.read_text() + r.audit.read_text()
        capsys.readouterr()

    def test_push_delivery_limits(self, tmp_path, capsys):
        """No redirects, a deadline for the whole exchange, failures
        recorded; and a host name that resolves to loopback only with an
        address of the allowlist."""
        hook = Hook(slow=3.0)
        r = Run(tmp_path, extra=("--push-allow", f"{hook.url}/hooks/",
                                 "--push-allow", "localhost", "--push-timeout", "1"))
        for i, path in enumerate(("redirect", "slow", "fail")):
            r.call("PLANNER", "SendMessage", params(
                "summarize", taskPushNotificationConfig=push_cfg(f"{hook.url}/hooks/{path}")))
            t0 = time.monotonic()
            assert r.agent.notify(f"up-task-{i + 1}", status_event(f"up-task-{i + 1}")) == [502]
            assert time.monotonic() - t0 < 2.5
        assert [g["path"] for g in hook.got] == ["/hooks/redirect", "/hooks/slow",
                                                 "/hooks/fail"]       # not /hooks/moved
        err = r.call("PLANNER", "SendMessage", params("summarize", taskPushNotificationConfig=
                     push_cfg(f"http://localhost:{hook.port}/hooks/x")))["error"]
        assert "loopback" in err["message"]
        r.stop()
        failed = [why for _, why in events(r.audit, "push_failed")]
        assert "redirect 302 is not followed" in failed[0]
        assert failed[1].endswith("timeout") and failed[2].endswith("HTTP status 500")
        r.replay_check()
        r = Run(tmp_path, name="lo", extra=("--push-allow", "localhost",
                                            "--push-allow", "127.0.0.0/8",
                                            "--push-allow", "::1"))
        r.call("PLANNER", "SendMessage", params("summarize", taskPushNotificationConfig=
               push_cfg(f"http://localhost:{hook.port}/hooks/y")))
        assert r.agent.notify("up-task-1", status_event("up-task-1")) == [204]
        assert hook.got[-1]["path"] == "/hooks/y"
        r.stop()
        hook.close()
        r.replay_check()
        capsys.readouterr()

    def test_push_over_rest_and_mutual_tls(self, tmp_path, pki, capsys):
        """The routes of 11.3.3, and with --client-ca the agent's certificate,
        which delivers notifications and is no client's."""
        hook = Hook()
        r = Run(tmp_path, extra=(*pki.gateway_args(), "--push-allow", f"{hook.url}/hooks/"))
        c = TLSClient(r.client.port, r.run_id, pki, "planner")
        _, _, body = rest(r, "PLANNER", "POST", "/message:send", params("summarize"), client=c)
        tid = body["task"]["id"]
        status, _, made = rest(r, "PLANNER", "POST", f"/tasks/{tid}/pushNotificationConfigs",
                               push_cfg(f"{hook.url}/hooks/r", id="r1"), client=c)
        assert status == 200 and made["id"] == "r1" and made["taskId"] == tid
        assert rest(r, "PLANNER", "GET", f"/tasks/{tid}/pushNotificationConfigs/r1",
                    client=c)[2]["url"].endswith("/hooks/r")
        assert len(rest(r, "PLANNER", "GET", f"/tasks/{tid}/pushNotificationConfigs?pageSize=5",
                        client=c)[2]["configs"]) == 1
        (_, cfg), = r.agent.push.items()
        assert cfg["url"].startswith(f"https://127.0.0.1:{r.client.port}/cadl-push/")
        # The agent delivers with its certificate (common name SUMMARIZER).
        agent = TLSClient(r.client.port, r.run_id, pki, "summarizer")
        note = json.dumps(status_event(tid)).encode()
        status = agent.raw(note, {
            "Content-Type": "application/json", "Content-Length": str(len(note)),
            "X-A2A-Notification-Token": cfg["token"],
            "Authorization": f"Bearer {cfg['authentication']['credentials']}"},
            path=cfg["url"].split(str(r.client.port), 1)[1])[0]
        assert status == 204
        assert hook.got[-1]["path"] == "/hooks/r"
        # A client's certificate cannot deliver; the agent's cannot send.
        assert c.raw(note, {"Content-Type": "application/json",
                            "Content-Length": str(len(note)),
                            "X-A2A-Notification-Token": cfg["token"],
                             "Authorization": "Bearer x"},
                     path=cfg["url"].split(str(r.client.port), 1)[1])[0] == 401
        assert agent.post("PLANNER", rpc("SendMessage", params("summarize")))[0] == 401
        status, _, _ = rest(r, "PLANNER", "DELETE", f"/tasks/{tid}/pushNotificationConfigs/r1",
                            client=c)
        assert status == 200 and r.agent.push == {}
        r.stop()
        hook.close()
        assert [x["target"] if "target" in x else x["method"] for x in r.agent.requests] == [
            "SendMessage", "CreateTaskPushNotificationConfig",
            "DeleteTaskPushNotificationConfig"]
        assert [why for _, why in events(r.audit, "unauthenticated_request")] == [
            "certificate_mismatch"]
        r.replay_check()
        capsys.readouterr()


def test_push_policy():
    """The SSRF checks of `cadl.a2a_push`, with a resolver of the test."""
    import socket as S

    from cadl.a2a_push import PushError, PushPolicy, blocked, check_auth
    import ipaddress as I
    names = {"hooks.example.com": ["93.184.216.34"], "inner.example.com": ["10.1.2.3"],
             "meta.example.com": ["169.254.169.254"], "mixed.example.com":
             ["93.184.216.34", "192.168.1.1"], "mapped.example.com": ["::ffff:10.0.0.1"],
             "nat64.example.com": ["64:ff9b::a00:1"], "v6.example.com": ["2606:4700::1"]}

    def resolve(host, port, type=None):
        try:
            names.setdefault(host, [str(I.ip_address(host))])
        except ValueError:
            pass
        if host not in names:
            raise S.gaierror("unknown")
        return [(S.AF_INET6 if ":" in a else S.AF_INET, S.SOCK_STREAM, 6, "", (a, port))
                for a in names[host]]
    p = PushPolicy(["https://hooks.example.com/a2a/", "inner.example.com:8443",
                    "meta.example.com", "mixed.example.com", "mapped.example.com",
                    "nat64.example.com", "v6.example.com", "10.1.0.0/16",
                    "169.254.0.0/16", "http://127.0.0.1:9/x"], resolve=resolve)

    def ok(url):
        return p.addresses(p.check(url))

    def refused(url, why):
        with pytest.raises(PushError, match=why):
            ok(url)
    assert ok("https://hooks.example.com/a2a/x?y=1") == ["93.184.216.34"]
    refused("https://hooks.example.com/a2ax", "no entry")
    refused("https://hooks.example.com:444/a2a/", "no entry")
    refused("http://hooks.example.com/a2a/", "https")
    assert ok("https://inner.example.com:8443/") == ["10.1.2.3"]     # in 10.1.0.0/16
    refused("https://inner.example.com/", "no entry")
    refused("https://meta.example.com/", "metadata")     # a network does not unlock it
    refused("https://mixed.example.com/", "private")
    refused("https://mapped.example.com/", "private")
    refused("https://nat64.example.com/", "private")
    assert ok("https://v6.example.com/") == ["2606:4700::1"]
    assert ok("http://127.0.0.1:9/x") == ["127.0.0.1"]
    refused("http://127.0.0.1:9/x/y", "no entry")
    refused("https://unknown.example.com/", "no entry")
    refused("ftp://hooks.example.com/a2a/", "http or https")
    refused("https://u:p@hooks.example.com/a2a/", "user")
    refused("https://hooks.example.com/a2a/é", "printable ASCII")
    assert PushPolicy(["169.254.169.254"], resolve=resolve).addresses(
        PushPolicy(["169.254.169.254"]).check("https://169.254.169.254/")) == [
        "169.254.169.254"]                               # named itself: allowed
    for addr, why in (("10.0.0.1", "private"), ("127.0.0.2", "loopback"),
                      ("169.254.1.1", "link-local"), ("100.64.0.1", "not global"),
                      ("fe80::1", "link-local"), ("fd00:ec2::254", "metadata"),
                      ("2002:0a00:0001::1", "private"), ("0.0.0.0", "private"),
                      ("224.0.0.1", "not global"), ("8.8.8.8", None)):
        got = blocked(I.ip_address(addr))
        assert (got is None) if why is None else (why in got), (addr, got)
    for bad in ([], ["ftp://x/"], ["https://x/?q"], ["bad host!"], ["h:99999"]):
        with pytest.raises(PushError):
            PushPolicy(bad)
    check_auth("t", {"scheme": "Bearer", "credentials": "c"})
    for token, auth in (("a\nb", None), (None, {"scheme": "Bearer x"}),
                        (None, {"scheme": "Bearer", "credentials": "a\r\n"}),
                        (None, {"scheme": "B", "other": 1}), (5, None)):
        with pytest.raises(PushError):
            check_auth(token, auth)


# === 4. gRPC (the extra cadl-lang[grpc]) ===========================================

@pytest.fixture
def grpc_extra():
    grpc = pytest.importorskip("grpc")
    pb = pytest.importorskip("a2a.types.a2a_pb2")
    return grpc, pb


class GrpcClient:
    """A gRPC client of the gateway that signs the serialized request."""

    def __init__(self, r, grpc, pb, port: int, pki: PKI | None = None, cert: str | None = None):
        self.r, self.grpc, self.pb = r, grpc, pb
        if pki is None:
            self.channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        else:
            key = (pki.dir / f"{cert}.key").read_bytes() if cert else None
            chain = (pki.dir / f"{cert}.pem").read_bytes() if cert else None
            creds = grpc.ssl_channel_credentials(pki.ca.read_bytes(), key, chain)
            self.channel = grpc.secure_channel(f"127.0.0.1:{port}", creds)

    def _md(self, actor, name, data, version="1.0", sign=None):
        md = []
        if version is not None:
            md.append(("a2a-version", version))
        if actor is not None:
            md.append(("authorization", authorization(
                CLIENT_KEYS[actor], actor, self.r.run_id, data,
                path=sign or f"/lf.a2a.v1.A2AService/{name}")))
        return md

    def call(self, actor, name, req, resp_cls, raw=None, **kw):
        data = raw if raw is not None else req.SerializeToString()
        fn = self.channel.unary_unary(f"/lf.a2a.v1.A2AService/{name}")
        return resp_cls.FromString(fn(data, metadata=self._md(actor, name, data, **kw),
                                      timeout=30))

    def stream(self, actor, name, req, **kw):
        data = req.SerializeToString()
        fn = self.channel.unary_stream(f"/lf.a2a.v1.A2AService/{name}")
        return [self.pb.StreamResponse.FromString(x)
                for x in fn(data, metadata=self._md(actor, name, data, **kw), timeout=30)]

    def error(self, fn, *args, **kw):
        """(status code name, ErrorInfo reason or None) of a failed RPC."""
        from google.rpc import error_details_pb2, status_pb2
        with pytest.raises(self.grpc.RpcError) as e:
            fn(*args, **kw)
        reason = None
        for k, v in e.value.trailing_metadata() or ():
            if k == "grpc-status-details-bin":
                st = status_pb2.Status.FromString(v)
                info = error_details_pb2.ErrorInfo()
                st.details[0].Unpack(info)
                assert info.domain == "a2a-protocol.org"
                reason = info.reason
        return e.value.code().name, reason


def grpc_message(pb, name, text="hello", **conf):
    from google.protobuf import json_format
    return json_format.ParseDict(params(name, text, **conf), pb.SendMessageRequest())


class TestGrpc:

    def test_grpc_scenarios(self, tmp_path, grpc_extra, capsys):
        grpc, pb = grpc_extra
        port_file = tmp_path / "grpc-port"
        r = Run(tmp_path, key=True, extra=("--grpc-listen", "127.0.0.1:0", "--grpc-port-file",
                                           str(tmp_path / "grpc-port")))
        wait_for(lambda: port_file.exists() and port_file.read_text().strip(), "the port")
        g = GrpcClient(r, grpc, pb, int(port_file.read_text()))
        got = g.call("PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                     pb.SendMessageResponse)
        assert got.task.id == "up-task-1" and got.task.status.state == pb.TASK_STATE_COMPLETED
        got = g.call("PLANNER", "SendMessage", grpc_message(pb, "translate"),
                     pb.SendMessageResponse)
        assert got.task.status.state == pb.TASK_STATE_REJECTED
        # A hold, approved.
        fut = r.pool.submit(g.call, "PLANNER", "SendMessage", grpc_message(pb, "publish"),
                            pb.SendMessageResponse)
        r.approve("h-1")
        assert fut.result(timeout=30).task.status.state == pb.TASK_STATE_COMPLETED
        assert g.call("PLANNER", "GetTask", pb.GetTaskRequest(id="up-task-1"), pb.Task).id == \
            "up-task-1"
        assert g.error(g.call, "OPERATOR", "GetTask", pb.GetTaskRequest(id="up-task-1"),
                       pb.Task) == ("NOT_FOUND", "TASK_NOT_FOUND")
        assert g.call("PLANNER", "CancelTask", pb.CancelTaskRequest(id="up-task-1"),
                      pb.Task).status.state == pb.TASK_STATE_CANCELED
        # Streams and a subscription.
        evs = g.stream("PLANNER", "SendStreamingMessage", grpc_message(pb, "summarize"))
        assert [e.WhichOneof("payload") for e in evs] == [
            "task", "status_update", "artifact_update", "status_update"]
        evs = g.stream("PLANNER", "SendStreamingMessage", grpc_message(pb, "translate"))
        assert [e.task.status.state for e in evs] == [pb.TASK_STATE_REJECTED]
        assert g.stream("PLANNER", "SubscribeToTask",
                        pb.SubscribeToTaskRequest(id="up-task-1"))[0].task.id == "up-task-1"
        assert g.error(g.stream, "PLANNER", "SendStreamingMessage",
                       grpc_message(pb, "summarize", "stream-foreign")) == (
            "INTERNAL", "INVALID_AGENT_RESPONSE")
        # Refused methods, versions, authentication, and unknown fields.
        assert g.error(g.call, "PLANNER", "ListTasks", pb.ListTasksRequest(),
                       pb.ListTasksResponse) == ("FAILED_PRECONDITION", "UNSUPPORTED_OPERATION")
        assert g.error(g.call, "PLANNER", "CreateTaskPushNotificationConfig",
                       pb.TaskPushNotificationConfig(task_id="up-task-1", url="https://x/"),
                       pb.TaskPushNotificationConfig) == (
            "FAILED_PRECONDITION", "PUSH_NOTIFICATION_NOT_SUPPORTED")
        assert g.error(g.call, "PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                       pb.SendMessageResponse, version=None) == (
            "FAILED_PRECONDITION", "VERSION_NOT_SUPPORTED")
        assert g.error(g.call, None, "SendMessage", grpc_message(pb, "summarize"),
                       pb.SendMessageResponse) == ("UNAUTHENTICATED", None)
        assert g.error(g.call, "PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                       pb.SendMessageResponse, sign="/lf.a2a.v1.A2AService/GetTask") == (
            "UNAUTHENTICATED", None)
        data = grpc_message(pb, "summarize").SerializeToString() + b"\xf8\x07\x01"  # field 127
        assert g.error(g.call, "PLANNER", "SendMessage", None, pb.SendMessageResponse,
                       raw=data) == ("INVALID_ARGUMENT", "INVALID_PARAMS")
        assert g.error(g.call, "PLANNER", "SendMessage", None, pb.SendMessageResponse,
                       raw=b"\xff\xff") == ("INVALID_ARGUMENT", "INVALID_PARAMS")
        g.channel.close()
        r.stop()
        assert r.agent.sent() == ["summarize", "publish"]
        assert r.agent.sent("SendStreamingMessage") == ["summarize", "summarize"]
        assert decisions(r.replay_check()) == [
            ("PLANNER", "summarize", "allow", "permission"),
            ("PLANNER", "translate", "deny", "default"),
            ("PLANNER", "publish", "escalate", "prohibition"),
            ("OPERATOR", "approve", "allow", "permission"),
            ("PLANNER", "summarize", "allow", "permission"),
            ("PLANNER", "translate", "deny", "default"),
            ("PLANNER", "summarize", "allow", "permission")]
        assert [why for _, why in events(r.audit, "unauthenticated_request")] == [
            "missing", "bad_mac"]
        capsys.readouterr()

    def test_grpc_mutual_tls_and_push(self, tmp_path, grpc_extra, pki, capsys):
        grpc, pb = grpc_extra
        hook = Hook()
        port_file = tmp_path / "grpc-port"
        r = Run(tmp_path, extra=(*pki.gateway_args(), "--grpc-listen", "127.0.0.1:0",
                                 "--grpc-port-file", str(port_file),
                                 "--push-allow", f"{hook.url}/hooks/"))
        wait_for(lambda: port_file.exists() and port_file.read_text().strip(), "the port")
        port = int(port_file.read_text())
        g = GrpcClient(r, grpc, pb, port, pki, "planner")
        req = grpc_message(pb, "summarize")
        req.configuration.task_push_notification_config.url = f"{hook.url}/hooks/g"
        req.configuration.task_push_notification_config.token = "tok"
        got = g.call("PLANNER", "SendMessage", req, pb.SendMessageResponse)
        assert got.task.id == "up-task-1"
        assert r.agent.notify("up-task-1", status_event("up-task-1"),
                              context=pki.context("summarizer")) == [204]
        assert hook.got[-1]["headers"]["X-A2A-Notification-Token"] == "tok"
        cfgs = g.call("PLANNER", "ListTaskPushNotificationConfigs",
                      pb.ListTaskPushNotificationConfigsRequest(task_id="up-task-1"),
                      pb.ListTaskPushNotificationConfigsResponse)
        assert [c.url for c in cfgs.configs] == [f"{hook.url}/hooks/g"]
        # A certificate of another actor than the header's, and none.
        other = GrpcClient(r, grpc, pb, port, pki, "operator")
        assert other.error(other.call, "PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                           pb.SendMessageResponse) == ("UNAUTHENTICATED", None)
        nocert = GrpcClient(r, grpc, pb, port, pki, None)
        assert nocert.error(nocert.call, "PLANNER", "SendMessage",
                            grpc_message(pb, "summarize"),
                            pb.SendMessageResponse)[0] == "UNAVAILABLE"
        for c in (g, other, nocert):
            c.channel.close()
        r.stop()
        hook.close()
        assert r.agent.sent() == ["summarize"]
        assert [why for _, why in events(r.audit, "unauthenticated_request")] == [
            "certificate_mismatch"]
        assert [e for e, _ in events(r.audit, "push_config", "push_sent")] == [
            "push_config", "push_sent"]
        r.replay_check()
        capsys.readouterr()

    def test_grpc_needs_tls_off_loopback(self, tmp_path):
        from cadl.a2a_gateway import serve
        from cadl.gateway_core import GatewayError
        with pytest.raises(GatewayError, match="loopback"):
            serve({}, listen="127.0.0.1:0", grpc_listen="0.0.0.0:0")
