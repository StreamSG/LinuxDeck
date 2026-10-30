#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE_SRC="${SCRIPT_DIR}/../udev/70-linuxdeck.rules"
RULE_DST="/etc/udev/rules.d/70-linuxdeck.rules"

echo "Installing LinuxDeck udev rules to ${RULE_DST}..."
cp "${RULE_SRC}" "${RULE_DST}"
chmod 644 "${RULE_DST}"

udevadm control --reload-rules
udevadm trigger --action=add --subsystem-match=tty
udevadm trigger --action=add --subsystem-match=usb

echo "udev rules installed and triggered successfully."
