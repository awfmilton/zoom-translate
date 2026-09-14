"""Push final English captions into Zoom's own caption stream (optional).

Zoom's "third-party closed captioning" integration gives the meeting host an API token URL
(In-meeting: Captions / Closed Caption menu -> "Copy the API token"). Anything POSTed to that
URL as text/plain shows up as the meeting's captions for every participant. Two query
parameters are required on top of the token URL: `seq`, a counter that must increase by one per
request, and `lang`, the caption language.

This runs on a background thread so a slow Zoom endpoint never delays the on-screen overlay.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from zoomlt.config import ZoomCcConfig

log = logging.getLogger(__name__)


def _seq_store_path() -> Path:
    from zoomlt.mt import default_cache_dir

    return default_cache_dir() / "zoom-caption-seq.json"


def load_seq(token_url: str, store: Optional[Path] = None) -> int:
    """Last sequence number used for this meeting's token (0 when it is a new meeting).

    Zoom drops a caption whose seq is not exactly one past the last it accepted, so a restart
    mid-meeting has to continue the count rather than start again at 1.
    """
    path = store or _seq_store_path()
    try:
        data = json.loads(path.read_text())
        return int(data.get(_token_key(token_url), 0))
    except Exception:
        return 0


def save_seq(token_url: str, seq: int, store: Optional[Path] = None) -> None:
    path = store or _seq_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {}
        data[_token_key(token_url)] = seq
        # keep the file small: only the most recent meetings matter
        for key in list(data)[:-20]:
            data.pop(key, None)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except Exception as exc:  # pragma: no cover - never break captioning over bookkeeping
        log.debug("could not persist the caption sequence: %s", exc)


def _token_key(token_url: str) -> str:
    """A stable id for the meeting that never stores the token itself."""
    return hashlib.sha256(token_url.encode("utf-8")).hexdigest()[:16]


def seq_url(token_url: str) -> str:
    """Zoom's GET endpoint for the last sequence number it accepted for this meeting.

    Zoom's own documentation: "The seq number of the last successful send can be obtained through
    the API: /closedcaption/seq [GET]". It is the caption URL with /seq appended to the path and
    the same signed query string.
    """
    parts = urllib.parse.urlsplit(token_url)
    path = parts.path.rstrip("/") + "/seq"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def parse_seq(body: str) -> Optional[int]:
    """Read a sequence number out of Zoom's reply, which may be bare digits or JSON."""
    body = body.strip()
    if body.isdigit():
        return int(body)
    try:
        data = json.loads(body)
    except Exception:
        return None
    if isinstance(data, int):
        return data
    if isinstance(data, dict):
        for key in ("seq", "sequence", "last_seq"):
            value = data.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def build_url(token_url: str, seq: int, lang: str) -> str:
    parts = urllib.parse.urlsplit(token_url)
    q = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    q["seq"] = str(seq)
    q["lang"] = lang
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(q), parts.fragment))


class ZoomCaptionPusher:
    def __init__(self, cfg: ZoomCcConfig, opener=None, seq_store: Optional[Path] = None):
        if not cfg.api_token_url:
            raise ValueError("zoom_cc.api_token_url is not set")
        self.cfg = cfg
        self.seq_store = seq_store
        self.seq = 0
        self.sent = 0
        self.failed = 0
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._open = opener or urllib.request.urlopen
        self._thread = threading.Thread(target=self._run, name="zoomlt-zoom-cc", daemon=True)
        self._stop = threading.Event()

    def start(self) -> "ZoomCaptionPusher":
        if self.cfg.resume_sequence:
            self.seq = self._resume_seq()
            if self.seq:
                log.info("continuing this meeting's caption sequence at %d", self.seq + 1)
        self._thread.start()
        return self

    def _resume_seq(self) -> int:
        """Where to continue numbering: ask Zoom, and fall back to what we last wrote locally.

        Zoom rejects a caption whose number does not follow the last one it accepted, so a restart
        mid-meeting (or a meeting someone else already captioned) has to pick up the count.
        """
        remote = self._fetch_remote_seq()
        local = load_seq(self.cfg.api_token_url, self.seq_store)
        if remote is None:
            return local
        if local > remote:
            # We posted captions Zoom did not count; trust Zoom, it decides what it accepts.
            log.debug("local caption sequence %d is ahead of Zoom's %d; using Zoom's", local, remote)
        return remote

    def _fetch_remote_seq(self) -> Optional[int]:
        req = urllib.request.Request(seq_url(self.cfg.api_token_url), method="GET")
        try:
            with self._open(req, timeout=self.cfg.timeout_s) as resp:
                body = resp.read()
            text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
            return parse_seq(text)
        except Exception as exc:      # best effort: older meetings and offline tests just skip it
            log.debug("could not read the caption sequence from Zoom: %s", exc)
            return None

    def stop(self, wait: bool = True) -> None:
        """Send what is still queued (the last caption of a meeting matters), then stop."""
        self._q.put(None)
        self._stop.set()
        if wait and self._thread.is_alive():
            pending = self._q.qsize()
            self._thread.join(timeout=self.cfg.timeout_s + 1 + pending * (self.cfg.timeout_s + 0.5))
        if self.cfg.resume_sequence and self.sent:
            save_seq(self.cfg.api_token_url, self.seq, self.seq_store)

    def push(self, text: str) -> None:
        text = " ".join(text.split())
        if text:
            self._q.put(text)

    # -- worker -------------------------------------------------------------------------------

    def _drain(self, first: str) -> str:
        """Coalesce everything that queued up while we were busy into one caption."""
        parts = [first]
        while True:
            try:
                nxt = self._q.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                self._q.put(None)
                break
            parts.append(nxt)
        return " ".join(parts)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            text = self._drain(item)
            self._send(text)
            if self._stop.is_set():
                time.sleep(min(self.cfg.min_interval_ms / 1000.0, 0.3))   # drain quickly at shutdown
            else:
                self._stop.wait(self.cfg.min_interval_ms / 1000.0)

    def _send(self, text: str) -> None:
        # Zoom: seq "must increase by one between posts of new caption data" and must not be
        # increased for retries, so a failed post gives its number back.
        self.seq += 1
        url = build_url(self.cfg.api_token_url, self.seq, self.cfg.lang)
        req = urllib.request.Request(url, data=text.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "text/plain; charset=utf-8"})
        try:
            with self._open(req, timeout=self.cfg.timeout_s) as resp:
                status = getattr(resp, "status", 200)
            if status >= 300:
                raise urllib.error.HTTPError(url, status, "unexpected status", None, None)
            self.sent += 1
            if self.cfg.resume_sequence and self.sent % 5 == 1:
                save_seq(self.cfg.api_token_url, self.seq, self.seq_store)
        except Exception as exc:
            self.seq -= 1
            self.failed += 1
            if self.failed in (1, 10) or self.failed % 100 == 0:
                log.warning("Zoom caption push failed (%d so far): %s", self.failed, exc)
