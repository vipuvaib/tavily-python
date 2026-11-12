"""Check installed Tavily retrieval against exact results on a retained IVF fixture."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import time
import traceback

from common import (check, connect, fingerprint, harness_fingerprint, preflight, private_values,
                    sanitize, settings, sha256, verify_owner, verify_schema_preserved, write_json)
from ivf_study import fixture, vector_hash, verify_corpus
from oracle_config import options
from run import clean_environment, command, load_package, verify_consumer


WORKER = r'''
import array, importlib.metadata, json, os, pathlib, sys
import oracledb, requests, statistics, tavily
from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB

config = json.load(sys.stdin)
assert pathlib.Path(tavily.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
class NoNetwork(requests.Session):
    def send(self, request, **kwargs):
        raise AssertionError("Unexpected external request during local-only SDK search")

conn = oracledb.connect(user=os.environ['ORACLE_USER'], password=os.environ['ORACLE_PASSWORD'],
                       dsn=os.environ['ORACLE_DSN'], tcp_connect_timeout=5, **config['connect_kwargs'])
conn.call_timeout = 15000
query_vector = None
def embed(texts, input_type):
    return [query_vector for text in texts]

database = OracleDB(connection=conn, table_name=config['table'], vector_index_type='IVF',
                    **config['sdk_options'])
session = NoNetwork()
client = TavilyHybridClient(api_key='local-fixture-key', database=database,
    embedding_function=embed, ranking_function=lambda query, docs, count:
        sorted(docs, key=lambda row: row['score'], reverse=True)[:count], session=session)
comparisons = []
try:
    with conn.cursor() as cursor:
        cursor.execute('SELECT ID,CONTENT FROM ' + config['table'])
        content_ids = {}
        for identity, content in cursor.fetchall():
            content = content.read() if hasattr(content, 'read') else content
            assert content not in content_ids
            content_ids[content] = int(identity)
        exact_sql = ('SELECT ID,1-VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) FROM ' + config['table'] +
                     ' ORDER BY VECTOR_DISTANCE(EMBEDDINGS,:v,COSINE) FETCH EXACT FIRST 10 ROWS ONLY')
        for number, query in enumerate(config['queries']):
            query_vector = query
            cursor.execute(exact_sql, v=array.array('f', query))
            exact = {int(identity): float(score) for identity, score in cursor.fetchall()}
            assert set(exact) == set(config['expected_ids'][number]), 'Archived exact baseline changed'
            hits = client.search('retained-vector-check-' + str(number), max_foreign=0,
                                 max_local=10, max_results=10)
            ids = [content_ids[hit['content']] for hit in hits]
            assert len(ids) == len(set(ids)) == 10, 'SDK top-k shape mismatch'
            assert all(hit['origin'] == 'local' for hit in hits)
            assert all(abs(hit['score'] - exact[identity]) < 1e-6 for hit, identity in zip(hits, ids)
                       if identity in exact), 'SDK score differs from exact distance'
            comparisons.append({'query': number, 'exact_ids': list(exact), 'sdk_ids': ids,
                                'recall_at_10': len(set(ids) & set(exact))/10})
        try:
            cursor.execute("SELECT SQL_ID,CHILD_NUMBER,SQL_FULLTEXT FROM V$SQL "
                "WHERE SQL_TEXT LIKE :pattern AND SQL_TEXT LIKE '%AS origin%' "
                "AND SQL_TEXT NOT LIKE '%V$SQL%' "
                "AND PARSING_SCHEMA_NAME=SYS_CONTEXT('USERENV','SESSION_USER') "
                "ORDER BY LAST_ACTIVE_TIME DESC FETCH FIRST 1 ROWS ONLY", pattern='%FROM '+config['table']+'%')
            found = cursor.fetchone()
            assert found is not None, 'Executed SDK SQL not found'
            sql_id, child, sql = found
            sql = sql.read() if hasattr(sql,'read') else sql
            cursor.execute('SELECT OPERATION,OPTIONS,OBJECT_NAME FROM V$SQL_PLAN '
                           'WHERE SQL_ID=:sid AND CHILD_NUMBER=:child ORDER BY ID', sid=sql_id,child=child)
            plan = {'status':'available', 'sql_id':sql_id,'child_number':child,'sql':sql,
                    'cached_optimizer_plan':[list(row) for row in cursor.fetchall()],
                    'scope':'Cached plan for the public SDK query; not actual row-source statistics'}
        except oracledb.DatabaseError as error:
            assert 'ORA-00942' in str(error) or 'ORA-01031' in str(error)
            plan = {'status':'unavailable','reason':str(error)}
finally:
    client.close()
    conn.close()
print(json.dumps({'comparisons': comparisons, 'queries': len(comparisons),
    'package_path': tavily.__file__, 'package_version': importlib.metadata.version('tavily-python'),
    'driver_version': oracledb.__version__, 'oracle_mode': 'thin' if oracledb.is_thin_mode() else 'thick',
    'external_requests': 0, 'mean_recall_at_10': statistics.mean(row['recall_at_10'] for row in comparisons),
    'minimum_recall_at_10': min(row['recall_at_10'] for row in comparisons),
    'all_queries_match_exact': all(row['recall_at_10']==1.0 for row in comparisons), 'sdk_query_plan':plan}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--package-run", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--compare-new-package", action="store_true",
                        help="Explicitly compare a new SDK source/wheel against immutable old fixture evidence")
    parser.add_argument("--approx-probes", type=int,
                        help="Test opt-in ANN with this IVF probe count against the unchanged mean recall gate")
    args = parser.parse_args()
    directory = args.artifact_dir.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    values = settings(args.env)
    secrets = private_values(values)
    evidence = SimpleNamespace(directory=directory, secrets=secrets)
    report = {"profile": "ivf-sdk-retrieval-check", "status": "running", "started_at": time.time(),
              "harness_revision": harness_fingerprint(), "source_before": fingerprint(),
              "acceptance_status": "Does not replace the failed approximate N02/B04 gates"}
    error = None
    try:
        study_path = args.study.resolve() / "report.json"
        study_hash = sha256(study_path)
        study = json.loads(study_path.read_text())
        check(study["status"] == "completed", "Probe study must complete before SDK comparison")
        package = load_package(args.package_run)
        if args.compare_new_package:
            baseline = json.loads(Path(study["source_report"]).read_text())
            old_package = json.loads((Path(baseline["package_run"]) / "report.json").read_text())
            check(old_package["source"] == study["source_before"] and
                  sha256(old_package["wheel"]) == old_package["wheel_sha256"] == study["original_wheel_sha256"],
                  "Original baseline package provenance changed")
            report["cross_source_comparison"] = {"explicitly_requested": True,
                "baseline_source_sha256": study["source_before"]["sha256"],
                "baseline_wheel_sha256": study["original_wheel_sha256"],
                "current_source_sha256": package["source"]["sha256"],
                "current_wheel_sha256": package["wheel_sha256"]}
        else:
            check(study["source_before"] == package["source"] and
                  study["original_wheel_sha256"] == package["wheel_sha256"], "Study/package provenance differs")
        report["installed_wheel_check"] = verify_consumer(package, evidence)
        report.update(study=str(study_path), study_sha256=study_hash, wheel_sha256=package["wheel_sha256"])
        retained = Path(study["source_report"]).parent
        manifest_path = retained / "ownership-ivf.json"
        manifest = json.loads(manifest_path.read_text())
        check(all(sha256(path) == digest for path, digest in study["input_sha256"].items()),
              "Retained inputs changed after the probe study")
        before = preflight(values)
        with connect(values) as conn:
            verify_owner(conn, manifest)
        corpus, original, heldout = fixture()
        check(vector_hash(original) == study["original_queries"]["query_sha256"] and
              vector_hash(heldout) == study["heldout_queries"]["query_sha256"], "Study queries differ")
        with connect(values) as conn, conn.cursor() as cursor:
            report["corpus_sha256"] = verify_corpus(cursor, manifest["table"], corpus)
        sdk_options = ({"vector_search_mode": "approx", "vector_search_probes": args.approx_probes}
                       if args.approx_probes is not None else {})
        config = {"table": manifest["table"], "connect_kwargs": options(values), "sdk_options": sdk_options,
                  "queries": [list(q) for q in original + heldout],
                  "expected_ids": study["original_queries"]["exact_ids"] + study["heldout_queries"]["exact_ids"]}
        output = command([package["consumer_python"], "-I", "-c", WORKER],
                         Path(package["consumer_workspace"]), evidence, "sdk-check", clean_environment(values),
                         json.dumps(config), timeout=180)
        report["consumer"] = json.loads(output)
        report["sdk_options"] = sdk_options
        check(report["consumer"]["queries"] == len(original + heldout) == 120, "SDK query inventory incomplete")
        verify_schema_preserved(before, preflight(values))
        with connect(values) as conn:
            verify_owner(conn, manifest)
            with conn.cursor() as cursor:
                check(report["corpus_sha256"] == verify_corpus(cursor, manifest["table"], corpus),
                      "Retained corpus changed during the SDK comparison")
        check(sha256(study_path) == study_hash, "Study evidence changed during SDK check")
        report["source_after"] = fingerprint()
        check(report["source_after"] == report["source_before"], "SDK sources changed")
        check(report["harness_revision"] == harness_fingerprint(), "Harness changed during SDK check")
        report.update(schema_preserved=True, preflight=before)
        if args.approx_probes is None:
            check(report["consumer"]["all_queries_match_exact"], "Public SDK retrieval differs from explicit exact top-10")
        else:
            check(report["consumer"]["mean_recall_at_10"] >= .90, "Opt-in ANN mean recall below unchanged .90 gate")
        report.update(status="passed")
    except Exception:
        error = traceback.format_exc()
        report.update(status="failed", error=error)
    report["finished_at"] = time.time()
    write_json(directory / "report.json", sanitize(report, secrets))
    for path in directory.iterdir():
        if path.is_file():
            path.chmod(0o600)
    print(report["status"] + ": " + str(directory / "report.json"), flush=True)
    if error:
        print(sanitize(error, secrets), flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
