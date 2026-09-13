"""Caption overlay server: one port serves the static overlay page and the WebSocket feed.

Messages (JSON, server -> browser)
    {"type": "hello",   "config": {...}, "history": [caption, ...]}
    {"type": "caption", ...Caption fields...}            kind = partial | final | retract
    {"type": "status",  ...PipelineStats fields...}

The pipeline runs on its own threads; `publish()` hops onto the asyncio loop with
call_soon_threadsafe so the WebSocket code never touches sockets from a worker thread.
"""

from __future__ import annotations

import asyncio
import collections
import http
import json
import logging
import mimetypes
import secrets
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Deque, Optional, Set

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from zoomlt.config import ServerConfig

log = logging.getLogger(__name__)

OVERLAY_DIR = Path(__file__).parent / "overlay"
WS_PATH = "/ws"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def is_loopback_host(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.startswith("127.")


class CaptionServer:
    def __init__(self, cfg: ServerConfig, app_config: Optional[dict] = None, overlay_dir: Path = OVERLAY_DIR):
        self.cfg = cfg
        self.app_config = app_config or {}
        self.overlay_dir = overlay_dir
        self.history: Deque[dict] = collections.deque(maxlen=cfg.history)
        self.clients: Set[ServerConnection] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._ready = threading.Event()
        self._stopping: Optional[asyncio.Future] = None
        self.port: int = cfg.port
        self.last_status: Optional[dict] = None
        self.error: Optional[BaseException] = None
        # Off loopback (LAN viewing) every request must carry a per-run token; the feed has no
        # other authentication and the meeting transcript is sensitive.
        self.token: Optional[str] = None if is_loopback_host(cfg.host) else secrets.token_urlsafe(12)

    # -- thread-safe entry points ---------------------------------------------------------------

    def publish(self, message: dict) -> None:
        """Broadcast a JSON message. Safe to call from any thread once the server is running."""
        if self.loop is None or not self._ready.is_set():
            return
        self.loop.call_soon_threadsafe(self._publish_on_loop, message)

    def shutdown(self) -> None:
        loop, stopping = self.loop, self._stopping
        if loop is None or stopping is None:
            return

        def _finish():
            if not stopping.done():
                stopping.set_result(True)

        try:
            loop.call_soon_threadsafe(_finish)
        except RuntimeError:       # loop already closed
            pass

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """True once the server is listening; False on timeout or if it failed to start (see .error)."""
        return self._ready.wait(timeout) and self.error is None

    # -- loop side --------------------------------------------------------------------------------

    def _publish_on_loop(self, message: dict) -> None:
        if message.get("type") == "caption" and message.get("kind") == "final":
            self.history.append(message)
        if message.get("type") == "status":
            self.last_status = message
        if not self.clients:
            return
        data = json.dumps(message, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                asyncio.ensure_future(self._send(ws, data))
            except Exception:  # pragma: no cover
                self.clients.discard(ws)

    async def _send(self, ws: ServerConnection, data: str) -> None:
        try:
            await ws.send(data)
        except Exception:
            self.clients.discard(ws)

    async def _handler(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        try:
            hello = {"type": "hello", "config": self.app_config, "history": list(self.history)}
            if self.last_status:
                hello["status"] = self.last_status
            await ws.send(json.dumps(hello, ensure_ascii=False))
            async for _ in ws:      # the browser sends nothing we act on; keep the socket open
                pass
        finally:
            self.clients.discard(ws)

    def url(self, host: Optional[str] = None) -> str:
        host = host or ("127.0.0.1" if self.cfg.host in ("0.0.0.0", "", "::") else self.cfg.host)
        base = f"http://{host}:{self.port}/"
        return base + (f"?t={self.token}" if self.token else "")

    def _process_request(self, connection: ServerConnection, request: Request) -> Optional[Response]:
        path, _, query = request.path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        host_header = request.headers.get("Host", "")
        host_only = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header.split("]")[0] + "]"
        # DNS-rebinding guard: on loopback only loopback Host headers are served.
        if self.token is None and not is_loopback_host(host_only):
            return _text_response(http.HTTPStatus.FORBIDDEN, "forbidden")
        if self.token is not None and not secrets.compare_digest(params.get("t", ""), self.token):
            return _text_response(http.HTTPStatus.FORBIDDEN, "missing or wrong token; use the URL zoomlt printed")
        origin = request.headers.get("Origin")
        if origin:
            # Browsers do not apply CORS to WebSocket handshakes: refuse other sites' pages.
            if urllib.parse.urlsplit(origin).netloc != host_header:
                return _text_response(http.HTTPStatus.FORBIDDEN, "cross-origin request refused")
        if path == WS_PATH:
            return None                      # proceed with the WebSocket handshake
        return self._static(path)

    def _static(self, path: str) -> Response:
        if path in ("", "/"):
            path = "/index.html"
        rel = urllib.parse.unquote(path.lstrip("/"))
        if "\x00" in rel or len(rel) > 255 or "\\" in rel:
            return _text_response(http.HTTPStatus.NOT_FOUND, "not found")
        try:
            target = (self.overlay_dir / rel).resolve()
            target.relative_to(self.overlay_dir.resolve())
            if not target.is_file():
                raise FileNotFoundError(rel)
        except (OSError, ValueError):
            return _text_response(http.HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        headers = Headers([
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-cache"),
            ("Connection", "close"),
        ])
        return Response(http.HTTPStatus.OK, "OK", headers, body)

    async def serve_forever(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stopping = self.loop.create_future()
        async with serve(
            self._handler,
            self.cfg.host,
            self.cfg.port,
            process_request=self._process_request,
            ping_interval=20,
            ping_timeout=20,
            compression=None,
        ) as server:
            self._server = server
            sockets = server.sockets or []
            if sockets:
                self.port = sockets[0].getsockname()[1]
            self._ready.set()
            log.info("overlay: http://%s:%d/", self.cfg.host, self.port)
            await self._stopping
        self._ready.clear()

    # -- convenience for running in a background thread (tests, CLI) -----------------------------

    def start_in_thread(self) -> threading.Thread:
        def runner():
            try:
                asyncio.run(self.serve_forever())
            except Exception as exc:
                self.error = exc
                log.error("caption server failed: %s", exc)
                self._ready.set()       # wake wait_ready(); it reports False because .error is set

        thread = threading.Thread(target=runner, name="zoomlt-server", daemon=True)
        thread.start()
        return thread


def _text_response(status: http.HTTPStatus, text: str) -> Response:
    body = text.encode("utf-8")
    headers = Headers([("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body))), ("Connection", "close")])
    return Response(status, status.phrase, headers, body)


def caption_message(caption: Any) -> dict:
    return caption.to_dict()
