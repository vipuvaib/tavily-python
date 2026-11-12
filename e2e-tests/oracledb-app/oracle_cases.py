"""Real Oracle acceptance cases, executed with the installed SDK."""
from datetime import datetime, timedelta, timezone
import hashlib
import time

import oracledb
import requests
from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from worker_support import Harness, Cases, check, expect, connection, control, RedirectSession
from catalog import ORACLE


def run(config):
    cases = Cases()
    h = Harness(config, cases)
    past = datetime.now(timezone.utc) - timedelta(days=2)

    def hybrid():
        control(config["endpoint"])
        with h.client() as (client, transport):
            client.database.persist([h.doc("ALPHA local"), h.doc("BETA local")])
            before = len(h.ranks)
            result = client.search("ALPHA", max_results=2, max_local=2, max_foreign=2)
            check(len(h.ranks) == before + 1 and len(h.ranks[-1]) == 4, "Combined candidates not reranked")
            check(len(result) == 2 and result[0]["content"] == "ALPHA local", "Ranking/limit mismatch")
            check(transport.calls == 1 and len(h.rows()) == 2, "Unexpected web call or write")

    def routes():
        for mode in ("hybrid_search", "freshness_cache", "cache_then_memory"):
            owner = h.scope + ":" + mode
            control(config["endpoint"])
            with h.client(scope=owner, retrieval_mode=mode) as (client, transport):
                client.database.persist([h.doc("ALPHA local", tenant_id=owner)])
                before = len(h.ranks)
                local = client.search("ALPHA", max_foreign=0, max_results=1)
                check(local[0]["origin"] == "local" and transport.calls == 0, "Local-only route failed")
                check(len(h.ranks) == before + (mode == "hybrid_search"), "Local ranking decision mismatch")
                before = len(h.ranks)
                foreign = client.search("ALPHA", max_local=0, max_foreign=1, max_results=1)
                check(foreign[0]["origin"] == "foreign" and len(h.ranks) == before, "Foreign fallback reranked")
                check(client.search("ALPHA", max_local=0, max_foreign=0) == [], "Disabled routes not empty")
            with h.client(scope=owner + ":empty", retrieval_mode=mode) as (client, transport):
                check(client.search("ALPHA", max_foreign=0) == [] and transport.calls == 0,
                      "Empty local-only search called provider")

    def freshness():
        with h.client(retrieval_mode="freshness_cache", cache_score_threshold=.79) as (client, transport):
            client.database.persist([
                h.doc("ALPHA explicit fresh", added_at=past.replace(tzinfo=None)),
                h.doc("NEAR timestamp fresh", expires_at=None),
                h.doc("ALPHA expired", expires_at=past), h.doc("BETA below threshold")])
            rows = client.search("ALPHA", max_local=10, max_foreign=0)
            check({r["content"] for r in rows} == {"ALPHA explicit fresh", "NEAR timestamp fresh"},
                  "Expiry/timestamp precedence mismatch")
            client.database.cache_score_threshold = 1.0
            check([r["content"] for r in client.search("ALPHA", max_foreign=0)] == ["ALPHA explicit fresh"],
                  "Inclusive score threshold failed")
            check(transport.calls == 0, "Fresh cache called provider")

    def memory():
        control(config["endpoint"])
        with h.client(retrieval_mode="cache_then_memory", cache_score_threshold=.9,
                      memory_score_threshold=.7) as (client, transport):
            client.database.persist([h.doc("ALPHA expired cache", expires_at=past),
                h.doc("NEAR durable", memory_scope="cache_plus_memory", expires_at=past)])
            hit = client.search("ALPHA", max_foreign=2)
            check(hit[0]["content"] == "NEAR durable" and transport.calls == 0, "Memory fallback failed")
            client.database.persist([h.doc("ALPHA fresh cache")])
            check(client.search("ALPHA")[0]["content"] == "ALPHA fresh cache", "Cache did not precede memory")
            h.update("EXPIRES_AT=SYSTIMESTAMP-INTERVAL '1' DAY")
            client.database.memory_score_threshold = .99
            check(client.search("ALPHA")[0]["origin"] == "foreign" and transport.calls == 1,
                  "Both-tier miss did not call provider")
        with h.client(scope=h.scope + ":new", retrieval_mode="cache_then_memory") as (client, _):
            client.search("ALPHA", save_foreign=True)
            rows = h.rows(scope=h.scope + ":new")
            check(all(row["MEMORY_SCOPE"] == "cache_plus_memory" for row in rows), "Memory default not durable")

    def persistence_filter():
        control(config["endpoint"])
        with h.client(oracle_persist_score_threshold=.9, oracle_max_persisted_foreign=1) as (client, _):
            before = len(h.embeddings)
            result = client.search("ALPHA", max_local=0, max_foreign=2, save_foreign=True)
            check(len(result) == 2 and len(h.rows()) == 1, "Persistence filter changed returned candidates")
            check(h.embeddings[before:] == [("search_query", 1), ("search_document", 1)],
                  "Filtered documents were embedded")

    def callbacks():
        control(config["endpoint"])
        with h.client() as (client, _):
            client.search("ALPHA", max_local=0, save_foreign=False)
            check(h.rows() == [], "save_foreign=False wrote rows")
            captured = []
            def callback(result):
                captured.append(result)
                return {"content": result["content"], "embeddings": result["embeddings"],
                        "tenant_id": h.scope, "label": "callback"} if "ALPHA" in result["content"] else None
            client.search("ALPHA", max_local=0, save_foreign=callback)
            row = h.rows()[0]
            check(len(captured) == 2 and row["LABEL"] == "callback" and row["PROVIDER_NAME"] is None,
                  "Callback did not bypass automatic metadata")
            before = h.rows()
            client.search("ALPHA", max_local=0, save_foreign=lambda result: None)
            check(h.rows() == before, "Fully filtered callback wrote data")

    def upsert_url():
        database = h.db(upsert_key="source_url")
        try:
            database.persist([h.doc(source_url="same", source_title="keep", label="first")])
            database.persist([h.doc("ALPHA replacement", source_url="same", label="second")])
            rows = h.rows()
            check(len(rows) == 1 and rows[0]["CONTENT"] == "ALPHA replacement" and
                  rows[0]["SOURCE_TITLE"] == "keep" and rows[0]["QUERY_COUNT"] == 2, "URL upsert metadata failed")
        finally:
            database.close()

    def upsert_hash():
        control(config["endpoint"])
        with h.client(upsert_key="content_hash", enable_memory_metadata=True) as (client, _):
            client.search("ALPHA", max_local=0, save_foreign=True)
            client.search("ALPHA", max_local=0, save_foreign=True)
            rows = h.rows()
            check(len(rows) == 2 and all(row["CONTENT_HASH"] == hashlib.sha256(row["CONTENT"].encode()).hexdigest()
                                        for row in rows), "Hash upsert failed")
            check(all(row["QUERY_COUNT"] == 2 for row in rows), "Hash refresh did not count writes")
            client.database.persist([h.doc("GAMMA unkeyed"), h.doc("GAMMA keyed", content_hash="separate")])
            check(len(h.rows()) == 4, "Mixed unkeyed insertion failed")

    def dedup():
        database = h.db(dedup_similarity_threshold=.99)
        try:
            database.persist([h.doc("ALPHA first"), h.doc("ALPHA batch duplicate"), h.doc("BETA independent")])
            check(len(h.rows()) == 2, "Within-batch dedup failed")
            database.persist([h.doc("ALPHA stored duplicate"), h.doc("GAMMA independent")])
            check(len(h.rows()) == 3, "Stored dedup failed")
            database.dedup_similarity_threshold = None
            database.persist([h.doc("ALPHA no suppression")])
            check(len(h.rows()) == 4, "Disabling dedup failed")
        finally:
            database.close()

    def dedup_origins():
        for label, fields, expected in [
            ("unmarked", {"provider_name": None, "inserted_from": None}, 2),
            ("expired", {"expires_at": past}, 2),
            ("durable", {"memory_scope": "cache_plus_memory", "expires_at": past}, 1)]:
            owner = h.scope + ":" + label
            database = h.db(scope=owner, dedup_similarity_threshold=.99)
            try:
                database.persist([h.doc("ALPHA stored", tenant_id=owner, **fields)])
                database.persist([h.doc("ALPHA next", tenant_id=owner)])
                check(len(h.rows(owner)) == expected, "Dedup eligibility failed: " + label)
            finally:
                database.close()
        control(config["endpoint"])
        owner = h.scope + ":auto"
        with h.client(scope=owner, dedup_similarity_threshold=.99) as (client, _):
            client.search("ALPHA", max_local=0, save_foreign=True)
            client.search("ALPHA", max_local=0, save_foreign=True)
            rows = h.rows(owner)
            check(len(rows) == 2 and all(row["PROVIDER_NAME"] == row["INSERTED_FROM"] == "tavily" for row in rows),
                  "Automatic origin markers did not permit round-trip dedup")

    def allowed_scope():
        a, b = h.scope + ":a", h.scope + ":b"
        database = h.db(metadata_filters={"TENANT_ID": [a, b]}, upsert_key="source_url", dedup_similarity_threshold=.99)
        try:
            database.persist([h.doc(tenant_id=a, source_url="same"), h.doc(tenant_id=b, source_url="same")])
            database.persist([h.doc("ALPHA refreshed", tenant_id=a, source_url="same")])
            check(h.rows(a)[0]["QUERY_COUNT"] == 2 and h.rows(b)[0]["QUERY_COUNT"] == 1,
                  "Allowed-list upsert crossed owners")
            database.oracle_upsert_key = None
            database.persist([h.doc("ALPHA duplicate", tenant_id=a), h.doc("BETA distinct", tenant_id=b)])
            check(len(h.rows(a)) == 1 and len(h.rows(b)) == 2, "Allowed-list dedup crossed exact owners")
        finally:
            database.close()

    def ownership():
        database = h.db(metadata_filters={"TENANT_ID": h.scope, "REGION": "east"})
        try:
            database.persist([{"content": "ALPHA filled", "embeddings": [1, 0, 0]}])
            check(h.rows()[0]["REGION"] == "east", "Scalar ownership not filled")
            before = h.rows()
            expect(ValueError, lambda: database.persist([h.doc(region="east"), h.doc(tenant_id="wrong", region="east")]))
            check(h.rows() == before, "Invalid batch partially persisted")
        finally:
            database.close()
        for allowed in ([h.scope], (h.scope,), {h.scope}, []):
            database = h.db(metadata_filters={"TENANT_ID": allowed})
            try:
                expect(ValueError, lambda: database.persist([{"content": "ALPHA", "embeddings": [1, 0, 0]}]))
            finally:
                database.close()
        database = h.db(metadata_filters={"TENANT_ID": None, "REGION": h.scope})
        try:
            database.persist([{"content": "ALPHA null owner", "embeddings": [1, 0, 0]}])
            with h.client(metadata_filters={"TENANT_ID": None, "REGION": h.scope}) as (client, _):
                check(client.search("ALPHA", max_foreign=0)[0]["content"] == "ALPHA null owner", "Null-owner read failed")
        finally:
            database.close()

    def cleanup():
        other = h.scope + ":other"
        database = h.db()
        try:
            database.persist([h.doc("ALPHA expired", expires_at=past), h.doc("BETA fresh"),
                h.doc("GAMMA durable", expires_at=past, memory_scope="cache_plus_memory"),
                h.doc("ALPHA legacy cache", memory_scope=None, expires_at=past, retrieval_mode="freshness_cache"),
                h.doc("ALPHA unrelated legacy", memory_scope=None, expires_at=past, provider_name="other"),
                h.doc("ALPHA timestamp expired", expires_at=None, added_at=past.replace(tzinfo=None))])
            sibling = h.db(scope=other)
            try:
                sibling.persist([h.doc("ALPHA sibling", tenant_id=other, expires_at=past)])
            finally:
                sibling.close()
            check(database.cleanup_cache() == 3, "Cleanup deleted wrong number of rows")
            check({row["CONTENT"] for row in h.rows()} == {"BETA fresh", "GAMMA durable", "ALPHA unrelated legacy"},
                  "Cleanup removed protected rows")
            check(len(h.rows(other)) == 1, "Cleanup crossed tenant boundary")
        finally:
            database.close()

    def automatic_cleanup():
        with h.client(auto_cleanup_cache=True, cache_cleanup_interval_seconds=3600) as (client, transport):
            client.database.persist([h.doc(expires_at=past)])
            client.search("ALPHA", max_local=0, max_foreign=0)
            check(h.rows() == [], "Cleanup did not precede max_local=0 route")
            client.database.persist([h.doc(expires_at=past)])
            client.search("ALPHA", max_local=0, max_foreign=0)
            check(len(h.rows()) == 1, "Cleanup was not throttled")
            # Database latency must not expire the window before the throttling assertion.
            client.database.cache_cleanup_interval_seconds = .01
            time.sleep(.02)
            client.search("ALPHA", max_local=0, max_foreign=0)
            check(h.rows() == [] and transport.calls == 0, "Cleanup did not resume after interval")

    def minimal():
        control(config["endpoint"])
        with h.client(table="minimal", metadata_filters={}, content_field="BODY", embeddings_field="VEC",
                      cache_timestamp_field="CREATED_AT", retrieval_mode="freshness_cache") as (client, transport):
            first = client.search("ALPHA", save_foreign=True)
            second = client.search("ALPHA")
            check(len(first) == len(second) == 2 and all(row["origin"] == "local" for row in second)
                  and transport.calls == 1, "Configured minimal cache failed")

    def partial():
        database = h.db(table="lifecycle", metadata_filters={}, retrieval_mode="freshness_cache")
        try:
            database.persist([{"content": "ALPHA expired", "embeddings": [1, 0, 0],
                               "memory_scope": "cache_only", "expires_at": past},
                              {"content": "BETA durable", "embeddings": [0, 1, 0],
                               "memory_scope": "cache_plus_memory", "expires_at": past}])
            check(database.cleanup_cache() == 1, "Expiry-only lifecycle cleanup failed")
        finally:
            database.close()
        database = h.db(table="minimal", metadata_filters={}, content_field="BODY", embeddings_field="VEC",
                        cache_timestamp_field="CREATED_AT")
        try:
            check(database.cleanup_cache() == 0, "Legacy rows without lifecycle/provenance were deleted")
        finally:
            database.close()

    def rollback():
        for key in (None, "source_url"):
            owner = h.scope + (":insert" if key is None else ":merge")
            database = h.db(scope=owner, upsert_key=key)
            try:
                documents = [h.doc("BETA accepted", tenant_id=owner),
                             h.doc("ALPHA rejected", tenant_id=owner, label="REJECT", source_url="key")]
                expect(oracledb.DatabaseError, lambda: database.persist(documents))
                check(h.rows(owner) == [], "Failed DML left committed partial rows")
                database.persist([h.doc("GAMMA recovered", tenant_id=owner)])
                check(len(h.rows(owner)) == 1, "Connection unusable after rollback")
            finally:
                database.close()

    def invalid_schema():
        database = h.db()
        try:
            expect((oracledb.DatabaseError, oracledb.Error), lambda: database.persist([h.doc(embeddings=[1, 0])]))
            check(h.rows() == [], "Dimension mismatch wrote data")
            expect(ValueError, lambda: database.persist([h.doc(unknown_column="bad")]))
            expect(ValueError, lambda: database.persist([h.doc(tenant_id="outside")]))
        finally:
            database.close()
        database = h.db(table="wrong", metadata_filters={})
        try:
            expect(ValueError, lambda: database.persist([{"content": "ALPHA", "embeddings": [1, 0, 0]}]))
        finally:
            database.close()

    def external_failures():
        def fail(*args):
            raise RuntimeError("provider failed")
        control(config["endpoint"])
        with h.client(embed=fail) as (client, transport):
            expect(RuntimeError, lambda: client.search("ALPHA", save_foreign=True))
            check(transport.calls == 0 and h.rows() == [], "Query embedding failure had side effects")
        with h.client(rank=fail) as (client, transport):
            expect(RuntimeError, lambda: client.search("ALPHA", save_foreign=True))
            check(transport.calls == 1 and h.rows() == [], "Ranking failure persisted rows")
        def fail_documents(texts, kind):
            return fail() if kind == "search_document" else h.embed(texts, kind)
        with h.client(embed=fail_documents) as (client, _):
            expect(RuntimeError, lambda: client.search("ALPHA", save_foreign=True))
            check(h.rows() == [], "Document embedding failure persisted rows")
        control(config["endpoint"], status=500)
        with h.client() as (client, _):
            expect(requests.HTTPError, lambda: client.search("ALPHA", save_foreign=True))
            check(h.rows() == [], "HTTP failure persisted rows")
        control(config["endpoint"])

    def ownership_close():
        with connection() as external:
            database = h.db(connection=external)
            client = TavilyHybridClient("fixture-key", database=database, embedding_function=h.embed, ranking_function=h.rank)
            client.close()
            external.ping()
            database = h.db(connection=external)
            expect(TypeError, lambda: TavilyHybridClient("fixture-key", database=database, embedding_function=123))
            external.ping()
        database = h.db()
        managed = database.connection
        client = TavilyHybridClient("fixture-key", database=database, embedding_function=h.embed, ranking_function=h.rank)
        client.close()
        expect(oracledb.Error, managed.ping)
        database = h.db()
        managed = database.connection
        expect(TypeError, lambda: TavilyHybridClient("fixture-key", database=database, embedding_function=123))
        expect(oracledb.Error, managed.ping)

    operations = [hybrid, routes, freshness, memory, persistence_filter, callbacks, upsert_url,
                  upsert_hash, dedup, dedup_origins, allowed_scope, ownership, cleanup, automatic_cleanup,
                  minimal, partial, rollback, invalid_schema, external_failures, ownership_close]
    for (case_id, name), operation in zip(ORACLE.items(), operations):
        cases.case(case_id, name, operation)
    return cases.output()
