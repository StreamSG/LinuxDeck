"""PipeWire / PulseAudio controller with smart app category matching and virtual volume memory."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Category aliases so profiles work across any chat/browser/game/music app
APP_CATEGORIES: dict[str, tuple[str, ...]] = {
    "chat": ("vesktop", "discord", "webrtc", "teamspeak", "mumble", "telegram", "signal", "element"),
    "discord": ("vesktop", "discord", "webrtc"),
    "vesktop": ("vesktop", "discord", "webrtc"),
    "browser": ("chrome", "chromium", "firefox", "brave", "vivaldi", "zen", "opera", "edge", "epiphany"),
    "chrome": ("chrome", "chromium", "brave", "firefox"),
    "game": ("steam", "wine", "proton", "gamescope", "lutris", "heroic", "fmod", "sdl", "openal"),
    "steam": ("steam", "wine", "proton", "gamescope"),
    "music": ("spotify", "elisa", "rhythmbox", "amberol", "vlc", "mpv", "cider", "audacious"),
}


@dataclass
class AudioEndpoint:
    """State of a PipeWire sink, source, or application stream."""

    id: str  # pactl name or sink-input index ("" if virtual/idle app)
    kind: str  # "sink", "source", "app"
    name: str
    description: str
    volume: float  # 0.0 .. 1.0
    muted: bool = False


class PipeWireController:
    """Zero-latency cached controller for PipeWire / PulseAudio sinks, sources, and app streams."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sinks: dict[str, AudioEndpoint] = {}
        self.sources: dict[str, AudioEndpoint] = {}
        self.app_streams: dict[str, AudioEndpoint] = {}
        self.default_sink: str = ""
        self.default_source: str = ""
        # Virtual state for app targets when the app is not currently playing audio
        self._virtual_app_state: dict[str, tuple[float, bool]] = {}
        self._last_poll = 0.0
        self._last_user_action_time = 0.0
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        """Poll pactl in the background thread. Never called on the UI/button press hot path."""
        now = time.monotonic()
        if (now - self._last_user_action_time) < 0.8:
            return
        if not force and (now - self._last_poll) < 1.5:
            return
        self._last_poll = now

        try:
            info_out = subprocess.check_output(
                ["pactl", "info"], text=True, stderr=subprocess.DEVNULL, timeout=0.8
            )
            def_sink = ""
            def_source = ""
            for line in info_out.splitlines():
                if line.startswith("Default Sink:"):
                    def_sink = line.split(":", 1)[1].strip()
                elif line.startswith("Default Source:"):
                    def_source = line.split(":", 1)[1].strip()

            sinks = self._parse_pactl_blocks("sinks", "sink")
            sources = {
                k: v
                for k, v in self._parse_pactl_blocks("sources", "source").items()
                if not k.endswith(".monitor")
            }
            apps = self._parse_sink_inputs()

            with self._lock:
                if (time.monotonic() - self._last_user_action_time) >= 0.8:
                    self.default_sink = def_sink
                    self.default_source = def_source
                    self.sinks = sinks
                    self.sources = sources
                    self.app_streams = apps
        except Exception as exc:
            logger.debug("PipeWire refresh error: %s", exc)

    def _parse_pactl_blocks(self, list_type: str, kind: str) -> dict[str, AudioEndpoint]:
        out = subprocess.check_output(
            ["pactl", "list", list_type], text=True, stderr=subprocess.DEVNULL, timeout=1.0
        )
        result: dict[str, AudioEndpoint] = {}
        current_name = ""
        current_desc = ""
        current_vol = 0.5
        current_mute = False

        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Name:"):
                if current_name:
                    result[current_name] = AudioEndpoint(
                        id=current_name,
                        kind=kind,
                        name=current_name,
                        description=current_desc or current_name,
                        volume=current_vol,
                        muted=current_mute,
                    )
                current_name = s.split(":", 1)[1].strip()
                current_desc = current_name
            elif s.startswith("Description:"):
                current_desc = s.split(":", 1)[1].strip()
            elif s.startswith("Mute:"):
                current_mute = "yes" in s.lower()
            elif s.startswith("Volume:") and "balance" not in s.lower():
                m = re.search(r"(\d+)%", s)
                if m:
                    current_vol = int(m.group(1)) / 100.0

        if current_name:
            result[current_name] = AudioEndpoint(
                id=current_name,
                kind=kind,
                name=current_name,
                description=current_desc or current_name,
                volume=current_vol,
                muted=current_mute,
            )
        return result

    def _parse_sink_inputs(self) -> dict[str, AudioEndpoint]:
        out = subprocess.check_output(
            ["pactl", "list", "sink-inputs"], text=True, stderr=subprocess.DEVNULL, timeout=1.0
        )
        result: dict[str, AudioEndpoint] = {}
        cur_idx = ""
        cur_app = ""
        cur_vol = 1.0
        cur_mute = False

        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Sink Input #"):
                if cur_idx and cur_app:
                    key = cur_app.lower()
                    result[key] = AudioEndpoint(
                        id=cur_idx,
                        kind="app",
                        name=cur_app,
                        description=cur_app,
                        volume=cur_vol,
                        muted=cur_mute,
                    )
                cur_idx = s.split("#", 1)[1].strip()
                cur_app = ""
                cur_vol = 1.0
                cur_mute = False
            elif s.startswith("Mute:"):
                cur_mute = "yes" in s.lower()
            elif s.startswith("Volume:") and "balance" not in s.lower():
                m = re.search(r"(\d+)%", s)
                if m:
                    cur_vol = int(m.group(1)) / 100.0
            elif s.startswith("application.name =") or s.startswith("node.name ="):
                val = s.split("=", 1)[1].strip().strip('"')
                if val and not cur_app:
                    cur_app = val
            elif s.startswith("application.process.binary ="):
                val = s.split("=", 1)[1].strip().strip('"')
                if val and not cur_app:
                    cur_app = val

        if cur_idx and cur_app:
            key = cur_app.lower()
            result[key] = AudioEndpoint(
                id=cur_idx,
                kind="app",
                name=cur_app,
                description=cur_app,
                volume=cur_vol,
                muted=cur_mute,
            )
        return result

    def resolve_target(self, target: str) -> Optional[AudioEndpoint]:
        """Instant in-memory lookup of a sink, source, or app stream."""
        t = target.strip().lower()
        with self._lock:
            if t in ("default_sink", "@default_audio_sink@", "master", "headset"):
                if self.default_sink in self.sinks:
                    return self.sinks[self.default_sink]
                # Prefer non-HDMI/non-virtual hardware headset/analog sink first
                for k, v in self.sinks.items():
                    if "hdmi" not in k.lower() and "easyeffects" not in k.lower():
                        return v
                return next(iter(self.sinks.values()), None)

            if t in ("default_source", "@default_audio_source@", "mic"):
                if self.default_source in self.sources:
                    return self.sources[self.default_source]
                for k, v in self.sources.items():
                    if "easyeffects" not in k.lower():
                        return v
                return next(iter(self.sources.values()), None)

            if t.startswith("app:"):
                app_query = t.split(":", 1)[1].strip()
                candidates = APP_CATEGORIES.get(app_query, (app_query,))
                for cand in candidates:
                    for k, v in self.app_streams.items():
                        if cand in k or cand in v.name.lower():
                            self._virtual_app_state[app_query] = (v.volume, v.muted)
                            return v
                v_vol, v_mute = self._virtual_app_state.get(app_query, (0.8, False))
                return AudioEndpoint(
                    id="",
                    kind="app",
                    name=app_query.capitalize(),
                    description=f"{app_query.capitalize()}",
                    volume=v_vol,
                    muted=v_mute,
                )

            for k, v in self.sinks.items():
                if t in k.lower() or t in v.description.lower():
                    return v
            for k, v in self.sources.items():
                if t in k.lower() or t in v.description.lower():
                    return v
            for k, v in self.app_streams.items():
                if t in k.lower() or t in v.name.lower():
                    return v

        return None

    def adjust_volume(self, target: str, delta_pct: int) -> Optional[AudioEndpoint]:
        """Optimistically update volume in memory and fire pactl asynchronously."""
        self._last_user_action_time = time.monotonic()
        ep = self.resolve_target(target)
        if ep is None:
            return None

        new_vol = max(0.0, min(1.0, ep.volume + (delta_pct / 100.0)))
        ep.volume = new_vol

        if target.strip().lower().startswith("app:"):
            app_key = target.strip().lower().split(":", 1)[1].strip()
            self._virtual_app_state[app_key] = (ep.volume, ep.muted)

        if not ep.id:
            return ep

        pct_str = f"{int(round(new_vol * 100))}%"
        cmd = (
            ["pactl", "set-sink-volume", ep.id, pct_str]
            if ep.kind == "sink"
            else ["pactl", "set-source-volume", ep.id, pct_str]
            if ep.kind == "source"
            else ["pactl", "set-sink-input-volume", ep.id, pct_str]
        )
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            logger.error("Failed to adjust volume for %s: %s", target, exc)

        return ep

    def toggle_mute(self, target: str) -> Optional[AudioEndpoint]:
        """Optimistically toggle mute in memory and fire pactl asynchronously."""
        self._last_user_action_time = time.monotonic()
        ep = self.resolve_target(target)
        if ep is None:
            return None

        ep.muted = not ep.muted
        if target.strip().lower().startswith("app:"):
            app_key = target.strip().lower().split(":", 1)[1].strip()
            self._virtual_app_state[app_key] = (ep.volume, ep.muted)

        if not ep.id:
            return ep

        mute_arg = "1" if ep.muted else "0"
        cmd = (
            ["pactl", "set-sink-mute", ep.id, mute_arg]
            if ep.kind == "sink"
            else ["pactl", "set-source-mute", ep.id, mute_arg]
            if ep.kind == "source"
            else ["pactl", "set-sink-input-mute", ep.id, mute_arg]
        )
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            logger.error("Failed to toggle mute for %s: %s", target, exc)

        return ep

    def set_default_sink(self, query: str) -> Optional[AudioEndpoint]:
        """Optimistically set the default PipeWire audio output sink."""
        self._last_user_action_time = time.monotonic()
        ep = self.resolve_target(query)
        if ep and ep.kind == "sink":
            self.default_sink = ep.id
            try:
                subprocess.Popen(
                    ["pactl", "set-default-sink", ep.id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.error("Failed to set default sink %s: %s", ep.id, exc)
        return ep
