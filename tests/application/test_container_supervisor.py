from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from runspec_contract.application import (
    ContainerSupervisor,
    InMemoryContainerRegistry,
    PidfdWaitOutcome,
)
from runspec_contract.domain import (
    AbsolutePath,
    ContainerExited,
    ContainerForceKilled,
    ContainerOOMKilled,
    ContainerStarted,
    CpuLimit,
    EnvironmentMap,
    Hostname,
    MemoryLimit,
    MountTopology,
    PidsLimit,
    ProcessSpec,
    ResourceSpec,
    RunSpec,
    SecurityEnvelope,
)
from runspec_contract.infrastructure import ExecutionResult


def _build_runspec() -> RunSpec:
    compiled_at = datetime(2026, 3, 13, 18, 0, tzinfo=timezone.utc)
    rootfs_path = AbsolutePath("/var/lib/containers/demo/rootfs")
    return RunSpec(
        process=ProcessSpec(
            argv=("/usr/bin/python", "-m", "demo"),
            uid=1000,
            gid=1000,
            cwd=AbsolutePath("/var/lib/containers/demo/rootfs/workspace"),
        ),
        environment=EnvironmentMap(entries=(("PATH", "/usr/bin"),)),
        mounts=MountTopology(),
        hostname=Hostname("demo-container"),
        resources=ResourceSpec(
            cpu=CpuLimit(quota_us=50_000, period_us=100_000),
            memory=MemoryLimit(max_bytes=8 * 1024 * 1024),
            pids=PidsLimit(max_pids=64),
        ),
        security=SecurityEnvelope(
            effective_caps=frozenset(),
            permitted_caps=frozenset(),
            bounding_caps=frozenset(),
            no_new_privs=True,
        ),
        rootfs_path=rootfs_path,
        canonical_hash=b"\xaa" * 32,
        compiled_at=compiled_at,
        correlation_id=uuid4(),
    )


class SequenceClock:
    def __init__(self, *timestamps: datetime) -> None:
        self._timestamps = list(timestamps)

    def __call__(self) -> datetime:
        if not self._timestamps:
            raise AssertionError("clock was called more times than expected")
        return self._timestamps.pop(0)


@dataclass
class FakeSerializer:
    serialized_runspec: bytes

    def __post_init__(self) -> None:
        self.calls: list[RunSpec] = []

    def serialize(self, runspec: RunSpec) -> bytes:
        self.calls.append(runspec)
        return self.serialized_runspec


@dataclass
class FakePidfdProcessIdResolver:
    process_id: int

    def __post_init__(self) -> None:
        self.calls: list[int] = []

    def resolve(self, pidfd: int) -> int:
        self.calls.append(pidfd)
        return self.process_id


class FakePidfdWaiter:
    def __init__(self, *results: PidfdWaitOutcome | None) -> None:
        self._results = list(results)
        self.calls: list[tuple[int, float | None]] = []

    def wait(
        self,
        pidfd: int,
        *,
        timeout_seconds: float | None = None,
    ) -> PidfdWaitOutcome | None:
        self.calls.append((pidfd, timeout_seconds))
        if not self._results:
            raise AssertionError("wait() was called more times than expected")
        return self._results.pop(0)


class FakePidfdSignaler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def send_signal(self, pidfd: int, signal_number: int) -> None:
        self.calls.append((pidfd, signal_number))


class FakeCgroupKiller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def kill(self, cgroup_path: str) -> None:
        self.calls.append(cgroup_path)


@dataclass
class FakeOomObserver:
    result: bool

    def __post_init__(self) -> None:
        self.calls: list[UUID] = []

    def was_oom_killed(self, handle) -> bool:
        self.calls.append(handle.container_id)
        return self.result


@dataclass
class FakePidfdOpener:
    pidfd: int

    def __post_init__(self) -> None:
        self.calls: list[int] = []

    def open(self, process_id: int) -> int:
        self.calls.append(process_id)
        return self.pidfd


class FakeBridge:
    def __init__(self, pidfd: int) -> None:
        self.pidfd = pidfd
        self.calls: list[tuple[bytes, str]] = []

    def execute(self, serialized_runspec: bytes, rootfs_path: str) -> ExecutionResult:
        self.calls.append((serialized_runspec, rootfs_path))
        return ExecutionResult(pidfd=self.pidfd)


def test_container_supervisor_start_wait_exit_flow_with_mocks() -> None:
    runspec = _build_runspec()
    started_at = datetime(2026, 3, 13, 18, 1, tzinfo=timezone.utc)
    exited_at = started_at + timedelta(seconds=3)
    serializer = FakeSerializer(serialized_runspec=b"serialized-runspec")
    bridge = FakeBridge(pidfd=41)
    waiter = FakePidfdWaiter(PidfdWaitOutcome(exit_code=0, signal=None))
    resolver = FakePidfdProcessIdResolver(process_id=1234)

    supervisor = ContainerSupervisor(
        serializer=serializer,
        bridge=bridge,
        pidfd_waiter=waiter,
        pidfd_process_id_resolver=resolver,
        cgroup_path_factory=lambda container_id: f"/sys/fs/cgroup/test/{container_id}",
        clock=SequenceClock(started_at, exited_at),
        pidfd_closer=lambda pidfd: None,
    )

    handle = supervisor.start(runspec, runspec.rootfs_path.value)
    exit_status = supervisor.wait(handle)
    emitted_events = supervisor.drain_events()

    assert serializer.calls == [runspec]
    assert bridge.calls == [(b"serialized-runspec", runspec.rootfs_path.value)]
    assert resolver.calls == [41]
    assert waiter.calls == [(41, None)]
    assert handle.pidfd == 41
    assert handle.init_process_id == 1234
    assert exit_status.exit_code == 0
    assert exit_status.signal is None
    assert exit_status.duration_seconds == pytest.approx(3.0)
    assert [type(event) for event in emitted_events] == [ContainerStarted, ContainerExited]
    assert emitted_events[0].container_id == handle.container_id
    assert emitted_events[1].container_id == handle.container_id


