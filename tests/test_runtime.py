"""Tests for CADL runtime support — EventLoop and base classes."""

from __future__ import annotations

import asyncio

import pytest

from cadl.codegen.runtime_support import (
    ActorBase,
    ContractMonitorBase,
    Event,
    EventLoop,
    Message,
    ProtocolExecutorBase,
    ProtocolState,
    RuntimeContext,
)


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------

class DummyProtocol(ProtocolExecutorBase):
    PROTOCOL_ID = "test_proto"
    TRIGGER = "test_event"

    def __init__(self):
        super().__init__()
        self.executed = False
        self.received_event = None

    async def _run_steps(self, ctx, trigger_event=None):
        self.executed = True
        self.received_event = trigger_event
        ctx.record_log("DummyProtocol executed")


class DummyMonitor(ContractMonitorBase):
    CONTRACT_ID = "test_contract"

    def check_assumptions(self, ctx):
        return []

    def check_guarantees(self, ctx):
        return []


class DummyRuntime:
    """Minimal runtime mock for EventLoop tests."""

    def __init__(self):
        self.ctx = RuntimeContext()
        self._protocols = []
        self._monitors = []

    def add_protocol(self, proto):
        self._protocols.append(proto)

    def add_monitor(self, monitor):
        self._monitors.append(monitor)

    def get_protocols(self):
        return self._protocols

    def run_monitor_cycle(self):
        all_violations = []
        for m in self._monitors:
            all_violations.extend(m.run_checks(self.ctx))
        return all_violations


# ---------------------------------------------------------------------------
# EventLoop tests
# ---------------------------------------------------------------------------

class TestEventLoop:
    def test_post_and_process_event(self):
        runtime = DummyRuntime()
        proto = DummyProtocol()
        runtime.add_protocol(proto)

        loop = EventLoop(runtime)
        loop.post_event(Event(name="test_event", source="test"))

        asyncio.get_event_loop().run_until_complete(loop.run(max_cycles=1))

        assert proto.executed
        assert proto.received_event is not None
        assert proto.received_event.name == "test_event"

    def test_non_matching_event_ignored(self):
        runtime = DummyRuntime()
        proto = DummyProtocol()
        runtime.add_protocol(proto)

        loop = EventLoop(runtime)
        loop.post_event(Event(name="other_event", source="test"))

        asyncio.get_event_loop().run_until_complete(loop.run(max_cycles=1))

        assert not proto.executed

    def test_stop(self):
        runtime = DummyRuntime()
        loop = EventLoop(runtime)

        async def run_and_stop():
            async def stopper():
                await asyncio.sleep(0.05)
                loop.stop()

            asyncio.ensure_future(stopper())
            await loop.run()

        asyncio.get_event_loop().run_until_complete(run_and_stop())
        assert not loop._running

    def test_max_cycles(self):
        runtime = DummyRuntime()
        loop = EventLoop(runtime)

        asyncio.get_event_loop().run_until_complete(loop.run(max_cycles=3))
        # Should complete without hanging

    def test_monitor_cycle_runs(self):
        runtime = DummyRuntime()
        monitor = DummyMonitor()
        runtime.add_monitor(monitor)

        loop = EventLoop(runtime)
        asyncio.get_event_loop().run_until_complete(loop.run(max_cycles=1))
        # No violations from DummyMonitor, but cycle ran without error

    def test_multiple_events_single_cycle(self):
        runtime = DummyRuntime()
        proto = DummyProtocol()
        runtime.add_protocol(proto)

        loop = EventLoop(runtime)
        loop.post_event(Event(name="test_event", source="a"))
        loop.post_event(Event(name="test_event", source="b"))

        asyncio.get_event_loop().run_until_complete(loop.run(max_cycles=1))

        assert proto.executed
        # Protocol executed for both events; last event captured
        assert proto.received_event.source == "b"


# ---------------------------------------------------------------------------
# RuntimeContext tests
# ---------------------------------------------------------------------------

class TestRuntimeContext:
    def test_register_and_get_actor(self):
        ctx = RuntimeContext()
        actor = ActorBase("a1", "role1")
        ctx.register_actor(actor)

        assert ctx.get_actor("a1") is actor

    def test_get_actors_by_role(self):
        ctx = RuntimeContext()
        a1 = ActorBase("a1", "worker")
        a2 = ActorBase("a2", "manager")
        a3 = ActorBase("a3", "worker")
        ctx.register_actor(a1)
        ctx.register_actor(a2)
        ctx.register_actor(a3)

        workers = ctx.get_actors_by_role("worker")
        assert len(workers) == 2
        assert a1 in workers and a3 in workers

    def test_send_message_delivers(self):
        ctx = RuntimeContext()
        receiver = ActorBase("r1", "role")
        ctx.register_actor(receiver)

        msg = Message(sender="s1", receiver="r1", content="hello")
        ctx.send_message(msg)

        assert len(ctx.messages) == 1
        inbox = receiver.get_messages()
        assert len(inbox) == 1
        assert inbox[0].content == "hello"


# ---------------------------------------------------------------------------
# Generated runtime create_event_loop test
# ---------------------------------------------------------------------------

class TestGeneratedEventLoop:
    def test_codegen_includes_create_event_loop(self):
        from cadl.parser import parse
        from cadl.codegen.runtime_gen import generate_runtime_module

        cadl = """\
sos:
  name: "TestSoS"
  type: Directed
  version: "1.0.0"
  actors:
    - id: AGENT
      role: "worker"
      autonomy: low
  contracts:
    - id: C1
      parties:
        - AGENT
      assume:
        - "true"
      guarantee:
        - "true"
      authority:
        decision_holder: AGENT
        beta: 0.5
      information:
        alpha: 0.5
      duration: indefinite
"""
        sos = parse(cadl)
        code = generate_runtime_module(sos)
        assert "def create_event_loop(self):" in code
        assert "EventLoop" in code
