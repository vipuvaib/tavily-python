from typing import Any, Dict, List, Optional, Sequence

from tavily.databases.base import (
    LocalSearchRequest,
    PersistenceContext,
    RetrievalDecision,
)


class MongoDB:
    """MongoDB Atlas Vector Search adapter for ``TavilyHybridClient``."""

    def __init__(
            self,
            collection=None,
            index: Optional[str] = None,
            embeddings_field: str = "embeddings",
            content_field: str = "content",
            uri: Optional[str] = None,
            database_name: Optional[str] = None,
            collection_name: Optional[str] = None,
            client_kwargs: Optional[dict] = None,
    ):
        self.collection = collection
        self.index = index
        self.embeddings_field = embeddings_field
        self.content_field = content_field
        self._managed_client = None

        should_connect = (
            collection is None
            and any(value is not None for value in (uri, database_name, collection_name))
        )
        if collection is None and not should_connect:
            raise ValueError("collection is required when a MongoDB URI is not provided.")
        if index is None:
            raise ValueError("index is required for MongoDB.")

        if should_connect:
            self._managed_client, self.collection = connect(
                uri,
                database_name,
                collection_name,
                client_kwargs=client_kwargs,
            )

    def validate(self) -> None:
        validate_index(self)

    def retrieve(self, request: LocalSearchRequest) -> RetrievalDecision:
        return RetrievalDecision.merge_foreign(search(
            self.collection,
            self.index,
            self.embeddings_field,
            self.content_field,
            request.query_embeddings,
            request.max_local,
        ))

    def build_persistence_document(
            self,
            foreign_result: Dict[str, Any],
            embedding: Sequence[float],
            _context: PersistenceContext,
    ) -> Dict[str, Any]:
        return {
            self.content_field: foreign_result["content"],
            self.embeddings_field: embedding,
        }

    def select_foreign_results_for_persistence(
            self,
            foreign_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """MongoDB persists every Tavily result selected by the caller."""
        return foreign_results

    def persist(self, documents: List[Dict[str, Any]]) -> None:
        insert_documents(self.collection, documents)

    def close(self) -> None:
        if self._managed_client is not None:
            self._managed_client.close()
            self._managed_client = None


def connect(uri, database_name, collection_name, client_kwargs=None):
    if not uri or not database_name or not collection_name:
        raise ValueError(
            "uri, database_name, and collection_name are required when "
            "collection is not provided."
        )

    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise ImportError(
            "MongoDB convenience connection parameters require the 'pymongo' "
            "package. Install it with the mongodb extra or pass an existing "
            "collection object."
        ) from exc

    client = MongoClient(uri, **(client_kwargs or {}))
    return client, client[database_name][collection_name]


def validate_index(database):
    """Check that the configured MongoDB Atlas index is a cosine vector index."""
    if not hasattr(database.collection, "list_search_indexes"):
        raise ValueError("MongoDB collection must provide list_search_indexes().")

    index_exists = False
    for index in database.collection.list_search_indexes():
        if index.get("name") != database.index:
            continue

        if index.get("type") != "vectorSearch":
            raise ValueError(f"Index '{database.index}' exists but is not of type "
                             "'vectorSearch'.")

        field_exists = False
        fields = index.get("latestDefinition", {}).get("fields", [])
        for field in fields:
            if field.get("path") != database.embeddings_field:
                continue

            if field.get("type") != "vector":
                raise ValueError(f"Field '{database.embeddings_field}' exists "
                                 "but is not of type 'vector'.")
            if field.get("similarity") != "cosine":
                raise ValueError(f"Field '{database.embeddings_field}' exists but has "
                                 f"similarity '{field.get('similarity')}' instead of 'cosine'.")

            field_exists = True
            break

        if not field_exists:
            raise ValueError(f"Field '{database.embeddings_field}' does not exist in "
                             f"index '{database.index}'.")

        index_exists = True

    if not index_exists:
        raise ValueError(f"Index '{database.index}' does not exist.")


def search(collection, index, embeddings_field, content_field, query_embeddings, max_local):
    return list(collection.aggregate([
        {
            "$vectorSearch": {
                "index": index,
                "path": embeddings_field,
                "queryVector": query_embeddings,
                "numCandidates": max_local + 3,
                "limit": max_local
            }
        },
        {
            "$project": {
                "_id": 0,
                "content": f"${content_field}",
                "score": {
                    "$meta": "vectorSearchScore"
                },
                "origin": "local"
            }
        }
    ]))


def insert_documents(collection, documents):
    collection.insert_many(documents)
