"""Music from several sources, as one library: Music Assistant, Plex's music libraries,
Jellyfin's music, and folders of audio files.

Every item is a plain dict::

    {"id", "source", "kind", "name", "artist", "album", "image", "duration", "number"}

``kind`` is ``artist``, ``album``, ``playlist`` or ``track`` (Music Assistant also has
``radio``). ``id`` is ``<source>|<kind>|<ref>``, the source's own reference after the
kind, so any item can be found again from its ID alone. ``image`` is a URL the control
panel can show: Music Assistant's own, or Marquee's ``/panel/api/music/art`` for sources
whose pictures need a token.

Playing is the speakers' business (speakers.py); a source only turns an item into its
tracks, and a track into something a speaker can play: an address (``stream``) for
AirPlay, Cast and Music Assistant speakers, or, for Music Assistant's own items, its URI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

log = logging.getLogger(__name__)

KINDS = ("artist", "album", "playlist", "track")
AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".wav": "audio/wav",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff",
    ".alac": "audio/mp4", ".wma": "audio/x-ms-wma",
}
PLAYLIST_TYPES = (".m3u", ".m3u8")
COVER_NAMES = ("cover", "folder", "front", "album", "artwork")


class MusicSourceError(Exception):
    pass


def item_id(source: str, kind: str, ref: str) -> str:
    return f"{source}|{kind}|{ref}"


def split_id(ident: str) -> tuple[str, str, str]:
    """``(source, kind, ref)`` from an item ID. Raises MusicSourceError if it isn't one."""
    parts = str(ident or "").split("|", 2)
    if len(parts) != 3 or not all(parts):
        raise MusicSourceError("That isn't a music item")
    return parts[0], parts[1], parts[2]


def make_item(source: str, kind: str, ref, name, artist="", album="", image=None, duration=None,
              number=None, **extra) -> dict:
    item = {"id": item_id(source, kind, str(ref)), "source": source, "kind": kind,
            "name": str(name or "Untitled"), "artist": str(artist or ""), "album": str(album or ""),
            "image": image, "duration": int(duration) if duration else None,
            "number": int(number) if isinstance(number, (int, float)) or str(number or "").isdigit() else None}
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


def art_url(ident: str) -> str:
    """The panel's address for an item's picture (fetched through Marquee)."""
    return f"panel/api/music/art?{urlencode({'id': ident})}"


class MusicSource:
    """One place music comes from."""
    kind = ""

    def __init__(self, source_id: str, name: str):
        self.id = source_id
        self.name = name

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind}

    def browse(self, kind: str, query: str = "", offset: int = 0, limit: int = 100) -> list[dict]:
        raise NotImplementedError

    def open(self, kind: str, ref: str) -> dict:
        """``{"item", "albums", "tracks"}``: an artist's albums, an album's or playlist's tracks."""
        raise NotImplementedError

    def search(self, query: str, limit: int = 8) -> list[dict]:
        found = []
        for kind in ("artist", "album", "playlist", "track"):
            try:
                found += self.browse(kind, query, 0, limit)
            except MusicSourceError:
                continue
        return found

    def tracks(self, kind: str, ref: str) -> list[dict]:
        """Everything an item plays, in order."""
        if kind == "track":
            return [self.track(ref)]
        opened = self.open(kind, ref)
        if opened.get("tracks"):
            return opened["tracks"]
        out = []
        for album in opened.get("albums") or []:
            out += self.tracks("album", split_id(album["id"])[2])
        return out

    def track(self, ref: str) -> dict:
        raise NotImplementedError

    def stream(self, track: dict) -> tuple[str, str]:
        """``(url, content type)`` a speaker can play the track from."""
        raise MusicSourceError(f"{self.name} can't stream to this speaker")

    def art(self, kind: str, ref: str) -> tuple[bytes, str] | None:
        return None

    def lyrics(self, track: dict) -> dict | None:
        """The track's lyrics (see lyrics.py), if the source has them."""
        return None


# ---- Music Assistant ----------------------------------------------------------------

