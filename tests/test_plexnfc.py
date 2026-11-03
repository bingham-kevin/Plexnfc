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


def test_unknown_client_lists_account_players(plex):
    plex.server.myPlexAccount.return_value.resources.return_value = [
        NS(name="Den", provides="client,player"), NS(name="Plex Server", provides="server")]
    r = create_app(CONFIG, plex).test_client().get("/m/tmdb-603?client=Bedroom")
    assert b"Den" in r.data and b"Plex Server" not in r.data


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
    monkeypatch.setattr("plexnfc.homeassistant.requests.get",
                        mock.Mock(return_value=mock.Mock(ok=True, status_code=200, json=lambda: {"state": "idle"})))
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
    assert b'href="tags"' in r.data and b'href="/' not in r.data
    # The book opens in a new tab, which ingress won't serve, so it uses the tag address
    assert b'action="http://nfc.local:8080/book" target="_blank"' in r.data


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


def test_sign_in_with_plex(plex, tmp_path, monkeypatch):
    from dataclasses import replace
    from plexnfc import plexauth
    auth = str(tmp_path / "auth.json")
    config = replace(CONFIG, plex_token="", auth_file=auth)
    plex.config = config
    calls = []

    def fake_post(url, params, headers, timeout):
        calls.append(headers["X-Plex-Client-Identifier"])
        return mock.Mock(json=lambda: {"id": 7, "code": "abcd"})

    approved = {"authToken": None}
    monkeypatch.setattr(plexauth.requests, "post", fake_post)
    monkeypatch.setattr(plexauth.requests, "get", lambda *a, **k: mock.Mock(json=lambda: approved))
    app = create_app(config, plex).test_client()

    assert b'href="plex/login"' in app.get("/").data
    r = app.get("/plex/login")
    assert b"app.plex.tv/auth#?clientID=" in r.data and b"code=abcd" in r.data
    assert app.get("/plex/check?id=99").status_code == 404  # not a PIN we asked for
    assert app.get("/plex/check?id=7").json == {"signed_in": False}
    approved["authToken"] = "newtok"
    assert app.get("/plex/check?id=7").json == {"signed_in": True}
    assert config.plex_token == "newtok" and plexauth.saved_token(auth) == "newtok"
    assert plexauth.client_id(auth) == calls[0]  # same device ID every time
    assert b"newtok" not in app.get("/").data and b"Sign out" in app.get("/").data

    app.post("/plex/logout")
    assert config.plex_token == "" and plexauth.saved_token(auth) == ""


def test_not_signed_in_is_reported():
    from dataclasses import replace
    p = Plex(replace(CONFIG, plex_token=""))
    with pytest.raises(PlexNFCError, match="Sign in with Plex"):
        p.find_movie("tmdb-603")


