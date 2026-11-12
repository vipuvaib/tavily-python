"""Read-only probe study of a retained, owned, failing ANN fixture.

This is diagnostic evidence, not a replacement for the ANN acceptance profile.
No index is created, rebuilt, or reconfigured; only the SELECT clause changes.
"""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
import struct
import time
import traceback

from ann_cases import cursor_statistics
from common import (check, connect, fingerprint, harness_fingerprint, preflight,
                    private_values, sanitize, settings, sha256, table_identifier,
                    verify_owner, verify_schema_preserved, write_json)

SEED, HELDOUT_SEED, ORDER_SEED = 104729, 130363, 15485863
CORPUS_SIZE, DIMENSIONS, ORIGINAL_QUERIES, HELDOUT_QUERIES = 4096, 32, 24, 96
THRESHOLD = .90
PROBES = (1, 2, 4, 8, 12, 16, 20, 24, 28, 32)


def vector(rng):
    values = [rng.uniform(-1, 1) for _ in range(DIMENSIONS)]
    length = math.sqrt(sum(value * value for value in values))
    return array.array("f", [value / length for value in values])


def fixture():
    rng = random.Random(SEED)
    corpus = [vector(rng) for _ in range(CORPUS_SIZE)]
    original = [vector(rng) for _ in range(ORIGINAL_QUERIES)]
    heldout_rng = random.Random(HELDOUT_SEED)
    heldout = [vector(heldout_rng) for _ in range(HELDOUT_QUERIES)]
    return corpus, original, heldout


def vector_hash(vectors):
    digest = hashlib.sha256()
    for values in vectors:
        digest.update(struct.pack("<" + "f" * DIMENSIONS, *values))
    return digest.hexdigest()


def query_sql(table, setting, diagnostic=False, tag="IVF_STUDY"):
    table_identifier(table)
    check(setting in ["exact", "accuracy95-unhinted", "accuracy95", "accuracy99", "accuracy100"] +
          ["probes" + str(value) for value in PROBES], "Unsupported query setting")
    check(bool(re.fullmatch(r"[A-Z0-9_]+", tag)), "Unsafe study SQL tag")
    hints = [tag]
    if diagnostic:
        hints.append("GATHER_PLAN_STATISTICS")
    if setting not in {"exact", "accuracy95-unhinted"}:
        hints.append("VECTOR_INDEX_TRANSFORM(t " + table + "_ANN)")
    sql = ("SELECT /*+ " + " ".join(hints) + " */ ID FROM " + table +
           " t ORDER BY VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) FETCH ")
    if setting == "exact":
        return sql + "EXACT FIRST 10 ROWS ONLY"
    suffix = ("PARAMETERS (NEIGHBOR PARTITION PROBES " + setting[6:] + ")"
              if setting.startswith("probes") else setting[8:].split("-")[0])
    return sql + "APPROX FIRST 10 ROWS ONLY WITH TARGET ACCURACY " + suffix


def percentile95(values):
    return sorted(values)[math.ceil(.95 * len(values)) - 1]


def summarize(samples, exact_samples):
    recalls = [sample["recall_at_10"] for sample in samples]
    times = [sample["elapsed_ms"] for sample in samples]
    queries = sorted({sample["query"] for sample in samples})
    unstable = sum(len({tuple(sorted(sample["ids"])) for sample in samples
                        if sample["query"] == query}) > 1 for query in queries)
    return {"mean_recall_at_10": statistics.mean(recalls), "minimum_recall_at_10": min(recalls),
            "quality_threshold": THRESHOLD, "meets_mean_threshold": statistics.mean(recalls) >= THRESHOLD,
            "unique_queries": len(queries), "executions": len(samples),
            "queries_with_varying_result_sets": unstable,
            "median_ms": statistics.median(times), "p95_ms": percentile95(times),
            "speed_ratio_vs_exact_medians": statistics.median(
                sample["elapsed_ms"] for sample in exact_samples) / statistics.median(times)}


def choose_probe(summaries):
    """Select on the original set only, before looking at held-out results."""
    return next(("probes" + str(n) for n in PROBES
                 if summaries["probes" + str(n)]["meets_mean_threshold"]), None)


