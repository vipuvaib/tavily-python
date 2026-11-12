# Explicit Oracle vector retrieval — November 10, 2026

Implemented on the VM in `/scratch/vipuvaib/tavily-final/tavily-python`.
The user's existing local checkout was not changed. The fork's package version
remains 0.8.5; source and wheel hashes identify this implementation separately
from the earlier wheel with implicit approximate retrieval.

`OracleDB` now defaults to `FETCH EXACT` for vector retrieval, including cache,
memory, native-hybrid vector candidates and recognized Text-error fallback.
Semantic deduplication also always uses `FETCH EXACT`, even when retrieval opts
into ANN. Existing ownership, expiry, persistence and connection policies remain
covered by the regression and integration suites.

The new keyword-only query configuration is independent of index creation:

```python
# Exact is the default; existing constructor calls continue to work.
database = OracleDB(connection=connection, table_name="DOCUMENTS")

# Opt in to ANN and choose one query-time search control.
database = OracleDB(
    connection=connection,
    table_name="DOCUMENTS",
    vector_index_type="IVF",
    vector_search_mode="approx",
    vector_search_probes=16,
)
```

Approximate mode accepts one of `vector_search_probes` (IVF),
`vector_search_efsearch` (HNSW), or `vector_search_accuracy` (integer 1–100).
Probe/EFSEARCH values must be positive integers; booleans, floats, strings,
conflicting controls and incompatible index types are rejected before a managed
connection opens. Tuning values become canonical integer SQL literals so each
optimizer setting gets its own statement/plan; embeddings and metadata remain
bound. With no tuning value, ANN inherits
the index accuracy. Existing `vector_index_*` settings still govern index creation.
There is no automatic or universal 16-probe default.

Oracle can choose exact execution for an approximate request. In the retained
Oracle 23ai fixture, the new 16-probe SDK query had a cached exact scan/sort plan
and recall 1.0; that result is not evidence of IVF index use. Both 26ai fixtures
reproduced the expected non-exact recall. The Free target exposed a cached IVF
plan; the original Enterprise account cannot read those diagnostic views.
Query target accuracy
is a heuristic target, including at 100, rather than an exactness guarantee.

Exact retrieval changes the previous implicit ANN behavior and can cost more on
large corpora. Approximate cache/memory retrieval can miss a threshold-eligible
row. Native hybrid still ranks bounded vector and Oracle Text candidate sets;
exact vector candidates do not imply exhaustive combined-score ranking.

## Validation

The fresh wheel was built from a new source distribution. Its SDK files were
verified byte-for-byte in a dedicated installed consumer outside the checkout.
All **505 SDK regression tests passed on both Python 3.9 and 3.12**, with no failures/errors/skips. This adds
49 tests for mode selection, validated optimizer literals, integer enums, invalid configuration, hybrid/fallback
paths and dedup independence, and updates existing exact-SQL expectations.
All **57 harness controls passed**, including ten additional index-retirement
safety checks.

The new required `retrieval-modes` profile has seven cases:

| Case | Coverage |
| --- | --- |
| U01 | Create real 4,096-vector IVF/HNSW indexes; default SDK top-10 matches explicit exact results for 24 original plus 96 held-out queries per index. |
| U02 | Explicit IVF probes 24 and HNSW EFSEARCH 500 meet mean recall >=0.90; reuse one connection while alternating probe settings and compare with matching SQL controls. Exact fallback is permitted. |
| U03 | Execute an explicit HNSW target accuracy through the installed SDK. |
| U04 | Real Oracle Text native hybrid with explicit exact and tuned ANN vector candidates. |
| U05 | A real missing Text index raises DRG; fallback preserves exact/ANN settings. |
| U06 | Inclusive and just-above-threshold cache/memory checks on an indexed table. |
| U07 | Find a real one-probe nearest-neighbor miss, then prove exact dedup skips at the inclusive threshold and inserts above it despite ANN retrieval being configured. |

