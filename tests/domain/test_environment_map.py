from __future__ import annotations

import pytest

from runspec_contract.domain import EnvironmentMap, ValidationError


def test_environment_map_accepts_sorted_entries() -> None:
    environment = EnvironmentMap(entries=(("HOME", "/root"), ("PATH", "/usr/bin")))

    assert environment.entries == (("HOME", "/root"), ("PATH", "/usr/bin"))


def test_environment_map_compile_from_layers_applies_precedence_and_sorts() -> None:
    environment = EnvironmentMap.compile_from_layers(
        {"HOME": "/image", "PATH": "/image/bin"},
        {"PATH": "/platform/bin"},
        {"LANG": "C.UTF-8"},
        {"PATH": "/user/bin"},
    )

    assert environment.entries == (
        ("HOME", "/image"),
        ("LANG", "C.UTF-8"),
        ("PATH", "/user/bin"),
    )


def test_environment_map_rejects_empty_key() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        EnvironmentMap(entries=(("", "value"),))


def test_environment_map_rejects_key_with_equals_sign() -> None:
    with pytest.raises(ValidationError, match="must not contain '='"):
        EnvironmentMap(entries=(("BAD=KEY", "value"),))


def test_environment_map_rejects_unsorted_entries() -> None:
    with pytest.raises(ValidationError, match="sorted lexicographically"):
        EnvironmentMap(entries=(("PATH", "/usr/bin"), ("HOME", "/root")))


def test_environment_map_rejects_duplicate_keys() -> None:
    with pytest.raises(ValidationError, match="duplicate environment variable key 'PATH'"):
        EnvironmentMap(entries=(("PATH", "/usr/bin"), ("PATH", "/custom/bin")))
