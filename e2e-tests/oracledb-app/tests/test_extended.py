import json
import urllib.request
import xml.etree.ElementTree as ET

import pytest

from catalog import PROFILES
from common import Evidence, REQUIRED
from extended import adopt
from service import provider_server


def test_all_extended_profiles_require_unique_nonempty_inventories():
    ids = []
    for profile, cases in PROFILES.items():
        assert cases and REQUIRED[profile] == list(cases)
        ids.extend(cases)
    assert len(ids) == len(set(ids))


def test_failed_consumer_case_is_never_reported_as_passed(tmp_path):
    evidence = Evidence("sdk", tmp_path / "run", ["private-secret"])
    cases = [{"id": key, "name": name, "status": "passed"} for key, name in PROFILES["sdk"].items()]
    cases[0].update(status="failed", error="private-secret provider failure")
    adopt(evidence, {"cases": cases, "pid": 1})
    assert evidence.finish() == 1
    # Failed child diagnostics must be redacted before being saved as evidence.
    xml = ET.parse(evidence.directory / "junit.xml")
    assert xml.getroot().get("failures") == "1"
    assert len(xml.findall("testcase/failure")) == 1
    assert "private-secret" not in (evidence.directory / "junit.xml").read_text()
    assert "private-secret" not in (evidence.directory / "report.json").read_text()


def test_general_fixture_research_pending_complete_and_stream():
    with provider_server() as (endpoint, state):
        def post(path, value):
            req = urllib.request.Request(endpoint + path, data=json.dumps(value).encode())
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        assert json.loads(post("/research", {"input": "query"})[1])["request_id"] == "owned-research"
        with urllib.request.urlopen(endpoint + "/research/owned-research?include_usage=true", timeout=5) as response:
            assert response.status == 202
        with urllib.request.urlopen(endpoint + "/research/owned-research?include_usage=true", timeout=5) as response:
            assert response.status == 200 and json.load(response)["status"] == "completed"
        assert b"event: complete" in post("/research", {"stream": True})[1]
        assert len(state["requests"]) == 4


def test_selected_cases_are_explicit_and_full_profiles_remain_strict(tmp_path):
    evidence = Evidence("live-sdk", tmp_path / "selected")
    evidence.report.update(required_cases=["V07"], coverage="selected cases only")
    evidence.passed("V07", "research")
    assert evidence.finish() == 0
    full = Evidence("live-sdk", tmp_path / "full")
    full.passed("V07", "research")
    assert full.finish() == 1


def test_summary_uses_latest_attempt_and_excludes_unrelated_runner_failure():
    from summarize import latest_cases
    reports = [
        {"finished_at": 1, "artifacts": "/first", "cases": [{"id": "V07", "status": "passed"}]},
        {"finished_at": 2, "artifacts": "/failed", "error": "Required case inventory did not pass exactly once",
         "cases": [{"id": "V07", "status": "failed"}]},
        {"finished_at": 3, "artifacts": "/runner-failure", "error": "Source fingerprint changed",
         "cases": [{"id": "V07", "status": "passed"}]},
    ]
    result = latest_cases(reports, ["V07"])["V07"]
    assert result["status"] == "failed" and result["evidence"] == "/failed/report.json"
