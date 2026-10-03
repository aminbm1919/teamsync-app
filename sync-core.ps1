# sync-core - the shared engine. Dot-sourced by teamsync.ps1.
#
# Holds every operation that touches git, so the daemon and any other caller
# behave identically. Nothing here loops or waits; callers decide when.

function Initialize-SyncCore {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [string]$Branch = 'main',
        [switch]$NoPopup,
        [string]$AppVersion = ''
    )
    $script:SC_Repo         = $Repo
    $script:SC_AppVersion   = $AppVersion
    # Where the engine itself lives - the templates it plants sit beside it.
    $script:SC_Home         = $PSScriptRoot
    $script:SC_Branch       = $Branch
    $script:SC_NoPopup      = [bool]$NoPopup
    $script:SC_Log          = Join-Path $Repo '.teamsync.log'
    $script:SC_ConflictRoot = Join-Path $Repo '_conflicts'
    $script:SC_Lock         = Join-Path $Repo '.teamsync.lock'
    $script:SC_Signal       = Join-Path $Repo '.teamsync-push-now'
    # Inside .git, not beside the work: nothing there is ever committed, and
    # the file watcher ignores it - so a request to stop can never travel.
    $script:SC_StopSignal   = Join-Path (Join-Path $Repo '.git') 'teamsync-stop'
    $script:SC_Offline      = $false
    $script:SC_PendingPublish = 0
    Set-GitOutputUtf8
    Disable-GitPathQuoting
}

function Set-GitOutputUtf8 {
    # Git prints UTF-8. Windows PowerShell decodes whatever a program prints
    # with the CONSOLE's code page, and the console the app gives its engine
    # (CREATE_NO_WINDOW, no parent console) starts on the machine's OEM page -
    # 437 here, 720 or 1256 on a Persian or Arabic machine, UTF-8 almost
    # nowhere. Measured under exactly those launch conditions: a file named
    # یادداشت.md came back from `git diff --name-only` as
    # "█î╪º╪»╪»╪º╪┤╪¬.md", and an edited Persian file was reported as not
    # existing - so the destructive guard skipped it and the read-side hold
    # compared a garbled name with the real one.
    #
    # core.quotePath (below) was half of the cure: it made git print the real
    # name instead of escapes. This is the other half - reading those bytes as
    # what they are. Every test had passed because the test harness set UTF-8
    # itself before calling in, which is the one condition the field never has.
    try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { }
}

function Test-SharedProject {
    # Is this a project this app set up, or joined? The engine runs on nothing
    # else. An 'origin' alone proves nothing: it may be the person's own GitHub
    # repository - a public one, even - and syncing there would publish their
    # work, and this app's files, into it. Before 2.2.0 that is exactly what
    # opening such a folder did: the engine checked only that an origin
    # existed, found no 'main' on it, "started the project" by pushing the
    # whole local history there, and went on committing every four minutes.
    #
    # The files the app plants are the local sign. Without them, the server is
    # asked: an EMPTY repository is fine to start (there is nothing in it to
    # overwrite - a joiner of a fresh project lands here), and one carrying
    # this app's own refs is a project in use. Anything else is refused.
    # Returns $true, $false, or $null when the server could not be asked.
    param([string]$Root)
    foreach ($m in 'push-now.ps1', 'TEAM-PROJECT-REFERENCE.md') {
        if (Test-Path -LiteralPath (Join-Path $Root $m)) { return $true }
    }
    $heads = git ls-remote --heads origin 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    if (-not $heads) { return $true }
    $ours = git ls-remote origin 'refs/teamsync/*' 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return [bool]$ours
}

function Disable-GitPathQuoting {
    # Git escapes non-ASCII paths in its output by default: a file called
    # "فصل-اول/یادداشت.md" comes back from `git diff --name-only` as
    # "\331\201\330\265\331\204-...". Everything else in this engine speaks
    # the real name - the editor extension, working.ps1, the pending refs -
    # so the two lists could never match.
    #
    # That is not cosmetic. The read-side hold compares "what is arriving"
    # against "what is in my hands", and with a non-ASCII name the comparison
    # silently found no overlap: the incoming change was NOT held and landed
    # on top of work in progress. Measured on a real two-clone repository.
    # The whole point of this project's paths being Persian is that this was
    # never a rare case here.
    #
    # Set once on the repository rather than passed at each call site,
    # because a call site added later would forget it, and the failure is
    # invisible when it happens.
    git config core.quotePath false 2>$null | Out-Null
}

function Invoke-LogRotation {
    # Keep the live log a readable page, not an archive. Everything beyond the
    # newest 100 lines moves to .teamsync-history.log (per project, local to
    # this machine). Runs at engine start and at each date change, so the live
    # log holds roughly today plus never fewer than the last 100 lines - the
    # full past stays one History button away.
    try {
        if (-not (Test-Path -LiteralPath $script:SC_Log)) { return }
        $lines = @(Get-Content -LiteralPath $script:SC_Log)
        if ($lines.Count -le 100) { return }
        $old  = $lines[0..($lines.Count - 101)]
        $keep = $lines[($lines.Count - 100)..($lines.Count - 1)]
        $hist = Join-Path $script:SC_Repo '.teamsync-history.log'
        [IO.File]::AppendAllLines($hist, [string[]](
            @("== moved to history $((Get-Date).ToString('yyyy-MM-dd HH:mm')) ==") + $old),
            (New-Object Text.UTF8Encoding $false))
        [IO.File]::WriteAllLines($script:SC_Log, [string[]]$keep,
            (New-Object Text.UTF8Encoding $false))
    } catch { }
}

function Write-Log {
    param([string]$Text, [string]$Color = 'Gray')
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Text
    Write-Host $line -ForegroundColor $Color
    try { Add-Content -LiteralPath $script:SC_Log -Value $line -Encoding UTF8 } catch { }
}

# --- cheap change detection ---------------------------------------------------
# Asking "has anything changed?" with an ETag costs a 304 and nothing else - a
# 304 does not count against the API rate limit at all (measured: remaining stayed
# 4991 across both calls). A git fetch costs ~2.3 s on this connection; the
# conditional check costs ~0.4 s. So we ask often and fetch only when the answer
# is yes: lower latency AND less traffic than polling with fetch.
#
# If anything about this is unavailable - no gh, no token, no network - the
# function reports "no news" and the slower periodic fetch still covers us.

function Initialize-RemoteWatch {
    $script:SC_Http = $null
    $script:SC_Slug = ''
    $script:SC_ETagRefs = $null

    $url = git remote get-url origin 2>$null
    if ($url -match 'github\.com[:/](.+?)(\.git)?$') { $script:SC_Slug = $Matches[1].Trim('/') }
    if (-not $script:SC_Slug) { return }

    $token = $null
    try { $token = (gh auth token 2>$null) } catch { }
    if (-not $token) { return }

    try {
        Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
        $c = New-Object System.Net.Http.HttpClient
        $c.Timeout = [TimeSpan]::FromSeconds(15)
        $c.DefaultRequestHeaders.Add('Authorization', "Bearer $token")
        $c.DefaultRequestHeaders.Add('User-Agent', 'teamsync')
        $c.DefaultRequestHeaders.Add('Accept', 'application/vnd.github+json')
        $script:SC_Http = $c
    } catch { $script:SC_Http = $null }
}

function Test-RemoteWatchAvailable { [bool]$script:SC_Http }

function Test-RemoteChanged {
    # Watches ALL refs, not just the branch: the partner's presence and
    # "unpublished work" markers are refs too, and those are what we want to see
    # quickly. Returns $false when nothing changed or when we cannot tell.
    if (-not $script:SC_Http) { return $false }
    $u = "https://api.github.com/repos/$($script:SC_Slug)/git/matching-refs/"
    try {
        $req = New-Object System.Net.Http.HttpRequestMessage ([System.Net.Http.HttpMethod]::Get, $u)
        if ($script:SC_ETagRefs) { $req.Headers.TryAddWithoutValidation('If-None-Match', $script:SC_ETagRefs) | Out-Null }
        $res = $script:SC_Http.SendAsync($req).Result
        if ([int]$res.StatusCode -eq 304) { return $false }
        if ($res.IsSuccessStatusCode) {
            if ($res.Headers.ETag) { $script:SC_ETagRefs = $res.Headers.ETag.ToString() }
            return $true
        }
        return $false
    } catch {
        return $false            # offline: the periodic fetch is the safety net
    }
}

function Set-NetState {
    # Log only the TRANSITIONS. A dropped VPN would otherwise write "fetch failed"
    # every ten seconds forever and bury everything else in the log.
    param([bool]$Ok)
    if ($Ok) {
        if ($script:SC_Offline) {
            Write-Log 'network is back - catching up' 'Green'
            $script:SC_Offline = $false
        }
    } else {
        if (-not $script:SC_Offline) {
            Write-Log 'cannot reach GitHub (VPN or network) - nothing is lost, still retrying' 'Yellow'
            $script:SC_Offline = $true
        }
    }
}

function Get-ProcessStart {
    # A process's start time, as a sortable string, or '' if it cannot be read.
    param([int]$ProcessId)
    try {
        $p = Get-Process -Id $ProcessId -ErrorAction Stop
        return $p.StartTime.ToString('o')
    } catch { return '' }
}

$script:SC_StartTime = Get-ProcessStart $PID

function Test-DaemonAlive {
    # Is an engine already running for this folder? Returns its pid, or 0.
    #
    # The old rule was "the lock is younger than 30 seconds AND its pid is
    # alive", and the AND was the defect. The engine writes the heartbeat once
    # per pass, and one pass can take far longer than 30 seconds when it has
    # many network round trips to make - so a perfectly healthy engine looked
    # dead, and a SECOND one was started on the same folder. Two engines then
    # commit and rebase the same worktree at once, which is the one thing git
    # cannot survive.
    #
    # So the pid decides, and the recorded start time keeps that honest: a
    # recycled id belongs to a process that started at another moment. Only
    # when the lock predates this scheme, or the start time cannot be read,
    # does the old timestamp rule stand in.
    param([string]$LockPath)
    if (-not (Test-Path -LiteralPath $LockPath)) { return 0 }
    $lines = @(Get-Content -LiteralPath $LockPath -ErrorAction SilentlyContinue)
    $field = { param($k) (($lines | Where-Object { $_ -like "$k=*" }) -replace "^$k=", '') }
    $lockPid = & $field 'pid'
    if (-not $lockPid) { return 0 }
    $proc = Get-Process -Id $lockPid -ErrorAction SilentlyContinue
    if (-not $proc) { return 0 }                      # the process is gone: free

    $recorded = & $field 'started'
    if ($recorded) {
        $actual = Get-ProcessStart ([int]$lockPid)
        # Same id AND same birth: certainly the engine that wrote this lock,
        # however long ago it last managed to breathe.
        if ($actual -and $actual -eq $recorded) { return [int]$lockPid }
        return 0                                      # id was reused by something else
    }

    # A lock from before the start time was recorded. Fall back to the old
    # rule rather than guessing: better a rare false "busy" than a second
    # engine.
    $age = ((Get-Date) - (Get-Item -LiteralPath $LockPath).LastWriteTime).TotalSeconds
    if ($age -lt 30) { return [int]$lockPid }
    return 0
}

function Receive-StopRequest {
    # Has the app asked this engine to stop? '' if not; 'stop' for the
    # person's Stop sync, Disconnect or a move; 'restart' when the app moves it
    # onto a newer build. Asked at the top of a pass, at rest - never in the
    # middle of a git command, which the app cannot see from outside: ended
    # mid-command, git can leave its lock files or a rebase half done.
    #
    # The request is TAKEN by deleting it, and the app withdraws one the same
    # way, so whichever delete succeeds decides: a request the app took back
    # is never half acted on.
    $f = $script:SC_StopSignal
    if (-not $f -or -not (Test-Path -LiteralPath $f)) { return '' }
    $what = ''
    try { $what = [IO.File]::ReadAllText($f).Trim() } catch { }
    try { Remove-Item -LiteralPath $f -Force -ErrorAction Stop } catch { return '' }
    if ($what -eq 'restart') { return 'restart' }
    return 'stop'
}

function Update-Heartbeat {
    # Lets push-now.ps1 (and the UI) know a daemon is alive and listening.
    try {
        # Written without a byte-order mark. Set-Content -Encoding UTF8 adds one
        # under Windows PowerShell 5.1 and not under pwsh 7, and the reader that
        # looks for a line starting with 'pid=' never sees it behind a mark.
        $lockLines = @(
            "pid=$PID"
            # Windows hands out process ids again after a process ends, so a
            # pid on its own cannot prove OUR engine is the one alive. The
            # start time pins it: a recycled id belongs to a process that
            # began at a different moment. This is what lets a live engine be
            # recognised even when its heartbeat has gone quiet - see
            # Test-DaemonAlive.
            "started=$($script:SC_StartTime)"
            "version=$($script:SC_AppVersion)"
            # This engine stops itself when asked (Receive-StopRequest), so
            # the app asks instead of choosing a moment to end it from outside.
            "stop=signal"
            "time=$((Get-Date).ToString('o'))"
            "branch=$($script:SC_Branch)"
            "net=$(if ($script:SC_Offline) { 'offline' } else { 'online' })"
            "pending=$($script:SC_PendingPublish)"
        )
        [IO.File]::WriteAllLines($script:SC_Lock, $lockLines,
                                 (New-Object Text.UTF8Encoding $false))
    } catch { }
}

