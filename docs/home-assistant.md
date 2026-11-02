# Home Assistant: get the TV ready automatically

With Home Assistant connected, tapping a poster does everything:

1. plexnfc runs your Home Assistant script. The script turns the TV on, switches to the right HDMI input and opens Plex.
2. plexnfc waits for the Plex app to appear, for up to `PLAYER_WAIT` seconds.
3. The movie starts.

If Home Assistant can't be reached, plexnfc still tries to play, in case the TV is already on.

## Setup

1. In Home Assistant, open your profile, then **Security → Long-lived access tokens**, and create a token.
2. Create a script, using one of the examples below.
3. Add the following to `.env`, then restart plexnfc:

   ```
   HA_URL=http://homeassistant.local:8123
   HA_TOKEN=<the token>
   HA_SCRIPT=script.movie_night
   HA_OFF_SCRIPT=script.movie_night_off   # optional, adds a "TV off" tag
   PLAYER_WAIT=45
   ```

plexnfc calls the script by name, so Home Assistant waits for the whole script to finish before plexnfc looks for the player. Use `wait_template` or `delay` steps so the script doesn't finish before the TV is actually ready.

Rebuild the book after you set `HA_OFF_SCRIPT`, and the back cover gets a **TV off** tag.

Test the script from **Settings → Automations & scenes → Scripts → ▶ Run** before writing any tags. Entity IDs, input names and app names differ between TVs, so check yours under **Developer tools → States**. The `source_list` attribute of a `media_player` shows the exact input and app names it accepts.

## Example scripts

Add these under **Settings → Automations & scenes → Scripts → ⋮ → Edit in YAML**, or put them in `scripts.yaml`.

### Apple TV (or any streaming box) plugged into the TV

Most TVs follow HDMI-CEC: waking the Apple TV also turns the TV on and switches it to the right input.

```yaml
movie_night:
  alias: Movie night
  mode: single
  sequence:
    - action: media_player.turn_on
      target: { entity_id: media_player.living_room_apple_tv }
    - delay: { seconds: 3 }
    - action: media_player.select_source          # the Apple TV integration lists apps as sources
      target: { entity_id: media_player.living_room_apple_tv }
      data: { source: Plex }
    - delay: { seconds: 5 }

movie_night_off:
  alias: Movie night off
  sequence:
    - action: media_player.turn_off               # with CEC this turns the TV off too
      target: { entity_id: media_player.living_room_apple_tv }
```

If CEC doesn't switch the input on your TV, add a step that switches the TV's own `media_player` to the right HDMI input. That step is shown in the next example.

## Let Home Assistant start the movie too (recommended for Apple TV)

By default, plexnfc finds the Plex player itself and starts the movie. Alternatively, Home Assistant's **Plex Media Server** integration can start it. This is handy for an Apple TV, because Home Assistant already tracks the Plex app on it as a `media_player` entity. plexnfc then only looks up which movie was tapped, and Home Assistant does the rest.

### Apple TV setup

1. Add two integrations in Home Assistant:
   - **Apple TV**, to wake the box and open apps.
   - **Plex Media Server**, which creates a `media_player.plex_…` entity for the Plex app on the Apple TV.
2. Check that the Plex app on the Apple TV accepts remote control: open Plex on your phone, tap the cast icon and pick the Apple TV. If a movie starts, plexnfc will work too. The Plex entity in Home Assistant shows as *unavailable* while the Plex app is closed. That's normal.
3. Add the Plex entity to `.env`:

   ```
   HA_SCRIPT=script.movie_night
   HA_PLAYER=media_player.plex_plex_for_apple_tv_living_room   # your entity ID
   PLAYER_WAIT=45
   ```

4. Use this script. It wakes the Apple TV, opens Plex, and waits until the Plex app can accept playback:

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
         data: { source: Plex }
       - wait_template: "{{ not is_state('media_player.plex_plex_for_apple_tv_living_room', 'unavailable') }}"
         timeout: "00:00:30"
         continue_on_timeout: true
   ```

After the script, plexnfc calls `media_player.play_media` on `HA_PLAYER`. If the Plex app isn't ready yet, it keeps retrying for up to `PLAYER_WAIT` seconds. The Pause, Resume and Stop tags are also sent through Home Assistant.

The Apple TV only accepts remote control while the Plex app is open on screen. tvOS suspends apps in the background. That's why the script opens Plex first, even if the Apple TV is already on.

To test without tapping a tag, run this from **Developer tools → Actions**:

```yaml
action: media_player.play_media
target: { entity_id: media_player.plex_plex_for_apple_tv_living_room }
data:
  media_content_type: movie
  media_content_id: 'plex://{"library_name": "Movies", "title": "The Matrix"}'
```

### TV and streaming box controlled separately (e.g. LG webOS + Shield)

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
    - action: remote.turn_on                       # Android TV Remote integration
      target: { entity_id: remote.shield }
      data: { activity: "market://launch?id=com.plexapp.android" }
    - delay: { seconds: 5 }
```

### Plex app on the TV itself (smart TV, no box)

You don't need to switch HDMI inputs. Open the app instead:

```yaml
movie_night:
  alias: Movie night
  mode: single
  sequence:
    - action: media_player.turn_on
      target: { entity_id: media_player.living_room_tv }
    - wait_template: "{{ is_state('media_player.living_room_tv', 'on') }}"
      timeout: "00:00:30"
    - action: media_player.select_source          # LG webOS: apps appear in source_list
      target: { entity_id: media_player.living_room_tv }
      data: { source: Plex }
    - delay: { seconds: 8 }
```

On Google TV / Android TV sets, use `remote.turn_on` with `activity: "market://launch?id=com.plexapp.android"`, as in the previous example. On Samsung, the app's name shown in `source_list` (if it's listed) can be used with `select_source`.

### Extras

You can add more steps to the same script: dim the lights, close the blinds, turn on the soundbar.

```yaml
    - action: light.turn_on
      target: { area_id: living_room }
      data: { brightness_pct: 10, transition: 5 }
```

## Several TVs

Each TV can have its own get-ready and off script. List them in `PLAYERS` (the **TVs** option in the Home Assistant app), for example a Roku TV with its scripts and an iMac with none:

```
PLAYERS=[{"name": "Den TV", "ha_script": "script.movie_night_den_roku", "ha_off_script": "script.movie_night_den_roku_off"}, {"name": "Kevins-iMac.local"}]
```

A tap with `?client=Den TV` runs only the Den scripts, and `/off?client=Den TV` turns only the Den TV off. Taps that name no player keep using `HA_SCRIPT` and `HA_OFF_SCRIPT`. See the README for all the fields, and [esphome/README.md](../esphome/README.md#one-reader-per-tv) to give each TV its own Movie Remote.

## Troubleshooting

- **The TV turns on, but the movie doesn't start**: the Plex app took longer than `PLAYER_WAIT` to start. Increase `PLAYER_WAIT`, or add a longer `delay` at the end of the script. Also check that **Advertise as player** / remote control is turned on in the Plex app.
- **"Home Assistant rejected HA_TOKEN"**: create a new long-lived token.
- **The phone page spins for a while**: that's expected. The page loads once the movie has started, which can take 20–40 seconds when the TV starts from off.
