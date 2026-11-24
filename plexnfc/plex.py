"""Movie lookups, and playback in the Marquee app on Apple TV.

Each poster's NFC tag stores a short, stable movie key such as ``tmdb-603``.
We prefer external IDs (TMDB / IMDb / TVDB) over Plex's internal ratingKey
because ratingKeys change if a movie is re-added or re-matched, which would
silently break a printed page. ``rk-12345`` is accepted as a fallback for
items without an external ID.
"""
from __future__ import annotations

import logging
import os
import re
import time
from types import SimpleNamespace

import requests
from plexapi.exceptions import NotFound
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

from .config import Config
from .appletv import AppleTVError, AppleTVs
from .cards import Cards
from .homeassistant import HomeAssistant, HomeAssistantError
from .sources import LIBRARY_KEY_RE, Library, LibraryMovie, SourceMovie, source_name
from .tvapp import AppInbox, AppPairings, plex_hls_url

log = logging.getLogger(__name__)

GUID_SOURCES = ("tmdb", "imdb", "tvdb")
KEY_RE = re.compile(r"^(tmdb|imdb|tvdb|rk)-([A-Za-z0-9]+)$")


class PlexNFCError(Exception):
    """Something the person tapping a tag should be told about."""


def movie_key(movie) -> str:
    """Return the stable key to write on a tag for this movie."""
    if isinstance(movie, SourceMovie):  # Jellyfin or a folder: made when it was read
        return movie.key
    if isinstance(key := movie.__dict__.get("_marquee_key") if hasattr(movie, "__dict__") else None, str):
        return key  # a Plex movie in the merged library, listed under the key it has in every source
    guids = {g.id.split("://", 1)[0]: g.id.split("://", 1)[1] for g in getattr(movie, "guids", [])}
    for source in GUID_SOURCES:
        if source in guids:
            return f"{source}-{guids[source]}"
    return f"rk-{movie.ratingKey}"


def app_movie(movie, key: str | None = None) -> dict:
    """What the Marquee app shows for a movie in rows, search and the player."""
    key = key or movie_key(movie)
    return {"key": key, "title": movie.title, "year": getattr(movie, "year", None),
            "summary": getattr(movie, "summary", None) or "",
            "duration": int((getattr(movie, "duration", 0) or 0) / 1000),
            "poster": f"/poster/{key}?w=400&h=600"}


