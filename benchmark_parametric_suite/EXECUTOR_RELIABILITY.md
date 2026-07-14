# Confiabilidade do executor Python nativo do Fusion

## Estado e escopo

- Data da investigação: 2026-07-14.
- Produto observado: Autodesk Fusion `2704.1.23` no Windows.
- Superfície afetada: scripts enviados por `fusion_mcp_execute`.
- Escopo deste documento: evidência local, causa raiz conhecida, riscos ainda
  não comprovados, mitigação local implementada e critérios de teste pendentes.
- Fora de escopo: modificar ou redistribuir binários da Autodesk.

Este documento registra dois comportamentos distintos:

1. um vazamento **comprovado** de wrappers de `stdout` e `stderr`, que torna o
   interpretador recursivo depois de aproximadamente 368–369 execuções; e
2. um no-op silencioso observado no B07 com um script guarded de 37.976 bytes.
   Um limite efetivo próximo de 32 KiB é uma **hipótese empírica ainda não
   provada**, não uma conclusão.

## Fontes de evidência

### Instalação do Fusion

Binário que contém o preâmbulo do executor:

```text
C:\Users\mathe\AppData\Local\Autodesk\webdeploy\production\257040cabc1dffce734a8079453b19b0ffe2b735\Applications\Fusion\NaFusion10.dll
```

Inventário observado:

| Campo | Valor |
|---|---|
| Fusion | `2704.1.23` |
| DLL | `NaFusion10.dll` |
| Tamanho | `51.852.864` bytes |
| SHA-256 | `EFB40633D8B650859231653758B21ED6B7CEDDD3991FC5118B5E2E53E5AAEB9C` |
| Última modificação UTC | `2026-07-09T12:53:44Z` |
| Início da fonte Python embutida | offset `35.961.264` |
| Fim da fonte Python embutida | offset `35.962.257` |
| Comprimento da fonte embutida | `993` bytes |

Strings imediatamente adjacentes identificam a origem interna como:

```text
R:\Core\Fusion\Server\Fusion\MCP\ToolSupport\Script.cpp
```

### Logs do Fusion

Os números de linha abaixo referem-se aos arquivos locais:

```text
C:\Users\mathe\AppData\Local\Autodesk\Autodesk Fusion 360\STS2ERSCR2QS2JH4\logs\AppLogFile20260714T003252.log
C:\Users\mathe\AppData\Local\Autodesk\Autodesk Fusion 360\STS2ERSCR2QS2JH4\logs\AppLogFile20260713T145412.log
```

O primeiro log registra `Version : 2704.1.23` na linha 46. Esses logs são
evidência diagnóstica da máquina e não fazem parte do repositório.

### Artefatos rastreados no repositório

- `benchmark_parametric_suite/tools/fusion_python_reset/FusionPythonReset.py`
- `benchmark_parametric_suite/cases/b07_packaging_machine/reference_result.json`
- `harness/packages/fusion_mcp_adapter/real_client.py`
- `harness/packages/agent_core/fast_path.py`

## Defeito comprovado: cadeia ilimitada de streams

### Fonte embutida

A DLL contém esta lógica, numerada de acordo com a própria string Python
embutida:

```python
 1 import sys as _sys
 2 class _NsSanitizedWriter:
 3     _MAX_BYTES = 1024 * 1024
 4     def __init__(self, original):
 5         self._original = original
   ...
22     def flush(self):
23         return self._original.flush()
24     def __getattr__(self, name):
25         return getattr(self._original, name)
26 _sys.stdout = _NsSanitizedWriter(_sys.stdout)
27 _sys.stderr = _NsSanitizedWriter(_sys.stderr)
```

Cada execução redefine a classe e envolve o stream que já estava instalado.
Não há, nessa fonte, detecção de wrapper anterior, unwrap ou restauração. O
estado do processo evolui assim:

```text
chamada 1: NsWriter -> stream original
chamada 2: NsWriter -> NsWriter -> stream original
...
chamada N: NsWriter -> ... N wrappers ... -> stream original
```

Como a classe é redefinida em cada execução, um `isinstance` contra a classe
mais recente também não reconheceria com segurança todas as camadas antigas.

### Correlação com 368–369 chamadas

| Processo/log | Primeira manifestação | Chamadas `executeScript` acumuladas | Observação |
|---|---:|---:|---|
| `AppLogFile20260714T003252.log` | linha 20925 | 368 | erro surgiu primeiro em callback de seleção de outro add-in |
| mesmo processo | linha 21628 | 370 | primeira resposta MCP explicitamente falha nesse trecho |
| `AppLogFile20260713T145412.log` | linha 26660 | 369 | primeira resposta MCP falha no processo anterior |

