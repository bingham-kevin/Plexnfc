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

This writes `out/book.html` and `out/tags.csv`. Open `book.html` in Chrome, then **Print** at 100% scale with *Background graphics* on. The book sets its own margins. Print straight to paper or save as a PDF for a print shop.

Each page has a dashed **NFC** circle where the sticker goes, in the same spot on every page. There's also a QR code with the same link, for phones without NFC (`--no-qr` leaves it out). The last page holds **Pause / Resume / Stop** control tags, which work well on the back cover.

A tip: make a Plex collection called "Poster Book" with only the movies you want. A 300-movie library makes a very thick book.

## 3. Write the tags

For each movie in `out/tags.csv`:

1. In NFC Tools, go to **Write → Add a record → URL**, and paste the URL.
2. Tap **Write** and hold the phone on a blank sticker.
3. Stick it over that movie's NFC circle.

If you serve plexnfc over HTTPS, Chrome on Android gets a **Write tag** button on `/tags`, which makes this step much faster.

### Or link blank cards, for the Movie Remote

With a [Movie Remote](esphome/README.md), nothing has to be written on the cards. Every NFC card has a fixed ID, and Marquee Server keeps a list of which ID plays which movie:

1. In the control panel, open **Cards** and choose the movies: a Plex **collection**, a **label**, or **Pick movies** by hand. Leave **Skip movies that already have a card** on to fill in only the ones that are missing.
2. Tap **Scan cards**. The first movie comes up; hold a blank card on the Movie Remote. The card is linked, and the next movie comes up. **Skip** and **Back** move through the list.
3. Stick each card onto its movie's page as you go.

To link one card, use **Link card** next to the movie under **Tags**. If the Movie Remote reads a card that isn't linked, the **Cards** page offers to link it. While a scan is going, cards held on a Movie Remote are linked instead of played.

Phones can't read these cards on their own, since there's no address on them. For phones, write the tag URLs as above; a written tag works on the Movie Remote too.

Put the stickers on the **back** of the page, or between pages in a page protector, so they don't show. One sheet of paper between the phone and the tag is fine. Don't stack tags directly on top of each other on facing pages; offset them, or give every other page a blank backing.

## Optional: Home Assistant

plexnfc can run a Home Assistant script before every movie. The script can turn the TV on, switch to the right HDMI input and open Plex, so one tap goes from a dark TV to the movie playing. Home Assistant can also start the movie itself through its Plex integration, which is the recommended setup for Apple TV. See [docs/home-assistant.md](docs/home-assistant.md) for setup and example scripts for Apple TV, Shield, LG, Samsung and Google TV.

The **Marquee** integration (`custom_components/marquee`, installable with HACS) works the other way round. Each Marquee player becomes a media player in Home Assistant, with *Movie started*, *paused*, *resumed* and *stopped* triggers for automations, such as dimming the lights. See [the integration's section](docs/home-assistant.md#the-marquee-integration-automations-that-follow-the-movie).

## Movie sources: Plex, Jellyfin and folders

Movies can come from Plex, from Jellyfin servers, and from folders of movie files, all at once. Add Jellyfin servers and folders on the control panel's **Sources** tab; they're saved in `sources.json`, next to the Plex sign-in. Their movies show up in the tag list, the card scan, the book and Marquee's library alongside Plex's, and the **Sources** tab can leave Plex out if you don't use it.

- **Jellyfin**: the server's address, then sign in with a Jellyfin user (the password goes to Jellyfin and isn't kept; Marquee Server keeps the sign-in token Jellyfin hands back) or paste an API key from Jellyfin's **Dashboard → API Keys**. Optionally name one library; otherwise every movie library is used. Collections and tags come from Jellyfin.
- **Folder**: a path on the machine Marquee Server runs on, read with the folders inside it. For a network share (SMB or NFS), mount it first and give the mount's path: `/Volumes/Movies` on a Mac, `/media/...` or `/share/...` in the Home Assistant app (add the share under **Settings → System → Storage**). Movies are named from their files or folders, `Alien (1979)/Alien (1979).mkv` or `Alien.1979.1080p.mkv`; a Kodi/Jellyfin-style `movie.nfo` (or `<file name>.nfo`) beside a movie gives its title, year, plot, collection (`<set>`) and tags. Trailers, extras folders and second parts (`cd2`) are skipped. Posters come from `poster.jpg`/`folder.jpg`/`cover.jpg` in the movie's own folder, `<file name>-poster.jpg`, or the cover art inside the file; a movie with none gets a plain card with its title. On a Mac, macOS asks once whether Marquee Server may read a folder in Documents, Desktop or Downloads, or on a network volume.

