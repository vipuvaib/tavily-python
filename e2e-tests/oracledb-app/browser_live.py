"""Internal browser demo: installed Tavily wheel -> Oracle cache -> live OCI RAG."""
import argparse
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import zipfile

from dotenv import dotenv_values
from browser_model import LiveModel
from common import (check, connect, preflight, private_values, sanitize, settings,
                    sha256, verify_owner, verify_schema_preserved, write_json)
from extended import FULL
from oracle_config import options
from run import load_package, provision

HERE = Path(__file__).resolve().parent
MAX_BODY = 8192
MAX_WEB_CALLS = 6
MAX_MODEL_CALLS = 16


def read_values(path):
    path = Path(path).expanduser().resolve()
    check(path.is_file() and path.stat().st_mode & 0o077 == 0, "Use an owner-only private env file")
    values = dict(dotenv_values(path, interpolate=False))
    required = ["CO_API_KEY", "TAVILY_API_KEY", "TAVILY_BROWSER_MODEL"] + [
        "TAVILY_BROWSER_OCI_" + name for name in
        ("CONFIG_FILE", "PROFILE", "AUTH", "ENDPOINT", "COMPARTMENT_ID", "MODEL_ID")]
    check(all(isinstance(values.get(k), str) and values[k] for k in required),
          "Missing private provider configuration")
    values.update(settings(path, live=True))
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
    return values


def verify_wheel(package):
    import tavily
    root = Path(tavily.__file__).resolve().parent
    check(Path(sys.prefix).resolve() in root.parents, "SDK must resolve inside the installed browser environment")
    checked = 0
    with zipfile.ZipFile(package["wheel"]) as archive:
        for name in archive.namelist():
            if name.startswith("tavily/") and not name.endswith("/"):
                path = root.parent / name
                check(path.is_file() and path.read_bytes() == archive.read(name), "Installed SDK differs from tested wheel")
                checked += 1
    check(checked > 10, "Installed-wheel verification was incomplete")
    return {"files": checked, "wheel_sha256": package["wheel_sha256"],
            "version": importlib.metadata.version("tavily-python")}


def normalize(value):
    if hasattr(value, "read"):
        return normalize(value.read())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return [normalize(v) for v in value]


def query_options(value):
    if not isinstance(value, dict) or set(value) - {"query", "refresh"}:
        raise ValueError("Expected query and optional refresh fields")
    query, refresh = value.get("query"), value.get("refresh", False)
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000 or not isinstance(refresh, bool):
        raise ValueError("Enter a question of 1–1000 characters")
    return query.strip(), refresh


def safe_origin(headers, port):
    hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
    return (headers.get("Host") in hosts and
            headers.get("Origin") in {"http://" + host for host in hosts})


