"""Real Oracle integration demo for TavilyHybridClient.

This script uses real Tavily + real OracleDB connections. No fake clients.

Quick start:
  1) Install deps in your venv:
       python -m pip install -e ".[oracle]"
  2) Configure the required local values described in .env.example.
  3) Run one of the demos below.

Oracle freshness cache demo (repeat query to show cache behavior):
  python examples/demo_integration_cache_hybrid.py \
    --oracle-retrieval-mode freshness_cache \
    --query "latest Oracle vector search features" \
    --repeat 2 \
    --save-foreign

Oracle cache-then-memory demo:
  python examples/demo_integration_cache_hybrid.py \
    --oracle-retrieval-mode cache_then_memory \
    --query "latest Oracle vector search features" \
    --repeat 2 \
    --save-foreign \
    --persistence-depth cache_plus_memory
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB


README_CHECKPOINTS = [
    'retrieval_mode="hybrid_search"',
    'retrieval_mode="freshness_cache"',
    'retrieval_mode="cache_then_memory"',
    "docs/oracle_architecture.md",
]


def first_env(*keys: str, default: str | None = None) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return default


def load_root_env_file(repo_root: Path) -> None:
    """Load key=value pairs from repo_root/.env into os.environ if missing."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]

        os.environ.setdefault(key, value)


def parser_with_defaults() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run real Tavily hybrid RAG integration demos for Oracle."
    )
    parser.add_argument("--api-key", default=os.getenv("TAVILY_API_KEY"))
    parser.add_argument(
        "--query",
        required=True,
        help="Query sent to TavilyHybridClient.search().",
    )
    parser.add_argument("--max-results", type=int, default=2)
    parser.add_argument("--max-local", type=int, default=3)
    parser.add_argument("--max-foreign", type=int, default=3)
    parser.add_argument(
        "--save-foreign",
        action="store_true",
        help="Persist Tavily foreign results into local DB.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Number of repeated search runs (useful to demonstrate cache hits).",
    )
    parser.add_argument(
        "--skip-docs-check",
        action="store_true",
        help="Skip README checkpoint validation output.",
    )

    # Oracle args
    parser.add_argument("--oracle-user", default=os.getenv("ORACLE_USER"))
    parser.add_argument("--oracle-password", default=os.getenv("ORACLE_PASSWORD"))
    parser.add_argument("--oracle-dsn", default=os.getenv("ORACLE_DSN"))
    parser.add_argument(
        "--oracle-table",
        default=first_env("ORACLE_TABLE", "ORACLE_CLI_DEMO_TABLE", "ORACLE_VECTOR_TABLE", default="TAVILY_DOCUMENTS"),
    )
    parser.add_argument(
        "--oracle-cache-timestamp-field",
        default=first_env("ORACLE_CACHE_TIMESTAMP_FIELD", default="ADDED_AT"),
        help="Oracle timestamp column used for cache TTL filtering.",
    )
    parser.add_argument(
        "--oracle-retrieval-mode",
        choices=["hybrid_search", "freshness_cache", "cache_then_memory"],
        default=os.getenv("ORACLE_RETRIEVAL_MODE", "freshness_cache"),
    )
    parser.add_argument("--cache-ttl-seconds", type=int, default=int(os.getenv("CACHE_TTL_SECONDS", "3600")))
    parser.add_argument("--cache-score-threshold", type=float, default=float(os.getenv("CACHE_SCORE_THRESHOLD", "0.75")))
    parser.add_argument("--memory-score-threshold", type=float, default=float(os.getenv("MEMORY_SCORE_THRESHOLD", "0.65")))
    parser.add_argument("--memory-max-results", type=int, default=int(os.getenv("MEMORY_MAX_RESULTS", "5")))
    parser.add_argument(
        "--persistence-depth",
        choices=["cache_only", "cache_plus_memory"],
        default=os.getenv("PERSISTENCE_DEPTH", "cache_only"),
    )
    parser.add_argument(
        "--enable-oracle-memory-metadata",
        action="store_true",
        help="Enable Oracle cache/memory metadata columns when persisting foreign results.",
    )
    parser.add_argument(
        "--enable-native-hybrid-search",
        action="store_true",
        help="Enable Oracle text + vector local candidate retrieval.",
    )

    return parser


def validate_common_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if not args.api_key:
        parser.error("Missing Tavily API key. Set TAVILY_API_KEY or pass --api-key.")
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")


