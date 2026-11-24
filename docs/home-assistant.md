# Home Assistant: get the TV ready, and follow the movie

Marquee Server leaves each tapped movie in Marquee's inbox on the Apple TV, then gets the TV ready:

- For an Apple TV paired under **Devices › Open Marquee automatically**, Marquee Server wakes it and opens Marquee itself. Home Assistant isn't needed.
- Otherwise, it can run a Home Assistant script that turns the TV on, switches to the right HDMI input and opens Marquee. Marquee picks the movie up as it opens.

If Home Assistant can't be reached, the movie still waits in Marquee, in case the TV is already on.

## Setup

1. In Home Assistant, open your profile, then **Security → Long-lived access tokens**, and create a token.
2. Create a script, using one of the examples below.
3. Add the following to `.env` (for the Mac app, the `.env` in its Application Support folder), then restart Marquee Server:

   ```
   HA_URL=http://homeassistant.local:8123
   HA_TOKEN=<the token>
   HA_SCRIPT=script.movie_night
   HA_OFF_SCRIPT=script.movie_night_off   # optional, adds a "TV off" tag
   HA_STARTED_SCRIPT=script.movie_started # optional, started once the movie is sent
   ```

`HA_SCRIPT` runs only for the default player. Give other rooms their own scripts with `PLAYER_SCRIPTS`, as `player = script` pairs separated by semicolons:

```
PLAYER_SCRIPTS=Living Room = script.movie_night; Den = script.den_movie_night
```

Rebuild the book after you set `HA_OFF_SCRIPT`, and the back cover gets a **TV off** tag.

Test the script from **Settings → Automations & scenes → Scripts → ▶ Run** before writing any tags. Entity IDs, input names and app names differ between TVs, so check yours under **Developer tools → States**. The `source_list` attribute of a `media_player` shows the exact input and app names it accepts.

## Example scripts

Add these under **Settings → Automations & scenes → Scripts → ⋮ → Edit in YAML**, or put them in `scripts.yaml`.

### Apple TV plugged into the TV

Most TVs follow HDMI-CEC: waking the Apple TV also turns the TV on and switches it to the right input. Home Assistant's **Apple TV** integration lists apps as sources, so it can open Marquee.

```yaml
movie_night:
  alias: Movie night
  mode: single
  sequence:
    - action: media_player.turn_on
      target: { entity_id: media_player.living_room_apple_tv }
    - delay: { seconds: 3 }
    - action: media_player.select_source
      target: { entity_id: media_player.living_room_apple_tv }
      data: { source: Marquee }

movie_night_off:
  alias: Movie night off
  sequence:
    - action: media_player.turn_off               # with CEC this turns the TV off too
      target: { entity_id: media_player.living_room_apple_tv }
```

### TV that doesn't follow CEC

Switch the TV to the Apple TV's input as well. For an LG webOS TV:

```yaml
movie_night:
  alias: Movie night
  mode: single
  sequence:
    # LG TVs that are off need Wake-on-LAN. Set up the Wake on LAN integration with the TV's MAC address.
    - action: wake_on_lan.send_magic_packet
      data: { mac: "AA:BB:CC:DD:EE:FF" }
    - wait_template: "{{ is_state('media_player.lg_tv', 'on') }}"
      timeout: "00:00:30"
    - action: media_player.select_source
      target: { entity_id: media_player.lg_tv }
      data: { source: "HDMI 2" }                   # must match an entry in source_list
    - action: media_player.select_source
      target: { entity_id: media_player.living_room_apple_tv }
      data: { source: Marquee }
```

### Extras

You can add more steps to the same script: dim the lights, close the blinds, turn on the soundbar.

```yaml
    - action: light.turn_on
      target: { area_id: living_room }
      data: { brightness_pct: 10, transition: 5 }
```

To dim the lights only once the movie is actually playing, use the Marquee integration's triggers instead (below).

## The Marquee integration: automations that follow the movie