class Plex:
    def __init__(self, config: Config, server: PlexServer | None = None,
                 ha: HomeAssistant | None = None, apple_tvs: AppleTVs | None = None):
        self.config = config
        self._server = server
        self._account = None
        self.ha = ha
        if self.ha is None and config.ha_url and config.ha_token:
            self.ha = HomeAssistant(config.ha_url, config.ha_token)
        self.app_inbox = AppInbox()
        # Saved next to the Plex sign-in, so it lasts as long as the app's /data does
        self.app_pairings = AppPairings(os.path.join(os.path.dirname(config.auth_file) or ".",
                                                     "marquee_devices.json"))
        self.apple_tvs = apple_tvs or AppleTVs(os.path.join(os.path.dirname(config.auth_file) or ".",
                                                            "apple_tvs.json"))
        # Cards linked to movies by their ID, for the Movie Remote
        self.cards = Cards(os.path.join(os.path.dirname(config.auth_file) or ".", "movie_cards.json"))
        # Jellyfin servers and folders added in the control panel, alongside Plex
        self.library = Library(self, os.path.dirname(config.auth_file) or ".")

    @property
    def server(self) -> PlexServer:
        if self._server is None:
            if not self.config.plex_token:
                raise PlexNFCError("Not signed in to Plex yet. Open plexnfc's page and choose Sign in with Plex.")
            if self.config.plex_url:
                self._server = PlexServer(self.config.plex_url, self.config.plex_token)
            else:
                self._server = self._account_server()
        return self._server

    def _account_server(self) -> PlexServer:
        """With no PLEX_URL, the Plex server on the signed-in account: its own (or
        PLEX_SERVER, by name), at whichever of its addresses answers."""
        try:
            account = MyPlexAccount(token=self.config.plex_token)
            servers = [r for r in account.resources() if "server" in (r.provides or "")]
        except Exception as e:  # plexapi raises several types here
            raise PlexNFCError(f"Couldn't reach plex.tv to find your Plex server: {e}") from None
        if name := self.config.plex_server:
            servers = [r for r in servers if r.name.casefold() == name.casefold()]
        if not servers:
            raise PlexNFCError(f"Your Plex account has no server named {name}" if name
                               else "Your Plex account has no Plex server")
        resource = sorted(servers, key=lambda r: not r.owned)[0]
        try:
            # Its plain home-network address first: the Apple TV streams from the same address,
            # and some routers won't look up plex.direct names, which point at home addresses.
            # (Docker and remote addresses can take 30s to give up, hence the timeout.)
            server = resource.connect(ssl=False, timeout=5, locations=["local"])
        except Exception:
            try:
                server = resource.connect(timeout=5)
            except Exception as e:
                raise PlexNFCError(f"Couldn't connect to the Plex server {resource.name}: {e}") from None
        log.info("Using Plex server %s at %s", resource.name, server._baseurl)
        self._account = account
        return server

    @property
    def plex_url(self) -> str:
        """The Plex server's address: PLEX_URL, or the one found from the Plex account."""
        return self.config.plex_url or self.server._baseurl.rstrip("/")

    def set_token(self, token: str) -> None:
        """Use a new token (after Sign in with Plex) from the next request on."""
        self.config.plex_token = token
        self._server = None
        self._account = None
        self.library.changed()

    @property
    def section(self):
        return self.server.library.section(self.config.library)

    # ---- lookup -------------------------------------------------------------

    def find_library_movie(self, key: str, title: str | None = None, year: int | None = None) -> LibraryMovie:
        """The movie for a tag's or card's key, with its copy in each source that has it (in the
        order sources are used). A card's title (and year) also finds it by title."""
        if self.library.plex_in_use or self.library.sources:
            if found := self.library.find(key, title, year):
                return found
        if KEY_RE.match(key) and self.library.use_plex and (self.config.plex_token or not self.library.sources):
            return LibraryMovie(key, [self.find_plex_movie(key)])  # outside the library, or says why not
        if not (KEY_RE.match(key) or LIBRARY_KEY_RE.match(key)):
            raise PlexNFCError(f"'{key}' isn't a valid movie key")
        raise PlexNFCError(f"No movie in your sources matches {f'{title} ({key})' if title else key}")

    def find_movie(self, key: str, title: str | None = None, year: int | None = None):
        """The first copy of the movie for a key (see find_library_movie)."""
        return self.find_library_movie(key, title, year).movie

    def find_plex_movie(self, key: str):
        m = KEY_RE.match(key)
        if not m:
            raise PlexNFCError(f"'{key}' isn't a valid movie key")
        source, ident = m.groups()
        try:
            if source == "rk":
                return self.server.fetchItem(int(ident))
            return self.section.getGuid(f"{source}://{ident}")
        except NotFound:
            raise PlexNFCError(f"No movie in '{self.config.library}' matches {key}") from None

    def movies(self, collection: str | None = None, label: str | None = None, source: str | None = None):
        """Every movie from Plex and the other sources, or those in a collection, with a label,
        or from one source."""
        return self.library.movies(collection, label, source)

    def movie_filters(self) -> dict:
        return self.library.filters()

    def plex_movies(self, collection: str | None = None, label: str | None = None):
        if collection:
            return self.section.collection(collection).items()
        if label:
            return self.section.search(label=label, sort="titleSort")
        return self.section.all(sort="titleSort")

    def plex_filters(self) -> dict:
        """The Plex library's collections (with how many movies each holds) and labels, to choose
        movies by."""
        def listed(get):
            try:
                return sorted(get(), key=lambda c: c.title.casefold())
            except Exception as e:  # an empty library or an older Plex: just none to offer
                log.debug("Couldn't list filters: %s", e)
                return []
        return {"collections": [{"title": c.title, "count": getattr(c, "childCount", None)}
                                for c in listed(self.section.collections)],
                "labels": sorted({c.title for c in listed(lambda: self.section.listFilterChoices("label"))},
                                 key=str.casefold)}

    # ---- playback: always in the Marquee app on Apple TV -----------------------

    def default_player(self) -> str:
        """The ID of the player taps go to when nothing else is chosen: ``app:APP_PLAYER``."""
        return f"app:{self.config.app_player}" if self.config.app_player else ""

    def players(self) -> list[dict]:
        """Every player a phone can send its taps to: the paired Marquee Apple TVs.
        ``id`` is what ``?client=`` takes."""
        return [{"id": f"app:{d['name']}", "name": d["name"], "kind": "Marquee"}
                for d in self.app_pairings.devices() if d["kind"] == "tv"]

    def _ready_script(self, client_name: str | None) -> str | None:
        """The get-ready script for this player: its own from PLAYER_SCRIPTS, else HA_SCRIPT,
        which belongs to the default player (so a tap for another room doesn't wake this one)."""
        if not client_name:
            return self.config.ha_script or None
        # Players are Marquee names, written with or without "app:"
        scripts = {k.removeprefix("app:").casefold(): v for k, v in (self.config.player_scripts or {}).items()}
        if script := scripts.get(client_name.removeprefix("app:").casefold()):
            return script
        if client_name.removeprefix("app:").casefold() == self.default_player().removeprefix("app:").casefold():
            return self.config.ha_script or None
        return None

    def prepare_tv(self, client_name: str | None = None) -> str | None:
        """Get the TV ready: open Marquee on its paired Apple TV, or else run the
        Home Assistant "get the TV ready" script for this player, if there is one.

        Returns an error message instead of raising: the TV may already be on,
        so a problem here shouldn't stop the movie from trying.
        """
        if (name := self._app_player(client_name)) and self.apple_tvs.link_for(name):
            try:
                self.apple_tvs.open_marquee(name)
            except AppleTVError as e:
                log.warning("Couldn't open Marquee on %s: %s", name, e)
                return str(e)
            return None
        script = self._ready_script(f"app:{client_name.removeprefix('app:')}" if client_name else None)
        if not (self.ha and script):
            return None
        try:
            self.ha.run_script(script)
        except HomeAssistantError as e:
            log.warning("%s", e)
            return str(e)
        return None

    def play(self, key: str, client_name: str | None = None, resume: bool | None = None):
        found = self.find_library_movie(key)
        resume = self.config.resume if resume is None else resume
        name = self._player(client_name)
        # Leave the movie in the inbox before the app is opened, which picks it up
        movie = self._play_in_app(name, found, resume)
        self.prepare_tv(client_name)
        self._run_started_script()
        return movie, SimpleNamespace(title=name)

    def _run_started_script(self) -> None:
        """Start the HA_STARTED_SCRIPT without waiting, e.g. to press a remote button
        once the movie is going."""
        if not (self.ha and self.config.ha_started_script):
            return
        try:
            self.ha.call_service("script", "turn_on", {"entity_id": self.config.ha_started_script})
        except HomeAssistantError as e:
            log.warning("%s", e)

    def _note_playing(self, player: str | None, key: str, movie) -> None:
        """Remember which source a player was sent a movie from, for Now Playing."""
        if player:
            self.library.playing[player.casefold()] = {"key": key, "source": source_name(movie)}

    def playing_source(self, player: str | None, key: str | None) -> str | None:
        """The source the movie playing on ``player`` came from, if this server started it."""
        noted = self.library.playing.get((player or "").casefold())
        return noted["source"] if noted and key and noted["key"] == key else None

    def _app_player(self, client_name: str | None) -> str | None:
        """The Marquee player to use: ``?client=NAME`` (or ``app:NAME``), else APP_PLAYER."""
        name = client_name.removeprefix("app:") if client_name else self.config.app_player
        return self._marquee_name(name) if name else None

    def _player(self, client_name: str | None) -> str:
        if name := self._app_player(client_name):
            return name
        raise PlexNFCError("No Marquee player chosen: pair Marquee on an Apple TV and choose it as the "
                           "player taps go to, or add ?client=NAME to the tag")

    def _marquee_name(self, name: str) -> str:
        """The paired Marquee's name for ``name``, which may instead be the name of the
        Apple TV paired for it (e.g. "Den Apple TV" for the Marquee named "Den")."""
        for device in self.app_pairings.devices():
            if device["kind"] == "tv" and device["name"].casefold() == name.casefold():
                return device["name"]
        for player, link in self.apple_tvs.linked().items():
            if link["name"].casefold() == name.casefold():
                return player
        return name

    def _play_in_app(self, name: str, found: LibraryMovie, resume: bool):
        # Marquee only collects movies from a server it's paired with, so don't report one as playing
        if not any(d["kind"] == "tv" and d["name"].casefold() == name.casefold()
                   for d in self.app_pairings.devices()):
            raise PlexNFCError(f"Marquee on {name} isn't connected to this server. In Marquee on that device, "
                               f"connect to {self.config.public_base_url.split('://', 1)[-1]} and approve its "
                               f"code in the control panel, naming it {name}.")
        movie, details = self._app_stream(name, found, resume)
        log.info("Sending %s (%s, from %s) to the Marquee app on %s", movie.title, found.key, source_name(movie), name)
        self.app_inbox.send(name, "play", **details)
        return movie

    def app_play_details(self, name: str, key: str, movie=None, resume: bool | None = None) -> dict:
        """A movie's stream address and details for the Marquee app on ``name``."""
        if movie is None:
            return self._app_stream(name, self.find_library_movie(key), resume)[1]
        return self._app_details(name, key, movie, resume)

    def _app_stream(self, name: str, found: LibraryMovie, resume: bool | None):
        """The first copy Marquee can stream (Plex's, if Plex comes first and answers; else the
        next source's), with its details."""
        errors = []
        for copy in found.copies:
            try:
                details = self._app_details(name, found.key, copy, resume)
            except Exception as e:  # noqa: BLE001 - a source that's away: try the next
                log.warning("Couldn't stream %s from %s: %s", found.title, source_name(copy), e)
                errors.append(f"{source_name(copy)}: {e}" if len(found.copies) > 1 else str(e))
                continue
            self._note_playing(f"app:{name}", found.key, copy)
            return copy, details
        raise PlexNFCError("; ".join(errors) or f"Nothing to play for {found.title}")

    def _app_details(self, name: str, key: str, movie, resume: bool | None) -> dict:
        resume = self.config.resume if resume is None else resume
        start = int((movie.viewOffset or 0) / 1000) if resume and getattr(movie, "viewOffset", 0) else 0
        if isinstance(movie, SourceMovie):  # from Jellyfin, or a file Marquee Server sends itself
            url = self.library.stream(movie, start, name)
        else:
            url = plex_hls_url(self.plex_url, self.config.plex_token, movie.ratingKey, start, name)
            self._start_plex_session(url)
        # The app shows these in its player; the poster comes from plexnfc's own /poster
        return {"url": url, "start": 0, **app_movie(movie, key), "poster": f"/poster/{key}?w=600&h=900"}

    def _start_plex_session(self, url: str) -> None:
        """Ask Plex to decide how to stream before the app asks for the stream.

        Newer Plex servers (1.43) turn the stream away with 400 Bad Request unless
        its session has had a decision first. Older ones don't need it, so a
        failure here only gets logged.
        """
        # A plain request: plexapi's own client headers would clash with the stream's
        decision = url.replace("/start.m3u8?", "/decision?", 1)
        try:
            status = requests.get(decision, timeout=15).status_code
        except requests.RequestException as e:
            status = type(e).__name__
        if status != 200:  # the address carries the Plex token, so it stays out of the log
            log.warning("Plex didn't decide how to stream this (%s); trying anyway", status)

    # ---- library for the Marquee app ------------------------------------------------

    def app_library(self, row_size: int = 24, collections: int = 6) -> list[dict]:
        """Poster rows for the app's home screen: recently added, then a few collections,
        then the newest movies from each Jellyfin server and folder."""
        rows = []
        if self.library.plex_in_use or not self.library.sources:
            rows.append({"title": "Recently Added",
                         "items": [self._app_movie(m) for m in self.section.recentlyAdded(maxresults=row_size)]})
            try:
                for collection in self.section.collections()[:collections]:
                    items = [self._app_movie(m) for m in collection.items()[:row_size]]
                    if items:
                        rows.append({"title": collection.title, "items": items})
            except Exception as e:  # collections are a nice-to-have
                log.debug("Couldn't list collections: %s", e)
        for source in self.library.sources:
            try:
                newest = sorted(source.movies(), key=lambda m: m.added, reverse=True)[:row_size]
            except Exception as e:  # noqa: BLE001 - one source being away shouldn't empty the screen
                log.warning("Couldn't list %s for Marquee: %s", source.name, e)
                continue
            if newest:
                rows.append({"title": f"New in {source.name}", "items": [app_movie(m) for m in newest]})
        return rows

    def _app_movie(self, movie) -> dict:
        """app_movie, under the key the movie has in every source."""
        return app_movie(movie, self.library.canonical(movie_key(movie)))

    def app_movies(self, start: int = 0, size: int = 60) -> dict:
        """One page of the whole library, sorted by title."""
        if not self.library.sources:
            items = self.section.search(sort="titleSort", container_start=start, container_size=size,
                                        maxresults=size)
            return {"start": start, "items": [app_movie(m) for m in items], "total": self.section.totalSize}
        from .sources import sort_title
        # Marquee asks a page at a time: read every source once for the pages that follow
        cached = getattr(self, "_app_movies_cache", None)
        if start == 0 or not cached or time.monotonic() - cached[0] > 120:
            cached = self._app_movies_cache = (time.monotonic(),
                                               sorted(self.movies(), key=lambda m: sort_title(m.title)))
        movies = cached[1]
        return {"start": start, "items": [app_movie(m) for m in movies[start:start + size]], "total": len(movies)}

    def app_search(self, query: str, size: int = 40) -> list[dict]:
        if not query.strip():
            return []
        if not self.library.sources:
            return [app_movie(m) for m in self.section.search(title=query, maxresults=size)]
        movies, _ = self.library.index()
        return [app_movie(m.movie, m.key) for m in movies if query.casefold() in m.title.casefold()][:size]

    def _app_now(self, name: str) -> dict:
        status = self.app_inbox.status(name)
        if not status or status.get("state") == "idle":
            return {"state": "idle"}
        key = status.get("key") or ""
        return {"state": status.get("state"), "title": status.get("title"), "key": key,
                "source": self.playing_source(f"app:{name}", key),
                "poster": f"/poster/{key}" if key else None, "player": name,
                "position": int(status.get("position") or 0), "duration": int(status.get("duration") or 0)}

    def tv_off(self) -> None:
        if not (self.ha and self.config.ha_off_script):
            raise PlexNFCError("No HA_OFF_SCRIPT configured")
        try:
            self.ha.run_script(self.config.ha_off_script)
        except HomeAssistantError as e:
            raise PlexNFCError(str(e)) from None

    # ---- controls and now playing (for the Movie Remote and phones) -------------

    def control(self, action: str, client_name: str | None = None):
        name = self._player(client_name)
        self.app_inbox.send(name, {"pause": "pause", "play": "resume", "stop": "stop"}[action])
        return SimpleNamespace(title=name)

    def now_playing(self, client_name: str | None = None) -> dict:
        name = self._app_player(client_name)
        return self._app_now(name) if name else {"state": "idle"}

    def toggle(self, client_name: str | None = None):
        playing = self.now_playing(client_name)["state"] == "playing"
        return self.control("pause" if playing else "play", client_name)

    def seek(self, by_seconds: int, client_name: str | None = None):
        name = self._player(client_name)
        self.app_inbox.send(name, "seek", by=by_seconds)
        return SimpleNamespace(title=name)

    def poster_url(self, movie, width: int = 600, height: int = 900) -> str:
        return self.server.transcodeImage(movie.thumb, height=height, width=width)
