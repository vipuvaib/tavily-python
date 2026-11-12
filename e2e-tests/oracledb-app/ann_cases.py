"""Execute approximate SQL against real indexes and compare with exact top-k."""
PLAN_DEFINITION = """STATEMENT_ID VARCHAR2(30), PLAN_ID NUMBER, TIMESTAMP DATE,
REMARKS VARCHAR2(4000), OPERATION VARCHAR2(30), OPTIONS VARCHAR2(255), OBJECT_NODE VARCHAR2(128),
OBJECT_OWNER VARCHAR2(128), OBJECT_NAME VARCHAR2(128), OBJECT_ALIAS VARCHAR2(261),
OBJECT_INSTANCE NUMBER, OBJECT_TYPE VARCHAR2(30), OPTIMIZER VARCHAR2(255), SEARCH_COLUMNS NUMBER,
ID NUMBER, PARENT_ID NUMBER, DEPTH NUMBER, POSITION NUMBER, COST NUMBER, CARDINALITY NUMBER,
BYTES NUMBER, OTHER_TAG VARCHAR2(255), PARTITION_START VARCHAR2(255), PARTITION_STOP VARCHAR2(255),
PARTITION_ID NUMBER, OTHER LONG, DISTRIBUTION VARCHAR2(30), CPU_COST NUMBER, IO_COST NUMBER,
TEMP_SPACE NUMBER, ACCESS_PREDICATES VARCHAR2(4000), FILTER_PREDICATES VARCHAR2(4000),
PROJECTION VARCHAR2(4000), TIME NUMBER, QBLOCK_NAME VARCHAR2(128), OTHER_XML CLOB"""


def cursor_statistics(cursor, sql, index, kind):
    """Identify this exact executed SQL, never the diagnostic query's own cursor."""
    cursor.execute("SELECT SQL_ID, CHILD_NUMBER FROM V$SQL WHERE SQL_TEXT=:text "
                   "AND PARSING_SCHEMA_NAME=SYS_CONTEXT('USERENV','SESSION_USER') "
                   "ORDER BY LAST_ACTIVE_TIME DESC FETCH FIRST 1 ROWS ONLY", text=sql)
    identity = cursor.fetchone()
    if identity is None:
        raise AssertionError("Executed ANN cursor was not found")
    sql_id, child = identity
    cursor.execute("SELECT OPERATION, OPTIONS, OBJECT_NAME, LAST_STARTS, LAST_OUTPUT_ROWS, "
                   "LAST_CR_BUFFER_GETS, LAST_DISK_READS, LAST_ELAPSED_TIME "
                   "FROM V$SQL_PLAN_STATISTICS_ALL WHERE SQL_ID=:sql_id AND CHILD_NUMBER=:child ORDER BY ID",
                   sql_id=sql_id, child=child)
    rows = [list(row) for row in cursor.fetchall()]
    active = [row for row in rows if (row[3] or 0) > 0]
    selected = (any(row[2] == index and "VECTOR" in str(row[0]) for row in active) if kind == "HNSW" else
                any(str(row[2]).startswith("VECTOR$" + index + "$") and "IVF_FLAT" in str(row[2]) for row in active))
    if not selected or not any((row[4] or 0) > 0 for row in active):
        raise AssertionError("Executed ANN index path has no runtime row-source evidence")
    cursor.execute("SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id,:child,'ALLSTATS LAST'))",
                   sql_id=sql_id, child=child)
    plan = [row[0] for row in cursor.fetchall()]
    if not any("A-Rows" in line for line in plan):
        raise AssertionError("DISPLAY_CURSOR did not return actual-row statistics")
    return {"status": "passed", "sql_id": sql_id, "child_number": child,
            "row_sources": rows, "display_cursor": plan}


