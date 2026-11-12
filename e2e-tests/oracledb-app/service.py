"""Loopback provider fixture: real HTTP, repeatable payloads, no provider calls."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit

RESULTS = [
    {"title": "Alpha document", "url": "https://example.invalid/alpha", "content": "ALPHA Oracle vectors", "score": .95},
    {"title": "Beta document", "url": "https://example.invalid/beta", "content": "BETA Oracle cache", "score": .65},
]


@contextmanager
def provider_server():
    lock = threading.Lock()
    state = {"requests": [], "results": RESULTS, "status": 200, "polls": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, value, content_type="application/json"):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def handle_call(self):
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1048576:
                return self.respond(413, {})
            body = json.loads(self.rfile.read(length)) if length else {}
            if path == "/control":
                with lock:
                    if body.get("reset", True):
                        state.update(requests=[], results=RESULTS, status=200, polls=0)
                    state.update({k: v for k, v in body.items() if k != "reset"})
                    output = {k: v for k, v in state.items() if k != "results"}
                return self.respond(200, output)
            with lock:
                state["requests"].append({"method": self.command, "path": self.path, "body": body,
                    "headers": {key: self.headers.get(key) for key in
                                ("X-Project-ID", "X-Session-Id", "X-Human-Id", "X-Client-Name",
                                 "X-Tavily-Access-Mode") if self.headers.get(key) is not None}})
                status = state["status"]
                results = list(state["results"])
            if body.get("query") == "slow":
                time.sleep(.2)
            if status != 200:
                return self.respond(status, {"detail": {"error": "fixture error"}})
            if path == "/search":
                limit = body.get("max_results", len(results))
                return self.respond(200, {"results": results[:limit], "answer": "Oracle supports vectors.",
                                          "usage": {"credits": 1}, "request_id": "owned-search"})
            if path in ("/extract", "/crawl"):
                return self.respond(200, {"results": [{"url": RESULTS[0]["url"], "raw_content": "Oracle content"}],
                    "failed_results": [{"url": "https://example.invalid/missing", "error": "not found"}],
                    "usage": {"credits": 1}})
            if path == "/map":
                return self.respond(200, {"results": [RESULTS[0]["url"]], "usage": {"credits": 1}})
            if path == "/feedback":
                return self.respond(200, {"success": True, "feedback_id": "owned-feedback"})
            if path == "/research":
                if set(body.get("output_schema") or {}) - {"properties", "required"}:
                    return self.respond(400, {"detail": {"error": "Only properties and required are allowed in output_schema"}})
                if body.get("stream"):
                    return self.respond(200, b'event: message\ndata: {"content":"Oracle vectors"}\n\nevent: complete\ndata: {"status":"completed"}\n\n', "text/event-stream")
                return self.respond(200, {"request_id": "owned-research", "status": "pending"})
            if path == "/research/owned-research":
                with lock:
                    state["polls"] += 1
                    pending = state["polls"] == 1
                return self.respond(202 if pending else 200, {"request_id": "owned-research",
                    "status": "pending" if pending else "completed", "content": "Oracle vectors [1].",
                    "sources": [{"url": RESULTS[0]["url"]}], "usage": {"credits": 1}})
            return self.respond(404, {"detail": {"error": "Unknown fixture endpoint"}})

        do_POST = handle_call
        do_GET = handle_call

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Provider fixture did not stop")
