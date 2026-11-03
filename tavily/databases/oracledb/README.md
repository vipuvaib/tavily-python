# OracleDB Provider for Tavily Hybrid RAG

Configure `OracleDB`, then pass it as `TavilyHybridClient(database=...)`.
The adapter owns Oracle retrieval, cache/memory routing, SQL, persistence,
deduplication, cleanup, and index helpers. The shared client owns embeddings,
Tavily calls, and merge reranking.

Start with the [integration guide](../../../examples/oracle/integration.md).
See the [Oracle architecture](architecture.md) for routing and write details,
and the [common backend contract](../architecture.md) for provider boundaries.

## Setup and connection ownership

Use this fork's `upstream-port` checkout:

```bash
python -m pip install -e .
python -m pip install oracledb cohere
```

This branch has no `oracle`, `mongodb`, or `hybrid` installation extras.
Cohere supplies the default embedding and reranking functions; custom
callables can use another embedding/ranking stack. Set `CO_API_KEY` before
starting Python when using the defaults.

You can let the adapter open and manage an Oracle connection:

```python
import os

from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB

client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=OracleDB(
        user=os.environ["ORACLE_USER"],
        password=os.environ["ORACLE_PASSWORD"],
        dsn=os.environ["ORACLE_DSN"],
        table_name="TAVILY_DOCUMENTS",
        retrieval_mode="hybrid_search",
    ),
)

try:
    results = client.search("Oracle vector search", max_results=5)
finally:
    client.close()
```

Alternatively, pass `OracleDB(connection=connection, table_name=...)`.
An injected connection remains caller-owned and is left open by
`client.close()`. A connection opened from adapter credentials is closed
by `client.close()`, including when callable validation or subsequent client
initialization fails. `connection_kwargs` forwards additional driver options.

Oracle persistence, cleanup, and index helpers commit on the supplied
connection. Use a connection whose transaction lifecycle fits those operations.

## Retrieval modes

| Mode | Local lookup | Tavily call | Ranking |
| --- | --- | --- | --- |
| `hybrid_search` | Oracle vector search, optionally combined with Oracle Text. | Whenever `max_foreign > 0`. | Rerank the combined local and foreign candidates. |
| `freshness_cache` | Fresh rows meeting `cache_score_threshold`. | On cache miss when `max_foreign > 0`. | Return local hits or projected Tavily results directly. |
| `cache_then_memory` | Fresh cache, then `cache_plus_memory` rows meeting `memory_score_threshold`. | When both local tiers miss and `max_foreign > 0`. | Return local hits or projected Tavily results directly. |

`max_local` and `max_foreign` are search arguments and default to
`max_results`. `max_local=0` skips Oracle result lookup and selects the
foreign fallback route, even in `hybrid_search`; it does not rerank foreign
results. Automatic cleanup can still run before that routing decision.
`max_foreign=0` disables Tavily; a cache/memory miss then returns `[]`.

Both cache and memory use `max_local` as their candidate limit. There is no
`memory_max_results` setting.

## Table and metadata

