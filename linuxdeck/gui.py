"""PyQt6 Desktop Control Center & System Tray GUI for LinuxDeck."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from PIL import Image
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .autodiscover import _shorten_audio_label, discover_system_environment
from .controller import StreamControllerEngine

ICON_CHOICES = [
    "scene",
    "record",
    "stream",
    "obs",
    "headset",
    "speaker",
    "mic",
    "eq",
    "play_pause",
    "next",
    "prev",
    "cpu",
    "gpu",
    "ram",
    "net",
    "temp",
    "psu",
    "discord",
    "steam",
    "browser",
    "terminal",
    "camera",
    "brightness",
    "clock",
    "lock",
    "default",
]

KEY_ACTION_TYPES = [
    "obs_scene",
    "obs_record",
    "obs_stream",
    "obs_launch",
    "audio_mute",
    "audio_default_sink",
    "media",
    "telemetry",
    "command",
    "brightness_delta",
    "page",
    "none",
]

DIAL_ACTION_TYPES = [
    "audio",
    "brightness",
    "page_cycle",
]

BUTTON_ACTION_TYPES = [
    "page",
    "audio_mute",
    "obs_record",
    "obs_stream",
    "media",
    "command",
]


class BridgeSignals(QObject):
    """Thread-safe Qt signals bridged from the background controller daemon thread."""

    screen_updated = pyqtSignal(object)
    event_logged = pyqtSignal(str)
    page_changed = pyqtSignal(int)
    status_changed = pyqtSignal(bool, str, str)


class ScreenCanvasWidget(QWidget):
    """Interactive 480x270 screen preview widget with click/double-click selection."""

    key_selected = pyqtSignal(int)
    key_triggered = pyqtSignal(int)
    strip_selected = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(640, 360)
        self._pixmap: Optional[QPixmap] = None
        self.selected_kind: str = "key"
        self.selected_index: int = 0

    def update_from_pil(self, pil_img: Image.Image) -> None:
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        data = pil_img.tobytes("raw", "RGB")
        qimg = QImage(data, pil_img.width, pil_img.height, pil_img.width * 3, QImage.Format.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg.copy())
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if self._pixmap is not None:
            painter.drawPixmap(self.rect(), self._pixmap)
        else:
            painter.fillRect(self.rect(), QColor(10, 12, 16))

        pen_bezel = QPen(QColor(35, 42, 55), 2)
        painter.setPen(pen_bezel)
        painter.drawLine(80, 0, 80, 360)
        painter.drawLine(560, 0, 560, 360)

        scale = 640.0 / 480.0
        pen_sel = QPen(QColor(0, 255, 102), 3)
        painter.setPen(pen_sel)
        if self.selected_kind == "key" and 0 <= self.selected_index < 12:
            col = self.selected_index % 4
            row = self.selected_index // 4
            rx = int((60 + col * 90) * scale) + 2
            ry = int((row * 90) * scale) + 2
            rw = int(90 * scale) - 4
            rh = int(90 * scale) - 4
            painter.drawRoundedRect(rx, ry, rw, rh, 10, 10)
        elif self.selected_kind == "dial" and 0 <= self.selected_index < 6:
            sx = 0 if self.selected_index < 3 else 420
            sy = (self.selected_index % 3) * 90
            rx = int(sx * scale) + 2
            ry = int(sy * scale) + 2
            rw = int(60 * scale) - 4
            rh = int(90 * scale) - 4
            painter.drawRoundedRect(rx, ry, rw, rh, 8, 8)

    def _map_pos(self, pos) -> tuple[str, int]:
        x_hw = int(pos.x() * 480 / self.width())
        y_hw = int(pos.y() * 270 / self.height())
        row = min(2, max(0, y_hw // 90))
        if x_hw < 60:
            return ("dial", row)
        if x_hw < 420:
            col = min(3, max(0, (x_hw - 60) // 90))
            return ("key", row * 4 + col)
        return ("dial", 3 + row)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        kind, idx = self._map_pos(event.position())
        self.selected_kind = kind
        self.selected_index = idx
        self.update()
        if kind == "key":
            self.key_selected.emit(idx)
        else:
            self.strip_selected.emit(idx)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        kind, idx = self._map_pos(event.position())
        if kind == "key":
            self.key_triggered.emit(idx)


def get_app_icon() -> QIcon:
    """Load the LinuxDeck application icon across source, PyInstaller, and system installs."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "linuxdeck", "assets", "linuxdeck.png"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.extend([
        os.path.join(here, "assets", "linuxdeck.png"),
        os.path.join(here, "..", "packaging", "linuxdeck.png"),
        os.path.expanduser("~/.local/share/icons/hicolor/512x512/apps/linuxdeck.png"),
        os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps/linuxdeck.png"),
        "/usr/share/icons/hicolor/512x512/apps/linuxdeck.png",
        "/usr/share/pixmaps/linuxdeck.png",
    ])
    for path in candidates:
        if os.path.exists(path):
            icon = QIcon(path)
            if not icon.isNull():
                return icon
    return QIcon.fromTheme("linuxdeck")


