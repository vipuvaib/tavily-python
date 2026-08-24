# Oracle Database Hybrid RAG

Use Tavily Search with Oracle Database to build a retrieval layer that stays fresh, persists useful web results, and reuses stored knowledge through Oracle vector search.

This integration is useful when your application needs both live web context and governed database memory. Tavily supplies fresh external results. Oracle stores reusable content, embeddings, JSON payloads, provenance metadata, and cache or memory lifecycle fields.

## What You'll Learn

- Configure `OracleDB` for `TavilyHybridClient`.
- Create the minimum Oracle table for vector retrieval.
- Choose between `hybrid_search`, `freshness_cache`, and `cache_then_memory`.
- Persist Tavily results into Oracle with provenance.
- Tune cache, memory, deduplication, and vector-index options for production.

## How Does It Work?

`TavilyHybridClient` has one application entry point: `client.search(...)`.

Depending on the `OracleDB` `retrieval_mode`, the adapter checks Oracle first, calls Tavily only when the selected strategy needs fresh external context, then optionally writes Tavily results back into Oracle.

```text
User query
  -> Embed query
  -> Search Oracle rows
  -> Call Tavily when the selected mode needs fresh web results
  -> Merge or return results
  -> Optionally persist Tavily results into Oracle
```

The returned results use a simple origin marker:

- `origin="local"` means the result came from Oracle.
- `origin="foreign"` means the result came from Tavily.

## Getting Started

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install "tavily-python[oracle]"
```

The default embedding and reranking helpers use Cohere. If you already have an embedding and ranking stack, pass custom `embedding_function` and `ranking_function` callables to `TavilyHybridClient`.

### 2. Set environment variables

Create an untracked `.env` file from `.env.example`, populate the required
values locally, and load it into the current shell when running standalone
examples:

```bash
set -a
source .env
set +a
```

`ORACLE_DSN` can point at Oracle Database 23ai, Oracle Free, or a compatible Oracle Database service with vector support.

### 3. Create an Oracle table

The minimum table needs a text column, a vector column, and a timestamp column. Match the vector dimension to the embedding model you use. Cohere `embed-english-v3.0` returns 1024-dimensional vectors.

```sql
CREATE TABLE tavily_documents (
    id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content CLOB,
    embeddings VECTOR(1024, FLOAT32),
    added_at TIMESTAMP DEFAULT SYSTIMESTAMP
);
```

Add the optional metadata columns when you want JSON payloads, provenance, cache lifecycle fields, memory lifecycle fields, upserts, or semantic deduplication.

```sql
ALTER TABLE tavily_documents ADD (
    raw_payload JSON,
    source_url VARCHAR2(1000),
    source_title VARCHAR2(500),
    retrieval_query VARCHAR2(1000),
    retrieval_timestamp TIMESTAMP WITH TIME ZONE,
    retrieval_mode VARCHAR2(30),
    cache_hit NUMBER(1),
    inserted_from VARCHAR2(30),
    provider_name VARCHAR2(50),
    memory_scope VARCHAR2(30),
    expires_at TIMESTAMP WITH TIME ZONE,
    last_seen_at TIMESTAMP WITH TIME ZONE,
    query_count NUMBER DEFAULT 0,
    content_hash VARCHAR2(64)
);
```

### 4. Connect Tavily to Oracle

```python
import os
import oracledb
from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB

connection = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    dsn=os.environ["ORACLE_DSN"],
)

client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=OracleDB(
        connection=connection,
        table_name="TAVILY_DOCUMENTS",
        retrieval_mode="hybrid_search",
        enable_json_payload=True,
        enable_provenance_metadata=True,
    ),
)
```

## Mode 1: Hybrid Search

Use `hybrid_search` when you want Oracle memory and fresh Tavily results in the same response.

```python
client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=OracleDB(
        connection=connection,
        table_name="TAVILY_DOCUMENTS",
        retrieval_mode="hybrid_search",
        persistence_depth="cache_plus_memory",
        enable_memory_metadata=True,
        enable_json_payload=True,
        enable_provenance_metadata=True,
    ),
)

results = client.search(
    query="latest Oracle Database vector search features",
    max_results=5,
    max_local=5,
    max_foreign=5,
    save_foreign=True,
    search_depth="basic",
)

