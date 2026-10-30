import struct
import threading
import time
import unittest
from PIL import Image

from linuxdeck.driver import (
    LoupedeckDriver,
    image_to_rgb565_le,
    CMD_BUTTON,
    CMD_ENCODER,
    CMD_TOUCH_MOVE,
    CMD_TOUCH_END,
)
from linuxdeck.renderer import render_key_tile, render_strip_slot
from linuxdeck.autodiscover import discover_system_environment
from linuxdeck.config import create_default_profile
from linuxdeck.integrations.audio import PipeWireController, AudioEndpoint


class TestLinuxDeckDriverProtocol(unittest.TestCase):
    def test_rgb565_le_color_packing(self):
        # Pure Red (255, 0, 0) -> R5=31 (0xF800) -> LE bytes: 0x00, 0xF8
        red_img = Image.new("RGB", (1, 1), (255, 0, 0))
        self.assertEqual(image_to_rgb565_le(red_img), b"\x00\xf8")

        # Pure Green (0, 255, 0) -> G6=63 (0x07E0) -> LE bytes: 0xE0, 0x07
        green_img = Image.new("RGB", (1, 1), (0, 255, 0))
        self.assertEqual(image_to_rgb565_le(green_img), b"\xe0\x07")

        # Pure Blue (0, 0, 255) -> B5=31 (0x001F) -> LE bytes: 0x1F, 0x00
        blue_img = Image.new("RGB", (1, 1), (0, 0, 255))
        self.assertEqual(image_to_rgb565_le(blue_img), b"\x1f\x00")

        # Pure White (255, 255, 255) -> 0xFFFF -> LE bytes: 0xFF, 0xFF
        white_img = Image.new("RGB", (1, 1), (255, 255, 255))
        self.assertEqual(image_to_rgb565_le(white_img), b"\xff\xff")

    def test_websocket_frame_wrapping(self):
        # Short payload (<= 125 bytes) uses 6-byte header
        short_payload = b"\x04\x09\x01\x08"
        wrapped_short = LoupedeckDriver._wrap_ws_frame(short_payload)
        self.assertEqual(wrapped_short[:2], bytes([0x82, 0x80 | len(short_payload)]))
        self.assertEqual(wrapped_short[2:6], b"\x00\x00\x00\x00")
        self.assertEqual(wrapped_short[6:], short_payload)

        # Extended payload (> 125 bytes) uses 14-byte 64-bit length header
        long_payload = b"\xaa" * 200
        wrapped_long = LoupedeckDriver._wrap_ws_frame(long_payload)
        self.assertEqual(wrapped_long[:2], b"\x82\xff")
        self.assertEqual(struct.unpack(">Q", wrapped_long[2:10])[0], 200)
        self.assertEqual(wrapped_long[10:14], b"\x00\x00\x00\x00")
        self.assertEqual(wrapped_long[14:], long_payload)

    def test_event_parsing_and_coalesced_dispatch(self):
        drv = LoupedeckDriver()
        drv._running = True
        dispatch_thread = threading.Thread(target=drv._dispatch_loop, daemon=True)
        dispatch_thread.start()

        buttons = []
        dial_rotations = []
        touches = []

        drv.on_button = lambda idx, pressed: buttons.append((idx, pressed))
        drv.on_dial_rotate = lambda idx, delta: dial_rotations.append((idx, delta))
        drv.on_touch = lambda ev: touches.append((ev.phase, ev.region, ev.key_index))

        try:
            # 1. Physical button 0 (wire id 0x07) press (0x00)
            drv._parse_packet_fast(bytes([5, CMD_BUTTON, 1, 0x07, 0x00]))
            # 2. Rapid dial 0 (wire id 0x01) ticks: +1, +1, +1 -> should coalesce or sum to +3
            for i in range(3):
                drv._parse_packet_fast(bytes([5, CMD_ENCODER, i + 2, 0x01, 0x01]))
            # 3. Touch start & end on Key 0 (x=100, y=45 -> center col 0, row 0 -> key 0)
            drv._parse_packet_fast(bytes([9, CMD_TOUCH_MOVE, 10]) + struct.pack("!BHHB", 0, 100, 45, 1))
            drv._parse_packet_fast(bytes([9, CMD_TOUCH_END, 11]) + struct.pack("!BHHB", 0, 100, 45, 1))

            time.sleep(0.05)

            self.assertEqual(buttons, [(0, True)])
            total_dial_delta = sum(d[1] for d in dial_rotations if d[0] == 0)
            self.assertEqual(total_dial_delta, 3)
            self.assertEqual(touches[0], ("start", "center", 0))
            self.assertEqual(touches[1], ("end", "center", 0))
        finally:
            drv._running = False
            drv._event_queue.put(None)
            dispatch_thread.join(timeout=1.0)


class TestRendererAndAutoDiscovery(unittest.TestCase):
    def test_tile_and_strip_dimensions(self):
        tile = render_key_tile(
            title="MIC MUTE",
            subtitle="UNMUTED",
            icon="mic",
            accent_color="#00FF66",
            active=True,
            progress=0.75,
            badge="LIVE",
        )
        self.assertEqual(tile.size, (90, 90))
        self.assertEqual(len(image_to_rgb565_le(tile)), 90 * 90 * 2)

        strip = render_strip_slot(
            label="MASTER",
            value_text="80%",
            level=0.8,
            muted=False,
            accent_color="#00FF66",
        )
        self.assertEqual(strip.size, (60, 90))
        self.assertEqual(len(image_to_rgb565_le(strip)), 60 * 90 * 2)

    def test_autodiscovery_and_default_profile(self):
        env = discover_system_environment()
        self.assertTrue(bool(env.cpu_title))
        self.assertTrue(bool(env.gpu_title))
        self.assertTrue(bool(env.primary_sink_title))
        self.assertTrue(bool(env.primary_mic_title))

        profile = create_default_profile()
        self.assertEqual(len(profile.pages), 4)
        self.assertEqual(len(profile.dials), 6)
        self.assertEqual(len(profile.buttons), 8)
        for page in profile.pages:
            self.assertEqual(len(page.keys), 12)

    def test_smart_audio_category_matching(self):
        pw = PipeWireController()
        pw.app_streams = {
            "vesktop": AudioEndpoint(
                id="42",
                kind="app",
                name="vesktop.bin",
                description="Vesktop Voice",
                volume=0.85,
                muted=False,
            ),
            "firefox": AudioEndpoint(
                id="43",
                kind="app",
                name="Firefox",
                description="Firefox Audio",
                volume=0.65,
                muted=False,
            ),
        }
        chat_ep = pw.resolve_target("app:chat")
        self.assertIsNotNone(chat_ep)
        self.assertEqual(chat_ep.id, "42")
        self.assertAlmostEqual(chat_ep.volume, 0.85)

        browser_ep = pw.resolve_target("app:browser")
        self.assertIsNotNone(browser_ep)
        self.assertEqual(browser_ep.id, "43")
        self.assertAlmostEqual(browser_ep.volume, 0.65)


if __name__ == "__main__":
    unittest.main()
