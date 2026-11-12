"""Observe only uniquely tagged test sessions through server fixed views."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time

import oracledb
from catalog import PROFILES
from oracle_config import options
from worker_support import Cases, Harness, check, connection
from relay import Relay


def run(config):
    cases = Cases()
    h = Harness(config, cases)

    def lifecycle():
        tag = "TVE:" + config["run_id"]
        workers, ready, release = 4, threading.Barrier(5), threading.Event()
        observer = connection()
        observer.call_timeout = 10000
        snapshots = []

        def sample(label):
            with observer.cursor() as cursor:
                cursor.execute("SELECT SID, SERIAL#, STATUS FROM V$SESSION WHERE CLIENT_IDENTIFIER=:tag", tag=tag)
                sessions = [list(row) for row in cursor.fetchall()]
                cursor.execute("SELECT n.NAME, SUM(s.VALUE) FROM V$SESSTAT s JOIN V$STATNAME n "
                               "ON n.STATISTIC#=s.STATISTIC# JOIN V$SESSION v ON v.SID=s.SID "
                               "WHERE v.CLIENT_IDENTIFIER=:tag AND n.NAME IN "
                               "('opened cursors current','session logical reads','physical reads',"
                               "'session pga memory','session uga memory') GROUP BY n.NAME", tag=tag)
                result = {"label": label, "sessions": sessions, "server_statistics": dict(cursor.fetchall())}
                snapshots.append(result)
                return result

        def wait_empty(label):
            deadline = time.monotonic() + 15
            while True:
                result = sample(label)
                if not result["sessions"]:
                    return
                check(time.monotonic() < deadline, "Server retained tagged sessions after physical close")
                time.sleep(.2)

        def tagged(raw):
            raw.client_identifier = tag
            raw.module = "tavily-e2e"
            raw.action = "session-lifecycle"
            raw.ping()  # Flush the tag to the database before the observer samples.
            return raw

        def workload(number):
            raw = tagged(connection())
            tenant = h.scope + ":" + str(number)
            try:
                with h.client(scope=tenant, connection=raw, upsert_key="source_url") as (client, transport):
                    for turn in range(24):
                        client.database.persist([h.doc("ALPHA session " + str(number), tenant_id=tenant,
                                                       source_url="key-" + str(turn % 6))])
                        check(client.search("ALPHA", max_foreign=0), "Observed workload lost rows")
                    check(transport.calls == 0, "Observed local workload called Tavily")
                    ready.wait(timeout=45)
                    check(release.wait(timeout=30), "Observer did not release workload")
                raw.ping()  # SDK must not close injected connections.
            finally:
                raw.close()
            return {"worker": number, "turns": 24}

        try:
            check(sample("baseline")["sessions"] == [], "Unique test tag already exists")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(workload, number) for number in range(workers)]
                try:
                    ready.wait(timeout=50)
                    active = sample("four-live-clients")
                    check(len(active["sessions"]) == workers, "Observer did not see every live client")
                    check(active["server_statistics"].get("session logical reads", 0) > 0,
                          "Database-side workload counters missing")
                finally:
                    release.set()
                completed = [future.result(timeout=40) for future in futures]
            wait_empty("after-client-close")
            pool = oracledb.create_pool(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
                                       dsn=os.environ["ORACLE_DSN"], min=0, max=2, increment=1,
                                       tcp_connect_timeout=5, **options())
            try:
                for _ in range(3):
                    raw = tagged(pool.acquire())
                    raw.close()
                    check(pool.busy == 0, "Pool release retained a busy checkout")
                pooled = sample("released-idle-pool")
                check(len(pooled["sessions"]) == 1, "Expected one reusable physical pooled session")
            finally:
                pool.close()
            wait_empty("after-pool-close")
            # Cut only this client's private relay, without administrative session kills.
            params = oracledb.ConnectParams()
            params.parse_connect_string(os.environ["ORACLE_DSN"])
            with Relay(params.host, params.port) as relay:
                raw = tagged(oracledb.connect(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
                             dsn="127.0.0.1:" + str(relay.port) + "/" + params.service_name,
                             tcp_connect_timeout=5, **options()))
                try:
                    check(len(sample("before-private-socket-cut")["sessions"]) == 1, "Interrupted session was not observed")
                    relay.cut()
                    try:
                        raw.ping()
                    except oracledb.Error:
                        pass
                    else:
                        raise AssertionError("Cut Oracle connection unexpectedly succeeded")
                finally:
                    try:
                        raw.close()
                    except oracledb.Error:
                        pass
            wait_empty("after-interrupted-client-retirement")
            return {"tag": tag, "workers": completed, "snapshots": snapshots,
                    "scope": "Database-observed uniquely tagged test sessions, not unrelated users or global capacity"}
        finally:
            release.set()
            observer.close()

    cases.case("G01", PROFILES["diagnostics"]["G01"], lifecycle)
    return cases.output()
