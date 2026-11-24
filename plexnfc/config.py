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


@dataclass
class Config:
    plex_url: str  # blank: the server on the signed-in Plex account
    plex_token: str
    library: str
    app_player: str  # the Marquee player (its name) taps go to when they don't say
    public_base_url: str
    resume: bool
    ha_url: str = ""
    ha_token: str = ""
    ha_script: str = ""
    ha_off_script: str = ""
    ha_started_script: str = ""  # run once playback has been started
    auth_file: str = ""  # where "Sign in with Plex" keeps its token
    player_scripts: dict | None = None  # player ID -> its own get-ready script, e.g. {"app:Living Room": "script.x"}
    plex_server: str = ""  # with no PLEX_URL: which of the account's servers, if it has several

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        auth_file = os.environ.get("PLEX_AUTH_FILE") or ".plex_auth.json"
        # A token set by hand wins; otherwise use the one from "Sign in with Plex", if any.
        from .plexauth import saved_token

        return cls(
            plex_url=os.environ.get("PLEX_URL", "").rstrip("/"),
            plex_token=os.environ.get("PLEX_TOKEN") or saved_token(auth_file),
            library=os.environ.get("PLEX_LIBRARY", "Movies"),
            app_player=os.environ.get("APP_PLAYER", ""),
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
            resume=os.environ.get("RESUME", "true").lower() in ("1", "true", "yes", "on"),
            ha_url=os.environ.get("HA_URL", "").rstrip("/"),
            ha_token=os.environ.get("HA_TOKEN", ""),
            ha_script=os.environ.get("HA_SCRIPT", ""),
            ha_off_script=os.environ.get("HA_OFF_SCRIPT", ""),
            ha_started_script=os.environ.get("HA_STARTED_SCRIPT", ""),
            auth_file=auth_file,
            player_scripts=parse_player_scripts(os.environ.get("PLAYER_SCRIPTS", "")),
            plex_server=os.environ.get("PLEX_SERVER", ""),
        )


def parse_player_scripts(value: str) -> dict:
    """``app:Living Room = script.living_room_movie; Den TV = script.den`` -> {player: script}."""
    scripts = {}
    for entry in value.split(";"):
        player, sep, script = entry.partition("=")
        if sep and player.strip() and script.strip():
            scripts[player.strip()] = script.strip()
    return scripts
