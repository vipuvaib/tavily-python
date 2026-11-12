# Extended E2E coverage — 2026-11-05

Target: VM `/scratch/vipuvaib/tavily-final/tavily-python`, SDK commit
`5c64e52f67bc97d7e2ef2821b856506c5f54d0f6`, installed package `0.8.5`.
The SDK wheel is the same artifact validated in the first slice:
`5496ea054939cdfe23ba391cc1458f5407dfc9ce91176901fa8dd8bef14e4a78`.
All Oracle/provider functional cases execute from installed consumer environments.
SDK source files were not changed.

## Results

The 52 new functional acceptance cases have passing evidence. Python compatibility
has one genuine failure on 3.8. Including the original three package checks and five
baseline journey checks, the aggregate is **60/61 passed**, with a nonzero exit code.
The existing SDK regression suite has **456 passed** and harness controls have
**19 passed**.

| Profile | Result | Evidence under `.artifacts/` |
| --- | --- | --- |
| Oracle behavior O01–O20 | 20/20 passed | `oracle-72050f54b8de/report.json` |
| Oracle Text/vector indexes X01–X04 | 4/4 passed | `indexes-eeb342379037/report.json` |
| Reliability R01–R04 | 4/4 passed | `reliability-b67aa57c92da/report.json` |
| SDK contracts S01–S13 | 13/13 passed | `sdk-282e1a2f6058/report.json` |
| Live SDK V01–V08 | 8/8 have passed, including the V07 focused retest | `live-sdk-ad08ea89c916/report.json`, `live-sdk-22bc4554125a/report.json` |
| Default Cohere pipeline L01–L03 | 3/3 passed | `live-cohere-d55797782c86/report.json` |
| Python compatibility Y01 | Failed because Python 3.8 cannot import the SDK | Latest `compatibility-*/report.json` |
| Existing SDK tests | 456 passed | `regression-extended.xml` |
| Harness controls | 19 passed | `harness-extended.xml` |
| Multi-table exact-run cleanup | Passed | `cleanup-9856355f82de/report.json` |

Run `run.py summary --package-run .artifacts/package-53e00e1ee60c` to generate a
fresh JSON/JUnit summary. It uses the latest attempt per case across reports for
this exact wheel/source fingerprint. Selected live retests are explicit; they are
not described as complete profile runs. A missing case, a latest failed attempt,
or a failed required Python runtime prevents a passing aggregate. Reports retain
their per-run harness hashes; these are separate implementation stages, not a
claim that every report used byte-identical harness source.

## Oracle behavior and persistence

`catalog.py` defines every mandatory scenario and `oracle_cases.py` implements them.
The real database cases exercise:

- Hybrid merge/rerank, local-only/foreign-only routes, disabled routes and limits.
- Explicit expiry, timestamp fallback, score thresholds, cache priority, durable
  memory reuse, and miss fallback. Expiry is seeded directly, without long sleeps.
- Persistence score/cap filtering before document embeddings, disabled saves,
  automatic metadata, and callbacks that filter or replace storage documents.
- URL/hash upserts, write counters, omitted optional metadata, unkeyed fallback,
  stored and within-batch cosine deduplication, origin markers and expired rows.
- Scalar, null, allowed-list/set/tuple and compound ownership predicates; whole
  batch validation; exact-owner upsert/dedup behavior and tenant-safe cleanup.
- Lifecycle/legacy cleanup protections, automatic cleanup before routing and
  interval throttling, configurable columns, minimal and partial schemas.
- Real Oracle constraint failures in inserts and mixed insert/MERGE batches,
  rollback visibility from separate connections, and later successful writes.
- Schema/type/dimension errors, embedding/ranking/HTTP failures, and managed versus
  injected connection ownership, including failed initialization.

The first slice's independent-session CLOB/Unicode/native-JSON/vector round trips
and process-restart cache checks remain part of the aggregate baseline.

## Index and reliability evidence

Real Oracle Text retrieval includes the text score, merges candidates by row
identity, sanitizes the query, falls back on a real missing-Text-index query error,
and propagates unrelated vector errors.

