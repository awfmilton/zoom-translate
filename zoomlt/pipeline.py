"""Glue: audio source -> segmenter -> ASR worker -> (MT) -> caption callback.

Threads
    producer (caller's thread)  reads audio blocks, runs the VAD/segmenter, queues utterances
    worker                      one thread doing Whisper (and MT) decodes, in order

Back-pressure: a partial that is still waiting when the next partial of the same utterance
arrives is replaced, and a final replaces any pending partial of its utterance. The worker can
therefore never fall further behind than "one utterance", however slow the CPU is.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Deque, List, Optional

from zoomlt.asr import AsrEngine
from zoomlt.audio.capture import AudioSource
from zoomlt.config import Config
from zoomlt.mt import Translator
from zoomlt.segmenter import Segmenter, SegmentEvent
from zoomlt.vad import FRAME_MS, StreamingVad

# Back-pressure limits for the decode queue. Past these, the oldest finals are shed: a caption
# that arrives 30 s late is worse than a gap, and without a bound the queue grows forever
# whenever the recogniser is slower than real time.
MAX_QUEUED_FINALS = 2
MAX_QUEUED_AUDIO_S = 20.0

log = logging.getLogger(__name__)


@dataclass
class Caption:
    id: int                          # utterance number; partials and the final share it
    kind: str                        # "partial" | "final"
    english: Optional[str]           # None in mode "transcribe"
    source_text: Optional[str]       # None in mode "translate"
    source_lang: Optional[str]
    language_probability: float
    start_s: float                   # stream time of the utterance
    end_s: float
    lag_s: float                     # wall-clock seconds between the end of speech and this caption
    decode_s: float                  # Whisper time
    mt_s: float = 0.0                # translation time (dual mode)
    reason: str = ""                 # why the utterance ended
    dropped: List[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = "caption"
        return d


@dataclass
class PipelineStats:
    utterances: int = 0
    partials: int = 0
    finals: int = 0
    dropped_segments: int = 0
    decode_total_s: float = 0.0
    audio_total_s: float = 0.0
    backlog: int = 0
    last_lag_s: float = 0.0
    shed: int = 0                    # finals discarded because the recogniser fell too far behind
    stale_partials_skipped: int = 0
    audio_overflows: int = 0         # capture-driver overruns (samples lost before the queue)
    started_at: float = field(default_factory=time.time)

    @property
    def realtime_factor(self) -> float:
        return self.decode_total_s / self.audio_total_s if self.audio_total_s else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["realtime_factor"] = round(self.realtime_factor, 3)
        d["type"] = "status"
        return d


def open_transcript(path: str):
    """Append-only transcript, created readable by the owner only (it is a meeting record)."""
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


class _Job:
    __slots__ = ("event", "utterance_id", "queued_wall", "queued_stream")

    def __init__(self, event: SegmentEvent, utterance_id: int, queued_wall: float, queued_stream: float):
        self.event = event
        self.utterance_id = utterance_id
        self.queued_wall = queued_wall
        self.queued_stream = queued_stream


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        engine: AsrEngine,
        translator: Translator,
        on_caption: Callable[[Caption], None],
        on_status: Optional[Callable[[PipelineStats], None]] = None,
        vad: Optional[Callable] = None,
        status_interval_s: float = 2.0,
    ):
        self.cfg = cfg
        self.engine = engine
        self.translator = translator
        self.on_caption = on_caption
        self.on_status = on_status
        self.status_interval_s = status_interval_s
        self.vad = vad or StreamingVad()
        self.segmenter = Segmenter(self.vad, cfg.vad, cfg.asr.partial_interval_ms)
        self.stats = PipelineStats()
        self._jobs: Deque[_Job] = collections.deque()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._busy = False
        self._base_partial_frames = self.segmenter.partial_interval_frames
        self._utterance_id = 0
        self._partial_shown_for: Optional[int] = None   # utterance id whose partial is on screen
        self._wall_start = time.monotonic()
        self._stream_offset_s = 0.0
        self._transcript = None
        if cfg.server.transcript_path:
            self._transcript = open_transcript(cfg.server.transcript_path)

    # -- lifecycle -------------------------------------------------------------------------------

    def start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="zoomlt-asr", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """Stop soon: pending partials are dropped and only the newest final is still decoded."""
        with self._cv:
            finals = [j for j in self._jobs if j.event.kind != "partial"]
            self._jobs = collections.deque(finals[-1:])
            self.stats.backlog = len(self._jobs)
            self._stop.set()
            self._cv.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=30)
        if self._transcript:
            self._transcript.close()
            self._transcript = None

    # -- producer side ---------------------------------------------------------------------------

    def run(self, source: AudioSource) -> None:
        """Consume the audio source until it ends or stop() is called. Blocks."""
        self.start_worker()
        self._wall_start = time.monotonic()
        last_status = time.monotonic()
        dropped_accounted = 0.0
        try:
            with source:
                for block in source.blocks():
                    if self._stop.is_set():
                        break
                    dropped = source.dropped_seconds()
                    if dropped > dropped_accounted:
                        # keep the stream clock honest so lag_s stays meaningful after an overrun
                        for ev in self.segmenter.skip(dropped - dropped_accounted):
                            self._enqueue(ev)
                        dropped_accounted = dropped
                    for ev in self.segmenter.feed(block):
                        self._enqueue(ev)
                    now = time.monotonic()
                    if self.on_status and now - last_status >= self.status_interval_s:
                        last_status = now
                        self.stats.audio_overflows = getattr(source, "overflows", 0)
                        self._emit_status()
                for ev in self.segmenter.flush():
                    self._enqueue(ev)
        finally:
            self._drain_and_stop()

    def _enqueue(self, ev: SegmentEvent) -> None:
        job = _Job(ev, self._utterance_id, time.monotonic(), self.segmenter.stream_time_s)
        with self._cv:
            if ev.kind == "partial":
                # keep at most one pending partial per utterance: the newest wins
                self._jobs = collections.deque(
                    j for j in self._jobs if not (j.event.kind == "partial" and j.utterance_id == job.utterance_id)
                )
            else:
                self._jobs = collections.deque(j for j in self._jobs if j.utterance_id != job.utterance_id)
                self._utterance_id += 1
                self.stats.utterances += 1
            self._jobs.append(job)
            self._shed_locked()
            self.stats.backlog = len(self._jobs)
            self._cv.notify()

    def _shed_locked(self) -> None:
        """Drop the oldest queued finals once the backlog exceeds the limits (lock held)."""
        while True:
            finals = [j for j in self._jobs if j.event.kind != "partial"]
            queued_s = sum(j.event.duration_s for j in self._jobs)
            if len(finals) <= MAX_QUEUED_FINALS and queued_s <= MAX_QUEUED_AUDIO_S:
                return
            victim = finals[0]
            self._jobs = collections.deque(j for j in self._jobs if j is not victim)
            self.stats.shed += 1
            if self.stats.shed in (1, 5) or self.stats.shed % 25 == 0:
                log.warning("recogniser is behind real time; skipped %d utterance(s) so far. "
                            "Use a smaller model, --no-partials, or --engine nemotron.", self.stats.shed)

    def _drain_and_stop(self) -> None:
        """Let the worker finish what is queued (finals matter), then stop it."""
        with self._cv:
            queued_s = sum(j.event.duration_s for j in self._jobs)
            deadline = time.monotonic() + 60 + 3 * queued_s
            while (self._jobs or self._busy) and not self._stop.is_set() and time.monotonic() < deadline:
                self._cv.wait(timeout=0.2)
        self.stop()

    def _emit_status(self) -> None:
        if self.on_status:
            try:
                self.on_status(self.stats)
            except Exception:  # pragma: no cover
                log.exception("status callback failed")

    # -- worker side -----------------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._jobs and not self._stop.is_set():
                    self._cv.wait(timeout=0.5)
                if not self._jobs:
                    if self._stop.is_set():
                        return
                    continue
                job = self._jobs.popleft()
                self.stats.backlog = len(self._jobs)
                self._busy = True
                self._cv.notify_all()
            try:
                if job.event.kind == "partial" and self._partial_is_stale(job):
                    self.stats.stale_partials_skipped += 1
                else:
                    self._process(job)
            except Exception:
                log.exception("caption job failed")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _partial_is_stale(self, job: _Job) -> bool:
        """A partial that waited longer than the partial interval, or whose utterance already
        ended, would only delay the final behind a full decode of the same audio."""
        if self._stop.is_set():
            return True
        if self._utterance_id != job.utterance_id or not self.segmenter.in_speech:
            return True
        interval_s = max(1.0, self.segmenter.partial_interval_frames * FRAME_MS / 1000.0)
        return (time.monotonic() - job.queued_wall) > interval_s

    def _process(self, job: _Job) -> None:
        ev, mode = job.event, self.cfg.asr.mode
        is_partial = ev.kind == "partial"
        task = "translate" if mode == "translate" else "transcribe"
        audio = ev.audio
        window = int(self.cfg.asr.partial_window_s * 16000)
        if is_partial and window > 0 and not getattr(self.engine, "streaming", False) and audio.shape[0] > window:
            audio = audio[-window:]      # a non-streaming decoder re-reads everything: cap the cost
        res = self.engine.transcribe(audio, task=task, is_partial=is_partial, utterance_id=job.utterance_id)
        self.stats.decode_total_s += res.decode_s
        if is_partial:
            # never ask for partials faster than the recogniser can produce them
            wanted = int(res.decode_s * 1.5 * 1000 / FRAME_MS)
            self.segmenter.partial_interval_frames = max(self._base_partial_frames, wanted)
        self.stats.dropped_segments += len(res.dropped)
        if not is_partial:
            self.stats.audio_total_s += res.duration_s
        if not res.text:
            if not is_partial and self._partial_shown_for == job.utterance_id:
                # This utterance showed partial captions but the final was filtered: retract them.
                self._deliver(self._caption(job, res, english=None, source_text=None, mt_s=0.0, retract=True))
            return

        english: Optional[str] = None
        source_text: Optional[str] = None
        mt_s = 0.0
        if mode == "translate":
            english = res.text
        elif mode == "transcribe":
            source_text = res.text
        else:  # dual
            source_text = res.text
            if res.language == self.cfg.mt.target:
                english = res.text                      # already English: nothing to translate
            elif not is_partial or self.cfg.asr.partial_interval_ms == 0:
                t0 = time.monotonic()
                english = self.translator.translate(res.text, res.language)
                mt_s = time.monotonic() - t0
        self._deliver(self._caption(job, res, english, source_text, mt_s))

    def _caption(self, job: _Job, res, english, source_text, mt_s, retract: bool = False) -> Caption:
        ev = job.event
        # lag = how long after the last captured sample this caption is ready
        wall_now = time.monotonic() - self._wall_start
        lag = max(0.0, wall_now - ev.end_s)
        cap = Caption(
            id=job.utterance_id,
            kind=ev.kind if not retract else "retract",
            english=english,
            source_text=source_text,
            source_lang=res.language,
            language_probability=res.language_probability,
            start_s=ev.start_s,
            end_s=ev.end_s,
            lag_s=round(lag, 3),
            decode_s=round(res.decode_s, 3),
            mt_s=round(mt_s, 3),
            reason=ev.reason,
            dropped=res.dropped,
        )
        return cap

    def _deliver(self, cap: Caption) -> None:
        if cap.kind == "partial":
            self.stats.partials += 1
            self._partial_shown_for = cap.id
        elif cap.kind == "final":
            self.stats.finals += 1
            self.stats.last_lag_s = cap.lag_s
            if self._transcript:
                self._transcript.write(json.dumps(cap.to_dict(), ensure_ascii=False) + "\n")
                self._transcript.flush()
        try:
            self.on_caption(cap)
        except Exception:  # pragma: no cover
            log.exception("caption callback failed")
