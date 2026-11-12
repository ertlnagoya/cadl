"""An A2A agent built with the official `a2a-sdk` (JSON-RPC binding at /
and HTTP+JSON binding at its routes, Starlette and uvicorn), for the interoperability tests of
`cadl a2a-gateway` (tests/test_sdk_interop.py). It runs in a thread of the
test.

The executor records the text of every message it runs, creates a task,
reports it working, adds an artifact, and completes it. A message whose
text is "wait" keeps the task working until it is cancelled or until
`release` is set, so that streams and cancellations can be observed.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import uvicorn
from a2a.helpers.proto_helpers import get_message_text, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    TaskState,
)
from starlette.applications import Starlette


class Recorder(AgentExecutor):
    def __init__(self) -> None:
        self.seen: list[str] = []
        self.release = threading.Event()

    async def execute(self, context, queue) -> None:
        text = get_message_text(context.message)
        self.seen.append(text)
        if context.current_task is None:
            await queue.enqueue_event(new_task(context.task_id, context.context_id,
                                               TaskState.TASK_STATE_SUBMITTED,
                                               history=[context.message]))
        up = TaskUpdater(queue, context.task_id, context.context_id)
        await up.start_work()
        await up.add_artifact([new_text_part(f"artifact: {text}")])
        if text == "wait":
            while not self.release.is_set():
                await asyncio.sleep(0.05)
        await up.complete(up.new_agent_message([new_text_part(f"done: {text}")]))

    async def cancel(self, context, queue) -> None:
        up = TaskUpdater(queue, context.task_id, context.context_id)
        await up.cancel()


def agent_card(url: str, extension_uri: str, streaming: bool = True,
               binding: str = "JSONRPC", push: bool = False) -> AgentCard:
    return AgentCard(
        name="summarizer", description="CADL interoperability test agent", version="1",
        supported_interfaces=[AgentInterface(url=url, protocol_binding=binding,
                                             protocol_version="1.0")],
        capabilities=AgentCapabilities(
            streaming=streaming, push_notifications=push,
            extensions=[AgentExtension(uri=extension_uri, required=True)]),
        default_input_modes=["text"], default_output_modes=["text"],
        skills=[AgentSkill(id="summarize", name="summarize", description="summarize",
                           tags=["text"])])


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Agent:
    """The agent on a free port of 127.0.0.1."""

    def __init__(self, extension_uri: str) -> None:
        port = free_port()
        self.url = f"http://127.0.0.1:{port}/"
        self.executor = Recorder()
        card = agent_card(self.url, extension_uri)
        handler = DefaultRequestHandler(self.executor, InMemoryTaskStore(), card)
        # The JSON-RPC binding at / and the HTTP+JSON binding at its routes.
        app = Starlette(routes=create_jsonrpc_routes(handler, "/")
                        + create_agent_card_routes(card) + create_rest_routes(handler))
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                    log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        end = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > end:
                raise RuntimeError("the A2A agent did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.executor.release.set()
        self.server.should_exit = True
