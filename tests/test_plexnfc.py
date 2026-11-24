import json
import threading
import time
from types import SimpleNamespace as NS
from unittest import mock

import pytest
from plexapi.exceptions import NotFound

from plexnfc.cards import Cards, normalize_uid
from plexnfc.config import Config
from plexnfc.plex import Plex, PlexNFCError, movie_key
from plexnfc.sources import Library
from plexnfc.server import create_app

CONFIG = Config("http://plex:32400", "tok", "Movies", "Living Room", "http://nfc.local:8080", True)


def make_movie(title="The Matrix", guids=("imdb://tt0133093", "tmdb://603"), offset=0):
    return NS(title=title, year=1999, ratingKey=42, thumb="/t.jpg", viewOffset=offset,
              guids=[NS(id=g) for g in guids])


@pytest.fixture
def plex(tmp_path):
    movie = make_movie()
    section = mock.Mock()
    section.getGuid.side_effect = lambda g: movie if g in [x.id for x in movie.guids] else (_ for _ in ()).throw(NotFound())
    section.all.return_value = [movie]
    server = mock.Mock()
    server.library.section.return_value = section
    from dataclasses import replace
    # A copy: signing out (set_token) changes the config, which mustn't reach other tests
    p = Plex(replace(CONFIG), server=server)
    p.cards = Cards(str(tmp_path / "movie_cards.json"))
    p.library = Library(p, str(tmp_path))
    p._movie = movie
    return p


def test_movie_key_prefers_tmdb():
    assert movie_key(make_movie()) == "tmdb-603"
    assert movie_key(make_movie(guids=["imdb://tt1"])) == "imdb-tt1"
    assert movie_key(make_movie(guids=[])) == "rk-42"


def test_play_sends_movie_to_marquee(plex):
    pair(plex, "Living Room")
    app = create_app(CONFIG, plex).test_client()
    r = app.get("/m/tmdb-603")
    assert r.status_code == 200 and b"The Matrix" in r.data
    command = plex.app_inbox.next("Living Room", 0)
    assert command["action"] == "play" and command["key"] == "tmdb-603" and "offset=0" in command["url"]


def test_taps_start_from_the_beginning(plex):
    pair(plex, "Living Room")
    plex._movie.viewOffset = 5000
    app = create_app(CONFIG, plex).test_client()
    app.get("/m/tmdb-603")
    assert "offset=0" in plex.app_inbox.next("Living Room", 0)["url"]
    app.get("/m/tmdb-603?resume=1")  # unless the tag asks
    assert "offset=5" in plex.app_inbox.next("Living Room", 0)["url"]


def test_no_player_chosen_says_so(plex):
    from dataclasses import replace
    plex.config = replace(CONFIG, app_player="")
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert r.status_code == 404 and b"No Marquee player chosen" in r.data


def test_unknown_movie_and_bad_key(plex):
    app = create_app(CONFIG, plex).test_client()
    assert app.get("/m/tmdb-999").status_code == 404
    assert app.get("/m/garbage").status_code == 404

def test_controls(plex):
    pair(plex, "Living Room")
    app = create_app(CONFIG, plex).test_client()
    assert app.get("/pause").status_code == 200
    assert plex.app_inbox.next("Living Room", 0)["action"] == "pause"


def test_tags_page(plex):
    r = create_app(CONFIG, plex).test_client().get("/tags")
    assert b"http://nfc.local:8080/m/tmdb-603" in r.data


def test_bad_key_rejected():
    with pytest.raises(PlexNFCError):
        Plex(CONFIG, server=mock.Mock()).find_movie("../etc")


def ha_plex(plex, **overrides):
    from dataclasses import replace
    settings = dict(ha_url="http://ha", ha_token="t", ha_script="script.movie_night",
                    ha_off_script="script.tv_off")
    plex.config = replace(CONFIG, **{**settings, **overrides})
    plex.ha = mock.Mock()
    return plex


def test_home_assistant_script_runs_after_the_movie_is_waiting(plex):
    plex = ha_plex(plex)
    pair(plex, "Living Room")
    order = []
    plex.ha.run_script.side_effect = lambda s: order.append((s, plex.app_inbox.next("Living Room", 0)["action"]))
    assert create_app(plex.config, plex).test_client().get("/m/tmdb-603").status_code == 200
    assert order == [("script.movie_night", "play")]


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

def test_now_idle(plex):
    assert create_app(CONFIG, plex).test_client().get("/now").json == {"state": "idle"}


def test_async_play_reports_starting_then_playing(plex):
    headers = pair(plex, "Living Room")
    gate = threading.Event()
    stream = plex._app_stream
    plex._app_stream = lambda *a: (gate.wait(5), stream(*a))[1]
    app = create_app(CONFIG, plex).test_client()
    r = app.get("/m/tmdb-603?async=1")
    assert r.status_code == 202
    now = app.get("/now").json
    assert now["state"] == "starting" and now["poster"] == "/poster/tmdb-603" and now["player"] == "Living Room"
    gate.set()
    app.post("/tv/status", headers=headers, json={"state": "playing", "position": 1, "duration": 7200,
                                                   "title": "The Matrix", "key": "tmdb-603"})
    assert app.get("/now").json["state"] == "playing"


def test_async_play_error_is_reported(plex):
    import time as _t
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


def test_book_pages_fit_the_paper_whatever_the_window():
    from plexnfc.book import render_html
    movies = [NS(title="Up", year=2009, duration=0, summary="", thumb=None, guids=[NS(id="tmdb://14160")])]
    for paper, size in (("letter", "8.5in 11in"), ("A4", "210mm 297mm"), ("nonsense", "8.5in 11in")):
        html = render_html(NS(config=NS(ha_off_script="")), "http://nfc", movies, page_size=paper)
        # never sized from the window (Safari prints with the window's height), and inside a printable margin
        assert "vh" not in html.split("<style>")[1].split("</style>")[0].replace("overflow", "")
        assert f"@page {{ size: {size}; margin: 0.25in; }}" in html
        assert "section:last-child { page-break-after: auto" in html  # no blank page at the end


def test_tags_csv(plex):
    r = create_app(CONFIG, plex).test_client().get("/tags.csv")
    assert b"The Matrix,1999,http://nfc.local:8080/m/tmdb-603" in r.data


