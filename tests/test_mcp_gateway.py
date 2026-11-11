"""`cadl mcp-gateway` (Appendix G, G.11): an MCP stdio proxy that decides
every tools/call with the online runtime, holds escalated calls, and
writes an audit log and a trace. No network: a fake MCP server
(`mcp_fake_server.py`) and a fake client drive the gateway as
subprocesses. Every scenario ends with the key consistency test: the
trace that the gateway wrote, replayed by `cadl agents-run`, gives the
same decisions and the same audit log, byte for byte."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cadl.agents_runtime import AgentsRuntime, load_run_trace, run
from cadl.audit_log import read_records, verify
from cadl.cli import main
from cadl.mcp_gateway import Gateway, GatewayError, approval_file, message_mac
from cadl.norms_eval import load_version

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "agents_tool_governance.cadl"
FAKE = Path(__file__).parent / "mcp_fake_server.py"
BOOT = "import sys; from cadl.cli import main; sys.exit(main(sys.argv[1:]))"
KEY = b"gateway-test-key-0123456789"
APPROVER_KEYS = {"OPERATOR": b"operator-key-0123456789abcdef",
                 "PLANNER": b"planner-key-0123456789abcdef"}


def keyring(directory: Path) -> Path:
    """A keyring for --approver-keys and the key files of the approvers."""
    ring = {}
    for actor, key in APPROVER_KEYS.items():
        (directory / f"{actor}.key").write_bytes(key)
        ring[actor] = f"{actor}.key"
    path = directory / "approvers.json"
    path.write_text(json.dumps(ring))
    return path


def short_timeout(tmp_path, seconds: str = "2s") -> Path:
    f = tmp_path / "governance.cadl"
    f.write_text(EXAMPLE.read_text().replace("timeout: 10min", f"timeout: {seconds}"))
    return f


class Run:
    """A gateway subprocess with a fake server, and a fake client."""

    def __init__(self, tmp_path, agent="PLANNER", server="FILES", cadl=EXAMPLE,
                 key: bool = False, extra=(), name="gw"):
        self.dir = tmp_path / name
        self.dir.mkdir()
        self.cadl = cadl
        self.audit = self.dir / "audit.jsonl"
        self.trace = self.dir / "audit.trace.jsonl"
        self.inbox = self.dir / "inbox"
        self.server_log = self.dir / "server.log"
        self.key = None
        self.ring = keyring(self.dir)
        args = ["mcp-gateway", str(cadl), "--agent", agent, "--server-actor", server,
                "--audit", str(self.audit), "--approvals", str(self.inbox),
                "--approver-keys", str(self.ring), "--poll", "0.02", *extra]
        if key:
            self.key = self.dir / "key"
            self.key.write_bytes(KEY)
            args += ["--audit-key", str(self.key)]
        args += ["--", sys.executable, str(FAKE), str(self.server_log)]
        self.proc = subprocess.Popen([sys.executable, "-c", BOOT, *args],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.out: queue.Queue = queue.Queue()
        self.seen: dict = {}
        threading.Thread(target=self._read, daemon=True).start()
        self._wait(lambda: self.trace.exists() and self.trace.read_text().strip(), "start")

    def _read(self):
        for raw in iter(self.proc.stdout.readline, b""):
            self.out.put(json.loads(raw))

    @staticmethod
    def _wait(cond, what, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")

    def send(self, obj) -> None:
        text = obj if isinstance(obj, str) else json.dumps(obj)
        self.proc.stdin.write(text.encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, id_, name, **arguments) -> None:
        self.send({"jsonrpc": "2.0", "id": id_, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}})

    def response(self, id_, timeout=30.0) -> dict:
        end = time.monotonic() + timeout
        while id_ not in self.seen:
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no response for id {id_!r}")
            try:
                msg = self.out.get(timeout=left)
            except queue.Empty:
                continue
            self.seen[msg.get("id")] = msg
        return self.seen.pop(id_)

    def no_response(self, id_, wait=0.3) -> None:
        time.sleep(wait)
        while not self.out.empty():
            msg = self.out.get()
            self.seen[msg.get("id")] = msg
        assert id_ not in self.seen

    def lines(self) -> int:
        return len(self.trace.read_text().splitlines())

    def wait_lines(self, n) -> None:
        self._wait(lambda: self.lines() >= n, f"{n} trace lines")

    def wait_hold(self, hold) -> Path:
        # The trace line of a call is written before it is decided, and the
        # hold is published after the decision: wait for the published hold,
        # not only for the trace line (a race on slow machines).
        path = self.inbox / "holds" / f"{hold}.json"
        self._wait(path.exists, f"the published hold {hold}")
        return path

    def approve(self, hold, decision="approve", actor="OPERATOR") -> None:
        self.wait_hold(hold)
        assert main(["mcp-approve", str(self.inbox), hold, decision, "--as", actor,
                     "--key", str(self.dir / f"{actor}.key")]) == 0

    def message(self, text, *args) -> None:
        sender = text.split("->")[0].strip()
        assert main(["mcp-message", str(self.inbox), text, "--key",
                     str(self.dir / f"{sender}.key"),
                     *[a for x in args for a in ("--arg", x)]]) == 0

    def server_calls(self) -> list[str]:
        if not self.server_log.exists():
            return []
        return [m["params"]["name"] for m in map(json.loads, self.server_log.read_text()
                                                  .splitlines())
                if m.get("method") == "tools/call"]

    def close(self, expect=0) -> str:
        self.proc.stdin.close()
        assert self.proc.wait(timeout=30) == expect
        return self.proc.stderr.read().decode()

    def replay_check(self, tmp_path) -> dict:
        """The key consistency test: `cadl agents-run` on the trace written
        by the gateway decides the same and writes the same audit log."""
        replayed = self.dir / "replayed.jsonl"
        args = ["agents-run", str(self.cadl), "--trace", str(self.trace),
                "--audit", str(replayed), "--format", "json"]
        if self.key:
            args += ["--audit-key", str(self.key)]
        assert main(args) == 0
        assert replayed.read_bytes() == self.audit.read_bytes()
        key = KEY if self.key else None
        result = verify(self.audit, key=key)
        assert result["valid"] and result["complete"], result
        return run(load_version(self.cadl), load_run_trace(self.trace))


def is_denial(resp, *words) -> bool:
    res = resp.get("result") or {}
    text = " ".join(c.get("text", "") for c in res.get("content", []))
    return res.get("isError") is True and all(w in text for w in words)


def decisions(result) -> list[tuple]:
    return [(s["message"]["from"], s["message"]["name"], s["result"]["decision"],
             s["result"]["reason"]) for s in result["steps"]]


class TestScenarios:
    """The scenarios of examples/agents_tool_governance.cadl through the
    gateway, each replayed through agents-run."""

    def test_allow_deny_undeclared(self, tmp_path, capsys):
        r = Run(tmp_path, agent="PLANNER", server="TICKETS")
        r.call(1, "search_tickets", q="zq-secret-arg")
        assert r.response(1)["result"]["content"][0]["text"] == "search_tickets done"
        r.call(2, "format_disk")                       # not a tool of TICKETS
        assert is_denial(r.response(2), "undeclared_tool")
        r.close()
        assert r.server_calls() == ["search_tickets"]
        result = r.replay_check(tmp_path)
        assert decisions(result) == [("PLANNER", "search_tickets", "allow", "permission"),
                                     ("PLANNER", "format_disk", "deny", "undeclared_tool")]
        assert "zq-secret-arg" not in r.trace.read_text() + r.audit.read_text()   # no arguments
        capsys.readouterr()

    def test_deny_without_permission(self, tmp_path, capsys):
        r = Run(tmp_path, agent="SUMMARIZER", server="FILES", key=True)
        r.call("a", "read_file", path="/etc/passwd")
        assert is_denial(r.response("a"), "reason: default")
        r.close()
        assert r.server_calls() == []
        assert decisions(r.replay_check(tmp_path)) == [
            ("SUMMARIZER", "read_file", "deny", "default")]
        capsys.readouterr()

    def test_escalate_approved(self, tmp_path, capsys):
        r = Run(tmp_path)
        r.call(1, "delete_file", path="/tmp/x")
        r.wait_lines(2)
        r.no_response(1)
        assert r.server_calls() == []
        r.approve("h-1")
        resp = r.response(1)
        assert resp["result"]["content"][0]["text"] == "delete_file done"
        r.close()
        assert r.server_calls() == ["delete_file"]
        result = r.replay_check(tmp_path)
        assert [h["state"] for h in result["holds"]] == ["approved"]
        assert result["holds"][0]["released"] is True
        assert decisions(result) == [("PLANNER", "delete_file", "escalate", "prohibition"),
                                     ("OPERATOR", "approve", "allow", "default")]
        capsys.readouterr()

    def test_escalate_rejected(self, tmp_path, capsys):
        r = Run(tmp_path)
        r.call(1, "delete_file")
        r.wait_lines(2)
        r.approve("h-1", "reject")
        resp = r.response(1)
        assert is_denial(resp, "rejected", "hold h-1", "fallback: PLANNER -> TICKETS : "
                                                       "create_ticket", "notify: OPERATOR")
        r.close()
        assert r.server_calls() == []
        assert [h["state"] for h in r.replay_check(tmp_path)["holds"]] == ["rejected"]
        capsys.readouterr()

    def test_escalate_timeout_with_fallback(self, tmp_path, capsys):
        r = Run(tmp_path, cadl=short_timeout(tmp_path))
        r.call(1, "delete_file")
        r.wait_lines(2)
        r.wait_hold("h-1")   # the approval is bound to the published hold
        late = approval_file(r.inbox, "h-1", "approve", "OPERATOR",
                             APPROVER_KEYS["OPERATOR"])
        r.no_response(1, wait=0.2)
        resp = r.response(1)
        assert is_denial(resp, "expired", "fallback: PLANNER -> TICKETS : create_ticket")
        assert not (r.inbox / "holds" / "h-1.json").exists()   # no longer published
        (r.inbox / "late.json").write_text(json.dumps(late))   # too late
        r.wait_lines(4)
        r.close()
        assert r.server_calls() == []
        result = r.replay_check(tmp_path)
        assert [h["state"] for h in result["holds"]] == ["expired"]
        assert any(a.get("event") == "unauthorised_approval" and a["reason"] == "not_held"
                   for a in result["audit"])
        capsys.readouterr()

    def test_delegation_then_delegated_call(self, tmp_path, capsys):
        r = Run(tmp_path, agent="SUMMARIZER")
        r.call(1, "read_file")
        assert is_denial(r.response(1), "default")
        r.message("OPERATOR -> PLANNER : grant_read")
        r.message("PLANNER -> SUMMARIZER : grant_read")
        r.wait_lines(4)
        r.call(2, "read_file")
        assert r.response(2)["result"]["isError"] is False
        r.message("OPERATOR -> PLANNER : revoke_read")
        r.wait_lines(6)
        r.call(3, "read_file")
        assert is_denial(r.response(3), "default")
        r.close()
        assert r.server_calls() == ["read_file"]
        result = r.replay_check(tmp_path)
        assert [d[2:] for d in decisions(result)] == [
            ("deny", "default"), ("allow", "default"), ("allow", "default"),
            ("allow", "permission"), ("allow", "default"), ("deny", "default")]
        assert result["audit"][next(i for i, a in enumerate(result["audit"])
                                    if a.get("decision") == "allow"
                                    and a["message"]["name"] == "read_file")][
            "provenance"] == [["OPERATOR", "PLANNER", "SUMMARIZER"]]
        capsys.readouterr()


class TestProtocol:
    def test_malformed_unknown_and_filtering(self, tmp_path, capsys):
        r = Run(tmp_path)
        r.send("{not json")
        assert r.response(None)["error"]["code"] == -32700
        r.send(json.dumps([{"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "delete_file"}}]))
        assert r.response(None)["error"]["code"] == -32600
        r.send({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {}})
        assert r.response(8)["error"]["code"] == -32602
        r.call(9, "weird-name")
        assert r.response(9)["error"]["code"] == -32602
        r.send({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "read_file", "arguments": [1]}})
        assert r.response(10)["error"]["code"] == -32602
        r.send({"jsonrpc": "2.0", "id": 11, "method": "nothing/here"})
        assert r.response(11)["error"]["code"] == -32601           # from the server
        r.send({"jsonrpc": "2.0", "id": 12, "method": "initialize", "params": {}})
        assert r.response(12)["result"]["serverInfo"]["name"] == "fake"
        r.send({"jsonrpc": "2.0", "id": 13, "method": "tools/list"})
        assert [t["name"] for t in r.response(13)["result"]["tools"]] == \
            ["read_file", "delete_file"]
        r.close()
        assert r.server_calls() == []                    # nothing malformed got through
        assert r.replay_check(tmp_path)["steps"] == []
        capsys.readouterr()

    def test_no_filter_tools(self, tmp_path, capsys):
        r = Run(tmp_path, extra=("--no-filter-tools",))
        r.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert len(r.response(1)["result"]["tools"]) == 6
        r.close()
        capsys.readouterr()

    def test_concurrent_calls_and_held_calls_do_not_block(self, tmp_path, capsys):
        r = Run(tmp_path)
        r.message("OPERATOR -> PLANNER : grant_read")
        r.wait_lines(2)
        r.call(1, "delete_file")                         # held
        r.call(2, "read_file", sleep=0.4)                # slow
        r.call(3, "read_file")                           # fast
        r.call(4, "format_disk")                         # denied at once
        first = [r.out.get(timeout=30) for _ in range(3)]
        # the slow call is answered last; the held call does not block them
        assert sorted(m["id"] for m in first) == [2, 3, 4] and first[-1]["id"] == 2
        r.no_response(1, wait=0.1)
        r.approve("h-1")
        assert r.response(1)["result"]["isError"] is False
        r.close()
        result = r.replay_check(tmp_path)
        # decided in the order of arrival
        assert [d[1:3] for d in decisions(result)] == [
            ("grant_read", "allow"), ("delete_file", "escalate"), ("read_file", "allow"),
            ("read_file", "allow"), ("format_disk", "deny"), ("approve", "allow")]
        capsys.readouterr()

    def test_server_crash(self, tmp_path, capsys):
        r = Run(tmp_path, extra=("--allow-method", "test/crash"))
        r.call(1, "delete_file")
        r.wait_lines(2)
        r.send({"jsonrpc": "2.0", "id": 2, "method": "test/crash"})
        got = {r.response(i)["error"]["code"] for i in (1, 2)}
        assert got == {-32603}
        err = r.close(expect=1)
        assert "closed its output" in err
        assert r.server_calls() == []
        r.replay_check(tmp_path)
        capsys.readouterr()

    def test_client_disconnect_with_a_held_call(self, tmp_path, capsys):
        r = Run(tmp_path)
        r.call(1, "delete_file")
        r.wait_lines(2)
        err = r.close()
        assert "1 held call(s) were not answered: h-1" in err
        result = r.replay_check(tmp_path)
        assert [h["state"] for h in result["holds"]] == ["cancelled"]   # instance ended
        capsys.readouterr()

    def test_unreadable_approval_is_not_an_approval(self, tmp_path, capsys):
        r = Run(tmp_path, cadl=short_timeout(tmp_path))
        r.call(1, "delete_file")
        r.wait_lines(2)
        (r.inbox / "001.json").write_text("{not json")
        (r.inbox / "002.json").write_text(json.dumps(
            {"kind": "approval", "hold_id": "h-1", "decision": "approve", "as": "NOBODY[x]"}))
        (r.inbox / "003.json").write_text(json.dumps(
            {"kind": "approval", "hold_id": "h-1", "decision": "approve", "as": "PLANNER"}))
        resp = r.response(1)
        assert is_denial(resp, "expired")
        assert sorted(p.name for p in (r.inbox / "invalid").glob("*.json")) == \
            ["001.json", "002.json", "003.json"]
        r.close()
        assert r.server_calls() == []
        r.replay_check(tmp_path)
        capsys.readouterr()

    def test_approval_of_another_hold_is_unauthorised(self, tmp_path, capsys):
        r = Run(tmp_path)
        r.call(1, "delete_file")
        r.wait_lines(2)
        hold = json.loads(r.wait_hold("h-1").read_text())
        forged = {"kind": "approval", "hold_id": "h-9", "decision": "approve",
                  "as": "OPERATOR", "run_id": hold["run_id"], "message": hold["message"],
                  "hold_time": hold["hold_time"], "nonce": "1" * 32}
        forged["mac"] = message_mac(APPROVER_KEYS["OPERATOR"], forged)
        (r.inbox / "a.json").write_text(json.dumps(forged))     # no such hold
        r.wait_lines(3)
        r.message("PLANNER -> OPERATOR : approve", "h-1")  # not through mcp-approve
        r.wait_lines(4)
        r.no_response(1)
        r.close()
        result = r.replay_check(tmp_path)
        assert [(a["event"], a["reason"]) for a in result["audit"]
                if a.get("event", "").startswith("unauthorised")] == \
            [("unauthorised_approval", "unknown_hold"),
             ("unauthorised_message", "approval_as_message")]
        assert r.server_calls() == []
        capsys.readouterr()


class TestUsage:
    def test_usage_errors(self, tmp_path, capsys):
        base = ["mcp-gateway", str(EXAMPLE), "--audit", str(tmp_path / "a.jsonl")]
        srv = ["--", sys.executable, str(FAKE), str(tmp_path / "s.log")]
        assert main(base + ["--agent", "PLANNER", "--server-actor", "PLANNER"] + srv) == 2
        assert main(base + ["--agent", "PLANNER", "--server-actor", "NOPE"] + srv) == 2
        assert main(base + ["--agent", "A B", "--server-actor", "FILES"] + srv) == 2
        assert main(base + ["--agent", "PLANNER", "--server-actor", "FILES"]) == 2
        (tmp_path / "exists.jsonl").write_text("")
        assert main(["mcp-gateway", str(EXAMPLE), "--audit", str(tmp_path / "exists.jsonl"),
                     "--agent", "PLANNER", "--server-actor", "FILES"] + srv) == 2
        assert main(["mcp-approve", str(tmp_path / "none"), "h-1", "approve",
                     "--as", "OPERATOR"]) == 2
        assert main(["mcp-message", str(tmp_path), "not a message"]) == 2
        err = capsys.readouterr().err
        assert "not a tool server" in err and "exists" in err
        assert not (tmp_path / "s.log").exists()       # no server was started


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def gateway(tmp_path, **kw):
    client, server = [], []
    clock = FakeClock()
    kw.setdefault("approver_keys", dict(APPROVER_KEYS))
    gw = Gateway(EXAMPLE, "PLANNER", "FILES", tmp_path / "a.jsonl",
                 tmp_path / "a.trace.jsonl", client.append, server.append,
                 inbox=tmp_path / "in", clock=clock, **kw)
    return gw, client, server, clock


def replay_equal(tmp_path, cadl=EXAMPLE) -> dict:
    """Stop nothing; replay the trace of an in-process gateway with
    `cadl agents-run` and require the same audit log, byte for byte."""
    out = tmp_path / "replayed.jsonl"
    assert main(["agents-run", str(cadl), "--trace", str(tmp_path / "a.trace.jsonl"),
                 "--audit", str(out)]) == 0
    assert out.read_bytes() == (tmp_path / "a.jsonl").read_bytes()
    assert verify(tmp_path / "a.jsonl")["valid"]
    return run(load_version(cadl), load_run_trace(tmp_path / "a.trace.jsonl"))


def server_view(lines) -> list:
    """What a server that reads its input with universal newlines (as the
    MCP Python SDK and mcp_fake_server.py do) parses from the lines sent."""
    import io
    raw = "".join(x + "\n" for x in lines).encode("utf-8")
    out = []
    for line in io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append(None)
    return out


def tool_calls(lines) -> list:
    return [m["params"].get("name") for m in server_view(lines)
            if isinstance(m, dict) and m.get("method") == "tools/call"]


def call(id_, name) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": "tools/call",
                       "params": {"name": name}})


class TestInProcess:
    """The decision part of the gateway, with a fake clock."""

    def test_timeout_on_the_fake_clock(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(call(1, "delete_file"))
        assert client == [] and server == []
        clock.t += 600.0                    # exactly the deadline: still held
        gw.tick()
        assert client == []
        clock.t += 0.001
        gw.tick()
        assert is_denial(json.loads(client[0]), "expired")
        gw.stop()
        replayed = run(load_version(EXAMPLE), load_run_trace(tmp_path / "a.trace.jsonl"))
        assert replayed["audit"] == read_records(tmp_path / "a.jsonl")
        assert replayed["holds"][0]["state"] == "expired"

    def test_a_crash_of_the_decision_step_is_a_denial(self, tmp_path, monkeypatch):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(call(1, "delete_file"))                  # held
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        assert len(server) == 1                                  # tools/list forwarded

        def boom(self, line):
            raise RuntimeError("decision step crashed")
        monkeypatch.setattr(AgentsRuntime, "observe", boom)
        gw.client_line(call(3, "delete_file"))
        # the new call and the held call 1 are denied
        assert sorted(json.loads(x)["id"] for x in client[-2:]) == [1, 3]
        assert all(is_denial(json.loads(x), "failed") for x in client[-2:])
        assert gw.held == {}
        monkeypatch.undo()
        gw.client_line(call(4, "delete_file"))                  # still denied
        assert is_denial(json.loads(client[-1]), "failed")
        assert len(server) == 1                                  # nothing forwarded
        assert gw.failed and "decision step crashed" in gw.failed
        gw.stop()
        r = verify(tmp_path / "a.jsonl")
        assert not r["valid"] and "no end record" in r["error"]["what"]
        assert verify(tmp_path / "a.jsonl", allow_open=True)["valid"]

    def test_write_failure_is_fail_closed(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.trace_f.close()                                       # cannot write the trace
        gw.client_line(call(1, "delete_file"))
        assert is_denial(json.loads(client[-1]), "failed") and server == []

    def test_cancelled_held_call_is_not_sent(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(call(1, "delete_file"))
        gw.client_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                   "params": {"requestId": 1}}))
        assert server == [] and gw.held == {}
        gw.client_line(call(1, "delete_file"))                  # the id is free again
        assert "h-2" in gw.held

    def test_init_keys_and_the_first_line(self, tmp_path):
        with pytest.raises(GatewayError, match="--init"):
            gateway(tmp_path, init={"messages": []})
        gw, *_ = gateway(tmp_path, init={"request": {"x": 1}})
        first = json.loads((tmp_path / "a.trace.jsonl").read_text().splitlines()[0])
        assert first["t"] == 0 and first["quantities"] == [] and first["request"] == {"x": 1}
        assert first["epoch"].endswith("Z")
        gw.stop()


class TestOnlineRuntime:
    """`AgentsRuntime.open` / `observe` / `close` give what `run` gives."""

    @pytest.mark.parametrize("path", sorted((ROOT / "examples" / "traces")
                                            .glob("agents_*.jsonl")), ids=lambda p: p.name)
    def test_online_equals_offline(self, path):
        cadl = EXAMPLE if "tool_governance" in path.name else \
            ROOT / "examples" / "agents_change_control.cadl"
        lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        rt = AgentsRuntime.open(cadl, path=str(path))
        produced = []
        for line in lines:
            produced += rt.observe(line)["audit"]
        produced += rt.close()["audit"]
        offline = run(load_version(cadl),
                      load_run_trace_from(lines + [{"t": lines[-1]["t"]}], path))
        assert produced == rt.audit == offline["audit"]
        assert rt.result() == offline

    def test_closed_and_failed(self, tmp_path):
        from cadl.norms_eval import NormsEvalError, TraceError
        rt = AgentsRuntime.open(EXAMPLE)
        rt.observe({"t": 1})
        with pytest.raises(TraceError):
            rt.observe({"t": 0})                                 # time goes back
        assert rt.failed
        with pytest.raises(NormsEvalError, match="failed"):
            rt.observe({"t": 2})
        rt2 = AgentsRuntime.open(EXAMPLE)
        rt2.close(5)
        assert rt2.closed
        with pytest.raises(NormsEvalError, match="closed"):
            rt2.observe({"t": 6})


def load_run_trace_from(lines, path):
    from cadl.agents_runtime import parse_run_trace
    return parse_run_trace([json.dumps(x) for x in lines], path=str(path))


class TestReviewFindings:
    """Regression tests of the review of the first gateway (G1 to G7 and
    the documented gaps i to iii); each failed before the correction."""

    def test_g1_no_smuggling_through_a_carriage_return(self, tmp_path, capsys):
        gw, client, server, clock = gateway(tmp_path)
        inner = ('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":'
                 '{"name":"delete_file","arguments":{"path":"/"}}}')
        gw.client_line('{"jsonrpc":"2.0","method":"notifications/progress","params":\r'
                       + inner + '\r}')
        gw.client_line('{"jsonrpc":"2.0","id":8,"method":"tools/list","params":{"x":\r'
                       + inner.replace('"id":7', '"id":9') + '\r}}')
        assert server == [] and tool_calls(server) == []
        assert [json.loads(c)["error"]["code"] for c in client] == [-32600, -32600]
        # A forwarded message is the parsed object again, every non-ASCII
        # character escaped: no reader splits it.
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/list",
                                   "params": {"x": "a\u2028b\u2029c\u0085d"}},
                                  ensure_ascii=False) + "\r")
        assert len(server) == 1 and server[0].isascii() and len(server_view(server)) == 1
        gw.stop()
        replay_equal(tmp_path)

    def test_g1_end_to_end(self, tmp_path, capsys):
        r = Run(tmp_path)
        inner = ('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":'
                 '{"name":"delete_file","arguments":{}}}')
        r.proc.stdin.write(('{"jsonrpc":"2.0","method":"notifications/progress","params":\r'
                            + inner + '\r}\n').encode())
        r.proc.stdin.flush()
        assert r.response(None)["error"]["code"] == -32600
        r.close()
        assert r.server_calls() == []
        r.replay_check(tmp_path)
        capsys.readouterr()

    def test_g2_duplicate_keys_are_refused(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":'
                       '"delete_file","arguments":{}},"params":{"name":"read_file"}}')
        gw.client_line('{"jsonrpc":"2.0","id":4,"method":"tools/call","method":'
                       '"tools/list","params":{"name":"delete_file"}}')
        gw.client_line('{"jsonrpc":"2.0","id":5,"method":"tools/list","params":'
                       '{"a":{"b":1,"b":2}}}')
        assert server == []
        assert all(json.loads(c)["error"]["code"] == -32600 for c in client)
        assert len(client) == 3

    def test_g3_names_are_matched_in_full(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        for k, name in enumerate(["read_file\n", "delete_file\u0000", "\uff44elete_file"]):
            gw.client_line(json.dumps({"jsonrpc": "2.0", "id": k, "method": "tools/call",
                                       "params": {"name": name}}))
        assert [json.loads(c)["error"]["code"] for c in client] == [-32602] * 3
        assert gw.failed is None                # no denial of service
        from cadl.agents import parse_concrete_actor
        assert parse_concrete_actor("PLANNER\n") is None
        with pytest.raises(GatewayError, match="concrete actor"):
            Gateway(EXAMPLE, "PLANNER\n", "FILES", tmp_path / "b.jsonl", tmp_path / "b.t",
                    client.append, server.append)

    def test_g4_methods_are_allowed_by_a_list(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        for k, method in enumerate(["resources/read", "prompts/get", "tools/lists",
                                    "Tools/List", "x/custom"]):
            gw.client_line(json.dumps({"jsonrpc": "2.0", "id": k, "method": method}))
        gw.client_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/roots/x"}))
        assert server == []
        assert [json.loads(c)["error"]["code"] for c in client] == [-32601] * 5
        for k, method in enumerate(["initialize", "ping", "server/discover", "tools/list"]):
            gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 100 + k, "method": method}))
        gw.client_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        assert len(server) == 5
        gw.stop()
        result = replay_equal(tmp_path)
        assert [a["reason"] for a in result["audit"] if a.get("event") == "method_refused"] \
            == ["resources/read", "prompts/get", "tools/lists", "Tools/List", "x/custom",
                "notifications/roots/x"]

    def test_g4_allow_method(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path, allow_methods=("resources/read",))
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/read"}))
        assert len(server) == 1 and client == []

    def test_g4_server_requests_are_refused(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.server_line(json.dumps({"jsonrpc": "2.0", "id": "s1",
                                   "method": "sampling/createMessage", "params": {}}))
        gw.server_line(json.dumps({"jsonrpc": "2.0", "id": "s2", "method": "tools/call",
                                   "params": {"name": "delete_file"}}))
        gw.server_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress",
                                   "params": {}}))
        assert len(client) == 1 and json.loads(client[0])["method"] == "notifications/progress"
        assert [json.loads(x)["error"]["code"] for x in server] == [-32601, -32601]
        gw.stop()
        replay_equal(tmp_path)

    def test_g5_unsolicited_responses_are_dropped(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(call(5, "delete_file"))                 # held, never forwarded
        forged = {"jsonrpc": "2.0", "id": 5, "result": {"content": [], "isError": False}}
        gw.server_line(json.dumps(forged))
        gw.server_line(json.dumps(dict(forged, id="never")))
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}))
        gw.server_line(json.dumps({"jsonrpc": "2.0", "id": 6, "result": {"tools": []}}))
        gw.server_line(json.dumps({"jsonrpc": "2.0", "id": 6, "result": {"tools": []}}))
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": "c", "result": {}}))
        assert [json.loads(c).get("id") for c in client] == [6]  # one answer for id 6
        assert len(server) == 1                                  # the tools/list request
        gw.stop()
        result = replay_equal(tmp_path)
        assert [a["reason"] for a in result["audit"]
                if a.get("event") == "unsolicited_response"] == \
            ["server: 5", 'server: "never"', "server: 6", 'client: "c"']

    def test_g6_inbox_files_and_directory(self, tmp_path):
        import os
        gw, client, server, clock = gateway(tmp_path, approver_keys=None,
                                            insecure_approvals=True)
        gw.client_line(call(1, "delete_file"))
        inbox = tmp_path / "in"
        outside = tmp_path / "evil.json"
        outside.write_text(json.dumps({"kind": "approval", "hold_id": "h-1",
                                       "decision": "approve", "as": "OPERATOR"}))
        os.symlink(outside, inbox / "1-link.json")
        os.link(outside, inbox / "2-hard.json")
        gw.tick()
        assert server == [] and "h-1" in gw.held
        assert sorted(p.name for p in (inbox / "invalid").glob("*.json")) == \
            ["1-link.json", "2-hard.json"]
        gw.stop()
        bad = tmp_path / "open"
        bad.mkdir(mode=0o777)
        os.chmod(bad, 0o777)
        with pytest.raises(GatewayError, match="writable"):
            Gateway(EXAMPLE, "PLANNER", "FILES", tmp_path / "c.jsonl", tmp_path / "c.t",
                    client.append, server.append, inbox=bad, insecure_approvals=True)
        with pytest.raises(GatewayError, match="approver-keys"):
            Gateway(EXAMPLE, "PLANNER", "FILES", tmp_path / "d.jsonl", tmp_path / "d.t",
                    client.append, server.append, inbox=tmp_path / "in2")

    def test_g7_lone_surrogates(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path, approver_keys=None,
                                            insecure_approvals=True)
        gw.client_line(call(1, "delete_file"))
        (tmp_path / "in" / "a.json").write_text(
            '{"kind":"approval","hold_id":"\\ud800","decision":"approve","as":"OPERATOR"}')
        gw.tick()
        assert gw.failed is None and (tmp_path / "in" / "invalid" / "a.json").exists()
        gw.client_line(call(2, "delete_file"))
        assert "h-2" in gw.held
        gw.client_line('{"jsonrpc":"2.0","id":3,"method":"tools/list","params":'
                       '{"x":"\\udc00"}}')
        assert json.loads(client[-1])["error"]["code"] == -32600
        gw.stop()
        replay_equal(tmp_path)
        trace = tmp_path / "sur.jsonl"
        trace.write_text('{"t":0,"messages":[{"from":"OPERATOR","to":"PLANNER",'
                         '"name":"approve","args":["\\ud800"]}]}\n')
        assert main(["agents-run", str(EXAMPLE), "--trace", str(trace)]) == 2
        assert main(["norms-eval", str(EXAMPLE), "--trace", str(trace)]) == 2

    def _approval(self, gw, hold_id="h-1", actor="OPERATOR", key=None, **change):
        from cadl.mcp_gateway import approval_file
        obj = approval_file(gw.inbox, hold_id, "approve", actor,
                            APPROVER_KEYS.get(actor) if key is None else key)
        obj.update(change)
        if change and "mac" not in change:
            obj["mac"] = message_mac(APPROVER_KEYS.get(actor) if key is None else key, obj)
        return obj

    def test_i_authenticated_approvals(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(call(1, "delete_file"))
        inbox = tmp_path / "in"
        good = self._approval(gw)
        cases = [
            ("unauthenticated", dict(good, mac="0" * 64)),
            ("unauthenticated", {k: v for k, v in good.items() if k != "mac"}),
            ("unauthenticated", self._approval(gw, key=b"not-the-operator-key-0123")),
            ("wrong_run", self._approval(gw, run_id="f" * 32)),
            ("wrong_message", self._approval(gw, message={"from": "PLANNER", "to": "FILES",
                                                           "name": "read_file"})),
            ("wrong_message", self._approval(gw, hold_time="2000-01-01T00:00:00Z")),
        ]
        for k, (_, obj) in enumerate(cases):
            (inbox / f"{k}.json").write_text(json.dumps(obj))
            clock.t += 1
            gw.tick()
        assert server == [] and "h-1" in gw.held
        (inbox / "9.json").write_text(json.dumps(good))
        clock.t += 1
        gw.tick()
        assert len(server) == 1 and json.loads(server[0])["id"] == 1
        (inbox / "9b.json").write_text(json.dumps(good))       # the same file again
        gw.tick()
        gw.stop()
        result = replay_equal(tmp_path)
        unauth = [a["reason"] for a in result["audit"]
                  if a.get("event") == "unauthorised_approval"]
        assert unauth == [c[0] for c in cases] + ["replayed"]
        evidence = [a for a in result["audit"] if a.get("event") == "approval_evidence"]
        assert len(evidence) == 1 and evidence[0]["reason"].startswith("file=sha256:")
        assert f"nonce={good['nonce']}" in evidence[0]["reason"]
        assert f"params=sha256:{good['params_sha256']}" in evidence[0]["reason"]
        # the evidence comes before the decision of the approval and its release
        k = next(i for i, a in enumerate(result["audit"]) if a.get("outcome") == "approved")
        assert result["audit"].index(evidence[0]) == k - 2

    def test_i_usage(self, tmp_path, capsys):
        ring = keyring(tmp_path)
        srv = ["--", sys.executable, str(FAKE), str(tmp_path / "s.log")]
        base = ["mcp-gateway", str(EXAMPLE), "--agent", "PLANNER", "--server-actor",
                "FILES", "--approvals", str(tmp_path / "in")]
        assert main(base + ["--audit", str(tmp_path / "a1.jsonl")] + srv) == 2
        (tmp_path / "audit.key").write_bytes(APPROVER_KEYS["OPERATOR"])
        assert main(base + ["--audit", str(tmp_path / "a2.jsonl"), "--approver-keys",
                            str(ring), "--audit-key", str(tmp_path / "audit.key")]
                    + srv) == 2
        err = capsys.readouterr().err
        assert "needs --approver-keys" in err and "key of the audit log" in err
        assert not (tmp_path / "s.log").exists()

    def test_ii_cancel_then_approve_is_withdrawn(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(call(1, "delete_file"))
        good = self._approval(gw)
        clock.t += 1
        gw.client_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                   "params": {"requestId": 1}}))
        clock.t += 1
        (tmp_path / "in" / "a.json").write_text(json.dumps(good))
        gw.tick()
        assert server == [] and client == []
        gw.stop()
        result = replay_equal(tmp_path)
        assert [h["state"] for h in result["holds"]] == ["withdrawn"]
        assert result["summary"]["holds"]["withdrawn"] == 1
        closing = [a for a in result["audit"] if a.get("outcome") == "withdrawn"]
        assert closing and closing[0]["decision"] == "deny" and \
            closing[0]["reason"] == "withdrawn"
        assert any(a.get("event") == "unauthorised_approval" and a["reason"] == "not_held"
                   for a in result["audit"])
        released = [s for s in result["releases"]]
        assert released == []

    def test_iii_run_id(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        first = json.loads((tmp_path / "a.trace.jsonl").read_text().splitlines()[0])
        head = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
        assert first["run_id"] == head["run_id"] == gw.run_id and len(gw.run_id) == 32
        published = json.loads((tmp_path / "in" / "run.json").read_text())
        assert published["run_id"] == gw.run_id
        gw.stop()
        replay_equal(tmp_path)
        # two runs never share a run id; a log of one run does not verify
        # as the log of another
        (tmp_path / "second").mkdir()
        gw2, *_ = gateway(tmp_path / "second")
        assert gw2.run_id != gw.run_id
        gw2.stop()
        r = verify(tmp_path / "a.jsonl", run_id=gw2.run_id)
        assert not r["valid"] and "run id" in r["error"]["what"]


class TestSecondReview:
    """Regression tests of the second review (M1, W2, W3, L1); each failed
    on bfc4302."""

    def test_m1_non_finite_numbers_are_refused_before_a_decision(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
                       '{"name":"delete_file","arguments":{"n":-1e999}}}')
        gw.client_line('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":'
                       '{"n":1e999}}')
        assert [json.loads(c)["error"]["code"] for c in client] == [-32600, -32600]
        assert server == [] and gw.held == {} and gw.failed is None
        gw.stop()
        assert replay_equal(tmp_path)["steps"] == []        # nothing was decided

    def test_m1_arguments_outside_the_canonical_form(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "delete_file",
                                              "arguments": {"n": 2 ** 60}}}))
        assert json.loads(client[0])["error"]["code"] == -32602
        assert gw.held == {} and server == []

    def test_m1_a_failure_after_the_decision_is_recorded(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(call(1, "delete_file"))               # h-1
        gw.client_line(call(2, "delete_file"))               # h-2
        good = TestReviewFindings._approval(None, gw, "h-1")
        sent = []

        def broken(text):
            raise OSError("pipe closed")
        gw.to_server = broken
        (tmp_path / "in" / "a.json").write_text(json.dumps(good))
        clock.t += 1
        gw.tick()
        gw.to_server = sent.append
        replies = {json.loads(c)["id"]: json.loads(c) for c in client}
        assert replies[1]["error"]["code"] == -32603          # released, not sent
        assert "h-2" in gw.held and 2 not in replies          # the other hold is intact
        assert gw.failed is None
        gw.stop()
        result = replay_equal(tmp_path)
        failed = [a for a in result["audit"] if a.get("event") == "forward_failed"]
        assert len(failed) == 1 and failed[0]["message"]["name"] == "delete_file"

    def test_m1_fail_held_withdraws_in_the_runtime(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(call(1, "delete_file"))
        gw.client_line(call(2, "delete_file"))
        gw._fail_held()
        assert sorted(json.loads(c)["id"] for c in client) == [1, 2]
        gw.stop()
        result = replay_equal(tmp_path)
        assert [h["state"] for h in result["holds"]] == ["withdrawn", "withdrawn"]

    def test_w2_an_approval_is_bound_to_the_arguments(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        for k, path in ((1, "/tmp/x"), (2, "/")):          # same time, same message
            gw.client_line(json.dumps({"jsonrpc": "2.0", "id": k, "method": "tools/call",
                                       "params": {"name": "delete_file",
                                                  "arguments": {"path": path}}}))
        holds = {h: json.loads((tmp_path / "in" / "holds" / f"{h}.json").read_text())
                 for h in ("h-1", "h-2")}
        assert holds["h-1"]["params"]["arguments"] == {"path": "/tmp/x"}
        assert holds["h-1"]["hold_time"] == holds["h-2"]["hold_time"]
        assert holds["h-1"]["params_sha256"] != holds["h-2"]["params_sha256"]
        # an approval made for h-1, re-addressed to h-2 and signed again
        moved = TestReviewFindings._approval(None, gw, "h-1")
        moved["hold_id"] = "h-2"
        moved["mac"] = message_mac(APPROVER_KEYS["OPERATOR"], moved)
        (tmp_path / "in" / "a.json").write_text(json.dumps(moved))
        gw.tick()
        assert server == [] and "h-2" in gw.held
        good = TestReviewFindings._approval(None, gw, "h-1")
        (tmp_path / "in" / "b.json").write_text(json.dumps(good))
        gw.tick()
        assert [json.loads(x)["id"] for x in server] == [1]
        assert json.loads(server[0])["params"]["arguments"] == {"path": "/tmp/x"}
        gw.stop()
        result = replay_equal(tmp_path)
        assert [a["reason"] for a in result["audit"]
                if a.get("event") == "unauthorised_approval"] == ["wrong_message"]

    def test_w2_mcp_approve_shows_the_arguments(self, tmp_path, capsys):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "delete_file",
                                              "arguments": {"path": "/etc/passwd"}}}))
        capsys.readouterr()
        assert main(["mcp-approve", str(tmp_path / "in"), "h-1", "approve", "--as",
                     "OPERATOR", "--show"]) == 0
        out = capsys.readouterr().out
        assert "/etc/passwd" in out and "PLANNER -> FILES : delete_file" in out
        assert list((tmp_path / "in").glob("*.json")) == [tmp_path / "in" / "run.json"]

    def test_w3_approvals_mode_and_evidence(self, tmp_path):
        modes = {}
        for name, kw in (("auth", {}), ("insecure", {"approver_keys": None,
                                                     "insecure_approvals": True})):
            d = tmp_path / name
            d.mkdir()
            gw, client, server, clock = gateway(d, **kw)
            gw.client_line(call(1, "delete_file"))
            from cadl.mcp_gateway import approval_file
            obj = approval_file(d / "in", "h-1", "approve", "OPERATOR",
                                APPROVER_KEYS["OPERATOR"] if name == "auth" else None)
            (d / "in" / "a.json").write_text(json.dumps(obj))
            gw.tick()
            gw.stop()
            result = replay_equal(d)
            head = json.loads((d / "a.jsonl").read_text().splitlines()[0])
            first = json.loads((d / "a.trace.jsonl").read_text().splitlines()[0])
            modes[name] = (head["approvals"], first["approvals"],
                           [a["event"] for a in result["audit"]
                            if a.get("event", "").startswith("approval_")])
        assert modes["auth"] == ("authenticated", "authenticated", ["approval_evidence"])
        assert modes["insecure"] == ("insecure", "insecure", ["approval_unauthenticated"])
        d = tmp_path / "none"
        d.mkdir()
        g = Gateway(EXAMPLE, "PLANNER", "FILES", d / "a.jsonl", d / "a.t", list().append,
                    list().append)
        g.stop()
        assert json.loads((d / "a.jsonl").read_text().splitlines()[0])["approvals"] == "none"

    def test_l1_deep_nesting_is_refused(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line("[" * 100000 + "]" * 100000)
        gw.client_line('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"x":'
                       + '{"a":' * 50000 + "1" + "}" * 50000 + "}}")
        gw.client_line('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"x":'
                       + "[" * 98 + "]" * 98 + "}}")              # depth 100: accepted
        codes = [json.loads(c)["error"]["code"] for c in client]
        assert codes == [-32600, -32600] and len(server) == 1 and gw.failed is None

    def test_l1_a_cancelled_forwarded_call_stays_in_flight(self, tmp_path):
        gw, client, server, clock = gateway(tmp_path)
        gw.client_line(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}))
        gw.client_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                   "params": {"requestId": 9}}))
        assert len(server) == 2                             # the cancellation is passed on
        gw.server_line(json.dumps({"jsonrpc": "2.0", "id": 9, "result": {"tools": []}}))
        assert [json.loads(c)["id"] for c in client] == [9]  # a late answer still passes
