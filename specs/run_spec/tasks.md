# Plano de Implementação: RunSpec Contract

## Visão Geral

Implementação incremental do RunSpec Contract seguindo a arquitetura DDD definida no design: Value Objects imutáveis com validação no construtor, Aggregate Root, Domain Events, Pipeline de Admissão, Serialização binária canônica com CRC32C, fronteira ACL Cython com nogil, KernelProbe e ContainerSupervisor. Linguagem: Python 3.12+ com Hypothesis para testes baseados em propriedades.

## Tarefas

- [ ] 1. Estrutura do projeto e tipos base
  - [ ] 1.1 Criar estrutura de diretórios e módulos iniciais
    - Criar `src/runspec_contract/domain/`, `src/runspec_contract/application/`, `src/runspec_contract/infrastructure/`, `tests/domain/`, `tests/application/`, `tests/infrastructure/`, `tests/properties/`
    - Criar `__init__.py` em cada pacote
    - Criar `src/runspec_contract/domain/types.py` com o tipo `AbsolutePath` (NewType ou wrapper validado), `SeccompProfile` e `MountOptions`
    - Criar `src/runspec_contract/domain/exceptions.py` com a hierarquia completa de exceções: `DomainError`, `CompilationError`, `ValidationError`, `CrossValidationError`, `SerializationError`, `ChecksumError`, `CorruptedPayloadError`, `KernelFeatureError`, `BootstrapError`, `ACLError`
    - _Requisitos: 1.3, 1.6, 14.3_

- [ ] 2. Value Objects do domínio
  - [ ] 2.1 Implementar ProcessSpec
    - `@dataclass(frozen=True)` com `argv: tuple[str, ...]`, `uid: int`, `gid: int`, `cwd: AbsolutePath`
    - Validação em `__post_init__`: argv não-vazio, elementos são strings, uid >= 0, gid >= 0
    - _Requisitos: 3.1, 3.2, 3.4, 3.5_

  - [ ] 2.2 Implementar EnvironmentMap
    - `@dataclass(frozen=True)` com `entries: tuple[tuple[str, str], ...]`
    - Validação: chaves não-vazias, sem `=` na chave, ordem lexicográfica, sem duplicatas
    - Método de classe `compile_from_layers(*layers: dict[str, str])` para compilação com precedência
    - _Requisitos: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ] 2.3 Implementar MountSpec, MountType, Propagation e MountTopology
    - Enums `MountType` (BIND, TMPFS) e `Propagation` (RPRIVATE, RSLAVE)
    - `MountSpec`: validação de source obrigatório para bind, None para tmpfs; propagação padrão RPRIVATE
    - `MountTopology`: validação de destinos únicos, preservação de ordem
    - _Requisitos: 5.1, 5.2, 5.3, 5.4, 5.6_

  - [ ] 2.4 Implementar Hostname
    - `@dataclass(frozen=True)` com `value: str`
    - Validação: 1-64 chars, regex `[a-zA-Z0-9-]`, sem hífen no início/fim
    - Método de classe `from_hash(canonical_hash: bytes)` para hostname determinístico (12 hex chars)
    - _Requisitos: 6.1, 6.2, 6.3_

  - [ ] 2.5 Implementar CpuLimit, MemoryLimit, PidsLimit e ResourceSpec
    - `CpuLimit`: quota_us > 0, period_us > 0, quota <= period
    - `MemoryLimit`: max_bytes >= 4194304 (4 MiB)
    - `PidsLimit`: max_pids >= 1
    - `ResourceSpec`: composição dos três + método `clamp_to_parent()`
    - _Requisitos: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 16.1_

  - [ ] 2.6 Implementar SecurityEnvelope
    - `@dataclass(frozen=True)` com effective_caps, permitted_caps, bounding_caps (frozenset), no_new_privs, seccomp_profile, lsm_profile
    - Validação: effective ⊆ permitted ⊆ bounding; seccomp requer no_new_privs=True
    - _Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8_

  - [ ]* 2.7 Escrever testes unitários para todos os Value Objects
    - Testes de construção válida e inválida para cada Value Object
    - Edge cases: argv vazio, chave com `=`, hostname com hífen no início, cpu.quota > period, memória < 4MiB
    - _Requisitos: 3.1, 4.4, 5.6, 6.2, 7.4, 7.5, 8.3, 8.6_

