# sbx VM image: Debian 13 (trixie, amd64) built with Packer.
# STATUS: built and booted on Windows (QEMU+WHPX), smoke test 16/16. See README.md for the two-stage flow.
#
# Run from image/packer. See README.md.
# Secrets come from the environment, never from this file:
#   PKR_VAR_build_password        (12-64 alphanumeric chars; sbxadmin's build-time password)
#   PKR_VAR_ssh_authorized_key    (the operator's public key; the shipped image is key-only)

packer {
  required_version = ">= 1.10.0"
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = ">= 1.0.0"
    }
    # Only used by the optional OVA source (sbx_ova). Remove this block if you never make an OVA.
    virtualbox = {
      source  = "github.com/hashicorp/virtualbox"
      version = ">= 1.0.0"
    }
  }
}

# ---------------------------------------------------------------- variables

variable "iso_url" {
  type        = string
  default     = ""
  description = "Debian 13 netinst ISO URL. Set from DEBIAN_ISO_URL in image/manifest.env via a var-file."
  validation {
    condition     = length(var.iso_url) > 0
    error_message = "Iso_url is required (DEBIAN_ISO_URL in image/manifest.env)."
  }
}

variable "iso_checksum" {
  type        = string
  default     = ""
  description = "sha256:<hex> of the ISO. Set from DEBIAN_ISO_SHA256 in image/manifest.env."
  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.iso_checksum))
    error_message = "Iso_checksum must be sha256:<64 lowercase hex chars>."
  }
}

variable "build_password" {
  type        = string
  sensitive   = true
  description = "Build-time password for sbxadmin (used for ssh and sudo during the build). Locked in cleanup."
  validation {
    condition     = can(regex("^[A-Za-z0-9]{12,64}$", var.build_password))
    error_message = "Build_password must be 12-64 letters/digits (it is typed as keystrokes at the boot prompt)."
  }
}

variable "ssh_authorized_key" {
  type        = string
  default     = ""
  description = "Public key installed for sbxadmin. Required: the shipped image has password login disabled."
  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) ", var.ssh_authorized_key))
    error_message = "Ssh_authorized_key must be a single OpenSSH public key line."
  }
}

variable "base_image" {
  type        = string
  default     = ""
  description = "Path to the installed-Debian qcow2 made by the 'base' build. The sbx-vm build provisions on top of it."
}

variable "accelerator" {
  type        = string
  default     = "kvm"
  description = "qemu accelerator: kvm (Linux host), whpx (Windows host), or tcg (software, slow)."
  validation {
    condition     = contains(["kvm", "whpx", "tcg"], var.accelerator)
    error_message = "Accelerator must be one of: kvm, whpx, tcg."
  }
}

variable "disk_size_gb" {
  type    = number
  default = 40
}

variable "memory_mb" {
  type    = number
  default = 4096
}

variable "cpus" {
  type    = number
  default = 4
}

variable "zero_free_space" {
  type        = bool
  default     = false
  description = "Fill free space with zeros before shutdown so the qcow2 compresses better. Slow."
}

# ---------------------------------------------------------------- locals

locals {
  ssh_user = "sbxadmin" # must match passwd/username in http/preseed.cfg
  repo     = "${path.root}/../.."
  http_dir = "${path.root}/http"
  out_dir  = "${path.root}/../../build/packer-out"

  # Kernel args for the unattended install. The password goes here rather than in preseed.cfg,
  # so it never sits in the served file. Typed as keystrokes, so it must stay alphanumeric.
  boot_base = [
    "<esc><wait>",
    "/install.amd/vmlinuz initrd=/install.amd/initrd.gz ",
    "auto=true priority=critical ",
    "preseed/url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/preseed.cfg ",
    "passwd/user-password=${var.build_password} ",
    "passwd/user-password-again=${var.build_password} ",
    "locale=en_US.UTF-8 keymap=us ",
    # Stable NIC name (eth0) on every machine type: stage 1 installs on q35, stage 2 boots on pc, and the
    # predictable names differ between them, which left the installed system with no network config.
    "net.ifnames=0 biosdevname=0 ",
  ]

  # Build steps run as root via sudo, password on stdin. {{ .Vars }} passes environment_vars through.
  sudo_cmd = "echo '${var.build_password}' | {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
}

# ---------------------------------------------------------------- sources

