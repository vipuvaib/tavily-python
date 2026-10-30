# Copyright (c) 2026, Oracle and/or its affiliates.

import re
import sqlite3

import pytest

from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from tavily.databases.oracledb import oracledb as oracle_database
from tests.test_hybrid_rag_oracle import (
    DEFAULT_SCHEMA_ROWS,
    FakeConnection,
    FakeTavilyClient,
)

MISSING = object()


def make_database(filters, upsert_key="source_url", **options):
    connection = FakeConnection(
        schema_rows=DEFAULT_SCHEMA_ROWS + [
            ("TENANT_ID", "VARCHAR2"), ("REGION", "VARCHAR2")
        ],
        fetchone_results=[(1.0,)] * 4,
    )
    return OracleDB(
        connection=connection,
        table_name="tavily_documents",
        metadata_filters=filters,
        upsert_key=upsert_key,
        **options,
    ), connection


def document(tenant=MISSING, keyed=True, **fields):
    result = {"content": "new content", "embeddings": [0.1, 0.2, 0.3]}
    if keyed:
        result.update(source_url="shared-url", content_hash="shared-hash")
    if tenant is not MISSING:
        result["tenant_id"] = tenant
    result.update(fields)
    return result


def assert_no_writes(connection):
    assert connection.cursor_instance.executemany_call is None
    assert not any(
        "MERGE INTO" in sql or "INSERT INTO" in sql
        for sql, _ in connection.cursor_instance.executed_calls
    )
    assert connection.committed is False


@pytest.mark.parametrize("upsert_key, key_column", [
    ("source_url", "SOURCE_URL"), ("content_hash", "CONTENT_HASH")
])
@pytest.mark.parametrize("filters, tenant, region", [
    ({"TENANT_ID": "A"}, "A", "east"),
    ({"tenant_id": None}, None, "east"),
    ({"TENANT_ID": ["A", "B"]}, "A", "east"),
    ({"TENANT_ID": ["A", "B"]}, "B", "east"),
    ({"TENANT_ID": ("A", "B")}, "A", "east"),
    ({"TENANT_ID": {"A", "B"}}, "B", "east"),
    ({"TENANT_ID": "A", "REGION": "east"}, "A", "east"),
])
def test_merge_matches_exact_document_owner(
        upsert_key, key_column, filters, tenant, region
):
    database, connection = make_database(filters, upsert_key)
    database.persist([document(tenant, region=region)])
    sql, bindings = connection.cursor_instance.executed_calls[-1]
    condition = re.search(r"\bON \((.*?)\)\s*WHEN", sql, re.S).group(1)
    update = sql.split("WHEN MATCHED THEN UPDATE SET", 1)[1].split(
        "WHEN NOT MATCHED", 1
    )[0]
    for column in filters:
        assert "target." + column.upper() + " =" not in update

    # Evaluate the emitted ON predicate against colliding tenant rows. This
    # catches allowed-list matching that still crosses individual owners.
    candidates = [
        (1, "A", "east"), (2, "B", "east"), (3, None, "east"),
        (4, "A", "west"), (5, "C", "east"),
    ]
    with sqlite3.connect(":memory:") as rows:
        rows.execute(
            "CREATE TABLE documents (ROW_ID INTEGER, TENANT_ID TEXT, "
            "REGION TEXT, " + key_column + " TEXT)"
        )
        rows.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?)",
            [row + (bindings[key_column],) for row in candidates],
        )
        rows.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?)",
            (6, tenant, region, "different-key"),
        )
        scalar_bindings = {
            key: value for key, value in bindings.items()
            if key in {key_column, "TENANT_ID", "REGION"}
        }
        matched = {
            row[0] for row in rows.execute(
                "SELECT target.ROW_ID FROM documents target CROSS JOIN "
                "(SELECT :" + key_column + " AS " + key_column + ") source "
                "WHERE " + condition,
                scalar_bindings,
            )
        }
    expected = {
        row_id for row_id, owner, row_region in candidates
        if owner == tenant and ("REGION" not in filters or row_region == region)
    }
    assert matched == expected
    assert bindings["TENANT_ID"] == tenant
    assert connection.committed is True


@pytest.mark.parametrize("upsert_key, keyed", [
    (None, True), ("source_url", True), ("content_hash", True),
    ("source_url", False), ("content_hash", False),
])
@pytest.mark.parametrize("owner", ["A", None])
def test_missing_scalar_scope_is_filled_for_every_write_path(upsert_key, keyed, owner):
    database, connection = make_database({"tenant_id": owner}, upsert_key)
    original = document(keyed=keyed)
    database.persist([original])
    if upsert_key and keyed:
        _, persisted = connection.cursor_instance.executed_calls[-1]
    else:
        _, persisted_rows = connection.cursor_instance.executemany_call
        persisted = persisted_rows[0]
    assert "tenant_id" not in original
    assert persisted["TENANT_ID"] == owner


