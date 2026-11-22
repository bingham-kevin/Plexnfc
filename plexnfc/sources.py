"""Where movies come from: Plex, Jellyfin servers, and folders of movie files.

Plex is set up as before (Sign in with Plex, PLEX_LIBRARY). Jellyfin servers and
folders (a local path, or an SMB/NFS share mounted on the server) are added in the
control panel and saved in ``sources.json``, next to the Plex sign-in.

Every movie has a key, which tags (``/m/KEY``) and cards (movie_cards.json) are
linked to, so keys stay the same as long as the movie is there, and never clash
between kinds of source:

  Plex      ``tmdb-603``, ``imdb-tt0133093``, ``tvdb-…``, else ``rk-<ratingKey>`` (unchanged)
  Jellyfin  ``jf-tmdb-603`` from the movie's TMDB/IMDb/TVDB ID, else ``jf-<item ID>``
  Folder    ``file-tmdb-603`` from a movie.nfo or ``{tmdb-603}`` in the name, else
            ``file-<hash>`` of the file's path inside the folder (so moving or renaming
            the file, but not the folder, changes it)

The same movie in two Jellyfin servers (or two folders) has the same key: whichever
source is listed first plays it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .filestream import FileStreams, embedded_poster, placeholder_poster
from .jellyfin import PROVIDERS, TICKS_PER_SECOND, Jellyfin, JellyfinError, tag_key

log = logging.getLogger(__name__)

CACHE_TTL = 300  # a source's movie list is read again after this long
MISS_RESCAN = 30  # a key that isn't found rescans its source, but not more often than this

JF_KEY_RE = re.compile(r"^jf-(?:(tmdb|imdb|tvdb)-[A-Za-z0-9]+|[0-9a-fA-F]{32})$")
FILE_KEY_RE = re.compile(r"^file-(?:(tmdb|imdb|tvdb)-[A-Za-z0-9]+|[0-9a-f]{12})$")

VIDEO_TYPES = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm",
               ".flv", ".vob", ".divx"}
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".webp")
# Folders and files that are a movie's extras, not movies
EXTRA_DIRS = {"extras", "featurettes", "behind the scenes", "deleted scenes", "interviews", "scenes", "shorts",
              "trailers", "samples", "sample", "other", "@eadir", "#recycle", "$recycle.bin"}
EXTRA_RE = re.compile(r"[-._ ](trailer|sample|featurette|behindthescenes|deleted|interview|scene|short|other)$", re.I)
LATER_PART_RE = re.compile(r"[-._ ]+(cd|dis[ck]|part|pt)[-._ ]?([2-9]|1\d)$", re.I)
ID_TAG_RE = re.compile(r"[\[{]\s*(tmdb|imdb|tvdb)(?:id)?[-=]\s*([A-Za-z0-9]+)\s*[\]}]", re.I)
YEAR_RE = re.compile(r"[(\[]?\b((?:19|20)\d\d)\b[)\]]?")
FIRST_PART_RE = re.compile(r"[-._ ]+(cd|dis[ck]|part|pt)[-._ ]?1$", re.I)


class SourceError(Exception):
    """A source couldn't be added or read; the message says why."""


@dataclass(eq=False)
class SourceMovie:
    """A movie from a Jellyfin server or a folder. It has the attributes of a Plex movie the
    rest of Marquee Server uses (title, year, summary, duration and viewOffset in ms...)."""
    key: str
    title: str
    year: int | None
    source: "Source"
    ref: str  # the Jellyfin item ID, or the file's path
    summary: str = ""
    duration: int = 0
    viewOffset: int = 0
    collections: tuple = ()
    labels: tuple = ()
    poster: str | None = None  # a folder movie's poster file
    added: float = 0
    thumb: str = "poster"  # always something to show: its poster, or a placeholder
    ratingKey = None
    guids: tuple = field(default=())


# ---- the sources ---------------------------------------------------------------------

