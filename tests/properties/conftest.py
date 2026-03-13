"""Shared Hypothesis strategies for the RunSpec domain properties."""

from __future__ import annotations

import string
from datetime import timezone

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from runspec_contract.domain import (
    AbsolutePath,
    ContainerExited,
    ContainerForceKilled,
    ContainerOOMKilled,
    ContainerStarted,
    CpuLimit,
    EnvironmentMap,
    Hostname,
    MemoryLimit,
    MIN_MEMORY_BYTES,
    MountSpec,
    MountTopology,
    MountType,
    PidsLimit,
    ProcessSpec,
    Propagation,
    ResourceSpec,
    RunSpec,
    RunSpecCompiled,
    SeccompProfile,
    SecurityEnvelope,
)

_ASCII_TOKEN_ALPHABET = string.ascii_letters + string.digits + "-_./"
_PATH_SEGMENT_ALPHABET = string.ascii_lowercase + string.digits + "-_"
_ENV_KEY_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "_"
_HOSTNAME_ALPHABET = string.ascii_letters + string.digits + "-"
_CAPABILITY_NAMES = (
    "CAP_CHOWN",
    "CAP_NET_BIND_SERVICE",
    "CAP_SETUID",
    "CAP_SETGID",
    "CAP_SYS_CHROOT",
    "CAP_KILL",
    "CAP_DAC_OVERRIDE",
    "CAP_FOWNER",
)


def _token_strings(min_size: int = 1, max_size: int = 20) -> SearchStrategy[str]:
    return st.text(alphabet=_ASCII_TOKEN_ALPHABET, min_size=min_size, max_size=max_size)


def _path_segments() -> SearchStrategy[str]:
    return st.text(alphabet=_PATH_SEGMENT_ALPHABET, min_size=1, max_size=12).filter(
        lambda segment: segment not in {".", ".."}
    )


def _env_keys() -> SearchStrategy[str]:
    return st.text(alphabet=_ENV_KEY_ALPHABET, min_size=1, max_size=16)


def _env_values() -> SearchStrategy[str]:
    return st.text(alphabet=_ASCII_TOKEN_ALPHABET + " ", min_size=0, max_size=24)


def _security_profile_names() -> SearchStrategy[str]:
    return st.text(alphabet=string.ascii_letters + string.digits + "-_/", min_size=1, max_size=24)


@st.composite
def _absolute_paths(
    draw,
    *,
    min_segments: int = 0,
    max_segments: int = 5,
) -> AbsolutePath:
    segment_count = draw(st.integers(min_value=min_segments, max_value=max_segments))
    if segment_count == 0:
        return AbsolutePath("/")

    segments = draw(st.lists(_path_segments(), min_size=segment_count, max_size=segment_count))
    return AbsolutePath("/" + "/".join(segments))


def _descendant_paths(parent_path: AbsolutePath) -> SearchStrategy[AbsolutePath]:
    @st.composite
    def _strategy(draw) -> AbsolutePath:
        extra_segments = draw(st.lists(_path_segments(), min_size=0, max_size=3))
        if parent_path.value == "/":
            if not extra_segments:
                return AbsolutePath("/")
            return AbsolutePath("/" + "/".join(extra_segments))
        if not extra_segments:
            return parent_path
        return AbsolutePath(parent_path.value + "/" + "/".join(extra_segments))

    return _strategy()


def _aware_datetimes() -> SearchStrategy:
    return st.datetimes(timezones=st.just(timezone.utc))


def _canonical_hashes() -> SearchStrategy[bytes]:
    return st.binary(min_size=6, max_size=32)


@st.composite
def arbitrary_process_spec(draw) -> ProcessSpec:
    return ProcessSpec(
        argv=tuple(draw(st.lists(_token_strings(), min_size=1, max_size=5))),
        uid=draw(st.integers(min_value=0, max_value=65_535)),
        gid=draw(st.integers(min_value=0, max_value=65_535)),
        cwd=draw(_absolute_paths()),
    )


def arbitrary_environment_map() -> SearchStrategy[EnvironmentMap]:
    return st.dictionaries(_env_keys(), _env_values(), max_size=6).map(
        lambda entries: EnvironmentMap.compile_from_layers(entries)
    )