def validate_oracle_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    missing = [
        name
        for name, value in (
            ("--oracle-user / ORACLE_USER", args.oracle_user),
            ("--oracle-password / ORACLE_PASSWORD", args.oracle_password),
            ("--oracle-dsn / ORACLE_DSN", args.oracle_dsn),
            ("--oracle-table / ORACLE_TABLE", args.oracle_table),
        )
        if not value
    ]
    if missing:
        parser.error("Missing Oracle configuration: " + ", ".join(missing))


def print_results(label: str, results: list[dict[str, Any]]) -> None:
    print(f"\n{label}")
    if not results:
        print("  (no results)")
        return

    for i, row in enumerate(results, start=1):
        content = str(row.get("content", ""))
        snippet = content[:90] + ("..." if len(content) > 90 else "")
        score = row.get("score", 0)
        origin = row.get("origin", "unknown")
        print(f"  {i}. origin={origin:<7} score={float(score):.4f} content={snippet}")

    local_count = sum(1 for row in results if row.get("origin") == "local")
    foreign_count = sum(1 for row in results if row.get("origin") == "foreign")
    print(f"  Summary: local={local_count}, foreign={foreign_count}, total={len(results)}")


def docs_checkpoint(repo_root: Path) -> None:
    print("\n=== Docs checkpoint ===")
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    for checkpoint in README_CHECKPOINTS:
        status = "FOUND" if checkpoint in readme else "MISSING"
        print(f"  - {checkpoint}: {status}")


def ensure_oracle_table_exists(connection: Any, table_name: str) -> None:
    sql = """
        SELECT COUNT(*)
        FROM USER_TABLES
        WHERE TABLE_NAME = :table_name
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, table_name=table_name.upper())
        count = cursor.fetchone()[0]
    if count == 0:
        raise RuntimeError(
            f"Oracle table '{table_name.upper()}' was not found in the connected schema. "
            "Create it in the same user/schema as ORACLE_USER, or connect with the schema owner."
        )


def ensure_oracle_column_exists(connection: Any, table_name: str, column_name: str) -> None:
    sql = """
        SELECT COUNT(*)
        FROM USER_TAB_COLUMNS
        WHERE TABLE_NAME = :table_name
          AND COLUMN_NAME = :column_name
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            table_name=table_name.upper(),
            column_name=column_name.upper(),
        )
        count = cursor.fetchone()[0]
    if count == 0:
        raise RuntimeError(
            f"Column '{column_name.upper()}' was not found in table '{table_name.upper()}'. "
            "For freshness cache modes, set --oracle-cache-timestamp-field to an existing "
            "timestamp column (for example CREATED_AT), or add ADDED_AT to the table."
        )


def run_oracle_demo(args: argparse.Namespace) -> None:
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError(
            "oracledb is not installed. Install with: python -m pip install -e '.[oracle]'"
        ) from exc

    print(f"\n=== Running REAL Oracle {args.oracle_retrieval_mode} demo ===")
    connection = oracledb.connect(
        user=args.oracle_user,
        password=args.oracle_password,
        dsn=args.oracle_dsn,
    )
    ensure_oracle_table_exists(connection, args.oracle_table)
    if args.oracle_retrieval_mode in ("freshness_cache", "cache_then_memory"):
        ensure_oracle_column_exists(
            connection,
            args.oracle_table,
            args.oracle_cache_timestamp_field,
        )

    client = TavilyHybridClient(
        api_key=args.api_key,
        database=OracleDB(
            connection=connection,
            table_name=args.oracle_table,
            retrieval_mode=args.oracle_retrieval_mode,
            cache_timestamp_field=args.oracle_cache_timestamp_field,
            cache_ttl_seconds=args.cache_ttl_seconds,
            cache_score_threshold=args.cache_score_threshold,
            memory_score_threshold=args.memory_score_threshold,
            memory_max_results=args.memory_max_results,
            persistence_depth=args.persistence_depth,
            enable_memory_metadata=args.enable_oracle_memory_metadata,
            enable_native_hybrid_search=args.enable_native_hybrid_search,
        ),
    )

    try:
        for i in range(1, args.repeat + 1):
            results = client.search(
                query=args.query,
                max_results=args.max_results,
                max_local=args.max_local,
                max_foreign=args.max_foreign,
                save_foreign=args.save_foreign,
            )
            print_results(f"Oracle run #{i}", results)
    finally:
        client.close()
        connection.close()


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    load_root_env_file(repo_root)

    parser = parser_with_defaults()
    args = parser.parse_args()

    validate_common_args(args, parser)

    if not args.skip_docs_check:
        docs_checkpoint(repo_root)

    validate_oracle_args(args, parser)
    run_oracle_demo(args)


if __name__ == "__main__":
    main()
