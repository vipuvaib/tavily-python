"""Nested acceptance profiles retain separate owned manifests and exact evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys

from common import HERE, check, harness_fingerprint, redact


def child(args, package, evidence, profile, directory, runtime, driver="26.1.0"):
    from run import command
    expected = harness_fingerprint()["sha256"]
    invocation = [sys.executable, str(HERE / "run.py"), profile, "--env", str(Path(args.env).resolve()),
                  "--package-run", str(args.package_run.resolve()), "--artifact-dir", str(directory),
                  "--runtime", runtime, "--driver", driver]
    result = subprocess.run(invocation, cwd=HERE, text=True, capture_output=True, timeout=900)
    (evidence.directory / ("child-"+profile+"-"+runtime+"-"+driver+".log")).write_text(redact(result.stdout+"\n"+result.stderr,evidence.secrets))
    report_path = directory / "report.json"
    check(report_path.exists(), "Child profile produced no report: "+str(report_path))
    report = json.loads(report_path.read_text())
    check(report.get("harness_revision") == expected == harness_fingerprint()["sha256"], "Child harness revision mismatch")
    check(report.get("source_sha256") == package["source"]["sha256"] and report.get("wheel_sha256") == package["wheel_sha256"],
          "Child package/source fingerprint mismatch")
    return {"profile": profile, "runtime": runtime, "driver": driver, "status": report["status"],
            "returncode": result.returncode, "report": str(report_path), "case_count": len(report["cases"]),
            "failed_cases": [case["id"] for case in report["cases"] if case["status"] != "passed"],
            "consumer": report.get("consumer", {})}


def runtime_integration(args, package, evidence):
    from dotenv import dotenv_values
    values = dotenv_values(args.env,interpolate=False)
    evidence.secrets = [os.environ.get(key) or values.get(key) for key in
                        ("ORACLE_PASSWORD","ORACLE_DSN","TAVILY_API_KEY","CO_API_KEY")]
    outcomes = []
    for runtime in ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14"):
        for profile in ("oracle", "live-cohere"):
            driver = "3.4.2" if runtime == "3.9" else "26.1.0"
            result = child(args, package, evidence, profile, evidence.directory / ("python-"+runtime) / profile, runtime, driver)
            outcomes.append(result)
            evidence.report["integration_matrix"] = outcomes
            print("Python "+runtime+" "+profile+" "+result["status"], flush=True)
    check(all(item["status"] == "passed" and item["returncode"] == 0 for item in outcomes), "One or more full Oracle/Cohere runtime integrations failed")
    evidence.passed("J01", "Full Oracle and live Cohere integration across Python >=3.9 runtimes", integrations=outcomes,
                    provider_budget="one real Tavily search, four embedding calls and one rerank per runtime")


def older_driver(args, package, evidence):
    result = child(args, package, evidence, "oracle", evidence.directory / "driver-3.4.2", "3.12", "3.4.2")
    evidence.report["driver_matrix"] = [result]
    check(result["status"] == "passed" and result["returncode"] == 0, "Oracle driver 3.4.2 behavior suite failed")
    evidence.passed("D02", "Full Oracle behavior suite using Oracle driver 3.4.2", integration=result)
