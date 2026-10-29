import os
import re
import time
import uuid
import shutil
import urllib.parse
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal, QThread

import yt_dlp
from core.config import get_config, get_proxy_url, get_category_for_file


def format_bytes(bytes_count):
    if not bytes_count or bytes_count <= 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.2f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.2f} PB"


def format_seconds(seconds):
    if seconds is None or seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class MediaInspector(QThread):
    """Inspects a URL in the background to fetch title, formats, duration, thumbnail."""
    info_ready = pyqtSignal(dict)
    info_error = pyqtSignal(str)

    def __init__(self, url, headers=None):
        super().__init__()
        self.url = url.strip()
        self.headers = headers or {}

    def run(self):
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extract_flat': False,
                'noplaylist': not get_config("playlist_support", True),
            }
            if self.headers:
                ydl_opts['http_headers'] = self.headers

            proxy = get_proxy_url()
            if proxy:
                ydl_opts['proxy'] = proxy

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)

                # Handle playlist (Persepolis-style)
                if info.get('_type') == 'playlist':
                    entries = info.get('entries', [])
                    max_items = get_config("max_playlist_items", 50)
                    playlist_items = []
                    for i, entry in enumerate(entries):
                        if i >= max_items:
                            break
                        if entry:
                            playlist_items.append({
                                "url": entry.get('url') or entry.get('webpage_url', ''),
                                "title": entry.get('title', f'Item {i+1}'),
                                "duration": entry.get('duration', 0),
                            })

                    result = {
                        "url": self.url,
                        "title": info.get('title', 'Playlist'),
                        "is_playlist": True,
                        "playlist_count": len(playlist_items),
                        "entries": playlist_items,
                        "uploader": info.get('uploader') or info.get('channel') or 'Playlist',
                        "thumbnail": info.get('thumbnails', [{}])[0].get('url', '') if info.get('thumbnails') else '',
                        "formats": [{"label": "🎬 Best Available Quality", "format_id": "best"}],
                    }
                    self.info_ready.emit(result)
                    return

                # Single video
                title = info.get('title') or "Unknown Media"
                duration = info.get('duration') or 0
                thumbnail = info.get('thumbnail') or ""
                uploader = info.get('uploader') or info.get('channel') or "Web Media"
                filesize = info.get('filesize') or info.get('filesize_approx') or 0
                ext = info.get('ext') or 'mp4'

                # Formats list
                heights_seen = set()
                raw_formats = info.get('formats', [])

                for f in raw_formats:
                    h = f.get('height')
                    vcodec = f.get('vcodec', 'none')
                    if h and vcodec != 'none' and h not in heights_seen:
                        heights_seen.add(h)

                sorted_heights = sorted(list(heights_seen), reverse=True)

                format_options = [{"label": "🎬 Best Available Quality", "format_id": "best"}]
                for h in sorted_heights:
                    if h >= 2160:
                        lbl = f"💎 4K Ultra HD ({h}p)"
                    elif h >= 1440:
                        lbl = f"✨ 2K Quad HD ({h}p)"
                    elif h >= 1080:
                        lbl = f"🔥 1080p Full HD"
                    elif h >= 720:
                        lbl = f"⚡ 720p HD"
                    elif h >= 480:
                        lbl = f"📺 480p SD"
                    else:
                        lbl = f"📱 {h}p"
                    format_options.append({"label": lbl, "format_id": f"{h}p"})

                format_options.append({"label": "🎵 Audio Only (MP3 320kbps)", "format_id": "mp3"})
                format_options.append({"label": "🎵 Audio Only (FLAC Lossless)", "format_id": "flac"})

                result = {
                    "url": self.url,
                    "title": title,
                    "is_playlist": False,
                    "duration": duration,
                    "duration_str": format_seconds(duration),
                    "thumbnail": thumbnail,
                    "uploader": uploader,
                    "filesize": filesize,
                    "filesize_str": format_bytes(filesize),
                    "ext": ext,
                    "formats": format_options
                }
                self.info_ready.emit(result)
        except Exception as e:
            # Fallback for direct download links like .mp4, .zip, etc.
            parsed = urllib.parse.urlparse(self.url)
            filename = os.path.basename(parsed.path) or "downloaded_file"
            if "." in filename:
                result = {
                    "url": self.url,
                    "title": filename,
                    "is_playlist": False,
                    "duration": 0,
                    "duration_str": "--:--",
                    "thumbnail": "",
                    "uploader": parsed.netloc or "Direct Link",
                    "filesize": 0,
                    "filesize_str": "Direct Link",
                    "ext": filename.split(".")[-1],
                    "formats": [{"label": "Direct File Download", "format_id": "direct"}]
                }
                self.info_ready.emit(result)
            else:
                self.info_error.emit(str(e))