def test_saved_token_used_when_none_configured(tmp_path, monkeypatch):
    import json
    from plexnfc import config as cfg
    (tmp_path / "auth.json").write_text(json.dumps({"client_id": "x", "token": "saved"}))
    for k in ("PLEX_TOKEN",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr(cfg, "HA_APP_OPTIONS", str(tmp_path / "none.json"))
    monkeypatch.chdir(tmp_path)
    assert cfg.Config.from_env().plex_token == "saved"
    monkeypatch.setenv("PLEX_TOKEN", "manual")
    assert cfg.Config.from_env().plex_token == "manual"


@pytest.mark.parametrize("status,state,message", [
    (200, "unavailable", "isn't ready"), (200, "unknown", "isn't ready"), (404, None, "no entity")])
def test_ha_play_plex_refuses_unavailable_player(monkeypatch, status, state, message):
    from plexnfc.homeassistant import HomeAssistant, HomeAssistantError
    get = mock.Mock(return_value=mock.Mock(ok=status == 200, status_code=status, json=lambda: {"state": state}))
    post = mock.Mock()
    monkeypatch.setattr("plexnfc.homeassistant.requests.get", get)
    monkeypatch.setattr("plexnfc.homeassistant.requests.post", post)
    with pytest.raises(HomeAssistantError, match=message):
        HomeAssistant("http://ha", "t").play_plex("media_player.x", 42, True)
    assert get.call_args.args[0] == "http://ha/api/states/media_player.x"
    post.assert_not_called()


def test_ha_playback_reports_player_never_ready(plex):
    import time
    from plexnfc.homeassistant import HomeAssistantError
    plex = ha_plex(plex, ha_player="media_player.plex_apple_tv", player_wait=0)
    plex.ha.play_plex.side_effect = HomeAssistantError("media_player.plex_apple_tv is unavailable")
    app = create_app(plex.config, plex).test_client()
    r = app.get("/m/tmdb-603")
    assert r.status_code == 404 and b"is unavailable" in r.data
    r = app.get("/m/tmdb-603?async=1")
    deadline = time.monotonic() + 5
    while app.get("/now").json["state"] == "starting" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app.get("/now").json["state"] == "error"


def test_now_uses_tag_key_when_session_has_no_guids(plex):
    session = add_session(plex)
    session.guids = []  # what Plex sends for a playing item
    plex.server.fetchItem.return_value = make_movie()
    app = create_app(CONFIG, plex).test_client()
    assert app.get("/now").json["key"] == "tmdb-603"
    assert app.get("/now").json["poster"] == "/poster/tmdb-603"
    plex.server.fetchItem.assert_called_once_with(42)  # looked up once, then cached


def test_async_play_clears_starting_once_session_matches(plex):
    plex.server.sessions.return_value = []
    app = create_app(CONFIG, plex).test_client()
    app.get("/m/tmdb-603?async=1")
    session = add_session(plex)
    session.guids = []
    plex.server.fetchItem.return_value = make_movie()
    assert app.get("/now").json["state"] == "playing"


def test_start_over_rewinds_player_that_resumed(plex, monkeypatch):
    """Plex for Roku ignores the start offset and resumes; plexnfc seeks back to 0."""
    monkeypatch.setattr("plexnfc.plex.time.sleep", lambda s: None)
    plex._movie.viewOffset = 658_000
    plex._client.playMedia.side_effect = lambda *a, **k: add_session(plex, offset=658_000)
    create_app(CONFIG, plex).test_client().get("/m/tmdb-603?resume=0")
    plex._client.seekTo.assert_called_once_with(0)


def test_start_over_leaves_player_that_started_at_zero(plex, monkeypatch):
    monkeypatch.setattr("plexnfc.plex.time.sleep", lambda s: None)
    plex._movie.viewOffset = 658_000
    plex._client.playMedia.side_effect = lambda *a, **k: add_session(plex, offset=1_000)
    create_app(CONFIG, plex).test_client().get("/m/tmdb-603?resume=0")
    plex._client.seekTo.assert_not_called()


def test_resume_does_not_rewind(plex):
    plex._movie.viewOffset = 658_000
    plex._client.playMedia.side_effect = lambda *a, **k: add_session(plex, offset=658_000)
    create_app(CONFIG, plex).test_client().get("/m/tmdb-603?resume=1")
    plex._client.seekTo.assert_not_called()


# ---- Jellyfin ------------------------------------------------------------------

def jf_plex(plex, **cfg):
    from dataclasses import replace
    from plexnfc.jellyfin import Jellyfin
    plex.config = replace(CONFIG, **{"jellyfin_url": "http://jf:8096", "jellyfin_api_key": "k",
                                     "jellyfin_player": "AppleTV", **cfg})
    plex.jellyfin = mock.Mock(spec=Jellyfin)
    plex.jellyfin.find_movie.return_value = {"Id": "abc", "Name": "The Matrix"}
    # Already playing with subtitles off, so the subtitle check finishes at once
    plex.jellyfin.session.return_value = {"Id": "s1", "DeviceName": "AppleTV", "NowPlayingItem": {"Id": "abc"},
                                          "PlayState": {"SubtitleStreamIndex": -1}}
    return plex


def test_jellyfin_play_starts_over_on_jellyfin_player(plex):
    plex = jf_plex(plex)
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603?resume=0")
    assert r.status_code == 200 and b"AppleTV" in r.data
    plex.jellyfin.find_movie.assert_called_once_with("The Matrix", 1999, "tmdb-603")
    plex.jellyfin.play.assert_called_once_with(plex.jellyfin.session.return_value, "abc", start_seconds=0)
    plex._client.playMedia.assert_not_called()


def test_jellyfin_resume_leaves_start_to_the_app(plex):
    plex = jf_plex(plex)
    create_app(plex.config, plex).test_client().get("/m/tmdb-603?resume=1")
    assert plex.jellyfin.play.call_args.kwargs == {"start_seconds": None}


def test_jellyfin_player_not_open_lists_players(plex):
    plex = jf_plex(plex)
    plex.jellyfin.session.return_value = None
    plex.jellyfin.players.return_value = ["Bedroom"]
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert r.status_code == 404 and b"Jellyfin player named" in r.data and b"Bedroom" in r.data


def test_client_param_picks_plex_or_jellyfin(plex):
    plex = jf_plex(plex, jellyfin_player="")
    app = create_app(plex.config, plex).test_client()
    app.get("/m/tmdb-603")  # no Jellyfin player set: Plex as before
    plex._client.playMedia.assert_called_once()
    app.get("/m/tmdb-603?client=jellyfin:AppleTV")
    plex.jellyfin.session.assert_called_with("AppleTV")
    plex.jellyfin.play.assert_called_once()


def test_jellyfin_controls_and_now(plex):
    plex = jf_plex(plex)
    plex.jellyfin.session.return_value = {
        "Id": "s1", "DeviceName": "AppleTV",
        "NowPlayingItem": {"Id": "abc", "Name": "The Matrix", "ProductionYear": 1999,
                           "RunTimeTicks": 7200 * 10_000_000, "ProviderIds": {"Tmdb": "603"}},
        "PlayState": {"PositionTicks": 60 * 10_000_000, "IsPaused": False}}
    app = create_app(plex.config, plex).test_client()
    now = app.get("/now").json
    assert now["state"] == "playing" and now["key"] == "tmdb-603" and now["position"] == 60
    app.get("/toggle")
    plex.jellyfin.command.assert_called_with(plex.jellyfin.session.return_value, "Pause")
    app.get("/stop")
    plex.jellyfin.command.assert_called_with(plex.jellyfin.session.return_value, "Stop")
    app.get("/seek?by=30")
    plex.jellyfin.seek.assert_called_once_with(plex.jellyfin.session.return_value, 90)


def test_jellyfin_without_settings_is_reported(plex):
    from dataclasses import replace
    plex.config = replace(CONFIG, jellyfin_player="AppleTV")
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert r.status_code == 404 and b"JELLYFIN_API_KEY" in r.data


@pytest.mark.parametrize("items,expected", [
    ([{"Id": "old", "Name": "3:10 to Yuma", "ProviderIds": {"Tmdb": "14168"}},
      {"Id": "new", "Name": "3:10 to Yuma", "ProviderIds": {"Tmdb": "5176"}}], "new"),
    ([{"Id": "only", "Name": "3:10 to Yuma", "ProviderIds": {}}], "only"),
])
def test_jellyfin_find_movie_matches_tag_id(monkeypatch, items, expected):
    from plexnfc.jellyfin import Jellyfin
    req = mock.Mock(return_value=mock.Mock(ok=True, status_code=200, content=b"x",
                                           json=lambda: {"Items": items}))
    monkeypatch.setattr("plexnfc.jellyfin.requests.request", req)
    item = Jellyfin("http://jf:8096", "k").find_movie("3:10 to Yuma", 2007, "tmdb-5176")
    assert item["Id"] == expected
    params = req.call_args.kwargs["params"]
    assert params["searchTerm"] == "3:10 to Yuma" and params["years"] == 2007
    assert req.call_args.kwargs["headers"]["Authorization"] == 'MediaBrowser Token="k"'


def test_jellyfin_find_movie_ambiguous(monkeypatch):
    from plexnfc.jellyfin import Jellyfin, JellyfinError
    items = [{"Id": "a", "Name": "Dune", "ProviderIds": {}}, {"Id": "b", "Name": "Dune", "ProviderIds": {}}]
    monkeypatch.setattr("plexnfc.jellyfin.requests.request", mock.Mock(
        return_value=mock.Mock(ok=True, status_code=200, content=b"x", json=lambda: {"Items": items})))
    with pytest.raises(JellyfinError, match="Couldn't find Dune"):
        Jellyfin("http://jf:8096", "k").find_movie("Dune", None, "tmdb-1")


def test_jellyfin_play_sends_session_command(monkeypatch):
    from plexnfc.jellyfin import Jellyfin
    req = mock.Mock(return_value=mock.Mock(ok=True, status_code=204, content=b""))
    monkeypatch.setattr("plexnfc.jellyfin.requests.request", req)
    Jellyfin("http://jf:8096", "k").play({"Id": "s1"}, "abc", start_seconds=0)
    assert req.call_args.args == ("POST", "http://jf:8096/Sessions/s1/Playing")
    assert req.call_args.kwargs["params"] == {"playCommand": "PlayNow", "itemIds": "abc",
                                              "subtitleStreamIndex": -1, "startPositionTicks": 0}


def test_starting_screen_names_jellyfin_player(plex):
    import threading
    plex = jf_plex(plex)
    gate = threading.Event()
    plex.jellyfin.play.side_effect = lambda *a, **k: gate.wait(5)
    app = create_app(plex.config, plex).test_client()
    app.get("/m/tmdb-603?async=1")
    plex.jellyfin.session.return_value = {"Id": "s1", "DeviceName": "AppleTV"}  # nothing playing yet
    assert app.get("/now").json["player"] == "AppleTV"
    gate.set()


def test_started_script_runs_after_play_without_waiting(plex):
    plex = ha_plex(plex, ha_started_script="script.hide_controls")
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    plex._client.playMedia.assert_called_once()
    plex.ha.call_service.assert_called_once_with("script", "turn_on", {"entity_id": "script.hide_controls"})


def test_started_script_not_run_when_play_fails(plex):
    plex = ha_plex(plex, ha_started_script="script.hide_controls")
    create_app(plex.config, plex).test_client().get("/m/tmdb-999")
    plex.ha.call_service.assert_not_called()


def test_jellyfin_turns_on_subtitles_off_once_playing(plex, monkeypatch):
    monkeypatch.setattr("plexnfc.plex.time.sleep", lambda s: None)
    plex = jf_plex(plex)
    idle = {"Id": "s1", "DeviceName": "AppleTV"}
    playing = {**idle, "NowPlayingItem": {"Id": "abc"}, "PlayState": {"SubtitleStreamIndex": 3}}
    plex.jellyfin.session.side_effect = [idle, idle, playing]
    plex.jellyfin.subtitles_off.return_value = True
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    plex.jellyfin.subtitles_off.assert_called_once_with(playing)


def test_jellyfin_leaves_subtitles_already_off(plex, monkeypatch):
    monkeypatch.setattr("plexnfc.plex.time.sleep", lambda s: None)
    plex = jf_plex(plex)
    plex.jellyfin.session.return_value = {"Id": "s1", "DeviceName": "AppleTV", "NowPlayingItem": {"Id": "abc"},
                                          "PlayState": {"SubtitleStreamIndex": -1}}
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    plex.jellyfin.subtitles_off.assert_not_called()


def test_jellyfin_subtitles_off_command(monkeypatch):
    from plexnfc.jellyfin import Jellyfin
    req = mock.Mock(return_value=mock.Mock(ok=True, status_code=204, content=b""))
    monkeypatch.setattr("plexnfc.jellyfin.requests.request", req)
    jf = Jellyfin("http://jf:8096", "k")
    assert not jf.subtitles_off({"Id": "s1", "SupportedCommands": ["DisplayMessage"]})
    req.assert_not_called()
    assert jf.subtitles_off({"Id": "s1", "SupportedCommands": ["SetSubtitleStreamIndex"]})
    assert req.call_args.args == ("POST", "http://jf:8096/Sessions/s1/Command")
    assert req.call_args.kwargs["json"] == {"Name": "SetSubtitleStreamIndex", "Arguments": {"Index": "-1"}}


def test_jellyfin_subtitles_wait_for_the_new_start(plex, monkeypatch):
    """Switching to the movie that's already playing: ignore the old playback's report."""
    monkeypatch.setattr("plexnfc.plex.time.sleep", lambda s: None)
    plex = jf_plex(plex)
    old = {"Id": "s1", "DeviceName": "AppleTV", "NowPlayingItem": {"Id": "abc"},
           "PlayState": {"SubtitleStreamIndex": 3, "PositionTicks": 900 * 10_000_000}}
    new = {**old, "PlayState": {"SubtitleStreamIndex": 3, "PositionTicks": 2 * 10_000_000}}
    plex.jellyfin.session.side_effect = [old, old, new]  # find player, then two checks
    plex.jellyfin.subtitles_off.return_value = True
    create_app(plex.config, plex).test_client().get("/m/tmdb-603?resume=0")
    plex.jellyfin.subtitles_off.assert_called_once_with(new)


def test_player_rejecting_play_is_reported(plex):
    from plexapi.exceptions import NotFound
    plex._client.playMedia.side_effect = NotFound("(404) not_found")
    r = create_app(CONFIG, plex).test_client().get("/m/tmdb-603")
    assert r.status_code == 404 and b"didn&#x27;t accept the movie" in r.data


# ---- Movie Book Apple TV app --------------------------------------------------

def app_plex(plex, **cfg):
    from dataclasses import replace
    plex.config = replace(plex.config, **{"app_player": "Den", **cfg})
    return plex


def test_app_play_leaves_stream_in_inbox_before_script(plex):
    plex = app_plex(ha_plex(plex), app_player="Den")
    order = []
    plex.ha.run_script.side_effect = lambda *a, **k: order.append(("script", plex.app_inbox.next("Den", 0)))
    app = create_app(plex.config, plex).test_client()
    r = app.get("/m/tmdb-603?resume=0")
    assert r.status_code == 200
    command = order[0][1]  # already waiting when the script opened the app
    assert command["action"] == "play" and command["key"] == "tmdb-603" and command["title"] == "The Matrix"
    assert "/video/:/transcode/universal/start.m3u8?" in command["url"] and "X-Plex-Token=tok" in command["url"]
    assert "subtitles=none" in command["url"] and "offset=0" in command["url"]
    plex._client.playMedia.assert_not_called()
    # the app picks it up once, by ID
    inbox = app.get(f"/tv/inbox?player=den&after=0").json
    assert inbox["id"] == command["id"]
    assert app.get(f"/tv/inbox?player=Den&after={command['id']}").status_code == 204


def test_app_play_uses_jellyfin_stream_when_set_up(plex):
    plex = jf_plex(plex, jellyfin_player="")
    plex = app_plex(plex, jellyfin_url="http://jf:8096", jellyfin_api_key="k")
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    url = plex.app_inbox.next("Den", 0)["url"]
    assert url.startswith("http://jf:8096/Videos/abc/master.m3u8?") and "ApiKey=k" in url


def test_app_status_controls_and_now(plex):
    plex = app_plex(plex)
    app = create_app(plex.config, plex).test_client()
    assert app.get("/now").json == {"state": "idle"}
    app.post("/tv/status", json={"player": "Den", "state": "playing", "position": 60, "duration": 7200,
                                 "title": "The Matrix", "key": "tmdb-603"})
    now = app.get("/now").json
    assert now["state"] == "playing" and now["key"] == "tmdb-603" and now["position"] == 60
    app.get("/toggle")
    assert plex.app_inbox.next("Den", 0)["action"] == "pause"
    app.get("/seek?by=30")
    assert plex.app_inbox.next("Den", 0) | {"id": 0} == {"id": 0, "action": "seek", "by": 30}
    app.get("/stop")
    assert plex.app_inbox.next("Den", 0)["action"] == "stop"
    assert app.post("/tv/status", json={}).status_code == 400


def test_app_inbox_expires_old_commands(monkeypatch):
    from plexnfc import tvapp
    inbox = tvapp.AppInbox()
    clock = [1000.0]
    monkeypatch.setattr(tvapp.time, "monotonic", lambda: clock[0])
    inbox.send("Den", "play", url="u")
    clock[0] += tvapp.PLAY_TTL + 1
    assert inbox.next("Den", 0) is None


def test_client_param_picks_app(plex):
    plex = app_plex(plex, app_player="")
    create_app(plex.config, plex).test_client().get("/m/tmdb-603?client=app:Bedroom")
    assert plex.app_inbox.next("Bedroom", 0)["action"] == "play"
