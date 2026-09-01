# Copyright (c) 2026, Oracle and/or its affiliates.

import pytest
import requests

from tavily import TavilyHybridClient
from tavily.databases.mongodb import MongoDB
from tavily.databases.oracledb import OracleDB
from tavily.databases.oracledb import oracledb as oracle_database
from tavily.databases import _UNSET, _resolve_constructor_arguments

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


class FakeMongoCollection:
    def __init__(self):
        self.insert_many_called = False
        self.aggregate_called = False

    def list_search_indexes(self):
        return [
            {
                "name": "vector_search",
                "type": "vectorSearch",
                "latestDefinition": {
                    "fields": [
                        {
                            "path": "embeddings",
                            "type": "vector",
                            "similarity": "cosine",
                        }
                    ]
                },
            }
        ]

    def aggregate(self, _):
        self.aggregate_called = True
        return []

    def insert_many(self, documents):
        self.insert_many_called = True
        if not documents:
            raise AssertionError("insert_many should not be called with an empty list.")

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

def embedding_function(texts, _):
    return [[0.1, 0.2, 0.3] for _ in texts]


def other_embedding_function(texts, _):
    return [[0.4, 0.5, 0.6] for _ in texts]


def ranking_function(_, documents, __):
    return documents

def other_ranking_function(_, documents, __):
    return list(reversed(documents))

def resolve(
    database=None,
    args=(),
    embedding=_UNSET,
    ranking=_UNSET,
    session=_UNSET,
    legacy_options=None,
):
    return _resolve_constructor_arguments(
        database=database,
        args=args,
        embedding_function=embedding,
        ranking_function=ranking,
        session=session,
        legacy_database_options=legacy_options or {},
    )

def test_resolve_legacy_positional_minimal():
    collection = FakeMongoCollection()

    database, embedding, ranking, session = resolve(
        database="mongodb",
        args=(
            collection,
            "vector_search",
        ),
    )

    assert isinstance(database, MongoDB)
    assert database.collection is collection
    assert database.index == "vector_search"

    assert embedding is None
    assert ranking is None
    assert session is None

def test_resolve_legacy_positional_full():
    collection = FakeMongoCollection()
    http_session = requests.Session()

    database, embedding, ranking, session = resolve(
        database="mongodb",
        args=(
            collection,
            "vector_search",
            "custom_embeddings",
            "custom_content",
            embedding_function,
            ranking_function,
            http_session,
        ),
    )

    assert isinstance(database, MongoDB)
    assert database.collection is collection
    assert database.index == "vector_search"
    assert database.embeddings_field == "custom_embeddings"
    assert database.content_field == "custom_content"

    assert embedding is embedding_function
    assert ranking is ranking_function
    assert session is http_session

def test_resolve_legacy_keyword_api():
    collection = FakeMongoCollection()

    database, embedding, ranking, session = resolve(
        legacy_options={
            "db_provider": "mongodb",
            "collection": collection,
            "index": "vector_search",
            "embeddings_field": "custom_embeddings",
            "content_field": "custom_content",
        },
    )

    assert isinstance(database, MongoDB)
    assert database.collection is collection
    assert database.index == "vector_search"
    assert database.embeddings_field == "custom_embeddings"
    assert database.content_field == "custom_content"

    assert embedding is None
    assert ranking is None
    assert session is None

def test_resolve_legacy_keyword_api_with_client_functions():
    collection = FakeMongoCollection()
    http_session = requests.Session()

    database, embedding, ranking, session = resolve(
        embedding=embedding_function,
        ranking=ranking_function,
        session=http_session,
        legacy_options={
            "db_provider": "mongodb",
            "collection": collection,
            "index": "vector_search",
        },
    )

    assert isinstance(database, MongoDB)
    assert embedding is embedding_function
    assert ranking is ranking_function
    assert session is http_session

def test_resolve_legacy_mixed_positional_and_keyword():
    collection = FakeMongoCollection()

    database, embedding, ranking, session = resolve(
        database="mongodb",
        args=(collection,),
        embedding=embedding_function,
        legacy_options={
            "index": "vector_search",
            "embeddings_field": "embeddings",
            "content_field": "content",
        },
    )

    assert isinstance(database, MongoDB)
    assert database.collection is collection
    assert database.index == "vector_search"
    assert embedding is embedding_function
    assert ranking is None
    assert session is None

@pytest.mark.parametrize(
    ("name", "args", "legacy_options"),
    [
        (
            "collection",
            (FakeMongoCollection(),),
            {"collection": FakeMongoCollection()},
        ),
        (
            "index",
            (FakeMongoCollection(), "vector_search"),
            {"index": "other_index"},
        ),
        (
            "embeddings_field",
            (
                FakeMongoCollection(),
                "vector_search",
                "embeddings",
            ),
            {"embeddings_field": "other_embeddings"},
        ),
        (
            "content_field",
            (
                FakeMongoCollection(),
                "vector_search",
                "embeddings",
                "content",
            ),
            {"content_field": "other_content"},
        ),
    ],
)

