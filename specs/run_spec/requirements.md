# Documento de Requisitos — RunSpec Contract

## Introdução

O RunSpec é o contrato de execução canônico, imutável e determinístico que serve como ponto único de sincronização entre o Control Plane (Python) e o Data Plane (C++). Ele representa a raiz de agregado central de um sistema de containers construído do zero, seguindo a função `f(RunSpec, RootFS) → Processo Isolado`.

O Control Plane é responsável por regras de negócio, metadados, pipeline de admissão e compilação do spec. O Data Plane é responsável pela execução nativa usando primitivas do kernel Linux (namespaces, cgroups v2, pivot_root, seccomp). A comunicação entre os planos ocorre via ACL (Anti-Corruption Layer) implementada em Cython com tipagem estrita e liberação de GIL.

Este documento define os requisitos funcionais e não-funcionais para a compilação, validação, serialização e handoff do RunSpec entre os dois planos.

## Glossário

- **RunSpec**: Contrato de execução imutável que é a raiz de agregado do domínio. Contém todos os Value Objects necessários para descrever completamente um processo isolado.
- **Control_Plane**: Componente Python responsável por regras de negócio, pipeline de admissão, compilação e gerenciamento do ciclo de vida do RunSpec.
- **Data_Plane**: Componente C++ responsável pela execução nativa de containers usando primitivas do kernel Linux.
- **ACL**: Anti-Corruption Layer — camada Cython que faz a ponte tipada entre Control Plane e Data Plane, com liberação de GIL.
- **ProcessSpec**: Value Object que encapsula argv, resolução de usuário (UID/GID) e diretório de trabalho.
- **MountSpec**: Value Object que descreve uma transformação VFS individual (bind mount, tmpfs) com propagação e flags.
- **MountTopology**: Sequência ordenada de MountSpecs representando a topologia completa de montagens.
- **SecurityEnvelope**: Value Object que encapsula capabilities (effective, permitted, bounding set), filtros seccomp, PR_SET_NO_NEW_PRIVS e perfis LSM.
- **ResourceSpec**: Value Object que descreve limites de recursos via cgroups v2 (CPU, memória, PIDs).
- **EnvironmentMap**: Value Object que representa variáveis de ambiente compiladas deterministicamente a partir de camadas de precedência.
- **Pipeline_de_Admissão**: Sequência de transformações que compila um RunSpec a partir de inputs brutos: resolução de precedência → enriquecimento de políticas → normalização de caminhos.
- **RootFS**: Sistema de arquivos raiz pré-preparado no disco, usado como base para pivot_root.
- **Handshake_de_Bootstrap**: Protocolo de sincronização entre Control Plane e Data Plane usando pipe2(O_CLOEXEC) para barreira de execução e frames de erro estruturados.
- **Cgroups_v2**: Interface unificada de controle de recursos do kernel Linux, com hierarquia de controladores e regra de single writer.
- **Pidfd**: File descriptor que referencia um processo de forma estável, obtido via clone3(CLONE_PIDFD).
- **Hash_Canônico**: Hash determinístico do RunSpec que garante que inputs idênticos produzem sempre o mesmo resultado.
- **Seccomp**: Mecanismo do kernel para filtragem de syscalls, aplicado como parte do SecurityEnvelope.
- **Bounding_Set**: Conjunto de capabilities Linux que limita o teto máximo de privilégios de um processo.

## Requisitos

### Requisito 1: Compilação Determinística do RunSpec

**User Story:** Como operador do Control Plane, eu quero compilar um RunSpec de forma determinística a partir de inputs estruturados, para que inputs idênticos sempre produzam o mesmo contrato de execução com o mesmo hash canônico.

#### Critérios de Aceitação

1. WHEN inputs idênticos de compilação são fornecidos (metadados de imagem, overrides de CLI/API, políticas), THE Control_Plane SHALL produzir um RunSpec com hash canônico idêntico em todas as execuções.
2. WHEN o Pipeline_de_Admissão recebe inputs brutos, THE Control_Plane SHALL aplicar as camadas de precedência na ordem fixa: metadados de imagem → defaults de plataforma → injeção de políticas → overrides do usuário.
3. WHEN uma combinação semanticamente inválida de campos é detectada durante a compilação (por exemplo, UID 0 com SecurityEnvelope restritivo incompatível), THE Control_Plane SHALL rejeitar a compilação com um erro de domínio descritivo antes de produzir o RunSpec.
4. THE RunSpec SHALL ser imutável após a compilação — nenhuma mutação é permitida após a construção do objeto.
5. WHEN o RunSpec é compilado com sucesso, THE Control_Plane SHALL emitir um evento de domínio RunSpecCompiled contendo o hash canônico, timestamp e correlation_id.
6. THE Control_Plane SHALL validar que todos os Value Objects internos do RunSpec (ProcessSpec, MountTopology, SecurityEnvelope, ResourceSpec, EnvironmentMap) satisfazem suas invariantes individuais antes de compor o agregado.

