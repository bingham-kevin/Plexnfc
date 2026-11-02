"""Run Home Assistant scripts, e.g. to turn the TV on before a movie starts."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class HomeAssistantError(Exception):
    pass


class HomeAssistant:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def run_script(self, entity_id: str, timeout: float = 90) -> None:
        """Run ``script.<name>`` and wait for it to finish.

        Calling the script by name (rather than ``script.turn_on``) makes Home
        Assistant wait until the script has finished, so the TV is ready by the
        time we return.
        """
        name = entity_id.removeprefix("script.")
        log.info("Running Home Assistant script.%s", name)
        try:
            r = requests.post(
                f"{self.url}/api/services/script/{name}",
                headers={"Authorization": f"Bearer {self.token}"},
                json={},
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise HomeAssistantError(f"Couldn't reach Home Assistant: {e}") from e
        if r.status_code == 401:
            raise HomeAssistantError("Home Assistant rejected HA_TOKEN")
        if not r.ok:
            raise HomeAssistantError(f"Home Assistant script.{name} failed: {r.status_code} {r.text[:200]}")
