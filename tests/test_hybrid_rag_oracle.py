import pytest

from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from tavily.databases.oracledb import oracledb as oracle_database


DEFAULT_SCHEMA_ROWS = [
    ("ADDED_AT", "TIMESTAMP"),
    ("CONTENT", "CLOB"),
    ("EMBEDDINGS", "VECTOR"),
    ("SITE_URL", "VARCHAR2"),
    ("RAW_PAYLOAD", "CLOB"),
    ("SOURCE_URL", "VARCHAR2"),
    ("SOURCE_TITLE", "VARCHAR2"),
    ("RETRIEVAL_QUERY", "VARCHAR2"),
    ("RETRIEVAL_TIMESTAMP", "TIMESTAMP WITH TIME ZONE"),
    ("RETRIEVAL_MODE", "VARCHAR2"),
    ("CACHE_HIT", "NUMBER"),
    ("INSERTED_FROM", "VARCHAR2"),
    ("PROVIDER_NAME", "VARCHAR2"),
    ("MEMORY_SCOPE", "VARCHAR2"),
    ("EXPIRES_AT", "TIMESTAMP WITH TIME ZONE"),
    ("LAST_SEEN_AT", "TIMESTAMP WITH TIME ZONE"),
    ("QUERY_COUNT", "NUMBER"),
    ("CONTENT_HASH", "VARCHAR2"),
]


class FakeCursor:
    def __init__(self, rows=None, rows_sequence=None, schema_rows=None,
                 fetchone_results=None, rowcount=0):
        self.executed = None
        self.executed_calls = []
        self.executemany_call = None
        self.rows = rows
        self.rows_sequence = list(rows_sequence or [])
        self.schema_rows = schema_rows
        self.active_rows = None
        self.fetchone_results = fetchone_results or []
        self.rowcount = rowcount

        if self.rows is None:
            self.rows = [("local content", 0.75, "local")]
        if self.schema_rows is None:
            self.schema_rows = DEFAULT_SCHEMA_ROWS

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, **kwargs):
        self.executed = (sql, kwargs)
        self.executed_calls.append((sql, kwargs))
        if "USER_TAB_COLUMNS" in sql:
            self.active_rows = self.schema_rows
        elif self.rows_sequence:
            self.active_rows = self.rows_sequence.pop(0)
        else:
            self.active_rows = self.rows

    def executemany(self, sql, rows):
        self.executemany_call = (sql, rows)

    def fetchall(self):
        return self.active_rows if self.active_rows is not None else self.rows

    def fetchone(self):
        if self.fetchone_results:
            return self.fetchone_results.pop(0)
        return None


class FakeConnection:
    def __init__(self, rows=None, rows_sequence=None, schema_rows=None,
                 fetchone_results=None, rowcount=0):
        self.cursor_instance = FakeCursor(
            rows=rows,
            rows_sequence=rows_sequence,
            schema_rows=schema_rows,
            fetchone_results=fetchone_results,
            rowcount=rowcount,
        )
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class OracleTextFailingCursor(FakeCursor):
    def execute(self, sql, **kwargs):
        self.executed = (sql, kwargs)
        self.executed_calls.append((sql, kwargs))
        if "CONTAINS" in sql:
            raise RuntimeError("ORA-29902: Oracle Text error DRG-50901")
        if "USER_TAB_COLUMNS" in sql:
            self.active_rows = self.schema_rows
        elif self.rows_sequence:
            self.active_rows = self.rows_sequence.pop(0)
        else:
            self.active_rows = self.rows


class OracleTextFailingConnection(FakeConnection):
    def __init__(self):
        self.cursor_instance = OracleTextFailingCursor()
        self.committed = False


class FailingExecutemanyCursor(FakeCursor):
    def executemany(self, sql, rows):
        self.executemany_call = (sql, rows)
        raise RuntimeError("insert failed")


class FailingExecutemanyConnection(FakeConnection):
    def __init__(self):
        self.cursor_instance = FailingExecutemanyCursor()
        self.committed = False
        self.rolled_back = False


class FakeTavilyClient:
    def __init__(self, results=None):
        self.calls = []
        self.results = results

    def search(self, *_args, **_kwargs):
        self.calls.append((_args, _kwargs))
        if self.results is not None:
            return {"results": self.results}
        return {
            "results": [
                {
                    "content": "foreign oracle content",
                    "score": 0.91,
                    "url": "https://example.com/oracle",
                    "title": "Oracle example",
                }
            ]
        }


class FailingTavilyClient:
    def search(self, *_args, **_kwargs):
        raise AssertionError("Tavily should not be called.")


def failing_ranking_function(*_args):
    raise AssertionError("ranking_function should not be called.")


def non_schema_calls(connection):
    return [
        (sql, kwargs)
        for sql, kwargs in connection.cursor_instance.executed_calls
        if "USER_TAB_COLUMNS" not in sql
    ]

