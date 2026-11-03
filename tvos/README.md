# Marquee for Apple TV

A small Apple TV app that plexnfc can start movies in. Plex for Apple TV can't be told to play from another device, and other apps ignore links, but Home Assistant can open an app. When Marquee opens (and every couple of seconds while it's open), it asks plexnfc what to play, then plays the stream in Apple's own player. Subtitles start off.

## How a tap works

1. plexnfc leaves the movie's stream address in that Apple TV's inbox (`GET /tv/inbox`). The stream comes from Jellyfin when plexnfc's Jellyfin settings are filled in, otherwise from Plex. Both turn MKV files into HLS, which Apple TV plays as is.
2. The get-ready script opens Marquee.
3. Marquee picks up the movie and plays it. It reports what's playing (`POST /tv/status`), so plexnfc's now-playing info, the Pause/Resume/Stop tags, ±30 s and the Movie Remote all work.

It also accepts links, for testing or other setups:

- `moviebook://play?url=<stream address>&title=<title>&start=<seconds>`
- `moviebook://configure?server=192.168.1.20:8080&player=Den`

## Install on your Apple TV

You need a Mac with Xcode and an Apple ID.

1. Open `MovieBook.xcodeproj` in Xcode.
2. In **Xcode → Settings → Accounts**, sign in with your Apple ID.
3. Select the **MovieBook** target, open **Signing & Capabilities** and pick your team (a free Apple ID shows as "Personal Team").
4. On the Apple TV, open **Settings → Remotes and Devices → Remote App and Devices** so it shows up in Xcode. In Xcode, open **Window → Devices and Simulators** and pair it.
5. Choose the Apple TV as the run destination and press Run.
6. On the Apple TV, enter plexnfc's address (its **Tag base URL**, for example `192.168.1.20:8080`) and a player name such as `Den`.

With a free Apple ID, Apple lets the app run for 7 days before it has to be installed again from Xcode. A paid Apple Developer account makes installs last a year.

## Set up plexnfc

- **Marquee player**: the player name you typed in the app, for example `Den`.
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