def test_resolve_legacy_rejects_duplicate_database_arguments(
    name,
    args,
    legacy_options,
):
    with pytest.raises(
        TypeError,
        match=rf"multiple values.*{name}",
    ):
        resolve(
            database="mongodb",
            args=args,
            legacy_options=legacy_options,
        )

def test_resolve_legacy_rejects_duplicate_db_provider():
    collection = FakeMongoCollection()

    with pytest.raises(
        TypeError,
        match=r"multiple values.*db_provider",
    ):
        resolve(
            database="mongodb",
            args=(
                collection,
                "vector_search",
            ),
            legacy_options={
                "db_provider": "mongodb",
            },
        )

@pytest.mark.parametrize(
    ("name", "args", "kwargs"),
    [
        (
            "embedding_function",
            (
                FakeMongoCollection(),
                "vector_search",
                "embeddings",
                "content",
                embedding_function,
            ),
            {
                "embedding": other_embedding_function,
            },
        ),
        (
            "ranking_function",
            (
                FakeMongoCollection(),
                "vector_search",
                "embeddings",
                "content",
                embedding_function,
                ranking_function,
            ),
            {
                "ranking": other_ranking_function,
            },
        ),
        (
            "session",
            (
                FakeMongoCollection(),
                "vector_search",
                "embeddings",
                "content",
                embedding_function,
                ranking_function,
                requests.Session(),
            ),
            {
                "session": requests.Session(),
            },
        ),
    ],
)
def test_resolve_legacy_rejects_duplicate_client_arguments(
    name,
    args,
    kwargs,
):
    with pytest.raises(
        TypeError,
        match=rf"multiple values.*{name}",
    ):
        resolve(
            database="mongodb",
            args=args,
            **kwargs,
        )

def test_resolve_legacy_explicit_none_is_still_duplicate():
    collection = FakeMongoCollection()

    with pytest.raises(
        TypeError,
        match=r"multiple values.*embedding_function",
    ):
        resolve(
            database="mongodb",
            args=(
                collection,
                "vector_search",
                "embeddings",
                "content",
                embedding_function,
            ),
            embedding=None,
        )

def test_resolve_legacy_rejects_too_many_positional_arguments():
    collection = FakeMongoCollection()

    with pytest.raises(TypeError, match="positional"):
        resolve(
            database="mongodb",
            args=(
                collection,
                "vector_search",
                "embeddings",
                "content",
                embedding_function,
                ranking_function,
                requests.Session(),
                "extra",
            ),
        )

def test_resolve_new_api_keyword_arguments():
    collection = FakeMongoCollection()
    database = MongoDB(
        collection=collection,
        index="vector_search",
    )
    http_session = requests.Session()

    resolved_database, embedding, ranking, session = resolve(
        database=database,
        embedding=embedding_function,
        ranking=ranking_function,
        session=http_session,
    )

    assert resolved_database is database
    assert embedding is embedding_function
    assert ranking is ranking_function
    assert session is http_session

def test_resolve_new_api_positional_arguments():
    collection = FakeMongoCollection()
    database = MongoDB(
        collection=collection,
        index="vector_search",
    )
    http_session = requests.Session()

    resolved_database, embedding, ranking, session = resolve(
        database=database,
        args=(
            embedding_function,
            ranking_function,
            http_session,
        ),
    )

    assert resolved_database is database
    assert embedding is embedding_function
    assert ranking is ranking_function
    assert session is http_session

def test_resolve_new_api_mixed_positional_and_keyword():
    database = MongoDB(
        collection=FakeMongoCollection(),
        index="vector_search",
    )
    http_session = requests.Session()

    resolved_database, embedding, ranking, session = resolve(
        database=database,
        args=(embedding_function,),
        ranking=ranking_function,
        session=http_session,
    )

    assert resolved_database is database
    assert embedding is embedding_function
    assert ranking is ranking_function
    assert session is http_session

@pytest.mark.parametrize(
    ("name", "args", "kwargs"),
    [
        (
            "embedding_function",
            (embedding_function,),
            {
                "embedding": other_embedding_function,
            },
        ),
        (
            "ranking_function",
            (
                embedding_function,
                ranking_function,
            ),
            {
                "ranking": other_ranking_function,
            },
        ),
        (
            "session",
            (
                embedding_function,
                ranking_function,
                requests.Session(),
            ),
            {
                "session": requests.Session(),
            },
        ),
    ],
)
def test_resolve_new_api_rejects_duplicate_arguments(
    name,
    args,
    kwargs,
):
    database = MongoDB(
        collection=FakeMongoCollection(),
        index="vector_search",
    )

    with pytest.raises(
        TypeError,
        match=rf"multiple values.*{name}",
    ):
        resolve(
            database=database,
            args=args,
            **kwargs,
        )