@pytest.mark.parametrize("filters, invalid_owner", [
    ({"TENANT_ID": "A"}, "B"),
    ({"TENANT_ID": "A"}, None),
    ({"TENANT_ID": None}, "A"),
    ({"TENANT_ID": ["A", "B"]}, "C"),
    ({"TENANT_ID": ["A", "B"]}, None),
    ({"TENANT_ID": ["A", "B"]}, MISSING),
    ({"TENANT_ID": ("A",)}, MISSING),
    ({"TENANT_ID": {"A", "B"}}, MISSING),
    ({"TENANT_ID": []}, MISSING),
    ({"TENANT_ID": ()}, MISSING),
    ({"TENANT_ID": set()}, MISSING),
    ({"TENANT_ID": [None]}, None),
    ({"TENANT_ID": "A"}, ""),
    ({"TENANT_ID": ""}, MISSING),
])
@pytest.mark.parametrize("write_path", ["persist", "insert", "fallback"])
def test_rejects_entire_invalid_batch_before_writes_or_dedup(
        filters, invalid_owner, write_path
):
    database, connection = make_database(
        filters, dedup_similarity_threshold=0.95
    )
    first_owner = None if filters["TENANT_ID"] is None else "A"
    documents = [
        document(first_owner, keyed=write_path != "fallback"),
        document(invalid_owner, keyed=write_path != "fallback"),
    ]
    with pytest.raises(ValueError, match="persistence scope"):
        if write_path == "insert":
            oracle_database.insert_documents(database, documents)
        else:
            database.persist(documents)
    assert_no_writes(connection)
    assert not any(
        "SELECT 1 - VECTOR_DISTANCE" in sql
        for sql, _ in connection.cursor_instance.executed_calls
    )


@pytest.mark.parametrize("upsert_key", [None, "source_url", "content_hash"])
@pytest.mark.parametrize("owner", ["A", None])
def test_generated_foreign_documents_inherit_scalar_scope(upsert_key, owner):
    database, connection = make_database({"TENANT_ID": owner}, upsert_key)
    client = TavilyHybridClient(
        "tvly-test", database=database,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for text in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()
    client.search("query", max_local=0, max_foreign=1, save_foreign=True)
    if upsert_key:
        _, persisted = connection.cursor_instance.executed_calls[-1]
    else:
        _, persisted_rows = connection.cursor_instance.executemany_call
        persisted = persisted_rows[0]
    assert persisted["TENANT_ID"] == owner


@pytest.mark.parametrize("upsert_key", [None, "source_url", "content_hash"])
def test_callback_cannot_reassign_ownership_or_partially_write_batch(upsert_key):
    database, connection = make_database({"TENANT_ID": "A"}, upsert_key)
    client = TavilyHybridClient(
        "tvly-test", database=database,
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for text in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient(results=[
        {"content": "allowed", "score": 0.9},
        {"content": "conflicting", "score": 0.9},
    ])

    def save_foreign(result):
        return document(
            "A" if result["content"] == "allowed" else "B",
            content=result["content"], embeddings=result["embeddings"],
        )

    with pytest.raises(ValueError, match="persistence scope"):
        client.search("query", max_local=0, max_foreign=2, save_foreign=save_foreign)
    assert_no_writes(connection)


def test_scoped_insert_fallback_and_merge_share_batch_validation():
    database, connection = make_database({"TENANT_ID": "A"})
    with pytest.raises(ValueError, match="persistence scope"):
        database.persist([document("A", keyed=False), document("B")])
    assert_no_writes(connection)


def test_missing_scope_column_fails_before_writes():
    database = OracleDB(
        connection=FakeConnection(), table_name="tavily_documents",
        metadata_filters={"TENANT_ID": "A"}, upsert_key="source_url",
    )
    with pytest.raises(ValueError, match="missing columns.*TENANT_ID"):
        database.persist([document()])
    assert_no_writes(database.connection)


def test_case_aliases_cannot_hide_conflicting_ownership():
    database, connection = make_database({"TENANT_ID": "A"})
    with pytest.raises(ValueError, match="persistence scope"):
        database.persist([document("B", TENANT_ID="A")])
    assert_no_writes(connection)


def test_conflicting_case_aliases_in_filters_fail_before_writes():
    database, connection = make_database({"tenant_id": "A", "TENANT_ID": "B"})
    with pytest.raises(ValueError, match="persistence scope"):
        database.persist([document()])
    assert_no_writes(connection)


@pytest.mark.parametrize("upsert_key", [None, "source_url", "content_hash"])
def test_no_filters_preserves_unscoped_persistence(upsert_key):
    database, connection = make_database({}, upsert_key)
    database.persist([document()])
    if upsert_key:
        sql, bindings = connection.cursor_instance.executed_calls[-1]
        key = "SOURCE_URL" if upsert_key == "source_url" else "CONTENT_HASH"
        assert "ON (target." + key + " = source." + key + ")" in sql
    else:
        _, persisted_rows = connection.cursor_instance.executemany_call
        bindings = persisted_rows[0]
    assert "TENANT_ID" not in bindings
