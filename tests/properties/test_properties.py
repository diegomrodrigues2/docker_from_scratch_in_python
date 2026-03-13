from __future__ import annotations

import string
from dataclasses import dataclass
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from uuid import UUID

import pytest
from hypothesis import assume, given, settings, strategies as st

from runspec_contract.application import (
    AdmissionPipeline,
    DefaultPathNormalizationPolicy,
    DefaultPolicyEnrichmentPolicy,
    DefaultPrecedenceResolutionPolicy,
    ImageMetadata,
    MountRequest,
    PlatformDefaults,
    PolicyConfig,
    RawCompilationInputs,
    UserOverrides,
)
from runspec_contract.domain import (
    ACLError,
    AbsolutePath,
    CpuLimit,
    DomainError,
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
    SeccompProfile,
    SecurityEnvelope,
    normalize_absolute_path,
)
from runspec_contract.infrastructure import CythonBridge, KernelCapabilities

from .conftest import (
    arbitrary_cpu_limit,
    arbitrary_domain_event,
    arbitrary_env_layers,
    arbitrary_environment_map,
    arbitrary_hostname,
    arbitrary_memory_limit,
    arbitrary_mount_spec,
    arbitrary_mount_topology,
    arbitrary_parent_limits,
    arbitrary_pids_limit,
    arbitrary_process_spec,
    arbitrary_resource_spec,
    arbitrary_runspec,
    arbitrary_security_envelope,
    invalid_argv,
    invalid_env_keys,
    invalid_hostnames,
)

PROPERTY_SETTINGS = settings(max_examples=100, deadline=None)
_SAFE_TEXT_ALPHABET = string.ascii_letters + string.digits + "-_./ "
_ENV_KEY_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "_"
_PATH_SEGMENT_ALPHABET = string.ascii_lowercase + string.digits + "-_"


def _immutable_domain_objects():
    return st.one_of(
        arbitrary_process_spec(),
        arbitrary_environment_map(),
        arbitrary_mount_spec(),
        arbitrary_mount_topology(),
        arbitrary_hostname(),
        arbitrary_cpu_limit(),
        arbitrary_memory_limit(),
        arbitrary_pids_limit(),
        arbitrary_resource_spec(),
        arbitrary_security_envelope(),
        arbitrary_runspec(),
    )


def _valid_env_key_strategy() -> st.SearchStrategy[str]:
    return st.text(alphabet=_ENV_KEY_ALPHABET, min_size=1, max_size=16)


def _valid_env_value_strategy() -> st.SearchStrategy[str]:
    return st.text(alphabet=_SAFE_TEXT_ALPHABET, min_size=0, max_size=20)


def _capability_name_strategy() -> st.SearchStrategy[str]:
    return st.text(alphabet=string.ascii_uppercase + "_", min_size=3, max_size=16).map(
        lambda suffix: f"CAP_{suffix}"
    )


def _raw_absolute_paths_with_traversal() -> st.SearchStrategy[str]:
    path_components = st.one_of(
        st.just(""),
        st.just("."),
        st.just(".."),
        st.text(alphabet=_PATH_SEGMENT_ALPHABET, min_size=1, max_size=8).filter(
            lambda segment: segment not in {".", ".."}
        ),
    )
    return st.lists(path_components, min_size=1, max_size=8).map(
        lambda segments: "/" + "/".join(segments)
    )


def _relative_paths() -> st.SearchStrategy[str]:
    return st.lists(
        st.text(alphabet=_PATH_SEGMENT_ALPHABET, min_size=1, max_size=8).filter(
            lambda segment: segment not in {".", ".."}
        ),
        min_size=1,
        max_size=6,
    ).map("/".join)


def _absolute_path_strings(
    *,
    min_segments: int = 1,
    max_segments: int = 5,
) -> st.SearchStrategy[str]:
    return st.lists(
        st.text(alphabet=_PATH_SEGMENT_ALPHABET, min_size=1, max_size=8).filter(
            lambda segment: segment not in {".", ".."}
        ),
        min_size=min_segments,
        max_size=max_segments,
    ).map(lambda segments: "/" + "/".join(segments))


