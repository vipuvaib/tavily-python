# MongoDB Provider for Tavily Hybrid RAG

This provider keeps the original MongoDB-backed Hybrid RAG behavior for `TavilyHybridClient`.

MongoDB acts as the local vector store. Tavily supplies fresh external results, and `save_foreign=True` can write Tavily results back into the MongoDB collection for future local retrieval.

## Retrieval Flow

The `MongoDB` adapter always uses hybrid-search behavior.

```text
User query
  -> embed query
  -> search MongoDB vector index
  -> optionally search Tavily
  -> merge local + Tavily results
  -> rerank
  -> optionally save Tavily results into MongoDB
```

Oracle-only modes such as `freshness_cache` and `cache_then_memory` are intentionally rejected for MongoDB.

## Connection Pattern

Your application can either pass an existing MongoDB collection:

```python
import os

from pymongo import MongoClient
from tavily import TavilyHybridClient
from tavily.databases.mongodb import MongoDB

mongo = MongoClient(os.environ["MONGO_URI"])
collection = mongo[os.environ["MONGO_DATABASE"]][os.environ["MONGO_COLLECTION"]]

client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=MongoDB(
        collection=collection,
        index=os.environ["MONGO_INDEX"],
        embeddings_field="embeddings",
        content_field="content",
    ),
)
```

Or use the convenience connection parameters:

```python
import os

from tavily import TavilyHybridClient
from tavily.databases.mongodb import MongoDB

client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=MongoDB(
        uri=os.environ["MONGO_URI"],
        database_name=os.environ["MONGO_DATABASE"],
        collection_name=os.environ["MONGO_COLLECTION"],
        index=os.environ["MONGO_INDEX"],
    ),
)
```

When `MongoDB` creates the MongoDB client through `uri`, `client.close()` closes that managed client.

## Expected Collection Shape

The provider expects each searchable document to contain:

| Field | Purpose |
| --- | --- |
| `content` | Text returned in local search results. Configurable with `content_field`. |
| `embeddings` | Vector embedding used by MongoDB Atlas Vector Search. Configurable with `embeddings_field`. |

You can store extra metadata in the same documents, but the default local result projection only returns:

- `content`
- `score`
- `origin="local"`

## Vector Index Requirements

The configured MongoDB search index must:

- exist on the collection,
- have `type="vectorSearch"`,
- include the configured embeddings field,
- use a vector field type,
- use cosine similarity.

`validate_index()` checks this during `TavilyHybridClient` initialization.

## Persistence Behavior

When `save_foreign=True`, Tavily results are embedded and inserted with:

```python
{
    content_field: result["content"],
    embeddings_field: result["embeddings"],
}
```

When `save_foreign` is a function, the function receives each Tavily result and can return a custom MongoDB document. Returning `None` skips that result.

The MongoDB provider uses append-style persistence today. It does not currently implement TTL cleanup, URL/content-hash upsert, semantic deduplication, or cache/memory lifecycle metadata. Those controls are Oracle-specific in the current implementation.

## Code Layout

MongoDB-specific implementation files:

- `tavily/databases/mongodb/mongodb.py`
- `tavily/databases/mongodb/__init__.py`

Shared Hybrid RAG orchestration lives in:

- `tavily/hybrid_rag/hybrid_rag.py`
- `tavily/hybrid_rag/embeddings.py`

MongoDB remains separate from the Oracle provider, so Oracle-specific cache and memory behavior does not change the existing MongoDB search path.
