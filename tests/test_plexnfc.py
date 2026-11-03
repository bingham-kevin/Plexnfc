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
    plex.config.player_scripts = {"Nope": "script.nope"}
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


def test_book_has_contents_with_page_numbers():
    from plexnfc.book import meta_line, render_html
    movies = [NS(title=t, year=2000, duration=5_820_000, contentRating="PG", summary="", thumb=None,
                 guids=[NS(id=f"tmdb://{i}")], ratingKey=i) for i, t in enumerate(["Zulu", "Alien", "Heat", "Up", "Big"])]
    html = render_html(NS(config=NS(ha_off_script="")), "http://nfc", movies, "grid", qr=False)
    # cover is page 1, contents page 2, then four posters a page: Big is the fifth, on page 4
    assert html.index("Alien") < html.index("Big") < html.index("Heat")  # contents is alphabetical
    assert '<span class="t">Big</span><span class="dots"></span><span class="n">4</span>' in html
    assert html.count('class="tap"') == 5 + 3  # a tag spot per poster, and pause/resume/stop
    assert meta_line(movies[0]) == "2000 · 1 h 37 m · PG"


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


# ---- Marquee Apple TV app -----------------------------------------------------

@pytest.fixture(autouse=True)
def _pairings_in_tmp(plex, tmp_path):
    """Keep every test's paired Apple TVs in a temporary file."""
    from plexnfc.tvapp import AppPairings
    plex.app_pairings = AppPairings(str(tmp_path / "marquee_devices.json"))


def pair(plex, name="Den") -> dict:
    """Pair an Apple TV the way the app and panel do (once per name); returns its request headers."""
    paired = plex.__dict__.setdefault("_test_pairs", {})
    if name not in paired:
        p = plex.app_pairings.start(name)
        plex.app_pairings.approve(p["pair_id"])
        paired[name] = {"Authorization": f"Bearer {plex.app_pairings.poll(p['pair_id'])['secret']}"}
    return paired[name]


def app_plex(plex, **cfg):
    """Play in Marquee on the APP_PLAYER Apple TV, which is paired."""
    from dataclasses import replace
    plex.config = replace(plex.config, **{"app_player": "Den", **cfg})
    if plex.config.app_player:
        pair(plex, plex.config.app_player)
    return plex


@pytest.fixture(autouse=True)
def requests_get(monkeypatch):
    """Plex's stream decision, which plexnfc asks for before handing out a stream."""
    get = mock.Mock(return_value=mock.Mock(status_code=200))
    monkeypatch.setattr("plexnfc.plex.requests.get", get)
    return get


def test_app_play_leaves_stream_in_inbox_before_script(plex, requests_get):
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
    assert "directStream=0" in command["url"]  # copying MKV video into HLS crashes Plex's converter
    assert "videoResolution=1920x1080" in command["url"]  # 4K conversions stall
    # Plex 1.43 only streams a session it has made a decision for, so that comes first
    decision = requests_get.call_args.args[0]
    assert decision == command["url"].replace("/start.m3u8?", "/decision?")
    plex._client.playMedia.assert_not_called()
    # the app picks it up once, by ID
    headers = pair(plex, "Den")
    inbox = app.get("/tv/inbox?after=0", headers=headers).json
    assert inbox["id"] == command["id"]
    assert app.get(f"/tv/inbox?after={command['id']}", headers=headers).status_code == 204


def test_app_play_uses_jellyfin_stream_when_chosen(plex):
    plex = jf_plex(plex, jellyfin_player="")
    plex = app_plex(plex, jellyfin_url="http://jf:8096", jellyfin_api_key="k", app_stream="jellyfin")
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    url = plex.app_inbox.next("Den", 0)["url"]
    assert url.startswith("http://jf:8096/Videos/abc/master.m3u8?") and "ApiKey=k" in url


