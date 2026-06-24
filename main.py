import os
import re
import sys
import json
import zipfile
import traceback
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any, Set

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QPixmap, QPainter, QColor, QMouseEvent
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QFileDialog, QMessageBox,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QGridLayout, QFrame, QCheckBox, QTextEdit,
    QProgressBar, QSpinBox
)

APP_NAME = "osu!mania set exporter (fast scan)"
CACHE_DIR_NAME = ".osu_mania_exporter_cache"
CACHE_JSON_NAME = "fast_set_cache.json"
SECTION_RE = re.compile(r'^\s*\[(.+?)\]\s*$')

# ---------------------------
# Data model
# ---------------------------

@dataclass
class SetInfo:
    folder_name: str
    folder_path: str
    beatmap_set_id: Optional[int]
    artist: str
    title: str
    creator: str
    keys: List[int]
    osu_count: int
    representative_osu: str

# ---------------------------
# Utils
# ---------------------------

def safe_int(x, default=None):
    try:
        if x is None or x == "":
            return default
        return int(float(str(x).strip()))
    except Exception:
        return default

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] if name else "untitled"

def parse_kv_line(line: str):
    if ":" not in line:
        return None
    k, v = line.split(":", 1)
    return k.strip(), v.strip()

def detect_osu_root() -> Optional[str]:
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "osu!"),
        os.path.join(os.environ.get("APPDATA", ""), "osu!"),
        r"C:\Program Files\osu!",
        r"C:\Program Files (x86)\osu!",
        os.path.expanduser("~/AppData/Local/osu!"),
    ]
    for p in candidates:
        if p and os.path.isdir(p):
            songs = os.path.join(p, "Songs")
            if os.path.isdir(songs):
                return p
    return None

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def get_cache_root(osu_root: str) -> str:
    return os.path.join(osu_root, CACHE_DIR_NAME)

def get_cache_file(osu_root: str) -> str:
    return os.path.join(get_cache_root(osu_root), CACHE_JSON_NAME)

def list_set_dirs(songs_dir: str) -> List[str]:
    out = []
    try:
        for name in os.listdir(songs_dir):
            full = os.path.join(songs_dir, name)
            if os.path.isdir(full):
                out.append(full)
    except Exception:
        pass
    return out

