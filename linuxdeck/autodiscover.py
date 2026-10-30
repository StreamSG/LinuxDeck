"""Automatic hardware, audio, sensor, OBS scene, and desktop application discovery for any Linux PC."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

import psutil

from .integrations.audio import PipeWireController
from .integrations.obs import OBSController

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredApp:
    title: str
    icon: str
    accent_color: str
    command: str
    audio_query: str = ""


@dataclass
class SystemProfileDiscovery:
    """Summary of hardware, audio devices, sensors, and apps discovered on the host PC."""

    cpu_title: str = "CPU Load"
    gpu_title: str = "GPU Load"
    has_psu_sensor: bool = False
    psu_title: str = "PSU Temp"
    has_nvme_sensor: bool = False
    has_battery: bool = False

    primary_sink_title: str = "Headset"
    primary_sink_dial: str = "HEADSET"
    primary_sink_query: str = "default_sink"

    primary_mic_title: str = "Mic"
    primary_mic_dial: str = "MIC GAIN"
    primary_mic_query: str = "default_source"

    secondary_mic_title: str = "Webcam Mic"
    secondary_mic_query: str = "default_source"

    has_easyeffects: bool = False
    easyeffects_cmd: str = ""

    output_sinks: list[tuple[str, str, str]] = field(default_factory=list)  # (short_title, query, color)
    obs_scenes: list[str] = field(default_factory=lambda: ["Main Scene", "Scene 2"])

    chat_app: DiscoveredApp = field(
        default_factory=lambda: DiscoveredApp("Discord", "discord", "#7289DA", "xdg-open https://discord.com/app", "app:chat")
    )
    browser_app: DiscoveredApp = field(
        default_factory=lambda: DiscoveredApp("Browser", "browser", "#FFAB40", "xdg-open https://", "app:browser")
    )
    game_app: DiscoveredApp = field(
        default_factory=lambda: DiscoveredApp("Steam", "steam", "#66C0F4", "steam", "app:game")
    )
    extra_app: DiscoveredApp = field(
        default_factory=lambda: DiscoveredApp("Files", "default", "#FF9100", "xdg-open ~")
    )
    terminal_cmd: str = "x-terminal-emulator"
    screenshot_cmd: str = "spectacle -r"
    lock_cmd: str = "loginctl lock-session"


def _shorten_audio_label(description: str, fallback: str) -> tuple[str, str, str]:
    """Extract a concise (tile_title, dial_label, match_query) from a PipeWire description."""
    d = description.strip()
    dl = d.lower()

    known_brands = [
        ("kraken v4", "Kraken V4", "HEADSET", "kraken"),
        ("kraken", "Kraken", "HEADSET", "kraken"),
        ("blackshark", "BlackShark", "HEADSET", "blackshark"),
        ("seiren v3", "Seiren V3", "SEIREN", "seiren"),
        ("seiren", "Seiren Mic", "SEIREN", "seiren"),
        ("brio", "BRIO Mic", "BRIO MIC", "brio"),
        ("yeti", "Blue Yeti", "YETI MIC", "yeti"),
        ("quadcast", "QuadCast", "QUADCAST", "quadcast"),
        ("wave", "Elgato Wave", "WAVE MIC", "wave"),
        ("arctis", "Arctis", "HEADSET", "arctis"),
        ("scarlett", "Scarlett", "INTERFACE", "scarlett"),
        ("focusrite", "Focusrite", "INTERFACE", "focusrite"),
        ("shure", "Shure Mic", "SHURE", "shure"),
        ("easy effects", "EasyFX", "EASYFX", "easyeffects"),
        ("hdmi", "HDMI Out", "HDMI", "hdmi"),
        ("iec958", "Digital Out", "OPTICAL", "iec958"),
        ("digital", "Digital Out", "DIGITAL", "iec958"),
    ]
    for pattern, tile_lbl, dial_lbl, query in known_brands:
        if pattern in dl:
            return tile_lbl, dial_lbl, query

    cleaned = re.sub(
        r"\b(Analog|Stereo|Digital|Controller|High Definition|Audio|USB|Generic|Razer|USA|Ltd|Inc)\b",
        "",
        d,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\([^)]*\)", "", cleaned)
    cleaned = " ".join(cleaned.split())
    if cleaned:
        words = cleaned.split()
        short_tile = " ".join(words[:2])[:11]
        short_dial = words[0][:8].upper()
        query = words[0].lower()
        return short_tile, short_dial, query

    return fallback, fallback[:8].upper(), fallback.lower()


def _get_installed_flatpaks() -> set[str]:
    if not shutil.which("flatpak"):
        return set()
    try:
        out = subprocess.check_output(
            ["flatpak", "list", "--app", "--columns=application"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
        return {line.strip() for line in out.splitlines() if line.strip()}
    except Exception:
        return set()


def _detect_cpu_title() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            content = f.read()
        if "Ryzen" in content:
            return "Ryzen CPU"
        if "Intel" in content:
            return "Intel CPU"
    except Exception:
        pass
    return "CPU Load"


def _detect_gpu_title() -> str:
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=0.8,
            ).strip()
            if out:
                first = out.splitlines()[0]
                m = re.search(r"((?:RTX|GTX)\s*\d{4}(?:\s*Ti)?)", first, re.IGNORECASE)
                if m:
                    return m.group(1).replace(" ", "") if len(m.group(1)) > 10 else m.group(1)
                return "NVIDIA GPU"
        except Exception:
            pass

    if shutil.which("lspci"):
        try:
            out = subprocess.check_output(["lspci"], text=True, stderr=subprocess.DEVNULL, timeout=0.8)
            for line in out.splitlines():
                if "VGA" in line or "3D controller" in line or "Display" in line:
                    m_rx = re.search(r"(RX\s*\d{4}\s*(?:XT|XTX)?)", line, re.IGNORECASE)
                    if m_rx:
                        return m_rx.group(1)[:11]
                    if "AMD" in line or "Radeon" in line:
                        return "Radeon GPU"
                    if "Arc" in line or "Intel" in line:
                        return "Intel GPU"
        except Exception:
            pass
    return "GPU Load"


def discover_system_environment() -> SystemProfileDiscovery:
    """Inspect the current Linux PC and return an optimal hardware/software discovery profile."""
    info = SystemProfileDiscovery()
    flatpaks = _get_installed_flatpaks()

    # 1. CPU, GPU, and Hardware Sensors
    info.cpu_title = _detect_cpu_title()
    info.gpu_title = _detect_gpu_title()

    try:
        temps = psutil.sensors_temperatures()
        info.has_nvme_sensor = "nvme" in temps or "drivetemp" in temps
        if "corsairpsu" in temps:
            info.has_psu_sensor = True
            info.psu_title = "Corsair PSU"
        elif any(k in temps for k in ("aquacomputer", "nct6775", "it87", "gigabyte_wmi", "asus")):
            info.has_psu_sensor = True
            info.psu_title = "Board Temp"
    except Exception:
        pass

    try:
        info.has_battery = psutil.sensors_battery() is not None
    except Exception:
        pass

    # 2. EasyEffects Detection
    if shutil.which("easyeffects"):
        info.has_easyeffects = True
        info.easyeffects_cmd = "easyeffects"
    elif "com.github.wwmm.easyeffects" in flatpaks:
        info.has_easyeffects = True
        info.easyeffects_cmd = "flatpak run com.github.wwmm.easyeffects"

    # 3. PipeWire Audio Sinks & Sources
    pw = PipeWireController()
    if pw.sinks:
        if "easyeffects_sink" in pw.sinks:
            info.has_easyeffects = True
            if not info.easyeffects_cmd:
                info.easyeffects_cmd = "flatpak run com.github.wwmm.easyeffects"

        # Pick primary hardware headset/speaker sink
        primary_sink = None
        for k, ep in pw.sinks.items():
            if "easyeffects" not in k.lower() and "hdmi" not in k.lower():
                primary_sink = ep
                break
        if primary_sink is None:
            primary_sink = next(iter(pw.sinks.values()))

        s_title, s_dial, s_query = _shorten_audio_label(primary_sink.description, "Headset")
        info.primary_sink_title = s_title
        info.primary_sink_dial = s_dial
        info.primary_sink_query = s_query

        # Discover up to 3 output sinks for output routing buttons
        colors = ["#00FF66", "#FFB300", "#00E5FF"]
        for k, ep in pw.sinks.items():
            lbl, _dl, qry = _shorten_audio_label(ep.description, "Speaker")
            if not any(existing[1] == qry for existing in info.output_sinks):
                col = colors[len(info.output_sinks) % len(colors)]
                info.output_sinks.append((f"Out: {lbl[:7]}", qry, col))
            if len(info.output_sinks) >= 3:
                break

    if pw.sources:
        # Prioritize dedicated USB studio mics over webcams over motherboard analog inputs
        real_sources = [ep for k, ep in pw.sources.items() if "easyeffects" not in k.lower()]
        real_sources.sort(
            key=lambda ep: (
                0
                if any(
                    m in ep.description.lower()
                    for m in ("seiren", "yeti", "quadcast", "wave", "shure", "rode", "scarlett", "focusrite", "usb")
                )
                and "webcam" not in ep.description.lower()
                and "brio" not in ep.description.lower()
                else 1
                if "usb" in ep.name.lower()
                else 2
            )
        )
        if real_sources:
            m_title, m_dial, m_query = _shorten_audio_label(real_sources[0].description, "Mic")
            info.primary_mic_title = m_title
            info.primary_mic_dial = m_dial
            info.primary_mic_query = m_query
        if len(real_sources) >= 2:
            m2_title, _m2_dial, m2_query = _shorten_audio_label(real_sources[1].description, "Mic 2")
            info.secondary_mic_title = m2_title
            info.secondary_mic_query = m2_query

    # 4. OBS Studio Scenes
    obs = OBSController()
    if obs.scenes:
        info.obs_scenes = obs.scenes[:4]
    if len(info.obs_scenes) < 2:
        info.obs_scenes = [info.obs_scenes[0] if info.obs_scenes else "Scene", "Scene 2"]

    # 5. Desktop Applications & Utilities
    # Chat app
    for cmd_bin, flatpak_id, title in [
        ("vesktop", "dev.vencord.Vesktop", "Vesktop"),
        ("discord", "com.discordapp.Discord", "Discord"),
        ("telegram-desktop", "org.telegram.desktop", "Telegram"),
        ("signal-desktop", "org.signal.Signal", "Signal"),
    ]:
        if shutil.which(cmd_bin):
            info.chat_app = DiscoveredApp(title, "discord", "#7289DA", cmd_bin, "app:chat")
            break
        if flatpak_id in flatpaks:
            info.chat_app = DiscoveredApp(title, "discord", "#7289DA", f"flatpak run {flatpak_id}", "app:chat")
            break

    # Browser
    for cmd_bin, flatpak_id, title in [
        ("google-chrome", "com.google.Chrome", "Chrome"),
        ("brave-browser", "com.brave.Browser", "Brave"),
        ("firefox", "org.mozilla.firefox", "Firefox"),
        ("chromium", "org.chromium.Chromium", "Chromium"),
    ]:
        if shutil.which(cmd_bin):
            info.browser_app = DiscoveredApp(title, "browser", "#FFAB40", cmd_bin, "app:browser")
            break
        if flatpak_id in flatpaks:
            info.browser_app = DiscoveredApp(title, "browser", "#FFAB40", f"flatpak run {flatpak_id}", "app:browser")
            break

    # Game Launcher
    for cmd_bin, flatpak_id, title in [
        ("steam", "com.valvesoftware.Steam", "Steam"),
        ("lutris", "net.lutris.Lutris", "Lutris"),
        ("heroic", "com.heroicgameslauncher.hgl", "Heroic"),
    ]:
        if shutil.which(cmd_bin):
            info.game_app = DiscoveredApp(title, "steam", "#66C0F4", cmd_bin, "app:game")
            break
        if flatpak_id in flatpaks:
            info.game_app = DiscoveredApp(title, "steam", "#66C0F4", f"flatpak run {flatpak_id}", "app:game")
            break

    # Extra app (Sunshine / Spotify / VLC / Files)
    for cmd_bin, flatpak_id, title, icon, color in [
        ("sunshine", "dev.lizardbyte.app.Sunshine", "Sunshine", "stream", "#FF9100"),
        ("spotify", "com.spotify.Client", "Spotify", "play_pause", "#1DB954"),
        ("vlc", "org.videolan.VLC", "VLC Media", "play", "#FF9100"),
    ]:
        if shutil.which(cmd_bin):
            info.extra_app = DiscoveredApp(title, icon, color, cmd_bin)
            break
        if flatpak_id in flatpaks:
            info.extra_app = DiscoveredApp(title, icon, color, f"flatpak run {flatpak_id}")
            break

    # Terminal emulator
    for term in (
        "konsole",
        "ptyxis",
        "gnome-terminal",
        "kgx",
        "kitty",
        "alacritty",
        "wezterm",
        "foot",
        "xfce4-terminal",
        "xterm",
    ):
        if shutil.which(term):
            info.terminal_cmd = term
            break

    # Screenshot tool
    for sc_bin, sc_cmd in (
        ("spectacle", "spectacle -r"),
        ("flameshot", "flameshot gui"),
        ("gnome-screenshot", "gnome-screenshot -i"),
        ("scrot", "scrot -s"),
    ):
        if shutil.which(sc_bin):
            info.screenshot_cmd = sc_cmd
            break

    return info