class Demo:
    def __init__(self, values, directory, package, model=None):
        self.values, self.directory, self.package = values, Path(directory).resolve(), package
        self.manifest = json.loads((self.directory / "ownership.json").read_text())
        check(self.manifest["state"] == "marked", "Retained fixture is not ready")
        with connect(values) as connection:
            verify_owner(connection, self.manifest)
            self.server_version = connection.version
        self.model = model or LiveModel(values)
        self.wheel = verify_wheel(package)
        self.process_id, self.token = secrets.token_hex(8), secrets.token_hex(24)
        self.lock = threading.Lock()
        self.events = self.directory / "browser-events.jsonl"
        self.redactions = private_values(values) + [v for k, v in values.items()
            if v and any(word in k for word in ("API_KEY", "PASSWORD", "COMPARTMENT", "MODEL_ID"))]

    def event(self, kind, **details):
        row = sanitize({"type": kind, "at": time.time(), "process_id": self.process_id, **details}, self.redactions)
        with self.events.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
        self.events.chmod(0o600)

    def counts(self):
        events = [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []
        return {"tavily": sum(x["type"] == "tavily_request" for x in events),
                "model": sum(x["type"] == "oci_request" for x in events),
                "queries": sum(x["type"] == "query_result" for x in events)}

    def snapshot(self, query_hash=None):
        with connect(self.values) as connection:
            verify_owner(connection, self.manifest)
            with connection.cursor() as cursor:
                sql = "SELECT * FROM " + self.manifest["table"] + " WHERE TENANT_ID=:owner"
                binds = {"owner": self.manifest["run_id"]}
                if query_hash:
                    sql += " AND QUERY_HASH=:query_hash"
                    binds["query_hash"] = query_hash
                cursor.execute(sql + " ORDER BY ID", **binds)
                names = [col[0] for col in cursor.description]
                rows = [dict(zip(names, normalize(row))) for row in cursor.fetchall()]
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        return rows, digest

    def status(self):
        rows, digest = self.snapshot()
        return {"ready": True, "run_id": self.manifest["run_id"], "process_id": self.process_id,
                "revision": self.package["browser_revision"], "sdk_version": self.wheel["version"],
                "wheel_sha256": self.wheel["wheel_sha256"], "model": self.model.name,
                "database_version": self.server_version, "vector_search_mode": "exact",
                "cache_rows": len(rows), "row_digest": digest, "counts": self.counts(),
                "maximum_web_calls": MAX_WEB_CALLS, "maximum_model_calls": MAX_MODEL_CALLS,
                "csrf_token": self.token}

    def query(self, value):
        from tavily import TavilyHybridClient
        from tavily.databases.oracledb import OracleDB
        import requests
        query, refresh = query_options(value)
        counts = self.counts()
        check(counts["model"] < MAX_MODEL_CALLS, "This retained run reached its live-model request limit")
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        before, before_digest = self.snapshot(query_hash)
        request_id = secrets.token_hex(8)
        self.event("query_start", request_id=request_id, query=query, refresh=refresh)
        owner = self

        class Session(requests.Session):
            calls = 0
            def send(self, request, **kwargs):
                url = urlsplit(request.url)
                check(url.scheme == "https" and url.hostname == "api.tavily.com" and url.path == "/search",
                      "Unexpected Tavily request endpoint")
                check(self.calls == 0 and owner.counts()["tavily"] < MAX_WEB_CALLS, "Live web request budget exceeded")
                self.calls += 1
                owner.event("tavily_request", request_id=request_id, method=request.method, path=url.path)
                kwargs["timeout"] = (10, 45)
                return super().send(request, **kwargs)

        started = time.monotonic()
        calls = {"embeddings": 0, "rerank": 0}
        session, client = Session(), None
        database = None
        try:
            database = OracleDB(table_name=self.manifest["table"], user=self.values["ORACLE_USER"],
                password=self.values["ORACLE_PASSWORD"], dsn=self.values["ORACLE_DSN"],
                connection_kwargs={"tcp_connect_timeout": 10, **options(self.values)},
                retrieval_mode="freshness_cache", cache_ttl_seconds=86400, cache_score_threshold=0,
                metadata_filters={"TENANT_ID": self.manifest["run_id"], "QUERY_HASH": query_hash},
                enable_json_payload=True, enable_provenance_metadata=True, enable_memory_metadata=True,
                upsert_key="source_url", oracle_max_persisted_foreign=2, vector_search_mode="exact")
            database.connection.call_timeout = 15000
            client = TavilyHybridClient(self.values["TAVILY_API_KEY"], database=database, session=session)
            embed, rank = client.embedding_function, client.ranking_function
            def embedding(*args, **kwargs):
                calls["embeddings"] += 1
                return embed(*args, **kwargs)
            def ranking(*args, **kwargs):
                calls["rerank"] += 1
                return rank(*args, **kwargs)
            client.embedding_function, client.ranking_function = embedding, ranking
            results = client.search(query, max_results=2, max_local=0 if refresh else 2,
                                    max_foreign=2, save_foreign=True, search_depth="basic",
                                    include_raw_content=False, timeout=45)
        finally:
            if client is not None:
                client.close()
            elif database is not None:
                database.close()
            session.close()
        check(results, "No retrieval results were returned")
        rows, after_digest = self.snapshot(query_hash)
        check(rows, "Retrieved web results were not independently visible in Oracle")
        stored = {row["CONTENT"]: row for row in rows}
        sources = [{"content": row["content"], "score": float(row["score"]), "origin": row["origin"],
                    "url": stored.get(row["content"], {}).get("SOURCE_URL"),
                    "title": stored.get(row["content"], {}).get("SOURCE_TITLE")}
                   for row in results]
        route = "oracle_cache" if session.calls == 0 else "tavily_web"
        if route == "oracle_cache":
            check(before_digest == after_digest and all(r["origin"] == "local" for r in results)
                  and calls["rerank"] == 0, "Warm-cache invariants failed")
        self.event("oci_request", request_id=request_id, model=self.model.name)
        generated = self.model.generate(query, sources)
        result = {"request_id": request_id, "query": query, "answer": generated["text"], "sources": sources,
            "trace": {"route": route, "tavily_calls": session.calls, "model_calls": 1, **calls,
                "rows_before": len(before), "rows_after": len(rows),
                "rows_unchanged": before_digest == after_digest, "row_digest": after_digest,
                "retrieval_mode": "freshness_cache", "vector_search_mode": "exact",
                "process_id": self.process_id, "run_id": self.manifest["run_id"],
                "model": self.model.name, "model_ms": generated["elapsed_ms"],
                "elapsed_ms": round((time.monotonic() - started) * 1000)}}
        write_json(self.directory / ("query-" + request_id + ".json"), result)
        self.event("query_result", request_id=request_id, **result["trace"])
        return result


def serve(demo, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data, content_type="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.headers.get("Host") not in {"127.0.0.1:" + str(port), "localhost:" + str(port)}:
                return self.respond(403, {"error": "Invalid host"})
            if self.path == "/":
                return self.respond(200, (HERE / "browser_ui.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/status":
                try:
                    return self.respond(200, demo.status())
                except Exception:
                    return self.respond(503, {"error": "Oracle fixture is unavailable or ownership verification failed"})
            if self.path == "/favicon.ico":
                return self.respond(204, b"")
            return self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if (not safe_origin(self.headers, port) or self.headers.get("X-Demo-Token") != demo.token):
                return self.respond(403, {"error": "Request origin or session token is invalid"})
            if self.path != "/api/query":
                return self.respond(404, {"error": "Not found"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.respond(415, {"error": "Expected application/json"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self.respond(413, {"error": "Request body exceeds the demo limit"})
                value = json.loads(self.rfile.read(length))
                query_options(value)
            except (ValueError, TypeError, UnicodeDecodeError):
                return self.respond(400, {"error": "Enter a valid question of 1–1000 characters"})
            if not demo.lock.acquire(blocking=False):
                return self.respond(409, {"error": "A live request is already running"})
            try:
                return self.respond(200, demo.query(value))
            except Exception as error:
                demo.event("query_error", error_type=type(error).__name__)
                return self.respond(502, {"error": "Live request failed. See the sanitized run evidence; no fallback answer was generated.",
                                          "error_type": type(error).__name__})
            finally:
                demo.lock.release()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    demo.event("server_start", port=port, wheel_sha256=demo.wheel["wheel_sha256"])
    print(json.dumps({"ready": True, "url": "http://127.0.0.1:" + str(port),
                      "run_id": demo.manifest["run_id"], "process_id": demo.process_id,
                      "model": demo.model.name}), flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        demo.event("server_stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "prepare", "serve", "audit"])
    parser.add_argument("--env", required=True)
    parser.add_argument("--package-run", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--port", type=int, default=4111)
    args = parser.parse_args()
    os.umask(0o077)
    values = read_values(args.env)
    package = load_package(args.package_run)
    package["browser_revision"] = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=HERE).decode().strip()
    verify_wheel(package)
    model = LiveModel(values)
    if args.command == "preflight":
        print(json.dumps(model.preflight()), flush=True)
        return
    directory = Path(args.run).resolve()
    if args.command == "prepare":
        check(not directory.exists(), "Use a fresh run directory; prepare never replaces a retained fixture")
        ready = model.preflight()
        before = preflight(values)
        directory.mkdir(parents=True, mode=0o700)
        evidence = type("Run", (), {"directory": directory})()
        manifest = provision(values, evidence, before, definition=FULL.format(dimensions=1024) + ", QUERY_HASH VARCHAR2(64)")
        verify_schema_preserved(before, preflight(values))
        files = ["browser_live.py", "browser_model.py", "browser_ui.html"]
        write_json(directory / "browser-run.json", {"run_id": manifest["run_id"],
            "revision": package["browser_revision"], "source_sha256": package["source"]["sha256"],
            "wheel_sha256": package["wheel_sha256"], "model_preflight": ready,
            "database_version": before["database_version"], "demo_files": {p: sha256(HERE / p) for p in files}})
        print(json.dumps({"prepared": True, "run_id": manifest["run_id"], "model_preflight": ready["status"]}), flush=True)
        return
    metadata = json.loads((directory / "browser-run.json").read_text())
    check(metadata["wheel_sha256"] == package["wheel_sha256"], "Retained run belongs to another SDK wheel")
    check(all(sha256(HERE / p) == digest for p, digest in metadata["demo_files"].items()), "Demo files changed after preparation")
    demo = Demo(values, directory, package, model)
    if args.command == "audit":
        result = demo.status()
        result.pop("csrf_token")
        print(json.dumps(result), flush=True)
    else:
        check(1024 <= args.port <= 65535, "Use an unprivileged port")
        serve(demo, args.port)


if __name__ == "__main__":
    main()
