"""Batch analysis: many SQL scripts -> one lineage graph stored in the catalog file, traced across scripts.

SQL comes from .sql files, BigQuery job history (what actually ran, scheduled queries included) and the views in
the catalog. Each script's lineage is stored as edges. A script is analyzed again only when its SQL, the settings,
bqlineage itself, or the schema of a table it reads changed. See docs/scanning.md.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import config
from .analyzer import analyze
from .catalog import Catalog

try:
    VERSION = version("bqlineage")
except PackageNotFoundError:  # running from a source checkout that isn't installed
    VERSION = "dev"

_TABLES = """
CREATE TABLE IF NOT EXISTS scripts (name TEXT PRIMARY KEY, sql_hash TEXT, reads TEXT, analyzed_at REAL,
                                    errors INTEGER, warnings INTEGER);
CREATE TABLE IF NOT EXISTS edges (script TEXT, target TEXT, source TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS edges_by_target ON edges (target);
CREATE INDEX IF NOT EXISTS edges_by_source ON edges (source);
"""  # edges.kind: "table" (target/source are tables), "direct" / "indirect" (target/source are table.column)


@dataclass
class Item:
    name: str  # file path, "job:<hash>" or "view:<table>"
    sql: str


def files(paths) -> list[Item]:
    """Every .sql file in `paths` (files or directories, searched recursively)."""
    items = []
    for p in map(Path, paths):
        for f in [p] if p.is_file() else sorted(p.rglob("*.sql")):
            items.append(Item(f.as_posix(), f.read_text(encoding="utf-8-sig")))
    return items


def job_history(bq, days: int, region: str) -> list[Item]:
    """The distinct SQL of the successful writes run in the last `days` days. Scheduled queries are included,
    since they run as jobs; a script counts once, not once per statement. Needs bigquery.jobs.listAll."""
    rows = bq.client.query(
        f"SELECT query FROM `{bq.project}`.`region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
        f"WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY) "
        "AND job_type = 'QUERY' AND state = 'DONE' AND error_result IS NULL AND parent_job_id IS NULL "
        "AND statement_type IN ('INSERT', 'UPDATE', 'MERGE', 'CREATE_TABLE_AS_SELECT', 'CREATE_VIEW', "
        "'CREATE_MATERIALIZED_VIEW', 'SCRIPT') GROUP BY query").result()
    return [Item("job:" + hashlib.sha256(r.query.encode()).hexdigest()[:12], r.query) for r in rows]


def views(catalog: Catalog) -> list[Item]:
    """Every view in the catalog, as the CREATE VIEW that defines it."""
    return [Item(f"view:{fqn}", f"CREATE OR REPLACE VIEW `{fqn}` AS {sql}") for fqn, sql in catalog.views()]


def edges(result) -> list[tuple[str, str, str]]:
    """(target, source, kind) for everything a script writes; plain SELECTs write nothing."""
    out = [(t, s, "table") for t, srcs in result.table_lineage().items() if not t.startswith("<") for s in srcs]
    for key, d in result.column_details().items():
        if not key.startswith("<"):
            out += [(key, s, "direct") for s in d["sources"]] + [(key, s, "indirect") for s in d["indirect_sources"]]
    return out


def _fingerprint(columns) -> str | None:
    return None if columns is None else hashlib.sha256(json.dumps(list(columns.items())).encode()).hexdigest()[:16]


def _key(item: Item, s: config.Settings) -> str:
    """Changes whenever the analysis of `item` could: its SQL, the settings that shape it, bqlineage's version."""
    return hashlib.sha256(json.dumps([VERSION, s.project, s.dataset, s.expand_views, s.vars, item.sql]).encode()).hexdigest()


class Store:
    """The lineage tables inside the catalog file."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.executescript(_TABLES)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.close()

    def state(self, name: str) -> tuple[str, dict] | None:
        row = self.conn.execute("SELECT sql_hash, reads FROM scripts WHERE name = ?", (name,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def save(self, r: dict):
        with self.conn:
            self.conn.execute("DELETE FROM edges WHERE script = ?", (r["name"],))
            self.conn.executemany("INSERT INTO edges VALUES (?, ?, ?, ?)", [(r["name"], *e) for e in r["edges"]])
            self.conn.execute("INSERT OR REPLACE INTO scripts VALUES (?, ?, ?, ?, ?, ?)", (
                r["name"], r["key"], json.dumps(r["reads"]), time.time(), len(r["errors"]), r["warnings"]))


# ---- analysis, in worker processes when jobs > 1 ------------------------------------------------

_worker: dict = {}


def _start(settings: config.Settings):
    _worker["settings"], _worker["provider"] = settings, config.provider(settings)


def _stop():
    close = getattr(_worker.pop("provider", None), "close", None)
    if close:
        close()


def _analyze(item: Item) -> dict:
    s = _worker["settings"]
    r = analyze(item.sql, _worker["provider"], s.project, s.dataset, s.expand_views, vars=s.vars)
    return {"name": item.name, "key": _key(item, s), "edges": edges(r),
            "reads": {t: _fingerprint(c) for t, c in r.schemas.items()},
            "errors": [str(i) for i in r.issues if i.severity == "error"],
            "warnings": sum(i.severity == "warning" for i in r.issues)}


def scan(items: list[Item], settings: config.Settings, jobs: int = 1, force: bool = False) -> list[dict]:
    """Analyze what changed since the last scan and store its lineage. One report row per item:
    {"name", "status": "analyzed" | "unchanged", "errors": [...], "warnings": n}."""
    settings = replace(settings, catalog=settings.catalog or config.DEFAULT_CATALOG)
    report, todo = [], []
    with Store(settings.catalog) as store, Catalog(settings.catalog) as cat:
        for item in items:
            state = None if force else store.state(item.name)
            if state and state[0] == _key(item, settings) and all(
                    _fingerprint(getattr(cat.cached(t), "columns", None)) == fp for t, fp in state[1].items()):
                report.append({"name": item.name, "status": "unchanged", "errors": [], "warnings": 0})
            else:
                todo.append(item)
        if jobs > 1 and len(todo) > 1:
            with ProcessPoolExecutor(jobs, initializer=_start, initargs=(settings,)) as pool:
                results = list(pool.map(_analyze, todo, chunksize=max(1, len(todo) // (jobs * 4))))
        else:
            _start(settings)
            try:
                results = [_analyze(i) for i in todo]
            finally:
                _stop()
        for r in results:  # only this process writes the lineage tables
            store.save(r)
            report.append({"name": r["name"], "status": "analyzed", "errors": r["errors"], "warnings": r["warnings"]})
    return report


def trace(path: str, name: str, downstream: bool = False) -> dict:
    """Where `name` - a table, or "table.column" - comes from across every scanned script; with downstream=True,
    everything it feeds instead. A tree of {"node", "scripts", "edges"}; a node reached again is marked "repeat"
    instead of being expanded twice. `name` may leave out the project (or dataset) when that's unambiguous."""
    with Store(path) as store:
        nodes = {n for (n,) in store.conn.execute("SELECT target FROM edges UNION SELECT source FROM edges")}
        match = name if name in nodes else None
        if match is None:
            hits = sorted(n for n in nodes if n.endswith("." + name))
            if len(hits) != 1:
                raise LookupError(f"`{name}` matches {', '.join(hits)}" if hits else
                                  f"`{name}` isn't in the lineage store: run `bqlineage scan` first")
            match = hits[0]
        is_table = store.conn.execute("SELECT 1 FROM edges WHERE kind = 'table' AND (target = ? OR source = ?) LIMIT 1",
                                      (match, match)).fetchone()
        near, far = ("source", "target") if downstream else ("target", "source")
        expanded = set()

        def walk(node):
            expanded.add(node)
            rows = store.conn.execute(f"SELECT {far}, script FROM edges WHERE {near} = ? AND kind = ? ORDER BY 1, 2",
                                      (node, "table" if is_table else "direct"))
            grouped: dict[str, list[str]] = {}
            for n, script in rows:
                grouped.setdefault(n, []).append(script)
            out = []
            for n, scripts in grouped.items():
                entry = {"node": n, "scripts": sorted(set(scripts))}
                if n in expanded:
                    entry["repeat"] = True
                else:
                    entry["edges"] = walk(n)
                out.append(entry)
            return out

        return {"node": match, "edges": walk(match)}