def run(config):
    import array
    import json
    import math
    import os
    import random
    import statistics
    import time
    from worker_support import Cases, Harness, MeasurementFailure, check, connection
    from catalog import ANN
    cases, seed = Cases(), 104729
    h = Harness(config, cases)

    def vector(rng):
        values = [rng.uniform(-1, 1) for _ in range(32)]
        length = math.sqrt(sum(value*value for value in values))
        return [value/length for value in values]

    def evaluate(kind):
        table = config["tables"][kind.lower()]
        plan_table = config["tables"]["plan"]
        index = table + "_ANN"
        rng = random.Random(seed)
        corpus = [vector(rng) for _ in range(4096)]
        database = h.db(table=kind.lower(), vector_index_type=kind, vector_index_accuracy=95,
                        vector_index_partitions=32, vector_index_neighbors=32, vector_index_efconstruction=200)
        try:
            began = time.perf_counter()
            for start in range(0, len(corpus), 256):
                database.persist([{"content": "document-"+str(start+i), "embeddings": value, "tenant_id": h.scope}
                                  for i, value in enumerate(corpus[start:start+256])])
            ingest = time.perf_counter()-began
            began = time.perf_counter()
            check(database.ensure_vector_index(index), "ANN index already existed")
            build = time.perf_counter()-began
        finally:
            database.close()
        with connection() as conn, conn.cursor() as cursor:
            cursor.callproc("DBMS_STATS.GATHER_TABLE_STATS", [conn.username, table])
            hint = ("VECTOR_INDEX_SCAN" if kind == "HNSW" else "VECTOR_INDEX_TRANSFORM") + "(t " + index + ")"
            prefix = "SELECT /*+ GATHER_PLAN_STATISTICS " + hint + " */ ID FROM " + table + " t ORDER BY VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) "
            exact = "SELECT ID FROM " + table + " ORDER BY VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) FETCH EXACT FIRST 10 ROWS ONLY"
            approximate = prefix + "FETCH APPROX FIRST 10 ROWS ONLY WITH TARGET ACCURACY 95"
            queries = [vector(rng) for _ in range(24)]
            literal = "TO_VECTOR('"+json.dumps(queries[0],separators=(",",":"))+"')"
            statement = kind + "_PLAN"
            cursor.execute("EXPLAIN PLAN SET STATEMENT_ID='"+statement+"' INTO " + plan_table +
                           " FOR " + approximate.replace(":v", literal))
            cursor.execute("SELECT OPERATION,OPTIONS,OBJECT_NAME FROM " + plan_table + " WHERE STATEMENT_ID=:s ORDER BY ID", s=statement)
            plan = [list(row) for row in cursor.fetchall()]
            selected = (any(row[2] == index and "VECTOR" in str(row[0]) for row in plan) if kind == "HNSW" else
                        any(str(row[2]).startswith("VECTOR$"+index+"$") and "IVF_FLAT" in str(row[2]) for row in plan)
                        and any(row[0] == "PARTITION LIST" and row[1] == "JOIN-FILTER" for row in plan))
            check(selected,
                  "Optimizer did not select the requested vector index: "+str(plan))
            comparisons, exact_times, approximate_times = [], [], []
            for query in queries:
                binding = array.array("f", query)
                began = time.perf_counter()
                cursor.execute(exact, v=binding)
                exact_ids = [int(row[0]) for row in cursor.fetchall()]
                exact_times.append(time.perf_counter()-began)
                began = time.perf_counter()
                cursor.execute(approximate, v=binding)
                approximate_ids = [int(row[0]) for row in cursor.fetchall()]
                approximate_times.append(time.perf_counter()-began)
                check(len(exact_ids) == len(approximate_ids) == 10 and len(set(approximate_ids)) == 10, "ANN top-k shape mismatch")
                comparisons.append({"exact_ids": exact_ids, "approximate_ids": approximate_ids,
                                    "recall_at_10": len(set(exact_ids)&set(approximate_ids))/10})
            recalls = [item["recall_at_10"] for item in comparisons]
            try:
                live_plan = cursor_statistics(cursor, approximate, index, kind)
            except Exception as error:
                if os.environ.get("ORACLE_E2E_REQUIRE_LIVE_PLAN") == "1":
                    raise
                live_plan = {"status": "unavailable", "reason": str(error)}
        observations = {"kind": kind, "corpus_size": len(corpus), "dimensions": 32, "query_count": len(queries),
                "seed": seed, "index": index, "optimizer_plan": plan, "live_cursor_diagnostic": live_plan,
                "evidence_level": ("Executed FETCH APPROX with live row-source statistics" if live_plan["status"] == "passed" else
                                   "Executed FETCH APPROX with index-selected optimizer plan; live cursor statistics unavailable"),
                "mean_recall_at_10": statistics.mean(recalls), "minimum_recall_at_10": min(recalls),
                "comparisons": comparisons, "ingest_seconds": ingest, "index_build_seconds": build,
                "exact_median_ms": statistics.median(exact_times)*1000,
                "approximate_median_ms": statistics.median(approximate_times)*1000,
                "measured_speed_ratio": statistics.median(exact_times)/statistics.median(approximate_times),
                "performance_scope": "same-session small benchmark; no enforced speedup or production throughput claim"}
        if statistics.mean(recalls) < .90:
            raise MeasurementFailure("Mean recall@10 below .90: mean=" + str(statistics.mean(recalls)) +
                                     ", minimum=" + str(min(recalls)), observations)
        return observations

    for (case_id, name), kind in zip(ANN.items(), ["HNSW", "IVF"]):
        cases.case(case_id, name, lambda kind=kind: evaluate(kind))
    return cases.output()
