"""OBS Studio WebSocket v5 client with Flatpak and native auto-discovery."""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Optional

from websockets.sync.client import connect as ws_connect

logger = logging.getLogger(__name__)

OBS_CONFIG_PATHS = [
    os.path.expanduser("~/.var/app/com.obsproject.Studio/config/obs-studio/plugin_config/obs-websocket/config.json"),
    os.path.expanduser("~/.config/obs-studio/plugin_config/obs-websocket/config.json"),
]

OBS_SCENE_GLOBS = [
    os.path.expanduser("~/.var/app/com.obsproject.Studio/config/obs-studio/basic/scenes/*.json"),
    os.path.expanduser("~/.config/obs-studio/basic/scenes/*.json"),
]


class OBSController:
    """Thread-safe OBS WebSocket v5 client with automatic Flatpak/native config discovery."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.host = "127.0.0.1"
        self.port = 4455
        self.password = ""
        self.server_enabled_in_config = False

        self.connected = False
        self.current_scene = "Scene"
        self.scenes: list[str] = ["Scene"]
        self.recording = False
        self.record_timecode = ""
        self.streaming = False
        self.stream_timecode = ""

        self._ws: Any = None
        self._req_id = 1
        self._last_connect_attempt = 0.0

        self.load_obs_config()
        self.load_static_scenes()

    def load_obs_config(self) -> None:
        """Load port and password from OBS Studio's obs-websocket config.json."""
        for path in OBS_CONFIG_PATHS:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.port = int(data.get("server_port", 4455))
                    self.password = str(data.get("server_password", ""))
                    self.server_enabled_in_config = bool(data.get("server_enabled", False))
                    return
                except Exception as exc:
                    logger.debug("Failed to read OBS config %s: %s", path, exc)

    def ensure_websocket_enabled_in_config(self) -> bool:
        """Enable server_enabled=true in OBS Studio's obs-websocket config.json if present."""
        for path in OBS_CONFIG_PATHS:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not data.get("server_enabled", False):
                        data["server_enabled"] = True
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                    self.server_enabled_in_config = True
                    return True
                except Exception as exc:
                    logger.error("Failed to enable OBS websocket in %s: %s", path, exc)
        return False

    def load_static_scenes(self) -> None:
        """Read scenes from OBS's saved scene collection JSON when OBS is offline."""
        for pattern in OBS_SCENE_GLOBS:
            for path in sorted(glob.glob(pattern)):
                if path.endswith(".bak"):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    scenes = [
                        s["name"]
                        for s in data.get("sources", [])
                        if s.get("id") == "scene" and "name" in s
                    ]
                    if scenes:
                        self.scenes = scenes
                    cur = data.get("current_scene")
                    if cur:
                        self.current_scene = cur
                    return
                except Exception:
                    pass

    def _compute_auth(self, password: str, salt: str, challenge: str) -> str:
        secret_hash = hashlib.sha256((password + salt).encode("utf-8")).digest()
        secret = base64.b64encode(secret_hash).decode("ascii")
        auth_hash = hashlib.sha256((secret + challenge).encode("utf-8")).digest()
        return base64.b64encode(auth_hash).decode("ascii")

    def _ensure_connected(self) -> bool:
        if self.connected and self._ws is not None:
            return True

        now = time.monotonic()
        if (now - self._last_connect_attempt) < 3.0:
            return False
        self._last_connect_attempt = now

        self.load_obs_config()
        url = f"ws://{self.host}:{self.port}"
        try:
            ws = ws_connect(url, open_timeout=0.35, close_timeout=0.2)
            hello = json.loads(ws.recv(timeout=0.4))
            if hello.get("op") != 0:
                ws.close()
                return False

            identify_payload: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth_info = hello.get("d", {}).get("authentication")
            if auth_info and self.password:
                identify_payload["authentication"] = self._compute_auth(
                    self.password, auth_info["salt"], auth_info["challenge"]
                )

            ws.send(json.dumps({"op": 1, "d": identify_payload}))
            identified = json.loads(ws.recv(timeout=0.4))
            if identified.get("op") != 2:
                ws.close()
                return False

            self._ws = ws
            self.connected = True
            logger.info("Connected to OBS WebSocket v5 at %s", url)
            return True
        except Exception:
            self.connected = False
            self._ws = None
            return False

    def request(self, request_type: str, request_data: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """Send an OBS WebSocket v5 Request (OpCode 6) and wait for RequestResponse (OpCode 7)."""
        with self._lock:
            if not self._ensure_connected():
                return None

            req_id = str(self._req_id)
            self._req_id += 1
            msg: dict[str, Any] = {
                "op": 6,
                "d": {
                    "requestType": request_type,
                    "requestId": req_id,
                },
            }
            if request_data:
                msg["d"]["requestData"] = request_data

            try:
                self._ws.send(json.dumps(msg))
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    raw = self._ws.recv(timeout=0.35)
                    resp = json.loads(raw)
                    if resp.get("op") == 7 and resp.get("d", {}).get("requestId") == req_id:
                        return resp.get("d", {}).get("responseData", {})
            except Exception as exc:
                logger.debug("OBS request %s failed: %s", request_type, exc)
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None
                self.connected = False
        return None

    def poll_status(self) -> None:
        """Refresh current scene, recording status, and streaming status from OBS if running."""
        scene_resp = self.request("GetSceneList")
        if scene_resp is not None:
            cur = scene_resp.get("currentProgramSceneName")
            if cur:
                self.current_scene = cur
            raw_scenes = scene_resp.get("scenes", [])
            if raw_scenes:
                self.scenes = [s["sceneName"] for s in reversed(raw_scenes) if "sceneName" in s]

        rec_resp = self.request("GetRecordStatus")
        if rec_resp is not None:
            self.recording = bool(rec_resp.get("outputActive", False))
            tc = str(rec_resp.get("outputTimecode", ""))
            self.record_timecode = tc.split(".")[0] if tc else ""

        stream_resp = self.request("GetStreamStatus")
        if stream_resp is not None:
            self.streaming = bool(stream_resp.get("outputActive", False))
            tc = str(stream_resp.get("outputTimecode", ""))
            self.stream_timecode = tc.split(".")[0] if tc else ""

    def set_scene(self, scene_name: str) -> bool:
        """Switch OBS program scene."""
        self.current_scene = scene_name
        if not self.connected:
            return False
        self.request("SetCurrentProgramScene", {"sceneName": scene_name})
        return True

    def toggle_record(self) -> bool:
        """Toggle OBS recording on/off."""
        if not self.connected:
            return False
        resp = self.request("ToggleRecord")
        if resp is not None:
            self.recording = bool(resp.get("outputActive", not self.recording))
            return True
        return False

    def toggle_stream(self) -> bool:
        """Toggle OBS streaming on/off."""
        if not self.connected:
            return False
        resp = self.request("ToggleStream")
        if resp is not None:
            self.streaming = bool(resp.get("outputActive", not self.streaming))
            return True
        return False

    def launch_obs(self) -> None:
        """Ensure obs-websocket is enabled in config and launch OBS Studio (native or Flatpak)."""
        self.ensure_websocket_enabled_in_config()
        if shutil.which("obs"):
            cmd = ["obs"]
        else:
            cmd = ["flatpak", "run", "com.obsproject.Studio"]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