The **Marquee** integration (in `custom_components/marquee`) works the other way round. Home Assistant hears what Marquee plays, so automations can dim the lights when a movie starts, raise them a little on pause, and bring them back up when it stops or ends. With Music Assistant, it also lets Movie Remote cards play music (see [Music cards](#music-cards-with-music-assistant)). It works with Marquee Server on a Mac, in Docker, or run with Python.

### Install

1. In HACS, add this repository (`https://github.com/bingham-kevin/Plexnfc`) as a custom repository of type **Integration**, and download **Marquee**. Or copy `custom_components/marquee` into your Home Assistant's `config/custom_components` folder.
2. Restart Home Assistant.
3. Home Assistant usually finds Marquee Server on its own: look for it under **Settings → Devices & services → Discovered**. If it doesn't appear, choose **Add Integration → Marquee** and enter the server's address, e.g. `http://marquee.local:8080`.
4. Home Assistant shows a 6-digit code. On the Mac, choose **Open Control Panel** in Marquee Server's menu and approve that code under **Marquee players and phones**, then choose **Submit** in Home Assistant.

### What you get

- **A media player for each Marquee player** (each Apple TV, iPhone or iPad running Marquee). It shows the title and poster, and is *playing*, *paused* or *idle*; Marquee in the background counts as idle. It can pause, play and stop. **Play media** starts a movie by its key, the end of its tag address, e.g. `tmdb-603`.
- **Triggers on each Marquee player's device:** *Movie started*, *Movie paused*, *Movie resumed* and *Movie stopped or finished*. Find them in the automation editor under **Device**. They fire within a second or so: the server holds Home Assistant's request open until something changes.
- **A `marquee_event` event** for each of those, for an Event trigger that should cover every player. The event data has `type` (`started`, `paused`, `resumed`, `stopped`), `player`, `title`, `key` and `device_id`.

```yaml
triggers:
  - trigger: event
    event_type: marquee_event
    event_data: { type: started }
actions:
  - action: light.turn_on
    target: { area_id: den }
    data: { brightness_pct: 5, transition: 4 }
```

If the server is reinstalled, or Home Assistant is removed in its control panel, Home Assistant asks you to pair again with a new code.

### Music cards, with Music Assistant

The **Music** section ([docs/music.md](music.md)) is where music cards are linked now, from any music source, for AirPlay, Cast or Music Assistant speakers. This section covers how Marquee reaches Music Assistant.

With [Music Assistant](https://music-assistant.io), a Movie Remote card can play music too: an album, playlist, artist, track or radio station, on a speaker you choose. Marquee reaches Music Assistant in one of two ways:

- **Directly** (Home Assistant isn't needed). In Marquee's control panel, open **Music › Sources**, and under **Music Assistant** choose **Connect**. Enter Music Assistant's address (port 8095, such as `http://homeassistant.local:8095`; it's the same when Music Assistant runs as a Home Assistant app) and a long-lived token: in Music Assistant, open **Settings › Profile**, and under **Long-lived access tokens** make one called Marquee. Marquee tries it before keeping it, and keeps the token on the server only (`music_assistant.json`, beside the cards). It can also be set with `MUSIC_ASSISTANT_URL` and `MUSIC_ASSISTANT_TOKEN`.
- **Through Home Assistant**, with the Marquee integration and Music Assistant's integration both set up there. Nothing to set up in Marquee.

When both are there, Marquee uses the direct connection, and falls back to Home Assistant if Music Assistant stops answering. A card keeps its speaker's Music Assistant player ID, so it works either way.

1. In Marquee's control panel, open **Music**. Choose one speaker, and find the music.
2. Choose **Link next card** and hold a blank card on a Movie Remote. A card a reader has just read that isn't linked gets its own button: **Link card XX-XX-XX-XX**.
3. Tap the card on the box: the music starts on that speaker, replacing what's playing there, and the box shows what's playing where.

**Any NFC reader in Home Assistant works too**, not only a Movie Remote: a screen running Tessera with a PN532, an ESPHome reader, or the Home Assistant app on a phone. The integration sends every card Home Assistant reads (its `tag_scanned` event) to Marquee: a music or book card plays on the reader's speakers (see [Readers](#readers-a-default-output-and-controls-in-the-room) below), else on the card's own speaker; a movie card plays on the reader's Marquee player, else the default one; a scan in the control panel links the card, and a card Marquee doesn't know shows under **Card not linked**. Each one also fires a `marquee_card` event (`tag_id`, `device_id`, `result`: `music`, `movie`, `linked`, `unknown` or `error`, `title`, `message`), for a sound or a notification. Tags with their own automations in Home Assistant still run them as well.

Cards read by a reader in Home Assistant need the integration, of course; with the direct connection, Marquee plays their music itself and the integration only passes the card on.

### Readers: a default output, and controls in the room

Every NFC reader has a **default output**: the speakers, one or several played together, that music and book cards held to it play on, whatever speaker the card was linked with. So a card plays in the room where it's held. Choose it in Marquee's control panel under **Devices › Readers**. A reader in Home Assistant shows up there once it has read a card; a Movie Remote, once it's paired. The same place chooses which Marquee player its movie cards start on.

For each reader in Home Assistant, the integration adds a device, *<reader> Marquee*, with:

- **A media player**: what's playing on the reader's speakers (music or a book), with pause, play, next, previous, stop and volume. Its `speakers` attribute names the speakers it plays on now, and `is_default` says whether they're the default.
- **A switch for each speaker**: on means the reader plays there too. Turning one on or off moves what's playing as well (Music Assistant's queue moves; a book carries on from where it was). The choice is remembered for the next card.
- **Restore default speakers** and **Make these speakers the default** buttons.

**A Tessera screen** can show these on a page of its own. Add a page to the screen's Tessera configuration with the media player, the switches and the two buttons, and enter that page's number for the reader under **Devices › Readers › Tessera page**. When a card is read there, Marquee opens that page (with the screen's `esphome.<node>_show_page` action), so whoever's holding the card can change the speakers and control the music on the same screen. The node name is the one in the screen's ESPHome configuration, which can differ from its device name in Home Assistant.

Without the direct connection, Marquee Server has no Home Assistant token for music either. The integration keeps a request open to the server, telling it which Music Assistant speakers there are; when a card is tapped or the panel searches, the server hands the job to the integration, which runs Music Assistant's own `music_assistant.search` and `music_assistant.play_media` actions. Music cards then work only while Home Assistant, the integration and Music Assistant are running, and the control panel says if one of them isn't.

## Troubleshooting

- **The TV turns on, but the movie doesn't start**: check that the script opens Marquee (its source name in `source_list`), and that Marquee on that Apple TV is approved under **Devices** with the player's name. The movie waits in Marquee for a few minutes, so a slow TV still gets it.
- **"Home Assistant rejected HA_TOKEN"**: create a new long-lived token.
