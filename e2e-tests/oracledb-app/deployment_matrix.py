"""Expanded deployment coverage requires real targets, not simulated versions."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from dotenv import dotenv_values
from catalog import PROFILES
from common import HERE, check, harness_fingerprint, redact, write_json, private_values, sha256
from oracle_config import server_major, server_release


def inventory(path):
    path = Path(path).resolve()
    check(path.is_file() and path.stat().st_mode & 0o077 == 0, "Target inventory must be private")
    result = json.loads(path.read_text())
    targets = result.get("targets", [])
    check(bool(targets), "Configure at least one real Oracle target")
    names = set()
    for target in targets + [result.get("tls", {}), result.get("diagnostics", {})]:
        check(isinstance(target, dict) and target.get("env"), "Target needs a private env file")
        env = Path(target["env"])
        check(env.is_absolute() and env.is_file() and env.stat().st_mode & 0o077 == 0,
              "Each target env must be an absolute private file")
        check(isinstance(target.get("expected_release"), int), "Target needs expected_release")
        server_major(str(target["expected_release"]))
    for target in targets:
        name = target.get("name", "")
        check(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name)) and name not in names, "Target names must be unique safe identifiers")
        names.add(name)
    library = Path(result.get("client_lib_dir", ""))
    check(library.is_absolute() and library.is_dir(), "Configure an installed native client directory")
    return result


def run(args, package, evidence):
    check(args.deployment_config is not None, "--deployment-config is required for expanded coverage")
    config = inventory(args.deployment_config)
    revision = harness_fingerprint()["sha256"]
    evidence.report.update(deployment_scope={"minimum": 23, "maximum": 26,
                           "required_endpoint_releases": [23, 26]}, deployment_runs=[])
    secrets = []
    for target in config["targets"] + [config["tls"], config["diagnostics"]]:
        values = dotenv_values(target["env"], interpolate=False)
        secrets.extend(private_values(values))
    evidence.secrets = secrets
    inputs = [Path(args.deployment_config).resolve()]
    inputs.extend(Path(target["env"]) for target in config["targets"] + [config["tls"], config["diagnostics"]])
    frozen = {str(path): sha256(path) for path in inputs}
    evidence.report["deployment_input_sha256"] = frozen

    def environment(target, mode="thin", strict_plan=False):
        env = {key: value for key, value in os.environ.items() if not key.startswith("ORACLE_")}
        env.update({key: value for key, value in dotenv_values(target["env"], interpolate=False).items() if value is not None})
        env["ORACLE_E2E_MODE"] = mode
        if mode == "thick":
            env["ORACLE_CLIENT_LIB_DIR"] = config["client_lib_dir"]
            env["LD_LIBRARY_PATH"] = config["client_lib_dir"] + ":" + env.get("LD_LIBRARY_PATH", "")
        if strict_plan:
            env["ORACLE_E2E_REQUIRE_LIVE_PLAN"] = "1"
        # Preserve outer campaign identity without inheriting another target's credentials.
        for key in ("ORACLE_E2E_CAMPAIGN_ID", "ORACLE_E2E_EXPECTED_HARNESS"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def child(target, profile, label, mode="thin", strict_plan=False):
        directory = evidence.directory / label
        command = [sys.executable, str(HERE / "run.py"), profile, "--env", target["env"],
                   "--artifact-dir", str(directory)]
        if profile != "preflight":
            command.extend(["--package-run", str(args.package_run.resolve())])
        print("Deployment coverage: " + label, flush=True)
        result = subprocess.run(command, env=environment(target, mode, strict_plan),
                                cwd=HERE, capture_output=True, text=True, timeout=900)
        (evidence.directory / (label + ".log")).write_text(redact(result.stdout + "\n" + result.stderr, secrets))
        report = json.loads((directory / "report.json").read_text())
        entry = {"label": label, "profile": profile, "mode": mode, "report": str(directory / "report.json"),
                 "status": report["status"], "case_count": len(report["cases"])}
        evidence.report["deployment_runs"].append(entry)
        write_json(evidence.directory / "progress.json", evidence.report)
        check(result.returncode == 0 and report["status"] == "passed", "Deployment profile failed: " + label)
        check(report.get("harness_revision") == revision == harness_fingerprint()["sha256"], "Deployment harness changed")
        expected = ["F01"] if profile == "preflight" else list(PROFILES[profile])
        check(sorted(case["id"] for case in report["cases"]) == sorted(expected) and
              all(case["status"] == "passed" for case in report["cases"]), "Deployment profile incomplete")
        before = report["preflight"]
        release = server_release(before["database_version"], before["database_banner"])
        check(release == target["expected_release"], "Actual Oracle product release differs from target declaration")
        check(before["oracle_mode"] == mode, "Requested Oracle mode was not used")
        if mode == "thick":
            check(tuple(before["client_version"][:2]) >= (23, 4), "Native VECTOR coverage requires Client >=23.4")
        if profile != "preflight":
            check(report.get("consumer", {}).get("oracle_mode") == mode, "Installed consumer used the wrong Oracle mode")
            check(report["source_sha256"] == package["source"]["sha256"] and
                  report["wheel_sha256"] == package["wheel_sha256"], "Deployment source/wheel differs")
        return report

    def versions():
        versions = []
        for target in config["targets"]:
            report = child(target, "preflight", target["name"] + "-preflight")
            versions.append({"name": target["name"], "version": report["preflight"]["database_version"],
                             "reported_major": report["preflight"]["database_major"],
                             "release": report["preflight"]["database_release"],
                             "banner": report["preflight"]["database_banner"]})
        check({23, 26}.issubset({entry["release"] for entry in versions}), "Both actual Oracle 23ai and 26ai targets are required")
        return {"targets": versions, "scope": "Recorded server versions only; no inferred intermediate releases"}

    def modes():
        reports = []
        failures = []
        for target in config["targets"]:
            for mode in ("thin", "thick"):
                for profile in ("oracle", "indexes", "retrieval-modes"):
                    label = target["name"] + "-" + mode + "-" + profile
                    try:
                        report = child(target, profile, label, mode)
                        reports.append({"label": label, "cases": len(report["cases"])})
                    except Exception as error:
                        failures.append(label + ": " + str(error))
        check(not failures, "; ".join(failures))
        return {"complete_profiles": reports}

    def tls():
        target = config["tls"]
        # A passing TCP run cannot be counted as encrypted deployment coverage.
        probe_script = ("import sys,json,os; sys.path.insert(0,sys.argv[1]); from common import connect; "
                        "c=connect(dict(os.environ)); q=c.cursor(); "
                        "q.execute(\"select sys_context('USERENV','NETWORK_PROTOCOL') from dual\"); "
                        "assert q.fetchone()[0].lower()=='tcps'; print('verified-tcps'); c.close()")
        for mode in ("thin", "thick"):
            result = subprocess.run([sys.executable, "-I", "-c", probe_script, str(HERE)],
                                    env=environment(target, mode), capture_output=True, text=True, timeout=30)
            (evidence.directory / ("tls-" + mode + "-transport.log")).write_text(redact(result.stdout + result.stderr, secrets))
            check(result.returncode == 0 and "verified-tcps" in result.stdout, "Database did not confirm TCPS: " + mode)
        reports = []
        for mode in ("thin", "thick"):
            for profile in ("oracle", "indexes", "retrieval-modes"):
                report = child(target, profile, "tls-" + mode + "-" + profile, mode)
                reports.append({"mode": mode, "profile": profile, "cases": len(report["cases"])})
        # Reject both an unexpected certificate identity and an untrusted certificate.
        script = ("import sys,json,os; sys.path.insert(0,sys.argv[1]); "
                  "from common import connect; from oracle_config import options; "
                  "v=dict(os.environ); p=options(v); "
                  "p.update(json.loads(sys.argv[2])); v['ORACLE_CONNECT_KWARGS']=json.dumps(p); "
                  "c=connect(v); c.close()")
        negative = []
        for mode in ("thin", "thick"):
            for label, overrides in (("wrong-identity", {"ssl_server_cert_dn": "CN=wrong-tavily-e2e"}),
                                     ("untrusted-wallet", {"wallet_location": config["tls"]["untrusted_wallet"]})):
                result = subprocess.run([sys.executable, "-I", "-c", script, str(HERE), json.dumps(overrides)],
                                        env=environment(target, mode), capture_output=True, text=True, timeout=30)
                detail = redact(result.stdout + "\n" + result.stderr, secrets)
                (evidence.directory / ("tls-" + mode + "-" + label + ".log")).write_text(detail)
                check(result.returncode != 0 and any(code in detail for code in
                      ("CERTIFICATE_VERIFY_FAILED", "DPY-6002", "ORA-29024", "ORA-28860", "ORA-29003", "ORA-28759")),
                      "TLS negative case did not reject certificate configuration: " + mode + "-" + label)
                healthy = subprocess.run([sys.executable, "-I", "-c", probe_script, str(HERE)],
                                         env=environment(target, mode), capture_output=True, text=True, timeout=30)
                check(healthy.returncode == 0 and "verified-tcps" in healthy.stdout,
                      "Correct TLS configuration did not recover after negative case")
                negative.append({"mode": mode, "case": label, "status": "rejected"})
        return {"complete_profiles": reports, "certificate_rejections": negative,
                "scope": "Wallet-based one-way TLS with certificate verification; mTLS is separate"}

    def plans():
        report = child(config["diagnostics"], "ann", "live-cursor-ann", strict_plan=True)
        check(all(case["live_cursor_diagnostic"]["status"] == "passed" for case in report["cases"]),
              "ANN runtime statistics were not available")
        return {"indexes": [{"kind": case["kind"], "sql_id": case["live_cursor_diagnostic"]["sql_id"],
                             "recall_at_10": case["mean_recall_at_10"]} for case in report["cases"]]}

    def sessions():
        report = child(config["diagnostics"], "diagnostics", "server-session-metrics")
        return {"report": report["artifacts"], "scope": "Server fixed-view observations of uniquely tagged test sessions"}

    for (case_id, name), operation in zip(PROFILES["deployment-coverage"].items(), [versions, modes, tls, plans, sessions]):
        try:
            evidence.passed(case_id, name, **operation())
        except Exception as error:
            evidence.report["cases"].append({"id": case_id, "name": name, "status": "failed",
                                             "error": redact(type(error).__name__ + ": " + str(error), secrets)})
            print(case_id + " FAIL " + name, flush=True)
    check(frozen == {path: sha256(path) for path in frozen}, "Deployment configuration changed during the run")