def verify_original_exact(actual, archived):
    check(len(actual) == ORIGINAL_QUERIES == len(archived), "Original query inventory changed")
    check(all(set(ids) == set(row["exact_ids"]) for ids, row in zip(actual, archived)),
          "Retained exact top-10 differs from the original failure evidence")


def normalize(value):
    if hasattr(value, "read"):
        return value.read()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, array.array):
        return list(value)
    return value


def records(cursor, sql, **bindings):
    cursor.execute(sql, bindings)
    columns = [column[0].lower() for column in cursor.description]
    return [dict(zip(columns, (normalize(value) for value in row))) for row in cursor.fetchall()]


def catalog(cursor, table):
    index = table + "_ANN"
    metadata = records(cursor, "SELECT INDEX_NAME, INDEX_TYPE, INDEX_SUBTYPE, TABLE_NAME, STATUS "
                       "FROM USER_INDEXES WHERE INDEX_NAME=:name", name=index)
    check(len(metadata) == 1 and metadata[0]["table_name"] == table and
          metadata[0]["index_type"] == "VECTOR" and metadata[0]["status"] == "VALID",
          "Retained vector index is missing, invalid, or belongs to another table")
    objects = records(cursor, "SELECT OBJECT_NAME, OBJECT_TYPE, OBJECT_ID FROM USER_OBJECTS "
                      "WHERE OBJECT_NAME IN (:tab,:idx)", tab=table, idx=index)
    auxiliary = records(cursor, "SELECT TABLE_NAME FROM USER_TABLES WHERE TABLE_NAME LIKE :prefix",
                        prefix="VECTOR$" + index + "$%")
    names = [row["table_name"] for row in auxiliary]
    pattern = re.compile(r"VECTOR\$" + re.escape(index) + r"\$\d+_\d+_\d+\$IVF_FLAT_"
                         r"(?:CENTROIDS|CENTROID_PARTITIONS|INCR_JOURNAL|INCR_TRACKING)")
    check(names and all(pattern.fullmatch(name) for name in names), "Unexpected IVF auxiliary identifier")
    centroids = [name for name in names if name.endswith("$IVF_FLAT_CENTROIDS")]
    partitions = [name for name in names if name.endswith("$IVF_FLAT_CENTROID_PARTITIONS")]
    check(len(centroids) == len(partitions) == 1, "Expected exactly one centroid and partition table")
    cursor.execute('SELECT COUNT(*) FROM "' + centroids[0] + '"')
    centroid_count = cursor.fetchone()[0]
    check(centroid_count == 32, "Study requires the original 32-centroid fixture")
    partition_rows = records(cursor, 'SELECT CENTROID_ID,COUNT(*) AS VECTOR_COUNT FROM "' +
                             partitions[0] + '" GROUP BY CENTROID_ID ORDER BY CENTROID_ID')
    check(sum(row["vector_count"] for row in partition_rows) == CORPUS_SIZE,
          "IVF auxiliary vector count differs from base corpus")
    return {"index": metadata[0], "base_objects": objects, "auxiliary_tables": names,
            "centroid_count": centroid_count, "vectors_by_centroid": partition_rows}


def verify_corpus(cursor, table, expected):
    rows = records(cursor, "SELECT ID,CONTENT,EMBEDDINGS FROM " + table + " ORDER BY ID")
    check(len(rows) == CORPUS_SIZE, "Retained corpus size changed")
    by_document, ids = {}, set()
    for row in rows:
        content = row["content"]
        check(bool(re.fullmatch(r"document-\d+", content)), "Unexpected retained document")
        number = int(content[9:])
        check(number not in by_document and row["id"] not in ids, "Duplicate retained document or ID")
        by_document[number] = row["embeddings"]
        ids.add(row["id"])
    check(set(by_document) == set(range(CORPUS_SIZE)), "Retained corpus document inventory changed")
    actual_hash = vector_hash([by_document[n] for n in range(CORPUS_SIZE)])
    check(actual_hash == vector_hash(expected), "Retained FLOAT32 corpus differs from the seeded fixture")
    return actual_hash


