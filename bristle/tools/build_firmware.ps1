# Build every Bristle node target on Windows with the PigletBuild toolchain and write a release folder.
#
#   powershell -ExecutionPolicy Bypass -File tools\build_firmware.ps1 [-Name v1-build3-dev]
#
# Needs arduino-cli with ESP32 core 3.3.10 and NimBLE-Arduino 2.5.1 (%USERPROFILE%\PigletBuild has
# both) and Python with PyYAML. It regenerates the protocol files first and stops on a test vector
# mismatch. Every target is built with partitions_bristle_8mb.csv (two 2.5 MB OTA app slots and a
# 2.875 MB LittleFS find queue) and the build fails if the app does not fit a slot.
#
# No path of this PC goes into the images. Assert and log messages carry the source file name
# (__FILE__), so every compile rewrites the folders: the toolchain becomes /toolchain, the sketch
# /src, anything else under the user folder /user. The build fails if a path is still found.
param(
  [string]$Name = "",
  [string]$ArduinoCli = (Join-Path $env:USERPROFILE "PigletBuild\tools\arduino-cli.exe"),
  [string]$ConfigFile = (Join-Path $env:USERPROFILE "PigletBuild\arduino-cli.yaml"),
  [string]$Python = "python",
  [string]$ToolchainRoot = (Join-Path $env:USERPROFILE "PigletBuild"),   # arduino-cli's data and user folders
  [switch]$CheckOnly          # compile every target, write nothing into firmware\releases
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Sketch = Join-Path $Root "firmware\bristle_node"
$Build = (Select-String -Path (Join-Path $Sketch "Boards.h") -Pattern '^#define BRISTLE_BUILD (\d+)').Matches[0].Groups[1].Value
if (-not $Name) { $Name = "v1-build$Build-dev" }
$Out = Join-Path $Root "firmware\releases\$Name"
$Tmp = Join-Path $env:TEMP ("bristle-build-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$SlotBytes = 0x280000   # app0/app1 in partitions_bristle_8mb.csv

if (-not (Test-Path $ArduinoCli)) { throw "arduino-cli not found at $ArduinoCli (pass -ArduinoCli)" }
& $Python -I (Join-Path $Root "tools\gen_proto.py")
if ($LASTEXITCODE -ne 0) { throw "gen_proto.py failed (test vector mismatch, or PyYAML missing)" }

# The Arduino build uses a file called partitions.csv next to the sketch. Build from a copy of the
# sketch that has it, so the source folder keeps one clearly named layout file.
$Stage = Join-Path $Tmp "bristle_node"
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Copy-Item (Join-Path $Sketch "*") $Stage -Include *.ino, *.h, *.cpp
Copy-Item (Join-Path $Sketch "partitions_bristle_8mb.csv") (Join-Path $Stage "partitions.csv")

# GCC uses the LAST option that matches, so the general rule comes first.
function Get-PrefixMaps($buildDir) {
  $pairs = @(
    @($env:USERPROFILE, "/user"),
    @($ToolchainRoot, "/toolchain"),
    @($Tmp, "/build"),
    @((Join-Path $buildDir "sketch"), "/src")
  )
  foreach ($pair in $pairs) {
    if ($pair[0] -match '[\s"=]') { throw "cannot rewrite a path with a space, a quote or '=' in it: $($pair[0])" }
  }
  ($pairs | ForEach-Object { "-ffile-prefix-map=$($_[0])=$($_[1]) -fmacro-prefix-map=$($_[0])=$($_[1])" }) -join " "
}
# What must not be inside an image: the user folder, in the spellings a compiler could have stored.
$Leaks = @($env:USERPROFILE, $env:USERPROFILE.Replace("\", "/"), ("Users\" + $env:USERNAME), ("Users/" + $env:USERNAME)) |
  Sort-Object -Unique

$cfg = @()
if (Test-Path $ConfigFile) { $cfg = @("--config-file", $ConfigFile) }
$sizes = @{}

function Build-Target($fqbn, $folder) {
  $dir = Join-Path $Tmp "out-$folder"
  $log = Join-Path $Tmp "$folder.log"
  # cmd /c so the compiler's stderr lands in the log instead of becoming PowerShell errors.
  $buildDir = Join-Path $Tmp "build-$folder"
  $maps = Get-PrefixMaps $buildDir
  # --clean: no object from an earlier build (compiled without the path rewriting) is reused.
  $cmdLine = '"' + $ArduinoCli + '" ' + (($cfg | ForEach-Object { '"' + $_ + '"' }) -join ' ') +
    ' compile --clean --warnings all -b "' + $fqbn + '" --build-property "upload.maximum_size=' + $SlotBytes + '"' +
    ' --build-property "compiler.c.extra_flags=' + $maps + '"' +
    ' --build-property "compiler.cpp.extra_flags=' + $maps + '"' +
    ' --build-property "compiler.S.extra_flags=' + $maps + '"' +
    ' --build-path "' + $buildDir + '" --output-dir "' + $dir + '" "' + $Stage + '" > "' + $log + '" 2>&1'
  cmd /c $cmdLine
  if ($LASTEXITCODE -ne 0) {
    Get-Content $log | Select-String -Pattern 'error|Error' | Select-Object -First 40 | ForEach-Object { Write-Host $_.Line }
    throw "compile failed for $folder (full log: $log)"
  }
  $warn = @(Get-Content $log | Select-String -Pattern 'bristle_node[\\/].*warning:')
  foreach ($image in "bristle_node.ino.merged.bin", "bristle_node.ino.bin") {
    $text = [Text.Encoding]::GetEncoding(28591).GetString([IO.File]::ReadAllBytes((Join-Path $dir $image)))
    foreach ($leak in $Leaks) {
      if ($text.IndexOf($leak, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        throw "a path of this PC is inside $image for ${folder}: '$leak' (the path rewriting did not cover it)"
      }
    }
  }
  if (-not $CheckOnly) {
    $dst = Join-Path $Out $folder
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item (Join-Path $dir "bristle_node.ino.merged.bin") (Join-Path $dst "bristle-$folder-FULL.bin")   # flash at 0x0
    Copy-Item (Join-Path $dir "bristle_node.ino.bin") (Join-Path $dst "bristle-$folder-app.bin")           # app only, 0x10000
  }
  $app = (Get-Item (Join-Path $dir "bristle_node.ino.bin")).Length
  $script:sizes[$folder] = $app
  Write-Host ("{0,-15} app {1,9:N0} B ({2:P0} of a slot), {3} warning(s) in Bristle files" -f $folder, $app, ($app / $SlotBytes), $warn.Count)
  $warn | ForEach-Object { Write-Host ("    " + $_.Line) }
}

# CPUFreq=80: the same clock the Piglet builds use; plenty for scanning, and it runs cooler.
Build-Target "esp32:esp32:XIAO_ESP32C5:CDCOnBoot=cdc,CPUFreq=80,FlashSize=8M" "xiao_c5"
Build-Target "esp32:esp32:esp32c5:CDCOnBoot=cdc,CPUFreq=80,FlashSize=8M" "devkit_c5"
# Boards whose only USB port is a CH343/UART bridge (no native USB): CDCOnBoot=default.
Build-Target "esp32:esp32:esp32c5:CDCOnBoot=default,CPUFreq=80,FlashSize=8M" "devkit_c5_uart"

if ($CheckOnly) {
  Remove-Item -Recurse -Force $Tmp
  Write-Host "compile check done: every target builds. Nothing was written to the releases folder."
  exit 0
}
Get-ChildItem -Recurse -Path $Out -Filter *.bin | Sort-Object FullName | ForEach-Object {
  $h = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower()
  "$h  $($_.Directory.Name)/$($_.Name)"
} | Set-Content -Encoding ascii (Join-Path $Out "SHA256SUMS")
# The notices travel with the images: what is compiled into them, and the licence texts.
Copy-Item (Join-Path $Root "firmware\THIRD_PARTY_NOTICES.md") $Out
Copy-Item (Join-Path $Root "firmware\licenses") $Out -Recurse -Force
Remove-Item -Recurse -Force $Tmp
Write-Host "release written to $Out"