def test_resolve_new_api_explicit_none_is_still_duplicate():
    database = MongoDB(
        collection=FakeMongoCollection(),
        index="vector_search",
    )

    with pytest.raises(
        TypeError,
        match=r"multiple values.*embedding_function",
    ):
        resolve(
            database=database,
            args=(embedding_function,),
            embedding=None,
        )

def test_resolve_new_api_allows_explicit_none():
    database = MongoDB(
        collection=FakeMongoCollection(),
        index="vector_search",
    )

    resolved_database, embedding, ranking, session = resolve(
        database=database,
        embedding=None,
        ranking=None,
        session=None,
    )

    assert resolved_database is database
    assert embedding is None
    assert ranking is None
    assert session is None

def test_resolve_new_api_normalizes_unset_values_to_none():
    database = MongoDB(
        collection=FakeMongoCollection(),
        index="vector_search",
    )

    resolved_database, embedding, ranking, session = resolve(
        database=database,
    )

    assert resolved_database is database
    assert embedding is None
    assert ranking is None
    assert session is None

def test_resolve_new_api_rejects_too_many_positional_arguments():
    database = MongoDB(
        collection=FakeMongoCollection(),
        index="vector_search",
    )

    with pytest.raises(TypeError, match="positional"):
        resolve(
            database=database,
            args=(
                embedding_function,
                ranking_function,
                requests.Session(),
                "extra",
            ),
        )

def test_resolve_new_api_rejects_legacy_database_options():
    database = MongoDB(
        collection=FakeMongoCollection(),
        index="vector_search",
    )

    with pytest.raises(
        TypeError,
        match="database cannot be combined with legacy",
    ):
        resolve(
            database=database,
            legacy_options={
                "collection": FakeMongoCollection(),
            },
        )

def test_resolve_rejects_unknown_legacy_database_option():
    with pytest.raises(
        TypeError,
        match="Unexpected database option.*typo_collection",
    ):
        resolve(
            legacy_options={
                "db_provider": "mongodb",
                "typo_collection": FakeMongoCollection(),
            },
        )

def test_resolve_rejects_missing_database_configuration():
    with pytest.raises(
        ValueError,
        match="Database is required",
    ):
        resolve()

def test_resolve_rejects_positional_args_when_database_is_none():
    with pytest.raises(
        TypeError,
        match="positional",
    ):
        resolve(
            database=None,
            args=(embedding_function,),
        )

def test_resolve_rejects_legacy_oracle_provider_positional():
    with pytest.raises(
        ValueError,
        match="Only MongoDB can be created via legacy API",
    ):
        resolve(
            database="oracledb",
        )

def test_resolve_rejects_legacy_oracle_provider_keyword():
    with pytest.raises(
        ValueError,
        match="Only MongoDB can be created via legacy API",
    ):
        resolve(
            legacy_options={
                "db_provider": "oracledb",
            },
        )

def test_resolve_rejects_unknown_legacy_provider():
    with pytest.raises(
        ValueError,
        match="Database is required",
    ):
        resolve(
            database="test",
        )

def test_resolve_new_api_rejects_non_database_object():
    with pytest.raises(
        TypeError,
        match="database must be",
    ):
        resolve(
            database=FakeMongoCollection(),
        )

def test_legacy_positional_constructor_integration():
    collection = FakeMongoCollection()

    client = TavilyHybridClient(
        "tvly-test",
        "mongodb",
        collection,
        "vector_search",
        "embeddings",
        "content",
        embedding_function,
        ranking_function,
    )

    assert client.database.collection is collection

    assert client.search(
        "test query",
        max_local=1,
        max_foreign=0,
    ) == []

    assert collection.aggregate_called is True

def test_legacy_keyword_constructor_integration():
    collection = FakeMongoCollection()

    client = TavilyHybridClient(
        api_key="tvly-test",
        db_provider="mongodb",
        collection=collection,
        index="vector_search",
        embedding_function=embedding_function,
        ranking_function=ranking_function,
    )

    assert client.database.collection is collection
    assert client.database.index == "vector_search"

def test_new_constructor_keyword_integration():
    collection = FakeMongoCollection()
    database = MongoDB(
        collection=collection,
        index="vector_search",
    )

    client = TavilyHybridClient(
        "tvly-test",
        database=database,
        embedding_function=embedding_function,
        ranking_function=ranking_function,
    )

    assert client.database is database

def test_new_constructor_positional_integration():
    collection = FakeMongoCollection()
    database = MongoDB(
        collection=collection,
        index="vector_search",
    )

    client = TavilyHybridClient(
        "tvly-test",
        database,
        embedding_function,
        ranking_function,
    )

    assert client.database is database