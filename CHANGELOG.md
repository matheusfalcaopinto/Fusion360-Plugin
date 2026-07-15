# Changelog

Todas as mudancas notaveis deste repositorio serao documentadas aqui.

## 0.4.0 - 2026-07-15

- Adiciona benchmark publico normalizado com adapters isolados e pinados para
  Fusion Agent Codex, Autodesk oficial, Faust, FrankS e ndoo; execucoes sem
  driver, licenca ou entitlement ficam honestamente como `not_run`.
- Adiciona CI completa em Windows, Ubuntu e macOS/Python 3.11, smoke de pacote
  em Python 3.12 e nightly/manual self-hosted para Fusion real em fixtures
  descartaveis, com artifacts preservados tambem em falha.
- Exige tres nightlies reais consecutivos para release 0.4.x.
- Evolui memoria para schema v2 com source, provenance, trust, scope, hash,
  expiracao, citacoes e taint; legado passa a `legacy_unverified`.
- Bloqueia segredos, instrucoes executaveis, prompt injection e documentacao
  remota nao pinada no fluxo de memoria.
- Remove o alias depreciado `exactly_once_dispatch`; a semantica publica e
  `post_dispatch_replay_suppressed`, sem promessa de idempotencia end-to-end.

## 0.3.0 - 2026-07-15

- Adiciona perfis `normal`, `advanced`, `diagnostic`, `benchmark` e `all`, com
  filtro em listagem e chamada; `normal` passa a ser o padrao com 12 tools.
- Publica output schemas dedicados, annotations conservadoras, resources
  paginados e quatro prompts MCP de workflow.
- Introduz CadSpec v2 estrito com dependencias, referencias tipadas,
  requisitos verificaveis e capability preflight antes do primeiro dispatch.
- Adiciona capability packs para sketch constraints/dimensions, revolve,
  sweep, loft, patterns, mirror, boolean/split, joints/rigid groups, analises e
  import/export. Sheet metal e CAM permanecem experimentais e gated.
- Formaliza `autodesk_http` e `faust_stdio` sem fallback automatico; Faust usa
  `fusion360-mcp-server==0.1.0`, subset tipado e nunca expoe `execute_code`.
- Adiciona politica de endpoint loopback/allowlist com HTTPS, DNS revalidation,
  token do ambiente e redacao de logs.
- Prepara `fusion_data` como segundo MCP OAuth opcional gerenciado pelo Codex,
  sem hard-code de URL, token no harness ou chamada cross-MCP.

## 0.2.2 - 2026-07-15

- Substitui a promessa de exactly-once por `no automatic replay after
  dispatch`, com `dispatched`, `may_have_applied`,
  `post_dispatch_replay_suppressed` e `mutation_outcome` autoritativos.
- Implementa Safe Change preview v2 com identidade estavel, fingerprint,
  bindings, budget/completude e maquina `ready -> applying -> consumed`, mais
  `stale` para drift; legacy preview exige refresh.
- Elimina TOCTOU sob lock, valida todos os alvos antes da primeira mutacao e
  consome permanentemente qualquer preview que tenha sido despachado.
- Separa mutation/assertion status, intent coverage e verification level;
  `applied_verified` requer readback completo e cobertura obrigatoria total.
- Adiciona release por tag com versoes cruzadas, build duplo determinista,
  comparacao do wheel rastreado e publicacao de plugin, wheel e SHA256SUMS.
- A 0.2.1 nao foi publicada: seu trabalho foi absorvido por esta release.

## 0.2.1 - nao publicada; absorvida pela 0.2.2

- Mantem `legacy` como transporte instalado padrao e adiciona os modos
  `persistent_post_only` e `auto` para validar persistencia sem depender do
  listener GET/SSE opcional.
- Separa efeito da chamada de sua politica de replay. Mutacoes e scripts
  internos de leitura sao transmitidos uma unica vez; timeout pos-dispatch de
  leitura retorna `READ_TIMEOUT_MAY_STILL_BE_RUNNING` e aplica cooldown.
- Centraliza a sessao persistente em uma worker task serial, incluindo
  initialize, manifest, chamadas e fechamento limitado a dois segundos.
- Torna `fusion_agent_inspect`, `fusion_agent_targeted_inspect` e
  `fusion_agent_compact_snapshot` limitados por entidades visitadas, deadline e
  tamanho da resposta, com completude e motivo de interrupcao explicitos.
- Remove propriedades fisicas, joints e metricas de receitas do fluxo de
  inspecao padrao; consultas por token e path evitam scans globais.
