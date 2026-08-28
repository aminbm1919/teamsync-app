# push-now - "I am done, publish my work now."
#
# Role   : publish immediately instead of waiting out the 4-minute quiet window.
# Input  : none. Run it from anywhere inside the project folder.
# Output : your work on GitHub, or a clear reason why not. Exit code 0 = published
#          (or nothing to publish), 1 = blocked, 2 = conflict.
# Never  : force-pushes or discards anything.
#
#   pwsh push-now.ps1
#
# AI agents: run this when you have finished a piece of work. It is safe to run at
# any time, and safe to run twice. If it reports a conflict, read the CONFLICT.md
# file it names before changing anything.

param([int]$TimeoutSeconds = 120)

$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
Set-Location -LiteralPath $repo

$lock   = Join-Path $repo '.teamsync.lock'
$signal = Join-Path $repo '.teamsync-push-now'

function Say($t, $c = 'Gray') { Write-Host $t -ForegroundColor $c }

if ((git rev-parse --is-inside-work-tree 2>$null) -ne 'true') {
    Say 'This folder is not a git repository. Nothing to publish.' 'Red'; exit 1
}

# A conflict outranks everything. Publishing on top of one is never right.
if (@(git diff --name-only --diff-filter=U 2>$null).Count -gt 0) {
    $latest = Get-ChildItem (Join-Path $repo '_conflicts') -Directory -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -notlike '*-crossed' } |
              Sort-Object Name | Select-Object -Last 1
    Say 'A conflict is open. Nothing was published.' 'Red'
    if ($latest) { Say "Read: _conflicts\$($latest.Name)\CONFLICT.md" 'Yellow' }
    Say 'Finish it, then run this again.' 'Yellow'
    exit 2
}

# Is the sync app running and listening?
$daemonAlive = $false
if (Test-Path -LiteralPath $lock) {
    $t = (Get-Content -LiteralPath $lock | Where-Object { $_ -like 'time=*' }) -replace '^time=', ''
    if ($t) {
        try { $daemonAlive = ((Get-Date) - [datetime]::Parse($t)).TotalSeconds -lt 30 } catch { }
    }
}

if ($daemonAlive) {
    # Nothing to commit and nothing to send: say that truthfully instead of
    # "Published." - and clear any work-in-progress announcement, because
    # finished-with-nothing-left is still finished.
    if (@(git status --porcelain 2>$null).Count -eq 0 -and
        (git rev-list --count 'origin/main..HEAD' 2>$null) -eq '0') {
        Remove-Item -LiteralPath (Join-Path $repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
        Say 'Nothing to publish - everything is already out.' 'Green'
        exit 0
    }

    Say 'Asking the sync app to publish now...' 'Cyan'
    New-Item -ItemType File -Path $signal -Force | Out-Null
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $taken = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500

        # The engine writes its network state into its heartbeat every second.
        # Offline is an answer, not something to time out on for two minutes.
        if ((Get-Content -LiteralPath $lock -ErrorAction SilentlyContinue) -contains 'net=offline') {
            Say 'The app is offline (VPN or network). Nothing was sent - and nothing is lost:' 'Yellow'
            Say 'your work is committed, and the engine publishes it by itself once the connection returns.' 'Yellow'
            exit 1
        }

        if (-not $taken) {
            # Phase 1: has the app picked the request up? It deletes the signal file.
            if (-not (Test-Path -LiteralPath $signal)) { $taken = $true }
            continue
        }

        # Phase 2: wait for the OUTCOME. A push to GitHub over a VPN takes seconds,
        # and how many is not predictable - so poll for the finished state instead
        # of sleeping a fixed amount and guessing.
        if (@(git diff --name-only --diff-filter=U 2>$null).Count -gt 0) {
            Say 'Conflict while publishing. Nothing was pushed, nothing was lost.' 'Red'
            Say 'Look in _conflicts\ for both versions side by side.' 'Yellow'
            exit 2
        }
        # Done means both: nothing left to commit, and nothing left to send.
        # Checking only the second would report success before the app has
        # committed the work at all.
        $dirty = @(git status --porcelain 2>$null).Count
        $ahead = git rev-list --count 'origin/main..HEAD' 2>$null
        if ($dirty -eq 0 -and $ahead -eq '0') {
            # Published means the announced work is out - clear the announcement
            # here too; only the direct-push path used to do this, and the
            # daemon path is the one that normally runs.
            Remove-Item -LiteralPath (Join-Path $repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
            Say 'Published.' 'Green'; exit 0
        }
    }

    if ($taken) {
        Say "The sync app took the request but has not finished within $TimeoutSeconds s." 'Yellow'
        Say 'Your work is committed and safe. Check the app window, and the network or VPN.' 'Yellow'
    } else {
        Say 'The sync app did not answer. Check its window.' 'Yellow'
    }
    exit 1
}

# No daemon: do it here. Same steps, without the side-by-side conflict export.
Say 'The sync app is not running, publishing directly...' 'Yellow'
git add -A 2>$null | Out-Null
if (@(git diff --cached --name-only 2>$null).Count -gt 0) {
    git commit -q -m "sync: $(Get-Date -Format 'MM-dd HH:mm:ss')" 2>$null | Out-Null
}
$ahead = git rev-list --count 'origin/main..HEAD' 2>$null
if ($ahead -eq '0' -or -not $ahead) { Say 'Nothing to publish.' 'Green'; exit 0 }

git fetch -q origin main 2>$null
if ($LASTEXITCODE -ne 0) { Say 'Could not reach GitHub. Check the network or VPN.' 'Yellow'; exit 1 }

git rebase origin/main 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Say 'Conflict. Nothing was pushed, nothing was lost.' 'Red'
    Say 'Start the sync app to get both versions saved side by side,' 'Yellow'
    Say 'or resolve the markers in the files and run: git add . ; git rebase --continue' 'Yellow'
    Say 'To back out entirely: git rebase --abort' 'Yellow'
    exit 2
}

git push -q origin main 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Say 'Push failed. Check the network or VPN, then try again.' 'Yellow'; exit 1 }
# Published means the work is out - whatever was announced as "in progress"
# is in progress no longer.
Remove-Item -LiteralPath (Join-Path $repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
Say "Published $ahead commit(s)." 'Green'
exit 0
