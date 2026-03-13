from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from runspec_contract.application import (
    AdmissionPipeline,
    DefaultPathNormalizationPolicy,
    DefaultPolicyEnrichmentPolicy,
    DefaultPrecedenceResolutionPolicy,
    ImageMetadata,
    MountRequest,
    PlatformDefaults,
    PolicyConfig,
    RawCompilationInputs,
    UserOverrides,
)
from runspec_contract.domain import KernelFeatureError, MountType, ValidationError


@dataclass(frozen=True, slots=True)
class FakeKernelCapabilities:
    kernel_version: tuple[int, int, int] = (6, 8, 0)
    has_cgroups_v2: bool = True
    has_clone3: bool = True
    has_pidfd: bool = True
    has_user_ns: bool = True
    has_pid_ns: bool = True
    has_mount_ns: bool = True
    has_uts_ns: bool = True
    has_net_ns: bool = True
    has_seccomp: bool = True
    has_pivot_root: bool = True
    available_cgroup_controllers: frozenset[str] = frozenset({"cpu", "memory", "pids"})

    def validate_minimum_requirements(self) -> None:
        if self.kernel_version < (5, 3, 0):
            raise KernelFeatureError(
                "kernel_version",
                f"kernel version {self.kernel_version} is below the minimum 5.3.0",
            )
        if not self.has_cgroups_v2:
            raise KernelFeatureError(
                "cgroups_v2",
                "cgroups v2 is required to compile a RunSpec",
            )
        required_features = {
            "clone3": self.has_clone3,
            "pidfd": self.has_pidfd,
            "seccomp": self.has_seccomp,
            "pivot_root": self.has_pivot_root,
        }
        missing = [name for name, available in required_features.items() if not available]
        if missing:
            raise KernelFeatureError(
                "kernel_features",
                f"missing required kernel features: {', '.join(missing)}",
            )


def _build_pipeline() -> AdmissionPipeline:
    return AdmissionPipeline(
        precedence=DefaultPrecedenceResolutionPolicy(),
        enrichment=DefaultPolicyEnrichmentPolicy(),
        normalization=DefaultPathNormalizationPolicy(),
    )


def _build_raw_inputs() -> RawCompilationInputs:
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
        platform_defaults=PlatformDefaults(
            environment={
                "HOME": "/home/app",
                "PATH": "/usr/local/bin",
            },
            pids_max=64,
            rootfs_path="/var/lib/containers/demo/rootfs",
        ),
        policy_config=PolicyConfig(
            environment={"POLICY_MODE": "strict"},
            effective_caps=frozenset({"CAP_CHOWN"}),
            permitted_caps=frozenset({"CAP_CHOWN"}),
            bounding_caps=frozenset({"CAP_CHOWN"}),
            seccomp_profile="runtime/default",
        ),
        user_overrides=UserOverrides(
            cwd="/workspace/./service/../service",
            environment={"PATH": "/custom/bin"},
            mounts=(
                MountRequest(
                    mount_type=MountType.BIND,
                    destination="/etc/./hosts",
                    source="/host/config/../config/hosts",
                    readonly=True,
                ),
                MountRequest(
                    mount_type=MountType.TMPFS,
                    destination="/tmp/../tmp",
                    source=None,
                    options={"nodev": None, "size": "64m"},
                ),
            ),
        ),
        compiled_at=datetime(2026, 3, 13, 18, 0, tzinfo=timezone.utc),
        correlation_id=uuid4(),
    )


def test_admission_pipeline_compiles_runspec_end_to_end() -> None:
    pipeline = _build_pipeline()

    runspec = pipeline.compile(_build_raw_inputs(), FakeKernelCapabilities())

    assert runspec.process.argv == ("/usr/bin/python", "-m", "demo_app")
    assert runspec.process.cwd.value == "/var/lib/containers/demo/rootfs/workspace/service"
    assert runspec.rootfs_path.value == "/var/lib/containers/demo/rootfs"
    assert dict(runspec.environment.entries) == {
        "HOME": "/home/app",
        "IMAGE_ONLY": "yes",
        "PATH": "/custom/bin",
        "POLICY_MODE": "strict",
    }
    assert runspec.mounts.mounts[0].destination.value == "/var/lib/containers/demo/rootfs/etc/hosts"
    assert runspec.mounts.mounts[0].source == "/host/config/hosts"
    assert runspec.mounts.mounts[1].destination.value == "/var/lib/containers/demo/rootfs/tmp"
    assert runspec.resources.pids.max_pids == 64
    assert runspec.hostname.value == runspec.canonical_hash.hex()[:12]


def test_admission_pipeline_rejects_non_absolute_paths_and_emits_no_event() -> None:
    pipeline = _build_pipeline()
    invalid_inputs = RawCompilationInputs(
        image_metadata=ImageMetadata(
            argv=("/bin/sh",),
            uid=1000,
            gid=1000,
            cwd="/workspace",
            cpu_quota_us=10_000,
            cpu_period_us=20_000,
            memory_max_bytes=8 * 1024 * 1024,
        ),
        platform_defaults=PlatformDefaults(rootfs_path="/var/lib/containers/demo/rootfs"),
        policy_config=PolicyConfig(),
        user_overrides=UserOverrides(cwd="relative/path"),
        compiled_at=datetime(2026, 3, 13, 18, 30, tzinfo=timezone.utc),
        correlation_id=uuid4(),
    )

    with pytest.raises(ValidationError, match="path must be absolute"):
        pipeline.compile(invalid_inputs, FakeKernelCapabilities())

    assert pipeline.drain_events() == ()


def test_admission_pipeline_emits_runspec_compiled_event_after_success() -> None:
    pipeline = _build_pipeline()

    runspec = pipeline.compile(_build_raw_inputs(), FakeKernelCapabilities())
    emitted_events = pipeline.drain_events()

    assert len(emitted_events) == 1
    event = emitted_events[0]
    assert event.runspec_hash == runspec.canonical_hash
    assert event.occurred_at == runspec.compiled_at
    assert event.correlation_id == runspec.correlation_id
    assert pipeline.drain_events() == ()
