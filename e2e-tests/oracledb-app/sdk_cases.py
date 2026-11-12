"""Public sync/async SDK contracts over real loopback HTTP, from the wheel."""
import asyncio
import json
import warnings

import httpx
import requests
from tavily import AsyncTavilyClient, TavilyClient, TavilyHybridClient
from tavily.errors import (BadRequestError, ForbiddenError, InvalidAPIKeyError,
                           KeylessUnsupportedEndpointError, TimeoutError, UsageLimitExceededError)
from worker_support import Cases, check, control, expect
from catalog import SDK


def run(config):
    cases = Cases()
    endpoint = config["endpoint"]
    warnings.simplefilter("ignore", DeprecationWarning)

    def sync():
        return TavilyClient("fixture-key", api_base_url=endpoint, project_id="default")

    def requests_seen():
        return control(endpoint, reset=False)["requests"]

    def sync_search():
        control(endpoint)
        with sync() as client:
            result = client.search("quoted \"query\"", search_depth="advanced", topic="news", max_results=1,
                max_age_hours=24, include_domains=["example.invalid"], exclude_domains=["excluded.invalid"],
                include_domains_mode="restrict", include_answer=True, include_raw_content="markdown",
                include_images=True, include_usage=True, exact_match=True, fetch_timeout=5,
                cache_fallback=False, language="english", filter_by_language=True,
                project_id="override", session_id="session", human_id="human", client_name="consumer")
            client.search("next")
            first, second = requests_seen()
            check(len(result["results"]) == 1 and result["usage"]["credits"] == 1, "Search contract failed")
            check(first["body"]["max_age_hours"] == 24 and first["body"]["cache_fallback"] is False,
                  "Request options lost")
            check(first["headers"]["X-Project-ID"] == "override" and second["headers"]["X-Project-ID"] == "default",
                  "Header override leaked")
            check(not {"project_id", "session_id", "human_id", "client_name"}.intersection(first["body"]),
                  "Header fields leaked into body")

    def sync_endpoints():
        control(endpoint)
        with sync() as client:
            for urls in ("https://example.invalid/alpha", ["https://example.invalid/alpha", "https://example.invalid/missing"]):
                result = client.extract(urls, format="text", query="Oracle", chunks_per_source=1)
                check(result["results"][0]["raw_content"] and result["failed_results"], "Partial extract failed")
            check(client.crawl("https://example.invalid", max_depth=1, limit=1)["results"], "Crawl failed")
            check(client.map("https://example.invalid", limit=1)["results"], "Map failed")

    def sync_helpers():
        control(endpoint)
        with sync() as client:
            context = json.loads(client.get_search_context("Oracle", max_tokens=1000))
            check(context and "url" in context[0] and "content" in context[0], "Context failed")
            check(client.qna_search("Oracle") == "Oracle supports vectors.", "Q&A failed")
            check(client.feedback(request_id="owned-search", agent_score=1, used_urls=[context[0]["url"]])["success"],
                  "Feedback failed")

    def sync_research():
        control(endpoint)
        with sync() as client:
            task = client.research("Oracle", model="mini", output_schema={"properties": {"summary": {"type": "string"}},
                                                                         "required": ["summary"]})
            check(task["request_id"] == "owned-research", "Submission failed")
            check(client.get_research(task["request_id"])["status"] == "pending", "202 polling failed")
            complete = client.get_research(task["request_id"], include_usage=True)
            check(complete["status"] == "completed" and complete["usage"], "Research completion failed")
            check("include_usage=true" in requests_seen()[-1]["path"], "Usage query parameter missing")
            expect(TypeError, lambda: client.get_research(task["request_id"], include_usage="true"))

    def sync_stream():
        control(endpoint)
        responses = []
        with sync() as client:
            client.session.hooks["response"].append(lambda response, **kwargs: responses.append(response))
            check(b"completed" in b"".join(client.research("Oracle", stream=True, timeout=5)), "Stream data missing")
            generator = client.research("Oracle", stream=True, timeout=5)
            check(next(generator), "Stream yielded no bytes")
            generator.close()
            check(responses[-1].raw.closed, "Early stream close leaked the response")

    def sync_errors():
        for code, kind in [(400, BadRequestError), (401, InvalidAPIKeyError), (403, ForbiddenError),
                           (429, UsageLimitExceededError), (500, requests.HTTPError)]:
            control(endpoint, status=code)
            with sync() as client:
                expect(kind, lambda: client.search("ALPHA"))
        control(endpoint)
        with sync() as client:
            expect(TimeoutError, lambda: client.search("slow", timeout=.02))
        control(endpoint)
        with TavilyClient(api_key="", api_base_url=endpoint) as client:
            client.search("keyless")
            client.extract("https://example.invalid")
            expect(KeylessUnsupportedEndpointError, lambda: client.map("https://example.invalid"))
            check(len(requests_seen()) == 2 and all(row["headers"]["X-Tavily-Access-Mode"] == "keyless"
                                                   for row in requests_seen()), "Keyless contract failed")
        session = requests.Session()
        try:
            client = TavilyClient("fixture-key", api_base_url=endpoint, session=session)
            client.close()
            check(session.post(endpoint + "/search", json={"query": "still usable"}, timeout=5).status_code == 200,
                  "Injected session closed")
        finally:
            session.close()

    async def async_case(number):
        control(endpoint)
        async with AsyncTavilyClient("fixture-key", api_base_url=endpoint, project_id="default") as client:
            if number == 7:
                await asyncio.gather(*(client.search("query-" + str(i), max_age_hours=24, exact_match=True,
                    fetch_timeout=5, cache_fallback=False, language="english", filter_by_language=True,
                    include_usage=True, project_id="project-" + str(i)) for i in range(4)))
                await client.search("next")
                rows = requests_seen()
                check({row["headers"]["X-Project-ID"] for row in rows[:-1]} == {"project-" + str(i) for i in range(4)},
                      "Concurrent headers leaked")
                check(rows[-1]["headers"]["X-Project-ID"] == "default" and
                      all("project_id" not in row["body"] for row in rows), "Default headers contaminated")
            elif number == 8:
                result = await client.extract(["https://example.invalid/alpha", "https://example.invalid/missing"], format="text")
                check(result["results"] and result["failed_results"], "Async partial extract failed")
                check((await client.crawl("https://example.invalid", limit=1, max_depth=1))["results"], "Async crawl failed")
                check((await client.map("https://example.invalid", limit=1))["results"], "Async map failed")
            elif number == 9:
                check(json.loads(await client.get_search_context("Oracle")), "Async context failed")
                check(await client.qna_search("Oracle"), "Async Q&A failed")
                check(len(await client.get_company_info("Oracle", max_results=2)) == 2, "Company helper failed")
                check((await client.feedback(request_id="owned-search", agent_score=1))["success"], "Async feedback failed")
            elif number == 10:
                task = await client.research("Oracle", model="mini", output_schema={"properties": {"summary": {"type": "string"}},
                                                                                     "required": ["summary"]})
                check((await client.get_research(task["request_id"]))["status"] == "pending", "Async pending poll failed")
                done = await client.get_research(task["request_id"], include_usage=True)
                check(done["status"] == "completed" and done["usage"], "Async completion failed")
                try:
                    await client.get_research(task["request_id"], include_usage="true")
                except TypeError:
                    pass
                else:
                    raise AssertionError("Invalid usage flag accepted")
            elif number == 11:
                captured = []
                async def capture(response):
                    captured.append(response)
                client._client.event_hooks["response"].append(capture)
                stream = await client.research("Oracle", stream=True, timeout=5)
                content = b"".join([chunk async for chunk in stream])
                check(b"completed" in content, "Async stream data missing")
                stream = await client.research("Oracle", stream=True, timeout=5)
                check(await anext(stream), "Async stream empty")
                await stream.aclose()
                check(captured[-1].is_closed, "Async stream close leaked response")
            elif number == 12:
                for code, kind in [(400, BadRequestError), (401, InvalidAPIKeyError), (403, ForbiddenError),
                                   (429, UsageLimitExceededError), (500, httpx.HTTPStatusError)]:
                    control(endpoint, status=code)
                    try:
                        await client.search("ALPHA")
                    except kind:
                        pass
                    else:
                        raise AssertionError("Expected HTTP error " + str(code))
                control(endpoint)
                try:
                    await client.search("slow", timeout=.02)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("Async request did not time out")
        if number == 12:
            check(client._client.is_closed, "Owned async client left open")
            control(endpoint)
            async with AsyncTavilyClient(api_key="", api_base_url=endpoint) as keyless:
                await keyless.search("keyless")
                try:
                    await keyless.map("https://example.invalid")
                except KeylessUnsupportedEndpointError:
                    pass
                else:
                    raise AssertionError("Keyless map accepted")
            async with httpx.AsyncClient(base_url=endpoint, trust_env=False) as external:
                injected = AsyncTavilyClient("fixture-key", client=external)
                await injected.close()
                check(not external.is_closed and (await external.get("/map")).status_code == 200,
                      "Injected async client closed")

    def legacy():
        class Collection:
            def list_search_indexes(self):
                return [{"name": "vectors", "type": "vectorSearch", "latestDefinition": {
                    "fields": [{"path": "embeddings", "type": "vector", "similarity": "cosine"}]}}]
            def aggregate(self, pipeline):
                return [{"content": "legacy local", "score": .9, "origin": "local"}]
        embed = lambda texts, kind: [[1., 0., 0.] for _ in texts]
        rank = lambda query, docs, count: docs[:count]
        for construct in (lambda: TavilyHybridClient("fixture-key", "mongodb", Collection(), "vectors",
                                                     embedding_function=embed, ranking_function=rank),
                          lambda: TavilyHybridClient("fixture-key", db_provider="mongodb", collection=Collection(),
                                                     index="vectors", embedding_function=embed, ranking_function=rank)):
            client = construct()
            try:
                check(client.search("Oracle", max_foreign=0)[0]["content"] == "legacy local", "Legacy call failed")
            finally:
                client.close()
        expect(ValueError, lambda: TavilyHybridClient("fixture-key", "oracledb"))

    operations = [sync_search, sync_endpoints, sync_helpers, sync_research, sync_stream, sync_errors]
    operations.extend(lambda number=number: asyncio.run(async_case(number)) for number in range(7, 13))
    operations.append(legacy)
    for (case_id, name), operation in zip(SDK.items(), operations):
        cases.case(case_id, name, operation)
    return cases.output()
