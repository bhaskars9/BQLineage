# bqlineage

Table- and column-level lineage for BigQuery SQL, built on [sqlglot](https://github.com/tobymao/sqlglot)'s BigQuery parser.

Handles single queries and whole scripts (statements separated by `;`, `--` / `#` / `/* */` comments):

- **Queries:** CTEs (incl. `WITH RECURSIVE`), nested subqueries, `UNION`, `UNNEST` / structs / implicit `FROM t, t.arr`, `PIVOT`/`UNPIVOT`, `QUALIFY`, `SELECT * EXCEPT/REPLACE`, wildcard tables (`events_*`, `_TABLE_SUFFIX`), pseudo-columns (`_PARTITIONTIME`).
- **Statements:** `CREATE [TEMP] TABLE … AS`, `CLONE`/`COPY`/`LIKE`, `CREATE [MATERIALIZED] VIEW`, `INSERT`, `MERGE` (incl. `INSERT ROW`), `UPDATE … FROM`, `EXPORT DATA`.
- **Temp tables and views:** traced through to base tables. A re-created temp table is versioned (`_SESSION.t`, `_SESSION.t#2`), so each reader keeps the right lineage.
- **Scripting:** `DECLARE`/`SET` variables, `SET @@dataset_id`, and the SQL inside `BEGIN…END`, `IF/ELSEIF/ELSE`, `LOOP`, `WHILE`, `REPEAT`, `FOR … IN`, `CASE`, `EXCEPTION` blocks. Every branch is analyzed, like straight-line code.
- **Columns in joins without aliases** are matched against the real table schemas. Ambiguous or unknown columns are reported, never guessed.

**Errors vs warnings:**
- **Errors** are definite problems: syntax errors, unknown tables, ambiguous columns, columns missing from known schemas, column-count mismatches.
- **Warnings** mean the tool couldn't know. For example, a table's schema was unavailable, or the statement uses `EXECUTE IMMEDIATE` or `CALL`.
- One bad statement doesn't stop the rest of the script from being analyzed.

## Install

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[bq]"
```

(On Mac or Linux, use `.venv/bin/python` instead of `.venv\Scripts\python`.)

## CLI

```bash
bqlineage auth                                    # gcloud application-default login (or set GOOGLE_APPLICATION_CREDENTIALS)
bqlineage validate script.sql --bq --project my-proj
bqlineage lineage script.sql --bq --project my-proj --dataset sales
bqlineage lineage script.sql --bq --project my-proj --table dw.fact                      # one target table
bqlineage lineage script.sql --bq --project my-proj --table dw.fact --columns total,name  # ...and only these columns
bqlineage lineage script.sql --schema schema.json   # offline: {"proj.ds.table": {"col": "TYPE"}}; keys may omit the project
```

- **Without `--bq` or `--schema`, no table schemas are known.** Unqualified columns in joins then can't be resolved, and you get warnings.
- `--table` accepts `project.dataset.table`, `dataset.table` or just `table`.
- `--columns` works with or without `--table`, and matches case-insensitively.
- `--level table|column|both`.
- `--raw` keeps temp tables and views as intermediate hops.
- `--no-expand-views` treats views as sources.

## Reading the column output

Each target column shows where its values come from, what decides its rows, and how it was built, step by step:

```json
"p.dw.fact.total": {
  "sources": ["p.sales.orders.amount"],
  "indirect_sources": ["p.sales.customers.customer_id", "p.sales.customers.region", "p.sales.orders.cust_id", "p.sales.orders.status"],
  "steps": [
    {"column": "p.dw.fact.total", "expression": "SUM(big.gross)", "from": ["big", "p.sales.customers AS c"],
     "joins": ["JOIN c ON big.cust_id = c.customer_id"], "where": "c.region = 'EU'", "group_by": ["c.region"]},
    {"column": "big.gross", "in": "p.dw.fact", "expression": "paid.gross", "from": ["_SESSION.paid"], "where": "paid.gross > 100"},
    {"column": "_SESSION.paid.gross", "expression": "o.amount * 1.2", "from": ["p.sales.orders AS o"], "where": "o.status = 'PAID'"}
  ]
}
```

- **`sources`** are the columns whose values flow into this column.
- **`indirect_sources`** are the columns read by the JOIN / WHERE / GROUP BY / HAVING / QUALIFY conditions along the way, including subqueries such as `NOT IN (SELECT …)`. These decide which rows and groups exist.
- **`steps`** run from the target back through CTEs (`"in"` names the statement), subqueries, temp tables and views. Each step has its expression and conditions. Names in step text are lower-cased, because BigQuery column names and aliases ignore case.
- **An empty `sources` list** means the value doesn't come from a table column. The first step's expression shows what it is: a constant (`'ETL'`), a function (`CURRENT_TIMESTAMP()`) or a script variable (`@run_date`). If a column's source genuinely can't be found, you get a "Lineage stops at …" warning.

## Python

```python
from bqlineage import analyze, BigQuerySchemaProvider
r = analyze(sql, BigQuerySchemaProvider("my-proj"), dataset="sales")
r.table_lineage()                                   # {"proj.dw.fact": ["proj.sales.customers", ...]}
r.column_lineage(table="dw.fact", columns=["total"])  # {"proj.dw.fact.total": ["proj.sales.orders.amount"]}
r.column_details(table="dw.fact", columns=["total"])  # sources + indirect_sources + steps, as in the CLI output
r.issues                                            # Issue(severity, message, statement, line, col)
```

## Multiple projects

Each table is looked up by its full `project.dataset.table` name, with your one login. The client's project only fills in names written without a project. Tables inside a view's SQL default to the view's own project. A table you can't access is reported as an error, and the rest of the script is still analyzed.

## Limits

- **Dynamic SQL is not analyzed:** `EXECUTE IMMEDIATE`, and the bodies of procedures run with `CALL`.
- **Variable values don't carry lineage.** Script and `FOR`-loop variables are treated as values, so `SET x = (SELECT …)` followed by `INSERT … SELECT x` shows no source.
- **Struct fields:** lineage of `address.city` stops at the column `address`.
- **`ALTER TABLE`** schema changes made inside the script aren't tracked.

## Tests (no BigQuery needed: the client is mocked)

```bash
.venv\Scripts\python -m pytest -q
```
