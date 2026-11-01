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

from plexapi.exceptions import NotFound
from plexapi.server import PlexServer

from .config import Config

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
    def __init__(self, config: Config, server: PlexServer | None = None):
        self.config = config
        self._server = server

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

    def get_client(self, name: str | None = None):
        """Find a player by name.

        Players on the local network that have "Advertise as player" turned on
        are found directly. Others (Apple TV, smart TVs, consoles) are found
        through your plex.tv account.
        """
        name = name or self.config.client
        if not name:
            raise PlexNFCError("No player configured: set PLEX_CLIENT or add ?client=NAME")
        try:
            return self.server.client(name)
        except NotFound:
            pass
        try:
            return self.server.myPlexAccount().resource(name).connect()
        except Exception as e:  # plexapi raises several types here
            log.warning("Could not reach player %r via plex.tv: %s", name, e)
        available = sorted({c.title for c in self.server.clients()})
        hint = f" Players found: {', '.join(available)}." if available else ""
        raise PlexNFCError(f"Couldn't find a Plex player named '{name}'. Is it switched on?{hint}")

    def play(self, key: str, client_name: str | None = None, resume: bool | None = None):
        movie = self.find_movie(key)
        client = self.get_client(client_name)
        resume = self.config.resume if resume is None else resume
        offset = movie.viewOffset if resume and getattr(movie, "viewOffset", 0) else 0
        log.info("Playing %s (%s) on %s from %sms", movie.title, key, client.title, offset)
        client.playMedia(movie, offset=offset)
        return movie, client

    def control(self, action: str, client_name: str | None = None):
        client = self.get_client(client_name)
        getattr(client, action)()
        return client

    def poster_url(self, movie, width: int = 600, height: int = 900) -> str:
        return self.server.transcodeImage(movie.thumb, height=height, width=width)
