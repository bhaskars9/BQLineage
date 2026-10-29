"""draw.io and OpenLineage exports."""
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from bqlineage import DictSchemaProvider, analyze, cli


def _pages(xml: str) -> dict:
    from bqlineage.drawio import read_page
    return {d.get("name"): read_page(d) for d in ET.fromstring(xml).iter("diagram")}


def _cells(page) -> dict:
    """cell id -> (label, mxCell), for boxes and rows (a cell with a tooltip is wrapped in a UserObject)"""
    out = {}
    for c in page.iter():
        if c.tag == "UserObject":
            out[c.get("id")] = (c.get("label"), c[0])
        elif c.tag == "mxCell" and c.get("id") not in (None, "0", "1"):
            out[c.get("id")] = (c.get("value"), c)
    return out


def boxes(page) -> dict:
    """box label -> its kind, read back from its style (the legend is left out)"""
    from bqlineage.drawio import KINDS, LEGEND
    return {label: next(k for k, (style, _) in KINDS.items() if style in c.get("style"))
            for label, c in _cells(page).values()
            if c.get("vertex") and c.get("parent") == "1" and not c.get("style").startswith(LEGEND)}


def legend(page) -> set:
    from bqlineage.drawio import LEGEND
    return {label for label, c in _cells(page).values() if (c.get("style") or "").startswith(LEGEND)}


def arrows(page) -> set:
    """(source, target) labels; a row is box.column"""
    cells = _cells(page)

    def name(cid):
        label, c = cells[cid]
        return label if c.get("parent") == "1" else f"{cells[c.get('parent')][0]}.{label}"

    return {(name(c.get("source")), name(c.get("target"))) for _, c in cells.values() if c.get("edge")}


def test_drawio_export_of_the_example(tmp_path, capsys):
    from pathlib import Path

    from bqlineage.drawio import read_page, to_drawio

    ex = Path(__file__).parent.parent / "examples"
    out = tmp_path / "orders.drawio"
    assert cli.main(["lineage", str(ex / "orders.sql"), "--schema", str(ex / "schema.json"), "--drawio", str(out)]) == 0
    capsys.readouterr()
    diagrams = list(ET.parse(out).getroot().iter("diagram"))
    assert all(d.text and not len(d) for d in diagrams)  # stored compressed, as draw.io saves pages
    pages = {d.get("name"): read_page(d) for d in diagrams}
    assert list(pages) == ["Tables", "Columns"]

    def labels(page):
        return {c.get("value") or c.get("label") for c in page.iter() if c.tag in ("mxCell", "UserObject")} - {None}

    # sources -> the temp table and the CTE -> the target
    assert boxes(pages["Tables"]) == {"shop.sales.orders": "source", "shop.crm.customers": "source",
                                      "_SESSION.paid_orders": "temp", "eu_customers": "cte",
                                      "shop.dw.customer_revenue": "target"}
    assert arrows(pages["Tables"]) == {("shop.sales.orders", "_SESSION.paid_orders"),
                                       ("_SESSION.paid_orders", "shop.dw.customer_revenue"),
                                       ("shop.crm.customers", "eu_customers"),
                                       ("eu_customers", "shop.dw.customer_revenue")}
    assert legend(pages["Tables"]) == {"source table", "temp table", "CTE", "target"}
    assert {"amount", "gross", "revenue", "loaded_at"} <= labels(pages["Columns"])
    revenue = next(u for u in pages["Columns"].iter("UserObject") if u.get("label") == "revenue")
    assert "SUM(p.gross)" in revenue.get("tooltip") and "o.status = 'PAID'" in revenue.get("tooltip")
    gross = next(u for u in pages["Columns"].iter("UserObject") if u.get("label") == "gross")
    assert gross.get("tooltip").startswith("gross = o.amount * 1.2")  # an intermediate column: its own step
    assert len(arrows(pages["Columns"])) == 10  # one per hop
    assert {("shop.sales.orders.amount", "_SESSION.paid_orders.gross"),
            ("_SESSION.paid_orders.gross", "shop.dw.customer_revenue.revenue"),
            ("shop.crm.customers.name", "eu_customers.name"),
            ("eu_customers.name", "shop.dw.customer_revenue.customer_name")} <= arrows(pages["Columns"])

    plain = _pages(to_drawio(analyze("SELECT a.x FROM `p.d.a` a"), "table"))["Tables"]
    assert "<query>" in labels(plain)  # labels are plain text, not HTML
    # control characters (illegal in XML) from pasted SQL can't break the file
    odd = to_drawio(analyze("INSERT INTO `p.d.out` (k) SELECT CONCAT(a, '\x0c\x01') FROM `p.d.a`"), "column")
    ET.fromstring(odd)
    _pages(odd)
    # rows keep the target's column order, not alphabetical
    target = next(u for u in pages["Columns"].iter("mxCell") if u.get("value") == "shop.dw.customer_revenue")
    rows = [u.get("label") for u in pages["Columns"].iter("UserObject") if u[0].get("parent") == target.get("id")]
    assert rows == ["customer_id", "customer_name", "region", "revenue", "loaded_at"]


