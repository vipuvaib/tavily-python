# Oracle 23–26 deployment coverage

The subsequent [explicit-retrieval implementation](EXPLICIT_RETRIEVAL.md) adds
default exact search and configurable ANN, and includes its new profile in
thin/thick and TLS workloads. The expansion results below describe the earlier
source/wheel; they are retained as historical evidence.

The requested server range is Oracle Database 23 through 26. Preflight rejects
reported versions and product-release banners outside that range. Expanded acceptance requires actual
23ai and 26ai targets; no intermediate release or older patch level is inferred from
those two endpoints. SDK files and published Python requirements are unchanged.

## Expanded acceptance

`deployment-coverage` executes five required gates:

| Gate | Required evidence |
| --- | --- |
| B01 | Actual server versions and banners in range, with both 23ai and 26ai endpoints present. A 26ai banner with version 23.26.x does not satisfy the 23ai endpoint. |
| B02 | Complete 20-case Oracle and four-case index profiles on every target in separate thin and thick processes; thick Client >=23.4; installed consumer mode and source/wheel fingerprints checked. |
| B03 | Server-confirmed TCPS, complete Oracle/index profiles in thin and thick modes, wrong certificate DN rejection, untrusted wallet rejection, and successful connection with the correct wallet after each rejection. |
| B04 | HNSW and IVF approximate/exact comparisons plus executed-cursor row statistics for each owned index. The existing mean recall@10 >=0.90 requirement remains enforced. |
| B05 | Server fixed-view observations of four tagged clients, 96 write/read turns, caller-owned connection lifetime, idle pooled sessions, pool shutdown, and retirement after a private TCP relay interruption. |

`diagnostics` runs G01 independently. Session observations are filtered to a unique
test identifier. Resource snapshots include opened cursors, logical/physical reads
and session PGA/UGA memory. Idle physical pooled sessions are expected until pool
shutdown; released checkouts must have `busy == 0`. This does not establish global
capacity, long-term leak freedom, or the behavior of unrelated applications.

ANN diagnostics use `GATHER_PLAN_STATISTICS`, find the exact executed SQL ID and
child number, require started row sources belonging to the owned index, and retain
`DISPLAY_CURSOR(...,'ALLSTATS LAST')` output containing actual rows. Explain plans
are retained separately. Failed recall measurements also retain their full
comparisons, latencies and live-cursor observations; diagnostics never turn a
failed quality gate into a pass.

## Private target configuration

Keep the JSON inventory and all referenced env files owner-only (mode 600).
The inventory contains paths, never inline passwords:

```json
{
  "targets": [
    {"name": "oracle23", "env": "/private/oracle23.env", "expected_release": 23},
    {"name": "oracle26", "env": "/private/oracle26.env", "expected_release": 26}
  ],
  "client_lib_dir": "/private/instantclient_23_26",
  "tls": {
    "env": "/private/tls.env",
    "expected_release": 26,
    "untrusted_wallet": "/private/untrusted-wallet"
  },
  "diagnostics": {"env": "/private/diagnostics.env", "expected_release": 26}
}
```

An env contains `ORACLE_USER`, `ORACLE_PASSWORD` and `ORACLE_DSN`. Optional
`ORACLE_CONNECT_KWARGS` is JSON containing wallet/configuration directories, wallet
password, certificate DN matching and bounded driver configuration options.
Disabling certificate DN matching is rejected. Thick initialization supplies
`config_dir` before connections and uses parsed driver connection descriptors.
The child process receives its native library path before Python starts.

```bash
.venv/bin/python run.py package
.venv/bin/python run.py deployment-coverage \
  --package-run .artifacts/package-<id> \
  --deployment-config /private/targets.json
.venv/bin/python run.py certification --deployment-config /private/targets.json
```

The expanded certification adds this profile to the existing 16 complete profiles.
Failed profiles remain failures while later profiles and installed-wheel regression
tests continue. A failed package prevents dependent consumers. Historical reports
and focused retests cannot satisfy campaign gates. Target/env SHA256 fingerprints
are recorded and checked for changes during expanded execution.

Schema protection retains pre-existing table/index identities and application
objects. Oracle can replace HNSW ROWID/VID map/index generation objects while the
base vector index remains valid. Only those numeric-generation auxiliary names
under catalog-confirmed vector indexes are recorded as transient. This applies to
both test and application indexes because Oracle performs the same maintenance
for either. Base tables/indexes, unrelated application objects, HNSW checkpoints
and IVF auxiliary objects remain protected by the original identity guard.
Every pre-existing vector index that was VALID must also remain VALID.

## Separate test-owned environment

`isolated_deployment.py start` creates a uniquely labeled Podman container using an
explicit image, separate generated credentials, a 6 GiB memory limit and two
loopback-only published ports. It never attaches to or replaces existing containers.
`configure --state <exact-state.json>` verifies the container ID/name/ownership label,
initializes a 256 MiB vector pool with a container-only database restart, creates
an application schema, and grants diagnostic reads only there.
`configure-tls --state <exact-state.json>` configures the owned listener and wallets.
This fixture exercises wallet-based one-way TLS, not mTLS or managed-cloud wallets.
Private keys, passwords, envs and configuration logs stay inside the ignored,
owner-only artifact directory. Test tables retain their usual ownership manifests.

## Initial November 10 observations

The first new server reports `23.26.1.0.0` with a **26ai Free** banner. It must not
be counted as an Oracle 23ai release based on its leading numeric component.
The native Client reports `23.26.4.0.0`.
The complete Oracle and index profiles passed on this server over TCP and TCPS
using both thin and thick modes. Server-observed session lifecycle checks passed.
HNSW live row-source statistics and recall passed.

