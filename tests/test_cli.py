"""The command line, end to end on files in a temporary directory."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from conftest import COPY_CHAIN, TABLES, information_schema, mock_client, mocked_bigquery
from google.api_core.exceptions import BadRequest

from bqlineage import BigQuerySchemaProvider, cli


def test_cli_raw_shows_the_hops_compaction_hides(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(TABLES))
    (tmp_path / "q.sql").write_text(COPY_CHAIN)
    base = ["lineage", str(tmp_path / "q.sql"), "--schema", str(tmp_path / "s.json"), "--project", "p",
            "--level", "column", "--columns", "total"]
    cli.main(base)
    compact = json.loads(capsys.readouterr().out)["columns"]["p.dw.fact.total"]["steps"]
    cli.main(base + ["--raw"])
    raw = json.loads(capsys.readouterr().out)["columns"]["p.dw.fact.total"]["steps"]
    assert len(compact) == 2 and len(raw) == 3 and all("same" not in s for s in compact + raw)


def test_cli(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(TABLES))
    (tmp_path / "q.sql").write_text("SELECT name FROM sales.customers JOIN sales.orders ON cust_id = customer_id")
    rc = cli.main(["lineage", str(tmp_path / "q.sql"), "--schema", str(tmp_path / "s.json"),
                   "--project", "p", "--dataset", "sales"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["columns"]["<query>.name"]["sources"] == ["p.sales.customers.name"]
    assert out["columns"]["<query>.name"]["indirect_sources"] == ["p.sales.customers.customer_id", "p.sales.orders.cust_id"]

    (tmp_path / "bad.sql").write_text("SELECT FROM WHERE")
    assert cli.main(["validate", str(tmp_path / "bad.sql")]) == 1

    with mocked_bigquery():
        rc = cli.main(["lineage", str(tmp_path / "q.sql"), "--bq", "--level", "table"])
    assert rc == 1  # customers isn't in the mocked BigQuery -> reported, not guessed
    assert "not found" in capsys.readouterr().out


def test_cli_table_and_column_filter(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(TABLES))
    (tmp_path / "q.sql").write_text("INSERT INTO dw.fact (order_id, total) SELECT order_id, amount FROM sales.orders;\n"
                                    "CREATE TABLE dw.other AS SELECT customer_id FROM sales.customers;")
    base = ["lineage", str(tmp_path / "q.sql"), "--schema", str(tmp_path / "s.json"), "--project", "p"]
    assert cli.main(base + ["--table", "dw.fact", "--columns", "total"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tables"] == {"p.dw.fact": ["p.sales.orders"]}
    assert out["columns"] == {"p.dw.fact.total": {
        "sources": ["p.sales.orders.amount"], "indirect_sources": [],
        "steps": [{"column": "p.dw.fact.total", "expression": "orders.amount", "from": ["p.sales.orders"]}]}}
    assert cli.main(base + ["--columns", "order_id,customer_id", "--level", "column"]) == 0
    assert set(json.loads(capsys.readouterr().out)["columns"]) == {"p.dw.fact.order_id", "p.dw.other.customer_id"}
    with pytest.raises(SystemExit, match="Targets: p.dw.fact, p.dw.other"):
        cli.main(base + ["--table", "nope"])
    with pytest.raises(SystemExit, match="None of the columns"):
        cli.main(base + ["--table", "fact", "--columns", "nope"])


def test_cli_bq_without_credentials_exits_cleanly(tmp_path):
    (tmp_path / "q.sql").write_text("SELECT 1")

    class NoLogin(BigQuerySchemaProvider):
        @property
        def client(self):
            raise RuntimeError("Your default credentials were not found")

    with patch("bqlineage.cli.BigQuerySchemaProvider", NoLogin), pytest.raises(SystemExit, match="bqlineage auth"):
        cli.main(["lineage", str(tmp_path / "q.sql"), "--bq"])


def test_config_file_and_vars(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(TABLES))
    (tmp_path / "bqlineage.toml").write_text('project = "p"\nschema = "s.json"\n[vars]\nenv = "dw"\n')
    (tmp_path / "q.sql").write_text("INSERT INTO {{ env }}.fact (total) SELECT amount FROM sales.orders WHERE order_id > ${min}")
    assert cli.main(["lineage", "q.sql", "--var", "min=10", "--level", "column"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == 1 and out["columns"]["p.dw.fact.total"]["sources"] == ["p.sales.orders.amount"]
    assert out["issues"] == []  # every placeholder had a value
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        cli.main(["lineage", "q.sql", "--var", "oops"])


def test_catalog_commands_and_offline_lineage(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(TABLES))
    assert cli.main(["catalog", "import", "s.json", "--catalog", "c.db"]) == 0
    (tmp_path / "q.sql").write_text("SELECT name FROM sales.customers JOIN sales.orders ON cust_id = customer_id")
    capsys.readouterr()
    # schemas from the catalog alone: no --bq, no --schema
    assert cli.main(["lineage", "q.sql", "--catalog", "c.db", "--project", "p", "--level", "column"]) == 0
    assert json.loads(capsys.readouterr().out)["columns"]["<query>.name"]["sources"] == ["p.sales.customers.name"]
    assert cli.main(["catalog", "export", "out.json", "--catalog", "c.db"]) == 0
    assert set(json.loads((tmp_path / "out.json").read_text())) == set(TABLES)
    with mocked_bigquery(information_schema(mock_client(), {"p.new.t": ({"a": "INT64"}, None)})):
        assert cli.main(["catalog", "sync", "--datasets", "new", "--catalog", "c.db"]) == 0
    assert "synced 1 tables" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="needs --datasets or --region"):
        cli.main(["catalog", "sync", "--catalog", "c.db"])


def test_scan_and_trace_commands(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps({**TABLES, "p.dw.stage": {"order_id": "INT64", "amount": "FLOAT64"}}))
    (tmp_path / "etl").mkdir()
    (tmp_path / "etl" / "a.sql").write_text("INSERT INTO dw.stage SELECT order_id, amount FROM sales.orders")
    (tmp_path / "etl" / "b.sql").write_text("INSERT INTO dw.fact (order_id, total) SELECT order_id, amount FROM dw.stage")
    base = ["--schema", "s.json", "--project", "p"]
    assert cli.main(["scan", "etl", *base]) == 0
    assert "2 analyzed, 0 unchanged -> bqlineage.db" in capsys.readouterr().out
    assert cli.main(["scan", "etl", *base]) == 0
    assert "0 analyzed, 2 unchanged" in capsys.readouterr().out
    assert cli.main(["trace", "fact.total"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "p.dw.fact.total", "`-- p.dw.stage.amount  [etl/b.sql]", "    `-- p.sales.orders.amount  [etl/a.sql]"]
    assert cli.main(["trace", "sales.orders", "--downstream", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["edges"][0]["node"] == "p.dw.stage"
    with pytest.raises(SystemExit, match="Nothing to scan"):
        cli.main(["scan"])


def test_validate_dry_run(tmp_path, capsys):
    (tmp_path / "q.sql").write_text("SELECT order_id FROM `p.sales.orders`")
    client = mock_client()
    client.query.return_value = SimpleNamespace(referenced_tables=[
        SimpleNamespace(project="p", dataset_id="sales", table_id="orders"),
        SimpleNamespace(project="p", dataset_id="hidden", table_id="t")])
    with mocked_bigquery(client):
        assert cli.main(["validate", "q.sql", "--bq", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "BigQuery dry run: OK" in out and "didn't see: p.hidden.t" in out  # e.g. SQL built at run time
    client.query.side_effect = BadRequest("Syntax error: Unexpected end of script at [1:8]")
    with mocked_bigquery(client):
        assert cli.main(["validate", "q.sql", "--bq", "--dry-run"]) == 1
    assert "ERROR: BigQuery dry run: Syntax error" in capsys.readouterr().out


def test_openlineage_file_and_publish_command(tmp_path, capsys):
    (tmp_path / "s.json").write_text(json.dumps(TABLES))
    (tmp_path / "q.sql").write_text("INSERT INTO dw.fact (total) SELECT amount FROM sales.orders")
    assert cli.main(["lineage", "q.sql", "--schema", "s.json", "--project", "p", "--openlineage", "ev.json"]) == 0
    event = json.loads((tmp_path / "ev.json").read_text())
    assert event["job"]["name"] == "q.sql" and event["outputs"][0]["name"] == "p.dw.fact"
    with patch("bqlineage.cli.publish") as send:
        assert cli.main(["publish", "ev.json", "--project", "p", "--region", "us"]) == 0
    send.assert_called_once_with(event, "p", "us")
    with pytest.raises(SystemExit, match="needs --project and --region"):
        cli.main(["publish", "ev.json"])
