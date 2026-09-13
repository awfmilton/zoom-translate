# Model choices, and the measurements behind them

All figures below were measured on this project's test clips on a 4-core cloud CPU (int8, 4 threads)
unless a source is cited. Real-time factor (RTF) is decode time divided by audio duration; anything
under 1.0 keeps up with a live meeting, and the practical target is under about 0.5 so that partial
captions have headroom.

## Speech recognition

| Engine | Spanish | Hindi | Measured RTF | Notes |
|---|---|---|---|---|
| Whisper `base` (int8) | transcribes, weak translation | **fails**: wrong script, invented content | 0.3–1.9 | not usable for Indian languages |
| Whisper `small` (int8) | good | marginal; drops words | 0.4–1.4 | the zero-setup default, all 99 languages |
| Nemotron 3.5 streaming 0.6B | near-verbatim | near-verbatim | **0.43–0.47** | es/hi/en only; transcribes, needs local MT |

Published word error rates agree with what we saw: OpenAI's FLEURS numbers give Whisper `small` a
38 % word error rate on Hindi (large-v2: 21.5 %), while NVIDIA reports 6.8 % for Nemotron 3.5 on the
same benchmark, and 4.1 % against Whisper large-v2's 3.0 % for Spanish.

Concretely, on one Hindi test sentence:

* Whisper `base`, translate task: *"The ancient era of the Prophet, the ancient era of the Prophet…"* — a hallucinated loop.
* Whisper `small`, translate task: *"But the spiritual tradition not only denies the existence of a Brahmin-Ramanand…"* — roughly on topic, details wrong.
* Nemotron + local translation: *"But modern scholarship denies not only the Brahman as a guru of Kabir, but also the contemporary of both"* — close to the reference.

That is why `--engine nemotron` is recommended whenever the meeting is Spanish, Hindi or English,
and Whisper remains the default for everything else (Tamil, Telugu, Marathi, Gujarati, Kannada,
Malayalam, Bengali, Punjabi, Urdu and 80+ more).

Rejected alternatives: `large-v3-turbo` and `distil-large-v3` cannot translate (OpenAI: "the turbo
model is not trained for translation tasks"); Parakeet and Canary cover no Indian language; Vosk's
Hindi models are far weaker; Voxtral Realtime needs a 16 GB GPU; SeamlessM4T is non-commercial.
Meta's Omnilingual ASR (Apache-2.0, 1 600+ languages, int8 ONNX for sherpa-onnx) is the strongest
candidate for adding the other Indian languages later.

## Translation (mode `dual`)

| Model | Size (int8) | Latency per sentence | Hindi quality |
|---|---|---|---|
| `opus-mt-hi-en` (old base model) | 157 MB | 0.29–0.78 s | weak: *"Women can't stop getting into the post-Prenuary."* |
| Argos `hi_en` pack | 116 MB | 0.18–0.57 s | fair: *"Women cannot stop getting into the doorstep."* |
| **`opus-mt-tc-bible-big-inc-en`** | 237 MB | 0.41–0.68 s | good: *"Women cannot be prevented from entering dargah: Maharashtra trustee"* |
| `opus-mt-es-en` (Spanish) | 160 MB | 0.35–0.50 s | good |

The transformer-big Indo-Aryan model scores 40.6 BLEU on FLORES-200 Hindi→English against 13.6 for
the old pair model, and it covers Marathi, Gujarati, Bengali and Urdu too; its Dravidian sibling
covers Tamil, Telugu, Kannada and Malayalam. Neither is published in CTranslate2 form, so
`zoomlt convert-mt` converts one in a single command, and the app falls back to models that need no
conversion (Hugging Face CTranslate2 conversions for Spanish, Argos packs for Hindi/Bengali/Urdu)
so the default install needs no extra step.

Rejected: NLLB-200 distilled 600M is good but CC-BY-NC (non-commercial) and 0.4–1.0 s per sentence;
MADLAD-400-3B is far too large; small local LLMs through llama.cpp took 3–7 s per sentence, 15–25×
slower than a dedicated translation model with no quality gain; Mozilla's Bergamot models are the
smallest but their runtime bindings are unmaintained and CTranslate2 cannot load them.

## Voice activity detection

Silero VAD v6, which ships inside faster-whisper, run frame by frame with its recurrent state
carried across 32 ms frames. It is the cheapest component in the pipeline and the one that does the
most for quality: only audio it marks as speech ever reaches the recogniser, which is the main
reason the app does not emit hallucinated captions during silence.
