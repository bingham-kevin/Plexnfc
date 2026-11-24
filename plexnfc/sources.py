"""Where movies come from: Plex, Jellyfin servers, and folders of movie files.

Plex is set up as before (Sign in with Plex, PLEX_LIBRARY). Jellyfin servers and
folders (a local path, or an SMB/NFS share mounted on the server) are added in the
control panel and saved in ``sources.json``, next to the Plex sign-in, with the
order to use them in.

A tag (``/m/KEY``) or a card (movie_cards.json) is linked to a movie, not to a source:
the same movie in Plex, Jellyfin and a folder is one movie, with one key, and plays
from whichever source comes first in the order that has it and can play on the
player (see ``Library``). Its key is, in this order:

  ``tmdb-603`` (or ``imdb-…``, ``tvdb-…``)  its ID, if any source knows it (Plex's keys, unchanged)
  ``rk-<ratingKey>``                       a Plex movie no source has an ID for
  ``t-<title>-<year>``                     otherwise, from its title and year: ``t-aliencovenant-2017``

A movie is found by any key it has ever had in any source: its IDs, its title and
year, ``rk-…``, and (for a while these were the keys) ``jf-…`` and ``file-…``.
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
from .jellyfin import PROVIDERS, TICKS_PER_SECOND, Jellyfin, JellyfinError
from .metadata import MetadataError, MovieEdits, Tmdb, poster_url

log = logging.getLogger(__name__)

CACHE_TTL = 300  # a source's movie list is read again after this long
MISS_RESCAN = 30  # a key that isn't found rescans its source, but not more often than this

INDEX_TTL = 300  # the merged list of every source's movies is rebuilt (in the background) after this long
LIBRARY_KEY_RE = re.compile(r"^(t-[a-z0-9]+(-\d{4})?|jf-[A-Za-z0-9-]+|file-[A-Za-z0-9-]+|px-[A-Za-z0-9-]+)$")

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
    ids: dict = field(default_factory=dict)  # {"tmdb": "603", ...}
    source_key: str = ""  # jf-<item ID> or file-<hash>: this copy in particular
    thumb: str = "poster"  # always something to show: its poster, or a placeholder
    ratingKey = None
    guids: tuple = field(default=())
    # Folder movies: the key its file name gives (still found by it after a match or an edit),
    # what it had before any edit, how it was matched with TMDB, and TMDB's poster
    parsed_key: str = ""
    base: dict | None = None
    matched: str = ""
    poster_path: str | None = None


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
                        for alias in aliases(m):
                            by_key.setdefault(alias, m)
                    self._movies, self._by_key = movies, by_key
                self._read_at = time.monotonic()
            return list(self._movies)

    def reindex(self) -> None:
        """Find movies by their keys again, after one of them was edited."""
        with self._lock:
            by_key: dict[str, SourceMovie] = {}
            for m in self._movies or []:
                for alias in aliases(m):
                    by_key.setdefault(alias, m)
            self._by_key = by_key

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
            ids = {k.lower(): str(v) for k, v in (item.get("ProviderIds") or {}).items()
                   if k.lower() in PROVIDERS and v}
            title = item.get("Name") or "?"
            movies.append(SourceMovie(
                key=movie_id(ids, title, item.get("ProductionYear")), title=title, ids=ids,
                source_key=f"jf-{item['Id']}",
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
        """Jellyfin's HLS stream, passed on by Marquee Server (``/jf/…``): Apple TVs can't always
        reach the address this server reaches Jellyfin at (a Tailscale name, say)."""
        from urllib.parse import urlsplit

        from .tvapp import jellyfin_hls_url
        url = urlsplit(jellyfin_hls_url(self.url, self.settings["token"], movie.ref, start, player))
        return f"{library.base_url}/jf/{self.id}/{library.files.token(f'jf:{self.id}')}{url.path}?{url.query}"

    def public(self) -> dict:
        return {**super().public(), "url": self.url, "library": self.library,
                "user": self.settings.get("user") or None}


class PlexServerSource(Source):
    """Another Plex server's movies (one shared with this Plex account, say), besides the Plex
    server signed in to. Reached at its address with the token Plex gives for it."""
    kind = "plex"
    prefix = "px"

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.url = settings["url"].rstrip("/")
        self.library = settings.get("library") or ""
        self._server = None

    def default_name(self) -> str:
        return self.settings.get("server") or "Plex"

    @property
    def server(self):
        if self._server is None:
            from plexapi.server import PlexServer
            try:
                self._server = PlexServer(self.url, self.settings["token"], timeout=15)
            except Exception as e:  # noqa: BLE001 - plexapi raises several types
                raise SourceError(f"Couldn't reach the Plex server {self.name} at {self.url}: {e}") from None
        return self._server

    def _sections(self) -> list:
        try:
            sections = [s for s in self.server.library.sections() if s.type == "movie"]
        except SourceError:
            raise
        except Exception as e:  # noqa: BLE001
            self._server = None  # connect again next time
            raise SourceError(f"{self.name}: {e}") from None
        if self.library:
            sections = [s for s in sections if s.title.casefold() == self.library.casefold()]
            if not sections:
                raise SourceError(f"{self.name} has no movie library named {self.library}")
        return sections

    def scan(self) -> list[SourceMovie]:
        movies = []
        for section in self._sections():
            try:
                items = section.all()
            except Exception as e:  # noqa: BLE001
                raise SourceError(f"{self.name}: {e}") from None
            for m in items:
                ids = plex_ids(m)
                title = m.title or "?"
                year = getattr(m, "year", None)
                movies.append(SourceMovie(
                    key=movie_id(ids, title, year), title=title, year=year, source=self, ids=ids,
                    source_key=f"px-{self.id}-{m.ratingKey}", ref=str(m.ratingKey),
                    summary=getattr(m, "summary", "") or "", duration=getattr(m, "duration", 0) or 0,
                    viewOffset=getattr(m, "viewOffset", 0) or 0,
                    collections=tuple(c.tag for c in getattr(m, "collections", None) or ()),
                    labels=tuple(label.tag for label in getattr(m, "labels", None) or ()),
                    poster=getattr(m, "thumb", None),
                    added=m.addedAt.timestamp() if getattr(m, "addedAt", None) else 0))
        return movies

    def poster(self, movie: SourceMovie, width: int | None, height: int | None) -> tuple[bytes, str] | None:
        import requests

        if not movie.poster:
            return None
        try:
            url = self.server.transcodeImage(movie.poster, height=height or 900, width=width or 600)
            r = requests.get(url, timeout=15)
        except Exception as e:  # noqa: BLE001
            log.debug("No poster for %s from %s: %s", movie.title, self.name, e)
            return None
        if r.ok and isinstance(r.content, bytes) and r.content:
            return r.content, r.headers.get("Content-Type", "image/jpeg")
        return None

    def stream(self, movie: SourceMovie, start: int, player: str, library: "Library") -> str:
        """Plex's HLS stream, as for the Plex server signed in to: the Apple TV plays it from the server."""
        from .tvapp import plex_hls_url
        url = plex_hls_url(self.url, self.settings["token"], movie.ref, start, player)
        library.plex._start_plex_session(url)
        return url

    def public(self) -> dict:
        return {**super().public(), "url": self.url, "library": self.library,
                "server": self.settings.get("server") or None, "machine": self.settings.get("machine") or None}