Vector retrieval needs the configured text and `VECTOR` columns. The
[integration schema](../../../examples/oracle/integration.md#3-create-the-table)
also includes `ADDED_AT`, the default cache timestamp column, and shows
optional metadata columns. Match vector dimensions to your embedding function.

| Feature | Required or used columns |
| --- | --- |
| Basic vector retrieval and insertion | `CONTENT`, `EMBEDDINGS`, or configured equivalents. |
| Fresh-cache lookup | `EXPIRES_AT` when available; otherwise the configured cache timestamp. If both exist, the timestamp is the fallback for rows with a null expiry. |
| Automatic cache/lifecycle document construction | The configured cache timestamp, normally `ADDED_AT`. |
| `enable_memory_metadata=True` | `MEMORY_SCOPE`, `EXPIRES_AT`, `LAST_SEEN_AT`, `QUERY_COUNT`, plus the cache timestamp. |
| `enable_json_payload=True` | `RAW_PAYLOAD` with JSON-compatible storage. |
| `enable_provenance_metadata=True` | `SOURCE_URL`, `SOURCE_TITLE`, `RETRIEVAL_QUERY`, `RETRIEVAL_TIMESTAMP`, `RETRIEVAL_MODE`, `CACHE_HIT`, `INSERTED_FROM`, `PROVIDER_NAME`. |
| `upsert_key="source_url"` | `SOURCE_URL`; populated automatically when the Tavily result has a URL. |
| `upsert_key="content_hash"` | `CONTENT_HASH`; populated from nonempty content. |
| `metadata_filters` | Every configured ownership/filter column. |

`OracleDB.validate()` is currently a no-op. Constructor validation checks
settings and identifiers, but does not create or fully preflight the table.
Column discovery and insert-schema checks happen when needed. The schema
column cache belongs to the adapter instance; construct a new instance after
changing the table shape.

## Cache and durable memory

`persistence_depth` describes how automatically built rows should be retained:

| Value | Behavior |
| --- | --- |
| `cache_only` | Eligible for expired-cache cleanup. |
| `cache_plus_memory` | Reusable by the memory tier after cache expiry; preserved by cleanup. |

The default is `cache_plus_memory` for `cache_then_memory`, and
`cache_only` for the other modes. `cache_then_memory` enables memory
metadata internally. Cache modes also write lifecycle metadata automatically
when all four lifecycle columns exist, even if `enable_memory_metadata` was
not explicitly enabled.

`LAST_SEEN_AT` and `QUERY_COUNT` describe writes/upserts. Local cache or
memory hits return immediately and do not refresh timestamps or increment
those columns. Cache hits are not written back by the shared client.

## Ownership filters

`metadata_filters` applies to retrieval, cleanup, insertion, semantic
deduplication, and keyed upserts. For example:

```python
database = OracleDB(
    connection=connection,
    table_name="TAVILY_DOCUMENTS",
    metadata_filters={"TENANT_ID": "tenant-a"},
)
```

The table must contain `TENANT_ID`. A missing scalar value is filled into
a persistence document; a conflicting value is rejected. `None` means
`IS NULL`. A list, tuple, or set is an allowed-value filter and requires an
explicit, non-null permitted value in every document; automatic mapping does
not choose an owner from that list. An empty allowed set cannot be written.

Upserts match the key and the document's exact scope values, and do not update
scope columns. Semantic deduplication compares only that same exact scope,
so one allowed owner cannot suppress another owner's write.

## Persistence and semantic deduplication

| Option | Behavior |
| --- | --- |
| `oracle_persist_score_threshold` | Retain Tavily results whose score is at least the configured threshold. |
| `oracle_max_persisted_foreign` | Cap retained Tavily results in their existing order, after score filtering. |
| `dedup_similarity_threshold` | Skip unkeyed inserts when cosine similarity to an eligible stored or earlier accepted batch document meets the threshold. |
| `upsert_key` | `"source_url"` or `"content_hash"`; keyed documents use an ownership-scoped upsert. |

Persistence filtering happens before document embedding and affects writes,
not the returned search candidates. `save_foreign=True` uses automatic
document construction. A callable `save_foreign` receives each original
Tavily result plus `embeddings` and returns the full storage document; the
automatic metadata builder is bypassed.

Deduplication validates the whole batch first, then checks candidates in
order. Earlier retained documents can suppress later near duplicates in the
batch. Documents with a nonempty configured upsert key bypass semantic
suppression so they can update existing rows. Such a retained document can
still serve as an eligible reference for a later unkeyed candidate.

When present, `PROVIDER_NAME` and `INSERTED_FROM` must each equal
`"tavily"` for a row to suppress a duplicate. Automatic document construction
fills the available origin markers when deduplication is enabled, even with
detailed provenance disabled. Custom callbacks/direct documents must supply
their own origin markers. Existing unmarked rows are not backfilled. Without
either origin column, deduplication uses the remaining scope and lifecycle
predicates.

If both `MEMORY_SCOPE` and `EXPIRES_AT` exist, expired `cache_only`
rows do not suppress a new write. Durable memory remains eligible. Oracle
performs both stored-row and pending-document similarity checks; large batches
can require pairwise reads. This is per-call duplicate suppression, not a
uniqueness guarantee across concurrent writers.

## Cleanup

`database.cleanup_cache()` returns the deleted row count and commits.
`auto_cleanup_cache=True` invokes it before retrieval, rate-limited by
`cache_cleanup_interval_seconds` (default `3600`).

Cleanup applies `metadata_filters` to the entire deletion condition. It
preserves `cache_plus_memory` rows. Lifecycle-marked `cache_only` rows
are eligible when expired; a null expiry can fall back to an old cache timestamp.
When both lifecycle columns exist, cleanup can operate from expiry alone
without the cache timestamp column.

For legacy rows without a memory scope, cleanup requires a cache retrieval
mode in `RETRIEVAL_MODE` and checks each available Tavily origin marker.
A table lacking enough lifecycle/provenance information is left unchanged.
See the [cleanup rules](architecture.md#cleanup-rules) for partial schemas.

## Scores and native Oracle Text search

Vector-only retrieval defaults to:

```sql
1 - VECTOR_DISTANCE(embeddings, :query_vector, COSINE)
```

Local and Tavily scores are ranking signals, not calibrated probabilities.
`vector_index_distance` changes the vector metric for ordinary hybrid
retrieval. Cache/memory modes and semantic deduplication require `COSINE`.

`enable_native_hybrid_search` defaults to `False`. When explicitly
enabled, Oracle combines vector and Oracle Text candidates by row identity,
using `MAX(vector_score) + MAX(text_score)`, with `SCORE(1) / 100` as
the text component. Cache/memory thresholds compare the returned score, which
is this combined score when native hybrid is enabled. Semantic deduplication
continues to compare cosine vector similarity alone.

Native hybrid requires an Oracle Text index on the content column. The
adapter sanitizes text queries and retries vector-only retrieval for recognized
Oracle Text query errors; unrelated errors propagate.

## Index helpers

`database.ensure_vector_index()` and `database.ensure_text_index()`
return `True` after creating an index, or `False` if the chosen index name
already exists in `USER_INDEXES`. These helpers check name existence, not
the definition of an existing index. They run only when explicitly called.

Vector helper settings include `vector_index_name`, `vector_index_type`
(`HNSW` or `IVF`), `vector_index_distance`, `vector_index_accuracy`,
`vector_index_neighbors`, `vector_index_efconstruction`, and
`vector_index_partitions`. The default name is
`{TABLE_NAME}_{EMBEDDINGS_FIELD}_VEC_IDX`. The text helper uses
`text_index_name` or `{TABLE_NAME}_{CONTENT_FIELD}_TEXT_IDX`.

The helpers create indexes, not tables or database security policies. Database
features and privileges must support the requested index type.

## Implementation and tests

- [Oracle adapter](oracledb.py): routing, SQL, metadata, writes, and helpers.
- [Oracle constants](oracle_config.py): metadata field names and allowed index options.
- [Shared client](../../hybrid_rag/hybrid_rag.py): Cohere defaults and common orchestration.
- [Oracle behavior tests](../../../tests/test_hybrid_rag_oracle.py).
- [Ownership tests](../../../tests/test_oracle_persistence_scope.py).
- [Deduplication/provenance tests](../../../tests/test_oracle_dedup_provenance.py).