Nos dois processos, o traceback mostra três frames visíveis de
`<string>:25:__getattr__` e mais 364 repetições. A linha 25 coincide exatamente
com `return getattr(self._original, name)` da fonte embutida. A variação de uma
ou duas chamadas é compatível com a profundidade de pilha já consumida pelo
contexto no qual o atributo do stream é consultado.

Essa repetibilidade em processos diferentes elimina como causa principal:

- a geometria do documento;
- um script específico do benchmark;
- o modelo que gerou o script;
- uma falha funcional anterior; e
- o fechamento de um documento.

Falha e `close` podem coincidir com o momento do esgotamento, mas não são
necessários para produzir o defeito.

## Sessão MCP não é processo Fusion

Os wrappers vivem no interpretador Python persistente do processo Fusion. Uma
sessão MCP nova não cria um interpretador novo e, portanto, não reduz a cadeia.

No log de 2026-07-14, a assinatura ocorreu em vários `sessionId`, entre eles:

- `8333039686928980761`;
- `1005457596884723517`; e
- `2993331374706572490`.

Durante o mesmo estado degradado, uma leitura nativa `activeCommand` passou nas
linhas 21644–21648. Isso separa dois estados de saúde:

```text
transporte/sessão MCP: READY
executor Python:       CORRUPTED
```

Reconectar, executar ping ou incrementar `connection_generation` não deve
limpar o estado do executor.

## Reset validado

O script normal do Fusion
`benchmark_parametric_suite/tools/fusion_python_reset/FusionPythonReset.py`
executa:

```python
sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__
```

No log de 2026-07-14:

1. a execução normal pela UI aparece na linha 22036, às `02:13:52`;
2. o canário MCP `after_ui_reset` é enviado nas linhas 22057–22060; e
3. a chamada termina com `success=true` na linha 22066, 15 segundos depois do
   reset.

Execuções posteriores também voltaram a produzir resultados. O reset não
modificou o documento; ele restaurou os streams pertencentes ao interpretador.

Um reconnect MCP isolado não produziu esse efeito.

## Mitigação implementada sem modificar a DLL

### 1. Colapsar a cadeia antes do código do usuário

O adaptador deve inserir um preâmbulo interno em **todo** script enviado a
`fusion_mcp_execute`, depois do lint e antes da primeira instrução do `run`.
Esse preâmbulo deve:

1. preservar o `_NsSanitizedWriter` externo criado para a chamada atual;
2. percorrer `_original` com `object.__getattribute__`, sem acionar o
   `__getattr__` defeituoso;
3. identificar wrappers antigos por estrutura, inclusive pelo nome de tipo
   `_NsSanitizedWriter`, porque cada chamada redefine a classe;
4. detectar ciclos e impor um limite de travessia;
5. ligar o wrapper externo diretamente ao primeiro delegate que não seja um
   `_NsSanitizedWriter`; e
6. usar `sys.__stdout__` ou `sys.__stderr__` apenas como fallback seguro.

O resultado desejado continua sendo um wrapper sanitizador por chamada:

```text
NsWriter atual -> stream base
```

Isso preserva a sanitização UTF-8/NUL, o limite de 1 MiB e o contador da chamada
atual. A injeção deve ocorrer centralmente no adaptador, tanto em transporte
persistente quanto legacy. Corrigir apenas `_guard_script` não cobre scripts de
inspeção, benchmark e outros scripts internos.

Como o preâmbulo contém reflexão interna que seria proibida ao modelo, ele deve
ser anexado **depois** do lint. A telemetria deve distinguir o hash do script
original do hash do payload efetivamente transmitido.

Esse contrato foi implementado em
`harness/packages/fusion_mcp_adapter/execute_guard.py` e aplicado centralmente
por `FusionMcpAdapter`. Testes offline comprovam preservação do writer atual,
colapso da cadeia, detecção de ciclos, idempotência e hashes distintos do código
original/transmitido. O Fast Execute também mede o payload final e bloqueia,
antes do dispatch, scripts acima de 28 KiB por padrão. O soak real e a sonda de
fronteira por bytes permanecem adiados; portanto, a hipótese de 32 KiB continua
sem promoção a fato.

### 2. Modelar saúde do executor separadamente

Estado sugerido:

