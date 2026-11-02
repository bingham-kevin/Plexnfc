# Movie Remote (ESP32 + touchscreen + NFC)

A small box that sits next to the TV. Hold a page of the poster book to it and the movie starts. The screen shows the poster of what's playing, its progress, and touch controls:

```
┌──────────────────────┐
│    ┌────────────┐    │
│    │            │    │
│    │   poster   │    │
│    │  200x300   │    │
│    │            │    │
│    └────────────┘    │
│      The Matrix      │
│ Playing on Living Rm │
│ ━━━━━━━━──────────── │
│  0:41:12 / 2:16:00   │
│  ⏪  ( ⏯ )  ⏩  ⏹  ⏻  │
└──────────────────────┘
```

The buttons are: back 30 s, play/pause, forward 30 s, stop, and TV off. TV off needs `HA_OFF_SCRIPT`, or an off script for this reader's TV (see [One reader per TV](#one-reader-per-tv)).

The box only talks to plexnfc, so it works however you've set up playback: plexnfc playing directly, or through Home Assistant on an Apple TV. Phones keep working alongside it.

## Parts (about $35–45)

| Part | Notes |
| --- | --- |
| **ESP32-S3-DevKitC-1 N16R8** (or a clone with 8 MB PSRAM) | The PSRAM holds the poster image and screen buffer. Don't use a plain ESP32 without PSRAM. |
| **3.5" SPI TFT, ST7796S, 320×480, with resistive touch (XPT2046)** | A 2:3 portrait screen, the same shape as a movie poster. Common as "3.5 inch SPI module ST7796". |
| **PN532 NFC module** (the red "V3" board, or the small blue one) | Set it to **I2C** mode with its switches or jumpers, following the table printed on the board. |
| Jumper wires, USB-C cable and 5 V USB power | |

## Wiring

| ESP32-S3 pin | Goes to |
| --- | --- |
| 3V3 | screen VCC, screen LED*, PN532 VCC |
| GND | screen GND, PN532 GND |
| GPIO12 | screen SCK **and** touch T_CLK |
| GPIO11 | screen SDI (MOSI) **and** touch T_DIN |
| GPIO13 | touch T_DO (MISO). Leave the screen's SDO (MISO) unconnected. |
| GPIO10 | screen CS |
| GPIO9 | screen DC / RS |
| GPIO14 | screen RESET |
| GPIO21 | screen LED (backlight, dimmable)* |
| GPIO15 | touch T_CS |
| GPIO16 | touch T_IRQ |
| GPIO4 | PN532 SDA |
| GPIO5 | PN532 SCL |

\*Connect the screen's LED pin to GPIO21, not to 3V3, so the backlight can turn off when nothing is playing. Most modules have a transistor on LED. If yours is very dim, power the module's VCC from 5V instead.

On many of these screens the display's MISO line doesn't release the bus, which stops touch from working. That's why the screen's SDO is left unconnected.

These pins avoid the ones the S3 uses for its PSRAM, flash, USB and boot mode (GPIO 0, 3, 19, 20, 26–37, 45 and 46).

## Flashing

```bash
pip install esphome
cd esphome
cp secrets.yaml.example secrets.yaml      # fill in your Wi-Fi
# edit plexnfc_url at the top of movie-remote.yaml to match PUBLIC_BASE_URL
# (and plexnfc_client if you have a reader per TV, see below)
esphome run movie-remote.yaml             # first time over USB; later updates go over Wi-Fi
```

You can also paste the YAML into the ESPHome add-on in Home Assistant. The box then shows up in Home Assistant too, with a backlight control and its logs.

## One reader per TV

With a Movie Remote next to each TV, tell each one which TV it's for. Set `plexnfc_client` at the top of its YAML to that TV's Plex player name, and give each box its own `name`:

```yaml
substitutions:
  name: movie-remote-den
  friendly_name: Den Movie Remote
  plexnfc_url: http://192.168.1.20:8080
  plexnfc_client: Den%20TV      # spaces as %20
```

The box adds `?client=Den%20TV` to every call it makes: the movie on a tag, `/now`, `/toggle`, `/seek`, `/stop` and `/off`. So the same book plays on whichever TV you hold it to, and the screen shows that TV's movie. A tag whose URL already names a player with `client=` keeps its own.

To have each TV woken by its own script, and turned off by its own off script, list the TVs in plexnfc's **TVs** setting (`PLAYERS` in `.env`); see plexnfc's README. A TV that isn't listed uses the single get-ready and off scripts. Leave `plexnfc_client` empty for plexnfc's default player.

## First-run tuning

- **Colours are wrong (blue looks orange)**: change `color_order` to `rgb`. **Colours look inverted**: set `invert_colors: true`.
- **Touch is off target or mirrored**: run `esphome logs movie-remote.yaml`, tap the four corners, and copy the raw x/y values from the log into `calibration`. If an axis is backwards, swap its min and max, or add `transform: { mirror_x: true }` under the touchscreen.
- **Tags aren't read**: check the PN532's I2C switches, and that `i2c` finds the device at address `0x24` in the log at boot.
- **"Not a plexnfc tag" in the log**: the URL on the tag must start with `plexnfc_url`. Use the same address as `PUBLIC_BASE_URL`.

## Building it into a stand

The PN532 reads about 3–5 cm through paper. Mount it face-up under a flat pad, like a small lectern or the lid of the box, and hold the page's NFC circle on the pad. Put the screen on the front.

Keep the book from lying flat on the reader. With many tags stacked in range, it may pick up the wrong page.

## How it works

- When it reads a tag, the box calls the tag's own URL with `?async=1`. plexnfc replies at once and starts the movie in the background. The screen shows "Starting…" with the poster, even while Home Assistant is still waking the TV.
- Every 2 seconds the box asks plexnfc's `/now` what's playing. The answer comes from the Plex server's list of active streams for `plexnfc_client`, or `PLEX_CLIENT` when that's empty. In Home Assistant mode, keep `PLEX_CLIENT` set to the Plex name of the Apple TV so the box can find its stream.
- Posters come from `/poster/<key>?w=200&h=300`. plexnfc resizes them and re-encodes them as baseline JPEGs, the format the ESP32's decoder can read.
- The buttons call `/seek?by=±30`, `/toggle`, `/stop` and `/off`.
