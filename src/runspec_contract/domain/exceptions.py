"""Domain exception hierarchy for RunSpec compilation and execution."""

from __future__ import annotations


def _require_text(argument_name: str, value: str) -> str:
    """Validate string payload carried by structured exceptions."""

    if not isinstance(value, str):
        raise TypeError(f"{argument_name} must be a string.")
    if not value.strip():
        raise ValueError(f"{argument_name} must not be empty.")
    return value


def _require_int(argument_name: str, value: int) -> int:
    """Validate integer payload carried by structured exceptions."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{argument_name} must be an integer.")
    return value


def _require_bytes(argument_name: str, value: bytes) -> bytes:
    """Validate binary payload carried by structured exceptions."""

    if not isinstance(value, bytes):
        raise TypeError(f"{argument_name} must be bytes.")
    return value


class DomainError(Exception):
    """Base error for the RunSpec domain."""


class CompilationError(DomainError):
    """Error raised while compiling a RunSpec."""


class ValidationError(CompilationError):
    """Violation of a single value-object invariant."""

    def __init__(self, field: str, detail: str) -> None:
        self.field = _require_text("field", field)
        self.detail = _require_text("detail", detail)
        super().__init__(f"Validation failed for '{self.field}': {self.detail}")


class CrossValidationError(CompilationError):
    """Violation of an invariant spanning multiple value objects."""

    def __init__(self, fields: tuple[str, ...], detail: str) -> None:
        if not isinstance(fields, tuple):
            raise TypeError("fields must be a tuple of strings.")
        if not fields:
            raise ValueError("fields must not be empty.")

        normalized_fields = tuple(_require_text("fields entry", field) for field in fields)
        self.fields = normalized_fields
        self.detail = _require_text("detail", detail)
        field_list = ", ".join(normalized_fields)
        super().__init__(f"Cross-validation failed for [{field_list}]: {self.detail}")


class SerializationError(DomainError):
    """Error raised while serializing or deserializing a RunSpec."""


class ChecksumError(SerializationError):
    """Serialized payload checksum does not match the expected digest."""

    def __init__(self, expected: bytes, actual: bytes) -> None:
        self.expected = _require_bytes("expected", expected)
        self.actual = _require_bytes("actual", actual)
        super().__init__(
            "Checksum mismatch while decoding payload: "
            f"expected={self.expected.hex()} actual={self.actual.hex()}"
        )


class CorruptedPayloadError(SerializationError):
    """Serialized payload is truncated or structurally corrupted."""

    def __init__(self, detail: str) -> None:
        self.detail = _require_text("detail", detail)
        super().__init__(f"Corrupted payload: {self.detail}")


class KernelFeatureError(DomainError):
    """A required kernel feature is missing or unusable."""

    def __init__(self, feature: str, detail: str) -> None:
        self.feature = _require_text("feature", feature)
        self.detail = _require_text("detail", detail)
        super().__init__(f"Kernel feature '{self.feature}' is unavailable: {self.detail}")


class BootstrapError(DomainError):
    """The data plane failed during container bootstrap."""

    def __init__(self, phase: str, errno: int, detail: str) -> None:
        self.phase = _require_text("phase", phase)
        self.errno = _require_int("errno", errno)
        self.detail = _require_text("detail", detail)
        super().__init__(
            f"Bootstrap failed during phase '{self.phase}' with errno={self.errno}: {self.detail}"
        )


class ACLError(DomainError):
    """A typed error returned by the anti-corruption layer boundary."""

    def __init__(self, native_code: int, detail: str) -> None:
        self.native_code = _require_int("native_code", native_code)
        self.detail = _require_text("detail", detail)
        super().__init__(f"ACL error native_code={self.native_code}: {self.detail}")


__all__ = [
    "DomainError",
    "CompilationError",
    "ValidationError",
    "CrossValidationError",
    "SerializationError",
    "ChecksumError",
    "CorruptedPayloadError",
    "KernelFeatureError",
    "BootstrapError",
    "ACLError",
]

