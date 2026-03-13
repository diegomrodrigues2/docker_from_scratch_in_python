"""Immutable domain events emitted by the RunSpec contract context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ._validation import (
    require_aware_datetime,
    require_bytes,
    require_int,
    require_non_empty_string,
    require_non_negative_int,
    require_non_negative_number,
    require_positive_int,
    require_uuid,
)
from .exceptions import ValidationError
from .value_objects import MIN_MEMORY_BYTES


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base event metadata shared by all immutable domain events."""

    event_id: UUID
    occurred_at: datetime
    correlation_id: UUID

    def __post_init__(self) -> None:
        self._validate_metadata()

    def _validate_metadata(self) -> None:
        require_uuid("DomainEvent.event_id", self.event_id)
        require_aware_datetime("DomainEvent.occurred_at", self.occurred_at)
        require_uuid("DomainEvent.correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class RunSpecCompiled(DomainEvent):
    """Emitted after successful RunSpec compilation."""

    runspec_hash: bytes

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        require_bytes("RunSpecCompiled.runspec_hash", self.runspec_hash)


@dataclass(frozen=True, slots=True)
class ContainerStarted(DomainEvent):
    """Emitted after bootstrap succeeds and the container starts running."""

    container_id: UUID
    runspec_hash: bytes
    pidfd: int

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        require_uuid("ContainerStarted.container_id", self.container_id)
        require_bytes("ContainerStarted.runspec_hash", self.runspec_hash)
        require_non_negative_int("ContainerStarted.pidfd", self.pidfd)


@dataclass(frozen=True, slots=True)
class ContainerExited(DomainEvent):
    """Emitted when a container process exits for any reason."""

    container_id: UUID
    exit_code: int
    signal: int | None
    duration_seconds: float

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        require_uuid("ContainerExited.container_id", self.container_id)
        require_non_negative_int("ContainerExited.exit_code", self.exit_code)
        if self.signal is not None:
            require_positive_int("ContainerExited.signal", self.signal)

        # Duration is normalized to float even when the caller provides an int so
        # telemetry, serialization, and tests observe one canonical representation.
        object.__setattr__(
            self,
            "duration_seconds",
            require_non_negative_number("ContainerExited.duration_seconds", self.duration_seconds),
        )


@dataclass(frozen=True, slots=True)
class ContainerOOMKilled(DomainEvent):
    """Emitted when the kernel OOM killer terminates the container."""

    container_id: UUID
    memory_limit: int

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        require_uuid("ContainerOOMKilled.container_id", self.container_id)
        memory_limit = require_int("ContainerOOMKilled.memory_limit", self.memory_limit)
        if memory_limit < MIN_MEMORY_BYTES:
            raise ValidationError(
                "ContainerOOMKilled.memory_limit",
                f"memory_limit must be at least {MIN_MEMORY_BYTES} bytes (4 MiB)",
            )


@dataclass(frozen=True, slots=True)
class ContainerForceKilled(DomainEvent):
    """Emitted when forced termination escalates to cgroup.kill."""

    container_id: UUID
    reason: str

    def __post_init__(self) -> None:
        DomainEvent.__post_init__(self)
        require_uuid("ContainerForceKilled.container_id", self.container_id)
        require_non_empty_string("ContainerForceKilled.reason", self.reason)


__all__ = [
    "ContainerExited",
    "ContainerForceKilled",
    "ContainerOOMKilled",
    "ContainerStarted",
    "DomainEvent",
    "RunSpecCompiled",
]
