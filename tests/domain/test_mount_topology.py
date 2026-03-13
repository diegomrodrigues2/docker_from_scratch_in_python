from __future__ import annotations

import pytest

from runspec_contract.domain import (
    AbsolutePath,
    MountSpec,
    MountTopology,
    MountType,
    Propagation,
    ValidationError,
)


def test_mount_spec_defaults_to_rprivate_for_bind_mounts() -> None:
    mount_spec = MountSpec(
        mount_type=MountType.BIND,
        destination=AbsolutePath("/data"),
        source="/host/data",
    )

    assert mount_spec.propagation is Propagation.RPRIVATE


def test_mount_spec_accepts_tmpfs_without_source() -> None:
    mount_spec = MountSpec(
        mount_type=MountType.TMPFS,
        destination=AbsolutePath("/tmp"),
        source=None,
        propagation=Propagation.RSLAVE,
    )

    assert mount_spec.mount_type is MountType.TMPFS
    assert mount_spec.propagation is Propagation.RSLAVE


def test_mount_spec_rejects_bind_without_source() -> None:
    with pytest.raises(ValidationError, match="bind mounts require a source path"):
        MountSpec(
            mount_type=MountType.BIND,
            destination=AbsolutePath("/data"),
            source=None,
        )


def test_mount_spec_rejects_tmpfs_with_source() -> None:
    with pytest.raises(ValidationError, match="tmpfs mounts must not define source"):
        MountSpec(
            mount_type=MountType.TMPFS,
            destination=AbsolutePath("/tmp"),
            source="/host/tmp",
        )


def test_mount_topology_preserves_mount_order() -> None:
    first_mount = MountSpec(
        mount_type=MountType.BIND,
        destination=AbsolutePath("/etc/hosts"),
        source="/host/hosts",
    )
    second_mount = MountSpec(
        mount_type=MountType.TMPFS,
        destination=AbsolutePath("/tmp"),
        source=None,
    )

    topology = MountTopology(mounts=(first_mount, second_mount))

    assert topology.mounts == (first_mount, second_mount)


def test_mount_topology_rejects_duplicate_destinations() -> None:
    first_mount = MountSpec(
        mount_type=MountType.BIND,
        destination=AbsolutePath("/data"),
        source="/host/first",
    )
    second_mount = MountSpec(
        mount_type=MountType.BIND,
        destination=AbsolutePath("/data"),
        source="/host/second",
    )

    with pytest.raises(ValidationError, match="duplicate mount destination '/data'"):
        MountTopology(mounts=(first_mount, second_mount))