def _hostname_strings() -> st.SearchStrategy[str]:
    return st.from_regex(
        r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,20}[A-Za-z0-9])?$",
        fullmatch=True,
    )


def _mount_request_strategy() -> st.SearchStrategy[MountRequest]:
    bind_mounts = st.builds(
        MountRequest,
        mount_type=st.just(MountType.BIND),
        destination=_absolute_path_strings(),
        source=_absolute_path_strings(),
        propagation=st.sampled_from((Propagation.RPRIVATE, Propagation.RSLAVE)),
        readonly=st.booleans(),
        options=st.dictionaries(
            st.sampled_from(["nodev", "nosuid", "size"]),
            st.one_of(st.none(), st.sampled_from(["16m", "64m", "128m"])),
            max_size=3,
        ),
    )
    tmpfs_mounts = st.builds(
        MountRequest,
        mount_type=st.just(MountType.TMPFS),
        destination=_absolute_path_strings(),
        source=st.just(None),
        propagation=st.sampled_from((Propagation.RPRIVATE, Propagation.RSLAVE)),
        readonly=st.booleans(),
        options=st.dictionaries(
            st.sampled_from(["nodev", "nosuid", "size"]),
            st.one_of(st.none(), st.sampled_from(["16m", "64m", "128m"])),
            max_size=3,
        ),
    )
    return st.one_of(bind_mounts, tmpfs_mounts)


@dataclass(frozen=True, slots=True)
class _FakeKernelCapabilities:
    kernel_version: tuple[int, int, int] = (6, 8, 0)
    has_cgroups_v2: bool = True
    has_clone3: bool = True
    has_pidfd: bool = True
    has_user_ns: bool = True
    has_pid_ns: bool = True
    has_mount_ns: bool = True
    has_uts_ns: bool = True
    has_net_ns: bool = True
    has_seccomp: bool = True
    has_pivot_root: bool = True
    available_cgroup_controllers: frozenset[str] = frozenset({"cpu", "memory", "pids"})

    def validate_minimum_requirements(self) -> None:
        return None


@st.composite
def _raw_compilation_inputs(draw) -> RawCompilationInputs:
    rootfs_path = draw(_absolute_path_strings(min_segments=2, max_segments=5))
    image_cwd = draw(_absolute_path_strings())
    override_cwd = draw(st.one_of(st.none(), _absolute_path_strings()))
    cpu_period = draw(st.integers(min_value=1_000, max_value=1_000_000))
    cpu_quota = draw(st.integers(min_value=1, max_value=cpu_period))
    memory_limit = draw(st.integers(min_value=MIN_MEMORY_BYTES, max_value=64 * 1024 * 1024))
    policy_caps = draw(
        st.one_of(
            st.none(),
            st.lists(
                st.sampled_from(
                    [
                        "CAP_CHOWN",
                        "CAP_SETUID",
                        "CAP_SETGID",
                        "CAP_NET_BIND_SERVICE",
                    ]
                ),
                unique=True,
                max_size=4,
            ).map(frozenset),
        )
    )
    mounts = draw(
        st.lists(
            _mount_request_strategy(),
            max_size=4,
            unique_by=lambda mount: mount.destination,
        ).map(tuple)
    )

    return RawCompilationInputs(
        image_metadata=ImageMetadata(
            argv=tuple(
                draw(
                    st.lists(
                        st.text(
                            alphabet=_SAFE_TEXT_ALPHABET.replace(" ", ""),
                            min_size=1,
                            max_size=12,
                        ),
                        min_size=1,
                        max_size=4,
                    )
                )
            ),
            uid=draw(st.integers(min_value=0, max_value=65_535)),
            gid=draw(st.integers(min_value=0, max_value=65_535)),
            cwd=image_cwd,
            environment=draw(
                st.dictionaries(
                    _valid_env_key_strategy(),
                    _valid_env_value_strategy(),
                    max_size=5,
                )
            ),
            cpu_quota_us=cpu_quota,
            cpu_period_us=cpu_period,
            memory_max_bytes=memory_limit,
        ),
        platform_defaults=PlatformDefaults(
            environment=draw(
                st.dictionaries(
                    _valid_env_key_strategy(),
                    _valid_env_value_strategy(),
                    max_size=5,
                )
            ),
            pids_max=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=512))),
            rootfs_path=rootfs_path,
        ),
        policy_config=PolicyConfig(
            environment=draw(
                st.dictionaries(
                    _valid_env_key_strategy(),
                    _valid_env_value_strategy(),
                    max_size=5,
                )
            ),
            effective_caps=policy_caps,
            permitted_caps=policy_caps,
            bounding_caps=policy_caps,
            seccomp_profile=draw(
                st.one_of(
                    st.none(),
                    st.sampled_from(["runtime/default", "runtime/restricted"]),
                )
            ),
        ),
        user_overrides=UserOverrides(
            cwd=override_cwd,
            environment=draw(
                st.dictionaries(
                    _valid_env_key_strategy(),
                    _valid_env_value_strategy(),
                    max_size=5,
                )
            ),
            mounts=mounts,
            hostname=draw(st.one_of(st.none(), _hostname_strings())),
        ),
        compiled_at=draw(st.datetimes(timezones=st.just(timezone.utc))),
        correlation_id=draw(st.uuids()),
    )


