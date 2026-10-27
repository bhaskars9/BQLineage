"""Where table schemas come from: a JSON file, live BigQuery, or a local SQLite catalog in front of either."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Iterator


@dataclass
class TableInfo:
    columns: dict[str, str]  # column name -> BigQuery type, in table order
    view_sql: str | None = None  # set when the table is a (materialized) view


def _to_info(value) -> TableInfo:
    if isinstance(value, TableInfo):
        return value
    if "columns" in value:
        return TableInfo(value["columns"], value.get("view_sql"))
    return TableInfo(value)


class DictSchemaProvider:
    """Offline schema: {"project.dataset.table": {"col": "TYPE"}} or {"...": {"columns": {...}, "view_sql": "..."}}."""

    def __init__(self, tables: dict, project: str | None = None):
        self.tables = {k: _to_info(v) for k, v in tables.items()}
        self.project = project

    @classmethod
    def from_json(cls, path: str, project: str | None = None) -> "DictSchemaProvider":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f), project)

    def get_table(self, fqn: str) -> TableInfo | None:
        # keys may leave out the project: "dataset.table"
        return self.tables.get(fqn) or self.tables.get(fqn.split(".", 1)[-1])


_LEGACY_TYPES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}
_STRUCT_FIELD = re.compile(r"([<,]\s*)([A-Za-z_]\w*)(\s+)(?=[A-Z])")  # "STRUCT<order STRING" -> "STRUCT<`order` STRING"


def _bq_type(field) -> str:
    t = _LEGACY_TYPES.get(field.field_type, field.field_type)
    if t == "STRUCT":
        t = "STRUCT<" + ", ".join(f"`{f.name}` {_bq_type(f)}" for f in field.fields) + ">"
    return f"ARRAY<{t}>" if field.mode == "REPEATED" else t


class BigQuerySchemaProvider:
    """Live schema from BigQuery. Auth comes from Application Default Credentials
    (`bqlineage auth` / `gcloud auth application-default login`, or GOOGLE_APPLICATION_CREDENTIALS).
    The client is created on first use, so a run answered entirely from the catalog works offline."""

    def __init__(self, project: str | None = None, client=None):
        self._project, self._client = project, client

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery  # optional dependency

            self._client = bigquery.Client(project=self._project)
        return self._client

    @property
    def project(self) -> str:
        return self._project or self.client.project

    def get_table(self, fqn: str) -> TableInfo | None:
        """Other API errors (e.g. 403 permission denied) propagate; analyze() reports them per table."""
        from google.api_core.exceptions import NotFound

        try:
            if fqn.endswith("*"):  # wildcard table: BigQuery uses the schema of the newest matching table
                project, dataset, prefix = fqn.rsplit(".", 2)
                shards = [t for t in self.client.list_tables(f"{project}.{dataset}") if t.table_id.startswith(prefix[:-1])]
                if not shards:
                    return None
                newest = max(shards, key=lambda t: (t.created.timestamp() if t.created else 0, t.table_id))
                t = self.client.get_table(newest.reference)
            else:
                t = self.client.get_table(fqn)
        except NotFound:
            return None
        view_sql = t.view_query if t.table_type == "VIEW" else getattr(t, "mview_query", None)
        return TableInfo({f.name: _bq_type(f) for f in t.schema}, view_sql)

    def iter_tables(self, datasets: Iterable[str] = (), region: str | None = None) -> Iterator[tuple[str, TableInfo]]:
        """Every table and view of `datasets` (or of the whole `region`) via INFORMATION_SCHEMA: two queries per
        scope instead of one API call per table."""
        scopes = [f"`{self.project}`.`{d}`" for d in datasets] or [f"`{self.project}`.`region-{region}`"]
        for scope in scopes:
            views = {f"{r.table_catalog}.{r.table_schema}.{r.table_name}": r.view_definition for r in self.client.query(
                f"SELECT table_catalog, table_schema, table_name, view_definition FROM {scope}.INFORMATION_SCHEMA.VIEWS"
            ).result()}
            current, columns = None, {}
            for r in self.client.query(
                "SELECT table_catalog, table_schema, table_name, column_name, data_type "
                f"FROM {scope}.INFORMATION_SCHEMA.COLUMNS WHERE is_hidden = 'NO' "
                "ORDER BY table_catalog, table_schema, table_name, ordinal_position"
            ).result():
                fqn = f"{r.table_catalog}.{r.table_schema}.{r.table_name}"
                if fqn != current:
                    if current:
                        yield current, TableInfo(columns, views.get(current))
                    current, columns = fqn, {}
                columns[r.column_name] = _STRUCT_FIELD.sub(r"\1`\2`\3", r.data_type)
            if current:
                yield current, TableInfo(columns, views.get(current))

    def dry_run(self, sql: str) -> tuple[str | None, set[str]]:
        """Ask BigQuery itself to validate `sql` without running it (free). Returns (error or None, tables read)."""
        from google.api_core.exceptions import GoogleAPICallError
        from google.cloud import bigquery

        try:
            job = self.client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        except GoogleAPICallError as e:
            return getattr(e, "message", None) or str(e), set()
        return None, {f"{t.project}.{t.dataset_id}.{t.table_id}" for t in job.referenced_tables or ()}


class Catalog:
    """A local SQLite catalog of table schemas, in front of another provider (BigQuery, a JSON file) or alone.

    get_table answers from the catalog. On a miss, with refresh=True (once per table per run), or for an entry
    older than max_age_days, it asks `fallback` and saves the answer - "table not found" included, so offline and
    repeat runs stay fast and quiet. The same file also holds what `bqlineage scan` stores (see scan.py)."""

    def __init__(self, path: str, fallback=None, refresh: bool = False, max_age_days: float | None = None,
                 project: str | None = None):
        self.path, self.fallback, self.refresh, self.max_age_days = path, fallback, refresh, max_age_days
        self._project = project
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")  # parallel scan workers read while others write
        self.conn.execute("CREATE TABLE IF NOT EXISTS tables (fqn TEXT PRIMARY KEY, columns TEXT, "
                          "view_sql TEXT, fetched_at REAL)")  # columns: JSON {name: type}; NULL = doesn't exist
        self._refreshed: set[str] = set()

    @property
    def project(self) -> str | None:
        """The default project: as given, else the fallback's (BigQuery's login project) when it can be had."""
        if self._project is None and self.fallback is not None:
            try:
                self._project = getattr(self.fallback, "project", None)
            except Exception:  # no credentials: fine while the catalog has the answers
                pass
        return self._project

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.conn.close()

    def _row(self, fqn: str):
        row = self.conn.execute("SELECT columns, view_sql, fetched_at FROM tables WHERE fqn = ?", (fqn,)).fetchone()
        if row is None and fqn.count(".") == 1:  # no project given: use the one table with this dataset.table
            like = "%." + fqn.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self.conn.execute("SELECT columns, view_sql, fetched_at FROM tables WHERE fqn LIKE ? ESCAPE '\\' "
                                     "LIMIT 2", (like,)).fetchall()
            row = rows[0] if len(rows) == 1 else None
        if row is None and fqn.endswith("*") and self.fallback is None:  # wildcard, offline: newest shard by name
            row = self.conn.execute("SELECT columns, view_sql, fetched_at FROM tables WHERE substr(fqn, 1, ?) = ? "
                                    "AND columns IS NOT NULL ORDER BY fqn DESC LIMIT 1",
                                    (len(fqn) - 1, fqn[:-1])).fetchone()
        return row

    def cached(self, fqn: str) -> TableInfo | None:
        """The catalog's entry for `fqn`, never asking the fallback."""
        row = self._row(fqn)
        return TableInfo(json.loads(row[0]), row[1]) if row and row[0] is not None else None

    def get_table(self, fqn: str) -> TableInfo | None:
        row = self._row(fqn)
        stale = row is None or (self.refresh and fqn not in self._refreshed) or (
            self.max_age_days is not None and time.time() - row[2] > self.max_age_days * 86400)
        if stale and self.fallback is not None:
            info = self.fallback.get_table(fqn)  # errors (permission denied, ...) propagate to the analyzer
            self.put(fqn, info)
            self._refreshed.add(fqn)
            return info
        return TableInfo(json.loads(row[0]), row[1]) if row and row[0] is not None else None

    def put(self, fqn: str, info: TableInfo | None):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO tables VALUES (?, ?, ?, ?)", (
                fqn, json.dumps(info.columns) if info else None, info.view_sql if info else None, time.time()))

    def sync(self, tables: Iterable[tuple[str, TableInfo]], rebuild: bool = False) -> int:
        """Store many tables at once (e.g. BigQuerySchemaProvider.iter_tables()). rebuild=True empties it first."""
        rows = [(fqn, json.dumps(i.columns), i.view_sql, time.time()) for fqn, i in tables]
        with self.conn:
            if rebuild:
                self.conn.execute("DELETE FROM tables")
            self.conn.executemany("INSERT OR REPLACE INTO tables VALUES (?, ?, ?, ?)", rows)
        return len(rows)

    def views(self) -> Iterator[tuple[str, str]]:
        """(view, its SQL) for every view in the catalog."""
        yield from self.conn.execute("SELECT fqn, view_sql FROM tables WHERE view_sql IS NOT NULL ORDER BY fqn")

    def export(self, path: str) -> int:
        """Write the catalog as JSON in the --schema format, e.g. to check a snapshot into the repo for CI."""
        rows = self.conn.execute("SELECT fqn, columns, view_sql FROM tables WHERE columns IS NOT NULL ORDER BY fqn")
        data = {fqn: {"columns": json.loads(c), **({"view_sql": v} if v else {})} for fqn, c, v in rows}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        return len(data)

    def load_json(self, path: str) -> int:
        """Add the tables of a --schema JSON file."""
        return self.sync(DictSchemaProvider.from_json(path).tables.items())
