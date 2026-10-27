"""Analysis: tables, columns, statements, scripting - one script at a time."""
import pytest
from conftest import COPY_CHAIN, cols

from bqlineage import DictSchemaProvider, LineageError, analyze, validate


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
    assert r.ok and r.issues[0].severity == "warning" and r.column_lineage() == {"<query>.x": []}
    assert r.column_details()["<query>.x"]["unresolved"] == ["x"]


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


def test_run_time_dynamic_sql_and_calls_warn():
    # SQL read from a table is only known when the script runs (literal SQL is analyzed: see test_variables.py)
    r = analyze("DECLARE q STRING;\nSET q = (SELECT MAX(text) FROM `p.d.queries`);\nEXECUTE IMMEDIATE q;\n"
                "CALL ds.proc();\nSELECT 1 AS one")
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


def test_column_details_show_how_and_where(schema):
    sql = """
    DECLARE run_date DATE DEFAULT CURRENT_DATE();
    CREATE TEMP TABLE paid AS
      SELECT o.order_id, o.cust_id, o.amount * 1.2 AS gross FROM sales.orders o
      WHERE DATE(o.order_ts) = run_date AND o.order_id NOT IN (SELECT e.emp_id FROM hr.employees e);
    INSERT INTO dw.fact (order_id, customer_name, total)
    WITH big AS (SELECT * FROM paid WHERE gross > 100)
    SELECT big.order_id, UPPER(c.name), SUM(big.gross)
    FROM big JOIN sales.customers c ON big.cust_id = c.customer_id
    WHERE c.region = 'EU'
    GROUP BY big.order_id, c.name;
    """
    r = analyze(sql, schema)
    assert r.ok and not r.issues, r.issues
    d = r.column_details(table="fact")
    total, name = d["p.dw.fact.total"], d["p.dw.fact.customer_name"]
    assert total["sources"] == ["p.sales.orders.amount"] and name["sources"] == ["p.sales.customers.name"]
    # everything that decides which rows/groups: join keys, both WHEREs, the CTE filter, the NOT IN subquery
    assert total["indirect_sources"] == name["indirect_sources"] == [
        "p.hr.employees.emp_id", "p.sales.customers.customer_id", "p.sales.customers.name", "p.sales.customers.region",
        "p.sales.orders.amount", "p.sales.orders.cust_id", "p.sales.orders.order_id", "p.sales.orders.order_ts"]
    assert total["steps"] == [
        {"column": "p.dw.fact.total", "expression": "SUM(big.gross)", "from": ["big", "p.sales.customers AS c"],
         "joins": ["JOIN c ON big.cust_id = c.customer_id"], "where": "c.region = 'EU'",
         "group_by": ["big.order_id", "c.name"]},
        {"column": "big.gross", "in": "p.dw.fact", "expression": "paid.gross", "from": ["_SESSION.paid"],
         "where": "paid.gross > 100"},
        {"column": "_SESSION.paid.gross", "expression": "o.amount * 1.2", "from": ["p.sales.orders AS o"],
         "where": "DATE(o.order_ts) = @run_date AND NOT o.order_id IN "
                  "(SELECT e.emp_id AS emp_id FROM p.hr.employees AS e)"},
    ]
    assert name["steps"][0]["expression"] == "UPPER(c.name)"
    assert r.column_lineage(table="fact") == {k: v["sources"] for k, v in d.items()}


def test_merge_and_update_conditions_are_in_the_steps(schema):
    r = analyze("MERGE dw.fact T USING sales.orders S ON T.order_id = S.order_id "
                "WHEN MATCHED AND S.amount > 0 THEN UPDATE SET total = S.amount", schema)
    step = r.column_details()["p.dw.fact.total"]["steps"][0]
    # aliases and columns are case-insensitive in BigQuery; step text shows them normalized to lower case
    assert step["joins"] == ["JOIN s ON t.order_id = s.order_id"] and step["where"] == "s.amount > 0"
    r = analyze("UPDATE dw.fact f SET total = o.amount FROM sales.orders o WHERE f.order_id = o.order_id", schema)
    d = r.column_details()["p.dw.fact.total"]
    assert d["steps"][0]["where"] == "f.order_id = o.order_id"
    assert d["indirect_sources"] == ["p.dw.fact.order_id", "p.sales.orders.order_id"]


