"""Plex lookups and playback.

Each poster's NFC tag stores a short, stable movie key such as ``tmdb-603``.
We prefer external IDs (TMDB / IMDb / TVDB) over Plex's internal ratingKey
because ratingKeys change if a movie is re-added or re-matched, which would
silently break a printed page. ``rk-12345`` is accepted as a fallback for
items without an external ID.
"""
from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace

from plexapi.exceptions import NotFound
from plexapi.server import PlexServer

from .config import Config
from .homeassistant import HomeAssistant, HomeAssistantError

log = logging.getLogger(__name__)

GUID_SOURCES = ("tmdb", "imdb", "tvdb")
KEY_RE = re.compile(r"^(tmdb|imdb|tvdb|rk)-([A-Za-z0-9]+)$")


class PlexNFCError(Exception):
    """Something the person tapping a tag should be told about."""


def movie_key(movie) -> str:
    """Return the stable key to write on a tag for this movie."""
    guids = {g.id.split("://", 1)[0]: g.id.split("://", 1)[1] for g in getattr(movie, "guids", [])}
    for source in GUID_SOURCES:
        if source in guids:
            return f"{source}-{guids[source]}"
    return f"rk-{movie.ratingKey}"


class Plex:
    def __init__(self, config: Config, server: PlexServer | None = None,
                 ha: HomeAssistant | None = None):
        self.config = config
        self._server = server
        self._account = None
        self.ha = ha
        if self.ha is None and config.ha_url and config.ha_token:
            self.ha = HomeAssistant(config.ha_url, config.ha_token)

    @property
    def server(self) -> PlexServer:
        if self._server is None:
            self._server = PlexServer(self.config.plex_url, self.config.plex_token)
        return self._server

    @property
    def section(self):
        return self.server.library.section(self.config.library)

    # ---- lookup -------------------------------------------------------------

    def find_movie(self, key: str):
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

    def movies(self, collection: str | None = None):
        if collection:
            return self.section.collection(collection).items()
        return self.section.all(sort="titleSort")

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
        available = sorted({c.title for c in self.server.clients()})
        hint = f" Players found: {', '.join(available)}." if available else ""
        raise PlexNFCError(f"Couldn't find a Plex player named '{name}'. Is it switched on?{hint}")

    def prepare_tv(self) -> str | None:
        """Run the Home Assistant "get the TV ready" script, if configured.

        Returns an error message instead of raising: the TV may already be on,
        so a Home Assistant problem shouldn't stop the movie from trying.
        """
        if not (self.ha and self.config.ha_script):
            return None
        try:
            self.ha.run_script(self.config.ha_script)
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
        movie = self.find_movie(key)
        ha_error = self.prepare_tv()
        resume = self.config.resume if resume is None else resume
        if entity := self._ha_entity(client_name):
            log.info("Playing %s (%s) on %s via Home Assistant", movie.title, key, entity)
            return self._play_via_ha(entity, movie, resume, ha_error)
        wait = self.config.player_wait if self.config.ha_script else 0
        try:
            client = self.get_client(client_name, wait=wait)
        except PlexNFCError as e:
            raise PlexNFCError(f"{e} (Home Assistant: {ha_error})" if ha_error else str(e)) from None
        offset = movie.viewOffset if resume and getattr(movie, "viewOffset", 0) else 0
        log.info("Playing %s (%s) on %s from %sms", movie.title, key, client.title, offset)
        client.playMedia(movie, offset=offset)
        return movie, client

    def tv_off(self) -> None:
        if not (self.ha and self.config.ha_off_script):
            raise PlexNFCError("No HA_OFF_SCRIPT configured")
        try:
            self.ha.run_script(self.config.ha_off_script)
        except HomeAssistantError as e:
            raise PlexNFCError(str(e)) from None

    def control(self, action: str, client_name: str | None = None):
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

    def now_playing(self, client_name: str | None = None) -> dict:
        session = self.current_session(client_name)
        if session is None:
            return {"state": "idle"}
        player = session.player
        key = movie_key(session) if session.type == "movie" else f"rk-{session.ratingKey}"
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
        session = self.current_session(client_name)
        playing = session is not None and session.player.state == "playing"
        return self.control("pause" if playing else "play", client_name)

    def seek(self, by_seconds: int, client_name: str | None = None):
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
