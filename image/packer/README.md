# sbx VM image (Packer), UNTESTED

This template has not been run or validated. Expect to fix something on the first build.

Needs: Packer >= 1.10, qemu-system-x86_64 (kvm on Linux, whpx on Windows), about 60 GB free, network access, and `image/artifacts/deskbox-linux-amd64` present. Pins live in `image/manifest.env`.

Build, from `image/packer`:
1. Write the ISO vars: `. ../manifest.env && printf 'iso_url = "%s"\niso_checksum = "sha256:%s"\n' "$DEBIAN_ISO_URL" "$DEBIAN_ISO_SHA256" > sbx.auto.pkrvars.hcl`
2. Set secrets: `export PKR_VAR_build_password='<12+ letters/digits>'` and `export PKR_VAR_ssh_authorized_key="$(cat ~/.ssh/id_ed25519.pub)"`
3. `packer init . && packer validate . && packer build -force .` (add `-only='sbx-vm.qemu.sbx'` to skip the optional OVA source)
4. Output: `build/packer-out/qemu/sbx-vm.qcow2` plus a `sbx-vm-qemu.sha256` checksum file.

First boot:
1. Boot the qcow2 and log in as `sbxadmin` with your key only (password login is disabled and the build password is locked).
2. The first boot regenerates SSH host keys and machine-id; `firstboot-warning` prints its notice in the banner.
3. Finish by hand: add the sbx forced-command key and set `SBX_HOST=127.0.0.1` in `/etc/sbx/broker.env` (lite mode, per `provision.sh`).