def _outside_rootfs_path(rootfs_path: AbsolutePath) -> AbsolutePath:
    candidate = "/__outside_rootfs__"
    if rootfs_path.value == candidate or rootfs_path.value.startswith(candidate + "/"):
        candidate = "/__alternate_outside_rootfs__"
    return AbsolutePath(candidate)


def _copy_mount(mount_spec: MountSpec) -> MountSpec:
    return MountSpec(
        mount_type=mount_spec.mount_type,
        destination=mount_spec.destination,
        source=mount_spec.source,
        propagation=mount_spec.propagation,
        readonly=mount_spec.readonly,
        options=mount_spec.options,
    )


def _first_field_name(instance: object) -> str:
    return fields(instance)[0].name


def _valid_environment_with_precedence(
    layers: tuple[dict[str, str], ...],
    shared_key: str,
    values: tuple[str, str, str, str],
) -> EnvironmentMap:
    image_layer, platform_layer, policy_layer, user_layer = layers
    image_value, platform_value, policy_value, user_value = values
    return EnvironmentMap.compile_from_layers(
        {**image_layer, shared_key: image_value},
        {**platform_layer, shared_key: platform_value},
        {**policy_layer, shared_key: policy_value},
        {**user_layer, shared_key: user_value},
    )


# Feature: runspec-contract, Property 3: Determinismo de Compilacao
@PROPERTY_SETTINGS
@given(raw_inputs=_raw_compilation_inputs())
def test_property_3_runspec_compilation_is_deterministic(
    raw_inputs: RawCompilationInputs,
) -> None:
    pipeline = AdmissionPipeline(
        precedence=DefaultPrecedenceResolutionPolicy(),
        enrichment=DefaultPolicyEnrichmentPolicy(),
        normalization=DefaultPathNormalizationPolicy(),
    )
    kernel_caps = _FakeKernelCapabilities()

    first_runspec = pipeline.compile(raw_inputs, kernel_caps)
    second_runspec = pipeline.compile(raw_inputs, kernel_caps)

    assert first_runspec.canonical_hash == second_runspec.canonical_hash


# Feature: runspec-contract, Property 4: Precedência de Camadas do EnvironmentMap
@PROPERTY_SETTINGS
@given(
    layers=arbitrary_env_layers(),
    shared_key=_valid_env_key_strategy(),
    values=st.tuples(
        _valid_env_value_strategy(),
        _valid_env_value_strategy(),
        _valid_env_value_strategy(),
        _valid_env_value_strategy(),
    ),
)
def test_property_4_environment_map_precedence(
    layers: tuple[dict[str, str], ...],
    shared_key: str,
    values: tuple[str, str, str, str],
) -> None:
    compiled = _valid_environment_with_precedence(layers, shared_key, values)

    assert dict(compiled.entries)[shared_key] == values[3]
    assert compiled.entries == tuple(sorted(compiled.entries, key=lambda item: item[0]))


