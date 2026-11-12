"""Orchestration for the broader installed-consumer acceptance profiles."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import hashlib

from common import HERE, check, connect, fingerprint, preflight, settings, verify_owner, write_json, private_values, verify_schema_preserved, retire_hnsw_index

FULL = """ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, CONTENT CLOB,
EMBEDDINGS VECTOR({dimensions}, FLOAT32), ADDED_AT TIMESTAMP DEFAULT SYSTIMESTAMP,
RAW_PAYLOAD JSON, SOURCE_URL VARCHAR2(4000), SOURCE_TITLE VARCHAR2(4000),
RETRIEVAL_QUERY VARCHAR2(1000), RETRIEVAL_TIMESTAMP TIMESTAMP WITH TIME ZONE,
RETRIEVAL_MODE VARCHAR2(30), CACHE_HIT NUMBER(1), INSERTED_FROM VARCHAR2(30),
PROVIDER_NAME VARCHAR2(50), MEMORY_SCOPE VARCHAR2(30), EXPIRES_AT TIMESTAMP WITH TIME ZONE,
LAST_SEEN_AT TIMESTAMP WITH TIME ZONE, QUERY_COUNT NUMBER, CONTENT_HASH VARCHAR2(64),
TENANT_ID VARCHAR2(64), REGION VARCHAR2(64), LABEL VARCHAR2(64) CHECK (LABEL <> 'REJECT')"""
SHAPES = {
    "minimal": "ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, BODY CLOB, VEC VECTOR(3,FLOAT32), CREATED_AT TIMESTAMP",
    "lifecycle": "ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, CONTENT CLOB, EMBEDDINGS VECTOR(3,FLOAT32), MEMORY_SCOPE VARCHAR2(30), EXPIRES_AT TIMESTAMP WITH TIME ZONE",
    "wrong": "ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, CONTENT CLOB, EMBEDDINGS VARCHAR2(100)",
}


def adopt(evidence, output):
    for original in output["cases"]:
        case = dict(original)
        if case.pop("status") == "passed":
            evidence.passed(case.pop("id"), case.pop("name"), **case)
        else:
            evidence.report["cases"].append(original)
            print(original["id"] + " FAIL " + original["name"], flush=True)
    evidence.report["consumer"] = {key: value for key, value in output.items() if key != "cases"}


def execute(args, evidence):
    from run import clean_environment, command, load_package, provision, verify_consumer
    from service import provider_server
    profile = args.profile
    package = load_package(args.package_run)
    evidence.report.update(package_run=package["artifacts"], wheel_sha256=package["wheel_sha256"],
                           source_sha256=package["source"]["sha256"])
    evidence.report["installed_wheel_check"] = verify_consumer(package, evidence)
    if profile == "deployment-coverage":
        from deployment_matrix import run
        return run(args, package, evidence)
    if profile == "compatibility":
        from matrix import run
        return run(package, evidence)
    if profile == "runtime-integration":
        from integration_matrix import runtime_integration
        return runtime_integration(args, package, evidence)
    live = profile in {"live-sdk", "live-cohere"}
    database = profile not in {"sdk", "live-sdk"}
    values = settings(args.env, live=live) if (database or live) else {}
    if profile == "live-cohere":
        from dotenv import dotenv_values
        key = os.environ.get("CO_API_KEY") or dotenv_values(args.env, interpolate=False).get("CO_API_KEY")
        check(bool(key), "Missing setting: CO_API_KEY")
        values["CO_API_KEY"] = key
    evidence.secrets = private_values(values)
    before = preflight(values) if database else None
    if before:
        evidence.report["preflight"] = before
    manifests = []
    config = {"profile": profile, "tables": {}, "run_id": evidence.directory.name.replace("-", "")[:32]}
    native = HERE / ".deps" / "instantclient_23_26"
    if native.is_dir():
        config["native_client_dir"] = str(native)
    if args.case:
        config["selected_cases"] = args.case
    shapes = (["full", "minimal", "lifecycle", "wrong"] if profile == "oracle" else
              ["text", "hnsw", "ivf"] if profile == "indexes" else ["full"] if database else [])
    if profile == "contention":
        shapes = ["full", "unique"]
    if profile == "ann":
        shapes = ["hnsw", "ivf", "plan"]
    if profile == "retrieval-modes":
        shapes = ["hnsw", "ivf"]
    for shape in shapes:
        definition = SHAPES.get(shape) or FULL.format(dimensions=1024 if profile == "live-cohere" else
                     32 if profile in {"ann", "retrieval-modes"} and shape != "plan" else 3)
        if shape == "unique":
            definition += ", UNIQUE(TENANT_ID,SOURCE_URL)"
        if shape == "plan":
            from ann_cases import PLAN_DEFINITION
            definition = PLAN_DEFINITION
        owned = provision(values, evidence, before, definition=definition, filename="ownership-" + shape + ".json")
        manifests.append(owned)
        config["tables"][shape] = owned["table"]
        write_json(evidence.directory / "ownership-set.json", manifests)
    workspace = Path(package["consumer_workspace"]) / ("run-"+hashlib.sha256(str(evidence.directory.resolve()).encode()).hexdigest()[:16])
    workspace.mkdir()
    # This directory contains only test helpers. The SDK remains installed in site-packages.
    for name in ("catalog.py", "worker_support.py", "oracle_config.py", "oracle_cases.py", "sdk_cases.py",
                 "index_cases.py", "reliability_cases.py", "live_cases.py", "relay.py",
                 "failure_cases.py", "contention_cases.py", "ann_cases.py", "soak_cases.py", "deployment_cases.py",
                 "diagnostic_cases.py", "retrieval_cases.py"):
        if (HERE / name).exists():
            shutil.copy2(HERE / name, workspace / name)
    launcher = workspace / "launch.py"
    launcher.write_text("import sys, pathlib, json\nsys.path.insert(0,str(pathlib.Path(__file__).parent))\n"
                        "config=json.load(sys.stdin)\n"
                        "module={'oracle':'oracle_cases','indexes':'index_cases','reliability':'reliability_cases',"
                        "'sdk':'sdk_cases','live-sdk':'live_cases','live-cohere':'live_cases',"
                        "'failures':'failure_cases','contention':'contention_cases','ann':'ann_cases',"
                        "'soak':'soak_cases','deployments':'deployment_cases','diagnostics':'diagnostic_cases',"
                        "'retrieval-modes':'retrieval_cases'}[config['profile']]\n"
                        "import importlib\nprint(json.dumps(importlib.import_module(module).run(config),sort_keys=True))\n")
    python = package["consumer_python"]
    env = clean_environment(values)
    if profile == "live-cohere" or args.runtime:
        # Keep the original base consumer free from optional providers.
        interpreter = sys.executable
        if args.runtime:
            interpreter = subprocess.check_output(["/home/vipuvaib/.local/bin/uv", "python", "find", "--no-python-downloads", args.runtime], text=True).strip()
        command([interpreter, "-m", "venv", str(workspace / "provider-venv")], workspace, evidence, "provider-venv")
        python = str(workspace / "provider-venv" / "bin" / "python")
        driver = args.driver if args.driver != "auto" else "3.4.2" if args.runtime == "3.9" else before["driver_version"]
        dependencies = [package["wheel"], "oracledb==" + driver] + (["cohere"] if profile == "live-cohere" else [])
        command([python, "-m", "pip", "install", *dependencies],
                workspace, evidence, "cohere-install", env)
        evidence.report["dependencies"] = json.loads(command([python, "-m", "pip", "list", "--format=json"],
                                                            workspace, evidence, "cohere-dependencies", env))
    with provider_server() as (endpoint, state):
        config["endpoint"] = endpoint
        config["worker_python"] = python
        with connect(values) if database else _nothing() as conn:
            if database:
                for manifest in manifests:
                    verify_owner(conn, manifest)
        output = json.loads(command([python, "-I", str(launcher)], workspace, evidence, profile,
                                    env, json.dumps(config), timeout=720 if live else 300))
        adopt(evidence, output)
        evidence.report["fixture_request_count"] = len(state["requests"])
    if profile in {"retrieval-modes", "indexes", "ann"}:
        # Retain corpus tables and IVF failure indexes, but release this run's
        # resident HNSW graph after all measurements. No pre-existing index is touched.
        owned = next(manifest for manifest in manifests if manifest["table"] == config["tables"]["hnsw"])
        with connect(values) as conn:
            evidence.report["retired_hnsw_fixture"] = retire_hnsw_index(conn, owned,
                                                                       "_VEC" if profile == "indexes" else "_ANN")
    if database:
        after = preflight(values)
        verify_schema_preserved(before, after)
        with connect(values) as conn:
            for manifest in manifests:
                verify_owner(conn, manifest)
    check(fingerprint() == package["source"], "SDK sources changed during acceptance profile")
    if profile == "deployments":
        from integration_matrix import older_driver
        older_driver(args, package, evidence)


from contextlib import contextmanager


@contextmanager
def _nothing():
    yield None
