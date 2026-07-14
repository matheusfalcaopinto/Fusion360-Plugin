# Catálogo técnico de benchmarks CAD de competição

Este catálogo transforma referências públicas de competições CAD em propostas de
benchmark **clean-room** para Fusion. O objetivo não é reproduzir uma prova, uma
peça ou uma solução oficial. É preservar as classes de dificuldade que realmente
separam um modelo apenas visual de um modelo mecânico paramétrico utilizável:
intenção de projeto, regeneração, montagem, movimento, ECO, documentação e
verificação independente.

## Política de procedência e não redistribuição

- A disponibilidade pública de um pacote não implica permissão para republicar
  desenhos, modelos, soluções, marking schemes ou dimensões da prova.
- Nenhum arquivo Autodesk Inventor, Fusion, desenho, imagem de solução ou dado de
  avaliação do pacote oficial deve ser incorporado a este repositório.
- Novos casos devem usar nomes, dimensões, topologia, arquitetura de montagem,
  scripts e oráculos criados de forma independente.
- As fontes oficiais servem para calibrar **categorias e escala de dificuldade**.
  O oráculo de um caso clean-room deve avaliar somente a especificação própria do
  caso, nunca comparar o resultado com a geometria oficial.
- Se uma referência visual for indispensável, ela deve ser substituída por um
  desenho autoral anexado ao caso, com origem e licença explícitas.

## Fontes oficiais

### WorldSkills — Mechanical Engineering CAD

