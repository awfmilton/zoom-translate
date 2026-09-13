"""Configuration: a plain dataclass, loadable from a TOML file, overridable from the CLI.

Every knob that matters for latency or accuracy lives here with a comment saying
what it trades off, so a non-ML user can tune it from the config file alone.
"""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class AudioConfig:
    # Which capture backend to use:
    #   "auto"        pick wasapi-loopback on Windows if PyAudioWPatch is installed, else sounddevice
    #   "sounddevice" any PortAudio input device (mic, BlackHole, VB-Cable, PulseAudio monitor)
    #   "wasapi"      Windows-only loopback of an output device (what the speakers play)
    #   "parec"       Linux: the PulseAudio/PipeWire monitor of the default output, via parec
    #   "file"        read a WAV/MP3 file at real-time speed (for testing without Zoom)
    backend: str = "auto"
    # Device name substring or integer index. None = the backend's default device.
    device: str | None = None
    # Path used by the "file" backend.
    file: str | None = None
    # Play the file at real-time speed (True) or as fast as the pipeline can eat it (False).
    file_realtime: bool = True
    # Native capture rate is whatever the device runs at; everything is resampled to 16 kHz mono.
    block_ms: int = 32


@dataclass
class VadConfig:
    # Silero VAD speech probability above which a 32 ms frame counts as speech.
    threshold: float = 0.5
    # A frame counts as silence again only below this (hysteresis stops flapping mid-word).
    neg_threshold: float = 0.35
    # Silence this long ends an utterance and triggers the FINAL caption. Lower = snappier
    # captions but sentences get split more; higher = more complete sentences, more delay.
    end_silence_ms: int = 600
    # Audio kept from before the VAD fired, so the first syllable is not clipped.
    pre_roll_ms: int = 320
    # Speech shorter than this is ignored (coughs, clicks, Zoom join chimes).
    min_speech_ms: int = 250
    # A speaker who never pauses gets cut here and the cut point is moved to the quietest
    # frame in the last `cut_search_ms` so words are not sliced in half.
    max_utterance_s: float = 15.0
    cut_search_ms: int = 2000


@dataclass
class AsrConfig:
    # Speech recogniser:
    #   "whisper"   faster-whisper. Every language; can output English directly (mode "translate").
    #   "nemotron"  NVIDIA Nemotron 3.5 streaming (sherpa-onnx). Spanish/Hindi/English only, far more
    #               accurate for those (Hindi WER ~7% vs ~38% for Whisper small) and faster on CPU.
    #               Transcribes only, so it always runs in mode "dual" (transcribe + local MT).
    engine: str = "whisper"
    # Whisper size. CPU: "small" is the sweet spot for Spanish+Hindi -> English. GPU: "large-v3".
    # NOTE: "large-v3-turbo" and "distil-*" are transcription-only models; they do not do the
    # translate task well. Use them only with mode = "dual" (transcribe + local MT).
    model: str = "small"
    device: str = "auto"       # "auto" | "cpu" | "cuda"
    compute_type: str = "auto" # "auto" -> int8 on CPU, float16 on GPU
    cpu_threads: int = 0        # 0 = let CTranslate2 decide
    download_root: str | None = None
    # What Whisper is asked to do:
    #   "translate"  one pass, English out (fastest, works for every Whisper language)
    #   "transcribe" one pass, original language out (no English)
    #   "dual"       transcribe, then translate the text with the local MT engine (shows both)
    mode: str = "translate"
    # Restrict/hint the spoken languages (ISO 639-1: es, hi, ta, te, mr, gu, kn, ml, bn, pa, ur).
    # Empty = let Whisper detect per utterance. One entry = fix the language (fastest, most stable).
    languages: list[str] = field(default_factory=list)
    beam_size: int = 1          # 1 = greedy (fast); 3-5 is more accurate but slower on CPU
    # Domain words Whisper should know how to spell (names, product terms).
    hotwords: str | None = None
    initial_prompt: str | None = None
    # Partial captions: re-decode the in-progress utterance every N ms so words show up while the
    # person is still talking. 0 disables partials (only finals; halves CPU load). The pipeline
    # stretches the interval automatically when a partial decode takes longer than this.
    partial_interval_ms: int = 1200
    # Whisper re-reads the whole utterance for every partial; cap that at the trailing N seconds
    # (the final still decodes everything). 0 = no cap. Ignored by the streaming Nemotron engine.
    partial_window_s: float = 8.0
    # Hallucination guards (Whisper invents text on silence / music / noise).
    no_speech_threshold: float = 0.6
    log_prob_threshold: float = -1.0
    compression_ratio_threshold: float = 2.4
    drop_phrases: list[str] = field(default_factory=lambda: [
        "subtitles by", "amara.org", "thank you for watching", "thanks for watching",
        "subscribe to", "like and subscribe", "copyright", "www.", "transcribed by",
    ])


@dataclass
class MtConfig:
    # Local text translation engine for mode = "dual":
    #   "ct2"    CTranslate2 + SentencePiece running OPUS-MT / Argos models (default; light deps)
    #   "argos"  the argostranslate package (pulls in stanza + torch; only if you already have it)
    #   "none"   no translation (mode "dual" then shows the transcript only)
    engine: str = "ct2"
    target: str = "en"
    # Download missing models on first use (one time; from Hugging Face / argos-net.com).
    auto_install: bool = True
    # ct2 engine: per source language, a directory with a CTranslate2 model. Overrides the built-in
    # defaults, e.g. a self-converted Helsinki-NLP/opus-mt-tc-bible-big-inc-en for better Hindi:
    #   model_dirs = { hi = "C:/models/inc-en-ct2" }
    model_dirs: dict = field(default_factory=dict)
    # ct2 engine: Hugging Face repo id per source language (a CTranslate2 conversion with
    # source.spm/target.spm). Defaults are Apache-2.0 OPUS-MT conversions.
    hf_repos: dict = field(default_factory=dict)
    beam_size: int = 2


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    # Keep this many final captions for late-joining overlay windows.
    history: int = 200
    # Write every final caption to this file (JSON lines). None = off.
    transcript_path: str | None = None


