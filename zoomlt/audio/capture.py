"""Audio sources. Every source yields 16 kHz mono float32 blocks through the same interface.

    source = open_source(cfg.audio)
    with source:
        for block in source.blocks():
            ...

Backends
    sounddevice  PortAudio input devices. Covers microphones, virtual cables (BlackHole on macOS,
                 VB-Cable on Windows) and PulseAudio/PipeWire "Monitor of ..." sources on Linux.
    wasapi       Windows loopback via PyAudioWPatch: records what an OUTPUT device plays, so the
                 meeting audio is captured with no virtual cable and no extra setup.
    file         A WAV/MP3/M4A file, optionally paced at real-time speed. Used to try the app
                 without a meeting, and by the tests.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from typing import Iterator, Optional

import numpy as np

from zoomlt.audio.resample import TARGET_RATE, Resampler, to_mono
from zoomlt.config import AudioConfig

log = logging.getLogger(__name__)

# If the consumer stalls, keep the newest N seconds and drop the oldest blocks. Captions that are
# minutes late are useless; captions with a small gap are not.
_MAX_QUEUED_SECONDS = 20.0


class AudioSource:
    """Base class. Subclasses implement start()/stop() and put raw blocks on self._queue."""

    name: str = "?"
    native_rate: int = TARGET_RATE
    channels: int = 1

    #: Live sources drop the oldest audio when the consumer falls behind (late captions are
    #: useless). File playback that is not paced at real time must instead wait, or a long file
    #: would lose everything but its last 20 s.
    drop_when_behind: bool = True
    #: Seconds without audio before blocks() warns that the device is silent.
    silence_warn_s: float = 5.0

    def __init__(self, block_ms: int = 32):
        self.block_ms = block_ms
        self._queue: "queue.Queue[Optional[np.ndarray]]" = queue.Queue()
        self._dropped = 0
        self._stopped = threading.Event()
        self._started = False
        self.overflows = 0            # driver-side overruns (audio lost before it reached us)

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def ensure_started(self) -> None:
        """Open the device once. Called early by the CLI so a bad device fails before models load."""
        if not self._started:
            self.start()
            self._started = True

    def stop(self) -> None:
        self._stopped.set()
        self._queue.put(None)

    def __enter__(self) -> "AudioSource":
        self.ensure_started()
        return self

    def _note_overflow(self) -> None:
        self.overflows += 1
        if self.overflows in (1, 10) or self.overflows % 100 == 0:
            log.warning("audio input overflow (%d so far): the CPU is too busy to keep up with capture; "
                        "captions may be choppy. Try a smaller model or --no-partials.", self.overflows)

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- producer side (called from the audio thread) --------------------------------------------

    def _push(self, raw: np.ndarray) -> None:
        max_blocks = int(_MAX_QUEUED_SECONDS * 1000 / max(self.block_ms, 1))
        if self.drop_when_behind:
            while self._queue.qsize() > max_blocks:
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                except queue.Empty:  # pragma: no cover - race with consumer
                    break
        else:
            while self._queue.qsize() > max_blocks and not self._stopped.is_set():
                time.sleep(0.01)
        self._queue.put(raw)

    def dropped_seconds(self) -> float:
        """Audio discarded so far because the consumer fell behind."""
        return self._dropped * self.block_ms / 1000.0

    # -- consumer side ---------------------------------------------------------------------------

    def blocks(self) -> Iterator[np.ndarray]:
        """Yield 16 kHz mono float32 blocks until stop() is called."""
        resampler = Resampler(self.native_rate, TARGET_RATE)
        last_drop_report = 0
        quiet_for = 0.0
        while True:
            try:
                raw = self._queue.get(timeout=self.silence_warn_s)
            except queue.Empty:
                if self._stopped.is_set():
                    break
                quiet_for += self.silence_warn_s
                if quiet_for in (self.silence_warn_s, self.silence_warn_s * 6):
                    log.warning("no audio from %s for %.0f s. Is the meeting audio playing to this device? "
                                "(`zoomlt devices` lists capture devices)", self.name, quiet_for)
                continue
            quiet_for = 0.0
            if raw is None:
                break
            if self._dropped and self._dropped != last_drop_report:
                log.warning("audio consumer fell behind; dropped %d blocks so far", self._dropped)
                last_drop_report = self._dropped
            block = resampler.process(to_mono(raw))
            if block.size:
                yield block

    def describe(self) -> str:
        return f"{self.name} @ {self.native_rate} Hz x{self.channels}"


# ---------------------------------------------------------------------------------------------
# sounddevice (PortAudio)
# ---------------------------------------------------------------------------------------------

LOOPBACK_HINTS = ("loopback", "monitor", "blackhole", "cable", "stereo mix", "what u hear", "virtual",
                  "soundflower", "loop", "parec:")


def looks_like_loopback(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in LOOPBACK_HINTS)


def _import_sounddevice():
    try:
        import sounddevice as sd
    except (OSError, ImportError) as exc:
        raise RuntimeError(
            "sounddevice could not load PortAudio. Linux: `sudo apt install libportaudio2` "
            "(Fedora: `sudo dnf install portaudio`), or use --backend parec. Detail: " + str(exc)
        ) from exc
    return sd


def _match_device(devices, wanted: Optional[str], want_input: bool = True) -> Optional[int]:
    """Resolve a device index from an int, a numeric string or a case-insensitive name substring."""
    if wanted is None or wanted == "":
        return None
    if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.strip().isdigit()):
        return int(wanted)
    needle = str(wanted).lower()
    for idx, dev in enumerate(devices):
        if want_input and dev.get("max_input_channels", 0) <= 0:
            continue
        if needle in str(dev.get("name", "")).lower():
            return idx
    raise ValueError(f"no input device matching {wanted!r}; run `zoomlt devices` to list them")


class SounddeviceSource(AudioSource):
    def __init__(self, device: Optional[str] = None, block_ms: int = 32):
        super().__init__(block_ms)
        sd = _import_sounddevice()
        self._sd = sd
        devices = sd.query_devices()
        idx = _match_device(devices, device)
        if idx is None:
            idx = sd.default.device[0]
            if idx is None or idx < 0:
                raise RuntimeError("no default input device; pass --device")
        info = sd.query_devices(idx)
        if info["max_input_channels"] <= 0:
            raise ValueError(f"device {idx} ({info['name']}) has no input channels")
        self.index = idx
        self.name = str(info["name"])
        self.native_rate = int(round(info["default_samplerate"]))
        self.channels = min(2, int(info["max_input_channels"]))
        self._stream = None

    def start(self) -> None:
        sd = self._sd
        blocksize = int(self.native_rate * self.block_ms / 1000)

        def callback(indata, frames, time_info, status):
            if status:
                if status.input_overflow:
                    self._note_overflow()
                else:
                    log.debug("sounddevice status: %s", status)
            self._push(np.array(indata, dtype=np.float32, copy=True))

        self._stream = sd.InputStream(
            device=self.index,
            channels=self.channels,
            samplerate=self.native_rate,
            blocksize=blocksize,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()
        log.info("capturing from %s", self.describe())

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        super().stop()


# ---------------------------------------------------------------------------------------------
# Windows WASAPI loopback (PyAudioWPatch)
# ---------------------------------------------------------------------------------------------

class WasapiLoopbackSource(AudioSource):
    """Record what an output device plays. Windows only."""

    def __init__(self, device: Optional[str] = None, block_ms: int = 32):
        super().__init__(block_ms)
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:  # pragma: no cover - platform specific
            raise RuntimeError("WASAPI loopback needs PyAudioWPatch: pip install PyAudioWPatch") from exc
        self._pa_mod = pyaudio
        self._pa = pyaudio.PyAudio()
        self._info = self._pick_loopback(device)
        self.index = int(self._info["index"])
        self.name = str(self._info["name"])
        self.native_rate = int(round(self._info["defaultSampleRate"]))
        self.channels = max(1, min(2, int(self._info["maxInputChannels"])))
        self._stream = None

    def _pick_loopback(self, device: Optional[str]):
        pa, pyaudio = self._pa, self._pa_mod
        loopbacks = list(pa.get_loopback_device_info_generator())
        if not loopbacks:
            raise RuntimeError("no WASAPI loopback devices found")
        if device is not None and str(device).strip() != "":
            if str(device).strip().isdigit():
                info = pa.get_device_info_by_index(int(device))
                if info.get("isLoopbackDevice"):
                    return info
                try:                       # an output device: use its loopback twin
                    return pa.get_wasapi_loopback_analogue_by_index(int(device))
                except Exception as exc:
                    raise ValueError(f"device {device} is not a WASAPI loopback device; run `zoomlt devices` "
                                     "and pass part of a name marked loopback") from exc
            needle = str(device).lower()
            for lb in loopbacks:
                if needle in str(lb["name"]).lower():
                    return lb
            raise ValueError(f"no loopback device matching {device!r}; run `zoomlt devices`")
        # Default: the loopback twin of the default output device (the speakers Zoom plays into).
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for lb in loopbacks:
            if str(speakers["name"]) in str(lb["name"]):
                return lb
        return loopbacks[0]

    def start(self) -> None:
        pyaudio = self._pa_mod
        frames = int(self.native_rate * self.block_ms / 1000)

        def callback(in_data, frame_count, time_info, status):
            if status and (status & pyaudio.paInputOverflow):
                self._note_overflow()
            raw = np.frombuffer(in_data, dtype=np.float32).reshape(-1, self.channels)
            self._push(raw.copy())
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self.channels,
            rate=self.native_rate,
            frames_per_buffer=frames,
            input=True,
            input_device_index=self.index,
            stream_callback=callback,
        )
        self._stream.start_stream()
        log.info("capturing WASAPI loopback from %s", self.describe())

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            finally:
                self._stream = None
        try:
            self._pa.terminate()
        except Exception:  # pragma: no cover
            pass
        super().stop()


# ---------------------------------------------------------------------------------------------
# Linux: parec on the default sink monitor (PulseAudio, or PipeWire through pipewire-pulse)
# ---------------------------------------------------------------------------------------------

class ParecSource(AudioSource):
    """`parec -d @DEFAULT_MONITOR@` hands us 16 kHz mono PCM of everything the speakers play."""

    def __init__(self, device: Optional[str] = None, block_ms: int = 32):
        super().__init__(block_ms)
        import shutil
        import subprocess

        if shutil.which("parec") is None:
            raise RuntimeError("parec not found; install pulseaudio-utils (or use --backend sounddevice)")
        self._subprocess = subprocess
        self.monitor = device or "@DEFAULT_MONITOR@"
        self.name = f"parec:{self.monitor}"
        self.native_rate = TARGET_RATE
        self.channels = 1
        self._proc = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        cmd = ["parec", "-d", self.monitor, "--raw", "--format=s16le", f"--rate={TARGET_RATE}", "--channels=1",
               "--latency-msec=50"]
        self._proc = self._subprocess.Popen(cmd, stdout=self._subprocess.PIPE, stderr=self._subprocess.DEVNULL)
        self._thread = threading.Thread(target=self._pump, name="zoomlt-parec", daemon=True)
        self._thread.start()
        log.info("capturing from %s", self.describe())

    def _pump(self) -> None:
        step = int(TARGET_RATE * self.block_ms / 1000) * 2
        out = self._proc.stdout
        while not self._stopped.is_set():
            data = out.read(step)
            if not data:
                break
            self._push(np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0)
        if not self._stopped.is_set():
            log.warning("parec exited (code %s)", self._proc.poll())
            self._queue.put(None)

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:  # pragma: no cover
                pass
            self._proc = None
        super().stop()


# ---------------------------------------------------------------------------------------------
# File playback
# ---------------------------------------------------------------------------------------------

def load_audio_16k(path: str) -> np.ndarray:
    """Decode any audio file to 16 kHz mono float32 (WAV via stdlib, everything else via PyAV)."""
    if path.lower().endswith(".wav"):
        import wave

        with wave.open(path, "rb") as wf:
            rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
        if width == 2:
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 4:
            data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        elif width == 1:
            data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            raise ValueError(f"unsupported WAV sample width {width}")
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
        if rate != TARGET_RATE:
            data = Resampler(rate, TARGET_RATE).process(data)
        return np.ascontiguousarray(data, dtype=np.float32)
    from faster_whisper.audio import decode_audio

    return np.ascontiguousarray(decode_audio(path, sampling_rate=TARGET_RATE), dtype=np.float32)


class FileSource(AudioSource):
    def __init__(self, path: str, realtime: bool = True, block_ms: int = 32, audio: Optional[np.ndarray] = None):
        super().__init__(block_ms)
        self.path = path
        self.realtime = realtime
        self.drop_when_behind = realtime
        self.name = f"file:{path}"
        self._audio = audio if audio is not None else load_audio_16k(path)
        self._thread: Optional[threading.Thread] = None

    @property
    def duration_s(self) -> float:
        return self._audio.shape[0] / TARGET_RATE

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="zoomlt-file-source", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        step = int(TARGET_RATE * self.block_ms / 1000)
        t0 = time.monotonic()
        for i in range(0, self._audio.shape[0], step):
            if self._stopped.is_set():
                return
            if self.realtime:
                due = t0 + i / TARGET_RATE
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            self._push(self._audio[i : i + step])
        # Trailing silence lets the VAD close the last utterance before the stream ends.
        for _ in range(int(1500 / self.block_ms)):
            if self._stopped.is_set():
                return
            if self.realtime:
                time.sleep(self.block_ms / 1000)
            self._push(np.zeros(step, dtype=np.float32))
        self._queue.put(None)


# ---------------------------------------------------------------------------------------------
# factory + listing
# ---------------------------------------------------------------------------------------------

def open_source(cfg: AudioConfig) -> AudioSource:
    backend = cfg.backend
    if backend == "auto":
        backend = "sounddevice"
        if sys.platform.startswith("linux") and cfg.device is None:
            import shutil

            if shutil.which("parec"):
                backend = "parec"      # the default sink's monitor: exactly what the speakers play
        if sys.platform.startswith("win"):
            try:
                import pyaudiowpatch  # noqa: F401

                backend = "wasapi"
            except ImportError:
                log.info("PyAudioWPatch not installed; using sounddevice (pip install PyAudioWPatch for loopback)")
    if backend == "file":
        if not cfg.file:
            raise ValueError("audio.file is required for the file backend")
        return FileSource(cfg.file, realtime=cfg.file_realtime, block_ms=cfg.block_ms)
    if backend == "wasapi":
        return WasapiLoopbackSource(cfg.device, block_ms=cfg.block_ms)
    if backend == "parec":
        return ParecSource(cfg.device, block_ms=cfg.block_ms)
    if backend == "sounddevice":
        return SounddeviceSource(cfg.device, block_ms=cfg.block_ms)
    raise ValueError(f"unknown audio backend {backend!r}")


def list_devices() -> list[dict]:
    """Enumerate capture candidates across backends (for `zoomlt devices`)."""
    out: list[dict] = []
    try:
        sd = _import_sounddevice()
        apis = sd.query_hostapis()
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            out.append({
                "backend": "sounddevice",
                "index": idx,
                "name": dev["name"],
                "hostapi": apis[dev["hostapi"]]["name"] if apis else "?",
                "channels": dev["max_input_channels"],
                "rate": int(round(dev["default_samplerate"])),
                "loopback": looks_like_loopback(str(dev["name"])),
            })
    except Exception as exc:  # PortAudio missing, etc.
        out.append({"backend": "sounddevice", "error": str(exc)})
    if sys.platform.startswith("win"):
        try:
            import pyaudiowpatch as pyaudio

            pa = pyaudio.PyAudio()
            try:
                for lb in pa.get_loopback_device_info_generator():
                    out.append({
                        "backend": "wasapi",
                        "index": lb["index"],
                        "name": lb["name"],
                        "hostapi": "WASAPI loopback",
                        "channels": lb["maxInputChannels"],
                        "rate": int(round(lb["defaultSampleRate"])),
                        "loopback": True,
                    })
            finally:
                pa.terminate()
        except ImportError:
            out.append({"backend": "wasapi", "error": "PyAudioWPatch not installed (pip install PyAudioWPatch)"})
        except Exception as exc:  # pragma: no cover
            out.append({"backend": "wasapi", "error": str(exc)})
    return out
