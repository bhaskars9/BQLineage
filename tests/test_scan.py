"""Batch scanning into the lineage store, and tracing across scripts."""
from types import SimpleNamespace

import pytest
from conftest import mock_client

from bqlineage import BigQuerySchemaProvider, Catalog, TableInfo, config, scan

TABLES = {
    "p.sales.orders": {"order_id": "INT64", "amount": "FLOAT64", "status": "STRING"},
    "p.dw.stage": {"order_id": "INT64", "amt": "FLOAT64"},
    "p.dw.fact": {"order_id": "INT64", "total": "FLOAT64"},
    "p.dw.report": {"total": "FLOAT64"},
}
LOAD_STAGE = "INSERT INTO dw.stage SELECT order_id, amount * 1.2 FROM sales.orders WHERE status = 'PAID'"
LOAD_FACT = "INSERT INTO dw.fact SELECT order_id, amt FROM dw.stage"


@pytest.fixture
def repo(tmp_path):
    """Two scripts, each run separately: stage is built from orders, fact from stage."""
    (tmp_path / "schema.json").write_text(__import__("json").dumps(TABLES))
    (tmp_path / "etl").mkdir()
    (tmp_path / "etl" / "1_stage.sql").write_text(LOAD_STAGE)
    (tmp_path / "etl" / "2_fact.sql").write_text(LOAD_FACT)
    return tmp_path


def settings(repo, **kw):
    return config.Settings(project="p", schema=str(repo / "schema.json"), catalog=str(repo / "lineage.db"), **kw)


def status(report):
    return {r["name"].rsplit("/", 1)[-1]: r["status"] for r in report}


def test_lineage_is_traced_across_scripts(repo):
    report = scan.scan(scan.files([repo / "etl"]), settings(repo))
    assert status(report) == {"1_stage.sql": "analyzed", "2_fact.sql": "analyzed"}
    db = str(repo / "lineage.db")
    tree = scan.trace(db, "dw.fact")  # project left out
    assert tree["node"] == "p.dw.fact"
    stage, = tree["edges"]
    assert stage["node"] == "p.dw.stage" and stage["scripts"][0].endswith("2_fact.sql")
    assert [e["node"] for e in stage["edges"]] == ["p.sales.orders"]
    total = scan.trace(db, "fact.total")  # a column: its values, through the other script's table
    assert total["edges"][0]["node"] == "p.dw.stage.amt"
    assert total["edges"][0]["edges"][0]["node"] == "p.sales.orders.amount"
    down = scan.trace(db, "p.sales.orders.amount", downstream=True)
    assert down["edges"][0]["node"] == "p.dw.stage.amt" and down["edges"][0]["edges"][0]["node"] == "p.dw.fact.total"


def test_only_what_changed_is_analyzed_again(repo):
    s = settings(repo)
    scan.scan(scan.files([repo / "etl"]), s)
    assert set(status(scan.scan(scan.files([repo / "etl"]), s)).values()) == {"unchanged"}
    (repo / "etl" / "2_fact.sql").write_text(LOAD_FACT + " WHERE amt > 0")  # the script changed
    assert status(scan.scan(scan.files([repo / "etl"]), s)) == {"1_stage.sql": "unchanged", "2_fact.sql": "analyzed"}
    with Catalog(s.catalog) as cat:  # a table the stage script reads changed in the catalog
        cat.put("p.sales.orders", TableInfo({**TABLES["p.sales.orders"], "region": "STRING"}))
    assert status(scan.scan(scan.files([repo / "etl"]), s)) == {"1_stage.sql": "analyzed", "2_fact.sql": "unchanged"}
    assert set(status(scan.scan(scan.files([repo / "etl"]), s, force=True)).values()) == {"analyzed"}
    assert status(scan.scan(scan.files([repo / "etl"]), settings(repo, vars={"x": "1"}))) == {
        "1_stage.sql": "analyzed", "2_fact.sql": "analyzed"}  # different settings: everything again


def test_reached_twice_is_shown_once(repo):
    (repo / "etl" / "3_report.sql").write_text(
        "INSERT INTO dw.report SELECT f.total + s.amt FROM dw.fact f JOIN dw.stage s USING (order_id)")
    scan.scan(scan.files([repo / "etl"]), settings(repo))
    tree = scan.trace(str(repo / "lineage.db"), "dw.report")
    fact, stage = tree["edges"]  # report <- fact <- stage, and report <- stage directly
    assert fact["node"] == "p.dw.fact" and fact["edges"][0]["node"] == "p.dw.stage"
    assert stage == {"node": "p.dw.stage", "scripts": [stage["scripts"][0]], "repeat": True}


def test_names_must_exist_and_be_unambiguous(repo):
    scan.scan(scan.files([repo / "etl"]), settings(repo))
    with pytest.raises(LookupError, match="run `bqlineage scan` first"):
        scan.trace(str(repo / "lineage.db"), "dw.nothing")
    with pytest.raises(LookupError, match="matches p.dw.fact.order_id, p.dw.stage.order_id"):
        scan.trace(str(repo / "lineage.db"), "order_id")


def test_parallel_scan_stores_the_same_lineage(repo):
    scan._worker.clear()
    report = scan.scan(scan.files([repo / "etl"]), settings(repo), jobs=2)
    assert set(status(report).values()) == {"analyzed"}
    assert not scan._worker  # analyzed in worker processes, not in this one
    assert scan.trace(str(repo / "lineage.db"), "fact.total")["edges"][0]["edges"][0]["node"] == "p.sales.orders.amount"


def test_job_history_and_catalog_views_are_sources_of_sql(tmp_path):
    client = mock_client()
    client.query.return_value = SimpleNamespace(result=lambda: [SimpleNamespace(query=LOAD_STAGE)] * 1)
    items = scan.job_history(BigQuerySchemaProvider(client=client), 7, "us")
    assert [i.sql for i in items] == [LOAD_STAGE] and items[0].name.startswith("job:")
    sql = client.query.call_args.args[0]
    assert "`p`.`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql and "INTERVAL 7 DAY" in sql
    assert "parent_job_id IS NULL" in sql  # a script counts once, not once per statement
    with Catalog(str(tmp_path / "c.db")) as cat:
        cat.put("p.dw.v", TableInfo({"x": "INT64"}, "SELECT order_id AS x FROM `p.sales.orders`"))
        assert [(i.name, i.sql) for i in scan.views(cat)] == [
            ("view:p.dw.v", "CREATE OR REPLACE VIEW `p.dw.v` AS SELECT order_id AS x FROM `p.sales.orders`")]
