"""Real competing writers, deliberate duplicate windows and cleanup/write races."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
import time

import oracledb
from worker_support import Cases, Harness, check, connection
from relay import ConnectionProxy
from catalog import CONTENTION


def run(config):
    cases = Cases()
    h = Harness(config, cases)

    def existing_key():
        owner = h.scope
        db = h.db(upsert_key="source_url")
        db.persist([h.doc(source_url="shared")])
        db.close()
        sibling = owner+":other"
        db = h.db(scope=sibling, upsert_key="source_url")
        db.persist([h.doc(tenant_id=sibling, source_url="shared")])
        db.close()
        barrier = threading.Barrier(4)
        def writer(number):
            db = h.db(scope=owner, upsert_key="source_url")
            try:
                barrier.wait(timeout=10)
                for turn in range(8):
                    db.persist([h.doc("ALPHA writer " + str(number)+":"+str(turn), tenant_id=owner, source_url="shared")])
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(writer, range(4)))
        rows = h.rows(owner)
        check(len(rows) == 1 and rows[0]["QUERY_COUNT"] == 33, "Competing existing-key updates lost a committed write")
        check(h.rows(sibling)[0]["QUERY_COUNT"] == 1, "Competing writes crossed tenants")
        return {"writers": 4, "committed_updates": 32, "final_write_count": 33, "last_content": rows[0]["CONTENT"]}

    def absent_key():
        owner, barrier = h.scope, threading.Barrier(4)
        def writer(number):
            with connection() as raw:
                def gate(inner):
                    barrier.wait(timeout=12)
                    return inner.commit()
                db = h.db(scope=owner, connection=ConnectionProxy(raw, commit=gate), upsert_key="source_url")
                db.persist([h.doc("ALPHA absent writer "+str(number), tenant_id=owner, source_url="same-new-key")])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(writer, range(4)))
        rows = h.rows(owner)
        check(len(rows) == 4 and len({row["SOURCE_URL"] for row in rows}) == 1,
              "Expected four concurrent inserts without a database uniqueness constraint")
        return {"writers": 4, "duplicate_rows": len(rows),
                "guarantee": "MERGE does not enforce uniqueness for competing absent keys without a constraint"}

    def semantic_race():
        owner, barrier = h.scope, threading.Barrier(4)
        def writer(number):
            with connection() as raw:
                proxy = ConnectionProxy(raw, executemany=lambda: barrier.wait(timeout=12))
                db = h.db(scope=owner, connection=proxy, dedup_similarity_threshold=.99)
                db.persist([h.doc("ALPHA duplicate writer "+str(number), tenant_id=owner)])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(writer, range(4)))
        rows = h.rows(owner)
        check(len(rows) == 4, "Expected per-call deduplication to allow concurrent duplicate candidates")
        return {"writers": 4, "duplicate_rows": len(rows), "guarantee": "Semantic deduplication has no cross-call uniqueness lock"}

    def constrained_key():
        owner, barrier = h.scope, threading.Barrier(4)
        def writer(number):
            db = h.db(scope=owner, table="unique", upsert_key="source_url")
            try:
                barrier.wait(timeout=10)
                try:
                    db.persist([h.doc("ALPHA constrained "+str(number), tenant_id=owner, source_url="shared")])
                    return "committed"
                except oracledb.DatabaseError as error:
                    check("ORA-00001" in str(error), "Unexpected constrained-writer error: "+str(error))
                    return "unique-conflict"
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(writer, range(4)))
        check(len(h.rows(owner, table="unique")) == 1, "Database unique key allowed duplicate rows")
        for _ in range(outcomes.count("unique-conflict")):
            db = h.db(scope=owner, table="unique", upsert_key="source_url")
            try:
                db.persist([h.doc("ALPHA explicit retry", tenant_id=owner, source_url="shared")])
            finally:
                db.close()
        check(h.rows(owner, table="unique")[0]["QUERY_COUNT"] == 4, "Explicit conflict recovery lost committed writes")
        return {"outcomes": outcomes, "rows": 1, "final_write_count": 4,
                "guarantee": "Schema uniqueness prevents duplicate keys; callers handle any ORA-00001"}

    def cleanup_race():
        outcomes = []
        for first in ("cleanup", "persist"):
            owner = h.scope+":"+first
            old = datetime.now(timezone.utc)-timedelta(days=2)
            db = h.db(scope=owner, upsert_key="source_url")
            db.persist([h.doc("ALPHA old", tenant_id=owner, source_url="race", expires_at=old)])
            db.close()
            sibling = owner+":other"
            db = h.db(scope=sibling)
            db.persist([h.doc("ALPHA protected", tenant_id=sibling, expires_at=old)])
            db.close()
            entered, release = threading.Event(), threading.Event()
            def gate(inner):
                entered.set()
                check(release.wait(12), "Race gate was not released")
                return inner.commit()
            with connection() as raw, ThreadPoolExecutor(max_workers=2) as pool:
                gated = h.db(scope=owner, connection=ConnectionProxy(raw, commit=gate), upsert_key="source_url")
                other = h.db(scope=owner, upsert_key="source_url")
                try:
                    document = h.doc("ALPHA fresh", tenant_id=owner, source_url="race")
                    a = pool.submit(gated.cleanup_cache if first == "cleanup" else gated.persist,
                                    *([] if first == "cleanup" else [[document]]))
                    check(entered.wait(8), "First race operation did not reach commit")
                    b = pool.submit(other.persist if first == "cleanup" else other.cleanup_cache,
                                    *([[document]] if first == "cleanup" else []))
                    time.sleep(.2)
                    release.set()
                    a_result, b_result = a.result(timeout=15), b.result(timeout=15)
                finally:
                    release.set()
                    gated.close()
                    other.close()
            rows = h.rows(owner)
            check(len(rows) == 1 and rows[0]["CONTENT"] == "ALPHA fresh", "Acknowledged fresh write lost in cleanup race")
            check(len(h.rows(sibling)) == 1, "Cleanup race crossed tenant boundary")
            outcomes.append({"first_lock": first, "cleanup_deleted": a_result if first=="cleanup" else b_result,
                             "final_rows": len(rows), "write_count": rows[0]["QUERY_COUNT"]})
        return {"orderings": outcomes, "isolation": "Oracle read committed with ordinary row locks; no application lock"}

    for (case_id, name), operation in zip(CONTENTION.items(), [existing_key, absent_key, semantic_race, constrained_key, cleanup_race]):
        cases.case(case_id, name, operation)
    return cases.output()
