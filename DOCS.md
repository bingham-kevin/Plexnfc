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
3. For the get-ready script, open Swiftfin instead of Plex (`media_player.select_source` with source `Swiftfin tvOS`).

The movie is matched in Jellyfin by title and year and checked against the tag's TMDB/IMDb ID. A single tag can pick a Jellyfin player with `?client=jellyfin:NAME`.

## Making the book and the tags

In the **Movie Book** sidebar panel:

- **Open printable book**: choose a title, a Plex collection and a layout. The book opens in a new tab from the **Tag base URL**, so do this on your home network. Wait for all the posters to load, then print with margins set to *None* and background graphics turned on. A whole library makes a very large book, so a Plex collection is recommended.
- **Tag URLs** / **tags.csv**: the URL to write onto each sticker with the NFC Tools app.

## Movie Remote

The ESP32 Movie Remote firmware is in the repository's `esphome/` folder. Paste it into the **ESPHome Builder** app. Set `plexnfc_url` to the same address as **Tag base URL**.

## Network

Phones and the Movie Remote connect on port **8080**. If you change the port on the **Network** tab, change **Tag base URL** to match. Anyone on your home network who has a URL can start a movie, so don't forward this port to the internet.