@st.composite
def arbitrary_mount_spec(draw) -> MountSpec:
    mount_type = draw(st.sampled_from((MountType.BIND, MountType.TMPFS)))
    destination = draw(_absolute_paths(min_segments=1))
    propagation = draw(st.sampled_from((Propagation.RPRIVATE, Propagation.RSLAVE)))
    readonly = draw(st.booleans())

    if mount_type is MountType.BIND:
        source = draw(_absolute_paths(min_segments=1).map(lambda path: path.value))
    else:
        source = None

    return MountSpec(
        mount_type=mount_type,
        destination=destination,
        source=source,
        propagation=propagation,
        readonly=readonly,
    )


@st.composite
def arbitrary_mount_topology(draw) -> MountTopology:
    destinations = draw(
        st.lists(_absolute_paths(min_segments=1), min_size=0, max_size=5, unique_by=lambda path: path.value)
    )

    mounts: list[MountSpec] = []
    for destination in destinations:
        mount_type = draw(st.sampled_from((MountType.BIND, MountType.TMPFS)))
        propagation = draw(st.sampled_from((Propagation.RPRIVATE, Propagation.RSLAVE)))
        readonly = draw(st.booleans())
        if mount_type is MountType.BIND:
            source = draw(_absolute_paths(min_segments=1).map(lambda path: path.value))
        else:
            source = None
        mounts.append(
            MountSpec(
                mount_type=mount_type,
                destination=destination,
                source=source,
                propagation=propagation,
                readonly=readonly,
            )
        )

    return MountTopology(mounts=tuple(mounts))


def arbitrary_hostname() -> SearchStrategy[Hostname]:
    hostname_pattern = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?$"
    return st.from_regex(hostname_pattern, fullmatch=True).map(Hostname)


@st.composite
def arbitrary_cpu_limit(draw) -> CpuLimit:
    period = draw(st.integers(min_value=1, max_value=1_000_000))
    quota = draw(st.integers(min_value=1, max_value=period))
    return CpuLimit(quota_us=quota, period_us=period)


def arbitrary_memory_limit() -> SearchStrategy[MemoryLimit]:
    return st.integers(min_value=MIN_MEMORY_BYTES, max_value=1_073_741_824).map(MemoryLimit)


def arbitrary_pids_limit() -> SearchStrategy[PidsLimit]:
    return st.integers(min_value=1, max_value=4_096).map(PidsLimit)


def arbitrary_resource_spec() -> SearchStrategy[ResourceSpec]:
    return st.builds(
        ResourceSpec,
        cpu=arbitrary_cpu_limit(),
        memory=arbitrary_memory_limit(),
        pids=arbitrary_pids_limit(),
    )


@st.composite
def arbitrary_security_envelope(draw) -> SecurityEnvelope:
    bounding_caps = frozenset(
        draw(st.lists(st.sampled_from(_CAPABILITY_NAMES), unique=True, max_size=len(_CAPABILITY_NAMES)))
    )

    if bounding_caps:
        permitted_caps = frozenset(
            draw(st.lists(st.sampled_from(sorted(bounding_caps)), unique=True, max_size=len(bounding_caps)))
        )
    else:
        permitted_caps = frozenset()

    if permitted_caps:
        effective_caps = frozenset(
            draw(st.lists(st.sampled_from(sorted(permitted_caps)), unique=True, max_size=len(permitted_caps)))
        )
    else:
        effective_caps = frozenset()

    use_seccomp = draw(st.booleans())
    no_new_privs = True if use_seccomp else draw(st.booleans())
    seccomp_profile = (
        SeccompProfile(draw(_security_profile_names()))
        if use_seccomp
        else None
    )
    lsm_profile = draw(st.one_of(st.none(), _security_profile_names()))

    return SecurityEnvelope(
        effective_caps=effective_caps,
        permitted_caps=permitted_caps,
        bounding_caps=bounding_caps,
        no_new_privs=no_new_privs,
        seccomp_profile=seccomp_profile,
        lsm_profile=lsm_profile,
    )


@st.composite
def arbitrary_runspec(draw) -> RunSpec:
    rootfs_path = draw(_absolute_paths(min_segments=1))
    process = ProcessSpec(
        argv=tuple(draw(st.lists(_token_strings(), min_size=1, max_size=5))),
        uid=draw(st.integers(min_value=0, max_value=65_535)),
        gid=draw(st.integers(min_value=0, max_value=65_535)),
        cwd=draw(_descendant_paths(rootfs_path)),
    )

    return RunSpec(
        process=process,
        environment=draw(arbitrary_environment_map()),
        mounts=draw(arbitrary_mount_topology()),
        hostname=draw(arbitrary_hostname()),
        resources=draw(arbitrary_resource_spec()),
        security=draw(arbitrary_security_envelope()),
        rootfs_path=rootfs_path,
        canonical_hash=draw(_canonical_hashes()),
        compiled_at=draw(_aware_datetimes()),
        correlation_id=draw(st.uuids()),
    )


