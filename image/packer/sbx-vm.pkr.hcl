# sbx VM image: Debian 13 (trixie, amd64) built with Packer.
# STATUS: UNTESTED. Written without running Packer. Run `packer validate` and a real build before trusting it.
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
    error_message = "iso_url is required (DEBIAN_ISO_URL in image/manifest.env)."
  }
}

variable "iso_checksum" {
  type        = string
  default     = ""
  description = "sha256:<hex> of the ISO. Set from DEBIAN_ISO_SHA256 in image/manifest.env."
  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.iso_checksum))
    error_message = "iso_checksum must be sha256:<64 lowercase hex chars>."
  }
}

variable "build_password" {
  type        = string
  sensitive   = true
  description = "Build-time password for sbxadmin (used for ssh and sudo during the build). Locked in cleanup."
  validation {
    condition     = can(regex("^[A-Za-z0-9]{12,64}$", var.build_password))
    error_message = "build_password must be 12-64 letters/digits (it is typed as keystrokes at the boot prompt)."
  }
}

variable "ssh_authorized_key" {
  type        = string
  default     = ""
  description = "Public key installed for sbxadmin. Required: the shipped image has password login disabled."
  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) ", var.ssh_authorized_key))
    error_message = "ssh_authorized_key must be a single OpenSSH public key line."
  }
}

variable "accelerator" {
  type        = string
  default     = "kvm"
  description = "qemu accelerator: kvm (Linux host), whpx (Windows host), or tcg (software, slow)."
  validation {
    condition     = contains(["kvm", "whpx", "tcg"], var.accelerator)
    error_message = "accelerator must be one of: kvm, whpx, tcg."
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
  ]

  # Build steps run as root via sudo, password on stdin. {{ .Vars }} passes environment_vars through.
  sudo_cmd = "echo '${var.build_password}' | {{ .Vars }} sudo -S -E bash '{{ .Path }}'"
}

# ---------------------------------------------------------------- sources

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

  iso_url      = var.iso_url
  iso_checksum = var.iso_checksum

  http_directory = local.http_dir
  boot_wait      = "10s"
  boot_command   = concat(local.boot_base, ["<enter>"])

  communicator             = "ssh"
  ssh_username             = local.ssh_user
  ssh_password             = var.build_password
  ssh_timeout              = "60m"
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
  name    = "sbx-vm"
  sources = ["source.qemu.sbx", "source.virtualbox-iso.sbx_ova"]

  # Stage the repo. image/ must contain artifacts/{agent-desk,tcs-shim,tcs-verify}.
  provisioner "shell" {
    inline = ["set -e", "mkdir -p /tmp/repo"]
  }

  provisioner "file" {
    source      = "${local.repo}/console"
    destination = "/tmp/repo/console"
  }

  provisioner "file" {
    source      = "${local.repo}/harness"
    destination = "/tmp/repo/harness"
  }

  provisioner "file" {
    source      = "${local.repo}/sbx"
    destination = "/tmp/repo/sbx"
  }

  provisioner "file" {
    source      = "${local.repo}/common"
    destination = "/tmp/repo/common"
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
    destination = "/tmp/repo/image"
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
      ExecStart=/usr/bin/ssh-keygen -A
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

      # 6) Lock the build password. After this, sbxadmin can only log in with its key, and cannot sudo with a password.
      passwd -l sbxadmin
      sync

      # 7) Power off 20 s from now, after this provisioner has returned to Packer.
      systemd-run --on-active=20s --unit=sbx-build-poweroff /bin/systemctl poweroff
      EOT
    ]
  }

  # SHA-256 manifest for each produced artifact (qcow2 and, if built, the OVA).
  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "${local.out_dir}/{{.BuildName}}-{{.BuildType}}.{{.ChecksumType}}"
  }
}
