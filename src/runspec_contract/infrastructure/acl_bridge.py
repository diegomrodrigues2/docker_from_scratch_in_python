"""Anti-corruption layer between Python and the native data plane.

This module implements the Python side of the ACL described in
``specs/run_spec/design.md`` under the "Fronteira ACL - Design Detalhado"
section. In that design, the bridge is responsible for a very small but
critical set of invariants before control crosses into Cython/native code:

1. validate the serialized envelope shape
2. verify the CRC32C checksum before the native handoff
3. measure traversal latency for observability
4. translate native status codes into stable Python domain errors

Requirements covered here are the ones listed in task 10 of
``specs/run_spec/tasks.md``: 14.3, 14.4, 14.5, and 14.6.

The code is intentionally explicit. The ACL is a boundary where hidden magic
would make production failures harder to diagnose, so each validation step is
kept visible and independently testable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from runspec_contract.domain import ACLError, ChecksumError, CorruptedPayloadError

RUNSPEC_MAGIC: Final[bytes] = b"RSPC"
RUNSPEC_FORMAT_VERSION: Final[int] = 1
RUNSPEC_HEADER_SIZE: Final[int] = 6
RUNSPEC_CHECKSUM_SIZE: Final[int] = 4
ACL_LATENCY_WARNING_NS: Final[int] = 2_000_000
_MIN_SERIALIZED_SIZE: Final[int] = RUNSPEC_HEADER_SIZE + RUNSPEC_CHECKSUM_SIZE
_CRC32C_POLYNOMIAL: Final[int] = 0x82F63B78
_UNKNOWN_NATIVE_ERROR_DETAIL: Final[str] = "native ACL execution failed."
_DEFAULT_ERROR_DETAILS = MappingProxyType(
    {
        # These messages are intentionally phrased in domain language instead of
        # leaking C++ implementation details. The caller only needs to know
        # which boundary failed, not how the native internals are structured.
        -1: "serialized runspec was rejected before handoff.",
        -2: "root filesystem path was rejected before handoff.",
        -3: "native bootstrap failed before the container became runnable.",
        -4: "native ACL handoff failed.",
    }
)


def _require_serialized(serialized: bytes) -> bytes:
    """Validate the serialized payload type at the Python boundary.

    Args:
        serialized: Candidate serialized RunSpec bytes supplied by the caller.

    Returns:
        The same ``serialized`` value once the type contract is validated.

    Raises:
        TypeError: If ``serialized`` is not ``bytes``.
    """

    if not isinstance(serialized, bytes):
        raise TypeError("serialized must be bytes.")
    return serialized


def _require_rootfs_path(rootfs_path: str) -> str:
    """Validate the root filesystem path before native execution.

    Args:
        rootfs_path: Host path that identifies the prepared container rootfs.

    Returns:
        The same ``rootfs_path`` when it satisfies the boundary contract.

    Raises:
        TypeError: If ``rootfs_path`` is not a string.
        ValueError: If ``rootfs_path`` is empty or only whitespace.
    """

    if not isinstance(rootfs_path, str):
        raise TypeError("rootfs_path must be a string.")
    if not rootfs_path.strip():
        raise ValueError("rootfs_path must not be empty.")
    return rootfs_path


def _require_native_code(native_code: int) -> int:
    """Validate native status codes before translating them into domain errors.

    Args:
        native_code: Integer status code returned by the native layer.

    Returns:
        The same ``native_code`` once validated.

    Raises:
        TypeError: If ``native_code`` is not an integer.
    """

    if isinstance(native_code, bool) or not isinstance(native_code, int):
        raise TypeError("native_code must be an integer.")
    return native_code


def _build_crc32c_table() -> tuple[int, ...]:
    """Precompute the CRC32C lookup table used by ``compute_crc32c``.

    The serializer format in the design uses CRC32C as the integrity checksum.
    The table is computed once at import time so checksum verification stays
    straightforward and fast during every bridge execution.

    Returns:
        A 256-entry lookup table for the Castagnoli CRC32 polynomial.
    """

    table: list[int] = []
    for entry in range(256):
        crc = entry
        for _ in range(8):
            # Each loop iteration folds one bit using the reflected CRC32C
            # polynomial. The final table lets the runtime algorithm process
            # one byte at a time instead of one bit at a time.
            if crc & 1:
                crc = (crc >> 1) ^ _CRC32C_POLYNOMIAL
            else:
                crc >>= 1
        table.append(crc & 0xFFFFFFFF)
    return tuple(table)


_CRC32C_TABLE = _build_crc32c_table()


def compute_crc32c(data: bytes) -> int:
    """Compute the CRC32C checksum for a serialized RunSpec envelope.

    Args:
        data: Envelope bytes excluding the trailing checksum field.

    Returns:
        The CRC32C checksum as an unsigned 32-bit integer.

    Raises:
        TypeError: If ``data`` is not ``bytes``.
    """

    data = _require_serialized(data)
    crc = 0xFFFFFFFF
    for byte in data:
        # The lookup table collapses the byte-wise CRC update into a simple
        # indexed XOR. This keeps the code readable while matching the format
        # expected by the serializer design.
        crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ byte) & 0xFF]
    return (~crc) & 0xFFFFFFFF


def verify_serialized_checksum(serialized: bytes) -> None:
    """Validate the serialized envelope before handing it to the data plane.

    Step by step, this function protects the ACL boundary by:

    1. asserting that the caller really provided bytes
    2. checking the minimum envelope size
    3. validating the fixed magic header
    4. validating the supported format version
    5. recomputing CRC32C and comparing it with the trailing checksum

    Args:
        serialized: Full serialized envelope ``Magic + Version + Payload + CRC``.

    Raises:
        TypeError: If ``serialized`` is not ``bytes``.
        CorruptedPayloadError: If the envelope is truncated, has invalid magic,
            or uses an unsupported version.
        ChecksumError: If the trailing CRC32C does not match the payload bytes.
    """

    serialized = _require_serialized(serialized)
    if len(serialized) < _MIN_SERIALIZED_SIZE:
        raise CorruptedPayloadError("serialized payload is truncated.")

    # The magic header lets the ACL reject obviously invalid frames before it
    # spends time doing deeper parsing or calling native code.
    magic = serialized[: len(RUNSPEC_MAGIC)]
    if magic != RUNSPEC_MAGIC:
        raise CorruptedPayloadError("serialized payload has an invalid magic header.")

    # The current project supports one wire-format version. Rejecting unknown
    # versions early prevents subtle mismatches between control plane and data
    # plane expectations.
    version = int.from_bytes(serialized[len(RUNSPEC_MAGIC) : RUNSPEC_HEADER_SIZE], "big")
    if version != RUNSPEC_FORMAT_VERSION:
        raise CorruptedPayloadError(
            f"serialized payload uses unsupported version {version}."
        )

    # The checksum occupies the last four bytes of the frame. Everything before
    # it participates in the CRC calculation.
    expected = serialized[-RUNSPEC_CHECKSUM_SIZE:]
    actual = compute_crc32c(serialized[:-RUNSPEC_CHECKSUM_SIZE]).to_bytes(
        RUNSPEC_CHECKSUM_SIZE,
        "big",
    )
    if actual != expected:
        raise ChecksumError(expected=expected, actual=actual)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Result returned by the ACL after a successful native handoff.

    Attributes:
        pidfd: Process file descriptor returned by the native runtime.
    """

    pidfd: int

    def __post_init__(self) -> None:
        """Validate the native result object immediately after construction.

        Raises:
            TypeError: If ``pidfd`` is not an integer.
            ValueError: If ``pidfd`` is negative.
        """

        if isinstance(self.pidfd, bool) or not isinstance(self.pidfd, int):
            raise TypeError("pidfd must be an integer.")
        if self.pidfd < 0:
            raise ValueError("pidfd must be non-negative.")


