# Tavily Oracle application E2E

This standalone harness tests a built `tavily-python` package across Oracle retrieval,
persistence, memory, cleanup, indexes, failures, concurrent consumers, and SDK workflows.
Its baseline journey is **cold search → committed Oracle persistence → warm cache → process restart**.
It uses the public `TavilyHybridClient` and `OracleDB` APIs, real SDK HTTP transport,
and a real Oracle database. The SDK implementation is not modified by the harness.

See [the first-slice validation record](VALIDATION.md) for executed checks and evidence.
See [remaining coverage and findings](COVERAGE.md) for the extended acceptance results.
See [the November 6 clean certification](CERTIFICATION.md) for the earlier Python >=3.9 campaign.
See [Oracle 23–26 deployment coverage](DEPLOYMENT_COVERAGE.md) for the expanded thin,
thick, wallet/TLS, live-cursor and server-session profiles.
See [the IVF investigation](IVF_INVESTIGATION.md) for retained-index probe sweeps,
held-out recall, and the installed package's implicit approximate retrieval behavior.
See [explicit retrieval implementation](EXPLICIT_RETRIEVAL.md) for the new exact
default, opt-in ANN query controls, and current installed-wheel validation.

The harness requires Python 3.12+ (independent of the SDK's declared Python support).
The initial database profile needs `VECTOR`, native `JSON`, `CREATE TABLE`, and
sufficient quota. Oracle Database testing is restricted to product releases
23 through 26, verified from both version numbers and product banners.
The default driver mode is thin. Exported settings override the
explicit env file; by default it reads the repository's private `.env`.

## Setup

From this directory on the VM:

```bash
/usr/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest
.venv/bin/python run.py preflight
.venv/bin/python run.py package
```

For a clean complete run, use `.venv/bin/python run.py certification`. It freezes
the executable harness/tests/configuration fingerprint, builds a new wheel, and
runs every required profile plus the installed-wheel SDK regression suite. A fresh
campaign never imports historical profile passes or focused retests. Its own
`runs/` directory contains all accepted evidence, including nested runtime runs.
Any revision/package mismatch, failed case, skip, missing profile, or partial retest
prevents certification. Documentation can be updated after a run; it is separate
from the frozen executable fingerprint.

The private env file contains `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`, and
`TAVILY_API_KEY`. Use permissions `600`. No env file is sourced as shell code or
copied to build artifacts. `package` needs no database or API credentials.

The package command prints a report path under `.artifacts/package-<id>/`.
Supply that directory to the subsequent profiles:

```bash
.venv/bin/python run.py smoke --package-run .artifacts/package-<id>
.venv/bin/python run.py live --package-run .artifacts/package-<id>
```

`--env /absolute/private/file.env` selects another private env file for any database
profile. Replace `<id>` with an actual printed run ID. Every command exits nonzero
on failure, a missing required case, or duplicate required case.

## Profiles and evidence

| Profile | Required checks |
| --- | --- |
| `preflight` | F01: read-only owner, connectivity, VECTOR query, and CREATE TABLE privilege checks. Existing objects are inventoried. Quota/native JSON are exercised when the run table is created. |
| `package` | P01: base imports without Oracle/Cohere; P02: driver installation, Oracle imports, `pip check`; P03: wheel built from the sdist with stable SDK-source fingerprints. |
| `smoke` | E01–E05 against real Oracle and a loopback HTTP fixture. No Tavily credits consumed. |
| `live` | Same E01–E05 assertions with one actual Tavily basic-search request, capped at two requested results. Missing credentials or provider failures fail the run. |
| `cleanup --run <directory>` | C01: drop only that run's recorded table after checking database identity, object ID, and ownership comment. |
| `oracle` | O01–O20: retrieval/routing, cache/memory, writes/upserts/deduplication, ownership, partial schemas, cleanup, rollback, failures, and connection ownership. |
| `indexes` | X01–X04: native Oracle Text and fallback, HNSW/IVF creation, valid catalog subtypes, and repeated helper calls. |
| `reliability` | R01–R04: SIGKILL persistence, unavailable listener, concurrent tenant isolation, and bounded repeated workloads. |
| `sdk` | S01–S13: sync/async HTTP endpoints, helpers, research polling/stream closure, headers/errors/keyless behavior, and legacy constructors. No provider credits. |
| `live-sdk` | V01–V08: live sync/async search, extract, map, crawl, feedback, structured research, and streaming. Maximum 50 HTTP requests and two mini research submissions per complete run. |
| `live-cohere` | L01–L03: unmodified default embeddings/reranking with real Cohere, live Tavily, Oracle VECTOR(1024), and a local cache hit. One Tavily request maximum. |
| `compatibility` | Y01: installed-wheel imports and sync/async HTTP on actual Python 3.9–3.14 runtimes. Any required runtime failure fails the profile. |
| `summary` | Collate the latest result for every required case across recorded runs of the same SDK source/wheel. Failed or missing cases keep the aggregate nonzero. |
| `failures` | A01–A03: interrupt active reads/locked writes and withhold a real commit acknowledgement through an owned TCP relay; inspect independent committed state and caller recovery. |
| `contention` | W01–W05: competing existing/absent keys, semantic duplicates, schema uniqueness, and cleanup/write row-lock races. |
| `ann` | N01–N02: 4,096 vectors per index, 24 actual approximate/exact query comparisons, optimizer index paths, recall and measured timings. |
| `retrieval-modes` | U01–U07: default exact top-10 after IVF/HNSW creation, opt-in query controls, native hybrid and Text fallback, indexed cache/memory thresholds and always-exact deduplication. |
| `soak` | T01: four clients, 1,024 seeded rows, 60 seconds, at most 1,000 write/read turns; FD/RSS/connection and throughput/latency evidence. |
| `deployments` | D01–D02: available thin/descriptor/pool/native-client configurations and the full Oracle suite on driver 3.4.2; unavailable deployments remain explicit. |
| `runtime-integration` | J01: the complete Oracle suite and live Cohere defaults on each Python 3.9–3.14 runtime. |
| `certification` | Fresh single-revision campaign of all required profiles plus harness controls and SDK tests from its installed wheel. |

Every extended profile uses the same `--package-run .artifacts/package-<id>` option:

```bash
.venv/bin/python run.py oracle --package-run .artifacts/package-<id>
.venv/bin/python run.py indexes --package-run .artifacts/package-<id>
.venv/bin/python run.py reliability --package-run .artifacts/package-<id>
.venv/bin/python run.py sdk --package-run .artifacts/package-<id>
.venv/bin/python run.py live-sdk --package-run .artifacts/package-<id>
.venv/bin/python run.py live-cohere --package-run .artifacts/package-<id>
.venv/bin/python run.py compatibility --package-run .artifacts/package-<id>
.venv/bin/python run.py summary --package-run .artifacts/package-<id>
```

For a focused live SDK retest, add e.g. `--case V07`. The report explicitly lists
its selected required cases and does not represent a complete profile run. The
summary retains the latest attempt for each ID, even when the latest attempt failed.

The Cohere profile also reads `CO_API_KEY` from the private env file and installs
the optional provider in a separate consumer venv. Compatibility uses `uv python find`
for preinstalled runtimes; provision them with `uv python install 3.9 3.10 3.11 3.12 3.13 3.14`.
Dependency versions are recorded in package/provider reports and matrix installation logs.
`--runtime 3.9` selects an isolated consumer, with driver 3.4.2 by default because
driver 26.1 requires Python >=3.10. Other runtime integrations use driver 26.1.0.
`--driver <version>` makes a driver choice explicit. The SDK's published Python
requirement is not modified by this testing scope.

| Case | Acceptance criterion |
| --- | --- |
| E01 | Cold response comes through the installed SDK's HTTP path and matches the provider response projection. Exactly one HTTP request. |
| E02 | An independent Oracle session sees committed content, vectors, native JSON payload, tenant ownership, provenance, and lifecycle metadata. |
| E03 | A same-process warm lookup returns local content/scores, makes no further HTTP request, and leaves all persisted row values unchanged. |
| E04 | A replacement Python process returns the same committed cache contents without any HTTP request. |
| E05 | Consumer PIDs differ, source fingerprints remain stable, and all pre-existing Oracle object identities remain present. |

The deterministic fixture includes Unicode and content exceeding 4 KB to exercise
CLOB binding. Fixed three-dimensional embeddings intentionally make cache matching
predictable in both profiles. Ranking must not be invoked in this cache journey.
**The live profile validates Tavily HTTP plus Oracle persistence; it does not test
Cohere defaults or certify semantic embedding/reranking quality.**

Build inputs are copied from tracked package sources into a private temporary
directory outside the checkout. A wheel is built from the generated source
distribution and installed into a new consumer venv. Consumers run with `python -I`.
Installed SDK files are compared byte-for-byte with the wheel before database work.
The consumer workspace is retained and its path is recorded in the package report.
Rebuild the package profile whenever SDK-source fingerprints change.

Reports include JUnit XML, JSON case evidence, source/wheel/sdist hashes, dependency
versions, subprocess logs, process IDs, request counts, and committed-row digests.
Consumer source is copied from this harness and its hashes are recorded per run.
Credentials are not included in case evidence; private run directories and logs are
created with owner-only permissions. Treat artifacts as private test evidence.

## Test-object lifecycle

The configured schema may already contain Mastra objects. Each run creates a unique
`TVE_<20 hex characters>` table. The table has a run-specific tenant and ownership
comment; `ownership.json` records the database identity and Oracle object ID. The
harness never clears the schema. It retains its table after success or failure.

After inspecting a specific run, clean it with:

```bash
.venv/bin/python run.py cleanup --run .artifacts/smoke-<id>
.venv/bin/python run.py cleanup --run .artifacts/live-<id>
```

Extended database runs record a set of separately owned tables. The same cleanup
command checks and drops every table in that exact run's manifest set. Interrupted
cleanup can continue from the per-table recorded cleaned state.

Cleanup refuses a changed database identity, recreated table, or wrong ownership
comment. Table drop also removes its own identity/index/LOB dependents. A successfully
recorded cleanup can be repeated. If the process stops between DDL and ownership
recording, preserve the manifest for manual inspection; the harness never guesses
ownership or drops unverified objects.

## Coverage boundaries

Passing functional profiles establish their required scenarios, not every possible
configuration combination or production capacity. The `ann` profile executes
approximate SQL and measures recall. The adapter now explicitly uses `FETCH EXACT`
by default and offers opt-in `FETCH APPROX` with query-time controls. The new
`retrieval-modes` profile checks indexed SDK quality separately from the original
accuracy-95 ANN benchmark. The historical [investigation](IVF_INVESTIGATION.md)
records the old implicit-search shortfall. Index selection is recorded through optimizer plans;
live row-source/session counters require diagnostic privileges, supplied only in
the separate test-owned deployment. Explicit
caller recovery is tested, without claiming automatic reconnect or exactly-once
retries. Failure profiles do not restart the database instance. The optional
isolated-deployment provisioner restarts only its marked test-owned container when
initializing vector memory. MongoDB coverage checks
constructor compatibility; it does not certify a live MongoDB deployment.

The Python 3.8 finding is retained in the historical COVERAGE.md. By explicit user
scope it is excluded from current acceptance, which assumes Python >=3.9. Other
Oracle deployment results are limited to the exact versions and configurations
recorded by each run. Expanded coverage requires a private target inventory;
unavailable targets and failed quality gates cannot count as passes.