def test_app_status_controls_and_now(plex):
    plex = app_plex(plex)
    app = create_app(plex.config, plex).test_client()
    assert app.get("/now").json == {"state": "idle"}
    app.post("/tv/status", headers=pair(plex, "Den"), json={"state": "playing", "position": 60, "duration": 7200,
                                                            "title": "The Matrix", "key": "tmdb-603"})
    now = app.get("/now").json
    assert now["state"] == "playing" and now["key"] == "tmdb-603" and now["position"] == 60
    app.get("/toggle")
    assert plex.app_inbox.next("Den", 0)["action"] == "pause"
    app.get("/seek?by=30")
    assert plex.app_inbox.next("Den", 0) | {"id": 0} == {"id": 0, "action": "seek", "by": 30}
    app.get("/stop")
    assert plex.app_inbox.next("Den", 0)["action"] == "stop"
    assert app.post("/tv/status", json={}).status_code == 401  # not paired


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
    pair(plex, "Bedroom")
    create_app(plex.config, plex).test_client().get("/m/tmdb-603?client=app:Bedroom")
    assert plex.app_inbox.next("Bedroom", 0)["action"] == "play"


def test_app_play_sends_poster_and_details(plex):
    plex._movie.summary = "A hacker learns the truth."
    plex = app_plex(plex)
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    command = plex.app_inbox.next("Den", 0)
    assert command["poster"] == "/poster/tmdb-603?w=600&h=900"
    assert command["year"] == 1999 and command["summary"] == "A hacker learns the truth."


def test_app_stream_is_plex_even_with_jellyfin_set_up(plex):
    plex = jf_plex(plex, jellyfin_player="")
    plex = app_plex(plex, app_stream="")
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert "/video/:/transcode/universal/start.m3u8?" in plex.app_inbox.next("Den", 0)["url"]
    plex.jellyfin.find_movie.assert_not_called()


# ---- Marquee pairing and library ---------------------------------------------

