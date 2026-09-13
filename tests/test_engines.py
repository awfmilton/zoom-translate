"""Engine selection, Nemotron helpers and the ct2 MT plumbing (no models needed)."""

import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from zoomlt.asr import build_engine
from zoomlt.asr_nemotron import _safe_extract, guess_language
from zoomlt.config import Config
from zoomlt.mt import (Ct2Translator, MtConfig, NoopTranslator, _extract_argos, build_translator,
                       detect_layout, split_sentences)


def test_nemotron_forces_dual_mode_and_checks_languages():
    cfg = Config.from_dict({"asr": {"engine": "nemotron", "languages": ["es", "hi"]}})
    assert cfg.asr.mode == "dual"
    with pytest.raises(ValueError, match="es, hi, en only"):
        Config.from_dict({"asr": {"engine": "nemotron", "languages": ["ta"]}})
    with pytest.raises(ValueError, match="asr.engine"):
        Config.from_dict({"asr": {"engine": "vosk"}})


def test_build_engine_dispatch():
    from zoomlt.asr import FakeEngine

    assert isinstance(build_engine(Config.from_dict({"asr": {"engine": "fake"}}).asr), FakeEngine)
    with pytest.raises(ValueError):
        from zoomlt.config import AsrConfig

        build_engine(AsrConfig(engine="nope"))


def test_guess_language_by_script_and_stopwords():
    assert guess_language("महिलाओं को दरगाह में", ["es", "hi"]) == "hi"
    assert guess_language("El resto de los equipos", ["es", "hi"]) == "es"
    assert guess_language("we will ship the feature next week", ["es", "en"]) == "en"
    assert guess_language("el equipo de ventas y la oficina", ["es", "en"]) == "es"
    assert guess_language("anything", ["hi"]) == "hi"
    assert guess_language("hola", []) in ("es", "en")


def test_split_sentences_handles_devanagari_danda():
    assert split_sentences("Hola. ¿Cómo estás?  Bien.") == ["Hola.", "¿Cómo estás?", "Bien."]
    assert split_sentences("पहला वाक्य। दूसरा वाक्य।") == ["पहला वाक्य।", "दूसरा वाक्य।"]
    assert split_sentences("   ") == []
    assert split_sentences("no punctuation here") == ["no punctuation here"]


def test_detect_layout(tmp_path):
    assert detect_layout(tmp_path) is None
    (tmp_path / "model.bin").write_bytes(b"x")
    assert detect_layout(tmp_path) is None
    (tmp_path / "sentencepiece.model").write_bytes(b"x")
    assert detect_layout(tmp_path) == "argos"
    (tmp_path / "source.spm").write_bytes(b"x")
    (tmp_path / "target.spm").write_bytes(b"x")
    assert detect_layout(tmp_path) == "marian"


def test_extract_argos_flattens_spm_packs_and_rejects_bpe(tmp_path):
    spm_pack = tmp_path / "hi_en.argosmodel"
    with zipfile.ZipFile(spm_pack, "w") as z:
        z.writestr("hi_en/metadata.json", json.dumps({"from_code": "hi", "to_code": "en"}))
        z.writestr("hi_en/sentencepiece.model", b"spm")
        z.writestr("hi_en/model/model.bin", b"bin")
        z.writestr("hi_en/model/shared_vocabulary.txt", b"vocab")
        z.writestr("hi_en/stanza/hi/tokenize/x.pt", b"ignored")
    dest = tmp_path / "hi-en"
    assert _extract_argos(spm_pack, dest) is True
    assert sorted(p.name for p in dest.iterdir()) == ["metadata.json", "model.bin", "sentencepiece.model",
                                                     "shared_vocabulary.txt", "zoomlt-mt.json"]
    assert detect_layout(dest) == "argos"

    bpe_pack = tmp_path / "es_en.argosmodel"
    with zipfile.ZipFile(bpe_pack, "w") as z:
        z.writestr("translate-es_en-1_9/bpe.model", b"bpe")
        z.writestr("translate-es_en-1_9/model/model.bin", b"bin")
    assert _extract_argos(bpe_pack, tmp_path / "es-en") is False


def test_ct2_resolution_prefers_custom_then_cache_and_never_downloads_when_disabled(tmp_path):
    custom = tmp_path / "custom"
    custom.mkdir()
    for n in ("model.bin", "source.spm", "target.spm"):
        (custom / n).write_bytes(b"x")
    cache = tmp_path / "cache"
    tr = Ct2Translator(MtConfig(engine="ct2", auto_install=False, model_dirs={"hi": str(custom)}), cache_dir=cache)
    assert tr.resolve("hi") == custom
    assert tr.resolve("es") is None                     # auto_install off: nothing fetched
    cached = cache / "es-en"
    cached.mkdir(parents=True)
    for n in ("model.bin", "sentencepiece.model"):
        (cached / n).write_bytes(b"x")
    assert tr.resolve("es") == cached
    assert tr.supports("en") is False and tr.translate("hi", "en") is None


def test_build_translator_dispatch():
    assert isinstance(build_translator(MtConfig(engine="none")), NoopTranslator)
    assert isinstance(build_translator(MtConfig(engine="ct2")), Ct2Translator)
    with pytest.raises(ValueError):
        build_translator(MtConfig(engine="nope"))


def test_safe_extract_refuses_traversal(tmp_path):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("../../etc/evil")
        info.size = 0
        tar.addfile(info)
    with tarfile.open(archive) as tar, pytest.raises(RuntimeError, match="refusing"):
        _safe_extract(tar, tmp_path / "out")


def test_ct2_does_not_download_for_unconfigured_languages(tmp_path, monkeypatch):
    tr = Ct2Translator(MtConfig(engine="ct2", auto_install=True), cache_dir=tmp_path, install_languages=["es"])
    calls = []
    monkeypatch.setattr(tr, "_fetch_hf", lambda repo, dest: calls.append(("hf", repo)))
    monkeypatch.setattr(tr, "_fetch_argos", lambda src, dest: calls.append(("argos", src)))
    assert tr.resolve("hi") is None and calls == []
    tr.resolve("es")
    assert calls and calls[0][0] == "hf"