- [ ] 3. Aggregate Root e Domain Events
  - [ ] 3.1 Implementar RunSpec (Aggregate Root)
    - `@dataclass(frozen=True)` com todos os Value Objects, rootfs_path, canonical_hash, compiled_at, correlation_id
    - Validação cruzada em `__post_init__`: CWD contido no rootfs_path
    - _Requisitos: 1.1, 1.4, 10.1, 10.6_

  - [ ] 3.2 Implementar Domain Events
    - Classe base `DomainEvent` com event_id, occurred_at, correlation_id
    - Eventos: `RunSpecCompiled`, `ContainerStarted`, `ContainerExited`, `ContainerOOMKilled`, `ContainerForceKilled`
    - Todos `@dataclass(frozen=True)` com campos específicos conforme design
    - _Requisitos: 1.5, 13.3, 16.4, 18.1, 18.2, 18.3, 18.4, 18.5, 18.6_

  - [ ]* 3.3 Escrever testes unitários para RunSpec e Domain Events
    - Teste de construção válida do RunSpec com todos os Value Objects
    - Teste de rejeição quando CWD fora do rootfs
    - Teste de imutabilidade dos eventos
    - _Requisitos: 1.4, 10.6, 18.6_

- [ ] 4. Checkpoint — Verificar modelo de domínio
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Generators Hypothesis e testes de propriedade do domínio
  - [ ] 5.1 Implementar generators Hypothesis em `tests/properties/conftest.py`
    - Strategies para todos os Value Objects válidos: `arbitrary_process_spec()`, `arbitrary_environment_map()`, `arbitrary_mount_spec()`, `arbitrary_mount_topology()`, `arbitrary_hostname()`, `arbitrary_cpu_limit()`, `arbitrary_memory_limit()`, `arbitrary_pids_limit()`, `arbitrary_resource_spec()`, `arbitrary_security_envelope()`, `arbitrary_runspec()`
    - Strategies para inputs inválidos: `invalid_argv()`, `invalid_env_keys()`, `invalid_hostnames()`
    - Strategies auxiliares: `arbitrary_env_layers()`, `arbitrary_parent_limits()`
    - _Requisitos: 1.1, 1.3_

  - [ ]* 5.2 Escrever teste de propriedade P5: Imutabilidade do RunSpec e Value Objects
    - **Propriedade 5: Imutabilidade do RunSpec e Value Objects**
    - Verificar que tentativas de mutação de qualquer atributo levantam `FrozenInstanceError`
    - **Valida: Requisitos 1.4, 4.3, 5.7, 7.8, 8.8**

  - [ ]* 5.3 Escrever teste de propriedade P6: Validação de Invariantes dos Value Objects
    - **Propriedade 6: Validação de Invariantes dos Value Objects**
    - Gerar inputs inválidos e verificar que a construção falha com `DomainError`
    - **Valida: Requisitos 1.3, 1.6, 3.1, 4.4, 5.6, 6.1, 6.2, 7.1, 7.2, 7.3, 7.4, 7.5**

  - [ ]* 5.4 Escrever teste de propriedade P7: Invariante de Subconjunto de Capabilities
    - **Propriedade 7: Invariante de Subconjunto de Capabilities**
    - Gerar combinações de capabilities e verificar effective ⊆ permitted ⊆ bounding
    - **Valida: Requisitos 8.1, 8.2, 8.3**

  - [ ]* 5.5 Escrever teste de propriedade P8: Seccomp Requer no_new_privs
    - **Propriedade 8: Seccomp Requer no_new_privs**
    - Verificar que seccomp com no_new_privs=False falha; com True, aceita
    - **Valida: Requisitos 8.6**

  - [ ]* 5.6 Escrever teste de propriedade P9: Normalização de Caminhos é Idempotente
    - **Propriedade 9: Normalização de Caminhos é Idempotente**
    - Verificar que normalize(normalize(p)) == normalize(p) e caminhos não-absolutos são rejeitados
    - **Valida: Requisitos 3.4, 3.5, 3.6, 5.4, 5.5**

  - [ ]* 5.7 Escrever teste de propriedade P10: CWD Contido no RootFS
    - **Propriedade 10: CWD Contido no RootFS**
    - Gerar CWDs fora do rootfs e verificar rejeição na construção do RunSpec
    - **Valida: Requisitos 10.1, 10.6**

  - [ ]* 5.8 Escrever teste de propriedade P4: Precedência de Camadas do EnvironmentMap
    - **Propriedade 4: Precedência de Camadas do EnvironmentMap**
    - Gerar camadas com chaves sobrepostas e verificar que a camada de maior precedência vence
    - **Valida: Requisitos 1.2, 4.1, 4.2, 4.3**

  - [ ]* 5.9 Escrever teste de propriedade P11: Hostname Determinístico a partir do Hash
    - **Propriedade 11: Hostname Determinístico a partir do Hash**
    - Verificar que from_hash() produz 12 hex chars e hostname POSIX válido
    - **Valida: Requisitos 6.3**

  - [ ]* 5.10 Escrever teste de propriedade P12: Clamping Hierárquico de Recursos
    - **Propriedade 12: Clamping Hierárquico de Recursos**
    - Verificar que clamp_to_parent() produz valores ≤ limites do pai e ≥ mínimos absolutos
    - **Valida: Requisitos 7.6, 7.7**

  - [ ]* 5.11 Escrever teste de propriedade P13: Destinos de Montagem Únicos
    - **Propriedade 13: Destinos de Montagem Únicos**
    - Gerar listas com destinos duplicados e verificar rejeição
    - **Valida: Requisitos 5.1, 5.6**

  - [ ]* 5.12 Escrever teste de propriedade P14: Propagação Padrão de Montagem
    - **Propriedade 14: Propagação Padrão de Montagem**
    - Verificar que MountSpec sem propagação explícita usa RPRIVATE
    - **Valida: Requisitos 5.2, 5.3**

  - [ ]* 5.13 Escrever teste de propriedade P15: Eventos de Domínio Imutáveis com Metadados
    - **Propriedade 15: Eventos de Domínio Imutáveis com Metadados**
    - Verificar imutabilidade e presença de event_id, occurred_at, correlation_id em todos os eventos
    - **Valida: Requisitos 1.5, 13.3, 16.4, 18.1, 18.2, 18.3, 18.4, 18.5, 18.6**

  - [ ]* 5.14 Escrever teste de propriedade P19: pids.max Sempre Presente
    - **Propriedade 19: pids.max Sempre Presente**
    - Verificar que todo ResourceSpec válido tem pids.max >= 1
    - **Valida: Requisitos 16.1**

