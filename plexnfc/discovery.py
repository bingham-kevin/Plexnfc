"""Announce plexnfc on the home network with Bonjour, so the Marquee apps find it.

The service is ``_moviebook._tcp`` on this machine's address from PUBLIC_BASE_URL,
and its TXT record carries that URL. Bonjour only reaches the home network when
plexnfc is on the host's network (``host_network`` for the Home Assistant app,
``network_mode: host`` with Docker); otherwise the apps still connect by address.

When PUBLIC_BASE_URL names a ``.local`` host, such as ``http://marquee.local:8080``,
plexnfc answers for that name itself, with whatever address this machine has
now. Tags written with it keep working when the router hands out a new address,
so nobody has to set up a fixed IP.
"""
from __future__ import annotations

import logging
import socket
import threading
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

SERVICE = "_moviebook._tcp.local."


def lan_address() -> str:
    """This machine's address on the home network: the one its traffic leaves from."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))  # nothing is sent; this only picks the route
        return sock.getsockname()[0]


def advertise(public_base_url: str, name: str = "Movie Book"):
    """Start announcing; returns the Zeroconf instance (keep it alive), or None if it can't."""
    try:
        from zeroconf import InterfaceChoice, ServiceInfo, Zeroconf
    except ImportError:
        log.info("zeroconf isn't installed, so the Marquee apps won't find this server by themselves")
        return None
    url = urlsplit(public_base_url)
    host = (url.hostname or "").lower()
    own_name = host.endswith(".local")
    try:
        ip = lan_address() if own_name else socket.gethostbyname(host)
        port = url.port or (443 if url.scheme == "https" else 80)
        server = f"{host}." if own_name else f"moviebook-{ip.replace('.', '-')}.local."
        info = ServiceInfo(SERVICE, f"{name}.{SERVICE}", addresses=[socket.inet_aton(ip)], port=port,
                           properties={"url": public_base_url}, server=server)
        # Answering for our own name has to follow the address to whichever interface it moves to
        zc = Zeroconf(interfaces=InterfaceChoice.All if own_name else [ip])
        zc.register_service(info, allow_name_change=True)
    except Exception as e:  # not on the host's network, or a bad address: the apps can still type it
        log.warning("Couldn't announce plexnfc on the network (%s). The Marquee apps can still connect "
                    "by address; for discovery, run plexnfc on the host network.", e)
        return None
    log.info("Announcing %s as %s on %s", public_base_url, info.name, ip)
    if own_name:
        threading.Thread(target=_follow_address, args=(zc, info, ip), name="bonjour", daemon=True).start()
    return zc


def _follow_address(zc, info, ip: str, every: float = 30, stop: threading.Event | None = None) -> None:
    """Keep the ``.local`` name pointing at this machine when its address changes."""
    stop = stop or threading.Event()
    while not stop.wait(every):
        try:
            new_ip = lan_address()
        except OSError:
            continue  # off the network for now
        if new_ip == ip:
            continue
        info.addresses = [socket.inet_aton(new_ip)]
        try:
            zc.update_service(info)
        except Exception as e:
            log.warning("Couldn't announce the new address %s: %s", new_ip, e)
            continue
        log.info("This machine's address changed to %s; %s now points there", new_ip, info.server)
        ip = new_ip
