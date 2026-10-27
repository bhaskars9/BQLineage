# Scanning many scripts, and tracing across them

`lineage` looks at one script. In practice one script writes `dw.stage`, another reads it to build `dw.fact`, and a third builds a report on top. `scan` analyzes them all into one lineage graph, and `trace` follows it end to end.

```bash
bqlineage scan etl/ sql/reports/              # every .sql file, recursively
bqlineage trace dw.fact.total                 # where it comes from, across all scripts
bqlineage trace sales.orders --downstream     # everything it feeds
```

## What scan reads

- **Files and directories:** every `.sql` file, recursively.
- **`--from-jobs DAYS`:** the distinct SQL of the writes BigQuery actually ran in the last `DAYS` days. That includes scheduled queries and ad-hoc writes nobody committed.
  - A script counts once, not once per statement.
  - Needs `--region` and permission to list the project's jobs (`bigquery.jobs.listAll`).
- **`--from-views`:** every view in the catalog, as the `CREATE VIEW` that defines it.

Results go into the catalog file (`bqlineage.db` unless configured otherwise), in its `scripts` and `edges` tables.

## Only what changed

A script is analyzed again only when one of these changed since the last scan:

- its SQL
- the settings that shape the analysis (project, dataset, vars, view expansion)
- bqlineage's version
- the schema of a table it reads

Everything else is reported as `unchanged`. `--force` re-analyzes everything.

`--jobs N` analyzes in `N` worker processes. Only the main process writes to the store.

## trace

```text
$ bqlineage trace fact.total
p.dw.fact.total
`-- p.dw.stage.amount  [etl/b.sql]
    `-- p.sales.orders.amount  [etl/a.sql]
```

- **Each line** is one hop. The brackets show the script that makes it.
- **A table name** follows table lineage. A `table.column` name follows where the values come from.
- **Names** can leave out the project or dataset when that's unambiguous. Otherwise trace lists the matches.
- **A node reached twice** is shown in full once, then marked `(see above)`.
- **`--json`** prints the same tree as JSON. Each node has its `scripts` and its `edges`, or `"repeat": true`.

## The store

```text
scripts (name, sql_hash, reads, analyzed_at, errors, warnings)
edges   (script, target, source, kind)   -- kind: table | direct | indirect
```

- **`table` edges** connect tables.
- **`direct` edges** connect a target column to the columns its values come from.
- **`indirect` edges** connect it to the columns that decide its rows (JOIN / WHERE / GROUP BY …).

Any SQLite tool can query it, for example "which scripts write `p.dw.fact`":

```sql
SELECT DISTINCT script FROM edges WHERE target = 'p.dw.fact' AND kind = 'table';
```
