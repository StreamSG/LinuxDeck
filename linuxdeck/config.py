"""Profile and configuration management for LinuxDeck with automatic host hardware/software discovery."""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from typing import Any

from .autodiscover import discover_system_environment

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.config/linuxdeck")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LEGACY_CONFIG_PATH = os.path.expanduser("~/.config/razer-stream-controller/config.json")


@dataclass
class KeyConfig:
    """Configuration for a single 90x90 touch key (0..11) on a page."""

    title: str = "Key"
    icon: str = "default"
    accent_color: str = "#00FF66"
    action_type: str = "none"
    param: str = ""


@dataclass
class DialConfig:
    """Configuration for one of the 6 rotary dials (0..5)."""

    label: str = "VOL"
    accent_color: str = "#00FF66"
    action_type: str = "audio"  # "audio", "brightness", "page_cycle"
    param: str = "default_sink"
    step: int = 4


@dataclass
class ButtonConfig:
    """Configuration for one of the 8 bottom RGB physical buttons (0..7)."""

    label: str = "Btn"
    color: str = "#00FF66"
    action_type: str = "page"  # "page", "audio_mute", "obs_record", "obs_stream", "media", "command"
    param: str = "0"


@dataclass
class PageConfig:
    """A page of 12 center touch keys (4x3 grid)."""

    name: str = "Main"
    accent_color: str = "#00FF66"
    keys: list[KeyConfig] = field(default_factory=list)


@dataclass
class ProfileConfig:
    """Complete controller profile saved to ~/.config/linuxdeck/config.json."""

    brightness: int = 9
    haptics_enabled: bool = True
    pages: list[PageConfig] = field(default_factory=list)
    dials: list[DialConfig] = field(default_factory=list)
    buttons: list[ButtonConfig] = field(default_factory=list)


