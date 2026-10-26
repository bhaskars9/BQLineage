"""Schema providers: where table columns (and view SQL) come from."""
from __future__ import annotations

import json
from dataclasses import dataclass


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


def _bq_type(field) -> str:
    t = _LEGACY_TYPES.get(field.field_type, field.field_type)
    if t == "STRUCT":
        t = "STRUCT<" + ", ".join(f"`{f.name}` {_bq_type(f)}" for f in field.fields) + ">"
    return f"ARRAY<{t}>" if field.mode == "REPEATED" else t


class BigQuerySchemaProvider:
    """Live schema from BigQuery. Auth comes from Application Default Credentials
    (`bqlineage auth` / `gcloud auth application-default login`, or GOOGLE_APPLICATION_CREDENTIALS)."""

    def __init__(self, project: str | None = None, client=None):
        if client is None:
            from google.cloud import bigquery  # optional dependency

            client = bigquery.Client(project=project)
        self.client = client
        self.project = project or client.project

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