function Test-Prerequisites {
    # Say what is missing and how to get it, in plain language. Without this the
    # user sees a raw "The term 'gh' is not recognized" exception, which tells
    # them nothing about what to install.
    $missing = @()
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        $missing += "  Git for Windows  ->  https://git-scm.com/download/win"
    }
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        $missing += "  GitHub CLI       ->  https://cli.github.com/"
    }
    if ($missing.Count -gt 0) {
        Write-Host ''
        Write-Host 'MISSING PROGRAMS - nothing was changed.' -ForegroundColor Red
        Write-Host ''
        Write-Host 'This needs two free programs that are not installed yet:' -ForegroundColor Yellow
        $missing | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
        Write-Host ''
        Write-Host 'Install them, CLOSE AND REOPEN this app (so it sees them), then:' -ForegroundColor Yellow
        Write-Host '  1. open a terminal and run:  gh auth login' -ForegroundColor Yellow
        Write-Host '  2. choose GitHub.com, then HTTPS, then log in through the browser' -ForegroundColor Yellow
        Write-Host '  3. try again here' -ForegroundColor Yellow
        Write-Host ''
        return $false
    }

    gh auth status 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host 'NOT SIGNED IN TO GITHUB - nothing was changed.' -ForegroundColor Red
        Write-Host ''
        Write-Host 'Open a terminal and run:  gh auth login' -ForegroundColor Yellow
        Write-Host 'Choose GitHub.com, then HTTPS, then log in through the browser.' -ForegroundColor Yellow
        Write-Host 'Then try again here.' -ForegroundColor Yellow
        Write-Host ''
        return $false
    }
    return $true
}

# --- presence -----------------------------------------------------------------
# Each side publishes a heartbeat as a git ref whose NAME carries the timestamp:
#   refs/teamsync/presence/<name>/<unix-seconds>
# A ref holds no date of its own, so the name is the payload. This costs no
# commits and never touches the project history - the refs live outside it.

function Get-PresenceName {
    $n = git config user.name
    if (-not $n) { $n = $env:USERNAME }
    ($n -replace '[^A-Za-z0-9._-]+', '-').Trim('-')
}

function Get-MyGitHubLogin {
    # The GitHub account this machine is signed in as, or '' offline.
    if ($null -ne $script:SC_Login) { return $script:SC_Login }
    $script:SC_Login = ''
    try {
        $out = gh api user --jq '.login' 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { $script:SC_Login = "$out".Trim() }
    } catch { }
    $script:SC_Login
}

function Get-MarkerSource {
    # What a marker ref - identity, presence, a pending file, a conflict -
    # points at. Only its NAME carries the news, but pushing it uploads
    # whatever it points at that GitHub does not have yet, and every
    # teammate's fetch downloads it again. These used to point at HEAD, which
    # can hold commits made here and not yet sent: something committed by
    # hand reached GitHub and every teammate through a heartbeat, without
    # ever passing the publish guards. The shared branch's tip is already
    # there, so a marker carries nothing. '' when the project has no shared
    # branch yet - then there is nothing to point at, and no marker is sent.
    $src = "refs/remotes/origin/$($script:SC_Branch)"
    git rev-parse -q --verify $src 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return $src }
    return ''
}

function Publish-Identity {
    # Say which GitHub account is behind this name.
    #
    # It answers the one question a bare name cannot: two machines publishing
    # as "amin" are either one person at their desk and their laptop, or two
    # different people who happen to share a name. The first is fine and only
    # needs telling apart; the second silently destroys both people's
    # warnings. The account is what separates them.
    #
    #   refs/teamsync/identity/<name>/<github-login>
    $me = Get-PresenceName
    $login = Get-MyGitHubLogin
    if (-not $me -or -not $login) { return }
    $ref = "refs/teamsync/identity/$me/$login"
    if ($script:SC_IdentityRef -eq $ref) { return }
    $src = Get-MarkerSource
    if (-not $src) { return }
    git push -q origin "${src}:$ref" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $script:SC_IdentityRef = $ref }
}

function Get-NameOwner {
    # Which GitHub account already publishes under this name, or '' if none.
    param([string]$Name)
    git fetch -q --prune origin '+refs/teamsync/identity/*:refs/teamsync/identity/*' 2>$null | Out-Null
    foreach ($line in @(git ls-remote origin "refs/teamsync/identity/*" 2>$null)) {
        $ref = ($line -split "`t")[-1]
        $parts = $ref -split '/'
        if ($parts.Count -lt 5) { continue }
        if ($parts[3] -eq $Name) { return $parts[4] }   # -eq: case-folding filesystems
    }
    return ''
}

function Resolve-MyName {
    # Settle this machine's name before anything is published under it.
    #
    # **One GitHub account, one name - however many computers it works from.**
    # That is the owner's rule and it is the right one: the unit of
    # collaboration is the PERSON, not the desk they happen to be sitting at.
    # Teammates want to know that Amin is here, not which of Amin's laptops.
    #
    # An earlier version numbered the second machine (amin, amin-2). It cost
    # two live defects in one day - the app renaming itself on its own leftover
    # heartbeat after a restart, and again on the single restart that crossed
    # an upgrade - and both times a person watched themselves appear in their
    # own team list. Machines are told apart INSIDE the ref path now, where
    # nobody has to look at it.
    #
    # So only one thing is still refused: a different GitHub ACCOUNT already
    # publishing under this name. Those two would delete each other's presence
    # and each other's file warnings without end, and neither would be told.
    #
    # Returns @{ Name; Action = 'ok'|'refused'; Owner }
    $me = Get-PresenceName
    if (-not $me) { return @{ Name = ''; Action = 'ok' } }
    $mine  = Get-MyGitHubLogin
    $owner = Get-NameOwner $me

    if ($owner -and $mine -and $owner -ne $mine) {
        return @{ Name = $me; Action = 'refused'; Owner = $owner }
    }

    # A numbered name left behind by the version that used to rename. Give it
    # back, quietly: the person never asked for it, and leaving it would make
    # them two people on everybody's screen for ever.
    $came = git config --local teamsync.renamedfrom 2>$null
    if ($came) {
        $came = "$came".Trim()
        $owner0 = Get-NameOwner $came
        if (-not $owner0 -or -not $mine -or $owner0 -eq $mine) {
            Clear-MyPresence -Name $me
            Clear-MyPending  -Name $me
            git config user.name $came 2>$null | Out-Null
            git config --local --unset teamsync.renamedfrom 2>$null | Out-Null
            $script:SC_IdentityRef = $null
            return @{ Name = $came; Action = 'restored'; From = $me }
        }
    }

    return @{ Name = $me; Action = 'ok' }
}

function Clear-MyPresence {
    # Every presence beat this MACHINE has under a name. Other machines of the
    # same person publish under the same name and must not be touched - which
    # is exactly why the machine is in the ref path.
    param([string]$Name)
    $mid = Get-MachineId
    foreach ($line in @(git ls-remote origin "refs/teamsync/presence/$Name/*" 2>$null)) {
        $r = ($line -split "`t")[-1]
        if (-not $r) { continue }
        $b = Split-PresenceRef $r
        # An unlabelled beat was written before machines were recorded. Under
        # a name we are abandoning it can only be ours.
        if ($b -and ($b.Machine -eq $mid -or -not $b.Machine)) {
            git push -q origin ":$r" 2>$null | Out-Null
        }
    }
}

function Clear-MyPending {
    param([string]$Name)
    $mid = Get-MachineId
    foreach ($ns in 'pending', 'conflict') {
        foreach ($line in @(git ls-remote origin "refs/teamsync/$ns/$Name/*" 2>$null)) {
            $r = ($line -split "`t")[-1]
            if (-not $r) { continue }
            $p = $r -split '/'
            # <ns>/<name>/<machine>/<hex> is 6 parts; the older <ns>/<name>/<hex>
            # is 5 and can only be ours under a name we are giving up.
            if ($p.Count -lt 6 -or $p[4] -eq $mid) {
                git push -q origin ":$r" 2>$null | Out-Null
            }
        }
    }
}

function Get-MachineId {
    # A stable token for THIS clone on THIS machine, kept in the project's
    # local git config, which never travels.
    #
    # Remembering "the last beat I published" was not enough, and the gap was
    # exactly one restart wide: the first start after an upgrade has no
    # memory yet, while the beat the previous version left behind is still
    # live - so the app renamed itself on its own shadow anyway. A beat that
    # SAYS which machine wrote it needs no memory at all.
    $id = git config --local teamsync.machine 2>$null
    if ($id) { return "$id".Trim() }
    $id = [Guid]::NewGuid().ToString('N').Substring(0, 12)
    git config --local teamsync.machine $id 2>$null | Out-Null
    return $id
}

function Split-PresenceRef {
    # refs/teamsync/presence/<name>/<machine>/<ts>   (this version)
    # refs/teamsync/presence/<name>/<ts>             (before it)
    #
    # The timestamp is the LAST segment in both shapes, which is what lets
    # one reader serve a team that is mid-upgrade. The machine is present
    # only in the new shape; '' means "written by a version that could not
    # say".
    param([string]$Ref)
    $p = $Ref -split '/'
    if ($p.Count -lt 5) { return $null }
    $ts = 0
    [void][long]::TryParse($p[-1], [ref]$ts)
    if ($ts -le 0) { return $null }
    @{ Name = $p[3]; Machine = $(if ($p.Count -ge 6) { $p[4] } else { '' }); Ts = $ts }
}

function Set-MyLastPresenceRef {
    param([string]$Ref)
    $script:SC_MyPresenceRef = $Ref
}

function Publish-Presence {
    $me = Get-PresenceName
    if (-not $me) { return }
    $ts  = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    # The machine goes IN the ref, so nobody has to remember whose beat it is.
    $new = "refs/teamsync/presence/$me/$(Get-MachineId)/$ts"
    $old = $script:SC_MyPresenceRef

    $src = Get-MarkerSource
    if (-not $src) { return }
    git push -q origin "${src}:$new" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { return }          # offline; try again next tick
    Set-MyLastPresenceRef $new

    if ($old -and $old -ne $new) {
        # Drop the previous beat separately, so its failure cannot cancel the new one.
        git push -q origin ":$old" 2>$null | Out-Null
    }

    if (-not $script:SC_SweptThisRun) {
        # First beat of this RUN - and it must be judged by the run, not by
        # whether a previous beat is remembered. Since 2.1.1 the last beat is
        # kept in git config so it survives a restart, which quietly meant this
        # sweep stopped running at all: the pile-up it exists to prevent was
        # being remembered rather than cleaned.
        $script:SC_SweptThisRun = $true
        Clear-DeadBeats -Keep $new -Now $ts
    }
}

function Get-MyOwnNames {
    # Every name THIS ACCOUNT has registered, from the identity refs on the
    # server. The local config cannot know a name retired on an earlier
    # install, or one an older version invented for a machine that never
    # existed - but that name still carries our login on the wire.
    $me = Get-PresenceName
    $names = @{}
    if ($me) { $names[$me] = $true }
    $login = Get-MyGitHubLogin
    if ($login) {
        foreach ($line in @(git ls-remote origin "refs/teamsync/identity/*" 2>$null)) {
            $r = ($line -split "`t")[-1]
            if (-not $r) { continue }
            $p = $r -split '/'
            # refs/teamsync/identity/<name>/<login>
            if ($p.Count -lt 5) { continue }
            if ($p[4] -ceq $login) { $names[$p[3]] = $true }
        }
    }
    return @($names.Keys)
}

