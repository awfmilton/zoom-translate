"""Local text translation for mode "dual" (original text + English).

Engines
    ct2    CTranslate2 + SentencePiece (both already installed with faster-whisper) running OPUS-MT
           Marian models or Argos Translate model packs. No torch, no stanza. Default.
    argos  the argostranslate package, if you already have it installed (it pulls in stanza + torch).
    none   nothing; mode "dual" shows the transcript only.

Model sources for the ct2 engine, per source language, first hit wins:
    1. mt.model_dirs[lang]                        your own CTranslate2 model directory
    2. <cache>/mt/<lang>-en/                       previously downloaded or converted (see `zoomlt convert-mt`)
    3. mt.hf_repos[lang] or the built-in default   a CTranslate2 conversion on Hugging Face (Apache-2.0)
    4. the Argos Translate package index           .argosmodel packs whose tokenizer is SentencePiece

Quality notes (measured on this project's Hindi test sentences): Helsinki-NLP/opus-mt-tc-bible-big-inc-en
(convert it once with `zoomlt convert-mt`) is clearly the best Hindi->English model; the Argos hi_en
pack is decent and needs no conversion; the old opus-mt-hi-en is weak. For Spanish the OPUS-MT
es-en conversion is good out of the box.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from zoomlt.config import MtConfig

log = logging.getLogger(__name__)

#: Hugging Face repos holding CTranslate2 conversions of OPUS-MT models (Apache-2.0), with the
#: Marian source.spm / target.spm tokenizers. Keyed by source language, target English.
DEFAULT_HF_REPOS: Dict[str, str] = {
    "es": "gaudi/opus-mt-es-en-ctranslate2",
}
#: Languages whose best zero-setup model is the Argos pack (SentencePiece tokenizer, no torch).
ARGOS_INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
ARGOS_PREFERRED = ("hi", "bn", "ur")
#: Conversion suggestions shown when a language has no model at all.
CONVERT_SUGGESTIONS: Dict[str, str] = {
    "hi": "Helsinki-NLP/opus-mt-tc-bible-big-inc-en", "mr": "Helsinki-NLP/opus-mt-tc-bible-big-inc-en",
    "gu": "Helsinki-NLP/opus-mt-tc-bible-big-inc-en", "bn": "Helsinki-NLP/opus-mt-tc-bible-big-inc-en",
    "ur": "Helsinki-NLP/opus-mt-tc-bible-big-inc-en", "ta": "Helsinki-NLP/opus-mt-tc-bible-big-dra-en",
    "te": "Helsinki-NLP/opus-mt-tc-bible-big-dra-en", "kn": "Helsinki-NLP/opus-mt-tc-bible-big-dra-en",
    "ml": "Helsinki-NLP/opus-mt-tc-bible-big-dra-en", "pa": "Helsinki-NLP/opus-mt-pa-en",
    "es": "Helsinki-NLP/opus-mt-es-en",
}
MARKER = "zoomlt-mt.json"
_SENT_SPLIT = re.compile(r"(?<=[.!?।؟])\s+")


def default_cache_dir(download_root: Optional[str] = None) -> Path:
    if download_root:
        return Path(download_root).expanduser()
    return Path(os.environ.get("ZOOMLT_CACHE", Path.home() / ".cache" / "zoomlt"))


def split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def detect_layout(model_dir: Path) -> Optional[str]:
    """'marian' (source.spm + target.spm), 'argos' (sentencepiece.model) or None."""
    if not (model_dir / "model.bin").exists():
        return None
    if (model_dir / "source.spm").exists() and (model_dir / "target.spm").exists():
        return "marian"
    if (model_dir / "sentencepiece.model").exists():
        return "argos"
    return None


class Translator:
    name = "abstract"

    def translate(self, text: str, source_lang: Optional[str]) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def supports(self, source_lang: Optional[str]) -> bool:  # pragma: no cover
        return False

    def prepare(self, languages: List[str]) -> Dict[str, bool]:
        """Resolve (download/load) the models for these languages up front, before the meeting."""
        return {lang: self.supports(lang) for lang in languages}


class NoopTranslator(Translator):
    name = "none"

    def translate(self, text: str, source_lang: Optional[str]) -> Optional[str]:
        return None

    def supports(self, source_lang: Optional[str]) -> bool:
        return False


# ---------------------------------------------------------------------------------------------
# ct2 engine
# ---------------------------------------------------------------------------------------------

class _Ct2Model:
    def __init__(self, model_dir: Path, layout: str, beam_size: int, threads: int = 0):
        import ctranslate2
        import sentencepiece as spm

        self.layout = layout
        self.beam_size = max(1, beam_size)
        self.translator = ctranslate2.Translator(str(model_dir), device="cpu", compute_type="auto",
                                                 inter_threads=1, intra_threads=threads)
        if layout == "marian":
            self.src = spm.SentencePieceProcessor(model_file=str(model_dir / "source.spm"))
            self.tgt = spm.SentencePieceProcessor(model_file=str(model_dir / "target.spm"))
        else:
            self.src = self.tgt = spm.SentencePieceProcessor(model_file=str(model_dir / "sentencepiece.model"))

    def translate(self, text: str) -> str:
        sentences = split_sentences(text)
        if not sentences:
            return ""
        batch = []
        for s in sentences:
            toks = self.src.encode(s, out_type=str)
            if self.layout == "marian":
                toks = toks + ["</s>"]
            batch.append(toks)
        results = self.translator.translate_batch(
            batch, beam_size=self.beam_size, max_decoding_length=256, replace_unknowns=True,
            max_batch_size=8,
        )
        out = [self.tgt.decode(r.hypotheses[0]).replace("▁", " ").strip() for r in results]
        return " ".join(o for o in out if o)


class Ct2Translator(Translator):
    name = "ct2"

    def __init__(self, cfg: MtConfig, cache_dir: Optional[Path] = None, threads: int = 0,
                 install_languages: Optional[List[str]] = None):
        try:
            import ctranslate2  # noqa: F401
            import sentencepiece  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("mt.engine = 'ct2' needs ctranslate2 and sentencepiece (pip install sentencepiece)") from exc
        self.cfg = cfg
        self.cache_dir = cache_dir or default_cache_dir() / "mt"
        self.threads = threads
        # Downloads happen only for these languages (None = any). The CLI passes the configured
        # languages so a mis-detected language can never trigger a download mid-meeting.
        self.install_languages: Optional[set] = set(install_languages) if install_languages is not None else None
        self._lock = threading.Lock()
        self._models: Dict[str, _Ct2Model] = {}
        self._missing: set = set()
        self._argos_index: Optional[list] = None

    # -- resolution ---------------------------------------------------------------------------

    def _pair_dir(self, src: str) -> Path:
        return self.cache_dir / f"{src}-{self.cfg.target}"

    def resolve(self, src: str) -> Optional[Path]:
        """Find (or fetch) a model directory for src -> target. None if nothing is available."""
        custom = (self.cfg.model_dirs or {}).get(src)
        if custom:
            p = Path(custom).expanduser()
            if detect_layout(p):
                return p
            log.warning("mt.model_dirs[%s] = %s is not a usable CTranslate2 model dir", src, custom)
        cached = self._pair_dir(src)
        if detect_layout(cached):
            return cached
        if not self.cfg.auto_install:
            return None
        if self.install_languages is not None and src not in self.install_languages:
            return None
        repo = (self.cfg.hf_repos or {}).get(src) or (DEFAULT_HF_REPOS.get(src) if src not in ARGOS_PREFERRED else None)
        if repo:
            p = self._fetch_hf(repo, cached)
            if p:
                return p
        p = self._fetch_argos(src, cached)
        if p:
            return p
        repo = DEFAULT_HF_REPOS.get(src)
        if repo and src in ARGOS_PREFERRED:
            return self._fetch_hf(repo, cached)
        return None

    def _fetch_hf(self, repo: str, dest: Path) -> Optional[Path]:
        try:
            from huggingface_hub import snapshot_download

            log.info("downloading MT model %s (one time)...", repo)
            snap = Path(snapshot_download(repo, allow_patterns=["*.bin", "*.json", "*.spm", "*.model", "*.txt"]))
            if detect_layout(snap) is None:
                log.warning("%s does not look like a CTranslate2 Marian model (need model.bin + source/target.spm)", repo)
                return None
            _copy_model(snap, dest, {"kind": "marian", "source": repo})
            return dest
        except Exception as exc:
            log.warning("could not fetch %s: %s", repo, exc)
            return None

    def _fetch_argos(self, src: str, dest: Path) -> Optional[Path]:
        try:
            import urllib.request

            if self._argos_index is None:
                with urllib.request.urlopen(ARGOS_INDEX_URL, timeout=30) as resp:
                    self._argos_index = json.load(resp)
            entry = next((e for e in self._argos_index
                          if e.get("from_code") == src and e.get("to_code") == self.cfg.target), None)
            if not entry:
                return None
            links = [l for l in entry.get("links", []) if l.startswith("http")]
            if not links:
                return None
            from zoomlt.asr_nemotron import download_file

            dest.parent.mkdir(parents=True, exist_ok=True)
            archive = dest.parent / f"{src}-{self.cfg.target}.argosmodel"
            log.info("downloading Argos %s->%s pack (one time)...", src, self.cfg.target)
            download_file(links[0], archive, label=f"Argos {src}->{self.cfg.target}")
            ok = _extract_argos(archive, dest)
            archive.unlink(missing_ok=True)
            if not ok:
                log.warning("Argos %s->%s pack uses a BPE tokenizer this engine cannot run; "
                            "convert an OPUS-MT model instead (zoomlt convert-mt)", src, self.cfg.target)
                return None
            return dest
        except Exception as exc:
            log.warning("could not fetch Argos pack for %s: %s", src, exc)
            return None

    # -- API -------------------------------------------------------------------------------------

    def _load(self, src: str) -> Optional[_Ct2Model]:
        if src in self._models:
            return self._models[src]
        if src in self._missing:
            return None
        model_dir = self.resolve(src)
        if model_dir is None:
            self._missing.add(src)
            hint = CONVERT_SUGGESTIONS.get(src)
            log.warning("no local MT model for %s->%s; captions show the transcript only.%s", src, self.cfg.target,
                        f" Convert one with: zoomlt convert-mt {hint} --lang {src}" if hint else "")
            return None
        t0 = time.monotonic()
        model = _Ct2Model(model_dir, detect_layout(model_dir) or "argos", self.cfg.beam_size, self.threads)
        log.info("loaded MT %s->%s from %s in %.1fs", src, self.cfg.target, model_dir, time.monotonic() - t0)
        self._models[src] = model
        return model

    def supports(self, source_lang: Optional[str]) -> bool:
        if not source_lang or source_lang == self.cfg.target:
            return False
        with self._lock:
            return self._load(source_lang) is not None

    def translate(self, text: str, source_lang: Optional[str]) -> Optional[str]:
        if not text.strip() or not source_lang or source_lang == self.cfg.target:
            return None
        with self._lock:
            model = self._load(source_lang)
            if model is None:
                return None
            try:
                return model.translate(text)
            except Exception as exc:
                log.warning("MT failed: %s", exc)
                return None


def _copy_model(src_dir: Path, dest: Path, marker: dict) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for f in src_dir.iterdir():
        if f.is_file() and f.suffix in (".bin", ".json", ".spm", ".model", ".txt"):
            shutil.copyfile(f, dest / f.name)
    (dest / MARKER).write_text(json.dumps(marker))


def _extract_argos(archive: Path, dest: Path) -> bool:
    """Flatten an .argosmodel zip into dest. False if it is a BPE (non-SentencePiece) pack."""
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        spm_name = next((n for n in names if n.endswith("/sentencepiece.model") or n == "sentencepiece.model"), None)
        if spm_name is None:
            return False
        dest.mkdir(parents=True, exist_ok=True)
        for n in names:
            base = n.rsplit("/", 1)[-1]
            if n.endswith("/") or "stanza" in n:
                continue
            if "/model/" in n or n.startswith("model/") or n == spm_name or base in ("metadata.json", "README.md"):
                if ".." in n or n.startswith("/"):
                    continue
                with z.open(n) as src, open(dest / base, "wb") as out:
                    shutil.copyfileobj(src, out)
    (dest / MARKER).write_text(json.dumps({"kind": "argos", "source": archive.name}))
    return detect_layout(dest) == "argos"


def convert_hf_marian(repo: str, src: str, target: str = "en", cache_dir: Optional[Path] = None,
                      quantization: str = "int8") -> Path:
    """One-time conversion of a Hugging Face Marian model to CTranslate2 (needs transformers + torch)."""
    import subprocess
    import sys

    dest = (cache_dir or default_cache_dir() / "mt") / f"{src}-{target}"
    if dest.exists():
        shutil.rmtree(dest)
    cmd = [sys.executable, "-m", "ctranslate2.converters.transformers", "--model", repo,
           "--output_dir", str(dest), "--quantization", quantization]
    log.info("converting %s -> %s (%s)", repo, dest, " ".join(cmd))
    subprocess.run(cmd, check=True)
    from huggingface_hub import hf_hub_download

    for name in ("source.spm", "target.spm"):
        shutil.copyfile(hf_hub_download(repo, name), dest / name)
    (dest / MARKER).write_text(json.dumps({"kind": "marian", "source": repo, "converted": True}))
    if detect_layout(dest) != "marian":
        raise RuntimeError(f"conversion of {repo} did not produce a usable model in {dest}")
    return dest


# ---------------------------------------------------------------------------------------------
# argos engine (optional; the argostranslate package brings stanza + torch)
# ---------------------------------------------------------------------------------------------

class ArgosTranslator(Translator):
    name = "argos"

    def __init__(self, cfg: MtConfig):
        try:
            import argostranslate.package  # noqa: F401
            import argostranslate.translate  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("mt.engine = 'argos' needs argostranslate: pip install 'zoomlt[argos]'") from exc
        self.cfg = cfg
        self._lock = threading.Lock()
        self._missing: set = set()
        self._index_refreshed = False

    def _installed_pair(self, src: str) -> bool:
        import argostranslate.translate as tr

        langs = {l.code: l for l in tr.get_installed_languages()}
        if src not in langs or self.cfg.target not in langs:
            return False
        return langs[src].get_translation(langs[self.cfg.target]) is not None

    def _ensure_pair(self, src: str) -> bool:
        if src in self._missing:
            return False
        if self._installed_pair(src):
            return True
        if not self.cfg.auto_install:
            self._missing.add(src)
            return False
        import argostranslate.package as pkg

        try:
            if not self._index_refreshed:
                pkg.update_package_index()
                self._index_refreshed = True
            match = next((p for p in pkg.get_available_packages()
                          if p.from_code == src and p.to_code == self.cfg.target), None)
            if match is None:
                log.warning("Argos has no %s->%s package; showing the transcript only for %s", src, self.cfg.target, src)
                self._missing.add(src)
                return False
            t0 = time.monotonic()
            log.info("downloading Argos %s->%s package (one time)...", src, self.cfg.target)
            pkg.install_from_path(match.download())
            log.info("installed Argos %s->%s in %.0fs", src, self.cfg.target, time.monotonic() - t0)
            return self._installed_pair(src)
        except Exception as exc:
            log.warning("could not install Argos %s->%s: %s", src, self.cfg.target, exc)
            self._missing.add(src)
            return False

    def supports(self, source_lang: Optional[str]) -> bool:
        if not source_lang or source_lang == self.cfg.target:
            return False
        with self._lock:
            return self._ensure_pair(source_lang)

    def translate(self, text: str, source_lang: Optional[str]) -> Optional[str]:
        if not text.strip() or not self.supports(source_lang):
            return None
        import argostranslate.translate as tr

        with self._lock:
            try:
                return tr.translate(text, source_lang, self.cfg.target).strip()
            except Exception as exc:
                log.warning("Argos translation failed: %s", exc)
                return None


class FakeTranslator(Translator):
    """Tests: tags the input and records calls."""

    name = "fake"

    def __init__(self, langs: Optional[set] = None):
        self.langs = langs if langs is not None else {"es", "hi"}
        self.calls: list = []

    def supports(self, source_lang: Optional[str]) -> bool:
        return source_lang in self.langs

    def translate(self, text: str, source_lang: Optional[str]) -> Optional[str]:
        self.calls.append((text, source_lang))
        if not self.supports(source_lang):
            return None
        return f"[{source_lang}->en] {text}"


def build_translator(cfg: MtConfig, install_languages: Optional[List[str]] = None) -> Translator:
    if cfg.engine == "none":
        return NoopTranslator()
    if cfg.engine == "ct2":
        return Ct2Translator(cfg, install_languages=install_languages)
    if cfg.engine == "argos":
        return ArgosTranslator(cfg)
    if cfg.engine == "fake":
        return FakeTranslator()
    raise ValueError(f"unknown mt.engine {cfg.engine!r}")
