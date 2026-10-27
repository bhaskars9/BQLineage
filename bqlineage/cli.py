"""bqlineage command line. Every command reads bqlineage.toml when present; flags override it (docs/cli.md)."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tomllib

from . import config, scan
from .analyzer import analyze
from .catalog import BigQuerySchemaProvider, Catalog
from .drawio import to_drawio
from .model import Issue
from .openlineage import publish, to_openlineage


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def _settings(args) -> config.Settings:
    variables = {}
    for v in getattr(args, "var", None) or []:
        key, eq, value = v.partition("=")
        if not eq:
            sys.exit(f"bqlineage: --var expects KEY=VALUE, got `{v}`")
        variables[key] = value
    try:
        return config.load(args.config, project=args.project, catalog=args.catalog, region=args.region,
                           dataset=getattr(args, "dataset", None), bq=getattr(args, "bq", None),
                           schema=getattr(args, "schema", None), refresh=getattr(args, "refresh", None),
                           expand_views=getattr(args, "expand_views", None), vars=variables)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        sys.exit(f"bqlineage: {e}")


def _bigquery(s: config.Settings) -> BigQuerySchemaProvider:
    """A connected BigQuery provider, or one clear message about logging in."""
    bq = BigQuerySchemaProvider(s.project)
    try:
        bq.client
    except Exception as e:  # no credentials / no default project
        sys.exit(f"Could not connect to BigQuery: {e}\n"
                 "Run `bqlineage auth` (or set GOOGLE_APPLICATION_CREDENTIALS) and set the project.")
    return bq


def _provider(s: config.Settings):
    if s.bq and not s.schema and not s.catalog:
        _bigquery(s)  # fail fast; behind a catalog BigQuery is only needed for tables not cached yet
    try:
        return config.provider(s)
    except (OSError, ValueError) as e:  # unreadable --schema file
        sys.exit(f"bqlineage: {e}")


def _tree(node: dict, prefix: str = "") -> list[str]:
    lines, edges = [], node.get("edges", [])
    for i, e in enumerate(edges):
        last = i == len(edges) - 1
        lines.append(f"{prefix}{'`-- ' if last else '|-- '}{e['node']}  [{', '.join(e['scripts'])}]"
                     + ("  (see above)" if e.get("repeat") else ""))
        lines += _tree(e, prefix + ("    " if last else "|   "))
    return lines


def _parser() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--config", help="settings file (default: ./bqlineage.toml when present)")
    base.add_argument("--project", help="default project for unqualified tables; the project queries run in")
    base.add_argument("--catalog", help="SQLite catalog file (schemas cache, scan results)")
    base.add_argument("--region", help="BigQuery region, e.g. us or eu (catalog sync, job history, publish)")
    common = argparse.ArgumentParser(add_help=False, parents=[base])
    common.add_argument("--dataset", help="default dataset for unqualified tables")
    source = common.add_mutually_exclusive_group()
    source.add_argument("--bq", action="store_true", default=None, help="fetch schemas from BigQuery")
    source.add_argument("--schema", help="offline schema JSON: {\"project.dataset.table\": {\"col\": \"TYPE\"}}")
    common.add_argument("--refresh", action="store_true", default=None, help="re-fetch catalog entries this run uses")
    common.add_argument("--var", action="append", metavar="KEY=VALUE", help="template value (repeatable)")
    common.add_argument("--no-expand-views", dest="expand_views", action="store_const", const=False,
                        help="treat views as sources, don't trace through them")

    p = argparse.ArgumentParser(prog="bqlineage", description="BigQuery table/column lineage")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="log in with gcloud Application Default Credentials")

    v = sub.add_parser("validate", parents=[common], help="check SQL for problems")
    v.add_argument("file", help="SQL file, or - for stdin")
    v.add_argument("--dry-run", action="store_true", help="also ask BigQuery itself to validate it (free)")

    lp = sub.add_parser("lineage", parents=[common], help="lineage of one script")
    lp.add_argument("file", help="SQL file, or - for stdin")
    lp.add_argument("--table", help="only this target table: project.dataset.table, dataset.table or table")
    lp.add_argument("--columns", nargs="+", metavar="COL", help="only these target columns (space or comma separated)")
    lp.add_argument("--level", choices=["table", "column", "both"], default="both")
    lp.add_argument("--raw", action="store_true",
                    help="show every hop: don't trace through temp tables/views or drop one-to-one copy steps")
    lp.add_argument("--drawio", metavar="FILE", help="also write a draw.io diagram (pages follow --level)")
    lp.add_argument("--openlineage", metavar="FILE", help="also write an OpenLineage event (see `publish`)")

    sp = sub.add_parser("scan", parents=[common], help="analyze many scripts into the catalog's lineage store")
    sp.add_argument("paths", nargs="*", help=".sql files or directories")
    sp.add_argument("--from-jobs", type=int, metavar="DAYS", help="also the writes BigQuery ran in the last DAYS days")
    sp.add_argument("--from-views", action="store_true", help="also every view in the catalog")
    sp.add_argument("--jobs", type=int, default=1, help="worker processes (default 1)")
    sp.add_argument("--force", action="store_true", help="re-analyze even unchanged scripts")

    tp = sub.add_parser("trace", parents=[base], help="lineage across scanned scripts")
    tp.add_argument("name", help="table or table.column (project/dataset may be left out)")
    tp.add_argument("--downstream", action="store_true", help="what it feeds, instead of where it comes from")
    tp.add_argument("--json", action="store_true", help="print JSON instead of a tree")

    cp = sub.add_parser("catalog", help="manage the schema catalog")
    csub = cp.add_subparsers(dest="action", required=True)
    sync = csub.add_parser("sync", parents=[base], help="load schemas from BigQuery INFORMATION_SCHEMA")
    sync.add_argument("--datasets", nargs="+", metavar="DATASET", help="these datasets (default: all of --region)")
    sync.add_argument("--rebuild", action="store_true", help="empty the catalog first")
    for name, what in (("export", "write the catalog as --schema JSON"), ("import", "add a --schema JSON file")):
        c = csub.add_parser(name, parents=[base], help=what)
        c.add_argument("file")

    pub = sub.add_parser("publish", parents=[base], help="send OpenLineage events to Google's Data Lineage API")
    pub.add_argument("file", help="JSON from `lineage --openlineage` (one event or a list)")
    return p


def main(argv=None) -> int:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)  # unsupported syntax is reported as our own warnings
    args = _parser().parse_args(argv)

    if args.cmd == "auth":
        gcloud = shutil.which("gcloud")
        if not gcloud:
            print("gcloud not found. Install the Google Cloud SDK, or set GOOGLE_APPLICATION_CREDENTIALS "
                  "to a service-account key file.", file=sys.stderr)
            return 1
        return subprocess.call([gcloud, "auth", "application-default", "login"])

    s = _settings(args)
    if args.cmd == "trace":
        try:
            tree = scan.trace(s.catalog or config.DEFAULT_CATALOG, args.name, args.downstream)
        except LookupError as e:
            sys.exit(str(e))
        print(json.dumps(tree, indent=2) if args.json else "\n".join([tree["node"], *_tree(tree)]))
        return 0
    if args.cmd == "catalog":
        return _catalog(args, s)
    if args.cmd == "publish":
        return _publish(args, s)
    if args.cmd == "scan":
        return _scan(args, s)

    result = analyze(_read(args.file), _provider(s), s.project, s.dataset, s.expand_views, vars=s.vars)
    if args.cmd == "validate":
        issues = result.issues + (_dry_run(result, s) if args.dry_run else [])
        for i in issues:
            print(i)
        ok = not any(i.severity == "error" for i in issues)
        print("OK" if ok else "FAILED")
        return 0 if ok else 1

    if args.table and not result.targets(args.table):
        sys.exit(f"No target table matches `{args.table}`. Targets: {', '.join(result.targets()) or 'none'}")
    columns = [c for arg in args.columns or [] for c in arg.split(",") if c] or None
    out = result.to_dict(not args.raw, args.table, columns)
    if columns and not out["columns"]:
        sys.exit(f"None of the columns {', '.join(columns)} found" + (f" in `{args.table}`" if args.table else ""))
    if args.drawio:
        with open(args.drawio, "w", encoding="utf-8") as f:
            f.write(to_drawio(result, args.level, not args.raw, args.table, columns))
        print(f"wrote {args.drawio} (open it in draw.io / diagrams.net)", file=sys.stderr)
    if args.openlineage:
        with open(args.openlineage, "w", encoding="utf-8") as f:
            json.dump(to_openlineage(result, args.file), f, indent=2)
        print(f"wrote {args.openlineage} (send it with `bqlineage publish`)", file=sys.stderr)
    if args.level == "table":
        out.pop("columns")
    elif args.level == "column":
        out.pop("tables")
    print(json.dumps(out, indent=2))
    return 0 if result.ok else 1


def _dry_run(result, s: config.Settings) -> list[Issue]:
    """BigQuery's own verdict, plus any table it reads that this analysis didn't see (e.g. run-time dynamic SQL)."""
    error, read = _bigquery(s).dry_run(result.sql)
    if error:
        return [Issue("error", f"BigQuery dry run: {error}")]
    seen = {t for st in result.statements for t in st.sources | {st.target}} | set(result.schemas)
    missed = sorted(t for t in read if not any(t == o or t.endswith("." + o) for o in seen))
    print("BigQuery dry run: OK")
    return [Issue("warning", f"BigQuery reads tables this analysis didn't see: {', '.join(missed)}")] if missed else []


def _scan(args, s: config.Settings) -> int:
    items = scan.files(args.paths)
    if args.from_jobs:
        if not s.region:
            sys.exit("--from-jobs needs --region (or region in bqlineage.toml)")
        items += scan.job_history(_bigquery(s), args.from_jobs, s.region)
    if args.from_views:
        with Catalog(s.catalog or config.DEFAULT_CATALOG) as cat:
            items += scan.views(cat)
    if not items:
        sys.exit("Nothing to scan: give .sql files or directories, --from-jobs or --from-views")
    if s.bq and not s.schema:
        _bigquery(s)
    report = scan.scan(items, s, args.jobs, args.force)
    for r in report:
        if r["status"] == "analyzed":
            print(f"{r['name']}: {len(r['errors'])} errors, {r['warnings']} warnings")
            for e in r["errors"]:
                print(f"    {e}")
    done = sum(r["status"] == "analyzed" for r in report)
    print(f"{done} analyzed, {len(report) - done} unchanged -> {s.catalog or config.DEFAULT_CATALOG}")
    return 1 if any(r["errors"] for r in report) else 0


def _catalog(args, s: config.Settings) -> int:
    path = s.catalog or config.DEFAULT_CATALOG
    with Catalog(path) as cat:
        if args.action == "sync":
            if not (args.datasets or s.region):
                sys.exit("catalog sync needs --datasets or --region (or region in bqlineage.toml)")
            n = cat.sync(_bigquery(s).iter_tables(args.datasets or (), s.region), args.rebuild)
            print(f"synced {n} tables and views into {path}")
        elif args.action == "export":
            print(f"wrote {cat.export(args.file)} tables to {args.file}")
        else:
            print(f"added {cat.load_json(args.file)} tables to {path}")
    return 0


def _publish(args, s: config.Settings) -> int:
    if not (s.project and s.region):
        sys.exit("publish needs --project and --region (or both in bqlineage.toml)")
    with open(args.file, encoding="utf-8") as f:
        events = json.load(f)
    events = events if isinstance(events, list) else [events]
    try:
        for e in events:
            publish(e, s.project, s.region)
    except Exception as e:  # HTTP 403 / API not enabled / no credentials
        sys.exit(f"Data Lineage API: {e}")
    print(f"published {len(events)} event(s) to Data Lineage in {s.project}/{s.region}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
