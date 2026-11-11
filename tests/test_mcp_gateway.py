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
from cadl.mcp_gateway import Gateway, GatewayError
from cadl.norms_eval import load_version

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "agents_tool_governance.cadl"
FAKE = Path(__file__).parent / "mcp_fake_server.py"
BOOT = "import sys; from cadl.cli import main; sys.exit(main(sys.argv[1:]))"
KEY = b"gateway-test-key-0123456789"


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
        args = ["mcp-gateway", str(cadl), "--agent", agent, "--server-actor", server,
                "--audit", str(self.audit), "--approvals", str(self.inbox),
                "--poll", "0.02", *extra]
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

    def approve(self, hold, decision="approve", actor="OPERATOR") -> None:
        assert main(["mcp-approve", str(self.inbox), hold, decision, "--as", actor]) == 0

    def message(self, text, *args) -> None:
        assert main(["mcp-message", str(self.inbox), text,
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
        r.no_response(1, wait=0.2)
        resp = r.response(1)
        assert is_denial(resp, "expired", "fallback: PLANNER -> TICKETS : create_ticket")
        r.approve("h-1")                                    # too late
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
        r = Run(tmp_path)
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
        r.approve("h-9")                                 # no such hold
        r.message("PLANNER -> OPERATOR : approve", "h-1")  # not a rule of G-9: no effect
        r.wait_lines(4)
        r.no_response(1)
        r.close()
        result = r.replay_check(tmp_path)
        assert [(a["event"], a["reason"]) for a in result["audit"]
                if a.get("event", "").startswith("unauthorised")] == \
            [("unauthorised_approval", "unknown_hold")]
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
    gw = Gateway(EXAMPLE, "PLANNER", "FILES", tmp_path / "a.jsonl",
                 tmp_path / "a.trace.jsonl", client.append, server.append,
                 inbox=tmp_path / "in", clock=clock, **kw)
    return gw, client, server, clock


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