function Clear-DeadBeats {
    # Remove heartbeats that belong to this account and cannot be alive.
    #
    # A run that was killed - window closed, machine powered off - never tidied
    # up, so its last beat is still there. Nothing else will ever remove it:
    # each engine sweeps only what it recognises as its own, so a beat under a
    # name nobody uses any more is orphaned for good and every reader goes on
    # listing it as a person. Measured on the user's own screen: "amin-2",
    # a name no machine had ever really used, still shown as a teammate.
    param([string]$Keep, [long]$Now)
    $mid = Get-MachineId
    $me  = Get-PresenceName
    foreach ($name in (Get-MyOwnNames)) {
        $survivors = 0
        foreach ($line in @(git ls-remote origin "refs/teamsync/presence/$name/*" 2>$null)) {
            $r = ($line -split "`t")[-1]
            if (-not $r) { continue }
            if ($r -eq $Keep) { $survivors++; continue }
            $b = Split-PresenceRef $r
            if (-not $b) { $survivors++; continue }
            # This machine's own leftovers go without question. Everything else
            # must be STALE first: a fresh beat under one of our other names
            # could be a second computer of ours still on an old build, and
            # deleting it would make that machine vanish from the team.
            $isMine  = ($b.Machine -ceq $mid)
            $isStale = (($Now - $b.Ts) -gt 150)
            if ($isMine -or $isStale) {
                git push -q origin ":$r" 2>$null | Out-Null
                if ($LASTEXITCODE -ne 0) { $survivors++ }
            } else {
                $survivors++
            }
        }
        # The retired name's registration goes too - but ONLY once nothing is
        # left beating under it. That ref is what tells every reader the name
        # is ours; dropping it while a beat survives would turn that beat back
        # into a stranger, which is the exact bug this is here to end.
        if ($name -cne $me -and $survivors -eq 0) {
            git push -q origin ":refs/teamsync/identity/$name/$(Get-MyGitHubLogin)" 2>$null | Out-Null
        }
    }
}

function Sync-PresenceRefs {
    # --prune matters: without it a beat that the other side deleted lingers here
    # and they would look online forever.
    git fetch -q --prune origin '+refs/teamsync/presence/*:refs/teamsync/presence/*' 2>$null | Out-Null
}

function Clear-Presence {
    # On a clean stop, remove our beat so the other side sees us drop off at once
    # instead of waiting for it to go stale.
    if ($script:SC_MyPresenceRef) {
        git push -q origin ":$($script:SC_MyPresenceRef)" 2>$null | Out-Null
        $script:SC_MyPresenceRef = $null
    }
}

# --- who is changing what -----------------------------------------------------
# A file that has unpublished work on it is exactly the file that will collide if
# the other person edits it too. So that, and nothing else, is what we announce.
#
# It needs no declaration, no timer and no release: git already knows. A file
# becomes "pending" the moment it is saved, and stops being pending the moment it
# is published. Nobody can forget to switch it off.
#
# It travels as refs, so it adds no commits to the project history:
#   refs/teamsync/pending/<name>/<path-as-hex>
# The path is hex-encoded because ref names may not contain most punctuation.

function ConvertTo-RefHex {
    param([string]$Text)
    ([BitConverter]::ToString([Text.Encoding]::UTF8.GetBytes($Text)) -replace '-', '').ToLower()
}

function ConvertFrom-RefHex {
    param([string]$Hex)
    try {
        $bytes = [byte[]]::new($Hex.Length / 2)
        for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($Hex.Substring($i * 2, 2), 16) }
        [Text.Encoding]::UTF8.GetString($bytes)
    } catch { '' }
}

function Read-PresenceReport {
    param([string]$Name, [int]$MaxAge)
    $f = Join-Path $script:SC_Repo $Name
    if (-not (Test-Path -LiteralPath $f)) { return @() }
    try {
        # -Encoding UTF8, because both writers speak UTF-8 and neither marks
        # it: the VS Code extension uses Node's default, and working.ps1
        # writes it deliberately without a byte-order mark. Read without this,
        # Windows PowerShell 5.1 falls back to the ANSI codepage and a Persian
        # file name comes back as mojibake - measured. The engine then holds a
        # path that exists nowhere and leaves the real one unguarded.
        $j = Get-Content -LiteralPath $f -Raw -Encoding UTF8 | ConvertFrom-Json
        $age = [Math]::Abs(((Get-Date) - ([datetime]$j.updated)).TotalSeconds)
        if ($age -gt $MaxAge) { return @() }
        return @($j.open)
    } catch { return @() }
}

function Get-EditorReport {
    # Hands-on-files, from the two witnesses that can actually see them.
    # Windows keeps no record of a file merely being open, so the knowledge
    # comes from inside: the VS Code extension reports the human's open tabs
    # and unsaved typing (heartbeat 10 s, stale after 45), and working.ps1
    # carries the agent's own announcement (stale after 15 minutes - a crashed
    # agent must not hold the door forever; push-now clears it on publish).
    # Open = every file with hands on it; Dirty = the subset whose content is
    # mid-flight (unsaved typing, or an agent composing its write).
    $entries = @(Read-PresenceReport '.teamsync-editor.json' 45) +
               @(Read-PresenceReport '.teamsync-agent.json' 900)
    return @{
        Open  = @($entries | ForEach-Object { $_.f } | Sort-Object -Unique)
        Dirty = @($entries | Where-Object { $_.dirty } | ForEach-Object { $_.f } | Sort-Object -Unique)
    }
}

function Get-PendingFiles {
    # Everything this machine has that the other machine does not yet have:
    # saved but not committed, and committed but not pushed.
    $set = @{}
    foreach ($line in @(git status --porcelain 2>$null)) {
        if ($line.Length -le 3) { continue }
        $p = $line.Substring(3).Trim().Trim('"')
        if ($p -match '\s->\s') { $p = ($p -split '\s->\s')[-1].Trim().Trim('"') }   # renames
        if ($p) { $set[$p] = $true }
    }
    # Three dots, not two: what is on MY side since we last agreed, not the
    # difference between the two branches (which would include their work too).
    foreach ($f in @(git diff --name-only "origin/$($script:SC_Branch)...HEAD" 2>$null)) {
        if ($f) { $set[$f] = $true }
    }
    @($set.Keys | Sort-Object)
}

function Publish-Pending {
    # Reconcile: push what is newly pending, withdraw what no longer is. The ref
    # name has no timestamp in it, so nothing churns while the set is unchanged.
    $me = Get-PresenceName
    if (-not $me) { return }

    $mid = Get-MachineId
    $want = @{}
    # Pending work AND files simply open in the editor: teammates should see
    # "hands on this file" from the moment of opening, not the first save.
    #
    # The MACHINE is in the path. One person may work from two computers under
    # one name, and this reconcile deletes every ref under its own prefix that
    # it does not itself want - so without the machine segment, their desk
    # would erase their laptop's announcements every minute, and each machine
    # would leave the other's files unguarded.
    $announce = @(Get-PendingFiles) + @((Get-EditorReport).Open) | Sort-Object -Unique
    foreach ($f in $announce) { $want["refs/teamsync/pending/$me/$mid/$(ConvertTo-RefHex $f)"] = $true }

    $have = @{}
    foreach ($line in @(git ls-remote origin "refs/teamsync/pending/$me/$mid/*" 2>$null)) {
        $r = ($line -split "`t")[-1]
        if ($r) { $have[$r] = $true }
    }
    if ($LASTEXITCODE -ne 0) { return }          # offline: try again next tick

    # One push per ref, each a network round trip of a couple of seconds. Open
    # a fifteen-file folder in an editor and this loop alone runs for most of a
    # minute. Breathe between them: the heartbeat is what tells the window and
    # push-now that this engine is alive, and letting it go quiet during
    # ordinary work is what made a healthy engine look dead.
    $src = Get-MarkerSource
    foreach ($r in $want.Keys) {
        if ($src -and -not $have.ContainsKey($r)) {
            git push -q origin "${src}:$r" 2>$null | Out-Null
            Update-Heartbeat
        }
    }
    foreach ($r in $have.Keys) {
        if (-not $want.ContainsKey($r)) {
            git push -q origin ":$r" 2>$null | Out-Null
            Update-Heartbeat
        }
    }
}

# --- who is stuck on what ------------------------------------------------------
# A conflict is LOCAL: it happens on one machine, when that person's work is
# replayed onto the shared branch. Everyone else's repository is perfectly
# healthy and origin/<branch> is a consistent state - which is why nothing
# here stops anybody else from working. What the others lack is knowledge:
# that a file is being untangled right now, by a named person, so piling more
# changes onto it will make their job harder and probably cause the next
# conflict.
#
# So it travels the same way presence and pending do - as refs, costing no
# commits:
#   refs/teamsync/conflict/<name>/<machine>/<path-as-hex>
#
# The machine is in the path for the same reason it is in the pending refs: a
# conflict happens on ONE computer, and a person may have two under one name.

function Publish-Conflict {
    param([string[]]$Files)
    $me = Get-PresenceName
    if (-not $me) { return }
    $mid = Get-MachineId
    $want = @{}
    foreach ($f in $Files) { if ($f) { $want["refs/teamsync/conflict/$me/$mid/$(ConvertTo-RefHex $f)"] = $true } }

    $have = @{}
    foreach ($line in @(git ls-remote origin "refs/teamsync/conflict/$me/$mid/*" 2>$null)) {
        $r = ($line -split "`t")[-1]
        if ($r) { $have[$r] = $true }
    }
    if ($LASTEXITCODE -ne 0) { return }          # offline: try again next tick

    $src = Get-MarkerSource
    foreach ($r in $want.Keys) {
        if ($src -and -not $have.ContainsKey($r)) { git push -q origin "${src}:$r" 2>$null | Out-Null; Update-Heartbeat }
    }
    foreach ($r in $have.Keys) {
        if (-not $want.ContainsKey($r)) { git push -q origin ":$r" 2>$null | Out-Null; Update-Heartbeat }
    }
}

function Publish-ConflictWork {
    # Make the stuck person's OWN side reachable by everybody else.
    #
    # Two of the three versions are already in every clone: THEIRS is the
    # shared branch, BASE is the merge base. The only one nobody else can see
    # is MINE - the commits being replayed, which by definition were never
    # pushed. One ref fixes that, and then any teammate can read the whole
    # conflict with plain git and, if they want, write the final version
    # themselves and publish it normally.
    #
    # It points at the pre-rebase tip, which git records for us: mid-rebase
    # HEAD is somewhere in the middle of the replay and would show a partial
    # picture.
    #
    # Those commits travel to GitHub and into every teammate's fetch - an
    # upload like a publish - so large content in them that nobody agreed to
    # send keeps MINE here. The conflict itself is still announced.
    $me = Get-PresenceName
    if (-not $me) { return }
    $ref = "refs/teamsync/conflictwork/$me"
    $orig = Get-RebaseOrigHead
    if (-not $orig) { git push -q origin ":$ref" 2>$null | Out-Null; return }
    try {
        $large = @(Get-LargeOutgoingIn -Root $script:SC_Repo -Upstream "refs/remotes/origin/$($script:SC_Branch)" -Tip $orig -CommittedOnly)
    } catch {
        return
    }
    if ($large.Count -gt 0 -and -not (Test-LargeApproved $large)) {
        $sig = Get-LargeSignature $large
        if ($script:SC_ConflictLargeSaid -ne $sig) {
            $script:SC_ConflictLargeSaid = $sig
            Write-Log "your side of the conflict stays on this machine - it carries large content nobody agreed to send: $(@($large | ForEach-Object { $_.Item }) -join ', ')" 'Yellow'
        }
        git push -q origin ":$ref" 2>$null | Out-Null
        return
    }
    git push -q -f origin "${orig}:$ref" 2>$null | Out-Null
}

function Clear-ConflictWork {
    $me = Get-PresenceName
    if (-not $me) { return }
    git push -q origin ":refs/teamsync/conflictwork/$me" 2>$null | Out-Null
}

function Sync-ConflictRefs {
    git fetch -q --prune origin '+refs/teamsync/conflict/*:refs/teamsync/conflict/*' 2>$null | Out-Null
    git fetch -q --prune origin '+refs/teamsync/conflictwork/*:refs/teamsync/conflictwork/*' 2>$null | Out-Null
    git fetch -q --prune origin '+refs/teamsync/volunteer/*:refs/teamsync/volunteer/*' 2>$null | Out-Null
}

function Clear-Volunteers {
    # Every claim on OUR conflicts, dropped when they are over. The person who
    # volunteered may have closed their window long ago; leaving the claim
    # standing would make the next conflict on the same file look taken by
    # somebody who is not thinking about it.
    $me = Get-PresenceName
    if (-not $me) { return }
    foreach ($line in @(git ls-remote origin "refs/teamsync/volunteer/$me/*" 2>$null)) {
        $ref = ($line -split "`t")[-1]
        if ($ref) { git push -q origin ":$ref" 2>$null | Out-Null }
    }
}

function Get-TeamConflicts {
    # @{ name = @(paths) } for every PERSON who is not us.
    #
    # By NAME, not by machine - unlike the pending warnings. A conflict is
    # already readable and resolvable by everybody through the Conflicts
    # window, so there is nothing here for one's own other machine to add;
    # and the app shows one's OWN conflict from the local unmerged files
    # already, so filtering by machine would only make it appear twice.
    $me  = Get-PresenceName
    $out = @{}
    foreach ($line in @(git for-each-ref --format='%(refname)' 'refs/teamsync/conflict' 2>$null)) {
        $parts = $line -split '/'
        if ($parts.Count -lt 5) { continue }
        $who = $parts[3]
        if ($who -ceq $me) { continue }
        $p = ConvertFrom-RefHex $parts[-1]
        if (-not $p) { continue }
        if (-not $out.ContainsKey($who)) { $out[$who] = @() }
        $out[$who] += $p
    }
    $out
}

