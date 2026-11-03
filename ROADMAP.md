# zoom-translate — Engineering Roadmap

This document tracks the planned engineering roadmap across Google Meet support, audio capture resilience, multilingual expansion, security, infrastructure, and CI/CD test automation (**~68 hours of planned work**).

---

## Phase 1: Google Meet Live Captioning & Browser Companion Integration (~14 hours)

Extend `zoom-translate` beyond Zoom so users get seamless live captions and in-meeting subtitle delivery in **Google Meet** (`zoomlt/`):

- [ ] **Google Meet Caption Delivery & Companion Extension / WebSocket Bridge (`zoomlt/meet_cc.py`, `zoomlt/server.py`):**
  - Unlike Zoom's host-token HTTP POST endpoint (`zoomlt/zoom_cc.py`), Google Meet runs in the browser (`meet.google.com`). Build a lightweight companion browser extension / userscript bridge and `zoomlt/meet_cc.py` connector that pairs with `CaptionServer` (`http://127.0.0.1:8765/ws`) to inject translated captions directly into a docked Picture-in-Picture bar inside the Google Meet tab or broadcast them into the Meet chat/caption stream.
- [ ] **Per-Application Audio Capture Target (`zoomlt/audio/capture.py`):**
  - Add Windows 10/11 Process-Specific WASAPI Loopback (`AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`) and Linux PipeWire application node targeting (`zoomlt run --app chrome` / `--app zoom`) so capturing Google Meet in Chrome/Edge only records the browser's meeting audio without capturing other desktop notification sounds.
- [ ] **Google Meet Active Speaker Detection & Utterance Tagging:**
  - Optional DOM observer in the Google Meet companion script that reads the currently active speaker tile name on `meet.google.com` and tags incoming WebSocket captions with the speaker's name in the overlay and `.jsonl` / `.srt` transcript.

---

## Phase 2: Audio Device Hot-Plug Recovery & Subtitle Exports (`SRT` / `WebVTT`) (~10 hours)

- [ ] **Automatic Audio Device Reconnection (`zoomlt/audio/capture.py:179-305`):**
  - Handle headset unplugging, Bluetooth profile switches, and Windows default output device changes mid-meeting by automatically re-enumerating WASAPI / PortAudio devices and reopening the capture stream without dropping the pipeline.
- [ ] **Subtitle & Transcript Export (`zoomlt export` CLI & Overlay Download):**
  - Add `zoomlt export meeting.jsonl --format srt|vtt|md|txt` and a one-click download button in `zoomlt/overlay/index.html` producing standard `SRT` and `WebVTT` subtitle files with millisecond cue timestamps (`start_s` / `end_s`).
- [ ] **Virtualized Overlay Scrollback & Live Search (`zoomlt/overlay/app.js:62-64`):**
  - Replace the destructive 60-line DOM `trim()` in `app.js` with full session scrollback, keyword search, and persistent `compact` / theme settings in `localStorage`.

---

## Phase 3: Multilingual Expansion (Bidirectional & Additional Language Pairs) (~10 hours)

- [ ] **Expanded Target Languages & Bidirectional Translation (`zoomlt/mt.py`, `zoomlt/config.py`):**
  - Currently `zoomlt` translates *into* English (`--lang es`, `--lang hi` → `en`). Add `--target-lang` (`en`, `es`, `hi`, `fr`, `de`, `pt`, `ja`, `zh`, `ar`) so non-English participants can also read live captions in their own native language during multilingual Zoom and Google Meet calls.
- [ ] **Localized Overlay UI (`zoomlt/overlay/`):**
  - Localize the browser overlay toolbar, status indicators, and CLI diagnostic messages (`zoomlt check`, `zoomlt devices`) across English, Spanish, Hindi, French, German, and Portuguese.

---

## Phase 4: Supporter Donations & Open-Source Sponsorship (~4 hours)

- [ ] **Supporter Links & Optional Sponsor Tier:** Add GitHub Sponsors / Stripe donation links (`FUNDING.yml`, README, and subtle overlay footer link) so individuals and teams saving on per-minute cloud captioning fees can support ongoing maintenance.

---

## Phase 5: Security Review (~8 hours)

- [ ] **Caption Server & WebSocket Hardening (`zoomlt/server.py:126-168`):**
  - Add strict `Content-Security-Policy` (`default-src 'self'; connect-src 'self' ws: wss:`), `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY` headers to `CaptionServer._static()`.
  - Move LAN authentication token (`?t=...`) from URL query parameters into an `HttpOnly; SameSite=Strict` session cookie after initial handshake so tokens do not leak in browser history or referrer headers.
- [ ] **Model Archive Checksum Verification (`zoomlt/mt.py`, `zoomlt/asr_nemotron.py`):**
  - Enforce SHA-256 cryptographic digest verification on all downloaded model archives and safe tar extraction (`filter="data"`) against path-traversal attacks.

---

## Phase 6: Infrastructure & Cross-Platform Packaging Review (~10 hours)

- [ ] **Zoom Closed-Caption Push Exponential Backoff & Sequence Recovery (`zoomlt/zoom_cc.py:207-226`):**
  - Add bounded exponential backoff retries on `HTTP 429` / `5xx` errors and automatic sequence re-synchronization via `GET /closedcaption/seq` instead of dropping failed captions immediately.
- [ ] **Standalone Single-File Executables & macOS ScreenCaptureKit Backend:**
  - Add PyInstaller / Briefcase packaging recipes for Windows/macOS/Linux so non-technical users can run `zoomlt` without managing a Python virtualenv.

---

## Phase 7: GitHub Actions & Automated Testing Review (~12 hours)

- [ ] **GitHub Actions Multi-OS CI Matrix (`.github/workflows/ci.yml`):**
  - Add `concurrency` with `cancel-in-progress: true`, job `timeout-minutes: 10`, and a multi-OS matrix (`ubuntu-latest`, `windows-latest`, `macos-latest`) across Python 3.10–3.12 with `pip` caching.
- [ ] **Expanded Pytest & Browser Overlay Test Suite (`tests/`):**
  - Add unit and integration tests for `meet_cc.py` Google Meet bridge messages, `zoom_cc.py` retry/sequence recovery, `SRT`/`WebVTT` subtitle formatting, security headers in `test_server.py`, and audio device hot-plug recovery in `capture.py`.
