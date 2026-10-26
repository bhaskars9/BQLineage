import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import NotFound

from bqlineage import BigQuerySchemaProvider, DictSchemaProvider, LineageError, analyze, validate
from bqlineage import cli

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


def test_unaliased_join_columns_resolved_from_schema(schema):
    r = analyze("SELECT order_id, name, amount * 2 AS dbl FROM `p.sales.orders` "
                "JOIN `p.sales.customers` ON cust_id = customer_id WHERE region = 'EU'", schema)
    assert r.ok, r.issues
    assert r.column_lineage() == {
        "<query>.dbl": ["p.sales.orders.amount"],
        "<query>.name": ["p.sales.customers.name"],
        "<query>.order_id": ["p.sales.orders.order_id"],
    }
    assert r.table_lineage() == {"<query>": ["p.sales.customers", "p.sales.orders"]}


def test_ambiguous_column_is_an_error_not_a_guess(schema):
    sql = "SELECT order_id FROM `p.sales.orders` o JOIN `p.dw.fact` f USING (order_id) JOIN `p.dw.fact` g ON total = amount"
    r = analyze("SELECT order_id FROM `p.sales.orders` JOIN `p.dw.fact` ON TRUE", schema)
    assert not r.ok
    assert "ambiguous" in r.issues[0].message and "p.dw.fact" in r.issues[0].message
    with pytest.raises(LineageError):
        analyze(sql.replace("USING (order_id)", "ON TRUE"), schema, strict=True)


def test_unknown_column_and_table(schema):
    msgs = [i.message for i in validate("SELECT nope FROM `p.sales.orders`", schema=schema)]
    assert any("`nope` not found" in m for m in msgs)
    msgs = [i.message for i in validate("SELECT x FROM `p.sales.missing`", schema=schema)]
    assert any("`p.sales.missing` not found" in m for m in msgs)


def test_syntax_errors_reported_with_position():
    issues = validate("SELECT a FROM t WHERE (a = 1")
    assert issues and issues[0].severity == "error" and issues[0].line == 1
    assert validate("SELECT 'unterminated FROM t")[0].severity == "error"
    assert validate("SELECT 1") == []


def test_ctes_nested_subqueries_and_union(schema):
    sql = """
    WITH c AS (SELECT customer_id, UPPER(name) AS nm FROM `p.sales.customers`),
         o AS (SELECT cust_id, SUM(amount) AS total FROM (SELECT * FROM `p.sales.orders`) GROUP BY cust_id)
    SELECT nm, total FROM c JOIN o ON c.customer_id = o.cust_id
    UNION ALL
    SELECT emp_name, CAST(emp_id AS FLOAT64) FROM `p.hr.employees`
    """
    assert cols(sql, schema) == {
        "<query>.nm": ["p.hr.employees.emp_name", "p.sales.customers.name"],
        "<query>.total": ["p.hr.employees.emp_id", "p.sales.orders.amount"],
    }


def test_recursive_cte(schema):
    sql = """
    WITH RECURSIVE chain AS (
      SELECT emp_id, manager_id, emp_name, 0 AS depth FROM `p.hr.employees` WHERE manager_id IS NULL
      UNION ALL
      SELECT e.emp_id, e.manager_id, e.emp_name, c.depth + 1 FROM `p.hr.employees` e JOIN chain c ON e.manager_id = c.emp_id
    )
    SELECT emp_name, depth FROM chain
    """
    c = cols(sql, schema)
    assert c["<query>.emp_name"] == ["p.hr.employees.emp_name"]
    assert c["<query>.depth"] == []  # literal seed + self reference