- [ ] 6. Checkpoint — Verificar testes de propriedade do domínio
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Pipeline de Admissão
  - [ ] 7.1 Implementar Protocols do Pipeline
    - Criar `src/runspec_contract/application/protocols.py` com `PrecedenceResolutionPolicy`, `PolicyEnrichmentPolicy`, `PathNormalizationPolicy`
    - Definir tipos de input/output: `RawCompilationInputs`, `ResolvedInputs`, `EnrichedInputs`, `NormalizedInputs`, `ImageMetadata`, `PlatformDefaults`, `PolicyConfig`, `UserOverrides`
    - _Requisitos: 1.2, 4.1_

  - [ ] 7.2 Implementar AdmissionPipeline
    - Criar `src/runspec_contract/application/admission_pipeline.py`
    - Orquestrar: resolução de precedência → enriquecimento de políticas → normalização de caminhos → RunSpecFactory.create()
    - Emitir evento `RunSpecCompiled` após compilação bem-sucedida
    - _Requisitos: 1.1, 1.2, 1.5, 1.6_

  - [ ] 7.3 Implementar RunSpecFactory
    - Criar `src/runspec_contract/domain/factory.py`
    - Método estático `create(normalized: NormalizedInputs) -> RunSpec`
    - Construção atômica: criar cada Value Object, validar invariantes cruzadas, calcular canonical_hash, retornar RunSpec imutável
    - _Requisitos: 1.1, 1.3, 1.4, 1.6_

  - [ ] 7.4 Implementar implementações padrão dos Policies
    - `DefaultPrecedenceResolutionPolicy`: merge de camadas na ordem fixa (imagem → plataforma → política → usuário)
    - `DefaultPathNormalizationPolicy`: normalização de caminhos com `os.path.normpath`, rejeição de caminhos não-absolutos
    - `DefaultPolicyEnrichmentPolicy`: enriquecimento com KernelCapabilities
    - _Requisitos: 1.2, 3.6, 5.5_

  - [ ]* 7.5 Escrever teste de propriedade P3: Determinismo de Compilação
    - **Propriedade 3: Determinismo de Compilação**
    - Compilar RunSpec 2x com mesmos inputs e verificar hash canônico idêntico
    - **Valida: Requisitos 1.1, 4.5**

  - [ ]* 7.6 Escrever testes unitários para o Pipeline de Admissão
    - Teste end-to-end com inputs reais
    - Teste de rejeição de inputs inválidos
    - Teste de emissão do evento RunSpecCompiled
    - _Requisitos: 1.1, 1.2, 1.5_

