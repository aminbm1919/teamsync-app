# teamsync - continuous two-way sync daemon for one shared folder.
#
# Role   : keep a local folder and its private GitHub repository in step, without
#          anyone typing a git command.
# Input  : -Path <folder that is already a git repo with an 'origin' remote>
# Output : commits + pushes after the folder goes quiet, OR immediately when asked;
#          pulls the other person's work in; on conflict writes both sides to disk.
# Never  : force-pushes, discards work, or moves files while they are being written.
#
#   pwsh teamsync.ps1 -Path "C:\path\to\project"
#
# Stop with Ctrl+C. Nothing is lost - unpushed work stays in local commits.

param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$AppVersion = '',
    [int]$QuietSeconds  = 240,  # publish this long after the last change (the backstop)
    [int]$WatchSeconds  = 3,    # ask GitHub "anything new?" this often (a 304 is free)
    [int]$PollSeconds   = 0,    # full fetch fallback; 0 = pick automatically
    [int]$SettleSeconds = 15,   # never move files unless the folder is this quiet
    [int]$PresenceSeconds = 60, # how often to publish and read the "I am here" beat
    [string]$Branch     = 'main',
    [switch]$NoPopup
)

$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path -LiteralPath $Path).Path

# Deliberately do NOT make the project folder this process's working directory.
# Windows refuses to move or rename a folder that a running process is sitting in,
# so doing that would quietly hold the user's own project hostage for as long as
# sync is on. Point git at the repository through the environment instead, and
# keep our feet somewhere harmless.
$env:GIT_DIR       = Join-Path $repo '.git'
$env:GIT_WORK_TREE = $repo
Set-Location -LiteralPath ([IO.Path]::GetTempPath())

. (Join-Path $PSScriptRoot 'sync-core.ps1')
Initialize-SyncCore -Repo $repo -Branch $Branch -NoPopup:$NoPopup -AppVersion $AppVersion

# Not --is-inside-work-tree: that answers about the CURRENT DIRECTORY, and ours
# is deliberately elsewhere so the project folder stays movable. Ask about the
# repository itself instead.
if (-not (Test-Path -LiteralPath (Join-Path $repo '.git'))) {
    Write-Host "Not a git repository: $repo" -ForegroundColor Red
    Write-Host 'Pick the project folder itself - the one that contains .git - not the folder above it.' -ForegroundColor Yellow
    exit 1
}
if (-not (git rev-parse --git-dir 2>$null)) {
    Write-Host "Git cannot read this repository: $repo" -ForegroundColor Red; exit 1
}
if (-not (git remote get-url origin 2>$null)) {
    Write-Host "No 'origin' remote. Run the setup first." -ForegroundColor Red; exit 1
}

# Single instance per folder. The daemon may be running detached in the
# background (the window was closed); starting a second one would race the
# first on every commit and rebase, and git cannot survive two processes
# rebasing one worktree. Test-DaemonAlive settles it on the process itself
# rather than on how recently it managed to write - see its comment.
$running = Test-DaemonAlive $script:SC_Lock
if ($running) {
    Write-Host "Another sync for this folder is already running (PID $running). Nothing to do."
    exit 0
}

$global:TS_LastChange = $null
# The quiet window measures from the last change - but with no change seen yet
# (engine just started, or just published), "no change" used to mean an
# infinitely old one, so brand-new work published within seconds instead of
# after the four quiet minutes. Field-proven on both machines. Quiet now
# counts from this baseline whenever no change event is on record.
$global:TS_QuietBase  = Get-Date
$fsw = New-Object System.IO.FileSystemWatcher
$fsw.Path = $repo
$fsw.IncludeSubdirectories = $true
$fsw.NotifyFilter = [IO.NotifyFilters]::FileName -bor [IO.NotifyFilters]::DirectoryName -bor [IO.NotifyFilters]::LastWrite
$fsw.EnableRaisingEvents = $true

$onChange = {
    $p = $Event.SourceEventArgs.FullPath
    # The trailing ([\\/]|$) matters as much as the leading one. Every git command
    # touches the .git DIRECTORY itself, which arrives as a change to a path that
    # ends in "\.git". Without the "$" alternative those events count as user
    # edits, so the engine's own fetch every ten seconds keeps resetting the idle
    # timer and the quiet-window publish never becomes due.
    if ($p -match '[\\/]\.git([\\/]|$)' -or
        $p -match '[\\/]_conflicts([\\/]|$)' -or
        $p -match '\.teamsync' ) { return }
    $global:TS_LastChange = Get-Date
}
foreach ($evt in 'Changed', 'Created', 'Deleted', 'Renamed') {
    Register-ObjectEvent -InputObject $fsw -EventName $evt -Action $onChange | Out-Null
}

