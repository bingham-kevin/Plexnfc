import json
import os
from dataclasses import dataclass, field


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
    with open(path) as f:
        options = json.load(f)
    for key, value in options.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (list, dict)):
            if not value:
                continue
            value = json.dumps(value)  # e.g. players, read back by _parse_players
        os.environ.setdefault(key.upper(), str(value))
    if os.environ.get("SUPERVISOR_TOKEN"):
        os.environ.setdefault("HA_URL", "http://supervisor/core")
        os.environ.setdefault("HA_TOKEN", os.environ["SUPERVISOR_TOKEN"])


@dataclass
class PlayerProfile:
    """Settings for one TV: which scripts wake it and turn it off.

    ``client`` is the Plex player's name (what ``?client=`` names).
    """
    client: str
    ha_script: str = ""
    ha_off_script: str = ""
    ha_player: str = ""
    player_wait: int = 45


def _parse_players(raw: str, default_wait: int) -> list[PlayerProfile]:
    """PLAYERS: a JSON list like [{"name": "Den TV", "ha_script": "script.den_on"}]."""
    if not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"PLAYERS isn't valid JSON: {e}") from None
    if not isinstance(items, list):
        raise SystemExit("PLAYERS must be a list of players")
    players = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            raise SystemExit(f"Each entry in PLAYERS needs a name: {item!r}")
        wait = item.get("player_wait")
        players.append(PlayerProfile(
            client=str(item["name"]),
            ha_script=item.get("ha_script") or "",
            ha_off_script=item.get("ha_off_script") or "",
            ha_player=item.get("ha_player") or "",
            player_wait=default_wait if wait in (None, "") else int(wait),
        ))
    return players


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
    ha_player: str = ""
    player_wait: int = 45
    auth_file: str = ""  # where "Sign in with Plex" keeps its token
    players: list[PlayerProfile] = field(default_factory=list)  # one per TV, see profile()

    def profile(self, client_name: str | None = None) -> PlayerProfile:
        """The settings for the player a request names with ``?client=``.

        A player listed in PLAYERS (by its Plex name or its ``ha_player``) uses
        only its own scripts. Anything else uses the single HA_SCRIPT /
        HA_OFF_SCRIPT / HA_PLAYER settings, as before there were profiles.
        """
        if client_name:
            wanted = client_name.casefold()
            for p in self.players:
                if wanted in (p.client.casefold(), p.ha_player.casefold()):
                    if p.client.startswith("media_player."):
                        return PlayerProfile(self.client, p.ha_script, p.ha_off_script,
                                             p.ha_player or p.client, p.player_wait)
                    return p
            # A player named in the request: the default scripts, but HA_PLAYER
            # only when no player is named (as before).
            ha_player = client_name if client_name.startswith("media_player.") else ""
            client = self.client if ha_player else client_name
            return PlayerProfile(client, self.ha_script, self.ha_off_script, ha_player, self.player_wait)
        return PlayerProfile(self.client, self.ha_script, self.ha_off_script, self.ha_player, self.player_wait)

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

        player_wait = int(os.environ.get("PLAYER_WAIT", "45"))
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
            ha_player=os.environ.get("HA_PLAYER", ""),
            player_wait=player_wait,
            auth_file=auth_file,
            players=_parse_players(os.environ.get("PLAYERS", ""), player_wait),
        )
