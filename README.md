# bqlineage

Table- and column-level lineage for BigQuery SQL, built on [sqlglot](https://github.com/tobymao/sqlglot)'s BigQuery parser.

Handles CTEs (incl. `WITH RECURSIVE`), nested subqueries, `UNION`, `UNNEST`/structs, temp tables (`CREATE TEMP TABLE`, traced through to base tables),
views (expanded via their SQL from BigQuery, or `CREATE VIEW` in the script), `INSERT`, `MERGE` (incl. `INSERT ROW`), `UPDATE ... FROM`.
Unqualified columns in joins are resolved against the real table schemas; ambiguous or unknown columns are reported as errors, never guessed.

## Install

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[bq]"
```

## CLI

```bash
bqlineage auth                                   # gcloud application-default login (or set GOOGLE_APPLICATION_CREDENTIALS)
bqlineage validate query.sql --bq --project my-proj
bqlineage lineage query.sql --bq --project my-proj --dataset sales --level column
bqlineage lineage query.sql --schema schema.json   # offline: {"proj.ds.table": {"col": "TYPE"}}
```

`--raw` keeps temp tables/views as intermediate hops; `--no-expand-views` treats views as sources.

## Python

```python
from bqlineage import analyze, BigQuerySchemaProvider
r = analyze(sql, BigQuerySchemaProvider("my-proj"), dataset="sales")
r.table_lineage()   # {"proj.dw.fact": ["proj.sales.customers", ...]}
r.column_lineage()  # {"proj.dw.fact.total": ["proj.sales.orders.amount"]}
r.issues            # syntax / unknown table / ambiguous column / column-count errors
```

## Tests (no BigQuery needed — the client is mocked)

```bash
.venv/Scripts/python -m pytest -q
```
