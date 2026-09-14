# What this costs, and whether you need a server

## The cost of running it: nothing

There is no subscription, no per-minute charge, no API key, no account, and no usage meter. Every
piece is free and open source, and every piece runs on your own computer:

| Component | Licence | What it costs |
|---|---|---|
| The app itself | MIT | nothing |
| Whisper speech models | MIT | nothing |
| Nemotron 3.5 speech model | OpenMDW-1.1 | nothing |
| OPUS-MT translation models | Apache-2.0 | nothing |
| Argos translation packs | MIT / CC-BY per pack | nothing |
| Silero voice detector | MIT | nothing |
| Zoom caption push | Zoom's built-in feature | nothing |

The only spending is on hardware you already own and the electricity to run it. A meeting costs a
few watt-hours more than it otherwise would.

The models download once, from Hugging Face and GitHub, and total a bit under a gigabyte for
Spanish and Hindi. After that the app works with the network unplugged, apart from the optional
caption push to Zoom.

## About the prices mentioned in the other documents

Those are **research about the alternatives we rejected**, not charges you will pay. To be
explicit, none of the following is part of this app:

* **Zoom Realtime Media Streams**, roughly a cent per streaming minute, needs a paid Developer
  Pack. We do not use it: we capture audio from your speakers instead.
* **Zoom's own translated captions**, which need Business Plus, Enterprise, or the paid Translated
  Captions add-on. We do not use them: we generate captions locally.
* **Commercial speech and translation APIs**, priced per minute or per character. We use none.
  This was your hard requirement and the app honours it.

The pricing pages exist in [zoom-platform.md](zoom-platform.md) so that the decision is on the
record: someone reading the repository later can see what the paid routes would have bought and
why the free one was chosen.

## Do you need to deploy anything to the cloud?

**No.** There is no backend to deploy, and adding one would not help.

The reason is physical rather than technical. The audio you want translated exists only on the
computer that is playing the meeting. A server in a data centre cannot hear your Zoom call. The app
has to run where the sound is, which is your laptop. Everything else in the pipeline (the speech
model, the translation model, the caption window) then has no reason to live anywhere else.

There is also nothing for a server to do. The app has no accounts, no database, no shared state,
no scheduled jobs and no public API. It is one process that reads a sound device and opens a
browser window on `127.0.0.1`.

So: no DigitalOcean droplet, no load balancer, no managed database, no object storage, no domain,
no TLS certificate, no monthly bill.

## When a server would actually help, and what it would cost

Two situations, neither of which applies to a single person captioning their own meetings.

**1. Your laptop cannot keep up.** The measure is the real-time factor in the caption window; above
1.0 the captions fall behind. On a modern laptop with `--engine nemotron` it sits near 0.45, with
plenty of headroom, so this is unlikely. If it did happen, the fix in order of effort is
`--no-partials`, then `--engine nemotron`, then `--model base`. Moving the recogniser to a remote
machine would mean streaming your meeting audio off the laptop, which costs latency, adds a network
dependency mid-meeting and gives away the privacy property that made this design attractive. **The
app does not support a remote recogniser today**, and adding one is a real project, not a
configuration change.

**2. You want transcripts of many recorded meetings.** Batch work on recordings is genuinely suited
to a server, because there is no live audio involved. `zoomlt file recording.m4a --fast` already
does the work; a droplet would just do it for many files while you sleep.

If you want a machine for that second case, here is the DigitalOcean setup, with current prices
([DigitalOcean droplet pricing](https://www.digitalocean.com/pricing/droplets), billed per second
since January 2026):

| Droplet | vCPU / RAM | Price | Suitable for |
|---|---|---|---|
| Basic | 1 / 2 GB, $12/mo | too small | nothing here: the models need ~2 GB |
| Basic | 2 / 4 GB, $24/mo | workable | batch transcription of recordings, slowly |
| CPU-Optimized | 4 / 8 GB, $84/mo | comfortable | batch transcription at several times real time |
| General Purpose | 4 / 16 GB, $126/mo | roomy | several jobs at once |

Because billing is per second, a batch job that runs for two hours a month costs cents, not the
monthly figure. Create it and destroy it around the work.

```bash
# on your laptop: create the droplet (doctl is DigitalOcean's CLI, `brew install doctl`)
doctl compute droplet create zoomlt-batch \
  --image ubuntu-24-04-x64 --size c-4 --region nyc3 \
  --ssh-keys "$(doctl compute ssh-key list --format ID --no-header | head -1)" --wait

doctl compute droplet list zoomlt-batch      # note the public IP
ssh root@<ip>

# on the droplet
apt update && apt install -y python3-venv python3-pip git ffmpeg
git clone https://github.com/awfmilton/zoom-translate && cd zoom-translate
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
zoomlt download --engine nemotron -l es -l hi

# copy recordings up, transcribe, copy transcripts back
# (from your laptop: scp meeting.m4a root@<ip>:~/ )
zoomlt file ~/meeting.m4a --fast --engine nemotron -l es -l hi \
  --no-browser --port 0 --transcript ~/meeting.jsonl

# when the batch is done, from your laptop:
doctl compute droplet delete zoomlt-batch
```

Keep the droplet firewalled: this app's caption window is meant for `127.0.0.1`, so do not open
port 8765 to the internet. For batch work you never need to; read the JSONL transcript over SSH.

A GPU droplet is not worth it unless you are processing many hours of audio: it would let you run
Whisper `large-v3`, but the CPU models here already run faster than real time and the quality gap
for Spanish and Hindi is small once you are using Nemotron.