_ORACLE_OPTION_ALIASES = {
    "oracle_upsert_key": "upsert_key",
    "oracle_user": "user",
    "oracle_password": "password",
    "oracle_dsn": "dsn",
    "oracle_connection_kwargs": "connection_kwargs",
    "enable_oracle_memory_metadata": "enable_memory_metadata",
    "enable_native_hybrid_search": "enable_native_hybrid_search",
    "oracle_metadata_filters": "metadata_filters",
    "enable_oracle_json_payload": "enable_json_payload",
}


def create_oracle_client(
        api_key,
        database=None,
        connection=None,
        table_name=None,
        embedding_function=None,
        ranking_function=None,
        session=None,
        **oracle_options,
):
    """Construct the client through the supported new Oracle adapter API."""
    if database is None:
        adapter_options = {
            _ORACLE_OPTION_ALIASES.get(name, name): value
            for name, value in oracle_options.items()
        }
        database = OracleDB(
            connection=connection,
            table_name=table_name,
            **adapter_options,
        )
    elif oracle_options or connection is not None or table_name is not None:
        raise AssertionError(
            "Pass either a configured OracleDB instance or Oracle options, not both."
        )

    return TavilyHybridClient(
        api_key=api_key,
        database=database,
        embedding_function=embedding_function,
        ranking_function=ranking_function,
        session=session,
    )


def test_oracle_search_uses_vector_distance_without_touching_mongodb_collection():
    connection = FakeConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        database=OracleDB(connection=connection, table_name="tavily_documents"),
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    results = client.search("test query", max_local=1, max_foreign=0)

    sql, kwargs = connection.cursor_instance.executed
    assert "VECTOR_DISTANCE(EMBEDDINGS, :query_vector, COSINE)" in sql
    assert "FROM TAVILY_DOCUMENTS" in sql
    assert "FETCH FIRST 1 ROWS ONLY" in sql
    assert kwargs["query_vector"].typecode == "f"
    assert list(kwargs["query_vector"]) == pytest.approx([0.1, 0.2, 0.3])
    assert results == [{"content": "local content", "score": 0.75, "origin": "local"}]
    assert "CONTAINS" not in sql


def test_oracle_search_uses_configured_vector_distance():
    connection = FakeConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        vector_index_distance="DOT",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    client.search("test query", max_local=1, max_foreign=0)

    sql, _kwargs = connection.cursor_instance.executed
    assert "VECTOR_DISTANCE(EMBEDDINGS, :query_vector, DOT)" in sql
    assert "VECTOR_DISTANCE(EMBEDDINGS, :query_vector, COSINE)" not in sql


def test_oracle_native_hybrid_search_uses_oracle_text_and_metadata_filters():
    connection = FakeConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        enable_native_hybrid_search=True,
        oracle_metadata_filters={"provider_name": "tavily"},
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    client.search("test query", max_local=1, max_foreign=0)

    sql, kwargs = connection.cursor_instance.executed
    assert "CONTAINS(CONTENT, :text_query, 1) > 0" in sql
    assert "SCORE(1) / 100 AS text_score" in sql
    assert "WITH vector_candidates AS" in sql
    assert "UNION ALL" in sql
    assert "VECTOR_DISTANCE(EMBEDDINGS, :query_vector, COSINE)" in sql
    assert "PROVIDER_NAME = :metadata_filter_0" in sql
    assert kwargs["text_query"] == "test query"
    assert kwargs["metadata_filter_0"] == "tavily"


def test_oracle_native_hybrid_search_sanitizes_oracle_text_query():
    connection = FakeConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        enable_native_hybrid_search=True,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    client.search("How can Oracle VECTOR + Tavily help?", max_local=1, max_foreign=0)

    _sql, kwargs = connection.cursor_instance.executed
    assert kwargs["text_query"] == "How can Oracle VECTOR Tavily help"


def test_oracle_native_hybrid_search_falls_back_to_vector_search_on_text_error():
    connection = OracleTextFailingConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        enable_native_hybrid_search=True,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    results = client.search("How can Oracle VECTOR + Tavily help?", max_local=1, max_foreign=0)

    native_sql, native_kwargs = connection.cursor_instance.executed_calls[0]
    fallback_sql, _fallback_kwargs = connection.cursor_instance.executed_calls[-1]
    assert "CONTAINS(CONTENT, :text_query, 1) > 0" in native_sql
    assert native_kwargs["text_query"] == "How can Oracle VECTOR Tavily help"
    assert "CONTAINS" not in fallback_sql
    assert "WITH vector_candidates AS" not in fallback_sql
    assert results == [{"content": "local content", "score": 0.75, "origin": "local"}]


def test_oracle_insert_converts_embeddings_to_vector_bind():
    connection = FakeConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    oracle_database.insert_documents(client.database, [
        {
            "content": "foreign content",
            "embeddings": [0.4, 0.5, 0.6],
            "site_url": "https://example.com",
        }
    ])

    sql, rows = connection.cursor_instance.executemany_call
    assert sql == (
        "INSERT INTO TAVILY_DOCUMENTS "
        "(EMBEDDINGS, SITE_URL, CONTENT) "
        "VALUES (:EMBEDDINGS, :SITE_URL, :CONTENT)"
    )
    assert rows[0]["CONTENT"] == "foreign content"
    assert rows[0]["SITE_URL"] == "https://example.com"
    assert rows[0]["EMBEDDINGS"].typecode == "f"
    assert list(rows[0]["EMBEDDINGS"]) == pytest.approx([0.4, 0.5, 0.6])
    assert connection.committed is True


