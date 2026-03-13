"""Composition root for the RunSpec compilation and execution workflow.

This module wires together the application and infrastructure components that
were implemented separately in earlier tasks. It exists so callers do not need
to know the construction order or the dependency graph of the runtime.

Bootstrap flow:
    1. probe the host kernel once and validate the minimum platform contract
    2. instantiate the default admission policies and pipeline
    3. instantiate the concrete serializer and ACL bridge
    4. instantiate the container supervisor around the serializer and bridge
    5. expose a small high-level API for compile, execute, wait, and event
       draining

The explicit composition order matters because bootstrap orchestration is a
cross-cutting infrastructure concern. Keeping it in one didactic module makes
the final wiring auditable against task 13.1 and requirements 1.1, 14.1, and
15.1.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import UUID

from runspec_contract.application import (
    AdmissionPipeline,
    CgroupKiller,
    ContainerHandle,
    ContainerRegistry,
    ContainerSupervisor,
    DefaultPathNormalizationPolicy,
    DefaultPolicyEnrichmentPolicy,
    DefaultPrecedenceResolutionPolicy,
    OomObserver,
    PidfdOpener,
    PidfdProcessIdResolver,
    PidfdSignaler,
    PidfdWaiter,
    RawCompilationInputs,
    UserOverrides,
)
from runspec_contract.domain import (
    ContainerExited,
    ContainerForceKilled,
    ContainerOOMKilled,
    ContainerStarted,
    RunSpec,
    RunSpecCompiled,
)
from runspec_contract.infrastructure import CythonBridge, KernelProbe
from runspec_contract.infrastructure.kernel_probe import KernelCapabilities
from runspec_contract.infrastructure.serializer import RunSpecSerializer

RuntimeEvent = (
    RunSpecCompiled | ContainerStarted | ContainerExited | ContainerOOMKilled | ContainerForceKilled
)


class KernelProbePort(Protocol):
    """Protocol used so tests can inject fake kernel probes into bootstrap."""

    def probe(self) -> KernelCapabilities:
        """Probe the host and return immutable kernel capabilities."""


class RunSpecRuntime:
    """High-level facade over the fully wired RunSpec runtime.

    The runtime caches the validated kernel probe result so repeated
    compilations reuse the same host capability snapshot. It also centralizes
    event draining across the admission pipeline and the lifecycle supervisor,
    which lets integration tests and embedding code observe the full flow
    without reaching into individual components.
    """

    def __init__(
        self,
        *,
        kernel_capabilities: KernelCapabilities,
        pipeline: AdmissionPipeline,
        serializer: RunSpecSerializer,
        bridge: CythonBridge,
        supervisor: ContainerSupervisor,
    ) -> None:
        """Create the runtime facade from already-wired components."""

        self._kernel_capabilities = kernel_capabilities
        self._pipeline = pipeline
        self._serializer = serializer
        self._bridge = bridge
        self._supervisor = supervisor

    @property
    def kernel_capabilities(self) -> KernelCapabilities:
        """Expose the cached immutable kernel capability snapshot."""

        return self._kernel_capabilities

    @property
    def serializer(self) -> RunSpecSerializer:
        """Expose the concrete serializer used by the supervisor."""

        return self._serializer

    @property
    def bridge(self) -> CythonBridge:
        """Expose the wired ACL bridge for advanced embeddings."""

        return self._bridge

    def compile(self, raw_inputs: RawCompilationInputs) -> RunSpec:
        """Compile raw inputs into an immutable aggregate using cached host facts."""

        return self._pipeline.compile(raw_inputs, self._kernel_capabilities)

    def compile_and_execute(
        self,
        raw_inputs: RawCompilationInputs,
        rootfs_path: str,
    ) -> ContainerHandle:
        """Compile a contract and execute it against one explicit rootfs path.

        The method treats the explicit ``rootfs_path`` argument as the final
        runtime source of truth. It is injected into the highest-precedence
        layer before compilation so the compiled contract and the later bridge
        invocation cannot diverge.
        """

        effective_raw_inputs = self._override_rootfs_path(raw_inputs, rootfs_path)
        runspec = self.compile(effective_raw_inputs)
        return self._supervisor.start(runspec, rootfs_path)

    def wait(self, handle: ContainerHandle):
        """Wait for the container to exit through the wired supervisor."""

        return self._supervisor.wait(handle)

    def terminate(self, handle: ContainerHandle, timeout_seconds: float) -> None:
        """Attempt graceful termination and escalate if the timeout expires."""

        self._supervisor.terminate(handle, timeout_seconds)

    def force_kill(self, handle: ContainerHandle) -> None:
        """Force-kill the container cgroup immediately."""

        self._supervisor.force_kill(handle)

    def reattach(self, container_id: UUID) -> ContainerHandle:
        """Reattach to a running container through the supervisor."""

        return self._supervisor.reattach(container_id)

    def drain_events(self) -> tuple[RuntimeEvent, ...]:
        """Drain compilation and lifecycle events in causal order."""

        # Pipeline events always happen before supervisor events for one
        # compile/execute cycle, so the combined drain preserves that order.
        return self._pipeline.drain_events() + self._supervisor.drain_events()

    @staticmethod
    def _override_rootfs_path(
        raw_inputs: RawCompilationInputs,
        rootfs_path: str,
    ) -> RawCompilationInputs:
        """Copy raw inputs while forcing ``rootfs_path`` into the top layer.

        This helper avoids a subtle but important bootstrap bug: if callers pass
        ``rootfs_path`` separately to execution while the compilation layers
        still contain a different rootfs, the control plane would serialize one
        contract and attempt to execute another. Overriding the top layer makes
        the high-level API coherent by construction.
        """

        effective_user_overrides = replace(raw_inputs.user_overrides, rootfs_path=rootfs_path)
        if not isinstance(effective_user_overrides, UserOverrides):
            raise TypeError("raw_inputs.user_overrides must be a UserOverrides instance.")
        return replace(raw_inputs, user_overrides=effective_user_overrides)


def bootstrap_runtime(
    *,
    kernel_probe: KernelProbePort | None = None,
    native_executor: Callable[[bytes, str], int] | None = None,
    bridge_clock_ns: Callable[[], int] = time.monotonic_ns,
    bridge_logger: logging.Logger | None = None,
    bridge_error_details: Mapping[int, str] | None = None,
    registry: ContainerRegistry | None = None,
    pidfd_waiter: PidfdWaiter | None = None,
    pidfd_signaler: PidfdSignaler | None = None,
    cgroup_killer: CgroupKiller | None = None,
    oom_observer: OomObserver | None = None,
    pidfd_opener: PidfdOpener | None = None,
    pidfd_process_id_resolver: PidfdProcessIdResolver | None = None,
    cgroup_path_factory: Callable[[UUID], str] | None = None,
    clock: Callable[[], datetime] | None = None,
    pidfd_closer: Callable[[int], None] | None = None,
    sigterm_number: int = 15,
) -> RunSpecRuntime:
    """Build the default runtime graph and validate the host kernel up front.

    Args:
        kernel_probe: Optional probe implementation. Tests inject a fake probe
            here to avoid depending on the local host kernel.
        native_executor: Optional callable used by ``CythonBridge`` to reach the
            native data plane.
        bridge_clock_ns: Monotonic clock used for ACL latency measurement.
        bridge_logger: Logger used by the ACL bridge.
        bridge_error_details: Optional mapping for native error translation.
        registry: Optional container registry implementation.
        pidfd_waiter: Optional pidfd waiter implementation.
        pidfd_signaler: Optional pidfd signal sender.
        cgroup_killer: Optional cgroup force-kill adapter.
        oom_observer: Optional OOM attribution adapter.
        pidfd_opener: Optional pidfd reopen adapter for reattach.
        pidfd_process_id_resolver: Optional adapter that maps pidfd -> PID.
        cgroup_path_factory: Optional strategy for container cgroup paths.
        clock: Optional aware clock shared with the supervisor.
        pidfd_closer: Optional pidfd close function.
        sigterm_number: Signal number used for graceful termination.

    Returns:
        A fully wired ``RunSpecRuntime`` ready to compile and execute.
    """

    effective_kernel_probe = kernel_probe or KernelProbe()
    kernel_capabilities = effective_kernel_probe.probe()

    # The enrichment policy validates again on every compilation, but bootstrap
    # must fail fast as well so callers discover an unsupported host before they
    # even attempt to compile one RunSpec.
    kernel_capabilities.validate_minimum_requirements()

    pipeline = AdmissionPipeline(
        precedence=DefaultPrecedenceResolutionPolicy(),
        enrichment=DefaultPolicyEnrichmentPolicy(),
        normalization=DefaultPathNormalizationPolicy(),
    )
    serializer = RunSpecSerializer()
    bridge = CythonBridge(
        native_executor=native_executor,
        clock_ns=bridge_clock_ns,
        logger=bridge_logger,
        error_details=bridge_error_details,
    )
    supervisor_kwargs: dict[str, object] = {
        "sigterm_number": sigterm_number,
    }
    for argument_name, argument_value in (
        ("registry", registry),
        ("pidfd_waiter", pidfd_waiter),
        ("pidfd_signaler", pidfd_signaler),
        ("cgroup_killer", cgroup_killer),
        ("oom_observer", oom_observer),
        ("pidfd_opener", pidfd_opener),
        ("pidfd_process_id_resolver", pidfd_process_id_resolver),
        ("cgroup_path_factory", cgroup_path_factory),
        ("clock", clock),
        ("pidfd_closer", pidfd_closer),
    ):
        if argument_value is not None:
            supervisor_kwargs[argument_name] = argument_value

    supervisor = ContainerSupervisor(
        serializer=serializer,
        bridge=bridge,
        **supervisor_kwargs,
    )

    return RunSpecRuntime(
        kernel_capabilities=kernel_capabilities,
        pipeline=pipeline,
        serializer=serializer,
        bridge=bridge,
        supervisor=supervisor,
    )


def compile_and_execute(
    raw_inputs: RawCompilationInputs,
    rootfs_path: str,
    *,
    runtime: RunSpecRuntime | None = None,
) -> ContainerHandle:
    """High-level convenience API for one compile-plus-start operation.

    Repeated callers should prefer ``bootstrap_runtime()`` once and then reuse
    the returned runtime so kernel probing and dependency construction happen
    only one time. The convenience function still exists because task 13.1
    explicitly requests a direct high-level API surface.
    """

    effective_runtime = runtime or bootstrap_runtime()
    return effective_runtime.compile_and_execute(raw_inputs, rootfs_path)


__all__ = [
    "KernelProbePort",
    "RunSpecRuntime",
    "RuntimeEvent",
    "bootstrap_runtime",
    "compile_and_execute",
]
