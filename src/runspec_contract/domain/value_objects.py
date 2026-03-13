"""Domain value objects that compose the RunSpec aggregate."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from ._validation import (
    require_bool,
    require_instance,
    require_int,
    require_non_empty_string,
    require_non_negative_int,
    require_positive_int,
)
from .exceptions import ValidationError
from .types import AbsolutePath, MountOptions, SeccompProfile

MIN_MEMORY_BYTES = 4_194_304
MIN_PIDS = 1

_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """Typed description of the process executed inside the container.

    The command line stays immutable because its exact order and contents are
    part of the canonical execution contract exchanged with the data plane.
    """

    argv: tuple[str, ...]
    uid: int
    gid: int
    cwd: AbsolutePath

    def __post_init__(self) -> None:
        self._validate_argv()
        require_non_negative_int("ProcessSpec.uid", self.uid)
        require_non_negative_int("ProcessSpec.gid", self.gid)
        require_instance("ProcessSpec.cwd", self.cwd, AbsolutePath, "cwd must be an AbsolutePath")

    def _validate_argv(self) -> None:
        if not isinstance(self.argv, tuple):
            raise ValidationError("ProcessSpec.argv", "argv must be provided as a tuple")
        if not self.argv:
            raise ValidationError("ProcessSpec.argv", "argv must contain at least one element")
        for argument in self.argv:
            if not isinstance(argument, str):
                raise ValidationError("ProcessSpec.argv", "all argv elements must be strings")


@dataclass(frozen=True, slots=True)
class EnvironmentMap:
    """Deterministic environment variables compiled from precedence layers.

    Environment variables are stored in lexical order so hashing and binary
    serialization never depend on the order in which inputs arrived.
    """

    entries: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        self._validate_entries()

    def _validate_entries(self) -> None:
        if not isinstance(self.entries, tuple):
            raise ValidationError("EnvironmentMap.entries", "entries must be provided as a tuple")

        previous_key: str | None = None
        for index, entry in enumerate(self.entries):
            key, value = self._validate_entry_shape(entry, index)
            self._validate_key(key, index)
            self._validate_value(key, value)
            previous_key = self._validate_ordering(key, previous_key)

    @staticmethod
    def _validate_entry_shape(entry: object, index: int) -> tuple[str, str]:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValidationError(
                "EnvironmentMap.entries",
                f"entry at index {index} must be a (key, value) tuple",
            )
        key, value = entry
        return key, value

    @staticmethod
    def _validate_key(key: object, index: int) -> None:
        if not isinstance(key, str):
            raise ValidationError(
                "EnvironmentMap.entries",
                f"key at index {index} must be a string",
            )
        if not key:
            raise ValidationError(
                "EnvironmentMap.entries",
                "environment variable keys must not be empty",
            )
        if "=" in key:
            raise ValidationError(
                "EnvironmentMap.entries",
                f"environment variable key '{key}' must not contain '='",
            )

    @staticmethod
    def _validate_value(key: str, value: object) -> None:
        if not isinstance(value, str):
            raise ValidationError(
                "EnvironmentMap.entries",
                f"value for key '{key}' must be a string",
            )

    @staticmethod
    def _validate_ordering(key: str, previous_key: str | None) -> str:
        if previous_key is not None and key < previous_key:
            raise ValidationError(
                "EnvironmentMap.entries",
                "entries must be sorted lexicographically by key",
            )
        if previous_key == key:
            raise ValidationError(
                "EnvironmentMap.entries",
                f"duplicate environment variable key '{key}' detected",
            )
        return key

    @classmethod
    def compile_from_layers(cls, *layers: Mapping[str, str]) -> "EnvironmentMap":
        """Compile a deterministic map where later layers override earlier ones."""

        # Merge policy:
        # 1. Read layers from lowest precedence to highest precedence.
        # 2. Let later layers overwrite keys from earlier layers.
        # 3. Sort once at the end so the stored representation is canonical.
        merged_entries: dict[str, str] = {}
        for layer_index, layer in enumerate(layers):
            if not isinstance(layer, Mapping):
                raise ValidationError(
                    "EnvironmentMap.layers",
                    f"layer at index {layer_index} must implement Mapping[str, str]",
                )
            for key, value in layer.items():
                if not isinstance(key, str):
                    raise ValidationError(
                        "EnvironmentMap.layers",
                        f"key '{key}' in layer {layer_index} must be a string",
                    )
                if not isinstance(value, str):
                    raise ValidationError(
                        "EnvironmentMap.layers",
                        f"value for key '{key}' in layer {layer_index} must be a string",
                    )
                merged_entries[key] = value

        return cls(entries=tuple(sorted(merged_entries.items(), key=lambda item: item[0])))


class MountType(str, Enum):
    """Supported mount operations exposed by the control plane."""

    BIND = "bind"
    TMPFS = "tmpfs"


class Propagation(str, Enum):
    """Supported propagation modes for individual mount specs."""

    RPRIVATE = "rprivate"
    RSLAVE = "rslave"


@dataclass(frozen=True, slots=True)
class MountSpec:
    """Single VFS transformation applied while building the container rootfs.

    Bind mounts and tmpfs mounts share a common shape, but they obey different
    source rules. The value object keeps those rules together so callers cannot
    accidentally build an impossible mount request.
    """

    mount_type: MountType
    destination: AbsolutePath
    source: str | None
    propagation: Propagation = Propagation.RPRIVATE
    readonly: bool = False
    options: MountOptions = field(default_factory=MountOptions)

    def __post_init__(self) -> None:
        self._validate_structure()
        self._validate_source_contract()

    def _validate_structure(self) -> None:
        require_instance(
            "MountSpec.mount_type",
            self.mount_type,
            MountType,
            "mount_type must be a MountType",
        )
        require_instance(
            "MountSpec.destination",
            self.destination,
            AbsolutePath,
            "destination must be an AbsolutePath",
        )
        require_instance(
            "MountSpec.propagation",
            self.propagation,
            Propagation,
            "propagation must be a Propagation value",
        )
        require_bool("MountSpec.readonly", self.readonly)
        require_instance("MountSpec.options", self.options, MountOptions, "options must be MountOptions")

    def _validate_source_contract(self) -> None:
        if self.mount_type is MountType.BIND:
            if self.source is None:
                raise ValidationError("MountSpec.source", "bind mounts require a source path")
            require_non_empty_string("MountSpec.source", self.source)
            return

        if self.mount_type is MountType.TMPFS and self.source is not None:
            raise ValidationError("MountSpec.source", "tmpfs mounts must not define source")


@dataclass(frozen=True, slots=True)
class MountTopology:
    """Ordered mount graph used during rootfs assembly.

    Mount order is semantically relevant during bootstrap, so the topology keeps
    insertion order while still rejecting duplicate destinations.
    """

    mounts: tuple[MountSpec, ...] = ()

    def __post_init__(self) -> None:
        self._validate_mounts()

    def _validate_mounts(self) -> None:
        if not isinstance(self.mounts, tuple):
            raise ValidationError("MountTopology.mounts", "mounts must be provided as a tuple")

        seen_destinations: set[AbsolutePath] = set()
        for index, mount in enumerate(self.mounts):
            if not isinstance(mount, MountSpec):
                raise ValidationError(
                    "MountTopology.mounts",
                    f"mount at index {index} must be a MountSpec",
                )
            if mount.destination in seen_destinations:
                raise ValidationError(
                    "MountTopology.mounts",
                    f"duplicate mount destination '{mount.destination}' detected",
                )
            seen_destinations.add(mount.destination)


@dataclass(frozen=True, slots=True)
class Hostname:
    """POSIX-compliant hostname stored in the UTS namespace."""

    value: str

    def __post_init__(self) -> None:
        text_value = require_non_empty_string("Hostname.value", self.value)
        if len(text_value) > 64:
            raise ValidationError("Hostname.value", "hostname must be at most 64 characters")
        if not _HOSTNAME_PATTERN.fullmatch(text_value):
            raise ValidationError(
                "Hostname.value",
                "hostname must use only [a-zA-Z0-9-] and cannot start or end with '-'",
            )

    @classmethod
    def from_hash(cls, canonical_hash: bytes) -> "Hostname":
        """Derive a deterministic hostname from the first 12 hex characters of a hash."""

        if not isinstance(canonical_hash, bytes):
            raise ValidationError("Hostname.canonical_hash", "canonical_hash must be bytes")
        if len(canonical_hash) < 6:
            raise ValidationError(
                "Hostname.canonical_hash",
                "canonical_hash must contain at least 6 bytes to derive 12 hex characters",
            )
        return cls(value=canonical_hash.hex()[:12])


@dataclass(frozen=True, slots=True)
class CpuLimit:
    """CPU cgroup v2 limits represented as quota/period in microseconds."""

    quota_us: int
    period_us: int

    def __post_init__(self) -> None:
        quota_us = require_positive_int("CpuLimit.quota_us", self.quota_us)
        period_us = require_positive_int("CpuLimit.period_us", self.period_us)
        if quota_us > period_us:
            raise ValidationError(
                "CpuLimit.quota_us",
                "cpu quota must be less than or equal to cpu period",
            )


@dataclass(frozen=True, slots=True)
class MemoryLimit:
    """Memory cgroup v2 limit in bytes."""

    max_bytes: int

    def __post_init__(self) -> None:
        max_bytes = require_int("MemoryLimit.max_bytes", self.max_bytes)
        if max_bytes < MIN_MEMORY_BYTES:
            raise ValidationError(
                "MemoryLimit.max_bytes",
                f"memory limit must be at least {MIN_MEMORY_BYTES} bytes (4 MiB)",
            )


@dataclass(frozen=True, slots=True)
class PidsLimit:
    """PID cgroup v2 limit protecting the host from fork bombs."""

    max_pids: int

    def __post_init__(self) -> None:
        max_pids = require_int("PidsLimit.max_pids", self.max_pids)
        if max_pids < MIN_PIDS:
            raise ValidationError(
                "PidsLimit.max_pids",
                f"pid limit must be at least {MIN_PIDS}",
            )


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """Aggregate of cgroup v2 CPU, memory, and PID limits.

    This value object groups the three resource controllers that the bootstrap
    pipeline must keep consistent when it creates a child cgroup.
    """

    cpu: CpuLimit
    memory: MemoryLimit
    pids: PidsLimit

    def __post_init__(self) -> None:
        require_instance("ResourceSpec.cpu", self.cpu, CpuLimit, "cpu must be a CpuLimit")
        require_instance(
            "ResourceSpec.memory",
            self.memory,
            MemoryLimit,
            "memory must be a MemoryLimit",
        )
        require_instance("ResourceSpec.pids", self.pids, PidsLimit, "pids must be a PidsLimit")

    def clamp_to_parent(
        self,
        parent_cpu_quota: int,
        parent_cpu_period: int,
        parent_memory_max: int,
        parent_pids_max: int,
    ) -> "ResourceSpec":
        """Clamp child limits so the resulting spec never exceeds parent envelopes."""

        # Clamp in two phases:
        # 1. Materialize the parent envelope through the same value objects used
        #    by children, so invalid parent limits fail fast as well.
        # 2. Take the minimum on each dimension so the child contract can never
        #    request more than the cgroup that contains it.
        parent_resources = self._build_parent_resources(
            parent_cpu_quota=parent_cpu_quota,
            parent_cpu_period=parent_cpu_period,
            parent_memory_max=parent_memory_max,
            parent_pids_max=parent_pids_max,
        )
        clamped_cpu = self._clamp_cpu_to_parent(parent_resources.cpu)

        return ResourceSpec(
            cpu=clamped_cpu,
            memory=MemoryLimit(
                max_bytes=min(self.memory.max_bytes, parent_resources.memory.max_bytes),
            ),
            pids=PidsLimit(max_pids=min(self.pids.max_pids, parent_resources.pids.max_pids)),
        )

    @staticmethod
    def _build_parent_resources(
        *,
        parent_cpu_quota: int,
        parent_cpu_period: int,
        parent_memory_max: int,
        parent_pids_max: int,
    ) -> "ResourceSpec":
        return ResourceSpec(
            cpu=CpuLimit(quota_us=parent_cpu_quota, period_us=parent_cpu_period),
            memory=MemoryLimit(max_bytes=parent_memory_max),
            pids=PidsLimit(max_pids=parent_pids_max),
        )

    def _clamp_cpu_to_parent(self, parent_cpu: CpuLimit) -> CpuLimit:
        clamped_period = min(self.cpu.period_us, parent_cpu.period_us)
        clamped_quota = min(self.cpu.quota_us, parent_cpu.quota_us, clamped_period)
        return CpuLimit(quota_us=clamped_quota, period_us=clamped_period)


@dataclass(frozen=True, slots=True)
class SecurityEnvelope:
    """Immutable privilege reduction contract applied before execve.

    The envelope models privilege as a monotonic narrowing operation:
    `effective_caps` must be a subset of `permitted_caps`, which must in turn be
    a subset of `bounding_caps`. That ordering mirrors the rules enforced by the
    kernel during container setup.
    """

    effective_caps: frozenset[str]
    permitted_caps: frozenset[str]
    bounding_caps: frozenset[str]
    no_new_privs: bool
    seccomp_profile: SeccompProfile | None = None
    lsm_profile: str | None = None

    def __post_init__(self) -> None:
        # Security validation is intentionally layered:
        # 1. Capability sets must be immutable and syntactically safe.
        # 2. Optional profile names must be safe to forward to kernel-facing code.
        # 3. The privilege hierarchy must only narrow, never expand.
        effective_caps = self._validate_capability_set(
            "SecurityEnvelope.effective_caps",
            self.effective_caps,
        )
        permitted_caps = self._validate_capability_set(
            "SecurityEnvelope.permitted_caps",
            self.permitted_caps,
        )
        bounding_caps = self._validate_capability_set(
            "SecurityEnvelope.bounding_caps",
            self.bounding_caps,
        )

        require_bool("SecurityEnvelope.no_new_privs", self.no_new_privs)
        self._validate_optional_profiles()
        self._validate_capability_hierarchy(
            effective_caps=effective_caps,
            permitted_caps=permitted_caps,
            bounding_caps=bounding_caps,
        )

    @staticmethod
    def _validate_capability_set(field_name: str, value: frozenset[str]) -> frozenset[str]:
        if not isinstance(value, frozenset):
            raise ValidationError(field_name, "capabilities must be provided as a frozenset")

        for capability in value:
            require_non_empty_string(field_name, capability)
            if capability != capability.strip():
                raise ValidationError(
                    field_name,
                    "capability names must not contain leading or trailing whitespace",
                )
            if "\x00" in capability:
                raise ValidationError(field_name, "capability names must not contain NUL bytes")

        return value

    def _validate_optional_profiles(self) -> None:
        if self.seccomp_profile is not None:
            require_instance(
                "SecurityEnvelope.seccomp_profile",
                self.seccomp_profile,
                SeccompProfile,
                "seccomp_profile must be a SeccompProfile or None",
            )
            if not self.no_new_privs:
                raise ValidationError(
                    "SecurityEnvelope.seccomp_profile",
                    "seccomp_profile requires no_new_privs=True",
                )

        if self.lsm_profile is not None:
            self._validate_lsm_profile(self.lsm_profile)

    @staticmethod
    def _validate_lsm_profile(lsm_profile: str) -> None:
        profile_value = require_non_empty_string("SecurityEnvelope.lsm_profile", lsm_profile)
        if profile_value != profile_value.strip():
            raise ValidationError(
                "SecurityEnvelope.lsm_profile",
                "lsm_profile must not contain leading or trailing whitespace",
            )
        if "\x00" in profile_value:
            raise ValidationError(
                "SecurityEnvelope.lsm_profile",
                "lsm_profile must not contain NUL bytes",
            )

    @staticmethod
    def _validate_capability_hierarchy(
        *,
        effective_caps: frozenset[str],
        permitted_caps: frozenset[str],
        bounding_caps: frozenset[str],
    ) -> None:
        missing_from_permitted = effective_caps - permitted_caps
        if missing_from_permitted:
            missing_caps = ", ".join(sorted(missing_from_permitted))
            raise ValidationError(
                "SecurityEnvelope.effective_caps",
                f"effective capabilities must be contained in permitted capabilities: {missing_caps}",
            )

        missing_from_bounding = permitted_caps - bounding_caps
        if missing_from_bounding:
            missing_caps = ", ".join(sorted(missing_from_bounding))
            raise ValidationError(
                "SecurityEnvelope.permitted_caps",
                f"permitted capabilities must be contained in bounding capabilities: {missing_caps}",
            )


__all__ = [
    "CpuLimit",
    "EnvironmentMap",
    "Hostname",
    "MIN_MEMORY_BYTES",
    "MIN_PIDS",
    "MountSpec",
    "MountTopology",
    "MountType",
    "PidsLimit",
    "ProcessSpec",
    "Propagation",
    "ResourceSpec",
    "SecurityEnvelope",
    "MemoryLimit",
]
