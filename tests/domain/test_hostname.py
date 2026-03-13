from __future__ import annotations

import pytest

from runspec_contract.domain import Hostname, ValidationError


def test_hostname_accepts_valid_value() -> None:
    hostname = Hostname("container-01")

    assert hostname.value == "container-01"


def test_hostname_from_hash_uses_first_twelve_hex_characters() -> None:
    hostname = Hostname.from_hash(bytes.fromhex("0123456789abcdef0123456789abcdef"))

    assert hostname.value == "0123456789ab"


def test_hostname_rejects_leading_hyphen() -> None:
    with pytest.raises(ValidationError, match="cannot start or end with '-'"):
        Hostname("-container")


def test_hostname_rejects_invalid_characters() -> None:
    with pytest.raises(ValidationError, match="must use only"):
        Hostname("container_name")


def test_hostname_rejects_more_than_sixty_four_characters() -> None:
    with pytest.raises(ValidationError, match="at most 64 characters"):
        Hostname("a" * 65)
