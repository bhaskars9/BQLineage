"""draw.io and OpenLineage exports."""
from types import SimpleNamespace

from bqlineage import DictSchemaProvider, analyze, cli


def test_drawio_export_of_the_example(tmp_path, capsys):
    import xml.etree.ElementTree as ET
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

    assert {"shop.sales.orders", "shop.crm.customers", "shop.dw.customer_revenue"} <= labels(pages["Tables"])
    assert sum(c.get("edge") == "1" for c in pages["Tables"].iter("mxCell")) == 2
    assert {"amount", "gross", "revenue", "loaded_at"} - labels(pages["Columns"]) == {"gross"}  # temp hidden
    revenue = next(u for u in pages["Columns"].iter("UserObject") if u.get("label") == "revenue")
    assert "SUM(p.gross)" in revenue.get("tooltip") and "o.status = 'PAID'" in revenue.get("tooltip")
    assert sum(c.get("edge") == "1" for c in pages["Columns"].iter("mxCell")) == 5  # one per source column
    def page(x):
        return read_page(next(ET.fromstring(x).iter("diagram")))

    raw = page(to_drawio(analyze((ex / "orders.sql").read_text(), DictSchemaProvider.from_json(
        str(ex / "schema.json"))), "column", resolve=False))
    assert "gross" in labels(raw)  # --raw keeps the temp table as a hop
    plain = page(to_drawio(analyze("SELECT a.x FROM `p.d.a` a"), "table"))
    assert "<query>" in labels(plain)  # labels are plain text, not HTML
    # control characters (illegal in XML) from pasted SQL can't break the file
    odd = to_drawio(analyze("INSERT INTO `p.d.out` (k) SELECT CONCAT(a, '\x0c\x01') FROM `p.d.a`"), "column")
    ET.fromstring(odd)
    page(odd)
    # rows keep the target's column order, not alphabetical
    target = next(u for u in pages["Columns"].iter("mxCell") if u.get("value") == "shop.dw.customer_revenue")
    rows = [u.get("label") for u in pages["Columns"].iter("UserObject") if u[0].get("parent") == target.get("id")]
    assert rows == ["customer_id", "customer_name", "region", "revenue", "loaded_at"]


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