function Sync-PendingRefs {
    # --prune matters: without it a file the other side has already published
    # would look like it is still being worked on, forever.
    git fetch -q --prune origin '+refs/teamsync/pending/*:refs/teamsync/pending/*' 2>$null | Out-Null
}

function Get-PartnerPending {
    # @{ name = @(paths) } for every PERSON who is not us - all of our own
    # machines included in "us".
    $me  = Get-PresenceName
    $out = @{}
    foreach ($line in @(git for-each-ref --format='%(refname)' 'refs/teamsync/pending' 2>$null)) {
        $parts = $line -split '/'
        if ($parts.Count -lt 5) { continue }
        $who = $parts[3]
        # Skip our own NAME - which means all of our own machines.
        #
        # A person's computers are ONE identity here, merged as far as they
        # can be, so that work started on one is picked up on another. Warning
        # somebody about a file they themselves have open elsewhere is not
        # safety, it is noise: there is only one head editing both, and it
        # already knows.
        #
        # The machine still appears IN the ref path, but only so the two do
        # not delete each other's announcements during the reconcile - never
        # to make them strangers.
        #
        # -ceq, not -eq: PowerShell's plain comparison ignores case, so
        # 'Ali-Reza' -eq 'ali-reza' is True while git's ref store treats them
        # as two real people.
        if ($who -ceq $me) { continue }
        # The file is the LAST segment. Newer refs carry the machine in
        # between, and reading position 4 would decode the machine id as a
        # filename.
        $path = ConvertFrom-RefHex $parts[-1]
        if (-not $path) { continue }
        if (-not $out.ContainsKey($who)) { $out[$who] = @() }
        $out[$who] += $path
    }
    $out
}

function Clear-Pending {
    $me = Get-PresenceName
    if (-not $me) { return }
    foreach ($line in @(git ls-remote origin "refs/teamsync/pending/$me/*" 2>$null)) {
        $r = ($line -split "`t")[-1]
        if ($r) { git push -q origin ":$r" 2>$null | Out-Null }
    }
}

function Test-Rebasing {
    $g = git rev-parse --git-dir 2>$null
    if (-not $g) { return $false }
    return (Test-Path (Join-Path $g 'rebase-merge')) -or (Test-Path (Join-Path $g 'rebase-apply'))
}

function Get-Unmerged { @(git diff --name-only --diff-filter=U 2>$null) }

function Test-RemoteBranch {
    # Three answers, not two: 'yes' the branch is there, 'empty' the remote
    # answered but has no such branch, 'unreachable' we could not ask.
    #
    # `git fetch origin main` fails for BOTH of the last two, and the engine
    # used to read that single non-zero exit as "offline". Measured live on
    # round 7: a machine that had just been invited to a repository with no
    # commits reported "cannot reach GitHub" and sat retrying - while the very
    # same engine was successfully pushing its presence refs to that very
    # remote. The network was never the problem; the BRANCH did not exist.
    $out = git ls-remote --heads origin $script:SC_Branch 2>$null
    if ($LASTEXITCODE -ne 0) { return 'unreachable' }
    if ($out) { return 'yes' }
    return 'empty'
}

function Initialize-EmptyProject {
    # A project whose branch does not exist yet cannot sync at all, and the
    # engine used to make it worse: with no .gitignore in place it committed
    # and ANNOUNCED its own bookkeeping - the live run published
    # refs/teamsync/pending/.../.teamsync.lock and .teamsync.log as work in
    # progress.
    #
    # So: put the ignore rules in first, whatever else happens. Then, only if
    # there is real content to send, publish it and create the branch. An empty
    # folder deliberately does NOT create one - two machines each starting
    # their own first commit would give the project two unrelated histories,
    # which is far worse than waiting for the person who set it up.
    $ignore = Join-Path $script:SC_Repo '.gitignore'
    $needed = @('.teamsync*', '_conflicts/')
    $have   = @()
    if (Test-Path -LiteralPath $ignore) {
        $have = @([IO.File]::ReadAllText($ignore) -split "`r?`n")
    }
    if (@($needed | Where-Object { $have -cnotcontains $_ }).Count -gt 0) {
        # Appended: the project's own lines - blank ones and comments
        # included - are its owners', and used to be rewritten here.
        Add-GitIgnoreLines -Root $script:SC_Repo -Lines $needed
        Write-Log 'this project had no .gitignore - added the sync rules so the engine stops announcing its own files' 'Yellow'
    }

    # The first version is an upload like any other - usually the biggest one
    # a project ever makes - so large content waits for the person's word
    # here exactly as it does on every later publish.
    if (Test-LargeHeld) { return $false }

    # Anything real to publish? The ignore file alone counts: it is the seed
    # every clone needs, and it is not the engine talking about itself.
    git add -A 2>$null | Out-Null
    $staged = @(git diff --cached --name-only 2>$null)
    if ($staged.Count -eq 0 -and -not (git rev-parse --verify -q HEAD 2>$null)) {
        Write-Log "this project has no '$($script:SC_Branch)' branch yet - waiting for its first version" 'Yellow'
        return $false
    }
    if ($staged.Count -gt 0) {
        git commit -q -m "sync: first version $(Get-Date -Format 'MM-dd HH:mm:ss')" 2>$null | Out-Null
    }
    git push -q origin "HEAD:$($script:SC_Branch)" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # Somebody created it in the meantime. Do NOT force: take theirs.
        Write-Log "another machine created '$($script:SC_Branch)' first - taking their version" 'Cyan'
        git fetch -q origin $script:SC_Branch 2>$null | Out-Null
        return $false
    }
    Clear-Approvals
    git branch --set-upstream-to "origin/$($script:SC_Branch)" 2>$null | Out-Null
    Write-Log "started this project: created '$($script:SC_Branch)' and published the first version" 'Green'
    return $true
}

function Get-AheadCount  { $n = git rev-list --count "origin/$($script:SC_Branch)..HEAD" 2>$null; if ($n) { [int]$n } else { 0 } }
function Get-BehindCount { $n = git rev-list --count "HEAD..origin/$($script:SC_Branch)" 2>$null; if ($n) { [int]$n } else { 0 } }

function Invoke-CommitLocal {
    # Snapshot whatever is on disk. Local only - nothing leaves this machine.
    git add -A 2>$null | Out-Null
    if (@(git diff --cached --name-only 2>$null).Count -eq 0) { return $false }
    git commit -q -m "sync: $(Get-Date -Format 'MM-dd HH:mm:ss')" 2>$null | Out-Null
    return $true
}

# The files this app plants inside a project. ONE list, because keeping three
# of them in step by hand had already failed twice: working.template.ps1 was
# missing from the BUILD's bundle entirely, so every project created by the
# packaged app silently never received working.ps1 at all - and the agent
# announcement, which the whole read-side hold depends on, had nothing to run.
# Update-PlantedFiles refreshes exactly this list, and build.ps1 refuses to
# build unless every entry is inside the package.
$SC_Planted = @(
    @{ Src = 'push-now.template.ps1';     Dst = 'push-now.ps1' }
    @{ Src = 'working.template.ps1';      Dst = 'working.ps1'  }
    @{ Src = 'who.template.ps1';          Dst = 'who.ps1'      }
    @{ Src = 'TEAM-PROJECT-REFERENCE.md'; Dst = 'TEAM-PROJECT-REFERENCE.md' }
)

function ConvertTo-VersionNumber {
    # "2.1.6" -> 2001006, so versions compare as plain numbers. Each part is
    # capped at three digits, which the project's own version rule guarantees:
    # only the first part may pass 9.
    param([string]$Text)
    if (-not $Text) { return 0 }
    $p = ($Text.Trim().TrimStart('v', 'V') -split '\.')
    if ($p.Count -lt 3) { return 0 }
    $n = 0
    foreach ($x in $p[0..2]) {
        $v = 0
        if (-not [int]::TryParse($x, [ref]$v)) { return 0 }
        $n = $n * 1000 + $v
    }
    return $n
}