def test_pairing_flow_and_panel(plex):
    app = create_app(CONFIG, plex).test_client()
    started = app.post("/tv/pair", json={"name": "Den"}).json
    assert len(started["code"]) == 6
    assert app.get(f"/tv/pair/{started['pair_id']}").json == {"status": "pending"}
    # the panel lists the request with its code
    page = app.get("/").data
    assert started["code"].encode() in page and b"Den (Apple TV) wants to pair" in page
    # approving from the network port is refused; through the panel (ingress) it works
    approve = f"/tv/pair/{started['pair_id']}/approve"
    assert app.post(approve, environ_base={"REMOTE_ADDR": "192.168.0.50"}).status_code == 403
    assert app.post(approve, environ_base={"REMOTE_ADDR": "172.30.32.2"}).status_code == 200
    # on the host network, ingress comes from the host's own address (the tag address's host)
    from dataclasses import replace
    host_app = create_app(replace(CONFIG, public_base_url="http://127.0.0.2:8080"), plex).test_client()
    again = host_app.post("/tv/pair", json={"name": "Den 2"}).json
    assert host_app.post(f"/tv/pair/{again['pair_id']}/approve",
                         environ_base={"REMOTE_ADDR": "127.0.0.2"}).status_code == 200
    result = app.get(f"/tv/pair/{started['pair_id']}").json
    assert result["status"] == "approved" and result["name"] == "Den"
    assert app.get(f"/tv/pair/{started['pair_id']}").status_code == 404  # the secret is handed out once
    headers = {"Authorization": f"Bearer {result['secret']}"}
    assert app.get("/tv/inbox", headers=headers).status_code == 204
    # removing the Apple TV cuts it off
    device = plex.app_pairings.devices()[0]
    app.post(f"/tv/devices/{device['id']}/remove", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert app.get("/tv/inbox", headers=headers).status_code == 401


def test_approving_names_the_apple_tv_and_keeps_names_unique(plex):
    app = create_app(CONFIG, plex).test_client()
    pair(plex, "Apple TV")
    names = []
    for given in ("Living Room", "", "living room"):
        started = app.post("/tv/pair", json={"name": "Apple TV"}).json
        app.post(f"/tv/pair/{started['pair_id']}/approve", data={"name": given},
                 environ_base={"REMOTE_ADDR": "172.30.32.2"})
        names.append(app.get(f"/tv/pair/{started['pair_id']}").json["name"])
    # blank keeps the Apple TV's own name; a name already taken gets a number
    assert names == ["Living Room", "Apple TV 2", "living room 2"]


def test_panel_renames_a_paired_apple_tv(plex):
    app = create_app(CONFIG, plex).test_client()
    secret = pair(plex, "Apple TV")["Authorization"].removeprefix("Bearer ")
    pair(plex, "Den")
    started = app.post("/tv/pair", json={"name": "iPhone", "kind": "phone"}).json
    app.post(f"/tv/pair/{started['pair_id']}/approve", environ_base={"REMOTE_ADDR": "172.30.32.2"})
    tv, _, phone = plex.app_pairings.devices()
    plex.app_pairings.set_player(phone["id"], "app:Apple TV")
    rename = f"/tv/devices/{tv['id']}/rename"
    assert app.post(rename, data={"name": "Living Room"}, environ_base={"REMOTE_ADDR": "192.168.0.50"}).status_code == 403
    assert app.post(rename, data={"name": " "}, environ_base={"REMOTE_ADDR": "172.30.32.2"}).status_code == 400
    assert app.post(rename, data={"name": "Living  Room"}, environ_base={"REMOTE_ADDR": "172.30.32.2"}).status_code == 200
    # the Apple TV keeps its pairing under the new name, and the phone follows it
    assert plex.app_pairings.device(secret) == "Living Room"
    assert plex.app_pairings.devices()[2]["player"] == "app:Living Room"
    # another device's name gets a number; its own name (even in another case) doesn't
    app.post(rename, data={"name": "den"}, environ_base={"REMOTE_ADDR": "172.30.32.2"})
    assert plex.app_pairings.device(secret) == "den 2"
    app.post(rename, data={"name": "DEN 2"}, environ_base={"REMOTE_ADDR": "172.30.32.2"})
    assert plex.app_pairings.device(secret) == "DEN 2"


def test_each_player_gets_its_own_ready_script(plex):
    plex = ha_plex(plex)
    plex.config.player_scripts = {"app:Living Room": "script.living_room"}
    plex.prepare_tv("app:living room")
    plex.prepare_tv("Living Room")  # the default player gets HA_SCRIPT
    plex.prepare_tv("Bedroom")  # another player with no script of its own wakes nothing
    assert [c.args[0] for c in plex.ha.run_script.call_args_list] == ["script.living_room", plex.config.ha_script]


def test_phone_picks_where_its_taps_go(plex):
    app = create_app(CONFIG, plex).test_client()
    pair(plex, "Den")  # a Marquee Apple TV
    started = app.post("/tv/pair", json={"name": "iPhone", "kind": "phone"}).json
    assert b"iPhone (phone) wants to pair" in app.get("/").data
    app.post(f"/tv/pair/{started['pair_id']}/approve", data={"name": "Kevin's iPhone"},
             environ_base={"REMOTE_ADDR": "172.30.32.2"})
    secret = app.get(f"/tv/pair/{started['pair_id']}").json["secret"]
    phone = {"headers": {"Authorization": f"Bearer {secret}"}, "environ_base": {"REMOTE_ADDR": "192.168.0.60"}}

    players = app.get("/phone/players", **phone).json
    assert players["default"] == "Living Room" and players["selected"] is None
    assert {"id": "app:Den", "name": "Den", "kind": "Marquee"} in players["players"]
    assert {"id": "Living Room", "name": "Living Room", "kind": "Plex"} in players["players"]
    assert app.post("/phone/player", json={"player": "app:Den"}, **phone).json["selected"] == "app:Den"
    assert b"taps go to app:Den" in app.get("/").data

    # a tap from the phone's address goes to its player; other phones get the default
    plex._play_in_app = mock.Mock()
    app.get("/m/tmdb-603", environ_base={"REMOTE_ADDR": "192.168.0.60"})
    assert plex._play_in_app.call_args.args[0] == "Den"
    app.get("/m/tmdb-603", environ_base={"REMOTE_ADDR": "192.168.0.61"})
    plex._client.playMedia.assert_called_once()
    # ?client= on a tag still wins
    app.get("/m/tmdb-603?client=Living Room", environ_base={"REMOTE_ADDR": "192.168.0.60"})
    assert plex._client.playMedia.call_count == 2

    # the phone moved to a new address: its taps follow it, and the old address is free
    app.get("/phone/players", headers=phone["headers"], environ_base={"REMOTE_ADDR": "192.168.0.77"})
    assert plex.app_pairings.player_for("192.168.0.77") == "app:Den"
    assert plex.app_pairings.player_for("192.168.0.60") is None

    # back to the default
    app.post("/phone/player", json={"player": None}, **phone)
    assert plex.app_pairings.player_for("192.168.0.77") is None


def test_phone_api_needs_a_paired_phone(plex):
    app = create_app(CONFIG, plex).test_client()
    tv = pair(plex, "Den")
    assert app.get("/phone/players").status_code == 401
    assert app.get("/phone/players", headers=tv).status_code == 401  # an Apple TV isn't a phone
    started = app.post("/tv/pair", json={"name": "iPad", "kind": "phone"}).json
    plex.app_pairings.approve(started["pair_id"])
    phone = {"Authorization": f"Bearer {plex.app_pairings.poll(started['pair_id'])['secret']}"}
    assert app.get("/tv/inbox", headers=phone).status_code == 401  # and a phone isn't an Apple TV
    assert app.post("/phone/player", json={"player": 5}, headers=phone).status_code == 400


def test_player_scripts_setting():
    from plexnfc.config import parse_player_scripts
    assert parse_player_scripts("app:Living Room = script.lr; Den TV=script.den;; junk") == {
        "app:Living Room": "script.lr", "Den TV": "script.den"}


def test_pairings_survive_restart_and_store_only_a_hash(plex, tmp_path):
    from plexnfc.tvapp import AppPairings
    headers = pair(plex, "Den")
    secret = headers["Authorization"].removeprefix("Bearer ")
    saved = (tmp_path / "marquee_devices.json").read_text()
    assert secret not in saved
    assert AppPairings(str(tmp_path / "marquee_devices.json")).device(secret) == "Den"


def test_app_api_needs_pairing(plex):
    app = create_app(CONFIG, plex).test_client()
    for path in ("/tv/inbox", "/tv/library", "/tv/movies", "/tv/search?q=matrix"):
        assert app.get(path).status_code == 401
        assert app.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert app.post("/tv/play", json={"key": "tmdb-603"}).status_code == 401


def test_app_library_search_and_play(plex):
    movie = plex._movie
    movie.duration = 8_160_000
    section = plex.server.library.section.return_value
    section.recentlyAdded.return_value = [movie]
    section.collections.return_value = [mock.Mock(title="Poster Book", items=lambda: [movie])]
    section.search.return_value = [movie]
    section.totalSize = 1
    app = create_app(CONFIG, plex).test_client()
    headers = pair(plex, "Den")
    rows = app.get("/tv/library", headers=headers).json["rows"]
    assert [r["title"] for r in rows] == ["Recently Added", "Poster Book"]
    assert rows[0]["items"][0] == {"key": "tmdb-603", "title": "The Matrix", "year": 1999, "summary": "",
                                   "duration": 8160, "poster": "/poster/tmdb-603?w=400&h=600"}
    page = app.get("/tv/movies?start=0&size=500", headers=headers).json
    assert page["total"] == 1 and len(page["items"]) == 1
    assert section.search.call_args.kwargs["container_size"] == 200  # capped
    assert app.get("/tv/search?q=matrix", headers=headers).json["items"][0]["key"] == "tmdb-603"
    played = app.post("/tv/play", headers=headers, json={"key": "tmdb-603", "resume": False}).json
    assert "/video/:/transcode/universal/start.m3u8?" in played["url"] and played["title"] == "The Matrix"
    assert "X-Plex-Client-Identifier=moviebook-Den" in played["url"]
    assert app.post("/tv/play", headers=headers, json={"key": "nope"}).status_code == 404


# ---- opening Marquee on Apple TVs without Home Assistant -------------------------------

def test_app_play_opens_marquee_on_paired_apple_tv_instead_of_script(plex):
    from plexnfc.appletv import AppleTVError
    plex = app_plex(ha_plex(plex), app_player="Den")
    plex.apple_tvs = mock.Mock()
    plex.apple_tvs.open_marquee.side_effect = lambda name: order.append(plex.app_inbox.next(name, 0))
    order = []
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert r.status_code == 200
    assert order[0]["key"] == "tmdb-603"  # the movie was waiting when Marquee opened
    plex.apple_tvs.open_marquee.assert_called_once_with("Den")
    plex.ha.run_script.assert_not_called()
    # a TV that doesn't answer doesn't stop the tap: the movie waits in the inbox
    plex.apple_tvs.open_marquee.side_effect = AppleTVError("The Apple TV didn't answer")
    assert plex.prepare_tv("app:Den") == "The Apple TV didn't answer"


def test_unpaired_app_player_still_runs_script(plex):
    plex = app_plex(ha_plex(plex), app_player="Den")
    plex.apple_tvs = mock.Mock()
    plex.apple_tvs.link_for.return_value = None
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    plex.apple_tvs.open_marquee.assert_not_called()
    plex.ha.run_script.assert_called_once()


def fake_pyatv(identifier="ATV1", address="192.168.0.30"):
    config = mock.Mock(identifier=identifier, address=address, all_identifiers=[identifier, "AA:BB"])
    config.name = "Living Room"
    config.device_info.raw_model = "AppleTV14,1"
    pairing = mock.Mock(has_paired=True, service=mock.Mock(credentials="creds"))
    pairing.begin = mock.AsyncMock()
    pairing.finish = mock.AsyncMock()
    pairing.close = mock.AsyncMock()
    atv = mock.Mock()
    atv.power.turn_on = mock.AsyncMock()
    atv.apps.launch_app = mock.AsyncMock()
    atv.close.return_value = set()
    fake = mock.Mock(scan=mock.AsyncMock(return_value=[config]), pair=mock.AsyncMock(return_value=pairing),
                     connect=mock.AsyncMock(return_value=atv))
    return fake, config, pairing, atv


def test_apple_tv_pairing_and_opening_marquee(tmp_path, monkeypatch):
    pytest.importorskip("pyatv")
    from plexnfc import appletv
    fake, config, pairing, atv = fake_pyatv()
    monkeypatch.setattr(appletv, "pyatv", fake)
    homepod = mock.Mock(identifier="HP1", address="192.168.0.6", all_identifiers=["HP1"])
    homepod.device_info.raw_model = "AudioAccessory1,1"
    fake.scan.return_value = [config, homepod]
    tvs = appletv.AppleTVs(str(tmp_path / "apple_tvs.json"))
    assert tvs.scan() == [{"identifier": "ATV1", "name": "Living Room", "address": "192.168.0.30", "linked": None}]
    pair_id = tvs.start_pairing("ATV1", "  Den ")
    pairing.begin.assert_awaited_once()
    assert tvs.finish_pairing(pair_id, "1234") == "Den"
    pairing.pin.assert_called_once_with(1234)
    with pytest.raises(appletv.AppleTVError):
        tvs.finish_pairing(pair_id, "1234")  # used up
    # saved without leaking credentials to the panel, and kept across restarts
    assert tvs.linked() == {"Den": {"identifier": "ATV1", "identifiers": ["ATV1", "AA:BB"], "name": "Living Room",
                                    "address": "192.168.0.30"}}
    tvs = appletv.AppleTVs(str(tmp_path / "apple_tvs.json"))
    # asked by address, pyatv names the same Apple TV by another of its IDs
    config.identifier = "AA:BB"
    assert tvs.scan()[0]["linked"] == "Den"
    tvs.open_marquee("den")
    config.set_credentials.assert_called_once()
    assert config.set_credentials.call_args.args[1] == "creds"
    atv.power.turn_on.assert_awaited_once()
    atv.apps.launch_app.assert_awaited_once_with("com.binghamkevin.moviebook")
    tvs.remove("Den")
    with pytest.raises(appletv.AppleTVError):
        tvs.open_marquee("Den")


def test_apple_tv_wrong_pin(tmp_path, monkeypatch):
    pytest.importorskip("pyatv")
    from plexnfc import appletv
    fake, _, pairing, _ = fake_pyatv()
    pairing.has_paired = False
    monkeypatch.setattr(appletv, "pyatv", fake)
    tvs = appletv.AppleTVs(str(tmp_path / "apple_tvs.json"))
    with pytest.raises(appletv.AppleTVError, match="PIN"):
        tvs.finish_pairing(tvs.start_pairing("ATV1", "Den"), "9999")
    pairing.close.assert_awaited_once()
    assert tvs.linked() == {}


def test_apple_tv_routes_are_panel_only(plex):
    plex.apple_tvs = mock.Mock(available=True)
    plex.apple_tvs.scan.return_value = [{"identifier": "ATV1", "name": "Living Room",
                                         "address": "192.168.0.30", "linked": None}]
    plex.apple_tvs.linked.return_value = {}
    plex.apple_tvs.link_for.return_value = {"identifier": "ATV1", "name": "Living Room", "address": "192.168.0.30"}
    app = create_app(CONFIG, plex).test_client()
    outside = {"REMOTE_ADDR": "192.168.0.50"}
    assert app.get("/appletv/scan", environ_base=outside).status_code == 403
    assert app.post("/appletv/pair", data={"identifier": "ATV1", "player": "Den"}, environ_base=outside).status_code == 403
    assert app.post("/appletv/open", data={"player": "Den"}, environ_base=outside).status_code == 403
    assert b"Living Room" in app.get("/appletv/scan").data
    plex.apple_tvs.start_pairing.return_value = "abc"
    assert b'action="pair/abc"' in app.post("/appletv/pair", data={"identifier": "ATV1", "player": "Den"}).data
    plex.apple_tvs.finish_pairing.return_value = "Den"
    assert b"app:Den" in app.post("/appletv/pair/abc", data={"pin": "1234"}).data
    assert b"Find Apple TVs" in app.get("/").data


# ---- finding the Plex server from the signed-in account ------------------------------

def test_no_plex_url_uses_the_accounts_own_server(monkeypatch):
    from dataclasses import replace
    from plexnfc import plex as plex_module
    shared = mock.Mock(provides="server", owned=False)
    shared.name = "Friend's Plex"
    own = mock.Mock(provides="server", owned=True)
    own.name = "Plex"
    own.connect.return_value = mock.Mock(_baseurl="https://192-168-0-142.abc.plex.direct:32400")
    player = mock.Mock(provides="player,pubsub-player")
    account = mock.Mock()
    account.resources.return_value = [player, shared, own]
    monkeypatch.setattr(plex_module, "MyPlexAccount", mock.Mock(return_value=account))
    p = Plex(replace(CONFIG, plex_url=""))
    assert p.server is own.connect.return_value
    assert p.plex_url == "https://192-168-0-142.abc.plex.direct:32400"
    shared.connect.assert_not_called()
    # PLEX_SERVER picks another by name
    p = Plex(replace(CONFIG, plex_url="", plex_server="friend's plex"))
    assert p.server is shared.connect.return_value
    with pytest.raises(PlexNFCError, match="no server named Den"):
        _ = Plex(replace(CONFIG, plex_url="", plex_server="Den")).server


def test_config_without_plex_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLEX_URL", raising=False)
    monkeypatch.setattr("plexnfc.config.HA_APP_OPTIONS", str(tmp_path / "none.json"))
    assert Config.from_env().plex_url == ""


# ---- a .local tag address that follows this machine ----------------------------------

def test_local_name_follows_the_address(monkeypatch):
    import socket
    import threading
    from plexnfc import discovery
    addresses = iter(["192.168.0.20", "192.168.0.20", "192.168.0.31"])
    monkeypatch.setattr(discovery, "lan_address", lambda: next(addresses))
    zc, info, stop = mock.Mock(), mock.Mock(server="marquee.local."), threading.Event()
    zc.update_service.side_effect = lambda _: stop.set() if info.addresses == [socket.inet_aton("192.168.0.31")] else None
    discovery._follow_address(zc, info, "192.168.0.20", every=0, stop=stop)
    zc.update_service.assert_called_once_with(info)
    assert info.addresses == [socket.inet_aton("192.168.0.31")]


def test_advertise_answers_for_a_local_name(monkeypatch):
    import zeroconf
    from plexnfc import discovery
    monkeypatch.setattr(discovery, "lan_address", lambda: "192.168.0.20")
    monkeypatch.setattr(discovery.threading, "Thread", mock.Mock())
    zc = mock.Mock()
    monkeypatch.setattr(zeroconf, "Zeroconf", mock.Mock(return_value=zc))
    discovery.advertise("http://marquee.local:8080")
    info = zc.register_service.call_args.args[0]
    assert info.server == "marquee.local." and info.port == 8080
    assert info.properties[b"url"] == b"http://marquee.local:8080"


def test_app_play_to_unpaired_marquee_says_so(plex):
    from dataclasses import replace
    plex.config = replace(plex.config, app_player="Den")  # Marquee on Den never paired with this server
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert r.status_code != 200 and b"isn&#x27;t connected to this server" in r.data
    assert plex.app_inbox.next("Den", 0) is None


def test_plex_server_reports_the_server_in_use(plex):
    plex.server.friendlyName = "Plex"
    app = create_app(CONFIG, plex).test_client()
    assert app.get("/plex/server").json == {"name": "Plex", "url": "http://plex:32400"}
    plex.set_token("")
    assert "Not signed in" in app.get("/plex/server").json["error"]


def test_app_player_can_be_named_after_its_apple_tv(plex):
    from dataclasses import replace
    plex.config = replace(plex.config, app_player="den apple tv")  # what the Apple TV calls itself
    plex.apple_tvs = mock.Mock()
    plex.apple_tvs.linked.return_value = {"Den": {"identifier": "ATV1", "name": "Den Apple TV"}}
    pair(plex, "Den")
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert r.status_code == 200
    assert plex.app_inbox.next("Den", 0)["action"] == "play"
    plex.apple_tvs.open_marquee.assert_called_once_with("Den")


# ---- Marquee pairing itself on an Apple TV plexnfc paired with ------------------------

def test_expected_apple_tv_pairs_marquee_without_a_code(plex):
    app = create_app(CONFIG, plex).test_client()
    tv, other = {"REMOTE_ADDR": "192.168.0.34"}, {"REMOTE_ADDR": "192.168.0.50"}
    old = pair(plex, "Den")  # Marquee paired before, e.g. with its old name
    assert app.get("/tv/expected", environ_base=tv).status_code == 204
    plex.app_pairings.expect("192.168.0.34", "Den")
    assert app.get("/tv/expected", environ_base=other).status_code == 204
    assert app.get("/tv/expected", environ_base=tv).json == {"name": "Den"}
    started = app.post("/tv/pair", json={"name": "Apple TV"}, environ_base=tv).json
    result = app.get(f"/tv/pair/{started['pair_id']}").json
    assert result["status"] == "approved" and result["name"] == "Den"  # takes the name over, no "Den 2"
    assert [d["name"] for d in plex.app_pairings.devices()] == ["Den"]
    assert app.get("/tv/inbox", headers=old).status_code == 401
    # once only, and never for phones or other addresses
    again = app.post("/tv/pair", json={"name": "Apple TV"}, environ_base=tv).json
    assert app.get(f"/tv/pair/{again['pair_id']}").json == {"status": "pending"}
    plex.app_pairings.expect("192.168.0.34", "Den")
    phone = app.post("/tv/pair", json={"name": "iPhone", "kind": "phone"}, environ_base=tv).json
    assert app.get(f"/tv/pair/{phone['pair_id']}").json == {"status": "pending"}
    stranger = app.post("/tv/pair", json={"name": "Apple TV"}, environ_base=other).json
    assert app.get(f"/tv/pair/{stranger['pair_id']}").json == {"status": "pending"}


def test_pairing_an_apple_tv_expects_its_marquee(plex):
    plex.apple_tvs = mock.Mock(available=True)
    plex.apple_tvs.finish_pairing.return_value = "Den"
    plex.apple_tvs.link_for.return_value = {"identifier": "ATV1", "name": "Den Apple TV", "address": "192.168.0.34"}
    app = create_app(CONFIG, plex).test_client()
    assert b"is connecting to this server as <b>Den</b>" in app.post("/appletv/pair/abc", data={"pin": "1234"}).data
    plex.apple_tvs.open_marquee.assert_called_once_with("Den")
    assert plex.app_pairings.expected("192.168.0.34") == "Den"
    # Connect Marquee does the same for an Apple TV paired already; panel only
    assert app.post("/appletv/connect", data={"player": "Den"},
                    environ_base={"REMOTE_ADDR": "192.168.0.50"}).status_code == 403
    assert app.post("/appletv/connect", data={"player": "Den"}).status_code == 200
    plex.apple_tvs.link_for.return_value = None
    assert app.post("/appletv/connect", data={"player": "Nope"}).status_code == 404