def test_temp_tables_resolved_through(schema):
    sql = """
    CREATE TEMP TABLE stage AS
      SELECT o.order_id, c.name AS customer_name, o.amount FROM sales.orders o JOIN sales.customers c ON o.cust_id = c.customer_id;
    CREATE TEMP TABLE stage2 AS SELECT order_id, customer_name, amount * 1.2 AS total FROM stage;
    INSERT INTO dw.fact (order_id, customer_name, total) SELECT order_id, customer_name, total FROM stage2;
    """
    r = analyze(sql, schema)
    assert r.ok, r.issues
    assert r.temp_tables == {"_SESSION.stage", "_SESSION.stage2"}
    assert r.table_lineage() == {"p.dw.fact": ["p.sales.customers", "p.sales.orders"]}
    assert r.column_lineage() == {
        "p.dw.fact.customer_name": ["p.sales.customers.name"],
        "p.dw.fact.order_id": ["p.sales.orders.order_id"],
        "p.dw.fact.total": ["p.sales.orders.amount"],
    }
    raw = r.table_lineage(resolve=False)
    assert raw["p.dw.fact"] == ["_SESSION.stage2"] and raw["_SESSION.stage2"] == ["_SESSION.stage"]


def test_temp_table_with_declared_schema_and_self_insert(schema):
    sql = """
    CREATE TEMP TABLE acc (id INT64, amt FLOAT64);
    INSERT INTO acc SELECT order_id, amount FROM sales.orders;
    INSERT INTO acc SELECT id, amt * 2 FROM acc;
    SELECT id AS k, amt FROM acc;
    """
    r = analyze(sql, schema)
    assert r.ok, r.issues
    assert r.column_lineage() == {"<query#4>.amt": ["p.sales.orders.amount"], "<query#4>.k": ["p.sales.orders.order_id"]}


def test_view_from_provider_expanded(schema):
    sql = "SELECT order_id, amount FROM sales.big_orders"
    assert cols(sql, schema)["<query>.amount"] == ["p.sales.orders.amount"]
    r = analyze(sql, schema, expand_views=False)
    assert r.column_lineage()["<query>.amount"] == ["p.sales.big_orders.amount"]
    assert r.table_lineage() == {"<query>": ["p.sales.big_orders"]}


def test_create_view_in_script(schema):
    sql = """
    CREATE OR REPLACE VIEW sales.eu AS SELECT customer_id, name FROM sales.customers WHERE region = 'EU';
    SELECT name FROM sales.eu;
    """
    r = analyze(sql, schema)
    assert r.ok, r.issues
    assert r.column_lineage() == {"<query#2>.name": ["p.sales.customers.name"],
                                  "p.sales.eu.customer_id": ["p.sales.customers.customer_id"],
                                  "p.sales.eu.name": ["p.sales.customers.name"]}


def test_merge(schema):
    sql = """
    MERGE dw.fact T
    USING (SELECT o.order_id, c.name, o.amount FROM sales.orders o JOIN sales.customers c ON o.cust_id = c.customer_id) S
    ON T.order_id = S.order_id
    WHEN MATCHED THEN UPDATE SET total = S.amount + T.total
    WHEN NOT MATCHED THEN INSERT (order_id, customer_name, total) VALUES (S.order_id, S.name, S.amount)
    WHEN NOT MATCHED BY SOURCE THEN DELETE
    """
    r = analyze(sql, schema)
    assert r.ok, r.issues
    assert r.column_lineage() == {
        "p.dw.fact.customer_name": ["p.sales.customers.name"],
        "p.dw.fact.order_id": ["p.sales.orders.order_id"],
        "p.dw.fact.total": ["p.dw.fact.total", "p.sales.orders.amount"],
    }
    assert r.table_lineage() == {"p.dw.fact": ["p.sales.customers", "p.sales.orders"]}


def test_merge_insert_row(schema):
    sql = """MERGE dw.fact T USING (SELECT order_id, 'x' AS n, amount FROM sales.orders) S ON T.order_id = S.order_id
             WHEN NOT MATCHED THEN INSERT ROW"""
    assert cols(sql, schema)["p.dw.fact.total"] == ["p.sales.orders.amount"]


