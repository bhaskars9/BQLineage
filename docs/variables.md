# Variables, templates and dynamic SQL

## Templates

Scripts from Airflow, dbt or Composer carry placeholders that aren't SQL. bqlineage fills them before parsing:

| Placeholder | Example | Filled from |
|---|---|---|
| `{{ name }}` | `WHERE dt = '{{ ds }}'` | `ds` |
| `{{ params.name }}` | `` `{{ params.project }}.sales.orders` `` | `params.project`, else `project` |
| `${name}` | `` `${env}.dw.fact` `` | `env` |

Values come from `[vars]` in `bqlineage.toml` and from `--var KEY=VALUE`; the flag wins.

```bash
bqlineage lineage load.sql --var project=my-proj --var ds=2026-10-27
```

- **A placeholder without a value** produces one warning naming it. It's left as `__name__`, so the SQL still parses. In an expression it counts as a value, not an unknown column.
- **Expressions** such as `{{ macros.ds_add(ds, 1) }}` are left as placeholders too, because they need the orchestrator to evaluate them.
- **`{% if %}` / `{% for %}` template logic** is removed, not evaluated, so every branch is analyzed, with a warning. Line numbers stay the same, so error positions still match your file.
- **`${…}` inside triple-quoted strings** is left alone, because that's JavaScript in a UDF body, not a template.

For exact results with heavy template logic, render the SQL with your orchestrator first (for example `airflow tasks render`).

## Script variables

`DECLARE x …` and `FOR row IN (…)` variables are values, not columns, following BigQuery's own order: a name is a column if any table in the query has it, otherwise the variable. Query parameters such as `@run_date` need no values.

## Constants and dynamic SQL

When a variable's value is built only from literals and other known constants, bqlineage tracks it:

```sql
DECLARE target STRING DEFAULT 'dw.fact';
DECLARE src STRING DEFAULT CONCAT('sales', '.', 'orders');
EXECUTE IMMEDIATE FORMAT('INSERT INTO `%s` SELECT order_id, amount * %d FROM `%s`', target, 2, src);
```

That `EXECUTE IMMEDIATE` is analyzed like any other statement: `dw.fact` ← `sales.orders`.

- **Building blocks it can evaluate:**
  - string and number literals, including raw and triple-quoted strings
  - `||`, `CONCAT`, `CAST`
  - `FORMAT` with `%s %d %i %f %t %T %%`
  - `SET x = …` of those
- **The `USING …` / `INTO …` part** of `EXECUTE IMMEDIATE` is ignored.
- **Problems inside the generated SQL** are reported at the `EXECUTE IMMEDIATE` line, prefixed `in EXECUTE IMMEDIATE:`.

## Limits

- **SQL known only at run time** isn't analyzed: SQL read from a table, or built from a query result. It's reported as a warning. `validate --dry-run` lists tables BigQuery reads that the analysis didn't see.
- **Variable values carry no lineage.** In `SET x = (SELECT …)` followed by `INSERT … SELECT x`, the inserted value has no source.
- **`CALL` doesn't trace into the procedure.** To get the lineage of what a procedure writes, analyze or [scan](scanning.md) its `CREATE PROCEDURE` script: the body is analyzed, with the parameters treated as script variables.
