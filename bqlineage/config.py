"""Settings: bqlineage.toml (optional), overridden by command-line flags. See docs/cli.md."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields

from .catalog import BigQuerySchemaProvider, Catalog, DictSchemaProvider

CONFIG_FILE = "bqlineage.toml"
DEFAULT_CATALOG = "bqlineage.db"  # used when it exists here and no other catalog is set; scan creates it


@dataclass
class Settings:
    project: str | None = None  # default project for unqualified tables; the project BigQuery queries run in
    dataset: str | None = None  # default dataset for unqualified tables
    bq: bool = False  # ask BigQuery for schemas (and view SQL) it doesn't have yet
    schema: str | None = None  # offline schema JSON instead of BigQuery
    catalog: str | None = None  # SQLite catalog file: schemas cached here, scan results stored here
    refresh: bool = False  # re-fetch the catalog entries this run touches
    max_age_days: float | None = None  # re-fetch catalog entries older than this
    expand_views: bool = True  # trace through views (False: views are sources)
    region: str | None = None  # BigQuery region ("us", "eu", ...) for catalog sync / job history / publish
    vars: dict[str, str] = field(default_factory=dict)  # values for template placeholders


def load(path: str | None = None, **overrides) -> Settings:
    """Read `path` (or ./bqlineage.toml when it exists), then apply the overrides that aren't None."""
    data: dict = {}
    path = path or (CONFIG_FILE if os.path.exists(CONFIG_FILE) else None)
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        unknown = set(data) - {f.name for f in fields(Settings)}
        if unknown:
            raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
    variables = {**data.pop("vars", {}), **(overrides.pop("vars", None) or {})}
    data.update({k: v for k, v in overrides.items() if v is not None})
    if not data.get("catalog") and os.path.exists(DEFAULT_CATALOG):
        data["catalog"] = DEFAULT_CATALOG  # e.g. after `bqlineage catalog sync` or `scan` in this folder
    return Settings(**data, vars={k: str(v) for k, v in variables.items()})


def provider(s: Settings):
    """The schema source the settings describe: a JSON file or BigQuery, behind the SQLite catalog when one is set."""
    base = DictSchemaProvider.from_json(s.schema, s.project) if s.schema else (
        BigQuerySchemaProvider(s.project) if s.bq else None)
    return Catalog(s.catalog, base, s.refresh, s.max_age_days, s.project) if s.catalog else base
