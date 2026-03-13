"""Canonical binary serializer for the immutable ``RunSpec`` aggregate.

This module implements the serializer described in ``specs/run_spec/design.md``
and fills the concrete adapter expected by ``ContainerSupervisor``. The format
keeps the design's envelope structure:

1. fixed magic bytes ``RSPC``
2. two-byte big-endian wire-format version
3. payload encoded as a fixed sequence of length-prefixed sections
4. trailing CRC32C computed over ``magic + version + payload``

Why the payload carries ``canonical_hash`` explicitly:
    The aggregate stores the hash as first-class metadata, and hostname
    derivation can make it impossible to reconstruct the original hash from the
    finalized hostname alone. Serializing the hash explicitly keeps
    deserialization lossless and therefore preserves the round-trip guarantee.

The payload order remains explicit and stable so a human reader can compare the
code directly with the spec:
    process -> environment -> mounts -> hostname -> resources -> security ->
    rootfs_path -> canonical_hash -> compiled_at -> correlation_id
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Final
from uuid import UUID

from runspec_contract.domain import (
    AbsolutePath,
    CorruptedPayloadError,
    CpuLimit,
    EnvironmentMap,
    Hostname,
    MemoryLimit,
    MountOptions,
    MountSpec,
    MountTopology,
    MountType,
    PidsLimit,
    ProcessSpec,
    Propagation,
    ResourceSpec,
    RunSpec,
    SeccompProfile,
    SecurityEnvelope,
)

from .acl_bridge import (
    RUNSPEC_CHECKSUM_SIZE,
    RUNSPEC_FORMAT_VERSION,
    RUNSPEC_HEADER_SIZE,
    RUNSPEC_MAGIC,
    compute_crc32c,
    verify_serialized_checksum,
)

_SECTION_LENGTH_SIZE: Final[int] = 4
_EXPECTED_SECTION_COUNT: Final[int] = 10


class RunSpecSerializer:
    """Serialize and deserialize ``RunSpec`` objects deterministically.

    The serializer deliberately uses a fixed sequence of sections instead of a
    map-shaped envelope. That choice makes the wire contract easy to audit:
    every field is emitted in one known place, and deserialization can reject
    truncation or extra data as soon as the section count does not line up.
    """

    def serialize(self, runspec: RunSpec) -> bytes:
        """Serialize one immutable aggregate into the canonical binary envelope.

        Args:
            runspec: Aggregate already validated by the domain layer.

        Returns:
            ``Magic + Version + Payload + CRC32C`` bytes ready for the ACL.
        """

        if not isinstance(runspec, RunSpec):
            raise TypeError("runspec must be a RunSpec instance.")

        payload_sections = (
            self._encode_json_section(
                {
                    "argv": list(runspec.process.argv),
                    "uid": runspec.process.uid,
                    "gid": runspec.process.gid,
                    "cwd": runspec.process.cwd.value,
                }
            ),
            self._encode_json_section(
                [[key, value] for key, value in runspec.environment.entries]
            ),
            self._encode_json_section(
                [
                    {
                        "mount_type": mount.mount_type.value,
                        "destination": mount.destination.value,
                        "source": mount.source,
                        "propagation": mount.propagation.value,
                        "readonly": mount.readonly,
                        "options": [
                            [option_name, option_value]
                            for option_name, option_value in mount.options.entries
                        ],
                    }
                    for mount in runspec.mounts.mounts
                ]
            ),
            self._encode_text_section(runspec.hostname.value),
            self._encode_json_section(
                {
                    "cpu_quota_us": runspec.resources.cpu.quota_us,
                    "cpu_period_us": runspec.resources.cpu.period_us,
                    "memory_max_bytes": runspec.resources.memory.max_bytes,
                    "pids_max": runspec.resources.pids.max_pids,
                }
            ),
            self._encode_json_section(
                {
                    "effective_caps": sorted(runspec.security.effective_caps),
                    "permitted_caps": sorted(runspec.security.permitted_caps),
                    "bounding_caps": sorted(runspec.security.bounding_caps),
                    "no_new_privs": runspec.security.no_new_privs,
                    "seccomp_profile": (
                        runspec.security.seccomp_profile.value
                        if runspec.security.seccomp_profile is not None
                        else None
                    ),
                    "lsm_profile": runspec.security.lsm_profile,
                }
            ),
            self._encode_text_section(runspec.rootfs_path.value),
            self._encode_binary_section(runspec.canonical_hash),
            self._encode_text_section(runspec.compiled_at.isoformat()),
            self._encode_text_section(str(runspec.correlation_id)),
        )

        payload = b"".join(payload_sections)
        envelope_without_checksum = (
            RUNSPEC_MAGIC
            + RUNSPEC_FORMAT_VERSION.to_bytes(RUNSPEC_HEADER_SIZE - len(RUNSPEC_MAGIC), "big")
            + payload
        )
        checksum = compute_crc32c(envelope_without_checksum).to_bytes(
            RUNSPEC_CHECKSUM_SIZE,
            "big",
        )
        return envelope_without_checksum + checksum

    def deserialize(self, data: bytes) -> RunSpec:
        """Deserialize a canonical wire payload back into ``RunSpec``.

        Args:
            data: Full serialized envelope including checksum.

        Returns:
            The validated immutable aggregate.

        Raises:
            CorruptedPayloadError: When the payload is truncated, contains
                extra bytes, or cannot be decoded into a valid aggregate.
            ChecksumError: Propagated when the CRC32C does not match.
        """

        verify_serialized_checksum(data)
        payload = data[RUNSPEC_HEADER_SIZE:-RUNSPEC_CHECKSUM_SIZE]

        try:
            (
                process_section,
                environment_section,
                mounts_section,
                hostname_section,
                resources_section,
                security_section,
                rootfs_section,
                canonical_hash_section,
                compiled_at_section,
                correlation_id_section,
            ) = self._decode_sections(payload)

            process_data = self._decode_json_section(process_section)
            environment_data = self._decode_json_section(environment_section)
            mounts_data = self._decode_json_section(mounts_section)
            hostname_value = self._decode_text_section(hostname_section)
            resources_data = self._decode_json_section(resources_section)
            security_data = self._decode_json_section(security_section)
            rootfs_path_value = self._decode_text_section(rootfs_section)
            canonical_hash = self._decode_binary_section(canonical_hash_section)
            compiled_at = datetime.fromisoformat(
                self._decode_text_section(compiled_at_section)
            )
            correlation_id = UUID(self._decode_text_section(correlation_id_section))

            return RunSpec(
                process=ProcessSpec(
                    argv=tuple(process_data["argv"]),
                    uid=process_data["uid"],
                    gid=process_data["gid"],
                    cwd=AbsolutePath(process_data["cwd"]),
                ),
                environment=EnvironmentMap(
                    entries=tuple(
                        (entry[0], entry[1])
                        for entry in environment_data
                    )
                ),
                mounts=MountTopology(
                    mounts=tuple(self._decode_mount(mount_data) for mount_data in mounts_data)
                ),
                hostname=Hostname(hostname_value),
                resources=ResourceSpec(
                    cpu=CpuLimit(
                        quota_us=resources_data["cpu_quota_us"],
                        period_us=resources_data["cpu_period_us"],
                    ),
                    memory=MemoryLimit(max_bytes=resources_data["memory_max_bytes"]),
                    pids=PidsLimit(max_pids=resources_data["pids_max"]),
                ),
                security=SecurityEnvelope(
                    effective_caps=frozenset(security_data["effective_caps"]),
                    permitted_caps=frozenset(security_data["permitted_caps"]),
                    bounding_caps=frozenset(security_data["bounding_caps"]),
                    no_new_privs=security_data["no_new_privs"],
                    seccomp_profile=(
                        SeccompProfile(security_data["seccomp_profile"])
                        if security_data["seccomp_profile"] is not None
                        else None
                    ),
                    lsm_profile=security_data["lsm_profile"],
                ),
                rootfs_path=AbsolutePath(rootfs_path_value),
                canonical_hash=canonical_hash,
                compiled_at=compiled_at,
                correlation_id=correlation_id,
            )
        except CorruptedPayloadError:
            raise
        except Exception as exc:
            raise CorruptedPayloadError(
                "serialized payload does not describe a valid RunSpec"
            ) from exc

    def _decode_mount(self, mount_data: object) -> MountSpec:
        """Decode one mount entry from the mount section payload."""

        if not isinstance(mount_data, dict):
            raise CorruptedPayloadError("mount entry must decode to an object")

        options_payload = mount_data.get("options")
        if not isinstance(options_payload, list):
            raise CorruptedPayloadError("mount options must decode to a list")

        return MountSpec(
            mount_type=MountType(mount_data["mount_type"]),
            destination=AbsolutePath(mount_data["destination"]),
            source=mount_data["source"],
            propagation=Propagation(mount_data["propagation"]),
            readonly=mount_data["readonly"],
            options=MountOptions(
                entries=tuple((entry[0], entry[1]) for entry in options_payload)
            ),
        )

    def _decode_sections(self, payload: bytes) -> tuple[bytes, ...]:
        """Decode the fixed sequence of length-prefixed payload sections."""

        sections: list[bytes] = []
        offset = 0
        for _ in range(_EXPECTED_SECTION_COUNT):
            section, offset = self._read_one_section(payload, offset)
            sections.append(section)

        if offset != len(payload):
            raise CorruptedPayloadError(
                "serialized payload contains trailing bytes after the last section"
            )

        return tuple(sections)

    @staticmethod
    def _read_one_section(payload: bytes, offset: int) -> tuple[bytes, int]:
        """Read one length-prefixed section from the payload."""

        next_length_offset = offset + _SECTION_LENGTH_SIZE
        if next_length_offset > len(payload):
            raise CorruptedPayloadError("serialized payload is truncated mid-section")

        section_length = int.from_bytes(payload[offset:next_length_offset], "big")
        section_end = next_length_offset + section_length
        if section_end > len(payload):
            raise CorruptedPayloadError("serialized payload ends before a full section")

        return payload[next_length_offset:section_end], section_end

    @staticmethod
    def _encode_json_section(value: object) -> bytes:
        """Encode a structured section as deterministic JSON bytes."""

        encoded_section = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return len(encoded_section).to_bytes(_SECTION_LENGTH_SIZE, "big") + encoded_section

    @staticmethod
    def _decode_json_section(section: bytes) -> object:
        """Decode one JSON section and convert syntax failures into domain errors."""

        try:
            return json.loads(section.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptedPayloadError("serialized JSON section is invalid") from exc

    @staticmethod
    def _encode_text_section(value: str) -> bytes:
        """Encode a text section as UTF-8 with an explicit length prefix."""

        if not isinstance(value, str):
            raise TypeError("text sections must be strings.")
        encoded_section = value.encode("utf-8")
        return len(encoded_section).to_bytes(_SECTION_LENGTH_SIZE, "big") + encoded_section

    @staticmethod
    def _decode_text_section(section: bytes) -> str:
        """Decode one UTF-8 section."""

        try:
            return section.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptedPayloadError("serialized text section is not valid UTF-8") from exc

    @staticmethod
    def _encode_binary_section(value: bytes) -> bytes:
        """Encode a binary section with an explicit length prefix."""

        if not isinstance(value, bytes):
            raise TypeError("binary sections must be bytes.")
        return len(value).to_bytes(_SECTION_LENGTH_SIZE, "big") + value

    @staticmethod
    def _decode_binary_section(section: bytes) -> bytes:
        """Return a binary section verbatim."""

        return bytes(section)


__all__ = ["RunSpecSerializer"]