def test_update_from(schema):
    sql = "UPDATE dw.fact f SET customer_name = c.name FROM sales.customers c WHERE f.order_id = c.customer_id"
    r = analyze(sql, schema)
    assert r.ok, r.issues
    assert r.column_lineage() == {"p.dw.fact.customer_name": ["p.sales.customers.name"]}
    assert r.table_lineage() == {"p.dw.fact": ["p.sales.customers"]}


def test_insert_without_column_list_uses_target_schema_and_checks_count(schema):
    ok = analyze("INSERT dw.fact SELECT order_id, 'n', amount FROM sales.orders", schema)
    assert ok.column_lineage()["p.dw.fact.total"] == ["p.sales.orders.amount"]
    bad = analyze("INSERT dw.fact SELECT order_id, amount FROM sales.orders", schema)
    assert not bad.ok and "returns 2 columns but target has 3" in bad.issues[0].message


def test_unnest_struct_and_star(schema):
    sql = """SELECT o.order_id, it.sku, address.city AS city
             FROM sales.orders o, UNNEST(o.items) AS it JOIN sales.customers ON o.cust_id = customer_id"""
    c = cols(sql, schema)
    assert c["<query>.sku"] == ["p.sales.orders.items"]
    assert c["<query>.city"] == ["p.sales.customers.address"]
    assert set(cols("SELECT * FROM `p.hr.employees`", schema)) == {
        "<query>.emp_id", "<query>.manager_id", "<query>.emp_name"}


def test_works_without_schema_for_qualified_columns():
    r = analyze("SELECT a.x, b.y FROM `p.d.a` a JOIN `p.d.b` b ON a.id = b.id")
    assert r.ok and r.column_lineage() == {"<query>.x": ["p.d.a.x"], "<query>.y": ["p.d.b.y"]}
    assert not analyze("SELECT x FROM `p.d.a` a JOIN `p.d.b` b ON a.id = b.id").ok  # no schema -> no guessing


def test_unparsed_statement_warns():
    r = analyze("DECLARE x INT64 DEFAULT 1; SELECT 1 AS one")
    assert r.ok


# ---- BigQuery provider with a mocked client (no BigQuery access needed) -----------------------

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


def test_bigquery_provider_types_views_and_not_found():
    prov = BigQuerySchemaProvider(client=mock_client())
    info = prov.get_table("p.sales.orders")
    assert info.columns == {"order_id": "INT64", "amount": "FLOAT64",
                            "items": "ARRAY<STRUCT<`sku` STRING, `qty` INT64>>"}
    assert prov.get_table("p.sales.v").view_sql.startswith("SELECT")
    assert prov.get_table("p.sales.nope") is None


def test_lineage_with_bigquery_provider_fetches_each_table_once():
    client = mock_client()
    r = analyze("SELECT oid FROM sales.v; SELECT it.qty FROM sales.orders, UNNEST(items) it; SELECT oid AS x FROM sales.v",
                BigQuerySchemaProvider(client=client))
    assert r.ok, r.issues
    assert r.column_lineage()["<query#1>.oid"] == ["p.sales.orders.order_id"]
    assert r.column_lineage()["<query#2>.qty"] == ["p.sales.orders.items"]
    assert client.get_table.call_count == 2


def test_cli(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(TABLES))
    (tmp_path / "q.sql").write_text("SELECT name FROM sales.customers JOIN sales.orders ON cust_id = customer_id")
    rc = cli.main(["lineage", str(tmp_path / "q.sql"), "--schema", str(tmp_path / "s.json"),
                   "--project", "p", "--dataset", "sales"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["columns"] == {"<query>.name": ["p.sales.customers.name"]}

    (tmp_path / "bad.sql").write_text("SELECT FROM WHERE")
    assert cli.main(["validate", str(tmp_path / "bad.sql")]) == 1

    with patch("bqlineage.cli.BigQuerySchemaProvider", lambda project: BigQuerySchemaProvider(client=mock_client())):
        rc = cli.main(["lineage", str(tmp_path / "q.sql"), "--bq", "--level", "table"])
    assert rc == 1  # customers isn't in the mocked BigQuery -> reported, not guessed
    assert "not found" in capsys.readouterr().out
