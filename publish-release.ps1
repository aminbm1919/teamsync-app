# Sign and publish a TeamSync build.
#
# Role   : the only sanctioned way to put a new version on the other machine.
# Input  : -Version 1.0.8  [-Notes "..."]  and the built dist\TeamSync.zip
# Output : a signed GitHub release. The app refuses anything not signed by the key.
# Never  : publishes an unsigned build, or a package it did not just verify itself.
#
# The published thing is the zip of the whole program folder, and the signature
# covers that zip. Signing the exe alone would leave every other file in the
# folder unprotected, which is most of the program.
#
#   pwsh publish-release.ps1 -Version 1.0.8 -Notes "what changed"
#
# Why the signature exists: automatic updates mean whoever can publish here runs
# code on the other person's machine. Owning the GitHub account must not be enough
# - the private key is the second lock, and it lives only with its owner.

param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = '',
    [string]$Repo = 'aminbm1919/teamsync-app',
    [string]$Key  = "$env:USERPROFILE\.ssh\teamsync-release"
)

$ErrorActionPreference = 'Stop'
function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Die($t)  { Write-Host $t -ForegroundColor Red; exit 1 }

# Version rule, set 2026-08-29: only the FIRST part may pass 9. The line runs
# 2.1.0 ... 2.1.9, then 2.2.0, on to 2.9.9, then 3.0.0.
#
# Checked FIRST, before the package and the key are even looked for, because
# this is the one thing that needs no files to judge and the one mistake that
# costs most: a tag, once pushed, is what every installed copy compares
# itself against forever. Checking it after the staleness test - where it
# first sat - meant a bad number was reported only after a full rebuild.
$vparts = $Version.TrimStart('v', 'V') -split '\.'
if ($vparts.Count -ne 3 -or ($vparts | Where-Object { $_ -notmatch '^\d+$' })) {
    Die "Version must be three numbers, like 2.1.0. Got: $Version"
}
if ([int]$vparts[1] -gt 9 -or [int]$vparts[2] -gt 9) {
    # The carry has to cascade, not happen once: 2.9.10 rolls the third part
    # into the second, which then also passes 9 and rolls into the first.
    # A single carry suggests 2.10.0 - a number this very rule forbids, which
    # would send the reader round the loop again.
    $a, $b, $c = [int]$vparts[0], [int]$vparts[1], [int]$vparts[2]
    if ($c -gt 9) { $c = 0; $b++ }
    if ($b -gt 9) { $b = 0; $a++ }
    Die ("Only the first part may pass 9, so $Version is not a valid version." +
         "`nAfter x.y.9 comes x.(y+1).0, and after x.9.9 comes (x+1).0.0." +
         "`nYou probably want: $a.$b.$c")
}

$exe = Join-Path $PSScriptRoot 'dist\TeamSync.zip'
if (-not (Test-Path $exe)) { Die "No package found at $exe - run build.ps1 first." }

# A zip older than the sources is a build that was never redone. Publishing it
# ships the previous version under the new number, and nothing downstream can
# notice: the tag, the notes and the signature would all be perfectly correct.
$newest = Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'ui') -Filter *.py |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newest -and $newest.LastWriteTime -gt (Get-Item $exe).LastWriteTime) {
    Die "$($newest.Name) was changed after the package was built.`nRun build.ps1 again, then publish."
}
if (-not (Test-Path $Key)) {
    Die "Signing key not found: $Key`n`nWithout it a release cannot be published, and that is the point.`nIf the key is lost, a new one must be generated AND a new exe handed over by hand once."
}

$tag = if ($Version.StartsWith('v')) { $Version } else { "v$Version" }

# The version inside the exe must match the tag, or the other side will keep
# re-downloading a build that never satisfies the comparison.
$declared = (Select-String -Path (Join-Path $PSScriptRoot 'ui\teamsync_ui.py') -Pattern '^APP_VERSION\s*=\s*"([^"]+)"').Matches.Groups[1].Value
if ($declared -ne $tag.TrimStart('v')) {
    Die "Mismatch: the source says APP_VERSION = $declared but you are publishing $tag.`nSet them to the same value and rebuild."
}

Step 'Signing the build'
$sig = "$exe.sig"
if (Test-Path $sig) { Remove-Item -LiteralPath $sig -Force }
ssh-keygen -Y sign -f $Key -n teamsync $exe
if (-not (Test-Path $sig)) { Die 'Signing failed - no signature file was produced.' }

Step 'Verifying the signature the same way the app will'
$allowed = Join-Path $env:TEMP 'teamsync_allowed_signers'
"teamsync $(Get-Content "$Key.pub")" | Set-Content -LiteralPath $allowed -Encoding ascii
$check = cmd /c "ssh-keygen -Y verify -f `"$allowed`" -I teamsync -n teamsync -s `"$sig`" < `"$exe`" 2>&1"
if ($LASTEXITCODE -ne 0) { Die "The signature does not verify: $check`nPublishing stopped." }
Write-Host "    $check" -ForegroundColor DarkGray

Step "Publishing $tag"
$noteText = if ($Notes) { $Notes } else { "TeamSync $($tag.TrimStart('v'))" }
gh release create $tag $exe $sig --repo $Repo --title "TeamSync $($tag.TrimStart('v'))" --notes $noteText
if ($LASTEXITCODE -ne 0) { Die 'gh release create failed.' }

Write-Host ''
Write-Host "Published $tag, signed and verified." -ForegroundColor Green
Write-Host 'The other machine will accept it because the signature matches the key' -ForegroundColor DarkGray
Write-Host 'built into the app. An unsigned build would be refused.' -ForegroundColor DarkGray
