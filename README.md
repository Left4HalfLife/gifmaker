# gifmaker

A small Flask web app for converting uploaded MP4 videos to GIFs with FFmpeg.

## Run with Docker Compose

```sh
docker compose up -d
```

Open <http://localhost:8000>, select or drag and drop an MP4, adjust the PTS
slider, and click **Convert & download GIF**. The default PTS multiplier of
`0.5` doubles playback speed; `1` preserves speed and `2` halves it.

Stop the app with `docker compose down`.

The app performs these two conversion steps:

```sh
ffmpeg -i input.mp4 -vf "fps=30,scale=480:-1:flags=lanczos,palettegen" palette.png
ffmpeg -i input.mp4 -i palette.png -filter_complex "fps=30,setpts=0.5*PTS,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse" output.gif
```

The slider replaces `0.5` in the second command. **Preserve last palette**
reuses the last successful conversion's palette for this browser session,
skipping the first command. If there is no saved palette, the app generates one.
Uncheck it when changing videos. Palettes expire after an hour of inactivity
and are lost when the server restarts; uploads and conversion files are removed
after each request. Uploads are limited to 100 MiB, with a 120-second timeout
per FFmpeg command.

FFmpeg is **not bundled with the source**: Docker downloads it from Debian's
package repositories during the build. Its licensing depends on the packaged
build; consult the FFmpeg/Debian license information before redistributing an image.

The container runs as an unprivileged user with one worker, which owns the
temporary palette cache. For deployment, use HTTPS behind a reverse proxy and
set a random `SECRET_KEY` environment variable. This is a small synchronous app,
not a public multi-tenant conversion service.

## Run locally

Install FFmpeg separately using your operating system's package manager, then:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
gunicorn --bind 127.0.0.1:8000 --workers 1 --timeout 300 app:app
```
