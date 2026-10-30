#!/usr/bin/env bash
set -euo pipefail

echo "Uninstalling LinuxDeck user integration files..."
systemctl --user disable --now linuxdeck.service >/dev/null 2>&1 || true
rm -f "${HOME}/.config/systemd/user/linuxdeck.service"
systemctl --user daemon-reload >/dev/null 2>&1 || true

rm -f "${HOME}/.local/bin/linuxdeck"
rm -f "${HOME}/.local/share/applications/linuxdeck.desktop"
rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/linuxdeck.svg"

echo "LinuxDeck user files removed."
echo "Note: Your profile configuration in ~/.config/linuxdeck/config.json was preserved."
echo "To remove udev rules as well: sudo rm -f /etc/udev/rules.d/70-linuxdeck.rules"