class Source:
    kind = ""
    prefix = ""

    def __init__(self, settings: dict):
        self.settings = settings
        self.id = settings["id"]
        self.name = settings.get("name") or self.default_name()
        self._lock = threading.Lock()
        self._movies: list[SourceMovie] | None = None
        self._by_key: dict[str, SourceMovie] = {}
        self._read_at = 0.0
        self._miss_at = 0.0
        self.error: str | None = None

    def default_name(self) -> str:
        return self.kind.title()

    def scan(self) -> list[SourceMovie]:
        raise NotImplementedError

    def movies(self, fresh: bool = False) -> list[SourceMovie]:
        with self._lock:
            if fresh or self._movies is None or time.monotonic() - self._read_at > CACHE_TTL:
                try:
                    movies = self.scan()
                except SourceError as e:
                    self.error = str(e)
                    if self._movies is None:
                        raise
                    log.warning("Couldn't read %s again, using the last list: %s", self.name, e)
                else:
                    self.error = None
                    movies.sort(key=lambda m: (sort_title(m.title), m.year or 0))
                    by_key: dict[str, SourceMovie] = {}
                    for m in movies:
                        by_key.setdefault(m.key, m)
                    self._movies, self._by_key = movies, by_key
                self._read_at = time.monotonic()
            return list(self._movies)

    def find(self, key: str) -> SourceMovie | None:
        self.movies()
        if key not in self._by_key and time.monotonic() - self._miss_at > MISS_RESCAN:
            self._miss_at = time.monotonic()
            self.movies(fresh=True)  # added since the list was read?
        return self._by_key.get(key)

    def collections(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in self.movies():
            for c in m.collections:
                counts[c] = counts.get(c, 0) + 1
        return counts

    def in_collection(self, movie: SourceMovie, name: str) -> bool:
        return name.casefold() in (c.casefold() for c in movie.collections)

    def poster(self, movie: SourceMovie, width: int | None, height: int | None) -> tuple[bytes, str] | None:
        return None

    def stream(self, movie: SourceMovie, start: int, player: str, library: "Library") -> str:
        raise NotImplementedError

    def public(self) -> dict:
        """What the control panel shows (never a token)."""
        return {"id": self.id, "kind": self.kind, "name": self.name, "error": self.error,
                "count": len(self._movies) if self._movies is not None else None}


class JellyfinSource(Source):
    """A Jellyfin server's movies, reached with an API key or a user's sign-in."""
    kind = "jellyfin"
    prefix = "jf"

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.url = settings["url"].rstrip("/")
        self.client = Jellyfin(self.url, settings["token"], settings.get("user_id"))
        self.library = settings.get("library") or ""
        self._boxsets: tuple[float, dict[str, list[str]]] | None = None

    def default_name(self) -> str:
        return "Jellyfin"

    @property
    def _items(self) -> str:
        return f"/Users/{self.client.user_id}/Items" if self.client.user_id else "/Items"

    def _get(self, path: str, **params):
        try:
            return self.client.get(path, **params) or {}
        except JellyfinError as e:
            raise SourceError(f"{self.name}: {e}") from None

    def _library_id(self) -> str | None:
        """The ID of the library chosen by name, or None for every movie library."""
        if not self.library:
            return None
        found = []
        for path in (["/Library/MediaFolders"] + ([f"/Users/{self.client.user_id}/Views"] if self.client.user_id else [])):
            try:
                found += (self.client.get(path) or {}).get("Items", [])
            except JellyfinError:
                continue
        for item in found:
            if (item.get("Name") or "").casefold() == self.library.casefold():
                return item["Id"]
        raise SourceError(f"{self.name} has no library named {self.library}")

    def scan(self) -> list[SourceMovie]:
        params = {"Recursive": "true", "IncludeItemTypes": "Movie", "SortBy": "SortName", "EnableUserData": "true",
                  "Fields": "ProviderIds,Overview,Tags,ProductionYear,RunTimeTicks,DateCreated"}
        if parent := self._library_id():
            params["ParentId"] = parent
        movies = []
        for item in self._get(self._items, **params).get("Items", []):
            ticks = (item.get("UserData") or {}).get("PlaybackPositionTicks") or 0
            movies.append(SourceMovie(
                key=f"jf-{tag_key(item) or item['Id']}", title=item.get("Name") or "?",
                year=item.get("ProductionYear"), source=self, ref=item["Id"], summary=item.get("Overview") or "",
                duration=int((item.get("RunTimeTicks") or 0) / TICKS_PER_SECOND * 1000),
                viewOffset=int(ticks / TICKS_PER_SECOND * 1000), labels=tuple(item.get("Tags") or ()),
                added=_iso_time(item.get("DateCreated"))))
        return movies

    def boxsets(self) -> dict[str, list[str]]:
        """Jellyfin's collections (box sets): name -> the item IDs in it. Read only when wanted,
        and kept as long as the movie list, since it takes a request per collection."""
        if self._boxsets and time.monotonic() - self._boxsets[0] < CACHE_TTL:
            return self._boxsets[1]
        sets: dict[str, list[str]] = {}
        for boxset in self._get(self._items, Recursive="true", IncludeItemTypes="BoxSet").get("Items", []):
            items = self._get(self._items, ParentId=boxset["Id"], IncludeItemTypes="Movie").get("Items", [])
            if items:
                sets.setdefault(boxset.get("Name") or "?", []).extend(i["Id"] for i in items)
        self._boxsets = (time.monotonic(), sets)
        return sets

    def collections(self) -> dict[str, int]:
        return {name: len(ids) for name, ids in self.boxsets().items()}

    def in_collection(self, movie: SourceMovie, name: str) -> bool:
        return any(n.casefold() == name.casefold() and movie.ref in ids for n, ids in self.boxsets().items())

    def poster(self, movie: SourceMovie, width: int | None, height: int | None) -> tuple[bytes, str] | None:
        try:
            return self.client.image(movie.ref, width, height)
        except JellyfinError as e:
            log.debug("No poster for %s from %s: %s", movie.title, self.name, e)
            return None

    def stream(self, movie: SourceMovie, start: int, player: str, library: "Library") -> str:
        from .tvapp import jellyfin_hls_url
        return jellyfin_hls_url(self.url, self.settings["token"], movie.ref, start, player)

    def public(self) -> dict:
        return {**super().public(), "url": self.url, "library": self.library,
                "user": self.settings.get("user") or None}


class FolderSource(Source):
    """Movie files in a folder (and the folders inside it): a local path, or a network share
    mounted on the server (on a Mac, under /Volumes; in Home Assistant, under /media or /share)."""
    kind = "folder"
    prefix = "file"

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.path = os.path.abspath(os.path.expanduser(settings["path"]))

    def default_name(self) -> str:
        return os.path.basename(os.path.normpath(self.settings["path"])) or self.settings["path"]

    def scan(self) -> list[SourceMovie]:
        if not os.path.isdir(self.path):
            raise SourceError(f"Can't open the folder {self.path}. Is it there, and the share mounted?")
        movies = []
        for root, dirs, files in os.walk(self.path, onerror=lambda e: log.debug("Skipping %s", e)):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.casefold() not in EXTRA_DIRS)
            videos = sorted(f for f in files if os.path.splitext(f)[1].lower() in VIDEO_TYPES
                            and not f.startswith(".") and not EXTRA_RE.search(os.path.splitext(f)[0])
                            and not LATER_PART_RE.search(os.path.splitext(f)[0]))
            for name in videos:
                try:
                    movies.append(self._movie(root, name, files, alone=len(videos) == 1))
                except OSError as e:
                    log.debug("Skipping %s: %s", name, e)
        return movies

    def _movie(self, folder: str, name: str, files: list[str], alone: bool) -> SourceMovie:
        path = os.path.join(folder, name)
        stem = os.path.splitext(name)[0]
        # A movie alone in its own folder is often named by the folder ("Alien (1979)/alien-1080p.mkv")
        names = [stem] + ([os.path.basename(folder)] if alone and folder != self.path else [])
        info = read_nfo(folder, stem, files, alone)
        title, year, ids = parse_name(names)
        ids = {**ids, **info.get("ids", {})}
        source_id = next(((s, ids[s]) for s in PROVIDERS if ids.get(s)), None)
        if source_id:
            key = f"file-{source_id[0]}-{source_id[1]}"
        else:
            relative = os.path.relpath(path, self.path).replace(os.sep, "/")
            key = f"file-{hashlib.sha1(relative.encode()).hexdigest()[:12]}"
        lower = {f.lower(): f for f in files}
        candidates = [f"{stem}-poster", stem] + (["poster", "folder", "cover", "movie"] if alone else [])
        poster = next((os.path.join(folder, lower[c.lower() + ext]) for c in candidates for ext in IMAGE_TYPES
                       if c.lower() + ext in lower), None)
        return SourceMovie(key=key, title=info.get("title") or title, year=info.get("year") or year, source=self,
                           ref=path, summary=info.get("plot", ""), duration=info.get("runtime", 0),
                           collections=tuple(info.get("sets", ())), labels=tuple(info.get("tags", ())),
                           poster=poster, added=os.path.getmtime(path))

    def poster(self, movie: SourceMovie, width: int | None, height: int | None) -> tuple[bytes, str] | None:
        if movie.poster:
            try:
                with open(movie.poster, "rb") as f:
                    ext = os.path.splitext(movie.poster)[1].lower().lstrip(".")
                    return f.read(), f"image/{'jpeg' if ext == 'jpg' else ext}"
            except OSError as e:
                log.debug("Couldn't read %s: %s", movie.poster, e)
        return None

    def stream(self, movie: SourceMovie, start: int, player: str, library: "Library") -> str:
        return library.files.url(library.base_url, movie.key, movie.ref, start, player)

    def public(self) -> dict:
        return {**super().public(), "path": self.path}


