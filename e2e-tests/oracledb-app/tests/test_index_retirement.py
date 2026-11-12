"""Resident graph cleanup cannot mutate another table or another index type."""
import pytest

import common
from common import retire_hnsw_index

TABLE = "TVE_" + "A" * 20
MANIFEST = {"table": TABLE, "identity": {"database": "expected"}, "object_id": 1, "run_id": "run"}


class Cursor:
    def __init__(self, row, marker="tavily-e2e:run"):
        self.rows = iter([(1,), (marker,), row, (1,), (marker,)])
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, **bindings):
        self.statements.append(sql)

    def fetchone(self):
        return next(self.rows)


class Connection:
    def __init__(self, cursor):
        self.value = cursor

    def cursor(self):
        return self.value


@pytest.mark.parametrize("row", [("OTHER", "VECTOR", "INMEMORY_NEIGHBOR_GRAPH_HNSW", 9),
                                  (TABLE, "NORMAL", None, 9),
                                  (TABLE, "VECTOR", "NEIGHBOR_PARTITIONS_IVF", 9)])
def test_retirement_refuses_wrong_table_or_index_kind(monkeypatch, row):
    monkeypatch.setattr(common, "database_identity", lambda conn: MANIFEST["identity"])
    cursor = Cursor(row)
    with pytest.raises(AssertionError):
        retire_hnsw_index(Connection(cursor), MANIFEST)
    assert not any(sql.startswith("DROP") for sql in cursor.statements)


def test_retirement_requires_original_table_marker(monkeypatch):
    monkeypatch.setattr(common, "database_identity", lambda conn: MANIFEST["identity"])
    cursor = Cursor((TABLE, "VECTOR", "INMEMORY_NEIGHBOR_GRAPH_HNSW", 9), "unrelated")
    with pytest.raises(AssertionError, match="marker"):
        retire_hnsw_index(Connection(cursor), MANIFEST)
    assert not any(sql.startswith("DROP") for sql in cursor.statements)


@pytest.mark.parametrize("object_id", [9, None])
@pytest.mark.parametrize("suffix", ["_ANN", "_VEC"])
def test_retirement_preserves_table_and_records_only_the_verified_graph(monkeypatch, object_id, suffix):
    monkeypatch.setattr(common, "database_identity", lambda conn: MANIFEST["identity"])
    cursor = Cursor((TABLE, "VECTOR", "INMEMORY_NEIGHBOR_GRAPH_HNSW", object_id))
    result = retire_hnsw_index(Connection(cursor), MANIFEST, suffix)
    assert result["table_preserved"] and result["index_object_id"] == object_id
    assert [sql for sql in cursor.statements if sql.startswith("DROP")] == ['DROP INDEX "' + TABLE + suffix + '"']


def test_retirement_refuses_an_arbitrary_suffix_before_any_sql():
    cursor = Cursor(None)
    with pytest.raises(AssertionError, match="suffix"):
        retire_hnsw_index(Connection(cursor), MANIFEST, '_ANN"; DROP TABLE X')
    assert cursor.statements == []


def test_retirement_handles_a_graph_that_was_never_created(monkeypatch):
    monkeypatch.setattr(common, "database_identity", lambda conn: MANIFEST["identity"])
    cursor = Cursor(None)
    assert retire_hnsw_index(Connection(cursor), MANIFEST)["status"] == "not_present"
    assert not any(sql.startswith("DROP") for sql in cursor.statements)
