# The schema catalog

Lineage needs every table's columns, because columns are matched to tables by schema, never guessed. The catalog is a local SQLite file that keeps those schemas. That makes it:

- **Fast.** Thousands of tables are read once, not once per run.
- **Offline.** It works without BigQuery access once filled.
- **Reproducible.** It can be exported to a JSON snapshot for CI.

```bash
bqlineage catalog sync --project my-proj --region us     # every dataset in the region
bqlineage catalog sync --datasets sales crm              # or just these datasets
bqlineage lineage etl.sql                                # uses ./bqlineage.db automatically
```

## How a table is looked up

The catalog answers first. Only when it doesn't have the table, and `--bq` (or `bq = true`) is set, does bqlineage ask BigQuery, and it saves the answer.

- **"Not found" is saved too,** so repeat and offline runs stay fast and quiet.
- **`--refresh`** re-fetches each table the run uses, once.
- **`max_age_days`** in `bqlineage.toml` re-fetches entries older than that.
- **`catalog sync --rebuild`** empties the catalog and loads it again.

Without `--bq`, the catalog works alone. A table it doesn't have is reported as `not found in the catalog (pass --bq to look it up in BigQuery)`.

A name written without a project, such as `sales.orders`, matches the catalog's one `*.sales.orders`.

A wildcard such as `events_*` uses the schema of the newest shard: by creation time when asking BigQuery, by name when offline.

## catalog sync

`sync` reads `INFORMATION_SCHEMA.COLUMNS` and `INFORMATION_SCHEMA.VIEWS`: two queries per dataset, or per region with `--region`, instead of one API call per table.

- **Cost:** a few MB of query bytes.
- **Permissions:** it lists only the tables you can access.
- **Materialized views** may be synced without their SQL, because `INFORMATION_SCHEMA.VIEWS` covers logical views. Lineage then stops at them; one run with `--bq --refresh` fetches their SQL.

## export and import

```bash
bqlineage catalog export schema.json     # snapshot, e.g. checked into the repo
bqlineage catalog import schema.json     # load a snapshot (or any --schema file) into a catalog
```

The JSON has the same format as `--schema`.

## Multiple projects

Each table is looked up by its full `project.dataset.table` name, with your one login.

- **Defaults:** the project you configure, or your login's default, only fills in names written without a project.
- **Inside views:** tables in a view's SQL default to the view's own project.
- **No access:** a table you can't access is reported as an error, and the rest of the script is still analyzed.

## The file

`bqlineage.db` also holds the lineage stored by [`scan`](scanning.md), in its `scripts` and `edges` tables. You can query it directly with any SQLite tool.
