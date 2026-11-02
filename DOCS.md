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

## Optional: several TVs

If you have more than one TV, each with its own NFC reader, list them under **TVs**, one entry per TV:

```yaml
- name: Den TV
  ha_script: script.movie_night_den_roku
  ha_off_script: script.movie_night_den_roku_off
- name: Kevins-iMac.local
```

- **name**: the player's name as Plex shows it. Readers send it as `?client=`.
- **ha_script** / **ha_off_script**: that TV's own get-ready and off scripts. Leave them out for a TV that needs none: it then runs no script.
- **ha_player** (optional): play on that TV through Home Assistant's Plex integration.
- **player_wait** (optional): defaults to **Player wait**.

A tap that names no player, or a player that isn't listed, uses the settings above, just as before. The **Tag URLs** page lists control tags for each TV, such as `/off?client=Den%20TV`.

## Making the book and the tags

In the **Movie Book** sidebar panel:

- **Open printable book**: choose a title, a Plex collection and a layout. The book opens in a new tab from the **Tag base URL**, so do this on your home network. Wait for all the posters to load, then print with margins set to *None* and background graphics turned on. A whole library makes a very large book, so a Plex collection is recommended.
- **Tag URLs** / **tags.csv**: the URL to write onto each sticker with the NFC Tools app.

## Movie Remote

The ESP32 Movie Remote firmware is in the repository's `esphome/` folder. Paste it into the **ESPHome Builder** app. Set `plexnfc_url` to the same address as **Tag base URL**. With a reader for each TV, set `plexnfc_client` on each one to its TV's Plex name (spaces as `%20`, for example `Den%20TV`).

## Network

Phones and the Movie Remote connect on port **8080**. If you change the port on the **Network** tab, change **Tag base URL** to match. Anyone on your home network who has a URL can start a movie, so don't forward this port to the internet.