### Requisito 2: Serialização e Round-Trip do RunSpec

**User Story:** Como desenvolvedor da ACL, eu quero serializar o RunSpec para um formato binário canônico e desserializá-lo de volta sem perda de informação, para que o contrato atravesse a fronteira Cython de forma segura e verificável.

#### Critérios de Aceitação

1. THE ACL SHALL serializar o RunSpec em um formato binário canônico com ordenação determinística de campos.
2. WHEN um RunSpec serializado é desserializado, THE ACL SHALL produzir um RunSpec semanticamente equivalente ao original.
3. FOR ALL RunSpecs válidos, serializar e depois desserializar e depois serializar novamente SHALL produzir bytes idênticos (propriedade round-trip).
4. WHEN bytes corrompidos ou truncados são fornecidos para desserialização, THE ACL SHALL retornar um erro estruturado descritivo em vez de produzir um RunSpec parcial ou inválido.
5. THE ACL SHALL incluir um checksum no payload serializado para detecção de corrupção em trânsito.
6. WHEN o checksum do payload não corresponde aos bytes recebidos, THE ACL SHALL rejeitar a desserialização com erro específico de integridade.

### Requisito 3: ProcessSpec — Resolução de Processo

**User Story:** Como operador do Control Plane, eu quero especificar o processo a ser executado (argv, usuário, diretório de trabalho) de forma validada e tipada, para que o Data Plane receba instruções inequívocas de execução.

#### Critérios de Aceitação

1. THE ProcessSpec SHALL conter argv como uma sequência não-vazia de strings, onde o primeiro elemento é o caminho do executável.
2. THE ProcessSpec SHALL resolver UID e GID a partir de nomes de usuário/grupo, armazenando os valores numéricos resolvidos.
3. WHEN um nome de usuário ou grupo não pode ser resolvido para UID/GID numérico, THE Control_Plane SHALL rejeitar a compilação com erro de domínio descritivo.
4. THE ProcessSpec SHALL conter um CWD (Current Working Directory) como caminho absoluto validado.
5. WHEN o CWD especificado não é um caminho absoluto, THE Control_Plane SHALL rejeitar a compilação com erro de domínio indicando a violação.
6. WHEN o CWD contém componentes de travessia de diretório (por exemplo, `..`), THE Control_Plane SHALL normalizar o caminho para sua forma canônica absoluta.

### Requisito 4: Compilação Determinística de Variáveis de Ambiente

**User Story:** Como operador do Control Plane, eu quero que as variáveis de ambiente sejam compiladas deterministicamente a partir de múltiplas camadas de precedência, para que o ambiente do processo seja previsível e auditável.

#### Critérios de Aceitação

1. THE Control_Plane SHALL compilar o EnvironmentMap aplicando camadas na ordem fixa: metadados de imagem → defaults de plataforma → injeção de políticas → overrides do usuário.
2. WHEN duas camadas definem a mesma variável de ambiente, THE Control_Plane SHALL usar o valor da camada de maior precedência (overrides do usuário > injeção de políticas > defaults de plataforma > metadados de imagem).
3. THE EnvironmentMap SHALL ser imutável após a compilação e armazenar pares chave-valor em ordem determinística (ordenação lexicográfica por chave).
4. WHEN uma variável de ambiente possui chave vazia ou contém o caractere `=` na chave, THE Control_Plane SHALL rejeitar a compilação com erro de domínio descritivo.
5. WHEN inputs idênticos de camadas são fornecidos, THE Control_Plane SHALL produzir um EnvironmentMap com hash idêntico.

### Requisito 5: Topologia de Montagens (MountTopology)

**User Story:** Como operador do Control Plane, eu quero definir uma sequência ordenada de montagens VFS com propagação e flags validados, para que o Data Plane construa o filesystem isolado de forma correta e segura.

#### Critérios de Aceitação

