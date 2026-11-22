"""Plex lookups and playback.

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
from .jellyfin import TICKS_PER_SECOND, Jellyfin, JellyfinError, tag_key
from .sources import FolderSource, JellyfinSource, Library, SourceMovie
from .tvapp import AppInbox, AppPairings, jellyfin_hls_url, plex_hls_url

log = logging.getLogger(__name__)

GUID_SOURCES = ("tmdb", "imdb", "tvdb")
KEY_RE = re.compile(r"^(tmdb|imdb|tvdb|rk)-([A-Za-z0-9]+)$")


class PlexNFCError(Exception):
    """Something the person tapping a tag should be told about."""


def movie_key(movie) -> str:
    """Return the stable key to write on a tag for this movie."""
    if isinstance(movie, SourceMovie):  # Jellyfin or a folder: made when it was read
        return movie.key
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
                 ha: HomeAssistant | None = None, jellyfin: Jellyfin | None = None,
                 apple_tvs: AppleTVs | None = None):
        self.config = config
        self._server = server
        self._account = None
        self._session_keys: dict[int, str] = {}  # ratingKey -> tag key, see _session_key
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
        self.jellyfin = jellyfin
        if self.jellyfin is None and config.jellyfin_url and config.jellyfin_api_key:
            self.jellyfin = Jellyfin(config.jellyfin_url, config.jellyfin_api_key)
        self._jellyfin_keys: dict[str, str] = {}  # Jellyfin item ID -> the key it was started with

    @property
    def jellyfin(self) -> Jellyfin | None:
        """The Jellyfin server for Jellyfin players: JELLYFIN_URL's, else the first Jellyfin source's."""
        return self._jellyfin or self.library.jellyfin_client()

    @jellyfin.setter
    def jellyfin(self, value: Jellyfin | None) -> None:
        self._jellyfin = value

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

    @property
    def section(self):
        return self.server.library.section(self.config.library)

    # ---- lookup -------------------------------------------------------------

    def find_movie(self, key: str):
        """The movie for a key: from Plex, or (``jf-…``, ``file-…``) a Jellyfin server or a folder."""
        if self.library.source_for_key(key):
            movie = self.library.find(key)
            if movie is None:
                kind = "Jellyfin server" if key.startswith("jf-") else "movie folder"
                raise PlexNFCError(f"No movie in any {kind} matches {key}")
            return movie
        return self.find_plex_movie(key)

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

    # ---- playback -----------------------------------------------------------

    def _find_client(self, name: str):
        try:
            return self.server.client(name)
        except NotFound:
            pass
        try:
            if self._account is None:
                self._account = self.server.myPlexAccount()
            return self._account.resource(name).connect()
        except Exception as e:  # plexapi raises several types here
            log.debug("Player %r not reachable via plex.tv: %s", name, e)
        return None

    def _account_players(self) -> list[str]:
        """Players on your plex.tv account (Apple TVs, smart TVs...), which local discovery misses."""
        try:
            if self._account is None:
                self._account = self.server.myPlexAccount()
            return [r.name for r in self._account.resources() if "player" in (r.provides or "")]
        except Exception as e:
            log.debug("Couldn't list plex.tv players: %s", e)
            return []

    def get_client(self, name: str | None = None, wait: float = 0):
        """Find a player by name, retrying for up to ``wait`` seconds.

        Players on the local network that have "Advertise as player" turned on
        are found directly. Others (Apple TV, smart TVs, consoles) are found
        through your plex.tv account. Waiting matters when Home Assistant has
        just switched the TV on and the Plex app is still starting.
        """
        name = name or self.config.client
        if not name:
            raise PlexNFCError("No player configured: set PLEX_CLIENT or add ?client=NAME")
        deadline = time.monotonic() + wait
        while True:
            client = self._find_client(name)
            if client is not None:
                return client
            if time.monotonic() >= deadline:
                break
            time.sleep(3)
        available = sorted({c.title for c in self.server.clients()} | set(self._account_players()))
        hint = f" Players found: {', '.join(available)}." if available else ""
        raise PlexNFCError(f"Couldn't find a Plex player named '{name}'. Is it switched on?{hint}")

    def default_player(self) -> str:
        """The ID of the player taps go to when nothing else is chosen."""
        c = self.config
        if c.app_player:
            return f"app:{c.app_player}"
        if c.jellyfin_player:
            return f"jellyfin:{c.jellyfin_player}"
        return c.ha_player or c.client

    def players(self) -> list[dict]:
        """Every player a phone can send its taps to: paired Marquee Apple TVs, Jellyfin
        apps that are open, and Plex players. ``id`` is what ``?client=`` takes."""
        found = [{"id": f"app:{d['name']}", "name": d["name"], "kind": "Marquee"}
                 for d in self.app_pairings.devices() if d["kind"] == "tv"]
        if self.jellyfin:
            try:
                found += [{"id": f"jellyfin:{n}", "name": n, "kind": "Jellyfin"} for n in self.jellyfin.players()]
            except JellyfinError as e:
                log.debug("Couldn't list Jellyfin players: %s", e)
        try:
            plex_names = {c.title for c in self.server.clients()} | set(self._account_players())
        except Exception as e:  # Plex unreachable or signed out: list the others
            log.debug("Couldn't list Plex players: %s", e)
            plex_names = set()
        if self.config.client:
            plex_names.add(self.config.client)
        found += [{"id": n, "name": n, "kind": "Plex"} for n in sorted(plex_names, key=str.casefold)]
        if self.config.ha_player:
            found.append({"id": self.config.ha_player, "name": self.config.ha_player, "kind": "Home Assistant"})
        seen = set()
        return [p for p in found if not (p["id"].casefold() in seen or seen.add(p["id"].casefold()))]

    def _ready_script(self, client_name: str | None) -> str | None:
        """The get-ready script for this player: its own from PLAYER_SCRIPTS, else HA_SCRIPT,
        which belongs to the default player (so a tap for another room doesn't wake this one)."""
        if not client_name:
            return self.config.ha_script or None
        scripts = {k.casefold(): v for k, v in (self.config.player_scripts or {}).items()}
        if script := scripts.get(client_name.casefold()):
            return script
        if client_name.casefold() == self.default_player().casefold():
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
        script = self._ready_script(client_name)
        if not (self.ha and script):
            return None
        try:
            self.ha.run_script(script)
        except HomeAssistantError as e:
            log.warning("%s", e)
            return str(e)
        return None

    def _ha_entity(self, client_name: str | None) -> str | None:
        """The Home Assistant media_player to play on, if HA should start playback."""
        if not self.ha:
            return None
        if client_name:
            return client_name if client_name.startswith("media_player.") else None
        return self.config.ha_player or None

    def _play_via_ha(self, entity: str, movie, resume: bool, ha_error: str | None):
        """Ask Home Assistant's Plex integration to start the movie.

        The Plex entity only accepts playback once the Plex app is running, so
        keep retrying for up to PLAYER_WAIT seconds after the TV was switched on.
        """
        deadline = time.monotonic() + self.config.player_wait
        while True:
            try:
                self.ha.play_plex(entity, movie.ratingKey, resume)
                return movie, SimpleNamespace(title=entity)
            except HomeAssistantError as e:
                if time.monotonic() >= deadline:
                    msg = f"Home Assistant couldn't start playback on {entity}: {e}"
                    raise PlexNFCError(f"{msg} (script: {ha_error})" if ha_error else msg) from None
            time.sleep(3)

    def play(self, key: str, client_name: str | None = None, resume: bool | None = None):
        result = self._play(key, client_name, resume)
        self._run_started_script()
        return result

    def _run_started_script(self) -> None:
        """Start the HA_STARTED_SCRIPT without waiting, e.g. to press a remote button
        once the movie is going."""
        if not (self.ha and self.config.ha_started_script):
            return
        try:
            self.ha.call_service("script", "turn_on", {"entity_id": self.config.ha_started_script})
        except HomeAssistantError as e:
            log.warning("%s", e)

    def _play(self, key: str, client_name: str | None, resume: bool | None):
        movie = self.find_movie(key)
        resume = self.config.resume if resume is None else resume
        if name := self._app_player(client_name):
            # Leave the movie in the inbox before the app is opened, which picks it up
            self._play_in_app(name, key, movie, resume)
            self.prepare_tv(client_name)
            return movie, SimpleNamespace(title=name)
        jellyfin_name = self._jellyfin_player(client_name)
        if isinstance(movie, SourceMovie) and not jellyfin_name:
            raise PlexNFCError(f"{movie.title} comes from {movie.source.name}, which plays on Marquee"
                               f"{' and Jellyfin players' if isinstance(movie.source, JellyfinSource) else ''}, "
                               f"not on {client_name or self.default_player() or 'this player'}")
        if isinstance(movie, SourceMovie) and isinstance(movie.source, FolderSource):
            raise PlexNFCError(f"{movie.title} is a file in {movie.source.name}, which plays on Marquee, "
                               f"not on Jellyfin players")
        ha_error = self.prepare_tv(client_name)
        if jellyfin_name:
            return self._play_via_jellyfin(jellyfin_name, key, movie, resume, ha_error)
        if entity := self._ha_entity(client_name):
            log.info("Playing %s (%s) on %s via Home Assistant", movie.title, key, entity)
            return self._play_via_ha(entity, movie, resume, ha_error)
        wait = self.config.player_wait if self._ready_script(client_name) else 0
        try:
            client = self.get_client(client_name, wait=wait)
        except PlexNFCError as e:
            raise PlexNFCError(f"{e} (Home Assistant: {ha_error})" if ha_error else str(e)) from None
        offset = movie.viewOffset if resume and getattr(movie, "viewOffset", 0) else 0
        log.info("Playing %s (%s) on %s from %sms", movie.title, key, client.title, offset)
        try:
            client.playMedia(movie, offset=offset)
        except Exception as e:  # plexapi raises several types when a player rejects it
            raise PlexNFCError(f"{client.title} didn't accept the movie: {e}") from None
        if not resume and getattr(movie, "viewOffset", 0):
            self._rewind_when_playing(client, movie)
        return movie, client

    # ---- the Marquee Apple TV app -------------------------------------------------

    def _app_player(self, client_name: str | None) -> str | None:
        """The Marquee player to use: ``?client=app:NAME``, else APP_PLAYER."""
        if client_name:
            name = client_name.removeprefix("app:") if client_name.startswith("app:") else None
        else:
            name = self.config.app_player or None
        return self._marquee_name(name) if name else None

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

    def _play_in_app(self, name: str, key: str, movie, resume: bool) -> None:
        # Marquee only collects movies from a server it's paired with, so don't report one as playing
        if not any(d["kind"] == "tv" and d["name"].casefold() == name.casefold()
                   for d in self.app_pairings.devices()):
            raise PlexNFCError(f"Marquee on {name} isn't connected to this server. In Marquee on that device, "
                               f"connect to {self.config.public_base_url.split('://', 1)[-1]} and approve its "
                               f"code in the control panel, naming it {name}.")
        log.info("Sending %s (%s) to the Marquee app on %s", movie.title, key, name)
        self.app_inbox.send(name, "play", **self.app_play_details(name, key, movie, resume))

    def app_play_details(self, name: str, key: str, movie=None, resume: bool | None = None) -> dict:
        """A movie's stream address and details for the Marquee app on ``name``."""
        movie = movie or self.find_movie(key)
        resume = self.config.resume if resume is None else resume
        start = int((movie.viewOffset or 0) / 1000) if resume and getattr(movie, "viewOffset", 0) else 0
        if isinstance(movie, SourceMovie):  # from Jellyfin, or a file Marquee Server sends itself
            url = self.library.stream(movie, start, name)
        elif self.jellyfin and self.config.app_stream == "jellyfin":
            item = self._jellyfin_item(key, movie)
            url = jellyfin_hls_url(self.config.jellyfin_url, self.config.jellyfin_api_key, item["Id"], start, name)
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
                         "items": [app_movie(m) for m in self.section.recentlyAdded(maxresults=row_size)]})
            try:
                for collection in self.section.collections()[:collections]:
                    items = [app_movie(m) for m in collection.items()[:row_size]]
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
        found = []
        if self.library.plex_in_use or not self.library.sources:
            found = [app_movie(m) for m in self.section.search(title=query, maxresults=size)]
        for source in self.library.sources:
            try:
                found += [app_movie(m) for m in source.movies() if query.casefold() in m.title.casefold()]
            except Exception as e:  # noqa: BLE001
                log.debug("Couldn't search %s: %s", source.name, e)
        return found[:size]

    def _app_now(self, name: str) -> dict:
        status = self.app_inbox.status(name)
        if not status or status.get("state") == "idle":
            return {"state": "idle"}
        key = status.get("key") or ""
        return {"state": status.get("state"), "title": status.get("title"), "key": key,
                "poster": f"/poster/{key}" if key else None, "player": name,
                "position": int(status.get("position") or 0), "duration": int(status.get("duration") or 0)}

    # ---- Jellyfin players (e.g. Swiftfin on Apple TV) ---------------------------

    def _jellyfin_player(self, client_name: str | None) -> str | None:
        """The Jellyfin player to use: ``?client=jellyfin:NAME``, else JELLYFIN_PLAYER."""
        if client_name:
            name = client_name.removeprefix("jellyfin:") if client_name.startswith("jellyfin:") else None
        else:
            name = self.config.jellyfin_player or None
        if name and self.jellyfin is None:
            raise PlexNFCError("Add a Jellyfin server in the control panel (or set JELLYFIN_URL and "
                               "JELLYFIN_API_KEY) to play on a Jellyfin player")
        return name

    def _jellyfin_session(self, name: str, wait: float = 0) -> dict:
        """The open Jellyfin app on player ``name``, retrying for up to ``wait`` seconds."""
        deadline = time.monotonic() + wait
        try:
            while True:
                if session := self.jellyfin.session(name):
                    return session
                if time.monotonic() >= deadline:
                    break
                time.sleep(3)
            available = self.jellyfin.players()
        except JellyfinError as e:
            raise PlexNFCError(str(e)) from None
        hint = f" Players found: {', '.join(available)}." if available else ""
        raise PlexNFCError(f"Couldn't find a Jellyfin player named '{name}'. Is the app open?{hint}")

    def _jellyfin_item(self, key: str, movie) -> dict:
        """The item on the Jellyfin players' server for a movie: itself, for a movie from that
        server; else found by title, year and ID (for a Plex movie, or one from another server)."""
        if isinstance(movie, SourceMovie) and movie.source.url == self.jellyfin.url:
            return {"Id": movie.ref, "Name": movie.title}
        inner = key.removeprefix("jf-") if isinstance(movie, SourceMovie) else key
        try:
            return self.jellyfin.find_movie(movie.title, movie.year, inner)
        except JellyfinError as e:
            raise PlexNFCError(str(e)) from None

    def _play_via_jellyfin(self, name: str, key: str, movie, resume: bool, ha_error: str | None):
        item = self._jellyfin_item(key, movie)
        self._jellyfin_keys[item["Id"]] = key
        wait = self.config.player_wait if self._ready_script(f"jellyfin:{name}") else 0
        try:
            session = self._jellyfin_session(name, wait)
        except PlexNFCError as e:
            raise PlexNFCError(f"{e} (Home Assistant: {ha_error})" if ha_error else str(e)) from None
        log.info("Playing %s (%s) on Jellyfin player %s", movie.title, key, name)
        try:
            # Without a start position the Jellyfin app picks up where the movie was left.
            self.jellyfin.play(session, item["Id"], start_seconds=None if resume else 0)
        except JellyfinError as e:
            raise PlexNFCError(str(e)) from None
        self._jellyfin_subtitles_off(name, item, from_start=not resume)
        return movie, SimpleNamespace(title=name)

    def _jellyfin_subtitles_off(self, name: str, item: dict, from_start: bool, wait: float = 20) -> None:
        """Turn subtitles off once the movie is playing.

        Swiftfin ignores the subtitle index in the play command and the user's
        Jellyfin subtitle setting, and turns on a movie's default subtitle track.
        If that movie was already playing, the session shows the old playback for
        a moment, so wait briefly and (when starting over) for a position near 0.
        """
        time.sleep(2)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                session = self.jellyfin.session(name)
                playing = (session or {}).get("NowPlayingItem") or {}
                position = ((session or {}).get("PlayState") or {}).get("PositionTicks") or 0
                restarted = not from_start or position < 60 * TICKS_PER_SECOND
                if playing.get("Id") == item["Id"] and restarted:
                    index = (session.get("PlayState") or {}).get("SubtitleStreamIndex")
                    if index is None or index < 0:
                        return
                    if self.jellyfin.subtitles_off(session):
                        log.info("Turned off subtitle track %s on %s", index, name)
                    else:
                        log.info("%s doesn't support turning subtitles off remotely", name)
                    return
            except JellyfinError as e:  # never let this stop a movie that's already playing
                log.debug("Couldn't check subtitles on %s: %s", name, e)
                return
            time.sleep(1)
        log.debug("%s didn't report %s playing; subtitles left as they are", name, item.get("Name"))

    def _jellyfin_command(self, name: str, command: str, *args):
        session = self._jellyfin_session(name)
        try:
            getattr(self.jellyfin, command)(session, *args)
        except JellyfinError as e:
            raise PlexNFCError(str(e)) from None
        return SimpleNamespace(title=name)

    def _jellyfin_now(self, name: str) -> dict:
        try:
            session = self.jellyfin.session(name)
        except JellyfinError as e:
            raise PlexNFCError(str(e)) from None
        item = (session or {}).get("NowPlayingItem")
        if not item:
            return {"state": "idle"}
        state = session.get("PlayState") or {}
        # The key it was started with (Plex's tmdb-603, or a Jellyfin source's jf-tmdb-603)
        key = self._jellyfin_keys.get(item["Id"]) or tag_key(item) or f"jf-{item['Id']}"
        return {
            "state": "paused" if state.get("IsPaused") else "playing",
            "title": item.get("Name"),
            "year": item.get("ProductionYear"),
            "key": key,
            "poster": f"/poster/{key}",
            "player": name,
            "position": int((state.get("PositionTicks") or 0) / TICKS_PER_SECOND),
            "duration": int((item.get("RunTimeTicks") or 0) / TICKS_PER_SECOND),
        }

    def _rewind_when_playing(self, client, movie, wait: float = 20) -> None:
        """Seek back to the start once the movie is playing.

        Some players (Plex for Roku) ignore the start offset and always resume from
        the saved spot, so a movie that should start over is rewound instead.
        """
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                session = self.current_session(client.title)
                if session is not None and str(session.ratingKey) == str(movie.ratingKey):
                    if (session.viewOffset or 0) > 5000:
                        log.info("%s resumed %s at %sms; seeking to the start",
                                 client.title, movie.title, session.viewOffset)
                        client.seekTo(0)
                    return
            except Exception as e:  # never let this stop a movie that's already playing
                log.debug("Couldn't check where %s started: %s", movie.title, e)
                return
            time.sleep(1)
        log.debug("%s didn't report %s playing; not rewinding", client.title, movie.title)

    def tv_off(self) -> None:
        if not (self.ha and self.config.ha_off_script):
            raise PlexNFCError("No HA_OFF_SCRIPT configured")
        try:
            self.ha.run_script(self.config.ha_off_script)
        except HomeAssistantError as e:
            raise PlexNFCError(str(e)) from None

    def control(self, action: str, client_name: str | None = None):
        if name := self._app_player(client_name):
            self.app_inbox.send(name, {"pause": "pause", "play": "resume", "stop": "stop"}[action])
            return SimpleNamespace(title=name)
        if name := self._jellyfin_player(client_name):
            command = {"pause": "Pause", "play": "Unpause", "stop": "Stop"}[action]
            return self._jellyfin_command(name, "command", command)
        if entity := self._ha_entity(client_name):
            service = {"pause": "media_pause", "play": "media_play", "stop": "media_stop"}[action]
            try:
                self.ha.call_service("media_player", service, {"entity_id": entity})
            except HomeAssistantError as e:
                raise PlexNFCError(str(e)) from None
            return SimpleNamespace(title=entity)
        client = self.get_client(client_name)
        getattr(client, action)()
        return client

    # ---- now playing (for the ESP32 remote) -----------------------------------

    def current_session(self, client_name: str | None = None):
        """The movie currently playing on our player, from the Plex server's sessions."""
        name = client_name if client_name and not client_name.startswith("media_player.") else self.config.client
        for session in self.server.sessions():
            if getattr(session, "type", None) not in ("movie", "episode"):
                continue
            player = getattr(session, "player", None)
            if not name or (player is not None and player.title == name):
                return session
        return None

    def _session_key(self, session) -> str:
        """The tag key for a playing movie.

        Session items come without their TMDB/IMDb IDs, so movie_key() would give
        ``rk-...`` rather than the key on the tag. Look the full item up once per
        movie (the Movie Remote asks every couple of seconds).
        """
        if session.type != "movie":
            return f"rk-{session.ratingKey}"
        key = movie_key(session)
        if not key.startswith("rk-"):
            return key
        rating_key = int(session.ratingKey)
        if rating_key not in self._session_keys:
            try:
                self._session_keys[rating_key] = movie_key(self.server.fetchItem(rating_key))
            except Exception as e:
                log.debug("Couldn't look up %s: %s", rating_key, e)
                return key
        return self._session_keys[rating_key]

    def now_playing(self, client_name: str | None = None) -> dict:
        if name := self._app_player(client_name):
            return self._app_now(name)
        if name := self._jellyfin_player(client_name):
            return self._jellyfin_now(name)
        session = self.current_session(client_name)
        if session is None:
            return {"state": "idle"}
        player = session.player
        key = self._session_key(session)
        return {
            "state": player.state,  # playing / paused / buffering
            "title": session.title,
            "year": session.year,
            "key": key,
            "poster": f"/poster/{key}",
            "player": player.title,
            "position": int((session.viewOffset or 0) / 1000),
            "duration": int((session.duration or 0) / 1000),
        }

    def toggle(self, client_name: str | None = None):
        if self._app_player(client_name):
            playing = self.now_playing(client_name)["state"] == "playing"
            return self.control("pause" if playing else "play", client_name)
        if self._jellyfin_player(client_name):
            playing = self.now_playing(client_name)["state"] == "playing"
            return self.control("pause" if playing else "play", client_name)
        session = self.current_session(client_name)
        playing = session is not None and session.player.state == "playing"
        return self.control("pause" if playing else "play", client_name)

    def seek(self, by_seconds: int, client_name: str | None = None):
        if name := self._app_player(client_name):
            self.app_inbox.send(name, "seek", by=by_seconds)
            return SimpleNamespace(title=name)
        if name := self._jellyfin_player(client_name):
            now = self._jellyfin_now(name)
            if now["state"] == "idle":
                raise PlexNFCError("Nothing is playing")
            target = max(0, now["position"] + by_seconds)
            if now["duration"]:
                target = min(target, now["duration"] - 5)
            return self._jellyfin_command(name, "seek", target)
        session = self.current_session(client_name)
        if session is None:
            raise PlexNFCError("Nothing is playing")
        target = max(0, int((session.viewOffset or 0) / 1000) + by_seconds)
        if session.duration:
            target = min(target, int(session.duration / 1000) - 5)
        if entity := self._ha_entity(client_name):
            try:
                self.ha.call_service("media_player", "media_seek", {"entity_id": entity, "seek_position": target})
            except HomeAssistantError as e:
                raise PlexNFCError(str(e)) from None
            return SimpleNamespace(title=entity)
        client = self.get_client(client_name)
        client.seekTo(target * 1000)
        return client

    def poster_url(self, movie, width: int = 600, height: int = 900) -> str:
        return self.server.transcodeImage(movie.thumb, height=height, width=width)
