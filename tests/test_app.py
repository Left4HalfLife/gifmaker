from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app as gifmaker


class GifmakerTestCase(unittest.TestCase):
    def setUp(self):
        gifmaker.app.config.update(TESTING=True)
        gifmaker.purge_expired_palettes()
        for path in gifmaker.palette_directory.glob("*.png"):
            path.unlink()
        self.client = gifmaker.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.client_id = session["client_id"]

    def convert(self, **extra_data):
        data = {
            "csrf_token": self.client_id,
            "video": (io.BytesIO(b"not a real mp4"), "sample video.mp4"),
            "pts": "0.5",
        }
        data.update(extra_data)

        def fake_ffmpeg(arguments):
            output = Path(arguments[-1])
            output.write_bytes(b"palette" if output.suffix == ".png" else b"GIF89a")

        with (
            patch.object(gifmaker, "probe_video"),
            patch.object(gifmaker, "ffmpeg", side_effect=fake_ffmpeg),
        ):
            return self.client.post(
                "/convert", data=data, content_type="multipart/form-data"
            )

    def test_conversion_keeps_palette_and_returns_previewable_gif(self):
        response = self.convert()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/gif")
        self.assertEqual(response.data, b"GIF89a")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(
            self.client.get("/palette").data,
            b"palette",
        )
        self.assertEqual(
            self.client.get("/palette-info").get_json(),
            {
                "available": True,
                "filename": "sample_video.mp4",
                "url": "/palette",
            },
        )

    def test_cleanup_deletes_only_current_browser_palette(self):
        self.convert()
        palette = gifmaker.palette_path(self.client_id)
        second_client = gifmaker.app.test_client()
        second_client.get("/")
        with second_client.session_transaction() as session:
            second_id = session["client_id"]
        second_palette = gifmaker.palette_path(second_id)
        second_palette.write_bytes(b"second")

        response = self.client.post(
            "/cleanup", data={"csrf_token": self.client_id}
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(palette.exists())
        self.assertTrue(second_palette.exists())
        self.assertEqual(self.client.get("/palette").status_code, 404)

    def test_cleanup_requires_valid_csrf_token(self):
        self.convert()

        response = self.client.post(
            "/cleanup", data={"csrf_token": "not-the-session-token"}
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(gifmaker.palette_path(self.client_id).exists())

    def test_sessions_cannot_read_another_palette(self):
        self.convert()
        second_client = gifmaker.app.test_client()
        second_client.get("/")

        self.assertEqual(second_client.get("/palette").status_code, 404)
        self.assertEqual(
            second_client.get("/palette-info").get_json(),
            {"available": False},
        )

    def test_palette_cache_evicts_oldest_but_keeps_reused_palette(self):
        oldest = gifmaker.palette_path("1" * 32)
        newer = gifmaker.palette_path("2" * 32)
        oldest.write_bytes(b"oldest")
        newer.write_bytes(b"newer")
        now = time.time()
        os.utime(oldest, (now - 20, now - 20))
        os.utime(newer, (now - 10, now - 10))

        with patch.object(gifmaker, "MAX_CACHED_PALETTES", 2):
            self.assertEqual(self.convert().status_code, 200)
            self.assertFalse(oldest.exists())
            self.assertTrue(newer.exists())
            self.assertTrue(gifmaker.palette_path(self.client_id).exists())
            self.assertEqual(self.convert(preserve_palette="on").status_code, 200)

        self.assertTrue(newer.exists())
        self.assertEqual(len(list(gifmaker.palette_directory.glob("*.png"))), 2)

    def test_palette_size_limit_and_preservation_of_cached_palette(self):
        with patch.object(gifmaker, "MAX_PALETTE_BYTES", 7):
            self.assertEqual(self.convert().status_code, 200)

        existing = gifmaker.palette_path(self.client_id)
        existing.write_bytes(b"old")

        with patch.object(gifmaker, "MAX_PALETTE_BYTES", 6):
            response = self.convert()

        self.assertEqual(response.status_code, 422)
        self.assertIn(b"The generated palette is too large.", response.data)
        self.assertEqual(existing.read_bytes(), b"old")

    def test_concurrent_palette_writes_respect_cache_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            sources = [Path(directory) / str(index) for index in range(12)]
            for source in sources:
                source.write_bytes(b"palette")
            destinations = [
                gifmaker.palette_path(f"{index:032x}") for index in range(12)
            ]
            with patch.object(gifmaker, "MAX_CACHED_PALETTES", 3):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(gifmaker.cache_palette, sources, destinations))

        self.assertEqual(len(list(gifmaker.palette_directory.glob("*.png"))), 3)

    def test_expired_palette_is_not_reused(self):
        cached = gifmaker.palette_path(self.client_id)
        cached.write_bytes(b"expired")
        expired = time.time() - gifmaker.PALETTE_TTL_SECONDS - 1
        os.utime(cached, (expired, expired))

        response = self.convert(preserve_palette="on")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cached.read_bytes(), b"palette")

    def test_security_headers_are_present(self):
        response = self.client.get("/")

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertNotIn("'unsafe-inline'", response.headers["Content-Security-Policy"])

    def test_secure_cookie_and_hsts_are_enabled_by_default(self):
        environment = os.environ.copy()
        environment.pop("SESSION_COOKIE_SECURE", None)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import app, json; response = app.app.test_client().get('/'); "
                "print(json.dumps([response.headers.get('Set-Cookie'), "
                "response.headers.get('Strict-Transport-Security')]))",
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        cookie, hsts = json.loads(result.stdout)

        self.assertIn("; Secure;", cookie)
        self.assertEqual(hsts, "max-age=31536000")

    def test_local_http_can_opt_out_without_disabling_hsts_for_https(self):
        with patch.dict(gifmaker.app.config, {"SESSION_COOKIE_SECURE": False}):
            response = gifmaker.app.test_client().get("/", base_url="http://localhost")
            self.assertNotIn("; Secure;", response.headers["Set-Cookie"])
            self.assertNotIn("Strict-Transport-Security", response.headers)

            response = gifmaker.app.test_client().get("/", base_url="https://localhost")
            self.assertEqual(
                response.headers["Strict-Transport-Security"], "max-age=31536000"
            )

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg and ffprobe are required",
    )
    def test_real_mp4_conversion_and_rejection_of_disguised_video(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.mp4"
            disguised = Path(directory) / "disguised.mp4"
            for path, container in ((valid, "mp4"), (disguised, "matroska")):
                subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=red:size=32x32:rate=5", "-t", "0.6",
                        "-c:v", "mpeg4", "-f", container, str(path),
                    ],
                    check=True,
                    capture_output=True,
                )

            gifmaker.probe_video(valid)
            with self.assertRaises(subprocess.CalledProcessError):
                gifmaker.probe_video(disguised)

            with valid.open("rb") as video:
                response = self.client.post(
                    "/convert",
                    data={
                        "csrf_token": self.client_id,
                        "video": (video, "valid.mp4"),
                        "pts": "0.5",
                    },
                    content_type="multipart/form-data",
                )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.mimetype, "image/gif")
        self.assertTrue(response.data.startswith(b"GIF"))
        self.assertEqual(self.client.get("/palette").status_code, 200)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg and ffprobe are required",
    )
    def test_conversion_uses_only_the_validated_video_track(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "multiple.mp4"
            output = Path(directory) / "output.gif"
            subprocess.run(
                [
                    "ffmpeg", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=red:size=32x32:rate=5",
                    "-f", "lavfi", "-i", "color=c=blue:size=64x512:rate=5",
                    "-t", "0.6", "-map", "0:v:0", "-map", "1:v:0",
                    "-disposition:v:0", "0", "-disposition:v:1", "default",
                    "-c:v", "mpeg4", str(source),
                ],
                check=True,
                capture_output=True,
            )
            with source.open("rb") as video:
                response = self.client.post(
                    "/convert",
                    data={
                        "csrf_token": self.client_id,
                        "video": (video, "multiple.mp4"),
                        "pts": "0.5",
                    },
                    content_type="multipart/form-data",
                )
            self.assertEqual(response.status_code, 200, response.data)
            output.write_bytes(response.data)
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height", "-of", "json",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        stream = json.loads(probe.stdout)["streams"][0]
        self.assertEqual((stream["width"], stream["height"]), (480, 480))
        pixels = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", "pipe:0",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ],
            input=self.client.get("/palette").data,
            check=True,
            capture_output=True,
        ).stdout
        colors = {tuple(pixels[i:i + 3]) for i in range(0, len(pixels), 3)}
        self.assertTrue(any(r > 150 and b < 100 for r, _, b in colors))
        self.assertFalse(any(b > 150 and r < 100 for r, _, b in colors))

    def test_rejects_overlong_media_before_ffmpeg(self):
        probe_output = json.dumps(
            {
                "streams": [{"width": 1920, "height": 1080}],
                "format": {"duration": "121"},
            }
        )
        with patch.object(
            gifmaker.subprocess,
            "run",
            return_value=SimpleNamespace(stdout=probe_output),
        ):
            with self.assertRaisesRegex(
                gifmaker.MediaValidationError, "2 minutes"
            ):
                gifmaker.probe_video(Path("upload.mp4"))

    def test_rejects_oversized_output_dimensions_before_ffmpeg(self):
        probe_output = json.dumps(
            {
                "streams": [{"width": 100, "height": 1000}],
                "format": {"duration": "1"},
            }
        )
        with patch.object(
            gifmaker.subprocess,
            "run",
            return_value=SimpleNamespace(stdout=probe_output),
        ):
            with self.assertRaisesRegex(
                gifmaker.MediaValidationError, "aspect ratio"
            ):
                gifmaker.probe_video(Path("upload.mp4"))

    def test_ui_uses_previews_instead_of_automatic_download(self):
        script = Path("static/app.js").read_text()
        template = Path("templates/index.html").read_text()

        self.assertIn("gifPreview.src = generatedGifUrl", script)
        self.assertNotIn("link.click()", script)
        self.assertIn("id=\"palette-preview\"", template)
        self.assertIn("id=\"gif-download\"", template)


if __name__ == "__main__":
    unittest.main()