HNSW and IVF helpers created real indexes. Oracle reported `VECTOR / VALID`, with
subtypes `INMEMORY_NEIGHBOR_GRAPH_HNSW` and `NEIGHBOR_PARTITIONS_IVF`, respectively.
Repeated helper calls and replacement adapters recognized the existing names.
The adapter's `FETCH FIRST` queries remained usable after index creation.
These checks certify index lifecycle/catalog state, not approximate-query recall
or an exact-result guarantee. `FETCH FIRST` can use approximate vector search.
The November 10 [IVF investigation](IVF_INVESTIGATION.md) records public-SDK recall
failures and corrects the earlier description of this SQL as necessarily exact.

Reliability checks killed a consumer with SIGKILL after commit, then read its data
through a replacement client. A reserved, non-listening loopback port produced a
bounded connection failure, followed by success with a fresh adapter. Four clients
ran 24 isolated write/read turns, then a bounded repeated workload of 48 turns.
Artifacts contain actual elapsed time, error outcomes and p50/p95/p99 latency.
No exactly-once guarantee for concurrent dedup writers or production throughput is
claimed. No administrative Oracle instance restart was performed.

## SDK and live provider evidence

The deterministic SDK profile uses real loopback HTTP with the installed sync and
async clients. It exercises search options, concurrent header isolation, extraction
including partial failures, bounded crawl/map, context/Q&A/company helpers,
feedback, research submission/polling/usage, streamed bytes and early closure,
HTTP errors, timeouts, keyless restrictions, external transport ownership, and
legacy MongoDB constructors. MongoDB itself is not provisioned.

Live Tavily checks passed sync/async search, extract, map, crawl and owned-request
feedback. A streamed mini research task completed. A structured mini research task
was polled by both clients and returned structured content with citations.

The first structured-research attempt was rejected because the test input included
a top-level `type` field. Tavily accepts `properties` and `required` at that level.
The harness and loopback fixture were corrected, and only V07 was repeated against
the live service. The original failed report remains available; summary collation
references the later successful V07 result without rerunning the seven live cases
that already passed.

The Cohere profile uses the SDK's default `embed-english-v3.0` and
`rerank-english-v3.0` callables. It made four real embedding calls and one rerank
call, persisted 1024-dimensional vectors, merged one live Tavily search with local
documents, and then served a cache hit without another Tavily call or rerank.
The optional Cohere dependency lives in a separate consumer environment. Its
resolved versions are recorded in that profile's report.

## Open SDK finding: declared Python 3.8 support

The wheel installs on Python 3.8.20, but `import tavily` fails immediately:

```text
tavily/async_tavily.py:49
    proxies: Optional[dict[str, str]] = None,
TypeError: 'type' object is not subscriptable
```

`setup.py:22` declares `python_requires='>=3.8'`. The same built-in generic appears
in `tavily/tavily.py:48`. This is an installed-package failure, not a database or
test-configuration issue. The matrix runs actual Python interpreters, installs the
same wheel into separate venvs, and tests imports plus sync/async HTTP requests.
Python 3.9–3.14 are included individually; per-runtime versions and logs are in the
latest compatibility report.

Reproduce the finding and retain its nonzero status with:

```bash
.venv/bin/python run.py compatibility --package-run .artifacts/package-53e00e1ee60c
```

A separate SDK change must either make the declared minimum work, including runtime
features such as dictionary union, or correct the declared minimum after deciding
the intended support policy. This coverage work preserves the failure rather than
silently dropping 3.8 or changing SDK behavior.

## Owned data and cleanup

The schema already contained application objects. Each database profile created
only uniquely named, marked `TVE_*` tables and recorded database identity, object
IDs and ownership comments. Pre-existing schema object identities were checked
before and after each functional run. Passing run tables are retained for inspection.
The superseded failed index run was cleaned using the multi-table ownership guard.
Use `run.py cleanup --run <exact-artifact-directory>` to remove another owned run.
