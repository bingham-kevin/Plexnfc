"""Movie sources besides Plex: folders of movie files and Jellyfin servers (plexnfc/sources.py)."""
import io
import json
from dataclasses import replace
from unittest import mock

import pytest
from PIL import Image

from plexnfc.server import create_app
from plexnfc.sources import FolderSource, Library, SourceError, parse_name
from test_plexnfc import CONFIG, _pairings_in_tmp, pair, plex, requests_get  # noqa: F401 (fixtures)

PANEL = {"REMOTE_ADDR": "127.0.0.1"}


def jpeg(width=20, height=30, color=(200, 0, 0)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def movies_dir(tmp_path):
    """A folder laid out the ways people keep movies."""
    root = tmp_path / "Movies"
    (root / "Alien (1979)").mkdir(parents=True)
    (root / "Alien (1979)" / "Alien (1979).mp4").write_bytes(b"0123456789" * 100)
    (root / "Alien (1979)" / "poster.jpg").write_bytes(jpeg())
    (root / "Alien (1979)" / "Alien (1979)-trailer.mp4").write_bytes(b"x")
    (root / "Alien (1979)" / "Extras").mkdir()
    (root / "Alien (1979)" / "Extras" / "Making of.mp4").write_bytes(b"x")
    (root / "Heat.1995.1080p.BluRay.x264").mkdir()
    (root / "Heat.1995.1080p.BluRay.x264" / "heat-1080p.mkv").write_bytes(b"x")
    (root / "Blade Runner (1982) {tmdb-78}.m4v").write_bytes(b"x")
    (root / "Kill Bill - cd1.avi").write_bytes(b"x")
    (root / "Kill Bill - cd2.avi").write_bytes(b"x")
    (root / "Arrival").mkdir()
    (root / "Arrival" / "Arrival.mkv").write_bytes(b"x")
    (root / "Arrival" / "movie.nfo").write_text(
        "<movie><title>Arrival</title><year>2016</year><plot>Linguists.</plot><runtime>116</runtime>"
        "<set><name>Villeneuve</name></set><tag>Sci-fi</tag><tag>Kids</tag>"
        '<uniqueid type="tmdb">329865</uniqueid><uniqueid type="imdb">tt2543164</uniqueid></movie>')
    (root / "notes.txt").write_text("not a movie")
    return root


def folder_plex(plex, movies_dir, use_plex=True):
    plex.library.add("folder", path=str(movies_dir))
    plex.library.set_plex(use_plex)
    return plex


# ---- folders ----------------------------------------------------------------------------

def test_names_give_title_year_and_ids():
    assert parse_name(["Alien (1979)"]) == ("Alien", 1979, {})
    assert parse_name(["Heat.1995.1080p.BluRay.x264"]) == ("Heat", 1995, {})
    assert parse_name(["Blade Runner (1982) {tmdb-78}"]) == ("Blade Runner", 1982, {"tmdb": "78"})
    assert parse_name(["Up [imdbid-tt1049413]"]) == ("Up", None, {"imdb": "tt1049413"})
    # a file named loosely in a folder named properly
    assert parse_name(["heat-1080p", "Heat (1995)"]) == ("Heat", 1995, {})
    assert parse_name(["2001 A Space Odyssey (1968)"]) == ("2001 A Space Odyssey", 1968, {})


def test_folder_lists_movies_with_stable_keys(movies_dir):
    source = FolderSource({"id": "f1", "kind": "folder", "path": str(movies_dir)})
    movies = {m.title: m for m in source.movies()}
    # trailers, extras and the second part of a split movie aren't movies
    assert sorted(movies) == ["Alien", "Arrival", "Blade Runner", "Heat", "Kill Bill"]
    assert movies["Alien"].year == 1979 and movies["Heat"].year == 1995
    # IDs from the name or the .nfo make the key; otherwise the title and year do
    assert movies["Blade Runner"].key == "tmdb-78"
    assert movies["Arrival"].key == "tmdb-329865"
    assert movies["Alien"].key == "t-alien-1979"
    # each file also has a key of its own, from its path inside the folder
    assert movies["Alien"].source_key.startswith("file-") and len(movies["Alien"].source_key) == len("file-") + 12
    # the same files somewhere else (a share mounted elsewhere) keep their keys
    moved = movies_dir.parent / "Elsewhere"
    movies_dir.rename(moved)
    again = {m.title: m.key for m in FolderSource({"id": "f2", "kind": "folder", "path": str(moved)}).movies()}
    assert again == {t: m.key for t, m in movies.items()}
    assert {m.title: m.source_key for m in FolderSource({"id": "f2", "kind": "folder", "path": str(moved)}).movies()} \
        == {t: m.source_key for t, m in movies.items()}


def test_folder_reads_nfo_details(movies_dir):
    source = FolderSource({"id": "f1", "kind": "folder", "path": str(movies_dir)})
    arrival = source.find("file-tmdb-329865")
    assert arrival.summary == "Linguists." and arrival.duration == 116 * 60_000
    assert arrival.collections == ("Villeneuve",) and arrival.labels == ("Sci-fi", "Kids")
    assert source.collections() == {"Villeneuve": 1}


def test_folder_that_isnt_there_cant_be_added(plex, tmp_path):
    with pytest.raises(SourceError, match="Can't open the folder"):
        plex.library.add("folder", path=str(tmp_path / "nope"))
    assert plex.library.sources == []


def test_folder_movies_in_panel_list_filters_and_book(plex, movies_dir):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    movies = app.get("/panel/api/movies").json["movies"]
    by_title = {m["title"]: m for m in movies}
    # Plex's movie and the folder's, each with its own key
    assert by_title["The Matrix"]["key"] == "tmdb-603" and by_title["The Matrix"]["source"] == "Plex"
    assert by_title["Arrival"]["key"] == "tmdb-329865" and by_title["Arrival"]["source"] == "Movies"
    assert by_title["Arrival"]["url"] == "http://nfc.local:8080/m/tmdb-329865"
    filters = app.get("/panel/api/filters").json
    assert {"title": "Villeneuve", "count": 1} in filters["collections"]
    assert "Kids" in filters["labels"]
    assert [s["name"] for s in filters["sources"]] == ["Plex", "Movies"]
    folder_id = filters["sources"][1]["id"]
    only = app.get(f"/panel/api/movies?source={folder_id}").json["movies"]
    assert "The Matrix" not in {m["title"] for m in only} and len(only) == 5
    plex.section.collection.return_value.items.return_value = []
    assert [m["title"] for m in app.get("/panel/api/movies?collection=Villeneuve").json["movies"]] == ["Arrival"]
    book = app.get("/book").data.decode()
    assert "Arrival" in book and "poster/tmdb-329865?w=1200&amp;h=1800" in book
    assert "Arrival" in app.get("/tags").data.decode()


def test_without_plex_the_folder_is_the_library(plex, movies_dir):
    plex.config = replace(plex.config, plex_token="")
    folder_plex(plex, movies_dir)
    plex.section.all.side_effect = AssertionError("Plex isn't signed in, so it isn't asked")
    app = create_app(plex.config, plex).test_client()
    titles = [m["title"] for m in app.get("/panel/api/movies").json["movies"]]
    assert titles == ["Alien", "Arrival", "Blade Runner", "Heat", "Kill Bill"]
    # Plex can be left out on purpose too
    plex.config = replace(plex.config, plex_token="tok")
    app.post("/panel/api/sources/plex", json={"enabled": False}, environ_base=PANEL)
    assert len(app.get("/panel/api/movies").json["movies"]) == 5


def test_folder_posters(plex, movies_dir):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    alien = next(m for m in plex.movies() if m.title == "Alien")
    # the poster beside the movie, resized for the box
    r = app.get(f"/poster/{alien.key}?w=200&h=300")
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
    img = Image.open(io.BytesIO(r.data))
    assert img.size == (200, 300) and img.getpixel((100, 150))[0] > 150
    # no poster and no cover art inside: a placeholder with its title
    with mock.patch("plexnfc.sources.embedded_poster", return_value=None) as embedded:
        r = app.get("/poster/file-tmdb-78?w=200&h=300")
        app.get("/poster/file-tmdb-78?w=200&h=300")
    assert r.status_code == 200 and Image.open(io.BytesIO(r.data)).size == (200, 300)
    assert embedded.call_count == 1  # finding none is remembered too
    assert app.get("/poster/file-0123456789ab").status_code == 404


def test_folder_poster_from_cover_art_inside_the_file(plex, movies_dir):
    folder_plex(plex, movies_dir)
    with mock.patch("plexnfc.sources.embedded_poster", return_value=jpeg(color=(0, 0, 200))):
        r = create_app(plex.config, plex).test_client().get("/poster/file-tmdb-78?w=40&h=60")
    assert Image.open(io.BytesIO(r.data)).getpixel((20, 30))[2] > 150


def test_folder_movie_plays_in_marquee_from_this_server(plex, movies_dir):
    from test_plexnfc import app_plex
    plex = app_plex(folder_plex(plex, movies_dir))
    app = create_app(plex.config, plex).test_client()
    alien = next(m for m in plex.movies() if m.title == "Alien")
    assert app.get(f"/m/{alien.key}").status_code == 200
    command = plex.app_inbox.next("Den", 0)
    assert command["title"] == "Alien" and command["key"] == alien.key
    assert command["url"].startswith(f"http://nfc.local:8080/files/{alien.source_key}/")
    assert command["url"].endswith("/movie.mp4")
    path = command["url"].removeprefix("http://nfc.local:8080")
    r = app.get(path, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.data == b"0123456789" and r.mimetype == "video/mp4"
    # the address only works for the movie it was made for
    token = path.split("/")[3]
    assert app.get(path.replace(token, "0" * 32)).status_code == 404
    other = next(m for m in plex.movies() if m.title == "Blade Runner").source_key
    assert app.get(f"/files/{other}/{token}/movie.m4v").status_code == 404


def test_mkv_is_converted_by_ffmpeg_for_marquee(plex, movies_dir, tmp_path, monkeypatch):
    from test_plexnfc import app_plex
    plex = app_plex(folder_plex(plex, movies_dir))
    process = mock.Mock()
    process.poll.return_value = None
    made = {}

    def popen(command, **kwargs):
        made["command"] = command
        out = command[-1]
        with open(out, "w") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-PLAYLIST-TYPE:EVENT\n")
        return process

    monkeypatch.setattr("plexnfc.filestream.find_tool", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("plexnfc.filestream.probe_codecs", lambda path: ("hevc", "dts"))
    monkeypatch.setattr("plexnfc.filestream.subprocess.Popen", popen)
    app = create_app(plex.config, plex).test_client()
    heat = next(m for m in plex.movies() if m.title == "Heat")
    app.get(f"/m/{heat.key}")
    url = plex.app_inbox.next("Den", 0)["url"]
    assert "/hls/" in url and url.endswith("/index.m3u8")
    command = made["command"]
    assert command[command.index("-c:v") + 1] == "copy" and "hvc1" in command  # HEVC copied, tagged for Apple
    assert command[command.index("-c:a") + 1] == "aac"  # DTS converted
    r = app.get(url.removeprefix("http://nfc.local:8080"))
    assert r.status_code == 200 and "#EXT-X-START:TIME-OFFSET=0" in r.data.decode()
    assert app.get(url.removeprefix("http://nfc.local:8080").replace("index.m3u8", "../x")).status_code == 404
    plex.library.files.stop_all()
    process.kill.assert_called_once()


def test_mkv_without_ffmpeg_says_what_to_install(plex, movies_dir, monkeypatch):
    from test_plexnfc import app_plex
    plex = app_plex(folder_plex(plex, movies_dir))
    monkeypatch.setattr("plexnfc.filestream.find_tool", lambda name: None)
    heat = next(m for m in plex.movies() if m.title == "Heat")
    r = create_app(plex.config, plex).test_client().get(f"/m/{heat.key}")
    assert r.status_code == 404 and b"Install ffmpeg" in r.data

def test_cards_and_tags_play_folder_movies(plex, movies_dir):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    r = app.post("/panel/api/cards/link", json={"uid": "04-11-22-33", "key": "file-tmdb-78"}, environ_base=PANEL)
    assert r.status_code == 200 and r.json["cards"][0]["title"] == "Blade Runner"
    assert r.json["cards"][0]["key"] == "tmdb-78" and r.json["cards"][0]["year"] == 1982
    p = plex.app_pairings.start("Box", "remote")
    plex.app_pairings.approve(p["pair_id"])
    box = {"Authorization": f"Bearer {plex.app_pairings.poll(p['pair_id'])['secret']}"}
    with mock.patch.object(plex, "play") as play:
        r = app.post("/remote/tap", json={"uid": "04:11:22:33"}, headers=box)
        assert r.status_code == 202 and r.json["title"] == "Blade Runner"
        r = app.post("/remote/tap", json={"uid": "04-99-99-99", "url": "http://x/m/file-tmdb-329865"}, headers=box)
        assert r.status_code == 202 and r.json["title"] == "Arrival"
    assert [c.args[0] for c in play.call_args_list] == ["tmdb-78", "file-tmdb-329865"]
    # Plex keys still work alongside
    assert plex.find_movie("tmdb-603").title == "The Matrix"


def test_new_files_are_found_by_key_without_waiting(plex, movies_dir):
    folder_plex(plex, movies_dir)
    plex.movies()
    (movies_dir / "Up (2009) {tmdb-14160}.mp4").write_bytes(b"x")
    assert plex.find_movie("file-tmdb-14160").title == "Up"


def test_sources_are_saved_and_removed(plex, movies_dir, tmp_path):
    folder_plex(plex, movies_dir, use_plex=False)
    saved = json.loads((tmp_path / "sources.json").read_text())
    assert saved["plex"] is False and saved["sources"][0]["path"] == str(movies_dir)
    again = Library(plex, str(tmp_path))
    assert again.use_plex is False and again.sources[0].name == "Movies"
    app = create_app(plex.config, plex).test_client()
    source_id = again.sources[0].id
    assert app.post(f"/panel/api/sources/{source_id}/remove", environ_base={"REMOTE_ADDR": "192.0.2.9"}).status_code == 403
    r = app.post(f"/panel/api/sources/{source_id}/remove", environ_base=PANEL)
    assert r.status_code == 200 and r.json["sources"] == []


def test_panel_adds_and_refreshes_a_folder(plex, movies_dir):
    app = create_app(plex.config, plex).test_client()
    assert app.post("/panel/api/sources", json={"kind": "folder", "path": str(movies_dir)},
                    environ_base={"REMOTE_ADDR": "192.0.2.9"}).status_code == 403
    r = app.post("/panel/api/sources", json={"kind": "folder", "path": str(movies_dir), "name": "Shelf"},
                 environ_base=PANEL)
    assert r.status_code == 200 and r.json["source"]["name"] == "Shelf" and r.json["source"]["count"] == 5
    (movies_dir / "Up (2009).mp4").write_bytes(b"x")
    r = app.post(f"/panel/api/sources/{r.json['source']['id']}/refresh", environ_base=PANEL)
    assert r.json["count"] == 6
    r = app.post("/panel/api/sources", json={"kind": "folder", "path": "/no/such/folder"}, environ_base=PANEL)
    assert r.status_code == 400 and "Can't open the folder" in r.json["message"]


# ---- Jellyfin ---------------------------------------------------------------------------

JF_MOVIES = [
    {"Id": "a" * 32, "Name": "The Matrix", "ProductionYear": 1999, "ProviderIds": {"Tmdb": "603"},
     "Overview": "Neo.", "RunTimeTicks": 8160 * 10_000_000, "Tags": ["Kids"], "DateCreated": "2024-01-02T03:04:05Z",
     "UserData": {"PlaybackPositionTicks": 600 * 10_000_000}},
    {"Id": "b" * 32, "Name": "Home Video", "ProductionYear": 2020, "ProviderIds": {}},
]


@pytest.fixture
def jellyfin_server(monkeypatch):
    """A Jellyfin server answering requests.request (and sign-in, through requests.post)."""
    calls = []

    def respond(method, url, params=None, json=None, headers=None, timeout=None):
        calls.append((method, url, params or {}, headers or {}))
        path = url.split(":8096", 1)[1]
        body, content, ctype = None, b"", "application/json"
        if path.endswith("/Images/Primary"):
            content, ctype = jpeg(color=(0, 200, 0)), "image/jpeg"
        elif path.endswith("/Items") and (params or {}).get("IncludeItemTypes") == "BoxSet":
            body = {"Items": [{"Id": "set1", "Name": "The Matrix Collection"}]}
        elif path.endswith("/Items") and (params or {}).get("ParentId") == "set1":
            body = {"Items": [{"Id": "a" * 32}]}
        elif path.endswith("/Items"):
            body = {"Items": JF_MOVIES}
        if body is not None:
            content = __import__("json").dumps(body).encode()
        return mock.Mock(ok=True, status_code=200, content=content, headers={"Content-Type": ctype},
                         json=lambda: __import__("json").loads(content))

    monkeypatch.setattr("plexnfc.jellyfin.requests.request", respond)
    return calls


def jf_source_plex(plex, **settings):
    plex.library.add("jellyfin", url="http://jf:8096", token="key", name="Den Jellyfin", **settings)
    return plex


def test_jellyfin_source_lists_movies_with_its_own_keys(plex, jellyfin_server):
    jf_source_plex(plex)
    app = create_app(plex.config, plex).test_client()
    movies = {m["title"]: m for m in app.get("/panel/api/movies").json["movies"]}
    # the same movie in Plex and Jellyfin is one movie, with Plex's key
    assert len(movies) == 2
    assert movies["The Matrix"]["key"] == "tmdb-603" and movies["The Matrix"]["sources"] == ["Plex", "Den Jellyfin"]
    assert movies["Home Video"]["key"] == "t-homevideo-2020"
    found = plex.find_library_movie("jf-tmdb-603")  # the key it briefly had still finds it
    matrix = found.copies[1]
    assert found.key == "tmdb-603" and found.copies[0] is plex._movie
    assert matrix.summary == "Neo." and matrix.duration == 8160 * 1000 and matrix.viewOffset == 600_000
    assert plex.find_movie("jf-" + "b" * 32).title == "Home Video"
    filters = app.get("/panel/api/filters").json
    assert {"title": "The Matrix Collection", "count": 1} in filters["collections"] and "Kids" in filters["labels"]
    plex.section.collection.return_value.items.return_value = []
    in_set = app.get("/panel/api/movies?collection=The Matrix Collection").json["movies"]
    assert [m["key"] for m in in_set] == ["tmdb-603"]
    with_label = app.get("/panel/api/movies?label=Kids").json["movies"]
    assert [m["key"] for m in with_label] == ["tmdb-603"]


def test_jellyfin_source_posters(plex, jellyfin_server):
    jf_source_plex(plex)
    r = create_app(plex.config, plex).test_client().get("/poster/t-homevideo-2020?w=200&h=300")
    assert r.status_code == 200 and Image.open(io.BytesIO(r.data)).getpixel((100, 150))[1] > 150
    method, url, params, headers = jellyfin_server[-1]
    assert url == f"http://jf:8096/Items/{'b' * 32}/Images/Primary" and params == {"maxWidth": 200, "maxHeight": 300}
    assert headers["Authorization"] == 'MediaBrowser Token="key"'


def test_jellyfin_source_streams_to_marquee(plex, jellyfin_server):
    from test_plexnfc import app_plex
    plex = app_plex(jf_source_plex(plex))
    app = create_app(plex.config, plex).test_client()
    # Plex comes first, so the movie in both streams from Plex
    app.get("/m/tmdb-603")
    assert "/video/:/transcode/universal/start.m3u8?" in plex.app_inbox.next("Den", 0)["url"]
    # put Jellyfin first, and it streams from Jellyfin
    jf_id = plex.library.sources[0].id
    assert app.post("/panel/api/sources/order", json={"order": [jf_id, "plex"]}, environ_base=PANEL).json["order"] \
        == [jf_id, "plex"]
    app.get("/m/tmdb-603")
    command = plex.app_inbox.next("Den", 0)
    # through this server, which can reach Jellyfin when the Apple TV can't (a Tailscale address, say)
    prefix = f"http://nfc.local:8080/jf/{jf_id}/"
    assert command["url"].startswith(prefix) and f"/Videos/{'a' * 32}/master.m3u8?" in command["url"]
    assert "ApiKey=key" in command["url"]
    assert command["key"] == "tmdb-603" and command["poster"] == "/poster/tmdb-603?w=600&h=900"
    # playlists and segments are passed on from Jellyfin, ranges and all
    upstream = mock.Mock(status_code=206, headers={"Content-Type": "video/mp4", "Content-Range": "bytes 0-3/10",
                                                   "Set-Cookie": "no"}, iter_content=lambda size: [b"abcd"])
    with mock.patch("plexnfc.server.requests.get", return_value=upstream) as get:
        path = command["url"].removeprefix("http://nfc.local:8080").replace("master.m3u8", "hls1/main/0.mp4")
        r = app.get(path, headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.data == b"abcd" and r.headers["Content-Range"] == "bytes 0-3/10"
    assert "Set-Cookie" not in r.headers
    assert get.call_args.args[0] == f"http://jf:8096/Videos/{'a' * 32}/hls1/main/0.mp4"
    assert get.call_args.kwargs["params"]["ApiKey"] == ["key"] and get.call_args.kwargs["headers"] == {"Range": "bytes=0-3"}
    token = path.split("/")[3]
    assert app.get(path.replace(token, "0" * 32)).status_code == 404

def test_jellyfin_sign_in_keeps_only_the_token(plex, jellyfin_server, tmp_path, monkeypatch):
    post = mock.Mock(return_value=mock.Mock(ok=True, status_code=200, json=lambda: {
        "AccessToken": "usertoken", "User": {"Id": "u1", "Name": "kim"}}))
    monkeypatch.setattr("plexnfc.jellyfin.requests.post", post)
    app = create_app(plex.config, plex).test_client()
    r = app.post("/panel/api/sources", environ_base=PANEL, json={
        "kind": "jellyfin", "url": "http://jf:8096/", "username": "kim", "password": "typed-by-the-user"})
    assert r.status_code == 200 and r.json["source"]["user"] == "kim" and "token" not in r.json["source"]
    assert post.call_args.kwargs["json"] == {"Username": "kim", "Pw": "typed-by-the-user"}
    saved = (tmp_path / "sources.json").read_text()
    assert "typed-by-the-user" not in saved and "usertoken" in saved
    # a user's movies come from their own view of the library, with the client named
    method, url, params, headers = jellyfin_server[-1]
    assert url == "http://jf:8096/Users/u1/Items" and 'Token="usertoken"' in headers["Authorization"]
    assert 'Client="Marquee"' in headers["Authorization"]
    assert "usertoken" not in json.dumps(app.get("/panel/api/sources").json)


def test_jellyfin_wrong_password_is_reported(plex, monkeypatch):
    monkeypatch.setattr("plexnfc.jellyfin.requests.post", mock.Mock(return_value=mock.Mock(ok=False, status_code=401)))
    r = create_app(plex.config, plex).test_client().post("/panel/api/sources", environ_base=PANEL, json={
        "kind": "jellyfin", "url": "http://jf:8096", "username": "kim", "password": "wrong"})
    assert r.status_code == 400 and "didn't accept" in r.json["message"]
    assert plex.library.sources == []


def test_jellyfin_needs_an_address_and_a_key(plex):
    app = create_app(plex.config, plex).test_client()
    r = app.post("/panel/api/sources", environ_base=PANEL, json={"kind": "jellyfin", "url": "jf", "api_key": "k"})
    assert r.status_code == 400 and "address" in r.json["message"]
    r = app.post("/panel/api/sources", environ_base=PANEL, json={"kind": "jellyfin", "url": "http://jf:8096"})
    assert r.status_code == 400 and "API key" in r.json["message"]


def test_marquee_browses_every_source(plex, movies_dir, jellyfin_server):
    from test_plexnfc import app_plex
    plex = app_plex(folder_plex(jf_source_plex(plex), movies_dir))
    plex.section.recentlyAdded.return_value = [plex._movie]
    plex.section.collections.return_value = []
    app = create_app(plex.config, plex).test_client()
    headers = pair(plex, "Den")
    rows = app.get("/tv/library", headers=headers).json["rows"]
    assert [r["title"] for r in rows] == ["Recently Added", "New in Den Jellyfin", "New in Movies"]
    page = app.get("/tv/movies?start=0&size=3", headers=headers).json
    assert page["total"] == 7 and [m["title"] for m in page["items"]] == ["Alien", "Arrival", "Blade Runner"]
    plex.section.search.return_value = []
    found = app.get("/tv/search?q=matrix", headers=headers).json["items"]
    assert [m["key"] for m in found] == ["tmdb-603"]


# ---- one movie, whichever sources have it -------------------------------------------------

@pytest.fixture
def matrix_file(movies_dir):
    """The Matrix as a file with no ID: found to be Plex's The Matrix by its title and year."""
    (movies_dir / "The Matrix (1999)").mkdir()
    (movies_dir / "The Matrix (1999)" / "The.Matrix.1999.1080p.mp4").write_bytes(b"x" * 100)
    return movies_dir


def test_the_same_movie_in_plex_and_a_folder_is_one_movie(plex, matrix_file):
    folder_plex(plex, matrix_file)
    rows = create_app(plex.config, plex).test_client().get("/panel/api/movies").json["movies"]
    matrix = [m for m in rows if m["title"] == "The Matrix"]
    assert len(matrix) == 1 and matrix[0]["key"] == "tmdb-603" and matrix[0]["sources"] == ["Plex", "Movies"]
    assert plex.find_library_movie("t-thematrix-1999").key == "tmdb-603"


def test_movies_with_the_same_title_but_different_ids_stay_apart(plex, movies_dir):
    (movies_dir / "The Matrix (1999) {tmdb-999}.mp4").write_bytes(b"x")
    folder_plex(plex, movies_dir)
    keys = [m["key"] for m in create_app(plex.config, plex).test_client().get("/panel/api/movies").json["movies"]
            if m["title"] == "The Matrix"]
    assert sorted(keys) == ["tmdb-603", "tmdb-999"]


def test_source_order_picks_where_marquee_streams_from(plex, matrix_file):
    from test_plexnfc import app_plex
    plex = app_plex(folder_plex(plex, matrix_file))
    app = create_app(plex.config, plex).test_client()
    folder_id = plex.library.sources[0].id
    app.get("/m/tmdb-603")
    assert "X-Plex-Token" in plex.app_inbox.next("Den", 0)["url"]
    assert plex.playing_source("app:Den", "tmdb-603") == "Plex"
    plex.library.set_order([folder_id, "plex"])
    app.get("/m/tmdb-603")
    assert plex.app_inbox.next("Den", 0)["url"].endswith("/movie.mp4")


def test_a_source_that_fails_falls_back_to_the_next(plex, matrix_file, monkeypatch):
    from test_plexnfc import app_plex
    plex = app_plex(folder_plex(plex, matrix_file))
    monkeypatch.setattr(type(plex), "plex_url", property(lambda self: (_ for _ in ()).throw(OSError("Plex is off"))))
    r = create_app(plex.config, plex).test_client().get("/m/tmdb-603")
    assert r.status_code == 200
    assert plex.app_inbox.next("Den", 0)["url"].endswith("/movie.mp4")
    assert plex.playing_source("app:Den", "tmdb-603") == "Movies"


def test_a_plex_card_plays_from_the_folder_once_plex_is_gone(plex, matrix_file):
    from test_plexnfc import app_plex
    plex = app_plex(plex)
    plex.cards.link("04-11-22-33", "tmdb-603", "The Matrix")  # linked before, with no year
    plex.config = replace(plex.config, plex_token="")  # signed out of Plex
    folder_plex(plex, matrix_file)
    p = plex.app_pairings.start("Box", "remote")
    plex.app_pairings.approve(p["pair_id"])
    box = {"Authorization": f"Bearer {plex.app_pairings.poll(p['pair_id'])['secret']}"}
    app = create_app(plex.config, plex).test_client()
    r = app.post("/remote/tap", json={"uid": "04-11-22-33"}, headers=box)
    assert r.status_code == 202 and r.json["title"] == "The Matrix"
    command = None
    for _ in range(50):
        if command := plex.app_inbox.next("Den", 0):
            break
        __import__("time").sleep(0.05)
    assert command["url"].endswith("/movie.mp4") and command["key"] == "t-thematrix-1999"
    # Now Playing says where it came from
    app.post("/tv/status", headers=pair(plex, "Den"), json={"state": "playing", "key": command["key"],
                                                            "title": "The Matrix", "position": 5, "duration": 100})
    den = next(d for d in app.get("/panel/api/overview").json["devices"] if d["name"] == "Den")
    assert den["status"]["source"] == "Movies"
    assert app.get("/now").json["source"] == "Movies"


# ---- choosing a folder, and matching folder movies with TMDB --------------------------------

def test_browse_shows_folders_to_the_panel_only(plex, movies_dir):
    app = create_app(plex.config, plex).test_client()
    r = app.get(f"/panel/api/browse?path={movies_dir}", environ_base=PANEL)
    assert r.status_code == 200
    assert r.json["dirs"] == ["Alien (1979)", "Arrival", "Heat.1995.1080p.BluRay.x264"]
    assert r.json["videos"] == 3 and r.json["parent"] == str(movies_dir.parent)
    assert isinstance(r.json["roots"], list)
    # another device on the network can't look around the server's files
    assert app.get(f"/panel/api/browse?path={movies_dir}", environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    assert app.get(f"/panel/api/browse?path={movies_dir}/nope", environ_base=PANEL).status_code == 400


TMDB_MOVIES = {
    348: {"id": 348, "title": "Alien", "release_date": "1979-05-25", "overview": "In space.", "runtime": 117,
          "imdb_id": "tt0078748", "poster_path": "/alien.jpg", "belongs_to_collection": {"name": "Alien Collection"}},
    679: {"id": 679, "title": "Aliens", "release_date": "1986-07-18", "overview": "More.", "poster_path": None},
    949: {"id": 949, "title": "Heat", "release_date": "1995-12-15", "overview": "LA.", "runtime": 170,
          "imdb_id": "tt0113277", "poster_path": "/heat.jpg"},
    950: {"id": 950, "title": "Heat", "release_date": "1986-03-14", "overview": "Vegas."},
    24: {"id": 24, "title": "Kill Bill: Vol. 1", "release_date": "2003-10-10", "overview": "Revenge."},
}


@pytest.fixture
def tmdb(monkeypatch):
    """TMDB's API and images, answering for the movies above (and only the key "goodkey")."""
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, params))
        if url.startswith("https://image.tmdb.org/"):
            return mock.Mock(ok=True, content=jpeg(color=(0, 0, 200)), status_code=200)
        if (params or {}).get("api_key") != "goodkey":
            return mock.Mock(ok=False, status_code=401)
        path = url.removeprefix("https://api.themoviedb.org/3")
        if path == "/search/movie":
            words = params["query"].lower()
            found = [m for m in TMDB_MOVIES.values() if words in m["title"].lower()]
            if params.get("year"):
                found = [m for m in found if m["release_date"].startswith(str(params["year"]))]
            return mock.Mock(ok=True, status_code=200, json=lambda: {"results": found})
        movie = TMDB_MOVIES.get(int(path.rsplit("/", 1)[1]))
        return mock.Mock(ok=bool(movie), status_code=200 if movie else 404, json=lambda: movie)

    monkeypatch.setattr("requests.get", get)
    return calls


def file_id(app, title):
    return next(m for m in app.get("/panel/api/movies").json["movies"] if m["title"] == title)["files"][0]["id"]


def test_tmdb_key_is_checked_and_never_shown(plex, movies_dir, tmdb, tmp_path):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    assert app.get("/panel/api/sources").json["tmdb"] is False
    r = app.post("/panel/api/sources/tmdb", json={"key": "badkey"}, environ_base=PANEL)
    assert r.status_code == 400 and "API key" in r.json["message"]
    assert app.post("/panel/api/sources/tmdb", json={"key": "goodkey"}, environ_base={"REMOTE_ADDR": "192.0.2.50"}
                    ).status_code == 403
    r = app.post("/panel/api/sources/tmdb", json={"key": "goodkey"}, environ_base=PANEL)
    assert r.status_code == 200 and r.json["tmdb"] is True and "goodkey" not in r.data.decode()
    assert json.loads((tmp_path / "sources.json").read_text())["tmdb_key"] == "goodkey"
    assert "goodkey" not in app.get("/panel/api/sources").data.decode()


def test_matching_a_folder_movie_with_tmdb(plex, movies_dir, tmdb, tmp_path):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    alien = file_id(app, "Alien")
    # no key yet: searching says where to add one
    assert "TMDB API key" in app.get("/panel/api/match/search?q=Alien").json["message"]
    app.post("/panel/api/sources/tmdb", json={"key": "goodkey"}, environ_base=PANEL)
    results = app.get("/panel/api/match/search?q=Alien&year=1979").json["results"]
    assert [r["id"] for r in results] == [348] and results[0]["poster"] == "https://image.tmdb.org/t/p/w342/alien.jpg"
    before = sorted(p.name for p in movies_dir.rglob("*"))

    r = app.post(f"/panel/api/files/{alien}/match", json={"tmdb_id": 348}, environ_base=PANEL)
    assert r.status_code == 200
    m = r.json["movie"]
    assert m["key"] == "tmdb-348" and m["matched"] == "manual" and m["summary"] == "In space."
    assert m["ids"] == {"tmdb": "348", "imdb": "tt0078748"} and m["collections"] == ["Alien Collection"]
    assert m["file"]["title"] == "Alien"
    # it's listed under its new key, and tags and cards made with the old one still find it
    row = next(x for x in app.get("/panel/api/movies").json["movies"] if x["title"] == "Alien")
    assert row["key"] == "tmdb-348" and row["files"][0]["matched"] == "manual"
    assert plex.find_movie("t-alien-1979").title == "Alien"
    assert {"title": "Alien Collection", "count": 1} in app.get("/panel/api/filters").json["collections"]
    # a movie matched by hand shows TMDB's poster, even with a poster file beside it
    poster = Image.open(io.BytesIO(app.get("/poster/tmdb-348?w=40&h=60").data))
    assert poster.getpixel((20, 30))[2] > 150
    # nothing was written to the share; the match is kept in the data folder, and survives a restart
    assert sorted(p.name for p in movies_dir.rglob("*")) == before
    assert alien in json.loads((tmp_path / "folder_metadata.json").read_text())
    assert Library(plex, str(tmp_path)).find("tmdb-348").title == "Alien"

    r = app.post(f"/panel/api/files/{alien}/reset", environ_base=PANEL)
    assert r.json["movie"]["key"] == "t-alien-1979" and r.json["movie"]["matched"] == ""
    assert plex.find_movie("t-alien-1979").summary == ""


def test_editing_a_folder_movie_by_hand(plex, movies_dir):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    heat = file_id(app, "Heat")
    assert app.post(f"/panel/api/files/{heat}/edit", json={"title": "Heat"},
                    environ_base={"REMOTE_ADDR": "192.0.2.50"}).status_code == 403
    assert app.post(f"/panel/api/files/{heat}/edit", json={"year": "95"}, environ_base=PANEL).status_code == 400
    assert app.post(f"/panel/api/files/{heat}/edit", json={"title": " "}, environ_base=PANEL).status_code == 400
    r = app.post(f"/panel/api/files/{heat}/edit", environ_base=PANEL, json={
        "title": "Heat (Director's Cut)", "year": "1995", "summary": "Long.", "collections": "Mann, Crime",
        "labels": "Favourites"})
    m = r.json["movie"]
    assert m["key"] == "t-heatdirectorscut-1995" and m["collections"] == ["Mann", "Crime"]
    assert m["labels"] == ["Favourites"] and m["edited"] and not m["matched"]
    assert plex.find_movie("t-heat-1995").title == "Heat (Director's Cut)"  # the old key
    # a TMDB ID typed by hand gives it that key
    r = app.post(f"/panel/api/files/{heat}/edit", json={"tmdb": "949"}, environ_base=PANEL)
    assert r.json["movie"]["key"] == "tmdb-949" and r.json["movie"]["title"] == "Heat (Director's Cut)"
    assert app.post(f"/panel/api/files/{heat}/edit", json={"tmdb": "x"}, environ_base=PANEL).status_code == 400
    assert app.get("/panel/api/files/file-000000000000").status_code == 400


def test_match_all_takes_only_sure_matches(plex, movies_dir, tmdb):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    folder = plex.library.sources[0].id
    assert "TMDB API key" in app.post(f"/panel/api/sources/{folder}/match", environ_base=PANEL).json["message"]
    app.post("/panel/api/sources/tmdb", json={"key": "goodkey"}, environ_base=PANEL)
    with mock.patch("plexnfc.sources.threading.Thread") as thread:
        r = app.post(f"/panel/api/sources/{folder}/match", environ_base=PANEL)
    assert r.json["job"] == {"running": True, "done": 0, "total": 3, "matched": 0, "error": None}
    thread.return_value.start.assert_called_once()
    plex.library._match_all(*thread.call_args.kwargs["args"])  # run it here
    job = app.get("/panel/api/sources").json["matching"][folder]
    # Alien and Heat (the 1995 one) are surely those; "Kill Bill" isn't surely "Kill Bill: Vol. 1"
    assert job == {"running": False, "done": 3, "total": 3, "matched": 2, "error": None}
    keys = {m["title"]: m["key"] for m in app.get("/panel/api/movies").json["movies"]}
    assert keys["Alien"] == "tmdb-348" and keys["Heat"] == "tmdb-949" and keys["Kill Bill"] == "t-killbill"
    assert plex.library.folder_copy(file_id(app, "Heat")).matched == "auto"
    # a movie matched automatically keeps the poster beside it
    poster = Image.open(io.BytesIO(app.get("/poster/tmdb-348?w=40&h=60").data))
    assert poster.getpixel((20, 30))[0] > 150


def test_best_match_allows_a_year_off_by_one(tmdb):
    from plexnfc.metadata import Tmdb
    assert Tmdb("goodkey").best_match("Alien", 1980)["id"] == 348
    assert Tmdb("goodkey").best_match("Heat", None) is None  # two of them
    assert Tmdb("goodkey").best_match("Aliens", None)["id"] == 679


def test_book_can_hold_only_movies_with_cards(plex, movies_dir):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    app.post("/panel/api/cards/link", json={"uid": "04-11-22-33", "key": "tmdb-78"}, environ_base=PANEL)
    book = app.get("/book?cards=1").data.decode()
    assert "Blade Runner" in book and "Arrival" not in book and "The Matrix" not in book
    assert "Arrival" in app.get("/book").data.decode()


# ---- more Plex servers ---------------------------------------------------------------------

@pytest.fixture
def office_plex(monkeypatch):
    """A second Plex server, "Office", shared with the account: The Matrix (as on the first
    server) and Heat, which only it has."""
    from types import SimpleNamespace as NS
    movies = [
        NS(title="The Matrix", year=1999, ratingKey=7, thumb="/m.jpg", summary="", duration=0, viewOffset=0,
           guids=[NS(id="tmdb://603")], collections=[], labels=[], addedAt=None),
        NS(title="Heat", year=1995, ratingKey=8, thumb="/h.jpg", summary="LA.", duration=100, viewOffset=0,
           guids=[NS(id="tmdb://949")], collections=[NS(tag="Mann")], labels=[NS(tag="Crime")], addedAt=None),
    ]
    section = NS(type="movie", title="Films", all=lambda: movies)
    server = mock.Mock(_baseurl="http://office:32400")
    server.library.sections.return_value = [section]
    server.transcodeImage.side_effect = lambda thumb, height, width: f"http://office:32400/photo{thumb}"
    made = mock.Mock(return_value=server)
    monkeypatch.setattr("plexapi.server.PlexServer", made)
    resource = mock.Mock(clientIdentifier="office-id", owned=False, provides="server", accessToken="office-token")
    resource.name = "Office"
    resource.connect.return_value = server
    mine = mock.Mock(clientIdentifier="home-id", owned=True, provides="server")
    mine.name = "Home"
    monkeypatch.setattr("plexnfc.sources.plex_account_servers", lambda token: [mine, resource])
    return NS(server=server, made=made, resource=resource)


def test_plex_servers_on_the_account_can_be_added(plex, office_plex, monkeypatch):
    plex.server.machineIdentifier = "home-id"
    app = create_app(plex.config, plex).test_client()
    servers = app.get("/panel/api/plex/servers").json["servers"]
    assert [(s["name"], s["owned"], s["signed_in"], s["added"]) for s in servers] == [
        ("Home", True, True, False), ("Office", False, False, False)]
    r = app.post("/panel/api/sources", json={"kind": "plex", "machine": "office-id"}, environ_base=PANEL)
    assert r.status_code == 200, r.json
    source = r.json["source"]
    assert source["name"] == "Office" and source["url"] == "http://office:32400" and source["count"] == 2
    assert "office-token" not in r.data.decode()
    office_plex.made.assert_called_with("http://office:32400", "office-token", timeout=15)
    assert next(s for s in app.get("/panel/api/plex/servers").json["servers"] if s["name"] == "Office")["added"]
    # the same server again is refused
    assert app.post("/panel/api/sources", json={"kind": "plex", "machine": "office-id"},
                    environ_base=PANEL).status_code == 400

    # one library: The Matrix from both servers is one movie; Heat comes from Office
    movies = {m["title"]: m for m in app.get("/panel/api/movies").json["movies"]}
    assert movies["The Matrix"]["sources"] == ["Plex", "Office"] and movies["Heat"]["key"] == "tmdb-949"
    assert {"title": "Mann", "count": 1} in app.get("/panel/api/filters").json["collections"]
    # Heat streams to Marquee from the Office server, with its own token
    with mock.patch("plexnfc.plex.requests.get", return_value=mock.Mock(status_code=200)):
        details = plex.app_play_details("Den", "tmdb-949")
    assert details["url"].startswith("http://office:32400/video/:/transcode/universal/start.m3u8?")
    assert "X-Plex-Token=office-token" in details["url"] and "%2Flibrary%2Fmetadata%2F8" in details["url"]
    # and its poster comes from there
    with mock.patch("requests.get", return_value=mock.Mock(ok=True, content=jpeg(), headers={})) as get:
        assert app.get("/poster/tmdb-949").status_code == 200
    assert get.call_args.args[0] == "http://office:32400/photo/h.jpg"


def test_a_plex_server_can_be_added_by_its_address(plex, office_plex):
    app = create_app(plex.config, plex).test_client()
    r = app.post("/panel/api/sources", environ_base=PANEL, json={
        "kind": "plex", "url": "http://office:32400/", "token": "typed", "library": "Films", "name": "Office"})
    assert r.status_code == 200 and r.json["source"]["library"] == "Films"
    office_plex.made.assert_called_with("http://office:32400", "typed", timeout=15)
    r = app.post("/panel/api/sources", environ_base=PANEL, json={"kind": "plex", "url": "http://x:32400", "token": "t",
                                                                  "library": "Nope"})
    assert r.status_code == 400 and "no movie library named Nope" in r.json["message"]
    assert app.post("/panel/api/sources", environ_base=PANEL, json={"kind": "plex", "url": "office"}).status_code == 400


def test_one_page_of_the_book_can_be_reprinted(plex, movies_dir):
    folder_plex(plex, movies_dir)
    app = create_app(plex.config, plex).test_client()
    whole = app.get("/book?layout=full").data.decode()
    assert whole.count('<section class="sheet">') == 6 and 'class="cover"' in whole
    # the page Heat is on, as in the whole book: its number at the foot, and nothing else
    page = app.get("/book?layout=full&movie=t-heat-1995").data.decode()
    assert page.count("<section") == 1 and "Heat" in page and "Alien" not in page and 'class="cover"' not in page
    sheet = page.split("<body>")[1].removesuffix("</body></html>")
    assert sheet in whole and "&#9670; 6 &#9670;" in sheet  # the very same page, page 6
    # four to a page: the whole sheet Heat is on, with the movies beside it
    grid = app.get("/book?layout=grid&movie=t-heat-1995").data.decode()
    assert grid.count("<section") == 1 and grid.count('class="card"') == 4  # Heat is 4th: the first sheet of 4
    # by page number: 1 is the cover, the last is the remote control page
    assert 'class="cover"' in app.get("/book?page=1").data.decode()
    assert 'class="remote"' in app.get("/book?page=9").data.decode()  # cover, contents, 6 sheets, remote
    r = app.get("/book?page=10")
    assert r.status_code == 404 and "pages 1 to 9" in r.data.decode()
    # a movie that isn't in this book (another collection, say)
    plex.section.collection.return_value.items.return_value = []
    r = app.get("/book?collection=Villeneuve&movie=t-heat-1995")
    assert r.status_code == 404 and "isn&#x27;t in this book" in r.data.decode()
