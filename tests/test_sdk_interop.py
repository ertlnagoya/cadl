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

    def __init__(self, tmp_path: Path, extra=(), pki=None, cert: str | None = "planner",
                 binding: str = "JSONRPC", push: bool = False, grpc: bool = False):
        from google.protobuf import json_format

        from cadl.a2a_gateway import EXTENSION_URI
        from tests.sdk_a2a_agent import Agent, agent_card, free_port
        self.dir = tmp_path
        self.ext = EXTENSION_URI
        self.agent = Agent(EXTENSION_URI, push=push)
        port = free_port()
        self.pki, self.cert = pki, cert
        self.url = f"{'https' if pki else 'http'}://127.0.0.1:{port}/"
        card = tmp_path / "card.json"
        if grpc:
            # The card names the gRPC listener of the gateway (A2A 1.0.0, 8.3.1).
            self.grpc_port = free_port()
            extra = (*extra, "--grpc-listen", f"127.0.0.1:{self.grpc_port}")
            card.write_text(json_format.MessageToJson(agent_card(
                f"127.0.0.1:{self.grpc_port}", EXTENSION_URI, binding="GRPC", push=push)))
        else:
            card.write_text(json_format.MessageToJson(agent_card(self.url, EXTENSION_URI,
                                                                 binding=binding, push=push)))
        if pki is not None:
            extra = (*pki.gateway_args(), *extra)
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
            # Every request but the agent card, with its method and its
            # request target (path and query).
            if request.url.path != "/.well-known/agent-card.json":
                body = request.read()
                request.headers["Authorization"] = authorization(
                    CLIENT_KEY, "PLANNER", self.run_id, body,
                    path=request.url.raw_path.decode("ascii"), method=request.method)
        verify = self.pki.context(self.cert) if self.pki is not None else True
        return httpx.AsyncClient(event_hooks={"request": [sign]}, timeout=60, verify=verify)

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


