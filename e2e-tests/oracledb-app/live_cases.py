"""Bounded live Tavily endpoints and the SDK's unmodified Cohere defaults."""
import asyncio
from datetime import datetime, timezone
import json
import os
import time

import httpx
import requests
from tavily import AsyncTavilyClient, TavilyClient, TavilyHybridClient
from tavily.databases.oracledb import OracleDB
from worker_support import Cases, Harness, check, connection
from catalog import COHERE, LIVE_SDK


class Budget:
    def __init__(self, maximum=50):
        self.maximum, self.calls = maximum, []

    def record(self, method, url):
        from urllib.parse import urlsplit
        parsed = urlsplit(str(url))
        check(parsed.scheme == "https" and parsed.netloc == "api.tavily.com", "Unexpected live host")
        check(len(self.calls) < self.maximum, "Live Tavily request budget exceeded")
        self.calls.append({"method": method, "path": parsed.path})


class BoundedSession(requests.Session):
    def __init__(self, budget):
        super().__init__()
        self.budget = budget

    def send(self, request, **kwargs):
        self.budget.record(request.method, request.url)
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = 30
        return super().send(request, **kwargs)


def live_sdk(config):
    cases, budget = Cases(), Budget()
    session = BoundedSession(budget)
    sync = TavilyClient(os.environ["TAVILY_API_KEY"], session=session)
    shared = {}

    async def call(operation):
        async def record(request):
            budget.record(request.method, request.url)
        async with httpx.AsyncClient(base_url="https://api.tavily.com", timeout=45,
                                     event_hooks={"request": [record]}) as external:
            client = AsyncTavilyClient(os.environ["TAVILY_API_KEY"], client=external)
            return await operation(client)

    def search_sync():
        result = sync.search("Oracle Database AI Vector Search official documentation", max_results=2,
                             search_depth="basic", include_answer=True, include_raw_content=True,
                             include_usage=True, include_domains=["docs.oracle.com"])
        check(0 < len(result["results"]) <= 2 and all(row.get("url") and row.get("content") for row in result["results"]),
              "Live sync search has no usable results")
        shared["sync_search"] = result
        shared["url"] = result["results"][0]["url"]
        return {"result_count": len(result["results"]), "usage": result.get("usage"), "request_id": result.get("request_id")}

    def search_async():
        async def operation(client):
            return await client.search("Oracle Database vector search", max_results=2, search_depth="basic", include_usage=True)
        result = asyncio.run(call(operation))
        check(result["results"] and len(result["results"]) <= 2, "Live async search empty")
        shared["async_search"] = result
        return {"result_count": len(result["results"]), "usage": result.get("usage"), "request_id": result.get("request_id")}

    def extract():
        url = shared.get("url", "https://docs.tavily.com/documentation/about")
        first = sync.extract(url, format="text", include_usage=True, timeout=45)
        second = asyncio.run(call(lambda client: client.extract([url], format="text", include_usage=True, timeout=45)))
        check(all(value["results"] and value["results"][0].get("raw_content") for value in (first, second)),
              "Live extraction has no content")
        return {"usage": [first.get("usage"), second.get("usage")]}

    def map_site():
        options = dict(max_depth=1, max_breadth=1, limit=1, allow_external=False, include_usage=True, timeout=45)
        first = sync.map("https://docs.tavily.com", **options)
        second = asyncio.run(call(lambda client: client.map("https://docs.tavily.com", **options)))
        check(all(0 < len(value["results"]) <= 1 for value in (first, second)), "Live bounded map failed")
        return {"usage": [first.get("usage"), second.get("usage")]}

    def crawl_site():
        options = dict(max_depth=1, max_breadth=1, limit=1, allow_external=False, include_usage=True, timeout=60)
        first = sync.crawl("https://docs.tavily.com", **options)
        second = asyncio.run(call(lambda client: client.crawl("https://docs.tavily.com", **options)))
        check(all(0 < len(value["results"]) <= 1 and value["results"][0].get("raw_content") for value in (first, second)),
              "Live bounded crawl failed")
        return {"usage": [first.get("usage"), second.get("usage")]}

    def feedback():
        first_id = shared.get("sync_search", {}).get("request_id")
        second_id = shared.get("async_search", {}).get("request_id")
        check(first_id and second_id, "Search request IDs unavailable for owned feedback")
        first = sync.feedback(request_id=first_id, agent_score=0, comment="Automated SDK E2E transport validation")
        second = asyncio.run(call(lambda client: client.feedback(request_id=second_id, agent_score=0,
                                            comment="Automated SDK E2E transport validation")))
        check(first.get("success") and second.get("success"), "Live feedback not acknowledged")

    def research():
        schema = {"properties": {"summary": {"type": "string",
                  "description": "One sentence naming Oracle's SQL type for vectors."}}, "required": ["summary"]}
        task = sync.research("According to official Oracle documentation, what is the SQL vector data type called? "
                             "Give one sentence and cite one official source.", model="mini", output_schema=schema, timeout=30)
        check(task.get("request_id"), "Research submission has no ID")
        deadline, polls = time.monotonic() + 180, 0
        response = task
        while time.monotonic() < deadline and polls < 24:
            polls += 1
            response = (sync.get_research(task["request_id"], include_usage=True) if polls % 2 else
                        asyncio.run(call(lambda client: client.get_research(task["request_id"], include_usage=True))))
            if response.get("status") in {"completed", "failed"}:
                break
            time.sleep(5)
        check(response.get("status") == "completed", "Live research did not complete: " + str(response.get("status")))
        content = response.get("content")
        structured = json.loads(content) if isinstance(content, str) else content
        check(isinstance(structured, dict) and isinstance(structured.get("summary"), str) and structured["summary"],
              "Research did not honor structured output")
        check(response.get("sources"), "Research produced no citations")
        return {"request_id": task["request_id"], "polls": polls, "usage": response.get("usage"),
                "source_count": len(response["sources"])}

    def streaming():
        async def operation(client):
            stream = await client.research("Name Oracle's SQL vector data type using one official source. "
                                           "Limit the answer to one sentence.", model="mini", stream=True, timeout=180)
            chunks = []
            try:
                async for chunk in stream:
                    chunks.append(chunk)
                    check(sum(map(len, chunks)) < 262144, "Research stream exceeded bounded size")
            finally:
                await stream.aclose()
            return b"".join(chunks).decode()
        content = asyncio.run(call(operation))
        event_types = []
        for line in content.splitlines():
            if line.startswith("event:"):
                event_types.append(line[6:].strip())
            elif line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    event_types.append("done")
                else:
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        event_types.append(str(event.get("type") or event.get("event") or ""))
        check(content and any(item.lower() in {"done", "complete", "completed", "research.completed"} for item in event_types),
              "Stream lacks a terminal completion event: " + str(sorted(set(event_types))))
        return {"bytes": len(content.encode()), "event_types": sorted(set(event_types))}

    try:
        for (case_id, name), operation in zip(LIVE_SDK.items(), [search_sync, search_async, extract, map_site,
                                                               crawl_site, feedback, research, streaming]):
            if config.get("selected_cases") and case_id not in config["selected_cases"]:
                continue
            cases.case(case_id, name, operation)
        return cases.output(tavily_requests=budget.calls, maximum_requests=budget.maximum,
                            research_budget="at most two mini research submissions")
    finally:
        sync.close()
        session.close()