def test_oracle_save_foreign_can_store_json_payload_and_provenance_metadata():
    connection = FakeConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        enable_oracle_json_payload=True,
        enable_provenance_metadata=True,
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    client.search(
        "test query",
        max_local=0,
        max_foreign=1,
        save_foreign=True,
    )

    sql, rows = connection.cursor_instance.executemany_call
    row = rows[0]
    raw_payload = row["RAW_PAYLOAD"]
    assert "RAW_PAYLOAD" in sql
    assert row["SOURCE_URL"] == "https://example.com/oracle"
    assert row["SOURCE_TITLE"] == "Oracle example"
    assert row["RETRIEVAL_QUERY"] == "test query"
    assert row["RETRIEVAL_MODE"] == "hybrid_search"
    assert row["CACHE_HIT"] == 0
    assert row["INSERTED_FROM"] == "tavily"
    assert row["PROVIDER_NAME"] == "tavily"
    assert raw_payload
    assert '"provider_name": "tavily"' in raw_payload
    assert '"url": "https://example.com/oracle"' in raw_payload


def test_oracle_insert_orders_lob_binds_last_to_avoid_ora_24816():
    connection = FakeConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        enable_oracle_json_payload=True,
        enable_provenance_metadata=True,
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    client.search(
        "test query",
        max_local=0,
        max_foreign=1,
        save_foreign=True,
    )

    sql, rows = connection.cursor_instance.executemany_call
    content_position = sql.index(":CONTENT")
    raw_payload_position = sql.index(":RAW_PAYLOAD")
    for scalar_bind in (":EMBEDDINGS", ":SOURCE_URL", ":SOURCE_TITLE", ":PROVIDER_NAME"):
        assert sql.index(scalar_bind) < content_position
        assert sql.index(scalar_bind) < raw_payload_position
    assert rows[0]["CONTENT"] == "foreign oracle content"
    assert rows[0]["RAW_PAYLOAD"]


def test_oracle_insert_orders_all_lob_binds_last_to_avoid_ora_24816():
    schema_rows = [
        ("CONTENT", "CLOB"),
        ("EMBEDDINGS", "VECTOR"),
        ("SOURCE_URL", "VARCHAR2"),
        ("SOURCE_TITLE", "CLOB"),
    ]
    connection = FakeConnection(schema_rows=schema_rows)
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    oracle_database.insert_documents(client.database, [
        {
            "content": "foreign content",
            "embeddings": [0.4, 0.5, 0.6],
            "source_url": "https://example.com",
            "source_title": "large title payload",
        }
    ])

    sql, _rows = connection.cursor_instance.executemany_call
    assert sql.index(":EMBEDDINGS") < sql.index(":CONTENT")
    assert sql.index(":SOURCE_URL") < sql.index(":CONTENT")
    assert sql.index(":EMBEDDINGS") < sql.index(":SOURCE_TITLE")
    assert sql.index(":SOURCE_URL") < sql.index(":SOURCE_TITLE")


def test_oracle_json_payload_rejects_blob_storage_for_text_payloads():
    schema_rows = [
        ("CONTENT", "CLOB"),
        ("EMBEDDINGS", "VECTOR"),
        ("RAW_PAYLOAD", "BLOB"),
    ]
    connection = FakeConnection(schema_rows=schema_rows)
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        enable_oracle_json_payload=True,
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    with pytest.raises(ValueError, match="RAW_PAYLOAD.*JSON-compatible"):
        client.search(
            "test query",
            max_local=0,
            max_foreign=1,
            save_foreign=True,
        )

    assert connection.cursor_instance.executemany_call is None
    assert connection.committed is False


def test_oracle_insert_schema_validation_rejects_missing_columns():
    schema_rows = [
        ("CONTENT", "CLOB"),
    ]
    connection = FakeConnection(schema_rows=schema_rows)
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    with pytest.raises(ValueError, match="missing columns.*EMBEDDINGS"):
        oracle_database.insert_documents(client.database, [
            {
                "content": "foreign content",
                "embeddings": [0.4, 0.5, 0.6],
            }
        ])

    assert connection.cursor_instance.executemany_call is None
    assert connection.committed is False


def test_oracle_insert_schema_validation_rejects_wrong_vector_column_type():
    schema_rows = [
        ("CONTENT", "CLOB"),
        ("EMBEDDINGS", "CLOB"),
    ]
    connection = FakeConnection(schema_rows=schema_rows)
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    with pytest.raises(ValueError, match="EMBEDDINGS.*VECTOR-compatible"):
        oracle_database.insert_documents(client.database, [
            {
                "content": "foreign content",
                "embeddings": [0.4, 0.5, 0.6],
            }
        ])

    assert connection.cursor_instance.executemany_call is None
    assert connection.committed is False