def open_in_explorer(path: str):
    """
    Windows 优先在资源管理器中选中该文件夹；
    失败则普通打开目录。
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(path)

    norm = os.path.normpath(path)

    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", norm])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", norm])
        else:
            subprocess.Popen(["xdg-open", norm])
    except Exception:
        # 兜底
        if hasattr(os, "startfile"):
            os.startfile(norm)
        else:
            raise

# ---------------------------
# Minimal .osu parser
# ---------------------------

def parse_osu_minimal(path: str) -> Optional[Dict[str, Any]]:
    current_section = None
    general = {}
    metadata = {}
    difficulty = {}

    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n\r")
                m = SECTION_RE.match(line)
                if m:
                    current_section = m.group(1).strip()
                    continue

                if current_section == "General":
                    kv = parse_kv_line(line)
                    if kv:
                        general[kv[0]] = kv[1]
                elif current_section == "Metadata":
                    kv = parse_kv_line(line)
                    if kv:
                        metadata[kv[0]] = kv[1]
                elif current_section == "Difficulty":
                    kv = parse_kv_line(line)
                    if kv:
                        difficulty[kv[0]] = kv[1]

        return {
            "mode": safe_int(general.get("Mode"), -1),
            "key": safe_int(difficulty.get("CircleSize"), None),
            "artist": metadata.get("Artist", "") or metadata.get("ArtistUnicode", ""),
            "title": metadata.get("Title", "") or metadata.get("TitleUnicode", ""),
            "creator": metadata.get("Creator", ""),
            "beatmap_set_id": safe_int(metadata.get("BeatmapSetID"), None),
            "osu_path": path,
            "osu_filename": os.path.basename(path),
        }
    except Exception:
        return None

# ---------------------------
# Cache
# ---------------------------

class CacheManager:
    def __init__(self, osu_root: str):
        self.osu_root = osu_root
        self.cache_root = get_cache_root(osu_root)
        self.cache_file = get_cache_file(osu_root)
        ensure_dir(self.cache_root)

    def load(self) -> Dict[str, Any]:
        if not os.path.isfile(self.cache_file):
            return {"sets": {}, "songs_mtime": 0}
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"sets": {}, "songs_mtime": 0}

    def save(self, data: Dict[str, Any]):
        tmp = self.cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cache_file)

# ---------------------------
# Scan worker
# ---------------------------

class ScanWorker(QThread):
    progress = Signal(str)
    finished = Signal(list, str)
    failed = Signal(str)

    def __init__(self, osu_root: str, songs_dir: str, force_rescan: bool = False):
        super().__init__()
        self.osu_root = osu_root
        self.songs_dir = songs_dir
        self.force_rescan = force_rescan

    def build_set_info(self, set_dir: str) -> Optional[SetInfo]:
        folder_name = os.path.basename(set_dir)

        try:
            names = os.listdir(set_dir)
        except Exception:
            return None

        osu_files = [os.path.join(set_dir, n) for n in names if n.lower().endswith(".osu")]
        if not osu_files:
            return None

        osu_files.sort(key=lambda p: os.path.basename(p).lower())
        sample_files = osu_files[:2] if len(osu_files) >= 2 else osu_files[:1]

        sample_infos = []
        mania_keys = set()

        for p in sample_files:
            info = parse_osu_minimal(p)
            if not info:
                continue
            sample_infos.append(info)
            if info["mode"] == 3 and info["key"] is not None:
                mania_keys.add(int(info["key"]))

        if not sample_infos or not mania_keys:
            return None

        rep = None
        for info in sample_infos:
            if info["mode"] == 3:
                rep = info
                break
        if rep is None:
            rep = sample_infos[0]

        return SetInfo(
            folder_name=folder_name,
            folder_path=set_dir,
            beatmap_set_id=rep.get("beatmap_set_id"),
            artist=rep.get("artist", ""),
            title=rep.get("title", ""),
            creator=rep.get("creator", ""),
            keys=sorted(list(mania_keys)),
            osu_count=len(osu_files),
            representative_osu=rep.get("osu_path", "")
        )

    def run(self):
        try:
            cache_mgr = CacheManager(self.osu_root)
            cache = cache_mgr.load()

            songs_mtime = os.path.getmtime(self.songs_dir) if os.path.isdir(self.songs_dir) else 0

            if not self.force_rescan:
                try:
                    if cache.get("songs_mtime") == songs_mtime and cache.get("sets"):
                        result = [SetInfo(**v) for v in cache["sets"].values()]
                        self.finished.emit(result, f"已从缓存加载，共 {len(result)} 个 mania set。")
                        return
                except Exception:
                    pass

            self.progress.emit("开始快速扫描 Songs ...")
            set_dirs = list_set_dirs(self.songs_dir)
            total = len(set_dirs)
            result = []

            for idx, set_dir in enumerate(set_dirs, 1):
                info = self.build_set_info(set_dir)
                if info:
                    result.append(info)

                if idx % 100 == 0 or idx == total:
                    self.progress.emit(f"扫描进度: {idx}/{total}")

            cache["songs_mtime"] = songs_mtime
            cache["sets"] = {s.folder_path: asdict(s) for s in result}
            cache_mgr.save(cache)

            self.finished.emit(result, f"扫描完成，共找到 {len(result)} 个 mania set。")
        except Exception:
            self.failed.emit(traceback.format_exc())

# ---------------------------
# Export worker
# ---------------------------

class ExportWorker(QThread):
    progress = Signal(str, int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, sets_to_export: List[SetInfo], output_dir: str):
        super().__init__()
        self.sets_to_export = sets_to_export
        self.output_dir = output_dir

    def build_osz_name(self, s: SetInfo) -> str:
        artist = s.artist or "Unknown Artist"
        title = s.title or s.folder_name
        creator = s.creator or "Unknown"
        sid = f"[{s.beatmap_set_id}]" if s.beatmap_set_id else ""
        return sanitize_filename(f"{artist} - {title} ({creator}) {sid}".strip()) + ".osz"

    def run(self):
        try:
            total = len(self.sets_to_export)
            if total == 0:
                self.finished.emit("没有可导出的 set。")
                return

            ensure_dir(self.output_dir)

            for idx, s in enumerate(self.sets_to_export, 1):
                osz_name = self.build_osz_name(s)
                out_path = os.path.join(self.output_dir, osz_name)

                if os.path.exists(out_path):
                    base, ext = os.path.splitext(out_path)
                    n = 2
                    while True:
                        cand = f"{base} ({n}){ext}"
                        if not os.path.exists(cand):
                            out_path = cand
                            break
                        n += 1

                with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(s.folder_path):
                        for fn in files:
                            full = os.path.join(root, fn)
                            arc = os.path.relpath(full, s.folder_path)
                            try:
                                zf.write(full, arc)
                            except Exception:
                                pass

                percent = int(idx * 100 / total)
                self.progress.emit(f"导出中 {idx}/{total}: {os.path.basename(out_path)}", percent)

            self.finished.emit(f"导出完成，共导出 {total} 个 .osz 文件。")
        except Exception:
            self.failed.emit(traceback.format_exc())

# ---------------------------
# Filter
# ---------------------------

def parse_query(query: str):
    q = query.strip().lower()
    tokens = [x for x in re.split(r"\s+", q) if x]
    text_terms = []
    key_terms = []

    for t in tokens:
        m = re.match(r"^(?:key[:=])(\d+)$", t)
        if m:
            key_terms.append(int(m.group(1)))
            continue
        m = re.match(r"^(\d+)k$", t)
        if m:
            key_terms.append(int(m.group(1)))
            continue
        text_terms.append(t)

    return text_terms, key_terms

def match_set_info(s: SetInfo, query: str) -> bool:
    if not query.strip():
        return True

    text_terms, key_terms = parse_query(query)

    if key_terms:
        set_keys = set(s.keys)
        for k in key_terms:
            if k not in set_keys:
                return False

    hay = " ".join([
        s.artist or "",
        s.title or "",
        s.creator or "",
        s.folder_name or "",
        str(s.beatmap_set_id or "")
    ]).lower()

    for t in text_terms:
        if t not in hay:
            return False

    return True

# ---------------------------
# UI helpers
# ---------------------------

def make_placeholder_pixmap(width=320, height=180, text="Set Folder") -> QPixmap:
    pix = QPixmap(width, height)
    pix.fill(QColor("#3a3d46"))
    painter = QPainter(pix)
    painter.setPen(QColor("#dcdcdc"))
    painter.drawText(pix.rect(), Qt.AlignCenter, text)
    painter.end()
    return pix

class ClickableFrame(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

# ---------------------------
# UI card
# ---------------------------

class SetCard(QFrame):
    selection_changed = Signal(str, bool)  # folder_path, checked
    open_folder_requested = Signal(str)    # folder_path

    def __init__(self, info: SetInfo, placeholder: QPixmap, checked: bool = False):
        super().__init__()
        self.info = info
        self.placeholder = placeholder
        self.setObjectName("SetCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
#SetCard {
    background: #23252b;
    border: 1px solid #3a3d46;
    border-radius: 10px;
}
#SetCard:hover {
    border: 1px solid #5a8cff;
}
QLabel { color: #e8e8e8; }
QCheckBox { color: #ffffff; }
QFrame#CardBody {
    background: transparent;
    border: none;
}
QFrame#CardBody:hover {
    background: rgba(255,255,255,0.02);
}
""")
        self.init_ui(checked)

    def init_ui(self, checked: bool):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(checked)
        self.checkbox.toggled.connect(self.on_toggled)

        top.addWidget(self.checkbox)
        top.addStretch()

        self.body = ClickableFrame()
        self.body.setObjectName("CardBody")
        self.body.clicked.connect(self.on_open_folder)

        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)

        self.thumb = QLabel()
        self.thumb.setFixedSize(320, 180)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setPixmap(self.placeholder)

        title = QLabel(f"{self.info.artist} - {self.info.title}")
        title.setStyleSheet("font-size:16px; font-weight:700;")
        title.setWordWrap(True)

        creator = QLabel(f"Mapper: {self.info.creator or 'Unknown'}")
        creator.setStyleSheet("color:#bfc7d5;")

        sid = QLabel(f"SetID: {self.info.beatmap_set_id if self.info.beatmap_set_id else 'None'}")
        sid.setStyleSheet("color:#9aa3b2;")

        keys_text = " / ".join(f"{k}K" for k in self.info.keys) if self.info.keys else "Unknown Key"
        keys = QLabel(f"Mode: mania    Keys: {keys_text}")
        keys.setStyleSheet("color:#8fe388; font-weight:600;")

        counts = QLabel(f".osu 数量: {self.info.osu_count}")
        counts.setStyleSheet("color:#c8ced8;")

        folder = QLabel(f"Folder: {self.info.folder_name}")
        folder.setStyleSheet("color:#8f98a8;")
        folder.setWordWrap(True)

        hint = QLabel("点击卡片打开 set 文件夹")
        hint.setStyleSheet("color:#6fa8ff; font-style:italic;")

        body_layout.addWidget(self.thumb)
        body_layout.addWidget(title)
        body_layout.addWidget(creator)
        body_layout.addWidget(sid)
        body_layout.addWidget(keys)
        body_layout.addWidget(counts)
        body_layout.addWidget(folder)
        body_layout.addWidget(hint)

        root.addLayout(top)
        root.addWidget(self.body)

    def on_toggled(self, checked: bool):
        self.selection_changed.emit(self.info.folder_path, checked)

    def on_open_folder(self):
        self.open_folder_requested.emit(self.info.folder_path)

    def set_checked_silent(self, checked: bool):
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(checked)
        self.checkbox.blockSignals(False)

