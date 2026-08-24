# Tavily Python SDK

[![GitHub stars](https://img.shields.io/github/stars/tavily-ai/tavily-python?style=social)](https://github.com/tavily-ai/tavily-python/stargazers)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/tavily-python)](https://pypi.org/project/tavily-python/)
[![License](https://img.shields.io/github/license/tavily-ai/tavily-python)](https://github.com/tavily-ai/tavily-python/blob/main/LICENSE)
[![CI](https://github.com/tavily-ai/tavily-python/actions/workflows/tests.yml/badge.svg)](https://github.com/tavily-ai/tavily-python/actions)

The Tavily Python wrapper allows for easy interaction with the Tavily API, offering the full range of our search, extract, crawl, map, and research functionalities directly from your Python programs. Easily integrate smart search, content extraction, and research capabilities into your applications, harnessing Tavily's powerful features.

## Installing

```bash
pip install tavily-python
```

## Keyless mode

You can try Tavily without an API key. Instantiate `TavilyClient()` with no arguments and the SDK runs in keyless mode against the public Tavily API. Keyless mode supports `search()` and `extract()` only; other methods raise an error explaining that an API key is required.

```python
from tavily import TavilyClient, TavilyKeylessLimitError

# No API key needed
client = TavilyClient()

try:
    response = client.search("Who is Leo Messi?")
    print(response)
except TavilyKeylessLimitError as e:
    # Rate-limit cap reached. The exception carries the human-readable
    # message plus structured fields (code, window, retry_after_seconds,
    # next_actions) returned by the Tavily API.
    print(e)
    print("retry after:", e.retry_after_seconds, "seconds")
```

Keyless usage is rate-limited. For higher limits and the full set of endpoints (including `crawl`, `map`, and `research`), [sign up for a Tavily API key](https://tavily.com), set `TAVILY_API_KEY`, and create `TavilyClient()`.

# Tavily Search

Search lets you search the web for a given query.

## Usage

Below are some code snippets that show you how to interact with our search API. The different steps and components of this code are explained in more detail in the API Methods section further down.

### Getting and printing the full Search API response

```python
from tavily import TavilyClient

# Step 1. Instantiating your TavilyClient
tavily_client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Step 2. Executing a simple search query
response = tavily_client.search("Who is Leo Messi?")

# Step 3. That's it! You've done a Tavily Search!
print(response)
```

### Using exact match to find specific names or phrases

```python
from tavily import TavilyClient

client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Use exact_match=True to only return results containing the exact phrase(s) inside quotes
response = client.search(
    query='"John Smith" CEO Acme Corp',
    exact_match=True
)
print(response)
```

This is equivalent to directly querying our REST API.

### Generating context for a RAG Application

```python
from tavily import TavilyClient

# Step 1. Instantiating your TavilyClient
tavily_client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Step 2. Executing a context search query
context = tavily_client.get_search_context(query="What happened during the Burning Man floods?")

# Step 3. That's it! You now have a context string that you can feed directly into your RAG Application
print(context)
```

This is how you can generate precise and fact-based context for your RAG application in one line of code.

### Getting a quick answer to a question

```python
from tavily import TavilyClient

# Step 1. Instantiating your TavilyClient
tavily_client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Step 2. Executing a Q&A search query
answer = tavily_client.qna_search(query="Who is Leo Messi?")

# Step 3. That's it! Your question has been answered!
print(answer)
```

This is how you get accurate and concise answers to questions, in one line of code. Perfect for usage by LLMs!

# Tavily Extract

Extract web page content from one or more specified URLs.

## Usage

Below are some code snippets that demonstrate how to interact with our Extract API. Each step and component of this code is explained in greater detail in the API Methods section below.

### Extracting Raw Content from Multiple URLs using Tavily Extract API

```python
from tavily import TavilyClient

# Step 1. Instantiating your TavilyClient
tavily_client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Step 2. Defining the list of URLs to extract content from
urls = [
    "https://en.wikipedia.org/wiki/Artificial_intelligence",
    "https://en.wikipedia.org/wiki/Machine_learning",
    "https://en.wikipedia.org/wiki/Data_science",
    "https://en.wikipedia.org/wiki/Quantum_computing",
    "https://en.wikipedia.org/wiki/Climate_change"
] # You can provide up to 20 URLs simultaneously

# Step 3. Executing the extract request
response = tavily_client.extract(urls=urls, include_images=True)

# Step 4. Printing the extracted raw content
for result in response["results"]:
    print(f"URL: {result['url']}")
    print(f"Raw Content: {result['raw_content']}")
    print(f"Images: {result['images']}\n")

# Note that URLs that could not be extracted will be stored in response["failed_results"]
```

# Tavily Crawl

Crawl lets you traverse a website's content starting from a base URL.

> **Note**: Crawl is currently available on an invite-only basis. For more information, please visit [crawl.tavily.com](https://crawl.tavily.com)

## Usage

Below are some code snippets that demonstrate how to interact with our Crawl API. Each step and component of this code is explained in greater detail in the API Methods section below.

### Crawling a website with instructions

```python
from tavily import TavilyClient

# Step 1. Instantiating your TavilyClient
tavily_client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Step 2. Defining the starting URL
start_url = "https://wikipedia.org/wiki/Lemon"

# Step 3. Executing the crawl request with instructions to surface only pages about citrus fruits
response = tavily_client.crawl(
    url=start_url,
    max_depth=3,
    limit=50,
    instructions="Find all pages on citrus fruits"
)

# Step 4. Printing pages matching the query
for result in response["results"]:
    print(f"URL: {result['url']}")
    print(f"Snippet: {result['raw_content'][:200]}...\n")

```

# Tavily Map

Map lets you discover and visualize the structure of a website starting from a base URL.

## Usage

Below are some code snippets that demonstrate how to interact with our Map API. Each step and component of this code is explained in greater detail in the API Methods section below.

### Mapping a website with instructions

```python
from tavily import TavilyClient

# Step 1. Instantiating your TavilyClient
tavily_client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Step 2. Defining the starting URL
start_url = "https://wikipedia.org/wiki/Lemon"

# Step 3. Executing the map request with parameters to focus on specific pages
response = tavily_client.map(
    url=start_url,
    max_depth=2,
    limit=30,
    instructions="Find pages on citrus fruits"
)

# Step 4. Printing the site structure
for result in response["results"]:
    print(f"URL: {result['url']}")

```

# Tavily Research

Research lets you create comprehensive research reports on any topic, with automatic source gathering, analysis, and structured output.

## Usage

Below are some code snippets that demonstrate how to interact with our Research API. Each step and component of this code is explained in greater detail in the API Methods section below.

### Creating a research task and retrieving results

```python
from tavily import TavilyClient

# Step 1. Instantiating your TavilyClient
tavily_client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Step 2. Creating a research task
response = tavily_client.research(
    input="Research the latest developments in AI",
    model="pro",
    citation_format="apa"
)

# Step 3. Retrieving the research results
request_id = response["request_id"]
result = tavily_client.get_research(request_id)

# Step 4. Printing the research report
print(f"Status: {result['status']}")
print(f"Content: {result['content']}")
print(f"Sources: {len(result['sources'])} sources found")
```

### Streaming research results

```python
from tavily import TavilyClient

# Step 1. Instantiating your TavilyClient
tavily_client = TavilyClient(api_key="tvly-YOUR_API_KEY")

# Step 2. Creating a streaming research task
stream = tavily_client.research(
    input="Research the latest developments in AI",
    model="pro",
    stream=True
)

# Step 3. Processing the stream as it arrives
for chunk in stream:
    print(chunk.decode('utf-8'))
```

# Tavily Hybrid RAG

`TavilyHybridClient` combines Tavily search with a local vector database. Tavily supplies fresh external results, while the local database stores reusable context for future retrieval.

The Hybrid RAG API supports multiple database providers without requiring the SDK to own your database connection lifecycle.
The default embedding and reranking helpers use Cohere, so install a provider extra such as `tavily-python[oracle]` / `tavily-python[mongodb]`, install `tavily-python[hybrid]`, or pass custom `embedding_function` and `ranking_function` callables.

| Provider | Best for | Connection pattern |
| --- | --- | --- |
| MongoDB | Existing MongoDB Atlas/local vector-search workflows | Configure `MongoDB`, then pass it as `database`. |
| OracleDB | Oracle-backed vector search, freshness cache, and memory workflows | Configure `OracleDB`, then pass it as `database`. |

MongoDB example:

```python
import os

from pymongo import MongoClient
from tavily import TavilyHybridClient
from tavily.databases.mongodb import MongoDB

mongo = MongoClient(os.environ["MONGO_URI"])
collection = mongo[os.environ["MONGO_DATABASE"]][os.environ["MONGO_COLLECTION"]]

client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=MongoDB(
        collection=collection,
        index=os.environ["MONGO_INDEX"],
    ),
)
```

OracleDB example:

```python
import os

import oracledb
from tavily import TavilyHybridClient
from tavily.databases.oracledb import OracleDB

connection = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    dsn=os.environ["ORACLE_DSN"],
)

client = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=OracleDB(
        connection=connection,
        table_name="TAVILY_DOCUMENTS",
        retrieval_mode="hybrid_search",
    ),
)
```

Provider-specific details live with the provider implementation:

- Architecture decisions, the MongoDB-compatibility boundary, and guidance for adding another provider: [tavily/databases/architecture.md](tavily/databases/architecture.md)
- OracleDB architecture, schema, cache/memory behavior, scoring notes, and notebook guide: [tavily/databases/oracledb/README.md](tavily/databases/oracledb/README.md)
- Runnable Oracle examples: [examples/oracle](examples/oracle)
- General deployment notes: [DEPLOYMENT.md](DEPLOYMENT.md)
- Troubleshooting notes: [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

## Troubleshooting Guide

Common setup issues are usually configuration-related:

| Issue | Resolution |
| --- | --- |
| Missing Tavily API key | Set `TAVILY_API_KEY`, pass `api_key=...`, or provide a pre-authenticated custom session/client. |
| Missing provider dependency | Install the optional provider extra, such as `python -m pip install -e ".[mongodb]"` or `python -m pip install -e ".[oracle]"`. These include Cohere for the default Hybrid RAG embedding/ranking helpers. |
| Missing Cohere dependency or key | Install `python -m pip install -e ".[hybrid]"` and set `CO_API_KEY`, or pass explicit custom embedding/ranking functions. |
| Database connection failure | Verify the connection string, credentials, service name, network access, and local database status. |
| Vector search errors | Confirm the target collection/table has compatible embedding data and the configured vector index/search fields match your schema. |
| Insert/persistence errors | Confirm the provider schema contains the fields required by the options you enabled. |

For Oracle-specific cache, memory, JSON, provenance, vector/text-index, and cleanup details, see [tavily/databases/oracledb/README.md](tavily/databases/oracledb/README.md).

## Contributing Guide

Contributors should preserve the existing public APIs and user workflows. Keep Tavily core APIs, database providers, cache behavior, retrieval behavior, persistence, hybrid search, deduplication, and vector search backward compatible.

Local development basics:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[oracle,mongodb]" pytest
python -m pytest
```

Lightweight quality tooling is configured but intentionally conservative:

```bash
ruff check .
mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for repository structure, coding standards, and pull request guidance.

## Advanced: Custom Session / Client Injection

For enterprise environments that proxy Tavily traffic through an API gateway (e.g., for centralized auth, logging, or policy enforcement), you can pass a pre-configured HTTP session instead of a Tavily API key.

### Sync (custom `requests.Session`)

```python
import requests
from tavily import TavilyClient

# Pre-configure a session with your gateway's auth
session = requests.Session()
session.headers["Authorization"] = "Bearer your-gateway-token"
session.headers["X-Subscription-Key"] = "your-subscription-key"

# No Tavily API key needed — auth is handled by the session
client = TavilyClient(
    session=session,
    api_base_url="https://your-gateway.com/tavily",
)

response = client.search("latest AI research")
```

### Async (custom `httpx.AsyncClient`)

```python
import httpx
from tavily import AsyncTavilyClient

# Pre-configure an async client with your gateway's auth
custom_client = httpx.AsyncClient(
    headers={"Authorization": "Bearer your-gateway-token"},
    base_url="https://your-gateway.com/tavily",
)

client = AsyncTavilyClient(client=custom_client)

response = await client.search("latest AI research")
```

**Key behaviors:**
- If a custom session/client is provided, `api_key` is optional
- Custom session headers take precedence over SDK defaults (e.g., your `Authorization` won't be overwritten)
- Custom session proxies take precedence over SDK proxy settings
- The SDK will **not** close externally-provided sessions — you manage the lifecycle

## Session & User Tracking

`session_id`, `human_id`, and `client_name` are optional identifiers that help attribute requests to a logical session, an end user, and a named client. All three are sent as HTTP headers (`X-Session-Id`, `X-Human-Id`, `X-Client-Name`) and are never persisted in raw form — `human_id` is hashed server-side.

Set them once at client init, or per-call (per-call wins):

```python
import os

from tavily import TavilyClient

# Client-level — applied to every request
client = TavilyClient(
    api_key=os.environ["TAVILY_API_KEY"],
    session_id="my-session-123",
    human_id="internal-user-id-42",
    client_name="my-app",
)

# Per-call override
client.search("hello", session_id="ad-hoc-session")
```

All three are opt-in. Leave them unset and the SDK sends nothing — behavior is identical to earlier versions.

## Documentation

For a complete guide on how to use the different endpoints and their parameters, please head to our [Python API Reference](https://docs.tavily.com/sdk/python/reference).

## Cost

Tavily is free for personal use for up to 1,000 credits per month.
Head to the [Credits & Pricing](https://docs.tavily.com/documentation/api-credits) in our documentation to learn more about how many API credits each request costs.

## License

This project is licensed under the terms of the MIT license.

## Contact

If you are encountering issues while using Tavily, please email us at support@tavily.com. We'll be happy to help you.

If you want to stay updated on the latest Tavily news and releases, head to our [Developer Community](https://community.tavily.com) to learn more!
