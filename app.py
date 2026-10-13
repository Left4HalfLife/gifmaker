import io
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time

from flask import Flask, render_template, request, send_file, session


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=100 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
palette_storage = tempfile.TemporaryDirectory(prefix="gifmaker-palettes-")
palette_directory = Path(palette_storage.name)


def page(error=None, status=200):
    session.setdefault("client_id", secrets.token_hex(16))
    return render_template(
        "index.html", csrf_token=session["client_id"], error=error
    ), status


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


@app.get("/")
def index():
    return page()


@app.post("/convert")
def convert():
    client_id = session.get("client_id")
    if not client_id or not secrets.compare_digest(
        request.form.get("csrf_token", "").encode(), client_id.encode()
    ):
        return page("Please reload the page and try again.", 400)

    upload = request.files.get("video")
    if not upload or not upload.filename.lower().endswith(".mp4"):
        return page("Please upload an MP4 file.", 400)
    try:
        pts = float(request.form.get("pts", "0.5"))
    except ValueError:
        return page("PTS must be a number between 0.1 and 2.", 400)
    if not math.isfinite(pts) or not 0.1 <= pts <= 2:
        return page("PTS must be a number between 0.1 and 2.", 400)

    for old_palette in palette_directory.glob("*.png"):
        if old_palette.stat().st_mtime < time.time() - 3600:
            old_palette.unlink(missing_ok=True)
    cached_palette = palette_directory / f"{client_id}.png"

    with tempfile.TemporaryDirectory(prefix="gifmaker-conversion-") as directory:
        directory = Path(directory)
        source = directory / "input.mp4"
        palette = directory / "palette.png"
        output = directory / "output.gif"
        upload.save(source)
        source_arguments = [
            "-protocol_whitelist", "file,pipe", "-f", "mp4", "-i", str(source)
        ]
        try:
            if request.form.get("preserve_palette") == "on" and cached_palette.exists():
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
                str(output),
            ])
            gif = io.BytesIO(output.read_bytes())
            os.replace(palette, cached_palette)
            cached_palette.touch()
        except FileNotFoundError:
            return page("FFmpeg is unavailable. Please run the app using Docker.", 503)
        except subprocess.TimeoutExpired:
            return page("Conversion timed out. Please try a shorter video.", 422)
        except subprocess.CalledProcessError:
            return page("Conversion failed. Please check that the file is a valid MP4.", 422)

    return send_file(
        gif, mimetype="image/gif", as_attachment=True, download_name="output.gif",
        max_age=0,
    )


@app.errorhandler(413)
def upload_too_large(error):
    return page("The upload is too large. The limit is 100 MiB.", 413)
