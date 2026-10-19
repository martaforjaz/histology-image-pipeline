# Métodos de alinhamento

## Definições

- **Método 1 — transforms calculados a 2× e aplicados a 40×:** o registo global e elástico é calculado sobre imagens RGB a aproximadamente 5 µm/píxel; os transforms resultantes são depois escalados e aplicados às imagens de 40× (aproximadamente 0,25 µm/píxel).
- **Método 2 — registo de novo em alta resolução:** os passos de correção e alinhamento elástico são calculados usando diretamente a representação de alta resolução, em vez de transferir os transforms do Método 1.

Os dois métodos têm de ser comparados em pares correspondentes do mesmo tecido e scanner, com referência, recorte, região tecidular válida e resolução comparáveis.

## Método 1: pipeline 2× → 40×

### Conversão e calibração

O código de `01_conversion_downsampling/` converte formatos de scanner para imagens RGB com resolução física explícita. A convenção do projeto é 2× = 5 µm/píxel e 40× = 0,25 µm/píxel. A conversão usa leitores específicos para VSI e CZI e um conversor partilhado para os outros formatos. As funções e opções estão em `run_conversion.py`, `streaming_conversion.py`, `image_metadata.py` e nos leitores correspondentes. O conversor pode produzir TIFF simples para entrada MATLAB de baixa resolução e OME-TIFF para as saídas de alta resolução.

A calibração é baseada em µm/píxel, não no nome da pasta. No fluxo atual, `scanner_calibration.py` corrige o MPP P1000 com o fator `1.0133191074458732`, estimado em 7 de outubro de 2026 a partir de 31 registos robustos de cinco lâminas físicas contra sete scanners. Essa correção altera a escala física atribuída à imagem, não as cores dos pixels. É uma calibração documentada no repositório e deve ser verificada quando se interpretam dimensões e escalas.

### Cálculo dos transforms

`02_calculate_registration/run_registration.m` valida os TIFFs, recolhe metadados de tempo e chama CODA MATLAB externo através de `timed_coda`. CODA calcula o alinhamento global e o elástico e escreve resultados sob `2x/registered/elastic registration/`, incluindo os transforms em `save_warps`. O wrapper acrescenta a matriz numérica `tform_python` às MAT para que a aplicação Python dos novos resultados não necessite do MATLAB Engine.

A chamada em lote `run_registration_batch.m` requer exatamente oito TIFFs RGB, um por scanner (S210, S360, Leica, Olympus, P1000, Pramana, Roche e Zeiss), cada um calibrado a 5 µm/píxel. Ela escolhe explicitamente o ficheiro `_S210.tif` como referência, usa H&E (`IHC=0`), máscara automática (`mask_style=[]`) e passa uma semente fixa `rng(20261008,'twister')`. O método de alinhamento CODA é uma dependência externa; os respetivos ficheiros MATLAB não são redistribuídos neste repositório. Ver `docs/matlab_sources.json`, `tools/setup_coda.py` e `docs/USER_GUIDE.md` para a proveniência e seleção do snapshot externo.

A lógica CODA de transformação global/elastic deve ser atribuída ao código externo. Este repositório integra, instrumenta e testa a chamada, a exportação numérica e a aplicação em outras resoluções; não reivindica autoria do algoritmo CODA.

### Transferência dos transforms para 40×

`03_apply_registration/apply_registration_to_20x_image.py` aplica a transformação affine e o campo de deslocamento elástico. A razão física é calculada como:

```text
escala = MPP_da_imagem_de_registo / MPP_da_imagem_de_entrada
```

Para 5 e 0,25 µm/píxel, a escala nominal é 20. O código usa MPP lido dos metadados e rejeita pixels não quadrados. O warp elástico é interpolado para a grelha de destino e o seu deslocamento multiplicado pela escala; affine, orientação/flip, padding e região fora da imagem são tratados na aplicação. Assim, 40× não significa que novos parâmetros de registo tenham sido estimados em 40×: significa que o campo calculado a 2× foi reescalado e aplicado ao ficheiro de 40×.

A aplicação tradicional exporta OME-TIFF piramidal, tiles de 1024 px, JPEG qualidade 95 e níveis de downsampling `[1, 2, 4, 8, 16]`, mais thumbnail, conforme `save_ome_tif`. Também cria `validation_overlay` para inspeção visual. Verificar sempre o código da revisão usada; existe também uma via tiled experimental mais recente com propriedades de exportação diferentes.

### Via tiled experimental do Método 1

`03_apply_registration/apply_40x_tiled.py` lê regiões com OpenSlide e escreve OME-TIFF tiled JPEG em blocos, sem alocar a lâmina nem o campo elástico de tamanho completo na RAM. A revisão atual do script escreve **um único nível de resolução**, não uma pirâmide. O README do manifesto descreve verificações técnicas e QC visual pendente. Testes de paridade com o aplicador de imagem completa usam dados sintéticos; isto não prova equivalência científica para lâminas reais.

### Fundamentação e limitações

