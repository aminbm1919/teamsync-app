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

# Single instance per folder. The daemon may be running detached in the background
# (the window was closed); starting a second one would race the first on every
# commit and rebase. The heartbeat rewrites the lock every second, so a fresh
# lock plus a live PID means "already covered".
if (Test-Path -LiteralPath $script:SC_Lock) {
    $lockAge = ((Get-Date) - (Get-Item -LiteralPath $script:SC_Lock).LastWriteTime).TotalSeconds
    $lockPid = ((Get-Content -LiteralPath $script:SC_Lock -ErrorAction SilentlyContinue |
                 Where-Object { $_ -like 'pid=*' }) -replace '^pid=', '')
    if ($lockAge -lt 30 -and $lockPid -and (Get-Process -Id $lockPid -ErrorAction SilentlyContinue)) {
        Write-Host "Another sync for this folder is already running (PID $lockPid). Nothing to do."
        exit 0
    }
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
            if (-not $wasBlocked) { Write-Log 'paused - conflict is open' 'Yellow'; $wasBlocked = $true }
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
        }

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
            if (-not $global:TS_LastChange -and -not $retryAt -and (Get-AheadCount) -gt 0) {
                if (Invoke-Publish -Reason 'catch-up') {
                    $script:SC_PendingPublish = 0
                } else {
                    $retryAt = $now.AddSeconds($retryBackoff)
                    $script:SC_PendingPublish = Get-AheadCount
                    Write-Log "will retry publishing in $retryBackoff s" 'Yellow'
                }
            }

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

        $due    = if ($publishASAP) { $SettleSeconds } else { $QuietSeconds }
        $reason = if ($publishASAP) { 'after conflict' } else { 'quiet window' }
        if ($global:TS_LastChange -and $quiet -ge $due) {
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
