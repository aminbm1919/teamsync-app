# who - who is here, and what is each side in the middle of?
#
# Role   : answer "is it safe to edit this file right now?" before you touch it.
# Input  : none. Run it from the project folder.
# Output : the other person's status, and every file either side has unpublished
#          work on. Exit code 0 always - this reports, it never blocks.
# Never  : locks anything. A file listed here can still be edited by anyone. It
#          is a warning, so that the choice is made knowingly.
#
#   pwsh who.ps1
#
# A file appears here from the moment it is saved until the moment it is
# published. That is exactly the window in which two people editing it collide.

$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot

function FromHex($h) {
    try {
        $b = [byte[]]::new($h.Length / 2)
        for ($i = 0; $i -lt $b.Length; $i++) { $b[$i] = [Convert]::ToByte($h.Substring($i * 2, 2), 16) }
        [Text.Encoding]::UTF8.GetString($b)
    } catch { '' }
}

$me = (git config user.name)
if (-not $me) { $me = $env:USERNAME }
$me = ($me -replace '[^A-Za-z0-9._-]+', '-').Trim('-')

# --- is the other side running right now? ---
$now  = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$seen = @{}
foreach ($r in @(git for-each-ref --format='%(refname)' 'refs/teamsync/presence' 2>$null)) {
    $p = $r -split '/'
    if ($p.Count -lt 5) { continue }
    if ($p[3] -eq $me) { continue }
    $ts = 0; [void][long]::TryParse($p[4], [ref]$ts)
    if (-not $seen.ContainsKey($p[3]) -or $ts -gt $seen[$p[3]]) { $seen[$p[3]] = $ts }
}

Write-Host ''
if ($seen.Count -eq 0) {
    Write-Host '  partner: has never connected' -ForegroundColor DarkGray
} else {
    foreach ($who in $seen.Keys) {
        $ago = $now - $seen[$who]
        if ($ago -le 150) { Write-Host "  partner: $who is online now" -ForegroundColor Green }
        else {
            $txt = if ($ago -lt 5400) { "$([int]($ago/60))m" } elseif ($ago -lt 172800) { "$([int]($ago/3600))h" } else { "$([int]($ago/86400))d" }
            Write-Host "  partner: $who was last seen $txt ago" -ForegroundColor Yellow
        }
    }
}

# --- what has unpublished work on it, on either side? ---
$pending = @{}
foreach ($r in @(git for-each-ref --format='%(refname)' 'refs/teamsync/pending' 2>$null)) {
    $p = $r -split '/'
    if ($p.Count -lt 5) { continue }
    $path = FromHex $p[4]
    if (-not $path) { continue }
    if (-not $pending.ContainsKey($p[3])) { $pending[$p[3]] = @() }
    $pending[$p[3]] += $path
}
# Our own side is computed locally: our refs may not be pushed yet.
$mine = @{}
foreach ($line in @(git status --porcelain 2>$null)) {
    if ($line.Length -le 3) { continue }
    $f = $line.Substring(3).Trim().Trim('"')
    if ($f -match '\s->\s') { $f = ($f -split '\s->\s')[-1].Trim().Trim('"') }
    if ($f) { $mine[$f] = $true }
}
foreach ($f in @(git diff --name-only 'origin/main...HEAD' 2>$null)) { if ($f) { $mine[$f] = $true } }

Write-Host ''
$theirs = @()
foreach ($who in $pending.Keys) { if ($who -ne $me) { $theirs += ,@($who, $pending[$who]) } }

if ($theirs.Count -eq 0) {
    Write-Host '  they have no unpublished work. Every file is safe to edit.' -ForegroundColor Green
} else {
    Write-Host '  THEY have unpublished work on these - avoid editing them:' -ForegroundColor Yellow
    foreach ($entry in $theirs) {
        foreach ($f in ($entry[1] | Sort-Object -Unique)) { Write-Host ("    {0,-14} {1}" -f $entry[0], $f) -ForegroundColor Yellow }
    }
    Write-Host '    (nothing is locked - you can still edit them, you just probably should not)' -ForegroundColor DarkGray
}

Write-Host ''
if ($mine.Count -eq 0) {
    Write-Host '  you have nothing unpublished.' -ForegroundColor Green
} else {
    Write-Host '  YOU have unpublished work on these - publish when the piece is finished:' -ForegroundColor Cyan
    foreach ($f in ($mine.Keys | Sort-Object)) { Write-Host "    $f" -ForegroundColor Cyan }
    Write-Host '    pwsh push-now.ps1' -ForegroundColor DarkGray
}
Write-Host ''
exit 0
