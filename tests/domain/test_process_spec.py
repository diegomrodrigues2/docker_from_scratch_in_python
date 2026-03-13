from __future__ import annotations

import pytest

from runspec_contract.domain import AbsolutePath, ProcessSpec, ValidationError


def test_process_spec_accepts_valid_process_configuration() -> None:
    process_spec = ProcessSpec(
        argv=("/bin/sh", "-c", "echo ok"),
        uid=1000,
        gid=1000,
        cwd=AbsolutePath("/workspace"),
    )

    assert process_spec.argv == ("/bin/sh", "-c", "echo ok")
    assert process_spec.uid == 1000
    assert process_spec.gid == 1000
    assert process_spec.cwd == AbsolutePath("/workspace")


def test_process_spec_rejects_empty_argv() -> None:
    with pytest.raises(ValidationError, match="argv must contain at least one element"):
        ProcessSpec(argv=(), uid=0, gid=0, cwd=AbsolutePath("/"))


def test_process_spec_rejects_non_string_arguments() -> None:
    with pytest.raises(ValidationError, match="all argv elements must be strings"):
        ProcessSpec(  # type: ignore[arg-type]
            argv=("/bin/sh", 1),
            uid=0,
            gid=0,
            cwd=AbsolutePath("/"),
        )


def test_process_spec_rejects_negative_uid() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to zero"):
        ProcessSpec(argv=("/bin/sh",), uid=-1, gid=0, cwd=AbsolutePath("/"))


def test_process_spec_rejects_negative_gid() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to zero"):
        ProcessSpec(argv=("/bin/sh",), uid=0, gid=-1, cwd=AbsolutePath("/"))
