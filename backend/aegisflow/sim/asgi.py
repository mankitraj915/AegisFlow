"""Call the real FastAPI app in-process, with no network.

Each request runs its own event loop on the simulated process's thread. FastAPI
hands the synchronous route to a worker thread and the loop waits for it, so one
thread is ever running, and the seams it reaches still belong to that process. A
simulated death (Killed, a BaseException) passes straight through Starlette, which
only handles Exception, and out of the request, the way a real SIGKILL ends one.
"""
import asyncio
import json
from dataclasses import dataclass
from typing import Any

from .. import api, runtime

_apps: dict = {}


def the_app():
    """One app per version of create_app, since building one is the slow part and a
    negative control may replace it. The simulator's open_db ignores the path."""
    factory = api.create_app
    if factory not in _apps:
        with runtime.use(runtime.Runtime()):
            _apps[factory] = factory(":memory:")
    return _apps[factory]


@dataclass
class Reply:
    status: int
    body: Any


def call(app, method: str, path: str, body: dict | None = None, headers: dict[str, str] | None = None) -> Reply:
    """Make one request. An unhandled exception in the app is raised here after its
    500 goes out, as ServerErrorMiddleware does for the server to log."""
    payload = json.dumps(body).encode() if body is not None else b""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    raw_headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    raw_headers += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": raw_headers,
        "client": ("sim", 1), "server": ("sim", 80),
    }
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Reply(start["status"], json.loads(content) if content else None)
