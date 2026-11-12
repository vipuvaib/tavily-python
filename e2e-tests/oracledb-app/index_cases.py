"""Build actual indexes and inspect Oracle's catalog; no ANN-use claim."""
import math
import random

import oracledb
from worker_support import Cases, Harness, check, connection, expect
from catalog import INDEXES


def run(config):
    cases = Cases()
    h = Harness(config, cases)

    def text():
        with h.client(table="text", enable_native_hybrid_search=True) as (client, _):
            client.database.persist([h.doc("ALPHA searchable"), h.doc("BETA alpha searchable")])
            index = config["tables"]["text"] + "_TXT"
            check(client.database.ensure_text_index(index) is True, "Text index not created")
            check(client.database.ensure_text_index(index) is False, "Text helper not idempotent")
            results = client.search('"ALPHA"!!!', max_foreign=0, max_local=10)
            check(len(results) == 2 and len({row["content"] for row in results}) == 2,
                  "Native hybrid lost or duplicated row identities")
            check(results[0]["score"] > 1.0, "Oracle Text contribution missing")
            with connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT INDEX_TYPE, DOMIDX_STATUS, DOMIDX_OPSTATUS FROM USER_INDEXES WHERE INDEX_NAME=:name",
                               name=index)
                kind, status, operation = cursor.fetchone()
                check(kind == "DOMAIN" and status == operation == "VALID", "Oracle Text catalog invalid")
            return {"index": index, "top_combined_score": results[0]["score"]}

    def fallback():
        # No Text index on this owned table. Oracle raises a real DRG query error.
        with h.client(table="hnsw", enable_native_hybrid_search=True) as (client, _):
            client.database.persist([h.doc("ALPHA fallback")])
            check(client.search("ALPHA", max_foreign=0)[0]["content"] == "ALPHA fallback",
                  "Recognized Oracle Text error did not fall back")
        with h.client(table="hnsw", enable_native_hybrid_search=True,
                      embed=lambda texts, kind: [[1., 0.] for _ in texts]) as (client, _):
            expect(oracledb.Error, lambda: client.search("ALPHA", max_foreign=0))

    def vector(kind):
        table = kind.lower()
        index = config["tables"][table] + "_VEC"
        rng = random.Random(104729)
        documents = []
        for number in range(128):
            vector = [rng.uniform(-1, 1) for _ in range(3)]
            norm = math.sqrt(sum(value * value for value in vector))
            documents.append(h.doc("vector-" + str(number), embeddings=[value / norm for value in vector]))
        with h.client(table=table, vector_index_type=kind, vector_index_partitions=4,
                      vector_index_neighbors=16, vector_index_efconstruction=64) as (client, _):
            client.database.persist(documents)
            check(client.database.ensure_vector_index(index) is True, kind + " index not created")
            check(client.database.ensure_vector_index(index) is False, kind + " helper not idempotent")
            with connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT INDEX_TYPE, INDEX_SUBTYPE, STATUS FROM USER_INDEXES WHERE INDEX_NAME=:name",
                               name=index)
                record = list(cursor.fetchone())
                check(record[0] == "VECTOR" and record[2] == "VALID", kind + " catalog invalid: " + str(record))
                expected = "NEIGHBOR GRAPH" if kind == "HNSW" else "NEIGHBOR PARTITIONS"
                check(expected in (record[1] or "").replace("_", " ") and kind in (record[1] or ""),
                      "Wrong vector index subtype: " + str(record))
            hits = client.search("ALPHA", max_foreign=0, max_results=5)
            check(len(hits) == 5 and all(row["origin"] == "local" for row in hits), "Indexed table not queryable")
        # A fresh adapter observes the same name and persisted index state.
        database = h.db(table=table, vector_index_type=kind)
        try:
            check(database.ensure_vector_index(index) is False, "Fresh adapter attempted duplicate index creation")
        finally:
            database.close()
        return {"index": index, "catalog": record, "seeded_vectors": len(documents),
                "adapter_query": "FETCH FIRST retrieval; Oracle may select ANN; quality is measured separately"}

    for (case_id, name), operation in zip(INDEXES.items(), [text, fallback, lambda: vector("HNSW"), lambda: vector("IVF")]):
        cases.case(case_id, name, operation)
    return cases.output()
