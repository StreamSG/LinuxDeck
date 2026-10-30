#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_DIR}/.venv"
DIST_DIR="${REPO_DIR}/dist"
BUILD_DIR="${REPO_DIR}/build"
VERSION="1.0.0"

echo "============================================================"
echo "  Building LinuxDeck v${VERSION} Release Artifacts"
echo "============================================================"

rm -rf "${DIST_DIR}" "${BUILD_DIR}"
mkdir -p "${DIST_DIR}"

# 1. Build Python Wheel (.whl) and Source Tarball (.tar.gz)
echo "[1/3] Building Python Wheel (.whl) and Source Distribution (.tar.gz)..."
"${VENV_DIR}/bin/python" -m build --outdir "${DIST_DIR}" "${REPO_DIR}" >/dev/null

# 2. Build Standalone Single-File Linux Binary via PyInstaller
echo "[2/3] Compiling standalone single-file Linux executable (PyInstaller)..."
"${VENV_DIR}/bin/pyinstaller" \
    --noconfirm \
    --clean \
    --onefile \
    --name "LinuxDeck-${VERSION}-linux-x86_64" \
    --hidden-import "PyQt6.QtCore" \
    --hidden-import "PyQt6.QtGui" \
    --hidden-import "PyQt6.QtWidgets" \
    --hidden-import "serial.tools.list_ports" \
    --hidden-import "websockets.sync.client" \
    --distpath "${DIST_DIR}" \
    --workpath "${BUILD_DIR}/pyinstaller" \
    --specpath "${BUILD_DIR}" \
    "${REPO_DIR}/linuxdeck/__main__.py" >/dev/null

chmod +x "${DIST_DIR}/LinuxDeck-${VERSION}-linux-x86_64"

# 3. Build Native Debian/Ubuntu .deb Installer Package
echo "[3/3] Building Debian/Ubuntu installer package (linuxdeck_${VERSION}_amd64.deb)..."
DEB_ROOT="${BUILD_DIR}/deb/linuxdeck_${VERSION}_amd64"
mkdir -p \
    "${DEB_ROOT}/DEBIAN" \
    "${DEB_ROOT}/usr/bin" \
    "${DEB_ROOT}/usr/share/applications" \
    "${DEB_ROOT}/usr/share/icons/hicolor/scalable/apps" \
    "${DEB_ROOT}/usr/lib/systemd/user" \
    "${DEB_ROOT}/lib/udev/rules.d"

cp "${DIST_DIR}/LinuxDeck-${VERSION}-linux-x86_64" "${DEB_ROOT}/usr/bin/linuxdeck"
chmod 755 "${DEB_ROOT}/usr/bin/linuxdeck"

cp "${REPO_DIR}/packaging/linuxdeck.svg" "${DEB_ROOT}/usr/share/icons/hicolor/scalable/apps/linuxdeck.svg"
chmod 644 "${DEB_ROOT}/usr/share/icons/hicolor/scalable/apps/linuxdeck.svg"

sed "s|^Exec=.*|Exec=/usr/bin/linuxdeck|g; s|^Icon=.*|Icon=linuxdeck|g" \
    "${REPO_DIR}/packaging/linuxdeck.desktop" > "${DEB_ROOT}/usr/share/applications/linuxdeck.desktop"
chmod 644 "${DEB_ROOT}/usr/share/applications/linuxdeck.desktop"

sed "s|^ExecStart=.*|ExecStart=/usr/bin/linuxdeck --tray|g" \
    "${REPO_DIR}/packaging/linuxdeck.service" > "${DEB_ROOT}/usr/lib/systemd/user/linuxdeck.service"
chmod 644 "${DEB_ROOT}/usr/lib/systemd/user/linuxdeck.service"

cp "${REPO_DIR}/udev/70-linuxdeck.rules" "${DEB_ROOT}/lib/udev/rules.d/70-linuxdeck.rules"
chmod 644 "${DEB_ROOT}/lib/udev/rules.d/70-linuxdeck.rules"

INSTALLED_KB="$(du -sk "${DEB_ROOT}/usr" "${DEB_ROOT}/lib" | awk '{s+=$1} END {print s}')"

cat > "${DEB_ROOT}/DEBIAN/control" <<EOF
Package: linuxdeck
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: amd64
Installed-Size: ${INSTALLED_KB}
Maintainer: StreamSG <mccarthyaaron0604@gmail.com>
Homepage: https://github.com/StreamSG/LinuxDeck
Description: Open-Source Loupedeck & Razer Stream Controller Suite for Linux
 LinuxDeck provides a low-latency USB serial driver, automatic host PC hardware
 and software discovery engine, PipeWire/PulseAudio mixer, OBS Studio WebSocket
 v5 controller, MPRIS2 media/macro launcher, live system telemetry gauges, and
 a PyQt6 Control Center for Loupedeck Live and Razer Stream Controller devices.
EOF

cat > "${DEB_ROOT}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
    udevadm trigger --action=add --subsystem-match=tty || true
    udevadm trigger --action=add --subsystem-match=usb || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
exit 0
EOF
chmod 755 "${DEB_ROOT}/DEBIAN/postinst"

dpkg-deb --build --root-owner-group "${DEB_ROOT}" "${DIST_DIR}/linuxdeck_${VERSION}_amd64.deb" >/dev/null

echo ""
echo "Release artifacts built successfully in ${DIST_DIR}:"
ls -lh "${DIST_DIR}"
