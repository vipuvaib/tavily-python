"""Acceptance controls prevent deployment and diagnostic evidence overclaims."""
import json

import pytest

from ann_cases import cursor_statistics
from deployment_matrix import inventory
from oracle_config import options, server_major, server_release
from common import private_values, sanitize, transient_vector_auxiliary, verify_schema_preserved


@pytest.mark.parametrize("version", ["21.1.0", "22.9.0", "27.1.0"])
def test_out_of_scope_server_is_rejected(version):
    with pytest.raises(ValueError):
        server_major(version)


def test_26ai_release_numbered_23_is_not_counted_as_23ai():
    assert server_release("23.26.1.0.0", "Oracle AI Database 26ai Free Release 23.26.1.0.0") == 26
    assert server_release("23.9.0.25.07", "Oracle Database 23ai Free Release 23.9.0.25.07") == 23
    with pytest.raises(ValueError):
        server_release("23.27.1.0.0", "Oracle AI Database 27ai")


def test_tls_identity_verification_cannot_be_disabled():
    with pytest.raises(ValueError):
        options({"ORACLE_CONNECT_KWARGS": json.dumps({"ssl_server_dn_match": False})})


def test_secret_redaction_preserves_transport_evidence():
    values = {"ORACLE_PASSWORD": "private-secret", "ORACLE_E2E_MODE": "thick",
              "ORACLE_CONNECT_KWARGS": json.dumps({"wallet_password": "wallet-secret"})}
    result = sanitize({"mode": "thick", "diagnostic": "private-secret wallet-secret"}, private_values(values))
    assert result == {"mode": "thick", "diagnostic": "<redacted> <redacted>"}


def test_internal_vector_rollover_does_not_exempt_base_or_unrelated_objects():
    index = "TVE_" + "A" * 20 + "_VEC"
    prefixes = ["VECTOR$" + index + "$"]
    assert transient_vector_auxiliary("VECTOR$" + index + "$1_2_0$HNSW_ROWID_VID_MAP", prefixes)
    assert transient_vector_auxiliary("VECTOR$" + index + "$1_2_1$HNSW_ROWID_VID_IDX", prefixes)
    for name in [index, "TVE_" + "A" * 20, "VECTOR$APPLICATION_INDEX$1$MAP", "VECTOR$" + index + "_OTHER$1$MAP",
                 "VECTOR$" + index + "$1_2_0$HNSW_CHECKPOINTS", "VECTOR$" + index + "$1_2_0$IVF_FLAT_CENTROIDS"]:
        assert not transient_vector_auxiliary(name, prefixes)


@pytest.mark.parametrize("objects,index_status", [([['APPLICATION', 'TABLE', 999], ['APP_VECTOR', 'INDEX', 2]], 'VALID'),
                                                ([['APPLICATION', 'TABLE', 1]], 'VALID'),
                                                ([['APPLICATION', 'TABLE', 1], ['APP_VECTOR', 'INDEX', 2]], 'UNUSABLE')])
def test_schema_guard_still_rejects_application_identity_or_index_damage(objects, index_status):
    before = {"existing_objects": [["APPLICATION", "TABLE", 1], ["APP_VECTOR", "INDEX", 2]],
              "existing_vector_indexes": [["APP_VECTOR", "APPLICATION", "VALID"]]}
    after = {"existing_objects": objects, "existing_vector_indexes": [["APP_VECTOR", "APPLICATION", index_status]]}
    with pytest.raises(AssertionError):
        verify_schema_preserved(before, after)


class Cursor:
    def __init__(self, sources, plan="A-Rows"):
        self.sources, self.plan, self.executed = sources, plan, []

    def execute(self, sql, **bindings):
        self.executed.append((sql, bindings))

    def fetchone(self):
        return ("owned_sql_id", 2)

    def fetchall(self):
        return [(self.plan,)] if "DISPLAY_CURSOR" in self.executed[-1][0] else self.sources


@pytest.mark.parametrize("source,display", [
    (["VECTOR INDEX", "HNSW SCAN", "OWNED_ANN", 0, 10, 0, 0, 1], "A-Rows"),
    (["VECTOR INDEX", "HNSW SCAN", "OTHER_ANN", 1, 10, 0, 0, 1], "A-Rows"),
    (["TABLE ACCESS", "FULL", "OWNED_TABLE", 1, 10, 0, 0, 1], "A-Rows"),
    (["VECTOR INDEX", "HNSW SCAN", "OWNED_ANN", 1, 10, 0, 0, 1], "Explain plan only"),
])
def test_live_plan_requires_executed_owned_index_and_actual_rows(source, display):
    with pytest.raises(AssertionError):
        cursor_statistics(Cursor([source], display), "owned approximate SQL", "OWNED_ANN", "HNSW")


def test_live_plan_uses_explicit_executed_cursor_identity():
    cursor = Cursor([["VECTOR INDEX", "HNSW SCAN", "OWNED_ANN", 1, 10, 2, 0, 100]])
    evidence = cursor_statistics(cursor, "owned approximate SQL", "OWNED_ANN", "HNSW")
    assert evidence["status"] == "passed"
    assert cursor.executed[-1][1] == {"sql_id": "owned_sql_id", "child": 2}
    assert cursor.executed[0][1] == {"text": "owned approximate SQL"}


def test_target_inventory_rejects_public_credential_files_and_duplicate_names(tmp_path):
    env = tmp_path / "private.env"
    env.write_text("ORACLE_PASSWORD=fixture-secret\n")
    env.chmod(0o600)
    target = {"name": "same", "env": str(env), "expected_release": 23}
    config = {"targets": [target, target], "tls": target, "diagnostics": target,
              "client_lib_dir": str(tmp_path)}
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    with pytest.raises(AssertionError, match="unique"):
        inventory(path)
    config["targets"] = [target]
    path.write_text(json.dumps(config))
    env.chmod(0o644)
    with pytest.raises(AssertionError, match="private"):
        inventory(path)
