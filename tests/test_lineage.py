import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import Forbidden, NotFound

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
    # no schema: can't tell which table owns x -> a warning (the query may be fine), and nothing guessed
    r = analyze("SELECT x FROM `p.d.a` a JOIN `p.d.b` b ON a.id = b.id")
    assert r.ok and r.issues[0].severity == "warning" and r.column_lineage() == {}


def test_comments_and_statement_numbering(schema):
    sql = """-- header
    /* block
       comment */
    INSERT INTO sales.orders (order_id)  -- target
    SELECT /* inline */ emp_id FROM hr.employees;  -- trailing comment
    /* next */
    SELECT nope FROM hr.employees;
    -- end of script
    """
    r = analyze(sql, schema)
    assert r.column_lineage()["p.sales.orders.order_id"] == ["p.hr.employees.emp_id"]
    assert [i.statement for i in r.issues] == [2]


def test_no_schema_source_is_named_in_errors():
    r = analyze("INSERT d.t SELECT x FROM `p.d.a` a JOIN `p.d.b` b ON a.id = b.id")
    assert all("no schema source given" in i.message for i in r.issues) and len(r.issues) == 2


def test_dynamic_sql_and_calls_warn():
    r = analyze("EXECUTE IMMEDIATE 'SELECT 1';\nCALL ds.proc();\nSELECT 1 AS one")
    assert r.ok and [i.message.split()[0] for i in r.issues] == ["EXECUTE", "CALL"]


def test_script_and_loop_variables_are_not_columns(schema):
    sql = """
    DECLARE run_date DATE DEFAULT CURRENT_DATE();
    DECLARE cfg STRUCT<min_amt FLOAT64>;
    INSERT INTO dw.fact (order_id, total)
    SELECT order_id, amount FROM sales.orders WHERE DATE(order_ts) = run_date AND amount > cfg.min_amt;
    FOR rec IN (SELECT customer_id FROM sales.customers) DO
      INSERT INTO dw.fact (order_id) SELECT order_id FROM sales.orders WHERE cust_id = rec.customer_id;
    END FOR;
    """
    r = analyze(sql, schema)
    assert r.ok and not r.issues, r.issues
    assert r.column_lineage() == {"p.dw.fact.order_id": ["p.sales.orders.order_id"],
                                  "p.dw.fact.total": ["p.sales.orders.amount"]}


def test_column_wins_over_variable_of_same_name(schema):
    assert cols("DECLARE amount FLOAT64 DEFAULT 0;\nSELECT amount FROM sales.orders", schema) == {
        "<query#2>.amount": ["p.sales.orders.amount"]}


def test_procedural_blocks_are_analyzed(schema):
    sql = """
    BEGIN
      DECLARE x INT64 DEFAULT 1;
      CREATE TEMP TABLE s AS SELECT order_id, amount FROM sales.orders;
      IF x > 0 THEN
        INSERT INTO dw.fact (order_id) SELECT order_id FROM s;
      ELSEIF x < 0 THEN
        INSERT INTO dw.fact (total) SELECT amount FROM s;
      ELSE
        SELECT 1;
      END IF;
      outer_loop: LOOP
        SET x = x + 1;
        IF x > 3 THEN LEAVE outer_loop; END IF;
      END LOOP;
      WHILE x < 5 DO
        UPDATE dw.fact SET customer_name = (SELECT MAX(name) FROM sales.customers) WHERE TRUE;
        SET x = x + 1;
      END WHILE;
      REPEAT
        SET x = x - 1;
      UNTIL x <= 0 END REPEAT;
      CASE x
        WHEN 0 THEN MERGE dw.fact T USING s ON T.order_id = s.order_id WHEN NOT MATCHED THEN INSERT (order_id) VALUES (s.order_id);
        ELSE SELECT 2;
      END CASE;
    EXCEPTION WHEN ERROR THEN
      SELECT @@error.message;
    END;
    """
    r = analyze(sql, schema)
    assert r.ok and not r.issues, r.issues
    assert r.column_lineage(table="dw.fact") == {
        "p.dw.fact.customer_name": ["p.sales.customers.name"],
        "p.dw.fact.order_id": ["p.sales.orders.order_id"],
        "p.dw.fact.total": ["p.sales.orders.amount"],
    }


def test_syntax_error_in_one_statement_keeps_the_rest(schema):
    r = analyze("INSERT INTO dw.fact (order_id) SELECT order_id FROM sales.orders;\nSELECT FROM WHERE;\n"
                "INSERT INTO dw.fact (total) SELECT amount FROM sales.orders;", schema)
    assert [(i.severity, i.statement, i.line) for i in r.issues] == [("error", 2, 2)]
    assert "<Token" not in r.issues[0].message
    assert set(r.column_lineage()) == {"p.dw.fact.order_id", "p.dw.fact.total"}


def test_windows_bom_and_crlf(schema):
    r = analyze("﻿-- header\r\nSELECT order_id\r\nFROM sales.orders\r\nWHERE nope = 1;\r\n", schema)
    assert [(i.severity, i.line) for i in r.issues] == [("error", 4)] and "nope" in r.issues[0].message


