# Installing zoom-translate

Start to finish this takes about fifteen minutes, most of it waiting for one download. You do not
need a Zoom developer account, a Zoom paid plan, a server, or any account with us or anyone else.
Everything runs on the computer you take your meetings on.

Jump to your system: **[Windows](#windows)** · **[macOS](#macos)** · **[Linux](#linux)**

---

## What you are installing

| Piece | What it is | Size |
|---|---|---|
| Python 3.10 or newer | the language the app is written in | ~30 MB |
| `zoomlt` and its libraries | this app | ~200 MB |
| A speech model | recognises what people say, runs offline | 470 MB (Whisper) or 650 MB (Nemotron) |
| Translation models | turn the words into English, run offline | 100 to 240 MB per language |

The models download once, from Hugging Face and GitHub, and are then cached on your disk. After
that the app never needs the internet, except to send captions to Zoom if you turn that on.

---

## Windows

**1. Install Python.** Get it from [python.org/downloads](https://www.python.org/downloads/) and,
on the first screen of the installer, tick **Add python.exe to PATH**. Then open PowerShell
(Start menu, type "PowerShell") and check it:

```powershell
python --version
```

You should see 3.10 or higher. If Windows opens the Microsoft Store instead, the PATH box was not
ticked: re-run the installer and choose Modify.

**2. Install the app.**

```powershell
git clone https://github.com/awfmilton/zoom-translate
cd zoom-translate
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[windows]"
```

No Git? Download the ZIP from the repository's green **Code** button, unzip it, and `cd` into the
folder instead of cloning.

The `[windows]` part adds PyAudioWPatch, which is what lets the app record what your speakers are
playing. Without it you would have to install a virtual audio cable by hand.

**3. Download the models** (one time, do it before a meeting, not during one):

```powershell
zoomlt download --engine nemotron -l es -l hi
```

**4. Check everything is ready.**

```powershell
zoomlt check
```

Lines marked `ok` are ready and lines marked `note` are advisory, such as a translation model that
has not been downloaded yet. Anything marked `MISSING` or `PROBLEM` needs fixing first; see
[Troubleshooting](#troubleshooting).

**5. Run it.** Start your Zoom meeting first, then:

```powershell
zoomlt run --engine nemotron -l es -l hi
```

A browser tab opens with the captions. Drag it beside Zoom, or keep it on top with
[PowerToys](https://learn.microsoft.com/windows/powertoys/) Always On Top (`Win+Ctrl+T`).

Windows needs no audio setup at all: the app captures whatever your default speakers play, which is
the meeting. Mute music and notifications, because it captures those too.

---

## macOS

macOS has no way to record its own sound output, so you install one free driver.

**1. Install Homebrew** if you do not have it, from [brew.sh](https://brew.sh), then:

```bash
brew install python@3.12
brew install --cask blackhole-2ch
```

BlackHole is a free, open-source virtual audio device. Restart your Mac if the installer asks.

**2. Route Zoom's sound through it.** Open **Audio MIDI Setup** (Spotlight, type its name):

1. Click **+** at the bottom left → **Create Multi-Output Device**.
2. Tick your real output first (MacBook Speakers or your headphones), then tick **BlackHole 2ch**.
3. Tick **Drift Correction** on the BlackHole row.
4. Rename it something like "Zoom + Captions" by double-clicking its name.

Then set that Multi-Output Device as your system output: click the volume icon in the menu bar with
the Option key held, or open System Settings → Sound → Output.

You still hear the meeting normally. BlackHole receives a silent copy for the app to read.

> The volume keys do not work while a Multi-Output Device is selected. Set your volume before
> switching to it, or change the level inside Zoom.

**3. Install the app.**

```bash
git clone https://github.com/awfmilton/zoom-translate
cd zoom-translate
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**4. Download the models and check.**

```bash
zoomlt download --engine nemotron -l es -l hi
zoomlt check
```

**5. Run it.** Start the meeting, then:

```bash
zoomlt run --engine nemotron -l es -l hi --device BlackHole
```

The first time, macOS asks whether your terminal may use the microphone. **Allow it.** BlackHole
counts as a microphone as far as macOS is concerned, and without permission the app hears nothing
but silence. If you clicked Don't Allow, fix it in System Settings → Privacy & Security →
Microphone.

Leave Zoom's own microphone setting pointing at your real microphone. Only the speaker output
changes.

---

## Linux

**1. Install the prerequisites.** On Ubuntu or Debian:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git pulseaudio-utils
```

On Fedora: `sudo dnf install python3 python3-pip git pulseaudio-utils`.

`pulseaudio-utils` provides `parec`, which the app uses to read the monitor of your speakers. It
works on PipeWire systems too, through pipewire-pulse, which is what modern Ubuntu and Fedora use.

**2. Install the app.**

```bash
git clone https://github.com/awfmilton/zoom-translate
cd zoom-translate
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**3. Download the models and check.**

```bash
zoomlt download --engine nemotron -l es -l hi
zoomlt check
```

**4. Run it.** Start the meeting, then:

```bash
zoomlt run --engine nemotron -l es -l hi
```

The app captures `@DEFAULT_MONITOR@`, which is whatever your current speakers are playing. To pick
a specific output, list them with `pactl list short sources` and pass the one ending in `.monitor`:

```bash
zoomlt run --device alsa_output.pci-0000_00_1f.3.analog-stereo.monitor -l hi
```

If `parec` is missing the app falls back to PortAudio, which needs `sudo apt install libportaudio2`
and rarely sees monitor sources. Installing `pulseaudio-utils` is the easier path.

---

## Which languages to use

Pass one `-l` per language your colleagues speak. Two engines cover different sets:

```bash
zoomlt run --engine nemotron -l es -l hi     # Spanish, Hindi, English: the accurate path
zoomlt run -l ta -l te                       # anything else: Whisper, 99 languages
```

See [docs/languages.md](docs/languages.md) for what to expect from each language, including Punjabi,
Tamil, Telugu, Bengali, Gujarati, Kannada, Malayalam, Marathi and Urdu.

## Trying it without a meeting

Record a voice memo in Spanish or Hindi, or grab any clip, and run:

```bash
zoomlt file my-recording.m4a --engine nemotron -l es
```

Captions print in the terminal and in the browser exactly as they would live. This is the fastest
way to confirm the install before you are in front of colleagues.

---

## Troubleshooting

**The captions never appear and the meter in the toolbar stays dark.** The app is listening to the
wrong device. Run `zoomlt devices`; rows marked as loopback or monitor are the ones that carry what
you hear. Pass part of the name with `--device`. On macOS, also check the microphone permission
above.

**"no loopback device found" from `zoomlt check`.** On Windows, `pip install PyAudioWPatch`. On
macOS, install BlackHole. On Linux, `sudo apt install pulseaudio-utils`.

**Captions arrive late and the status line shows rtf above 1.** The machine cannot keep up. In
order: add `--no-partials`, switch to `--engine nemotron` if your languages allow it, then drop to
`--model base`. See [docs/models.md](docs/models.md) for the measured numbers.

**`pip install` fails building something.** You are probably on Python 3.9 or older, or on a very
new release that has no wheels yet. Python 3.11 or 3.12 is the safest choice.

**The captions are nonsense in an Indian language.** Whisper is weak in several of them. Check
[docs/languages.md](docs/languages.md) for which engine to use for that language.

**It captures my own voice as well as theirs.** Your microphone is being mixed into the output you
are capturing, usually by a headset's sidetone or by "Original sound" in Zoom. Turn the sidetone
off, or capture the specific device Zoom plays into rather than a mix.