function Update-PlantedFiles {
    # The app updates itself. The things it PLANTED did not.
    #
    # who.ps1, working.ps1, push-now.ps1 and TEAM-PROJECT-REFERENCE.md are
    # copied into a project once, by init-owner, and nothing ever refreshed
    # them. Measured on a real project a week old: push-now.ps1 was 5,868 bytes
    # against a shipped 11,358 - nearly half the script missing, including the
    # whole guard that stops a deletion being published. So a safety feature
    # shipped one day protected only projects created the next.
    #
    # The reference is worse than the scripts, because it is what the AGENTS
    # read: a copy two versions behind teaches rules that no longer hold.
    #
    # These files are TRACKED IN GIT, so one machine refreshing them carries
    # the fix to everybody. That is also the danger: two machines on different
    # app versions would otherwise overwrite each other every startup, for
    # ever. So each planted file carries a stamp of the version that wrote it,
    # and an older app leaves a newer file alone.
    param([string]$AppVersion)
    $mine = ConvertTo-VersionNumber $AppVersion
    if ($mine -le 0) { return @() }

    $marks = @{ '.ps1' = '# teamsync-artifact-version: '
                '.md'  = '<!-- teamsync-artifact-version: ' }

    $changed = @()
    foreach ($item in $SC_Planted) {
        $src = @(
            (Join-Path $script:SC_Home $item.Src)
            (Join-Path $script:SC_Home "..\$($item.Src)")
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
        if (-not $src) { continue }

        $dstPath = Join-Path $script:SC_Repo $item.Dst
        $ext     = [IO.Path]::GetExtension($item.Dst)
        $mark    = $marks[$ext]
        if (-not $mark) { continue }

        $shipped = [IO.File]::ReadAllText($src)
        $current = ''
        $stamped = 0
        if (Test-Path -LiteralPath $dstPath) {
            $current = [IO.File]::ReadAllText($dstPath)
            foreach ($line in ($current -split "`r?`n")) {
                if ($line.StartsWith($mark)) {
                    $stamped = ConvertTo-VersionNumber (
                        $line.Substring($mark.Length).TrimEnd(' ', '-', '>'))
                    break
                }
            }
        }

        # A machine running an older app must never drag a newer file back.
        if ($stamped -gt $mine) { continue }

        $stampLine = if ($ext -eq '.md') { "$mark$AppVersion -->" } else { "$mark$AppVersion" }
        $wanted    = $stampLine + "`r`n" + $shipped

        # Compare the BODY, ignoring the stamp.
        $currentBody = ($current -split "`r?`n" | Where-Object { -not $_.StartsWith($mark) }) -join "`n"
        $wantedBody  = ($shipped -split "`r?`n") -join "`n"

        # Identical content AND the stamp already ours: nothing to do. This is
        # the ordinary case on every startup, and it must cost nothing - a
        # rewrite here would be a commit that travels to the whole team and
        # shows up as somebody's unpublished work.
        if ($currentBody -eq $wantedBody -and $stamped -eq $mine) { continue }

        # Identical content under an OLDER stamp still gets re-stamped, once.
        # The stamp is the only thing that stops an older app overwriting a
        # newer file, and it is compared with -gt: leaving it behind would mean
        # a machine whose shipped copy of this file is OLDER than what is here,
        # but whose app version merely EQUALS the stale stamp, sails past the
        # guard and downgrades it. One small commit per upgrade closes that.

        try {
            # UTF-8 without a byte-order mark, the same rule as every other
            # file this engine writes - a mark at the head of a .ps1 is a
            # parse error waiting for the first non-latin path.
            [IO.File]::WriteAllText($dstPath, $wanted, (New-Object Text.UTF8Encoding($false)))
            $changed += $item.Dst
        } catch {
            Write-Log "could not refresh $($item.Dst): $($_.Exception.Message)" 'Yellow'
        }
    }

    if ($changed.Count -gt 0) {
        Write-Log "refreshed from this version of the app: $($changed -join ', ')" 'Cyan'
    }
    return $changed
}

# >>> destructive guard
# This block is IDENTICAL in sync-core.ps1 and push-now.template.ps1, and
# test_destructive_agreement fails the moment the two copies differ by a
# single character. push-now cannot dot-source this file - it is planted into
# projects and runs without the app - so the rule travels as a copy, and the
# test is what keeps it one rule.
#
# What is about to leave this machine that DESTROYS work rather than adding
# to it. Two shapes, both measured on this engine before the guard existed:
#
#   deleted  - a file the team has, or this machine had, that will no longer
#              exist once this is published. It disappears from every
#              teammate's disk, and the log used to say only "pushed 1
#              commit(s)". A MOVE is not a deletion: when the file's exact
#              content arrives under a new name in the same publish, nothing
#              is destroyed and nobody is asked (the user's ruling,
#              2026-10-01 - asking on every move teaches people to approve
#              without reading, and then the real deletion slips through).
#              Moved AND changed, or moved somewhere the project ignores,
#              still asks.
#   reverted - a file whose new content is byte-identical to an EARLIER
#              version of itself. Not a guess: the blob hash matches one the
#              file really held, which is exactly what putting a backup back
#              looks like. Newer text is replaced by older text wherever it
#              lands.
#
# "About to leave" means everything not yet on the shared branch: work still
# on the disk AND work already committed here but not yet sent. The second
# half used to be invisible - a deletion made with `git commit` by hand, or
# with an editor's commit button, went out without anybody being asked.
#
# "Earlier version" means EVERY earlier version. It used to mean the last
# forty, a ceiling that existed only because each version cost one git
# process: 14 changed files of a real project meant about 250 processes per
# look, every four seconds, which froze the app's window for good. One
# `git log` pass now reads every version of every file at once, so the
# ceiling saved nothing and is gone.
#
# A machine that is merely BEHIND is still never caught. Everything is
# measured from where this machine and the team last agreed - the merge base
# of HEAD and the shared branch - never from the server's tip, so a file this
# machine simply has not received yet is not "deleted". "I never had it" and
# "I deleted it" stay different states.
#
# Any git question that goes unanswered throws. It must never read as
# "nothing destructive here"; the caller holds the publish instead.
$script:DG_Zero = '0' * 40

function Invoke-DGGit {
    param([string[]]$GitArgs, [string]$What)
    $ErrorActionPreference = 'Continue'
    # Read-only questions must not take git's optional index lock: the app's
    # window asks the same questions while the engine is committing, and a
    # lock held for a moment by a reader made the writer's `git add` fail.
    $locks = $env:GIT_OPTIONAL_LOCKS
    $env:GIT_OPTIONAL_LOCKS = '0'
    try {
        $out = & git @GitArgs 2>$null
        $code = $LASTEXITCODE
    } finally {
        $env:GIT_OPTIONAL_LOCKS = $locks
    }
    if ($code -ne 0) { throw "git could not answer the destructive check ($What)" }
    $out
}

function Split-DGZ {
    # git -z output, as PowerShell hands it over: lines, with NULs inside.
    param($Lines)
    foreach ($t in ((@($Lines) -join "`n").Split([char]0))) {
        $t = $t.TrimStart([char]13, [char]10)
        if ($t) { $t }
    }
}

function Split-DGBatches {
    # Windows caps a command line at 32,767 characters, and a guard that died
    # on a large change set would hide exactly the large accidents.
    param([string[]]$Items, [int]$Limit = 8000)
    $batches = New-Object 'System.Collections.Generic.List[object]'
    $batch = New-Object 'System.Collections.Generic.List[string]'
    $size = 0
    foreach ($it in $Items) {
        if ($batch.Count -gt 0 -and ($size + $it.Length + 3) -gt $Limit) {
            $batches.Add($batch.ToArray())
            $batch = New-Object 'System.Collections.Generic.List[string]'
            $size = 0
        }
        $batch.Add($it)
        $size += $it.Length + 3
    }
    if ($batch.Count -gt 0) { $batches.Add($batch.ToArray()) }
    return ,$batches
}

function Get-DGChanged {
    # Paths that differ between $Base and the disk, by kind: D, M or A.
    # --no-renames: a rename is a deletion of the old name for everybody else,
    # whether or not it happened to be staged with `git mv`.
    param([string]$Base, [string]$Letter)
    Split-DGZ (Invoke-DGGit @('diff', '--name-only', '-z', '--no-renames',
                              "--diff-filter=$Letter", $Base, '--') "diff $Letter $Base")
}

function Get-DGVersions {
    # Every blob each path has held anywhere in the history of $Rev - both
    # sides of every change, and merges too: -c lists a merge whose result
    # differs from all of its parents, which is where a hand-made conflict
    # resolution lives. One pass for all paths.
    param([string]$Rev, [string[]]$Paths)
    $found = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    foreach ($p in $Paths) {
        $found[$p] = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    }
    foreach ($batch in (Split-DGBatches $Paths)) {
        $gitArgs = @('--literal-pathspecs', 'log', $Rev, '--full-history', '--root', '-c', '--raw',
                     '--no-abbrev', '--no-renames', '-z', '--format=', '--') + $batch
        $tokens = @(Split-DGZ (Invoke-DGGit $gitArgs "log $Rev"))
        $i = 0
        while ($i -lt $tokens.Count) {
            $meta = $tokens[$i]
            if ($meta.StartsWith(':') -and ($i + 1) -lt $tokens.Count) {
                # ":<modes> <blobs> <status>" with one colon per parent.
                $body = $meta.TrimStart(':')
                $parents = $meta.Length - $body.Length
                $fields = $body.Split(' ')
                $path = $tokens[$i + 1]
                if ($found.ContainsKey($path)) {
                    for ($k = $parents + 1; $k -lt (2 * ($parents + 1)) -and $k -lt $fields.Count; $k++) {
                        if ($fields[$k] -ne $script:DG_Zero) { [void]$found[$path].Add($fields[$k]) }
                    }
                }
                $i += 2
            } else {
                $i += 1
            }
        }
    }
    return ,$found
}

function Get-DGWorkingBlobs {
    # What each file on the disk would be recorded as: its blob id after git's
    # own clean filters, exactly what a commit would store.
    param([string]$Root, [string[]]$Paths)
    $out = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    $byFull = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    foreach ($p in $Paths) {
        $full = Join-Path $Root $p
        if (Test-Path -LiteralPath $full -PathType Leaf) { $byFull[$full] = $p }
    }
    foreach ($batch in (Split-DGBatches @($byFull.Keys))) {
        $shas = @(Invoke-DGGit (@('hash-object', '--') + $batch) 'hash-object' | Where-Object { $_ })
        if ($shas.Count -ne $batch.Count) { throw 'git hash-object answered for the wrong number of files' }
        for ($k = 0; $k -lt $batch.Count; $k++) { $out[$byFull[$batch[$k]]] = "$($shas[$k])".Trim() }
    }
    return ,$out
}

function Get-DGRemoved {
    # Paths that will be gone, measured from $Base, each with the blob it held
    # there - what a move would have to carry somewhere else intact.
    param([string]$Base)
    $out = New-Object 'System.Collections.Generic.List[object]'
    $tokens = @(Split-DGZ (Invoke-DGGit @('diff', '--raw', '-z', '--no-renames', '--no-abbrev',
                                          '--diff-filter=D', $Base, '--') "diff --raw D $Base"))
    $i = 0
    while ($i -lt $tokens.Count) {
        $meta = $tokens[$i]
        if ($meta.StartsWith(':') -and ($i + 1) -lt $tokens.Count) {
            # ":<old mode> <new mode> <old blob> <new blob> D"
            $fields = $meta.TrimStart(':').Split(' ')
            $out.Add(@($tokens[$i + 1], $fields[2]))
            $i += 2
        } else {
            $i += 1
        }
    }
    return ,$out
}

function Get-DGArrivals {
    # The blob of every file that will exist after this publish but was not
    # there at one of $Bases: added and tracked, or new on the disk and not
    # ignored - exactly what `git add -A` is about to take in. A file that
    # disappears while its exact content arrives here was MOVED.
    param([string]$Root, [string[]]$Bases)
    $paths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($b in $Bases) {
        foreach ($p in @(Get-DGChanged $b 'A')) { [void]$paths.Add($p) }
    }
    foreach ($p in @(Split-DGZ (Invoke-DGGit @('ls-files', '-z', '--others', '--exclude-standard') 'ls-files --others'))) {
        [void]$paths.Add($p)
    }
    $blobs = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    if ($paths.Count -gt 0) {
        $now = Get-DGWorkingBlobs -Root $Root -Paths @($paths)
        foreach ($v in $now.Values) { [void]$blobs.Add($v) }
    }
    return ,$blobs
}

function Get-DestructiveChangesIn {
    param([string]$Root, [string]$Upstream)
    $ErrorActionPreference = 'Continue'
    $head = git rev-parse -q --verify HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $head) { return @{ Deleted = @(); Reverted = @() } }
    $head = "$head".Trim()
    $base = git merge-base HEAD $Upstream 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $base) { $base = $head } else { $base = "$base".Trim() }

    # Gone, with the content each one held - from this machine's last commit,
    # and (below) from what the team has.
    $removed = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    $gone = { param($pair)
        if (-not $removed.ContainsKey($pair[0])) {
            $removed[$pair[0]] = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        }
        [void]$removed[$pair[0]].Add($pair[1])
    }
    foreach ($pair in (Get-DGRemoved 'HEAD')) { & $gone $pair }
    # Changed on the disk since this machine's last commit: judged against
    # every version this machine has ever held.
    $uncommitted = New-Object 'System.Collections.Generic.List[string]'
    foreach ($p in @(Get-DGChanged 'HEAD' 'M')) { $uncommitted.Add($p) }
    # Committed here but not yet sent: judged against every version the TEAM
    # has held, so an edit that is merely unpublished is never taken for a
    # roll-back of itself.
    $committedOnly = New-Object 'System.Collections.Generic.List[string]'
    if ($base -ne $head) {
        foreach ($pair in (Get-DGRemoved $base)) { & $gone $pair }
        $readded = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($p in @(Get-DGChanged 'HEAD' 'A')) { [void]$readded.Add($p) }
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($p in $uncommitted) { [void]$seen.Add($p) }
        foreach ($p in @(Get-DGChanged $base 'M')) {
            if (-not $seen.Add($p)) { continue }
            # Removed by a local commit and then put back on the disk: that is
            # uncommitted work like any other edit.
            if ($readded.Contains($p)) { $uncommitted.Add($p) } else { $committedOnly.Add($p) }
        }
    }

    $now = Get-DGWorkingBlobs -Root $Root -Paths (@($uncommitted) + @($committedOnly))
    $reverted = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $group = @($uncommitted | Where-Object { $now.ContainsKey($_) })
    if ($group.Count -gt 0) {
        $hist = Get-DGVersions 'HEAD' $group
        foreach ($p in $group) { if ($hist[$p].Contains($now[$p])) { [void]$reverted.Add($p) } }
    }
    $group = @($committedOnly | Where-Object { $now.ContainsKey($_) })
    if ($group.Count -gt 0) {
        $hist = Get-DGVersions $base $group
        foreach ($p in $group) { if ($hist[$p].Contains($now[$p])) { [void]$reverted.Add($p) } }
    }

    # A deletion is excused only when EVERY version it takes away arrives
    # intact under another name: moved, not destroyed. Moved and changed -
    # or changed here before it moved, so the team's version is not what
    # arrives - still asks.
    $deleted = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    if ($removed.Count -gt 0) {
        $bases = if ($base -ne $head) { @('HEAD', $base) } else { @('HEAD') }
        $arrived = Get-DGArrivals -Root $Root -Bases $bases
        foreach ($p in $removed.Keys) {
            foreach ($blob in $removed[$p]) {
                if (-not $arrived.Contains($blob)) { [void]$deleted.Add($p); break }
            }
        }
    }

    $d = [string[]]@($deleted)
    [Array]::Sort($d, [StringComparer]::Ordinal)
    $r = [string[]]@($reverted)
    [Array]::Sort($r, [StringComparer]::Ordinal)
    return @{ Deleted = $d; Reverted = $r }
}

function Get-DestructiveSignature {
    # Identifies one particular set of destructive changes, so a confirmation
    # covers what the person actually looked at and nothing else. Delete one
    # more file afterwards and the signature changes, so it is asked again.
    param($Changes)
    # ORDINAL sort, deliberately: the window computes this same signature in
    # Python, and PowerShell's default Sort-Object is culture-aware. Two
    # spellings of "sorted" would make the app and the engine disagree about
    # which accident the person confirmed - silently, and only for names where
    # the cultures differ.
    $all = [Collections.Generic.List[string]]@(@($Changes.Deleted) + @($Changes.Reverted))
    if ($all.Count -eq 0) { return '' }
    $all.Sort([StringComparer]::Ordinal)
    $text = ($all -join "`n")
    $sha  = [Security.Cryptography.SHA1]::Create()
    return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text))) -replace '-', '').Substring(0, 12)
}