1. THE MountTopology SHALL representar uma sequência ordenada de MountSpecs, onde a ordem define a sequência de aplicação das transformações VFS.
2. THE MountSpec SHALL suportar os tipos: bind mount e tmpfs, cada um com flags e opções específicas do tipo.
3. THE MountSpec SHALL especificar propagação como rprivate ou rslave, com rprivate como valor padrão.
4. WHEN um caminho de destino de montagem não é absoluto, THE Control_Plane SHALL rejeitar a compilação com erro de domínio descritivo.
5. WHEN um caminho de destino de montagem contém componentes de travessia de diretório (`..`), THE Control_Plane SHALL normalizar o caminho para forma canônica absoluta e validar que permanece dentro do RootFS.
6. WHEN dois MountSpecs possuem o mesmo caminho de destino, THE Control_Plane SHALL rejeitar a compilação com erro de domínio indicando conflito de ponto de montagem.
7. THE MountTopology SHALL ser imutável após a compilação.

### Requisito 6: Hostname e Identidade UTS

**User Story:** Como operador do Control Plane, eu quero definir o hostname do container de forma validada, para que o namespace UTS seja configurado corretamente pelo Data Plane.

#### Critérios de Aceitação

1. THE RunSpec SHALL conter um hostname como string não-vazia com no máximo 64 caracteres, conforme limite POSIX.
2. WHEN o hostname contém caracteres inválidos (fora de `[a-zA-Z0-9-]`) ou começa/termina com hífen, THE Control_Plane SHALL rejeitar a compilação com erro de domínio descritivo.
3. WHEN nenhum hostname é especificado pelo usuário, THE Control_Plane SHALL gerar um hostname derivado deterministicamente do hash canônico do RunSpec (primeiros 12 caracteres hexadecimais).

### Requisito 7: Governança de Recursos via Cgroups v2 (ResourceSpec)

**User Story:** Como operador do Control Plane, eu quero definir limites de recursos (CPU, memória, PIDs) que serão aplicados via cgroups v2, para que o Data Plane governe o consumo de recursos do container de forma previsível.

#### Critérios de Aceitação

1. THE ResourceSpec SHALL especificar limites de CPU como cpu.max (quota e period em microsegundos), onde quota é menor ou igual a period.
2. THE ResourceSpec SHALL especificar limite de memória como memory.max em bytes, com valor mínimo de 4 MiB (4194304 bytes).
3. THE ResourceSpec SHALL especificar limite de PIDs como pids.max com valor mínimo de 1.
4. WHEN um valor de recurso viola os limites mínimos ou máximos definidos, THE Control_Plane SHALL rejeitar a compilação com erro de domínio descritivo indicando o campo e a violação.
5. WHEN cpu.quota é especificado como maior que cpu.period, THE Control_Plane SHALL rejeitar a compilação com erro de domínio indicando a inconsistência.
6. THE ResourceSpec SHALL suportar clamping hierárquico — os limites do container não podem exceder os limites do cgroup pai.
7. WHEN os limites solicitados excedem os limites do cgroup pai, THE Control_Plane SHALL aplicar clamping (ajuste para o máximo permitido pelo pai) e registrar um aviso no evento de domínio.
8. THE ResourceSpec SHALL ser imutável após a compilação.

### Requisito 8: Envelope de Segurança (SecurityEnvelope)

**User Story:** Como operador do Control Plane, eu quero definir o envelope de segurança do container (capabilities, seccomp, no_new_privs, perfis LSM) de forma validada e coerente, para que o Data Plane aplique a redução irreversível de privilégios corretamente.

#### Critérios de Aceitação

1. THE SecurityEnvelope SHALL especificar três conjuntos de capabilities Linux: effective, permitted e bounding set, cada um como conjunto imutável de capabilities nomeadas.
2. THE SecurityEnvelope SHALL garantir que o conjunto effective é subconjunto do conjunto permitted, e o conjunto permitted é subconjunto do bounding set.
3. WHEN o conjunto effective contém capabilities ausentes do conjunto permitted, THE Control_Plane SHALL rejeitar a compilação com erro de domínio descritivo indicando as capabilities inconsistentes.
4. THE SecurityEnvelope SHALL conter um flag booleano no_new_privs que, quando ativado, indica que PR_SET_NO_NEW_PRIVS deve ser aplicado antes do execve.
5. THE SecurityEnvelope SHALL conter uma referência a um perfil seccomp (identificador do filtro ou política inline) que define as syscalls permitidas.
6. WHEN no_new_privs é false e um perfil seccomp é especificado, THE Control_Plane SHALL rejeitar a compilação com erro de domínio, pois seccomp em modo FILTER requer no_new_privs ativo.
7. WHERE um perfil LSM (AppArmor ou SELinux) é especificado, THE SecurityEnvelope SHALL conter o identificador do perfil como string validada.
8. THE SecurityEnvelope SHALL ser imutável após a compilação.

