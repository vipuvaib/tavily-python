"""Installed-SDK quality checks with real HNSW/IVF indexes and explicit modes."""
import array
from datetime import datetime, timedelta, timezone
import math
import random
import statistics

from catalog import RETRIEVAL
from worker_support import Cases, Harness, MeasurementFailure, check, connection

IVF_TEST_PROBES = 24  # Conformance workload setting, never an SDK default.

def fixture():
    def vector(rng):
        values = [rng.uniform(-1, 1) for _ in range(32)]
        length = math.sqrt(sum(value * value for value in values))
        return array.array("f", [value / length for value in values])
    rng = random.Random(104729)
    corpus = [vector(rng) for _ in range(4096)]
    queries = [vector(rng) for _ in range(24)]
    heldout = random.Random(130363)
    return corpus, queries + [vector(heldout) for _ in range(96)]


def run(config):
    cases = Cases()
    h = Harness(config, cases)
    scope = config["run_id"] + ":vector-quality"
    corpus, queries = fixture()

    def reference(cursor, kind, query, limit=10):
        table = config["tables"][kind.lower()]
        cursor.execute("SELECT CONTENT,1-VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) FROM " + table +
                       " WHERE TENANT_ID=:tenant ORDER BY VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) "
                       "FETCH EXACT FIRST " + str(limit) + " ROWS ONLY", v=query, tenant=scope)
        return {row[0].read() if hasattr(row[0], "read") else row[0]: float(row[1])
                for row in cursor.fetchall()}

    def measure(kind, options, selected=None, exact=False, native=False):
        selected = queries if selected is None else selected
        current = None
        comparisons = []
        def embed(texts, input_type):
            return [list(current) for text in texts]
        with h.client(scope=scope, table=kind.lower(), vector_index_type=kind,
                      enable_native_hybrid_search=native, embed=embed, **options) as (client, session), \
                connection() as conn, conn.cursor() as cursor:
            for number, query in enumerate(selected):
                current = query
                expected = reference(cursor, kind, query)
                hits = client.search("zzznomatchingtermxyz", max_local=10, max_results=10, max_foreign=0)
                contents = [hit["content"] for hit in hits]
                check(len(contents) == len(set(contents)) == 10, "Indexed SDK top-k shape mismatch")
                check(all(hit["origin"] == "local" for hit in hits), "Nonlocal result in local-only search")
                check(all(abs(hit["score"] - expected[hit["content"]]) < 1e-6
                          for hit in hits if hit["content"] in expected), "SDK cosine score mismatch")
                comparisons.append({"query": number, "exact_contents": list(expected),
                                    "sdk_contents": contents,
                                    "recall_at_10": len(set(contents) & set(expected)) / 10})
            check(session.calls == 0, "Local-only quality check called Tavily")
        recalls = [row["recall_at_10"] for row in comparisons]
        result = {"kind": kind, "options": options, "native_hybrid": native,
                  "query_count": len(selected), "mean_recall_at_10": statistics.mean(recalls),
                  "minimum_recall_at_10": min(recalls), "comparisons": comparisons,
                  "original_queries": min(24, len(selected)), "external_requests": 0}
        if (exact and min(recalls) != 1.0) or statistics.mean(recalls) < .90:
            raise MeasurementFailure("Indexed SDK retrieval quality requirement failed", result)
        return result

    def defaults():
        for kind in ["IVF", "HNSW"]:
            database = h.db(scope=scope, table=kind.lower(), vector_index_type=kind,
                            vector_index_accuracy=95, vector_index_partitions=32,
                            vector_index_neighbors=32, vector_index_efconstruction=200)
            try:
                for start in range(0, len(corpus), 256):
                    database.persist([h.doc("document-" + str(start + number), embeddings=list(value),
                                            tenant_id=scope) for number, value in enumerate(corpus[start:start + 256])])
                check(database.ensure_vector_index(config["tables"][kind.lower()] + "_ANN"),
                      "Fresh quality fixture index already existed")
            finally:
                database.close()
            with connection() as conn, conn.cursor() as cursor:
                cursor.callproc("DBMS_STATS.GATHER_TABLE_STATS", [conn.username, config["tables"][kind.lower()]])
        return {"corpus_per_index": 4096, "dimensions": 32, "seed": 104729, "heldout_seed": 130363,
                "results": [measure(kind, {}, exact=True) for kind in ["IVF", "HNSW"]]}

    def tuned():
        results = [measure("IVF", {"vector_search_mode": "approx", "vector_search_probes": IVF_TEST_PROBES}),
                   measure("HNSW", {"vector_search_mode": "approx", "vector_search_efsearch": 500})]
        # Reuse one real connection while changing validated optimizer settings.
        # A low-probe result is a parameter-control check, not a recall acceptance pass.
        controls = []
        query = queries[0]
        with connection() as conn, conn.cursor() as cursor:
            for probes in [1, 16, 1, 16]:
                cursor.execute("SELECT CONTENT,1-VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) AS score,'local' AS origin FROM " +
                               config["tables"]["ivf"] + " WHERE EMBEDDINGS IS NOT NULL AND TENANT_ID=:tenant "
                               "ORDER BY VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) "
                               "FETCH APPROX FIRST 10 ROWS ONLY WITH TARGET ACCURACY PARAMETERS "
                               "(NEIGHBOR PARTITION PROBES " + str(probes) + ")", tenant=scope, v=query)
                expected = [row[0].read() if hasattr(row[0], "read") else row[0] for row in cursor.fetchall()]
                with h.client(scope=scope, table="ivf", connection=conn, vector_index_type="IVF",
                              vector_search_mode="approx", vector_search_probes=probes,
                              embed=lambda texts, kind: [list(query) for text in texts]) as (client, session):
                    hits = client.search("probe bind reuse", max_local=10, max_results=10, max_foreign=0)
                    actual = [hit["content"] for hit in hits]
                    check(set(actual) == set(expected), "Reused SDK optimizer setting differs from matching SQL control")
                    check(session.calls == 0, "Local probe control made a request")
                controls.append({"probes": probes, "sdk_contents": actual, "literal_contents": expected})
        return {"results": results, "same_connection_probe_controls": controls,
                "probe_budgets_returned_same_contents":
                    set(controls[0]["sdk_contents"]) == set(controls[1]["sdk_contents"]),
                "optimizer_scope": "Approximate requests may execute exactly; different budgets need not change results",
                "scope": "Explicit settings on these 32-centroid fixtures; no universal default or speed claim"}

    def percentage():
        return measure("HNSW", {"vector_search_mode": "approx", "vector_search_accuracy": 95})

    def native():
        database = h.db(scope=scope, table="ivf")
        try:
            check(database.ensure_text_index(config["tables"]["ivf"] + "_TXT"), "Text index already existed")
        finally:
            database.close()
        return {"results": [measure("IVF", {"vector_search_mode": "exact"}, queries[:24], exact=True, native=True),
                            measure("IVF", {"vector_search_mode": "approx", "vector_search_probes": IVF_TEST_PROBES},
                                    queries[:24], native=True)],
                "text_query": "A nonmatching term keeps the real Text path while isolating vector candidate quality"}

    def fallback():
        # HNSW table deliberately has no Text index: Oracle raises a real DRG error.
        return {"results": [measure("HNSW", {}, queries[:24], exact=True, native=True),
                            measure("HNSW", {"vector_search_mode": "approx", "vector_search_efsearch": 500},
                                    queries[:24], native=True)]}

    def cache():
        query = queries[0]
        with connection() as conn, conn.cursor() as cursor:
            nearest = reference(cursor, "IVF", query, 1)
        content, score = next(iter(nearest.items()))
        observations = []
        def assert_route(mode, threshold, expected, memory=False):
            options = {"retrieval_mode": mode, "cache_score_threshold": threshold}
            if memory:
                options["memory_score_threshold"] = threshold
            with h.client(scope=scope, table="ivf", vector_index_type="IVF", **options,
                          embed=lambda texts, kind: [list(query) for text in texts]) as (client, session):
                hits = client.search("threshold boundary", max_local=1, max_results=1, max_foreign=0)
                check([hit["content"] for hit in hits] == expected, "Indexed cache/memory threshold mismatch")
                check(session.calls == 0, "Disabled foreign route made a request")
            observations.append({"mode": mode, "threshold": threshold, "contents": expected})
        assert_route("freshness_cache", score, [content])
        assert_route("freshness_cache", score + 1e-8, [])
        past = datetime.now(timezone.utc) - timedelta(days=1)
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE " + config["tables"]["ivf"] +
                           " SET MEMORY_SCOPE='cache_plus_memory',EXPIRES_AT=:past WHERE TENANT_ID=:tenant",
                           past=past, tenant=scope)
            check(cursor.rowcount == 4096, "Memory fixture update count changed")
            conn.commit()
        assert_route("cache_then_memory", score, [content], memory=True)
        assert_route("cache_then_memory", score + 1e-8, [], memory=True)
        return {"nearest_content": content, "exact_score": score, "boundaries": observations}

    def dedup():
        selected = None
        with connection() as conn, conn.cursor() as cursor:
            for query in queries[:24]:
                expected = reference(cursor, "IVF", query, 1)
                exact_content, score = next(iter(expected.items()))
                table = config["tables"]["ivf"]
                cursor.execute("SELECT /*+ VECTOR_INDEX_TRANSFORM(t " + table + "_ANN) */ "
                               "CONTENT,1-VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) FROM " + table + " t "
                               "WHERE TENANT_ID=:tenant ORDER BY VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) "
                               "FETCH APPROX FIRST 1 ROWS ONLY WITH TARGET ACCURACY PARAMETERS "
                               "(NEIGHBOR PARTITION PROBES 1)", v=query, tenant=scope)
                content, approximate_score = cursor.fetchone()
                content = content.read() if hasattr(content, "read") else content
                if content != exact_content and approximate_score < score - 1e-8:
                    selected = query, score, content, float(approximate_score)
                    break
        check(selected is not None, "Fixture did not expose a low-probe nearest-neighbor miss")
        query, score, approximate_content, approximate_score = selected
        def count():
            with connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM " + config["tables"]["ivf"] + " WHERE TENANT_ID=:tenant", tenant=scope)
                return cursor.fetchone()[0]
        before = count()
        for threshold, expected in [(score, before), (score + 1e-8, before + 1)]:
            database = h.db(scope=scope, table="ivf", vector_index_type="IVF", vector_search_mode="approx",
                            vector_search_probes=1, dedup_similarity_threshold=threshold)
            try:
                database.persist([h.doc("dedup-boundary-" + str(threshold), embeddings=list(query),
                                        tenant_id=scope)])
            finally:
                database.close()
            check(count() == expected, "Indexed dedup inclusive threshold or exact lookup failed")
        return {"retrieval_mode": "approx", "retrieval_probes": 1, "dedup_mode": "exact",
                "inclusive_threshold": score, "rows_before": before, "rows_after": count(),
                "low_probe_control_content": approximate_content, "low_probe_control_score": approximate_score}

    for (case_id, name), operation in zip(RETRIEVAL.items(),
                                         [defaults, tuned, percentage, native, fallback, cache, dedup]):
        cases.case(case_id, name, operation)
    return cases.output()
