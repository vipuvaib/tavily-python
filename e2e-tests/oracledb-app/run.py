#!/usr/bin/env python3
"""Package, preflight, smoke, live, and exact-run cleanup profiles."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
import zipfile

from common import (HERE, ROOT, Evidence, check, connect, fingerprint, preflight,
                    redact, settings, sha256, source_files, table_identifier,
                    verify_owner, write_json)
from common import harness_fingerprint
from common import private_values, verify_schema_preserved

QUERY = "Oracle AI Vector Search"


def verify_consumer(report, evidence):
    probe = """
import pathlib, sys, sysconfig, zipfile, tavily, json
root = pathlib.Path(sysconfig.get_path('purelib')).resolve()
assert pathlib.Path(tavily.__file__).resolve().is_relative_to(root)
count = 0
with zipfile.ZipFile(sys.argv[1]) as wheel:
    for name in wheel.namelist():
        if name.startswith('tavily/') and not name.endswith('/'):
            assert (root / name).read_bytes() == wheel.read(name), 'Installed SDK differs: ' + name
            count += 1
assert count > 0
print(json.dumps({'verified_sdk_files': count, 'package_path': tavily.__file__}))
"""
    return json.loads(command([report["consumer_python"], "-I", "-c", probe, report["wheel"]],
                              Path(report["consumer_workspace"]), evidence, "installed-wheel-check",
                              clean_environment()))


def command(args, cwd, evidence, name, env=None, payload=None, timeout=300):
    try:
        result = subprocess.run(args, cwd=cwd, env=env, input=payload, text=True,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(name + " exceeded " + str(timeout) + " seconds") from None
    log = redact(result.stdout + "\n" + result.stderr, evidence.secrets)
    (evidence.directory / (name + ".log")).write_text(log)
    if result.returncode:
        raise RuntimeError(name + " failed; inspect " + str(evidence.directory / (name + ".log")))
    return result.stdout


def clean_environment(values=None):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("TAVILY_") or key in {"PYTHONPATH", "PYTHONHOME", "CO_API_KEY"}:
            env.pop(key)
    env.update(values or {})
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def package(evidence):
    print("Building source distribution and consumer wheel...", flush=True)
    workspace = Path(tempfile.mkdtemp(prefix="tavily-oracle-consumer-"))
    snapshot = workspace / "source"
    snapshot.mkdir()
    original = fingerprint()
    for name in source_files():
        source = ROOT / name
        check(not source.is_symlink(), "Source symlinks are not supported: " + name)
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    env = clean_environment()
    dist = evidence.directory / "dist"
    dist.mkdir()
    command([sys.executable, "-m", "build", "--sdist", "--outdir", str(dist), str(snapshot)],
            workspace, evidence, "build-sdist", env)
    archives = list(dist.glob("*.tar.gz"))
    check(len(archives) == 1, "Expected exactly one source distribution")
    extracted = workspace / "sdist"
    extracted.mkdir()
    with tarfile.open(archives[0]) as archive:
        archive.extractall(extracted, filter="data")
    roots = list(extracted.iterdir())
    check(len(roots) == 1 and roots[0].is_dir(), "Unexpected source distribution structure")
    command([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(roots[0])],
            workspace, evidence, "build-wheel-from-sdist", env)
    wheels = list(dist.glob("*.whl"))
    check(len(wheels) == 1, "Expected exactly one wheel")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        check("tavily/databases/oracledb/oracledb.py" in names, "Oracle adapter missing from wheel")
        check(not any(Path(name).name.startswith(".env") for name in names), "Env file in wheel")
    command([sys.executable, "-m", "venv", str(workspace / "venv")],
            workspace, evidence, "create-consumer", env)
    python = workspace / "venv" / "bin" / "python"
    command([str(python), "-m", "pip", "install", str(wheel)],
            workspace, evidence, "install-base-wheel", env)
    probe = """
