"""Wake an Apple TV and open Marquee on it, without Home Assistant.

Marquee only picks up a tapped movie while it's open (see tvapp.py), so
something has to bring it to the front. This does it with pyatv over Apple's
Companion protocol, the same way Home Assistant's Apple TV integration does:
pair once with the PIN the Apple TV shows, then plexnfc can switch the Apple TV
on and open Marquee whenever a tag is tapped. No prompt appears on the TV.

Each paired Apple TV is linked to a Marquee player name (``app:NAME``), and the
Companion credentials are saved in ``path``. pyatv is optional: without it,
``available`` is False and the get-ready scripts are used as before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time

log = logging.getLogger(__name__)

try:
    import pyatv
    from pyatv.const import Protocol
except ImportError:  # the Home Assistant app doesn't need it
    pyatv = None

MARQUEE_BUNDLE_ID = "com.binghamkevin.moviebook"
PAIRING_TTL = 300  # the PIN on the Apple TV goes away after a few minutes anyway
TIMEOUT = 15


class AppleTVError(Exception):
    pass


def _identifiers(link: dict) -> list[str]:
    """Every ID an Apple TV goes by. pyatv's main ``identifier`` depends on how the
    Apple TV was found (a UUID from Bonjour, a MAC address when asked directly)."""
    return link.get("identifiers") or [link["identifier"]]


class AppleTVs:
    """Apple TVs plexnfc can wake and open Marquee on, by Marquee player name."""

    def __init__(self, path: str, bundle_id: str = MARQUEE_BUNDLE_ID):
        self.path = path
        self.bundle_id = bundle_id
        self._lock = threading.Lock()
        self._pairings: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        try:
            with open(path) as f:
                self._links: dict[str, dict] = json.load(f).get("apple_tvs", {})
        except (OSError, ValueError):
            self._links = {}

    @property
    def available(self) -> bool:
        return pyatv is not None

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"apple_tvs": self._links}, f)
        os.replace(tmp, self.path)

    def _run(self, coro, timeout: float = TIMEOUT):
        """Run a pyatv coroutine on plexnfc's one asyncio loop, from any request thread.

        Pairing spans two requests (start, then the PIN), so its connection has to
        stay on a loop that outlives the first one.
        """
        if not self.available:
            coro.close()
            raise AppleTVError("Install pyatv to open Marquee on Apple TVs without Home Assistant")
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="pyatv", daemon=True).start()
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)
        except AppleTVError:
            raise
        except TimeoutError:
            raise AppleTVError("The Apple TV didn't answer. Is it on this network?") from None
        except Exception as e:  # pyatv raises many types for network and protocol trouble
            raise AppleTVError(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__) from None

    # ---- finding and pairing ---------------------------------------------------------

    def scan(self) -> list[dict]:
        """Apple TVs on this network, with whether each is linked already. (HomePods
        run tvOS too, so they're told apart by model.)"""
        async def scan():
            return await pyatv.scan(asyncio.get_running_loop(), timeout=5, protocol=Protocol.Companion)

        linked = {i: player for player, link in self.linked().items() for i in _identifiers(link)}
        found = [{"identifier": c.identifier, "name": c.name, "address": str(c.address),
                  "linked": next((linked[i] for i in c.all_identifiers if i in linked), None)}
                 for c in self._run(scan()) if (c.device_info.raw_model or "").startswith("AppleTV")]
        return sorted(found, key=lambda d: d["name"].casefold())

    def start_pairing(self, identifier: str, player: str) -> str:
        """Ask the Apple TV to show a PIN; returns a pairing ID for ``finish_pairing``."""
        player = " ".join(player.split())[:40]
        if not player:
            raise AppleTVError("Choose the Marquee player this Apple TV is")

        async def begin():
            loop = asyncio.get_running_loop()
            configs = await pyatv.scan(loop, identifier=identifier, timeout=5)
            if not configs:
                raise AppleTVError("That Apple TV isn't on the network any more")
            pairing = await pyatv.pair(configs[0], Protocol.Companion, loop, name="Marquee Server")
            await pairing.begin()
            return configs[0], pairing

        config, pairing = self._run(begin())
        pair_id = secrets.token_urlsafe(16)
        with self._lock:
            self._pairings[pair_id] = {"config": config, "pairing": pairing, "player": player,
                                       "created": time.monotonic()}
        self._drop_old_pairings()
        return pair_id

    def finish_pairing(self, pair_id: str, pin: str) -> str:
        """Pair with the PIN from the TV screen; returns the linked player name."""
        with self._lock:
            entry = self._pairings.pop(pair_id, None)
        if not entry:
            raise AppleTVError("That pairing has expired. Start again.")
        pairing = entry["pairing"]

        async def finish():
            try:
                pairing.pin(int(pin.strip()))
                await pairing.finish()
                if not pairing.has_paired:
                    raise AppleTVError("The Apple TV didn't accept that PIN")
                return pairing.service.credentials
            finally:
                await pairing.close()

        try:
            credentials = self._run(finish())
        except ValueError:
            raise AppleTVError("The PIN is the 4 digits on the TV") from None
        config = entry["config"]
        with self._lock:
            # one Apple TV per player, and one player per Apple TV
            self._links = {p: link for p, link in self._links.items()
                           if p.casefold() != entry["player"].casefold()
                           and not set(_identifiers(link)) & set(config.all_identifiers)}
            self._links[entry["player"]] = {"identifier": config.identifier, "identifiers": config.all_identifiers,
                                            "name": config.name, "address": str(config.address),
                                            "credentials": credentials}
            self._save()
        log.info("Paired Apple TV %s for Marquee player %s", config.name, entry["player"])
        return entry["player"]

    def _drop_old_pairings(self) -> None:
        with self._lock:
            old = [k for k, v in self._pairings.items() if time.monotonic() - v["created"] > PAIRING_TTL]
            stale = [self._pairings.pop(k)["pairing"] for k in old]
        for pairing in stale:
            try:
                self._run(pairing.close())
            except AppleTVError:
                pass

    def linked(self) -> dict[str, dict]:
        """Marquee player name -> its Apple TV (without the credentials)."""
        with self._lock:
            return {p: {k: v for k, v in link.items() if k != "credentials"} for p, link in self._links.items()}

    def credentials_for(self, identifiers) -> str | None:
        """The pairing credentials of the linked Apple TV going by any of these IDs.
        They're good for AirPlay audio too, so music needn't pair it again."""
        wanted = set(identifiers)
        with self._lock:
            return next((link.get("credentials") for link in self._links.values()
                         if wanted & set(_identifiers(link)) and link.get("credentials")), None)

    def link_for(self, player: str) -> dict | None:
        with self._lock:
            return next((link for p, link in self._links.items() if p.casefold() == player.casefold()), None)

    def remove(self, player: str) -> None:
        with self._lock:
            before = len(self._links)
            self._links = {p: link for p, link in self._links.items() if p.casefold() != player.casefold()}
            if len(self._links) != before:
                self._save()

    # ---- opening Marquee -------------------------------------------------------------

    def open_marquee(self, player: str) -> None:
        """Switch on the Apple TV linked to ``player`` and bring Marquee to the front."""
        link = self.link_for(player)
        if not link:
            raise AppleTVError(f"No Apple TV is paired for {player}")

        async def open_app():
            loop = asyncio.get_running_loop()
            # Ask the address it had first (quick, and works where multicast doesn't), then look again
            ids = set(_identifiers(link))
            configs = (await pyatv.scan(loop, hosts=[link["address"]], timeout=3)
                       or await pyatv.scan(loop, identifier=ids, timeout=5))
            config = next((c for c in configs if ids & set(c.all_identifiers)), None)
            if config is None:
                raise AppleTVError(f"Couldn't find the Apple TV {link['name']} on the network")
            config.set_credentials(Protocol.Companion, link["credentials"])
            atv = await pyatv.connect(config, loop, protocol=Protocol.Companion)
            try:
                await atv.power.turn_on()
                await atv.apps.launch_app(self.bundle_id)
            finally:
                await asyncio.gather(*atv.close(), return_exceptions=True)
            return str(config.address), list(config.all_identifiers)

        address, identifiers = self._run(open_app())
        if address != link["address"] or identifiers != link.get("identifiers"):
            with self._lock:
                for p, stored in self._links.items():
                    if p.casefold() == player.casefold():
                        stored.update(address=address, identifiers=identifiers)
                self._save()
        log.info("Opened Marquee on %s (%s)", link["name"], player)
