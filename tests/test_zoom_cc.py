import io
import time
import urllib.parse

import pytest

from zoomlt.config import ZoomCcConfig
from zoomlt.zoom_cc import ZoomCaptionPusher, build_url, parse_seq, seq_url

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

    def __init__(self, body: bytes = b""):
        super().__init__(body)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pusher_sends_in_order_with_increasing_seq_and_coalesces():
    import threading

    sent = []
    first_sent = threading.Event()

    def opener(req, timeout):
        sent.append((req.full_url, req.data.decode("utf-8"), req.get_header("Content-type")))
        first_sent.set()
        return FakeResponse()

    cfg = ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=200, resume_sequence=False)
    p = ZoomCaptionPusher(cfg, opener=opener).start()
    p.push("first  caption")
    assert first_sent.wait(5)  # the worker sends it alone...
    p.push("second")
    p.push("third")            # ...and these two wait out the interval, then go as one request
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

    cfg = ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=10, resume_sequence=False)
    p = ZoomCaptionPusher(cfg, opener=opener).start()
    p.push("a")
    p.stop()
    p2 = ZoomCaptionPusher(cfg, opener=opener).start()
    p2.push("b")
    p2.stop()
    assert p.failed == 1 and p2.failed == 1 and p.sent == 0 and calls["n"] == 2
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


def test_http_error_status_counts_as_failure():
    class Bad(FakeResponse):
        status = 500

    p = ZoomCaptionPusher(ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=10, resume_sequence=False),
                          opener=lambda req, timeout: Bad()).start()
    p.push("x")
    p.stop()
    assert p.failed == 1 and p.sent == 0 and p.seq == 0


def test_sequence_continues_after_a_restart(tmp_path):
    """Zoom drops captions whose seq does not follow the last one it accepted."""
    store = tmp_path / "seq.json"
    seqs = []

    def opener(req, timeout):
        if req.get_method() == "GET":
            raise OSError("this meeting has no seq endpoint")
        seqs.append(int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))["seq"]))
        return FakeResponse()

    cfg = ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=10)
    first = ZoomCaptionPusher(cfg, opener=opener, seq_store=store).start()
    first.push("one")
    first.stop()
    second = ZoomCaptionPusher(cfg, opener=opener, seq_store=store).start()
    second.push("two")
    second.stop()
    assert seqs == [1, 2]
    assert TOKEN not in store.read_text()        # the token itself is never written to disk

    other = ZoomCaptionPusher(ZoomCcConfig(api_token_url=TOKEN + "&x=2", min_interval_ms=10),
                              opener=opener, seq_store=store).start()
    other.push("different meeting")
    other.stop()
    assert seqs == [1, 2, 1]                     # a different token is a different meeting


def test_seq_url_appends_to_the_path_and_keeps_the_signature():
    url = seq_url(TOKEN)
    parts = urllib.parse.urlsplit(url)
    assert parts.path == "/closedcaption/seq"
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert q["signature"] == "sig" and q["id"] == "123"
    assert "seq" not in q


def test_parse_seq_accepts_both_shapes():
    assert parse_seq(" 41 ") == 41
    assert parse_seq('{"seq": 7}') == 7
    assert parse_seq('{"sequence": "9"}') == 9
    assert parse_seq("12") == 12
    assert parse_seq("not a number") is None
    assert parse_seq('{"other": 3}') is None


def test_resume_prefers_zooms_own_sequence_number(tmp_path):
    store = tmp_path / "seq.json"
    posted = []

    def opener(req, timeout):
        if req.get_method() == "GET":
            return FakeResponse(b"40")           # Zoom says it last accepted 40
        posted.append(int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))["seq"]))
        return FakeResponse()

    p = ZoomCaptionPusher(ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=10),
                          opener=opener, seq_store=store).start()
    p.push("hello")
    p.stop()
    assert posted == [41]


def test_resume_falls_back_to_the_local_file_when_zoom_does_not_answer(tmp_path):
    store = tmp_path / "seq.json"
    posted = []

    def opener(req, timeout):
        if req.get_method() == "GET":
            raise OSError("no such endpoint")
        posted.append(int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))["seq"]))
        return FakeResponse()

    cfg = ZoomCcConfig(api_token_url=TOKEN, min_interval_ms=10)
    first = ZoomCaptionPusher(cfg, opener=opener, seq_store=store).start()
    first.push("one")
    first.stop()
    second = ZoomCaptionPusher(cfg, opener=opener, seq_store=store).start()
    second.push("two")
    second.stop()
    assert posted == [1, 2]
