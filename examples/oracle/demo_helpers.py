import hashlib
import math
import os
import re
from collections import Counter
from pathlib import Path
from textwrap import shorten

import oracledb
from IPython.display import Markdown, display

from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB, oracledb as tavily_oracle_db


def load_local_env():
    """Load .env from the current directory or one of its parents."""
    cwd = Path.cwd().resolve()
    helper_dir = Path(__file__).resolve().parent
    candidates = [
        cwd / ".env",
        *[parent / ".env" for parent in cwd.parents],
        helper_dir / ".env",
        *[parent / ".env" for parent in helper_dir.parents],
    ]
    seen = set()
    for path in candidates:
        path = path.expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            continue

        with path.open(encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not value:
                    continue
                if key.startswith("ORACLE_"):
                    os.environ[key] = value
                else:
                    os.environ.setdefault(key, value)

        print("Loaded environment from", path)
        return

    print("No .env file found; using already exported environment variables.")


def clear_proxy_env_for_tavily():
    """Avoid routing Tavily API calls through stale local proxy settings."""
    if os.environ.get("TAVILY_USE_ENV_PROXY", "0") == "1":
        print("Keeping proxy variables because TAVILY_USE_ENV_PROXY=1")
        return

    for key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        os.environ.pop(key, None)


load_local_env()
clear_proxy_env_for_tavily()

_required_env_vars = ["TAVILY_API_KEY", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN"]
_missing_env_vars = [name for name in _required_env_vars if not os.environ.get(name)]
if _missing_env_vars:
    raise RuntimeError("Missing required environment variables: " + ", ".join(_missing_env_vars))

TABLE_NAME = tavily_oracle_db.validate_identifier(
    os.environ.get("ORACLE_VECTOR_TABLE", "TAVILY_DOCUMENTS"),
    "ORACLE_VECTOR_TABLE",
)
CONTENT_FIELD = tavily_oracle_db.validate_identifier(
    os.environ.get("ORACLE_CONTENT_FIELD", "CONTENT"),
    "ORACLE_CONTENT_FIELD",
)
EMBEDDINGS_FIELD = tavily_oracle_db.validate_identifier(
    os.environ.get("ORACLE_EMBEDDINGS_FIELD", "EMBEDDINGS"),
    "ORACLE_EMBEDDINGS_FIELD",
)
CACHE_TIMESTAMP_FIELD = tavily_oracle_db.validate_identifier(
    os.environ.get("ORACLE_CACHE_TIMESTAMP_FIELD", "ADDED_AT"),
    "ORACLE_CACHE_TIMESTAMP_FIELD",
)
VECTOR_INDEX_NAME = tavily_oracle_db.validate_identifier(
    os.environ.get("ORACLE_VECTOR_INDEX_NAME", "TAVILY_DOCS_VEC_IDX"),
    "ORACLE_VECTOR_INDEX_NAME",
)
EMBEDDING_DIMENSION = int(os.environ.get("ORACLE_VECTOR_DIMENSION", "1024"))
TAVILY_SEARCH_OPTIONS = {"search_depth": os.environ.get("TAVILY_SEARCH_DEPTH", "basic")}

connection = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    dsn=os.environ["ORACLE_DSN"],
)
print("Connected to Oracle. Table:", TABLE_NAME)


def stable_embedding(text, dimensions=EMBEDDING_DIMENSION):
    """Deterministic demo embedding; production should use the app embedding model."""
    vector = [0.0] * dimensions
    tokens = re.findall(r"[a-z0-9]+", str(text).lower())
    if not tokens:
        return vector

    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def demo_embedding_function(texts, input_type):
    return [stable_embedding(text) for text in texts]


def score_ranking_function(query, documents, top_n):
    return sorted(
        documents,
        key=lambda document: float(document.get("score") or 0),
        reverse=True,
    )[:top_n]


def markdown_table(rows, columns):
    if not rows:
        return "No rows."

    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column, "")
            text = str(value).replace("\n", " ").replace("|", "\\|")
            values.append(text)
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([header, separator, *body])


