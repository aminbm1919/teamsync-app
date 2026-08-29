# Turn an existing folder into a shared private GitHub project, then keep it synced.
#
# Role   : one-time setup for the person who starts the project.
# Input  : -Path <an existing folder that already has your project and its docs>
# Output : a private GitHub repository containing that folder and everything under
#          it, your friend invited as a collaborator, and teamsync running.
# Never  : creates a public repository, or touches a folder that is already a repo.
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
    [switch]$NoWatch
)

$ErrorActionPreference = 'Stop'
function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Die($t)  { Write-Host $t -ForegroundColor Red; exit 1 }

if (-not (Test-Path -LiteralPath $Path)) { Die "Folder does not exist: $Path" }
$repo = (Resolve-Path -LiteralPath $Path).Path
if (Test-Path (Join-Path $repo '.git')) {
    Die "This folder is already a git repository. Just run:`n  pwsh teamsync.ps1 -Path `"$repo`""
}

# GitHub repository names must be plain latin. The folder name here may not be.
if (-not $RepoName) {
    $RepoName = ((Split-Path $repo -Leaf).ToLower() -replace '[^a-z0-9._-]+', '-').Trim('-')
}
if ($RepoName -notmatch '^[a-z0-9][a-z0-9._-]*$') {
    Die "Could not derive a usable repository name from the folder name.`nPass one yourself, e.g.  -RepoName my-project"
}

. (Join-Path $PSScriptRoot 'sync-core.ps1')
if (-not (Test-Prerequisites)) { exit 1 }

Set-Location -LiteralPath $repo

Step 'Preparing the folder'
# Keep sync bookkeeping out of the shared history.
$ignore = Join-Path $repo '.gitignore'
$needed = @('_conflicts/', '.teamsync.log', '.teamsync.lock', '.teamsync-push-now', '.teamsync-editor.json', '.teamsync-agent.json', '.teamsync-history.log',
            '.env', '.env.*', '**/*secret*.json', '**/*secrets*.json')
$current = if (Test-Path $ignore) { Get-Content $ignore } else { @() }
$add = $needed | Where-Object { $current -notcontains $_ }
if ($add) { Add-Content -LiteralPath $ignore -Value $add -Encoding UTF8 }

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

# AGENTS.md is what Codex looks for; CLAUDE.md points at the same file. Both are
# short and defer to the reference, so the rules exist in exactly one place.
$agents = Join-Path $repo 'AGENTS.md'
if (-not (Test-Path $agents)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'AGENTS.project.md') -Destination $agents -Force
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
}

# Without this, a CRLF/LF difference between two Windows machines makes git see
# every line of a file as changed, turning ordinary edits into total conflicts.
$attrs = Join-Path $repo '.gitattributes'
if (-not (Test-Path $attrs)) { Set-Content -LiteralPath $attrs -Value '* text=auto eol=lf' -Encoding UTF8 }

Step 'Initializing git'
git init -b main | Out-Null
if (git config core.hooksPath) { git config core.hooksPath '.git/hooks' }
if ($Me)      { git config user.name  $Me }
if ($MyEmail) { git config user.email $MyEmail }
git add -A
git commit -q -m 'chore: start shared project'

$count = @(git show --stat --name-only --pretty=format: HEAD | Where-Object { $_ }).Count
Write-Host "    $count file(s) captured" -ForegroundColor DarkGray

Step "Creating the private repository: $RepoName"
$desc = if ($Description) { $Description } else { "Shared project: $RepoName" }
gh repo create $RepoName --private --source=. --remote=origin --description $desc --push
if ($LASTEXITCODE -ne 0) { Die 'gh repo create failed. That name may already be taken on your account.' }

$owner = (gh api user --jq '.login')

$friends = @($Friend -split '[,;]' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$invited = @()
foreach ($who in $friends) {
    Step "Inviting $who"
    gh api -X PUT "repos/$owner/$RepoName/collaborators/$who" -f permission=push | Out-Null
    if ($LASTEXITCODE -ne 0) {
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
