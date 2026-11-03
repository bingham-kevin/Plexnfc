# Marquee for Apple TV

An Apple TV app for your home movie library. Browse poster rows (Recently Added and your collections), every movie, and search, then play in Apple's own player with the poster, title and summary. Subtitles start off. Taps on a printed Movie Book start movies in Marquee too.

Without a server, **Try the Demo Library** plays a few freely licensed films, so the app can be tried (and reviewed for the App Store) on its own. See `APP_STORE.md` for submitting and `PRIVACY.md` for the privacy policy.

## How it connects

1. Choose the Movie Book server from the list (plexnfc announces itself on the network with Bonjour as `_moviebook._tcp`), or enter its address (its **Tag base URL**, for example `192.168.1.20:8080`).
2. Marquee shows a 6-digit code. Type the room in the name box under **Marquee Apple TVs and phones** in the server's sidebar panel, then approve it. Approvals only work from the panel (Home Assistant's ingress) or on the server's own machine, not from the open port on your network.
3. The Apple TV gets a secret it keeps in its Keychain and sends with every request, so only Apple TVs you approved can browse or get stream addresses. Remove an Apple TV in the panel to cut it off.

Taps reach Marquee through the server's inbox for that Apple TV (`GET /tv/inbox`), which Marquee checks every 2 seconds while it's open; it reports what's playing (`POST /tv/status`) for now-playing and the control tags. The library comes from `/tv/library`, `/tv/movies` and `/tv/search`, and `/tv/play` returns a movie's stream: from Plex, or Jellyfin when **Marquee stream** is `jellyfin`.

`moviebook://play?url=<stream address>&title=<title>` plays a stream directly, for testing.

## Install on your Apple TV

You need a Mac with Xcode and an Apple ID.

1. Open `MovieBook.xcodeproj` in Xcode.
2. In **Xcode → Settings → Accounts**, sign in with your Apple ID.
3. Select the **MovieBook** target, open **Signing & Capabilities** and pick your team (a free Apple ID shows as "Personal Team").
4. On the Apple TV, open **Settings → Remotes and Devices → Remote App and Devices** so it shows up in Xcode. In Xcode, open **Window → Devices and Simulators** and pair it.
5. Choose the Apple TV as the run destination and press Run.
6. On the Apple TV, connect to the server and approve the pairing code in its panel.

With a free Apple ID, Apple lets the app run for 7 days before it has to be installed again from Xcode. A paid Apple Developer account makes installs last a year.

## Set up plexnfc

- **Marquee player**: the Apple TV's name as the panel lists it under **Marquee Apple TVs and phones** (the name you gave it when you approved it).
- **Get-ready script**: wake the Apple TV and open Marquee, for example:

  ```yaml
  sequence:
    - action: media_player.turn_on
      target: { entity_id: media_player.den_apple_tv }
    - delay: { seconds: 3 }
    - action: media_player.select_source
      target: { entity_id: media_player.den_apple_tv }
      data: { source: Marquee }
  ```

A single tag can pick an app with `?client=app:NAME`.

## Notes

- The stream address includes your Plex token or Jellyfin API key, and the app collects it from plexnfc over your home network. That's the same trust plexnfc already places in your network: don't expose its port to the internet.
- Build for the simulator without signing:

  ```bash
  xcodebuild -project tvos/MovieBook/MovieBook.xcodeproj -scheme MovieBook -sdk appletvsimulator CODE_SIGNING_ALLOWED=NO build
  ```
