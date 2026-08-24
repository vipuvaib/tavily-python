from tavily.databases.base import Database, RetrievalAction, RetrievalDecision
from tavily.databases.mongodb.mongodb import MongoDB
from tavily.databases.oracledb.oracledb import OracleDB


def create_legacy_database(options):
    """Build a database adapter from the pre-adapter client keyword arguments.

    This compatibility bridge keeps existing applications working while new
    code passes a configured ``MongoDB`` or ``OracleDB`` object to
    ``TavilyHybridClient``. It deliberately belongs in the database package so
    the shared client does not contain provider-specific construction logic.
    """
    options = dict(options)
    provider = options.pop("db_provider", None)

    if provider == "mongodb":
        retrieval_mode = options.get("retrieval_mode", "hybrid_search")
        if retrieval_mode in ("freshness_cache", "cache_then_memory"):
            raise ValueError(
                "retrieval_mode='freshness_cache' and "
                "retrieval_mode='cache_then_memory' are only supported when "
                "db_provider='oracle'."
            )
        return MongoDB(
            collection=options.get("collection"),
            index=options.get("index"),
            embeddings_field=options.get("embeddings_field", "embeddings"),
            content_field=options.get("content_field", "content"),
            uri=options.get("mongo_uri"),
            database_name=options.get("mongo_database"),
            collection_name=options.get("mongo_collection"),
            client_kwargs=options.get("mongo_client_kwargs"),
        )

    if provider == "oracle":
        return OracleDB(
            connection=options.get("connection"),
            table_name=options.get("table_name"),
            embeddings_field=options.get("embeddings_field", "embeddings"),
            content_field=options.get("content_field", "content"),
            retrieval_mode=options.get("retrieval_mode", "hybrid_search"),
            cache_ttl_seconds=options.get("cache_ttl_seconds", 86400),
            cache_score_threshold=options.get("cache_score_threshold", 0.0),
            cache_timestamp_field=options.get("cache_timestamp_field", "ADDED_AT"),
            persistence_depth=options.get("persistence_depth"),
            memory_score_threshold=options.get("memory_score_threshold", 0.0),
            memory_max_results=options.get("memory_max_results"),
            enable_memory_metadata=options.get("enable_oracle_memory_metadata", False),
            enable_native_hybrid_search=options.get("enable_native_hybrid_search", False),
            metadata_filters=options.get("oracle_metadata_filters"),
            enable_json_payload=options.get("enable_oracle_json_payload", False),
            enable_provenance_metadata=options.get("enable_provenance_metadata", False),
            vector_index_name=options.get("vector_index_name"),
            vector_index_type=options.get("vector_index_type", "HNSW"),
            vector_index_distance=options.get("vector_index_distance", "COSINE"),
            vector_index_accuracy=options.get("vector_index_accuracy", 90),
            vector_index_neighbors=options.get("vector_index_neighbors", 40),
            vector_index_efconstruction=options.get("vector_index_efconstruction", 500),
            vector_index_partitions=options.get("vector_index_partitions", 10),
            dedup_similarity_threshold=options.get("dedup_similarity_threshold"),
            upsert_key=options.get("oracle_upsert_key"),
            oracle_max_persisted_foreign=options.get(
                "oracle_max_persisted_foreign",
                options.get("max_persisted_foreign"),
            ),
            oracle_persist_score_threshold=options.get(
                "oracle_persist_score_threshold",
                options.get("persist_score_threshold"),
            ),
            auto_cleanup_cache=options.get("auto_cleanup_cache", False),
            cache_cleanup_interval_seconds=options.get("cache_cleanup_interval_seconds", 3600),
            text_index_name=options.get("text_index_name"),
            user=options.get("oracle_user"),
            password=options.get("oracle_password"),
            dsn=options.get("oracle_dsn"),
            connection_kwargs=options.get("oracle_connection_kwargs"),
        )

    raise ValueError(
        "database is required. Pass a configured MongoDB or OracleDB instance."
    )


__all__ = [
    "Database",
    "MongoDB",
    "OracleDB",
    "RetrievalAction",
    "RetrievalDecision",
    "create_legacy_database",
]
