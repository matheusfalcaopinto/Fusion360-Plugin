# Suite de benchmarks paramétricos do Fusion

Esta pasta contém seis fixtures de complexidade progressiva para avaliar construção paramétrica, uso da API do Autodesk Fusion, integridade geométrica e verificação independente. B02–B04 são casos de fase única; B05–B07 acrescentam uma construção inicial e uma ordem de mudança de engenharia (ECO) no mesmo documento descartável. Ela valida o caminho técnico do Fusion Agent; **não é um benchmark A/B entre Claude e Codex**.

## Estado das referências

Execução canônica completa B02–B07: `ref_20260714T055207Z`, concluída em `2026-07-14T05:52:57Z`.

| Caso | Artefato | Tempo | Fast Path | Oracle | Status |
|---|---|---:|---|---:|---|
| B02 | `b02_vented_enclosure` | 4.536 ms | `applied_verified` | 13/13 | `completed_pass` |
| B03 | `b03_split_pillow_block` | 4.215 ms | `applied_verified` | 15/15 | `completed_pass` |
| B04 | `b04_offset_duct_adapter` | 4.663 ms | `applied_verified` | 17/17 | `completed_pass`¹ |
| B05 initial | `b05_spherical_lattice_radome` | 8.024 ms total | `applied_verified` | 11/11 | `completed_pass` |
| B05 ECO | `b05_spherical_lattice_radome` | — | `applied_verified` | 8/8 | `completed_pass` |
| B06 initial | `b06_robot_arm_assembly` | 9.127 ms total | `applied_verified` | 8/8 | `completed_pass` |
| B06 ECO | `b06_robot_arm_assembly` | — | `applied_verified` | 7/7 | `completed_pass` |
| B07 initial | `b07_packaging_machine` | 17.869 ms total | `applied_verified` | 13/13 | `completed_pass` |
| B07 ECO | `b07_packaging_machine` | — | `applied_verified` | 10/10 | `completed_pass` |

Todas as fases transmitiram sua mutação uma única vez e preservaram a identidade do documento entre initial e ECO. Cada fase produziu PNGs nas direções isométrica, frontal, superior e direita; os arquivos direcionais agora são preservados separadamente, e um conjunto que colapse para o mesmo hash em todas as vistas falha no gate. O documento marcado foi fechado sem salvar. `restored=true`, o documento ativo original foi restaurado e os oito IDs do inventário final são idênticos, na mesma ordem, aos oito IDs originais.

¹ B04 passou integralmente no oracle geométrico, mas conserva uma limitação de acabamento: quatro sketches de loft e o eixo offset permaneceram visíveis. Isso está documentado abaixo e não é ocultado pelo status de aprovação geométrica.

`run_reference_suite.py` usa B02–B07 em `DEFAULT_CASES`. A aprovação continua dependendo do resultado agregado e dos gates por caso; a mera presença de um diretório ou `reference_result.json` não equivale a aprovação.

## Casos

### B02 — Enclosure ventilado

Exercita topologia oca, espessuras de parede e piso, bosses conectados, semântica de comprimento total de slots, padrão retangular 5×3 e mirror entre paredes. O oracle comprovou um único sólido/lump, cavidade aberta, quatro bosses ocos e trinta slots com grade e dimensões corretas.

![B02 — referência isométrica](cases/b02_vented_enclosure/images/reference_isometric.png)

### B03 — Mancal bipartido

Exercita dois componentes/occurrences, gap de montagem, bore coaxial entre base e tampa, furos de aperto alinhados, counterbores exclusivos da tampa e furos de montagem exclusivos do corpo inferior. O oracle comprovou todos os 15 requisitos.

Durante o endurecimento da fixture, dimensionar centros independentemente em cada quadrante causou `VCS_SKETCH_OVER_CONSTRAINTS`. A solução robusta foi construir um seed e gerar as demais instâncias por padrões retangulares. O frame dos perfis no plano YZ também foi corrigido com conversão explícita entre model space e sketch space.

