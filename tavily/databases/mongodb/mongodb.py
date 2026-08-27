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
    ):
        self.collection = collection
        self.index = index
        self.embeddings_field = embeddings_field
        self.content_field = content_field

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
        """The application owns the MongoDB client supplied through collection."""
        return None


def validate_index(database):
    """
    Check that the index specified by the parameters exists and is a valid vector search index.

    Raises:
        ValueError: If the index does not exist, is not of type 'vectorSearch', or if the embeddings field
                    does not exist, is not of type 'vector', or has similarity other than 'cosine'.
    """
    index_exists = False
    for index in database.collection.list_search_indexes():
        if index['name'] != database.index:
            continue
        
        if index['type'] != 'vectorSearch':
            raise ValueError(f"Index '{database.index}' exists but is not of type "
                                "'vectorSearch'.")
        
        field_exists = False
        for field in index['latestDefinition']['fields']:
            if field['path'] != database.embeddings_field:
                continue
            
            if field['type'] != 'vector':
                raise ValueError(f"Field '{database.embeddings_field}' exists "
                                    "but is not of type 'vector'.")
            elif field['similarity'] != 'cosine':
                raise ValueError(f"Field '{database.embeddings_field}' exists but has "
                                    f"similarity '{field['similarity']}' instead of 'cosine'.")
            
            field_exists = True
            break
        
        if not field_exists:
            raise ValueError(f"Field '{database.embeddings_field}' does not exist in "
                                "index '{database.index}'.")
        
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