# --- large new content ------------------------------------------------------
# The second thing a publish waits for a person's word on: something BIG
# about to go out for the first time. Measured on the first real project: the
# folder was 144 MB and 124 MB of it was node_modules - kept home only because
# the project's own .gitignore happened to say so. Without that line every
# byte would have gone, into a history that never forgets, and the next
# `npm install` anywhere in any project would publish within four minutes.
#
# "Going out" means what the upload really carries, in two parts:
#
#   on the disk - new files that are not ignored, and files added or changed
#                 since this machine's last commit. Sized from the disk.
#   in commits  - every file version inside a commit made here that the team
#                 does not have yet. A push sends commits, not the folder's
#                 final state: something committed by hand and then deleted
#                 still travels, inside the first commit.
#
# Content the team already has uploads nothing - a moved or copied file is
# only a new name - so a file whose exact content the team already holds, or
# that one of these commits already carries, does not count again.
#
# Each piece is filed under an ITEM, the thing a person would name: the
# outermost folder the team does not have yet (all of node_modules/ is ONE
# item, however many small files it holds, and however it got here), or else
# the file itself. "Keep it home" names exactly that and nothing above it. An
# item is held when one file in it is 25 MB or more (GitHub warns at 50 and
# refuses at 100), or when it adds 50 MB or more in all.
#
# Shared marks a file the team already has: only its new version would go,
# and keeping it home would mean taking it out of the project for everybody.
# Committed marks content inside a commit made here, which keeping it home
# has to take back out of that commit.
$script:DG_FileLimit  = 25MB
$script:DG_GroupLimit = 50MB
$script:DG_TeamTree   = $null

function Get-DGTeamTree {
    # What the team has at $Rev: its folders, the content id of each file,
    # and the size of each content id. Read once per version of the shared
    # branch - it changes only when somebody publishes.
    param([string]$Rev)
    if ($script:DG_TeamTree -and $script:DG_TeamTree.Rev -eq $Rev) { return $script:DG_TeamTree }
    $dirs  = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $files = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    $sizes = New-Object 'System.Collections.Generic.Dictionary[string,long]' ([StringComparer]::Ordinal)
    if ($Rev) {
        foreach ($t in @(Split-DGZ (Invoke-DGGit @('ls-tree', '-r', '-t', '-l', '-z', $Rev) "ls-tree $Rev"))) {
            # "<mode> <type> <id> <size>`t<path>"
            $tab = $t.IndexOf("`t")
            if ($tab -lt 0) { continue }
            $f = $t.Substring(0, $tab) -split ' +'
            $path = $t.Substring($tab + 1)
            if ($f[1] -eq 'tree') { [void]$dirs.Add($path) }
            elseif ($f[1] -eq 'blob') { $files[$path] = $f[2]; $sizes[$f[2]] = [long]$f[3] }
        }
    }
    $script:DG_TeamTree = @{ Rev = $Rev; Dirs = $dirs; Files = $files; Sizes = $sizes }
    return $script:DG_TeamTree
}

function Get-DGCommitted {
    # Every file version inside commits reachable from $Tip that the team
    # does not have - what a push of $Tip carries besides the disk. Each
    # content id once, under the first name git reports for it.
    param([string]$Tip, [string]$Upstream)
    $range = if ($Upstream) { @($Tip, '--not', $Upstream) } else { @($Tip) }
    $named = New-Object 'System.Collections.Generic.List[object]'
    foreach ($line in @(Invoke-DGGit (@('rev-list', '--objects') + $range + @('--')) 'rev-list --objects')) {
        # "<id> <path>"; a commit has no path, the top folder an empty one
        $line = "$line"
        $sp = $line.IndexOf(' ')
        if ($sp -gt 0 -and $sp -lt ($line.Length - 1)) { $named.Add(@($line.Substring(0, $sp), $line.Substring($sp + 1))) }
    }
    $out = New-Object 'System.Collections.Generic.List[object]'
    if ($named.Count -eq 0) { return ,$out }
    $ErrorActionPreference = 'Continue'
    $info = @($named | ForEach-Object { $_[0] } |
              & git cat-file '--batch-check=%(objectname) %(objecttype) %(objectsize)' 2>$null)
    if ($LASTEXITCODE -ne 0 -or $info.Count -ne $named.Count) {
        throw 'git could not answer the destructive check (cat-file sizes)'
    }
    for ($k = 0; $k -lt $named.Count; $k++) {
        $f = "$($info[$k])".Split(' ')
        if ($f.Count -ge 3 -and $f[1] -eq 'blob') {
            $out.Add(@{ Path = $named[$k][1]; Blob = $f[0]; Size = [long]$f[2]; Committed = $true })
        }
    }
    return ,$out
}

function Get-LargeOutgoingIn {
    # Large content about to leave: one @{Item; Bytes; Files; Shared;
    # Committed} per held item, ordinal by item. $Tip other than HEAD, with
    # -CommittedOnly, measures what pushing that commit would carry.
    param([string]$Root, [string]$Upstream, [string]$Tip = 'HEAD', [switch]$CommittedOnly)
    $ErrorActionPreference = 'Continue'
    $tipId = git rev-parse -q --verify "$Tip^{commit}" 2>$null
    $tipId = if ($LASTEXITCODE -eq 0 -and $tipId) { "$tipId".Trim() } else { '' }
    $upId = git rev-parse -q --verify "$Upstream^{commit}" 2>$null
    $upId = if ($LASTEXITCODE -eq 0 -and $upId) { "$upId".Trim() } else { '' }

    $pieces = New-Object 'System.Collections.Generic.List[object]'
    if ($tipId) { foreach ($c in (Get-DGCommitted $tipId $upId)) { $pieces.Add($c) } }
    if (-not $CommittedOnly) {
        $disk = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($p in @(Split-DGZ (Invoke-DGGit @('ls-files', '-z', '--others', '--exclude-standard') 'ls-files --others'))) {
            [void]$disk.Add($p)
        }
        if ($tipId) {
            foreach ($letter in 'A', 'M') { foreach ($p in @(Get-DGChanged 'HEAD' $letter)) { [void]$disk.Add($p) } }
        } else {
            # Nothing committed yet: everything staged goes in the first commit.
            foreach ($p in @(Split-DGZ (Invoke-DGGit @('ls-files', '-z') 'ls-files'))) { [void]$disk.Add($p) }
        }
        foreach ($p in $disk) {
            # .NET rather than Test-Path/Get-Item: a folder nobody ignored can
            # hold fifty thousand files, and this runs on every publish.
            try { $fi = New-Object IO.FileInfo ([IO.Path]::Combine($Root, $p)) } catch { continue }
            if ($fi.Exists) { $pieces.Add(@{ Path = $p; Blob = $null; Size = [long]$fi.Length; Committed = $false }) }
        }
    }
    if ($pieces.Count -eq 0) { return }

    $team = Get-DGTeamTree $upId
    $items = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    $under = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    foreach ($pc in $pieces) {
        $path = $pc.Path
        $shared = $team.Files.ContainsKey($path)
        $item = $path
        $cut = $path.LastIndexOf('/')
        if (-not $shared -and $cut -gt 0) {
            # The outermost folder the team does not have - once per folder.
            $dir = $path.Substring(0, $cut)
            if (-not $under.ContainsKey($dir)) {
                $owner = ''
                $acc = ''
                foreach ($part in $dir.Split('/')) {
                    $acc = if ($acc) { "$acc/$part" } else { $part }
                    if (-not $team.Dirs.Contains($acc)) { $owner = "$acc/"; break }
                }
                $under[$dir] = $owner
            }
            if ($under[$dir]) { $item = $under[$dir] }
        }
        if (-not $items.ContainsKey($item)) {
            $items[$item] = @{ Item = $item; Shared = $shared; Pieces = New-Object 'System.Collections.Generic.List[object]' }
        }
        $items[$item].Pieces.Add($pc)
    }

    $measure = {
        param($it)
        $total = [long]0; $biggest = [long]0; $committed = $false
        $paths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($pc in $it.Pieces) {
            if ($pc.Known) { continue }
            $total += $pc.Size
            if ($pc.Size -gt $biggest) { $biggest = $pc.Size }
            [void]$paths.Add($pc.Path)
            if ($pc.Committed) { $committed = $true }
        }
        @{ Total = $total; Big = ($biggest -ge $script:DG_FileLimit -or $total -ge $script:DG_GroupLimit)
           Files = $paths.Count; Committed = $committed }
    }
    $candidates = @($items.Values | Where-Object { (& $measure $_).Big })
    if ($candidates.Count -eq 0) { return }

    # Already there, or already on its way. Reading a file's content id means
    # reading all of it, so a file is read only when that could change the
    # answer: its size matches something known, AND without it the item
    # would no longer be large. A folder of fresh downloads stays large
    # whatever a few of its files turn out to be, and costs no reading.
    $knownIds = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $knownSizes = New-Object 'System.Collections.Generic.HashSet[long]'
    foreach ($kv in $team.Sizes.GetEnumerator()) { [void]$knownIds.Add($kv.Key); [void]$knownSizes.Add($kv.Value) }
    foreach ($pc in $pieces) { if ($pc.Committed) { [void]$knownIds.Add($pc.Blob); [void]$knownSizes.Add($pc.Size) } }
    $toHash = New-Object 'System.Collections.Generic.List[string]'
    foreach ($it in $candidates) {
        $rest = [long]0; $restBig = [long]0
        $maybe = New-Object 'System.Collections.Generic.List[string]'
        foreach ($pc in $it.Pieces) {
            if ($pc.Committed) {
                if ($team.Sizes.ContainsKey($pc.Blob)) { $pc.Known = $true; continue }
            } elseif ($knownSizes.Contains($pc.Size)) {
                $maybe.Add($pc.Path); continue
            }
            $rest += $pc.Size
            if ($pc.Size -gt $restBig) { $restBig = $pc.Size }
        }
        if ($maybe.Count -gt 0 -and $restBig -lt $script:DG_FileLimit -and $rest -lt $script:DG_GroupLimit) {
            $toHash.AddRange($maybe)
        }
    }
    if ($toHash.Count -gt 0) {
        $now = Get-DGWorkingBlobs -Root $Root -Paths @($toHash)
        foreach ($it in $candidates) {
            foreach ($pc in $it.Pieces) {
                if (-not $pc.Committed -and $now.ContainsKey($pc.Path) -and $knownIds.Contains($now[$pc.Path])) { $pc.Known = $true }
            }
        }
    }

    $held = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    foreach ($it in $candidates) {
        $m = & $measure $it
        if ($m.Big) {
            $held[$it.Item] = @{ Item = $it.Item; Bytes = $m.Total; Files = $m.Files
                                 Shared = $it.Shared; Committed = $m.Committed }
        }
    }
    # One item per pipeline object: callers collect them with @(...), which
    # gives an empty array for none - never one nested array counted as one.
    $names = [string[]]@($held.Keys)
    [Array]::Sort($names, [StringComparer]::Ordinal)
    foreach ($n in $names) { $held[$n] }
}

function Get-LargeSignature {
    # One particular set of large items, so "send them" covers only those.
    param($Large)
    $all = New-Object 'System.Collections.Generic.List[string]'
    foreach ($l in @($Large)) { if ($l -and $l.Item) { $all.Add($l.Item) } }
    if ($all.Count -eq 0) { return '' }
    $all.Sort([StringComparer]::Ordinal)
    $sha = [Security.Cryptography.SHA1]::Create()
    return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($all -join "`n")))) -replace '-', '').Substring(0, 12)
}
# <<< destructive guard

function Get-DestructiveChanges {
    # The engine's view: this repository, measured against the shared branch.
    Get-DestructiveChangesIn -Root $script:SC_Repo -Upstream "refs/remotes/origin/$($script:SC_Branch)"
}

function Get-LargeOutgoing {
    Get-LargeOutgoingIn -Root $script:SC_Repo -Upstream "refs/remotes/origin/$($script:SC_Branch)"
}

function Approve-Large {
    # "Send them": recorded against exactly these items, like a destructive OK.
    param($Large)
    git config --local teamsync.largeok (Get-LargeSignature $Large) 2>$null | Out-Null
}

function Test-LargeApproved {
    param($Large)
    $sig = Get-LargeSignature $Large
    if (-not $sig) { return $true }
    return ((git config --local --get teamsync.largeok 2>$null) -eq $sig)
}

function Format-LargeLine {
    param($Item)
    $what = if ($Item.Shared) { ', a new version of a file the team has' }
            elseif ($Item.Committed) { ', inside a commit made here' } else { '' }
    # Invariant digits, as the window shows them: "60.0" whatever the
    # machine's own way of writing decimals.
    $mb = ($Item.Bytes / 1MB).ToString('0.0', [Globalization.CultureInfo]::InvariantCulture)
    "{0} ({1} MB, {2} file(s){3})" -f $Item.Item, $mb, $Item.Files, $what
}