class AssistantSource(MusicSource):
    """Music Assistant's library and providers (Apple Music, Spotify...), reached directly.
    Its music plays only on Music Assistant's own speakers."""
    kind = "music_assistant"

    def __init__(self, music):
        super().__init__("ma", "Music Assistant")
        self.music = music  # music.Music

    @property
    def api(self):
        direct = self.music._direct()
        if direct is None:
            raise MusicSourceError("Music Assistant isn't connected directly. Connect it under Speakers.")
        return direct

    def _call(self, command: str, timeout: float = 15, **args):
        from .music import MusicError
        try:
            return self.api.call(command, timeout=timeout, **args)
        except MusicError as e:
            raise MusicSourceError(str(e)) from None

    def _item(self, raw: dict) -> dict | None:
        if not isinstance(raw, dict) or "://" not in str(raw.get("uri") or ""):
            return None
        kind = raw.get("media_type") or ""
        if kind not in KINDS + ("radio",):
            return None
        artists = raw.get("artists") or []
        album = raw.get("album") if isinstance(raw.get("album"), dict) else {}
        name = raw.get("name")
        if raw.get("version") and name:
            name = f"{name} ({raw['version']})"
        return make_item(self.id, kind, raw["uri"], name,
                         ", ".join(a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")),
                         album.get("name", ""), self.api.image_url(raw), raw.get("duration"),
                         raw.get("track_number") or raw.get("position"), uri=raw["uri"], media_type=kind)

    def _items(self, found) -> list[dict]:
        return [i for raw in (found if isinstance(found, list) else []) if (i := self._item(raw))]

    def browse(self, kind: str, query: str = "", offset: int = 0, limit: int = 100) -> list[dict]:
        if kind not in KINDS:
            return []
        args = {"limit": limit, "offset": offset, "order_by": "sort_name"}
        if query:
            args["search"] = query
        return self._items(self._call(f"music/{kind}s/library_items", **args))

    def search(self, query: str, limit: int = 8) -> list[dict]:
        if self.music._direct() is None:  # through Home Assistant: search is all it can do
            from .music import MusicError
            try:
                found = self.music.search(query)
            except MusicError as e:
                raise MusicSourceError(str(e)) from None
            return [make_item(self.id, i["media_type"], i["uri"], i["name"], i["artist"], image=i["image"],
                              uri=i["uri"], media_type=i["media_type"]) for i in found]
        found = self._call("music/search", search_query=query, limit=limit,
                           media_types=["artist", "album", "playlist", "track", "radio"])
        out = []
        for key in ("artists", "albums", "playlists", "tracks", "radio"):
            out += self._items((found or {}).get(key) if isinstance(found, dict) else [])
        return out

    @staticmethod
    def _parts(uri: str) -> tuple[str, str]:
        """``(provider, item ID)`` from a URI such as ``library://album/4``."""
        scheme, _, rest = uri.partition("://")
        return scheme, rest.split("/", 1)[1] if "/" in rest else rest

    def open(self, kind: str, ref: str) -> dict:
        found = self._call("music/item_by_uri", uri=ref)
        item = self._item(found) or make_item(self.id, kind, ref, ref)
        provider, ident = self._parts(ref)
        if kind == "artist":
            return {"item": item, "albums": self._items(self._call(
                "music/artists/artist_albums", item_id=ident, provider_instance_id_or_domain=provider))}
        if kind in ("album", "playlist"):
            tracks = self._call(f"music/{kind}s/{kind}_tracks", timeout=30, item_id=ident,
                                provider_instance_id_or_domain=provider)
            return {"item": item, "tracks": self._items(tracks)}
        return {"item": item, "tracks": [item]}

    def tracks(self, kind: str, ref: str) -> list[dict]:
        # Music Assistant expands albums, artists and playlists itself
        opened = self._item(self._call("music/item_by_uri", uri=ref))
        return [opened or make_item(self.id, kind, ref, ref, uri=ref, media_type=kind)]

    def track(self, ref: str) -> dict:
        return self.tracks("track", ref)[0]


# ---- Plex ---------------------------------------------------------------------------

class PlexMusicSource(MusicSource):
    """The music libraries on the Plex server Marquee uses for movies."""
    kind = "plex"

    def __init__(self, plex):
        super().__init__("plex", "Plex")
        self.plex = plex

    def _server(self):
        try:
            return self.plex.server
        except Exception as e:  # noqa: BLE001 - plexapi raises many kinds
            raise MusicSourceError(f"Couldn't reach Plex: {e}") from None

    def sections(self) -> list:
        try:
            return [s for s in self._server().library.sections() if s.type == "artist"]
        except MusicSourceError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MusicSourceError(f"Couldn't read Plex's libraries: {e}") from None

    def _item(self, obj) -> dict | None:
        kind = {"artist": "artist", "album": "album", "track": "track", "playlist": "playlist"}.get(
            getattr(obj, "type", None) or getattr(obj, "TYPE", ""))
        if not kind or (kind == "playlist" and getattr(obj, "playlistType", "audio") != "audio"):
            return None
        ref = str(obj.ratingKey)
        has_art = bool(getattr(obj, "thumb", None) or getattr(obj, "composite", None)
                       or getattr(obj, "parentThumb", None))
        artist = {"album": getattr(obj, "parentTitle", ""), "track": getattr(obj, "grandparentTitle", "")}.get(kind, "")
        if kind == "track" and getattr(obj, "originalTitle", None):
            artist = obj.originalTitle
        return make_item(self.id, kind, ref, obj.title, artist,
                         getattr(obj, "parentTitle", "") if kind == "track" else "",
                         art_url(item_id(self.id, kind, ref)) if has_art else None,
                         (getattr(obj, "duration", None) or 0) / 1000 or None, getattr(obj, "index", None),
                         year=getattr(obj, "year", None) if kind == "album" else None)

    def _items(self, objs) -> list[dict]:
        return [i for o in objs if (i := self._item(o))]

    def browse(self, kind: str, query: str = "", offset: int = 0, limit: int = 100) -> list[dict]:
        try:
            if kind == "playlist":
                lists = self._server().playlists(playlistType="audio")
                if query:
                    lists = [p for p in lists if query.lower() in p.title.lower()]
                return self._items(sorted(lists, key=lambda p: p.title.lower())[offset:offset + limit])
            out = []
            for section in self.sections():
                kwargs = {"libtype": kind, "sort": "titleSort", "container_start": offset, "maxresults": limit}
                if query:
                    kwargs["title__icontains"] = query
                out += self._items(section.search(**kwargs))
            return out
        except MusicSourceError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MusicSourceError(f"Plex: {e}") from None

    def _fetch(self, ref: str):
        try:
            return self._server().fetchItem(int(ref))
        except MusicSourceError:
            raise
        except Exception:  # noqa: BLE001
            raise MusicSourceError("Plex doesn't have that any more") from None

    def open(self, kind: str, ref: str) -> dict:
        obj = self._fetch(ref)
        item = self._item(obj) or make_item(self.id, kind, ref, getattr(obj, "title", ref))
        if kind == "artist":
            return {"item": item, "albums": self._items(obj.albums())}
        if kind == "album":
            return {"item": item, "tracks": self._items(obj.tracks())}
        if kind == "playlist":
            return {"item": item, "tracks": self._items(obj.items())}
        return {"item": item, "tracks": [item]}

    def track(self, ref: str) -> dict:
        return self.open("track", ref)["item"]

    def stream(self, track: dict) -> tuple[str, str]:
        obj = self._fetch(split_id(track["id"])[2])
        try:
            part = obj.media[0].parts[0]
        except (AttributeError, IndexError):
            raise MusicSourceError(f"Plex has no file for {track['name']}") from None
        ext = os.path.splitext(getattr(part, "file", "") or "")[1].lower() or f".{getattr(part, 'container', 'mp3')}"
        return self._server().url(part.key, includeToken=True), AUDIO_TYPES.get(ext, "audio/mpeg")

    def lyrics(self, track: dict) -> dict | None:
        """Plex's lyrics: a sidecar .lrc or .txt file, or Plex Pass's, as a lyrics stream."""
        from .lyrics import parse
        obj = self._fetch(split_id(track["id"])[2])
        server = self._server()
        for media in getattr(obj, "media", None) or []:
            for part in getattr(media, "parts", None) or []:
                for stream in getattr(part, "streams", None) or []:
                    if getattr(stream, "streamType", None) != 4 or not getattr(stream, "key", None):
                        continue
                    r = server._session.get(server.url(stream.key, includeToken=True), timeout=10)
                    if r.ok and (found := parse(r.text)):
                        return found
        return None

    def art(self, kind: str, ref: str) -> tuple[bytes, str] | None:
        obj = self._fetch(ref)
        thumb = getattr(obj, "thumb", None) or getattr(obj, "composite", None) or getattr(obj, "parentThumb", None)
        if not thumb:
            return None
        server = self._server()
        url = server.url(f"/photo/:/transcode?{urlencode({'url': thumb, 'width': 300, 'height': 300})}",
                         includeToken=True)
        r = server._session.get(url, timeout=15)
        return (r.content, r.headers.get("Content-Type", "image/jpeg")) if r.ok else None


# ---- Jellyfin -----------------------------------------------------------------------

class JellyfinMusicSource(MusicSource):
    """The music on a Jellyfin server added as a movie source."""
    kind = "jellyfin"
    TYPES = {"artist": "MusicArtist", "album": "MusicAlbum", "playlist": "Playlist", "track": "Audio"}

    def __init__(self, source):
        super().__init__(f"jf-{source.id}", source.name)
        self.source = source  # sources.JellyfinSource
        self.client = source.client

    def _get(self, path: str, **params):
        from .jellyfin import JellyfinError
        try:
            return self.client.get(path, **params) or {}
        except JellyfinError as e:
            raise MusicSourceError(f"{self.name}: {e}") from None

    @property
    def _items_path(self) -> str:
        return f"/Users/{self.client.user_id}/Items" if self.client.user_id else "/Items"

    def _item(self, raw: dict) -> dict | None:
        kind = {v: k for k, v in self.TYPES.items()}.get(raw.get("Type"))
        if not kind or (kind == "playlist" and raw.get("MediaType") not in (None, "Audio")):
            return None
        artist = raw.get("AlbumArtist") or ", ".join(raw.get("Artists") or [])
        has_art = bool((raw.get("ImageTags") or {}).get("Primary") or raw.get("AlbumPrimaryImageTag"))
        ticks = raw.get("RunTimeTicks")
        return make_item(self.id, kind, raw["Id"], raw.get("Name"), "" if kind == "artist" else artist,
                         raw.get("Album", "") if kind == "track" else "",
                         art_url(item_id(self.id, kind, raw["Id"])) if has_art else None,
                         ticks / 10_000_000 if ticks else None, raw.get("IndexNumber"),
                         year=raw.get("ProductionYear") if kind == "album" else None)

    def _list(self, **params) -> list[dict]:
        found = self._get(self._items_path, Recursive="true", Fields="PrimaryImageAspectRatio", **params)
        return [i for raw in found.get("Items") or [] if (i := self._item(raw))]

    def browse(self, kind: str, query: str = "", offset: int = 0, limit: int = 100) -> list[dict]:
        if kind not in self.TYPES:
            return []
        params = {"IncludeItemTypes": self.TYPES[kind], "SortBy": "SortName", "StartIndex": offset, "Limit": limit}
        if kind == "playlist":
            params["MediaTypes"] = "Audio"
        if query:
            params["SearchTerm"] = query
        if kind == "artist":
            # Album artists only: Jellyfin also lists everyone named in any metadata
            params.pop("IncludeItemTypes")
            if self.client.user_id:
                params["UserId"] = self.client.user_id
            found = self._get("/Artists/AlbumArtists", Fields="PrimaryImageAspectRatio", **params)
            return [i for raw in found.get("Items") or [] if (i := self._item(raw))]
        return self._list(**params)

    def open(self, kind: str, ref: str) -> dict:
        raw = self._get(f"{self._items_path}/{ref}") if self.client.user_id else \
            ((self._get("/Items", Ids=ref).get("Items") or [None])[0] or {})
        item = self._item(raw) if raw.get("Type") else make_item(self.id, kind, ref, ref)
        if kind == "artist":
            return {"item": item, "albums": self._list(IncludeItemTypes="MusicAlbum", AlbumArtistIds=ref,
                                                       SortBy="ProductionYear,SortName")}
        if kind == "album":
            return {"item": item, "tracks": self._list(ParentId=ref, IncludeItemTypes="Audio",
                                                       SortBy="ParentIndexNumber,IndexNumber,SortName")}
        if kind == "playlist":
            found = self._get(f"/Playlists/{ref}/Items", **({"UserId": self.client.user_id} if self.client.user_id else {}))
            return {"item": item, "tracks": [i for r in found.get("Items") or [] if (i := self._item(r))]}
        return {"item": item, "tracks": [item]}

    def track(self, ref: str) -> dict:
        return self.open("track", ref)["item"]

    def stream(self, track: dict) -> tuple[str, str]:
        # Jellyfin turns anything into MP3, which every kind of speaker plays
        ref = split_id(track["id"])[2]
        query = urlencode({"static": "false", "audioCodec": "mp3", "audioBitRate": 320000, "api_key": self.client.api_key})
        return f"{self.client.url}/Audio/{quote(ref)}/stream.mp3?{query}", "audio/mpeg"

    def lyrics(self, track: dict) -> dict | None:
        """Jellyfin's lyrics (10.9 and later): lines with their start in ticks, if timed."""
        from .lyrics import from_lines
        try:
            found = self._get(f"/Audio/{quote(split_id(track['id'])[2])}/Lyrics")
        except MusicSourceError:
            return None
        lines = [(l["Start"] / 10_000_000 if isinstance(l.get("Start"), (int, float)) else None,
                  str(l.get("Text") or "")) for l in found.get("Lyrics") or [] if isinstance(l, dict)]
        return from_lines(lines)

    def art(self, kind: str, ref: str) -> tuple[bytes, str] | None:
        from .jellyfin import JellyfinError
        try:
            return self.client.image(ref, 300, 300)
        except JellyfinError:
            return None


# ---- folders ------------------------------------------------------------------------

def _tag(tags, *names) -> str:
    for name in names:
        value = tags.get(name) if tags else None
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            return str(value).strip()
    return ""


def _number(value: str) -> int | None:
    head = str(value or "").split("/")[0].strip()
    return int(head) if head.isdigit() else None


def read_tags(path: str) -> dict:
    """Title, artist, album, track number and length from a file's tags (mutagen), else
    from its folders: ``Artist/Album/01 Title.mp3``."""
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    parent = os.path.basename(folder)
    grand = os.path.basename(os.path.dirname(folder))
    number = None
    head, _, rest = stem.partition(" ")
    if head.rstrip(".-").isdigit() and rest:
        number, stem = int(head.rstrip(".-")), rest.lstrip("-. ")
    info = {"title": stem, "artist": grand, "album_artist": "", "album": parent, "number": number,
            "disc": None, "duration": None, "year": None}
    try:
        import mutagen
        audio = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 - not every file has tags mutagen reads
        audio = None
    if audio is not None:
        tags = audio.tags or {}
        info.update({k: v for k, v in {
            "title": _tag(tags, "title"), "artist": _tag(tags, "artist"), "album_artist": _tag(tags, "albumartist"),
            "album": _tag(tags, "album"), "number": _number(_tag(tags, "tracknumber")),
            "disc": _number(_tag(tags, "discnumber")), "year": _number(_tag(tags, "date", "year")[:4]),
        }.items() if v})
        length = getattr(getattr(audio, "info", None), "length", None)
        info["duration"] = int(length) if length else None
    return info


def embedded_art(path: str) -> tuple[bytes, str] | None:
    """The picture inside an audio file (ID3, MP4, FLAC or Vorbis), if any."""
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:  # noqa: BLE001
        return None
    if audio is None:
        return None
    for pic in getattr(audio, "pictures", None) or []:  # FLAC
        return pic.data, pic.mime or "image/jpeg"
    tags = audio.tags or {}
    try:
        for key in tags.keys():
            if str(key).startswith("APIC"):
                return tags[key].data, tags[key].mime or "image/jpeg"
        if "covr" in tags and tags["covr"]:
            cover = tags["covr"][0]
            return bytes(cover), "image/png" if getattr(cover, "imageformat", 13) == 14 else "image/jpeg"
        if "metadata_block_picture" in tags:
            import base64
            from mutagen.flac import Picture
            pic = Picture(base64.b64decode(tags["metadata_block_picture"][0]))
            return pic.data, pic.mime or "image/jpeg"
    except Exception:  # noqa: BLE001
        return None
    return None


def folder_cover(folder: str) -> str | None:
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in sorted(names):
        stem, ext = os.path.splitext(name.lower())
        if ext in (".jpg", ".jpeg", ".png") and stem in COVER_NAMES:
            return os.path.join(folder, name)
    return None


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


class FolderMusicSource(MusicSource):
    """Audio files in a folder on this machine or a mounted network share. Albums come
    from the files' tags (or their folders), playlists from .m3u files."""
    kind = "folder"
    RESCAN = 600

    def __init__(self, settings: dict, files):
        self.settings = settings
        self.path = os.path.abspath(os.path.expanduser(settings["path"]))
        super().__init__(settings["id"], settings.get("name") or os.path.basename(self.path.rstrip("/")) or self.path)
        self.files = files  # filestream.FileStreams, for signed addresses
        self._lock = threading.Lock()
        self._index: dict | None = None
        self._scanned = 0.0
        self.scanning = False

    def public(self) -> dict:
        index = self._index or {}
        return {**super().public(), "path": self.path, "tracks": len(index.get("tracks", {})),
                "scanning": self.scanning}

    # ---- the index ----

    def index(self, fresh: bool = False) -> dict:
        with self._lock:
            if self._index is not None and not fresh and time.monotonic() - self._scanned < self.RESCAN:
                return self._index
        return self.scan()

    def scan(self) -> dict:
        if not os.path.isdir(self.path):
            raise MusicSourceError(f"{self.path} isn't a folder this machine can open")
        self.scanning = True
        try:
            tracks: dict[str, dict] = {}
            playlists: dict[str, dict] = {}
            for folder, dirs, names in os.walk(self.path):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(names):
                    if name.startswith("."):
                        continue
                    path = os.path.join(folder, name)
                    ext = os.path.splitext(name)[1].lower()
                    if ext in AUDIO_TYPES:
                        tracks[short_hash(path)] = {"path": path, **read_tags(path)}
                    elif ext in PLAYLIST_TYPES:
                        playlists[short_hash(path)] = {"path": path, "name": os.path.splitext(name)[0]}
            albums: dict[str, dict] = {}
            artists: dict[str, dict] = {}
            for ref, t in tracks.items():
                album_artist = t["album_artist"] or t["artist"] or "Unknown artist"
                album_ref = short_hash(f"{album_artist.lower()}\n{(t['album'] or '').lower()}")
                t["album_ref"] = album_ref
                album = albums.setdefault(album_ref, {"name": t["album"] or "Unknown album", "artist": album_artist,
                                                      "tracks": [], "year": t["year"]})
                album["tracks"].append(ref)
                artist_ref = short_hash(album_artist.lower())
                artists.setdefault(artist_ref, {"name": album_artist, "albums": []})
                if album_ref not in artists[artist_ref]["albums"]:
                    artists[artist_ref]["albums"].append(album_ref)
            for album in albums.values():
                album["tracks"].sort(key=lambda r: (tracks[r]["disc"] or 0, tracks[r]["number"] or 0,
                                                    tracks[r]["title"].lower()))
            by_path = {t["path"]: ref for ref, t in tracks.items()}
            for p in playlists.values():
                p["tracks"] = [ref for entry in self._read_m3u(p["path"]) if (ref := by_path.get(entry))]
            index = {"tracks": tracks, "albums": albums, "artists": artists, "playlists": playlists}
            with self._lock:
                self._index, self._scanned = index, time.monotonic()
            return index
        finally:
            self.scanning = False

    @staticmethod
    def _read_m3u(path: str) -> list[str]:
        folder = os.path.dirname(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = [line.strip() for line in f]
        except OSError:
            return []
        return [os.path.normpath(line if os.path.isabs(line) else os.path.join(folder, line))
                for line in lines if line and not line.startswith("#")]

    # ---- items ----

    def _track_item(self, ref: str, t: dict) -> dict:
        return make_item(self.id, "track", ref, t["title"], t["artist"] or t["album_artist"], t["album"],
                         art_url(item_id(self.id, "album", t["album_ref"])), t["duration"], t["number"])

    def _album_item(self, ref: str, a: dict) -> dict:
        return make_item(self.id, "album", ref, a["name"], a["artist"], image=art_url(item_id(self.id, "album", ref)),
                         year=a.get("year"))

    def _artist_item(self, ref: str, a: dict) -> dict:
        first = a["albums"][0] if a["albums"] else None
        return make_item(self.id, "artist", ref, a["name"],
                         image=art_url(item_id(self.id, "album", first)) if first else None)

    def _playlist_item(self, ref: str, p: dict) -> dict:
        return make_item(self.id, "playlist", ref, p["name"], count=len(p["tracks"]))

    def browse(self, kind: str, query: str = "", offset: int = 0, limit: int = 100) -> list[dict]:
        index = self.index()
        q = query.lower()
        if kind == "artist":
            items = [self._artist_item(r, a) for r, a in index["artists"].items() if q in a["name"].lower()]
        elif kind == "album":
            items = [self._album_item(r, a) for r, a in index["albums"].items()
                     if q in a["name"].lower() or q in a["artist"].lower()]
        elif kind == "playlist":
            items = [self._playlist_item(r, p) for r, p in index["playlists"].items() if q in p["name"].lower()]
        elif kind == "track":
            items = [self._track_item(r, t) for r, t in index["tracks"].items() if q in t["title"].lower()]
        else:
            return []
        items.sort(key=lambda i: i["name"].lower())
        return items[offset:offset + limit]

    def open(self, kind: str, ref: str) -> dict:
        index = self.index()
        if kind == "artist" and ref in index["artists"]:
            a = index["artists"][ref]
            albums = sorted((self._album_item(r, index["albums"][r]) for r in a["albums"]),
                            key=lambda i: (i.get("year") or 0, i["name"].lower()))
            return {"item": self._artist_item(ref, a), "albums": albums}
        if kind == "album" and ref in index["albums"]:
            a = index["albums"][ref]
            return {"item": self._album_item(ref, a), "tracks": [self._track_item(r, index["tracks"][r]) for r in a["tracks"]]}
        if kind == "playlist" and ref in index["playlists"]:
            p = index["playlists"][ref]
            return {"item": self._playlist_item(ref, p), "tracks": [self._track_item(r, index["tracks"][r]) for r in p["tracks"]]}
        if kind == "track" and ref in index["tracks"]:
            item = self._track_item(ref, index["tracks"][ref])
            return {"item": item, "tracks": [item]}
        raise MusicSourceError(f"{self.name} doesn't have that any more")

    def track(self, ref: str) -> dict:
        return self.open("track", ref)["item"]

    def file(self, ref: str) -> str | None:
        t = self.index()["tracks"].get(ref)
        return t["path"] if t else None

    def stream(self, track: dict) -> tuple[str, str]:
        ref = split_id(track["id"])[2]
        path = self.file(ref)
        if not path:
            raise MusicSourceError(f"{track['name']} isn't in {self.name} any more")
        ext = os.path.splitext(path)[1].lower()
        key = f"music-{self.id}-{ref}"
        return (f"{self.files_base}/music/file/{self.id}/{ref}/{self.files.token(key)}/track{ext}",
                AUDIO_TYPES.get(ext, "application/octet-stream"))

    files_base = ""  # Marquee's public address, set by the library

    def lyrics(self, track: dict) -> dict | None:
        """A .lrc (or .txt) file beside the track, with the same name."""
        from .lyrics import parse
        path = self.file(split_id(track["id"])[2])
        if not path:
            return None
        base = os.path.splitext(path)[0]
        for ext in (".lrc", ".LRC", ".txt"):
            try:
                with open(base + ext, encoding="utf-8", errors="replace") as f:
                    if found := parse(f.read()):
                        return found
            except OSError:
                continue
        return None

    def art(self, kind: str, ref: str) -> tuple[bytes, str] | None:
        index = self.index()
        album = index["albums"].get(ref) if kind == "album" else None
        if kind == "track" and ref in index["tracks"]:
            album = index["albums"].get(index["tracks"][ref]["album_ref"])
        if not album:
            return None
        first = index["tracks"][album["tracks"][0]]["path"] if album["tracks"] else None
        if first and (cover := folder_cover(os.path.dirname(first))):
            try:
                with open(cover, "rb") as f:
                    return f.read(), "image/png" if cover.lower().endswith(".png") else "image/jpeg"
            except OSError:
                pass
        for r in album["tracks"][:3]:
            if art := embedded_art(index["tracks"][r]["path"]):
                return art
        return None


# ---- all of them ----------------------------------------------------------------------

class MusicLibrary:
    """Every music source: Music Assistant (when connected directly), Plex (when it has
    music libraries), each Jellyfin source, and the music folders added in the panel."""

    def __init__(self, plex, music, data_dir: str, files, base_url: str):
        self.plex = plex
        self.music = music
        self.files = files
        self.base_url = base_url
        self.path = os.path.join(data_dir, "music_library.json")
        self._lock = threading.Lock()
        try:
            with open(self.path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        self.folders: list[FolderMusicSource] = []
        for settings in saved.get("folders", []):
            try:
                self.folders.append(self._folder(settings))
            except (KeyError, TypeError):
                continue
        self.plex_music = PlexMusicSource(plex)
        self.assistant = AssistantSource(music)
        # Audiobooks, for the Books section
        from .audiobookshelf import Audiobookshelf, AudiobookshelfSource
        self.audiobookshelf = Audiobookshelf(os.path.join(data_dir, "audiobookshelf.json"),
                                             os.environ.get("AUDIOBOOKSHELF_URL", ""),
                                             os.environ.get("AUDIOBOOKSHELF_TOKEN", ""))
        self.books = AudiobookshelfSource(self.audiobookshelf)
        self._plex_has_music: tuple[float, bool] | None = None

    def _folder(self, settings: dict) -> FolderMusicSource:
        source = FolderMusicSource(settings, self.files)
        source.files_base = self.base_url
        return source

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"folders": [s.settings for s in self.folders]}, f, indent=1)
        os.replace(tmp, self.path)

    def _plex_ok(self) -> bool:
        cached = self._plex_has_music
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        ok = False
        if self.plex.config.plex_token or self.plex.config.plex_url:
            try:
                ok = bool(self.plex_music.sections())
            except MusicSourceError:
                ok = False
        self._plex_has_music = (time.monotonic(), ok)
        return ok

    def sources(self, section: str | None = None) -> list[MusicSource]:
        """Where music (``section="music"``) or audiobooks (``"books"``) come from; both
        if ``section`` is None."""
        books = [self.books] if self.audiobookshelf.configured else []
        if section == "books":
            return books
        music = self._music_sources()
        return music if section == "music" else music + books

    def _music_sources(self) -> list[MusicSource]:
        out: list[MusicSource] = []
        if self.music._direct() is not None or self.music.bridge.status()["music_assistant"]:
            out.append(self.assistant)
        if self._plex_ok():
            out.append(self.plex_music)
        library = getattr(self.plex, "library", None)
        for s in getattr(library, "sources", []) or []:
            if getattr(s, "kind", "") == "jellyfin":
                out.append(JellyfinMusicSource(s))
        return out + list(self.folders)

    def source(self, source_id: str) -> MusicSource:
        if source_id == "ma":
            return self.assistant
        if source_id == "plex":
            return self.plex_music
        if source_id == "abs":
            if not self.audiobookshelf.configured:
                raise MusicSourceError("Connect Audiobookshelf under Books › Sources")
            return self.books
        for s in self.sources():
            if s.id == source_id:
                return s
        raise MusicSourceError("That music source isn't there any more")

    def public(self, section: str | None = "music") -> list[dict]:
        out = [{"section": "music", **s.public()} for s in self.sources(section)]
        for s in out:
            if s["id"] == "ma" and self.music._direct() is None:
                s["search_only"] = True  # through Home Assistant
        return out

    # ---- folders (the panel) ----

    def add_folder(self, path: str, name: str = "") -> FolderMusicSource:
        path = os.path.abspath(os.path.expanduser(path.strip()))
        if not path or not os.path.isdir(path):
            raise MusicSourceError("That isn't a folder this machine can open")
        if any(f.path == path for f in self.folders):
            raise MusicSourceError("That folder is added already")
        settings = {"id": f"mf-{short_hash(path)[:8]}", "path": path}
        if name.strip():
            settings["name"] = name.strip()
        source = self._folder(settings)
        with self._lock:
            self.folders.append(source)
            self._save()
        threading.Thread(target=self._scan_quietly, args=(source,), daemon=True).start()
        return source

    @staticmethod
    def _scan_quietly(source: FolderMusicSource) -> None:
        try:
            source.scan()
        except MusicSourceError as e:
            log.warning("Couldn't read music in %s: %s", source.path, e)

    def remove_folder(self, source_id: str) -> bool:
        with self._lock:
            kept = [f for f in self.folders if f.id != source_id]
            if len(kept) == len(self.folders):
                return False
            self.folders = kept
            self._save()
        return True

    def rescan(self, source_id: str) -> None:
        source = next((f for f in self.folders if f.id == source_id), None)
        if source is None:
            raise MusicSourceError("That folder isn't there any more")
        threading.Thread(target=self._scan_quietly, args=(source,), daemon=True).start()

    # ---- items ----

    def browse(self, source_id: str, kind: str, query: str = "", offset: int = 0, limit: int = 100) -> list[dict]:
        return self.source(source_id).browse(kind, query, offset, limit)

    def search(self, query: str, source_id: str | None = None, section: str = "music") -> list[dict]:
        sources = [self.source(source_id)] if source_id else self.sources(section)
        out = []
        for s in sources:
            try:
                out += s.search(query)
            except MusicSourceError as e:
                log.info("Search in %s failed: %s", s.name, e)
        return out

    def open(self, ident: str) -> dict:
        source, kind, ref = split_id(ident)
        return self.source(source).open(kind, ref)

    def tracks(self, ident: str) -> list[dict]:
        source, kind, ref = split_id(ident)
        return self.source(source).tracks(kind, ref)

    def item(self, ident: str) -> dict:
        return self.open(ident)["item"]

    def stream(self, track: dict) -> tuple[str, str]:
        return self.source(track["source"]).stream(track)

    def art(self, ident: str) -> tuple[bytes, str] | None:
        source, kind, ref = split_id(ident)
        return self.source(source).art(kind, ref)

    # ---- addresses speakers fetch (served by server.py, signed) ----

    def signed(self, what: str, ident: str) -> str:
        """``/music/<what>/<token>?id=...``: a track's picture or its MP3, for speakers."""
        return f"{self.base_url}/music/{what}/{self.files.token(f'{what}|{ident}')}?{urlencode({'id': ident})}"

    def check(self, what: str, ident: str, token: str) -> bool:
        return self.files.check(f"{what}|{ident}", token)

    def transcoded(self, track: dict) -> str:
        """An address that plays the track as MP3 (made by ffmpeg), for speakers that can't
        play its own format."""
        return self.signed("mp3", track["id"])

    def source_for_ffmpeg(self, ident: str) -> str:
        """What ffmpeg reads a track from: its file, or its address."""
        source, kind, ref = split_id(ident)
        s = self.source(source)
        if isinstance(s, FolderMusicSource) and (path := s.file(ref)):
            return path
        return s.stream(s.track(ref))[0]

    def folder_file(self, source_id: str, ref: str, token: str) -> str | None:
        """The file behind a signed ``/music/file`` address, or None."""
        source = next((f for f in self.folders if f.id == source_id), None)
        if source is None or not self.files.check(f"music-{source_id}-{ref}", token):
            return None
        return source.file(ref)

    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""
