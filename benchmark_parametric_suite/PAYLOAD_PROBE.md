# Probe fail-closed do tamanho de payload do executor

Este scaffold testa, em uma campanha futura e controlada, a hipótese de um
limite próximo de 32 KiB no executor Python embarcado do Fusion. A implementação
presente é **somente offline**: não inclui adapter Autodesk, não abre Fusion,
não controla o desktop e seu CLI não possui modo real.

## O que fica congelado

A matriz canônica está em
[`payload_probe_matrix.json`](payload_probe_matrix.json) e mede bytes UTF-8 do
script **já protegido**, isto é, depois do preâmbulo central do executor:

```text
20480, 24576, 28672, 31744, 32512,
32767, 32768, 32769, 33024,
36864, 37976, 40960 bytes
```

Os pontos 25.468 B (`known_good_verified`) e 37.976 B
(`silent_noop_observed`) aparecem numa seção histórica separada. Eles são
contexto observacional, nunca expectativa e nunca oracle. O resultado de cada
trial novo é determinado apenas por readback independente.

Cada braço começa com um warmup pequeno de 20 KiB, registrado separadamente e
excluído das contagens medidas. Depois, cada tamanho é repetido três vezes em
ordem pseudoaleatória por seed, tanto no mesmo processo quanto num processo
novo. O adapter futuro é responsável por materializar essa diferença. O runner
exige `process_generation` constante em `same_process` e único em cada trial de
`fresh_process`.

## Script sintético e canaries

`PayloadScriptCalibrator` gera scripts com uma única topologia AST. Durante a
calibração, somente a string ASCII atribuída a
`_payload_probe_padding` muda. O calibrador aplica a mesma transformação usada
no wire, mede bytes UTF-8 e falha se não atingir exatamente o alvo.

O `run(_context: str)` valida primeiro um marcador de fixture descartável. Em
seguida grava atributos exclusivos na raiz do design nesta ordem:

1. `trial_id` e `start`;
2. padding inerte;
3. `mutation`;
4. `end`.

Esses atributos são o canary programático. Eles não dependem de texto impresso
ou do retorno autorrelatado do executor. "Uma mutação por trial" significa um
único dispatch MCP mutável contendo esse contrato completo; o runner nunca
chama o dispatcher uma segunda vez.

## Classificação independente

O readback produz exatamente uma destas classes:

- `complete`: identidade correta, todos os atributos exatos e oracle completo;
- `silent_noop`: transporte confirmou sucesso, mas nenhum canary ou mudança de
  estado apareceu;
- `partial`: qualquer efeito incompleto ou outcome não confirmado;
- `contaminated`: documento/fixture/canary estrangeiro, save, dispatch duplicado
  ou drift inesperado.

`partial`, `contaminated`, drift ou falha ao fechar/restaurar abortam a campanha
inteira. `silent_noop` é dado experimental válido e pode seguir para o próximo
trial desde que a restauração seja integral. Nenhum resultado é inferido apenas
de `success=true`, duração curta ou stdout.

## Contrato do adapter real futuro

O integrador deve fornecer dois objetos:

- `ProbeDispatcher.dispatch_once(request)`, com capabilities declarando
  `retry_policy="never"` e exactly-once;
- `ProbeLifecycle`, que cria um documento novo e não salvo, instala o marcador
  de fixture, faz readback independente, fecha sem salvar e restaura documento,
  fingerprint e conjunto de documentos abertos.

O runner bloqueia qualquer adapter real sem **duas confirmações explícitas**:
`confirm_real_dispatch=True` e `confirm_temporary_gate_elevation=True`. Como o
gate de produto atual é 28 KiB, a campanha futura precisará elevá-lo
temporariamente para no mínimo 40.960 B e restaurá-lo ao terminar. Essa exceção
serve somente ao experimento; não altera o default do produto.

Prepare, dispatch, readback e cleanup possuem timeouts positivos e limitados.
Timeout/cancelamento depois de entrar no dispatch vira outcome desconhecido,
jamais retry. O cleanup ainda é tentado dentro do seu próprio limite; em seguida
a campanha aborta e propaga a interrupção como falha, sem continuar a matriz.

Uma campanha real também deve ser serial, usar documento descartável por trial,
fechar sem salvar, restaurar o documento original e interromper imediatamente
em qualquer incerteza. O scaffold não envia script, não faz retry e não inclui
atalho para contornar essas regras.

## Validação offline

O comando abaixo apenas valida JSON, calibra todos os tamanhos e comprova a
topologia AST constante. O resultado deve registrar `dispatch_count: 0`.

```powershell
.venv\Scripts\python.exe -m benchmark_parametric_suite.payload_probe `
  --matrix benchmark_parametric_suite\payload_probe_matrix.json
```

Os testes mock cobrem calibração exata, ordem determinística, um dispatch por
trial, zero retry, as quatro classificações e abort imediato em partial, drift
ou restauração incompleta.