# Stage 1: unattended Debian install only. The installer powers the VM off when done, so no SSH is needed.
# q35 + kernel-irqchip=off is what the WHPX installer tolerates; the installed system boots fine under pc (stage 2).
source "qemu" "install" {
  vm_name          = "base.qcow2"
  output_directory = "${local.out_dir}/base"
  format           = "qcow2"
  disk_size        = "${var.disk_size_gb}G"
  disk_interface   = "virtio"
  net_device       = "virtio-net"
  memory           = var.memory_mb
  cpus             = var.cpus
  accelerator      = var.accelerator
  headless         = true
  qemuargs = var.accelerator == "whpx" ? [
    ["-machine", "type=q35,accel=whpx,kernel-irqchip=off"],
    ["-cpu", "qemu64"],
    ["-smp", "2"],
  ] : []

  iso_url      = var.iso_url
  iso_checksum = var.iso_checksum

  http_directory = local.http_dir
  boot_wait      = "10s"
  boot_command   = concat(local.boot_base, ["<enter>"])

  communicator     = "none"
  shutdown_timeout = "90m"
}

# Stage 2: boot the installed base disk, provision over SSH, clean up. No installer, no ISO.
# WHPX (Windows): the defaults (4 vCPUs, host-ish CPU) triple-faulted ("Unexpected VP exit code 4").
# Verified: pc + qemu64 + 2 vCPUs boots the installed Debian 13 to a login prompt.
source "qemu" "sbx" {
  vm_name          = "sbx-vm.qcow2"
  output_directory = "${local.out_dir}/qemu"
  format           = "qcow2"
  disk_size        = "${var.disk_size_gb}G"
  disk_compression = true
  disk_interface   = "virtio"
  net_device       = "virtio-net"
  memory           = var.memory_mb
  cpus             = var.cpus
  accelerator      = var.accelerator
  headless         = true
  qemuargs = var.accelerator == "whpx" ? [
    ["-machine", "type=pc,accel=whpx"],
    ["-cpu", "Nehalem"],
    ["-smp", "2"],
  ] : []

  disk_image       = true
  use_backing_file = false
  iso_url          = var.base_image
  iso_checksum     = "none"
  boot_wait        = "5s"

  communicator             = "ssh"
  ssh_username             = local.ssh_user
  ssh_password             = var.build_password
  ssh_timeout              = "30m"
  ssh_file_transfer_method = "sftp"
  # Cleanup schedules the poweroff 20 s after it returns, so this is a no-op and Packer waits for the VM to exit.
  shutdown_command = "true"
  shutdown_timeout = "15m"
}

# ALTERNATIVE, optional: produces an OVA through VirtualBox. Build only this one with
#   packer build -only='sbx-vm.virtualbox-iso.sbx_ova' .
# The installer disk is /dev/sda (SATA) here, so the boot command overrides the preseed's /dev/vda.
source "virtualbox-iso" "sbx_ova" {
  vm_name              = "sbx-vm"
  output_directory     = "${local.out_dir}/ova"
  format               = "ova"
  guest_os_type        = "Debian_64"
  disk_size            = var.disk_size_gb * 1024 # VirtualBox wants MiB
  memory               = var.memory_mb
  cpus                 = var.cpus
  headless             = true
  guest_additions_mode = "disable"
  hard_drive_interface = "sata"

  iso_url      = var.iso_url
  iso_checksum = var.iso_checksum

  http_directory = local.http_dir
  boot_wait      = "10s"
  boot_command   = concat(local.boot_base, ["partman-auto/disk=/dev/sda grub-installer/bootdev=/dev/sda ", "<enter>"])

  communicator             = "ssh"
  ssh_username             = local.ssh_user
  ssh_password             = var.build_password
  ssh_timeout              = "60m"
  ssh_file_transfer_method = "sftp"
  shutdown_command         = "true"
  shutdown_timeout         = "15m"
}

# ---------------------------------------------------------------- build

build {
  name    = "base"
  sources = ["source.qemu.install"]
}

