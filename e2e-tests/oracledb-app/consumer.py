"""Run from an isolated installed-wheel environment, in a fresh process."""
import hashlib
import importlib.metadata
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import oracledb
import requests
import tavily
from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def normalize(value):
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return [normalize(item) for item in value]


def snapshot(config):
    # A separate session proves visibility of committed writes.
    with oracledb.connect(user=os.environ["ORACLE_USER"],
                          password=os.environ["ORACLE_PASSWORD"],
                          dsn=os.environ["ORACLE_DSN"], tcp_connect_timeout=10) as connection:
        connection.call_timeout = 15000
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM " + config["table"] + " ORDER BY ID")
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, map(normalize, row))) for row in cursor.fetchall()]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Transport(requests.Session):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.attempts = 0
        self.responses = []
        if config["profile"] == "smoke":
            self.trust_env = False

    def send(self, request, **kwargs):
        self.attempts += 1
        allowed = 1 if self.config["phase"] == "seed" else 0
        check(self.attempts <= allowed, "Unexpected additional Tavily HTTP request")
        url = urlsplit(request.url)
        check(url.scheme == "https" and url.netloc == "api.tavily.com" and url.path == "/search",
              "Unexpected SDK endpoint")
        if self.config["profile"] == "smoke":
            request.url = self.config["endpoint"] + "/search"
        response = super().send(request, **kwargs)
        check(not response.is_redirect, "Unexpected redirect")
        if response.status_code == 200:
            self.responses.append(response.json())
        return response


