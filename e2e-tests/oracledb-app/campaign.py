"""A clean campaign consumes only reports generated under one frozen revision."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from catalog import PROFILES
from common import Evidence, HERE, ROOT, REQUIRED, check, fingerprint, harness_fingerprint, redact, write_json

CAMPAIGN_PROFILES = ["preflight", "package", "smoke", "oracle", "indexes", "retrieval-modes", "reliability", "failures",
                     "contention", "ann", "soak", "sdk", "live-sdk", "live-cohere", "compatibility",
                     "deployments", "runtime-integration"]


def validate_report(report, profile, campaign_id, revision, source, wheel=None):
    check(report.get("profile") == profile and report.get("status") == "passed", "Campaign profile did not pass: "+profile)
    check(report.get("campaign_id") == campaign_id, "Historical or foreign report cannot join this campaign")
    check(report.get("harness_revision") == revision, "Campaign harness revision mismatch")
    check(not report.get("coverage") and not report.get("required_cases"), "Selected retest cannot replace a complete profile")
    expected = REQUIRED[profile]
    check(sorted(case["id"] for case in report["cases"]) == sorted(expected) and
          all(case["status"] == "passed" for case in report["cases"]), "Campaign required cases missing, duplicated or failed")
    if profile != "preflight":
        digest = report.get("source_sha256") or report.get("source", {}).get("sha256")
        check(digest == source, "Campaign source fingerprint mismatch")
        if wheel:
            check(report.get("wheel_sha256") == wheel, "Campaign package fingerprint mismatch")


def junit_pass(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {key: sum(int(suite.get(key, 0)) for suite in suites) for key in ("tests", "failures", "errors", "skipped")}
    check(totals["tests"] > 0 and not any(totals[key] for key in ("failures", "errors", "skipped")),
          "Campaign tests had failures, errors, skips or zero tests")
    return totals


def run(args):
    profiles = CAMPAIGN_PROFILES + (["deployment-coverage"] if args.deployment_config else [])
    campaign_id = "certification-"+uuid.uuid4().hex[:12]
    directory = (args.artifact_dir or HERE / ".artifacts" / campaign_id).resolve()
    step_ids = ["Q"+str(i+1).zfill(2) for i in range(len(profiles)+3)]
    REQUIRED["certification"] = step_ids
    evidence = Evidence("certification", directory)
    from dotenv import dotenv_values
    private = dotenv_values(args.env,interpolate=False)
    evidence.secrets = [os.environ.get(key) or private.get(key) for key in
                        ("ORACLE_PASSWORD","ORACLE_DSN","TAVILY_API_KEY","CO_API_KEY")]
    def log(path,text):
        path.write_text(redact(text,evidence.secrets))
    revision, source = harness_fingerprint(), fingerprint()
    evidence.report.update(campaign_id=campaign_id, harness_revision=revision["sha256"],
                           source_sha256=source["sha256"], started_with_new_package=True,
                           scope="Python >=3.9; historical Python 3.8 finding retained outside acceptance",
                           campaign_profiles=profiles, historical_reports_consumed=False)
    write_json(directory / "frozen-harness.json", revision)
    env = dict(os.environ)
    env.update(ORACLE_E2E_CAMPAIGN_ID=campaign_id, ORACLE_E2E_EXPECTED_HARNESS=revision["sha256"])
    reports, package_run, wheel = [], None, None
    try:
        # Validate this exact final harness, then all application profiles; no historical collation.
        result = subprocess.run([sys.executable, "-m", "pytest", "--junitxml="+str(directory / "harness.xml")],
                                cwd=HERE, capture_output=True, text=True, env=env, timeout=90)
        log(directory / "harness.log",result.stdout+"\n"+result.stderr)
        check(result.returncode == 0, "Final harness control tests failed")
        evidence.passed(step_ids[0], "Final revision harness controls", **junit_pass(directory / "harness.xml"))
        for number, profile in enumerate(profiles):
            check(harness_fingerprint() == revision and fingerprint() == source, "Inputs changed during certification")
            child_dir = directory / "runs" / (str(number+1).zfill(2)+"-"+profile)
            invocation = [sys.executable, str(HERE / "run.py"), profile, "--env", str(Path(args.env).resolve()),
                          "--artifact-dir", str(child_dir)]
            if profile not in {"preflight", "package"}:
                invocation.extend(["--package-run", str(package_run)])
            if profile == "deployment-coverage":
                invocation.extend(["--deployment-config", str(args.deployment_config.resolve())])
            print("Campaign "+campaign_id+": "+profile, flush=True)
            result = subprocess.run(invocation, cwd=HERE, env=env, text=True, capture_output=True, timeout=3600)
            log(directory / (profile+".log"),result.stdout+"\n"+result.stderr)
            check((child_dir / "report.json").exists(), "Profile produced no campaign report: "+profile)
            report = json.loads((child_dir / "report.json").read_text())
            reports.append({"profile": profile, "report": str(child_dir / "report.json"), "status": report["status"]})
            evidence.report["runs"] = reports
            try:
                validate_report(report, profile, campaign_id, revision["sha256"], source["sha256"], wheel)
                check(result.returncode == 0, "Campaign command failed: "+profile)
            except Exception as error:
                if profile == "package":
                    raise  # No later consumer can run without a valid package.
                evidence.report["cases"].append({"id": step_ids[number+1], "name": "Complete "+profile+" profile",
                                                 "status": "failed", "error": str(error),
                                                 "report": str(child_dir / "report.json")})
                write_json(directory / "progress.json", evidence.report)
                continue  # Keep the remaining coverage and installed-wheel regression evidence.
            if profile == "package":
                package_run, wheel = child_dir, report["wheel_sha256"]
                evidence.report["wheel_sha256"] = wheel
            evidence.passed(step_ids[number+1], "Complete "+profile+" profile", report=str(child_dir / "report.json"),
                            required_case_count=len(report["cases"]))
            write_json(directory / "progress.json", evidence.report)
        # The SDK tests execute from the campaign wheel, not the editable checkout.
        package = json.loads((package_run / "report.json").read_text())
        consumer = Path(package["consumer_workspace"])
        copied_tests = consumer / "certification-tests"
        shutil.copytree(ROOT / "tests", copied_tests / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        install = subprocess.run([package["consumer_python"], "-m", "pip", "install", "pytest"], capture_output=True, text=True, timeout=180)
        log(directory / "regression-install.log",install.stdout+"\n"+install.stderr)
        check(install.returncode == 0, "Regression driver could not be installed")
        probe = "import sys,pathlib,tavily,pytest; assert pathlib.Path(tavily.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()); sys.path.insert(0,sys.argv[1]); raise SystemExit(pytest.main([sys.argv[1]+'/tests','--junitxml='+sys.argv[2],'-q']))"
        result = subprocess.run([package["consumer_python"], "-I", "-c", probe, str(copied_tests), str(directory / "regression.xml")],
                                cwd=copied_tests, env=env, capture_output=True, text=True, timeout=180)
        log(directory / "regression.log",result.stdout+"\n"+result.stderr)
        check(result.returncode == 0, "Installed campaign package regression suite failed")
        evidence.passed(step_ids[-2], "Installed campaign wheel regression suite", **junit_pass(directory / "regression.xml"))
        check(harness_fingerprint() == revision and fingerprint() == source, "Final campaign fingerprint changed")
        nested, failed_nested = [], []
        for path in (directory / "runs").rglob("report.json"):
            report = json.loads(path.read_text())
            check(report.get("campaign_id") == campaign_id and report.get("harness_revision") == revision["sha256"],
                  "Nested profile revision/campaign mismatch")
            if report["status"] != "passed":
                failed_nested.append(str(path))
            if report["profile"] != "preflight":
                digest = report.get("source_sha256") or report.get("source", {}).get("sha256")
                check(digest == source["sha256"] and report.get("wheel_sha256") == wheel, "Nested package fingerprint mismatch")
            nested.append(str(path))
        if failed_nested:
            evidence.report["cases"].append({"id": step_ids[-1], "name": "Single-revision fresh-campaign provenance",
                                             "status": "failed", "error": "Nested campaign profiles did not all pass",
                                             "failed_reports": failed_nested, "nested_reports": nested})
        else:
            evidence.passed(step_ids[-1], "Single-revision fresh-campaign provenance", nested_reports=nested,
                            harness_revision=revision["sha256"], wheel_sha256=wheel)
        return evidence.finish()
    except Exception as error:
        return evidence.finish(type(error).__name__+": "+str(error))