Initialize-RemoteWatch
if ($PollSeconds -le 0) {
    # With the cheap check available, a full fetch is only a safety net. Without
    # it, the full fetch IS the detector and has to stay frequent.
    $PollSeconds = if (Test-RemoteWatchAvailable) { 60 } else { 10 }
}

Write-Host ''
Write-Host 'teamsync is running.' -ForegroundColor Green
Write-Host "  folder    : $repo"
Write-Host "  branch    : $Branch"
Write-Host "  push      : when you ask, or $QuietSeconds s after the last change"
Write-Host ("  download  : " + $(if (Test-RemoteWatchAvailable) { "within ~$WatchSeconds s of any change (fallback every $PollSeconds s)" } else { "every $PollSeconds s" }))
Write-Host '  stop      : Ctrl+C  (unpushed work stays safe in local commits)'
Write-Host ''


Invoke-LogRotation
$script:TS_LogDate = (Get-Date).Date
Update-Heartbeat

# Before publishing a single ref: is this name already taken on this project?
# Everything the engine owns is filed under the sanitised git user.name, and
# each engine deletes what it finds under its own name. Two people sharing
# one name would delete each other's heartbeats and each other's file
# announcements without end - turning OFF the collision warning for exactly
# the pair most at risk of colliding, and saying nothing about it.
#
# Refused rather than worked around. A generated unique key would fix the
# refs and break the thing they are for: the sanitised name is what joins a
# presence beat to a commit author, which is how anyone is attributed at all.
$named = Resolve-MyName
if ($named.Action -eq 'refused') {
    Write-Host ''
    Write-Host "The name '$($named.Name)' on this project belongs to a different" -ForegroundColor Red
    Write-Host "GitHub account ($($named.Owner)), so it is already somebody else's." -ForegroundColor Red
    Write-Host 'Two PEOPLE cannot share one name here: your presence beats and your' -ForegroundColor Yellow
    Write-Host '"working on this file" warnings would delete each other, and the' -ForegroundColor Yellow
    Write-Host 'warnings that stop you overwriting one another would stop working.' -ForegroundColor Yellow
    Write-Host ''
    Write-Host 'Pick a name of your own, in this folder, then start again:' -ForegroundColor Cyan
    Write-Host '    git config user.name "your name"'
    Write-Host ''
    Write-Log "refusing to start: the name '$($named.Name)' belongs to $($named.Owner)" 'Red'
    exit 1
}
if ($named.Action -eq 'renamed') {
    # The SAME person, on a second machine. That is an ordinary thing to do
    # and is not refused - the two just have to be tellable apart, so this
    # machine takes a numbered name. Written into this project's git config,
    # not only into the presence key, so commits carry it too.
    Write-Host ''
    Write-Host "You are already working here as '$($named.From)' from another machine." -ForegroundColor Cyan
    Write-Host "This one will show up as '$($named.Name)', so your teammates can tell" -ForegroundColor Cyan
    Write-Host 'the two apart. Nothing else changes.' -ForegroundColor Cyan
    Write-Host ''
    Write-Log "this machine will publish as '$($named.Name)' - '$($named.From)' is your other machine" 'Cyan'
}
Publish-Identity

Invoke-Integrate | Out-Null
Publish-Presence
Sync-PresenceRefs
Publish-Pending
Sync-PendingRefs
$partnerPending = Get-PartnerPending

$lastPresence = Get-Date
$lastWatch   = Get-Date
$lastPoll    = Get-Date
$wasBlocked  = $false
$publishASAP = $false
# A conflict is announced to the team more often than presence: it is news,
# and it is over quickly. Empty until one happens.
$lastConflictBeat = (Get-Date).AddSeconds(-60)
$script:TS_ExportedConflict = ''
Sync-ConflictRefs
# Deliberately EMPTY, not the current state: the first pass then reports
# whatever it finds, so opening the app while somebody is already resolving
# tells you so. Seeded with the real state, a conflict that began before this
# engine started would never be mentioned at all.
$teamConflicts = @{}
# While somebody else is resolving, the automatic publish waits - but not
# forever, and it says so exactly once each way.
$conflictPauseSince  = $null
$conflictPauseSaid   = $false
$conflictPauseGaveUp = $false
# Each volunteer is announced once, not once per tick.
$seenVolunteers = @{}

