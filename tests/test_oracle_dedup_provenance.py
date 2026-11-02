# Copyright (c) 2026, Oracle and/or its affiliates.

import json
import math
import sqlite3

import pytest

from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from tests.test_hybrid_rag_oracle import FakeConnection, FakeCursor, FakeTavilyClient


ORIGIN_COLUMNS = ("PROVIDER_NAME", "INSERTED_FROM")
MARKER_SCHEMAS = [
    pytest.param(ORIGIN_COLUMNS, id="both-markers"),
    pytest.param(("PROVIDER_NAME",), id="provider-only"),
    pytest.param(("INSERTED_FROM",), id="inserted-from-only"),
    pytest.param((), id="no-markers"),
]


def cosine_distance(left, right, metric):
    assert metric == "COSINE"
    left, right = json.loads(left), json.loads(right)
    dot = sum(a * b for a, b in zip(left, right))
    norms = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return 1 - dot / norms


def sqlite_bindings(bindings):
    return {
        key: json.dumps(list(value)) if key in {"EMBEDDINGS", "query_vector"} else value
        for key, value in bindings.items()
    }


class StoredRowsCursor(FakeCursor):
    """Execute emitted insert and dedup predicates against nullable stored rows."""

    def __init__(self, marker_columns):
        schema_rows = [
            ("CONTENT", "CLOB"), ("EMBEDDINGS", "VECTOR"), ("TENANT_ID", "VARCHAR2")
        ] + [(column, "VARCHAR2") for column in marker_columns]
        super().__init__(rows=[], schema_rows=schema_rows)
        self.store = sqlite3.connect(":memory:")
        self.store.row_factory = sqlite3.Row
        self.store.create_function("VECTOR_DISTANCE", 3, cosine_distance)
        self.store.execute(
            "CREATE TABLE TAVILY_DOCUMENTS ("
            + ", ".join(column + " TEXT" for column, _ in schema_rows) + ")"
        )

    def execute(self, sql, **kwargs):
        super().execute(sql, **kwargs)
        if "USER_TAB_COLUMNS" not in sql:
            # Adapt Oracle syntax, preserving the actual WHERE predicates.
            sql = sql.replace(", COSINE)", ", 'COSINE')").replace(
                "FETCH FIRST 1 ROWS ONLY", "LIMIT 1"
            )
            self.active_rows = self.store.execute(sql, sqlite_bindings(kwargs)).fetchall()

    def executemany(self, sql, rows):
        super().executemany(sql, rows)
        self.store.executemany(sql, [sqlite_bindings(row) for row in rows])

    def fetchone(self):
        return self.active_rows[0] if self.active_rows else None


class StoredRowsConnection(FakeConnection):
    def __init__(self, marker_columns):
        super().__init__()
        self.cursor_instance = StoredRowsCursor(marker_columns)

    def commit(self):
        self.cursor_instance.store.commit()
        super().commit()

    def rollback(self):
        self.cursor_instance.store.rollback()
        super().rollback()


@pytest.fixture
def connection_factory():
    connections = []

    def create(marker_columns):
        connection = StoredRowsConnection(marker_columns)
        connections.append(connection)
        return connection

    yield create
    for connection in connections:
        connection.cursor_instance.store.close()


