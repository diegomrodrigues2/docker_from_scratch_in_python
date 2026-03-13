from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from runspec_contract.domain import (
    ContainerExited,
    ContainerForceKilled,
    ContainerOOMKilled,
    ContainerStarted,
    MIN_MEMORY_BYTES,
    RunSpecCompiled,
)


def _base_event_kwargs() -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "occurred_at": datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc),
        "correlation_id": uuid4(),
    }


def test_domain_events_accept_valid_payloads() -> None:
    base_kwargs = _base_event_kwargs()
    container_id = uuid4()

    runspec_compiled = RunSpecCompiled(
        **base_kwargs,
        runspec_hash=b"\xaa\xbb\xcc\xdd\xee\xff",
    )
    container_started = ContainerStarted(
        **base_kwargs,
        container_id=container_id,
        runspec_hash=b"\xaa\xbb\xcc\xdd\xee\xff",
        pidfd=42,
    )
    container_exited = ContainerExited(
        **base_kwargs,
        container_id=container_id,
        exit_code=0,
        signal=None,
        duration_seconds=1.25,
    )
    container_oom_killed = ContainerOOMKilled(
        **base_kwargs,
        container_id=container_id,
        memory_limit=MIN_MEMORY_BYTES,
    )
    container_force_killed = ContainerForceKilled(
        **base_kwargs,
        container_id=container_id,
        reason="termination timeout exceeded",
    )

    assert runspec_compiled.runspec_hash == b"\xaa\xbb\xcc\xdd\xee\xff"
    assert container_started.pidfd == 42
    assert container_exited.duration_seconds == 1.25
    assert container_oom_killed.memory_limit == MIN_MEMORY_BYTES
    assert container_force_killed.reason == "termination timeout exceeded"


@pytest.mark.parametrize(
    ("event_instance", "attribute_name", "replacement_value"),
    [
        (
            RunSpecCompiled(
                **_base_event_kwargs(),
                runspec_hash=b"\xaa\xbb\xcc\xdd\xee\xff",
            ),
            "runspec_hash",
            b"\x00",
        ),
        (
            ContainerStarted(
                **_base_event_kwargs(),
                container_id=uuid4(),
                runspec_hash=b"\xaa\xbb\xcc\xdd\xee\xff",
                pidfd=7,
            ),
            "pidfd",
            9,
        ),
        (
            ContainerExited(
                **_base_event_kwargs(),
                container_id=uuid4(),
                exit_code=0,
                signal=None,
                duration_seconds=0.5,
            ),
            "duration_seconds",
            1.0,
        ),
        (
            ContainerOOMKilled(
                **_base_event_kwargs(),
                container_id=uuid4(),
                memory_limit=MIN_MEMORY_BYTES,
            ),
            "memory_limit",
            MIN_MEMORY_BYTES * 2,
        ),
        (
            ContainerForceKilled(
                **_base_event_kwargs(),
                container_id=uuid4(),
                reason="manual escalation",
            ),
            "reason",
            "other reason",
        ),
    ],
)
def test_domain_events_are_immutable(
    event_instance: object,
    attribute_name: str,
    replacement_value: object,
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(event_instance, attribute_name, replacement_value)
