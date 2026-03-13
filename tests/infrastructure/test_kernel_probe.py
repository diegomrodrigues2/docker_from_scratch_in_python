from __future__ import annotations

from pathlib import Path

import pytest

from runspec_contract.domain import DomainError, KernelFeatureError
from runspec_contract.infrastructure import KernelProbe


def _linux_probe_fixture(monkeypatch: pytest.MonkeyPatch) -> KernelProbe:
    probe = KernelProbe()
    controllers_path = Path("/sys/fs/cgroup/cgroup.controllers")
    namespace_root = Path("/proc/self/ns")
    version_path = Path("/proc/version")

    existing_paths = {
        controllers_path,
        namespace_root / "user",
        namespace_root / "pid",
        namespace_root / "mnt",
        namespace_root / "uts",
        namespace_root / "net",
    }
    file_contents = {
        version_path: "Linux version 6.8.12-custom #1 SMP PREEMPT_DYNAMIC",
        controllers_path: "cpu memory pids io",
    }

    monkeypatch.setattr(probe, "_path_exists", lambda path: path in existing_paths)
    monkeypatch.setattr(probe, "_read_text", lambda path: file_contents.get(path))
    monkeypatch.setattr(probe, "_probe_clone3", lambda: True)
    monkeypatch.setattr(probe, "_probe_pidfd", lambda: True)
    monkeypatch.setattr(probe, "_probe_seccomp", lambda: True)
    monkeypatch.setattr(probe, "_probe_pivot_root", lambda: True)
    return probe


def test_kernel_probe_returns_valid_cached_capabilities_when_requirements_are_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _linux_probe_fixture(monkeypatch)

    first = probe.probe()
    second = probe.probe()

    assert first is second
    assert first.kernel_version == (6, 8, 12)
    assert first.has_cgroups_v2 is True
    assert first.has_clone3 is True
    assert first.has_pidfd is True
    assert first.has_user_ns is True
    assert first.has_pid_ns is True
    assert first.has_mount_ns is True
    assert first.has_uts_ns is True
    assert first.has_net_ns is True
    assert first.has_seccomp is True
    assert first.has_pivot_root is True
    assert first.available_cgroup_controllers == frozenset({"cpu", "memory", "pids", "io"})
    first.validate_minimum_requirements()


def test_kernel_probe_detects_kernel_version_below_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _linux_probe_fixture(monkeypatch)
    monkeypatch.setattr(probe, "_read_text", lambda path: "Linux version 5.2.18-custom")

    capabilities = probe.probe()

    with pytest.raises(KernelFeatureError) as exc_info:
        capabilities.validate_minimum_requirements()

    assert exc_info.value.feature == "kernel_version"
    assert "5.3.0" in exc_info.value.detail


def test_kernel_probe_detects_missing_cgroups_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _linux_probe_fixture(monkeypatch)
    monkeypatch.setattr(
        probe,
        "_path_exists",
        lambda path: path
        in {
            Path("/proc/self/ns/user"),
            Path("/proc/self/ns/pid"),
            Path("/proc/self/ns/mnt"),
            Path("/proc/self/ns/uts"),
            Path("/proc/self/ns/net"),
        },
    )

    capabilities = probe.probe()

    with pytest.raises(KernelFeatureError) as exc_info:
        capabilities.validate_minimum_requirements()

    assert capabilities.has_cgroups_v2 is False
    assert exc_info.value.feature == "cgroups_v2"


@pytest.mark.parametrize(
    ("attribute_name", "feature_name"),
    (
        ("_probe_clone3", "clone3"),
        ("_probe_pidfd", "pidfd"),
        ("_probe_seccomp", "seccomp"),
        ("_probe_pivot_root", "pivot_root"),
    ),
)
def test_kernel_probe_detects_missing_required_kernel_feature(
    monkeypatch: pytest.MonkeyPatch,
    attribute_name: str,
    feature_name: str,
) -> None:
    probe = _linux_probe_fixture(monkeypatch)
    monkeypatch.setattr(probe, attribute_name, lambda: False)

    capabilities = probe.probe()

    with pytest.raises(DomainError) as exc_info:
        capabilities.validate_minimum_requirements()

    assert isinstance(exc_info.value, KernelFeatureError)
    assert exc_info.value.feature == feature_name
