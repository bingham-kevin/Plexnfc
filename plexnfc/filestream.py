"""Movie files for the Marquee players, and posters for movies without one.

A movie from a folder source (see sources.py) reaches Marquee from Marquee Server
itself, at an address signed for that movie (``/files/KEY/TOKEN/...``), so only
players that were handed the address can fetch it:

- MP4, M4V and MOV files are sent as they are; AVPlayer plays them directly.
- Anything else (MKV, AVI...) needs ffmpeg, which repackages the video (or converts it,
  if Apple TV can't play its format) into an HLS stream as it goes.

Posters come from a poster file beside the movie, else the cover art inside the file
(read with ffmpeg), else a plain card with the title on it.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import textwrap
import threading
import time
import uuid

log = logging.getLogger(__name__)

DIRECT_TYPES = {".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime"}
HLS_IDLE = 15 * 60  # a stream nobody has asked for in this long is stopped
HLS_MAX = 3  # streams converting at once; the oldest stops when another starts


def find_tool(name: str) -> str | None:
    """ffmpeg or ffprobe: FFMPEG/FFPROBE, the PATH, or Homebrew's (the Mac app's PATH has none)."""
    if path := os.environ.get(name.upper()):
        return path if os.path.exists(path) else None
    return shutil.which(name) or next(
        (p for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
         if os.access(p := os.path.join(d, name), os.X_OK)), None)


class FileStreams:
    """Signed addresses for movie files, and the ffmpeg streams for files that need one."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._secret: bytes | None = None
        self._lock = threading.Lock()
        self._streams: dict[str, dict] = {}  # stream ID -> {"process", "dir", "key", "used"}

    # ---- signed addresses ---------------------------------------------------------

    @property
    def secret(self) -> bytes:
        if self._secret is None:
            path = os.path.join(self.data_dir, "stream_secret")
            try:
                with open(path, "rb") as f:
                    self._secret = f.read().strip()
            except OSError:
                self._secret = b""
            if len(self._secret) < 32:
                self._secret = secrets.token_hex(32).encode()
                with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                    f.write(self._secret)
        return self._secret

    def token(self, key: str) -> str:
        return hmac.new(self.secret, key.encode(), hashlib.sha256).hexdigest()[:32]

    def check(self, key: str, token: str) -> bool:
        return hmac.compare_digest(self.token(key), token or "")

    def url(self, base_url: str, key: str, path: str, start: int, player: str) -> str:
        """The address Marquee plays this movie file from."""
        ext = os.path.splitext(path)[1].lower()
        prefix = f"{base_url}/files/{key}/{self.token(key)}"
        if ext in DIRECT_TYPES:
            return f"{prefix}/movie{ext}"
        return f"{prefix}/hls/{self.start_hls(key, path, start)}/index.m3u8"

    # ---- HLS streams made by ffmpeg -------------------------------------------------

    def start_hls(self, key: str, path: str, start: int = 0) -> str:
        """Start ffmpeg making an HLS stream of ``path`` from ``start`` seconds; its stream ID."""
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            from .plex import PlexNFCError
            raise PlexNFCError(f"Marquee can't play {os.path.splitext(path)[1] or 'these'} files by itself. Install "
                               "ffmpeg on the server (e.g. brew install ffmpeg) so it can convert them.")
        video, audio = probe_codecs(path)
        stream_id = uuid.uuid4().hex
        out = tempfile.mkdtemp(prefix=f"marquee-{stream_id[:8]}-")
        command = [ffmpeg, "-nostdin", "-loglevel", "error"]
        if start:
            command += ["-ss", str(int(start))]
        command += ["-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
        if video == "h264":
            command += ["-c:v", "copy"]
        elif video == "hevc":
            command += ["-c:v", "copy", "-tag:v", "hvc1"]  # Apple wants HEVC tagged hvc1
        else:  # MPEG-4, VC-1...: Apple TV can't play them, so convert
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
        command += ["-c:a", "copy"] if audio in ("aac", "ac3", "eac3") else ["-c:a", "aac", "-b:a", "384k"]
        command += ["-f", "hls", "-hls_time", "6", "-hls_list_size", "0", "-hls_playlist_type", "event",
                    "-hls_segment_type", "fmp4", "-hls_flags", "independent_segments",
                    "-hls_segment_filename", os.path.join(out, "seg%05d.m4s"), os.path.join(out, "index.m3u8")]
        log.info("Converting %s for Marquee (video %s, audio %s)", os.path.basename(path), video, audio)
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with self._lock:
            self._streams[stream_id] = {"process": process, "dir": out, "key": key, "used": time.monotonic()}
            self._tidy()
        return stream_id

    def _tidy(self) -> None:
        """Stop streams nobody's watching, and the oldest beyond HLS_MAX. Holds the lock."""
        now = time.monotonic()
        ordered = sorted(self._streams.items(), key=lambda s: s[1]["used"], reverse=True)
        for i, (stream_id, stream) in enumerate(ordered):
            if i >= HLS_MAX or now - stream["used"] > HLS_IDLE:
                self._stop(stream_id)

    def _stop(self, stream_id: str) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream:
            if stream["process"].poll() is None:
                stream["process"].kill()
            shutil.rmtree(stream["dir"], ignore_errors=True)

    def stop_all(self) -> None:
        with self._lock:
            for stream_id in list(self._streams):
                self._stop(stream_id)

    def hls_file(self, key: str, stream_id: str, name: str, wait: float = 30) -> tuple[bytes, str] | None:
        """A file of a stream (the playlist, or a segment) and its type; None if there's no such
        stream or file. The playlist waits up to ``wait`` seconds for ffmpeg's first segment."""
        if "/" in name or name.startswith("."):
            return None
        with self._lock:
            stream = self._streams.get(stream_id)
            if not stream or stream["key"] != key:
                return None
            stream["used"] = time.monotonic()
        path = os.path.join(stream["dir"], name)
        deadline = time.monotonic() + wait
        while not os.path.exists(path):
            if stream["process"].poll() is not None or time.monotonic() > deadline:
                if stream["process"].poll():
                    log.warning("ffmpeg couldn't convert the movie: %s",
                                stream["process"].stderr.read().decode(errors="replace")[-500:])
                return None
            time.sleep(0.25)
        with open(path, "rb") as f:
            data = f.read()
        if name.endswith(".m3u8"):
            # A growing (EVENT) playlist plays from the start only if it says so
            text = data.decode()
            if "#EXT-X-START" not in text:
                text = text.replace("#EXTM3U\n", "#EXTM3U\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES\n", 1)
            return text.encode(), "application/vnd.apple.mpegurl"
        return data, "video/mp4"


def probe_codecs(path: str) -> tuple[str, str]:
    """The video and audio codec names of a file (e.g. ``("hevc", "eac3")``), blank if unknown."""
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return "", ""

    def codec(stream: str) -> str:
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-select_streams", f"{stream}:0", "-show_entries",
                                  "stream=codec_name", "-of", "csv=p=0", path],
                                 capture_output=True, timeout=30, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out.decode(errors="replace").strip().splitlines()[0] if out.strip() else ""

    return codec("v"), codec("a")


