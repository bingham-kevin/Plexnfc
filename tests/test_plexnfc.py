from types import SimpleNamespace as NS
from unittest import mock

import pytest
from plexapi.exceptions import NotFound

from plexnfc.config import Config
from plexnfc.plex import Plex, PlexNFCError, movie_key
from plexnfc.server import create_app

CONFIG = Config("http://plex:32400", "tok", "Movies", "Living Room", "http://nfc.local:8080", True)


def make_movie(title="The Matrix", guids=("imdb://tt0133093", "tmdb://603"), offset=0):
    return NS(title=title, year=1999, ratingKey=42, thumb="/t.jpg", viewOffset=offset,
              guids=[NS(id=g) for g in guids])


@pytest.fixture
def plex():
    movie = make_movie()
    client = mock.Mock(title="Living Room")
    section = mock.Mock()
    section.getGuid.side_effect = lambda g: movie if g in [x.id for x in movie.guids] else (_ for _ in ()).throw(NotFound())
    section.all.return_value = [movie]
    server = mock.Mock()
    server.library.section.return_value = section
    server.client.side_effect = lambda n: client if n == "Living Room" else (_ for _ in ()).throw(NotFound())
    server.myPlexAccount.return_value.resource.side_effect = NotFound()
    server.clients.return_value = [client]
    p = Plex(CONFIG, server=server)
    p._movie, p._client = movie, client
    return p


def test_movie_key_prefers_tmdb():
    assert movie_key(make_movie()) == "tmdb-603"
    assert movie_key(make_movie(guids=["imdb://tt1"])) == "imdb-tt1"
    assert movie_key(make_movie(guids=[])) == "rk-42"


def test_play_starts_movie_on_client(plex):
    app = create_app(CONFIG, plex).test_client()
    r = app.get("/m/tmdb-603")
    assert r.status_code == 200 and b"The Matrix" in r.data
    plex._client.playMedia.assert_called_once_with(plex._movie, offset=0)


def test_play_resumes(plex):
    plex._movie.viewOffset = 5000
    create_app(CONFIG, plex).test_client().get("/m/tmdb-603")
    plex._client.playMedia.assert_called_once_with(plex._movie, offset=5000)
    plex._client.playMedia.reset_mock()
    create_app(CONFIG, plex).test_client().get("/m/tmdb-603?resume=0")
    plex._client.playMedia.assert_called_once_with(plex._movie, offset=0)


def test_unknown_movie_and_bad_key(plex):
    app = create_app(CONFIG, plex).test_client()
    assert app.get("/m/tmdb-999").status_code == 404
    assert app.get("/m/garbage").status_code == 404


def test_unknown_client_lists_available(plex):
    r = create_app(CONFIG, plex).test_client().get("/m/tmdb-603?client=Bedroom")
    assert r.status_code == 404 and b"Living Room" in r.data


def test_controls(plex):
    app = create_app(CONFIG, plex).test_client()
    assert app.get("/pause").status_code == 200
    plex._client.pause.assert_called_once()


def test_tags_page(plex):
    r = create_app(CONFIG, plex).test_client().get("/tags")
    assert b"http://nfc.local:8080/m/tmdb-603" in r.data


def test_bad_key_rejected():
    with pytest.raises(PlexNFCError):
        Plex(CONFIG, server=mock.Mock()).find_movie("../etc")


def ha_plex(plex, **overrides):
    from dataclasses import replace
    settings = dict(ha_url="http://ha", ha_token="t", ha_script="script.movie_night",
                    ha_off_script="script.tv_off", player_wait=0)
    plex.config = replace(CONFIG, **{**settings, **overrides})
    plex.ha = mock.Mock()
    return plex


def test_home_assistant_script_runs_before_play(plex):
    plex = ha_plex(plex)
    order = []
    plex.ha.run_script.side_effect = lambda s: order.append(s)
    plex._client.playMedia.side_effect = lambda *a, **k: order.append("play")
    assert create_app(plex.config, plex).test_client().get("/m/tmdb-603").status_code == 200
    assert order == ["script.movie_night", "play"]