def test_oracle_insert_rolls_back_when_normal_insert_fails():
    connection = FailingExecutemanyConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    with pytest.raises(RuntimeError, match="insert failed"):
        oracle_database.insert_documents(client.database, [
            {
                "content": "foreign content",
                "embeddings": [0.4, 0.5, 0.6],
            }
        ])

    assert connection.cursor_instance.executemany_call is not None
    assert connection.rolled_back is True
    assert connection.committed is False


def test_oracle_freshness_cache_hit_skips_tavily_and_returns_local_results():
    connection = FakeConnection(rows=[("fresh local content", 0.82, "local")])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        retrieval_mode="freshness_cache",
        cache_ttl_seconds=300,
        cache_score_threshold=0.8,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=failing_ranking_function,
    )
    client.tavily = FailingTavilyClient()

    results = client.search(
        "test query",
        max_results=2,
        max_local=1,
        max_foreign=1,
        save_foreign=True,
    )

    sql, kwargs = connection.cursor_instance.executed
    assert "EXPIRES_AT > :freshness_current_timestamp" in sql
    assert "EXPIRES_AT IS NULL" in sql
    assert "ADDED_AT >=" in sql
    assert ":freshness_current_timestamp" in sql
    assert ":cache_cutoff_timestamp" in sql
    assert kwargs["freshness_current_timestamp"] is not None
    assert kwargs["cache_cutoff_timestamp"] is not None
    assert len(non_schema_calls(connection)) == 1
    assert connection.cursor_instance.executemany_call is None
    assert results == [
        {"content": "fresh local content", "score": 0.82, "origin": "local"}
    ]


def test_oracle_freshness_cache_miss_calls_tavily_and_saves_foreign_results():
    connection = FakeConnection(rows=[("low score local content", 0.4, "local")])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        retrieval_mode="freshness_cache",
        cache_ttl_seconds=60,
        cache_score_threshold=0.8,
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=failing_ranking_function,
    )
    client.tavily = FakeTavilyClient()

    results = client.search(
        "test query",
        max_results=1,
        max_local=1,
        max_foreign=1,
        save_foreign=True,
    )

    assert len(client.tavily.calls) == 1
    sql, rows = connection.cursor_instance.executemany_call
    row = rows[0]
    assert results == [
        {"content": "foreign oracle content", "score": 0.91, "origin": "foreign"}
    ]
    assert sql == (
        "INSERT INTO TAVILY_DOCUMENTS "
        "(ADDED_AT, EMBEDDINGS, EXPIRES_AT, LAST_SEEN_AT, MEMORY_SCOPE, "
        "QUERY_COUNT, CONTENT) "
        "VALUES (:ADDED_AT, :EMBEDDINGS, :EXPIRES_AT, :LAST_SEEN_AT, "
        ":MEMORY_SCOPE, :QUERY_COUNT, :CONTENT)"
    )
    assert row["ADDED_AT"] is not None
    assert row["MEMORY_SCOPE"] == "cache_only"
    assert row["EXPIRES_AT"] > row["LAST_SEEN_AT"]
    assert row["QUERY_COUNT"] == 1
    assert row["CONTENT"] == "foreign oracle content"
    assert row["EMBEDDINGS"].typecode == "f"
    assert list(row["EMBEDDINGS"]) == pytest.approx([0.7, 0.8, 0.9])
    assert connection.committed is True


def test_oracle_freshness_cache_minimal_schema_saves_timestamp_without_memory_metadata():
    schema_rows = [
        ("ADDED_AT", "TIMESTAMP"),
        ("CONTENT", "CLOB"),
        ("EMBEDDINGS", "VECTOR"),
    ]
    connection = FakeConnection(
        rows=[("low score local content", 0.4, "local")],
        schema_rows=schema_rows,
    )
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        retrieval_mode="freshness_cache",
        cache_ttl_seconds=60,
        cache_score_threshold=0.8,
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=failing_ranking_function,
    )
    client.tavily = FakeTavilyClient()

    client.search(
        "test query",
        max_results=1,
        max_local=1,
        max_foreign=1,
        save_foreign=True,
    )

    sql, rows = connection.cursor_instance.executemany_call
    assert sql == (
        "INSERT INTO TAVILY_DOCUMENTS "
        "(ADDED_AT, EMBEDDINGS, CONTENT) "
        "VALUES (:ADDED_AT, :EMBEDDINGS, :CONTENT)"
    )
    assert rows[0]["ADDED_AT"] is not None
    assert "MEMORY_SCOPE" not in rows[0]


def test_oracle_cache_then_memory_cache_hit_skips_memory_and_tavily():
    connection = FakeConnection(rows=[("fresh cache content", 0.86, "local")])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        retrieval_mode="cache_then_memory",
        cache_ttl_seconds=300,
        cache_score_threshold=0.8,
        memory_score_threshold=0.6,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=failing_ranking_function,
    )
    client.tavily = FailingTavilyClient()

    results = client.search(
        "test query",
        max_results=2,
        max_local=1,
        max_foreign=1,
        save_foreign=True,
    )

    search_calls = non_schema_calls(connection)
    assert len(search_calls) == 1
    sql, kwargs = search_calls[0]
    assert "MEMORY_SCOPE IN (:memory_scope_0, :memory_scope_1)" in sql
    assert kwargs["memory_scope_0"] == "cache_only"
    assert kwargs["memory_scope_1"] == "cache_plus_memory"
    assert connection.cursor_instance.executemany_call is None
    assert results == [
        {"content": "fresh cache content", "score": 0.86, "origin": "local"}
    ]


