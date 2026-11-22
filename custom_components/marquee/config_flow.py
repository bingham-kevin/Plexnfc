"""Add Marquee: find the server, then approve this Home Assistant's code in its control panel."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import MarqueeAPI, MarqueeAuthError, MarqueeError, normalize_url
from .const import CONF_SECRET, DOMAIN

DEFAULT_URL = "http://marquee.local:8080"


class MarqueeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self.url: str = DEFAULT_URL
        self.pairing: dict[str, str] = {}

    def _api(self, secret: str | None = None) -> MarqueeAPI:
        return MarqueeAPI(async_get_clientsession(self.hass), self.url, secret)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self.url = normalize_url(user_input[CONF_URL])
            await self.async_set_unique_id(self.url)
            self._abort_if_unique_id_configured()
            if (error := await self._start_pairing()) is None:
                return await self.async_step_approve()
            errors["base"] = error
        return self.async_show_form(
            step_id="user", errors=errors,
            data_schema=vol.Schema({vol.Required(CONF_URL, default=self.url): str}))

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Marquee Server announces itself with the address its tags use, e.g. http://marquee.local:8080."""
        url = discovery_info.properties.get("url") or f"http://{discovery_info.host}:{discovery_info.port}"
        self.url = normalize_url(url)
        await self.async_set_unique_id(self.url)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"url": self.url}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if (error := await self._start_pairing()) is None:
                return await self.async_step_approve()
            errors["base"] = error
        return self.async_show_form(step_id="discovery_confirm", errors=errors,
                                    description_placeholders={"url": self.url})

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The server forgot this Home Assistant (it was removed there): pair again."""
        self.url = entry_data[CONF_URL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if (error := await self._start_pairing()) is None:
                return await self.async_step_approve()
            errors["base"] = error
        return self.async_show_form(step_id="reauth_confirm", errors=errors,
                                    description_placeholders={"url": self.url})

    async def _start_pairing(self) -> str | None:
        """Ask the server for a code; an error key if that didn't work."""
        api = self._api()
        try:
            if not await api.supports_home_assistant():
                return "too_old"
            self.pairing = await api.start_pairing(self.hass.config.location_name or "Home Assistant")
        except MarqueeError:
            return "cannot_connect"
        return None

    async def async_step_approve(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the code to approve in the control panel; check once they say it's done."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                secret = await self._api().pairing_secret(self.pairing["pair_id"])
            except MarqueeAuthError:
                return self.async_abort(reason="pairing_expired")
            except MarqueeError:
                secret, errors["base"] = None, "cannot_connect"
            else:
                if secret is None:
                    errors["base"] = "not_approved"
            if secret:
                data = {CONF_URL: self.url, CONF_SECRET: secret}
                if self.source == SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
                return self.async_create_entry(title=f"Marquee Server ({self.url.split('://')[-1]})", data=data)
        return self.async_show_form(step_id="approve", errors=errors,
                                    description_placeholders={"code": self.pairing["code"], "url": self.url})