### Requisito 9: Handshake de Bootstrap e Barreira de Execução

**User Story:** Como desenvolvedor do Data Plane, eu quero um protocolo de handshake estruturado entre Control Plane e Data Plane usando pipe2(O_CLOEXEC), para que a execução do container só prossiga após confirmação de setup completo e erros sejam reportados de forma estruturada.

#### Critérios de Aceitação

1. WHEN o Data_Plane inicia o bootstrap de um container, THE Data_Plane SHALL criar um pipe de sincronização via pipe2(O_CLOEXEC) antes do clone3.
2. WHEN o setup do namespace, montagens, cgroups e segurança é concluído com sucesso no processo filho, THE Data_Plane SHALL sinalizar prontidão ao Control_Plane através do pipe de sincronização.
3. WHEN um erro ocorre durante o setup do processo filho (antes do execve), THE Data_Plane SHALL enviar um frame de erro estruturado pelo pipe contendo: código de erro, fase do setup que falhou e mensagem descritiva.
4. THE Control_Plane SHALL aguardar o sinal de prontidão ou frame de erro no pipe antes de considerar o container como iniciado.
5. WHEN o pipe é fechado sem sinal de prontidão ou frame de erro (indicando crash do processo filho), THE Control_Plane SHALL tratar como falha de bootstrap com erro específico.
6. THE Data_Plane SHALL fechar automaticamente o pipe após execve bem-sucedido (via O_CLOEXEC), sinalizando implicitamente que a barreira foi ultrapassada.

### Requisito 10: Isolamento VFS e pivot_root

**User Story:** Como desenvolvedor do Data Plane, eu quero que o RunSpec forneça informações suficientes para executar pivot_root com propagação correta e validação de CWD dentro do rootfs, para que o processo execute em um filesystem completamente isolado.

#### Critérios de Aceitação

1. THE RunSpec SHALL conter o caminho absoluto do RootFS pré-preparado no disco.
2. WHEN o caminho do RootFS não existe ou não é um diretório, THE Control_Plane SHALL rejeitar a compilação com erro de domínio descritivo.
3. THE Data_Plane SHALL aplicar a MountTopology na ordem especificada pelo RunSpec antes de executar pivot_root.
4. WHEN o CWD especificado no ProcessSpec não existe dentro do RootFS após pivot_root, THE Data_Plane SHALL reportar erro estruturado via Handshake_de_Bootstrap indicando CWD inválido.
5. THE Data_Plane SHALL configurar propagação rprivate no mount namespace raiz antes de aplicar montagens individuais, garantindo isolamento de propagação.
6. THE Control_Plane SHALL validar que o CWD do ProcessSpec, após normalização, está contido dentro do caminho do RootFS (prevenção de escape via travessia de diretório).

### Requisito 11: Governança de Cgroups v2 no Data Plane

**User Story:** Como desenvolvedor do Data Plane, eu quero que o RunSpec contenha informações suficientes para criar e configurar cgroups v2 leaf com a regra top-down e single writer, para que os recursos do container sejam governados corretamente.

#### Critérios de Aceitação

1. WHEN o Data_Plane recebe um RunSpec com ResourceSpec, THE Data_Plane SHALL criar um cgroup leaf dedicado para o container na hierarquia v2.
2. THE Data_Plane SHALL aplicar os limites de recursos (cpu.max, memory.max, pids.max) no cgroup leaf conforme especificado no ResourceSpec.
3. THE Data_Plane SHALL respeitar a regra top-down do cgroups v2 — controladores devem ser habilitados no pai antes de serem usados no filho.
4. THE Data_Plane SHALL respeitar a regra de single writer — apenas um processo gerenciador escreve nos arquivos de controle de um cgroup.
5. WHEN o sistema operacional não suporta cgroups v2 (por exemplo, apenas v1 está disponível), THE Control_Plane SHALL rejeitar a compilação com erro de domínio indicando que cgroups v1 não é suportado.
6. WHEN o kernel não suporta um controlador específico solicitado no ResourceSpec, THE Data_Plane SHALL reportar erro estruturado via Handshake_de_Bootstrap indicando o controlador ausente.
7. WHERE delegação systemd é utilizada, THE Data_Plane SHALL criar o cgroup dentro do escopo delegado pelo systemd.

