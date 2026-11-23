import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

from flask import Flask, jsonify, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=100 * 1024 * 1024,
    MAX_FORM_MEMORY_SIZE=1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
)
palette_storage = tempfile.TemporaryDirectory(prefix="gifmaker-palettes-")
palette_directory = Path(palette_storage.name)
palette_lock = threading.Lock()
PALETTE_TTL_SECONDS = 3600
MAX_VIDEO_DURATION_SECONDS = 120
MAX_VIDEO_PIXELS = 3840 * 2160
MAX_GIF_HEIGHT = 2160
MAX_GIF_BYTES = 50 * 1024 * 1024
CLIENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def page(error=None, status=200):
    session.setdefault("client_id", secrets.token_hex(16))
    return render_template("index.html", csrf_token=session["client_id"], error=error), status


def is_valid_client_id(client_id):
    return isinstance(client_id, str) and CLIENT_ID_PATTERN.fullmatch(client_id)


def palette_path(client_id):
    if not is_valid_client_id(client_id):
        return None
    return palette_directory / f"{client_id}.png"


def current_palette(client_id):
    path = palette_path(client_id)
    if path is None or not path.exists():
        return None
    try:
        if path.stat().st_mtime < time.time() - PALETTE_TTL_SECONDS:
            path.unlink(missing_ok=True)
            return None
    except FileNotFoundError:
        return None
    return path


def purge_expired_palettes():
    cutoff = time.time() - PALETTE_TTL_SECONDS
    with palette_lock:
        for path in palette_directory.glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def palette_cleanup_worker():
    while True:
        time.sleep(60)
        purge_expired_palettes()


threading.Thread(target=palette_cleanup_worker, name="palette-cleanup", daemon=True).start()


def valid_csrf_token(token):
    client_id = session.get("client_id")
    return (
        is_valid_client_id(client_id)
        and isinstance(token, str)
        and secrets.compare_digest(token, client_id)
    )


def remove_client_palette(client_id):
    path = palette_path(client_id)
    if path is None:
        return
    with palette_lock:
        path.unlink(missing_ok=True)
    session.pop("palette_video_name", None)


class MediaValidationError(Exception):
    pass


def probe_video(source):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(source),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        metadata = json.loads(result.stdout)
        stream = metadata["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration = float(metadata["format"]["duration"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MediaValidationError("The upload is not a readable MP4 video.") from error

    if width <= 0 or height <= 0 or not math.isfinite(duration) or duration <= 0:
        raise MediaValidationError("The upload does not contain a usable video stream.")
    if duration > MAX_VIDEO_DURATION_SECONDS:
        raise MediaValidationError("Videos must be 2 minutes or shorter.")
    if width * height > MAX_VIDEO_PIXELS:
        raise MediaValidationError("Video resolution must not exceed 4K.")
    gif_height = math.ceil(480 * height / width)
    if gif_height > MAX_GIF_HEIGHT:
        raise MediaValidationError("The video's aspect ratio is too tall for conversion.")


def ffmpeg(arguments):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
         "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1"]
        + arguments,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=120,
    )


@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' blob:; object-src 'none'; "
        "script-src 'self'; style-src 'self'"
    )
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/")
def index():
    purge_expired_palettes()
    return page()


@app.post("/convert")
def convert():
    client_id = session.get("client_id")
    if not valid_csrf_token(request.form.get("csrf_token")):
        return page("Please reload the page and try again.", 400)

    upload = request.files.get("video")
    video_name = secure_filename(upload.filename or "") if upload else ""
    if not upload or not video_name.lower().endswith(".mp4"):
        return page("Please upload an MP4 file.", 400)
    try:
        pts = float(request.form.get("pts", "0.5"))
    except ValueError:
        return page("PTS must be a number between 0.1 and 2.", 400)
    if not math.isfinite(pts) or not 0.1 <= pts <= 2:
        return page("PTS must be a number between 0.1 and 2.", 400)

    purge_expired_palettes()
    cached_palette = palette_path(client_id)
    assert cached_palette is not None

    with tempfile.TemporaryDirectory(prefix="gifmaker-conversion-") as directory:
        directory = Path(directory)
        source = directory / "input.mp4"
        palette = directory / "palette.png"
        output = directory / "output.gif"
        upload.save(source)
        try:
            probe_video(source)
            source_arguments = [
                "-protocol_whitelist", "file,pipe", "-f", "mp4", "-i", str(source)
            ]
            reuse_palette = (
                request.form.get("preserve_palette") == "on"
                and cached_palette.exists()
            )
            if reuse_palette:
                shutil.copyfile(cached_palette, palette)
            else:
                ffmpeg(source_arguments + [
                    "-vf", "fps=30,scale=480:-1:flags=lanczos,palettegen",
                    "-frames:v", "1", str(palette),
                ])
            ffmpeg(source_arguments + [
                "-protocol_whitelist", "file,pipe", "-i", str(palette),
                "-filter_complex",
                f"fps=30,setpts={pts:g}*PTS,scale=480:-1:flags=lanczos[x];"
                "[x][1:v]paletteuse",
                "-fs", str(MAX_GIF_BYTES),
                str(output),
            ])
            if output.stat().st_size > MAX_GIF_BYTES:
                raise MediaValidationError("The generated GIF is too large.")
            gif = io.BytesIO(output.read_bytes())
            with palette_lock:
                os.replace(palette, cached_palette)
                cached_palette.touch()
            if not reuse_palette:
                session["palette_video_name"] = video_name
            session.setdefault(
                "palette_video_name", video_name if reuse_palette else "previous video"
            )
        except FileNotFoundError:
            return page("FFmpeg is unavailable. Please run the app using Docker.", 503)
        except subprocess.TimeoutExpired:
            return page("Conversion timed out. Please try a shorter or smaller video.", 422)
        except subprocess.CalledProcessError:
            return page("Conversion failed. Please check that the file is a valid MP4.", 422)
        except MediaValidationError as error:
            return page(str(error), 422)

    response = send_file(
        gif,
        mimetype="image/gif",
        as_attachment=True,
        download_name=f"{Path(video_name).stem}.gif",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Gifmaker-Palette-Ready"] = "1"
    return response


@app.get("/palette")
def palette_preview():
    client_id = session.get("client_id")
    path = current_palette(client_id)
    if path is None:
        return ("", 404)
    with palette_lock:
        try:
            image = io.BytesIO(path.read_bytes())
        except FileNotFoundError:
            return ("", 404)
    response = send_file(image, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "no-store, private"
    return response


@app.get("/palette-info")
def palette_info():
    client_id = session.get("client_id")
    if current_palette(client_id) is None:
        return jsonify(available=False)
    return jsonify(
        available=True,
        filename=session.get("palette_video_name", "previous video.mp4"),
        url=url_for("palette_preview"),
    )


@app.post("/cleanup")
def cleanup():
    client_id = session.get("client_id")
    if not valid_csrf_token(request.form.get("csrf_token")):
        return ("", 403)
    remove_client_palette(client_id)
    response = app.response_class(status=204)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(413)
def upload_too_large(error):
    return page("The upload is too large. The limit is 100 MiB.", 413)
