# Plexnfc: Marquee and the movie book

A physical book of movie posters. Tap a page with your phone, or hold it to a Movie Remote, and the movie starts playing on your TV in **Marquee**, the Apple TV app in [tvos/](tvos/).

```
 tap ──► Marquee Server ──► movie waits in Marquee's inbox
                        └─► wakes the Apple TV, opens Marquee ──► Marquee streams it from Plex, Jellyfin or a folder
```

No app is needed on the phone. iPhones (XS and newer) and Android phones read URL tags on their own: tap, then open the link from the notification.

## What you need

- An **Apple TV** with Marquee ([tvos/README.md](tvos/README.md)).
- Movies in **Plex**, **Jellyfin**, or **folders** of movie files, in any mix.
- **Marquee Server**, always on, on the same network:
  - the **Mac app** ([macos/README.md](macos/README.md)), which is the easiest, or
  - **Docker** or **Python** on another always-on machine: a Raspberry Pi, a NAS, or the Plex server itself.
- **NFC stickers**: NTAG213 or NTAG215, about 25 mm round. They're cheap in packs of 50 or 100. Avoid "on-metal" tags and foil-backed paper.
- An NFC writing app: **NFC Tools** (free, iOS and Android). You don't need one for Movie Remote cards.
- A printer, plus a binder, photo album or bookbinding service.

## 1. Set up

**On a Mac**, install and run the Marquee Server app, as [macos/README.md](macos/README.md) describes. It serves the tags at `http://marquee.local:8080`, so the Mac doesn't need a fixed address.

**With Docker or Python**, give the machine a **fixed IP or hostname**, because that address is written onto every tag. Then:

```bash
git clone <this repo> && cd Plexnfc
cp .env.example .env      # then fill in your Plex library, Marquee player and tag address
```

Then run it with Docker:

```bash
docker compose up -d
```

or with plain Python:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m plexnfc.server
```

**Connect Marquee.** Open the control panel (`http://<that-machine>:8080/`) and choose **Sign in with Plex**, if you use Plex. Then open Marquee on the Apple TV, and approve the code it shows under **Devices**, naming it after its room. That name is the player taps go to: choose the default one in the panel (or with `APP_PLAYER`). To have the server wake the Apple TV and open Marquee by itself, pair the Apple TV too, under **Devices › Open Marquee automatically**.

**Movies › Tags** lists every movie with the URL for its tag.

## 2. Make the book

```bash
python -m plexnfc.book                              # whole library, one poster per page
python -m plexnfc.book --collection "Poster Book"   # only a Plex collection (recommended)
python -m plexnfc.book --layout grid --page-size A4 # four per page, A4 paper
```

The book can also be made from the control panel (**Movies › Book**). The command writes `out/book.html` and `out/tags.csv`. Open `book.html` in Chrome, then **Print** at 100% scale with *Background graphics* on. The book sets its own margins. Print straight to paper or save as a PDF for a print shop.

Each page has a dashed **NFC** circle where the sticker goes, in the same spot on every page. There's also a QR code with the same link, for phones without NFC (`--no-qr` leaves it out). The last page holds **Pause / Resume / Stop** control tags, which work well on the back cover.

A tip: make a Plex collection called "Poster Book" with only the movies you want. A 300-movie library makes a very thick book.

## 3. Write the tags

For each movie in `out/tags.csv`:

1. In NFC Tools, go to **Write → Add a record → URL**, and paste the URL.
2. Tap **Write** and hold the phone on a blank sticker.
3. Stick it over that movie's NFC circle.

If you serve Marquee Server over HTTPS, Chrome on Android gets a **Write tag** button on `/tags`, which makes this step much faster.

### Or link blank cards, for the Movie Remote

With a [Movie Remote](esphome/README.md), nothing has to be written on the cards. Every NFC card has a fixed ID, and Marquee Server keeps a list of which ID plays which movie:

1. In the control panel, open **Movies › Cards** and choose the movies: a Plex **collection**, a **label**, or **Pick movies** by hand. Leave **Skip movies that already have a card** on to fill in only the ones that are missing.
2. Tap **Scan cards**. The first movie comes up; hold a blank card on the Movie Remote. The card is linked, and the next movie comes up. **Skip** and **Back** move through the list.
3. Stick each card onto its movie's page as you go.

To link one card, use **Link card** next to the movie under **Movies › Tags**. If the Movie Remote reads a card that isn't linked, the **Cards** page offers to link it. While a scan is going, cards held on a Movie Remote are linked instead of played.