Também foi observado que `Occurrence.name` é read-only nesta instalação. Os nomes finais das ocorrências derivam corretamente dos componentes, sem tentar atribuir diretamente essa propriedade.

![B03 — referência isométrica](cases/b03_split_pillow_block/images/reference_isometric.png)

### B04 — Adaptador de duto com offset

Exercita lofts externo/interno em frames deslocados, passagem oca contínua, tool bodies, eixo offset real, padrão inferior 2×2 e padrão circular superior de seis furos. O oracle comprovou 17/17 checks, incluindo um body/lump, passagem aberta, bbox, coaxialidade, offset e cardinalidades.

Nesta versão da API, `ConstructionAxisInput` oferece `setByCircularFace`; a tentativa com `setByCylinderOrConeFace` falhou por método inexistente e foi substituída antes da execução final.

Limitação de acabamento observada no resultado aprovado:

- sketches visíveis: `SK02_Outer_Lower_86x56`, `SK03_Outer_Outlet_OD60`, `SK05_Inner_Inlet_80x50` e `SK06_Inner_Outlet_ID54`;
- eixo visível: `CA01_Offset_Outlet_Axis`.

Esses itens permaneceram visíveis porque o Fast Path bloqueia mutações explícitas de visibilidade. A geometria, a saúde das features e a passagem foram aprovadas; o acabamento visual não foi promovido silenciosamente a perfeito.

![B04 — referência isométrica](cases/b04_offset_duct_adapter/images/reference_isometric.png)

### B05 — Radome esférico com lattice

Inspirado de forma clean-room no desafio de isogrid em superfície curva da PTC, B05 combina casca esférica oca, flange anular, doze ribs meridionais, cinco anéis de latitude e doze furos em bolt circle. O modelo inicial usa 28 parâmetros, um único body/lump, 13 features e dez sketches.

A ECO é aplicada no mesmo documento: `DomeRadius` passa de 90 para 105 mm, `BaseFlangeOD` de 200 para 230 mm, o bolt circle de 184 para 210 mm e as cardinalidades de bolts/meridians de 12 para 16. O run canônico `ref_20260714T055207Z` aprovou 11/11 checks initial e 8/8 checks ECO, com quatro PNGs direcionais por fase, close sem salvar e restauração integral.

| Initial | ECO |
|---|---|
| ![B05 initial](cases/b05_spherical_lattice_radome/images/reference_initial_isometric.png) | ![B05 ECO](cases/b05_spherical_lattice_radome/images/reference_eco_isometric.png) |

### B06 — Montagem paramétrica de braço robótico

B06 eleva o teste para 16 componentes/occurrences, 16 sólidos, 36 parâmetros, doze joints as-built — quatro revolute —, cadeia base→garra e chicote em três trechos. A ECO altera `UpperArmLength` de 160 para 195 mm, `ForearmLength` de 130 para 155 mm e `WristLength` de 70 para 85 mm; a ponta nominal deveria migrar de `(400, 290)` para `(450, 315)` mm no plano XZ sem perder a continuidade da cadeia.

No run canônico `ref_20260714T055207Z`, initial e ECO retornaram `applied_verified`; o oracle initial passou 8/8 e o oracle ECO passou 7/7. O documento marcado foi fechado sem salvar, o documento ativo foi restaurado e o inventário de oito documentos voltou exatamente ao baseline. B06 está aprovado como trial composto completo, não apenas como geometria isolada.

| Initial | ECO |
|---|---|
| ![B06 initial](cases/b06_robot_arm_assembly/images/reference_initial_isometric.png) | ![B06 ECO](cases/b06_robot_arm_assembly/images/reference_eco_isometric.png) |

### B07 — Máquina modular de embalagem