def test_waits_for_player_to_appear(plex, monkeypatch):
    plex = ha_plex(plex, player_wait=30)
    monkeypatch.setattr("plexnfc.plex.time.sleep", lambda s: None)
    calls = iter([NotFound(), NotFound(), plex._client])

    def client(name):
        c = next(calls)
        if isinstance(c, Exception):
            raise c
        return c
    plex.server.client.side_effect = client
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    plex._client.playMedia.assert_called_once()


def test_home_assistant_failure_still_tries_to_play(plex):
    from plexnfc.homeassistant import HomeAssistantError
    plex = ha_plex(plex)
    plex.ha.run_script.side_effect = HomeAssistantError("HA down")
    assert create_app(plex.config, plex).test_client().get("/m/tmdb-603").status_code == 200
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603?client=Nope")
    assert b"HA down" in r.data


def test_off_tag(plex):
    plex = ha_plex(plex)
    assert create_app(plex.config, plex).test_client().get("/off").status_code == 200
    plex.ha.run_script.assert_called_once_with("script.tv_off")


def test_off_without_config(plex):
    assert create_app(CONFIG, plex).test_client().get("/off").status_code == 404


def test_ha_client_calls_script_service(monkeypatch):
    from plexnfc.homeassistant import HomeAssistant
    post = mock.Mock(return_value=mock.Mock(ok=True, status_code=200))
    monkeypatch.setattr("plexnfc.homeassistant.requests.post", post)
    HomeAssistant("http://ha:8123/", "tok").run_script("script.movie_night")
    url = post.call_args.args[0]
    assert url == "http://ha:8123/api/services/script/movie_night"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"


def test_playback_via_home_assistant(plex):
    plex = ha_plex(plex, ha_player="media_player.plex_apple_tv")
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert r.status_code == 200 and b"media_player.plex_apple_tv" in r.data
    plex.ha.run_script.assert_called_once_with("script.movie_night")
    plex.ha.play_plex.assert_called_once_with("media_player.plex_apple_tv", 42, True)
    plex._client.playMedia.assert_not_called()


def test_ha_playback_retries_until_player_ready(plex, monkeypatch):
    from plexnfc.homeassistant import HomeAssistantError
    plex = ha_plex(plex, ha_player="media_player.plex_apple_tv", player_wait=30)
    monkeypatch.setattr("plexnfc.plex.time.sleep", lambda s: None)
    plex.ha.play_plex.side_effect = [HomeAssistantError("not accepting playback"), None]
    assert create_app(plex.config, plex).test_client().get("/m/tmdb-603").status_code == 200
    assert plex.ha.play_plex.call_count == 2


def test_ha_controls(plex):
    plex = ha_plex(plex, ha_player="media_player.plex_apple_tv")
    create_app(plex.config, plex).test_client().get("/pause")
    plex.ha.call_service.assert_called_once_with("media_player", "media_pause", {"entity_id": "media_player.plex_apple_tv"})


def test_ha_play_plex_payload(monkeypatch):
    import json
    from plexnfc.homeassistant import HomeAssistant
    post = mock.Mock(return_value=mock.Mock(ok=True, status_code=200))
    monkeypatch.setattr("plexnfc.homeassistant.requests.post", post)
    HomeAssistant("http://ha", "t").play_plex("media_player.x", "42", False)
    assert post.call_args.args[0] == "http://ha/api/services/media_player/play_media"
    body = post.call_args.kwargs["json"]
    assert body["media_content_type"] == "movie"
    assert json.loads(body["media_content_id"].removeprefix("plex://")) == {"plex_key": 42, "resume": False}


def add_session(plex, state="playing", offset=60_000, player="Living Room"):
    session = make_movie(offset=offset)
    session.type = "movie"
    session.duration = 7_200_000
    session.player = NS(title=player, state=state)
    plex.server.sessions.return_value = [session]
    return session


def test_now_idle(plex):
    plex.server.sessions.return_value = []
    assert create_app(CONFIG, plex).test_client().get("/now").json == {"state": "idle"}


