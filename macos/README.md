# Marquee Server for Mac

plexnfc as a menu bar app, so the movie book works without Home Assistant. It serves the tag addresses, and when a tag is tapped it switches the Apple TV on and opens Marquee itself.

```
 tap ──► Marquee Server (this Mac) ──► movie waits in the inbox
                                   └─► pyatv: wake Apple TV, open Marquee ──► Marquee plays it from Plex
```

Opening Marquee uses [pyatv](https://pyatv.dev) over Apple's Companion protocol, the same way Home Assistant's Apple TV integration does. Opening an app this way shows no prompt on the TV; sending a `moviebook://` link would ask "Open in Marquee?" first, so the server doesn't send links. Because Companion is reverse-engineered rather than a public API, distribute this app notarized outside the Mac App Store.

## Prototype status

- The app runs the repository's `plexnfc` package (bundled at build time) in a Python environment it makes on first launch in `~/Library/Application Support/Marquee Server/venv`. That needs Python 3.10 or newer on the Mac: it looks for Homebrew's `python3.13`, `3.12`… first, or you can choose one in Settings. A release would bundle its own Python instead.
- Everything plexnfc saves lives in that folder too: the Plex sign-in, paired Apple TVs and phones (`apple_tvs.json`, `marquee_devices.json`). An optional `.env` there can hold extra settings (Jellyfin, Home Assistant…). The settings in the app win over it.

## Set up

1. Open `MarqueeServer/MarqueeServer.xcodeproj`, pick your team under **Signing & Capabilities**, and run it. A film icon appears in the menu bar.
2. Allow **Local Network** access when macOS asks. If it doesn't ask, or you said no, turn **Marquee Server** on in **System Settings → Privacy & Security → Local Network**. Without it the server can't find Apple TVs.
3. In **Settings…**, set the movie library and the **Marquee player** taps play on by default. Leave **Plex server** blank to use the one on your Plex account.
4. **Open Control Panel** (⌘O in the menu) opens the control panel as a Mac window, with the web panel's tabs down the side: Now Playing, Devices, Tags, Cards, Book and Sources. It does everything the web panel does; **Open Control Panel in Browser** opens the web one. In it, sign in with Plex, then under **Open Marquee automatically** choose **Find Apple TVs** and pair each Apple TV with a player name (its room), typing the PIN the TV shows. Marquee on that Apple TV then connects to this server by itself under the same name: the server opens it, and Marquee pairs with a server that's expecting it. For an Apple TV paired already, **Connect Marquee** does the same. Approve Marquee on an iPhone or iPad, and Marquee Remote on each phone, the usual way, with the code it shows. Under **Devices › Readers**, choose each NFC reader's default output: the speakers its music and book cards play on.
5. Write the tags from the control panel's tag list. They point at `http://marquee.local:8080/m/…`, a name Marquee Server answers for with whatever address the Mac has, so the Mac needs no fixed address. iPhones and iPads open `.local` names; many Android phones can't.

## The control panel window

`MarqueeServer/Panel/` is the control panel in SwiftUI. It calls the same `/panel/api` the web panel (`panel/`) uses, on the server running on this Mac, so a change to the API shows up in both; keep the two panels in step when adding a feature. The Mac window differs only where the Mac does it better: folders are chosen with the Finder's folder picker, the tag list is a sortable table with a right-click menu, **Download CSV** saves with a Save dialog, and the printable book opens in your browser to print. Plex's sign-in opens plex.tv in the browser and the window waits for it (`POST /panel/api/plex/login`). While the window is open, Marquee Server shows in the Dock.