### Requisito 12: Redução Irreversível de Privilégios

**User Story:** Como desenvolvedor do Data Plane, eu quero que o SecurityEnvelope do RunSpec seja aplicado de forma irreversível antes do execve, para que o processo do container execute com o mínimo de privilégios necessários.

#### Critérios de Aceitação

1. WHEN o SecurityEnvelope especifica no_new_privs como true, THE Data_Plane SHALL aplicar PR_SET_NO_NEW_PRIVS via prctl antes do execve.
2. THE Data_Plane SHALL reduzir o bounding set de capabilities para exatamente o conjunto especificado no SecurityEnvelope, removendo todas as capabilities não listadas.
3. THE Data_Plane SHALL configurar os conjuntos effective e permitted de capabilities conforme especificado no SecurityEnvelope, após a redução do bounding set.
4. WHEN um perfil seccomp é especificado no SecurityEnvelope, THE Data_Plane SHALL carregar o filtro seccomp após a configuração de capabilities e antes do execve.
5. THE Data_Plane SHALL aplicar as reduções de privilégio na ordem: bounding set → capabilities effective/permitted → no_new_privs → seccomp → execve.
6. WHEN a aplicação de qualquer etapa de redução de privilégio falha, THE Data_Plane SHALL reportar erro estruturado via Handshake_de_Bootstrap e abortar a execução do container.
7. WHERE um perfil LSM é especificado no SecurityEnvelope, THE Data_Plane SHALL aplicar a transição de perfil LSM antes do execve.

### Requisito 13: Supervisão via Pidfd e Reaping

**User Story:** Como operador do Control Plane, eu quero supervisionar o processo do container via pidfd e garantir reaping correto de processos órfãos, para que o ciclo de vida do container seja gerenciado de forma confiável e sem race conditions.

#### Critérios de Aceitação

1. WHEN o Data_Plane cria o processo do container via clone3, THE Data_Plane SHALL solicitar CLONE_PIDFD para obter um file descriptor estável referenciando o processo.
2. THE Control_Plane SHALL usar waitid(P_PIDFD) com o pidfd para aguardar a terminação do processo do container, evitando race conditions de PID recycling.
3. WHEN o processo do container termina, THE Control_Plane SHALL coletar o status de saída via pidfd e emitir um evento de domínio ContainerExited contendo exit code, signal (se aplicável) e duração.
4. THE Data_Plane SHALL configurar o processo init do container (PID 1 no namespace) para reaping de processos órfãos filhos.
5. WHEN o Control_Plane sofre crash e reinicia, THE Control_Plane SHALL ser capaz de reattach ao pidfd do container em execução dentro de 500ms.
6. WHEN o processo do container é terminado por OOM killer, THE Control_Plane SHALL detectar via cgroup memory events, marcar o container como FAILED e emitir evento de domínio ContainerOOMKilled.

### Requisito 14: Fronteira ACL entre Control Plane e Data Plane

**User Story:** Como desenvolvedor da ACL, eu quero que a fronteira entre Control Plane e Data Plane seja estritamente tipada e com liberação de GIL, para que a comunicação seja segura, eficiente e sem vazamento de abstrações entre os planos.

#### Critérios de Aceitação

1. THE ACL SHALL traduzir o RunSpec do modelo de domínio Python para uma representação C-compatível com tipagem estrita, sem uso de PyObject* ou dict genéricos.
2. THE ACL SHALL liberar o GIL (nogil) durante toda a chamada ao Data Plane, permitindo que o Control Plane continue processando outras requisições.
3. WHEN o Data_Plane retorna um erro, THE ACL SHALL traduzir o código de erro nativo para uma exceção de domínio Python descritiva, sem expor detalhes de implementação C++.
4. THE ACL SHALL validar a integridade do RunSpec serializado antes de passar ao Data Plane (verificação de checksum).
5. THE Data_Plane SHALL executar apenas a lógica de execução nativa — sem regras de negócio, callbacks Python ou acesso ao modelo de domínio.
6. WHEN a latência da travessia ACL excede 2ms, THE Control_Plane SHALL registrar um aviso de performance com métricas de timing.

### Requisito 15: Probing de Features do Kernel

**User Story:** Como operador do Control Plane, eu quero que o sistema valide a disponibilidade de features do kernel necessárias antes de aceitar compilações de RunSpec, para que falhas sejam detectadas cedo e com mensagens claras.

