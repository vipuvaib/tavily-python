"""Abrupt death, real network failure, concurrency, and bounded repeated work."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

import oracledb
from tavily.databases.oracledb import OracleDB
from worker_support import Cases, Harness, check, connection, expect
from catalog import RELIABILITY


def run(config):
    cases = Cases()
    h = Harness(config, cases)

    def abrupt():
        script = Path(__file__).with_name("abrupt-child.py")
        script.write_text("import sys,json,os,signal\nfrom pathlib import Path\n"
            "sys.path.insert(0,str(Path(__file__).parent))\n"
            "from worker_support import Cases,Harness\n"
            "config=json.load(sys.stdin)\ncases=Cases();cases.current='R01'\nh=Harness(config,cases)\n"
            "db=h.db();db.persist([h.doc('ALPHA committed before abrupt death',source_url='committed')])\n"
            "os.kill(os.getpid(),signal.SIGKILL)\n")
        result = subprocess.run([config["worker_python"], "-I", str(script)], input=json.dumps(config),
                                capture_output=True, text=True, timeout=30)
        check(result.returncode == -signal.SIGKILL, "Child did not die abruptly: " + result.stderr)
        with h.client() as (client, transport):
            rows = client.search("ALPHA", max_foreign=0)
            check(rows[0]["content"] == "ALPHA committed before abrupt death" and transport.calls == 0,
                  "Committed data did not survive SIGKILL")

    def listener():
        # Reserve a TCP port without listening, so nothing else can claim the target.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
            start = time.monotonic()
            expect(oracledb.Error, lambda: OracleDB(user=os.environ["ORACLE_USER"],
                password=os.environ["ORACLE_PASSWORD"], dsn="127.0.0.1:" + str(port) + "/unavailable",
                table_name=config["tables"]["full"], connection_kwargs={"tcp_connect_timeout": 1, "retry_count": 0}))
            elapsed = time.monotonic() - start
            check(elapsed < 10, "Unavailable connection exceeded bound")
        database = h.db()
        try:
            database.persist([h.doc("ALPHA recovered")])
            check(len(h.rows()) == 1, "Fresh adapter did not recover")
        finally:
            database.close()
        return {"failure_seconds": elapsed, "recovery": "new adapter; automatic same-adapter reconnect is not implemented"}

    def concurrent(repeat=False):
        base = h.scope
        iterations = 12 if repeat else 6
        latency = []
        start = time.monotonic()
        def conversation(number):
            owner = base + ":" + str(number)
            timings = []
            with h.client(scope=owner, upsert_key="source_url", retrieval_mode="freshness_cache") as (client, transport):
                for turn in range(iterations):
                    began = time.monotonic()
                    client.database.persist([h.doc("ALPHA owner " + owner + " turn " + str(turn),
                                                   tenant_id=owner, source_url="turn-" + str(turn))])
                    results = client.search("ALPHA", max_results=iterations, max_local=iterations, max_foreign=0)
                    check(len(results) == turn + 1 and all(owner in row["content"] for row in results),
                          "Concurrent retrieval crossed owners or lost rows")
                    timings.append(time.monotonic() - began)
                    if repeat:
                        time.sleep(.05)
                check(transport.calls == 0, "Local workload called Tavily")
            rows = h.rows(owner)
            check(len(rows) == iterations and len({row["SOURCE_URL"] for row in rows}) == iterations,
                  "Concurrent row/key count mismatch")
            return timings
        with ThreadPoolExecutor(max_workers=4) as pool:
            for timings in pool.map(conversation, range(4)):
                latency.extend(timings)
        latency.sort()
        def percentile(fraction):
            return latency[min(len(latency)-1, math.ceil(len(latency) * fraction)-1)]
        return {"workers": 4, "operations": len(latency), "elapsed_seconds": time.monotonic()-start,
                "p50_seconds": percentile(.5), "p95_seconds": percentile(.95), "p99_seconds": percentile(.99),
                "scope": "bounded consistency workload, not production capacity or concurrent dedup uniqueness"}

    for (case_id, name), operation in zip(RELIABILITY.items(), [abrupt, listener, concurrent, lambda: concurrent(True)]):
        cases.case(case_id, name, operation)
    return cases.output()
