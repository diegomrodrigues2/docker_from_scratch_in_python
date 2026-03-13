from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from runspec_contract.application import (
    ImageMetadata,
    PidfdWaitOutcome,
    PlatformDefaults,
    PolicyConfig,
    RawCompilationInputs,
    UserOverrides,
)
from runspec_contract.bootstrap import bootstrap_runtime, compile_and_execute
from runspec_contract.domain import ContainerExited, ContainerStarted, RunSpecCompiled, ValidationError
from runspec_contract.infrastructure import KernelCapabilities


def _valid_kernel_capabilities() -> KernelCapabilities:
    return KernelCapabilities(
        kernel_version=(6, 8, 0),
        has_cgroups_v2=True,
        has_clone3=True,
        has_pidfd=True,
        has_user_ns=True,
        has_pid_ns=True,
        has_mount_ns=True,
        has_uts_ns=True,
        has_net_ns=True,
        has_seccomp=True,
        has_pivot_root=True,
        available_cgroup_controllers=frozenset({"cpu", "memory", "pids"}),
    )


def _build_raw_inputs(*, rootfs_path: str | None, cwd: str = "/workspace") -> RawCompilationInputs:
    platform_defaults = (
        PlatformDefaults(rootfs_path=rootfs_path)
        if rootfs_path is not None
        else PlatformDefaults()
    )
    return RawCompilationInputs(
        image_metadata=ImageMetadata(
            argv=("/usr/bin/python", "-m", "demo_app"),
            uid=1000,
            gid=1000,
            cwd="/workspace",
            environment={
                "IMAGE_ONLY": "yes",
                "PATH": "/usr/bin",
            },
            cpu_quota_us=50_000,
            cpu_period_us=100_000,
            memory_max_bytes=8 * 1024 * 1024,
        ),
        platform_defaults=platform_defaults,
        policy_config=PolicyConfig(
            environment={"POLICY_MODE": "strict"},
            effective_caps=frozenset({"CAP_CHOWN"}),
            permitted_caps=frozenset({"CAP_CHOWN"}),
            bounding_caps=frozenset({"CAP_CHOWN"}),
            seccomp_profile="runtime/default",
        ),
        user_overrides=UserOverrides(
            cwd=cwd,
            environment={"PATH": "/custom/bin"},
        ),
        compiled_at=datetime(2026, 3, 13, 18, 0, tzinfo=timezone.utc),
        correlation_id=uuid4(),
    )


class FixedKernelProbe:
    def __init__(self, capabilities: KernelCapabilities | None = None) -> None:
        self._capabilities = capabilities or _valid_kernel_capabilities()
        self.calls = 0

    def probe(self) -> KernelCapabilities:
        self.calls += 1
        return self._capabilities


class SequenceClock:
    def __init__(self, *timestamps: datetime) -> None:
        self._timestamps = list(timestamps)

    def __call__(self) -> datetime:
        if not self._timestamps:
            raise AssertionError("clock was called more times than expected")
        return self._timestamps.pop(0)


@dataclass
class CapturingNativeExecutor:
    pidfd: int

    def __post_init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []

    def __call__(self, serialized: bytes, rootfs_path: str) -> int:
        self.calls.append((serialized, rootfs_path))
        return self.pidfd


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


def test_bootstrap_runtime_compiles_and_round_trips_runspec_serialization() -> None:
    kernel_probe = FixedKernelProbe()
    runtime = bootstrap_runtime(kernel_probe=kernel_probe)

    runspec = runtime.compile(
        _build_raw_inputs(rootfs_path="/var/lib/containers/demo/rootfs")
    )
    serialized = runtime.serializer.serialize(runspec)
    restored_runspec = runtime.serializer.deserialize(serialized)

    assert kernel_probe.calls == 1
    assert restored_runspec == runspec
    assert runtime.serializer.serialize(restored_runspec) == serialized


def test_bootstrap_runtime_rejects_invalid_inputs_before_bridge_execution() -> None:
    native_executor = CapturingNativeExecutor(pidfd=41)
    runtime = bootstrap_runtime(
        kernel_probe=FixedKernelProbe(),
        native_executor=native_executor,
    )

    with pytest.raises(ValidationError, match="path must be absolute"):
        runtime.compile_and_execute(
            _build_raw_inputs(
                rootfs_path="/var/lib/containers/demo/rootfs",
                cwd="relative/path",
            ),
            "/var/lib/containers/demo/rootfs",
        )

    assert native_executor.calls == []
    assert runtime.drain_events() == ()


def test_bootstrap_runtime_emits_events_across_compile_execute_and_wait() -> None:
    started_at = datetime(2026, 3, 13, 18, 5, tzinfo=timezone.utc)
    exited_at = started_at + timedelta(seconds=2)
    native_executor = CapturingNativeExecutor(pidfd=77)
    pidfd_process_id_resolver = FakePidfdProcessIdResolver(process_id=4321)
    pidfd_waiter = FakePidfdWaiter(PidfdWaitOutcome(exit_code=0, signal=None))

    runtime = bootstrap_runtime(
        kernel_probe=FixedKernelProbe(),
        native_executor=native_executor,
        pidfd_process_id_resolver=pidfd_process_id_resolver,
        pidfd_waiter=pidfd_waiter,
        cgroup_path_factory=lambda container_id: f"/sys/fs/cgroup/test/{container_id}",
        clock=SequenceClock(started_at, exited_at),
        pidfd_closer=lambda pidfd: None,
    )

    handle = compile_and_execute(
        _build_raw_inputs(rootfs_path=None),
        "/var/lib/containers/demo/rootfs",
        runtime=runtime,
    )
    exit_status = runtime.wait(handle)
    emitted_events = runtime.drain_events()

    assert native_executor.calls[0][1] == "/var/lib/containers/demo/rootfs"
    assert pidfd_process_id_resolver.calls == [77]
    assert pidfd_waiter.calls == [(77, None)]
    assert handle.pidfd == 77
    assert handle.init_process_id == 4321
    assert exit_status.exit_code == 0
    assert exit_status.duration_seconds == pytest.approx(2.0)
    assert [type(event) for event in emitted_events] == [
        RunSpecCompiled,
        ContainerStarted,
        ContainerExited,
    ]