build {
  name    = "sbx-vm"
  sources = ["source.qemu.sbx", "source.virtualbox-iso.sbx_ova"]

  # Stage the repo. image/ must contain artifacts/{agent-desk,tcs-shim,tcs-verify}.
  provisioner "shell" {
    inline = ["set -e", "mkdir -p /tmp/repo"]
  }

  provisioner "file" {
    source      = "${local.repo}/console"
    destination = "/tmp/repo/" # directory source without trailing slash lands INSIDE destination
  }

  provisioner "file" {
    source      = "${local.repo}/harness"
    destination = "/tmp/repo/" # directory source without trailing slash lands INSIDE destination
  }

  provisioner "file" {
    source      = "${local.repo}/sbx"
    destination = "/tmp/repo/" # directory source without trailing slash lands INSIDE destination
  }

  provisioner "file" {
    source      = "${local.repo}/common"
    destination = "/tmp/repo/" # directory source without trailing slash lands INSIDE destination
  }

  provisioner "file" {
    source      = "${local.repo}/tools.list"
    destination = "/tmp/repo/tools.list"
  }

  provisioner "file" {
    source      = "${local.repo}/sync.py"
    destination = "/tmp/repo/sync.py"
  }

  provisioner "file" {
    source      = "${local.repo}/image"
    destination = "/tmp/repo/" # directory source without trailing slash lands INSIDE destination
  }

  # Base system, Ollama, Loom, DeskBox, Pi (build-pi.sh), sbx bridge and lite-mode SSH (setup-lite.sh), all via provision.sh.
  provisioner "shell" {
    execute_command = local.sudo_cmd
    inline = [
      "set -e",
      "bash /tmp/repo/image/provision.sh",
    ]
  }

  # Boot-time warning and login banners, from image/ (written by others; see those files).
  provisioner "shell" {
    execute_command = local.sudo_cmd
    inline = [
      "set -e",
      "install -m 0644 /tmp/repo/image/issue.txt /etc/issue",
      "install -m 0644 /tmp/repo/image/motd.txt /etc/motd",
      "install -m 0755 /tmp/repo/image/firstboot-warning.sh /usr/local/sbin/firstboot-warning.sh",
      "install -m 0644 /tmp/repo/image/firstboot-warning.service /etc/systemd/system/firstboot-warning.service",
      "systemctl daemon-reload",
      "systemctl enable firstboot-warning.service",
    ]
  }

  # Final cleanup: operator key, key-only sshd, host keys and machine-id regenerated on first boot,
  # caches and build traces removed, build password locked, then power off.
  provisioner "shell" {
    execute_command = local.sudo_cmd
    environment_vars = [
      "SBX_AUTHKEY=${var.ssh_authorized_key}",
      "SBX_ZERO=${var.zero_free_space ? "1" : "0"}",
    ]
    inline = [<<-EOT
      set -e

      # 1) Operator key, then keys-only sshd. sshd -t checks the config before we rely on it.
      install -d -m 700 -o sbxadmin -g sbxadmin /home/sbxadmin/.ssh
      echo "$SBX_AUTHKEY" > /home/sbxadmin/.ssh/authorized_keys
      chown sbxadmin:sbxadmin /home/sbxadmin/.ssh/authorized_keys
      chmod 600 /home/sbxadmin/.ssh/authorized_keys
      echo 'PasswordAuthentication no' > /etc/ssh/sshd_config.d/10-sbx-keys-only.conf
      echo 'KbdInteractiveAuthentication no' >> /etc/ssh/sshd_config.d/10-sbx-keys-only.conf
      echo 'PermitRootLogin no' >> /etc/ssh/sshd_config.d/10-sbx-keys-only.conf
      sshd -t

      # 2) SSH host keys: delete now, regenerate once on first boot before ssh starts.
      rm -f /etc/ssh/ssh_host_*
      cat > /etc/systemd/system/sbx-ssh-hostkeys.service <<'EOF'
      [Unit]
      Description=Generate SSH host keys on first boot
      Before=ssh.service
      ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

      [Service]
      Type=oneshot
      ExecStart=/usr/local/sbin/sbx-regen-hostkeys
      RemainAfterExit=yes

      [Install]
      WantedBy=multi-user.target
      EOF
      systemctl enable sbx-ssh-hostkeys.service

      # 3) machine-id: empty, so systemd generates a fresh one on first boot.
      truncate -s 0 /etc/machine-id
      rm -f /var/lib/dbus/machine-id

      # 4) Caches, logs, and build traces.
      apt-get clean
      rm -rf /var/lib/apt/lists/* /tmp/repo /tmp/sbx-*
      find /var/log -type f -exec truncate -s 0 {} +
      rm -f /var/cache/debconf/passwords.dat /root/.bash_history /home/sbxadmin/.bash_history

      # 5) Optional: zero free space (slow; off by default).
      if [ "$SBX_ZERO" = 1 ]; then dd if=/dev/zero of=/zero.fill bs=1M status=none || true; rm -f /zero.fill; fi

      # 6) Replace the random build password with a documented one that MUST be changed at first console login
      #    (expired). Nothing on the network can use it: sshd only listens on 127.0.0.1 and only admits user `sbx`.
      echo 'sbxadmin:sbx-change-me' | chpasswd
      passwd -e sbxadmin
      sync

      # 7) Power off 20 s from now, after this provisioner has returned to Packer.
      systemd-run --on-active=20s --unit=sbx-build-poweroff /bin/systemctl poweroff
      EOT
    ]
  }

  # SHA-256 manifest for each produced artifact (qcow2 and, if built, the OVA).
  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "${local.out_dir}/{{.BuildName}}-{{.BuilderType}}.{{.ChecksumType}}"
  }
}
