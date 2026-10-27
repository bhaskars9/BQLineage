"""Golden tests: realistic scripts in tests/golden/ and the lineage they must produce (the .json next to each).

A change in output fails here with a diff. When the change is intended, regenerate and review:
    pytest tests/test_golden.py --update-golden
    git diff tests/golden
Add your own (anonymized) production scripts the same way: drop the .sql in, add its tables to schema.json,
run with --update-golden, and check the new .json by hand before committing it.
"""
import json
from pathlib import Path

import pytest

from bqlineage import DictSchemaProvider, analyze

GOLDEN = Path(__file__).parent / "golden"
SCRIPTS = sorted(GOLDEN.glob("*.sql"))


def lineage(sql: str) -> dict:
    r = analyze(sql, DictSchemaProvider.from_json(str(GOLDEN / "schema.json")),
                vars=json.loads((GOLDEN / "vars.json").read_text()))
    return {
        "tables": r.table_lineage(),
        "columns": {k: {"sources": d["sources"], "indirect_sources": d["indirect_sources"]}
                    for k, d in r.column_details().items()},
        "issues": [str(i) for i in r.issues],
    }


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.stem for s in SCRIPTS])
def test_golden(script, request):
    actual = lineage(script.read_text(encoding="utf-8"))
    expected_file = script.with_suffix(".json")
    if request.config.getoption("--update-golden"):
        expected_file.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
    assert expected_file.exists(), f"no {expected_file.name}: run pytest tests/test_golden.py --update-golden"
    assert actual == json.loads(expected_file.read_text(encoding="utf-8"))
