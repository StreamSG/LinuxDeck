"""Low-level wire protocol driver for Loupedeck Live / Live S / CT and Razer Stream Controller."""

from __future__ import annotations

import glob
import logging
import os
import queue
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import serial
import serial.tools.list_ports
from PIL import Image

logger = logging.getLogger(__name__)

# Supported USB VID:PID pairs across Razer & Loupedeck devices
SUPPORTED_DEVICES = [
    (0x1532, 0x0D06, "Razer Stream Controller"),
    (0x1532, 0x0D09, "Razer Stream Controller X"),
    (0x2EC2, 0x0004, "Loupedeck Live"),
    (0x2EC2, 0x0006, "Loupedeck Live S"),
    (0x2EC2, 0x0003, "Loupedeck CT"),
]

# Unified display identifier ("\x00M") and dimensions
DISPLAY_ID_CENTER = b"\x00M"
DISPLAY_WIDTH = 480
DISPLAY_HEIGHT = 270

# Screen regions on the unified 480x270 display
LEFT_STRIP_X = 0
LEFT_STRIP_W = 60
CENTER_GRID_X = 60
CENTER_GRID_W = 360
RIGHT_STRIP_X = 420
RIGHT_STRIP_W = 60
KEY_SIZE = 90  # 4 cols x 3 rows of 90x90 keys in center grid

# Commands (Inner Loupedeck protocol byte 1)
CMD_BUTTON = 0x00
CMD_ENCODER = 0x01
CMD_SET_COLOR = 0x02
CMD_SERIAL = 0x03
CMD_RESET = 0x06
CMD_VERSION = 0x07
CMD_SET_BRIGHTNESS = 0x09
CMD_REFRESH = 0x0F
CMD_FRAMEBUFF = 0x10
CMD_SET_VIBRATION = 0x1B
CMD_TOUCH_MOVE = 0x4D
CMD_TOUCH_END = 0x6D

# Vibration / Haptic patterns (DRV2605L waveform IDs)
HAPTIC_SHORT = 0x01
HAPTIC_MEDIUM = 0x0A
HAPTIC_LONG = 0x0F
HAPTIC_LOW = 0x31
HAPTIC_SHORT_LOW = 0x32
HAPTIC_SHORT_LOWER = 0x33
HAPTIC_LOWER = 0x40
HAPTIC_LOWEST = 0x41

# Physical button wire IDs: index 0..7 -> 0x07..0x0E
BUTTON_HW_IDS = [0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E]

# Rotary dial wire IDs: index 0..5 -> 0x01..0x06
DIAL_HW_IDS = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06]
DIAL_NAMES = ["knobTL", "knobCL", "knobBL", "knobTR", "knobCR", "knobBR"]

WS_UPGRADE_REQUEST = (
    b"GET /index.html\n"
    b"HTTP/1.1\n"
    b"Connection: Upgrade\n"
    b"Upgrade: websocket\n"
    b"Sec-WebSocket-Key: 123abc\n\n"
)

WS_CLOSE_FRAME = bytes([0x88, 0x80, 0x00, 0x00, 0x00, 0x00])


@dataclass
class TouchEvent:
    """Capacitive touch screen event on the 480x270 surface."""

    phase: str  # "start", "move", "end"
    x: int
    y: int
    touch_id: int
    region: str  # "left", "center", "right"
    key_index: Optional[int]  # 0..11 if region == "center", else None
    strip_index: Optional[int]  # 0..2 if left, 3..5 if right, else None