def make_client(connection, **options):
    database = OracleDB(
        connection=connection,
        table_name="tavily_documents",
        metadata_filters={"TENANT_ID": "A"},
        **options,
    )
    client = TavilyHybridClient(
        "tvly-test", database=database,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    # No URL, so source-url upserts must use the semantic-dedup insert fallback.
    client.tavily = FakeTavilyClient(results=[{"content": "same content", "score": 0.9}])
    return client


def save_result(client, save_foreign=True):
    return client.search("query", max_local=0, max_foreign=1, save_foreign=save_foreign)


@pytest.mark.parametrize("marker_columns", MARKER_SCHEMAS)
@pytest.mark.parametrize("threshold", [pytest.param(0.95, id="dedup"), None])
@pytest.mark.parametrize("upsert_key", [None, "source_url"])
def test_dedup_round_trip_without_provenance(
        connection_factory, marker_columns, threshold, upsert_key
):
    connection = connection_factory(marker_columns)
    client = make_client(
        connection, dedup_similarity_threshold=threshold, upsert_key=upsert_key
    )

    save_result(client)
    assert connection.cursor_instance.store.execute(
        "SELECT COUNT(*) FROM TAVILY_DOCUMENTS"
    ).fetchone()[0] == 1
    save_result(client)

    stored = connection.cursor_instance.store.execute(
        "SELECT * FROM TAVILY_DOCUMENTS"
    ).fetchall()
    assert len(stored) == (1 if threshold is not None else 2)
    assert stored[0]["TENANT_ID"] == "A"
    for column in marker_columns:
        assert stored[0][column] == ("tavily" if threshold is not None else None)
    assert client.database.enable_provenance_metadata is False


@pytest.mark.parametrize("marker_columns", MARKER_SCHEMAS[:3])
def test_dedup_preserves_origin_and_tenant_boundaries(connection_factory, marker_columns):
    connection = connection_factory(marker_columns)
    client = make_client(connection, dedup_similarity_threshold=0.95)
    store = connection.cursor_instance.store
    columns = ["CONTENT", "EMBEDDINGS", "TENANT_ID", *marker_columns]
    store.executemany(
        "INSERT INTO TAVILY_DOCUMENTS (" + ", ".join(columns) + ") VALUES ("
        + ", ".join("?" for _ in columns) + ")",
        [
            ["same content", json.dumps([0.1, 0.2, 0.3]), tenant]
            + [origin] * len(marker_columns)
            for tenant, origin in [("B", "tavily"), ("A", "other")]
        ],
    )

    save_result(client)
    save_result(client)

    stored = [dict(row) for row in store.execute("SELECT * FROM TAVILY_DOCUMENTS")]
    assert len(stored) == 3
    assert len([
        row for row in stored
        if row["TENANT_ID"] == "A"
        and all(row[column] == "tavily" for column in marker_columns)
    ]) == 1


@pytest.mark.parametrize("write_path", ["direct", "callback"])
def test_arbitrary_documents_are_not_automatically_marked_tavily(
        connection_factory, write_path
):
    connection = connection_factory(ORIGIN_COLUMNS)
    client = make_client(connection, dedup_similarity_threshold=0.95)
    document = {"content": "external content", "embeddings": [0.1, 0.2, 0.3]}

    if write_path == "direct":
        client.database.persist([document])
    else:
        save_result(client, save_foreign=lambda _: document)

    stored = connection.cursor_instance.store.execute(
        "SELECT * FROM TAVILY_DOCUMENTS"
    ).fetchone()
    assert stored["PROVIDER_NAME"] is None
    assert stored["INSERTED_FROM"] is None
    assert set(document) == {"content", "embeddings"}


@pytest.mark.parametrize("upsert_key", ["source_url", "content_hash"])
def test_keyed_upserts_still_bypass_semantic_dedup_without_provenance(upsert_key):
    connection = FakeConnection(fetchone_results=[(0.99,)])
    client = make_client(connection, dedup_similarity_threshold=0.95, upsert_key=upsert_key)
    client.tavily = FakeTavilyClient()
    # The default fake schema has no tenant column; this test focuses on keyed writes.
    client.database.oracle_metadata_filters = {}

    save_result(client)

    calls = connection.cursor_instance.executed_calls
    assert not any("SELECT 1 - VECTOR_DISTANCE" in sql for sql, _ in calls)
    sql, bindings = calls[-1]
    assert "MERGE INTO" in sql
    # Keyed rows must also remain visible to later unkeyed deduplication.
    assert bindings["PROVIDER_NAME"] == "tavily"
    assert bindings["INSERTED_FROM"] == "tavily"
