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
    $script:SC_Branch       = $Branch
    $script:SC_NoPopup      = [bool]$NoPopup
    $script:SC_Log          = Join-Path $Repo '.teamsync.log'
    $script:SC_ConflictRoot = Join-Path $Repo '_conflicts'
    $script:SC_Lock         = Join-Path $Repo '.teamsync.lock'
    $script:SC_Signal       = Join-Path $Repo '.teamsync-push-now'
    $script:SC_Offline      = $false
    $script:SC_PendingPublish = 0
    Disable-GitPathQuoting
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
    git push -q origin "HEAD:$ref" 2>$null | Out-Null
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
    # Three outcomes, and the middle one is the point:
    #   free            - nothing to do
    #   taken by ME     - the same GitHub account on another machine. That is
    #                     a normal thing to do and must not be refused; the
    #                     two just have to be tellable apart, so a number is
    #                     added and the colleagues see "amin-2".
    #   taken by ANOTHER account - a real collision. Refused, because the two
    #                     would delete each other's presence and each other's
    #                     file warnings without end.
    #
    # The rename is written to this project's git config, not only to the
    # presence key, so the commit author matches too - that pairing is what
    # attributes anybody's work to them at all.
    #
    # Returns @{ Name; Action = 'ok'|'renamed'|'refused'; Owner; From }
    $me = Get-PresenceName
    if (-not $me) { return @{ Name = ''; Action = 'ok' } }
    $mine = Get-MyGitHubLogin

    # Did WE take this numbered name, and is the original free again? A rename
    # must be able to undo itself: it is a machine's guess about another
    # machine, and a guess that can only ever accumulate would leave somebody
    # as name-7 for ever because of restarts nobody remembers.
    $came = git config --local teamsync.renamedfrom 2>$null
    if ($came) {
        $came = "$came".Trim()
        $owner0 = Get-NameOwner $came
        if ((-not $owner0 -or -not $mine -or $owner0 -eq $mine) -and -not (Test-NameBeating $came)) {
            git config user.name $came 2>$null | Out-Null
            git config --local --unset teamsync.renamedfrom 2>$null | Out-Null
            $script:SC_IdentityRef = $null
            return @{ Name = $came; Action = 'restored'; From = $me }
        }
    }

    $owner = Get-NameOwner $me

    if ($owner -and $mine -and $owner -ne $mine) {
        return @{ Name = $me; Action = 'refused'; Owner = $owner }
    }
    # Free, or registered to our own account. Registration is not use: this
    # machine registered the name on its last run too. What decides is whether
    # a beat is arriving under it RIGHT NOW from somewhere that is not us -
    # judging by registration instead would bump the number on every restart,
    # for ever.
    if (-not (Test-NameBeating $me)) { return @{ Name = $me; Action = 'ok' } }

    for ($n = 2; $n -lt 100; $n++) {
        $try = "$me-$n"
        $who = Get-NameOwner $try
        if ($who -and $mine -and $who -ne $mine) { continue }   # somebody else's
        if (Test-NameBeating $try) { continue }                 # a live machine of ours
        git config user.name $try 2>$null | Out-Null
        # Remember where we came from, so this can be given back when the
        # original name falls quiet again.
        git config --local teamsync.renamedfrom $me 2>$null | Out-Null
        $script:SC_IdentityRef = $null
        return @{ Name = $try; Action = 'renamed'; From = $me }
    }
    return @{ Name = $me; Action = 'ok' }
}

function Get-MyLastPresenceRef {
    # The last beat THIS machine published, remembered across restarts.
    #
    # In memory alone it was useless for the question it exists to answer: a
    # restart forgets it, and the beat the previous run left behind - still
    # fresh, because it stopped seconds ago - then looks like a second machine
    # of ours. The app renamed itself to name-2 on its own restart and the
    # person watched themselves appear in the team list as a stranger.
    #
    # Kept in the project's own git config, which is local to this clone and
    # never travels.
    if ($script:SC_MyPresenceRef) { return $script:SC_MyPresenceRef }
    $v = git config --local teamsync.lastpresence 2>$null
    if ($v) { return "$v".Trim() }
    return ''
}

