# Fusion360 Plugin para Codex

Plugin local do Codex para operar o **Fusion Agent Harness**, uma camada segura,
mock-first e orientada por verificacao para automacoes de CAD no Autodesk Fusion
360 por MCP.

Este repositorio contem a distribuicao pronta do plugin:

- manifesto do plugin Codex em `.codex-plugin/plugin.json`;
- servidor MCP `fusion_agent` em `.mcp.json`;
- skill do Codex em `skills/fusion-cad-harness/SKILL.md`;
- scripts de setup para Windows e Linux/macOS;
- wheel Python embutido em `wheels/fusion_agent_harness-0.1.0-py3-none-any.whl`.

O plugin **nao registra ferramentas MCP cruas do Autodesk Fusion**. Todas as
acoes passam pelo harness seguro, que faz planejamento, validacao, execucao
mock/dry-run/real, verificacao programatica, reparo limitado, memoria e
journals antes de tocar em uma sessao real.

## Indice

- [Para que serve](#para-que-serve)
- [Principios de seguranca](#principios-de-seguranca)
- [Arquitetura](#arquitetura)
- [Conteudo do repositorio](#conteudo-do-repositorio)
- [Requisitos](#requisitos)
- [Instalacao rapida](#instalacao-rapida)
- [Instalacao no Codex](#instalacao-no-codex)
- [Primeira verificacao](#primeira-verificacao)
- [Como usar no Codex](#como-usar-no-codex)
- [Modos de execucao](#modos-de-execucao)
- [Variaveis de ambiente](#variaveis-de-ambiente)
- [Fluxos recomendados](#fluxos-recomendados)
- [Exemplos de prompts](#exemplos-de-prompts)
- [Solucao de problemas](#solucao-de-problemas)
- [Desenvolvimento](#desenvolvimento)
- [Publicacao e versao](#publicacao-e-versao)

## Para que serve

O Fusion Agent Codex conecta o Codex a um servidor MCP local chamado
`fusion_agent`. Esse servidor oferece ferramentas de CAD de alto nivel para:

- inspecionar o estado do ambiente e da sessao Fusion;
- validar especificacoes CAD com unidades explicitas;
- fazer dry-runs antes de executar alteracoes reais;
- executar sessoes mock ou reais por uma fachada segura;
- verificar corpos, parametros, bounding boxes, features, exports e screenshots;
- capturar evidencias visuais do viewport;
- ler artefatos e traces de sessoes;
- executar benchmarks;
- registrar memoria factual do projeto.

O objetivo e permitir automacao CAD assistida por agente sem expor o Codex
diretamente a uma superficie ampla e perigosa de comandos Fusion.

## Principios de seguranca

Este plugin foi empacotado para ser conservador por padrao.

1. **Mock-first**: quando nao houver endpoint Fusion real, o harness permanece em
   modo mock ou dry-run.
2. **Sem ferramentas cruas**: use apenas o servidor `fusion_agent`; nao conecte
   Codex diretamente a servidores chamados `fusion360`, `autodesk_fusion` ou
   equivalentes.
3. **Unidades explicitas**: especificacoes CAD devem usar valores como `10 mm`,
   `45 deg` ou parametros nomeados.
4. **Planejamento antes da escrita**: prefira `fusion_agent_plan_spec` ou
   `fusion_agent_dry_run_session` antes de `fusion_agent_run_session`.
5. **Verificacao programatica**: screenshots ajudam, mas a validacao primaria e
   por contagens, nomes, parametros, bounding boxes, integridade de corpos,
   features, exports, juntas e propriedades fisicas.
6. **Reparo limitado**: loops de reparo devem ter limite e classificar a causa
   da falha.
7. **Documentos existentes exigem cuidado**: inspecione, crie checkpoints quando
   aplicavel e confirme mudancas destrutivas.

## Arquitetura

Fluxo conceitual:

```text
Pedido do usuario
  -> memoria relevante
  -> planner
  -> CAD Spec validada
  -> executor mock/dry-run/real
  -> fachada segura do Fusion
  -> verificador
  -> reparo limitado
  -> journal, artefatos, traces e memoria
```

Componentes principais:

- **Codex plugin manifest**: descreve o plugin para o Codex.
- **Codex skill**: ensina o Codex a usar o harness com limites seguros.
- **MCP launcher**: resolve o Python correto e inicia `fusion_agent_mcp.server`.
- **Wheel embutido**: entrega o pacote `fusion-agent-harness` sem exigir checkout
  de desenvolvimento.
- **Setup scripts**: criam `.venv`, instalam o wheel e fazem uma checagem basica.

## Conteudo do repositorio

```text
.
|-- .codex/
|   `-- config.toml
|-- .codex-plugin/
|   `-- plugin.json
|-- skills/
|   `-- fusion-cad-harness/
|       `-- SKILL.md
|-- scripts/
|   |-- fusion_agent_codex_mcp_launcher.py
|   |-- setup.ps1
|   `-- setup.sh
|-- wheels/
|   `-- fusion_agent_harness-0.1.0-py3-none-any.whl
|-- .mcp.json
`-- README.md
```

Arquivos adicionados para GitHub, como `LICENSE`, `.gitignore`,
`.gitattributes`, `CONTRIBUTING.md`, `SECURITY.md` e templates em `.github/`,
servem apenas para manutencao do repositorio.

## Requisitos

Obrigatorios para instalar o plugin:

- Codex Desktop ou ambiente Codex com suporte a plugins locais;
- Python **3.11+** disponivel como `python`, ou caminho explicito em
  `FUSION_AGENT_PYTHON`;
- acesso de escrita ao diretorio do plugin para criar `.venv`.

Dependencias instaladas pelo wheel:

- `pydantic>=2.0`
- `typer>=0.12`
- `pytest>=8.0`
- `pytest-asyncio>=0.23`
- `rich>=13.0`
- `jsonschema>=4.0`
- `mcp>=1.0`
- `python-dotenv>=1.0`
- `PyYAML>=6.0`

Para executar contra o Autodesk Fusion real:

- Autodesk Fusion instalado normalmente em uma maquina Windows;
- servidor MCP/Fusion real acessivel por endpoint ou comando;
- variavel `FUSION_MCP_ENDPOINT` configurada quando o endpoint for remoto;
- documento descartavel ou checkpoint antes de qualquer escrita real.

## Instalacao rapida

Clone o repositorio:

```powershell
git clone https://github.com/matheusfalcaopinto/Fusion360-Plugin.git
cd Fusion360-Plugin
```

Prepare o ambiente local do plugin no Windows:

```powershell
.\scripts\setup.ps1
```

Linux/macOS:

```bash
bash scripts/setup.sh
```

O setup cria `.venv`, instala o wheel em `wheels/` e executa uma checagem do
launcher. A pasta `.venv` e local e nao deve ser commitada.

## Instalacao no Codex

Este repositorio ja esta no formato de plugin local do Codex. Ha duas formas
comuns de usa-lo.

### Opcao 1: usar o checkout como plugin local

1. Clone este repositorio em uma pasta estavel.
2. Rode `scripts/setup.ps1` ou `scripts/setup.sh`.
3. Aponte o Codex para o diretorio do plugin, mantendo a raiz do repositorio
   como raiz do plugin.
4. Reinicie ou recarregue o Codex se necessario.

### Opcao 2: copiar para a pasta de plugins pessoais

Copie a pasta inteira do repositorio para o diretorio de plugins pessoais usado
pelo seu Codex. Depois execute o setup dentro da copia:

```powershell
.\scripts\setup.ps1
```

Mantenha estes caminhos juntos:

- `.codex-plugin/plugin.json`
- `.mcp.json`
- `skills/`
- `scripts/`
- `wheels/`

O launcher calcula a raiz do plugin a partir de `scripts/`, entao mover apenas
um arquivo isolado quebra a resolucao de caminhos.

## Primeira verificacao

Depois do setup, rode:

```powershell
.\.venv\Scripts\python.exe scripts\fusion_agent_codex_mcp_launcher.py --check
```

Saida esperada:

- `plugin_root=` apontando para este repositorio;
- `harness_root=<installed-package>` quando estiver usando o wheel embutido;
- `bundled_wheels=1`;
- `installed_server_available=True`;
- `fusion_agent_codex=1`.

No Linux/macOS:

```bash
./.venv/bin/python scripts/fusion_agent_codex_mcp_launcher.py --check
```

## Como usar no Codex

Quando o plugin estiver ativo, o Codex tera acesso a skill
`fusion-cad-harness` e ao servidor MCP `fusion_agent`.

Fluxo recomendado em qualquer trabalho CAD:

1. Comece com `fusion_agent_doctor` e `fusion_agent_inspect`.
2. Busque memoria relevante com `fusion_agent_memory_search`.
3. Gere plano/spec com `fusion_agent_plan_spec`.
4. Rode `fusion_agent_dry_run_session`.
5. Execute `fusion_agent_run_session` somente quando o contexto estiver claro.
6. Valide com `fusion_agent_verify_active_design`.
7. Capture evidencia visual com `fusion_agent_capture_viewport`.
8. Leia artefatos e traces com `fusion_agent_read_session_artifact` e
   `fusion_agent_read_trace`.

O Codex deve rejeitar pedidos com unidades ambiguas. Por exemplo, prefira:

```text
Crie uma placa de montagem de 120 mm x 80 mm x 6 mm com quatro furos de 5 mm,
centralizados a 12 mm das bordas.
```

Evite:

```text
Crie uma placa 120 x 80 x 6 com quatro furos.
```

## Modos de execucao

### Mock

Modo seguro para testes, demos e desenvolvimento sem Autodesk Fusion. O harness
simula a sessao e produz artefatos verificaveis.

Use mock quando:

- estiver validando prompts;
- estiver testando instalacao;
- nao houver Fusion real disponivel;
- quiser reproduzir uma sessao sem risco de alterar documento real.

### Dry-run

Modo para planejar e validar a intencao antes da escrita real. Deve ser o passo
padrao antes de qualquer automacao em Fusion real.

Use dry-run quando:

- a geometria ainda nao foi revisada;
- as unidades, nomes ou parametros precisam ser conferidos;
- voce quer artefatos de planejamento antes de executar.

### Real Fusion

Modo para executar contra um endpoint Fusion real. Use apenas depois de
inspecionar ambiente, plano e dry-run.

Exemplo de endpoint remoto:

```powershell
$env:FUSION_MCP_ENDPOINT = "http://127.0.0.1:27182/mcp"
```

Linux conectando a um host Windows:

```bash
export FUSION_MCP_ENDPOINT="http://<windows-host>:17182/mcp"
```

## Variaveis de ambiente

| Variavel | Uso |
| --- | --- |
| `FUSION_AGENT_CODEX` | Definida como `1` pelo plugin para indicar execucao via Codex. |
| `FUSION_AGENT_PYTHON` | Caminho explicito para o Python que deve hospedar o MCP server. |
| `FUSION_AGENT_HARNESS_ROOT` | Checkout de desenvolvimento do harness; usado em vez do wheel instalado. |
| `FUSION_MCP_ENDPOINT` | Endpoint HTTP de um servidor MCP/Fusion real. |
| `PYTHONPATH` | Ajustado pelo launcher quando `FUSION_AGENT_HARNESS_ROOT` esta definido. |

## Fluxos recomendados

### Validar instalacao sem Fusion

1. Execute `scripts/setup.ps1` ou `scripts/setup.sh`.
2. Rode o launcher com `--check`.
3. No Codex, peça para inspecionar o harness.
4. Rode uma sessao mock simples.

### Criar uma peca simples

1. Descreva a peca com dimensoes e unidades.
2. Peça um plano ou dry-run.
3. Revise nomes de parametros e features.
4. Execute em mock.
5. Se precisar de Fusion real, configure endpoint e rode nova inspecao.
6. Execute em documento descartavel ou com checkpoint.
7. Verifique o design ativo.

### Trabalhar com montagens

O wheel inclui workflows candidatos para montagens como:

- `spacer_plate_assembly`;
- `hinge_assembly`.

Gates profissionais podem falhar fechado com causas como:

- `METADATA_MISSING`;
- `JOINT_MISMATCH`;
- `INTERFERENCE_DETECTED`;
- `PHYSICAL_PROPERTY_MISMATCH`;
- `SCREENSHOT_FAILED`.

Essas falhas sao esperadas quando o design nao satisfaz contratos de verificacao.
Use os artefatos e traces para diagnosticar.

## Exemplos de prompts

Inspecao:

```text
Use o Fusion Agent Harness para rodar doctor e inspect. Quero saber se estou em
mock, dry-run ou conectado a um Fusion real.
```

Dry-run de uma peca:

```text
Planeje e rode dry-run de uma placa de montagem de 100 mm x 60 mm x 6 mm, com
quatro furos de 5 mm a 10 mm das bordas, nomes estaveis para parametros e
verificacao de bounding box.
```

Sessao mock:

```text
Execute em mock uma peca chamada base_plate_demo: placa 120 mm x 80 mm x 8 mm,
quatro furos M5, chanfro de 1 mm nas bordas externas e captura de evidencia.
```

Montagem:

```text
Crie em mock uma montagem de duas placas separadas por quatro standoffs de
25 mm, com contratos de junta rigida, metadados e verificacao de interferencia.
```

Verificacao:

```text
Verifique o design ativo contra a especificacao planejada, leia os artefatos da
sessao e resuma qualquer divergencia entre geometria, nomes e propriedades.
```

## Solucao de problemas

### `python` nao encontrado

Instale Python 3.11+ ou defina:

```powershell
$env:FUSION_AGENT_PYTHON = "C:\Caminho\Para\python.exe"
```

Depois rode novamente:

```powershell
.\scripts\setup.ps1
```

### `installed_server_available=False`

O wheel pode nao ter sido instalado no Python que o launcher esta usando.
Rode o setup novamente ou confirme `FUSION_AGENT_PYTHON`.

### `Missing bundled fusion_agent_harness wheel`

Confirme se o arquivo existe em `wheels/`. Este repositorio deve manter o wheel
publicado porque ele faz parte da distribuicao do plugin.

### Codex nao mostra o servidor `fusion_agent`

Confira:

- `.codex-plugin/plugin.json` existe na raiz do plugin;
- `.mcp.json` existe na raiz do plugin;
- o setup foi executado;
- o Codex foi recarregado depois da instalacao;
- o caminho do plugin nao aponta para uma subpasta.

### Conexao real com Fusion falha

Comece em mock/dry-run. Depois valide:

- Fusion esta aberto;
- o servidor MCP/Fusion real esta rodando;
- `FUSION_MCP_ENDPOINT` aponta para host e porta corretos;
- firewall permite conexao;
- o documento ativo pode ser usado para teste.

### Unidades ambiguas

Reescreva a especificacao com unidades explicitas:

- `10 mm`, `2.5 cm`, `45 deg`;
- nomes como `plate_length = 120 mm`;
- tolerancias e offsets nomeados quando aplicavel.

## Desenvolvimento

Este repositorio e uma distribuicao pronta do plugin. Para desenvolvimento do
harness a partir de um checkout fonte, defina:

```powershell
$env:FUSION_AGENT_HARNESS_ROOT = "C:\caminho\para\fusion-agent-harness"
.\scripts\setup.ps1
```

Linux/macOS:

```bash
export FUSION_AGENT_HARNESS_ROOT="/caminho/para/fusion-agent-harness"
bash scripts/setup.sh
```

Quando `FUSION_AGENT_HARNESS_ROOT` esta definido, o launcher monta `PYTHONPATH`
com `packages/` e `apps/` do checkout fonte. Em uma instalacao normal, deixe essa
variavel vazia para usar o wheel embutido.

## Publicacao e versao

Versao atual do plugin: `0.1.0`.

Ao publicar novas versoes:

1. atualize `.codex-plugin/plugin.json`;
2. gere um novo wheel `fusion_agent_harness-<versao>-py3-none-any.whl`;
3. mantenha apenas wheels intencionais em `wheels/`;
4. rode setup e `--check`;
5. atualize este README e `CHANGELOG.md`;
6. crie uma tag GitHub como `v0.1.0`.

## Licenca

Distribuido sob a licenca MIT. Veja `LICENSE`.
