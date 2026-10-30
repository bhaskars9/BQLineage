"""draw.io and OpenLineage exports."""
import xml.etree.ElementTree as ET
from itertools import pairwise
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


def _name(cells, cid) -> str:
    label, c = cells[cid]
    return label if c.get("parent") == "1" else f"{cells[c.get('parent')][0]}.{label}"


def arrows(page, dashed=None) -> set:
    """(source, target) labels; a row is box.column. dashed: only the dashed (True) or solid (False) ones"""
    cells = _cells(page)
    return {(_name(cells, c.get("source")), _name(cells, c.get("target"))) for _, c in cells.values()
            if c.get("source") and dashed in (None, "dashed=1" in c.get("style"))}  # the legend's samples have none


def _routes(page) -> tuple[dict, list]:
    """(source, target) -> the arrow's points (where it leaves, its bends, where it arrives); every box as
    (x, y, w, h)."""
    cells = _cells(page)

    def geo(cid):  # on the page: a row is placed inside its box
        c = cells[cid][1]
        g = c.find("mxGeometry")
        x, y, w, h = (float(g.get(k, 0)) for k in ("x", "y", "width", "height"))
        if c.get("parent") != "1":
            px, py, _, _ = geo(c.get("parent"))
            x, y = x + px, y + py
        return x, y, w, h

    rects = [geo(i) for i, (_, c) in cells.items() if c.get("vertex") and c.get("parent") == "1" and geo(i)[1] >= 0]
    routes = {}
    for _, c in cells.values():
        if c.get("source"):
            st = dict(kv.split("=", 1) for kv in c.get("style").split(";") if "=" in kv)
            (sx, sy, sw, sh), (tx, ty, _, th) = geo(c.get("source")), geo(c.get("target"))
            routes[_name(cells, c.get("source")), _name(cells, c.get("target"))] = [
                (sx + sw, sy + sh * float(st["exitY"])),
                *((float(p.get("x")), float(p.get("y"))) for p in c.iter("mxPoint")),
                (tx, ty + th * float(st["entryY"]))]
    return routes, rects


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
    assert legend(pages["Tables"]) == {"source table", "temp table", "CTE", "target",
                                       "driving table (FROM)", "joined in / filter"}
    # FROM paid_orders p JOIN eu_customers c: paid_orders decides the rows, eu_customers adds columns
    assert arrows(pages["Tables"], dashed=True) == {("eu_customers", "shop.dw.customer_revenue")}
    assert ("eu_customers.name", "shop.dw.customer_revenue.customer_name") in arrows(pages["Columns"], dashed=True)
    assert ("_SESSION.paid_orders.gross", "shop.dw.customer_revenue.revenue") in arrows(pages["Columns"], dashed=False)
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
    assert legend(pages["Tables"]) == {"source table", "view", "temp table", "CTE", "subquery", "target",
                                       "driving table (FROM)", "joined in / filter"}
    assert arrows(pages["Tables"], dashed=True) == {("p.d.blocked", "_SESSION.t")}  # both UNION branches drive u
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


def test_drawio_solid_arrows_come_from_the_driving_table():
    from bqlineage.drawio import to_drawio

    schema = DictSchemaProvider({**STEPS_SCHEMA, **{f"p.d.out{i}": {"id": "INT64", "y": "INT64"} for i in (2, 3, 4)}})
    r = analyze("""
INSERT INTO `p.d.out` (id, v) SELECT a.id, b.y FROM `p.d.a` a LEFT JOIN `p.d.b` b ON b.id = a.id;
INSERT INTO `p.d.out2` (id, y) SELECT b.id, a.x FROM `p.d.a` a RIGHT JOIN `p.d.b` b ON b.id = a.id;
INSERT INTO `p.d.out3` (id, y) SELECT a.id, b.y FROM `p.d.a` a FULL JOIN `p.d.b` b USING (id)
                               JOIN `p.d.blocked` k USING (id);
INSERT INTO `p.d.out4` (id, y) WITH c AS (SELECT * FROM `p.d.b`) SELECT a.id, c.y FROM `p.d.a` a LEFT JOIN c USING (id)
""", schema)
    assert r.ok, r.issues
    pages = _pages(to_drawio(r))
    assert arrows(pages["Tables"], dashed=False) == {
        ("p.d.a", "p.d.out"),  # FROM a LEFT JOIN b: a
        ("p.d.b", "p.d.out2"),  # FROM a RIGHT JOIN b: b
        ("p.d.a", "p.d.out3"), ("p.d.b", "p.d.out3"),  # FULL JOIN: both
        ("p.d.a", "p.d.out4")}
    assert arrows(pages["Tables"], dashed=True) == {
        ("p.d.b", "p.d.out"), ("p.d.a", "p.d.out2"),
        ("p.d.blocked", "p.d.out3"),  # an INNER JOIN that only filters
        ("p.d.b", "p.d.out4")}  # through c, which only copies: joined in all the same
    assert arrows(pages["Columns"], dashed=True) == {
        ("p.d.b.y", "p.d.out.v"), ("p.d.a.x", "p.d.out2.y"), ("p.d.b.y", "p.d.out4.y")}