def create_default_profile() -> ProfileConfig:
    """Auto-discover the host Linux PC's hardware, audio devices, sensors, and apps to build the best profile."""
    sys_info = discover_system_environment()

    scene1 = sys_info.obs_scenes[0] if sys_info.obs_scenes else "Scene"
    scene2 = sys_info.obs_scenes[1] if len(sys_info.obs_scenes) > 1 else "Scene 2"

    fx_tile = (
        KeyConfig("EasyEffects", "eq", "#FFB300", "command", sys_info.easyeffects_cmd)
        if sys_info.has_easyeffects
        else KeyConfig(sys_info.chat_app.title, sys_info.chat_app.icon, sys_info.chat_app.accent_color, "command", sys_info.chat_app.command)
    )

    page_stream = PageConfig(
        name="Stream & OBS",
        accent_color="#00FF66",
        keys=[
            KeyConfig(scene1[:12], "scene", "#00FF66", "obs_scene", scene1),
            KeyConfig(scene2[:12], "scene", "#00E5FF", "obs_scene", scene2),
            KeyConfig("Record", "record", "#FF3355", "obs_record", ""),
            KeyConfig("Stream", "stream", "#B388FF", "obs_stream", ""),
            KeyConfig(sys_info.primary_sink_title, "headset", "#00FF66", "audio_mute", "default_sink"),
            KeyConfig(sys_info.primary_mic_title, "mic", "#00E5FF", "audio_mute", sys_info.primary_mic_query),
            fx_tile,
            KeyConfig("Launch OBS", "obs", "#00FF66", "obs_launch", ""),
            KeyConfig(sys_info.cpu_title, "cpu", "#00FF66", "telemetry", "cpu"),
            KeyConfig(sys_info.gpu_title, "gpu", "#76B900", "telemetry", "gpu"),
            KeyConfig("Play/Pause", "play_pause", "#1DB954", "media", "PlayPause"),
            KeyConfig("Next Track", "next", "#00E5FF", "media", "Next"),
        ],
    )

    # Output routing keys for Page 2 (Audio Mixer)
    out_keys: list[KeyConfig] = []
    for out_lbl, out_qry, out_col in sys_info.output_sinks[:3]:
        out_keys.append(
            KeyConfig(
                out_lbl,
                "eq" if "easyfx" in out_lbl.lower() else "speaker",
                out_col,
                "audio_default_sink",
                out_qry,
            )
        )
    while len(out_keys) < 3:
        out_keys.append(KeyConfig("Default Out", "speaker", "#00FF66", "audio_default_sink", "default_sink"))

    page_audio = PageConfig(
        name="Audio Mixer",
        accent_color="#00E5FF",
        keys=[
            KeyConfig(sys_info.primary_sink_title, "headset", "#00FF66", "audio_mute", sys_info.primary_sink_query),
            KeyConfig(sys_info.primary_mic_title, "mic", "#00E5FF", "audio_mute", sys_info.primary_mic_query),
            KeyConfig(sys_info.secondary_mic_title, "mic", "#40C4FF", "audio_mute", sys_info.secondary_mic_query),
            KeyConfig(
                "EasyFX Sink" if sys_info.has_easyeffects else "Master Out",
                "eq" if sys_info.has_easyeffects else "speaker",
                "#FFB300",
                "audio_mute",
                "easyeffects_sink" if sys_info.has_easyeffects else "default_sink",
            ),
            KeyConfig(sys_info.chat_app.title, "discord", "#7289DA", "audio_mute", "app:chat"),
            KeyConfig(sys_info.browser_app.title, "browser", "#FFAB40", "audio_mute", "app:browser"),
            KeyConfig(f"{sys_info.game_app.title} Audio", "steam", "#66C0F4", "audio_mute", "app:game"),
            KeyConfig(
                "EasyFX Mic" if sys_info.has_easyeffects else "Music Audio",
                "eq" if sys_info.has_easyeffects else "play_pause",
                "#B388FF",
                "audio_mute",
                "easyeffects_source" if sys_info.has_easyeffects else "app:music",
            ),
            out_keys[0],
            out_keys[1],
            out_keys[2],
            fx_tile,
        ],
    )

    extra_sensor_tile = (
        KeyConfig(sys_info.psu_title, "psu", "#B388FF", "telemetry", "psu_temp")
        if sys_info.has_psu_sensor
        else KeyConfig("Battery", "temp", "#00FF66", "telemetry", "battery")
        if sys_info.has_battery
        else KeyConfig("CPU Temp 2", "temp", "#B388FF", "telemetry", "cpu_temp")
    )

    page_telemetry = PageConfig(
        name="System Stats",
        accent_color="#B388FF",
        keys=[
            KeyConfig(sys_info.cpu_title, "cpu", "#00FF66", "telemetry", "cpu"),
            KeyConfig("CPU Temp", "temp", "#FF6E40", "telemetry", "cpu_temp"),
            KeyConfig(sys_info.gpu_title, "gpu", "#76B900", "telemetry", "gpu"),
            KeyConfig("GPU VRAM", "ram", "#76B900", "telemetry", "vram"),
            KeyConfig("System RAM", "ram", "#00E5FF", "telemetry", "ram"),
            KeyConfig("Drive Temp", "temp", "#FFB300", "telemetry", "nvme_temp"),
            extra_sensor_tile,
            KeyConfig("Net Down", "net", "#00E5FF", "telemetry", "net_down"),
            KeyConfig("Net Up", "net", "#B388FF", "telemetry", "net_up"),
            KeyConfig("Clock", "clock", "#00FF66", "telemetry", "clock"),
            KeyConfig("Dim Deck", "brightness", "#40C4FF", "brightness_delta", "-2"),
            KeyConfig("Bright Deck", "brightness", "#FFD740", "brightness_delta", "+2"),
        ],
    )

    page_macros = PageConfig(
        name="Media & Apps",
        accent_color="#FFB300",
        keys=[
            KeyConfig("Prev Track", "prev", "#00E5FF", "media", "Previous"),
            KeyConfig("Play/Pause", "play_pause", "#1DB954", "media", "PlayPause"),
            KeyConfig("Next Track", "next", "#00E5FF", "media", "Next"),
            KeyConfig("Stop Media", "play", "#FF5252", "media", "Stop"),
            KeyConfig("OBS Studio", "obs", "#00FF66", "obs_launch", ""),
            KeyConfig(sys_info.chat_app.title, sys_info.chat_app.icon, sys_info.chat_app.accent_color, "command", sys_info.chat_app.command),
            KeyConfig(sys_info.game_app.title, sys_info.game_app.icon, sys_info.game_app.accent_color, "command", sys_info.game_app.command),
            fx_tile,
            KeyConfig(sys_info.extra_app.title, sys_info.extra_app.icon, sys_info.extra_app.accent_color, "command", sys_info.extra_app.command),
            KeyConfig("Terminal", "terminal", "#00FF66", "command", sys_info.terminal_cmd),
            KeyConfig("Screenshot", "camera", "#B388FF", "command", sys_info.screenshot_cmd),
            KeyConfig("Lock PC", "lock", "#FF5252", "command", sys_info.lock_cmd),
        ],
    )

    dial_2 = (
        DialConfig("EASYFX", "#FFB300", "audio", "easyeffects_sink", 4)
        if sys_info.has_easyeffects
        else DialConfig("GAMES", "#66C0F4", "audio", "app:game", 4)
    )

    dials = [
        DialConfig(sys_info.primary_sink_dial, "#00FF66", "audio", "default_sink", 4),
        DialConfig(sys_info.primary_mic_dial, "#00E5FF", "audio", sys_info.primary_mic_query, 4),
        dial_2,
        DialConfig(sys_info.chat_app.title[:8].upper(), "#7289DA", "audio", "app:chat", 4),
        DialConfig(sys_info.browser_app.title[:8].upper(), "#FFAB40", "audio", "app:browser", 4),
        DialConfig("BRIGHT", "#B388FF", "brightness", "", 1),
    ]

    buttons = [
        ButtonConfig("Home", "#00FF00", "page", "0"),
        ButtonConfig("Page 1: OBS", "#00FF66", "page", "0"),
        ButtonConfig("Page 2: Audio", "#00E5FF", "page", "1"),
        ButtonConfig("Page 3: Stats", "#B388FF", "page", "2"),
        ButtonConfig("Page 4: Apps", "#FFB300", "page", "3"),
        ButtonConfig("Mic Mute", "#00E5FF", "audio_mute", sys_info.primary_mic_query),
        ButtonConfig("Audio Mute", "#00FF66", "audio_mute", "default_sink"),
        ButtonConfig("OBS Record", "#FF3355", "obs_record", ""),
    ]

    return ProfileConfig(
        brightness=9,
        haptics_enabled=True,
        pages=[page_stream, page_audio, page_telemetry, page_macros],
        dials=dials,
        buttons=buttons,
    )


