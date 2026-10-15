# Enclave VM images (Packer + QEMU)

Built and booted on Windows 11 with QEMU 11.1 + WHPX acceleration; the booted image passes `sbx-smoke-test`
(16/16). Not yet tried: Linux/KVM hosts, the VirtualBox/OVA source, UEFI.

## Two stages

1. **base**: unattended Debian 13 install (`q35`), powers itself off. About 4 minutes.
2. **sbx-vm**: boots that disk (`pc`), uploads the repo, runs `image/provision.sh`, cleans up, writes
   `build/packer-out/qemu/sbx-vm.qcow2` and `build/packer-out/sbx-qemu.sha256`. About 5 minutes.

They differ on purpose. Under WHPX the installer only runs on `q35` + `kernel-irqchip=off`, but that setup hangs
GRUB on the installed system, which boots fine on `pc`. Stage 2 also needs `-cpu Nehalem`: with the default `qemu64`
model Pi's binary hung for an hour.

## Build

Needs Packer >= 1.10, QEMU, about 20 GB free, network. DeskBox binaries must exist in `image/artifacts/`
(see `image/BUILD-DESKBOX.md`). Pins live in `image/manifest.env`.

```powershell
cd image\packer
$env:PACKER_CACHE_DIR = "$env:TEMP\packer_cache"      # MUST be outside the repo: image/ is uploaded into the VM
$env:PKR_VAR_build_password = "<12-64 letters/digits>"
$env:PKR_VAR_ssh_authorized_key = (Get-Content ~\.ssh\id_ed25519.pub -Raw).Trim()
# sbx.auto.pkrvars.hcl: iso_url, iso_checksum (from manifest.env) and accelerator = "whpx" | "kvm" | "tcg"
packer init .
packer build -force -only='base.qemu.install' -var base_image=x .
packer build -force -only='sbx-vm.qemu.sbx' -var "base_image=$((Resolve-Path ..\..\build\packer-out\base\base.qcow2).Path)" .
```

`tcg` (software) works but is roughly 50x slower; the install alone takes over an hour.

## The sandbox box (second image, for the two-VM layout)

`bash image/gen-keys.sh` once, then build it from the same base disk. It takes under two minutes and comes out around 700 MB, with no Loom,
Ollama, Pi, DeskBox, broker key or console in it:

```powershell
packer build -force -only='sbx-node.qemu.sbxnode' -var "base_image=<path to base.qcow2>" .
# -> build/packer-out/sbxnode/sbx-node.qcow2
```

Run both with `image\run-two-vm.ps1` (see `image/SETUP.md`, Full mode). The core build needs `build/keys/core` and this one needs
`build/keys/sbx`, both made by `gen-keys.sh`.

## First boot

Console login `sbxadmin` / `sbx-change-me`, forced to change on first login. sshd listens on 127.0.0.1 only and
admits only the `sbx` forced-command user, so that password is not usable over the network. SSH host keys and
machine-id are regenerated on first boot, and the broker's pinned host key is refreshed by `sbx-regen-hostkeys`.
Then run `sudo sbx-smoke-test`.
