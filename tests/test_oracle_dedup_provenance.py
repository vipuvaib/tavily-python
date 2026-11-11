# Copyright (c) 2026, Oracle and/or its affiliates.

import array
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3

import pytest

from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from tavily.databases.oracledb import oracledb as oracle_database
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
        key: json.dumps(list(value)) if isinstance(value, array.array)
        else value.isoformat(" ") if isinstance(value, datetime) else value
        for key, value in bindings.items()
    }


class StoredRowsCursor(FakeCursor):
    """Execute emitted insert and dedup predicates against nullable stored rows."""

    def __init__(self, marker_columns, extra_columns=()):
        schema_rows = [
            ("CONTENT", "CLOB"), ("EMBEDDINGS", "VECTOR"), ("TENANT_ID", "VARCHAR2")
        ] + list(extra_columns) + [(column, "VARCHAR2") for column in marker_columns]
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
                "FETCH EXACT FIRST 1 ROWS ONLY", "LIMIT 1"
            ).replace("FROM DUAL", "")
            self.active_rows = self.store.execute(sql, sqlite_bindings(kwargs)).fetchall()

    def executemany(self, sql, rows):
        super().executemany(sql, rows)
        self.store.executemany(sql, [sqlite_bindings(row) for row in rows])

    def fetchone(self):
        return self.active_rows[0] if self.active_rows else None


class StoredRowsConnection(FakeConnection):
    def __init__(self, marker_columns, extra_columns=()):
        super().__init__()
        self.cursor_instance = StoredRowsCursor(marker_columns, extra_columns)

    def commit(self):
        self.cursor_instance.store.commit()
        super().commit()

    def rollback(self):
        self.cursor_instance.store.rollback()
        super().rollback()


@pytest.fixture
def connection_factory():
    connections = []

    def create(marker_columns, extra_columns=()):
        connection = StoredRowsConnection(marker_columns, extra_columns)
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


@pytest.mark.parametrize("filters, existing_owner, existing_region, owner, region", [
    pytest.param(
        {"tenant_id": ["A", "B"]}, "B", "east", "A", "east",
        id="list-and-case-normalization",
    ),
    pytest.param(
        {"TENANT_ID": ("A", "B")}, "A", "east", "B", "east",
        id="tuple",
    ),
    pytest.param(
        {"TENANT_ID": {"A", "B"}}, "B", "east", "A", "east",
        id="set",
    ),
    pytest.param(
        {"TENANT_ID": ["A", "B"], "REGION": ["east", "west"]},
        "B", "east", "A", "east", id="compound-different-tenant",
    ),
    pytest.param(
        {"TENANT_ID": ["A", "B"], "REGION": ["east", "west"]},
        "A", "west", "A", "east", id="compound-different-region",
    ),
    pytest.param(
        {"TENANT_ID": None, "region": ["east", "west"]},
        None, "west", None, "east", id="null-owner-and-region-list",
    ),
    pytest.param(
        {"TENANT_ID": "A", "REGION": ["east", "west"]},
        "A", "west", "A", "east", id="scalar-owner-and-region-list",
    ),
    pytest.param(
        {"tenant_id": None}, "A", "east", None, "east",
        id="null-owner",
    ),
])
@pytest.mark.parametrize("upsert_key", [None, "source_url", "content_hash"])
def test_semantic_dedup_matches_exact_document_scope(
        connection_factory, filters, existing_owner, existing_region,
        owner, region, upsert_key):
    connection = connection_factory(ORIGIN_COLUMNS, [("REGION", "VARCHAR2")])
    database = OracleDB(
        connection=connection,
        table_name="tavily_documents",
        metadata_filters=filters,
        dedup_similarity_threshold=0.95,
        upsert_key=upsert_key,
    )
    store = connection.cursor_instance.store
    store.execute(
        "INSERT INTO TAVILY_DOCUMENTS "
        "(CONTENT, EMBEDDINGS, TENANT_ID, REGION, PROVIDER_NAME, INSERTED_FROM) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("same content", json.dumps([0.1, 0.2, 0.3]), existing_owner,
         existing_region, "tavily", "tavily"),
    )
    candidate = {
        "content": "same content", "embeddings": [0.1, 0.2, 0.3],
        "tenant_id": owner, "region": region,
        "provider_name": "tavily", "inserted_from": "tavily",
    }

    database.persist([candidate])
    database.persist([candidate])

    stored = list(store.execute("SELECT TENANT_ID, REGION FROM TAVILY_DOCUMENTS"))
    assert len(stored) == 2
    assert sum(
        row["TENANT_ID"] == owner and row["REGION"] == region for row in stored
    ) == 1
    assert set(candidate) == {
        "content", "embeddings", "tenant_id", "region", "provider_name", "inserted_from"
    }