def embedded_poster(path: str) -> bytes | None:
    """The cover art stored inside a movie file (an MP4's cover, an MKV's attached
    cover.jpg) as a JPEG, or None if it has none or there's no ffmpeg."""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None
    try:
        # 0:v but not 0:V: video streams that are attached pictures
        result = subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-i", path, "-map", "0:v", "-map", "-0:V",
                                 "-frames:v", "1", "-c:v", "mjpeg", "-f", "image2", "pipe:1"],
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def placeholder_poster(title: str, year: int | None = None, width: int = 600, height: int = 900) -> bytes:
    """A plain poster for a movie that has none: its title on a dark card with a gold frame."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), (24, 22, 20))
    draw = ImageDraw.Draw(img)
    gold = (201, 162, 39)
    inset = max(4, width // 24)
    draw.rectangle([inset, inset, width - inset, height - inset], outline=gold, width=max(2, width // 150))

    def font(size: int):
        for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                     "/System/Library/Fonts/Helvetica.ttc"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        try:
            return ImageFont.load_default(size)
        except TypeError:  # Pillow before 10.1
            return ImageFont.load_default()

    size = max(10, width // 11)
    title_font = font(size)
    lines = textwrap.wrap(title, width=max(6, int((width - 4 * inset) / (size * 0.58)))) or [title]
    lines = lines[:6]
    line_height = int(size * 1.25)
    y = height // 2 - (len(lines) * line_height) // 2 - (line_height // 2 if year else 0)
    for line in lines:
        draw.text((width / 2, y), line, fill=(240, 232, 214), font=title_font, anchor="ma")
        y += line_height
    if year:
        draw.text((width / 2, y + line_height // 3), str(year), fill=gold, font=font(max(8, size * 2 // 3)),
                  anchor="ma")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()
