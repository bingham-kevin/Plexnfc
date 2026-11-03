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
from .jellyfin import TICKS_PER_SECOND, Jellyfin, JellyfinError, tag_key

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
                 ha: HomeAssistant | None = None, jellyfin: Jellyfin | None = None):
        self.config = config
        self._server = server
        self._account = None
        self._session_keys: dict[int, str] = {}  # ratingKey -> tag key, see _session_key
        self.ha = ha
        if self.ha is None and config.ha_url and config.ha_token:
            self.ha = HomeAssistant(config.ha_url, config.ha_token)
        self.jellyfin = jellyfin
        if self.jellyfin is None and config.jellyfin_url and config.jellyfin_api_key:
            self.jellyfin = Jellyfin(config.jellyfin_url, config.jellyfin_api_key)

    @property
    def server(self) -> PlexServer:
        if self._server is None:
            if not self.config.plex_token:
                raise PlexNFCError("Not signed in to Plex yet. Open plexnfc's page and choose Sign in with Plex.")
            self._server = PlexServer(self.config.plex_url, self.config.plex_token)
        return self._server

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
        ha_error = self.prepare_tv()
        resume = self.config.resume if resume is None else resume
        if name := self._jellyfin_player(client_name):
            return self._play_via_jellyfin(name, key, movie, resume, ha_error)
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
        try:
            client.playMedia(movie, offset=offset)
        except Exception as e:  # plexapi raises several types when a player rejects it
            raise PlexNFCError(f"{client.title} didn't accept the movie: {e}") from None
        if not resume and getattr(movie, "viewOffset", 0):
            self._rewind_when_playing(client, movie)
        return movie, client

    # ---- Jellyfin players (e.g. Swiftfin on Apple TV) ---------------------------

    def _jellyfin_player(self, client_name: str | None) -> str | None:
        """The Jellyfin player to use: ``?client=jellyfin:NAME``, else JELLYFIN_PLAYER."""
        if client_name:
            name = client_name.removeprefix("jellyfin:") if client_name.startswith("jellyfin:") else None
        else:
            name = self.config.jellyfin_player or None
        if name and self.jellyfin is None:
            raise PlexNFCError("Set JELLYFIN_URL and JELLYFIN_API_KEY to play on a Jellyfin player")
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

    def _play_via_jellyfin(self, name: str, key: str, movie, resume: bool, ha_error: str | None):
        try:
            item = self.jellyfin.find_movie(movie.title, movie.year, key)
        except JellyfinError as e:
            raise PlexNFCError(str(e)) from None
        wait = self.config.player_wait if self.config.ha_script else 0
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
        key = tag_key(item) or f"jf-{item['Id']}"
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
        if self._jellyfin_player(client_name):
            playing = self.now_playing(client_name)["state"] == "playing"
            return self.control("pause" if playing else "play", client_name)
        session = self.current_session(client_name)
        playing = session is not None and session.player.state == "playing"
        return self.control("pause" if playing else "play", client_name)

    def seek(self, by_seconds: int, client_name: str | None = None):
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
