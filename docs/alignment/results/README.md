# Resultados da comparação de alinhamento — auditoria pendente

Esta pasta está reservada aos resultados derivados da comparação Método 1 vs Método 2. **Nenhum CSV ou PDF quantitativo foi copiado nesta versão**, porque os ficheiros indicados estão em `C:\Users\mperei13\Documents\ChatGPT\Thesis\...`, inacessível neste ambiente.

## Ficheiros esperados

- `paired_metrics.csv` original da pasta `eight_tissues_20261015`, especialmente as colunas `m1_shift_um`, `m2_shift_um`, `m1_ncc`, `m2_ncc`, `m1_dice`, `m2_dice`.
- Scripts usados para gerar o CSV, e quaisquer CSVs de amostragem/tiles/tempo associados.
- PDFs existentes (preservar os originais):
  - `comparasion_alignment_techniques_Hum_Colon_A_bilingual.pdf`
  - `comparasion_alignment_3_tissues_2026-10-14.pdf`
  - `comparasion_alignment_8_tissues_2026-10-15.pdf`
  - `comparasion_alignment_runtime_estimate_8_tissues_2026-10-15.pdf`
- Conteúdo exportado dos scripts e do commit `584bd1a5f27bf961daafd13397b3303403abdf68` do repositório Kiémen, se a ligação privada não puder ser autorizada.

## Sem inferências numéricas nesta revisão

Não foram publicados números de tecidos, scanners, pares, tiles, valores de shift/NCC/Dice, tempos observados ou previstos nem conclusões de igualdade/diferença. Uma pesquisa contextual encontrou referências a resultados históricos, mas eles não foram usados como fonte numérica porque não puderam ser confirmados nos arquivos primários solicitados. Quando os artefactos forem disponibilizados, recalcular agregados a partir dos CSVs e ligar cada afirmação ao script e aos pares incluídos.

## Critérios para fechar esta pasta

- preservar os arquivos recebidos e registar SHA-256;
- verificar que os pares do CSV correspondem a saídas válidas dos dois métodos;
- contar tecidos, scanners, pares e tiles diretamente pelos dados filtrados;
- reproduzir as métricas com o script-fonte e esclarecer unidades, máscaras, recortes e amostragem;
- separar runtime medido de projeções de runtime;
- exportar tabelas e figuras legíveis com legenda, n e fonte;
- adicionar um registo de QC/limitações e atualizar os links nesta pasta e no README principal.