def test_semantic_dedup_without_configured_scope_remains_global(connection_factory):
    connection = connection_factory(ORIGIN_COLUMNS)
    database = OracleDB(
        connection=connection, table_name="tavily_documents",
        dedup_similarity_threshold=0.95,
    )
    store = connection.cursor_instance.store
    store.execute(
        "INSERT INTO TAVILY_DOCUMENTS "
        "(CONTENT, EMBEDDINGS, TENANT_ID, PROVIDER_NAME, INSERTED_FROM) "
        "VALUES (?, ?, ?, ?, ?)",
        ("same content", json.dumps([0.1, 0.2, 0.3]), "B", "tavily", "tavily"),
    )

    database.persist([{
        "content": "same content", "embeddings": [0.1, 0.2, 0.3],
        "tenant_id": "A", "provider_name": "tavily", "inserted_from": "tavily",
    }])

    assert list(store.execute("SELECT TENANT_ID FROM TAVILY_DOCUMENTS"))[0][0] == "B"
    assert store.execute("SELECT COUNT(*) FROM TAVILY_DOCUMENTS").fetchone()[0] == 1


def batch_document(content, embedding=None, **fields):
    return {
        "content": content,
        "embeddings": [1.0, 0.0, 0.0] if embedding is None else embedding,
        "provider_name": "tavily", "inserted_from": "tavily",
        **fields,
    }


@pytest.mark.parametrize("marker_columns", MARKER_SCHEMAS)
@pytest.mark.parametrize("upsert_key", [None, "source_url", "content_hash"])
@pytest.mark.parametrize("second_vector", [[1.0, 0.0, 0.0], [0.99, 0.01, 0.0]],
                         ids=["identical", "near-identical"])
def test_batch_dedup_keeps_first_similar_candidate(
        connection_factory, marker_columns, upsert_key, second_vector):
    connection = connection_factory(marker_columns)
    database = OracleDB(
        connection=connection, table_name="tavily_documents",
        metadata_filters={"TENANT_ID": "A"},
        dedup_similarity_threshold=0.95, upsert_key=upsert_key,
    )
    documents = [
        batch_document("first"),
        batch_document("similar", second_vector),
        batch_document("different", [0.0, 1.0, 0.0]),
    ]
    for document in documents:
        for column in set(ORIGIN_COLUMNS) - set(marker_columns):
            document.pop(column.lower())

    database.persist(documents)

    stored = list(connection.cursor_instance.store.execute(
        "SELECT CONTENT FROM TAVILY_DOCUMENTS ORDER BY rowid"
    ))
    assert [row[0] for row in stored] == ["first", "different"]


def test_batch_dedup_disabled_keeps_similar_candidates(connection_factory):
    connection = connection_factory(ORIGIN_COLUMNS)
    database = OracleDB(connection=connection, table_name="tavily_documents")

    database.persist([batch_document("first"), batch_document("second")])

    assert connection.cursor_instance.store.execute(
        "SELECT COUNT(*) FROM TAVILY_DOCUMENTS"
    ).fetchone()[0] == 2
    assert not any(
        "VECTOR_DISTANCE" in sql
        for sql, _ in connection.cursor_instance.executed_calls
    )


def test_batch_dedup_compares_only_previously_accepted_candidates(connection_factory):
    connection = connection_factory(ORIGIN_COLUMNS)
    database = OracleDB(
        connection=connection, table_name="tavily_documents",
        dedup_similarity_threshold=0.9,
    )
    database.persist([
        batch_document("zero-degrees"),
        batch_document("twenty-degrees", [0.9396926, 0.3420201, 0.0]),
        batch_document("forty-degrees", [0.7660444, 0.6427876, 0.0]),
    ])

    assert [row[0] for row in connection.cursor_instance.store.execute(
        "SELECT CONTENT FROM TAVILY_DOCUMENTS ORDER BY rowid"
    )] == ["zero-degrees", "forty-degrees"]


