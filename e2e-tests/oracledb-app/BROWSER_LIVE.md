# Live RAG browser testing

Run this guide from the internal `e2e-coverage` branch. Select it from the
repository root with `git switch e2e-coverage` before entering this directory.
The upstream SDK pull request must use `upstream-port`, which excludes this
harness. See the [internal branch workflow](../README.md).

This internal application exercises the installed Tavily SDK, real Cohere
embeddings, Oracle persistence/vector lookup, and text generation through the
official OCI Python SDK with IAM signing. It does not require changes to the
Tavily SDK or a Mastra/Studio installation.

The flow is **browser question → Tavily search → committed Oracle sources → OCI
answer**, followed by repeat-question cache reuse. A fresh OCI request generates
every answer; model answers are not cached. The UI displays source excerpts,
source links, request counts, independently observed row counts/digests, and
the server process/run identifiers.

Cache entries are scoped by the retained run and the SHA-256 of the exact
question. Oracle performs explicit exact cosine vector retrieval over that
scope with a 24-hour TTL. This demonstrates source caching and vector retrieval;
it does not certify cross-question semantic caching, ANN quality, conversational
memory, calculator tools, or semantic accuracy of every generated statement.
The UI displays the model's text verbatim; it does not normalize Markdown or
model-specific citation styles. Source links are independently displayed.

## Private configuration and environment

Keep the env file outside the repository with owner-only permissions. It needs:

- `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN` (application test schema).
- `TAVILY_API_KEY`, `CO_API_KEY`.
- `TAVILY_BROWSER_MODEL` (display identity of the fixed approved OCI model).
- `TAVILY_BROWSER_OCI_CONFIG_FILE`, `TAVILY_BROWSER_OCI_PROFILE`.
- `TAVILY_BROWSER_OCI_AUTH`: `config` for an OCI signing-key profile, or
  `session` for a security-token profile with its signing key.
- `TAVILY_BROWSER_OCI_ENDPOINT`, `TAVILY_BROWSER_OCI_COMPARTMENT_ID`, and
  `TAVILY_BROWSER_OCI_MODEL_ID` for the approved model and matching endpoint region.

IAM signing uses the official SDK and does not use `OCI_GENAI_API_KEY`. The UI
has no model override/picker. Optional `ORACLE_CONNECT_KWARGS` follows the existing
harness's allowlisted driver options. Do not include env contents, signing keys,
wallets, authorization headers or model/compartment OCIDs in screenshots/reports.

Use a separate consumer environment containing the recorded SDK wheel,
`oracledb`, `cohere`, `oci`, and `python-dotenv`. The initial run used the versions
pinned in `browser-requirements.txt`, including Cohere 7.3.1, OCI 2.173.1 and Oracle
driver 26.1.0. Install those with `python -m pip install -r browser-requirements.txt`
and install the recorded fork wheel explicitly; a same-version PyPI package is
not an interchangeable artifact. The server checks that Tavily loads
inside the consumer environment and that every installed SDK file matches the
recorded wheel. The existing package report must still match SDK sources.

## Prepare and serve

From this harness directory, replace the environment, package and run paths:

```bash
DEMO_PY=/absolute/path/to/browser-venv/bin/python
DEMO_ENV=/absolute/private/tavily-browser.env
DEMO_PACKAGE=.artifacts/recorded-package-run
DEMO_RUN=.artifacts/fresh-browser-run

"$DEMO_PY" browser_live.py prepare \
  --env "$DEMO_ENV" --package-run "$DEMO_PACKAGE" --run "$DEMO_RUN"

"$DEMO_PY" browser_live.py serve \
  --env "$DEMO_ENV" --package-run "$DEMO_PACKAGE" --run "$DEMO_RUN" --port 4111
```

`prepare` first sends one bounded real OCI readiness request, then checks Oracle
and creates only a new marked 1024-dimensional test table. It never replaces a
retained run. Startup with `serve` verifies the recorded database/table ownership,
wheel and demo-file hashes. After a restart, use the same env and `--run` directory.
The retained run contains the lease/ownership manifest; an unrelated run ID or a
different database identity cannot be substituted.

Forward the loopback port from a separate terminal on the workstation:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:4111:127.0.0.1:4111 USER@VM
```

Open `http://127.0.0.1:4111/`. Client requests use relative URLs. Neither listener
is exposed publicly. The server validates Host/Origin and a per-process browser
token; requests after a server restart need a page reload. It serializes live
queries and retains request budgets across server restarts: at most six Tavily
searches and sixteen RAG model calls per retained run. Readiness requests are
separate and occur only during explicit preflight/preparation.

## Browser checks

| Check | Expected evidence |
| --- | --- |
| Cold question | One real Tavily request, two committed source rows, live OCI answer and source links. |
| Repeat question | `Oracle cache hit`, zero Tavily requests, one query embedding, no rerank, unchanged persisted row digest, fresh OCI answer. |
| Browser reload | Re-submit the question; cache hit remains and no old answer is restored from browser storage. |
| Server restart | Stop only the demo, restart with the same configuration/run, reload the page and re-submit; a different process ID serves the unchanged cached rows. |
| Different question | No source leakage from the first question; a new scoped cold request persists its own sources. |
| Force refresh | Select refresh and re-submit a cached question; one actual Tavily request occurs and source upserts are independently visible. |
| Invalid question | Whitespace-only input shows an error without a provider request; live-submit buttons disable during requests and recover afterwards. |

The `audit` command independently verifies the retained fixture and reports
current row counts/digest and persisted request budgets. Private evidence contains
`browser-run.json`, `ownership.json`, `browser-events.jsonl`, and one
`query-<request-id>.json` per completed request. Browser screenshots and AX
observations are separate evidence and must correspond to the same run IDs.
Boundary controls run with `python -m pytest tests/test_browser_live.py`.

Stop the demo and workstation tunnel with Ctrl+C after recording evidence. This
does not delete retained data. Only the existing ownership-guarded exact-run
cleanup command may remove its fixture. The internal demo/evidence stays outside
the SDK/upstream change set.
