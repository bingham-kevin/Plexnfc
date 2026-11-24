# Music

The control panel's **Music** section plays music from all your sources on AirPlay, Cast and Music Assistant speakers, and links NFC cards to albums and playlists. Like **Movies**, it has four pages: **Library**, **Cards**, **Book** and **Sources**. Audiobooks have their own **Books** section: see [docs/books.md](books.md).

## Where music comes from

- **Music Assistant**: your library and every provider it has (Apple Music, Spotify, Tidal…). Connect it under **Music › Sources › Music Assistant** with its address (port 8095, such as `http://homeassistant.local:8095`) and a long-lived token. To make the token, open **Settings › Profile** in Music Assistant, and under **Long-lived access tokens** make one called Marquee. Through the Marquee integration in Home Assistant alone, Music Assistant can only be searched, not browsed.
- **Plex**: the music libraries on the Plex server Marquee uses for movies. Nothing to set up.
- **Jellyfin**: the music on each Jellyfin server added under **Movies › Sources**.
- **Music folders**: folders of audio files on the server, or on a mounted network share. Add them under **Music › Sources › Music folders**.
  - Albums come from the files' tags (MP3, M4A, FLAC, Ogg and WAV, read with mutagen), or from `Artist/Album/01 Title.mp3` folders when there are no tags.
  - The pictures come from a `cover.jpg` or `folder.jpg` in the album's folder, or from the files themselves.
  - `.m3u` and `.m3u8` files become playlists.
  - Folders are read again every 10 minutes, or straight away with **Read again**.

Choose a source to browse its albums, artists, playlists and tracks, or leave it on **Search everywhere** to search all of them.

## Where it plays

Pick one or more speakers under **Speakers**, then use **Play**, **Play next** or **Add to queue** on an album, artist, playlist or track. The ▶ beside a track plays the album or playlist from that track.

| Speakers | Played by | In sync when grouped | Notes |
|---|---|---|---|
| **Music Assistant** players | Music Assistant | Yes: choosing several makes a Music Assistant group | Plays music from every source. Music from Plex, Jellyfin and folders is passed to it as addresses on this server. **Ungroup** splits a group. |
| **AirPlay** (HomePods, Apple TVs, AirPlay speakers, Macs) | Marquee, with pyatv | No | HomePods and Apple TVs need **Allow speaker access** set to everyone on the same network (Home app › Home settings › Speakers & TV). Apple TVs and Macs also ask to be paired once: see [Pairing Apple TVs](#pairing-apple-tvs). AirPlay can't pause a stream: ▶ after ⏸ starts the track again, where it was if the server has ffmpeg. |
| **Cast** (Chromecast, Google/Nest speakers, Cast groups) | Marquee, with pychromecast | Use a speaker group made in Google Home: it shows up as one speaker | |
| **Marquee** (an Apple TV, iPhone or iPad paired as a TV, shown as "Den (Marquee)") | The Marquee app itself | No | Opens Marquee if it isn't open, and shows **Now Playing** while it plays. It plays MP3, AAC/M4A, FLAC, WAV and AIFF as they are. Its volume is the TV's. |

Music Assistant's own music (Apple Music and so on) plays only on Music Assistant's speakers.

For AirPlay, Cast and Marquee speakers, Marquee keeps the queue itself. It moves on when the first speaker finishes a track. Choosing several of them sends each the same track, but they aren't sample-accurate together.

A format a speaker can't play is turned into MP3 by ffmpeg, if the server has it:
- AirPlay plays MP3, FLAC, WAV and Ogg as they are.
- Cast plays MP3, M4A/AAC, FLAC, Ogg and WAV.
- Jellyfin always sends MP3.

Speakers fetch files and pictures from the server's public address (`PUBLIC_BASE_URL`), so it has to be reachable from them. The addresses are signed, so nothing else on the network can read your music through them.

**Playing** shows what's on each speaker or group, with previous, play/pause, next, stop and the queue. It shows only music played through Music Assistant or Marquee. It doesn't show what an Apple TV plays in its own apps, and it never controls that.

## Now Playing on the TV

The Marquee app has a **Now Playing** screen for music and books, like its movie screen:

- **Music:** the album cover, title and artist, a timeline, the album's tracks in order with what's up next, and lyrics. Synced lyrics highlight and scroll along with the song.
- **Books:** the cover, author, a timeline for the whole book with its chapters marked, and the chapter list. ⏮ ⏭ move between chapters, and the other buttons skip back or forward 30 seconds.

It shows on its own when Marquee is the speaker. To watch music playing on other speakers, open it from the Now Playing button (top right on Apple TV, at the bottom on iPhone and iPad). Its buttons control those speakers.

A reader can show its music on a TV too. Under **Devices › Readers**, set **Shows Now Playing on** to a Marquee screen. Each music or book card held to that reader then puts Now Playing on that TV. With **Turn the TV on for it**, Marquee is opened (which wakes the TV). Without it, Now Playing only shows if Marquee is already open.

### Where lyrics come from

Marquee tries these sources in order:

1. The source's own lyrics:
   - Plex: lyrics it found or that sit beside the files (Plex Pass);
   - Jellyfin: its lyrics;
   - folders: a `.lrc` or `.txt` file with the same name as the track;
   - Music Assistant: lyrics in its metadata.
2. If the source has none, [LRCLIB](https://lrclib.net), a free, open lyrics database. Marquee sends it only the title, artist, album and length. To turn this off, set `LYRICS_LRCLIB=0`.

`.lrc` files with timestamps give synced lyrics. Plain text is shown as it is.

## Music cards

**Music › Cards** links a whole shelf of albums at once, the way **Movies › Cards** does for movies:

1. Choose a source, then **Albums**, **Playlists** or **An artist's albums**. Everything in the list is picked; untick what you don't want.
2. Choose the speaker the cards play on, and leave **Skip albums that already have a card** on to fill in only the missing ones.
3. Click **Scan cards**. Each album comes up in turn; hold a blank card on a Movie Remote (or an NFC reader in Home Assistant) and it moves on to the next. **Skip** and **Back** move through the list.

To link one card from **Music › Library**, open any album, artist, playlist or track with **one** speaker chosen:

- **Link next card**, then hold a blank card on a reader.
- **Link card XX-XX-XX-XX** links the card a reader has just read, if it isn't linked to anything.

Tapping the card later plays that music on that speaker, replacing what's playing there. **Music › Cards** lists the music cards; movie cards stay under **Movies › Cards**. Cards linked to Music Assistant music before the Music section existed keep working.

## A printable book of albums

**Music › Book** makes a book like the movie book: one or four covers to a page, each with a gold circle where its card goes. Choose **Everything with a card** or a source's albums or playlists, the layout and the paper, then **Open printable book** and print at 100% with background graphics on. With **QR code** on, each page with a card gets a code that plays it from a phone (it opens `/c/<card>` on the server).

## Pairing Apple TVs

Most Apple TVs (and Macs) won't take AirPlay audio from Marquee until it's paired with them; streaming to one that isn't fails with "Connection Authorization Required". Their speakers show a **Pair** button in the Music tab:

1. Click **Pair** on the speaker. The TV turns on its screen and shows a 4-digit PIN.
2. Type the PIN into the box under the speaker and click **Pair**.

The credentials are saved in `airplay_pairings.json` next to Marquee's other settings (readable only by you), so it's once per Apple TV. **Unpair** forgets them; remove "Marquee Server" on the Apple TV too (Settings › AirPlay and HomeKit) if you want it gone there. HomePods don't need pairing, and neither does an Apple TV already paired in the **Apple TVs** section (for opening Marquee on it): AirPlay uses that pairing too.

