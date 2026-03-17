"""CADL Runtime Support — base classes for generated code.

Generated CADL code imports these base classes to provide
runtime monitoring, protocol execution, and actor management.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set


logger = logging.getLogger("cadl.runtime")


# === Enums ===

class AutonomyLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProtocolState(Enum):
    IDLE = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    TIMED_OUT = auto()


# === Data types ===

@dataclass
class Message:
    """A message passed between actors."""
    sender: str
    receiver: str
    content: str
    payload: Any = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Event:
    """An event that can trigger protocols."""
    name: str
    source: str = ""
    data: Any = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class CheckResult:
    """Result of a single assume/guarantee check."""
    predicate: str
    satisfied: bool
    actual_value: Any = None

    def __str__(self) -> str:
        icon = "OK" if self.satisfied else "VIOLATED"
        return f"[{icon}] {self.predicate}"


@dataclass
class Violation:
    """A contract violation."""
    contract_id: str
    predicate: str
    violation_type: str  # "assume" or "guarantee"
    actual_value: Any = None
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self) -> str:
        return f"Violation({self.contract_id}): {self.violation_type} — {self.predicate}"


# === Runtime Context ===

class RuntimeContext:
    """Shared runtime context for contract monitors and protocol executors."""

    def __init__(self) -> None:
        self.actors: Dict[str, ActorBase] = {}
        self.state: Dict[str, Any] = {}
        self.messages: List[Message] = []
        self.violations: List[Violation] = []
        self.log: List[str] = []

    def register_actor(self, actor: ActorBase) -> None:
        self.actors[actor.actor_id] = actor

    def get_actor(self, actor_id: str) -> ActorBase:
        return self.actors[actor_id]

    def get_actors_by_role(self, role: str) -> List[ActorBase]:
        return [a for a in self.actors.values() if a.role == role]

    def send_message(self, msg: Message) -> None:
        self.messages.append(msg)
        logger.info("Message: %s -> %s : %s", msg.sender, msg.receiver, msg.content)
        receiver = self.actors.get(msg.receiver)
        if receiver:
            receiver.receive_message(msg)

    def record_violation(self, violation: Violation) -> None:
        self.violations.append(violation)
        logger.warning("Violation: %s", violation)

    def record_log(self, entry: str) -> None:
        self.log.append(f"[{datetime.now().isoformat()}] {entry}")


# === Actor Base ===

class ActorBase:
    """Base class for generated actor classes."""

    ROLE: str = ""
    AUTONOMY: AutonomyLevel = AutonomyLevel.MEDIUM

    def __init__(self, actor_id: str, role: str = "") -> None:
        self.actor_id = actor_id
        self.role = role or self.ROLE
        self.state: Dict[str, Any] = {}
        self._inbox: List[Message] = []

    def receive_message(self, msg: Message) -> None:
        self._inbox.append(msg)

    def get_messages(self, clear: bool = True) -> List[Message]:
        msgs = list(self._inbox)
        if clear:
            self._inbox.clear()
        return msgs

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.actor_id!r}, role={self.role!r})"


# === Contract Monitor Base ===

class ContractMonitorBase:
    """Base class for generated contract monitors."""

    CONTRACT_ID: str = ""
    PARTIES: List[str] = []

    def __init__(self) -> None:
        self.violation_count: int = 0
        self.check_history: List[CheckResult] = []

    def _check(self, predicate: str, value: bool) -> CheckResult:
        result = CheckResult(predicate=predicate, satisfied=bool(value))
        self.check_history.append(result)
        return result

    def check_assumptions(self, ctx: RuntimeContext) -> List[CheckResult]:
        raise NotImplementedError

    def check_guarantees(self, ctx: RuntimeContext) -> List[CheckResult]:
        raise NotImplementedError

    def run_checks(self, ctx: RuntimeContext) -> List[Violation]:
        """Run all checks and return any violations."""
        violations = []

        for result in self.check_assumptions(ctx):
            if not result.satisfied:
                v = Violation(
                    contract_id=self.CONTRACT_ID,
                    predicate=result.predicate,
                    violation_type="assume",
                )
                violations.append(v)

        for result in self.check_guarantees(ctx):
            if not result.satisfied:
                v = Violation(
                    contract_id=self.CONTRACT_ID,
                    predicate=result.predicate,
                    violation_type="guarantee",
                )
                violations.append(v)
                self.violation_count += 1

        return violations

    def on_violation(self, violation: Violation, ctx: RuntimeContext) -> None:
        """Handle a violation. Override in generated code."""
        ctx.record_violation(violation)


# === Protocol Executor Base ===

class ProtocolExecutorBase:
    """Base class for generated protocol executors."""

    PROTOCOL_ID: str = ""
    TRIGGER: str = ""

    def __init__(self) -> None:
        self.state: ProtocolState = ProtocolState.IDLE
        self.current_step: int = 0
        self.start_time: Optional[datetime] = None
        self.timing: Dict[str, timedelta] = {}
        self.fallback: Dict[str, str] = {}

    def matches_trigger(self, event: Event) -> bool:
        """Check if an event matches this protocol's trigger."""
        return event.name == self.TRIGGER

    async def execute(self, ctx: RuntimeContext, trigger_event: Event = None) -> bool:
        """Execute the protocol. Returns True if completed successfully."""
        self.state = ProtocolState.RUNNING
        self.start_time = datetime.now()
        self.current_step = 0

        ctx.record_log(f"Protocol {self.PROTOCOL_ID} started")

        try:
            await self._run_steps(ctx, trigger_event)
            self.state = ProtocolState.COMPLETED
            ctx.record_log(f"Protocol {self.PROTOCOL_ID} completed")
            return True
        except asyncio.TimeoutError:
            self.state = ProtocolState.TIMED_OUT
            ctx.record_log(f"Protocol {self.PROTOCOL_ID} timed out")
            await self._handle_timeout(ctx)
            return False
        except Exception as e:
            self.state = ProtocolState.FAILED
            ctx.record_log(f"Protocol {self.PROTOCOL_ID} failed: {e}")
            await self._handle_failure(ctx)
            return False

    async def _run_steps(self, ctx: RuntimeContext, trigger_event: Event = None) -> None:
        """Override with generated step execution logic."""
        raise NotImplementedError

    async def _handle_timeout(self, ctx: RuntimeContext) -> None:
        """Handle timeout using fallback configuration."""
        action = self.fallback.get("on_timeout")
        if action:
            ctx.record_log(f"Protocol {self.PROTOCOL_ID} timeout fallback: {action}")

    async def _handle_failure(self, ctx: RuntimeContext) -> None:
        """Handle failure using fallback configuration."""
        action = self.fallback.get("on_failure")
        if action:
            ctx.record_log(f"Protocol {self.PROTOCOL_ID} failure fallback: {action}")

    def _check_max_total(self) -> None:
        """Check if the protocol has exceeded its max_total timing."""
        max_total = self.timing.get("max_total")
        if max_total and self.start_time:
            elapsed = datetime.now() - self.start_time
            if elapsed > max_total:
                raise asyncio.TimeoutError(f"Protocol exceeded max_total: {max_total}")


# === Metrics Collector Base ===

class MetricsCollectorBase:
    """Base class for generated metrics collectors."""

    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}
        self.history: Dict[str, List[Any]] = {}

    def record(self, metric_id: str, value: Any) -> None:
        self.values[metric_id] = value
        self.history.setdefault(metric_id, []).append(value)

    def get(self, metric_id: str) -> Any:
        return self.values.get(metric_id)

    def check_target(self, metric_id: str) -> Optional[bool]:
        """Override with generated target checking logic."""
        return None
