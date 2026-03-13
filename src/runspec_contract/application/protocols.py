"""Protocols and immutable data carriers used by the admission pipeline.

This module implements the data contracts described in the "Pipeline de
Admissao" section of ``specs/run_spec/design.md``. The goal is to make the
compilation flow readable in stages:

1. raw inputs arrive from image metadata, platform defaults, policy injection,
   and user overrides
2. precedence resolution chooses one effective value for each field
3. policy enrichment fills mandatory defaults and validates kernel-dependent
   constraints
4. path normalization converts lexical paths into canonical absolute paths
5. the domain factory materializes immutable value objects and the aggregate

Requirements covered here include the contract shape needed for requirements
1.2 and 4.1.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from runspec_contract.domain.value_objects import MountType, Propagation


def _freeze_mapping(mapping: Mapping[str, str]) -> Mapping[str, str]:
    """Store string mappings in deterministic key order and make them read-only.

    Args:
        mapping: Arbitrary mapping received from one pipeline stage.

    Returns:
        A read-only mapping sorted by key so later hashing and comparisons stay
        deterministic.
    """

    return MappingProxyType(dict(sorted(mapping.items(), key=lambda item: item[0])))


def _freeze_optional_mapping(
    mapping: Mapping[str, str | None],
) -> Mapping[str, str | None]:
    """Freeze mount options so later stages cannot mutate them accidentally.

    Args:
        mapping: Mount option mapping where values may be ``None`` for flag-like
            options.

    Returns:
        A deterministic read-only mapping.
    """

    return MappingProxyType(dict(sorted(mapping.items(), key=lambda item: item[0])))


def _freeze_optional_capabilities(
    capabilities: Iterable[str] | None,
) -> frozenset[str] | None:
    """Normalize optional capability collections to frozensets for stable hashing.

    Args:
        capabilities: Optional iterable of Linux capability names.

    Returns:
        ``None`` when the caller omitted the field, otherwise a ``frozenset``
        suitable for immutable pipeline snapshots.
    """

    if capabilities is None:
        return None
    return frozenset(capabilities)


@dataclass(frozen=True, slots=True)
class MountRequest:
    """Primitive mount description used before domain value objects are created.

    The design distinguishes between the application pipeline and the domain
    factory. At the application level we still carry plain strings because path
    normalization has not happened yet.

    Attributes:
        mount_type: Mount operation requested by the caller.
        destination: Container-visible destination before normalization.
        source: Host source path for bind mounts. ``None`` for tmpfs.
        propagation: Mount propagation mode forwarded to the domain layer.
        readonly: Whether the mount is read-only.
        options: Free-form mount options that will later become ``MountOptions``.
    """

    mount_type: MountType
    destination: str
    source: str | None
    propagation: Propagation = Propagation.RPRIVATE
    readonly: bool = False
    options: Mapping[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # This dataclass intentionally does not replicate all domain validation.
        # Its job is narrower: keep application-stage snapshots immutable and
        # deterministic until the factory upgrades them to value objects.
        object.__setattr__(self, "options", _freeze_optional_mapping(self.options))


@dataclass(frozen=True, slots=True)
class CompilationLayer:
    """Common shape shared by every precedence layer.

    This dataclass keeps all precedence layers structurally identical, which
    makes the merge policy explicit and testable.

    Merge semantics used later by ``DefaultPrecedenceResolutionPolicy``:
    - scalar fields use "last non-None value wins"
    - environment variables merge per key
    - mounts replace the whole sequence when explicitly provided

    Attributes:
        argv: Command line to execute inside the container.
        uid: Numeric UID already resolved by higher layers.
        gid: Numeric GID already resolved by higher layers.
        cwd: Working directory before normalization.
        environment: Environment variables contributed by this layer.
        mounts: Optional full mount list contributed by this layer.
        hostname: Explicit hostname override, if any.
        cpu_quota_us: Desired CPU quota in microseconds.
        cpu_period_us: Desired CPU period in microseconds.
        memory_max_bytes: Desired memory ceiling.
        pids_max: Desired PID limit.
        effective_caps: Requested effective capability set.
        permitted_caps: Requested permitted capability set.
        bounding_caps: Requested bounding capability set.
        no_new_privs: Explicit PR_SET_NO_NEW_PRIVS value.
        seccomp_profile: Requested seccomp profile identifier.
        lsm_profile: Requested LSM profile identifier.
        rootfs_path: Host path to the prepared root filesystem.
    """

    argv: tuple[str, ...] | None = None
    uid: int | None = None
    gid: int | None = None
    cwd: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[MountRequest, ...] | None = None
    hostname: str | None = None
    cpu_quota_us: int | None = None
    cpu_period_us: int | None = None
    memory_max_bytes: int | None = None
    pids_max: int | None = None
    effective_caps: frozenset[str] | None = None
    permitted_caps: frozenset[str] | None = None
    bounding_caps: frozenset[str] | None = None
    no_new_privs: bool | None = None
    seccomp_profile: str | None = None
    lsm_profile: str | None = None
    rootfs_path: str | None = None

    def __post_init__(self) -> None:
        # We canonicalize collection-shaped fields here so every stage that
        # copies a layer inherits deterministic ordering for free.
        object.__setattr__(self, "environment", _freeze_mapping(self.environment))
        if self.argv is not None:
            object.__setattr__(self, "argv", tuple(self.argv))
        if self.mounts is not None:
            object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(
            self,
            "effective_caps",
            _freeze_optional_capabilities(self.effective_caps),
        )
        object.__setattr__(
            self,
            "permitted_caps",
            _freeze_optional_capabilities(self.permitted_caps),
        )
        object.__setattr__(
            self,
            "bounding_caps",
            _freeze_optional_capabilities(self.bounding_caps),
        )


class ImageMetadata(CompilationLayer):
    """Lowest-precedence inputs extracted from the image configuration."""


class PlatformDefaults(CompilationLayer):
    """Host-level defaults that apply to every container when not overridden."""


class PolicyConfig(CompilationLayer):
    """Security and governance policies injected by the control plane."""


class UserOverrides(CompilationLayer):
    """Highest-precedence inputs coming from CLI or API calls."""


@dataclass(frozen=True, slots=True)
class RawCompilationInputs:
    """Bundle of all raw pipeline inputs plus compilation metadata.

    Attributes:
        image_metadata: Lowest-precedence OCI or image-level defaults.
        platform_defaults: Host/platform defaults shared by containers.
        policy_config: Policy-injected values added by the control plane.
        user_overrides: Highest-precedence API or CLI overrides.
        compiled_at: Timestamp that will be forwarded into the resulting
            ``RunSpec`` and ``RunSpecCompiled`` event.
        correlation_id: Trace identifier shared by the compilation flow.
    """

    image_metadata: ImageMetadata
    platform_defaults: PlatformDefaults
    policy_config: PolicyConfig
    user_overrides: UserOverrides
    compiled_at: datetime
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class ResolvedInputs:
    """Snapshot produced after precedence resolution.

    At this point requirement 1.2 has already been enforced: every field has
    been chosen from the fixed layer order. However, some fields are still
    allowed to be incomplete because the next stage may inject mandatory policy
    defaults.
    """

    argv: tuple[str, ...]
    uid: int
    gid: int
    cwd: str
    environment: Mapping[str, str]
    mounts: tuple[MountRequest, ...]
    hostname: str | None
    cpu_quota_us: int
    cpu_period_us: int
    memory_max_bytes: int
    pids_max: int | None
    effective_caps: frozenset[str] | None
    permitted_caps: frozenset[str] | None
    bounding_caps: frozenset[str] | None
    no_new_privs: bool | None
    seccomp_profile: str | None
    lsm_profile: str | None
    rootfs_path: str
    compiled_at: datetime
    correlation_id: UUID

    def __post_init__(self) -> None:
        # The pipeline snapshots are intentionally frozen after construction.
        # That makes stage boundaries explicit and prevents accidental mutation
        # across policy implementations.
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "environment", _freeze_mapping(self.environment))
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(
            self,
            "effective_caps",
            _freeze_optional_capabilities(self.effective_caps),
        )
        object.__setattr__(
            self,
            "permitted_caps",
            _freeze_optional_capabilities(self.permitted_caps),
        )
        object.__setattr__(
            self,
            "bounding_caps",
            _freeze_optional_capabilities(self.bounding_caps),
        )


@dataclass(frozen=True, slots=True)
class EnrichedInputs:
    """Snapshot after policy enrichment has filled mandatory defaults.

    Compared with ``ResolvedInputs``, the key difference is that mandatory
    policy-driven fields are now guaranteed to be present. For example,
    ``pids_max`` and the capability hierarchy are fully materialized.
    """

    argv: tuple[str, ...]
    uid: int
    gid: int
    cwd: str
    environment: Mapping[str, str]
    mounts: tuple[MountRequest, ...]
    hostname: str | None
    cpu_quota_us: int
    cpu_period_us: int
    memory_max_bytes: int
    pids_max: int
    effective_caps: frozenset[str]
    permitted_caps: frozenset[str]
    bounding_caps: frozenset[str]
    no_new_privs: bool
    seccomp_profile: str | None
    lsm_profile: str | None
    rootfs_path: str
    compiled_at: datetime
    correlation_id: UUID

    def __post_init__(self) -> None:
        # Enrichment is the point where optional security collections become
        # mandatory, so we freeze them as concrete frozensets here.
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "environment", _freeze_mapping(self.environment))
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "effective_caps", frozenset(self.effective_caps))
        object.__setattr__(self, "permitted_caps", frozenset(self.permitted_caps))
        object.__setattr__(self, "bounding_caps", frozenset(self.bounding_caps))


@dataclass(frozen=True, slots=True)
class NormalizedInputs:
    """Snapshot consumed by the factory after all path rules are normalized.

    Requirement 3.6 and 5.5 are enforced before the domain factory is called:
    the path-carrying fields are now canonical absolute POSIX paths that can be
    upgraded directly into ``AbsolutePath`` value objects.
    """

    argv: tuple[str, ...]
    uid: int
    gid: int
    cwd: str
    environment: Mapping[str, str]
    mounts: tuple[MountRequest, ...]
    hostname: str | None
    cpu_quota_us: int
    cpu_period_us: int
    memory_max_bytes: int
    pids_max: int
    effective_caps: frozenset[str]
    permitted_caps: frozenset[str]
    bounding_caps: frozenset[str]
    no_new_privs: bool
    seccomp_profile: str | None
    lsm_profile: str | None
    rootfs_path: str
    compiled_at: datetime
    correlation_id: UUID

    def __post_init__(self) -> None:
        # The normalized snapshot is the last primitive representation before
        # domain materialization. Keeping it immutable helps deterministic hash
        # generation and simplifies reasoning in the factory.
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "environment", _freeze_mapping(self.environment))
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "effective_caps", frozenset(self.effective_caps))
        object.__setattr__(self, "permitted_caps", frozenset(self.permitted_caps))
        object.__setattr__(self, "bounding_caps", frozenset(self.bounding_caps))


class KernelCapabilities(Protocol):
    """Protocol consumed by enrichment so the application layer stays decoupled.

    The concrete implementation will live in infrastructure, but the admission
    pipeline only needs the minimal contract described in requirement 15.5.
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

    def validate_minimum_requirements(self) -> None:
        """Validate mandatory kernel prerequisites.

        Raises:
            DomainError: When the host misses a required feature such as cgroups
                v2, clone3, pidfd, seccomp, or pivot_root.
        """


