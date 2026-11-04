# Movie Remote (ESP32 touchscreen + NFC reader)

A small box that sits next to the TV. Hold a page of the movie book to it and the movie starts on your Marquee player. The screen shows the poster of what's playing, its progress, and touch buttons: back 30 s, play/pause, forward 30 s and stop.

The box pairs with Marquee Server the way Marquee does. Until you approve it, it can't start or control anything, and if you remove it in the control panel it goes back to asking to pair.

## Parts

| Part | Notes |
| --- | --- |
| **Hosyond ESP32-32E E32R40T** (4", ST7796, 320×480, resistive touch) | An ESP32 with 4 MB flash. The display and touch share one SPI bus, and the I2C header is free for the reader. |
| **PN532 NFC module** (the red "V3" board, or the small blue one) | Set it to **I2C** with its switches or jumpers, following the table printed on the board. |
| Jumper wires, and 5 V USB power | |

## Wiring

The PN532 goes on the board's I2C header:

| Header pin | PN532 pin |
| --- | --- |
| 3.3V | VCC |
| GND | GND |
| IO32 | SDA |
| IO25 | SCL |

Other boards need the pins at the top of `movie-remote.yaml` (`nfc_sda`, `nfc_scl`) and the screen section changed.

## Flashing

```bash
pip install esphome
cd esphome
cp secrets.yaml.example secrets.yaml      # set ota_password to something random
esphome run movie-remote.yaml             # the first time over USB; later updates go over Wi-Fi
```

No Wi-Fi network is written into the firmware. If the board doesn't already know one, it offers a **Movie Remote** Wi-Fi hotspot: join it on your phone and pick your network. You can also set Wi-Fi over USB at [web.esphome.io](https://web.esphome.io).

`marquee_url` at the top of the file is Marquee Server's address, `http://marquee.local:8080` by default.

## Pairing

1. The box shows **Pair with Marquee** and a six-digit code.
2. Open the control panel on the Mac (or in Home Assistant), check the code matches, name the box, and approve it.
3. In **Devices**, choose the player the box controls. With "The default player", it uses the Marquee player chosen in Marquee Server's settings.

## First-run tuning

- **Colours are inverted**: set `invert_colors: true` under `display`.
- **Touch is off target**: run `esphome logs movie-remote.yaml`, tap the four corners, and copy the raw x/y values from the log into `calibration`.
- **Tags aren't read**: check the PN532's I2C switches and its four wires, and that the I2C scan at start finds a device at `0x24`. If the reader stops answering after you rewire it, unplug the box's USB for a few seconds.
- **"Not a movie book tag"**: the tag's URL must contain `/m/` and a movie, as the book's tags do. Tags written for any address work: the box sends only the movie.

## Building it into a stand

The PN532 reads about 3–5 cm through paper. Mount it face-up under a flat pad, and hold the page's NFC circle on the pad.

Don't leave the book lying flat on the reader. With many tags stacked in range, it may pick up the wrong page.

## How it works

- **Pairing:** the box asks for a code with `POST /tv/pair` (`kind: remote`), then checks `/tv/pair/<id>` until you approve it. It keeps the secret it's given in flash and sends it with every request.
- **Tags:** when it reads a tag, it sends the tag's URL to `POST /remote/tap`. Marquee Server plays the movie on the box's player and answers at once. Meanwhile the screen shows "Starting…" with the poster.
- **The screen:** every 2 seconds the box asks `/remote/now` what its player is playing.
- **Posters:** they come from `/poster/<key>?w=112&h=168`, resized and re-encoded as baseline JPEGs that the ESP32 can decode.
- **The buttons:** they call `POST /remote/control` with `toggle`, `seek` or `stop`.
