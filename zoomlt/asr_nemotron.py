"""NVIDIA Nemotron 3.5 ASR Streaming 0.6B through sherpa-onnx: the accuracy option for Spanish and Hindi.

Nemotron-3.5-asr-streaming (June 2026, OpenMDW-1.1 licence) is a cache-aware streaming
transducer. On FLEURS it reaches roughly 4 % WER for Spanish and 7 % for Hindi, versus ~38 % for
Whisper `small` and ~22 % for Whisper large-v2 on Hindi, and the int8 ONNX export runs at a
fraction of real time on a laptop CPU. It transcribes only (no English output), so the app pairs
it with the local MT stage (mode "dual").

Supported here: es, hi, en (plus "auto"). Other Indian languages fall back to Whisper.
"""

from __future__ import annotations

import logging
import os
import re
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Sequence

import numpy as np

from zoomlt.asr import AsrEngine, AsrResult
from zoomlt.config import AsrConfig

log = logging.getLogger(__name__)

MODEL_NAME = "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{MODEL_NAME}.tar.bz2"
MODEL_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
SUPPORTED_LANGUAGES = ("es", "hi", "en")
_TAIL_PADDING_S = 0.8          # silence appended before finishing so the last chunk is decoded
_MAX_STREAMS = 4               # utterances kept open for incremental partial decoding


def default_cache_dir(download_root: Optional[str] = None) -> Path:
    if download_root:
        return Path(download_root).expanduser()
    return Path(os.environ.get("ZOOMLT_CACHE", Path.home() / ".cache" / "zoomlt"))


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            raise RuntimeError(f"refusing to extract {member.name!r} outside {dest}")
        if member.issym() or member.islnk():
            raise RuntimeError(f"refusing to extract link {member.name!r}")
    tar.extractall(dest)


