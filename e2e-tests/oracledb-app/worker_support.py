"""Installed-consumer utilities; no checkout imports or replacement database driver."""
import array
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
import urllib.request

import oracledb
import requests
import tavily
from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from oracle_config import initialize, options as connection_options

initialize()


def check(value, message):
    if not value:
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
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return [normalize(item) for item in value]


def expect(error_type, operation):
    try:
        operation()
    except error_type as error:
        return str(error)
    raise AssertionError("Expected " + str(error_type))


class MeasurementFailure(AssertionError):
    def __init__(self, message, observations):
        super().__init__(message)
        self.observations = observations


class Cases:
    def __init__(self):
        self.cases = []
        self.current = ""

    def case(self, case_id, name, operation):
        self.current = case_id
        started = time.monotonic()
        entry = {"id": case_id, "name": name, "status": "passed"}
        try:
            entry.update(operation() or {})
        except Exception as error:
            text = traceback.format_exc()
            for key in ("ORACLE_PASSWORD", "ORACLE_DSN", "TAVILY_API_KEY", "CO_API_KEY"):
                if os.environ.get(key):
                    text = text.replace(os.environ[key], "<redacted>")
            entry.update(status="failed", error=text)
            if hasattr(error, "observations"):
                entry["observations"] = normalize(error.observations)
        entry["elapsed_seconds"] = round(time.monotonic() - started, 4)
        self.cases.append(entry)

    def output(self, **details):
        path = Path(tavily.__file__).resolve()
        check(path.is_relative_to(Path(sys.prefix).resolve()), "SDK resolved outside installed consumer")
        return {"cases": self.cases, "pid": os.getpid(), "package_path": str(path),
                "package_version": importlib.metadata.version("tavily-python"),
                "python": sys.version.split()[0], "oracle_driver_version": oracledb.__version__,
                "oracle_mode": "thin" if oracledb.is_thin_mode() else "thick", **details}


def connection():
    result = oracledb.connect(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
                             dsn=os.environ["ORACLE_DSN"], tcp_connect_timeout=5, **connection_options())
    result.call_timeout = 15000
    return result


def control(endpoint, **changes):
    request = urllib.request.Request(endpoint + "/control", data=json.dumps(changes).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


class RedirectSession(requests.Session):
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint
        self.trust_env = False
        self.calls = 0

    def send(self, request, **kwargs):
        from urllib.parse import urlsplit
        url = urlsplit(request.url)
        check(url.netloc == "api.tavily.com", "Unexpected SDK host")
        request.url = self.endpoint + url.path + ("?" + url.query if url.query else "")
        self.calls += 1
        return super().send(request, **kwargs)


class Harness:
    def __init__(self, config, cases):
        self.config, self.cases = config, cases
        self.embeddings, self.ranks = [], []
        for table in config["tables"].values():
            check(re.fullmatch(r"TVE_[A-F0-9]{20}", table), "Unsafe table name")

    @property
    def scope(self):
        return self.config["run_id"] + ":" + self.cases.current

    def embed(self, texts, input_type):
        self.embeddings.append((input_type, len(texts)))
        return [([0., 1., 0.] if "BETA" in text else [.8, .6, 0.] if "NEAR" in text
                 else [0., 0., 1.] if "GAMMA" in text else [1., 0., 0.]) for text in texts]

    def rank(self, query, documents, count):
        self.ranks.append(list(documents))
        return sorted(documents, key=lambda row: row["score"], reverse=True)[:count]

    def db(self, scope=None, table="full", **options):
        defaults = {"metadata_filters": {"TENANT_ID": self.scope if scope is None else scope}}
        defaults.update(options)
        supplied = defaults.pop("connection", None)
        credentials = ({"connection": supplied} if supplied is not None else
                       {"user": os.environ["ORACLE_USER"], "password": os.environ["ORACLE_PASSWORD"],
                        "dsn": os.environ["ORACLE_DSN"], "connection_kwargs": {"tcp_connect_timeout": 5, **connection_options()}})
        result = OracleDB(table_name=self.config["tables"][table], **credentials, **defaults)
        result.connection.call_timeout = 15000
        return result

    @contextmanager
    def client(self, scope=None, table="full", embed=None, rank=None, **options):
        database = self.db(scope, table, **options)
        session = RedirectSession(self.config["endpoint"])
        client = None
        try:
            client = TavilyHybridClient(api_key="fixture-key", database=database,
                                        embedding_function=embed or self.embed,
                                        ranking_function=rank or self.rank, session=session)
            yield client, session
        finally:
            if client is not None:
                client.close()
            else:
                database.close()
            session.close()

    def doc(self, content="ALPHA local", **fields):
        now = datetime.now(timezone.utc)
        result = {"content": content, "embeddings": self.embed([content], "search_document")[0],
                  "tenant_id": self.scope, "added_at": now.replace(tzinfo=None),
                  "provider_name": "tavily", "inserted_from": "tavily", "memory_scope": "cache_only",
                  "expires_at": now + timedelta(hours=1), "query_count": 1}
        result.update(fields)
        return result

    def rows(self, scope=None, table="full", filters=None):
        filters = filters if filters is not None else {"TENANT_ID": self.scope if scope is None else scope}
        clauses, binds = [], {}
        for index, (key, value) in enumerate(filters.items()):
            check(re.fullmatch(r"[A-Z_]+", key), "Unsafe filter")
            if value is None:
                clauses.append(key + " IS NULL")
            else:
                clauses.append(key + "=:b" + str(index))
                binds["b" + str(index)] = value
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT * FROM " + self.config["tables"][table] +
                           (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY ID", binds)
            names = [item[0] for item in cursor.description]
            return [dict(zip(names, map(normalize, row))) for row in cursor.fetchall()]

    def update(self, assignments, scope=None, table="full"):
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE " + self.config["tables"][table] + " SET " + assignments +
                           " WHERE TENANT_ID=:owner", owner=self.scope if scope is None else scope)
            conn.commit()
