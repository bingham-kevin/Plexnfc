# Plexnfc

A physical book of movie posters. Tap a page with your phone and the movie starts playing on your TV through Plex.

```
 phone taps NFC sticker ──► opens http://<plexnfc>/m/tmdb-603 ──► plexnfc tells Plex ──► TV plays The Matrix
```

No app is needed on the phone. iPhones (XS and newer) and Android phones read URL tags on their own: tap, then open the link from the notification.

## What you need

- A Plex Media Server, and a Plex player that can be remote-controlled (Apple TV, Roku, Fire TV, Android TV, Shield, Plex HTPC, a smart TV app…). In the player's settings, turn on **Advertise as player** / **Allow remote control**.
- An always-on machine on the same network to run plexnfc: a Raspberry Pi, a NAS, or the Plex server itself. Give it a **fixed IP or hostname**, because that address gets written onto every tag.
- **NFC stickers**: NTAG213 or NTAG215, about 25 mm round. They're cheap in packs of 50 or 100. Avoid "on-metal" tags and foil-backed paper.
- An NFC writing app: **NFC Tools** (free, iOS and Android).
- A printer, plus a binder, photo album or bookbinding service.

## Run it as a Home Assistant app (easiest)

If you have Home Assistant OS or Supervised, plexnfc can run as an app, which older versions of Home Assistant call an add-on. It runs on the Home Assistant machine, so you don't need a separate server. You make the book and get the tag URLs from a **Movie Book** panel in the sidebar. It talks to Home Assistant without a token.

**If this repository is public:** go to **Settings → Apps** (Add-ons in older versions) **→ App store → ⋮ → Repositories**, add `https://github.com/bingham-kevin/Plexnfc`, then install **plexnfc**.

**If it's private:** Home Assistant can only add public repositories, so install it as a local app instead:
1. Install the **Samba share** or **Advanced SSH & Web Terminal** app.
2. Copy this whole repository into the `addons` share, so that `addons/plexnfc/config.yaml` exists.
3. In **Settings → Apps → App store**, choose **⋮ → Check for updates**. **plexnfc** appears under *Local apps*.

Then fill in the **Configuration** tab and start the app. The app's **Documentation** tab walks you through the rest. To update a local app, copy the new files over and bump `version` in `config.yaml`.

The rest of this section covers running plexnfc on another machine instead, with Docker or Python.

## 1. Set up

```bash
git clone <this repo> && cd Plexnfc
cp .env.example .env      # then fill in your Plex URL, token, library and player name
```

