<#
Enclave launcher: one menu to check, build, start, stop and reset the two VMs on this PC.

  enclave.cmd                      the menu
  enclave.cmd status|start|stop    one action, no menu
  enclave.cmd build sbx|core|all   build images without the menu

The core VM keeps one persistent disk (build\run\core.qcow2) layered on the built image, so its password, keys and Loom
data survive restarts. The sandbox VM gets a fresh disk every start. Rebuilding the core image makes the core disk stale
(Status says so); Reset throws the disk away and starts again from the new image.
#>
param([ValidateSet("", "status", "start", "stop", "build")][string]$Do = "", [ValidateSet("sbx", "core", "all")][string]$What = "core")
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$out = "$root\build\packer-out"
$run = "$root\build\run"
$qemu = "C:\Program Files\qemu"
$base = @{ core = "$out\qemu\sbx-vm.qcow2"; sbx = "$out\sbxnode\sbx-node.qcow2" }
$disk = @{ core = "$run\core.qcow2"; sbx = "$run\sbx.qcow2" }
$stamp = "$run\core.base"            # size|mtime of the core image the core disk was made from
$sums = @{ core = "$out\sbx-qemu.sha256"; sbx = "$out\sbxnode-qemu.sha256" }
$linkPort = 19777; $monPort = 19778

function Stamp($p) { $f = Get-Item $p; "$($f.Length)|$($f.LastWriteTimeUtc.Ticks)" }
function Find-Vm($n) {
  Get-CimInstance Win32_Process -Filter "Name='qemu-system-x86_64.exe'" |
    Where-Object { $_.CommandLine -like "*\$n.qcow2*" } | Select-Object -First 1
}
function Find-Packer {   # winget installs it without always putting it on PATH
  $c = Get-Command packer -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter packer.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}
function Mb($p) { "{0:N0} MB" -f ((Get-Item $p).Length / 1MB) }

# ---- status: a list of (name, level, text); level is ok, warn or bad
function Get-Status {
  $s = New-Object Collections.ArrayList
  $add = { param($n, $l, $t) [void]$s.Add([pscustomobject]@{ Name = $n; Level = $l; Text = $t }) }
  $tool = (Test-Path "$qemu\qemu-system-x86_64.exe")
  & $add "QEMU" $(if ($tool) { "ok" } else { "bad" }) $(if ($tool) { $qemu } else { "not found in $qemu" })
  $pk = Find-Packer
  & $add "Packer" $(if ($pk) { "ok" } else { "warn" }) $(if ($pk) { $pk } else { "not found (only needed to build images)" })
  foreach ($n in "core", "sbx") {
    $label = if ($n -eq "core") { "Core image" } else { "Sandbox image" }
    if (Test-Path $base[$n]) { & $add $label "ok" "$(Mb $base[$n]), built $((Get-Item $base[$n]).LastWriteTime.ToString('yyyy-MM-dd HH:mm'))" }
    else { & $add $label "bad" "not built yet (press B)" }
  }
  if (-not (Test-Path $disk.core)) { & $add "Core disk" "ok" "none yet; created on first start" }
  elseif (-not (Test-Path $stamp) -or -not (Test-Path $base.core) -or (Get-Content $stamp -Raw).Trim() -ne (Stamp $base.core)) {
    & $add "Core disk" "bad" "made from a different image than the current one; Reset (R) to use the new image"
  } else { & $add "Core disk" "ok" "persistent, $(Mb $disk.core), matches the image" }
  foreach ($n in "core", "sbx") {
    $p = Find-Vm $n; $label = if ($n -eq "core") { "Core VM" } else { "Sandbox VM" }
    & $add $label $(if ($p) { "ok" } else { "warn" }) $(if ($p) { "running (pid $($p.ProcessId))" } else { "stopped" })
  }
  $s
}