function Set-MyLastPresenceRef {
    param([string]$Ref)
    $script:SC_MyPresenceRef = $Ref
    if ($Ref) { git config --local teamsync.lastpresence $Ref 2>$null | Out-Null }
    else { git config --local --unset teamsync.lastpresence 2>$null | Out-Null }
}

function Test-NameBeating {
    # Is a presence beat arriving under this name right now, from a machine
    # that is not this one?
    param([string]$Name)
    $now  = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $mine = Get-MyLastPresenceRef
    foreach ($line in @(git ls-remote origin "refs/teamsync/presence/$Name/*" 2>$null)) {
        $ref = ($line -split "`t")[-1]
        if (-not $ref -or $ref -eq $mine) { continue }
        $parts = $ref -split '/'
        if ($parts.Count -lt 5) { continue }
        $ts = 0
        [void][long]::TryParse($parts[4], [ref]$ts)
        if (($now - $ts) -le 150) { return $true }
    }
    return $false
}

function Test-NameCollision {
    # Is somebody else already publishing under our name?
    #
    # Every ref this engine owns is keyed on the sanitised git user.name, and
    # each engine treats that whole namespace as its own property: the
    # first-beat sweep deletes every presence ref under our name, and the
    # pending reconcile deletes every announcement under it that we do not
    # ourselves want. Two people who resolve to the same name - two who left
    # it at "User", or "amin" and "Amin" - therefore delete each other's
    # heartbeats and each other's file announcements, forever. The collision
    # warning would be silently switched off for exactly the pair most likely
    # to collide, and nothing would ever say so.
    #
    # Compared case-INSENSITIVELY on purpose, unlike the "is this mine?"
    # checks elsewhere: on Windows the ref store folds case, so 'amin' and
    # 'Amin' would land in one directory even though git's ref names are
    # case-sensitive in principle. Two names that differ only in case are a
    # collision here whether or not git thinks so.
    #
    # Returns the colliding name, or '' when the coast is clear.
    $me = Get-PresenceName
    if (-not $me) { return '' }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $mineRef = $script:SC_MyPresenceRef
    foreach ($line in @(git ls-remote origin 'refs/teamsync/presence/*' 2>$null)) {
        $ref = ($line -split "`t")[-1]
        if (-not $ref -or $ref -eq $mineRef) { continue }
        $parts = $ref -split '/'
        if ($parts.Count -lt 5) { continue }
        $who = $parts[3]
        if ($who -cne $me -and $who -eq $me) {
            # differs only in case: still one directory on this filesystem
            return $who
        }
        if ($who -cne $me) { continue }
        # Same spelling exactly. It is ours only if it is a beat we published
        # in this run; anything else fresh belongs to somebody else.
        $ts = 0
        [void][long]::TryParse($parts[4], [ref]$ts)
        if (($now - $ts) -le 150) { return $who }
    }
    return ''
}

