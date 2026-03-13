"""Container lifecycle supervision built around pidfd-based process control.

This module implements the ``ContainerSupervisor`` described in section 5 of
``specs/run_spec/design.md`` and covers the lifecycle requirements referenced
by task 12 in ``specs/run_spec/tasks.md``.

Why this module exists:
    The ACL bridge can start a container and return a pidfd, but the control
    plane still needs a dedicated orchestration component to supervise the
    process after bootstrap succeeds.

Step-by-step runtime flow:
    1. ``start()`` serializes the ``RunSpec`` and invokes ``CythonBridge``.
    2. The returned pidfd is converted into a durable ``ContainerHandle``.
    3. ``wait()`` blocks on ``waitid(P_PIDFD)`` and then emits exit events.
    4. ``terminate()`` sends ``SIGTERM`` and waits for a bounded grace period.
    5. If the grace period expires, ``force_kill()`` writes ``cgroup.kill``.
    6. ``reattach()`` reopens a new pidfd from the persisted init PID.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Protocol
from uuid import UUID, uuid4

from runspec_contract.domain import (
    ContainerExited,
    ContainerForceKilled,
    ContainerOOMKilled,
    ContainerStarted,
    KernelFeatureError,
    MIN_MEMORY_BYTES,
    RunSpec,
)
from runspec_contract.infrastructure import CythonBridge

LifecycleEvent = (
    ContainerStarted | ContainerExited | ContainerOOMKilled | ContainerForceKilled
)

_DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"
_DEFAULT_SIGTERM = getattr(signal, "SIGTERM", 15)
_DEFAULT_WAIT_POLL_INTERVAL_SECONDS = 0.01


def _utc_now() -> datetime:
    """Return an aware UTC timestamp for domain event emission."""

    return datetime.now(tz=timezone.utc)


def _require_uuid(argument_name: str, value: UUID) -> UUID:
    """Validate UUID payloads carried by application-level dataclasses."""

    if not isinstance(value, UUID):
        raise TypeError(f"{argument_name} must be a UUID.")
    return value


def _require_non_negative_int(argument_name: str, value: int) -> int:
    """Validate integer fields that may be zero but never negative."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{argument_name} must be an integer.")
    if value < 0:
        raise ValueError(f"{argument_name} must be non-negative.")
    return value


def _require_positive_int(argument_name: str, value: int) -> int:
    """Validate integer fields that must be strictly positive."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{argument_name} must be an integer.")
    if value <= 0:
        raise ValueError(f"{argument_name} must be positive.")
    return value


def _require_non_negative_float(argument_name: str, value: float) -> float:
    """Validate timeout and duration values represented as floats."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{argument_name} must be a number.")
    numeric_value = float(value)
    if numeric_value < 0:
        raise ValueError(f"{argument_name} must be non-negative.")
    return numeric_value


def _require_bytes(argument_name: str, value: bytes) -> bytes:
    """Validate binary payloads such as canonical hashes."""

    if not isinstance(value, bytes):
        raise TypeError(f"{argument_name} must be bytes.")
    return value


def _require_non_empty_string(argument_name: str, value: str) -> str:
    """Validate text fields forwarded to kernel-facing adapters."""

    if not isinstance(value, str):
        raise TypeError(f"{argument_name} must be a string.")
    if not value.strip():
        raise ValueError(f"{argument_name} must not be empty.")
    return value


def _require_aware_datetime(argument_name: str, value: datetime) -> datetime:
    """Validate event timestamps and persisted start times."""

    if not isinstance(value, datetime):
        raise TypeError(f"{argument_name} must be a datetime.")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{argument_name} must be timezone-aware.")
    return value


