"""Cross-vendor system telemetry collector (AMD/Intel CPU, NVIDIA/AMD/Intel GPU, RAM, NVMe, PSU, Battery, Net)."""

from __future__ import annotations

import collections
import glob
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Deque

import psutil

CPU_TEMP_SENSOR_PRIORITY = (
    "k10temp",
    "zenpower",
    "coretemp",
    "x86_pkg_temp",
    "cpu_thermal",
    "acpitz",
)

STORAGE_TEMP_SENSORS = ("nvme", "drivetemp")
EXTRA_TEMP_SENSORS = ("corsairpsu", "aquacomputer", "nct6775", "it87", "gigabyte_wmi", "asus")


@dataclass
class SystemMetrics:
    cpu_percent: float = 0.0
    cpu_temp_c: float = 0.0
    gpu_name: str = "GPU"
    gpu_percent: float = 0.0
    gpu_temp_c: float = 0.0
    gpu_vram_used_gb: float = 0.0
    gpu_vram_total_gb: float = 8.0
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 16.0
    nvme_temp_c: float = 0.0
    psu_temp_c: float = 0.0
    battery_percent: float = 100.0
    net_down_mbps: float = 0.0
    net_up_mbps: float = 0.0

    cpu_history: Deque[float] = field(default_factory=lambda: collections.deque([0.0] * 18, maxlen=18))
    gpu_history: Deque[float] = field(default_factory=lambda: collections.deque([0.0] * 18, maxlen=18))
    ram_history: Deque[float] = field(default_factory=lambda: collections.deque([0.0] * 18, maxlen=18))
    net_history: Deque[float] = field(default_factory=lambda: collections.deque([0.0] * 18, maxlen=18))


class TelemetryCollector:
    """Polls CPU, GPU (NVIDIA/AMD/Intel), RAM, sensors, and network rates with sparkline history."""

    def __init__(self) -> None:
        self.metrics = SystemMetrics()
        self._has_nvidia_smi = shutil.which("nvidia-smi") is not None
        self._amd_drm_card = self._find_drm_gpu_path()
        self._last_net = psutil.net_io_counters()
        self._last_net_time = time.monotonic()
        self._last_poll = 0.0
        psutil.cpu_percent(interval=None)
        self.poll(force=True)

    @staticmethod
    def _find_drm_gpu_path() -> str:
        """Find /sys/class/drm/cardX/device for AMD/Intel GPUs."""
        for card_dev in sorted(glob.glob("/sys/class/drm/card[0-9]/device")):
            if os.path.exists(os.path.join(card_dev, "gpu_busy_percent")) or glob.glob(
                os.path.join(card_dev, "hwmon/hwmon*/temp1_input")
            ):
                return card_dev
        return ""

    def poll(self, force: bool = False) -> SystemMetrics:
        now = time.monotonic()
        if not force and (now - self._last_poll) < 0.9:
            return self.metrics
        self._last_poll = now

        m = self.metrics
        m.cpu_percent = psutil.cpu_percent(interval=None)
        m.cpu_history.append(m.cpu_percent)

        vm = psutil.virtual_memory()
        m.ram_percent = vm.percent
        m.ram_used_gb = vm.used / (1024**3)
        m.ram_total_gb = vm.total / (1024**3)
        m.ram_history.append(m.ram_percent)

        # Temperatures across AMD/Intel CPU, NVMe/SATA, PSU/Motherboard
        try:
            temps = psutil.sensors_temperatures()
            for key in CPU_TEMP_SENSOR_PRIORITY:
                if key in temps and temps[key]:
                    m.cpu_temp_c = temps[key][0].current
                    break

            for key in STORAGE_TEMP_SENSORS:
                if key in temps and temps[key]:
                    m.nvme_temp_c = temps[key][0].current
                    break

            for key in EXTRA_TEMP_SENSORS:
                if key in temps and temps[key]:
                    m.psu_temp_c = temps[key][0].current
                    break
        except Exception:
            pass

        # Battery (for laptops / Steam Deck)
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                m.battery_percent = float(bat.percent)
        except Exception:
            pass

        # GPU telemetry: NVIDIA (nvidia-smi) or AMD/Intel (sysfs drm/hwmon)
        if self._has_nvidia_smi:
            try:
                out = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=0.5,
                ).strip()
                if out:
                    parts = [p.strip() for p in out.splitlines()[0].split(",")]
                    if len(parts) >= 4:
                        m.gpu_temp_c = float(parts[0])
                        m.gpu_percent = float(parts[1])
                        m.gpu_vram_used_gb = float(parts[2]) / 1024.0
                        m.gpu_vram_total_gb = float(parts[3]) / 1024.0
                        m.gpu_history.append(m.gpu_percent)
            except Exception:
                pass
        elif self._amd_drm_card:
            try:
                busy_file = os.path.join(self._amd_drm_card, "gpu_busy_percent")
                if os.path.exists(busy_file):
                    with open(busy_file, "r", encoding="utf-8") as f:
                        m.gpu_percent = float(f.read().strip())
                        m.gpu_history.append(m.gpu_percent)

                vram_used_file = os.path.join(self._amd_drm_card, "mem_info_vram_used")
                vram_tot_file = os.path.join(self._amd_drm_card, "mem_info_vram_total")
                if os.path.exists(vram_used_file) and os.path.exists(vram_tot_file):
                    with open(vram_used_file, "r", encoding="utf-8") as f:
                        m.gpu_vram_used_gb = float(f.read().strip()) / (1024**3)
                    with open(vram_tot_file, "r", encoding="utf-8") as f:
                        m.gpu_vram_total_gb = float(f.read().strip()) / (1024**3)

                temp_files = glob.glob(os.path.join(self._amd_drm_card, "hwmon/hwmon*/temp1_input"))
                if temp_files:
                    with open(temp_files[0], "r", encoding="utf-8") as f:
                        m.gpu_temp_c = float(f.read().strip()) / 1000.0
            except Exception:
                pass

        # Network throughput
        try:
            net_now = psutil.net_io_counters()
            dt = max(0.05, now - self._last_net_time)
            down_bps = max(0, net_now.bytes_recv - self._last_net.bytes_recv) / dt
            up_bps = max(0, net_now.bytes_sent - self._last_net.bytes_sent) / dt
            m.net_down_mbps = down_bps / (1024 * 1024)
            m.net_up_mbps = up_bps / (1024 * 1024)
            m.net_history.append(min(100.0, (m.net_down_mbps + m.net_up_mbps) * 10.0))
            self._last_net = net_now
            self._last_net_time = now
        except Exception:
            pass

        return m