for result in results:
    print(result["origin"], round(result["score"], 3), result["content"][:120])
```

When `max_foreign > 0`, this mode calls Tavily, merges Oracle and Tavily candidates, reranks them, and can persist Tavily results for later reuse.

## Mode 2: Freshness Cache

Use `freshness_cache` when repeated or nearby queries should be served from Oracle while the cache TTL is valid.

```python
cache_client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=OracleDB(
        connection=connection,
        table_name="TAVILY_DOCUMENTS",
        retrieval_mode="freshness_cache",
        cache_ttl_seconds=3600,
        cache_score_threshold=0.70,
        persistence_depth="cache_only",
        enable_memory_metadata=True,
        enable_json_payload=True,
        enable_provenance_metadata=True,
    ),
)

first = cache_client.search(
    "latest Oracle Database vector search features",
    max_results=3,
    max_local=3,
    max_foreign=3,
    save_foreign=True,
)

second = cache_client.search(
    "latest Oracle Database vector search features",
    max_results=3,
    max_local=3,
    max_foreign=3,
    save_foreign=True,
)

print([row["origin"] for row in first])
print([row["origin"] for row in second])
```

Expected behavior:

- First run: Oracle cache misses, Tavily returns fresh results, and `save_foreign=True` writes them into Oracle.
- Second run: Oracle returns fresh cache rows and Tavily is skipped.

## Mode 3: Cache Then Memory

Use `cache_then_memory` when you want a fresh-cache tier first, durable Oracle memory second, and Tavily only as the final fallback.

This mode requires the Oracle memory metadata columns because the memory tier is scoped by `MEMORY_SCOPE`. When `persistence_depth` is omitted, this mode defaults it to `cache_plus_memory` so newly saved Tavily rows remain reusable after the cache TTL expires.

```python
memory_client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=OracleDB(
        connection=connection,
        table_name="TAVILY_DOCUMENTS",
        retrieval_mode="cache_then_memory",
        cache_ttl_seconds=3600,
        cache_score_threshold=0.70,
        memory_score_threshold=0.65,
        memory_max_results=5,
        persistence_depth="cache_plus_memory",
        enable_memory_metadata=True,
        enable_json_payload=True,
        enable_provenance_metadata=True,
    ),
)

results = memory_client.search(
    "How can Oracle VECTOR help AI agents keep search memory fresh?",
    max_results=5,
    max_local=5,
    max_foreign=5,
    save_foreign=True,
)
```

This mode checks:

1. Fresh Oracle rows inside `cache_ttl_seconds`.
2. Durable Oracle memory rows with `memory_scope="cache_plus_memory"`.
3. Tavily Search, if both Oracle tiers miss.

## Inspect Stored Provenance

With `enable_json_payload=True` and `enable_provenance_metadata=True`, persisted rows are reviewable with normal SQL.

```sql
SELECT source_title,
       source_url,
       retrieval_query,
       retrieval_mode,
       inserted_from,
       provider_name
FROM tavily_documents
ORDER BY retrieval_timestamp DESC
FETCH FIRST 10 ROWS ONLY;
```

You can also query the JSON payload directly.

```sql
SELECT JSON_VALUE(raw_payload, '$.provenance.retrieval_query') AS query_text,
       JSON_VALUE(raw_payload, '$.provenance.provider_name') AS provider
FROM tavily_documents
WHERE raw_payload IS NOT NULL;
```

## Optional: Native Oracle Hybrid Search

Oracle local retrieval can combine vector similarity with Oracle Text scoring.

Create or verify the Oracle Text index through the adapter helper, then enable native hybrid search:

```python
client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=OracleDB(
        connection=connection,
        table_name="TAVILY_DOCUMENTS",
        retrieval_mode="hybrid_search",
        text_index_name="TAVILY_DOCS_TEXT_IDX",
        enable_native_hybrid_search=True,
    ),
)

