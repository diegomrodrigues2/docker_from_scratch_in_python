"""Foundational domain value objects shared across the RunSpec aggregate."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ._validation import require_non_empty_string, require_string
from .exceptions import ValidationError


@dataclass(frozen=True, slots=True)
class AbsolutePath:
    """Canonical absolute POSIX path.

    The domain keeps only normalized paths so hashing, serialization, and ACL
    translations remain deterministic. Raw user input should be normalized before
    this type is instantiated.
    """

    value: str

    def __post_init__(self) -> None:
        self._validate_shape()

    def _validate_shape(self) -> None:
        path_value = require_non_empty_string("AbsolutePath.value", self.value)
        if "\\" in path_value:
            raise ValidationError(
                "AbsolutePath.value",
                "absolute path must use POSIX separators only",
            )
        if not path_value.startswith("/"):
            raise ValidationError(
                "AbsolutePath.value",
                "absolute path must start with '/'",
            )
        if path_value.startswith("//"):
            raise ValidationError(
                "AbsolutePath.value",
                "absolute path must use a single root slash",
            )
        if path_value != "/" and path_value.endswith("/"):
            raise ValidationError(
                "AbsolutePath.value",
                "absolute path must not end with '/' unless it is the root path",
            )

        segments = [] if path_value == "/" else path_value.split("/")[1:]
        if any(segment == "" for segment in segments):
            raise ValidationError(
                "AbsolutePath.value",
                "absolute path must not contain empty segments",
            )
        if "." in segments or ".." in segments:
            raise ValidationError(
                "AbsolutePath.value",
                "absolute path must already be normalized and must not contain '.' or '..'",
            )

    def __str__(self) -> str:
        return self.value

    def __fspath__(self) -> str:
        return self.value


def normalize_absolute_path(raw_path: str) -> AbsolutePath:
    """Normalize raw POSIX paths into the canonical AbsolutePath representation."""

    normalized_input = require_non_empty_string("normalize_absolute_path.raw_path", raw_path)
    if "\\" in normalized_input:
        raise ValidationError(
            "normalize_absolute_path.raw_path",
            "raw path must use POSIX separators only",
        )
    if not normalized_input.startswith("/"):
        raise ValidationError(
            "normalize_absolute_path.raw_path",
            "raw path must start with '/'",
        )

    # Canonicalization is intentionally lexical. We never touch the host
    # filesystem here because the compiler may run on a machine that does not
    # contain the container rootfs being described.
    normalized_path = posixpath.normpath(normalized_input)
    if normalized_path.startswith("//"):
        normalized_path = "/" + normalized_path.lstrip("/")

    return AbsolutePath(normalized_path)


@dataclass(frozen=True, slots=True)
class SeccompProfile:
    """Reference to a seccomp filter identifier or inline policy payload."""

    value: str

    def __post_init__(self) -> None:
        profile_value = require_string("SeccompProfile.value", self.value)
        if not profile_value.strip():
            raise ValidationError(
                "SeccompProfile.value",
                "seccomp profile must not be empty",
            )
        if profile_value != profile_value.strip():
            raise ValidationError(
                "SeccompProfile.value",
                "seccomp profile must not contain leading or trailing whitespace",
            )
        if "\x00" in profile_value:
            raise ValidationError(
                "SeccompProfile.value",
                "seccomp profile must not contain NUL bytes",
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class MountOptions:
    """Deterministic mount option entries.

    Options are stored as sorted `(name, value)` pairs. Flags such as `nodev` are
    represented with a `None` value, while keyed options such as `size=64m` use
    their string payload.
    """

    entries: tuple[tuple[str, str | None], ...] = ()

    def __post_init__(self) -> None:
        seen_names: set[str] = set()
        previous_name: str | None = None

        for index, entry in enumerate(self.entries):
            option_name, option_value = self._validate_entry_shape(entry, index)
            self._validate_option_name(option_name, index)
            self._validate_option_value(option_name, option_value)
            previous_name = self._validate_ordering(option_name, previous_name, seen_names)

    @staticmethod
    def _validate_entry_shape(entry: object, index: int) -> tuple[str, str | None]:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValidationError(
                "MountOptions.entries",
                f"entry at index {index} must be a (name, value) tuple",
            )
        option_name, option_value = entry
        return option_name, option_value

    @staticmethod
    def _validate_option_name(option_name: object, index: int) -> None:
        if not isinstance(option_name, str):
            raise ValidationError(
                "MountOptions.entries",
                f"option name at index {index} must be a string",
            )
        if not option_name:
            raise ValidationError(
                "MountOptions.entries",
                f"option name at index {index} must not be empty",
            )
        if option_name != option_name.strip():
            raise ValidationError(
                "MountOptions.entries",
                f"option '{option_name}' must not contain leading or trailing whitespace",
            )
        if "=" in option_name or "," in option_name:
            raise ValidationError(
                "MountOptions.entries",
                f"option name '{option_name}' contains reserved characters",
            )
        if "\x00" in option_name:
            raise ValidationError(
                "MountOptions.entries",
                f"option '{option_name}' must not contain NUL bytes",
            )

    @staticmethod
    def _validate_option_value(option_name: str, option_value: object) -> None:
        if option_value is not None and not isinstance(option_value, str):
            raise ValidationError(
                "MountOptions.entries",
                f"option '{option_name}' must map to a string or None",
            )
        if isinstance(option_value, str) and option_value != option_value.strip():
            raise ValidationError(
                "MountOptions.entries",
                f"option '{option_name}' must not contain padded values",
            )
        if isinstance(option_value, str) and "\x00" in option_value:
            raise ValidationError(
                "MountOptions.entries",
                f"option '{option_name}' must not contain NUL bytes",
            )

    @staticmethod
    def _validate_ordering(
        option_name: str,
        previous_name: str | None,
        seen_names: set[str],
    ) -> str:
        if previous_name is not None and option_name < previous_name:
            raise ValidationError(
                "MountOptions.entries",
                "mount options must be sorted lexicographically by name",
            )
        if option_name in seen_names:
            raise ValidationError(
                "MountOptions.entries",
                f"duplicate mount option '{option_name}' detected",
            )

        seen_names.add(option_name)
        return option_name

    @classmethod
    def from_mapping(cls, options: Mapping[str, str | None]) -> "MountOptions":
        """Build a deterministic mount option set from an arbitrary mapping."""

        if not isinstance(options, Mapping):
            raise TypeError("options must implement the Mapping protocol.")
        return cls(entries=tuple(sorted(options.items(), key=lambda item: item[0])))

    def to_mapping(self) -> Mapping[str, str | None]:
        """Expose the options as a read-only mapping when integration code needs one."""

        return MappingProxyType(dict(self.entries))

    def __bool__(self) -> bool:
        return bool(self.entries)


__all__ = ["AbsolutePath", "normalize_absolute_path", "SeccompProfile", "MountOptions"]
