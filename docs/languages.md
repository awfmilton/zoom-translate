# What to expect in each language

Everything below was measured on real speech in this repository's test clips, with the models the
app actually ships with. Where a published benchmark is quoted it is named. "Gist" means the
sentence's meaning survives but the wording drifts; "verbatim" means you could quote it.

## The short version

```bash
zoomlt run --engine nemotron -l es -l hi     # Spanish, Hindi, English
zoomlt run -l pa                             # Punjabi and the other Indian languages
zoomlt run -l ta -l te                       # any mix Whisper knows
```

| Language | Engine to use | Mode | English quality | Original-language text |
|---|---|---|---|---|
| Spanish | `nemotron` | dual | verbatim | yes, accurate |
| Hindi | `nemotron` | dual | verbatim | yes, accurate |
| English (Indian or other accent) | `nemotron` | dual | passthrough | yes, accurate |
| Urdu | `whisper` | translate | gist | weak |
| Tamil | `whisper` | translate | gist | fair |
| Punjabi | `whisper` | **translate only** | gist | no, do not use |
| Marathi, Gujarati, Kannada | `whisper` | **translate only** | gist | no, do not use |
| Telugu, Malayalam, Bengali | `whisper` | **translate only** | weak gist | no, do not use |
| Anything else Whisper knows (99 languages) | `whisper` | translate | varies | varies |

The app warns you at startup if you ask for transcribe or dual mode in a language where Whisper's
transcription is unreliable, and its filters drop the specific garbage those cases produce.

## Punjabi

Punjabi is supported through Whisper's direct-to-English translation, which is the default mode.
Use it exactly like any other language:

```bash
zoomlt run -l pa                    # Punjabi meeting
zoomlt run -l pa -l hi -l en        # a mix, which is common
zoomlt run -l pa --model medium     # better, if the machine can keep up
```

What this looks like in practice, on a real Punjabi sentence whose reference translation is
*"Anguri is the very new wife of the very old servant at the house of my neighbours' neighbours'
neighbours"*:

| Setting | Output |
|---|---|
| `--model small`, translate | "Anguri is a very new wife of the great old servant of the Gwandis." |
| `--model medium`, translate | "Anguri Mere is the new wife of his old servant, who lives in the house of my grandmothers." |
| any model, transcribe | unusable: a repeated Gurmukhi character, or Punjabi words spelled in Hindi script |

So: you will follow what a Punjabi speaker is saying, and you should not quote them from it. Names
and numbers are the least reliable part. OpenAI's own figures agree with what we see: Whisper
scores Punjabi at 15.7 BLEU for speech translation, in the same range as Urdu (17.2) and better
than Tamil (9.2), while its Punjabi *transcription* word error rate is around 100 %, which is why
this app steers Punjabi to translate mode.

**Do not use `--mode dual` or `--mode transcribe` for Punjabi.** The English line stays fine but the
Punjabi line underneath will be wrong, which is worse than not showing it. The app warns you if you
try.

**If you need accurate Punjabi text**, not just English, the options today are all more work than
they are worth for a meeting: Meta's Omnilingual ASR recognises the words but writes them in a mix
of Gurmukhi and Shahmukhi script (we tested it); AI4Bharat's IndicConformer is accurate and MIT
licensed but its download is gated behind a Hugging Face account. If Punjabi transcripts become
important, IndicConformer is the thing to add, and the engine interface in `zoomlt/asr.py` is
where it would slot in.

## Spanish and Hindi

These two are the app's best case, because Nemotron 3.5 covers them and it is far stronger than
Whisper on Hindi:

| | Nemotron | Whisper `small` |
|---|---|---|
| Spanish word error rate (FLEURS) | 4.1 % | 5.6 % |
| Hindi word error rate (FLEURS) | 6.8 % | 38.4 % |
| Measured real-time factor here | 0.43 to 0.47 | 1.1 to 2.6 |

On our Hindi test sentence Nemotron transcribed it essentially verbatim and the local translation
produced *"Women cannot be prevented from entering the dargah: Maharashtra trustee"*, against
Whisper `small`'s *"Women cannot stop anything from entering the village"*.

Nemotron only knows Spanish, Hindi and English. Asking it for another language is refused at
startup rather than silently mis-transcribed.

## Mixed-language meetings

If your colleagues switch between languages, pass every language you expect. The app picks one per
sentence rather than per meeting:

```bash
zoomlt run --engine nemotron -l es -l hi -l en
```

Hinglish, the very common mix of Hindi and English in one sentence, is handled the way the model
handles it: Nemotron usually writes the English words in English and the Hindi in Devanagari, then
the translation step passes the English through. It is the case most likely to produce an odd
caption, so keep `dual` mode on and glance at the original line when something reads strangely.

Restricting languages matters. Left to guess, the smaller Whisper models regularly mistake a few
seconds of Hindi for Tamil, Urdu or Japanese. Naming your languages removes that failure entirely,
and naming exactly one is both the fastest and the most stable choice when a meeting is monolingual.

## Adding a language the app does not handle well

Two independent pieces decide quality: the recogniser and the translator.

* **Recogniser.** Whisper covers 99 languages out of the box. For a language it handles badly, the
  engine interface in `zoomlt/asr.py` takes a new class with one `transcribe` method; the Nemotron
  engine in `zoomlt/asr_nemotron.py` is a 200-line worked example of adding one through sherpa-onnx.
* **Translator.** `zoomlt convert-mt <hugging-face-model> --lang <code>` converts any
  Helsinki-NLP OPUS-MT model for the `dual` mode. The strongest free choices are
  `opus-mt-tc-bible-big-inc-en` for Hindi, Marathi, Gujarati, Bengali and Urdu,
  `opus-mt-tc-bible-big-dra-en` for Tamil, Telugu, Kannada and Malayalam, and `opus-mt-pa-en` for
  Punjabi. See [models.md](models.md) for how they scored.