- [ ] 8. Serialização binária canônica
  - [ ] 8.1 Implementar RunSpecSerializer
    - Criar `src/runspec_contract/infrastructure/serializer.py`
    - Formato: Magic (0x52535043, 4B) + Version (uint16 BE, 2B) + Payload (campos length-prefixed uint32 BE) + CRC32C (4B)
    - Ordem fixa de campos: process, environment, mounts, hostname, resources, security, rootfs_path, compiled_at, correlation_id
    - Métodos `serialize(runspec: RunSpec) -> bytes` e `deserialize(data: bytes) -> RunSpec`
    - _Requisitos: 2.1, 2.2, 2.3, 2.5_

  - [ ] 8.2 Implementar verificação de checksum CRC32C
    - Calcular CRC32C sobre Magic + Version + Payload
    - Verificar na desserialização; levantar `ChecksumError` se não corresponder
    - Rejeitar bytes corrompidos ou truncados com `CorruptedPayloadError`
    - _Requisitos: 2.4, 2.5, 2.6_

  - [ ] 8.3 Implementar serialização/desserialização do ErrorFrame
    - Formato: Phase (1B enum) + ErrCode (4B uint32) + Message (length-prefixed UTF-8)
    - Round-trip: serialize(deserialize(ef)) == ef
    - _Requisitos: 9.3_

  - [ ]* 8.4 Escrever teste de propriedade P1: Round-trip de Serialização
    - **Propriedade 1: Round-trip de Serialização**
    - Para qualquer RunSpec válido, `serialize(deserialize(serialize(rs))) == serialize(rs)`
    - **Valida: Requisitos 2.1, 2.2, 2.3**

  - [ ]* 8.5 Escrever teste de propriedade P2: Detecção de Corrupção via Checksum
    - **Propriedade 2: Detecção de Corrupção via Checksum**
    - Mutar bytes do payload e verificar que desserialização falha com erro de integridade
    - Bytes aleatórios → erro estruturado, nunca RunSpec parcial
    - **Valida: Requisitos 2.4, 2.5, 2.6, 14.4**

  - [ ]* 8.6 Escrever teste de propriedade P16: Round-trip do ErrorFrame
    - **Propriedade 16: Round-trip do ErrorFrame**
    - Para qualquer ErrorFrame válido, serialize(deserialize(ef)) == ef
    - **Valida: Requisitos 9.3**

  - [ ]* 8.7 Escrever testes unitários para o Serializer
    - Teste de serialização/desserialização com RunSpec concreto
    - Teste de rejeição de magic bytes inválidos
    - Teste de rejeição de payload truncado
    - _Requisitos: 2.1, 2.4, 2.6_

- [ ] 9. Checkpoint — Verificar serialização
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Fronteira ACL e Cython Bridge
  - [ ] 10.1 Implementar CythonBridge
    - Criar `src/runspec_contract/infrastructure/acl_bridge.py` com interface Python
    - Criar `src/runspec_contract/infrastructure/acl_bridge.pyx` com implementação Cython (nogil)
    - Método `execute(serialized: bytes, rootfs_path: str) -> ExecutionResult`
    - Verificação de checksum antes de passar ao Data Plane
    - Medição de latência; warning se > 2ms
    - _Requisitos: 14.1, 14.2, 14.4, 14.5, 14.6_

  - [ ] 10.2 Implementar tradução de erros na ACL
    - Mapeamento determinístico: código de erro nativo → exceção de domínio Python (`ACLError`)
    - Sem exposição de detalhes de implementação C++
    - _Requisitos: 14.3_

  - [ ]* 10.3 Escrever teste de propriedade P17: Tradução de Erros na ACL
    - **Propriedade 17: Tradução de Erros na ACL**
    - Verificar que o mesmo código de erro sempre produz a mesma classe de exceção
    - **Valida: Requisitos 14.3**

  - [ ]* 10.4 Escrever testes unitários para a ACL Bridge
    - Teste de verificação de checksum antes da execução
    - Teste de warning de latência > 2ms
    - Teste de tradução de erros nativos
    - _Requisitos: 14.3, 14.4, 14.6_

