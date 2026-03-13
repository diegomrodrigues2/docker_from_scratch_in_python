"""Factory responsible for atomically building the ``RunSpec`` aggregate.

This module implements the "RunSpecFactory" from ``specs/run_spec/design.md``.
It is the boundary where application-layer primitive snapshots are upgraded into
domain value objects.

Step-by-step flow:
    1. build each value object independently
    2. validate cross-object invariants that individual value objects cannot see
    3. compute the canonical hash deterministically
    4. derive the hostname from that hash when the user omitted one
    5. return the immutable aggregate root

This serves requirements 1.1, 1.3, 1.4, and 1.6.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from .aggregate import RunSpec
from .exceptions import CrossValidationError
from .types import AbsolutePath, MountOptions, SeccompProfile
from .value_objects import (
    CpuLimit,
    EnvironmentMap,
    Hostname,
    MemoryLimit,
    MountSpec,
    MountTopology,
    PidsLimit,
    ProcessSpec,
    ResourceSpec,
    SecurityEnvelope,
)

if TYPE_CHECKING:
    from runspec_contract.application.protocols import NormalizedInputs


class RunSpecFactory:
    """Build a fully materialized RunSpec in one place.

    Keeping this construction centralized makes the lifecycle explicit:
    primitive inputs become validated value objects, cross-object invariants are
    checked once, and only then is the immutable aggregate returned.
    """

    @staticmethod
    def create(normalized: "NormalizedInputs") -> RunSpec:
        """Create the immutable aggregate from normalized primitive inputs.

        Args:
            normalized: Last application-layer snapshot after path normalization.

        Returns:
            A fully materialized and validated ``RunSpec`` aggregate.

        Raises:
            CrossValidationError: If a cross-field invariant is violated.
            ValidationError: Indirectly, when any value object rejects input.
        """
        # Build each value object independently first. This keeps validation
        # errors localized to the component that owns the invariant.
        process = RunSpecFactory._build_process(normalized)
        environment = RunSpecFactory._build_environment(normalized)
        mounts = RunSpecFactory._build_mount_topology(normalized)
        resources = RunSpecFactory._build_resources(normalized)
        security = RunSpecFactory._build_security(normalized)
        rootfs_path = AbsolutePath(normalized.rootfs_path)

        # Cross-object invariants live here instead of inside individual value
        # objects because they need visibility across multiple components.
        RunSpecFactory._validate_cross_invariants(
            process=process,
            mounts=mounts,
            rootfs_path=rootfs_path,
        )

        # The canonical hash must be computed before hostname derivation so the
        # fallback hostname can be deterministically derived from the hash.
        canonical_hash = RunSpecFactory._calculate_canonical_hash(normalized)
        hostname = (
            Hostname(normalized.hostname)
            if normalized.hostname is not None
            else Hostname.from_hash(canonical_hash)
        )

        return RunSpec(
            process=process,
            environment=environment,
            mounts=mounts,
            hostname=hostname,
            resources=resources,
            security=security,
            rootfs_path=rootfs_path,
            canonical_hash=canonical_hash,
            compiled_at=normalized.compiled_at,
            correlation_id=normalized.correlation_id,
        )

    @staticmethod
    def _build_process(normalized: "NormalizedInputs") -> ProcessSpec:
        """Build the ``ProcessSpec`` value object."""
        return ProcessSpec(
            argv=tuple(normalized.argv),
            uid=normalized.uid,
            gid=normalized.gid,
            cwd=AbsolutePath(normalized.cwd),
        )

    @staticmethod
    def _build_environment(normalized: "NormalizedInputs") -> EnvironmentMap:
        """Build the deterministic ``EnvironmentMap`` value object."""
        return EnvironmentMap.compile_from_layers(dict(normalized.environment))

    @staticmethod
    def _build_mount_topology(normalized: "NormalizedInputs") -> MountTopology:
        """Build the ordered ``MountTopology`` from primitive mount requests."""
        mount_specs = tuple(
            MountSpec(
                mount_type=mount.mount_type,
                destination=AbsolutePath(mount.destination),
                source=mount.source,
                propagation=mount.propagation,
                readonly=mount.readonly,
                options=MountOptions.from_mapping(mount.options),
            )
            for mount in normalized.mounts
        )
        return MountTopology(mounts=mount_specs)

    @staticmethod
    def _build_resources(normalized: "NormalizedInputs") -> ResourceSpec:
        """Build the ``ResourceSpec`` aggregate of cgroup limits."""
        return ResourceSpec(
            cpu=CpuLimit(
                quota_us=normalized.cpu_quota_us,
                period_us=normalized.cpu_period_us,
            ),
            memory=MemoryLimit(max_bytes=normalized.memory_max_bytes),
            pids=PidsLimit(max_pids=normalized.pids_max),
        )

    @staticmethod
    def _build_security(normalized: "NormalizedInputs") -> SecurityEnvelope:
        """Build the immutable ``SecurityEnvelope`` value object."""
        seccomp_profile = (
            SeccompProfile(normalized.seccomp_profile)
            if normalized.seccomp_profile is not None
            else None
        )
        return SecurityEnvelope(
            effective_caps=frozenset(normalized.effective_caps),
            permitted_caps=frozenset(normalized.permitted_caps),
            bounding_caps=frozenset(normalized.bounding_caps),
            no_new_privs=normalized.no_new_privs,
            seccomp_profile=seccomp_profile,
            lsm_profile=normalized.lsm_profile,
        )

    @staticmethod
    def _validate_cross_invariants(
        *,
        process: ProcessSpec,
        mounts: MountTopology,
        rootfs_path: AbsolutePath,
    ) -> None:
        """Validate invariants that span more than one value object.

        Args:
            process: Process specification already validated in isolation.
            mounts: Mount topology already validated in isolation.
            rootfs_path: Canonical rootfs path.

        Raises:
            CrossValidationError: If a mount destination or CWD escapes the
                declared rootfs boundary.
        """
        # Requirement 10.6 requires the working directory to stay inside the
        # rootfs. The aggregate also checks this, but the factory validates it
        # earlier so cross-field failures surface with factory context.
        if not RunSpec._is_descendant_path(process.cwd, rootfs_path):
            raise CrossValidationError(
                ("process.cwd", "rootfs_path"),
                f"cwd '{process.cwd}' must be contained inside rootfs '{rootfs_path}'",
            )

        # Mount destinations are also anchored inside the assembled rootfs.
        # Validating this here prevents impossible VFS topologies from becoming
        # part of the aggregate.
        for mount in mounts.mounts:
            if not RunSpec._is_descendant_path(mount.destination, rootfs_path):
                raise CrossValidationError(
                    ("mounts.destination", "rootfs_path"),
                    f"mount destination '{mount.destination}' must be contained inside rootfs '{rootfs_path}'",
                )

    @staticmethod
    def _calculate_canonical_hash(normalized: "NormalizedInputs") -> bytes:
        """Calculate the canonical hash from the normalized semantic payload.

        The hash is computed from a JSON payload whose ordering is fully
        deterministic:
        - dictionaries are sorted
        - sets are converted to sorted lists
        - mount options are emitted in lexical order

        Args:
            normalized: The normalized snapshot used as the source of truth.

        Returns:
            A SHA-256 digest representing the canonical semantic contract.
        """
        canonical_payload = {
            "process": {
                "argv": list(normalized.argv),
                "uid": normalized.uid,
                "gid": normalized.gid,
                "cwd": normalized.cwd,
            },
            "environment": [
                [key, value]
                for key, value in sorted(normalized.environment.items(), key=lambda item: item[0])
            ],
            "mounts": [
                {
                    "mount_type": mount.mount_type.value,
                    "destination": mount.destination,
                    "source": mount.source,
                    "propagation": mount.propagation.value,
                    "readonly": mount.readonly,
                    "options": [
                        [option_name, option_value]
                        for option_name, option_value in sorted(
                            mount.options.items(),
                            key=lambda item: item[0],
                        )
                    ],
                }
                for mount in normalized.mounts
            ],
            # When hostname is omitted, the hash is computed over the semantic
            # payload without a hostname and then the hostname is derived from
            # that hash. This avoids a circular dependency between hash and
            # hostname while still satisfying requirement 6.3.
            "hostname": normalized.hostname,
            "resources": {
                "cpu_quota_us": normalized.cpu_quota_us,
                "cpu_period_us": normalized.cpu_period_us,
                "memory_max_bytes": normalized.memory_max_bytes,
                "pids_max": normalized.pids_max,
            },
            "security": {
                "effective_caps": sorted(normalized.effective_caps),
                "permitted_caps": sorted(normalized.permitted_caps),
                "bounding_caps": sorted(normalized.bounding_caps),
                "no_new_privs": normalized.no_new_privs,
                "seccomp_profile": normalized.seccomp_profile,
                "lsm_profile": normalized.lsm_profile,
            },
            "rootfs_path": normalized.rootfs_path,
        }
        serialized_payload = json.dumps(
            canonical_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(serialized_payload).digest()


__all__ = ["RunSpecFactory"]
