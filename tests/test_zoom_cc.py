import io
import time
import urllib.parse

import pytest

from zoomlt.config import ZoomCcConfig
from zoomlt.zoom_cc import ZoomCaptionPusher, build_url

TOKEN = "https://wmcc.zoom.us/closedcaption?id=123&ns=abc&expire=86400&sparams=id%2Cns%2Cexpire&signature=sig"


def test_build_url_adds_seq_and_lang_keeping_token_params():
    url = build_url(TOKEN, 7, "en-US")
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert parts.netloc == "wmcc.zoom.us" and parts.path == "/closedcaption"
    assert q["seq"] == "7" and q["lang"] == "en-US"
    assert q["id"] == "123" and q["signature"] == "sig" and q["sparams"] == "id,ns,expire"


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pusher_sends_in_order_with_increasing_seq_and_coalesces():
    sent = []

    def opener(req, timeout):
        sent.append((req.full_url, req.data.decode("utf-8"), req.get_header("Content-type")))
        return FakeResponse()

    cfg = ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=200)
    p = ZoomCaptionPusher(cfg, opener=opener).start()
    p.push("first  caption")
    time.sleep(0.1)            # the worker sends it alone...
    p.push("second")
    p.push("third")            # ...and these two wait out the interval, then go as one request
    time.sleep(0.6)
    p.stop()
    assert p.sent == 2 and p.failed == 0
    seqs = [int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))["seq"]) for u, _, _ in sent]
    assert seqs == list(range(1, len(sent) + 1))
    assert sent[0][1] == "first caption"
    assert sent[1][1] == "second third"                              # coalesced while rate-limited
    assert sent[0][2].startswith("text/plain")


def test_pusher_counts_failures_and_keeps_going():
    calls = {"n": 0}

    def opener(req, timeout):
        calls["n"] += 1
        raise OSError("boom")

    cfg = ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=10)
    p = ZoomCaptionPusher(cfg, opener=opener).start()
    p.push("a")
    time.sleep(0.1)
    p.push("b")
    time.sleep(0.1)
    p.stop()
    assert p.failed == 2 and p.sent == 0 and calls["n"] == 2
    assert p.seq == 0                      # failed posts do not consume sequence numbers


def test_requires_token():
    with pytest.raises(ValueError):
        ZoomCaptionPusher(ZoomCcConfig())


def test_stop_sends_queued_captions_before_exiting():
    sent = []

    def opener(req, timeout):
        sent.append(req.data.decode("utf-8"))
        return FakeResponse()

    p = ZoomCaptionPusher(ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=50), opener=opener).start()
    p.push("one")
    p.push("two")
    p.push("three")
    p.stop()
    assert " ".join(sent).split() == ["one", "two", "three"]
