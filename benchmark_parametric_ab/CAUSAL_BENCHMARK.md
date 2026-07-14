# Benchmark causal de CAD paramétrico

Este framework separa a comparação Claude/Codex em três perguntas. Ele não
importa scripts congelados, não inicializa modelos e não se conecta ao Fusion.
Execução real só pode ser adicionada por adaptadores injetados pelo chamador.

## As três camadas

| Camada | Entrada mantida igual | Variável comparada | Contrato fail-closed |
|---|---|---|---|
| `transport_replay` | mesmo script SHA-256 e mesmo `runner_id` | conector/transporte de cada braço | executor deve reconhecer exatamente runner e artefato |
| `planner_isolated` | mesmo prompt, fixture, oracle e runner | plano/script previamente produzidos por cada modelo | planos/scripts ficam congelados por hash e o runner é comum |
| `native_e2e` | mesmo prompt, fixture e oracle | sistema completo | cada braço recebe route-lock exclusivo e deve confirmá-lo |

O oracle é um objeto separado do executor e recebe apenas `TrialContext`. Ele
não recebe a resposta autorrelatada do executor. Os braços são serializados;
não há paralelismo real nem risco de um route-lock global vazar entre trials.

## Entrada

O formato canônico é `fusion_causal_suite.v1`, validado por
[`causal_suite.schema.json`](causal_suite.schema.json). Cada caso precisa
declarar as três camadas. A ordem dos dois itens em `arms` define A e B para os
deltas pareados; a ordem de execução é contrabalanceada AB/BA por seed e
alternada entre repetições vizinhas.

Referências a artefatos são relativas ao diretório da suite e exigem SHA-256.
O plano de cada braço também deve obedecer a
[`planner_artifact.schema.json`](planner_artifact.schema.json), declarar
`provider`, modelo e reasoning profile, conter um build graph acíclico e estar
vinculado ao hash do script. Nenhuma resposta textual livre é interpretada pelo
runner.

Neste ambiente, o braço Codex está congelado como `gpt-5.6-sol` com reasoning
`ultra`; o caminho observado do launcher é
`%LOCALAPPDATA%\OpenAI\Codex\bin\a7c12ebff69fb123\codex.exe`, versão local
`codex-cli 0.144.0-alpha.4`. Uma coleta
estruturada pode ser feita assim (o comando abaixo não é executado pelo runner):

```powershell
$codex = "$env:LOCALAPPDATA\OpenAI\Codex\bin\a7c12ebff69fb123\codex.exe"
& $codex exec --ephemeral --sandbox read-only `
  --model gpt-5.6-sol -c 'model_reasoning_effort="ultra"' `
  --output-schema benchmark_parametric_ab\planner_submission.schema.json `
  --output-last-message outputs\planner\codex_submission.json `
  "Produza somente o bundle tipado de plano e script para o caso congelado."
```

Depois, o bundle é validado e separado sem importar ou executar Python:

```powershell
.venv\Scripts\python.exe -m benchmark_parametric_ab.causal_benchmark.freeze_submission `
  --submission outputs\planner\codex_submission.json `
  --output-dir outputs\planner\frozen
```

O mesmo [`planner_submission.schema.json`](planner_submission.schema.json) vale
para um export externo do Claude. O freezer rejeita campos extras, JSON
inválido, grafo cíclico, script maior que 64 KiB ou sobrescrita de artefatos;
em seguida liga o plano ao script pelo hash.

Falham antes de qualquer dispatch:

- JSON ou schema inválido;
- propriedades desconhecidas;
- braços ou casos duplicados;
- cobertura incompleta dos dois braços;
- route-lock repetido;
- caminho absoluto ou que escape do diretório da suite;
- arquivo ausente ou hash divergente;
- artefatos de planner idênticos nos dois braços;
- executor de camada ou oracle independente não registrado.

[`causal_suite.example.json`](causal_suite.example.json) é uma suite inerte e
válida. Os scripts nela referenciados lançam erro se alguém tentar importá-los;
o modo mock apenas reconhece seus hashes.

## CLI seguro

Somente validação:

```powershell
.venv\Scripts\python.exe -m benchmark_parametric_ab.causal_benchmark `
  --suite benchmark_parametric_ab\causal_suite.example.json `
  --mode validate
```

Agendamento mock completo, sem subprocessos:

```powershell
.venv\Scripts\python.exe -m benchmark_parametric_ab.causal_benchmark `
  --suite benchmark_parametric_ab\causal_suite.example.json `
  --mode mock `
  --output outputs\causal `
  --warmups 1 --repetitions 3 --seed 42
```

Não existe modo `real` no CLI. Para uma campanha real, o integrador instancia
`CausalBenchmarkRunner` e fornece um único `LayerExecutor` por camada e um
`IndependentOracle` por `oracle_id`. Isso mantém políticas, credenciais e
launchers fora do benchmark declarativo.

```python
runner = CausalBenchmarkRunner(
    output_dir="outputs/causal",
    executors={
        "transport_replay": transport_adapter,
        "planner_isolated": common_runner_adapter,
        "native_e2e": native_system_adapter,
    },
    oracles={"nema17_bracket_oracle": independent_oracle},
)
await runner.run_suite(
    "benchmark_parametric_ab/causal_suite.json",
    config=CausalRunConfig(warmups=1, repetitions=3, seed=42),
)
```

Para `native_e2e`, o runner define durante um único trial:

- `FUSION_CAUSAL_ROUTE_LOCK`;
- `FUSION_CAUSAL_ARM`;
- `FUSION_CAUSAL_TRIAL_ID`.

O adapter deve encaminhar o lock ao processo/servidor real e devolver
`observed_route_lock` igual ao valor esperado. Uma chamada fora da rota aborta
a campanha; o framework nunca repete automaticamente o trial.

## Saída e métricas

Cada execução recebe `run_id` e `trial_id` novos e grava um diretório imutável:

- `report.json`;
- `summary.md`;
- `trials.jsonl`;
- `environment.json`.

Warmups aparecem no trace, mas não entram nas métricas. O relatório agrega por
camada e braço: sucesso final, oracle pass, p50/p90 de duração, planejamento,
chamadas e tokens. Também calcula deltas pareados B−A e bootstrap de 95% sem
remover outliers. Gates básicos exigem oracle/sucesso integral, pares completos,
zero saves, zero duplicações e zero outcomes desconhecidos.
