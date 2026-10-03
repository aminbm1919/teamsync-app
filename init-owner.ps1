# Turn a folder into a shared private GitHub project, then keep it synced.
#
# Role   : one-time setup for the person who starts the project.
# Input  : -Path <an existing folder, with or without its own git history>
# Output : a NEW private GitHub repository holding that folder (and its history,
#          if it has one), the people in -Friend invited, and teamsync running.
# Never  : creates a public repository; publishes into a repository it did not
#          create right here; rewrites or discards any history.
#
# Sharing makes a new private WORKSPACE for developing together. It is not a
# way of publishing a project somewhere it already lives:
#
#   - A folder that is already a git repository is shared like any other. Its
#     history comes along; a branch with another name becomes 'main', the name
#     every shared project uses; and an 'origin' it already had - its own
#     GitHub repository, say - is kept, untouched, as 'origin-before-teamsync'.
#   - A folder already shared by this app is never set up a second time. The
#     app sees that before it gets here and adds the people to the existing
#     project instead; run by hand on such a folder, this script refuses.
#
# What goes, and what stays on this machine:
#
#   - The project's own .gitignore applies, as always.
#   - Things tools make and every machine rebuilds - node_modules, Python's
#     caches and virtual environments, the system's thumbnail files - stay
#     home by default (see $machineMade below). Build output does not: for
#     some projects that IS the work.
#   - -Exclude names more: files or folders that stay on this machine.
#   - Large content (a file of 25 MB or more, or one item adding 50 MB or
#     more) stops the setup before anything is committed or uploaded, unless
#     -AllowLarge says the person has seen it and agreed. Once uploaded it is
#     in the history for good.
#   - -Preview says all of this - item by item, with sizes - and touches
#     nothing. The app's share window is built on it.
#
# Usage:
#   pwsh init-owner.ps1 -Path "C:\...\my-project" -Me amin -MyEmail me@example.com
#
# Run it once per project. Afterwards just start the daemon:
#   pwsh teamsync.ps1 -Path "C:\...\my-project"

param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$RepoName,
    [string]$Me,
    [string]$MyEmail,
    # One name or several, comma separated: "ali,sara". It is deliberately a
    # single string rather than [string[]]: with powershell.exe -File, an array
    # written as "-Friend a b c" binds only "a" to Friend and hands "b" to the
    # NEXT parameter without a word of complaint - measured here, and it would
    # have silently overwritten -Me. One string that this script splits itself
    # cannot be mis-bound.
    [string]$Friend = '',
    [string]$Description = '',
    # What stays on this machine: paths inside the folder, separated by "|" -
    # a character no Windows file name can hold - a folder ending in "/". One
    # string, for the same reason as -Friend.
    [string]$Exclude = '',
    # Upload large content without stopping. The app passes this only after
    # the person has seen the large items and agreed.
    [switch]$AllowLarge,
    # Say what WOULD be shared, and touch nothing.
    [switch]$Preview,
    [switch]$NoWatch
)

$ErrorActionPreference = 'Stop'
function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Note($t) { Write-Host "    $t" -ForegroundColor DarkGray }
function Warn($t) { Write-Host "    $t" -ForegroundColor Yellow }
function Die($t)  { Write-Host $t -ForegroundColor Red; exit 1 }