def image_to_rgb565_le(img: Image.Image) -> bytes:
    """Vectorized NumPy conversion of a PIL Image to 16-bit RGB565 little-endian bytes (~0.2ms)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    arr = np.frombuffer(img.tobytes(), dtype=np.uint8).reshape(-1, 3)
    r = (arr[:, 0].astype(np.uint16) & 0xF8) << 8
    g = (arr[:, 1].astype(np.uint16) & 0xFC) << 3
    b = arr[:, 2].astype(np.uint16) >> 3
    rgb565 = (r | g | b).astype("<u2")
    return rgb565.tobytes()


def find_device_port() -> tuple[Optional[str], str]:
    """Locate the serial port and model name for a connected Loupedeck or Razer Stream Controller."""
    for port in serial.tools.list_ports.comports():
        for vid, pid, model_name in SUPPORTED_DEVICES:
            if port.vid == vid and port.pid == pid:
                return port.device, model_name

    for pattern, label in (
        ("*Razer_Stream_Controller*", "Razer Stream Controller"),
        ("*Loupedeck_Live_S*", "Loupedeck Live S"),
        ("*Loupedeck_Live*", "Loupedeck Live"),
        ("*Loupedeck_CT*", "Loupedeck CT"),
    ):
        matches = sorted(glob.glob(f"/dev/serial/by-id/{pattern}"))
        if matches:
            return os.path.realpath(matches[0]), label

    return None, "Loupedeck / Razer Stream Controller"


class LoupedeckDriver:
    """Low-latency, non-blocking serial WebSocket driver for Loupedeck & Razer Stream Controller."""

    def __init__(self, port: Optional[str] = None) -> None:
        self.port_path = port
        self.device_name = "Stream Controller"
        self._serial: Optional[serial.Serial] = None
        self._write_lock = threading.Lock()
        self._draw_lock = threading.Lock()
        self._tx_id = 0
        self._running = False
        self._reader_thread: Optional[threading.Thread] = None
        self._dispatch_thread: Optional[threading.Thread] = None

        self.serial_number: str = ""
        self.firmware_version: str = ""
        self.connected: bool = False

        # Active touch tracking (touch_id -> (start_x, start_y))
        self._active_touches: dict[int, tuple[int, int]] = {}

        # Synchronization for command ACKs
        self._ack_events: dict[int, threading.Event] = {}

        # Non-blocking event dispatch queue + coalesced dial rotation deltas
        self._event_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._dial_delta_lock = threading.Lock()
        self._pending_dial_deltas: dict[int, int] = {}

        # Event callbacks
        self.on_button: Optional[Callable[[int, bool], None]] = None
        self.on_dial_press: Optional[Callable[[int, bool], None]] = None
        self.on_dial_rotate: Optional[Callable[[int, int], None]] = None
        self.on_touch: Optional[Callable[[TouchEvent], None]] = None
        self.on_swipe: Optional[Callable[[str], None]] = None
        self.on_connection_changed: Optional[Callable[[bool], None]] = None

    def _next_tx_id(self) -> int:
        self._tx_id = (self._tx_id % 255) + 1
        return self._tx_id

    def connect(self, port: Optional[str] = None) -> bool:
        """Open the serial port, perform the WebSocket upgrade handshake, and start reader/dispatch threads."""
        detected_port, detected_name = find_device_port()
        target_port = port or self.port_path or detected_port
        if not target_port:
            logger.debug("No Loupedeck / Razer Stream Controller found.")
            return False

        self.device_name = detected_name
        self.disconnect()

        try:
            ser = serial.Serial(
                port=target_port,
                baudrate=460800,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.015,
                write_timeout=0.5,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception as exc:
            logger.error("Failed to open serial port %s: %s", target_port, exc)
            return False

        self.port_path = target_port
        self._serial = ser

        try:
            # 1. Always send a WebSocket Close frame first in case the MCU was left in WS mode
            ser.write(WS_CLOSE_FRAME)
            ser.flush()
            time.sleep(0.05)
            ser.reset_input_buffer()

            # 2. Send HTTP WebSocket Upgrade request and wait 120ms for full MCU response buffer
            ser.write(WS_UPGRADE_REQUEST)
            ser.flush()
            time.sleep(0.12)

            handshake_buf = bytearray()
            while ser.in_waiting > 0:
                handshake_buf.extend(ser.read(ser.in_waiting))
                time.sleep(0.01)
            ser.reset_input_buffer()

            if b"101 Switching Protocols" in handshake_buf:
                logger.info(
                    "WebSocket upgrade handshake succeeded on %s (%s, drained %d bytes)",
                    target_port,
                    self.device_name,
                    len(handshake_buf),
                )
            else:
                logger.info(
                    "Device on %s ready (drained %d handshake bytes)",
                    target_port,
                    len(handshake_buf),
                )
        except Exception as exc:
            logger.error("Handshake failed on %s: %s", target_port, exc)
            ser.close()
            self._serial = None
            return False

        self._event_queue = queue.SimpleQueue()
        self._pending_dial_deltas.clear()
        self._running = True
        self.connected = True

        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="LinuxDeckReader", daemon=True
        )
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, name="LinuxDeckDispatch", daemon=True
        )
        self._reader_thread.start()
        self._dispatch_thread.start()

        self.request_device_info()

        if self.on_connection_changed:
            try:
                self.on_connection_changed(True)
            except Exception:
                pass
        return True

    def disconnect(self) -> None:
        """Gracefully close the WebSocket framing and serial connection."""
        was_connected = self.connected
        self._running = False
        self.connected = False

        if self._reader_thread and self._reader_thread.is_alive():
            if threading.current_thread() is not self._reader_thread:
                self._reader_thread.join(timeout=0.3)
        self._reader_thread = None

        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._event_queue.put(None)
            if threading.current_thread() is not self._dispatch_thread:
                self._dispatch_thread.join(timeout=0.3)
        self._dispatch_thread = None

        with self._write_lock:
            if self._serial is not None:
                try:
                    self._serial.write(WS_CLOSE_FRAME)
                    self._serial.flush()
                except Exception:
                    pass
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None

        if was_connected and self.on_connection_changed:
            try:
                self.on_connection_changed(False)
            except Exception:
                pass

    @staticmethod
    def _wrap_ws_frame(payload: bytes) -> bytes:
        """Wrap an inner Loupedeck message in a masked RFC 6455 binary WebSocket frame."""
        length = len(payload)
        if length <= 125:
            header = struct.pack("!BB4s", 0x82, 0x80 | length, b"\x00\x00\x00\x00")
        else:
            header = struct.pack("!BBQ4s", 0x82, 0xFF, length, b"\x00\x00\x00\x00")
        return header + payload

    def send_command(
        self, command: int, data: bytes = b"", wait_ack: bool = True, timeout: float = 0.08
    ) -> int:
        """Send a Loupedeck command wrapped in a WebSocket binary frame, serialized with MCU ACK."""
        if not self.connected or self._serial is None:
            return 0

        if wait_ack and threading.current_thread() is self._reader_thread:
            wait_ack = False

        with self._write_lock:
            if not self.connected or self._serial is None:
                return 0

            tx_id = self._next_tx_id()
            msg_len = min(3 + len(data), 0xFF)
            inner = bytes([msg_len, command & 0xFF, tx_id]) + data
            frame = self._wrap_ws_frame(inner)

            ack_event: Optional[threading.Event] = None
            if wait_ack:
                ack_event = threading.Event()
                self._ack_events[tx_id] = ack_event

            try:
                self._serial.write(frame)
                self._serial.flush()
            except Exception as exc:
                logger.error("Serial write error: %s", exc)
                self._ack_events.pop(tx_id, None)
                self.connected = False
                return 0

            if ack_event is not None:
                ack_event.wait(timeout=timeout)
                self._ack_events.pop(tx_id, None)

            return tx_id

    def request_device_info(self) -> None:
        self.send_command(CMD_SERIAL, b"", wait_ack=True)
        self.send_command(CMD_VERSION, b"", wait_ack=True)

    def set_brightness(self, level: int) -> None:
        clamped = max(0, min(10, int(level)))
        self.send_command(CMD_SET_BRIGHTNESS, bytes([clamped]), wait_ack=True)

    def vibrate(self, pattern: int = HAPTIC_SHORT) -> None:
        self.send_command(CMD_SET_VIBRATION, bytes([pattern & 0xFF]), wait_ack=True)

    def set_button_color(self, button_index: int, r: int, g: int, b: int) -> None:
        if 0 <= button_index < len(BUTTON_HW_IDS):
            hw_id = BUTTON_HW_IDS[button_index]
            payload = bytes([hw_id, r & 0xFF, g & 0xFF, b & 0xFF])
            self.send_command(CMD_SET_COLOR, payload, wait_ack=True)

    def draw_rgb565(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        rgb565_le: bytes,
        refresh: bool = True,
        wait_ack: bool = True,
    ) -> None:
        """Write a rectangle of RGB565 LE pixels to the unified 480x270 display."""
        expected_bytes = width * height * 2
        if len(rgb565_le) != expected_bytes:
            raise ValueError(f"Expected {expected_bytes} RGB565 bytes, got {len(rgb565_le)}")

        with self._draw_lock:
            header = struct.pack("!2sHHHH", DISPLAY_ID_CENTER, x, y, width, height)
            self.send_command(CMD_FRAMEBUFF, header + rgb565_le, wait_ack=wait_ack)
            if refresh:
                self.refresh_display(wait_ack=wait_ack)

    def draw_image(
        self,
        x: int,
        y: int,
        image: Image.Image,
        refresh: bool = True,
        wait_ack: bool = True,
    ) -> None:
        rgb565_le = image_to_rgb565_le(image)
        self.draw_rgb565(
            x=x,
            y=y,
            width=image.width,
            height=image.height,
            rgb565_le=rgb565_le,
            refresh=refresh,
            wait_ack=wait_ack,
        )

    def draw_key(self, key_index: int, image: Image.Image, refresh: bool = True) -> None:
        if not (0 <= key_index < 12):
            raise ValueError(f"key_index must be 0..11, got {key_index}")
        if image.size != (KEY_SIZE, KEY_SIZE):
            image = image.resize((KEY_SIZE, KEY_SIZE), Image.Resampling.LANCZOS)
        col = key_index % 4
        row = key_index // 4
        x = CENTER_GRID_X + col * KEY_SIZE
        y = row * KEY_SIZE
        self.draw_image(x, y, image, refresh=refresh, wait_ack=True)

    def draw_strip_slot(self, slot_index: int, image: Image.Image, refresh: bool = True) -> None:
        if not (0 <= slot_index < 6):
            raise ValueError(f"slot_index must be 0..5, got {slot_index}")
        if image.size != (LEFT_STRIP_W, KEY_SIZE):
            image = image.resize((LEFT_STRIP_W, KEY_SIZE), Image.Resampling.LANCZOS)
        x = LEFT_STRIP_X if slot_index < 3 else RIGHT_STRIP_X
        y = (slot_index % 3) * KEY_SIZE
        self.draw_image(x, y, image, refresh=refresh, wait_ack=True)

    def refresh_display(self, wait_ack: bool = True) -> None:
        self.send_command(CMD_REFRESH, DISPLAY_ID_CENTER, wait_ack=wait_ack)

    # -------------------------------------------------------------------------
    # High-Speed Reader & Coalescing Dispatcher
    # -------------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Dedicated low-latency serial reader. Never executes user callbacks directly."""
        buf = bytearray()
        while self._running and self._serial is not None:
            try:
                waiting = self._serial.in_waiting
                chunk = self._serial.read(waiting if waiting > 0 else 1)
            except Exception as exc:
                if self._running:
                    logger.error("Serial read error: %s", exc)
                    self.connected = False
                    self._event_queue.put(("conn", False))
                break

            if not chunk:
                continue

            buf.extend(chunk)
            try:
                extra = self._serial.in_waiting
                if extra > 0:
                    buf.extend(self._serial.read(extra))
            except Exception:
                pass

            while len(buf) >= 2:
                if buf[0] != 0x82:
                    idx = buf.find(0x82)
                    if idx == -1:
                        buf.clear()
                        break
                    del buf[:idx]
                    if len(buf) < 2:
                        break

                payload_len = buf[1] & 0x7F
                header_len = 2
                if payload_len == 126:
                    if len(buf) < 4:
                        break
                    payload_len = struct.unpack("!H", buf[2:4])[0]
                    header_len = 4
                elif payload_len == 127:
                    if len(buf) < 10:
                        break
                    payload_len = struct.unpack("!Q", buf[2:10])[0]
                    header_len = 10

                if len(buf) < header_len + payload_len:
                    break

                payload = bytes(buf[header_len : header_len + payload_len])
                del buf[: header_len + payload_len]
                self._parse_packet_fast(payload)

    def _parse_packet_fast(self, pkt: bytes) -> None:
        if len(pkt) < 3:
            return

        msg_len = pkt[0]
        cmd = pkt[1]
        tx_id = pkt[2]
        data = pkt[3:]

        if tx_id != 0:
            ev_ack = self._ack_events.get(tx_id)
            if ev_ack is not None:
                ev_ack.set()

        if cmd == CMD_BUTTON:
            if msg_len < 5 or len(data) < 2:
                return
            hw_id = data[0]
            pressed = data[1] == 0x00
            if hw_id in BUTTON_HW_IDS:
                self._event_queue.put(("button", BUTTON_HW_IDS.index(hw_id), pressed))
            elif hw_id in DIAL_HW_IDS:
                self._event_queue.put(("dial_press", DIAL_HW_IDS.index(hw_id), pressed))

        elif cmd == CMD_ENCODER:
            if len(data) < 2:
                return
            hw_id = data[0]
            delta = struct.unpack("b", bytes([data[1]]))[0]
            if hw_id in DIAL_HW_IDS:
                dial_idx = DIAL_HW_IDS.index(hw_id)
                with self._dial_delta_lock:
                    prev = self._pending_dial_deltas.get(dial_idx, 0)
                    self._pending_dial_deltas[dial_idx] = prev + delta
                    if prev == 0:
                        self._event_queue.put(("dial_rotate", dial_idx))

        elif cmd in (CMD_TOUCH_MOVE, CMD_TOUCH_END):
            if len(data) < 6:
                return
            x = struct.unpack("!H", data[1:3])[0]
            y = struct.unpack("!H", data[3:5])[0]
            touch_id = data[5]

            if cmd == CMD_TOUCH_MOVE:
                if touch_id not in self._active_touches:
                    self._active_touches[touch_id] = (x, y)
                    phase = "start"
                else:
                    return
            else:
                start_pos = self._active_touches.pop(touch_id, None)
                phase = "end"
                if start_pos is not None:
                    dx = x - start_pos[0]
                    dy = y - start_pos[1]
                    if abs(dx) >= 95 and abs(dx) > abs(dy) * 1.6:
                        direction = "right" if dx > 0 else "left"
                        self._event_queue.put(("swipe", direction))

            row = min(2, max(0, y // KEY_SIZE))
            if x < CENTER_GRID_X:
                region = "left"
                key_index = None
                strip_index = row
            elif x < RIGHT_STRIP_X:
                region = "center"
                col = min(3, max(0, (x - CENTER_GRID_X) // KEY_SIZE))
                key_index = row * 4 + col
                strip_index = None
            else:
                region = "right"
                key_index = None
                strip_index = 3 + row

            ev = TouchEvent(
                phase=phase,
                x=x,
                y=y,
                touch_id=touch_id,
                region=region,
                key_index=key_index,
                strip_index=strip_index,
            )
            self._event_queue.put(("touch", ev))

        elif cmd == CMD_SERIAL and len(data) >= 1:
            self.serial_number = data.decode("ascii", errors="ignore").strip("\x00 ")
            logger.info("Device Serial Number: %s", self.serial_number)

        elif cmd == CMD_VERSION and len(data) >= 3:
            self.firmware_version = f"{data[0]}.{data[1]}.{data[2]}"
            logger.info("Device Firmware Version: %s", self.firmware_version)

    def _dispatch_loop(self) -> None:
        """Executes input events off the reader thread, coalescing rapid dial movements."""
        while self._running:
            item = self._event_queue.get()
            if item is None or not self._running:
                break

            kind = item[0]
            try:
                if kind == "button" and self.on_button:
                    self.on_button(item[1], item[2])
                elif kind == "dial_press" and self.on_dial_press:
                    self.on_dial_press(item[1], item[2])
                elif kind == "dial_rotate":
                    dial_idx = item[1]
                    with self._dial_delta_lock:
                        delta = self._pending_dial_deltas.pop(dial_idx, 0)
                    if delta != 0 and self.on_dial_rotate:
                        self.on_dial_rotate(dial_idx, delta)
                elif kind == "touch" and self.on_touch:
                    self.on_touch(item[1])
                elif kind == "swipe" and self.on_swipe:
                    self.on_swipe(item[1])
                elif kind == "conn" and self.on_connection_changed:
                    self.on_connection_changed(item[1])
            except Exception as exc:
                logger.exception("Error dispatching %s event: %s", kind, exc)


# Backward-compatible alias
RazerStreamControllerDriver = LoupedeckDriver
