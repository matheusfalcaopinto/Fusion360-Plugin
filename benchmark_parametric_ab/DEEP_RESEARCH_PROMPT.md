# Prompt para Deep Research — agente Codex para CAD paramétrico no Autodesk Fusion

Atue como pesquisador principal de sistemas de agentes para CAD paramétrico. Faça uma investigação técnica profunda, atualizada e orientada a implementação sobre como aumentar drasticamente a confiabilidade do Codex ao projetar peças paramétricas no Autodesk Fusion, especialmente quando comparado ao Claude Desktop usando um Fusion connector.

Não quero uma comparação genérica entre modelos. O objetivo é descobrir quais diferenças de arquitetura, planejamento, representação geométrica, desenho das ferramentas, ciclo de inspeção/edição e verificação podem explicar a diferença observada — e propor mudanças concretas para eliminá-la ou mitigá-la.

## Evidência experimental disponível

Foi executado um benchmark A/B end-to-end no mesmo PC e na mesma sessão do Autodesk Fusion. A tarefa idêntica era criar, em documento novo e não salvo, um suporte paramétrico ajustável para motor NEMA 17 com:

- base de 90 × 70 × 6 mm;
- flange traseira;
- envelope final de 90 × 70 × 66 mm;
- furo central Ø24;
- quatro furos Ø4,5 em padrão quadrado de 31 mm;
- dois slots de comprimento total 24 mm e largura 6,5 mm;
- dois gussets triangulares conectados e espelhados;
- fillets R3;
- 17 parâmetros de usuário;
- sketches ocultos e totalmente restritos;
- um único corpo sólido conectado.

Resultado observado do Claude Desktop, com a opção de modelo exibida pelo cliente como “Fable 5 Alto”:

- 1 corpo sólido, 1 lump e envelope correto de 90 × 70 × 66 mm;
- 17 parâmetros corretos;
- 5/5 sketches totalmente restritos;
- 7/7 features saudáveis;
- slots com 24 mm de comprimento total;
- gussets conectados e posicionados corretamente;
- uma única mutação observável;
- porém excedeu o limite de 20 minutos e terminou sem resposta textual por limite de mensagem/uso.

Resultado observado do Codex:

- 17 parâmetros e 7 features saudáveis;
- base, flange, furos e fillets visualmente plausíveis;
- 3 corpos sólidos e 3 lumps, em vez de um;
- dois gussets desconectados e suspensos abaixo da peça;
- bbox global de 90 × 70 × 130,06 mm, apesar de o corpo principal isolado ter a bbox esperada;
- apenas 4/5 sketches totalmente restritos;
- slots com 30,5 mm de comprimento total, em vez de 24 mm;
- duas tentativas funcionais falharam e foram revertidas; a terceira terminou;
- cada mutação foi transmitida exatamente uma vez, sem duplicação;
- o Fast Path retornou `applied_verified`, embora seu próprio summary já mostrasse 3 corpos visíveis e a bbox global incorreta.

A reconstrução inicial identificou três falhas principais:

1. O script Codex improvisou uma transformação de coordenadas por produto escalar para um sketch em plano YZ deslocado, em vez de usar `Sketch.modelToSketchSpace`/`sketchToModelSpace`. Isso trocou ou inverteu eixos e posicionou os gussets aproximadamente 64 mm abaixo da peça.
2. O Codex tratou o argumento geométrico do slot como metade do comprimento total externo. Para obter comprimento total `L` e largura `W`, a construção usada exigia distância entre centros `L - W`, ou meia distância `(L - W)/2`, não `L/2`.
3. O contrato de verificação foi derivado dos nomes e expectativas do próprio script. Ele validou o corpo principal e a existência das features, mas não compilou os requisitos do usuário em invariantes globais independentes, como `body_count == 1`, `lump_count == 1`, bbox global, conectividade dos gussets, comprimento real dos slots e restrição completa dos sketches.

Considere `REPORT.md` apenas como registro histórico não scoreable. O `results.json` legado e traces/audits raw foram removidos por não estarem vinculados à revisão/documento e por conterem referências privadas. Qualquer nova análise deve usar somente artifacts públicos de um run 0.4.1 com `RevisionIdentity` exata, provenance completa, oracle independente e comparator elegível. “Fable 5 Alto” é apenas o rótulo observado na interface; não presuma que seja o nome público ou canônico de um modelo.

## Arquitetura atual que deve ser preservada

O sistema é o Fusion Agent Codex 0.2:

- somente ferramentas públicas `fusion_agent_*`; ferramentas Autodesk cruas permanecem internas;
- o Codex externo é o planner; não há segundo modelo ou credencial de modelo dentro do harness;
- o harness controla conexão persistente MCP, política, lint, execução, verificação, telemetria e auditoria;
- mutações são enviadas exatamente uma vez e nunca recebem retry após dispatch;
- perda de conexão após dispatch gera outcome incerto e somente readback pode ocorrer após reconnect;
- operações destrutivas, bulk, ambíguas, ocultas ou compartilhadas permanecem no Safe Harness;
- Fast Path atual segue baseline direcionado → uma escrita → readback → assertions;
- screenshots são evidência secundária; verificação programática é obrigatória;
- o objetivo não é simplesmente permitir Python Autodesk irrestrito nem remover guardrails.

## Perguntas de pesquisa

Investigue e responda, com evidências:

1. Quais diferenças publicamente documentadas ou observáveis no Claude Desktop, MCP, Fusion connectors, skills/prompts e desenho de ferramentas podem plausivelmente produzir um fluxo CAD mais coerente? Procure documentação oficial, código público, manifests, schemas, repositórios, discussões técnicas e exemplos. Separe rigorosamente fatos públicos, inferências a partir do trace e especulação. Não invente detalhes proprietários do Claude.
2. Existe implementação pública ou documentação verificável do connector Fusion usado com Claude Desktop? Se existir, analise ferramentas, schemas, prompts, persistência de estado, granularidade das operações, ciclo de readback e tratamento de erros. Se não existir, diga claramente e proponha um checklist seguro de inspeção local para comparar instalações, sem coletar tokens, credenciais ou dados pessoais.
3. O que a literatura e os sistemas atuais mostram sobre agentes para CAD paramétrico, geração de feature trees, program synthesis para CAD, constraint solving, geometric reasoning, topological naming, self-correction e verificação independente?
4. Para esta classe de tarefa, compare ao menos estas estratégias:
   - geração de um script Python monolítico;
   - execução incremental feature por feature com inspeção entre etapas;
   - DSL/IR semântica de CAD compilada para a API do Fusion;
   - biblioteca de primitivas tipadas e testadas, mantendo o modelo como planner;
   - plano de features explícito com dependências e invariantes;
   - estado geométrico simbólico ou scene graph mantido pelo harness;
   - executor com critic/verificador independente do plano gerado.
5. Quais falhas são mais provavelmente de modelo, de prompt/skill, de tool schema, de API de baixo nível, de falta de estado, de estratégia de reparo e de oracle? Atribua confiança e diga que experimento discriminaria cada hipótese.
6. Até que ponto prompt engineering pode ajudar? Identifique o limite provável de melhorias apenas por prompt e quando é necessária mudança estrutural no harness.
7. Como evitar especificamente erros de frame local, semântica de slots, corpos desconectados, sketches sub-restringidos, referências frágeis e reparos que tratam sintomas?
8. Como impedir falsos `applied_verified` sem tornar cada execução excessivamente lenta ou cara?
9. Quais sinais devem provocar bloqueio antes da mutação, verificação extra após a mutação, reparo autorizado ou roteamento para Safe Harness?
10. Como criar um benchmark estatisticamente defensável que separe capacidade do modelo de qualidade do conector/harness?

## Solução esperada

Proponha uma arquitetura “Fusion Agent Codex 0.3” implementável. Ela deve incluir:

- diagrama do fluxo de planejamento, compilação, execução e verificação;
- representação intermediária recomendada para feature plans, coordenadas, referências e invariantes;
- catálogo mínimo de primitivas semânticas, incluindo slot por comprimento total, sketch em plano tipado, extrude/join, mirror, gusset, hole pattern e fillet;
- como essas primitivas devem usar APIs oficiais como `modelToSketchSpace` e evitar transformações manuais;
- schema de ferramentas MCP ou JSON para `plan`, `inspect`, `execute_step`, `verify` e `repair`, sem expor Autodesk cruamente;
- compilador requisito → assertions, com rastreabilidade entre cada frase verificável do pedido e seu oracle;
- verificação global obrigatória de corpos, lumps, bbox, conectividade, parâmetros, graus de liberdade dos sketches, feature health e dimensões geométricas reais;
- política de confiança/cobertura que só permita `applied_verified` com 100% dos requisitos obrigatórios verificados; caso contrário, `applied_unverified`;
- estratégia de reparo baseada no delta observado, sem reenviar mutações incertas;
- papel opcional de visão computacional apenas como critic secundário;
- impacto esperado em qualidade, latência, número de chamadas e complexidade.

Apresente três níveis de intervenção:

1. **Correções imediatas, 1–2 semanas:** mudanças pequenas no Fast Path e no oracle que bloqueiem a falha reproduzida.
2. **Arquitetura intermediária, 4–8 semanas:** primitivas semânticas, frames tipados, feature plan e verificação compilada.
3. **Arquitetura robusta de longo prazo:** DSL/IR CAD, executor incremental, estado geométrico e benchmark contínuo.

Para cada recomendação, forneça prioridade, esforço, dependências, risco, ganho esperado e teste de aceite. Inclua pseudocódigo ou schemas concretos nos pontos críticos. Diga explicitamente quais mudanças têm maior probabilidade de fechar a diferença observada com o Claude.

## Padrão de evidência

- Pesquise a web atual e cite diretamente cada afirmação técnica relevante.
- Priorize documentação oficial da Autodesk, Anthropic/MCP e OpenAI/Codex; depois artigos científicos, repositórios oficiais e projetos open source com código inspecionável.
- Em questões técnicas, prefira fontes primárias a posts agregadores.
- Informe data ou versão quando a API ou produto puder ter mudado.
- Não trate marketing como evidência de capacidade.
- Não conclua causalidade a partir deste único benchmark.
- Marque cada conclusão importante como `confirmado`, `fortemente sustentado`, `hipótese plausível` ou `desconhecido`.
- Quando não houver evidência pública sobre o funcionamento interno do connector Claude/Fusion, não preencha a lacuna com suposições.

## Formato do relatório final

Produza o relatório em português, nesta ordem:

1. Resumo executivo com as cinco ações de maior impacto.
2. Reconstrução causal da falha observada.
3. O que é conhecido e desconhecido sobre Claude Desktop + Fusion connector.
4. Estado da arte relevante, com fontes.
5. Matriz de hipóteses, evidência, confiança e experimento discriminante.
6. Comparação das alternativas arquiteturais.
7. Arquitetura recomendada para Fusion Agent Codex 0.3, com diagrama e schemas.
8. Roadmap imediato, intermediário e longo prazo.
9. Plano experimental A/B e gates quantitativos.
10. Riscos, limitações e perguntas ainda abertas.
11. Lista de fontes primárias com links diretos.

Evite conselhos genéricos como “melhorar o prompt”, “adicionar mais validação” ou “usar chain-of-thought”. Cada recomendação deve apontar qual falha concreta corrige, onde seria implementada e como sua eficácia seria medida.