def test_control_panel_is_served_with_relative_addresses(plex):
    app = create_app(CONFIG, plex).test_client()
    page = app.get("/").data
    # relative, so it works under any address
    assert b'src="./assets/' in page and b'"/assets' not in page
    script = page.split(b'src="./', 1)[1].split(b'"', 1)[0].decode()
    assert app.get(f"/{script}").status_code == 200
    overview = app.get("/panel/api/overview").json
    assert overview["tag_address"] == "http://nfc.local:8080" and overview["default_player_label"] == "Marquee on Living Room"
    # the tag list, and trying a tag from the panel
    movies = app.get("/panel/api/movies").json
    assert movies["movies"] == [{"key": "tmdb-603", "title": "The Matrix", "year": 1999,
                                 "url": "http://nfc.local:8080/m/tmdb-603", "cards": [], "source": "Plex", "sources": ["Plex"], "files": []}]
    assert [c["action"] for c in movies["controls"]] == ["pause", "play", "stop"]
    assert app.post("/panel/api/play", json={"key": "tmdb-603"}, environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    with mock.patch("plexnfc.server.threading.Thread") as thread:
        assert app.post("/panel/api/play", json={"key": "tmdb-603", "player": "Bedroom"}).json["title"] == "The Matrix"
    assert thread.call_args.kwargs["args"] == ("tmdb-603", "Bedroom", False)
    pair(plex, "Living Room")
    assert app.post("/panel/api/control", json={"action": "pause"}).json["ok"]
    assert plex.app_inbox.next("Living Room", 0)["action"] == "pause"

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

    assert app.get("/panel/api/overview").json["plex"] == {"signed_in": False, "signed_in_here": False}
    r = app.get("/plex/login")
    assert b"app.plex.tv/auth#?clientID=" in r.data and b"code=abcd" in r.data
    assert app.get("/plex/check?id=99").status_code == 404  # not a PIN we asked for
    assert app.get("/plex/check?id=7").json == {"signed_in": False}
    approved["authToken"] = "newtok"
    assert app.get("/plex/check?id=7").json == {"signed_in": True}
    assert config.plex_token == "newtok" and plexauth.saved_token(auth) == "newtok"
    assert plexauth.client_id(auth) == calls[0]  # same device ID every time
    overview = app.get("/panel/api/overview")
    assert b"newtok" not in overview.data and overview.json["plex"]["signed_in_here"]

    assert app.post("/panel/api/plex/logout", environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    app.post("/panel/api/plex/logout")
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
    monkeypatch.chdir(tmp_path)
    assert cfg.Config.from_env().plex_token == "saved"
    monkeypatch.setenv("PLEX_TOKEN", "manual")
    assert cfg.Config.from_env().plex_token == "manual"


def test_started_script_runs_after_play_without_waiting(plex):
    plex = ha_plex(plex, ha_started_script="script.hide_controls")
    pair(plex, "Living Room")
    create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert plex.app_inbox.next("Living Room", 0)["action"] == "play"
    plex.ha.call_service.assert_called_once_with("script", "turn_on", {"entity_id": "script.hide_controls"})


def test_started_script_not_run_when_play_fails(plex):
    plex = ha_plex(plex, ha_started_script="script.hide_controls")
    create_app(plex.config, plex).test_client().get("/m/tmdb-999")
    plex.ha.call_service.assert_not_called()

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
    # the app picks it up once, by ID
    headers = pair(plex, "Den")
    inbox = app.get("/tv/inbox?after=0", headers=headers).json
    assert inbox["id"] == command["id"]
    assert app.get(f"/tv/inbox?after={command['id']}", headers=headers).status_code == 204

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

# ---- Marquee pairing and library ---------------------------------------------

def test_pairing_flow_and_panel(plex):
    app = create_app(CONFIG, plex).test_client()
    started = app.post("/tv/pair", json={"name": "Den"}).json
    assert len(started["code"]) == 6
    assert app.get(f"/tv/pair/{started['pair_id']}").json == {"status": "pending"}
    # the panel lists the request with its code
    assert app.get("/panel/api/overview").json["pairing_requests"] == [
        {"pair_id": started["pair_id"], "name": "Den", "kind": "tv", "code": started["code"]}]
    # approving from another device is refused; from the panel on this machine it works
    approve = f"/panel/api/pairings/{started['pair_id']}/approve"
    assert app.post(approve, json={}, environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    assert app.post(approve, json={}, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200
    assert app.post("/panel/api/pairings/nope/approve", json={}).status_code == 404
    # this machine by the tag address's host counts too
    from dataclasses import replace
    host_app = create_app(replace(CONFIG, public_base_url="http://127.0.0.2:8080"), plex).test_client()
    again = host_app.post("/tv/pair", json={"name": "Den 2"}).json
    assert host_app.post(f"/panel/api/pairings/{again['pair_id']}/approve", json={},
                         environ_base={"REMOTE_ADDR": "127.0.0.2"}).status_code == 200
    # and so does this machine by its network address (the panel opened at marquee.local on the Mac)
    own = own_network_address()
    if own:
        third = app.post("/tv/pair", json={"name": "Den 3"}).json
        assert app.get("/panel/api/overview", environ_base={"REMOTE_ADDR": own}).json["can_manage"]
        assert app.post(f"/panel/api/pairings/{third['pair_id']}/approve", json={},
                        environ_base={"REMOTE_ADDR": f"::ffff:{own}"}).status_code == 200
    result = app.get(f"/tv/pair/{started['pair_id']}").json
    assert result["status"] == "approved" and result["name"] == "Den"
    assert app.get(f"/tv/pair/{started['pair_id']}").status_code == 404  # the secret is handed out once
    headers = {"Authorization": f"Bearer {result['secret']}"}
    assert app.get("/tv/inbox", headers=headers).status_code == 204
    # removing the Apple TV cuts it off
    device = plex.app_pairings.devices()[0]
    app.post(f"/panel/api/devices/{device['id']}/remove", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert app.get("/tv/inbox", headers=headers).status_code == 401


def test_approving_names_the_apple_tv_and_keeps_names_unique(plex):
    app = create_app(CONFIG, plex).test_client()
    pair(plex, "Apple TV")
    names = []
    for given in ("Living Room", "", "living room"):
        started = app.post("/tv/pair", json={"name": "Apple TV"}).json
        app.post(f"/panel/api/pairings/{started['pair_id']}/approve", json={"name": given},
                 environ_base={"REMOTE_ADDR": "127.0.0.1"})
        names.append(app.get(f"/tv/pair/{started['pair_id']}").json["name"])
    # blank keeps the Apple TV's own name; a name already taken gets a number
    assert names == ["Living Room", "Apple TV 2", "living room 2"]


def test_panel_renames_a_paired_apple_tv(plex):
    app = create_app(CONFIG, plex).test_client()
    secret = pair(plex, "Apple TV")["Authorization"].removeprefix("Bearer ")
    pair(plex, "Den")
    started = app.post("/tv/pair", json={"name": "iPhone", "kind": "phone"}).json
    app.post(f"/panel/api/pairings/{started['pair_id']}/approve", json={}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    tv, _, phone = plex.app_pairings.devices()
    plex.app_pairings.set_player(phone["id"], "app:Apple TV")
    rename = f"/panel/api/devices/{tv['id']}/rename"
    assert app.post(rename, json={"name": "Living Room"}, environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    assert app.post(rename, json={"name": " "}, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 400
    assert app.post(rename, json={"name": "Living  Room"}, environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200
    # the Apple TV keeps its pairing under the new name, and the phone follows it
    assert plex.app_pairings.device(secret) == "Living Room"
    assert plex.app_pairings.devices()[2]["player"] == "app:Living Room"
    # another device's name gets a number; its own name (even in another case) doesn't
    app.post(rename, json={"name": "den"}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert plex.app_pairings.device(secret) == "den 2"
    app.post(rename, json={"name": "DEN 2"}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert plex.app_pairings.device(secret) == "DEN 2"


def test_each_player_gets_its_own_ready_script(plex):
    plex = ha_plex(plex)
    plex.config.player_scripts = {"app:Den": "script.den"}
    plex.prepare_tv("app:den")
    plex.prepare_tv("Living Room")  # the default player gets HA_SCRIPT
    plex.prepare_tv("Bedroom")  # another player with no script of its own wakes nothing
    assert [c.args[0] for c in plex.ha.run_script.call_args_list] == ["script.den", plex.config.ha_script]


def test_phone_picks_where_its_taps_go(plex):
    app = create_app(CONFIG, plex).test_client()
    pair(plex, "Den")  # a Marquee Apple TV
    started = app.post("/tv/pair", json={"name": "iPhone", "kind": "phone"}).json
    assert app.get("/panel/api/overview").json["pairing_requests"][0]["kind"] == "phone"
    app.post(f"/panel/api/pairings/{started['pair_id']}/approve", json={"name": "Kevin's iPhone"},
             environ_base={"REMOTE_ADDR": "127.0.0.1"})
    secret = app.get(f"/tv/pair/{started['pair_id']}").json["secret"]
    phone = {"headers": {"Authorization": f"Bearer {secret}"}, "environ_base": {"REMOTE_ADDR": "192.168.0.60"}}

    players = app.get("/phone/players", **phone).json
    assert players["default"] == "app:Living Room" and players["selected"] is None
    assert players["players"] == [{"id": "app:Den", "name": "Den", "kind": "Marquee"}]
    assert app.post("/phone/player", json={"player": "app:Den"}, **phone).json["selected"] == "app:Den"
    assert app.get("/panel/api/overview").json["devices"][1]["player"] == "app:Den"

    # a tap from the phone's address goes to its player; other phones get the default
    plex._play_in_app = mock.Mock()
    app.get("/m/tmdb-603", environ_base={"REMOTE_ADDR": "192.168.0.60"})
    assert plex._play_in_app.call_args.args[0] == "Den"
    app.get("/m/tmdb-603", environ_base={"REMOTE_ADDR": "192.168.0.61"})
    assert plex._play_in_app.call_args.args[0] == "Living Room"
    # ?client= on a tag still wins
    app.get("/m/tmdb-603?client=Bedroom", environ_base={"REMOTE_ADDR": "192.168.0.60"})
    assert plex._play_in_app.call_args.args[0] == "Bedroom"

    # the phone moved to a new address: its taps follow it, and the old address is free
    app.get("/phone/players", headers=phone["headers"], environ_base={"REMOTE_ADDR": "192.168.0.77"})
    assert plex.app_pairings.player_for("192.168.0.77") == "app:Den"
    assert plex.app_pairings.player_for("192.168.0.60") is None

    # back to the default
    app.post("/phone/player", json={"player": None}, **phone)
    assert plex.app_pairings.player_for("192.168.0.77") is None


def test_marquee_on_a_phone_plays_itself_or_where_its_chosen(plex):
    app = create_app(CONFIG, plex).test_client()
    pair(plex, "Den")  # a Marquee Apple TV
    phone = {"headers": pair(plex, "Kevin's iPhone"), "environ_base": {"REMOTE_ADDR": "192.168.0.60"}}

    players = app.get("/tv/players", **phone).json
    assert players["selected"] is None and players["name"] == "Kevin's iPhone"
    ids = [p["id"] for p in players["players"]]
    assert "app:Den" in ids and "app:Kevin's iPhone" not in ids  # itself is "this iPhone"

    # its taps play on itself, with a way back to Marquee from Safari
    plex._play_in_app = mock.Mock()
    page = app.get("/m/tmdb-603", environ_base={"REMOTE_ADDR": "192.168.0.60"}).data
    assert plex._play_in_app.call_args.args[0] == "Kevin's iPhone"
    assert b"moviebook://open" in page
    # until it chooses another player
    assert app.post("/tv/player", json={"player": "app:Den"}, **phone).json["selected"] == "app:Den"
    page = app.get("/m/tmdb-603", environ_base={"REMOTE_ADDR": "192.168.0.60"}).data
    assert plex._play_in_app.call_args.args[0] == "Den" and b"moviebook://" not in page
    # Apple TVs never noted an address, so no one else's taps change
    app.get("/m/tmdb-603", environ_base={"REMOTE_ADDR": "192.168.0.61"})
    assert plex._play_in_app.call_args.args[0] == "Living Room"

    # a scanned code starts on the chosen player in the background
    with mock.patch("plexnfc.server.threading.Thread") as thread:
        started = app.post("/tv/start", json={"key": "tmdb-603", "player": "app:Den"}, **phone)
    assert started.status_code == 202 and started.json["player"] == "app:Den"
    assert thread.call_args.kwargs["args"][:2] == ("tmdb-603", "app:Den")
    assert app.post("/tv/start", json={"key": "tmdb-603"}, **phone).status_code == 400
    assert app.post("/tv/player", json={"player": 5}, **phone).status_code == 400


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


def pair_home_assistant(plex, app) -> dict:
    started = app.post("/tv/pair", json={"name": "Home Assistant", "kind": "homeassistant"}).json
    plex.app_pairings.approve(started["pair_id"])
    return {"Authorization": f"Bearer {plex.app_pairings.poll(started['pair_id'])['secret']}"}


def test_home_assistant_hears_what_marquee_players_play(plex):
    app = create_app(CONFIG, plex).test_client()
    den = pair(plex, "Den")
    pair(plex, "Kevin's iPhone")
    assert app.get("/ha/players").status_code == 401
    assert app.get("/ha/players", headers=den).status_code == 401  # a Marquee isn't Home Assistant
    ha = pair_home_assistant(plex, app)
    assert app.get("/tv/inbox", headers=ha).status_code == 401  # and Home Assistant isn't a Marquee
    assert app.get("/panel/api/overview").json["devices"][2]["kind"] == "homeassistant"

    first = app.get("/ha/players", headers=ha).json
    assert [(p["id"], p["state"]) for p in first["players"]] == [("app:Den", "idle"), ("app:Kevin's iPhone", "idle")]

    # nothing new: waits, then answers with the same tag
    t = time.monotonic()
    assert app.get(f"/ha/players?tag={first['tag']}&wait=0.3", headers=ha).json["tag"] == first["tag"]
    assert time.monotonic() - t >= 0.3

    # a movie starting answers a waiting request straight away
    report = {"state": "playing", "key": "tmdb-603", "title": "The Matrix", "position": 5, "duration": 8160}
    threading.Timer(0.2, lambda: app.post("/tv/status", json=report, headers=den)).start()
    t = time.monotonic()
    changed = app.get(f"/ha/players?tag={first['tag']}&wait=10", headers=ha).json
    assert time.monotonic() - t < 5
    assert changed["tag"] != first["tag"]
    assert changed["players"][0] == {"id": "app:Den", "device_id": mock.ANY, "name": "Den", "state": "playing",
                                     "key": "tmdb-603", "title": "The Matrix", "poster": "/poster/tmdb-603",
                                     "position": 5, "duration": 8160}
    # the position moving on isn't a change
    app.post("/tv/status", json={**report, "position": 11}, headers=den)
    assert app.get(f"/ha/players?tag={changed['tag']}&wait=0", headers=ha).json["tag"] == changed["tag"]

    # controls and starting a movie
    assert app.post("/ha/control", json={"player": "app:Den", "action": "pause"}, headers=ha).json["ok"]
    assert app.get("/tv/inbox", headers=den).json["action"] == "pause"
    assert app.post("/ha/control", json={"player": "app:Den", "action": "eject"}, headers=ha).status_code == 400
    with mock.patch("plexnfc.server.threading.Thread") as thread:
        started = app.post("/ha/play", json={"key": "tmdb-603", "player": "app:Den"}, headers=ha)
    assert started.status_code == 202 and started.json["title"] == "The Matrix"
    assert thread.call_args.kwargs["args"] == ("tmdb-603", "app:Den", False)


def test_movie_remote_pairs_then_plays_on_its_player(plex):
    app = create_app(CONFIG, plex).test_client()
    den = pair(plex, "Den")
    # an unpaired box can't do anything
    assert app.get("/remote/now").status_code == 401
    assert app.post("/remote/tap", json={"url": "http://marquee.local:8080/m/tmdb-603"}).status_code == 401
    assert app.get("/remote/now", headers=den).status_code == 401  # a Marquee isn't a Movie Remote
    started = app.post("/tv/pair", json={"name": "Movie Remote", "kind": "remote"}).json
    app.post(f"/panel/api/pairings/{started['pair_id']}/approve", json={"name": "Den Remote"},
             environ_base={"REMOTE_ADDR": "127.0.0.1"})
    secret = app.get("/tv/pair/" + started["pair_id"]).json["secret"]
    box = {"Authorization": f"Bearer {secret}"}
    device = next(d for d in app.get("/panel/api/overview").json["devices"] if d["kind"] == "remote")
    assert device["name"] == "Den Remote"
    assert "app:Den Remote" not in [p["id"] for p in plex.players()]  # it's not somewhere to play

    # its player is chosen in the panel
    assert app.post(f"/panel/api/devices/{device['id']}/player", json={"player": "app:Den"},
                    environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    assert app.post(f"/panel/api/devices/{device['id']}/player", json={"player": "app:Den"}).json["ok"]

    # a tag from any address plays there, from the beginning unless the tag asks to resume
    with mock.patch.object(plex, "play") as play:
        tapped = app.post("/remote/tap", json={"url": "https://movies.1912productions.com/m/tmdb-603"}, headers=box)
        assert tapped.status_code == 202 and tapped.json["player"] == "Den"
        assert app.get("/remote/now", headers=box).json["state"] in ("starting", "idle")
        for _ in range(50):
            if play.called:
                break
            time.sleep(0.02)
        play.assert_called_with("tmdb-603", "app:Den", False)
    assert app.post("/remote/tap", json={"url": "http://marquee.local:8080/book"}, headers=box).status_code == 404

    # the screen shows what Den reports
    app.post("/tv/status", json={"state": "playing", "key": "tmdb-603", "title": "The Matrix", "position": 5,
                                 "duration": 8160}, headers=den)
    now = app.get("/remote/now", headers=box).json
    assert (now["state"], now["title"], now["player"], now["name"]) == ("playing", "The Matrix", "Den", "Den Remote")

    # titles the box's fonts can't draw are made plain on the server: 8½ isn't "8☐"
    app.post("/tv/status", json={"state": "playing", "key": "plex-1", "title": "8½", "position": 5,
                                 "duration": 8160}, headers=den)
    assert app.get("/remote/now", headers=box).json["title"] == "8 1/2"

    # the buttons control Den
    assert app.post("/remote/control", json={"action": "toggle"}, headers=box).json["ok"]
    assert app.get("/tv/inbox", headers=den).json["action"] == "pause"
    assert app.post("/remote/control", json={"action": "seek", "by": -30}, headers=box).json["ok"]
    assert app.get("/tv/inbox?after=0", headers=den).json == mock.ANY
    assert app.post("/remote/control", json={"action": "eject"}, headers=box).status_code == 400

    # removed in the panel: it's refused, and pairs again
    app.post(f"/panel/api/devices/{device['id']}/remove")
    assert app.get("/remote/now", headers=box).status_code == 401


def test_movie_remote_reset_forgets_its_pairing(plex):
    app = create_app(CONFIG, plex).test_client()
    started = app.post("/tv/pair", json={"name": "Movie Remote", "kind": "remote"}).json
    plex.app_pairings.approve(started["pair_id"])
    box = {"Authorization": f"Bearer {plex.app_pairings.poll(started['pair_id'])['secret']}"}
    assert app.post("/remote/unpair", json={}).status_code == 401  # only the box itself
    assert app.post("/remote/unpair", json={}, headers=box).json["ok"]
    assert not [d for d in app.get("/panel/api/overview").json["devices"] if d["kind"] == "remote"]
    assert app.get("/remote/now", headers=box).status_code == 401


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
    # how far it's been watched, for Marquee's "Resume?" after scanning a code
    movie.viewOffset = 2_530_000
    assert app.get("/tv/movie/tmdb-603", headers=headers).json["watched"] == 2530
    assert app.get("/tv/movie/tmdb-603", headers=headers).json["title"] == "The Matrix"


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
    outside = {"REMOTE_ADDR": "192.0.2.50"}
    assert app.post("/panel/api/appletvs/scan", environ_base=outside).status_code == 403
    assert app.post("/panel/api/appletvs/pair", json={"identifier": "ATV1", "player": "Den"},
                    environ_base=outside).status_code == 403
    assert app.post("/panel/api/appletvs/open", json={"player": "Den"}, environ_base=outside).status_code == 403
    assert app.post("/panel/api/appletvs/scan").json["found"][0]["name"] == "Living Room"
    plex.apple_tvs.start_pairing.return_value = "abc"
    assert app.post("/panel/api/appletvs/pair", json={"identifier": "ATV1", "player": "Den"}).json["pair_id"] == "abc"
    plex.apple_tvs.finish_pairing.return_value = "Den"
    assert app.post("/panel/api/appletvs/pair/abc", json={"pin": "1234"}).json["player"] == "Den"
    assert app.get("/panel/api/overview").json["apple_tvs"] == {"available": True, "linked": []}
    from plexnfc.appletv import AppleTVError
    plex.apple_tvs.open_marquee.side_effect = AppleTVError("The Apple TV didn't answer")
    failed = app.post("/panel/api/appletvs/open", json={"player": "Den"})
    assert failed.status_code == 400 and failed.json["message"] == "The Apple TV didn't answer"


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
    tv, other = {"REMOTE_ADDR": "192.168.0.34"}, {"REMOTE_ADDR": "192.0.2.50"}
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
    assert "is connecting to this server as Den" in app.post("/panel/api/appletvs/pair/abc", json={"pin": "1234"}).json["message"]
    plex.apple_tvs.open_marquee.assert_called_once_with("Den")
    assert plex.app_pairings.expected("192.168.0.34") == "Den"
    # Connect Marquee does the same for an Apple TV paired already; panel only
    assert app.post("/panel/api/appletvs/connect", json={"player": "Den"},
                    environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    assert app.post("/panel/api/appletvs/connect", json={"player": "Den"}).status_code == 200
    plex.apple_tvs.link_for.return_value = None
    assert app.post("/panel/api/appletvs/connect", json={"player": "Nope"}).status_code == 404


def own_network_address() -> str | None:
    """This machine's address on the network, if it has one (no packet is sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


# ---- movie cards ----------------------------------------------------------------

def paired_box(plex, app):
    started = app.post("/tv/pair", json={"name": "Movie Remote", "kind": "remote"}).json
    plex.app_pairings.approve(started["pair_id"])
    return {"Authorization": f"Bearer {plex.app_pairings.poll(started['pair_id'])['secret']}"}


def test_card_ids_are_normalized():
    assert normalize_uid("04:f3:21:b8:49:02:89") == "04-F3-21-B8-49-02-89"
    assert normalize_uid("04f321b8") == "04-F3-21-B8"
    assert normalize_uid("04-F3-2") is None and normalize_uid("") is None and normalize_uid("zz-zz-zz-zz") is None


def test_bulk_scan_links_cards_in_order(tmp_path):
    cards = Cards(str(tmp_path / "cards.json"))
    assert cards.scanned("04-00-00-01") is None  # no scan going
    with pytest.raises(ValueError):
        cards.start_scan([])
    movies = [{"key": "tmdb-1", "title": "One"}, {"key": "tmdb-2", "title": "Two"}, {"key": "tmdb-3", "title": "Three"}]
    scan = cards.start_scan(movies)
    assert (scan["index"], scan["total"], scan["current"]["title"], scan["next"]["title"]) == (0, 3, "One", "Two")

    first = cards.scanned("04-00-00-01")
    assert "Linked: One" in first["message"] and first["result"] == "linked"
    # the same card read again doesn't go on to the next movie
    again = cards.scanned("04:00:00:01")
    assert "Use the next card" in again["message"] and again["result"] == "repeat"
    assert cards.scan()["current"]["title"] == "Two"
    cards.move(1)  # skip Two
    assert cards.scan()["current"]["title"] == "Three"
    cards.move(-1)
    cards.move(1)
    # a card linked before moves to the new movie, and says so
    message = cards.scanned("04-00-00-01")["message"]
    assert "Linked: Three" in message and "(was One)" in message
    assert cards.movie("04-00-00-01")["key"] == "tmdb-3"
    done = cards.scan()
    assert done["done"] and done["linked"] == 2 and done["current"] is None
    assert cards.scanned("04-00-00-02")["result"] == "done"

    # saved, and read back
    cards.link("04-00-00-02", "tmdb-1", "One")
    again = Cards(str(tmp_path / "cards.json"))
    assert again.movie("04-00-00-02")["title"] == "One" and again.by_movie() == {"tmdb-3": ["04-00-00-01"],
                                                                                   "tmdb-1": ["04-00-00-02"]}
    assert again.link("04-00-00-02", "tmdb-2", "Two") == "One"
    assert again.remove("04-00-00-02") and not again.remove("04-00-00-02")
    cards.stop_scan()
    assert cards.scan() is None


def test_movie_remote_plays_linked_cards_and_links_them_in_a_scan(plex):
    app = create_app(CONFIG, plex).test_client()
    box = paired_box(plex, app)
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    elsewhere = {"environ_base": {"REMOTE_ADDR": "192.0.2.50"}}

    # a blank card that isn't linked: the box is told, and the panel offers to link it
    r = app.post("/remote/tap", json={"uid": "04-AA-BB-CC-DD-EE-FF", "url": ""}, headers=box)
    assert r.status_code == 404 and "isn't linked" in r.json["message"]
    assert app.get("/panel/api/cards").json["unknown"]["uid"] == "04-AA-BB-CC-DD-EE-FF"
    assert app.post("/panel/api/cards/link", json={"uid": "04-AA-BB-CC-DD-EE-FF", "key": "tmdb-603"},
                    **elsewhere).status_code == 403
    linked = app.post("/panel/api/cards/link", json={"uid": "04-AA-BB-CC-DD-EE-FF", "key": "tmdb-603"}, **panel).json
    assert linked["cards"][0]["title"] == "The Matrix" and linked["unknown"] is None
    assert app.get("/panel/api/movies").json["movies"][0]["cards"] == ["04-AA-BB-CC-DD-EE-FF"]

    # now it plays, with no URL written on it
    with mock.patch.object(plex, "play") as play:
        tapped = app.post("/remote/tap", json={"uid": "04-AA-BB-CC-DD-EE-FF"}, headers=box)
        assert tapped.status_code == 202 and tapped.json["title"] == "The Matrix"
        for _ in range(50):
            if play.called:
                break
            time.sleep(0.02)
        play.assert_called_with("tmdb-603", None, False)

    # during a scan, cards are linked instead of played
    assert app.post("/panel/api/cards/scan", json={"movies": [{"key": "tmdb-603", "title": "The Matrix"}]},
                    **elsewhere).status_code == 403
    scan = app.post("/panel/api/cards/scan", json={"movies": [{"key": "tmdb-603", "title": "The Matrix"},
                                                              {"key": "tmdb-604", "title": "Reloaded"}]}, **panel).json
    assert scan["scan"]["current"]["title"] == "The Matrix"
    with mock.patch.object(plex, "play") as play:
        r = app.post("/remote/tap", json={"uid": "04-11-22-33-44-55-66", "url": ""}, headers=box)
        assert r.json["scan"] and "Next: Reloaded" in r.json["message"] and r.json["result"] == "linked"
        assert app.post("/panel/api/cards/scan/skip", **panel).json["scan"]["done"]
        assert app.post("/panel/api/cards/scan/back", **panel).json["scan"]["current"]["title"] == "Reloaded"
        app.post("/remote/tap", json={"uid": "04-77-22-33-44-55-66"}, headers=box)
        state = app.post("/panel/api/cards/scan/stop", **panel).json
        assert state["scan"] is None and len(state["cards"]) == 3
        assert not play.called
    assert app.post("/panel/api/cards/04-77-22-33-44-55-66/remove", **panel).json["ok"]
    assert app.post("/panel/api/cards/04-77-22-33-44-55-66/remove", **panel).status_code == 404

    # tags with a written URL still work
    with mock.patch.object(plex, "play"):
        assert app.post("/remote/tap", json={"uid": "04-99-99-99", "url": "http://x/m/tmdb-603"},
                        headers=box).status_code == 202


def test_panel_chooses_movies_by_label_and_lists_filters(plex):
    app = create_app(CONFIG, plex).test_client()
    plex.section.search.return_value = [plex._movie]
    plex.section.collections.return_value = [NS(title="Pixar", childCount=10), NS(title="noir", childCount=0)]
    plex.section.listFilterChoices.return_value = [NS(title="Kids")]
    assert app.get("/panel/api/movies?label=Kids").json["movies"][0]["title"] == "The Matrix"
    plex.section.search.assert_called_once_with(label="Kids", sort="titleSort")
    assert app.get("/panel/api/filters").json == {"collections": [{"title": "noir", "count": 0},
                                                                  {"title": "Pixar", "count": 10}], "labels": ["Kids"],
                                                    "sources": []}


def test_box_text():
    from plexnfc.screen_text import box_json, box_text
    assert box_text("8½") == "8 1/2"
    assert box_text("½ Nelson") == "1/2 Nelson"
    assert box_text("Amélie") == "Amelie"
    assert box_text("Alien³") == "Alien3"
    assert box_text("Mission: Impossible – Fallout") == "Mission: Impossible - Fallout"
    assert box_text("Schindler’s List") == "Schindler's List"
    assert box_text("“Wall·E”…") == '"Wall-E"...'
    assert box_text("Æon Flux") == "AEon Flux"
    assert box_text("Léon:\nThe Professional") == "Leon:\nThe Professional"
    assert box_text("七人の侍 Seven Samurai") == "Seven Samurai"
    assert box_text("Plain title") == "Plain title"
    assert box_json({"title": "8½", "n": 3, "l": ["É"]}) == {"title": "8 1/2", "n": 3, "l": ["E"]}


def test_apps_start_the_plex_sign_in_with_json(plex, tmp_path):
    from dataclasses import replace
    plex.config = replace(plex.config, auth_file=str(tmp_path / "plex_auth.json"))
    app = create_app(plex.config, plex).test_client()
    with mock.patch("plexnfc.plexauth.start_pin", return_value=(42, "https://app.plex.tv/auth#?code=x")), \
         mock.patch("plexnfc.plexauth.check_pin", return_value="tok") as check:
        assert app.post("/panel/api/plex/login",
                        environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
        r = app.post("/panel/api/plex/login")
        assert r.json == {"ok": True, "pin_id": 42, "auth_url": "https://app.plex.tv/auth#?code=x"}
        assert app.get("/plex/check?id=42").json == {"signed_in": True}
    check.assert_called_once()


ABBEY_ROAD = {"uri": "library://album/4", "media_type": "album", "name": "Abbey Road", "artist": "The Beatles",
              "image": "http://ma.local:8095/imageproxy/abbey"}


def fake_home_assistant(app, ha, answers, stop):
    """The Marquee integration's music link: poll for jobs and answer them from ``answers``."""
    speakers = [{"entity_id": "media_player.kitchen", "name": "Kitchen", "player_id": "ma_kitchen"}]
    while not stop.is_set():
        jobs = app.post("/ha/music/poll?wait=0.2", json={"speakers": speakers, "music_assistant": True},
                        headers=ha).json["jobs"]
        for job in jobs:
            answers.append(job)
            if job["kind"] == "search":
                body = {"ok": True, "result": [ABBEY_ROAD, {"uri": "bad", "media_type": "album", "name": "x"}]}
            elif job["data"]["speaker"] == "media_player.gone":
                body = {"ok": False, "message": "Home Assistant has no speaker media_player.gone"}
            else:
                body = {"ok": True}
            app.post(f"/ha/music/jobs/{job['id']}", json=body, headers=ha)


def test_cards_play_music_through_home_assistant(plex):
    app = create_app(CONFIG, plex).test_client()
    box = paired_box(plex, app)
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}

    # without Home Assistant, the panel says so and music can't be searched
    status = app.get("/panel/api/music").json
    assert (status["connected"], status["music_assistant"], status["speakers"], status["via"]) == (False, False, [], None)
    assert status["assistant"] == {"url": "", "token": False, "connected": False, "message": None}
    assert app.get("/panel/api/music/search?q=abbey").json["items"] == []  # no music anywhere
    assert app.post("/ha/music/poll", json={}).status_code == 401

    ha = pair_home_assistant(plex, app)
    answers: list = []
    stop = threading.Event()
    worker = threading.Thread(target=fake_home_assistant, args=(app, ha, answers, stop), daemon=True)
    worker.start()
    try:
        for _ in range(50):
            if app.get("/panel/api/music").json["connected"]:
                break
            time.sleep(0.02)
        # speakers go by their Music Assistant player ID, which a direct connection knows too
        assert app.get("/panel/api/music").json["speakers"] == [{"id": "ma_kitchen", "name": "Kitchen"}]
        assert app.get("/panel/api/music").json["via"] == "home_assistant"

        # search Music Assistant through Home Assistant; what isn't an item is dropped
        found = app.get("/panel/api/music/search?q=abbey road").json["items"]
        assert [(i["id"], i["name"], i["artist"]) for i in found] == [
            ("ma|album|library://album/4", "Abbey Road", "The Beatles")]
        assert answers[-1]["data"] == {"query": "abbey road"}

        # link a card the box read to the album on the kitchen speaker
        app.post("/remote/tap", json={"uid": "04-11-22-33"}, headers=box)
        linked = app.post("/panel/api/cards/link", json={"uid": "04-11-22-33", "music": ABBEY_ROAD,
                                                         "speaker": "ma_kitchen"}, **panel).json
        card = linked["cards"][0]
        assert (card["title"], card["speaker_name"], card["music"]["uri"]) == \
            ("Abbey Road - The Beatles", "Kitchen", "library://album/4")
        assert linked["unknown"] is None
        assert app.post("/panel/api/cards/link", json={"uid": "04-11-22-33", "music": {"uri": "x"},
                                                       "speaker": "media_player.kitchen"}, **panel).status_code == 400
        # a music card isn't any movie's card
        assert all(not m["cards"] for m in app.get("/panel/api/movies").json["movies"])

        # tapping it plays the album there (Home Assistant gets its own entity), and the box
        # shows what and where
        with mock.patch.object(plex, "play") as play_movie:
            tapped = app.post("/remote/tap", json={"uid": "04-11-22-33"}, headers=box)
        assert tapped.status_code == 200 and tapped.json["result"] == "music"
        assert tapped.json["message"] == "Playing Abbey Road - The Beatles\non Kitchen"
        assert answers[-1]["data"] == {"uri": "library://album/4", "media_type": "album",
                                       "speaker": "media_player.kitchen"}
        play_movie.assert_not_called()

        # a scan can link music too
        app.post("/panel/api/cards/scan", json={"movies": [{"music": ABBEY_ROAD, "speaker": "media_player.gone"}]},
                 **panel)
        scanned = app.post("/remote/tap", json={"uid": "04-44-55-66"}, headers=box).json
        assert scanned["result"] == "linked" and "Abbey Road - The Beatles" in scanned["message"]
        app.post("/panel/api/cards/scan/stop", **panel)

        # Home Assistant's error reaches the box
        failed = app.post("/remote/tap", json={"uid": "04-44-55-66"}, headers=box)
        assert failed.status_code == 502 and "no speaker media_player.gone" in failed.json["message"]
    finally:
        stop.set()
        worker.join(2)


def test_music_bridge_tells_a_slow_home_assistant_from_a_missing_one():
    from plexnfc.music import MusicBridge, MusicError

    bridge = MusicBridge()
    bridge.poll([{"entity_id": "media_player.kitchen"}], True, wait=0)
    # the job waits for Home Assistant: not done yet, but not failed either
    assert bridge.play(ABBEY_ROAD, "media_player.kitchen", timeout=0.05) is False
    assert [j["kind"] for j in bridge.poll([], True, wait=0)] == ["play"]
    # a search that isn't answered fails, and is taken off the list
    with pytest.raises(MusicError, match="in time"):
        bridge.request("search", {"query": "x"}, timeout=0.05)
    assert bridge.poll([], True, wait=0) == []
    # without Music Assistant in Home Assistant
    bridge.poll([], False, wait=0)
    with pytest.raises(MusicError, match="Music Assistant"):
        bridge.search("x")


def test_cards_read_by_home_assistant_readers(plex):
    """A Tessera screen (or a phone) reads a card in Home Assistant; the integration asks Marquee."""
    app = create_app(CONFIG, plex).test_client()
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    assert app.post("/ha/tap", json={"uid": "A2-09-B0-9E"}).status_code == 401
    ha = pair_home_assistant(plex, app)

    # a card Marquee doesn't know: the panel offers to link it
    unknown = app.post("/ha/tap", json={"uid": "A2-09-B0-9E", "reader": "Office_Test"}, headers=ha).json
    assert (unknown["ok"], unknown["result"]) == (False, "unknown")
    assert app.get("/panel/api/cards").json["unknown"]["uid"] == "A2-09-B0-9E"

    # linked to music: the integration is told what to play where
    plex.cards.link_music("A2-09-B0-9E", ABBEY_ROAD, "media_player.kitchen", "Kitchen")
    tapped = app.post("/ha/tap", json={"uid": "a2:09:b0:9e"}, headers=ha).json
    assert tapped["result"] == "music" and tapped["message"] == "Playing Abbey Road - The Beatles on Kitchen"
    assert tapped["play"] == {"uri": "library://album/4", "media_type": "album", "speaker": "media_player.kitchen"}

    # a movie card plays on the default player
    with mock.patch.object(plex, "play") as play:
        plex.cards.link("04-AA-BB-CC", "tmdb-603", "The Matrix")
        tapped = app.post("/ha/tap", json={"uid": "04-AA-BB-CC"}, headers=ha).json
        assert (tapped["result"], tapped["title"]) == ("movie", "The Matrix")
        for _ in range(50):
            if play.called:
                break
            time.sleep(0.02)
        play.assert_called_with("tmdb-603", None, False)

    # and a scan links cards read there too
    app.post("/panel/api/cards/scan", json={"movies": [{"key": "tmdb-603", "title": "The Matrix"}]}, **panel)
    scanned = app.post("/ha/tap", json={"uid": "04-77-88-99"}, headers=ha).json
    assert scanned["scan"] and scanned["result"] == "linked"


class FakeMusicAssistant:
    """Music Assistant's ``POST /api``, for ``requests.post``."""

    def __init__(self, token="good"):
        self.token = token
        self.calls: list = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        assert url == "http://ma.local:8095/api"
        if headers.get("Authorization") != f"Bearer {self.token}":
            return mock.Mock(status_code=401, text="Authentication required")
        self.calls.append((json["command"], json["args"]))
        result = {
            "players/all": [
                {"player_id": "ma_kitchen", "display_name": "Kitchen", "available": True, "enabled": True},
                {"player_id": "ma_attic", "name": "Attic", "available": False},
                # Music Assistant's player for a Home Assistant speaker is named after its entity
                {"player_id": "media_player.office", "name": "Office", "available": True},
            ],
            "music/search": {"albums": [{
                "uri": "library://album/4", "media_type": "album", "name": "Abbey Road",
                "artists": [{"name": "The Beatles"}],
                "metadata": {"images": [{"type": "thumb", "path": "/art/4.jpg", "provider": "filesystem",
                                         "remotely_accessible": False}]}}],
                "artists": [{"uri": "library://artist/1", "media_type": "artist", "name": "The Beatles",
                             "metadata": {"images": [{"type": "thumb", "path": "/art/1.jpg", "provider": "filesystem",
                                                      "proxy_id": "f46d1a"}]}}],
                "playlists": [{"uri": "apple_music://playlist/9", "media_type": "playlist", "name": "Essentials",
                               "version": "2019 Mix", "metadata": {"images": [
                                   {"type": "thumb", "path": "album/l.x", "provider": "apple_music", "remotely_accessible": False},
                                   {"type": "thumb", "path": "https://art/9.jpg", "provider": "apple_music",
                                    "remotely_accessible": True}]}}],
                "tracks": [{"uri": "nope", "media_type": "track", "name": "x"}]},
            "player_queues/play_media": None,
        }[json["command"]]
        return mock.Mock(status_code=200, json=mock.Mock(return_value=result))


def test_cards_play_music_through_music_assistant_directly(plex, tmp_path):
    from dataclasses import replace
    plex.config = replace(plex.config, auth_file=str(tmp_path / "plex_auth.json"))
    app = create_app(CONFIG, plex).test_client()
    box = paired_box(plex, app)
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    fake = FakeMusicAssistant()
    with mock.patch("plexnfc.music.requests.post", side_effect=fake):
        # a wrong token isn't kept
        bad = app.post("/panel/api/music/assistant", json={"url": "ma.local", "token": "bad"}, **panel)
        assert bad.status_code == 400 and "token" in bad.json["message"]
        assert not (tmp_path / "music_assistant.json").exists()

        # the port is filled in; it's kept once it works, and lists the speakers there are
        ok = app.post("/panel/api/music/assistant", json={"url": "ma.local", "token": "good"}, **panel).json
        assert (ok["via"], ok["speakers"]) == ("music_assistant", [{"id": "ma_kitchen", "name": "Kitchen"},
                                                                   {"id": "media_player.office", "name": "Office"}])
        assert ok["assistant"] == {"url": "http://ma.local:8095", "token": True, "connected": True, "message": None}
        saved = json.loads((tmp_path / "music_assistant.json").read_text())
        assert saved == {"url": "http://ma.local:8095", "token": "good"}
        assert oct((tmp_path / "music_assistant.json").stat().st_mode & 0o777) == "0o600"

        # search: pictures Music Assistant has come through its image proxy
        items = app.get("/panel/api/music/search?q=abbey").json["items"]
        assert [(i["uri"], i["name"]) for i in items] == [("library://artist/1", "The Beatles"),
                                                          ("library://album/4", "Abbey Road"),
                                                          ("apple_music://playlist/9", "Essentials (2019 Mix)")]
        assert items[0]["image"] == "http://ma.local:8095/imageproxy/f46d1a?size=256"  # Music Assistant 2.10
        assert items[2]["image"] == "https://art/9.jpg"  # a picture on the web wins over the proxy
        assert items[1]["image"].startswith("http://ma.local:8095/imageproxy?path=%252Fart%252F4.jpg&provider=filesystem")

        # link a card and tap it: Music Assistant plays it, with no Home Assistant at all
        app.post("/panel/api/cards/link", json={"uid": "04-11-22-33", "music": ABBEY_ROAD, "speaker": "ma_kitchen"},
                 **panel)
        tapped = app.post("/remote/tap", json={"uid": "04-11-22-33"}, headers=box).json
        assert tapped["message"] == "Playing Abbey Road - The Beatles\non Kitchen"
        assert fake.calls[-1] == ("player_queues/play_media",
                                  {"queue_id": "ma_kitchen", "media": "library://album/4", "option": "replace"})

        # a player whose ID looks like a Home Assistant entity is still Music Assistant's
        app.post("/panel/api/cards/link", json={"uid": "04-77-88-99", "music": ABBEY_ROAD,
                                                "speaker": "media_player.office"}, **panel)
        assert app.post("/remote/tap", json={"uid": "04-77-88-99"}, headers=box).json["message"].endswith("on Office")
        assert fake.calls[-1][1]["queue_id"] == "media_player.office"

        # a reader in Home Assistant: played here too, so the integration isn't asked to
        ha = pair_home_assistant(plex, app)
        fake.calls.clear()
        read = app.post("/ha/tap", json={"uid": "04-11-22-33"}, headers=ha).json
        assert read["result"] == "music" and "play" not in read
        assert fake.calls[-1][0] == "player_queues/play_media"

        # a blank address stops it, and forgets the token
        off = app.post("/panel/api/music/assistant", json={"url": ""}, **panel).json
        assert off["assistant"]["url"] == "" and off["via"] is None
        assert json.loads((tmp_path / "music_assistant.json").read_text()) == {}


def write_wav(path, seconds=1):
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000 * seconds)


@pytest.fixture
def music_app(plex, tmp_path, monkeypatch):
    """The app, with a music folder: Artist/Album/NN Title.wav, a cover and a playlist."""
    from dataclasses import replace
    monkeypatch.setenv("LYRICS_LRCLIB", "0")  # tests never ask lrclib.net
    plex.config = replace(plex.config, auth_file=str(tmp_path / "data" / "plex_auth.json"), plex_token="", plex_url="")
    (tmp_path / "data").mkdir()
    root = tmp_path / "Music"
    write_wav(root / "The Beatles" / "Abbey Road" / "01 Come Together.wav")
    write_wav(root / "The Beatles" / "Abbey Road" / "02 Something.wav", 2)
    write_wav(root / "Nina Simone" / "Pastel Blues" / "01 Be My Husband.wav")
    (root / "The Beatles" / "Abbey Road" / "cover.jpg").write_bytes(b"JPEGDATA")
    (root / "mix.m3u").write_text("#EXTM3U\nNina Simone/Pastel Blues/01 Be My Husband.wav\n"
                                  "The Beatles/Abbey Road/02 Something.wav\nmissing.wav\n")
    app = create_app(replace(CONFIG, public_base_url="http://marquee:8080"), plex)
    client = app.test_client()
    client.plex = plex
    return client, root


def music_player(app):
    """The MusicPlayer behind the app's routes."""
    return app.application.extensions["marquee_music"]


def test_music_folders_browse_and_stream(music_app):
    app, root = music_app
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    assert app.post("/panel/api/music/folders", json={"path": str(root / "nope")}, **panel).status_code == 400
    added = app.post("/panel/api/music/folders", json={"path": str(root), "name": "Records"}, **panel).json
    folder = added["folders"][0]
    assert (folder["name"], folder["kind"]) == ("Records", "folder")
    sid = folder["id"]
    assert [s["id"] for s in app.get("/panel/api/music/sources").json["sources"]] == [sid]

    albums = app.get(f"/panel/api/music/browse?source={sid}&kind=album").json["items"]
    assert [(a["name"], a["artist"]) for a in albums] == [("Abbey Road", "The Beatles"), ("Pastel Blues", "Nina Simone")]
    artists = app.get(f"/panel/api/music/browse?source={sid}&kind=artist&q=nina").json["items"]
    assert [a["name"] for a in artists] == ["Nina Simone"]

    # an album's tracks, in order, from their file names
    opened = app.get("/panel/api/music/item", query_string={"id": albums[0]["id"]}).json
    assert [(t["number"], t["name"], t["duration"]) for t in opened["tracks"]] == [(1, "Come Together", 1),
                                                                                    (2, "Something", 2)]
    # its picture is the folder's cover
    art = app.get("/" + albums[0]["image"])
    assert art.status_code == 200 and art.data == b"JPEGDATA"
    # an artist's albums, and the .m3u playlist (a missing file is left out)
    nina = app.get("/panel/api/music/item", query_string={"id": artists[0]["id"]}).json
    assert [a["name"] for a in nina["albums"]] == ["Pastel Blues"]
    mix = app.get(f"/panel/api/music/browse?source={sid}&kind=playlist").json["items"][0]
    tracks = app.get("/panel/api/music/item", query_string={"id": mix["id"]}).json["tracks"]
    assert [t["name"] for t in tracks] == ["Be My Husband", "Something"]

    # search finds it in every source
    assert [i["name"] for i in app.get("/panel/api/music/search?q=something").json["items"]] == ["Something"]

    # speakers get a signed address for each file (see the next test); a wrong one gets nothing
    from plexnfc.musiclib import split_id
    ref = split_id(opened["tracks"][1]["id"])[2]
    url, content_type = music_player(app).library.stream(opened["tracks"][1])
    assert content_type == "audio/wav" and app.get(url.replace("http://marquee:8080", "")).status_code == 200
    assert app.get(f"/music/file/{sid}/{ref}/wrongtoken/track.wav").status_code == 404

    # removing the folder forgets it (the files stay)
    assert app.post(f"/panel/api/music/folders/{sid}/remove", **panel).json["folders"] == []
    assert (root / "mix.m3u").exists()


class FakeAirPlay:
    available = True

    def __init__(self):
        self.played: list = []
        self.stopped: list = []
        self.futures: list = []

    def speakers(self):
        return [{"id": "airplay:den", "name": "Den HomePod", "kind": "airplay", "group": False}]

    def name(self, ref):
        return {"den": "Den HomePod"}.get(ref)

    def play(self, ref, source, meta):
        import concurrent.futures
        future = concurrent.futures.Future()
        self.played.append((ref, source, meta["name"]))
        self.futures.append(future)
        return future

    def stop(self, ref):
        self.stopped.append(ref)

    def volume(self, ref, level):
        self.played.append(("volume", ref, level))


class FakeCast:
    available = True

    def __init__(self):
        self.played: list = []
        self.controls: list = []
        self.state = "PLAYING"

    def speakers(self):
        return [{"id": "cast:kitchen", "name": "Kitchen speaker", "kind": "cast", "group": False},
                {"id": "cast:home", "name": "Whole house", "kind": "cast", "group": True}]

    def name(self, ref):
        return {"kitchen": "Kitchen speaker", "home": "Whole house"}.get(ref)

    def play(self, ref, url, content_type, meta, seek=0.0):
        self.played.append((ref, url, content_type, meta["name"], meta.get("public_image")))
        self.seeks = getattr(self, "seeks", []) + [seek]

    def status(self, ref):
        return {"state": self.state, "idle_reason": None, "position": 0, "duration": 1}

    def control(self, ref, action):
        self.controls.append((ref, action))

    def volume(self, ref, level):
        self.controls.append(("volume", ref, level))

    def get_volume(self, ref):
        return 40


def wait_for(check, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def test_music_plays_on_airplay_and_cast_speakers(music_app):
    app, root = music_app
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    # the player behind the routes, with fake speakers
    player = music_player(app)
    player.airplay, player.cast = FakeAirPlay(), FakeCast()

    speakers = app.get("/panel/api/music/speakers").json["speakers"]
    assert [(s["id"], s["group"]) for s in speakers] == [("airplay:den", False), ("cast:kitchen", False),
                                                          ("cast:home", True)]
    album = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=abbey").json["items"][0]
    assert app.post("/panel/api/music/play", json={"ids": [album["id"]], "speakers": []}, **panel).status_code == 400

    # an album on a HomePod and a Cast speaker at once: the same track on both
    zones = app.post("/panel/api/music/play", json={"ids": [album["id"]], "speakers": ["airplay:den", "cast:kitchen"]},
                     **panel).json["zones"]
    # AirPlay can't pause, so a track starts again on resume: where it was, given ffmpeg
    assert zones[0]["names"] == ["Den HomePod", "Kitchen speaker"]
    assert zones[0]["restarts_on_resume"] == (player._ffmpeg_path() is None)
    assert wait_for(lambda: player.airplay.played and player.cast.played)
    # AirPlay gets the file itself (WAV plays as it is); Cast gets the signed address and the cover
    assert player.airplay.played[0][1].endswith("01 Come Together.wav")
    ref, url, content_type, name, image = player.cast.played[0]
    assert (ref, content_type, name) == ("kitchen", "audio/wav", "Come Together")
    assert url.startswith(f"http://marquee:8080/music/file/{sid}/")
    played = app.get(url.replace("http://marquee:8080", ""), headers={"Range": "bytes=0-3"})
    assert played.status_code == 206 and played.data == b"RIFF"
    assert app.get(image.replace("http://marquee:8080", "")).data == b"JPEGDATA"

    # when the first speaker finishes, the next track starts
    player.airplay.futures[0].set_result(True)
    assert wait_for(lambda: len(player.airplay.played) == 2)
    assert player.airplay.played[1][2] == "Something"
    zone = app.get("/panel/api/music/now").json["zones"][0]
    assert (zone["index"], zone["track"]["name"], zone["state"]) == (1, "Something", "playing")

    # "play next" goes after it; previous goes back; pause and play; volume; stop
    nina = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=pastel").json["items"][0]
    app.post("/panel/api/music/play", json={"ids": [nina["id"]], "speakers": ["airplay:den"], "option": "next"}, **panel)
    assert [t["name"] for t in app.get("/panel/api/music/queue", query_string={"zone": zone["id"]}).json["items"]] == \
        ["Come Together", "Something", "Be My Husband"]
    app.post("/panel/api/music/control", json={"zone": zone["id"], "action": "previous"}, **panel)
    assert wait_for(lambda: len(player.airplay.played) == 3 and player.airplay.played[2][2] == "Come Together")
    app.post("/panel/api/music/control", json={"zone": zone["id"], "action": "pause"}, **panel)
    assert app.get("/panel/api/music/now").json["zones"][0]["state"] == "paused"
    assert ("kitchen", "pause") in player.cast.controls
    app.post("/panel/api/music/control", json={"zone": zone["id"], "action": "play"}, **panel)
    # AirPlay can't pause, so the track starts again
    assert wait_for(lambda: len(player.airplay.played) == 4)
    app.post("/panel/api/music/volume", json={"speaker": "cast:kitchen", "level": 30}, **panel)
    assert ("volume", "kitchen", 30) in player.cast.controls
    app.post("/panel/api/music/control", json={"zone": zone["id"], "action": "stop"}, **panel)
    assert app.get("/panel/api/music/now").json["zones"] == []
    assert "den" in player.airplay.stopped and ("kitchen", "stop") in player.cast.controls


def test_music_from_any_source_plays_on_music_assistant_speakers(music_app):
    app, root = music_app
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    fake = FakeMusicAssistant()

    def answer(url, json=None, headers=None, timeout=None):
        if json["command"] == "players/all":
            fake.calls.append((json["command"], json["args"]))
            return mock.Mock(status_code=200, json=mock.Mock(return_value=[
                {"player_id": "ma_kitchen", "display_name": "Kitchen", "type": "player", "playback_state": "playing",
                 "volume_level": 20, "group_members": ["ma_kitchen", "ma_den"], "can_group_with": ["ma_den"],
                 "current_media": {"title": "Something", "artist": "The Beatles", "duration": 182}, "elapsed_time": 12,
                 "elapsed_time_last_updated": time.time() - 30},
                {"player_id": "ma_den", "display_name": "Den", "synced_to": "ma_kitchen", "playback_state": "playing"},
                # an Apple TV paused in its own Plex app: not Music Assistant's, so not shown or controlled
                {"player_id": "ma_tv", "display_name": "TV", "playback_state": "paused", "active_source": "com.plexapp.plex",
                 "current_media": {"title": "Kim Possible"}}]))
        if json["command"] in ("players/cmd/set_members", "players/cmd/volume_set", "player_queues/pause"):
            fake.calls.append((json["command"], json["args"]))
            return mock.Mock(status_code=200, json=mock.Mock(return_value=None))
        return fake(url, json, headers, timeout)

    with mock.patch("plexnfc.music.requests.post", side_effect=answer):
        app.post("/panel/api/music/assistant", json={"url": "ma.local", "token": "good"}, **panel)
        assert [s["id"] for s in app.get("/panel/api/music/sources").json["sources"]] == ["ma", sid]
        album = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=abbey").json["items"][0]
        # a folder album on two Music Assistant speakers: grouped, then its tracks' addresses queued
        app.post("/panel/api/music/play", json={"ids": [album["id"]], "speakers": ["ma:ma_kitchen", "ma:ma_den"]},
                 **panel)
        assert ("players/cmd/set_members", {"target_player": "ma_kitchen", "player_ids_to_add": ["ma_den"]}) in fake.calls
        def last(command):
            return next(args for c, args in reversed(fake.calls) if c == command)
        args = last("player_queues/play_media")
        assert args["queue_id"] == "ma_kitchen"
        assert [u.rsplit("/", 1)[1] for u in args["media"]] == ["track.wav", "track.wav"]
        # Music Assistant's own music goes by its URI
        app.post("/panel/api/music/play", json={"ids": ["ma|album|library://album/4"], "speakers": ["ma:ma_kitchen"],
                                                "option": "add"}, **panel)
        assert last("player_queues/play_media") == {"queue_id": "ma_kitchen", "media": ["library://album/4"],
                                                    "option": "add"}
        # but not on AirPlay or Cast, which Marquee streams to itself
        refused = app.post("/panel/api/music/play", json={"ids": ["ma|album|library://album/4"],
                                                          "speakers": ["cast:kitchen"]}, **panel)
        assert refused.status_code == 400 and "Music Assistant's speakers" in refused.json["message"]
        # what's playing: the group, as one
        zones = app.get("/panel/api/music/now").json["zones"]
        assert [(z["id"], z["names"], z["track"]["name"]) for z in zones] == [
            ("ma:ma_kitchen", ["Kitchen", "Den"], "Something")]
        assert 41 < zones[0]["position"] < 45  # 12 s when last updated, 30 s ago
        app.post("/panel/api/music/control", json={"zone": "ma:ma_kitchen", "action": "pause"}, **panel)
        assert last("player_queues/pause") == {"queue_id": "ma_kitchen"}
        app.post("/panel/api/music/volume", json={"speaker": "ma:ma_den", "level": 15}, **panel)
        assert last("players/cmd/volume_set") == {"player_id": "ma_den", "volume_level": 15}
        # grouping: Den out of the Kitchen's group
        app.post("/panel/api/music/group", json={"leader": "ma:ma_kitchen", "members": []}, **panel)
        assert ("players/cmd/set_members", {"target_player": "ma_kitchen", "player_ids_to_add": [],
                                            "player_ids_to_remove": ["ma_den"]}) in fake.calls
        bad = app.post("/panel/api/music/group", json={"leader": "cast:x", "members": ["cast:y"]}, **panel)
        assert bad.status_code == 400 and "Google Home" in bad.json["message"]


def give_reader_speakers(app, kind, speakers):
    """Set the default output of the (first) reader of this kind."""
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    reader = next(r for r in app.get("/panel/api/readers").json["readers"] if r["kind"] == kind)
    app.post(f"/panel/api/readers/{reader['id']}", json={"default": speakers}, **panel)


def test_music_cards_from_the_music_tab(music_app):
    app, root = music_app
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    player = music_player(app)
    player.airplay, player.cast = FakeAirPlay(), FakeCast()
    album = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=pastel").json["items"][0]

    # a scan links the next card to the album, with no speaker of its own
    started = app.post("/panel/api/cards/scan", json={"movies": [{"music": album}]}, **panel)
    assert started.status_code == 200, started.json
    box_headers = paired_box(app.plex, app)
    linked = app.post("/remote/tap", json={"uid": "04-AA-BB-CC"}, headers=box_headers).json
    assert linked["result"] == "linked" and "Pastel Blues - Nina Simone" in linked["message"]
    app.post("/panel/api/cards/scan/stop", **panel)
    card = app.get("/panel/api/cards").json["cards"][0]
    assert card["music"]["id"] == album["id"] and "speaker" not in card

    # a reader without speakers says to choose them
    tapped = app.post("/remote/tap", json={"uid": "04-AA-BB-CC"}, headers=box_headers)
    assert tapped.status_code == 502 and "choose its default output" in tapped.json["message"].lower()
    # tapping it plays the album on the reader's speakers
    give_reader_speakers(app, "remote", ["cast:kitchen"])
    tapped = app.post("/remote/tap", json={"uid": "04-AA-BB-CC"}, headers=box_headers).json
    assert tapped["result"] == "music" and tapped["message"].endswith("on Kitchen speaker")
    assert wait_for(lambda: player.cast.played)
    assert player.cast.played[0][3] == "Be My Husband"
    # a speaker sent along (by an older panel) isn't kept
    app.post("/panel/api/cards/link", json={"uid": "04-11-22-33", "music": album, "speaker": "cast:den"}, **panel)
    assert "speaker" not in next(c for c in app.get("/panel/api/cards").json["cards"] if c["uid"] == "04-11-22-33")


def test_jellyfin_music_source():
    from plexnfc.musiclib import JellyfinMusicSource

    client = mock.Mock(url="http://jf:8096", api_key="KEY", user_id=None)
    responses = {
        "/Items": lambda **p: {"Items": [
            {"Id": "a1", "Type": "MusicAlbum", "Name": "Blue", "AlbumArtist": "Joni Mitchell", "ProductionYear": 1971,
             "ImageTags": {"Primary": "x"}},
        ]} if p.get("IncludeItemTypes") == "MusicAlbum" and "ParentId" not in p else {"Items": [
            {"Id": "t2", "Type": "Audio", "Name": "River", "Album": "Blue", "AlbumArtist": "Joni Mitchell",
             "IndexNumber": 7, "RunTimeTicks": 2_000_000_000},
        ]} if p.get("ParentId") == "a1" else {"Items": [{"Id": "a1", "Type": "MusicAlbum", "Name": "Blue"}]},
    }
    client.get.side_effect = lambda path, **params: responses[path](**params)
    source = JellyfinMusicSource(mock.Mock(id="j1", name="Jellyfin", client=client))
    albums = source.browse("album", "blue")
    assert [(a["id"], a["name"], a["artist"], a["year"]) for a in albums] == [("jf-j1|album|a1", "Blue", "Joni Mitchell", 1971)]
    assert albums[0]["image"] == "panel/api/music/art?id=jf-j1%7Calbum%7Ca1"
    assert client.get.call_args.kwargs["SearchTerm"] == "blue"
    tracks = source.open("album", "a1")["tracks"]
    assert [(t["number"], t["name"], t["duration"]) for t in tracks] == [(7, "River", 200)]
    # Jellyfin makes MP3 of anything, which every kind of speaker plays
    url, content_type = source.stream(tracks[0])
    assert content_type == "audio/mpeg" and url.startswith("http://jf:8096/Audio/t2/stream.mp3?") and "api_key=KEY" in url


def test_plex_music_source():
    from plexnfc.musiclib import PlexMusicSource

    track = mock.Mock(type="track", ratingKey=55, title="Hey Jude", grandparentTitle="The Beatles", originalTitle=None,
                      parentTitle="1", thumb="/t", duration=431000, index=21)
    track.media = [mock.Mock(parts=[mock.Mock(key="/library/parts/9/file.flac", file="/music/Hey Jude.flac")])]
    album = mock.Mock(type="album", ratingKey=12, title="1", parentTitle="The Beatles", thumb="/a", duration=None,
                      index=None, year=2000)
    album.tracks.return_value = [track]
    section = mock.Mock(type="artist")
    section.search.return_value = [album]
    server = mock.Mock()
    server.library.sections.return_value = [mock.Mock(type="movie"), section]
    server.fetchItem.side_effect = lambda key: {12: album, 55: track}[key]
    server.url.side_effect = lambda path, includeToken=False: f"http://plex:32400{path}?X-Plex-Token=tok"
    source = PlexMusicSource(mock.Mock(server=server))

    albums = source.browse("album", "one")
    assert [(a["id"], a["name"], a["artist"], a["year"]) for a in albums] == [("plex|album|12", "1", "The Beatles", 2000)]
    assert section.search.call_args.kwargs["title__icontains"] == "one"
    tracks = source.open("album", "12")["tracks"]
    assert [(t["name"], t["artist"], t["duration"], t["number"]) for t in tracks] == [("Hey Jude", "The Beatles", 431, 21)]
    assert source.stream(tracks[0]) == ("http://plex:32400/library/parts/9/file.flac?X-Plex-Token=tok", "audio/flac")


class FakeRAOPService:
    def __init__(self, mandatory):
        from pyatv.const import PairingRequirement
        self.pairing = PairingRequirement.Mandatory if mandatory else PairingRequirement.NotNeeded
        self.credentials = None


class FakeAirPlayConf:
    def __init__(self, identifier, name, mandatory):
        self.identifier, self.name = identifier, name
        self.service = FakeRAOPService(mandatory)
        self.credentials = None

    def get_service(self, protocol):
        return self.service

    def set_credentials(self, protocol, credentials):
        self.credentials = credentials


class FakeRAOPPairing:
    """pyatv's AirPlay pairing handler: the TV shows 1234."""
    def __init__(self, service):
        self.service, self.has_paired, self.began, self.closed, self._pin = service, False, False, False, None

    async def begin(self):
        self.began = True

    def pin(self, pin):
        self._pin = pin

    async def finish(self):
        if self._pin == 1234:
            self.has_paired = True
            self.service.credentials = "CREDS"

    async def close(self):
        self.closed = True


def test_airplay_speakers_pair_with_the_pin_on_the_tv(music_app, tmp_path):
    import asyncio
    from plexnfc.speakers import AirPlay
    app, root = music_app
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    player = music_player(app)
    path = tmp_path / "data" / "airplay_pairings.json"
    den = FakeAirPlayConf("DEN", "Den Apple TV", mandatory=True)
    homepod = FakeAirPlayConf("POD", "Kitchen HomePod", mandatory=False)
    player.airplay = airplay = AirPlay(str(path))
    airplay._devices, airplay._scanned = {"DEN": den, "POD": homepod}, time.monotonic()
    handlers = []

    async def pair(conf, protocol, loop, **kwargs):
        handlers.append(FakeRAOPPairing(conf.service))
        return handlers[-1]

    speakers = {s["id"]: s for s in app.get("/panel/api/music/speakers", **panel).json["speakers"]}
    assert speakers["airplay:DEN"]["pairing"] == "needed"
    assert speakers["airplay:POD"]["pairing"] is None
    # an unpaired Apple TV is refused before streaming, with a hint
    with pytest.raises(Exception, match="Pair Den Apple TV first"):
        airplay.play("DEN", "/x.mp3", {"name": "x"})

    assert app.post("/panel/api/music/airplay/pair", json={"speaker": "cast:x"}, **panel).status_code == 400
    with mock.patch("plexnfc.speakers.pyatv.pair", pair):
        pair_id = app.post("/panel/api/music/airplay/pair", json={"speaker": "airplay:DEN"}, **panel).json["pair_id"]
        assert handlers[0].began
        wrong = app.post("/panel/api/music/airplay/pair/finish", json={"pair_id": pair_id, "pin": "9999"}, **panel)
        assert wrong.status_code == 400 and "wasn't accepted" in wrong.json["message"]
        # a pairing is used once
        again = app.post("/panel/api/music/airplay/pair/finish", json={"pair_id": pair_id, "pin": "1234"}, **panel)
        assert "expired" in again.json["message"]
        pair_id = app.post("/panel/api/music/airplay/pair", json={"speaker": "airplay:DEN"}, **panel).json["pair_id"]
        done = app.post("/panel/api/music/airplay/pair/finish", json={"pair_id": pair_id, "pin": " 12 34 "}, **panel).json
    assert done["name"] == "Den Apple TV" and handlers[-1].closed
    assert {s["id"]: s for s in done["speakers"]}["airplay:DEN"]["pairing"] == "paired"
    assert json.loads(path.read_text())["speakers"]["DEN"]["credentials"] == "CREDS"
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    # kept across restarts, and handed to pyatv before connecting
    airplay = AirPlay(str(path))
    airplay._devices, airplay._scanned = {"DEN": den}, time.monotonic()
    assert airplay.pairing_state("DEN") == "paired"

    async def connect(conf, loop):
        raise ConnectionError("not really")
    with mock.patch("plexnfc.speakers.pyatv.connect", connect):
        with pytest.raises(Exception):
            airplay.play("DEN", "/x.mp3", {"name": "x"}).result(3)
    assert den.credentials == "CREDS"

    player.airplay = airplay
    app.post("/panel/api/music/airplay/unpair", json={"speaker": "airplay:DEN"}, **panel)
    assert airplay.pairing_state("DEN") == "needed" and json.loads(path.read_text())["speakers"] == {}


def test_airplay_uses_the_apple_tv_pairing_from_the_apple_tvs_section(tmp_path):
    from plexnfc.speakers import AirPlay
    den = FakeAirPlayConf("DEN", "Den Apple TV", mandatory=True)
    den.all_identifiers = ["DEN", "aa:bb"]
    other = FakeAirPlayConf("GUEST", "Guest Room Apple TV", mandatory=True)
    linked = {"aa:bb": "LINKED"}
    airplay = AirPlay(str(tmp_path / "p.json"), lambda ids: next((linked[i] for i in ids if i in linked), None))
    airplay._devices, airplay._scanned = {"DEN": den, "GUEST": other}, time.monotonic()
    states = {s["id"]: s["pairing"] for s in airplay.speakers()}
    assert states == {"airplay:DEN": "linked", "airplay:GUEST": "needed"}
    assert airplay.credentials("DEN") == "LINKED"


def test_apple_tv_credentials_by_identifier(tmp_path):
    from plexnfc.appletv import AppleTVs
    tvs = AppleTVs(str(tmp_path / "apple_tvs.json"))
    tvs._links = {"Den": {"identifier": "X", "identifiers": ["X", "aa:bb"], "name": "Den", "credentials": "C"}}
    assert tvs.credentials_for(["zz", "aa:bb"]) == "C"
    assert tvs.credentials_for(["zz"]) is None


# ---- audiobooks (Audiobookshelf) ------------------------------------------------------

def abs_book(book_id, title, series=None, cover=True, seq=None):
    """A library item the way Audiobookshelf lists one (minified)."""
    return {"id": book_id, "mediaType": "book", "media": {
        "metadata": {"title": title, "authorName": "Ada Author", "narratorName": "Sam Reader",
                     "seriesName": series, "series": {"id": "s1", "name": series, "sequence": seq} if series else None,
                     "publishedYear": "2001", "description": f"About {title}"},
        "coverPath": "/metadata/cover.jpg" if cover else None, "duration": 270}}


class FakeAudiobookshelf:
    """Answers Audiobookshelf's API from memory, like the real 2.38 server, and keeps
    the progress it's sent."""

    def __init__(self):
        self.progress = {"b1": {"currentTime": 100.5, "isFinished": False}}
        self.saved: list = []
        self.calls: list = []

    def __call__(self, method, path, *, raw=False, timeout=15, params=None, json=None):
        self.calls.append((method, path, params))
        books = [abs_book("b1", "Book One", "Test Series", seq="1"), abs_book("b2", "Lonely Book", cover=False)]
        if path == "/api/libraries":
            return {"libraries": [{"id": "L1", "name": "Audiobooks", "mediaType": "book"},
                                  {"id": "P1", "name": "Podcasts", "mediaType": "podcast"}]}
        if path == "/api/me":
            return {"username": "kevin"}
        if path == "/api/libraries/L1/items":
            if (params or {}).get("filter", "").startswith("series."):
                return {"results": books[:1]}
            return {"results": books, "total": 2}
        if path == "/api/libraries/L1/search":
            return {"book": [{"libraryItem": b} for b in books if params["q"].lower() in b["media"]["metadata"]["title"].lower()],
                    "authors": [], "series": []}
        if path == "/api/libraries/L1/authors":
            return {"authors": [{"id": "a1", "name": "Ada Author", "lastFirst": "Author, Ada", "numBooks": 2, "imagePath": None}]}
        if path == "/api/libraries/L1/series":
            return {"results": [{"id": "s1", "name": "Test Series", "books": books[:1]}]}
        if path == "/api/authors/a1":
            return {"id": "a1", "name": "Ada Author", "libraryItems": books}
        if path == "/api/items/b1":
            item = abs_book("b1", "Book One", "Test Series", seq="1")
            item["media"]["metadata"]["series"] = [{"id": "s1", "name": "Test Series", "sequence": "1"}]
            item["media"]["tracks"] = [
                {"index": i + 1, "ino": str(120 + i), "startOffset": i * 90, "duration": 90, "title": f"Chapter {i + 1}",
                 "mimeType": "audio/mpeg"} for i in range(3)]
            item["media"]["chapters"] = [{"id": i, "start": i * 90, "end": (i + 1) * 90, "title": f"Chapter {i + 1}"}
                                         for i in range(3)]
            return item
        if path.startswith("/api/me/progress/"):
            book = path.rsplit("/", 1)[1]
            if method == "PATCH":
                self.saved.append((book, json))
                self.progress[book] = json
                return {}
            return self.progress.get(book)
        if path.endswith("/cover"):
            return mock.Mock(content=b"COVER", headers={"Content-Type": "image/jpeg"})
        return None


def books_app(music_app):
    """The app, with Audiobookshelf connected (a fake) and fake speakers."""
    app, root = music_app
    player = music_player(app)
    abs_ = player.library.audiobookshelf
    abs_.url, abs_.token = "http://abs:13378", "KEY"
    fake = FakeAudiobookshelf()
    abs_.request = fake
    player.airplay, player.cast = FakeAirPlay(), FakeCast()
    return app, player, fake


def test_audiobookshelf_books_authors_and_series(music_app):
    app, player, fake = books_app(music_app)
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    state = app.get("/panel/api/books", **panel).json
    # only the audiobook library; podcasts are left out
    assert state["audiobookshelf"]["libraries"] == [{"id": "L1", "name": "Audiobooks"}]
    assert [s["id"] for s in state["sources"]] == ["abs"]
    # Audiobookshelf is a Books source, not a music one
    assert "abs" not in [s["id"] for s in app.get("/panel/api/music/sources", **panel).json["sources"]]

    books = app.get("/panel/api/music/browse?source=abs&kind=book", **panel).json["items"]
    assert [(b["id"], b["name"], b["artist"], b["album"], b["narrator"], b["year"]) for b in books] == [
        ("abs|book|b1", "Book One", "Ada Author", "Test Series", "Sam Reader", 2001),
        ("abs|book|b2", "Lonely Book", "Ada Author", "", "Sam Reader", 2001)]
    assert books[0]["image"] == "panel/api/music/art?id=abs%7Cbook%7Cb1" and books[1]["image"] is None
    assert [b["name"] for b in app.get("/panel/api/music/browse?source=abs&kind=book&q=lonely", **panel).json["items"]] == [
        "Lonely Book"]
    authors = app.get("/panel/api/music/browse?source=abs&kind=author", **panel).json["items"]
    assert [(a["name"], a["count"]) for a in authors] == [("Ada Author", 2)]
    assert [b["name"] for b in app.get("/panel/api/music/item?id=abs|author|a1", **panel).json["albums"]] == [
        "Book One", "Lonely Book"]
    series = app.get("/panel/api/music/browse?source=abs&kind=series", **panel).json["items"]
    assert [(x["name"], x["count"]) for x in series] == [("Test Series", 1)]
    assert [b["name"] for b in app.get("/panel/api/music/item?id=abs|series|s1", **panel).json["albums"]] == ["Book One"]

    # a book: its files as tracks (where each starts), its chapters, and where you are in it
    opened = app.get("/panel/api/music/item?id=abs|book|b1", **panel).json
    assert [(t["name"], t["offset"]) for t in opened["tracks"]] == [("Chapter 1", 0), ("Chapter 2", 90), ("Chapter 3", 180)]
    assert [c["title"] for c in opened["chapters"]] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert opened["progress"] == {"position": 100.5, "finished": False}
    assert app.get("/panel/api/music/art?id=abs|book|b1", **panel).data == b"COVER"
    # speakers get the file with the key (as Audiobookshelf's own apps cast it)
    url, content_type = player.library.stream(opened["tracks"][1])
    assert url == "http://abs:13378/api/items/b1/file/121?token=KEY" and content_type == "audio/mpeg"


def test_audiobookshelf_connection_is_tried_and_kept_private(tmp_path):
    from plexnfc.audiobookshelf import Audiobookshelf, AudiobookshelfError

    abs_ = Audiobookshelf(str(tmp_path / "audiobookshelf.json"))
    answers = {"good": mock.Mock(ok=True, status_code=200, json=lambda: {"username": "kevin"}),
               "bad": mock.Mock(ok=False, status_code=401)}
    with mock.patch("plexnfc.audiobookshelf.requests.get",
                    side_effect=lambda url, headers, timeout: answers[headers["Authorization"].split()[1]]):
        with pytest.raises(AudiobookshelfError, match="didn't accept"):
            abs_.set("nas.local:13378", "bad")
        assert not abs_.configured
        abs_.set("nas.local:13378", "good")
    assert (abs_.url, abs_.user) == ("http://nas.local:13378", "kevin")
    path = tmp_path / "audiobookshelf.json"
    assert json.loads(path.read_text()) == {"url": "http://nas.local:13378", "token": "good"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    abs_.set("", None)
    assert not abs_.configured and json.loads(path.read_text()) == {}


def test_books_resume_where_you_left_off_and_report_back(music_app):
    from plexnfc.speakers import BookProgress
    app, player, fake = books_app(music_app)
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    player.cast.state = "PLAYING"

    # Audiobookshelf says 100.5 s: that's 10.5 s into the second file
    zones = app.post("/panel/api/music/play", json={"ids": ["abs|book|b1"], "speakers": ["cast:kitchen"]}, **panel).json["zones"]
    assert wait_for(lambda: player.cast.played)
    assert player.cast.played[0][1] == "http://abs:13378/api/items/b1/file/121?token=KEY"
    assert player.cast.seeks[0] == pytest.approx(10.5)
    zone = player.now()[0]
    assert zone["book"]["id"] == "abs|book|b1" and zone["book"]["duration"] == 270
    assert zone["book"]["position"] == pytest.approx(100.5, abs=1)
    assert [t["name"] for t in zone["queue"]] == ["Chapter 2", "Chapter 3"]

    # stopping reports where it got to
    session = player.sessions[0]
    session.started -= 20  # 20 s later
    app.post("/panel/api/music/control", json={"zone": zones[0]["id"], "action": "stop"}, **panel)
    book, body = fake.saved[-1]
    assert book == "b1" and body["currentTime"] == pytest.approx(120.5, abs=1) and body["duration"] == 270
    assert body["isFinished"] is False

    # a chapter from the panel: from its start; and from the start of the book
    player.cast.played.clear()
    app.post("/panel/api/music/play", json={"ids": ["abs|book|b1"], "speakers": ["cast:kitchen"], "position": 180},
             **panel)
    assert wait_for(lambda: player.cast.played)
    assert player.cast.played[0][1].endswith("/file/122?token=KEY") and player.cast.seeks[-1] == 0
    # the end of the book counts as finished
    progress = BookProgress(player)
    tracks = player.library.books.book_tracks("b1")
    progress._zones["z"] = {"book": "b1", "tracks": tracks, "duration": 270, "last": 268, "sent": None}
    progress._send(progress._zones["z"])
    assert fake.saved[-1][1]["isFinished"] is True
    player.control(player.sessions[0].id, "stop")


def test_book_cards_scan_tap_and_print(music_app):
    app, player, fake = books_app(music_app)
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    book = app.get("/panel/api/music/browse?source=abs&kind=book", **panel).json["items"][0]
    started = app.post("/panel/api/cards/scan", json={"movies": [{"music": book}]}, **panel)
    assert started.json["scan"]["current"]["title"] == "Book One - Ada Author"
    box = paired_box(app.plex, app)
    give_reader_speakers(app, "remote", ["cast:kitchen"])
    linked = app.post("/remote/tap", json={"uid": "04-B0-0C-01"}, headers=box).json
    assert linked["result"] == "linked"
    app.post("/panel/api/cards/scan/stop", **panel)

    # tapping it picks the book up where it was left
    tapped = app.post("/remote/tap", json={"uid": "04-B0-0C-01"}, headers=box).json
    assert tapped["result"] == "music" and tapped["message"].endswith("on Kitchen speaker")
    assert wait_for(lambda: player.cast.played) and player.cast.seeks[0] == pytest.approx(10.5)
    player.control(player.sessions[0].id, "stop")

    # the printed book: a page per book, the card's QR code on its page
    page = app.get("/book/books?layout=grid&qr=1").get_data(as_text=True)
    assert "Book One" in page and "Lonely Book" in page and "2 books" in page
    assert "Plays on" not in page and page.count('class="qr"') == 1
    assert "Story Time" in page and "read by Sam Reader" in page
    only = app.get("/book/books?cards=1").get_data(as_text=True)
    assert "Book One" in only and "Lonely Book" not in only
    assert "Lonely Book" not in app.get("/book/books?what=series:s1").get_data(as_text=True)
    # the QR code's address plays the card, on a reader's speakers
    player.cast.played.clear()
    played = app.get("/c/04-B0-0C-01")
    assert played.status_code == 200 and "Book One" in played.get_data(as_text=True)
    assert wait_for(lambda: player.cast.played)
    player.control(player.sessions[0].id, "stop")
    assert app.get("/c/04-FF-FF-FF").status_code == 404


def test_music_book_of_albums(music_app):
    app, root = music_app
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    player = music_player(app)
    player.airplay, player.cast = FakeAirPlay(), FakeCast()
    albums = {a["name"]: a for a in app.get(f"/panel/api/music/browse?source={sid}&kind=album", **panel).json["items"]}
    app.post("/panel/api/cards/link", json={"uid": "04-AB-CD-EF", "music": albums["Abbey Road"], "speaker": "cast:kitchen"},
             **panel)
    # every album with a card, when no source is chosen
    carded = app.get("/book/music?layout=full").get_data(as_text=True)
    assert "Abbey Road" in carded and "Pastel Blues" not in carded and "Now spinning" in carded
    # a source's albums, all of them or only those with a card
    every = app.get(f"/book/music?source={sid}&what=album&cards=0").get_data(as_text=True)
    assert "Abbey Road" in every and "Pastel Blues" in every and "2 albums" in every
    assert "Pastel Blues" not in app.get(f"/book/music?source={sid}&cards=1").get_data(as_text=True)
    assert app.get("/book/music?layout=nope").status_code == 400


def test_books_on_music_assistant_start_partway_and_are_followed(music_app):
    app, player, fake = books_app(music_app)
    calls = []

    def call(command, timeout=15, **args):
        calls.append((command, args))
        if command == "player_queues/get":
            return {"state": "playing", "elapsed_time": 15, "current_item": {"media_item": {
                "uri": "builtin://track/http://abs:13378/api/items/b1/file/121?token=KEY"}}}
        return None
    player._assistant = lambda: object()
    player._call = call
    player.play(["abs|book|b1"], ["ma:den"])
    # the files from the one you're in, then a move 10 s into it once it's playing
    media = next(a for c, a in calls if c == "player_queues/play_media")["media"]
    assert media == ["http://abs:13378/api/items/b1/file/121?token=KEY", "http://abs:13378/api/items/b1/file/122?token=KEY"]
    assert wait_for(lambda: ("player_queues/seek", {"queue_id": "den", "position": 10}) in calls)
    # where it is: the file Music Assistant is on, and how far into it
    assert player.progress.book_of("ma:den")["position"] == 105
    player.progress.report("ma:den")
    assert fake.saved[-1][0] == "b1" and fake.saved[-1][1]["currentTime"] == 105


# ---- NFC readers with a default output ------------------------------------------------

def test_readers_play_on_their_own_speakers_and_remember_them(music_app):
    app, root = music_app
    plex = app.plex
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    player = music_player(app)
    player.airplay, player.cast = FakeAirPlay(), FakeCast()
    album = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=abbey").json["items"][0]
    # a card linked to the album on the kitchen speaker
    app.post("/panel/api/cards/link", json={"uid": "04-AA-BB-CC", "music": {"id": album["id"], "name": album["name"]},
                                            "speaker": "cast:kitchen"}, **panel)

    # a Movie Remote shows up as a reader; the panel gives it a default output
    box = paired_box(plex, app)
    readers = app.get("/panel/api/readers").json
    reader = readers["readers"][0]
    assert (reader["kind"], reader["name"], reader["speakers"]) == ("remote", "Movie Remote", [])
    assert [s["id"] for s in readers["speakers"]] == ["airplay:den", "cast:kitchen", "cast:home"]
    assert app.post(f"/panel/api/readers/{reader['id']}", json={"default": ["airplay:den"]},
                    environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    app.post(f"/panel/api/readers/{reader['id']}", json={"default": ["airplay:den"]}, **panel)

    # the card plays on the box's speaker, not the card's own
    tapped = app.post("/remote/tap", json={"uid": "04:aa:bb:cc"}, headers=box).json
    assert tapped["result"] == "music" and "Den HomePod" in tapped["message"]
    assert wait_for(lambda: player.airplay.played)
    assert not player.cast.played

    # on the box: what's playing on its speakers, and every speaker it could add
    state = app.get("/remote/reader", headers=box).json
    assert state["now"]["title"] == "Come Together" and state["speaker_names"] == ["Den HomePod"]
    assert [(c["id"], c["on"]) for c in state["choices"]] == [("airplay:den", True), ("cast:kitchen", False),
                                                              ("cast:home", False)]
    # adding a speaker carries the album on there too, from where it was, and is remembered
    state = app.post("/remote/reader", json={"action": "speaker", "speaker": "cast:kitchen", "on": True},
                     headers=box).json
    assert state["speakers"] == ["airplay:den", "cast:kitchen"] and not state["is_default"]
    assert wait_for(lambda: player.cast.played and player.cast.played[-1][3] == "Come Together")
    assert app.post("/remote/reader", json={"action": "speaker", "speaker": "airplay:den", "on": False},
                    headers=box).json["speakers"] == ["cast:kitchen"]
    assert app.post("/remote/reader", json={"action": "speaker", "speaker": "cast:kitchen", "on": False},
                    headers=box).status_code == 400  # one has to stay on
    # controls go to what's playing there
    app.post("/remote/reader", json={"action": "toggle"}, headers=box)
    assert ("kitchen", "pause") in player.cast.controls
    assert app.get("/panel/api/readers").json["readers"][0]["speakers"] == ["cast:kitchen"]

    # back to the default, then a new default
    assert app.post("/remote/reader", json={"action": "restore"}, headers=box).json["speakers"] == ["airplay:den"]
    app.post("/remote/reader", json={"action": "speakers", "speakers": ["cast:home"]}, headers=box)
    made = app.post("/remote/reader", json={"action": "make_default"}, headers=box).json
    assert made["default"] == ["cast:home"] and made["is_default"] and made["message"]
    assert app.post("/remote/reader", json={"action": "eject"}, headers=box).status_code == 400

    # it's all remembered
    from plexnfc.readers import Readers
    saved = Readers(str(tmp_path_of(plex) / "readers.json")).get(reader["id"])
    assert saved["default"] == ["cast:home"] and saved["speakers"] == ["cast:home"]
    # removing the box forgets its reader
    device = next(d for d in plex.app_pairings.devices() if d["kind"] == "remote")
    app.post(f"/panel/api/devices/{device['id']}/remove", **panel)
    assert app.get("/panel/api/readers").json["readers"] == []


def tmp_path_of(plex):
    import pathlib
    return pathlib.Path(plex.config.auth_file).parent


def test_home_assistant_readers_play_there_and_open_their_tessera_page(music_app):
    app, root = music_app
    plex = app.plex
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    player = music_player(app)
    player.airplay, player.cast = FakeAirPlay(), FakeCast()
    album = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=abbey").json["items"][0]
    app.post("/panel/api/cards/link", json={"uid": "04-AA-BB-CC", "music": {"id": album["id"], "name": album["name"]},
                                            "speaker": "cast:kitchen"}, **panel)
    ha = pair_home_assistant(plex, app)

    # an unknown reader becomes known; with no speakers yet (its own or Home Assistant's) it says so
    tapped = app.post("/ha/tap", json={"uid": "04aabbcc", "reader": "Office_Test", "reader_id": "dev1"}, headers=ha).json
    assert tapped["result"] == "error" and tapped["reader"] == {"id": "ha:dev1", "page": None}
    assert "Devices › Readers" in tapped["message"] and not player.cast.played
    hub, reader = app.get("/panel/api/readers").json["readers"]
    assert (reader["id"], reader["kind"], reader["name"]) == ("ha:dev1", "homeassistant", "Office_Test")
    assert hub["kind"] == "assistant" and hub["id"].startswith("home:")  # the paired Home Assistant itself

    # given a default output and a Tessera page, the next card plays there and opens the page
    app.post("/panel/api/readers/ha:dev1", json={"default": ["airplay:den"], "page": 3}, **panel)
    assert app.post("/panel/api/readers/ha:dev1", json={"page": 0}, **panel).status_code == 400
    tapped = app.post("/ha/tap", json={"uid": "04aabbcc", "reader": "Office_Test", "reader_id": "dev1"}, headers=ha).json
    assert tapped["reader"] == {"id": "ha:dev1", "page": 3}
    assert wait_for(lambda: player.airplay.played)

    # Home Assistant hears of the reader, its speakers and what's playing there
    players = app.get("/ha/players?wait=0", headers=ha).json
    reader = players["readers"][0]
    assert reader["speakers"] == ["airplay:den"] and reader["now"]["title"] == "Come Together"
    assert any(c["id"] == "cast:home" and not c["on"] for c in reader["choices"])
    # and controls it: a speaker switch, then back to the default
    changed = app.post("/ha/readers/ha:dev1", json={"action": "speaker", "speaker": "cast:home", "on": True},
                       headers=ha).json
    assert changed["reader"]["speakers"] == ["airplay:den", "cast:home"]
    assert app.get(f"/ha/players?tag={players['tag']}&wait=0", headers=ha).json["tag"] != players["tag"]
    assert app.post("/ha/readers/ha:dev1", json={"action": "restore"}, headers=ha).json["reader"]["speakers"] == \
        ["airplay:den"]
    assert app.post("/ha/readers/ha:nope", json={"action": "restore"}, headers=ha).status_code == 400
    assert app.post("/ha/readers/ha:dev1", json={"action": "restore"}).status_code == 401

    # a movie card read there goes to the reader's Marquee player
    pair(plex, "Office")
    app.post("/panel/api/readers/ha:dev1", json={"movie_player": "app:Office"}, **panel)
    plex.cards.link("04-11-22-33", "tmdb-603", "The Matrix", 1999)
    plex.find_library_movie = lambda key, title=None, year=None: NS(key=key, title=title)
    with mock.patch("plexnfc.server.threading.Thread") as thread:
        assert app.post("/ha/tap", json={"uid": "04112233", "reader_id": "dev1"}, headers=ha).json["result"] == "movie"
    assert thread.call_args.kwargs["args"] == ("tmdb-603", "app:Office", False)
    # forgetting it
    app.post("/panel/api/readers/ha:dev1/remove", **panel)
    assert [r["kind"] for r in app.get("/panel/api/readers").json["readers"]] == ["assistant"]


def test_the_paired_home_assistant_is_a_reader_for_all_of_its_readers(music_app):
    app, root = music_app
    plex = app.plex
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    player = music_player(app)
    player.airplay, player.cast = FakeAirPlay(), FakeCast()
    album = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=abbey").json["items"][0]
    app.post("/panel/api/cards/link", json={"uid": "04-AA-BB-CC", "music": {"id": album["id"], "name": album["name"]},
                                            "speaker": "cast:kitchen"}, **panel)
    ha = pair_home_assistant(plex, app)

    # it's listed as soon as it's paired, before any card is read, and set up like a Movie Remote
    (hub,) = app.get("/panel/api/readers").json["readers"]
    assert hub["kind"] == "assistant"
    pair(plex, "Office")
    app.post(f"/panel/api/readers/{hub['id']}", json={"default": ["airplay:den"], "movie_player": "app:Office"},
             **panel)

    # a card read on a phone with the Home Assistant app (a reader with nothing of its own) plays there
    app.post("/ha/tap", json={"uid": "04aabbcc", "reader": "Kevin's iPhone", "reader_id": "phone1"}, headers=ha)
    assert wait_for(lambda: player.airplay.played) and not player.cast.played
    # and so does one Home Assistant can't place
    player.airplay.played.clear()
    app.post("/ha/tap", json={"uid": "04aabbcc"}, headers=ha)
    assert wait_for(lambda: player.airplay.played)
    plex.cards.link("04-11-22-33", "tmdb-603", "The Matrix", 1999)
    plex.find_library_movie = lambda key, title=None, year=None: NS(key=key, title=title)
    with mock.patch("plexnfc.server.threading.Thread") as thread:
        app.post("/ha/tap", json={"uid": "04112233", "reader_id": "phone1"}, headers=ha)
    assert thread.call_args.kwargs["args"] == ("tmdb-603", "app:Office", False)

    # a reader given its own speakers uses them instead
    app.post("/panel/api/readers/ha:phone1", json={"default": ["cast:home"]}, **panel)
    app.post("/ha/tap", json={"uid": "04aabbcc", "reader_id": "phone1"}, headers=ha)
    assert wait_for(lambda: player.cast.played)

    # unpairing Home Assistant removes it
    device = next(d for d in plex.app_pairings.devices() if d["kind"] == "homeassistant")
    app.post(f"/panel/api/devices/{device['id']}/remove", **panel)
    assert [r["kind"] for r in app.get("/panel/api/readers").json["readers"]] == ["homeassistant"]


def test_moving_music_assistant_queue_to_other_speakers(music_app):
    app, _ = music_app
    player = music_player(app)
    calls = []
    player._call = lambda command, timeout=15, **args: calls.append((command, args))
    player._assistant_players = lambda: [{"player_id": "bedroom", "group_members": []}]
    player.move("ma:kitchen", ["ma:bedroom", "ma:office"])
    assert calls == [("player_queues/transfer", {"source_queue_id": "kitchen", "target_queue_id": "bedroom",
                                                 "auto_play": True}),
                     ("players/cmd/set_members", {"target_player": "bedroom", "player_ids_to_add": ["office"],
                                                  "player_ids_to_remove": []})]
    from plexnfc.speakers import SpeakerError
    with pytest.raises(SpeakerError, match="next card"):
        player.move("ma:kitchen", ["cast:kitchen"])


def test_marquee_plays_music_itself_and_shows_now_playing(music_app):
    app, root = music_app
    plex = app.plex
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    (root / "The Beatles" / "Abbey Road" / "01 Come Together.lrc").write_text(
        "[ar: The Beatles]\n[00:12.50]Here come old flat-top\n[00:15.00]He come groovin' up slowly\n")
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    player = music_player(app)
    player.airplay, player.cast = FakeAirPlay(), FakeCast()
    tv = pair(plex, "Den")
    app.post("/tv/status", headers=tv, json={"state": "idle"})  # Marquee is open
    # each Marquee is a speaker
    assert {"id": "marquee:Den", "name": "Den (Marquee)", "kind": "marquee", "group": False}.items() <= \
        next(s for s in player.speakers() if s["id"] == "marquee:Den").items()
    album = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=abbey").json["items"][0]
    player.play([album["id"]], ["marquee:Den"])

    # the track goes to Marquee's inbox, to play itself
    assert wait_for(lambda: (plex.app_inbox.next("Den", 0) or {}).get("action") == "audio")
    sent = plex.app_inbox.next("Den", 0)
    assert sent["title"] == "Come Together" and sent["artist"] == "The Beatles" and sent["type"] == "audio/wav"
    assert sent["url"].startswith("http://marquee:8080/music/file/") and sent["image"]
    app.post("/tv/status", headers=tv, json={"state": "idle", "audio": {"play": sent["play"], "state": "playing",
                                                                         "position": 13}})
    # Now Playing: the track, the album's queue and the timed lyrics
    now = app.get("/tv/nowplaying", headers=tv).json
    assert now["here"] and now["title"] == "Come Together" and now["state"] == "playing"
    assert [t["name"] for t in now["queue"]] == ["Come Together", "Something"] and now["index"] == 0
    assert wait_for(lambda: (app.get("/tv/nowplaying", headers=tv).json["lyrics"] or {}).get("synced"))
    lines = app.get("/tv/nowplaying", headers=tv).json["lyrics"]["lines"]
    assert lines[0] == {"time": 12.5, "text": "Here come old flat-top"} and len(lines) == 2

    # its controls: pause goes to the app; when the track ends, the next one comes
    app.post("/tv/nowplaying", headers=tv, json={"action": "toggle"})
    assert plex.app_inbox.next("Den", sent["id"])["action"] == "audio_pause"
    app.post("/tv/nowplaying", headers=tv, json={"action": "toggle"})
    app.post("/tv/status", headers=tv, json={"state": "idle", "audio": {"play": sent["play"], "ended": sent["play"]}})
    assert wait_for(lambda: (plex.app_inbox.next("Den", 0) or {}).get("title") == "Something")
    assert app.get("/tv/nowplaying", headers=tv).json["index"] == 1
    # Marquee was closed: the queue stops rather than racing through the album
    second = plex.app_inbox.next("Den", 0)
    app.post("/tv/status", headers=tv, json={"state": "idle", "audio": {"play": second["play"], "state": "stopped"}})
    assert wait_for(lambda: not player.sessions)
    assert app.get("/tv/nowplaying", headers=tv).json["state"] == "idle"
    assert app.post("/tv/nowplaying", headers=tv, json={"action": "toggle"}).status_code == 400


def test_a_readers_marquee_screen_shows_its_music(music_app):
    app, root = music_app
    plex = app.plex
    panel = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
    sid = app.post("/panel/api/music/folders", json={"path": str(root)}, **panel).json["folders"][0]["id"]
    player = music_player(app)
    player.airplay, player.cast = FakeAirPlay(), FakeCast()
    tv = pair(plex, "Den")
    album = app.get(f"/panel/api/music/browse?source={sid}&kind=album&q=abbey").json["items"][0]
    app.post("/panel/api/cards/link", json={"uid": "04-AA-BB-CC", "music": {"id": album["id"], "name": album["name"]},
                                            "speaker": "cast:kitchen"}, **panel)
    box = paired_box(plex, app)
    readers = app.get("/panel/api/readers").json
    assert readers["screens"] == ["Den"]
    reader = readers["readers"][0]
    app.post(f"/panel/api/readers/{reader['id']}", json={"default": ["airplay:den"], "screen": "Den"}, **panel)
    assert app.get("/panel/api/readers").json["readers"][0]["screen"] == "Den"

    # Marquee isn't open, and the reader doesn't wake the TV: nothing's shown
    app.post("/remote/tap", json={"uid": "04:aa:bb:cc"}, headers=box)
    assert (plex.app_inbox.next("Den", 0) or {}).get("action") != "nowplaying"
    # open: the tap shows the reader's music there
    app.post("/tv/status", headers=tv, json={"state": "idle"})
    app.post("/remote/tap", json={"uid": "04:aa:bb:cc"}, headers=box)
    assert plex.app_inbox.next("Den", 0) | {"id": 0} == {"id": 0, "action": "nowplaying", "reader": reader["id"]}
    assert wait_for(lambda: app.get("/tv/nowplaying", headers=tv).json["title"] == "Come Together")
    now = app.get("/tv/nowplaying", headers=tv).json
    assert not now["here"] and now["reader"] == "Movie Remote" and now["speakers"] == ["Den HomePod"]
    # its controls are the reader's music's; hiding it stops showing it
    app.post("/tv/nowplaying", headers=tv, json={"action": "seek", "value": 30})
    assert wait_for(lambda: len(player.airplay.played) > 1)  # the track starts again, 30 s in
    app.post("/tv/nowplaying", headers=tv, json={"action": "hide"})
    assert app.get("/tv/nowplaying", headers=tv).json["state"] == "idle"


def test_lyrics_parse_lrc_and_plain():
    from plexnfc.lyrics import parse
    timed = parse("[ti: x]\n[00:01.00][00:05.5]Chorus\n[00:03.250]Verse\n")
    assert timed == {"synced": True, "lines": [{"time": 1.0, "text": "Chorus"}, {"time": 3.25, "text": "Verse"},
                                               {"time": 5.5, "text": "Chorus"}]}
    assert parse("\nOne\n\nTwo\n") == {"synced": False, "lines": [{"time": None, "text": "One"},
                                                                   {"time": None, "text": ""},
                                                                   {"time": None, "text": "Two"}]}
    assert parse("  ") is None
