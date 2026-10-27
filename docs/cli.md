# Command line

```bash
bqlineage auth                                    # log in (gcloud application-default login)
bqlineage validate FILE [--dry-run]               # problems in the SQL
bqlineage lineage FILE [--table T] [--columns C ...] [--level table|column|both] [--raw]
                       [--drawio OUT.drawio] [--openlineage OUT.json]
bqlineage scan [PATH ...] [--from-jobs DAYS] [--from-views] [--jobs N] [--force]
bqlineage trace NAME [--downstream] [--json]      # lineage across scanned scripts
bqlineage catalog sync [--datasets D ...] [--rebuild]
bqlineage catalog export FILE.json
bqlineage catalog import FILE.json
bqlineage publish EVENTS.json                     # OpenLineage events -> Google Data Lineage API
```

`FILE` can be `-` to read SQL from stdin.

## bqlineage.toml

Settings that belong to an environment rather than a single run live in `bqlineage.toml`, read from the current folder or from `--config FILE`. Command-line flags override it.

```toml
project = "my-proj"        # default project for unqualified tables; the project queries run in
dataset = "sales"          # default dataset for unqualified tables
region = "us"              # for catalog sync, scan --from-jobs and publish
bq = true                  # fetch from BigQuery what the catalog doesn't have yet
catalog = "bqlineage.db"   # SQLite catalog: schema cache and scan results (this one is used automatically when present)
max_age_days = 7           # re-fetch catalog entries older than this (with bq = true)
expand_views = true        # trace through views; false treats them as sources
# schema = "schema.json"   # offline schema file instead of BigQuery
# refresh = true           # re-fetch every catalog entry a run uses

[vars]                     # values for template placeholders
run_date = "2026-10-27"
env = "prod"
```

An unknown key is an error, so a typo doesn't go unnoticed.

## Options shared by validate, lineage and scan

| Option | Meaning |
|---|---|
| `--config FILE` | Settings file (default `./bqlineage.toml` when present) |
| `--project`, `--dataset` | Defaults for table names written without them |
| `--bq` | Fetch schemas (and view SQL) from BigQuery |
| `--schema FILE` | Offline schema JSON instead: `{"project.dataset.table": {"col": "TYPE"}}`, keys may leave out the project |
| `--catalog FILE` | SQLite catalog to use ([catalog.md](catalog.md)) |
| `--refresh` | Re-fetch the catalog entries this run uses |
| `--var KEY=VALUE` | A template value; repeatable ([variables.md](variables.md)) |
| `--no-expand-views` | Treat views as sources instead of tracing through them |

`trace`, `catalog` and `publish` take `--config`, `--project`, `--catalog` and `--region`.

## Per command

**`validate`**
- `--dry-run` also sends the SQL to BigQuery's dry run. That's free, and it's BigQuery's own verdict on whether the SQL is valid.
- It also warns about any table BigQuery reads that the analysis didn't see, for example SQL built only at run time.
- Needs a login, and permission to run jobs in the project.

**`lineage`**
- `--table` picks a target, as `project.dataset.table`, `dataset.table` or just `table`.
- `--columns` picks columns, case-insensitively, separated by spaces or commas, with or without `--table`.
- `--level` chooses what's printed, and which [diagram](exports.md) pages are drawn.
- `--raw` shows every hop: temp tables and views as their own targets, and the one-to-one copy steps that are otherwise compacted away ([output.md](output.md)).

**`scan`, `trace`**: see [scanning.md](scanning.md).

**`catalog`**: see [catalog.md](catalog.md).

**`publish`**: see [exports.md](exports.md).

## Exit codes

`validate`, `lineage` and `scan` exit with 1 when any error was found, and 0 otherwise. Warnings don't change the exit code.
