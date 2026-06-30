# Contribuindo

Obrigado por considerar uma contribuicao para o Fusion360 Plugin para Codex.

Este repositorio distribui um plugin local pronto. Mudancas devem preservar a
propriedade principal do projeto: o Codex fala com o Autodesk Fusion apenas por
meio do servidor seguro `fusion_agent`, nunca por uma superficie MCP crua.

## Antes de abrir uma mudanca

- Descreva o problema ou melhoria em uma issue.
- Informe se a mudanca afeta plugin, launcher, skill, setup ou wheel.
- Para qualquer alteracao de comportamento CAD, inclua plano de teste em mock ou
  dry-run.
- Evite commits que misturem documentacao, empacotamento e mudanca funcional.

## Padroes de seguranca

- Nao adicione servidores MCP crus do Fusion ao manifesto.
- Nao remova verificacoes de unidades explicitas.
- Nao transforme screenshot em unica evidencia de verificacao.
- Nao execute escrita real em documentos existentes sem inspecao e checkpoint.
- Prefira falhar fechado quando metadados, juntas, propriedades fisicas ou
  interferencias nao puderem ser verificados.

## Checklist local

Windows:

```powershell
.\scripts\setup.ps1
.\.venv\Scripts\python.exe scripts\fusion_agent_codex_mcp_launcher.py --check
```

Linux/macOS:

```bash
bash scripts/setup.sh
./.venv/bin/python scripts/fusion_agent_codex_mcp_launcher.py --check
```

## Pull requests

Inclua no PR:

- resumo da mudanca;
- motivacao;
- impacto para usuarios do plugin;
- comandos usados para validar;
- riscos conhecidos ou limitacoes.