def download_file(url: str, dest: Path, label: str = "") -> None:
    """Stream a download to disk with periodic progress logs."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "zoomlt"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last = 0, time.monotonic()
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if time.monotonic() - last > 5:
                last = time.monotonic()
                pct = f"{100 * done / total:.0f}%" if total else f"{done >> 20} MB"
                log.info("downloading %s... %s", label or url, pct)
    tmp.replace(dest)


def ensure_model(download_root: Optional[str] = None) -> Path:
    """Return the model directory, downloading the ~650 MB archive once."""
    cache = default_cache_dir(download_root) / "nemotron"
    model_dir = cache / MODEL_NAME
    if all((model_dir / f).exists() for f in MODEL_FILES):
        return model_dir
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{MODEL_NAME}.tar.bz2"
    log.info("Nemotron 3.5 ASR model not found; downloading ~650 MB to %s (one time)", cache)
    download_file(MODEL_URL, archive, label="Nemotron 3.5 ASR")
    with tarfile.open(archive, "r:bz2") as tar:
        _safe_extract(tar, cache)
    archive.unlink(missing_ok=True)
    if not all((model_dir / f).exists() for f in MODEL_FILES):
        raise RuntimeError(f"model archive did not contain the expected files under {model_dir}")
    return model_dir


# ---------------------------------------------------------------------------------------------
# language guess from the transcript (sherpa-onnx does not expose the model's language tag)
# ---------------------------------------------------------------------------------------------

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_ES_HINTS = {"el", "la", "los", "las", "de", "del", "que", "y", "en", "un", "una", "es", "no", "para", "con",
             "por", "se", "su", "al", "como", "pero", "más", "este", "esta", "está", "son", "lo", "ya", "muy"}
_EN_HINTS = {"the", "and", "of", "to", "is", "in", "that", "it", "for", "with", "on", "are", "this", "we",
             "you", "be", "have", "not", "was", "as", "at", "but", "they", "so", "if", "will", "can", "our"}


def guess_language(text: str, allowed: Sequence[str]) -> Optional[str]:
    """Best-effort source language of a transcript, restricted to `allowed` (empty = any)."""
    allowed = [a for a in allowed if a] or list(SUPPORTED_LANGUAGES)
    if _DEVANAGARI.search(text):
        return "hi" if "hi" in allowed else allowed[0]
    latin = [a for a in allowed if a != "hi"]
    if not latin:
        return allowed[0]
    if len(latin) == 1:
        return latin[0]
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    es = sum(w in _ES_HINTS for w in words)
    en = sum(w in _EN_HINTS for w in words)
    if es == en:
        return "es" if "es" in latin else latin[0]
    return ("es" if es > en else "en") if ("es" in latin and "en" in latin) else latin[0]


# ---------------------------------------------------------------------------------------------

class NemotronEngine(AsrEngine):
    name = "nemotron"
    can_translate = False

    def __init__(self, cfg: AsrConfig):
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError("engine 'nemotron' needs sherpa-onnx: pip install sherpa-onnx") from exc
        self.cfg = cfg
        self.allowed = [l.strip().lower() for l in cfg.languages if l and l.strip()]
        bad = [l for l in self.allowed if l not in SUPPORTED_LANGUAGES]
        if bad:
            raise ValueError(f"engine 'nemotron' supports {SUPPORTED_LANGUAGES}; got {bad}. Use engine 'whisper' for them.")
        self.language_option = self.allowed[0] if len(self.allowed) == 1 else "auto"
        model_dir = ensure_model(cfg.download_root)
        t0 = time.monotonic()
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.int8.onnx"),
            joiner=str(model_dir / "joiner.int8.onnx"),
            num_threads=cfg.cpu_threads or 4,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            enable_endpoint_detection=False,
            provider="cpu",
        )
        log.info("loaded Nemotron 3.5 ASR (sherpa-onnx) in %.1fs; language=%s", time.monotonic() - t0, self.language_option)
        self._lock = threading.Lock()
        self._streams: Dict[int, tuple] = {}   # utterance_id -> (stream, samples_fed)

    # -- streams ----------------------------------------------------------------------------------

    def _new_stream(self):
        stream = self.recognizer.create_stream()
        try:
            stream.set_option("language", self.language_option)
        except Exception as exc:  # pragma: no cover - older sherpa-onnx
            log.debug("set_option(language) unsupported: %s", exc)
        return stream

    def _stream_for(self, utterance_id: Optional[int]):
        if utterance_id is None:
            return self._new_stream(), 0, False
        if utterance_id in self._streams:
            stream, fed = self._streams[utterance_id]
            return stream, fed, True
        if len(self._streams) >= _MAX_STREAMS:
            oldest = min(self._streams)
            self._streams.pop(oldest, None)
        stream = self._new_stream()
        self._streams[utterance_id] = (stream, 0)
        return stream, 0, True

    # -- decode -----------------------------------------------------------------------------------

    def transcribe(self, audio: np.ndarray, task: str, is_partial: bool = False,
                   utterance_id: Optional[int] = None) -> AsrResult:
        if task == "translate":
            raise ValueError("Nemotron transcribes only; use asr.mode = 'dual' (transcribe + local MT)")
        t0 = time.monotonic()
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        rec = self.recognizer
        with self._lock:
            stream, fed, tracked = self._stream_for(utterance_id)
            if audio.shape[0] > fed:
                stream.accept_waveform(16000, audio[fed:])
                fed = audio.shape[0]
            if not is_partial:
                stream.accept_waveform(16000, np.zeros(int(16000 * _TAIL_PADDING_S), dtype=np.float32))
                stream.input_finished()
            while rec.is_ready(stream):
                rec.decode_stream(stream)
            text = rec.get_result(stream).strip()
            if tracked:
                if is_partial:
                    self._streams[utterance_id] = (stream, fed)
                else:
                    self._streams.pop(utterance_id, None)
        text = re.sub(r"\s+", " ", text)
        language = guess_language(text, self.allowed) if text else (self.allowed[0] if len(self.allowed) == 1 else None)
        return AsrResult(
            text=text,
            language=language,
            language_probability=1.0 if len(self.allowed) == 1 else 0.5,
            task="transcribe",
            duration_s=audio.shape[0] / 16000.0,
            decode_s=time.monotonic() - t0,
        )

    def warmup(self) -> None:
        try:
            self.transcribe(np.zeros(16000, dtype=np.float32), task="transcribe")
        except Exception as exc:  # pragma: no cover
            log.debug("warmup failed: %s", exc)
