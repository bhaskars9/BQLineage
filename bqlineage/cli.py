"""bqlineage CLI: auth | validate | lineage."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from .core import analyze
from .schema import BigQuerySchemaProvider, DictSchemaProvider


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


def _provider(args):
    if args.schema:
        return DictSchemaProvider.from_json(args.schema, args.project)
    if args.bq:
        return BigQuerySchemaProvider(args.project)
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bqlineage", description="BigQuery table/column lineage")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="log in with gcloud Application Default Credentials")
    for name in ("validate", "lineage"):
        s = sub.add_parser(name)
        s.add_argument("file", help="SQL file, or - for stdin")
        g = s.add_mutually_exclusive_group()
        g.add_argument("--schema", help="offline schema JSON: {\"project.dataset.table\": {\"col\": \"TYPE\"}}")
        g.add_argument("--bq", action="store_true", help="fetch table schemas/view SQL from BigQuery")
        s.add_argument("--project", help="default project for unqualified tables")
        s.add_argument("--dataset", help="default dataset for unqualified tables")
        s.add_argument("--no-expand-views", action="store_true", help="treat views as sources, don't trace through them")
    lp = sub.choices["lineage"]
    lp.add_argument("--level", choices=["table", "column", "both"], default="both")
    lp.add_argument("--raw", action="store_true", help="don't trace through temp tables/views")
    args = p.parse_args(argv)

    if args.cmd == "auth":
        gcloud = shutil.which("gcloud")
        if not gcloud:
            print("gcloud not found. Install the Google Cloud SDK, or set GOOGLE_APPLICATION_CREDENTIALS "
                  "to a service-account key file.", file=sys.stderr)
            return 1
        return subprocess.call([gcloud, "auth", "application-default", "login"])

    result = analyze(_read(args.file), _provider(args), args.project, args.dataset, not args.no_expand_views)
    if args.cmd == "validate":
        for i in result.issues:
            print(i)
        print("OK" if result.ok else "FAILED")
        return 0 if result.ok else 1

    out = result.to_dict(resolve=not args.raw)
    if args.level == "table":
        out.pop("columns")
    elif args.level == "column":
        out.pop("tables")
    print(json.dumps(out, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
