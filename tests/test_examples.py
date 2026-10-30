"""The examples in examples/ (see examples/README.md): each command still gives the output checked in next to it.

After an intended change, regenerate and review:
    pytest tests/test_examples.py --update-golden
    git diff examples
"""
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from bqlineage import cli
from bqlineage.drawio import read_page

ROOT = Path(__file__).parent.parent
EX = ROOT / "examples"
SCHEMA = ["--schema", "examples/schema.json"]
LINEAGE = {name: SCHEMA for name in ("orders", "joins", "views", "merge", "procedural", "recursive")}
LINEAGE["templated"] = ["--config", "examples/bqlineage.toml"]


@pytest.fixture(autouse=True)
def at_root(monkeypatch):  # the commands in examples/README.md run from the repo root
    monkeypatch.chdir(ROOT)


def check(path: Path, text: str, request):
    if request.config.getoption("--update-golden"):
        path.write_text(text, encoding="utf-8", newline="\n")
    assert text == path.read_text(encoding="utf-8"), f"{path.name} changed: --update-golden rewrites it"


def pages(xml: str) -> list[str]:  # page content, not the compressed bytes, which can vary between zlib builds
    return [ET.tostring(read_page(d), encoding="unicode") for d in ET.fromstring(xml).iter("diagram")]


@pytest.mark.parametrize("name", LINEAGE)
def test_lineage_example(name, tmp_path, capsys, request):
    out = tmp_path / f"{name}.drawio"
    assert cli.main(["lineage", f"examples/{name}.sql", *LINEAGE[name], "--drawio", str(out)]) == 0
    check(EX / f"{name}.lineage.json", capsys.readouterr().out, request)
    if request.config.getoption("--update-golden"):
        (EX / f"{name}.drawio").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    assert pages(out.read_text(encoding="utf-8")) == pages((EX / f"{name}.drawio").read_text(encoding="utf-8"))


def test_openlineage_example(tmp_path, capsys, request):
    out = tmp_path / "event.json"
    assert cli.main(["lineage", "examples/orders.sql", *SCHEMA, "--level", "table", "--openlineage", str(out)]) == 0
    event = json.loads(out.read_text(encoding="utf-8"))
    # new on every run
    event["eventTime"], event["run"]["runId"] = "2026-10-29T00:00:00Z", "00000000-0000-0000-0000-000000000000"
    check(EX / "orders.openlineage.json", json.dumps(event, indent=2) + "\n", request)


def test_validate_example(capsys, request):
    assert cli.main(["validate", "examples/validate.sql", *SCHEMA]) == 1  # errors: exit code 1
    check(EX / "validate.txt", capsys.readouterr().out, request)


def test_scan_and_trace_example(tmp_path, capsys, request):
    db = str(tmp_path / "lineage.db")
    assert cli.main(["scan", "examples/scan", *SCHEMA, "--catalog", db]) == 0
    capsys.readouterr()
    out = []
    for args in (["rpt.region_totals.total"], ["sales.orders", "--downstream"]):
        assert cli.main(["trace", *args, "--catalog", db]) == 0
        out.append(f"$ bqlineage trace {' '.join(args)}\n{capsys.readouterr().out}")
    check(EX / "scan" / "trace.txt", "\n".join(out), request)


def test_catalog_example(tmp_path, capsys):
    db = str(tmp_path / "shop.db")
    assert cli.main(["catalog", "import", "examples/schema.json", "--catalog", db]) == 0
    capsys.readouterr()
    assert cli.main(["lineage", "examples/orders.sql", "--catalog", db]) == 0  # offline, from the catalog alone
    assert capsys.readouterr().out == (EX / "orders.lineage.json").read_text(encoding="utf-8")


def test_python_api_example():
    r = subprocess.run([sys.executable, "examples/api.py"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "revenue: {'shop.dw.customer_revenue.revenue': ['shop.sales.orders.amount']}" in r.stdout
