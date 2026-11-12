# First-slice validation — 2026-11-05

Executed on the VM against SDK commit `5c64e52f67bc97d7e2ef2821b856506c5f54d0f6`.
The installed consumer reported `tavily-python 0.8.5`; Python was `3.12.13`,
Oracle Database was `26.1.0.24.0`, and the thin Python driver was `26.1.0`.

| Check | Result | Evidence under `.artifacts/` |
| --- | --- | --- |
| Existing SDK regression suite | 456 passed | `regression.xml` |
| Harness control tests | 14 passed | `harness.xml` |
| Read-only Oracle preflight | F01 passed | `preflight-c3bf49ba6d49/report.json` |
| Isolated package consumer | P01–P03 passed | `package-53e00e1ee60c/report.json` |
| Deterministic Oracle run 1 | E01–E05 passed | `smoke-7228ccd8b80a/report.json` |
| Deterministic Oracle run 2 | E01–E05 passed | `smoke-4706eef5350d/report.json` |
| Deterministic Oracle run 3 | E01–E05 passed | `smoke-4c4421392d82/report.json` |
| Live Tavily → Oracle | E01–E05 passed; one search, one reported credit | `live-fd1680c39667/report.json` |
| Exact-run cleanup | C01 passed | `cleanup-bcb8bf4e97eb/report.json` |
| Repeated cleanup | C01 passed | `cleanup-5325a47a419e/report.json` |

All three successful deterministic runs used the same SDK wheel and unchanged
harness code. The wheel SHA-256 was
`5496ea054939cdfe23ba391cc1458f5407dfc9ce91176901fa8dd8bef14e4a78`.
Each run verified installed SDK files against that wheel before touching Oracle.

The live cold lookup persisted two results. The same-process warm lookup and the
replacement process made zero additional HTTP requests. Independent Oracle sessions
verified committed CLOB content, FLOAT32 vectors, native JSON payloads, provenance,
ownership, and lifecycle metadata. Cache reads left persisted values unchanged.

An initial deterministic attempt (`smoke-79995b3fd72e`) failed in the harness's
JSON-number conversion because Oracle returned `Decimal` values. The harness was
corrected, then the three fresh-table runs above passed. Its failed report remains
available, and only its recorded table was removed to verify cleanup. No SDK source
fix was needed.

The four successful run tables are retained for inspection. Existing schema object
identities were preserved. Use the exact-run cleanup commands in the README when
the retained data is no longer needed. The package consumer is retained outside
the checkout; its location is recorded in the package report.

This validates the first cache/persistence/restart slice. Fixed embeddings were used
for both deterministic and live tests. Cohere defaults, semantic relevance,
hybrid reranking, memory expiry, tenant boundary cases, upsert/deduplication,
Oracle Text/indexes, failures/concurrency, broader SDK endpoints, and the full
declared Python-version range remain subsequent coverage.