The profile is included in future default certification campaigns and in the
expanded thin/thick and TLS deployment gates. Mean recall >=0.90 is unchanged;
default exact checks require every top-10 set to match exactly. The tests call
the public `TavilyHybridClient` API with custom fixture embeddings/ranking and
foreign retrieval disabled. The separate Cohere profile exercises real defaults.

The new wheel also reuses the original failing IVF fixtures without rebuilding
or modifying them. The comparison explicitly records its old/new SDK source and
wheel hashes, verifies the original evidence and old wheel, checks ownership and
the full FLOAT32 corpus before/after, and preserves schema identities.

| Retained fixture | Default exact, 120 queries | Opt-in probes 16, 120 queries |
| --- | ---: | ---: |
| Oracle 23ai Free 23.9.0.25.7 | 1.000000 | 1.000000; observed exact fallback |
| Oracle 26ai Enterprise Development 26.1.0.24.0 | 1.000000 | 0.917500 |
| Oracle 26ai Free 23.26.1.0.0 | 1.000000 | 0.922500 |

Every default exact query matched, for **360/360 exact top-10 comparisons**.
The 360 opt-in queries met the existing mean-recall gate on each target.
No historical failure was rewritten or counted as a pass.

The new seven-case profile also passed on **Python 3.9.25 with driver 3.4.2**
against Oracle 23ai. The bounded real **Cohere/Tavily/Oracle profile passed all
three cases** on Python 3.12.13 / driver 26.1.0, including a default-embedding
cache hit without Tavily or reranking.

All **24 functional child profiles and 248 cases passed** across the three
recorded server targets in thin/thick mode and wallet-based one-way TLS. This
includes eight complete Oracle profiles, eight index profiles and eight new
retrieval-mode profiles. All four wrong-DN/untrusted-wallet checks passed, with
successful verified TCPS reconnects after each rejection. Server-observed session
lifecycle checks passed. Native mode used Client 23.26.4.0.0.

The final expanded report remains **FAILED**: B01/B02/B03/B05 passed, while B04
failed solely on the unchanged, separate accuracy-95 IVF benchmark. HNSW
mean/minimum recall was 1.0/1.0; IVF was 0.579167/0.2. Both had executed
row-source statistics. Default exact SDK checks and explicitly configured ANN
checks are separate from that failed benchmark. No threshold was lowered.

Final-wheel imports and sync/async HTTP checks passed on Python 3.9–3.14. The
complete seven-case integration profile passed on Python 3.9.25 / driver 3.4.2
against Oracle 23ai, using the final query/lifecycle implementation. SDK
regressions, provider checks, retained-fixture comparisons and the final deployment
report share the same source and wheel. They are standalone validations with
recorded harness revisions, not one successful full certification campaign.

## Evidence and provenance

Artifacts below are beneath the VM harness's private `.artifacts/` directory:

| Evidence | Directory |
| --- | --- |
| Current source-distribution/wheel build | `explicit-retrieval-package-v2-20261110` |
| Final-wheel Python 3.12 regressions, retained fixtures and provider checks | `explicit-retrieval-v2-validation-20261110` |
| Final-wheel Python 3.9 / driver 3.4.2 regressions | `explicit-retrieval-current-python39-regressions-20261110` |
| Retained default exact comparisons | `explicit-retrieval-v2-validation-20261110/retained-{oracle23ai,enterprise26,free26}-exact` |
| Retained opt-in ANN comparisons | `explicit-retrieval-v2-validation-20261110/retained-{oracle23ai,enterprise26,free26}-ann16` |
| Current deployment sweep | `explicit-retrieval-current-deployments-20261110` |
| Current Python 3.9 / older-driver retrieval profile | `explicit-retrieval-current-python39-integration-20261110` |
| Real Cohere/Tavily integration | `explicit-retrieval-v2-validation-20261110/live-cohere` |
| Python 3.9–3.14 imports/HTTP | `explicit-retrieval-current-python-imports-20261110` |
| Final patch, source/harness archives and verification summary | `explicit-retrieval-current-review-20261110` |
| Development diagnostics and retirement audits | `explicit-retrieval-review-20261110` |

