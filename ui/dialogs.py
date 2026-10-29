import os
import shutil
import requests
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QFileDialog, QFrame, QProgressBar,
    QMessageBox, QCheckBox, QScrollArea, QWidget
)
from PyQt6.QtCore import Qt, pyqtSignal, QByteArray
from PyQt6.QtGui import QPixmap, QImage

from core.downloader import MediaInspector
from core.config import get_config


class AddDownloadDialog(QDialog):
    """Enhanced add-download dialog with playlist support (Persepolis-style)."""
    download_requested = pyqtSignal(dict)  # emits {url, title, quality, destination, auto_start, headers}
    batch_requested = pyqtSignal(list, str, str)  # emits (items_list, quality, destination)

    def __init__(self, parent=None, initial_url="", initial_title="", initial_headers=None):
        super().__init__(parent)
        self.setWindowTitle("⚡ New Download - OmniDL Grabber")
        self.setMinimumWidth(580)
        self.setModal(True)
        self.inspector = None
        self.media_info = None
        self.headers = initial_headers or {}
        self.initial_title = initial_title

        self._init_ui(initial_url)

        if initial_title:
            self.title_label.setText(initial_title)

        if initial_url.strip():
            self._analyze_url()

    def _init_ui(self, initial_url):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Title
        hdr_label = QLabel("📥 Add New Download")
        hdr_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #58a6ff;")
        layout.addWidget(hdr_label)

        # URL Input Row
        url_row = QHBoxLayout()
        self.url_input = QLineEdit(initial_url)
        self.url_input.setPlaceholderText("Paste any video URL or file link (YouTube, Facebook, TikTok, Instagram, MP4...)")
        self.url_input.textChanged.connect(self._on_url_changed)
        url_row.addWidget(self.url_input)

        self.btn_analyze = QPushButton("🔍 Analyze")
        self.btn_analyze.setProperty("class", "btn-secondary")
        self.btn_analyze.clicked.connect(self._analyze_url)
        url_row.addWidget(self.btn_analyze)
        layout.addLayout(url_row)

        # Progress / Status during inspection
        self.status_lbl = QLabel("Ready to analyze link.")
        self.status_lbl.setStyleSheet("color: #8b949e; font-size: 12px;")
        layout.addWidget(self.status_lbl)

        # Preview Card (Thumbnail, Title, Metadata)
        self.preview_card = QFrame()
        self.preview_card.setStyleSheet("background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px;")
        preview_layout = QHBoxLayout(self.preview_card)
        preview_layout.setSpacing(14)

        # Thumbnail Label
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(140, 80)
        self.thumb_label.setStyleSheet("background-color: #0d1117; border-radius: 6px; border: 1px solid #21262d;")
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setText("No Preview")
        preview_layout.addWidget(self.thumb_label)

        # Metadata info
        meta_layout = QVBoxLayout()
        meta_layout.setSpacing(4)
        self.title_label = QLabel("Waiting for link...")
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-weight: 600; font-size: 13px; color: #c9d1d9;")
        meta_layout.addWidget(self.title_label)

        self.details_label = QLabel("Supports: YouTube, Reels, TikTok, Twitter/X, MP4, etc.")
        self.details_label.setStyleSheet("font-size: 11px; color: #8b949e;")
        meta_layout.addWidget(self.details_label)
        meta_layout.addStretch()

        preview_layout.addLayout(meta_layout)
        layout.addWidget(self.preview_card)

        # Playlist items panel (hidden by default, shown for playlists)
        self.playlist_frame = QFrame()
        self.playlist_frame.setStyleSheet("background-color: #161b22; border: 1px solid #30363d; border-radius: 8px;")
        self.playlist_frame.setVisible(False)
        pl_layout = QVBoxLayout(self.playlist_frame)

        pl_header = QHBoxLayout()
        self.playlist_title = QLabel("📋 Playlist Items")
        self.playlist_title.setStyleSheet("font-weight: 600; color: #58a6ff; font-size: 12px;")
        pl_header.addWidget(self.playlist_title)

        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.setProperty("class", "btn-secondary")
        self.btn_select_all.setFixedHeight(24)
        self.btn_select_all.clicked.connect(self._select_all_playlist)
        pl_header.addWidget(self.btn_select_all)

        self.btn_deselect_all = QPushButton("Deselect All")
        self.btn_deselect_all.setProperty("class", "btn-secondary")
        self.btn_deselect_all.setFixedHeight(24)
        self.btn_deselect_all.clicked.connect(self._deselect_all_playlist)
        pl_header.addWidget(self.btn_deselect_all)

        pl_layout.addLayout(pl_header)

        # Scrollable playlist items
        self.pl_scroll = QScrollArea()
        self.pl_scroll.setWidgetResizable(True)
        self.pl_scroll.setMaximumHeight(180)
        self.pl_scroll_widget = QWidget()
        self.pl_scroll_layout = QVBoxLayout(self.pl_scroll_widget)
        self.pl_scroll_layout.setSpacing(4)
        self.pl_scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.pl_scroll.setWidget(self.pl_scroll_widget)
        pl_layout.addWidget(self.pl_scroll)

        layout.addWidget(self.playlist_frame)
        self.playlist_checkboxes = []

        # Options Row: Quality & Destination
        opt_layout = QVBoxLayout()
        opt_layout.setSpacing(10)

        # Quality Row
        q_row = QHBoxLayout()
        q_lbl = QLabel("Quality / Format:")
        q_lbl.setFixedWidth(110)
        q_lbl.setStyleSheet("font-weight: 600; color: #c9d1d9;")
        self.combo_quality = QComboBox()
        self.combo_quality.addItem("🎬 Best Available Quality", "best")
        self.combo_quality.addItem("🔥 1080p Full HD", "1080p")
        self.combo_quality.addItem("⚡ 720p HD", "720p")
        self.combo_quality.addItem("🎵 Audio Only (MP3 320kbps)", "mp3")
        self.combo_quality.addItem("🎵 Audio Only (FLAC Lossless)", "flac")
        q_row.addWidget(q_lbl)
        q_row.addWidget(self.combo_quality)
        opt_layout.addLayout(q_row)

        # Destination Row
        dest_row = QHBoxLayout()
        dest_lbl = QLabel("Save to:")
        dest_lbl.setFixedWidth(110)
        dest_lbl.setStyleSheet("font-weight: 600; color: #c9d1d9;")
        self.dest_input = QLineEdit(get_config("download_dir"))
        self.btn_browse = QPushButton("📁 Browse")
        self.btn_browse.setProperty("class", "btn-secondary")
        self.btn_browse.clicked.connect(self._browse_dir)
        dest_row.addWidget(dest_lbl)
        dest_row.addWidget(self.dest_input)
        dest_row.addWidget(self.btn_browse)
        opt_layout.addLayout(dest_row)

        layout.addLayout(opt_layout)

        # Action Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setProperty("class", "btn-secondary")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)

        self.btn_queue = QPushButton("📋 Add to Queue")
        self.btn_queue.setProperty("class", "btn-blue")
        self.btn_queue.clicked.connect(lambda: self._start_download(auto_start=False))
        btn_row.addWidget(self.btn_queue)

        self.btn_download = QPushButton("⚡ Download Now")
        self.btn_download.setProperty("class", "btn-primary")
        self.btn_download.clicked.connect(lambda: self._start_download(auto_start=True))
        btn_row.addWidget(self.btn_download)

        layout.addLayout(btn_row)

    def _on_url_changed(self, text):
        if text.startswith("http://") or text.startswith("https://"):
            self.status_lbl.setText("Click 'Analyze' or press Download Now.")

    def _analyze_url(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Invalid URL", "Please enter a valid video link.")
            return

        self.status_lbl.setText("⏳ Analyzing video source and available streams...")
        self.btn_analyze.setEnabled(False)

        self.inspector = MediaInspector(url, headers=self.headers)
        self.inspector.info_ready.connect(self._on_info_ready)
        self.inspector.info_error.connect(self._on_info_error)
        self.inspector.start()

    def _on_info_ready(self, info):
        self.media_info = info
        self.btn_analyze.setEnabled(True)

        if info.get("is_playlist"):
            # Playlist detected (Persepolis-style)
            count = info.get("playlist_count", 0)
            self.status_lbl.setText(f"📋 Playlist detected: {count} items found!")
            self.title_label.setText(info.get('title', 'Playlist'))
            self.details_label.setText(f"Channel: {info.get('uploader', 'Unknown')} | Items: {count}")

            # Show playlist panel
            self.playlist_frame.setVisible(True)
            self.playlist_title.setText(f"📋 Playlist Items ({count})")

            # Clear existing
            for cb in self.playlist_checkboxes:
                cb.setParent(None)
            self.playlist_checkboxes.clear()

            entries = info.get("entries", [])
            for i, entry in enumerate(entries):
                cb = QCheckBox(f"{i+1}. {entry.get('title', 'Unknown')}")
                cb.setChecked(True)
                cb.setProperty("entry_data", entry)
                self.pl_scroll_layout.addWidget(cb)
                self.playlist_checkboxes.append(cb)

            self.btn_download.setText(f"⚡ Download {count} Items")
        else:
            # Single video
            self.status_lbl.setText("✅ Source detected successfully!")
            self.playlist_frame.setVisible(False)
            self.btn_download.setText("⚡ Download Now")

            self.title_label.setText(info.get('title', 'Unknown'))

            duration = info.get('duration_str', '--:--')
            uploader = info.get('uploader', 'Web')
            size = info.get('filesize_str', '')
            self.details_label.setText(f"Channel: {uploader} | Duration: {duration} | Approx: {size}")

            # Update formats combo
            formats = info.get('formats', [])
            if formats:
                self.combo_quality.clear()
                for f in formats:
                    self.combo_quality.addItem(f['label'], f['format_id'])

            # Load thumbnail
            thumb_url = info.get('thumbnail')
            if thumb_url:
                self._load_thumbnail(thumb_url)

    def _load_thumbnail(self, thumb_url):
        try:
            resp = requests.get(thumb_url, timeout=4)
            if resp.status_code == 200:
                pix = QPixmap()
                pix.loadFromData(resp.content)
                scaled = pix.scaled(140, 80, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                self.thumb_label.setPixmap(scaled)
        except Exception:
            self.thumb_label.setText("Preview N/A")

    def _on_info_error(self, err_msg):
        self.btn_analyze.setEnabled(True)
        self.status_lbl.setText(f"⚠️ Notice: Will download with best stream fallback.")

    def _browse_dir(self):
        chosen = QFileDialog.getExistingDirectory(self, "Select Download Directory", self.dest_input.text())
        if chosen:
            self.dest_input.setText(chosen)

    def _select_all_playlist(self):
        for cb in self.playlist_checkboxes:
            cb.setChecked(True)

    def _deselect_all_playlist(self):
        for cb in self.playlist_checkboxes:
            cb.setChecked(False)

    def _start_download(self, auto_start=True):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Invalid URL", "Please enter a valid video link.")
            return

        quality = self.combo_quality.currentData() or "best"
        dest = self.dest_input.text().strip() or get_config("download_dir")

        # Validate FFmpeg requirement for MP3/FLAC and stream merging
        needs_ffmpeg = quality in ("mp3", "flac") or (isinstance(quality, str) and (quality == "1080p" or quality.endswith("p") or "1080" in quality))
        if needs_ffmpeg and not shutil.which("ffmpeg"):
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setWindowTitle("FFmpeg Required")
            msg_box.setTextFormat(Qt.TextFormat.RichText)
            msg_box.setText(
                "<h3>FFmpeg is required</h3>"
                "<p>Extracting audio (MP3/FLAC) and merging high-resolution video streams (1080p+) require <b>FFmpeg</b>.</p>"
                "<p>Please download and install FFmpeg to use this option:<br>"
                "<a href='https://ffmpeg.org/download.html'>https://ffmpeg.org/download.html</a></p>"
            )
            msg_box.exec()
            return

        # Check if playlist with selected items
        if self.media_info and self.media_info.get("is_playlist"):
            selected_items = []
            for cb in self.playlist_checkboxes:
                if cb.isChecked():
                    entry = cb.property("entry_data")
                    if entry:
                        selected_items.append(entry)

            if not selected_items:
                QMessageBox.warning(self, "No Items Selected", "Please select at least one playlist item.")
                return

            # Emit individual download for each selected item
            for item in selected_items:
                self.download_requested.emit({
                    "url": item["url"],
                    "title": item.get("title"),
                    "quality": quality,
                    "destination": dest,
                    "headers": self.headers,
                    "auto_start": auto_start
                })
            self.accept()
            return

        # Single download
        title = self.title_label.text() if self.media_info else (self.initial_title or None)

        self.download_requested.emit({
            "url": url,
            "title": title,
            "quality": quality,
            "destination": dest,
            "headers": self.headers,
            "auto_start": auto_start
        })
        self.accept()
