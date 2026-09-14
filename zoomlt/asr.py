"""Speech recognition engines.

`FasterWhisperEngine` wraps faster-whisper (Whisper on CTranslate2). It is tuned for LIVE use:
greedy decoding, no temperature fallback (retries are where CPU latency explodes), language
restricted to what the user said their colleagues speak, and hallucination filtering on the
per-segment statistics Whisper reports.

`FakeEngine` returns canned results so the pipeline, server and CLI can be tested in milliseconds
without a model download.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from zoomlt.config import AsrConfig

log = logging.getLogger(__name__)

# Whisper model families that were trained for transcription only. Their `translate` output is
# unreliable, so we refuse to run them in translate mode instead of producing gibberish.
_TRANSCRIBE_ONLY_MARKERS = ("turbo", "distil")

#: Languages where Whisper's TRANSCRIPTION is unreliable (wrong script, or a repeated-character
#: loop) even though its direct-to-English TRANSLATION is usable. Measured on real speech for
#: Punjabi; the rest are the languages OpenAI's own FLEURS table scores at or near 100 % word
#: error. In these languages the app should stay in "translate" mode.
WEAK_TRANSCRIPTION_LANGUAGES = {"pa", "te", "ml", "bn", "gu", "kn", "mr"}


@dataclass
class AsrResult:
    text: str
    language: Optional[str]
    language_probability: float
    task: str                       # "transcribe" | "translate"
    duration_s: float
    decode_s: float
    dropped: List[str] = field(default_factory=list)   # segments removed as probable hallucinations
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None


class AsrEngine:
    name = "abstract"
    #: True when the engine can emit English directly (Whisper's translate task).
    can_translate = False
    #: True when the engine keeps per-utterance state and only processes new audio (Nemotron).
    #: Non-streaming engines get their partial decodes capped to a trailing window.
    streaming = False

    def transcribe(self, audio: np.ndarray, task: str, is_partial: bool = False,
                   utterance_id: Optional[int] = None) -> AsrResult:  # pragma: no cover
        """Decode one utterance.

        `audio` is always the WHOLE utterance so far (16 kHz mono float32). Streaming engines may
        use `utterance_id` to keep per-utterance state and only process the new samples.
        """
        raise NotImplementedError

    def warmup(self) -> None:
        """Run one decode on silence so the first real caption does not pay JIT/allocation costs."""


def is_transcribe_only(model_name: str) -> bool:
    name = model_name.lower()
    return any(marker in name for marker in _TRANSCRIBE_ONLY_MARKERS)


def _looks_hallucinated(text: str, drop_phrases: Sequence[str]) -> bool:
    lowered = text.lower()
    if any(p and p.lower() in lowered for p in drop_phrases):
        return True
    words = lowered.split()
    # "the the the the" / a single token repeated: classic Whisper loop output.
    if len(words) >= 6 and len(set(words)) <= max(2, len(words) // 4):
        return True
    # A single long run of one character, which is what Whisper emits for the Indian languages it
    # cannot transcribe (measured: 70+ copies of one Gurmukhi character for Punjabi speech).
    stripped = "".join(lowered.split())
    if len(stripped) >= 12 and len(set(stripped)) <= 2:
        return True
    return False


class FasterWhisperEngine(AsrEngine):
    name = "faster-whisper"
    can_translate = True

    def __init__(self, cfg: AsrConfig):
        from faster_whisper import WhisperModel

        self.cfg = cfg
        device = cfg.device
        compute_type = cfg.compute_type
        if compute_type == "auto":
            # CTranslate2's "auto" picks the fastest supported type: int8 on CPU, float16 on CUDA.
            # Explicit "cpu" gets int8 spelled out so the log line says what actually runs.
            compute_type = "int8" if device == "cpu" else "auto"
        from faster_whisper.tokenizer import _LANGUAGE_CODES

        bad = [l for l in cfg.languages if l.strip().lower() not in _LANGUAGE_CODES]
        if bad:
            raise ValueError(f"unknown Whisper language code(s) {bad}; use ISO 639-1 such as es, hi, ta, te")
        weak = sorted(set(cfg.languages) & WEAK_TRANSCRIPTION_LANGUAGES)
        if weak and cfg.mode != "translate":
            log.warning("Whisper transcribes %s poorly (wrong script or repeated characters), even though it "
                        "translates them into English usably. Prefer mode 'translate' for these; in '%s' mode "
                        "the original-language line will often be wrong.", ", ".join(weak), cfg.mode)
        if cfg.mode == "translate" and is_transcribe_only(cfg.model):
            raise ValueError(
                f"model {cfg.model!r} is transcription-only and cannot translate. Use asr.model = "
                f"'small' / 'medium' / 'large-v3' for mode 'translate', or switch to mode 'dual'."
            )
        t0 = time.monotonic()
        self.model = WhisperModel(
            cfg.model,
            device=device,
            compute_type=compute_type,
            cpu_threads=cfg.cpu_threads,
            download_root=cfg.download_root,
        )
        log.info("loaded Whisper %s on %s/%s in %.1fs", cfg.model, device, compute_type, time.monotonic() - t0)
        self._lock = threading.Lock()   # CTranslate2 is thread-safe, but one decode at a time keeps latency predictable
        self.allowed = [l.strip().lower() for l in cfg.languages if l and l.strip()]
        self.fixed_language = self.allowed[0] if len(self.allowed) == 1 else None
        self._lang_cache: "collections.OrderedDict[int, tuple]" = collections.OrderedDict()
        self._last_language: Optional[tuple] = None

    # -- language -------------------------------------------------------------------------------

    def _choose_language(self, audio: np.ndarray, is_partial: bool = False,
                         utterance_id: Optional[int] = None) -> tuple[Optional[str], float]:
        """Language for this decode. Detection is a full encoder pass, so it runs at most once per
        utterance (cached by id) and not at all on a very short partial."""
        if self.fixed_language:
            return self.fixed_language, 1.0
        if not self.allowed:
            return None, 0.0           # Whisper detects on its own, inside transcribe()
        if utterance_id is not None and utterance_id in self._lang_cache:
            return self._lang_cache[utterance_id]
        if is_partial and audio.shape[0] < 2 * 16000 and self._last_language:
            return self._last_language   # too little audio to judge; keep the previous speaker's language
        choice = self._detect_restricted(audio)
        self._last_language = choice
        if utterance_id is not None:
            self._lang_cache[utterance_id] = choice
            while len(self._lang_cache) > 16:
                self._lang_cache.popitem(last=False)
        return choice

    def _detect_restricted(self, audio: np.ndarray) -> tuple[Optional[str], float]:
        lang, prob, all_probs = self.model.detect_language(audio=audio)
        if lang in self.allowed:
            return lang, prob
        # Pick the most likely of the languages the user listed; the raw argmax was something else
        # (short Hindi clips are routinely mis-detected as Tamil/Urdu/Japanese by small models).
        best = max(((l, p) for l, p in all_probs if l in self.allowed), key=lambda lp: lp[1], default=None)
        if best is None:
            return self.allowed[0], 0.0
        return best

    # -- decode -----------------------------------------------------------------------------------

    def transcribe(self, audio: np.ndarray, task: str, is_partial: bool = False,
                   utterance_id: Optional[int] = None) -> AsrResult:
        cfg = self.cfg
        t0 = time.monotonic()
        duration = audio.shape[0] / 16000.0
        with self._lock:
            language, lang_prob = self._choose_language(audio, is_partial, utterance_id)
            segments, info = self.model.transcribe(
                audio,
                language=language,
                task=task,
                beam_size=cfg.beam_size,
                best_of=1,
                temperature=0.0,                       # no fallback ladder: bounded latency
                condition_on_previous_text=False,      # independent utterances; kills repetition loops
                vad_filter=False,                      # the segmenter already gated on VAD
                without_timestamps=True,
                no_speech_threshold=cfg.no_speech_threshold,
                log_prob_threshold=cfg.log_prob_threshold,
                compression_ratio_threshold=cfg.compression_ratio_threshold,
                initial_prompt=cfg.initial_prompt,
                hotwords=cfg.hotwords,
            )
            kept: List[str] = []
            dropped: List[str] = []
            logprobs: List[float] = []
            nsp: List[float] = []
            for seg in segments:
                text = seg.text.strip()
                if not text:
                    continue
                logprobs.append(seg.avg_logprob)
                nsp.append(seg.no_speech_prob)
                if seg.no_speech_prob > cfg.no_speech_threshold and seg.avg_logprob < cfg.log_prob_threshold:
                    dropped.append(text)
                    continue
                if seg.compression_ratio > cfg.compression_ratio_threshold:
                    dropped.append(text)
                    continue
                if _looks_hallucinated(text, cfg.drop_phrases):
                    dropped.append(text)
                    continue
                kept.append(text)
        if language is None:
            language, lang_prob = info.language, info.language_probability
        return AsrResult(
            text=" ".join(kept),
            language=language,
            language_probability=float(lang_prob),
            task=task,
            duration_s=duration,
            decode_s=time.monotonic() - t0,
            dropped=dropped,
            avg_logprob=(sum(logprobs) / len(logprobs)) if logprobs else None,
            no_speech_prob=max(nsp) if nsp else None,
        )

    def warmup(self) -> None:
        silence = np.zeros(16000, dtype=np.float32)
        try:
            self.transcribe(silence, task="transcribe")
        except ValueError:
            raise                      # a configuration error: surface it before the meeting starts
        except Exception as exc:  # pragma: no cover - warmup is otherwise best effort
            log.warning("warmup decode failed (captions may still work): %s", exc)


class FakeEngine(AsrEngine):
    """Deterministic stand-in for tests: returns a text derived from the audio length."""

    name = "fake"
    can_translate = True

    def __init__(self, texts: Optional[Sequence[str]] = None, language: str = "es", delay_s: float = 0.0):
        self.texts = list(texts or [])
        self.language = language
        self.delay_s = delay_s
        self.calls: List[dict] = []
        self._i = 0

    def transcribe(self, audio: np.ndarray, task: str, is_partial: bool = False,
                   utterance_id: Optional[int] = None) -> AsrResult:
        if self.delay_s:
            time.sleep(self.delay_s)
        duration = audio.shape[0] / 16000.0
        if self.texts:
            text = self.texts[min(self._i, len(self.texts) - 1)]
            if not is_partial:
                self._i += 1
        else:
            text = f"{task} {duration:.1f}s"
        self.calls.append({"task": task, "partial": is_partial, "duration": duration, "utterance_id": utterance_id})
        return AsrResult(text=text, language=self.language, language_probability=0.9, task=task,
                         duration_s=duration, decode_s=self.delay_s)


def build_engine(cfg: AsrConfig) -> AsrEngine:
    if cfg.model == "fake" or cfg.engine == "fake":
        return FakeEngine()
    if cfg.engine == "nemotron":
        from zoomlt.asr_nemotron import NemotronEngine

        return NemotronEngine(cfg)
    if cfg.engine == "whisper":
        return FasterWhisperEngine(cfg)
    raise ValueError(f"unknown asr.engine {cfg.engine!r}")