def test_set_default_dataset(schema):
    assert cols("SET @@dataset_id = 'sales';\nSELECT order_id FROM orders", schema) == {
        "<query#2>.order_id": ["p.sales.orders.order_id"]}


def test_recreated_temp_table_keeps_each_version_apart(schema):
    sql = """
    CREATE TEMP TABLE t AS SELECT order_id AS k, amount FROM sales.orders;
    INSERT INTO dw.fact (order_id) SELECT k FROM t;
    CREATE OR REPLACE TEMP TABLE t AS SELECT k, amount * 2 AS amount FROM t WHERE k > 0;  -- refines the old t
    INSERT INTO dw.fact (total) SELECT amount FROM t;
    CREATE OR REPLACE TEMP TABLE t AS SELECT customer_id AS k FROM sales.customers;
    INSERT INTO hr.employees (emp_id) SELECT k FROM t;
    """
    r = analyze(sql, schema)
    assert r.ok, r.issues
    assert r.column_lineage() == {
        "p.dw.fact.order_id": ["p.sales.orders.order_id"],
        "p.dw.fact.total": ["p.sales.orders.amount"],
        "p.hr.employees.emp_id": ["p.sales.customers.customer_id"],
    }
    assert r.temp_tables == {"_SESSION.t", "_SESSION.t#2", "_SESSION.t#3"}


def test_clone_copy_like(schema):
    r = analyze("CREATE TABLE dw.c1 CLONE hr.employees;\nCREATE TABLE dw.c2 LIKE hr.employees;\n"
                "SELECT emp_name FROM dw.c1;\nSELECT emp_name FROM dw.c2;", schema)
    assert r.ok, r.issues
    c = r.column_lineage()
    assert c["p.dw.c1.emp_name"] == ["p.hr.employees.emp_name"]
    assert c["<query#3>.emp_name"] == ["p.dw.c1.emp_name"]
    assert c["<query#4>.emp_name"] == ["p.dw.c2.emp_name"]  # LIKE copies the schema, not the data
    assert "p.dw.c2" not in r.table_lineage()


def test_merge_insert_values_without_column_list(schema):
    sql = ("MERGE dw.fact T USING sales.orders S ON T.order_id = S.order_id "
           "WHEN NOT MATCHED THEN INSERT VALUES (S.order_id, 'x', S.amount)")
    assert cols(sql, schema) == {"p.dw.fact.customer_name": [], "p.dw.fact.order_id": ["p.sales.orders.order_id"],
                                 "p.dw.fact.total": ["p.sales.orders.amount"]}
    r = analyze("MERGE `p.d.x` T USING `p.d.o` S ON T.id = S.id WHEN NOT MATCHED THEN INSERT VALUES (S.id)")
    assert r.ok and "INSERT without column list not mapped" in r.issues[0].message


def test_information_schema_and_export(schema):
    r = analyze("SELECT table_name FROM sales.INFORMATION_SCHEMA.TABLES", schema)
    assert r.ok and r.table_lineage() == {"<query>": ["p.sales.INFORMATION_SCHEMA.TABLES"]}
    r = analyze("EXPORT DATA OPTIONS (uri = 'gs://b/o-*.csv', format = 'CSV') AS SELECT order_id FROM sales.orders", schema)
    assert r.column_lineage() == {"gs://b/o-*.csv.order_id": ["p.sales.orders.order_id"]}


def test_names_keep_their_case_and_ctes_are_case_insensitive():
    s = DictSchemaProvider({"p.d.Customers": {"CustomerID": "INT64", "FullName": "STRING"}})
    assert cols("WITH Recent AS (SELECT * FROM `p.d.Customers`) SELECT customerid, FULLNAME AS DisplayName FROM recent", s) == {
        "<query>.DisplayName": ["p.d.Customers.FullName"], "<query>.customerid": ["p.d.Customers.CustomerID"]}


def test_unknown_alias_message(schema):
    issues = validate("SELECT x.order_id FROM sales.orders o", schema=schema)
    assert issues[0].severity == "error" and "`x` is not a table alias" in issues[0].message


def test_star_over_unknown_schema_warns_instead_of_count_error():
    r = analyze("INSERT INTO `p.d.t` (a, b) SELECT * FROM `p.d.src`;\n"
                "CREATE TEMP TABLE x AS SELECT *, 1 AS one FROM `p.d.src`;\nSELECT a FROM x")
    assert r.ok and r.issues, r.issues


def test_offline_schema_keys_may_omit_project():
    s = DictSchemaProvider({"sales.orders": {"order_id": "INT64"}})
    assert cols("SELECT order_id FROM sales.orders", s, project="p") == {"<query>.order_id": ["p.sales.orders.order_id"]}