def test_container_supervisor_terminate_escalates_from_sigterm_to_cgroup_kill() -> None:
    runspec = _build_runspec()
    started_at = datetime(2026, 3, 13, 18, 2, tzinfo=timezone.utc)
    serializer = FakeSerializer(serialized_runspec=b"serialized-runspec")
    bridge = FakeBridge(pidfd=52)
    waiter = FakePidfdWaiter(None)
    resolver = FakePidfdProcessIdResolver(process_id=2222)
    signaler = FakePidfdSignaler()
    cgroup_killer = FakeCgroupKiller()

    supervisor = ContainerSupervisor(
        serializer=serializer,
        bridge=bridge,
        pidfd_waiter=waiter,
        pidfd_signaler=signaler,
        cgroup_killer=cgroup_killer,
        pidfd_process_id_resolver=resolver,
        cgroup_path_factory=lambda container_id: f"/sys/fs/cgroup/test/{container_id}",
        clock=SequenceClock(started_at, started_at + timedelta(milliseconds=250)),
        pidfd_closer=lambda pidfd: None,
    )

    handle = supervisor.start(runspec, runspec.rootfs_path.value)
    supervisor.drain_events()

    supervisor.terminate(handle, timeout_seconds=0.25)
    emitted_events = supervisor.drain_events()

    assert signaler.calls == [(52, 15)]
    assert waiter.calls == [(52, 0.25)]
    assert cgroup_killer.calls == [handle.cgroup_path]
    assert [type(event) for event in emitted_events] == [ContainerForceKilled]
    assert "0.250 seconds" in emitted_events[0].reason


def test_container_supervisor_detects_oom_kill_and_emits_both_events() -> None:
    runspec = _build_runspec()
    started_at = datetime(2026, 3, 13, 18, 3, tzinfo=timezone.utc)
    exited_at = started_at + timedelta(seconds=1)
    serializer = FakeSerializer(serialized_runspec=b"serialized-runspec")
    bridge = FakeBridge(pidfd=60)
    waiter = FakePidfdWaiter(PidfdWaitOutcome(exit_code=137, signal=9))
    resolver = FakePidfdProcessIdResolver(process_id=3333)
    oom_observer = FakeOomObserver(result=True)

    supervisor = ContainerSupervisor(
        serializer=serializer,
        bridge=bridge,
        pidfd_waiter=waiter,
        oom_observer=oom_observer,
        pidfd_process_id_resolver=resolver,
        cgroup_path_factory=lambda container_id: f"/sys/fs/cgroup/test/{container_id}",
        clock=SequenceClock(started_at, exited_at),
        pidfd_closer=lambda pidfd: None,
    )

    handle = supervisor.start(runspec, runspec.rootfs_path.value)
    supervisor.drain_events()

    exit_status = supervisor.wait(handle)
    emitted_events = supervisor.drain_events()

    assert exit_status.was_oom_killed is True
    assert oom_observer.calls == [handle.container_id]
    assert [type(event) for event in emitted_events] == [
        ContainerOOMKilled,
        ContainerExited,
    ]
    assert emitted_events[0].memory_limit == runspec.resources.memory.max_bytes
    assert emitted_events[1].signal == 9


def test_container_supervisor_can_reattach_after_control_plane_restart() -> None:
    runspec = _build_runspec()
    started_at = datetime(2026, 3, 13, 18, 4, tzinfo=timezone.utc)
    shared_registry = InMemoryContainerRegistry()
    serializer = FakeSerializer(serialized_runspec=b"serialized-runspec")
    bridge = FakeBridge(pidfd=70)
    resolver = FakePidfdProcessIdResolver(process_id=4444)

    first_supervisor = ContainerSupervisor(
        serializer=serializer,
        bridge=bridge,
        registry=shared_registry,
        pidfd_process_id_resolver=resolver,
        cgroup_path_factory=lambda container_id: f"/sys/fs/cgroup/test/{container_id}",
        clock=SequenceClock(started_at),
        pidfd_closer=lambda pidfd: None,
    )

    initial_handle = first_supervisor.start(runspec, runspec.rootfs_path.value)

    pidfd_opener = FakePidfdOpener(pidfd=99)
    restarted_supervisor = ContainerSupervisor(
        serializer=serializer,
        bridge=bridge,
        registry=shared_registry,
        pidfd_opener=pidfd_opener,
        clock=SequenceClock(started_at + timedelta(seconds=1)),
        pidfd_closer=lambda pidfd: None,
    )

    reattached_handle = restarted_supervisor.reattach(initial_handle.container_id)

    assert pidfd_opener.calls == [4444]
    assert reattached_handle.container_id == initial_handle.container_id
    assert reattached_handle.init_process_id == 4444
    assert reattached_handle.pidfd == 99
    assert shared_registry.load(initial_handle.container_id).pidfd == 99
