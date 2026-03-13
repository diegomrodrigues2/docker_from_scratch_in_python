"""Shared primitive validation helpers for the RunSpec domain.

The domain model keeps invariants close to each aggregate or value object, but
the low-level "is this a UUID / integer / aware datetime?" checks live here so
error messages stay uniform across modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from .exceptions import ValidationError


def require_instance(
    field_name: str,
    value: object,
    expected_type: type[Any] | tuple[type[Any], ...],
    detail: str,
) -> None:
    """Ensure callers pass already-materialized domain objects where required."""

    if not isinstance(value, expected_type):
        raise ValidationError(field_name, detail)


def require_uuid(field_name: str, value: UUID) -> UUID:
    """Validate identity values shared by aggregates and domain events."""

    if not isinstance(value, UUID):
        raise ValidationError(field_name, "value must be a UUID")
    return value


def require_aware_datetime(field_name: str, value: datetime) -> datetime:
    """Require timezone-aware timestamps so audit data remains unambiguous."""

    if not isinstance(value, datetime):
        raise ValidationError(field_name, "value must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(field_name, "datetime must be timezone-aware")
    return value


def require_bytes(field_name: str, value: bytes, *, allow_empty: bool = False) -> bytes:
    """Validate binary payloads such as hashes and checksums."""

    if not isinstance(value, bytes):
        raise ValidationError(field_name, "value must be bytes")
    if not allow_empty and not value:
        raise ValidationError(field_name, "value must not be empty")
    return value


def require_int(field_name: str, value: int) -> int:
    """Validate integer payloads while rejecting booleans masquerading as ints."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(field_name, "value must be an integer")
    return value


def require_non_negative_int(field_name: str, value: int) -> int:
    """Validate integer fields where zero is a valid lower bound."""

    integer_value = require_int(field_name, value)
    if integer_value < 0:
        raise ValidationError(field_name, "value must be greater than or equal to zero")
    return integer_value


def require_positive_int(field_name: str, value: int) -> int:
    """Validate integer fields that must remain strictly positive."""

    integer_value = require_int(field_name, value)
    if integer_value <= 0:
        raise ValidationError(field_name, "value must be greater than zero")
    return integer_value


def require_non_negative_number(field_name: str, value: float | int) -> float:
    """Validate numeric payloads stored canonically as floating-point values."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(field_name, "value must be a number")
    numeric_value = float(value)
    if numeric_value < 0.0:
        raise ValidationError(field_name, "value must be greater than or equal to zero")
    return numeric_value


def require_string(field_name: str, value: str) -> str:
    """Validate plain string payloads used by the domain model."""

    if not isinstance(value, str):
        raise ValidationError(field_name, "value must be a string")
    return value


def require_non_empty_string(field_name: str, value: str) -> str:
    """Validate string fields that must carry at least one character."""

    text_value = require_string(field_name, value)
    if not text_value:
        raise ValidationError(field_name, "value must not be empty")
    return text_value


def require_bool(field_name: str, value: bool) -> bool:
    """Keep boolean domain flags explicit instead of accepting truthy integers."""

    if not isinstance(value, bool):
        raise ValidationError(field_name, "value must be a boolean")
    return value
