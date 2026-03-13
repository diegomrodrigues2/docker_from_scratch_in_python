"""Default implementations for the admission pipeline policies.

These classes implement the three policy stages described in
``specs/run_spec/design.md``:

1. precedence resolution
2. policy enrichment
3. path normalization

Together they cover the application-side compilation flow required by
requirements 1.2, 3.6, 4.1, 5.5, and 15.5.
"""

from __future__ import annotations

import os
from datetime import datetime
from uuid import UUID

from runspec_contract.domain.exceptions import CompilationError, KernelFeatureError, ValidationError
from runspec_contract.domain.types import normalize_absolute_path
from runspec_contract.domain.value_objects import MountType

from .protocols import (
    EnrichedInputs,
    ImageMetadata,
    KernelCapabilities,
    MountRequest,
    NormalizedInputs,
    PlatformDefaults,
    PolicyConfig,
    ResolvedInputs,
    UserOverrides,
)


class DefaultPrecedenceResolutionPolicy:
    """Merge the four precedence layers in the fixed order defined by the spec.

    Requirement references:
        - Requirement 1.2: fixed precedence order
        - Requirement 4.1: environment compilation across layers

    Merge strategy:
        1. Scalars use "last non-None value wins".
        2. Environment variables merge per key, so higher precedence can
           override one variable without replacing the whole environment.
        3. Mounts replace the whole sequence when a higher-precedence layer
           explicitly provides one.
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
        """Resolve the four precedence layers into one application snapshot.

        Args:
            image_metadata: Lowest-precedence image metadata.
            platform_defaults: Platform-level defaults.
            policy_injection: Policy-provided values.
            user_overrides: Highest-precedence caller overrides.
            compiled_at: Timestamp propagated through compilation.
            correlation_id: Trace identifier propagated through compilation.

        Returns:
            A ``ResolvedInputs`` instance containing one chosen value per field.

        Raises:
            CompilationError: If a required field is still missing after merging.
        """
        layers = (
            image_metadata,
            platform_defaults,
            policy_injection,
            user_overrides,
        )

        # The return object is intentionally assembled field-by-field instead of
        # through a generic merge helper. That makes the precedence contract
        # explicit and easy to audit against the spec.
        return ResolvedInputs(
            argv=self._require_value("argv", layers),
            uid=self._require_value("uid", layers),
            gid=self._require_value("gid", layers),
            cwd=self._require_value("cwd", layers),
            environment=self._merge_environment(layers),
            mounts=self._resolve_optional_value("mounts", layers) or (),
            hostname=self._resolve_optional_value("hostname", layers),
            cpu_quota_us=self._require_value("cpu_quota_us", layers),
            cpu_period_us=self._require_value("cpu_period_us", layers),
            memory_max_bytes=self._require_value("memory_max_bytes", layers),
            pids_max=self._resolve_optional_value("pids_max", layers),
            effective_caps=self._resolve_optional_value("effective_caps", layers),
            permitted_caps=self._resolve_optional_value("permitted_caps", layers),
            bounding_caps=self._resolve_optional_value("bounding_caps", layers),
            no_new_privs=self._resolve_optional_value("no_new_privs", layers),
            seccomp_profile=self._resolve_optional_value("seccomp_profile", layers),
            lsm_profile=self._resolve_optional_value("lsm_profile", layers),
            rootfs_path=self._require_value("rootfs_path", layers),
            compiled_at=compiled_at,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _merge_environment(
        layers: tuple[ImageMetadata | PlatformDefaults | PolicyConfig | UserOverrides, ...],
    ) -> dict[str, str]:
        """Merge environment variables using the fixed precedence order.

        Later layers overwrite earlier ones on a per-key basis, which is the
        behavior required by requirement 4.2.
        """
        merged_environment: dict[str, str] = {}
        for layer in layers:
            merged_environment.update(layer.environment)
        return merged_environment

    @staticmethod
    def _resolve_optional_value(
        field_name: str,
        layers: tuple[ImageMetadata | PlatformDefaults | PolicyConfig | UserOverrides, ...],
    ):
        """Return the last non-``None`` value found across the precedence layers."""
        sentinel = object()
        resolved_value = sentinel
        for layer in layers:
            candidate = getattr(layer, field_name)
            if candidate is not None:
                resolved_value = candidate
        if resolved_value is sentinel:
            return None
        return resolved_value

    def _require_value(
        self,
        field_name: str,
        layers: tuple[ImageMetadata | PlatformDefaults | PolicyConfig | UserOverrides, ...],
    ):
        """Resolve a field that the pipeline considers mandatory."""
        resolved_value = self._resolve_optional_value(field_name, layers)
        if resolved_value is None:
            raise CompilationError(
                f"precedence resolution could not determine required field '{field_name}'"
            )
        return resolved_value


class DefaultPolicyEnrichmentPolicy:
    """Apply kernel-aware defaults after precedence has selected raw values.

    Requirement references:
        - Requirement 1.2: policy injection must happen before compilation
        - Requirement 15.5: kernel capabilities are consulted during compilation
        - Requirement 16.1: ``pids.max`` must always be present

    This stage does three jobs:
        1. validate that the host kernel can satisfy the requested contract
        2. inject mandatory defaults that the user may omit
        3. complete security fields so the domain factory receives a full shape
    """

    DEFAULT_PIDS_LIMIT = 256

    def enrich(
        self,
        resolved: ResolvedInputs,
        kernel_caps: KernelCapabilities,
    ) -> EnrichedInputs:
        """Enrich resolved inputs with policy defaults and kernel validation.

        Args:
            resolved: Snapshot produced by precedence resolution.
            kernel_caps: Cached kernel probe result.

        Returns:
            An ``EnrichedInputs`` snapshot with mandatory policy defaults filled.

        Raises:
            KernelFeatureError: If the host cannot honor required controllers or
                requested kernel-backed features.
        """
        # First validate the host globally. This keeps the failure close to the
        # application boundary instead of letting the domain factory build an
        # impossible contract.
        kernel_caps.validate_minimum_requirements()
        self._validate_required_cgroup_controllers(kernel_caps)
        self._validate_optional_kernel_features(resolved, kernel_caps)

        # Then fill fields that policies guarantee must exist before the domain
        # layer is invoked.
        effective_caps, permitted_caps, bounding_caps = self._complete_capability_hierarchy(
            resolved
        )
        no_new_privs = self._resolve_no_new_privs(resolved)
        pids_max = resolved.pids_max if resolved.pids_max is not None else self.DEFAULT_PIDS_LIMIT

        return EnrichedInputs(
            argv=resolved.argv,
            uid=resolved.uid,
            gid=resolved.gid,
            cwd=resolved.cwd,
            environment=resolved.environment,
            mounts=resolved.mounts,
            hostname=resolved.hostname,
            cpu_quota_us=resolved.cpu_quota_us,
            cpu_period_us=resolved.cpu_period_us,
            memory_max_bytes=resolved.memory_max_bytes,
            pids_max=pids_max,
            effective_caps=effective_caps,
            permitted_caps=permitted_caps,
            bounding_caps=bounding_caps,
            no_new_privs=no_new_privs,
            seccomp_profile=resolved.seccomp_profile,
            lsm_profile=resolved.lsm_profile,
            rootfs_path=resolved.rootfs_path,
            compiled_at=resolved.compiled_at,
            correlation_id=resolved.correlation_id,
        )

    @staticmethod
    def _validate_required_cgroup_controllers(kernel_caps: KernelCapabilities) -> None:
        """Ensure the host exposes the cgroup controllers required by the spec."""
        for controller_name in ("cpu", "memory", "pids"):
            if controller_name not in kernel_caps.available_cgroup_controllers:
                raise KernelFeatureError(
                    controller_name,
                    f"required cgroup v2 controller '{controller_name}' is unavailable",
                )

    @staticmethod
    def _validate_optional_kernel_features(
        resolved: ResolvedInputs,
        kernel_caps: KernelCapabilities,
    ) -> None:
        """Reject optional features that were requested on unsupported kernels."""
        if resolved.seccomp_profile is not None and not kernel_caps.has_seccomp:
            raise KernelFeatureError(
                "seccomp",
                "a seccomp profile was requested but the kernel does not support seccomp",
            )

    @staticmethod
    def _complete_capability_hierarchy(
        resolved: ResolvedInputs,
    ) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        """Complete the capability hierarchy expected by ``SecurityEnvelope``.

        The hierarchy is filled from the most concrete information outward:
        effective -> permitted -> bounding. This lets callers specify only one
        layer and still produce a valid security envelope later in the factory.
        """
        effective_caps = frozenset(resolved.effective_caps or ())
        permitted_caps = (
            frozenset(resolved.permitted_caps)
            if resolved.permitted_caps is not None
            else frozenset(effective_caps)
        )
        bounding_caps = (
            frozenset(resolved.bounding_caps)
            if resolved.bounding_caps is not None
            else frozenset(permitted_caps)
        )

        if resolved.permitted_caps is None:
            permitted_caps = frozenset(effective_caps or permitted_caps)
        if resolved.bounding_caps is None:
            bounding_caps = frozenset(permitted_caps or bounding_caps)
        if resolved.effective_caps is None:
            effective_caps = frozenset(permitted_caps)

        return effective_caps, permitted_caps, bounding_caps

    @staticmethod
    def _resolve_no_new_privs(resolved: ResolvedInputs) -> bool:
        """Default ``no_new_privs`` to true when seccomp is requested."""
        if resolved.no_new_privs is not None:
            return resolved.no_new_privs
        return resolved.seccomp_profile is not None


class DefaultPathNormalizationPolicy:
    """Normalize every path using lexical rules before domain objects exist.

    Requirement references:
        - Requirement 3.6: CWD path normalization
        - Requirement 5.5: mount destination normalization
        - Requirement 10.6: containment is enforced after normalization

    The domain model only accepts canonical POSIX-style absolute paths. This
    policy therefore does two steps:
        1. Use ``os.path.normpath`` because that is the explicit requirement.
        2. Convert separators back to POSIX form so the domain types remain
           platform-independent even when tests run on Windows.
    """

    def normalize(self, enriched: EnrichedInputs) -> NormalizedInputs:
        """Normalize rootfs, cwd, and mount paths before factory materialization.

        Args:
            enriched: Policy-complete snapshot that may still contain lexical
                path artifacts such as ``.`` or ``..``.

        Returns:
            A ``NormalizedInputs`` snapshot whose paths are canonical.
        """
        # Normalize the rootfs first because both CWD and mount destinations are
        # rewritten relative to this host path.
        normalized_rootfs = self._normalize_host_path(
            enriched.rootfs_path,
            field_name="rootfs_path",
        )
        # The pipeline treats CWD and mount destinations as container-relative
        # paths. At normalization time they are anchored under the rootfs so the
        # aggregate can later validate containment lexically.
        normalized_cwd = self._normalize_container_path_inside_rootfs(
            enriched.cwd,
            rootfs_path=normalized_rootfs,
            field_name="cwd",
        )
        normalized_mounts = tuple(
            self._normalize_mount(mount, normalized_rootfs, index)
            for index, mount in enumerate(enriched.mounts)
        )

        return NormalizedInputs(
            argv=enriched.argv,
            uid=enriched.uid,
            gid=enriched.gid,
            cwd=normalized_cwd,
            environment=enriched.environment,
            mounts=normalized_mounts,
            hostname=enriched.hostname,
            cpu_quota_us=enriched.cpu_quota_us,
            cpu_period_us=enriched.cpu_period_us,
            memory_max_bytes=enriched.memory_max_bytes,
            pids_max=enriched.pids_max,
            effective_caps=enriched.effective_caps,
            permitted_caps=enriched.permitted_caps,
            bounding_caps=enriched.bounding_caps,
            no_new_privs=enriched.no_new_privs,
            seccomp_profile=enriched.seccomp_profile,
            lsm_profile=enriched.lsm_profile,
            rootfs_path=normalized_rootfs,
            compiled_at=enriched.compiled_at,
            correlation_id=enriched.correlation_id,
        )

    def _normalize_mount(
        self,
        mount: MountRequest,
        rootfs_path: str,
        index: int,
    ) -> MountRequest:
        """Normalize one mount request.

        Args:
            mount: Primitive mount request from the enriched snapshot.
            rootfs_path: Canonical host path to the prepared rootfs.
            index: Positional index used only to improve validation messages.

        Returns:
            A new mount request with normalized destination and, for bind
            mounts, normalized source.
        """
        destination_field = f"mounts[{index}].destination"
        normalized_destination = self._normalize_container_path_inside_rootfs(
            mount.destination,
            rootfs_path=rootfs_path,
            field_name=destination_field,
        )

        normalized_source: str | None
        if mount.mount_type is MountType.BIND:
            # Only bind mounts carry host source paths. Tmpfs mounts are purely
            # synthetic and therefore have no source to normalize.
            normalized_source = self._normalize_host_path(
                mount.source,
                field_name=f"mounts[{index}].source",
            )
        else:
            normalized_source = None

        return MountRequest(
            mount_type=mount.mount_type,
            destination=normalized_destination,
            source=normalized_source,
            propagation=mount.propagation,
            readonly=mount.readonly,
            options=mount.options,
        )

    def _normalize_container_path_inside_rootfs(
        self,
        raw_container_path: str,
        *,
        rootfs_path: str,
        field_name: str,
    ) -> str:
        """Anchor a container path under the concrete host rootfs path.

        This is the key bridge between the container-facing API contract and the
        host-facing aggregate model: ``/workspace`` becomes
        ``<rootfs>/workspace``.
        """
        normalized_container_path = self._normalize_host_path(
            raw_container_path,
            field_name=field_name,
        )
        # The root directory of the container maps exactly to the rootfs path on
        # the host. Every other absolute container path is appended under it.
        combined_path = (
            rootfs_path
            if normalized_container_path == "/"
            else f"{rootfs_path.rstrip('/')}{normalized_container_path}"
        )
        return normalize_absolute_path(combined_path).value

    @staticmethod
    def _normalize_host_path(raw_path: str | None, *, field_name: str) -> str:
        """Normalize one absolute path using lexical normalization only.

        Args:
            raw_path: Raw path supplied by a caller or previous stage.
            field_name: Field name used in validation errors.

        Returns:
            The canonical absolute POSIX path.

        Raises:
            CompilationError: If the field was omitted entirely.
            ValidationError: If the provided value is not a valid absolute path.
        """
        if raw_path is None:
            raise CompilationError(f"path field '{field_name}' must not be omitted")
        if not isinstance(raw_path, str):
            raise ValidationError(field_name, "path must be a string")
        if not raw_path:
            raise ValidationError(field_name, "path must not be empty")
        if not raw_path.startswith("/"):
            raise ValidationError(field_name, "path must be absolute")

        # ``os.path.normpath`` is a requirement, but on Windows it emits
        # backslashes. We convert them back to POSIX separators so the domain
        # layer remains platform-independent and deterministic.
        normalized_platform_path = os.path.normpath(raw_path).replace("\\", "/")
        return normalize_absolute_path(normalized_platform_path).value


__all__ = [
    "DefaultPathNormalizationPolicy",
    "DefaultPolicyEnrichmentPolicy",
    "DefaultPrecedenceResolutionPolicy",
]