# ---------------------------
# Main window
# ---------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.osu_root = detect_osu_root() or ""
        self.songs_dir = os.path.join(self.osu_root, "Songs") if self.osu_root else ""

        self.all_sets: List[SetInfo] = []
        self.filtered_sets: List[SetInfo] = []
        self.cards: List[SetCard] = []

        self.page_size = 120
        self.current_page = 1

        self.scan_worker = None
        self.export_worker = None

        self.selected_paths: Set[str] = set()

        self.placeholder = make_placeholder_pixmap()

        self.setWindowTitle(APP_NAME)
        self.resize(1280, 860)
        self.init_ui()

        if self.osu_root:
            self.path_label.setText(f"osu! stable 路径: {self.osu_root}")
        else:
            self.path_label.setText("未自动识别到 osu! stable 路径，请手动选择。")

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top_bar = QHBoxLayout()
        self.path_label = QLabel("osu! 路径: ")
        self.path_label.setStyleSheet("color:#d8dee9;")

        btn_choose = QPushButton("选择 osu! 根目录")
        btn_choose.clicked.connect(self.choose_osu_root)

        btn_scan = QPushButton("扫描 Songs")
        btn_scan.clicked.connect(self.start_scan)

        btn_force = QPushButton("强制重扫")
        btn_force.clicked.connect(lambda: self.start_scan(force=True))

        btn_export = QPushButton("导出已选 set")
        btn_export.clicked.connect(self.start_export)

        top_bar.addWidget(self.path_label, 1)
        top_bar.addWidget(btn_choose)
        top_bar.addWidget(btn_scan)
        top_bar.addWidget(btn_force)
        top_bar.addWidget(btn_export)

        filter_bar = QHBoxLayout()
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("搜索示例：7k / key=4 / Camellia / 标题关键字")
        self.query_edit.returnPressed.connect(self.apply_filter)

        btn_apply = QPushButton("应用筛选")
        btn_apply.clicked.connect(self.apply_filter)

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self.clear_filter)

        self.page_size_spin = QSpinBox()
        self.page_size_spin.setRange(30, 500)
        self.page_size_spin.setValue(self.page_size)
        self.page_size_spin.valueChanged.connect(self.on_page_size_changed)

        filter_bar.addWidget(QLabel("筛选："))
        filter_bar.addWidget(self.query_edit, 1)
        filter_bar.addWidget(btn_apply)
        filter_bar.addWidget(btn_clear)
        filter_bar.addWidget(QLabel("每页："))
        filter_bar.addWidget(self.page_size_spin)

        page_bar = QHBoxLayout()
        self.page_info_label = QLabel("第 0 / 0 页")
        btn_prev = QPushButton("上一页")
        btn_prev.clicked.connect(self.prev_page)
        btn_next = QPushButton("下一页")
        btn_next.clicked.connect(self.next_page)

        btn_select_all_filtered = QPushButton("全选当前筛选结果")
        btn_select_all_filtered.clicked.connect(self.select_all_filtered)

        btn_unselect_all_filtered = QPushButton("取消全选当前筛选结果")
        btn_unselect_all_filtered.clicked.connect(self.unselect_all_filtered)

        page_bar.addWidget(self.page_info_label)
        page_bar.addStretch()
        page_bar.addWidget(btn_prev)
        page_bar.addWidget(btn_next)
        page_bar.addWidget(btn_select_all_filtered)
        page_bar.addWidget(btn_unselect_all_filtered)

        self.selection_info_label = QLabel("已选 0 个")
        self.status_label = QLabel("就绪")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)

        self.cards_container = QWidget()
        self.grid = QGridLayout(self.cards_container)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.grid.setSpacing(12)
        self.scroll.setWidget(self.cards_container)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFixedHeight(140)

        root.addLayout(top_bar)
        root.addLayout(filter_bar)
        root.addLayout(page_bar)
        root.addWidget(self.selection_info_label)
        root.addWidget(self.status_label)
        root.addWidget(self.progress_bar)
        root.addWidget(self.scroll, 1)
        root.addWidget(QLabel("日志："))
        root.addWidget(self.log_edit)

        self.setStyleSheet("""
QMainWindow, QWidget {
    background: #1b1d22;
    color: #e6e6e6;
}
QLineEdit, QTextEdit, QSpinBox {
    background: #23262d;
    border: 1px solid #3a3f4b;
    border-radius: 8px;
    padding: 6px;
    color: #f0f0f0;
}
QPushButton {
    background: #2b67f6;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 8px 12px;
}
QPushButton:hover {
    background: #3a78ff;
}
QPushButton:disabled {
    background: #555;
}
QScrollArea {
    border: 1px solid #333842;
    border-radius: 8px;
}
QProgressBar {
    background: #23262d;
    border: 1px solid #3a3f4b;
    border-radius: 6px;
    text-align: center;
}
QProgressBar::chunk {
    background: #4c8dff;
    border-radius: 6px;
}
""")

    def log(self, text: str):
        self.log_edit.append(text)

    def update_selection_label(self):
        self.selection_info_label.setText(f"已选 {len(self.selected_paths)} 个")

    def choose_osu_root(self):
        path = QFileDialog.getExistingDirectory(self, "选择 osu! stable 根目录")
        if not path:
            return
        songs = os.path.join(path, "Songs")
        if not os.path.isdir(songs):
            QMessageBox.warning(self, "无效路径", "该目录下未找到 Songs 文件夹，请选择 stable 客户端根目录。")
            return
        self.osu_root = path
        self.songs_dir = songs
        self.path_label.setText(f"osu! stable 路径: {self.osu_root}")

    def clear_filter(self):
        self.query_edit.clear()
        self.apply_filter()

    def on_page_size_changed(self, value: int):
        self.page_size = value
        self.current_page = 1
        self.render_current_page()

    def clear_cards(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.cards.clear()

    def get_total_pages(self) -> int:
        if not self.filtered_sets:
            return 0
        return (len(self.filtered_sets) + self.page_size - 1) // self.page_size

    def render_current_page(self):
        self.clear_cards()

        total_pages = self.get_total_pages()
        if total_pages == 0:
            self.page_info_label.setText("第 0 / 0 页")
            self.status_label.setText("当前显示 0 个 set")
            return

        if self.current_page < 1:
            self.current_page = 1
        if self.current_page > total_pages:
            self.current_page = total_pages

        start = (self.current_page - 1) * self.page_size
        end = min(start + self.page_size, len(self.filtered_sets))
        page_items = self.filtered_sets[start:end]

        cols = 3
        row = 0
        col = 0
        for s in page_items:
            checked = s.folder_path in self.selected_paths
            card = SetCard(s, self.placeholder, checked=checked)
            card.selection_changed.connect(self.on_card_selection_changed)
            card.open_folder_requested.connect(self.open_set_folder)

            self.cards.append(card)
            self.grid.addWidget(card, row, col)

            col += 1
            if col >= cols:
                col = 0
                row += 1

        self.page_info_label.setText(f"第 {self.current_page} / {total_pages} 页")
        self.status_label.setText(f"当前显示 {len(page_items)} 个 set，筛选结果总数 {len(self.filtered_sets)}")
        self.update_selection_label()

    def apply_filter(self):
        query = self.query_edit.text().strip()
        self.filtered_sets = [s for s in self.all_sets if match_set_info(s, query)]
        self.current_page = 1
        self.render_current_page()

    def prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self.render_current_page()

    def next_page(self):
        total_pages = self.get_total_pages()
        if self.current_page < total_pages:
            self.current_page += 1
            self.render_current_page()

    def on_card_selection_changed(self, folder_path: str, checked: bool):
        if checked:
            self.selected_paths.add(folder_path)
        else:
            self.selected_paths.discard(folder_path)
        self.update_selection_label()

    def select_all_filtered(self):
        for s in self.filtered_sets:
            self.selected_paths.add(s.folder_path)

        visible_paths = {c.info.folder_path for c in self.cards}
        for card in self.cards:
            if card.info.folder_path in visible_paths:
                card.set_checked_silent(True)

        self.update_selection_label()
        self.log(f"已全选当前筛选结果，共 {len(self.filtered_sets)} 个。")

    def unselect_all_filtered(self):
        filtered_paths = {s.folder_path for s in self.filtered_sets}
        self.selected_paths.difference_update(filtered_paths)

        for card in self.cards:
            if card.info.folder_path in filtered_paths:
                card.set_checked_silent(False)

        self.update_selection_label()
        self.log(f"已取消全选当前筛选结果，共 {len(filtered_paths)} 个。")

    def open_set_folder(self, folder_path: str):
        try:
            open_in_explorer(folder_path)
            self.status_label.setText(f"已打开文件夹: {folder_path}")
        except Exception as e:
            self.log(traceback.format_exc())
            QMessageBox.warning(self, "打开失败", f"无法打开文件夹：\n{folder_path}\n\n错误：{e}")

    def start_scan(self, force: bool = False):
        if not self.osu_root or not os.path.isdir(self.songs_dir):
            QMessageBox.warning(self, "路径无效", "请先选择 osu! stable 根目录。")
            return

        self.progress_bar.setValue(0)
        self.status_label.setText("扫描中...")
        self.log("开始快速扫描 Songs ...")

        self.selected_paths.clear()
        self.update_selection_label()

        self.scan_worker = ScanWorker(self.osu_root, self.songs_dir, force_rescan=force)
        self.scan_worker.progress.connect(self.on_scan_progress)
        self.scan_worker.finished.connect(self.on_scan_finished)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.start()

    def on_scan_progress(self, msg: str):
        self.log(msg)
        self.status_label.setText(msg)

    def on_scan_finished(self, sets_: List[SetInfo], msg: str):
        self.all_sets = sets_
        self.log(msg)
        self.status_label.setText(msg)
        self.progress_bar.setValue(100)
        self.apply_filter()

    def on_scan_failed(self, err: str):
        self.log(err)
        self.status_label.setText("扫描失败")
        QMessageBox.critical(self, "扫描失败", err)

    def start_export(self):
        selected = [s for s in self.all_sets if s.folder_path in self.selected_paths]
        if not selected:
            QMessageBox.information(self, "未选择", "请先勾选要导出的 set。")
            return

        out_dir = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not out_dir:
            return

        self.progress_bar.setValue(0)
        self.status_label.setText("导出中...")
        self.log(f"开始导出 {len(selected)} 个 set ...")

        self.export_worker = ExportWorker(selected, out_dir)
        self.export_worker.progress.connect(self.on_export_progress)
        self.export_worker.finished.connect(self.on_export_finished)
        self.export_worker.failed.connect(self.on_export_failed)
        self.export_worker.start()

    def on_export_progress(self, msg: str, value: int):
        self.log(msg)
        self.status_label.setText(msg)
        self.progress_bar.setValue(value)

    def on_export_finished(self, msg: str):
        self.log(msg)
        self.status_label.setText(msg)
        self.progress_bar.setValue(100)
        QMessageBox.information(self, "完成", msg)

    def on_export_failed(self, err: str):
        self.log(err)
        self.status_label.setText("导出失败")
        QMessageBox.critical(self, "导出失败", err)

def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
