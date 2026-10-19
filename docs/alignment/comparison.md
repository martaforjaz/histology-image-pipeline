# Comparação entre os métodos

## Pergunta comparativa

A comparação mede se o **Método 2 (registo de novo em alta resolução)** produz correspondência local diferente do **Método 1 (transforms calculados a 2×, aplicados a 40×)** nos mesmos pares de lâminas/tecido e scanner. O Método 1 não é simplesmente outro export do Método 2: a resolução e os dados usados para estimar os transforms diferem.

## Unidade e elegibilidade

A unidade primária deve ser o par (tecido, scanner), com S210 como referência e um resultado presente e tecnicamente válido para ambos os métodos. Excluir pares sem um dos resultados, com dimensões/resolução incompatíveis, recorte sem sobreposição tecidular suficiente, saídas interrompidas ou QC falhado. Publicar os pares incluídos e excluídos e a regra de amostragem efetivamente implementada no script, antes de resumir métricas.

O número de tecidos, scanners, pares e tiles não pode ser confirmado nesta revisão: os arquivos `paired_metrics.csv`, scripts de análise, PDFs e diretórios correspondentes não estavam neste computador. Não usar totais de execução de workers como denominador da análise. Não extrapolar uma análise de subset para todo o cohort.

## Métricas: interpretação correta

| Métrica | Definição/escala a documentar no script de análise | Interpretação e limite |
|---|---|---|
| `m1_shift_um`, `m2_shift_um` | Deslocamento residual entre os métodos ou imagem móvel e referência, convertido para µm; confirmar no código como é calculado e se é mediana, média ou norma vetorial. | Quantifica erro de correspondência na amostra/local de teste; não é a deformação aplicada em si. Relatar estatística espacial e unidade. |
| `m1_ncc`, `m2_ncc` | Coeficiente de correlação normalizada (NCC) nos pixels/região comparáveis; confirmar normalização, máscara e recorte. | Concordância de intensidade/estrutura na região; pode ser afetada por coloração, scanner e interpolação, portanto não é precisão geométrica isolada. |
| `m1_dice`, `m2_dice` | Dice de máscaras/segmentações comparáveis; confirmar como as máscaras foram obtidas, classe/threshold e domínio do recorte. | Sobreposição global ou regional de máscaras; pode ocultar erros locais e depende da definição da máscara. |
| IoU | `intersection / union`, quando reportada. | Métrica de sobreposição de regiões; não substitui deslocamento local em µm. Dice e IoU estão relacionados, não são observações independentes. |
| tempo | Tempo observado em log, com limites de cronómetro e estado de conclusão definidos. | Comparar mesmo hardware, carga NAS, número de workers, fase medida e amostra. Runtime previsto/estimado tem de aparecer em coluna/série separada e nunca ser apresentado como observado. |

### Global versus local

NCC e Dice/IoU agregados sobre uma região podem indicar sobreposição geral elevada ainda que existam erros locais relevantes. A avaliação de precisão local deve apresentar resíduos espaciais em µm em pontos/patches correspondentes, distribuição por tile e mapas/crops representativos. Uma métrica de deslocamento resumida não informa onde ocorreu o erro; deve ser acompanhada de contagem de tiles válidos, regra de amostragem e mapas/figuras.

## Plano de resultados a publicar

1. Tabela pareada por tecido e scanner: métricas dos dois métodos, diferença `M2 − M1`, número de tiles/imagens válidos, resolução e runtime observado.
2. Resumo agregado por scanner e total: n de tecidos/pairs, mediana e intervalo interquartil (ou estatística realmente usada pelo script), distribuição das diferenças pareadas e tratamento de casos ausentes.
3. Figuras: distribuição de residual shift em µm; NCC e Dice/IoU pareados; mapa espacial/crops que mostrem casos com discrepância; sobreposições globais; gráfico de runtime observado separado de estimativa.
4. Casos problemáticos e QC: recorte/região, scanner, razão de exclusão ou falha, estado do NAS e fonte do valor.
5. Script reprodutível e proveniência: comando, dependências, commit de origem, hash dos CSVs de entrada e caminho privado dos OME-TIFFs/warps usados.

Nenhum resultado numérico ou visual está incluído nesta revisão. Os títulos de PDFs e nomes de colunas fornecidos pela utilizadora orientam a estrutura acima, mas a semântica exata das métricas precisa de ser verificada nos scripts que geraram os CSVs.