# Feature: runspec-contract, Property 5: Imutabilidade do RunSpec e Value Objects
@PROPERTY_SETTINGS
@given(domain_object=_immutable_domain_objects())
def test_property_5_immutability(domain_object: object) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(domain_object, _first_field_name(domain_object), getattr(domain_object, _first_field_name(domain_object)))


# Feature: runspec-contract, Property 6: Validação de Invariantes dos Value Objects
@PROPERTY_SETTINGS
@given(
    argv=invalid_argv(),
    env_key=invalid_env_keys(),
    hostname=invalid_hostnames(),
    cpu_period=st.integers(min_value=1, max_value=1_000_000),
    memory_limit=st.integers(min_value=0, max_value=MIN_MEMORY_BYTES - 1),
    pids_limit=st.integers(min_value=-64, max_value=0),
)
def test_property_6_invalid_value_object_inputs_raise_domain_error(
    argv: tuple[str, ...],
    env_key: str,
    hostname: str,
    cpu_period: int,
    memory_limit: int,
    pids_limit: int,
) -> None:
    with pytest.raises(DomainError):
        ProcessSpec(argv=argv, uid=0, gid=0, cwd=AbsolutePath("/"))

    with pytest.raises(DomainError):
        EnvironmentMap(entries=((env_key, "value"),))

    duplicate_mount = MountSpec(
        mount_type=MountType.BIND,
        destination=AbsolutePath("/duplicated"),
        source="/source",
    )
    with pytest.raises(DomainError):
        MountTopology(mounts=(duplicate_mount, _copy_mount(duplicate_mount)))

    with pytest.raises(DomainError):
        Hostname(hostname)

    with pytest.raises(DomainError):
        CpuLimit(quota_us=cpu_period + 1, period_us=cpu_period)

    with pytest.raises(DomainError):
        MemoryLimit(max_bytes=memory_limit)

    with pytest.raises(DomainError):
        PidsLimit(max_pids=pids_limit)


# Feature: runspec-contract, Property 7: Invariante de Subconjunto de Capabilities
@PROPERTY_SETTINGS
@given(envelope=arbitrary_security_envelope(), extra_capability=_capability_name_strategy())
def test_property_7_capability_sets_follow_subset_invariant(
    envelope: SecurityEnvelope,
    extra_capability: str,
) -> None:
    assert envelope.effective_caps <= envelope.permitted_caps <= envelope.bounding_caps

    assume(extra_capability not in envelope.bounding_caps)

    with pytest.raises(DomainError):
        SecurityEnvelope(
            effective_caps=envelope.effective_caps | frozenset({extra_capability}),
            permitted_caps=envelope.permitted_caps,
            bounding_caps=envelope.bounding_caps,
            no_new_privs=envelope.no_new_privs,
            seccomp_profile=envelope.seccomp_profile,
            lsm_profile=envelope.lsm_profile,
        )

    with pytest.raises(DomainError):
        SecurityEnvelope(
            effective_caps=envelope.effective_caps,
            permitted_caps=envelope.permitted_caps | frozenset({extra_capability}),
            bounding_caps=envelope.bounding_caps,
            no_new_privs=envelope.no_new_privs,
            seccomp_profile=envelope.seccomp_profile,
            lsm_profile=envelope.lsm_profile,
        )


# Feature: runspec-contract, Property 8: Seccomp Requer no_new_privs
@PROPERTY_SETTINGS
@given(envelope=arbitrary_security_envelope())
def test_property_8_seccomp_requires_no_new_privs(envelope: SecurityEnvelope) -> None:
    valid_envelope = SecurityEnvelope(
        effective_caps=envelope.effective_caps,
        permitted_caps=envelope.permitted_caps,
        bounding_caps=envelope.bounding_caps,
        no_new_privs=True,
        seccomp_profile=SeccompProfile("runtime/default"),
        lsm_profile=envelope.lsm_profile,
    )

    assert valid_envelope.no_new_privs is True
    assert valid_envelope.seccomp_profile == SeccompProfile("runtime/default")

    with pytest.raises(DomainError):
        SecurityEnvelope(
            effective_caps=envelope.effective_caps,
            permitted_caps=envelope.permitted_caps,
            bounding_caps=envelope.bounding_caps,
            no_new_privs=False,
            seccomp_profile=SeccompProfile("runtime/default"),
            lsm_profile=envelope.lsm_profile,
        )


