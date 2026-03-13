from __future__ import annotations

import pytest

from runspec_contract.domain import (
    CpuLimit,
    MemoryLimit,
    MIN_MEMORY_BYTES,
    PidsLimit,
    ResourceSpec,
    ValidationError,
)


def test_resource_spec_accepts_valid_limits() -> None:
    resource_spec = ResourceSpec(
        cpu=CpuLimit(quota_us=50_000, period_us=100_000),
        memory=MemoryLimit(max_bytes=16 * 1024 * 1024),
        pids=PidsLimit(max_pids=32),
    )

    assert resource_spec.cpu.quota_us == 50_000
    assert resource_spec.cpu.period_us == 100_000
    assert resource_spec.memory.max_bytes == 16 * 1024 * 1024
    assert resource_spec.pids.max_pids == 32


def test_cpu_limit_rejects_quota_greater_than_period() -> None:
    with pytest.raises(ValidationError, match="less than or equal to cpu period"):
        CpuLimit(quota_us=200_000, period_us=100_000)


def test_memory_limit_rejects_values_below_four_mebibytes() -> None:
    with pytest.raises(ValidationError, match="at least 4194304 bytes"):
        MemoryLimit(max_bytes=MIN_MEMORY_BYTES - 1)


def test_pids_limit_rejects_values_lower_than_one() -> None:
    with pytest.raises(ValidationError, match="pid limit must be at least 1"):
        PidsLimit(max_pids=0)


def test_resource_spec_clamps_child_limits_to_parent_limits() -> None:
    child = ResourceSpec(
        cpu=CpuLimit(quota_us=80_000, period_us=100_000),
        memory=MemoryLimit(max_bytes=64 * 1024 * 1024),
        pids=PidsLimit(max_pids=64),
    )

    clamped = child.clamp_to_parent(
        parent_cpu_quota=40_000,
        parent_cpu_period=50_000,
        parent_memory_max=32 * 1024 * 1024,
        parent_pids_max=16,
    )

    assert clamped.cpu == CpuLimit(quota_us=40_000, period_us=50_000)
    assert clamped.memory == MemoryLimit(max_bytes=32 * 1024 * 1024)
    assert clamped.pids == PidsLimit(max_pids=16)