def test_now_playing(plex):
    add_session(plex)
    data = create_app(CONFIG, plex).test_client().get("/now").json
    assert data["state"] == "playing" and data["title"] == "The Matrix"
    assert data["poster"] == "/poster/tmdb-603" and data["position"] == 60 and data["duration"] == 7200


def test_now_ignores_other_players(plex):
    add_session(plex, player="Bedroom")
    assert create_app(CONFIG, plex).test_client().get("/now").json["state"] == "idle"


def test_toggle_pauses_when_playing(plex):
    add_session(plex, state="playing")
    create_app(CONFIG, plex).test_client().get("/toggle")
    plex._client.pause.assert_called_once()
    add_session(plex, state="paused")
    create_app(CONFIG, plex).test_client().get("/toggle")
    plex._client.play.assert_called_once()


def test_seek(plex):
    add_session(plex, offset=60_000)
    app = create_app(CONFIG, plex).test_client()
    assert app.get("/seek?by=30").json["ok"]
    plex._client.seekTo.assert_called_with(90_000)
    app.get("/seek?by=-600")
    plex._client.seekTo.assert_called_with(0)


def test_async_play_reports_starting_then_playing(plex):
    import threading
    gate = threading.Event()
    plex._client.playMedia.side_effect = lambda *a, **k: gate.wait(5)
    plex.server.sessions.return_value = []
    app = create_app(CONFIG, plex).test_client()
    r = app.get("/m/tmdb-603?async=1")
    assert r.status_code == 202
    now = app.get("/now").json
    assert now["state"] == "starting" and now["poster"] == "/poster/tmdb-603"
    gate.set()
    add_session(plex)
    assert app.get("/now").json["state"] == "playing"


def test_async_play_error_is_reported(plex):
    import time as _t
    plex.server.sessions.return_value = []
    app = create_app(CONFIG, plex).test_client()
    app.get("/m/tmdb-603?async=1&client=Nope")
    for _ in range(50):
        if app.get("/now").json["state"] == "error":
            break
        _t.sleep(0.02)
    assert "Nope" in app.get("/now").json["message"]


def test_resize_jpeg_is_exact_and_baseline():
    import io
    from PIL import Image
    from plexnfc.server import resize_jpeg
    src = io.BytesIO()
    Image.new("RGB", (600, 900), "red").save(src, "JPEG", progressive=True)
    out = Image.open(io.BytesIO(resize_jpeg(src.getvalue(), 200, 300)))
    assert out.size == (200, 300) and not out.info.get("progressive")


def test_book_page_uses_relative_poster_urls(plex):
    r = create_app(CONFIG, plex).test_client().get("/book?layout=grid&title=Our+Movies")
    assert r.status_code == 200
    assert b'src="poster/tmdb-603?w=1200&amp;h=1800"' in r.data and b"Our Movies" in r.data


def test_tags_csv(plex):
    r = create_app(CONFIG, plex).test_client().get("/tags.csv")
    assert b"The Matrix,1999,http://nfc.local:8080/m/tmdb-603" in r.data


def test_index_links_are_relative(plex):
    r = create_app(CONFIG, plex).test_client().get("/")
    assert b'href="tags"' in r.data and b'action="book"' in r.data and b'href="/' not in r.data


def test_ha_app_options(tmp_path, monkeypatch):
    import json
    from plexnfc import config as cfg
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"plex_url": "http://plex:32400", "plex_token": "abc", "resume": False,
                                "ha_player": "", "player_wait": 30}))
    for k in ("PLEX_URL", "PLEX_TOKEN", "RESUME", "HA_PLAYER", "PLAYER_WAIT", "HA_URL", "HA_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
    monkeypatch.setattr(cfg, "HA_APP_OPTIONS", str(opts))
    monkeypatch.chdir(tmp_path)
    c = cfg.Config.from_env()
    assert c.plex_url == "http://plex:32400" and c.resume is False and c.player_wait == 30
    assert c.ha_url == "http://supervisor/core" and c.ha_token == "sup" and c.ha_player == ""
