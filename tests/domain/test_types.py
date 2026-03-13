from __future__ import annotations

from runspec_contract.domain import AbsolutePath


def test_absolute_path_accepts_root_path() -> None:
    root_path = AbsolutePath("/")

    assert root_path.value == "/"
