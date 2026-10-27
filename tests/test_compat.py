"""The sqlglot behaviours bqlineage depends on (see bqlineage/compat.py). When one of these fails after a sqlglot
upgrade, the message says what to look at; fix compat.py rather than the code that uses it."""
import inspect

import sqlglot
from sqlglot.optimizer import qualify_columns
from sqlglot.optimizer.scope import build_scope
from sqlglot.tokens import TokenType

from bqlineage.compat import BQ, PSEUDOCOLUMNS, lineage, star_friendly_resolver, to_node, tokenize


def test_pinned_major_version():
    assert sqlglot.__version__.startswith("30."), "pyproject pins sqlglot 30.x; re-run this suite before widening it"


def test_procedural_keywords_still_need_the_tokenizer_fix():
    # sqlglot swallows the rest of a statement after these into one string token. If this starts failing,
    # sqlglot stopped doing that and compat.Tokenizer's keyword override can probably go.
    plain = BQ.tokenizer_class(dialect=BQ).tokenize("BEGIN SELECT 1")
    assert [t.token_type for t in plain] == [TokenType.COMMAND, TokenType.STRING]
    assert [t.text for t in tokenize("BEGIN SELECT 1")] == ["BEGIN", "SELECT", "1"]


def test_resolver_hook_exists_and_is_restored():
    original = qualify_columns.Resolver
    assert "get_source_columns" in vars(original), "compat._Resolver overrides Resolver.get_source_columns"
    with star_friendly_resolver():
        assert qualify_columns.Resolver is not original
    assert qualify_columns.Resolver is original


def test_lineage_internals_we_call():
    params = inspect.signature(to_node).parameters
    assert {"column", "scope", "dialect", "schema", "trim_selects", "_cache"} <= set(params)
    params = inspect.signature(lineage).parameters
    assert {"scope", "trim_selects", "copy"} <= set(params)
    q = sqlglot.parse_one("SELECT a.x AS x, a.y AS y FROM t AS a", read="bigquery")
    nodes = lineage(None, q, schema=None, dialect="bigquery", scope=build_scope(q), copy=False, trim_selects=False)
    assert set(nodes) == {"x", "y"}, "lineage(None, ...) returns every output column at once"


def test_bigquery_pseudocolumns_are_known():
    assert {"_PARTITIONTIME", "_PARTITIONDATE", "_TABLE_SUFFIX"} <= PSEUDOCOLUMNS