STEPS_SCHEMA = {
    "p.d.a": {"id": "INT64", "x": "INT64"}, "p.d.b": {"id": "INT64", "y": "INT64"}, "p.d.blocked": {"id": "INT64"},
    "p.d.out": {"id": "INT64", "v": "INT64"},
    "p.d.a_v": {"columns": {"id": "INT64", "x": "INT64"}, "view_sql": "SELECT id, x FROM `p.d.a` WHERE x > 0"},
}
STEPS_SQL = """
CREATE TEMP TABLE t AS SELECT id, x * 2 AS x FROM `p.d.a_v` WHERE id NOT IN (SELECT id FROM `p.d.blocked`);
INSERT INTO `p.d.out` (id, v)
WITH copy AS (SELECT * FROM t),                                     -- only copies: left out unless --raw
     u AS (SELECT id, x FROM copy UNION ALL SELECT id, y FROM `p.d.b`)
SELECT s.id, s.v FROM (SELECT id, SUM(x) AS v FROM u GROUP BY id) s
"""


def test_drawio_draws_every_intermediate_step_in_its_own_style():
    from bqlineage.drawio import to_drawio

    r = analyze(STEPS_SQL, DictSchemaProvider(STEPS_SCHEMA))
    assert r.ok, r.issues
    pages = _pages(to_drawio(r))
    assert boxes(pages["Tables"]) == {"p.d.a": "source", "p.d.blocked": "source", "p.d.b": "source",
                                      "p.d.a_v": "view", "_SESSION.t": "temp", "u": "cte",
                                      "(subquery) s": "subquery", "p.d.out": "target"}
    assert arrows(pages["Tables"]) == {
        ("p.d.a", "p.d.a_v"), ("p.d.a_v", "_SESSION.t"),
        ("p.d.blocked", "_SESSION.t"),  # only filters (WHERE ... IN): drawn to the step that reads it
        ("_SESSION.t", "u"), ("p.d.b", "u"),  # through `copy`, which only copies
        ("u", "(subquery) s"), ("(subquery) s", "p.d.out")}
    assert legend(pages["Tables"]) == {"source table", "view", "temp table", "CTE", "subquery", "target"}
    assert arrows(pages["Columns"]) == {
        ("p.d.a.id", "p.d.a_v.id"), ("p.d.a.x", "p.d.a_v.x"),
        ("p.d.a_v.id", "_SESSION.t.id"), ("p.d.a_v.x", "_SESSION.t.x"),
        ("_SESSION.t.id", "u.id"), ("_SESSION.t.x", "u.x"), ("p.d.b.id", "u.id"), ("p.d.b.y", "u.x"),
        ("u.id", "(subquery) s.id"), ("u.x", "(subquery) s.v"),
        ("(subquery) s.id", "p.d.out.id"), ("(subquery) s.v", "p.d.out.v")}
    tips = {(u.get("label"), u.get("tooltip").split("\n")[0]) for u in pages["Columns"].iter("UserObject")}
    assert ("u", "CTE in the query that writes p.d.out") in tips  # a box: where it lives
    assert ("x", "x = a_v.x * 2") in tips  # a row of the temp table: its own step
    union = next(t for label, t in ((u.get("label"), u.get("tooltip")) for u in pages["Columns"].iter("UserObject"))
                 if label == "x" and "b.y" in t)
    assert union == "x = copy.x\nx = b.y"  # one line per UNION branch
    # --raw: every hop, including the CTE that only copies
    raw = _pages(to_drawio(r, resolve=False))
    assert boxes(raw["Tables"])["copy"] == "cte"
    assert {("_SESSION.t", "copy"), ("copy", "u")} <= arrows(raw["Tables"])
    assert ("_SESSION.t.x", "copy.x") in arrows(raw["Columns"])


