# Politica de seguranca

Este projeto automatiza fluxos CAD e pode, quando configurado para Fusion real,
modificar documentos ativos. Trate qualquer mudanca que amplie a superficie de
execucao como sensivel.

## Versoes suportadas

| Versao | Suporte |
| --- | --- |
| 0.1.x | Suportada |

## Como reportar vulnerabilidades

Abra uma issue privada ou entre em contato com o mantenedor do repositorio antes
de publicar detalhes exploraveis. Inclua:

- versao do plugin;
- sistema operacional;
- modo usado (`mock`, `dry-run` ou real);
- configuracao relevante de MCP;
- passos de reproducao;
- impacto esperado.

## Escopo de seguranca

Sao considerados problemas de seguranca:

- exposicao direta de ferramentas cruas do Autodesk Fusion;
- execucao real sem etapa de planejamento/dry-run quando exigida;
- interpretacao ambigua de unidades;
- escrita destrutiva em documento existente sem protecao;
- bypass de verificacao programatica;
- vazamento de caminhos, tokens ou dados de projeto em artefatos publicados.

## Uso seguro

- Comece em mock ou dry-run.
- Use documentos descartaveis ao testar escrita real.
- Revise artefatos e traces antes de aceitar reparos automaticos.
- Nunca coloque tokens, endpoints privados ou arquivos proprietarios em issues
  publicas.