@pytest.mark.parametrize("filters, owners", [
    ({"TENANT_ID": ["A", "B"], "REGION": ["east", "west"]},
     [("A", "east"), ("B", "east"), ("A", "west")]),
    ({"TENANT_ID": None, "REGION": ["east", "west"]},
     [(None, "east"), (None, "west")]),
])
def test_batch_dedup_preserves_exact_owner_scope(connection_factory, filters, owners):
    connection = connection_factory(ORIGIN_COLUMNS, [("REGION", "VARCHAR2")])
    database = OracleDB(
        connection=connection, table_name="tavily_documents",
        metadata_filters=filters, dedup_similarity_threshold=0.95,
    )
    documents = [
        batch_document(f"{tenant}-{region}-{copy}", tenant_id=tenant, region=region)
        for tenant, region in owners for copy in range(2)
    ]

    database.persist(documents)

    stored = connection.cursor_instance.store.execute(
        "SELECT TENANT_ID, REGION, COUNT(*) AS N FROM TAVILY_DOCUMENTS "
        "GROUP BY TENANT_ID, REGION"
    ).fetchall()
    assert {(row["TENANT_ID"], row["REGION"]) for row in stored} == set(owners)
    assert all(row["N"] == 1 for row in stored)


@pytest.mark.parametrize("reference_fields, expected_rows", [
    ({"provider_name": "other"}, 2),
    ({"inserted_from": "other"}, 2),
    ({"provider_name": None}, 2),
    ({"inserted_from": None}, 2),
    ({"memory_scope": "cache_only", "expires_at": -1}, 2),
    ({"memory_scope": "cache_only", "expires_at": 0}, 2),
    ({"memory_scope": "cache_only", "expires_at": 1}, 1),
    ({"memory_scope": "cache_plus_memory", "expires_at": -1}, 1),
    ({"memory_scope": None, "expires_at": None}, 1),
])
def test_batch_dedup_respects_reference_origin_and_expiration(
        connection_factory, monkeypatch, reference_fields, expected_rows):
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(oracle_database, "utc_now", lambda: now)
    connection = connection_factory(
        ORIGIN_COLUMNS, [("MEMORY_SCOPE", "VARCHAR2"), ("EXPIRES_AT", "TIMESTAMP")]
    )
    database = OracleDB(
        connection=connection, table_name="tavily_documents",
        metadata_filters={"TENANT_ID": "A"}, dedup_similarity_threshold=0.95,
    )
    fields = dict(reference_fields)
    if isinstance(fields.get("expires_at"), int):
        fields["expires_at"] = now + timedelta(days=fields["expires_at"])

    database.persist([
        batch_document("first", **fields),
        batch_document("second"),
    ])

    assert connection.cursor_instance.store.execute(
        "SELECT COUNT(*) FROM TAVILY_DOCUMENTS"
    ).fetchone()[0] == expected_rows


@pytest.mark.parametrize("upsert_key", ["source_url", "content_hash"])
def test_batch_keyed_upserts_still_bypass_semantic_dedup(upsert_key):
    connection = FakeConnection()
    database = OracleDB(
        connection=connection, table_name="tavily_documents",
        dedup_similarity_threshold=0.95, upsert_key=upsert_key,
    )

    database.persist([
        batch_document("first", **{upsert_key: "key-one"}),
        batch_document("second", **{upsert_key: "key-two"}),
    ])

    calls = connection.cursor_instance.executed_calls
    assert sum("MERGE INTO" in sql for sql, _ in calls) == 2
    assert not any("VECTOR_DISTANCE" in sql for sql, _ in calls)


def test_batch_dedup_preserves_insert_rollback(connection_factory):
    connection = connection_factory(ORIGIN_COLUMNS)
    store = connection.cursor_instance.store
    store.execute(
        "CREATE TRIGGER reject_invalid BEFORE INSERT ON TAVILY_DOCUMENTS "
        "WHEN NEW.CONTENT = 'invalid' BEGIN "
        "SELECT RAISE(ABORT, 'rejected test row'); END"
    )
    database = OracleDB(
        connection=connection, table_name="tavily_documents",
        dedup_similarity_threshold=0.95,
    )

    with pytest.raises(sqlite3.IntegrityError, match="rejected test row"):
        database.persist([
            batch_document("first"),
            batch_document("duplicate"),
            batch_document("invalid", [0.0, 1.0, 0.0]),
        ])

    assert store.execute("SELECT COUNT(*) FROM TAVILY_DOCUMENTS").fetchone()[0] == 0
    assert connection.committed is False