# ---- actions
function New-Overlay($from, $to) {
  Remove-Item $to -ErrorAction SilentlyContinue
  & "$qemu\qemu-img.exe" create -q -f qcow2 -F qcow2 -b $from $to | Out-Null
  if ($LASTEXITCODE) { throw "qemu-img failed" }
}
function Start-Vm($name, $opts) {
  $p = Start-Process "$qemu\qemu-system-x86_64.exe" -PassThru -WindowStyle Hidden -ArgumentList (@('-machine', 'type=pc,accel=whpx', '-cpu', 'Nehalem', '-smp', '2', '-name', $name) + $opts)
  $p.Id
}
function Invoke-Start {
  foreach ($n in "core", "sbx") { if (-not (Test-Path $base[$n])) { throw "$n image is not built (press B)" } }
  if (Find-Vm "core") { throw "already running" }
  New-Item -ItemType Directory -Force $run | Out-Null
  if (-not (Test-Path $disk.core)) { New-Overlay $base.core $disk.core; Stamp $base.core | Set-Content $stamp }
  elseif ((Get-Content $stamp -Raw).Trim() -ne (Stamp $base.core)) { throw "core disk is stale; Reset (R) first" }
  New-Overlay $base.sbx $disk.sbx
  # The core has a window (its console, where you log in). window-close=off so closing it cannot power the VM off.
  $c = Start-Vm "enclave-core" @('-m', 4096, '-display', 'gtk,window-close=off', '-vnc', '127.0.0.1:7', '-drive', "file=$($disk.core),if=virtio",
    '-netdev', 'user,id=wan', '-device', 'virtio-net-pci,netdev=wan,mac=52:54:00:77:00:10',
    '-netdev', "socket,id=lan,listen=127.0.0.1:$linkPort", '-device', 'virtio-net-pci,netdev=lan,mac=52:54:00:77:00:01',
    '-monitor', "tcp:127.0.0.1:$monPort,server=on,wait=off")
  Start-Sleep 2
  $s = Start-Vm "enclave-sbx" @('-m', 1024, '-display', 'none', '-drive', "file=$($disk.sbx),if=virtio",
    '-netdev', "socket,id=lan,connect=127.0.0.1:$linkPort", '-device', 'virtio-net-pci,netdev=lan,mac=52:54:00:77:00:02')
  "started: core pid $c, sandbox pid $s"
}
function Invoke-Stop {
  $core = Find-Vm "core"
  if ($core) {   # ask the guest to power off (ACPI) so the persistent disk is closed cleanly
    try { $t = New-Object Net.Sockets.TcpClient("127.0.0.1", $monPort); $w = New-Object IO.StreamWriter($t.GetStream()); $w.WriteLine("system_powerdown"); $w.Flush(); Start-Sleep 1; $t.Close() } catch {}
    $gone = $false
    for ($i = 0; $i -lt 60 -and -not $gone; $i++) { Start-Sleep 1; $gone = -not (Find-Vm "core") }
    if (-not $gone) { Stop-Process -Id $core.ProcessId -Force; $note = " (core did not shut down in 60 s and was killed)" }
  }
  $sbx = Find-Vm "sbx"; if ($sbx) { Stop-Process -Id $sbx.ProcessId -Force }
  Start-Sleep 1; Remove-Item $disk.sbx -ErrorAction SilentlyContinue
  "stopped$note"
}
function Invoke-Reset {
  if (Find-Vm "core") { throw "stop the VMs first" }
  Remove-Item $disk.core, $stamp -ErrorAction SilentlyContinue
  "core disk removed; the next start builds a fresh one from the image"
}
function Invoke-Verify {
  foreach ($n in "core", "sbx") {
    if (-not (Test-Path $base[$n]) -or -not (Test-Path $sums[$n])) { "$n : nothing to check"; continue }
    $want = ((Get-Content $sums[$n] | Select-Object -Last 1) -split "\s+")[0]   # the last build is the current one
    $got = (Get-FileHash $base[$n] -Algorithm SHA256).Hash.ToLower()
    "$n : " + $(if ($got -eq $want) { "image matches its recorded checksum" } else { "MISMATCH (image differs from the last recorded build)" })
  }
}
function Invoke-Build($which) {
  $basePath = "$out\base\base.qcow2"
  $env:PATH += ";$qemu"                                  # packer finds qemu-system-x86_64 here
  $env:PACKER_CACHE_DIR = "$env:TEMP\packer_cache"      # outside the repo: image/ is uploaded into the VM
  # The OS base is installed with this password and every later stage logs in with it, so it is made once and kept.
  $pw = "$root\build\keys\build-password"
  if (-not (Test-Path $pw)) {
    New-Item -ItemType Directory -Force "$root\build\keys" | Out-Null
    -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ }) | Set-Content $pw
    if (Test-Path $basePath) { Write-Host "  No saved build password for the existing OS base: rebuilding the base too." -ForegroundColor Yellow; $which = "all" }
  }
  $env:PKR_VAR_build_password = (Get-Content $pw -Raw).Trim()
  $env:PKR_VAR_ssh_authorized_key = (Get-Content "$HOME\.ssh\sbx_build_ed25519.pub" -Raw).Trim()
  if (-not (Test-Path "$root\build\keys\core")) { & "C:\Program Files\Git\bin\bash.exe" "$root\image\gen-keys.sh"; if ($LASTEXITCODE) { throw "gen-keys failed" } }
  $steps = switch ($which) {
    "sbx" { @("sbx-node.qemu.sbxnode") }
    "core" { @("sbx-vm.qemu.sbx") }
    "all" { @("base.qemu.install", "sbx-vm.qemu.sbx", "sbx-node.qemu.sbxnode") }
  }
  $packer = Find-Packer; if (-not $packer) { throw "packer not found (winget install Hashicorp.Packer)" }
  Push-Location "$root\image\packer"
  try {
    & $packer init .
    foreach ($st in $steps) {
      $bi = if ($st -like "base.*") { "x" } else { $basePath }
      if ($st -notlike "base.*" -and -not (Test-Path $basePath)) { throw "base image missing: build 'all' first" }
      & $packer build -force -only="$st" -var "base_image=$bi" .
      if ($LASTEXITCODE) { throw "packer failed on $st" }
    }
  } finally { Pop-Location }
  "build done"
}