def plex_account_servers(token: str) -> list:
    """The Plex servers a Plex account can reach (its own and ones shared with it), as plex.tv's resources."""
    from plexapi.myplex import MyPlexAccount
    try:
        account = MyPlexAccount(token=token)
        return [r for r in account.resources() if "server" in (r.provides or "")]
    except Exception as e:  # noqa: BLE001 - plexapi raises several types here
        raise SourceError(f"Couldn't ask plex.tv for your Plex servers: {e}") from None


def connect_plex_server(token: str, machine: str) -> dict:
    """The settings for one of the account's Plex servers (by its machine ID): the address it
    answers at (its home-network address first, which the Apple TV streams from too) and its token."""
    resource = next((r for r in plex_account_servers(token) if r.clientIdentifier == machine), None)
    if resource is None:
        raise SourceError("Your Plex account can't reach that server any more")
    try:
        server = resource.connect(ssl=False, timeout=5, locations=["local"])
    except Exception:  # noqa: BLE001
        try:
            server = resource.connect(timeout=5)
        except Exception as e:  # noqa: BLE001
            raise SourceError(f"Couldn't connect to the Plex server {resource.name}: {e}") from None
    return {"url": server._baseurl.rstrip("/"), "token": resource.accessToken or token, "machine": machine,
            "server": resource.name}