def live_cohere(config):
    import tavily.hybrid_rag.hybrid_rag as shared_client
    check(shared_client.co is not None, "Default Cohere client did not initialize")
    cases, budget = Cases(), Budget(1)
    calls = {"embed": 0, "rerank": 0}
    original_embed, original_rank = shared_client.co.embed, shared_client.co.rerank
    def embed(*args, **kwargs):
        calls["embed"] += 1
        return original_embed(*args, **kwargs)
    def rank(*args, **kwargs):
        calls["rerank"] += 1
        return original_rank(*args, **kwargs)
    shared_client.co.embed, shared_client.co.rerank = embed, rank
    h = Harness(config, cases)
    owner = config["run_id"] + ":cohere"
    query = "How can Oracle vector search combine local database documents with Tavily web results?"
    documents = ["Oracle vector search retrieves local documents by embedding similarity. Tavily supplies web results.",
                 "Bananas are yellow fruit and banana bread is baked in an oven."]
    session = BoundedSession(budget)
    database = h.db(scope=owner)
    client = TavilyHybridClient(os.environ["TAVILY_API_KEY"], database=database, session=session)
    try:
        def seed():
            vectors = client.embedding_function(documents, "search_document")
            check(len(vectors) == 2 and all(len(vector) == 1024 for vector in vectors), "Default embed dimension mismatch")
            database.persist([{"content": content, "embeddings": vector, "tenant_id": owner,
                               "added_at": datetime.now(timezone.utc).replace(tzinfo=None)}
                              for content, vector in zip(documents, vectors)])
            rows = h.rows(owner)
            check(len(rows) == 2 and all(len(row["EMBEDDINGS"]) == 1024 for row in rows), "Embedded rows not committed")
            return {"dimensions": 1024, "model": "embed-english-v3.0"}
        def merged():
            result = client.search(query, max_results=3, max_local=2, max_foreign=2, save_foreign=True)
            check(1 <= len(result) <= 3 and calls["rerank"] == 1 and len(budget.calls) == 1,
                  "Default hybrid provider pipeline not called")
            check(result[0]["content"] != documents[1], "Irrelevant document ranked first")
            check(len(h.rows(owner)) > 2, "Live foreign results not persisted")
            return {"results": len(result), "origins": sorted({row["origin"] for row in result}),
                    "model": "rerank-english-v3.0"}
        def cached():
            cached_db = h.db(scope=owner, retrieval_mode="freshness_cache", cache_score_threshold=0)
            cached_client = TavilyHybridClient(os.environ["TAVILY_API_KEY"], database=cached_db, session=session)
            before = dict(calls)
            try:
                result = cached_client.search(query, max_results=2, max_foreign=2)
                check(result and all(row["origin"] == "local" for row in result), "Cohere-backed cache missed")
                check(calls["embed"] == before["embed"] + 1 and calls["rerank"] == before["rerank"]
                      and len(budget.calls) == 1, "Cache called Tavily or reranker")
            finally:
                cached_client.close()
        for (case_id, name), operation in zip(COHERE.items(), [seed, merged, cached]):
            cases.case(case_id, name, operation)
        return cases.output(cohere_calls=calls, tavily_requests=budget.calls)
    finally:
        client.close()
        session.close()
        shared_client.co.embed, shared_client.co.rerank = original_embed, original_rank


def run(config):
    return live_cohere(config) if config["profile"] == "live-cohere" else live_sdk(config)