class PrecedenceResolutionPolicy(Protocol):
    """Resolve fixed-order precedence layers into one merged snapshot.

    This protocol maps directly to the first stage in the admission pipeline
    from ``specs/run_spec/design.md``.
    """

    def resolve(
        self,
        image_metadata: ImageMetadata,
        platform_defaults: PlatformDefaults,
        policy_injection: PolicyConfig,
        user_overrides: UserOverrides,
        *,
        compiled_at: datetime,
        correlation_id: UUID,
    ) -> ResolvedInputs:
        """Merge the precedence layers into one resolved snapshot.

        Args:
            image_metadata: Lowest-precedence layer.
            platform_defaults: Platform-wide defaults.
            policy_injection: Policy-derived overrides.
            user_overrides: Highest-precedence overrides.
            compiled_at: Compilation timestamp to keep with the snapshot.
            correlation_id: Trace identifier to keep with the snapshot.

        Returns:
            The resolved snapshot consumed by the enrichment stage.
        """


class PolicyEnrichmentPolicy(Protocol):
    """Inject mandatory policy defaults and validate kernel-driven constraints.

    This is the second stage in the admission pipeline. It turns partially
    resolved data into a fully policy-compliant snapshot.
    """

    def enrich(
        self,
        resolved: ResolvedInputs,
        kernel_caps: KernelCapabilities,
    ) -> EnrichedInputs:
        """Enrich resolved inputs with policy defaults and kernel knowledge.

        Args:
            resolved: Snapshot produced by precedence resolution.
            kernel_caps: Cached kernel feature information.

        Returns:
            The enriched snapshot that is ready for path normalization.
        """


class PathNormalizationPolicy(Protocol):
    """Normalize lexical paths before domain value objects are constructed.

    This is the last application-stage transformation before the domain factory
    takes over.
    """

    def normalize(self, enriched: EnrichedInputs) -> NormalizedInputs:
        """Normalize all path fields into canonical absolute paths.

        Args:
            enriched: Policy-complete snapshot that may still contain non-canonical
                lexical paths.

        Returns:
            A normalized snapshot ready for value-object construction.
        """


__all__ = [
    "CompilationLayer",
    "EnrichedInputs",
    "ImageMetadata",
    "KernelCapabilities",
    "MountRequest",
    "NormalizedInputs",
    "PathNormalizationPolicy",
    "PlatformDefaults",
    "PolicyConfig",
    "PolicyEnrichmentPolicy",
    "PrecedenceResolutionPolicy",
    "RawCompilationInputs",
    "ResolvedInputs",
    "UserOverrides",
]
