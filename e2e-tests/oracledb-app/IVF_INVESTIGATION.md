# IVF recall investigation — November 10, 2026

This is the pre-change investigation. The subsequent
[explicit-retrieval implementation](EXPLICIT_RETRIEVAL.md) changes the SDK default
and records new source/wheel validation separately. These original failures and
measurements remain unchanged.

The retained-index probe investigation is complete on Oracle 23ai Free and both
26ai targets. Explicit 16-probe SQL met the existing mean recall@10 >=0.90
requirement on both the original and held-out queries. Those settings have not
been applied to the SDK or substituted for the failed accuracy-95 gates.

**The public installed-SDK comparison also exposed a package-level quality
finding.** Its `FETCH FIRST` query is eligible for implicit approximate search.
All three tested configurations reproduced the IVF recall shortfall through
`TavilyHybridClient.search(..., max_foreign=0)`. Earlier descriptions of that
adapter SQL as necessarily exact were incorrect. Functional index creation and
result-count checks did not establish exact top-10 retrieval.

The SDK source and wheel are unchanged. The existing N02/B04 results and full
certification remain FAILED. This investigation is separate diagnostic evidence.

## Method and controls

The two existing failing Oracle 26ai indexes were reused without rebuilding or
changing them. The minimum-release comparison used a newly marked, test-owned
fixture created by the installed-wheel `ann` profile on Oracle 23ai. Its HNSW
case passed with mean/minimum recall 1.0/1.0; its IVF case failed at 0.570833/0.0.

Each IVF fixture contains the same 4,096 normalized FLOAT32, 32-dimensional
vectors, generated with seed 104729, and has 32 centroids. The study verified
database identity, table ownership comment/object ID, valid vector-index subtype,
auxiliary vector counts, and the complete stored corpus against the seeded
FLOAT32 SHA256. Every original exact top-10 set matched its archived failure
report. Every original accuracy-95 approximate result set also reproduced.

The predeclared settings were exact; accuracy 95 with and without the vector hint;
accuracy 99 and 100; and explicit probe counts 1, 2, 4, 8, 12, 16, 20, 24, 28, 32.
Each setting ran three repetitions with shuffled query/setting order after two
warm-up queries. Timings measure execute plus fetch in a warm, same-session,
shared-host workload. Timing queries omit `GATHER_PLAN_STATISTICS`; separate
diagnostic queries include it. Repetitions are not counted as independent queries.

The smallest passing tested probe count was selected using only the original
24 queries. That choice was saved to `selection.json` before evaluating 96 new
queries generated with independent seed 130363. The selected count was 16 on
every target, and it passed on held-out queries without a subsequent adjustment.
This is the smallest passing count on the tested grid, not a proven global minimum.

No DML, index rebuild, database configuration change or provider request occurs
in the probe study. Ownership, corpus/catalog state, pre-existing schema objects,
SDK source, harness inputs and original evidence hashes are checked afterward.
The additional Oracle 23ai ANN profile created only its manifest-owned fixtures.

## Recall and latency results

All figures below are mean recall@10. The gate is a mean requirement, not a
per-query guarantee: 16 probes still had minimum held-out recall 0.7 on each target.

| Target and verified server version | Accuracy 95, original 24 | Accuracy 95, held-out 96 | 16 probes, original 24 | 16 probes, held-out 96 | 32 probes, held-out 96 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Oracle 23ai Free, 23.9.0.25.7 | 0.570833 | 0.583333 | 0.904167 | 0.928125 | 1.000000 |
| Oracle 26ai Enterprise Development, 26.1.0.24.0 | 0.845833 | 0.847917 | 0.904167 | 0.920833 | 1.000000 |
| Oracle 26ai Free, 23.26.1.0.0 | 0.504167 | 0.593750 | 0.908333 | 0.926042 | 1.000000 |

Held-out execute-plus-fetch latency, milliseconds, measured independently on each
server. These are small synthetic-fixture observations, not production forecasts.