KINDS = {"jellyfin": JellyfinSource, "folder": FolderSource}


# ---- reading names and .nfo files -------------------------------------------------------

def sort_title(title: str) -> str:
    title = title.casefold()
    for article in ("the ", "a ", "an "):
        if title.startswith(article):
            return title[len(article):]
    return title


def parse_name(names: list[str]) -> tuple[str, int | None, dict]:
    """A title, year and any TMDB/IMDb/TVDB IDs from file or folder names like
    ``Alien (1979) {tmdb-348}`` or ``Alien.1979.1080p.BluRay``: the first name with a year."""
    ids: dict[str, str] = {}
    parsed = []
    for name in names:
        for source, ident in ID_TAG_RE.findall(name):
            ids.setdefault(source.lower(), ident)
        name = FIRST_PART_RE.sub("", ID_TAG_RE.sub(" ", name).strip())
        # The last year with a title before it: "2001 A Space Odyssey (1968)", "Heat.1995.1080p"
        years = [m for m in YEAR_RE.finditer(name) if name[:m.start()].strip(" ._-([")]
        if years:
            parsed.append((clean_title(name[:years[-1].start()]), int(years[-1].group(1))))
        else:
            parsed.append((clean_title(name), None))
    title, year = next((p for p in parsed if p[1]), parsed[0])
    return title or names[0], year, ids