function Test-LargeHeld {
    # True when large content waits for the person's word - said once per
    # set, in the log and as an alert - or when the check itself could not be
    # made: that is never read as "nothing large here".
    try {
        $large = @(Get-LargeOutgoing)
        $script:SC_GuardFailSaid = $null
    } catch {
        $why = $_.Exception.Message
        if ($script:SC_GuardFailSaid -ne $why) {
            $script:SC_GuardFailSaid = $why
            Write-Log "not publishing - $why; trying again on the next pass" 'Yellow'
        }
        return $true
    }
    $script:SC_Large = $large
    if ($large.Count -eq 0 -or (Test-LargeApproved $large)) {
        $script:SC_LargeSaid = $null
        return $false
    }
    $sig = Get-LargeSignature $large
    if ($script:SC_LargeSaid -ne $sig) {
        $script:SC_LargeSaid = $sig
        $lines = @($large | Select-Object -First 12 | ForEach-Object { Format-LargeLine $_ })
        Write-Log 'publishing is PAUSED - this would upload large new content' 'Red'
        foreach ($l in $lines) { Write-Log "  $l" 'Yellow' }
        Write-Log '  send it, or keep it home, in the app - nothing goes out until you do' 'Yellow'
        Show-Alert -Title 'teamsync: large new content waiting' -Body (
            "Publishing is paused.`n`n" + ($lines -join "`n") + "`n`n" +
            "Once sent, it stays in the project's history for good. If it can be " +
            "rebuilt on each machine (installed libraries, caches), keep it home.") -OpenFolder $script:SC_Repo
    }
    return $true
}

function Clear-Approvals {
    # A "yes" covers the publish it was given for, and nothing after it: the
    # same names deleted again next month, or a different big folder under an
    # approved name, ask again.
    git config --local --unset teamsync.destructiveok 2>$null | Out-Null
    git config --local --unset teamsync.largeok 2>$null | Out-Null
}

function Format-GitIgnoreLine {
    # One exact path as a .gitignore line: anchored to the project root, so it
    # names this item and never a same-named one elsewhere, with git's
    # pattern characters escaped so a name like "[draft].md" means itself.
    param([string]$Path)
    $escaped = ($Path -replace '([\[\]\*\?\\])', '\$1')
    if ($escaped.StartsWith('#') -or $escaped.StartsWith('!')) { $escaped = '\' + $escaped }
    return '/' + $escaped
}

function Keep-LargeHome {
    # "Keep them home": each item goes into .gitignore, so it is never sent -
    # and the .gitignore travels, so no other machine sends its own copy
    # either. An item already tracked here is also taken out of the index.
    #
    # A file the team already has is not kept home this way: that would take
    # it out of the project for everybody. Only new items are.
    #
    # Something already inside a commit made here would still travel - a push
    # sends commits, not the folder's final state. So the commits not yet
    # sent are folded back into the next one first: every file stays exactly
    # as it is on the disk, only the unsent steps between are let go. Throws
    # when that cannot be done safely.
    param($Large)
    $new = @(@($Large) | Where-Object { -not $_.Shared })
    if (@($new | Where-Object { $_.Committed }).Count -gt 0) {
        $gitDir = git rev-parse --git-dir 2>$null
        if ((Test-Rebasing) -or ($gitDir -and (Test-Path -LiteralPath (Join-Path $gitDir 'MERGE_HEAD')))) {
            throw 'a merge is still being finished here - try again once it is done'
        }
        $base = git merge-base HEAD "refs/remotes/origin/$($script:SC_Branch)" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $base) {
            throw "it is already in this folder's own history, which goes up as it is"
        }
        git reset -q --soft "$base".Trim() 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'git could not set the unsent commits aside' }
    }
    Add-GitIgnoreLines -Root $script:SC_Repo -Lines @($new | ForEach-Object { Format-GitIgnoreLine $_.Item })
    foreach ($l in $new) {
        git --literal-pathspecs rm -r -q --cached --ignore-unmatch -- $l.Item.TrimEnd('/') 2>$null | Out-Null
    }
    git config --local --unset teamsync.largeok 2>$null | Out-Null
}

function Add-GitIgnoreLines {
    # APPENDS the lines the file lacks - the project's own lines, blank lines
    # and comments are its owners' and are never rewritten - in the line
    # ending the file already uses.
    param([string]$Root, [string[]]$Lines)
    $ignore = Join-Path $Root '.gitignore'
    $text = ''
    if (Test-Path -LiteralPath $ignore) { $text = [IO.File]::ReadAllText($ignore) }
    $have = @($text -split "`r?`n")
    $nl = if ($text.Contains("`r`n") -or -not $text) { "`r`n" } else { "`n" }
    $add = @($Lines | Where-Object { $_ -and $have -cnotcontains $_ } | Select-Object -Unique)
    if ($add.Count -eq 0) { return }
    if ($text -and -not $text.EndsWith("`n")) { $text += $nl }
    $text += ($add -join $nl) + $nl
    [IO.File]::WriteAllText($ignore, $text, (New-Object Text.UTF8Encoding($false)))
}

function Approve-Destructive {
    # The person said "yes, I meant it". Recorded on the disk against THIS set,
    # so it survives a restart and cannot silently bless a later accident.
    param($Changes)
    git config --local teamsync.destructiveok (Get-DestructiveSignature $Changes) 2>$null | Out-Null
}

function Test-DestructiveApproved {
    param($Changes)
    $sig = Get-DestructiveSignature $Changes
    if (-not $sig) { return $true }
    return ((git config --local --get teamsync.destructiveok 2>$null) -eq $sig)
}

function Restore-Destructive {
    # The other answer: put them back. This is the undo the app never had -
    # every version is already in every clone, there was simply no door to it.
    #
    # Where each file comes back FROM depends on where the damage sits. Still
    # on the disk: from this machine's own newest version (HEAD). Already
    # committed here: from the version the team has (the merge base), because
    # HEAD itself is the damage. A plain `git checkout -- <file>` restored
    # from the index, so a deletion staged with `git rm` could not be undone
    # at all.
    param($Changes)
    $base = git merge-base HEAD "refs/remotes/origin/$($script:SC_Branch)" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $base) { $base = 'HEAD' } else { $base = "$base".Trim() }
    foreach ($f in @(@($Changes.Deleted) + @($Changes.Reverted))) {
        if (-not $f) { continue }
        git cat-file -e "HEAD:$f" 2>$null
        $inHead = ($LASTEXITCODE -eq 0)
        $dirty = $true
        if ($inHead) {
            git --literal-pathspecs diff --quiet HEAD -- "$f" 2>$null
            $dirty = ($LASTEXITCODE -ne 0)
        }
        $source = if ($inHead -and $dirty) { 'HEAD' } else { $base }
        git --literal-pathspecs checkout $source -- "$f" 2>$null | Out-Null
    }
    git config --local --unset teamsync.destructiveok 2>$null | Out-Null
}

function Show-Alert {
    param([string]$Title, [string]$Body, [string]$OpenFolder)
    Write-Host ''
    Write-Host ('=' * 66) -ForegroundColor Red
    Write-Host "  $Title" -ForegroundColor Red
    Write-Host ('=' * 66) -ForegroundColor Red
    Write-Host $Body
    Write-Host ''
    if ($script:SC_NoPopup) { return }
    if ($OpenFolder -and (Test-Path -LiteralPath $OpenFolder)) { Start-Process explorer.exe $OpenFolder }
    $safe = $Body -replace "'", "''"
    $cmd  = "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show('$safe','$Title')"
    $enc  = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    Start-Process powershell.exe -ArgumentList '-NoProfile', '-WindowStyle', 'Hidden', '-EncodedCommand', $enc
}

function Get-RebaseOrigHead {
    # The branch tip as it stood before the rebase started, or '' outside one.
    # Git writes it down; mid-rebase HEAD is partway through the replay and
    # answers a different question.
    foreach ($d in 'rebase-merge', 'rebase-apply') {
        $p = Join-Path (git rev-parse --git-dir 2>$null) "$d/orig-head"
        if ($p -and (Test-Path -LiteralPath $p)) {
            return (Get-Content -LiteralPath $p -Raw -ErrorAction SilentlyContinue).Trim()
        }
    }
    return ''
}

function Get-RebaseBase {
    # The commit both sides started from, asked for in a way that still works
    # DURING a rebase.
    #
    # `git merge-base HEAD origin/<branch>` is the obvious call and it is
    # wrong here: mid-rebase, HEAD is detached ON TOP of the upstream, so the
    # merge base IS the upstream and the range "$mb..origin/branch" comes back
    # empty - which reads as "nobody contributed" exactly when we are asking
    # who did. Git records the pre-rebase tip in the rebase directory, so use
    # that; ORIG_HEAD is the fallback, and only outside a rebase is plain HEAD
    # the right question.
    $orig = Get-RebaseOrigHead
    if (-not $orig) { $orig = (git rev-parse --verify -q ORIG_HEAD 2>$null) }
    if (-not $orig) { $orig = 'HEAD' }
    git merge-base $orig "origin/$($script:SC_Branch)" 2>$null
}

function Save-Name {
    # One saved copy per SOURCE FILE, not per file name.
    #
    # These folders used to be named from the leaf alone, so src/alpha/notes.md
    # and src/beta/notes.md both wrote "notes.MINE.md": the second overwrote
    # the first, and the report still listed both, pointing each at the single
    # survivor. Somebody resolving alpha then read beta's text believing it was
    # alpha's. Keeping the whole path, with the separators folded to __, makes
    # the name unique again while staying a legal Windows filename.
    param([string]$Path)
    ($Path -replace '[\\/]', '__')
}

function Save-Side {
    # Pull one side of a conflicted file out of git's index into a real file.
    #   stage 1 = common ancestor
    #   stage 2 = "ours"   -> during a rebase this is the UPSTREAM (the other person)
    #   stage 3 = "theirs" -> during a rebase this is YOUR replayed commit
    # That inversion is why MINE below is stage 3 and THEIRS is stage 2.
    # Verified against a live conflict, not assumed.
    param([int]$Stage, [string]$File, [string]$Destination)
    $content = git show ":${Stage}:${File}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Set-Content -LiteralPath $Destination -Value '<< this side has no version of this file >>' -Encoding UTF8
        return
    }
    Set-Content -LiteralPath $Destination -Value $content -Encoding UTF8
}