- Bloqueia mutacoes com baseline parcial e impede `applied_verified` quando o
  readback foi truncado, inexato ou perdeu evidencias durante a serializacao.
- Amplia `benchmark_suite.v2` com primeira leitura fria persistente, inspecao
  global limitada e lookup direto por token em montagem grande.
- Preserva as 35 ferramentas `fusion_agent_*`, Fast Path `read_only`, schemas
  anteriores e o fallback `legacy` durante toda a serie 0.2.x.
- Corrige o round-trip de `entity_token` para retornos Python e colecoes
  Autodesk de `Design.findEntityByToken`; falhas da API agora produzem
  evidencia incompleta e bloqueiam o fluxo em vez de parecer zero matches exato.

## 0.2.0 - 2026-07-13

- Move a fonte canonica do harness para `harness/` e adiciona build determinista
  do wheel `fusion-agent-harness 0.2.0`, com `RECORD` e hashes verificados.
- Introduz runtime unico e conexao MCP persistente lazy, serializacao de
  operacoes, shutdown limitado, timeouts por semantica e fallback `legacy`.
- Suprime replay automatico depois do dispatch de mutacoes: falha nesse ponto
  retorna `MUTATION_OUTCOME_UNKNOWN` e exige readback antes de recuperacao.
- Migra manifests para schema v2 com fingerprint canonico, persistencia atomica,
  deteccao de drift e migracao do alias real legado.
- Adiciona `fusion_agent_native_read`, `fusion_agent_targeted_inspect`,
  `fusion_agent_fast_execute` e `fusion_agent_recover_change`, elevando a
  superficie publica segura de 31 para 35 ferramentas.
- Adiciona linter AST, guard de identidade do documento, baseline/readback,
  assertions programaticas, preservacao de `structuredContent` e PNG real.
- Usa identidade estavel de documento (`dataFile.id` ou marker descartavel),
  binding canonico do root component e normalizacao de `BaseVector`; respostas
  Autodesk com `success=false` viram erro funcional mesmo quando `isError=false`.
- Adiciona `benchmark_suite.v2`, driver internal/Codex E2E, A/B contrabalanceado,
  route-lock, oracles independentes, estatisticas e artefatos por `run_id`.
- Implementa lifecycle real de fixture nao salva com marker, `close(False)` e
  restauracao do documento original; documento original nao salvo e sem
  identidade persistente bloqueia antes da criacao da fixture.
- Endurece a revisao final: ACK negativo aninhado continua erro funcional, o
  simbolo interno do wrapper AST e reservado, fixtures sao fechadas por marker
  ou fingerprint com inventario de documentos, e o handshake MCP anuncia a
  versao `0.2.0` do harness em vez da versao do SDK.
- Normaliza centralmente a cadeia `_NsSanitizedWriter` sem descartar o capturador
  da chamada atual, registra hashes/tamanhos original e transmitido e bloqueia
  Fast Execute acima de 28 KiB antes do dispatch.
- Adiciona benchmark causal offline em tres camadas (`transport_replay`,
  `planner_isolated` e `native_e2e`) com artefatos congelados, route-lock,
  ordem AB/BA e oracle independente.
- Mantem Fast Path em `read_only` por padrao; delete, cleanup, bulk, move,
  visibilidade, componentize e entidades ocultas/compartilhadas permanecem no
  Safe Harness.

## 0.1.0+guardrails - 2026-07-06

- Adiciona ferramentas MCP seguras para `session_health`, `readiness_report`,
  `compact_snapshot`, `hub_inventory`, `safe_change_preview` e
  `safe_change_apply`.
- Separa manifests latest real/mock para evitar que discovery mock sobrescreva
  a superficie real.
- Adiciona guardrail de planner para recusar auditoria, hub, reorg, delete,
  cleanup e read-only como `unsupported_for_planner`.
- Torna captura de viewport prova explicita: arquivos ausentes ou vazios falham
  e capturas validas retornam `evidence_quality=verified_file`.
- Atualiza skill, README e regras globais para priorizar diagnostico, snapshots
  programaticos e destructive batch guard.
- Adiciona `scripts/validate_plugin.py` e integra a checagem aos scripts de
  setup.
- Adiciona testes unitarios para guardrails, manifests, diff de snapshots,
  nomes duplicados e captura invalida.

## 0.1.0 - 2026-06-30

- Publicacao inicial do plugin Fusion Agent Codex.
- Inclui manifesto Codex, skill `fusion-cad-harness`, configuracao MCP, launcher,
  scripts de setup e wheel `fusion_agent_harness-0.1.0`.
- Adiciona documentacao completa para instalacao, uso seguro, modos de execucao
  e troubleshooting.