def measure(cursor, table, queries, settings_list, repeats, tag):
    sqls = {setting: query_sql(table, setting, tag=tag) for setting in settings_list}
    samples = {setting: [] for setting in settings_list}
    expected = []
    exact_sql = query_sql(table, "exact", tag=tag)
    for query in queries:
        cursor.execute(exact_sql, v=query)
        ids = [int(row[0]) for row in cursor.fetchall()]
        check(len(set(ids)) == 10, "Exact top-k shape changed")
        expected.append(ids)
    # One warm-up of every prepared SQL on two queries; excluded from timings.
    for sql in sqls.values():
        for query in queries[:2]:
            cursor.execute(sql, v=query)
            cursor.fetchall()
    order = random.Random(ORDER_SEED)
    for repeat in range(repeats):
        indices = list(range(len(queries)))
        order.shuffle(indices)
        for number in indices:
            shuffled = list(settings_list)
            order.shuffle(shuffled)
            for setting in shuffled:
                began = time.perf_counter()
                cursor.execute(sqls[setting], v=queries[number])
                ids = [int(row[0]) for row in cursor.fetchall()]
                elapsed = (time.perf_counter() - began) * 1000
                check(len(ids) == len(set(ids)) == 10, "Top-k shape mismatch: " + setting)
                if setting == "exact":
                    check(set(ids) == set(expected[number]), "Exact result changed during measurement")
                samples[setting].append({"repeat": repeat, "query": number, "ids": ids,
                                         "recall_at_10": len(set(ids) & set(expected[number])) / 10,
                                         "elapsed_ms": elapsed})
        print(tag + " repetition " + str(repeat + 1) + "/" + str(repeats), flush=True)
    summaries = {setting: summarize(rows, samples["exact"]) for setting, rows in samples.items()}
    return {"exact_ids": expected, "summaries": summaries, "samples": samples,
            "query_sha256": vector_hash(queries)}