function Report-Conflict {
    param([string]$Phase)
    $files = Get-Unmerged
    if ($files.Count -eq 0) { return }

    $stamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
    $dir   = Join-Path $script:SC_ConflictRoot $stamp
    New-Item -ItemType Directory -Path $dir -Force | Out-Null

    $md = New-Object System.Collections.Generic.List[string]
    $md.Add('# Conflict')
    $md.Add('')
    $md.Add("Detected during: $Phase")
    $md.Add("Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    $md.Add('')
    $md.Add('The same lines were changed here and on at least one other machine.')
    $md.Add('Nothing was pushed and nothing of yours was lost - your work is in')
    $md.Add('local commits.')
    $md.Add('')
    $md.Add('For each file below you get three copies, side by side:')
    $md.Add('')
    $md.Add('- `*.MINE.*`   - your version, from this machine')
    $md.Add('- `*.THEIRS.*` - the version from GitHub. That is the shared branch, so')
    $md.Add('                 with more than two people it may combine several')
    $md.Add('                 teammates'' work. Each file below names who is in it.')
    $md.Add('- `*.BASE.*`   - the file before anybody touched it')
    $md.Add('')
    $md.Add('The copies keep the whole path with folders joined by __, so two files')
    $md.Add('of the same name in different folders stay apart.')
    $md.Add('')
    $md.Add('The file in the project folder itself holds both versions with markers.')
    $md.Add('')
    $md.Add('## Files')
    $md.Add('')
    foreach ($f in $files) {
        $flat = Save-Name $f
        $base = [IO.Path]::GetFileNameWithoutExtension($flat)
        $ext  = [IO.Path]::GetExtension($flat)
        Save-Side -Stage 3 -File $f -Destination (Join-Path $dir "$base.MINE$ext")
        Save-Side -Stage 2 -File $f -Destination (Join-Path $dir "$base.THEIRS$ext")
        Save-Side -Stage 1 -File $f -Destination (Join-Path $dir "$base.BASE$ext")
        $md.Add("### $f")
        $md.Add('')
        $md.Add("- mine   : ``$base.MINE$ext``")
        $md.Add("- theirs : ``$base.THEIRS$ext``")
        $md.Add("- before : ``$base.BASE$ext``")
        # Who is actually inside THEIRS. With two people it could only be one
        # person; on a shared branch it is however many landed work here since
        # the common start, and resolving as though it were one drops the rest.
        $mb = Get-RebaseBase
        if ($mb) {
            $authors = @(git log --format='%an' "$mb..origin/$($script:SC_Branch)" -- $f 2>$null |
                         Where-Object { $_ } | Sort-Object -Unique)
            if ($authors.Count -gt 1) {
                $md.Add("- theirs holds work from: $($authors -join ', ') - keep EVERY one of them")
            } elseif ($authors.Count -eq 1) {
                $md.Add("- theirs is from: $($authors[0])")
            }
        }
        $md.Add('')
    }
    $md.Add('## How to finish')
    $md.Add('')
    $md.Add('Hand this to your AI agent:')
    $md.Add('')
    $md.Add("> Read _conflicts/$stamp/CONFLICT.md and resolve the conflict. Keep both intents.")
    $md.Add('')
    $md.Add('Or edit the real file yourself until it is what you want, then run:')
    $md.Add('')
    $md.Add('    git add . ; git rebase --continue')
    $md.Add('')
    $md.Add('Sync resumes by itself as soon as the conflict is finished.')
    $md.Add('To back out instead: git rebase --abort')
    Set-Content -LiteralPath (Join-Path $dir 'CONFLICT.md') -Value $md -Encoding UTF8

    $body = "$($files.Count) file(s) conflicted during $Phase.`n`n" +
            "Both versions were saved for you in:`n_conflicts\$stamp`n`n" +
            "Nothing was pushed. Nothing was lost.`nSync is paused until you finish it."
    Show-Alert -Title 'teamsync: conflict' -Body $body -OpenFolder $dir
    Write-Log "CONFLICT during $Phase - $($files.Count) file(s). See _conflicts\$stamp" 'Red'
}

function Invoke-Integrate {
    # Bring the other person's commits in underneath ours. $true on success.
    #
    # -AtPublish is the design's single crossing point: outside of publishing,
    # any local work in flight (saved-uncommitted OR committed-unpushed) HOLDS
    # incoming changes to the same files; at publish - the moment the system
    # already defines as "my work is done" - the gate opens, the merge happens,
    # and a crossing is declared explicitly with both originals kept.
    param([switch]$AtPublish)
    git fetch -q origin $script:SC_Branch 2>$null
    if ($LASTEXITCODE -ne 0) {
        # A failed fetch has two very different causes, and calling both
        # "offline" is what made a newly joined empty project sit for ever
        # saying "cannot reach GitHub" while it was demonstrably online.
        switch (Test-RemoteBranch) {
            'unreachable' { Set-NetState $false; return $false }
            'empty' {
                Set-NetState $true
                if (-not $script:SC_SaidEmpty) {
                    $script:SC_SaidEmpty = $true
                    Initialize-EmptyProject | Out-Null
                }
                return $true          # online, nothing to integrate yet
            }
            default { Set-NetState $false; return $false }
        }
    }
    Set-NetState $true
    $script:SC_SaidEmpty = $false

    $behind = Get-BehindCount
    if ($behind -eq 0) { return $true }

    # Three-dot diffs, deliberately: HEAD...origin is what THEY changed since the
    # last common point, origin...HEAD is what WE changed. The plain two-sided
    # diff mixes both and would name our own files as "arrived".
    $incoming = @(git diff --name-only "HEAD...origin/$($script:SC_Branch)" 2>$null)

    # The read-side gate, by STATE and not by clock. "In flux" means the local
    # file carries edits not yet published in either sense: saved-but-
    # uncommitted, or committed-but-unpushed. While an incoming file is in
    # flux here, its replacement is held; publishing is the one exit, and the
    # gate never blocks the publish flow itself ($AtPublish).
    if (-not $AtPublish) {
        $unpushed = @(git diff --name-only "origin/$($script:SC_Branch)...HEAD" 2>$null)
        $typing   = (Get-EditorReport).Dirty
        $hot = @()
        foreach ($f in $incoming) {
            if ($unpushed -contains $f -or
                $typing -contains $f -or
                @(git status --porcelain -- $f 2>$null).Count -gt 0) { $hot += $f }
        }
        if ($hot.Count -gt 0) {
            if (-not $script:SC_HoldStart) { $script:SC_HoldStart = Get-Date }
            $held = ((Get-Date) - $script:SC_HoldStart).TotalSeconds
            if (-not $script:SC_Holding) {
                Write-Log "holding the download - your work on $($hot -join ', ') is not published yet; it lands at your next publish" 'Yellow'
                $script:SC_Holding = $true
            }
            if ($held -ge 600 -and -not $script:SC_HoldNagged) {
                # Deliberately NOT forcing the merge - the design routes every
                # crossing through the publish moment. Long holds get a human
                # nudge instead of a silent override.
                $script:SC_HoldNagged = $true
                Show-Alert -Title 'teamsync: changes are waiting' -Body (
                    "Your teammate changed: $($hot -join ', ')`n`n" +
                    "Those files are also mid-work on this machine, so the download " +
                    "is waiting for you. It has been ten minutes.`n`n" +
                    "Finish the piece and press Publish now (or let the four-minute " +
                    "quiet window fire) - both sides then come together in one step.")
            }
            return $true                 # not an error: try again next tick
        }
    }
    $script:SC_Holding    = $false
    $script:SC_HoldStart  = $null
    $script:SC_HoldNagged = $false

    Write-Log "$behind new commit(s) from the other side - integrating" 'Cyan'
    Invoke-CommitLocal | Out-Null    # protect local work before moving anything
    $mine = @(git diff --name-only "origin/$($script:SC_Branch)...HEAD" 2>$null)

    # Crossed edits, decided BEFORE the merge so both originals can be saved
    # exactly as they were: my committed version, theirs, and the common base.
    $crossed = @($mine | Where-Object { $incoming -contains $_ })
    $crossDir = ''
    if ($crossed.Count -gt 0) {
        $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss') + '-crossed'
        $crossDir = Join-Path $script:SC_ConflictRoot $stamp
        New-Item -ItemType Directory -Path $crossDir -Force | Out-Null
        $mb = git merge-base HEAD "origin/$($script:SC_Branch)" 2>$null
        foreach ($f in $crossed) {
            # The whole path, not the leaf. Two files called notes.md in
            # different folders both wrote "notes.MINE.md" here, so the second
            # silently overwrote the first and CROSSED.md pointed both entries
            # at the survivor - the reader then edits one folder's file from
            # the other folder's content.
            $flat = Save-Name $f
            $base = [IO.Path]::GetFileNameWithoutExtension($flat)
            $ext  = [IO.Path]::GetExtension($flat)
            # -Encoding UTF8 on every one of these. Without it Windows
            # PowerShell 5.1 writes the ANSI codepage and every non-ASCII
            # character becomes a literal "?" - measured: a file reading
            # "سلام دنیا" came out of this exact pipeline as "???? ????".
            # These copies are the ONLY untouched originals, so losing them
            # loses the work they were kept to protect.
            git show "HEAD:$f" 2>$null |
                Set-Content -LiteralPath (Join-Path $crossDir "$base.MINE$ext") -Encoding UTF8
            git show "origin/$($script:SC_Branch):$f" 2>$null |
                Set-Content -LiteralPath (Join-Path $crossDir "$base.THEIRS$ext") -Encoding UTF8
            if ($mb) {
                git show "${mb}:$f" 2>$null |
                    Set-Content -LiteralPath (Join-Path $crossDir "$base.BASE$ext") -Encoding UTF8
            }
        }
        @("Crossed edits, merged automatically.",
          "",
          "You and at least one other person changed these file(s) in the same window:",
          ($crossed | ForEach-Object { "  $_" }),
          "",
          "Neither edit was based on the other. The lines merged cleanly, so the",
          "live files now carry BOTH changes - nothing was lost and nothing is",
          "blocked. These copies are the untouched originals:",
          "  NAME.MINE.ext   - your version, exactly as you committed it",
          "  NAME.THEIRS.ext - the version that arrived from GitHub. That is the",
          "                    shared branch, so with more than two people it may",
          "                    already carry SEVERAL teammates' work.",
          "  NAME.BASE.ext   - the version everybody started from",
          "",
          "The names above keep the whole path, with folders joined by __, so two",
          "files with the same name in different folders stay apart.",
          "",
          "Open the live file(s) and give the combined result a final human look.") |
            ForEach-Object { $_ } |
            Set-Content -LiteralPath (Join-Path $crossDir 'CROSSED.md') -Encoding UTF8
    }

    git rebase "origin/$($script:SC_Branch)" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Report-Conflict -Phase 'download'; return $false }
    if ($incoming.Count -gt 0) {
        # Named so that someone with one of these files open in an editor knows
        # to reload it before saving - a stale buffer saved over the integrated
        # file is the one overwrite no sync tool can see coming.
        $shown = ($incoming | Select-Object -First 5) -join ', '
        if ($incoming.Count -gt 5) { $shown += " (+$($incoming.Count - 5) more)" }
        Write-Log "integrated $behind commit(s): $shown" 'Green'
    } else {
        Write-Log "integrated $behind commit(s)" 'Green'
    }
    # Both sides changed the same file in the same window and git merged the
    # lines cleanly - so git stays silent, but silence is wrong: text that
    # merges is not always meaning that merges. Say it, loudly enough to see.
    if ($crossed.Count -gt 0) {
        $list = $crossed -join "`n  "
        Write-Log "CROSSED EDITS on: $($crossed -join ', ') - merged cleanly, both originals kept in _conflicts; give it a final look" 'Yellow'
        Show-Alert -Title 'teamsync: crossed edits - review the result' -OpenFolder $crossDir -Body (
            "You and your teammate changed the same file(s) at the same time:`n`n  $list`n`n" +
            "Neither edit was based on the other. The lines merged cleanly, so the " +
            "live files now carry both changes - nothing was lost and nothing is blocked.`n`n" +
            "Both untouched originals were kept next to this note. " +
            "Open the live file(s) and give the combined result a final human look.")
    }
    return $true
}

function Invoke-Publish {
    param([string]$Reason = 'quiet window')

    if ((Test-Rebasing) -or (Get-Unmerged).Count -gt 0) {
        Write-Log 'not publishing - a conflict is still open' 'Yellow'
        return $false
    }

    # Adding work needs no permission; destroying it does. A deletion or a
    # reversion is published exactly like an edit - it was measured removing a
    # file from every teammate's machine while the log said "pushed 1
    # commit(s)" - so the machine no longer makes that call on its own.
    try {
        $destructive = Get-DestructiveChanges
        $script:SC_GuardFailSaid = $null
    } catch {
        # Could not look. That is not the same as "nothing to see": hold the
        # publish rather than send something nobody was able to check.
        $why = $_.Exception.Message
        if ($script:SC_GuardFailSaid -ne $why) {
            $script:SC_GuardFailSaid = $why
            Write-Log "not publishing - $why; trying again on the next pass" 'Yellow'
        }
        return $false
    }
    $script:SC_Destructive = $destructive
    if (@($destructive.Deleted).Count -or @($destructive.Reverted).Count) {
        if (-not (Test-DestructiveApproved $destructive)) {
            $lines = @()
            if (@($destructive.Deleted).Count)  { $lines += "removed: $((@($destructive.Deleted)  | Select-Object -First 12) -join ', ')" }
            if (@($destructive.Reverted).Count) { $lines += "put back to an older version: $((@($destructive.Reverted) | Select-Object -First 12) -join ', ')" }
            if (-not $script:SC_DestructiveSaid -or $script:SC_DestructiveSaid -ne (Get-DestructiveSignature $destructive)) {
                $script:SC_DestructiveSaid = Get-DestructiveSignature $destructive
                Write-Log "publishing is PAUSED - this would destroy work on everybody's machine" 'Red'
                foreach ($l in $lines) { Write-Log "  $l" 'Yellow' }
                Write-Log '  say so in the app, or put them back - nothing goes out until you do' 'Yellow'
                Show-Alert -Title 'teamsync: this would delete work for everyone' -Body (
                    "Publishing is paused.`n`n" + ($lines -join "`n") + "`n`n" +
                    "If you meant it, confirm it in TeamSync and it goes out. " +
                    "If it was a mistake, choose to put them back - every version " +
                    "is still here.") -OpenFolder $script:SC_Repo
            }
            return $false
        }
    } else {
        $script:SC_DestructiveSaid = $null
    }

    # Something LARGE going out for the first time waits too - see the large
    # new content part of the guard block for why.
    if (Test-LargeHeld) { return $false }

    Invoke-CommitLocal | Out-Null
    $ahead = Get-AheadCount
    if ($ahead -eq 0) {
        # Nothing of ours to send - but publishing is also the ONLY moment
        # that lets held downloads through, and returning here skipped it.
        # A file left dirty, or merely announced, therefore blocked every
        # teammate's incoming work with no way out: the hold's exit could
        # only fire when there was something to push, and there never was.
        # Local work is already committed above, so integrating here is the
        # same safe step the full path takes.
        if ($script:SC_Holding) {
            Write-Log "nothing to publish ($Reason) - releasing held downloads" 'DarkGray'
            return (Invoke-Integrate -AtPublish)
        }
        Write-Log "nothing to publish ($Reason)" 'DarkGray'
        return $true
    }

    if (-not (Invoke-Integrate -AtPublish)) { return $false }

    git push -q origin $script:SC_Branch 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # Someone pushed in the meantime. Take theirs, then try once more.
        if (-not (Invoke-Integrate -AtPublish)) { return $false }
        git push -q origin $script:SC_Branch 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Set-NetState $false; return $false }
    }
    Set-NetState $true
    Clear-Approvals
    # Published means the announced work is out - however the publish happened.
    # push-now clears this too, but the engine's own quiet-window publish must
    # not leave a stale announcement holding the other side's downloads.
    Remove-Item -LiteralPath (Join-Path $script:SC_Repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
    Write-Log "pushed $ahead commit(s) [$Reason]" 'Green'
    return $true
}
