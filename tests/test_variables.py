"""Template placeholders, script constants and the dynamic SQL built from them."""
from conftest import cols

from bqlineage import analyze
from bqlineage.render import render


def test_render_fills_jinja_and_shell_placeholders():
    sql, missing, logic = render("SELECT {{ x }}, '{{ params.day }}', ${env}, {{ ds_add(ds, 1) }} FROM `${env}.t`",
                                 {"x": "1", "day": "2026-10-27", "env": "prod"})
    assert sql == "SELECT 1, '2026-10-27', prod, __ds_add_ds_1__ FROM `prod.t`"  # params.day found as `day`
    assert missing == ["ds_add(ds, 1)"] and not logic


def test_render_leaves_javascript_alone_and_keeps_lines():
    js = 'CREATE TEMP FUNCTION f(x STRING) RETURNS STRING LANGUAGE js AS r"""return `${x}!`;""";'
    assert render(js) == (js, [], False)  # ${x} in a JS body is JavaScript, not a template
    sql, _, logic = render("SELECT a\n{% if full %}\n, b\n{% endif %}\nFROM t")
    assert logic and sql.count("\n") == 4  # logic removed (not evaluated), line numbers kept


def test_templated_names_resolve_and_missing_values_warn(schema):
    sql = ("INSERT INTO `{{ params.project }}.dw.fact` (order_id) "
           "SELECT order_id FROM `${project}.sales.orders` WHERE order_id > {{ min_id }}")
    r = analyze(sql, schema, vars={"project": "p"})
    assert r.column_lineage() == {"p.dw.fact.order_id": ["p.sales.orders.order_id"]}
    # the unfilled placeholder is one warning, not a "column not found" error
    assert r.ok and [i.message.split(":")[0] for i in r.issues] == ["Template placeholder `min_id` has no value"]
    assert "__min_id__" in r.sql


def test_execute_immediate_built_from_constants(schema):
    sql = """
    DECLARE target STRING DEFAULT 'dw.fact';
    DECLARE src STRING DEFAULT CONCAT('sales', '.', 'orders');
    DECLARE factor INT64 DEFAULT 2;
    EXECUTE IMMEDIATE FORMAT('INSERT INTO `%s` (order_id, total) SELECT order_id, amount * %d FROM `%s`',
                             target, factor, src);
    SET target = 'hr.employees';
    EXECUTE IMMEDIATE 'INSERT INTO ' || target || ' (emp_id) SELECT order_id FROM sales.orders' USING 1 AS unused;
    """
    assert cols(sql, schema) == {"p.dw.fact.order_id": ["p.sales.orders.order_id"],
                                 "p.dw.fact.total": ["p.sales.orders.amount"],
                                 "p.hr.employees.emp_id": ["p.sales.orders.order_id"]}


def test_dynamic_sql_problems_point_at_the_execute(schema):
    r = analyze("SELECT 1;\nEXECUTE IMMEDIATE 'SELECT nope FROM sales.orders';\n"
                "DECLARE q STRING;\nSET q = (SELECT 'SELECT 1');\nEXECUTE IMMEDIATE q", schema)
    err, warn = r.issues
    assert (err.severity, err.statement, err.line) == ("error", 2, 2)
    assert err.message.startswith("in EXECUTE IMMEDIATE: Column `nope` not found")
    assert (warn.statement, warn.message) == (5, "EXECUTE IMMEDIATE not analyzed: its SQL is only known at run time")


def test_format_specifiers():
    from bqlineage.analyzer import _format
    assert _format("%s|%d|%t|%T|%T|%%", ["a", "1", "b", "it's", "3"]) == "a|1|b|'it\\'s'|3|%"
    assert _format("%s %s", ["only one"]) is None
