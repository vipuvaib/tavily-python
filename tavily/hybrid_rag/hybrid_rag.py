from typing import Callable, Optional, Sequence, Union

import requests

from tavily import TavilyClient
from tavily.databases import create_legacy_database
from tavily.databases.base import (
    Database,
    LocalSearchRequest,
    PersistenceContext,
    RetrievalAction,
)
from tavily.hybrid_rag.embeddings import cohere_embed, cohere_rerank

class TavilyHybridClient:
    """Orchestrate Tavily Search with a configured local database adapter.

    ``database`` is normally a :class:`MongoDB` or :class:`OracleDB` instance.
    The deprecated keyword-only provider configuration remains available through
    a database-package compatibility bridge, but the client itself no longer
    owns provider connection, schema, or retrieval-mode configuration.
    """

    def __init__(
            self,
            api_key: Union[str, None],
            database: Optional[Database] = None,
            embedding_function: Optional[
                Callable[[Sequence[str], str], Sequence[Sequence[float]]]
            ] = None,
            ranking_function: Optional[Callable[[str, list, int], list]] = None,
            session: Optional[requests.Session] = None,
            **legacy_database_options
    ):
        if database is None:
            database = create_legacy_database(legacy_database_options)
        elif legacy_database_options:
            unexpected = ", ".join(sorted(legacy_database_options))
            raise TypeError(
                "database cannot be combined with legacy database options: "
                f"{unexpected}."
            )

        self.database = database
        self.embedding_function = (
            cohere_embed if embedding_function is None else embedding_function
        )
        self.ranking_function = (
            cohere_rerank if ranking_function is None else ranking_function
        )
        self.tavily = None

        if not callable(self.embedding_function):
            raise TypeError("embedding_function must be callable.")
        if not callable(self.ranking_function):
            raise TypeError("ranking_function must be callable.")

        try:
            self.database.validate()
            self.tavily = TavilyClient(api_key, session=session)
        except Exception:
            self.close()
            raise

    def search(self, query, max_results=10, max_local=None, max_foreign=None,
               save_foreign=False, **kwargs):
        """Return local and Tavily results according to the database decision."""
        if max_local is None:
            max_local = max_results
        if max_foreign is None:
            max_foreign = max_results

        query_embeddings = self.embedding_function([query], "search_query")[0]
        decision = self.database.retrieve(LocalSearchRequest(
            query=query,
            query_embeddings=query_embeddings,
            max_results=max_results,
            max_local=max_local,
        ))

        if decision.action == RetrievalAction.RETURN_LOCAL:
            return decision.local_results[:max_results]

        foreign_results = self._search_tavily(query, max_foreign, **kwargs)
        projected_foreign_results = self._project_foreign_results(foreign_results)

        if decision.action == RetrievalAction.FALLBACK_TO_FOREIGN:
            results = projected_foreign_results[:max_results]
        elif decision.action == RetrievalAction.MERGE_FOREIGN:
            combined_results = decision.local_results + projected_foreign_results
            if not combined_results:
                return []
            results = self.ranking_function(query, combined_results, max_results)
            results = results[:max_results]
        else:
            raise ValueError(f"Unsupported retrieval action: {decision.action}")

        self._save_foreign_results(foreign_results, save_foreign, max_foreign, query)
        return results

    def _search_tavily(self, query, max_foreign, **kwargs):
        if max_foreign > 0:
            return self.tavily.search(query, max_results=max_foreign, **kwargs)["results"]
        return []

    @staticmethod
    def _project_foreign_results(foreign_results):
        return [
            {
                "content": result["content"],
                "score": result["score"],
                "origin": "foreign",
            }
            for result in foreign_results
        ]

    def _save_foreign_results(self, foreign_results, save_foreign, max_foreign,
                              query=None, cache_hit=False):
        # ``save_foreign`` may be a transform callable, so do not use truthiness.
        if not (max_foreign > 0 and save_foreign != False):
            return

        foreign_results = self.database.select_foreign_results_for_persistence(
            foreign_results
        )
        if not foreign_results:
            return

        documents = []
        embeddings = self.embedding_function(
            [result["content"] for result in foreign_results],
            "search_document",
        )
        context = PersistenceContext(query=query, cache_hit=cache_hit)
        for result, embedding in zip(foreign_results, embeddings):
            if save_foreign is True:
                documents.append(self.database.build_persistence_document(
                    result,
                    embedding,
                    context,
                ))
            else:
                result_with_embedding = dict(result)
                result_with_embedding["embeddings"] = embedding
                document = save_foreign(result_with_embedding)
                if document:
                    documents.append(document)

        if documents:
            self.database.persist(documents)

    def close(self):
        if hasattr(self.tavily, "close"):
            self.tavily.close()
        if self.database is not None:
            self.database.close()
