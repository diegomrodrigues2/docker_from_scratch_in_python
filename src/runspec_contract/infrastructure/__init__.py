"""Infrastructure adapters for the RunSpec contract."""

from .acl_bridge import (
    ACL_LATENCY_WARNING_NS,
    CythonBridge,
    ExecutionResult,
    RUNSPEC_CHECKSUM_SIZE,
    RUNSPEC_FORMAT_VERSION,
    RUNSPEC_HEADER_SIZE,
    RUNSPEC_MAGIC,
    compute_crc32c,
    verify_serialized_checksum,
)
from .kernel_probe import KernelCapabilities, KernelProbe, MINIMUM_KERNEL_VERSION
from .serializer import RunSpecSerializer

__all__ = [
    "ACL_LATENCY_WARNING_NS",
    "CythonBridge",
    "ExecutionResult",
    "KernelCapabilities",
    "KernelProbe",
    "MINIMUM_KERNEL_VERSION",
    "RUNSPEC_CHECKSUM_SIZE",
    "RUNSPEC_FORMAT_VERSION",
    "RUNSPEC_HEADER_SIZE",
    "RUNSPEC_MAGIC",
    "RunSpecSerializer",
    "compute_crc32c",
    "verify_serialized_checksum",
]
