"""Kernel feature probing for control-plane bootstrap.

This module implements Requirement 15 from
``specs/run_spec/requirements.md`` and follows the ``KernelProbe`` section in
``specs/run_spec/design.md``.

Why this code exists:
    The admission pipeline can only compile a valid ``RunSpec`` when the host
    kernel exposes the primitives expected by the data plane. Probing these
    features once during bootstrap lets the control plane fail early with a
    descriptive domain error instead of letting a container fail much later.

Probe flow:
    1. Read the kernel version from ``/proc`` and parse it conservatively.
    2. Detect cgroups v2 by checking ``/sys/fs/cgroup/cgroup.controllers``.
    3. Detect namespace support by looking for entries under ``/proc/self/ns``.
    4. Detect syscall-backed features such as ``clone3`` and ``pivot_root`` by
       issuing a deliberately invalid syscall and interpreting the resulting
       ``errno``. An "invalid arguments" error means the syscall exists, which
       is enough for capability probing.

Design choice:
    The probe returns ``False`` for unsupported or non-Linux environments
    instead of raising infrastructure exceptions. That keeps the boundary
    deterministic: the caller always receives a ``KernelCapabilities`` snapshot
    and can then apply the domain-level validation rules explicitly.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from runspec_contract.domain import KernelFeatureError

MINIMUM_KERNEL_VERSION: Final[tuple[int, int, int]] = (5, 3, 0)
_PRCTL_GET_SECCOMP_OPERATION: Final[int] = 21
_LINUX_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"Linux version\s+(\d+)\.(\d+)(?:\.(\d+))?"
)
_GENERIC_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_SYSCALL_NUMBERS_BY_ARCHITECTURE: Final[dict[str, dict[str, int]]] = {
    "x86_64": {"clone3": 435, "pidfd_open": 434, "pivot_root": 155},
    "amd64": {"clone3": 435, "pidfd_open": 434, "pivot_root": 155},
    "aarch64": {"clone3": 435, "pidfd_open": 434, "pivot_root": 41},
    "arm64": {"clone3": 435, "pidfd_open": 434, "pivot_root": 41},
}


@dataclass(frozen=True, slots=True)
class KernelCapabilities:
    """Immutable snapshot of probed kernel support.

    Attributes:
        kernel_version: Parsed Linux version as ``(major, minor, patch)``.
        has_cgroups_v2: Whether the unified cgroup hierarchy is available.
        has_clone3: Whether the ``clone3`` syscall exists.
        has_pidfd: Whether pidfd APIs are available.
        has_user_ns: Whether user namespaces are visible through ``/proc``.
        has_pid_ns: Whether pid namespaces are visible through ``/proc``.
        has_mount_ns: Whether mount namespaces are visible through ``/proc``.
        has_uts_ns: Whether UTS namespaces are visible through ``/proc``.
        has_net_ns: Whether network namespaces are visible through ``/proc``.
        has_seccomp: Whether seccomp probing indicates kernel support.
        has_pivot_root: Whether the ``pivot_root`` syscall exists.
        available_cgroup_controllers: Controllers listed by cgroups v2.

    Invariants:
        - ``kernel_version`` is always a 3-item integer tuple.
        - Every feature flag is a boolean.
        - ``available_cgroup_controllers`` is normalized to a ``frozenset`` of
          non-empty strings so callers can rely on deterministic comparisons.
    """

    kernel_version: tuple[int, int, int]
    has_cgroups_v2: bool
    has_clone3: bool
    has_pidfd: bool
    has_user_ns: bool
    has_pid_ns: bool
    has_mount_ns: bool
    has_uts_ns: bool
    has_net_ns: bool
    has_seccomp: bool
    has_pivot_root: bool
    available_cgroup_controllers: frozenset[str]

    def __post_init__(self) -> None:
        """Validate and normalize the immutable snapshot.

        The goal here is to keep bad probe data out of the rest of the system.
        Even though ``KernelProbe`` builds this object internally, validating at
        the boundary makes the class safe to instantiate directly in tests.

        Raises:
            TypeError: If any field violates the declared immutable contract.
        """

        if (
            not isinstance(self.kernel_version, tuple)
            or len(self.kernel_version) != 3
            or any(
                isinstance(version_component, bool) or not isinstance(version_component, int)
                for version_component in self.kernel_version
            )
        ):
            raise TypeError("kernel_version must be a tuple[int, int, int].")

        for flag_field_name in (
            "has_cgroups_v2",
            "has_clone3",
            "has_pidfd",
            "has_user_ns",
            "has_pid_ns",
            "has_mount_ns",
            "has_uts_ns",
            "has_net_ns",
            "has_seccomp",
            "has_pivot_root",
        ):
            if not isinstance(getattr(self, flag_field_name), bool):
                raise TypeError(f"{flag_field_name} must be a boolean.")

        normalized_controller_names = frozenset(self.available_cgroup_controllers)
        if any(
            not isinstance(controller_name, str) or not controller_name.strip()
            for controller_name in normalized_controller_names
        ):
            raise TypeError("available_cgroup_controllers must contain non-empty strings.")

        object.__setattr__(
            self,
            "available_cgroup_controllers",
            normalized_controller_names,
        )

    def validate_minimum_requirements(self) -> None:
        """Validate the mandatory kernel prerequisites for the runtime.

        The design splits probing from validation on purpose:
            1. probing gathers facts about the host
            2. validation converts those facts into domain-level acceptance
               rules for the control plane

        Raises:
            KernelFeatureError: If the host kernel does not satisfy the minimum
                platform contract.
        """

        if self.kernel_version < MINIMUM_KERNEL_VERSION:
            detected_version = ".".join(str(component) for component in self.kernel_version)
            raise KernelFeatureError(
                "kernel_version",
                f"minimum supported version is 5.3.0; detected {detected_version}",
            )

        if not self.has_cgroups_v2:
            raise KernelFeatureError(
                "cgroups_v2",
                "cgroups v2 is unavailable; cgroups v1 is not supported",
            )

        required_feature_flags = {
            "clone3": self.has_clone3,
            "pidfd": self.has_pidfd,
            "seccomp": self.has_seccomp,
            "pivot_root": self.has_pivot_root,
        }
        missing_required_features = [
            feature_name
            for feature_name, is_available in required_feature_flags.items()
            if not is_available
        ]

        if len(missing_required_features) == 1:
            raise KernelFeatureError(
                missing_required_features[0],
                "required kernel feature is unavailable",
            )

        if missing_required_features:
            raise KernelFeatureError(
                "required_features",
                "missing required kernel features: "
                + ", ".join(missing_required_features),
            )


class KernelProbe:
    """Probe the local host for the Linux features required by the runtime.

    The object caches its result because kernel capabilities are effectively
    static for the lifetime of the process. Caching also keeps bootstrap cheap
    and prevents repeating syscall probes during every compilation request.
    """

    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        sys_root: str | Path = "/sys",
    ) -> None:
        """Build a probe with explicit filesystem roots.

        Args:
            proc_root: Root directory used for procfs lookups. Tests override
                this or monkeypatch helpers to simulate different hosts.
            sys_root: Root directory used for sysfs lookups.
        """

        self._proc_root = Path(proc_root)
        self._sys_root = Path(sys_root)
        self._cached_capabilities: KernelCapabilities | None = None
        self._libc = self._load_libc()

    def probe(self) -> KernelCapabilities:
        """Probe and cache the immutable kernel capability snapshot.

        Returns:
            A cached or newly constructed ``KernelCapabilities`` instance.
        """

        if self._cached_capabilities is not None:
            return self._cached_capabilities

        # The probing order mirrors the module docstring. Grouping the calls in
        # one place makes it obvious which facts end up in the immutable
        # snapshot exposed to the rest of the application.
        has_cgroups_v2, available_controller_names = self._probe_cgroups_v2()
        probed_capabilities = KernelCapabilities(
            kernel_version=self._probe_kernel_version(),
            has_cgroups_v2=has_cgroups_v2,
            has_clone3=self._probe_clone3(),
            has_pidfd=self._probe_pidfd(),
            has_user_ns=self._probe_namespace("user"),
            has_pid_ns=self._probe_namespace("pid"),
            has_mount_ns=self._probe_namespace("mnt"),
            has_uts_ns=self._probe_namespace("uts"),
            has_net_ns=self._probe_namespace("net"),
            has_seccomp=self._probe_seccomp(),
            has_pivot_root=self._probe_pivot_root(),
            available_cgroup_controllers=available_controller_names,
        )
        self._cached_capabilities = probed_capabilities
        return probed_capabilities

    @staticmethod
    def _load_libc() -> ctypes.CDLL | None:
        """Load ``libc`` when the current platform can support probing.

        Returns:
            The process C runtime handle on Linux, or ``None`` when the current
            environment cannot support the syscall-based probes.
        """

        if not KernelProbe._is_linux():
            return None
        try:
            return ctypes.CDLL(None, use_errno=True)
        except (OSError, TypeError):
            # Returning ``None`` keeps the rest of the module simple: every
            # syscall-based probe can treat "no libc" as "feature unavailable".
            return None

    @staticmethod
    def _parse_kernel_version(raw_text: str) -> tuple[int, int, int] | None:
        """Extract a kernel version from arbitrary release text.

        Args:
            raw_text: Raw text read from procfs or ``platform.release()``.

        Returns:
            A normalized ``(major, minor, patch)`` tuple when parsing succeeds,
            otherwise ``None``.
        """

        # ``/proc/version`` usually starts with ``Linux version X.Y.Z`` while
        # ``osrelease`` and ``platform.release()`` can contain distro suffixes
        # such as ``6.8.12-custom``. Trying both patterns keeps parsing robust
        # without depending on one exact vendor format.
        for version_pattern in (_LINUX_VERSION_PATTERN, _GENERIC_VERSION_PATTERN):
            match = version_pattern.search(raw_text)
            if match is not None:
                major = int(match.group(1))
                minor = int(match.group(2))
                patch = int(match.group(3) or 0)
                return (major, minor, patch)
        return None

    def _probe_kernel_version(self) -> tuple[int, int, int]:
        """Detect the running kernel version.

        Returns:
            The best-effort kernel version tuple. When every source fails, the
            method returns ``(0, 0, 0)`` so later validation fails explicitly.
        """

        # Prefer procfs because it reflects the real running kernel instead of
        # a Python abstraction. The fallback to ``platform.release()`` keeps the
        # method usable in tests and odd environments.
        for version_source in (
            self._proc_root / "version",
            self._proc_root / "sys" / "kernel" / "osrelease",
        ):
            source_text = self._read_text(version_source)
            if source_text is None:
                continue

            parsed_version = self._parse_kernel_version(source_text)
            if parsed_version is not None:
                return parsed_version

        fallback_version = self._parse_kernel_version(platform.release())
        if fallback_version is not None:
            return fallback_version
        return (0, 0, 0)

    def _probe_cgroups_v2(self) -> tuple[bool, frozenset[str]]:
        """Detect cgroups v2 and list available controllers.

        Returns:
            A tuple ``(has_cgroups_v2, controller_names)``.
        """

        controllers_file = self._sys_root / "fs" / "cgroup" / "cgroup.controllers"

        # This file exists on the unified hierarchy and lists controllers such
        # as ``cpu``, ``memory`` and ``pids``. Its absence is the clearest cheap
        # signal that the host is not exposing cgroups v2.
        if not self._path_exists(controllers_file):
            return False, frozenset()

        controller_names_text = self._read_text(controllers_file) or ""
        available_controller_names = frozenset(
            controller_name
            for controller_name in controller_names_text.split()
            if controller_name.strip()
        )
        return True, available_controller_names

    def _probe_namespace(self, namespace_name: str) -> bool:
        """Detect whether a namespace type is visible in procfs.

        Args:
            namespace_name: One of the namespace file names under
                ``/proc/self/ns`` such as ``user`` or ``net``.

        Returns:
            ``True`` when the namespace entry exists.
        """

        return self._path_exists(self._proc_root / "self" / "ns" / namespace_name)

    def _probe_clone3(self) -> bool:
        """Detect whether the ``clone3`` syscall exists.

        Returns:
            ``True`` when the kernel recognizes the syscall number.
        """

        # Passing a null pointer and zero-sized structure is intentionally
        # invalid. If the kernel returns ``EFAULT`` or ``EINVAL`` it already
        # proved the syscall exists; we are not trying to spawn a process here.
        return self._probe_syscall(
            "clone3",
            ctypes.c_void_p(),
            ctypes.c_size_t(0),
            supported_errors=frozenset(
                {
                    errno.EFAULT,
                    errno.EINVAL,
                    errno.E2BIG,
                    errno.EPERM,
                }
            ),
        )

    def _probe_pidfd(self) -> bool:
        """Detect pidfd support through Python or raw syscall fallback.

        Returns:
            ``True`` when pidfd support appears to exist.
        """

        if hasattr(os, "pidfd_open"):
            try:
                pid_file_descriptor = os.pidfd_open(os.getpid(), 0)
            except OSError as error:
                return error.errno != errno.ENOSYS

            os.close(pid_file_descriptor)
            return True

        return self._probe_syscall(
            "pidfd_open",
            os.getpid(),
            0,
            supported_errors=frozenset({errno.EPERM, errno.EINVAL, errno.ESRCH}),
            close_fd_on_success=True,
        )

    def _probe_seccomp(self) -> bool:
        """Detect seccomp support with ``prctl(PR_GET_SECCOMP)``.

        Returns:
            ``True`` when the kernel recognizes the seccomp query operation.
        """

        if not self._is_linux() or self._libc is None or not hasattr(self._libc, "prctl"):
            return False

        ctypes.set_errno(0)
        prctl_result = self._libc.prctl(_PRCTL_GET_SECCOMP_OPERATION, 0, 0, 0, 0)
        if prctl_result >= 0:
            return True

        # ``EINVAL`` and ``ENOSYS`` mean the operation is not supported. Other
        # errors still prove the kernel understood the request and reached the
        # seccomp code path.
        return ctypes.get_errno() not in {errno.EINVAL, errno.ENOSYS}

    def _probe_pivot_root(self) -> bool:
        """Detect whether the ``pivot_root`` syscall exists.

        Returns:
            ``True`` when the kernel recognizes the syscall number.
        """

        # As with ``clone3``, the arguments are intentionally invalid. We only
        # need proof that the syscall exists, not a successful VFS transition.
        return self._probe_syscall(
            "pivot_root",
            ctypes.c_char_p(),
            ctypes.c_char_p(),
            supported_errors=frozenset(
                {
                    errno.EFAULT,
                    errno.EINVAL,
                    errno.ENOENT,
                    errno.EPERM,
                }
            ),
        )

    @staticmethod
    def _is_linux() -> bool:
        """Return whether the current process is running on Linux."""

        return os.name == "posix" and platform.system() == "Linux"

    def _probe_syscall(
        self,
        syscall_name: str,
        *args: object,
        supported_errors: frozenset[int],
        close_fd_on_success: bool = False,
    ) -> bool:
        """Probe a syscall by number and interpret the result conservatively.

        Args:
            syscall_name: Human-readable syscall identifier such as ``clone3``.
            *args: Raw arguments forwarded to ``libc.syscall``.
            supported_errors: ``errno`` values that count as "syscall exists but
                this probe used invalid or unauthorized arguments".
            close_fd_on_success: Whether the syscall returns a file descriptor
                that must be closed on success.

        Returns:
            ``True`` when the kernel recognized the syscall.
        """

        syscall_number = self._syscall_number(syscall_name)
        if syscall_number is None or not self._is_linux() or self._libc is None:
            return False

        if not hasattr(self._libc, "syscall"):
            return False

        ctypes.set_errno(0)
        syscall_result = self._libc.syscall(syscall_number, *args)
        if syscall_result >= 0:
            if close_fd_on_success:
                os.close(syscall_result)
            return True

        # ``ENOSYS`` means "syscall not implemented". Other specific errors
        # such as ``EINVAL`` or ``EFAULT`` are positive evidence that the kernel
        # recognized the syscall and rejected only the test arguments.
        return ctypes.get_errno() in supported_errors

    def _syscall_number(self, syscall_name: str) -> int | None:
        """Resolve an architecture-specific syscall number.

        Args:
            syscall_name: Logical syscall identifier.

        Returns:
            The integer syscall number for the current architecture, or
            ``None`` when this module does not know the mapping.
        """

        architecture_name = platform.machine().lower()
        return _SYSCALL_NUMBERS_BY_ARCHITECTURE.get(architecture_name, {}).get(syscall_name)

    @staticmethod
    def _read_text(path: Path) -> str | None:
        """Read a text file defensively.

        Args:
            path: Filesystem path to read.

        Returns:
            The decoded file content, or ``None`` when the path cannot be read.
        """

        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

    @staticmethod
    def _path_exists(path: Path) -> bool:
        """Check path existence defensively.

        Args:
            path: Filesystem path to inspect.

        Returns:
            ``True`` when the path exists and the check itself succeeded.
        """

        try:
            return path.exists()
        except OSError:
            return False


__all__ = [
    "KernelCapabilities",
    "KernelProbe",
    "MINIMUM_KERNEL_VERSION",
]
