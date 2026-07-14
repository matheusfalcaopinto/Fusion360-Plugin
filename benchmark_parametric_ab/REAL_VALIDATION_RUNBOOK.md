# Roteiro de validação real adiada

Este roteiro existe para executar a etapa interativa somente quando o computador
estiver livre. Nenhum item abaixo deve ser iniciado enquanto outra pessoa estiver
usando Fusion 360 ou Claude Desktop.

## Condições de entrada

- reservar uma janela exclusiva e serializada;
- registrar documentos abertos, documento ativo, versão do Fusion, commit, wheel,
  plugin, endpoint MCP e fingerprints;
- confirmar o wheel instalado e a superfície de 35 ferramentas `fusion_agent_*`;
- exigir `initialize` e `tools/list` válidos em cada conector antes de envolver um
  modelo;
- usar apenas documentos novos, não salvos e marcados por `trial_id`;
- nunca salvar, sincronizar, usar projeto pessoal ou repetir uma mutação cujo
  resultado seja desconhecido.

O conector Autodesk Fusion 1.0.1 observado no Claude Desktop falhou em
`tools/list` dentro de `mcp-remote` 0.1.38. Enquanto a resposta não contiver um
array real de ferramentas, o braço Claude deve ser classificado como
`infrastructure_blocked`, nunca como falha do Fable 5.

## Ordem dos gates

1. **P0 somente leitura:** vinte leituras sequenciais na mesma sessão; esperar um
   `initialize`, um `tools/list`, zero reconnects e fila serializada.
2. **P1 somente leitura:** `api_documentation`, resumo do documento, inspeção
   direcionada e screenshot com `structuredContent` e bloco PNG preservados.
3. **P1 escrita mínima:** criar uma feature aditiva nomeada em documento
   descartável, com baseline, um único dispatch e readback/oracle independente.
4. **Sonda do executor:** executar a matriz de payload em campanha separada. O
   limite de 28 KiB só pode ser elevado explicitamente para essa campanha; cada
   tamanho usa um documento e uma mutação novos, sem retry.
5. **Benchmark causal:** executar `transport_replay`, depois
   `planner_isolated`, e somente então `native_e2e`.
6. **Suite difícil:** promover para B02–B07 apenas depois que os gates anteriores
   passarem sem drift, save, duplicação ou restauração incompleta.

## Braços congelados

- Claude: `Fable 5 Alto` no cliente Anthropic, com export estruturado do plano e
  script antes da execução.
- Codex: `gpt-5.6-sol`, reasoning `ultra`, tarefa efêmera e sandbox read-only para
  a fase de planejamento.

Em `transport_replay`, os dois braços recebem o mesmo script SHA-256 e o mesmo
runner. Em `planner_isolated`, cada modelo produz um bundle que obedece a
`planner_submission.schema.json`; o freezer valida e separa plano/script sem
executar Python. Em `native_e2e`, cada braço recebe um route-lock exclusivo e
uma tarefa nova. A ordem é AB/BA definida pelo seed.

## Oracles mínimos

- identidade e marker do documento;
- contagem de bodies e lumps;
- conectividade e ausência de geometria suspensa;
- bounding box global e por componente;
- sketches totalmente constrangidos quando exigidos;
- dimensões paramétricas e semântica de slots;
- feature health, nomes e propagação da ECO;
- zero saves, zero dispatches duplicados e restauração exata do inventário.

Screenshots são evidência secundária. A aprovação vem do oracle programático e
do teardown. `applied_verified` relatado pelo executor, sozinho, não aprova o
trial.

## Critérios de aborto

Abortar a campanha inteira diante de:

- troca inesperada do documento ativo ou do fingerprint;
- diálogo interativo aberto;
- `tools/list` inválido, manifest drift ou route-lock incorreto;
- canary parcial, outcome desconhecido ou timeout pós-dispatch;
- qualquer save, sincronização ou geometria fora do documento marcado;
- falha ao fechar sem salvar ou restaurar exatamente o estado original.

## Artefatos esperados

Cada `run_id` deve preservar `report.json`, `summary.md`, `trials.jsonl`,
`environment.json`, traces redigidos, saídas dos oracles e imagens secundárias.
Os resultados históricos NEMA17 permanecem baseline exploratório `n=1`; não
devem ser misturados estatisticamente com a campanha causal nova.