- [ ] 11. KernelProbe
  - [ ] 11.1 Implementar KernelCapabilities e KernelProbe
    - Criar `src/runspec_contract/infrastructure/kernel_probe.py`
    - `KernelCapabilities` como `@dataclass(frozen=True)` com todos os campos de probing
    - Método `validate_minimum_requirements()`: kernel >= 5.3, cgroups v2, clone3, pidfd, seccomp, pivot_root
    - `KernelProbe.probe() -> KernelCapabilities`: probing real via `/proc/version`, `/sys/fs/cgroup`, etc.
    - _Requisitos: 15.1, 15.2, 15.3, 15.4, 15.5_

  - [ ]* 11.2 Escrever teste de propriedade P18: KernelCapabilities Valida Requisitos Mínimos
    - **Propriedade 18: KernelCapabilities Valida Requisitos Mínimos**
    - Kernel < 5.3 ou sem cgroups v2 → DomainError; requisitos atendidos → sem erro
    - **Valida: Requisitos 15.1, 15.2, 15.3, 15.5**

  - [ ]* 11.3 Escrever testes unitários para KernelProbe
    - Teste com KernelCapabilities válido (todos os requisitos atendidos)
    - Teste com kernel < 5.3
    - Teste com cgroups v2 ausente
    - Teste com feature obrigatória ausente (clone3, pidfd, etc.)
    - _Requisitos: 15.1, 15.2, 15.3_

- [ ] 12. ContainerSupervisor
  - [ ] 12.1 Implementar ContainerSupervisor
    - Criar `src/runspec_contract/application/container_supervisor.py`
    - Métodos: `start()`, `wait()`, `terminate()`, `force_kill()`, `reattach()`
    - Integração com CythonBridge para execução
    - Supervisão via pidfd com waitid(P_PIDFD)
    - Escalação de terminação: SIGTERM → timeout → cgroup.kill
    - Emissão de eventos: ContainerStarted, ContainerExited, ContainerOOMKilled, ContainerForceKilled
    - _Requisitos: 9.1, 9.2, 9.4, 9.5, 9.6, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 16.2, 16.3, 16.4_

  - [ ]* 12.2 Escrever testes unitários para ContainerSupervisor
    - Teste de fluxo start → wait → exit com mocks
    - Teste de escalação SIGTERM → cgroup.kill
    - Teste de detecção de OOM kill
    - Teste de reattach após crash
    - _Requisitos: 13.2, 13.3, 13.5, 13.6, 16.3, 16.4_

- [ ] 13. Integração e wiring final
  - [ ] 13.1 Wiring de todos os componentes
    - Criar `src/runspec_contract/bootstrap.py` como ponto de entrada de composição
    - Instanciar KernelProbe → validar requisitos mínimos
    - Instanciar AdmissionPipeline com policies padrão e KernelCapabilities
    - Instanciar RunSpecSerializer e CythonBridge
    - Instanciar ContainerSupervisor com bridge e serializer
    - Expor API de alto nível: `compile_and_execute(raw_inputs, rootfs_path) -> ContainerHandle`
    - _Requisitos: 1.1, 14.1, 15.1_

  - [ ]* 13.2 Escrever testes de integração
    - Teste end-to-end: compilação → serialização → round-trip → desserialização
    - Teste de rejeição de inputs inválidos no pipeline completo
    - Teste de emissão de eventos no fluxo completo
    - _Requisitos: 1.1, 2.3, 18.1_

- [ ] 14. Checkpoint final — Verificar integração completa
  - Ensure all tests pass, ask the user if questions arise.

## Notas

- Tarefas marcadas com `*` são opcionais e podem ser puladas para um MVP mais rápido
- Cada tarefa referencia requisitos específicos para rastreabilidade
- Checkpoints garantem validação incremental
- Testes de propriedade validam propriedades universais de corretude (19 propriedades do design)
- Testes unitários validam exemplos específicos e edge cases
