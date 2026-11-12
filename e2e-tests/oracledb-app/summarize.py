"""Collate the latest result for every required case, retaining failed findings."""
import json
from pathlib import Path
import uuid

from catalog import PROFILES
from common import Evidence, HERE, REQUIRED, check


def latest_cases(reports, expected):
    selected = {}
    for report in sorted(reports, key=lambda item: item.get("finished_at", 0)):
        # An unrelated runner failure cannot contribute a passing certification.
        if report.get("error") and report["error"] != "Required case inventory did not pass exactly once":
            continue
        for case in report["cases"]:
            if case["id"] in expected:
                selected[case["id"]] = {**case, "evidence": report["artifacts"] + "/report.json"}
    return selected


def run(args):
    from run import load_package
    package = load_package(args.package_run)
    profiles = {"package": ["P01", "P02", "P03"], "smoke": ["E01", "E02", "E03", "E04", "E05"],
                **{key: list(value) for key, value in PROFILES.items()}}
    REQUIRED["summary"] = [case for cases in profiles.values() for case in cases]
    evidence = Evidence("summary", HERE / ".artifacts" / ("summary-" + uuid.uuid4().hex[:12]))
    available = []
    for path in (HERE / ".artifacts").glob("*/report.json"):
        report = json.loads(path.read_text())
        if report.get("wheel_sha256") != package["wheel_sha256"]:
            continue
        digest = report.get("source_sha256") or report.get("source", {}).get("sha256")
        if digest == package["source"]["sha256"]:
            available.append(report)
    outcomes = {}
    for profile, required in profiles.items():
        reports = [report for report in available if report["profile"] == profile]
        selected = latest_cases(reports, required)
        if profile == "compatibility" and "Y01" not in selected and reports:
            report = max(reports, key=lambda item: item.get("finished_at", 0))
            selected["Y01"] = {"id": "Y01", "name": PROFILES["compatibility"]["Y01"], "status": "failed",
                "error": report.get("error", "Compatibility case did not pass"),
                "python_matrix": report.get("python_matrix", []), "evidence": report["artifacts"] + "/report.json"}
        for case in required:
            if case not in selected:
                selected[case] = {"id": case, "name": "Required case missing", "status": "failed", "error": "No eligible recorded result"}
        evidence.report["cases"].extend(selected[case] for case in required)
        passed = sum(selected[case]["status"] == "passed" for case in required)
        outcomes[profile] = {"passed": passed, "required": len(required),
                             "evidence": sorted({selected[case].get("evidence") for case in required if selected[case].get("evidence")})}
        print(profile + ": " + str(passed) + "/" + str(len(required)) + " passed", flush=True)
    evidence.report.update(profiles=outcomes, source_sha256=package["source"]["sha256"],
                           wheel_sha256=package["wheel_sha256"],
                           scope="Latest result per acceptance case across recorded runs; selected retests are explicit")
    return evidence.finish()
