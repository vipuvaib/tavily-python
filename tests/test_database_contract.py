# Copyright (c) 2026, Oracle and/or its affiliates.

"""Provider-neutral contract tests for the Hybrid RAG backend interface."""

import pytest

import tavily.hybrid_rag.hybrid_rag as hybrid_rag
from tavily import TavilyHybridClient
from tavily.databases.base import (
    DatabaseAdapter,
    LocalSearchRequest,
    LocalSearchResult,
    PersistenceContext,
    RetrievalDecision,
    TavilyResult,
)


class FakeTavilyClient:
    def __init__(self, results=None):
        self.results = results or []
        self.search_calls = []
        self.close_calls = 0

    def search(self, query, max_results, **kwargs):
        self.search_calls.append((query, max_results, kwargs))
        return {"results": self.results}

    def close(self):
        self.close_calls += 1


class FakeBackend(DatabaseAdapter):
    def __init__(self, decision, selected_results=None, validate_error=None):
        self.decision = decision
        self.selected_results = selected_results
        self.validate_error = validate_error
        self.validate_calls = 0
        self.retrieve_requests = []
        self.selection_calls = []
        self.built_documents = []
        self.persisted_documents = []
        self.close_calls = 0

    def validate(self):
        self.validate_calls += 1
        if self.validate_error is not None:
            raise self.validate_error

    def retrieve(self, request: LocalSearchRequest) -> RetrievalDecision:
        self.retrieve_requests.append(request)
        return self.decision

    def select_foreign_results_for_persistence(self, foreign_results):
        self.selection_calls.append(foreign_results)
        if self.selected_results is None:
            return foreign_results
        return self.selected_results

    def build_persistence_document(
            self,
            foreign_result: TavilyResult,
            embedding,
            context: PersistenceContext,
    ):
        document = {
            "payload": foreign_result["content"],
            "embedding": list(embedding),
            "query": context.query,
        }
        self.built_documents.append(document)
        return document

    def persist(self, documents):
        self.persisted_documents.extend(documents)

    def close(self):
        self.close_calls += 1


def _client(monkeypatch, backend, tavily_results=None, ranked_results=None):
    tavily = FakeTavilyClient(tavily_results)
    monkeypatch.setattr(hybrid_rag, "TavilyClient", lambda *_args, **_kwargs: tavily)
    ranking_calls = []

    def embed(texts, _input_type):
        return [[float(index)] for index, _text in enumerate(texts, start=1)]

    def rank(query, documents, max_results):
        ranking_calls.append((query, documents, max_results))
        return documents if ranked_results is None else ranked_results

    client = TavilyHybridClient(
        api_key="tvly-test",
        database=backend,
        embedding_function=embed,
        ranking_function=rank,
    )
    return client, tavily, ranking_calls


def _local(content="local", score=0.7) -> LocalSearchResult:
    return {"content": content, "score": score, "origin": "local"}


def _foreign(content="foreign", score=0.9) -> TavilyResult:
    return {"content": content, "score": score}


def test_contract_validates_and_closes_backend(monkeypatch):
    backend = FakeBackend(RetrievalDecision.merge_foreign([]))
    client, tavily, _ = _client(monkeypatch, backend)

    assert backend.validate_calls == 1
    client.close()
    assert backend.close_calls == 1
    assert tavily.close_calls == 1


def test_contract_returns_local_results_without_tavily_or_reranking(monkeypatch):
    backend = FakeBackend(RetrievalDecision.return_local([_local()]))
    client, tavily, ranking_calls = _client(monkeypatch, backend, [_foreign()])

    assert client.search("query") == [_local()]
    assert tavily.search_calls == []
    assert ranking_calls == []
    assert backend.retrieve_requests[0].query == "query"


def test_contract_merges_local_and_foreign_results_then_reranks(monkeypatch):
    backend = FakeBackend(RetrievalDecision.merge_foreign([_local()]))
    client, tavily, ranking_calls = _client(monkeypatch, backend, [_foreign()])

    assert client.search("query") == [
        _local(),
        {"content": "foreign", "score": 0.9, "origin": "foreign"},
    ]
    assert tavily.search_calls == [("query", 10, {})]
    assert ranking_calls[0][1] == [
        _local(),
        {"content": "foreign", "score": 0.9, "origin": "foreign"},
    ]


def test_contract_falls_back_to_tavily_without_reranking(monkeypatch):
    backend = FakeBackend(RetrievalDecision.fallback_to_foreign())
    client, tavily, ranking_calls = _client(monkeypatch, backend, [_foreign()])

    assert client.search("query") == [
        {"content": "foreign", "score": 0.9, "origin": "foreign"},
    ]
    assert tavily.search_calls == [("query", 10, {})]
    assert ranking_calls == []


def test_contract_selects_builds_and_persists_foreign_results(monkeypatch):
    selected = [_foreign("persisted", 0.95)]
    backend = FakeBackend(
        RetrievalDecision.merge_foreign([]),
        selected_results=selected,
    )
    client, _tavily, _ranking_calls = _client(
        monkeypatch,
        backend,
        [_foreign("discarded", 0.5), selected[0]],
    )

    client.search("query", save_foreign=True)

    assert backend.selection_calls == [[
        _foreign("discarded", 0.5),
        _foreign("persisted", 0.95),
    ]]
    assert backend.built_documents == [{
        "payload": "persisted",
        "embedding": [1.0],
        "query": "query",
    }]
    assert backend.persisted_documents == backend.built_documents


def test_contract_closes_backend_when_validation_fails(monkeypatch):
    backend = FakeBackend(
        RetrievalDecision.merge_foreign([]),
        validate_error=RuntimeError("invalid backend"),
    )
    monkeypatch.setattr(
        hybrid_rag,
        "TavilyClient",
        lambda *_args, **_kwargs: pytest.fail("Tavily must not be created"),
    )

    with pytest.raises(RuntimeError, match="invalid backend"):
        TavilyHybridClient(
            api_key="tvly-test",
            database=backend,
            embedding_function=lambda texts, _: [[0.1] for _text in texts],
            ranking_function=lambda _query, documents, _max_results: documents,
        )

    assert backend.close_calls == 1