def test_filter_by_table_and_columns(schema):
    r = analyze("INSERT INTO dw.fact (order_id, total) SELECT order_id, amount FROM sales.orders;\n"
                "CREATE TABLE dw.other AS SELECT customer_id, name FROM sales.customers;", schema)
    assert r.targets() == ["p.dw.fact", "p.dw.other"]
    for t in ("p.dw.fact", "dw.fact", "fact"):
        assert r.table_lineage(table=t) == {"p.dw.fact": ["p.sales.orders"]}
    assert r.column_lineage(table="fact", columns=["TOTAL"]) == {"p.dw.fact.total": ["p.sales.orders.amount"]}
    assert set(r.column_lineage(columns=["order_id", "name"])) == {"p.dw.fact.order_id", "p.dw.other.name"}
    assert r.table_lineage(table="act") == {}  # matches whole name parts only


def test_wide_tables_stay_fast():
    import time
    cols_ = {f"c{i}": "INT64" for i in range(200)}
    s = DictSchemaProvider({"p.d.wide": cols_, "p.d.out": cols_})
    ctes = ", ".join(f"s{i} AS (SELECT * FROM {'`p.d.wide`' if i == 0 else f's{i - 1}'})" for i in range(4))
    t0 = time.time()
    r = analyze(f"INSERT INTO `p.d.out` WITH {ctes} SELECT * FROM s3", s)
    assert r.ok and r.column_lineage()["p.d.out.c199"] == ["p.d.wide.c199"]
    assert time.time() - t0 < 5  # was quadratic (~20s+) when sqlglot trimmed selects per column


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


def test_permission_denied_is_reported_not_raised():
    client = mock_client()
    original = client.get_table.side_effect

    def get_table(fqn):
        if "secret" in str(fqn):
            raise Forbidden("Access Denied: Table p:secret.t")
        return original(fqn)

    client.get_table.side_effect = get_table
    r = analyze("SELECT o.order_id, s.x FROM sales.orders o JOIN secret.t s ON TRUE", BigQuerySchemaProvider(client=client))
    assert r.issues[0].severity == "error" and "Access Denied" in r.issues[0].message
    assert r.column_lineage() == {"<query>.order_id": ["p.sales.orders.order_id"], "<query>.x": ["p.secret.t.x"]}


def test_wildcard_table_uses_newest_shard_schema():
    client = mock_client()
    client.list_tables.return_value = [
        SimpleNamespace(table_id="events_20240101", created=datetime(2024, 1, 2), reference="p.sales.events_20240101"),
        SimpleNamespace(table_id="events_20240102", created=datetime(2024, 1, 3), reference="p.sales.events_20240102"),
        SimpleNamespace(table_id="orders", created=datetime(2025, 1, 1), reference="p.sales.orders"),
    ]
    shard = SimpleNamespace(table_type="TABLE", view_query=None, schema=[field("event_name", "STRING")])
    original = client.get_table.side_effect
    client.get_table.side_effect = lambda ref: shard if ref == "p.sales.events_20240102" else original(ref)
    r = analyze("SELECT event_name, _TABLE_SUFFIX AS sfx FROM `p.sales.events_*` WHERE _TABLE_SUFFIX > '2024'",
                BigQuerySchemaProvider(client=client))
    assert r.ok, r.issues
    assert r.column_lineage() == {"<query>.event_name": ["p.sales.events_*.event_name"],
                                  "<query>.sfx": ["p.sales.events_*._TABLE_SUFFIX"]}


def test_cli_table_and_column_filter(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(TABLES))
    (tmp_path / "q.sql").write_text("INSERT INTO dw.fact (order_id, total) SELECT order_id, amount FROM sales.orders;\n"
                                    "CREATE TABLE dw.other AS SELECT customer_id FROM sales.customers;")
    base = ["lineage", str(tmp_path / "q.sql"), "--schema", str(tmp_path / "s.json"), "--project", "p"]
    assert cli.main(base + ["--table", "dw.fact", "--columns", "total"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tables"] == {"p.dw.fact": ["p.sales.orders"]}
    assert out["columns"] == {"p.dw.fact.total": ["p.sales.orders.amount"]}
    assert cli.main(base + ["--columns", "order_id,customer_id", "--level", "column"]) == 0
    assert set(json.loads(capsys.readouterr().out)["columns"]) == {"p.dw.fact.order_id", "p.dw.other.customer_id"}
    with pytest.raises(SystemExit, match="Targets: p.dw.fact, p.dw.other"):
        cli.main(base + ["--table", "nope"])
    with pytest.raises(SystemExit, match="None of the columns"):
        cli.main(base + ["--table", "fact", "--columns", "nope"])


def test_cli_bq_without_credentials_exits_cleanly(tmp_path):
    (tmp_path / "q.sql").write_text("SELECT 1")

    def no_credentials(project):
        raise RuntimeError("Your default credentials were not found")

    with patch("bqlineage.cli.BigQuerySchemaProvider", no_credentials), pytest.raises(SystemExit, match="bqlineage auth"):
        cli.main(["lineage", str(tmp_path / "q.sql"), "--bq"])
