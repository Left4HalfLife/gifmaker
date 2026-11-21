import io
import json
from pathlib import Path
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

    def test_security_headers_are_present(self):
        response = self.client.get("/")

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertNotIn("'unsafe-inline'", response.headers["Content-Security-Policy"])

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
