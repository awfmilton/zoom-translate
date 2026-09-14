# zoom-translate

Live English captions for Zoom meetings where colleagues speak **Spanish, Hindi or other Indian
languages**, generated entirely on your own computer. No translation API, no AI subscription,
no per-minute fees: the only cost is the laptop (or a small box on your network) that runs it.

```
Zoom speaker output ──► loopback capture ──► Silero VAD ──► speech recognition ──► (local MT) ──► overlay
   (what you hear)     (no virtual cable    (who's talking)  Whisper or Nemotron     OPUS-MT on      browser window,
                        on Windows/Linux)                    on CPU                  CTranslate2     or Zoom's own captions
```

* **Audio in:** the app records what Zoom plays to your speakers. Windows: WASAPI loopback, nothing
  to install. Linux: the PulseAudio/PipeWire monitor. macOS: the free BlackHole virtual device.
* **Speech → text → English**, two engines, both free and offline after a one-time model download:
  * **Whisper** (default) via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), CTranslate2
    int8 on CPU. Its built-in *translate* task turns Spanish, Hindi, Tamil, Telugu, Marathi, Gujarati,
    Kannada, Malayalam, Bengali, Punjabi, Urdu and 80+ other languages into English in one pass.
    [docs/languages.md](docs/languages.md) says what each one is actually like in practice.
  * **Nemotron** for Spanish and Hindi: NVIDIA's Nemotron 3.5 streaming recogniser through
    [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), about 4 % word error rate on Spanish and
    7 % on Hindi (Whisper `small` is around 38 % on Hindi), faster than Whisper on CPU, then a local
    OPUS-MT translation model on CTranslate2. Recommended when your team speaks es/hi/en.
* **Captions out:** a local web page (`http://127.0.0.1:8765/`) you keep on top of Zoom. Words appear
  while the person is still talking (partials) and settle when they pause (finals). Optionally the
  same captions are pushed *into* Zoom for every participant through Zoom's free third-party
  caption API (no developer account, no Marketplace app).

Everything is open source (MIT / Apache-2.0 / OpenMDW models). Audio never leaves the machine.

---

## Quick start

Step-by-step instructions for each operating system are in **[INSTALL.md](INSTALL.md)**. In brief:
Python 3.10+, ~2 GB RAM for the default model, ~1 GB disk for models. Linux also
needs the PortAudio library for the `sounddevice` backend (`sudo apt install libportaudio2`, Fedora:
`sudo dnf install portaudio`); the default Linux capture path (`parec`) does not need it.

```bash
git clone https://github.com/awfmilton/zoom-translate
cd zoom-translate
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -e ".[windows]"      # Windows (adds WASAPI loopback capture)
pip install -e .                 # macOS / Linux

zoomlt check                     # effective config + the checks that otherwise fail mid-meeting
zoomlt devices                   # which device carries Zoom's audio (see "Routing Zoom audio")
zoomlt run -l es -l hi           # start; the overlay opens in your browser
```

**Cost: nothing, ever.** No subscription, no API key, no account, no server, nothing to deploy.
[docs/cost-and-hosting.md](docs/cost-and-hosting.md) spells that out, including what the prices
quoted in the other documents refer to.

Spanish + Hindi team? Use the accurate engine (one-time ~750 MB download):

```bash
zoomlt run --engine nemotron -l es -l hi
```

Try it **without a meeting** on any audio file (a voice memo, a recorded call):

```bash
zoomlt file recording.mp3 -l es
zoomlt file recording.mp3 --engine nemotron -l hi
```

`Ctrl+C` stops. `--transcript meeting.jsonl` keeps every final caption (file created private to your
user). `zoomlt download` fetches the models ahead of a meeting. Exit codes: 2 for a configuration or
device problem reported before the meeting starts, 3 if the pipeline died mid-run.

### What `-l` does

`-l es -l hi` tells the recogniser the meeting is Spanish and/or Hindi. Language *detection* on short
utterances is unreliable (a few seconds of Hindi is regularly guessed as Tamil, Urdu or Japanese by
the smaller Whisper models); restricting it to the languages your team speaks removes that whole
class of errors. One `-l` when everyone speaks the same language, several when they mix, none to
let Whisper guess. Whisper accepts any of its 99 language codes; Nemotron accepts `es`, `hi`, `en`.

---

## Routing Zoom audio into the app

Zoom's sound goes to your speakers/headphones. The app has to hear that same stream, not your
microphone. It warns at startup if the chosen device looks like a microphone.

### Windows (nothing to install)

`pip install -e ".[windows]"` adds [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch), which
records an *output* device through WASAPI loopback. `zoomlt run` picks the default speakers
automatically. If Zoom plays through another device (a headset), pass part of its name:

```bash
zoomlt devices                       # loopback devices are marked
zoomlt run --device "Headset" -l es
```

Loopback captures **everything** the device plays, so mute music and notification sounds during the
meeting. When nothing at all is playing, Windows can pause the loopback stream; the app logs
"no audio from ... for 5 s" so you can tell the difference between a quiet meeting and a wrong device.

### macOS (BlackHole, free)

macOS has no built-in loopback. Install the free [BlackHole](https://github.com/ExistentialAudio/BlackHole)
virtual device (`brew install --cask blackhole-2ch`, reboot if asked), then:

1. **Audio MIDI Setup** → `+` → *Create Multi-Output Device*. Tick your real output (speakers or
   headphones) **first**, then *BlackHole 2ch*, and enable *Drift Correction* on BlackHole.
2. Make the Multi-Output Device the system output (Sound settings, or Option-click the volume icon).
   You still hear the meeting; BlackHole gets a copy. (The volume keys stop working for a
   Multi-Output Device; set the level on the real output beforehand.)
3. `zoomlt run --device BlackHole -l es`

macOS asks for **Microphone** access for your terminal app (Terminal, iTerm, VS Code) the first
time any input device is opened, BlackHole included. Allow it, or the app hears silence (System
Settings → Privacy & Security → Microphone). Zoom's microphone setting stays on your real mic; only
the speaker output changes.

### Linux (PulseAudio / PipeWire)

The default sink's *monitor* carries exactly what the speakers play. If `parec` is installed
(`pulseaudio-utils`, works with PipeWire through pipewire-pulse) the app uses it automatically:

```bash
zoomlt run -l hi                                  # parec -d @DEFAULT_MONITOR@ under the hood
zoomlt run --backend parec --device alsa_output.pci-0000_00_1f.3.analog-stereo.monitor -l hi
zoomlt run --backend sounddevice --device "Monitor of" -l hi    # PortAudio route instead
```

`pactl list short sources` lists monitor names; `pavucontrol` → *Recording* can move a running
capture to another source.

---

## Keeping the captions on top of Zoom

The overlay is a normal browser tab, so use whatever keeps a window on top:

* **Windows:** [PowerToys](https://learn.microsoft.com/windows/powertoys/) *Always On Top*
  (`Win+Ctrl+T`), or dock the browser beside Zoom.
* **macOS:** a small browser window next to Zoom; Chrome/Edge users can pop the page into a
  floating *Document Picture-in-Picture* window from the browser's media controls.
* **Any OS:** a second monitor, or a phone/tablet on the same Wi-Fi: `zoomlt run --host 0.0.0.0 ...`
  prints a URL with a per-run token for other devices. The page is served without encryption, so
  only do this on a network you trust.

`http://127.0.0.1:8765/?compact=1` shows only the last three lines with no toolbar: a narrow strip
along the bottom of the screen. Keys: `space` pause, `c` compact, `o` show the original text
(dual mode), `+`/`-` font size. The toolbar's *copy* button copies the whole transcript.

The toolbar also carries an **input meter**. If it stays dark while someone is speaking, the app is
capturing the wrong device: that is the single most common setup mistake, and the terminal says so
after twenty seconds of digital silence. A dropped connection shows as a yellow "reconnecting" band,
visible in compact mode too.

### Pushing captions into Zoom itself (optional)

Zoom's *third-party closed captioning* feature accepts captions from any program and shows them to
every participant behind the normal **Show Captions** button. It is free; the **host** enables it:

1. Zoom web settings → *In Meeting (Advanced)* → **Manual captions** on → tick *Allow use of caption
   API token to integrate with 3rd-party closed captioning services*.
2. In the meeting: **Show Captions** → up-arrow → *Set up manual captioner* → **Copy the API token**
   (the host can also assign you as captioner so you copy it yourself).
3. Give the token to the app. It is a secret (anyone holding it can write captions into your
   meeting), so prefer the environment or the config file over the command line, which shows up
   in process lists and shell history:

   ```bash
   export ZOOMLT_ZOOM_CC_URL="https://wmcc.zoom.us/closedcaption?id=...&signature=..."   # or [zoom_cc] in config.toml
   zoomlt run -l es
   ```

Only *final* captions are pushed, at most one request per 0.8 s (each post replaces the caption
line; Zoom shows two lines). Participants have to click *Show Captions*. Zoom counts captions with a
sequence number and drops anything out of order, so the counter is remembered per meeting and
continues if you restart the app mid-meeting. The token only works for that meeting; `zoomlt check`
hides it, nothing logs it, and only a hash of it is written to disk. `config.toml` and `*.jsonl`
transcripts are git-ignored for the same reason.

Why not Zoom's own translated captions? They need a Business Plus / Enterprise plan or the paid
Translated Captions add-on. Zoom's Meeting SDK raw-audio access needs a Marketplace app and, since
2026, per-meeting tokens; Realtime Media Streams is a paid developer pack. Loopback capture plus the
free caption API is the only route that costs nothing and works in anyone's meeting.

---

## Choosing the engine and model

The pipeline waits for the speaker to pause (0.6 s by default), then decodes the sentence. Caption
delay ≈ **pause + decode time**, and decode time depends on the engine and CPU. The overlay's status
line shows the real-time factor (RTF): it must stay **below 1** or captions drift behind.

### Nemotron 3.5 (`--engine nemotron`), for Spanish / Hindi / English

| | |
|---|---|
| download | ~650 MB recogniser + ~100–160 MB per translation model, one time |
| RAM | ~1.5 GB |
| quality | FLEURS WER: Spanish 4.1 %, Hindi 6.8 % (Whisper large-v2: 3.0 % / 21.5 %; Whisper small: 5.6 % / 38.4 %) |
| CPU speed | RTF ≈ 0.12–0.45 measured (int8, 4 threads) |
| licence | OpenMDW-1.1 (model), Apache-2.0 (sherpa-onnx) |

It transcribes, so the app runs it in *dual* mode: the overlay shows the English translation with
the original underneath (press `o`). Translation uses the `ct2` engine below. On this project's test
clips it transcribed Hindi and Spanish nearly verbatim where Whisper `small` mangled the Hindi.

### Whisper (`--engine whisper`, default), for every language

| model | download | RAM | Spanish → English | Hindi → English | typical laptop CPU RTF |
|---|---|---|---|---|---|
| `tiny` / `base` | 75 / 145 MB | <1 GB | rough | unusable | ~0.1 |
| **`small`** (default) | 470 MB | ~1.5 GB | good | marginal (WER ~38 %, translation BLEU ~11) | ~0.3–0.6 |
| `medium` | 1.5 GB | ~3 GB | very good | usable (WER ~27 %) | ~1–2, GPU advised |
| `large-v3` | 3 GB | ~5 GB | best | best Whisper has (WER ~20 %) | GPU only |

Figures are OpenAI's FLEURS numbers for Whisper large-v2/medium/small; Tamil and Urdu are similar to
Hindi, while Whisper is close to useless for Telugu, Malayalam, Gujarati, Bengali and Punjabi.

* **CPU laptop:** `small`; if RTF creeps above 1 add `--no-partials` (only finished sentences, about
  half the CPU), then drop to `base`.
* **NVIDIA GPU:** `--model large-v3 --asr-device cuda` (CUDA 12 + cuDNN 9 runtime; see the
  faster-whisper README). `medium --compute-type int8_float16` is a middle ground.
* **Apple Silicon:** CTranslate2 runs on the CPU cores; `small` is comfortable on M1 and newer.
* **`large-v3-turbo` / `distil-large-v3`** are transcription-only and cannot translate; the app
  refuses them in translate mode. Use them with `--mode dual`.

### Modes

* `translate` (Whisper default): one pass, English out. Fastest; every Whisper language.
* `transcribe`: original-language text only.
* `dual` (Nemotron default): transcribe, then translate the text locally; shows both.

### Local translation models (`[mt]`, engine `ct2`)

Text translation runs on CTranslate2 with SentencePiece, both already installed. Models are fetched
once, per source language, in this order: your own directory from `mt.model_dirs`, a previously
converted model, an Apache-2.0 OPUS-MT conversion on Hugging Face (Spanish), then an Argos Translate
pack (Hindi, Bengali, Urdu; no torch or stanza needed). A sentence takes 0.1–0.5 s on a laptop CPU.

Best Hindi quality is the Helsinki-NLP *tc-bible-big-inc-en* model (FLORES BLEU 40.6 vs 13.6 for the
old opus-mt-hi-en). It is not published in CTranslate2 form, so convert it once yourself:

```bash
pip install -e ".[convert]"                    # transformers + torch, only for the conversion
zoomlt convert-mt Helsinki-NLP/opus-mt-tc-bible-big-inc-en --lang hi
zoomlt convert-mt Helsinki-NLP/opus-mt-tc-bible-big-dra-en --lang ta   # Tamil/Telugu/Kannada/Malayalam
```

The converted model lands in `~/.cache/zoomlt/mt/<lang>-en/` and is used automatically from then on.
Languages without any local model show the transcript only (Whisper's translate mode remains the
zero-setup route for those).

### Tuning knobs (`config.example.toml`)

* `vad.end_silence_ms` – 600 ms default. 400 feels snappier but splits sentences; 900 gives more
  complete sentences with more delay.
* `asr.partial_interval_ms` – how often the in-progress sentence is re-decoded (1.2 s). `0` = off.
* `asr.hotwords` / `asr.initial_prompt` – names and product terms Whisper should spell correctly.
* `asr.beam_size` – 1 (greedy) for speed; 3–5 on a fast machine. `mt.beam_size` likewise (2).
* `vad.threshold` – raise to 0.6 if background noise keeps triggering captions.

Copy `config.example.toml` to `config.toml`, edit, and run `zoomlt run -c config.toml`.

---

## How it works

1. **Capture** (`zoomlt/audio/capture.py`): WASAPI / PortAudio / parec → queue → 16 kHz mono
   (libswresample via PyAV). The device is opened before any model loads, so a wrong device fails
   fast. A live source drops its oldest 20 s if the consumer stalls, the stream clock is corrected
   for the gap, and driver overruns are counted and reported.
2. **VAD** (`zoomlt/vad.py`): Silero VAD v6 (the ONNX file that ships with faster-whisper), run
   frame-by-frame with its LSTM state carried across 32 ms frames.
3. **Segmenter** (`zoomlt/segmenter.py`): speech starts above 0.5 probability (with 320 ms pre-roll),
   ends after 600 ms of consecutive frames below 0.35 (hysteresis), and is force-cut at 15 s at the
   quietest recent frame. Only VAD-confirmed audio ever reaches the recogniser: the main defence
   against hallucinated captions on silence.
4. **ASR** (`zoomlt/asr.py`, `zoomlt/asr_nemotron.py`): Whisper with greedy decoding, no temperature
   fallback (retries are where CPU latency explodes), no cross-utterance conditioning (stops
   repetition loops), language restricted to `-l`, and per-segment hallucination filters. Nemotron
   keeps one streaming state per utterance, so partial captions only process the new audio.
5. **MT** (`zoomlt/mt.py`): CTranslate2 Marian / Argos models, sentence-split, batched.
6. **Pipeline** (`zoomlt/pipeline.py`): one decode worker. A newer partial replaces a waiting one, a
   final replaces its utterance's partial, stale partials are skipped, the partial cadence stretches
   to match the decoder's speed, and when the recogniser falls behind real time the oldest finals
   are shed (counted and shown in the overlay status) rather than queued forever.
7. **Server** (`zoomlt/server.py`): one port, static overlay + WebSocket. Only loopback `Host`
   headers are served (DNS-rebinding guard), cross-origin WebSocket handshakes are refused, and LAN
   mode requires a per-run token. Late-joining windows get the last 200 finals.

## Tests

```bash
pip install -e ".[test]"
pytest                                        # ~15 s: fake ASR engine, real VAD on bundled clips
ZOOMLT_INTEGRATION=1 pytest tests/test_integration_whisper.py    # downloads Whisper 'tiny'
ZOOMLT_INTEGRATION=1 pytest tests/test_integration_nemotron.py   # downloads Nemotron + MT models
```

`tests/fixtures/` holds two short CC0 clips from Mozilla Common Voice 17 (Spanish, Hindi).

## Privacy

Audio never leaves the machine. The only network calls are one-time model downloads (Hugging Face,
GitHub releases for the Nemotron archive, argos-net.com for Argos packs) and, only if you enable it,
the caption POSTs to Zoom's own servers.

## Further reading

* **[INSTALL.md](INSTALL.md)** — step-by-step setup for Windows, macOS and Linux, and what to do
  when something does not work.
* **[docs/zoom-setup.md](docs/zoom-setup.md)** — using it with Zoom, putting captions on screen for
  everyone, and whether you need a Zoom developer account (you do not).
* **[docs/languages.md](docs/languages.md)** — what to expect in each language, including Punjabi,
  Tamil, Telugu, Bengali, Gujarati, Kannada, Malayalam, Marathi and Urdu.
* **[docs/cost-and-hosting.md](docs/cost-and-hosting.md)** — what it costs (nothing), what the
  prices quoted elsewhere refer to, and when a server would help.
* **[docs/zoom-platform.md](docs/zoom-platform.md)** — which Zoom integration route this app uses
  and why the Meeting SDK, Realtime Media Streams and Zoom's translated captions were rejected.
* **[docs/models.md](docs/models.md)** — the measurements behind the engine and translation-model
  choices, including the rejected alternatives.

## Licence

MIT. Models: Whisper (MIT), Nemotron 3.5 ASR (OpenMDW-1.1), OPUS-MT (Apache-2.0), Argos packs
(CC-BY / MIT per pack), Silero VAD (MIT).