The initial IVF sample on this server returned mean recall@10 `0.6208333333`
at requested accuracy 95, below the unchanged 0.90 requirement. A read-only study
of the same index found the same mean at requested accuracy 99 and `0.6625` at 100;
all three had actual IVF row-source evidence. An unhinted query reproduced the
same mean at accuracy 95. Explicit partition-probe counts 8, 16 and 32 produced
mean recall@10 0.7333333333, 0.8875 and 1.0 on that same index. This follow-up
does not replace or pass the original accuracy-95 gate. These are findings for this test
corpus and server. The subsequent [IVF investigation](IVF_INVESTIGATION.md)
also reproduced the quality shortfall through the installed SDK. The earlier
assumption that its `FETCH FIRST` query necessarily returned exact results was
incorrect: Oracle selected the approximate IVF path without an `EXACT` keyword.

The original Oracle 26 endpoint was initially unavailable (ORA-01034/ORA-27101).
Following the user's restoration instruction, its original SID was started using
its existing initialization file and the configured test PDB was opened. The
instance then reported `26.1.0.24.0 OPEN`, with the target PDB `READ WRITE`.

## Executed results, November 10

The final expanded run is `.artifacts/deployment-acceptance-current-20261110/report.json`.
It is **FAILED**, with B01/B02/B03/B05 passed and B04 failed on IVF recall. No
unavailable configuration or failed measurement was counted as a pass.

| Target | Verified product banner | Reported server version | Thin / thick Oracle and index profiles |
| --- | --- | --- | --- |
| Minimum release | Oracle Database 23ai Free | 23.9.0.25.7 | Passed / passed |
| Existing instance | Oracle AI Database 26ai Enterprise, Development | 26.1.0.24.0 | Passed / passed |
| Isolated current-release fixture | Oracle AI Database 26ai Free | 23.26.1.0.0 | Passed / passed |

The expanded-run harness had **43 passing controls**. All **16 functional child profiles,
192 Oracle/index cases** passed across the three targets and wallet TLS. All four
certificate DN/trust rejection checks passed, with successful verified TCPS
connections after each rejection. Native mode used Client 23.26.4.0.0.

Both HNSW and IVF returned live executed-cursor row statistics. On the final
26ai Free sample, HNSW mean/minimum recall@10 was **1.0/1.0**; IVF was
**0.5041666667/0.3**, below the unchanged 0.90 mean requirement. Independent index
builds produced different recall despite the same fixed corpus/query seed. The final
full-campaign Enterprise sample also failed IVF recall: mean **0.8458333333**,
minimum **0.6**, while HNSW passed at 1.0. Functional SDK checks passed, but did
not establish exactness after vector-index creation. A subsequent public-SDK
top-10 comparison reproduced the IVF recall shortfall; see the investigation.
The earlier probe-count study is
retained as a diagnostic experiment, not substituted for either failed gate.

Server observations saw 0 tagged sessions initially, 4 live clients, 0 after
client closure, 1 expected idle pooled session after checkout release, and 0
after pool shutdown. A privately interrupted connection was also observed at
the server and retired to 0. Unrelated users were not included in those counts.

The complete baseline campaign used the installed wheel for **456 passing SDK
regressions**, with no failures/errors/skips. Its 138 Oracle/Cohere case assertions
passed across Python 3.9–3.14. The Python 3.14 Cohere profile's post-run guard
flagged an application HNSW auxiliary generation rollover, despite unchanged
base table/index IDs and VALID index state. After correcting the guard, the
complete Python 3.14 Cohere profile passed in
`.artifacts/cohere314-guard-retest-20261110/report.json`.

Evidence provenance is explicit: the full campaign
`.artifacts/certification-23ai-26ai-final-20261110/report.json` remains FAILED and
predates the last catalog-guard correction. The final expanded run and Python
3.14 retest share the expanded-run harness revision, and every referenced run used
the same SDK source and installed wheel. These separate runs are not presented
as one successful frozen certification campaign.

```text
SDK source SHA256: 1dc02d4eef5e473542d267683a17892147376c82f4373cb9fa149a9289937cb4
Wheel SHA256:      6639eafba9dec5d3d2e18ade7492e0ef1afead232a6325e13ae19cf5217ed21b
Expanded harness:  db9f78378248d59957665739848faf9e0da953604c9c827ecacca57bafd1ae6e
```

The retained-index investigation is complete on Oracle 23ai and both 26ai targets:
16 explicit probes passed the 0.90 mean requirement on the original queries and
96 held-out queries per target. It provided no measured speed advantage over exact
search on this 4,096-vector fixture. Public installed-SDK search also reproduced
the default IVF recall failures. See [IVF_INVESTIGATION.md](IVF_INVESTIGATION.md)
for measurements, reproduction commands, and the retrieval-mode decision now
needed. SDK sources, query behavior and the 0.90 acceptance threshold remain
unchanged. The expanded run and full campaign remain FAILED.
Only the exact recorded server versions are verified; other patch levels, mTLS
and managed-cloud wallet configurations are not inferred from these results.

## Technical references

- [python-oracledb initialization](https://python-oracledb.readthedocs.io/en/stable/user_guide/initialization.html)
- [Oracle DBMS_XPLAN security model](https://docs.oracle.com/en/database/oracle/oracle-database/26/arpls/DBMS_XPLAN.html)
- [Oracle Instant Client download and checksums](https://www.oracle.com/database/technologies/instant-client/linux-x86-64-downloads.html)
- [Oracle vector index hints](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/vector-index-hints.html)
