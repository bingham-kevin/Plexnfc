"""Announce plexnfc on the home network with Bonjour, so the Marquee apps find it.

The service is ``_moviebook._tcp`` on this machine's address from PUBLIC_BASE_URL,
and its TXT record carries that URL. Bonjour only reaches the home network when
plexnfc is on the host's network (``host_network`` for the Home Assistant app,
``network_mode: host`` with Docker); otherwise the apps still connect by address.
"""
from __future__ import annotations

import logging
import socket
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

SERVICE = "_moviebook._tcp.local."


def advertise(public_base_url: str, name: str = "Movie Book"):
    """Start announcing; returns the Zeroconf instance (keep it alive), or None if it can't."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        log.info("zeroconf isn't installed, so the Marquee apps won't find this server by themselves")
        return None
    url = urlsplit(public_base_url)
    try:
        ip = socket.gethostbyname(url.hostname or "")
        port = url.port or (443 if url.scheme == "https" else 80)
        info = ServiceInfo(SERVICE, f"{name}.{SERVICE}", addresses=[socket.inet_aton(ip)], port=port,
                           properties={"url": public_base_url}, server=f"moviebook-{ip.replace('.', '-')}.local.")
        zc = Zeroconf(interfaces=[ip])
        zc.register_service(info, allow_name_change=True)
    except Exception as e:  # not on the host's network, or a bad address: the apps can still type it
        log.warning("Couldn't announce plexnfc on the network (%s). The Marquee apps can still connect "
                    "by address; for discovery, run plexnfc on the host network.", e)
        return None
    log.info("Announcing %s as %s on %s", public_base_url, info.name, ip)
    return zc
