# Using this with Zoom

Short version: there is nothing to install inside Zoom, and no Zoom account, plan or developer
registration is needed to use this app for yourself. It sits beside Zoom and listens to your
speakers. The one optional feature that touches Zoom itself, putting captions on screen for every
participant, needs a setting the meeting **host** turns on, and nothing else.

## This is not a Zoom plugin, and that is on purpose

Zoom "apps" live in the Zoom App Marketplace, run inside the Zoom client, and require a developer
registration, an app listing and a security review. This tool is not one of those. It is an
ordinary program on your computer that records the sound your speakers are playing and shows text
on your screen.

That choice has consequences worth knowing:

* Works in **anyone's** meeting, including meetings you did not schedule and cannot administer.
* Works on a **free** Zoom account, and on your employer's account without asking IT for anything.
* Nothing is installed into Zoom, so nothing can be revoked, expire, or break when Zoom updates.
* No audio ever leaves your machine, so there is no vendor to trust with your meetings.
* The trade-off: captions are yours alone unless you turn on the caption push below, and the app
  cannot tell who is speaking, because it only hears the mixed output.

## Do I need a Zoom developer account?

**To use it yourself: no.** Install it, start a meeting, run it. Nothing in the app authenticates
to Zoom.

**To push captions into the meeting for everyone: still no.** That feature uses Zoom's built-in
"third-party closed captioning" support. The host enables a setting and hands you a per-meeting
URL. No developer account, no app listing, no review, no OAuth.

**To publish it as a Zoom app that other people install: yes, but you almost certainly do not want
to.** Per [Zoom's distribution docs](https://developers.zoom.us/docs/distribute/), a published
public app goes through a Marketplace Operations review of "metadata, usability, and compliance"
plus a Marketplace Security "Security Audit and a focused security test", and you must supply
"a Terms of Use, Privacy Policy, Self-Serve Documentation, and Support URL" and provide ongoing
support to users. Apps distributed only within your own Zoom account are called account-level apps
and "don't require the standard Marketplace review", and building anything requires your Zoom user
role to have "Zoom for developers" permissions enabled by an account admin
([Zoom: before you build](https://developers.zoom.us/docs/build-flow/before-you-build/)).

None of that applies here, because a Marketplace app could not do what this app does anyway. An app
running inside the Zoom client cannot read the meeting's audio; that needs the Meeting SDK or
Realtime Media Streams, which are the paid, credential-heavy routes described in
[zoom-platform.md](zoom-platform.md). To share the tool with colleagues, send them the repository
and the [install guide](../INSTALL.md).

## Putting captions on screen for everyone (optional)

By default the captions are private to you, in a browser window. If you want the whole meeting to
see them, Zoom accepts captions from outside programs.

**The host does this once, in their Zoom web settings:**

1. Sign in at zoom.us → **Settings** → **In Meeting (Advanced)**.
2. Turn on **Manual captions**.
3. Tick **Allow use of caption API token to integrate with 3rd-party closed captioning services**.

**Then, in each meeting:**

1. The host (or a co-host they promote) clicks the arrow next to **Show Captions** in the toolbar.
2. Chooses **Set up manual captioner**.
3. Clicks **Copy the API token**. That copies a long URL, unique to that meeting.

**Give the URL to the app.** It is a password for that meeting's captions, so keep it out of your
shell history:

```bash
export ZOOMLT_ZOOM_CC_URL="https://wmcapi.zoom.us/closedcaption?id=...&signature=..."
zoomlt run --engine nemotron -l es -l hi
```

On Windows PowerShell:

```powershell
$env:ZOOMLT_ZOOM_CC_URL = "https://wmcapi.zoom.us/closedcaption?id=...&signature=..."
zoomlt run --engine nemotron -l es -l hi
```

Participants then click **Show Captions** and see the English text under the video. The app posts
only finished sentences, at most one request per 0.8 seconds, as UTF-8 plain text, with the
sequence number Zoom requires. If you restart the app mid-meeting it asks Zoom for the last
sequence number it accepted and carries on from there, so captions do not silently stop.

Details worth knowing: the token works only for that meeting and expires with it; Zoom shows about
two lines at a time and each new caption replaces the previous one; and captions you push are
visible to everyone in the meeting, including anyone recording it.

## What about Zoom's own translated captions?

Zoom can do this itself, and if your company already pays for it you do not need this app. It
requires a Business Plus or Enterprise plan, or the paid Translated Captions add-on. Free and Pro
accounts get English automated captions only. That paid feature is the reason this app exists.

## Sharing captions with someone else in the room

If a colleague wants to read the captions on their own laptop or phone, and you would rather not
push them into the meeting for everybody:

```bash
zoomlt run --host 0.0.0.0 --engine nemotron -l es -l hi
```

The app prints a second URL containing a one-time token. Anyone on your network with that URL sees
the live captions. The connection is not encrypted, so use it on a network you trust, and the token
changes every time you start the app.
