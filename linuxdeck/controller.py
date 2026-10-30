"""Main controller daemon orchestrating hardware events, coalesced dirty-tile rendering, and Linux integrations."""

from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Callable, Optional

from PIL import Image

from .config import (
    ButtonConfig,
    DialConfig,
    KeyConfig,
    ProfileConfig,
    create_default_profile,
    load_profile,
    save_profile,
)
from .driver import HAPTIC_MEDIUM, HAPTIC_SHORT, LoupedeckDriver, TouchEvent
from .integrations.audio import PipeWireController
from .integrations.obs import OBSController
from .integrations.system_actions import MediaAndSystemActions
from .integrations.telemetry import TelemetryCollector
from .renderer import blend_rgb, hex_to_rgb, render_key_tile, render_strip_slot

logger = logging.getLogger(__name__)


class StreamControllerEngine:
    """Ultra-low-latency state machine and daemon loop for LinuxDeck."""

    def __init__(self, profile: Optional[ProfileConfig] = None, force_autoconfig: bool = False) -> None:
        self.profile = profile or load_profile(force_autoconfig=force_autoconfig)
        self.driver = LoupedeckDriver()
        self.audio = PipeWireController()
        self.obs = OBSController()
        self.telemetry = TelemetryCollector()
        self.media = MediaAndSystemActions()

        self.obs.ensure_websocket_enabled_in_config()

        self.current_page_index = 0
        self.screen_sleeping = False
        self._pressed_keys: set[int] = set()

        # Cached rendered images for GUI preview and dirty-tile diffing
        self.last_screen_image: Image.Image = Image.new("RGB", (480, 270), (8, 10, 14))
        self.last_key_images: list[Image.Image] = [
            Image.new("RGB", (90, 90), (8, 10, 14)) for _ in range(12)
        ]
        self.last_strip_images: list[Image.Image] = [
            Image.new("RGB", (60, 90), (8, 10, 14)) for _ in range(6)
        ]
        self.last_button_leds: list[tuple[int, int, int]] = [(0, 255, 100)] * 8

        # Sent hardware state for dirty-tile & dirty-LED diffing
        self._sent_key_bytes: list[bytes] = [b""] * 12
        self._sent_strip_bytes: list[bytes] = [b""] * 6
        self._sent_button_leds: list[Optional[tuple[int, int, int]]] = [None] * 8

        self._running = False
        self._loop_thread: Optional[threading.Thread] = None
        self._render_thread: Optional[threading.Thread] = None
        self._render_lock = threading.Lock()
        self._render_event = threading.Event()
        self._force_full_render = False

        # GUI / listener callbacks
        self.on_screen_updated: Optional[Callable[[Image.Image], None]] = None
        self.on_event_log: Optional[Callable[[str], None]] = None
        self.on_page_changed: Optional[Callable[[int], None]] = None
        self.on_status_changed: Optional[Callable[[bool, str, str], None]] = None

        # Wire hardware callbacks
        self.driver.on_button = self._on_hw_button
        self.driver.on_dial_press = self._on_hw_dial_press
        self.driver.on_dial_rotate = self._on_hw_dial_rotate
        self.driver.on_touch = self._on_hw_touch
        self.driver.on_swipe = self._on_hw_swipe
        self.driver.on_connection_changed = self._on_hw_connection_changed

    def log_event(self, message: str) -> None:
        logger.info(message)
        if self.on_event_log:
            try:
                self.on_event_log(message)
            except Exception:
                pass

    def auto_configure_for_system(self) -> ProfileConfig:
        """Re-run hardware and software auto-discovery for this PC and apply the generated profile."""
        self.profile = create_default_profile()
        self.save_config()
        self._reset_dirty_cache()
        self.update_button_leds(force=True)
        self.request_render(force_full=True)
        self.log_event("Auto-configured profile for this PC's hardware, audio devices, and apps.")
        return self.profile

    def request_render(self, force_full: bool = False) -> None:
        """Signal the dedicated render thread to push dirty tiles without blocking the caller."""
        if force_full:
            self._force_full_render = True
        self._render_event.set()

    def start(self) -> None:
        """Start the controller daemon, render worker, and connect to hardware."""
        if self._running:
            return
        self._running = True

        self._render_thread = threading.Thread(
            target=self._render_worker_loop, name="LinuxDeckRenderer", daemon=True
        )
        self._render_thread.start()

        if self.driver.connect():
            self._initialize_hardware()
        else:
            self.render_all(push_to_hardware=False)

        self._loop_thread = threading.Thread(
            target=self._daemon_loop, name="LinuxDeckLoop", daemon=True
        )
        self._loop_thread.start()

    def stop(self) -> None:
        """Stop the controller daemon and disconnect cleanly."""
        self._running = False
        self._render_event.set()
        if self._render_thread and self._render_thread.is_alive():
            if threading.current_thread() is not self._render_thread:
                self._render_thread.join(timeout=0.5)
        if self._loop_thread and self._loop_thread.is_alive():
            if threading.current_thread() is not self._loop_thread:
                self._loop_thread.join(timeout=0.5)
        self.driver.disconnect()

    def save_config(self) -> None:
        save_profile(self.profile)

    def _reset_dirty_cache(self) -> None:
        self._sent_key_bytes = [b""] * 12
        self._sent_strip_bytes = [b""] * 6
        self._sent_button_leds = [None] * 8

    def _initialize_hardware(self) -> None:
        self._reset_dirty_cache()
        self.driver.set_brightness(self.profile.brightness)
        self.update_button_leds(force=True)
        self.request_render(force_full=True)
        if self.on_status_changed:
            try:
                self.on_status_changed(
                    self.driver.connected,
                    self.driver.serial_number,
                    self.driver.firmware_version,
                )
            except Exception:
                pass

    def _on_hw_connection_changed(self, connected: bool) -> None:
        self.log_event(f"Hardware {'connected' if connected else 'disconnected'}")
        if self.on_status_changed:
            try:
                self.on_status_changed(
                    connected,
                    self.driver.serial_number,
                    self.driver.firmware_version,
                )
            except Exception:
                pass

    def _render_worker_loop(self) -> None:
        """Dedicated coalescing render worker so input handlers never block on serial pixel writes."""
        while self._running:
            self._render_event.wait()
            if not self._running:
                break
            self._render_event.clear()
            force = self._force_full_render
            self._force_full_render = False
            try:
                self.render_all(push_to_hardware=True, force_full=force)
            except Exception as exc:
                logger.debug("Render worker error: %s", exc)

    def _daemon_loop(self) -> None:
        last_poll = 0.0
        while self._running:
            now = time.monotonic()

            if not self.driver.connected:
                if (now - last_poll) >= 2.0:
                    last_poll = now
                    if self.driver.connect():
                        self._initialize_hardware()
                time.sleep(0.25)
                continue

            if (now - last_poll) >= 1.0:
                last_poll = now
                try:
                    self.audio.refresh(force=False)
                    self.telemetry.poll(force=True)
                    self.media.poll_media(force=False)
                    self.obs.poll_status()
                    if not self.screen_sleeping:
                        self.request_render(force_full=False)
                    self.update_button_leds(force=False)
                    if self.on_status_changed:
                        self.on_status_changed(
                            self.driver.connected,
                            self.driver.serial_number,
                            self.driver.firmware_version,
                        )
                except Exception as exc:
                    logger.debug("Periodic update error: %s", exc)

            time.sleep(0.1)

    # -------------------------------------------------------------------------
    # Hardware Event Handlers (Zero-Blocking Fast Path)
    # -------------------------------------------------------------------------

    def _on_hw_button(self, btn_idx: int, pressed: bool) -> None:
        if not pressed:
            return
        if self.profile.haptics_enabled:
            self.driver.vibrate(HAPTIC_SHORT)

        if self.screen_sleeping:
            self.screen_sleeping = False
            self.driver.set_brightness(self.profile.brightness)

        if not (0 <= btn_idx < len(self.profile.buttons)):
            return
        bcfg = self.profile.buttons[btn_idx]
        self.log_event(f"Button {btn_idx} ({bcfg.label}) pressed -> {bcfg.action_type}:{bcfg.param}")
        self._execute_button_or_key_action(bcfg.action_type, bcfg.param)
        self.update_button_leds()
        self.request_render()

    def _on_hw_dial_press(self, dial_idx: int, pressed: bool) -> None:
        if not pressed:
            return
        if self.profile.haptics_enabled:
            self.driver.vibrate(HAPTIC_SHORT)

        if not (0 <= dial_idx < len(self.profile.dials)):
            return
        dcfg = self.profile.dials[dial_idx]
        self.log_event(f"Dial {dial_idx} ({dcfg.label}) pressed -> {dcfg.action_type}:{dcfg.param}")

        if dcfg.action_type == "audio":
            self.audio.toggle_mute(dcfg.param)
            self.update_button_leds()
            self.request_render()
        elif dcfg.action_type == "brightness":
            self.screen_sleeping = not self.screen_sleeping
            self.driver.set_brightness(0 if self.screen_sleeping else self.profile.brightness)
            self.request_render()
        elif dcfg.action_type == "page_cycle":
            self.set_page((self.current_page_index + 1) % len(self.profile.pages))

    def _on_hw_dial_rotate(self, dial_idx: int, delta: int) -> None:
        if not (0 <= dial_idx < len(self.profile.dials)):
            return
        dcfg = self.profile.dials[dial_idx]
        step = dcfg.step * delta

        if dcfg.action_type == "audio":
            ep = self.audio.adjust_volume(dcfg.param, step)
            vol_str = f"{int(round(ep.volume * 100))}%" if ep else "?"
            self.log_event(f"Dial {dial_idx} ({dcfg.label}) rotated {delta:+d} -> {vol_str}")
            self.request_render()
        elif dcfg.action_type == "brightness":
            self.screen_sleeping = False
            self.profile.brightness = max(1, min(10, self.profile.brightness + delta))
            self.driver.set_brightness(self.profile.brightness)
            self.log_event(f"Brightness set to {self.profile.brightness}/10")
            self.request_render()
        elif dcfg.action_type == "page_cycle":
            new_page = (self.current_page_index + (1 if delta > 0 else -1)) % len(self.profile.pages)
            self.set_page(new_page)

    def _on_hw_touch(self, ev: TouchEvent) -> None:
        if self.screen_sleeping and ev.phase == "start":
            self.screen_sleeping = False
            self.driver.set_brightness(self.profile.brightness)
            return

        if ev.region == "center" and ev.key_index is not None:
            k_idx = ev.key_index
            if ev.phase == "start":
                self._pressed_keys.add(k_idx)
                if self.profile.haptics_enabled:
                    self.driver.vibrate(HAPTIC_SHORT)
                page = self.profile.pages[self.current_page_index]
                kcfg = page.keys[k_idx]
                self.log_event(f"Touch Key {k_idx} ({kcfg.title}) -> {kcfg.action_type}:{kcfg.param}")
                self._execute_button_or_key_action(kcfg.action_type, kcfg.param)
                self.update_button_leds()
                self.request_render()
            elif ev.phase == "end":
                if k_idx in self._pressed_keys:
                    self._pressed_keys.discard(k_idx)
                    self.request_render()

        elif ev.region in ("left", "right") and ev.strip_index is not None and ev.phase == "start":
            if self.profile.haptics_enabled:
                self.driver.vibrate(HAPTIC_SHORT)
            self._on_hw_dial_press(ev.strip_index, True)

    def _on_hw_swipe(self, direction: str) -> None:
        if self.profile.haptics_enabled:
            self.driver.vibrate(HAPTIC_MEDIUM)
        if direction == "left":
            new_page = (self.current_page_index + 1) % len(self.profile.pages)
        else:
            new_page = (self.current_page_index - 1) % len(self.profile.pages)
        self.log_event(f"Swipe {direction} -> switching to Page {new_page + 1}")
        self.set_page(new_page)

    # -------------------------------------------------------------------------
    # Action Execution
    # -------------------------------------------------------------------------

    def set_page(self, page_index: int) -> None:
        if 0 <= page_index < len(self.profile.pages):
            self.current_page_index = page_index
            self.update_button_leds()
            self.request_render()
            if self.on_page_changed:
                try:
                    self.on_page_changed(page_index)
                except Exception:
                    pass

    def trigger_key(self, key_index: int) -> None:
        """Trigger a center key action (from GUI click or hardware touch)."""
        page = self.profile.pages[self.current_page_index]
        if 0 <= key_index < len(page.keys):
            kcfg = page.keys[key_index]
            self.log_event(f"Triggered Key {key_index} ({kcfg.title}) -> {kcfg.action_type}:{kcfg.param}")
            self._execute_button_or_key_action(kcfg.action_type, kcfg.param)
            self.update_button_leds()
            self.request_render()

    def _execute_button_or_key_action(self, action_type: str, param: str) -> None:
        at = action_type.strip().lower()
        if at == "page":
            try:
                target_pg = int(param)
                if 0 <= target_pg < len(self.profile.pages):
                    self.current_page_index = target_pg
                    if self.on_page_changed:
                        self.on_page_changed(target_pg)
            except ValueError:
                pass
        elif at == "obs_scene":
            self.obs.set_scene(param)
        elif at == "obs_record":
            self.obs.toggle_record()
        elif at == "obs_stream":
            self.obs.toggle_stream()
        elif at == "obs_launch":
            self.obs.launch_obs()
        elif at == "audio_mute":
            self.audio.toggle_mute(param or "default_sink")
        elif at == "audio_default_sink":
            self.audio.set_default_sink(param)
        elif at == "media":
            self.media.media_command(param or "PlayPause")
        elif at == "command":
            self.media.run_command_async(param)
        elif at == "brightness_delta":
            try:
                delta = int(param)
                self.profile.brightness = max(1, min(10, self.profile.brightness + delta))
                self.driver.set_brightness(self.profile.brightness)
            except ValueError:
                pass

    # -------------------------------------------------------------------------
    # Dirty-Tile Rendering & Hardware Sync
    # -------------------------------------------------------------------------

    def _build_key_tile_image(self, key_index: int, kcfg: KeyConfig) -> Image.Image:
        pressed = key_index in self._pressed_keys
        at = kcfg.action_type.lower()
        subtitle = ""
        active = False
        progress: Optional[float] = None
        sparkline = None
        badge = ""
        icon = kcfg.icon
        accent = kcfg.accent_color

        if at == "obs_scene":
            active = self.obs.current_scene.lower() == kcfg.param.lower()
            subtitle = "ACTIVE" if active else ("READY" if self.obs.connected else "OFFLINE")
            if active:
                badge = "ON"

        elif at == "obs_record":
            active = self.obs.recording
            subtitle = (self.obs.record_timecode or "REC") if active else "STOPPED"
            if active:
                badge = "REC"

        elif at == "obs_stream":
            active = self.obs.streaming
            subtitle = (self.obs.stream_timecode or "LIVE") if active else "OFFLINE"
            if active:
                badge = "LIVE"

        elif at == "obs_launch":
            active = self.obs.connected
            subtitle = "CONNECTED" if active else "LAUNCH"
            if active:
                badge = "WS"

        elif at == "audio_mute":
            ep = self.audio.resolve_target(kcfg.param or "default_sink")
            if ep is not None:
                if ep.muted:
                    subtitle = "MUTED"
                    badge = "MUTE"
                    accent = "#FF3355"
                    if "mic" in icon:
                        icon = "mic_mute"
                    else:
                        icon = "mute"
                else:
                    active = True
                    subtitle = f"{int(round(ep.volume * 100))}%"
                    progress = min(1.0, ep.volume)
            else:
                subtitle = "N/A"

        elif at == "audio_default_sink":
            ep = self.audio.resolve_target(kcfg.param)
            if ep is not None:
                active = ep.id == self.audio.default_sink
                subtitle = "ACTIVE" if active else f"{int(round(ep.volume * 100))}%"
                if active:
                    badge = "OUT"

        elif at == "media":
            if kcfg.param == "PlayPause":
                active = self.media.playing
                subtitle = "PLAYING" if active else "PAUSED"
            else:
                subtitle = kcfg.param.upper()

        elif at == "telemetry":
            m = self.telemetry.metrics
            p = kcfg.param.lower()
            if p == "cpu":
                subtitle = f"{m.cpu_percent:.0f}% {m.cpu_temp_c:.0f}°C"
                sparkline = list(m.cpu_history)
            elif p == "cpu_temp":
                subtitle = f"{m.cpu_temp_c:.1f}°C"
                progress = min(1.0, m.cpu_temp_c / 95.0)
            elif p == "gpu":
                subtitle = f"{m.gpu_percent:.0f}% {m.gpu_temp_c:.0f}°C"
                sparkline = list(m.gpu_history)
            elif p == "vram":
                subtitle = f"{m.gpu_vram_used_gb:.1f}/{m.gpu_vram_total_gb:.0f}G"
                progress = m.gpu_vram_used_gb / max(1.0, m.gpu_vram_total_gb)
            elif p == "ram":
                subtitle = f"{m.ram_used_gb:.1f}G ({m.ram_percent:.0f}%)"
                sparkline = list(m.ram_history)
            elif p == "nvme_temp":
                subtitle = f"{m.nvme_temp_c:.0f}°C"
                progress = min(1.0, m.nvme_temp_c / 85.0)
            elif p == "psu_temp":
                subtitle = f"{m.psu_temp_c:.1f}°C"
                progress = min(1.0, m.psu_temp_c / 70.0)
            elif p == "battery":
                subtitle = f"{m.battery_percent:.0f}%"
                progress = min(1.0, m.battery_percent / 100.0)
            elif p == "net_down":
                subtitle = f"{m.net_down_mbps:.2f} MB/s"
                sparkline = list(m.net_history)
            elif p == "net_up":
                subtitle = f"{m.net_up_mbps:.2f} MB/s"
                sparkline = list(m.net_history)
            elif p == "clock":
                now_dt = datetime.datetime.now()
                subtitle = now_dt.strftime("%H:%M:%S")

        elif at == "brightness_delta":
            subtitle = f"{self.profile.brightness * 10}%"
            progress = self.profile.brightness / 10.0

        elif at == "command":
            subtitle = "RUN"

        return render_key_tile(
            title=kcfg.title,
            subtitle=subtitle,
            icon=icon,
            accent_color=accent,
            active=active,
            pressed=pressed,
            progress=progress,
            sparkline=sparkline,
            badge=badge,
        )

    def _build_strip_slot_image(self, slot_index: int, dcfg: DialConfig) -> Image.Image:
        at = dcfg.action_type.lower()
        if at == "audio":
            ep = self.audio.resolve_target(dcfg.param)
            if ep is not None:
                return render_strip_slot(
                    label=dcfg.label,
                    value_text=f"{int(round(ep.volume * 100))}%",
                    level=min(1.0, ep.volume),
                    muted=ep.muted,
                    accent_color=dcfg.accent_color,
                )
            return render_strip_slot(
                label=dcfg.label,
                value_text="IDLE",
                level=0.0,
                muted=False,
                accent_color=dcfg.accent_color,
            )

        if at == "brightness":
            lv = 0.0 if self.screen_sleeping else (self.profile.brightness / 10.0)
            return render_strip_slot(
                label=dcfg.label,
                value_text="OFF" if self.screen_sleeping else f"{self.profile.brightness * 10}%",
                level=lv,
                muted=self.screen_sleeping,
                accent_color=dcfg.accent_color,
            )

        return render_strip_slot(
            label=dcfg.label,
            value_text=f"P{self.current_page_index + 1}",
            level=(self.current_page_index + 1) / max(1, len(self.profile.pages)),
            muted=False,
            accent_color=dcfg.accent_color,
        )

    def render_all(self, push_to_hardware: bool = True, force_full: bool = False) -> None:
        """Render tiles and push ONLY dirty (changed) tiles or regions to hardware."""
        with self._render_lock:
            canvas = Image.new("RGB", (480, 270), (8, 10, 14))
            dirty_strips: list[tuple[int, Image.Image]] = []
            dirty_keys: list[tuple[int, Image.Image]] = []

            for i, dcfg in enumerate(self.profile.dials[:6]):
                s_img = self._build_strip_slot_image(i, dcfg)
                self.last_strip_images[i] = s_img
                sx = 0 if i < 3 else 420
                sy = (i % 3) * 90
                canvas.paste(s_img, (sx, sy))
                s_bytes = s_img.tobytes()
                if force_full or s_bytes != self._sent_strip_bytes[i]:
                    self._sent_strip_bytes[i] = s_bytes
                    dirty_strips.append((i, s_img))

            page = self.profile.pages[self.current_page_index]
            for k, kcfg in enumerate(page.keys[:12]):
                k_img = self._build_key_tile_image(k, kcfg)
                self.last_key_images[k] = k_img
                kx = 60 + (k % 4) * 90
                ky = (k // 4) * 90
                canvas.paste(k_img, (kx, ky))
                k_bytes = k_img.tobytes()
                if force_full or k_bytes != self._sent_key_bytes[k]:
                    self._sent_key_bytes[k] = k_bytes
                    dirty_keys.append((k, k_img))

            self.last_screen_image = canvas

            if push_to_hardware and self.driver.connected and not self.screen_sleeping:
                if force_full:
                    self.driver.draw_image(0, 0, canvas, refresh=True)
                elif len(dirty_keys) >= 6 and not dirty_strips:
                    center_crop = canvas.crop((60, 0, 420, 270))
                    self.driver.draw_image(60, 0, center_crop, refresh=True)
                elif dirty_strips or dirty_keys:
                    for s_idx, s_img in dirty_strips:
                        self.driver.draw_strip_slot(s_idx, s_img, refresh=False)
                    for k_idx, k_img in dirty_keys:
                        self.driver.draw_key(k_idx, k_img, refresh=False)
                    self.driver.refresh_display()

            if self.on_screen_updated and (force_full or dirty_strips or dirty_keys):
                try:
                    self.on_screen_updated(canvas.copy())
                except Exception:
                    pass

    def update_button_leds(self, force: bool = False) -> None:
        """Update the 8 bottom RGB physical button colors, sending only changed LEDs over USB."""
        for idx, bcfg in enumerate(self.profile.buttons[:8]):
            base_rgb = hex_to_rgb(bcfg.color)
            at = bcfg.action_type.lower()

            if idx == 0:
                rgb = base_rgb
            elif at == "page":
                try:
                    target_page = int(bcfg.param)
                except ValueError:
                    target_page = -1
                if target_page == self.current_page_index:
                    rgb = base_rgb
                else:
                    rgb = blend_rgb((0, 0, 0), base_rgb, 0.22)
            elif at == "audio_mute":
                ep = self.audio.resolve_target(bcfg.param or "default_sink")
                if ep and ep.muted:
                    rgb = (255, 25, 45)
                else:
                    rgb = base_rgb
            elif at == "obs_record":
                if self.obs.recording:
                    rgb = (255, 10, 30)
                else:
                    rgb = blend_rgb((0, 0, 0), base_rgb, 0.28)
            else:
                rgb = base_rgb

            self.last_button_leds[idx] = rgb
            if self.driver.connected and (force or rgb != self._sent_button_leds[idx]):
                self._sent_button_leds[idx] = rgb
                self.driver.set_button_color(idx, rgb[0], rgb[1], rgb[2])
