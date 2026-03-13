from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from runspec_contract.domain import (
    AbsolutePath,
    CpuLimit,
    EnvironmentMap,
    Hostname,
    MemoryLimit,
    MountSpec,
    MountTopology,
    MountType,
    PidsLimit,
    ProcessSpec,
    ResourceSpec,
    RunSpec,
    SeccompProfile,
    SecurityEnvelope,
    ValidationError,
)


def _build_runspec(process_cwd: AbsolutePath) -> RunSpec:
    return RunSpec(
        process=ProcessSpec(
            argv=("/usr/bin/python", "-m", "app"),
            uid=1000,
            gid=1000,
            cwd=process_cwd,
        ),
        environment=EnvironmentMap.compile_from_layers(
            {"HOME": "/root"},
            {"PATH": "/usr/local/bin"},
        ),
        mounts=MountTopology(
            mounts=(
                MountSpec(
                    mount_type=MountType.BIND,
                    destination=AbsolutePath("/var/lib/containers/demo/rootfs/etc/hosts"),
                    source="/etc/hosts",
                ),
            )
        ),
        hostname=Hostname("container-01"),
        resources=ResourceSpec(
            cpu=CpuLimit(quota_us=50_000, period_us=100_000),
            memory=MemoryLimit(max_bytes=8 * 1024 * 1024),
            pids=PidsLimit(max_pids=32),
        ),
        security=SecurityEnvelope(
            effective_caps=frozenset({"CAP_CHOWN"}),
            permitted_caps=frozenset({"CAP_CHOWN"}),
            bounding_caps=frozenset({"CAP_CHOWN"}),
            no_new_privs=True,
            seccomp_profile=SeccompProfile("runtime/default"),
        ),
        rootfs_path=AbsolutePath("/var/lib/containers/demo/rootfs"),
        canonical_hash=b"\x01\x02\x03\x04\x05\x06\x07\x08",
        compiled_at=datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc),
        correlation_id=uuid4(),
    )


def test_runspec_accepts_valid_aggregate() -> None:
    runspec = _build_runspec(
        process_cwd=AbsolutePath("/var/lib/containers/demo/rootfs/workspace"),
    )

    assert runspec.process.cwd == AbsolutePath("/var/lib/containers/demo/rootfs/workspace")
    assert runspec.rootfs_path == AbsolutePath("/var/lib/containers/demo/rootfs")
    assert runspec.canonical_hash == b"\x01\x02\x03\x04\x05\x06\x07\x08"


def test_runspec_rejects_cwd_outside_rootfs() -> None:
    with pytest.raises(ValidationError, match="must be contained inside rootfs"):
        _build_runspec(process_cwd=AbsolutePath("/var/lib/containers/other-rootfs/workspace"))