- [WorldSkills India — Sample Test Project ISC 2024](https://worldskillsindia.co.in/world_skills_test_project_isc_2024.php)
  publica a entrada de Mechanical Engineering CAD e o respectivo pacote de teste.
- [Pacote oficial MechanicalEngineering.zip](https://worldskillsindia.co.in/file/test-project-2024/MechanicalEngineering.zip)
  é a fonte do inventário local abaixo; o ZIP não é parte deste repositório.
- [WorldSkills — página da skill Mechanical Engineering CAD](https://worldskills.org/skills/id/217/)
  descreve o domínio profissional da modalidade.
- [WorldSkills Lyon 2024 — skill 05](https://worldskills.org/what/projects/wsos/2024/events/579/skills/1664)
  registra o projeto e o padrão de excelência da edição.
- [WorldSkills Occupational Standards — Mechanical Engineering CAD](https://api.worldskills.org/resources/download/6673/6764/7658?l=en)
  cobre modelagem de componentes, assemblies/subassemblies, funções móveis,
  animação, desenhos e reverse engineering.
- [WorldSkills — visão geral de Mechanical Engineering Design CAD](https://worldskills.org/media/news/mechanical-engineering-design-cad/)
  contextualiza componentes, sistemas mecânicos completos e reconstrução a
  partir de peças físicas.

### PTC Creo Community Challenge

- [Challenge 2 — Isogrid on a Curved Surface](https://community.ptc.com/3d-part-assembly-design-327/creo-parametric-community-challenge-2-isogrid-on-a-curved-surface-145937)
  é a referência oficial para padrões estruturais sobre superfícies cilíndricas,
  cônicas ou de radome, com dificuldades reais de topologia e regeneração.
- [Arquivo e regras dos PTC Creo Community Challenges](https://community.ptc.com/t5/Creo-Parametric-Tips/PTC-Creo-Community-Challenge-Announcement-and-Rules/ta-p/889282)
  fornece o contexto da série de desafios.

### Dassault Systèmes Model Mania

- [Dassault Systèmes — What is Model Mania?](https://3dswym.3dexperience.3ds.com/post/3dexperience-edu-students/what-is-model-mania_W7n8WPXUSrCJEVV4Tjn1nw)
  formaliza o formato em duas fases: construir com rapidez e precisão a partir de
  um desenho 2D e, em seguida, aplicar uma alteração de engenharia (ECO). Esse
  formato é a base recomendada para avaliar robustez paramétrica, não apenas a
  aparência do primeiro resultado.

## Inventário local auditado do pacote WorldSkills

Snapshot auditado em 2026-07-14, fora do repositório:

- ZIP: `WorldSkills_MechanicalEngineering.zip`
- Tamanho: 893.351.906 bytes
- SHA-256: `DEB40B44C904D4CA596728471B4AB9B7C1A05DFFC7596B41BBF9AC95F15E2589`
- Extração: 1.774 arquivos, 1.082.105.611 bytes

| Módulo | Arquivos | Assemblies `.iam` | Parts `.ipt` | PDFs | Evidência de escala observada |
| --- | ---: | ---: | ---: | ---: | --- |
| `WSC2024_TP05_M1` | 341 | 54 | 245 | 18 | Famílias TIOHRA e MRWG-6M, com given files, solução, impressão e marking separados. |
| `WSC2024_TP05_M2` | 793 | 163 | 601 | 9 | Grandes árvores HEABBM; um diretório de solução contém 298 arquivos CAD/PDF relevantes. |
| `WSC2024_TP05_M3` | 619 | 82 | 506 | 10 | Projetos aninhados; a família D25DC contém centenas de parts e assemblies. |
| `WSC2024_TP05_M4` | 19 | 1 | 4 | 10 | Conjunto compacto de gearbox cover, componentes, desenhos e avaliação. |

O inventário demonstra dois extremos que o benchmark deve cobrir: uma peça
compacta cuja geometria e documentação são avaliadas profundamente, e árvores de
montagem com centenas de arquivos. Contagem bruta, porém, não é um objetivo: um
assembly cheio de blocos sem relações mecânicas não deve obter pontuação alta.

## Matriz de desafios clean-room propostos

Escala sugerida: `E4` é difícil para um usuário experiente; `E5` é nível expert;
`E6` combina múltiplos domínios e exige planejamento de um engenheiro mecânico.
Os tempos são limites de benchmark propostos, não tempos oficiais das fontes.

| ID | Desafio autoral | Classe | Nível / tempo | Dificuldades que devem ser obrigatórias | ECO obrigatório | Oracle independente mínimo | Cobertura atual |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C01 | Radome estrutural com lattice sobre casca esférica | Peça/superfície | E5 / 45–75 min | Casca de espessura constante, meridianos e anéis unidos, padrões sem self-intersection, flange e furação associativos | Alterar raio, altura, contagem de meridianos e bolt circle sem reconstruir features | Uma única massa sólida, bbox, espessura amostrada, cardinalidade dos padrões, saúde das features e zero regressões | **B05** |
| C02 | Impeller fechado de bomba centrífuga | Peça/freeform | E6 / 90–150 min | Hub e shroud, pás com twist por loft/sweep, periodicidade, folga na ponta, canais sem interpenetração e transições fabricáveis | Alterar número de pás, diâmetros e ângulos de entrada/saída preservando área de passagem | Volume/massa, contagem de pás, gap mínimo, continuidade, seção de canais e ausência de interferência | Futuro |
| C03 | Carcaça fundida de redutor com tampa usinada | Peça multi-body | E6 / 120–180 min | Shell, draft, nervuras, bosses coaxiais, alojamentos de rolamento, split line, vedação, fixadores e acessos de ferramenta | Mudar distância entre eixos e dois rolamentos; ribs, tampa e furação devem acompanhar | Espessura, draft, coaxialidade, planos de junta, clearances, massa e validade dos corpos | Futuro; inspirado na classe compacta de M4 |
| C04 | Redutor planetário de dois estágios | Montagem/movimento | E6 / 150–240 min | Engrenagens paramétricas, carrier, splines simplificadas, bearings, fasteners, relações de movimento e fechamento axial | Alterar relação e torque nominal sem trocar identidades dos eixos e interfaces externas | BFS do grafo de joints, DOF, razão cinemática, backlash/clearance, colisão, BOM e envelope | Futuro |
| C05 | Robô industrial articulado com end effector | Montagem paramétrica | E5 / 60–120 min | Componentes distintos, cadeia cinemática completa, eixos ortogonais, wrist, gripper e envelopes de motores/cabos | Estender links e wrist preservando cadeia base-to-tool e tool center point | Componentes únicos, conectividade, revolute joints, DOF, bbox, tool-tip e interferências | **B06** |
| C06 | Célula automática de embalagem | Sistema multi-subsystem | E6 / 120–240 min | Estrutura fechada, enclosure, porta realmente articulada, conveyor apoiado, roletes tangentes, motor, hopper funcional e gabinete ancorado | Aumentar largura da máquina, belt e porta sem romper alimentação, suportes ou segurança | Grafo mecânico conexo, joints e endpoints, interferences críticas zero, clearances, tangência, envelope e restauração | **B07** |
| C07 | Mecanismo indexador por came com dwell | Peça + montagem | E6 / 120–180 min | Lei de movimento, came conjugado, seguidor, pré-carga, roletes, fase e singularidades evitadas | Alterar número de estações e fração de dwell mantendo aceleração máxima limitada | Curva deslocamento/velocidade/aceleração, contato, DOF, colisão e erro de indexação | Futuro |
| C08 | Enclosure industrial em sheet metal | Sheet metal + assembly | E5 / 90–150 min | Regras de chapa, bends, hems, louvers, door, hardware, flat patterns e nesting sem sobreposição | Alterar espessura e envelope; manter dimensões externas, bend reliefs e interfaces | Flat-pattern válido, raio/K-factor, colisões, contagem de bends, bbox e yield do nesting | Futuro |
| C09 | Reconstrução de steering knuckle fundido | Reverse engineering | E6 / 150–240 min | Dados incompletos, datum scheme explícito, bosses oblíquos, blends de curvatura, parting line e superfícies usinadas | Incorporar uma segunda rodada de medições contraditórias com tolerâncias | Desvio contra nuvem/medidas reservadas, GD&T, espessura, massa, curvatura e estabilidade do histórico | Futuro |
| C10 | Reconstrução de máquina a partir de BOM e peças dadas | Montagem de grande porte | E6 / 180–300 min | Centenas de ocorrências, subassemblies, hardware repetido, referências faltantes, propriedades e desenho geral | Substituir uma família de componentes e mudar envelope sem perder constraints | Identidade/BOM, conectividade, loops de constraints, colisões, missing refs, massa e desenho | Futuro; inspirado na escala de M2/M3 |
| C11 | Fixture modular de usinagem para família de peças | Assembly + configuração | E5 / 120–180 min | Datums 3-2-1, clamps, acessibilidade, colisão de ferramenta, configurações e peças intercambiáveis | Trocar a variante da peça e uma ferramenta mantendo localização e aperto | Seis graus restringidos, forças/clearances, reach da ferramenta, BOM e variantes | Futuro |
| C12 | Bracket aeroespacial com nervuras e otimização manual | Peça/engenharia | E5 / 90–150 min | Load paths explícitos, envelopes keep-out, bosses, ribs, pockets, fillets e fabricação em 3+2 eixos | Aumentar carga e deslocar interfaces com limite de massa | Massa, tensão de referência externa, espessura mínima, raios, keep-outs e setup de fabricação | Futuro |

## Mapeamento de B05–B07

### B05 — `b05_spherical_lattice_radome`

B05 é a adaptação clean-room mais direta da classe PTC isogrid-on-curved-surface.
Ele testa casca revolvida, cortes concêntricos, ribs meridionais, latitude rings,
padrões circulares, parâmetros trigonométricos derivados e regeneração pós-ECO.
O ganho sobre uma reprodução visual é claro: o resultado deve continuar sendo um
único sólido válido após mudar raio, flange e cardinalidades.

Lacunas para uma futura versão expert: amostrar espessura sobre a superfície,
medir interseções rib/ring, verificar continuidade local e executar dois ECOs em
ordens diferentes para detectar dependências acidentais.

### B06 — `b06_robot_arm_assembly`

B06 representa a classe WorldSkills de montagem mecânica funcional. A árvore tem
componentes individualizados, cadeia de as-built joints, revolute joints,
envelopes de motores, wrist, gripper e cabos. O ECO altera os principais links e
exige que o tool-tip e as identidades sobrevivam.

Lacunas para a próxima versão: medir DOF de fato, varrer o workspace, validar
colisão ao longo de poses, verificar limites de junta e provar continuidade dos
cabos sob movimento. Essas verificações evitam premiar uma montagem estaticamente
bonita, porém impossível de operar.

### B07 — `b07_packaging_machine`

B07 sobe para uma máquina completa: frame, enclosure, acesso, conveyor, roletes,
acionamento, hopper, feed path e gabinete. A alteração simultânea de máquina,
belt e porta é inspirada no formato ECO da Model Mania, aplicada a um assembly
multi-subsystem.

Para que B07 seja considerado competição-expert, o gate não pode se limitar a
contagens. Ele deve provar: estrutura e subsistemas conectados por um grafo
mecânico; porta em cadeia revoluta; conveyor fisicamente apoiado; interferences
críticas ausentes; hopper-to-belt contínuo; e invariantes preservados após ECO.
O arquivo de definição do caso é a fonte canônica para números e assertions, pois
esses detalhes podem evoluir durante a qualificação real no Fusion.

## Protocolo de avaliação recomendado

Todo desafio deve ter ao menos duas fases no espírito Model Mania:

1. **Build:** construir a partir de um brief autoral e de um desenho 2D incompleto
   o suficiente para exigir intenção de projeto, mas completo o suficiente para
   admitir um único resultado verificável.
2. **ECO:** alterar interfaces e parâmetros de alto impacto sem apagar/recriar o
   modelo inteiro nem substituir identidades de componentes.

Gates fail-closed recomendados:

- zero erro de regeneração e zero geometria inválida;
- uma única aplicação mutável por fase, sem retry de escrita;
- sketches críticos totalmente restringidos ou com graus de liberdade
  explicitamente permitidos;
- identidade persistente de features, bodies, componentes e interfaces;
- massa, bbox, espessura, contagens e dimensões avaliadas por código;
- assemblies com grafo conexo, DOF esperado e joints semanticamente corretos;
- zero interferência proibida e folgas mínimas verificadas numericamente;
- desenhos/BOM/flat patterns validados quando fizerem parte da categoria;
- ECO aprovado pelo mesmo tipo de oracle, incluindo invariantes `unchanged`;
- screenshot apenas como evidência secundária, nunca como prova de correção;
- documento descartável restaurado/fechado sem salvar ao fim de cada trial.

Métricas úteis para comparar agentes: sucesso inicial e final, tempo de
planejamento/build/ECO/verificação, chamadas de ferramenta, bytes de script,
reparos, features recriadas, referências quebradas, duplicações, outcome incerto
e diferença entre o resultado geométrico e o oracle. Para assemblies, adicionar
número de componentes realmente conectados, loops de constraint, DOF e volume de
interferência por par crítico.

## Ordem de expansão sugerida

1. Qualificar B05–B07 com os gates acima e registrar seus traces como baseline.
2. Implementar C03 (carcaça) para revelar falhas de shell/draft/rib e C07 (came)
   para revelar falhas de matemática e movimento.
3. Implementar C09 com medições autorais e uma nuvem reservada ao oracle; isso
   testa planejamento sob informação incompleta sem usar propriedade oficial.
4. Implementar C04 ou C10 somente depois de o harness medir conectividade, DOF,
   interferência e BOM; antes disso, a contagem de ocorrências seria um proxy
   fraco para qualidade mecânica.
5. Manter uma divisão balanceada entre peça, superfície, assembly, sistema, ECO e
   reverse engineering para evitar otimização do agente a uma única receita.