```text
ExecutorHealth = HEALTHY | SUSPECT | CORRUPTED | RECOVERING
```

A detecção conhecida deve exigir a combinação de sinais, reduzindo falso
positivo com uma `RecursionError` legítima do script do usuário:

- `RecursionError` ou `Stack overflow`;
- frame em `<string>`;
- `__getattr__` repetido; e
- para o build conhecido, referência à linha 25.

Ao detectar a assinatura:

- marcar o executor como `CORRUPTED`;
- não repetir a chamada;
- bloquear localmente novos scripts genéricos;
- manter disponíveis ferramentas nativas que não executem Python; e
- limpar a quarentena somente após processo novo, ou reset explícito seguido
  de canário exato.

Uma inspeção opcional do log pode melhorar o diagnóstico local, mas não deve
ser dependência de funcionamento do harness.

## Semântica para mutações afetadas

Se a assinatura aparecer depois do dispatch de uma chamada mutável, não é
possível saber se a recursão ocorreu antes, durante ou depois do `run` do
usuário. A resposta correta é:

```text
MUTATION_OUTCOME_UNKNOWN
```

Regras obrigatórias:

- registrar exatamente um dispatch;
- nunca reenviar o script;
- não converter automaticamente o resultado em `execution_failed`;
- invalidar a saúde do executor, não necessariamente a conexão MCP;
- fazer readback somente por um oracle independente e disponível; e
- retornar `applied_verified` apenas se esse oracle provar integralmente o
  pós-estado. Caso contrário, manter `outcome_unknown`.

`real_client.py` atualmente converte uma resposta MCP funcional concluída em
`ToolResult` e a retorna. O detector deve atuar sobre todos os canais do
resultado antes da classificação final. `fast_path.py` já possui tratamento de
`MUTATION_OUTCOME_UNKNOWN`, mas o readback direcionado atual também usa script
Python e pode estar indisponível durante a quarentena.

## Segunda observação: no-op do B07 com 37.976 bytes

### Fatos observados

Na execução `ref_20260714T053257Z`, caso `b07_packaging_machine`:

- o payload do request 12, sessão `8117125482084897280`, contém um script
  guarded com exatamente `37.976` caracteres e `37.976` bytes UTF-8;
- o script contém uma definição `_fusion_agent_user_run` e uma definição
  pública `run`, cujo final chama `_fusion_agent_user_run`;
- `MCP:Script.cpp:112` registra o payload na linha 25614 do log;
- `MCP:Script.cpp:152` registra o arquivo temporário
  `script-3088ede7-73e8-4bad-a812-4bcd08609aad.py` na linha 25615;
- o runner registra início e execução Python nas linhas 25616–25620;
- a telemetria Autodesk retorna `success=true` em apenas 4 ms na linha 25621;
- o harness registra uma chamada mutável e um único dispatch em
  `reference_result.json`, linhas 1653–1679; e
- baseline e readback têm exatamente o mesmo fingerprint
  `2dcca78bfdb7c2c4f7dc19d061d10cf14aa2b0430bb5abaf897c3454e7167068`,
  com um componente raiz e zero parâmetros, corpos, features, occurrences e
  sketches; ver linhas 1431–1442 e 1632–1643.

O primeiro efeito do `_fusion_agent_user_run` seria adicionar parâmetros. A
ausência total de qualquer alteração, combinada com retorno em 4 ms e nenhuma
exceção, é forte evidência de que o entrypoint de usuário não foi efetivamente
executado. Ainda não há evidência suficiente para afirmar em qual camada ele
deixou de ser chamado.

### Hipótese de limite próximo de 32 KiB

`32 KiB = 32.768 bytes` é uma hipótese empírica motivada pelo tamanho do B07 e
por possível limite interno do runner. Ela **não está provada** porque:

- há somente uma observação controlada acima desse valor documentada aqui;
- não foi executada uma varredura em torno de 32.768 bytes;
- não foi isolado tamanho de complexidade sintática; e
- `success=true` da telemetria Autodesk não prova que `run(None)` foi chamado.

Até a hipótese ser testada, scripts native-fast acima de 32 KiB devem ser
tratados como risco de no-op silencioso. Opções fail-closed são bloquear e
rotear para o Safe Harness ou exigir uma representação menor. Dividir uma
mutação em vários scripts não é uma mitigação geral segura e não pode introduzir
replay ou estados intermediários não verificados.

### Experimento necessário para provar ou rejeitar a hipótese

