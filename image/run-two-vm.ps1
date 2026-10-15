<#
Boot the core VM and the sandbox VM on this Windows PC (QEMU + WHPX) with a private link between them.

  core:  NIC 1 (eth0) = user-mode NAT to the real network; NIC 2 (eth1, 10.77.0.1) = the private link
  sbx:   one NIC (eth0, 10.77.0.2) = the private link and nothing else

The link is a QEMU socket network between the two processes, so the sandbox has no path to anything but the core.
Console of each VM is VNC: core 127.0.0.1:5907, sbx 127.0.0.1:5908 (change with -CoreVnc / -SbxVnc).
Each start boots a fresh copy-on-write overlay of the images; the images themselves are never modified.

  powershell -File image\run-two-vm.ps1            start both
  powershell -File image\run-two-vm.ps1 -Stop      stop both and remove the overlays
#>
param(
  [string]$Core = (Join-Path $PSScriptRoot "..\build\packer-out\qemu\sbx-vm.qcow2"),
  [string]$Sbx = (Join-Path $PSScriptRoot "..\build\packer-out\sbxnode\sbx-node.qcow2"),
  [string]$Qemu = "C:\Program Files\qemu",
  [int]$CoreVnc = 7,
  [int]$SbxVnc = 8,
  [int]$LinkPort = 19777,
  [int]$CoreMb = 4096,
  [int]$SbxMb = 1024,
  [switch]$Stop
)
$ErrorActionPreference = "Stop"
$run = Join-Path $PSScriptRoot "..\build\run"
New-Item -ItemType Directory -Force $run | Out-Null
$run = (Resolve-Path $run).Path
$pidFile = Join-Path $run "pids.txt"

function Stop-Both {
  if (Test-Path $pidFile) {
    Get-Content $pidFile | ForEach-Object { Stop-Process -Id ([int]$_) -Force -ErrorAction SilentlyContinue }
    Remove-Item $pidFile
  }
  Start-Sleep 1
  Remove-Item (Join-Path $run "core.qcow2"), (Join-Path $run "sbx.qcow2") -ErrorAction SilentlyContinue
}

if ($Stop) { Stop-Both; "stopped"; return }
if (Test-Path $pidFile) { throw "already running (pids in $pidFile); use -Stop first" }
foreach ($img in $Core, $Sbx) { if (-not (Test-Path $img)) { throw "missing image: $img" } }
$Core = (Resolve-Path $Core).Path
$Sbx = (Resolve-Path $Sbx).Path

function New-Overlay($base, $name) {
  $o = Join-Path $run $name
  Remove-Item $o -ErrorAction SilentlyContinue
  & "$Qemu\qemu-img.exe" create -q -f qcow2 -F qcow2 -b $base $o | Out-Null
  $o
}

$common = @('-machine', 'type=pc,accel=whpx', '-cpu', 'Nehalem', '-smp', '2', '-display', 'none')
$coreOv = New-Overlay $Core "core.qcow2"
$sbxOv = New-Overlay $Sbx "sbx.qcow2"

# Core first: it listens for the link; the sandbox connects to it.
$c = Start-Process "$Qemu\qemu-system-x86_64.exe" -PassThru -WindowStyle Hidden -ArgumentList ($common + @(
    '-m', $CoreMb, '-drive', "file=$coreOv,if=virtio",
    '-netdev', 'user,id=wan', '-device', 'virtio-net-pci,netdev=wan,mac=52:54:00:77:00:10',
    '-netdev', "socket,id=lan,listen=127.0.0.1:$LinkPort", '-device', 'virtio-net-pci,netdev=lan,mac=52:54:00:77:00:01',
    '-vnc', "127.0.0.1:$CoreVnc"))
Start-Sleep 2
$s = Start-Process "$Qemu\qemu-system-x86_64.exe" -PassThru -WindowStyle Hidden -ArgumentList ($common + @(
    '-m', $SbxMb, '-drive', "file=$sbxOv,if=virtio",
    '-netdev', "socket,id=lan,connect=127.0.0.1:$LinkPort", '-device', 'virtio-net-pci,netdev=lan,mac=52:54:00:77:00:02',
    '-vnc', "127.0.0.1:$SbxVnc"))
@($c.Id, $s.Id) | Set-Content $pidFile
"core pid $($c.Id)  VNC 127.0.0.1:$(5900 + $CoreVnc)"
"sbx  pid $($s.Id)  VNC 127.0.0.1:$(5900 + $SbxVnc)"