def run(config):
    package = Path(tavily.__file__).resolve()
    check(package.is_relative_to(Path(sys.prefix).resolve()), "SDK did not resolve from consumer venv")
    query = config["query"]
    embedding_calls = []

    def embed(texts, input_type):
        embedding_calls.append({"input_type": input_type, "count": len(texts)})
        # Fixed vectors certify routing/storage; this slice does not measure semantic quality.
        return [[1.0, 0.0, 0.0] for _ in texts]

    def rank(*args):
        raise AssertionError("Freshness cache unexpectedly invoked reranking")

    session = Transport(config)
    database = OracleDB(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
                        dsn=os.environ["ORACLE_DSN"], table_name=config["table"],
                        connection_kwargs={"tcp_connect_timeout": 10},
                        retrieval_mode="freshness_cache", cache_ttl_seconds=3600,
                        cache_score_threshold=0.99, persistence_depth="cache_only",
                        metadata_filters={"TENANT_ID": config["run_id"]},
                        enable_memory_metadata=True, enable_provenance_metadata=True,
                        enable_json_payload=True)
    database.connection.call_timeout = 15000
    client = None
    cases = []
    try:
        client = TavilyHybridClient(
            api_key=os.environ["TAVILY_API_KEY"] if config["profile"] == "live" else "fixture-key",
            database=database, embedding_function=embed, ranking_function=rank, session=session)
        search_options = dict(max_results=2, max_local=2, max_foreign=2, save_foreign=True,
                              search_depth="basic", include_usage=True, timeout=30)
        if config["phase"] == "seed":
            check(snapshot(config) == [], "Fresh run table is not empty")
            cold = client.search(query, **search_options)
            check(0 < len(cold) <= 2, "Cold search did not return 1-2 results")
            check(all(set(row) == {"content", "score", "origin"} and row["origin"] == "foreign"
                      for row in cold), "Cold response projection mismatch")
            check(session.attempts == 1 and len(session.responses) == 1, "Expected one HTTP response")
            foreign = session.responses[0]["results"]
            check(cold == [{"content": row["content"], "score": row["score"], "origin": "foreign"}
                           for row in foreign[:2]], "Cold response differs from provider response")
            cases.append({"id": "E01", "name": "Cold search uses real SDK HTTP transport",
                          "http_requests": 1, "result_count": len(cold),
                          "usage": session.responses[0].get("usage")})
            stored = snapshot(config)
            check(len(stored) == len(foreign), "Committed row count differs from provider results")
            for row, original in zip(stored, foreign):
                check(row["CONTENT"] == original["content"], "CLOB content round trip failed")
                check(row["EMBEDDINGS"] == [1.0, 0.0, 0.0], "Vector round trip failed")
                check(row["SOURCE_URL"] == original.get("url") and
                      row["SOURCE_TITLE"] == original.get("title"), "Source metadata mismatch")
                check(row["TENANT_ID"] == config["run_id"], "Ownership mismatch")
                check(row["RETRIEVAL_QUERY"] == query and row["RETRIEVAL_MODE"] == "freshness_cache",
                      "Retrieval provenance mismatch")
                check(row["PROVIDER_NAME"] == row["INSERTED_FROM"] == "tavily", "Origin mismatch")
                check(row["MEMORY_SCOPE"] == "cache_only" and row["CACHE_HIT"] == 0 and
                      row["QUERY_COUNT"] == 1, "Lifecycle metadata mismatch")
                check(all(row[key] for key in ("ADDED_AT", "EXPIRES_AT", "LAST_SEEN_AT",
                                               "RETRIEVAL_TIMESTAMP")), "Missing timestamps")
                payload = row["RAW_PAYLOAD"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                check(payload["result"] == original and payload["provenance"]["retrieval_query"] == query,
                      "Native JSON payload round trip failed")
            check(embedding_calls == [{"input_type": "search_query", "count": 1},
                                      {"input_type": "search_document", "count": len(foreign)}],
                  "Unexpected embedding calls")
            cases.append({"id": "E02", "name": "Independent Oracle session sees committed vectors and metadata",
                          "row_count": len(stored), "snapshot_sha256": digest(stored)})
        else:
            stored = snapshot(config)
            check(stored and digest(stored) == config["snapshot_sha256"],
                  "Committed rows changed before restart")
        before_requests = session.attempts
        warm = client.search(query, **search_options)
        check(len(warm) == min(len(stored), 2), "Warm result count mismatch")
        # Equal vectors have no defined tie ordering. Compare the full fixture result set.
        check(sorted(row["content"] for row in warm) == sorted(row["CONTENT"] for row in stored[:2]),
              "Warm content differs from persisted content")
        check(all(set(row) == {"content", "score", "origin"} and row["origin"] == "local"
                  and abs(row["score"] - 1.0) < 1e-6 for row in warm), "Warm result mismatch")
        check(session.attempts == before_requests, "Cache hit attempted HTTP")
        check(snapshot(config) == stored, "Cache hit changed persisted metadata or row count")
        cases.append({"id": "E03" if config["phase"] == "seed" else "E04",
                      "name": "Warm cache avoids HTTP and writes" if config["phase"] == "seed"
                              else "Replacement process reads persisted cache without HTTP",
                      "http_requests": 0, "result_count": len(warm)})
        return {"cases": cases, "pid": os.getpid(), "package_path": str(package),
                "package_version": importlib.metadata.version("tavily-python"),
                "snapshot_sha256": digest(stored), "http_requests": session.attempts,
                "embedding_calls": embedding_calls}
    finally:
        if client is not None:
            client.close()
        else:
            database.close()
        session.close()


if __name__ == "__main__":
    try:
        config = json.load(sys.stdin)
        import re
        check(bool(re.fullmatch(r"TVE_[A-F0-9]{20}", config["table"])), "Invalid table name")
        print(json.dumps(run(config), sort_keys=True))
    except Exception as error:
        message = str(error)
        for key in ("ORACLE_PASSWORD", "ORACLE_DSN", "TAVILY_API_KEY"):
            if os.environ.get(key):
                message = message.replace(os.environ[key], "<redacted>")
        print(json.dumps({"error": type(error).__name__ + ": " + message}))
        raise SystemExit(1)
