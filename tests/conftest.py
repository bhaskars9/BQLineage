"""Shared test data: an offline schema and a mocked BigQuery client - no BigQuery access needed."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import NotFound

from bqlineage import BigQuerySchemaProvider, DictSchemaProvider, analyze


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", help="rewrite tests/golden/*.json from current output")


TABLES = {
    "p.sales.orders": {"order_id": "INT64", "cust_id": "INT64", "amount": "FLOAT64", "order_ts": "TIMESTAMP",
                       "items": "ARRAY<STRUCT<sku STRING, qty INT64>>"},
    "p.sales.customers": {"customer_id": "INT64", "name": "STRING", "region": "STRING",
                          "address": "STRUCT<city STRING, zip STRING>"},
    "p.hr.employees": {"emp_id": "INT64", "manager_id": "INT64", "emp_name": "STRING"},
    "p.dw.fact": {"order_id": "INT64", "customer_name": "STRING", "total": "FLOAT64"},
    "p.sales.big_orders": {"columns": {"order_id": "INT64", "amount": "FLOAT64"},
                           "view_sql": "SELECT order_id, amount FROM sales.orders WHERE amount > 100"},
}


@pytest.fixture
def schema():
    return DictSchemaProvider(TABLES, project="p")


def cols(sql, schema, **kw):
    r = analyze(sql, schema, **kw)
    assert r.ok, r.issues
    return r.column_lineage()


COPY_CHAIN = """
CREATE TEMP TABLE stage AS SELECT order_id, amount FROM sales.orders;            -- copies, no filter
INSERT INTO dw.fact (order_id, total)
WITH c1 AS (SELECT order_id, amount FROM stage),                                   -- copies, no filter
     c2 AS (SELECT order_id, amount * 2 AS amt FROM c1 WHERE amount > 0)           -- real work
SELECT order_id, amt FROM c2
"""


def field(name, ftype, mode="NULLABLE", fields=()):
    return SimpleNamespace(name=name, field_type=ftype, mode=mode, fields=list(fields))


def mock_client():
    tables = {
        "p.sales.orders": SimpleNamespace(table_type="TABLE", view_query=None, schema=[
            field("order_id", "INTEGER"), field("amount", "FLOAT"),
            field("items", "RECORD", "REPEATED", [field("sku", "STRING"), field("qty", "INTEGER")])]),
        "p.sales.v": SimpleNamespace(table_type="VIEW", view_query="SELECT order_id AS oid FROM `p.sales.orders`",
                                     schema=[field("oid", "INTEGER")]),
    }
    client = MagicMock(project="p")

    def get_table(fqn):
        if fqn not in tables:
            raise NotFound(fqn)
        return tables[fqn]

    client.get_table.side_effect = get_table
    return client


@contextmanager
def mocked_bigquery(client=None):
    """Route every BigQuery call the CLI makes to `client` (default: mock_client())."""
    client = client or mock_client()

    def make(project=None):
        return BigQuerySchemaProvider(project, client=client)

    with patch("bqlineage.cli.BigQuerySchemaProvider", make), patch("bqlineage.config.BigQuerySchemaProvider", make):
        yield client


def information_schema(client, rows):
    """Answer the INFORMATION_SCHEMA queries iter_tables() runs, from {"p.ds.t": ({"col": "TYPE"}, view_sql)}."""
    def query(sql, job_config=None):
        result = []
        for fqn, (columns, view_sql) in rows.items():
            p, d, t = fqn.split(".")
            if "INFORMATION_SCHEMA.VIEWS" in sql and view_sql:
                result.append(SimpleNamespace(table_catalog=p, table_schema=d, table_name=t, view_definition=view_sql))
            if "INFORMATION_SCHEMA.COLUMNS" in sql:
                result += [SimpleNamespace(table_catalog=p, table_schema=d, table_name=t, column_name=c, data_type=ty)
                           for c, ty in columns.items()]
        return SimpleNamespace(result=lambda: result, sql=sql)

    client.query.side_effect = query
    return client


@pytest.fixture(autouse=True)
def _in_a_temp_dir(tmp_path, monkeypatch):
    """No test sees a bqlineage.toml or bqlineage.db from wherever pytest runs."""
    monkeypatch.chdir(tmp_path)
