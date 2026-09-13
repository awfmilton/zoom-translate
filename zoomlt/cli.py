"""Command line: `zoomlt run`, `zoomlt devices`, `zoomlt file`, `zoomlt download`, `zoomlt check`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from typing import Any, Optional

from zoomlt import __version__
from zoomlt.config import Config

log = logging.getLogger("zoomlt")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", "-c", help="TOML config file (see config.example.toml)")
    p.add_argument("--engine", choices=["whisper", "nemotron"],
                   help="whisper = any language, English out (default); nemotron = best accuracy for es/hi, needs local MT")
    p.add_argument("--model", help="Whisper model: tiny, base, small, medium, large-v3 (default: small)")
    p.add_argument("--mode", choices=["translate", "transcribe", "dual"], help="translate = English out (default)")
    p.add_argument("--lang", "-l", action="append", dest="languages", metavar="CODE",
                   help="spoken language(s), repeatable: -l es -l hi. Omit to auto-detect.")
    p.add_argument("--device", help="capture device name substring or index (see `zoomlt devices`)")
    p.add_argument("--backend", choices=["auto", "sounddevice", "wasapi", "parec", "file"])
    p.add_argument("--asr-device", choices=["auto", "cpu", "cuda"], help="where Whisper runs")
    p.add_argument("--compute-type", help="int8 (CPU default), float16 (GPU), float32")
    p.add_argument("--port", type=int, help="overlay port (default 8765)")
    p.add_argument("--no-browser", action="store_true", help="do not open the overlay automatically")
    p.add_argument("--no-partials", action="store_true", help="only show finished sentences (less CPU)")
    p.add_argument("--zoom-cc-url", help="Zoom third-party caption API token URL: push captions into the meeting")
    p.add_argument("--transcript", help="append final captions to this JSONL file")
    p.add_argument("-v", "--verbose", action="store_true")


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config.from_toml(args.config) if getattr(args, "config", None) else Config()
    overrides: dict[str, Any] = {
        "asr.engine": getattr(args, "engine", None),
        "asr.model": getattr(args, "model", None),
        "asr.mode": getattr(args, "mode", None),
        "asr.languages": getattr(args, "languages", None),
        "asr.device": getattr(args, "asr_device", None),
        "asr.compute_type": getattr(args, "compute_type", None),
        "audio.device": getattr(args, "device", None),
        "audio.backend": getattr(args, "backend", None),
        "server.port": getattr(args, "port", None),
        "zoom_cc.api_token_url": getattr(args, "zoom_cc_url", None),
        "server.transcript_path": getattr(args, "transcript", None),
    }
    if getattr(args, "no_browser", False):
        overrides["server.open_browser"] = False
    if getattr(args, "no_partials", False):
        overrides["asr.partial_interval_ms"] = 0
    if getattr(args, "file", None):
        overrides["audio.backend"] = "file"
        overrides["audio.file"] = args.file
        if getattr(args, "fast", False):
            overrides["audio.file_realtime"] = False
    if getattr(args, "verbose", False):
        overrides["log_level"] = "DEBUG"
    # log_level is top-level, not a section
    level = overrides.pop("log_level", None)
    cfg.apply_overrides(overrides)
    if level:
        cfg.log_level = level
    return cfg


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------

def cmd_devices(args: argparse.Namespace) -> int:
    from zoomlt.audio.capture import list_devices

    rows = list_devices()
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return 0
    print("Capture devices (pass the index or part of the name to --device):\n")
    for r in rows:
        if "error" in r:
            print(f"  [{r['backend']}] unavailable: {r['error']}")
            continue
        tag = "  <- loopback/monitor: captures what you HEAR" if r.get("loopback") else ""
        print(f"  {r['index']:>3}  {r['name']}  ({r['backend']}, {r['hostapi']}, {r['channels']} ch, {r['rate']} Hz){tag}")
    print("\nZoom audio arrives on a loopback/monitor device, not a microphone. See README: 'Routing Zoom audio'.")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    from faster_whisper.utils import download_model

    if cfg.asr.engine == "nemotron":
        from zoomlt.asr_nemotron import ensure_model

        print("downloading Nemotron 3.5 ASR (sherpa-onnx int8, ~650 MB) if missing...")
        print(f"  -> {ensure_model(cfg.asr.download_root)}")
    else:
        print(f"downloading Whisper '{cfg.asr.model}' (cached under ~/.cache/huggingface unless asr.download_root is set)")
        path = download_model(cfg.asr.model, cache_dir=cfg.asr.download_root)   # same layout WhisperModel uses
        print(f"  -> {path}")
    if cfg.asr.mode == "dual" and cfg.mt.engine != "none":
        from zoomlt.mt import build_translator

        tr = build_translator(cfg.mt)
        for code in cfg.asr.languages or ["es", "hi"]:
            if code == cfg.mt.target:
                continue
            print(f"MT {code}->{cfg.mt.target} ({tr.name}): {'ready' if tr.supports(code) else 'not available'}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Print what would run, plus a quick environment sanity check."""
    cfg = build_config(args)
    import platform

    print(f"zoomlt {__version__} on {platform.system()} {platform.release()} / Python {platform.python_version()}")
    shown = cfg.to_dict()
    if shown["zoom_cc"].get("api_token_url"):
        shown["zoom_cc"]["api_token_url"] = "<set, hidden>"      # it is a meeting secret
    print(json.dumps(shown, indent=2))
    ok = True
    for mod, why in [("faster_whisper", "speech recognition"), ("onnxruntime", "voice activity detection"),
                     ("websockets", "overlay"), ("sounddevice", "audio capture")]:
        try:
            __import__(mod)
            print(f"  ok   {mod:16s} {why}")
        except Exception as exc:
            ok = False
            print(f"  MISSING {mod:13s} {why}: {exc}")
    if sys.platform.startswith("win"):
        try:
            __import__("pyaudiowpatch")
            print("  ok   pyaudiowpatch    WASAPI loopback (no virtual cable needed)")
        except Exception:
            print("  note pyaudiowpatch    not installed: pip install PyAudioWPatch for loopback capture")
    try:
        import ctranslate2

        print(f"  CTranslate2 {ctranslate2.__version__}: CUDA devices = {ctranslate2.get_cuda_device_count()}, "
              f"CPU compute types = {sorted(ctranslate2.get_supported_compute_types('cpu'))}")
    except Exception as exc:
        print(f"  ctranslate2 probe failed: {exc}")
    return 0 if ok else 1


