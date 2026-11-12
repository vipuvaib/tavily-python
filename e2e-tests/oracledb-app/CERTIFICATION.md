# Clean certification — 2026-11-06

The November 10 Oracle 23ai–26ai expansion and its current findings are recorded in
[DEPLOYMENT_COVERAGE.md](DEPLOYMENT_COVERAGE.md). The November 6 campaign below is
preserved as historical evidence for its original scope.

The current testing scope is Python >=3.9. The historical Python 3.8 import failure
is preserved in COVERAGE.md and prior artifacts. The SDK's published Python
requirement and SDK source files have not been changed.

## Reproduce a clean campaign

From the VM's `e2e-tests/oracledb-app` directory:

```bash
.venv/bin/python run.py certification
```

The campaign creates a new artifact directory and freshly builds the source
distribution and wheel. All accepted reports must belong to that campaign and
match its single executable-harness revision, SDK-source fingerprint, and wheel
fingerprint. It does not use `summary`'s historical per-case collation or focused
retests. The installed-wheel SDK regression suite and harness controls must pass
without skips or zero tests. Nested driver/Python reports are checked as well.

The required profiles are preflight, package, smoke, oracle, indexes, reliability,
failures, contention, ann, soak, sdk, live-sdk, live-cohere, compatibility, deployments,
and runtime-integration. Its frozen executable inputs are recorded in
`frozen-harness.json`; documentation is maintained separately from that fingerprint.

## New failure and contention checks

The failure profile uses an owned loopback TCP relay that carries actual Oracle
traffic. It cuts an active delayed SELECT, interrupts a MERGE blocked on a row
lock owned by the test, and deliberately withholds a server commit reply. The
last case observes committed data from a separate direct session while the caller
is still waiting, then cuts the connection and records the caller's failure.
Explicit retries and independently observed write counters demonstrate that
recovery is caller-managed and retries are not exactly-once.

Competing-writer tests cover one existing key, an absent key without a unique
constraint, concurrent semantic deduplication, a database-enforced unique key, and
cleanup racing with persistence in both lock orderings. Existing-key counters must
account for every committed write, fresh acknowledged writes must survive the
cleanup race, and another tenant must remain unchanged. Deliberately synchronized
absent-key and semantic-dedup races expose duplicates where the adapter does not
promise cross-call uniqueness. A unique constraint prevents duplicates but may
require explicit caller retry after ORA-00001.

## ANN and longer workloads

Each native HNSW/IVF recall case seeds 4,096 normalized 32-dimensional vectors,
builds a real index through the adapter, and executes 24 `FETCH APPROX` top-ten
queries paired with `FETCH EXACT`. Mean recall@10 must be >=0.90. Evidence includes
every returned ID comparison, index-build/ingest timings, medians for exact and
approximate queries, and the measured speed ratio. No minimum speedup is assumed.