def test_oracle_cache_then_memory_requires_memory_scope_column_for_scoped_lookup():
    schema_rows = [
        row for row in DEFAULT_SCHEMA_ROWS
        if row[0] != "MEMORY_SCOPE"
    ]
    connection = FakeConnection(
        rows=[("fresh cache content", 0.86, "local")],
        schema_rows=schema_rows,
    )
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        retrieval_mode="cache_then_memory",
        cache_ttl_seconds=300,
        cache_score_threshold=0.8,
        memory_score_threshold=0.6,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=failing_ranking_function,
    )
    client.tavily = FailingTavilyClient()

    with pytest.raises(ValueError, match="missing MEMORY_SCOPE"):
        client.search(
            "test query",
            max_results=2,
            max_local=1,
            max_foreign=1,
            save_foreign=True,
        )


def test_oracle_cache_then_memory_memory_hit_skips_tavily_after_cache_miss():
    connection = FakeConnection(rows_sequence=[
        [("low score cache content", 0.2, "local")],
        [("durable memory content", 0.72, "local")],
    ])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        retrieval_mode="cache_then_memory",
        cache_ttl_seconds=300,
        cache_score_threshold=0.8,
        memory_score_threshold=0.6,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=failing_ranking_function,
    )
    client.tavily = FailingTavilyClient()

    results = client.search(
        "test query",
        max_results=2,
        max_local=1,
        max_foreign=1,
        save_foreign=True,
    )

    search_calls = non_schema_calls(connection)
    assert len(search_calls) == 2
    cache_sql, cache_kwargs = search_calls[0]
    memory_sql, memory_kwargs = search_calls[1]
    assert "MEMORY_SCOPE IN (:memory_scope_0, :memory_scope_1)" in cache_sql
    assert cache_kwargs["memory_scope_0"] == "cache_only"
    assert cache_kwargs["memory_scope_1"] == "cache_plus_memory"
    assert "FETCH FIRST 1 ROWS ONLY" in memory_sql
    assert "MEMORY_SCOPE IN (:memory_scope_0)" in memory_sql
    assert memory_kwargs["memory_scope_0"] == "cache_plus_memory"
    assert connection.cursor_instance.executemany_call is None
    assert results == [
        {"content": "durable memory content", "score": 0.72, "origin": "local"}
    ]


def test_oracle_max_local_zero_falls_back_to_foreign_in_all_modes_after_cleanup():
    for retrieval_mode in (
        "hybrid_search",
        "freshness_cache",
        "cache_then_memory",
    ):
        connection = FakeConnection()
        client = create_oracle_client(
            api_key="tvly-test",
            connection=connection,
            table_name="tavily_documents",
            retrieval_mode=retrieval_mode,
            auto_cleanup_cache=True,
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
            ranking_function=failing_ranking_function,
        )
        client.tavily = FakeTavilyClient()

        results = client.search(
            "test query",
            max_results=1,
            max_local=0,
            max_foreign=1,
        )

        executed_sql = [
            sql for sql, _kwargs in connection.cursor_instance.executed_calls
        ]
        assert results == [
            {
                "content": "foreign oracle content",
                "score": 0.91,
                "origin": "foreign",
            }
        ]
        assert len(client.tavily.calls) == 1
        assert any("DELETE FROM TAVILY_DOCUMENTS" in sql for sql in executed_sql)
        assert not any(
            "VECTOR_DISTANCE" in sql or "CONTAINS(" in sql
            for sql in executed_sql
        )


def test_oracle_cache_then_memory_miss_calls_tavily_and_saves_memory_metadata():
    connection = FakeConnection(rows_sequence=[
        [],
        [],
    ])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        retrieval_mode="cache_then_memory",
        cache_ttl_seconds=300,
        cache_score_threshold=0.8,
        memory_score_threshold=0.6,
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=failing_ranking_function,
    )
    client.tavily = FakeTavilyClient()

    results = client.search(
        "test query",
        max_results=1,
        max_local=1,
        max_foreign=1,
        save_foreign=True,
    )

    sql, rows = connection.cursor_instance.executemany_call
    row = rows[0]
    assert len(client.tavily.calls) == 1
    assert "MEMORY_SCOPE" in sql
    assert row["MEMORY_SCOPE"] == "cache_plus_memory"
    assert row["EXPIRES_AT"] > row["LAST_SEEN_AT"]
    assert row["QUERY_COUNT"] == 1
    assert results == [
        {"content": "foreign oracle content", "score": 0.91, "origin": "foreign"}
    ]
    assert connection.committed is True