class FolderSource(Source):
    """Movie files in a folder (and the folders inside it): a local path, or a network share
    mounted on the server (on a Mac, under /Volumes; in Home Assistant, under /media or /share)."""
    kind = "folder"
    prefix = "file"

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.path = os.path.abspath(os.path.expanduser(settings["path"]))
        self.edits: MovieEdits | None = None  # the panel's changes to its movies (set by Library)

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
        relative = os.path.relpath(path, self.path).replace(os.sep, "/")
        title, year = info.get("title") or title, info.get("year") or year
        lower = {f.lower(): f for f in files}
        candidates = [f"{stem}-poster", stem] + (["poster", "folder", "cover", "movie"] if alone else [])
        poster = next((os.path.join(folder, lower[c.lower() + ext]) for c in candidates for ext in IMAGE_TYPES
                       if c.lower() + ext in lower), None)
        movie = SourceMovie(key=movie_id(ids, title, year), title=title, year=year, source=self, ids=ids,
                            source_key=f"file-{hashlib.sha1(relative.encode()).hexdigest()[:12]}",
                            ref=path, summary=info.get("plot", ""), duration=info.get("runtime", 0),
                            collections=tuple(info.get("sets", ())), labels=tuple(info.get("tags", ())),
                            poster=poster, added=os.path.getmtime(path), parsed_key=title_key(title, year))
        movie.base = {"title": title, "year": year, "summary": movie.summary, "duration": movie.duration,
                      "ids": dict(ids), "collections": list(movie.collections), "labels": list(movie.labels)}
        apply_edit(movie, self.edits.get(movie.source_key) if self.edits else None)
        return movie

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
        return library.files.url(library.base_url, movie.source_key, movie.ref, start, player)

    def public(self) -> dict:
        return {**super().public(), "path": self.path}


KINDS = {"jellyfin": JellyfinSource, "folder": FolderSource, "plex": PlexServerSource}


def apply_edit(movie: SourceMovie, edit: dict | None) -> None:
    """Give a folder movie what its file and .nfo say (``base``), then the panel's changes."""
    base = movie.base or {}
    edit = edit or {}
    movie.title = edit.get("title") or base.get("title") or movie.title
    movie.year = edit["year"] if "year" in edit else base.get("year")
    movie.summary = edit["summary"] if "summary" in edit else base.get("summary", "")
    movie.duration = edit.get("duration") or base.get("duration", 0)
    movie.ids = dict(edit["ids"] if "ids" in edit else base.get("ids", {}))
    movie.collections = tuple(edit["collections"] if "collections" in edit else base.get("collections", ()))
    movie.labels = tuple(edit["labels"] if "labels" in edit else base.get("labels", ()))
    movie.poster_path = edit.get("poster_path")
    movie.matched = edit.get("matched", "")
    movie.key = movie_id(movie.ids, movie.title, movie.year)


# ---- choosing a folder in the control panel ----------------------------------------------

NETWORK_FS = {"smbfs", "nfs", "nfs4", "cifs", "afpfs", "webdav", "smb3", "fuse.sshfs"}


def mounts() -> dict[str, str]:
    """Mounted file systems: mount point -> type (``smbfs``, ``nfs``...)."""
    found: dict[str, str] = {}
    try:
        with open("/proc/mounts") as f:  # Linux (Home Assistant)
            for line in f:
                parts = line.split()
                if len(parts) > 2:
                    found[parts[1].replace("\\040", " ")] = parts[2]
        return found
    except OSError:
        pass
    import subprocess
    try:  # macOS: "//user@host/share on /Volumes/share (smbfs, nodev, ...)"
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return found
    for line in out.splitlines():
        if m := re.match(r"^.+? on (.+) \(([^,)]+)", line):
            found[m.group(1)] = m.group(2)
    return found


def browse_roots() -> list[dict]:
    """Places to start looking for a movie folder: mounted shares and disks, and home."""
    types = mounts()
    roots = []
    for base in ("/Volumes", "/media", "/share", "/mnt"):
        if not os.path.isdir(base):
            continue
        if base == "/Volumes":
            for name in sorted(os.listdir(base)):
                path = os.path.join(base, name)
                if name.startswith(".") or name == "Recovery" or os.path.islink(path) or not os.path.isdir(path):
                    continue  # the startup disk is a link to /
                roots.append({"name": name, "path": path, "network": types.get(path) in NETWORK_FS})
        else:
            roots.append({"name": base, "path": base, "network": types.get(base) in NETWORK_FS})
    home = os.path.expanduser("~")
    if os.path.isdir(home) and home != "/root":
        roots.append({"name": "Home", "path": home, "network": False})
    return roots


