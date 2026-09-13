import asyncio
import json
import urllib.request

import pytest

from zoomlt.config import ServerConfig
from zoomlt.server import CaptionServer


@pytest.fixture
def server():
    cfg = ServerConfig(port=0, open_browser=False, history=3)
    s = CaptionServer(cfg, app_config={"mode": "translate", "model": "fake"})
    s.start_in_thread()
    assert s.wait_ready(10)
    yield s
    s.shutdown()


def _get(server, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def test_static_overlay_files(server):
    status, ctype, body = _get(server, "/")
    assert status == 200 and ctype.startswith("text/html") and b"zoomlt" in body
    status, ctype, body = _get(server, "/app.js")
    assert status == 200 and "javascript" in ctype and b"WebSocket" in body
    status, ctype, _ = _get(server, "/style.css")
    assert status == 200 and ctype.startswith("text/css")


def test_path_traversal_and_missing_are_404(server):
    for path in ["/../pyproject.toml", "/%2e%2e/pyproject.toml", "/nope.txt", "/../../etc/passwd"]:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError(f"{path} should be 404")


def test_websocket_hello_history_and_broadcast(server):
    async def client():
        from websockets.asyncio.client import connect

        server.publish({"type": "caption", "kind": "final", "id": 0, "english": "one"})
        server.publish({"type": "caption", "kind": "partial", "id": 1, "english": "tw"})
        await asyncio.sleep(0.2)
        async with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello"
            assert hello["config"]["model"] == "fake"
            assert [c["english"] for c in hello["history"]] == ["one"]      # partials are not history
            server.publish({"type": "caption", "kind": "final", "id": 1, "english": "two"})
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert msg["english"] == "two"
            server.publish({"type": "status", "finals": 2, "last_lag_s": 0.4})
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert msg["type"] == "status"
        # history is bounded
        for i in range(10):
            server.publish({"type": "caption", "kind": "final", "id": 10 + i, "english": str(i)})
        await asyncio.sleep(0.2)
        async with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
            hello = json.loads(await ws.recv())
            assert [c["english"] for c in hello["history"]] == ["7", "8", "9"]

    asyncio.run(client())


def test_bind_failure_is_reported_not_swallowed(server):
    clash = CaptionServer(ServerConfig(port=server.port, open_browser=False))
    clash.start_in_thread()
    assert clash.wait_ready(10) is False
    assert clash.error is not None


def test_host_and_origin_checks(server):
    import http.client

    def get(headers):
        c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        c.request("GET", "/", headers=headers)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    assert get({"Host": f"127.0.0.1:{server.port}"}) == 200
    assert get({"Host": f"localhost:{server.port}"}) == 200
    assert get({"Host": "evil.example.com"}) == 403                       # DNS rebinding
    assert get({"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}"}) == 200
    assert get({"Host": f"127.0.0.1:{server.port}", "Origin": "http://attacker.test"}) == 403
    assert get({"Host": f"127.0.0.1:{server.port}"}) == 200


def test_websocket_refuses_foreign_origin(server):
    import asyncio

    async def attempt():
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        try:
            async with connect(f"ws://127.0.0.1:{server.port}/ws", origin="http://attacker.test") as ws:
                await ws.recv()
        except InvalidStatus as exc:
            return exc.response.status_code
        return 101

    assert asyncio.run(attempt()) == 403


def test_lan_mode_requires_token():
    import urllib.error
    import urllib.request

    lan = CaptionServer(ServerConfig(host="0.0.0.0", port=0, open_browser=False))
    lan.start_in_thread()
    assert lan.wait_ready(10) and lan.token
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{lan.port}/", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("token should be required")
        with urllib.request.urlopen(lan.url(), timeout=5) as r:
            assert r.status == 200 and b"zoomlt" in r.read()
        assert "?t=" in lan.url() and lan.url().startswith("http://127.0.0.1:")
    finally:
        lan.shutdown()


def test_static_handler_survives_hostile_paths(server):
    import http.client

    for path in ["/%00", "/" + "a" * 300, "/..%5c..%5cpyproject.toml", "/%2e%2e%2fpyproject.toml"]:
        c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{server.port}"})
        r = c.getresponse()
        r.read()
        c.close()
        assert r.status == 404, path