The matching optimizer plan must select the HNSW scan or IVF centroid/partition
path belonging to the recorded index. The account cannot read `V$SESSION`, so
`DISPLAY_CURSOR` row-source statistics are unavailable; optimizer-path evidence is
kept distinct from live cursor instrumentation. SQL interpretation follows
[Oracle's vector index hint documentation](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/vector-index-hints.html).

The longer bounded soak seeds 1,024 rows across four owners and runs for 60 seconds,
with a maximum 1,000 write/read turns. It records throughput, p50/p95/p99 latency,
owned connection opens/closes, process file descriptors and half-second RSS samples.
Rows, unique keys, counters and tenant isolation must remain consistent. Owned
connections must all close, FDs must return near baseline, and second-half RSS growth
is bounded. Database-wide session leakage is not claimed without observer privileges.

## Deployment and full-runtime scope

Available configurations are tested against the single configured Oracle 26
target: thin Easy Connect, a connect descriptor with OOB disabled and statement
cache disabled, caller-owned pooled connections, and Oracle driver versions 3.4.2
and 26.1.0. The full Oracle behavior suite is rerun for the older driver.

The complete 20-case Oracle profile plus three real Cohere-default cases execute
on each Python 3.9, 3.10, 3.11, 3.12, 3.13 and 3.14 runtime. Python 3.9 uses driver
3.4.2 because driver 26.1 requires Python >=3.10. Provider versions resolved for
each runtime are recorded rather than assumed identical across Python versions.

Only one Oracle server target is configured. Native Client 21.1 connects, but vector
integration reports DPI-1050 requiring Client >=23.4. No vector-capable native
client, wallet/TLS configuration, or other Oracle server version is configured.
Those deployments are explicitly unavailable and are not counted as successful
vector integrations. No credentials or broad database administrative operations
are used to invent additional deployment coverage.

## Evidence lifecycle

All database profiles continue to use unique marked tables and object-ID/database
identity guards. Passing data is retained for inspection. Cleanup accepts the exact
directory containing that run's ownership manifests; nested runtime/driver runs
have their own manifests. The campaign root is not a schema-wide cleanup target.
Private env credentials are never copied into reports, source snapshots or command
arguments. Reports and logs are redacted and stored with owner-only access.

Historical development runs remain available alongside the fresh campaign and must
not be presented as its evidence.

## Completed campaign results

Campaign `certification-47cfb274405f` **passed**. Its authoritative report is
`.artifacts/certification-47cfb274405f/report.json`.

| Check | Result |
| --- | --- |
| Required complete profiles | 16/16 passed |
| Campaign gates | 19/19 passed |
| Final harness controls | 27 passed, no skips |
| SDK regression tests from the campaign's installed wheel | 456 passed, no skips |
| Main and nested profile reports | 29 passed, matching campaign/revision/source/wheel |
| Complete Oracle profile on each Python 3.9–3.14 | 20/20 cases per runtime |
| Live Cohere defaults on each Python 3.9–3.14 | 3/3 cases per runtime |
| Historical or focused reports consumed | None |

Fingerprints:

```text
Executable harness: 686918c72eae5a173af82ba0fc06b08e2fd50ac7af3bffcb996b15ae70ef15da
SDK source:         1dc02d4eef5e473542d267683a17892147376c82f4373cb9fa149a9289937cb4
Campaign wheel:     af2c0cce2808645dd4ddbdb1485de45b7a98579b963e20db9dcc13b3d11fa6a7
```

The campaign used Python 3.9 with driver 3.4.2, and Python 3.10–3.14 with driver
26.1.0. All twelve full Oracle/Cohere runtime child runs passed. Driver 3.4.2 also
passed the full Oracle behavior suite on Python 3.12 in the deployment profile.

ANN measurements from this campaign:

| Index | Mean recall@10 | Minimum recall@10 | Exact median | Approximate median |
| --- | --- | --- | --- | --- |
| HNSW | 1.000 | 1.000 | 1.47 ms | 0.65 ms |
| IVF | 0.979 | 0.900 | 2.09 ms | 3.72 ms |

HNSW was faster and IVF slower than exact retrieval in this small same-session
sample. These measurements are retained as observed, not generalized into an ANN
speedup guarantee. Each index used 4,096 vectors and 24 exact/approximate query pairs.

The 60-second workload completed 968 write/read turns over 1,024 seeded rows in
61.69 seconds, including final verification. Measured throughput was 16.05 turns/s
with deliberate pacing; p95 operation latency was 9.49 ms. Four owned connections
opened and all four closed; process FDs were 4 before and 4 after. RSS changed from
54,341,632 to 58,826,752 bytes, within the configured growth bounds. This is a bounded
consistency/resource test, not saturation capacity or proof of long-term server
session behavior.

The campaign verified actual read/write connection interruption, a committed write
whose acknowledgement was lost, and caller-controlled reconciliation/retry. Existing
key counters accounted for all 32 competing updates. Four deliberately competing
absent-key writers and four semantic-dedup writers each produced four unconstrained
rows, documenting the lack of cross-call uniqueness. The database-constrained case
retained one key with explicit conflict recovery. Both cleanup/write lock orderings
retained the fresh acknowledged row and preserved another owner.

A supplementary read-only check of both known Mastra Oracle configurations and the
Tavily configuration reported Oracle 26.1.0.24.0. No additional server version was
discovered. The alternate Mastra credentials were used only for version discovery,
not for creating or changing objects there. Other server versions, wallet/TLS,
vector-capable thick clients, live cursor statistics, and database-wide session
metrics remain explicitly unverified or unavailable.