# A publish that fails because the network is down must not be forgotten. Without
# this, finished work sits unpublished until the user happens to edit another file
# or press the button - which, with a dropped VPN, could be never.
$retryAt      = $null
$retryBackoff = 30          # seconds; doubles up to the cap on each failure
$RETRY_MAX    = 300

try {
    while ($true) {
        Start-Sleep -Seconds 1
        $now = Get-Date

        # The folder can be moved or deleted underneath us. Every git command then
        # fails, forever, in a loop nobody can see. Stop instead - the app will
        # start a fresh engine once someone points it at the new location.
        if (-not (Test-Path -LiteralPath $repo)) {
            Write-Host "Project folder is gone: $repo" -ForegroundColor Yellow
            Write-Host 'Stopping. Open the project at its new location in the app.' -ForegroundColor Yellow
            break
        }

        if ((Get-Date).Date -ne $script:TS_LogDate) {
            $script:TS_LogDate = (Get-Date).Date
            Invoke-LogRotation
        }
        Update-Heartbeat

        $blocked = (Test-Rebasing) -or ((Get-Unmerged).Count -gt 0)

        # An explicit "I am done, publish now" request. Read it even while blocked,
        # so it is consumed rather than firing unexpectedly later.
        $asked = Test-Path -LiteralPath $script:SC_Signal
        if ($asked) {
            Remove-Item -LiteralPath $script:SC_Signal -Force -ErrorAction SilentlyContinue
            if ($blocked) {
                Write-Log 'push requested, but a conflict is open - finish it first' 'Yellow'
            } else {
                Write-Log 'push requested' 'Cyan'
                $global:TS_LastChange = $null
                $global:TS_QuietBase  = Get-Date
                $publishASAP = $false
                if (Invoke-Publish -Reason 'requested') {
                    $retryAt = $null; $retryBackoff = 30; $script:SC_PendingPublish = 0
                } else {
                    $retryAt = $now.AddSeconds($retryBackoff)
                    $script:SC_PendingPublish = Get-AheadCount
                    Write-Log "will retry publishing in $retryBackoff s" 'Yellow'
                }
                continue
            }
        }

        if ($blocked) {
            $unmerged = @(Get-Unmerged)
            if (-not $wasBlocked) {
                Write-Log 'paused - conflict is open' 'Yellow'
                $wasBlocked = $true
            }

            # A rebase replays our commits one at a time, so resolving the
            # first conflict can raise a second on the next commit. Nothing
            # used to notice: the export ran only at the failed rebase call,
            # and this branch simply skipped everything. The person was left
            # with no MINE/THEIRS/BASE for the new file while the window still
            # pointed at the previous folder. Export whenever the unmerged set
            # is not the one already exported.
            $signature = ($unmerged | Sort-Object) -join '|'
            if ($signature -and $signature -ne $script:TS_ExportedConflict) {
                if ($script:TS_ExportedConflict) {
                    Write-Log 'the merge raised another conflict - writing the copies' 'Yellow'
                }
                Report-Conflict -Phase 'download (continued)'
                $script:TS_ExportedConflict = $signature
            }

            # Stay visible. This branch used to `continue` past the presence
            # beat, so a person went dark on everybody's screen for exactly as
            # long as they were untangling a conflict - the one time the team
            # most needs to know where they are and what they are holding.
            if (($now - $lastPresence).TotalSeconds -ge $PresenceSeconds) {
                $lastPresence = $now
                Publish-Presence
                Sync-PresenceRefs
            }
            # And say WHAT is stuck, so the others can leave that file alone
            # until the next version of it arrives.
            if (($now - $lastConflictBeat).TotalSeconds -ge 15) {
                $lastConflictBeat = $now
                Publish-Conflict -Files $unmerged
                # And our own side of it, so a teammate can read the whole
                # thing and, if they want, write the final version and
                # publish it themselves. Without this they can see two of
                # the three versions; ours has never left this machine.
                Publish-ConflictWork
                Sync-ConflictRefs

                # Somebody putting their hand up is news for the person who
                # is stuck: it means help is coming, and that their own copy
                # may be overtaken by the version that lands.
                foreach ($line in @(git for-each-ref --format='%(refname)' "refs/teamsync/volunteer/$(Get-PresenceName)" 2>$null)) {
                    # refs/teamsync/volunteer/<owner>/<hex>/<volunteer> = 6 parts
                    $parts = $line -split '/', 6
                    if ($parts.Count -lt 6) { continue }
                    $p = ConvertFrom-RefHex $parts[4]
                    $volunteer = $parts[5]
                    $key = "$volunteer|$p"
                    if (-not $seenVolunteers.ContainsKey($key)) {
                        $seenVolunteers[$key] = $true
                        Write-Log "$volunteer volunteered to settle the conflict in $p - their version will arrive here" 'Cyan'
                    }
                }
            }
            continue
        }
        if ($wasBlocked) {
            # Resolving a conflict is a deliberate, finished act, and the point is to
            # get both sides back in step. Do not make the user wait out another full
            # quiet window - publish as soon as the files settle.
            Write-Log 'conflict finished - sync resumed' 'Green'
            $wasBlocked  = $false
            $publishASAP = $true
            $global:TS_LastChange = $now
            # Tell everyone it is over, in the same breath. An announcement
            # with no ending is a warning people learn to ignore.
            $script:TS_ExportedConflict = ''
            $seenVolunteers = @{}
            Publish-Conflict -Files @()
            Clear-ConflictWork
            Clear-Volunteers
            Sync-ConflictRefs
        }

        # The watcher can miss a write (an external tool's copy, a burst of
        # events) - and then the quiet clock would run from a stale baseline
        # and publish brand-new work in seconds. So dirt is cross-checked by
        # sight: the first tick that SEES uncommitted changes with no fresh
        # change event on record starts the clock right there.
        $dirtyNow = @(git status --porcelain 2>$null).Count -gt 0
        if ($dirtyNow -and -not $global:TS_WasDirty) {
            $ref = if ($global:TS_LastChange) { $global:TS_LastChange } else { $global:TS_QuietBase }
            if (($now - $ref).TotalSeconds -gt 30) { $global:TS_LastChange = $now }
        }
        $global:TS_WasDirty = $dirtyNow

        $quiet = if ($global:TS_LastChange) { ($now - $global:TS_LastChange).TotalSeconds }
                 else { ($now - $global:TS_QuietBase).TotalSeconds }

        if (($now - $lastPresence).TotalSeconds -ge $PresenceSeconds) {
            $lastPresence = $now
            Publish-Presence
            Sync-PresenceRefs
        }

        # Ask the cheap question often; do the expensive fetch only when the
        # answer is yes, or when the safety-net interval comes round anyway.
        $remoteMoved = $false
        if (($now - $lastWatch).TotalSeconds -ge $WatchSeconds) {
            $lastWatch = $now
            $remoteMoved = Test-RemoteChanged
        }

        if ($remoteMoved -or ($now - $lastPoll).TotalSeconds -ge $PollSeconds) {
            $lastPoll = $now
            # Fetch on every tick; only move files once the folder has settled.
            if ($quiet -ge $SettleSeconds) { Invoke-Integrate | Out-Null }
            else { git fetch -q origin $Branch 2>$null }

            # Work can be committed but unpublished with no file event left to
            # trigger it - the engine was restarted, or a conflict was resolved
            # while it was down. Nothing else in this loop would ever notice, so
            # the work would sit there forever. Catch it here.
            #
            # A standing HOLD needs the same catch for a different reason: the
            # publish moment is the only thing that lets held downloads
            # through, and while there is nothing of ours to send, nothing
            # here would ever reach it. A teammate's work would then wait on a
            # file we are merely sitting on, with no event able to free it.
            # The catch-up publish is automatic too, so the same restraint
            # applies - otherwise it would quietly send the very work the
            # quiet-window pause is holding back. A standing HOLD is the one
            # exception: that one hurts a teammate by NOT publishing.
            $othersStuck = $teamConflicts.Count -gt 0 -and -not $script:SC_Holding
            if (-not $othersStuck -and
                -not $global:TS_LastChange -and -not $retryAt -and
                ((Get-AheadCount) -gt 0 -or $script:SC_Holding)) {
                if (Invoke-Publish -Reason 'catch-up') {
                    $script:SC_PendingPublish = 0
                } else {
                    $retryAt = $now.AddSeconds($retryBackoff)
                    $script:SC_PendingPublish = Get-AheadCount
                    Write-Log "will retry publishing in $retryBackoff s" 'Yellow'
                }
            }

            # Read what the others are stuck on, and say so once each way.
            # This is the whole point of announcing a conflict: everyone else
            # keeps working normally - their repositories are fine - but they
            # know to leave that file alone until the fixed version arrives.
            Sync-ConflictRefs
            $nowStuck = Get-TeamConflicts
            foreach ($who in @($nowStuck.Keys + $teamConflicts.Keys | Sort-Object -Unique)) {
                $before = @(); if ($teamConflicts.ContainsKey($who)) { $before = $teamConflicts[$who] }
                $after  = @(); if ($nowStuck.ContainsKey($who))      { $after  = $nowStuck[$who] }
                foreach ($f in $after) {
                    if ($before -notcontains $f) {
                        Write-Log "$who hit a conflict in $f and is resolving it - best to leave that file alone until it lands" 'Yellow'
                    }
                }
                foreach ($f in $before) {
                    if ($after -notcontains $f) { Write-Log "$who resolved the conflict in $f" 'Green' }
                }
            }
            $teamConflicts = $nowStuck

            # Say which files we have unpublished work on, and read theirs.
            Publish-Pending
            Sync-PendingRefs
            $fresh = Get-PartnerPending
            foreach ($who in @($fresh.Keys + $partnerPending.Keys | Sort-Object -Unique)) {
                $before = @(); if ($partnerPending.ContainsKey($who)) { $before = $partnerPending[$who] }
                $after  = @(); if ($fresh.ContainsKey($who))          { $after  = $fresh[$who] }
                foreach ($f in $after)  { if ($before -notcontains $f) { Write-Log "$who is changing $f" 'Cyan' } }
                foreach ($f in $before) { if ($after  -notcontains $f) { Write-Log "$who published $f" 'Green' } }
            }
            $partnerPending = $fresh
        }

        # Retry a publish that the network refused earlier.
        if ($retryAt -and $now -ge $retryAt) {
            if (Invoke-Publish -Reason "retry after $retryBackoff s") {
                $retryAt = $null
                $retryBackoff = 30
                $script:SC_PendingPublish = 0
            } else {
                $retryBackoff = [Math]::Min($retryBackoff * 2, $RETRY_MAX)
                $retryAt = $now.AddSeconds($retryBackoff)
                $script:SC_PendingPublish = Get-AheadCount
            }
        }

        # While somebody else is untangling a conflict, hold back the
        # AUTOMATIC publish - and only that. Nothing is locked: Publish now
        # and push-now.ps1 go out immediately, because a person or their
        # agent deciding to send something is a judgement, and this rule
        # exists to stop the machine making that judgement by itself. Every
        # commit that lands on the shared branch while somebody is resolving
        # is work they may have to rebase over, or conflict with again.
        #
        # With a ceiling, because an announcement can outlive the person who
        # made it - a machine that loses power mid-resolve would otherwise
        # freeze the whole team's automatic publishing for good. After this
        # long the engine says so and carries on.
        $CONFLICT_PAUSE_MAX = 600
        $pausedByOthers = $false
        if ($teamConflicts.Count -gt 0) {
            if (-not $conflictPauseSince) { $conflictPauseSince = $now }
            if (($now - $conflictPauseSince).TotalSeconds -lt $CONFLICT_PAUSE_MAX) {
                $pausedByOthers = $true
            } elseif (-not $conflictPauseGaveUp) {
                $conflictPauseGaveUp = $true
                Write-Log ("a conflict elsewhere has been open for " +
                           [int]($CONFLICT_PAUSE_MAX / 60) +
                           " minutes - publishing automatically again") 'Yellow'
            }
        } else {
            $conflictPauseSince = $null
            $conflictPauseGaveUp = $false
        }

        $due    = if ($publishASAP) { $SettleSeconds } else { $QuietSeconds }
        $reason = if ($publishASAP) { 'after conflict' } else { 'quiet window' }
        if ($pausedByOthers -and $global:TS_LastChange -and $quiet -ge $due) {
            if (-not $conflictPauseSaid) {
                $conflictPauseSaid = $true
                $who = ($teamConflicts.Keys | Sort-Object) -join ', '
                Write-Log ("holding back the automatic publish while $who resolves a " +
                           "conflict - press Publish now, or run push-now.ps1, to send it anyway") 'Yellow'
            }
        } elseif ($global:TS_LastChange -and $quiet -ge $due) {
            $conflictPauseSaid = $false
            $global:TS_LastChange = $null
            $publishASAP = $false
            if (Invoke-Publish -Reason $reason) {
                $retryAt = $null; $retryBackoff = 30; $script:SC_PendingPublish = 0
            } else {
                $retryAt = $now.AddSeconds($retryBackoff)
                $script:SC_PendingPublish = Get-AheadCount
                Write-Log "will retry publishing in $retryBackoff s" 'Yellow'
            }
        }
    }
}
finally {
    Clear-Pending
    Clear-Presence
    Get-EventSubscriber | Where-Object { $_.SourceObject -eq $fsw } | Unregister-Event
    $fsw.EnableRaisingEvents = $false
    $fsw.Dispose()
    Remove-Item -LiteralPath $script:SC_Lock -Force -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Log 'teamsync stopped.' 'Yellow'
}
