import sys
import types
from unittest.mock import sentinel

import pytest

from tavily import TavilyHybridClient
from tavily.databases.mongodb import MongoDB
from tavily.databases.oracledb import OracleDB


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


class FakeTavilyClient:
    def search(self, *_args, **_kwargs):
        return {
            "results": [
                {
                    "content": "foreign content",
                    "score": 0.8,
                }
            ]
        }


def test_save_foreign_skips_empty_insert_after_custom_filter_removes_everything():
    collection = FakeMongoCollection()
    client = TavilyHybridClient(
        api_key="tvly-test",
        database=MongoDB(collection=collection, index="vector_search"),
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )
    client.tavily = FakeTavilyClient()

    results = client.search(
        "test query",
        save_foreign=lambda _: None,
    )

    assert results == [{"content": "foreign content", "score": 0.8, "origin": "foreign"}]
    assert collection.insert_many_called is False


def test_embedding_and_ranking_functions_must_be_callable():
    with pytest.raises(TypeError, match="embedding_function must be callable"):
        TavilyHybridClient(
            api_key="tvly-test",
            db_provider="mongodb",
            collection=FakeMongoCollection(),
            index="vector_search",
            embedding_function="not-callable",
            ranking_function=lambda _, documents, __: documents,
        )


def test_mongodb_rejects_freshness_cache_mode():
    with pytest.raises(ValueError, match="freshness_cache.*db_provider='oracle'"):
        TavilyHybridClient(
            api_key="tvly-test",
            db_provider="mongodb",
            collection=FakeMongoCollection(),
            index="vector_search",
            retrieval_mode="freshness_cache",
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
            ranking_function=lambda _, documents, __: documents,
        )


def test_mongodb_rejects_cache_then_memory_mode():
    with pytest.raises(ValueError, match="cache_then_memory.*db_provider='oracle'"):
        TavilyHybridClient(
            api_key="tvly-test",
            db_provider="mongodb",
            collection=FakeMongoCollection(),
            index="vector_search",
            retrieval_mode="cache_then_memory",
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
            ranking_function=lambda _, documents, __: documents,
        )


def test_mongodb_ignores_oracle_only_feature_flags_in_hybrid_mode():
    collection = FakeMongoCollection()
    client = TavilyHybridClient(
        api_key="tvly-test",
        db_provider="mongodb",
        collection=collection,
        index="vector_search",
        enable_native_hybrid_search=True,
        enable_oracle_json_payload=True,
        enable_provenance_metadata=True,
        enable_oracle_memory_metadata=True,
        persistence_depth="not-used-by-mongodb",
        memory_score_threshold="not-used-by-mongodb",
        memory_max_results=0,
        cache_cleanup_interval_seconds=0,
        dedup_similarity_threshold=0.95,
        max_persisted_foreign=0,
        persist_score_threshold="not-used-by-mongodb",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )

    results = client.search("test query", max_local=1, max_foreign=0)

    assert results == []
    assert collection.aggregate_called is True
    assert collection.insert_many_called is False


def test_oracle_rejects_invalid_upsert_key():
    with pytest.raises(ValueError, match="oracle_upsert_key"):
        TavilyHybridClient(
            api_key="tvly-test",
            db_provider="oracle",
            connection=object(),
            table_name="tavily_documents",
            oracle_upsert_key="url",
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
            ranking_function=lambda _, documents, __: documents,
        )


def test_oracle_rejects_invalid_persistence_controls():
    with pytest.raises(ValueError, match="oracle_max_persisted_foreign"):
        OracleDB(
            connection=object(),
            table_name="tavily_documents",
            oracle_max_persisted_foreign=0,
        )

    with pytest.raises(ValueError, match="oracle_persist_score_threshold"):
        OracleDB(
            connection=object(),
            table_name="tavily_documents",
            oracle_persist_score_threshold="not-a-number",
        )

    with pytest.raises(ValueError, match="oracle_persist_score_threshold must be finite"):
        OracleDB(
            connection=object(),
            table_name="tavily_documents",
            oracle_persist_score_threshold="nan",
        )

    with pytest.raises(ValueError, match="cache_score_threshold must be finite"):
        TavilyHybridClient(
            api_key="tvly-test",
            db_provider="oracle",
            connection=object(),
            table_name="tavily_documents",
            retrieval_mode="freshness_cache",
            cache_score_threshold="nan",
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
            ranking_function=lambda _, documents, __: documents,
        )

    with pytest.raises(ValueError, match="dedup_similarity_threshold must be finite"):
        TavilyHybridClient(
            api_key="tvly-test",
            db_provider="oracle",
            connection=object(),
            table_name="tavily_documents",
            dedup_similarity_threshold="inf",
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
            ranking_function=lambda _, documents, __: documents,
        )