def invalid_argv() -> SearchStrategy[tuple[str, ...]]:
    return st.just(())


def invalid_env_keys() -> SearchStrategy[str]:
    return st.one_of(
        st.just(""),
        st.just("="),
        st.text(alphabet=_ENV_KEY_ALPHABET + "=", min_size=1, max_size=16).filter(
            lambda key: "=" in key
        ),
    )


def invalid_hostnames() -> SearchStrategy[str]:
    return st.one_of(
        st.text(alphabet=_HOSTNAME_ALPHABET.replace("-", ""), min_size=1, max_size=20).map(
            lambda value: f"-{value}"
        ),
        st.text(alphabet=_HOSTNAME_ALPHABET.replace("-", ""), min_size=1, max_size=20).map(
            lambda value: f"{value}-"
        ),
        st.text(alphabet=string.ascii_letters + string.digits + "_", min_size=1, max_size=20).filter(
            lambda value: "_" in value
        ),
        st.text(alphabet=_HOSTNAME_ALPHABET, min_size=65, max_size=80).filter(
            lambda value: not value.startswith("-") and not value.endswith("-")
        ),
    )


def arbitrary_env_layers() -> SearchStrategy[tuple[dict[str, str], ...]]:
    env_layer = st.dictionaries(_env_keys(), _env_values(), max_size=5)
    return st.tuples(env_layer, env_layer, env_layer, env_layer)


@st.composite
def arbitrary_parent_limits(draw) -> tuple[int, int, int, int]:
    period = draw(st.integers(min_value=1, max_value=1_000_000))
    quota = draw(st.integers(min_value=1, max_value=period))
    memory_max = draw(st.integers(min_value=MIN_MEMORY_BYTES, max_value=1_073_741_824))
    pids_max = draw(st.integers(min_value=1, max_value=4_096))
    return (quota, period, memory_max, pids_max)


def arbitrary_domain_event() -> SearchStrategy[
    RunSpecCompiled | ContainerStarted | ContainerExited | ContainerOOMKilled | ContainerForceKilled
]:
    event_id = st.uuids()
    occurred_at = _aware_datetimes()
    correlation_id = st.uuids()
    container_id = st.uuids()
    runspec_hash = st.binary(min_size=1, max_size=32)

    return st.one_of(
        st.builds(
            RunSpecCompiled,
            event_id=event_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            runspec_hash=runspec_hash,
        ),
        st.builds(
            ContainerStarted,
            event_id=event_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            container_id=container_id,
            runspec_hash=runspec_hash,
            pidfd=st.integers(min_value=0, max_value=1_024),
        ),
        st.builds(
            ContainerExited,
            event_id=event_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            container_id=container_id,
            exit_code=st.integers(min_value=0, max_value=255),
            signal=st.one_of(st.none(), st.integers(min_value=1, max_value=64)),
            duration_seconds=st.floats(
                min_value=0.0,
                max_value=10_000.0,
                allow_nan=False,
                allow_infinity=False,
            ),
        ),
        st.builds(
            ContainerOOMKilled,
            event_id=event_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            container_id=container_id,
            memory_limit=st.integers(min_value=MIN_MEMORY_BYTES, max_value=1_073_741_824),
        ),
        st.builds(
            ContainerForceKilled,
            event_id=event_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            container_id=container_id,
            reason=_token_strings(),
        ),
    )


__all__ = [
    "arbitrary_cpu_limit",
    "arbitrary_domain_event",
    "arbitrary_env_layers",
    "arbitrary_environment_map",
    "arbitrary_hostname",
    "arbitrary_memory_limit",
    "arbitrary_mount_spec",
    "arbitrary_mount_topology",
    "arbitrary_parent_limits",
    "arbitrary_pids_limit",
    "arbitrary_process_spec",
    "arbitrary_resource_spec",
    "arbitrary_runspec",
    "arbitrary_security_envelope",
    "invalid_argv",
    "invalid_env_keys",
    "invalid_hostnames",
]
