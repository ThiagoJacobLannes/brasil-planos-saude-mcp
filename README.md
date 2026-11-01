# brasil-planos-saude-mcp

<!-- mcp-name: io.github.ThiagoJacobLannes/brasil-planos-saude-mcp -->

MCP server that helps answer **"is this health plan any good?"** in Brazil. Claude (or any MCP client) can look up a health-plan operator (operadora de plano de saúde) and see what the regulator, **ANS**, publishes about it: complaints, quality score, access-to-care monitoring, fines and readjustments. It can compare operators side by side and rank them.

**Read-only, no key, no account.** Everything comes from the [ANS open-data portal](https://dadosabertos.ans.gov.br/FTP/PDA/). Not affiliated with ANS or any operator.

> **Operators, not plans or prices.** ANS publishes these indicators per operator. A single operator sells many plans with different networks; check the network and the price with the operator or a broker.
>
> **Not legal advice.** For a denied or delayed procedure, use `where_to_complain` (Disque ANS **0800 701 9656**). In an emergency, get care first.

## Tools

### `operator_profile`
Everything about one operator: registration and status (active, or cancelled with date and reason), beneficiaries and size class, the complaints index with its position among operators of the same size, five years of IDSS, the access-to-care band, fines, and the small-group readjustment.

Parameters: `operator` (name or part of it, CNPJ, or ANS registry number), `coverage` (`medica` or `odonto`). A shared brand such as "Unimed" (hundreds of separate cooperatives) returns the largest matches and asks for a more specific name.

### `compare_operators`
Two to eight operators side by side: 12-month complaints index against the median of their size class, latest IDSS, latest access-to-care band, fines in the last five years, latest small-group readjustment.

Parameters: `operators` (list), `coverage`.

### `rank_operators`
Active operators ranked by complaints (`by="reclamacoes"`, lower is better) or quality (`by="qualidade"`, latest IDSS, higher is better).

Parameters: `by`, `coverage`, `porte` (`pequeno`, `medio`, `grande`), `uf`, `modality`, `min_beneficiaries` (default 5000), `worst_first`, `limit`.

### `find_operators`
Search by name, CNPJ or registry number, or list by head-office state, city or modality (cooperativa médica, medicina de grupo, seguradora, autogestão, odontologia de grupo, filantropia). Largest first.

### `where_to_complain`
The official ANS channels, checked on the [ANS consumer-service page](https://www.gov.br/ans/pt-br/canais_atendimento/canais-de-atendimento-ao-consumidor), and how a complaint (NIP) works.

## What the numbers mean

| Indicator | What it is | Better | Updated |
|---|---|---|---|
| **IGR** (Índice Geral de Reclamações) | Complaints to ANS per 100,000 beneficiaries in a month (2023 method). ANS compares operators of the same size: small < 20k, medium 20k-100k, large > 100k beneficiaries. The server also gives a 12-month average, which is steadier. | lower | monthly |
| **IDSS** (Índice de Desempenho da Saúde Suplementar) | ANS's yearly 0-1 score, with four dimensions: quality of care (IDQS), access (IDGA), market sustainability (IDSM), processes and regulation (IDGR). | higher | yearly |
| **Garantia de atendimento** | Quarterly band for denied or delayed coverage: 0 no upheld complaints, 1 below the median, 3 more than 50% above the median. Band 3 again and again leads ANS to suspend sales of plans. | lower | quarterly, when ANS publishes |
| **Penalidades** | Fines ANS applied, by year and status, including fines under appeal, suspended or archived. Not what was paid. | fewer, relative to size | when ANS publishes |
| **Reajuste do agrupamento** | The readjustment the operator applied to its pool of collective contracts with fewer than 30 lives. Individual/family plans follow a cap set by ANS instead. | | yearly |

Every result says which month, quarter or year each figure belongs to, and links the file it came from.

## Data and cache

The first call downloads seven ANS files (about 65 MB) and keeps a compact copy (about 1 MB) in your user cache folder. After 24 hours the server asks ANS whether a file changed and downloads it again only if it did. If ANS is down, it uses the cached copy and says so. To fill the cache in advance:

```bash
brasil-planos-saude-mcp --refresh
```

Set `BRASIL_PLANOS_SAUDE_CACHE_DIR` to use another folder.

The operator registry file also has the operator's contact e-mail and the name of its legal representative. This server leaves both out.

## Install

**Quickest** (needs [uv](https://docs.astral.sh/uv/)): no clone needed, it runs straight from PyPI.

```bash
claude mcp add brasil-planos-saude -- uvx brasil-planos-saude-mcp
```

For Claude Desktop, use `"command": "uvx", "args": ["brasil-planos-saude-mcp"]`.

**From source:**

Requires Python 3.10+.

```bash
git clone https://github.com/ThiagoJacobLannes/brasil-planos-saude-mcp
cd brasil-planos-saude-mcp
python -m pip install -e .
```

On Windows, if you keep code in a cloud-synced folder (Google Drive, OneDrive), create the virtual environment **outside** it. Sync clients drop `desktop.ini` files into folders, which can crash packages on startup.

## Add to Claude Code

```bash
claude mcp add brasil-planos-saude -- python -m brasil_planos_saude_mcp.server
```

## Add to Claude Desktop

Merge `claude_desktop_config.snippet.json` into your Claude Desktop `claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "brasil-planos-saude": {
      "command": "python",
      "args": ["-m", "brasil_planos_saude_mcp.server"]
    }
  }
}
```

## Example questions

- "Is Hapvida any good? How does it do on complaints?"
- "Compare Amil, Bradesco Saúde, SulAmérica and Porto Seguro."
- "Which large health plans get the fewest complaints?"
- "Best-rated dental plans."
- "Has Unimed Campinas been fined by ANS?"
- "My plan denied a procedure. Where do I complain?"

## License

MIT