created = client.database.ensure_text_index()
print("Created text index:", created)
```

`ensure_text_index()` returns `True` when it creates the index and `False` when the configured index already exists. If `text_index_name` is omitted, the helper uses `{TABLE_NAME}_{CONTENT_FIELD}_TEXT_IDX`.

Use this when your local Oracle table has enough content for both lexical and semantic matching. The provider sanitizes free-form user questions before routing them into Oracle Text. If Oracle Text still rejects a query at runtime, the provider falls back to vector-only Oracle search so the request can continue.

## Optional: Create a Vector Index

For larger tables, create a vector index through the SDK helper.

```python
client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=OracleDB(
        connection=connection,
        table_name="TAVILY_DOCUMENTS",
        vector_index_name="TAVILY_DOCS_VEC_IDX",
        vector_index_type="HNSW",
        vector_index_distance="COSINE",
    ),
)

created = client.database.ensure_vector_index()
print("Created index:", created)
```

Oracle local hybrid search uses the configured `vector_index_distance` for `VECTOR_DISTANCE(...)`. Cache/memory score thresholds and semantic deduplication currently require `COSINE`, because those controls compare against cosine-style similarity scores.

## Critical Knobs

| Option | Default | Use it when |
| --- | --- | --- |
| `retrieval_mode` | `"hybrid_search"` | You want to choose between merge, cache-only, and cache-then-memory behavior. |
| `cache_ttl_seconds` | `86400` | You need a shorter or longer freshness window. |
| `cache_score_threshold` | `0.0` | You only want high-confidence cache hits. |
| `memory_score_threshold` | `0.0` | You only want high-confidence durable memory hits. |
| `persistence_depth` | `"cache_only"`; `"cache_plus_memory"` in `cache_then_memory` | You want rows to expire as cache or survive as memory. |
| `enable_memory_metadata` | `False` | You want lifecycle fields such as `MEMORY_SCOPE`, `EXPIRES_AT`, and `QUERY_COUNT`; `cache_then_memory` enables this internally. |
| `enable_json_payload` | `False` | You want the raw Tavily result payload stored in Oracle. |
| `enable_provenance_metadata` | `False` | You want source URL, query, mode, cache-hit, and provider columns. |
| `dedup_similarity_threshold` | `None` | You want to skip near-duplicate Oracle inserts. |
| `upsert_key` | `None` | You want to update existing rows by `source_url` or `content_hash`. |
| `oracle_max_persisted_foreign` | `None` | You want to cap how many Tavily results are written per search. |
| `oracle_persist_score_threshold` | `None` | You only want to persist Tavily results above a score threshold. |
| `auto_cleanup_cache` | `False` | You want expired cache-only rows cleaned before searches. |

## Production Notes

- Keep database connection lifecycle in your application. Pass an existing `oracledb` connection into `OracleDB`.
- Use `save_foreign=True` only when you want Tavily results written into Oracle.
- Use `cache_only` for short-lived cache rows and `cache_plus_memory` for long-term memory rows.
- Keep unrelated hand-curated local knowledge in a separate table, or mark Tavily-managed rows with provenance fields, so cleanup can safely tell cache rows apart from durable knowledge.
- Treat Oracle vector scores and Tavily scores as ranking signals, not calibrated probabilities.
- Add `oracle_max_persisted_foreign`, `oracle_persist_score_threshold`, `upsert_key`, or `dedup_similarity_threshold` before running high-volume workloads.
- Use `AsyncTavilyClient` for direct Tavily API fan-out. `TavilyHybridClient` is currently a synchronous hybrid retrieval helper.
- Store secrets in environment variables or a secret manager, not in notebooks or committed files.

## Troubleshooting

| Issue | Check |
| --- | --- |
| `connection is required when Oracle credentials are not provided` | Create an `oracledb.connect(...)` connection before constructing `OracleDB`, or pass credentials to `OracleDB`. |
| `table_name is required for OracleDB` | Pass the Oracle table that stores `content` and `embeddings`. |
| Missing column errors on `save_foreign=True` | Add the optional metadata columns for the features you enabled. `cache_then_memory` needs the memory metadata columns. |
| Vector search errors | Confirm the database supports `VECTOR`, the column dimension matches your embedding model, and rows have embeddings. |
| Cache always misses | Confirm `ADDED_AT` exists, has timestamps, and `cache_score_threshold` is not too high. |
| Tavily keeps getting called in `hybrid_search` | That is expected when `max_foreign > 0`; use `freshness_cache` or `cache_then_memory` to avoid repeat Tavily calls. |
| Default embedding/ranking errors | Install the `oracle` or `hybrid` extra, set `CO_API_KEY`, or pass custom `embedding_function` and `ranking_function`. |