import importlib.metadata, importlib.util, json, pathlib, sys
import tavily
from tavily import TavilyClient, AsyncTavilyClient, TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from tavily.databases.mongodb import MongoDB
assert importlib.util.find_spec('oracledb') is None
assert importlib.util.find_spec('cohere') is None
assert pathlib.Path(tavily.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
client = TavilyClient(api_key='fixture-key')
client.close()
print(json.dumps({'version': importlib.metadata.version('tavily-python'), 'path': tavily.__file__,
                  'python': sys.version.split()[0], 'oracle_and_cohere_absent': True}))
"""
    probe_result = json.loads(command([str(python), "-I", "-c", probe],
                                     workspace, evidence, "base-imports", env))
    evidence.passed("P01", "Installed base wheel imports without Oracle or Cohere", **probe_result)
    driver = importlib.metadata.version("oracledb")
    command([str(python), "-m", "pip", "install", "oracledb==" + driver],
            workspace, evidence, "install-oracle-driver", env)
    oracle_probe = """
import importlib.util, json, oracledb
from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB
assert importlib.util.find_spec('cohere') is None
print(json.dumps({'driver': oracledb.__version__, 'OracleDB': OracleDB.__name__}))
"""
    oracle_result = json.loads(command([str(python), "-I", "-c", oracle_probe],
                                      workspace, evidence, "oracle-imports", env))
    command([str(python), "-m", "pip", "check"], workspace, evidence, "pip-check", env)
    evidence.passed("P02", "Oracle consumer dependencies and imports resolve", **oracle_result)
    verify_consumer({"consumer_python": str(python), "wheel": str(wheel),
                     "consumer_workspace": str(workspace)}, evidence)
    check(fingerprint() == original, "Package sources changed during build")
    evidence.passed("P03", "Wheel built from source distribution with stable source fingerprint",
                    source_sha256=original["sha256"], wheel_sha256=sha256(wheel),
                    sdist_sha256=sha256(archives[0]))
    evidence.report.update({"source": original, "commit": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "consumer_workspace": str(workspace), "consumer_python": str(python),
        "wheel": str(wheel), "wheel_sha256": sha256(wheel),
        "dependencies": json.loads(command([str(python), "-m", "pip", "list", "--format=json"],
                                           workspace, evidence, "dependencies", env))})


def load_package(directory):
    report = json.loads((Path(directory) / "report.json").read_text())
    check(report["profile"] == "package" and report["status"] == "passed", "Package profile must pass")
    check(sorted(case["id"] for case in report["cases"]) == ["P01", "P02", "P03"],
          "Package report has incomplete cases")
    check(report["source"] == fingerprint(), "Sources differ from certified package")
    check(sha256(report["wheel"]) == report["wheel_sha256"], "Wheel fingerprint changed")
    check(Path(report["consumer_python"]).is_file(), "Consumer environment is no longer present")
    return report


def provision(values, evidence, before, definition=None, filename="ownership.json"):
    run_id = uuid.uuid4().hex
    table = table_identifier("TVE_" + run_id[:20].upper())
    manifest = {"run_id": run_id, "table": table, "identity": before["identity"],
                "object_id": None, "state": "planned"}
    manifest_path = evidence.directory / filename
    write_json(manifest_path, manifest)
    with connect(values) as connection:
        with connection.cursor() as cursor:
            default_definition = """
                ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                CONTENT CLOB, EMBEDDINGS VECTOR(3, FLOAT32), ADDED_AT TIMESTAMP,
                RAW_PAYLOAD JSON, SOURCE_URL VARCHAR2(4000), SOURCE_TITLE VARCHAR2(4000),
                RETRIEVAL_QUERY VARCHAR2(1000), RETRIEVAL_TIMESTAMP TIMESTAMP WITH TIME ZONE,
                RETRIEVAL_MODE VARCHAR2(30), CACHE_HIT NUMBER(1), INSERTED_FROM VARCHAR2(30),
                PROVIDER_NAME VARCHAR2(50), MEMORY_SCOPE VARCHAR2(30),
                EXPIRES_AT TIMESTAMP WITH TIME ZONE, LAST_SEEN_AT TIMESTAMP WITH TIME ZONE,
                QUERY_COUNT NUMBER, TENANT_ID VARCHAR2(64) NOT NULL
            """
            cursor.execute("CREATE TABLE " + table + " (" + (definition or default_definition) + ")")
            cursor.execute("SELECT object_id FROM user_objects WHERE object_name=:name "
                           "AND object_type='TABLE'", name=table)
            manifest["object_id"] = cursor.fetchone()[0]
            manifest["state"] = "created"
            write_json(manifest_path, manifest)
            cursor.execute("COMMENT ON TABLE " + table + " IS 'tavily-e2e:" + run_id + "'")
            manifest["state"] = "marked"
            write_json(manifest_path, manifest)
        verify_owner(connection, manifest)
    return manifest


@contextmanager
def fixture_server(query):
    state = {"requests": []}
    results = [
        {"title": "Oracle vector fixture", "url": "https://example.invalid/vector",
         "content": "Oracle vector fixture — Unicode café नमस्ते. " * 160, "score": 0.95},
        {"title": "Oracle cache fixture", "url": "https://example.invalid/cache",
         "content": "Oracle cache fixture preserves committed search results.", "score": 0.87},
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                check(0 < length < 32768, "Invalid request size")
                body = json.loads(self.rfile.read(length))
                state["requests"].append({"method": "POST", "path": self.path, "body": body})
                check(self.path == "/search" and len(state["requests"]) == 1, "Unexpected request")
                check(self.headers.get("Authorization") == "Bearer fixture-key", "Auth header mismatch")
                check(body == {"query": query, "max_results": 2, "search_depth": "basic",
                               "include_usage": True}, "Search payload mismatch")
                status, data = 200, {"results": results, "usage": {"credits": 1}}
            except Exception as error:
                state["error"] = str(error)
                status, data = 400, {"detail": {"error": str(error)}}
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        check(not thread.is_alive(), "Fixture server did not shut down")


def journey(values, evidence, package_report):
    evidence.report["installed_wheel_check"] = verify_consumer(package_report, evidence)
    before = preflight(values)
    evidence.report["preflight"] = before
    owned = provision(values, evidence, before)
    workspace = Path(package_report["consumer_workspace"])
    worker = workspace / ("consumer-" + owned["run_id"] + ".py")
    shutil.copy2(HERE / "consumer.py", worker)
    env = clean_environment(values)
    if evidence.profile == "smoke":
        env["TAVILY_API_KEY"] = "fixture-key"
    config = {"table": owned["table"], "run_id": owned["run_id"],
              "profile": evidence.profile, "query": QUERY}
    evidence.report.update({"ownership": owned, "package_run": package_report["artifacts"],
                            "wheel_sha256": package_report["wheel_sha256"],
                            "source_sha256": package_report["source"]["sha256"],
                            "embedding": "fixed 3-dimensional fixture; no semantic quality claim"})
    with fixture_server(QUERY) as (endpoint, fixture):
        config["endpoint"] = endpoint
        outputs = []
        for phase in ("seed", "restart"):
            config["phase"] = phase
            with connect(values) as connection:
                verify_owner(connection, owned)
            output = json.loads(command([package_report["consumer_python"], "-I", str(worker)],
                                        workspace, evidence, phase, env,
                                        json.dumps(config), timeout=120))
            outputs.append(output)
            for case in output["cases"]:
                evidence.passed(case.pop("id"), case.pop("name"), **case)
            config["snapshot_sha256"] = output["snapshot_sha256"]
        check(outputs[0]["pid"] != outputs[1]["pid"], "Restart reused the original process")
        check(outputs[0]["http_requests"] == 1 and outputs[1]["http_requests"] == 0,
              "Unexpected provider request count")
        if evidence.profile == "smoke":
            check(len(fixture["requests"]) == 1 and "error" not in fixture, "Fixture request contract failed")
        else:
            check(not fixture["requests"], "Live profile unexpectedly used the fixture")
        evidence.report["processes"] = [{key: output[key] for key in
            ("pid", "package_path", "package_version", "http_requests", "embedding_calls")}
            for output in outputs]
        evidence.report["fixture_requests"] = fixture["requests"]
    after = preflight(values)
    verify_schema_preserved(before, after)
    check(fingerprint()["sha256"] == package_report["source"]["sha256"], "SDK sources changed during journey")
    evidence.passed("E05", "Distinct process and unchanged pre-existing schema object identities",
                    previous_object_count=len(existing), seed_pid=outputs[0]["pid"], restart_pid=outputs[1]["pid"])


def cleanup(values, target, evidence):
    directory = Path(target).resolve()
    set_file = directory / "ownership-set.json"
    if set_file.exists():
        manifests = json.loads(set_file.read_text())
        paths = list(directory.glob("ownership-*.json"))
        paths = [path for path in paths if path.name != "ownership-set.json"]
        check(len(paths) == len(manifests) > 0, "Incomplete ownership manifest set")
        tables = {json.loads(path.read_text())["table"] for path in paths}
        check(tables == {manifest["table"] for manifest in manifests}, "Ownership manifest set changed")
    else:
        paths = [directory / "ownership.json"]
    for path in paths:
        cleanup_manifest(values, path)
    evidence.passed("C01", "Exact owned test tables cleaned", table_count=len(paths))


def cleanup_manifest(values, path):
    manifest = json.loads(path.read_text())
    with connect(values) as connection:
        if manifest.get("state") == "cleaned":
            from common import database_identity
            check(database_identity(connection) == manifest["identity"], "Database identity mismatch")
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM user_objects WHERE object_name=:name",
                               name=table_identifier(manifest["table"]))
                check(cursor.fetchone()[0] == 0, "An object now occupies the cleaned name")
        else:
            verify_owner(connection, manifest)
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE " + table_identifier(manifest["table"]) + " PURGE")
            manifest["state"] = "cleaned"
            write_json(path, manifest)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    from catalog import PROFILES
    parser.add_argument("profile", choices=["preflight", "package", "smoke", "live", "cleanup", "summary", "certification", *PROFILES])
    parser.add_argument("--env", type=Path, default=ROOT / ".env", help="Private env file; exported values override")
    parser.add_argument("--package-run", type=Path, help="Passing package artifact directory (required for smoke/live)")
    parser.add_argument("--run", type=Path, help="Exact smoke/live artifact directory to clean")
    parser.add_argument("--case", action="append", help="Select live-sdk case IDs for a focused retest")
    parser.add_argument("--artifact-dir", type=Path, help="Fresh report directory, used by certification campaigns")
    parser.add_argument("--runtime", choices=["3.9","3.10","3.11","3.12","3.13","3.14"], help="Isolated Oracle/Cohere consumer Python")
    parser.add_argument("--driver", default="auto", help="Pinned Oracle driver; auto uses 3.4.2 on Python 3.9 and 26.1.0 otherwise")
    parser.add_argument("--deployment-config", type=Path, help="Private target inventory for Oracle 23–26 expanded coverage")
    args = parser.parse_args()
    current_harness = harness_fingerprint()
    expected_harness = os.environ.get("ORACLE_E2E_EXPECTED_HARNESS")
    if expected_harness and current_harness["sha256"] != expected_harness:
        parser.error("Executable harness revision changed during certification")
    if args.profile == "certification":
        from campaign import run
        return run(args)
    if args.case:
        from catalog import LIVE_SDK
        if args.profile != "live-sdk" or any(case not in LIVE_SDK for case in args.case) or len(set(args.case)) != len(args.case):
            parser.error("--case accepts unique live-sdk IDs V01-V08 only")
    if args.profile in {"smoke", "live", "summary", *PROFILES} and args.package_run is None:
        parser.error("--package-run is required; run the package profile first")
    if args.profile == "cleanup" and args.run is None:
        parser.error("--run is required for cleanup")
    if args.profile == "summary":
        from summarize import run
        return run(args)
    evidence = Evidence(args.profile, args.artifact_dir.resolve() if args.artifact_dir else HERE / ".artifacts" / (args.profile + "-" + uuid.uuid4().hex[:12]))
    evidence.report.update(harness_revision=current_harness["sha256"],
                           campaign_id=os.environ.get("ORACLE_E2E_CAMPAIGN_ID"),
                           runtime=args.runtime, driver_requested=args.driver)
    if args.case:
        evidence.report.update(required_cases=args.case, coverage="selected cases only; not a complete profile run")
    evidence.report["harness_sha256"] = {name: sha256(HERE / name) for name in
                                       (path.name for path in HERE.glob("*.py"))}
    try:
        if args.profile == "package":
            package(evidence)
        elif args.profile in PROFILES:
            from extended import execute
            execute(args, evidence)
        else:
            values = settings(args.env, live=args.profile == "live")
            evidence.secrets = private_values(values)
            if args.profile == "preflight":
                evidence.report["preflight"] = preflight(values)
                evidence.passed("F01", "Oracle owner, connectivity, VECTOR support, and CREATE TABLE privilege")
            elif args.profile == "cleanup":
                cleanup(values, args.run, evidence)
            else:
                package_report = load_package(args.package_run)
                journey(values, evidence, package_report)
        return evidence.finish()
    except Exception as error:
        return evidence.finish(type(error).__name__ + ": " + str(error))


if __name__ == "__main__":
    raise SystemExit(main())
