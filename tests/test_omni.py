import os
import sys
import time
import hashlib
import threading
import http.server
import socketserver
import unittest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set offscreen for PyQt tests
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from core.config import get_config, set_config, DEFAULT_CONFIG
from core.clipboard_sniffer import is_video_url, ClipboardSniffer
from core.downloader import format_bytes, format_seconds, DownloadWorker

class TestOmniDownloader(unittest.TestCase):

    def test_01_config_persistence(self):
        """Test reading and writing configuration."""
        val = get_config("concurrent_fragments", 16)
        self.assertIsInstance(val, int)
        
        # Test override, then restore the user's real config
        from core.config import load_config, save_config
        original = load_config()
        try:
            set_config("test_metric", 999)
            self.assertEqual(get_config("test_metric"), 999)
        finally:
            original.pop("test_metric", None)
            save_config(original)

    def test_02_clipboard_regex_positive(self):
        """Test URL sniffer against various supported video platforms."""
        test_urls = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/abc123XYZ",
            "https://www.facebook.com/reel/1234567890",
            "https://www.facebook.com/watch/?v=987654321",
            "https://fb.watch/xyz123/",
            "https://www.instagram.com/reel/CxY12345678/",
            "https://www.tiktok.com/@user/video/7123456789012345678",
            "https://twitter.com/user/status/1234567890123456789",
            "https://x.com/user/status/1234567890123456789",
            "https://vimeo.com/123456789",
            "https://reddit.com/r/videos/comments/xyz123/cool_video/",
            "https://example.com/media/sample_video.mp4",
            "https://cdn.site.org/stream/index.m3u8",
            "https://files.test.com/audio/track.mp3"
        ]
        for url in test_urls:
            with self.subTest(url=url):
                self.assertTrue(is_video_url(url), f"Failed to detect valid URL: {url}")

    def test_03_clipboard_regex_negative(self):
        """Test URL sniffer rejects non-video links and random text."""
        invalid_urls = [
            "https://www.google.com/search?q=python",
            "https://en.wikipedia.org/wiki/Computer",
            "https://github.com/torvalds/linux",
            "Just some random copied text from an article",
            "file:///C:/Users/test/document.pdf",
            "http://example.com/page.html"
        ]
        for text in invalid_urls:
            with self.subTest(text=text):
                self.assertFalse(is_video_url(text), f"Incorrectly flagged non-video URL: {text}")

    def test_04_format_helpers(self):
        """Test byte and second formatting utilities."""
        self.assertEqual(format_bytes(1024), "1.00 KB")
        self.assertEqual(format_bytes(1048576), "1.00 MB")
        self.assertEqual(format_bytes(1073741824), "1.00 GB")
        self.assertEqual(format_seconds(65), "01:05")
        self.assertEqual(format_seconds(3665), "01:01:05")
        self.assertEqual(format_seconds(None), "--:--")

    def test_05_local_loopback_download(self):
        """Test downloading a real file over local loopback HTTP server with chunking."""
        # 1. Create a dummy test file
        test_dir = PROJECT_ROOT / "tests" / "test_data"
        test_dir.mkdir(parents=True, exist_ok=True)
        
        dummy_file = test_dir / "sample_video.mp4"
        dummy_data = b"OmniDL_TEST_VIDEO_CHUNK_" * 50000  # ~1.2 MB
        dummy_file.write_bytes(dummy_data)
        expected_hash = hashlib.sha256(dummy_data).hexdigest()

        # 2. Start local HTTP server
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def translate_path(self, path):
                return str(test_dir / "sample_video.mp4")

        server = socketserver.TCPServer(("127.0.0.1", 0), QuietHandler)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        local_url = f"http://127.0.0.1:{port}/sample_video.mp4"
        download_target_dir = test_dir / "out"
        download_target_dir.mkdir(parents=True, exist_ok=True)

        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance() or QApplication(sys.argv)

            worker = DownloadWorker(
                task_id="test_task_01",
                url=local_url,
                quality="direct",
                destination=str(download_target_dir),
                title="test_sample"
            )

            progress_reports = []
            worker.progress_updated.connect(lambda tid, p, s, eta, d, t: progress_reports.append(p))
            
            finished_event = threading.Event()
            finished_path = []
            
            def on_finish(tid, path):
                finished_path.append(path)
                finished_event.set()

            worker.download_finished.connect(on_finish)
            worker.download_failed.connect(lambda tid, err: finished_event.set())

            worker.start()
            start_t = time.time()
            while not finished_event.is_set() and time.time() - start_t < 10:
                app.processEvents()
                time.sleep(0.05)

            self.assertTrue(len(finished_path) > 0, "Download worker did not complete in time")
            out_file = Path(finished_path[0])
            self.assertTrue(out_file.exists(), f"Downloaded file not found: {out_file}")
            
            # Verify data integrity
            actual_hash = hashlib.sha256(out_file.read_bytes()).hexdigest()
            self.assertEqual(expected_hash, actual_hash, "File hash does not match original data!")
        finally:
            server.shutdown()
            server.server_close()
            # Clean up test output
            if (test_dir / "out").exists():
                for f in (test_dir / "out").glob("*"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
                try:
                    (test_dir / "out").rmdir()
                    dummy_file.unlink()
                    test_dir.rmdir()
                except Exception:
                    pass

    def test_06_ui_instantiation(self):
        """Test instantiation of PyQt6 MainWindow and UI subcomponents without error."""
        from PyQt6.QtWidgets import QApplication
        from ui.main_window import MainWindow
        from ui.task_widget import TaskWidget

        app = QApplication.instance() or QApplication(sys.argv)
        win = MainWindow()
        self.assertIsNotNone(win)
        self.assertEqual(win.windowTitle(), "⚡ OmniDL Pro - Universal Turbo Video Downloader")

        # Test adding a task widget
        sample_task = {
            "id": "t1",
            "url": "https://example.com/video.mp4",
            "title": "Sample Test Video",
            "status": "Queued"
        }
        w = TaskWidget(sample_task)
        w.update_progress(50.0, "15 MB/s", "00:10", "50 MB", "100 MB")
        self.assertEqual(w.progress_bar.value(), 50)
        self.assertEqual(w.status, "Queued")
        win.close()

    def test_07_bridge_server_and_headers(self):
        """Test local bridge server communication with browser extension."""
        import requests
        from core.bridge_server import BridgeServer

        bridge = BridgeServer(port=48912)  # Use distinct test port
        bridge.start()

        captured_payloads = []
        bridge.stream_captured.connect(lambda p: captured_payloads.append(p))

        try:
            # 1. Test health check GET /status
            resp = requests.get("http://127.0.0.1:48912/status", timeout=3)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data.get("app"), "OmniDL Pro")

            # 2. Test stream capture POST /download with Referer & Origin headers (streaming-site style)
            test_payload = {
                "url": "https://stream.server.org/master.m3u8",
                "title": "Sample Stream HD",
                "pageUrl": "https://video.example.com/watch/12345",
                "headers": {
                    "Referer": "https://video.example.com/",
                    "Origin": "https://video.example.com",
                    "User-Agent": "Mozilla/5.0 TestBrowser"
                }
            }
            post_resp = requests.post("http://127.0.0.1:48912/download", json=test_payload, timeout=3)
            self.assertEqual(post_resp.status_code, 200)
            self.assertTrue(post_resp.json().get("success"))

            # Check signal received
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance() or QApplication(sys.argv)
            app.processEvents()

            self.assertEqual(len(captured_payloads), 1)
            received = captured_payloads[0]
            self.assertEqual(received["url"], test_payload["url"])
            self.assertEqual(received["headers"]["Referer"], "https://video.example.com/")
        finally:
            bridge.stop()

    def test_09_ffmpeg_missing_detection(self):
        """Test MediaDownloader emits clear error when FFmpeg is required but missing."""
        import unittest.mock as mock
        from core.downloader import DownloadWorker

        failed_signals = []
        downloader = DownloadWorker("test_task", "https://example.com/audio", "/tmp", quality="mp3")
        downloader.download_failed.connect(lambda task_id, msg: failed_signals.append((task_id, msg)))

        with mock.patch("shutil.which", return_value=None):
            downloader.run()

        self.assertEqual(len(failed_signals), 1)
        self.assertIn("FFmpeg is missing", failed_signals[0][1])
        self.assertIn("https://ffmpeg.org/download.html", failed_signals[0][1])

if __name__ == "__main__":
    unittest.main()
