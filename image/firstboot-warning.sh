#!/bin/sh
# One-time first-boot step: install the warning banners and show the warning on the console.
# Installed by image/setup-lite.sh to /usr/local/sbin; banner texts live in /usr/local/share/sbx.
# The marker is written last, so a failed run retries on the next boot.
set -eu

SRC=/usr/local/share/sbx
MARK_DIR=/var/lib/sbx
MARK="$MARK_DIR/warning-shown"

[ -e "$MARK" ] && exit 0

install -m 644 "$SRC/issue.txt" /etc/issue
install -m 644 "$SRC/motd.txt" /etc/motd

for t in /dev/tty1 /dev/console; do
  if [ -w "$t" ]; then
    cat "$SRC/issue.txt" >"$t" 2>/dev/null || true
  fi
done

install -d -m 755 "$MARK_DIR"
: >"$MARK"
