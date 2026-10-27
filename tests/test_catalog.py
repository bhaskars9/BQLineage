"""Schema providers: BigQuery (mocked client) and the SQLite catalog."""
from datetime import datetime
from types import SimpleNamespace

from conftest import field, information_schema, mock_client
from google.api_core.exceptions import BadRequest, Forbidden

from bqlineage import BigQuerySchemaProvider, Catalog, TableInfo, analyze


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


# ---- the SQLite catalog ---------------------------------------------------------------------------

class NoLogin(BigQuerySchemaProvider):
    """A BigQuery provider that fails the moment anything actually needs BigQuery."""

    @property
    def client(self):
        raise AssertionError("BigQuery was asked although the catalog had the answer")


def test_catalog_answers_from_disk_after_the_first_fetch(tmp_path):
    db, client = str(tmp_path / "c.db"), mock_client()
    with Catalog(db, BigQuerySchemaProvider(client=client)) as cat:
        assert cat.get_table("p.sales.orders").columns["amount"] == "FLOAT64"
        assert cat.get_table("p.sales.nope") is None  # "not found" is remembered too
    calls = client.get_table.call_count
    with Catalog(db, NoLogin()) as cat:  # next run: offline, no BigQuery client ever created
        assert cat.get_table("p.sales.orders").columns["amount"] == "FLOAT64"
        assert cat.get_table("p.sales.nope") is None
    assert client.get_table.call_count == calls == 2


def test_refresh_and_max_age_refetch(tmp_path):
    db, client = str(tmp_path / "c.db"), mock_client()
    with Catalog(db, BigQuerySchemaProvider(client=client)) as cat:
        cat.get_table("p.sales.orders")
    with Catalog(db, BigQuerySchemaProvider(client=client), refresh=True) as cat:
        cat.get_table("p.sales.orders")
        cat.get_table("p.sales.orders")  # refreshed once per run, then served from the catalog
    assert client.get_table.call_count == 2
    with Catalog(db, BigQuerySchemaProvider(client=client), max_age_days=1) as cat:
        cat.get_table("p.sales.orders")  # fresh: no call
        assert client.get_table.call_count == 2
        cat.conn.execute("UPDATE tables SET fetched_at = fetched_at - 3 * 86400")
        cat.get_table("p.sales.orders")  # 3 days old: re-fetched
    assert client.get_table.call_count == 3


def test_catalog_names_without_project_and_offline_wildcards(tmp_path):
    with Catalog(str(tmp_path / "c.db")) as cat:
        cat.sync([("p.ev.events_20240101", TableInfo({"a": "STRING"})),
                  ("p.ev.events_20240102", TableInfo({"a": "STRING", "b": "INT64"})),
                  ("p.sales.orders", TableInfo({"x": "INT64"}))])
        assert cat.get_table("sales.orders").columns == {"x": "INT64"}  # the one sales.orders there is
        assert cat.get_table("p.ev.events_*").columns == {"a": "STRING", "b": "INT64"}  # newest shard
        cat.put("q.sales.orders", TableInfo({"y": "INT64"}))
        assert cat.get_table("sales.orders") is None  # now ambiguous: two projects have one


def test_catalog_export_and_import_round_trip(tmp_path):
    tables = {"p.d.t": TableInfo({"a": "INT64"}), "p.d.v": TableInfo({"a": "INT64"}, "SELECT a FROM p.d.t")}
    with Catalog(str(tmp_path / "a.db")) as cat:
        cat.sync(tables.items())
        cat.put("p.d.gone", None)  # "not found" entries aren't exported
        assert cat.export(str(tmp_path / "snap.json")) == 2
    with Catalog(str(tmp_path / "b.db")) as cat:
        assert cat.load_json(str(tmp_path / "snap.json")) == 2
        assert cat.get_table("p.d.v") == tables["p.d.v"] and list(cat.views()) == [("p.d.v", "SELECT a FROM p.d.t")]


def test_bulk_sync_from_information_schema(tmp_path):
    client = information_schema(mock_client(), {
        "p.sales.orders": ({"order_id": "INT64", "items": "ARRAY<STRUCT<order STRING, qty INT64>>"}, None),
        "p.sales.v": ({"order_id": "INT64"}, "SELECT order_id FROM `p.sales.orders`"),
    })
    bq = BigQuerySchemaProvider(client=client)
    with Catalog(str(tmp_path / "c.db")) as cat:
        assert cat.sync(bq.iter_tables(datasets=["sales"])) == 2
        assert cat.get_table("p.sales.orders").columns["items"] == "ARRAY<STRUCT<`order` STRING, `qty` INT64>>"
        assert cat.get_table("p.sales.v").view_sql.startswith("SELECT")
        assert cat.sync([("p.x.y", TableInfo({"a": "INT64"}))], rebuild=True) == 1
        assert cat.get_table("p.sales.orders") is None
    sqls = [c.args[0] for c in client.query.call_args_list]
    assert all("`p`.`sales`.INFORMATION_SCHEMA" in s for s in sqls) and "is_hidden = 'NO'" in sqls[1]
    list(bq.iter_tables(region="eu"))
    assert "`p`.`region-eu`.INFORMATION_SCHEMA.COLUMNS" in client.query.call_args_list[-1].args[0]


def test_analysis_through_the_catalog_records_the_schemas_it_used(tmp_path):
    with Catalog(str(tmp_path / "c.db"), BigQuerySchemaProvider(client=mock_client())) as cat:
        r = analyze("SELECT oid FROM sales.v", cat)
    assert r.column_lineage() == {"<query>.oid": ["p.sales.orders.order_id"]}
    assert set(r.schemas) == {"p.sales.v", "p.sales.orders"}


def test_dry_run_reports_bigquerys_verdict():
    client = mock_client()
    client.query.return_value = SimpleNamespace(referenced_tables=[
        SimpleNamespace(project="p", dataset_id="sales", table_id="orders")])
    assert BigQuerySchemaProvider(client=client).dry_run("SELECT 1") == (None, {"p.sales.orders"})
    assert client.query.call_args.kwargs["job_config"].dry_run
    client.query.side_effect = BadRequest("Unrecognized name: nope at [1:8]")
    assert BigQuerySchemaProvider(client=client).dry_run("SELECT nope")[0] == "Unrecognized name: nope at [1:8]"


def test_offline_catalog_says_how_to_find_missing_tables(tmp_path):
    with Catalog(str(tmp_path / "c.db")) as cat:
        issue, = analyze("SELECT a FROM `p.d.missing`", cat).issues[:1]
    assert issue.message == "Table `p.d.missing` not found in the catalog (pass --bq to look it up in BigQuery)"