# Feature: runspec-contract, Property 9: Normalização de Caminhos é Idempotente
@PROPERTY_SETTINGS
@given(raw_absolute_path=_raw_absolute_paths_with_traversal(), relative_path=_relative_paths())
def test_property_9_absolute_path_normalization_is_idempotent(
    raw_absolute_path: str,
    relative_path: str,
) -> None:
    normalized = normalize_absolute_path(raw_absolute_path)

    assert normalize_absolute_path(normalized.value) == normalized

    with pytest.raises(DomainError):
        normalize_absolute_path(relative_path)


# Feature: runspec-contract, Property 10: CWD Contido no RootFS
@PROPERTY_SETTINGS
@given(valid_runspec=arbitrary_runspec())
def test_property_10_cwd_must_be_contained_inside_rootfs(valid_runspec: RunSpec) -> None:
    outside_cwd = _outside_rootfs_path(valid_runspec.rootfs_path)
    invalid_process = ProcessSpec(
        argv=valid_runspec.process.argv,
        uid=valid_runspec.process.uid,
        gid=valid_runspec.process.gid,
        cwd=outside_cwd,
    )

    with pytest.raises(DomainError):
        RunSpec(
            process=invalid_process,
            environment=valid_runspec.environment,
            mounts=valid_runspec.mounts,
            hostname=valid_runspec.hostname,
            resources=valid_runspec.resources,
            security=valid_runspec.security,
            rootfs_path=valid_runspec.rootfs_path,
            canonical_hash=valid_runspec.canonical_hash,
            compiled_at=valid_runspec.compiled_at,
            correlation_id=valid_runspec.correlation_id,
        )


# Feature: runspec-contract, Property 11: Hostname Determinístico a partir do Hash
@PROPERTY_SETTINGS
@given(canonical_hash=st.binary(min_size=6, max_size=64))
def test_property_11_hostname_is_deterministically_derived_from_hash(canonical_hash: bytes) -> None:
    hostname = Hostname.from_hash(canonical_hash)

    assert hostname.value == canonical_hash.hex()[:12]
    assert len(hostname.value) == 12
    assert set(hostname.value) <= set("0123456789abcdef")


# Feature: runspec-contract, Property 12: Clamping Hierárquico de Recursos
@PROPERTY_SETTINGS
@given(resource_spec=arbitrary_resource_spec(), parent_limits=arbitrary_parent_limits())
def test_property_12_resource_clamping_respects_parent_limits(
    resource_spec: ResourceSpec,
    parent_limits: tuple[int, int, int, int],
) -> None:
    parent_cpu_quota, parent_cpu_period, parent_memory_max, parent_pids_max = parent_limits
    clamped = resource_spec.clamp_to_parent(*parent_limits)

    assert clamped.cpu.quota_us <= parent_cpu_quota
    assert clamped.cpu.period_us <= parent_cpu_period
    assert clamped.memory.max_bytes <= parent_memory_max
    assert clamped.pids.max_pids <= parent_pids_max
    assert clamped.memory.max_bytes >= MIN_MEMORY_BYTES
    assert clamped.pids.max_pids >= 1
    assert clamped.cpu.quota_us <= clamped.cpu.period_us


# Feature: runspec-contract, Property 13: Destinos de Montagem Únicos
@PROPERTY_SETTINGS
@given(mount_topology=arbitrary_mount_topology(), mount_spec=arbitrary_mount_spec())
def test_property_13_mount_destinations_must_be_unique(
    mount_topology: MountTopology,
    mount_spec: MountSpec,
) -> None:
    destinations = [mount.destination for mount in mount_topology.mounts]

    assert destinations == [mount.destination for mount in mount_topology.mounts]
    assert len(destinations) == len(set(destinations))

    with pytest.raises(DomainError):
        MountTopology(mounts=(mount_spec, _copy_mount(mount_spec)))


