"""Aggregate roots for the RunSpec domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ._validation import require_aware_datetime, require_bytes, require_instance, require_uuid
from .exceptions import ValidationError
from .types import AbsolutePath
from .value_objects import (
    EnvironmentMap,
    Hostname,
    MountTopology,
    ProcessSpec,
    ResourceSpec,
    SecurityEnvelope,
)


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Canonical execution contract shared between control plane and data plane.

    Every field is already a validated value object before the aggregate is
    created. The aggregate is responsible for the invariants that span more than
    one component, such as keeping the process working directory inside the
    assembled root filesystem.
    """

    process: ProcessSpec
    environment: EnvironmentMap
    mounts: MountTopology
    hostname: Hostname
    resources: ResourceSpec
    security: SecurityEnvelope
    rootfs_path: AbsolutePath
    canonical_hash: bytes
    compiled_at: datetime
    correlation_id: UUID

    def __post_init__(self) -> None:
        # Validation pipeline:
        # 1. Ensure every nested component is already expressed in domain terms.
        # 2. Validate metadata used for deterministic hashing and tracing.
        # 3. Enforce the boundary that keeps the process inside the declared rootfs.
        self._validate_components()
        self._validate_metadata()
        self._validate_process_boundary()

    def _validate_components(self) -> None:
        require_instance("RunSpec.process", self.process, ProcessSpec, "process must be a ProcessSpec")
        require_instance(
            "RunSpec.environment",
            self.environment,
            EnvironmentMap,
            "environment must be an EnvironmentMap",
        )
        require_instance("RunSpec.mounts", self.mounts, MountTopology, "mounts must be a MountTopology")
        require_instance("RunSpec.hostname", self.hostname, Hostname, "hostname must be a Hostname")
        require_instance(
            "RunSpec.resources",
            self.resources,
            ResourceSpec,
            "resources must be a ResourceSpec",
        )
        require_instance(
            "RunSpec.security",
            self.security,
            SecurityEnvelope,
            "security must be a SecurityEnvelope",
        )
        require_instance(
            "RunSpec.rootfs_path",
            self.rootfs_path,
            AbsolutePath,
            "rootfs_path must be an AbsolutePath",
        )

    def _validate_metadata(self) -> None:
        require_bytes("RunSpec.canonical_hash", self.canonical_hash)
        require_aware_datetime("RunSpec.compiled_at", self.compiled_at)
        require_uuid("RunSpec.correlation_id", self.correlation_id)

    def _validate_process_boundary(self) -> None:
        if not self._is_descendant_path(self.process.cwd, self.rootfs_path):
            raise ValidationError(
                "RunSpec.process.cwd",
                f"cwd '{self.process.cwd}' must be contained inside rootfs '{self.rootfs_path}'",
            )

    @staticmethod
    def _is_descendant_path(child_path: AbsolutePath, parent_path: AbsolutePath) -> bool:
        """Check lexical path containment without touching the host filesystem."""

        child_value = child_path.value
        parent_value = parent_path.value
        if parent_value == "/":
            return True
        return child_value == parent_value or child_value.startswith(f"{parent_value}/")


__all__ = ["RunSpec"]