def display_table(rows, columns, title=None):
    if title:
        display(Markdown(f"### {title}"))
    display(Markdown(markdown_table(rows, columns)))


def result_rows(results, limit=5):
    rows = []
    for rank, result in enumerate(results[:limit], start=1):
        score = result.get("score")
        try:
            score = f"{float(score):.4f}"
        except (TypeError, ValueError):
            score = ""

        rows.append({
            "rank": rank,
            "origin": result.get("origin", ""),
            "score": score,
            "preview": shorten(
                str(result.get("content", "")).replace("\n", " "),
                width=120,
                placeholder="...",
            ),
        })
    return rows


def show_results(title, results):
    origins = Counter(result.get("origin", "unknown") for result in results)
    display(Markdown(f"### {title}\n`total={len(results)}` `origins={dict(origins)}`"))
    display_table(result_rows(results), ["rank", "origin", "score", "preview"])


def table_exists():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME = :table_name",
            table_name=TABLE_NAME,
        )
        return cursor.fetchone()[0] > 0


def existing_columns():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM USER_TAB_COLUMNS
            WHERE TABLE_NAME = :table_name
            """,
            table_name=TABLE_NAME,
        )
        return {row[0] for row in cursor.fetchall()}


def ensure_column(column_name, ddl):
    if column_name in existing_columns():
        return False
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {TABLE_NAME} ADD ({ddl})")
    connection.commit()
    return True


def ensure_demo_table():
    if not table_exists():
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE {TABLE_NAME} (
                    ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    {CONTENT_FIELD} CLOB,
                    {EMBEDDINGS_FIELD} VECTOR({EMBEDDING_DIMENSION}, FLOAT32),
                    {CACHE_TIMESTAMP_FIELD} TIMESTAMP DEFAULT SYSTIMESTAMP,
                    RAW_PAYLOAD JSON,
                    SOURCE_URL VARCHAR2(1000),
                    SOURCE_TITLE VARCHAR2(500),
                    RETRIEVAL_QUERY VARCHAR2(1000),
                    RETRIEVAL_TIMESTAMP TIMESTAMP WITH TIME ZONE,
                    RETRIEVAL_MODE VARCHAR2(30),
                    CACHE_HIT NUMBER(1),
                    INSERTED_FROM VARCHAR2(30),
                    PROVIDER_NAME VARCHAR2(50),
                    MEMORY_SCOPE VARCHAR2(30),
                    EXPIRES_AT TIMESTAMP WITH TIME ZONE,
                    LAST_SEEN_AT TIMESTAMP WITH TIME ZONE,
                    QUERY_COUNT NUMBER DEFAULT 0,
                    CONTENT_HASH VARCHAR2(64)
                )
                """
            )
        connection.commit()
        print("Created table", TABLE_NAME)
        return

    column_ddls = {
        "RAW_PAYLOAD": "RAW_PAYLOAD JSON",
        "SOURCE_URL": "SOURCE_URL VARCHAR2(1000)",
        "SOURCE_TITLE": "SOURCE_TITLE VARCHAR2(500)",
        "RETRIEVAL_QUERY": "RETRIEVAL_QUERY VARCHAR2(1000)",
        "RETRIEVAL_TIMESTAMP": "RETRIEVAL_TIMESTAMP TIMESTAMP WITH TIME ZONE",
        "RETRIEVAL_MODE": "RETRIEVAL_MODE VARCHAR2(30)",
        "CACHE_HIT": "CACHE_HIT NUMBER(1)",
        "INSERTED_FROM": "INSERTED_FROM VARCHAR2(30)",
        "PROVIDER_NAME": "PROVIDER_NAME VARCHAR2(50)",
        "MEMORY_SCOPE": "MEMORY_SCOPE VARCHAR2(30)",
        "EXPIRES_AT": "EXPIRES_AT TIMESTAMP WITH TIME ZONE",
        "LAST_SEEN_AT": "LAST_SEEN_AT TIMESTAMP WITH TIME ZONE",
        "QUERY_COUNT": "QUERY_COUNT NUMBER DEFAULT 0",
        "CONTENT_HASH": "CONTENT_HASH VARCHAR2(64)",
    }
    added = [name for name, ddl in column_ddls.items() if ensure_column(name, ddl)]
    print("Table exists.", "Added columns: " + ", ".join(added) if added else "Schema is ready.")


