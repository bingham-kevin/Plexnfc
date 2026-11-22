# Movie Remote (ESP32 touchscreen + NFC reader)

A small box that sits next to the TV. Hold a page of the movie book to it and the movie starts on your Marquee player. The pages can carry blank NFC cards linked to their movies in the control panel, or tags written with the movie's address. The screen shows the poster of what's playing, its progress, and touch buttons: back 30 s, play/pause, forward 30 s and stop.

The box pairs with Marquee Server the way Marquee does. Until you approve it, it can't start or control anything, and if you remove it in the control panel it goes back to asking to pair.

## Parts

Two screens are supported. Each has its own file; the logic they share is in `common/movie-remote.yaml`.

| Screen | File | Notes |
| --- | --- | --- |
| **Elecrow CrowPanel Advance 5.0** (DIS02050A v1.2/v1.3, 800×480 IPS, capacitive touch) | `movie-remote-crowpanel.yaml` | An ESP32-S3 with 8 MB PSRAM: a large poster and smooth screens. |
| **Hosyond ESP32-32E E32R40T** (4", ST7796, 320×480, resistive touch) | `movie-remote.yaml` | An ESP32 with no PSRAM, so the poster is small (96×144). |

You also need:

- A **PN532 NFC module** (the red "V3" board, or the small blue one). Set it to **I2C** with its switches or jumpers, following the table printed on the board, and power-cycle it after.
- Jumper wires, and 5 V USB power.

## Wiring

The PN532 goes on the board's I2C connector:

| PN532 pin | CrowPanel **I2C-OUT** | Hosyond I2C header |
| --- | --- | --- |
| VCC | 3V3 | 3.3V |
| GND | GND | GND |
| SDA | IO15 | IO32 |
| SCL | IO16 | IO25 |

On the CrowPanel, the reader shares the bus with the touch chip (0x5D), the clock (0x51) and the board's helper chip (0x30). The PN532 answers at 0x24, so they don't clash. The helper chip runs the backlight and the touch chip's reset, and the firmware wakes it at start-up.

**A speaker (CrowPanel, optional):** plug a small 4–8 Ω speaker into the **SPK** connector, and the box beeps when a card plays a movie, rises when a scan links a card, double-beeps for a card that's already linked, and gives a low tone when something goes wrong. The speaker shares its pins (IO4/5/6) with the microSD slot through the board's DIP switch: if it stays silent, unplug the box and flip the switch to the audio position. Tap the speaker button at the top right of the screen to mute or unmute; hold it for a volume slider over the screen, which goes away a few seconds after you let go. The **Volume** slider on the settings screen does the same (all the way down is silent); `sound_volume` at the top of `movie-remote-crowpanel.yaml` is where it starts.

The CrowPanel's USB serial chip is a CH340K. Recent macOS has no driver for it: install WCH's CH34xVCPDriver (from wch-ic.com) and approve it in System Settings before flashing over USB.

## Flashing

```bash
pip install esphome
cd esphome
cp secrets.yaml.example secrets.yaml      # set ota_password to something random
esphome run movie-remote-crowpanel.yaml   # or movie-remote.yaml for the Hosyond board
```

The first time is over USB; later updates go over Wi-Fi.

No Wi-Fi network is written into the firmware. If the box doesn't know one, it offers a **Movie Remote** Wi-Fi hotspot. Join it on your phone and a setup page opens: tap your network, type its password and tap **Join**. You can also set Wi-Fi over USB at [web.esphome.io](https://web.esphome.io).

`marquee_url` at the top of the file is Marquee Server's address, `http://marquee.local:8080` by default.

## Pairing

1. The box shows **Pair with Marquee** and a six-digit code.
2. Open the control panel on the Mac (or in Home Assistant), check the code matches, name the box, and approve it.
3. In **Devices**, choose the player the box controls. With "The default player", it uses the Marquee player chosen in Marquee Server's settings.

## Settings on the screen

Tap the gear at the top left of the screen for:

- **Network details:** the name the box is paired as, the server, the Wi-Fi network and its signal, the IP and MAC addresses, and the firmware version.
- **Brightness**, and on the CrowPanel, **Volume**: the box beeps at the new volume when you let go.
- **Change Wi-Fi:** lists the networks the box can see. Tap one, type its password on the on-screen keyboard and press the check key. The box joins it and keeps it. If the password is wrong, the box falls back to the Movie Remote hotspot after a minute.
- **Reset to defaults:** the box tells Marquee Server to forget it, wipes its Wi-Fi and its pairing, and restarts into setup.

The setup page is in `components/movie_portal`. It takes over from ESPHome's built-in captive portal page, and still uses the captive portal to scan for networks and join one.

## First-run tuning

- **Colours are inverted**: set `invert_colors: true` under `display`.
- **Touch is off target (Hosyond)**: run `esphome logs movie-remote.yaml`, tap the four corners, and copy the raw x/y values from the log into `calibration`. The CrowPanel's capacitive touch needs no calibration.
- **CrowPanel screen flickers or shifts**: the build uses its own copy of ESPHome's screen driver (`components/mipi_rgb`), set up as Elecrow's driver is (16-line bounce buffers, 64-byte DMA bursts), with a 32 KB instruction cache so Wi-Fi isn't slowed by the screen's memory traffic. With ESPHome's stock driver, part of the picture slips down while the screen redraws. After updating ESPHome, copy its new `mipi_rgb` into `components/` and make the same change.
- **Tags aren't read**: check the PN532's I2C switches and its four wires, and that the I2C scan at start finds a device at `0x24`. If the reader stops answering after you rewire it, unplug the box's USB for a few seconds.
- **"This card isn't linked to a movie"**: link it on the control panel's **Cards** page, which offers the card the box just read. Tags written with a movie's URL (containing `/m/`) need no linking; tags written for any address work, since the box sends only the movie.

## Building it into a stand

The PN532 reads about 3–5 cm through paper. Mount it face-up under a flat pad, and hold the page's NFC circle on the pad.

Don't leave the book lying flat on the reader. With many tags stacked in range, it may pick up the wrong page.

## How it works

- **Pairing:** the box asks for a code with `POST /tv/pair` (`kind: remote`), then checks `/tv/pair/<id>` until you approve it. It keeps the secret it's given in flash and sends it with every request.
- **Cards and tags:** when it reads a card, it sends the card's ID, and the URL written on it if any, to `POST /remote/tap`. Marquee Server plays the movie linked to that card (or the one in the URL) on the box's player and answers at once. Meanwhile the screen shows "Starting…" with the poster. During a card scan from the control panel's **Cards** page, the card is linked to the next movie instead, and the screen shows which movie it was linked to and which comes next.
- **The screen:** every 2 seconds the box asks `/remote/now` what its player is playing.
- **Posters:** they come from `/poster/<key>?w=…&h=…` (240×360 on the CrowPanel, 96×144 on the Hosyond), resized and re-encoded as baseline JPEGs that the ESP32 can decode.
- **The buttons:** they call `POST /remote/control` with `toggle`, `seek` or `stop`.
