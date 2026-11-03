import os
from dataclasses import dataclass


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so there's no extra dependency. Real env vars win."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Home Assistant app: Supervisor writes the app's options here.
HA_APP_OPTIONS = "/data/options.json"


def _load_ha_app_options(path: str | None = None) -> None:
    """When running as a Home Assistant app, take settings from the app's options.

    Options are named like the .env settings, in lower case. Inside the app,
    Home Assistant itself is reached through the Supervisor with the token it
    provides, so no long-lived access token is needed.
    """
    path = path or HA_APP_OPTIONS
    if not os.path.exists(path):
        return
    import json

    with open(path) as f:
        options = json.load(f)
    for key, value in options.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        os.environ.setdefault(key.upper(), str(value))
    if os.environ.get("SUPERVISOR_TOKEN"):
        os.environ.setdefault("HA_URL", "http://supervisor/core")
        os.environ.setdefault("HA_TOKEN", os.environ["SUPERVISOR_TOKEN"])


@dataclass
class Config:
    plex_url: str
    plex_token: str
    library: str
    client: str
    public_base_url: str
    resume: bool
    ha_url: str = ""
    ha_token: str = ""
    ha_script: str = ""
    ha_off_script: str = ""
    ha_started_script: str = ""  # run once playback has been started
    ha_player: str = ""
    player_wait: int = 45
    auth_file: str = ""  # where "Sign in with Plex" keeps its token
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    jellyfin_player: str = ""  # play on this Jellyfin player instead of PLEX_CLIENT

    @classmethod
    def from_env(cls) -> "Config":
        _load_ha_app_options()
        _load_dotenv()
        if not os.environ.get("PLEX_URL"):
            raise SystemExit("Missing required setting: PLEX_URL (see .env.example)")
        auth_file = os.environ.get("PLEX_AUTH_FILE") or (
            "/data/plex_auth.json" if os.path.exists(HA_APP_OPTIONS) else ".plex_auth.json")
        # A token set by hand wins; otherwise use the one from "Sign in with Plex", if any.
        from .plexauth import saved_token

        return cls(
            plex_url=os.environ["PLEX_URL"].rstrip("/"),
            plex_token=os.environ.get("PLEX_TOKEN") or saved_token(auth_file),
            library=os.environ.get("PLEX_LIBRARY", "Movies"),
            client=os.environ.get("PLEX_CLIENT", ""),
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
            resume=os.environ.get("RESUME", "true").lower() in ("1", "true", "yes", "on"),
            ha_url=os.environ.get("HA_URL", "").rstrip("/"),
            ha_token=os.environ.get("HA_TOKEN", ""),
            ha_script=os.environ.get("HA_SCRIPT", ""),
            ha_off_script=os.environ.get("HA_OFF_SCRIPT", ""),
            ha_started_script=os.environ.get("HA_STARTED_SCRIPT", ""),
            ha_player=os.environ.get("HA_PLAYER", ""),
            player_wait=int(os.environ.get("PLAYER_WAIT", "45")),
            auth_file=auth_file,
            jellyfin_url=os.environ.get("JELLYFIN_URL", "").rstrip("/"),
            jellyfin_api_key=os.environ.get("JELLYFIN_API_KEY", ""),
            jellyfin_player=os.environ.get("JELLYFIN_PLAYER", ""),
        )