Every movie keeps its own key, so tags and cards from different sources never clash, and Plex keys are unchanged:

| Source | Key | Stays the same while… |
| --- | --- | --- |
| Plex | `tmdb-603` (or `imdb-…`, `tvdb-…`, `rk-…`) | the movie is matched in Plex |
| Jellyfin | `jf-tmdb-603`, else `jf-<Jellyfin item ID>` | the movie has a TMDB/IMDb/TVDB ID, or isn't re-added |
| Folder | `file-tmdb-603` (from a `movie.nfo` or `{tmdb-603}` in the name), else `file-<hash>` | the file keeps its name and place inside the folder (the folder itself can move) |

Where they play:

- **Marquee** plays movies from every source. Jellyfin movies stream from Jellyfin. Folder movies stream from Marquee Server itself, at an address signed for that movie: MP4, M4V and MOV files as they are; other files (MKV, AVI…) need **ffmpeg** on the server (`brew install ffmpeg` on a Mac; the Docker image and Home Assistant app include it), which repackages them for Apple TV as they play, converting only what Apple TV can't play. While a repackaged movie is starting, you can only skip ahead as far as ffmpeg has got.
- **Jellyfin players** (Swiftfin) play Jellyfin movies. With no `JELLYFIN_URL` set, the first Jellyfin source's players are used.
- **Plex players** and Home Assistant's Plex integration play Plex movies only.

## Optional: play on Jellyfin

Plex for Apple TV can't be remote-started, but Swiftfin (Jellyfin's Apple TV app) can. With `JELLYFIN_URL`, `JELLYFIN_API_KEY` and `JELLYFIN_PLAYER` set, taps, controls and `/now` go to that Jellyfin app instead of the Plex player. Plex still makes the book and the tag list, so the movies need to be in both libraries. `?client=jellyfin:NAME` picks a Jellyfin player for one tag.

## Optional: a "Movie Remote" with a screen

[esphome/](esphome/) has firmware for a 4" ESP32 touchscreen (the Hosyond ESP32-32E) with a PN532 NFC reader. Hold a page to the box and the movie starts on your Marquee player. The screen then shows the poster, the progress, and play/pause, ±30 s and stop buttons. The box pairs with Marquee Server like Marquee does, and you choose its player in the control panel. The parts list and wiring are in [esphome/README.md](esphome/README.md).

## Tag URLs

| URL | What it does |
| --- | --- |
| `/m/tmdb-603` | Play the movie from the beginning |
| `/m/tmdb-603?resume=1` | Resume where it was left off |
| `/m/tmdb-603?client=Bedroom TV` | Play on a different player (or `?client=media_player.xyz` to play through Home Assistant) |
| `/pause`, `/play`, `/stop` | Control whatever is playing |
| `/now`, `/toggle`, `/seek?by=30` | Used by the Movie Remote: what's playing (JSON), play/pause, skip |
| `/off` | Run your Home Assistant "off" script (needs `HA_OFF_SCRIPT`) |

Tags store the movie's TMDB/IMDb ID rather than Plex's internal ID, so they keep working if you rebuild your library or move servers. Jellyfin and folder movies have keys of their own (`jf-…`, `file-…`); see [Movie sources](#movie-sources-plex-jellyfin-and-folders).

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