USING_TABLES = {
    "p.d.t1": {"k1": "INT64", "k2": "STRING", "k3": "DATE", "a": "FLOAT64", "col77": "STRING", "load_ts": "TIMESTAMP"},
    "p.d.t2": {"k1": "INT64", "k2": "STRING", "k3": "DATE", "b": "STRING", "col88": "STRING", "load_ts": "TIMESTAMP"},
    "p.d.t3": {"k1": "INT64", "k2": "STRING", "c": "INT64"},
    "p.d.out": {"k1": "INT64", "k2": "STRING", "k3": "DATE", "a": "FLOAT64", "c": "INT64"},
}


USING_CTES = """
WITH cte1 AS (SELECT * FROM `p.d.t1` WHERE a > 0),
     cte2 AS (SELECT * FROM `p.d.t2`),
     cte3 AS (SELECT * FROM `p.d.t3`),
"""


def test_using_join_ctes_with_qualified_star_except():
    # c1.* EXCEPT (...), c2.* EXCEPT (...) over a USING join; load_ts survives both EXCEPTs, so j repeats it.
    # BigQuery allows that as long as load_ts isn't referenced; sqlglot used to stop expanding j.* -> no lineage.
    sql = "INSERT INTO `p.d.out`" + USING_CTES + """
         j AS (SELECT c1.* EXCEPT (col77), c2.* EXCEPT (col88, b)
               FROM cte1 c1 LEFT JOIN cte2 c2 USING (k1, k2, k3)),
         j2 AS (SELECT j.*, c3.c FROM j LEFT JOIN cte3 c3 USING (k1, k2))
    SELECT k1, k2, k3, a, c FROM j2"""
    r = analyze(sql, DictSchemaProvider(USING_TABLES))
    assert r.ok and not r.issues, r.issues
    assert r.column_lineage() == {
        "p.d.out.a": ["p.d.t1.a"],
        "p.d.out.c": ["p.d.t3.c"],
        "p.d.out.k1": ["p.d.t1.k1", "p.d.t2.k1", "p.d.t3.k1"],  # USING keys merge both sides
        "p.d.out.k2": ["p.d.t1.k2", "p.d.t2.k2", "p.d.t3.k2"],
        "p.d.out.k3": ["p.d.t1.k3", "p.d.t2.k3"],
    }
    assert "p.d.t1.a" in r.column_details()["p.d.out.k1"]["indirect_sources"]  # cte1's WHERE a > 0


def test_select_star_over_cte_with_repeated_names():
    # was a false "Column `k1` is ambiguous" error and no lineage for the whole statement
    sql = USING_CTES + """
         j AS (SELECT * FROM cte1 c1 LEFT JOIN cte2 c2 USING (k1, k2, k3)),
         j2 AS (SELECT * FROM j)
    SELECT k1, a, b FROM j2"""
    r = analyze(sql, DictSchemaProvider(USING_TABLES))
    assert r.ok and not r.issues, r.issues
    assert r.column_lineage() == {"<query>.a": ["p.d.t1.a"], "<query>.b": ["p.d.t2.b"],
                                  "<query>.k1": ["p.d.t1.k1", "p.d.t2.k1"]}


def test_temp_table_from_parenthesized_cte_query_gets_its_schema():
    # CREATE ... AS ( WITH ... ) used to lose every CTE column, so the temp table got no schema and each
    # statement reading it failed too
    sql = """
    CREATE TEMP TABLE stage AS (
      WITH c1 AS (SELECT * FROM `p.d.t1`), c2 AS (SELECT * FROM `p.d.t2`),
           j AS (SELECT * FROM c1 LEFT JOIN c2 USING (k1, k2, k3))  -- load_ts, from both sides
      SELECT * FROM j
    );
    INSERT INTO `p.d.out` (k1, a) (SELECT k1, a FROM stage);
    """
    r = analyze(sql, DictSchemaProvider(USING_TABLES))
    assert r.ok and not r.issues, r.issues
    assert r.column_lineage() == {"p.d.out.a": ["p.d.t1.a"], "p.d.out.k1": ["p.d.t1.k1", "p.d.t2.k1"]}