Executar, em processo Fusion limpo e documento descartável, scripts
sinteticamente equivalentes que diferem apenas em padding inerte. Tamanhos
mínimos recomendados:

```text
24 KiB, 28 KiB, 31 KiB,
32.767, 32.768 e 32.769 bytes,
36 KiB, 37.976 bytes e 40 KiB
```

Para cada tamanho:

- três repetições;
- ordem embaralhada;
- um `run` trivial que emita marcador único e retorne o mesmo marcador;
- captura do tamanho real UTF-8 transmitido e do arquivo temporário;
- confirmação independente de que o marcador veio do `run`;
- stream chain colapsada antes da matriz, para não confundir os dois defeitos;
- mesmo processo e também processo novo para distinguir limite por chamada de
  estado acumulado; e
- registro de duração, conteúdo MCP e telemetria Autodesk.

Somente uma transição repetível em torno de um tamanho específico autoriza
transformar a hipótese em limite de produto.

## Plano de testes de confiabilidade

### Unitários

1. Reproduzir a fonte `_NsSanitizedWriter` extraída da DLL e comprovar
   `RecursionError` aproximadamente na 369ª camada.
2. Aplicar o colapso proposto por 1.000 ciclos e comprovar profundidade efetiva
   menor ou igual a um.
3. Preservar escrita UTF-8, remoção de NUL, `flush` e truncamento de 1 MiB.
4. Detectar a assinatura em `content`, `structuredContent`, `_meta`, JSON
   aninhado e `isError`.
5. Não classificar uma `RecursionError` comum de script nomeado como corrupção
   do capturador sem os demais sinais.

### Fake MCP

1. Leitura com a assinatura: zero retry, erro específico e quarentena.
2. Mutação com a assinatura: um dispatch, `MUTATION_OUTCOME_UNKNOWN` e zero
   replay.
3. Reconnect e novo `sessionId`: quarentena permanece.
4. Reset reconhecido sem canário: quarentena permanece.
5. Reset seguido de canário válido: transição para `HEALTHY`.
6. Chamada de execute bloqueada localmente durante quarentena: zero dispatch.
7. Ferramenta nativa sem Python permanece utilizável.

### Fusion real

O gate mínimo de integração é um soak de **pelo menos 500** scripts sequenciais.
O gate de release recomendado é **1.000** scripts. A execução deve:

- alternar sessões MCP sem reiniciar o Fusion;
- incluir scripts com `print`, retorno, exceção funcional e JSON;
- confirmar captura válida em todas as chamadas;
- registrar zero `RecursionError`, zero reconnect causado pelo executor e zero
  replay;
- verificar profundidade efetiva do wrapper menor ou igual a um por canário
  interno auditado; e
- repetir uma parte do soak com outro cliente MCP contribuindo chamadas, pois
  a cadeia pertence ao processo compartilhado.

A matriz de tamanho do B07 deve ser um teste separado do soak de streams. Um
resultado não pode mascarar o outro.

## Critérios de aceite

A confiabilidade do executor só deve ser considerada resolvida quando:

1. o soak real de 500 chamadas passa continuamente;
2. o soak de release de 1.000 chamadas passa antes de promoção;
3. nenhum reconnect é usado como falso reset do interpretador;
4. toda mutação afetada mantém exatamente um dispatch e sem replay;
5. a classificação `MUTATION_OUTCOME_UNKNOWN` é preservada quando o oracle não
   consegue provar o resultado;
6. reset + canário é o único caminho de recuperação no mesmo processo;
7. sanitização e limite de saída continuam funcionando; e
8. a hipótese de 32 KiB é confirmada por matriz controlada ou explicitamente
   rejeitada, sem ser apresentada como fato antes disso.

## Encaminhamento para a Autodesk

Um relatório upstream pode ser reproduzido sem geometria CAD:

1. iniciar Fusion 2704.1.23 em processo limpo;
2. executar um script trivial via `fusion_mcp_execute` aproximadamente 370
   vezes, inclusive trocando sessões;
3. observar `<string>:25:__getattr__` e `RecursionError: Stack overflow`;
4. anexar versão, hash da DLL, offsets, contagem e dois logs independentes; e
5. solicitar que `Script.cpp` restaure os streams em `finally`, reuse um único
   wrapper ou envolva sempre um delegate base não sanitizado.

A correção ideal permanece no produto Autodesk. A mitigação do harness existe
para manter a integração segura e determinística enquanto o comportamento do
executor nativo persistir.