def clean_title(title: str) -> str:
    title = title.strip(" ._-([")
    if " " not in title:  # Alien.Covenant or Alien_Covenant
        title = re.sub(r"[._]", " ", title)
    return re.sub(r"\s{2,}", " ", title).strip()


def read_nfo(folder: str, stem: str, files: list[str], alone: bool) -> dict:
    """A Kodi/Jellyfin-style .nfo beside the movie (``<stem>.nfo``, or ``movie.nfo`` in the
    movie's own folder): title, year, plot, runtime, collection (``<set>``), tags and IDs."""
    lower = {f.lower(): f for f in files}
    name = lower.get(f"{stem.lower()}.nfo") or (lower.get("movie.nfo") if alone else None)
    if not name:
        return {}
    try:
        root = ET.parse(os.path.join(folder, name)).getroot()
    except (ET.ParseError, OSError) as e:
        log.debug("Couldn't read %s: %s", name, e)
        return {}
    if root.tag != "movie":
        return {}

    def text(tag: str) -> str:
        return (root.findtext(tag) or "").strip()

    info: dict = {"title": text("title"), "plot": text("plot") or text("outline"),
                  "tags": [t.text.strip() for t in root.findall("tag") if t.text and t.text.strip()]}
    year = text("year") or text("premiered")[:4]
    if year.isdigit():
        info["year"] = int(year)
    if text("runtime").isdigit():
        info["runtime"] = int(text("runtime")) * 60_000
    sets = []
    for s in root.findall("set"):
        set_name = (s.findtext("name") or s.text or "").strip()
        if set_name:
            sets.append(set_name)
    info["sets"] = sets
    ids = {}
    for u in root.findall("uniqueid"):
        kind = (u.get("type") or "").lower()
        if kind in PROVIDERS and u.text and u.text.strip():
            ids[kind] = u.text.strip()
    for kind in PROVIDERS:  # older .nfo files: <tmdbid>, <imdbid>, or an IMDb ID in <id>
        if kind not in ids and text(f"{kind}id"):
            ids[kind] = text(f"{kind}id")
    if "imdb" not in ids and text("id").startswith("tt"):
        ids["imdb"] = text("id")
    info["ids"] = {k: v for k, v in ids.items() if re.fullmatch(r"[A-Za-z0-9]+", v)}
    return info