ensure_demo_table()


def delete_rows_for_query(query):
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {TABLE_NAME} WHERE RETRIEVAL_QUERY = :query", query=query)
        deleted = cursor.rowcount
    connection.commit()
    print(f"Deleted {deleted} old demo rows for this query.")
    return deleted


def count_rows_for_query(query):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE RETRIEVAL_QUERY = :query", query=query)
        return cursor.fetchone()[0]


def show_persisted_rows(query, title="Persisted Oracle rows"):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT MEMORY_SCOPE,
                   RETRIEVAL_MODE,
                   CACHE_HIT,
                   QUERY_COUNT,
                   SOURCE_TITLE,
                   DBMS_LOB.SUBSTR({CONTENT_FIELD}, 120, 1) AS PREVIEW
            FROM {TABLE_NAME}
            WHERE RETRIEVAL_QUERY = :query
            ORDER BY LAST_SEEN_AT DESC NULLS LAST,
                     RETRIEVAL_TIMESTAMP DESC NULLS LAST
            FETCH FIRST 10 ROWS ONLY
            """,
            query=query,
        )
        rows = [
            {
                "memory_scope": row[0],
                "retrieval_mode": row[1],
                "cache_hit": row[2],
                "query_count": row[3],
                "source_title": shorten(str(row[4] or ""), width=40, placeholder="..."),
                "preview": shorten(str(row[5] or ""), width=90, placeholder="..."),
            }
            for row in cursor.fetchall()
        ]

    display_table(
        rows,
        ["memory_scope", "retrieval_mode", "cache_hit", "query_count", "source_title", "preview"],
        title,
    )


def make_client(retrieval_mode, **overrides):
    oracle_config = {
        "connection": connection,
        "table_name": TABLE_NAME,
        "embeddings_field": EMBEDDINGS_FIELD,
        "content_field": CONTENT_FIELD,
        "cache_timestamp_field": CACHE_TIMESTAMP_FIELD,
        "retrieval_mode": retrieval_mode,
        "enable_native_hybrid_search": False,
        "enable_json_payload": True,
        "enable_provenance_metadata": True,
        "enable_memory_metadata": True,
        "cache_ttl_seconds": 3600,
        "cache_score_threshold": -1.0,
        "memory_score_threshold": -1.0,
        "memory_max_results": 5,
        "persistence_depth": "cache_only",
        "dedup_similarity_threshold": None,
        "upsert_key": None,
        "oracle_max_persisted_foreign": None,
        "oracle_persist_score_threshold": None,
    }
    client_config = {
        "api_key": os.environ["TAVILY_API_KEY"],
        "embedding_function": demo_embedding_function,
        "ranking_function": score_ranking_function,
    }
    for key, value in overrides.items():
        if key == "oracle_upsert_key":
            oracle_config["upsert_key"] = value
        elif key == "max_persisted_foreign":
            oracle_config["oracle_max_persisted_foreign"] = value
        elif key == "persist_score_threshold":
            oracle_config["oracle_persist_score_threshold"] = value
        else:
            oracle_config[key] = value
    return TavilyHybridClient(
        database=OracleDB(**oracle_config),
        **client_config,
    )


print("Demo helpers are ready. Scores are ranking signals, not probabilities.")
