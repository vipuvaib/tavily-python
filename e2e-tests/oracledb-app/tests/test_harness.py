"""Exercise the controls that make a real-database test run trustworthy."""
import json
from pathlib import Path
import urllib.error
import urllib.request

import pytest

import common
from common import Evidence, check, redact, settings, table_identifier, verify_owner
from run import fixture_server


@pytest.mark.parametrize("cases", [[], ["P01", "P02"], ["P01", "P02", "P03", "P03"]])
def test_incomplete_or_duplicate_cases_fail_profile(tmp_path, cases):
    evidence = Evidence("package", tmp_path / "run")
    for case in cases:
        evidence.passed(case, "fixture")
    assert evidence.finish() == 1
    assert json.loads((evidence.directory / "report.json").read_text())["status"] == "failed"


def test_complete_inventory_and_redacted_failure(tmp_path):
    evidence = Evidence("package", tmp_path / "run", ["private-password"])
    for case in ["P01", "P02", "P03"]:
        evidence.passed(case, "fixture")
    assert evidence.finish() == 0
    assert evidence.finish("connection private-password failed") == 1
    for name in ("report.json", "junit.xml"):
        assert "private-password" not in (evidence.directory / name).read_text()


def test_env_preserves_literal_values_and_export_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('ORACLE_USER="user"\nORACLE_PASSWORD=\'literal${TOKEN}#password\'\n'
                   'ORACLE_DSN="host/service"\nTAVILY_API_KEY="test-key"\n')
    env.chmod(0o600)
    for key in ["ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN", "TAVILY_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ORACLE_USER", "override")
    result = settings(env, live=True)
    assert result["ORACLE_PASSWORD"] == "literal${TOKEN}#password"
    assert result["ORACLE_USER"] == "override"
    env.chmod(0o644)
    with pytest.raises(AssertionError, match="owner-only"):
        settings(env)


@pytest.mark.parametrize("name", ["TAVILY_DOCUMENTS", "TVE_" + "A" * 20 + "; DROP TABLE X",
                                 "TVE_short", "TVE_" + "a" * 20])
def test_cleanup_names_cannot_target_arbitrary_tables(name):
    with pytest.raises(AssertionError):
        table_identifier(name)


class Cursor:
    def __init__(self, object_id=1, marker="tavily-e2e:run"):
        self.rows = iter([(object_id,), (marker,)])
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, **kwargs):
        check(sql.startswith("SELECT "), "Ownership verification performed a mutation")
        self.queries.append(sql)

    def fetchone(self):
        return next(self.rows)


class Connection:
    def __init__(self, cursor):
        self.value = cursor

    def cursor(self):
        return self.value


@pytest.mark.parametrize("identity,oid,marker,error", [
    ({"database": "other"}, 1, "tavily-e2e:run", "Database identity"),
    ({"database": "expected"}, 2, "tavily-e2e:run", "table identity"),
    ({"database": "expected"}, 1, "unrelated-table", "marker"),
])
def test_cleanup_refuses_changed_database_object_or_marker(monkeypatch, identity, oid, marker, error):
    monkeypatch.setattr(common, "database_identity", lambda connection: identity)
    manifest = {"table": "TVE_" + "A" * 20, "identity": {"database": "expected"},
                "object_id": 1, "run_id": "run"}
    with pytest.raises(AssertionError, match=error):
        verify_owner(Connection(Cursor(oid, marker)), manifest)


def test_fixture_validates_real_http_payload_and_rejects_extra_request():
    with fixture_server("query") as (endpoint, state):
        body = json.dumps({"query": "query", "max_results": 2, "search_depth": "basic",
                           "include_usage": True}).encode()
        request = urllib.request.Request(endpoint + "/search", data=body,
                                         headers={"Authorization": "Bearer fixture-key"})
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.load(response)
        assert len(data["results"]) == 2
        assert len(data["results"][0]["content"].encode()) > 4000
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(request, timeout=5)
        assert len(state["requests"]) == 2 and "error" in state


def test_redacts_provider_keys_and_index_credentials():
    text = redact("tvly-dev-test-secret https://user:password@example.invalid/simple")
    assert "test-secret" not in text and "password" not in text
