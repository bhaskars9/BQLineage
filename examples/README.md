# Examples

One example per feature, all offline: [`schema.json`](schema.json) stands in for the catalog. Run the commands from the repo root. The files next to each script are exactly what its command produces, and [`tests/test_examples.py`](../tests/test_examples.py) checks they stay that way.

| Example | Shows | Output |
|---|---|---|
| [`orders.sql`](orders.sql) | the [worked example](../docs/output.md#a-worked-example): a temp table, a CTE, a `JOIN … USING`, each column's steps | [json](orders.lineage.json), [draw.io](orders.drawio), [OpenLineage](orders.openlineage.json) |
| [`joins.sql`](joins.sql) | driving vs joined tables: `LEFT`, `RIGHT` and `FULL JOIN`, a table that only filters, a subquery | [json](joins.lineage.json), [draw.io](joins.drawio) |
| [`views.sql`](views.sql) | views traced to the tables under them (a view over a view), a wildcard table with `_TABLE_SUFFIX` | [json](views.lineage.json), [draw.io](views.drawio) |
| [`merge.sql`](merge.sql) | `MERGE` with `WHEN MATCHED` / `NOT MATCHED`, `UPDATE … FROM` | [json](merge.lineage.json), [draw.io](merge.drawio) |
| [`procedural.sql`](procedural.sql) | `BEGIN … EXCEPTION … END`, `DECLARE`, `IF`, `WHILE`, a temp table created twice (`_SESSION.refunds#2`) | [json](procedural.lineage.json), [draw.io](procedural.drawio) |
| [`templated.sql`](templated.sql) | Airflow / dbt templates, `EXECUTE IMMEDIATE` built from constants, [`bqlineage.toml`](bqlineage.toml) | [json](templated.lineage.json), [draw.io](templated.drawio) |
| [`recursive.sql`](recursive.sql) | a recursive CTE, `UNION ALL`, subqueries in `FROM` and `WHERE`, a CTE that only copies | [json](recursive.lineage.json), [draw.io](recursive.drawio) |
| [`validate.sql`](validate.sql) | what `validate` reports: errors and warnings, with line numbers | [text](validate.txt) |
| [`scan/`](scan) | three scripts that build on each other, scanned together and traced end to end | [text](scan/trace.txt) |
| [`api.py`](api.py) | the same from Python | prints |

## Lineage of one script

```bash
bqlineage lineage examples/joins.sql --schema examples/schema.json --drawio examples/joins.drawio
```

The JSON goes to stdout (each `.lineage.json` here), the diagram to `--drawio` ([what the diagram shows](../docs/exports.md#drawio)). Try these on any of the scripts:

```bash
bqlineage lineage examples/joins.sql --schema examples/schema.json --table order_report                  # one target
bqlineage lineage examples/joins.sql --schema examples/schema.json --table order_report --columns segment # one column
bqlineage lineage examples/joins.sql --schema examples/schema.json --level table                         # tables only
bqlineage lineage examples/recursive.sql --schema examples/schema.json --raw              # every hop, all_staff too
bqlineage lineage examples/views.sql --schema examples/schema.json --no-expand-views      # stop at the views
```

## Templates and settings

```bash
bqlineage lineage examples/templated.sql --config examples/bqlineage.toml
bqlineage lineage examples/templated.sql --schema examples/schema.json --var project=shop --var ds=2026-10-27 --var min_qty=1
```

Both give the same result. [`bqlineage.toml`](bqlineage.toml) holds the schema file and the template values ([variables.md](../docs/variables.md)).

## Validate

```bash
bqlineage validate examples/validate.sql --schema examples/schema.json
```

It prints [`validate.txt`](validate.txt) and exits with 1, because there are errors.

## Scan and trace

```bash
bqlineage scan examples/scan --schema examples/schema.json --catalog examples.db
bqlineage trace rpt.region_totals.total --catalog examples.db
bqlineage trace sales.orders --downstream --catalog examples.db
```

`region_totals.total` comes from `sales.orders.amount` through all three scripts ([`scan/trace.txt`](scan/trace.txt), [scanning.md](../docs/scanning.md)).

## Catalog, offline

```bash
bqlineage catalog import examples/schema.json --catalog examples.db
bqlineage lineage examples/orders.sql --catalog examples.db       # schemas from the catalog alone
bqlineage catalog export snapshot.json --catalog examples.db
```

## OpenLineage

```bash
bqlineage lineage examples/orders.sql --schema examples/schema.json --openlineage orders.openlineage.json
```

[`orders.openlineage.json`](orders.openlineage.json) is that event. Its `eventTime` and `runId` are placeholders here, because they change on every run. `bqlineage publish` sends it to Google's Data Lineage API ([exports.md](../docs/exports.md#openlineage)).

## Python

```bash
python examples/api.py
```

## Needs BigQuery

These need a login (`bqlineage auth`), so they have no offline example:

- `catalog sync`: fill the catalog from `INFORMATION_SCHEMA` ([catalog.md](../docs/catalog.md))
- `--bq`: fetch what the catalog doesn't have
- `validate --dry-run`: BigQuery's own check ([cli.md](../docs/cli.md))
- `scan --from-jobs DAYS`: the SQL BigQuery actually ran ([scanning.md](../docs/scanning.md))
- `publish`: send OpenLineage events ([exports.md](../docs/exports.md))
