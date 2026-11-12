"""Controls prevent unsafe fixtures and tuning from replacing failed gates."""
import pytest

from ivf_study import choose_probe, fixture, query_sql, vector_hash, verify_original_exact


def test_study_rejects_unowned_tables_and_unbounded_sql_settings():
    for table, setting in [("APPLICATION", "probes32"), ("TVE_" + "A" * 20, "probes1000000"),
                           ("TVE_" + "A" * 20, "accuracy95; DELETE FROM X")]:
        with pytest.raises(AssertionError):
            query_sql(table, setting)


def test_probe_candidate_is_selected_only_from_passing_calibration_settings():
    summaries = {"probes" + str(n): {"meets_mean_threshold": n >= 20}
                 for n in [1, 2, 4, 8, 12, 16, 20, 24, 28, 32]}
    assert choose_probe(summaries) == "probes20"
    assert choose_probe({key: {"meets_mean_threshold": False} for key in summaries}) is None


def test_original_exact_results_cannot_be_replaced_by_another_fixture():
    expected = [{"exact_ids": list(range(i, i + 10))} for i in range(24)]
    verify_original_exact([list(reversed(row["exact_ids"])) for row in expected], expected)
    changed = [row["exact_ids"][:] for row in expected]
    changed[-1][-1] = 999
    with pytest.raises(AssertionError):
        verify_original_exact(changed, expected)


def test_heldout_queries_are_reproducible_and_separate_from_original():
    corpus, original, heldout = fixture()
    assert (len(corpus), len(original), len(heldout)) == (4096, 24, 96)
    assert len({tuple(v) for v in corpus + original + heldout}) == 4096 + 24 + 96
    assert vector_hash(heldout) == vector_hash(fixture()[2])
