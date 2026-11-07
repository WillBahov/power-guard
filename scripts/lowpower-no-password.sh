#!/bin/bash
# power-guard: let the guard flip Low Power Mode without a password prompt.
#
# Installs a narrowly scoped sudoers rule that permits ONLY these two exact
# commands for the invoking user, and nothing else:
#     /usr/bin/pmset -a lowpowermode 0
#     /usr/bin/pmset -a lowpowermode 1
#
# Run once:   sudo bash scripts/lowpower-no-password.sh
# Undo:       sudo rm /etc/sudoers.d/power-guard
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo bash \"$0\""
  exit 1
fi

USER_NAME="${SUDO_USER:-$(logname 2>/dev/null)}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
  echo "Could not determine your username. Aborting."
  exit 1
fi

TMP="$(mktemp)"
cat > "$TMP" <<EOF
# Installed by power-guard. Allows ONLY these two commands without a password.
# Remove with: sudo rm /etc/sudoers.d/power-guard
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/pmset -a lowpowermode 0
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/pmset -a lowpowermode 1
EOF

# Validate before installing: a malformed sudoers file can lock you out of sudo.
if ! visudo -cf "$TMP" >/dev/null 2>&1; then
  echo "Validation failed; nothing was changed."
  rm -f "$TMP"
  exit 1
fi

install -m 0440 -o root -g wheel "$TMP" /etc/sudoers.d/power-guard
rm -f "$TMP"

echo "Done. power-guard can now toggle Low Power Mode without a password."
echo "Undo: sudo rm /etc/sudoers.d/power-guard"
