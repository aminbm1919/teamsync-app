# working.ps1 - the agent's "I have my hands on these files", said out loud.
#
# Role   : an AI agent reads a file in milliseconds and holds its working copy
#          invisibly in its own head - the machine equivalent of unsaved typing.
#          This command makes that visible: run it BEFORE starting edits.
# Input  : the files about to be changed. Or -Done when nothing is in progress.
# Output : within seconds the other machine shows "working on these files", and
#          incoming versions of them are held instead of landing mid-thought.
# Clears : by push-now.ps1 on a successful publish, by -Done, or by itself
#          after 15 minutes - a crashed agent must not hold the door forever.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File working.ps1 src\core.py docs\plan.md
#   powershell -NoProfile -ExecutionPolicy Bypass -File working.ps1 -Done
param([switch]$Done)
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$report = Join-Path $repo '.teamsync-agent.json'
if ($Done) {
    Remove-Item -LiteralPath $report -Force -ErrorAction SilentlyContinue
    Write-Host 'cleared - nothing marked as in progress'
    exit 0
}
$files = @($args | ForEach-Object { "$_".Replace('\', '/') } | Where-Object { $_ })
if ($files.Count -eq 0) {
    Write-Host 'Name the file(s) you are about to change, or pass -Done.'
    exit 1
}
$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
$open = @($files | ForEach-Object { @{ f = $_; dirty = $true } })
$json = @{ updated = $stamp; open = $open } | ConvertTo-Json -Compress
# UTF-8 with no byte-order mark, written straight to disk.
#
# This used to be `Set-Content -Encoding ascii`, which turns every non-latin
# character into a literal "?" - measured: announcing "فصل-اول/یادداشت.md"
# wrote "???-???/???????.md". The engine then held a file nobody has, while
# the file actually being edited travelled to teammates unprotected. The
# announcement is worse than useless when it names the wrong file.
#
# Not `-Encoding utf8` either: under Windows PowerShell 5.1 that prepends a
# byte-order mark, and this project has already been bitten once by a reader
# that could not see past one.
[IO.File]::WriteAllText($report, $json, (New-Object Text.UTF8Encoding $false))
Write-Host ('announced work in progress on: ' + ($files -join ', '))
Write-Host 'publish with push-now.ps1 when finished - that clears this too'
