from __future__ import annotations

import pytest

from runspec_contract.domain import SeccompProfile, SecurityEnvelope, ValidationError


def test_security_envelope_accepts_consistent_capability_sets() -> None:
    envelope = SecurityEnvelope(
        effective_caps=frozenset({"CAP_CHOWN"}),
        permitted_caps=frozenset({"CAP_CHOWN", "CAP_NET_BIND_SERVICE"}),
        bounding_caps=frozenset({"CAP_CHOWN", "CAP_NET_BIND_SERVICE", "CAP_SETUID"}),
        no_new_privs=True,
        seccomp_profile=SeccompProfile("runtime/default"),
        lsm_profile="docker-default",
    )

    assert envelope.no_new_privs is True
    assert envelope.seccomp_profile == SeccompProfile("runtime/default")
    assert envelope.lsm_profile == "docker-default"


def test_security_envelope_rejects_effective_capabilities_outside_permitted() -> None:
    with pytest.raises(ValidationError, match="effective capabilities must be contained"):
        SecurityEnvelope(
            effective_caps=frozenset({"CAP_SYS_ADMIN"}),
            permitted_caps=frozenset({"CAP_CHOWN"}),
            bounding_caps=frozenset({"CAP_CHOWN", "CAP_SYS_ADMIN"}),
            no_new_privs=True,
        )


def test_security_envelope_rejects_permitted_capabilities_outside_bounding() -> None:
    with pytest.raises(ValidationError, match="permitted capabilities must be contained"):
        SecurityEnvelope(
            effective_caps=frozenset({"CAP_CHOWN"}),
            permitted_caps=frozenset({"CAP_CHOWN", "CAP_SYS_ADMIN"}),
            bounding_caps=frozenset({"CAP_CHOWN"}),
            no_new_privs=True,
        )


def test_security_envelope_rejects_seccomp_without_no_new_privs() -> None:
    with pytest.raises(ValidationError, match="requires no_new_privs=True"):
        SecurityEnvelope(
            effective_caps=frozenset(),
            permitted_caps=frozenset(),
            bounding_caps=frozenset(),
            no_new_privs=False,
            seccomp_profile=SeccompProfile("runtime/default"),
        )


def test_security_envelope_rejects_blank_lsm_profile() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        SecurityEnvelope(
            effective_caps=frozenset(),
            permitted_caps=frozenset(),
            bounding_caps=frozenset(),
            no_new_privs=True,
            lsm_profile="",
        )
