"""Admission pipeline that compiles raw inputs into an immutable ``RunSpec``.

This module implements the orchestration described in the "AdmissionPipeline"
section of ``specs/run_spec/design.md``.

Step-by-step flow:
    1. resolve precedence across the four input layers
    2. enrich the resolved snapshot with policy defaults and kernel constraints
    3. normalize lexical paths into canonical absolute paths
    4. delegate aggregate construction to ``RunSpecFactory``
    5. emit ``RunSpecCompiled`` after successful compilation

This directly serves requirements 1.1, 1.2, 1.5, and 1.6.
"""

from __future__ import annotations

from uuid import uuid4

from runspec_contract.domain.events import RunSpecCompiled
from runspec_contract.domain.factory import RunSpecFactory

from .protocols import (
    KernelCapabilities,
    PathNormalizationPolicy,
    PolicyEnrichmentPolicy,
    PrecedenceResolutionPolicy,
    RawCompilationInputs,
)


class AdmissionPipeline:
    """Orchestrate the admission stages in the order mandated by the design.

    The pipeline itself stays intentionally thin. It does not own domain rules.
    Instead, it wires together the policies and the domain factory, then records
    the event that proves compilation completed successfully.
    """

    def __init__(
        self,
        precedence: PrecedenceResolutionPolicy,
        enrichment: PolicyEnrichmentPolicy,
        normalization: PathNormalizationPolicy,
    ) -> None:
        """Create a pipeline with injectable strategy objects.

        Args:
            precedence: Strategy responsible for fixed-order precedence merging.
            enrichment: Strategy responsible for policy defaults and kernel
                validation.
            normalization: Strategy responsible for canonical path normalization.
        """
        self._precedence = precedence
        self._enrichment = enrichment
        self._normalization = normalization
        self._pending_events: list[RunSpecCompiled] = []

    def compile(
        self,
        raw_inputs: RawCompilationInputs,
        kernel_caps: KernelCapabilities,
    ):
        """Compile a ``RunSpec`` from raw inputs.

        Args:
            raw_inputs: Raw inputs grouped by precedence layer plus metadata.
            kernel_caps: Cached kernel probe result used by the enrichment stage.

        Returns:
            The immutable ``RunSpec`` aggregate produced by the factory.
        """
        # Each assignment below corresponds to one stage from the spec. The code
        # stays intentionally linear so a reader can compare it line-by-line
        # against the design document.
        resolved = self._precedence.resolve(
            raw_inputs.image_metadata,
            raw_inputs.platform_defaults,
            raw_inputs.policy_config,
            raw_inputs.user_overrides,
            compiled_at=raw_inputs.compiled_at,
            correlation_id=raw_inputs.correlation_id,
        )
        enriched = self._enrichment.enrich(resolved, kernel_caps)
        normalized = self._normalization.normalize(enriched)
        runspec = RunSpecFactory.create(normalized)

        # Event emission happens only after the aggregate exists successfully.
        # That preserves the "fact that happened" semantics required by the
        # domain event model.
        self._pending_events.append(
            RunSpecCompiled(
                event_id=uuid4(),
                occurred_at=runspec.compiled_at,
                correlation_id=runspec.correlation_id,
                runspec_hash=runspec.canonical_hash,
            )
        )
        return runspec

    def drain_events(self) -> tuple[RunSpecCompiled, ...]:
        """Return and clear the events emitted since the last drain.

        Returns:
            A tuple of emitted ``RunSpecCompiled`` events.
        """

        pending_events = tuple(self._pending_events)
        self._pending_events.clear()
        return pending_events


__all__ = ["AdmissionPipeline"]
