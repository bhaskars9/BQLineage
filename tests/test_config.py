"""bqlineage.toml and how settings become a schema provider."""
import pytest

from bqlineage import BigQuerySchemaProvider, Catalog, DictSchemaProvider, config


def test_file_then_flags(tmp_path):
    path = tmp_path / "bqlineage.toml"
    path.write_text('project = "p"\ndataset = "sales"\nbq = true\nmax_age_days = 7\n[vars]\nenv = "prod"\nday = 1\n')
    s = config.load(str(path), dataset=None, refresh=True, vars={"day": "2"})
    assert (s.project, s.dataset, s.bq, s.refresh, s.max_age_days) == ("p", "sales", True, True, 7)
    assert s.vars == {"env": "prod", "day": "2"}  # flags win; values become strings


def test_found_in_the_current_directory_and_typos_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config.load() == config.Settings()  # no file: defaults
    (tmp_path / "bqlineage.toml").write_text('project = "p"\n')
    assert config.load().project == "p"
    (tmp_path / "bqlineage.toml").write_text('projcet = "p"\n')
    with pytest.raises(ValueError, match="unknown setting.*projcet"):
        config.load()


def test_provider_follows_the_settings(tmp_path):
    (tmp_path / "s.json").write_text('{"p.d.t": {"a": "INT64"}}')
    assert config.provider(config.Settings()) is None
    assert isinstance(config.provider(config.Settings(schema=str(tmp_path / "s.json"))), DictSchemaProvider)
    assert isinstance(config.provider(config.Settings(bq=True, project="p")), BigQuerySchemaProvider)  # lazy: no login
    with config.provider(config.Settings(schema=str(tmp_path / "s.json"), catalog=str(tmp_path / "c.db"))) as cat:
        assert isinstance(cat, Catalog) and isinstance(cat.fallback, DictSchemaProvider)
        assert cat.get_table("p.d.t").columns == {"a": "INT64"}


def test_a_catalog_in_this_folder_is_used(tmp_path):
    assert config.load().catalog is None
    (tmp_path / "bqlineage.db").write_bytes(b"")  # e.g. left by `catalog sync` or `scan`
    assert config.load().catalog == "bqlineage.db"
    assert config.load(catalog="other.db").catalog == "other.db"