STRESS_SQL = """
CREATE TEMP TABLE base AS
SELECT order_id, customer_id, product_id, amount, order_ts FROM `p.d.orders` WHERE status = 'PAID';
INSERT INTO `p.d.facts` (order_id, customer_name, segment, product_name, amount, refunded, net)
WITH cust AS (SELECT c.customer_id, c.name, s.segment FROM `p.d.customers` c
              LEFT JOIN `p.d.segments` s ON s.customer_id = c.customer_id),
     prod AS (SELECT product_id, name AS product_name FROM `p.d.products` WHERE live),
     ref AS (SELECT order_id, SUM(refund) AS refunded FROM `p.d.refunds` GROUP BY order_id),
     joined AS (SELECT b.order_id, cu.name AS customer_name, cu.segment, pr.product_name, b.amount,
                       IFNULL(r.refunded, 0) AS refunded
                FROM base b LEFT JOIN cust cu USING (customer_id) LEFT JOIN prod pr USING (product_id)
                LEFT JOIN ref r USING (order_id)
                WHERE b.customer_id NOT IN (SELECT customer_id FROM `p.d.blocked`))
SELECT order_id, customer_name, segment, product_name, amount, refunded, amount - refunded FROM joined;
INSERT INTO `p.d.daily` (day, net)
SELECT DATE(b.order_ts), SUM(f.net) FROM `p.d.facts` f JOIN base b USING (order_id) GROUP BY 1
"""
STRESS_SCHEMA = {
    "p.d.orders": {"order_id": "INT64", "customer_id": "INT64", "product_id": "INT64", "amount": "NUMERIC",
                   "order_ts": "TIMESTAMP", "status": "STRING"},
    "p.d.customers": {"customer_id": "INT64", "name": "STRING"},
    "p.d.segments": {"customer_id": "INT64", "segment": "STRING"},
    "p.d.products": {"product_id": "INT64", "name": "STRING", "live": "BOOL"},
    "p.d.refunds": {"order_id": "INT64", "refund": "NUMERIC"},
    "p.d.blocked": {"customer_id": "INT64"},
    "p.d.facts": {"order_id": "INT64", "customer_name": "STRING", "segment": "STRING", "product_name": "STRING",
                  "amount": "NUMERIC", "refunded": "NUMERIC", "net": "NUMERIC"},
    "p.d.daily": {"day": "DATE", "net": "NUMERIC"},
}


def test_drawio_arrows_run_between_boxes_never_through_them():
    from bqlineage.drawio import to_drawio

    stress = analyze(STRESS_SQL, DictSchemaProvider(STRESS_SCHEMA))
    assert stress.ok, stress.issues
    steps = analyze(STEPS_SQL, DictSchemaProvider(STEPS_SCHEMA))
    for xml in (to_drawio(stress), to_drawio(stress, resolve=False), to_drawio(steps), to_drawio(steps, resolve=False)):
        for name, page in _pages(xml).items():
            routes, rects = _routes(page)
            assert routes and len(rects) > 3
            for pts in routes.values():
                for (x1, y1), (x2, y2) in pairwise(pts):
                    assert abs(x1 - x2) < .01 or abs(y1 - y2) < .01, (name, pts)  # straight runs, square bends
                    for bx, by, bw, bh in rects:
                        if abs(y1 - y2) < .01:
                            assert not (by < y1 < by + bh and max(x1, x2) > bx and min(x1, x2) < bx + bw), (name, pts)
                        else:
                            assert not (bx < x1 < bx + bw and max(y1, y2) > by and min(y1, y2) < by + bh), (name, pts)
            cells = [c for c in page.iter() if c.tag == "mxCell" and (c.get("source") or c.get("vertex"))]
            kinds = ["box" if c.get("vertex") else "dashed" if "dashed=1" in c.get("style") else "solid" for c in cells]
            # arrows behind the boxes, the dashed ones behind the solid ones
            assert kinds == sorted(kinds, key=["dashed", "solid", "box"].index)
            # rows stack top to bottom: draw.io re-stacks them whenever a box moves
            assert all("horizontalStack=0" in c.get("style") for c in cells if "swimlane" in c.get("style", ""))
    # base.order_ts -> daily.day passes two columns of boxes in lanes, and bends once
    routes = _routes(_pages(to_drawio(stress))["Columns"])[0]
    assert len(routes["_SESSION.base.order_ts", "p.d.daily.day"]) == 4


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