def test_mongodb_convenience_connection_params_create_collection(monkeypatch):
    created = {}

    class FakeMongoDatabase:
        def __getitem__(self, name):
            assert name == "documents"
            return FakeMongoCollection()

    class FakeMongoClient:
        def __init__(self, uri, **kwargs):
            self.uri = uri
            self.kwargs = kwargs
            self.closed = False
            created["client"] = self

        def __getitem__(self, name):
            assert name == "memory"
            return FakeMongoDatabase()

        def close(self):
            self.closed = True

    monkeypatch.setitem(
        sys.modules,
        "pymongo",
        types.SimpleNamespace(MongoClient=FakeMongoClient)
    )

    client = TavilyHybridClient(
        api_key="tvly-test",
        db_provider="mongodb",
        mongo_uri="mongodb://localhost:27017",
        mongo_database="memory",
        mongo_collection="documents",
        mongo_client_kwargs={"appname": "tavily-test"},
        index="vector_search",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )

    assert created["client"].uri == "mongodb://localhost:27017"
    assert created["client"].kwargs == {"appname": "tavily-test"}
    client.close()
    assert created["client"].closed is True


def test_oracle_convenience_connection_params_create_connection(monkeypatch):
    created = {}

    class FakeOracleConnection:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def fake_connect(**kwargs):
        created["kwargs"] = kwargs
        created["connection"] = FakeOracleConnection()
        return created["connection"]

    monkeypatch.setitem(
        sys.modules,
        "oracledb",
        types.SimpleNamespace(connect=fake_connect)
    )

    client = TavilyHybridClient(
        api_key="tvly-test",
        db_provider="oracle",
        oracle_user="intern_user",
        oracle_password=sentinel.oracle_credential,
        oracle_dsn="localhost:1521/FREEPDB1",
        oracle_connection_kwargs={"config_dir": "/wallet"},
        table_name="tavily_documents",
        embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
        ranking_function=lambda _, documents, __: documents,
    )

    assert client.database.connection is created["connection"]
    assert created["kwargs"] == {
        "user": "intern_user",
        "password": sentinel.oracle_credential,
        "dsn": "localhost:1521/FREEPDB1",
        "config_dir": "/wallet",
    }
    client.close()
    assert created["connection"].closed is True


def test_oracle_convenience_connection_not_opened_when_validation_fails(monkeypatch):
    created = {"connect_called": False}

    def fake_connect(**_kwargs):
        created["connect_called"] = True
        raise AssertionError("Oracle connection should not be opened.")

    monkeypatch.setitem(
        sys.modules,
        "oracledb",
        types.SimpleNamespace(connect=fake_connect)
    )

    with pytest.raises(ValueError, match="oracle_upsert_key"):
        TavilyHybridClient(
            api_key="tvly-test",
            db_provider="oracle",
            oracle_user="intern_user",
            oracle_password=sentinel.oracle_credential,
            oracle_dsn="localhost:1521/FREEPDB1",
            table_name="tavily_documents",
            oracle_upsert_key="url",
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
            ranking_function=lambda _, documents, __: documents,
        )

    assert created["connect_called"] is False


def test_mongodb_managed_client_closes_when_provider_validation_fails(monkeypatch):
    created = {}

    class InvalidIndexCollection(FakeMongoCollection):
        def list_search_indexes(self):
            return []

    class FakeMongoDatabase:
        def __getitem__(self, name):
            assert name == "documents"
            return InvalidIndexCollection()

    class FakeMongoClient:
        def __init__(self, uri, **kwargs):
            self.uri = uri
            self.kwargs = kwargs
            self.closed = False
            created["client"] = self

        def __getitem__(self, name):
            assert name == "memory"
            return FakeMongoDatabase()

        def close(self):
            self.closed = True

    monkeypatch.setitem(
        sys.modules,
        "pymongo",
        types.SimpleNamespace(MongoClient=FakeMongoClient)
    )

    with pytest.raises(ValueError, match="Index 'vector_search' does not exist"):
        TavilyHybridClient(
            api_key="tvly-test",
            db_provider="mongodb",
            mongo_uri="mongodb://localhost:27017",
            mongo_database="memory",
            mongo_collection="documents",
            index="vector_search",
            embedding_function=lambda texts, _: [[0.1, 0.2, 0.3] for _ in texts],
            ranking_function=lambda _, documents, __: documents,
        )

    assert created["client"].closed is True