Calcular o alinhamento a 2× reduz o custo computacional e facilita processar lâminas inteiras. Aplicar transforms via MPP evita assumir que o rótulo de magnificação determina exatamente a escala. Porém, informação espacial mais fina que não está representada a 5 µm/píxel não pode orientar o ajuste; deformações locais, microestrutura, artefactos de aquisição ou correspondência anatómica localizada podem não ser capturados adequadamente. A interpolação do campo e diferenças de amostragem/codec também podem afetar a aplicação. Portanto, o resultado a 40× não equivale a um alinhamento calculado diretamente nessa resolução; inspeção de overlays e métricas locais são necessárias.

### Ficheiros de suporte no repositório

| Parte | Ficheiros principais |
|---|---|
| Conversão, formatos, MPP | `01_conversion_downsampling/run_conversion.py`, `streaming_conversion.py`, `image_metadata.py`, `scanner_calibration.py`, leitores `WSI2OMEtif_All_file_types.py`, `CZI2OMEtif.py`, `vsi2ometif.py` |
| Cálculo CODA e lote | `02_calculate_registration/run_registration.m`, `run_registration_batch.m`, `run_worker.m`, `PipelineTiming.m`, `prepare_timed_coda.m` |
| Aplicação a alta resolução | `03_apply_registration/apply_registration_to_20x_image.py`, `apply_40x_tiled.py`, `prepare_40x_manifest.py`, `verify_registered_ome.py` |
| Configuração/proveniência | `docs/USER_GUIDE.md`, `docs/SELECTION.md`, `docs/matlab_sources.json`, `requirements/`, `environment.yml` |
| Limites e verificação | `03_apply_registration/README_40X_MANIFEST.md`, `docs/VALIDATION.md`, `tests/` |

**Revisão auditada do repositório principal:** `9698003e3fd9b3f50fac463bab504c378a356986` (HEAD consultado em 19-10-2026). As versões de ambiente, incluindo Python/MATLAB, devem ser tomadas dos arquivos de ambiente e dos registos de execução associados à análise específica, não presumidas a partir do método.

## Método 2: registo de novo em alta resolução

### Descrição e configuração comunicadas para este projeto

A configuração comunicada pela responsável do projeto é: `tile_size=750`, `tile_spacing=2500`, `n_iterations=2`, `levels=(4,1)`, `reference_contains='S210'`, `n_workers=4`, `max_memory_gb=16`, `staging_folder=False`. Os restantes parâmetros foram indicados como defaults do commit `584bd1a5f27bf961daafd13397b3303403abdf68` do repositório privado `kiemen/register_high_resolution`.

A descrição de trabalho é: registo global por PyCODA; correção affine; depois registo elástico diretamente em alta resolução, com amostragem por tiles e níveis; exportação de OME-TIFF piramidal. O fluxo reserva a S210 como referência, considera tiles de 750 px separados por 2500 px, passa por níveis 4 e 1 e faz duas iterações. Uma região/recorte final deve resultar do canvas e da área tecidular alinhada, mas a regra exata de seleção do recorte precisa de ser confirmada no código do commit indicado.

O modo de baixa memória informado escreve a imagem em strips diretamente no NAS. Isso muda a arquitetura de leitura/exportação e o perfil de memória; não constitui, por si só, evidência de que o alinhamento científico é equivalente ao do modo convencional.

### Proveniência ainda não confirmada

A consulta ao repositório Kiémen devolveu 404 neste ambiente; não foi possível ler o código do commit nem verificar os defaults, as versões de PyCODA/Python, os parâmetros restantes, o algoritmo da correção affine, a construção do campo elástico, a definição exata de levels/iterations, a seleção do recorte ou a pirâmide exportada. Os itens acima são, por isso, configuração fornecida pela responsável do projeto, e não afirmações auditadas contra o código-fonte. Para fechar esta secção é necessário acesso autorizado ao repo/commit ou um ZIP/export do commit e dos scripts usados.

## Estado operacional, não resultado científico

Os resultados deste método foram apontados para `<tecido>/40x/registered_high_resolution_denovo_750_2500` no NAS. A localização e configuração não demonstram que cada tecido terminou nem que passou QC. Foram reportadas interrupções por falhas NAS/rede e falta de memória quando vários registos correram em paralelo. Logo, a existência da pasta, isoladamente, não prova conclusão. São necessários manifestos/logs de execução, verificação de todos os oito outputs esperados por tecido e auditoria das dimensões/metadata/overlays.

Dez tecidos foram reservados inicialmente para um computador: Hum Colon A, Hum Kid RCC B, Hum Lung D, Hum Skin Mel A, Mac Brain A, Mac Fetus A, Mouse Fetus A, Mouse Gyn D, Mouse Lung KPC B e Mouse Panc KPC I. Outro worker foi descrito como percorrendo os restantes tecidos por ordem Z→A. Esta atribuição de trabalho, fornecida pela responsável, não é uma contagem de execução final; qualquer número de tecidos concluídos deve ser reconstruído de logs/manifestos recentes e outputs validados.