def load_profile(path: str = CONFIG_PATH, force_autoconfig: bool = False) -> ProfileConfig:
    """Load ProfileConfig from JSON, migrating legacy config or running auto-discovery on first launch."""
    if force_autoconfig:
        prof = create_default_profile()
        save_profile(prof, path)
        return prof

    if not os.path.exists(path) and os.path.exists(LEGACY_CONFIG_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy2(LEGACY_CONFIG_PATH, path)

    if not os.path.exists(path):
        prof = create_default_profile()
        save_profile(prof, path)
        return prof

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
        pages = []
        for p in raw.get("pages", []):
            keys = [KeyConfig(**k) for k in p.get("keys", [])]
            while len(keys) < 12:
                keys.append(KeyConfig())
            pages.append(
                PageConfig(
                    name=p.get("name", "Page"),
                    accent_color=p.get("accent_color", "#00FF66"),
                    keys=keys[:12],
                )
            )
        dials = [DialConfig(**d) for d in raw.get("dials", [])]
        buttons = [ButtonConfig(**b) for b in raw.get("buttons", [])]
        default_prof = create_default_profile()
        if not pages:
            pages = default_prof.pages
        if len(dials) < 6:
            dials = default_prof.dials
        if len(buttons) < 8:
            buttons = default_prof.buttons
        return ProfileConfig(
            brightness=int(raw.get("brightness", 9)),
            haptics_enabled=bool(raw.get("haptics_enabled", True)),
            pages=pages,
            dials=dials[:6],
            buttons=buttons[:8],
        )
    except Exception as exc:
        logger.error("Failed to load config %s (%s), generating auto-discovered profile.", path, exc)
        return create_default_profile()


def save_profile(profile: ProfileConfig, path: str = CONFIG_PATH) -> None:
    """Persist ProfileConfig to JSON."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(profile), f, indent=2)
