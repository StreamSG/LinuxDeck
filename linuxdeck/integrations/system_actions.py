"""Cross-desktop MPRIS2 media controls (playerctl/qdbus6/qdbus/gdbus) and async command runner."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)


class MediaAndSystemActions:
    """Executes MPRIS2 media commands across any Linux desktop and launches apps/scripts."""

    def __init__(self) -> None:
        self.playing = False
        self.track_title = "Media"
        self._cached_service: Optional[str] = None
        self._has_playerctl = shutil.which("playerctl") is not None
        self._qdbus_bin = (
            shutil.which("qdbus6")
            or shutil.which("qdbus-qt6")
            or shutil.which("qdbus")
            or ""
        )
        self._has_gdbus = shutil.which("gdbus") is not None
        self._last_poll = 0.0

    def _find_mpris_service(self) -> Optional[str]:
        if self._qdbus_bin:
            try:
                out = subprocess.check_output(
                    [self._qdbus_bin], text=True, stderr=subprocess.DEVNULL, timeout=0.6
                )
                services = [
                    s.strip()
                    for s in out.splitlines()
                    if "org.mpris.MediaPlayer2." in s
                ]
                for s in services:
                    if "kdeconnect" not in s:
                        self._cached_service = s
                        return s
                self._cached_service = services[0] if services else None
                return self._cached_service
            except Exception:
                return self._cached_service
        return self._cached_service

    def poll_media(self, force: bool = False) -> tuple[bool, str]:
        now = time.monotonic()
        if not force and (now - self._last_poll) < 1.5:
            return self.playing, self.track_title
        self._last_poll = now

        if self._has_playerctl:
            try:
                status = subprocess.check_output(
                    ["playerctl", "status"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=0.5,
                ).strip()
                self.playing = status.lower() == "playing"
                self.track_title = "Playing" if self.playing else status
                return self.playing, self.track_title
            except Exception:
                self.playing = False
                return self.playing, self.track_title

        svc = self._find_mpris_service()
        if not svc:
            self.playing = False
            self.track_title = "Idle"
            return self.playing, self.track_title

        if self._qdbus_bin:
            try:
                status = subprocess.check_output(
                    [
                        self._qdbus_bin,
                        svc,
                        "/org/mpris/MediaPlayer2",
                        "org.mpris.MediaPlayer2.Player.PlaybackStatus",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=0.5,
                ).strip()
                self.playing = status.lower() == "playing"
                self.track_title = "Playing" if self.playing else status
            except Exception:
                self.playing = False

        return self.playing, self.track_title

    def media_command(self, method: str) -> bool:
        """Invoke an MPRIS2 Player method asynchronously: PlayPause, Next, Previous, Stop."""
        if method == "PlayPause":
            self.playing = not self.playing

        if self._has_playerctl:
            pctl_map = {
                "PlayPause": "play-pause",
                "Next": "next",
                "Previous": "previous",
                "Stop": "stop",
            }
            sub = pctl_map.get(method, "play-pause")
            try:
                subprocess.Popen(
                    ["playerctl", sub],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                pass

        svc = self._cached_service or self._find_mpris_service()
        if svc and self._qdbus_bin:
            try:
                subprocess.Popen(
                    [
                        self._qdbus_bin,
                        svc,
                        "/org/mpris/MediaPlayer2",
                        f"org.mpris.MediaPlayer2.Player.{method}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception as exc:
                logger.error("MPRIS command %s failed: %s", method, exc)
                return False

        if self._has_gdbus and svc:
            try:
                subprocess.Popen(
                    [
                        "gdbus",
                        "call",
                        "--session",
                        "--dest",
                        svc,
                        "--object-path",
                        "/org/mpris/MediaPlayer2",
                        "--method",
                        f"org.mpris.MediaPlayer2.Player.{method}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                pass
        return False

    @staticmethod
    def run_command_async(command_str: str) -> None:
        """Spawn a background shell command or desktop app."""
        if not command_str.strip():
            return
        try:
            subprocess.Popen(
                command_str,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            logger.error("Failed to launch command %r: %s", command_str, exc)
