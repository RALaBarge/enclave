#!/bin/sh
# One-time, at the sandbox box's keyboard: give it the "install" verb, so the core pushes tools from then on
# (sbx-push-tools). Served by the core's sbx-bootstrap-serve; typed on the box as:
#   curl -fsS 10.77.0.1:8000/b.sh | sudo sh
# Last step asks the core for /done, so whoever runs the server sees it finish without a screen on the box.
set -eu
U=http://10.77.0.1:8000
cd "$(mktemp -d)"
for f in sbx-channel sbx-run sbx-install.py smoke-test-sbx.sh toollist.py; do curl -fsSO "$U/$f"; done
install -m 755 sbx-channel /opt/sbx/sbx-channel
install -m 644 toollist.py /opt/sbx/toollist.py
install -m 755 sbx-run /opt/sbx/sbx-run
install -d -m 700 -o sbx -g sbx /var/lib/sbx-work
install -m 755 sbx-install.py /usr/local/sbin/sbx-install
install -m 755 smoke-test-sbx.sh /usr/local/sbin/sbx-node-test
printf '%s\n' 'Defaults:sbx !requiretty' 'sbx ALL=(root) NOPASSWD: /usr/local/sbin/sbx-reset, /usr/local/sbin/sbx-install' >sudoers
visudo -cf sudoers && install -m 440 sudoers /etc/sudoers.d/sbx-reset
echo "bootstrap done"
curl -fsS -o /dev/null "$U/done" 2>/dev/null || true