_LOOPBACK_HINTS = ("loopback", "monitor", "blackhole", "cable", "stereo mix", "what u hear", "virtual", "soundflower", "loop", "parec:")


def _looks_like_loopback(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _LOOPBACK_HINTS)


def _run_pipeline(cfg: Config) -> int:
    from zoomlt.asr import build_engine
    from zoomlt.audio.capture import open_source
    from zoomlt.mt import NoopTranslator, build_translator
    from zoomlt.pipeline import Caption, Pipeline, PipelineStats
    from zoomlt.server import CaptionServer

    for noisy in ("faster_whisper", "httpx", "huggingface_hub", "websockets", "websockets.server", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    source = open_source(cfg.audio)
    log.info("audio source: %s", source.describe())
    if cfg.audio.backend != "file" and not _looks_like_loopback(source.name):
        log.warning("'%s' looks like a microphone, not a loopback/monitor device. Zoom's audio arrives on "
                    "a loopback device (Windows: WASAPI loopback, macOS: BlackHole, Linux: 'Monitor of ...'); "
                    "see README 'Routing Zoom audio'. Run `zoomlt devices` and pass --device.", source.name)

    log.info("loading Whisper '%s' (first run downloads the model)...", cfg.asr.model)
    engine = build_engine(cfg.asr)
    engine.warmup()
    translator = build_translator(cfg.mt) if cfg.asr.mode == "dual" else NoopTranslator()

    pusher = None
    if cfg.zoom_cc.api_token_url:
        from zoomlt.zoom_cc import ZoomCaptionPusher

        pusher = ZoomCaptionPusher(cfg.zoom_cc).start()
        log.info("pushing final captions into Zoom via the caption API token")

    if cfg.asr.engine == "whisper" and cfg.asr.languages and all(l in ("es", "hi", "en") for l in cfg.asr.languages):
        log.info("tip: for Spanish/Hindi, `--engine nemotron` is markedly more accurate and faster on CPU")
    app_config = {
        "mode": cfg.asr.mode, "engine": engine.name, "model": cfg.asr.model if cfg.asr.engine == "whisper" else "nemotron-3.5",
        "languages": cfg.asr.languages,
        "partials": cfg.asr.partial_interval_ms > 0, "source": source.describe(), "version": __version__,
    }
    server = CaptionServer(cfg.server, app_config)
    server.start_in_thread()
    if not server.wait_ready(10):
        log.error("overlay server did not start on %s:%d: %s. Pick another port with --port.",
                  cfg.server.host, cfg.server.port, server.error or "timeout")
        source.stop()
        return 2
    url = f"http://{cfg.server.host}:{server.port}/"
    print(f"\n  Overlay: {url}   (keep this window on top of Zoom)\n", flush=True)
    if cfg.server.open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover
            pass

    def on_caption(cap: Caption) -> None:
        server.publish(cap.to_dict())
        if cap.kind == "final":
            shown = cap.english or cap.source_text or ""
            extra = f"  [{cap.source_text}]" if cap.english and cap.source_text else ""
            print(f"[{cap.source_lang or '??'} {cap.lag_s:4.1f}s] {shown}{extra}", flush=True)
            if pusher and cap.english:
                pusher.push(cap.english)
        elif cap.kind == "retract":
            pass
        elif log.isEnabledFor(logging.DEBUG):
            log.debug("partial #%d: %s", cap.id, cap.english or cap.source_text)

    def on_status(stats: PipelineStats) -> None:
        server.publish(stats.to_dict())

    pipeline = Pipeline(cfg, engine, translator, on_caption, on_status)
    stop_requested = threading.Event()

    def run():
        try:
            pipeline.run(source)
        except Exception:
            log.exception("pipeline stopped with an error")
        finally:
            stop_requested.set()

    t = threading.Thread(target=run, name="zoomlt-pipeline", daemon=True)
    t.start()
    try:
        while t.is_alive() and not stop_requested.is_set():
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nstopping...", flush=True)
        pipeline.stop()
        source.stop()
        t.join(timeout=10)
    finally:
        if pusher:
            pusher.stop()
        server.shutdown()
    s = pipeline.stats
    if s.finals:
        print(f"\n{s.finals} captions, real-time factor {s.realtime_factor:.2f} "
              f"(under 1.0 keeps up), {s.dropped_segments} hallucinated segments dropped", flush=True)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    return _run_pipeline(cfg)


def cmd_file(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    return _run_pipeline(cfg)


def cmd_convert_mt(args: argparse.Namespace) -> int:
    """One-time conversion of a Hugging Face Marian (OPUS-MT) model to CTranslate2 for the ct2 engine."""
    from zoomlt.mt import convert_hf_marian

    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        print("convert-mt needs the conversion extras (one time, ~1 GB): pip install 'zoomlt[convert]'", file=sys.stderr)
        return 2
    dest = convert_hf_marian(args.repo, args.lang, args.target, quantization=args.quantization)
    print(f"converted {args.repo} -> {dest}\nThe ct2 engine now uses it for {args.lang}->{args.target}.")
    return 0


# ---------------------------------------------------------------------------------------------

def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zoomlt", description="Free, local live captions that translate Zoom audio into English.")
    parser.add_argument("--version", action="version", version=f"zoomlt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="capture live audio and caption it")
    _add_common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("file", help="caption an audio file as if it were live (try it without a meeting)")
    p.add_argument("file", help="WAV/MP3/M4A path")
    p.add_argument("--fast", action="store_true", help="do not pace playback at real-time speed")
    _add_common(p)
    p.set_defaults(func=cmd_file)

    p = sub.add_parser("devices", help="list audio capture devices")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("download", help="pre-download the Whisper model (and Argos packs for dual mode)")
    _add_common(p)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("convert-mt", help="convert an OPUS-MT model from Hugging Face for the ct2 translation engine")
    p.add_argument("repo", help="e.g. Helsinki-NLP/opus-mt-tc-bible-big-inc-en (best Hindi->English)")
    p.add_argument("--lang", required=True, help="source language code this model will serve (hi, ta, ...)")
    p.add_argument("--target", default="en")
    p.add_argument("--quantization", default="int8")
    p.set_defaults(func=cmd_convert_mt)

    p = sub.add_parser("check", help="show the effective config and verify the environment")
    _add_common(p)
    p.set_defaults(func=cmd_check)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    level = "DEBUG" if getattr(args, "verbose", False) else os.environ.get("ZOOMLT_LOG", "INFO")
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        return int(args.func(args) or 0)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