Each result directory contains `report.json`, with detailed case outcomes.
The audit checks all current SDK regression files against the actual test copy
used for the installed-wheel tests. `sdk.patch` includes the new untracked
regression file as well as tracked changes; no commit or publication was made.

```text
Previous source: 1dc02d4eef5e473542d267683a17892147376c82f4373cb9fa149a9289937cb4
Current source:  5827229de39a29010bf1bb8c68e3c864236e8cbfdfc7b3cc1d24df53519ec961
Current wheel:   f88bf5f7cfa8312c6cdd3f37f4c7fc39db7c45fcf86dd925770d69b60137c6b3
```

Development attempts are preserved separately. The first deployment sweep was
interrupted after diagnosing a test assertion that required probe budgets to
produce different results even when Oracle chose exact execution. Its
`INTERRUPTED.txt` excludes it from acceptance. The corrected test still checks
query controls and retains the unchanged recall gate.
These standalone results are not a fresh successful full certification campaign.

The second sweep, `explicit-retrieval-deployments-final-20261110`, also remains
FAILED. Its Enterprise thick native-hybrid ANN16 sample had mean recall 0.895833
on 24 queries (minimum 0.5). Ordinary ANN16 on the same index returned exactly
the same 24 sets; its full 120-query mean was 0.910833. This is a probe-budget
limitation for that build/sample, not evidence of a thick-mode or native-hybrid
SQL mismatch. The failed measurement is preserved. The conformance workload now
explicitly uses 24 IVF probes for additional recall margin, informed by the
earlier probe sweep. No SDK default, query set, or recall threshold was changed
to substitute for that failed 16-probe result.

That sweep also exhausted the isolated server's vector memory (ORA-51962) after
many historical test HNSW graphs accumulated. Manifest-verified HNSW indexes
from this task's development runs were retired to recover memory.
Every corpus table, IVF index and original study/certification index was retained.
The exact operations are recorded in the three `development-hnsw-retirement*.json`
audits in the development review directory. Future `retrieval-modes`, `indexes`
and `ann` profiles retire their own newly created HNSW indexes after all
measurements, keeping the corpus tables, recorded plans and IVF evidence.
Retirement requires the recorded database identity, table object ID/comment,
fixed index name, matching table and HNSW subtype; it cannot target application
indexes. No instance memory setting or container image was changed.

Oracle 23ai can expose a HNSW index in `USER_INDEXES` without an entry in
`USER_OBJECTS`. Retirement now treats that object ID as optional and still requires
the verified base-table identity/comment and matching HNSW index relationship.
The minimum-runtime retest confirmed actual retirement with a null catalog object
ID. The supplemental retirement audit is `development-hnsw-retirement-v2.json`.

An interim bound-control build also exposed a plan-caching effect. With session
row-source statistics enabled on an unchanged owned IVF fixture, a statement
first optimized at 24 probes used exact execution and kept that cached exact plan
when bound values changed to 1 or 16. Otherwise identical literal-control queries
accessed 1 and 16 partitions. Exact fallback is permitted, but retaining one plan
made configuration changes less useful for tuning. The final SDK uses strictly
validated canonical integer literals for optimizer settings, with a separate SQL
statement for each setting; vectors and metadata remain bound. The private
`probe-binding-diagnostic-v2.json` retains the executed SQL IDs and actual starts.
Interim build/sweep reports remain separate from the final deployment and
standalone feature validations.

The historical [IVF investigation](IVF_INVESTIGATION.md) documents the original
implicit-search failures. Its accuracy-95 ANN benchmarks remain distinct from
the new exact default and explicitly configured ANN checks. Representative
application embeddings and larger-corpus performance measurements are still
needed before choosing a production ANN setting.

Technical references: [Oracle SELECT fetch/accuracy semantics](https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/SELECT.html),
[IVF query controls](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/approximate-search-using-ivf.html),
[HNSW query controls](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/approximate-search-using-hnsw.html).