def test_drawio_steps_follow_the_table_and_columns_filters():
    from bqlineage.drawio import to_drawio

    sql = ("INSERT INTO `p.d.out` WITH base AS (SELECT id, x FROM `p.d.a` WHERE x > 0) SELECT id, x FROM base;\n"
           "CREATE TABLE `p.d.out2` AS WITH base AS (SELECT id, y FROM `p.d.b` WHERE y > 0) SELECT id, y FROM base")
    r = analyze(sql, DictSchemaProvider(STEPS_SCHEMA))
    both = _pages(to_drawio(r, "table"))["Tables"]
    bases = [u.get("tooltip") for u in both.iter("UserObject") if u.get("label") == "base"]
    # a CTE name can repeat across statements: two boxes, each saying where it lives
    assert sorted(bases) == ["CTE in the query that writes p.d.out", "CTE in the query that writes p.d.out2"]
    one = _pages(to_drawio(r, "both", True, "out2", ["y"]))
    assert arrows(one["Tables"]) == {("p.d.b", "base"), ("base", "p.d.out2")}
    assert arrows(one["Columns"]) == {("p.d.b.y", "base.y"), ("base.y", "p.d.out2.y")}


# ---- OpenLineage -----------------------------------------------------------------------------------

def example_result():
    from pathlib import Path
    ex = Path(__file__).parent.parent / "examples"
    return analyze((ex / "orders.sql").read_text(), DictSchemaProvider.from_json(str(ex / "schema.json")))


def test_openlineage_event_for_the_example():
    from bqlineage.openlineage import COLUMN_LINEAGE, to_openlineage

    e = to_openlineage(example_result(), "etl/orders.sql")
    assert (e["eventType"], e["job"]) == ("COMPLETE", {"namespace": "bqlineage", "name": "etl/orders.sql"})
    assert e["eventTime"].endswith("Z") and e["run"]["runId"]
    assert e["inputs"] == [{"namespace": "bigquery", "name": "shop.crm.customers"},
                           {"namespace": "bigquery", "name": "shop.sales.orders"}]  # no temp tables
    out, = e["outputs"]
    assert out["name"] == "shop.dw.customer_revenue"
    facet = out["facets"]["columnLineage"]
    assert facet["_schemaURL"] == COLUMN_LINEAGE
    fields = facet["fields"]
    assert set(fields) == {"customer_id", "customer_name", "region", "revenue", "loaded_at"}

    def direct(col):
        return [(f["name"], f["field"], f["transformations"][0]["subtype"]) for f in fields[col]["inputFields"]
                if f["transformations"][0]["type"] == "DIRECT"]

    assert direct("revenue") == [("shop.sales.orders", "amount", "AGGREGATION")]  # SUM(...)
    assert direct("customer_name") == [("shop.crm.customers", "name", "IDENTITY")]  # copied as is
    assert direct("customer_id")[0][2] == "TRANSFORMATION"  # COALESCE of both USING sides
    assert direct("loaded_at") == []  # CURRENT_TIMESTAMP()
    indirect = {(f["name"], f["field"]) for f in fields["revenue"]["inputFields"]
                if f["transformations"] == [{"type": "INDIRECT"}]}
    assert ("shop.sales.orders", "status") in indirect  # the WHERE that picks the rows


def test_openlineage_file_targets_and_publish():
    from bqlineage.openlineage import publish, to_openlineage

    r = analyze("EXPORT DATA OPTIONS (uri = 'gs://bucket/out/*.csv', format = 'CSV') AS SELECT a FROM `p.d.t`")
    out, = to_openlineage(r, "export")["outputs"]
    assert (out["namespace"], out["name"]) == ("gs://bucket", "out/*.csv")

    class Session:
        def post(self, url, json):
            self.url, self.body = url, json
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"name": "process/1"})

    s = Session()
    assert publish({"eventType": "COMPLETE"}, "p", "us", session=s) == {"name": "process/1"}
    assert s.url == "https://datalineage.googleapis.com/v1/projects/p/locations/us:processOpenLineageRunEvent"
    assert s.body == {"eventType": "COMPLETE"}
