# plexnfc

Tap an NFC-tagged movie poster with your phone (or hold it to the Movie Remote) and the movie plays on your TV through Plex.

## Setup

1. Fill in the **Configuration** tab:
   - **Plex server URL**. Leave **Plex token** blank: you'll sign in from the panel in step 2.
   - **Plex player**: the player's name as Plex shows it, for example `Living Room`.
   - **Tag base URL**: this Home Assistant machine's fixed IP address on port 8080, for example `http://192.168.1.20:8080`. It is written onto every tag, so give Home Assistant a fixed IP in your router first.
2. Start the app and open **Movie Book** in the sidebar. (If it isn't there, turn on **Show in sidebar** on the app's **Info** tab.) Choose **Sign in with Plex** and approve plexnfc on plex.tv. The token is saved inside the app, so you only do this once.

## Optional: let Home Assistant get the TV ready

- **Get-ready script**: a script that turns the TV on, switches the HDMI input and opens Plex, for example `script.movie_night`. The app runs it before every movie.
- **Home Assistant Plex player**: let Home Assistant's Plex integration start the movie on this `media_player`. This is recommended for Apple TV.
- **Off script**: adds a "TV off" control tag.

You don't need a token for any of this. The app talks to Home Assistant directly. The repository's `docs/home-assistant.md` has example scripts for Apple TV, Shield, LG and other TVs.

## Optional: play on Jellyfin (Swiftfin on Apple TV)

Plex for Apple TV can't be started from another device, but Swiftfin, the Jellyfin app, can. If your movies are also in Jellyfin, taps can play there instead, while Plex still makes the book:

1. In Jellyfin, open **Dashboard → API Keys** and add a key for plexnfc.
2. Fill in **Jellyfin server URL**, **Jellyfin API key** and **Jellyfin player**: the Jellyfin app's device name, for example `AppleTV`. Leave the app open on the TV; it only shows up as a player while it's open. A tap that can't find it lists the Jellyfin players it can see.
3. For the get-ready script, wake the Apple TV, press **Home** (`remote.send_command` with `command: home`), wait a couple of seconds, then open Swiftfin (`media_player.select_source` with source `Swiftfin tvOS`). Swiftfin only reconnects to Jellyfin when it's brought back from the Home screen; opening it while it's already on screen leaves it disconnected, and the tap fails.
4. Movies start with subtitles off. Swiftfin ignores the subtitle choice in the play command and your Jellyfin "Subtitle mode", and turns on a movie's default subtitle track (its player controls then stay on screen too), so plexnfc turns subtitles off once the movie is playing. You don't need an **After-playback script** for Swiftfin: a Menu press would stop the movie.

The movie is matched in Jellyfin by title and year and checked against the tag's TMDB/IMDb ID. A single tag can pick a Jellyfin player with `?client=jellyfin:NAME`.

## Optional: the Marquee Apple TV app

`tvos/MovieBook` in the repository is a small Apple TV app that plexnfc can start movies in. Plex for Apple TV can't be started remotely, but Home Assistant can open an app, and Marquee then asks plexnfc what to play.

1. Build and install it from Xcode (see `tvos/README.md`). Open it and choose this server from the list (it announces itself on your network), or enter this app's **Tag base URL** address, and approve the 6-digit code it shows under **Marquee Apple TVs and phones** on this panel. Type the room in the name box before you approve it, e.g. `Living Room`, since tvOS calls every Apple TV "Apple TV". To rename one later, change its name in the panel's list and choose **Rename**; phones sending taps to it follow the new name, but change **Marquee player** and any get-ready script setting that names it yourself.
2. Set **Marquee player** to the Apple TV's name as the panel lists it.
3. Use a get-ready script that wakes the Apple TV and opens Marquee (`media_player.select_source` with source `Marquee`).

Streams come from Plex, which converts whatever the Apple TV can't play. To stream from Jellyfin instead, fill in the Jellyfin settings and set **Marquee stream** to `jellyfin`. A single tag can pick an app with `?client=app:NAME`.

## Optional: choose a room on each phone (Marquee Remote)

`ios/MarqueeRemote` is an iPhone and iPad app that pairs the same way: choose **Connect**, enter the **Tag base URL** address, and approve its code on this panel. It then lists every player plexnfc can see (Marquee Apple TVs, open Jellyfin apps, Plex players), and taps from that phone go to the one you pick. Other phones keep using the default player.

plexnfc recognizes the phone by its Wi-Fi address, because a tapped tag opens in Safari, not in the app. Open Marquee Remote once after joining the network (or if taps go to the wrong room), and set the home network's **Private Wi-Fi Address** to **Fixed** in the phone's Wi-Fi settings rather than *Rotating*.

The **Get-ready script** only runs for the default player, so a tap for another room doesn't wake the default one. Give other players their own script with **Get-ready scripts for other players**: `app:Living Room = script.living_room_movie; Den TV = script.den_roku`. Use the player IDs the panel and the app show (Marquee Apple TVs start with `app:`, Jellyfin apps with `jellyfin:`).

## Making the book and the tags

In the **Movie Book** sidebar panel:

- **Open printable book**: choose a title, a Plex collection and a layout. The book opens in a new tab from the **Tag base URL**, so do this on your home network. Wait for all the posters to load, then print at 100% scale with background graphics turned on. The book sets its own margins. A whole library makes a very large book, so a Plex collection is recommended.
- **Tag URLs** / **tags.csv**: the URL to write onto each sticker with the NFC Tools app.
- **Cards**: for the Movie Remote, link blank NFC cards to movies by their ID instead of writing them. Choose a collection, a label or some movies, tap **Scan cards**, and hold each movie's card on a Movie Remote when it comes up.

## Movie Remote

The ESP32 Movie Remote firmware is in the repository's `esphome/` folder. Paste it into the **ESPHome Builder** app. Set `marquee_url` to the same address as **Tag base URL**. Then approve the code the box shows in this panel.

## Network

Phones and the Movie Remote connect on port **8080**. If you change the port on the **Network** tab, change **Tag base URL** to match. Anyone on your home network who has a URL can start a movie, so don't forward this port to the internet.