B07 é o caso de maior dificuldade da suite. A referência aprovada contém 59 parâmetros, 34 componentes/occurrences com transform identidade, 34 sólidos, 34 features, 35 sketches de componente mais o sketch root `SK00_Machine_Envelope`, e uma árvore de 33 joints as-built — cinco revolute. A máquina reúne frame fechado, painéis e porta articulada, conveyor, quatro rollers, motor, hopper/throat, suportes e cabinet. O envelope global initial aprovado é `600 × 620 × 500 mm`, de `(-300, -310, 0)` a `(300, 310, 500)` mm.

A ECO altera `MachineWidth` de 600 para 760 mm, `BeltWidth` de 300 para 400 mm e `DoorWidth` de 360 para 460 mm, propagando frame, painéis, porta, belt, rollers, motor e hopper; o envelope chega a X=±380 mm. No run `ref_20260714T055207Z`, initial e ECO retornaram `applied_verified`, com 13/13 e 10/10 checks independentes, oito PNGs direcionais e teardown integral.

Limitação deliberada: o motor é coaxial ao roller de saída e preserva um gap axial de 20 mm para a interface, mas o acoplamento físico eixo/rolamento não foi modelado. O pass comprova a interface e o envelope declarados, não um conjunto de eixo, rolamento e acoplamento pronto para fabricação. Flat patterns, cut list e BOM também permanecem fora do escopo.

| Initial | ECO |
|---|---|
| ![B07 initial](cases/b07_packaging_machine/images/reference_initial_isometric.png) | ![B07 ECO](cases/b07_packaging_machine/images/reference_eco_isometric.png) |

## Achado operacional do executor persistente

O executor nativo do Fusion acumula wrappers de `stdout`/`stderr` por processo e pode entrar em `RecursionError` depois de muitas chamadas. A causa, o reset validado, a mitigação proposta e os testes soak estão registrados em [`EXECUTOR_RELIABILITY.md`](EXECUTOR_RELIABILITY.md). A execução canônica completa passou, mas isso **não** significa que o defeito foi corrigido upstream pela Autodesk. Nenhuma mutação canônica foi repetida: B02–B04 registram uma única fase mutável; nos casos compostos, initial e ECO usam operation IDs distintos e cada fase é transmitida uma única vez.

## Protocolo seguro

Cada trial real seguiu:

1. registrar documento ativo e inventário de documentos abertos;
2. criar documento novo, não salvo, com marker e fingerprint exclusivos;
3. confirmar baseline vazio;
4. transmitir a construção initial exatamente uma vez, sem retry de mutação;
5. executar oracle canônico initial, sem confiar no resumo do executor;
6. nos casos compostos, aplicar a ECO uma vez no mesmo documento e executar o oracle ECO;
7. capturar e preservar vistas isométrica, frontal, superior e direita em PNG para cada fase; direção ausente ou quatro hashes idênticos bloqueiam o gate;
8. fechar somente o documento marcado, sem salvar ou sincronizar;
9. restaurar documento e inventário originais.

`applied_verified` isolado não é aprovação. O caso exige também `oracle.passed=true`, 100% de cobertura obrigatória e teardown comprovado.

## Artefatos

- [`suite_definition.json`](suite_definition.json): composição, política e resultados indexados.
- [`reference_suite_result.json`](reference_suite_result.json): resultado agregado canônico de B02–B07.
- [`REPORT.md`](REPORT.md): relatório técnico e limitações.
- [`COMPETITION_SOURCES.md`](COMPETITION_SOURCES.md): fontes públicas, critérios clean-room e posicionamento de dificuldade.
- [`EXECUTOR_RELIABILITY.md`](EXECUTOR_RELIABILITY.md): confiabilidade do executor Python nativo e limites conhecidos.
- [`run_reference_suite.py`](run_reference_suite.py): runner da referência.
- `cases/<case_id>/definition.json`: contrato congelado.
- `cases/<case_id>/eco_script.py` e `eco_oracle_script.py`: mudança e verificação independente dos casos compostos.
- `cases/<case_id>/reference_result.json`: Fast Path, oracles, imagens e cleanup; a existência do arquivo não implica aprovação.
