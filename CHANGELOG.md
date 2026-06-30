# Changelog

Todas as mudancas notaveis deste repositorio serao documentadas aqui.

## Unreleased

- Adiciona contrato de resposta MCP com `schema_version`, `tool`, `ok`,
  `artifacts` e erros estruturados.
- Adiciona `fusion_agent_capabilities` para descoberta agentica da superficie
  segura, workflow recomendado, allowlist de artefatos e politica de escrita
  real.
- Adiciona `fusion_agent_run_sandbox_session` para validar escrita real em
  documento scratch fechado sem salvar.
- Exige `dry_run_session_id` e `allow_existing_document_write=true` antes de
  `fusion_agent_run_session` em `mode=real`.
- Atualiza CLI, skill, README e testes para cobrir manifesto, schemas, sessoes,
  artefatos, memoria, skills, benchmark e integracao real obrigatoria.

## 0.1.0 - 2026-06-30

- Publicacao inicial do plugin Fusion Agent Codex.
- Inclui manifesto Codex, skill `fusion-cad-harness`, configuracao MCP, launcher,
  scripts de setup e wheel `fusion_agent_harness-0.1.0`.
- Adiciona documentacao completa para instalacao, uso seguro, modos de execucao
  e troubleshooting.