@dataclass(frozen=True, slots=True)
class ContainerHandle:
    """Stable metadata required to supervise and later reattach a container.

    The pidfd itself is process-local and cannot survive a control-plane crash.
    For that reason the handle also persists ``init_process_id``. On reattach we
    reopen a fresh pidfd from that PID and replace only the volatile pidfd
    portion of the handle, keeping the rest of the lifecycle metadata intact.
    """

    container_id: UUID
    correlation_id: UUID
    runspec_hash: bytes
    pidfd: int
    init_process_id: int
    cgroup_path: str
    memory_limit_bytes: int
    started_at: datetime

    def __post_init__(self) -> None:
        _require_uuid("ContainerHandle.container_id", self.container_id)
        _require_uuid("ContainerHandle.correlation_id", self.correlation_id)
        _require_bytes("ContainerHandle.runspec_hash", self.runspec_hash)
        _require_non_negative_int("ContainerHandle.pidfd", self.pidfd)
        _require_positive_int("ContainerHandle.init_process_id", self.init_process_id)
        _require_non_empty_string("ContainerHandle.cgroup_path", self.cgroup_path)

        memory_limit_bytes = _require_non_negative_int(
            "ContainerHandle.memory_limit_bytes",
            self.memory_limit_bytes,
        )
        if memory_limit_bytes < MIN_MEMORY_BYTES:
            raise ValueError(
                "ContainerHandle.memory_limit_bytes must be at least "
                f"{MIN_MEMORY_BYTES} bytes (4 MiB)."
            )

        _require_aware_datetime("ContainerHandle.started_at", self.started_at)


@dataclass(frozen=True, slots=True)
class ContainerExitStatus:
    """Terminal status returned by ``wait()`` after pidfd-based supervision.

    ``exit_code`` follows the conventional process-status mapping used by most
    supervisors:
    - normal exit: the process exit status
    - signal exit: ``128 + signal``

    The raw signal number is still preserved separately in ``signal`` so event
    consumers do not need to reverse that convention.
    """

    container_id: UUID
    exit_code: int
    signal: int | None
    duration_seconds: float
    was_oom_killed: bool
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_uuid("ContainerExitStatus.container_id", self.container_id)
        _require_non_negative_int("ContainerExitStatus.exit_code", self.exit_code)
        if self.signal is not None:
            _require_positive_int("ContainerExitStatus.signal", self.signal)
        _require_non_negative_float(
            "ContainerExitStatus.duration_seconds",
            self.duration_seconds,
        )
        if not isinstance(self.was_oom_killed, bool):
            raise TypeError("ContainerExitStatus.was_oom_killed must be a bool.")
        _require_aware_datetime("ContainerExitStatus.occurred_at", self.occurred_at)


@dataclass(frozen=True, slots=True)
class PidfdWaitOutcome:
    """Normalized result of ``waitid(P_PIDFD)``.

    The dedicated dataclass keeps the translation from platform-specific
    ``waitid_result`` structures localized in the waiter adapter instead of
    spreading ``si_code`` handling throughout ``ContainerSupervisor``.
    """

    exit_code: int
    signal: int | None

    def __post_init__(self) -> None:
        _require_non_negative_int("PidfdWaitOutcome.exit_code", self.exit_code)
        if self.signal is not None:
            _require_positive_int("PidfdWaitOutcome.signal", self.signal)


class RunSpecSerializer(Protocol):
    """Serialize a compiled ``RunSpec`` into the ACL binary envelope."""

    def serialize(self, runspec: RunSpec) -> bytes:
        """Serialize the immutable aggregate into bytes for bridge execution."""


class ContainerRegistry(Protocol):
    """Persist running-container metadata needed by supervision and reattach."""

    def save(self, handle: ContainerHandle) -> None:
        """Persist or replace the metadata for one running container."""

    def load(self, container_id: UUID) -> ContainerHandle:
        """Load the currently known handle for ``container_id``."""

    def delete(self, container_id: UUID) -> None:
        """Forget the container metadata once the process has exited."""


class PidfdWaiter(Protocol):
    """Wait for a process referenced by pidfd using ``waitid(P_PIDFD)``."""

    def wait(
        self,
        pidfd: int,
        *,
        timeout_seconds: float | None = None,
    ) -> PidfdWaitOutcome | None:
        """Wait for process exit or return ``None`` when a timeout expires."""


class PidfdSignaler(Protocol):
    """Send signals directly to a process referenced by pidfd."""

    def send_signal(self, pidfd: int, signal_number: int) -> None:
        """Send ``signal_number`` to the process referenced by ``pidfd``."""


class CgroupKiller(Protocol):
    """Forcefully terminate every process in a container cgroup."""

    def kill(self, cgroup_path: str) -> None:
        """Write to ``cgroup.kill`` for the given cgroup leaf."""


class OomObserver(Protocol):
    """Inspect cgroup memory events to decide whether OOM killed the container."""

    def was_oom_killed(self, handle: ContainerHandle) -> bool:
        """Return ``True`` when cgroup memory events show an OOM kill."""