# Feature: runspec-contract, Property 14: Propagação Padrão de Montagem
@PROPERTY_SETTINGS
@given(mount_spec=arbitrary_mount_spec())
def test_property_14_mount_propagation_defaults_to_rprivate(mount_spec: MountSpec) -> None:
    default_mount = MountSpec(
        mount_type=mount_spec.mount_type,
        destination=mount_spec.destination,
        source=mount_spec.source if mount_spec.mount_type is MountType.BIND else None,
        readonly=mount_spec.readonly,
        options=mount_spec.options,
    )
    explicit_mount = MountSpec(
        mount_type=mount_spec.mount_type,
        destination=mount_spec.destination,
        source=mount_spec.source if mount_spec.mount_type is MountType.BIND else None,
        propagation=Propagation.RSLAVE,
        readonly=mount_spec.readonly,
        options=mount_spec.options,
    )

    assert default_mount.propagation is Propagation.RPRIVATE
    assert explicit_mount.propagation is Propagation.RSLAVE


# Feature: runspec-contract, Property 15: Eventos de Domínio Imutáveis com Metadados
@PROPERTY_SETTINGS
@given(event=arbitrary_domain_event())
def test_property_15_domain_events_are_immutable_and_traced(event: object) -> None:
    assert isinstance(getattr(event, "event_id"), UUID)
    assert isinstance(getattr(event, "occurred_at"), datetime)
    assert isinstance(getattr(event, "correlation_id"), UUID)

    with pytest.raises(FrozenInstanceError):
        setattr(event, "event_id", getattr(event, "event_id"))


# Feature: runspec-contract, Property 17: TraduÃ§Ã£o de Erros na ACL
@PROPERTY_SETTINGS
@given(native_code=st.integers(min_value=-4_096, max_value=-1))
def test_property_17_acl_error_translation_is_deterministic(native_code: int) -> None:
    bridge = CythonBridge(native_executor=lambda serialized, rootfs_path: 0)

    first_error = bridge.translate_error(native_code)
    second_error = bridge.translate_error(native_code)

    assert type(first_error) is type(second_error) is ACLError
    assert first_error.native_code == second_error.native_code == native_code
    assert first_error.detail == second_error.detail
    assert "C++" not in first_error.detail


# Feature: runspec-contract, Property 18: KernelCapabilities Valida Requisitos MÃ­nimos
@PROPERTY_SETTINGS
@given(
    kernel_version=st.tuples(
        st.integers(min_value=0, max_value=8),
        st.integers(min_value=0, max_value=32),
        st.integers(min_value=0, max_value=32),
    ),
    has_cgroups_v2=st.booleans(),
)
def test_property_18_kernel_capabilities_validate_minimum_requirements(
    kernel_version: tuple[int, int, int],
    has_cgroups_v2: bool,
) -> None:
    capabilities = KernelCapabilities(
        kernel_version=kernel_version,
        has_cgroups_v2=has_cgroups_v2,
        has_clone3=True,
        has_pidfd=True,
        has_user_ns=True,
        has_pid_ns=True,
        has_mount_ns=True,
        has_uts_ns=True,
        has_net_ns=True,
        has_seccomp=True,
        has_pivot_root=True,
        available_cgroup_controllers=frozenset({"cpu", "memory", "pids"}),
    )

    if kernel_version < (5, 3, 0) or not has_cgroups_v2:
        with pytest.raises(DomainError):
            capabilities.validate_minimum_requirements()
    else:
        capabilities.validate_minimum_requirements()


# Feature: runspec-contract, Property 19: pids.max Sempre Presente
@PROPERTY_SETTINGS
@given(resource_spec=arbitrary_resource_spec())
def test_property_19_resource_spec_always_contains_pids_limit(resource_spec: ResourceSpec) -> None:
    assert isinstance(resource_spec.pids, PidsLimit)
    assert resource_spec.pids.max_pids >= 1