@dataclass
class ZoomCcConfig:
    # Zoom's third-party closed-caption API token URL (host copies it from the CC menu).
    # When set, every final English caption is POSTed into the meeting's caption stream.
    api_token_url: str | None = None
    lang: str = "en-US"
    # Zoom rejects bursts; captions are coalesced and sent at most this often.
    min_interval_ms: int = 800
    timeout_s: float = 5.0


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    mt: MtConfig = field(default_factory=MtConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    zoom_cc: ZoomCcConfig = field(default_factory=ZoomCcConfig)
    log_level: str = "INFO"

    # ---- loading -------------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        cfg = cls()
        for section_name, section_val in data.items():
            if section_name == "log_level":
                cfg.log_level = str(section_val)
                continue
            if not hasattr(cfg, section_name):
                raise ValueError(f"unknown config section [{section_name}]")
            section = getattr(cfg, section_name)
            if not dataclasses.is_dataclass(section) or not isinstance(section_val, dict):
                raise ValueError(f"config section [{section_name}] must be a table")
            for key, val in section_val.items():
                if not hasattr(section, key):
                    raise ValueError(f"unknown config key {section_name}.{key}")
                setattr(section, key, val)
        cfg.validate()
        return cfg

    @classmethod
    def from_toml(cls, path: str | Path) -> "Config":
        with open(path, "rb") as fh:
            return cls.from_dict(tomllib.load(fh))

    def apply_overrides(self, overrides: dict[str, Any]) -> None:
        """Apply dotted overrides such as {"asr.model": "small"} (used by the CLI)."""
        for dotted, val in overrides.items():
            if val is None:
                continue
            section_name, _, key = dotted.partition(".")
            section = getattr(self, section_name)
            if not hasattr(section, key):
                raise ValueError(f"unknown config key {dotted}")
            setattr(section, key, val)
        self.validate()

    def validate(self) -> None:
        if self.asr.mode not in ("translate", "transcribe", "dual"):
            raise ValueError(f"asr.mode must be translate|transcribe|dual, got {self.asr.mode!r}")
        if self.audio.backend not in ("auto", "sounddevice", "wasapi", "parec", "file"):
            raise ValueError(f"audio.backend must be auto|sounddevice|wasapi|parec|file, got {self.audio.backend!r}")
        if self.audio.backend == "file" and not self.audio.file:
            raise ValueError("audio.file is required when audio.backend = 'file'")
        if not (0.0 < self.vad.threshold <= 1.0):
            raise ValueError("vad.threshold must be in (0, 1]")
        if self.vad.neg_threshold > self.vad.threshold:
            raise ValueError("vad.neg_threshold must be <= vad.threshold")
        if self.vad.end_silence_ms < 100:
            raise ValueError("vad.end_silence_ms below 100 ms will split words")
        floor_ms = self.vad.cut_search_ms + self.vad.pre_roll_ms + self.vad.min_speech_ms + 500
        if self.vad.max_utterance_s * 1000 <= floor_ms:
            raise ValueError(f"vad.max_utterance_s must exceed {floor_ms / 1000:.1f} s "
                             "(cut_search_ms + pre_roll_ms + min_speech_ms + 0.5 s)")
        for code in self.asr.languages:
            if not (isinstance(code, str) and code.isalpha() and code.islower() and 2 <= len(code) <= 3):
                raise ValueError(f"asr.languages entries are ISO 639-1 codes such as 'es' or 'hi', got {code!r}")
        if self.asr.partial_interval_ms < 0:
            raise ValueError("asr.partial_interval_ms must be >= 0")
        if self.mt.engine not in ("ct2", "argos", "none", "fake"):
            raise ValueError(f"mt.engine must be ct2|argos|none, got {self.mt.engine!r}")
        if self.asr.engine not in ("whisper", "nemotron", "fake"):
            raise ValueError(f"asr.engine must be whisper|nemotron, got {self.asr.engine!r}")
        if self.asr.engine == "nemotron":
            if self.asr.mode == "translate":
                # Nemotron cannot emit English itself; route through the local MT stage instead.
                self.asr.mode = "dual"
            bad = [l for l in self.asr.languages if l.lower() not in ("es", "hi", "en")]
            if bad:
                raise ValueError(f"asr.engine = 'nemotron' supports es, hi, en only; {bad} need engine 'whisper'")
        if self.asr.mode == "dual" and self.mt.engine == "none":
            raise ValueError("asr.mode = 'dual' needs an MT engine (mt.engine = 'ct2' or 'argos')")
        if not (0 <= self.server.port <= 65535):  # 0 = let the OS pick (tests)
            raise ValueError("server.port out of range")
        if self.asr.partial_window_s < 0:
            raise ValueError("asr.partial_window_s must be >= 0")
        url = self.zoom_cc.api_token_url
        if url:
            import urllib.parse

            parts = urllib.parse.urlsplit(str(url))
            if parts.scheme != "https" or not parts.netloc or not parts.query:
                # never echo the value: it is a meeting secret
                raise ValueError("zoom_cc.api_token_url must be the https:// URL copied from Zoom's caption menu (value not shown)")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
