"""Interoperability of the gateways with the official SDKs (Appendix G,
G.13): an MCP server and client of the `mcp` SDK through
`cadl mcp-gateway`, and an A2A agent and client of `a2a-sdk` through
`cadl a2a-gateway`. The tests are skipped when the SDKs are not
installed; the CI job "sdk-interop" installs them on Python 3.12
(`pip install -e .[...] mcp a2a-sdk ...`, see .github/workflows/ci.yml).
They are not runtime dependencies of cadl.

Each scenario covers allow, deny, a hold that is approved or rejected
through the inbox, and ends with the replay check: `cadl agents-run` on
the gateway's trace writes the same audit log, byte for byte."""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cadl.audit_log import read_records, verify
from cadl.cli import main

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "agents_tool_governance.cadl"
A2A_FILE = Path(__file__).parent / "data" / "a2a_delegated_work.cadl"
BOOT = "import sys; from cadl.cli import main; sys.exit(main(sys.argv[1:]))"
APPROVER_KEY = b"operator-approver-key-0123456789"
CLIENT_KEY = b"planner-client-key-0123456789"


def wait_for(cond, what: str, timeout: float = 30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = cond()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def keyring(directory: Path, actor: str, key: bytes, name: str) -> Path:
    (directory / f"{name}.key").write_bytes(key)
    ring = directory / f"{name}.json"
    ring.write_text(json.dumps({actor: f"{name}.key"}))
    return ring


def approve_when_published(inbox: Path, hold: str, decision: str, key_file: Path,
                           extra=()) -> None:
    wait_for((inbox / "holds" / f"{hold}.json").exists, f"the published hold {hold}")
    assert main(["mcp-approve", str(inbox), hold, decision, "--as", "OPERATOR",
                 "--key", str(key_file), *extra]) == 0


def replay_equal(cadl: Path, trace: Path, audit: Path) -> list[tuple]:
    wait_for(lambda: audit.exists() and verify(audit)["complete"], "the end of the log")
    replayed = audit.parent / "replayed.jsonl"
    assert main(["agents-run", str(cadl), "--trace", str(trace), "--audit",
                 str(replayed)]) == 0
    assert replayed.read_bytes() == audit.read_bytes()
    return [(r["message"]["name"], r["decision"], r["reason"])
            for r in read_records(audit) if "decision" in r]


def require(dist: str, module: str, major: int):
    """Skip unless the distribution `dist` of at least version `major` is
    installed (the modules have no __version__)."""
    from importlib import metadata
    try:
        version = metadata.version(dist)
    except metadata.PackageNotFoundError:
        pytest.skip(f"{dist} is not installed")
    if int(version.split(".")[0]) < major:
        pytest.skip(f"{dist} {version} is older than {major}")
    return pytest.importorskip(module)


# === MCP =========================================================================

@pytest.fixture
def mcp_sdk():
    require("mcp", "mcp", 2)
    anyio = pytest.importorskip("anyio")
    return anyio


def _mcp_params(tmp_path: Path, agent: str, extra=()):
    from mcp.client.stdio import StdioServerParameters
    ring = keyring(tmp_path, "OPERATOR", APPROVER_KEY, "approver")
    args = ["-c", BOOT, "mcp-gateway", str(EXAMPLE), "--agent", agent, "--server-actor",
            "FILES", "--audit", str(tmp_path / "audit.jsonl"), "--approvals",
            str(tmp_path / "inbox"), "--approver-keys", str(ring), "--poll", "0.02",
            *extra, "--", sys.executable, str(Path(__file__).parent / "sdk_mcp_server.py"),
            str(tmp_path / "server.log")]
    return StdioServerParameters(command=sys.executable, args=args)


def _ran(tmp_path: Path) -> list[str]:
    log = tmp_path / "server.log"
    return log.read_text().split() if log.exists() else []


@pytest.mark.parametrize("client", ["session", "client"])
def test_mcp_sdk_allow_deny_hold(tmp_path, mcp_sdk, client):
    """`session`: ClientSession with `initialize` (revision 2025-11-25);
    `client`: the high-level Client, which negotiates 2026-07-28 with
    `server/discover`."""
    anyio = mcp_sdk
    from mcp import Client
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client
    params = _mcp_params(tmp_path, "PLANNER")
    inbox = tmp_path / "inbox"
    results: dict = {}

    async def scenario(call):
        results["tools"] = sorted(t.name for t in (await call.list_tools()).tools)
        results["format"] = await call.call_tool("format_disk", {})
        results["read"] = await call.call_tool("read_file", {"path": "/r"})
        async with anyio.create_task_group() as tg:
            async def held():
                results["delete1"] = await call.call_tool("delete_file", {"path": "/a"})
            tg.start_soon(held)
            await anyio.to_thread.run_sync(approve_when_published, inbox, "h-1", "approve",
                                           tmp_path / "approver.key")
        async with anyio.create_task_group() as tg:
            async def held2():
                results["delete2"] = await call.call_tool("delete_file", {"path": "/b"})
            tg.start_soon(held2)
            await anyio.to_thread.run_sync(approve_when_published, inbox, "h-2", "reject",
                                           tmp_path / "approver.key")

    async def run():
        if client == "session":
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    results["protocol"] = (await s.initialize()).protocol_version
                    await scenario(s)
        else:
            async with Client(params) as c:
                results["protocol"] = c.session.protocol_version
                await scenario(c)

    anyio.run(run)
    assert results["protocol"] == ("2025-11-25" if client == "session" else "2026-07-28")
    assert results["tools"] == ["delete_file", "read_file"]           # filtered
    assert results["format"].is_error and "undeclared_tool" in results["format"].content[0].text
    assert results["read"].is_error and "reason: default" in results["read"].content[0].text
    assert not results["delete1"].is_error
    assert results["delete1"].content[0].text == "deleted /a"
    assert results["delete2"].is_error and "rejected" in results["delete2"].content[0].text
    assert _ran(tmp_path) == ["delete_file"]
    assert replay_equal(EXAMPLE, tmp_path / "audit.trace.jsonl", tmp_path / "audit.jsonl") == [
        ("format_disk", "deny", "undeclared_tool"), ("read_file", "deny", "default"),
        ("delete_file", "escalate", "prohibition"), ("approve", "allow", "default"),
        ("delete_file", "allow", "approved"), ("delete_file", "escalate", "prohibition"),
        ("reject", "allow", "default"), ("delete_file", "deny", "rejected")]


def test_mcp_sdk_allowed_call(tmp_path, mcp_sdk):
    anyio = mcp_sdk
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client
    params = _mcp_params(tmp_path, "OPERATOR")
    out: dict = {}

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                out["read"] = await s.call_tool("read_file", {"path": "/x"})

    anyio.run(run)
    assert not out["read"].is_error and out["read"].content[0].text == "read /x"
    assert _ran(tmp_path) == ["read_file"]
    assert replay_equal(EXAMPLE, tmp_path / "audit.trace.jsonl", tmp_path / "audit.jsonl") == [
        ("read_file", "allow", "permission")]
    assert not [r for r in read_records(tmp_path / "audit.jsonl")
                if r.get("event") in ("method_refused", "unsolicited_response")]


# === A2A =========================================================================

@pytest.fixture
def a2a_sdk():
    require("a2a-sdk", "a2a", 1)
    pytest.importorskip("uvicorn")
    pytest.importorskip("httpx")
    return True


class A2AGateway:
    """The agent of `sdk_a2a_agent.py` behind a `cadl a2a-gateway`
    subprocess, and an `a2a-sdk` client whose httpx client adds the
    CADL-HMAC header."""

    def __init__(self, tmp_path: Path, extra=()):
        from google.protobuf import json_format

        from cadl.a2a_gateway import EXTENSION_URI
        from tests.sdk_a2a_agent import Agent, agent_card, free_port
        self.dir = tmp_path
        self.ext = EXTENSION_URI
        self.agent = Agent(EXTENSION_URI)
        port = free_port()
        self.url = f"http://127.0.0.1:{port}/"
        card = tmp_path / "card.json"
        card.write_text(json_format.MessageToJson(agent_card(self.url, EXTENSION_URI)))
        clients = keyring(tmp_path, "PLANNER", CLIENT_KEY, "client")
        approvers = keyring(tmp_path, "OPERATOR", APPROVER_KEY, "approver")
        self.audit = tmp_path / "audit.jsonl"
        self.trace = tmp_path / "audit.trace.jsonl"
        self.inbox = tmp_path / "inbox"
        self.proc = subprocess.Popen(
            [sys.executable, "-c", BOOT, "a2a-gateway", str(A2A_FILE), "--agent-actor",
             "SUMMARIZER", "--upstream", self.agent.url, "--client-keys", str(clients),
             "--audit", str(self.audit), "--approvals", str(self.inbox), "--approver-keys",
             str(approvers), "--listen", f"127.0.0.1:{port}", "--port-file",
             str(tmp_path / "port"), "--agent-card", str(card), "--poll", "0.02", *extra],
            stderr=subprocess.PIPE)
        wait_for((tmp_path / "port").exists, "the port file")
        wait_for((self.inbox / "run.json").exists, "the published run.json")
        self.run_id = json.loads((self.inbox / "run.json").read_text())["run_id"]

    def httpx_client(self):
        import httpx

        from cadl.a2a_gateway import authorization

        async def sign(request):
            if request.method == "POST":
                body = request.read()
                request.headers["Authorization"] = authorization(
                    CLIENT_KEY, "PLANNER", self.run_id, body, path=request.url.path)
        return httpx.AsyncClient(event_hooks={"request": [sign]}, timeout=60)

    def message(self, name: str | None, text: str = "hello", **fields):
        from a2a.types import Message, Part, Role
        msg = Message(role=Role.ROLE_USER, message_id=f"m-{time.monotonic_ns()}",
                      parts=[Part(text=text)], **fields)
        if name is not None:
            msg.extensions.append(self.ext)
            msg.metadata.update({self.ext: {"name": name}})
        return msg

    def stop(self) -> str:
        self.proc.send_signal(signal.SIGTERM)
        assert self.proc.wait(timeout=30) == 0
        self.agent.stop()
        return self.proc.stderr.read().decode()


def _dict(ev):
    from google.protobuf import json_format
    return json_format.MessageToDict(ev)


def test_a2a_sdk_allow_deny_hold(tmp_path, a2a_sdk):
    import asyncio

    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import (
        CancelTaskRequest,
        GetTaskRequest,
        SendMessageConfiguration,
        SendMessageRequest,
    )
    gw = A2AGateway(tmp_path)
    out: dict = {}

    async def send(client, msg, **conf):
        req = SendMessageRequest(message=msg)
        if conf:
            req.configuration.CopyFrom(SendMessageConfiguration(**conf))
        return [_dict(ev) async for ev in client.send_message(req)]

    async def run():
        async with gw.httpx_client() as hc:
            client = await ClientFactory(ClientConfig(httpx_client=hc, streaming=False)) \
                .create_from_url(gw.url)
            out["allow"] = await send(client, gw.message("summarize", "one"))
            out["deny"] = await send(client, gw.message("translate"))
            out["noname"] = await send(client, gw.message(None))
            held = asyncio.create_task(send(client, gw.message("publish", "two")))
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-1", "approve",
                                    tmp_path / "approver.key")
            out["approved"] = await held
            held = asyncio.create_task(send(client, gw.message("publish", "three")))
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-2", "reject",
                                    tmp_path / "approver.key")
            out["rejected"] = await held
            sub = await send(client, gw.message("publish", "four"), return_immediately=True)
            out["submitted"] = sub
            gw_id = sub[0]["task"]["id"]
            out["get"] = _dict(await client.get_task(GetTaskRequest(id=gw_id)))
            out["cancel"] = _dict(await client.cancel_task(CancelTaskRequest(id=gw_id)))
            up_id = out["allow"][0]["task"]["id"]
            out["get_up"] = _dict(await client.get_task(GetTaskRequest(id=up_id)))

    asyncio.run(run())
    err = gw.stop()
    assert out["allow"][0]["task"]["status"]["state"] == "TASK_STATE_COMPLETED", err
    assert out["deny"][0]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert out["noname"][0]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert out["approved"][0]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert out["rejected"][0]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert "rejected" in out["rejected"][0]["task"]["status"]["message"]["parts"][0]["text"]
    assert out["submitted"][0]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert out["get"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert out["cancel"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert out["get_up"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert gw.agent.executor.seen == ["one", "two"]
    events = sorted({r["event"] for r in read_records(gw.audit) if r.get("event")} -
                    {"instance_start", "instance_end"})
    assert events == ["approval_evidence", "task_cancel", "unauthorised_message"], events
    assert replay_equal(A2A_FILE, gw.trace, gw.audit) == [
        ("summarize", "allow", "permission"), ("translate", "deny", "default"),
        ("publish", "escalate", "prohibition"), ("approve", "allow", "permission"),
        ("publish", "allow", "approved"), ("publish", "escalate", "prohibition"),
        ("reject", "allow", "permission"), ("publish", "deny", "rejected"),
        ("publish", "escalate", "prohibition"), ("publish", "deny", "withdrawn")]