class DownloadWorker(QThread):
    """Worker thread with multi-connection acceleration.

    Merges best practices from:
    - FireDM: auto-retry with delay, speed throttling, proxy support
    - Persepolis: aria2c external downloader integration
    - PyIDM: concurrent fragment downloads with segment acceleration
    """
    progress_updated = pyqtSignal(str, float, str, str, str, str)  # task_id, percent, speed_str, eta_str, downloaded_str, total_str
    status_changed = pyqtSignal(str, str)  # task_id, status
    download_finished = pyqtSignal(str, str)  # task_id, output_path
    download_failed = pyqtSignal(str, str)  # task_id, error_msg

    def __init__(self, task_id, url, quality="best", destination=None, title=None, headers=None):
        super().__init__()
        self.task_id = task_id
        self.url = url
        self.quality = quality
        self.destination = destination or get_config("download_dir")
        self.custom_title = title
        self.headers = headers or {}
        self._is_cancelled = False
        self._last_time = time.time()
        self._last_bytes = 0

    def cancel(self):
        self._is_cancelled = True

    def _progress_hook(self, d):
        if self._is_cancelled:
            raise Exception("Download cancelled by user.")

        status = d.get('status')
        if status == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)

            percent = 0.0
            if total and total > 0:
                percent = (downloaded / total) * 100.0
            elif 'fragment_index' in d and 'fragment_count' in d:
                frag_idx = d.get('fragment_index', 0)
                frag_cnt = d.get('fragment_count', 1)
                if frag_cnt > 0:
                    percent = (frag_idx / frag_cnt) * 100.0

            speed = d.get('speed', 0) or 0
            eta = d.get('eta', None)

            speed_str = f"{format_bytes(speed)}/s" if speed else "Calculating..."
            eta_str = format_seconds(eta) if eta is not None else "--:--"
            downloaded_str = format_bytes(downloaded)
            total_str = format_bytes(total) if total else "Unknown"

            self.progress_updated.emit(
                self.task_id,
                min(max(percent, 0.0), 100.0),
                speed_str,
                eta_str,
                downloaded_str,
                total_str
            )
        elif status == 'finished':
            self.status_changed.emit(self.task_id, "Processing / Merging...")

    def _build_ydl_opts(self):
        """Build yt-dlp options dict with all merged features."""
        concurrent_frags = get_config("concurrent_fragments", 16)
        out_template = os.path.join(self.destination, '%(title)s.%(ext)s')

        ydl_opts = {
            'outtmpl': out_template,
            'concurrent_fragment_downloads': concurrent_frags,
            'progress_hooks': [self._progress_hook],
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'retries': get_config("auto_retry_count", 3),
            'fragment_retries': get_config("auto_retry_count", 3),
            'continuedl': get_config("auto_resume", True),  # Resume support (FireDM feature)
        }

        # Custom headers
        if self.headers:
            ydl_opts['http_headers'] = self.headers

        # Proxy support (Persepolis feature)
        proxy = get_proxy_url()
        if proxy:
            ydl_opts['proxy'] = proxy

        # Speed throttling (FireDM feature)
        speed_limit = get_config("speed_limit", 0)
        if speed_limit and speed_limit > 0:
            ydl_opts['ratelimit'] = speed_limit

        # Aria2c external downloader (Persepolis/aria2p feature)
        use_aria2 = get_config("use_aria2c", False)
        if use_aria2 and shutil.which("aria2c"):
            aria2_conns = get_config("aria2c_connections", 16)
            aria2_split = get_config("aria2c_split", 16)
            ydl_opts['external_downloader'] = 'aria2c'
            ydl_opts['external_downloader_args'] = {
                'aria2c': [
                    '-c',  # Continue/resume
                    f'-x{aria2_conns}',
                    f'-s{aria2_split}',
                    '-k1M',
                    '--file-allocation=none',
                    '--auto-file-renaming=false',
                    '--summary-interval=1',
                ]
            }

        # Quality/format selection
        if self.quality == 'mp3':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320',
                }],
            })
        elif self.quality == 'flac':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'flac',
                }],
            })
        elif self.quality.endswith('p'):
            h = self.quality.replace('p', '')
            ydl_opts.update({
                'format': f'bestvideo[height<={h}]+bestaudio/best[height<={h}]/best',
                'merge_output_format': 'mp4'
            })
        elif self.quality == 'direct':
            ydl_opts.update({'format': 'best'})
        else:
            ydl_opts.update({
                'format': 'bestvideo+bestaudio/best',
                'merge_output_format': 'mp4'
            })

        return ydl_opts

    def run(self):
        # Fail early with clear message if FFmpeg is required but missing
        needs_ffmpeg = self.quality in ("mp3", "flac") or (isinstance(self.quality, str) and (self.quality == "1080p" or self.quality.endswith("p")))
        if needs_ffmpeg and not shutil.which("ffmpeg"):
            self.status_changed.emit(self.task_id, "Failed")
            self.download_failed.emit(
                self.task_id,
                "FFmpeg is missing. Audio conversion and stream merging require FFmpeg: https://ffmpeg.org/download.html"
            )
            return

        max_retries = get_config("auto_retry_count", 3)
        retry_delay = get_config("auto_retry_delay", 5)

        for attempt in range(max_retries + 1):
            if self._is_cancelled:
                self.status_changed.emit(self.task_id, "Cancelled")
                return

            try:
                if attempt > 0:
                    self.status_changed.emit(self.task_id, f"Retrying ({attempt}/{max_retries})...")
                    time.sleep(retry_delay)
                else:
                    self.status_changed.emit(self.task_id, "Connecting...")

                ydl_opts = self._build_ydl_opts()

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(self.url, download=True)
                    final_filename = ydl.prepare_filename(info)

                    # If converted to audio, replace extension
                    if self.quality in ('mp3', 'flac'):
                        base, _ = os.path.splitext(final_filename)
                        final_filename = f"{base}.{self.quality}"

                    if not os.path.exists(final_filename):
                        base, _ = os.path.splitext(final_filename)
                        for test_ext in ['.mp4', '.mkv', '.webm', '.mp3', '.flac', '.m4a']:
                            if os.path.exists(f"{base}{test_ext}"):
                                final_filename = f"{base}{test_ext}"
                                break

                    fsize = os.path.getsize(final_filename) if os.path.exists(final_filename) else 0
                    self.progress_updated.emit(self.task_id, 100.0, "Done", "00:00",
                                               format_bytes(fsize), format_bytes(fsize))
                    self.status_changed.emit(self.task_id, "Completed")
                    self.download_finished.emit(self.task_id, final_filename)
                    return  # Success, exit retry loop

            except Exception as e:
                if self._is_cancelled:
                    self.status_changed.emit(self.task_id, "Cancelled")
                    return

                if attempt < max_retries:
                    continue  # Retry (FireDM auto-retry feature)
                else:
                    self.status_changed.emit(self.task_id, "Failed")
                    self.download_failed.emit(self.task_id, str(e))


class BatchDownloadManager(QObject):
    """Manages batch/playlist downloads with concurrency control (Persepolis-style)."""
    batch_progress = pyqtSignal(int, int)  # completed_count, total_count
    batch_item_started = pyqtSignal(str, str)  # url, title
    batch_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = []
        self._active_workers = []
        self._completed = 0
        self._total = 0

    def add_batch(self, items, quality="best", destination=None):
        """Add a list of {url, title} dicts to the batch queue."""
        self._total += len(items)
        for item in items:
            self._queue.append({
                "url": item["url"],
                "title": item.get("title", ""),
                "quality": quality,
                "destination": destination or get_config("download_dir"),
            })

    def get_queue_size(self):
        return len(self._queue)
