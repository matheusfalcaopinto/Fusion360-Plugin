# Changelog

Todas as mudancas notaveis deste repositorio serao documentadas aqui.

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