# Git prints UTF-8; Windows PowerShell reads a program's output in the console's
# code page unless told otherwise, which turns every non-latin path and name
# into garbage. Read it as UTF-8 here, and give the console back as it was.
$script:ConsoleEncodingWas = $null
try { $script:ConsoleEncodingWas = [Console]::OutputEncoding } catch { }
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { }
try {

if (-not (Test-Path -LiteralPath $Path)) { Die "Folder does not exist: $Path" }
$repo = (Resolve-Path -LiteralPath $Path).Path
$hadGit = Test-Path -LiteralPath (Join-Path $repo '.git')

# The sync engine's own bookkeeping, and the usual places secrets live.
$needed = @('_conflicts/', '.teamsync.log', '.teamsync.lock', '.teamsync-push-now', '.teamsync-editor.json', '.teamsync-agent.json', '.teamsync-history.log',
            '.env', '.env.*', '**/*secret*.json', '**/*secrets*.json')
# Made by tools and rebuilt on every machine - and usually the bulk of a
# folder that has them: node_modules alone was 124 of the first real shared
# project's 144 MB. Matched at any depth. Never build output (dist/, build/):
# for some projects that IS the work.
$machineMade = @('node_modules/', '.venv/', '__pycache__/', '*.pyc', '.pytest_cache/', '.mypy_cache/',
                 'Thumbs.db', 'desktop.ini', '.DS_Store')
# Every shared project carries these; staying home would break it for the
# others (no rules for their agents, no publish button).
$reserved = @('.gitignore', '.gitattributes', 'AGENTS.md', 'CLAUDE.md', 'TEAM-PROJECT-REFERENCE.md',
              'push-now.ps1', 'who.ps1', 'working.ps1')
$keep = @()
foreach ($k in @($Exclude -split '\|')) {
    $k = $k.Trim().Replace('\', '/')
    while ($k.StartsWith('./')) { $k = $k.Substring(2) }
    $k = $k.TrimStart('/')
    if (-not $k) { continue }
    if ($k.Contains(':') -or (@($k.TrimEnd('/').Split('/')) -contains '..')) {
        Die "Not a path inside the folder: $k`nNothing was changed."
    }
    if ($reserved -contains $k.TrimEnd('/')) {
        Die "$k is part of every shared project and cannot stay on this machine.`nNothing was changed."
    }
    if ($keep -cnotcontains $k) { $keep += $k }
}
# Nothing is shared yet, so nothing counts as already there: a ref that never
# exists. Never the folder's own origin - that is somebody else's repository.
$nothingShared = 'refs/teamsync-never/none'

# GitHub repository names must be plain latin. The folder name here may not be.
if (-not $RepoName) {
    $RepoName = ((Split-Path $repo -Leaf).ToLower() -replace '[^a-z0-9._-]+', '-').Trim('-')
}
if ($RepoName -notmatch '^[a-z0-9][a-z0-9._-]*$') {
    Die "Could not derive a usable repository name from the folder name.`nPass one yourself, e.g.  -RepoName my-project"
}

. (Join-Path $PSScriptRoot 'sync-core.ps1')

if ($Preview) {
    # What WOULD be shared, measured exactly as the real run measures it - and
    # nothing in the folder is touched: git looks at it through a scratch
    # repository (or, if it has git, its own, read-only), with this run's
    # exclusions given to git from a scratch file rather than written into
    # the project's .gitignore. Machine-readable, one line per fact:
    #   TS-ITEM <tab> top-level item <tab> bytes <tab> files    shared
    #   TS-MACHINE <tab> path <tab> bytes <tab> files           left out by default
    #   TS-HISTORY <tab> bytes <tab> commits                    an existing history
    #   TS-LARGE <tab> item <tab> bytes <tab> files <tab> 1 if in the history
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('teamsync-preview-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $saved = @{}
    foreach ($n in 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0') {
        $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
    }
    try {
        $utf8 = New-Object Text.UTF8Encoding($false)
        $all = @($needed) + $machineMade + @($keep | ForEach-Object { Format-GitIgnoreLine $_ })
        [IO.File]::WriteAllText((Join-Path $tmp 'exclude'), (($all -join "`n") + "`n"), $utf8)
        [IO.File]::WriteAllText((Join-Path $tmp 'machine'), (($machineMade -join "`n") + "`n"), $utf8)
        $ErrorActionPreference = 'Continue'
        if ($hadGit) {
            Set-Location -LiteralPath $repo
            $gd = git rev-parse --absolute-git-dir 2>$null
            if (-not $gd) { Die "Git cannot read the repository in this folder: $repo" }
        } else {
            git init -q (Join-Path $tmp 'scratch') 2>$null | Out-Null
            $gd = Join-Path $tmp 'scratch\.git'
        }
        $env:GIT_DIR = "$gd".Trim()
        $env:GIT_WORK_TREE = $repo
        $env:GIT_CONFIG_COUNT = '1'
        $env:GIT_CONFIG_KEY_0 = 'core.excludesFile'
        $env:GIT_CONFIG_VALUE_0 = Join-Path $tmp 'exclude'
        Set-Location -LiteralPath $tmp
        Set-GitOutputUtf8

        $under = { param($p) foreach ($k in $keep) { if ($p -ceq $k.TrimEnd('/') -or ($k.EndsWith('/') -and $p.StartsWith($k, [StringComparison]::Ordinal))) { return $true } }; $false }
        $items = New-Object 'System.Collections.Generic.Dictionary[string,long[]]' ([StringComparer]::Ordinal)
        foreach ($p in @(Split-DGZ (Invoke-DGGit @('ls-files', '-z', '--cached', '--others', '--exclude-standard') 'ls-files'))) {
            if (& $under $p) { continue }
            $fi = $null
            try { $fi = New-Object IO.FileInfo ([IO.Path]::Combine($repo, $p)) } catch { continue }
            if (-not $fi.Exists) { continue }
            $cut = $p.IndexOf('/')
            $top = if ($cut -gt 0) { $p.Substring(0, $cut + 1) } else { $p }
            if (-not $items.ContainsKey($top)) { $items[$top] = [long[]]@(0, 0) }
            $items[$top][0] += $fi.Length
            $items[$top][1] += 1
        }
        foreach ($kv in $items.GetEnumerator()) { Write-Output ("TS-ITEM`t{0}`t{1}`t{2}" -f $kv.Key, $kv.Value[0], $kv.Value[1]) }

        foreach ($p in @(Split-DGZ (Invoke-DGGit @('ls-files', '-z', '--others', '--ignored', '--directory',
                                                    "--exclude-from=$(Join-Path $tmp 'machine')") 'ls-files machine-made'))) {
            $full = [IO.Path]::Combine($repo, $p.TrimEnd('/'))
            $bytes = [long]0; $count = 0
            if ($p.EndsWith('/')) {
                try {
                    foreach ($f in [IO.Directory]::EnumerateFiles($full, '*', [IO.SearchOption]::AllDirectories)) {
                        $bytes += (New-Object IO.FileInfo $f).Length; $count++
                    }
                } catch { }
            } else {
                try { $bytes = (New-Object IO.FileInfo $full).Length; $count = 1 } catch { }
            }
            Write-Output ("TS-MACHINE`t{0}`t{1}`t{2}" -f $p, $bytes, $count)
        }

        if ($hadGit -and (git rev-parse -q --verify HEAD 2>$null)) {
            $usage = git rev-list --disk-usage --objects HEAD 2>$null
            $commits = git rev-list --count HEAD 2>$null
            if ($LASTEXITCODE -eq 0 -and $usage) { Write-Output ("TS-HISTORY`t{0}`t{1}" -f "$usage".Trim(), "$commits".Trim()) }
        }

        foreach ($l in @(Get-LargeOutgoingIn -Root $repo -Upstream $nothingShared)) {
            Write-Output ("TS-LARGE`t{0}`t{1}`t{2}`t{3}" -f $l.Item, $l.Bytes, $l.Files, $(if ($l.Committed) { 1 } else { 0 }))
        }
        Write-Output 'TS-END'
    } catch {
        Write-Output "TS-ERROR`t$($_.Exception.Message)"
        exit 1
    } finally {
        foreach ($n in $saved.Keys) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
        Set-Location -LiteralPath ([IO.Path]::GetTempPath())
        try { [IO.Directory]::Delete($tmp, $true) } catch { }
    }
    exit 0
}

if (-not (Test-Prerequisites)) { exit 1 }

Set-Location -LiteralPath $repo

# Everything that can refuse is asked BEFORE the folder is touched, so that a
# refusal leaves it exactly as it was.
$ErrorActionPreference = 'Continue'
$owner = (gh api user --jq '.login' 2>$null)
if (-not $owner) { Die 'Could not ask GitHub who you are. Check the connection, then: gh auth status' }
gh repo view "$owner/$RepoName" --json name 2>$null | Out-Null
$taken = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if ($taken) {
    Die "A repository called '$RepoName' already exists on your account ($owner/$RepoName).`nChoose another name in the 'Repository name' box and start again.`nNothing in the folder was changed."
}

$keptRemote = ''
if ($hadGit) {
    $ErrorActionPreference = 'Continue'
    if (-not (git rev-parse --git-dir 2>$null)) { Die "Git cannot read the repository in this folder: $repo" }
    git config core.quotePath false 2>$null | Out-Null
    $gitDir = git rev-parse --git-dir 2>$null
    if (-not [IO.Path]::IsPathRooted($gitDir)) { $gitDir = Join-Path $repo $gitDir }
    foreach ($busy in 'rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD') {
        if (Test-Path -LiteralPath (Join-Path $gitDir $busy)) {
            Die "A merge or rebase is in progress in this folder. Finish it (or abort it) first.`nNothing was changed."
        }
    }
    $branch = git symbolic-ref --short -q HEAD 2>$null
    if (-not $branch) { Die "This repository is not on a branch (a detached HEAD). Check out a branch first.`nNothing was changed." }
    if ($branch -ne 'main') {
        git show-ref --verify -q refs/heads/main 2>$null
        if ($LASTEXITCODE -eq 0) {
            Die "This repository is on '$branch', and it also has a separate branch called 'main'.`nA shared project works on 'main' only - merge the two, or check out 'main', then start again.`nNothing was changed."
        }
    }
    $origin = git remote get-url origin 2>$null
    if ($origin) {
        # Never set up twice: the app checks this before it gets here, and a
        # hand run must not create a second project for the same work. The
        # files this app plants are the local sign; its refs on the server are
        # the remote one.
        $planted = (Test-Path -LiteralPath (Join-Path $repo 'push-now.ps1')) -and
                   (Test-Path -LiteralPath (Join-Path $repo 'TEAM-PROJECT-REFERENCE.md'))
        $ts = git ls-remote origin 'refs/teamsync/*' 2>$null
        if ($planted -or $ts) {
            Die "This folder is already a shared project ($origin).`nAdd people to it from the app instead (Add people). Nothing was changed."
        }
    }
    $hasCommits = [bool](git rev-parse -q --verify HEAD 2>$null)
    $ErrorActionPreference = 'Stop'

    Step 'Preparing the existing repository'
    if ($branch -ne 'main') {
        if ($hasCommits) {
            git branch -m $branch main
        } else {
            git symbolic-ref HEAD refs/heads/main     # no commit yet: nothing to rename
        }
        Note "your branch '$branch' is now called 'main' - every shared project works on that name"
    }
    if ($origin) {
        $keptRemote = 'origin-before-teamsync'
        $n = 2
        while (@(git remote) -contains $keptRemote) { $keptRemote = "origin-before-teamsync-$n"; $n++ }
        git remote rename origin $keptRemote
        Note "this folder's own remote is kept, untouched, as '$keptRemote':"
        Note "  $origin"
        Note 'the shared project gets a NEW private repository; nothing is sent to that one'
    }
}

Step 'Preparing the folder'
# Keep sync bookkeeping, what tools rebuild, and what the person chose out of
# the shared history. Appended, never rewritten: the project's own lines stay
# as they are. (Add-Content used to glue the first new line onto a last line
# that had no line break - "node_modules" became "node_modules_conflicts/",
# and the project's own rule stopped working.)
Add-GitIgnoreLines -Root $repo -Lines (@($needed) + $machineMade + @($keep | ForEach-Object { Format-GitIgnoreLine $_ }))
foreach ($k in $keep) { Note "stays on this machine: $k" }

# The "publish now" button, placed inside the project so an AI agent can find and
# run it without knowing where this toolkit is installed. It is committed, so the
# other person gets it automatically when they connect.
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'push-now.template.ps1') `
          -Destination (Join-Path $repo 'push-now.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'working.template.ps1') `
          -Destination (Join-Path $repo 'working.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'who.template.ps1') `
          -Destination (Join-Path $repo 'who.ps1') -Force

# The reference both people and both agents work against. A copy travels with the
# project so the other machine has it too, without installing this toolkit.
# Inside the packaged app everything sits side by side; from source it is one
# level up. Accept either.
$refSrc = @(
    (Join-Path $PSScriptRoot 'TEAM-PROJECT-REFERENCE.md')
    (Join-Path $PSScriptRoot '..\TEAM-PROJECT-REFERENCE.md')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($refSrc) {
    Copy-Item -LiteralPath $refSrc -Destination (Join-Path $repo 'TEAM-PROJECT-REFERENCE.md') -Force
} else {
    Write-Host '    WARNING: TEAM-PROJECT-REFERENCE.md not found - agents will have no rules.' -ForegroundColor Yellow
}

# AGENTS.md is what Codex looks for; CLAUDE.md is what Claude Code reads. A
# project that has neither gets short files that defer to the reference. A
# project that ALREADY has them - its own instructions, written for working
# alone - keeps every word, and gets a short marked note at the very top: an
# agent that reads only its usual file would otherwise never learn that it is
# now one of several, and the rules for that live in the reference.
$teamNote = @(
    '<!-- teamsync:start - added when this folder became a shared TeamSync project; keep it first -->'
    '> **This is a shared TeamSync project.** Several people - each with their own AI'
    '> agent - work in this folder at the same time. Before any other action, read'
    '> `TEAM-PROJECT-REFERENCE.md` in this folder: how to publish, what to do in a'
    '> conflict, and what never to touch. Run `who.ps1` before editing,'
    '> `working.ps1 <files>` before writing, and `push-now.ps1` when a piece is done.'
    '<!-- teamsync:end -->'
    ''
) -join "`n"
function Add-TeamNote([string]$file) {
    $text = [IO.File]::ReadAllText($file)
    if ($text.Contains('<!-- teamsync:start')) { return }
    [IO.File]::WriteAllText($file, $teamNote + "`n" + $text, (New-Object Text.UTF8Encoding $false))
    Note "$(Split-Path $file -Leaf) keeps its own text; a short team note was added at the top"
}
$agents = Join-Path $repo 'AGENTS.md'
if (-not (Test-Path $agents)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'AGENTS.project.md') -Destination $agents -Force
} else {
    Add-TeamNote $agents
}

# Autosave, seeded into the project so it reaches every machine through the
# repository itself. With it, an open file being typed in becomes a saved file
# within a second - which is what lets the sync engine SEE work in progress:
# the read-side gate holds downloads of it, and the partner's "working on this
# file" warning lights up seconds after work starts instead of minutes.
# Editors ignore settings they do not know, so this is inert outside VS Code.
$vsdir = Join-Path $repo '.vscode'
if (-not (Test-Path (Join-Path $vsdir 'settings.json'))) {
    New-Item -ItemType Directory -Path $vsdir -Force | Out-Null
    @'
{
    "files.autoSave": "afterDelay",
    "files.autoSaveDelay": 1000
}
'@ | Set-Content -LiteralPath (Join-Path $vsdir 'settings.json') -Encoding ascii
}
$claude = Join-Path $repo 'CLAUDE.md'
if (-not (Test-Path $claude)) {
    Set-Content -LiteralPath $claude -Encoding UTF8 -Value @(
        '# CLAUDE.md'
        ''
        'The rules for this repository live in `AGENTS.md`, so that Claude Code and'
        'Codex read the same thing. **Read `AGENTS.md` now, before any other action.**'
        ''
        'Do not duplicate rules here. A rule in two places is a rule that will'
        'eventually disagree with itself.'
    )
} else {
    Add-TeamNote $claude
}

# Without this, a CRLF/LF difference between two Windows machines makes git see
# every line of a file as changed, turning ordinary edits into total conflicts.
$attrs = Join-Path $repo '.gitattributes'
$newAttrs = -not (Test-Path $attrs)
if ($newAttrs) { Set-Content -LiteralPath $attrs -Value '* text=auto eol=lf' -Encoding UTF8 }

if (-not $hadGit) {
    Step 'Initializing git'
    git init -b main | Out-Null
    git config core.quotePath false
}
if (git config core.hooksPath) { git config core.hooksPath '.git/hooks' }
if ($Me)      { git config user.name  $Me }
if ($MyEmail) { git config user.email $MyEmail }

$ErrorActionPreference = 'Continue'
if ($hadGit -and $keep) {
    # Already part of this folder's history: from now on it is no longer
    # sent, but the versions already in the history go up with it.
    foreach ($k in $keep) {
        $tracked = @(git --literal-pathspecs ls-files -- $k.TrimEnd('/') 2>$null)
        if ($tracked.Count -gt 0) {
            git --literal-pathspecs rm -r -q --cached --ignore-unmatch -- $k.TrimEnd('/') 2>$null | Out-Null
            Warn "$k is in this folder's own history: it stays home from now on, but its earlier versions go up with the history"
        }
    }
}

# Large content waits for the person's word, here as on every later publish:
# once uploaded it is in the history for good.
try {
    $large = @(Get-LargeOutgoingIn -Root $repo -Upstream $nothingShared)
} catch {
    Die "Could not measure what would be uploaded: $($_.Exception.Message)`nNothing was uploaded."
}
if ($large.Count -gt 0 -and -not $AllowLarge) {
    Write-Host 'This would upload LARGE content:' -ForegroundColor Red
    foreach ($l in ($large | Select-Object -First 15)) { Warn "  $(Format-LargeLine $l)" }
    if ($large.Count -gt 15) { Warn "  ... and $($large.Count - 15) more" }
    Die ("Nothing was committed or uploaded.`n" +
         "Share again from the app and choose what stays on this machine - or, to upload it anyway, run this again with -AllowLarge.")
}
$ErrorActionPreference = 'Stop'

Step 'Recording the starting point'
$ErrorActionPreference = 'Continue'
if ($hadGit -and $newAttrs) {
    # The line-ending rule above applies to files already in the history too;
    # settle them now, in this one commit, rather than as a trickle of
    # phantom "changes" the engine would publish one by one.
    git add --renormalize . 2>$null | Out-Null
}
git add -A 2>$null | Out-Null
$staged = @(git diff --cached --name-only 2>$null).Count
if ($staged -gt 0) {
    git commit -q -m 'chore: start shared project' 2>$null | Out-Null
}
$ErrorActionPreference = 'Stop'
if (-not (git rev-parse -q --verify HEAD 2>$null)) { Die 'The folder is empty - there is nothing to share yet.' }

$commits = [int](git rev-list --count HEAD)
$files = @(git ls-files).Count
Note "$files file(s) in $commits commit(s) will be uploaded"

# Said before anything leaves the machine, because once it is on GitHub every
# person invited can read it - and a private repository can be made public
# later by anybody with admin rights.
$ErrorActionPreference = 'Continue'
$risky = @(git ls-files 2>$null | Where-Object {
    $_ -match '(^|/)\.env($|\.)' -or $_ -match '(?i)secret|credential|password' -or
    $_ -match '(?i)\.(pem|key|pfx|p12)$' -or $_ -match '(^|/)id_(rsa|ed25519)'
})
if ($risky.Count -gt 0) {
    Warn 'These files look like they may hold secrets, and they are part of the project:'
    foreach ($r in ($risky | Select-Object -First 10)) { Warn "  $r" }
    Warn 'Everyone you invite will be able to read them.'
}
$mails = @(git log --format='%ae' 2>$null | Sort-Object -Unique |
           Where-Object { $_ -and $_ -notmatch '@users\.noreply\.github\.com$' })
if ($mails.Count -gt 0) {
    Warn "The history names these e-mail addresses as authors: $($mails -join ', ')"
    Warn 'GitHub shows them to everyone who can see the project.'
}
$ErrorActionPreference = 'Stop'

Step "Creating the private repository: $RepoName"
$desc = if ($Description) { $Description } else { "Shared project: $RepoName" }
$ErrorActionPreference = 'Continue'
gh repo create $RepoName --private --source=. --remote=origin --description $desc --push
$created = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $created) {
    $ErrorActionPreference = 'Continue'
    if ($keptRemote -and -not (git remote get-url origin 2>$null)) {
        # Put the folder's own remote back where it was.
        git remote rename $keptRemote origin 2>$null | Out-Null
    }
    Die ("gh repo create failed. Check the connection and press Start again - it carries on from here.`n" +
         "Nothing was uploaded. The folder keeps the shared-project files in one commit" +
         $(if ($hadGit) { ", on a branch called 'main'." } else { '.' }))
}

$friends = @($Friend -split '[,;]' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$invited = @()
foreach ($who in $friends) {
    Step "Inviting $who"
    $ErrorActionPreference = 'Continue'
    gh api -X PUT "repos/$owner/$RepoName/collaborators/$who" -f permission=push | Out-Null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = 'Stop'
    if (-not $ok) {
        Write-Host "    Could not invite $who automatically. Add them at:" -ForegroundColor Yellow
        Write-Host "    https://github.com/$owner/$RepoName/settings/access"
    } else {
        $invited += $who
        Write-Host "    Invitation sent to $who." -ForegroundColor DarkGray
    }
}
if ($invited) {
    Write-Host '    Their app shows the invitation and joins with one press.' -ForegroundColor DarkGray
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host "  Repository : https://github.com/$owner/$RepoName"
Write-Host "  Folder     : $repo"
if ($keptRemote) { Write-Host "  Kept       : the folder's own remote, as '$keptRemote'" }
Write-Host ''
if ($invited) {
    Write-Host "Invited: $($invited -join ', ')" -ForegroundColor Cyan
    Write-Host '  They open TeamSync and the invitation is waiting on the first screen.'
} else {
    Write-Host 'Nobody was invited yet. Add people from the app, or at:' -ForegroundColor Cyan
    Write-Host "  https://github.com/$owner/$RepoName/settings/access"
}
Write-Host ''
Write-Host 'Without the app, the other side would run:' -ForegroundColor DarkGray
Write-Host "  pwsh init-friend.ps1 -RepoName $RepoName -Owner $owner -Path `"C:\somewhere\$RepoName`""
Write-Host ''

if ($NoWatch) {
    Write-Host 'Start syncing whenever you are ready:' -ForegroundColor Yellow
    Write-Host "  pwsh teamsync.ps1 -Path `"$repo`""
} else {
    Write-Host 'Starting teamsync...' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'teamsync.ps1') -Path $repo
}

} finally {
    if ($script:ConsoleEncodingWas) { try { [Console]::OutputEncoding = $script:ConsoleEncodingWas } catch { } }
}
