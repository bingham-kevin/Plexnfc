# Marquee Server for Mac

plexnfc as a menu bar app, so the movie book works without Home Assistant. It serves the tag addresses, and when a tag is tapped it switches the Apple TV on and opens Marquee itself.

```
 tap ──► Marquee Server (this Mac) ──► movie waits in the inbox
                                   └─► pyatv: wake Apple TV, open Marquee ──► Marquee plays it from Plex
```

Opening Marquee uses [pyatv](https://pyatv.dev) over Apple's Companion protocol, the same way Home Assistant's Apple TV integration does. The Apple TV shows no prompt. Because Companion is reverse-engineered rather than a public API, distribute this app notarized outside the Mac App Store.

## Prototype status

- The app runs the repository's `plexnfc` package (bundled at build time) in a Python environment it makes on first launch in `~/Library/Application Support/Marquee Server/venv`. That needs Python 3.10 or newer on the Mac: it looks for Homebrew's `python3.13`, `3.12`… first, or you can choose one in Settings. A release would bundle its own Python instead.
- Everything plexnfc saves lives in that folder too: the Plex sign-in, paired Apple TVs and phones (`apple_tvs.json`, `marquee_devices.json`). An optional `.env` there can hold extra settings (Jellyfin, Home Assistant…). The settings in the app win over it.

## Set up

1. Open `MarqueeServer/MarqueeServer.xcodeproj`, pick your team under **Signing & Capabilities**, and run it. A film icon appears in the menu bar.
2. Allow **Local Network** access when macOS asks. If it doesn't ask, or you said no, turn **Marquee Server** on in **System Settings → Privacy & Security → Local Network**. Without it the server can't find Apple TVs.
3. In **Settings…**, set the movie library and the **Marquee player** taps play on by default. Leave **Plex server** blank to use the one on your Plex account.
4. **Open Control Panel**: sign in with Plex, approve Marquee on each Apple TV and Marquee Remote on each phone, then under **Open Marquee automatically** choose **Find Apple TVs**, pair each Apple TV with its Marquee player name and type the PIN the TV shows.
5. Write the tags from the control panel's tag list. They point at `http://marquee.local:8080/m/…`, a name Marquee Server answers for with whatever address the Mac has, so the Mac needs no fixed address. iPhones and iPads open `.local` names; many Android phones can't.
