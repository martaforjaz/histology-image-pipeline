# Proveniência, versões e auditoria

## Fontes verificadas no repositório

A auditoria desta documentação foi feita sobre `martaforjaz/histology-image-pipeline` no commit `9698003e3fd9b3f50fac463bab504c378a356986` (HEAD observado em 19 de outubro de 2026). Fontes principais: `01_conversion_downsampling/`, `02_calculate_registration/`, `03_apply_registration/`, `docs/USER_GUIDE.md`, `docs/VALIDATION.md`, `docs/matlab_sources.json`, `requirements/` e `environment.yml`.

O código identifica Python 3.11 como interpretador testado; a integração MATLAB documentada em `docs/VALIDATION.md` usou MATLAB R2024a com Image Processing Toolbox. Esses valores caracterizam testes do repositório, não necessariamente cada execução de análise comparativa. A configuração Python/PyCODA e a revisão do Método 2 continuam por confirmar.

O CODA MATLAB é uma dependência externa, obtida do projeto dos autores; o repositório contém um manifesto de fontes/hash, wrapper e instruções para instalação local, mas não redistribui os fontes MATLAB. Consultar `THIRD_PARTY_NOTICES.md`, `docs/USER_GUIDE.md` e `tools/setup_coda.py`.

## Repositório do Método 2 e chats de projeto

A consulta GitHub a `kiemen/register_high_resolution` e ao README no commit solicitado respondeu 404. Portanto, não foi possível confirmar visibilidade, arquivos, autoria/estado da revisão, defaults nem implementação. Os parâmetros enumerados em `methods.md` foram comunicados pela responsável do projeto e continuam rotulados como não auditados.

Os chats nomeados “1.2) Comparasion between allignments” e “1.2) Allign High resolution” não foram encontrados por esses títulos na pesquisa contextual disponível. A memória de projeto fornece pistas históricas, mas não foi tratada como fonte primária dos resultados quantitativos.

## Procedimento de auditoria dos dados ainda necessários

1. Disponibilizar CSVs/PDFs e scripts através de anexos nesta conversa ou de acesso autorizado aos diretórios locais. Para o repo privado, conceder acesso GitHub ou fornecer ZIP/export do commit.
2. Copiar os arquivos fornecidos para `docs/alignment/data/` (CSV derivado e metadados de execução; não incluir WSI, OME-TIFF integral, MAT de warp ou dados clínicos). Preservar os PDFs recebidos sem alteração em `docs/alignment/results/` ou `docs/alignment/figures/`, conforme o conteúdo.
3. Gerar SHA-256 dos arquivos originais; guardar junto de um `manifest.csv` com nome, tamanho, hash, fonte, commit, data de execução (se disponível), hardware, método, filtro e papel analítico.
4. Ler scripts de análise para determinar unidade amostral, filtros, máscaras, definição de shift e NCC, amostragem de tiles, temporização observada/estimada e geração das figuras.
5. Confrontar cada linha com tecido/scanner e estado do output. Auditar completude por manifestos e logs recentes; não inferir sucesso apenas da existência de `registered_high_resolution_denovo_750_2500`.
6. Não fazer cópia de imagens brutas ou resultados gigantes para GitHub. Manter no NAS privado, registrar o caminho lógico sem credenciais e executar uma auditoria de arquivos/outputs por tecido e scanner.
7. Publicar CSV agregado/derivado e figuras com n explícito, sem PHI e sem IDs de espécimes identificáveis; revisar a política do dataset antes de incluir metadados linha a linha.

## Limitações de validade

- O repositório principal prova configuração e testes do pipeline, não qualidade anatómica de resultados em todos os tecidos/scanners.
- A via tiled do Método 1 tem validação sintética e algumas verificações técnicas; QC visual em imagens reais continua necessário.
- Não há, neste momento, amostra pareada auditada nem resultado quantitativo reproduzido no checkout.
- Contagens históricas de workers, pastas existentes e runtime estimado não são usados como resultados finais.
