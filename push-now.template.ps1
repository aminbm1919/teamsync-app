# push-now - "I am done, publish my work now."
#
# Role   : publish immediately instead of waiting out the 4-minute quiet window.
# Input  : none. Run it from anywhere inside the project folder.
# Output : your work on GitHub, or a clear reason why not. Exit code 0 = published
#          (or nothing to publish), 1 = blocked, 2 = conflict, 3 = this would
#          delete or roll back work and needs a human's word first.
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

# Git escapes non-ASCII paths in its output unless told not to, so a Persian
# or Arabic file name comes back as "\331\201..." and matches nothing. The
# engine sets this too; this script can run before the engine ever has.
git config core.quotePath false 2>$null | Out-Null

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

# Adding work needs no permission; destroying it does. The engine refuses to
# publish a deletion or a roll-back on its own - but THIS script publishes
# directly whenever the engine is not running, so without the same check it
# would be the way round the guard. That matters most here: this is the script
# AGENTS run, and an agent that tidied a file away would send that tidy-up to
# everybody's machine with no one having seen it.
#
# The rule lives in Get-DestructiveChanges in sync-core.ps1; this is the same
# question asked with the same two git commands, and test_pushnow_destructive
# compares the two verdicts on one repository so they cannot drift apart.
$destroyed = @()
foreach ($f in @(git diff --name-only --diff-filter=D HEAD 2>$null)) {
    if ($f) { $destroyed += "deleted: $f" }
}
foreach ($f in @(git diff --name-only --diff-filter=M HEAD 2>$null)) {
    if (-not $f) { continue }
    $full = Join-Path $repo $f
    if (-not (Test-Path -LiteralPath $full)) { continue }
    $now = (git hash-object -- "$full" 2>$null | Select-Object -First 1)
    if (-not $now) { continue }
    foreach ($c in @(git rev-list -n 40 HEAD -- "$f" 2>$null)) {
        if (-not $c) { continue }
        $was = (git rev-parse -q --verify "${c}:$f" 2>$null | Select-Object -First 1)
        if ($was -and $was -eq $now) { $destroyed += "rolled back: $f"; break }
    }
}
if ($destroyed.Count -gt 0) {
    # ORDINAL sort: the engine and the app both compute this signature, and a
    # culture-aware sort would make a confirmation cover a different set here
    # than it does there.
    $paths = [Collections.Generic.List[string]]@($destroyed | ForEach-Object { ($_ -split ': ', 2)[1] })
    $paths.Sort([StringComparer]::Ordinal)
    $sha = [Security.Cryptography.SHA1]::Create()
    $sig = ([BitConverter]::ToString($sha.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes(($paths -join "`n")))) -replace '-', '').Substring(0, 12)
    if ((git config --local --get teamsync.destructiveok 2>$null) -ne $sig) {
        Say 'This would DESTROY work on everybody machine. Nothing was published.' 'Red'
        foreach ($d in ($destroyed | Select-Object -First 12)) { Say "  $d" 'Yellow' }
        Say ''
        Say 'If it was a mistake, put them back:  git checkout -- <file>' 'Yellow'
        Say 'If you meant it, confirm it in the TeamSync window (Needs your OK),' 'Yellow'
        Say 'then run this again. Agents: this is a decision for your human.' 'Yellow'
        exit 3
    }
}