def test_a2a_sdk_streaming(tmp_path, a2a_sdk):
    """SendStreamingMessage and SubscribeToTask with the streaming client of
    a2a-sdk: an allowed stream passes the agent's events, a denied one is a
    single rejected task, a held one waits and then passes the events of the
    released request, and a subscription follows a task of the client."""
    import asyncio

    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import SendMessageConfiguration, SendMessageRequest, SubscribeToTaskRequest
    gw = A2AGateway(tmp_path)
    out: dict = {}

    async def stream(client, msg, **conf):
        req = SendMessageRequest(message=msg)
        if conf:
            req.configuration.CopyFrom(SendMessageConfiguration(**conf))
        return [_dict(ev) async for ev in client.send_message(req)]

    async def run():
        async with gw.httpx_client() as hc:
            client = await ClientFactory(ClientConfig(httpx_client=hc, streaming=True)) \
                .create_from_url(gw.url)
            out["allow"] = await stream(client, gw.message("summarize", "one"))
            out["deny"] = await stream(client, gw.message("translate"))
            held = asyncio.create_task(stream(client, gw.message("publish", "two")))
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-1", "approve",
                                    tmp_path / "approver.key")
            out["approved"] = await held
            held = asyncio.create_task(stream(client, gw.message("publish", "three")))
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-2", "reject",
                                    tmp_path / "approver.key")
            out["rejected"] = await held
            sub = await stream(client, gw.message("publish", "wait"), return_immediately=True)
            out["submitted"] = sub
            gw_id = sub[0]["task"]["id"]

            async def follow():
                events = []
                async for ev in client.subscribe(SubscribeToTaskRequest(id=gw_id)):
                    events.append(_dict(ev))
                    gw.agent.executor.release.set()         # then the task completes
                return events
            following = asyncio.create_task(follow())      # waits while the task is held
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-3", "approve",
                                    tmp_path / "approver.key")
            out["subscribed"] = await following

    asyncio.run(run())
    err = gw.stop()
    kinds = [next(iter(ev)) for ev in out["allow"]]
    assert kinds[0] == "task" and kinds[-1] == "statusUpdate", (kinds, err)
    assert "artifactUpdate" in kinds
    assert out["allow"][-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert [next(iter(ev)) for ev in out["deny"]] == ["task"]
    assert out["deny"][0]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert out["approved"][-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert [ev["task"]["status"]["state"] for ev in out["rejected"]] == ["TASK_STATE_REJECTED"]
    assert out["submitted"][0]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    sub_ids = {ev.get("task", {}).get("id") or ev.get("statusUpdate", ev.get("artifactUpdate",
                                                                               {})).get("taskId")
               for ev in out["subscribed"]}
    assert sub_ids == {out["submitted"][0]["task"]["id"]}        # the gateway's task id
    assert gw.agent.executor.seen == ["one", "two", "wait"]
    assert replay_equal(A2A_FILE, gw.trace, gw.audit) == [
        ("summarize", "allow", "permission"), ("translate", "deny", "default"),
        ("publish", "escalate", "prohibition"), ("approve", "allow", "permission"),
        ("publish", "allow", "approved"), ("publish", "escalate", "prohibition"),
        ("reject", "allow", "permission"), ("publish", "deny", "rejected"),
        ("publish", "escalate", "prohibition"), ("approve", "allow", "permission"),
        ("publish", "allow", "approved")]


def test_a2a_sdk_mutual_tls(tmp_path, a2a_sdk):
    """The `a2a-sdk` client over HTTPS with a client certificate (mutual
    TLS, --client-ca): allowed and denied messages pass as without it; a
    client without a certificate gets no answer."""
    import asyncio

    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import SendMessageRequest

    from tests.a2a_certs import PKI
    pki = PKI(tmp_path / "pki", clients={"planner": "PLANNER"}, foreign={})
    gw = A2AGateway(tmp_path, pki=pki)
    out: dict = {}

    async def run():
        async with gw.httpx_client() as hc:
            client = await ClientFactory(ClientConfig(httpx_client=hc, streaming=False)) \
                .create_from_url(gw.url)
            out["allow"] = [_dict(ev) async for ev in client.send_message(
                SendMessageRequest(message=gw.message("summarize", "one")))]
            out["deny"] = [_dict(ev) async for ev in client.send_message(
                SendMessageRequest(message=gw.message("translate")))]
        gw.cert = None
        async with gw.httpx_client() as hc:
            try:
                await ClientFactory(ClientConfig(httpx_client=hc)).create_from_url(gw.url)
                out["nocert"] = "answered"
            except Exception as e:      # noqa: BLE001 - the client's error type
                out["nocert"] = type(e).__name__

    asyncio.run(run())
    err = gw.stop()
    assert out["allow"][0]["task"]["status"]["state"] == "TASK_STATE_COMPLETED", err
    assert out["deny"][0]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert out["nocert"] != "answered"
    assert gw.agent.executor.seen == ["one"]
    assert any(r.get("event") == "handshake_failed" for r in read_records(gw.audit))
    assert replay_equal(A2A_FILE, gw.trace, gw.audit) == [
        ("summarize", "allow", "permission"), ("translate", "deny", "default")]


@pytest.mark.parametrize("upstream", ["jsonrpc", "rest"])
def test_a2a_sdk_rest_binding(tmp_path, a2a_sdk, upstream):
    """The `a2a-sdk` client over the HTTP+JSON binding (A2A 1.0.0, 11), with
    and without streaming, in front of the SDK agent spoken to over JSON-RPC
    or over the HTTP+JSON binding (--upstream-binding): allow, deny, holds
    approved and rejected, returnImmediately with GetTask and CancelTask,
    a stream, and a subscription to a held task."""
    import asyncio

    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import (
        CancelTaskRequest,
        GetTaskRequest,
        SendMessageConfiguration,
        SendMessageRequest,
        SubscribeToTaskRequest,
    )
    extra = ("--upstream-binding", "rest") if upstream == "rest" else ()
    gw = A2AGateway(tmp_path, extra=extra, binding="HTTP+JSON")
    out: dict = {}

    async def send(client, msg, **conf):
        req = SendMessageRequest(message=msg)
        if conf:
            req.configuration.CopyFrom(SendMessageConfiguration(**conf))
        return [_dict(ev) async for ev in client.send_message(req)]

    async def run():
        async with gw.httpx_client() as hc:
            factory = ClientFactory(ClientConfig(httpx_client=hc, streaming=False,
                                                 supported_protocol_bindings=["HTTP+JSON"]))
            client = await factory.create_from_url(gw.url)
            out["allow"] = await send(client, gw.message("summarize", "one"))
            out["deny"] = await send(client, gw.message("translate"))
            held = asyncio.create_task(send(client, gw.message("publish", "two")))
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-1", "approve",
                                    tmp_path / "approver.key")
            out["approved"] = await held
            held = asyncio.create_task(send(client, gw.message("publish", "three")))
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-2", "reject",
                                    tmp_path / "approver.key")
            out["rejected"] = await held
            sub = await send(client, gw.message("publish", "four"), return_immediately=True)
            gw_id = sub[0]["task"]["id"]
            out["get"] = _dict(await client.get_task(GetTaskRequest(id=gw_id,
                                                                    history_length=1)))
            out["cancel"] = _dict(await client.cancel_task(CancelTaskRequest(id=gw_id)))
            up_id = out["allow"][0]["task"]["id"]
            out["get_up"] = _dict(await client.get_task(GetTaskRequest(id=up_id)))
            factory = ClientFactory(ClientConfig(httpx_client=hc, streaming=True,
                                                 supported_protocol_bindings=["HTTP+JSON"]))
            streaming = await factory.create_from_url(gw.url)
            out["stream"] = await send(streaming, gw.message("summarize", "five"))
            sub = await send(streaming, gw.message("publish", "wait"), return_immediately=True)
            gw_id = out["sub_id"] = sub[0]["task"]["id"]

            async def follow():
                events = []
                async for ev in streaming.subscribe(SubscribeToTaskRequest(id=gw_id)):
                    events.append(_dict(ev))
                    gw.agent.executor.release.set()
                return events
            following = asyncio.create_task(follow())
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-4", "approve",
                                    tmp_path / "approver.key")
            out["subscribed"] = await following

    asyncio.run(run())
    err = gw.stop()
    assert out["allow"][0]["task"]["status"]["state"] == "TASK_STATE_COMPLETED", err
    assert out["deny"][0]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert out["approved"][0]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert out["rejected"][0]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert out["get"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert out["cancel"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert out["get_up"]["status"]["state"] == "TASK_STATE_COMPLETED"
    kinds = [next(iter(ev)) for ev in out["stream"]]
    assert kinds[0] == "task" and "artifactUpdate" in kinds, kinds
    assert out["stream"][-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    ids = {ev.get("task", {}).get("id") or ev.get("statusUpdate", ev.get("artifactUpdate",
                                                                        {})).get("taskId")
           for ev in out["subscribed"]}
    assert ids == {out["sub_id"]}         # the gateway's task, not the agent's
    assert gw.agent.executor.seen == ["one", "two", "five", "wait"]
    assert replay_equal(A2A_FILE, gw.trace, gw.audit) == [
        ("summarize", "allow", "permission"), ("translate", "deny", "default"),
        ("publish", "escalate", "prohibition"), ("approve", "allow", "permission"),
        ("publish", "allow", "approved"), ("publish", "escalate", "prohibition"),
        ("reject", "allow", "permission"), ("publish", "deny", "rejected"),
        ("publish", "escalate", "prohibition"), ("publish", "deny", "withdrawn"),
        ("summarize", "allow", "permission"),
        ("publish", "escalate", "prohibition"), ("approve", "allow", "permission"),
        ("publish", "allow", "approved")]



@pytest.mark.parametrize("binding", ["JSONRPC", "HTTP+JSON"])
def test_a2a_sdk_push_notifications(tmp_path, a2a_sdk, binding):
    """Push notifications (--push-allow) with the push sender of the SDK
    agent and the `a2a-sdk` client: a configuration given with a SendMessage
    with returnImmediately, and one created for the running task. A
    configuration is bound to the task that the agent's answer names; the
    gateway drops the notifications that come before that answer (the SDK
    agent sends some while it creates the task) and relays the later ones
    to the client's webhook, with the client's token and credentials."""
    import asyncio

    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import (
        AuthenticationInfo,
        ListTaskPushNotificationConfigsRequest,
        SendMessageConfiguration,
        SendMessageRequest,
        TaskPushNotificationConfig,
    )

    from tests.test_a2a_bindings import Hook
    hook = Hook()
    gw = A2AGateway(tmp_path, extra=("--push-allow", f"{hook.url}/hooks/"), binding=binding,
                    push=True)
    out: dict = {}

    def config(path: str, **kw) -> TaskPushNotificationConfig:
        return TaskPushNotificationConfig(
            url=f"{hook.url}/hooks/{path}", token=f"token-{path}",
            authentication=AuthenticationInfo(scheme="Bearer", credentials=f"cred-{path}"),
            **kw)

    def completed(path: str) -> bool:
        return any(g["path"] == path and g["body"].get("statusUpdate", {}).get(
            "status", {}).get("state") == "TASK_STATE_COMPLETED" for g in hook.got)

    async def run():
        async with gw.httpx_client() as hc:
            client = await ClientFactory(ClientConfig(
                httpx_client=hc, streaming=False,
                supported_protocol_bindings=[binding])).create_from_url(gw.url)
            req = SendMessageRequest(message=gw.message("summarize", "wait"))
            req.configuration.CopyFrom(SendMessageConfiguration(
                return_immediately=True, task_push_notification_config=config("a", id="a1")))
            sent = [_dict(ev) async for ev in client.send_message(req)]
            tid = out["tid"] = sent[0]["task"]["id"]
            out["made"] = _dict(await client.create_task_push_notification_config(
                config("b", task_id=tid, id="b1")))
            out["list"] = _dict(await client.list_task_push_notification_configs(
                ListTaskPushNotificationConfigsRequest(task_id=tid)))
            gw.agent.executor.release.set()
            await asyncio.to_thread(wait_for, lambda: completed("/hooks/a")
                                    and completed("/hooks/b"), "the notifications of completion")

    asyncio.run(run())
    err = gw.stop()
    hook.close()
    a = [g for g in hook.got if g["path"] == "/hooks/a"]
    b = [g for g in hook.got if g["path"] == "/hooks/b"]
    assert a and b, (hook.got, err)
    assert all(g["headers"]["X-A2A-Notification-Token"] == "token-a"
               and g["headers"]["Authorization"] == "Bearer cred-a" for g in a)
    assert all(g["headers"]["Authorization"] == "Bearer cred-b" for g in b)
    tasks = {g["body"].get("task", {}).get("id") or next(iter(g["body"].values())).get("taskId")
             for g in hook.got}
    assert tasks == {out["tid"]}
    assert out["made"]["id"] == "b1" and out["made"]["url"].endswith("/hooks/b")
    assert sorted(c["id"] for c in out["list"]["configs"]) == ["a1", "b1"]
    records = read_records(gw.audit)
    events = [r.get("event") for r in records]
    assert events.count("push_config") == 2
    assert events.count("push_sent") == len(hook.got)
    assert "push_failed" not in events
    assert all((r["reason"] or "").startswith("unbound ") for r in records
               if r.get("event") == "push_dropped")
    assert replay_equal(A2A_FILE, gw.trace, gw.audit) == [("summarize", "allow", "permission")]



def test_a2a_sdk_grpc_binding(tmp_path, a2a_sdk):
    """The `a2a-sdk` client over the gRPC binding (A2A 1.0.0, 10) of the
    gateway (--grpc-listen, the extra cadl-lang[grpc]), with an interceptor
    of grpc.aio that adds the CADL-HMAC metadata over the serialized
    request: allow, deny, a hold approved, GetTask, a stream, and a
    subscription to a held task; the agent behind it speaks JSON-RPC."""
    import asyncio

    grpc = pytest.importorskip("grpc")
    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import (
        GetTaskRequest,
        SendMessageConfiguration,
        SendMessageRequest,
        SubscribeToTaskRequest,
    )

    from cadl.a2a_gateway import authorization
    gw = A2AGateway(tmp_path, grpc=True)
    out: dict = {}

    class Sign:
        def _details(self, details, request):
            method = details.method.decode() if isinstance(details.method, bytes) \
                else details.method
            md = grpc.aio.Metadata(*list(details.metadata or ()))
            md.add("authorization", authorization(CLIENT_KEY, "PLANNER", gw.run_id,
                                                  request.SerializeToString(), path=method))
            return grpc.aio.ClientCallDetails(details.method, details.timeout, md,
                                              details.credentials, details.wait_for_ready)

    # grpc.aio takes an interceptor as one kind only, so one of each.
    class SignUnary(Sign, grpc.aio.UnaryUnaryClientInterceptor):
        async def intercept_unary_unary(self, continuation, details, request):
            return await continuation(self._details(details, request), request)

    class SignStream(Sign, grpc.aio.UnaryStreamClientInterceptor):
        async def intercept_unary_stream(self, continuation, details, request):
            return await continuation(self._details(details, request), request)

    async def send(client, msg, **conf):
        req = SendMessageRequest(message=msg)
        if conf:
            req.configuration.CopyFrom(SendMessageConfiguration(**conf))
        return [_dict(ev) async for ev in client.send_message(req)]

    async def run():
        async with gw.httpx_client() as hc:
            def channel(url):
                return grpc.aio.insecure_channel(url, interceptors=[SignUnary(), SignStream()])
            client = await ClientFactory(ClientConfig(
                httpx_client=hc, streaming=False, grpc_channel_factory=channel,
                supported_protocol_bindings=["GRPC"])).create_from_url(gw.url)
            out["allow"] = await send(client, gw.message("summarize", "one"))
            out["deny"] = await send(client, gw.message("translate"))
            held = asyncio.create_task(send(client, gw.message("publish", "two")))
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-1", "approve",
                                    tmp_path / "approver.key")
            out["approved"] = await held
            up_id = out["allow"][0]["task"]["id"]
            out["get"] = _dict(await client.get_task(GetTaskRequest(id=up_id)))
            streaming = await ClientFactory(ClientConfig(
                httpx_client=hc, streaming=True, grpc_channel_factory=channel,
                supported_protocol_bindings=["GRPC"])).create_from_url(gw.url)
            out["stream"] = await send(streaming, gw.message("summarize", "three"))
            sub = await send(streaming, gw.message("publish", "wait"), return_immediately=True)
            gw_id = out["sub_id"] = sub[0]["task"]["id"]

            async def follow():
                events = []
                async for ev in streaming.subscribe(SubscribeToTaskRequest(id=gw_id)):
                    events.append(_dict(ev))
                    gw.agent.executor.release.set()
                return events
            following = asyncio.create_task(follow())
            await asyncio.to_thread(approve_when_published, gw.inbox, "h-2", "approve",
                                    tmp_path / "approver.key")
            out["subscribed"] = await following

    asyncio.run(run())
    err = gw.stop()
    assert out["allow"][0]["task"]["status"]["state"] == "TASK_STATE_COMPLETED", err
    assert out["deny"][0]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert out["approved"][0]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert out["get"]["status"]["state"] == "TASK_STATE_COMPLETED"
    kinds = [next(iter(ev)) for ev in out["stream"]]
    assert kinds[0] == "task" and "artifactUpdate" in kinds, kinds
    ids = {ev.get("task", {}).get("id") or ev.get("statusUpdate", ev.get("artifactUpdate",
                                                                        {})).get("taskId")
           for ev in out["subscribed"]}
    assert ids == {out["sub_id"]}
    assert gw.agent.executor.seen == ["one", "two", "three", "wait"]
    assert replay_equal(A2A_FILE, gw.trace, gw.audit) == [
        ("summarize", "allow", "permission"), ("translate", "deny", "default"),
        ("publish", "escalate", "prohibition"), ("approve", "allow", "permission"),
        ("publish", "allow", "approved"), ("summarize", "allow", "permission"),
        ("publish", "escalate", "prohibition"), ("approve", "allow", "permission"),
        ("publish", "allow", "approved")]