def test_oracle_cache_then_memory_respects_explicit_cache_only_persistence_depth():
    connection = FakeConnection(rows_sequence=[
        [],
        [],
    ])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        retrieval_mode="cache_then_memory",
        cache_ttl_seconds=300,
        cache_score_threshold=0.8,
        memory_score_threshold=0.6,
        persistence_depth="cache_only",
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=failing_ranking_function,
    )
    client.tavily = FakeTavilyClient()

    client.search(
        "test query",
        max_results=1,
        max_local=1,
        max_foreign=1,
        save_foreign=True,
    )

    _sql, rows = connection.cursor_instance.executemany_call
    assert rows[0]["MEMORY_SCOPE"] == "cache_only"


def test_oracle_save_foreign_respects_persistence_limit_and_score_threshold():
    connection = FakeConnection(rows=[])
    client = create_oracle_client(
        api_key="tvly-test",
        database=OracleDB(
            connection=connection,
            table_name="tavily_documents",
            oracle_max_persisted_foreign=1,
            oracle_persist_score_threshold=0.8,
        ),
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient(results=[
        {"content": "persisted content", "score": 0.95},
        {"content": "low score content", "score": 0.7},
        {"content": "also high content", "score": 0.9},
    ])

    results = client.search(
        "test query",
        max_results=3,
        max_local=0,
        max_foreign=3,
        save_foreign=True,
    )

    _, rows = connection.cursor_instance.executemany_call
    assert len(results) == 3
    assert len(rows) == 1
    assert rows[0]["CONTENT"] == "persisted content"


def test_oracle_source_url_upsert_uses_merge_without_requiring_provenance_flag():
    connection = FakeConnection(rows=[])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        oracle_upsert_key="source_url",
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    client.search(
        "test query",
        max_local=0,
        max_foreign=1,
        save_foreign=True,
    )

    sql, kwargs = connection.cursor_instance.executed_calls[-1]
    assert connection.cursor_instance.executemany_call is None
    assert "MERGE INTO TAVILY_DOCUMENTS target" in sql
    assert "ON (target.SOURCE_URL = source.SOURCE_URL)" in sql
    assert sql.index(":EMBEDDINGS") < sql.index(":CONTENT")
    assert kwargs["SOURCE_URL"] == "https://example.com/oracle"
    assert connection.committed is True


def test_oracle_upsert_bypasses_semantic_dedup_to_refresh_existing_keyed_rows():
    connection = FakeConnection(rows=[], fetchone_results=[(0.99,)])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        oracle_upsert_key="source_url",
        dedup_similarity_threshold=0.95,
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    client.search(
        "test query",
        max_local=0,
        max_foreign=1,
        save_foreign=True,
    )

    executed_sql = [sql for sql, _kwargs in connection.cursor_instance.executed_calls]
    assert any("MERGE INTO TAVILY_DOCUMENTS target" in sql for sql in executed_sql)
    assert not any("SELECT 1 - VECTOR_DISTANCE" in sql for sql in executed_sql)
    assert connection.committed is True


def test_oracle_upsert_does_not_null_missing_optional_metadata_columns():
    connection = FakeConnection(rows=[])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        oracle_upsert_key="source_url",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    oracle_database.insert_documents(client.database, [
        {
            "content": "first content",
            "embeddings": [0.1, 0.2, 0.3],
            "source_url": "https://example.com/one",
            "source_title": "Existing title",
        },
        {
            "content": "second content",
            "embeddings": [0.4, 0.5, 0.6],
            "source_url": "https://example.com/two",
        },
    ])

    merge_calls = [
        (sql, kwargs)
        for sql, kwargs in connection.cursor_instance.executed_calls
        if "MERGE INTO TAVILY_DOCUMENTS target" in sql
    ]
    assert len(merge_calls) == 2
    assert "target.SOURCE_TITLE = :SOURCE_TITLE" in merge_calls[0][0]
    assert "target.SOURCE_TITLE = :SOURCE_TITLE" not in merge_calls[1][0]
    assert "SOURCE_TITLE" not in merge_calls[1][1]
    assert connection.committed is True


def test_oracle_content_hash_upsert_adds_stable_hash_key():
    connection = FakeConnection(rows=[])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        oracle_upsert_key="content_hash",
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    client.search(
        "test query",
        max_local=0,
        max_foreign=1,
        save_foreign=True,
    )

    sql, kwargs = connection.cursor_instance.executed_calls[-1]
    assert "ON (target.CONTENT_HASH = source.CONTENT_HASH)" in sql
    assert len(kwargs["CONTENT_HASH"]) == 64


def test_oracle_cleanup_cache_deletes_expired_cache_only_rows():
    connection = FakeConnection(rowcount=2)
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    deleted_rows = client.database.cleanup_cache()

    sql, kwargs = connection.cursor_instance.executed_calls[-1]
    assert deleted_rows == 2
    assert "DELETE FROM TAVILY_DOCUMENTS" in sql
    assert "MEMORY_SCOPE = :memory_scope" in sql
    assert "EXPIRES_AT < :current_timestamp" in sql
    assert "MEMORY_SCOPE IS NULL" in sql
    assert "ADDED_AT < :cache_cutoff_timestamp" in sql
    assert "RETRIEVAL_MODE IN (:cleanup_retrieval_mode_0, :cleanup_retrieval_mode_1)" in sql
    assert "PROVIDER_NAME = :cleanup_provider_name" in sql
    assert "INSERTED_FROM = :cleanup_inserted_from" in sql
    assert kwargs["memory_scope"] == "cache_only"
    assert kwargs["current_timestamp"] is not None
    assert kwargs["cache_cutoff_timestamp"] is not None
    assert kwargs["cleanup_retrieval_mode_0"] == "freshness_cache"
    assert kwargs["cleanup_retrieval_mode_1"] == "cache_then_memory"
    assert kwargs["cleanup_provider_name"] == "tavily"
    assert kwargs["cleanup_inserted_from"] == "tavily"
    assert connection.committed is True


def test_oracle_cleanup_cache_deletes_legacy_cache_mode_rows_without_memory_scope():
    schema_rows = [
        row for row in DEFAULT_SCHEMA_ROWS
        if row[0] not in {"MEMORY_SCOPE", "EXPIRES_AT"}
    ]
    connection = FakeConnection(rowcount=1, schema_rows=schema_rows)
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    deleted_rows = client.database.cleanup_cache(cache_ttl_seconds=120)

    sql, kwargs = connection.cursor_instance.executed_calls[-1]
    assert deleted_rows == 1
    assert "MEMORY_SCOPE" not in sql
    assert "EXPIRES_AT" not in sql
    assert "ADDED_AT < :cache_cutoff_timestamp" in sql
    assert "RETRIEVAL_MODE IN (:cleanup_retrieval_mode_0, :cleanup_retrieval_mode_1)" in sql
    assert "PROVIDER_NAME = :cleanup_provider_name" in sql
    assert "INSERTED_FROM = :cleanup_inserted_from" in sql
    assert kwargs["cache_cutoff_timestamp"] is not None
    assert kwargs["cleanup_retrieval_mode_0"] == "freshness_cache"
    assert kwargs["cleanup_retrieval_mode_1"] == "cache_then_memory"
    assert connection.committed is True


def test_oracle_cleanup_cache_skips_legacy_rows_without_provenance_scope():
    schema_rows = [
        ("ADDED_AT", "TIMESTAMP"),
        ("CONTENT", "CLOB"),
        ("EMBEDDINGS", "VECTOR"),
    ]
    connection = FakeConnection(rowcount=5, schema_rows=schema_rows)
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    deleted_rows = client.database.cleanup_cache(cache_ttl_seconds=120)

    assert deleted_rows == 0
    assert not any(
        "DELETE FROM TAVILY_DOCUMENTS" in sql
        for sql, _kwargs in connection.cursor_instance.executed_calls
    )
    assert connection.committed is False


def test_oracle_auto_cleanup_runs_before_search_once_per_interval():
    connection = FakeConnection(rowcount=2)
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        auto_cleanup_cache=True,
        cache_cleanup_interval_seconds=3600,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    client.search("test query", max_local=1, max_foreign=0)
    client.search("test query", max_local=1, max_foreign=0)

    delete_calls = [
        (sql, kwargs)
        for sql, kwargs in connection.cursor_instance.executed_calls
        if "DELETE FROM TAVILY_DOCUMENTS" in sql
    ]
    vector_search_calls = [
        sql for sql, _ in connection.cursor_instance.executed_calls
        if "VECTOR_DISTANCE(EMBEDDINGS, :query_vector, COSINE)" in sql
    ]
    assert len(delete_calls) == 1
    assert len(vector_search_calls) == 2
    assert delete_calls[0][1]["memory_scope"] == "cache_only"


def test_oracle_save_foreign_inserts_tavily_results():
    connection = FakeConnection()
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    results = client.search(
        "test query",
        max_results=2,
        max_local=1,
        max_foreign=1,
        save_foreign=True,
    )

    sql, rows = connection.cursor_instance.executemany_call
    assert results == [
        {"content": "local content", "score": 0.75, "origin": "local"},
        {"content": "foreign oracle content", "score": 0.91, "origin": "foreign"},
    ]
    assert sql == (
        "INSERT INTO TAVILY_DOCUMENTS "
        "(EMBEDDINGS, CONTENT) "
        "VALUES (:EMBEDDINGS, :CONTENT)"
    )
    assert rows[0]["CONTENT"] == "foreign oracle content"
    assert rows[0]["EMBEDDINGS"].typecode == "f"
    assert list(rows[0]["EMBEDDINGS"]) == pytest.approx([0.7, 0.8, 0.9])
    assert connection.committed is True


def test_oracle_vector_index_helper_creates_index_when_missing():
    connection = FakeConnection(fetchone_results=[(0,)])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        vector_index_name="tavily_docs_vec_idx",
        vector_index_type="HNSW",
        vector_index_distance="COSINE",
        vector_index_neighbors=32,
        vector_index_efconstruction=200,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    created = client.database.ensure_vector_index()

    assert created is True
    assert connection.committed is True
    _, create_kwargs = connection.cursor_instance.executed_calls[-1]
    assert "DBMS_VECTOR.CREATE_INDEX" in connection.cursor_instance.executed_calls[-1][0]
    assert create_kwargs["index_name"] == "TAVILY_DOCS_VEC_IDX"
    assert create_kwargs["table_name"] == "TAVILY_DOCUMENTS"
    assert create_kwargs["idx_vector_col"] == "EMBEDDINGS"
    assert create_kwargs["idx_organization"] == "INMEMORY NEIGHBOR GRAPH"
    assert create_kwargs["idx_distance_metric"] == "COSINE"
    assert '"type": "HNSW"' in create_kwargs["idx_parameters"]
    assert '"neighbors": 32' in create_kwargs["idx_parameters"]
    assert '"efConstruction": 200' in create_kwargs["idx_parameters"]


def test_oracle_vector_index_helper_skips_existing_index():
    connection = FakeConnection(fetchone_results=[(1,)])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        vector_index_name="tavily_docs_vec_idx",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    created = client.database.ensure_vector_index()

    assert created is False
    assert connection.committed is False
    assert len(connection.cursor_instance.executed_calls) == 1


def test_oracle_text_index_helper_creates_index_when_missing():
    connection = FakeConnection(fetchone_results=[(0,)])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        text_index_name="tavily_docs_text_idx",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    created = client.database.ensure_text_index()

    assert created is True
    assert connection.committed is True
    lookup_sql, lookup_kwargs = connection.cursor_instance.executed_calls[0]
    create_sql, create_kwargs = connection.cursor_instance.executed_calls[-1]
    assert "USER_INDEXES" in lookup_sql
    assert lookup_kwargs["index_name"] == "TAVILY_DOCS_TEXT_IDX"
    assert "CREATE INDEX TAVILY_DOCS_TEXT_IDX" in create_sql
    assert "ON TAVILY_DOCUMENTS(CONTENT)" in create_sql
    assert "INDEXTYPE IS CTXSYS.CONTEXT" in create_sql
    assert create_kwargs == {}


def test_oracle_text_index_helper_skips_existing_index():
    connection = FakeConnection(fetchone_results=[(1,)])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        text_index_name="tavily_docs_text_idx",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
        ranking_function=lambda _, documents, __: documents,
    )

    created = client.database.ensure_text_index()

    assert created is False
    assert connection.committed is False
    assert len(connection.cursor_instance.executed_calls) == 1