class PidfdOpener(Protocol):
    """Open a fresh pidfd from a persisted process identifier."""

    def open(self, process_id: int) -> int:
        """Recreate a pidfd for ``process_id`` during reattach."""


class PidfdProcessIdResolver(Protocol):
    """Resolve the numeric PID associated with a live pidfd."""

    def resolve(self, pidfd: int) -> int:
        """Translate a pidfd into the corresponding init-process PID."""


class InMemoryContainerRegistry:
    """Process-local metadata store used by tests and simple embeddings.

    This registry deliberately keeps behavior minimal: it stores immutable
    handles keyed by container UUID. Production code can swap it for a durable
    backing store, but tests can still share one instance across supervisor
    objects to simulate a control-plane restart.
    """

    def __init__(self) -> None:
        self._handles_by_container_id: dict[UUID, ContainerHandle] = {}

    def save(self, handle: ContainerHandle) -> None:
        self._handles_by_container_id[handle.container_id] = handle

    def load(self, container_id: UUID) -> ContainerHandle:
        try:
            return self._handles_by_container_id[container_id]
        except KeyError as exc:
            raise LookupError(f"container '{container_id}' is not registered") from exc

    def delete(self, container_id: UUID) -> None:
        self._handles_by_container_id.pop(container_id, None)


