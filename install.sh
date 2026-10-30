#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${REPO_DIR}/.venv"
LOCAL_BIN="${HOME}/.local/bin"
APPS_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

echo "============================================================"
echo "  LinuxDeck Installer — Open-Source Loupedeck Suite for Linux"
echo "============================================================"

# 1. Create Python virtual environment
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[1/5] Creating Python virtual environment at ${VENV_DIR}..."
    python3 -m venv --system-site-packages "${VENV_DIR}"
else
    echo "[1/5] Using existing Python virtual environment at ${VENV_DIR}..."
fi

echo "[2/5] Installing LinuxDeck package and Python dependencies..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "${REPO_DIR}"

# 2. Install ~/.local/bin/linuxdeck CLI launcher
echo "[3/5] Installing CLI launcher to ${LOCAL_BIN}/linuxdeck..."
mkdir -p "${LOCAL_BIN}"
cat > "${LOCAL_BIN}/linuxdeck" <<EOF
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/python" -m linuxdeck "\$@"
EOF
chmod +x "${LOCAL_BIN}/linuxdeck"

# 3. Install desktop entry, icon, and systemd user unit
echo "[4/5] Installing desktop entry, icon, and systemd user service..."
mkdir -p "${APPS_DIR}" "${ICON_DIR}" "${SYSTEMD_USER_DIR}"
cp "${REPO_DIR}/packaging/linuxdeck.svg" "${ICON_DIR}/linuxdeck.svg"

sed "s|^Exec=.*|Exec=${LOCAL_BIN}/linuxdeck|g; s|^Icon=.*|Icon=${ICON_DIR}/linuxdeck.svg|g" \
    "${REPO_DIR}/packaging/linuxdeck.desktop" > "${APPS_DIR}/linuxdeck.desktop"
chmod +x "${APPS_DIR}/linuxdeck.desktop"

cp "${REPO_DIR}/packaging/linuxdeck.service" "${SYSTEMD_USER_DIR}/linuxdeck.service"
systemctl --user daemon-reload >/dev/null 2>&1 || true

# 4. Install udev rules if not already present
if [[ -f "/etc/udev/rules.d/70-linuxdeck.rules" || -f "/etc/udev/rules.d/70-razer-stream-controller.rules" ]]; then
    echo "[5/5] udev rules already present in /etc/udev/rules.d/."
else
    echo "[5/5] Installing udev rules (requires root privileges)..."
    if command -v sudo >/dev/null 2>&1; then
        sudo "${REPO_DIR}/scripts/install-udev.sh"
    elif command -v pkexec >/dev/null 2>&1; then
        pkexec "${REPO_DIR}/scripts/install-udev.sh"
    else
        echo "WARNING: Could not find sudo or pkexec. Run manually as root:"
        echo "  sudo ${REPO_DIR}/scripts/install-udev.sh"
    fi
fi

# 5. Generate initial auto-configured profile if none exists
if [[ ! -f "${HOME}/.config/linuxdeck/config.json" ]]; then
    echo ""
    echo "Running first-time PC hardware & software auto-discovery..."
    "${LOCAL_BIN}/linuxdeck" --autoconfig-only
fi

echo ""
echo "LinuxDeck installation complete!"
echo "  • Run GUI & daemon:         linuxdeck"
echo "  • Re-scan PC & auto-config: linuxdeck --autoconfig"
echo "  • Run headless daemon:      linuxdeck --headless"
echo "  • Enable background autostart:"
echo "      systemctl --user enable --now linuxdeck.service"