| Target | Exact median / p95 | 16 probes median / p95 | 32 probes median / p95 |
| --- | ---: | ---: | ---: |
| Oracle 23ai Free | 2.005 / 2.461 | 2.106 / 2.693 | 3.324 / 4.107 |
| Oracle 26ai Enterprise | 2.221 / 2.896 | 3.214 / 4.670 | 5.384 / 6.970 |
| Oracle 26ai Free | 2.061 / 2.331 | 2.076 / 2.606 | 3.220 / 3.916 |

16 probes provided no measured latency advantage over exact search at this corpus
size. Probing all 32 matched exact results for all 120 tested queries on each
retained build, but took longer. Neither observation establishes behavior at
production scale or for other vector distributions.

On both Free servers, executed row-source statistics show 5 partitions accessed
at accuracy 95 and 99, 6 at accuracy 100, 16 with explicit probes 16, and 32 with
explicit probes 32. Every diagnosed setting had a started IVF auxiliary-table
path and actual output rows. The original Enterprise account lacks diagnostic
view grants; no privileges were added there. Its probe executions/results are
recorded, but no live row-source claim is made for that account.

Removing the IVF hint reproduced the same accuracy-95 result sets on all 120
queries per target. Increasing the percentage alone did not meet the gate:
accuracy 99 matched 95; accuracy 100 still failed on every target. These findings
point to insufficient automatic search breadth for these fixtures. They do not
establish an Oracle defect or explain all variation between separate index builds.

## Installed package behavior

`ivf_sdk_check.py` verifies the retained wheel hash and every installed SDK file,
then launches the dedicated consumer with Python `-I`, outside the checkout.
The public `TavilyHybridClient.search` call uses real `OracleDB` retrieval,
explicit fixture query embeddings, a score-preserving local reranker and
`max_foreign=0`. Its HTTP session raises if any external request is attempted.

All 120 original/held-out queries ran on each server, for **360 public SDK calls**.
Each returned ten distinct local documents; overlapping result scores matched
their explicit exact cosine scores. Exact-result-set comparison failed on every
target. The original and held-out SDK result sets reproduced the accuracy-95
approximate SQL results, so the synthetic ANN quality finding affects this
installed-package configuration as well.

| Target | SDK mean recall, original 24 | SDK mean recall, held-out 96 | Combined 120 | Exact top-10 check |
| --- | ---: | ---: | ---: | --- |
| Oracle 23ai Free | 0.570833 | 0.583333 | 0.580833 | FAILED |
| Oracle 26ai Enterprise | 0.845833 | 0.847917 | 0.847500 | FAILED |
| Oracle 26ai Free | 0.504167 | 0.593750 | 0.575833 | FAILED |

On both Free targets, the cached plan for the SDK's actual executed SQL uses the
owned IVF centroid/partition tables. This is cached optimizer-plan evidence,
kept separate from the probe study's executed row-source statistics. The SDK SQL
ends with `FETCH FIRST 10 ROWS ONLY`, without `EXACT` or an explicit query probe
count. Oracle's current [SELECT reference](https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/SELECT.html)
documents that omitted APPROX/EXACT permits approximate search when its rules,
available index and optimizer choice allow it. This agrees with the observed
results on the recorded versions; it is not inferred certification of other patches.

Historical index-profile reports containing the label “FETCH FIRST exact retrieval”
are retained unchanged. The current harness annotation and coverage notes now
correct that label. Their original lifecycle/count assertions remain distinct
from this new quality comparison.

## Reproduction and retained evidence

From the VM harness directory
`/scratch/vipuvaib/tavily-final/tavily-python/e2e-tests/oracledb-app`:

```bash
.venv/bin/python ivf_study.py \
  --env /private/target.env \
  --retained-run .artifacts/retained-ann-run \
  --artifact-dir .artifacts/new-ivf-study \
  --require-live

.venv/bin/python ivf_sdk_check.py \
  --env /private/target.env \
  --study .artifacts/new-ivf-study \
  --package-run .artifacts/certification-23ai-26ai-final-20261110/runs/02-package \
  --artifact-dir .artifacts/new-sdk-check
```