def test_missing_schema_keeps_lineage_of_the_other_columns():
    partial = DictSchemaProvider({k: v for k, v in USING_TABLES.items() if k != "p.d.t2"})
    r = analyze("WITH cte1 AS (SELECT k1, k2, k3, a FROM `p.d.t1`), cte2 AS (SELECT k1, k2, k3, b FROM `p.d.t2`)\n"
                "SELECT k1, a, b FROM cte1 c1 LEFT JOIN cte2 c2 USING (k1, k2, k3)", partial)
    assert [i.severity for i in r.issues] == ["error", "warning"]  # t2 not found; then why b can't be traced
    assert "schema unknown for p.d.t2" in r.issues[1].message
    d = r.column_details()
    assert d["<query>.a"]["sources"] == ["p.d.t1.a"] and d["<query>.k1"]["sources"] == ["p.d.t1.k1"]
    assert d["<query>.b"]["sources"] == [] and d["<query>.b"]["unresolved"]
    # SELECT * CTEs over the missing table: a warning, not "Cannot automatically join"
    r = analyze(USING_CTES + " j AS (SELECT * FROM cte1 c1 LEFT JOIN cte2 c2 USING (k1, k2, k3))\nSELECT * FROM j", partial)
    assert all("Cannot automatically join" not in i.message for i in r.issues), r.issues


def test_one_to_one_hops_are_compacted(schema):
    r = analyze(COPY_CHAIN, schema)
    assert r.ok, r.issues
    total = r.column_details()["p.dw.fact.total"]
    # the target's own step and the step that does work stay; copies through c1 and the temp table go
    assert [(s["column"], s["expression"]) for s in total["steps"]] == [
        ("p.dw.fact.total", "c2.amt"), ("c2.amt", "c1.amount * 2")]
    assert total["sources"] == ["p.sales.orders.amount"]
    # a copy that filters is not one-to-one in rows: it stays
    assert [s["column"] for s in r.column_details()["p.dw.fact.order_id"]["steps"]] == [
        "p.dw.fact.order_id", "c2.order_id"]
    # raw keeps every hop
    raw = r.column_details(resolve=False)["p.dw.fact.total"]["steps"]
    assert [s["column"] for s in raw] == ["p.dw.fact.total", "c2.amt", "c1.amount"]
    assert r.column_details(resolve=False)["_SESSION.stage.amount"]["steps"][0]["expression"] == "orders.amount"


def test_wide_tables_stay_fast():
    import time
    cols_ = {f"c{i}": "INT64" for i in range(200)}
    s = DictSchemaProvider({"p.d.wide": cols_, "p.d.out": cols_})
    ctes = ", ".join(f"s{i} AS (SELECT * FROM {'`p.d.wide`' if i == 0 else f's{i - 1}'})" for i in range(4))
    t0 = time.time()
    r = analyze(f"INSERT INTO `p.d.out` WITH {ctes} SELECT * FROM s3", s)
    assert r.ok and r.column_lineage()["p.d.out.c199"] == ["p.d.wide.c199"]
    assert time.time() - t0 < 5  # was quadratic (~20s+) when sqlglot trimmed selects per column


def test_procedure_body_is_analyzed_with_its_parameters_as_values(schema):
    sql = """
    CREATE OR REPLACE PROCEDURE dw.load_fact(IN min_amount FLOAT64, OUT loaded STRUCT<n INT64, at TIMESTAMP>)
    OPTIONS (description = 'nightly')
    BEGIN
      INSERT INTO dw.fact (order_id, total) SELECT order_id, amount FROM sales.orders WHERE amount > min_amount;
      SET loaded = (SELECT AS STRUCT 1, CURRENT_TIMESTAMP());
    END;
    """
    r = analyze(sql, schema)
    assert r.ok and not r.issues, r.issues  # min_amount is a parameter, not an unknown column
    assert r.column_lineage() == {"p.dw.fact.order_id": ["p.sales.orders.order_id"],
                                  "p.dw.fact.total": ["p.sales.orders.amount"]}