class WaitIdPidfdWaiter:
    """Linux waiter that polls ``waitid(P_PIDFD)`` until a process exits.

    ``waitid`` itself does not accept a timeout parameter, so the implementation
    uses ``WNOHANG`` in a small polling loop when the caller requests a bounded
    wait. This preserves the required pidfd-based wait semantics while allowing
    ``terminate()`` to implement the SIGTERM grace period from requirement 16.3.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = sleep,
        poll_interval_seconds: float = _DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._poll_interval_seconds = _require_non_negative_float(
            "poll_interval_seconds",
            poll_interval_seconds,
        )

    def wait(
        self,
        pidfd: int,
        *,
        timeout_seconds: float | None = None,
    ) -> PidfdWaitOutcome | None:
        """Wait for ``pidfd`` to exit, optionally with a timeout.

        Args:
            pidfd: Process file descriptor returned by ``clone3(CLONE_PIDFD)``.
            timeout_seconds: Maximum grace period. ``None`` means "wait
                indefinitely" and uses a blocking ``waitid`` call.

        Returns:
            ``PidfdWaitOutcome`` when the process exited, otherwise ``None`` if
            the bounded wait timed out.

        Raises:
            KernelFeatureError: When the local Python runtime cannot expose the
                ``waitid(P_PIDFD)`` primitive required by the spec.
            OSError: Propagated when the underlying syscall fails.
        """

        _require_non_negative_int("pidfd", pidfd)
        if timeout_seconds is not None:
            timeout_seconds = _require_non_negative_float(
                "timeout_seconds",
                timeout_seconds,
            )

        if not hasattr(os, "waitid") or not hasattr(os, "P_PIDFD"):
            raise KernelFeatureError(
                "waitid(P_PIDFD)",
                "the local Python runtime does not expose waitid(P_PIDFD)",
            )

        if timeout_seconds is None:
            waitid_result = os.waitid(os.P_PIDFD, pidfd, os.WEXITED)
            return self._translate_waitid_result(pidfd, waitid_result)

        if not hasattr(os, "WNOHANG"):
            raise KernelFeatureError(
                "waitid(WNOHANG)",
                "the local Python runtime does not expose WNOHANG for bounded pidfd waits",
            )

        deadline = self._monotonic() + timeout_seconds
        while True:
            waitid_result = os.waitid(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG)
            if waitid_result is not None:
                return self._translate_waitid_result(pidfd, waitid_result)

            remaining_seconds = deadline - self._monotonic()
            if remaining_seconds <= 0:
                return None

            # The sleep interval is clamped to the remaining budget so the
            # grace period never overshoots the requested timeout.
            self._sleeper(min(self._poll_interval_seconds, remaining_seconds))

    @staticmethod
    def _translate_waitid_result(pidfd: int, waitid_result: object) -> PidfdWaitOutcome:
        """Translate Python's ``waitid_result`` into a stable application type.

        ``waitid`` reports signal-related exits through ``si_code`` and
        ``si_status``. The supervisor normalizes that into the explicit pair
        ``(exit_code, signal)`` so the rest of the application layer can stay
        independent from the raw ``siginfo_t`` encoding.
        """

        status_code = getattr(waitid_result, "si_code", None)
        status_value = getattr(waitid_result, "si_status", None)

        exited_code = getattr(os, "CLD_EXITED", object())
        killed_code = getattr(os, "CLD_KILLED", object())
        dumped_code = getattr(os, "CLD_DUMPED", object())

        if status_code == exited_code:
            return PidfdWaitOutcome(exit_code=int(status_value), signal=None)

        if status_code in {killed_code, dumped_code}:
            signal_number = int(status_value)
            return PidfdWaitOutcome(
                exit_code=128 + signal_number,
                signal=signal_number,
            )

        raise RuntimeError(
            "waitid(P_PIDFD) returned an unexpected result for "
            f"pidfd {pidfd}: si_code={status_code!r} si_status={status_value!r}"
        )


class LinuxPidfdSignaler:
    """Signal sender implemented with ``signal.pidfd_send_signal``."""

    def send_signal(self, pidfd: int, signal_number: int) -> None:
        _require_non_negative_int("pidfd", pidfd)
        _require_positive_int("signal_number", signal_number)

        if not hasattr(signal, "pidfd_send_signal"):
            raise KernelFeatureError(
                "pidfd_send_signal",
                "the local Python runtime does not expose signal.pidfd_send_signal",
            )

        signal.pidfd_send_signal(pidfd, signal_number)


class LinuxCgroupKiller:
    """cgroup v2 killer implemented by writing ``1`` into ``cgroup.kill``.

    The control plane writes a single byte to the dedicated leaf's
    ``cgroup.kill`` file. In cgroup v2 that operation instructs the kernel to
    terminate every process currently attached to the cgroup, which is exactly
    the fork-bomb containment behavior required by requirement 16.3.
    """

    def kill(self, cgroup_path: str) -> None:
        cgroup_path = _require_non_empty_string("cgroup_path", cgroup_path)
        cgroup_kill_path = Path(cgroup_path) / "cgroup.kill"

        with cgroup_kill_path.open("w", encoding="ascii", newline="") as kill_file:
            kill_file.write("1")


class NullOomObserver:
    """Fallback OOM observer for environments without memory-event wiring."""

    def was_oom_killed(self, handle: ContainerHandle) -> bool:
        del handle
        return False


class PythonPidfdOpener:
    """Recreate pidfds with ``os.pidfd_open`` during control-plane reattach."""

    def open(self, process_id: int) -> int:
        _require_positive_int("process_id", process_id)

        if not hasattr(os, "pidfd_open"):
            raise KernelFeatureError(
                "pidfd_open",
                "the local Python runtime does not expose os.pidfd_open",
            )

        return os.pidfd_open(process_id, 0)


class ProcfsPidfdProcessIdResolver:
    """Resolve the PID associated with a pidfd via ``/proc/self/fdinfo``.

    Why this adapter exists:
        A pidfd integer alone is not durable across a control-plane restart.
        The supervisor therefore needs to capture the numeric init PID at start
        time so ``reattach()`` can later call ``pidfd_open(pid)`` and recreate a
        fresh pidfd in the new process.

    Linux exposes that mapping in ``/proc/self/fdinfo/<pidfd>``. Reading the
    procfs metadata is safe here because the lookup is introspecting the current
    process's own file descriptor table, not traversing any untrusted container
    filesystem path.
    """

    def resolve(self, pidfd: int) -> int:
        _require_non_negative_int("pidfd", pidfd)
        fdinfo_path = Path("/proc/self/fdinfo") / str(pidfd)

        try:
            fdinfo_text = fdinfo_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise KernelFeatureError(
                "pidfd fdinfo",
                f"pidfd metadata file '{fdinfo_path}' is unavailable",
            ) from exc

        for line in fdinfo_text.splitlines():
            if line.startswith("Pid:"):
                _, _, raw_pid = line.partition(":")
                return _require_positive_int("resolved_pid", int(raw_pid.strip()))

        raise RuntimeError(
            f"pidfd metadata file '{fdinfo_path}' does not contain a 'Pid:' entry"
        )


class ContainerSupervisor:
    """Supervise container lifecycle after bridge startup has succeeded.

    The supervisor owns only lifecycle orchestration. It does not compile
    RunSpecs and it does not contain native bootstrap logic. Its responsibility
    starts exactly when a serialized ``RunSpec`` is ready and the control plane
    needs to manage the resulting process over time.
    """

    def __init__(
        self,
        serializer: RunSpecSerializer,
        bridge: CythonBridge,
        *,
        registry: ContainerRegistry | None = None,
        pidfd_waiter: PidfdWaiter | None = None,
        pidfd_signaler: PidfdSignaler | None = None,
        cgroup_killer: CgroupKiller | None = None,
        oom_observer: OomObserver | None = None,
        pidfd_opener: PidfdOpener | None = None,
        pidfd_process_id_resolver: PidfdProcessIdResolver | None = None,
        cgroup_path_factory: Callable[[UUID], str] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        pidfd_closer: Callable[[int], None] = os.close,
        sigterm_number: int = _DEFAULT_SIGTERM,
    ) -> None:
        """Create a supervisor with explicit infrastructure dependencies.

        Args:
            serializer: Application-to-ACL serializer used before bridge
                execution.
            bridge: ACL boundary that performs native execution.
            registry: Metadata store used to persist running handles.
            pidfd_waiter: Wait adapter that must use ``waitid(P_PIDFD)``.
            pidfd_signaler: Signal adapter used by graceful termination.
            cgroup_killer: Force-kill adapter used for escalation.
            oom_observer: Adapter that inspects cgroup memory events.
            pidfd_opener: Adapter used by ``reattach()`` to recreate pidfds.
            pidfd_process_id_resolver: Adapter that extracts the init PID from
                the initial pidfd so reattach metadata can be persisted.
            cgroup_path_factory: Strategy that maps a container UUID to its leaf
                cgroup path. The default uses ``/sys/fs/cgroup/<container_id>``.
            clock: Aware clock used for event timestamps and durations.
            pidfd_closer: Callable used to close pidfds once exit has been
                reaped. Tests can inject a no-op when they use synthetic fds.
            sigterm_number: Signal number used for graceful termination.
        """

        self._serializer = serializer
        self._bridge = bridge
        self._registry = registry or InMemoryContainerRegistry()
        self._pidfd_waiter = pidfd_waiter or WaitIdPidfdWaiter()
        self._pidfd_signaler = pidfd_signaler or LinuxPidfdSignaler()
        self._cgroup_killer = cgroup_killer or LinuxCgroupKiller()
        self._oom_observer = oom_observer or NullOomObserver()
        self._pidfd_opener = pidfd_opener or PythonPidfdOpener()
        self._pidfd_process_id_resolver = (
            pidfd_process_id_resolver or ProcfsPidfdProcessIdResolver()
        )
        self._cgroup_path_factory = cgroup_path_factory or self._default_cgroup_path
        self._clock = clock
        self._pidfd_closer = pidfd_closer
        self._sigterm_number = _require_positive_int("sigterm_number", sigterm_number)

        self._pending_events: list[LifecycleEvent] = []
        self._completed_statuses: dict[UUID, ContainerExitStatus] = {}
        self._force_kill_reasons: dict[UUID, str] = {}

    def start(self, runspec: RunSpec, rootfs_path: str) -> ContainerHandle:
        """Start a container through the serializer + ACL bridge pipeline.

        Args:
            runspec: Immutable execution contract produced by the admission
                pipeline.
            rootfs_path: Prepared root filesystem path forwarded to the native
                bootstrap logic.

        Returns:
            A ``ContainerHandle`` containing the metadata required for later
            supervision, force-kill, and reattach.
        """

        if not isinstance(runspec, RunSpec):
            raise TypeError("runspec must be a RunSpec instance.")

        rootfs_path = _require_non_empty_string("rootfs_path", rootfs_path)
        self._ensure_rootfs_matches_runspec(runspec, rootfs_path)

        serialized_runspec = self._serializer.serialize(runspec)
        execution_result = self._bridge.execute(serialized_runspec, rootfs_path)

        started_at = _require_aware_datetime("started_at", self._clock())
        init_process_id = self._pidfd_process_id_resolver.resolve(execution_result.pidfd)
        container_id = uuid4()
        container_handle = ContainerHandle(
            container_id=container_id,
            correlation_id=runspec.correlation_id,
            runspec_hash=runspec.canonical_hash,
            pidfd=execution_result.pidfd,
            init_process_id=init_process_id,
            cgroup_path=self._cgroup_path_factory(container_id),
            memory_limit_bytes=runspec.resources.memory.max_bytes,
            started_at=started_at,
        )

        # Persistence happens before event emission so a consumer reacting to the
        # start event can immediately look up reattach metadata if needed.
        self._registry.save(container_handle)
        self._emit_event(
            ContainerStarted(
                event_id=uuid4(),
                occurred_at=started_at,
                correlation_id=runspec.correlation_id,
                container_id=container_id,
                runspec_hash=runspec.canonical_hash,
                pidfd=execution_result.pidfd,
            )
        )
        return container_handle

    def wait(self, handle: ContainerHandle) -> ContainerExitStatus:
        """Wait until the container exits and emit terminal lifecycle events."""

        active_handle = self._resolve_active_handle(handle)
        cached_status = self._completed_statuses.get(active_handle.container_id)
        if cached_status is not None:
            return cached_status

        wait_outcome = self._pidfd_waiter.wait(active_handle.pidfd, timeout_seconds=None)
        if wait_outcome is None:
            raise RuntimeError(
                "pidfd waiter returned no result during an unbounded wait; "
                "this indicates a broken waiter implementation"
            )
        return self._record_exit(active_handle, wait_outcome)

    def terminate(self, handle: ContainerHandle, timeout_seconds: float) -> None:
        """Attempt graceful termination and escalate to ``cgroup.kill`` on timeout.

        The method may reap the process if it exits during the grace period. In
        that case a later ``wait()`` call returns the cached terminal status
        instead of performing a second ``waitid`` call.
        """

        timeout_seconds = _require_non_negative_float("timeout_seconds", timeout_seconds)
        active_handle = self._resolve_active_handle(handle)
        if active_handle.container_id in self._completed_statuses:
            return

        self._pidfd_signaler.send_signal(active_handle.pidfd, self._sigterm_number)
        wait_outcome = self._pidfd_waiter.wait(
            active_handle.pidfd,
            timeout_seconds=timeout_seconds,
        )
        if wait_outcome is not None:
            self._record_exit(active_handle, wait_outcome)
            return

        self._force_kill_with_reason(
            active_handle,
            reason=(
                "SIGTERM grace period expired after "
                f"{timeout_seconds:.3f} seconds"
            ),
        )

    def force_kill(self, handle: ContainerHandle) -> None:
        """Forcefully terminate the entire container cgroup immediately."""

        active_handle = self._resolve_active_handle(handle)
        if active_handle.container_id in self._completed_statuses:
            return

        self._force_kill_with_reason(
            active_handle,
            reason="force_kill requested explicitly",
        )

    def reattach(self, container_id: UUID) -> ContainerHandle:
        """Reopen a fresh pidfd for a still-running container after a crash."""

        _require_uuid("container_id", container_id)
        if container_id in self._completed_statuses:
            raise LookupError(f"container '{container_id}' has already exited")

        persisted_handle = self._registry.load(container_id)
        reattached_pidfd = self._pidfd_opener.open(persisted_handle.init_process_id)
        reattached_handle = replace(persisted_handle, pidfd=reattached_pidfd)
        self._registry.save(reattached_handle)
        return reattached_handle

    def drain_events(self) -> tuple[LifecycleEvent, ...]:
        """Return and clear the lifecycle events emitted since the last drain."""

        pending_events = tuple(self._pending_events)
        self._pending_events.clear()
        return pending_events

    @staticmethod
    def _default_cgroup_path(container_id: UUID) -> str:
        """Map each container UUID to a deterministic leaf under ``/sys/fs/cgroup``."""

        return str(Path(_DEFAULT_CGROUP_ROOT) / str(container_id))

    @staticmethod
    def _ensure_rootfs_matches_runspec(runspec: RunSpec, rootfs_path: str) -> None:
        """Reject mismatched rootfs paths before native execution starts.

        The bridge takes ``rootfs_path`` as a separate argument, but the
        aggregate already carries the canonical rootfs path chosen by the
        admission pipeline. Keeping both values aligned prevents the control
        plane from serializing one contract and executing another.
        """

        if runspec.rootfs_path.value != rootfs_path:
            raise ValueError(
                "rootfs_path must match RunSpec.rootfs_path.value to keep the "
                "execution contract coherent"
            )

    def _resolve_active_handle(self, handle: ContainerHandle) -> ContainerHandle:
        """Prefer the registry copy so reattached pidfds are automatically used."""

        if not isinstance(handle, ContainerHandle):
            raise TypeError("handle must be a ContainerHandle instance.")

        cached_status = self._completed_statuses.get(handle.container_id)
        if cached_status is not None:
            return handle

        try:
            return self._registry.load(handle.container_id)
        except LookupError:
            return handle

    def _force_kill_with_reason(self, handle: ContainerHandle, *, reason: str) -> None:
        """Write ``cgroup.kill`` once and emit the corresponding domain event."""

        if handle.container_id in self._force_kill_reasons:
            return

        self._cgroup_killer.kill(handle.cgroup_path)
        self._force_kill_reasons[handle.container_id] = reason
        self._emit_event(
            ContainerForceKilled(
                event_id=uuid4(),
                occurred_at=self._clock(),
                correlation_id=handle.correlation_id,
                container_id=handle.container_id,
                reason=reason,
            )
        )

    def _record_exit(
        self,
        handle: ContainerHandle,
        wait_outcome: PidfdWaitOutcome,
    ) -> ContainerExitStatus:
        """Finalize one container exit exactly once.

        The method centralizes all terminal side effects:
            1. compute duration from the persisted ``started_at`` timestamp
            2. query cgroup memory events for OOM attribution
            3. emit the cause-specific ``ContainerOOMKilled`` event when needed
            4. emit the generic ``ContainerExited`` event
            5. remove the running-handle record and cache the terminal status

        Caching matters because ``waitid`` consumes the exit state. Once one
        caller has reaped the pidfd, later callers must read the cached result
        instead of attempting a second wait.
        """

        cached_status = self._completed_statuses.get(handle.container_id)
        if cached_status is not None:
            return cached_status

        occurred_at = _require_aware_datetime("occurred_at", self._clock())
        was_oom_killed = self._oom_observer.was_oom_killed(handle)
        duration_seconds = max(0.0, (occurred_at - handle.started_at).total_seconds())

        exit_status = ContainerExitStatus(
            container_id=handle.container_id,
            exit_code=wait_outcome.exit_code,
            signal=wait_outcome.signal,
            duration_seconds=duration_seconds,
            was_oom_killed=was_oom_killed,
            occurred_at=occurred_at,
        )

        if was_oom_killed:
            self._emit_event(
                ContainerOOMKilled(
                    event_id=uuid4(),
                    occurred_at=occurred_at,
                    correlation_id=handle.correlation_id,
                    container_id=handle.container_id,
                    memory_limit=handle.memory_limit_bytes,
                )
            )

        self._emit_event(
            ContainerExited(
                event_id=uuid4(),
                occurred_at=occurred_at,
                correlation_id=handle.correlation_id,
                container_id=handle.container_id,
                exit_code=wait_outcome.exit_code,
                signal=wait_outcome.signal,
                duration_seconds=duration_seconds,
            )
        )

        self._completed_statuses[handle.container_id] = exit_status
        self._registry.delete(handle.container_id)
        self._force_kill_reasons.pop(handle.container_id, None)
        self._close_pidfd_safely(handle.pidfd)
        return exit_status

    def _close_pidfd_safely(self, pidfd: int) -> None:
        """Best-effort pidfd close after exit has been reaped.

        Closing the pidfd after reaping avoids descriptor leaks in long-lived
        supervisors. The close is best-effort because tests may use synthetic fd
        numbers and some embeddings may already have closed the descriptor.
        """

        try:
            self._pidfd_closer(pidfd)
        except OSError:
            return

    def _emit_event(self, event: LifecycleEvent) -> None:
        """Append one immutable domain event to the internal buffer."""

        self._pending_events.append(event)


__all__ = [
    "CgroupKiller",
    "ContainerExitStatus",
    "ContainerHandle",
    "ContainerRegistry",
    "ContainerSupervisor",
    "InMemoryContainerRegistry",
    "LifecycleEvent",
    "LinuxCgroupKiller",
    "LinuxPidfdSignaler",
    "NullOomObserver",
    "OomObserver",
    "PidfdOpener",
    "PidfdProcessIdResolver",
    "PidfdSignaler",
    "PidfdWaitOutcome",
    "PidfdWaiter",
    "ProcfsPidfdProcessIdResolver",
    "PythonPidfdOpener",
    "RunSpecSerializer",
    "WaitIdPidfdWaiter",
]
