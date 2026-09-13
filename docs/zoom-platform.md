# Which Zoom integration route this app uses, and why

Zoom offers several ways to get at meeting audio and to put text back on screen. Only one
combination is free, works in meetings you do not host, and needs no Marketplace app. That is the
combination this app implements.

## What we use

| Direction | Mechanism | Cost | Needs |
|---|---|---|---|
| Audio **in** | System-audio loopback of whatever Zoom plays to the speakers | free | nothing on Windows/Linux; the free BlackHole driver on macOS |
| Captions **out** (optional) | Zoom's third-party closed-caption **API token** | free | the host enables *Manual captions* and shares the token |

Loopback capture is outside Zoom entirely: the operating system lets us record the audio the Zoom
client is already playing. That means it works in any meeting we can hear, on any plan, with no
credentials, and Zoom never knows the app exists. The trade-off is that it captures every sound the
device plays, so other apps should be muted, and it cannot separate speakers.

The caption API token is Zoom's own documented integration point for third-party captioning
services. The host turns on *In Meeting (Advanced) → Manual captions → Allow use of caption API
token*, then copies a per-meeting URL from the in-meeting caption menu. Anything POSTed to that URL
as `text/plain`, with `seq` incrementing by exactly one per caption and `lang` set, appears behind
the participants' **Show Captions** button. Details that matter in practice:

* `seq` must not be reused or skipped. A failed POST must retry with the *same* number, and a
  restart mid-meeting must continue where it left off, which is why `zoomlt` persists the counter
  per meeting (hashed, never the token itself).
* Each POST replaces the caption line rather than appending, and Zoom renders about two lines.
* The token is a bearer credential for the meeting and expires with it. Keep it out of shell history
  and process lists: use `ZOOMLT_ZOOM_CC_URL` or `config.toml`.

## What we deliberately do not use

**Meeting SDK raw audio.** The native (C++) Meeting SDK can deliver raw PCM per participant, which
would be the technically ideal input. It requires a Zoom Marketplace app, host/co-host or local
recording rights, and since March 2026 a per-meeting authorization token for meetings hosted outside
the app owner's account. The Python bindings are unofficial and Linux-only. Too much friction and
too many permissions for a tool one person runs on their laptop.

**Realtime Media Streams (RTMS).** Zoom's 2025 streaming product delivers live media to your own
endpoint. It is metered: Zoom staff quote roughly $0.01 per streaming minute, and it needs an active
Developer Pack. The brief for this project was explicitly zero marginal cost, so RTMS is out.

**Zoom's own translated captions.** Zoom can translate Spanish and Hindi captions itself, but only
on Business Plus / Enterprise plans or with the paid Translated Captions add-on. Free accounts get
English automated captions only. If your organisation already pays for that, you do not need this
app; if it does not, this app gives you the same outcome for the price of the CPU it runs on.

**Local recording / "Share computer sound".** Recording is not real time, and sharing sound sends
audio out of your machine rather than capturing what comes in.

## Consequences for the design

* The app never authenticates to Zoom, so there is nothing to revoke and no OAuth flow to maintain.
* Because we capture the mixed output, speaker labels are impossible; captions are attributed by
  detected language instead.
* Zoom's echo cancellation and noise suppression run before playback, which actually helps: the
  audio we capture is already cleaned up.
* The user's own microphone is *not* in the loopback, so the app captions the people they are
  listening to and not themselves. That is usually what you want. To caption yourself as well,
  select a device that mixes both, or run a second instance on the microphone.