A card can play music or an audiobook instead. **Music › Cards** and **Books › Cards** scan cards for albums, playlists and audiobooks the same way, to play on a chosen speaker; a book card picks up where you left off ([music](docs/music.md#music-cards), [books](docs/books.md#book-cards)). **Music › Book** and **Books › Book** print matching books of covers.

Phones can't read these cards on their own, since there's no address on them. For phones, write the tag URLs as above; a written tag works on the Movie Remote too.

Put the stickers on the **back** of the page, or between pages in a page protector, so they don't show. One sheet of paper between the phone and the tag is fine. Don't stack tags directly on top of each other on facing pages; offset them, or give every other page a blank backing.

## Optional: Home Assistant

For an Apple TV paired under **Devices**, Marquee Server wakes it and opens Marquee itself. For anything else in the way, such as a TV that has to be switched on and set to the right HDMI input, Marquee Server can run a Home Assistant script before every movie (`HA_SCRIPT`, or one per player with `PLAYER_SCRIPTS`). See [docs/home-assistant.md](docs/home-assistant.md) for setup and example scripts.

The **Marquee** integration (`custom_components/marquee`, installable with HACS) works the other way round. Each Marquee player becomes a media player in Home Assistant, with *Movie started*, *paused*, *resumed* and *stopped* triggers for automations, such as dimming the lights. It also passes on cards read by NFC readers in Home Assistant, and can carry music cards to Music Assistant when Marquee isn't connected to it directly. See [the integration's section](docs/home-assistant.md#the-marquee-integration-automations-that-follow-the-movie).

## Movie sources: Plex, Jellyfin and folders

Movies can come from Plex, from Jellyfin servers, and from folders of movie files, all at once. Add them with **Add source** on the control panel's **Sources** tab (choose Network or local folder, Jellyfin server, or Plex); they're saved in `sources.json`, next to the Plex sign-in. Their movies show up in the tag list, the card scan, the book and Marquee's library alongside Plex's, and **Remove** beside Plex leaves it out if you don't use it.

- **Jellyfin**: the server's address, then sign in with a Jellyfin user (the password goes to Jellyfin and isn't kept; Marquee Server keeps the sign-in token Jellyfin hands back) or paste an API key from Jellyfin's **Dashboard → API Keys**. Optionally name one library; otherwise every movie library is used. Collections and tags come from Jellyfin.
- **Folder**: a path on the machine Marquee Server runs on, read with the folders inside it. For a network share (SMB or NFS), mount it first and give the mount's path: `/Volumes/Movies` on a Mac, or wherever Docker or Linux mounts it. Movies are named from their files or folders, `Alien (1979)/Alien (1979).mkv` or `Alien.1979.1080p.mkv`; a Kodi/Jellyfin-style `movie.nfo` (or `<file name>.nfo`) beside a movie gives its title, year, plot, collection (`<set>`) and tags. Trailers, extras folders and second parts (`cd2`) are skipped. Posters come from `poster.jpg`/`folder.jpg`/`cover.jpg` in the movie's own folder, `<file name>-poster.jpg`, or the cover art inside the file; a movie with none gets a plain card with its title. On a Mac, macOS asks once whether Marquee Server may read a folder in Documents, Desktop or Downloads, or on a network volume. **Browse…** beside the path picks a folder on the server, starting from its mounted shares and disks.

**Matching folder movies with TMDB.** A folder movie named loosely, or with no `.nfo`, can be matched with TMDB (The Movie Database, which Plex and Jellyfin use) for its proper title, year, summary, IDs, collection and poster. Paste a free TMDB API key (or read access token) from themoviedb.org's **Settings → API** with **Set up** beside *Matching folder movies with TMDB* on the **Sources** tab, then:

- **Match all** beside a folder matches its movies that have no ID yet, in the background, taking only sure matches: the same title and a year within one (or, with no year, the only movie of that title). The rest are left alone.
- **Match…** (or **Edit**) beside a movie under **Movies › Tags** searches TMDB to pick its match yourself, or changes its title, year, summary, collections, labels or TMDB ID by hand. **Reset to the file** forgets it all.

Matches and changes are kept in `folder_metadata.json` in the data folder, never written to the share. A match gives the movie its TMDB ID, so its tag address becomes `tmdb-…`; it's still found by the key its file name gave (`t-…`), so tags and cards made before keep working. A movie matched by hand shows TMDB's poster; one matched automatically keeps a poster file beside it, if it has one.

A movie in several sources is one movie, with one tag address: a tag or card is linked to the movie, not to a source. Copies are matched by their TMDB/IMDb/TVDB ID, else by title and year (letters and digits only, so `Alien: Covenant` and `Alien Covenant (2017).mkv` match), unless their IDs disagree. Its key is:

| Key | When |
| --- | --- |
| `tmdb-603` (or `imdb-…`, `tvdb-…`) | any source knows its ID: Plex's keys are unchanged |
| `rk-12345` | only Plex has it, without an ID |
| `t-aliencovenant-2017` | no source knows its ID: from its title and year |

A movie is found by any key it has had, and a card also remembers its movie's title and year, so a card linked while the movie was in Plex still plays it from a folder if Plex is gone or signed out. To keep tag addresses the same whatever happens to Plex, give folder movies their IDs (`{tmdb-603}` in the name, or a `movie.nfo`).

**Order.** The **Sources** tab lists the sources in the order movies play from them (Plex first, unless you move it): a tap plays the first copy the player can play, and if that source doesn't answer (Plex signed out or switched off, say), the next one. **Now Playing** shows which source a Marquee movie is playing from.

Marquee plays movies from every source, from the first one in the order that has it and answers. Jellyfin movies stream from Jellyfin, passed on by Marquee Server, so they play even when Jellyfin's address only works on the server (a Tailscale name, say). Folder movies stream from Marquee Server itself, at an address signed for that movie: MP4, M4V and MOV files as they are; other files (MKV, AVI…) need **ffmpeg** on the server (`brew install ffmpeg` on a Mac; the Docker image doesn't include it, so there use MP4, M4V or MOV files), which repackages them for Apple TV as they play, converting only what Apple TV can't play. While a repackaged movie is starting, you can only skip ahead as far as ffmpeg has got.