class StreamControllerMainWindow(QMainWindow):
    """Main PyQt6 window for configuring and monitoring LinuxDeck."""

    def __init__(self, engine: StreamControllerEngine, start_minimized: bool = False) -> None:
        super().__init__()
        self.engine = engine
        self.signals = BridgeSignals()

        self.setWindowTitle("LinuxDeck — Open-Source Loupedeck & Razer Stream Controller")
        self.setWindowIcon(get_app_icon())
        self.resize(1180, 740)
        self._apply_dark_theme()

        self.selected_kind = "key"
        self.selected_index = 0

        self._build_ui()
        self._setup_tray()

        self.signals.screen_updated.connect(self._on_screen_updated)
        self.signals.event_logged.connect(self._on_event_logged)
        self.signals.page_changed.connect(self._on_page_changed)
        self.signals.status_changed.connect(self._on_status_changed)

        self.engine.on_screen_updated = lambda img: self.signals.screen_updated.emit(img)
        self.engine.on_event_log = lambda msg: self.signals.event_logged.emit(msg)
        self.engine.on_page_changed = lambda pg: self.signals.page_changed.emit(pg)
        self.engine.on_status_changed = lambda c, s, f: self.signals.status_changed.emit(c, s, f)

        self._on_screen_updated(self.engine.last_screen_image)
        self._on_status_changed(
            self.engine.driver.connected,
            self.engine.driver.serial_number,
            self.engine.driver.firmware_version,
        )
        self._load_selection_into_inspector("key", 0)

        self._led_timer = QTimer(self)
        self._led_timer.timeout.connect(self._sync_hw_button_styles)
        self._led_timer.start(400)

        if start_minimized:
            self.hide()
        else:
            self.show()

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #0d1016;
                color: #e6edf5;
                font-family: "Noto Sans", sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #232a38;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
                color: #00ff66;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QPushButton {
                background-color: #1a212d;
                border: 1px solid #2c3648;
                border-radius: 6px;
                padding: 6px 12px;
                color: #e6edf5;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #242e3f;
                border-color: #00ff66;
            }
            QPushButton:checked {
                background-color: #00381b;
                border: 2px solid #00ff66;
                color: #00ff66;
            }
            QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
                background-color: #141923;
                border: 1px solid #2b3547;
                border-radius: 5px;
                padding: 5px 8px;
                color: #f0f5fa;
            }
            """
        )

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(14, 12, 14, 12)
        root_layout.setSpacing(10)

        # 1. Top Status Bar
        top_bar = QHBoxLayout()
        self.lbl_status = QLabel("Connecting to controller...")
        self.lbl_status.setStyleSheet("font-weight: bold; color: #00ff66; font-size: 14px;")
        top_bar.addWidget(self.lbl_status)
        top_bar.addStretch()

        top_bar.addWidget(QLabel("Brightness:"))
        self.slider_brightness = QSlider(Qt.Orientation.Horizontal)
        self.slider_brightness.setRange(1, 10)
        self.slider_brightness.setValue(self.engine.profile.brightness)
        self.slider_brightness.setFixedWidth(100)
        self.slider_brightness.valueChanged.connect(self._on_brightness_slider)
        top_bar.addWidget(self.slider_brightness)

        self.chk_haptics = QCheckBox("Haptics")
        self.chk_haptics.setChecked(self.engine.profile.haptics_enabled)
        self.chk_haptics.toggled.connect(self._on_haptics_toggled)
        top_bar.addWidget(self.chk_haptics)

        btn_autoconfig = QPushButton("Auto-Configure for This PC")
        btn_autoconfig.setToolTip("Automatically detect this PC's audio devices, GPU/CPU sensors, OBS scenes, and apps")
        btn_autoconfig.clicked.connect(self._run_auto_configure)
        top_bar.addWidget(btn_autoconfig)

        btn_service = QPushButton("Install Autostart Service")
        btn_service.clicked.connect(self._install_systemd_service)
        top_bar.addWidget(btn_service)

        root_layout.addLayout(top_bar)

        # 2. Main Split Area: Hardware Deck Replica (Left) + Inspector (Right)
        split = QHBoxLayout()
        split.setSpacing(14)

        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        page_bar = QHBoxLayout()
        page_bar.addWidget(QLabel("Active Page:"))
        self.page_buttons: list[QPushButton] = []
        for idx, page in enumerate(self.engine.profile.pages):
            btn = QPushButton(f"{idx + 1}: {page.name}")
            btn.setCheckable(True)
            btn.setChecked(idx == self.engine.current_page_index)
            btn.clicked.connect(lambda _checked, i=idx: self.engine.set_page(i))
            self.page_buttons.append(btn)
            page_bar.addWidget(btn)
        page_bar.addStretch()
        left_col.addLayout(page_bar)

        deck_box = QGroupBox("LinuxDeck Live Surface (Click to Edit, Double-Click Key to Trigger)")
        deck_v = QVBoxLayout(deck_box)

        deck_mid = QHBoxLayout()
        left_dials_col = QVBoxLayout()
        self.dial_buttons: list[QPushButton] = []
        for d_idx in range(3):
            left_dials_col.addWidget(self._create_dial_widget(d_idx))
        deck_mid.addLayout(left_dials_col)

        self.canvas = ScreenCanvasWidget()
        self.canvas.key_selected.connect(lambda k: self._load_selection_into_inspector("key", k))
        self.canvas.key_triggered.connect(lambda k: self.engine.trigger_key(k))
        self.canvas.strip_selected.connect(lambda d: self._load_selection_into_inspector("dial", d))
        deck_mid.addWidget(self.canvas)

        right_dials_col = QVBoxLayout()
        for d_idx in range(3, 6):
            right_dials_col.addWidget(self._create_dial_widget(d_idx))
        deck_mid.addLayout(right_dials_col)

        deck_v.addLayout(deck_mid)

        btn_row = QHBoxLayout()
        self.hw_btn_widgets: list[QPushButton] = []
        for b_idx in range(8):
            lbl = "Home (0)" if b_idx == 0 else f"Btn {b_idx}"
            b_widget = QPushButton(lbl)
            b_widget.setMinimumHeight(38)
            b_widget.clicked.connect(lambda _c, bi=b_idx: self._on_gui_hw_button_clicked(bi))
            self.hw_btn_widgets.append(b_widget)
            btn_row.addWidget(b_widget)
        deck_v.addLayout(btn_row)

        left_col.addWidget(deck_box)

        log_box = QGroupBox("Live Hardware & Integration Event Log")
        log_layout = QVBoxLayout(log_box)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumHeight(120)
        log_layout.addWidget(self.txt_log)
        left_col.addWidget(log_box)

        split.addLayout(left_col, stretch=3)

        right_box = QGroupBox("Control Inspector & Action Editor")
        right_layout = QVBoxLayout(right_box)

        self.lbl_selected_header = QLabel("Editing: Touch Key #1")
        self.lbl_selected_header.setStyleSheet("font-size: 15px; font-weight: bold; color: #00e5ff;")
        right_layout.addWidget(self.lbl_selected_header)

        form = QFormLayout()
        form.setSpacing(10)

        self.inp_title = QLineEdit()
        form.addRow("Label / Title:", self.inp_title)

        self.cmb_icon = QComboBox()
        self.cmb_icon.addItems(ICON_CHOICES)
        form.addRow("Vector Icon:", self.cmb_icon)

        color_row = QHBoxLayout()
        self.inp_color = QLineEdit("#00FF66")
        btn_pick_color = QPushButton("Pick Color")
        btn_pick_color.clicked.connect(self._pick_accent_color)
        color_row.addWidget(self.inp_color)
        color_row.addWidget(btn_pick_color)
        form.addRow("Accent / LED Color:", color_row)

        self.cmb_action_type = QComboBox()
        self.cmb_action_type.addItems(KEY_ACTION_TYPES)
        self.cmb_action_type.currentTextChanged.connect(self._populate_preset_targets)
        form.addRow("Action Type:", self.cmb_action_type)

        self.inp_param = QLineEdit()
        form.addRow("Target / Command:", self.inp_param)

        self.cmb_presets = QComboBox()
        self.cmb_presets.activated.connect(self._on_preset_chosen)
        form.addRow("Quick Presets:", self.cmb_presets)

        self.spin_step = QSpinBox()
        self.spin_step.setRange(1, 25)
        self.spin_step.setValue(4)
        form.addRow("Dial Step (%):", self.spin_step)

        right_layout.addLayout(form)

        btn_save = QPushButton("Apply & Save to Profile")
        btn_save.setStyleSheet(
            "background-color: #005a2b; border: 1px solid #00ff66; color: #ffffff; padding: 9px; font-size: 14px;"
        )
        btn_save.clicked.connect(self._save_inspector_changes)
        right_layout.addWidget(btn_save)

        btn_trigger = QPushButton("Trigger / Test Action Now")
        btn_trigger.clicked.connect(self._trigger_selected_control)
        right_layout.addWidget(btn_trigger)

        right_layout.addStretch()
        split.addWidget(right_box, stretch=1)

        root_layout.addLayout(split)

    def _create_dial_widget(self, dial_idx: int) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet("QFrame { background-color: #141923; border: 1px solid #263042; border-radius: 8px; }")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        dcfg = self.engine.profile.dials[dial_idx]
        btn_select = QPushButton(f"Dial {dial_idx + 1}: {dcfg.label}")
        btn_select.clicked.connect(lambda _c, di=dial_idx: self._load_selection_into_inspector("dial", di))
        self.dial_buttons.append(btn_select)
        lay.addWidget(btn_select)

        ctrl_row = QHBoxLayout()
        btn_minus = QPushButton("–")
        btn_minus.setFixedWidth(30)
        btn_minus.clicked.connect(lambda _c, di=dial_idx: self.engine._on_hw_dial_rotate(di, -1))
        btn_press = QPushButton("Push")
        btn_press.clicked.connect(lambda _c, di=dial_idx: self.engine._on_hw_dial_press(di, True))
        btn_plus = QPushButton("+")
        btn_plus.setFixedWidth(30)
        btn_plus.clicked.connect(lambda _c, di=dial_idx: self.engine._on_hw_dial_rotate(di, +1))
        ctrl_row.addWidget(btn_minus)
        ctrl_row.addWidget(btn_press)
        ctrl_row.addWidget(btn_plus)
        lay.addLayout(ctrl_row)
        return frame

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        app_icon = get_app_icon()
        if app_icon.isNull():
            pix = QPixmap(32, 32)
            pix.fill(QColor(0, 0, 0, 0))
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor(0, 255, 102))
            p.setPen(QPen(QColor(15, 20, 28), 2))
            p.drawEllipse(4, 4, 24, 24)
            p.end()
            app_icon = QIcon(pix)

        self.tray = QSystemTrayIcon(app_icon, self)
        self.tray.setToolTip("LinuxDeck")
        menu = QMenu()
        act_show = QAction("Open LinuxDeck Control Center", self)
        act_show.triggered.connect(self._restore_from_tray)
        menu.addAction(act_show)
        menu.addSeparator()
        act_quit = QAction("Quit LinuxDeck", self)
        act_quit.triggered.connect(self._quit_application)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self._restore_from_tray()

    def closeEvent(self, event) -> None:
        if hasattr(self, "tray") and self.tray.isVisible():
            self.hide()
            event.ignore()
        else:
            self._quit_application()

    def _quit_application(self) -> None:
        self.engine.stop()
        QApplication.quit()

    # -------------------------------------------------------------------------
    # Inspector & Live Sync
    # -------------------------------------------------------------------------

    def _load_selection_into_inspector(self, kind: str, index: int) -> None:
        self.selected_kind = kind
        self.selected_index = index
        self.canvas.selected_kind = kind
        self.canvas.selected_index = index
        self.canvas.update()

        self.cmb_action_type.blockSignals(True)
        self.cmb_action_type.clear()

        if kind == "key":
            page = self.engine.profile.pages[self.engine.current_page_index]
            kcfg = page.keys[index]
            self.lbl_selected_header.setText(f"Editing: Page {self.engine.current_page_index + 1} — Key #{index + 1}")
            self.inp_title.setText(kcfg.title)
            self.cmb_icon.setEnabled(True)
            self.cmb_icon.setCurrentText(kcfg.icon)
            self.inp_color.setText(kcfg.accent_color)
            self.cmb_action_type.addItems(KEY_ACTION_TYPES)
            self.cmb_action_type.setCurrentText(kcfg.action_type)
            self.inp_param.setText(kcfg.param)
            self.spin_step.setEnabled(False)

        elif kind == "dial":
            dcfg = self.engine.profile.dials[index]
            self.lbl_selected_header.setText(f"Editing: Rotary Dial #{index + 1} ({dcfg.label})")
            self.inp_title.setText(dcfg.label)
            self.cmb_icon.setEnabled(False)
            self.inp_color.setText(dcfg.accent_color)
            self.cmb_action_type.addItems(DIAL_ACTION_TYPES)
            self.cmb_action_type.setCurrentText(dcfg.action_type)
            self.inp_param.setText(dcfg.param)
            self.spin_step.setEnabled(True)
            self.spin_step.setValue(dcfg.step)

        elif kind == "button":
            bcfg = self.engine.profile.buttons[index]
            lbl_name = "Home Circle (0)" if index == 0 else f"Bottom Button #{index}"
            self.lbl_selected_header.setText(f"Editing: {lbl_name}")
            self.inp_title.setText(bcfg.label)
            self.cmb_icon.setEnabled(False)
            self.inp_color.setText(bcfg.color)
            self.cmb_action_type.addItems(BUTTON_ACTION_TYPES)
            self.cmb_action_type.setCurrentText(bcfg.action_type)
            self.inp_param.setText(bcfg.param)
            self.spin_step.setEnabled(False)

        self.cmb_action_type.blockSignals(False)
        self._populate_preset_targets(self.cmb_action_type.currentText())

    def _populate_preset_targets(self, action_type: str) -> None:
        self.cmb_presets.clear()
        self.cmb_presets.addItem("-- Choose a discovered preset --", "")
        at = action_type.lower()

        if at in ("audio", "audio_mute", "audio_default_sink"):
            self.cmb_presets.addItem("Default Output Sink (default_sink)", "default_sink")
            self.cmb_presets.addItem("Default Input Mic (default_source)", "default_source")
            for k, ep in self.engine.audio.sinks.items():
                lbl, _dl, qry = _shorten_audio_label(ep.description, "Sink")
                self.cmb_presets.addItem(f"Output: {ep.description} ({qry})", qry)
            for k, ep in self.engine.audio.sources.items():
                lbl, _dl, qry = _shorten_audio_label(ep.description, "Mic")
                self.cmb_presets.addItem(f"Input: {ep.description} ({qry})", qry)
            self.cmb_presets.addItem("Category: Chat / Discord / Vesktop (app:chat)", "app:chat")
            self.cmb_presets.addItem("Category: Web Browser (app:browser)", "app:browser")
            self.cmb_presets.addItem("Category: Games / Steam (app:game)", "app:game")
            self.cmb_presets.addItem("Category: Music Player (app:music)", "app:music")
            for k, ep in self.engine.audio.app_streams.items():
                self.cmb_presets.addItem(f"Active App: {ep.name} (app:{k})", f"app:{k}")

        elif at == "obs_scene":
            for sc in self.engine.obs.scenes:
                self.cmb_presets.addItem(f"OBS Scene: {sc}", sc)

        elif at == "media":
            for m in ("PlayPause", "Next", "Previous", "Stop"):
                self.cmb_presets.addItem(f"MPRIS: {m}", m)

        elif at == "telemetry":
            for code, desc in [
                ("cpu", "CPU Load % + Temp + Sparkline"),
                ("cpu_temp", "CPU Temperature (°C)"),
                ("gpu", "GPU Load % + Temp + Sparkline"),
                ("vram", "GPU VRAM Usage (GB)"),
                ("ram", "System RAM Usage + Sparkline"),
                ("nvme_temp", "Drive / NVMe Temp (°C)"),
                ("psu_temp", "PSU / Motherboard Temp (°C)"),
                ("battery", "Battery Percentage (%)"),
                ("net_down", "Network Download Speed (MB/s)"),
                ("net_up", "Network Upload Speed (MB/s)"),
                ("clock", "Digital Clock (HH:MM:SS)"),
            ]:
                self.cmb_presets.addItem(desc, code)

        elif at == "command":
            sys_info = discover_system_environment()
            presets = [
                (f"Launch {sys_info.chat_app.title}", sys_info.chat_app.command),
                (f"Launch {sys_info.browser_app.title}", sys_info.browser_app.command),
                (f"Launch {sys_info.game_app.title}", sys_info.game_app.command),
                (f"Launch {sys_info.extra_app.title}", sys_info.extra_app.command),
                ("Open Terminal", sys_info.terminal_cmd),
                ("Take Screenshot", sys_info.screenshot_cmd),
                ("Lock Session", sys_info.lock_cmd),
            ]
            if sys_info.has_easyeffects:
                presets.insert(0, ("Launch EasyEffects", sys_info.easyeffects_cmd))
            for name, cmd in presets:
                self.cmb_presets.addItem(name, cmd)

        elif at == "page":
            for idx, pg in enumerate(self.engine.profile.pages):
                self.cmb_presets.addItem(f"Page {idx + 1}: {pg.name}", str(idx))

    def _on_preset_chosen(self, index: int) -> None:
        val = self.cmb_presets.itemData(index)
        if val:
            self.inp_param.setText(str(val))

    def _pick_accent_color(self) -> None:
        col = QColorDialog.getColor(QColor(self.inp_color.text()), self, "Select Accent / LED Color")
        if col.isValid():
            self.inp_color.setText(col.name().upper())

    def _save_inspector_changes(self) -> None:
        kind = self.selected_kind
        idx = self.selected_index
        if kind == "key":
            page = self.engine.profile.pages[self.engine.current_page_index]
            kcfg = page.keys[idx]
            kcfg.title = self.inp_title.text().strip() or "Key"
            kcfg.icon = self.cmb_icon.currentText()
            kcfg.accent_color = self.inp_color.text().strip() or "#00FF66"
            kcfg.action_type = self.cmb_action_type.currentText()
            kcfg.param = self.inp_param.text().strip()
        elif kind == "dial":
            dcfg = self.engine.profile.dials[idx]
            dcfg.label = self.inp_title.text().strip() or f"DIAL {idx + 1}"
            dcfg.accent_color = self.inp_color.text().strip() or "#00FF66"
            dcfg.action_type = self.cmb_action_type.currentText()
            dcfg.param = self.inp_param.text().strip()
            dcfg.step = self.spin_step.value()
            self.dial_buttons[idx].setText(f"Dial {idx + 1}: {dcfg.label}")
        elif kind == "button":
            bcfg = self.engine.profile.buttons[idx]
            bcfg.label = self.inp_title.text().strip() or f"Btn {idx}"
            bcfg.color = self.inp_color.text().strip() or "#00FF66"
            bcfg.action_type = self.cmb_action_type.currentText()
            bcfg.param = self.inp_param.text().strip()

        self.engine.save_config()
        self.engine.update_button_leds()
        self.engine.request_render()
        self.engine.log_event(f"Saved configuration for {kind} #{idx + 1}")

    def _run_auto_configure(self) -> None:
        reply = QMessageBox.question(
            self,
            "Auto-Configure for This PC",
            "Scan this PC's audio devices, GPU/CPU sensors, OBS scenes, and installed apps and regenerate the profile?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.engine.auto_configure_for_system()
            for i, dcfg in enumerate(self.engine.profile.dials[:6]):
                self.dial_buttons[i].setText(f"Dial {i + 1}: {dcfg.label}")
            self._load_selection_into_inspector(self.selected_kind, self.selected_index)

    def _trigger_selected_control(self) -> None:
        if self.selected_kind == "key":
            self.engine.trigger_key(self.selected_index)
        elif self.selected_kind == "dial":
            self.engine._on_hw_dial_press(self.selected_index, True)
        elif self.selected_kind == "button":
            self.engine._on_hw_button(self.selected_index, True)

    def _on_gui_hw_button_clicked(self, btn_idx: int) -> None:
        self._load_selection_into_inspector("button", btn_idx)
        self.engine._on_hw_button(btn_idx, True)

    def _on_brightness_slider(self, value: int) -> None:
        self.engine.profile.brightness = value
        self.engine.driver.set_brightness(value)
        self.engine.save_config()
        self.engine.request_render()

    def _on_haptics_toggled(self, checked: bool) -> None:
        self.engine.profile.haptics_enabled = checked
        self.engine.save_config()

    def _on_screen_updated(self, pil_img: Image.Image) -> None:
        self.canvas.update_from_pil(pil_img)

    def _on_event_logged(self, msg: str) -> None:
        self.txt_log.appendPlainText(msg)

    def _on_page_changed(self, page_idx: int) -> None:
        for i, btn in enumerate(self.page_buttons):
            btn.setChecked(i == page_idx)
        if self.selected_kind == "key":
            self._load_selection_into_inspector("key", self.selected_index)

    def _on_status_changed(self, connected: bool, serial_num: str, fw_ver: str) -> None:
        obs_str = "OBS: Connected" if self.engine.obs.connected else "OBS: Standby (WebSocket Ready)"
        if connected:
            self.lbl_status.setText(
                f"● Connected: {self.engine.driver.device_name} ({self.engine.driver.port_path}) | SN: {serial_num or 'N/A'} | FW: {fw_ver or '0.2.x'} | {obs_str}"
            )
            self.lbl_status.setStyleSheet("font-weight: bold; color: #00ff66; font-size: 14px;")
        else:
            self.lbl_status.setText(f"○ Disconnected (Waiting for Loupedeck / Razer Stream Controller) | {obs_str}")
            self.lbl_status.setStyleSheet("font-weight: bold; color: #ff5252; font-size: 14px;")

    def _sync_hw_button_styles(self) -> None:
        for idx, b_widget in enumerate(self.hw_btn_widgets):
            r, g, b = self.engine.last_button_leds[idx]
            bcfg = self.engine.profile.buttons[idx]
            prefix = "Home" if idx == 0 else f"[{idx}]"
            b_widget.setText(f"{prefix} {bcfg.label}")
            b_widget.setStyleSheet(
                f"background-color: #161b24; border: 2px solid rgb({r},{g},{b}); color: rgb({max(160,r)},{max(160,g)},{max(160,b)});"
            )

    def _install_systemd_service(self) -> None:
        service_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(service_dir, exist_ok=True)
        service_file = os.path.join(service_dir, "linuxdeck.service")
        repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        python_bin = os.path.join(repo_dir, ".venv", "bin", "python")
        if not os.path.exists(python_bin):
            python_bin = sys.executable

        unit_content = f"""[Unit]
Description=LinuxDeck — Open-Source Loupedeck & Razer Stream Controller Daemon
After=graphical-session.target pipewire.service

[Service]
Type=simple
WorkingDirectory={repo_dir}
ExecStart={python_bin} -m linuxdeck --tray
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""
        try:
            with open(service_file, "w", encoding="utf-8") as f:
                f.write(unit_content)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(["systemctl", "--user", "enable", "linuxdeck.service"], check=False)
            QMessageBox.information(
                self,
                "Autostart Service Installed",
                f"Installed and enabled user service:\n{service_file}\n\n"
                "LinuxDeck will automatically start in your system tray on login!",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Service Error", f"Failed to install systemd user service:\n{exc}")
