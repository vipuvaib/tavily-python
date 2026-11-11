"""Regression controls for explicit Oracle exact/approximate query semantics."""
import json
from enum import IntEnum

import pytest

from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from tavily.databases.oracledb.oracledb import is_duplicate
from tests.test_hybrid_rag_oracle import (
    BindValidatingConnection, FakeConnection, OracleTextFailingConnection,
)


def search(connection, **options):
    database = OracleDB(connection=connection, table_name="documents", **options)
    client = TavilyHybridClient(
        "local-test-key", database=database,
        embedding_function=lambda texts, kind: [[1., 0., 0.] for text in texts],
        ranking_function=lambda query, docs, count: docs[:count],
    )
    try:
        return client.search("test", max_foreign=0, max_local=1)
    finally:
        client.close()


@pytest.mark.parametrize("options", [{}, {"enable_native_hybrid_search": True},
                                    {"retrieval_mode": "freshness_cache"},
                                    {"retrieval_mode": "cache_then_memory"}])
def test_default_exact_search_covers_vector_hybrid_and_cache_paths(options):
    connection = BindValidatingConnection()
    assert search(connection, **options)
    statements = [sql for sql, values in connection.cursor_instance.executed_calls if "VECTOR_DISTANCE" in sql]
    assert statements and all("FETCH EXACT FIRST 1 ROWS ONLY" in sql for sql in statements)
    assert not any("FETCH APPROX" in sql for sql in statements)


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("control,kind,expected", [
    ({"vector_search_accuracy": 97}, "IVF", "WITH TARGET ACCURACY 97"),
    ({"vector_search_probes": 16}, "IVF", "NEIGHBOR PARTITION PROBES 16"),
    ({"vector_search_efsearch": 500}, "HNSW", "EFSEARCH 500"),
    ({}, "HNSW", None),
])
def test_validated_optimizer_controls_get_distinct_statements_in_both_queries(hybrid, control, kind, expected):
    connection = BindValidatingConnection()
    assert search(connection, enable_native_hybrid_search=hybrid, vector_index_type=kind,
                  vector_search_mode="approx", **control)
    sql, values = connection.cursor_instance.executed
    assert "FETCH APPROX FIRST 1 ROWS ONLY" in sql
    assert "FETCH EXACT" not in sql
    if expected:
        assert expected in sql
    else:
        assert "WITH TARGET ACCURACY" not in sql
    assert not any(name in values for name in control)


def test_recognized_text_error_preserves_approximate_controls_on_vector_fallback():
    connection = OracleTextFailingConnection()
    assert search(connection, enable_native_hybrid_search=True, vector_index_type="IVF",
                  vector_search_mode="approx", vector_search_probes=16)
    statements = [(sql, values) for sql, values in connection.cursor_instance.executed_calls
                  if "VECTOR_DISTANCE" in sql]
    assert len(statements) == 2
    assert "CONTAINS" in statements[0][0] and "CONTAINS" not in statements[1][0]
    assert all("FETCH APPROX FIRST" in sql and "NEIGHBOR PARTITION PROBES 16" in sql
               for sql, values in statements)


@pytest.mark.parametrize("name,kind", [("vector_search_accuracy", "HNSW"),
                                      ("vector_search_probes", "IVF"),
                                      ("vector_search_efsearch", "HNSW")])
def test_integer_enums_are_rendered_as_canonical_numbers(name, kind):
    class Budget(IntEnum):
        VALUE = 16
    connection = BindValidatingConnection()
    search(connection, vector_index_type=kind, vector_search_mode="approx", **{name: Budget.VALUE})
    sql, values = connection.cursor_instance.executed
    assert "16" in sql and "Budget" not in sql
    assert name not in values


@pytest.mark.parametrize("options", [
    {"vector_search_mode": "automatic"}, {"vector_search_mode": None},
    {"vector_search_probes": 16},
    {"vector_search_mode": "approx", "vector_search_probes": 16},
    {"vector_search_mode": "approx", "vector_index_type": "IVF", "vector_search_efsearch": 100},
    {"vector_search_mode": "approx", "vector_search_accuracy": 90, "vector_search_efsearch": 100},
    {"vector_search_mode": "approx", "vector_search_accuracy": 101},
])
def test_invalid_modes_and_conflicting_controls_fail_before_connection(monkeypatch, options):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid vector search configuration opened a connection")
    monkeypatch.setattr(OracleDB, "_connect", unexpected)
    with pytest.raises(ValueError):
        OracleDB(table_name="documents", user="test", password="private", dsn="unused", **options)


@pytest.mark.parametrize("name,kind", [("vector_search_accuracy", "HNSW"),
                                      ("vector_search_probes", "IVF"),
                                      ("vector_search_efsearch", "HNSW")])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "16", "1) OR 1=1", float("nan")])
def test_query_controls_reject_nonpositive_noninteger_and_sql_values(name, kind, value):
    with pytest.raises(ValueError):
        OracleDB(connection=FakeConnection(), table_name="documents", vector_index_type=kind,
                 vector_search_mode="approx", **{name: value})


def test_deduplication_is_exact_even_when_retrieval_is_approximate():
    connection = BindValidatingConnection(fetchone_results=[(.95,)])
    database = OracleDB(connection=connection, table_name="documents", vector_index_type="IVF",
                        vector_search_mode="approx", vector_search_probes=1,
                        dedup_similarity_threshold=.90)
    assert is_duplicate(database, [1., 0., 0.], {})
    sql, values = connection.cursor_instance.executed
    assert "FETCH EXACT FIRST 1 ROWS ONLY" in sql
    assert "FETCH APPROX" not in sql
    assert "vector_search_probes" not in values


def test_query_controls_do_not_change_index_creation_parameters():
    connection = FakeConnection(fetchone_results=[(0,)])
    database = OracleDB(connection=connection, table_name="documents", vector_index_type="IVF",
                        vector_index_accuracy=95, vector_index_partitions=32,
                        vector_search_mode="approx", vector_search_probes=16)
    assert database.ensure_vector_index()
    sql, values = connection.cursor_instance.executed
    assert "DBMS_VECTOR.CREATE_INDEX" in sql and values["idx_accuracy"] == 95
    assert json.loads(values["idx_parameters"]) == {"type": "IVF", "partitions": 32}