function Publish-Presence {
    $me = Get-PresenceName
    if (-not $me) { return }
    $ts  = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $new = "refs/teamsync/presence/$me/$ts"
    $old = $script:SC_MyPresenceRef

    git push -q origin "HEAD:$new" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { return }          # offline; try again next tick
    Set-MyLastPresenceRef $new

    if ($old) {
        # Drop the previous beat separately, so its failure cannot cancel the new one.
        if ($old -ne $new) { git push -q origin ":$old" 2>$null | Out-Null }
    } else {
        # First beat of this run. A run that was killed (window closed, machine
        # powered off) never got to tidy up, so its last beat is still on the
        # server. Sweep every beat of ours except the one just published,
        # otherwise they pile up one per restart, forever.
        $mine = @(git ls-remote origin "refs/teamsync/presence/$me/*" 2>$null |
                  ForEach-Object { ($_ -split "`t")[-1] } |
                  Where-Object { $_ -and $_ -ne $new })
        foreach ($r in $mine) { git push -q origin ":$r" 2>$null | Out-Null }
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

    $want = @{}
    # Pending work AND files simply open in the editor: the partner should see
    # "hands on this file" from the moment of opening, not the first save.
    $announce = @(Get-PendingFiles) + @((Get-EditorReport).Open) | Sort-Object -Unique
    foreach ($f in $announce) { $want["refs/teamsync/pending/$me/$(ConvertTo-RefHex $f)"] = $true }

    $have = @{}
    foreach ($line in @(git ls-remote origin "refs/teamsync/pending/$me/*" 2>$null)) {
        $r = ($line -split "`t")[-1]
        if ($r) { $have[$r] = $true }
    }
    if ($LASTEXITCODE -ne 0) { return }          # offline: try again next tick

    # One push per ref, each a network round trip of a couple of seconds. Open
    # a fifteen-file folder in an editor and this loop alone runs for most of a
    # minute. Breathe between them: the heartbeat is what tells the window and
    # push-now that this engine is alive, and letting it go quiet during
    # ordinary work is what made a healthy engine look dead.
    foreach ($r in $want.Keys) {
        if (-not $have.ContainsKey($r)) {
            git push -q origin "HEAD:$r" 2>$null | Out-Null
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
#   refs/teamsync/conflict/<name>/<path-as-hex>

function Publish-Conflict {
    param([string[]]$Files)
    $me = Get-PresenceName
    if (-not $me) { return }
    $want = @{}
    foreach ($f in $Files) { if ($f) { $want["refs/teamsync/conflict/$me/$(ConvertTo-RefHex $f)"] = $true } }

    $have = @{}
    foreach ($line in @(git ls-remote origin "refs/teamsync/conflict/$me/*" 2>$null)) {
        $r = ($line -split "`t")[-1]
        if ($r) { $have[$r] = $true }
    }
    if ($LASTEXITCODE -ne 0) { return }          # offline: try again next tick

    foreach ($r in $want.Keys) {
        if (-not $have.ContainsKey($r)) { git push -q origin "HEAD:$r" 2>$null | Out-Null; Update-Heartbeat }
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
    $me = Get-PresenceName
    if (-not $me) { return }
    $ref = "refs/teamsync/conflictwork/$me"
    $orig = Get-RebaseOrigHead
    if (-not $orig) { git push -q origin ":$ref" 2>$null | Out-Null; return }
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
    # @{ name = @(paths) } for everyone who is not us.
    $me  = Get-PresenceName
    $out = @{}
    foreach ($line in @(git for-each-ref --format='%(refname)' 'refs/teamsync/conflict' 2>$null)) {
        $parts = $line -split '/', 5
        if ($parts.Count -lt 5) { continue }
        $who = $parts[3]
        if ($who -ceq $me) { continue }
        $p = ConvertFrom-RefHex $parts[4]
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
    # @{ name = @(paths) } for everyone who is not us.
    $me  = Get-PresenceName
    $out = @{}
    foreach ($line in @(git for-each-ref --format='%(refname)' 'refs/teamsync/pending' 2>$null)) {
        $parts = $line -split '/'
        if ($parts.Count -lt 5) { continue }
        $who = $parts[3]
        # -cne, not -ne: PowerShell's plain comparison ignores case, so
        # 'Ali-Reza' -eq 'ali-reza' is True. Git's ref store does not ignore
        # case, so those are two real people - and the loose comparison threw
        # away every announcement belonging to the second of them, as if it
        # were our own. With two people the names were distinct by luck; with
        # a team it is a matter of time.
        if ($who -ceq $me) { continue }
        $path = ConvertFrom-RefHex $parts[4]
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

function Get-AheadCount  { $n = git rev-list --count "origin/$($script:SC_Branch)..HEAD" 2>$null; if ($n) { [int]$n } else { 0 } }
function Get-BehindCount { $n = git rev-list --count "HEAD..origin/$($script:SC_Branch)" 2>$null; if ($n) { [int]$n } else { 0 } }

function Invoke-CommitLocal {
    # Snapshot whatever is on disk. Local only - nothing leaves this machine.
    git add -A 2>$null | Out-Null
    if (@(git diff --cached --name-only 2>$null).Count -eq 0) { return $false }
    git commit -q -m "sync: $(Get-Date -Format 'MM-dd HH:mm:ss')" 2>$null | Out-Null
    return $true
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
    if ($LASTEXITCODE -ne 0) { Set-NetState $false; return $false }
    Set-NetState $true

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
    # Published means the announced work is out - however the publish happened.
    # push-now clears this too, but the engine's own quiet-window publish must
    # not leave a stale announcement holding the other side's downloads.
    Remove-Item -LiteralPath (Join-Path $script:SC_Repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
    Write-Log "pushed $ahead commit(s) [$Reason]" 'Green'
    return $true
}