# Is the sync engine running and listening?
#
# This used to be decided by the heartbeat's timestamp alone, with no check on
# the process at all. The engine writes that timestamp once per pass, and a
# pass with many network round trips can take longer than the 30 seconds this
# allowed - so a perfectly healthy engine was declared dead and the fallback
# below started publishing DIRECTLY, on top of an engine mid-commit or
# mid-rebase. `git add -A` at that moment stages the other process's
# half-applied conflict markers as if they were content.
#
# So the process decides. The pid alone would not be enough - Windows reuses
# ids - which is why the engine records its start time in the lock and this
# compares both. Only a lock written before that scheme falls back to the old
# timestamp rule.
$daemonAlive = $false
$daemonPid = 0
if (Test-Path -LiteralPath $lock) {
    $lines = @(Get-Content -LiteralPath $lock -ErrorAction SilentlyContinue)
    $get = { param($k) (($lines | Where-Object { $_ -like "$k=*" }) -replace "^$k=", '') }
    $lockPid = & $get 'pid'
    $recorded = & $get 'started'
    if ($lockPid) {
        $proc = Get-Process -Id $lockPid -ErrorAction SilentlyContinue
        if ($proc) {
            if ($recorded) {
                $actual = ''
                try { $actual = $proc.StartTime.ToString('o') } catch { }
                if ($actual -and $actual -eq $recorded) { $daemonAlive = $true; $daemonPid = [int]$lockPid }
            } else {
                $t = & $get 'time'
                if ($t) {
                    try {
                        if (((Get-Date) - [datetime]::Parse($t)).TotalSeconds -lt 30) {
                            $daemonAlive = $true; $daemonPid = [int]$lockPid
                        }
                    } catch { }
                }
            }
        }
    }
}

# A rebase in progress belongs to whoever started it. Publishing into one
# means committing somebody else's unfinished merge, so refuse outright
# rather than choosing a side.
$gitDir = git rev-parse --git-dir 2>$null
if ($gitDir -and ((Test-Path -LiteralPath (Join-Path $gitDir 'rebase-merge')) -or
                  (Test-Path -LiteralPath (Join-Path $gitDir 'rebase-apply')))) {
    Say 'A merge is in progress in this folder. Nothing was published.' 'Red'
    Say 'Wait for it to finish, or resolve it, then run this again.' 'Yellow'
    exit 1
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

    # Wait on EVIDENCE, not on a clock.
    #
    # A fixed two-minute deadline reported "the sync app did not answer" while
    # the engine published the very same work thirty seconds later. Measured,
    # on a live test, with the partner's agent waiting on the result. For an
    # agent that is the worst answer available: it says UNDONE about work that
    # is done, and the obvious next move - run it again - is exactly wrong.
    #
    # A pass with several network round trips can take minutes, especially on
    # a machine that has just woken. But the engine stamps its heartbeat every
    # pass, so "still working" is observable. Keep waiting while that keeps
    # moving; stop when it stops, which is the honest end of the story.
    function Get-Beat {
        foreach ($l in (Get-Content -LiteralPath $lock -ErrorAction SilentlyContinue)) {
            if ($l -like 'time=*') {
                $t = [datetime]::MinValue
                if ([datetime]::TryParse($l.Substring(5), [ref]$t)) { return $t }
            }
        }
        return [datetime]::MinValue
    }
    $lastBeat  = Get-Beat
    $beatSeen  = Get-Date
    $ceiling   = (Get-Date).AddSeconds([Math]::Max($TimeoutSeconds, 600))
    $stalled   = $false
    $taken = $false
    while ((Get-Date) -lt $ceiling) {
        Start-Sleep -Milliseconds 500

        $beat = Get-Beat
        if ($beat -gt $lastBeat) { $lastBeat = $beat; $beatSeen = Get-Date }
        if (((Get-Date) - $beatSeen).TotalSeconds -gt 90) { $stalled = $true; break }

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

    # Before saying anything failed, LOOK. The engine may have finished in the
    # gap between the last poll and here, and reporting failure over finished
    # work is the fault this whole block exists to prevent.
    if (@(git status --porcelain 2>$null).Count -eq 0 -and
        (git rev-list --count 'origin/main..HEAD' 2>$null) -eq '0') {
        Remove-Item -LiteralPath (Join-Path $repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
        Say 'Published.' 'Green'; exit 0
    }

    if ($stalled) {
        Say 'The sync app stopped responding - its heartbeat has not moved for 90s.' 'Yellow'
        Say 'Nothing is lost: your work is on disk. Check the app window.' 'Yellow'
    } elseif ($taken) {
        Say "The sync app took the request and is still working after $([int]((Get-Date) - $beatSeen).TotalSeconds)s." 'Yellow'
        Say 'Your work is on disk and the engine publishes it by itself. Do not run this in a loop.' 'Yellow'
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