def _missing_native_executor(serialized: bytes, rootfs_path: str) -> int:
    """Fail loudly when the Python bridge is used without a native executor.

    Args:
        serialized: Serialized payload that would have been sent natively.
        rootfs_path: Root filesystem path that would have been sent natively.

    Raises:
        RuntimeError: Always, because the Cython/native layer is not wired in.
    """

    raise RuntimeError(
        "Native ACL executor is unavailable. Build the Cython bridge before executing."
    )


class CythonBridge:
    """Strict ACL boundary that mediates Python-to-native execution.

    The bridge exists to keep the domain model decoupled from the native data
    plane. It accepts already-serialized bytes and performs only the boundary
    duties mandated by the design:

    - validate envelope integrity
    - time the native traversal
    - convert native failures into Python domain errors

    The actual native call is dependency-injected so tests can exercise the ACL
    behavior without requiring a compiled Cython extension.
    """

    def __init__(
        self,
        native_executor: Callable[[bytes, str], int] | None = None,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        logger: logging.Logger | None = None,
        error_details: Mapping[int, str] | None = None,
    ) -> None:
        """Build a bridge instance with explicit infrastructure dependencies.

        Args:
            native_executor: Callable that performs the real native execution.
                When omitted, the bridge raises a runtime error on use.
            clock_ns: Monotonic clock used for latency measurements.
            logger: Logger used for ACL observability warnings.
            error_details: Optional native-code-to-message mapping. The mapping
                is copied into an immutable proxy to keep translations stable.
        """

        self._native_executor = native_executor or _missing_native_executor
        self._clock_ns = clock_ns
        self._logger = logger or logging.getLogger(__name__)
        self._error_details = MappingProxyType(
            dict(_DEFAULT_ERROR_DETAILS if error_details is None else error_details)
        )

    def execute(self, serialized: bytes, rootfs_path: str) -> ExecutionResult:
        """Execute a serialized RunSpec through the ACL boundary.

        The method follows the same order as the design pseudocode:

        1. validate Python input types
        2. verify checksum and envelope structure
        3. invoke the native executor while timing the traversal
        4. emit a warning if the traversal is slower than 2 ms
        5. translate negative native status codes into ``ACLError``
        6. wrap successful results in ``ExecutionResult``

        Args:
            serialized: Serialized RunSpec envelope produced by the serializer.
            rootfs_path: Host path of the prepared root filesystem.

        Returns:
            ``ExecutionResult`` containing the native ``pidfd``.

        Raises:
            TypeError: If the Python input types are invalid.
            ValueError: If ``rootfs_path`` is empty.
            CorruptedPayloadError: If the frame shape is invalid.
            ChecksumError: If the frame fails CRC32C verification.
            ACLError: If the native layer returns a negative status code.
            RuntimeError: If no native executor is configured.
        """

        # Step 1: enforce the Python-facing contract before touching the
        # serialized bytes or any infrastructure dependency.
        serialized = _require_serialized(serialized)
        rootfs_path = _require_rootfs_path(rootfs_path)

        # Step 2: fail fast on corrupted frames so invalid bytes never reach the
        # native side of the ACL boundary.
        verify_serialized_checksum(serialized)

        # Step 3: measure only the native traversal. Validation time is kept
        # outside the measurement because the latency warning is about boundary
        # crossing, not caller-side preparation.
        start_ns = self._clock_ns()
        try:
            native_result = self._execute_native(serialized, rootfs_path)
        finally:
            # The warning must also fire when the native executor raises, so the
            # timing logic lives in ``finally`` rather than after the call.
            elapsed_ns = self._clock_ns() - start_ns
            self._warn_if_slow(elapsed_ns)

        # Step 4: negative codes represent native failures. The translation is
        # centralized so the rest of the Python code sees only domain errors.
        if native_result < 0:
            raise self.translate_error(native_result)

        # Step 5: on success, expose only the stable contract expected by the
        # application layer.
        return ExecutionResult(pidfd=native_result)

    def translate_error(self, native_code: int) -> ACLError:
        """Translate native status codes into stable Python domain errors.

        Args:
            native_code: Native status code returned by the data plane.

        Returns:
            ``ACLError`` with a deterministic message for the given code.

        Raises:
            TypeError: If ``native_code`` is not an integer.
        """

        native_code = _require_native_code(native_code)
        detail = self._error_details.get(native_code, _UNKNOWN_NATIVE_ERROR_DETAIL)
        return ACLError(native_code=native_code, detail=detail)

    def _execute_native(self, serialized: bytes, rootfs_path: str) -> int:
        """Delegate the call to the configured native executor.

        This helper keeps the public ``execute`` method focused on orchestration
        while making the handoff point explicit for readers and tests.

        Args:
            serialized: Serialized RunSpec envelope.
            rootfs_path: Prepared container root filesystem.

        Returns:
            Integer status code returned by the native executor.
        """

        return self._native_executor(serialized, rootfs_path)

    def _warn_if_slow(self, elapsed_ns: int) -> None:
        """Emit an observability warning when ACL traversal is too slow.

        Args:
            elapsed_ns: Measured native traversal latency in nanoseconds.
        """

        if elapsed_ns > ACL_LATENCY_WARNING_NS:
            self._logger.warning(
                "ACL traversal exceeded 2ms",
                extra={"elapsed_ms": elapsed_ns / 1_000_000},
            )


__all__ = [
    "ACL_LATENCY_WARNING_NS",
    "CythonBridge",
    "ExecutionResult",
    "RUNSPEC_CHECKSUM_SIZE",
    "RUNSPEC_FORMAT_VERSION",
    "RUNSPEC_HEADER_SIZE",
    "RUNSPEC_MAGIC",
    "compute_crc32c",
    "verify_serialized_checksum",
]
