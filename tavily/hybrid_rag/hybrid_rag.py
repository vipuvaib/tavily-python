import os
from typing import Callable, List, Optional, Union

import requests
from tavily import TavilyClient
from tavily.databases import _resolve_constructor_arguments, _UNSET
from tavily.databases.base import (
    HybridRAGBackend,
    LocalSearchResult,
    LocalSearchRequest,
    PersistenceContext,
    RetrievalAction,
    TavilyResult,
)

try:
    import cohere
    co = cohere.Client()
except:
    co = None

def _cohere_embed(texts, type):
    return co.embed(
        model='embed-english-v3.0',
        texts=texts,
        input_type=type
    ).embeddings

def _cohere_rerank(query, documents, top_n):
    response = co.rerank(model='rerank-english-v3.0', query=query,
                         documents=[doc['content'] for doc in documents], top_n=top_n)
    
    return [
        documents[result.index] | {'score': result.relevance_score}
        for result in response.results
    ]

class TavilyHybridClient():

    def __init__(
            self,
            api_key: Union[str, None],
            database: Optional[Union[str, HybridRAGBackend]] = None, # str for Legacy API
            *args,
            embedding_function: Optional[callable] = _UNSET, # Sentinel objects to distinguish when user explicitly passes None
            ranking_function: Optional[callable] = _UNSET,
            session: Optional[requests.Session] = _UNSET,
            **legacy_database_options
        ):
        
        '''
        # Legacy positional form:
        #
        # TavilyHybridClient(
        #     api_key,
        #     "mongodb",
        #     collection,
        #     index,
        #     embeddings_field,
        #     content_field,
        #     embedding_function,
        #     ranking_function,
        #     session,
        # )

        A client for performing hybrid RAG using both the Tavily API and a local database collection.
        
        Parameters:
        api_key (str): The Tavily API key. If this is set to None, it will be loaded from the environment variable TAVILY_API_KEY.
        db_provider (str): The database provider. Currently only 'mongodb' is supported.
        collection (str): The name of the collection in the database that will be used for local search.
        index (str): The name of the collection's vector search index.
        embeddings_field (str): The name of the field in the collection that contains the embeddings.
        content_field (str): The name of the field in the collection that contains the content.
        embedding_function (callable): If provided, this function will be used to generate embeddings for the search query and documents.
        ranking_function (callable): If provided, this function will be used to rerank the combined results.
        session (requests.Session): If provided, this pre-configured session will be used for HTTP requests. When set, api_key is optional.
        '''

        (
            database,
            embedding_function,
            ranking_function,
            session,
        ) = _resolve_constructor_arguments(
            database=database,
            args=args,
            embedding_function=embedding_function,
            ranking_function=ranking_function,
            session=session,
            legacy_database_options=legacy_database_options,
        )
        
        self.database = database
        self.embedding_function = (
            _cohere_embed if embedding_function is None else embedding_function
        )
        self.ranking_function = (
            _cohere_rerank if ranking_function is None else ranking_function
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

    def search(self, query: str, max_results: int = 10,
               max_local: Optional[int] = None,
               max_foreign: Optional[int] = None,
               save_foreign=False, **kwargs) -> List[LocalSearchResult]:
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

    def _search_tavily(
            self, query: str, max_foreign: int, **kwargs
    ) -> List[TavilyResult]:
        if max_foreign > 0:
            return self.tavily.search(query, max_results=max_foreign, **kwargs)["results"]
        return []

    @staticmethod
    def _project_foreign_results(
            foreign_results: List[TavilyResult],
    ) -> List[LocalSearchResult]:
        return [
            {
                "content": result["content"],
                "score": result["score"],
                "origin": "foreign",
            }
            for result in foreign_results
        ]

    def _save_foreign_results(
            self,
            foreign_results: List[TavilyResult],
            save_foreign,
            max_foreign: int,
            query: Optional[str] = None,
            cache_hit: bool = False,
    ) -> None:
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