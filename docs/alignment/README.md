# Alinhamento multiscanner de lâminas histológicas

Esta secção documenta dois fluxos de alinhamento usados no projeto, separando explicitamente os transform calculados em baixa resolução dos calculados diretamente em alta resolução.

## Estado da documentação

- **Método 1 (2× → 40×): documentado a partir do código versionado** neste repositório.
- **Método 2 (de novo em alta resolução): descrição de configuração fornecida pela responsável do projeto; ainda não auditada contra o repositório Kiémen**, que não está acessível através da ligação GitHub desta sessão (resposta 404). O SHA de referência indicado é `584bd1a5f27bf961daafd13397b3303403abdf68`.
- **Comparação quantitativa/visual: pendente.** Os PDFs e os diretórios CSV indicados estão num caminho Windows que não existe neste ambiente. Nenhum CSV, PDF, contagem amostral, métrica ou figura comparativa foi publicado como resultado nesta revisão.

Consultar [Métodos](methods.md), [Comparação e métricas](comparison.md), [Resultados e estado da auditoria](results/README.md) e [Proveniência e protocolo de auditoria](provenance.md).

## Estrutura

```text
alignment/
├── README.md
├── methods.md
├── comparison.md
├── provenance.md
├── results/
│   └── README.md
├── figures/          # reservado para figuras rastreáveis aos dados de origem
└── data/             # reservado para CSVs derivados; nunca imagens WSI brutas
```

## Limites de dados

As lâminas brutas, os OME-TIFFs integrais, os transforms MATLAB e os dados identificáveis permanecem fora do GitHub. Os resultados quantitativos publicados devem ser CSVs derivados e figuras agregadas, acompanhados de script de análise, hash/commit do código, descrição da amostra e ligação para o armazenamento privado. Não adicionar identificadores pessoais nem caminhos NAS com informação de conta.
