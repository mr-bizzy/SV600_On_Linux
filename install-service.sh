#!/usr/bin/env bash
#
# install-service.sh — run the SV600 button watcher as a systemd USER service.
#
# Usage:  ./install-service.sh            # install (and show what to do next)
#         ./install-service.sh --uninstall
#
# No sudo, and no udev rule: this machine already exposes the SV600 as
# crw-rw-rw- via /etc/udev/rules.d/99-vfio.rules (added for VM passthrough), and
# open_dev() was verified to claim the interface as an ordinary user. If that
# rule is ever removed, access can be restored with:
#     echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="04c5", ATTR{idProduct}=="128e", \
#           MODE="0660", GROUP="scanner", TAG+="uaccess"' \
#       | sudo tee /etc/udev/rules.d/60-scansnap-sv600.rules
#     sudo usermod -aG scanner "$USER" && sudo udevadm control --reload
#
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PYTHON="$(command -v python3)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/sv600-watch.service"
CONF_DIR="$HOME/.config/sv600"
CONF="$CONF_DIR/watch.conf"

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now sv600-watch.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo ">> Removed $UNIT (config left at $CONF)"
  exit 0
fi

[[ -f "$DIR/sv600_watch.py" ]] || { echo "sv600_watch.py not found beside this script" >&2; exit 1; }

mkdir -p "$UNIT_DIR" "$CONF_DIR"

# Config is only written once, so re-running the installer never clobbers your
# settings.
if [[ ! -f "$CONF" ]]; then
  mkdir -p "$HOME/Scans"
  cat > "$CONF" <<EOF
# Options for the SV600 button watcher. Edit, then:
#     systemctl --user restart sv600-watch
#
# Everything sv600_watch.py accepts works here — run
#     python3 $DIR/sv600_watch.py --help
# for the full list. Common choices:
#
#   --format png|jpeg|tiff|pdf|pdf-ocr    --color color|gray|bw
#   --dpi 300|600                          --pages N     --join
#   --batch SEC        successive presses become ONE document
#   --prefix STR       filename prefix
#
#   --prompt           after each page, a dialog offers Continue / Finish &
#                      Save. Pressing the scan button adds the next page and
#                      replaces the dialog, so you only ever click to FINISH.
#                      With --prompt, --batch is just a safety net for a
#                      document you walked away from.
SV600_ARGS="--outdir $HOME/Scans --format pdf-ocr --color gray --batch 300 --prompt"
EOF
  echo ">> Wrote default config: $CONF"
else
  echo ">> Kept existing config:  $CONF"
fi

sed -e "s|@DIR@|$DIR|g" -e "s|@PYTHON@|$PYTHON|g" \
    "$DIR/sv600-watch.service.in" > "$UNIT"
echo ">> Wrote unit:            $UNIT"

systemctl --user daemon-reload

# --prompt needs the graphical session's DISPLAY/XAUTHORITY. A systemd user
# service does not inherit them unless the session has exported them into the
# user manager, which not every desktop does. Doing it here means the prompt
# works on this login; the service degrades to the --batch timeout otherwise
# (and says so in the log) rather than failing.
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY 2>/dev/null || true
  echo ">> Imported DISPLAY into the user manager (needed by --prompt)"
fi

echo
echo "=================================================================="
echo " Next steps"
echo "=================================================================="
cat <<EOF
  systemctl --user enable --now sv600-watch     # start it, and at login
  systemctl --user status sv600-watch
  journalctl --user -u sv600-watch -f           # watch it scan

  systemctl --user restart sv600-watch          # after editing the config
  systemctl --user stop sv600-watch             # BEFORE passing the scanner
                                                # to the Windows VM — the
                                                # daemon holds the USB
                                                # interface while it runs

To keep it running when you are not logged in:
  sudo loginctl enable-linger $USER
EOF