def browse_folder(path: str | None) -> dict:
    """A folder's folders, for choosing a folder source: its path, its parent, the folders in
    it, and how many movie files are right in it."""
    path = os.path.abspath(os.path.expanduser(path)) if path else ""
    result = {"path": path, "parent": None, "dirs": [], "videos": 0, "roots": browse_roots()}
    if not path:
        return result
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        raise SourceError(f"Can't open {path}: {e.strerror or e}") from e
    dirs = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name.startswith("@") or entry.name.startswith("#"):
            continue
        try:
            if entry.is_dir():
                dirs.append(entry.name)
            elif os.path.splitext(entry.name)[1].lower() in VIDEO_TYPES:
                result["videos"] += 1
        except OSError:
            continue
    result["dirs"] = sorted(dirs, key=str.casefold)
    parent = os.path.dirname(path)
    result["parent"] = parent if parent != path else None
    return result


# ---- keys ------------------------------------------------------------------------------

def id_keys(ids: dict) -> list[str]:
    """``tmdb-603``, ``imdb-tt0133093``…: the keys a movie's IDs give, best first."""
    return [f"{s}-{ids[s]}" for s in PROVIDERS if ids.get(s) and re.fullmatch(r"[A-Za-z0-9]+", str(ids[s]))]


def title_key(title: str, year: int | None) -> str:
    """``t-aliencovenant-2017``: a key from the title (letters and digits only, so "Alien:
    Covenant" and "Alien Covenant" agree) and the year."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    plain = re.sub(r"[^a-z0-9]", "", plain.replace("&", "and")) or "untitled"
    return f"t-{plain}-{year}" if year else f"t-{plain}"


def movie_id(ids: dict, title: str, year: int | None) -> str:
    keys = id_keys(ids)
    return keys[0] if keys else title_key(title, year)


def plex_ids(movie) -> dict:
    ids = {}
    for guid in getattr(movie, "guids", None) or []:
        source, _, ident = str(getattr(guid, "id", "")).partition("://")
        if source in PROVIDERS and ident:
            ids.setdefault(source, ident)
    return ids


def aliases(movie) -> list[str]:
    """Every key a movie can be found by: IDs, its own key in its source, its title key, and
    the ``jf-…``/``file-…`` forms."""
    if isinstance(movie, SourceMovie):
        found = id_keys(movie.ids)
        prefix = movie.source_key.split("-", 1)[0]
        found += [f"{prefix}-{k}" for k in found] + [movie.source_key]
        if movie.key not in found:
            found.append(movie.key)
        if movie.parsed_key and movie.parsed_key != title_key(movie.title, movie.year):
            found.append(movie.parsed_key)  # its key before a match or an edit renamed it
    else:  # Plex
        found = id_keys(plex_ids(movie)) + [f"rk-{movie.ratingKey}"]
    return found + [title_key(movie.title, getattr(movie, "year", None))]


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
    title = title.strip(" ._-").rstrip("([").strip()
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

@dataclass(eq=False)
class LibraryMovie:
    """One movie, from every source that has it: ``copies`` in the order sources are used."""
    key: str
    copies: list

    @property
    def movie(self):
        return self.copies[0]

    @property
    def title(self) -> str:
        return self.movie.title

    @property
    def year(self) -> int | None:
        return getattr(self.movie, "year", None)


def source_name(movie) -> str:
    return movie.source.name if isinstance(movie, SourceMovie) else "Plex"


def source_id(movie) -> str:
    return movie.source.id if isinstance(movie, SourceMovie) else "plex"


class Library:
    """Plex and the sources added in the panel, as one list of movies.

    The same movie in several sources is one ``LibraryMovie``: copies are matched by
    TMDB/IMDb/TVDB ID, else by title and year (unless their IDs disagree). Its copies
    are kept in the order the sources are used in (``order``, set on the Sources tab;
    Plex first unless moved), and playing picks the first copy the player can play.
    """

    def __init__(self, plex, data_dir: str):
        self.plex = plex
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "sources.json")
        self.files = FileStreams(data_dir)
        self._lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._index: tuple[float, list[LibraryMovie], dict[str, LibraryMovie]] | None = None
        self._rebuilding = False
        self._miss_at = 0.0
        self._plex_cache: tuple[float, list] | None = None
        self.playing: dict[str, dict] = {}  # player ID -> {"key", "source"}: what each player was last sent
        self.edits = MovieEdits(data_dir)
        self.match_jobs: dict[str, dict] = {}  # folder source ID -> its "Match all" progress
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
        self.order: list[str] = [i for i in saved.get("order", []) if isinstance(i, str)]
        self.tmdb_key: str = saved.get("tmdb_key") or ""
        for s in self.sources:
            if isinstance(s, FolderSource):
                s.edits = self.edits

    @property
    def base_url(self) -> str:
        return self.plex.config.public_base_url

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"plex": self.use_plex, "order": self.ordered_ids(), "tmdb_key": self.tmdb_key,
                       "sources": [s.settings for s in self.sources]}, f, indent=1)
        os.replace(tmp, self.path)

    def changed(self, keep_plex: bool = False) -> None:
        """Sources, their order or the Plex sign-in changed: build the merged list again."""
        self._index = None
        if not keep_plex:
            self._plex_cache = None

    # ---- settings (the control panel) ----

    @property
    def plex_in_use(self) -> bool:
        return self.use_plex and bool(self.plex.config.plex_token)

    def ordered_ids(self) -> list[str]:
        """Every source's ID (``plex`` for Plex), in the order they're used."""
        known = ["plex"] + [s.id for s in self.sources]
        return [i for i in self.order if i in known] + [i for i in known if i not in self.order]

    def ordered(self) -> list:
        """``"plex"`` and the Source objects, in the order they're used."""
        by_id = {s.id: s for s in self.sources}
        return ["plex" if i == "plex" else by_id[i] for i in self.ordered_ids()]

    def set_order(self, ids: list[str]) -> None:
        with self._lock:
            self.order = [i for i in ids if isinstance(i, str)]
            self._save()
        self.changed()

    def public(self) -> dict:
        plex = {"id": "plex", "kind": "plex", "enabled": self.use_plex, "signed_in": bool(self.plex.config.plex_token),
                "library": self.plex.config.library}
        by_id = {s.id: s for s in self.sources}
        return {"plex": plex, "sources": [s.public() for s in self.sources],
                "order": self.ordered_ids(), "tmdb": bool(self.tmdb_key),
                "matching": {i: j for i, j in self.match_jobs.items() if i in by_id},
                "ordered": [plex if i == "plex" else by_id[i].public() for i in self.ordered_ids()]}

    def add(self, kind: str, **settings) -> Source:
        """Add a Jellyfin server or a folder, once its movies can be read."""
        if kind not in KINDS:
            raise SourceError("Choose a Plex server, Jellyfin or a folder")
        settings = {k: v.strip() if isinstance(v, str) else v for k, v in settings.items() if v not in (None, "")}
        settings.update(id=uuid.uuid4().hex[:8], kind=kind)
        if kind == "plex":
            if not re.match(r"^https?://[^/\s]+", settings.get("url", "")):
                raise SourceError("Type the Plex server's address, e.g. http://192.168.1.20:32400")
            if not settings.get("token"):
                raise SourceError("Give the Plex server's token, or choose it from your Plex account")
            if settings.get("machine") and any(s.settings.get("machine") == settings["machine"] for s in self.sources):
                raise SourceError(f"{settings.get('server') or 'That Plex server'} is already a source")
        if kind == "folder" and not settings.get("path"):
            raise SourceError("Type the folder's path")
        if kind == "jellyfin":
            url = settings.get("url", "")
            if not re.match(r"^https?://[^/\s]+", url):
                raise SourceError("Type the Jellyfin server's address, e.g. http://192.168.1.20:8096")
            if not settings.get("token"):
                raise SourceError("Give an API key, or sign in")
        source = KINDS[kind](settings)
        if isinstance(source, FolderSource):
            source.edits = self.edits
        source.movies(fresh=True)  # raises SourceError if it can't be read
        with self._lock:
            self.sources.append(source)
            self._save()
        self.changed()
        return source

    def remove(self, source_id: str) -> bool:
        with self._lock:
            before = len(self.sources)
            self.sources = [s for s in self.sources if s.id != source_id]
            if len(self.sources) == before:
                return False
            self._save()
        self.changed()
        return True

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def set_plex(self, enabled: bool) -> None:
        with self._lock:
            self.use_plex = bool(enabled)
            self._save()
        self.changed()

    # ---- matching folder movies with TMDB, and editing them ----

    def set_tmdb_key(self, key: str) -> None:
        """Keep a TMDB API key, once TMDB accepts it (a blank one forgets it)."""
        key = (key or "").strip()
        if key:
            try:
                Tmdb(key).search("Alien", 1979)
            except MetadataError as e:
                raise SourceError(str(e)) from e
        with self._lock:
            self.tmdb_key = key
            self._save()

    def tmdb(self) -> Tmdb:
        if not self.tmdb_key:
            raise SourceError("Add a TMDB API key on the Sources tab first")
        return Tmdb(self.tmdb_key)

    def folder_copy(self, source_key: str) -> SourceMovie | None:
        """A folder movie by its ``file-…`` key."""
        for s in self.sources:
            if isinstance(s, FolderSource):
                try:
                    if (m := s.find(source_key)) and m.source_key == source_key:
                        return m
                except SourceError:
                    continue
        return None

    def edit_movie(self, copy: SourceMovie, edit: dict | None) -> SourceMovie:
        """Save a folder movie's changes (None: back to what its file says) and use them now."""
        if edit is None:
            self.edits.remove(copy.source_key)
        else:
            edit = self.edits.set(copy.source_key, edit)
        apply_edit(copy, edit)
        copy.source.reindex()
        self.changed(keep_plex=True)
        return copy

    def match_movie(self, copy: SourceMovie, tmdb_id: int | str, how: str = "manual") -> SourceMovie:
        """Match a folder movie with a TMDB movie: its title, year, summary, IDs, poster and
        collection (added to any its .nfo gives). Its labels stay."""
        try:
            details = self.tmdb().details(tmdb_id)
        except MetadataError as e:
            raise SourceError(str(e)) from e
        return self.edit_movie(copy, self._match_edit(copy, details, how))

    @staticmethod
    def _match_edit(copy: SourceMovie, details: dict, how: str) -> dict:
        base = copy.base or {}
        collections = list(dict.fromkeys(list(base.get("collections", [])) + details.pop("collections", [])))
        return {**details, "collections": collections, "matched": how}

    def search_tmdb(self, query: str, year: int | None = None) -> list[dict]:
        try:
            return self.tmdb().search(query, year)
        except MetadataError as e:
            raise SourceError(str(e)) from e

    def start_match_all(self, source_id: str) -> dict:
        """Match a folder's movies that have no IDs and no match yet, in the background. Only
        sure matches are taken (the same title, and a year within one); the rest are left."""
        source = self.source(source_id)
        if not isinstance(source, FolderSource):
            raise SourceError("Only folder movies can be matched")
        tmdb = self.tmdb()
        job = self.match_jobs.get(source_id)
        if job and job["running"]:
            return job
        todo = [m for m in source.movies() if not m.matched and not m.ids]
        job = {"running": True, "done": 0, "total": len(todo), "matched": 0, "error": None}
        self.match_jobs[source_id] = job
        threading.Thread(target=self._match_all, args=(source, tmdb, todo, job), daemon=True).start()
        return job

    def _match_all(self, source: FolderSource, tmdb: Tmdb, todo: list[SourceMovie], job: dict) -> None:
        try:
            for m in todo:
                try:
                    if found := tmdb.best_match(m.title, m.year):
                        edit = self._match_edit(m, tmdb.details(found["id"]), "auto")
                        apply_edit(m, self.edits.set(m.source_key, edit))
                        job["matched"] += 1
                except MetadataError as e:
                    if "API key" in str(e):
                        raise
                    log.info("Couldn't match %s: %s", m.title, e)
                job["done"] += 1
        except MetadataError as e:
            job["error"] = str(e)
        finally:
            job["running"] = False
            source.reindex()
            self.changed(keep_plex=True)
            log.info("Matched %d of %d movies in %s with TMDB", job["matched"], job["total"], source.name)

    def tmdb_poster(self, poster_path: str, width: int | None = None) -> bytes | None:
        """A TMDB poster, kept in poster_cache/."""
        import requests

        url = poster_url(poster_path, width)
        cache = os.path.join(self.data_dir, "poster_cache")
        path = os.path.join(cache, f"tmdb-{hashlib.sha1(url.encode()).hexdigest()[:16]}.jpg")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read() or None
        try:
            r = requests.get(url, timeout=15)
        except requests.RequestException as e:
            log.debug("No TMDB poster %s: %s", url, e)
            return None
        if not (r.ok and isinstance(r.content, bytes) and r.content):
            return None
        os.makedirs(cache, exist_ok=True)
        with open(path, "wb") as f:
            f.write(r.content)
        return r.content

    def _folder_poster(self, copy: SourceMovie, width: int | None, height: int | None) -> tuple[bytes, str] | None:
        """A poster file beside the movie, else TMDB's; TMDB's first if it was matched by hand."""
        tmdb = (lambda: (art, "image/jpeg") if copy.poster_path and (art := self.tmdb_poster(copy.poster_path, width))
                else None)
        if copy.matched == "manual" and (found := tmdb()):
            return found
        return copy.source.poster(copy, width, height) or tmdb()

    # ---- the merged list ----

    def _plex_movies(self, fresh: bool = False) -> list:
        if fresh or not self._plex_cache or time.monotonic() - self._plex_cache[0] > CACHE_TTL:
            self._plex_cache = (time.monotonic(), list(self.plex.plex_movies()))
        return self._plex_cache[1]

    def _build(self, fresh: bool = False) -> tuple[float, list[LibraryMovie], dict[str, LibraryMovie]]:
        """Merge every source's movies, in the order sources are used."""
        movies: list[LibraryMovie] = []
        owner: dict[str, LibraryMovie] = {}
        for source in self.ordered():
            try:
                if source == "plex":
                    copies = self._plex_movies(fresh) if self.plex_in_use else []
                else:
                    copies = source.movies(fresh=fresh)
            except Exception as e:  # noqa: BLE001 - one source being away leaves the others
                log.warning("Couldn't read %s: %s", "Plex" if source == "plex" else source.name, e)
                continue
            for copy in copies:
                keys = aliases(copy)
                ids = keys[:len(id_keys(copy.ids if isinstance(copy, SourceMovie) else plex_ids(copy)))]
                found = next((owner[k] for k in ids if k in owner), None)
                if found is None and keys[-1] in owner and not _ids_disagree(owner[keys[-1]], copy):
                    found = owner[keys[-1]]  # same title and year
                if found is None:
                    found = LibraryMovie("", [])
                    movies.append(found)
                found.copies.append(copy)
                for k in keys:
                    owner.setdefault(k, found)
        for m in movies:
            m.key = _main_key(m)
            owner.setdefault(m.key, m)
            for copy in m.copies:
                if isinstance(copy, SourceMovie):
                    copy.key = m.key
                else:
                    copy._marquee_key = m.key
        movies.sort(key=lambda m: (sort_title(m.title), m.year or 0))
        return time.monotonic(), movies, owner

    def index(self) -> tuple[list[LibraryMovie], dict[str, LibraryMovie]]:
        """The merged list and every key it's found by. A list that's getting old is used
        while a new one is built in the background."""
        current = self._index
        if current is None:
            with self._index_lock:
                if self._index is None:
                    self._index = self._build()
                current = self._index
        elif time.monotonic() - current[0] > INDEX_TTL and not self._rebuilding:
            self._rebuilding = True
            threading.Thread(target=self._rebuild, daemon=True).start()
        return current[1], current[2]

    def _rebuild(self, fresh: bool = False) -> None:
        try:
            built = self._build(fresh)
            with self._index_lock:
                self._index = built
        finally:
            self._rebuilding = False

    def find(self, key: str, title: str | None = None, year: int | None = None) -> LibraryMovie | None:
        """The movie for a key from a tag or a card, from whichever sources have it. A card
        also knows its movie's title (and year), which finds it in sources that know it by
        another key."""
        movies, owner = self.index()
        found = owner.get(key)
        if found is None and time.monotonic() - self._miss_at > MISS_RESCAN:
            self._miss_at = time.monotonic()  # added since the list was read?
            with self._index_lock:
                self._index = self._build(fresh=True)
            movies, owner = self._index[1], self._index[2]
            found = owner.get(key)
        if found is None and title:
            found = owner.get(title_key(title, year))
            if found is None and not year:
                plain = title_key(title, None)
                same = [m for m in movies if title_key(m.title, None) == plain]
                found = same[0] if len(same) == 1 else None
        return found

    def canonical(self, key: str) -> str:
        """The key a movie is listed under, for any key it has (without reading any source)."""
        current = self._index
        found = current[2].get(key) if current else None
        return found.key if found else key

    def movie_for(self, copy) -> LibraryMovie | None:
        """The merged movie one source's copy belongs to."""
        _, owner = self.index()
        return next((owner[k] for k in aliases(copy) if k in owner), None)

    def movies(self, collection: str | None = None, label: str | None = None, source: str | None = None) -> list:
        """Every movie once (as its first copy), or those in a collection or with a label in any
        source, or in one source (``plex`` or a source's ID)."""
        if not self.plex_in_use and not self.sources:
            return list(self.plex.plex_movies(collection, label))  # says why: not signed in
        movies, _ = self.index()
        if not (collection or label or source):
            return [m.movie for m in movies]
        found: list = []
        errors: list[str] = []
        tried = 0
        for s in self.ordered():
            if source and source != ("plex" if s == "plex" else s.id):
                continue
            tried += 1
            try:
                if s == "plex":
                    if not self.plex_in_use:
                        continue
                    copies = (list(self.plex.plex_movies(collection, label)) if collection or label
                              else self._plex_movies())
                else:
                    copies = s.movies()
                    if collection:
                        copies = [m for m in copies if s.in_collection(m, collection)]
                    if label:
                        copies = [m for m in copies if label.casefold() in (x.casefold() for x in m.labels)]
                found += copies
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
                log.warning("%s", e)
        if errors and len(errors) == tried and not found:
            from .plex import PlexNFCError
            raise PlexNFCError("; ".join(errors))
        merged: dict[str, LibraryMovie] = {}
        for copy in found:
            if (m := self.movie_for(copy)) and m.key not in merged:
                merged[m.key] = m
        return [m.movie for m in sorted(merged.values(), key=lambda m: (sort_title(m.title), m.year or 0))]

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
        sources = [{"id": "plex", "name": "Plex"} if s == "plex" else {"id": s.id, "name": s.name}
                   for s in self.ordered() if s != "plex" or self.plex_in_use]
        return {"collections": [{"title": t, "count": c} for t, c in sorted(collections.items(),
                                                                          key=lambda c: c[0].casefold())],
                "labels": sorted(labels, key=str.casefold),
                "sources": sources if len(sources) > 1 else []}

    # ---- posters and streams ----

    def poster(self, movie: LibraryMovie, width: int | None = None, height: int | None = None) -> tuple[bytes, str]:
        """The poster of the first copy that has one; the art inside a file; else a placeholder."""
        import requests

        for copy in movie.copies:
            if isinstance(copy, SourceMovie):
                found = (self._folder_poster(copy, width, height) if isinstance(copy.source, FolderSource)
                         else copy.source.poster(copy, width, height))
                if found:
                    return found
            elif getattr(copy, "thumb", None):
                try:
                    r = requests.get(self.plex.poster_url(copy, width or 600, height or 900), timeout=15)
                    if r.ok and isinstance(r.content, bytes) and r.content:
                        return r.content, r.headers.get("Content-Type", "image/jpeg")
                except Exception as e:  # noqa: BLE001 - Plex away: try the next copy
                    log.debug("No Plex poster for %s: %s", copy.title, e)
        for copy in movie.copies:
            if isinstance(copy, SourceMovie) and isinstance(copy.source, FolderSource):
                if art := self._embedded(copy):
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
        """The address the Marquee app on ``player`` plays this copy from."""
        return movie.source.stream(movie, start, player, self)

    def file_movie(self, key: str, token: str) -> SourceMovie | None:
        """A folder copy for a signed ``/files`` address (made for its ``file-…`` key), or None."""
        if not self.files.check(key, token):
            return None
        for s in self.sources:
            if isinstance(s, FolderSource):
                try:
                    if (m := s.find(key)) and m.source_key == key:
                        return m
                except SourceError:
                    continue
        return None


def _ids_disagree(movie: LibraryMovie, copy) -> bool:
    """Whether a copy and a movie have different IDs from the same provider (a remake with
    the same title and year, say), so they aren't the same movie."""
    mine = copy.ids if isinstance(copy, SourceMovie) else plex_ids(copy)
    for other in movie.copies:
        theirs = other.ids if isinstance(other, SourceMovie) else plex_ids(other)
        if any(mine.get(p) and theirs.get(p) and str(mine[p]) != str(theirs[p]) for p in PROVIDERS):
            return True
    return False


def _main_key(movie: LibraryMovie) -> str:
    """The key a movie is listed (and new tags and cards linked) under: an ID from the first
    copy that has one; else a Plex copy's ``rk-…``; else its title and year."""
    for copy in movie.copies:
        if keys := id_keys(copy.ids if isinstance(copy, SourceMovie) else plex_ids(copy)):
            return keys[0]
    for copy in movie.copies:
        if not isinstance(copy, SourceMovie):
            return f"rk-{copy.ratingKey}"
    return title_key(movie.title, movie.year)