def test_oracle_semantic_dedup_skips_near_duplicate_foreign_insert():
    connection = FakeConnection(rows=[], fetchone_results=[(0.99,)])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        dedup_similarity_threshold=0.95,
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    results = client.search(
        "test query",
        max_local=0,
        max_foreign=1,
        save_foreign=True,
    )

    assert results == [
        {"content": "foreign oracle content", "score": 0.91, "origin": "foreign"}
    ]
    assert connection.cursor_instance.executemany_call is None
    assert connection.committed is False
    assert "FETCH FIRST 1 ROWS ONLY" in connection.cursor_instance.executed_calls[-1][0]


def test_oracle_semantic_dedup_is_scoped_to_tavily_rows_and_live_cache():
    connection = FakeConnection(rows=[], fetchone_results=[(0.99,)])
    client = create_oracle_client(
        api_key="tvly-test",
        connection=connection,
        table_name="tavily_documents",
        dedup_similarity_threshold=0.95,
        oracle_metadata_filters={"provider_name": "tavily"},
        embedding_function=lambda texts, _: [[0.7, 0.8, 0.9] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    client.search(
        "test query",
        max_local=0,
        max_foreign=1,
        save_foreign=True,
    )

    sql, kwargs = connection.cursor_instance.executed_calls[-1]
    assert "PROVIDER_NAME = :metadata_filter_0" in sql
    assert "PROVIDER_NAME = :dedup_provider_name" in sql
    assert "INSERTED_FROM = :dedup_inserted_from" in sql
    assert "MEMORY_SCOPE <> :dedup_cache_only_scope" in sql
    assert "EXPIRES_AT > :dedup_current_timestamp" in sql
    assert kwargs["metadata_filter_0"] == "tavily"
    assert kwargs["dedup_provider_name"] == "tavily"
    assert kwargs["dedup_inserted_from"] == "tavily"
    assert kwargs["dedup_cache_only_scope"] == "cache_only"
    assert kwargs["dedup_current_timestamp"] is not None


def test_oracle_cache_and_dedup_similarity_thresholds_require_cosine_distance():
    with pytest.raises(ValueError, match="require vector_index_distance='COSINE'"):
        create_oracle_client(
            api_key="tvly-test",
            connection=FakeConnection(),
            table_name="tavily_documents",
            retrieval_mode="freshness_cache",
            vector_index_distance="DOT",
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
            ranking_function=lambda _, documents, __: documents,
        )

    with pytest.raises(ValueError, match="require vector_index_distance='COSINE'"):
        create_oracle_client(
            api_key="tvly-test",
            connection=FakeConnection(),
            table_name="tavily_documents",
            vector_index_distance="DOT",
            dedup_similarity_threshold=0.95,
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3]],
            ranking_function=lambda _, documents, __: documents,
        )


def test_oracle_rejects_unsafe_identifiers():
    with pytest.raises(ValueError, match="Invalid Oracle identifier"):
        create_oracle_client(
            api_key="tvly-test",
            connection=FakeConnection(),
            table_name="documents; drop table users",
        )
