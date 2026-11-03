"""Run Home Assistant scripts, e.g. to turn the TV on before a movie starts."""
from __future__ import annotations

import json
import logging

import requests

log = logging.getLogger(__name__)


class HomeAssistantError(Exception):
    pass


class HomeAssistant:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call_service(self, domain: str, service: str, data: dict | None = None, timeout: float = 90) -> None:
        """Call a Home Assistant action and wait for it to finish."""
        try:
            r = requests.post(
                f"{self.url}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=data or {},
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise HomeAssistantError(f"Couldn't reach Home Assistant: {e}") from e
        if r.status_code == 401:
            raise HomeAssistantError("Home Assistant rejected HA_TOKEN")
        if not r.ok:
            raise HomeAssistantError(f"Home Assistant {domain}.{service} failed: {r.status_code} {r.text[:200]}")

    def state(self, entity_id: str) -> str | None:
        """The entity's current state, or None if Home Assistant doesn't know it."""
        try:
            r = requests.get(f"{self.url}/api/states/{entity_id}",
                             headers={"Authorization": f"Bearer {self.token}"}, timeout=15)
        except requests.RequestException as e:
            raise HomeAssistantError(f"Couldn't reach Home Assistant: {e}") from e
        if r.status_code == 401:
            raise HomeAssistantError("Home Assistant rejected HA_TOKEN")
        if r.status_code == 404:
            return None
        if not r.ok:
            raise HomeAssistantError(f"Home Assistant couldn't read {entity_id}: {r.status_code}")
        return r.json().get("state")

    def run_script(self, entity_id: str, timeout: float = 90) -> None:
        """Run ``script.<name>`` and wait for it to finish.

        Calling the script by name (rather than ``script.turn_on``) makes Home
        Assistant wait until the script has finished, so the TV is ready by the
        time we return.
        """
        name = entity_id.removeprefix("script.")
        log.info("Running Home Assistant script.%s", name)
        self.call_service("script", name, timeout=timeout)

    def play_plex(self, entity_id: str, rating_key: int, resume: bool) -> None:
        """Start a Plex item on a Plex media_player entity via HA's Plex integration."""
        # Home Assistant skips actions on an unavailable entity with only a log
        # warning, so check first rather than report a movie that never started.
        state = self.state(entity_id)
        if state is None:
            raise HomeAssistantError(f"Home Assistant has no entity {entity_id}")
        if state in ("unavailable", "unknown"):
            raise HomeAssistantError(f"{entity_id} is {state}, so the Plex app isn't ready for playback")
        payload = json.dumps({"plex_key": int(rating_key), "resume": resume})
        self.call_service("media_player", "play_media", {
            "entity_id": entity_id,
            "media_content_type": "movie",
            "media_content_id": f"plex://{payload}",
        })