#### Critérios de Aceitação

1. WHEN o sistema é inicializado, THE Control_Plane SHALL executar probing de features do kernel para verificar: suporte a cgroups v2, clone3, pidfd, namespaces (user, pid, mount, uts, net), seccomp e pivot_root.
2. WHEN o kernel não suporta cgroups v2 (apenas v1 disponível), THE Control_Plane SHALL recusar inicialização com erro descritivo indicando que cgroups v1 não é suportado.
3. WHEN o kernel é anterior à versão 5.3, THE Control_Plane SHALL recusar inicialização com erro descritivo indicando a versão mínima requerida.
4. WHEN uma feature opcional não está disponível (por exemplo, um controlador de cgroup específico), THE Control_Plane SHALL registrar aviso e desabilitar funcionalidades dependentes daquela feature.
5. THE Control_Plane SHALL cachear o resultado do probing de features e disponibilizá-lo como Value Object imutável (KernelCapabilities) para consulta durante compilação de RunSpecs.

### Requisito 16: Proteção contra Fork Bombs e Escalação de Terminação

**User Story:** Como operador do Control Plane, eu quero que containers com fork bombs sejam contidos via pids.max e que a terminação possa ser escalada via cgroup.kill, para que um container malicioso não comprometa o host.

#### Critérios de Aceitação

1. THE ResourceSpec SHALL sempre incluir um valor pids.max, sem possibilidade de omissão (valor padrão de política se não especificado pelo usuário).
2. WHEN o número de processos no cgroup atinge pids.max, THE Data_Plane SHALL garantir que fork/clone retorne EAGAIN para o processo do container.
3. WHEN o Control_Plane solicita terminação de um container e o SIGTERM não é efetivo dentro de um timeout configurável, THE Control_Plane SHALL escalar para cgroup.kill para terminação forçada de todos os processos no cgroup.
4. WHEN cgroup.kill é utilizado, THE Control_Plane SHALL emitir evento de domínio ContainerForceKilled contendo o motivo da escalação.

### Requisito 17: Requisitos Não-Funcionais de Performance

**User Story:** Como operador do sistema, eu quero que o sistema atenda requisitos de latência e escala definidos, para que a operação em produção seja viável.

#### Critérios de Aceitação

1. THE Data_Plane SHALL completar a sequência clone3→execve em menos de 50ms no percentil 95.
2. THE ACL SHALL completar a travessia da fronteira Cython (serialização + chamada + desserialização) em menos de 2ms no percentil 95.
3. THE Control_Plane SHALL suportar pelo menos 500 containers simultâneos por host sem degradação de latência acima dos limites especificados.
4. WHEN o Control_Plane sofre crash, THE Control_Plane SHALL ser capaz de reattach a containers em execução em menos de 500ms após reinicialização.
5. WHEN o Control_Plane sofre crash, THE Data_Plane SHALL manter os containers em execução sem interrupção — o crash do Python não afeta processos nativos já em execução.

### Requisito 18: Eventos de Domínio do Ciclo de Vida

**User Story:** Como desenvolvedor do sistema, eu quero que transições de estado do container emitam eventos de domínio imutáveis, para que o sistema seja auditável e extensível via event-driven architecture.

#### Critérios de Aceitação

1. WHEN um RunSpec é compilado com sucesso, THE Control_Plane SHALL emitir evento RunSpecCompiled contendo: event_id, runspec_hash, occurred_at, correlation_id.
2. WHEN um container inicia execução com sucesso (após handshake de bootstrap), THE Control_Plane SHALL emitir evento ContainerStarted contendo: event_id, container_id, runspec_hash, pidfd, occurred_at.
3. WHEN um container termina (por qualquer motivo), THE Control_Plane SHALL emitir evento ContainerExited contendo: event_id, container_id, exit_code, signal, duration, occurred_at.
4. WHEN um container é terminado por OOM killer, THE Control_Plane SHALL emitir evento ContainerOOMKilled contendo: event_id, container_id, memory_limit, occurred_at.
5. WHEN um container é terminado forçadamente via cgroup.kill, THE Control_Plane SHALL emitir evento ContainerForceKilled contendo: event_id, container_id, reason, occurred_at.
6. THE Control_Plane SHALL garantir que todos os eventos de domínio são imutáveis, contêm metadados de rastreabilidade (event_id, occurred_at, correlation_id) e são nomeados no passado.