# ---- menu
function Show($s) {
  Clear-Host
  Write-Host "`n  ENCLAVE" -ForegroundColor Cyan
  Write-Host "  " ("-" * 60) -ForegroundColor DarkGray
  $col = @{ ok = "Green"; warn = "Yellow"; bad = "Red" }
  foreach ($r in $s) { Write-Host ("  {0,-14}" -f $r.Name) -NoNewline; Write-Host $r.Text -ForegroundColor $col[$r.Level] }
  Write-Host "  " ("-" * 60) -ForegroundColor DarkGray
}
function Menu-Items($s) {
  $running = [bool](Find-Vm "core") -or [bool](Find-Vm "sbx")
  $ready = -not ($s | Where-Object { $_.Level -eq "bad" })
  $m = [ordered]@{}
  if (-not $running -and $ready) { $m.S = "Start both VMs" }
  if ($running) { $m.T = "Stop (clean shutdown of the core)" }
  $m.V = "Verify image checksums"
  if (-not $running) { $m.B = "Build or rebuild images"; if (Test-Path $disk.core) { $m.R = "Reset core disk (loses its keys and data)" } }
  $m.Q = "Quit (VMs keep running)"
  $m
}
function Pause-Key { Write-Host "`n  press any key..." -ForegroundColor DarkGray; [void][Console]::ReadKey($true) }

function Menu {
  $last = ""
  while ($true) {
    $s = Get-Status; $m = Menu-Items $s
    $sig = ($s | ForEach-Object { $_.Text }) -join "|"
    if ($sig -ne $last) {
      $last = $sig; Show $s; Write-Host
      foreach ($k in $m.Keys) { Write-Host "  [$k] $($m[$k])" }
      if ((Find-Vm "core")) { Write-Host "`n  Core console: the 'enclave-core' window. Log in there (first time: sbxadmin / sbx-change-me)." -ForegroundColor DarkGray }
    }
    if (-not [Console]::KeyAvailable) { Start-Sleep -Milliseconds 700; continue }
    $k = ([Console]::ReadKey($true).KeyChar.ToString()).ToUpper()
    if (-not $m.Contains($k)) { continue }
    try {
      switch ($k) {
        "S" { Write-Host "`n  starting..."; Invoke-Start }
        "T" { Write-Host "`n  stopping (up to 60 s)..."; Invoke-Stop }
        "V" { Write-Host "`n  hashing images..."; Invoke-Verify }
        "R" { Write-Host "`n  type RESET to confirm: " -NoNewline; if ((Read-Host) -ceq "RESET") { Invoke-Reset } else { "cancelled" } }
        "B" {
          Write-Host "`n  [1] sandbox image (about 2 min)   [2] core image (long; makes the core disk stale)   [3] everything incl. OS base (hours)   [x] back"
          $b = [Console]::ReadKey($true).KeyChar
          $w = @{ "1" = "sbx"; "2" = "core"; "3" = "all" }[[string]$b]
          if ($w) { Invoke-Build $w }
        }
        "Q" { return }
      }
    } catch { Write-Host "`n  $($_.Exception.Message)" -ForegroundColor Red }
    Pause-Key; $last = ""
  }
}

switch ($Do) {
  "status" { Get-Status | ForEach-Object { "{0,-14}{1,-5}{2}" -f $_.Name, $_.Level, $_.Text } }
  "start" { Invoke-Start }
  "stop" { Invoke-Stop }
  "build" { Invoke-Build $What }
  default { Menu }
}
