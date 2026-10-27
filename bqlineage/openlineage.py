"""OpenLineage events (openlineage.io): the lineage format Marquez, DataHub and Google's Data Lineage API read.
See docs/exports.md."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

PRODUCER = "https://github.com/bhaskars9/BQLineage"
RUN_EVENT = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
COLUMN_LINEAGE = "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json#/$defs/ColumnLineageDatasetFacet"
_AGGREGATE = re.compile(r"\b(SUM|COUNT|COUNTIF|AVG|MIN|MAX|ANY_VALUE|ARRAY_AGG|STRING_AGG|ARRAY_CONCAT_AGG|"
                        r"LOGICAL_AND|LOGICAL_OR|BIT_(AND|OR|XOR)|APPROX_\w+|STDDEV\w*|VAR\w*|CORR|COVAR\w*)\s*\(", re.I)
_COLUMN = re.compile(r"[\w`]+(\.[\w`]+)+")


def _dataset(name: str) -> dict:
    """OpenLineage dataset naming: BigQuery tables in namespace "bigquery", files in "gs://bucket"."""
    if name.startswith("gs://"):
        bucket, _, path = name[5:].partition("/")
        return {"namespace": f"gs://{bucket}", "name": path}
    return {"namespace": "bigquery", "name": name}


def _subtype(steps: list[dict]) -> str:
    """How the values were derived: AGGREGATION, IDENTITY (copied as is) or TRANSFORMATION."""
    if any(_AGGREGATE.search(s["expression"]) for s in steps):
        return "AGGREGATION"
    if all(_COLUMN.fullmatch(s["expression"]) for s in steps):
        return "IDENTITY"
    return "TRANSFORMATION"


def to_openlineage(result, job: str, namespace: str = "bqlineage") -> dict:
    """One COMPLETE RunEvent for a script: the tables it reads as inputs, the tables it writes as outputs, each output
    with a columnLineage facet - DIRECT inputs (where values come from) and INDIRECT ones (what decides the rows)."""
    tables = result.table_lineage()
    outputs: dict[str, dict] = {t: {} for t in tables if not t.startswith("<")}
    for key, d in result.column_details().items():
        table, column = key.rsplit(".", 1)
        if table not in outputs:
            continue
        direct = [{"type": "DIRECT", "subtype": _subtype(d["steps"])}]
        outputs[table][column] = {"inputFields": [
            {**_dataset(s.rsplit(".", 1)[0]), "field": s.rsplit(".", 1)[1], "transformations": t}
            for srcs, t in ((d["sources"], direct), (d["indirect_sources"], [{"type": "INDIRECT"}])) for s in srcs]}
    return {
        "eventType": "COMPLETE",
        "eventTime": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT,
        "run": {"runId": str(uuid.uuid4())},
        "job": {"namespace": namespace, "name": job},
        "inputs": [_dataset(s) for s in sorted({s for srcs in tables.values() for s in srcs})],
        "outputs": [{**_dataset(t), "facets": {"columnLineage": {
            "_producer": PRODUCER, "_schemaURL": COLUMN_LINEAGE, "fields": fields}}} for t, fields in outputs.items()],
    }


def publish(event: dict, project: str, location: str, session=None) -> dict:
    """Send an event to Google's Data Lineage API so the lineage appears in the BigQuery / Dataplex console next to
    the lineage BigQuery records itself. Needs the Data Lineage API enabled and datalineage.events.create."""
    if session is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(credentials)
    url = f"https://datalineage.googleapis.com/v1/projects/{project}/locations/{location}:processOpenLineageRunEvent"
    response = session.post(url, json=event)
    response.raise_for_status()
    return response.json()