You can leave `PLEX_TOKEN` out: open `http://<that-machine>:8080/` and choose **Sign in with Plex**, which saves the token to `.plex_auth.json`. To set it by hand instead, follow [Finding an authentication token](https://support.plex.tv/articles/204059436). `PLEX_CLIENT` is the player's name as it appears in Plex.

Run it with Docker:

```bash
docker compose up -d
```

or with plain Python:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m plexnfc.server
```

Open `http://<that-machine>:8080/tags` to see every movie and the URL for its tag.

## 2. Make the book

```bash
python -m plexnfc.book                              # whole library, one poster per page
python -m plexnfc.book --collection "Poster Book"   # only a Plex collection (recommended)
python -m plexnfc.book --layout grid --page-size A4 # four per page, A4 paper
```

This writes `out/book.html` and `out/tags.csv`. Open `book.html` in Chrome, then **Print** with margins set to *None* and *Background graphics* on. Print straight to paper or save as a PDF for a print shop.

Each page has a dashed **NFC** circle where the sticker goes, in the same spot on every page. There's also a QR code with the same link, for phones without NFC (`--no-qr` leaves it out). The last page holds **Pause / Resume / Stop** control tags, which work well on the back cover.

A tip: make a Plex collection called "Poster Book" with only the movies you want. A 300-movie library makes a very thick book.

## 3. Write the tags

For each movie in `out/tags.csv`:

1. In NFC Tools, go to **Write → Add a record → URL**, and paste the URL.
2. Tap **Write** and hold the phone on a blank sticker.
3. Stick it over that movie's NFC circle.

If you serve plexnfc over HTTPS, Chrome on Android gets a **Write tag** button on `/tags`, which makes this step much faster.

Put the stickers on the **back** of the page, or between pages in a page protector, so they don't show. One sheet of paper between the phone and the tag is fine. Don't stack tags directly on top of each other on facing pages; offset them, or give every other page a blank backing.

## Optional: Home Assistant

plexnfc can run a Home Assistant script before every movie. The script can turn the TV on, switch to the right HDMI input and open Plex, so one tap goes from a dark TV to the movie playing. Home Assistant can also start the movie itself through its Plex integration, which is the recommended setup for Apple TV. See [docs/home-assistant.md](docs/home-assistant.md) for setup and example scripts for Apple TV, Shield, LG, Samsung and Google TV.

The **Marquee** integration (`custom_components/marquee`, installable with HACS) works the other way round. Each Marquee player becomes a media player in Home Assistant, with *Movie started*, *paused*, *resumed* and *stopped* triggers for automations, such as dimming the lights. See [the integration's section](docs/home-assistant.md#the-marquee-integration-automations-that-follow-the-movie).

## Optional: play on Jellyfin

Plex for Apple TV can't be remote-started, but Swiftfin (Jellyfin's Apple TV app) can. With `JELLYFIN_URL`, `JELLYFIN_API_KEY` and `JELLYFIN_PLAYER` set, taps, controls and `/now` go to that Jellyfin app instead of the Plex player. Plex still makes the book and the tag list, so the movies need to be in both libraries. `?client=jellyfin:NAME` picks a Jellyfin player for one tag.

## Optional: a "Movie Remote" with a screen

[esphome/](esphome/) has firmware for an ESP32-S3 box with an NFC reader and a 3.5" touchscreen. Hold a page to the box and the movie starts. The screen then shows the poster, the progress, and play/pause, ±30 s, stop and TV-off buttons. It costs about $40 in parts. The parts list and wiring are in [esphome/README.md](esphome/README.md).

## Tag URLs

| URL | What it does |
| --- | --- |
| `/m/tmdb-603` | Play the movie from the beginning |
| `/m/tmdb-603?resume=1` | Resume where it was left off |
| `/m/tmdb-603?client=Bedroom TV` | Play on a different player (or `?client=media_player.xyz` to play through Home Assistant) |
| `/pause`, `/play`, `/stop` | Control whatever is playing |
| `/now`, `/toggle`, `/seek?by=30` | Used by the Movie Remote: what's playing (JSON), play/pause, skip |
| `/off` | Run your Home Assistant "off" script (needs `HA_OFF_SCRIPT`) |

Tags store the movie's TMDB/IMDb ID rather than Plex's internal ID, so they keep working if you rebuild your library or move servers.

## Troubleshooting

- **"Couldn't find a Plex player named…"**: the TV app has to be open (or at least running), and remote control has to be on. The error page lists the players plexnfc can see.
- **Nothing happens when tapping**: on iPhone, the screen must be on and unlocked. Hold the top edge of the phone on the sticker. On Android, NFC must be turned on in settings.
- **The link opens but the page won't load**: the phone has to be on your home Wi-Fi, and `PUBLIC_BASE_URL` has to be an address the phone can reach.
- **Security**: anyone on your network who has a URL can start a movie. Don't expose port 8080 to the internet.

## Development

```bash
pip install -r requirements.txt pytest
pytest
```

The control panel is a React app (TanStack Query and Table) in `panel/`. It's built into `plexnfc/static/panel`, and the build is committed, so the Home Assistant app and Marquee Server don't need Node. After changing it:

```bash
cd panel
npm install
npm run build
```

`npm run dev` serves it with live reload, passing API requests to a server on port 8080.