## Music and books: AirPlay, Cast and Music Assistant

The control panel has three sections, **Movies**, **Music** and **Books**, each with **Cards**, **Book** and **Sources** pages beside its library (**Tags** for movies).

The **Music** section browses and searches music from Music Assistant, Plex, Jellyfin and folders of audio files, and plays it on AirPlay speakers, Cast speakers and Music Assistant's players, with a queue, volume and grouping. See [docs/music.md](docs/music.md).

The **Books** section plays audiobooks from Audiobookshelf on the same speakers, resumes where you left off and reports progress back. See [docs/books.md](docs/books.md).

## Optional: a "Movie Remote" with a screen

[esphome/](esphome/) has firmware for a 4" ESP32 touchscreen (the Hosyond ESP32-32E) with a PN532 NFC reader. Hold a page to the box and the movie starts on your Marquee player. The screen then shows the poster, the progress, and play/pause, ±30 s and stop buttons. The box pairs with Marquee Server like Marquee does, and you choose its player in the control panel. The parts list and wiring are in [esphome/README.md](esphome/README.md).

## Tag URLs

| URL | What it does |
| --- | --- |
| `/m/tmdb-603` | Play the movie from the beginning |
| `/m/tmdb-603?resume=1` | Resume where it was left off |
| `/m/tmdb-603?client=Bedroom` | Play on a different Marquee player |
| `/pause`, `/play`, `/stop` | Control whatever is playing |
| `/now`, `/toggle`, `/seek?by=30` | Used by the Movie Remote: what's playing (JSON), play/pause, skip |
| `/off` | Run your Home Assistant "off" script (needs `HA_OFF_SCRIPT`) |

Tags store the movie's TMDB/IMDb ID rather than Plex's internal ID, so they keep working if you rebuild your library or move servers. Movies only in Jellyfin or a folder, with no ID, get keys from their title and year (`t-…`); see [Movie sources](#movie-sources-plex-jellyfin-and-folders).

## Troubleshooting

- **"Marquee on … isn't connected to this server"**: approve Marquee on that Apple TV under **Devices**, with the name the tag or default player uses.
- **The movie waits but the TV stays dark**: pair the Apple TV under **Devices › Open Marquee automatically**, or set a Home Assistant get-ready script.
- **Nothing happens when tapping**: on iPhone, the screen must be on and unlocked. Hold the top edge of the phone on the sticker. On Android, NFC must be turned on in settings.
- **The link opens but the page won't load**: the phone has to be on your home Wi-Fi, and `PUBLIC_BASE_URL` has to be an address the phone can reach.
- **Security**: anyone on your network who has a URL can start a movie. Don't expose port 8080 to the internet.

## Development

```bash
pip install -r requirements.txt pytest
pytest
```

The control panel is a React app (TanStack Query and Table) in `panel/`. It's built into `plexnfc/static/panel`, and the build is committed, so Marquee Server doesn't need Node. After changing it:

```bash
cd panel
npm install
npm run build
```

`npm run dev` serves it with live reload, passing API requests to a server on port 8080.