def _iso_time(value: str | None) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")[:26]).timestamp()
    except ValueError:
        return 0


# ---- the whole library -----------------------------------------------------------------

class Library:
    """Plex and the sources added in the panel, as one list of movies."""

    def __init__(self, plex, data_dir: str):
        self.plex = plex
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "sources.json")
        self.files = FileStreams(data_dir)
        self._lock = threading.Lock()
        try:
            with open(self.path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        self.use_plex: bool = saved.get("plex", True)
        self.sources: list[Source] = []
        for settings in saved.get("sources", []):
            try:
                self.sources.append(KINDS[settings["kind"]](settings))
            except (KeyError, TypeError) as e:
                log.warning("Skipping a source in %s that can't be read: %s", self.path, e)

    @property
    def base_url(self) -> str:
        return self.plex.config.public_base_url

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"plex": self.use_plex, "sources": [s.settings for s in self.sources]}, f, indent=1)
        os.replace(tmp, self.path)

    # ---- settings (the control panel) ----

    @property
    def plex_in_use(self) -> bool:
        return self.use_plex and bool(self.plex.config.plex_token)

    def public(self) -> dict:
        return {"plex": {"enabled": self.use_plex, "signed_in": bool(self.plex.config.plex_token),
                         "library": self.plex.config.library},
                "sources": [s.public() for s in self.sources]}

    def add(self, kind: str, **settings) -> Source:
        """Add a Jellyfin server or a folder, once its movies can be read."""
        if kind not in KINDS:
            raise SourceError("Choose Jellyfin or a folder")
        settings = {k: v.strip() if isinstance(v, str) else v for k, v in settings.items() if v not in (None, "")}
        settings.update(id=uuid.uuid4().hex[:8], kind=kind)
        if kind == "folder" and not settings.get("path"):
            raise SourceError("Type the folder's path")
        if kind == "jellyfin":
            url = settings.get("url", "")
            if not re.match(r"^https?://[^/\s]+", url):
                raise SourceError("Type the Jellyfin server's address, e.g. http://192.168.1.20:8096")
            if not settings.get("token"):
                raise SourceError("Give an API key, or sign in")
        source = KINDS[kind](settings)
        source.movies(fresh=True)  # raises SourceError if it can't be read
        with self._lock:
            self.sources.append(source)
            self._save()
        return source

    def remove(self, source_id: str) -> bool:
        with self._lock:
            before = len(self.sources)
            self.sources = [s for s in self.sources if s.id != source_id]
            if len(self.sources) == before:
                return False
            self._save()
        return True

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def set_plex(self, enabled: bool) -> None:
        with self._lock:
            self.use_plex = bool(enabled)
            self._save()

    def jellyfin_client(self) -> Jellyfin | None:
        """The first Jellyfin source's server, for Jellyfin players when JELLYFIN_URL isn't set."""
        return next((s.client for s in self.sources if isinstance(s, JellyfinSource)), None)

    # ---- movies ----

    def source_for_key(self, key: str) -> type[Source] | None:
        """The kind of source a key belongs to (None: Plex)."""
        if key.startswith("jf-"):
            return JellyfinSource
        if key.startswith("file-"):
            return FolderSource
        return None

    def find(self, key: str) -> SourceMovie | None:
        """A Jellyfin or folder movie by its key; None if no source has it."""
        kind = self.source_for_key(key)
        if kind is JellyfinSource and not JF_KEY_RE.match(key) or kind is FolderSource and not FILE_KEY_RE.match(key):
            return None
        for source in self.sources:
            if isinstance(source, kind):
                try:
                    if movie := source.find(key):
                        return movie
                except SourceError as e:
                    log.warning("%s", e)
        return None

    def movies(self, collection: str | None = None, label: str | None = None, source: str | None = None) -> list:
        """Every movie (Plex's, then each source's), or those in a collection or with a label,
        or from one source (``plex`` or a source's ID). A source that can't be read is left
        out, unless every one fails."""
        found: list = []
        errors: list[str] = []
        tried = 0
        if source in (None, "", "plex") and (self.plex_in_use or (self.use_plex and not self.sources)):
            tried += 1
            try:
                found += list(self.plex.plex_movies(collection, label))
            except Exception as e:  # noqa: BLE001 - Plex signed out or unreachable: still list the rest
                if not self.sources or source == "plex":
                    raise
                errors.append(str(e))
                log.warning("Couldn't list Plex movies: %s", e)
        for s in self.sources:
            if source not in (None, "", s.id):
                continue
            tried += 1
            try:
                movies = s.movies()
                if collection:
                    movies = [m for m in movies if s.in_collection(m, collection)]
                if label:
                    movies = [m for m in movies if label.casefold() in (x.casefold() for x in m.labels)]
                found += movies
            except SourceError as e:
                errors.append(str(e))
                log.warning("%s", e)
        if errors and len(errors) == tried:
            from .plex import PlexNFCError
            raise PlexNFCError("; ".join(errors))
        return found

    def filters(self) -> dict:
        """Collections (with how many movies each holds) and labels from every source, and
        the sources themselves."""
        collections: dict[str, int | None] = {}
        labels: set[str] = set()
        if self.plex_in_use:
            plex = self.plex.plex_filters()
            for c in plex["collections"]:
                collections[c["title"]] = c["count"]
            labels.update(plex["labels"])
        for s in self.sources:
            try:
                for name, count in s.collections().items():
                    collections[name] = (collections.get(name) or 0) + count if name in collections else count
                for m in s.movies():
                    labels.update(m.labels)
            except SourceError as e:
                log.warning("%s", e)
        sources = ([{"id": "plex", "name": "Plex"}] if self.plex_in_use else []) + \
                  [{"id": s.id, "name": s.name} for s in self.sources]
        return {"collections": [{"title": t, "count": c} for t, c in sorted(collections.items(),
                                                                          key=lambda c: c[0].casefold())],
                "labels": sorted(labels, key=str.casefold),
                "sources": sources if len(sources) > 1 else []}

    def poster(self, movie: SourceMovie, width: int | None = None, height: int | None = None) -> tuple[bytes, str]:
        """A Jellyfin or folder movie's poster: its own, the art inside its file, or a placeholder."""
        if found := movie.source.poster(movie, width, height):
            return found
        if isinstance(movie.source, FolderSource):
            if art := self._embedded(movie):
                return art, "image/jpeg"
        return placeholder_poster(movie.title, movie.year, width or 600, height or 900), "image/jpeg"

    def _embedded(self, movie: SourceMovie) -> bytes | None:
        """The cover art inside a movie file, kept in poster_cache/ (as is finding none)."""
        cache = os.path.join(self.data_dir, "poster_cache")
        stamp = hashlib.sha1(f"{movie.ref}:{movie.added}".encode()).hexdigest()[:16]
        path = os.path.join(cache, f"{stamp}.jpg")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read() or None
        art = embedded_poster(movie.ref)
        os.makedirs(cache, exist_ok=True)
        with open(path, "wb") as f:
            f.write(art or b"")
        return art

    def stream(self, movie: SourceMovie, start: int, player: str) -> str:
        """The address the Marquee app on ``player`` plays this movie from."""
        return movie.source.stream(movie, start, player, self)

    def file_movie(self, key: str, token: str) -> SourceMovie | None:
        """A folder movie for a signed ``/files`` address, or None."""
        if not self.files.check(key, token):
            return None
        movie = self.find(key)
        return movie if movie and isinstance(movie.source, FolderSource) else None
