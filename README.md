# gifmaker

A small Flask web app for converting uploaded MP4 videos to GIFs with FFmpeg.

## Run with Docker Compose

```sh
docker compose up -d
```

Open <http://localhost:8000>, select or drag and drop an MP4, adjust the PTS
slider, and click **Generate GIF**. The default PTS multiplier of
`0.5` doubles playback speed; `1` preserves speed and `2` halves it.
Generated GIFs are shown as a preview before downloading.

Stop the app with `docker compose down`.

The app performs these two conversion steps:

```sh
ffmpeg -i input.mp4 -vf "fps=30,scale=480:-1:flags=lanczos,palettegen" palette.png
ffmpeg -i input.mp4 -i palette.png -filter_complex "fps=30,setpts=0.5*PTS,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse" output.gif
```

The slider replaces `0.5` in the second command. **Preserve last palette**
reuses the last successful conversion's palette for this browser session,
skipping the first command. If there is no saved palette, the app generates one.
The saved palette preview identifies the source video. Uncheck the option when
changing videos. Closing or leaving the page sends a best-effort cleanup request;
palettes also expire after an hour and are removed by a background cleanup
worker. Uploads and conversion files are removed after each request, and
generated GIF bytes are returned directly rather than stored on the server.

Uploads are limited to 100 MiB, two minutes, and 4K resolution. Output is limited
to 50 MiB and a 2160-pixel generated height, with a 120-second timeout per
FFmpeg command. Browser shutdown is not observable perfectly, so the one-hour
expiry protects storage if the browser cannot send the cleanup request.

FFmpeg is **not bundled with the source**: Docker downloads it from Debian's
package repositories during the build. Its licensing depends on the packaged
build; consult the FFmpeg/Debian license information before redistributing an image.

The container runs as an unprivileged user with one worker, which owns the
temporary palette cache. For deployment, use HTTPS behind a reverse proxy and
set a random `SECRET_KEY` environment variable. This is a small synchronous app,
not a public multi-tenant conversion service. Set
`SESSION_COOKIE_SECURE=1` when the app is served over HTTPS.

## Run locally

Install FFmpeg separately using your operating system's package manager, then:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
gunicorn --bind 127.0.0.1:8000 --workers 1 --timeout 300 app:app
```
