# Build TeamSync as a folder, and pack that folder into one zip.
#
# Role   : turns the sources into the thing people actually run.
# Input  : ui\teamsync_ui.py and the engine scripts beside this file.
# Output : dist\TeamSync\  (the program) and dist\TeamSync.zip (what is published).
#
#   pwsh build.ps1
#
# Why a folder and not a single file: a one-file build unpacks about 970 files
# into %TEMP% on every single launch. On a machine whose antivirus or disk gets in
# the way of that, one file goes missing and the program dies with an error that
# points nowhere near the cause - which happened three separate times on the
# teammate's machine, wearing three different faces. A folder is never unpacked.

param([switch]$SkipClean)

$ErrorActionPreference = 'Stop'
function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Die($t)  { Write-Host $t -ForegroundColor Red; exit 1 }

Set-Location -LiteralPath $PSScriptRoot

$out = Join-Path $PSScriptRoot 'dist\TeamSync'
$zip = Join-Path $PSScriptRoot 'dist\TeamSync.zip'

# A copy running out of the build folder holds its files open, so the clean fails
# and - with $ErrorActionPreference = Stop - the build never happens. Left to
# itself that produces the worst outcome available: you publish the previous
# version believing you built the new one.
$running = Get-CimInstance Win32_Process -Filter "Name='TeamSync.exe'" |
           Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($out, 'OrdinalIgnoreCase') }
foreach ($r in $running) {
    Write-Host "    closing the copy running from the build folder (pid $($r.ProcessId))" -ForegroundColor DarkGray
    Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($running) { Start-Sleep -Milliseconds 800 }

if (-not $SkipClean) {
    Step 'Clearing the previous build'
    foreach ($p in @($out, $zip, (Join-Path $PSScriptRoot 'build'))) {
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
    }
}

Step 'Building'
$bundled = @(
    'teamsync.ps1', 'sync-core.ps1', 'init-owner.ps1', 'init-friend.ps1',
    'push-now.template.ps1', 'who.template.ps1', 'AGENTS.project.md'
)
$icon = Join-Path $PSScriptRoot 'ui\assets\teamsync.ico'
if (-not (Test-Path -LiteralPath $icon)) { Die "Missing the app icon: $icon" }
$args = @('-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed', '--name', 'TeamSync',
          '--icon', $icon)
# Shipped as a file too, because the window sets its own icon at runtime: the icon
# baked into the exe covers the taskbar and the shortcut, not the title bar.
$args += @('--add-data', "$icon;assets")
foreach ($f in $bundled) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $f))) { Die "Missing source file: $f" }
    $args += @('--add-data', "$f;engine")
}
# TEAM-PROJECT-REFERENCE.md lives one level up; the init script looks in both places.
$ref = Join-Path $PSScriptRoot '..\TEAM-PROJECT-REFERENCE.md'
if (Test-Path -LiteralPath $ref) { $args += @('--add-data', "$ref;engine") }
$args += @('--add-data', 'ui\editor-extension;editor-extension')
$args += @('--add-data', 'ui\assets\help;assets\help')
$args += 'ui/teamsync_ui.py'

& python @args
if ($LASTEXITCODE -ne 0) { Die 'PyInstaller failed.' }
if (-not (Test-Path -LiteralPath (Join-Path $out 'TeamSync.exe'))) { Die "No exe at $out" }

Step 'Packing the folder'
# Zipped from the folder's contents, so the exe sits at the root of the archive.
# The updater accepts a wrapper folder too, but this way the zip can also just be
# extracted by hand into an empty directory.
Compress-Archive -Path (Join-Path $out '*') -DestinationPath $zip -CompressionLevel Optimal -Force

$folderMB = '{0:N1}' -f ((Get-ChildItem -LiteralPath $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
$zipMB    = '{0:N1}' -f ((Get-Item -LiteralPath $zip).Length / 1MB)
Write-Host ''
Write-Host "Built.  folder $folderMB MB on disk,  zip $zipMB MB to download." -ForegroundColor Green
Write-Host "  run     $out\TeamSync.exe" -ForegroundColor DarkGray
Write-Host "  publish pwsh publish-release.ps1 -Version x.y.z" -ForegroundColor DarkGray
