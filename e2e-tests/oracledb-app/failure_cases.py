"""Interrupt active Oracle traffic; reconcile independently before caller recovery."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time

import oracledb
from worker_support import Cases, Harness, check, connection, expect
from relay import Relay, ConnectionProxy
from catalog import FAILURES


def via_relay(relay):
    params = oracledb.ConnectParams()
    params.parse_connect_string(os.environ["ORACLE_DSN"])
    check(params.protocol == "tcp", "Fault relay requires plain TCP deployment")
    result = oracledb.connect(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
        host="127.0.0.1", port=relay.port, service_name=params.service_name,
        tcp_connect_timeout=3, retry_count=0, disable_oob=True)
    result.call_timeout = 10000
    return result


def run(config):
    cases = Cases()
    h = Harness(config, cases)
    params = oracledb.ConnectParams()
    params.parse_connect_string(os.environ["ORACLE_DSN"])

    def read_failure():
        db = h.db()
        try:
            db.persist([h.doc("ALPHA unchanged during failed read")])
        finally:
            db.close()
        before = h.rows()
        entered = threading.Event()
        def delayed(cursor, sql, args, kwargs):
            if "VECTOR_DISTANCE" in sql:
                entered.set()
                sql = "WITH FUNCTION tv_delay RETURN NUMBER IS BEGIN DBMS_SESSION.SLEEP(4); RETURN 1; END; " + sql
                sql = sql.replace("ORDER BY", "AND tv_delay()=1 ORDER BY", 1)
            return cursor.execute(sql, *args, **kwargs)
        with Relay(params.host, params.port) as relay:
            raw = via_relay(relay)
            try:
                proxy = ConnectionProxy(raw, execute=delayed)
                with h.client(connection=proxy) as (client, _), ThreadPoolExecutor(max_workers=1) as pool:
                    start = time.monotonic()
                    task = pool.submit(client.search, "ALPHA", max_foreign=0)
                    check(entered.wait(5), "Read did not enter real Oracle cursor")
                    time.sleep(.25)
                    relay.cut()
                    error = expect(oracledb.Error, lambda: task.result(timeout=12))
                    elapsed = time.monotonic() - start
                    check(elapsed < 12 and relay.bytes["client"] and relay.bytes["server"], "Read fault was not bounded actual traffic")
            finally:
                raw.close()
        check(h.rows() == before, "Failed read changed committed rows")
        with h.client() as (client, _):
            check(client.search("ALPHA", max_foreign=0)[0]["content"] == before[0]["CONTENT"], "Caller read recovery failed")
        return {"error": error, "failure_seconds": elapsed, "traffic_bytes": relay.bytes,
                "injection": "Oracle SELECT delayed with a local WITH FUNCTION; its live TCP connection was cut"}

    def write_failure():
        db = h.db(upsert_key="source_url")
        try:
            db.persist([h.doc("ALPHA original", source_url="locked")])
        finally:
            db.close()
        entered = threading.Event()
        def mark(cursor, sql, args, kwargs):
            if "MERGE INTO" in sql:
                entered.set()
            return cursor.execute(sql, *args, **kwargs)
        with connection() as locker, locker.cursor() as cursor:
            cursor.execute("UPDATE " + config["tables"]["full"] + " SET LABEL='locked' WHERE TENANT_ID=:owner", owner=h.scope)
            with Relay(params.host, params.port) as relay:
                raw = via_relay(relay)
                try:
                    db = h.db(connection=ConnectionProxy(raw, execute=mark), upsert_key="source_url")
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        start = time.monotonic()
                        task = pool.submit(db.persist, [h.doc("ALPHA interrupted", source_url="locked")])
                        check(entered.wait(5), "MERGE did not start")
                        time.sleep(.25)
                        check(not task.done(), "MERGE was not blocked on the owned row lock")
                        relay.cut()
                        error = expect(oracledb.Error, lambda: task.result(timeout=12))
                    locker.rollback()
                finally:
                    raw.close()
        rows = h.rows()
        check(len(rows) == 1 and rows[0]["CONTENT"] == "ALPHA original" and rows[0]["QUERY_COUNT"] == 1,
              "Interrupted pre-commit write changed visible committed state")
        db = h.db(upsert_key="source_url")
        try:
            db.persist([h.doc("ALPHA caller recovered", source_url="locked")])
        finally:
            db.close()
        check(h.rows()[0]["QUERY_COUNT"] == 2, "Caller write recovery failed")
        return {"error": error, "failure_seconds": time.monotonic()-start, "committed_before_retry": 1}

    def unknown_commit():
        entered = threading.Event()
        with Relay(params.host, params.port) as relay:
            raw = via_relay(relay)
            def commit(inner):
                relay.hold_responses.set()
                entered.set()
                return inner.commit()
            try:
                db = h.db(connection=ConnectionProxy(raw, commit=commit), upsert_key="source_url")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    task = pool.submit(db.persist, [h.doc("ALPHA committed without acknowledged reply", source_url="uncertain")])
                    check(entered.wait(5), "Commit was not invoked")
                    deadline = time.monotonic()+8
                    visible = []
                    while time.monotonic() < deadline:
                        visible = h.rows()
                        if visible:
                            break
                        time.sleep(.05)
                    check(len(visible) == 1 and not task.done(), "Did not observe a real committed-but-unacknowledged write")
                    relay.cut()
                    error = expect(oracledb.Error, lambda: task.result(timeout=12))
            finally:
                raw.close()
        check(h.rows()[0]["QUERY_COUNT"] == 1, "Unknown commit state was not retained")
        db = h.db(upsert_key="source_url")
        try:
            db.persist([h.doc("ALPHA explicit caller retry", source_url="uncertain")])
        finally:
            db.close()
        rows = h.rows()
        check(len(rows) == 1 and rows[0]["QUERY_COUNT"] == 2, "Explicit retry outcome differs from two committed writes")
        return {"error": error, "committed_visible_before_lost_ack": True, "reply_bytes_withheld": relay.bytes["held_server"],
                "rows_after_retry": len(rows), "write_count_after_retry": rows[0]["QUERY_COUNT"],
                "guarantee": "Caller reconciles independently; retries are not exactly-once"}

    for (case_id, name), operation in zip(FAILURES.items(), [read_failure, write_failure, unknown_commit]):
        cases.case(case_id, name, operation)
    return cases.output()