Use a new artifact directory for every attempt. Omit `--require-live` only for the
original account without diagnostic grants; unavailable diagnostics stay explicit.
The SDK comparison intentionally exits nonzero when its results differ from exact.
The probe study's `completed` status means its measurements/guards completed; its
individual recall results and the original acceptance failures remain separate.

| Evidence | VM artifact directory, beneath `.artifacts/` |
| --- | --- |
| Oracle 23ai installed-wheel HNSW/IVF acceptance | `ann-oracle23ai-20261110` |
| Oracle 23ai read-only probe study | `ivf-probe-study-oracle23ai-20261110` |
| Enterprise 26ai read-only probe study | `ivf-probe-study-enterprise26-20261110` |
| Free 26ai read-only probe study | `ivf-probe-study-free26-20261110` |
| Oracle 23ai complete SDK comparison | `ivf-sdk-check-oracle23ai-current-20261110` |
| Enterprise 26ai complete SDK comparison | `ivf-sdk-check-enterprise26-current-20261110` |
| Free 26ai complete SDK comparison | `ivf-sdk-check-free26-current-20261110` |

Each directory contains `report.json`. Probe reports include all per-query IDs,
recall, execution timings, live diagnostics, source hashes, before/after catalog
observations and the selected candidate. SDK reports include all 120 comparisons,
wheel verification, source/schema checks and the cached SDK plan where permitted.
The exact probe-study executable snapshot is
`.artifacts/ivf-probe-study-harness-20261110.tar`.
The installed-SDK comparison snapshot is
`.artifacts/ivf-sdk-check-harness-current-20261110.tar`. Their contents were
verified against the corresponding report file hashes. The audit and final
47-control JUnit/log are in `.artifacts/ivf-investigation-20261110/`.

Initial SDK-check development attempts stopped on the first quality mismatch or
an invalid diagnostic bind name (`:table`, subsequently corrected to `:pattern`).
Those attempts remain preserved. Only the three complete `*-current-*` reports
above support the SDK measurements in this note; they fail on the measured
exactness check after source/schema guards complete.

Four added study controls cover owned identifiers/bounded SQL settings, candidate
selection, archived exact-baseline mismatch rejection, and independent reproducible
held-out queries. The harness now has **47 passing controls**. This is not a new
successful full certification campaign.

```text
SDK source SHA256: 1dc02d4eef5e473542d267683a17892147376c82f4373cb9fa149a9289937cb4
Wheel SHA256:      6639eafba9dec5d3d2e18ade7492e0ef1afead232a6325e13ae19cf5217ed21b
Probe harness:     1873dabbda1e538fbb0db3c8fd39ea822e405f35f26e3c359e634023a69ced95
SDK-check harness: 002948f445b8d9631ea9f283a4429180738c29e9f63a88a04338d52e01fcaa81
Final controls:    84964384ed8694d17c4102c27857333e8dbcc689f965130cac8079c6aba3103f
```

## Recommended next implementation

Make retrieval semantics explicit before another certification campaign. For this
small-package baseline, prefer `FETCH EXACT` for predictable top-10 quality. If
approximate retrieval is required, expose a validated query-time search setting
and test it separately against the unchanged recall gate. The measured 16-probe
choice is a candidate for these 32-centroid fixtures, not a universal production
default. Existing index-creation parameters alone do not expose this query-time
probe control.

That implementation should cover ordinary vector retrieval, native-hybrid vector
candidates, and similarity-dedup queries where applicable. Add index-backed SDK
quality checks, including cache-threshold and dedup boundaries, with explicit
expected semantics. Representative application embeddings and larger corpora are
then needed before choosing an ANN performance setting. Finally, run one fresh,
frozen certification campaign; no diagnostic tuning pass can replace N02/B04's
recorded accuracy-95 failures. No SDK change has been made in this investigation.

Oracle references: [approximate search using IVF](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/approximate-search-using-ivf.html),
[target accuracy and heuristic behavior](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/understand-approximate-similarity-search-using-vector-indexes.html),
[IVF creation parameters](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/inverted-file-flat-index-creation-syntax-and-parameters.html).
