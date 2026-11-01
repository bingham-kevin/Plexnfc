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
    plex_url: str
    plex_token: str
    library: str
    client: str
    public_base_url: str
    resume: bool

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        missing = [k for k in ("PLEX_URL", "PLEX_TOKEN") if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"Missing required settings: {', '.join(missing)} (see .env.example)")
        return cls(
            plex_url=os.environ["PLEX_URL"].rstrip("/"),
            plex_token=os.environ["PLEX_TOKEN"],
            library=os.environ.get("PLEX_LIBRARY", "Movies"),
            client=os.environ.get("PLEX_CLIENT", ""),
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
            resume=os.environ.get("RESUME", "true").lower() in ("1", "true", "yes", "on"),
        )