def live_diagnostics(cursor, table, query, names, tag, required):
    result = {}
    for name in names:
        sql = query_sql(table, name, diagnostic=True, tag=tag)
        cursor.execute(sql, v=query)
        ids = [int(row[0]) for row in cursor.fetchall()]
        try:
            stats = cursor_statistics(cursor, sql, table + "_ANN", "IVF")
        except Exception as error:
            if required:
                raise
            # An unavailable grant is a known limitation on the original endpoint;
            # other failures must not silently lose evidence on diagnostic targets.
            check("ORA-00942" in str(error) or "ORA-01031" in str(error),
                  "Unexpected runtime diagnostic failure: " + str(error))
            stats = {"status": "unavailable", "reason": str(error)}
        result[name] = {"sql": sql, "ids": ids, **stats}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--retained-run", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--repeats", default=3, type=int, choices=range(3, 11))
    parser.add_argument("--require-live", action="store_true")
    args = parser.parse_args()
    directory = args.artifact_dir.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    values = settings(args.env)
    secrets = private_values(values)
    retained = args.retained_run.resolve()
    inputs = [retained / "report.json", retained / "ownership-ivf.json"]
    input_hashes = {str(path): sha256(path) for path in inputs}
    archived = json.loads(inputs[0].read_text())
    manifest = json.loads(inputs[1].read_text())
    report = {"profile": "retained-ivf-probe-study", "status": "running", "started_at": time.time(),
              "acceptance_status": "unchanged; this study cannot satisfy or replace N02/B04",
              "source_report": str(inputs[0]), "input_sha256": input_hashes,
              "source_before": fingerprint(), "harness_revision": harness_fingerprint(),
              "repeats": args.repeats, "warmup_queries_per_setting": 2,
              "seeds": {"fixture": SEED, "heldout": HELDOUT_SEED, "ordering": ORDER_SEED},
              "method": "Warm same-session SELECT execute+fetch; shuffled query and setting order each repetition. "
                        "Latency queries omit GATHER_PLAN_STATISTICS; separate diagnostic executions include it. "
                        "Repetitions are not independent queries. Candidate selected on original 24 only, "
                        "then frozen before evaluating the 96 held-out queries. No DML or DDL.",
              "performance_scope": "4096 synthetic FLOAT32 32D vectors on one retained build per server; "
                                   "shared-host latency, not a production throughput or capacity benchmark"}
    error = None
    try:
        failed = next(item for item in archived["cases"] if item["id"] == "N02")
        observations = failed["observations"]
        check(failed["status"] == "failed" and observations["kind"] == "IVF", "Expected retained IVF failure")
        check((observations["seed"], observations["corpus_size"], observations["dimensions"],
               observations["query_count"], observations["index"]) ==
              (SEED, CORPUS_SIZE, DIMENSIONS, ORIGINAL_QUERIES, manifest["table"] + "_ANN"),
              "Retained benchmark configuration differs from this study")
        check(archived["source_sha256"] == report["source_before"]["sha256"], "SDK source changed since retained run")
        report.update(original_recall=observations["mean_recall_at_10"],
                      original_wheel_sha256=archived["wheel_sha256"])
        corpus, original, heldout = fixture()
        before = preflight(values)
        report["preflight"] = before
        names = ["exact", "accuracy95", "accuracy95-unhinted", "accuracy99", "accuracy100"] + [
            "probes" + str(n) for n in PROBES]
        tag = "IVF_STUDY_" + hashlib.sha256(str(directory).encode()).hexdigest()[:12].upper()
        with connect(values) as conn, conn.cursor() as cursor:
            verify_owner(conn, manifest)
            table = manifest["table"]
            report["catalog_before"] = catalog(cursor, table)
            report["corpus_sha256"] = verify_corpus(cursor, table, corpus)
            original_result = measure(cursor, table, original, names, args.repeats, tag + "_ORIGINAL")
            verify_original_exact(original_result["exact_ids"], observations["comparisons"])
            baseline_samples = [sample for sample in original_result["samples"]["accuracy95"]
                                if sample["repeat"] == 0]
            report["original_accuracy95_result_sets_reproduced"] = all(
                set(sample["ids"]) == set(observations["comparisons"][sample["query"]]["approximate_ids"])
                for sample in baseline_samples)
            # Write the decision before any held-out measurement, retaining full provenance.
            selected = choose_probe(original_result["summaries"])
            report["original_queries"] = original_result
            report["selected_on_original_only"] = selected
            write_json(directory / "selection.json", sanitize({"selected": selected,
                       "selection_rule": "smallest predeclared tested probe count with original mean recall >=0.90",
                       "summaries": original_result["summaries"], "selected_at": time.time()}, secrets))
            write_json(directory / "report.json", sanitize(report, secrets))
            heldout_result = measure(cursor, table, heldout, names, args.repeats, tag + "_HELDOUT")
            report["heldout_queries"] = heldout_result
            report["selected_heldout_meets_threshold"] = bool(selected and
                heldout_result["summaries"][selected]["meets_mean_threshold"])
            report["live_diagnostics"] = live_diagnostics(cursor, table, original[-1],
                [name for name in names if name != "exact"], tag + "_DIAGNOSTIC", args.require_live)
            verify_owner(conn, manifest)
            report["catalog_after"] = catalog(cursor, table)
            check(report["catalog_before"] == report["catalog_after"], "Retained IVF catalog changed during study")
            check(report["corpus_sha256"] == verify_corpus(cursor, table, corpus), "Corpus changed during study")
        after = preflight(values)
        verify_schema_preserved(before, after)
        report["schema_preserved"] = True
        report["source_after"] = fingerprint()
        check(report["source_after"] == report["source_before"], "SDK source changed during study")
        check(report["harness_revision"] == harness_fingerprint(), "Harness changed during study")
        check(all(sha256(path) == digest for path, digest in input_hashes.items()), "Original evidence changed")
        report["status"] = "completed"
    except Exception:
        error = traceback.format_exc()
        report.update(status="failed", error=error)
    report["finished_at"] = time.time()
    write_json(directory / "report.json", sanitize(report, secrets))
    print("Study " + report["status"] + ": " + str(directory / "report.json"), flush=True)
    if error:
        print(sanitize(error, secrets), flush=True)
    else:
        print(json.dumps({"selected_on_original_only": report["selected_on_original_only"],
                          "selected_heldout_meets_threshold": report["selected_heldout_meets_threshold"],
                          "original": report["original_queries"]["summaries"],
                          "heldout": report["heldout_queries"]["summaries"]}, indent=2), flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
