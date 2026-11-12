"""Bounded larger-data stress with owned connection and process resource samples."""
from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
import threading
import time

from worker_support import Cases, Harness, check, connection
from relay import ConnectionProxy
from catalog import SOAK


def run(config):
    cases = Cases()
    h = Harness(config, cases)

    def soak():
        workers, rows_per_owner, duration, cap = 4, 256, 60, 250
        owner = h.scope
        for number in range(workers):
            tenant = owner+":"+str(number)
            db = h.db(scope=tenant, upsert_key="source_url")
            try:
                db.persist([h.doc("ALPHA owner "+tenant+" record "+str(i), tenant_id=tenant, source_url="key-"+str(i))
                            for i in range(rows_per_owner)])
            finally:
                db.close()
        lock, stop = threading.Lock(), threading.Event()
        counts = {"open": 0, "peak": 0, "opened": 0, "closed": 0}
        def resources():
            rss = int(Path("/proc/self/statm").read_text().split()[1])*os.sysconf("SC_PAGE_SIZE")
            return {"fds": len(list(Path("/proc/self/fd").iterdir())), "rss_bytes": rss}
        baseline, samples = resources(), []
        start = time.monotonic()
        def sample():
            while not stop.wait(.5):
                with lock:
                    opened = counts["open"]
                samples.append({"seconds": time.monotonic()-start, "connections": opened, **resources()})
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        def conversation(number):
            tenant = owner+":"+str(number)
            raw = connection()
            with lock:
                counts["open"] += 1
                counts["opened"] += 1
                counts["peak"] = max(counts["peak"], counts["open"])
            def closed():
                with lock:
                    counts["open"] -= 1
                    counts["closed"] += 1
            proxy = ConnectionProxy(raw, close=closed)
            timings = []
            try:
                with h.client(scope=tenant, connection=proxy, upsert_key="source_url") as (client, transport):
                    for turn in range(cap):
                        if time.monotonic()-start >= duration:
                            break
                        began = time.perf_counter()
                        client.database.persist([h.doc("ALPHA owner "+tenant+" turn "+str(turn),
                                                       tenant_id=tenant, source_url="key-"+str(turn%rows_per_owner))])
                        hits = client.search("ALPHA", max_results=5, max_local=5, max_foreign=0)
                        check(len(hits) == 5 and all(tenant in row["content"] for row in hits), "Soak crossed tenants or lost rows")
                        timings.append(time.perf_counter()-began)
                        time.sleep(.24)
                    check(transport.calls == 0, "Soak called Tavily")
            finally:
                proxy.close()
            rows = h.rows(tenant)
            check(len(rows) == rows_per_owner and len({row["SOURCE_URL"] for row in rows}) == rows_per_owner,
                  "Soak row/key count changed")
            check(sum(row["QUERY_COUNT"] for row in rows) == rows_per_owner+len(timings), "Soak lost committed write counters")
            return timings
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                per_worker = list(pool.map(conversation, range(workers)))
        finally:
            stop.set()
            sampler.join(timeout=3)
        elapsed, after = time.monotonic()-start, resources()
        latency = sorted(value for items in per_worker for value in items)
        check(elapsed >= 55 and elapsed < 90 and 0 < len(latency) <= workers*cap, "Soak escaped workload bounds")
        check(counts["open"] == 0 and counts["opened"] == counts["closed"] == workers, "Owned connections leaked")
        check(after["fds"] <= baseline["fds"]+4, "Process file descriptors did not return near baseline")
        late = [item["rss_bytes"] for item in samples if item["seconds"] >= 30]
        check(not late or late[-1]-late[0] <= 32*1024*1024, "Second-half RSS grew more than 32 MiB")
        def percentile(p):
            return latency[min(len(latency)-1, math.ceil(len(latency)*p)-1)]
        return {"workers": workers, "seeded_rows": workers*rows_per_owner, "operations": len(latency),
                "elapsed_seconds": elapsed, "operations_per_second": len(latency)/elapsed,
                "p50_seconds": percentile(.5), "p95_seconds": percentile(.95), "p99_seconds": percentile(.99),
                "connections": counts, "baseline_resources": baseline, "after_resources": after,
                "resource_samples": samples, "limits": {"seconds": duration, "operation_cap": workers*cap},
                "unavailable_observation": "Database-wide V$SESSION counters are not granted; driver-owned closes and process FDs are measured"}

    cases.case("T01", SOAK["T01"], soak)
    return cases.output()
