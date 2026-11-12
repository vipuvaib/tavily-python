"""Required acceptance IDs; profiles fail if any case is absent or fails."""
ORACLE = {
    "O01": "Hybrid merge, reranking and result limits",
    "O02": "Local-only, foreign-only and disabled routes in all modes",
    "O03": "Fresh cache expiry, timestamp fallback and inclusive threshold",
    "O04": "Cache then memory priority and durable expiry",
    "O05": "Persistence filtering before embeddings without filtering returns",
    "O06": "Disabled writes and custom persistence callbacks",
    "O07": "URL upsert refresh, counters and optional metadata preservation",
    "O08": "Content hash upsert and mixed unkeyed fallback",
    "O09": "Stored and within-batch semantic duplicate suppression",
    "O10": "Deduplication origin and lifecycle eligibility",
    "O11": "Exact tenant scope for allowed-list upsert and deduplication",
    "O12": "Scalar, null, collection and compound ownership validation",
    "O13": "Cleanup preserves fresh, durable, legacy and other-tenant rows",
    "O14": "Automatic cleanup ordering and interval throttling",
    "O15": "Configured column names and timestamp-only cache schema",
    "O16": "Partial lifecycle schema and conservative legacy cleanup",
    "O17": "Real insert and mixed MERGE failures roll back the whole call",
    "O18": "Schema, vector dimension and ownership errors have no writes",
    "O19": "Embedding, reranking and HTTP failures propagate without persistence",
    "O20": "Managed and injected connection ownership and initialization failure",
}
INDEXES = {
    "X01": "Oracle Text creation, combined scoring and identifier-safe queries",
    "X02": "Recognized Text failure falls back; unrelated errors propagate",
    "X03": "HNSW creation, catalog validity and repeated helper invocation",
    "X04": "IVF creation, catalog validity and repeated helper invocation",
}
RELIABILITY = {
    "R01": "Committed data survives abrupt consumer process death",
    "R02": "Unavailable listener fails within a bounded interval and fresh client recovers",
    "R03": "Concurrent owned conversations retain isolated rows and keys",
    "R04": "Bounded repeated workloads retain counts, isolation and latency evidence",
}
SDK = {
    "S01": "Sync search options, request headers and per-call isolation",
    "S02": "Sync extract, crawl, map and partial extraction",
    "S03": "Sync context, Q&A and feedback workflows",
    "S04": "Sync research submission, polling and usage",
    "S05": "Sync research streaming and early close",
    "S06": "Sync HTTP errors, timeout, keyless restrictions and session ownership",
    "S07": "Async search options and concurrent header isolation",
    "S08": "Async extract, crawl, map and partial extraction",
    "S09": "Async context, Q&A, company information and feedback",
    "S10": "Async research submission, polling and usage",
    "S11": "Async research streaming and early close",
    "S12": "Async HTTP errors, timeout, keyless restrictions and client ownership",
    "S13": "Legacy MongoDB constructor compatibility and unsupported Oracle legacy form",
}
LIVE_SDK = {
    "V01": "Live sync search and usage with real results",
    "V02": "Live async search and usage with real results",
    "V03": "Live sync and async extraction",
    "V04": "Live sync and async bounded map",
    "V05": "Live sync and async bounded crawl",
    "V06": "Live feedback tied to test-owned requests",
    "V07": "Live research submission and sync/async completion polling",
    "V08": "Live research stream completes and closes",
}
COHERE = {
    "L01": "Default Cohere embeddings persist compatible Oracle vectors",
    "L02": "Default reranker merges real Tavily and Oracle candidates",
    "L03": "Cohere query embedding permits a cache hit without Tavily or reranking",
}
FAILURES = {
    "A01": "Active Oracle read interruption and independent state reconciliation",
    "A02": "Active locked MERGE interruption and explicit caller recovery",
    "A03": "Committed write with withheld commit acknowledgement and explicit retry",
}
CONTENTION = {
    "W01": "Competing updates to one existing key preserve committed counters",
    "W02": "Competing absent-key MERGEs expose unconstrained duplicates",
    "W03": "Competing semantic dedup calls expose cross-call duplicates",
    "W04": "Database unique key and explicit caller conflict recovery",
    "W05": "Cleanup racing with persistence in both row-lock orderings",
}
ANN = {"N01": "Executed HNSW approximate search, optimizer index path, recall and timings",
       "N02": "Executed IVF approximate search, optimizer index path, recall and timings"}
SOAK = {"T01": "Bounded 60-second larger-data workload with connection, FD, RSS and latency evidence"}
RETRIEVAL = {
    "U01": "Default exact SDK top-10 after IVF and HNSW creation, original and held-out queries",
    "U02": "Explicit IVF probes and HNSW EFSEARCH meet the unchanged mean recall gate",
    "U03": "Explicit HNSW query target accuracy executes through the installed SDK",
    "U04": "Native Oracle Text hybrid preserves explicit exact and approximate vector lookup",
    "U05": "Real missing Text index fallback preserves exact and approximate settings",
    "U06": "Indexed exact cache and memory lookup preserve inclusive score boundaries",
    "U07": "Indexed semantic dedup remains exact with approximate retrieval configured",
}
PROFILES = {"oracle": ORACLE, "indexes": INDEXES, "reliability": RELIABILITY,
            "sdk": SDK, "live-sdk": LIVE_SDK, "live-cohere": COHERE,
            "compatibility": {"Y01": "Testing-scope Python >=3.9 import and HTTP matrix"},
            "failures": FAILURES, "contention": CONTENTION, "ann": ANN, "soak": SOAK,
            "deployments": {"D01": "Available connection configurations with explicit unavailable targets",
                            "D02": "Full Oracle behavior suite using Oracle driver 3.4.2"},
            "runtime-integration": {"J01": "Full Oracle and live Cohere integration across Python >=3.9 runtimes"}}
PROFILES["diagnostics"] = {"G01": "Server-observed tagged sessions, pool release and interrupted connection retirement"}
PROFILES["retrieval-modes"] = RETRIEVAL
PROFILES["deployment-coverage"] = {
    "B01": "Actual Oracle server versions stay within 23–26 and include both endpoints",
    "B02": "Complete Oracle, index and retrieval-mode profiles in thin and vector-capable thick modes",
    "B03": "Wallet TLS workloads and certificate identity/trust rejection",
    "B04": "Executed HNSW and IVF cursor row-source statistics",
    "B05": "Database-observed test session lifecycle and server resource counters",
}
