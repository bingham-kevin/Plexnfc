# Music

The control panel's **Music** tab plays music from all your sources on AirPlay, Cast and Music Assistant speakers, and links NFC cards to music.

## Where music comes from

- **Music Assistant**: your library and every provider it has (Apple Music, Spotify, Tidal…). Connect it under **Music settings › Music Assistant** with its address (port 8095, such as `http://homeassistant.local:8095`) and a long-lived token. To make the token, open **Settings › Profile** in Music Assistant, and under **Long-lived access tokens** make one called Marquee. Through the Marquee integration in Home Assistant alone, Music Assistant can only be searched, not browsed.
- **Plex**: the music libraries on the Plex server Marquee uses for movies. Nothing to set up.
- **Jellyfin**: the music on each Jellyfin server added on the **Sources** tab.
- **Music folders**: folders of audio files on the server, or on a mounted network share. Add them under **Music settings › Music folders**.
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
| **AirPlay** (HomePods, Apple TVs, AirPlay speakers, Macs) | Marquee, with pyatv | No | HomePods and Apple TVs need **Allow speaker access** set to everyone on the same network (Home app › Home settings › Speakers & TV). Apple TVs and Macs also ask to be paired once: see [Pairing Apple TVs](#pairing-apple-tvs). AirPlay can't pause a stream: ▶ after ⏸ starts the track again. |
| **Cast** (Chromecast, Google/Nest speakers, Cast groups) | Marquee, with pychromecast | Use a speaker group made in Google Home: it shows up as one speaker | |

Music Assistant's own music (Apple Music and so on) plays only on Music Assistant's speakers.

For AirPlay and Cast speakers, Marquee keeps the queue itself. It moves on when the first speaker finishes a track. Choosing several of them sends each the same track, but they aren't sample-accurate together.

A format a speaker can't play is turned into MP3 by ffmpeg, if the server has it:
- AirPlay plays MP3, FLAC, WAV and Ogg as they are.
- Cast plays MP3, M4A/AAC, FLAC, Ogg and WAV.
- Jellyfin always sends MP3.

Speakers fetch files and pictures from the server's public address (`PUBLIC_BASE_URL`), so it has to be reachable from them. The addresses are signed, so nothing else on the network can read your music through them.

**Playing** shows what's on each speaker or group, with previous, play/pause, next, stop and the queue. It shows only music played through Music Assistant or Marquee. It doesn't show what an Apple TV plays in its own apps, and it never controls that.

## Music cards

On any album, artist, playlist or track, with **one** speaker chosen:

- **Link next card**, then hold a blank card on a Movie Remote, or on an NFC reader in Home Assistant (a Tessera screen, the Home Assistant app…).
- **Link card XX-XX-XX-XX** links the card a reader has just read, if it isn't linked to anything.

Tapping the card later plays that music on that speaker, replacing what's playing there. Linked cards are listed on the **Cards** tab with the movie cards. Cards linked to Music Assistant music before the Music tab existed keep working.

## Pairing Apple TVs

Most Apple TVs (and Macs) won't take AirPlay audio from Marquee until it's paired with them; streaming to one that isn't fails with "Connection Authorization Required". Their speakers show a **Pair** button in the Music tab:

1. Click **Pair** on the speaker. The TV turns on its screen and shows a 4-digit PIN.
2. Type the PIN into the box under the speaker and click **Pair**.

The credentials are saved in `airplay_pairings.json` next to Marquee's other settings (readable only by you), so it's once per Apple TV. **Unpair** forgets them; remove "Marquee Server" on the Apple TV too (Settings › AirPlay and HomeKit) if you want it gone there. HomePods don't need pairing, and neither does an Apple TV already paired in the **Apple TVs** section (for opening Marquee on it): AirPlay uses that pairing too.

