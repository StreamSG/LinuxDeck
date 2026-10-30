"""CLI and GUI entry point for LinuxDeck."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from . import __version__
from .controller import StreamControllerEngine


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="linuxdeck",
        description="LinuxDeck — Open-Source Linux Daemon & Control Center for Loupedeck & Razer Stream Controller",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the background hardware daemon without launching the PyQt6 GUI",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Start the PyQt6 Control Center minimized to the system tray",
    )
    parser.add_argument(
        "--autoconfig",
        action="store_true",
        help="Re-scan this PC's audio devices, GPU/CPU sensors, OBS scenes, and apps to regenerate the profile",
    )
    parser.add_argument(
        "--autoconfig-only",
        action="store_true",
        help="Generate and save the auto-discovered profile for this PC, then exit immediately",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"LinuxDeck {__version__}",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.autoconfig_only:
        from .config import load_profile, CONFIG_PATH
        from .autodiscover import discover_system_environment
        env = discover_system_environment()
        load_profile(force_autoconfig=True)
        print(f"Auto-configured LinuxDeck profile saved to {CONFIG_PATH}")
        print(
            f"  • CPU:   {env.cpu_title}\n"
            f"  • GPU:   {env.gpu_title}\n"
            f"  • Audio: {env.primary_sink_title} / {env.primary_mic_title}"
        )
        return 0

    engine = StreamControllerEngine(force_autoconfig=args.autoconfig)
    engine.start()

    if args.headless:
        def _sig_handler(_signum, _frame):
            engine.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
        logging.info("LinuxDeck running in headless daemon mode. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            engine.stop()
        return 0

    from PyQt6.QtWidgets import QApplication
    from .gui import StreamControllerMainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("LinuxDeck")
    window = StreamControllerMainWindow(engine, start_minimized=args.tray)  # noqa: F841

    def _qt_sigint(*_args):
        engine.stop()
        app.quit()

    signal.signal(signal.SIGINT, _qt_sigint)
    signal.signal(signal.SIGTERM, _qt_sigint)

    ret = app.exec()
    engine.stop()
    return ret


if __name__ == "__main__":
    sys.exit(main())
