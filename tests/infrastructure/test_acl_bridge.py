from __future__ import annotations

import logging

import pytest

from runspec_contract.domain import ACLError, ChecksumError
from runspec_contract.infrastructure import (
    CythonBridge,
    RUNSPEC_CHECKSUM_SIZE,
    RUNSPEC_FORMAT_VERSION,
    RUNSPEC_MAGIC,
    compute_crc32c,
)


def _serialized_frame(payload: bytes = b"runspec-payload") -> bytes:
    envelope = RUNSPEC_MAGIC + RUNSPEC_FORMAT_VERSION.to_bytes(2, "big") + payload
    checksum = compute_crc32c(envelope).to_bytes(RUNSPEC_CHECKSUM_SIZE, "big")
    return envelope + checksum


def test_acl_bridge_verifies_checksum_before_native_execution() -> None:
    native_calls = 0

    def fake_native_executor(serialized: bytes, rootfs_path: str) -> int:
        nonlocal native_calls
        native_calls += 1
        return 7

    corrupted = bytearray(_serialized_frame())
    corrupted[6] ^= 0x01

    bridge = CythonBridge(native_executor=fake_native_executor)

    with pytest.raises(ChecksumError):
        bridge.execute(bytes(corrupted), "/containers/rootfs")

    assert native_calls == 0


def test_acl_bridge_warns_when_latency_exceeds_two_milliseconds(caplog: pytest.LogCaptureFixture) -> None:
    clock_values = iter((1_000, 3_101_000))
    logger = logging.getLogger("tests.acl_bridge")
    bridge = CythonBridge(
        native_executor=lambda serialized, rootfs_path: 41,
        clock_ns=lambda: next(clock_values),
        logger=logger,
    )

    with caplog.at_level(logging.WARNING, logger="tests.acl_bridge"):
        result = bridge.execute(_serialized_frame(), "/containers/rootfs")

    assert result.pidfd == 41
    assert len(caplog.records) == 1
    assert caplog.records[0].message == "ACL traversal exceeded 2ms"
    assert caplog.records[0].elapsed_ms == pytest.approx(3.1)


def test_acl_bridge_translates_native_errors_without_exposing_cpp_details() -> None:
    bridge = CythonBridge(native_executor=lambda serialized, rootfs_path: -3)

    with pytest.raises(ACLError) as exc_info:
        bridge.execute(_serialized_frame(), "/containers/rootfs")

    assert exc_info.value.native_code == -3
    assert exc_info.value.detail == "native bootstrap failed before the container became runnable."
    assert "C++" not in exc_info.value.detail
